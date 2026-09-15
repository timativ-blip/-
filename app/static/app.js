'use strict';
const $ = selector => document.querySelector(selector);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let config, db, profile, draft, screen = 'profile', syncing = false, submitting = false, connected = false, authNeeded = false, lastSmsAttempt = 0;
const channel = 'BroadcastChannel' in window ? new BroadcastChannel('exit-poll') : null;

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
  toast('Начался новый день. Укажите имя и УИК для новой смены.');
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
  if (!notice.hidden) notice.innerHTML = `Анкеты сохраняются на телефоне. Откройте приложение при восстановлении связи — они отправятся автоматически.<button data-action="sms">Помощь и SMS</button>`;
  if (!ok || slow) maybeRequestSms();
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
}
function steps(n) { return `<div class="steps" aria-label="Шаг ${n} из 3">${[1,2,3].map(i=>`<span class="${i<=n?'done':''}"></span>`).join('')}</div><div class="section-kicker">АНКЕТА · ШАГ ${n} ИЗ 3</div>`; }
function card(content) { return `<section class="card">${content}<p id="error" class="error" role="alert"></p></section>`; }
const back = (target, text='Назад') => `<button class="back" data-action="${target}">← ${text}</button>`;
const precinctLabel = () => config.precincts.find(p => p.id === profile?.precinct)?.label || profile?.precinct;
function render() {
  $('#toast').hidden = true;
  if (screen === 'profile') {
    const previous = stored('previous', {});
    $('#app').innerHTML = card(`<div class="section-kicker">НАЧАЛО РАБОЧЕГО ДНЯ</div><div class="title-row"><h2>Ваша смена</h2><span class="tag">${escapeHTML(dateLabel())}</span></div><p class="muted">Представьтесь и выберите участок.<br>Это нужно сделать один раз в день.</p>
      <form id="profile-form"><div class="two-col"><div><label class="field" for="surname">Фамилия</label><input id="surname" name="surname" autocomplete="family-name" placeholder="Иванов" maxlength="80" required value="${escapeHTML(previous.surname || '')}"></div><div><label class="field" for="name">Имя</label><input id="name" name="name" autocomplete="given-name" placeholder="Иван" maxlength="80" required value="${escapeHTML(previous.name || '')}"></div></div>
      <label class="field" for="precinct-search">Найти УИК</label><input id="precinct-search" type="search" placeholder="Номер участка или населённый пункт" autocomplete="off"><label class="field" for="precinct">Ваш УИК</label><select name="precinct" id="precinct" required><option value="">Выберите участок</option>${precinctOptions('')}</select>
      ${config.demo?'<p class="hint">Тестовая версия. Демонстрационные УИК нужно заменить официальным списком перед запуском.</p>':''}
      <button class="primary action" type="submit">Сохранить и начать <span>→</span></button><p class="hint">Имя относится к интервьюеру. Личные данные респондента не запрашиваются.</p></form>`);
  } else if (screen === 'login') {
    $('#app').innerHTML = card(`<div class="section-kicker">ДОСТУП К ИССЛЕДОВАНИЮ</div><h2>Код вашей команды</h2><p class="muted">Код выдаёт координатор. Он защищает сбор анкет от посторонних отправок.</p><form id="login-form"><label for="code" class="field">Код доступа</label><input id="code" name="code" type="password" autocomplete="current-password" required><button class="primary action">Продолжить →</button></form>${profile?'<button class="text-button" data-action="home">Продолжить сбор офлайн</button>':''}`);
  } else if (screen === 'home') {
    const initials = (profile.name[0] + profile.surname[0]).toUpperCase();
    $('#app').innerHTML = card(`<div class="shift"><div class="badge">${escapeHTML(initials)}</div><span class="tag">${escapeHTML(dateLabel())}</span></div><div class="section-kicker">СМЕНА ОТКРЫТА</div><h2>${escapeHTML(profile.name)}, вы на месте.</h2><p class="summary-line">${escapeHTML(profile.surname)} ${escapeHTML(profile.name)}<br>${escapeHTML(precinctLabel())}</p>
      <div class="stats"><div class="stat"><strong id="completed">—</strong><span>анкет сегодня</span></div><div class="stat"><strong id="refused">—</strong><span>отказов сегодня</span></div></div><button class="primary" data-action="new">＋ Новая анкета</button>
      <div class="sync-row"><span id="sync-status">Проверяем отправку…</span><button class="text-button" data-action="sync">Обновить</button></div><button id="auth-link" class="secondary" data-action="login" hidden>Ввести код доступа</button><button id="export-link" class="text-button" data-action="export" hidden>Скачать резервную копию</button>
      ${!config.sheets_configured?'<div class="status-note">Google Таблицы ещё не подключены. Анкеты будут сохранены на сервере до подключения.</div>':'<p class="hint">Сервер передаёт анкеты в Google Таблицы отдельной очередью. При сбое передача повторяется автоматически.</p>'}
      <hr class="divider"><div class="title-row"><button class="text-button" data-action="sms">Связь и SMS</button><button class="text-button" data-action="change">Изменить данные смены</button></div><p class="hint">Счётчики учитывают анкеты этого интервьюера в этом браузере за сегодня, включая ещё не отправленные.</p>`);
    updateStats().catch(e=>error(friendlyError(e)));
  } else if (screen === 'party') {
    $('#app').innerHTML = card(`${back('home','К смене')}${steps(1)}<h2>Выбор респондента</h2><p class="script">Добрый день, я провожу анонимный опрос сразу после голосования. Подскажите, пожалуйста, за какую партию вы только что проголосовали?</p><div class="parties">${config.parties.map(p=>`<button class="option ${p.id==='refused'?'refusal':p.id==='spoiled'?'spoiled':''}" data-party="${escapeHTML(p.id)}" ${p.disabled?'disabled aria-disabled="true"':''}>${/^\d+$/.test(p.id)?`<span class="num">${p.id}</span>`:''}<span>${escapeHTML(p.label)}</span>${p.disabled?'<small>недоступно</small>':''}</button>`).join('')}</div>`);
  } else if (screen === 'demographics') {
    $('#app').innerHTML = card(`${back('party')}${steps(2)}<h2>О респонденте</h2><p class="muted">Укажите пол и возрастную группу.</p><fieldset class="choice-group"><legend>Пол респондента</legend><div class="choices">${[['male','Мужской'],['female','Женский']].map(([id,label])=>`<button class="choice" data-gender="${id}" aria-pressed="${draft.gender===id}">${label}</button>`).join('')}</div></fieldset><fieldset class="choice-group"><legend>Возраст респондента</legend><div class="choices ages">${config.ages.map(age=>`<button class="choice" data-age="${escapeHTML(age)}" aria-pressed="${draft.age===age}">${escapeHTML(age)}</button>`).join('')}</div></fieldset><button class="primary action" data-action="review" ${!draft.gender||!draft.age?'disabled':''}>Далее <span>→</span></button>`);
  } else if (screen === 'review') {
    $('#app').innerHTML = card(`${back(draft.party==='refused'&&!config.refusal_demographics?'party':'demographics')}${steps(3)}<h2>Всё верно?</h2><p class="muted">Проверьте ответы перед отправкой.</p><dl class="review"><div><dt>Партия / ответ</dt><dd class="${draft.party==='refused'?'red':''}">${escapeHTML(config.parties.find(p=>p.id===draft.party).label)}</dd></div><div><dt>Пол респондента</dt><dd>${draft.gender==='male'?'Мужской':draft.gender==='female'?'Женский':'Не указан'}</dd></div><div><dt>Возраст респондента</dt><dd>${escapeHTML(draft.age||'Не указан')}</dd></div></dl><p class="hint">${escapeHTML(precinctLabel())}</p><button class="primary action" data-action="submit">Отправить анкету <span>✓</span></button><p class="hint">При отсутствии связи анкета сохранится на телефоне и будет отправлена при следующем подключении.</p>`);
  } else if (screen === 'sms') {
    const preferences = stored('sms', {});
    const number = config.sms_request_number;
    $('#app').innerHTML = card(`${back(profile?'home':'profile')}<div class="section-kicker">РАБОТА ПРИ СЛАБОЙ СВЯЗИ</div><h2>Продолжайте опрос</h2><p class="muted">Открытые ранее анкеты доступны без интернета. После возвращения связи оставьте приложение открытым для отправки.</p><div class="status-note">SMS содержит текст опроса. Заполнять ответы и отправлять их нужно в приложении. Ответы на SMS пока не принимаются.</div>
      ${config.sms_configured?`<form id="sms-form"><label class="field" for="phone">Телефон интервьюера для SMS</label><input id="phone" name="phone" type="tel" placeholder="+79001234567" pattern="[+][1-9][0-9]{7,14}" value="${escapeHTML(preferences.phone||'')}" required><label class="check"><input name="enabled" type="checkbox" ${preferences.enabled?'checked':''}><span>Присылать SMS при слабой связи. Мой номер будет передан подключённому SMS-сервису.</span></label><button class="primary action">Сохранить настройки SMS</button></form><button class="text-button" data-action="request-sms">Запросить SMS сейчас</button>`:'<p class="hint">SMS-сервис ещё не подключён координатором.</p>'}
      <p class="hint">Без интернета автоматический запрос SMS не дойдёт до сервера. Если работает сотовая сеть, можно отправить запрос на служебный номер, когда координатор подключит приём SMS.</p>${/^\+[1-9]\d{7,14}$/.test(number)?`<a href="sms:${escapeHTML(number)}?body=EXITPOLL" class="secondary">Открыть SMS с запросом</a>`:''}<hr class="divider"><button class="secondary" data-action="export">Скачать резервную копию анкет</button><p class="hint">Не очищайте данные браузера, пока все анкеты не переданы на сервер.</p>`);
  }
}
function precinctOptions(query) {
  return config.precincts.filter(p=>p.label.toLocaleLowerCase('ru').includes(query.toLocaleLowerCase('ru'))||p.id.includes(query)).map(p=>`<option value="${escapeHTML(p.id)}">${escapeHTML(p.label)}</option>`).join('');
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
    draft = null; saveDraft(); go('home'); toast('Анкета сохранена. Можно начинать следующий опрос.');
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
async function requestSms(automatic=false) {
  const prefs = stored('sms', {});
  if (!prefs.phone || !config?.sms_configured) { if(!automatic) error('Сначала сохраните телефон в настройках SMS.'); return; }
  if (Date.now() - lastSmsAttempt < 15*60*1000) { if(!automatic) error('Повторный запрос доступен через 15 минут.'); return; }
  lastSmsAttempt = Date.now();
  try { await api('/api/sms/request',{phone:prefs.phone},15000); toast('Запрос SMS принят сервисом.'); }
  catch(e) { if(!automatic) error(e.status?e.message:'Нет связи с сервером. Продолжайте заполнять анкеты офлайн.'); }
}
function maybeRequestSms() { if (stored('sms',{}).enabled) void requestSms(true); }
document.addEventListener('input', event => {
  if (event.target.id === 'precinct-search') $('#precinct').innerHTML = '<option value="">Выберите участок</option>' + precinctOptions(event.target.value);
});
document.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, data = new FormData(form);
  try {
    if (form.id === 'profile-form') {
      const surname = data.get('surname').trim(), name = data.get('name').trim();
      if (!surname || !name) throw new Error('Укажите фамилию и имя.');
      profile = {id:crypto.randomUUID(),surname,name,precinct:data.get('precinct'),day:today()};
      store('profile',profile); store('previous',{surname,name}); draft = null; saveDraft();
      go(authNeeded?'login':'home');
      if (navigator.storage?.persist) void navigator.storage.persist();
    } else if (form.id === 'login-form') {
      await api('/api/login',{code:data.get('code')}); authNeeded=false; store('authorized',true); go(profile?'home':'profile'); void sync();
    } else if (form.id === 'sms-form') {
      store('sms',{phone:data.get('phone'),enabled:data.get('enabled')==='on'}); toast('Настройки SMS сохранены.');
    }
  } catch(e) { error(friendlyError(e)); }
});
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button || button.disabled) return;
  try {
    if (button.dataset.party) { if(!currentDay())return; draft.party=button.dataset.party; saveDraft(); go(draft.party==='refused'&&!config.refusal_demographics?'review':'demographics'); }
    else if (button.dataset.gender) { draft.gender=button.dataset.gender; saveDraft(); render(); }
    else if (button.dataset.age) { draft.age=button.dataset.age; saveDraft(); render(); }
    else switch(button.dataset.action) {
      case 'new': if(currentDay()){ draft={id:crypto.randomUUID(),party:null,gender:null,age:null};saveDraft();go('party');} break;
      case 'submit': await submitSurvey(); break;
      case 'sync': await sync(); break;
      case 'change': go('profile'); break;
      case 'export': await exportBackup(); break;
      case 'request-sms': await requestSms(); break;
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
    if(profile?.day!==today()){profile=null;draft=null;store('profile',null);saveDraft();}
    if(config.auth_required){
      try {await api('/api/session');store('authorized',true);}
      catch(e){authNeeded=e.status===401||!stored('authorized',false);}
    }
    screen=authNeeded?'login':profile?(draft?.party?'review':draft?'party':'home'):'profile';
    if(screen==='review'&&(!draft.gender||!draft.age)) screen='demographics';
    render();
    if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>toast('Офлайн-загрузка не включилась. Оставьте приложение открытым и проверьте HTTPS.'));
    void sync();
    setInterval(()=>{if(profile&&!currentDay())return;void sync();},30000);
    window.addEventListener('online',()=>void sync());
    window.addEventListener('offline',()=>connection(false));
    window.addEventListener('pageshow',()=>{if(profile)currentDay();});
    document.addEventListener('visibilitychange',()=>{if(!document.hidden){if(profile)currentDay();void sync();}});
    channel?.addEventListener('message',()=>{if(screen==='home')void updateStats();});
  } catch(e) { $('#app').innerHTML=card(`<h2>Не удалось начать</h2><p class="error">${escapeHTML(friendlyError(e))}</p><p class="muted">Проверьте соединение и разрешите хранение данных сайта. Затем обновите страницу.</p>`); }
}
void boot();
