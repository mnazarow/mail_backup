/* ===================================================================
   MailArchiver — веб-интерфейс (SPA на чистом JS, без внешних зависимостей).
   =================================================================== */
"use strict";

// ---------- Утилиты ----------
const $ = (sel, root=document) => root.querySelector(sel);
const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));
const root = () => document.getElementById('root');

function h(html){ const t=document.createElement('template'); t.innerHTML=html.trim(); return t.content.firstElementChild; }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDate(iso){ if(!iso) return '—'; try{ const d=new Date(iso); return d.toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}); }catch(e){ return iso; } }
function fmtBytes(n){ n=Number(n||0); const u=['Б','КБ','МБ','ГБ','ТБ']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return (i===0?n:n.toFixed(1))+' '+u[i]; }

const State = { user:null, help:null, view:null, ws:null, wsTimer:null, engines:[], accounts:[], logLevel:'' };

//: Пути входа: их 401 — обычный ответ «неверный пароль/сессии нет», а не конец сессии.
const AUTH_PATHS = new Set(['/login', '/login/otp', '/setup', '/needs-setup']);

async function api(path, opts={}){
  const o = Object.assign({}, opts);
  o.headers = Object.assign({}, opts.headers||{});
  // Фоновый запрос (живое обновление, ход задания) не продлевает сессию:
  // иначе «Тайм-аут бездействия» не срабатывал, пока вкладка открыта.
  const bg = !!o.bg; delete o.bg;
  if(o.body && !(o.body instanceof FormData)){ o.headers['Content-Type']='application/json'; o.body=JSON.stringify(o.body); }
  o.headers['X-Requested-With']='fetch';
  if(bg) o.headers['X-MA-Background']='1';
  const res = await fetch('/api'+path, o);
  let data=null; const ct=res.headers.get('content-type')||'';
  if(ct.includes('application/json')) data=await res.json();
  // HTTPException с объектом в detail: поднимаем поля наверх (message/code/hint)
  if(data && data.detail && typeof data.detail==='object' && !Array.isArray(data.detail)){
    data = Object.assign({}, data.detail, {detail: data.detail.message||''});
  }
  if(!res.ok){
    const msg = (data && (data.message||(typeof data.detail==='string'?data.detail:''))) || ('Ошибка '+res.status);
    const err = new Error(msg); err.data=data; err.status=res.status;
    if(data && data.code==='2fa_required' && State.user && !State.enrolling2fa){ twoFactorModal({mandatory:true}); }
    if(res.status===401 && State.user && !AUTH_PATHS.has(path.split('?')[0])) sessionGone();
    throw err;
  }
  return data;
}

// ---------- Тосты ----------
function toast(title, message='', type='success', ttl=4200){
  const t = h(`<div class="toast ${type}"><div class="tt">${esc(title)}</div>${message?`<div class="tm">${esc(message)}</div>`:''}</div>`);
  $('#toasts').appendChild(t);
  setTimeout(()=>{ t.style.opacity='0'; t.style.transform='translateX(20px)'; setTimeout(()=>t.remove(),200); }, ttl);
}
function toastErr(e){ toast('Ошибка', (e&&e.message)||String(e), 'error', 6000); const hint=e&&e.data&&e.data.hint; if(hint) toast('Подсказка', hint, 'warn', 7000); }

/** Сессия закончилась (тайм-аут, выход в другой вкладке, смена пароля):
 *  останавливаем живое обновление и возвращаемся к форме входа. Раньше раздел
 *  навсегда оставался со спиннером, а вкладка продолжала опрашивать сервер. */
function sessionGone(){
  if(State.sessionGone) return;
  State.sessionGone=true;
  try{ if(State.ws) State.ws.close(); }catch(_){}
  if(State.wsTimer){ clearInterval(State.wsTimer); State.wsTimer=null; }
  if(State.checkLoginsTimer){ clearInterval(State.checkLoginsTimer); }
  toast('Сеанс завершён', 'Войдите снова — откроется тот же раздел.', 'warn', 5000);
  setTimeout(()=>location.reload(), 1500);
}

// ---------- Тема ----------
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme');
  const next = cur==='dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('ma-theme', next); }catch(e){}
  // перерисовать графики под новую тему (canvas не наследует CSS-переменные)
  if(State.redraw){ requestAnimationFrame(()=>{ try{ State.redraw(); }catch(e){} }); }
}

// ---------- Подсказка по параметру ----------
function helpIcon(entry){
  if(!entry) return '';
  const ex = entry.example ? `<div class="ex">Пример: <code>${esc(entry.example)}</code></div>` : '';
  const rec = entry.recommend ? `<div class="rec">💡 ${esc(entry.recommend)}</div>` : '';
  const df = entry.default ? `<div class="ex">По умолчанию: <code>${esc(entry.default)}</code></div>` : '';
  // tabindex + role: подсказку должно быть видно с клавиатуры и на планшете,
  // где наведения курсора не бывает.
  return `<span class="help-ic" tabindex="0" role="button" aria-label="Подсказка">?<span class="help-pop"><div class="t">${esc(entry.title||'')}</div><div>${esc(entry.help||'')}</div>${rec}${ex}${df}</span></span>`;
}

// ---------- Модалки ----------
function modal(title, bodyHtml, {wide=false, footer='', onClose=null, locked=false}={}){
  const back = h(`<div class="modal-back" role="dialog" aria-modal="true"><div class="modal ${wide?'wide':''}">
    <div class="modal-head"><h2>${esc(title)}</h2>${locked?'':'<button class="x" type="button" aria-label="Закрыть">×</button>'}</div>
    <div class="modal-body">${bodyHtml}</div>
    ${footer?`<div class="modal-foot">${footer}</div>`:''}</div></div>`);
  // Escape закрывает только ВЕРХНИЙ диалог: раньше один Escape закрывал и
  // подтверждение, и окно под ним. Tab не выпускает фокус из окна.
  const focusables=()=>Array.from(back.querySelectorAll(
      'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'))
    .filter(el=>el.offsetParent!==null || el===document.activeElement);
  const onKey=(e)=>{
    const top = back===document.querySelector('.modal-back:last-of-type');
    if(!top) return;
    if(!locked && e.key==='Escape'){ close(); return; }
    if(e.key==='Tab'){
      const list=focusables(); if(!list.length) return;
      const first=list[0], last=list[list.length-1];
      if(!back.contains(document.activeElement)){ e.preventDefault(); first.focus(); }
      else if(e.shiftKey && document.activeElement===first){ e.preventDefault(); last.focus(); }
      else if(!e.shiftKey && document.activeElement===last){ e.preventDefault(); first.focus(); }
    }
  };
  let closed=false;
  const prevFocus=document.activeElement;
  const close=()=>{
    if(closed) return; closed=true;
    back.remove(); document.removeEventListener('keydown', onKey);
    if(prevFocus && prevFocus.focus) { try{ prevFocus.focus(); }catch(e){} }
    if(onClose) onClose();               // закрытие крестиком/Escape/фоном — тоже ответ
  };
  if(!locked){
    back.querySelector('.x').onclick=close;
    back.onclick=(e)=>{ if(e.target===back) close(); };
  }
  document.addEventListener('keydown', onKey);
  document.body.appendChild(back);
  // Начальный фокус — на первое поле формы, а не на «×» в заголовке; в окне
  // подтверждения — на «Отмена» (случайный Enter не подтвердит удаление).
  const first=back.querySelector('.modal-body input:not([type=hidden]):not([disabled]),.modal-body select,.modal-body textarea')
    || back.querySelector('.modal-foot [data-c]') || back.querySelector('.modal-foot .btn') || back.querySelector('.x');
  if(first) { try{ first.focus(); }catch(e){} }
  return { el:back, close, body:back.querySelector('.modal-body'), foot:back.querySelector('.modal-foot') };
}
function confirmDlg(title, message, {okText='Подтвердить', okClass='danger'}={}){
  return new Promise(res=>{
    // Промис обязан разрешиться при ЛЮБОМ закрытии: иначе «await confirmDlg»
    // висел вечно, и кнопка выглядела сломанной.
    const m=modal(title, `<p>${esc(message)}</p>`,
      {onClose:()=>res(false),
       footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn ${okClass}" data-ok>${esc(okText)}</button>`});
    m.foot.querySelector('[data-c]').onclick=()=>{m.close();};
    m.foot.querySelector('[data-ok]').onclick=()=>{ res(true); m.close(); };
  });
}

// ===================================================================
//  Аутентификация / первичная настройка
// ===================================================================
async function boot(){
  try{
    const ns = await api('/needs-setup');
    if(ns.needs_setup) return renderSetup();
    if(!ns.auth_enabled){ try{ State.user=await api('/me'); }catch(_){ State.user={username:'admin',role:'admin'}; } return startApp(); }
    try{ State.user = await api('/me'); startApp(); }
    catch(e){ renderLogin(); }
  }catch(e){ root().innerHTML=`<div class="auth-wrap"><div class="card auth-card"><h2>Сервис недоступен</h2><p class="muted">${esc(e.message)}</p></div></div>`; }
}

function renderLogin(){
  root().innerHTML='';
  const card=h(`<div class="auth-wrap"><div class="card auth-card">
    <div class="brand"><div class="logo">📥</div><div><div class="name">MailArchiver</div><div class="ver muted">Резервное копирование почты</div></div></div>
    <h3 style="text-align:center">Вход в систему</h3>
    <div class="form-row"><label>Пользователь</label><input id="lu" type="text" autocomplete="username" autofocus></div>
    <div class="form-row"><label>Пароль</label><input id="lp" type="password" autocomplete="current-password"></div>
    <button class="btn primary" id="lb" style="width:100%;justify-content:center">Войти</button>
  </div></div>`);
  root().appendChild(card);
  // Один запрос за раз: Enter на кнопке «Войти» вызывал и keydown, и click —
  // два запроса, и каждая ошибка считалась дважды.
  let busy=false;
  const submit=async()=>{
    if(busy) return; busy=true; $('#lb').disabled=true;
    try{
      const r=await api('/login',{method:'POST',body:{username:$('#lu').value,password:$('#lp').value}});
      if(r.otp_required) return renderOtpStep(r.challenge);
      State.user=r.user; afterLogin();
    }catch(e){ toastErr(e); }
    finally{ busy=false; const b=$('#lb'); if(b) b.disabled=false; }
  };
  $('#lb').onclick=submit;
  card.addEventListener('keydown',e=>{ if(e.key==='Enter' && e.target.tagName!=='BUTTON'){ e.preventDefault(); submit(); } });
}

/** Второй шаг входа: код из приложения-аутентификатора или резервный код. */
function renderOtpStep(challenge){
  root().innerHTML='';
  const card=h(`<div class="auth-wrap"><div class="card auth-card">
    <div class="brand"><div class="logo">🔐</div><div><div class="name">MailArchiver</div><div class="ver muted">Подтверждение входа</div></div></div>
    <h3 style="text-align:center">Код из приложения</h3>
    <p class="muted small" style="text-align:center">Откройте приложение-аутентификатор и введите 6 цифр. Если телефона нет под рукой — введите один из резервных кодов.</p>
    <div class="form-row"><label>Код</label><input id="lo" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="11" autofocus style="font-size:20px;letter-spacing:4px;text-align:center"></div>
    <button class="btn primary" id="lob" style="width:100%;justify-content:center">Подтвердить</button>
    <div style="text-align:center;margin-top:12px"><a href="#" id="loback" class="small">← Войти заново</a></div>
  </div></div>`);
  root().appendChild(card);
  // Один запрос за раз: у билета второго шага ограничено число попыток, и
  // двойная отправка (Enter + click) тратила бы сразу две.
  let busy=false;
  const go=async()=>{
    if(busy) return; busy=true; $('#lob').disabled=true;
    try{
      const r=await api('/login/otp',{method:'POST',body:{challenge, code:$('#lo').value}});
      State.user=r.user;
      if(r.user && r.user.recovery_left!=null) toast('Вход по резервному коду', `Осталось резервных кодов: ${r.user.recovery_left}. Выпустите новые в профиле.`, 'warn', 9000);
      afterLogin();
    }catch(e){ toastErr(e); if(e.status===400 && /истекло|войдите заново/i.test(e.message||'')) setTimeout(renderLogin, 1500); }
    finally{ busy=false; const b=$('#lob'); if(b) b.disabled=false; }
  };
  $('#lob').onclick=go;
  $('#loback').onclick=(e)=>{ e.preventDefault(); renderLogin(); };
  card.addEventListener('keydown',e=>{ if(e.key==='Enter' && e.target.tagName!=='BUTTON' && e.target.tagName!=='A'){ e.preventDefault(); go(); } });
  setTimeout(()=>{ try{ $('#lo').focus(); }catch(_){} }, 50);
}

/** После входа: подтянуть /me (флаги 2FA) и открыть приложение. */
async function afterLogin(){
  try{ State.user = await api('/me'); }catch(_){}
  startApp();
}

function renderSetup(){
  root().innerHTML='';
  const card=h(`<div class="auth-wrap"><div class="card auth-card">
    <div class="brand"><div class="logo">📥</div><div><div class="name">MailArchiver</div><div class="ver muted">Первичная настройка</div></div></div>
    <h3 style="text-align:center">Создайте администратора</h3>
    <p class="muted small" style="text-align:center">Это единственный шаг настройки. Запомните эти данные — они нужны для входа.</p>
    <div class="form-row"><label>Имя администратора</label><input id="su" type="text" value="admin" autofocus></div>
    <div class="form-row"><label>Пароль (минимум 8 символов)</label><input id="sp" type="password"></div>
    <div class="form-row"><label>Повторите пароль</label><input id="sp2" type="password"></div>
    <button class="btn primary" id="sb" style="width:100%;justify-content:center">Создать и войти</button>
  </div></div>`);
  root().appendChild(card);
  $('#sb').onclick=async()=>{
    if($('#sp').value!==$('#sp2').value) return toast('Проверьте пароль','Пароли не совпадают','error');
    try{
      await api('/setup',{method:'POST',body:{username:$('#su').value,password:$('#sp').value}});
      const r=await api('/login',{method:'POST',body:{username:$('#su').value,password:$('#sp').value}});
      State.user=r.user; toast('Готово','Администратор создан'); afterLogin();
    }catch(e){ toastErr(e); }
  };
}

// ===================================================================
//  Двухфакторный вход (TOTP)
// ===================================================================
async function twoFactorModal({mandatory=false}={}){
  if(State.enrolling2fa) return;
  let st;
  try{ st=await api('/me/2fa'); }catch(e){ return toastErr(e); }
  if(st.enabled && !mandatory){
    const m=modal('Двухфакторный вход', `
      <p>✅ Включён. При входе после пароля запрашивается код из приложения.</p>
      <p class="muted small">Неиспользованных резервных кодов: <b>${st.recovery_left}</b>. Каждый код действует один раз.</p>
      <div class="form-row"><label>Код из приложения</label><input id="tf-code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="11"></div>
      <div class="form-row"><label>Пароль (нужен только для отключения)</label><input id="tf-pw" type="password" autocomplete="current-password"></div>
      ${st.required?'<div class="hint">Двухфакторный вход обязателен по настройкам безопасности — отключить его нельзя.</div>':''}`,
      {footer:`<button class="btn" data-rec>Новые резервные коды</button>${st.required?'':'<button class="btn danger" data-off>Отключить</button>'}`});
    m.foot.querySelector('[data-rec]').onclick=async()=>{
      try{ const r=await api('/me/2fa/recovery',{method:'POST',body:{code:m.body.querySelector('#tf-code').value}}); m.close(); showRecoveryCodes(r.recovery_codes, false); }
      catch(e){ toastErr(e); }
    };
    const off=m.foot.querySelector('[data-off]');
    if(off) off.onclick=async()=>{
      try{ await api('/me/2fa/disable',{method:'POST',body:{code:m.body.querySelector('#tf-code').value,password:m.body.querySelector('#tf-pw').value}});
        m.close(); toast('Двухфакторный вход отключён'); State.user.totp_enabled=false; }
      catch(e){ toastErr(e); }
    };
    return;
  }
  // включение
  State.enrolling2fa=true;
  let setup;
  try{ setup=await api('/me/2fa/setup',{method:'POST'}); }catch(e){ State.enrolling2fa=false; return toastErr(e); }
  const m=modal(mandatory?'Включите двухфакторный вход':'Включение двухфакторного входа', `
    ${mandatory?'<div class="hint" style="margin-bottom:12px;border-color:var(--warn)">По настройкам безопасности администратор обязан входить с кодом из приложения. Пока это не сделано, остальные разделы недоступны.</div>':''}
    <ol class="small" style="padding-left:18px;margin-top:0">
      <li>Установите на телефон приложение-аутентификатор: Яндекс Ключ, Google Authenticator, Microsoft Authenticator или FreeOTP.</li>
      <li>Отсканируйте QR-код (или введите ключ вручную).</li>
      <li>Введите 6 цифр, которые покажет приложение.</li>
    </ol>
    <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
      <div class="qr-box" style="background:#fff;padding:6px;border-radius:8px;line-height:0">${setup.qr_svg}</div>
      <div style="flex:1;min-width:200px">
        <div class="muted small">Ключ для ручного ввода:</div>
        <div class="mono" style="font-size:15px;word-break:break-all;margin:4px 0 12px">${esc(setup.secret)}</div>
        <div class="form-row"><label>Код из приложения</label><input id="tf-new" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" style="font-size:18px;letter-spacing:3px"></div>
      </div>
    </div>`,
    {wide:true, locked:mandatory, onClose:()=>{ State.enrolling2fa=false; },
     footer:`${mandatory?'<button class="btn ghost" data-out>Выйти</button>':'<button class="btn ghost" data-c>Отмена</button>'}<button class="btn primary" data-ok>Включить</button>`});
  const cancel=m.foot.querySelector('[data-c]'); if(cancel) cancel.onclick=m.close;
  const out=m.foot.querySelector('[data-out]');
  if(out) out.onclick=async()=>{ try{await api('/logout',{method:'POST'});}catch(_){} location.reload(); };
  const ok=async()=>{
    try{
      const r=await api('/me/2fa/enable',{method:'POST',body:{code:m.body.querySelector('#tf-new').value}});
      State.enrolling2fa=false; m.close();
      State.user.totp_enabled=true; State.user.must_enroll_2fa=false;
      showRecoveryCodes(r.recovery_codes, mandatory);
    }catch(e){ toastErr(e); }
  };
  m.foot.querySelector('[data-ok]').onclick=ok;
  m.body.querySelector('#tf-new').addEventListener('keydown',e=>{ if(e.key==='Enter') ok(); });
}

function showRecoveryCodes(codes, reloadAfter){
  const text=codes.join('\n');
  const m=modal('Резервные коды', `
    <p>Сохраните эти коды в надёжном месте (менеджер паролей, распечатка в сейфе). Каждый код подходит для входа <b>один раз</b>, если телефона нет под рукой. Больше они показаны не будут.</p>
    <pre class="mono" style="font-size:16px;line-height:1.7;column-count:2;background:var(--surface-2,#f4f6fa);padding:12px;border-radius:8px">${esc(text)}</pre>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="rc-ok"><span class="track"></span></span><label for="rc-ok">Я сохранил резервные коды</label></div>`,
    {locked:true, footer:`<button class="btn" data-copy>Скопировать</button><button class="btn" data-dl>Скачать .txt</button><button class="btn primary" data-done disabled>Готово</button>`});
  m.foot.querySelector('[data-copy]').onclick=async()=>{ try{ await navigator.clipboard.writeText(text); toast('Скопировано'); }catch(_){ toast('Не удалось скопировать','Выделите коды и скопируйте вручную','warn'); } };
  m.foot.querySelector('[data-dl]').onclick=()=>{
    const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([`MailArchiver — резервные коды (${State.user.username})\n\n${text}\n`],{type:'text/plain'}));
    a.download='mailarchiver-recovery-codes.txt'; document.body.appendChild(a); a.click(); setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 500);
  };
  const done=m.foot.querySelector('[data-done]');
  m.body.querySelector('#rc-ok').onchange=(e)=>{ done.disabled=!e.target.checked; };
  done.onclick=()=>{ m.close(); toast('Двухфакторный вход включён'); if(reloadAfter) location.reload(); };
}

// ===================================================================
//  Оболочка приложения
// ===================================================================
// Кто видит раздел: a — администратор, o — оператор, m — сотрудник (вход по ящику).
// Оператор следит за копированием и запускает его, но не читает письма, не
// меняет настроек и ничего не удаляет — это же проверяет и сервер.
const NAV = [
  {id:'dashboard', icon:'📊', title:'Дашборд', roles:'aom'},
  {id:'analytics', icon:'📈', title:'Аналитика', roles:'ao'},
  {id:'mailanalytics', icon:'🔎', title:'Аналитика писем', roles:'a'},
  {id:'mail', icon:'📧', title:'Почта', roles:'am'},
  {id:'accounts', icon:'📬', title:'Почтовые ящики', roles:'ao'},
  {id:'employees', icon:'🧑‍💼', title:'Сотрудники', roles:'a'},
  {id:'jobs', icon:'⚙️', title:'Очередь и задания', roles:'aom'},
  {id:'exports', icon:'📤', title:'Экспорт (PST)', roles:'am'},
  {id:'schedules', icon:'⏰', title:'Расписания', roles:'ao'},
  {id:'axigen', icon:'📮', title:'Сервер Axigen', roles:'a', feature:'axigen'},
  {id:'mailservers', icon:'🔌', title:'Почтовые серверы', roles:'a'},
  {id:'server', icon:'🖥️', title:'Ящики на сервере', roles:'a'},
  {sep:true},
  {id:'logs', icon:'📋', title:'Логи', roles:'ao'},
  {id:'settings', icon:'🔧', title:'Настройки', roles:'a'},
  {id:'users', icon:'👥', title:'Пользователи', roles:'a'},
  {id:'security', icon:'🔐', title:'Безопасность', roles:'a'},
  {id:'audit', icon:'🛡️', title:'Аудит', roles:'a'},
];
const ROLE_LABELS = {admin:'администратор', operator:'оператор', mailbox:'вход по ящику'};
function isMailbox(){ return !!State.user && State.user.role === 'mailbox'; }
function isAdmin(){ return !!State.user && State.user.role === 'admin'; }
function isOperator(){ return !!State.user && State.user.role === 'operator'; }
/** Администратор или оператор — учётная запись архива, а не вход по ящику. */
function isStaff(){ return isAdmin() || isOperator(); }
function roleCode(){ return isAdmin()?'a':(isMailbox()?'m':'o'); }
function navAllowed(item){ return !item.sep && item.roles.includes(roleCode()) && (!item.feature || !!((State.user&&State.user.features)||{})[item.feature]); }
/** Сотрудник (вход по ящику) может отменять и повторять только свои задания:
 *  копирование по расписанию запускает не он, и снимать его ему нельзя.
 *  Оператор — только копирование и проверки (не «с нуля», не выгрузки). */
function canTouchJob(j){
  if(isMailbox()) return !!j && j.created_by === State.user.username;
  if(isOperator()) return !!j && !!j.operator_can_manage;
  return true;
}
/** Сообщение там, где оператору нужно обратиться к администратору. */
const ADMIN_ONLY_HINT = 'Доступно администратору';

// Клик и Enter по значку «?» открывают подсказку: на тач-экране навести
// курсор невозможно, а помощь по параметрам нужна именно там.
/** У правого края окна подсказка раскрывается влево (иначе её обрезает край экрана). */
function placeHelp(ic){
  const r=ic.getBoundingClientRect(), w=Math.min(320, window.innerWidth*0.8);
  ic.classList.toggle('flip', r.left + w > window.innerWidth - 12);
}
document.addEventListener('mouseover', (e)=>{ const ic=e.target.closest && e.target.closest('.help-ic'); if(ic) placeHelp(ic); });
document.addEventListener('focusin', (e)=>{ const ic=e.target.closest && e.target.closest('.help-ic'); if(ic) placeHelp(ic); });
document.addEventListener('click', (e)=>{
  const ic=e.target.closest && e.target.closest('.help-ic');
  document.querySelectorAll('.help-ic.show').forEach(el=>{ if(el!==ic) el.classList.remove('show'); });
  if(ic){ placeHelp(ic); ic.classList.toggle('show'); }
});
// Переключатели «вкл/выкл» (.switch): сам флажок скрыт, поэтому щелчок по дорожке
// или по подписи рядом переключает его явно. Без этого флажок включался только
// с клавиатуры (Tab + пробел), а мышью — никак.
document.addEventListener('click', (e)=>{
  const el=e.target;
  if(!el || !el.closest || el.closest('.help-ic')) return;   // «?» в подписи открывает подсказку, а не флажок
  let input=null;
  const track=el.closest('.switch .track');
  if(track) input=track.parentElement.querySelector('input[type=checkbox]');
  else{
    const lab=el.closest('.form-row.check > label');
    // у подписи с for= и у подписи, внутри которой сам флажок, браузер справится сам
    if(lab && !lab.htmlFor && !lab.querySelector('input')) input=lab.parentElement.querySelector('.switch input[type=checkbox]');
  }
  if(!input || input.disabled) return;
  e.preventDefault();
  input.checked=!input.checked;
  input.dispatchEvent(new Event('change', {bubbles:true}));
});
document.addEventListener('keydown', (e)=>{
  if(e.key!=='Enter' && e.key!==' ') return;
  const ic=document.activeElement;
  if(ic && ic.classList && ic.classList.contains('help-ic')){ e.preventDefault(); ic.classList.toggle('show'); }
});

async function startApp(){
  try{ State.help = await api('/help'); }catch(e){ State.help={params:{},account:{},export:{},restore:{},schedule:{}}; }
  root().innerHTML='';
  const shell=h(`<div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="logo">📥</div><div><div class="name">MailArchiver</div><div class="ver">${State.user.version?'v'+esc(State.user.version):''}</div></div></div>
      <nav class="nav" id="nav"></nav>
      <div class="sidebar-foot"><a href="#" id="profileLink" title="Профиль и двухфакторный вход${isOperator()?' · роль: оператор':''}">${State.user.totp_enabled?'🔐 ':''}${esc(State.user.username)}${isOperator()?' <span class="tag">оператор</span>':''}</a><a href="#" id="logout" title="Выход">Выход ⎋</a></div>
    </aside>
    <div class="nav-backdrop" id="navBack"></div>
    <main class="main">
      <header class="topbar">
        <button class="btn ghost sm nav-toggle" id="navToggle" aria-label="Меню" aria-expanded="false">☰</button>
        <div class="page-title" id="pageTitle">Дашборд</div>
        <div class="spacer"></div>
        <span id="schedInd" class="small muted"></span>
        <button class="btn ghost sm" id="themeBtn" title="Сменить тему">🌓</button>
        <a class="btn ghost sm" href="/static/docs/index.html" target="_blank" title="Документация">📖 <span class="hide-narrow">Документация</span></a>
      </header>
      <div class="content" id="content"></div>
    </main>
  </div>`);
  root().appendChild(shell);
  renderNav();
  if(isMailbox() && State.user.account_id){ State.mailAccount = State.user.account_id; }
  // Меню на узком экране: раньше боковая панель просто пропадала, и перейти
  // в другой раздел можно было только правкой адреса в строке браузера.
  const sidebar=shell.querySelector('.sidebar'), navBack=$('#navBack');
  const setNav=(open)=>{
    sidebar.classList.toggle('open', open);
    navBack.classList.toggle('show', open);
    $('#navToggle').setAttribute('aria-expanded', open?'true':'false');
  };
  $('#navToggle').onclick=()=>setNav(!sidebar.classList.contains('open'));
  navBack.onclick=()=>setNav(false);
  nav.addEventListener('click', ()=>setNav(false));   // выбрали раздел — меню закрылось
  window.addEventListener('hashchange', ()=>setNav(false));
  $('#themeBtn').onclick=toggleTheme;
  $('#logout').onclick=async(e)=>{ e.preventDefault(); try{await api('/logout',{method:'POST'});}catch(_){} location.reload(); };
  $('#profileLink').onclick=(e)=>{ e.preventDefault(); if(isMailbox()) return toast('Вход по ящику','Защита такого входа — пароль самого почтового ящика.','warn'); twoFactorModal({}); };
  window.addEventListener('hashchange', route);
  if(State.user.must_enroll_2fa){ twoFactorModal({mandatory:true}); return; }
  let rzT=null;
  window.addEventListener('resize', ()=>{ if(!State.redraw) return; clearTimeout(rzT); rzT=setTimeout(()=>{ try{State.redraw();}catch(e){} }, 220); });
  startLive();
  route();
}

/** Пункты меню по роли и включённым разделам (перерисовывается, когда раздел включили в настройках). */
function renderNav(){
  const nav=$('#nav'); if(!nav) return;
  nav.innerHTML='';
  NAV.forEach(item=>{
    if(item.sep){ if(!isMailbox()) nav.appendChild(h('<div class="sep"></div>')); return; }
    if(!navAllowed(item)) return;
    const a=h(`<a href="#/${item.id}" data-view="${item.id}"><span class="ic">${item.icon}</span><span>${esc(item.title)}</span></a>`);
    a.onclick=(e)=>{ e.preventDefault(); location.hash='#/'+item.id; };
    nav.appendChild(a);
  });
  if(State.view) setActiveNav(State.view);
}

function setActiveNav(view){
  $$('#nav a').forEach(a=>a.classList.toggle('active', a.dataset.view===view));
  const item=NAV.find(n=>n.id===view);
  $('#pageTitle').textContent=item?item.title:'';
}

function route(){
  let view=(location.hash.replace('#/','').split('?')[0]||(isMailbox()?'mail':'dashboard'));
  // сотруднику и оператору доступны только их разделы
  const allowed = NAV.filter(navAllowed).map(n=>n.id);
  if(!allowed.includes(view)) view = isMailbox()?'mail':'dashboard';
  State.view=view; setActiveNav(view); State.redraw=null;
  const content=$('#content');
  // почтовому клиенту нужна вся ширина экрана — три колонки в 1240px тесно
  content.classList.toggle('wide', view==='mail');
  // Каждый переход рисует в СВОЙ контейнер. Медленный ответ прежнего раздела
  // (аналитика, поиск) раньше дорисовывался поверх нового: заголовок «Логи»,
  // а на экране аналитика. Теперь он пишет в уже отцепленный узел.
  content.innerHTML='';
  const c=h('<div class="view-host"><div class="empty"><div class="spinner"></div></div></div>');
  content.appendChild(c);
  State.routeSeq=(State.routeSeq||0)+1;
  const map={dashboard:viewDashboard,analytics:viewAnalytics,mailanalytics:viewMailAnalytics,mail:viewMail,accounts:viewAccounts,employees:viewEmployees,jobs:viewJobs,exports:viewExports,schedules:viewSchedules,axigen:viewAxigen,mailservers:viewMailservers,server:viewServer,logs:viewLogs,settings:viewSettings,users:viewUsers,security:viewSecurity,audit:viewAudit};
  (map[view]||viewDashboard)(c).catch(toastErr);
}

// ---------- Живое обновление (WebSocket + запасной опрос) ----------
function startLive(){
  try{
    const proto = location.protocol==='https:'?'wss':'ws';
    const ws=new WebSocket(`${proto}://${location.host}/ws`);
    State.ws=ws;
    // Как только сокет ожил — снимаем запасной опрос. Без этого после первого
    // же обрыва связи опрос жил вечно ПАРАЛЛЕЛЬНО с сокетом, и каждая открытая
    // вкладка постоянно дёргала /state (на 500 ящиках это заметная нагрузка).
    ws.onopen=()=>{ if(State.wsTimer){ clearInterval(State.wsTimer); State.wsTimer=null; } };
    ws.onmessage=(ev)=>{
      let d=null; try{ d=JSON.parse(ev.data); }catch(e){ return; }
      if(d && d.type==='error'){ if(d.message==='unauthorized') sessionGone(); return; }
      try{ onLive(d); }catch(e){}
    };
    ws.onclose=(ev)=>{
      State.ws=null;
      if(State.sessionGone) return;
      if(ev && ev.code===4401){ sessionGone(); return; }
      if(!State.wsTimer) State.wsTimer=setInterval(pollLive, 3500);
      setTimeout(()=>{ if(!State.ws && !State.sessionGone) startLive(); }, 8000);
    };
    ws.onerror=()=>{ try{ws.close();}catch(e){} };
  }catch(e){ if(!State.wsTimer) State.wsTimer=setInterval(pollLive,3500); }
}
// Запасной опрос: лёгкий /live (задания, счётчики, журнал), а не вся сводка /state.
async function pollLive(){ if(State.sessionGone) return; try{ onLive(await api('/live',{bg:true})); }catch(e){} }
function onLive(d){
  const ind=$('#schedInd'); if(ind) ind.innerHTML = d.scheduler_running ? '<span class="status-dot on"></span> Планировщик активен' : '<span class="status-dot off"></span> Планировщик выключен';
  if(State.view==='dashboard') updateDashboardLive(d);
  if(State.view==='jobs') updateJobsLive(d);
  // Живой поток отдаёт последние 40 строк ВСЕХ уровней: при выбранном фильтре
  // он затирал отфильтрованный список, и фильтр выглядел сломанным.
  if(State.view==='logs' && d.logs && !State.logLevel) renderLogLines(d.logs, true);
}

// ===================================================================
//  Дашборд
// ===================================================================
//: Через сколько дней без успешной копии ящик считается «забытым».
const STALE_BACKUP_DAYS = 3;

/** Карточка «ящики, которые требуют внимания» — или null, если всё в порядке. */
function dashboardAttention(accounts){
  const now=Date.now();
  const stale=[], nopass=[], failed=[], broken=[], badpw=[];
  accounts.forEach(a=>{
    if(!a.enabled) return;                       // выключенные — осознанное решение
    if(a.secret_broken){ broken.push(a); return; }
    if(!a.has_password){ nopass.push(a); return; }
    if(a.login_status==='auth_error'){ badpw.push(a); return; }
    const last=a.last_run;
    if(!last || !last.finished_at){ stale.push(a); return; }
    const age=(now - new Date(last.finished_at).getTime())/86400000;
    if(last.status==='failed') failed.push(a);
    else if(age > STALE_BACKUP_DAYS) stale.push(a);
  });
  if(!stale.length && !nopass.length && !failed.length && !broken.length && !badpw.length) return null;
  const line=(items, text, filter)=>items.length
    ? `<div class="kv"><div style="flex:1">${text}: <b>${items.length}</b>
        <div class="muted small">${items.slice(0,5).map(a=>esc(a.name)).join(', ')}${items.length>5?' и др.':''}</div></div>
        ${isMailbox()?'':`<a class="btn small" href="#/accounts" data-filter="${filter}">Показать</a>`}</div>`
    : '';
  const card=h(`<div class="card" style="margin-bottom:16px;border-color:var(--warn)">
    <div class="section-title"><h3>⚠️ Требуют внимания</h3></div>
    ${line(badpw, 'Неверный пароль — сервер не пускает в ящик, копирование не пойдёт', 'badpw')}
    ${line(failed, 'Последняя копия завершилась ошибкой', 'failed')}
    ${line(stale, `Не копировались дольше ${STALE_BACKUP_DAYS} дней`, 'stale')}
    ${line(nopass, 'Включены, но без пароля — копирование не пойдёт', 'nopassword')}
    ${line(broken, 'Пароль не расшифровывается — файл secret.key заменён или утрачен, введите пароль заново', 'badpw')}</div>`);
  card.querySelectorAll('[data-filter]').forEach(a=>a.onclick=()=>{
    Acc.filter=a.dataset.filter||''; Acc.query=''; Acc.shown=ACC_PAGE;
  });
  return card;
}

async function viewDashboard(c){
  const s=await api('/state'); State.engines=s.engines; State.accounts=s.accounts;
  c.innerHTML='';
  const stats=h(`<div class="grid cols-4" style="margin-bottom:16px">
    <div class="card stat-card"><div class="label">📬 Ящиков всего</div><div class="value">${s.totals.accounts}</div><div class="sub">${(()=>{const en=(s.accounts||[]).filter(a=>a.enabled).length; const off=s.totals.accounts-en; return `под копированием: ${en}${off?` · выключено: ${off}`:''}`;})()}</div></div>
    <div class="card stat-card"><div class="label">✉️ Писем в архиве</div><div class="value">${s.totals.messages.toLocaleString('ru-RU')}</div><div class="sub">${esc(s.totals.bytes_h)}</div></div>
    <div class="card stat-card"><div class="label">⚙️ В очереди / работе</div><div class="value" id="dq">${(s.job_counts.queued||0)+(s.job_counts.running||0)}</div><div class="sub">воркеров: ${s.workers}</div></div>
    <div class="card stat-card"><div class="label">💽 Свободно на диске</div><div class="value">${esc(s.disk.free_h)}</div><div class="sub">каталог копий</div></div>
  </div>`);
  c.appendChild(stats);

  // Шифрование включено, а ключа нет: новые письма не сохраняются вовсе
  // (иначе они легли бы на диск открытым текстом). Это важнее всего остального.
  if(s.encryption_blocked){
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--danger)">
      <div class="section-title"><h3>🔒 Копирование остановлено: нет ключа шифрования</h3></div>
      <div>${esc(s.encryption_blocked)}</div>
      <div class="muted small" style="margin-top:6px">Пока ключ недоступен, новые письма не сохраняются, чтобы не лечь
        на диск открытым текстом. Верните файл ключа (параметр «Файл ключа» в разделе «Настройки → Хранилище»)
        и перезапустите службу — либо осознанно выключите шифрование.</div>
      ${isAdmin()?'<a class="btn small" href="#/settings" style="margin-top:10px">Открыть настройки</a>':''}</div>`));
  }
  // Копия вне сервера включена, но давно не обновлялась или завершилась ошибкой.
  if(s.replica_alert){
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--warn)">
      <div class="section-title"><h3>🛰️ Копия вне сервера</h3></div>
      <div>${esc(s.replica_alert)}</div>
      ${isAdmin()?'<a class="btn small" href="#/settings" style="margin-top:10px">Открыть настройки копии</a>':'<div class="muted small" style="margin-top:6px">Сообщите администратору.</div>'}</div>`));
  }
  // Сервер Axigen требует внимания: недоступен, очередь растёт, лицензия кончается…
  if((s.axigen_alerts||[]).length){
    const bad=s.axigen_alerts.some(a=>a.level==='error');
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--${bad?'danger':'warn'})">
      <div class="section-title"><h3>📮 Сервер Axigen</h3></div>
      <ul style="margin:0;padding-left:18px">${s.axigen_alerts.map(a=>`<li>${esc(a.text)}</li>`).join('')}</ul>
      <a class="btn small" href="#/axigen" style="margin-top:10px">Открыть раздел «Сервер Axigen»</a></div>`));
  }
  // Подключённые почтовые серверы (Microsoft 365, Google, Яндекс 360, Zimbra…) требуют внимания
  if((s.mailserver_alerts||[]).length){
    const bad=s.mailserver_alerts.some(a=>a.level==='error');
    const shown=s.mailserver_alerts.slice(0,8);
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--${bad?'danger':'warn'})">
      <div class="section-title"><h3>🔌 Почтовые серверы</h3></div>
      <ul style="margin:0;padding-left:18px">${shown.map(a=>`<li${a.level==='error'?' style="color:var(--danger)"':''}>${esc(a.text)}</li>`).join('')}</ul>
      ${s.mailserver_alerts.length>shown.length?`<div class="small muted" style="margin-top:4px">и ещё ${fmtNum(s.mailserver_alerts.length-shown.length)}</div>`:''}
      <a class="btn small" href="#/mailservers" style="margin-top:10px">Открыть раздел «Почтовые серверы»</a></div>`));
  }
  // Плашка «требуют внимания»: без неё ящик, который давно не копируется или
  // остался без пароля, ничем себя не выдаёт — и это обнаруживается тогда,
  // когда письма уже нужны.
  const attention=dashboardAttention(s.accounts||[]);
  if(attention) c.appendChild(attention);

  const live=h(`<div class="card" style="margin-bottom:16px"><div class="section-title"><h3>🔴 Текущие операции</h3><div class="spacer"></div><span class="muted small">обновляется в реальном времени</span></div><div id="liveJobs"></div></div>`);
  c.appendChild(live);
  updateDashboardLive({active_jobs:s.active_jobs, job_counts:s.job_counts});

  const grid=h(`<div class="grid cols-2">
    <div class="card"><div class="section-title"><h3>📈 Активность</h3><span class="muted small">новые письма за 30 дней</span></div><div class="an-chart" id="chartBox"></div></div>
    <div class="card"><div class="section-title"><h3>📬 ${isMailbox()?'Мой ящик':'Ящики'}</h3>${isMailbox()?'':`<span class="tag">${s.totals.accounts}</span><div class="spacer"></div>${isAdmin()?'<button class="btn sm primary" id="addAcc">+ Добавить</button>':''}`}</div><div id="accList"></div></div>
  </div>`);
  c.appendChild(grid);
  const addAcc=$('#addAcc', grid); if(addAcc) addAcc.onclick=()=>accountModal();
  renderAccountMini(s.accounts, $('#accList'));

  // График активности рисуем тем же движком, что и в разделе «Аналитика»:
  // он сам подгоняет холст под контейнер и учитывает плотность экрана.
  try{
    const st=await api('/stats?days=30');
    const items=(st.series||[]).map(d=>({label:(d.day||'').slice(5), value:d.messages||0}));
    const draw=()=>chartLine($('#chartBox'), items, {height:220});
    draw();
    State.redraw=draw;   // перерисовать при смене темы и размера окна
  }catch(e){}
}

function updateDashboardLive(d){
  const box=$('#liveJobs'); if(!box) return;
  const active=(d.active_jobs||[]).filter(j=>j.status==='running'||j.status==='queued');
  const dq=$('#dq'); if(dq && d.job_counts) dq.textContent=(d.job_counts.queued||0)+(d.job_counts.running||0);
  if(!active.length){ box.innerHTML='<div class="empty" style="padding:24px"><div class="big">😴</div>Сейчас нет активных операций</div>'; return; }
  box.innerHTML='';
  active.forEach(j=>{
    const pct=j.percent||0;
    const row=h(`<div style="padding:10px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
        <span class="badge ${j.status}">${esc(j.status_label)}</span>
        <strong>${esc(j.type_label)}</strong>
        <span class="muted small">#${j.id}${(j.account_name||accName(j.account_id))?(' · '+esc(j.account_name||accName(j.account_id))):''}</span>
        <span class="spacer" style="flex:1"></span>
        <span class="muted small">${esc(j.progress_message||'')}</span>
        ${(j.status==='running'||j.status==='queued') && canTouchJob(j)?`<button class="btn danger sm" data-cancel="${j.id}">Отменить</button>`:''}
      </div>
      <div class="progress running"><span style="width:${pct}%"></span></div>
      <div class="muted small" style="margin-top:4px">${pct}% ${j.progress_total?`· ${j.progress_current}/${j.progress_total}`:''} ${j.bytes_done?('· '+esc(j.bytes_done_h)):''} ${j.speed?('· '+esc(j.speed_h)):''}</div>
    </div>`);
    box.appendChild(row);
  });
  box.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{ try{ await api(`/jobs/${b.dataset.cancel}/cancel`,{method:'POST'}); toast('Отмена запрошена'); }catch(e){toastErr(e);} });
}
function accName(id){ const a=(State.accounts||[]).find(x=>x.id===id); return a?a.name:''; }
function renderAccountMini(accounts, box){
  if(!accounts.length){ box.innerHTML='<div class="empty">Нет ящиков. Добавьте первый.</div>'; return; }
  box.innerHTML='';
  // На дашборде показываем первые 12: рисовать все 500 строк с кнопками
  // бессмысленно — полный список и отбор есть в разделе «Почтовые ящики».
  const MINI_LIMIT=12;
  const rest=accounts.length-MINI_LIMIT;
  accounts.slice(0, MINI_LIMIT).forEach(a=>{
    const st=a.last_run?`<span class="badge ${esc(a.last_run.status)}">${esc(STATUS_LBL[a.last_run.status]||a.last_run.status)}</span>`:'<span class="tag">нет копий</span>';
    const row=h(`<div class="kv" style="align-items:center"><div style="flex:1"><strong>${esc(a.name)}</strong>${a.enabled?'':' <span class="tag">выключен</span>'}<div class="muted small">${esc(a.username)} · ${fmtNum(a.messages)} ${plural(a.messages||0,'письмо','письма','писем')} · ${esc(a.bytes_h)}</div></div>${st}
      <button class="btn sm primary" data-bk="${a.id}" ${a.enabled?'':'disabled'} title="${a.enabled?'Сделать резервную копию этого ящика сейчас':'Ящик выключен — включите его, чтобы делать копии'}">💾 Копия сейчас</button></div>`);
    row.querySelector('[data-bk]').onclick=()=>backupNow(a.id, a.name);
    box.appendChild(row);
  });
  if(rest>0){
    const more=h(`<div class="kv" style="justify-content:center"><a href="#/accounts" class="muted small">…и ещё ${fmtNum(rest)} — открыть «Почтовые ящики»</a></div>`);
    box.appendChild(more);
  }
}


// ===================================================================
//  Ящики
// ===================================================================
// ===================================================================
//  Сотрудники
// ===================================================================
const Emp = { query:'', status:'', offset:0, limit:100 };

async function viewEmployees(c){
  let d;
  const q = new URLSearchParams({limit:Emp.limit, offset:Emp.offset});
  if(Emp.query) q.set('query', Emp.query);
  if(Emp.status) q.set('status', Emp.status);
  try{ d = await api('/employees?'+q.toString()); }
  catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить список сотрудников</div></div>'; return; }
  // Сведения об источнике не критичны: если запрос не прошёл, список всё равно показываем.
  let src=null; try{ src=(await api('/employees/source')).source; }catch(e){}
  const cnt = d.counts||{};
  c.innerHTML='';

  const head=h(`<div class="section-title"><h2 style="margin:0">Сотрудники</h2>
    <span class="tag" title="Всего в справочнике">всего: ${cnt.active+cnt.archived||0}${cnt.archived?` · уволено: ${cnt.archived}`:''}</span>
    <div class="spacer"></div>
    <button class="btn" id="empTemplate" title="Скачать образец файла со списком сотрудников">📄 Образец файла</button>
    <button class="btn" id="empImport" title="Загрузить список сотрудников из файла CSV или Excel">⬆️ Импорт из файла</button>
    <button class="btn" id="empAccTpl" title="Шаблон настроек ящиков, которые заводятся сотрудникам автоматически">🧩 Шаблон ящиков</button>
    <button class="btn" id="empSync" title="Синхронизировать с источником, указанным в настройках">↻ Синхронизировать</button>
    <button class="btn primary" id="empAdd">+ Добавить сотрудника</button></div>`);
  c.appendChild(head);
  $('#empAdd',c).onclick=()=>employeeModal(null);
  $('#empImport',c).onclick=()=>employeeImportModal();
  $('#empSync',c).onclick=()=>employeeSyncNow();
  $('#empAccTpl',c).onclick=()=>employeeAccountTemplateModal();
  $('#empTemplate',c).onclick=()=>{ location.href='/api/employees/template.csv'; };

  // Плашка источника: сразу видно, откуда берётся список и включено ли расписание.
  if(src){
    const isUrl = src.type==='url';
    const target = src.target ? esc(src.target) : '<i>не задан</i>';
    const state = src.configured
      ? (src.sync_enabled ? `<span class="tag ok">по расписанию: ${esc(src.cron||'')}</span>`
                          : '<span class="tag">только по кнопке</span>')
      : '<span class="tag warn">не настроен</span>';
    const bar=h(`<div class="card" style="margin-bottom:14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>${isUrl?'🔗':'📁'} <b>Источник:</b> ${isUrl?'адрес':'файл на сервере'}</span>
      <code class="mono small" style="word-break:break-all">${target}</code>
      ${isUrl&&src.auth?'<span class="tag">с авторизацией</span>':''}
      ${isUrl&&!src.verify_ssl?'<span class="tag warn">без проверки сертификата</span>':''}
      ${state}
      <div class="spacer"></div>
      <button class="btn small" id="empSrcCheck">🔍 Проверить источник</button>
      <button class="btn small ghost" id="empSrcSettings">⚙️ Настроить</button></div>`);
    c.appendChild(bar);
    $('#empSrcCheck',bar).onclick=()=>employeeSourceCheck($('#empSrcCheck',bar));
    $('#empSrcSettings',bar).onclick=()=>{ location.hash='#/settings'; };
  }

  // сводка
  c.appendChild(h(`<div class="an-kpis">
    <div class="kpi accent"><div class="k-label">🧑‍💼 Сотрудников</div><div class="k-value">${fmtNum(cnt.active||0)}</div><div class="k-sub">${cnt.archived?`уволено: ${cnt.archived}`:'активных'}</div></div>
    <div class="kpi"><div class="k-label">📬 С ящиком</div><div class="k-value">${fmtNum(cnt.with_account||0)}</div><div class="k-sub">привязан почтовый ящик</div></div>
    <div class="kpi"><div class="k-label">➖ Без ящика</div><div class="k-value">${fmtNum(cnt.without_account||0)}</div><div class="k-sub">копирование не настроено</div></div>
  </div>`));

  // поиск и фильтр
  const bar=h(`<div class="an-toolbar">
    <input id="empQ" type="text" placeholder="Поиск по ФИО, почте, отделу…" value="${esc(Emp.query)}"
      style="width:320px;padding:8px 11px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elev);color:var(--text)">
    <select id="empStatus">
      <option value="">Все статусы</option>
      <option value="active"${Emp.status==='active'?' selected':''}>Активные</option>
      <option value="archived"${Emp.status==='archived'?' selected':''}>Уволенные (в архиве)</option>
    </select>
    <span class="spacer" style="flex:1"></span>
    <span class="muted small">показано ${d.employees.length} из ${d.total}</span></div>`);
  c.appendChild(bar);
  let t=null;
  $('#empQ',bar).oninput=(e)=>{ clearTimeout(t); t=setTimeout(()=>{ Emp.query=e.target.value.trim(); Emp.offset=0; viewEmployees(c); }, 400); };
  $('#empStatus',bar).onchange=(e)=>{ Emp.status=e.target.value; Emp.offset=0; viewEmployees(c); };

  if(!d.employees.length){
    c.appendChild(h(`<div class="card"><div class="empty"><div class="big">🧑‍💼</div>
      ${Emp.query||Emp.status?'Ничего не найдено по заданному условию.':'Справочник пуст.<br>Добавьте сотрудника вручную или загрузите список из файла.'}</div></div>`));
    return;
  }

  const wrap=h(`<div class="card table-wrap"><table class="tbl"><thead><tr>
    <th>ФИО</th><th>E-mail</th><th>Должность</th><th>Отдел</th><th>Почтовый ящик</th><th>Статус</th><th></th>
  </tr></thead><tbody></tbody></table></div>`);
  const tb=wrap.querySelector('tbody');
  d.employees.forEach(e=>{
    const box = e.account_id
      ? `<span class="tag" title="${esc(e.account_name)}">${esc(trunc(e.account_name||'ящик',20))}</span>
         <span class="badge ${e.account_enabled?'success':'queued'}" title="${e.account_enabled?'Ящик участвует в копировании':'Ящик выключен: задайте пароль и включите его в разделе «Почтовые ящики»'}">${e.account_enabled?'вкл':'выкл'}</span>`
      : '<span class="muted small">—</span>';
    const tr=h(`<tr>
      <td><strong>${esc(e.full_name)}</strong>${e.external_id?`<div class="muted small">таб. № ${esc(e.external_id)}</div>`:''}</td>
      <td class="small">${esc(e.email||'—')}</td>
      <td class="small">${esc(e.position||'—')}</td>
      <td class="small">${esc(e.department||'—')}</td>
      <td>${box}</td>
      <td><span class="badge ${e.status==='active'?'success':'queued'}" ${e.dismissed_at?`title="Уволен ${esc(fmtDate(e.dismissed_at))}"`:''}>${e.status==='active'?'работает':'уволен'}</span>${e.status!=='active'&&e.account_hold_until?`<div class="small muted" title="Архив ящика не удаляется по сроку хранения">🔒 ${e.account_hold_until==='9999-12-31'?'бессрочно':'до '+esc(holdDate(e.account_hold_until))}</div>`:''}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm" data-edit>✏️</button>
        <button class="btn danger sm" data-del>✕</button>
      </td></tr>`);
    tr.querySelector('[data-edit]').onclick=()=>employeeModal(e);
    tr.querySelector('[data-del]').onclick=async()=>{
      if(await confirmDlg('Удалить сотрудника?',
          `Запись «${e.full_name}» будет удалена из справочника. Почтовый ящик и уже сделанные копии писем НЕ удаляются.`)){
        try{ await api(`/employees/${e.id}`,{method:'DELETE'}); toast('Удалено'); viewEmployees(c); }catch(err){ toastErr(err); }
      }
    };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);

  // постраничная навигация
  if(d.total > Emp.limit){
    const pages=Math.ceil(d.total/Emp.limit), cur=Math.floor(Emp.offset/Emp.limit)+1;
    const nav=h(`<div class="an-toolbar" style="margin-top:14px;justify-content:center">
      <button class="btn sm" ${Emp.offset<=0?'disabled':''} data-prev>← Назад</button>
      <span class="muted small">страница ${cur} из ${pages}</span>
      <button class="btn sm" ${Emp.offset+Emp.limit>=d.total?'disabled':''} data-next>Вперёд →</button></div>`);
    nav.querySelector('[data-prev]').onclick=()=>{ Emp.offset=Math.max(0,Emp.offset-Emp.limit); viewEmployees(c); };
    nav.querySelector('[data-next]').onclick=()=>{ Emp.offset+=Emp.limit; viewEmployees(c); };
    c.appendChild(nav);
  }
}

/** Создание и редактирование карточки сотрудника. */
function employeeModal(e){
  const isNew=!e;
  e = e || {full_name:'',email:'',position:'',department:'',phone:'',external_id:'',status:'active',notes:''};
  const body=`
    <div class="form-row"><label>ФИО <span style="color:var(--danger)">*</span></label>
      <input id="e-name" type="text" value="${esc(e.full_name)}" placeholder="Иванов Иван Иванович"></div>
    <div class="grid cols-2">
      <div class="form-row"><label>E-mail</label><input id="e-mail" type="text" value="${esc(e.email||'')}" placeholder="ivanov@example.ru">
        <div class="hint">По адресу сотрудник связывается с почтовым ящиком.</div></div>
      <div class="form-row"><label>Табельный номер</label><input id="e-ext" type="text" value="${esc(e.external_id||'')}" placeholder="1024">
        <div class="hint">Используется для сопоставления при синхронизации.</div></div>
    </div>
    <div class="grid cols-2">
      <div class="form-row"><label>Должность</label><input id="e-pos" type="text" value="${esc(e.position||'')}"></div>
      <div class="form-row"><label>Отдел</label><input id="e-dep" type="text" value="${esc(e.department||'')}"></div>
    </div>
    <div class="grid cols-2">
      <div class="form-row"><label>Телефон</label><input id="e-phone" type="text" value="${esc(e.phone||'')}"></div>
      <div class="form-row"><label>Статус</label><select id="e-status">
        <option value="active"${e.status==='active'?' selected':''}>Работает</option>
        <option value="archived"${e.status==='archived'?' selected':''}>Уволен (архив удерживается)</option></select></div>
    </div>
    <div class="form-row"><label>Заметки</label><input id="e-notes" type="text" value="${esc(e.notes||'')}"></div>
    ${isNew?`<div class="form-row check"><span class="switch"><input type="checkbox" id="e-acc"><span class="track"></span></span>
      <label>Завести почтовый ящик для резервного копирования</label></div>
      <div class="hint">Ящик создаётся <b>выключенным</b> и без пароля — задайте пароль и включите его в разделе «Почтовые ящики».
      Адрес ящика берётся из поля E-mail, сервер — из настроек (раздел «Сотрудники»).</div>`:
      (e.account_id?`<div class="hint">Привязанный ящик: <b>${esc(e.account_name||'')}</b> ${e.account_enabled?'(включён)':'(выключен)'}</div>`:'')}`;
  const m=modal(isNew?'Новый сотрудник':`Сотрудник: ${e.full_name}`, body,
    {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=x=>m.body.querySelector(x);
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=async()=>{
    const payload={full_name:g('#e-name').value.trim(), email:g('#e-mail').value.trim(), position:g('#e-pos').value.trim(),
      department:g('#e-dep').value.trim(), phone:g('#e-phone').value.trim(), external_id:g('#e-ext').value.trim(),
      status:g('#e-status').value, notes:g('#e-notes').value.trim()};
    if(!payload.full_name) return toast('Укажите ФИО','Поле обязательно','warn');
    try{
      if(isNew){
        payload.create_account = g('#e-acc').checked;
        const r=await api('/employees',{method:'POST',body:payload});
        toast('Сотрудник добавлен', r.account_id?'Почтовый ящик создан выключенным':'');
        if(r.warning) toast('Ящик не заведён', r.warning, 'warn', 9000);
      } else {
        const r=await api(`/employees/${e.id}`,{method:'PUT',body:payload});
        if(r.dismissed && r.dismissed.account){
          const d=r.dismissed; const what={final_backup:'последняя копия ящика поставлена в очередь, затем копирование выключится', disabled:'копирование ящика выключено', kept:'копирование ящика продолжается', already_disabled:'копирование ящика уже было выключено'}[d.action]||'';
          toast('Сотрудник уволен', `Архив ящика «${d.account}» удерживается ${d.hold_until==='9999-12-31'?'бессрочно':'до '+holdDate(d.hold_until)}; ${what}.`, 'success', 9000);
        } else if(r.rehired && r.rehired.account){
          toast('Сотрудник снова работает', `Удержание снято${r.rehired.enabled?', копирование ящика снова включено':''}.`);
        } else toast('Сохранено');
      }
      m.close(); route();
    }catch(err){ toastErr(err); }
  };
}

/** Загрузка списка сотрудников файлом. */
function employeeImportModal(){
  const m=modal('Импорт сотрудников из файла', `
    <p class="muted">Поддерживаются файлы <b>CSV</b> и <b>Excel (.xlsx)</b>. Первая строка — заголовки столбцов.
    Распознаются названия: ФИО, E-mail, Должность, Отдел, Телефон, Табельный номер (в любом регистре,
    принимаются и английские варианты).</p>
    <p class="muted small">Сотрудники сопоставляются по табельному номеру, а если его нет — по адресу почты.
    Уже заведённые карточки обновляются, новые добавляются. Никто не удаляется и не выключается.</p>
    <div class="form-row"><label>Файл со списком</label><input id="i-emp" type="file" accept=".csv,.xlsx,.xlsm,text/csv"></div>
    <div class="hint">Не уверены в формате — скачайте «📄 Образец файла» и заполните его.</div>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Загрузить</button>`});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const f=m.body.querySelector('#i-emp').files[0];
    if(!f) return toast('Выберите файл','','warn');
    const btn=m.foot.querySelector('[data-go]'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Загрузка…';
    const fd=new FormData(); fd.append('file', f);
    try{
      const r=await api('/employees/import',{method:'POST',body:fd});
      m.close();
      employeeSyncReport(r);
      route();
    }catch(err){ toastErr(err); btn.disabled=false; btn.textContent='Загрузить'; }
  };
}

/** Показать итоги импорта/синхронизации, включая проблемные строки. */
function employeeSyncReport(r){
  const problems=r.problems||[];
  const rows=problems.length
    ? `<h3 style="margin-top:14px">Строки, которые не удалось разобрать (${problems.length})</h3>
       <div class="table-wrap" style="max-height:260px;overflow:auto"><table class="tbl">
       <thead><tr><th>Строка</th><th>Причина</th></tr></thead><tbody>
       ${problems.map(p=>`<tr><td>${p.row}</td><td class="small">${esc(p.reason)}</td></tr>`).join('')}
       </tbody></table></div>`
    : '<div class="hint" style="margin-top:12px">Все строки файла разобраны без ошибок.</div>';
  modal('Итоги загрузки списка', `
    <div class="an-kpis">
      <div class="kpi accent"><div class="k-label">Добавлено</div><div class="k-value">${fmtNum(r.created||0)}</div><div class="k-sub">новых карточек</div></div>
      <div class="kpi"><div class="k-label">Обновлено</div><div class="k-value">${fmtNum(r.updated||0)}</div><div class="k-sub">существующих</div></div>
      <div class="kpi"><div class="k-label">Ящиков создано</div><div class="k-value">${fmtNum(r.accounts_created||0)}</div><div class="k-sub">выключенными</div></div>
      <div class="kpi"><div class="k-label">Ящиков привязано</div><div class="k-value">${fmtNum(r.accounts_linked||0)}</div><div class="k-sub">уже существовали</div></div>
    </div>
    <div class="muted small">Строк в файле: ${fmtNum(r.total_rows||0)}.${r.skipped_inactive?` Пропущено как не работающие: ${fmtNum(r.skipped_inactive)}.`:''}</div>
    ${(r.warnings||[]).map(w=>`<div class="hint" style="margin-top:10px;border-color:var(--warn)">⚠️ ${esc(w)}</div>`).join('')}
    ${(r.accounts_created?`<div class="hint" style="margin-top:10px">Созданные ящики <b>выключены</b> и без пароля. Чтобы начать копирование, откройте «Почтовые ящики», задайте пароль и включите нужные.</div>`:'')}
    ${rows}`, {wide:true});
}

/** Проверить источник (файл или URL), ничего не записывая в справочник. */
async function employeeSourceCheck(btn){
  const old = btn ? btn.textContent : '';
  if(btn){ btn.disabled=true; btn.textContent='⏳ Проверяем…'; }
  try{
    const r = await api('/employees/source/check', {method:'POST'});
    const src = r.source||{};
    const problems = (r.problems||[]).map(p=>`<tr><td>${p.row}</td><td>${esc(p.reason)}</td></tr>`).join('');
    modal('Проверка источника', `
      <div class="kv"><b>Источник</b><div class="spacer"></div>${src.type==='url'?'адрес':'файл на сервере'}</div>
      <div class="kv"><b>Адрес</b><div class="spacer"></div><code class="mono small" style="word-break:break-all">${esc(src.target||'')}</code></div>
      <div class="kv"><b>Прочитано</b><div class="spacer"></div>${esc(r.filename||'')} · ${fmtBytes(r.bytes||0)}</div>
      <div class="kv"><b>Строк с сотрудниками</b><div class="spacer"></div>${fmtNum(r.rows||0)}</div>
      <div class="kv"><b>Проблемных строк</b><div class="spacer"></div>${fmtNum(r.problem_count||0)}</div>
      ${problems?`<h3 style="margin:14px 0 6px">Проблемные строки</h3>
        <table class="tbl"><thead><tr><th>Строка</th><th>Что не так</th></tr></thead><tbody>${problems}</tbody></table>`:''}
      <div class="hint" style="margin-top:12px">Справочник не изменён — это только проверка. Чтобы применить список,
        нажмите «Синхронизировать».</div>`, {wide:true});
  }catch(e){ toastErr(e); }
  finally{ if(btn){ btn.disabled=false; btn.textContent=old; } }
}

/** Шаблон настроек ящиков, создаваемых сотрудникам: предпросмотр и переход к настройкам. */
async function employeeAccountTemplateModal(){
  let r;
  try{ r = await api('/employees/account-template'); }catch(e){ return toastErr(e); }
  const p = r.preview||{};
  const SEC = {ssl:'SSL/TLS', starttls:'STARTTLS', plain:'без шифрования'};
  const list = a => (a&&a.length) ? esc(a.join(', ')) : '<i>все</i>';
  const keep = p.retention_days<0 ? 'как в общих настройках'
             : (p.retention_days===0 ? 'хранить вечно' : `${p.retention_days} дн.`);
  const vars = (r.placeholders||[]).map(v=>`<code class="mono">{${v}}</code>`).join(' ');
  const m = modal('Шаблон ящиков для сотрудников', `
    <p class="muted">По этому шаблону заводится почтовый ящик, когда у сотрудника
      появляется e-mail — при синхронизации и при добавлении вручную.
      ${r.create_accounts?'':'<b>Сейчас автосоздание ящиков выключено</b> — шаблон не применяется.'}</p>
    <h3 style="margin:14px 0 6px">Пример: Иванов Иван Иванович &lt;ivanov@example.ru&gt;, Менеджер, Отдел продаж</h3>
    <div class="kv"><b>Название</b><div class="spacer"></div>${esc(p.name||'')}</div>
    <div class="kv"><b>Логин</b><div class="spacer"></div><code class="mono">${esc(p.username||'')}</code></div>
    <div class="kv"><b>Сервер</b><div class="spacer"></div>${p.host?esc(p.host):'<span class="tag warn">не задан</span>'}:${p.port} · ${SEC[p.security]||esc(p.security||'')}</div>
    <div class="kv"><b>Состояние</b><div class="spacer"></div>${p.enabled?'<span class="tag ok">включён</span>':'<span class="tag">выключен до ввода пароля</span>'}</div>
    <div class="kv"><b>Копировать папки</b><div class="spacer"></div>${list(p.folder_include)}</div>
    <div class="kv"><b>Пропускать папки</b><div class="spacer"></div>${list(p.folder_exclude)}</div>
    <div class="kv"><b>Срок хранения</b><div class="spacer"></div>${esc(keep)}</div>
    <div class="kv"><b>Расписание копирования</b><div class="spacer"></div>${p.sequence_covers?'<span class="tag ok" title="В «Расписаниях» включено копирование всех ящиков по очереди — новый ящик попадёт в него">общее: все ящики по очереди</span>':(p.schedule_enabled?`<span class="tag ok">${esc(p.schedule_cron||'')}</span>`:'<span class="tag">не создаётся</span>')}</div>
    <div class="kv"><b>Заметка</b><div class="spacer"></div>${esc(p.notes||'—')}</div>
    <div class="kv"><b>Способ копирования</b><div class="spacer"></div>${p.use_axigen_ftp?'<span class="tag ok">через FTP Backup Axigen — без пароля</span>':(p.use_master?'<span class="tag ok">через администратора почты — без пароля</span>':(p.server_id?'<span class="tag ok">через подключение к почтовому серверу — без пароля</span>':'по паролю ящика'))}</div>
    <div class="hint" style="margin-top:12px">${(p.use_axigen_ftp||p.use_master||p.server_id)?'Пароль ящику не нужен: копирование начнётся сразу, если ящик включён.':'Пароль шаблон не задаёт: копирование начнётся только после того, как администратор впишет пароль в карточке ящика.'}</div>
    <div class="hint" style="margin-top:8px">Подстановки в шаблонах названия, логина и заметки: ${vars}</div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Закрыть</button><button class="btn primary" data-s>⚙️ Изменить шаблон</button>'});
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  m.foot.querySelector('[data-s]').onclick=()=>{ m.close(); location.hash='#/settings'; };
}

/** Синхронизация с источником из настроек — файлом или URL (фоновое задание). */
async function employeeSyncNow(){
  try{
    const r=await api('/employees/sync',{method:'POST'});
    toast(r.already?'Синхронизация уже идёт':'Синхронизация запущена','Следите за ходом в разделе «Очередь и задания»');
    location.hash='#/jobs';
  }catch(e){ toastErr(e); }
}

// ---------- Резервное копирование по кнопке ----------
function plural(n, one, few, many){
  const m10=n%10, m100=n%100;
  if(m10===1 && m100!==11) return one;
  if(m10>=2 && m10<=4 && (m100<12 || m100>14)) return few;
  return many;
}

/** Поставить в очередь копирование одного ящика. */
async function backupNow(id, name){
  try{
    await api(`/accounts/${id}/backup`,{method:'POST'});
    toast('Резервное копирование запущено', name?`Ящик «${name}» добавлен в очередь`:'Задание добавлено в очередь');
    location.hash='#/jobs';
  }catch(e){ toastErr(e); }
}

/** Поставить в очередь копирование сразу всех включённых ящиков — параллельно или по очереди. */
function backupAllNow(){
  const accs=State.accounts||[];
  const enabled=accs.filter(a=>a.enabled).length;
  const off=accs.length-enabled;
  if(!enabled) return toast('Нет включённых ящиков','Включите хотя бы один ящик, чтобы запустить копирование','warn');
  const m=modal('Сделать резервную копию всех ящиков?', `
    <p style="margin-top:0">Копирование будет запущено для <b>${fmtNum(enabled)}</b> ${plural(enabled,'включённого ящика','включённых ящиков','включённых ящиков')}.${off?` Выключенные (${fmtNum(off)}) пропускаются.`:''}</p>
    <label class="radio-card"><input type="radio" name="bkm" value="parallel" checked>
      <span><b>Параллельно</b> — по заданию на ящик; одновременно идёт столько копирований, сколько задано
      в «Одновременных заданий». Быстрее всего.</span></label>
    <label class="radio-card"><input type="radio" name="bkm" value="seq">
      <span><b>По очереди, ящик за ящиком</b> — одно задание копирует ящики строго по одному, начиная с тех,
      что копировались давнее всего. Бережнее к почтовому серверу.</span></label>
    <div class="form-row" id="bkPause" style="display:none;margin-top:10px"><label>Пауза между ящиками, секунд</label>
      <input type="number" id="bkPauseIn" min="0" max="3600" value="0"></div>
    <div class="hint">Ящики, по которым копирование уже идёт, повторно запущены не будут. Регулярное копирование
      всех ящиков по очереди в заданное время настраивается в разделе «Расписания».</div>`,
    {footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Запустить</button>'});
  const seq=()=>(m.body.querySelector('input[name=bkm]:checked')||{}).value==='seq';
  m.body.querySelectorAll('input[name=bkm]').forEach(r=>r.onchange=()=>{ m.body.querySelector('#bkPause').style.display=seq()?'':'none'; });
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const body = seq() ? {sequential:true, pause_seconds:parseInt(m.body.querySelector('#bkPauseIn').value||'0',10)||0} : {};
    try{
      const r=await api('/accounts/backup-all',{method:'POST',body});
      m.close();
      if(r.sequential){
        toast(r.already?'Копирование по очереди уже идёт':'Копирование по очереди запущено',
              r.already?`Задание №${r.job_id}`:`Ящиков: ${fmtNum(r.total_enabled||0)} — по одному, задание №${r.job_id}`);
      }else{
        const st=(r.started||[]).length, sk=(r.skipped||[]).length;
        if(st) toast('Резервное копирование запущено',
                     `Ящиков в очереди: ${st}` + (sk?`, пропущено (уже копируются): ${sk}`:''));
        else toast('Новых заданий нет','Все включённые ящики уже копируются','warn');
      }
      location.hash='#/jobs';
    }catch(e){ toastErr(e); }
  };
}

/** Массовая загрузка паролей ящиков из файла «адрес — пароль». */
function passwordImportModal(){
  const m=modal('Загрузить пароли ящиков', `
    <p class="muted">Файл <b>XLSX</b> или <b>CSV</b> из двух колонок: <b>адрес ящика</b> и <b>пароль</b>.
      Заголовки («email», «пароль») можно не писать — формат распознаётся и без них.</p>
    <div class="table-wrap" style="margin:10px 0"><table class="tbl">
      <thead><tr><th>email</th><th>пароль</th></tr></thead>
      <tbody>
        <tr><td class="mono small">ivanov@company.ru</td><td class="mono small">••••••••</td></tr>
        <tr><td class="mono small">petrova@company.ru</td><td class="mono small">••••••••</td></tr>
      </tbody></table></div>
    <div class="form-row"><label>Файл с паролями</label>
      <input id="i-pw" type="file" accept=".xlsx,.xlsm,.csv,.xml,text/csv"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="i-pw-enable" checked><span class="track"></span></span>
      <label>Включить ящики после установки пароля</label></div>
    <div class="hint">Ящик ищется по логину, а если такого логина нет — по адресу сотрудника из
      справочника. Пароли сохраняются в базе в зашифрованном виде: ни в журнал, ни обратно в
      интерфейс они не попадают, а загруженный файл удаляется сразу после разбора.</div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Загрузить</button>'});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const f=m.body.querySelector('#i-pw').files[0];
    if(!f) return toast('Выберите файл','','warn');
    const btn=m.foot.querySelector('[data-go]'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Загрузка…';
    const fd=new FormData(); fd.append('file', f);
    fd.append('enable', m.body.querySelector('#i-pw-enable').checked ? 'true' : 'false');
    try{
      const r=await api('/accounts/import-passwords',{method:'POST',body:fd});
      m.close(); passwordImportReport(r); route();
    }catch(err){ toastErr(err); btn.disabled=false; btn.textContent='Загрузить'; }
  };
}

/** Итог загрузки паролей: что обновилось и что не нашлось. */
function passwordImportReport(r){
  const notFound=(r.not_found||[]).map(e=>`<tr><td class="mono small">${esc(e)}</td></tr>`).join('');
  const problems=(r.problems||[]).map(p=>`<tr><td>${p.row}</td><td>${esc(p.reason)}</td></tr>`).join('');
  modal('Пароли загружены', `
    <div class="an-kpis">
      <div class="kpi accent"><div class="k-label">🔑 Обновлено</div><div class="k-value">${fmtNum(r.updated||0)}</div><div class="k-sub">ящиков с новым паролем</div></div>
      <div class="kpi"><div class="k-label">✅ Включено</div><div class="k-value">${fmtNum(r.enabled||0)}</div><div class="k-sub">были выключены</div></div>
      <div class="kpi"><div class="k-label">❓ Не найдено</div><div class="k-value">${fmtNum(r.not_found_count||0)}</div><div class="k-sub">адресов без ящика</div></div>
    </div>
    <div class="muted small" style="margin-top:10px">Строк в файле: ${fmtNum(r.total_rows||0)}${r.problem_count?` · проблемных: ${fmtNum(r.problem_count)}`:''}.</div>
    ${notFound?`<h3 style="margin-top:14px">Адреса, для которых ящик не найден (${r.not_found_count})</h3>
      <div class="table-wrap" style="max-height:220px;overflow:auto"><table class="tbl"><tbody>${notFound}</tbody></table></div>
      <div class="hint" style="margin-top:8px">Заведите ящики этим сотрудникам (раздел «Сотрудники» → синхронизация) и загрузите файл ещё раз.</div>`:''}
    ${problems?`<h3 style="margin-top:14px">Проблемные строки (${r.problem_count})</h3>
      <div class="table-wrap" style="max-height:220px;overflow:auto"><table class="tbl"><thead><tr><th>Строка</th><th>Что не так</th></tr></thead><tbody>${problems}</tbody></table></div>`:''}`,
    {wide:true});
}

//: Состояние отбора в разделе «Почтовые ящики». Живёт между перерисовками,
//: поэтому после правки ящика список остаётся отфильтрованным так же.
const Acc = { query:'', filter:'', shown:100, sel:new Set() };
//: По сколько строк дорисовывать в списке ящиков: после синхронизации
//: сотрудников их бывают сотни, и рисовать всё сразу незачем.
const ACC_PAGE = 100;

/** Подходит ли ящик под текущий отбор. */
// Сколько часов без удачной копии считаем «давно не копировался» (как на дашборде).
const ACC_STALE_HOURS = STALE_BACKUP_DAYS * 24;
function hoursSince(iso){ if(!iso) return Infinity; const t=Date.parse(iso); return isNaN(t)?Infinity:(Date.now()-t)/3600000; }
function accBadPassword(a){ return a.secret_broken || a.login_status==='auth_error' || a.login_status==='secret_broken'; }
function accNoBackup(a){ return !a.last_backup_at && !a.messages; }
function accStale(a){ return a.enabled && !accNoBackup(a) && hoursSince(a.last_backup_at)>ACC_STALE_HOURS; }
function accFailed(a){ return !!(a.last_run ? a.last_run.status==='failed' : a.last_backup_status==='failed'); }

function accountMatches(a){ return accountMatchesBy(a, Acc.filter, Acc.query); }
/** Подходит ли ящик под отбор f и строку поиска query (для списка и групповых действий). */
function accountMatchesBy(a, f, query){
  if(f==='enabled' && !a.enabled) return false;
  if(f==='disabled' && a.enabled) return false;
  if(f==='nopassword' && a.has_password) return false;
  if(f==='ready' && !(a.enabled && a.has_password)) return false;
  if(f==='badpw' && !accBadPassword(a)) return false;
  if(f==='connerr' && a.login_status!=='conn_error') return false;
  if(f==='unchecked' && (a.login_status || !a.has_password)) return false;
  if(f==='nobackup' && !accNoBackup(a)) return false;
  if(f==='stale' && !accStale(a)) return false;
  if(f==='failed' && !accFailed(a)) return false;
  if(f==='dismissed' && !a.dismissed_at) return false;
  if(f==='hold' && !a.on_hold) return false;
  if(f==='holdexpired' && !a.hold_expired) return false;
  if(f==='quarantine' && !a.quarantines) return false;
  const q=(query||'').trim().toLowerCase();
  if(!q) return true;
  return [a.name, a.username, a.host].some(v=>String(v||'').toLowerCase().includes(q));
}

/** Итог последней проверки входа (пароля) — для колонки «Вход». */
function loginCell(a){
  const when = a.login_checked_at ? ` title="Проверено ${esc(fmtDate(a.login_checked_at))}${a.login_error?': '+esc(a.login_error):''}"` : '';
  if(a.secret_broken || a.login_status==='secret_broken')
    return `<span class="tag err"${when}>пароль не читается</span>`;
  if(!a.has_password) return '<span class="tag warn">нет пароля</span>';
  // ящик копирует администратор Axigen через FTP Backup — пароль сотрудника не нужен
  if(a.auth_type==='axigen_ftp' && !['auth_error','conn_error'].includes(a.login_status))
    return `<span class="tag ok" title="Копируется через FTP Backup Axigen — без пароля ящика${a.imap_login?'; пароль для IMAP задан':''}">📮 FTP Backup</span>`;
  switch(a.login_status){
    case 'ok': return `<span class="tag ok"${when}>✓ пароль верный</span>`;
    case 'auth_error': return `<span class="tag err"${when}>✗ неверный пароль</span>`;
    case 'conn_error': return `<span class="tag warn"${when}>нет связи</span>`;
    case 'no_password': return `<span class="tag warn"${when}>нет пароля</span>`;
    default: return '<span class="tag" title="Вход в ящик ещё не проверялся — нажмите «Проверить пароли»">не проверялся</span>';
  }
}

/** Даты резервных копий ящика — для колонки «Резервные копии». */
function fmtDay(iso){ if(!iso) return '—'; try{ return new Date(iso).toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric'}); }catch(e){ return iso; } }
/** Колонка «Резервные копии»: последняя копия — строкой, первая (только дата), число писем и объём — второй;
 *  extra (ссылка «история») дописывается в конец, чтобы строка ящика не вырастала ещё на одну. */
function backupCell(a, extra=''){
  if(accNoBackup(a)){
    const lr=a.last_run;
    const tail = lr && lr.status==='failed' ? `<div class="small" style="color:var(--danger)">попытка ${esc(fmtDate(lr.finished_at||lr.started_at))} не удалась</div>` : '';
    return `<span class="tag warn">копий нет</span>${extra?` <span class="small">${extra}</span>`:''}${tail}`;
  }
  const st=a.last_backup_status||'';
  const stale = accStale(a) ? ` <span class="tag warn" title="Удачной копии не было больше ${ACC_STALE_HOURS} ч">давно</span>` : '';
  const lr=a.last_run;
  const failedLater = lr && lr.status==='failed' && (!a.last_backup_at || (lr.started_at||'')>a.last_backup_at)
    ? `<div class="small" style="color:var(--danger)" title="${esc(lr.detail||'')}">последняя попытка ${esc(fmtDate(lr.finished_at||lr.started_at))} — ошибка</div>` : '';
  return `<div class="small"><span class="nowrap">последняя: <b>${esc(fmtDate(a.last_backup_at))}</b></span>${st==='partial'?' <span class="badge partial">частично</span>':''}${stale}</div>
    <div class="small muted"><span class="nowrap">первая: ${esc(fmtDay(a.first_backup_at))}</span> · <span class="nowrap">писем ${fmtNum(a.messages)} (${esc(a.bytes_h||'')})</span>${extra?' · '+extra:''}</div>${failedLater}`;
}

async function viewAccounts(c){
  const accs=await api('/accounts'); State.accounts=accs;
  c.innerHTML='';
  // Администратор — всё; оператор — копирование, проверки и групповые действия
  // копирования (без добавления, выключения и удаления ящиков).
  const admin=isAdmin(), staff=isStaff();
  const tab = staff && /[?&]bulk\b/.test(location.hash) ? 'bulk' : 'list';
  const enabledCnt=accs.filter(a=>a.enabled).length;
  const disabledCnt=accs.length-enabledCnt;
  const noPwCnt=accs.filter(a=>!a.has_password).length;
  // выбор ящиков живёт между перерисовками, но ящики, которых уже нет, из него уходят
  const known=new Set(accs.map(a=>a.id));
  Acc.sel.forEach(id=>{ if(!known.has(id)) Acc.sel.delete(id); });
  const head=h(`<div class="section-title"><h2 style="margin:0">Почтовые ящики</h2>
    <span class="tag" title="Всего ящиков в системе">всего: ${accs.length}${disabledCnt?` · выключено: ${disabledCnt}`:''}</span>
    <div class="spacer"></div>
    ${admin?'<button class="btn" id="addMany" title="Завести много ящиков сразу из списка адресов">+ Списком</button><button class="btn primary" id="add">+ Добавить ящик</button>':''}</div>`);
  c.appendChild(head);
  if(staff){
    // Вкладки и действия сразу со всеми ящиками — одной строкой.
    const tabs=h(`<div class="tabs" role="tablist">
      <a href="#/accounts" class="${tab==='list'?'active':''}" role="tab">📬 Список ящиков</a>
      <a href="#/accounts?bulk" class="${tab==='bulk'?'active':''}" role="tab">🧰 Групповые действия${Acc.sel.size?` <span class="count">${fmtNum(Acc.sel.size)}</span>`:''}</a>
      <div class="spacer"></div>
      <div class="tab-actions">
        <button class="btn sm" id="pwCheck" title="Войти в каждый ящик и сразу выйти — найти ящики с неверным паролем">🔐 Проверить пароли</button>
        ${admin?'<button class="btn sm" id="pwImport" title="Массово проставить пароли ящикам из файла «адрес — пароль»">🔑 Загрузить пароли</button>':''}
        <button class="btn sm" id="bkAll" ${enabledCnt?'':'disabled'} title="Поставить в очередь резервное копирование всех включённых ящиков — параллельно или по очереди">💾 Копия всех сейчас</button>
        ${admin?`<button class="btn sm danger" id="bkZero" ${enabledCnt?'':'disabled'} title="Копия всех ящиков с нуля: прежние копии всех включённых ящиков уходят в карантин, письма скачиваются заново">🧨 Копия всех с нуля</button>`:''}
      </div></div>`);
    c.appendChild(tabs);
    const on=(sel, fn)=>{ const el=$(sel,c); if(el) el.onclick=fn; };
    on('#add', ()=>accountModal());
    on('#addMany', ()=>bulkCreateModal());
    on('#bkAll', ()=>backupAllNow());
    on('#bkZero', ()=>rebuildAllModal());
    on('#pwImport', ()=>passwordImportModal());
    on('#pwCheck', ()=>checkLoginsModal(accs));
  }
  if(tab==='bulk') return viewBulk(c, accs);
  const progress=h('<div id="pwProgress" style="display:none" class="hint"></div>');
  c.appendChild(progress);
  if(State.checkLoginsJob) watchCheckLogins(State.checkLoginsJob);
  if(!accs.length){ c.appendChild(h(`<div class="card"><div class="empty"><div class="big">📭</div>Пока нет ни одного ящика.<br>${admin?'Нажмите «Добавить ящик», чтобы начать.':'Ящики заводит администратор.'}</div></div>`)); return; }

  // Отбор: ящиков бывает несколько сотен (их заводит синхронизация сотрудников),
  // и найти среди них выключенные, оставшиеся без пароля или без копий глазами нереально.
  const opts=accFilterOptions(accs);
  const bar=h(`<div class="an-toolbar">
    <input type="text" id="accQ" placeholder="Поиск по названию, логину, серверу…" style="flex:0 1 320px;min-width:200px" value="${esc(Acc.query)}">
    <select id="accF">${opts.map(([v,t])=>`<option value="${v}" ${v===Acc.filter?'selected':''}>${esc(t)}</option>`).join('')}</select>
    <button class="btn small ghost" id="accReset" ${(Acc.query||Acc.filter)?'':'style="display:none"'}>Сбросить</button>
    <div class="spacer"></div><span class="muted small" id="accCount"></span></div>`);
  c.appendChild(bar);
  // Панель выбранных ящиков: частые действия — сразу, остальное — в «Групповых действиях».
  const selBar=h(`<div class="sel-bar" id="selBar" style="display:none"></div>`);
  if(staff) c.appendChild(selBar);

  const wrap=h(`<div class="card table-wrap"><table class="tbl acc-tbl"><thead><tr>
    ${staff?'<th class="chk"><input type="checkbox" id="selAll" aria-label="Выбрать все ящики по отбору" title="Выбрать все ящики по отбору"></th>':''}
    <th>Название и сервер</th><th>Логин</th><th>Вход</th><th>Резервные копии</th><th>Статус</th><th></th></tr></thead><tbody></tbody></table></div>`);
  const tb=wrap.querySelector('tbody');
  const cols=staff?7:6;
  let lastClicked=null;
  const renderSel=()=>{
    if(!staff) return;
    const shown=accs.filter(accountMatches);
    const inShown=shown.filter(a=>Acc.sel.has(a.id)).length;
    const all=$('#selAll',wrap);
    if(all){ all.checked=shown.length>0 && inShown===shown.length; all.indeterminate=inShown>0 && inShown<shown.length; }
    const n=Acc.sel.size;
    const tabCount=$('.tabs .count', c);
    if(tabCount) tabCount.textContent=fmtNum(n);
    else if(n){ const t=$$('.tabs a',c)[1]; if(t) t.insertAdjacentHTML('beforeend',` <span class="count">${fmtNum(n)}</span>`); }
    if(!n){ selBar.style.display='none'; return; }
    selBar.style.display='';
    const hidden=n-inShown;
    selBar.innerHTML=`<b>Выбрано: ${fmtNum(n)}</b>
      ${hidden>0?`<span class="muted small" title="Отмеченные ящики, которые сейчас скрыты отбором">(скрыто отбором: ${fmtNum(hidden)})</span>`:''}
      ${inShown<shown.length?`<button class="btn sm ghost" data-s="all" title="Отметить все ящики, попавшие под отбор">Выбрать все (${fmtNum(shown.length)})</button>`:''}
      <button class="btn sm ghost" data-s="none">Снять выбор</button>
      <div class="spacer"></div>
      <button class="btn sm" data-q="backup" title="Поставить копирование выбранных ящиков в очередь (параллельно)">💾 Копия</button>
      <button class="btn sm" data-q="backup_sequence" title="Одно задание: выбранные ящики копируются строго по одному">🔁 По очереди</button>
      ${admin?`<button class="btn sm" data-q="enable" title="Включить копирование выбранных ящиков">▶️ Включить</button>
      <button class="btn sm" data-q="disable" title="Выключить копирование выбранных ящиков">⏸️ Выключить</button>`:''}
      <button class="btn sm" data-q="export_list" title="Список выбранных ящиков в Excel">📊 Excel</button>
      <button class="btn sm primary" data-q="more" title="Все групповые действия с выбранными ящиками">🧰 Ещё действия…</button>`;
    selBar.querySelectorAll('[data-s]').forEach(b=>b.onclick=()=>{
      if(b.dataset.s==='all') shown.forEach(a=>Acc.sel.add(a.id)); else Acc.sel.clear();
      render();
    });
    selBar.querySelectorAll('[data-q]').forEach(b=>b.onclick=()=>{
      const q=b.dataset.q, ids=[...Acc.sel];
      if(q==='more'){ Bulk.source='selected'; location.hash='#/accounts?bulk'; return; }
      if(q==='export_list') return exportAccountList(ids);
      bulkDialog(q, ids);
    });
  };
  const render=()=>{
    tb.innerHTML='';
    const shown=accs.filter(accountMatches);
    $('#accCount',bar).textContent=`показано ${Math.min(shown.length, Acc.shown)} из ${shown.length}`
      + (shown.length!==accs.length?` (всего ${accs.length})`:'');
    $('#accReset',bar).style.display=(Acc.query||Acc.filter)?'':'none';
    renderSel();
    if(!shown.length){
      tb.appendChild(h(`<tr><td colspan="${cols}" class="empty">Под отбор не попал ни один ящик</td></tr>`));
      return;
    }
    const page=shown.slice(0, Acc.shown);
    page.forEach((a, i)=>{
      // «Копируется» — только если копирование действительно может пройти:
      // включённый ящик без пароля или с отвергнутым паролем не копируется.
      const state = !a.enabled ? '<span class="tag">выключен</span>'
        : (!a.has_password || a.secret_broken) ? '<span class="tag warn" title="Ящик включён, но без пароля копирование не пойдёт">не копируется</span>'
        : a.login_status==='auth_error' ? '<span class="tag err" title="Сервер отверг пароль — исправьте его">не копируется</span>'
        : '<span class="tag ok">копируется</span>';
      const hold = holdTags(a) + (a.quarantines ? ` <span class="tag" title="Прежние копии после копирования «с нуля»${admin?' (меню ⋯ → «Прежние копии»)':' — разбирает администратор'}: ${a.quarantines}">🧺 прежние копии</span>` : '');
      const tr=h(`<tr class="${Acc.sel.has(a.id)?'selected':''}">
        ${staff?`<td class="chk"><input type="checkbox" data-sel ${Acc.sel.has(a.id)?'checked':''} aria-label="Выбрать ящик ${esc(a.name)}"></td>`:''}
        <td><strong>${esc(a.name)}</strong><div class="small muted nowrap">${esc(a.host)}:${a.port} · ${esc(a.security)}${a.auth_type==='oauth2'?' · OAuth2':(a.auth_type==='master'?' · через администратора':'')}</div></td>
        <td class="small login">${esc(a.username).replace('@','<wbr>@')}</td>
        <td>${loginCell(a)}</td>
        <td class="bk">${backupCell(a, '<a href="javascript:void(0)" data-runs>история</a>')}</td>
        <td>${state}${hold?'<div style="margin-top:4px">'+hold+'</div>':''}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm" data-test title="Проверить подключение к ящику"><span class="ic-only">🔌</span><span class="lbl">Проверить</span></button>
          <button class="btn sm primary" data-bk ${a.enabled?'':'disabled'} title="${a.enabled?'Сделать резервную копию этого ящика сейчас':'Ящик выключен — включите его, чтобы делать копии'}">💾<span class="lbl"> Копия сейчас</span></button>
          <button class="btn sm" data-menu>⋯</button>
        </td></tr>`);
      tr.querySelector('[data-test]').onclick=()=>testAccount(a.id);
      tr.querySelector('[data-bk]').onclick=()=>backupNow(a.id, a.name);
      tr.querySelector('[data-menu]').onclick=()=>accountMenu(a);
      tr.querySelector('[data-runs]').onclick=()=>runsModal(a);
      const cb=tr.querySelector('[data-sel]');
      if(cb) cb.onclick=(e)=>{
        // Shift+щелчок отмечает диапазон — как в проводнике и почтовых клиентах.
        if(e.shiftKey && lastClicked!==null){
          const from=Math.min(lastClicked, i), to=Math.max(lastClicked, i);
          page.slice(from, to+1).forEach(x=>{ if(cb.checked) Acc.sel.add(x.id); else Acc.sel.delete(x.id); });
          lastClicked=i; render(); return;
        }
        lastClicked=i;
        if(cb.checked) Acc.sel.add(a.id); else Acc.sel.delete(a.id);
        tr.classList.toggle('selected', cb.checked);
        renderSel();
      };
      tb.appendChild(tr);
    });
    if(shown.length>page.length){
      const rest=shown.length-page.length;
      const more=h(`<tr><td colspan="${cols}" style="text-align:center">
        <button class="btn small" id="accMore">Показать ещё ${Math.min(ACC_PAGE, rest)} из ${fmtNum(rest)}</button></td></tr>`);
      more.querySelector('#accMore').onclick=()=>{ Acc.shown+=ACC_PAGE; render(); };
      tb.appendChild(more);
    }
  };
  State.accountsShown=()=>accs.filter(accountMatches);
  let accTimer=null;
  $('#accQ',bar).oninput=e=>{
    Acc.query=e.target.value; Acc.shown=ACC_PAGE;
    // Задержка: без неё каждая буква пересобирала до 500 строк таблицы.
    clearTimeout(accTimer); accTimer=setTimeout(render, 200);
  };
  $('#accF',bar).onchange=e=>{ Acc.filter=e.target.value; Acc.shown=ACC_PAGE; render(); };
  $('#accReset',bar).onclick=()=>{ Acc.query=''; Acc.filter=''; Acc.shown=ACC_PAGE; $('#accQ',bar).value=''; $('#accF',bar).value=''; render(); };
  const selAll=$('#selAll',wrap);
  if(selAll) selAll.onclick=()=>{
    const shown=accs.filter(accountMatches);
    const allIn=shown.length && shown.every(a=>Acc.sel.has(a.id));
    shown.forEach(a=>{ if(allIn) Acc.sel.delete(a.id); else Acc.sel.add(a.id); });
    render();
  };
  c.appendChild(wrap);
  render();
}

/** Варианты отбора ящиков (с количеством) — общие для списка и групповых действий. */
function accFilterOptions(accs){
  const cnt=(fn)=>accs.filter(fn).length;
  const enabledCnt=cnt(a=>a.enabled);
  return [['','Все ящики'+` (${accs.length})`],
          ['enabled','Только включённые'+` (${enabledCnt})`],
          ['disabled','Только выключенные'+` (${accs.length-enabledCnt})`],
          ['ready','Готовые к копированию'+` (${cnt(a=>a.enabled&&a.has_password)})`],
          ['badpw','С неправильным паролем'+` (${cnt(accBadPassword)})`],
          ['nopassword','Без пароля'+` (${cnt(a=>!a.has_password)})`],
          ['connerr','Нет связи при проверке входа'+` (${cnt(a=>a.login_status==='conn_error')})`],
          ['unchecked','Пароль не проверялся'+` (${cnt(a=>!a.login_status&&a.has_password)})`],
          ['nobackup','Без резервных копий'+` (${cnt(accNoBackup)})`],
          ['stale',`Копия старше ${ACC_STALE_HOURS} ч`+` (${cnt(accStale)})`],
          ['failed','Последняя копия с ошибкой'+` (${cnt(accFailed)})`],
          ['dismissed','Уволенные сотрудники'+` (${cnt(a=>!!a.dismissed_at)})`],
          ['hold','Архив удерживается'+` (${cnt(a=>a.on_hold)})`],
          ['holdexpired','Удержание истекло'+` (${cnt(a=>a.hold_expired)})`],
          ['quarantine','Есть прежние копии (после «с нуля»)'+` (${cnt(a=>a.quarantines)})`]];
}

/** «Проверить пароли»: войти в ящики и сразу выйти, итог — в колонке «Вход». */
function checkLoginsModal(accs){
  const withPw=accs.filter(a=>a.has_password&&!a.secret_broken);
  const enabled=withPw.filter(a=>a.enabled);
  const shown=(State.accountsShown?State.accountsShown():accs).filter(a=>a.has_password&&!a.secret_broken);
  const filtered=(Acc.query||Acc.filter) && shown.length!==withPw.length;
  const m=modal('Проверить пароли ящиков', `
    <p class="muted">Сервис по очереди войдёт в каждый ящик (IMAP LOGIN) и сразу выйдет — письма не
      скачиваются. Итог появится в колонке «Вход», а отбор «С неправильным паролем» покажет, какие
      ящики не смогут копироваться.</p>
    <div class="form-row"><label>Какие ящики проверить</label>
      <select id="ck-scope">
        <option value="all">Все ящики с паролем (${fmtNum(withPw.length)})</option>
        <option value="enabled">Только включённые (${fmtNum(enabled.length)})</option>
        ${filtered?`<option value="shown" selected>Только показанные сейчас в отборе (${fmtNum(shown.length)})</option>`:''}
      </select></div>
    <div class="hint" style="border-color:var(--warn)">Каждый неверный пароль — это неудачный вход на
      почтовом сервере. Если там включена защита от перебора (например fail2ban), большое число
      неверных паролей подряд может временно заблокировать адрес сервера архива. Проверка идёт не
      быстрее ${4} ящиков одновременно — так же, как ночное копирование.</div>`,
    {footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Проверить</button>'});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const scope=m.body.querySelector('#ck-scope').value;
    const body = scope==='enabled' ? {only_enabled:true}
               : scope==='shown' ? {account_ids: shown.map(a=>a.id)} : {};
    try{
      const r=await api('/accounts/check-logins',{method:'POST',body});
      m.close();
      toast(r.already?'Проверка уже идёт':'Проверка паролей запущена', 'Результат появится в колонке «Вход».');
      State.checkLoginsJob=r.job_id;
      watchCheckLogins(r.job_id);
    }catch(e){ toastErr(e); }
  };
}

/** Показывать ход проверки паролей и обновить таблицу, когда она закончится. */
function watchCheckLogins(jobId){
  clearInterval(State.checkLoginsTimer);
  const tick=async()=>{
    const box=$('#pwProgress');
    let j;
    try{ j=await api('/jobs/'+jobId,{bg:true}); }catch(e){ clearInterval(State.checkLoginsTimer); State.checkLoginsJob=null; return; }
    if(box){
      box.style.display='';
      box.textContent = j.status==='queued' ? '🔐 Проверка паролей ждёт своей очереди…'
        : `🔐 Проверка паролей: ${fmtNum(j.progress_current||0)} из ${fmtNum(j.progress_total||0)}…`;
    }
    if(['success','failed','cancelled','partial'].includes(j.status)){
      clearInterval(State.checkLoginsTimer); State.checkLoginsJob=null;
      const summary=(j.result&&j.result.summary)||j.error||'';
      toast('Проверка паролей завершена', summary, j.status==='success'?'success':'warn', 9000);
      if(State.view==='accounts'){
        if((j.result&&j.result.counts&&j.result.counts.auth_error)>0){ Acc.filter='badpw'; Acc.shown=ACC_PAGE; }
        route();
      }
    }
  };
  State.checkLoginsTimer=setInterval(tick, 1500);
  tick();
}

/** История копирования ящика: даты прогонов и их итоги. */
async function runsModal(a){
  let runs;
  try{ runs=await api(`/accounts/${a.id}/runs?limit=200`); }catch(e){ return toastErr(e); }
  const rows=runs.map(r=>`<tr>
      <td class="small">${esc(fmtDate(r.started_at))}</td>
      <td class="small">${esc(fmtDate(r.finished_at))}</td>
      <td><span class="badge ${esc(r.status||'')}">${esc(STATUS_LBL[r.status]||r.status||'—')}</span></td>
      <td class="small">${fmtNum(r.messages_new)} (${esc(r.bytes_new_h)})</td>
      <td class="small">${fmtNum(r.errors)}</td>
      <td class="small muted" style="max-width:320px;overflow:hidden;text-overflow:ellipsis" title="${esc(r.detail)}">${esc(r.detail)}</td>
    </tr>`).join('');
  modal(`Резервные копии: ${a.name}`, `
    <p class="muted">Первая копия: <b>${esc(fmtDate(a.first_backup_at))}</b> · последняя удачная:
      <b>${esc(fmtDate(a.last_backup_at))}</b> · писем в копии: <b>${fmtNum(a.messages)}</b> (${esc(a.bytes_h||'')}).
      Копия инкрементная: каждый прогон докачивает только новые письма, поэтому ниже —
      даты прогонов и сколько писем каждый добавил.</p>
    ${runs.length?`<div class="table-wrap"><table class="tbl"><thead><tr><th>Начало</th><th>Окончание</th><th>Итог</th><th>Новых писем</th><th>Ошибок</th><th>Подробности</th></tr></thead><tbody>${rows}</tbody></table></div>`
      :'<div class="empty">Прогонов копирования ещё не было.</div>'}
    <p class="small muted">Хранится история последних прогонов (параметр «Хранить записей истории»).</p>`,
    {wide:true});
}

/** Карантинные копии: что осталось на диске после пересоздания «с нуля». */
async function quarantineModal(a, changed=false){
  let d;
  try{ d=await api(`/accounts/${a.id}/quarantines`); }catch(e){ return toastErr(e); }
  const list=d.quarantines||[];
  const job=d.job;
  // «account_9_old_20260921_031500» → «21.09.2026 03:15» — когда сделана копия «с нуля»
  const qWhen=(name)=>{ const st=/_old_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/.exec(name||'');
    return st?`${st[3]}.${st[2]}.${st[1]} ${st[4]}:${st[5]}`:''; };
  const cmpCell=(q)=>{
    const c=q.check;
    if(!c) return '<span class="tag warn" title="Неизвестно, есть ли в прежней копии письма, которых нет в новой">не сравнивалась</span>';
    const blocked=c.unique_unreadable||0;
    return `<div class="small">есть в новой: <b>${fmtNum(c.identical)}</b>${c.other_version?` · <span title="Тот же Message-ID, тема, отправитель и дата, но другие байты: сервер пересобрал заголовки того же письма">другой вариант: ${fmtNum(c.other_version)}</span>`:''}${c.outside_retention?` · <span title="Есть только здесь, но старше срока хранения ящика (${fmtNum(c.retention_days||0)} дн.) — ночная очистка удалила бы их из архива и так">старше срока хранения: ${fmtNum(c.outside_retention)}</span>`:''}</div>
      <div style="margin:3px 0">${c.unique?`<span class="tag warn" title="Этих писем нет в новой копии: удалены на сервере, лежат в исключённых или неоткрывающихся папках либо больше лимита размера">только здесь: ${fmtNum(c.unique)} · ${esc(c.unique_bytes_h||'')}</span>`
        :'<span class="tag ok">всё есть в новой копии</span>'}${c.unreadable?` <span class="tag err" title="Файлы не прочитались (нет ключа шифрования или файл повреждён)">не прочитано: ${fmtNum(c.unreadable)}</span>`:''}</div>
      ${blocked?`<div class="small muted">из папок, которые сейчас не открываются на сервере: ${fmtNum(blocked)} — вернутся, когда папки снова начнут копироваться</div>`:''}
      ${c.unique&&!c.new_copy_complete?'<div class="small muted">новая копия после «с нуля» ещё не прочитала все папки — вернуть письма можно после полного копирования</div>':''}
      <div class="small muted">сравнено ${esc(fmtDate(c.checked_at))}${c.rescued?` · возвращено в архив: ${fmtNum(c.rescued)}`:''}</div>`;
  };
  const rows=list.map(q=>{
    const c=q.check;
    const canRescue=c&&c.unique>(c.unique_unreadable||0);
    const rescueBlock=!job&&canRescue&&!c.new_copy_complete
      ? ' disabled title="После копии «с нуля» копирование ещё не прочитало все папки ящика — иначе письма непрочитанных папок попали бы в архив дважды"' : '';
    const when=qWhen(q.name);
    return `<tr><td>${when?`<div class="nowrap"><b>${esc(when)}</b></div>`:''}<div class="mono small muted q-name">${esc(q.name)}</div></td>
    <td class="small nowrap">${esc(q.bytes_h)}<div class="muted">файлов: ${fmtNum(q.files)}</div></td><td class="q-cmp">${cmpCell(q)}</td>
    <td><div class="q-acts">
      <button class="btn sm" data-cmp="${esc(q.path)}" ${job?'disabled':''} title="Сравнить с новой копией: какие письма есть только здесь (фоновое задание, ничего не меняет)">🔍 Сравнить</button>
      ${canRescue?`<button class="btn sm" data-rescue="${esc(q.path)}" data-n="${fmtNum(c.unique-(c.unique_unreadable||0))}" ${job?'disabled':''}${rescueBlock} title="Вернуть в архив ящика письма, которых нет в новой копии">🛟 Вернуть ${fmtNum(c.unique-(c.unique_unreadable||0))}</button>`:''}
      <button class="btn danger sm" data-del="${esc(q.path)}" ${a.on_hold?'disabled title="Архив удерживается — прежние копии не удаляются"':(job?'disabled title="Идёт задание с прежними копиями — дождитесь его окончания"':'')}>Удалить</button></div></td></tr>`;
  }).join('');
  // Письма «только здесь» — списком, чтобы было видно, что именно потеряется при удалении.
  const samples=list.filter(q=>q.check&&q.check.unique&&(q.check.samples||[]).length).map(q=>{
    const c=q.check, sm=c.samples||[];
    const when=qWhen(q.name);
    return `<details class="q-samples"><summary>Письма только в прежней копии ${when?`от ${esc(when)}`:`«${esc(q.name)}»`} — ${fmtNum(c.unique)}${c.unique>sm.length?` (показаны первые ${sm.length})`:''}</summary>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>Дата</th><th>Папка</th><th>От кого</th><th>Тема</th></tr></thead><tbody>
      ${sm.map(x=>`<tr><td class="small nowrap">${esc(fmtDate(x.date))}</td><td class="small">${esc(x.folder)}</td><td class="small">${esc(x.from)}</td><td class="small">${esc(x.subject||'(без темы)')}</td></tr>`).join('')}
      </tbody></table></div></details>`;
  }).join('');
  let reopening=false;
  const JOBWHAT={quarantine_rescue:'возврат писем в архив', quarantine_check:'сравнение с новой копией', cleanup:'удаление прежней копии'};
  const banner = job ? `<div class="hint" style="margin-bottom:12px">⏳ ${job.status==='queued'?'В очереди':'Идёт'} ${JOBWHAT[job.type]||'задание'} —
      <a href="javascript:void(0)" data-jobinfo="${job.id}">задание №${job.id}</a>. Итог появится здесь.</div>` : '';
  const m=modal(`Прежние копии: ${a.name}`, list.length ? `
    <p class="muted" style="margin-top:0">При пересоздании копии «с нуля» прежние письма не удаляются, а переносятся
      в карантин. Письма, которых на сервере уже нет, остаются только здесь. <b>🔍 Сравнить</b> покажет, есть ли такие;
      <b>🛟 Вернуть</b> запишет их в архив ящика (в те же папки) — после этого карантин можно удалить без потерь.</p>
    ${banner}
    <div class="table-wrap"><table class="tbl q-tbl">
      <thead><tr><th>Прежняя копия</th><th>Размер</th><th>Сравнение с новой копией</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>${samples}`
    : '<div class="empty">Карантинных копий нет — на диске только актуальная копия.</div>',
    // после сравнения, возврата или удаления список ящиков (отметка «прежние копии», счётчики) устарел
    {wide:true, onClose:()=>{ if(changed && !reopening && State.view==='accounts') route(); }});
  const reopen=()=>{ reopening=true; m.close(); quarantineModal(a, true); };
  const ji=m.body.querySelector('[data-jobinfo]'); if(ji) ji.onclick=()=>jobDetails(parseInt(ji.dataset.jobinfo,10));
  const start=async(path, rescue)=>{
    try{
      const r=await api(`/accounts/${a.id}/quarantines/check`,{method:'POST',body:{path, rescue}});
      toast(r.already?'Задание уже идёт':(rescue?'Возврат писем запущен':'Сравнение запущено'), `Задание №${r.job_id}. Итог появится в «Прежних копиях».`);
      reopen();
    }catch(e){ toastErr(e); }
  };
  m.body.querySelectorAll('[data-cmp]').forEach(b=>b.onclick=()=>start(b.dataset.cmp, false));
  m.body.querySelectorAll('[data-rescue]').forEach(b=>b.onclick=async()=>{
    const ok=await confirmDlg('Вернуть письма в архив?',
      `Писем, которых нет в новой копии: ${b.dataset.n}. Они будут записаны в архив ящика — в те же папки — и станут видны в просмотре, поиске и выгрузке. Письма старше срока хранения ящика не возвращаются (их удалила бы ночная очистка).`,
      {okText:'Вернуть', okClass:'primary'});
    if(ok) start(b.dataset.rescue, true);
  });
  m.body.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
    const q=list.find(x=>x.path===b.dataset.del)||{};
    const c=q.check;
    const warn = !c ? 'Прежняя копия НЕ сравнивалась с новой: неизвестно, есть ли в ней письма, которых нет в новой копии. Надёжнее сначала нажать «Сравнить». '
      : !q.safe ? `${q.unsafe_reason?q.unsafe_reason.charAt(0).toUpperCase()+q.unsafe_reason.slice(1):'Есть письма, которых нет в новой копии'}. Эти письма будут потеряны. `
      : '';
    const ok=await confirmDlg(q.safe?'Удалить карантинную копию?':'Удалить прежнюю копию с потерей писем?',
      warn+'Каталог с прежними письмами будет удалён безвозвратно (фоновым заданием).',
      {okText:q.safe?'Удалить':'Удалить с потерей писем', okClass:'danger'});
    if(!ok) return;
    try{
      const r=await api(`/accounts/${a.id}/quarantines/delete`,{method:'POST',body:{path:b.dataset.del, force:!q.safe}});
      toast('Удаление поставлено в очередь', `Задание №${r.job_id}.`); reopen();
    }catch(e){ toastErr(e); }
  });
}

/** Копирование заново: докачать потерянное или стереть копию и скачать всё. */
function rebuildModal(a){
  const m=modal(`Скопировать заново: ${a.name}`, `
    <p class="muted">Обычная копия скачивает только те письма, которых ещё нет в архиве.
      Здесь можно перепроверить архив целиком.</p>
    <label class="form-row check" style="align-items:flex-start;gap:10px">
      <input type="radio" name="rb" value="missing" checked style="margin-top:4px">
      <span><b>Докачать потерянные письма</b><br>
        <span class="muted small">Сверяет индекс с файлами на диске: если файл письма пропал
        (сбой диска, оборванный прогон, чужая уборка), письмо скачивается заново.
        <b>Ничего не удаляется</b> — это безопасный режим.</span></span></label>
    ${isAdmin()?`<label class="form-row check" style="align-items:flex-start;gap:10px;margin-top:10px">
      <input type="radio" name="rb" value="full" style="margin-top:4px">
      <span><b>Полностью с нуля</b><br>
        <span class="muted small">Прежняя копия ящика уходит в карантин («Прежние копии»), индекс
        очищается, и всё скачивается с сервера заново.</span></span></label>
    <div class="hint" style="margin-top:12px;border-color:var(--danger)">
      <b>Осторожно с режимом «с нуля».</b> Письма, которых уже нет на почтовом сервере, останутся только
      в карантине: в просмотре, поиске и экспорте их не будет, а после удаления карантина их не вернуть.
      Выбирайте его, только если архив испорчен и нужен именно чистый лист.
      ${a.on_hold?'<br><b>Архив этого ящика удерживается — «с нуля» его не пересоздать, пока действует удержание.</b>':''}</div>`
      :'<div class="muted small" style="margin-top:10px">Копию «с нуля» запускает администратор.</div>'}`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Запустить</button>'});
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const mode=(m.body.querySelector('input[name=rb]:checked')||{}).value||'missing';
    if(mode==='full'){
      const ok=await confirmDlg('Скопировать ящик заново с нуля?',
        `Прежняя копия ящика «${a.name}» уйдёт в карантин, а письма будут заново скачаны с сервера. `+
        `Письма, которых на сервере уже нет, останутся только в карантине. Продолжить?`,
        {okText:'Да, скопировать с нуля', okClass:'danger'});
      if(!ok) return;
    }
    try{
      await api(`/accounts/${a.id}/backup`,{method:'POST',body:{rebuild:mode}});
      m.close();
      toast(mode==='full'?'Копия пересоздаётся с нуля':'Докачка потерянных писем запущена',
            'Следите за ходом в разделе «Очередь и задания»');
      location.hash='#/jobs';
    }catch(e){ toastErr(e); }
  };
}

/** Проверка папок на сервере: что открывается, что нет и теряются ли письма. */
async function folderDiagnoseModal(a){
  const m=modal(`Папки на сервере: ${a.name}`,
    '<div class="empty">⏳ Опрашиваем сервер по каждой папке…</div>', {wide:true});
  let d;
  try{ d=await api(`/accounts/${a.id}/folders/diagnose`,{method:'POST'}); }
  catch(e){ m.close(); return toastErr(e); }
  if(!d.ok){
    m.body.innerHTML=`<div class="empty">Не удалось подключиться: ${esc(d.error||'')}</div>`+
      (d.hint?`<div class="hint">${esc(d.hint)}</div>`:'');
    return;
  }
  const V={
    ok:['ok','копируется'],
    container:['', 'контейнер'],
    noselect:['', 'контейнер (\\Noselect)'],
    empty_broken:['warn','пустая, не открывается'],
    broken:['bad','НЕ ЧИТАЕТСЯ'],
    excluded:['', 'исключена'],
  };
  const c=d.counts||{};
  const rows=(d.folders||[]).map(f=>{
    const [cls,label]=V[f.verdict]||['',f.verdict];
    return `<tr>
      <td class="mono small" style="word-break:break-all">${esc(f.name)}</td>
      <td><span class="tag ${cls}">${esc(label)}</span></td>
      <td class="small">${f.messages==null?'—':fmtNum(f.messages)}</td>
      <td class="small muted">${esc(f.detail||'')}${f.fails?` <span class="tag warn">отказов подряд: ${f.fails}</span>`:''}</td></tr>`;
  }).join('');
  m.body.innerHTML=`
    <div class="an-kpis" style="margin-bottom:12px">
      <div class="kpi accent"><div class="k-label">✅ Копируется</div><div class="k-value">${fmtNum(c.ok||0)}</div><div class="k-sub">папок открывается</div></div>
      <div class="kpi"><div class="k-label">🗂️ Контейнеры</div><div class="k-value">${fmtNum((c.container||0)+(c.noselect||0))}</div><div class="k-sub">своих писем не хранят</div></div>
      <div class="kpi"><div class="k-label">⚠️ Не читается</div><div class="k-value">${fmtNum(c.broken||0)}</div><div class="k-sub">${d.messages_lost?`писем недоступно: ${fmtNum(d.messages_lost)}`:'писем в них не видно'}</div></div>
    </div>
    ${(c.empty_broken||c.excluded)?`<div class="muted small" style="margin-bottom:10px">Пустых нечитаемых папок: ${fmtNum(c.empty_broken||0)} · исключено настройками: ${fmtNum(c.excluded||0)}</div>`:''}
    <div class="table-wrap" style="max-height:420px;overflow:auto">
      <table class="tbl"><thead><tr><th>Папка</th><th>Состояние</th><th>Писем</th><th>Пояснение</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    ${(d.broken_folders&&d.broken_folders.length)?`
      <div class="card" style="margin-top:12px;border-color:var(--warn)">
        <b>⚠️ Папки, которые сервер не даёт прочитать (${d.broken_folders.length})</b>
        <div class="muted small" style="margin:6px 0">Чинить их нужно на почтовом сервере. Если это
          невозможно — исключите их, чтобы задание перестало помечаться неполным.</div>
        ${isAdmin()?'<button class="btn small" id="diagExclude">🚫 Больше не копировать эти папки</button>':'<div class="muted small">Исключить папки может администратор.</div>'}
      </div>`:''}`;
  const ex=$('#diagExclude', m.el);
  if(ex) ex.onclick=async()=>{
    try{
      const r=await api(`/accounts/${a.id}/exclude-folders`,{method:'POST',body:{folders:d.broken_folders}});
      toast('Папки исключены', r.added.length?`Добавлено: ${r.added.length}`:'Они уже были в списке');
      ex.disabled=true;
    }catch(e){ toastErr(e); }
  };
}

function accountMenu(a){
  // Оператору — только копирование и проверки: письма, настройки ящика,
  // выгрузки и удаление доступны администратору.
  if(isOperator()){
    const m=modal(`Ящик: ${a.name}`, `<div class="btn-row" style="flex-direction:column;align-items:stretch;gap:10px">
      <button class="btn primary" data-a="backup" ${a.enabled?'':'disabled title="Ящик выключен — включает его администратор"'}>💾 Сделать резервную копию сейчас</button>
      <button class="btn" data-a="test">🔌 Проверить подключение</button>
      <button class="btn" data-a="verify">🔍 Проверить целостность копии</button>
      <button class="btn" data-a="folders">🗂️ Проверить папки на сервере</button>
      <button class="btn" data-a="rebuild" ${a.enabled?'':'disabled title="Ящик выключен"'}>🩹 Докачать потерянные письма</button>
      <button class="btn" data-a="runs">📜 История копирования</button>
      <div class="muted small">Просмотр писем, настройки ящика, выгрузка и удаление — у администратора.</div>
    </div>`);
    m.body.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
      const act=b.dataset.a; m.close();
      if(act==='backup') backupNow(a.id, a.name);
      else if(act==='test') testAccount(a.id);
      else if(act==='folders') folderDiagnoseModal(a);
      else if(act==='rebuild') rebuildModal(a);
      else if(act==='runs') runsModal(a);
      else if(act==='verify'){ try{await api(`/accounts/${a.id}/verify`,{method:'POST'}); toast('Запущено','Проверка целостности в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} }
    });
    return;
  }
  const m=modal(`Ящик: ${a.name}`, `<div class="btn-row" style="flex-direction:column;align-items:stretch;gap:10px">
    <button class="btn primary" data-a="backup" ${a.enabled?'':'disabled title="Ящик выключен — включите его, чтобы делать копии"'}>💾 Сделать резервную копию сейчас</button>
    <button class="btn" data-a="mail">📧 Просмотр писем</button>
    <button class="btn" data-a="edit">✏️ Редактировать</button>
    <button class="btn" data-a="export">📤 Экспорт в PST / EML / MBOX</button>
    <button class="btn" data-a="restore">♻️ Восстановить на сервер</button>
    <button class="btn" data-a="import">📥 Импорт из .pst</button>
    <button class="btn" data-a="retention">🗓️ Хранение копий (3 дня / неделя)</button>
    <button class="btn" data-a="verify">🔍 Проверить целостность копии</button>
    <button class="btn" data-a="folders">🗂️ Проверить папки на сервере</button>
    <button class="btn" data-a="rebuild" ${a.enabled?'':'disabled title="Ящик выключен"'}>🔄 Скопировать заново</button>
    <button class="btn" data-a="quarantine">🧺 Прежние копии (карантин)${a.quarantines?` — ${a.quarantines}`:''}</button>
    <button class="btn" data-a="logout" title="Сотрудник, вошедший в интерфейс по паролю этого ящика, будет разлогинен">🚪 Завершить сеансы сотрудника</button>
    <button class="btn" data-a="hold" title="Пока действует удержание, письма ящика не удаляются по сроку хранения">🔒 Удержание архива${a.on_hold?' (до '+esc(holdDate(a.hold_until))+')':''}</button>
    ${axFeature()?'<button class="btn" data-a="axigen" title="Учётная запись этого ящика на сервере Axigen: вход, квота, действия">📮 На сервере Axigen</button>':''}
    <button class="btn danger" data-a="del" ${a.on_hold?'disabled title="Архив удерживается — удалить ящик нельзя"':''}>🗑️ Удалить ящик</button>
    <button class="btn danger" data-a="purge" ${a.on_hold?'disabled title="Архив удерживается — удалить нельзя"':''}>🧹 Удалить ящик вместе с архивом писем</button>
  </div>`);
  m.body.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
    const act=b.dataset.a; m.close();
    if(act==='backup') backupNow(a.id, a.name);
    else if(act==='mail'){ State.mailAccount=a.id; location.hash='#/mail'; }
    else if(act==='edit') accountModal(a.id);
    else if(act==='export') exportModal(a);
    else if(act==='restore') restoreModal(a);
    else if(act==='import') importModal(a);
    else if(act==='retention') retentionModal(a);
    else if(act==='folders') folderDiagnoseModal(a);
    else if(act==='rebuild') rebuildModal(a);
    else if(act==='quarantine') quarantineModal(a);
    else if(act==='hold') holdModal(a);
    else if(act==='axigen') axigenForMailbox(a);
    else if(act==='purge') purgeModal(a);
    else if(act==='logout'){ try{ const r=await api(`/accounts/${a.id}/logout-sessions`,{method:'POST'}); toast('Готово', r.closed?`Завершено сеансов: ${r.closed}`:'Открытых сеансов не было'); }catch(e){toastErr(e);} }
    else if(act==='verify'){ try{await api(`/accounts/${a.id}/verify`,{method:'POST'}); toast('Запущено','Проверка целостности в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} }
    else if(act==='del'){ if(await confirmDlg('Удалить ящик?', `Ящик «${a.name}» и его настройки будут удалены. Локальные копии писем на диске останутся.${a.on_hold?' Архив ящика удерживается до '+holdDate(a.hold_until)+' — файлы писем не трогаются.':''} Продолжить?`)){ try{await api(`/accounts/${a.id}`,{method:'DELETE'}); toast('Удалено'); route();}catch(e){toastErr(e);} } }
  });
}

// ===================================================================
//  Групповые действия с ящиками
// ===================================================================
//: Состояние раздела «Групповые действия» (живёт между перерисовками).
const Bulk = { source:'selected', filter:'', query:'', list:'', action:null, params:{}, showTargets:false };

/** Описание действий с сервера (один раз за сеанс). */
async function bulkMeta(key){
  if(!State.bulkActions) State.bulkActions=await api('/accounts/bulk/actions');
  if(!key) return State.bulkActions;
  return State.bulkActions.actions.find(a=>a.key===key) || {key, label:key, params:[], desc:'', danger:0};
}

/** Разбор списка адресов: ящик ищется по логину, затем по названию. */
function bulkParseList(text, accs){
  const byLogin=new Map(), byName=new Map();
  accs.forEach(a=>{ byLogin.set(String(a.username||'').toLowerCase(), a); byName.set(String(a.name||'').toLowerCase(), a); });
  const found=[], missing=[], seen=new Set();
  const tokens=[];
  String(text||'').split(/[\n,;\t]+/).forEach(raw=>{
    const t=raw.trim(); if(!t) return;
    const inAngle=t.match(/<([^<>\s]+@[^<>\s]+)>/g);
    if(inAngle) inAngle.forEach(x=>tokens.push(x.slice(1,-1)));          // «Иванов Иван <ivanov@…>»
    else if(t.includes('@') && /\s/.test(t)) t.split(/\s+/).filter(x=>x.includes('@')).forEach(x=>tokens.push(x));  // «Иванов Иван ivanov@…», несколько адресов
    else tokens.push(t);                                                  // адрес или название ящика
  });
  tokens.map(s=>s.trim()).filter(Boolean).forEach(item=>{
    const key=item.toLowerCase();
    const a=byLogin.get(key)||byName.get(key);
    if(!a){ if(!missing.includes(item)) missing.push(item); return; }
    if(!seen.has(a.id)){ seen.add(a.id); found.push(a); }
  });
  return {found, missing};
}

/** Ящики, над которыми будет выполнено действие, по выбранному источнику. */
function bulkTargets(accs){
  switch(Bulk.source){
    case 'all': return accs.slice();
    case 'enabled': return accs.filter(a=>a.enabled);
    case 'filter': return accs.filter(a=>accountMatchesBy(a, Bulk.filter, Bulk.query));
    case 'list': return bulkParseList(Bulk.list, accs).found;
    default: return accs.filter(a=>Acc.sel.has(a.id));
  }
}

async function viewBulk(c, accs){
  let meta;
  State.bulkActions=null;     // список ящиков-образцов и умолчания могли измениться
  try{ meta=await bulkMeta(); }catch(e){ c.appendChild(h(`<div class="card"><div class="empty">${esc(e.message)}</div></div>`)); return; }
  const opts=accFilterOptions(accs);
  const targetsCard=h(`<div class="card bulk-card"><h3>1. Какие ящики</h3>
    <div class="bulk-sources">
      <label class="radio"><input type="radio" name="bsrc" value="selected"> Отмеченные в списке — <b>${fmtNum(Acc.sel.size)}</b>
        ${Acc.sel.size?'':'<span class="muted small">(отметьте ящики флажками на вкладке «Список ящиков»)</span>'}</label>
      <label class="radio"><input type="radio" name="bsrc" value="enabled"> Все включённые — <b>${fmtNum(accs.filter(a=>a.enabled).length)}</b></label>
      <label class="radio"><input type="radio" name="bsrc" value="all"> Все ящики — <b>${fmtNum(accs.length)}</b></label>
      <label class="radio"><input type="radio" name="bsrc" value="filter"> По отбору:</label>
      <div class="bulk-sub" data-sub="filter">
        <select id="bF">${opts.map(([v,t])=>`<option value="${v}" ${v===Bulk.filter?'selected':''}>${esc(t)}</option>`).join('')}</select>
        <input type="text" id="bQ" placeholder="и поиск по названию, логину, серверу…" value="${esc(Bulk.query)}">
      </div>
      <label class="radio"><input type="radio" name="bsrc" value="list"> По списку адресов (вставьте из Excel или письма):</label>
      <div class="bulk-sub" data-sub="list">
        <textarea id="bL" rows="4" placeholder="ivanov@company.ru&#10;petrova@company.ru">${esc(Bulk.list)}</textarea>
        <div class="small muted" id="bLinfo"></div>
      </div>
    </div>
    <div class="bulk-total"><span id="bTotal"></span> <a href="javascript:void(0)" id="bShow" class="small"></a>
      <span class="spacer"></span>
      <button class="btn sm" id="bExport" title="Выгрузить список этих ящиков в Excel">📊 Список в Excel</button></div>
    <div id="bList" class="bulk-list" style="display:none"></div></div>`);
  c.appendChild(targetsCard);

  const actionsCard=h(`<div class="card bulk-card"><h3>2. Что сделать</h3><div class="bulk-groups"></div></div>`);
  const groupsBox=actionsCard.querySelector('.bulk-groups');
  meta.groups.forEach(g=>{
    const items=meta.actions.filter(a=>a.group===g.key);
    if(!items.length && g.key!=='manage') return;
    const box=h(`<div class="bulk-group"><div class="bg-title">${esc(g.label)}</div><div class="bulk-tiles"></div></div>`);
    const tiles=box.querySelector('.bulk-tiles');
    items.forEach(a=>{
      const t=h(`<button class="bulk-tile ${a.danger>=2?'danger':(a.danger===1?'warn':'')}" data-act="${esc(a.key)}" title="${esc(a.desc)}">
        <span class="ic">${a.icon}</span><span>${esc(a.label)}</span></button>`);
      tiles.appendChild(t);
    });
    if(g.key==='manage'){
      if(isAdmin()){
        tiles.appendChild(h(`<button class="bulk-tile" data-client="create" title="Завести много ящиков сразу из списка адресов"><span class="ic">➕</span><span>Добавить ящики списком</span></button>`));
        tiles.appendChild(h(`<button class="bulk-tile" data-client="passwords" title="Массово проставить пароли из файла «адрес — пароль»"><span class="ic">🔑</span><span>Загрузить пароли из файла</span></button>`));
      }
      tiles.appendChild(h(`<button class="bulk-tile" data-client="excel" title="Выгрузить список ящиков из пункта 1 в Excel"><span class="ic">📊</span><span>Список ящиков в Excel</span></button>`));
    }
    groupsBox.appendChild(box);
  });
  if(isOperator()) actionsCard.appendChild(h('<div class="muted small" style="margin-top:8px">Оператору доступны копирование и проверки. Остальные групповые действия — у администратора.</div>'));
  c.appendChild(actionsCard);

  const paramsCard=h(`<div class="card bulk-card" id="bParams" style="display:none"></div>`);
  c.appendChild(paramsCard);
  const histCard=h(`<div class="card bulk-card"><div class="section-title" style="margin-bottom:8px"><h3 style="margin:0">История групповых действий</h3><div class="spacer"></div><button class="btn sm ghost" id="bHistR">↻ Обновить</button></div><div id="bHist"><div class="empty"><span class="spinner"></span></div></div></div>`);
  c.appendChild(histCard);

  const radios=$$('input[name=bsrc]', targetsCard);
  const updateTargets=()=>{
    radios.forEach(r=>{ r.checked=(r.value===Bulk.source); });
    $$('[data-sub]', targetsCard).forEach(el=>el.classList.toggle('on', el.dataset.sub===Bulk.source));
    const list=bulkTargets(accs);
    $('#bTotal',targetsCard).innerHTML=`Будет обработано: <b>${fmtNum(list.length)}</b> ${plural(list.length,'ящик','ящика','ящиков')}`;
    if(Bulk.source==='list'){
      const r=bulkParseList(Bulk.list, accs);
      $('#bLinfo',targetsCard).innerHTML = Bulk.list.trim()
        ? `Найдено: <b>${r.found.length}</b>` + (r.missing.length?` · не найдено: <b style="color:var(--danger)">${r.missing.length}</b> — ${esc(r.missing.slice(0,12).join(', '))}${r.missing.length>12?' …':''}`:'')
        : 'По одному адресу (логину) или названию ящика в строке; подойдут и через запятую.';
    }
    const show=$('#bShow',targetsCard);
    show.textContent = list.length ? (Bulk.showTargets?'скрыть список':'показать список') : '';
    const box=$('#bList',targetsCard);
    box.style.display = Bulk.showTargets && list.length ? '' : 'none';
    if(Bulk.showTargets && list.length){
      const max=300;
      box.innerHTML=`<table class="tbl"><tbody>${list.slice(0,max).map(a=>`<tr><td>${esc(a.name)}</td><td class="small muted">${esc(a.username)}</td><td>${a.enabled?'<span class="tag ok">включён</span>':'<span class="tag">выключен</span>'}</td></tr>`).join('')}</tbody></table>`
        + (list.length>max?`<div class="muted small" style="padding:6px 10px">… и ещё ${fmtNum(list.length-max)}</div>`:'');
    }
    const go=$('#bGo', paramsCard); if(go) go.disabled=!list.length;
  };
  radios.forEach(r=>r.onchange=()=>{ Bulk.source=r.value; updateTargets(); });
  $('#bF',targetsCard).onchange=e=>{ Bulk.filter=e.target.value; Bulk.source='filter'; updateTargets(); };
  let qT=null;
  $('#bQ',targetsCard).oninput=e=>{ Bulk.query=e.target.value; Bulk.source='filter'; clearTimeout(qT); qT=setTimeout(updateTargets,200); };
  let lT=null;
  $('#bL',targetsCard).oninput=e=>{ Bulk.list=e.target.value; Bulk.source='list'; clearTimeout(lT); lT=setTimeout(updateTargets,250); };
  $('#bShow',targetsCard).onclick=()=>{ Bulk.showTargets=!Bulk.showTargets; updateTargets(); };
  $('#bExport',targetsCard).onclick=()=>{ const ids=bulkTargets(accs).map(a=>a.id); if(!ids.length) return toast('Не выбрано ни одного ящика','','warn'); exportAccountList(ids); };

  const selectAction=(key)=>{
    Bulk.action=key;
    $$('.bulk-tile', actionsCard).forEach(t=>t.classList.toggle('active', t.dataset.act===key));
    const a=meta.actions.find(x=>x.key===key);
    if(!a){ paramsCard.style.display='none'; return; }
    const saved=(Bulk.params[key]||{});
    const tabParams=a.params.filter(p=>!p.on_warning);
    paramsCard.style.display='';
    paramsCard.innerHTML=`<h3>3. ${a.icon} ${esc(a.label)}</h3>
      <p class="muted" style="margin-top:-4px">${esc(a.desc)}</p>
      ${a.note?`<div class="hint" style="margin-bottom:12px">💡 ${esc(a.note)}</div>`:''}
      ${tabParams.length?`<div class="bulk-form" data-form>${bulkFormHtml(tabParams, saved)}</div>`:''}
      <div class="btn-row" style="margin-top:6px">
        <button class="btn ${a.danger>=2?'danger':'primary'}" id="bGo">Проверить и запустить…</button>
        <span class="muted small" style="align-self:center">Сначала покажем, что будет сделано с каждым ящиком, — без изменений.</span></div>`;
    const form=paramsCard.querySelector('[data-form]');
    if(form) bulkFormWire(form, tabParams);
    $('#bGo',paramsCard).onclick=async()=>{
      const values=form?bulkFormRead(form, tabParams):{};
      Bulk.params[key]=values;
      const ids=bulkTargets(accs).map(x=>x.id);
      if(!ids.length) return toast('Не выбрано ни одного ящика','Укажите ящики в пункте 1','warn');
      const done=await bulkDialog(key, ids, values, {onDone:()=>{ Bulk.action=null; }});
      if(done) loadBulkHistory($('#bHist',histCard));
    };
    updateTargets();
    paramsCard.scrollIntoView({behavior:'smooth', block:'nearest'});
  };
  $$('.bulk-tile', actionsCard).forEach(t=>t.onclick=()=>{
    if(t.dataset.client==='passwords') return passwordImportModal();
    if(t.dataset.client==='create') return bulkCreateModal();
    if(t.dataset.client==='excel'){ const ids=bulkTargets(accs).map(a=>a.id); return ids.length?exportAccountList(ids):toast('Не выбрано ни одного ящика','Укажите ящики в пункте 1','warn'); }
    selectAction(t.dataset.act);
  });
  $('#bHistR',histCard).onclick=()=>loadBulkHistory($('#bHist',histCard));
  updateTargets();
  if(Bulk.action) selectAction(Bulk.action);
  loadBulkHistory($('#bHist',histCard));
}

// ---------- Форма параметров действия (по описанию с сервера) ----------
function bulkFormHtml(params, values){
  return params.map(p=>{
    const v = (values && p.key in values) ? values[p.key] : p.default;
    const help = p.help?`<div class="hint">${esc(p.help)}</div>`:'';
    const warnOnly = p.on_warning ? ' data-onwarn style="display:none"' : '';
    const attrs=`data-p="${esc(p.key)}"`;
    let input;
    switch(p.kind){
      case 'bool':
        return `<div class="form-row check" data-row="${esc(p.key)}"${warnOnly}><span class="switch"><input type="checkbox" ${attrs} ${v?'checked':''}><span class="track"></span></span><label>${esc(p.label)}</label>${help}</div>`;
      case 'select':
        input=`<select ${attrs}>${p.options.map(o=>`<option value="${esc(o.value)}" ${String(o.value)===String(v)?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`; break;
      case 'int':
        input=`<input type="number" ${attrs} value="${v==null?'':esc(v)}" ${p.min!=null?`min="${p.min}"`:''} ${p.max!=null?`max="${p.max}"`:''} placeholder="${esc(p.placeholder||'')}">`; break;
      case 'textarea':
        input=`<textarea ${attrs} rows="3" placeholder="${esc(p.placeholder||'')}">${esc(v||'')}</textarea>`; break;
      case 'date': input=`<input type="date" ${attrs} value="${esc(v||'')}">`; break;
      case 'time': input=`<input type="time" ${attrs} value="${esc(v||'')}">`; break;
      default: input=`<input type="text" ${attrs} value="${esc(v==null?'':v)}" placeholder="${esc(p.placeholder||'')}">`;
    }
    return `<div class="form-row" data-row="${esc(p.key)}"${warnOnly}><label>${esc(p.label)}</label>${input}${help}</div>`;
  }).join('');
}
function bulkFormRead(root, params){
  const out={};
  params.forEach(p=>{
    const el=root.querySelector(`[data-p="${p.key}"]`); if(!el) return;
    if(p.kind==='bool') out[p.key]=el.checked;
    else if(p.kind==='int') out[p.key]=el.value===''?null:parseInt(el.value,10);
    else out[p.key]=el.value;
  });
  return out;
}
/** Показывать поля, зависящие от других (show_if). */
function bulkFormWire(root, params){
  const apply=()=>{
    const vals=bulkFormRead(root, params);
    params.forEach(p=>{
      if(!p.show_if) return;
      const row=root.querySelector(`[data-row="${p.key}"]`); if(!row) return;
      const ok=Object.entries(p.show_if).every(([k,list])=>list.some(x=>String(x)===String(vals[k])));
      row.style.display=ok?'':'none';
    });
  };
  root.addEventListener('change', apply);
  apply();
}

// ---------- Проверка, подтверждение и запуск ----------
const BULK_STATUS = {ok:['ok','будет сделано','сделано'], skip:['','пропуск','пропуск'], fail:['err','ошибка','ошибка']};

/** Итог по ящикам: плитки, причины пропусков, таблица с отбором. */
function bulkResultHtml(r, preview){
  const c=r.counts||{};
  const reasons=Object.entries(r.reasons||{}).sort((a,b)=>b[1]-a[1]);
  const disk=(r.extra&&r.extra.disk)||null;
  return `<div class="an-kpis" style="margin:4px 0 12px">
      <div class="kpi accent"><div class="k-label">${preview?'✅ Будет обработано':'✅ Выполнено'}</div><div class="k-value">${fmtNum(c.ok||0)}</div><div class="k-sub">из ${fmtNum(r.total)}</div></div>
      <div class="kpi"><div class="k-label">⏭️ Пропущено</div><div class="k-value">${fmtNum(c.skip||0)}</div><div class="k-sub">${(c.skip||0)?'причины ниже':'нет'}</div></div>
      <div class="kpi"><div class="k-label">⚠️ Ошибок</div><div class="k-value">${fmtNum(c.fail||0)}</div><div class="k-sub">${(c.fail||0)?'см. таблицу':'нет'}</div></div>
      ${disk?`<div class="kpi"><div class="k-label">💽 Место</div><div class="k-value" style="font-size:1.05rem">${esc(disk.need_h)}</div><div class="k-sub">нужно · свободно ${esc(disk.free_h)}</div></div>`:''}
      ${(r.jobs&&r.jobs.length)?`<div class="kpi"><div class="k-label">⚙️ Заданий</div><div class="k-value">${fmtNum(r.jobs.length)}</div><div class="k-sub">в очереди</div></div>`:''}
    </div>
    ${(r.warnings||[]).map(w=>`<div class="hint warnbox">⚠️ ${esc(w)}</div>`).join('')}
    ${reasons.length?`<div class="bulk-reasons"><b>Почему пропущены:</b> ${reasons.map(([t,n])=>`<span class="tag">${esc(t)} — ${fmtNum(n)}</span>`).join(' ')}</div>`:''}
    <div class="bulk-chips" data-chips>
      <button class="chip active" data-f="">Все (${fmtNum(r.results.length)})</button>
      <button class="chip" data-f="ok">${preview?'Будут обработаны':'Выполнены'} (${fmtNum(c.ok||0)})</button>
      <button class="chip" data-f="skip">Пропущены (${fmtNum(c.skip||0)})</button>
      ${(c.fail||0)?`<button class="chip" data-f="fail">Ошибки (${fmtNum(c.fail)})</button>`:''}
    </div>
    <div class="table-wrap bulk-results"><table class="tbl"><thead><tr><th>Ящик</th><th>Итог</th><th>Подробности</th></tr></thead><tbody data-rows></tbody></table></div>`;
}
function bulkWireResults(root, r, preview){
  const rows=root.querySelector('[data-rows]');
  const draw=(f)=>{
    const list=r.results.filter(x=>!f||x.status===f);
    const max=500;
    rows.innerHTML=list.slice(0,max).map(x=>{
      const [cls,pv,done]=BULK_STATUS[x.status]||['',x.status,x.status];
      const js=x.job_status?` <span class="badge ${esc(x.job_status)}">${esc(STATUS_LBL[x.job_status]||x.job_status)}</span>`:'';
      return `<tr><td><b>${esc(x.name)}</b><div class="small muted">${esc(x.username||'')}</div></td>
        <td><span class="tag ${cls}">${esc(preview?pv:done)}</span>${js}</td>
        <td class="small">${esc(x.detail||'')}${x.job_id?` <a href="javascript:void(0)" class="small" data-job="${x.job_id}">задание №${x.job_id}</a>`:''}</td></tr>`;
    }).join('') + (list.length>max?`<tr><td colspan="3" class="muted small">… показаны первые ${max} из ${fmtNum(list.length)}</td></tr>`:'')
      + (list.length?'':'<tr><td colspan="3" class="empty">Нет строк</td></tr>');
    rows.querySelectorAll('[data-job]').forEach(a=>a.onclick=()=>jobDetails(parseInt(a.dataset.job,10)));
  };
  root.querySelectorAll('[data-chips] .chip').forEach(b=>b.onclick=()=>{
    root.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x===b));
    draw(b.dataset.f);
  });
  draw('');
}

/** Окно групповой операции: проверка → подтверждение → запуск → итог.
 *  Возвращает промис, который разрешается true, если операция выполнена. */
function bulkDialog(actionKey, ids, params={}, opts={}){
  // opts.ep / opts.idsKey / opts.meta — то же окно для раздела «Ящики на сервере»
  // (свой адрес API, ключи ящиков вместо номеров, описание действия уже загружено).
  const ep=opts.ep||'/accounts/bulk', idsKey=opts.idsKey||'ids';
  return new Promise(async resolve=>{
    let meta;
    try{ meta=opts.meta||await bulkMeta(actionKey); }catch(e){ toastErr(e); return resolve(false); }
    // Параметры, которых не передали, настраиваются прямо в окне.
    const inline=(meta.params||[]).filter(p=>!(p.key in params));
    let finished=false;
    const m=modal(opts.title||`${meta.icon||''} ${meta.label}`, `
      ${opts.intro||`<p class="muted" style="margin-top:0">${esc(meta.desc||'')}</p>`}
      ${meta.note&&!opts.intro?`<div class="hint" style="margin-bottom:12px">💡 ${esc(meta.note)}</div>`:''}
      ${inline.length?`<div class="bulk-form boxed" data-form>${bulkFormHtml(inline, opts.values||{})}</div>`:''}
      <div data-preview><div class="empty"><span class="spinner"></span> Проверяем ${fmtNum(ids.length)} ${plural(ids.length,'ящик','ящика','ящиков')}…</div></div>
      <div data-confirm></div>`,
      {wide:true, onClose:()=>resolve(finished),
       footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go disabled>Выполнить</button>`});
    const form=m.body.querySelector('[data-form]');
    if(form) bulkFormWire(form, inline);
    // Поля «только при предупреждении» (например, «запустить, даже если места мало»)
    // показываются, лишь когда проверка о чём-то предупредила.
    const syncForm=(warnings)=>{
      if(!form) return;
      form.querySelectorAll('[data-onwarn]').forEach(row=>{ row.style.display=(warnings||[]).length?'':'none'; });
      form.style.display=[...form.querySelectorAll('[data-row]')].some(row=>row.style.display!=='none')?'':'none';
    };
    syncForm([]);
    const go=m.foot.querySelector('[data-go]');
    const current=()=>Object.assign({}, params, form?bulkFormRead(form, inline):{});
    let pv=null, seq=0;
    const box=m.body.querySelector('[data-preview]');
    const conf=m.body.querySelector('[data-confirm]');
    const refresh=async()=>{
      const my=++seq;
      go.disabled=true;
      try{ pv=await api(ep,{method:'POST',body:{action:actionKey, [idsKey]:ids, params:current(), preview:true}}); }
      catch(e){ if(my!==seq) return; box.innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; conf.innerHTML=''; return; }
      if(my!==seq) return;
      syncForm(pv.warnings);
      box.innerHTML=bulkResultHtml(pv, true);
      bulkWireResults(box, pv, true);
      const n=(pv.counts&&pv.counts.ok)||0;
      go.className='btn '+(pv.danger>=2?'danger':(pv.danger===1?'danger':'primary'));
      go.textContent=n?`Выполнить для ${fmtNum(n)} ${plural(n,'ящика','ящиков','ящиков')}`:'Нечего выполнять';
      if(pv.danger>=2 && n){
        conf.innerHTML=`<div class="form-row confirm-row"><label>Это действие трудно или невозможно отменить. Для подтверждения введите число ящиков: <b>${fmtNum(n)}</b></label>
          <input type="text" id="bConfirm" inputmode="numeric" autocomplete="off" placeholder="${n}"></div>`;
        const inp=conf.querySelector('#bConfirm');
        const check=()=>{ go.disabled=inp.value.replace(/\s/g,'')!==String(n); };
        inp.oninput=check; check();
      }else{ conf.innerHTML=''; go.disabled=!n; }
    };
    if(form){
      let t=null;
      form.addEventListener('change', ()=>{ clearTimeout(t); t=setTimeout(refresh, 250); });
      form.addEventListener('input', (e)=>{ if(e.target.type==='checkbox') return; clearTimeout(t); t=setTimeout(refresh, 700); });
    }
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    go.onclick=async()=>{
      if(go.disabled||!pv) return;
      const confirmValue=(conf.querySelector('#bConfirm')||{}).value||'';
      go.disabled=true; go.innerHTML='<span class="spinner"></span> Выполняется…';
      let r;
      try{ r=await api(ep,{method:'POST',body:{action:actionKey, [idsKey]:ids, params:current(), preview:false, confirm:confirmValue.replace(/\s/g,'')}}); }
      catch(e){ toastErr(e); go.disabled=false; go.textContent='Выполнить'; return; }
      finished=true;
      if(opts.onDone) try{ opts.onDone(r); }catch(_){}
      if(form) form.remove();
      conf.innerHTML='';
      box.innerHTML=`<div class="hint okbox">✅ ${esc(r.summary)}${r.op_id?` Операция №${r.op_id} — в ${opts.historyName||'истории групповых действий'}.`:''}</div>`+bulkResultHtml(r, false);
      bulkWireResults(box, r, false);
      m.foot.innerHTML=`${(r.jobs&&r.jobs.length)?'<button class="btn" data-jobs>⚙️ Открыть очередь заданий</button>':''}<button class="btn primary" data-close>Готово</button>`;
      const jb=m.foot.querySelector('[data-jobs]'); if(jb) jb.onclick=()=>{ m.close(); location.hash='#/jobs'; };
      m.foot.querySelector('[data-close]').onclick=()=>{ m.close(); if(State.view==='accounts' || (opts.refreshView && State.view===opts.refreshView)) route(); };
      toast('Готово', r.summary, (r.counts.fail?'warn':'success'), 7000);
    };
    refresh();
  });
}

// ---------- История групповых действий ----------
async function loadBulkHistory(box){
  if(!box) return;
  let d;
  try{ d=await api('/accounts/bulk/history?limit=30',{bg:true}); }catch(e){ box.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
  const items=d.items||[];
  if(!items.length){ box.innerHTML='<div class="empty">Групповых действий ещё не было.</div>'; return; }
  box.innerHTML=`<div class="table-wrap"><table class="tbl"><thead><tr><th>№</th><th>Когда</th><th>Кто</th><th>Действие</th><th>Ящиков</th><th>Итог</th><th></th></tr></thead><tbody>${items.map(o=>`<tr>
      <td class="muted nowrap">${o.id}</td><td class="small nowrap">${esc(fmtDate(o.created_at))}</td><td class="small">${esc(o.user)}</td>
      <td>${esc(o.label)}</td><td class="small">${fmtNum(o.total)}</td>
      <td class="small"><span class="tag ok">выполнено ${fmtNum(o.ok)}</span>${o.skipped?` <span class="tag">пропущено ${fmtNum(o.skipped)}</span>`:''}${o.failed?` <span class="tag err">ошибок ${fmtNum(o.failed)}</span>`:''}${o.jobs?` <span class="tag">заданий ${fmtNum(o.jobs)}</span>`:''}</td>
      <td style="text-align:right"><button class="btn sm" data-op="${o.id}">Подробнее</button></td></tr>`).join('')}</tbody></table></div>`;
  box.querySelectorAll('[data-op]').forEach(b=>b.onclick=()=>bulkOpModal(parseInt(b.dataset.op,10), box));
}

async function bulkOpModal(opId, historyBox){
  let o;
  try{ o=await api(`/accounts/bulk/history/${opId}`); }catch(e){ return toastErr(e); }
  const pr=o.job_progress||{};
  const jobsTotal=Object.values(pr).reduce((s,n)=>s+n,0);
  const doneJobs=(pr.success||0)+(pr.partial||0)+(pr.failed||0)+(pr.cancelled||0);
  const params=Object.entries(o.params||{}).filter(([k,v])=>v!==''&&v!==null&&v!==undefined)
    .map(([k,v])=>`<span class="tag">${esc(k)}: ${esc(typeof v==='object'?JSON.stringify(v):String(v))}</span>`).join(' ');
  const r={total:o.total, counts:{ok:o.ok, skip:o.skipped, fail:o.failed}, results:o.results||[], reasons:{}, warnings:[], jobs:[]};
  (o.results||[]).forEach(x=>{ if(x.status==='skip') r.reasons[x.detail]=(r.reasons[x.detail]||0)+1; });
  const m=modal(`Групповое действие №${o.id}: ${o.label}`, `
    <div class="kv"><div class="k">Когда и кто</div><div>${esc(fmtDate(o.created_at))} · ${esc(o.user)}</div></div>
    ${params?`<div class="kv"><div class="k">Параметры</div><div>${params}</div></div>`:''}
    <div class="kv"><div class="k">Итог</div><div>${esc(o.summary)}</div></div>
    ${jobsTotal?`<div class="card" style="margin:12px 0">
      <b>Задания операции: ${fmtNum(doneJobs)} из ${fmtNum(jobsTotal)} завершены</b>
      <div class="progress" style="margin:8px 0"><span style="width:${Math.round(100*doneJobs/Math.max(1,jobsTotal))}%"></span></div>
      <div class="small">${Object.entries(pr).map(([s,n])=>`<span class="badge ${esc(s)}">${esc(STATUS_LBL[s]||s)}: ${fmtNum(n)}</span>`).join(' ')}</div>
      ${o.jobs_active?`<button class="btn danger sm" id="opCancel" style="margin-top:10px">⏹️ Отменить незавершённые (${fmtNum(o.jobs_active)})</button>`:''}
    </div>`:''}
    <div data-res>${bulkResultHtml(r, false)}</div>`, {wide:true});
  bulkWireResults(m.body.querySelector('[data-res]'), r, false);
  const cb=$('#opCancel', m.el);
  if(cb) cb.onclick=async()=>{
    if(!await confirmDlg('Отменить незавершённые задания?', `Задания этой операции, которые ещё ждут очереди или выполняются (${o.jobs_active}), будут отменены. Уже сделанное останется.`, {okText:'Отменить задания'})) return;
    try{ const res=await api(`/accounts/bulk/history/${o.id}/cancel`,{method:'POST'}); toast('Готово', `Отменено заданий: ${res.cancelled}`); m.close(); bulkOpModal(opId, historyBox); if(historyBox) loadBulkHistory(historyBox); }
    catch(e){ toastErr(e); }
  };
}

// ---------- Добавление ящиков списком ----------
function bulkCreateModal(){
  State.bulkActions=null;
  bulkMeta().then(meta=>{
    const d=meta.create_defaults||{};
    const m=modal('Добавить ящики списком', `
      <p class="muted" style="margin-top:0">По одному ящику в строке: <b>адрес;пароль</b>. Пароль можно не указывать —
        такой ящик заведётся выключенным, а пароли загрузите потом файлом («Загрузить пароли»). Подойдут и две колонки,
        скопированные из Excel, и строки вида «Иванов Иван &lt;ivanov@company.ru&gt;;пароль». Если адрес есть в
        справочнике сотрудников, ящик получит ФИО и сразу свяжется с карточкой.</p>
      <div class="form-row"><label>Список ящиков</label><textarea id="c-text" rows="7" class="mono" placeholder="ivanov@company.ru;Пароль1&#10;petrova@company.ru"></textarea></div>
      <div class="grid cols-2">
        <div class="form-row"><label>IMAP-сервер</label><input type="text" id="c-host" value="${esc(d.host||'')}" placeholder="imap.company.ru"></div>
        <div class="form-row"><label>Порт</label><input type="number" id="c-port" min="1" max="65535" value="${esc(d.port||993)}"></div>
      </div>
      <div class="grid cols-2">
        <div class="form-row"><label>Шифрование</label><select id="c-sec">
          <option value="ssl" ${d.security!=='starttls'&&d.security!=='plain'?'selected':''}>SSL/TLS (порт 993)</option>
          <option value="starttls" ${d.security==='starttls'?'selected':''}>STARTTLS (порт 143)</option>
          <option value="plain" ${d.security==='plain'?'selected':''}>Без шифрования</option></select></div>
        <div class="form-row"><label>Вход в ящики</label><select id="c-auth"><option value="password">по паролю ящика</option>
          ${d.master?'<option value="master">через администратора почты</option>':''}</select></div>
      </div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="c-en" checked><span class="track"></span></span><label>Сразу включить ящики, у которых есть пароль</label></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="c-sch" ${d.global_schedule?'':'checked'}><span class="track"></span></span><label>Завести каждому своё расписание копирования</label></div>
      <div class="form-row" id="c-timerow"><label>Время копирования (каждый день)</label><input type="time" id="c-time" value="02:00"></div>
      ${d.global_schedule?'<div class="hint" style="margin-bottom:12px">🔁 Включено копирование всех ящиков по очереди — новые ящики попадут в него и без своего расписания.</div>':''}
      <div data-preview></div>`,
      {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn" data-check>Проверить</button><button class="btn primary" data-go disabled>Добавить</button>'});
    const g=x=>m.body.querySelector(x);
    const box=g('[data-preview]'), go=m.foot.querySelector('[data-go]');
    const syncTime=()=>{ g('#c-timerow').style.display=g('#c-sch').checked?'':'none'; };
    g('#c-sch').addEventListener('change', syncTime); syncTime();
    const body=(preview)=>({text:g('#c-text').value, host:g('#c-host').value.trim(), port:parseInt(g('#c-port').value||'993',10),
      security:g('#c-sec').value, auth_type:g('#c-auth').value, enable:g('#c-en').checked,
      schedule_time:g('#c-sch').checked?(g('#c-time').value||''):'', preview});
    let seq=0;
    const check=async()=>{
      const my=++seq; go.disabled=true;
      if(!g('#c-text').value.trim()){ box.innerHTML=''; return; }
      box.innerHTML='<div class="empty"><span class="spinner"></span> Проверяем список…</div>';
      try{
        const r=await api('/accounts/bulk-create',{method:'POST',body:body(true)});
        if(my!==seq) return;
        box.innerHTML=bulkResultHtml(r, true); bulkWireResults(box, r, true);
        go.disabled=!r.counts.ok; go.textContent=r.counts.ok?`Добавить ${fmtNum(r.counts.ok)} ${plural(r.counts.ok,'ящик','ящика','ящиков')}`:'Добавлять нечего';
      }catch(e){ if(my!==seq) return; box.innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}</div>`; }
    };
    let t=null;
    m.body.addEventListener('input', ()=>{ clearTimeout(t); t=setTimeout(check, 700); });
    m.body.addEventListener('change', ()=>{ clearTimeout(t); t=setTimeout(check, 200); });
    m.foot.querySelector('[data-check]').onclick=check;
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    go.onclick=async()=>{
      go.disabled=true; go.innerHTML='<span class="spinner"></span> Добавляем…';
      try{
        const r=await api('/accounts/bulk-create',{method:'POST',body:body(false)});
        box.innerHTML=`<div class="hint okbox">✅ ${esc(r.summary)}</div>`+bulkResultHtml(r, false); bulkWireResults(box, r, false);
        m.foot.innerHTML='<button class="btn primary" data-close>Готово</button>';
        m.foot.querySelector('[data-close]').onclick=()=>{ m.close(); route(); };
        g('#c-text').value='';
        toast('Ящики добавлены', r.summary);
      }catch(e){ toastErr(e); go.disabled=false; go.textContent='Добавить'; }
    };
  }).catch(toastErr);
}

// ---------- Кнопки на странице ящиков ----------
/** «Копия всех ящиков с нуля»: групповое действие над всеми включёнными ящиками. */
function rebuildAllModal(){
  const accs=State.accounts||[];
  const ids=accs.filter(a=>a.enabled).map(a=>a.id);
  if(!ids.length) return toast('Нет включённых ящиков','','warn');
  bulkDialog('rebuild_full', ids, {}, {title:'🧨 Копия всех ящиков с нуля', intro:`
    <p style="margin-top:0">Для каждого включённого ящика прежняя локальная копия будет перенесена в <b>карантин</b>
      («Прежние копии»), а все письма — скачаны с почтового сервера заново.</p>
    <ul class="small" style="margin:0 0 12px;padding-left:20px;line-height:1.6">
      <li>Письма, которых на сервере уже нет, останутся только в карантине: в просмотре, поиске и экспорте их не будет.</li>
      <li>Карантин занимает место на диске, пока вы его не удалите (Групповые действия → «Удалить прежние копии»).</li>
      <li>Все письма скачиваются заново — это долго и нагружает почтовый сервер; ящики идут через общую очередь.</li>
      <li>Ящики с удержанием архива, выключенные и без пароля пропускаются.</li>
      <li>Если нужно лишь вернуть пропавшие файлы, безопаснее «Докачать потерянные письма» — ничего не удаляется.</li>
    </ul>`});
}

/** Список ящиков файлом Excel. */
async function exportAccountList(ids){
  try{
    await apiDownload('/accounts/export-list', {ids: ids&&ids.length?ids:null, format:'xlsx'}, 'mailboxes.xlsx');
    toast('Список выгружен', ids&&ids.length?`Ящиков: ${fmtNum(ids.length)}`:'Все ящики');
  }catch(e){ toastErr(e); }
}

/** Скачать файл, который сервер отдаёт на POST-запрос. */
async function apiDownload(path, body, fallbackName){
  const res=await fetch('/api'+path, {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'fetch'}, body:JSON.stringify(body||{})});
  if(!res.ok){
    let msg='Ошибка '+res.status;
    try{ const d=await res.json(); msg=d.message||(typeof d.detail==='string'?d.detail:'')||msg; }catch(_){}
    if(res.status===401) sessionGone();
    throw new Error(msg);
  }
  const blob=await res.blob();
  const cd=res.headers.get('content-disposition')||'';
  const mm=/filename="([^"]+)"/.exec(cd);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=(mm&&mm[1])||fallbackName;
  document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}

function fieldHelp(group, key){ return (State.help && State.help[group] && State.help[group][key]) || null; }

async function accountModal(id){
  let a={name:'',host:'',port:993,username:'',password:'',security:'ssl',auth_type:'password',enabled:true,folder_exclude:[],notes:'',oauth_client_id:'',oauth_client_secret:'',oauth_refresh_token:'',oauth_token_url:''};
  if(id){ a=await api(`/accounts/${id}`); a.password=''; }
  const H=(k)=>helpIcon(fieldHelp('account',k));
  const body=`
    <div class="form-row"><label>Название ${H('name')}</label><input id="f-name" type="text" value="${esc(a.name)}"></div>
    <div class="grid cols-2">
      <div class="form-row"><label>IMAP-сервер ${H('host')}</label><input id="f-host" type="text" value="${esc(a.host)}" placeholder="imap.yandex.ru"></div>
      <div class="form-row"><label>Порт ${H('port')}</label><input id="f-port" type="number" value="${a.port}"></div>
    </div>
    <div class="grid cols-2">
      <div class="form-row"><label>Шифрование ${H('security')}</label><select id="f-sec">
        <option value="ssl"${a.security==='ssl'?' selected':''}>SSL/TLS (порт 993)</option>
        <option value="starttls"${a.security==='starttls'?' selected':''}>STARTTLS (порт 143)</option>
        <option value="plain"${a.security==='plain'?' selected':''}>Без шифрования</option></select></div>
      <div class="form-row"><label>Способ входа ${H('auth_type')}</label><select id="f-auth">
        <option value="password"${a.auth_type==='password'?' selected':''}>Логин и пароль</option>
        <option value="oauth2"${a.auth_type==='oauth2'?' selected':''}>OAuth2 (Gmail / Microsoft 365)</option>
        <option value="master"${a.auth_type==='master'?' selected':''}>Через администратора почты (без пароля ящика)</option>
        ${(a.auth_type==='axigen_ftp'||((State.user||{}).features||{}).axigen)?`<option value="axigen_ftp"${a.auth_type==='axigen_ftp'?' selected':''}>Через FTP Backup Axigen (без пароля ящика)</option>`:''}
        ${(a.auth_type==='server'||((State.user||{}).features||{}).mailservers)?`<option value="server"${a.auth_type==='server'?' selected':''}>Через подключение к почтовому серверу (без пароля ящика)</option>`:''}</select></div>
    </div>
    <div class="form-row"><label>Логин ${H('username')}</label><input id="f-user" type="text" value="${esc(a.username)}" placeholder="user@example.ru"></div>
    <div class="hint" id="axftpHint" style="display:${a.auth_type==='axigen_ftp'?'block':'none'};margin-bottom:8px">Письма берёт администратор Axigen через службу FTP Backup («Настройки → Сервер Axigen») — пароль сотрудника не нужен. Логин — полный адрес. Пароль ниже необязателен: он нужен только для восстановления по IMAP и входа сотрудника в архив.</div>
    <div id="srvAuthBox" style="display:${a.auth_type==='server'?'block':'none'}">
      <div class="form-row"><label>Подключение</label><select id="f-srv"><option value="">— загружается… —</option></select>
        <div class="hint" id="f-srv-hint">В ящик входит само подключение (токен приложения или администратор почты) — пароль сотрудника не нужен. Сервер, порт и шифрование подставляются из подключения.</div></div>
    </div>
    <div class="form-row" id="pwrow" style="display:${(a.auth_type==='password'||a.auth_type==='axigen_ftp')?'block':'none'}"><label>Пароль ${H('password')}</label><input id="f-pass" type="password" placeholder="${id?'оставьте пустым, чтобы не менять':(a.auth_type==='axigen_ftp'?'необязательно':'пароль или пароль приложения')}"></div>
    <div id="axBox"></div>
    <div id="oauthBox" style="display:${a.auth_type==='oauth2'?'block':'none'}">
      <div class="form-row"><label>OAuth2 Client ID ${H('oauth_client_id')}</label><input id="f-ocid" type="text" value="${esc(a.oauth_client_id||'')}"></div>
      <div class="form-row"><label>OAuth2 Client Secret ${H('oauth_client_secret')}</label><input id="f-ocs" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Refresh Token ${H('oauth_refresh_token')}</label><input id="f-ort" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Token URL ${H('oauth_token_url')}</label><input id="f-otu" type="text" value="${esc(a.oauth_token_url||'')}" placeholder="https://oauth2.googleapis.com/token"></div>
    </div>
    <div class="form-row"><label>Копировать только папки ${H('folder_include')}</label><input id="f-inc" type="text" value="${esc((a.folder_include||[]).join(', '))}" placeholder="пусто — все папки"></div>
    <div class="form-row"><label>Исключить папки ${H('folder_exclude')}</label><input id="f-exc" type="text" value="${esc((a.folder_exclude||[]).join(', '))}" placeholder="Спам, Корзина"></div>
    <div class="form-row"><label>Заметки</label><input id="f-notes" type="text" value="${esc(a.notes||'')}" placeholder="например: кто владелец ящика"></div>
    <div class="form-row"><label>Хранение локальных копий</label><select id="f-ret">
      <option value="-1"${(a.retention_days??-1)===-1?' selected':''}>По глобальной настройке</option>
      <option value="0"${a.retention_days===0?' selected':''}>Хранить всё (бессрочно)</option>
      <option value="3"${a.retention_days===3?' selected':''}>Последние 3 дня</option>
      <option value="7"${a.retention_days===7?' selected':''}>Последняя неделя</option>
      <option value="30"${a.retention_days===30?' selected':''}>Последние 30 дней</option>
      ${(a.retention_days>0 && ![3,7,30].includes(a.retention_days))?`<option value="${a.retention_days}" selected>Свой срок: ${a.retention_days} дн.</option>`:''}</select>
      <div class="hint">Копии старше срока удаляются ежедневно (на письма на сервере не влияет). Свой срок в днях задаётся в меню ящика → «Хранение копий».</div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="f-en" ${a.enabled?'checked':''}><span class="track"></span></span><label>Ящик включён (участвует в бэкапе)</label></div>`;
  const m=modal(id?'Редактирование ящика':'Новый почтовый ящик', body, {wide:true,
    footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn" data-test>Проверить подключение</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=(x)=>m.body.querySelector(x);
  let msConns=[];
  const srvFill=()=>{ const sel=g('#f-srv'), cur=msConns.find(x=>String(x.id)===sel.value);
    if(cur){ g('#f-host').value=cur.imap_host||''; g('#f-port').value=cur.imap_port||993; g('#f-sec').value=cur.imap_security||'ssl'; } };
  const srvLoad=async()=>{
    if(msConns.length || !(((State.user||{}).features||{}).mailservers)) return;
    try{ msConns=((await api('/mailservers/brief',{bg:true})).items||[]).filter(x=>x.imap_admin && (x.enabled || x.id===a.server_id)); }catch(e){ msConns=[]; }
    const sel=g('#f-srv');
    sel.innerHTML=msConns.length?msConns.map(x=>`<option value="${x.id}"${x.id===a.server_id?' selected':''}>${esc(x.label)} «${esc(x.name)}» — ${esc(x.imap_host||'')}</option>`).join('')
      :'<option value="">нет подключений, которые входят в ящики без паролей</option>';
    if(!a.server_id || !msConns.some(x=>x.id===a.server_id)) srvFill();
  };
  g('#f-srv').onchange=srvFill;
  g('#f-auth').onchange=()=>{ const v=g('#f-auth').value; m.body.querySelector('#oauthBox').style.display=v==='oauth2'?'block':'none'; m.body.querySelector('#pwrow').style.display=(v==='password'||v==='axigen_ftp')?'block':'none'; m.body.querySelector('#axftpHint').style.display=v==='axigen_ftp'?'block':'none';
    m.body.querySelector('#srvAuthBox').style.display=v==='server'?'block':'none'; if(v==='server') srvLoad().then(srvFill); };
  if(a.auth_type==='server') srvLoad();
  if(id && ((State.user||{}).features||{}).axigen) axigenMailboxInfo(id, g('#axBox'));
  const collect=()=>({name:g('#f-name').value,host:g('#f-host').value,port:parseInt(g('#f-port').value||'993'),username:g('#f-user').value,
     password:g('#f-pass').value,security:g('#f-sec').value,auth_type:g('#f-auth').value,enabled:g('#f-en').checked,
     folder_exclude:g('#f-exc').value.split(',').map(s=>s.trim()).filter(Boolean),
     // Эти поля обязательно отправляем: сервер сохраняет их всегда, и раньше
     // «Сохранить» (и даже «Проверить подключение») молча обнуляло белый список
     // папок и заметки у ящиков, заведённых по шаблону сотрудников.
     folder_include:g('#f-inc').value.split(',').map(s=>s.trim()).filter(Boolean),
     notes:g('#f-notes').value,
     retention_days:parseInt(g('#f-ret').value),
     oauth_client_id:g('#f-ocid')?g('#f-ocid').value:'',oauth_client_secret:g('#f-ocs')?g('#f-ocs').value:'',
     oauth_refresh_token:g('#f-ort')?g('#f-ort').value:'',oauth_token_url:g('#f-otu')?g('#f-otu').value:'',
     server_id:g('#f-auth').value==='server'&&g('#f-srv').value?parseInt(g('#f-srv').value,10):null});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=async()=>{ try{ if(id) await api(`/accounts/${id}`,{method:'PUT',body:collect()}); else await api('/accounts',{method:'POST',body:collect()}); toast('Сохранено'); m.close(); route(); }catch(e){toastErr(e);} };
  m.foot.querySelector('[data-test]').onclick=async()=>{
    const btn=m.foot.querySelector('[data-test]'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Проверка…';
    try{ let tid=id; if(!id){ const r=await api('/accounts',{method:'POST',body:collect()}); tid=r.id; }
      else { await api(`/accounts/${id}`,{method:'PUT',body:collect()}); }
      const res=await api(`/accounts/${tid}/test`,{method:'POST'});
      if(res.ok) toast('Подключение успешно', `Найдено папок: ${res.folders.length}`, 'success');
      else toast('Не удалось подключиться', res.error||'', 'error', 7000);
      if(!id){ toast('Ящик сохранён','Создан при проверке'); m.close(); route(); }
    }catch(e){toastErr(e);} finally{ btn.disabled=false; btn.textContent='Проверить подключение'; }
  };
}

async function testAccount(id){
  toast('Проверка…', 'Подключаемся к серверу', 'warn', 2000);
  try{ const res=await api(`/accounts/${id}/test`,{method:'POST'});
    if(res.ok){ const list=res.folders.map(f=>f.name).join(', '); toast('Успешно', `Папок: ${res.folders.length}. ${list.slice(0,120)}`, 'success', 6000); }
    else toast('Ошибка подключения', res.error||'', 'error', 8000);
  }catch(e){toastErr(e);}
}

/** Дата удержания для людей: «бессрочно» вместо 9999-12-31. */
function holdDate(until){ return until==='9999-12-31' ? 'бессрочно' : fmtDateShort(until+'T00:00:00'); }

/** Метки «уволен», «удерживается до …», «удержание истекло». */
function holdTags(a){
  const out=[];
  if(a.dismissed_at) out.push(`<span class="tag" title="Сотрудник уволен ${esc(fmtDate(a.dismissed_at))}${a.auto_disabled?'; копирование выключено автоматически':''}">уволен</span>`);
  if(a.on_hold) out.push(`<span class="tag ok" title="Письма ящика не удаляются по сроку хранения${a.hold_reason==='dismissed'?' (архив уволенного сотрудника)':''}">🔒 ${a.hold_until==='9999-12-31'?'бессрочно':'до '+esc(holdDate(a.hold_until))}</span>`);
  else if(a.hold_expired) out.push(`<span class="tag warn" title="Срок удержания архива прошёл — архив можно удалить (меню ящика)">удержание истекло</span>`);
  return out.join(' ');
}

function holdModal(a){
  const cur=a.hold_until||'';
  const m=modal(`Удержание архива: ${a.name}`, `
    <p class="small">Пока действует удержание, письма ящика <b>не удаляются</b> очисткой по сроку хранения — даже если у ящика
    срок «3 дня». Удобно для архива уволенного сотрудника, проверок и споров.${a.hold_reason==='dismissed'?' Сейчас архив удерживается потому, что сотрудник уволен.':''}</p>
    <div class="form-row"><label>Удерживать до</label><input type="date" id="h-date" value="${cur && cur!=='9999-12-31'?esc(cur):''}"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="h-forever" ${cur==='9999-12-31'?'checked':''}><span class="track"></span></span><label>Бессрочно</label></div>`,
    {footer:`${cur?'<button class="btn ghost" data-clear>Снять удержание</button>':''}<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const send=async(until)=>{ try{ await api(`/accounts/${a.id}/hold`,{method:'POST',body:{until}}); toast('Сохранено', until?'Удержание установлено':'Удержание снято'); m.close(); route(); }catch(e){ toastErr(e); } };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=()=>{ const f=m.body.querySelector('#h-forever').checked; const d=m.body.querySelector('#h-date').value; if(!f&&!d) return toast('Укажите дату или «Бессрочно»','','warn'); send(f?'forever':d); };
  const cl=m.foot.querySelector('[data-clear]'); if(cl) cl.onclick=async()=>{ if(await confirmDlg('Снять удержание?', 'Письма ящика снова будут удаляться по сроку хранения (если он задан).', {okText:'Снять', okClass:'danger'})) send(''); };
}

function purgeModal(a){
  const m=modal(`Удалить вместе с архивом: ${a.name}`, `
    <div class="hint" style="border-color:var(--danger)">⛔ Будут удалены ящик, его настройки, индекс и <b>все файлы писем на диске</b>
    (писем: ${fmtNum(a.messages)}, ${esc(a.bytes_h||'')}). Это необратимо; письма, которых уже нет на почтовом сервере, не восстановить.
    Копия вне сервера удалит их у себя при следующем прогоне.</div>
    <div class="form-row" style="margin-top:10px"><label>Для подтверждения введите название ящика: <b>${esc(a.name)}</b></label><input type="text" id="p-name" autocomplete="off"></div>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn danger" data-go>Удалить навсегда</button>`});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    try{ const r=await api(`/accounts/${a.id}/purge`,{method:'POST',body:{confirm_name:m.body.querySelector('#p-name').value}});
      toast('Удалено', `Файлов писем удалено: ${fmtNum(r.files)} (${fmtBytes(r.bytes)})`); m.close(); route(); }
    catch(e){ toastErr(e); }
  };
}

async function retentionModal(a){
  let cur = a.retention_days, retired = 0;
  try{ const full=await api(`/accounts/${a.id}`); cur=full.retention_days; retired=full.retired||0; }
  catch(e){ if(cur===undefined) cur=-1; }
  const label = cur===-1?'глобальная настройка':(cur===0?'хранить всё (бессрочно)':`последние ${cur} дн.`);
  const m=modal(`Хранение копий: ${a.name}`, `
    <p class="muted">Сколько дней хранить <b>локальные копии</b> писем этого ящика от текущей даты. Копии старше выбранного срока будут автоматически удаляться (ежедневная очистка). На письма на самом сервере это не влияет.</p>
    <p>Сейчас: <b>${esc(label)}</b></p>
    <div class="btn-row" style="margin:14px 0">
      <button class="btn ${cur===3?'primary':''}" data-days="3">📅 Последние 3 дня</button>
      <button class="btn ${cur===7?'primary':''}" data-days="7">🗓️ Последняя неделя</button>
      <button class="btn ${cur===0?'primary':''}" data-days="0">♾️ Хранить всё</button>
    </div>
    <div class="form-row"><label>Свой срок (дней, 0 = хранить всё, пусто = глобально)</label>
      <input id="ret-custom" type="number" min="0" placeholder="напр. 30"></div>
    ${retired?`<div class="hint" style="margin-top:12px">Отсеяно по сроку хранения: <b>${fmtNum(retired)}</b> ${plural(retired,'письмо','письма','писем')} —
      копирование их не скачивает (они старше срока) или очистка уже удалила их из архива, а на почтовом сервере они ещё лежат.
      <div style="margin-top:8px"><button class="btn sm" data-forget>Забыть отсеянные письма</button></div>
      <div class="small muted" style="margin-top:6px">Следующее копирование заново проверит даты этих писем на сервере: моложе срока — скачаются,
      старше — снова будут пропущены. Нужно, если часы сервера архива уходили вперёд или у писем на почтовом сервере были неверные даты.</div></div>`:''}`,
    {footer:`<button class="btn ghost" data-c>Закрыть</button><button class="btn primary" data-save>Сохранить свой срок</button>`});
  const fg=m.body.querySelector('[data-forget]');
  if(fg) fg.onclick=async()=>{
    if(!await confirmDlg('Забыть отсеянные письма?', `Следующее копирование ящика «${a.name}» заново проверит даты ${fmtNum(retired)} ${plural(retired,'письма','писем','писем')} на сервере. Письма моложе срока хранения скачаются.`, {okText:'Забыть', okClass:'primary'})) return;
    try{ const r=await api(`/accounts/${a.id}/retired/clear`,{method:'POST'}); toast('Готово', `Забыто: ${fmtNum(r.cleared)}. Проверка — при следующем копировании.`); m.close(); }
    catch(e){ toastErr(e); }
  };
  const apply=async(days)=>{
    try{ const r=await api(`/accounts/${a.id}/retention`,{method:'POST',body:{days:days,run_now:true}});
      toast('Сохранено', days>0?`Хранить последние ${days} дн. Старые копии очищаются ежедневно.`:(days===0?'Хранить всё':'Глобальная настройка'));
      if(r.job_id) toast('Очистка запущена','Применяется сейчас');
      m.close(); route();
    }catch(e){ toastErr(e); }
  };
  m.body.querySelectorAll('[data-days]').forEach(b=>b.onclick=()=>apply(parseInt(b.dataset.days)));
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=()=>{ const v=m.body.querySelector('#ret-custom').value; apply(v===''?-1:parseInt(v)); };
}

async function foldersModal(a){
  const acc=await api(`/accounts/${a.id}`);
  const rows=(acc.folders||[]).map(f=>`<tr><td>${esc(f.folder)}</td><td>${f.count}</td><td>${esc(f.bytes_h)}</td></tr>`).join('')
    || '<tr><td colspan="3" class="muted">Нет локальных копий. Сначала выполните бэкап.</td></tr>';
  modal(`Папки: ${a.name}`, `<div class="table-wrap"><table class="tbl"><thead><tr><th>Папка</th><th>Писем</th><th>Размер</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

// ---------- Экспорт ----------
async function exportModal(a){
  const engines=State.engines.length?State.engines:await api('/export/engines');
  const H=(k)=>helpIcon(fieldHelp('export',k));
  const opts=engines.map(e=>`<option value="${e.name}" ${e.available?'':'disabled'}>${esc(e.title)}${e.available?'':' — недоступен'}${e.experimental?' ⚗️':''}</option>`).join('');
  const body=`
    <div class="form-row"><label>Формат / движок ${H('engine')}</label><select id="e-engine">${opts}</select>
      <div class="hint" id="e-desc"></div></div>
    <div class="form-row"><label>Формат PST ${H('pst_format')}</label><select id="e-pst"><option value="unicode">Unicode — Outlook 2003 и новее</option><option value="ansi">ANSI — Outlook 97–2002 (до 2 ГБ)</option></select></div>
    <div class="grid cols-2">
      <div class="form-row"><label>Дата с ${H('date_from')}</label><input id="e-df" type="date"></div>
      <div class="form-row"><label>Дата по ${H('date_to')}</label><input id="e-dt" type="date"></div>
    </div>
    <div class="form-row"><label>Только папки (через запятую, пусто = все) ${H('folders')}</label><input id="e-folders" type="text" placeholder="INBOX, Отправленные"></div>
    <div class="hint">Файл будет доступен для скачивания в разделе «Экспорт» после завершения.</div>`;
  const m=modal(`Экспорт: ${a.name}`, body, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Начать экспорт</button>`});
  const g=x=>m.body.querySelector(x);
  // значения по умолчанию — из раздела «Настройки» → «Экспорт»
  try{
    const d=await api('/export/defaults');
    const pick=(sel,val)=>{ const el=g(sel); if(el && [...el.options].some(o=>o.value===val && !o.disabled)) el.value=val; };
    pick('#e-engine', d.engine); pick('#e-pst', d.pst_format);
  }catch(_){}
  // Встроенный движок пишет только Unicode PST — выбор формата ему не нужен.
  const upd=()=>{ const e=engines.find(x=>x.name===g('#e-engine').value); g('#e-desc').innerHTML=e?esc(e.desc)+(e.experimental?' <strong style="color:var(--warn)">Новый движок — перед массовой выгрузкой откройте пробный файл в своём Outlook.</strong>':''):''; const isPst=e&&e.fmt==='pst'&&e.name!=='native'; g('#e-pst').closest('.form-row').style.display=isPst?'block':'none'; };
  g('#e-engine').onchange=upd; upd();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const e=engines.find(x=>x.name===g('#e-engine').value);
    const body={engine:g('#e-engine').value, format:e?e.fmt:'pst', pst_format:(e&&e.name==='native')?'unicode':g('#e-pst').value,
      date_from:g('#e-df').value||null, date_to:g('#e-dt').value||null,
      folders:g('#e-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
    const btn=m.foot.querySelector('[data-go]'); if(btn.disabled) return; btn.disabled=true;
    try{ await api(`/accounts/${a.id}/export`,{method:'POST',body}); toast('Экспорт запущен','Следите за прогрессом в очереди'); m.close(); location.hash='#/jobs'; }catch(e){toastErr(e);}
    finally{ btn.disabled=false; }
  };
}

// ---------- Восстановление ----------
async function restoreModal(a){
  const H=(k)=>helpIcon(fieldHelp('restore',k));
  const body=`
    <div class="card" style="background:var(--warn-soft);border-color:var(--warn);margin-bottom:14px"><strong>♻️ Восстановление заливает письма из локальной копии обратно на IMAP-сервер.</strong><div class="small muted">Рекомендуется сначала сделать пробный прогон, а заливать — в папку с префиксом, чтобы не смешивать с текущей почтой.</div></div>
    <div class="form-row"><label>Куда восстанавливать ${H('target_mode')}</label><select id="r-mode">
      <option value="prefixed">В папки с префиксом (безопасно)</option>
      <option value="original">В исходные папки</option>
      <option value="single">Всё в одну папку</option></select></div>
    <div class="form-row" id="r-prefixrow"><label>Префикс папки ${H('target_prefix')}</label><input id="r-prefix" type="text" value="Восстановлено"></div>
    <div class="form-row" id="r-singlerow" style="display:none"><label>Имя папки</label><input id="r-single" type="text" value="Восстановлено"></div>
    <div class="form-row"><label>Только папки (пусто = все)</label><input id="r-folders" type="text" placeholder="INBOX"></div>
    ${a.auth_type==='axigen_ftp'?`<div class="form-row"><label>Как заливать</label><select id="r-via">
      <option value="ftp"${a.imap_login?'':' selected'}>Через FTP Backup Axigen — без пароля ящика (без отметок «прочитано» и «важное»)</option>
      <option value="imap"${a.imap_login?' selected':''}${a.imap_login?'':' disabled'}>По IMAP с паролем ящика${a.imap_login?'':' — пароль не задан'}</option></select>
      <div class="hint">Через FTP Backup письма заливает администратор Axigen: нужно «Разрешить изменения на сервере» в настройках Axigen. В конце MailArchiver проверяет, что сервер показывает залитые письма.</div></div>`:''}
    <div class="grid cols-2">
      <div class="form-row check"><span class="switch"><input type="checkbox" id="r-dup" checked><span class="track"></span></span><label>Пропускать дубли ${H('check_duplicates')}</label></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="r-dry"><span class="track"></span></span><label>Пробный прогон ${H('dry_run')}</label></div>
    </div>`;
  const m=modal(`Восстановление: ${a.name}`, body, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Восстановить</button>`});
  const g=x=>m.body.querySelector(x);
  g('#r-mode').onchange=()=>{ g('#r-prefixrow').style.display=g('#r-mode').value==='prefixed'?'block':'none'; g('#r-singlerow').style.display=g('#r-mode').value==='single'?'block':'none'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const mode=g('#r-mode').value;
    const body={target_mode:mode,target_prefix:g('#r-prefix').value.trim(),target_folder:g('#r-single').value.trim(),
      check_duplicates:g('#r-dup').checked,dry_run:g('#r-dry').checked,
      folders:g('#r-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
    if(g('#r-via')) body.via=g('#r-via').value;
    // Пустой префикс/имя папки — это НЕ «в исходные папки»: раньше очистка
    // поля молча заливала архив прямо в рабочий INBOX. Сервер такое тоже
    // отклоняет, здесь — понятное сообщение до отправки.
    if(mode==='prefixed' && !body.target_prefix){ toastErr({message:'Укажите префикс папки — иначе письма попадут прямо в рабочие папки ящика.'}); return; }
    if(mode==='single' && !body.target_folder){ toastErr({message:'Укажите имя папки назначения.'}); return; }
    if(mode==='original' && !body.dry_run){
      const okGo=await confirmDlg('Залить в ИСХОДНЫЕ папки?',
        `Письма из архива будут добавлены прямо в рабочие папки ящика «${a.name}». Отменить это нельзя.`);
      if(!okGo) return;
    }
    try{ await api(`/accounts/${a.id}/restore`,{method:'POST',body}); toast('Восстановление запущено', body.dry_run?'Пробный прогон':'Заливка на сервер'); m.close(); location.hash='#/jobs'; }catch(e){toastErr(e);}
  };
}

// ---------- Импорт PST ----------
/** Имя или путь для команды на сервере — как shlex.quote: без кавычек путь с пробелом
 *  или кавычкой из подсказки, вставленный в терминал, сделал бы не то, что написано. */
function shq(s){ s=String(s==null?'':s); return /^[A-Za-z0-9_@%+=:,.\/-]+$/.test(s) ? s : "'"+s.replace(/'/g,"'\\''")+"'"; }
/** Текст без точки в конце: к сообщению сервера точку ставит сама фраза (иначе выходило «..»). */
function noDot(s){ return String(s==null?'':s).replace(/[.\s]+$/,''); }

function importModal(a){
  const H=(k)=>helpIcon(fieldHelp('import',k));
  const m=modal(`Импорт .pst: ${a.name}`, `
    <p class="muted small">Письма из файла Outlook (.pst или .ost) переносятся в ящик — на почтовый сервер или сразу в локальную копию. Файл разбирает сам MailArchiver: внешние программы не нужны. Переносятся папки, письма с вложениями, флаги («прочитано», «отвечено», «помечено») и даты получения; контакты, календарь и задачи пропускаются.</p>
    <div class="form-row"><label>Откуда взять файл ${H('source')}</label><select id="i-src">
      <option value="upload">Загрузить с компьютера</option>
      <option value="server">Взять с сервера (файл уже лежит в каталоге импорта)</option></select></div>
    <div class="form-row" id="i-uprow"><label>Файл .pst или .ost</label><input id="i-file" type="file" accept=".pst,.ost">
      <div class="hint" id="i-uphint"></div></div>
    <div id="i-srvrow" style="display:none">
      <div class="form-row"><label>Файл на сервере</label><select id="i-sfile"><option value="">загружается список…</option></select></div>
      <div class="hint" id="i-srvhint" style="margin-bottom:15px"></div>
    </div>
    <div class="form-row"><label>Куда импортировать ${H('target')}</label><select id="i-target">
      <option value="imap">На почтовый сервер (IMAP)</option>
      <option value="local">В локальную копию ящика (архив)</option></select></div>
    <div class="hint" id="i-localhint" style="display:none">Письма будут записаны только в архив MailArchiver: на почтовый сервер ничего не заливается. Письма старше срока хранения ящика не записываются — если импортируете старый архив, сначала поставьте архив ящика на удержание или задайте «хранить всё».</div>
    <div class="form-row"><label>Префикс папки назначения ${H('target_prefix')}</label><input id="i-prefix" type="text" value="Импорт PST" placeholder="пусто — папки как в файле"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="i-skip" checked><span class="track"></span></span><label>Пропускать письма, которые уже есть ${H('skip_existing')}</label></div>
    <details style="margin-top:8px"><summary class="small">Дополнительно</summary>
      <div class="form-row" style="margin-top:10px"><label>Способ разбора файла ${H('engine')}</label><select id="i-engine">
        <option value="builtin">Встроенный (без внешних программ)</option>
        <option value="readpst">readpst (запасной, если установлен на сервере)</option></select></div>
      <div class="form-row"><label>Кодировка старых файлов (ANSI) ${H('ansi_codepage')}</label><select id="i-cp">
        <option value="cp1251">Windows-1251 (кириллица)</option>
        <option value="cp1252">Windows-1252 (Западная Европа)</option>
        <option value="cp1250">Windows-1250 (Центральная Европа)</option>
        <option value="cp1257">Windows-1257 (Балтия)</option>
        <option value="cp866">DOS 866 (кириллица)</option></select></div>
    </details>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Загрузить и импортировать</button>`});
  const g=x=>m.body.querySelector(x);
  g('#i-target').onchange=()=>{ g('#i-localhint').style.display=g('#i-target').value==='local'?'block':'none'; };
  const goBtn=m.foot.querySelector('[data-go]');
  const fromServer=()=>g('#i-src').value==='server';
  // Имя выбранного файла — прямо на кнопке: видно, что именно уйдёт в импорт (длинное — сокращается).
  const goText=()=>!fromServer() ? 'Загрузить и импортировать'
    : chosen ? `Импортировать «${chosen.length>30 ? chosen.slice(0,29)+'…' : chosen}»` : 'Импортировать';
  const showGo=()=>{ if(goBtn.disabled) return; goBtn.textContent=goText(); goBtn.title=(fromServer()&&chosen)?`Файл на сервере: ${chosen}`:''; };
  // Каталог импорта на сервере: файл больше предела загрузки кладут туда сами и выбирают по имени.
  let srv=null;
  // Предел загрузки, пока сервер не назвал свой (то же число — MAX_PST_UPLOAD_BYTES в api.py).
  // Раньше при неудачном запросе списка пропадали и подсказка о пределе, и проверка размера.
  let maxUpload=2*1024**3;
  // Что выбрал пользователь. Хранится отдельно от списка: список перечитывается (в том
  // числе после ошибки), и выбранным молча оказывался первый файл — уже не тот, что выбирали.
  let chosen='', lost='', loadSeq=0;
  const sel=g('#i-sfile'), code=x=>`<code>${esc(x)}</code>`;
  const showUploadLimit=()=>{ g('#i-uphint').textContent=`Через браузер загружается файл не больше ${fmtBytes(maxUpload)}. Файл крупнее положите на сервер и выберите «Взять с сервера».`; };
  const SANDBOX=' Учтите: служба под systemd не видит каталоги <code>/home</code>, <code>/root</code> и <code>/tmp</code> (в её unit-файле ProtectHome=true и PrivateTmp=true) — что бы ни лежало там и какие бы права ни стояли, для службы там пусто или закрыто. Держите файлы в другом месте, например в <code>/srv</code> или <code>/mnt</code>.';
  // Совет под причину, по которой каталог импорта недоступен: причину (problem, reason) называет сервер.
  const unavailableHtml=(owner)=>{
    const dir=srv.directory||'', real=srv.link_target||'';      // куда ведёт каталог импорта, если он ссылка
    const who=owner?` ${code(owner)}`:'', chown=owner?` && sudo chown ${shq(owner)}: ${shq(dir)}`:'';
    let advice='';
    if(srv.problem==='dangling') advice='Если каталог лежит на отдельном диске — подключите диск (<code>findmnt</code>, <code>sudo mount -a</code>).'+SANDBOX;
    else if(srv.problem==='missing') advice=`Создайте его на сервере: ${code(`sudo mkdir -p ${shq(dir)}${chown}`)}.`;
    else if(srv.problem==='not_dir') advice=`На месте каталога лежит файл: уберите или переименуйте его и создайте каталог: ${code(`sudo mkdir ${shq(dir)}${chown}`)}.`;
    else if(srv.problem==='closed') advice= !owner ? 'Проверьте права на каталог.'
      : real ? `Каталог импорта — ссылка на ${code(real)}. Дайте пользователю службы право читать этот каталог, например: ${code(`sudo setfacl -m u:${shq(owner)}:rx ${shq(real)}`)}. Отдавать его целиком (<code>chown</code>) стоит, только если он нужен одному импорту.`
      : `Отдайте каталог пользователю службы: ${code(`sudo chown ${shq(owner)}: ${shq(dir)}`)}.`;
    else if(srv.problem==='denied') advice=`Каждый каталог на пути к нему должен пускать пользователя службы${who}. Права по всему пути покажет ${code(`namei -l ${shq(real||dir)}`)}.`+(real?SANDBOX:'');
    else if(srv.problem==='overlap') advice= real
      ? `Сейчас каталог импорта — ссылка на ${code(real)}, а там лежат и файлы самой службы. Направьте ссылку на отдельный каталог, где будут только файлы .pst и .ost: ${code(`sudo mkdir -p ${shq(real+'/pst-import')}${owner?` && sudo chown ${shq(owner)}: ${shq(real+'/pst-import')}`:''} && sudo ln -sfn ${shq(real+'/pst-import')} ${shq(dir)}`)}.`+SANDBOX
      : 'Параметр <code>export.tmp_dir</code> в config.yaml направляет временные файлы службы в каталог импорта: задайте для них другой каталог (или уберите параметр) и перезапустите службу.';
    else if(srv.problem==='loop') advice='Пересоздайте ссылку так, чтобы она вела на настоящий каталог: куда ведёт каждая, покажет <code>ls -l</code>.'+SANDBOX;
    else advice=`Проверьте на сервере, что каталог существует и открыт пользователю службы${who}.`;
    return `${esc(noDot(srv.reason)||`Служба не может прочитать каталог импорта ${dir}`)}. ${advice} Затем нажмите «Обновить список».`;
  };
  const renderServerFiles=(error)=>{
    const hint=g('#i-srvhint');
    const refresh=' <button class="btn small" type="button" data-refresh>Обновить список</button>';
    // у root доступ есть к любому файлу — про chown подсказывать незачем
    const owner=(srv && srv.user && srv.user!=='root') ? srv.user : '';
    if(!srv){
      sel.innerHTML='<option value="">список не получен</option>';
      hint.innerHTML=`Не удалось получить список файлов на сервере${error?': '+esc(noDot(error)):''}. Загрузить файл с компьютера можно и без него.${refresh}`;
    }else if(srv.available===false){
      // каталог создан не службой (командой от root), это ссылка на отключённый диск и т. п.
      sel.innerHTML='<option value="">каталог импорта недоступен</option>';
      hint.innerHTML=unavailableHtml(owner)+refresh;
    }else{
      const files=srv.files||[], dir=srv.directory||'';
      const locked=files.filter(f=>f.readable===false);
      // Первой идёт пустая строка: файл выбирает человек, а не порядок в списке.
      sel.innerHTML=files.length
        ? '<option value="">— выберите файл —</option>'+files.map(f=>`<option value="${esc(f.name)}" ${f.readable===false?'disabled':''}>${esc(f.name)} — ${esc(fmtBytes(f.size))}, изменён ${esc(fmtDate(f.modified))}${f.readable===false?' — нет доступа у службы':''}</option>`).join('')
        : '<option value="">в каталоге импорта нет файлов .pst и .ost</option>';
      if(chosen && !files.some(f=>f.name===chosen && f.readable!==false)){ lost=chosen; chosen=''; }
      sel.value=chosen;
      hint.innerHTML=(lost ? `<span data-lost style="color:var(--danger)">Файла «${esc(lost)}», который был выбран, в каталоге импорта больше нет (его убрали, переименовали или закрыли для службы) — выберите файл заново.</span> ` : '')
        + (files.length
          ? `Файлы лежат на сервере в каталоге импорта ${code(dir)}. Дождитесь, пока файл скопируется на сервер целиком, — и только потом запускайте импорт.`
          : `В каталоге импорта на сервере пока нет файлов .pst и .ost. Скопируйте файл на сервер (например, командой <code>scp</code>), положите его в каталог ${code(dir)}${owner?` и отдайте его пользователю службы: ${code(`sudo chown ${shq(owner)}: ${shq(dir+'/файл.pst')}`)}`:''} — затем нажмите «Обновить список».`)
        + (locked.length && owner ? ` Файл с пометкой «нет доступа» служба прочитать не может — выполните на сервере: ${code(`sudo chown ${shq(owner)}: ${shq(dir+'/'+locked[0].name)}`)}${locked.length>1?' (и так же для остальных)':''}.` : '')
        + ' После импорта файл остаётся на месте — удалите его сами, когда он больше не нужен.' + refresh;
    }
    const again=hint.querySelector('[data-refresh]'); if(again) again.onclick=loadServerFiles;
    showUploadLimit(); showGo();
  };
  sel.onchange=sel.oninput=()=>{ chosen=sel.value; lost=''; const note=g('#i-srvhint [data-lost]'); if(note) note.remove(); showGo(); };
  async function loadServerFiles(){
    const seq=++loadSeq;
    let data=null, error='';
    try{ data=await api('/import-files'); }catch(e){ error=(e&&e.message)||String(e); }
    // Список обновляет только последний запрос: ответ на более ранний мог прийти позже
    // и вернуть в окно уже устаревший набор файлов.
    if(seq!==loadSeq) return;
    srv=data;
    if(srv && srv.max_upload_bytes) maxUpload=srv.max_upload_bytes;
    renderServerFiles(error);
  }
  g('#i-src').onchange=()=>{
    g('#i-uprow').style.display=fromServer()?'none':'block';
    g('#i-srvrow').style.display=fromServer()?'block':'none';
    showGo();
  };
  showUploadLimit();
  loadServerFiles();
  m.foot.querySelector('[data-c]').onclick=m.close;
  goBtn.onclick=async()=>{
    const server=fromServer();
    let f=null, serverFile='';
    if(server){
      serverFile=chosen=sel.value;
      if(!serverFile){
        // сам файл не выбирается никогда: что импортировать, решает только пользователь
        if(!srv) return toast('Список файлов на сервере не получен', 'Нажмите «Обновить список» под полем «Файл на сервере».', 'warn', 7000);
        if(srv.available===false) return toast('Каталог импорта на сервере недоступен', 'Причина и что с ней делать — под полем «Файл на сервере».', 'warn', 7000);
        if(!(srv.files||[]).length) return toast('В каталоге импорта нет файлов', 'Скопируйте файл .pst или .ost в каталог импорта на сервере и нажмите «Обновить список».', 'warn', 7000);
        return toast('Файл на сервере не выбран', 'Выберите файл в списке «Файл на сервере».', 'warn', 7000);
      }
    }else{
      f=g('#i-file').files[0]; if(!f) return toast('Выберите файл','','warn');
      // Не ждать загрузки гигабайтов ради отказа: предел известен заранее.
      if(f.size>maxUpload) return toast('Файл слишком большой', `Через браузер загружается не больше ${fmtBytes(maxUpload)}. Положите файл в каталог импорта на сервере и выберите «Взять с сервера».`, 'warn', 9000);
    }
    const fileName=server?serverFile:f.name;
    const target=g('#i-target').value, prefix=g('#i-prefix').value.trim();
    if(!prefix){
      // Пустой префикс — письма лягут прямо в рабочие папки ящика («Входящие» из
      // файла — во «Входящие» сотрудника или архива). Отменить это нельзя.
      const okGo=await confirmDlg('Импортировать без префикса?',
        `Папки из файла «${fileName}» будут созданы ${target==='local'?'в архиве ящика':'в ящике'} «${a.name}» под своими именами, а письма папок с совпадающими именами смешаются с ${target==='local'?'уже сохранёнными':'текущей почтой'}. Отменить это нельзя.`);
      if(!okGo) return;
    }
    // Файл с сервера передаётся именем: сам файл никуда не копируется.
    const fd=new FormData(); if(server) fd.append('server_file', serverFile); else fd.append('file', f);
    // «Без префикса» — отдельным полем: пустое значение сервер принял бы за «не указано».
    if(prefix) fd.append('target_prefix', prefix); else fd.append('no_prefix', '1');
    fd.append('target', target); fd.append('skip_existing', g('#i-skip').checked?'1':'0');
    fd.append('engine', g('#i-engine').value); fd.append('ansi_codepage', g('#i-cp').value);
    goBtn.disabled=true; goBtn.innerHTML=`<span class="spinner"></span> ${server?'Запуск…':'Загрузка…'}`;
    try{
      await api(`/accounts/${a.id}/import-pst`,{method:'POST',body:fd});
      toast('Импорт запущен', `${server?'Файл с сервера':'Файл'}: «${fileName}»`); m.close(); location.hash='#/jobs';
    }catch(e){
      // Без возврата кнопки диалог после ошибки приходилось закрывать и
      // открывать заново — кнопка оставалась серой со спиннером.
      toastErr(e); goBtn.disabled=false; showGo();
      // файл на сервере могли убрать или переименовать — список стоит перечитать
      // (выбор при этом остаётся прежним или сбрасывается на пустую строку)
      if(server) loadServerFiles();
    }
  };
}

// ===================================================================
//  Очередь и задания
// ===================================================================
async function viewJobs(c){
  const jobs=await api('/jobs?limit=80');
  c.innerHTML='';
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Очередь и задания</h2><div class="spacer"></div><span class="muted small">обновляется автоматически</span></div>`));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>#</th><th>Тип</th><th>Ящик</th><th>Статус</th><th>Прогресс</th><th>Создано</th><th></th></tr></thead><tbody id="jobsBody"></tbody></table></div>');
  c.appendChild(wrap);
  jobsRefreshAt = Date.now();
  renderJobsRows(jobs);
}
function renderJobsRows(jobs){
  const tb=$('#jobsBody'); if(!tb) return; tb.innerHTML='';
  if(!jobs.length){ tb.innerHTML='<tr><td colspan="7" class="empty">Заданий пока нет</td></tr>'; return; }
  jobs.forEach(j=>{
    const prog=jobProgressHtml(j);
    const tr=h(`<tr data-job="${j.id}">
      <td class="muted nowrap">${j.id}</td>
      <td>${esc(j.type_label)}</td>
      <td class="small">${esc(j.account_name||accName(j.account_id))||'—'}</td>
      <td><span class="badge ${j.status}">${esc(j.status_label)}</span></td>
      <td style="min-width:160px" data-prog>${prog}</td>
      <td class="small muted nowrap">${fmtDate(j.created_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm" data-info>Детали</button>
        ${(j.status==='running'||j.status==='queued') && canTouchJob(j)?`<button class="btn danger sm" data-cancel>Отмена</button>`:''}
        ${(j.status==='failed'||j.status==='cancelled'||j.status==='partial') && canTouchJob(j) && !NO_RETRY_JOBS.has(j.type)?`<button class="btn sm" data-retry>Повторить</button>`:''}
      </td></tr>`);
    tr.querySelector('[data-info]').onclick=()=>jobDetails(j.id);
    const cb=tr.querySelector('[data-cancel]'); if(cb) cb.onclick=async()=>{try{await api(`/jobs/${j.id}/cancel`,{method:'POST'});toast('Отмена запрошена');}catch(e){toastErr(e);}};
    const rb=tr.querySelector('[data-retry]'); if(rb) rb.onclick=async()=>{try{await api(`/jobs/${j.id}/retry`,{method:'POST'});toast('Возвращено в очередь');route();}catch(e){toastErr(e);}};
    tb.appendChild(tr);
  });
}
let jobsRefreshAt = 0;
/** Живое обновление очереди.
 *
 * Активные задания уже приходят в снимке по WebSocket — берём их оттуда, а
 * полный список (с завершёнными) перезапрашиваем не чаще раза в 10 секунд.
 * Раньше каждые полторы секунды уходил отдельный запрос /jobs?limit=80 и
 * таблица целиком пересобиралась: пропадало выделение, строки «прыгали».
 */
async function updateJobsLive(d){
  const active=(d && d.active_jobs) || null;
  const now=Date.now();
  if(active && now - jobsRefreshAt < 10000){
    const tb=$('#jobsBody'); if(!tb) return;
    // Обновляем только прогресс уже нарисованных строк — DOM не пересобираем.
    active.forEach(j=>{
      const row=tb.querySelector(`tr[data-job="${j.id}"]`);
      if(!row) { jobsRefreshAt = 0; return; }        // появилось новое задание — обновим целиком
      const cell=row.querySelector('[data-prog]');
      if(cell) cell.innerHTML = jobProgressHtml(j);
      const badge=row.querySelector('.badge');
      if(badge){ badge.className='badge '+j.status; badge.textContent=j.status_label; }
    });
    return;
  }
  try{ jobsRefreshAt=now; const jobs=await api('/jobs?limit=80',{bg:true}); if(State.view==='jobs') renderJobsRows(jobs); }catch(e){}
}

/** Разметка ячейки прогресса — общая для первой отрисовки и живого обновления. */
function jobProgressHtml(j){
  if(j.progress_total){
    return `<div class="progress ${j.status}"><span style="width:${j.percent}%"></span></div>`+
           `<div class="muted small">${j.percent}% ${j.progress_message?('· '+esc(j.progress_message)):''}</div>`;
  }
  return j.status==='running' ? '<span class="muted small">выполняется…</span>' : '—';
}

//: Подписи полей итога задания (остальные показываются как есть).
const RESULT_LBL={summary:'Итог', final_status:'Статус итога', total:'Всего ящиков', ok:'Успешно', partial:'Частично',
  failed:'С ошибкой', skipped:'Пропущено', messages:'Новых писем', bytes:'Скачано', left:'Не успели',
  messages_new:'Новых писем', bytes_new:'Скачано', errors:'Ошибок', messages_failed:'Сервер не отдал писем',
  messages_vanished:'Удалено на сервере во время копирования', reconnects:'Переподключений', rebuild:'Режим пересоздания',
  empty_unreadable_folders:'Пустые нечитаемые папки', container_folders:'Папки-контейнеры',
  known_unreadable_folders:'Давно нечитаемые папки', purged:'Удалено ящиков', quarantines:'Удалено прежних копий',
  files:'Файлов', freed:'Освобождено', removed:'Удалено писем', restored:'Восстановлено', missing:'Отсутствует файлов',
  corrupt:'Повреждено', unreadable:'Не читается', no_key:'Без ключа шифрования', exported:'Выгружено писем',
  path:'Файл', size:'Размер', messages_relinked:'Узнано в архиве после смены нумерации (не скачивались)',
  messages_outside_retention:'Старше срока хранения — не скачано', with_problems:'С неоткрывающимися папками',
  messages_lost:'Писем в неоткрывающихся папках', indexed:'Переиндексировано писем',
  imported:'Импортировано писем', messages_total:'Писем и других элементов в файле', duplicates:'Уже были — пропущено',
  not_mail:'Не письма (контакты, календарь…) — пропущено', outdated:'Старше срока хранения — не записано',
  lossy:'Перенесено с потерями', damaged_blocks:'Блоков файла с неверной контрольной суммой',
  target:'Куда (imap — сервер, local — локальная копия)', engine:'Способ разбора файла',
  source:'Откуда файл (upload — загружен через интерфейс, server — каталог импорта на сервере)',
  domains:'Доменов прочитано', accounts:'Учётных записей', new:'Новых учётных записей', gone:'Удалено на сервере',
  added:'Заведено ящиков в архиве', incomplete:'Прочитаны не полностью', commands:'Команд CLI',
  messages_recognized:'Уже были в архиве — не записаны', other_files:'Файлов, не похожих на письма'};
const RESULT_BYTES=new Set(['bytes','bytes_new','freed','size']);

async function jobDetails(id){
  const j=await api(`/jobs/${id}`); const ev=await api(`/jobs/${id}/events`);
  // Непрочитанные папки показываем отдельным блоком с кнопкой: так их можно
  // сразу перестать копировать, а не искать ящик и править список руками.
  const skipped=(j.result&&j.result.skipped_folders)||[];
  // Длинные списки результата показываются таблицами ниже, а не строкой JSON.
  const LISTS=new Set(['skipped_folders','items','skip_reasons','problems','rebuild_info','hosts_down','not_reached','final_status']);
  // Нули «частично / пропущено / не успели» ничего не говорят — не загромождаем ими итог.
  const HIDE_ZERO=new Set(['partial','skipped','left','messages_relinked','messages_outside_retention','messages_lost',
                           'duplicates','not_mail','outdated','lossy','damaged_blocks','messages_recognized','other_files',
                           'incomplete','added','gone']);
  const notReached=(j.result&&Array.isArray(j.result.not_reached))?j.result.not_reached:[];
  const resEntries=j.result?Object.entries(j.result).filter(([k,v])=>!LISTS.has(k) && !(HIDE_ZERO.has(k) && v===0)):[];
  const items=(j.result&&Array.isArray(j.result.items))?j.result.items:[];
  const probs=(j.result&&Array.isArray(j.result.problems))?j.result.problems:[];
  const IST={success:['ok','успешно'],partial:['warn','частично'],failed:['err','ошибка'],skipped:['','пропущен']};
  // сначала ящики с ошибкой, неполные и пропущенные — среди сотен удачных их иначе не найти
  const IRANK={failed:0, partial:1, skipped:2, success:3};
  // Что считает числовая колонка — зависит от задания.
  const NCOL={backup_all:'Новых писем', quarantine_check:'Только в прежней', quarantine_rescue:'Возвращено писем',
              folders_check:'Проблемных папок', search_reindex:'Писем'}[j.type]||'Новых писем';
  const ITEMS_TITLE={quarantine_check:'Прежние копии', quarantine_rescue:'Прежние копии'}[j.type]||'Ящики';
  const hasNum=items.some(it=>it.new!==undefined);
  const itemsBlock = items.length ? `<h3 style="margin-top:16px">${ITEMS_TITLE} (${fmtNum(items.length)})</h3>
    <div class="table-wrap" style="max-height:320px;overflow:auto"><table class="tbl"><thead><tr><th>${ITEMS_TITLE==='Ящики'?'Ящик':'Каталог'}</th><th>Итог</th>${hasNum?`<th>${NCOL}</th>`:''}<th>Подробности</th></tr></thead><tbody>
    ${items.slice().reverse().sort((a,b)=>(IRANK[a.status]??3)-(IRANK[b.status]??3)).map(it=>{ const [cls,lbl]=IST[it.status]||['',it.status]; return `<tr><td>${esc(it.name)}</td><td><span class="tag ${cls}">${esc(lbl)}</span></td>
      ${hasNum?`<td class="small">${fmtNum(it.new||0)}${it.bytes?` (${esc(fmtBytes(it.bytes))})`:''}</td>`:''}<td class="small muted">${esc(it.detail||'')}</td></tr>`; }).join('')}
    </tbody></table></div>` : '';
  const probBlock = (probs.length ? `<div class="card warn-card" style="margin:14px 0"><b>Не обработано (${probs.length})</b>
    <div class="small" style="margin-top:6px">${probs.map(esc).join('<br>')}</div></div>` : '')
    + (notReached.length ? `<div class="card warn-card" style="margin:14px 0"><b>Не дошла очередь (${fmtNum(notReached.length)})</b>
    <div class="small muted" style="margin-top:4px">В следующий проход эти ящики пойдут первыми.</div>
    <div class="small" style="margin-top:6px">${notReached.map(esc).join(', ')}</div></div>` : '');
  const res=resEntries.map(([k,v])=>{
    const label=RESULT_LBL[k]||k;
    const val=(RESULT_BYTES.has(k) && typeof v==='number') ? `${fmtBytes(v)}` : (typeof v==='object'?JSON.stringify(v):v);
    return `<div class="kv"><div class="k">${esc(label)}</div><div>${esc(val)}</div></div>`;
  }).join('');
  const evs=ev.map(e=>`<div class="l-${e.level}">${fmtDate(e.ts)} [${e.level}] ${esc(e.message)}</div>`).join('')||'<span class="muted">нет событий</span>';
  const skipBlock = skipped.length ? `
    <div class="card" style="margin:14px 0;border-color:var(--warn)">
      <b>⚠️ Копия неполная: не удалось прочитать папки (${skipped.length})</b>
      <div class="mono small" style="margin:8px 0;word-break:break-all">${skipped.map(esc).join('<br>')}</div>
      <div class="muted small">Причина у каждой папки — в журнале событий ниже. Если папку на почтовом
        сервере не восстановить, добавьте её в «Пропускать папки» — тогда копия перестанет считаться
        неполной из-за неё.</div>
      ${(j.account_id && isAdmin())?'<button class="btn small" id="jobExclude" style="margin-top:10px">🚫 Больше не копировать эти папки</button>':''}
    </div>` : '';
  const m=modal(`Задание #${j.id} — ${j.type_label}`, `
    <div class="kv"><div class="k">Статус</div><div><span class="badge ${j.status}">${esc(j.status_label)}</span></div></div>
    <div class="kv"><div class="k">Создано / завершено</div><div>${fmtDate(j.created_at)} → ${fmtDate(j.finished_at)}</div></div>
    <div class="kv"><div class="k">Попыток</div><div>${j.attempts}/${j.max_attempts}</div></div>
    ${j.error?`<div class="kv"><div class="k">Ошибка</div><div style="color:var(--danger)">${esc(j.error)}</div></div>`:''}
    ${res}
    ${j.restricted?'<div class="hint" style="margin-top:10px">Подробности этого задания (списки писем, файлов, параметры) видит администратор.</div>':''}
    ${skipBlock}
    ${probBlock}
    ${itemsBlock}
    ${j.restricted?'':`<h3 style="margin-top:16px">Журнал событий</h3><div class="log-view">${evs}</div>`}`, {wide:true});
  const ex=$('#jobExclude', m.el);
  if(ex) ex.onclick=async()=>{
    const ok=await confirmDlg('Больше не копировать эти папки?',
      `Папки (${skipped.length}) будут добавлены в «Пропускать папки» ящика. `+
      `Их письма копироваться не будут, зато копия перестанет помечаться неполной. `+
      `Убрать исключение можно в карточке ящика.`, {okText:'Добавить в исключения', okClass:'primary'});
    if(!ok) return;
    try{
      const r=await api(`/accounts/${j.account_id}/exclude-folders`,
                        {method:'POST', body:{folders:skipped}});
      toast('Папки исключены', r.added.length?`Добавлено: ${r.added.length}`:'Они уже были в списке');
      ex.disabled=true;
    }catch(e){ toastErr(e); }
  };
}

// ===================================================================
//  Экспорты (список готовых файлов)
// ===================================================================
async function viewExports(c){
  const list=await api('/exports');
  const mine = isMailbox() && State.user.account_id ? {id:State.user.account_id, name:State.user.account_name||'мой ящик'} : null;
  c.innerHTML=''; c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Готовые экспорты</h2><div class="spacer"></div>
    ${mine?'<button class="btn primary" id="expMine">📤 Выгрузить мой ящик</button>':''}</div>`));
  if(mine) $('#expMine',c).onclick=()=>exportModal(mine);
  if(!list.length){
    const how = mine ? 'Нажмите «Выгрузить мой ящик».' : 'Откройте «Почтовые ящики» → меню ящика (⋯) → «Экспорт».';
    c.appendChild(h(`<div class="card"><div class="empty"><div class="big">📤</div>Экспортов пока нет.<br>${how}</div></div>`)); return;
  }
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Ящик</th><th>Формат</th><th>Размер</th><th>Статус</th><th>Создан</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  list.forEach(e=>{
    const tr=h(`<tr>
      <td class="mono small">${esc(e.filename||'—')}</td>
      <td class="small">${esc(e.account_name||accName(e.account_id))||'—'}</td>
      <td><span class="tag">${esc(e.format)} · ${esc(e.engine)}</span></td>
      <td>${esc(e.size_h)}</td>
      <td><span class="badge ${esc(e.status)}"${e.error?` title="${esc(e.error)}"`:''}>${esc(e.status_label||STATUS_LBL[e.status]||e.status)}</span></td>
      <td class="small muted">${fmtDate(e.created_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        ${e.exists?`<a class="btn sm primary" href="/api/exports/${e.id}/download">⬇ Скачать</a>`:'<span class="muted small">файл удалён</span>'}
        ${e.can_delete===false?'':'<button class="btn danger sm" data-del>✕</button>'}</td></tr>`);
    const del=tr.querySelector('[data-del]');
    if(del) del.onclick=async()=>{ if(await confirmDlg('Удалить экспорт?','Файл будет удалён с диска.')){ try{await api(`/exports/${e.id}`,{method:'DELETE'});toast('Удалено');route();}catch(err){toastErr(err);} } };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}

// ===================================================================
//  Расписания
// ===================================================================
async function viewSchedules(c){
  const [sch, accs]=await Promise.all([api('/schedules'), api('/accounts')]); State.accounts=accs;
  c.innerHTML='';
  // Оператор видит расписания, но менять их может только администратор.
  const admin=isAdmin();
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Расписания</h2><div class="spacer"></div>
    ${admin?`<button class="btn" id="addAll" ${accs.length?'':'disabled'} title="Одно задание в заданное время копирует все включённые ящики строго по одному">🔁 Копирование всех ящиков по очереди</button>
    <button class="btn primary" id="add" ${accs.length?'':'disabled'}>+ Добавить расписание</button>`:'<span class="muted small">только просмотр — расписания меняет администратор</span>'}</div>`));
  if(admin){
    $('#add',c).onclick=()=>scheduleModal(null, accs, sch);
    $('#addAll',c).onclick=()=>scheduleModal(null, accs, sch, {job_type:'backup_all', cron_expr:'0 1 * * *'});
  }
  if(!accs.length){ c.appendChild(h(`<div class="card"><div class="empty">${admin?'Сначала добавьте хотя бы один ящик.':'Ящиков пока нет.'}</div></div>`)); return; }
  if(!sch.length){ c.appendChild(h(`<div class="card"><div class="empty"><div class="big">⏰</div>Нет расписаний.${admin?` Добавьте, чтобы бэкап шёл автоматически.<br><br>
      <span class="small">Чтобы каждую ночь копировать все ящики строго по одному — нажмите «Копирование всех ящиков по очереди».</span>`:' Их настраивает администратор.'}</div></div>`)); return; }
  // Общее расписание «все ящики по очереди» и собственные расписания ящиков вместе
  // означают, что ящики копируются дважды — предупреждаем и предлагаем убрать лишнее.
  const global=sch.filter(s=>s.job_type==='backup_all' && s.enabled);
  const ownIds=[...new Set(sch.filter(s=>s.account_id && s.job_type==='backup' && s.enabled).map(s=>s.account_id))];
  if(global.length && ownIds.length){
    const warn=h(`<div class="card warn-card" style="margin-bottom:14px">⚠️ Включено копирование всех ящиков по очереди, и при этом у
      <b>${fmtNum(ownIds.length)}</b> ${plural(ownIds.length,'ящика','ящиков','ящиков')} есть свои расписания копирования — такие ящики копируются дважды.
      ${admin?'<button class="btn sm" id="dropOwn" style="margin-left:8px">Удалить свои расписания ящиков…</button>':''}</div>`);
    c.appendChild(warn);
    const dropOwn=$('#dropOwn',warn);
    if(dropOwn) dropOwn.onclick=async()=>{ if(await bulkDialog('schedule_remove', ownIds, {which:'backup'})) route(); };
  }
  const wrap=h('<div class="card table-wrap"><table class="tbl sched-tbl"><thead><tr><th>Ящик</th><th>Задание</th><th>Когда</th><th>Последний запуск</th><th>Следующий</th><th>Вкл</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  const ordered=sch.slice().sort((a,b)=>(a.account_id?1:0)-(b.account_id?1:0));
  ordered.forEach(s=>{
    const human=s.kind==='cron'?cronHuman(s.cron_expr):null;
    let when=s.kind==='cron'?(human?`<span title="cron: ${esc(s.cron_expr)}">${esc(human)}</span>`:`cron: <code>${esc(s.cron_expr)}</code>`)
                           :`каждые ${Math.round(s.interval_seconds/60)} мин`;
    const o=s.options||{};
    if(s.job_type==='backup_all'){
      const extra=[];
      if(o.pause_seconds) extra.push(`пауза ${esc(o.pause_seconds)} с`);
      if(o.stop_at) extra.push(`не начинать после ${esc(o.stop_at)}`);
      extra.push(o.order==='name'?'по алфавиту':'давние первыми');
      when+=`<div class="small muted">${extra.join(' · ')}</div>`;
    }
    const who=s.account_id ? esc(accName(s.account_id)||('№'+s.account_id))
      : '<b>Все включённые ящики</b><div class="small muted">по очереди, ящик за ящиком</div>';
    const tr=h(`<tr>
      <td>${who}</td>
      <td><span class="tag">${esc(JOBLBL[s.job_type]||s.job_type)}</span></td>
      <td class="small">${when}</td>
      <td class="small muted nowrap">${fmtDate(s.last_run)}</td>
      <td class="small muted nowrap">${fmtDate(s.next_run)}</td>
      <td><span class="badge nowrap ${s.enabled?'success':'queued'}">${s.enabled?'да':'нет'}</span></td>
      <td style="text-align:right;white-space:nowrap">${admin?'<button class="btn sm" data-edit title="Изменить">✏️</button><button class="btn danger sm" data-del title="Удалить">✕</button>':''}</td></tr>`);
    if(admin){
      tr.querySelector('[data-edit]').onclick=()=>scheduleModal(s, accs, sch);
      tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить расписание?','')){ try{await api(`/schedules/${s.id}`,{method:'DELETE'});toast('Удалено');route();}catch(e){toastErr(e);} } };
    }
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}

/** Человеческое описание простых cron-выражений: «ежедневно в 02:00». */
function cronHuman(expr){
  const m=/^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|2-6|0,6|6,0|[0-7])$/.exec(String(expr||'').trim());
  if(!m || +m[1]>59 || +m[2]>23) return null;
  const t=`${m[2].padStart(2,'0')}:${m[1].padStart(2,'0')}`;
  const days=['воскресеньям','понедельникам','вторникам','средам','четвергам','пятницам','субботам'];
  const d={'*':'ежедневно','1-5':'по будням','2-6':'со вторника по субботу','0,6':'по выходным','6,0':'по выходным'}[m[3]] || ('по '+days[(+m[3])%7]);
  return `${d} в ${t}`;
}

function scheduleModal(s, accs, allSchedules, preset){
  const H=(k)=>helpIcon(fieldHelp('schedule',k));
  s=s||Object.assign({account_id:accs[0]?accs[0].id:null,kind:'cron',job_type:'backup',cron_expr:'0 3 * * *',interval_seconds:21600,enabled:true,options:{}}, preset||{});
  const o=s.options||{};
  const accOpts=accs.map(a=>`<option value="${a.id}" ${a.id===s.account_id?'selected':''}>${esc(a.name)}</option>`).join('');
  // Разбор времени из простого cron: «M H * * дни» — для полей «Когда» и «Время».
  const cm=/^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|0,6)$/.exec(String(s.cron_expr||'').trim());
  const when=cm?({'*':'daily','1-5':'weekdays','0,6':'weekends'}[cm[3]]):'custom';
  const time=cm?`${cm[2].padStart(2,'0')}:${cm[1].padStart(2,'0')}`:'03:00';
  const body=`
    <div class="form-row"><label>Что делать ${H('job_type')}</label><select id="s-job">
      <option value="backup_all"${s.job_type==='backup_all'?' selected':''}>Копирование ВСЕХ включённых ящиков по очереди</option>
      <option value="backup"${s.job_type==='backup'?' selected':''}>Резервное копирование одного ящика</option>
      <option value="retention"${s.job_type==='retention'?' selected':''}>Очистка (ретеншн) ящика</option>
      <option value="verify"${s.job_type==='verify'?' selected':''}>Проверка целостности ящика</option>
      ${s.job_type==='restore'?'<option value="restore" selected>Восстановление ящика</option>':''}</select></div>
    <div class="form-row" id="s-accrow"><label>Ящик</label><select id="s-acc">${accOpts}</select></div>
    <div class="hint" id="s-allhint" style="margin-bottom:14px">🔁 В заданное время запускается <b>одно задание</b>, которое копирует все включённые ящики
      <b>строго по одному</b>: следующий ящик начинается, когда закончен предыдущий. Сначала — ящики, которые копировались
      давнее всего, поэтому незаконченный проход в следующий раз продолжится с тех, до кого очередь не дошла.
      Выключенные ящики и ящики без пароля пропускаются.</div>
    <div class="form-row"><label>Тип ${H('kind')}</label><select id="s-kind">
      <option value="cron"${s.kind==='cron'?' selected':''}>В определённое время</option>
      <option value="interval"${s.kind==='interval'?' selected':''}>Через интервал</option></select></div>
    <div id="s-cronbox">
      <div class="grid cols-2">
        <div class="form-row"><label>Когда</label><select id="s-when">
          <option value="daily"${when==='daily'?' selected':''}>каждый день</option>
          <option value="weekdays"${when==='weekdays'?' selected':''}>по будням (пн–пт)</option>
          <option value="weekends"${when==='weekends'?' selected':''}>по выходным (сб, вс)</option>
          <option value="custom"${when==='custom'?' selected':''}>своё cron-выражение</option></select></div>
        <div class="form-row" id="s-timerow"><label>Время</label><input type="time" id="s-time" value="${esc(time)}"></div>
      </div>
      <div class="form-row"><label>Cron-выражение ${H('cron_expr')}</label><input id="s-cron" type="text" value="${esc(s.cron_expr||'0 3 * * *')}">
        <div class="hint">Примеры: <code>0 3 * * *</code> — ежедневно в 03:00; <code>0 */6 * * *</code> — каждые 6 часов; <code>30 2 * * 1</code> — по понедельникам в 02:30.</div></div>
    </div>
    <div class="form-row" id="s-introw"><label>Интервал (минут) ${H('interval_seconds')}</label><input id="s-int" type="number" min="1" value="${Math.round((s.interval_seconds||21600)/60)}"></div>
    <div id="s-allopts">
      <div class="grid cols-2">
        <div class="form-row"><label>Пауза между ящиками, секунд</label><input id="s-pause" type="number" min="0" max="3600" value="${esc(o.pause_seconds||0)}">
          <div class="hint">Передышка почтовому серверу. 0 — без паузы.</div></div>
        <div class="form-row"><label>Не начинать новые ящики после</label><input id="s-stop" type="time" value="${esc(o.stop_at||'')}">
          <div class="hint">Например 07:00 — к началу рабочего дня. Пусто — без ограничения.</div></div>
      </div>
      <div class="form-row"><label>Порядок ящиков</label><select id="s-order">
        <option value="oldest"${o.order!=='name'?' selected':''}>сначала те, что копировались давнее всего</option>
        <option value="name"${o.order==='name'?' selected':''}>по алфавиту</option></select></div>
      <div id="s-ownwarn"></div>
    </div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="s-en" ${s.enabled?'checked':''}><span class="track"></span></span><label>Расписание включено</label></div>`;
  const m=modal(s.id?'Редактирование расписания':'Новое расписание', body, {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=x=>m.body.querySelector(x);
  const isAll=()=>g('#s-job').value==='backup_all';
  const cronFromWhen=()=>{
    const w=g('#s-when').value; if(w==='custom') return;
    const [hh,mm]=(g('#s-time').value||'03:00').split(':');
    g('#s-cron').value=`${parseInt(mm||'0',10)} ${parseInt(hh||'0',10)} * * ${({daily:'*',weekdays:'1-5',weekends:'0,6'})[w]}`;
  };
  const sync=()=>{
    const all=isAll(), cron=g('#s-kind').value==='cron';
    g('#s-accrow').style.display=all?'none':'';
    g('#s-allhint').style.display=all?'':'none';
    g('#s-allopts').style.display=all?'':'none';
    g('#s-cronbox').style.display=cron?'':'none';
    g('#s-introw').style.display=cron?'none':'';
    g('#s-timerow').style.display=g('#s-when').value==='custom'?'none':'';
    const own=(allSchedules||[]).filter(x=>x.account_id && x.job_type==='backup' && x.enabled);
    g('#s-ownwarn').innerHTML = all && own.length ? `<div class="hint warnbox">⚠️ У ${fmtNum(new Set(own.map(x=>x.account_id)).size)} ящиков есть свои расписания копирования — вместе с этим они будут копироваться дважды.
      Уберите их: «Почтовые ящики → Групповые действия → Удалить расписания» или кнопкой на странице расписаний.</div>` : '';
  };
  g('#s-job').onchange=sync; g('#s-kind').onchange=sync;
  g('#s-when').onchange=()=>{ cronFromWhen(); sync(); };
  g('#s-time').onchange=cronFromWhen; g('#s-time').oninput=cronFromWhen;
  g('#s-cron').oninput=()=>{ g('#s-when').value='custom'; sync(); };
  if(when!=='custom') cronFromWhen();
  sync();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=async()=>{
    const all=isAll();
    const body={account_id: all?null:parseInt(g('#s-acc').value,10), job_type:g('#s-job').value, kind:g('#s-kind').value,
      cron_expr:g('#s-cron').value, interval_seconds:parseInt(g('#s-int').value||'60',10)*60, enabled:g('#s-en').checked};
    // Параметры: у «всех по очереди» — пауза и время; у остальных при правке их
    // не отправляем, сервер сохраняет прежние (раньше каждое сохранение их обнуляло).
    if(all) body.options={pause_seconds:parseInt(g('#s-pause').value||'0',10)||0, stop_at:g('#s-stop').value||'', order:g('#s-order').value};
    else if(!s.id) body.options={};
    try{ if(s.id) await api(`/schedules/${s.id}`,{method:'PUT',body}); else await api('/schedules',{method:'POST',body}); toast('Сохранено'); m.close(); route(); }catch(e){toastErr(e);}
  };
}

// ===================================================================
//  Логи
// ===================================================================
async function viewLogs(c){
  c.innerHTML=''; c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Логи</h2><div class="spacer"></div>
    <select id="logLevel" style="width:auto;padding:6px 10px"><option value="">Все уровни</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select></div>`));
  const box=h('<div class="card"><div class="log-view" id="logView"></div></div>'); c.appendChild(box);
  const load=async()=>{ try{ const lv=$('#logLevel').value; State.logLevel=lv;
    const logs=await api('/logs?limit=400'+(lv?('&level='+lv):'')); renderLogLines(logs); }catch(e){toastErr(e);} };
  $('#logLevel').onchange=load; await load();
}
function logLineHtml(l){
  const d=new Date((l.ts||0)*1000).toLocaleTimeString('ru-RU');
  return `<div class="l-${esc(l.level)}">${d} [${esc(l.level)}] ${esc(l.message)}</div>`;
}
/** append=true — живое обновление: дописываем только новые строки. Раньше
 *  каждые 1,5 с весь журнал заменялся последними 40 строками, выделение
 *  сбрасывалось, и скопировать строку было невозможно. */
function renderLogLines(logs, append){
  const v=$('#logView'); if(!v) return;
  if(!append){
    v.innerHTML=logs.map(logLineHtml).join('');
    State.logSeq = logs.length ? (logs[logs.length-1].seq||0) : 0;
    v.scrollTop=v.scrollHeight;
    return;
  }
  const fresh=logs.filter(l=>(l.seq||0) > (State.logSeq||0));
  if(!fresh.length) return;
  const atBottom = v.scrollTop + v.clientHeight >= v.scrollHeight - 30;
  v.insertAdjacentHTML('beforeend', fresh.map(logLineHtml).join(''));
  State.logSeq = fresh[fresh.length-1].seq || State.logSeq;
  while(v.childElementCount > 2000) v.removeChild(v.firstElementChild);
  if(atBottom) v.scrollTop=v.scrollHeight;
}

// ===================================================================
//  Настройки (с подсказками к каждому параметру)
// ===================================================================
const ENUM_OPTS = {
  'logging.level':[['DEBUG','DEBUG — максимум подробностей'],['INFO','INFO — обычный'],['WARNING','WARNING — предупреждения'],['ERROR','ERROR — только ошибки']],
  'export.default_engine':[['auto','Авто'],['aspose','Aspose PST'],['native','Встроенный PST'],['mbox','MBOX'],['eml','EML']],
  'export.pst_format':[['unicode','Unicode (Outlook 2003+)'],['ansi','ANSI (Outlook 97–2002)']],
  'export.outlook_target':[['2016+','2016+'],['2013','2013'],['2010','2010'],['2007','2007'],['2003','2003'],['2002','2002 и старше'],['365','365']],
  'notifications.smtp_security':[['starttls','STARTTLS'],['ssl','SSL'],['none','Без шифрования']],
  'employees.source_type':[['file','Файл на сервере'],['url','Адрес (URL)']],
  'employees.source_url_format':[['auto','Определять автоматически'],['csv','CSV'],['xlsx','Excel (XLSX)']],
  'employees.account_security':[['ssl','SSL/TLS'],['starttls','STARTTLS'],['plain','Без шифрования']],
  'replica.target':[['dir','Сетевая папка или диск'],['rsync','Другой сервер по SSH (rsync)'],['s3','S3-хранилище']],
  'mailadmin.mode':[['sasl_plain','SASL PLAIN с authzid (стандарт)'],['separator','Логин с разделителем (ящик*админ)']],
  'employees.dismissed_action':[['final_backup_disable','Последняя копия и выключить копирование'],['keep','Продолжать копировать']],
  'axigen.cli_tls':[['none','Без шифрования (только эта же машина)'],['tls','SSL-слушатель CLI']],
  'axigen.ftp_tls':[['none','Без шифрования (только эта же машина)'],['implicit','TLS сразу (SSL-слушатель, implicit)'],['explicit','AUTH TLS на обычном порту (explicit)']],
  'axigen.ftp_name_encoding':[['auto','Определять'],['utf7','UTF-7 (по умолчанию у Axigen)'],['utf8','UTF-8']],
  'axigen.new_mailbox_auth':[['axigen_ftp','Через FTP Backup (без пароля)'],['password','По IMAP с паролем']],
};
// Параметры, которые имеют смысл не всегда: показываем только когда включено то,
// от чего они зависят. Иначе в разделе «Сотрудники» рядом стоят путь к файлу и
// адрес выгрузки, и непонятно, что из этого работает.
const SETTING_DEPS = {
  'employees.source_file':          v=>v['employees.source_type']!=='url',
  'employees.source_url':           v=>v['employees.source_type']==='url',
  'employees.source_url_user':      v=>v['employees.source_type']==='url',
  'employees.source_url_password':  v=>v['employees.source_type']==='url',
  'employees.source_url_verify_ssl':v=>v['employees.source_type']==='url',
  'employees.source_url_timeout_s': v=>v['employees.source_type']==='url',
  'employees.source_url_format':    v=>v['employees.source_type']==='url',
  'employees.cron':                 v=>!!v['employees.sync_enabled'],
  'employees.account_host':          v=>!!v['employees.create_accounts'],
  'employees.account_port':          v=>!!v['employees.create_accounts'],
  'employees.account_security':      v=>!!v['employees.create_accounts'],
  'employees.account_name_template': v=>!!v['employees.create_accounts'],
  'employees.account_username_template': v=>!!v['employees.create_accounts'],
  'employees.account_notes_template':    v=>!!v['employees.create_accounts'],
  'employees.account_enabled':           v=>!!v['employees.create_accounts'],
  'employees.account_folder_include':    v=>!!v['employees.create_accounts'],
  'employees.account_folder_exclude':    v=>!!v['employees.create_accounts'],
  'employees.account_retention_days':    v=>!!v['employees.create_accounts'],
  'employees.account_schedule_enabled':  v=>!!v['employees.create_accounts'],
  'employees.account_schedule_cron': v=>!!v['employees.create_accounts'] && !!v['employees.account_schedule_enabled'],
  'replica.dir_path':       v=>v['replica.target']==='dir',
  'replica.parallel':       v=>v['replica.target']!=='rsync',
  'replica.verify_every_days': v=>v['replica.target']!=='rsync',
  'replica.rsync_dest':     v=>v['replica.target']==='rsync',
  'replica.ssh_port':       v=>v['replica.target']==='rsync',
  'replica.ssh_key_file':   v=>v['replica.target']==='rsync',
  'replica.bwlimit_kbps':   v=>v['replica.target']==='rsync',
  'replica.s3_endpoint':    v=>v['replica.target']==='s3',
  'replica.s3_region':      v=>v['replica.target']==='s3',
  'replica.s3_bucket':      v=>v['replica.target']==='s3',
  'replica.s3_prefix':      v=>v['replica.target']==='s3',
  'replica.s3_access_key':  v=>v['replica.target']==='s3',
  'replica.s3_secret_key':  v=>v['replica.target']==='s3',
  'replica.s3_path_style':  v=>v['replica.target']==='s3',
  'replica.s3_storage_class': v=>v['replica.target']==='s3',
  'replica.s3_verify_ssl':  v=>v['replica.target']==='s3',
  'replica.max_delete_percent': v=>!!v['replica.mirror_deletions'],
  'notifications.smtp_host':     v=>!!v['notifications.enabled'],
  'notifications.smtp_port':     v=>!!v['notifications.enabled'],
  'notifications.smtp_security': v=>!!v['notifications.enabled'],
  'notifications.smtp_user':     v=>!!v['notifications.enabled'],
  'notifications.smtp_password': v=>!!v['notifications.enabled'],
  'notifications.mail_from':     v=>!!v['notifications.enabled'],
  'notifications.mail_to':       v=>!!v['notifications.enabled'],
  'notifications.on_success':    v=>!!v['notifications.enabled'],
  'notifications.on_failure':    v=>!!v['notifications.enabled'],
  'notifications.summary_enabled': v=>!!v['notifications.enabled'],
  'notifications.summary_cron':  v=>!!v['notifications.enabled'] && !!v['notifications.summary_enabled'],
  'employees.account_use_master':  v=>!!v['employees.create_accounts'],
  'employees.account_use_axigen_ftp': v=>!!v['employees.create_accounts'],
  'mailadmin.host':      v=>!!v['mailadmin.enabled'],
  'mailadmin.user':      v=>!!v['mailadmin.enabled'],
  'mailadmin.password':  v=>!!v['mailadmin.enabled'],
  'mailadmin.mode':      v=>!!v['mailadmin.enabled'],
  'mailadmin.separator': v=>!!v['mailadmin.enabled'] && v['mailadmin.mode']==='separator',
  'axigen.sync_cron':     v=>!!v['axigen.enabled'] && !!v['axigen.sync_enabled'],
  'axigen.alert_queue':   v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.alert_license_days': v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.alert_quota_percent': v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.cli_cert_sha256': v=>!!v['axigen.enabled'] && v['axigen.cli_tls']==='tls',
  'axigen.ftp_cert_sha256': v=>!!v['axigen.enabled'] && v['axigen.ftp_tls']!=='none',
  'axigen.tls_verify':      v=>!!v['axigen.enabled'] && (v['axigen.cli_tls']==='tls' || v['axigen.ftp_tls']!=='none'),
  'monitoring.metrics_token':    v=>!!v['monitoring.metrics_enabled'],
  'monitoring.metrics_allowed_ips': v=>!!v['monitoring.metrics_enabled'],
  'monitoring.per_account_metrics': v=>!!v['monitoring.metrics_enabled'],
};
async function viewSettings(c){
  const data=await api('/settings');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Настройки</h2><div class="spacer"></div><button class="btn primary" id="saveAll">💾 Сохранить всё</button></div>'));
  const help=data.help.params||{};
  const changed={};
  // Текущие значения всех параметров — по ним решается, какие поля показывать.
  const cur={}; data.sections.forEach(sec=>sec.keys.forEach(k=>{ cur[sec.section+'.'+k]=(data.values[sec.section]||{})[k]; }));
  const rows={};
  const set=(full,value)=>{ changed[full]=value; cur[full]=value; applyDeps(); };
  const applyDeps=()=>{ Object.entries(SETTING_DEPS).forEach(([full,test])=>{
      const row=rows[full]; if(!row) return;
      let show=true; try{ show=test(cur); }catch(e){}
      row.style.display = show ? '' : 'none';
    }); };
  // Раздел Axigen: всё, кроме переключателя, показываем только когда он включён.
  data.sections.filter(sec=>sec.section==='axigen').forEach(sec=>sec.keys.forEach(k=>{
    const full='axigen.'+k; if(k==='enabled' || SETTING_DEPS[full]) return;
    SETTING_DEPS[full]=v=>!!v['axigen.enabled'];
  }));
  data.sections.forEach(sec=>{
    const card=h(`<div class="card" style="margin-bottom:16px"><h3>${sec.icon||''} ${esc(sec.title)}</h3><div class="settings-grid"></div></div>`);
    const grid=card.querySelector('.settings-grid');
    sec.keys.forEach(key=>{
      const full=sec.section+'.'+key; const val=(data.values[sec.section]||{})[key];
      const hp=help[full];
      const ro=(data.readonly||[]).includes(full);
      const label=`${esc(hp?hp.title:key)} ${helpIcon(hp)}${ro?' <span class="tag" title="Меняется в файле config.yaml, затем перезапуск службы">только в config.yaml</span>':(hp&&hp.restart?' <span class="tag" title="Применяется только после перезапуска службы">после перезапуска</span>':'')}`;
      let input;
      if(ro){
        const shown = typeof val==='boolean' ? (val?'да':'нет') : (Array.isArray(val)?val.join(', '):(val==null?'':val));
        input=h(`<div class="form-row"><label>${label}</label><input type="text" value="${esc(shown)}" disabled></div>`);
      } else if(typeof val==='boolean'){
        input=h(`<div class="form-row check"><span class="switch"><input type="checkbox" ${val?'checked':''}><span class="track"></span></span><label>${label}</label></div>`);
        input.querySelector('input').onchange=e=>set(full, e.target.checked);
      } else if(ENUM_OPTS[full]){
        const o=ENUM_OPTS[full].map(([v,t])=>`<option value="${v}" ${String(v)===String(val)?'selected':''}>${esc(t)}</option>`).join('');
        input=h(`<div class="form-row"><label>${label}</label><select>${o}</select></div>`);
        input.querySelector('select').onchange=e=>set(full, e.target.value);
      } else if(Array.isArray(val)){
        input=h(`<div class="form-row"><label>${label}</label><input type="text" value="${esc(val.join(', '))}"></div>`);
        input.querySelector('input').oninput=e=>set(full, e.target.value.split(',').map(s=>s.trim()).filter(Boolean));
      } else if(typeof val==='number'){
        input=h(`<div class="form-row"><label>${label}</label><input type="number" value="${val}" step="${full.includes('backoff')?'0.1':'1'}"></div>`);
        input.querySelector('input').oninput=e=>set(full, full.includes('backoff')?parseFloat(e.target.value):parseInt(e.target.value));
      } else {
        const isPw=key.includes('password')||key.includes('secret')||key.includes('token');
        const isSet=!!(data.secrets_set||{})[full];
        input=h(`<div class="form-row"><label>${label}</label><input type="${isPw?'password':'text'}" value="${esc(val==null?'':val)}" ${isPw?`placeholder="${isSet?'задан (скрыт) — пусто = без изменений':'не задан'}"`:''} autocomplete="${isPw?'new-password':'off'}"></div>`);
        input.querySelector('input').oninput=e=>set(full, e.target.value);
      }
      rows[full]=input;
      grid.appendChild(input);
    });
    c.appendChild(card);
    if(sec.section==='storage') storageEncryptionCard(card);
    if(sec.section==='replica') replicaCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('replica.')));
    if(sec.section==='notifications') summaryCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('notifications.')));
    if(sec.section==='monitoring') monitoringCard(card, rows, set, data.secrets_set||{});
    if(sec.section==='mailadmin') mailadminCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('mailadmin.')));
    if(sec.section==='search') searchCard(card);
    if(sec.section==='axigen') axigenSettingsCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('axigen.')));
  });
  applyDeps();
  // grid layout
  $$('.settings-grid',c).forEach(g=>{ g.style.display='grid'; g.style.gap='4px 24px'; g.style.gridTemplateColumns='repeat(auto-fill,minmax(300px,1fr))'; });
  $('#saveAll').onclick=async()=>{
    if(!Object.keys(changed).length) return toast('Нет изменений','','warn');
    try{
      const r=await api('/settings',{method:'PUT',body:{values:changed}});
      toast('Сохранено', `Изменено параметров: ${r.changed.length}`);
      if(r.notice) toast('Важно', r.notice, 'warn', 15000);
      if('axigen.enabled' in changed && State.user){ State.user.features=Object.assign({}, State.user.features, {axigen:!!changed['axigen.enabled']}); State.bulkActions=null; renderNav(); }
      if(Object.keys(changed).some(k=>k.startsWith('axigen.'))) State.bulkActions=null;
      const storageChanged=Object.keys(changed).some(k=>k.startsWith('storage.'));
      // сохранённое больше не «несохранённое»: кнопки проверки в карточках снова доступны
      Object.keys(changed).forEach(k=>delete changed[k]);
      if(storageChanged) route();
    }catch(e){toastErr(e);}
  };
}

/** Блок «Шифрование копии» в карточке хранилища: состояние ключа и перешифровка. */
async function storageEncryptionCard(card){
  let st;
  try{ st=await api('/storage/encryption'); }catch(e){ return; }
  const pct=st.total?Math.round(100*st.encrypted/st.total):0;
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🔒 Шифрование копии</h4>
    <div class="small">Новые письма: <b>${st.active?'шифруются':'не шифруются'}</b> ·
      зашифровано <b>${fmtNum(st.encrypted)}</b> из ${fmtNum(st.total)} (${pct}%)</div>
    <div class="small muted" style="margin-top:4px">Файл ключа: <span class="mono">${esc(st.key_path)}</span>${st.key_id?` · отпечаток <span class="mono">${esc(st.key_id)}</span>`:''}</div>
    ${st.error?`<div class="hint" style="margin-top:8px;border-color:var(--danger)">⛔ ${esc(st.error)}</div>`:''}
    ${st.active&&st.key_inside_data_dir?'<div class="hint" style="margin-top:8px;border-color:var(--warn)">Ключ лежит внутри каталога данных: он защищает письма, только если копию каталога писем уносят без него. Сохраните копию ключа отдельно (флешка в сейфе, менеджер паролей) — без неё зашифрованный архив не восстановить.</div>':''}
    <div class="btn-row" style="margin-top:10px">
      ${st.active&&st.plain?`<button class="btn sm primary" data-enc>Зашифровать уже сохранённые (${fmtNum(st.plain)})</button>`:''}
      ${!st.requested&&st.encrypted&&!st.error?`<button class="btn sm" data-dec>Расшифровать все (${fmtNum(st.encrypted)})</button>`:''}
    </div></div>`);
  card.appendChild(box);
  const run=async(mode)=>{
    const text = mode==='encrypt'
      ? 'Все сохранённые письма будут перезаписаны в зашифрованном виде (по заданию на каждый ящик). Убедитесь, что копия ключа сохранена отдельно.'
      : 'Все письма будут перезаписаны в открытом виде.';
    if(!await confirmDlg(mode==='encrypt'?'Зашифровать архив?':'Расшифровать архив?', text, {okText:'Запустить', okClass:'primary'})) return;
    try{ const r=await api('/storage/convert',{method:'POST',body:{mode}}); toast('Запущено', `Заданий: ${r.jobs.length}. Прогресс — в разделе «Очередь и задания».`); }catch(e){toastErr(e);}
  };
  const eb=box.querySelector('[data-enc]'); if(eb) eb.onclick=()=>run('encrypt');
  const db=box.querySelector('[data-dec]'); if(db) db.onclick=()=>run('decrypt');
}

const REPLICA_STATUS = {success:'успешно', partial:'с замечаниями', failed:'ошибка', cancelled:'прервано'};

/** Блок «Состояние копии» в карточке «Копия вне сервера»: итог прогона и кнопки. */
async function replicaCard(card, unsaved){
  let st;
  try{ st=await api('/replica'); }catch(e){ return; }
  const box=h('<div class="replica-box" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)"></div>');
  card.appendChild(box);
  const render=()=>{
    const last=st.last_run;
    const lbl=last?(REPLICA_STATUS[last.status]||last.status):'';
    const snaps=st.snapshots||[];
    const lastSnap=snaps[0];
    box.innerHTML=`<h4 style="margin:0 0 8px">🛰️ Состояние копии</h4>
      <div class="small">${last?`Последний прогон: <b>${fmtDate(last.finished_at)}</b> · <span class="badge ${esc(last.status)}">${esc(lbl)}</span>`:'Копия ещё ни разу не делалась.'}
        ${st.last_ok?` · последняя удачная: ${fmtDate(st.last_ok)}`:''}${st.last_verify?` · полная сверка: ${fmtDate(st.last_verify)}`:''}</div>
      ${last?`<div class="small muted" style="margin-top:4px">${esc(last.message||'')}</div>`:''}
      <div class="small muted" style="margin-top:4px">${st.enabled?(st.next_run?`Следующий прогон: ${fmtDate(st.next_run)}.`:'Расписание не работает (выключен планировщик?).'):'Копирование по расписанию выключено.'}
        Снимков базы на сервере: ${snaps.length}${lastSnap?` (последний ${fmtDate(lastSnap.created_at)}, ${fmtBytes(lastSnap.size)}${lastSnap.encrypted?', зашифрован':''})`:''}.</div>
      ${st.active_job?`<div class="hint" style="margin-top:8px">⏳ Идёт копирование — задание #${st.active_job}. Ход — в разделе «Очередь и задания».</div>`:''}
      ${last&&last.deletions_blocked?`<div class="hint" style="margin-top:8px;border-color:var(--warn)">⚠️ Удаление в копии приостановлено: прогон собирался удалить ${fmtNum(last.deletions_blocked)} файлов. Так выглядит отключившийся диск с почтой. Если удаление ожидаемо (очистка по сроку, удалённые ящики) — разрешите его.
        <div class="btn-row" style="margin-top:6px"><button class="btn sm" data-allow>Разрешить удаление и запустить</button></div></div>`:''}
      <div class="btn-row" style="margin-top:10px">
        <button class="btn sm" data-check>Проверить подключение</button>
        <button class="btn sm" data-prepare>Подготовить место</button>
        <button class="btn sm primary" data-run ${st.active_job?'disabled':''}>Запустить сейчас</button>
        <button class="btn sm" data-verify ${st.active_job?'disabled':''} title="Сравнить список файлов в копии с архивом целиком">Полная сверка</button>
        <button class="btn sm" data-snap ${st.active_snapshot_job?'disabled':''}>Снять снимок базы</button>
        ${st.target==='rsync'?'<button class="btn sm" data-sshkey>SSH-ключ службы</button>':''}
      </div>`;
    const guard=()=>{ if(unsaved()){ toast('Сначала сохраните настройки','Кнопки работают с сохранёнными настройками копии.','warn'); return true; } return false; };
    const run=async(body)=>{ if(guard()) return; try{ const r=await api('/replica/run',{method:'POST',body}); toast(r.already?'Уже идёт':'Запущено', `Задание #${r.job_id}. Ход — в разделе «Очередь и задания».`); await refresh(); }catch(e){toastErr(e);} };
    box.querySelector('[data-run]').onclick=()=>run({});
    box.querySelector('[data-verify]').onclick=()=>run({verify:true});
    const allow=box.querySelector('[data-allow]');
    if(allow) allow.onclick=async()=>{ if(await confirmDlg('Разрешить удаление в копии?', `В копии будут удалены файлы, которых больше нет в архиве (${fmtNum(last.deletions_blocked)}). Это разовое разрешение для одного прогона.`, {okText:'Разрешить', okClass:'danger'})) run({allow_mass_delete:true}); };
    box.querySelector('[data-snap]').onclick=async()=>{ try{ const r=await api('/replica/snapshot',{method:'POST'}); toast('Снимок базы', `Задание #${r.job_id}.`); await refresh(); }catch(e){toastErr(e);} };
    box.querySelector('[data-check]').onclick=async(ev)=>{
      if(guard()) return;
      const b=ev.currentTarget; b.disabled=true; const old=b.textContent; b.textContent='Проверяю…';
      try{
        const r=await api('/replica/check',{method:'POST'});
        const list=(r.checks||[]).map(c=>`<li>${c.ok?'✅':'⛔'} ${esc(c.text)}</li>`).join('');
        modal(r.ok?'Копия: связь есть':'Копия: есть проблемы', `<div class="small muted">${esc(r.target||'')}</div><ul style="margin:10px 0 0 18px;padding:0">${list}</ul>
          ${r.free_bytes!=null?`<div class="small" style="margin-top:8px">Свободно в месте копии: <b>${fmtBytes(r.free_bytes)}</b></div>`:''}
          ${r.hint?`<div class="hint" style="margin-top:10px">${esc(r.hint)}</div>`:''}`);
      }catch(e){ toastErr(e); }
      finally{ b.disabled=false; b.textContent=old; }
    };
    box.querySelector('[data-prepare]').onclick=async()=>{
      if(guard()) return;
      try{
        let r=await api('/replica/prepare',{method:'POST',body:{}});
        if(!r.ok && r.action==='not_empty'){
          if(!await confirmDlg('В месте для копии уже есть файлы', r.message+' Использовать это место? Существующие файлы вне папок mailboxes/ и db/ не трогаются.', {okText:'Использовать', okClass:'danger'})) return;
          r=await api('/replica/prepare',{method:'POST',body:{force:true}});
        }
        toast(r.ok?'Готово':'Не подготовлено', r.message, r.ok?'success':'warn', 7000);
        await refresh();
      }catch(e){ toastErr(e); }
    };
    const sk=box.querySelector('[data-sshkey]');
    if(sk) sk.onclick=async()=>{
      let key=st.ssh_public_key;
      if(!key){
        if(!await confirmDlg('Создать SSH-ключ службы?', 'Будет создан ключ ed25519 без пароля в каталоге данных (replica_ssh_key). Его открытую часть нужно добавить на сервер-получатель.', {okText:'Создать', okClass:'primary'})) return;
        try{ key=(await api('/replica/ssh-key',{method:'POST'})).public_key; st.ssh_public_key=key; }catch(e){ return toastErr(e); }
      }
      const m=modal('SSH-ключ службы', `<p class="small">Добавьте эту строку в файл <span class="mono">~/.ssh/authorized_keys</span> пользователя на сервере-получателе (одной строкой):</p>
        <textarea class="mono" readonly style="width:100%;height:90px">${esc(key)}</textarea>
        <p class="small muted">Например: <span class="mono">echo '…' &gt;&gt; ~backup/.ssh/authorized_keys</span>. Затем нажмите «Проверить подключение».</p>`,
        {footer:'<button class="btn primary" data-copy>Скопировать</button>'});
      const cb=m.el.querySelector('[data-copy]'); if(cb) cb.onclick=()=>{ navigator.clipboard.writeText(key).then(()=>toast('Скопировано'),()=>toast('Не скопировано','Выделите текст и скопируйте вручную.','warn')); };
    };
  };
  const refresh=async()=>{ try{ st=await api('/replica'); render(); }catch(e){} };
  render();
}

/** Блок «Индекс поиска»: сколько проиндексировано и кнопка перестройки. */
async function searchCard(card){
  let st; try{ st=await api('/search/status'); }catch(e){ return; }
  const pct=st.total?Math.round(100*st.indexed/st.total):100;
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🔎 Индекс поиска</h4>
    ${st.available?`<div class="small">Проиндексировано <b>${fmtNum(st.indexed)}</b> из ${fmtNum(st.total)} писем (${pct}%).${st.active_job?` Идёт индексация — задание #${st.active_job}.`:(st.pending?' Остальные проиндексируются в фоне (каждые 10 минут).':'')}</div>
      <div class="small muted" style="margin-top:4px">${st.bodies?'Ищется и по тексту писем.':'Текст писем не индексируется — поиск по теме, адресам и вложениям.'}${st.bodies_blocked_by_encryption?' Текст не индексируется, потому что включено шифрование копии.':''}</div>
      <div class="btn-row" style="margin-top:10px"><button class="btn sm" data-re>Перестроить индекс</button></div>`
    :'<div class="hint" style="border-color:var(--warn)">SQLite на этом сервере собран без FTS5 — поиск работает только по теме и отправителю.</div>'}</div>`);
  card.appendChild(box);
  const re=box.querySelector('[data-re]');
  if(re) re.onclick=async()=>{
    if(!await confirmDlg('Перестроить индекс поиска?', 'Индекс будет очищен и заполнен заново в фоне (для большого архива — часы). Нужно после смены настроек индексации текста.', {okText:'Перестроить', okClass:'primary'})) return;
    try{ const r=await api('/search/reindex',{method:'POST'}); toast('Запущено', `Задание #${r.job_id}.`); }catch(e){ toastErr(e); }
  };
}

/** Блок «Вход через администратора»: проверка и перевод ящиков. */
function mailadminCard(card, unsaved){
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🗝️ Проверка и перевод ящиков</h4>
    <div class="small muted">Сначала сохраните настройки, затем проверьте вход администратора в любой ящик этого сервера.</div>
    <div class="form-row" style="margin-top:8px"><label>Адрес ящика для проверки</label><input type="text" data-user placeholder="пусто — первый ящик этого сервера"></div>
    <div class="btn-row"><button class="btn sm primary" data-check>Проверить вход администратора</button>
      <button class="btn sm" data-to-master>Перевести ящики сервера на вход через администратора</button>
      <button class="btn sm" data-to-password>Вернуть вход по паролям</button></div></div>`);
  card.appendChild(box);
  const guard=()=>{ if(unsaved()){ toast('Сначала сохраните настройки','','warn'); return true; } return false; };
  box.querySelector('[data-check]').onclick=async(ev)=>{
    if(guard()) return;
    const b=ev.currentTarget; b.disabled=true;
    try{
      const r=await api('/mailadmin/check',{method:'POST',body:{username:box.querySelector('[data-user]').value.trim()}});
      if(r.ok) toast('Вход работает', `Администратор вошёл в ящик ${r.username}. Можно переводить ящики.`, 'success', 8000);
      else modal('Вход администратора не работает', `<p>Ящик: <b>${esc(r.username)}</b></p><div class="hint" style="border-color:var(--danger)">${esc(r.error||r.status)}</div>
        <p class="small muted">Если сервер не поддерживает вход администратора в чужой ящик (например Axigen), оставьте вход по паролям ящиков.</p>`);
    }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
  const convert=async(to)=>{
    if(guard()) return;
    const text = to==='master'
      ? 'Все ящики этого сервера со способом «Логин и пароль» будут копироваться входом администратора. Сохранённые пароли ящиков не удаляются — вернуть можно в любой момент.'
      : 'Ящики этого сервера со способом «через администратора» снова будут входить по своим паролям (у кого пароль не задан — копироваться не будут).';
    if(!await confirmDlg(to==='master'?'Перевести ящики на вход через администратора?':'Вернуть вход по паролям?', text, {okText:'Перевести', okClass:'primary'})) return;
    try{ const r=await api('/mailadmin/convert',{method:'POST',body:{to}}); toast('Готово', `Изменено ящиков: ${r.changed}`); }catch(e){ toastErr(e); }
  };
  box.querySelector('[data-to-master]').onclick=()=>convert('master');
  box.querySelector('[data-to-password]').onclick=()=>convert('password');
}

/** Блок «Еженедельная сводка»: предпросмотр и отправка сейчас. */
function summaryCard(card, unsaved){
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🗓️ Еженедельная сводка</h4>
    <div class="small muted">Письмо администраторам: ящики, требующие внимания, итоги недели, место на диске, копия вне сервера.</div>
    <div class="btn-row" style="margin-top:10px"><button class="btn sm" data-preview>Посмотреть сводку</button>
      <button class="btn sm" data-send>Отправить сейчас</button></div></div>`);
  card.appendChild(box);
  box.querySelector('[data-preview]').onclick=async()=>{
    try{
      const r=await api('/monitoring/summary');
      modal('Сводка: '+r.subject, `<pre class="mono small" style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(r.body)}</pre>
        <div class="small muted">${r.last_sent?'Последняя отправка: '+fmtDate(r.last_sent)+'. ':''}${r.next_run?'Следующая: '+fmtDate(r.next_run)+'.':'По расписанию не отправляется (уведомления или сводка выключены).'}</div>`, {wide:true});
    }catch(e){ toastErr(e); }
  };
  box.querySelector('[data-send]').onclick=async(ev)=>{
    if(unsaved()) return toast('Сначала сохраните настройки','Отправка использует сохранённые настройки SMTP.','warn');
    const b=ev.currentTarget; b.disabled=true;
    try{ const r=await api('/monitoring/summary',{method:'POST'}); toast('Сводка отправлена', r.subject); }
    catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
}

/** Блок «Как подключить мониторинг»: адрес метрик, создание токена, примеры. */
function monitoringCard(card, rows, set, secretsSet){
  const url=location.origin+'/metrics';
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">📈 Подключение</h4>
    <div class="small">Адрес метрик: <span class="mono">${esc(url)}</span>${secretsSet['monitoring.metrics_token']?' · токен задан':''}</div>
    <div class="btn-row" style="margin-top:8px"><button class="btn sm" data-token>Создать токен</button></div>
    <details style="margin-top:8px"><summary class="small">Пример для Prometheus и Zabbix</summary>
      <pre class="mono small" style="white-space:pre-wrap">scrape_configs:
  - job_name: mailarchiver
    metrics_path: /metrics
    scheme: ${esc(location.protocol.replace(':',''))}
    authorization:
      credentials: ТОКЕН
    static_configs:
      - targets: ['${esc(location.host)}']

# Пример правила тревоги: ящик не копировался больше 3 суток
- alert: MailArchiverStaleMailbox
  expr: mailarchiver_account_enabled == 1 and time() - mailarchiver_account_last_backup_timestamp_seconds > 259200
  for: 1h</pre>
      <div class="small">Zabbix: элемент «HTTP-агент», URL ${esc(url)}, заголовок <span class="mono">Authorization: Bearer ТОКЕН</span>; зависимые элементы — с предобработкой «Prometheus pattern», например <span class="mono">mailarchiver_accounts{state="stale"}</span>.</div>
    </details></div>`);
  card.appendChild(box);
  box.querySelector('[data-token]').onclick=()=>{
    const bytes=new Uint8Array(24); crypto.getRandomValues(bytes);
    const token=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
    const row=rows['monitoring.metrics_token']; const input=row&&row.querySelector('input');
    if(input){ input.type='text'; input.value=token; }
    set('monitoring.metrics_token', token);
    modal('Токен метрик', `<p class="small">Токен вставлен в поле. <b>Сохраните настройки</b> и скопируйте токен в конфигурацию Prometheus/Zabbix — после сохранения он больше не показывается.</p>
      <textarea class="mono" readonly style="width:100%;height:60px">${esc(token)}</textarea>`);
  };
}

// ===================================================================
//  Пользователи и аудит (админ)
// ===================================================================
/** Что может роль — для подсказок на странице пользователей. */
const ROLE_HELP = {
  admin:'всё: ящики, письма, настройки, пользователи, удаление',
  operator:'следит за копированием и запускает его: копия ящиков, «Докачать потерянные», проверки входа, папок и целостности, отмена и повтор этих заданий, журнал. Писем не читает, настроек не меняет, ничего не удаляет и не выгружает',
};
async function viewUsers(c){
  const users=await api('/users');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Пользователи</h2><div class="spacer"></div><button class="btn primary" id="add">+ Добавить</button></div>'));
  $('#add',c).onclick=()=>userModal();
  c.appendChild(h(`<div class="hint" style="margin-bottom:12px"><b>Администратор</b> — ${esc(ROLE_HELP.admin)}.<br>
    <b>Оператор</b> — ${esc(ROLE_HELP.operator)}.<br>
    Сотрудники входят в свой ящик по email и паролю ящика — отдельные пользователи для этого не нужны.</div>`));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Имя</th><th>Роль</th><th>2FA</th><th>Создан</th><th>Вход</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  users.forEach(u=>{
    const self=u.id===State.user.id;
    const roleCell = self ? `<span class="tag ${u.role==='admin'?'ok':''}" title="Свою роль поменять нельзя">${esc(ROLE_LABELS[u.role]||u.role)}</span>`
      : `<select data-role aria-label="Роль пользователя ${esc(u.username)}" title="Сменить роль (сеансы пользователя будут завершены)">
          <option value="admin" ${u.role==='admin'?'selected':''}>администратор</option>
          <option value="operator" ${u.role==='operator'?'selected':''}>оператор</option></select>`;
    const tr=h(`<tr><td><strong>${esc(u.username)}</strong>${u.disabled?' <span class="tag">выключен</span>':''}</td><td>${roleCell}</td>
      <td>${u.totp_enabled?'<span class="tag ok">🔐 вкл.</span>':'<span class="tag">нет</span>'}</td>
      <td class="small muted">${fmtDate(u.created_at)}</td><td class="small muted">${fmtDate(u.last_login)}</td>
      <td style="text-align:right;white-space:nowrap"><button class="btn sm" data-pw>Пароль</button><button class="btn sm" data-logout title="Завершить все открытые сеансы этого пользователя (его текущие cookie перестанут действовать)">Завершить сеансы</button>${u.totp_enabled && u.id!==State.user.id?'<button class="btn sm" data-reset2fa title="Отключить двухфакторный вход (например, пользователь потерял телефон)">Сбросить 2FA</button>':''}<button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-pw]').onclick=()=>{ const m=modal(`Новый пароль: ${u.username}`,`<div class="form-row"><label>Пароль</label><input id="np" type="password"></div><div class="hint">После смены пароля все открытые сеансы этого пользователя будут завершены.</div>`,{footer:`<button class="btn primary" data-ok>Сохранить</button>`}); m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api(`/users/${u.id}/password`,{method:'PUT',body:{password:m.body.querySelector('#np').value}});toast('Пароль изменён, сеансы завершены');m.close();}catch(e){toastErr(e);}}; };
    const r2=tr.querySelector('[data-reset2fa]');
    if(r2) r2.onclick=async()=>{ if(await confirmDlg('Сбросить двухфакторный вход?',`Пользователь «${u.username}» сможет войти по одному паролю и должен будет заново привязать приложение. Все его сеансы будут завершены.`)){ try{await api(`/users/${u.id}/2fa/reset`,{method:'POST'});toast('2FA сброшена');route();}catch(e){toastErr(e);} } };
    tr.querySelector('[data-logout]').onclick=async()=>{ if(await confirmDlg('Завершить все сеансы?',`Пользователю «${u.username}» придётся войти заново на всех устройствах.`)){ try{const r=await api(`/users/${u.id}/logout-all`,{method:'POST'});toast(`Завершено сеансов: ${r.closed}`);}catch(e){toastErr(e);} } };
    tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить пользователя?','')){ try{await api(`/users/${u.id}`,{method:'DELETE'});toast('Удалено');route();}catch(e){toastErr(e);} } };
    const rs=tr.querySelector('[data-role]');
    if(rs) rs.onchange=async()=>{
      const role=rs.value;
      const ok=await confirmDlg('Сменить роль?', `«${u.username}» станет: ${ROLE_LABELS[role]||role}. `+
        (role==='operator'?'Оператор не читает письма, не меняет настроек и ничего не удаляет. ':'Администратору доступно всё. ')+
        'Открытые сеансы пользователя будут завершены.', {okText:'Сменить', okClass:'primary'});
      if(!ok){ rs.value=u.role; return; }
      try{ await api(`/users/${u.id}/role`,{method:'POST',body:{role}}); toast('Роль изменена'); route(); }
      catch(e){ rs.value=u.role; toastErr(e); }
    };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}
function userModal(){
  const m=modal('Новый пользователь',`<div class="form-row"><label>Имя</label><input id="u-name" type="text" autocomplete="off"></div>
    <div class="form-row"><label>Пароль</label><input id="u-pass" type="password" autocomplete="new-password"></div>
    <div class="form-row"><label>Роль</label><select id="u-role">
      <option value="operator">Оператор — следит за копированием и запускает его</option>
      <option value="admin">Администратор — всё</option></select></div>
    <div class="hint small" id="u-role-help"></div>`,{footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Создать</button>`});
  const help=()=>{ const t=ROLE_HELP[m.body.querySelector('#u-role').value]||'';
    m.body.querySelector('#u-role-help').textContent=t?t.charAt(0).toUpperCase()+t.slice(1)+'.':''; };
  m.body.querySelector('#u-role').onchange=help; help();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api('/users',{method:'POST',body:{username:m.body.querySelector('#u-name').value,password:m.body.querySelector('#u-pass').value,role:m.body.querySelector('#u-role').value}});toast('Создан');m.close();route();}catch(e){toastErr(e);}};
}
async function viewAudit(c){
  const rows=await api('/audit');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Аудит действий</h2></div>'));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  rows.forEach(r=>tb.appendChild(h(`<tr><td class="small muted">${fmtDate(r.ts)}</td><td>${esc(r.user)}</td><td><span class="tag">${esc(r.action)}</span></td><td class="small">${esc(r.detail)}</td></tr>`)));
  if(!rows.length) tb.innerHTML='<tr><td colspan="4" class="empty">Записей нет</td></tr>';
  c.appendChild(wrap);
}

// ===================================================================
//  Журнал безопасности
// ===================================================================
const SEC_ACTIONS = {login:'вход', login_failed:'неверный логин или пароль', login_password_ok:'пароль верный, ждём код 2FA',
  login_2fa:'вход с кодом 2FA', login_2fa_failed:'неверный код 2FA', login_2fa_locked:'ввод кодов 2FA заблокирован',
  login_mailbox:'вход сотрудника по паролю ящика', login_mailbox_limited:'вход по ящику приостановлен',
  login_busy:'вход по ящику: сервер занят', '2fa_enabled':'2FA включена', '2fa_disabled':'2FA выключена', '2fa_reset':'2FA сброшена',
  '2fa_code_failed':'неверный код 2FA (настройки)', '2fa_disable_bad_password':'неверный пароль при отключении 2FA',
  '2fa_recovery_regenerated':'новые резервные коды', user_password:'смена пароля', user_logout_all:'завершены сеансы',
  user_disable:'пользователь отключён', user_create:'создан пользователь', user_delete:'удалён пользователь', user_role:'смена роли',
  create_admin:'создан администратор', account_logout_sessions:'завершены сеансы сотрудника', security_unblock:'снята блокировка',
  search:'поиск по почте'};
const SEC_KIND = {'':'пароль', otp:'код 2FA', imap:'пароль ящика'};

async function viewSecurity(c){
  const d=await api('/security');
  c.innerHTML='';
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Безопасность</h2><div class="spacer"></div>
    <span class="muted small">Порог: ${d.limits.max_login_attempts} неудач за ${d.limits.lockout_minutes} мин</span></div>`));
  const blocks=h(`<div class="card" style="margin-bottom:16px;${d.blocks.length?'border-color:var(--warn)':''}">
    <div class="section-title"><h3>${d.blocks.length?'⛔ Действующие блокировки':'✅ Блокировок нет'}</h3></div>
    ${d.blocks.length?'':'<div class="muted small">Никто сейчас не заблокирован защитой от подбора пароля.</div>'}</div>`);
  d.blocks.forEach(b=>{
    const row=h(`<div class="kv"><div style="flex:1">${esc(b.text)}<div class="muted small">неудач: ${b.failures}${b.last?' · последняя '+esc(fmtDate(b.last)):''}</div></div>
      <button class="btn sm">Снять блокировку</button></div>`);
    row.querySelector('button').onclick=async()=>{
      if(!await confirmDlg('Снять блокировку?', 'Записи о неудачных попытках будут удалены, и вход снова станет возможен. Делайте это, только если уверены, что попытки были ваши (или сотрудника).', {okText:'Снять', okClass:'primary'})) return;
      try{ await api('/security/unblock',{method:'POST',body:{kind:b.kind, username:b.username, ip:b.ip}}); toast('Блокировка снята'); viewSecurity(c); }catch(e){ toastErr(e); }
    };
    blocks.appendChild(row);
  });
  c.appendChild(blocks);
  const wc=d.week_counts||{};
  const n=(...keys)=>keys.reduce((s,k)=>s+(wc[k]||0),0);
  c.appendChild(h(`<div class="grid cols-4" style="margin-bottom:16px">
    <div class="card stat-card"><div class="label">Успешных входов за неделю</div><div class="value">${fmtNum(n('login','login_2fa','login_mailbox'))}</div><div class="sub">из них сотрудников: ${fmtNum(n('login_mailbox'))}</div></div>
    <div class="card stat-card"><div class="label">Неудачных входов</div><div class="value">${fmtNum(n('login_failed'))}</div><div class="sub">за 7 дней</div></div>
    <div class="card stat-card"><div class="label">Неверных кодов 2FA</div><div class="value">${fmtNum(n('login_2fa_failed','2fa_code_failed'))}</div><div class="sub">блокировок: ${fmtNum(n('login_2fa_locked'))}</div></div>
    <div class="card stat-card"><div class="label">Поисков по почте</div><div class="value">${fmtNum(n('search'))}</div><div class="sub">администраторами</div></div></div>`));
  const grid=h('<div class="grid cols-2" style="margin-bottom:16px"></div>');
  grid.appendChild(h(`<div class="card table-wrap"><div class="section-title"><h3>Адреса с неудачами (сутки)</h3></div>
    ${d.top_ips.length?`<table class="tbl"><thead><tr><th>Адрес</th><th>Неудач</th><th>Учётных записей</th><th>Последняя</th></tr></thead><tbody>
      ${d.top_ips.map(r=>`<tr><td class="mono">${esc(r.ip)}</td><td>${r.failures}</td><td>${r.users}</td><td class="small muted">${esc(fmtDate(r.last))}</td></tr>`).join('')}</tbody></table>`
      :'<div class="muted small">Неудачных попыток за сутки нет.</div>'}</div>`));
  grid.appendChild(h(`<div class="card table-wrap"><div class="section-title"><h3>Последние попытки входа</h3></div>
    ${d.recent_attempts.length?`<table class="tbl"><thead><tr><th>Время</th><th>Кто</th><th>Адрес</th><th>Что</th><th></th></tr></thead><tbody>
      ${d.recent_attempts.slice(0,50).map(r=>`<tr><td class="small muted">${esc(fmtDate(r.ts))}</td><td class="small">${esc(r.username)}</td><td class="mono small">${esc(r.ip||'')}</td><td class="small">${esc(SEC_KIND[r.kind||'']||r.kind)}</td><td>${r.success?'<span class="tag ok">успех</span>':'<span class="tag err">неудача</span>'}</td></tr>`).join('')}</tbody></table>`
      :'<div class="muted small">Попыток входа пока не было (журнал попыток хранится около суток).</div>'}</div>`));
  c.appendChild(grid);
  const ev=h(`<div class="card table-wrap"><div class="section-title"><h3>События безопасности</h3><span class="muted small">вход, 2FA, пароли, сеансы, поиск по почте</span></div>
    <table class="tbl"><thead><tr><th>Время</th><th>Пользователь</th><th>Событие</th><th>Подробности</th></tr></thead><tbody>
    ${d.events.map(r=>`<tr><td class="small muted">${esc(fmtDate(r.ts))}</td><td class="small">${esc(r.user)}</td><td><span class="tag ${/failed|locked|limited|bad_password/.test(r.action)?'err':''}">${esc(SEC_ACTIONS[r.action]||r.action)}</span></td><td class="small">${esc(r.detail||'')}</td></tr>`).join('')||'<tr><td colspan="4" class="empty">Событий нет</td></tr>'}
    </tbody></table></div>`);
  c.appendChild(ev);
}

// ===================================================================
//  Почта (просмотр писем)
// ===================================================================
const Mail = { acc:null, folder:null, offset:0, total:0, limit:50, msg:null, search:null, openAfter:null };

async function viewMail(c){
  c.innerHTML='';
  let accId = isMailbox() ? State.user.account_id : (State.mailAccount||null);
  let accounts = [];
  const head=h(`<div class="section-title"><h2 style="margin:0">📧 Почта</h2><div class="spacer"></div><span id="mailAccWrap"></span></div>`);
  c.appendChild(head);
  if(isMailbox() && accId){
    // Сотруднику — выгрузка и восстановление своего ящика прямо отсюда: меню
    // ящика в разделе «Почтовые ящики» ему недоступно.
    const mine={id:accId, name:State.user.account_name||'мой ящик'};
    const acts=h(`<span class="btn-row"><button class="btn sm" id="mbExport">📤 Выгрузить</button><button class="btn sm" id="mbRestore">♻️ Восстановить в ящик</button></span>`);
    acts.querySelector('#mbExport').onclick=()=>exportModal(mine);
    acts.querySelector('#mbRestore').onclick=()=>restoreModal(mine);
    head.querySelector('#mailAccWrap').appendChild(acts);
  }
  if(!isMailbox()){
    accounts = await api('/accounts'); State.accounts=accounts;
    if(!accounts.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">📭</div>Нет ящиков. Добавьте ящик и сделайте бэкап.</div></div>')); return; }
    if(!accId || !accounts.find(a=>a.id===accId)) accId=accounts[0].id;
    State.mailAccount=accId;
    const sel=h(`<select id="mailAcc" style="width:auto;padding:7px 12px">${accounts.map(a=>`<option value="${a.id}" ${a.id===accId?'selected':''}>${esc(a.name)}</option>`).join('')}</select>`);
    sel.onchange=()=>{ State.mailAccount=parseInt(sel.value); Mail.folder=null; Mail.offset=0; viewMail(c); };
    head.querySelector('#mailAccWrap').appendChild(sel);
  }
  Mail.acc=accId;
  const sbar=h(`<div class="mail-search">
    <input type="search" id="msq" placeholder="Поиск по письмам: слова, «фраза в кавычках», тема:, от:, вложение:" value="${esc(Mail.search?Mail.search.q:'')}">
    ${isMailbox()?'':`<select id="msScope"><option value="account">в этом ящике</option><option value="all" ${Mail.search&&Mail.search.scope==='all'?'selected':''}>во всех ящиках</option></select>`}
    <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="msAttach" ${Mail.search&&Mail.search.attach?'checked':''}> с вложениями</label>
    <button class="btn sm primary" id="msGo">🔎 Найти</button>
    <button class="btn sm ghost" id="msClear" ${Mail.search?'':'style="display:none"'}>✕ Сбросить</button>
    <span class="small muted" id="msInfo"></span></div>`);
  c.appendChild(sbar);
  const runSearch=()=>{
    const q=$('#msq',sbar).value.trim();
    if(!q){ Mail.search=null; $('#msClear',sbar).style.display='none'; loadMailMessages(accId); return; }
    Mail.search={q, scope:($('#msScope',sbar)||{}).value||'account', attach:$('#msAttach',sbar).checked, offset:0};
    $('#msClear',sbar).style.display='';
    loadSearchResults(accId);
  };
  $('#msGo',sbar).onclick=runSearch;
  $('#msq',sbar).onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); runSearch(); } };
  $('#msClear',sbar).onclick=()=>{ $('#msq',sbar).value=''; Mail.search=null; $('#msClear',sbar).style.display='none'; loadMailMessages(accId); };
  api('/search/status',{bg:true}).then(st=>{
    const info=$('#msInfo',sbar); if(!info) return;
    if(!st.available) info.textContent='Поиск только по теме и отправителю (SQLite без FTS5).';
    else if(st.pending>0) info.textContent=`Проиндексировано ${fmtNum(st.indexed)} из ${fmtNum(st.total)} писем — остальные индексируются в фоне.`;
    else if(!st.bodies) info.textContent='Поиск по теме, адресам и вложениям (текст писем не индексируется).';
  }).catch(()=>{});
  const layout=h(`<div class="mail-layout">
    <div class="mail-folders" id="mfolders"><div class="empty" style="padding:20px"><div class="spinner"></div></div></div>
    <div class="mail-list" id="mlist"><div class="empty">Выберите папку</div></div>
    <div class="mail-reader" id="mreader"><div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div></div>
  </div>`);
  c.appendChild(layout);
  await loadMailFolders(accId);
}

async function loadMailFolders(accId){
  const box=$('#mfolders'); if(!box) return;
  let data;
  try{ data=await api(`/accounts/${accId}/mailfolders`); }catch(e){ box.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  if(!data.folders.length){ box.innerHTML='<div class="empty small" style="padding:16px">Нет локальных копий.<br>Сделайте бэкап ящика.</div>'; return; }
  box.innerHTML='';
  if(!Mail.folder || !data.folders.find(f=>f.folder===Mail.folder)) Mail.folder=data.folders[0].folder;
  data.folders.forEach(f=>{
    const el=h(`<div class="mail-folder ${f.folder===Mail.folder?'active':''}" tabindex="0" role="button"><span>📁</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.folder)}</span><span class="cnt">${f.count}</span></div>`);
    el.onclick=()=>{ Mail.folder=f.folder; Mail.offset=0; Mail.search=null; const q=$('#msq'); if(q) q.value=''; const cl=$('#msClear'); if(cl) cl.style.display='none'; box.querySelectorAll('.mail-folder').forEach(x=>x.classList.remove('active')); el.classList.add('active'); loadMailMessages(accId); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  });
  if(Mail.search) loadSearchResults(accId); else loadMailMessages(accId);
  // письмо, открытое из отчёта аналитики
  if(Mail.openAfter && Mail.openAfter.acc===accId){ const id=Mail.openAfter.id; Mail.openAfter=null; loadMailMessage(accId, id); }
}

/** Выделение найденного: сервер помечает его символами \x02…\x03. */
function markSnippet(text){ return esc(text||'').replace(/\u0002/g,'<mark>').replace(/\u0003/g,'</mark>'); }

async function loadSearchResults(accId, append){
  const box=$('#mlist'); if(!box || !Mail.search) return;
  const S=Mail.search;
  if(!append){ box.innerHTML='<div class="empty" style="padding:20px"><div class="spinner"></div></div>'; }
  let data;
  const qs=new URLSearchParams({q:S.q, scope:S.scope, offset:String(S.offset||0), limit:'50'});
  if(S.scope!=='all') qs.set('account_id', String(accId));
  if(S.attach) qs.set('attach','true');
  try{ data=await api('/search?'+qs.toString()); }
  catch(e){ box.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  if(!append){
    box.innerHTML='';
    box.appendChild(h(`<div style="padding:8px 12px;border-bottom:1px solid var(--border);font-size:.82rem" class="muted">
      Найдено: ${data.results.length}${data.more?'+':''} · ${S.scope==='all'?'во всех ящиках':'в этом ящике'}</div>`));
  }
  const old=box.querySelector('#msMore'); if(old) old.remove();
  if(!data.results.length && !append){ box.appendChild(h('<div class="empty">Ничего не найдено</div>')); return; }
  data.results.forEach(r=>{
    const el=h(`<div class="msg-item" tabindex="0" role="button">
      ${S.scope==='all'?`<div class="msg-acc">${esc(r.account_name||'')} · ${esc(r.folder)}</div>`:`<div class="msg-acc">${esc(r.folder)}</div>`}
      <div class="msg-top"><span class="msg-from">${esc(r.from||'—')}</span><span>${r.has_attach?'📎':''}</span><span>${esc(fmtDateShort(r.date))}</span></div>
      <div class="msg-subj">${esc(r.subject||'(без темы)')}</div>
      ${r.snippet?`<div class="msg-snip">${markSnippet(r.snippet)}</div>`:''}</div>`);
    el.onclick=()=>{ box.querySelectorAll('.msg-item').forEach(x=>x.classList.remove('active')); el.classList.add('active'); loadMailMessage(r.account_id, r.id); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  });
  if(data.more){
    const more=h('<div style="padding:10px;text-align:center"><button class="btn sm" id="msMore">Показать ещё</button></div>');
    more.querySelector('button').onclick=()=>{ S.offset=(S.offset||0)+50; loadSearchResults(accId, true); };
    box.appendChild(more);
  }
}

async function loadMailMessages(accId){
  const box=$('#mlist'); if(!box) return;
  box.innerHTML='<div class="empty" style="padding:20px"><div class="spinner"></div></div>';
  let data;
  try{ data=await api(`/accounts/${accId}/messages?folder=${encodeURIComponent(Mail.folder)}&limit=${Mail.limit}&offset=${Mail.offset}`); }
  catch(e){ box.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  Mail.total=data.total;
  box.innerHTML='';
  const bar=h(`<div style="padding:8px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:.82rem" class="muted">
    <span>${fmtNum(data.total)} ${plural(data.total||0,'письмо','письма','писем')}</span><span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" ${Mail.offset<=0?'disabled':''} data-prev>←</button>
    <span>${Math.floor(Mail.offset/Mail.limit)+1}/${Math.max(1,Math.ceil(data.total/Mail.limit))}</span>
    <button class="btn ghost sm" ${Mail.offset+Mail.limit>=data.total?'disabled':''} data-next>→</button></div>`);
  box.appendChild(bar);
  bar.querySelector('[data-prev]').onclick=()=>{ Mail.offset=Math.max(0,Mail.offset-Mail.limit); loadMailMessages(accId); };
  bar.querySelector('[data-next]').onclick=()=>{ Mail.offset+=Mail.limit; loadMailMessages(accId); };
  if(!data.messages.length){ box.appendChild(h('<div class="empty">Папка пуста</div>')); return; }
  data.messages.forEach(m=>{
    const el=h(`<div class="msg-item ${m.seen?'':'unread'}" data-id="${m.id}" tabindex="0" role="button">
      <div class="msg-top"><span class="msg-from">${m.flagged?'⭐ ':''}${m.answered?'↩ ':''}${esc(m.from||'—')}</span><span>${m.has_attach?'📎':''}</span><span>${esc(fmtDateShort(m.date))}</span></div>
      <div class="msg-subj">${esc(m.subject)}</div>
      <div class="muted small">${esc(m.size_h)}</div></div>`);
    el.onclick=()=>{ box.querySelectorAll('.msg-item').forEach(x=>x.classList.remove('active')); el.classList.add('active'); el.classList.remove('unread'); loadMailMessage(accId, m.id); };
    el.onkeydown=(e)=>{
      if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); }
      else if(e.key==='ArrowDown' && el.nextElementSibling){ e.preventDefault(); el.nextElementSibling.focus(); }
      else if(e.key==='ArrowUp' && el.previousElementSibling && el.previousElementSibling.classList.contains('msg-item')){ e.preventDefault(); el.previousElementSibling.focus(); }
    };
    box.appendChild(el);
  });
}
// Политика безопасности для HTML письма. Внешние ресурсы блокирует сам
// браузер, а не регулярные выражения: прежний фильтр пропускал дюжину вариантов
// разметки (src без кавычек, srcset, <picture>, SVG <image>, @import, <link>…),
// и трекер узнавал об открытии письма.
const MAIL_CSP_STRICT = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";
const MAIL_CSP_IMAGES = "default-src 'none'; img-src data: https: http:; style-src 'unsafe-inline'; font-src data:";
const REMOTE_RE = /(^|[\s,("'=])(?:https?:)?\/\/[^\s"'<>)]/i;

/** HTML письма для iframe srcdoc: своя CSP, ссылки — в новую вкладку, без
 *  meta refresh/base/скриптов. remote — есть ли в письме внешние ресурсы. */
function mailSrcdoc(html, allowImages){
  const raw=String(html||'');
  let body=raw, remote=REMOTE_RE.test(raw), doctype='';
  try{
    const doc=new DOMParser().parseFromString(raw, 'text/html');
    // meta http-equiv (refresh уводил бы окно письма на чужой сайт), свой base
    // и активное содержимое удаляем: песочница и CSP их и так не пустят.
    doc.querySelectorAll('meta[http-equiv], base, script, noscript, iframe, frame, frameset, object, embed, applet')
       .forEach(el=>el.remove());
    remote=false;
    for(const el of doc.querySelectorAll('[src],[srcset],[background],[poster],[href],[style],style,image,use,link,input')){
      if(el.tagName==='A' || el.tagName==='AREA') continue;       // ссылки — не ресурсы
      const vals=[el.getAttribute('src'), el.getAttribute('srcset'), el.getAttribute('background'),
                  el.getAttribute('poster'), el.getAttribute('href'), el.getAttribute('xlink:href'),
                  el.getAttribute('style'), el.tagName==='STYLE'?el.textContent:''];
      if(vals.some(v=>v && REMOTE_RE.test(' '+v))){ remote=true; break; }
    }
    if(doc.doctype) doctype=new XMLSerializer().serializeToString(doc.doctype);
    body=doc.documentElement.outerHTML;
  }catch(e){ /* не разобралось — остаётся исходный текст под той же CSP */ }
  const csp = allowImages ? MAIL_CSP_IMAGES : MAIL_CSP_STRICT;
  const head = `<meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="referrer" content="no-referrer"><base target="_blank">`;
  return {srcdoc: doctype + head + body, remote};
}

function fmtDateShort(iso){ if(!iso) return ''; try{ const d=new Date(iso); return d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit',year:'2-digit'}); }catch(e){ return ''; } }

async function loadMailMessage(accId, pk){
  const box=$('#mreader'); if(!box) return;
  box.innerHTML='<div class="empty" style="padding:40px"><div class="spinner"></div></div>';
  let m;
  try{ m=await api(`/accounts/${accId}/messages/${pk}`); }catch(e){ box.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
  const hd=m.headers||{};
  // Картинки, уже показанные в тексте письма (cid:), в списке вложений не дублируем.
  const visibleAtts=(m.attachments||[]).filter(a=>!a.inline);
  const atts=visibleAtts.map(a=>`<a class="attach-chip" href="/api/accounts/${accId}/messages/${pk}/attachment/${a.index}" download>📎 ${esc(a.filename)} <span class="muted">(${fmtBytes(a.size)})</span></a>`).join('');
  const hasText=!!(m.text&&m.text.trim()), hasHtml=!!(m.html&&m.html.trim());
  box.innerHTML='';
  const el=h(`<div>
    <div class="rhead">
      <div class="rsubj">${esc(hd.subject||'(без темы)')}</div>
      <div class="rmeta"><b>От:</b> ${esc(hd.from||'—')}</div>
      <div class="rmeta"><b>Кому:</b> ${esc(hd.to||'—')}</div>
      ${hd.cc?`<div class="rmeta"><b>Копия:</b> ${esc(hd.cc)}</div>`:''}
      <div class="rmeta"><b>Дата:</b> ${esc(hd.date||'')}</div>
      <div style="margin-top:10px" class="btn-row">
        <a class="btn sm" href="/api/accounts/${accId}/messages/${pk}/raw" download>⬇ Скачать .eml</a>
      </div>
    </div>
    ${(m.truncated||m.notice)?`<div class="hint" style="margin-bottom:12px;border-color:var(--warn)">📦 ${esc(m.notice||'Письмо слишком большое для показа в браузере.')}</div>`:''}
    ${atts?`<div style="margin-bottom:14px"><div class="muted small" style="margin-bottom:4px">Вложения (${visibleAtts.length}):</div>${atts}</div>`:''}
    ${(hasText&&hasHtml)?`<div class="body-tabs"><button class="btn sm primary" data-tab="text">Текст</button><button class="btn sm" data-tab="html">HTML</button></div>`:''}
    <div id="mbody"></div>
  </div>`);
  box.appendChild(el);
  const bodyBox=el.querySelector('#mbody');
  const showText=()=>{ bodyBox.innerHTML=''; bodyBox.appendChild(h(`<div class="mail-body-text">${esc(m.text||'(пустое тело)')}</div>`)); };
  // Внешние картинки в письме по умолчанию НЕ грузим: это трекинг-пиксели,
  // по которым отправитель узнаёт, что архивное письмо открыли, когда и с
  // какого адреса. Показываем по явной кнопке.
  let imagesAllowed=false;
  const showHtml=()=>{
    bodyBox.innerHTML='';
    const doc=mailSrcdoc(m.html, imagesAllowed);
    if(!imagesAllowed && doc.remote){
      const bar=h(`<div class="hint" style="margin-bottom:8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span>🚫 Внешние картинки заблокированы — они сообщают отправителю, что письмо открыли.</span>
        <button class="btn small" id="showImgs">Показать картинки</button></div>`);
      bar.querySelector('#showImgs').onclick=()=>{ imagesAllowed=true; showHtml(); };
      bodyBox.appendChild(bar);
    }
    const f=document.createElement('iframe'); f.className='mail-body-frame';
    // Без allow-scripts и allow-same-origin: письмо не может выполнить код и
    // обратиться к API. allow-popups — чтобы ссылки открывались в новой вкладке
    // (раньше щелчок по ссылке ничего не делал).
    f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox');
    f.setAttribute('referrerpolicy','no-referrer');
    f.srcdoc=doc.srcdoc; bodyBox.appendChild(f);
  };
  if(m.truncated){ /* тело не разбиралось — показываем только шапку и подсказку */ }
  else if(hasText) showText(); else if(hasHtml) showHtml(); else showText();
  el.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{ el.querySelectorAll('[data-tab]').forEach(x=>x.className='btn sm'); b.className='btn sm primary'; b.dataset.tab==='html'?showHtml():showText(); });
}

// ===================================================================
//  Аналитика — набор графиков на canvas (без внешних библиотек)
// ===================================================================
const CHART_COLORS = ['#2f6fed','#1f9d55','#d98a00','#d64545','#2b8ca6','#7c5cff','#e0567f','#2bb0a6','#b0862e','#8e6bd8','#3aa0a0','#c76b3a'];
const STATUS_LBL = {queued:'в очереди',running:'выполняется',success:'успешно',failed:'ошибка',cancelled:'отменено',partial:'частично'};
// Задания, которые меняли почтовый сервер: «Повторить» у них нет — повтор вслепую изменил бы ещё раз и
// уже изменённые ящики. Их запускают заново из раздела, с проверкой.
const NO_RETRY_JOBS = new Set(['axigen_action','server_action']);
const JOBLBL = {backup:'Резервное копирование',restore:'Восстановление',export:'Экспорт',import_pst:'Импорт PST',test:'Проверка подключения',retention:'Очистка (ретеншн)',verify:'Проверка целостности',analyze:'Глубокий анализ писем',sync_employees:'Синхронизация сотрудников',storage_convert:'Шифрование копии',check_logins:'Проверка паролей',replicate:'Копия вне сервера',db_snapshot:'Снимок базы',search_index:'Индексация поиска',dedup_report:'Отчёт об одинаковых вложениях',cleanup:'Удаление архивов и прежних копий',backup_all:'Копирование всех ящиков по очереди',quarantine_check:'Сравнение прежней копии с новой',quarantine_rescue:'Возврат писем из прежней копии',folders_check:'Проверка папок на сервере',search_reindex:'Переиндексация поиска',axigen_sync:'Сверка с сервером Axigen',axigen_action:'Действия на сервере Axigen',server_action:'Действия с ящиками на сервере',server_sync:'Сверка с почтовым сервером'};
function cvar(n,f){ const v=getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v||f; }
function fmtNum(n){ return Number(n||0).toLocaleString('ru-RU'); }
function trunc(s,n){ s=String(s==null?'':s); return s.length>n? s.slice(0,n-1)+'…':s; }
function hexA(hex,a){ hex=String(hex).replace('#',''); if(hex.length===3)hex=hex.split('').map(c=>c+c).join(''); const r=parseInt(hex.slice(0,2),16),g=parseInt(hex.slice(2,4),16),b=parseInt(hex.slice(4,6),16); return `rgba(${r||0},${g||0},${b||0},${a})`; }
function niceStep(v){ const p=Math.pow(10,Math.floor(Math.log10(v))); const n=v/p; let m; if(n<=1)m=1;else if(n<=2)m=2;else if(n<=2.5)m=2.5;else if(n<=5)m=5;else m=10; return m*p; }
/** Верх оси для 4 делений с «круглым» ЦЕЛЫМ шагом: при малых значениях
 *  прежний расчёт давал подписи «1, 1, 1, 0» (0,75 и 0,5 округлялись до 1). */
function niceMax(v){ v=Math.max(1,v); const step=Math.max(1, Math.ceil(niceStep(v/4))); return step*4; }
function rr(ctx,x,y,w,h,r){ if(h<0){y+=h;h=-h;} r=Math.max(0,Math.min(r,h/2,w/2)); ctx.beginPath(); ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r); ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath(); }
function cEmpty(ctx,W,H){ ctx.fillStyle=cvar('--text-dim','#888'); ctx.textAlign='center'; ctx.font='13px sans-serif'; ctx.fillText('Нет данных',W/2,H/2); }
function drawInto(container, height, fn){
  container.innerHTML=''; const cv=document.createElement('canvas'); container.appendChild(cv);
  const dpr=window.devicePixelRatio||1;
  const cssW=Math.max(220, container.clientWidth||container.parentElement.clientWidth||600), cssH=height;
  cv.style.width='100%'; cv.style.height=cssH+'px'; cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr);
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  fn(ctx, cssW, cssH);
}
function chartVBars(container, items, opts={}){
  const H=opts.height||220, color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const padL=40,padR=10,padT=12,padB=opts.rotate?52:26, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=niceMax(Math.max(1,...items.map(d=>d.value)));
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(fmtNum(Math.round(max*(4-i)/4)),padL-5,y+3); }
    const bw=plotW/n;
    items.forEach((d,i)=>{
      const bh=plotH*(d.value/max), x=padL+i*bw+bw*0.15, w=Math.max(1,bw*0.7), y=padT+plotH-bh;
      ctx.fillStyle=opts.colorByIndex?CHART_COLORS[i%CHART_COLORS.length]:color; rr(ctx,x,y,w,bh,3); ctx.fill();
      ctx.fillStyle=dim; ctx.textAlign='center'; ctx.font='10px sans-serif';
      if(opts.rotate){ ctx.save(); ctx.translate(x+w/2,H-padB+11); ctx.rotate(-Math.PI/4); ctx.textAlign='right'; ctx.fillText(trunc(d.label,10),0,0); ctx.restore(); }
      else { const step=Math.max(1,Math.ceil(n/(W>560?24:8))); if(i%step===0||n<=12) ctx.fillText(d.label,x+w/2,H-padB+14); }
    });
  });
}
function chartHBars(container, items, opts={}){
  const rowH=opts.rowH||27;
  // Потолок высоты: холст выше ~8000 CSS-px после умножения на devicePixelRatio
  // упирается в ограничение браузера и не рисуется вовсе.
  const H=Math.min(8000, opts.height||(items.length*rowH+14));
  const color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const dim=cvar('--text-dim','#888'), txt=cvar('--text','#222');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const max=Math.max(1,...items.map(d=>d.value)); const chars=opts.labelChars||24;
    ctx.font='12px sans-serif'; let labelW=0; items.forEach(d=>labelW=Math.max(labelW,ctx.measureText(trunc(d.label,chars)).width));
    labelW=Math.min(labelW+6, opts.labelW||210); const valW=opts.valW||52, barX=labelW+8, barW=Math.max(20,W-barX-valW-4);
    items.forEach((d,i)=>{
      const y=7+i*rowH, bh=rowH*0.6, by=y+(rowH-bh)/2;
      ctx.fillStyle=dim; ctx.textAlign='left'; ctx.font='12px sans-serif'; ctx.fillText(trunc(d.label,chars),0,by+bh*0.72);
      ctx.fillStyle=opts.colorByIndex?CHART_COLORS[i%CHART_COLORS.length]:color; const w=Math.max(2,barW*(d.value/max)); rr(ctx,barX,by,w,bh,3); ctx.fill();
      ctx.fillStyle=txt; ctx.font='11px sans-serif'; ctx.textAlign='left'; ctx.fillText(opts.fmt?opts.fmt(d):fmtNum(d.value), barX+w+5, by+bh*0.72);
    });
  });
}
function chartLine(container, items, opts={}){
  const H=opts.height||230, color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const padL=44,padR=12,padT=12,padB=28, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=niceMax(Math.max(1,...items.map(d=>d.value)));
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(opts.yfmt?opts.yfmt(max*(4-i)/4):fmtNum(Math.round(max*(4-i)/4)),padL-5,y+3); }
    const xat=i=> n<=1?padL+plotW/2:padL+plotW*i/(n-1), yat=v=>padT+plotH*(1-v/max);
    ctx.beginPath(); ctx.moveTo(xat(0),yat(items[0].value)); items.forEach((d,i)=>ctx.lineTo(xat(i),yat(d.value))); ctx.lineTo(xat(n-1),padT+plotH); ctx.lineTo(xat(0),padT+plotH); ctx.closePath(); ctx.fillStyle=color; ctx.globalAlpha=.13; ctx.fill(); ctx.globalAlpha=1;
    ctx.beginPath(); items.forEach((d,i)=>{ const x=xat(i),y=yat(d.value); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.strokeStyle=color; ctx.lineWidth=2; ctx.stroke();
    if(n<=40){ items.forEach((d,i)=>{ ctx.beginPath(); ctx.arc(xat(i),yat(d.value),2.4,0,7); ctx.fillStyle=color; ctx.fill(); }); }
    ctx.fillStyle=dim; ctx.font='10px sans-serif'; const step=Math.max(1,Math.ceil(n/(W>620?12:6)));
    items.forEach((d,i)=>{ if(i%step===0||i===n-1){ ctx.textAlign = i===0?'left':(i===n-1?'right':'center'); ctx.fillText(d.label,xat(i),H-padB+15); } });
  });
}
function chartDonut(container, slices, opts={}){
  const H=opts.height||200;
  drawInto(container,H,(ctx,W)=>{
    const total=slices.reduce((s,d)=>s+(d.value||0),0);
    const cx=W/2, cy=H/2, r=Math.min(W,H)/2-10, ri=r*0.6;
    if(!total){ cEmpty(ctx,W,H); return; }
    let a=-Math.PI/2;
    slices.forEach((d,i)=>{ const ang=2*Math.PI*(d.value||0)/total; if(ang>0){ ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,r,a,a+ang); ctx.closePath(); ctx.fillStyle=d.color||CHART_COLORS[i%CHART_COLORS.length]; ctx.fill(); } a+=ang; });
    ctx.beginPath(); ctx.arc(cx,cy,ri,0,7); ctx.fillStyle=cvar('--bg-elev','#fff'); ctx.fill();
    ctx.fillStyle=cvar('--text','#222'); ctx.textAlign='center'; ctx.font='700 19px sans-serif'; ctx.fillText(fmtNum(total),cx,cy+1);
    ctx.fillStyle=cvar('--text-dim','#888'); ctx.font='11px sans-serif'; ctx.fillText(opts.centerLabel||'всего',cx,cy+17);
  });
}
function donutLegend(slices){ const tot=slices.reduce((s,d)=>s+(d.value||0),0)||1; return `<div class="an-legend">${slices.map((d,i)=>`<span class="lg"><span class="dot" style="background:${d.color||CHART_COLORS[i%CHART_COLORS.length]}"></span>${esc(d.label)} <b>${fmtNum(d.value)}</b> · ${Math.round(100*(d.value||0)/tot)}%</span>`).join('')}</div>`; }
function chartHeat(container, matrix, rowLabels, opts={}){
  const rows=matrix.length, cols=(matrix[0]||[]).length, H=opts.height||(rows*22+30);
  drawInto(container,H,(ctx,W)=>{
    const dim=cvar('--text-dim','#888'), base=cvar('--primary','#2f6fed');
    if(!rows||!cols){ cEmpty(ctx,W,H); return; }
    const padL=34,padT=6,padB=16, gw=(W-padL-6)/cols, gh=(H-padT-padB)/rows;
    let max=opts.max||1; matrix.forEach(r=>r.forEach(v=>{ if(v>max)max=v; }));
    for(let r=0;r<rows;r++){
      for(let c=0;c<cols;c++){ const v=matrix[r][c]; ctx.fillStyle= v? hexA(base,0.14+0.86*(v/max)) : cvar('--bg-soft','#eee'); ctx.fillRect(padL+c*gw+1,padT+r*gh+1,gw-2,gh-2); }
      ctx.fillStyle=dim; ctx.textAlign='right'; ctx.font='10px sans-serif'; ctx.fillText(rowLabels[r],padL-4,padT+r*gh+gh*0.66);
    }
    ctx.fillStyle=dim; ctx.textAlign='center'; ctx.font='9px sans-serif';
    for(let c=0;c<cols;c+=3){ ctx.fillText(String(c).padStart(2,'0'),padL+c*gw+gw/2,H-4); }
  });
}
function wordCloud(items, opts={}){
  if(!items.length) return '<div class="an-empty-hint">Нет данных</div>';
  const max=Math.max(...items.map(w=>w.value)), min=Math.min(...items.map(w=>w.value));
  const lo=opts.min||13, hi=opts.max||30;
  return `<div class="an-cloud">${items.map((w,i)=>{ const t=max===min?1:(w.value-min)/(max-min); const sz=(lo+(hi-lo)*t).toFixed(1); const col=CHART_COLORS[i%CHART_COLORS.length]; return `<span class="w" title="${fmtNum(w.value)}" style="font-size:${sz}px;color:${hexA(col,0.55+0.45*t)};font-weight:${400+Math.round(t*3)*100}">${esc(w.label)}</span>`; }).join('')}</div>`;
}
// реестр перерисовки при resize/смене темы
function registerCharts(list){ State.redraw=()=>list.forEach(fn=>{ try{fn();}catch(e){} }); }

// ===================================================================
//  Раздел «Аналитика» (система)
// ===================================================================
async function viewAnalytics(c){
  const accs = (State.accounts&&State.accounts.length)?State.accounts:await api('/accounts'); State.accounts=accs;
  const scope=State.anScope||''; const days=State.anDays||90;
  let d; try{ d=await api('/analytics/system'+(scope?('?account_id='+scope+'&days='+days):('?days='+days))); }catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить аналитику</div></div>'; return; }
  c.innerHTML='';
  // Панель управления
  const bar=h(`<div class="an-toolbar">
    <label class="muted small">Область:</label>
    <select id="anScope"><option value="">Все ящики</option>${accs.map(a=>`<option value="${a.id}" ${String(a.id)===String(scope)?'selected':''}>${esc(a.name)}</option>`).join('')}</select>
    <label class="muted small">Период активности:</label>
    <select id="anDays">${[[30,'30 дней'],[90,'90 дней'],[180,'полгода'],[365,'год']].map(([v,t])=>`<option value="${v}" ${v===days?'selected':''}>${t}</option>`).join('')}</select>
    <span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" id="anRefresh">↻ Обновить</button></div>`);
  c.appendChild(bar);
  $('#anScope',bar).onchange=e=>{ State.anScope=e.target.value; viewAnalytics(c); };
  $('#anDays',bar).onchange=e=>{ State.anDays=parseInt(e.target.value); viewAnalytics(c); };
  $('#anRefresh',bar).onclick=()=>viewAnalytics(c);

  const o=d.overview;
  const kpi=(label,val,sub,accent)=>`<div class="kpi ${accent?'accent':''}"><div class="k-label">${label}</div><div class="k-value">${val}</div><div class="k-sub">${sub||''}</div></div>`;
  c.appendChild(h(`<div class="an-kpis">
    ${kpi('✉️ Писем в архиве',fmtNum(o.messages),o.bytes_h,true)}
    ${kpi('💾 Объём копий',o.bytes_h,'ср. письмо '+o.avg_message_h)}
    ${kpi('📬 Ящиков',o.accounts_total,'активных: '+o.accounts_enabled)}
    ${kpi('🗂️ Папок',fmtNum(o.folders),'уникальных')}
    ${kpi('⚙️ Заданий',fmtNum(o.jobs_total),o.jobs_success_rate!=null?('успех '+o.jobs_success_rate+'%'):'—')}
    ${kpi('🔄 Прогонов',fmtNum(d.runs.total),'нов. писем '+fmtNum(d.runs.messages_new))}
    ${kpi('📤 Экспортов',fmtNum(o.exports),d.exports.bytes_h)}
    ${kpi('⏰ Расписаний',fmtNum(o.schedules),'вкл: '+o.schedules_enabled)}
    ${kpi('💽 Свободно',o.disk_free_h,'на диске')}
    ${kpi('🗄️ База',o.db_size_h,'файл БД')}
    ${kpi('👥 Пользователей',fmtNum(o.users),'сессий: '+o.active_sessions)}
    ${kpi('🚦 Планировщик',o.scheduler_running?'вкл':'выкл','воркеров: '+o.workers)}
  </div>`));

  const redraws=[];
  const reg=(fn)=>{ fn(); redraws.push(fn); };

  // Активность по дням (с переключателем метрики)
  const actCard=h(`<div class="card an-card"><h3>📈 Активность по дням <span class="h-sub">— последние ${d.activity.length} дн.</span><span class="spacer" style="flex:1"></span>
    <span class="btn-row" id="actMetric" style="gap:6px"></span></h3><div class="an-chart" id="actChart"></div></div>`);
  c.appendChild(actCard);
  const METRICS=[['messages','Письма',cvar('--primary','#2f6fed')],['bytes','Объём',cvar('--info','#2b8ca6')],['jobs','Задания',cvar('--success','#1f9d55')],['errors','Ошибки',cvar('--danger','#d64545')]];
  let actMetric='messages';
  const mbtns=$('#actMetric',actCard);
  METRICS.forEach(([k,t])=>{ const b=h(`<button class="btn sm ${k==='messages'?'primary':''}" data-m="${k}">${t}</button>`); b.onclick=()=>{ actMetric=k; mbtns.querySelectorAll('button').forEach(x=>x.className='btn sm'); b.className='btn sm primary'; drawAct(); }; mbtns.appendChild(b); });
  const drawAct=()=>{ const meta=METRICS.find(m=>m[0]===actMetric); const isB=actMetric==='bytes';
    const items=d.activity.map(a=>({label:(a.day||'').slice(5), value:isB?(a.bytes/1048576):a[actMetric]}));
    chartLine($('#actChart',actCard), items, {height:230, color:meta[2], yfmt:isB?(v=>v.toFixed(0)+'М'):null}); };
  reg(drawAct);

  // Задания: статусы (donut) + типы (hbars)
  const jobsCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⚙️ Задания по статусам</h3><div class="an-chart" id="jobStatus"></div><div id="jobStatusLeg"></div></div>
    <div class="card"><h3>🧩 Задания по типам</h3><div class="an-chart" id="jobType"></div></div></div>`);
  c.appendChild(jobsCard);
  const jStatus=d.jobs.by_status.map((s,i)=>({label:s.label,value:s.value,color:({success:cvar('--success','#1f9d55'),failed:cvar('--danger','#d64545'),running:cvar('--primary','#2f6fed'),queued:cvar('--text-dim','#888'),partial:cvar('--warn','#d98a00'),cancelled:cvar('--warn','#d98a00')})[s.key]||CHART_COLORS[i]}));
  reg(()=>{ chartDonut($('#jobStatus',jobsCard), jStatus, {centerLabel:'заданий'}); $('#jobStatusLeg',jobsCard).innerHTML=donutLegend(jStatus); });
  reg(()=>chartHBars($('#jobType',jobsCard), d.jobs.by_type, {colorByIndex:true, height:Math.max(90,d.jobs.by_type.length*30)}));

  // Длительность + прогоны
  if(d.jobs.durations.length){
    const durRows=d.jobs.durations.map(x=>`<tr><td>${esc(x.type_label)}</td><td>${x.count}</td><td>${x.avg_s} с</td><td>${x.max_s} с</td></tr>`).join('');
    c.appendChild(h(`<div class="card an-card"><h3>⏱️ Длительность заданий</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Тип</th><th>Кол-во</th><th>Средняя</th><th>Максимум</th></tr></thead><tbody>${durRows}</tbody></table></div></div>`));
  }

  // Экспорт: форматы (donut) + движки (hbars); восстановление
  if(d.exports.total){
    const exCard=h(`<div class="grid cols-2 an-card">
      <div class="card"><h3>📤 Экспорт по форматам</h3><div class="an-chart" id="exFmt"></div><div id="exFmtLeg"></div></div>
      <div class="card"><h3>🔧 Экспорт по движкам</h3><div class="an-chart" id="exEng"></div></div></div>`);
    c.appendChild(exCard);
    const exF=d.exports.by_format.map((x,i)=>({label:x.label,value:x.value}));
    reg(()=>{ chartDonut($('#exFmt',exCard),exF,{centerLabel:'файлов'}); $('#exFmtLeg',exCard).innerHTML=donutLegend(exF); });
    reg(()=>chartHBars($('#exEng',exCard),d.exports.by_engine,{colorByIndex:true,height:Math.max(80,d.exports.by_engine.length*30)}));
  }

  // Хранилище по ящикам
  if(d.accounts.length){
    const rows=d.accounts.map(a=>`<tr><td><strong>${esc(a.name)}</strong>${a.enabled?'':' <span class="tag">выкл</span>'}</td><td>${fmtNum(a.messages)}</td><td>${esc(a.bytes_h)}</td><td>${a.folders}</td><td>${a.last_run?`<span class="badge ${esc(a.last_run.status)}">${esc(STATUS_LBL[a.last_run.status]||a.last_run.status)}</span>`:'<span class="muted small">—</span>'}</td></tr>`).join('');
    const stCard=h(`<div class="grid cols-2 an-card">
      <div class="card"><h3>🗄️ Хранилище по ящикам</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Ящик</th><th>Писем</th><th>Объём</th><th>Папок</th><th>Последний</th></tr></thead><tbody>${rows}</tbody></table></div></div>
      <div class="card"><h3>📊 Писем по ящикам</h3><div class="an-chart" id="accBars"></div></div></div>`);
    c.appendChild(stCard);
    // Только первые 20 ящиков: на 500 холст вырастал до 16 000 CSS-пикселей
    // (с учётом devicePixelRatio — за предел canvas в Safari, график просто
    // не рисовался). Полный список всё равно есть в разделе «Почтовые ящики».
    const accTop=[...d.accounts].sort((a,b)=>(b.messages||0)-(a.messages||0)).slice(0,20);
    reg(()=>chartHBars($('#accBars',stCard), accTop.map(a=>({label:a.name,value:a.messages})), {colorByIndex:true, height:Math.max(90,accTop.length*32), labelChars:20}));
    if(d.accounts.length>accTop.length){
      stCard.appendChild(h(`<div class="muted small">Показаны 20 самых крупных ящиков из ${fmtNum(d.accounts.length)}.</div>`));
    }
  }

  // Одинаковые вложения: отчёт догружается отдельно (его подсчёт — фоновое задание).
  // Он читает письма — только администратору.
  if(!scope && isAdmin()){
    const ddWrap=h('<div id="dedupWrap"></div>'); c.appendChild(ddWrap);
    loadDedup(ddWrap, redraws);
  }

  // Расписания и аудит
  const botGrid=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⏰ Ближайшие запуски</h3><div id="upNext"></div></div>
    <div class="card"><h3>🛡️ Топ действий (аудит)</h3><div class="an-chart" id="auditBars"></div></div></div>`);
  c.appendChild(botGrid);
  const up=d.schedules.upcoming;
  $('#upNext',botGrid).innerHTML = up.length? `<table class="an-mini-table">${up.map(u=>`<tr><td>${u.account_id?esc(accName(u.account_id)||('#'+u.account_id)):'все ящики по очереди'}</td><td>${esc(JOBLBL[u.job_type]||u.job_type)}</td><td>${fmtDate(u.next_run)}</td></tr>`).join('')}</table>` : '<div class="an-empty-hint">Нет включённых расписаний</div>';
  if(d.audit_top.length) reg(()=>chartHBars($('#auditBars',botGrid), d.audit_top, {colorByIndex:true, labelChars:22, height:Math.max(80,d.audit_top.length*24)}));
  else $('#auditBars',botGrid).innerHTML='<div class="an-empty-hint">Нет записей аудита</div>';

  registerCharts(redraws);
}

// ---- Отчёт «Одинаковые вложения» (сколько места сэкономит хранение одной копии) ----
async function loadDedup(wrap, redraws){
  let d; try{ d=await api('/analytics/dedup',{bg:true}); }catch(e){ return; }
  if(!wrap.isConnected) return;
  const r=d.report, st=d.status, job=(d.running||[])[0];
  wrap.innerHTML='';
  const card=h(`<div class="card an-card">
    <div class="section-title" style="flex-wrap:wrap;gap:8px"><h3 style="margin:0">🧬 Одинаковые вложения <span class="h-sub">— сколько места освободится, если хранить одну копию</span></h3>
      <span class="spacer" style="flex:1"></span>
      ${r&&!job?'<button class="btn sm ghost" id="ddFull" title="Забыть прочитанное и прочитать весь архив заново">Посчитать заново</button>':''}
      <button class="btn sm primary" id="ddRun" ${job?'disabled':''}>${job?'<span class="spinner"></span> Идёт подсчёт…':(r?'↻ Обновить':'▶ Посчитать')}</button></div>
    <div class="muted small" style="margin-bottom:10px">Рассылки всем сотрудникам, логотипы в подписях, пересланные по цепочке договоры лежат в каждом письме отдельной копией. Отчёт читает письма и находит вложения с одинаковым содержимым (имя файла может отличаться). <b>Формат хранения не меняется</b> — это только подсчёт.</div>
    <div id="ddProgress"></div><div id="ddBody"></div></div>`);
  wrap.appendChild(card);
  const start=async(full)=>{
    if(full && !await confirmDlg('Посчитать заново?','Прочитанное забудется, и весь архив будет прочитан снова — на большом архиве это часы (копирования ящиков при этом идут своим чередом).',{okText:'Посчитать заново', okClass:'primary'})) return;
    try{ const res=await api('/analytics/dedup/scan',{method:'POST',body:{full:!!full}});
      toast('Подсчёт запущен', res.already_running?'Уже выполняется':'Письма читаются в фоне; отчёт обновится здесь сам.');
      loadDedup(wrap, redraws);
    }catch(e){ toastErr(e); }
  };
  $('#ddRun',card).onclick=()=>start(false);
  const fb=$('#ddFull',card); if(fb) fb.onclick=()=>start(true);
  const prog=$('#ddProgress',card);
  const drawProgress=(s)=>{
    const pct=s.total?Math.floor(100*s.scanned/s.total):100;
    prog.innerHTML=`<div class="progress running" style="margin:4px 0 6px"><span style="width:${pct}%"></span></div>
      <div class="muted small" style="margin-bottom:10px">Прочитано писем: ${fmtNum(s.scanned)} из ${fmtNum(s.total)} (${pct}%) · учтено вложений: ${fmtNum(s.attachments)}. Большой архив читается частями — между ними идут копирования ящиков.</div>`;
  };
  if(job){
    drawProgress(st);
    const t=setInterval(async()=>{
      if(!wrap.isConnected || State.view!=='analytics'){ clearInterval(t); return; }
      let x; try{ x=await api('/analytics/dedup',{bg:true}); }catch(e){ clearInterval(t); return; }
      if(!(x.running||[]).length){ clearInterval(t); loadDedup(wrap, redraws); return; }
      drawProgress(x.status);
    }, 4000);
  }
  const body=$('#ddBody',card);
  if(!r){
    body.innerHTML=job?'':`<div class="an-empty-hint">Отчёт ещё не считался. Нажмите «Посчитать»: первый подсчёт читает весь архив (на большом архиве — часы), следующие — только новые письма.</div>`;
    return;
  }
  renderDedup(body, r, st, redraws);
}

function renderDedup(body, r, st, redraws){
  const s=r.savings, a=r.attachments, cov=r.coverage;
  const kpi=(l,v,sub,ac)=>`<div class="kpi ${ac?'accent':''}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${sub||''}</div></div>`;
  const notes=[];
  if(!cov.complete) notes.push(`⏳ Прочитано ${cov.percent}% писем (${fmtNum(cov.scanned)} из ${fmtNum(cov.total)}) — цифры вырастут, когда подсчёт дойдёт до конца.`);
  else if(st && st.pending>0) notes.push(`С момента отчёта в архив добавилось писем: ${fmtNum(st.pending)} — нажмите «Обновить», чтобы учесть их (прочитаются только новые).`);
  if(s.separate_store>s.bytes) notes.push(`Если хранить вложения отдельно от писем и без кодировки base64 (она увеличивает файлы примерно на треть), освободится до <b>${esc(s.separate_store_h)}</b> (${s.separate_store_percent}% архива).`);
  if(r.compressed) notes.push('Сжатие копии включено — на диске экономия будет примерно на четверть меньше указанной.');
  if(r.whole_messages && r.whole_messages.copies) notes.push(`Писем-двойников целиком: ${fmtNum(r.whole_messages.copies)} (${esc(r.whole_messages.bytes_h)}) — одно и то же письмо лежит в нескольких папках; их вложения уже учтены выше.`);
  if(cov.errors) notes.push(`Не удалось прочитать писем: ${fmtNum(cov.errors)} (подробности — в событиях задания в разделе «Задания»).`);
  notes.push(`Вложения меньше ${esc(r.min_size_h)} не учитываются. Отчёт от ${esc(fmtDate(r.generated_at))}.`);
  const when=(t)=>{ const a=fmtDateShort(t.first), z=fmtDateShort(t.last); return a&&z&&a!==z?a+' — '+z:(a||z); };
  const top=(r.top||[]).map((t,i)=>`<tr>
      <td><strong>${esc(trunc(t.filename,48))}</strong>${t.names>1?`<div class="muted small">и ещё ${t.names-1} ${plural(t.names-1,'имя','имени','имён')}</div>`:''}</td>
      <td class="small" title="${esc(t.ctype||'')}">${esc(trunc(t.type_label,28))}</td><td class="nowrap">${esc(t.size_h)}</td><td>${fmtNum(t.copies)}</td><td>${fmtNum(t.mailboxes)}</td>
      <td class="nowrap"><strong>${esc(t.wasted_h)}</strong></td>
      <td class="small muted">${esc(when(t))}</td>
      <td>${t.example?`<button class="btn ghost sm" data-ex="${i}" title="Открыть одно из писем с этим вложением">✉️</button>`:''}</td></tr>`).join('');
  const box=h(`<div>
    <div class="an-kpis">
      ${kpi('💾 Освободится',esc(s.bytes_h),s.percent_of_archive+'% архива',true)}
      ${kpi('📎 Вложения',esc(a.bytes_h),fmtNum(a.count)+' шт. · '+a.percent_of_archive+'% архива')}
      ${kpi('🔁 Лишних копий',fmtNum(r.duplicates.copies),fmtNum(r.duplicates.groups)+' '+plural(r.duplicates.groups||0,'файл','файла','файлов')+' повторяются')}
      ${kpi('📬 Только внутри ящиков',esc(s.within_mailbox_h),'если не объединять копии разных сотрудников')}
    </div>
    <div class="muted small" style="margin:6px 0 12px;line-height:1.55">${notes.map(n=>'<div>'+n+'</div>').join('')}</div>
    ${top?`<h4 style="margin:6px 0">Чаще всего повторяются</h4><div class="table-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Тип</th><th class="nowrap">Размер</th><th class="nowrap">Копий</th><th class="nowrap">Ящиков</th><th class="nowrap">Лишнее место</th><th>Когда</th><th></th></tr></thead><tbody>${top}</tbody></table></div>`:'<div class="an-empty-hint">Одинаковых вложений не найдено.</div>'}
    ${top?`<div class="grid cols-2" style="margin-top:12px">
      <div><h4 style="margin:6px 0">Лишнее место по типам файлов</h4><div class="an-chart" id="ddTypes"></div></div>
      <div><h4 style="margin:6px 0">Ящики с повторяющимися вложениями</h4><div class="an-chart" id="ddBoxes"></div></div></div>`:''}
  </div>`);
  body.innerHTML=''; body.appendChild(box);
  box.querySelectorAll('[data-ex]').forEach(b=>{ b.onclick=()=>{ const ex=r.top[parseInt(b.dataset.ex)].example; openMailMessage(ex.account_id, ex.message_id, ex.folder); }; });
  if(top){
    const add=(fn)=>{ fn(); redraws.push(fn); };
    add(()=>chartHBars($('#ddTypes',box), r.by_type, {colorByIndex:true, labelChars:22, valW:70, fmt:x=>x.value_h, height:Math.max(80,r.by_type.length*27+14)}));
    add(()=>chartHBars($('#ddBoxes',box), r.by_account, {colorByIndex:true, labelChars:22, valW:70, fmt:x=>x.value_h, height:Math.max(80,r.by_account.length*27+14)}));
    registerCharts(redraws);
  }
}

/** Открыть письмо в разделе «Почта» (из отчётов аналитики). */
function openMailMessage(accId, msgId, folder){
  State.mailAccount=accId; Mail.folder=folder||null; Mail.offset=0; Mail.search=null; Mail.openAfter={acc:accId, id:msgId};
  if(location.hash==='#/mail') route(); else location.hash='#/mail';
}

// ===================================================================
//  Раздел «Аналитика писем» (содержание)
// ===================================================================
async function viewMailAnalytics(c){
  const accs=(State.accounts&&State.accounts.length)?State.accounts:await api('/accounts'); State.accounts=accs;
  const scope=State.maScope||'';
  let d; try{ d=await api('/analytics/mail'+(scope?('?account_id='+scope):'')); }catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить аналитику писем</div></div>'; return; }
  c.innerHTML='';
  const bar=h(`<div class="an-toolbar">
    <label class="muted small">Ящик:</label>
    <select id="maScope"><option value="">Все ящики</option>${accs.map(a=>`<option value="${a.id}" ${String(a.id)===String(scope)?'selected':''}>${esc(a.name)}</option>`).join('')}</select>
    <span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" id="maRefresh">↻ Обновить</button></div>`);
  c.appendChild(bar);
  $('#maScope',bar).onchange=e=>{ State.maScope=e.target.value; viewMailAnalytics(c); };
  $('#maRefresh',bar).onclick=()=>viewMailAnalytics(c);

  const o=d.overview;
  if(!o.messages){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">🔎</div>Нет локальных копий для анализа.<br>Сделайте резервное копирование ящика.</div></div>')); return; }
  const kpi=(l,v,s,ac)=>`<div class="kpi ${ac?'accent':''}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s||''}</div></div>`;
  const span=o.date_from?`${(o.date_from||'').slice(0,10)} — ${(o.date_to||'').slice(0,10)}`:'—';
  c.appendChild(h(`<div class="an-kpis">
    ${kpi('✉️ Писем',fmtNum(o.messages),span,true)}
    ${kpi('💾 Объём',o.bytes_h,'ср. '+o.avg_size_h+' · медиана '+o.median_size_h)}
    ${kpi('👤 Отправителей',fmtNum(o.unique_senders),'доменов: '+o.unique_domains)}
    ${kpi('📎 С вложениями',o.with_attach_pct+'%',fmtNum(o.with_attach)+' '+plural(o.with_attach||0,'письмо','письма','писем'))}
    ${kpi('📬 Непрочитанных',o.unseen_pct+'%',fmtNum(o.unseen)+' из '+fmtNum(o.messages))}
    ${kpi('↩️ Ответы / Пересылки',fmtNum(o.reply)+' / '+fmtNum(o.forward),'⭐ важных: '+fmtNum(o.flagged))}
    ${kpi('🗂️ Папок',fmtNum(o.folders),'без темы: '+fmtNum(o.empty_subject))}
    ${kpi('📅 В среднем/день',fmtNum(o.avg_per_day),'за '+fmtNum(o.span_days)+' дн.')}
  </div>`));

  const redraws=[]; const reg=(fn)=>{ fn(); redraws.push(fn); };

  // Динамика по месяцам
  const mCard=h(`<div class="card an-card"><h3>📈 Динамика по месяцам</h3><div class="an-chart" id="maMonth"></div></div>`);
  c.appendChild(mCard);
  reg(()=>chartLine($('#maMonth',mCard), d.by_month, {height:230}));

  // Год + день недели
  const ywCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>🗓️ По годам</h3><div class="an-chart" id="maYear"></div></div>
    <div class="card"><h3>📆 По дням недели</h3><div class="an-chart" id="maWeek"></div></div></div>`);
  c.appendChild(ywCard);
  reg(()=>chartVBars($('#maYear',ywCard), d.by_year, {height:220}));
  reg(()=>chartVBars($('#maWeek',ywCard), d.by_weekday, {height:220, color:cvar('--info','#2b8ca6')}));

  // Часы
  const hCard=h(`<div class="card an-card"><h3>🕐 Распределение по часам суток</h3><div class="an-chart" id="maHour"></div></div>`);
  c.appendChild(hCard);
  reg(()=>chartVBars($('#maHour',hCard), d.by_hour, {height:210, color:cvar('--success','#1f9d55')}));

  // Тепловая карта день×час
  const heatCard=h(`<div class="card an-card"><h3>🔥 Активность: день недели × час <span class="h-sub">— чем ярче, тем больше писем</span></h3><div class="an-chart" id="maHeat"></div></div>`);
  c.appendChild(heatCard);
  reg(()=>chartHeat($('#maHeat',heatCard), d.heatmap.matrix, d.heatmap.rows, {max:d.heatmap.max, height:7*24+30}));

  // Размеры + состояния (donuts)
  const szCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>📐 Размеры писем</h3><div class="an-chart" id="maSize"></div></div>
    <div class="card"><h3>👁️ Прочитанность и вложения</h3>
      <div class="grid cols-2"><div><div class="an-chart" id="maRead"></div><div id="maReadLeg"></div></div>
      <div><div class="an-chart" id="maAtt"></div><div id="maAttLeg"></div></div></div></div></div>`);
  c.appendChild(szCard);
  reg(()=>chartHBars($('#maSize',szCard), d.size_hist, {height:Math.max(120,d.size_hist.length*30), labelChars:14, color:cvar('--warn','#d98a00')}));
  const readS=d.read_state.map((s,i)=>({...s,color:i===0?cvar('--success','#1f9d55'):cvar('--text-dim','#888')}));
  const attS=d.attach_state.map((s,i)=>({...s,color:i===0?cvar('--primary','#2f6fed'):cvar('--text-dim','#888')}));
  reg(()=>{ chartDonut($('#maRead',szCard),readS,{height:170,centerLabel:'писем'}); $('#maReadLeg',szCard).innerHTML=donutLegend(readS); });
  reg(()=>{ chartDonut($('#maAtt',szCard),attS,{height:170,centerLabel:'писем'}); $('#maAttLeg',szCard).innerHTML=donutLegend(attS); });

  // Отправители + домены
  const sdCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>👤 Топ отправителей</h3><div class="an-chart" id="maSenders"></div></div>
    <div class="card"><h3>🌐 Топ доменов</h3><div class="an-chart" id="maDomains"></div></div></div>`);
  c.appendChild(sdCard);
  reg(()=>chartHBars($('#maSenders',sdCard), d.top_senders.slice(0,15), {colorByIndex:true, labelChars:30, labelW:250, height:Math.max(90,Math.min(15,d.top_senders.length)*28)}));
  reg(()=>chartHBars($('#maDomains',sdCard), d.top_domains, {colorByIndex:true, labelChars:26, height:Math.max(90,d.top_domains.length*28)}));

  // Папки + слова темы
  const ffCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>🗂️ Папки</h3><div class="an-chart" id="maFolders"></div></div>
    <div class="card"><h3>🔤 Частые слова в темах</h3><div id="maWords"></div></div></div>`);
  c.appendChild(ffCard);
  reg(()=>chartHBars($('#maFolders',ffCard), d.folders, {colorByIndex:true, labelChars:20, height:Math.max(90,d.folders.length*28), fmt:x=>fmtNum(x.value)+' · '+x.bytes_h, valW:120}));
  $('#maWords',ffCard).innerHTML=wordCloud(d.subject_words);

  // Крупнейшие письма
  if(d.largest.length){
    const rows=d.largest.map(m=>`<tr><td>${esc(trunc(m.subject,48))}<div class="muted small">${esc(trunc(m.from,42))}</div></td><td>${esc(m.folder)}</td><td>${esc(m.size_h)}</td><td class="small muted">${fmtDateShort(m.date)}</td></tr>`).join('');
    c.appendChild(h(`<div class="card an-card"><h3>🏋️ Крупнейшие письма</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Тема / отправитель</th><th>Папка</th><th>Размер</th><th>Дата</th></tr></thead><tbody>${rows}</tbody></table></div></div>`));
  }

  // ---- Глубокий анализ содержимого (чтение .eml) ----
  const deepWrap=h(`<div id="deepWrap"></div>`); c.appendChild(deepWrap);
  registerCharts(redraws);   // регистрируем то, что уже есть; глубокий догрузим отдельно
  loadDeep(deepWrap, scope, redraws);
}

async function loadDeep(wrap, scope, redraws){
  let dd; try{ dd=await api('/analytics/mail/deep'+(scope?('?account_id='+scope):'')); }catch(e){ return; }
  const running = dd.running&&dd.running.length;
  const when = dd.data? fmtDate(dd.data.generated_at):null;
  wrap.innerHTML='';
  const banner=h(`<div class="deep-banner">
    <div><strong>🔬 Глубокий анализ содержимого</strong><div class="muted small">${dd.available?('обновлён: '+when+' · разобрано '+fmtNum(dd.data.scanned)+' '+plural(dd.data.scanned||0,'письмо','письма','писем')):'Разбирает сами письма (.eml): типы вложений, домены получателей, текст/HTML, язык, частые слова тела.'}</div></div>
    <span class="spacer" style="flex:1"></span>
    <button class="btn primary sm" id="deepRun" ${running?'disabled':''}>${running?'<span class="spinner"></span> Идёт анализ…':(dd.available?'↻ Пересчитать':'▶ Запустить анализ')}</button></div>`);
  wrap.appendChild(banner);
  $('#deepRun',banner).onclick=async()=>{
    try{ const r=await api('/analytics/mail/scan'+(scope?('?account_id='+scope):''),{method:'POST'});
      toast('Анализ запущен', r.already_running?'Уже выполняется':'Следите за прогрессом; результат появится здесь автоматически');
      const b=$('#deepRun',banner); b.disabled=true; b.innerHTML='<span class="spinner"></span> Идёт анализ…';
      pollDeep(wrap, scope, redraws, r.job_id);
    }catch(e){ toastErr(e); }
  };
  if(running){ pollDeep(wrap, scope, redraws, dd.running[0].id); }
  if(dd.available && dd.data) renderDeep(wrap, dd.data, redraws);
}
function pollDeep(wrap, scope, redraws, jobId){
  let n=0;
  const t=setInterval(async()=>{
    n++; if(n>120){ clearInterval(t); return; }
    if(State.view!=='mailanalytics'){ clearInterval(t); return; }
    try{ const j=await api('/jobs/'+jobId,{bg:true}); if(j.status && ['success','failed','cancelled','partial'].includes(j.status)){ clearInterval(t); if(State.view==='mailanalytics') loadDeep(wrap, scope, redraws); } }catch(e){ clearInterval(t); }
  }, 2500);
}
function renderDeep(wrap, dp, redraws){
  const at=dp.attachments, rc=dp.recipients, bd=dp.body;
  const box=h(`<div>
    <div class="an-kpis">
      <div class="kpi accent"><div class="k-label">📎 Вложений</div><div class="k-value">${fmtNum(at.count)}</div><div class="k-sub">${at.bytes_h} · в ${fmtNum(at.messages_with_attach)} письмах</div></div>
      <div class="kpi"><div class="k-label">🌐 Доменов получателей</div><div class="k-value">${fmtNum(rc.unique_domains)}</div><div class="k-sub">уникальных</div></div>
      <div class="kpi"><div class="k-label">📝 Ср. длина текста</div><div class="k-value">${fmtNum(bd.avg_text_len)}</div><div class="k-sub">символов</div></div>
      <div class="kpi"><div class="k-label">🔎 Разобрано</div><div class="k-value">${fmtNum(dp.scanned)}</div><div class="k-sub">ошибок: ${fmtNum(dp.errors)}</div></div>
    </div>
    <div class="grid cols-2 an-card">
      <div class="card"><h3>📎 Типы вложений (расширение)</h3><div class="an-chart" id="dpExt"></div></div>
      <div class="card"><h3>🧾 Типы вложений (MIME)</h3><div class="an-chart" id="dpType"></div></div></div>
    <div class="grid cols-2 an-card">
      <div class="card"><h3>📤 Домены получателей</h3><div class="an-chart" id="dpTo"></div></div>
      <div class="card"><h3>🧬 Формат тела и язык</h3><div class="grid cols-2"><div><div class="an-chart" id="dpBody"></div><div id="dpBodyLeg"></div></div><div><div class="an-chart" id="dpLang"></div><div id="dpLangLeg"></div></div></div></div></div>
    <div class="card an-card"><h3>🔤 Частые слова в тексте писем</h3><div id="dpWords"></div></div>
  </div>`);
  wrap.appendChild(box);
  const add=(fn)=>{ fn(); redraws.push(fn); };
  add(()=>chartHBars($('#dpExt',box), at.by_ext, {colorByIndex:true, labelChars:14, height:Math.max(80,at.by_ext.length*26)}));
  add(()=>chartHBars($('#dpType',box), at.by_type, {colorByIndex:true, labelChars:34, labelW:260, height:Math.max(80,at.by_type.length*26)}));
  add(()=>chartHBars($('#dpTo',box), rc.top_domains, {colorByIndex:true, labelChars:26, height:Math.max(80,rc.top_domains.length*26)}));
  add(()=>{ chartDonut($('#dpBody',box), bd.kinds, {height:170,centerLabel:'писем'}); $('#dpBodyLeg',box).innerHTML=donutLegend(bd.kinds); });
  add(()=>{ chartDonut($('#dpLang',box), bd.languages, {height:170,centerLabel:'писем'}); $('#dpLangLeg',box).innerHTML=donutLegend(bd.languages); });
  $('#dpWords',box).innerHTML=wordCloud(bd.top_words, {min:13, max:34});
  registerCharts(redraws);
}

// ---------- Старт ----------
boot();

// ===================================================================
//  Сервер Axigen
// ===================================================================
const Ax = { filter:'', query:'', sel:new Set(), logKind:'people', shown:200 };
const AX_TABS = [['overview','📊 Обзор'],['accounts','👤 Учётные записи'],['queue','📨 Очередь'],['log','📜 Журнал команд'],['console','⌨️ Консоль'],['diag','🩺 Диагностика']];
const AX_KIND = {read:'чтение', write:'изменение', console:'консоль', check:'проверка', monitor:'плановая проверка', error:'ошибка'};
const AX_COPY = {axigen_ftp:'FTP Backup', password:'IMAP, пароль', master:'IMAP, администратор', oauth2:'OAuth2'};
const AX_INACTIVE_DAYS = 90;

/** «10.6.30|Linux|x86_64» → «10.6.30 · Linux · x86_64». */
function axVersion(v){ return String(v||'').split('|').map(x=>x.trim()).filter(Boolean).join(' · '); }
const AX_FIELD_LBL={licensetype:'Тип лицензии', type:'Тип лицензии', edition:'Редакция', expirationdate:'Действует до', expirydate:'Действует до', expires:'Действует до', validuntil:'Действует до', maxaccounts:'Учётных записей не больше', maxdomains:'Доменов не больше', maxmailboxes:'Ящиков не больше', totalsize:'всего', usedsize:'занято', accounts:'учётных записей', status:'состояние'};
function axFieldLbl(k){ const key=String(k||'').toLowerCase().replace(/[\s_\-]+/g,''); return AX_FIELD_LBL[key]||k; }
function axFeature(){ return !!(((State.user||{}).features||{}).axigen); }
function axDaysSince(iso){ if(!iso) return null; const t=Date.parse(iso); return isNaN(t)?null:Math.floor((Date.now()-t)/86400000); }
function axLoginTag(r){
  if(r.deleted_at) return '<span class="tag err">удалена на сервере</span>';
  if(r.login_blocked===true) return `<span class="tag warn" title="${esc(r.blocked_at?'Закрыт '+fmtDate(r.blocked_at)+(r.blocked_by?' ('+r.blocked_by+')':''):'Нет служб входа')}">🔒 вход закрыт</span>`;
  if(r.login_blocked===false) return '<span class="tag ok">вход открыт</span>';
  return '<span class="tag">неизвестно</span>';
}
function axQuotaCell(r){
  if(r.used_bytes==null && !r.quota_kb) return '<span class="muted">—</span>';
  const used=r.used_bytes!=null?fmtBytes(r.used_bytes):'?';
  if(!r.quota_kb) return `${esc(used)} <span class="muted small">без квоты</span>`;
  const pct=r.quota_percent; const cls=pct>=90?'err':(pct>=75?'warn':'ok');
  return `<div class="small">${esc(used)} из ${esc(fmtBytes(r.quota_kb*1024))}${pct!=null?` <span class="tag ${cls}">${pct}%</span>`:''}</div>
    ${pct!=null?`<div class="progress" style="height:5px;margin-top:3px"><span style="width:${Math.min(100,pct)}%"></span></div>`:''}`;
}
function axMailboxCell(r){
  const b=r.mailbox;
  if(!b) return '<span class="tag warn">нет в архиве</span>';
  const how=AX_COPY[b.auth_type]||b.auth_type;
  return `<b>${esc(b.name)}</b>${b.enabled?'':' <span class="tag">выключен</span>'}
    <div class="small muted">${esc(how)}${b.same_server?'':' · другой сервер: '+esc(b.host)} · писем ${fmtNum(b.messages)} · ${b.last_backup_at?'копия '+esc(fmtDate(b.last_backup_at)):'копий не было'}</div>`;
}
function axMatches(r, f, q){
  const pctLimit=90;
  if(f==='nomailbox' && (r.mailbox || r.deleted_at)) return false;
  if(f==='archived' && !r.mailbox) return false;
  if(f==='blocked' && !(r.login_blocked===true && !r.deleted_at)) return false;
  if(f==='quota' && !(r.quota_percent!=null && r.quota_percent>=pctLimit)) return false;
  if(f==='inactive'){ const d=axDaysSince(r.last_login); if(r.deleted_at || d==null || d<AX_INACTIVE_DAYS) return false; }
  if(f==='deleted' && !r.deleted_at) return false;
  if(f==='errors' && !r.errors) return false;
  if(!f && r.deleted_at && !r.mailbox) return false;      // удалённые без архива — только в своём отборе
  q=(q||'').trim().toLowerCase();
  if(!q) return true;
  return [r.email, r.first_name, r.last_name, (r.aliases||[]).join(' '), r.mailbox&&r.mailbox.name].some(v=>String(v||'').toLowerCase().includes(q));
}

async function viewAxigen(c){
  const tab=((location.hash.match(/[?&]tab=(\w+)/)||[])[1])||'overview';
  const st=await api('/axigen/status');
  c.innerHTML='';
  const s=st.status||{};
  const stateTag = !st.configured ? '<span class="tag warn">не настроен</span>'
    : (s.checked_at ? (s.ok?`<span class="tag ok" title="Проверен ${esc(fmtDate(s.checked_at))}">● доступен</span>`:`<span class="tag err" title="${esc(s.error||'')}">● недоступен</span>`) : '<span class="tag">не проверялся</span>');
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Сервер Axigen</h2>
    ${st.settings.host?`<span class="tag mono">${esc(st.settings.host)}</span>`:''} ${stateTag}
    ${s.version?`<span class="tag" title="Версия Axigen">${esc(axVersion(s.version))}</span>`:''}
    <div class="spacer"></div>
    <button class="btn sm" id="axCheck" title="Проверить CLI и FTP Backup по сохранённым настройкам">🔌 Проверить подключение</button>
    <button class="btn sm" id="axRefresh" title="Прочитать версию, лицензию, домены и очередь сейчас">🔄 Обновить состояние</button>
    <button class="btn sm primary" id="axSync" ${st.sync_running?'disabled':''} title="Прочитать учётные записи сервера и сверить с архивом">${st.sync_running?'<span class="spinner"></span> Сверка идёт…':'🔁 Сверить учётные записи'}</button>
    <a class="btn sm ghost" href="#/settings" title="Настройки → Сервер Axigen">⚙️</a></div>`));
  if(!st.enabled){
    c.appendChild(h(`<div class="card empty"><div class="big">📮</div><div>Работа с сервером Axigen выключена.</div>
      <div class="small muted" style="margin:6px 0 12px">Включите её и заполните адрес и администратора в «Настройки → Сервер Axigen».</div>
      <a class="btn primary" href="#/settings">Открыть настройки</a></div>`));
    return;
  }
  const on=(sel, fn)=>{ const el=$(sel,c); if(el) el.onclick=fn; };
  on('#axCheck', ()=>axigenCheckModal());
  on('#axRefresh', async(ev)=>{ const b=ev.currentTarget; b.disabled=true; b.innerHTML='<span class="spinner"></span> Читаем…';
    try{ const r=await api('/axigen/refresh',{method:'POST'}); toast(r.status.ok?'Состояние обновлено':'Сервер не ответил', r.status.ok?'':(r.status.error||''), r.status.ok?'success':'error', 7000); route(); }
    catch(e){ toastErr(e); b.disabled=false; b.textContent='🔄 Обновить состояние'; } });
  on('#axSync', async(ev)=>{ const b=ev.currentTarget; b.disabled=true;
    try{ const r=await api('/axigen/sync',{method:'POST'}); toast(r.already?'Сверка уже идёт':'Сверка запущена', `Задание №${r.job_id}`); axWatchJob(r.job_id); route(); }
    catch(e){ toastErr(e); b.disabled=false; } });
  if(!st.configured){
    c.appendChild(h(`<div class="hint warnbox">Не заданы адрес сервера, логин или пароль администратора Axigen — заполните «Настройки → Сервер Axigen» и сохраните.</div>`));
  }
  if((st.problems||[]).length){
    const bad=st.problems.some(p=>p.level==='error');
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--${bad?'danger':'warn'})">
      <div class="section-title"><h3>⚠️ Требует внимания</h3></div>
      <ul style="margin:0;padding-left:18px">${st.problems.map(p=>`<li${p.level==='error'?' style="color:var(--danger)"':''}>${esc(p.text)}</li>`).join('')}</ul></div>`));
  }
  c.appendChild(h(`<div class="tabs" role="tablist">${AX_TABS.map(([k,t])=>`<a href="#/axigen?tab=${k}" class="${k===tab?'active':''}" role="tab">${t}</a>`).join('')}</div>`));
  const body=h('<div></div>'); c.appendChild(body);
  const views={overview:axOverview, accounts:axAccounts, queue:axQueue, log:axLog, console:axConsole, diag:axDiag};
  await (views[tab]||axOverview)(body, st);
}

/** Следить за заданием сверки и перерисовать раздел, когда оно закончится. */
function axWatchJob(jobId){
  let n=0;
  const tick=async()=>{
    n++;
    try{ const j=await api(`/jobs/${jobId}`,{bg:true});
      if(['success','partial','failed','cancelled'].includes(j.status)){
        toast(j.status==='failed'?'Сверка не удалась':'Сверка закончена', (j.result&&j.result.summary)||j.error||'', j.status==='failed'?'error':'success', 8000);
        if(State.view==='axigen') route(); return; }
    }catch(e){ return; }
    if(n<240) setTimeout(tick, 3000);
  };
  setTimeout(tick, 2500);
}

async function axOverview(box, st){
  let data={accounts:[], orphans:[]};
  try{ data=await api('/axigen/accounts'); }catch(e){}
  const s=st.status||{}, live=data.accounts.filter(r=>!r.deleted_at);
  const archived=live.filter(r=>r.mailbox).length, lic=s.license||{};
  const days=lic.expires?Math.floor((Date.parse(lic.expires+'T00:00:00')-Date.now())/86400000):null;
  const kpi=(label, value, sub, cls='')=>`<div class="kpi ${cls}"><div class="k-label">${label}</div><div class="k-value">${value}</div><div class="k-sub">${sub}</div></div>`;
  box.appendChild(h(`<div class="grid cols-4" style="margin-bottom:16px">
    ${kpi('👤 Учётных записей', fmtNum(live.length), `доменов: ${fmtNum(st.counts.domains)}${st.last_sync?'':' · сверки ещё не было'}`, 'accent')}
    ${kpi('🗄️ В архиве', fmtNum(archived), live.length?`без копии: ${fmtNum(live.length-archived)}`:'—')}
    ${kpi('🔒 Вход закрыт', fmtNum(st.counts.blocked), `удалены на сервере: ${fmtNum(st.counts.deleted)}`)}
    ${kpi('📨 Очередь', s.queue!=null?fmtNum(s.queue):'—', lic.expires?`лицензия до ${esc(fmtDay(lic.expires+'T00:00:00'))}${days!=null?' ('+(days<0?'истекла':days+' дн.')+')':''}`:'лицензия: нет сведений')}
  </div>`));
  const grid=h('<div class="grid cols-2"></div>'); box.appendChild(grid);
  const doms=(s.domains||[]).map(d=>`<tr><td><b>${esc(d.name)}</b></td><td class="small muted">${Object.entries(d.fields||{}).map(([k,v])=>esc(axFieldLbl(k))+': '+esc(v)).join(' · ')}</td></tr>`).join('');
  const licRows=Object.entries(s.license_raw||{}).slice(0,12).map(([k,v])=>`<div class="kv"><span class="k">${esc(axFieldLbl(k))}</span><span>${esc(v)}</span></div>`).join('');
  grid.appendChild(h(`<div class="card"><div class="section-title"><h3>🖥️ Сервер</h3><div class="spacer"></div><span class="small muted">${s.checked_at?'проверен '+esc(fmtDate(s.checked_at)):'не проверялся'}</span></div>
    ${s.checked_at&&!s.ok?`<div class="hint warnbox">⚠️ ${esc(s.error||'нет ответа')}${s.hint?'<br><span class="small">'+esc(s.hint)+'</span>':''}</div>`:''}
    <div class="kv"><span class="k">Версия</span><span>${esc(axVersion(s.version)||'—')}</span></div>
    ${licRows||'<div class="muted small">Сведений о лицензии нет — нажмите «Обновить состояние».</div>'}
    ${(s.warnings||[]).length?`<div class="hint" style="margin-top:8px">Не прочитано: ${esc(s.warnings.join('; '))}</div>`:''}
    ${doms?`<h4 style="margin:14px 0 6px">Домены</h4><div class="table-wrap"><table class="tbl"><tbody>${doms}</tbody></table></div>`:''}</div>`));
  const set=st.settings, ls=st.last_sync;
  grid.appendChild(h(`<div class="card"><div class="section-title"><h3>🔁 Сверка и доступ</h3></div>
    <div class="kv"><span class="k">Последняя сверка</span><span>${ls?`${esc(fmtDate(ls.finished_at))} — <span class="badge ${esc(ls.status)}">${esc(STATUS_LBL[ls.status]||ls.status)}</span> <a href="javascript:void(0)" data-job="${ls.job_id}" class="small">задание №${ls.job_id}</a><div class="small muted">${esc(ls.summary||'')}</div>`:'не было'}</span></div>
    <div class="kv"><span class="k">По расписанию</span><span>${set.sync_enabled?`да, <span class="mono">${esc(set.sync_cron||'')}</span> (${esc(cronHuman(set.sync_cron||''))})`:'нет'}</span></div>
    <div class="kv"><span class="k">Слежение за сервером</span><span>${set.monitor_enabled?'каждые 15 минут':'выключено'}</span></div>
    <div class="kv"><span class="k">CLI</span><span>${esc(set.host)}:${set.cli_port} · ${set.cli_tls==='tls'?'SSL'+(set.cli_cert?' · сертификат по отпечатку':''):(set.loopback?'без шифрования (эта же машина)':(set.allow_plaintext?'<span class="tag err">без шифрования</span>':'<span class="tag warn">нужен SSL</span>'))}</span></div>
    <div class="kv"><span class="k">FTP Backup</span><span>${esc(set.host)}:${set.ftp_port} · ${set.ftp_tls==='none'?(set.loopback?'без шифрования (эта же машина)':(set.allow_plaintext?'<span class="tag err">без шифрования</span>':'<span class="tag warn">нужен TLS</span>')):(set.ftp_tls==='implicit'?'TLS сразу':'AUTH TLS')+(set.ftp_cert?' · сертификат по отпечатку':'')}</span></div>
    <div class="kv"><span class="k">Домены</span><span>${(set.domains||[]).length?esc(set.domains.join(', ')):'все'}</span></div>
    <div class="kv"><span class="k">Изменения на сервере</span><span>${set.allow_changes?'<span class="tag warn">разрешены</span> (по кнопке, с проверкой и журналом)':'<span class="tag">только чтение</span>'}</span></div>
    ${data.orphans.length?`<div class="hint warnbox" style="margin-top:10px">Ящиков архива этого сервера, которых нет среди учётных записей: <b>${fmtNum(data.orphans.length)}</b> — ${esc(data.orphans.slice(0,5).map(o=>o.name).join(', '))}${data.orphans.length>5?' …':''}. <a href="#/axigen?tab=accounts" data-orph>Показать</a></div>`:''}</div>`));
  grid.querySelectorAll('[data-job]').forEach(a=>a.onclick=()=>jobDetails(parseInt(a.dataset.job,10)));
  const orph=grid.querySelector('[data-orph]'); if(orph) orph.onclick=()=>{ Ax.filter='orphans'; };
}

async function axAccounts(box, st){
  const data=await api('/axigen/accounts');
  const rows=data.accounts;
  const byEmail=new Map(rows.map(r=>[r.email, r]));
  Ax.sel.forEach(e=>{ if(!byEmail.has(e)) Ax.sel.delete(e); });
  const count=f=>rows.filter(r=>axMatches(r,f,'')).length;
  const chips=[['', 'Все'],['nomailbox','Без копии в архиве'],['archived','Копируются'],['blocked','Вход закрыт'],['quota','У предела квоты'],
    ['inactive',`Не входили ${AX_INACTIVE_DAYS}+ дн.`],['deleted','Удалены на сервере'],['errors','Прочитаны не полностью']];
  const top=h(`<div>
    <div class="btn-row" style="margin-bottom:10px;flex-wrap:wrap;gap:6px" data-chips>
      ${chips.map(([f,t])=>`<button class="chip ${Ax.filter===f?'active':''}" data-f="${f}">${t} (${fmtNum(count(f))})</button>`).join('')}
      <button class="chip ${Ax.filter==='orphans'?'active':''}" data-f="orphans" title="Ящики архива с адресом этого сервера, которых нет среди учётных записей">Нет на сервере (${fmtNum(data.orphans.length)})</button></div>
    <div class="an-toolbar" style="margin-bottom:10px;flex-wrap:wrap">
      <input type="text" id="axQ" placeholder="Поиск: адрес, ФИО, псевдоним" value="${esc(Ax.query)}" style="flex:1 1 260px;min-width:200px">
      <button class="btn sm" id="axAdd" title="Завести в архиве ящики для отмеченных учётных записей (на сервере ничего не меняется)">📥 Завести ящики в архиве</button>
      <button class="btn sm" id="axAct" title="Действие с ящиками отмеченных учётных записей: проверка, затем выполнение">🧰 Действие…</button>
      ${st.settings.allow_changes?'<button class="btn sm" id="axNew">➕ Создать учётную запись</button>':''}
      <button class="btn sm ghost" id="axXls" title="Учётные записи и их ящики в архиве — файлом Excel">📊 Excel</button>
    </div>
    <div class="small muted" id="axSelInfo" style="margin-bottom:6px"></div></div>`);
  box.appendChild(top);
  const wrap=h('<div class="card table-wrap"></div>'); box.appendChild(wrap);
  const selInfo=$('#axSelInfo', top);
  const visible=()=>Ax.filter==='orphans'?[]:rows.filter(r=>axMatches(r, Ax.filter, Ax.query));
  const draw=()=>{
    if(Ax.filter==='orphans'){
      wrap.innerHTML=data.orphans.length?`<table class="tbl"><thead><tr><th>Ящик архива</th><th>Логин</th><th>Копирование</th><th></th></tr></thead><tbody>${data.orphans.map(o=>`<tr><td><b>${esc(o.name)}</b></td><td class="mono small">${esc(o.username)}</td><td>${o.enabled?'<span class="tag ok">включено</span>':'<span class="tag">выключено</span>'}</td><td><button class="btn sm" data-open="${o.id}">Открыть</button></td></tr>`).join('')}</tbody></table>
        <div class="hint" style="margin:8px">Такого адреса нет среди учётных записей последней сверки: ящик удалён на сервере до первой сверки, логин записан иначе (псевдоним, другой регистр, без домена) или домен не входит в «Домены» настроек.</div>`:'<div class="empty">Все ящики архива этого сервера нашлись среди учётных записей.</div>';
      wrap.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>accountModal(parseInt(b.dataset.open,10)));
      selInfo.textContent=''; return;
    }
    const list=visible();
    const shown=list.slice(0, Ax.shown);
    const allSel=shown.length && shown.every(r=>Ax.sel.has(r.email));
    wrap.innerHTML=list.length?`<table class="tbl"><thead><tr><th style="width:28px"><input type="checkbox" data-all ${allSel?'checked':''}></th><th>Учётная запись</th><th>Вход</th><th>Место на сервере</th><th>Последний вход</th><th>В архиве</th></tr></thead><tbody>
      ${shown.map(r=>{ const d=axDaysSince(r.last_login);
        return `<tr data-row="${esc(r.email)}" style="cursor:pointer">
        <td data-cb><input type="checkbox" data-sel="${esc(r.email)}" ${Ax.sel.has(r.email)?'checked':''}></td>
        <td><b>${esc(r.email)}</b>${r.errors?' <span class="tag warn" title="'+esc(r.errors)+'">не всё прочитано</span>':''}<div class="small muted">${esc([[r.first_name,r.last_name].filter(Boolean).join(' '), (r.aliases||[]).length?'псевдонимы: '+r.aliases.join(', '):''].filter(Boolean).join(' · ')||'—')}</div></td>
        <td>${axLoginTag(r)}</td><td style="min-width:150px">${axQuotaCell(r)}</td>
        <td class="small">${r.last_login?esc(fmtDay(r.last_login))+(d!=null&&d>=AX_INACTIVE_DAYS?` <span class="tag warn">${d} дн.</span>`:''):'<span class="muted">—</span>'}</td>
        <td>${axMailboxCell(r)}</td></tr>`; }).join('')}</tbody></table>
      ${list.length>shown.length?`<div style="padding:10px"><button class="btn sm" data-more>Показать ещё (${fmtNum(list.length-shown.length)})</button></div>`:''}`
      : `<div class="empty">${rows.length?'Ничего не подходит под отбор.':'Учётных записей ещё нет — нажмите «Сверить учётные записи».'}</div>`;
    const selected=[...Ax.sel].map(e=>byEmail.get(e)).filter(Boolean);
    const noBox=selected.filter(r=>!r.mailbox && !r.deleted_at).length, withBox=selected.filter(r=>r.mailbox).length;
    selInfo.innerHTML=selected.length?`Отмечено: <b>${fmtNum(selected.length)}</b> (с ящиком в архиве — ${fmtNum(withBox)}, без ящика — ${fmtNum(noBox)}) · <a href="javascript:void(0)" data-clear>снять отметки</a>`:'Отметьте учётные записи флажками, чтобы завести им ящики или выполнить действие.';
    const cl=selInfo.querySelector('[data-clear]'); if(cl) cl.onclick=()=>{ Ax.sel.clear(); draw(); };
    wrap.querySelectorAll('[data-sel]').forEach(cb=>cb.onchange=()=>{ cb.checked?Ax.sel.add(cb.dataset.sel):Ax.sel.delete(cb.dataset.sel); draw(); });
    const all=wrap.querySelector('[data-all]'); if(all) all.onchange=()=>{ shown.forEach(r=>all.checked?Ax.sel.add(r.email):Ax.sel.delete(r.email)); draw(); };
    wrap.querySelectorAll('[data-cb]').forEach(td=>td.onclick=e=>e.stopPropagation());
    wrap.querySelectorAll('tr[data-row]').forEach(tr=>tr.onclick=()=>axigenAccountModal(byEmail.get(tr.dataset.row), st));
    const more=wrap.querySelector('[data-more]'); if(more) more.onclick=()=>{ Ax.shown+=200; draw(); };
  };
  top.querySelectorAll('[data-chips] .chip').forEach(b=>b.onclick=()=>{ Ax.filter=b.dataset.f; Ax.shown=200; top.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x===b)); draw(); });
  let qt=null; $('#axQ', top).oninput=e=>{ clearTimeout(qt); qt=setTimeout(()=>{ Ax.query=e.target.value; Ax.shown=200; draw(); }, 200); };
  $('#axAdd', top).onclick=()=>{
    const list=[...Ax.sel].map(e=>byEmail.get(e)).filter(r=>r && !r.mailbox && !r.deleted_at);
    if(!list.length) return toast('Нечего заводить', 'Отметьте учётные записи без ящика в архиве (отбор «Без копии в архиве»).', 'warn');
    axigenAddMailboxes(list.map(r=>r.email));
  };
  $('#axAct', top).onclick=()=>{
    const ids=[...Ax.sel].map(e=>byEmail.get(e)).filter(r=>r && r.mailbox).map(r=>r.mailbox.id);
    if(!ids.length) return toast('Нет ящиков', 'Действия выполняются с ящиками архива — отметьте учётные записи, у которых есть ящик.', 'warn');
    axigenActionPicker(ids);
  };
  const nb=$('#axNew', top); if(nb) nb.onclick=()=>axigenCreateModal();
  $('#axXls', top).onclick=()=>{ location.href='/api/axigen/accounts/export?format=xlsx'; };
  draw();
}

/** Выбор группового действия Axigen для ящиков (дальше — обычное окно проверки и запуска). */
async function axigenActionPicker(ids){
  let meta;
  try{ meta=await api('/accounts/bulk/actions'); State.bulkActions=meta; }catch(e){ return toastErr(e); }
  const acts=(meta.actions||[]).filter(a=>a.group==='axigen');
  const m=modal(`Действие с ящиками: ${fmtNum(ids.length)}`, `<p class="muted" style="margin-top:0">Сначала будет проверка: что сделается с каждым ящиком и что пропустится — без изменений, пока вы не нажмёте «Выполнить».</p>
    <div class="btn-row" style="flex-direction:column;align-items:stretch;gap:8px">${acts.map(a=>`<button class="btn${a.danger>=2?' danger':''}" data-k="${esc(a.key)}" title="${esc(a.desc||'')}">${a.icon||''} ${esc(a.label)}</button>`).join('')}</div>`);
  m.body.querySelectorAll('[data-k]').forEach(b=>b.onclick=()=>{ m.close(); bulkDialog(b.dataset.k, ids, {}, {onDone:()=>{}}); });
}

async function axigenAddMailboxes(emails){
  const m=modal('Завести ящики в архиве', `<p class="muted" style="margin-top:0">Ящики заводятся только в MailArchiver — на сервере Axigen ничего не меняется.</p>
    <div class="form-row"><label>Как копировать</label><select id="axAuth"><option value="axigen_ftp">Через FTP Backup Axigen — без паролей сотрудников</option><option value="password">По IMAP с паролем (пароль нужно будет вписать)</option></select></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="axEn" checked><span class="track"></span></span><label>Сразу включить копирование</label></div>
    <div data-pv><div class="empty"><span class="spinner"></span></div></div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go disabled>Завести</button>'});
  const g=x=>m.body.querySelector(x), go=m.foot.querySelector('[data-go]');
  const body=preview=>({emails, auth:g('#axAuth').value, enable:g('#axEn').checked, preview});
  const pv=async()=>{ try{ const r=await api('/axigen/mailboxes',{method:'POST',body:body(true)});
      g('[data-pv]').innerHTML=`<div class="table-wrap" style="max-height:300px;overflow:auto"><table class="tbl"><tbody>${r.items.map(i=>`<tr><td class="mono small">${esc(i.email)}</td><td><span class="tag ${i.status==='ok'?'ok':''}">${i.status==='ok'?'будет заведён':'пропуск'}</span></td><td class="small">${esc(i.text)}</td></tr>`).join('')}</tbody></table></div>`;
      go.disabled=!r.would_create; go.textContent=r.would_create?`Завести ${fmtNum(r.would_create)}`:'Нечего заводить';
    }catch(e){ g('[data-pv]').innerHTML=`<div class="hint warnbox">${esc(e.message)}</div>`; } };
  g('#axAuth').onchange=pv; g('#axEn').onchange=pv;
  m.foot.querySelector('[data-c]').onclick=m.close;
  go.onclick=async()=>{ go.disabled=true; try{ const r=await api('/axigen/mailboxes',{method:'POST',body:body(false)}); toast('Готово', `Заведено ящиков: ${r.created}`); m.close(); Ax.sel.clear(); route(); }catch(e){ toastErr(e); go.disabled=false; } };
  pv();
}

/** Карточка учётной записи сервера: что знает сверка, ящик в архиве и действия. */
function axigenAccountModal(r, st){
  if(!r) return;
  const allow=!!(st&&st.settings&&st.settings.allow_changes);
  const b=r.mailbox;
  const reg=Object.entries((r.info||{}).registry||{}).map(([k,v])=>`<div class="kv"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`).join('');
  const kv=(k,v)=>`<div class="kv"><span class="k">${k}</span><span>${v}</span></div>`;
  const acts=b?[
    ['axigen_block','🔒 Заблокировать вход', r.login_blocked!==true],
    ['axigen_unblock','🔓 Разблокировать вход', r.login_blocked===true],
    ['axigen_password','🔑 Сменить пароль на случайный', true],
    ['axigen_purge','🧹 Удалить старые письма с сервера', b.auth_type==='axigen_ftp'],
    ['axigen_delete','🗑️ Удалить учётную запись', r.login_blocked===true],
    ['axigen_use_ftp', b.auth_type==='axigen_ftp'?'↩️ Копировать по IMAP с паролем':'📮 Копировать через FTP Backup', true],
  ]:[];
  const m=modal(`Учётная запись ${r.email}`, `
    <div class="grid cols-2">
      <div>
        <h4 style="margin:0 0 6px">На сервере Axigen</h4>
        ${kv('ФИО', esc([r.first_name,r.last_name].filter(Boolean).join(' ')||'—'))}
        ${kv('Псевдонимы', esc((r.aliases||[]).join(', ')||'—'))}
        ${kv('Вход', axLoginTag(r))}
        ${kv('Службы', esc(r.services==null?'неизвестно':(r.services.join(', ')||'нет ни одной')))}
        ${r.saved_services?kv('До блокировки', esc(r.saved_services.join(', '))):''}
        ${kv('Место', axQuotaCell(r))}
        ${kv('Писем / папок', `${r.messages!=null?fmtNum(r.messages):'?'} / ${r.folders!=null?fmtNum(r.folders):'?'}`)}
        ${kv('Последний вход', r.last_login?esc(fmtDate(r.last_login)):'—')}
        ${kv('Сверка', `впервые ${esc(fmtDate(r.first_seen))}, последняя ${esc(fmtDate(r.last_seen))}`)}
        ${r.deleted_at?kv('Удалена на сервере', esc(fmtDate(r.deleted_at))):''}
        ${r.errors?`<div class="hint warnbox">Не прочитано при сверке: ${esc(r.errors)}</div>`:''}
        ${reg?`<details style="margin-top:8px"><summary class="small">Регистрационные сведения</summary>${reg}</details>`:''}
      </div>
      <div>
        <h4 style="margin:0 0 6px">В архиве</h4>
        ${b?`${kv('Ящик', `<b>${esc(b.name)}</b>${b.enabled?'':' <span class="tag">выключен</span>'}`)}
          ${kv('Копируется', esc(AX_COPY[b.auth_type]||b.auth_type)+(b.same_server?'':' · <span class="tag warn">другой сервер: '+esc(b.host)+'</span>'))}
          ${kv('Последняя копия', b.last_backup_at?esc(fmtDate(b.last_backup_at)):'не было')}
          ${kv('Писем в архиве', `${fmtNum(b.messages)} (${esc(fmtBytes(b.bytes))})`)}
          ${b.hold_until?kv('Удержание', esc(holdDate(b.hold_until))):''}
          <div class="btn-row" style="margin-top:8px"><button class="btn sm" data-open>✏️ Ящик</button><button class="btn sm" data-mail>📧 Письма</button><button class="btn sm" data-backup ${b.enabled?'':'disabled'}>💾 Копия сейчас</button></div>`
        : `<div class="hint warnbox">Этой учётной записи нет в архиве — её письма не копируются.</div>
           ${r.deleted_at?'':'<button class="btn sm primary" data-addbox>📥 Завести ящик в архиве</button>'}`}
        ${b?`<h4 style="margin:16px 0 6px">Действия</h4>
          <div class="btn-row" style="flex-direction:column;align-items:stretch;gap:6px">${acts.filter(a=>a[2]).map(([k,t])=>`<button class="btn sm${k==='axigen_delete'||k==='axigen_purge'?' danger':''}" data-act="${k}">${t}</button>`).join('')}</div>
          <div class="small muted" style="margin-top:6px">${allow?'Каждое действие сначала показывает проверку и ничего не меняет, пока вы не подтвердите.':'Изменения на сервере не разрешены — включите «Разрешить изменения на сервере» в настройках Axigen.'}</div>`:''}
      </div></div>`,
    {wide:true, footer:`<button class="btn ghost" data-c>Закрыть</button>${r.deleted_at?'':'<button class="btn" data-reload>🔄 Перечитать с сервера</button>'}${allow&&!r.deleted_at?'<button class="btn" data-pw>🔑 Задать пароль…</button>':''}`});
  const q=x=>m.body.querySelector(x), f=x=>m.foot.querySelector(x);
  f('[data-c]').onclick=m.close;
  if(q('[data-open]')) q('[data-open]').onclick=()=>{ m.close(); accountModal(b.id); };
  if(q('[data-mail]')) q('[data-mail]').onclick=()=>{ m.close(); State.mailAccount=b.id; location.hash='#/mail'; };
  if(q('[data-backup]')) q('[data-backup]').onclick=()=>backupNow(b.id, b.name);
  if(q('[data-addbox]')) q('[data-addbox]').onclick=()=>{ m.close(); axigenAddMailboxes([r.email]); };
  m.body.querySelectorAll('[data-act]').forEach(btn=>btn.onclick=()=>{ m.close(); bulkDialog(btn.dataset.act, [b.id], btn.dataset.act==='axigen_use_ftp'?{to:b.auth_type==='axigen_ftp'?'password':'axigen_ftp'}:{}, {onDone:()=>{ if(State.view==='axigen') setTimeout(route, 400); }}); });
  if(f('[data-pw]')) f('[data-pw]').onclick=()=>{ m.close(); axigenPasswordModal(r.email); };
  if(f('[data-reload]')) f('[data-reload]').onclick=async(ev)=>{ const btn=ev.currentTarget; btn.disabled=true; btn.innerHTML='<span class="spinner"></span>';
    try{ const res=await api('/axigen/accounts/reload',{method:'POST',body:{email:r.email}}); m.close(); toast('Перечитано', r.email);
      const fresh=Object.assign({}, r, res.account||{}); axigenAccountModal(fresh, st); if(State.view==='axigen') route(); }
    catch(e){ toastErr(e); btn.disabled=false; btn.textContent='🔄 Перечитать с сервера'; } };
}

function axShowPasswordOnce(title, email, password){
  modal(title, `<p class="small">Новый пароль учётной записи <b>${esc(email)}</b>. Он показывается <b>один раз</b> и нигде не сохраняется (кроме карточки ящика, который копируется по IMAP) — передайте его сотруднику.</p>
    <input type="text" class="mono" readonly value="${esc(password)}" style="width:100%" onclick="this.select()">`);
}

function axigenPasswordModal(email){
  const m=modal(`Пароль: ${email}`, `
    <div class="form-row check"><span class="switch"><input type="checkbox" id="axGen" checked><span class="track"></span></span><label>Случайный пароль (покажется один раз)</label></div>
    <div class="form-row" id="axPwRow" style="display:none"><label>Новый пароль</label><input type="password" id="axPw" autocomplete="new-password" placeholder="не короче 6 знаков, без пробелов и кавычек"></div>
    <div class="hint">Если ящик этой учётной записи копируется по IMAP, новый пароль сразу сохранится и в его карточке — копирование не прервётся.</div>`,
    {footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn danger" data-go>Задать пароль</button>'});
  const g=x=>m.body.querySelector(x);
  g('#axGen').onchange=()=>{ g('#axPwRow').style.display=g('#axGen').checked?'none':'block'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async(ev)=>{ const b=ev.currentTarget; b.disabled=true;
    try{ const r=await api('/axigen/accounts/password',{method:'POST',body:{email, generate:g('#axGen').checked, password:g('#axPw').value}});
      m.close(); toast('Пароль изменён', r.saved_in_mailbox?'Сохранён и в карточке ящика.':'');
      if(r.password) axShowPasswordOnce('Новый пароль', email, r.password); }
    catch(e){ toastErr(e); b.disabled=false; } };
}

function axigenCreateModal(){
  const m=modal('Новая учётная запись на сервере Axigen', `
    <div class="form-row"><label>Адрес</label><input type="email" id="axE" placeholder="ivanov@company.ru"></div>
    <div class="grid cols-2"><div class="form-row"><label>Имя</label><input type="text" id="axF"></div><div class="form-row"><label>Фамилия</label><input type="text" id="axL"></div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="axGen" checked><span class="track"></span></span><label>Случайный пароль (покажется один раз)</label></div>
    <div class="form-row" id="axPwRow" style="display:none"><label>Пароль</label><input type="password" id="axPw" autocomplete="new-password"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="axArch" checked><span class="track"></span></span><label>Сразу завести ящик в архиве</label></div>`,
    {footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Создать</button>'});
  const g=x=>m.body.querySelector(x);
  g('#axGen').onchange=()=>{ g('#axPwRow').style.display=g('#axGen').checked?'none':'block'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async(ev)=>{ const b=ev.currentTarget; b.disabled=true;
    const email=g('#axE').value.trim();
    try{ const r=await api('/axigen/accounts/create',{method:'POST',body:{email, first_name:g('#axF').value, last_name:g('#axL').value, generate:g('#axGen').checked, password:g('#axPw').value, add_to_archive:g('#axArch').checked}});
      m.close(); toast('Учётная запись создана', r.email + (r.mailbox&&r.mailbox.status==='ok'?' · ящик в архиве заведён':''));
      if(r.password) axShowPasswordOnce('Пароль новой учётной записи', r.email, r.password);
      if(State.view==='axigen') route(); }
    catch(e){ toastErr(e); b.disabled=false; } };
}

async function axQueue(box){
  box.innerHTML='<div class="card"><div class="section-title"><h3>📨 Очередь писем сервера</h3><div class="spacer"></div><button class="btn sm" data-r>🔄 Обновить</button></div><div data-q><div class="empty"><span class="spinner"></span></div></div><div class="small muted" style="margin-top:6px">Только просмотр. Повторить отправку или удалить письмо из очереди — в WebAdmin Axigen.</div></div>';
  const q=box.querySelector('[data-q]');
  const load=async()=>{ q.innerHTML='<div class="empty"><span class="spinner"></span></div>';
    try{ const r=await api('/axigen/queue');
      if(!r.rows.length){ q.innerHTML=`<div class="empty">${r.count?'Писем в очереди: '+fmtNum(r.count):'Очередь пуста'}</div>${r.raw?'<pre class="mono small" style="white-space:pre-wrap">'+esc(r.raw)+'</pre>':''}`; return; }
      q.innerHTML=`<div class="small muted" style="margin-bottom:6px">Писем: <b>${fmtNum(r.count)}</b>${r.truncated?' (показаны первые '+fmtNum(r.rows.length)+')':''}</div>
        <div class="table-wrap"><table class="tbl"><thead><tr>${r.columns.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${r.rows.map(row=>`<tr>${r.keys.map(k=>`<td class="small">${esc(row[k]||'')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }catch(e){ q.innerHTML=`<div class="hint warnbox">${esc(e.message)}</div>`; } };
  box.querySelector('[data-r]').onclick=load;
  load();
}

async function axLog(box){
  const kinds=[['people','Действия людей'],['','Всё'],['write','Изменения на сервере'],['console','Консоль'],['check','Проверки подключения'],['monitor','Плановые проверки'],['error','Ошибки']];
  box.innerHTML=`<div class="btn-row" style="margin-bottom:10px;gap:6px;flex-wrap:wrap" data-chips>${kinds.map(([k,t])=>`<button class="chip ${Ax.logKind===k?'active':''}" data-k="${k}">${t}</button>`).join('')}</div>
    <div class="card table-wrap" data-list></div>
    <div class="small muted" style="margin-top:6px">Каждая сессия CLI записывается целиком: что отправлено серверу и что он ответил. Пароли в журнал не попадают.</div>`;
  const list=box.querySelector('[data-list]');
  let items=[];
  const render=()=>{ list.innerHTML=items.length?`<table class="tbl"><thead><tr><th>Когда</th><th>Кто</th><th>Вид</th><th>Что</th><th>Команд</th><th>Итог</th></tr></thead><tbody>${items.map(i=>`<tr data-id="${i.id}" style="cursor:pointer"><td class="small nowrap">${esc(fmtDate(i.ts))}</td><td class="small">${esc(i.username||'')}</td><td><span class="tag">${esc(AX_KIND[i.kind]||i.kind)}</span></td><td class="small">${esc(i.summary||'')}</td><td class="small">${fmtNum(i.commands)}</td><td>${i.ok?'<span class="tag ok">успешно</span>':'<span class="tag err">ошибка</span>'}</td></tr>`).join('')}</tbody></table>
      ${items.length%100===0?'<div style="padding:10px"><button class="btn sm" data-more>Показать раньше</button></div>':''}`:'<div class="empty">Записей нет.</div>';
    list.querySelectorAll('tr[data-id]').forEach(tr=>tr.onclick=()=>axLogItem(parseInt(tr.dataset.id,10)));
    const more=list.querySelector('[data-more]'); if(more) more.onclick=()=>load(items[items.length-1].id);
  };
  const load=async(before=0)=>{ try{ const r=await api(`/axigen/log?kind=${encodeURIComponent(Ax.logKind)}&before=${before}&limit=100`); items=before?items.concat(r.items):r.items; render(); }catch(e){ list.innerHTML=`<div class="hint warnbox">${esc(e.message)}</div>`; } };
  box.querySelectorAll('[data-chips] .chip').forEach(b=>b.onclick=()=>{ Ax.logKind=b.dataset.k; box.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x===b)); load(); });
  load();
}

async function axLogItem(id){
  try{ const i=await api(`/axigen/log/${id}`);
    const m=modal(`Сессия CLI: ${i.summary||''}`, `<div class="small muted" style="margin-bottom:8px">${esc(fmtDate(i.ts))} · ${esc(i.username||'')} · ${esc(AX_KIND[i.kind]||i.kind)} · команд: ${fmtNum(i.commands)} · ${i.ok?'успешно':'ошибка'}</div>
      <pre class="mono small" style="white-space:pre-wrap;max-height:65vh;overflow:auto;background:var(--bg-soft);padding:10px;border-radius:6px">${esc(i.transcript||'')}</pre>`,
      {wide:true, footer:'<button class="btn ghost" data-dl>⬇️ Скачать</button><button class="btn primary" data-c>Закрыть</button>'});
    m.foot.querySelector('[data-c]').onclick=m.close;
    m.foot.querySelector('[data-dl]').onclick=()=>axDownloadText(`axigen_session_${id}.txt`, i.transcript||'');
  }catch(e){ toastErr(e); }
}

function axDownloadText(name, text){
  const url=URL.createObjectURL(new Blob([text], {type:'text/plain;charset=utf-8'}));
  const a=document.createElement('a'); a.href=url; a.download=name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url), 2000);
}

async function axConsole(box){
  const samples=['LIST Domains','SHOW LicenseInfo','UPDATE Domain name '+((State.axDomain)||'company.ru')+'\nLIST Accounts','ENTER QUEUE\nLIST','GET VERSION'];
  box.innerHTML=`<div class="card">
    <div class="section-title"><h3>⌨️ Консоль CLI — только чтение</h3></div>
    <div class="small muted" style="margin-bottom:8px">Команды выполняются одной сессией по порядку (по одной в строке). Разрешено только читать: LIST, SHOW, GET, SEARCH, LOOKUP, HELP и переход в контекст (UPDATE Domain/Account/Group/List, CONFIG, ENTER QUEUE, BACK). Изменения — кнопками MailArchiver или в WebAdmin.</div>
    <div class="btn-row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${samples.map((s,i)=>`<button class="chip" data-s="${i}">${esc(s.split('\n').join(' → '))}</button>`).join('')}</div>
    <textarea id="axCmd" class="mono" rows="5" style="width:100%" placeholder="LIST Domains"></textarea>
    <div class="btn-row" style="margin-top:8px"><button class="btn primary" data-run>▶️ Выполнить</button></div>
    <div data-out style="margin-top:12px"></div></div>`;
  const ta=box.querySelector('#axCmd'), out=box.querySelector('[data-out]');
  box.querySelectorAll('[data-s]').forEach(b=>b.onclick=()=>{ ta.value=samples[parseInt(b.dataset.s,10)]; ta.focus(); });
  ta.onkeydown=e=>{ if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); box.querySelector('[data-run]').click(); } };
  box.querySelector('[data-run]').onclick=async(ev)=>{
    const cmds=ta.value.split('\n').map(s=>s.trim()).filter(Boolean);
    if(!cmds.length) return;
    const b=ev.currentTarget; b.disabled=true; out.innerHTML='<div class="empty"><span class="spinner"></span></div>';
    try{ const r=await api('/axigen/console',{method:'POST',body:{commands:cmds}});
      out.innerHTML=r.replies.map(x=>`<div style="margin-bottom:10px"><div class="mono small"><b>&lt;${esc(x.prompt||'')}&gt; ${esc(x.command)}</b> ${x.ok===false?'<span class="tag err">'+esc(x.status||'ошибка')+'</span>':(x.ok?'<span class="tag ok">+OK</span>':'')}</div>
        ${x.text?`<pre class="mono small" style="white-space:pre-wrap;max-height:50vh;overflow:auto;background:var(--bg-soft);padding:8px;border-radius:6px;margin:4px 0 0">${esc(x.text)}</pre>`:''}</div>`).join('');
    }catch(e){ out.innerHTML=`<div class="hint warnbox">${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; }
    finally{ b.disabled=false; }
  };
}

async function axDiag(box){
  box.innerHTML=`<div class="card"><div class="section-title"><h3>🩺 Диагностика</h3></div>
    <div class="small muted" style="margin-bottom:8px">Сырые ответы сервера на команды, которыми пользуется MailArchiver: версия, лицензия, домены, учётная запись (папки, псевдонимы, квота, службы), ограничения домена, очередь. Если сверка читает что-то не так — пришлите этот отчёт разработчикам (паролей в нём нет).</div>
    <div class="btn-row" style="gap:8px"><input type="email" id="axDE" placeholder="адрес учётной записи (пусто — первая из первого домена)" style="flex:1;min-width:260px"><button class="btn primary" data-run>Собрать</button></div>
    <div data-out style="margin-top:12px"></div></div>`;
  const out=box.querySelector('[data-out]');
  box.querySelector('[data-run]').onclick=async(ev)=>{ const b=ev.currentTarget; b.disabled=true; out.innerHTML='<div class="empty"><span class="spinner"></span> Опрашиваем сервер…</div>';
    try{ const r=await api('/axigen/diagnostics?email='+encodeURIComponent(box.querySelector('#axDE').value.trim()));
      const text=r.sections.map(s=>`### ${s.title}\n> ${s.command}\n${s.text}\n${s.ok===false?'-ERR ':'+OK '}${s.status||''}\n`).join('\n');
      out.innerHTML=`<div class="btn-row" style="margin-bottom:8px"><button class="btn sm" data-dl>⬇️ Скачать отчёт</button></div>`+r.sections.map(s=>`<details open style="margin-bottom:8px"><summary><b>${esc(s.title)}</b> <span class="mono small">${esc(s.command)}</span> ${s.ok===false?'<span class="tag err">'+esc(s.status||'ошибка')+'</span>':'<span class="tag ok">+OK</span>'}</summary>
        <pre class="mono small" style="white-space:pre-wrap;max-height:40vh;overflow:auto;background:var(--bg-soft);padding:8px;border-radius:6px">${esc(s.text||'(пусто)')}</pre></details>`).join('');
      out.querySelector('[data-dl]').onclick=()=>axDownloadText('axigen_diagnostics.txt', text);
    }catch(e){ out.innerHTML=`<div class="hint warnbox">${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; }
    finally{ b.disabled=false; } };
}

/** Проверка подключения: CLI и FTP Backup, сертификаты, «доверять этому сертификату». */
async function axigenCheckModal(){
  const m=modal('Проверка подключения к Axigen', '<div class="empty"><span class="spinner"></span> Подключаемся к CLI и FTP Backup…</div>', {wide:true});
  let r;
  try{ r=await api('/axigen/check',{method:'POST'}); }catch(e){ m.body.innerHTML=`<div class="hint warnbox">${esc(e.message)}</div>`; return; }
  if(r.error){ m.body.innerHTML=`<div class="hint warnbox">${esc(r.error)}${r.hint?'<br><span class="small">'+esc(r.hint)+'</span>':''}</div>`; return; }
  const part=(title, x, which, extra)=>`<div class="card" style="margin-bottom:12px;border-color:var(--${x.ok?'success':'danger'})">
    <div class="section-title"><h3>${title}</h3><div class="spacer"></div>${x.ok?'<span class="tag ok">✓ работает</span>':'<span class="tag err">✗ не работает</span>'}</div>
    ${x.ok?'':`<div class="hint warnbox">${esc(x.error||'')}${x.hint?'<br><span class="small">'+esc(x.hint)+'</span>':''}</div>`}
    ${x.fingerprint?`<div class="kv"><span class="k">Сертификат (SHA-256)</span><span class="mono small" style="word-break:break-all">${esc(x.fingerprint)}</span></div>
      ${(x.cert_problem||!x.ok)?`<div class="small muted">Сверьте отпечаток с сертификатом на сервере Axigen (например <span class="mono">openssl x509 -noout -fingerprint -sha256 -in файл.pem</span>). Совпал — доверяйте.</div><div class="btn-row" style="margin-top:6px"><button class="btn sm primary" data-trust="${which}" data-fp="${esc(x.fingerprint)}">🔐 Доверять этому сертификату</button></div>`:''}`:''}
    ${extra||''}</div>`;
  const cli=r.cli||{}, ftp=r.ftp||{}, lic=cli.license||{};
  const cliExtra=cli.ok?`<div class="kv"><span class="k">Версия</span><span>${esc(axVersion(cli.version)||'—')}</span></div>
    <div class="kv"><span class="k">Лицензия</span><span>${lic.expires?'до '+esc(fmtDay(lic.expires+'T00:00:00')):'срок не найден'}${lic.type?' · '+esc(lic.type):''}${lic.accounts_limit?' · учётных записей до '+fmtNum(lic.accounts_limit):''}</span></div>
    <div class="kv"><span class="k">Домены</span><span>${esc((cli.domains||[]).map(d=>d.name).join(', ')||'—')}</span></div>
    <div class="kv"><span class="k">Очередь</span><span>${cli.queue!=null?fmtNum(cli.queue)+' писем':'—'}</span></div>
    ${(cli.warnings||[]).length?`<div class="hint">Не прочитано: ${esc(cli.warnings.join('; '))}</div>`:''}`:'';
  const ftpExtra=ftp.ok?`<div class="kv"><span class="k">Шифрование</span><span>${esc(ftp.encryption||'')}</span></div>
    <div class="kv"><span class="k">Корень</span><span class="small">${esc((ftp.root||[]).join(', ')||'пусто')}</span></div>
    <div class="kv"><span class="k">Список каталогов</span><span>${ftp.mlsd?'MLSD':'LIST'}</span></div>`:'';
  m.body.innerHTML=part('⌨️ CLI', cli, 'cli', cliExtra)+(ftp.skipped?'':part('📦 FTP Backup', ftp, 'ftp', ftpExtra))
    +`<div class="small muted">Изменения на сервере: ${r.changes_allowed?'<b>разрешены</b>':'не разрешены (только чтение)'}. Журнал этой проверки — «Сервер Axigen → Журнал команд».</div>`;
  m.body.querySelectorAll('[data-trust]').forEach(b=>b.onclick=async()=>{
    const password=await axTrustDialog(b.dataset.trust, b.dataset.fp);
    if(!password) return;
    try{ await api('/axigen/trust',{method:'POST',body:{which:b.dataset.trust, fingerprint:b.dataset.fp, password}}); toast('Сертификат сохранён', 'Проверяем подключение заново'); m.close(); axigenCheckModal(); }
    catch(e){ toastErr(e); } });
}

/** «Доверять сертификату?» — с повторным вводом пароля (он уйдёт только серверу с этим сертификатом). */
function axTrustDialog(which, fp){
  return new Promise(res=>{
    let done=false; const answer=v=>{ if(done) return; done=true; res(v); };
    const m=modal('Доверять сертификату?', `<p>Отпечаток <span class="mono small">${esc(fp)}</span> будет сохранён: соединение с другим сертификатом MailArchiver отвергнет. Убедитесь, что отпечаток совпадает с сертификатом на сервере Axigen.</p>
      <div class="form-row"><label>${which==='ftp'?'Пароль для FTP Backup (как в настройках)':'Пароль администратора Axigen'}</label><input type="password" id="axTrustPw" autocomplete="off"></div>
      <div class="hint">Пароль вводится заново: сохранённый пароль не уходит серверу с другим сертификатом без подтверждения того, кто его знает.</div>`,
      {onClose:()=>answer(null), footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Доверять</button>`});
    const inp=m.body.querySelector('#axTrustPw');
    const ok=()=>{ if(!inp.value){ toast('Введите пароль','','warn'); inp.focus(); return; } answer(inp.value); m.close(); };
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    m.foot.querySelector('[data-ok]').onclick=ok;
    inp.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); ok(); } });
  });
}

/** Блок в «Настройки → Сервер Axigen». */
function axigenSettingsCard(card, unsaved){
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🔌 Проверка</h4>
    <div class="small muted">Сохраните настройки и проверьте подключение к CLI и FTP Backup. Если Axigen на другой машине, нужны SSL-слушатели CLI и FTP Backup на сетевом адресе (WebAdmin → Services → Listeners) — без шифрования пароль администратора по сети не отправляется.</div>
    <div class="btn-row" style="margin-top:8px"><button class="btn sm primary" data-check>Проверить подключение</button><a class="btn sm" href="#/axigen">Открыть раздел «Сервер Axigen»</a></div></div>`);
  card.appendChild(box);
  box.querySelector('[data-check]').onclick=()=>{ if(unsaved()) return toast('Сначала сохраните настройки','Проверка идёт по сохранённым настройкам.','warn'); axigenCheckModal(); };
}

/** Сведения сервера Axigen в карточке ящика. */
async function axigenMailboxInfo(id, box){
  try{
    const r=await api(`/axigen/mailbox/${id}`,{bg:true});
    if(!r.enabled || !box) return;
    const row=r.row;
    if(!row){ if(r.same_server) box.innerHTML='<div class="hint" style="margin-bottom:8px">📮 Этого адреса нет в последней сверке с Axigen.</div>'; return; }
    box.innerHTML=`<div class="hint" style="margin-bottom:8px">📮 <b>На сервере Axigen:</b> ${axLoginTag(row)} · ${row.used_bytes!=null?'занято '+esc(fmtBytes(row.used_bytes)):'место неизвестно'}${row.quota_kb?' из '+esc(fmtBytes(row.quota_kb*1024)):''}${row.last_login?' · последний вход '+esc(fmtDay(row.last_login)):''}${r.ftp_path?` · <span class="small">каталог FTP: <span class="mono">${esc(r.ftp_path)}</span>, взято файлов ${fmtNum(r.ftp.files)}</span>`:''}</div>`;
  }catch(e){}
}

/** Из меню ящика: учётная запись Axigen этого ящика (по последней сверке). */
async function axigenForMailbox(a){
  try{
    const [r, st]=await Promise.all([api(`/axigen/mailbox/${a.id}`), api('/axigen/status')]);
    if(!r.row) return modal(`Ящик ${a.name}`, `<div class="hint warnbox">Учётной записи «${esc(a.username)}» нет в последней сверке с Axigen${r.same_server?'':' — и ящик копируется не с этого сервера'}.</div><div class="small muted">Запустите сверку в разделе «Сервер Axigen» или проверьте логин ящика.</div>`);
    const data=await api('/axigen/accounts');
    const row=(data.accounts||[]).find(x=>x.email===r.row.email) || r.row;
    axigenAccountModal(row, st);
  }catch(e){ toastErr(e); }
}

// ===================================================================
//  Ящики на сервере: групповые действия с учётными записями Axigen и
//  с папками и письмами в ящиках любого сервера (по IMAP).
// ===================================================================
const Srv = { tab:'list', filter:'', query:'', sel:new Set(), shown:200, source:'selected', list:'', action:null, params:{}, meta:null,
  // откуда учётные записи: '' — все, 'ax' — Axigen, 'mb' — ящики архива без учётной записи, '<номер>' — подключение
  server:'', servers:[], ax:null };
const SRV_TABS = [['list','🧰 Ящики и действия'],['history','📜 История'],['schedules','🕑 Расписания']];
const SRV_SOON_DAYS = 30;
const SRV_FILTERS = [['','Все'],['srv','С учётной записью на сервере'],['other','Без учётной записи (IMAP)'],['imap','Вход по IMAP есть'],['noimap','Без входа по IMAP'],
  ['archived','Есть в архиве'],['noarchive','Нет в архиве'],['keeps','Архив хранит всё'],['blocked','Вход закрыт'],['quota','У предела квоты'],
  ['soon',`Упрутся в квоту за ${SRV_SOON_DAYS} дн.`],['inactive',`Не входили ${AX_INACTIVE_DAYS}+ дн.`],['dismissed','Уволены, вход открыт'],
  ['deleted','Удалены на сервере']];
const SRV_LOGIN = {ok:['ok','вход удался'], auth_error:['err','неверный пароль'], conn_error:['warn','нет связи'], no_password:['','нет пароля'], secret_broken:['err','пароль не читается']};

function srvMatches(r, f, q){
  const mb=r.mailbox;
  if(Srv.server){
    if(Srv.server==='ax'){ if(r.kind!=='axigen') return false; }
    else if(Srv.server==='mb'){ if(r.kind!=='imap') return false; }
    else if(!(r.server && String(r.server.id)===Srv.server)) return false;
  }
  if(f==='axigen' && r.kind!=='axigen') return false;
  if(f==='srv' && r.kind==='imap') return false;
  if(f==='other' && r.kind!=='imap') return false;
  if(f==='imap' && !(mb && mb.imap)) return false;
  if(f==='noimap' && (mb && mb.imap)) return false;
  if(f==='archived' && !mb) return false;
  if(f==='noarchive' && (mb || r.deleted)) return false;
  if(f==='keeps' && !(mb && mb.keeps_all)) return false;
  if(f==='blocked' && !(r.login_blocked===true && !r.deleted)) return false;
  if(f==='quota' && !(r.quota_percent!=null && r.quota_percent>=90)) return false;
  if(f==='soon' && !srvSoonFull(r)) return false;
  if(f==='dismissed' && !srvDismissedOpen(r)) return false;
  if(f==='inactive'){ const d=axDaysSince(r.last_login); if(r.deleted || d==null || d<AX_INACTIVE_DAYS) return false; }
  if(f==='deleted' && !r.deleted) return false;
  if(f!=='deleted' && r.deleted && !mb) return false;      // удалённые без архива — только в своём отборе
  q=(q||'').trim().toLowerCase();
  if(!q) return true;
  return [r.email, r.name, r.host, (r.aliases||[]).join(' '), mb&&mb.name, r.server&&r.server.name, r.server&&r.server.label, r.department]
    .some(v=>String(v||'').toLowerCase().includes(q));
}

/** Сотрудник уволен, а вход на сервере открыт. */
function srvDismissedOpen(r){ return !!r.dismissed_at && r.login_blocked===false && !r.deleted; }
/** При нынешнем росте упрётся в квоту за SRV_SOON_DAYS дней. */
function srvSoonFull(r){ const t=r.trend; return !r.deleted && !!t && t.days_to_quota!=null && t.days_to_quota<=SRV_SOON_DAYS; }
/** Рост места: «+120 МБ/мес · до квоты ~45 дн.» (по замерам при сверке и отчётах). */
function srvTrendHtml(r){
  const t=r.trend; if(!t) return '';
  const pm=t.per_month||0;
  const grow=Math.abs(pm)<1024*1024?'почти не растёт':`${pm>0?'+':'−'}${fmtBytes(Math.abs(pm))}/мес`;
  const d=t.days_to_quota;
  const left=d==null?'':(d<=0?' · <span class="tag err">квота занята</span>':` · <span class="${d<=SRV_SOON_DAYS?'tag err':(d<=90?'tag warn':'muted')}">до квоты ~${fmtNum(d)} дн.</span>`);
  return `<div class="small muted" title="${esc(`По замерам с ${srvDay(t.since)} (${t.span_days} дн.): при сверке с Axigen, «Перечитать с сервера» и «Отчёте»`)}">📈 ${esc(grow)}${left}</div>`;
}
/** «2026-10-20» → «20.10.2026» (день замера — без времени). */
function srvDay(d){ const m=String(d||'').match(/^(\d{4})-(\d\d)-(\d\d)/); return m?`${m[3]}.${m[2]}.${m[1]}`:String(d||''); }
/** Место ящика не на Axigen — по последнему «Отчёту». */
function srvUsageCell(r){
  if(r.kind==='server' && (r.used_bytes!=null || r.quota_kb)) return axQuotaCell(r);
  const u=r.usage; if(!u) return `<span class="muted small">— (${r.kind==='server'?'сервер не сообщает; ':''}в «Отчёте»)</span>`;
  const pct=u.quota?Math.round(1000*u.used/u.quota)/10:null; const cls=pct>=90?'err':(pct>=75?'warn':'ok');
  return `<div class="small" title="${esc('По «Отчёту» от '+srvDay(u.day))}">${esc(fmtBytes(u.used))}${u.quota?' из '+esc(fmtBytes(u.quota))+` <span class="tag ${cls}">${pct}%</span>`:' <span class="muted">без квоты</span>'}</div>
    <div class="small muted">по «Отчёту» от ${esc(srvDay(u.day))}</div>`;
}

/** Ячейка «Сервер»: Axigen, подключение (вид и название) или ящик архива на своём IMAP-сервере. */
function srvServerCell(r){
  if(r.kind==='axigen') return `<span class="tag">Axigen</span><div class="muted mono">${esc(r.host||'—')}</div>`;
  if(r.kind==='server'){ const sv=r.server||{};
    return `<span class="tag" title="${esc(sv.label||'')}">${esc(sv.label||'сервер')}</span><div class="muted small">«${esc(sv.name||'')}»${sv.allow_changes?'':' · <span title="Изменения на этом сервере не разрешены (раздел «Почтовые серверы»)">только чтение</span>'}</div>`; }
  return `<span class="tag">IMAP</span><div class="muted mono">${esc(r.host||'—')}</div>`;
}
/** Под адресом: автоответ, пересылка, отдел и должность (у учётных записей подключений). */
function srvExtraTags(r){
  if(r.kind!=='server') return '';
  const out=[];
  if(r.autoreply) out.push('<span class="tag" title="Включён автоответ">🏖️ автоответ</span>');
  if((r.forward_to||[]).length) out.push(`<span class="tag" title="${esc('Пересылка: '+r.forward_to.join(', '))}">↪️ пересылка</span>`);
  if(r.account_kind && !['user',''].includes(r.account_kind)) out.push(`<span class="tag" title="Вид учётной записи на сервере">${esc(MS_KIND[r.account_kind]||r.account_kind)}</span>`);
  return out.length?' '+out.join(' '):'';
}

/** Список адресов → строки таблицы (по адресу, псевдониму или названию ящика). */
function srvParseList(text, rows){
  const by=new Map();
  rows.forEach(r=>{ by.set(String(r.email||'').toLowerCase(), r); (r.aliases||[]).forEach(a=>by.set(String(a).toLowerCase(), r)); if(r.mailbox) by.set(String(r.mailbox.name||'').toLowerCase(), r); });
  const found=[], missing=[], seen=new Set();
  String(text||'').split(/[\n,;\t]+/).forEach(raw=>{
    const t=raw.trim(); if(!t) return;
    const m=t.match(/<([^<>\s]+@[^<>\s]+)>/);
    const key=(m?m[1]:t).toLowerCase();
    const r=by.get(key);
    if(!r){ if(!missing.includes(t)) missing.push(t); return; }
    if(!seen.has(r.key)){ seen.add(r.key); found.push(r); }
  });
  return {found, missing};
}

function srvTargets(rows){
  if(Srv.source==='visible') return rows.filter(r=>srvMatches(r, Srv.filter, Srv.query));
  if(Srv.source==='list') return srvParseList(Srv.list, rows).found;
  return rows.filter(r=>Srv.sel.has(r.key));
}

async function srvMeta(){
  if(!Srv.meta) Srv.meta=await api('/server/actions');
  return Srv.meta;
}

async function viewServer(c){
  const tab=((location.hash.match(/[?&]tab=(\w+)/)||[])[1])||'list';
  Srv.meta=null;
  const [data, meta]=await Promise.all([api('/server/targets'), srvMeta()]);
  c.innerHTML='';
  const ax=data.axigen||{};
  const rows=data.rows||[];
  const servers=data.servers||[];
  Srv.servers=servers; Srv.ax=ax;
  const want=(location.hash.match(/[?&]srv=(\w+)/)||[])[1];
  if(want){ Srv.server=want; Srv.filter=''; }
  if(Srv.server && !['ax','mb'].includes(Srv.server) && !servers.some(x=>String(x.id)===Srv.server)) Srv.server='';
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Ящики на сервере</h2>
    <span class="tag">${fmtNum(rows.filter(r=>!r.deleted).length)} ${plural(rows.filter(r=>!r.deleted).length,'ящик','ящика','ящиков')}</span>
    ${ax.enabled?`<span class="tag mono" title="Сервер Axigen">${esc(ax.host||'Axigen')}</span>`:''}
    ${servers.map(x=>`<span class="tag" title="${esc(x.label+' — подключение «'+x.name+'»')}">${esc(x.label)}</span>`).join(' ')}
    <div class="spacer"></div>
    <a class="btn sm ghost" href="#/mailservers" title="Подключения к Microsoft 365, Google Workspace, Яндекс 360, Zimbra и другим серверам">🔌 Почтовые серверы</a>
    ${ax.enabled?`<button class="btn sm" id="srvSync" ${ax.sync_running?'disabled':''} title="Прочитать учётные записи Axigen заново">${ax.sync_running?'<span class="spinner"></span> Сверка идёт…':'🔁 Сверить с Axigen'}</button>`:''}
    <button class="btn sm ghost" id="srvXls" title="Список ящиков раздела — файлом Excel">📊 Excel</button></div>`));
  const notes=[];
  if(!ax.enabled && !servers.length) notes.push('Почтовый сервер не подключён — доступны действия по IMAP с ящиками архива. Учётные записи сервера (вход, пароли, квоты, псевдонимы, автоответ) появятся, когда вы подключите сервер в разделе «Почтовые серверы» (Microsoft 365, Google Workspace, Яндекс 360, Zimbra, Kerio…) или настроите Axigen («Настройки → Сервер Axigen»).');
  servers.filter(x=>!x.last_sync_at).forEach(x=>notes.push(`Сверки с «${x.name}» (${x.label}) ещё не было — учётные записи этого сервера появятся после неё (раздел «Почтовые серверы» → «Сверить»).`));
  if(servers.length && servers.every(x=>!x.allow_changes) && !(ax.enabled && ax.allow_changes)) notes.push('Изменения на подключённых серверах не разрешены — доступны чтение, отчёты и действия по IMAP. Разрешить можно в карточке подключения («Почтовые серверы» → «Изменить»).');
  else if(!ax.allow_changes) notes.push('Изменения учётных записей Axigen выключены — действия, которые их меняют, недоступны. Включите «Разрешить изменения на сервере» в «Настройки → Сервер Axigen».');
  if(ax.enabled && !ax.last_sync) notes.push('Сверки с Axigen ещё не было — нажмите «Сверить с Axigen», чтобы увидеть учётные записи сервера.');
  notes.forEach(t=>c.appendChild(h(`<div class="hint" style="margin-bottom:10px">💡 ${esc(t)}</div>`)));
  if((data.running||[]).length) c.appendChild(h(`<div class="hint warnbox" style="margin-bottom:10px">⚙️ Идёт действие с ящиками на сервере: ${data.running.map(j=>`<a href="javascript:void(0)" data-job="${j.id}">задание №${j.id}</a>`).join(', ')}. Новое встанет в очередь за ним.</div>`));
  c.querySelectorAll('[data-job]').forEach(a=>a.onclick=()=>jobDetails(parseInt(a.dataset.job,10)));
  const sync=$('#srvSync',c); if(sync) sync.onclick=async()=>{ sync.disabled=true; try{ const r=await api('/axigen/sync',{method:'POST'}); toast(r.already?'Сверка уже идёт':'Сверка запущена', `Задание №${r.job_id}`); axWatchJob(r.job_id); }catch(e){ toastErr(e); sync.disabled=false; } };
  $('#srvXls',c).onclick=()=>apiDownload('/server/targets/export', {}, 'server_mailboxes.xlsx').catch(toastErr);
  c.appendChild(h(`<div class="tabs" role="tablist">${SRV_TABS.map(([k,t])=>`<a href="#/server?tab=${k}" class="${k===tab?'active':''}" role="tab">${t}</a>`).join('')}</div>`));
  const body=h('<div></div>'); c.appendChild(body);
  if(tab==='history') return srvHistory(body);
  if(tab==='schedules') return srvSchedules(body, meta);
  return srvList(body, rows, meta, ax, servers);
}

function srvLoginCell(r){
  if(r.kind==='axigen' || r.kind==='server') return axLoginTag(r) + (r.kind==='server'&&r.last_login?`<div class="small muted" title="Последний вход на сервере">вход ${esc(fmtDate(r.last_login))}</div>`:'');
  const mb=r.mailbox; if(!mb) return '<span class="muted">—</span>';
  const st=SRV_LOGIN[mb.login_status]; return st?`<span class="tag ${st[0]}">${st[1]}</span>`:'<span class="muted small">не проверялся</span>';
}
function srvArchiveCell(r){
  const mb=r.mailbox;
  if(!mb) return r.deleted?'<span class="tag err">удалена на сервере</span>':'<span class="tag warn">нет в архиве</span>';
  return `<b>${esc(mb.name)}</b>${mb.enabled?'':' <span class="tag">выключен</span>'}${mb.keeps_all?' <span class="tag ok" title="Архив хранит все письма навсегда: «хранить всё» или бессрочное удержание">хранит всё</span>':''}
    <div class="small muted">${esc(mb.auth)} · писем ${fmtNum(mb.messages)} · ${mb.last_backup_at?'копия '+esc(fmtDate(mb.last_backup_at)):'копий не было'}
    ${mb.imap?' · <span title="Войти в ящик по IMAP можно">IMAP ✓</span>':` · <span title="${esc(mb.imap_problem||'')}">IMAP ✗</span>`}</div>`;
}

function srvList(box, rows, meta, ax, servers){
  servers=servers||[];
  const canChange=(ax.enabled&&ax.allow_changes) || servers.some(x=>x.allow_changes);
  const byKey=new Map(rows.map(r=>[r.key, r]));
  const asked=Srv.sel.size;
  Srv.sel.forEach(k=>{ if(!byKey.has(k)) Srv.sel.delete(k); });
  if(Srv.pending){
    // повтор: отмеченные ящики должны быть видны — удалённые без архива показывает только свой отбор
    const p=Srv.pending; Srv.pending=null;
    const picked=[...Srv.sel].map(k=>byKey.get(k));
    const hidden=picked.filter(r=>!srvMatches(r,'','')).length;
    if(picked.length && hidden===picked.length) Srv.filter='deleted';
    const n=Srv.sel.size, gone=asked-n;
    const notes=[`«${p.label}» с прежними параметрами — нажмите «Проверить и запустить…».`];
    if(hidden && hidden<picked.length) notes.push(`${fmtNum(hidden)} из них удалены на сервере — их видно в отборе «Удалены на сервере» (отмечены и там).`);
    if(gone>0) notes.push(`${fmtNum(gone)} ${plural(gone,'ящика','ящиков','ящиков')} больше нет в таблице.`);
    if(p.blank) notes.push('Пароль введите заново — он не сохраняется.');
    if(p.uncertain) notes.push(`На ${fmtNum(p.uncertain)} ${plural(p.uncertain,'ящике','ящиках','ящиках')} задание прервалось посреди работы — часть изменений могла выполниться; посмотрите их в проверке.`);
    toast(`Отмечено ${fmtNum(n)} ${plural(n,'ящик','ящика','ящиков')}`, notes.join(' '), (gone||p.uncertain)?'warn':'success', 12000);
  }
  const count=f=>rows.filter(r=>srvMatches(r,f,'')).length;
  // что требует внимания: уволенные с открытым входом, ящики накануне квоты
  const nDism=count('dismissed'), nSoon=count('soon');
  const alerts=h(`<div>${nDism?`<div class="hint warnbox" style="margin-bottom:10px">👤 Сотрудники уволены, а вход на сервере открыт: <b>${fmtNum(nDism)}</b> — ${esc(rows.filter(srvDismissedOpen).slice(0,5).map(r=>r.email).join(', '))}${nDism>5?' …':''}.
      ${canChange?'<button class="btn sm" data-dism style="margin-left:6px">Показать и закрыть вход…</button>':'<span class="small">Закрыть вход можно, когда разрешены изменения на сервере.</span>'}</div>`:''}
    ${nSoon?`<div class="hint warnbox" style="margin-bottom:10px">📈 При нынешнем росте упрутся в квоту за ${SRV_SOON_DAYS} дней: <b>${fmtNum(nSoon)}</b> — ${esc(rows.filter(srvSoonFull).slice(0,5).map(r=>r.email).join(', '))}${nSoon>5?' …':''}.
      <button class="btn sm" data-soon style="margin-left:6px">Показать</button></div>`:''}</div>`);
  box.appendChild(alerts);
  // 1. ящики
  const card=h(`<div class="card bulk-card"><h3>1. Какие ящики</h3>
    <div class="bulk-chips" data-chips>${SRV_FILTERS.map(([f,t])=>`<button class="chip ${Srv.filter===f?'active':''}" data-f="${f}">${t} (${fmtNum(count(f))})</button>`).join('')}</div>
    <div class="an-toolbar" style="margin-bottom:8px;flex-wrap:wrap"><input type="text" id="srvQ" placeholder="Поиск: адрес, имя, псевдоним, сервер, отдел" value="${esc(Srv.query)}" style="flex:1 1 260px;min-width:200px">
      ${(servers.length+(ax.enabled?1:0))>=1?`<select id="srvFrom" title="Учётные записи какого сервера показывать" style="flex:0 1 260px">
        <option value="">Все серверы</option>${ax.enabled?`<option value="ax"${Srv.server==='ax'?' selected':''}>Axigen</option>`:''}
        ${servers.map(x=>`<option value="${x.id}"${Srv.server===String(x.id)?' selected':''}>${esc(x.label)} «${esc(x.name)}»</option>`).join('')}
        <option value="mb"${Srv.server==='mb'?' selected':''}>Ящики архива без учётной записи</option></select>`:''}</div>
    <div class="table-wrap" data-table style="max-height:460px;overflow:auto"></div>
    <div class="bulk-sources" style="margin-top:12px">
      <label class="radio"><input type="radio" name="srvSrc" value="selected"> Отмеченные в таблице — <b data-n="selected"></b></label>
      <label class="radio"><input type="radio" name="srvSrc" value="visible"> Все, что видны в таблице (отбор и поиск) — <b data-n="visible"></b></label>
      <label class="radio"><input type="radio" name="srvSrc" value="list"> По списку адресов (вставьте из Excel или письма):</label>
      <div class="bulk-sub" data-sub="list"><textarea id="srvL" rows="3" placeholder="ivanov@company.ru&#10;petrova@company.ru">${esc(Srv.list)}</textarea><div class="small muted" id="srvLinfo"></div></div>
    </div>
    <div class="bulk-total"><span id="srvTotal"></span></div></div>`);
  box.appendChild(card);
  const table=card.querySelector('[data-table]');
  const visible=()=>rows.filter(r=>srvMatches(r, Srv.filter, Srv.query));
  const updateTotals=()=>{
    $$('input[name=srvSrc]', card).forEach(x=>{ x.checked=(x.value===Srv.source); });
    $$('[data-sub]', card).forEach(el=>el.classList.toggle('on', el.dataset.sub===Srv.source));
    card.querySelector('[data-n=selected]').textContent=fmtNum(Srv.sel.size);
    card.querySelector('[data-n=visible]').textContent=fmtNum(visible().length);
    if(Srv.source==='list'){
      const res=srvParseList(Srv.list, rows);
      $('#srvLinfo',card).innerHTML=Srv.list.trim()?`Найдено: <b>${res.found.length}</b>`+(res.missing.length?` · не найдено: <b style="color:var(--danger)">${res.missing.length}</b> — ${esc(res.missing.slice(0,12).join(', '))}${res.missing.length>12?' …':''}`:''):'По одному адресу (или псевдониму) в строке; подойдут и через запятую.';
    }
    const n=srvTargets(rows).length;
    $('#srvTotal',card).innerHTML=`Будет обработано: <b>${fmtNum(n)}</b> ${plural(n,'ящик','ящика','ящиков')}`;
  };
  const draw=()=>{
    const list=visible(), shown=list.slice(0, Srv.shown);
    const allSel=shown.length && shown.every(r=>Srv.sel.has(r.key));
    table.innerHTML=list.length?`<table class="tbl srv-tbl"><thead><tr><th style="width:28px"><input type="checkbox" data-all ${allSel?'checked':''}></th><th>Ящик</th><th>Сервер</th><th>Вход</th><th>Место на сервере</th><th>В архиве</th></tr></thead><tbody>
      ${shown.map(r=>`<tr data-row="${esc(r.key)}">
        <td><input type="checkbox" data-sel="${esc(r.key)}" ${Srv.sel.has(r.key)?'checked':''}></td>
        <td class="srv-mail"><b>${esc(r.email)}</b>${srvExtraTags(r)}${r.dismissed_at?` <span class="tag ${srvDismissedOpen(r)?'err':''}" title="${esc('Сотрудник уволен '+fmtDate(r.dismissed_at)+(srvDismissedOpen(r)?' — а вход на сервере открыт':''))}">уволен</span>`:''}${r.errors?' <span class="tag warn" title="'+esc(r.errors)+'">не всё прочитано</span>':''}<div class="small muted">${esc([r.name!==r.email?r.name:'', (r.aliases||[]).length?'псевдонимы: '+r.aliases.join(', '):''].filter(Boolean).join(' · ')||'—')}</div></td>
        <td class="small">${srvServerCell(r)}</td>
        <td>${srvLoginCell(r)}</td><td style="min-width:150px">${r.kind==='axigen'?axQuotaCell(r):srvUsageCell(r)}${srvTrendHtml(r)}</td>
        <td>${srvArchiveCell(r)}</td></tr>`).join('')}</tbody></table>
      ${list.length>shown.length?`<div style="padding:10px"><button class="btn sm" data-more>Показать ещё (${fmtNum(list.length-shown.length)})</button></div>`:''}`
      : `<div class="empty">${rows.length?'Ничего не подходит под отбор.':'Ящиков нет: заведите ящики в «Почтовых ящиках», подключите почтовый сервер («Почтовые серверы») или настройте сервер Axigen.'}</div>`;
    table.querySelectorAll('[data-sel]').forEach(cb=>cb.onchange=()=>{ cb.checked?Srv.sel.add(cb.dataset.sel):Srv.sel.delete(cb.dataset.sel); if(Srv.source!=='selected'){ Srv.source='selected'; } draw(); });
    const all=table.querySelector('[data-all]'); if(all) all.onchange=()=>{ shown.forEach(r=>all.checked?Srv.sel.add(r.key):Srv.sel.delete(r.key)); Srv.source='selected'; draw(); };
    const more=table.querySelector('[data-more]'); if(more) more.onclick=()=>{ Srv.shown+=200; draw(); };
    updateTotals();
  };
  card.querySelectorAll('[data-chips] .chip').forEach(b=>b.onclick=()=>{ Srv.filter=b.dataset.f; Srv.shown=200; card.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x===b)); draw(); });
  let qt=null; $('#srvQ',card).oninput=e=>{ clearTimeout(qt); qt=setTimeout(()=>{ Srv.query=e.target.value; Srv.shown=200; draw(); }, 200); };
  const from=$('#srvFrom',card);
  if(from) from.onchange=()=>{ Srv.server=from.value; Srv.shown=200;
    // счётчики отборов — по выбранному серверу
    card.querySelectorAll('[data-chips] .chip').forEach(b=>{ b.textContent=`${SRV_FILTERS.find(x=>x[0]===b.dataset.f)[1]} (${fmtNum(count(b.dataset.f))})`; });
    if(location.hash.includes('srv=')) history.replaceState(null,'','#/server?tab=list');
    draw(); };
  $$('input[name=srvSrc]', card).forEach(x=>x.onchange=()=>{ Srv.source=x.value; updateTotals(); });
  let lt=null; $('#srvL',card).oninput=e=>{ Srv.list=e.target.value; Srv.source='list'; clearTimeout(lt); lt=setTimeout(updateTotals,250); };
  draw();

  // 2. действия
  const acts=h(`<div class="card bulk-card"><h3>2. Что сделать</h3><div class="bulk-groups"></div></div>`);
  const groupsBox=acts.querySelector('.bulk-groups');
  const clientTiles={
    accounts: ((ax.enabled&&ax.allow_changes) || servers.some(x=>x.allow_changes && (x.caps||[]).includes('create')))?[['create','➕','Создать учётные записи списком','Завести на сервере (Axigen или подключённом) много учётных записей сразу: из списка адресов или из раздела «Сотрудники»']]:[],
    archive: [['backup','💾','Сделать копию сейчас','Скопировать ящики архива выбранных строк'],['restore','♻️','Восстановить на сервер','Залить письма из архива обратно в ящики на сервере'],['excel','📊','Список ящиков в Excel','Выбранные ящики раздела — файлом Excel']],
  };
  meta.groups.forEach(g=>{
    const items=meta.actions.filter(a=>a.group===g.key);
    const extra=clientTiles[g.key]||[];
    if(!items.length && !extra.length) return;
    const gb=h(`<div class="bulk-group"><div class="bg-title">${esc(g.label)}</div><div class="bulk-tiles"></div></div>`);
    const tiles=gb.querySelector('.bulk-tiles');
    extra.filter(x=>g.key!=='archive').forEach(([k,ic,t,d])=>tiles.appendChild(h(`<button class="bulk-tile" data-client="${k}" title="${esc(d)}"><span class="ic">${ic}</span><span>${esc(t)}</span></button>`)));
    items.forEach(a=>{
      const cls=a.danger>=2?'danger':(a.danger===1?'warn':'');
      tiles.appendChild(h(`<button class="bulk-tile ${cls}" data-act="${esc(a.key)}" ${a.unavailable?'disabled style="opacity:.5;cursor:not-allowed"':''} title="${esc(a.unavailable?('Недоступно: '+a.unavailable):a.desc)}"><span class="ic">${a.icon}</span><span>${esc(a.label)}</span></button>`));
    });
    extra.filter(x=>g.key==='archive').forEach(([k,ic,t,d])=>tiles.appendChild(h(`<button class="bulk-tile" data-client="${k}" title="${esc(d)}"><span class="ic">${ic}</span><span>${esc(t)}</span></button>`)));
    groupsBox.appendChild(gb);
  });
  box.appendChild(acts);
  const paramsCard=h('<div class="card bulk-card" style="display:none"></div>');
  box.appendChild(paramsCard);
  const keys=()=>srvTargets(rows).map(r=>r.key);
  const select=(key)=>{
    Srv.action=key;
    $$('.bulk-tile', acts).forEach(t=>t.classList.toggle('active', t.dataset.act===key));
    const a=meta.actions.find(x=>x.key===key);
    if(!a){ paramsCard.style.display='none'; return; }
    const saved=Srv.params[key]||{};
    paramsCard.style.display='';
    paramsCard.innerHTML=`<h3>3. ${a.icon} ${esc(a.label)}</h3>
      <p class="muted" style="margin-top:-4px">${esc(a.desc)}</p>
      ${a.note?`<div class="hint" style="margin-bottom:12px">💡 ${esc(a.note)}</div>`:''}
      ${a.params.length?`<div class="bulk-form" data-form>${bulkFormHtml(a.params, saved)}</div>`:''}
      <div class="btn-row" style="margin-top:6px"><button class="btn ${a.danger>=2?'danger':'primary'}" data-go>Проверить и запустить…</button>
      ${a.schedulable&&!a.unavailable?'<button class="btn" data-sched title="Выполнять это действие для выбранных ящиков регулярно — только с вашего явного согласия">🕑 По расписанию…</button>':''}
      <span class="muted small" style="align-self:center">Сначала покажем, что будет сделано с каждым ящиком, — без изменений.</span></div>`;
    const form=paramsCard.querySelector('[data-form]');
    if(form) bulkFormWire(form, a.params);
    const sched=paramsCard.querySelector('[data-sched]');
    if(sched) sched.onclick=()=>{
      const values=form?bulkFormRead(form, a.params):{};
      Srv.params[key]=values;
      const ks=keys();
      if(!ks.length) return toast('Не выбрано ни одного ящика','Отметьте ящики в таблице или выберите их отбором','warn');
      srvScheduleModal(a, ks, values);
    };
    paramsCard.querySelector('[data-go]').onclick=async()=>{
      const values=form?bulkFormRead(form, a.params):{};
      Srv.params[key]=values;
      const ks=keys();
      if(!ks.length) return toast('Не выбрано ни одного ящика','Отметьте ящики в таблице или выберите их отбором','warn');
      const done=await bulkDialog(key, ks, values, {ep:'/server/ops', idsKey:'keys', meta:a, refreshView:'server', historyName:'истории раздела'});
      if(done){ Srv.action=null; }
    };
    paramsCard.scrollIntoView({behavior:'smooth', block:'nearest'});
  };
  $$('.bulk-tile', acts).forEach(t=>t.onclick=()=>{
    if(t.disabled) return;
    const k=t.dataset.client;
    if(k==='create') return srvCreateModal();
    if(k==='excel'){ const ks=keys(); return ks.length?apiDownload('/server/targets/export',{keys:ks},'server_mailboxes.xlsx').catch(toastErr):toast('Не выбрано ни одного ящика','','warn'); }
    if(k==='backup'||k==='restore'){
      const ids=[...new Set(srvTargets(rows).filter(r=>r.mailbox).map(r=>r.mailbox.id))];
      if(!ids.length) return toast('Нет ящиков в архиве','У выбранных строк нет ящиков в архиве — сначала «Завести ящики в архиве».','warn');
      return bulkDialog(k, ids, {}, {});
    }
    select(t.dataset.act);
  });
  if(Srv.action) select(Srv.action);
  const showFilter=f=>{ Srv.filter=f; Srv.query=''; $('#srvQ',card).value=''; Srv.shown=200; Srv.source='visible';
    card.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x.dataset.f===f)); draw(); };
  const dism=alerts.querySelector('[data-dism]');
  if(dism) dism.onclick=()=>{ showFilter('dismissed'); if(meta.actions.some(a=>a.key==='ax_block'&&!a.unavailable)) select('ax_block'); else card.scrollIntoView({behavior:'smooth'}); };
  const soon=alerts.querySelector('[data-soon]');
  if(soon) soon.onclick=()=>{ showFilter('soon'); card.scrollIntoView({behavior:'smooth', block:'start'}); };
}

// ---------- История раздела ----------
async function srvHistory(box){
  box.appendChild(h('<div class="card"><div class="section-title" style="margin-bottom:8px"><h3 style="margin:0">Действия с ящиками на сервере</h3><div class="spacer"></div><button class="btn sm ghost" data-r>↻ Обновить</button></div><div data-list><div class="empty"><span class="spinner"></span></div></div></div>'));
  const list=box.querySelector('[data-list]');
  const load=async()=>{
    let d; try{ d=await api('/server/history?limit=50',{bg:true}); }catch(e){ list.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
    const items=d.items||[];
    if(!items.length){ list.innerHTML='<div class="empty">Действий с ящиками на сервере ещё не было.</div>'; return; }
    list.innerHTML=`<div class="table-wrap"><table class="tbl srv-hist"><thead><tr><th>№</th><th>Когда</th><th>Кто</th><th>Действие</th><th>Ящиков</th><th>Итог</th><th></th></tr></thead><tbody>${items.map(o=>`<tr>
      <td class="muted nowrap">${o.id}</td><td class="small nowrap">${esc(fmtDate(o.created_at))}</td><td class="small">${esc(o.user)}</td>
      <td>${esc(o.label)}</td><td class="small">${fmtNum(o.total)}</td><td class="small">${esc(o.summary||'')}</td>
      <td style="text-align:right"><button class="btn sm" data-op="${o.id}">Подробнее</button></td></tr>`).join('')}</tbody></table></div>`;
    list.querySelectorAll('[data-op]').forEach(b=>b.onclick=()=>srvOpModal(parseInt(b.dataset.op,10), load));
  };
  box.querySelector('[data-r]').onclick=load;
  load();
}

const SRV_IST = {success:['ok','сделано'], partial:['warn','частично'], failed:['err','ошибка'], skipped:['','пропущено']};

async function srvOpModal(opId, reload){
  let o; try{ o=await api(`/server/history/${opId}`); }catch(e){ return toastErr(e); }
  let meta=null; try{ meta=await srvMeta(); }catch(_){}
  const job=o.job;
  const r={total:o.total, counts:{ok:o.ok, skip:o.skipped, fail:o.failed}, results:o.results||[], reasons:{}, warnings:[], jobs:[]};
  (o.results||[]).forEach(x=>{ if(x.status==='skip') r.reasons[x.detail]=(r.reasons[x.detail]||0)+1; });
  // параметры — подписями из описания действия («Какие пароли: случайные…»), а не ключами
  const pdefs=new Map((((meta&&meta.actions)||[]).find(a=>a.key===o.action)||{params:[]}).params.map(p=>[p.key,p]));
  const pval=(p,v)=>{
    if(p && p.kind==='bool') return v?'да':'нет';
    const opt=p && (p.options||[]).find(x=>String(x.value)===String(v));
    return opt?opt.label:(typeof v==='object'?JSON.stringify(v):String(v));
  };
  const params=Object.entries(o.params||{}).filter(([k,v])=>v!==''&&v!==null&&v!==undefined&&v!==false)
    .map(([k,v])=>{ const p=pdefs.get(k); return `<span class="tag">${esc(p?p.label:k)}: ${esc(pval(p,v))}</span>`; }).join(' ');
  const items=(job&&job.items)||[];
  // есть что повторить: сервер знает по отметкам задания, какие ящики не сделаны
  const retryBlock = o.retry ? `<div style="margin-top:10px"><button class="btn sm" data-retry title="Отметить ящики с ошибкой и не обработанные — и подставить то же действие с теми же параметрами; запуск — как обычно, через проверку">🔁 Повторить для неудачных (${fmtNum(o.retry)})…</button></div>`
    : (o.retry_problem ? `<div class="small muted" style="margin-top:8px">🔁 Повторить для неудачных нельзя: ${esc(o.retry_problem)}</div>` : '');
  const jobBlock = job ? `<div class="card" style="margin:12px 0">
      <div class="section-title" style="margin:0 0 6px"><b>Задание №${job.id}</b> <span class="badge ${esc(job.status)}">${esc(STATUS_LBL[job.status]||job.status)}</span><div class="spacer"></div>
        <button class="btn sm ghost" data-jd>Журнал задания</button></div>
      ${job.summary?`<div class="small">${esc(job.summary)}</div>`:''}
      ${job.passwords?`<div class="hint warnbox" style="margin-top:8px">🔑 Новые пароли (${fmtNum(job.passwords)}) ждут выдачи. Файл скачивается <b>один раз</b> и не позже чем через сутки — сохраните его и передайте сотрудникам. <button class="btn sm primary" data-pw style="margin-left:6px">Скачать пароли</button></div>`:''}
      ${job.report?'<div style="margin-top:8px"><button class="btn sm" data-rep>📊 Отчёт в Excel</button></div>':''}
      ${['queued','running'].includes(job.status)?'<button class="btn danger sm" data-cancel style="margin-top:8px">⏹️ Отменить задание</button>':''}
      ${items.length?`<div class="table-wrap" style="max-height:320px;overflow:auto;margin-top:10px"><table class="tbl srv-items"><thead><tr><th>Ящик</th><th>Итог</th><th>Подробности</th></tr></thead><tbody>
        ${items.map(it=>{ const [cls,lbl]=SRV_IST[it.status]||['',it.status]; return `<tr><td class="small"><b>${esc(it.name)}</b></td><td><span class="tag ${cls}">${esc(lbl)}</span></td><td class="small muted">${esc(it.detail||'')}</td></tr>`; }).join('')}
      </tbody></table></div>`:'<div class="small muted" style="margin-top:6px">Итог по ящикам появится, когда задание их обработает.</div>'}
    </div>` : '';
  const m=modal(`№${o.id}: ${o.label}`, `
    <div class="kv"><div class="k">Когда и кто</div><div>${esc(fmtDate(o.created_at))} · ${esc(o.user)}</div></div>
    ${params?`<div class="kv"><div class="k">Параметры</div><div>${params}</div></div>`:''}
    <div class="kv"><div class="k">Итог запуска</div><div>${esc(o.summary)}</div></div>
    ${jobBlock}${retryBlock}
    <details style="margin-top:8px"><summary class="small muted">Проверка перед запуском (${fmtNum(o.total)})</summary><div data-res>${bulkResultHtml(r, false)}</div></details>`, {wide:true});
  bulkWireResults(m.body.querySelector('[data-res]'), r, false);
  const on=(sel, fn)=>{ const el=m.body.querySelector(sel); if(el) el.onclick=fn; };
  on('[data-jd]', ()=>jobDetails(job.id));
  on('[data-rep]', ()=>apiDownload(`/server/history/${o.id}/report`, {}, `server_report_${o.id}.xlsx`).catch(toastErr));
  on('[data-pw]', async()=>{
    if(!await confirmDlg('Скачать пароли?', 'Файл можно скачать только один раз: после этого пароли стираются. Сохраните его в надёжном месте и передайте сотрудникам.', {okText:'Скачать', okClass:'primary'})) return;
    try{ await apiDownload(`/server/history/${o.id}/passwords`, {}, `passwords_${o.id}.xlsx`); toast('Пароли скачаны', 'Повторно их не выдать.'); m.close(); srvOpModal(opId, reload); }catch(e){ toastErr(e); }
  });
  on('[data-cancel]', async()=>{
    if(!await confirmDlg('Отменить задание?', 'Уже сделанное останется; остальные ящики обработаны не будут.', {okText:'Отменить задание'})) return;
    try{ await api(`/server/history/${o.id}/cancel`,{method:'POST'}); toast('Задание отменяется'); m.close(); if(reload) reload(); }catch(e){ toastErr(e); }
  });
  on('[data-retry]', async()=>{
    let r; try{ r=await api(`/server/history/${o.id}/retry`); }catch(e){ return toastErr(e); }
    if(!(r.keys||[]).length) return toast('Повторять нечего', 'Все ящики операции обработаны.', 'warn');
    m.close();
    if(r.action==='ax_create') return srvCreateModal({lines:r.lines, params:r.params});
    if(r.follow && (r.keys||[]).length){
      // часть учётных записей удалить не удалось, у части удаление прошло, а адрес не передан — два разных повтора
      const f=r.follow;
      const c=modal('Что повторить?', `<p style="margin-top:0">У этой операции два вида незаконченных ящиков:</p>
        <ul><li>«${esc(r.label)}» не выполнено — <b>${fmtNum(r.keys.length)}</b>;</li>
        <li>учётная запись удалена, а адрес коллеге не передан — <b>${fmtNum(f.keys.length)}</b>: для них — «${esc(f.label)}».</li></ul>
        <p class="muted small">Выберите одно; ко второму вернитесь через «Подробнее» этой операции.</p>`,
        {footer:`<button class="btn" data-a>${esc(r.label)} (${fmtNum(r.keys.length)})</button><button class="btn primary" data-b>${esc(f.label)} (${fmtNum(f.keys.length)})</button>`});
      c.foot.querySelector('[data-a]').onclick=()=>{ c.close(); srvRetry(r); };
      c.foot.querySelector('[data-b]').onclick=()=>{ c.close(); srvRetry({keys:f.keys, action:f.action, label:f.label, params:f.params}); };
      return;
    }
    srvRetry(r);
  });
}

/** Повторить операцию для неудачных: отметить их ящики и подставить действие с параметрами. */
function srvRetry(r){
  Srv.sel=new Set(r.keys); Srv.source='selected'; Srv.filter=''; Srv.query=''; Srv.shown=200;
  Srv.action=r.action; Srv.params[r.action]=Object.assign({}, Srv.params[r.action]||{}, r.params||{});
  // сколько ящиков на самом деле отмечено и не скрыты ли они отбором — скажет таблица (srvList)
  Srv.pending={label:r.label, total:(r.keys||[]).length, blank:(r.blank||[]).length, uncertain:(r.uncertain||[]).length};
  if(location.hash==='#/server?tab=list' || location.hash==='#/server') route(); else location.hash='#/server?tab=list';
}

// ---------- Создание учётных записей списком ----------
function srvCreateModal(pre){
  pre=pre||{}; const pp=pre.params||{};
  const ax=Srv.ax||{}, targets=[];
  if(ax.enabled && ax.allow_changes) targets.push({id:0, title:`Axigen${ax.host?' — '+ax.host:''}`, rule:'От 6 до 64 знаков, без пробелов, кавычек и «\\».', caps:['quota','must_change','imap_admin'], label:'Axigen'});
  (Srv.servers||[]).filter(x=>x.allow_changes && (x.caps||[]).includes('create'))
    .forEach(x=>targets.push({id:x.id, title:`${x.label} «${x.name}»`, rule:x.password_rule||'', caps:x.caps||[], label:x.label}));
  if(!targets.length) return toast('Создавать негде', 'Нет сервера, на котором разрешено создавать учётные записи.', 'warn');
  const first=targets.find(t=>t.id===(pp.server_id||0))||targets[0];
  const m=modal('Создать учётные записи на сервере', `
    <p class="muted" style="margin-top:0">Сначала — проверка: что будет с каждой строкой. Учётные записи создаются заданием, по одной; пароли — случайные (файлом для раздачи сотрудникам) или один общий временный.</p>
    <div class="form-row"${targets.length>1?'':' style="display:none"'}><label>Где создать</label><select id="scWhere">${targets.map(t=>`<option value="${t.id}"${t===first?' selected':''}>${esc(t.title)}</option>`).join('')}</select></div>
    <div class="form-row"><label>Откуда</label><select id="scSrc"><option value="text">Список адресов</option><option value="employees">Сотрудники с адресом, у которых нет учётной записи</option></select></div>
    <div class="form-row" data-text><label>Адреса</label><textarea id="scText" rows="6" placeholder="ivanov@company.ru; Иван; Иванов&#10;Петрова Мария <petrova@company.ru>">${esc(pre.lines||'')}</textarea>
      <div class="hint">По одной в строке: «адрес; имя; фамилия» или «Фамилия Имя &lt;адрес&gt;».</div></div>
    <div class="form-row"><label>Пароли</label><select id="scMode"><option value="random_file">Случайные — файлом для раздачи (скачать один раз)</option><option value="same">Один общий временный пароль</option></select></div>
    <div class="form-row" data-pw style="display:none"><label>Пароль</label><input type="text" id="scPw" autocomplete="off"><div class="hint" id="scRule"></div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="scMust" checked><span class="track"></span></span><label>Потребовать сменить пароль при первом входе</label></div>
    <div class="form-row" data-quota><label>Квота, МБ</label><input type="number" id="scQuota" min="0" value="0"><div class="hint">0 — как задано у домена (на сервере по умолчанию).</div></div>
    <div class="form-row check" data-arch><span class="switch"><input type="checkbox" id="scArch" checked><span class="track"></span></span><label id="scArchLbl">Сразу завести ящики в архиве</label></div>
    <div class="hint" data-caps style="display:none"></div>
    <div data-pv></div>`, {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn" data-check>Проверить</button><button class="btn primary" data-go disabled>Создать</button>'});
  const g=x=>m.body.querySelector(x), go=m.foot.querySelector('[data-go]');
  const where=()=>targets.find(t=>String(t.id)===g('#scWhere').value)||first;
  const sync=()=>{ g('[data-text]').style.display=g('#scSrc').value==='text'?'':'none'; g('[data-pw]').style.display=g('#scMode').value==='same'?'':'none';
    const t=where(), has=c=>t.caps.includes(c);
    g('#scRule').textContent=t.rule?('Требования сервера: '+t.rule+'.'):'';
    g('[data-quota]').style.display=has('quota')?'':'none';
    g('#scArchLbl').textContent=t.id?'Сразу завести ящики в архиве (копирование через подключение — без паролей сотрудников)':'Сразу завести ящики в архиве (копирование через FTP Backup)';
    g('[data-arch]').style.display=has('imap_admin')?'':'none';
    const miss=[!has('quota')&&'квоту', !has('must_change')&&'требование сменить пароль при входе'].filter(Boolean);
    const caps=g('[data-caps]'); caps.style.display=(t.id&&miss.length)?'':'none';
    caps.textContent=miss.length?`${t.label} через API не задаёт ${miss.join(' и ')} — эти настройки будут такими, как на сервере по умолчанию.`:'';
    go.disabled=true; };
  if(pre.lines){
    if(pp.password_mode) g('#scMode').value=pp.password_mode;
    if(pp.must_change!=null) g('#scMust').checked=!!pp.must_change;
    if(pp.quota_mb!=null) g('#scQuota').value=pp.quota_mb;
    if(pp.add_to_archive!=null) g('#scArch').checked=!!pp.add_to_archive;
  }
  g('#scSrc').onchange=sync; g('#scMode').onchange=sync; g('#scWhere').onchange=sync; sync();
  // поменяли список или параметры — прежняя проверка уже не о том: сначала «Проверить» заново
  m.body.addEventListener('input', ()=>{ if(!go.disabled){ go.disabled=true; go.textContent='Создать'; } });
  const body=preview=>({source:g('#scSrc').value, text:g('#scText').value, password_mode:g('#scMode').value, password:g('#scPw').value,
    must_change:g('#scMust').checked, quota_mb:where().caps.includes('quota')?(parseInt(g('#scQuota').value||'0',10)||0):0,
    add_to_archive:where().caps.includes('imap_admin')&&g('#scArch').checked, server_id:where().id, preview});
  const check=async()=>{
    try{ const r=await api('/server/create',{method:'POST',body:body(true)});
      const items=r.items||[];
      g('[data-pv]').innerHTML=`${(r.warnings||[]).map(w=>`<div class="hint warnbox">⚠️ ${esc(w)}</div>`).join('')}
        <div class="small" style="margin:8px 0">Будет создано: <b>${fmtNum(r.counts.ok)}</b>${r.counts.skip?`, пропущено: <b>${fmtNum(r.counts.skip)}</b>`:''}</div>
        <div class="table-wrap" style="max-height:300px;overflow:auto"><table class="tbl"><tbody>${items.map(i=>`<tr><td class="mono small">${esc(i.email)}</td><td><span class="tag ${i.status==='ok'?'ok':''}">${i.status==='ok'?'будет создана':'пропуск'}</span></td><td class="small">${esc(i.text)}</td></tr>`).join('')}</tbody></table></div>`;
      go.disabled=!r.counts.ok; go.textContent=r.counts.ok?`Создать ${fmtNum(r.counts.ok)}`:'Нечего создавать';
    }catch(e){ g('[data-pv]').innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; go.disabled=true; }
    // итог проверки — внизу длинного окна: показать его, а не оставлять за краем
    g('[data-pv]').scrollIntoView({behavior:'smooth', block:'nearest'});
  };
  m.foot.querySelector('[data-check]').onclick=check;
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  go.onclick=async()=>{ go.disabled=true; go.innerHTML='<span class="spinner"></span> Запуск…';
    try{ const r=await api('/server/create',{method:'POST',body:body(false)}); m.close(); toast('Создание запущено', r.summary, 'success', 8000);
      if(r.op_id) srvOpModal(r.op_id, null); }
    catch(e){ toastErr(e); go.disabled=false; go.textContent='Создать'; } };
}

// ---------- По расписанию — только по явному согласию ----------
const SRV_DOW=[[1,'понедельник'],[2,'вторник'],[3,'среда'],[4,'четверг'],[5,'пятница'],[6,'суббота'],[7,'воскресенье']];

function srvScheduleModal(a, keys, values){
  const n=keys.length;
  const m=modal(`🕑 По расписанию: ${a.label}`, `
    <p class="muted" style="margin-top:0">Сам MailArchiver почтовый сервер не меняет — расписание вы включаете сами, для этого действия и этих ящиков.
      При каждом запуске условия проверяются заново по каждому ящику (полный архив, свежая копия, сверка писем), итог — в «Истории» раздела.
      В расписании — именно эти ${fmtNum(n)} ${plural(n,'ящик','ящика','ящиков')}: новые ящики сами в него не попадут.</p>
    <div data-pv><div class="empty"><span class="spinner"></span> Проверяем ${fmtNum(n)} ${plural(n,'ящик','ящика','ящиков')}…</div></div>
    <div class="bulk-form boxed" style="margin-top:10px">
      <div class="form-row"><label>Как часто</label><select id="ssFreq"><option value="daily">Каждый день</option><option value="weekly">Каждую неделю</option><option value="monthly" selected>Каждый месяц</option></select></div>
      <div class="form-row" data-dow style="display:none"><label>День недели</label><select id="ssDow">${SRV_DOW.map(([v,t])=>`<option value="${v}" ${v===6?'selected':''}>${t}</option>`).join('')}</select></div>
      <div class="form-row" data-day><label>Число месяца</label><input type="number" id="ssDay" min="1" max="28" value="1"><div class="hint">От 1 до 28 — такое число есть в любом месяце.</div></div>
      <div class="form-row"><label>Время</label><input type="time" id="ssTime" value="03:00"><div class="hint">По часовому поясу расписаний («Настройки → Планировщик»). Лучше ночью, когда ящиками почти не пользуются.</div></div>
    </div>
    <div class="form-row check" style="margin-top:10px"><span class="switch"><input type="checkbox" id="ssConsent"><span class="track"></span></span>
      <label>Разрешаю выполнять «${esc(a.label)}» для ${fmtNum(n)} ${plural(n,'ящика','ящиков','ящиков')} по расписанию, без проверки перед каждым запуском. Кто и когда разрешил — запишется в журнал.</label></div>
    ${a.danger>=2?`<div class="form-row confirm-row"><label>Действие удаляет письма с сервера. Для подтверждения введите число ящиков в расписании: <b>${fmtNum(n)}</b></label><input type="text" id="ssConfirm" inputmode="numeric" autocomplete="off" placeholder="${n}"></div>`:''}
    <div data-sw></div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go disabled>Создать расписание</button>'});
  const g=x=>m.body.querySelector(x), go=m.foot.querySelector('[data-go]');
  const syncWhen=()=>{ const f=g('#ssFreq').value; g('[data-dow]').style.display=f==='weekly'?'':'none'; g('[data-day]').style.display=f==='monthly'?'':'none'; };
  const ready=()=>{ const c=g('#ssConfirm'); go.disabled=!(g('#ssConsent').checked && (!c || c.value.replace(/\s/g,'')===String(n))); };
  g('#ssFreq').onchange=syncWhen; syncWhen();
  m.body.addEventListener('input', ready); m.body.addEventListener('change', ready); ready();
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  (async()=>{
    try{
      const [pv, st]=await Promise.all([api('/server/ops',{method:'POST',body:{action:a.key, keys, params:values, preview:true}}), api('/server/schedules',{bg:true})]);
      g('[data-pv]').innerHTML=`<div class="small" style="margin-bottom:4px">Если бы запуск был сейчас:</div>${bulkResultHtml(pv, true)}
        ${(pv.counts&&pv.counts.ok)?'':'<div class="hint warnbox" style="margin-top:8px">⚠️ Сейчас ни один из ящиков не подходит — запуски будут их пропускать, пока не выполнятся условия (причины — выше).</div>'}`;
      bulkWireResults(g('[data-pv]'), pv, true);
      if(!st.scheduler_enabled) g('[data-sw]').innerHTML='<div class="hint warnbox">⚠️ Планировщик выключен («Настройки → Планировщик»): расписание сохранится, но запускаться не будет, пока его не включить.</div>';
    }catch(e){ g('[data-pv]').innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; }
  })();
  go.onclick=async()=>{
    const when={freq:g('#ssFreq').value, time:g('#ssTime').value||'03:00', dow:parseInt(g('#ssDow').value,10), day:parseInt(g('#ssDay').value||'1',10)};
    go.disabled=true; go.innerHTML='<span class="spinner"></span> Сохраняем…';
    try{
      const r=await api('/server/schedules',{method:'POST',body:{action:a.key, keys, params:values, when, consent:g('#ssConsent').checked, confirm:((g('#ssConfirm')||{}).value||'').replace(/\s/g,'')}});
      m.close(); toast('Расписание создано', r.summary, 'success', 8000);
      location.hash='#/server?tab=schedules';
    }catch(e){ toastErr(e); go.textContent='Создать расписание'; ready(); }
  };
}

async function srvSchedules(box, meta){
  box.appendChild(h(`<div class="card"><div class="section-title" style="margin-bottom:8px"><h3 style="margin:0">Действия по расписанию</h3><div class="spacer"></div><button class="btn sm ghost" data-r>↻ Обновить</button></div>
    <p class="muted small" style="margin-top:0">Создаются только вами: на вкладке «Ящики и действия» выберите ящики и действие (очистить корзину или спам, удалить или перенести старые письма, отчёт) и нажмите «🕑 По расписанию…».
      Каждый запуск — обычное задание: условия проверяются заново, итог — в «Истории» (кто — «расписание №…»).</p>
    <div data-sw></div><div data-list><div class="empty"><span class="spinner"></span></div></div></div>`));
  const list=box.querySelector('[data-list]');
  const pdefs=k=>new Map((((meta&&meta.actions)||[]).find(a=>a.key===k)||{params:[]}).params.map(p=>[p.key,p]));
  const pval=(p,v)=>{ if(p&&p.kind==='bool') return v?'да':'нет'; const opt=p&&(p.options||[]).find(x=>String(x.value)===String(v)); return opt?opt.label:(typeof v==='object'?JSON.stringify(v):String(v)); };
  const load=async()=>{
    let d; try{ d=await api('/server/schedules',{bg:true}); }catch(e){ list.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
    box.querySelector('[data-sw]').innerHTML=d.scheduler_enabled?'':'<div class="hint warnbox" style="margin-bottom:10px">⚠️ Планировщик выключен («Настройки → Планировщик») — расписания не запускаются.</div>';
    const items=d.items||[];
    if(!items.length){ list.innerHTML='<div class="empty">Расписаний нет. Без вашего согласия MailArchiver ничего на сервере сам не делает.</div>'; return; }
    list.innerHTML=`<div class="table-wrap"><table class="tbl"><thead><tr><th>№</th><th>Действие</th><th>Когда</th><th>Ящики</th><th class="nowrap">Согласие</th><th class="nowrap">Запуски</th><th>Состояние</th><th></th></tr></thead><tbody>${items.map(x=>{
      const pd=pdefs(x.action);
      const params=Object.entries(x.params||{}).filter(([k,v])=>v!==''&&v!==null&&v!==undefined&&v!==false).map(([k,v])=>{ const p=pd.get(k); return `<span class="tag">${esc(p?p.label:k)}: ${esc(pval(p,v))}</span>`; }).join(' ');
      return `<tr>
        <td class="muted nowrap">${x.id}</td>
        <td><b>${esc(x.label)}</b>${params?`<div style="margin-top:3px">${params}</div>`:''}</td>
        <td class="small">${esc(x.when_text)}</td>
        <td class="small"><b>${fmtNum(x.count)}</b><div class="muted" title="${esc(x.emails.join(', '))}">${esc(x.emails.slice(0,3).join(', '))}${x.count>3?' …':''}</div></td>
        <td class="small">${esc(x.consent_by||'—')}<div class="muted nowrap">${x.consent_at?esc(fmtDate(x.consent_at)):''}</div></td>
        <td class="small">${x.last_run?'был: '+esc(fmtDate(x.last_run)):'ещё не было'}<div class="muted">${x.enabled&&x.next_run?'следующий: '+esc(fmtDate(x.next_run)):''}</div></td>
        <td>${x.enabled?'<span class="tag ok">включено</span>':'<span class="tag">выключено</span>'}</td>
        <td style="text-align:right" class="nowrap"><button class="btn sm" data-en="${x.id}" data-on="${x.enabled?0:1}">${x.enabled?'⏸ Выключить':'▶ Включить'}</button> <button class="btn sm" data-sel="${x.id}" title="Отметить ящики расписания на вкладке «Ящики и действия»">Ящики</button> <button class="btn sm danger" data-del="${x.id}">Удалить</button></td></tr>`; }).join('')}</tbody></table></div>`;
    list.querySelectorAll('[data-en]').forEach(b=>b.onclick=async()=>{
      const on=b.dataset.on==='1'; b.disabled=true;
      try{ await api(`/server/schedules/${b.dataset.en}`,{method:'PUT',body:{enabled:on}}); toast(on?'Расписание включено':'Расписание выключено'); load(); }
      catch(e){ toastErr(e); b.disabled=false; }
    });
    list.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
      if(!await confirmDlg(`Удалить расписание №${b.dataset.del}?`, 'Больше запусков не будет; история прошлых запусков останется.', {okText:'Удалить'})) return;
      try{ await api(`/server/schedules/${b.dataset.del}`,{method:'DELETE'}); toast('Расписание удалено'); load(); }catch(e){ toastErr(e); }
    });
    list.querySelectorAll('[data-sel]').forEach(b=>b.onclick=()=>{
      const x=items.find(i=>String(i.id)===b.dataset.sel); if(!x) return;
      srvRetry({keys:x.keys, action:x.action, label:x.label, params:x.params});
    });
  };
  box.querySelector('[data-r]').onclick=load;
  load();
}

// ===================================================================
// Почтовые серверы: подключения к Microsoft 365, Google Workspace, Яндекс 360, Zimbra, Kerio…
// Подключение читает учётные записи сервера (сверка), следит за ним, а если
// разрешить изменения — через него выполняются действия «Ящиков на сервере».
const MS = { types:null };
const MS_KIND = {shared:'общий ящик', resource:'ресурс', room:'переговорная', equipment:'оборудование', admin:'администратор',
  unlicensed:'без лицензии', alias:'псевдоним', group:'группа', robot:'служебная', system:'системная', disabled:'отключена'};
/** Порядок возможностей в карточках: сначала то, что меняет учётные записи. */
const MS_CAPS = ['block','password','must_change','quota','aliases','contact','create','delete','autoreply','forward','usage','last_login','imap_admin'];
function msCapsHtml(caps, labels){
  return MS_CAPS.filter(k=>(caps||[]).includes(k)).map(k=>`<span class="tag">${esc((labels||{})[k]||k)}</span>`).join(' ');
}
const MS_LOG_KIND = {check:'проверка', monitor:'слежение', sync:'сверка', read:'чтение', write:'изменение'};

async function msTypes(){ if(!MS.types) MS.types=(await api('/mailservers/types')).types||[]; return MS.types; }

function msStateTag(x){
  if(!x.enabled) return '<span class="tag">выключено</span>';
  if(!x.last_check_at) return '<span class="tag">не проверялось</span>';
  const p=((x.status||{}).problems||[]).find(p=>p[0]==='unreachable');
  return x.last_check_ok ? `<span class="tag ok" title="Проверено ${esc(fmtDate(x.last_check_at))}">● доступен</span>`
    : `<span class="tag err" title="${esc((p&&p[2])||'')}">● недоступен</span>`;
}

async function viewMailservers(c){
  const [list, types]=await Promise.all([api('/mailservers'), msTypes()]);
  c.innerHTML='';
  const items=list.items||[];
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Почтовые серверы</h2>
    <span class="tag">${fmtNum(items.length)} ${plural(items.length,'подключение','подключения','подключений')}</span>
    <div class="spacer"></div>
    <a class="btn sm ghost" href="#/server" title="Учётные записи всех серверов и действия с ними">🖥️ Ящики на сервере</a>
    <button class="btn sm primary" id="msAdd">➕ Подключить сервер</button></div>`));
  c.appendChild(h(`<p class="muted" style="margin-top:-4px">Через подключение к API почтового сервера MailArchiver сверяет учётные записи с архивом, следит за сервером и — если разрешить изменения — выполняет действия раздела «Ящики на сервере»: закрыть вход, пароли, ФИО, псевдонимы, квоты, автоответ, пересылку, удаление при полном архиве. Серверов можно подключить несколько (например, Axigen и Microsoft 365 на время переезда).</p>`));
  $('#msAdd',c).onclick=()=>msTypePicker(types);
  if(!items.length){
    const box=h(`<div class="card"><div class="section-title"><h3>Какой сервер подключить?</h3></div><div class="ms-types"></div>
      <p class="small muted" style="margin:12px 0 0">Сервер Axigen настраивается отдельно — «Настройки → Сервер Axigen».</p></div>`);
    msTypeTiles(box.querySelector('.ms-types'), types, t=>msFormModal(null, t));
    c.appendChild(box);
    return;
  }
  const grid=h('<div class="grid cols-2"></div>'); c.appendChild(grid);
  items.forEach(x=>grid.appendChild(msCard(x, types)));
}

function msTypeTiles(box, types, pick){
  box.innerHTML=`<div class="bulk-tiles">${types.map(t=>`<button class="bulk-tile" data-t="${esc(t.type)}" title="${esc(t.summary)}" style="min-height:76px;align-items:flex-start;text-align:left">
    <span class="ic">${t.cloud?'☁️':'🖥️'}</span><span><b>${esc(t.label)}</b><div class="small muted" style="font-weight:400;margin-top:2px">${esc(t.summary)}</div></span></button>`).join('')}</div>`;
  box.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>pick(types.find(t=>t.type===b.dataset.t)));
}

function msTypePicker(types){
  const m=modal('Подключить почтовый сервер', '<div class="ms-types"></div>', {wide:true});
  msTypeTiles(m.body.querySelector('.ms-types'), types, t=>{ m.close(); msFormModal(null, t); });
}

function msCard(x, types){
  const t=types.find(t=>t.type===x.type)||{caps_labels:{}, limits:[]};
  const st=x.status||{}, acc=x.accounts||{};
  const caps=msCapsHtml(x.caps, t.caps_labels);
  const probs=(x.problems||[]).map(p=>`<li${p.level==='error'?' style="color:var(--danger)"':''}>${esc(p.text.replace(/^«[^»]*»: /,''))}</li>`).join('');
  const sync=x.last_sync_at?`${esc(fmtDate(x.last_sync_at))} — <span class="badge ${x.last_sync_status==='success'?'success':'partial'}">${x.last_sync_status==='success'?'успешно':'не всё прочитано'}</span><div class="small muted">${esc(x.last_sync_summary||'')}</div>`:'не было';
  const card=h(`<div class="card${(x.problems||[]).some(p=>p.level==='error')?' ms-bad':''}">
    <div class="section-title"><h3 style="margin:0">${t.cloud?'☁️':'🖥️'} ${esc(x.name)}</h3><span class="tag">${esc(x.label)}</span> ${msStateTag(x)}
      ${st.version?`<span class="tag" title="Версия">${esc(st.version)}</span>`:''}</div>
    ${probs?`<div class="hint warnbox" style="margin:8px 0"><ul style="margin:0;padding-left:18px">${probs}</ul></div>`:''}
    <div class="kv"><span class="k">Учётных записей</span><span>${fmtNum(acc.total||0)}${acc.blocked?` · вход закрыт: ${fmtNum(acc.blocked)}`:''}${acc.deleted?` · удалены на сервере: ${fmtNum(acc.deleted)}`:''}</span></div>
    <div class="kv"><span class="k">Копируются через подключение</span><span>${fmtNum(x.mailboxes||0)} ${plural(x.mailboxes||0,'ящик','ящика','ящиков')}</span></div>
    ${st.seats_total!=null?`<div class="kv"><span class="k">Лицензии</span><span>занято ${fmtNum(st.seats_used||0)} из ${fmtNum(st.seats_total)}</span></div>`:''}
    <div class="kv"><span class="k">Последняя сверка</span><span>${sync}</span></div>
    <div class="kv"><span class="k">По расписанию</span><span>${x.sync_enabled?`<span class="mono">${esc(x.sync_cron)}</span> (${esc(cronHuman(x.sync_cron))})${x.sync_add_new?' · новые — сразу в архив':''}`:'нет'}</span></div>
    <div class="kv"><span class="k">Изменения на сервере</span><span>${x.allow_changes?'<span class="tag warn">разрешены</span>':'<span class="tag">только чтение</span>'} · слежение ${x.monitor?'каждые 15 минут':'выключено'}</span></div>
    ${(st.details||[]).length?`<details style="margin-top:6px"><summary class="small muted">Что видно на сервере</summary><div class="small">${st.details.map(d=>esc(d)).join('<br>')}</div></details>`:''}
    <div class="small" style="margin-top:8px">Умеет: ${caps||'<span class="muted">только сверка</span>'}</div>
    ${x.secret_broken?'<div class="hint warnbox" style="margin-top:8px">Секреты подключения не расшифровываются (сменился secret.key) — откройте «Изменить» и введите их заново.</div>':''}
    <div class="btn-row" style="margin-top:12px;flex-wrap:wrap">
      <button class="btn sm" data-test>🔌 Проверить</button>
      <button class="btn sm primary" data-sync ${x.sync_running||!x.enabled?'disabled':''}>${x.sync_running?'<span class="spinner"></span> Сверка идёт…':'🔁 Сверить'}</button>
      <a class="btn sm" href="#/server?tab=list&srv=${x.id}">🖥️ Учётные записи</a>
      <button class="btn sm" data-edit>✏️ Изменить</button>
      <button class="btn sm ghost" data-log>📜 Журнал</button>
      <button class="btn sm ghost" data-del title="Удалить подключение (учётные записи сервера не трогаются)">🗑️</button></div></div>`);
  const on=(sel, fn)=>{ const el=card.querySelector(sel); if(el) el.onclick=fn; };
  on('[data-test]', ()=>msTest(x.id, x.name));
  on('[data-sync]', async(ev)=>{ const b=ev.currentTarget; b.disabled=true;
    try{ const r=await api(`/mailservers/${x.id}/sync`,{method:'POST'}); toast('Сверка запущена', `Задание №${r.job_id}`); msWatchJob(r.job_id); }
    catch(e){ toastErr(e); b.disabled=false; } });
  on('[data-edit]', ()=>msFormModal(x, t));
  on('[data-log]', ()=>msLogModal(x));
  on('[data-del]', async()=>{
    if(!await confirmDlg(`Удалить подключение «${x.name}»?`, 'Учётные записи на сервере не изменятся, архив писем останется. Сведения последней сверки и журнал подключения удалятся.', {okText:'Удалить'})) return;
    try{ await api(`/mailservers/${x.id}`,{method:'DELETE'}); toast('Подключение удалено'); refreshFeatures(); route(); }catch(e){ toastErr(e); }
  });
  return card;
}

/** Меню показывает «через подключение» в карточке ящика, только если подключения есть — обновить после изменения. */
async function refreshFeatures(){ try{ const me=await api('/me',{bg:true}); if(State.user) State.user.features=me.features||State.user.features; }catch(e){} }

function msWatchJob(jobId){
  let n=0;
  const tick=async()=>{
    n++;
    try{ const j=await api(`/jobs/${jobId}`,{bg:true});
      if(['success','partial','failed','cancelled'].includes(j.status)){
        toast(j.status==='failed'?'Сверка не удалась':'Сверка закончена', (j.result&&j.result.summary)||j.error||'', j.status==='failed'?'error':(j.status==='partial'?'warn':'success'), 9000);
        if(['mailservers','server'].includes(State.view)) route(); return; }
    }catch(e){ return; }
    if(n<400) setTimeout(tick, 3000);
  };
  setTimeout(tick, 2000);
}

function msFieldHtml(f, conn){
  const set=(conn&&conn.settings)||{}, isSet=!!(conn&&(conn.secrets_set||[]).includes(f.key));
  const v=set[f.key]!==undefined&&set[f.key]!==null?set[f.key]:(f.default!==null&&f.default!==undefined?f.default:'');
  const help=f.help?`<div class="hint">${esc(f.help)}</div>`:'';
  const ph=f.secret?(isSet?'задан — оставьте пустым, чтобы не менять':(f.placeholder||'')):(f.placeholder||'');
  const req=f.required?' <span class="muted" title="Обязательное поле">*</span>':'';
  const attrs=`data-f="${esc(f.key)}" placeholder="${esc(ph)}"`;
  let input;
  switch(f.kind){
    case 'bool': return `<div class="form-row check" data-row="${esc(f.key)}"><span class="switch"><input type="checkbox" data-f="${esc(f.key)}" ${v?'checked':''}><span class="track"></span></span><label>${esc(f.label)}</label>${help}</div>`;
    case 'select': input=`<select data-f="${esc(f.key)}">${(f.options||[]).map(o=>`<option value="${esc(o.value)}"${String(o.value)===String(v)?' selected':''}>${esc(o.label)}</option>`).join('')}</select>`; break;
    case 'int': input=`<input type="number" ${attrs} value="${esc(v)}">`; break;
    case 'password': input=`<input type="password" autocomplete="new-password" ${attrs}>`; break;
    case 'secret_text': input=`<textarea rows="4" class="mono" autocomplete="off" ${attrs}></textarea>`; break;
    case 'textarea': input=`<textarea rows="4" class="mono" ${attrs}>${esc(v)}</textarea>`; break;
    default: input=`<input type="text" ${attrs} value="${esc(v)}">`;
  }
  return `<div class="form-row" data-row="${esc(f.key)}"><label>${esc(f.label)}${req}${f.secret?' 🔒':''}</label>${input}${help}</div>`;
}

function msFormModal(conn, t){
  const isNew=!conn;
  const basic=t.fields.filter(f=>!f.advanced), adv=t.fields.filter(f=>f.advanced);
  const cron=(conn&&conn.sync_cron)||'50 4 * * *';
  const steps=(t.setup||[]).map(s=>`<li>${esc(s)}</li>`).join(''), limits=(t.limits||[]).map(s=>`<li>${esc(s)}</li>`).join('');
  const caps=msCapsHtml(t.caps, t.caps_labels);
  const m=modal(isNew?`Подключить: ${t.label}`:`Подключение «${conn.name}»`, `
    <p class="muted" style="margin-top:0">${esc(t.summary)}</p>
    ${steps?`<details ${isNew?'open':''} style="margin-bottom:10px"><summary><b>Что настроить на стороне сервера</b></summary><ol class="small" style="margin:6px 0 0;padding-left:20px">${steps}</ol></details>`:''}
    <div class="small" style="margin-bottom:6px">Умеет: ${caps||'только сверка'}</div>
    ${limits?`<details style="margin-bottom:10px"><summary class="small">Чего этот сервер через API не позволяет</summary><ul class="small" style="margin:6px 0 0;padding-left:20px">${limits}</ul></details>`:''}
    <div class="form-row"><label>Название подключения</label><input type="text" data-n="name" value="${esc((conn&&conn.name)||t.label)}" maxlength="100"></div>
    ${basic.map(f=>msFieldHtml(f, conn)).join('')}
    ${adv.length?`<details style="margin:6px 0 10px"><summary class="small">Дополнительно</summary>${adv.map(f=>msFieldHtml(f, conn)).join('')}</details>`:''}
    <div class="bulk-form boxed">
      <div class="form-row check"><span class="switch"><input type="checkbox" data-n="enabled" ${!conn||conn.enabled?'checked':''}><span class="track"></span></span><label>Подключение включено</label></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" data-n="allow_changes" ${conn&&conn.allow_changes?'checked':''}><span class="track"></span></span><label>Разрешить изменения на сервере</label>
        <div class="hint">Без этого — только чтение: сверка, отчёты, слежение. С ним в «Ящиках на сервере» доступны действия, которые меняют учётные записи (закрыть вход, пароли, удаление при полном архиве…). Каждое действие сначала показывает проверку.</div></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" data-n="sync_enabled" ${!conn||conn.sync_enabled?'checked':''}><span class="track"></span></span><label>Сверять учётные записи по расписанию</label></div>
      <div class="form-row" data-cron><label>Когда (cron)</label><input type="text" data-n="sync_cron" class="mono" value="${esc(cron)}"><div class="hint" data-cronh>${esc(cronHuman(cron))}</div></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" data-n="sync_add_new" ${conn&&conn.sync_add_new?'checked':''}><span class="track"></span></span><label>Новые учётные записи — сразу заводить ящики в архиве</label>
        <div class="hint">${(t.caps||[]).includes('imap_admin')?'Ящик копируется через подключение — без пароля сотрудника; настройки (срок хранения, папки) — из шаблона ящиков сотрудников.':'Этот сервер не пускает в ящики без паролей — ящики заводятся вручную, с паролем.'}</div></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" data-n="monitor" ${!conn||conn.monitor?'checked':''}><span class="track"></span></span><label>Следить за сервером (каждые 15 минут) и писать администраторам о проблемах</label></div>
    </div>`, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn" data-save>Сохранить</button><button class="btn primary" data-savetest>Сохранить и проверить</button>`});
  const q=sel=>m.body.querySelector(sel);
  const fval=f=>{ const el=q(`[data-f="${f.key}"]`); if(!el) return undefined; return f.kind==='bool'?el.checked:el.value; };
  const showIf=()=>{
    t.fields.forEach(f=>{ if(!f.show_if) return; const row=q(`[data-row="${f.key}"]`); if(!row) return;
      const ok=Object.entries(f.show_if).every(([k,vals])=>{ const dep=t.fields.find(x=>x.key===k); const v=dep?fval(dep):undefined; return vals.map(String).includes(String(v)); });
      row.style.display=ok?'':'none'; });
    q('[data-cron]').style.display=q('[data-n=sync_enabled]').checked?'':'none';
  };
  m.body.addEventListener('change', showIf); showIf();
  q('[data-n=sync_cron]').oninput=e=>{ q('[data-cronh]').textContent=cronHuman(e.target.value); };
  const collect=()=>{
    const values={name:q('[data-n=name]').value};
    if(isNew) values.type=t.type;
    ['enabled','allow_changes','sync_enabled','sync_add_new','monitor'].forEach(k=>values[k]=q(`[data-n=${k}]`).checked);
    values.sync_cron=q('[data-n=sync_cron]').value;
    t.fields.forEach(f=>{ const v=fval(f); if(v===undefined) return; if(f.secret && v==='') return; values[f.key]=f.kind==='int'&&v!==''?parseInt(v,10):v; });
    return values;
  };
  const save=async(test)=>{
    const btns=m.foot.querySelectorAll('button'); btns.forEach(b=>b.disabled=true);
    try{
      const r=isNew?await api('/mailservers',{method:'POST',body:{values:collect()}}):await api(`/mailservers/${conn.id}`,{method:'PUT',body:{values:collect()}});
      (r.warnings||[]).forEach(w=>toast('Обратите внимание', w, 'warn', 9000));
      toast('Сохранено'); m.close(); await refreshFeatures();
      if(test) await msTest(r.id, q('[data-n=name]').value); else route();
    }catch(e){ toastErr(e); btns.forEach(b=>b.disabled=false); }
  };
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  m.foot.querySelector('[data-save]').onclick=()=>save(false);
  m.foot.querySelector('[data-savetest]').onclick=()=>save(true);
}

async function msTest(id, name){
  const m=modal(`Проверка подключения «${name||''}»`, '<div class="empty"><span class="spinner"></span> Обращаемся к серверу…</div>', {wide:true, onClose:()=>{ if(State.view==='mailservers') route(); }});
  let r; try{ r=await api(`/mailservers/${id}/test`,{method:'POST'}); }catch(e){ m.body.innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}</div>`; return; }
  const st=r.status||{};
  const probs=(st.problems||[]).filter(p=>p[0]!=='unreachable').map(p=>`<li${p[1]==='error'?' style="color:var(--danger)"':''}>${esc(p[2])}</li>`).join('');
  const fp=r.fingerprint||'';
  m.body.innerHTML=`${r.ok?'<div class="hint okbox" style="margin-bottom:10px">✅ Подключение работает.</div>':`<div class="hint warnbox" style="margin-bottom:10px">❌ ${esc(r.error||'Сервер сообщил о проблемах.')}${r.hint?'<br><span class="small">'+esc(r.hint)+'</span>':''}</div>`}
    ${st.version?`<div class="kv"><span class="k">Версия</span><span>${esc(st.version)}</span></div>`:''}
    ${st.seats_total!=null?`<div class="kv"><span class="k">Лицензии</span><span>занято ${fmtNum(st.seats_used||0)} из ${fmtNum(st.seats_total)}</span></div>`:''}
    ${(st.details||[]).length?`<div class="small" style="margin:8px 0">${st.details.map(d=>esc(d)).join('<br>')}</div>`:''}
    ${probs?`<h4 style="margin:12px 0 4px">Требует внимания</h4><ul style="margin:0;padding-left:18px">${probs}</ul>`:''}
    ${fp&&!r.ok?`<div class="card" style="margin-top:12px"><div class="small">Сервер прислал сертификат с отпечатком SHA-256:</div><div class="mono small" style="word-break:break-all;margin:4px 0">${esc(fp)}</div>
      <div class="small muted">Если сертификат самоподписанный, сверьте отпечаток с тем, что показывает сервер (например, <span class="mono">openssl x509 -noout -fingerprint -sha256</span>), и разрешите доверие только при совпадении.</div>
      <button class="btn sm" data-trust style="margin-top:8px">Доверять этому сертификату</button></div>`:''}
    <div class="small muted" style="margin-top:10px">Что именно запрашивалось и что ответил сервер — в «Журнале» подключения (без паролей и токенов).</div>`;
  const tb=m.body.querySelector('[data-trust]');
  if(tb) tb.onclick=async()=>{
    if(!await confirmDlg('Доверять сертификату?', `Соединение с сервером будет приниматься, только если отпечаток сертификата — ${fp}. Убедитесь, что это сертификат вашего сервера.`, {okText:'Доверять', okClass:'primary'})) return;
    try{ await api(`/mailservers/${id}/trust`,{method:'POST',body:{fingerprint:fp}}); toast('Сертификат принят'); m.close(); msTest(id, name); }catch(e){ toastErr(e); }
  };
}

async function msLogModal(x){
  let kind='people', last=0;
  const m=modal(`Журнал подключения «${x.name}»`, `
    <div class="an-toolbar" style="margin-bottom:8px"><select data-kind><option value="people">Действия людей и сверки</option><option value="">Всё, включая проверки и слежение</option></select></div>
    <div data-list><div class="empty"><span class="spinner"></span></div></div>`, {wide:true});
  const list=m.body.querySelector('[data-list]');
  const row=e=>`<tr data-id="${e.id}" style="cursor:pointer"><td class="small nowrap">${esc(fmtDate(e.ts))}</td><td class="small">${esc(e.username||'')}</td>
    <td><span class="tag">${esc(MS_LOG_KIND[e.kind]||e.kind)}</span></td><td class="small">${esc(e.summary||'')}</td>
    <td>${e.ok?'<span class="tag ok">успешно</span>':'<span class="tag err">ошибка</span>'}</td><td class="small muted">${fmtNum(e.calls)}</td></tr>`;
  const wire=()=>list.querySelectorAll('[data-id]').forEach(tr=>tr.onclick=async()=>{
    try{ const e=await api(`/mailservers/${x.id}/log/${tr.dataset.id}`);
      modal(`${fmtDate(e.ts)} — ${e.summary||''}`, `<div class="small muted" style="margin-bottom:6px">${esc(e.username||'')} · ${esc(MS_LOG_KIND[e.kind]||e.kind)} · обращений к API: ${fmtNum(e.calls)}</div><pre class="mono small" style="white-space:pre-wrap;max-height:60vh;overflow:auto">${esc(e.transcript||'')}</pre>`, {wide:true});
    }catch(err){ toastErr(err); } });
  const load=async(more)=>{
    let d; try{ d=await api(`/mailservers/${x.id}/log?kind=${encodeURIComponent(kind)}&limit=100${more?'&before='+last:''}`); }catch(e){ list.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
    const items=d.items||[];
    if(!more && !items.length){ list.innerHTML='<div class="empty">Записей нет.</div>'; return; }
    if(items.length) last=items[items.length-1].id;
    if(!more) list.innerHTML=`<div class="table-wrap" style="max-height:60vh;overflow:auto"><table class="tbl"><thead><tr><th>Когда</th><th>Кто</th><th>Что</th><th>Описание</th><th>Итог</th><th>Вызовов</th></tr></thead><tbody></tbody></table></div><div data-more></div>`;
    list.querySelector('tbody').insertAdjacentHTML('beforeend', items.map(row).join(''));
    list.querySelector('[data-more]').innerHTML=items.length===100?'<button class="btn sm" style="margin-top:8px">Показать ещё</button>':'';
    const mb=list.querySelector('[data-more] button'); if(mb) mb.onclick=()=>load(true);
    wire();
  };
  m.body.querySelector('[data-kind]').onchange=e=>{ kind=e.target.value; last=0; list.innerHTML='<div class="empty"><span class="spinner"></span></div>'; load(false); };
  load(false);
}
