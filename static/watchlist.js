// Watchlist and autopilot panel. Loaded after app.js and reuses its helpers
// ($, api, form, setBusy, brokerLine, euros, dollars).
const ACTION_LABELS = { SELL: 'Sell next', HOLD: 'Holding', BUY: 'Buy next', BUY_BLOCKED: 'Buy candidate', WATCH: 'Watching', AVOID: 'Avoid', MUTED: 'Muted' };
const SOURCE_TAGS = {
  insiders: ['I', 'SEC Form 4 insiders'], congress: ['H', 'US House disclosures'], contracts: ['C', 'Federal contracts'],
  trends: ['G', 'Google Trends'], retail: ['R', 'Reddit + StockTwits']
};
const SOURCE_NAMES = { insiders: 'Insider', congress: 'House', contracts: 'Contract', trends: 'Trends', retail: 'Retail' };
const SETTING_FIELDS = ['min_score', 'min_sources', 'strong_score', 'budget_eur_per_position', 'max_positions', 'max_new_per_day',
  'hard_floor_pct', 'trail_trigger_pct', 'trail_distance_pct', 'exit_score', 'cooldown_days', 'min_price_usd', 'min_iex_dollar_volume_usd'];
let watchBoard = null;
let watchFilter = 'all';
let autopilotPreviewToken = null;
let researchPoll = null;

// US trading hours, shown in the viewer's own time zone. The open/closed state
// is derived from the session times on every render, so it flips at the bell
// even between server refreshes.
let marketHours = null;
const HOUR_MINUTE = { hour: '2-digit', minute: '2-digit' };
function localTime(iso) { return new Date(iso).toLocaleTimeString([], HOUR_MINUTE); }
function dayLabel(iso) {
  const day = new Date(iso);
  const midnight = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const offset = Math.round((midnight(day) - midnight(new Date())) / 86400000);
  if (offset === 0) return 'today';
  if (offset === 1) return 'tomorrow';
  // English day names to match the rest of the interface; times keep the viewer's locale.
  return day.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
}
function untilText(iso) {
  const minutes = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 60000));
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours >= 48) return `in ${Math.round(hours / 24)} days`;
  return `in ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
}
function marketState() {
  if (!marketHours?.sessions?.length) return null;
  const now = Date.now();
  const sessions = marketHours.sessions.filter((session) => new Date(session.close_at).getTime() > now);
  const current = sessions.find((session) => new Date(session.open_at).getTime() <= now) || null;
  return { current, next: sessions.find((session) => session !== current) || null, sessions };
}
function sessionBar(session, isCurrent) {
  const opens = new Date(session.open_at);
  const closes = new Date(session.close_at);
  const midnight = new Date(opens.getFullYear(), opens.getMonth(), opens.getDate()).getTime();
  const percent = (time) => (time - midnight) / 86400000 * 100;
  const start = percent(opens.getTime());
  const end = percent(closes.getTime());
  const wrap = el('div', 'mh-day');
  wrap.setAttribute('role', 'img');
  wrap.setAttribute('aria-label', `US trading session ${dayLabel(session.open_at)} from ${localTime(session.open_at)} to ${localTime(session.close_at)} your time`);
  const edges = el('div', 'mh-edges');
  // A close after local midnight is labelled on the wrapped piece at the left.
  [[start, localTime(session.open_at)], [end > 100 ? end - 100 : end, localTime(session.close_at)]].forEach(([at, text]) => {
    const label = el('span', '', text);
    label.style.left = `${at}%`;
    label.style.transform = at < 5 ? 'none' : at > 95 ? 'translateX(-100%)' : 'translateX(-50%)';
    edges.append(label);
  });
  const track = el('div', 'mh-track');
  const band = (from, to) => {
    const part = el('span', isCurrent ? 'mh-session live' : 'mh-session');
    part.style.left = `${from}%`;
    part.style.width = `${Math.max(0.5, to - from)}%`;
    track.append(part);
  };
  band(Math.max(0, start), Math.min(100, end));
  if (end > 100) band(0, end - 100); // A session that runs past local midnight wraps.
  const now = percent(Date.now());
  if (now >= 0 && now <= 100) {
    const marker = el('i', 'mh-now');
    marker.style.left = `${now}%`;
    marker.title = `Now, ${new Date().toLocaleTimeString([], HOUR_MINUTE)}`;
    track.append(marker);
  }
  const ticks = el('div', 'mh-ticks');
  ['00:00', '06:00', '12:00', '18:00', '24:00'].forEach((tick) => ticks.append(el('span', '', tick)));
  wrap.append(edges, track, ticks);
  return wrap;
}
function renderMarketHours() {
  const box = $('#market-hours');
  const state = marketState();
  box.replaceChildren();
  box.hidden = !state;
  if (!state) return;
  const open = Boolean(state.current);
  const session = state.current || state.next;
  box.className = `market-hours ${open ? 'is-open' : 'is-closed'}`;
  const head = el('div', 'mh-head');
  head.append(el('span', 'mh-pill', open ? 'US market open' : 'US market closed'));
  if (session) {
    head.append(el('strong', 'mh-when', open
      ? `Closes ${localTime(session.close_at)} · ${untilText(session.close_at)}`
      : `Opens ${dayLabel(session.open_at)}, ${localTime(session.open_at)} · ${untilText(session.open_at)}`));
  }
  box.append(head);
  if (!session) return;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const hours = el('p', 'mh-hours');
  hours.append(document.createTextNode(`Trading hours ${dayLabel(session.open_at)} `),
    el('b', '', `${localTime(session.open_at)} – ${localTime(session.close_at)}`),
    document.createTextNode(` your time${zone ? ` (${zone.replaceAll('_', ' ')})` : ''} · ${session.open_new_york}–${session.close_new_york} New York`));
  if (session.early_close) hours.append(el('em', 'mh-early', 'Early close'));
  box.append(hours, sessionBar(session, open));
  const later = state.sessions.filter((item) => item !== session).slice(0, 4)
    .map((item) => `${dayLabel(item.open_at)} ${localTime(item.open_at)}–${localTime(item.close_at)}${item.early_close ? ' (early close)' : ''}`);
  box.append(el('p', 'mh-foot', `${later.length ? `Then: ${later.join(' · ')}. ` : ''}The autopilot only buys and sells inside these hours. ${marketHours.note}`));
}

function ago(iso) {
  if (!iso) return 'never';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}
function safeLink(url, text) {
  const node = document.createElement(url && /^https:\/\//.test(url) ? 'a' : 'span');
  node.textContent = text;
  if (node.tagName === 'A') { node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer'; }
  return node;
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderAutopilotBar(ap) {
  const bar = $('#autopilot-bar');
  bar.className = 'autopilot-bar' + (ap.enabled ? ' on' : '') + (ap.status === 'attention' ? ' attention' : '');
  bar.replaceChildren();
  const left = el('div');
  const stateLine = el('div', 'ap-state');
  stateLine.append(el('i'), el('span', '', ap.enabled ? `Autopilot on · ${ap.status.replaceAll('_', ' ')}` : 'Autopilot off'));
  left.append(stateLine);
  let message = ap.message || (ap.enabled ? 'Waiting for the next check.' : 'Research keeps running. No automatic buys or evidence exits are sent.');
  const nextSession = marketState()?.next;
  if (ap.enabled && ap.status === 'waiting_for_market' && nextSession) {
    message = `US market closed. Trading resumes ${dayLabel(nextSession.open_at)} at ${localTime(nextSession.open_at)} your time (${untilText(nextSession.open_at)}); no buys or exits until then.`;
  }
  left.append(el('p', 'ap-message', message));
  const s = ap.settings;
  left.append(el('p', 'ap-meta', `${ap.positions_used}/${s.max_positions} positions · ${ap.buys_today}/${s.max_new_per_day} buys today · €${s.budget_eur_per_position} each · checks every ${Math.round(ap.entry_interval_seconds / 60)} min in US market hours · last check ${ago(ap.last_checked_at)}`));
  const button = el('button', ap.enabled ? 'outline' : 'primary small', ap.enabled ? 'Pause autopilot' : 'Set up autopilot');
  button.type = 'button';
  button.addEventListener('click', async () => {
    if (!ap.enabled) { $('#autopilot-config').open = true; $('#autopilot-config').scrollIntoView({ behavior: 'smooth', block: 'center' }); return; }
    setBusy(button, true, true);
    try { await api('/api/autopilot/pause', { method: 'POST' }); await refreshWatchlist(); }
    catch (error) { $('#ap-result').textContent = error.message; }
    finally { setBusy(button, false); }
  });
  bar.append(left, button);
  const status = document.querySelector('header .status');
  if (status) { status.replaceChildren(el('i'), document.createTextNode(` Local app · Alpaca paper · Autopilot ${ap.enabled ? 'on' : 'off'}`)); }
}

function renderSources(research) {
  const strip = $('#source-strip');
  strip.replaceChildren();
  research.sources.forEach((source) => {
    const chip = el('div', `source-chip ${source.status}`);
    chip.title = source.message || `${source.label}: weight ${source.weight}, half-life ${source.half_life_days} days`;
    chip.append(el('strong', '', source.label));
    const detail = source.status === 'running' ? 'refreshing now…'
      : source.status === 'idle' ? 'waiting for first run'
      : `${ago(source.last_finished_at)} · ${source.total_signals} signals${source.status !== 'ok' ? ' · ' + source.status : ''}`;
    chip.append(el('span', '', detail));
    strip.append(chip);
  });
  $('#research-refresh').textContent = research.refresh_running ? 'Refreshing…' : 'Refresh sources';
  $('#research-refresh').disabled = research.refresh_running;
}

function rowMatches(row) {
  if (watchFilter === 'all') return true;
  if (watchFilter === 'act') return ['BUY', 'SELL', 'BUY_BLOCKED'].includes(row.action);
  if (watchFilter === 'radar') return row.origin === 'radar';
  return row.action === watchFilter;
}

function watchButton(label, handler) {
  const button = el('button', 'text-btn', label);
  button.type = 'button';
  button.addEventListener('click', async () => {
    setBusy(button, true, true);
    try { await handler(); } catch (error) { $('#watch-message').textContent = error.message; }
    finally { setBusy(button, false); }
  });
  return button;
}

function renderWatchRow(row, thresholds) {
  const card = el('article', `watch-row a-${row.action}${row.muted ? ' is-muted' : ''}`);
  const main = el('div', 'wr-main');
  main.append(el('span', 'action-pill', ACTION_LABELS[row.action] || row.action));
  const name = el('div', 'wr-name');
  name.append(el('strong', '', row.symbol), el('span', '', row.name || ''), el('em', '', row.origin));
  main.append(name);
  const score = el('div', 'wr-score');
  const track = el('div', 'score-track');
  const fill = el('i'); fill.style.width = `${Math.max(2, Math.min(100, row.score))}%`;
  const marker = el('b'); marker.style.left = `${thresholds.buy}%`; marker.title = `Buy score ${thresholds.buy}`;
  track.append(fill, marker);
  score.append(track, el('span', '', Math.round(row.score)));
  score.title = `Evidence score ${row.score} (50 = no evidence). ${row.supporting_count} supporting, ${row.cautionary_count} cautionary signals.`;
  main.append(score);
  const sources = el('div', 'wr-sources');
  Object.entries(SOURCE_TAGS).forEach(([key, [tag, label]]) => {
    const value = row.sources[key];
    const node = el('abbr', value > 0.02 ? 'pos' : value < -0.02 ? 'neg' : '', tag);
    node.title = value !== undefined ? `${label}: ${value > 0 ? '+' : ''}${value.toFixed(2)}` : `${label}: no recent evidence`;
    sources.append(node);
  });
  main.append(sources);
  card.append(main, el('p', 'wr-reason', row.reason));
  if (row.plan?.last_error) card.append(el('p', 'broker-warning', row.plan.last_error));
  if (row.evidence.length) {
    const list = el('ul', 'wr-evidence');
    row.evidence.slice(0, 3).forEach((item) => {
      const li = el('li');
      li.append(el('span', 'src', SOURCE_NAMES[item.source] || item.source), safeLink(item.url, item.headline));
      li.lastChild.title = item.headline;
      li.append(el('b', item.contribution >= 0 ? 'pos' : 'neg', `${item.contribution >= 0 ? '+' : ''}${item.contribution.toFixed(2)}`));
      list.append(li);
    });
    card.append(list);
  }
  const buttons = el('div', 'wr-buttons');
  buttons.append(watchButton('All evidence', () => showEvidence(row.symbol)));
  buttons.append(watchButton('Load in planner', async () => {
    form.elements.symbol.value = row.symbol;
    form.dispatchEvent(new Event('input', { bubbles: true }));
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  if (row.origin === 'radar') buttons.append(watchButton('Add to watchlist', async () => { await api('/api/watchlist', { method: 'POST', body: JSON.stringify({ symbol: row.symbol }) }); await refreshWatchlist(); }));
  else if (row.muted) buttons.append(watchButton('Unmute', async () => { await api(`/api/watchlist/${encodeURIComponent(row.symbol)}/mute`, { method: 'POST', body: JSON.stringify({ muted: false }) }); await refreshWatchlist(); }));
  else if (row.origin === 'manual') buttons.append(watchButton('Remove', async () => { await api(`/api/watchlist/${encodeURIComponent(row.symbol)}`, { method: 'DELETE' }); await refreshWatchlist(); }));
  else if (row.action !== 'HOLD' && row.action !== 'SELL') buttons.append(watchButton('Mute (never auto-buy)', async () => { await api(`/api/watchlist/${encodeURIComponent(row.symbol)}/mute`, { method: 'POST', body: JSON.stringify({ muted: true }) }); await refreshWatchlist(); }));
  card.append(buttons);
  return card;
}

function renderWatchRows() {
  const area = $('#watch-rows');
  area.replaceChildren();
  if (!watchBoard) return;
  const thresholds = { buy: watchBoard.autopilot.settings.min_score };
  const rows = watchBoard.rows.filter(rowMatches);
  if (!rows.length) {
    const research = watchBoard.research;
    area.append(el('p', 'empty', watchBoard.rows.length ? 'Nothing in this view right now.'
      : research.signals_in_horizon ? 'No symbol has enough evidence yet. Sources keep collecting in the background.'
      : 'Collecting the first research. The first full pass takes a few minutes; use "Refresh sources" to start it now.'));
    return;
  }
  rows.forEach((row) => area.append(renderWatchRow(row, thresholds)));
}

function renderDecisions(list) {
  const log = $('#decision-log');
  log.replaceChildren();
  if (!list.length) { log.append(el('li', 'empty-log', 'No autopilot actions yet. Buys, exits, skips and switch changes appear here with their reasons.')); return; }
  list.forEach((item) => {
    const li = el('li', `d-${item.action}`);
    const time = el('time', '', new Date(item.created_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
    time.dateTime = item.created_at;
    li.append(time, el('strong', '', item.symbol === '*' ? '—' : item.symbol));
    const text = el('span');
    text.append(el('span', 'd-state', `${item.action.replaceAll('_', ' ')}${item.state !== 'done' ? ' · ' + item.state : ''}`), document.createTextNode(item.reason));
    li.append(text);
    log.append(li);
  });
}

function fillSettings(settings) {
  const config = $('#autopilot-form');
  if (config.dataset.touched === 'true') return;
  SETTING_FIELDS.forEach((field) => { if (config.elements[field]) config.elements[field].value = settings[field]; });
  config.elements.block_on_caution.checked = settings.block_on_caution;
  config.elements.exit_on_bearish_evidence.checked = settings.exit_on_bearish_evidence;
}
function settingsPayload() {
  const config = $('#autopilot-form');
  const payload = {};
  SETTING_FIELDS.forEach((field) => { payload[field] = Number(config.elements[field].value); });
  payload.block_on_caution = config.elements.block_on_caution.checked;
  payload.exit_on_bearish_evidence = config.elements.exit_on_bearish_evidence.checked;
  return payload;
}

async function refreshWatchlist() {
  try {
    watchBoard = await api('/api/watchlist');
    marketHours = watchBoard.market || null;
    renderMarketHours();
    renderAutopilotBar(watchBoard.autopilot);
    renderSources(watchBoard.research);
    renderWatchRows();
    renderDecisions(watchBoard.decisions);
    fillSettings(watchBoard.autopilot.settings);
    $('#watch-message').textContent = '';
    if (watchBoard.research.refresh_running && !researchPoll) researchPoll = setInterval(pollResearch, 4000);
  } catch (error) { $('#watch-message').textContent = error.message; }
}
async function pollResearch() {
  try {
    const status = await api('/api/research/status');
    renderSources(status);
    if (!status.refresh_running) { clearInterval(researchPoll); researchPoll = null; await refreshWatchlist(); }
  } catch (error) { clearInterval(researchPoll); researchPoll = null; }
}

async function showEvidence(symbol) {
  const box = $('#evidence-body');
  $('#evidence-title').textContent = symbol;
  box.replaceChildren(el('p', 'evidence-summary', 'Loading evidence…'));
  $('#evidence-modal').hidden = false;
  const data = await api(`/api/watchlist/${encodeURIComponent(symbol)}/evidence`);
  box.replaceChildren();
  $('#evidence-title').textContent = `${symbol}${data.name ? ' · ' + data.name : ''}`;
  const score = data.score;
  box.append(el('p', 'evidence-summary', score
    ? `Score ${score.score} from ${score.conviction} source(s). Net evidence ${score.net_evidence >= 0 ? '+' : ''}${score.net_evidence} after age decay and per-source caps. 50 means no evidence either way.`
    : 'No evidence in the last 60 days.'));
  const list = el('ul', 'evidence-list');
  data.evidence.forEach((item) => {
    const li = el('li');
    const meta = el('div', 'meta');
    meta.append(el('span', '', `${SOURCE_NAMES[item.source] || item.source} · ${item.kind.replaceAll('_', ' ')} · ${new Date(item.event_at).toLocaleDateString()}`),
      el('span', '', `now ${item.contribution >= 0 ? '+' : ''}${item.contribution.toFixed(3)}`));
    li.append(meta, safeLink(item.url, item.headline));
    list.append(li);
  });
  box.append(list);
  if (data.decisions.length) {
    box.append(el('h3', '', 'Autopilot decisions'));
    const decisions = el('ul', 'evidence-list');
    data.decisions.forEach((item) => decisions.append(el('li', '', `${new Date(item.created_at).toLocaleString()} · ${item.action} ${item.state} · ${item.reason}`)));
    box.append(decisions);
  }
}

document.querySelectorAll('.watch-filter button').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.watch-filter button').forEach((other) => other.classList.toggle('active', other === button));
  watchFilter = button.dataset.filter;
  renderWatchRows();
}));
$('#watch-add').addEventListener('submit', async (event) => {
  event.preventDefault();
  const symbol = $('#watch-symbol').value.trim();
  if (!symbol) return;
  try {
    await api('/api/watchlist', { method: 'POST', body: JSON.stringify({ symbol }) });
    $('#watch-symbol').value = '';
    await refreshWatchlist();
  } catch (error) { $('#watch-message').textContent = error.message; }
});
$('#watch-reload').addEventListener('click', refreshWatchlist);
$('#research-refresh').addEventListener('click', async () => {
  try {
    await api('/api/research/refresh', { method: 'POST', body: JSON.stringify({}) });
    $('#research-refresh').textContent = 'Refreshing…';
    $('#research-refresh').disabled = true;
    if (!researchPoll) researchPoll = setInterval(pollResearch, 4000);
  } catch (error) { $('#watch-message').textContent = error.message; }
});
$('#autopilot-form').addEventListener('input', () => {
  $('#autopilot-form').dataset.touched = 'true';
  autopilotPreviewToken = null;
  $('#ap-preview').hidden = true;
  $('#ap-ack').checked = false;
});
$('#autopilot-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('#ap-preview-button');
  setBusy(button, true, true);
  autopilotPreviewToken = null;
  $('#ap-preview').hidden = true;
  try {
    const response = await api('/api/autopilot/preview', { method: 'POST', body: JSON.stringify(settingsPayload()) });
    autopilotPreviewToken = response.preview_token;
    const summary = response.summary;
    const details = $('#ap-preview-details');
    details.replaceChildren();
    details.append(brokerLine('Paper account equity', dollars(summary.account_equity_usd || 0)));
    details.append(brokerLine('Per position', `€${summary.settings.budget_eur_per_position} ≈ ${dollars(summary.budget_usd_per_position)} (ECB ${summary.fx.reference_date})`));
    details.append(brokerLine('Maximum exposure', `${euros(summary.max_exposure_eur)} across ${summary.settings.max_positions} positions`));
    details.append(brokerLine('Loss if every stop fills at its level', euros(summary.stop_loss_at_full_exposure_eur)));
    details.append(brokerLine('Would buy now', summary.candidates_now.length ? summary.candidates_now.join(', ') : 'nothing yet: no symbol clears every gate'));
    details.append(brokerLine('US market', summary.market_open_now ? 'open now' : 'closed; waits for the open'));
    summary.warnings.forEach((warning) => details.append(el('p', 'broker-warning', warning)));
    $('#ap-ack').checked = false;
    $('#ap-activate').disabled = true;
    $('#ap-preview').hidden = false;
    $('#ap-result').textContent = '';
  } catch (error) { $('#ap-result').textContent = error.message; }
  finally { setBusy(button, false); }
});
$('#ap-ack').addEventListener('change', () => { $('#ap-activate').disabled = !$('#ap-ack').checked || !autopilotPreviewToken; });
$('#ap-activate').addEventListener('click', async () => {
  if (!autopilotPreviewToken || !$('#ap-ack').checked) return;
  setBusy($('#ap-activate'), true, true);
  try {
    await api('/api/autopilot/activate', { method: 'POST', body: JSON.stringify({ preview_token: autopilotPreviewToken, acknowledge_auto_orders: true }) });
    autopilotPreviewToken = null;
    $('#ap-preview').hidden = true;
    $('#autopilot-form').dataset.touched = 'false';
    $('#ap-result').textContent = 'Autopilot is on. It checks every 5 minutes while the US market is open.';
    await Promise.all([refreshWatchlist(), refreshAutomation()]);
  } catch (error) { $('#ap-result').textContent = error.message; }
  finally { setBusy($('#ap-activate'), false); }
});
$('#close-evidence').addEventListener('click', () => { $('#evidence-modal').hidden = true; });
$('#evidence-modal').addEventListener('click', (event) => { if (event.target.id === 'evidence-modal') $('#evidence-modal').hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') $('#evidence-modal').hidden = true; });

refreshWatchlist();
setInterval(() => { if (!document.hidden) refreshWatchlist(); }, 30000);
setInterval(() => { if (!document.hidden) renderMarketHours(); }, 20000);
