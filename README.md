# TradeHub — local trading dashboard

A single dashboard covering stock positions, options tracking, crypto positions, and a trade journal.
Runs entirely on your machine. Live prices come from **yfinance** (stocks + options, no key) and
**CoinGecko** (crypto, no key). Your positions and journal entries are stored in a local SQLite file
(`trade_hub.db`) that's created automatically the first time you run it — nothing you enter gets sent anywhere.

## Setup

Requires Python 3.9+.

```bash
cd tradehub
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Using it

- **Overview** — total value across stocks/crypto, unrealized P/L, options premium at risk, recent journal entries.
- **Stocks** — add positions (ticker, shares, cost basis, risk tier). Prices refresh live via yfinance.
- **Options** — log contracts you're holding (calls/puts, long/short, strike, expiry, premium). There's also a
  live chain lookup: type a ticker, pick an expiry, and see real strikes/bids/asks/IV/open interest pulled
  straight from Yahoo — handy for picking a strike before you log the trade.
- **Crypto** — add positions using the coin's **CoinGecko ID** (not its ticker) — e.g. `solana` for SOL,
  `bitcoin` for BTC, `chiliz` for CHZ. You can look these up at coingecko.com/en/api/documentation or just
  search "coingecko api id <coin name>". Tracks staked amount and APY alongside the live price.
- **Journal** — log trades with entry/exit, size, win/loss/open, strategy tag, and notes. Win rate calculates
  automatically from your closed trades.

## Importing accounts (Fidelity / Webull / any broker CSV)

There's no safe official API for personal Fidelity or Webull account linking, so this uses CSV export/import
instead — you export your positions from the broker, drop the file in, and it syncs.

1. In Fidelity: Positions → **Download** (or Portfolio Positions → export). In Webull: Positions tab → **Export**.
2. On the Stocks tab, click **⇪ Import CSV**, give the account a label (e.g. `fidelity`, `webull`), and upload the file.
3. Confirm the column mapping — it auto-guesses ticker/shares/cost-basis columns from common header names, but
   check them since export formats change between broker versions.
4. Hit **Confirm import**.

Each broker label is tracked separately, so importing your Fidelity and Webull exports won't mix them together.
Re-importing the same broker's CSV later **syncs the changes non-destructively**: new tickers get added, changed
share counts get updated, and anything missing from that particular export gets **flagged** (not deleted) —
a ticker only disappears from your positions if you remove it yourself. That's deliberate: a partial or
oddly-scoped export shouldn't be able to silently erase real position data. Every add/update/flag is logged
to the Journal automatically, so you get a running record of what moved between syncs without typing it in by hand.

## Backtest tab

Runs a rule set against **one specific ticker's own price history** — not a generic model, not a promised
win rate. Two strategies ship out of the box:

- **Breakout + trailing stop** — enters on a close above the N-bar rolling high (optional volume confirmation),
  exits on a trailing stop or optional take-profit.
- **Moving average crossover** — enters when the fast MA crosses above the slow MA, exits on a trailing stop or
  the fast MA crossing back below.

What it reports, and why each stat is there:

- **Expectancy per trade** — `(win% × avg win) − (loss% × avg loss)`. The single most honest number for "does
  this edge exist" — win rate alone is meaningless without it.
- **Win rate, avg win/loss, profit factor** — profit factor > 1.5 is decent, > 2 is strong.
- **Max drawdown** — worst peak-to-trough equity decline over the test.
- **Sharpe (per trade)** — return per unit of volatility across the trade sequence.
- **In-sample vs out-of-sample split** — the results table splits trades chronologically (default: last 30% held
  out). If the strategy only looks good in-sample and falls apart out-of-sample, that's curve-fitting showing
  itself, not real edge.
- **Fee/slippage (bps)** is subtracted from every trade on both sides — a strategy that only works before fees
  isn't a strategy.

Data comes from yfinance. Intraday bars are limited by the source itself — 15m/5m only go back ~60 days,
1h back roughly 1–2 years, daily bars go back much further. If a backtest errors out with "not enough price
history," it's almost always this — drop to a longer bar (1h or 1d) or shorten the period.

This tells you what a rule set *would have done* on real historical data for that ticker. It is not a prediction
and not a guarantee — markets change regime, and a strategy that worked on the last two years of a stock's
history isn't assured to keep working. Use it to falsify bad ideas and size positions sanely, not as a crystal ball.

## Paper Trading tab

Forward-tests hypotheticals against **live** prices with virtual cash — the natural next step after backtesting:
backtest tells you what rules did on history, paper trading tells you what they do going forward, in real time,
without risking money.

- Starts with a virtual $10,000 (changeable on reset).
- **Market orders** fill immediately at the current live price.
- **Limit orders** (fill at/below a price) and **stop orders** (fill at/above a price) sit pending until triggered.
- Every position can carry a **stop-loss %, take-profit %, and/or trailing stop %** — set once at entry, then
  managed automatically.
- A background thread (started when you run `python app.py`) checks every 30 seconds: it fills pending
  limit/stop orders when conditions are met, and closes open positions the moment a stop-loss, take-profit, or
  trailing stop triggers. **The app has to stay running for this automation to work** — close the terminal and
  the checks stop (existing positions/orders are still saved, they just won't auto-manage until you restart it).
- Every fill and every auto-exit gets logged to the main Journal automatically, tagged `paper`.
- The stats panel applies the same expectancy/win-rate/profit-factor/Sharpe/drawdown math as the backtester —
  but computed from trades that actually happened forward-in-time against real prices, not simulated history.

Scope note: this covers stocks and crypto spot positions (long only). It doesn't paper-trade options — pricing
options accurately forward in time needs a real options pricing model (Greeks, IV surface), which is a bigger
build than a stop-loss/take-profit engine. If that'd be useful, it's a reasonable next add.

## Performance tab

This is different from the Stocks-tab CSV import — that one reads a **positions snapshot** (what you hold right
now). This reads your broker's **Activity/History export** (every deposit, withdrawal, dividend, buy, and sell)
so it can answer "am I actually ahead," not just "what do I hold."

In Fidelity: Account → Activity → export/download. In Webull: transaction history → export.

What it computes:

- **Net contributions** — total deposited minus total withdrawn. This is your actual cost basis for the whole
  account, not just one position.
- **Total dividends received** — summed across real positions, excluding cash-sweep vehicles (SPAXX, FDRXX, etc.)
  since those are just parked cash, not an investment decision.
- **Per-symbol cash flow** — for each symbol: total bought, total sold, dividends received, net share count (when
  a quantity column was available), and two P/L numbers:
  - **Cash P/L to date** = money out (sales + dividends) − money in (buys). This is real and accurate regardless
    of whether the position is still open, because it's just cash that has actually moved.
  - **Total P/L** = cash P/L + the live value of any shares still held (fetched automatically for stock/ETF
    tickers). This is the number that answers "am I ahead on this position overall."
- **Full raw ledger** — every imported transaction, so you can sanity-check the auto-classification (it's
  heuristic pattern-matching on the description text, not perfect for every broker's exact phrasing).

Re-uploading the same export later is safe — duplicate rows (same date + description + amount) are automatically
skipped, so you can re-export weekly and just re-upload without double-counting.

This matters most for high-yield options-income funds (synthetic covered-call ETFs and similar): they can pay
a large weekly/monthly distribution while the share price erodes underneath it, so the *total* return (dividends
+ price change) is the honest number, not the distribution rate in isolation. That's exactly what this tab is
built to surface, for whatever tickers you actually hold.

Click **"↻ Refresh all prices"** on the Overview tab any time to pull fresh quotes.

## Optional: auto-post a snapshot to a notes app

Set the `TRADEHUB_SNAPSHOT_NOTE` environment variable to a markdown file path (e.g. an Obsidian vault note),
and every time you import a CSV, TradeHub writes/updates a P&L + allocation summary block there automatically —
no need to open the app to see current numbers. Leave it unset and the feature just does nothing; there's no
default path baked in.

```bash
export TRADEHUB_SNAPSHOT_NOTE="/path/to/your/notes/TradeHub.md"   # macOS/Linux
$env:TRADEHUB_SNAPSHOT_NOTE = "C:\path\to\your\notes\TradeHub.md" # Windows PowerShell
```

## Notes / limitations

- **Options data**: not every ticker has a listed chain in yfinance, and low-volume names can return spotty
  bid/ask. Treat it as a reference, not an execution-grade feed.
- **Rate limits**: yfinance and CoinGecko are free/unofficial or lightly-rate-limited endpoints. Prices are
  cached for 20 seconds per symbol so you don't hammer them on rapid refreshes. If you start seeing errors
  after heavy use, wait a minute.
- **This isn't a broker connection** — you're not placing trades from here, just tracking positions you already
  hold elsewhere (Webull, Fidelity, OKX, etc.) with live pricing layered on top.
- To reset everything, just delete `trade_hub.db` and restart the app.

## Extending it

The Flask routes in `app.py` are small and separated by concern (`/api/stocks`, `/api/options`, `/api/crypto-positions`,
`/api/journal`) — straightforward to add fields (e.g. realized P/L on close, tags, screenshots) if you want to
grow it into something closer to your full research-paper-style trade log.
