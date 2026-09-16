'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let config, db, profile, draft, screen = 'profile', syncing = false, submitting = false, connected = false, authNeeded = false;
let installPrompt = null;
let offlineReadiness = {state:'checking', detail:'Проверяем сохранённые файлы приложения…'};
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('exit-poll') : null;

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installPrompt = event;
  renderReadiness();
});
window.addEventListener('appinstalled', () => {
  installPrompt = null;
  store('installed', true);
  renderReadiness();
});

function stored(key, fallback = null) {
  try { return JSON.parse(localStorage.getItem('ep-' + key)) ?? fallback; } catch { return fallback; }
}
function store(key, value) { localStorage.setItem('ep-' + key, JSON.stringify(value)); }
function today() {
  const parts = new Intl.DateTimeFormat('en-CA', {timeZone: config.timezone, year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
  const part = type => parts.find(x => x.type === type).value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}
function dateLabel() { return new Intl.DateTimeFormat('ru', {day:'numeric',month:'long',timeZone:config.timezone}).format(new Date()); }
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; }
function error(message) { const box = $('#error'); if (box) box.textContent = message; else toast(message); }
function friendlyError(e) { return e.message || 'Не удалось выполнить действие. Попробуйте ещё раз.'; }
function saveDraft() { store('draft', draft); }
function currentDay() {
  if (profile?.day === today()) return true;
  profile = null; draft = null; store('profile', null); saveDraft(); screen = 'profile'; render();
  toast('Начался новый день. Укажите имя, ТИК и УИК для новой смены.');
  return false;
}
async function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('exit-poll', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('surveys', {keyPath:'id'});
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
function database(mode, fn) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction('surveys', mode);
    const request = fn(tx.objectStore('surveys'));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('Не удалось сохранить анкету на устройстве'));
  });
}
const allSurveys = () => database('readonly', s => s.getAll());
const putSurvey = item => database('readwrite', s => s.put(item));
async function api(url, body, timeout = 7000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {method:body === undefined ? 'GET' : 'POST', credentials:'same-origin', cache:'no-store', signal:controller.signal,
      headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    if (!response.ok) {
      let detail; try { detail = (await response.json()).detail; } catch { /* Show generic error below. */ }
      const e = new Error(typeof detail === 'string' ? detail : 'Сервер отклонил анкету. Проверьте данные или обратитесь к координатору.');
      e.status = response.status; throw e;
    }
    return await response.json();
  } finally { clearTimeout(timer); }
}
function connection(ok, slow = false) {
  connected = ok;
  $('#connection').textContent = !ok ? 'Нет связи · офлайн' : slow ? 'Слабая связь' : 'На связи';
  $('#connection').className = 'connection' + (!ok || slow ? ' offline' : '');
  const notice = $('#notice');
  notice.hidden = ok && !slow;
  if (!notice.hidden) notice.innerHTML = `<strong>${ok ? 'Слабая связь' : 'Нет подключения'}</strong><br>Анкеты сохраняются на телефоне. Их можно отправить по SMS или автоматически после восстановления связи.<button data-action="sms">Открыть очередь и SMS</button>`;
}
async function syncUnlocked() {
  if (syncing) return;
  syncing = true;
  try {
    const start = performance.now();
    await api('/api/health');
    connection(true, performance.now() - start > 3000);
    const rows = await allSurveys();
    for (const item of rows.filter(x => !x.received && !x.rejected)) {
      const {received, rejected, problem, ...body} = item;
      try {
        await api('/api/surveys', body);
        await putSurvey({...body, received:true});
      } catch (e) {
        if (e.status === 401) { authNeeded = true; break; }
        if ([409,422].includes(e.status)) { await putSurvey({...body, received:false,rejected:true,problem:e.message}); continue; }
        throw e;
      }
    }
  } catch (e) {
    connection(false);
  } finally {
    syncing = false;
    if (screen === 'home') await updateStats();
    channel?.postMessage('refresh');
  }
}
async function sync() {
  if (navigator.locks) return navigator.locks.request('ep-sync', {ifAvailable:true}, lock => lock ? syncUnlocked() : undefined);
  return syncUnlocked();
}
async function updateStats() {
  if (screen !== 'home') return;
  const rows = await allSurveys();
  const mine = rows.filter(x => x.profile.day === today() && x.profile.name === profile.name && x.profile.surname === profile.surname);
  const refused = mine.filter(x => x.party === 'refused').length;
  if (!$('#completed')) return;
  $('#completed').textContent = mine.length - refused;
  $('#refused').textContent = refused;
  const pending = rows.filter(x => !x.received && !x.rejected).length;
  const rejected = rows.filter(x => x.rejected).length;
  $('#sync-status').textContent = authNeeded ? 'Для отправки нужен код доступа' : rejected ? `Требуют внимания: ${rejected}. В очереди: ${pending}` : pending ? `Ожидают отправки: ${pending}` : 'Все анкеты переданы на сервер';
  $('#auth-link').hidden = !authNeeded;
  $('#export-link').hidden = !rejected && !pending;
  await renderPendingSurveys('offline-queue', rows.filter(x => !x.received && !x.rejected));
  renderReadiness();
}
function steps(n) { return `<div class="steps" aria-label="Шаг ${n} из 3">${[1,2,3].map(i=>`<span class="${i<=n?'done':''}"></span>`).join('')}</div><div class="section-kicker">АНКЕТА · ШАГ ${n} ИЗ 3</div>`; }
function card(content) { return `<section class="card">${content}<p id="error" class="error" role="alert"></p></section>`; }
const back = (target, text='Назад') => `<button class="back" data-action="${target}">← ${text}</button>`;
const precinctLabel = () => {
  const precinct = config.precincts.find(p => p.id === profile?.precinct);
  return precinct ? [precinct.tik, precinct.label].filter(Boolean).join(' · ') : profile?.precinct;
};
const tikList = () => [...new Set(config.precincts.map(p => p.tik).filter(Boolean))].sort((a,b) => a.localeCompare(b,'ru'));
function refreshPrecincts() {
  const tik = $('#tik').value, query = $('#precinct-search').value;
  const select = $('#precinct'), selected = select.value;
  select.disabled = !tik;
  $('#precinct-search').disabled = !tik;
  const options = precinctOptions(query, tik);
  select.innerHTML = `<option value="">${!tik ? 'Сначала выберите ТИК' : !options ? 'УИК не найден' : 'Выберите УИК'}</option>` + options;
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}

function standaloneMode() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}
function renderReadiness() {
  const box = $('#offline-readiness');
  if (!box) return;
  const ready = offlineReadiness.state === 'ready';
  const checking = offlineReadiness.state === 'checking';
  const installed = standaloneMode() || stored('installed', false);
  const installControl = installPrompt
    ? '<button class="secondary compact" data-action="install">Добавить на главный экран</button>'
    : (!installed && /iPhone|iPad|iPod/.test(navigator.userAgent)
      ? '<p class="hint install-hint">На iPhone нажмите «Поделиться» → «На экран Домой».</p>'
      : '');
  box.className = `readiness ${ready ? 'ready' : checking ? 'checking' : 'not-ready'}`;
  box.innerHTML = `<div class="readiness-title"><span>${ready ? '✓' : checking ? '…' : '!'}</span><strong>${ready ? 'Устройство готово к офлайн-работе' : checking ? 'Проверяем офлайн-режим' : 'Офлайн-режим ещё не готов'}</strong></div><p>${escapeHTML(offlineReadiness.detail)}</p>${ready ? '<button class="text-button" data-action="check-offline">Проверить снова</button>' : '<button class="secondary compact" data-action="check-offline">Подготовить и проверить</button>'}${installControl}`;
}
async function checkOfflineReadiness() {
  offlineReadiness = {state:'checking', detail:'Сохраняем интерфейс и справочники на телефоне…'};
  renderReadiness();
  try {
    if (!('serviceWorker' in navigator)) throw new Error('Этот браузер не поддерживает офлайн-приложения.');
    if (!window.isSecureContext && location.hostname !== '127.0.0.1' && location.hostname !== 'localhost') throw new Error('Для офлайн-режима откройте приложение по защищённой HTTPS-ссылке.');
    if (!stored('config')) throw new Error('Сначала дождитесь загрузки ТИК и УИК при подключённом интернете.');
    const registration = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    const worker = registration.active || registration.waiting || registration.installing;
    if (!worker) throw new Error('Офлайн-модуль ещё устанавливается. Нажмите «Проверить снова» через несколько секунд.');
    const result = await Promise.race([
      new Promise((resolve, reject) => {
        const message = new MessageChannel();
        message.port1.onmessage = event => resolve(event.data);
        message.port1.onmessageerror = () => reject(new Error('Не удалось проверить локальную копию.'));
        worker.postMessage({type:'CHECK_OFFLINE_READY'}, [message.port2]);
      }),
      new Promise((_, reject) => setTimeout(() => reject(new Error('Проверка заняла слишком много времени. Обновите страницу при интернете.')), 8000))
    ]);
    if (!result?.ready) throw new Error('Не все файлы сохранились. Оставьте приложение открытым при интернете и повторите проверку.');
    if (navigator.storage?.persist) void navigator.storage.persist();
    store('offline-ready', {version:result.version, checked_at:new Date().toISOString()});
    offlineReadiness = {state:'ready', detail:'Форма, оформление и список участков сохранены на этом телефоне. Можно отключить интернет и продолжить опрос.'};
  } catch (e) {
    offlineReadiness = {state:'not-ready', detail:friendlyError(e)};
  }
  renderReadiness();
}
async function installApplication() {
  if (!installPrompt) return;
  const prompt = installPrompt;
  installPrompt = null;
  await prompt.prompt();
  await prompt.userChoice;
  renderReadiness();
}
function compactTimestamp(value) {
  const parts = new Intl.DateTimeFormat('en-GB', {timeZone:config.timezone, year:'2-digit',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).formatToParts(new Date(value));
  const part = type => parts.find(x => x.type === type)?.value || '00';
  return `${part('year')}${part('month')}${part('day')}${part('hour')}${part('minute')}`;
}
function smsCode(item) {
  const uik = item.profile.precinct.match(/(\d+)(?!.*\d)/)?.[1] || item.profile.precinct.replace(/\s+/g, '');
  const party = item.party === 'spoiled' ? '12' : item.party === 'refused' ? '13' : item.party;
  const gender = item.gender === 'male' ? '1' : '2';
  const age = String(config.ages.indexOf(item.age) + 1);
  const surveyId = item.id.replace(/-/g, '');
  const shiftId = item.profile.id.replace(/-/g, '').slice(0, 8);
  return `EP1 ${uik} ${party} ${gender} ${age} ${compactTimestamp(item.created_at)} ${surveyId} ${shiftId}`;
}
async function copyText(value) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const field = document.createElement('textarea');
  field.value = value; field.style.position = 'fixed'; field.style.opacity = '0';
  document.body.append(field); field.select(); document.execCommand('copy'); field.remove();
}
async function copySurveySms(id) {
  const item = (await allSurveys()).find(row => row.id === id);
  if (!item) throw new Error('Анкета не найдена на устройстве.');
  await copyText(smsCode(item));
  toast('SMS-код анкеты скопирован. Вставьте его в сообщение координатору.');
}
async function openSurveySms(id) {
  const item = (await allSurveys()).find(row => row.id === id);
  if (!item) throw new Error('Анкета не найдена на устройстве.');
  const code = smsCode(item);
  const number = String(config.sms_request_number || '').trim();
  if (!/^\+[1-9]\d{7,14}$/.test(number)) {
    await copyText(code);
    toast('Служебный SMS-номер ещё не настроен. Код скопирован — отправьте его координатору вручную.');
    return;
  }
  const separator = /iPhone|iPad|iPod/.test(navigator.userAgent) ? '&body=' : '?body=';
  window.location.href = `sms:${number}${separator}${encodeURIComponent(code)}`;
}
async function renderPendingSurveys(containerId, providedRows) {
  const box = $('#' + containerId);
  if (!box) return;
  const rows = providedRows || (await allSurveys()).filter(item => !item.received && !item.rejected);
  if (!rows.length) {
    if (containerId === 'offline-queue') { box.hidden = true; box.innerHTML = ''; }
    else box.innerHTML = '<div class="status-note success-note">Все сохранённые анкеты отправлены на сервер.</div>';
    return;
  }
  box.hidden = false;
  const hasNumber = /^\+[1-9]\d{7,14}$/.test(String(config.sms_request_number || '').trim());
  const items = rows.sort((a,b) => new Date(a.created_at) - new Date(b.created_at)).map(item => {
    const answer = config.parties.find(p => p.id === item.party)?.label || item.party;
    const time = new Intl.DateTimeFormat('ru', {timeZone:config.timezone,hour:'2-digit',minute:'2-digit'}).format(new Date(item.created_at));
    return `<div class="queue-item"><div><strong>${escapeHTML(time)} · ${escapeHTML(answer)}</strong><small>${escapeHTML(item.profile.tik || '')} · ${escapeHTML(item.profile.precinct.replace('mo-uik-', 'УИК № '))}</small></div><div class="queue-actions"><button class="secondary compact" data-sms-id="${escapeHTML(item.id)}">${hasNumber ? 'Отправить через SMS' : 'Скопировать SMS-код'}</button><button class="text-button" data-copy-sms-id="${escapeHTML(item.id)}">Копировать</button></div></div>`;
  }).join('');
  box.innerHTML = `<div class="queue-head"><div><div class="section-kicker">${connected ? 'ОЧЕРЕДЬ ОТПРАВКИ' : 'НЕТ ПОДКЛЮЧЕНИЯ'}</div><h3>${rows.length} ${rows.length === 1 ? 'анкета сохранена' : 'анкеты сохранены'} на устройстве</h3></div><span class="queue-count">${rows.length}</span></div><p>${connected ? 'Идёт повторная отправка на сервер.' : 'Отправьте каждую анкету по SMS или дождитесь интернета — приложение повторит отправку автоматически.'}</p>${items}<button class="secondary compact retry-button" data-action="sync">Попробовать снова</button>`;
}

function render() {
  $('#toast').hidden = true;
  if (screen === 'profile') {
    const previous = stored('previous', {});
    $('#app').innerHTML = card(`<div class="section-kicker">НАЧАЛО РАБОЧЕГО ДНЯ</div><div class="title-row"><h2>Ваша смена</h2><span class="tag">${escapeHTML(dateLabel())}</span></div><p class="muted">Представьтесь, выберите ТИК и свой УИК.<br>Это нужно сделать один раз в день.</p>
      <form id="profile-form"><div class="two-col"><div><label class="field" for="surname">Фамилия</label><input id="surname" name="surname" autocomplete="family-name" placeholder="Иванов" maxlength="80" required value="${escapeHTML(previous.surname || '')}"></div><div><label class="field" for="name">Имя</label><input id="name" name="name" autocomplete="given-name" placeholder="Иван" maxlength="80" required value="${escapeHTML(previous.name || '')}"></div></div>
      <label class="field" for="tik">Ваш ТИК</label><select name="tik" id="tik" required><option value="">Выберите территориальную комиссию</option>${tikList().map(tik=>`<option value="${escapeHTML(tik)}">${escapeHTML(tik)}</option>`).join('')}</select>
      <label class="field" for="precinct-search">Найти УИК по номеру</label><input id="precinct-search" type="search" inputmode="numeric" placeholder="Например, 1259" autocomplete="off" disabled><label class="field" for="precinct">Ваш УИК</label><select name="precinct" id="precinct" required disabled><option value="">Сначала выберите ТИК</option></select><p class="hint">Показаны только УИК выбранной территориальной комиссии.</p>
      
      <button class="primary action" type="submit">Сохранить и начать <span>→</span></button><p class="hint">Имя относится к интервьюеру. Личные данные респондента не запрашиваются.</p></form><hr class="divider"><div id="offline-readiness"></div>`);
    renderReadiness();
  } else if (screen === 'login') {
    $('#app').innerHTML = card(`<div class="section-kicker">ДОСТУП К ИССЛЕДОВАНИЮ</div><h2>Код вашей команды</h2><p class="muted">Код выдаёт координатор. Он защищает сбор анкет от посторонних отправок.</p><form id="login-form"><label for="code" class="field">Код доступа</label><input id="code" name="code" type="password" autocomplete="current-password" required><button class="primary action">Продолжить →</button></form>${profile?'<button class="text-button" data-action="home">Продолжить сбор офлайн</button>':''}`);
  } else if (screen === 'home') {
    const initials = (profile.name[0] + profile.surname[0]).toUpperCase();
    $('#app').innerHTML = card(`<div class="shift"><div class="badge">${escapeHTML(initials)}</div><span class="tag">${escapeHTML(dateLabel())}</span></div><div class="section-kicker">СМЕНА ОТКРЫТА</div><h2>${escapeHTML(profile.name)}, вы на месте.</h2><p class="summary-line">${escapeHTML(profile.surname)} ${escapeHTML(profile.name)}<br>${escapeHTML(precinctLabel())}</p>
      <div class="stats"><div class="stat"><strong id="completed">—</strong><span>анкет сегодня</span></div><div class="stat"><strong id="refused">—</strong><span>отказов сегодня</span></div></div><button class="primary" data-action="new">＋ Новая анкета</button>
      <div class="sync-row"><span id="sync-status">Проверяем отправку…</span><button class="text-button" data-action="sync">Обновить</button></div><button id="auth-link" class="secondary" data-action="login" hidden>Ввести код доступа</button><button id="export-link" class="text-button" data-action="export" hidden>Скачать резервную копию</button><div id="offline-queue" class="offline-queue" hidden></div>
      ${!config.sheets_configured?'<div class="status-note">Google Таблицы ещё не подключены. Анкеты будут сохранены на сервере до подключения.</div>':'<p class="hint">Сервер передаёт анкеты в Google Таблицы отдельной очередью. При сбое передача повторяется автоматически.</p>'}
      <hr class="divider"><div id="offline-readiness"></div><hr class="divider"><div class="title-row"><button class="text-button" data-action="sms">Офлайн и SMS</button><button class="text-button" data-action="change">Изменить данные смены</button></div><p class="hint">Счётчики учитывают анкеты этого интервьюера в этом браузере за сегодня, включая ещё не отправленные.</p>`);
    renderReadiness();
    updateStats().catch(e=>error(friendlyError(e)));
  } else if (screen === 'party') {
    $('#app').innerHTML = card(`${back('home','К смене')}${steps(1)}<h2>Выбор респондента</h2><p class="script">Добрый день, я провожу анонимный опрос сразу после голосования. Подскажите, пожалуйста, за какую партию вы только что проголосовали?</p><div class="parties">${config.parties.map(p=>`<button class="option ${p.id==='refused'?'refusal':p.id==='spoiled'?'spoiled':''}" data-party="${escapeHTML(p.id)}" ${p.disabled?'disabled aria-disabled="true"':''}>${/^\d+$/.test(p.id)?`<span class="num">${p.id}</span>`:''}<span>${escapeHTML(p.label)}</span>${p.disabled?'<small>недоступно</small>':''}</button>`).join('')}</div>`);
  } else if (screen === 'demographics') {
    $('#app').innerHTML = card(`${back('party')}${steps(2)}<h2>О респонденте</h2><p class="muted">Укажите пол и возрастную группу.</p><fieldset class="choice-group"><legend>Пол респондента</legend><div class="choices">${[['male','Мужской'],['female','Женский']].map(([id,label])=>`<button class="choice" data-gender="${id}" aria-pressed="${draft.gender===id}">${label}</button>`).join('')}</div></fieldset><fieldset class="choice-group"><legend>Возраст респондента</legend><div class="choices ages">${config.ages.map(age=>`<button class="choice" data-age="${escapeHTML(age)}" aria-pressed="${draft.age===age}">${escapeHTML(age)}</button>`).join('')}</div></fieldset><button class="primary action" data-action="review" ${!draft.gender||!draft.age?'disabled':''}>Далее <span>→</span></button>`);
  } else if (screen === 'review') {
    $('#app').innerHTML = card(`${back(draft.party==='refused'&&!config.refusal_demographics?'party':'demographics')}${steps(3)}<h2>Всё верно?</h2><p class="muted">Проверьте ответы перед отправкой.</p><dl class="review"><div><dt>Партия / ответ</dt><dd class="${draft.party==='refused'?'red':''}">${escapeHTML(config.parties.find(p=>p.id===draft.party).label)}</dd></div><div><dt>Пол респондента</dt><dd>${draft.gender==='male'?'Мужской':draft.gender==='female'?'Женский':'Не указан'}</dd></div><div><dt>Возраст респондента</dt><dd>${escapeHTML(draft.age||'Не указан')}</dd></div></dl><p class="hint">${escapeHTML(precinctLabel())}</p><button class="primary action" data-action="submit">Отправить анкету <span>✓</span></button><p class="hint">При отсутствии связи анкета сохранится на телефоне и будет отправлена при следующем подключении.</p>`);
  } else if (screen === 'sms') {
    const number = String(config.sms_request_number || '').trim();
    $('#app').innerHTML = card(`${back(profile?'home':'profile')}<div class="section-kicker">АВАРИЙНАЯ ОТПРАВКА</div><h2>Офлайн и SMS</h2><p class="muted">Анкеты хранятся на этом телефоне. Когда интернет вернётся, приложение отправит их на сервер автоматически.</p><div class="status-note">Если мобильная сеть работает, кнопка ниже подготовит короткое SMS с данными анкеты. Текст уже заполнен — останется нажать «Отправить».</div>
      <p class="sms-number">${/^\+[1-9]\d{7,14}$/.test(number) ? `Служебный номер: <strong>${escapeHTML(number)}</strong>` : '<strong>Служебный номер пока не настроен.</strong> Код можно скопировать и отправить координатору вручную.'}</p><div id="sms-queue" class="offline-queue sms-queue"></div><hr class="divider"><div id="offline-readiness"></div><hr class="divider"><button class="secondary" data-action="export">Скачать резервную копию анкет</button><p class="hint">Не очищайте данные браузера, пока все анкеты не переданы на сервер.</p>`);
    renderReadiness();
    renderPendingSurveys('sms-queue').catch(e=>error(friendlyError(e)));
  }
}
function precinctOptions(query, tik) {
  const needle = query.trim().replace(/^№\s*/, '');
  return config.precincts.filter(p => p.tik === tik && (!needle || p.label.includes(needle)))
    .sort((a,b) => a.label.localeCompare(b.label, 'ru', {numeric:true}))
    .map(p=>`<option value="${escapeHTML(p.id)}">${escapeHTML(p.label)}</option>`).join('');
}
function go(target) { screen = target; render(); window.scrollTo({top:0}); }
async function submitSurvey() {
  if (submitting || !currentDay() || !draft) return;
  submitting = true;
  const button = $('[data-action="submit"]'); if (button) button.disabled = true;
  try {
    // Freeze the id and timestamp before any I/O, so retries retain the same identity.
    draft.created_at ||= new Date().toISOString(); saveDraft();
    const body = {...draft, profile};
    await putSurvey({...body, received:false});
    draft = null; saveDraft(); go('home');
    toast(connected ? 'Анкета сохранена. Можно начинать следующий опрос.' : 'Нет подключения. Анкета сохранена на устройстве — отправьте её через SMS или дождитесь интернета.');
    void sync();
  } catch (e) { error('Не удалось сохранить анкету на телефоне. Освободите место и повторите. Ответы остаются на экране.'); if (button) button.disabled = false; }
  finally { submitting = false; }
}
async function exportBackup() {
  const blob = new Blob([JSON.stringify({exported_at:new Date().toISOString(),surveys:await allSurveys()},null,2)], {type:'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = `exit-poll-backup-${today()}.json`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  toast('Резервная копия содержит ответы и имя интервьюера. Передайте её только координатору.');
}
document.addEventListener('input', event => {
  if (event.target.id === 'precinct-search') refreshPrecincts();
});
document.addEventListener('change', event => {
  if (event.target.id === 'tik') {
    $('#precinct-search').value = '';
    $('#precinct').value = '';
    refreshPrecincts();
  }
});
document.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, data = new FormData(form);
  try {
    if (form.id === 'profile-form') {
      const surname = data.get('surname').trim(), name = data.get('name').trim();
      if (!surname || !name) throw new Error('Укажите фамилию и имя.');
      const tik = data.get('tik'), precinct = data.get('precinct');
      if (!tik || !config.precincts.some(p => p.id === precinct && p.tik === tik)) throw new Error('Выберите ТИК и УИК из его списка.');
      profile = {id:crypto.randomUUID(),surname,name,tik,precinct,day:today()};
      store('profile',profile); store('previous',{surname,name}); draft = null; saveDraft();
      go(authNeeded?'login':'home');
      if (navigator.storage?.persist) void navigator.storage.persist();
    } else if (form.id === 'login-form') {
      await api('/api/login',{code:data.get('code')}); authNeeded=false; store('authorized',true); go(profile?'home':'profile'); void sync();
    }
  } catch(e) { error(friendlyError(e)); }
});
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button || button.disabled) return;
  try {
    if (button.dataset.smsId) { await openSurveySms(button.dataset.smsId); }
    else if (button.dataset.copySmsId) { await copySurveySms(button.dataset.copySmsId); }
    else if (button.dataset.party) { if(!currentDay())return; draft.party=button.dataset.party; saveDraft(); go(draft.party==='refused'&&!config.refusal_demographics?'review':'demographics'); }
    else if (button.dataset.gender) { draft.gender=button.dataset.gender; saveDraft(); render(); }
    else if (button.dataset.age) { draft.age=button.dataset.age; saveDraft(); render(); }
    else switch(button.dataset.action) {
      case 'new': if(currentDay()){ draft={id:crypto.randomUUID(),party:null,gender:null,age:null};saveDraft();go('party');} break;
      case 'submit': await submitSurvey(); break;
      case 'sync': await sync(); break;
      case 'change': go('profile'); break;
      case 'export': await exportBackup(); break;
      case 'check-offline': await checkOfflineReadiness(); break;
      case 'install': await installApplication(); break;
      default: if(button.dataset.action) go(button.dataset.action);
    }
  } catch(e) { error(friendlyError(e)); }
});
async function boot() {
  try {
    db = await openDatabase();
    // Verify that storage is writable before promising offline collection.
    store('storage-check',true);
    try { config=await api('/api/config');store('config',config);connection(true); }
    catch { config=stored('config');connection(false); }
    if(!config) throw new Error('Для первого запуска подключитесь к интернету и обновите страницу.');
    profile=stored('profile'); draft=stored('draft');
    if(profile && (profile.day!==today() || !config.precincts.some(p => p.id === profile.precinct))){profile=null;draft=null;store('profile',null);saveDraft();}
    if(config.auth_required){
      try {await api('/api/session');store('authorized',true);}
      catch(e){authNeeded=e.status===401||!stored('authorized',false);}
    }
    screen=authNeeded?'login':profile?(draft?.party?'review':draft?'party':'home'):'profile';
    if(screen==='review'&&(!draft.gender||!draft.age)) screen='demographics';
    render();
    if('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('controllerchange',()=>void checkOfflineReadiness());
      navigator.serviceWorker.register('/sw.js')
        .then(()=>checkOfflineReadiness())
        .catch(()=>{offlineReadiness={state:'not-ready',detail:'Офлайн-загрузка не включилась. Оставьте приложение открытым при интернете и проверьте HTTPS.'};renderReadiness();});
    } else {
      offlineReadiness={state:'not-ready',detail:'Этот браузер не поддерживает офлайн-приложения.'};renderReadiness();
    }
    void sync();
    setInterval(()=>{if(profile&&!currentDay())return;void sync();},30000);
    window.addEventListener('online',()=>void sync());
    window.addEventListener('offline',()=>{connection(false);if(screen==='home')void updateStats();});
    window.addEventListener('pageshow',()=>{if(profile)currentDay();});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(profile)currentDay();void sync();}});
    channel?.addEventListener('message',()=>{if(screen==='home')void updateStats();});
  } catch(e) { $('#app').innerHTML=card(`<h2>Не удалось начать</h2><p class="error">${escapeHTML(friendlyError(e))}</p><p class="muted">Проверьте соединение и разрешите хранение данных сайта. Затем обновите страницу.</p>`); }
}
void boot();
