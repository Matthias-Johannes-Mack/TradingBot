const $ = (selector) => document.querySelector(selector);
const form = $('#strategy-form');
const previewEmpty = $('#preview-empty');
const previewContent = $('#preview-content');
const orders = $('#orders');
const arm = $('#arm');
const explain = $('#explain');
const tickButton = $('#send-tick');
let strategyId = null;
let currentPreview = null;

function strategyPayload() {
  const values = Object.fromEntries(new FormData(form));
  return {
    symbol: values.symbol,
    shares_owned: Number(values.shares_owned),
    entry_price: Number(values.entry_price),
    current_price: Number(values.current_price),
    hard_floor_pct: Number(values.hard_floor_pct),
    trail_trigger_pct: Number(values.trail_trigger_pct),
    trail_distance_pct: Number(values.trail_distance_pct),
    ladder_step_pct: Number(values.ladder_step_pct),
    ladder_shares: Number(values.ladder_shares),
    ladder_levels: Number(values.ladder_levels),
    tax: {
      church_tax_rate_pct: Number(values.church_tax_rate_pct),
      remaining_allowance_eur: Number(values.remaining_allowance_eur),
      buy_fee_eur: Number(values.buy_fee_eur),
      sell_fee_eur: Number(values.sell_fee_eur),
      minimum_net_profit_eur: Number(values.minimum_net_profit_eur),
      broker_cost_basis_eur_per_share: values.broker_cost_basis_eur_per_share ? Number(values.broker_cost_basis_eur_per_share) : null
    }
  };
}
function euros(amount) { return new Intl.NumberFormat('de-DE', { style: 'currency', currency: 'EUR' }).format(amount); }
function dollars(amount) { return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount); }
function setBusy(button, busy, label) { button.disabled = busy; if (label) button.dataset.label = button.textContent; button.textContent = busy ? 'Working…' : (button.dataset.label || button.textContent); }
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Something went wrong.');
  return payload;
}
function addOrder(order) {
  const card = document.createElement('article');
  card.className = 'order ' + order.side.toLowerCase();
  const top = document.createElement('div');
  top.className = 'order-top';
  top.innerHTML = `<strong>${order.side} · ${order.order_type.replace('_', ' ')}</strong><span>${order.status}</span>`;
  const price = document.createElement('div');
  price.className = 'order-price';
  price.textContent = order.price ? euros(order.price) : 'Dynamic price';
  const detail = document.createElement('p');
  detail.textContent = order.condition;
  card.append(top, price, detail);
  orders.append(card);
}
function renderTaxSummary(container, analysis) {
  if (container.id === 'simulation-tax') container.replaceChildren();
  if (!analysis) return;
  const box = document.createElement('div');
  box.className = 'tax-summary';
  const heading = document.createElement('h3');
  heading.textContent = 'After-tax EUR estimate · Baden-Württemberg';
  box.append(heading);
  box.append(brokerLine('EUR basis used', `${euros(analysis.basis_total_eur)} · ${analysis.basis_source}`));
  box.append(brokerLine('Minimum net-positive exit', `${euros(analysis.minimum_exit_eur_per_share)} per share`));
  const rate = new Intl.NumberFormat('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(analysis.marginal_tax_rate_pct));
  box.append(brokerLine('Tax assumptions', `${rate} % on taxable gains · ${analysis.church_tax_rate_pct}% church tax · ${euros(analysis.remaining_allowance_eur)} allowance left · ${euros(analysis.minimum_net_profit_eur)} minimum net gain`));
  Object.entries(analysis.scenarios).forEach(([name, item]) => {
    const row = brokerLine(name, `gross ${euros(item.pre_tax_profit_eur)} − tax ${euros(item.total_tax_eur)} = net ${euros(item.net_profit_eur)}`);
    row.classList.add(Number(item.net_profit_eur) >= 0 ? 'tax-positive' : 'tax-negative');
    box.append(row);
  });
  const caveat = document.createElement('p');
  caveat.textContent = analysis.limitations;
  box.append(caveat);
  container.append(box);
}
function renderPreview(preview) {
  currentPreview = preview;
  previewEmpty.hidden = true;
  previewContent.hidden = false;
  $('#position-label').textContent = `${preview.strategy.symbol} · ${preview.strategy.shares_owned} shares · entry ${euros(preview.strategy.entry_price)}`;
  orders.replaceChildren();
  preview.orders.forEach(addOrder);
  preview.warnings.forEach((warning) => {
    const note = document.createElement('p');
    note.className = 'warning';
    note.textContent = warning;
    orders.append(note);
  });
  renderTaxSummary($('#simulation-tax'), preview.tax_analysis);
  drawChart();
  arm.disabled = false;
  explain.disabled = false;
  $('#confirm-title').textContent = 'Ready for confirmation';
  $('#confirm-copy').textContent = 'The paper orders above are calculated from your values. Arm only when they look right.';
  $('#monitor-status').textContent = 'Not armed';
  $('#monitor-status').className = 'badge muted';
}
function showError(message) {
  previewEmpty.hidden = false;
  previewContent.hidden = true;
  previewEmpty.textContent = message;
}
function renderActivity(activities) {
  const activity = $('#activity');
  activity.replaceChildren();
  if (!activities.length) { activity.innerHTML = '<p>No paper activity yet.</p>'; return; }
  activities.forEach((event) => {
    const row = document.createElement('article');
    const text = document.createElement('strong');
    text.textContent = event.message;
    const time = document.createElement('time');
    time.textContent = new Date(event.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    row.append(text, time);
    activity.append(row);
  });
}
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setBusy(form.querySelector('.primary'), true, true);
  try { renderPreview(await api('/api/preview', { method: 'POST', body: JSON.stringify(strategyPayload()) })); }
  catch (error) { showError(error.message); }
  finally { setBusy(form.querySelector('.primary'), false); }
});
form.addEventListener('input', () => {
  strategyId = null;
  brokerPreviewToken = null;
  brokerPreviewSummary = null;
  currentPreview = null;
  $('#broker-preview').hidden = true;
  arm.disabled = true;
  explain.disabled = true;
  tickButton.disabled = true;
  $('#monitor-status').textContent = 'Review changes';
  $('#monitor-status').className = 'badge muted';
  brokerTaxAnalysis = null;
  automationPreviewToken = null;
  $('#automation-preview').hidden = true;
  $('#automation-ack').checked = false;
  drawChart();
  if (chartData?.symbol !== strategyPayload().symbol.trim().toUpperCase()) {
    clearTimeout(chartRefreshDebounce);
    chartRefreshDebounce = setTimeout(async () => { await Promise.all([refreshBroker(), refreshChart()]); }, 500);
  }
});
arm.addEventListener('click', async () => {
  setBusy(arm, true, true);
  try {
    const response = await api('/api/strategies', { method: 'POST', body: JSON.stringify(strategyPayload()) });
    strategyId = response.strategy_id;
    $('#confirm-title').textContent = 'Paper strategy armed';
    $('#confirm-copy').textContent = 'The strategy is in memory for this local session. No orders were sent anywhere.';
    $('#monitor-status').textContent = 'Armed';
    $('#monitor-status').className = 'badge live';
    tickButton.disabled = false;
    arm.textContent = 'Paper strategy armed';
    renderActivity(response.activities);
  } catch (error) { $('#confirm-copy').textContent = error.message; }
  finally { setBusy(arm, false); arm.disabled = Boolean(strategyId); }
});
explain.addEventListener('click', async () => {
  $('#modal').hidden = false;
  $('#explanation').textContent = 'Asking your local Ollama model…';
  setBusy(explain, true, true);
  try {
    const response = await api('/api/explain', { method: 'POST', body: JSON.stringify(strategyPayload()) });
    $('#model-note').textContent = `Explanation generated by local ${response.model}.`;
    $('#explanation').textContent = response.explanation;
  } catch (error) { $('#explanation').textContent = error.message; }
  finally { setBusy(explain, false); }
});
tickButton.addEventListener('click', async () => {
  if (!strategyId) return;
  setBusy(tickButton, true, true);
  try {
    const price = Number($('#tick-price').value);
    const response = await api(`/api/strategies/${strategyId}/ticks`, { method: 'POST', body: JSON.stringify({ price }) });
    renderActivity(response.activities);
    $('#monitor-status').textContent = response.closed ? 'Closed (simulated)' : 'Armed';
    if (response.closed) { $('#monitor-status').className = 'badge muted'; tickButton.disabled = true; }
  } catch (error) { $('#activity').innerHTML = `<p>${error.message}</p>`; }
  finally { setBusy(tickButton, false); }
});
$('#reset').addEventListener('click', () => { form.reset(); strategyId = null; currentPreview = null; brokerPreviewSummary = null; previewEmpty.hidden = false; previewContent.hidden = true; previewEmpty.textContent = 'Fill in your position to preview the paper orders.'; arm.disabled = true; explain.disabled = true; tickButton.disabled = true; $('#activity').innerHTML = '<p>No paper activity yet.</p>'; drawChart(); });
$('#close-modal').addEventListener('click', () => { $('#modal').hidden = true; });
$('#modal').addEventListener('click', (event) => { if (event.target.id === 'modal') $('#modal').hidden = true; });

let brokerPreviewToken = null;
let automationPreviewToken = null;
let chartRefreshDebounce = null;
let brokerStatus = null;
let chartData = null;
let brokerTaxAnalysis = null;
let brokerPreviewSummary = null;
const brokerKind = $('#broker-kind');
const brokerPreviewButton = $('#broker-preview-button');
const brokerConfirmButton = $('#broker-confirm');

function brokerLine(label, value) {
  const row = document.createElement('div');
  row.className = 'broker-line';
  const name = document.createElement('span');
  name.textContent = label;
  const content = document.createElement('strong');
  content.textContent = value;
  row.append(name, content);
  return row;
}

function chartLines() {
  const fx = Number(brokerStatus?.fx?.usd_per_eur || 0);
  const result = [];
  const add = (label, price, color, dash = false) => {
    if (Number.isFinite(price) && price > 0) result.push({ label, price, color, dash });
  };
  (brokerStatus?.open_orders || []).forEach((order) => {
    const price = Number(order.stop_price || order.limit_price || 0);
    if (price) add(`Open ${order.side} ${order.type}`, price, order.side === 'sell' ? '#9d2f55' : '#345ac0');
  });
  const symbol = strategyPayload().symbol.trim().toUpperCase();
  if (brokerPreviewSummary?.symbol === symbol && brokerPreviewSummary.price_usd) {
    add(`Preview ${brokerPreviewSummary.side} ${brokerPreviewSummary.order_type}`, Number(brokerPreviewSummary.price_usd), '#8c4fba', true);
  }
  if (brokerPreviewSummary?.symbol === symbol && brokerPreviewSummary.kind === 'take_profit_limit' && brokerTaxAnalysis && fx > 0) {
    add('After-tax minimum est.', Number(brokerTaxAnalysis.minimum_exit_eur_per_share) * fx, '#6c3da2', true);
  }
  if ($('#chart-overlays').value === 'plan' && currentPreview?.strategy.symbol === symbol && fx > 0) {
    add('Plan hard stop', Number(currentPreview.hard_stop_price) * fx, '#cf5b50', true);
    add('Plan trail starts', Number(currentPreview.trailing_trigger_price) * fx, '#0d8d80', true);
    currentPreview.orders.filter((order) => order.id.startsWith('ladder-') && order.status !== 'BLOCKED').forEach((order) => {
      add(`Plan ${order.id}`, Number(order.price) * fx, '#a87924', true);
    });
    add('After-tax target est.', Number(currentPreview.tax_analysis?.minimum_exit_eur_per_share) * fx, '#6c3da2', true);
  }
  return result;
}

function drawChart() {
  if (!chartData?.bars?.length) return;
  const canvas = $('#broker-chart');
  const width = Math.max(320, canvas.clientWidth);
  const height = 370;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);
  const bars = chartData.bars.filter((b) => [b.o, b.h, b.l, b.c].every((n) => Number.isFinite(Number(n)) && Number(n) > 0));
  if (!bars.length) return;
  const lines = chartLines();
  const candleMin = Math.min(...bars.map((b) => Number(b.l)));
  const candleMax = Math.max(...bars.map((b) => Number(b.h)));
  const padding = Math.max(0.5, (candleMax - candleMin) * 0.12);
  const yMin = candleMin - padding;
  const yMax = candleMax + padding;
  const left = 52, right = width - 36, top = 20, bottom = height - 35;
  const y = (price) => bottom - (price - yMin) / (yMax - yMin) * (bottom - top);
  ctx.font = '11px system-ui, sans-serif';
  ctx.textAlign = 'right';
  for (let tick = 0; tick <= 4; tick++) {
    const value = yMin + (yMax - yMin) * tick / 4;
    const yy = y(value);
    ctx.strokeStyle = '#e3ebea'; ctx.lineWidth = 1; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke();
    ctx.fillStyle = '#687c81'; ctx.fillText(value.toFixed(2), left - 6, yy + 4);
  }
  const step = (right - left) / bars.length;
  bars.forEach((bar, index) => {
    const x = left + (index + 0.5) * step;
    const open = Number(bar.o), close = Number(bar.c);
    const rising = close >= open;
    ctx.strokeStyle = rising ? '#0a887b' : '#c2574d';
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(x, y(Number(bar.h))); ctx.lineTo(x, y(Number(bar.l))); ctx.stroke();
    const bodyTop = Math.min(y(open), y(close));
    ctx.fillRect(x - Math.max(1.2, Math.min(4, step * 0.35)), bodyTop,
      Math.max(2.4, Math.min(8, step * 0.7)), Math.max(1.5, Math.abs(y(open) - y(close))));
  });
  ctx.textAlign = 'center'; ctx.fillStyle = '#687c81';
  [0, Math.floor((bars.length - 1) / 2), bars.length - 1].forEach((index) => {
    const bar = bars[index];
    ctx.fillText(new Date(bar.t).toLocaleString([], chartData.timeframe === '1Day' ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }), left + (index + 0.5) * step, bottom + 18);
  });
  const legend = $('#chart-legend');
  legend.replaceChildren();
  lines.forEach((line) => {
    const visible = line.price >= yMin && line.price <= yMax;
    if (visible) {
      ctx.strokeStyle = line.color; ctx.lineWidth = line.dash ? 1.5 : 2.5;
      ctx.setLineDash(line.dash ? [5, 4] : []);
      ctx.beginPath(); ctx.moveTo(left, y(line.price)); ctx.lineTo(right, y(line.price)); ctx.stroke();
      ctx.setLineDash([]);
    }
    const item = document.createElement('span');
    item.className = visible ? 'chart-level' : 'chart-level outside';
    item.style.setProperty('--line-color', line.color);
    item.textContent = `${line.label} · ${dollars(line.price)}${visible ? '' : line.price < yMin ? ' · below view' : ' · above view'}`;
    legend.append(item);
  });
  if (!lines.length) {
    const item = document.createElement('span');
    item.className = 'chart-empty-levels';
    item.textContent = $('#chart-overlays').value === 'plan'
      ? 'No reviewed plan yet. Use “Review paper orders” to add plan levels.'
      : 'No priced open orders. Market orders have no fixed price.';
    legend.append(item);
  }
}

async function refreshChart() {
  const symbol = strategyPayload().symbol.trim().toUpperCase();
  if (!/^[A-Z0-9.-]{1,10}$/.test(symbol)) return;
  const timeframe = $('#chart-timeframe').value;
  try {
    const data = await api(`/api/broker/chart?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`);
    if (strategyPayload().symbol.trim().toUpperCase() !== data.symbol || $('#chart-timeframe').value !== data.timeframe) return;
    chartData = data;
    $('#chart-title').textContent = `${data.symbol} candlesticks · USD`;
    const last = data.bars[data.bars.length - 1];
    $('#chart-meta').textContent = `${data.feed} single-exchange · last candle ${new Date(last.t).toLocaleString()} · checked ${new Date(data.polled_at).toLocaleTimeString()}`;
    drawChart();
  } catch (error) {
    $('#chart-meta').textContent = error.message;
  }
}

async function refreshBroker() {
  const symbol = strategyPayload().symbol.trim().toUpperCase();
  $('#broker-status').textContent = 'Checking connection…';
  try {
    const status = await api(`/api/broker/status?symbol=${encodeURIComponent(symbol)}`);
    brokerStatus = status;
    if (!status.configured) {
      $('#broker-status').textContent = status.message;
      $('#broker-position').textContent = '';
      $('#broker-open-orders').replaceChildren();
    } else {
      const account = status.account;
      $('#broker-status').textContent = `Connected · ${account.status} paper account · ${account.currency} balance ${dollars(account.equity_usd || 0)}${account.equity_eur_estimate ? ` (about ${euros(account.equity_eur_estimate)})` : ''}`;
      $('#broker-position').textContent = status.position
        ? `${status.position.qty} ${symbol} shares at broker average ${dollars(status.position.avg_entry_price)}${status.position.avg_entry_eur_estimate ? ` (about ${euros(status.position.avg_entry_eur_estimate)} per share)` : ''}.`
        : `No open ${symbol} position at Alpaca.`;
      const open = $('#broker-open-orders');
      open.replaceChildren();
      if (status.open_orders.length) {
        const heading = document.createElement('strong');
        heading.textContent = `${status.open_orders.length} open ${symbol} broker order(s)`;
        open.append(heading);
        status.open_orders.forEach((order) => open.append(brokerLine(`${order.side} ${order.type} · ${order.qty} shares`, `${order.status} · ${order.stop_price || order.limit_price || 'market'} USD`)));
      }
    }
    if (status.fx) $('#fx-line').textContent = `ECB reference ${status.fx.reference_date}: €1 = $${status.fx.usd_per_eur}. Euro values are estimates.`;
    drawChart();
  } catch (error) {
    brokerStatus = null;
    $('#broker-status').textContent = error.message;
    $('#broker-position').textContent = '';
    $('#broker-open-orders').replaceChildren();
  }
  try {
    const fx = await api('/api/fx');
    $('#fx-line').textContent = `ECB reference ${fx.reference_date}: €1 = $${fx.usd_per_eur}. Euro values are estimates.`;
    if (!brokerStatus) brokerStatus = { fx, open_orders: [], position: null };
    drawChart();
  } catch (error) {
    $('#fx-line').textContent = error.message;
  }
}

function renderBrokerPreview(summary) {
  brokerPreviewSummary = summary;
  const details = $('#broker-preview-details');
  details.replaceChildren();
  details.append(brokerLine('Alpaca paper order', `${summary.side.toUpperCase()} ${summary.quantity} ${summary.symbol} · ${summary.order_type}`));
  if (summary.price_usd) details.append(brokerLine('Order price sent to Alpaca', dollars(summary.price_usd)));
  else details.append(brokerLine('Order price sent to Alpaca', summary.order_type === 'market' ? 'Market price at execution' : `${summary.trail_percent}% below high`));
  if (summary.price_eur_estimate) details.append(brokerLine('Euro estimate per share', euros(summary.price_eur_estimate)));
  if (summary.total_usd) details.append(brokerLine('Order value at trigger or limit', `${dollars(summary.total_usd)} · about ${euros(summary.total_eur_estimate)}`));
  if (summary.reference_price_usd) details.append(brokerLine('Latest IEX reference', `${dollars(summary.reference_price_usd)} · about ${euros(summary.reference_price_eur)}`));
  if (summary.reference_total_usd) details.append(brokerLine('Estimated market buy value', `${dollars(summary.reference_total_usd)} · about ${euros(summary.reference_total_eur)}`));
  if (summary.reference_time) details.append(brokerLine('IEX trade time', new Date(summary.reference_time).toLocaleString()));
  if (summary.broker_entry_usd) details.append(brokerLine('Alpaca average entry', `${dollars(summary.broker_entry_usd)} · about ${euros(summary.broker_entry_eur_estimate)}`));
  if (summary.trigger_price_usd) details.append(brokerLine('Trailing trigger', `${dollars(summary.trigger_price_usd)} · about ${euros(summary.trigger_price_eur_estimate)}`));
  renderTaxSummary(details, summary.tax_analysis);
  brokerTaxAnalysis = summary.tax_analysis || null;
  drawChart();
  details.append(brokerLine('Exchange rate', `€1 = $${summary.fx.usd_per_eur} · ECB ${summary.fx.reference_date}`));
  summary.warnings.forEach((warning) => {
    const item = document.createElement('p');
    item.className = 'broker-warning';
    item.textContent = warning;
    details.append(item);
  });
  $('#broker-preview').hidden = false;
  $('#broker-result').replaceChildren();
}

brokerKind.addEventListener('change', () => {
  $('#broker-level-label').hidden = brokerKind.value !== 'ladder_buy';
  $('#broker-target-label').hidden = brokerKind.value !== 'take_profit_limit';
  brokerPreviewToken = null;
  brokerPreviewSummary = null;
  brokerTaxAnalysis = null;
  $('#broker-preview').hidden = true;
  drawChart();
});
$('#broker-level').addEventListener('input', () => { brokerPreviewToken = null; brokerPreviewSummary = null; $('#broker-preview').hidden = true; drawChart(); });
$('#broker-target').addEventListener('input', () => { brokerPreviewToken = null; brokerPreviewSummary = null; $('#broker-preview').hidden = true; drawChart(); });
$('#refresh-broker').addEventListener('click', async () => { await Promise.all([refreshBroker(), refreshChart()]); });
brokerPreviewButton.addEventListener('click', async () => {
  if (!form.reportValidity()) return;
  brokerPreviewToken = null;
  brokerPreviewSummary = null;
  $('#broker-preview').hidden = true;
  drawChart();
  setBusy(brokerPreviewButton, true, true);
  try {
    const response = await api('/api/broker/order-preview', { method: 'POST', body: JSON.stringify({
      strategy: strategyPayload(), kind: brokerKind.value, level: Number($('#broker-level').value),
      target_exit_eur_per_share: $('#broker-target').value ? Number($('#broker-target').value) : null
    }) });
    brokerPreviewToken = response.preview_token;
    renderBrokerPreview(response.summary);
  } catch (error) {
    $('#broker-result').textContent = error.message;
  } finally { setBusy(brokerPreviewButton, false); }
});
brokerConfirmButton.addEventListener('click', async () => {
  if (!brokerPreviewToken) return;
  setBusy(brokerConfirmButton, true, true);
  try {
    const response = await api('/api/broker/orders', { method: 'POST', body: JSON.stringify({ preview_token: brokerPreviewToken }) });
    const result = $('#broker-result');
    result.replaceChildren();
    result.append(brokerLine('Submitted to Alpaca paper', `${response.order.side.toUpperCase()} ${response.order.qty} ${response.order.symbol} · ${response.order.type}`));
    result.append(brokerLine('Broker status', response.order.status || 'submitted'));
    result.append(brokerLine('Order ID', response.order.id || 'pending'));
    if (response.order.stop_price || response.order.limit_price) result.append(brokerLine('USD order price', dollars(response.order.stop_price || response.order.limit_price)));
    if (response.order.filled_qty) result.append(brokerLine('Shares filled so far', response.order.filled_qty));
    if (response.order.filled_avg_price) result.append(brokerLine('Actual average fill', dollars(response.order.filled_avg_price)));
    brokerPreviewToken = null;
    brokerPreviewSummary = null;
    brokerTaxAnalysis = null;
    $('#broker-preview').hidden = true;
    await Promise.all([refreshBroker(), refreshChart()]);
  } catch (error) { $('#broker-result').textContent = error.message; }
  finally { setBusy(brokerConfirmButton, false); }
});
function renderAutomationPlan(plan) {
  const article = document.createElement('article');
  const title = document.createElement('strong');
  title.textContent = `${plan.symbol} · ${plan.active ? 'ACTIVE' : 'PAUSED'} · ${plan.status.replaceAll('_', ' ')}`;
  article.append(title);
  const details = document.createElement('p');
  details.textContent = `Checks every 30s during market hours · floor ${plan.strategy.hard_floor_pct}% · trail starts +${plan.strategy.trail_trigger_pct}% / distance ${plan.strategy.trail_distance_pct}% · re-entry ${plan.strategy.ladder_shares} shares every ${plan.strategy.ladder_step_pct}% (max ${plan.strategy.ladder_levels} levels). Last check: ${plan.last_checked_at ? new Date(plan.last_checked_at).toLocaleString() : 'not yet'}.`;
  article.append(details);
  if (plan.last_error) { const error = document.createElement('p'); error.className = 'broker-warning'; error.textContent = plan.last_error; article.append(error); }
  if (plan.events?.length) {
    const list = document.createElement('ul');
    plan.events.slice(0, 5).forEach((event) => { const item = document.createElement('li'); item.textContent = `${new Date(event.created_at).toLocaleString()} · ${event.message}`; list.append(item); });
    article.append(list);
  }
  if (plan.active) {
    const pause = document.createElement('button');
    pause.type = 'button'; pause.className = 'outline'; pause.textContent = 'Pause future automatic actions';
    pause.addEventListener('click', async () => {
      setBusy(pause, true, true);
      try { await api(`/api/automation/plans/${encodeURIComponent(plan.id)}/pause`, { method: 'POST' }); await refreshAutomation(); }
      catch (error) { $('#automation-result').textContent = error.message; }
      finally { setBusy(pause, false); }
    });
    article.append(pause);
  }
  return article;
}

async function refreshAutomation() {
  try {
    const response = await api('/api/automation/plans');
    const area = $('#automation-status'); area.replaceChildren();
    if (!response.plans.length) area.textContent = 'No confirmed automatic plan. The scheduler will not place orders.';
    else response.plans.forEach((plan) => area.append(renderAutomationPlan(plan)));
  } catch (error) { $('#automation-status').textContent = error.message; }
}

$('#automation-ack').addEventListener('change', () => { $('#automation-activate').disabled = !$('#automation-ack').checked || !automationPreviewToken; });
$('#automation-preview-button').addEventListener('click', async () => {
  if (!form.reportValidity()) return;
  automationPreviewToken = null; $('#automation-preview').hidden = true;
  setBusy($('#automation-preview-button'), true, true);
  try {
    const response = await api('/api/automation/preview', { method: 'POST', body: JSON.stringify(strategyPayload()) });
    automationPreviewToken = response.preview_token;
    const summary = response.summary;
    const details = $('#automation-preview-details'); details.replaceChildren();
    details.append(brokerLine('Paper symbol and current position', `${summary.symbol} · ${summary.current_position_qty || 'no filled position yet'} shares`));
    details.append(brokerLine('Protective floor', `${summary.hard_floor_pct}% below actual broker USD entry`));
    details.append(brokerLine('Raise floor after', `+${summary.trail_trigger_pct}% from entry; then ${summary.trail_distance_pct}% below observed high`));
    details.append(brokerLine('After a plan-owned exit fills', `Limit re-entry: ${summary.reentry_shares} shares, step ${summary.reentry_step_pct}%, up to ${summary.reentry_levels} levels`));
    details.append(brokerLine('Alpaca market', summary.market_open_now ? 'Open now' : 'Closed now; worker waits'));
    summary.warnings.forEach((warning) => { const item = document.createElement('p'); item.className = 'broker-warning'; item.textContent = warning; details.append(item); });
    $('#automation-ack').checked = false; $('#automation-activate').disabled = true;
    $('#automation-preview').hidden = false;
    $('#automation-result').textContent = '';
  } catch (error) { $('#automation-result').textContent = error.message; }
  finally { setBusy($('#automation-preview-button'), false); }
});
$('#automation-activate').addEventListener('click', async () => {
  if (!automationPreviewToken || !$('#automation-ack').checked) return;
  setBusy($('#automation-activate'), true, true);
  try {
    const response = await api('/api/automation/activate', { method: 'POST', body: JSON.stringify({ preview_token: automationPreviewToken, acknowledge_auto_orders: true }) });
    automationPreviewToken = null; $('#automation-preview').hidden = true;
    $('#automation-result').textContent = `Automatic paper plan ${response.plan.id} is active. The Docker worker will check it during market hours.`;
    await refreshAutomation();
  } catch (error) { $('#automation-result').textContent = error.message; }
  finally { setBusy($('#automation-activate'), false); }
});
$('#refresh-automation').addEventListener('click', refreshAutomation);

refreshBroker();
refreshChart();
refreshAutomation();
setInterval(() => { if (!document.hidden) refreshChart(); }, 15000);
setInterval(() => { if (!document.hidden) refreshBroker(); }, 30000);
setInterval(() => { if (!document.hidden) refreshAutomation(); }, 30000);
$('#chart-timeframe').addEventListener('change', refreshChart);
$('#chart-overlays').addEventListener('change', drawChart);
window.addEventListener('resize', drawChart);
