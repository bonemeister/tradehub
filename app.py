"""
TradeHub — a locally-hosted trading dashboard.

Covers: stock portfolio, options tracker, crypto tracker, and a day-trade journal.
Live prices come from yfinance (stocks/options, no key needed) and CoinGecko (crypto, no key needed).
All your positions/trades/journal entries are stored locally in trade_hub.db (SQLite) — nothing leaves your machine except the price lookups.

Run with:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""

import csv
import io
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import yfinance as yf
from flask import Flask, g, jsonify, request, render_template

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "trade_hub.db"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,
    cost_basis REAL NOT NULL,
    tier TEXT DEFAULT 'Core',
    notes TEXT DEFAULT '',
    broker TEXT DEFAULT 'manual',
    last_synced_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS option_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    option_type TEXT NOT NULL,      -- 'call' or 'put'
    side TEXT NOT NULL,             -- 'long' or 'short' (e.g. selling puts = short put)
    strike REAL NOT NULL,
    expiry TEXT NOT NULL,           -- YYYY-MM-DD
    contracts REAL NOT NULL,
    premium REAL NOT NULL,          -- premium paid or collected, per share
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS crypto_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,           -- coingecko id, e.g. 'solana'
    display_symbol TEXT NOT NULL,   -- e.g. 'SOL'
    amount REAL NOT NULL,
    cost_basis REAL NOT NULL,       -- total USD cost basis for the amount held
    staked REAL DEFAULT 0,          -- amount currently staked
    apy REAL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT,                 -- long/short
    entry_price REAL,
    exit_price REAL,
    size REAL,
    result TEXT,                    -- win/loss/open
    strategy TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash_balance REAL NOT NULL,
    starting_balance REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,        -- 'stock' or 'crypto'
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    trail_pct REAL,
    highest_price REAL,
    status TEXT DEFAULT 'open',      -- open/closed
    exit_price REAL,
    exit_reason TEXT,
    opened_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    order_type TEXT NOT NULL,        -- market/limit/stop
    qty REAL NOT NULL,
    limit_price REAL,
    stop_price REAL,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    trail_pct REAL,
    status TEXT DEFAULT 'pending',   -- pending/filled/cancelled
    created_at TEXT DEFAULT (datetime('now')),
    filled_at TEXT,
    filled_price REAL
);

CREATE TABLE IF NOT EXISTS paper_equity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now')),
    equity REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS account_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    broker TEXT DEFAULT 'manual',
    action TEXT NOT NULL,            -- deposit/withdrawal/dividend/buy/sell/reinvestment/fee/other
    symbol TEXT,
    quantity REAL,
    amount REAL NOT NULL,
    raw_description TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, raw_description, amount, broker)
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.executescript(SCHEMA)
        _migrate(g.db)
    return g.db


def _migrate(db):
    """Add columns introduced after the initial release, for people upgrading an existing trade_hub.db."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(stock_positions)").fetchall()}
    if "broker" not in cols:
        db.execute("ALTER TABLE stock_positions ADD COLUMN broker TEXT DEFAULT 'manual'")
    if "last_synced_at" not in cols:
        db.execute("ALTER TABLE stock_positions ADD COLUMN last_synced_at TEXT")
    db.commit()


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Simple in-memory cache for price lookups (avoid hammering APIs on refresh)
# ---------------------------------------------------------------------------

_price_cache = {}
CACHE_TTL = 20  # seconds


def cached(key):
    entry = _price_cache.get(key)
    if entry and (time.time() - entry["ts"] < CACHE_TTL):
        return entry["data"]
    return None


def set_cache(key, data):
    _price_cache[key] = {"data": data, "ts": time.time()}


# ---------------------------------------------------------------------------
# Live price routes
# ---------------------------------------------------------------------------

@app.route("/api/quote/<ticker>")
def stock_quote(ticker):
    try:
        return jsonify(_get_stock_price(ticker))
    except Exception as e:
        return jsonify({"ticker": ticker.upper(), "error": str(e)}), 502


def _get_stock_price(ticker):
    ticker = ticker.upper()
    hit = cached(f"stock:{ticker}")
    if hit:
        return hit
    t = yf.Ticker(ticker)
    fast = t.fast_info
    # fast_info.get(...) silently returns None on this yfinance version — direct
    # attribute access is what actually resolves to a real value.
    price = fast.last_price
    prev_close = fast.previous_close
    data = {
        "ticker": ticker,
        "price": round(float(price), 4) if price else None,
        "prev_close": round(float(prev_close), 4) if prev_close else None,
        "change_pct": round((price - prev_close) / prev_close * 100, 2) if price and prev_close else None,
    }
    set_cache(f"stock:{ticker}", data)
    return data


@app.route("/api/options/<ticker>")
def options_chain(ticker):
    """Return available expiries, or a chain for a specific expiry if ?expiry=YYYY-MM-DD is passed."""
    ticker = ticker.upper()
    expiry = request.args.get("expiry")
    try:
        t = yf.Ticker(ticker)
        if not expiry:
            return jsonify({"ticker": ticker, "expiries": list(t.options)})
        chain = t.option_chain(expiry)
        calls = chain.calls[["strike", "lastPrice", "bid", "ask", "impliedVolatility", "openInterest"]].to_dict("records")
        puts = chain.puts[["strike", "lastPrice", "bid", "ask", "impliedVolatility", "openInterest"]].to_dict("records")
        return jsonify({"ticker": ticker, "expiry": expiry, "calls": calls, "puts": puts})
    except Exception as e:
        return jsonify({"ticker": ticker, "error": str(e)}), 502


@app.route("/api/crypto/<symbol>")
def crypto_quote(symbol):
    try:
        return jsonify(_get_crypto_price(symbol))
    except Exception as e:
        return jsonify({"symbol": symbol.lower(), "error": str(e)}), 502


def _get_crypto_price(symbol):
    """symbol = CoinGecko id, e.g. 'solana', 'bitcoin', 'chiliz'"""
    symbol = symbol.lower()
    hit = cached(f"crypto:{symbol}")
    if hit:
        return hit
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": symbol, "vs_currencies": "usd", "include_24hr_change": "true"},
        timeout=8,
    )
    r.raise_for_status()
    payload = r.json().get(symbol, {})
    data = {
        "symbol": symbol,
        "price": payload.get("usd"),
        "change_pct": round(payload.get("usd_24h_change", 0), 2) if payload.get("usd_24h_change") is not None else None,
    }
    set_cache(f"crypto:{symbol}", data)
    return data


# ---------------------------------------------------------------------------
# Stock positions CRUD
# ---------------------------------------------------------------------------

@app.route("/api/stocks", methods=["GET", "POST"])
def stocks_collection():
    db = get_db()
    if request.method == "POST":
        d = request.json
        db.execute(
            "INSERT INTO stock_positions (ticker, shares, cost_basis, tier, notes, broker) VALUES (?,?,?,?,?,?)",
            (d["ticker"].upper(), d["shares"], d["cost_basis"], d.get("tier", "Core"), d.get("notes", ""), d.get("broker", "manual")),
        )
        db.commit()
        return jsonify({"ok": True}), 201
    rows = db.execute("SELECT * FROM stock_positions ORDER BY broker, tier, ticker").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/stocks/<int:pos_id>", methods=["PUT", "DELETE"])
def stocks_item(pos_id):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM stock_positions WHERE id=?", (pos_id,))
        db.commit()
        return jsonify({"ok": True})
    d = request.json
    db.execute(
        "UPDATE stock_positions SET ticker=?, shares=?, cost_basis=?, tier=?, notes=? WHERE id=?",
        (d["ticker"].upper(), d["shares"], d["cost_basis"], d.get("tier", "Core"), d.get("notes", ""), pos_id),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# CSV import — Fidelity / Webull position exports (or any broker's CSV)
# ---------------------------------------------------------------------------

# header names we've seen in Fidelity "Portfolio Positions" and Webull position exports,
# used only to *guess* the right column for the user to confirm — never applied blindly.
TICKER_HINTS = ["symbol", "ticker"]
SHARES_HINTS = ["quantity", "qty", "shares"]
COST_HINTS = ["average cost basis", "avg cost", "cost basis per share", "cost price", "avg price"]
TOTAL_COST_HINTS = ["cost basis total", "total cost", "cost basis"]


def _guess_column(headers, hints):
    lowered = {h: h.lower().strip() for h in headers}
    for hint in hints:
        for h, lh in lowered.items():
            if hint == lh:
                return h
    for hint in hints:
        for h, lh in lowered.items():
            if hint in lh:
                return h
    return None


def _clean_number(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s in ("", "--", "N/A", "n/a"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("$", "").replace(",", "").replace("%", "").strip("()").strip()
    try:
        val = float(s)
        return -val if neg else val
    except ValueError:
        return None


@app.route("/api/import/preview", methods=["POST"])
def import_preview():
    """Takes raw CSV text, returns headers + a few sample rows + best-guess column mapping."""
    d = request.json
    csv_text = d.get("csv_text", "")
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = []
    for i, row in enumerate(reader):
        if i >= 5:
            break
        rows.append(row)
    guess = {
        "ticker_col": _guess_column(headers, TICKER_HINTS),
        "shares_col": _guess_column(headers, SHARES_HINTS),
        "cost_col": _guess_column(headers, COST_HINTS),
        "total_cost_col": _guess_column(headers, TOTAL_COST_HINTS),
    }
    return jsonify({"headers": headers, "sample_rows": rows, "guess": guess, "row_count_sample": len(rows)})


@app.route("/api/import/commit", methods=["POST"])
def import_commit():
    """
    Takes raw CSV text + confirmed column mapping + a broker label.
    Upserts stock_positions for that broker (matched on ticker), and writes a journal
    entry for every new position, size change, or ticker missing from this export
    it detects — this is the 'automatic diff between apps' step.

    Non-destructive by design: a ticker absent from THIS import is only flagged
    (journal entry + "closed" in the response), never auto-deleted from
    stock_positions. Only an explicit DELETE via the UI removes a position.
    """
    d = request.json
    csv_text = d.get("csv_text", "")
    broker = (d.get("broker") or "manual").strip().lower()
    ticker_col = d["ticker_col"]
    shares_col = d["shares_col"]
    cost_col = d.get("cost_col")            # per-share cost basis column
    total_cost_col = d.get("total_cost_col")  # total cost basis column (used if cost_col absent)

    reader = csv.DictReader(io.StringIO(csv_text))
    imported = {}  # ticker -> (shares, cost_per_share)
    for row in reader:
        ticker = (row.get(ticker_col) or "").strip().upper()
        if not ticker:
            continue
        shares = _clean_number(row.get(shares_col))
        if shares is None:
            continue
        cost_per_share = None
        if cost_col:
            cost_per_share = _clean_number(row.get(cost_col))
        if cost_per_share is None and total_cost_col:
            total_cost = _clean_number(row.get(total_cost_col))
            if total_cost is not None and shares:
                cost_per_share = total_cost / shares
        imported[ticker] = {"shares": shares, "cost_basis": cost_per_share or 0}

    db = get_db()
    existing_rows = db.execute(
        "SELECT * FROM stock_positions WHERE broker = ?", (broker,)
    ).fetchall()
    existing = {r["ticker"]: r for r in existing_rows}

    now = datetime.now().strftime("%Y-%m-%d")
    changes = {"added": [], "updated": [], "closed": []}

    for ticker, vals in imported.items():
        if ticker in existing:
            old = existing[ticker]
            if abs(old["shares"] - vals["shares"]) > 1e-9:
                db.execute(
                    "UPDATE stock_positions SET shares=?, cost_basis=?, last_synced_at=? WHERE id=?",
                    (vals["shares"], vals["cost_basis"], now, old["id"]),
                )
                db.execute(
                    """INSERT INTO journal_entries (date, asset, direction, size, result, strategy, notes)
                       VALUES (?,?,?,?,?,?,?)""",
                    (now, ticker, "long" if vals["shares"] > old["shares"] else "short",
                     abs(vals["shares"] - old["shares"]), "open", "csv sync",
                     f"{broker}: shares {old['shares']} -> {vals['shares']}"),
                )
                changes["updated"].append(ticker)
            else:
                db.execute("UPDATE stock_positions SET last_synced_at=? WHERE id=?", (now, old["id"]))
        else:
            db.execute(
                "INSERT INTO stock_positions (ticker, shares, cost_basis, tier, broker, last_synced_at) VALUES (?,?,?,?,?,?)",
                (ticker, vals["shares"], vals["cost_basis"], "Core", broker, now),
            )
            db.execute(
                """INSERT INTO journal_entries (date, asset, direction, size, result, strategy, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (now, ticker, "long", vals["shares"], "open", "csv sync", f"{broker}: new position, {vals['shares']} shares"),
            )
            changes["added"].append(ticker)

    for ticker, old in existing.items():
        if ticker not in imported:
            # Flagged, not deleted: a ticker missing from THIS particular export
            # doesn't necessarily mean the position is actually closed (partial
            # export, different scope, a broker CSV quirk) — only you deleting it
            # via the UI should ever remove real position data. The journal entry
            # still records that it happened so you notice and can check.
            db.execute(
                """INSERT INTO journal_entries (date, asset, direction, size, result, strategy, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (now, ticker, "long", old["shares"], "open", "csv sync", f"{broker}: not present in this export (verify before assuming closed)"),
            )
            changes["closed"].append(ticker)

    db.commit()
    _write_obsidian_snapshot()
    return jsonify({"ok": True, "broker": broker, "changes": changes, "total_imported": len(imported)})


# ---------------------------------------------------------------------------
# Transaction ledger + performance analytics
#
# This is a different data model from the position importer above: it ingests
# a broker's ACTIVITY/HISTORY export (deposits, withdrawals, dividends, buys,
# sells) rather than a point-in-time positions snapshot. That's what lets us
# answer "am I actually ahead" — net contributions vs current value, dividend
# income, and per-symbol cash flow — instead of just "what do I hold now".
# ---------------------------------------------------------------------------

DATE_HINTS = ["run date", "date", "trade date"]
ACTION_HINTS = ["action", "description", "activity", "transaction"]
TX_SYMBOL_HINTS = ["symbol", "ticker"]
AMOUNT_HINTS_TX = ["amount ($)", "amount", "net amount"]
QUANTITY_HINTS_TX = ["quantity", "qty", "shares"]

# cash-sweep / money-market vehicles — excluded from per-symbol trading analysis
# since they're not "positions" in the investing sense, just parked cash
SWEEP_SYMBOLS = {"SPAXX", "FDRXX", "FZFXX", "FCASH", "FDIC", "SPRXX", "CORE"}

_TICKER_IN_PARENS = re.compile(r"\(([A-Z]{1,6})\)")


def _extract_symbol(desc):
    if not desc:
        return None
    for m in _TICKER_IN_PARENS.findall(desc.upper()):
        if m not in ("CASH", "ETF", "USD"):
            return m
    return None


def _classify_transaction(desc):
    """Heuristic classification off the description text. Not perfect for every
    broker's phrasing — the raw ledger is always visible so it can be sanity-checked."""
    u = (desc or "").upper()
    if "TRANSFER" in u and "RECEIVED" in u:
        return "deposit"
    if "TRANSFER" in u and "PAID" in u:
        return "withdrawal"
    if "DIVIDEND" in u:
        return "dividend"
    if "REINVEST" in u:
        return "reinvestment"
    if "YOU BOUGHT" in u or "PURCHASE" in u:
        return "buy"
    if "YOU SOLD" in u or "REDEMPTION" in u:
        return "sell"
    if "FEE" in u or "INTEREST" in u:
        return "fee"
    return "other"


def _strip_leading_blank_lines(csv_text):
    """Fidelity (and others) export activity CSVs with a couple of blank lines
    before the real header row — skip them so DictReader finds the real headers."""
    lines = csv_text.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    return "\n".join(lines[start:])


# ---------------------------------------------------------------------------
# Obsidian snapshot — after every CSV import commits, writes a P&L + allocation
# summary into the vault note so it's visible without opening the app.
# ---------------------------------------------------------------------------

# Optional: point this at a markdown note (e.g. an Obsidian vault file) to get an
# auto-updated P&L + allocation snapshot written there on every CSV import. Unset
# by default — the feature just no-ops until you set the env var yourself.
_obsidian_note_env = os.environ.get("TRADEHUB_SNAPSHOT_NOTE")
OBSIDIAN_TRADEHUB_NOTE = Path(_obsidian_note_env) if _obsidian_note_env else None
SNAPSHOT_START = "<!-- tradehub:snapshot:start -->"
SNAPSHOT_END = "<!-- tradehub:snapshot:end -->"


def _compute_allocation(db, perf=None):
    """Current holdings valued at live prices, split into invested positions vs.
    idle cash sitting in a sweep/money-market vehicle.

    Prefers the stock_positions snapshot (accurate when it's been kept current via
    the positions-import flow). Falls back to deriving open holdings from the
    transaction ledger (perf['symbols']) when stock_positions is empty/stale —
    that table can go stale independently of the transaction ledger, which is the
    one actually kept current every time an activity CSV is imported.
    """
    positions = db.execute("SELECT * FROM stock_positions").fetchall()
    holdings = []
    idle_cash = 0.0
    idle_cash_known = False

    for p in positions:
        ticker = p["ticker"].upper()
        if ticker in SWEEP_SYMBOLS:
            # Money-market sweep funds hold a stable ~$1.00 NAV by design —
            # shares are treated as dollars rather than fetched as a live quote.
            idle_cash += p["shares"]
            idle_cash_known = True
            continue
        price = None
        try:
            price = _get_stock_price(ticker).get("price")
        except Exception:
            price = None
        value = (p["shares"] * price) if price else None
        holdings.append({
            "ticker": ticker,
            "shares": p["shares"],
            "price": price,
            "value": round(value, 2) if value is not None else None,
        })

    if not holdings and perf is not None:
        # stock_positions has nothing usable — fall back to the ledger-derived
        # open positions that _compute_performance already computed.
        for s in perf["symbols"]:
            if s["status"] == "open" and s["qty_net"] and s["qty_net"] > 0:
                holdings.append({
                    "ticker": s["symbol"],
                    "shares": s["qty_net"],
                    "price": s["current_price"],
                    "value": s["unrealized_value"],
                })
        # No sweep-fund row to read from in this fallback path, so idle cash is
        # genuinely unknown rather than guessed — the ledger excludes sweep-symbol
        # transactions entirely, so there's no reliable way to derive it here.

    invested_value = sum(h["value"] for h in holdings if h["value"] is not None)
    total_value = invested_value + (idle_cash if idle_cash_known else 0)
    for h in holdings:
        h["allocation_pct"] = (
            round(h["value"] / total_value * 100, 1) if (h["value"] is not None and total_value > 0) else None
        )
    holdings.sort(key=lambda h: h["value"] or 0, reverse=True)

    return {
        "holdings": holdings,
        "idle_cash": round(idle_cash, 2) if idle_cash_known else None,
        "invested_value": round(invested_value, 2),
        "total_value": round(total_value, 2),
    }


def _allocation_notes(alloc):
    """Plain-language observations, not directives — flags what's true about the
    current allocation and one thing worth weighing, framed as a suggestion to
    consider rather than an instruction."""
    lines = []
    holdings = [h for h in alloc["holdings"] if h["value"]]
    total = alloc["total_value"]
    if not holdings or total <= 0:
        return ["No live-priced holdings to assess yet."]

    top = holdings[0]
    if top["allocation_pct"] and top["allocation_pct"] >= 70:
        lines.append(
            f"**{top['allocation_pct']}% of the account is in {top['ticker']}** — "
            f"heavily concentrated in a single position rather than spread across several."
        )
    if len(holdings) == 1:
        lines.append("Only one live position open right now — no diversification across tickers or strategies yet.")

    if alloc["idle_cash"] is None:
        lines.append(
            "Idle cash balance unknown — the positions snapshot hasn't been (re)imported recently, so the cash-sweep "
            "amount can't be read. Re-run the positions import in the app to see it."
        )
    elif alloc["idle_cash"] >= 10:
        lines.append(
            f"**${alloc['idle_cash']:.2f} is sitting idle** in the cash sweep, earning money-market rates "
            f"rather than being invested."
        )
        if top.get("allocation_pct") and top["allocation_pct"] >= 70:
            lines.append(
                "Worth weighing: putting that idle cash into a broad-market index fund (e.g. VTI or VOO) would add "
                "diversification away from the current concentration without abandoning the existing position — "
                "though staying concentrated is also a coherent choice if the point right now is specifically "
                "watching this one strategy closely before scaling up."
            )

    if not lines:
        lines.append("No concentration or idle-cash flags right now.")
    return lines


def _render_snapshot_markdown(perf, alloc):
    now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    lines = [SNAPSHOT_START, f"## \U0001F4CA Performance Snapshot (auto-updated {now})", ""]
    if alloc["idle_cash"] is None:
        value_suffix = "+"  # total_value excludes an unknown idle-cash amount, so it's a floor, not exact
        idle_str = "idle cash unknown"
    else:
        value_suffix = ""
        idle_str = f"${alloc['idle_cash']:.2f} idle cash"
    lines.append(
        f"**Account value:** ${alloc['total_value']:.2f}{value_suffix}  "
        f"(${alloc['invested_value']:.2f} invested + {idle_str})"
    )
    lines.append(
        f"**Net contributions:** ${perf['net_contributions']:.2f}  |  "
        f"**Total dividends:** ${perf['total_dividends']:.2f}"
    )
    lines.append("")

    if perf["symbols"]:
        lines.append("| Symbol | Status | P&L to date | Dividends |")
        lines.append("|---|---|---|---|")
        for s in perf["symbols"]:
            lines.append(f"| {s['symbol']} | {s['status']} | ${s['total_pl']:.2f} | ${s['dividends']:.2f} |")
        lines.append("")

    lines.append("**Allocation check:**")
    for note in _allocation_notes(alloc):
        lines.append(f"- {note}")
    lines.append("")
    lines.append(SNAPSHOT_END)
    return "\n".join(lines)


def _write_obsidian_snapshot():
    """Best-effort: pulls current performance + allocation and writes/replaces the
    auto-generated block in a markdown note (see TRADEHUB_SNAPSHOT_NOTE in the
    README). No-ops entirely if that env var isn't set. Never raises otherwise — a
    note-write failure must not break the CSV import itself."""
    if OBSIDIAN_TRADEHUB_NOTE is None:
        return
    try:
        db = get_db()
        perf = _compute_performance(db)
        alloc = _compute_allocation(db, perf)
        block = _render_snapshot_markdown(perf, alloc)

        if not OBSIDIAN_TRADEHUB_NOTE.exists():
            OBSIDIAN_TRADEHUB_NOTE.write_text(f"# TradeHub\n\n{block}\n", encoding="utf-8")
            return

        content = OBSIDIAN_TRADEHUB_NOTE.read_text(encoding="utf-8")
        if SNAPSHOT_START in content and SNAPSHOT_END in content:
            pre = content.split(SNAPSHOT_START)[0]
            post = content.split(SNAPSHOT_END)[1]
            new_content = pre + block + post
        else:
            # First run against an existing note: insert right after the H1 title
            # (which sits after the YAML frontmatter), not at the top of the file.
            note_lines = content.split("\n")
            insert_at = len(note_lines)
            for i, line in enumerate(note_lines):
                if line.startswith("# ") and not line.startswith("## "):
                    insert_at = i + 1
                    break
            new_content = "\n".join(note_lines[:insert_at] + ["", block] + note_lines[insert_at:])

        OBSIDIAN_TRADEHUB_NOTE.write_text(new_content, encoding="utf-8")
    except Exception as e:
        print(f"[tradehub] obsidian snapshot write failed (non-fatal): {e}")


@app.route("/api/transactions/import/preview", methods=["POST"])
def tx_import_preview():
    d = request.json
    csv_text = _strip_leading_blank_lines(d.get("csv_text", ""))
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    rows = []
    for i, row in enumerate(reader):
        if i >= 5:
            break
        rows.append(row)
    guess = {
        "date_col": _guess_column(headers, DATE_HINTS),
        "action_col": _guess_column(headers, ACTION_HINTS),
        "symbol_col": _guess_column(headers, TX_SYMBOL_HINTS),
        "amount_col": _guess_column(headers, AMOUNT_HINTS_TX),
        "quantity_col": _guess_column(headers, QUANTITY_HINTS_TX),
    }
    return jsonify({"headers": headers, "sample_rows": rows, "guess": guess})


@app.route("/api/transactions/import/commit", methods=["POST"])
def tx_import_commit():
    d = request.json
    csv_text = _strip_leading_blank_lines(d.get("csv_text", ""))
    broker = (d.get("broker") or "manual").strip().lower()
    date_col = d["date_col"]
    action_col = d["action_col"]
    symbol_col = d.get("symbol_col") or None
    amount_col = d["amount_col"]
    qty_col = d.get("quantity_col") or None

    reader = csv.DictReader(io.StringIO(csv_text))
    db = get_db()
    inserted = 0
    action_counts = {}

    for row in reader:
        date = (row.get(date_col) or "").strip()
        desc = (row.get(action_col) or "").strip()
        amount = _clean_number(row.get(amount_col))
        if not date or not desc or amount is None:
            continue
        symbol = (row.get(symbol_col) or "").strip().upper() if symbol_col else None
        if not symbol:
            symbol = _extract_symbol(desc)
        qty = _clean_number(row.get(qty_col)) if qty_col else None
        action = _classify_transaction(desc)

        cur = db.execute(
            """INSERT OR IGNORE INTO account_transactions (date, broker, action, symbol, quantity, amount, raw_description)
               VALUES (?,?,?,?,?,?,?)""",
            (date, broker, action, symbol, qty, amount, desc),
        )
        if cur.rowcount:
            inserted += 1
            action_counts[action] = action_counts.get(action, 0) + 1

    db.commit()
    _write_obsidian_snapshot()
    return jsonify({"ok": True, "broker": broker, "inserted": inserted, "action_counts": action_counts})


@app.route("/api/transactions", methods=["GET"])
def transactions_list():
    db = get_db()
    rows = db.execute("SELECT * FROM account_transactions ORDER BY date DESC, id DESC LIMIT 200").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/performance", methods=["GET"])
def performance():
    db = get_db()
    return jsonify(_compute_performance(db))


def _compute_performance(db):
    rows = db.execute("SELECT * FROM account_transactions ORDER BY date").fetchall()

    total_deposits = sum(r["amount"] for r in rows if r["action"] == "deposit")
    total_withdrawals = sum(abs(r["amount"]) for r in rows if r["action"] == "withdrawal")
    net_contributions = total_deposits - total_withdrawals

    per_symbol = {}
    total_dividends = 0.0
    for r in rows:
        sym = (r["symbol"] or "").upper()
        if not sym or sym in SWEEP_SYMBOLS:
            continue
        s = per_symbol.setdefault(sym, {"bought": 0.0, "sold": 0.0, "dividends": 0.0, "qty_net": 0.0, "has_qty": False})
        if r["action"] in ("buy", "reinvestment"):
            s["bought"] += abs(r["amount"])
            if r["quantity"] is not None:
                s["qty_net"] += r["quantity"]
                s["has_qty"] = True
        elif r["action"] == "sell":
            s["sold"] += abs(r["amount"])
            if r["quantity"] is not None:
                s["qty_net"] -= abs(r["quantity"])
                s["has_qty"] = True
        elif r["action"] == "dividend":
            s["dividends"] += r["amount"]
            total_dividends += r["amount"]

    symbol_rows = []
    for sym, s in per_symbol.items():
        current_price = None
        try:
            current_price = _get_stock_price(sym).get("price")
        except Exception:
            current_price = None
        unrealized_value = (s["qty_net"] * current_price) if (current_price and s["has_qty"] and s["qty_net"] > 0) else None
        cash_pl = s["sold"] - s["bought"] + s["dividends"]
        total_pl = cash_pl + (unrealized_value or 0)
        status = "unknown"
        if s["has_qty"]:
            status = "closed" if abs(s["qty_net"]) < 1e-4 else "open"
        symbol_rows.append({
            "symbol": sym,
            "bought": round(s["bought"], 2),
            "sold": round(s["sold"], 2),
            "dividends": round(s["dividends"], 2),
            "qty_net": round(s["qty_net"], 4) if s["has_qty"] else None,
            "status": status,
            "current_price": current_price,
            "unrealized_value": round(unrealized_value, 2) if unrealized_value is not None else None,
            "cash_pl_to_date": round(cash_pl, 2),
            "total_pl": round(total_pl, 2),
        })
    symbol_rows.sort(key=lambda r: r["symbol"])

    # Cumulative realized cash flow over time (dividends + sells - buys), excluding
    # deposits/withdrawals (not investment return) and cash-sweep vehicles (not real positions).
    cash_flow_series = []
    running = 0.0
    for r in rows:
        if r["action"] in ("deposit", "withdrawal"):
            continue
        if (r["symbol"] or "").upper() in SWEEP_SYMBOLS:
            continue
        running += r["amount"]
        cash_flow_series.append({"date": r["date"], "cumulative": round(running, 2)})

    return {
        "total_deposits": round(total_deposits, 2),
        "total_withdrawals": round(total_withdrawals, 2),
        "net_contributions": round(net_contributions, 2),
        "total_dividends": round(total_dividends, 2),
        "symbols": symbol_rows,
        "cash_flow_series": cash_flow_series,
        "transaction_count": len(rows),
    }


# ---------------------------------------------------------------------------
# Option positions CRUD
# ---------------------------------------------------------------------------

@app.route("/api/options", methods=["GET", "POST"])
def options_collection():
    db = get_db()
    if request.method == "POST":
        d = request.json
        db.execute(
            """INSERT INTO option_positions
               (ticker, option_type, side, strike, expiry, contracts, premium, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                d["ticker"].upper(), d["option_type"], d["side"], d["strike"],
                d["expiry"], d["contracts"], d["premium"], d.get("notes", ""),
            ),
        )
        db.commit()
        return jsonify({"ok": True}), 201
    rows = db.execute("SELECT * FROM option_positions ORDER BY expiry, ticker").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/options/position/<int:pos_id>", methods=["DELETE"])
def options_item(pos_id):
    db = get_db()
    db.execute("DELETE FROM option_positions WHERE id=?", (pos_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Crypto positions CRUD
# ---------------------------------------------------------------------------

@app.route("/api/crypto-positions", methods=["GET", "POST"])
def crypto_collection():
    db = get_db()
    if request.method == "POST":
        d = request.json
        db.execute(
            """INSERT INTO crypto_positions
               (symbol, display_symbol, amount, cost_basis, staked, apy, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (
                d["symbol"].lower(), d["display_symbol"].upper(), d["amount"], d["cost_basis"],
                d.get("staked", 0), d.get("apy", 0), d.get("notes", ""),
            ),
        )
        db.commit()
        return jsonify({"ok": True}), 201
    rows = db.execute("SELECT * FROM crypto_positions ORDER BY display_symbol").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/crypto-positions/<int:pos_id>", methods=["DELETE"])
def crypto_item(pos_id):
    db = get_db()
    db.execute("DELETE FROM crypto_positions WHERE id=?", (pos_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Journal CRUD
# ---------------------------------------------------------------------------

@app.route("/api/journal", methods=["GET", "POST"])
def journal_collection():
    db = get_db()
    if request.method == "POST":
        d = request.json
        db.execute(
            """INSERT INTO journal_entries
               (date, asset, direction, entry_price, exit_price, size, result, strategy, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                d.get("date", datetime.now().strftime("%Y-%m-%d")), d["asset"], d.get("direction", ""),
                d.get("entry_price"), d.get("exit_price"), d.get("size"),
                d.get("result", "open"), d.get("strategy", ""), d.get("notes", ""),
            ),
        )
        db.commit()
        return jsonify({"ok": True}), 201
    rows = db.execute("SELECT * FROM journal_entries ORDER BY date DESC, id DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/journal/<int:entry_id>", methods=["DELETE"])
def journal_item(entry_id):
    db = get_db()
    db.execute("DELETE FROM journal_entries WHERE id=?", (entry_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Backtest engine — runs a rule set against ONE ticker's own price history.
# No claims of future performance; this reports what the rules would have
# done historically, split into in-sample and out-of-sample so curve-fitting
# is visible rather than hidden.
# ---------------------------------------------------------------------------

def _fetch_ohlcv(ticker, interval, period):
    df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, tuple) or hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    return df


def _run_breakout_strategy(df, lookback, trail_pct, tp_pct, vol_mult, fee_bps):
    """Long-only: enter on a close above the N-bar rolling high (optionally with volume
    confirmation), exit on a trailing stop or optional take-profit."""
    highs = df["High"].rolling(lookback).max().shift(1)
    avg_vol = df["Volume"].rolling(lookback).mean().shift(1) if "Volume" in df.columns else None

    trades = []
    in_pos = False
    entry_price = entry_date = peak = None

    for i in range(len(df)):
        row = df.iloc[i]
        date = df.index[i]
        if not in_pos:
            breakout_level = highs.iloc[i]
            if breakout_level != breakout_level:  # NaN guard
                continue
            vol_ok = True
            if vol_mult and avg_vol is not None:
                av = avg_vol.iloc[i]
                vol_ok = (av == av) and row["Volume"] > vol_mult * av
            if row["Close"] > breakout_level and vol_ok:
                in_pos = True
                entry_price = row["Close"]
                entry_date = date
                peak = entry_price
        else:
            peak = max(peak, row["High"])
            trail_stop = peak * (1 - trail_pct / 100)
            tp_level = entry_price * (1 + tp_pct / 100) if tp_pct else None
            exit_reason = None
            exit_price = None
            if row["Low"] <= trail_stop:
                exit_price = trail_stop
                exit_reason = "trailing_stop"
            elif tp_level and row["High"] >= tp_level:
                exit_price = tp_level
                exit_reason = "take_profit"
            if exit_price:
                fee = (fee_bps / 10000) * 2  # entry + exit
                ret_pct = (exit_price - entry_price) / entry_price * 100 - fee * 100
                trades.append({
                    "entry_date": str(entry_date.date()) if hasattr(entry_date, "date") else str(entry_date),
                    "exit_date": str(date.date()) if hasattr(date, "date") else str(date),
                    "entry_price": round(float(entry_price), 4),
                    "exit_price": round(float(exit_price), 4),
                    "return_pct": round(ret_pct, 3),
                    "exit_reason": exit_reason,
                })
                in_pos = False
    return trades


def _run_ma_cross_strategy(df, fast, slow, trail_pct, fee_bps):
    """Long-only: enter when fast MA crosses above slow MA, exit on trailing stop
    or when fast crosses back below slow."""
    ma_fast = df["Close"].rolling(fast).mean()
    ma_slow = df["Close"].rolling(slow).mean()

    trades = []
    in_pos = False
    entry_price = entry_date = peak = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        date = df.index[i]
        f0, s0 = ma_fast.iloc[i - 1], ma_slow.iloc[i - 1]
        f1, s1 = ma_fast.iloc[i], ma_slow.iloc[i]
        if any(v != v for v in (f0, s0, f1, s1)):
            continue
        if not in_pos:
            if f0 <= s0 and f1 > s1:
                in_pos = True
                entry_price = row["Close"]
                entry_date = date
                peak = entry_price
        else:
            peak = max(peak, row["High"])
            trail_stop = peak * (1 - trail_pct / 100)
            crossed_down = f0 >= s0 and f1 < s1
            exit_price = None
            exit_reason = None
            if row["Low"] <= trail_stop:
                exit_price = trail_stop
                exit_reason = "trailing_stop"
            elif crossed_down:
                exit_price = row["Close"]
                exit_reason = "ma_cross_down"
            if exit_price:
                fee = (fee_bps / 10000) * 2
                ret_pct = (exit_price - entry_price) / entry_price * 100 - fee * 100
                trades.append({
                    "entry_date": str(entry_date.date()) if hasattr(entry_date, "date") else str(entry_date),
                    "exit_date": str(date.date()) if hasattr(date, "date") else str(date),
                    "entry_price": round(float(entry_price), 4),
                    "exit_price": round(float(exit_price), 4),
                    "return_pct": round(ret_pct, 3),
                    "exit_reason": exit_reason,
                })
                in_pos = False
    return trades


def _compute_stats(trades):
    if not trades:
        return {
            "trade_count": 0, "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None,
            "expectancy_pct": None, "profit_factor": None, "max_drawdown_pct": None,
            "sharpe": None, "total_return_pct": None,
        }
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss
    gross_win = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else None)

    # equity curve assuming compounding of each trade's % return, equal sizing
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    curve = [1.0]
    for r in rets:
        equity *= (1 + r / 100)
        curve.append(equity)
        peak = max(peak, equity)
        dd = (equity - peak) / peak * 100
        max_dd = min(max_dd, dd)
    total_return = (equity - 1) * 100

    mean_r = sum(rets) / len(rets)
    variance = sum((r - mean_r) ** 2 for r in rets) / len(rets) if len(rets) > 1 else 0
    stdev = variance ** 0.5
    sharpe = (mean_r / stdev) if stdev > 0 else None

    return {
        "trade_count": len(trades),
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 3),
        "avg_loss_pct": round(avg_loss, 3),
        "expectancy_pct": round(expectancy, 3),
        "profit_factor": round(profit_factor, 2) if profit_factor not in (None, float("inf")) else profit_factor,
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "total_return_pct": round(total_return, 2),
        "equity_curve": [round(c, 4) for c in curve],
    }


@app.route("/api/backtest", methods=["POST"])
def backtest():
    d = request.json
    ticker = d["ticker"].upper()
    interval = d.get("interval", "1d")
    period = d.get("period", "2y")
    strategy = d.get("strategy", "breakout")
    fee_bps = float(d.get("fee_bps", 5))
    oos_pct = float(d.get("oos_pct", 30))

    try:
        df = _fetch_ohlcv(ticker, interval, period)
    except Exception as e:
        return jsonify({"error": f"Data fetch failed: {e}"}), 502
    if df is None or len(df) < 30:
        return jsonify({"error": f"Not enough price history for {ticker} at {interval}/{period}. "
                                  f"Intraday intervals (15m/5m) are limited to ~60 days by the data source — try a shorter period or daily bars."}), 400

    if strategy == "breakout":
        trades = _run_breakout_strategy(
            df,
            lookback=int(d.get("lookback", 20)),
            trail_pct=float(d.get("trail_pct", 5)),
            tp_pct=float(d["tp_pct"]) if d.get("tp_pct") not in (None, "") else None,
            vol_mult=float(d["vol_mult"]) if d.get("vol_mult") not in (None, "") else None,
            fee_bps=fee_bps,
        )
    elif strategy == "ma_cross":
        trades = _run_ma_cross_strategy(
            df,
            fast=int(d.get("fast", 10)),
            slow=int(d.get("slow", 30)),
            trail_pct=float(d.get("trail_pct", 5)),
            fee_bps=fee_bps,
        )
    else:
        return jsonify({"error": f"Unknown strategy '{strategy}'"}), 400

    split_idx = int(len(trades) * (1 - oos_pct / 100))
    in_sample = trades[:split_idx]
    out_sample = trades[split_idx:]

    return jsonify({
        "ticker": ticker, "interval": interval, "period": period, "strategy": strategy,
        "bars_used": len(df),
        "date_range": [str(df.index[0].date()) if hasattr(df.index[0], "date") else str(df.index[0]),
                        str(df.index[-1].date()) if hasattr(df.index[-1], "date") else str(df.index[-1])],
        "combined": _compute_stats(trades),
        "in_sample": _compute_stats(in_sample),
        "out_of_sample": _compute_stats(out_sample),
        "trades": trades[-50:],  # most recent 50 for review
    })


# ---------------------------------------------------------------------------
# Paper trading — forward-tested hypotheticals against LIVE prices. Virtual
# cash only; nothing here places a real order anywhere. A background thread
# checks pending limit/stop orders and open positions' stop-loss / take-profit
# / trailing-stop rules on a fixed interval and fills/exits them automatically.
# ---------------------------------------------------------------------------

PAPER_CHECK_INTERVAL = 30  # seconds between background checks
_paper_lock = threading.Lock()


def _paper_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _ensure_paper_account(db):
    row = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if row is None:
        db.execute(
            "INSERT INTO paper_account (id, cash_balance, starting_balance) VALUES (1, 10000, 10000)"
        )
        db.commit()
        row = db.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    return row


def _live_price(symbol, asset_type):
    try:
        if asset_type == "crypto":
            return _get_crypto_price(symbol).get("price")
        return _get_stock_price(symbol).get("price")
    except Exception:
        return None


def _paper_equity(db):
    account = _ensure_paper_account(db)
    equity = account["cash_balance"]
    for pos in db.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall():
        price = _live_price(pos["symbol"], pos["asset_type"])
        if price:
            equity += price * pos["qty"]
        else:
            equity += pos["entry_price"] * pos["qty"]
    return equity


@app.route("/api/paper/account", methods=["GET"])
def paper_account_get():
    db = get_db()
    account = _ensure_paper_account(db)
    equity = _paper_equity(db)
    return jsonify({
        "cash_balance": account["cash_balance"],
        "starting_balance": account["starting_balance"],
        "equity": round(equity, 2),
        "total_return_pct": round((equity - account["starting_balance"]) / account["starting_balance"] * 100, 2)
                            if account["starting_balance"] else 0,
    })


@app.route("/api/paper/account/reset", methods=["POST"])
def paper_account_reset():
    d = request.json or {}
    starting = float(d.get("starting_balance", 10000))
    db = get_db()
    db.execute("DELETE FROM paper_positions")
    db.execute("DELETE FROM paper_orders")
    db.execute("DELETE FROM paper_equity_log")
    db.execute("DELETE FROM paper_account")
    db.execute("INSERT INTO paper_account (id, cash_balance, starting_balance) VALUES (1, ?, ?)", (starting, starting))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/paper/order", methods=["POST"])
def paper_order_create():
    """Places a buy order. 'market' fills immediately at the current live price;
    'limit'/'stop' sit pending and are checked by the background loop."""
    d = request.json
    symbol = d["symbol"].strip().upper() if d["asset_type"] == "stock" else d["symbol"].strip().lower()
    asset_type = d["asset_type"]
    order_type = d.get("order_type", "market")
    qty = float(d["qty"])
    db = get_db()
    account = _ensure_paper_account(db)

    common = dict(
        stop_loss_pct=float(d["stop_loss_pct"]) if d.get("stop_loss_pct") not in (None, "") else None,
        take_profit_pct=float(d["take_profit_pct"]) if d.get("take_profit_pct") not in (None, "") else None,
        trail_pct=float(d["trail_pct"]) if d.get("trail_pct") not in (None, "") else None,
    )

    if order_type == "market":
        price = _live_price(symbol, asset_type)
        if price is None:
            return jsonify({"error": f"Couldn't get a live price for {symbol} right now."}), 502
        cost = price * qty
        if cost > account["cash_balance"]:
            return jsonify({"error": f"Order costs ${cost:.2f}, only ${account['cash_balance']:.2f} cash available."}), 400
        db.execute("UPDATE paper_account SET cash_balance = cash_balance - ? WHERE id=1", (cost,))
        db.execute(
            """INSERT INTO paper_positions (symbol, asset_type, qty, entry_price, stop_loss_pct, take_profit_pct, trail_pct, highest_price)
               VALUES (?,?,?,?,?,?,?,?)""",
            (symbol, asset_type, qty, price, common["stop_loss_pct"], common["take_profit_pct"], common["trail_pct"], price),
        )
        db.commit()
        return jsonify({"ok": True, "filled": True, "fill_price": price}), 201
    else:
        db.execute(
            """INSERT INTO paper_orders (symbol, asset_type, order_type, qty, limit_price, stop_price, stop_loss_pct, take_profit_pct, trail_pct)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (symbol, asset_type, order_type, qty,
             float(d["limit_price"]) if d.get("limit_price") not in (None, "") else None,
             float(d["stop_price"]) if d.get("stop_price") not in (None, "") else None,
             common["stop_loss_pct"], common["take_profit_pct"], common["trail_pct"]),
        )
        db.commit()
        return jsonify({"ok": True, "filled": False}), 201


@app.route("/api/paper/orders", methods=["GET"])
def paper_orders_list():
    db = get_db()
    rows = db.execute("SELECT * FROM paper_orders WHERE status='pending' ORDER BY created_at DESC").fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/paper/orders/<int:order_id>", methods=["DELETE"])
def paper_order_cancel(order_id):
    db = get_db()
    db.execute("UPDATE paper_orders SET status='cancelled' WHERE id=?", (order_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/paper/positions", methods=["GET"])
def paper_positions_list():
    db = get_db()
    rows = db.execute("SELECT * FROM paper_positions ORDER BY status, opened_at DESC").fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        if d["status"] == "open":
            price = _live_price(d["symbol"], d["asset_type"])
            d["live_price"] = price
            if price:
                d["unrealized_pl"] = round((price - d["entry_price"]) * d["qty"], 2)
                d["unrealized_pl_pct"] = round((price - d["entry_price"]) / d["entry_price"] * 100, 2)
        out.append(d)
    return jsonify(out)


@app.route("/api/paper/positions/<int:pos_id>/close", methods=["POST"])
def paper_position_close(pos_id):
    db = get_db()
    pos = db.execute("SELECT * FROM paper_positions WHERE id=?", (pos_id,)).fetchone()
    if not pos or pos["status"] != "open":
        return jsonify({"error": "Position not found or already closed"}), 404
    price = _live_price(pos["symbol"], pos["asset_type"])
    if price is None:
        return jsonify({"error": "Couldn't get a live price to close at."}), 502
    _close_paper_position(db, pos, price, "manual")
    db.commit()
    return jsonify({"ok": True, "exit_price": price})


def _close_paper_position(db, pos, exit_price, reason):
    proceeds = exit_price * pos["qty"]
    db.execute("UPDATE paper_account SET cash_balance = cash_balance + ? WHERE id=1", (proceeds,))
    db.execute(
        "UPDATE paper_positions SET status='closed', exit_price=?, exit_reason=?, closed_at=datetime('now') WHERE id=?",
        (exit_price, reason, pos["id"]),
    )
    pl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100
    db.execute(
        """INSERT INTO journal_entries (date, asset, direction, entry_price, exit_price, size, result, strategy, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (datetime.now().strftime("%Y-%m-%d"), pos["symbol"], "long", pos["entry_price"], exit_price, pos["qty"],
         "win" if pl_pct > 0 else "loss", "paper", f"paper trade closed ({reason})"),
    )


@app.route("/api/paper/stats", methods=["GET"])
def paper_stats():
    db = get_db()
    closed = db.execute("SELECT * FROM paper_positions WHERE status='closed' ORDER BY closed_at").fetchall()
    trades = [{
        "return_pct": (r["exit_price"] - r["entry_price"]) / r["entry_price"] * 100
    } for r in closed]
    stats = _compute_stats(trades)
    curve = db.execute("SELECT ts, equity FROM paper_equity_log ORDER BY ts").fetchall()
    return jsonify({
        "stats": stats,
        "equity_log": [{"ts": r["ts"], "equity": r["equity"]} for r in curve],
        "closed_count": len(closed),
    })


def _paper_trading_tick():
    """One pass: check pending orders for fills, check open positions for stop/target/trailing exits, log equity."""
    with _paper_lock:
        try:
            db = _paper_db()
            _ensure_paper_account(db)

            pending = db.execute("SELECT * FROM paper_orders WHERE status='pending'").fetchall()
            for order in pending:
                price = _live_price(order["symbol"], order["asset_type"])
                if price is None:
                    continue
                triggered = False
                if order["order_type"] == "limit" and order["limit_price"] is not None and price <= order["limit_price"]:
                    triggered = True
                elif order["order_type"] == "stop" and order["stop_price"] is not None and price >= order["stop_price"]:
                    triggered = True
                if triggered:
                    account = _ensure_paper_account(db)
                    cost = price * order["qty"]
                    if cost <= account["cash_balance"]:
                        db.execute("UPDATE paper_account SET cash_balance = cash_balance - ? WHERE id=1", (cost,))
                        db.execute(
                            """INSERT INTO paper_positions (symbol, asset_type, qty, entry_price, stop_loss_pct, take_profit_pct, trail_pct, highest_price)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (order["symbol"], order["asset_type"], order["qty"], price,
                             order["stop_loss_pct"], order["take_profit_pct"], order["trail_pct"], price),
                        )
                        db.execute(
                            "UPDATE paper_orders SET status='filled', filled_at=datetime('now'), filled_price=? WHERE id=?",
                            (price, order["id"]),
                        )
                    else:
                        db.execute("UPDATE paper_orders SET status='cancelled' WHERE id=?", (order["id"],))

            open_positions = db.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()
            for pos in open_positions:
                price = _live_price(pos["symbol"], pos["asset_type"])
                if price is None:
                    continue
                highest = max(pos["highest_price"] or pos["entry_price"], price)
                if highest != pos["highest_price"]:
                    db.execute("UPDATE paper_positions SET highest_price=? WHERE id=?", (highest, pos["id"]))

                exit_price, reason = None, None
                if pos["stop_loss_pct"] and price <= pos["entry_price"] * (1 - pos["stop_loss_pct"] / 100):
                    exit_price, reason = price, "stop_loss"
                elif pos["take_profit_pct"] and price >= pos["entry_price"] * (1 + pos["take_profit_pct"] / 100):
                    exit_price, reason = price, "take_profit"
                elif pos["trail_pct"] and price <= highest * (1 - pos["trail_pct"] / 100):
                    exit_price, reason = price, "trailing_stop"

                if exit_price:
                    fresh = db.execute("SELECT * FROM paper_positions WHERE id=?", (pos["id"],)).fetchone()
                    if fresh["status"] == "open":
                        _close_paper_position(db, fresh, exit_price, reason)

            equity = _paper_equity(db)
            db.execute("INSERT INTO paper_equity_log (equity) VALUES (?)", (equity,))
            db.commit()
            db.close()
        except Exception as e:
            print(f"[paper trading loop error] {e}")


def _paper_trading_loop():
    while True:
        _paper_trading_tick()
        time.sleep(PAPER_CHECK_INTERVAL)


# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    # use_reloader=False: the reloader spawns a second process, which would double
    # up the background paper-trading loop. Debug error pages still work fine.
    threading.Thread(target=_paper_trading_loop, daemon=True).start()
    app.run(debug=True, port=5000, use_reloader=False)
