'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let timer = null;
let allInterviewers = [];
let interviewersTotal = 0;
let anomalyPayload = null;
let lastAgeHeat = null;
let activeDetail = null;
let mapGeo = null;
let mapPayload = null;
let mapLayer = 'share';
let mapLevel = 'tik';
let mapBuilt = false;
let focusParty = 'Новые люди';
try { focusParty = localStorage.getItem('focusParty') || focusParty; } catch { /* storage may be blocked */ }
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

const PARTY_COLORS = {'Единая Россия':'#2C5FA8', 'КПРФ':'#CF3B34', 'ЛДПР':'#E2A91B', 'Новые люди':'#1AA7A0', 'Справедливая Россия':'#F0862A',
  'Зелёные':'#5BA44F', 'Родина':'#8B4A3A', 'Яблоко':'#88B04B', 'Партия прямой демократии':'#7A5BB5', 'Партия пенсионеров':'#B5678F',
  'Коммунисты России':'#8E1F3A', 'Испортил бюллетень':'#9AA39C', 'Отказался отвечать':'#D97862'};
const GENDER_COLORS = {'Мужской':'#3F7CC4', 'Женский':'#B03A6B'};
const NEUTRAL_COLOR = '#B9C4BA';
const PARTY_LOGOS = {'Единая Россия':'edinaya-rossiya', 'КПРФ':'kprf', 'ЛДПР':'ldpr', 'Новые люди':'novye-lyudi', 'Справедливая Россия':'spravedlivaya-rossiya',
  'Зелёные':'zelenye', 'Родина':'rodina', 'Яблоко':'yabloko', 'Партия прямой демократии':'pryamaya-demokratiya', 'Партия пенсионеров':'pensionery',
  'Коммунисты России':'kommunisty-rossii'};
let lastParties = null, lastPartyCompare = null, lastPartySelected = '';
let partyForecastOn = (() => { try { return localStorage.getItem('partyForecast') !== 'off'; } catch { return true; } })();
let partyCompareOn = (() => { try { return localStorage.getItem('partyCompare') !== 'off'; } catch { return true; } })();
let kpiBase = (() => { try { return localStorage.getItem('kpiBase') === 'avg2' ? 'avg2' : 'prev'; } catch { return 'prev'; } })();
let lastCompare = null, lastSummary = null;
let forecastMode = (() => { try { return localStorage.getItem('forecastMode') === 'day' ? 'day' : 'all'; } catch { return 'all'; } })();
let lastForecastData = null;
const FORECAST_NOTE_ALL = 'Прогноз считается по всем данным (все дни, вся область) и не зависит от фильтров. «Изменение» — прогноз минус «Ответили». «Тренд» — на сколько сдвинулась оценка за последние 30% поступивших данных. Это оценка по опросу на участках, а не официальный результат.';
// One hue for every other chart: the bigger the value, the darker and richer the bar (scaled between the chart's own min and max).
function scaleColor(value, values) {
  const low = Math.min(...values), high = Math.max(...values), from = [196, 216, 190], to = [33, 70, 48];
  const t = high === low ? 0.6 : 0.12 + 0.88 * (value - low) / (high - low);
  return `rgb(${from.map((c, i) => Math.round(c + (to[i] - c) * t)).join(',')})`;
}
function renderColumns(target, items, {compact = false, kind = '', keyOf = item => item.label, colors = null, logos = null, fill = false} = {}) {
  const max = Math.max(1, ...items.map(x => x.count));
  let maxHeight = compact ? 90 : 150;
  const box = $(target);
  const logo = item => logos && logos[item.label] ? `<span class="column-logo"><img src="/static/logos/${logos[item.label]}.png" alt="" width="34" height="34" loading="lazy"></span>` : logos ? '<span class="column-logo empty"></span>' : '';
  box.innerHTML = `<div class="columns${compact ? ' compact' : ''}${fill ? ' fill' : ''}">${items.map(item => `<div class="column"${kind ? ` data-kind="${kind}" data-key="${escapeHTML(keyOf(item))}" role="button" tabindex="0"` : ''}>${logo(item)}<div class="column-value"><strong>${number(item.count)}</strong>${item.percent}%</div><div class="column-bar"></div><div class="column-label" title="${escapeHTML(item.title || item.label)}">${escapeHTML(item.label)}</div></div>`).join('')}</div>`;
  // CSP (style-src 'self', no unsafe-inline) drops style="" written via innerHTML;
  // assigning through the DOM style API below is unaffected and actually renders the bar height.
  if (fill) {  // stretch the bars to the room the panel gives the chart: logo 48 + value 42 + label 34 + gaps
    const room = box.querySelector('.columns').clientHeight;
    maxHeight = room > 0 ? Math.max(120, room - 150) : 300;
  }
  box.querySelectorAll('.column-bar').forEach((el, index) => {
    const item = items[index];
    el.style.height = (item.count ? Math.max(4, item.count / max * maxHeight) : 0) + 'px';
    el.style.background = colors ? (colors[item.label] || NEUTRAL_COLOR) : scaleColor(item.count, items.map(x => x.count));
  });
}
function shortDay(iso) { return iso.slice(8, 10) + '.' + iso.slice(5, 7); }
function hatch(color) { return `repeating-linear-gradient(135deg, ${color} 0 3px, rgba(255,255,255,.7) 3px 6px)`; }
function renderPartyChart() {
  if (!lastParties) return;
  const compare = lastPartyCompare && lastPartyCompare.average ? lastPartyCompare : null;
  const dayInfo = lastForecastData && lastForecastData.forecast_day;
  const forecastRows = (forecastMode === 'day' ? dayInfo && dayInfo.forecast : lastForecastData && lastForecastData.forecast);
  const forecastOf = new Map((forecastRows ? forecastRows.rows : []).map(r => [r.label, r.forecast]));
  $('#party-compare-switch').hidden = !compare;
  $('#party-forecast-switch').hidden = !forecastOf.size;
  $('#party-compare').checked = partyCompareOn;
  $('#party-forecast').checked = partyForecastOn;
  const showAverage = Boolean(compare) && partyCompareOn, showForecast = forecastOf.size > 0 && partyForecastOn;
  if (!showAverage && !showForecast) {
    $('#party-legend').innerHTML = '';
    renderColumns('#party-chart', lastParties, {kind:'party', colors:PARTY_COLORS, logos:PARTY_LOGOS, fill:true});
    return;
  }
  const items = lastParties, box = $('#party-chart');
  const average = showAverage ? compare.average : null;
  const averageDays = showAverage ? compare.days.map(d => shortDay(d.day)).join(', ') : '';
  const mainLabel = lastPartySelected === 'all' ? 'Все дни' : shortDay(lastPartySelected || (compare && compare.selected) || '0000-00-00');
  const forecastTitle = forecastMode === 'day' && dayInfo && dayInfo.day ? `Прогноз на день (${shortDay(dayInfo.day)})` : 'Общий прогноз';
  const avgIndex = 1, forecastIndex = average ? 2 : 1;
  const valuesOf = item => [item.percent, ...(average ? [average.shares[item.label] ?? 0] : []), ...(showForecast ? [forecastOf.get(item.label) ?? 0] : [])];
  const top = Math.max(1, ...items.flatMap(valuesOf));
  const bar = (cls, i) => `<div class="column-bar ${cls}" data-i="${i}"></div>`;
  const notes = item => {
    const lines = [];
    if (average) {
      const diff = Math.round((item.percent - (average.shares[item.label] ?? 0)) * 10) / 10;
      lines.push(`<div class="column-delta ${diff > 0 ? 'up' : diff < 0 ? 'down' : ''}" title="Разница выбранного периода со средним по другим дням">${diff > 0 ? '▲' : diff < 0 ? '▼' : '='} ${Math.abs(diff)}</div>`);
    }
    if (showForecast && forecastOf.has(item.label)) lines.push(`<div class="column-fc" title="${escapeHTML(forecastTitle)}"><small>прогноз</small>${forecastOf.get(item.label)}%</div>`);
    return lines.join('');
  };
  box.innerHTML = `<div class="columns fill compare">${items.map(item => `<div class="column" data-kind="party" data-key="${escapeHTML(item.label)}" role="button" tabindex="0">${PARTY_LOGOS[item.label] ? `<span class="column-logo"><img src="/static/logos/${PARTY_LOGOS[item.label]}.png" alt="" width="34" height="34" loading="lazy"></span>` : '<span class="column-logo empty"></span>'}<div class="column-value"><strong>${number(item.count)}</strong>${item.percent}%</div><div class="bars">${bar('main', 0)}${average ? bar('avg', avgIndex) : ''}${showForecast ? bar('fc', forecastIndex) : ''}</div><div class="column-label" title="${escapeHTML(item.label)}">${escapeHTML(item.label)}</div>${notes(item)}</div>`).join('')}</div>`;
  const room = box.querySelector('.columns').clientHeight;
  const maxHeight = room > 0 ? Math.max(120, room - 225) : 250;
  box.querySelectorAll('.column').forEach((column, index) => {
    const item = items[index], color = PARTY_COLORS[item.label] || NEUTRAL_COLOR, values = valuesOf(item);
    column.querySelectorAll('.column-bar').forEach(el => {
      const i = Number(el.dataset.i), value = values[i];
      const isAvg = Boolean(average) && i === avgIndex, isForecast = showForecast && i === forecastIndex;
      el.style.height = (value ? Math.max(3, value / top * maxHeight) : 0) + 'px';
      if (isForecast) { el.style.background = color + '2E'; el.style.border = `2px solid ${color}`; el.style.borderBottom = '0'; }
      else el.style.background = isAvg ? hatch(color) : color;
      el.title = i === 0 ? `${item.label}, ${mainLabel}: ${value}%` : isForecast ? `${item.label}, ${forecastTitle}: ${value}% (доля среди назвавших с учётом отказавшихся)` : `${item.label}, среднее по другим дням (${averageDays}): ${value}%`;
    });
  });
  const key = (label, style) => `<span class="lg"><i data-style="${style}"></i>${escapeHTML(label)}</span>`;
  $('#party-legend').innerHTML = key(mainLabel, 'main') + (average ? key(`Среднее по другим дням (${averageDays})`, 'avg') : '') + (showForecast ? key(forecastTitle, 'fc') : '');
  $('#party-legend').querySelectorAll('i').forEach(el => {
    const style = el.dataset.style;
    el.style.background = style === 'avg' ? hatch('#46634d') : style === 'fc' ? '#46634d2E' : '#46634d';
    if (style === 'fc') el.style.border = '2px solid #46634d';
  });
}
function renderNewPeopleAge(groups) {
  renderColumns('#newpeople-age-chart', groups.map(g => ({...g, title: `${g.label}: ${number(g.count)} из ${number(g.total)} анкет`})), {kind:'age'});
  const votes = groups.reduce((sum, g) => sum + g.count, 0);
  const enough = groups.filter(g => g.total >= 20);
  const byRate = [...enough].sort((a, b) => b.percent - a.percent)[0];
  const byVotes = [...groups].sort((a, b) => b.count - a.count)[0];
  const parts = [];
  if (!votes) parts.push('Пока нет голосов за «Новых людей»');
  else {
    if (byRate) parts.push(`Чаще всего голосует группа ${byRate.label}: ${byRate.percent}% её анкет`);
    else parts.push('Мало данных: в каждой группе меньше 20 анкет');
    parts.push(`больше всего голосов у ${byVotes.label}: ${number(byVotes.count)}`);
  }
  $('#newpeople-age-insight').textContent = parts.join(' · ');
}
const SERIES_COLORS = ['#46634d', '#d97862', '#5f86a3', '#c9a227'];
function fmtSigned(value) { return (value > 0 ? '+' : '') + (Math.round(value * 10) / 10); }
function forecastMoment(iso) {
  return iso ? new Date(iso).toLocaleString('ru-RU', {day:'numeric', month:'long', hour:'2-digit', minute:'2-digit'}) : '—';
}
function renderForecastHistory(history) {
  const box = $('#forecast-history');
  if (history.points.length < 2) { box.innerHTML = '<p class="empty">Пока мало данных для динамики</p>'; return; }
  const width = 620, height = 220, left = 42, right = 12, top = 14, bottom = 30;
  const all = history.series.flatMap(s => s.values);
  const low = Math.max(0, Math.floor(Math.min(...all) - 1));
  const tickStep = Math.max(1, Math.ceil((Math.ceil(Math.max(...all) + 1) - low) / 3));
  const high = low + tickStep * 3;
  const x = i => left + (width - left - right) * i / (history.points.length - 1);
  const y = v => top + (height - top - bottom) * (1 - (v - low) / ((high - low) || 1));
  const grid = [0, 1, 2, 3].map(step => { const v = low + (high - low) * step / 3; return `<line x1="${left}" x2="${width - right}" y1="${y(v)}" y2="${y(v)}" stroke="#dfe7dc"/><text class="axis-label" x="${left - 6}" y="${y(v) + 3}" text-anchor="end">${Math.round(v * 10) / 10}%</text>`; }).join('');
  const ticks = history.points.map((point, i) => `<text class="axis-label" x="${x(i)}" y="${height - 10}" text-anchor="middle">${Math.round(point.fraction * 100)}%</text>`).join('');
  const lines = history.series.map((serie, k) => {
    const path = serie.values.map((v, i) => `${i ? 'L' : 'M'}${x(i)},${y(v)}`).join(' ');
    const dots = serie.values.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="${SERIES_COLORS[k]}"><title>${escapeHTML(serie.label)}: ${v}% · ${number(history.points[i].n)} анкет</title></circle>`).join('');
    return `<path d="${path}" fill="none" stroke="${SERIES_COLORS[k]}" stroke-width="2"/>${dots}`;
  }).join('');
  const legend = history.series.map((serie, k) => `<span class="legend-item"><svg width="10" height="10" aria-hidden="true"><rect width="10" height="10" rx="2" fill="${SERIES_COLORS[k]}"/></svg>${escapeHTML(serie.label)}</span>`).join('');
  box.innerHTML = `<svg class="history-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Динамика прогноза">${grid}${ticks}${lines}</svg><div class="legend">${legend}</div><p class="panel-note">Ось X — доля поступивших данных по времени заполнения анкет; на каждой точке прогноз пересчитан по всем анкетам до неё.</p>`;
}
function renderForecastMode() {
  const data = lastForecastData;
  if (!data) return;
  const dayInfo = data.forecast_day || {day: null, forecast: null};
  const label = dayInfo.day ? dayInfo.day.slice(8, 10) + '.' + dayInfo.day.slice(5, 7) : '';
  $('#forecast-day-button').textContent = label ? `Прогноз на день (${label})` : 'Прогноз на день';
  document.querySelectorAll('[data-fmode]').forEach(button => button.classList.toggle('active', button.dataset.fmode === forecastMode));
  $('#forecast-note').textContent = forecastMode === 'day'
    ? `Прогноз построен только по анкетам за ${dayInfo.day ? dateLabel(dayInfo.day) : 'выбранный день'} (вся область, фильтры округа и ТИК не влияют). Он показывает, как выглядит расклад сегодняшнего дня отдельно от прошлых; объём данных за один день меньше, поэтому коридор шире. «Изменение» — прогноз минус «Ответили». Это оценка по опросу на участках, а не официальный результат.`
    : FORECAST_NOTE_ALL;
  renderForecast(forecastMode === 'day' ? dayInfo.forecast : data.forecast, data.summary);
  renderPartyChart();
}
function renderForecast(forecast, summary) {
  const body = $('#forecast-body'), warn = $('#forecast-warn');
  if (!forecast) {
    body.innerHTML = '<tr><td colspan="7" class="empty">Недостаточно ответов для прогноза</td></tr>';
    for (const id of ['forecast-summary', 'forecast-steps', 'forecast-history', 'forecast-ages', 'deg-head', 'deg-body', 'forecast-deg']) $('#' + id).innerHTML = '';
    warn.hidden = true;
    return;
  }
  const {scope, rows} = forecast;
  const top = Math.max(1, ...rows.map(r => r.forecast));
  body.innerHTML = rows.map(r => `<tr><td class="territory">${escapeHTML(r.label)}</td><td>${r.answered}%</td><td><div class="share"><i></i><span><strong>${r.forecast}%</strong></span></div></td><td class="${r.delta > 0 ? 'delta-up' : r.delta < 0 ? 'delta-down' : ''}">${fmtSigned(r.delta)}</td><td>± ${r.margin} п.п.</td><td class="${r.trend > 0 ? 'delta-up' : r.trend < 0 ? 'delta-down' : ''}">${r.trend === null ? '—' : fmtSigned(r.trend)}</td><td>${r.y2021 === null ? '—' : `${r.y2021}% (${fmtSigned(r.forecast - r.y2021)})`}</td></tr>`).join('');
  body.querySelectorAll('.share i').forEach((el, index) => { el.style.width = (rows[index].forecast / top * 90) + 'px'; });
  const refusalPercent = scope.anket ? Math.round(scope.refusers * 1000 / scope.anket) / 10 : 0;
  $('#forecast-summary').textContent = `Данные: ${number(scope.anket)} анкет · ответили ${number(scope.respondents)} · отказались ${number(scope.refusers)} (${refusalPercent}%) · ТИК с данными ${scope.tiks_with_data} из ${scope.tiks_total}`;
  warn.hidden = scope.respondents >= 300;
  if (!warn.hidden) warn.textContent = `Ответивших мало (${number(scope.respondents)}): при таком объёме прогноз ненадёжен.`;

  const lead = rows.slice(0, 3);
  const effect = (from, to) => lead.map(r => `${r.label} ${fmtSigned(r[to] - r[from])}`).join(', ');
  const drift = Math.max(...lead.map(r => Math.abs(r.trend ?? 0)));
  const stable = lead.every(r => r.trend !== null) ? (drift <= 1 ? `оценка стабильна: у лидеров сдвиг не больше ${Math.round(drift * 10) / 10} п.п.` : `оценка ещё дрейфует (до ${Math.round(drift * 10) / 10} п.п.): данных пока недостаточно для устойчивого прогноза`) : 'для тренда пока мало данных';
  const steps = [
    `<strong>Ответившие.</strong> ${number(scope.respondents)} человек назвали партию: ${lead.map(r => `${escapeHTML(r.label)} ${r.answered}%`).join(', ')}.`,
    `<strong>Отказавшиеся.</strong> ${number(scope.refusers)} человек не назвали партию. Мы знаем их округ, пол и возраст, поэтому распределили их по партиям так же, как ответивших в той же группе; малочисленные группы сглажены. Эффект шага, п.п.: ${effect('answered', 'with_refusals')}. Эффект мал, когда отказавшиеся по составу похожи на ответивших.`,
    `<strong>Территории.</strong> Данные есть по ${scope.tiks_with_data} из ${scope.tiks_total} ТИК (${scope.covered_share}% электората области). Вес территории — число избирателей её округа (итоги 2021 как ориентир), между ТИК округа оно делится по числу УИК; ТИК без данных берут оценку своего округа. Эффект шага, п.п.: ${effect('with_refusals', 'forecast')}.`,
    `<strong>Поток.</strong> Оценка пересчитывалась по мере поступления анкет (график ниже): ${stable}.`,
    forecast.flow ? `<strong>Охват потока.</strong> К ${forecast.flow.as_of} ${escapeHTML(dateLabel(forecast.flow.day))} на участках проголосовало ${forecast.flow.voted_by_share}% дневного потока (${escapeHTML(forecast.flow.source)}), последняя анкета этого дня: ${forecastMoment(forecast.flow.last_at)}. Вечерние ${forecast.flow.evening_share}% потока опросом почти не охвачены. Выборка к ${forecast.flow.as_of} — ${number(forecast.flow.anket_by_as_of)} анкет, то есть ${forecast.flow.sample_fraction}% проголосовавших (${number(forecast.flow.paper_voters)}). Если вечерние избиратели голосуют иначе на 5 п.п., итог дня сдвинется примерно на ${(forecast.flow.evening_share * 0.05).toFixed(1)} п.п.` : null,
    `<strong>Что не учтено.</strong> Пока есть данные за ${scope.days.length} дн. (${escapeHTML(scope.days.join(', '))}), последняя анкета: ${forecastMoment(scope.last_at)}. Оставшееся время и дни предполагаются такими же по структуре голосующих; когда придут новые анкеты, прогноз пересчитается сам. Электронное голосование опросом не охвачено (сценарии ниже).`,
  ];
  $('#forecast-steps').innerHTML = steps.filter(Boolean).map(item => `<li>${item}</li>`).join('');
  renderForecastHistory(forecast.history);
  $('#forecast-ages').innerHTML = forecast.ages.map(a => `<tr><td class="territory">${escapeHTML(a.age)}</td><td>${a.sample_share}%</td><td>${a.refusal_rate}%</td><td>${escapeHTML(a.leader)}</td><td>${a.leader_share}%</td></tr>`).join('');
  const deg = forecast.deg;
  $('#forecast-deg').textContent = `По данным ${deg.source} на ${deg.as_of}, явка в области ${deg.turnout}%, из них около ${Math.round(deg.share * 1000) / 10}% проголосовали дистанционно (ДЭГ). Наш опрос идёт только на участках, поэтому итог по области зависит от того, как голосуют ДЭГ-избиратели. Это допущения, а не прогноз: ДЭГ-долю голосов мы берём как у респондентов соответствующего возраста.`;
  $('#deg-head').innerHTML = `<tr><th>Партия</th><th>Участки (прогноз)</th>${deg.scenarios.map(sc => `<th>${escapeHTML(sc.title)}</th>`).join('')}</tr>`;
  $('#deg-body').innerHTML = rows.slice(0, 6).map(r => `<tr><td class="territory">${escapeHTML(r.label)}</td><td>${r.forecast}%</td>${deg.scenarios.map(sc => `<td>${sc.values[r.label]}%</td>`).join('')}</tr>`).join('');
}
function renderAgeHeatmap(heat) {
  const validOnly = $('#age-heat-valid').checked;
  const service = label => label === 'Испортил бюллетень' || label === 'Отказался отвечать';
  const partyRows = heat.rows.filter(row => !service(row.label));
  const columnTotals = heat.ages.map((age, i) => validOnly ? partyRows.reduce((sum, row) => sum + row.cells[i].count, 0) : age.total);
  const overall = validOnly ? partyRows.reduce((sum, row) => sum + row.total, 0) : heat.overall;
  const percent = (count, base) => base ? Math.round(count * 1000 / base) / 10 : 0;
  $('#ageheat-head').innerHTML = `<tr><th class="sticky-col">Ответ</th>${heat.ages.map((age, i) => `<th>${escapeHTML(age.age)}<small>n = ${number(columnTotals[i])}</small></th>`).join('')}<th>Все<small>n = ${number(overall)}</small></th></tr>`;
  const body = $('#ageheat-body');
  const rows = validOnly ? partyRows : heat.rows;
  body.innerHTML = rows.map(row => {
    const values = row.cells.map((cell, i) => percent(cell.count, columnTotals[i]));
    const top = Math.max(0, ...values);
    return `<tr class="${service(row.label) ? 'service-row' : ''}"><th>${escapeHTML(row.label)}</th>${values.map((value, i) => `<td class="heat" data-level="${top ? value / top : 0}" title="${number(row.cells[i].count)} анкет">${value}%</td>`).join('')}<td>${percent(row.total, overall)}%</td></tr>`;
  }).join('');
  body.querySelectorAll('tr').forEach(tr => {
    const rgb = tr.classList.contains('service-row') ? '217,120,98' : '70,99,77';
    tr.querySelectorAll('td.heat').forEach(cell => {
      const level = Number(cell.dataset.level);
      cell.style.background = level ? `rgba(${rgb}, ${0.08 + 0.72 * level})` : '';
      cell.classList.toggle('heat-dark', level > 0.6);
    });
  });
  $('#ageheat-note').textContent = validOnly ? 'Доля внутри возрастной группы среди ответивших партии (без отказов и испорченных бюллетеней).' : 'Доля внутри возрастной группы от всех её анкет, включая отказы. Яркость — относительно максимума в строке.';
}
function detailQuery(kind, key) {
  const query = new URLSearchParams({kind, key, focus: focusParty});
  if (kind === 'kpi') query.set('base', kpiBase);
  if (filters.day) query.set('day', filters.day);
  if (filters.okrug) query.set('okrug', filters.okrug);
  if (filters.tik) query.set('tik', filters.tik);
  if (filters.precinct) query.set('precinct', filters.precinct);
  return query;
}
function renderDetailTable(table) {
  const head = table.columns.map(c => `<th>${escapeHTML(c)}</th>`).join('');
  const rows = table.rows.map(row => `<tr>${row.map(cell => `<td class="${escapeHTML(cell.cls || '')}">${cell.bar != null ? `<div class="mini"><span>${escapeHTML(cell.t)}</span><i data-w="${cell.bar}"></i></div>` : escapeHTML(cell.t)}</td>`).join('')}</tr>`).join('');
  return `<div class="dtable-wrap"><h4>${escapeHTML(table.title)}</h4><div class="table-scroll"><table class="dtable"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>${table.note ? `<p class="panel-note">${escapeHTML(table.note)}</p>` : ''}</div>`;
}
function renderDetail(data) {
  const drawer = $('#detail-drawer'), scroll = drawer.scrollTop;
  $('#drawer-role').textContent = (data.role || 'Анализ графы').toUpperCase();
  document.querySelector('#detail-drawer .focus-select').hidden = data.kind === 'kpi';
  $('#drawer-title').textContent = data.title;
  $('#drawer-sub').textContent = `${data.subtitle} · пересчитывается вместе с данными`;
  const metrics = data.metrics.map(m => `<div class="detail-metric"><span>${escapeHTML(m.label)}</span><strong>${escapeHTML(m.value)}</strong></div>`).join('');
  const sections = data.sections.map(section => `<section class="dsection"><h3>${escapeHTML(section.title)}</h3>${section.findings.length ? `<ul class="findings">${section.findings.map(f => `<li class="lvl-${escapeHTML(f.level)}">${escapeHTML(f.text)}</li>`).join('')}</ul>` : ''}${section.tables.map(renderDetailTable).join('')}${section.note ? `<p class="panel-note">${escapeHTML(section.note)}</p>` : ''}</section>`).join('');
  $('#drawer-body').innerHTML = `<div class="headline ${escapeHTML(data.headline.level)}">${escapeHTML(data.headline.text)}</div><div class="detail-metrics">${metrics}</div>${sections}`;
  drawer.querySelectorAll('.dtable').forEach(table => {
    const columns = {};
    table.querySelectorAll('.mini i').forEach(bar => { const index = bar.closest('td').cellIndex; (columns[index] = columns[index] || []).push(bar); });
    Object.values(columns).forEach(bars => { const top = Math.max(1, ...bars.map(b => Number(b.dataset.w))); bars.forEach(b => { b.style.width = (Number(b.dataset.w) / top * 100) + '%'; }); });
  });
  drawer.scrollTop = scroll;
}
function openDrawer() {
  $('#detail-drawer').hidden = false;
  document.body.classList.add('drawer-open');
}
async function refreshDetail(loading) {
  if (!activeDetail) return;
  const {kind, key} = activeDetail;
  if (loading) { openDrawer(); $('#drawer-title').textContent = key; $('#drawer-body').innerHTML = '<p class="empty">Считаем…</p>'; }
  try {
    const data = await request('/api/dashboard/detail?' + detailQuery(kind, key));
    if (activeDetail && activeDetail.kind === kind && activeDetail.key === key) renderDetail(data);
  } catch (error) {
    if (loading) $('#drawer-body').innerHTML = `<p class="error">${escapeHTML(error.message)}</p>`;
  }
}
function focusOptions(parties) {
  const select = $('#focus-party');
  if (select.options.length) return;
  const names = parties.map(p => p.label).filter(label => label !== 'Испортил бюллетень' && label !== 'Отказался отвечать');
  if (!names.includes(focusParty)) focusParty = names.includes('Новые люди') ? 'Новые люди' : names[0];
  select.innerHTML = names.map(name => option(name, name, focusParty)).join('');
  $('#map-focus').innerHTML = select.innerHTML;
}
function markActiveBar() {
  document.querySelectorAll('[data-kind][data-key]').forEach(el => {
    el.classList.toggle('selected', Boolean(activeDetail) && el.dataset.kind === activeDetail.kind && el.dataset.key === activeDetail.key);
  });
}
function closeDetail() { activeDetail = null; $('#detail-drawer').hidden = true; document.body.classList.remove('drawer-open'); markActiveBar(); }
function renderSwing(swing) {
  const head = $('#swing-head'), body = $('#swing-body');
  if (!swing) { head.innerHTML = ''; body.innerHTML = ''; return; }
  head.innerHTML = `<tr><th class="sticky-col">Партия · итог 2021</th>${swing.okrugs.map(o => `<th>Округ ${escapeHTML(o.okrug)}<small>n = ${number(o.n)}</small></th>`).join('')}<th>Область<small>n = ${number(swing.region_n)}</small></th></tr>`;
  const cell = c => {
    const tip = c.y2021 === null ? 'в 2021 партии не было' : `2021: ${c.y2021}% · опрос: ${c.poll === null ? '—' : c.poll + '%'} (n = ${number(c.n)})`;
    if (c.delta === null) return `<td class="heat" title="${escapeHTML(tip)}">—</td>`;
    return `<td class="heat swing${c.significant ? ' sig' : ''}" data-delta="${c.delta}" title="${escapeHTML(tip)}">${fmtSigned(c.delta)}</td>`;
  };
  body.innerHTML = swing.rows.map(row => `<tr><th>${escapeHTML(row.label)}<small>2021: ${row.y2021 === null ? '—' : row.y2021 + '%'}</small></th>${row.cells.map(cell).join('')}${cell(row.region)}</tr>`).join('');
  body.querySelectorAll('td.swing').forEach(el => {
    const delta = Number(el.dataset.delta), level = Math.min(1, Math.abs(delta) / 12);
    el.style.background = `rgba(${delta >= 0 ? '70,99,77' : '217,120,98'}, ${0.08 + 0.72 * level})`;
    el.classList.toggle('heat-dark', level > 0.6);
  });
}
const MAP_MIN_N = 30;
const MAP_NO_DATA = '#e6ebe1';
function mixColor(from, to, t) { return `rgb(${from.map((c, i) => Math.round(c + (to[i] - c) * t)).join(',')})`; }
function mapUnits() {
  const named = counts => Object.entries(counts).filter(([label]) => label !== 'Испортил бюллетень' && label !== 'Отказался отвечать').reduce((sum, [, n]) => sum + n, 0);
  const groups = new Map();
  mapPayload.tiks.forEach(t => {
    const key = mapLevel === 'tik' ? t.tik : t.okrug;
    const unit = groups.get(key) || {key, label: mapLevel === 'tik' ? t.tik : `Округ ${t.okrug}`, okrug: t.okrug, n: 0, named: 0, votes: 0, refusals: 0, tiks: []};
    unit.n += t.n; unit.named += named(t.answers); unit.votes += t.answers[focusParty] || 0; unit.refusals += t.answers['Отказался отвечать'] || 0; unit.tiks.push(t.tik);
    groups.set(key, unit);
  });
  const all = [...groups.values()];
  const totalNamed = all.reduce((s, u) => s + u.named, 0), totalVotes = all.reduce((s, u) => s + u.votes, 0);
  const overall = totalNamed ? totalVotes / totalNamed : 0;
  all.forEach(u => {
    u.share = u.named ? u.votes * 100 / u.named : 0;
    u.index = overall && u.named ? u.votes / u.named / overall * 100 : 0;
    u.refusalRate = u.n ? u.refusals * 100 / u.n : 0;
    u.turnout = mapPayload.turnout_2021[u.okrug]?.turnout ?? null;
  });
  return {units: all, overallShare: overall * 100};
}
const MAP_LAYERS = {
  share: {value: u => u.named >= MAP_MIN_N ? u.share : null, format: v => `${v.toFixed(1)}%`, kind: 'seq', color: [70, 99, 77]},
  index: {value: u => u.named >= MAP_MIN_N ? u.index : null, format: v => v.toFixed(0), kind: 'div'},
  refusal: {value: u => u.n >= MAP_MIN_N ? u.refusalRate : null, format: v => `${v.toFixed(1)}%`, kind: 'seq', color: [217, 120, 98]},
  count: {value: u => u.n || null, format: v => number(v), kind: 'seq', color: [95, 134, 163]},
  turnout: {value: u => u.turnout, format: v => `${v.toFixed(1)}%`, kind: 'seq', color: [201, 162, 39]},
};
function projectMap() {
  const [minX, minY, maxX, maxY] = mapGeo.bbox;
  const k = Math.cos((minY + maxY) / 2 * Math.PI / 180), scale = 900 / ((maxX - minX) * k);
  return {width: 900, height: Math.round((maxY - minY) * scale), point: ([x, y]) => `${((x - minX) * k * scale).toFixed(1)},${((maxY - y) * scale).toFixed(1)}`, at: ([x, y]) => [(x - minX) * k * scale, (maxY - y) * scale]};
}
function buildMap() {
  const projection = projectMap();
  const paths = mapGeo.features.map(f => `<path data-tik="${escapeHTML(f.tik)}" data-okrug="${escapeHTML(f.okrug)}" tabindex="0" role="button" aria-label="${escapeHTML(f.tik)}" fill="${MAP_NO_DATA}" fill-rule="evenodd" d="${f.polygons.map(poly => poly.map(ring => 'M' + ring.map(projection.point).join('L') + 'Z').join('')).join('')}"/>`).join('');
  $('#map').innerHTML = `<svg viewBox="0 0 ${projection.width} ${projection.height}" role="img" aria-label="Картограмма Московской области по ТИК">${paths}<g id="map-labels"></g></svg>`;
  mapBuilt = true;
}
function paintMap() {
  if (!mapGeo || !mapPayload || !mapBuilt) return;
  const {units, overallShare} = mapUnits();
  const layer = MAP_LAYERS[mapLayer];
  const byKey = new Map(units.map(u => [u.key, u]));
  const values = units.map(layer.value).filter(v => v !== null);
  const low = Math.min(...values), high = Math.max(...values), deviation = Math.max(1, ...values.map(v => Math.abs(v - 100)));
  const color = value => {
    if (value === null) return MAP_NO_DATA;
    if (layer.kind === 'div') return mixColor([233, 239, 228], value >= 100 ? [70, 99, 77] : [217, 120, 98], Math.min(1, Math.abs(value - 100) / deviation));
    return mixColor([238, 243, 233], layer.color, high === low ? 0.6 : 0.12 + 0.88 * (value - low) / (high - low));
  };
  $('#map svg').querySelectorAll('path[data-tik]').forEach(el => {
    const unit = byKey.get(mapLevel === 'tik' ? el.dataset.tik : el.dataset.okrug);
    el.setAttribute('fill', unit ? color(layer.value(unit)) : MAP_NO_DATA);
  });
  const projection = projectMap(), labels = [];
  if (mapLevel === 'okrug') {
    const byOkrug = {};
    mapGeo.features.forEach(f => { (byOkrug[f.okrug] = byOkrug[f.okrug] || []).push(projection.at(f.label)); });
    Object.entries(byOkrug).forEach(([okrug, points]) => {
      const x = points.reduce((s, p) => s + p[0], 0) / points.length, y = points.reduce((s, p) => s + p[1], 0) / points.length;
      labels.push(`<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle" class="map-label">${escapeHTML(okrug)}</text>`);
    });
  }
  $('#map-labels').innerHTML = labels.join('');
  const ramp = layer.kind === 'div' ? `linear-gradient(90deg, rgb(217,120,98), rgb(233,239,228), rgb(70,99,77))` : `linear-gradient(90deg, rgb(238,243,233), rgb(${layer.color.join(',')}))`;
  const legend = $('#map-legend');
  legend.innerHTML = `<span>${values.length ? layer.format(layer.kind === 'div' ? 100 - deviation : low) : '—'}</span><i></i><span>${values.length ? layer.format(layer.kind === 'div' ? 100 + deviation : high) : '—'}</span><em><b></b> нет данных</em>`;
  legend.querySelector('i').style.background = ramp;
  legend.querySelector('em b').style.background = MAP_NO_DATA;
  $('#map-panel .panel-note.table-note').dataset.overall = overallShare.toFixed(1);
}
function mapTip(event) {
  const path = event.target.closest?.('path[data-tik]'), tip = $('#map-tip');
  if (!path || !mapPayload) { tip.hidden = true; return; }
  const {units, overallShare} = mapUnits();
  const unit = units.find(u => u.key === (mapLevel === 'tik' ? path.dataset.tik : path.dataset.okrug));
  if (!unit) { tip.hidden = true; return; }
  const small = unit.named < MAP_MIN_N;
  tip.innerHTML = `<strong>${escapeHTML(mapLevel === 'tik' ? path.dataset.tik : unit.label)}</strong>${mapLevel === 'tik' ? `<span>Округ ${escapeHTML(unit.okrug)}</span>` : ''}<ul><li>Анкет: ${number(unit.n)} · назвали партию: ${number(unit.named)}</li><li>«${escapeHTML(focusParty)}»: ${small ? 'мало данных' : `${unit.share.toFixed(1)}% (индекс ${unit.index.toFixed(0)}, в области ${overallShare.toFixed(1)}%)`}</li><li>Отказы: ${unit.n >= MAP_MIN_N ? unit.refusalRate.toFixed(1) + '%' : 'мало данных'}</li>${unit.turnout !== null ? `<li>Явка 2021 (округ): ${unit.turnout}%</li>` : ''}</ul>`;
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  tip.style.left = `${Math.min(window.innerWidth - box.width - 12, event.clientX + 16)}px`;
  tip.style.top = `${Math.min(window.innerHeight - box.height - 12, event.clientY + 16)}px`;
}
async function initMap() {
  if (mapGeo !== null) return;
  try { mapGeo = await request('/static/tik_map.json'); } catch { mapGeo = false; }
  if (mapGeo) { buildMap(); paintMap(); } else $('#map').innerHTML = '<p class="empty">Границы территорий недоступны</p>';
}
function renderMap(payload) {
  mapPayload = payload;
  if (mapGeo === null) void initMap(); else paintMap();
}
function renderSummary(summary, comparisons) {
  lastSummary = summary; lastCompare = comparisons;
  const compare = comparisons && comparisons[kpiBase];
  const cards = [
    ['total','Всего анкет',summary.total,'За выбранный период','accent'],
    ['refusals','Отказались',summary.refusals,summary.total ? `${Math.round(summary.refusals*100/summary.total)}% от анкет` : 'Нет данных','tone-red'],
    ['spoiled','Испортили бюллетень',summary.spoiled,'Отдельный вариант ответа','tone-yellow'],
    ['interviewers','Интервьюеров',summary.interviewers,'Уникальных людей','tone-sage'],
    ['uiks','УИК с данными',summary.uiks,'Охвачено участков','tone-blue'],
    ['tiks','ТИК с данными',summary.tiks,'Охвачено территорий','tone-sand'],
  ];
  const deltaLine = key => {
    const item = compare && compare.values[key];
    if (!compare) return '';
    const target = kpiBase === 'avg2' ? 'среднему за 2 дня' : 'вчера';
    if (!compare.has_previous || !item || item.before === null) return kpiBase === 'avg2' ? 'нет данных за прошлые дни' : 'вчера данных нет';
    const diff = item.today - item.before, arrow = diff > 0 ? '▲' : diff < 0 ? '▼' : '=';
    const pct = item.change === null ? '' : ` ${Math.abs(item.change)}%`;
    const partial = kpiBase === 'avg2' && compare.days === 1 ? ' (1 день)' : '';
    return `${arrow}${pct} к ${target}${partial}${compare.cutoff ? ' ' + compare.cutoff : ''}`;
  };
  $('#summary').innerHTML = cards.map(([key,label,value,note,tone]) => `<article class="metric ${tone}" data-kind="kpi" data-key="${key}" role="button" tabindex="0" title="Сравнить с прошлым днём"><span>${escapeHTML(label)}</span><strong>${number(value)}</strong><small>${escapeHTML(note)}</small><em class="delta">${escapeHTML(deltaLine(key))}</em></article>`).join('');
}
function renderHours(items) {
  const max = Math.max(1, ...items.map(x => x.count));
  const box = $('#hours-chart');
  box.innerHTML = items.map(item => `<div class="hour"${item.count ? ` data-kind="hour" data-key="${escapeHTML(item.hour.slice(0,2))}" role="button" tabindex="0"` : ''}><b>${item.count || ''}</b><i></i><span>${escapeHTML(item.hour.slice(0,2))}</span></div>`).join('');
  box.querySelectorAll('.hour i').forEach((el, index) => { el.style.height = Math.max(3, items[index].count / max * 145) + 'px'; el.style.background = scaleColor(items[index].count, items.map(x => x.count)); });
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
  renderFilters(data); renderSummary(data.summary, data.compare); lastParties = data.parties; lastPartyCompare = data.party_compare; lastPartySelected = data.selected_day; renderPartyChart();
  renderColumns('#gender-chart',data.genders,{compact:true,kind:'gender',colors:GENDER_COLORS}); renderColumns('#age-chart',data.ages,{compact:true,kind:'age'}); renderHours(data.hours);
  renderColumns('#newpeople-chart',data.new_people_by_okrug,{kind:'okrug',keyOf:item => item.label.split(' ').pop()}); renderNewPeopleAge(data.new_people_by_age); renderHeatmap(data.party_okrug); lastAgeHeat = data.party_age; renderAgeHeatmap(lastAgeHeat); renderSwing(data.swing); renderMap(data.map); lastForecastData = data; renderForecastMode();
  renderGeo(data); renderInterviewers(data.interviewers, data.summary.total); renderAnomalies(data.anomalies); renderRecent(data.recent);
  const scopeLabel = filters.tik || (filters.okrug ? 'Округ ' + filters.okrug : '');
  $('#period-label').textContent = `${dateLabel(data.selected_day)}${scopeLabel ? ' · ' + scopeLabel : ''}`;
  focusOptions(data.parties); markActiveBar(); void refreshDetail();
  $('#updated-at').textContent = 'Обновлено в ' + new Date(data.generated_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
let rosterOpen = false;
const ROSTER_STATUS = {absent: ['Нет сегодня', 'roster-absent'], new: ['Новый сегодня', 'roster-new'], both: ['Вышел', 'roster-both']};
const ROSTER_ACTIVITY = {active: ['Работает', 'roster-both'], pause: ['Пауза', 'roster-pause'], silent: ['Молчит', 'roster-absent'], done: ['Смена окончена', 'roster-new']};
function agoLabel(minutes) {
  if (minutes === null) return '—';
  if (minutes < 1) return 'только что';
  return minutes < 60 ? `${minutes} мин назад` : `${Math.floor(minutes / 60)} ч ${minutes % 60} мин назад`;
}
function renderRoster(data) {
  const totals = data.totals, view = $('#roster-view').value;
  $('#roster-sub').textContent = `Вчера ${dateLabel(data.yesterday)} · сегодня ${dateLabel(data.today)}`;
  $('#roster-summary').innerHTML = [['Вчера работали', totals.yesterday, ''], ['Сегодня вышли', totals.today, ''], ['Нет сегодня', totals.absent, 'warn'],
    ['Работают сейчас', totals.active, ''], ['Пауза до часа', totals.pause, ''], ['Молчат больше часа', totals.silent, 'warn']]
    .map(([label, value, tone]) => `<article class="metric ${tone}"><span>${label}</span><strong>${number(value)}</strong></article>`).join('');
  const keep = {absent: p => p.status === 'absent', silent: p => p.activity === 'silent', today: p => p.status !== 'absent', all: () => true}[view];
  $('#roster-okrugs').innerHTML = data.okrugs.map(item => {
    const people = item.people.filter(keep);
    if (!item.people.length) return '';
    const rows = people.map(p => {
      const [label, tone] = p.activity ? ROSTER_ACTIVITY[p.activity] : ROSTER_STATUS[p.status];
      const note = p.status === 'absent' && p.replaced_by.length ? `Замена: ${escapeHTML(p.replaced_by.join(', '))}` : p.status === 'new' ? 'новый сегодня' : '';
      const last = p.last_today ? `${p.last_today} · ${agoLabel(p.minutes_since)}` : '—';
      return `<tr><td>${escapeHTML(p.name)}</td><td>${escapeHTML(p.tik)}</td><td>${escapeHTML(p.precinct)}</td><td class="num">${number(p.yesterday)}</td><td class="num">${number(p.today)}</td><td>${p.last_yesterday || '—'}</td><td>${last}</td><td><span class="roster-chip ${tone}">${label}</span> ${note}</td></tr>`;
    }).join('');
    return `<details class="roster-okrug" ${item.absent || item.silent ? 'open' : ''}><summary><strong>Округ ${escapeHTML(item.okrug)}</strong><span>вчера ${item.yesterday} · сегодня ${item.today} · нет сегодня <b>${item.absent}</b> · молчат <b>${item.silent}</b> · пауза ${item.pause}</span></summary>${people.length ? `<div class="table-scroll"><table><thead><tr><th>Интервьюер</th><th>ТИК</th><th>УИК</th><th>Вчера</th><th>Сегодня</th><th>Вчера был в</th><th>Последняя анкета сегодня</th><th>Статус</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="panel-note">В этом фильтре никого нет.</p>'}</details>`;
  }).join('');
}
function lockRoster(message = '') {
  rosterOpen = false; rosterData = null;
  $('#roster-body').hidden = true; $('#roster-login').hidden = false; $('#roster-lock').hidden = true;
  $('#roster-sub').textContent = 'Раздел закрыт отдельным паролем'; $('#roster-error').textContent = message;
}
let rosterData = null;
async function loadRoster() {
  if (!rosterOpen) return;
  try {
    rosterData = await request('/api/dashboard/roster');
    $('#roster-login').hidden = true; $('#roster-body').hidden = false; $('#roster-lock').hidden = false; renderRoster(rosterData);
  } catch (error) {
    if (error.status === 401) lockRoster(); else { $('#roster-sub').textContent = error.message; }
  }
}
async function load() {
  clearTimeout(timer); $('#refresh').disabled = true; status('', 'Обновляем данные'); $('#data-error').hidden = true;
  const query = new URLSearchParams();
  if (filters.day) query.set('day',filters.day); if (filters.okrug) query.set('okrug',filters.okrug);
  if (filters.tik) query.set('tik',filters.tik); if (filters.precinct) query.set('precinct',filters.precinct);
  try {
    const data = await request('/api/dashboard/data?' + query); render(data); showDashboard();
    const stale = data.data_age !== null && data.data_age > 180;
    status(stale ? 'error' : 'ready', stale ? 'Данные устарели' : 'Данные актуальны');
    if (stale) { $('#data-error').textContent = `Google Таблица не читается уже ${Math.round(data.data_age / 60)} мин: показаны последние полученные данные.`; $('#data-error').hidden = false; }
  } catch (error) {
    if (error.status === 401) return showLogin('Сессия завершилась. Введите код ещё раз.');
    $('#data-error').textContent = error.message; $('#data-error').hidden = false; status('error','Ошибка обновления');
  } finally { $('#refresh').disabled = false; timer = setTimeout(load,30000); void loadRoster(); }
}
$('#login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#login-error').textContent = '';
  try { await request('/api/dashboard/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:$('#code').value})}); $('#code').value=''; await load(); }
  catch(error) { $('#login-error').textContent = error.message; }
});
async function openRoster() {
  $('#roster-error').textContent = '';
  try {
    await request('/api/dashboard/roster/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code:$('#roster-code').value})});
    $('#roster-code').value = ''; rosterOpen = true; await loadRoster();
  } catch (error) { $('#roster-error').textContent = error.message; }
}
$('#roster-open').addEventListener('click', openRoster);
$('#roster-code').addEventListener('keydown', event => { if (event.key === 'Enter') void openRoster(); });
$('#roster-lock').addEventListener('click', () => lockRoster());
$('#roster-view').addEventListener('change', () => { if (rosterData) renderRoster(rosterData); });
document.querySelectorAll('[data-base]').forEach(button => button.addEventListener('click', () => {
  kpiBase = button.dataset.base;
  try { localStorage.setItem('kpiBase', kpiBase); } catch { /* storage may be blocked */ }
  document.querySelectorAll('[data-base]').forEach(other => other.classList.toggle('active', other === button));
  if (lastSummary) renderSummary(lastSummary, lastCompare);
  if (activeDetail && activeDetail.kind === 'kpi') void refreshDetail(false);
}));
document.querySelectorAll('[data-base]').forEach(button => button.classList.toggle('active', button.dataset.base === kpiBase));
document.querySelectorAll('[data-fmode]').forEach(button => button.addEventListener('click', () => {
  forecastMode = button.dataset.fmode;
  try { localStorage.setItem('forecastMode', forecastMode); } catch { /* storage may be blocked */ }
  renderForecastMode();
}));
$('#party-compare').addEventListener('change', event => {
  partyCompareOn = event.target.checked;
  try { localStorage.setItem('partyCompare', partyCompareOn ? 'on' : 'off'); } catch { /* storage may be blocked */ }
  renderPartyChart();
});
$('#party-forecast').addEventListener('change', event => {
  partyForecastOn = event.target.checked;
  try { localStorage.setItem('partyForecast', partyForecastOn ? 'on' : 'off'); } catch { /* storage may be blocked */ }
  renderPartyChart();
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
document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(other => { other.classList.toggle('active', other === tab); other.setAttribute('aria-selected', String(other === tab)); });
  $('#tab-answers').hidden = tab.dataset.tab !== 'answers';
  $('#tab-forecast').hidden = tab.dataset.tab !== 'forecast';
  if (tab.dataset.tab === 'answers') renderPartyChart();  // bars are sized from the visible height
}));
$('#age-heat-valid').addEventListener('change', () => { if (lastAgeHeat) renderAgeHeatmap(lastAgeHeat); });
document.addEventListener('click', event => {
  const bar = event.target.closest('[data-kind][data-key]');
  if (bar) {
    const same = activeDetail && activeDetail.kind === bar.dataset.kind && activeDetail.key === bar.dataset.key;
    if (same) { closeDetail(); return; }
    activeDetail = {kind: bar.dataset.kind, key: bar.dataset.key};
    markActiveBar();
    void refreshDetail(true);
  } else if (event.target.closest('#detail-close')) closeDetail();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && activeDetail) closeDetail();
  else if ((event.key === 'Enter' || event.key === ' ') && event.target.matches?.('[data-kind][data-key]')) { event.preventDefault(); event.target.click(); }
});
$('#focus-party').addEventListener('change', event => {
  focusParty = event.target.value;
  $('#map-focus').value = focusParty; paintMap();
  try { localStorage.setItem('focusParty', focusParty); } catch { /* storage may be blocked */ }
  void refreshDetail(true);
});
document.querySelectorAll('.seg[data-layer], .seg[data-level]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.layer) mapLayer = button.dataset.layer; else mapLevel = button.dataset.level;
  document.querySelectorAll(`.seg[data-${button.dataset.layer ? 'layer' : 'level'}]`).forEach(other => other.classList.toggle('active', other === button));
  paintMap();
}));
$('#map-focus').addEventListener('change', event => {
  focusParty = event.target.value;
  try { localStorage.setItem('focusParty', focusParty); } catch { /* storage may be blocked */ }
  $('#focus-party').value = focusParty;
  paintMap();
  void refreshDetail(true);
});
$('#map').addEventListener('mousemove', mapTip);
$('#map').addEventListener('mouseleave', () => { $('#map-tip').hidden = true; });
$('#map').addEventListener('click', event => {
  const path = event.target.closest('path[data-tik]');
  if (!path) return;
  filters.okrug = path.dataset.okrug; filters.tik = mapLevel === 'tik' ? path.dataset.tik : ''; filters.precinct = '';
  void load();
});
$('#map').addEventListener('keydown', event => { if ((event.key === 'Enter' || event.key === ' ') && event.target.matches?.('path[data-tik]')) { event.preventDefault(); event.target.dispatchEvent(new MouseEvent('click', {bubbles: true})); } });
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
