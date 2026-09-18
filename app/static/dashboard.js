'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let timer = null;
let allInterviewers = [];
let interviewersTotal = 0;
let anomalyPayload = null;
let showClosedAnomalies = false;
let anomalyRenderPending = false;
const interviewerSort = {key:'', dir:'desc'};
const anomalyNotes = {};
const METRIC_RGB = {success:'70,99,77', refusal:'217,120,98', share:'95,134,163'};
const ANOMALY_STATUS = {open:'Открыта', clarified:'Уточнена', resolved:'Устранена'};
const filters = {day:'', okrug:'', tik:'', precinct:''};

async function request(url, options = {}) {
  const response = await fetch(url, {credentials:'same-origin', cache:'no-store', ...options});
  if (!response.ok) {
    let message = 'Не удалось загрузить данные';
    try { message = (await response.json()).detail || message; } catch {}
    const error = new Error(message); error.status = response.status; throw error;
  }
  return response.json();
}
function option(value, label, selected) { return `<option value="${escapeHTML(value)}"${value === selected ? ' selected' : ''}>${escapeHTML(label)}</option>`; }
function number(value) { return new Intl.NumberFormat('ru-RU').format(value || 0); }
function dateLabel(value) {
  if (value === 'all') return 'Все дни';
  if (!value) return 'Нет данных';
  return new Intl.DateTimeFormat('ru-RU', {day:'numeric',month:'long',year:'numeric'}).format(new Date(value + 'T12:00:00'));
}
function showLogin(message = '') {
  $('#dashboard').hidden = true; $('#login-view').hidden = false; $('#login-error').textContent = message;
}
function showDashboard() { $('#login-view').hidden = true; $('#dashboard').hidden = false; }
function status(kind, text) { $('#live-status').className = `live ${kind}`; $('#live-status span').textContent = text; }

function renderColumns(target, items, {compact = false} = {}) {
  const max = Math.max(1, ...items.map(x => x.count));
  const maxHeight = compact ? 90 : 150;
  const box = $(target);
  box.innerHTML = `<div class="columns${compact ? ' compact' : ''}">${items.map(item => `<div class="column"><div class="column-value"><strong>${number(item.count)}</strong>${item.percent}%</div><div class="column-bar"></div><div class="column-label" title="${escapeHTML(item.label)}">${escapeHTML(item.label)}</div></div>`).join('')}</div>`;
  // CSP (style-src 'self', no unsafe-inline) drops style="" written via innerHTML;
  // assigning through the DOM style API below is unaffected and actually renders the bar height.
  box.querySelectorAll('.column-bar').forEach((el, index) => {
    const item = items[index];
    el.style.height = (item.count ? Math.max(4, item.count / max * maxHeight) : 0) + 'px';
  });
}
function renderSummary(summary) {
  const cards = [
    ['Всего анкет',summary.total,'За выбранный период','accent'],
    ['Отказались',summary.refusals,summary.total ? `${Math.round(summary.refusals*100/summary.total)}% от анкет` : 'Нет данных','warm'],
    ['Испортили бюллетень',summary.spoiled,'Отдельный вариант ответа',''],
    ['Интервьюеров',summary.interviewers,'Уникальных смен',''],
    ['УИК с данными',summary.uiks,'Охвачено участков',''],
    ['ТИК с данными',summary.tiks,'Охвачено территорий',''],
  ];
  $('#summary').innerHTML = cards.map(([label,value,note,kind]) => `<article class="metric ${kind}"><span>${escapeHTML(label)}</span><strong>${number(value)}</strong><small>${escapeHTML(note)}</small></article>`).join('');
}
function renderHours(items) {
  const max = Math.max(1, ...items.map(x => x.count));
  const box = $('#hours-chart');
  box.innerHTML = items.map(item => `<div class="hour"><b>${item.count || ''}</b><i></i><span>${escapeHTML(item.hour.slice(0,2))}</span></div>`).join('');
  box.querySelectorAll('.hour i').forEach((el, index) => { el.style.height = Math.max(3, items[index].count / max * 145) + 'px'; });
}
function renderGeo(data) {
  const level = filters.tik ? 'uik' : (filters.okrug ? 'tik' : 'okrug');
  const titles = {okrug:'Контроль по округам', tik:'Контроль по ТИК', uik:'Контроль по УИК'};
  const subheads = {okrug:'ТИК с данными', tik:'УИК с данными', uik:'УИК с данными'};
  $('#geo-title').textContent = titles[level];
  $('#geo-col5').textContent = subheads[level];
  const items = level === 'uik' ? data.uik_stats : level === 'tik' ? data.tik_stats : data.okrug_stats;
  const max = Math.max(1, ...items.map(x => x.total));
  const table = $('#geo-table');
  table.innerHTML = items.length ? items.map(item => {
    const name = level === 'uik' ? item.label : level === 'tik' ? item.tik : `Округ ${item.okrug}`;
    const cells = level === 'okrug'
      ? `<td>${number(item.total)}</td><td>${number(item.refusals)}</td><td>${number(item.spoiled)}</td><td>${number(item.tiks)}</td><td>${number(item.interviewers)}</td>`
      : level === 'tik'
      ? `<td>${number(item.total)}</td><td>${number(item.refusals)}</td><td>${number(item.spoiled)}</td><td>${number(item.uiks)}</td><td>${number(item.interviewers)}</td>`
      : `<td>${number(item.total)}</td><td>${number(item.refusals)}</td><td>${number(item.spoiled)}</td><td>${item.total ? '1' : '0'}</td><td>${number(item.interviewers)}</td>`;
    const clickable = level !== 'uik';
    const dataAttr = level === 'okrug' ? `data-okrug="${escapeHTML(item.okrug)}"` : level === 'tik' ? `data-tik="${escapeHTML(item.tik)}"` : '';
    return `<tr class="${clickable ? 'clickable' : ''}" ${dataAttr}><td class="territory">${escapeHTML(name)}</td>${cells}<td><div class="share"><i></i><span>${data.summary.total ? Math.round(item.total*100/data.summary.total) : 0}%</span></div></td></tr>`;
  }).join('') : '<tr><td colspan="7" class="empty">За выбранный период данных нет</td></tr>';
  table.querySelectorAll('.share i').forEach((el, index) => { el.style.width = (items[index].total / max * 70) + 'px'; });
  table.querySelectorAll('tr[data-okrug]').forEach(row => row.addEventListener('click', () => { filters.okrug = row.dataset.okrug; filters.tik = ''; filters.precinct = ''; load(); }));
  table.querySelectorAll('tr[data-tik]').forEach(row => row.addEventListener('click', () => { filters.tik = row.dataset.tik; filters.precinct = ''; load(); }));
}
function readRange(id) { const value = $('#' + id)?.value; return value === undefined || value === '' ? null : Number(value); }
function applyInterviewerFilter() {
  const query = ($('#interviewer-search')?.value || '').trim().toLowerCase();
  const rows = allInterviewers.map(item => ({
    ...item, success: item.total - item.refusals,
    refusal: item.total ? Math.round(item.refusals * 1000 / item.total) / 10 : 0,
    share: interviewersTotal ? Math.round(item.total * 1000 / interviewersTotal) / 10 : 0,
  }));
  const max = {success: Math.max(1, ...rows.map(r => r.success)), refusal: 100, share: Math.max(1, ...rows.map(r => r.share))};
  const ranges = ['success', 'refusal', 'share'].map(key => [key, readRange(`f-${key}-min`), readRange(`f-${key}-max`)]);
  let items = rows.filter(r => (!query || r.name.toLowerCase().includes(query))
    && ranges.every(([key, min, maxValue]) => (min === null || r[key] >= min) && (maxValue === null || r[key] <= maxValue)));
  if (interviewerSort.key) {
    const factor = interviewerSort.dir === 'asc' ? 1 : -1;
    items = [...items].sort((a, b) => (a[interviewerSort.key] - b[interviewerSort.key]) * factor || a.name.localeCompare(b.name, 'ru'));
  }
  document.querySelectorAll('.sort-button').forEach(button => {
    const active = button.dataset.sort === interviewerSort.key;
    button.classList.toggle('active', active);
    button.querySelector('span').textContent = active ? (interviewerSort.dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
  const empty = allInterviewers.length ? 'Совпадений не найдено' : 'Нет данных об интервьюерах';
  const body = $('#interviewers');
  body.innerHTML = items.length ? items.map(item => `<tr><td class="territory">${escapeHTML(item.name)}</td><td title="${escapeHTML(item.tik)}">${escapeHTML(item.precinct)}</td><td>${number(item.total)}</td><td>${number(item.refusals)}</td><td class="metric-cell" data-metric="success" data-level="${item.success / max.success}">${number(item.success)}</td><td class="metric-cell" data-metric="refusal" data-level="${item.refusal / max.refusal}">${item.refusal}%</td><td class="metric-cell" data-metric="share" data-level="${item.share / max.share}">${item.share}%</td></tr>`).join('') : `<tr><td colspan="7" class="empty">${empty}</td></tr>`;
  body.querySelectorAll('td.metric-cell').forEach(cell => {
    cell.style.background = `rgba(${METRIC_RGB[cell.dataset.metric]}, ${0.1 + 0.5 * Number(cell.dataset.level)})`;
  });
}
function renderInterviewers(items, total) {
  allInterviewers = items;
  interviewersTotal = total;
  applyInterviewerFilter();
}
function renderHeatmap(heat) {
  $('#heatmap-head').innerHTML = `<tr><th class="sticky-col">Ответ</th>${heat.okrugs.map(o => `<th>Округ ${escapeHTML(o.okrug)}<small>n = ${number(o.total)}</small></th>`).join('')}<th>Всего</th></tr>`;
  const body = $('#heatmap-body');
  body.innerHTML = heat.rows.map(row => {
    const service = row.label === 'Испортил бюллетень' || row.label === 'Отказался отвечать';
    const top = Math.max(0, ...row.cells.map(c => c.percent));
    return `<tr class="${service ? 'service-row' : ''}"><th>${escapeHTML(row.label)}</th>${row.cells.map(c => `<td class="heat" data-level="${top ? c.percent / top : 0}" title="${number(c.count)} анкет">${c.percent}%</td>`).join('')}<td>${number(row.total)}</td></tr>`;
  }).join('');
  body.querySelectorAll('tr').forEach(tr => {
    const rgb = tr.classList.contains('service-row') ? '217,120,98' : '70,99,77';
    tr.querySelectorAll('td.heat').forEach(cell => {
      const level = Number(cell.dataset.level);
      cell.style.background = level ? `rgba(${rgb}, ${0.08 + 0.72 * level})` : '';
      cell.classList.toggle('heat-dark', level > 0.6);
    });
  });
}
function anomalyEditing() { return Boolean(document.activeElement?.matches?.('#anomalies input')); }
function anomalyCard(item) {
  const id = escapeHTML(item.id);
  const closed = item.status !== 'open';
  const when = /^\d{4}-\d{2}-\d{2}$/.test(item.day) ? dateLabel(item.day) : item.day;
  const controls = closed
    ? `<div class="anomaly-controls"><span class="answer">${ANOMALY_STATUS[item.status]}</span>${item.note ? `<small class="anomaly-note">${escapeHTML(item.note)}</small>` : ''}<button class="ghost-button" type="button" data-anomaly="${id}" data-status="open">Вернуть в работу</button></div>`
    : `<div class="anomaly-controls"><input type="text" maxlength="500" placeholder="Комментарий (необязательно)" aria-label="Комментарий" data-note-for="${id}" value="${escapeHTML(anomalyNotes[item.id] || '')}"><button class="ghost-button" type="button" data-anomaly="${id}" data-status="clarified">Уточнена</button><button class="ghost-button" type="button" data-anomaly="${id}" data-status="resolved">Устранена</button></div>`;
  return `<article class="anomaly ${item.severity}${closed ? ' closed' : ''}"><span class="severity">${item.severity === 'high' ? 'Высокая' : 'Средняя'}</span><div class="anomaly-main"><strong>${escapeHTML(item.title)}</strong><p>${escapeHTML(item.detail)}</p><small>${escapeHTML(item.interviewer)} · ${escapeHTML(item.precinct)} · ${escapeHTML(item.tik)} · ${escapeHTML(when)}</small></div>${controls}</article>`;
}
function renderAnomalies(payload) {
  if (payload) anomalyPayload = payload;
  if (!anomalyPayload) return;
  // A refresh must not wipe a comment the coordinator is typing.
  if (anomalyEditing()) { anomalyRenderPending = true; return; }
  anomalyRenderPending = false;
  const {items, open, closed, statuses_ok} = anomalyPayload;
  $('#anomaly-summary').textContent = `Открыто: ${open} · закрыто: ${closed}`;
  $('#anomaly-notice').hidden = statuses_ok !== false;
  const visible = items.filter(item => showClosedAnomalies || item.status === 'open');
  $('#anomalies').innerHTML = visible.length ? visible.map(anomalyCard).join('')
    : `<p class="empty">${items.length ? 'Открытых аномалий нет. Включите «Показывать закрытые», чтобы увидеть остальные.' : 'Аномалий не обнаружено.'}</p>`;
}
function renderRecent(items) {
  $('#recent').innerHTML = items.length ? items.map(item => `<div class="recent-row"><div class="recent-time">${escapeHTML(item.time)}</div><div class="recent-place"><strong>${escapeHTML(item.precinct)}</strong><span>${escapeHTML(item.tik)}</span></div><span class="answer ${item.answer === 'Отказался отвечать' ? 'refused' : ''}">${escapeHTML(item.answer)}</span></div>`).join('') : '<p class="empty">Пока нет анкет</p>';
}
function renderFilters(data) {
  filters.day = data.selected_day; filters.okrug = data.selected_okrug;
  filters.tik = data.selected_tik; filters.precinct = data.selected_precinct;
  const dayOptions = data.available_dates.length
    ? data.available_dates.map(day => option(day,dateLabel(day),filters.day)).join('')
    : (filters.day !== 'all' ? option(filters.day,dateLabel(filters.day),filters.day) : '');
  $('#day').innerHTML = option('all','Все дни',filters.day) + dayOptions;
  $('#okrug').innerHTML = option('','Все округа',filters.okrug) + data.filters.okrugs.map(o => option(o,'Округ ' + o,filters.okrug)).join('');
  const tikPlaceholder = filters.okrug ? 'Все ТИК' : 'Сначала выберите округ';
  $('#tik').innerHTML = option('',tikPlaceholder,filters.tik) + data.filters.tiks.map(tik => option(tik,tik,filters.tik)).join('');
  $('#tik').disabled = !filters.okrug;
  $('#precinct').innerHTML = option('','Все УИК',filters.precinct) + data.filters.precincts.map(p => option(p.id,p.label,filters.precinct)).join('');
  $('#precinct').disabled = !filters.tik;
}
function render(data) {
  renderFilters(data); renderSummary(data.summary); renderColumns('#party-chart',data.parties);
  renderColumns('#gender-chart',data.genders,{compact:true}); renderColumns('#age-chart',data.ages,{compact:true}); renderHours(data.hours);
  renderColumns('#newpeople-chart',data.new_people_by_okrug); renderHeatmap(data.party_okrug);
  renderGeo(data); renderInterviewers(data.interviewers, data.summary.total); renderAnomalies(data.anomalies); renderRecent(data.recent);
  const scopeLabel = filters.tik || (filters.okrug ? 'Округ ' + filters.okrug : '');
  $('#period-label').textContent = `${dateLabel(data.selected_day)}${scopeLabel ? ' · ' + scopeLabel : ''}`;
  $('#updated-at').textContent = 'Обновлено в ' + new Date(data.generated_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
async function load() {
  clearTimeout(timer); $('#refresh').disabled = true; status('', 'Обновляем данные'); $('#data-error').hidden = true;
  const query = new URLSearchParams();
  if (filters.day) query.set('day',filters.day); if (filters.okrug) query.set('okrug',filters.okrug);
  if (filters.tik) query.set('tik',filters.tik); if (filters.precinct) query.set('precinct',filters.precinct);
  try {
    const data = await request('/api/dashboard/data?' + query); render(data); showDashboard(); status('ready','Данные актуальны');
  } catch (error) {
    if (error.status === 401) return showLogin('Сессия завершилась. Введите код ещё раз.');
    $('#data-error').textContent = error.message; $('#data-error').hidden = false; status('error','Ошибка обновления');
  } finally { $('#refresh').disabled = false; timer = setTimeout(load,30000); }
}
$('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#login-error').textContent = '';
  try { await request('/api/dashboard/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('#code').value})}); $('#code').value=''; await load(); }
  catch(error) { $('#login-error').textContent = error.message; }
});
$('#refresh').addEventListener('click',load);
$('#day').addEventListener('change',event => { filters.day=event.target.value; filters.precinct=''; load(); });
$('#okrug').addEventListener('change',event => { filters.okrug=event.target.value; filters.tik=''; filters.precinct=''; load(); });
$('#tik').addEventListener('change',event => { filters.tik=event.target.value; filters.precinct=''; load(); });
$('#precinct').addEventListener('change',event => { filters.precinct=event.target.value; load(); });
document.addEventListener('input', event => {
  if (event.target.id === 'interviewer-search' || event.target.closest?.('.range-filters')) applyInterviewerFilter();
  if (event.target.dataset?.noteFor) anomalyNotes[event.target.dataset.noteFor] = event.target.value;
});
document.querySelectorAll('.sort-button').forEach(button => button.addEventListener('click', () => {
  if (interviewerSort.key !== button.dataset.sort) { interviewerSort.key = button.dataset.sort; interviewerSort.dir = 'desc'; }
  else if (interviewerSort.dir === 'desc') interviewerSort.dir = 'asc';
  else interviewerSort.key = '';
  applyInterviewerFilter();
}));
$('#f-reset').addEventListener('click', () => {
  document.querySelectorAll('.range-filters input').forEach(input => { input.value = ''; });
  applyInterviewerFilter();
});
$('#anomaly-show-closed').addEventListener('change', event => { showClosedAnomalies = event.target.checked; renderAnomalies(); });
$('#anomalies').addEventListener('focusout', () => setTimeout(() => { if (anomalyRenderPending && !anomalyEditing()) renderAnomalies(); }, 0));
$('#anomalies').addEventListener('click', async event => {
  const button = event.target.closest('button[data-anomaly]');
  if (!button) return;
  const id = button.dataset.anomaly;
  button.disabled = true;
  try {
    await request('/api/dashboard/anomalies/status', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, status: button.dataset.status, note: anomalyNotes[id] || ''})});
    delete anomalyNotes[id];
    await load();
  } catch (error) {
    $('#data-error').textContent = error.message; $('#data-error').hidden = false; button.disabled = false;
  }
});
document.addEventListener('visibilitychange',() => { if (!document.hidden && !$('#dashboard').hidden) load(); });
(async function init(){ try { await request('/api/dashboard/session'); await load(); } catch(error) { showLogin(error.status === 503 ? error.message : ''); } })();
