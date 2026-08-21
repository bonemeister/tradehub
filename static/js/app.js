// ---------- Tabs ----------
document.querySelectorAll('.rail-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.rail-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('pane-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'paper') loadPaperAll();
    if (tab.dataset.tab === 'performance') loadPerformanceAll();
  });
});

// ---------- Clock ----------
function tickClock(){
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
}
tickClock(); setInterval(tickClock, 1000);

// ---------- Modal handling ----------
const backdrop = document.getElementById('modal-backdrop');
document.querySelectorAll('[data-open]').forEach(btn => {
  btn.addEventListener('click', () => openModal(btn.dataset.open));
});
document.querySelectorAll('[data-close]').forEach(btn => {
  btn.addEventListener('click', closeModals);
});
backdrop.addEventListener('click', closeModals);

function openModal(id){
  backdrop.classList.add('show');
  document.getElementById(id).classList.add('show');
}
function closeModals(){
  backdrop.classList.remove('show');
  document.querySelectorAll('.modal').forEach(m => m.classList.remove('show'));
}

// ---------- Generic fetch helpers ----------
async function getJSON(url){
  const r = await fetch(url);
  return r.json();
}
async function postJSON(url, body){
  const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
  return r.json();
}
async function del(url){
  const r = await fetch(url, { method:'DELETE' });
  return r.json();
}
function fmt(n, decimals=2){
  if (n === null || n === undefined || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}
function money(n, decimals=2){
  if (n === null || n === undefined || isNaN(n)) return '—';
  const sign = n < 0 ? '-' : '';
  return sign + '$' + fmt(Math.abs(n), decimals);
}
function pctClass(n){ return n > 0 ? 'up' : (n < 0 ? 'down' : ''); }
function pctStr(n){
  if (n === null || n === undefined || isNaN(n)) return '—';
  return (n > 0 ? '+' : '') + fmt(n) + '%';
}

// ---------- Form submit wiring ----------
document.querySelectorAll('form[data-submit]').forEach(form => {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const endpoint = form.dataset.submit;
    const data = Object.fromEntries(new FormData(form).entries());
    await postJSON('/api/' + endpoint, data);
    form.reset();
    closeModals();
    loadAll();
  });
});
// default journal date to today when opened
document.querySelector('[data-open="journal-form"]').addEventListener('click', () => {
  document.querySelector('#journal-form input[name=date]').value = new Date().toISOString().slice(0,10);
});

// ============================================================
// STOCKS
// ============================================================
let stockCache = [];

async function loadStocks(){
  stockCache = await getJSON('/api/stocks');
  const tbody = document.querySelector('#stocks-table tbody');
  if (!stockCache.length){
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No positions yet. Add one to get started.</td></tr>`;
  } else {
    tbody.innerHTML = stockCache.map(p => `
      <tr data-id="${p.id}">
        <td><strong>${p.ticker}</strong></td>
        <td>${p.broker || 'manual'}</td>
        <td>${p.tier}</td>
        <td>${fmt(p.shares, 4)}</td>
        <td>${money(p.cost_basis)}</td>
        <td class="live-price">…</td>
        <td class="live-chg">…</td>
        <td class="live-value">…</td>
        <td class="live-pl">…</td>
        <td><button class="btn danger" data-del-stock="${p.id}">✕</button></td>
      </tr>`).join('');
    wireStockDeletes();
  }
  refreshStockPrices();
}

function wireStockDeletes(){
  document.querySelectorAll('[data-del-stock]').forEach(b => {
    b.addEventListener('click', async () => { await del('/api/stocks/' + b.dataset.delStock); loadStocks(); loadOverview(); });
  });
}

async function refreshStockPrices(){
  let totalValue = 0, totalCost = 0;
  for (const p of stockCache){
    const row = document.querySelector(`#stocks-table tr[data-id="${p.id}"]`);
    if (!row) continue;
    try {
      const q = await getJSON('/api/quote/' + p.ticker);
      if (q.error || q.price == null){
        row.querySelector('.live-price').textContent = 'n/a';
        row.querySelector('.live-chg').textContent = '—';
        row.querySelector('.live-value').textContent = '—';
        row.querySelector('.live-pl').textContent = '—';
        continue;
      }
      const value = q.price * p.shares;
      const cost = p.cost_basis * p.shares;
      const pl = value - cost;
      const plPct = cost ? (pl / cost * 100) : 0;
      totalValue += value; totalCost += cost;
      row.querySelector('.live-price').textContent = money(q.price);
      row.querySelector('.live-chg').innerHTML = `<span class="${pctClass(q.change_pct)}">${pctStr(q.change_pct)}</span>`;
      row.querySelector('.live-value').textContent = money(value);
      row.querySelector('.live-pl').innerHTML = `<span class="${pctClass(pl)}">${money(pl)} (${pctStr(plPct)})</span>`;
    } catch(err){
      row.querySelector('.live-price').textContent = 'err';
    }
  }
  document.getElementById('kpi-stocks').textContent = money(totalValue);
  const pl = totalValue - totalCost;
  const plPct = totalCost ? (pl/totalCost*100) : 0;
  const kpiSub = document.getElementById('kpi-stocks-pl');
  kpiSub.innerHTML = `<span class="${pctClass(pl)}">${money(pl)} (${pctStr(plPct)})</span>`;
}

// ============================================================
// OPTIONS
// ============================================================
let optionCache = [];

async function loadOptions(){
  optionCache = await getJSON('/api/options');
  const tbody = document.querySelector('#options-table tbody');
  if (!optionCache.length){
    tbody.innerHTML = `<tr class="empty-row"><td colspan="9">No contracts logged yet.</td></tr>`;
  } else {
    let totalNotional = 0;
    tbody.innerHTML = optionCache.map(o => {
      const notional = o.premium * o.contracts * 100;
      totalNotional += notional;
      return `
      <tr>
        <td><strong>${o.ticker}</strong></td>
        <td>${o.option_type}</td>
        <td>${o.side}</td>
        <td>${money(o.strike)}</td>
        <td>${o.expiry}</td>
        <td>${o.contracts}</td>
        <td>${money(o.premium)}</td>
        <td>${money(notional)}</td>
        <td><button class="btn danger" data-del-opt="${o.id}">✕</button></td>
      </tr>`;
    }).join('');
    document.getElementById('kpi-options').textContent = money(totalNotional);
    document.getElementById('kpi-options-count').textContent = optionCache.length + ' open contract' + (optionCache.length===1?'':'s');
    document.querySelectorAll('[data-del-opt]').forEach(b => {
      b.addEventListener('click', async () => { await del('/api/options/position/' + b.dataset.delOpt); loadOptions(); });
    });
  }
}

document.getElementById('load-chain').addEventListener('click', async () => {
  const ticker = document.getElementById('chain-ticker').value.trim().toUpperCase();
  if (!ticker) return;
  const expirySelect = document.getElementById('chain-expiry');
  const resultsDiv = document.getElementById('chain-results');
  expirySelect.innerHTML = `<option>Loading…</option>`;
  const data = await getJSON('/api/options/' + ticker);
  if (data.error || !data.expiries){
    expirySelect.innerHTML = `<option>Error / no options for ${ticker}</option>`;
    resultsDiv.innerHTML = '';
    return;
  }
  expirySelect.innerHTML = data.expiries.map(e => `<option value="${e}">${e}</option>`).join('');
  loadChainForExpiry(ticker, data.expiries[0]);
  expirySelect.onchange = () => loadChainForExpiry(ticker, expirySelect.value);
});

async function loadChainForExpiry(ticker, expiry){
  const resultsDiv = document.getElementById('chain-results');
  resultsDiv.innerHTML = `<div style="padding:14px;color:var(--text-faint);font-family:var(--mono);font-size:12px;">Loading chain…</div>`;
  const data = await getJSON(`/api/options/${ticker}?expiry=${expiry}`);
  if (data.error){ resultsDiv.innerHTML = `<div style="padding:14px;">Error: ${data.error}</div>`; return; }
  const rows = (arr, label) => arr.slice(0, 8).map(r => `
    <tr>
      <td>${label}</td><td>${money(r.strike)}</td><td>${money(r.lastPrice)}</td>
      <td>${money(r.bid)}</td><td>${money(r.ask)}</td>
      <td>${r.impliedVolatility ? fmt(r.impliedVolatility*100,1)+'%' : '—'}</td>
      <td>${r.openInterest ?? '—'}</td>
    </tr>`).join('');
  resultsDiv.innerHTML = `
    <table class="data-table">
      <thead><tr><th>Type</th><th>Strike</th><th>Last</th><th>Bid</th><th>Ask</th><th>IV</th><th>OI</th></tr></thead>
      <tbody>${rows(data.calls, 'Call')}${rows(data.puts, 'Put')}</tbody>
    </table>`;
}

// ============================================================
// CRYPTO
// ============================================================
let cryptoCache = [];

async function loadCrypto(){
  cryptoCache = await getJSON('/api/crypto-positions');
  const tbody = document.querySelector('#crypto-table tbody');
  if (!cryptoCache.length){
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No crypto positions yet.</td></tr>`;
  } else {
    tbody.innerHTML = cryptoCache.map(p => `
      <tr data-id="${p.id}" data-symbol="${p.symbol}">
        <td><strong>${p.display_symbol}</strong></td>
        <td>${fmt(p.amount, 4)}</td>
        <td>${fmt(p.staked, 4)}</td>
        <td>${fmt(p.apy, 2)}%</td>
        <td>${money(p.cost_basis)}</td>
        <td class="live-price">…</td>
        <td class="live-chg">…</td>
        <td class="live-value">…</td>
        <td class="live-pl">…</td>
        <td><button class="btn danger" data-del-crypto="${p.id}">✕</button></td>
      </tr>`).join('');
    document.querySelectorAll('[data-del-crypto]').forEach(b => {
      b.addEventListener('click', async () => { await del('/api/crypto-positions/' + b.dataset.delCrypto); loadCrypto(); loadOverview(); });
    });
  }
  refreshCryptoPrices();
}

async function refreshCryptoPrices(){
  let totalValue = 0, totalCost = 0;
  for (const p of cryptoCache){
    const row = document.querySelector(`#crypto-table tr[data-id="${p.id}"]`);
    if (!row) continue;
    try {
      const q = await getJSON('/api/crypto/' + p.symbol);
      if (q.error || q.price == null){
        row.querySelector('.live-price').textContent = 'n/a';
        continue;
      }
      const value = q.price * p.amount;
      const pl = value - p.cost_basis;
      const plPct = p.cost_basis ? (pl / p.cost_basis * 100) : 0;
      totalValue += value; totalCost += p.cost_basis;
      row.querySelector('.live-price').textContent = money(q.price, 4);
      row.querySelector('.live-chg').innerHTML = `<span class="${pctClass(q.change_pct)}">${pctStr(q.change_pct)}</span>`;
      row.querySelector('.live-value').textContent = money(value);
      row.querySelector('.live-pl').innerHTML = `<span class="${pctClass(pl)}">${money(pl)} (${pctStr(plPct)})</span>`;
    } catch(err){
      row.querySelector('.live-price').textContent = 'err';
    }
  }
  document.getElementById('kpi-crypto').textContent = money(totalValue);
  const pl = totalValue - totalCost;
  const plPct = totalCost ? (pl/totalCost*100) : 0;
  document.getElementById('kpi-crypto-pl').innerHTML = `<span class="${pctClass(pl)}">${money(pl)} (${pctStr(plPct)})</span>`;
}

// ============================================================
// JOURNAL
// ============================================================
async function loadJournal(){
  const entries = await getJSON('/api/journal');
  const tbody = document.querySelector('#journal-table tbody');
  if (!entries.length){
    tbody.innerHTML = `<tr class="empty-row"><td colspan="10">No trades logged yet.</td></tr>`;
  } else {
    tbody.innerHTML = entries.map(e => `
      <tr>
        <td>${e.date}</td>
        <td><strong>${e.asset}</strong></td>
        <td>${e.direction || '—'}</td>
        <td>${e.entry_price != null ? money(e.entry_price, 4) : '—'}</td>
        <td>${e.exit_price != null ? money(e.exit_price, 4) : '—'}</td>
        <td>${e.size != null ? fmt(e.size, 4) : '—'}</td>
        <td><span class="${e.result==='win'?'up':(e.result==='loss'?'down':'')}">${e.result}</span></td>
        <td>${e.strategy || '—'}</td>
        <td>${e.notes || '—'}</td>
        <td><button class="btn danger" data-del-journal="${e.id}">✕</button></td>
      </tr>`).join('');
    document.querySelectorAll('[data-del-journal]').forEach(b => {
      b.addEventListener('click', async () => { await del('/api/journal/' + b.dataset.delJournal); loadJournal(); loadOverview(); });
    });
  }
  const wins = entries.filter(e => e.result === 'win').length;
  const losses = entries.filter(e => e.result === 'loss').length;
  const open = entries.filter(e => e.result === 'open').length;
  const winRate = (wins + losses) ? (wins / (wins + losses) * 100) : 0;
  document.getElementById('journal-stats').innerHTML = `
    <div class="kpi"><div class="kpi-label">Win rate</div><div class="kpi-value">${fmt(winRate,1)}%</div><div class="kpi-sub">${wins}W / ${losses}L</div></div>
    <div class="kpi"><div class="kpi-label">Open trades</div><div class="kpi-value">${open}</div><div class="kpi-sub">tracked positions</div></div>
    <div class="kpi"><div class="kpi-label">Total logged</div><div class="kpi-value">${entries.length}</div><div class="kpi-sub">all-time entries</div></div>`;
}

// ============================================================
// OVERVIEW
// ============================================================
async function loadOverview(){
  const entries = await getJSON('/api/journal');
  const recent = entries.slice(0, 6);
  const div = document.getElementById('overview-journal');
  if (!recent.length){
    div.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No trades logged yet — head to the Journal tab.</td></tr></tbody></table>`;
  } else {
    div.innerHTML = `<table class="data-table">
      <thead><tr><th>Date</th><th>Asset</th><th>Dir</th><th>Result</th><th>Strategy</th></tr></thead>
      <tbody>${recent.map(e => `
        <tr><td>${e.date}</td><td><strong>${e.asset}</strong></td><td>${e.direction||'—'}</td>
        <td><span class="${e.result==='win'?'up':(e.result==='loss'?'down':'')}">${e.result}</span></td>
        <td>${e.strategy||'—'}</td></tr>`).join('')}</tbody></table>`;
  }
}

// ============================================================
// CSV IMPORT
// ============================================================
let importCsvText = null;
let importHeaders = [];

function resetImportModal(){
  document.getElementById('import-step-upload').style.display = '';
  document.getElementById('import-step-map').style.display = 'none';
  document.getElementById('import-step-result').style.display = 'none';
  document.getElementById('import-file').value = '';
}
document.querySelector('[data-open="import-form"]').addEventListener('click', resetImportModal);

document.getElementById('import-preview-btn').addEventListener('click', async () => {
  const fileInput = document.getElementById('import-file');
  if (!fileInput.files.length){ alert('Choose a CSV file first.'); return; }
  const text = await fileInput.files[0].text();
  importCsvText = text;
  const preview = await postJSON('/api/import/preview', { csv_text: text });
  importHeaders = preview.headers;

  const fillSelect = (sel, includeBlank) => {
    sel.innerHTML = (includeBlank ? '<option value="">— none —</option>' : '') +
      importHeaders.map(h => `<option value="${h}">${h}</option>`).join('');
  };
  fillSelect(document.getElementById('map-ticker'), false);
  fillSelect(document.getElementById('map-shares'), false);
  fillSelect(document.getElementById('map-cost'), true);
  fillSelect(document.getElementById('map-total-cost'), true);

  if (preview.guess.ticker_col) document.getElementById('map-ticker').value = preview.guess.ticker_col;
  if (preview.guess.shares_col) document.getElementById('map-shares').value = preview.guess.shares_col;
  if (preview.guess.cost_col) document.getElementById('map-cost').value = preview.guess.cost_col;
  if (preview.guess.total_cost_col) document.getElementById('map-total-cost').value = preview.guess.total_cost_col;

  const sampleDiv = document.getElementById('import-sample');
  if (preview.sample_rows.length){
    sampleDiv.innerHTML = `<table class="data-table"><thead><tr>${importHeaders.map(h=>`<th>${h}</th>`).join('')}</tr></thead>
      <tbody>${preview.sample_rows.map(r => `<tr>${importHeaders.map(h=>`<td>${r[h] ?? ''}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  } else {
    sampleDiv.innerHTML = '';
  }

  document.getElementById('import-step-upload').style.display = 'none';
  document.getElementById('import-step-map').style.display = '';
});

document.getElementById('import-back-btn').addEventListener('click', () => {
  document.getElementById('import-step-upload').style.display = '';
  document.getElementById('import-step-map').style.display = 'none';
});

document.getElementById('import-commit-btn').addEventListener('click', async () => {
  const broker = (document.getElementById('import-broker').value || 'manual').trim().toLowerCase();
  const body = {
    csv_text: importCsvText,
    broker: broker,
    ticker_col: document.getElementById('map-ticker').value,
    shares_col: document.getElementById('map-shares').value,
    cost_col: document.getElementById('map-cost').value || null,
    total_cost_col: document.getElementById('map-total-cost').value || null,
  };
  const result = await postJSON('/api/import/commit', body);
  const resultBody = document.getElementById('import-result-body');
  if (result.error){
    resultBody.innerHTML = `<p>Import failed: ${result.error}</p>`;
  } else {
    const c = result.changes;
    resultBody.innerHTML = `
      <p><strong>${result.total_imported}</strong> positions read from <strong>${result.broker}</strong>.</p>
      <ul style="font-family:var(--mono); font-size:12.5px; line-height:1.9; padding-left:18px;">
        <li class="up">${c.added.length} added${c.added.length ? ': ' + c.added.join(', ') : ''}</li>
        <li>${c.updated.length} updated${c.updated.length ? ': ' + c.updated.join(', ') : ''}</li>
        <li class="down">${c.closed.length} closed / no longer present${c.closed.length ? ': ' + c.closed.join(', ') : ''}</li>
      </ul>
      <p style="color:var(--text-faint); font-size:12px;">Changes were logged to your Journal automatically.</p>`;
  }
  document.getElementById('import-step-map').style.display = 'none';
  document.getElementById('import-step-result').style.display = '';
  loadAll();
});

// ============================================================
// BACKTEST
// ============================================================
document.getElementById('bt-strategy').addEventListener('change', () => {
  const isBreakout = document.getElementById('bt-strategy').value === 'breakout';
  document.getElementById('bt-params-breakout').style.display = isBreakout ? '' : 'none';
  document.getElementById('bt-params-ma').style.display = isBreakout ? 'none' : '';
});

function statOrDash(v, suffix=''){ return (v === null || v === undefined) ? '—' : (fmt(v) + suffix); }

function renderSplitRow(label, s){
  return `<tr>
    <td><strong>${label}</strong></td>
    <td>${s.trade_count}</td>
    <td>${statOrDash(s.win_rate,'%')}</td>
    <td class="${s.expectancy_pct>0?'up':(s.expectancy_pct<0?'down':'')}">${statOrDash(s.expectancy_pct,'%')}</td>
    <td>${s.profit_factor === null ? '—' : (s.profit_factor === Infinity ? '∞' : fmt(s.profit_factor))}</td>
    <td class="down">${statOrDash(s.max_drawdown_pct,'%')}</td>
    <td>${statOrDash(s.sharpe)}</td>
    <td class="${s.total_return_pct>0?'up':(s.total_return_pct<0?'down':'')}">${statOrDash(s.total_return_pct,'%')}</td>
  </tr>`;
}

function drawEquityCurve(curve){
  const canvas = document.getElementById('bt-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0,0,w,h);
  if (!curve || curve.length < 2){
    ctx.fillStyle = '#565e66'; ctx.font = '13px monospace';
    ctx.fillText('Not enough trades to plot.', 20, h/2);
    return;
  }
  const min = Math.min(...curve), max = Math.max(...curve);
  const pad = 20;
  const xStep = (w - pad*2) / (curve.length - 1);
  const yScale = (h - pad*2) / ((max - min) || 1);
  const yOf = v => h - pad - (v - min) * yScale;

  // zero/start line
  ctx.strokeStyle = '#24292f'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, yOf(1)); ctx.lineTo(w-pad, yOf(1)); ctx.stroke();

  ctx.strokeStyle = curve[curve.length-1] >= 1 ? '#3ecf8e' : '#e0575b';
  ctx.lineWidth = 2;
  ctx.beginPath();
  curve.forEach((v, i) => {
    const x = pad + i * xStep, y = yOf(v);
    if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();
}

document.getElementById('bt-run').addEventListener('click', async () => {
  const btn = document.getElementById('bt-run');
  const errorDiv = document.getElementById('bt-error');
  const resultsDiv = document.getElementById('bt-results');
  errorDiv.style.display = 'none';
  resultsDiv.style.display = 'none';
  btn.disabled = true; btn.textContent = 'Running…';

  const strategy = document.getElementById('bt-strategy').value;
  const body = {
    ticker: document.getElementById('bt-ticker').value.trim(),
    interval: document.getElementById('bt-interval').value,
    period: document.getElementById('bt-period').value,
    strategy: strategy,
    fee_bps: document.getElementById('bt-fee').value,
    oos_pct: document.getElementById('bt-oos').value,
  };
  if (strategy === 'breakout'){
    body.lookback = document.getElementById('bt-lookback').value;
    body.trail_pct = document.getElementById('bt-trail').value;
    body.tp_pct = document.getElementById('bt-tp').value || null;
    body.vol_mult = document.getElementById('bt-volmult').value || null;
  } else {
    body.fast = document.getElementById('bt-fast').value;
    body.slow = document.getElementById('bt-slow').value;
    body.trail_pct = document.getElementById('bt-trail-ma').value;
  }

  try {
    const res = await postJSON('/api/backtest', body);
    btn.disabled = false; btn.textContent = 'Run backtest';
    if (res.error){
      errorDiv.textContent = res.error;
      errorDiv.style.display = '';
      return;
    }
    const c = res.combined;
    document.getElementById('bt-expectancy').innerHTML = `<span class="${c.expectancy_pct>0?'up':(c.expectancy_pct<0?'down':'')}">${statOrDash(c.expectancy_pct,'%')}</span>`;
    document.getElementById('bt-tradecount').textContent = `${c.trade_count} trades · ${res.date_range[0]} → ${res.date_range[1]}`;
    document.getElementById('bt-winrate').textContent = statOrDash(c.win_rate,'%');
    document.getElementById('bt-avgs').textContent = `avg win ${statOrDash(c.avg_win_pct,'%')} / avg loss ${statOrDash(c.avg_loss_pct,'%')}`;
    document.getElementById('bt-pf').textContent = c.profit_factor === null ? '—' : (c.profit_factor === Infinity ? '∞' : fmt(c.profit_factor));
    document.getElementById('bt-sharpe').textContent = `Sharpe (per trade) ${statOrDash(c.sharpe)} · max DD ${statOrDash(c.max_drawdown_pct,'%')}`;

    document.getElementById('bt-split-table').innerHTML = `
      <table class="data-table">
        <thead><tr><th>Set</th><th>Trades</th><th>Win%</th><th>Expectancy</th><th>Profit Factor</th><th>Max DD</th><th>Sharpe</th><th>Total Return</th></tr></thead>
        <tbody>
          ${renderSplitRow('In-sample', res.in_sample)}
          ${renderSplitRow('Out-of-sample', res.out_of_sample)}
        </tbody>
      </table>`;

    drawEquityCurve(c.equity_curve);

    const tradesDiv = document.getElementById('bt-trades-table');
    if (!res.trades.length){
      tradesDiv.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No trades triggered with these rules over this period.</td></tr></tbody></table>`;
    } else {
      tradesDiv.innerHTML = `<table class="data-table">
        <thead><tr><th>Entry date</th><th>Entry</th><th>Exit date</th><th>Exit</th><th>Return</th><th>Exit reason</th></tr></thead>
        <tbody>${res.trades.map(t => `
          <tr>
            <td>${t.entry_date}</td><td>${money(t.entry_price)}</td>
            <td>${t.exit_date}</td><td>${money(t.exit_price)}</td>
            <td class="${t.return_pct>0?'up':'down'}">${pctStr(t.return_pct)}</td>
            <td>${t.exit_reason}</td>
          </tr>`).join('')}</tbody></table>`;
    }
    resultsDiv.style.display = '';
  } catch (err){
    btn.disabled = false; btn.textContent = 'Run backtest';
    errorDiv.textContent = 'Request failed: ' + err;
    errorDiv.style.display = '';
  }
});

// ============================================================
// PAPER TRADING
// ============================================================
document.getElementById('paper-order-type').addEventListener('change', () => {
  const type = document.getElementById('paper-order-type').value;
  const row = document.getElementById('paper-trigger-row');
  const limitLabel = document.getElementById('paper-limit-label');
  const stopLabel = document.getElementById('paper-stop-label');
  if (type === 'market'){
    row.style.display = 'none';
  } else {
    row.style.display = '';
    limitLabel.style.display = type === 'limit' ? '' : 'none';
    stopLabel.style.display = type === 'stop' ? '' : 'none';
  }
});

async function loadPaperAccount(){
  const a = await getJSON('/api/paper/account');
  document.getElementById('paper-cash').textContent = money(a.cash_balance);
  document.getElementById('paper-equity').textContent = money(a.equity);
  document.getElementById('paper-return').innerHTML = `<span class="${pctClass(a.total_return_pct)}">${pctStr(a.total_return_pct)}</span>`;
}

async function loadPaperPositions(){
  const positions = await getJSON('/api/paper/positions');
  const open = positions.filter(p => p.status === 'open');
  const closed = positions.filter(p => p.status === 'closed');

  const posDiv = document.getElementById('paper-positions-table');
  if (!open.length){
    posDiv.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No open paper positions.</td></tr></tbody></table>`;
  } else {
    posDiv.innerHTML = `<table class="data-table">
      <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Live</th><th>Unrealized P/L</th><th>SL/TP/Trail</th><th></th></tr></thead>
      <tbody>${open.map(p => `
        <tr>
          <td><strong>${p.symbol.toUpperCase()}</strong></td>
          <td>${fmt(p.qty,4)}</td>
          <td>${money(p.entry_price,4)}</td>
          <td>${p.live_price != null ? money(p.live_price,4) : 'n/a'}</td>
          <td>${p.unrealized_pl != null ? `<span class="${pctClass(p.unrealized_pl)}">${money(p.unrealized_pl)} (${pctStr(p.unrealized_pl_pct)})</span>` : '—'}</td>
          <td style="font-size:11px;color:var(--text-faint);">${p.stop_loss_pct?`SL ${p.stop_loss_pct}%`:''} ${p.take_profit_pct?`TP ${p.take_profit_pct}%`:''} ${p.trail_pct?`Tr ${p.trail_pct}%`:''}</td>
          <td><button class="btn danger" data-close-paper="${p.id}">Close</button></td>
        </tr>`).join('')}</tbody></table>`;
    document.querySelectorAll('[data-close-paper]').forEach(b => {
      b.addEventListener('click', async () => { await postJSON('/api/paper/positions/' + b.dataset.closePaper + '/close', {}); loadPaperAll(); });
    });
  }

  const closedDiv = document.getElementById('paper-closed-table');
  if (!closed.length){
    closedDiv.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No closed paper trades yet.</td></tr></tbody></table>`;
  } else {
    closedDiv.innerHTML = `<table class="data-table">
      <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Return</th><th>Reason</th><th>Closed</th></tr></thead>
      <tbody>${closed.map(p => {
        const ret = (p.exit_price - p.entry_price) / p.entry_price * 100;
        return `<tr>
          <td><strong>${p.symbol.toUpperCase()}</strong></td>
          <td>${fmt(p.qty,4)}</td>
          <td>${money(p.entry_price,4)}</td>
          <td>${money(p.exit_price,4)}</td>
          <td class="${pctClass(ret)}">${pctStr(ret)}</td>
          <td>${p.exit_reason}</td>
          <td>${p.closed_at}</td>
        </tr>`;
      }).join('')}</tbody></table>`;
  }
}

async function loadPaperOrders(){
  const orders = await getJSON('/api/paper/orders');
  const div = document.getElementById('paper-orders-table');
  if (!orders.length){
    div.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No pending orders.</td></tr></tbody></table>`;
  } else {
    div.innerHTML = `<table class="data-table">
      <thead><tr><th>Symbol</th><th>Type</th><th>Qty</th><th>Trigger</th><th></th></tr></thead>
      <tbody>${orders.map(o => `
        <tr>
          <td><strong>${o.symbol.toUpperCase()}</strong></td>
          <td>${o.order_type}</td>
          <td>${fmt(o.qty,4)}</td>
          <td>${money(o.limit_price ?? o.stop_price, 4)}</td>
          <td><button class="btn danger" data-cancel-paper="${o.id}">Cancel</button></td>
        </tr>`).join('')}</tbody></table>`;
    document.querySelectorAll('[data-cancel-paper]').forEach(b => {
      b.addEventListener('click', async () => { await del('/api/paper/orders/' + b.dataset.cancelPaper); loadPaperOrders(); });
    });
  }
}

function drawPaperEquity(log){
  const canvas = document.getElementById('paper-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0,0,w,h);
  if (!log || log.length < 2){
    ctx.fillStyle = '#565e66'; ctx.font = '13px monospace';
    ctx.fillText('Equity history builds up as the background loop runs.', 20, h/2);
    return;
  }
  const values = log.map(p => p.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const pad = 20;
  const xStep = (w - pad*2) / (values.length - 1);
  const yScale = (h - pad*2) / ((max - min) || 1);
  const yOf = v => h - pad - (v - min) * yScale;
  ctx.strokeStyle = values[values.length-1] >= values[0] ? '#3ecf8e' : '#e0575b';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v,i) => { const x = pad + i*xStep, y = yOf(v); if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); });
  ctx.stroke();
}

async function loadPaperStats(){
  const data = await getJSON('/api/paper/stats');
  const s = data.stats;
  document.getElementById('paper-stats-kpis').innerHTML = `
    <div class="kpi"><div class="kpi-label">Expectancy / trade</div><div class="kpi-value">${statOrDash(s.expectancy_pct,'%')}</div><div class="kpi-sub">${data.closed_count} closed trades</div></div>
    <div class="kpi"><div class="kpi-label">Win rate</div><div class="kpi-value">${statOrDash(s.win_rate,'%')}</div><div class="kpi-sub">avg win ${statOrDash(s.avg_win_pct,'%')} / avg loss ${statOrDash(s.avg_loss_pct,'%')}</div></div>
    <div class="kpi"><div class="kpi-label">Profit factor</div><div class="kpi-value">${s.profit_factor===null?'—':(s.profit_factor===Infinity?'∞':fmt(s.profit_factor))}</div><div class="kpi-sub">max DD ${statOrDash(s.max_drawdown_pct,'%')} · Sharpe ${statOrDash(s.sharpe)}</div></div>`;
  drawPaperEquity(data.equity_log);
}

async function loadPaperAll(){
  await Promise.all([loadPaperAccount(), loadPaperPositions(), loadPaperOrders(), loadPaperStats()]);
}

document.getElementById('paper-submit').addEventListener('click', async () => {
  const msg = document.getElementById('paper-order-msg');
  msg.textContent = 'Placing…';
  const orderType = document.getElementById('paper-order-type').value;
  const body = {
    symbol: document.getElementById('paper-symbol').value.trim(),
    asset_type: document.getElementById('paper-asset-type').value,
    order_type: orderType,
    qty: document.getElementById('paper-qty').value,
    stop_loss_pct: document.getElementById('paper-sl').value || null,
    take_profit_pct: document.getElementById('paper-tp').value || null,
    trail_pct: document.getElementById('paper-trail').value || null,
  };
  if (orderType === 'limit') body.limit_price = document.getElementById('paper-limit-price').value;
  if (orderType === 'stop') body.stop_price = document.getElementById('paper-stop-price').value;

  if (!body.symbol){ msg.textContent = 'Enter a symbol first.'; return; }
  const res = await postJSON('/api/paper/order', body);
  if (res.error){
    msg.textContent = res.error;
  } else if (res.filled){
    msg.textContent = `Filled at ${money(res.fill_price)}.`;
  } else {
    msg.textContent = 'Order placed, pending trigger.';
  }
  loadPaperAll();
});

document.getElementById('paper-reset').addEventListener('click', async () => {
  if (!confirm('Reset paper account? This clears all paper positions, orders, and equity history.')) return;
  const starting = prompt('Starting cash balance:', '10000');
  if (starting === null) return;
  await postJSON('/api/paper/account/reset', { starting_balance: parseFloat(starting) || 10000 });
  loadPaperAll();
});

// refresh paper tab data periodically while it's the active tab
setInterval(() => {
  if (document.getElementById('pane-paper').classList.contains('active')) loadPaperAll();
}, 20000);

// ============================================================
// TRANSACTION LEDGER IMPORT (Performance tab)
// ============================================================
let txCsvText = null;
let txHeaders = [];

function resetTxImportModal(){
  document.getElementById('tx-import-step-upload').style.display = '';
  document.getElementById('tx-import-step-map').style.display = 'none';
  document.getElementById('tx-import-step-result').style.display = 'none';
  document.getElementById('tx-import-file').value = '';
}
document.querySelector('[data-open="tx-import-form"]').addEventListener('click', resetTxImportModal);

document.getElementById('tx-import-preview-btn').addEventListener('click', async () => {
  const fileInput = document.getElementById('tx-import-file');
  if (!fileInput.files.length){ alert('Choose a CSV file first.'); return; }
  const text = await fileInput.files[0].text();
  txCsvText = text;
  const preview = await postJSON('/api/transactions/import/preview', { csv_text: text });
  txHeaders = preview.headers;

  const fillSelect = (sel, includeBlank) => {
    sel.innerHTML = (includeBlank ? '<option value="">— none —</option>' : '') +
      txHeaders.map(h => `<option value="${h}">${h}</option>`).join('');
  };
  fillSelect(document.getElementById('tx-map-date'), false);
  fillSelect(document.getElementById('tx-map-action'), false);
  fillSelect(document.getElementById('tx-map-symbol'), true);
  fillSelect(document.getElementById('tx-map-amount'), false);
  fillSelect(document.getElementById('tx-map-qty'), true);

  if (preview.guess.date_col) document.getElementById('tx-map-date').value = preview.guess.date_col;
  if (preview.guess.action_col) document.getElementById('tx-map-action').value = preview.guess.action_col;
  if (preview.guess.symbol_col) document.getElementById('tx-map-symbol').value = preview.guess.symbol_col;
  if (preview.guess.amount_col) document.getElementById('tx-map-amount').value = preview.guess.amount_col;
  if (preview.guess.quantity_col) document.getElementById('tx-map-qty').value = preview.guess.quantity_col;

  const sampleDiv = document.getElementById('tx-import-sample');
  if (preview.sample_rows.length){
    sampleDiv.innerHTML = `<table class="data-table"><thead><tr>${txHeaders.map(h=>`<th>${h}</th>`).join('')}</tr></thead>
      <tbody>${preview.sample_rows.map(r => `<tr>${txHeaders.map(h=>`<td>${r[h] ?? ''}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  } else {
    sampleDiv.innerHTML = '';
  }

  document.getElementById('tx-import-step-upload').style.display = 'none';
  document.getElementById('tx-import-step-map').style.display = '';
});

document.getElementById('tx-import-back-btn').addEventListener('click', () => {
  document.getElementById('tx-import-step-upload').style.display = '';
  document.getElementById('tx-import-step-map').style.display = 'none';
});

document.getElementById('tx-import-commit-btn').addEventListener('click', async () => {
  const broker = (document.getElementById('tx-import-broker').value || 'manual').trim().toLowerCase();
  const body = {
    csv_text: txCsvText,
    broker: broker,
    date_col: document.getElementById('tx-map-date').value,
    action_col: document.getElementById('tx-map-action').value,
    symbol_col: document.getElementById('tx-map-symbol').value || null,
    amount_col: document.getElementById('tx-map-amount').value,
    quantity_col: document.getElementById('tx-map-qty').value || null,
  };
  const result = await postJSON('/api/transactions/import/commit', body);
  const resultBody = document.getElementById('tx-import-result-body');
  if (result.error){
    resultBody.innerHTML = `<p>Import failed: ${result.error}</p>`;
  } else {
    const counts = Object.entries(result.action_counts).map(([k,v]) => `${v} ${k}`).join(', ') || 'nothing new';
    resultBody.innerHTML = `
      <p><strong>${result.inserted}</strong> new transactions imported from <strong>${result.broker}</strong>.</p>
      <p style="font-family:var(--mono); font-size:12.5px; color:var(--text-dim);">${counts}</p>
      <p style="color:var(--text-faint); font-size:12px;">Re-uploading the same file won't double-count — duplicate rows are skipped automatically.</p>`;
  }
  document.getElementById('tx-import-step-map').style.display = 'none';
  document.getElementById('tx-import-step-result').style.display = '';
  loadPerformance();
});

function drawSymbolPLBars(symbols){
  const canvas = document.getElementById('perf-pl-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0,0,w,h);
  if (!symbols || !symbols.length){
    ctx.fillStyle = '#565e66'; ctx.font = '13px monospace';
    ctx.fillText('No symbols to plot.', 20, h/2);
    return;
  }
  const pad = 30;
  const zeroY = h/2;
  const maxAbs = Math.max(...symbols.map(s => Math.abs(s.total_pl)), 0.01);
  const scale = (h/2 - pad) / maxAbs;
  const barW = (w - pad*2) / symbols.length;

  ctx.strokeStyle = '#24292f'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, zeroY); ctx.lineTo(w-pad, zeroY); ctx.stroke();

  ctx.textAlign = 'center';
  symbols.forEach((s, i) => {
    const val = s.total_pl;
    const x = pad + i*barW + barW*0.2;
    const bw = barW*0.6;
    const barH = Math.abs(val) * scale;
    const y = val >= 0 ? zeroY - barH : zeroY;
    const color = val >= 0 ? '#3ecf8e' : '#e0575b';
    ctx.fillStyle = color;
    ctx.fillRect(x, y, bw, barH);
    ctx.font = '11px monospace';
    ctx.fillText(s.symbol, x + bw/2, zeroY + (val >= 0 ? 14 : -6));
    ctx.fillStyle = color;
    ctx.fillText(money(val), x + bw/2, val >= 0 ? y - 6 : y + barH + 14);
  });
  ctx.textAlign = 'left';
}

function drawCashFlowLine(series){
  const canvas = document.getElementById('perf-cashflow-canvas');
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0,0,w,h);
  if (!series || series.length < 2){
    ctx.fillStyle = '#565e66'; ctx.font = '13px monospace';
    ctx.fillText('Not enough transaction history to plot.', 20, h/2);
    return;
  }
  const values = series.map(p => p.cumulative);
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const pad = 24;
  const xStep = (w - pad*2) / (values.length - 1);
  const yScale = (h - pad*2) / ((max - min) || 1);
  const yOf = v => h - pad - (v - min) * yScale;

  ctx.strokeStyle = '#24292f'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, yOf(0)); ctx.lineTo(w-pad, yOf(0)); ctx.stroke();

  ctx.strokeStyle = values[values.length-1] >= 0 ? '#3ecf8e' : '#e0575b';
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = pad + i*xStep, y = yOf(v);
    if (i === 0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();

  ctx.fillStyle = '#565e66'; ctx.font = '11px monospace';
  ctx.textAlign = 'left';
  ctx.fillText(series[0].date, pad, h-6);
  ctx.textAlign = 'right';
  ctx.fillText(series[series.length-1].date, w-pad, h-6);
  ctx.textAlign = 'left';
}

async function loadPerformance(){
  const perf = await getJSON('/api/performance');
  document.getElementById('perf-net-contrib').textContent = money(perf.net_contributions);
  document.getElementById('perf-deposits-sub').textContent = `${money(perf.total_deposits)} in / ${money(perf.total_withdrawals)} out`;
  document.getElementById('perf-dividends').textContent = money(perf.total_dividends);
  document.getElementById('perf-tx-count').textContent = perf.transaction_count;

  drawSymbolPLBars(perf.symbols);
  drawCashFlowLine(perf.cash_flow_series);

  const symDiv = document.getElementById('perf-symbols-table');
  if (!perf.symbols.length){
    symDiv.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No transactions imported yet.</td></tr></tbody></table>`;
  } else {
    symDiv.innerHTML = `<table class="data-table">
      <thead><tr><th>Symbol</th><th>Bought</th><th>Sold</th><th>Dividends</th><th>Qty net</th><th>Status</th><th>Live price</th><th>Unrealized</th><th>Cash P/L to date</th><th>Total P/L</th></tr></thead>
      <tbody>${perf.symbols.map(s => `
        <tr>
          <td><strong>${s.symbol}</strong></td>
          <td>${money(s.bought)}</td>
          <td>${money(s.sold)}</td>
          <td>${money(s.dividends)}</td>
          <td>${s.qty_net != null ? fmt(s.qty_net,4) : '—'}</td>
          <td>${s.status}</td>
          <td>${s.current_price != null ? money(s.current_price) : '—'}</td>
          <td>${s.unrealized_value != null ? money(s.unrealized_value) : '—'}</td>
          <td class="${pctClass(s.cash_pl_to_date)}">${money(s.cash_pl_to_date)}</td>
          <td class="${pctClass(s.total_pl)}"><strong>${money(s.total_pl)}</strong></td>
        </tr>`).join('')}</tbody></table>`;
  }
}

async function loadLedger(){
  const rows = await getJSON('/api/transactions');
  const div = document.getElementById('perf-ledger-table');
  if (!rows.length){
    div.innerHTML = `<table class="data-table"><tbody><tr class="empty-row"><td>No transactions imported yet.</td></tr></tbody></table>`;
  } else {
    div.innerHTML = `<table class="data-table">
      <thead><tr><th>Date</th><th>Broker</th><th>Action</th><th>Symbol</th><th>Qty</th><th>Amount</th><th>Description</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${r.date}</td>
          <td>${r.broker}</td>
          <td>${r.action}</td>
          <td>${r.symbol || '—'}</td>
          <td>${r.quantity != null ? fmt(r.quantity,4) : '—'}</td>
          <td class="${pctClass(r.amount)}">${money(r.amount)}</td>
          <td style="font-size:11px; color:var(--text-faint);">${r.raw_description || ''}</td>
        </tr>`).join('')}</tbody></table>`;
  }
}

async function loadPerformanceAll(){
  await Promise.all([loadPerformance(), loadLedger()]);
}

// ============================================================
document.getElementById('refresh-all').addEventListener('click', loadAll);

async function loadAll(){
  await Promise.all([loadStocks(), loadOptions(), loadCrypto(), loadJournal(), loadOverview()]);
}

loadAll();
