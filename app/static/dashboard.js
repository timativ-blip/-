'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let timer = null;
const filters = {day:'', tik:'', precinct:''};

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
  const uikMode = Boolean(filters.tik);
  $('#geo-title').textContent = uikMode ? 'Контроль по УИК' : 'Контроль по ТИК';
  const items = uikMode ? data.uik_stats : data.tik_stats;
  const max = Math.max(1, ...items.map(x => x.total));
  const table = $('#geo-table');
  table.innerHTML = items.length ? items.map(item => {
    const name = uikMode ? item.label : item.tik;
    const cells = uikMode
      ? `<td>${number(item.total)}</td><td>${number(item.refusals)}</td><td>${number(item.spoiled)}</td><td>${item.total ? '1' : '0'}</td><td>${number(item.interviewers)}</td>`
      : `<td>${number(item.total)}</td><td>${number(item.refusals)}</td><td>${number(item.spoiled)}</td><td>${number(item.uiks)}</td><td>${number(item.interviewers)}</td>`;
    return `<tr class="${uikMode ? '' : 'clickable'}" ${uikMode ? '' : `data-tik="${escapeHTML(item.tik)}"`}><td class="territory">${escapeHTML(name)}</td>${cells}<td><div class="share"><i></i><span>${data.summary.total ? Math.round(item.total*100/data.summary.total) : 0}%</span></div></td></tr>`;
  }).join('') : '<tr><td colspan="7" class="empty">За выбранный период данных нет</td></tr>';
  table.querySelectorAll('.share i').forEach((el, index) => { el.style.width = (items[index].total / max * 70) + 'px'; });
  document.querySelectorAll('#geo-table tr[data-tik]').forEach(row => row.addEventListener('click', () => { filters.tik = row.dataset.tik; filters.precinct = ''; load(); }));
}
function renderInterviewers(items) {
  $('#interviewers').innerHTML = items.length ? items.map(item => `<tr><td class="territory">${escapeHTML(item.name)}</td><td title="${escapeHTML(item.tik)}">${escapeHTML(item.precinct)}</td><td>${number(item.total)}</td><td>${number(item.refusals)}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">Нет данных об интервьюерах</td></tr>';
}
function renderRecent(items) {
  $('#recent').innerHTML = items.length ? items.map(item => `<div class="recent-row"><div class="recent-time">${escapeHTML(item.time)}</div><div class="recent-place"><strong>${escapeHTML(item.precinct)}</strong><span>${escapeHTML(item.tik)}</span></div><span class="answer ${item.answer === 'Отказался отвечать' ? 'refused' : ''}">${escapeHTML(item.answer)}</span></div>`).join('') : '<p class="empty">Пока нет анкет</p>';
}
function renderFilters(data) {
  filters.day = data.selected_day; filters.tik = data.selected_tik; filters.precinct = data.selected_precinct;
  const dayOptions = data.available_dates.length
    ? data.available_dates.map(day => option(day,dateLabel(day),filters.day)).join('')
    : (filters.day !== 'all' ? option(filters.day,dateLabel(filters.day),filters.day) : '');
  $('#day').innerHTML = option('all','Все дни',filters.day) + dayOptions;
  $('#tik').innerHTML = option('','Все ТИК',filters.tik) + data.filters.tiks.map(tik => option(tik,tik,filters.tik)).join('');
  $('#precinct').innerHTML = option('','Все УИК',filters.precinct) + data.filters.precincts.map(p => option(p.id,p.label,filters.precinct)).join('');
  $('#precinct').disabled = !filters.tik;
}
function render(data) {
  renderFilters(data); renderSummary(data.summary); renderColumns('#party-chart',data.parties);
  renderColumns('#gender-chart',data.genders,{compact:true}); renderColumns('#age-chart',data.ages,{compact:true}); renderHours(data.hours);
  renderGeo(data); renderInterviewers(data.interviewers); renderRecent(data.recent);
  $('#period-label').textContent = `${dateLabel(data.selected_day)}${filters.tik ? ' · ' + filters.tik : ''}`;
  $('#updated-at').textContent = 'Обновлено в ' + new Date(data.generated_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
async function load() {
  clearTimeout(timer); $('#refresh').disabled = true; status('', 'Обновляем данные'); $('#data-error').hidden = true;
  const query = new URLSearchParams();
  if (filters.day) query.set('day',filters.day); if (filters.tik) query.set('tik',filters.tik); if (filters.precinct) query.set('precinct',filters.precinct);
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
$('#tik').addEventListener('change',event => { filters.tik=event.target.value; filters.precinct=''; load(); });
$('#precinct').addEventListener('change',event => { filters.precinct=event.target.value; load(); });
document.addEventListener('visibilitychange',() => { if (!document.hidden && !$('#dashboard').hidden) load(); });
(async function init(){ try { await request('/api/dashboard/session'); await load(); } catch(error) { showLogin(error.status === 503 ? error.message : ''); } })();
