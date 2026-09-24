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
const NAV = [
  {id:'dashboard', icon:'📊', title:'Дашборд', mb:true},
  {id:'analytics', icon:'📈', title:'Аналитика', admin:true},
  {id:'mailanalytics', icon:'🔎', title:'Аналитика писем', admin:true},
  {id:'mail', icon:'📧', title:'Почта', mb:true},
  {id:'accounts', icon:'📬', title:'Почтовые ящики'},
  {id:'employees', icon:'🧑‍💼', title:'Сотрудники', admin:true},
  {id:'jobs', icon:'⚙️', title:'Очередь и задания', mb:true},
  {id:'exports', icon:'📤', title:'Экспорт (PST)', mb:true},
  {id:'schedules', icon:'⏰', title:'Расписания'},
  {sep:true},
  {id:'logs', icon:'📋', title:'Логи'},
  {id:'settings', icon:'🔧', title:'Настройки'},
  {id:'users', icon:'👥', title:'Пользователи', admin:true},
  {id:'security', icon:'🔐', title:'Безопасность', admin:true},
  {id:'audit', icon:'🛡️', title:'Аудит', admin:true},
];
function isMailbox(){ return State.user && State.user.role === 'mailbox'; }
/** Сотрудник (вход по ящику) может отменять и повторять только свои задания:
 *  копирование по расписанию запускает не он, и снимать его ему нельзя. */
function canTouchJob(j){ return !isMailbox() || (j && j.created_by === State.user.username); }

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
      <div class="sidebar-foot"><a href="#" id="profileLink" title="Профиль и двухфакторный вход">${State.user.totp_enabled?'🔐 ':''}${esc(State.user.username)}</a><a href="#" id="logout" title="Выход">Выход ⎋</a></div>
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
  const nav=$('#nav');
  NAV.forEach(item=>{
    if(item.sep){ if(!isMailbox()) nav.appendChild(h('<div class="sep"></div>')); return; }
    if(item.admin && State.user.role!=='admin') return;
    if(isMailbox() && !item.mb) return;
    const a=h(`<a href="#/${item.id}" data-view="${item.id}"><span class="ic">${item.icon}</span><span>${esc(item.title)}</span></a>`);
    a.onclick=(e)=>{ e.preventDefault(); location.hash='#/'+item.id; };
    nav.appendChild(a);
  });
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

function setActiveNav(view){
  $$('#nav a').forEach(a=>a.classList.toggle('active', a.dataset.view===view));
  const item=NAV.find(n=>n.id===view);
  $('#pageTitle').textContent=item?item.title:'';
}

function route(){
  let view=(location.hash.replace('#/','').split('?')[0]||(isMailbox()?'mail':'dashboard'));
  // mailbox-пользователю доступны только его разделы
  const allowed = NAV.filter(n=>!n.sep && (!n.admin||State.user.role==='admin') && (!isMailbox()||n.mb)).map(n=>n.id);
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
  const map={dashboard:viewDashboard,analytics:viewAnalytics,mailanalytics:viewMailAnalytics,mail:viewMail,accounts:viewAccounts,employees:viewEmployees,jobs:viewJobs,exports:viewExports,schedules:viewSchedules,logs:viewLogs,settings:viewSettings,users:viewUsers,security:viewSecurity,audit:viewAudit};
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
      ${State.user.role==='admin'?'<a class="btn small" href="#/settings" style="margin-top:10px">Открыть настройки</a>':''}</div>`));
  }
  // Копия вне сервера включена, но давно не обновлялась или завершилась ошибкой.
  if(s.replica_alert){
    c.appendChild(h(`<div class="card" style="margin-bottom:16px;border-color:var(--warn)">
      <div class="section-title"><h3>🛰️ Копия вне сервера</h3></div>
      <div>${esc(s.replica_alert)}</div>
      <a class="btn small" href="#/settings" style="margin-top:10px">Открыть настройки копии</a></div>`));
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
    <div class="card"><div class="section-title"><h3>📬 ${isMailbox()?'Мой ящик':'Ящики'}</h3>${isMailbox()?'':`<span class="tag">${s.totals.accounts}</span><div class="spacer"></div><button class="btn sm primary" id="addAcc">+ Добавить</button>`}</div><div id="accList"></div></div>
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
    <div class="kv"><b>Расписание копирования</b><div class="spacer"></div>${p.schedule_enabled?`<span class="tag ok">${esc(p.schedule_cron||'')}</span>`:'<span class="tag">не создаётся</span>'}</div>
    <div class="kv"><b>Заметка</b><div class="spacer"></div>${esc(p.notes||'—')}</div>
    <div class="hint" style="margin-top:12px">Пароль шаблон не задаёт: копирование начнётся только после того,
      как администратор впишет пароль в карточке ящика.</div>
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

/** Поставить в очередь копирование сразу всех включённых ящиков. */
async function backupAllNow(){
  const accs=State.accounts||[];
  const enabled=accs.filter(a=>a.enabled).length;
  const off=accs.length-enabled;
  if(!enabled) return toast('Нет включённых ящиков','Включите хотя бы один ящик, чтобы запустить копирование','warn');
  const ok=await confirmDlg('Сделать резервную копию всех ящиков?',
    `В очередь будет поставлено копирование для ${enabled} ${plural(enabled,'ящика','ящиков','ящиков')}.`
    + (off?` Выключенные ящики (${off}) пропускаются.`:'')
    + ' Ящики, по которым копирование уже идёт, повторно запущены не будут.',
    {okText:'Запустить', okClass:'primary'});
  if(!ok) return;
  try{
    const r=await api('/accounts/backup-all',{method:'POST'});
    const st=(r.started||[]).length, sk=(r.skipped||[]).length;
    if(st) toast('Резервное копирование запущено',
                 `Ящиков в очереди: ${st}` + (sk?`, пропущено (уже копируются): ${sk}`:''));
    else toast('Новых заданий нет','Все включённые ящики уже копируются','warn');
    location.hash='#/jobs';
  }catch(e){ toastErr(e); }
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
const Acc = { query:'', filter:'', shown:100 };
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

function accountMatches(a){
  const f=Acc.filter;
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
  const q=(Acc.query||'').trim().toLowerCase();
  if(!q) return true;
  return [a.name, a.username, a.host].some(v=>String(v||'').toLowerCase().includes(q));
}

/** Итог последней проверки входа (пароля) — для колонки «Вход». */
function loginCell(a){
  const when = a.login_checked_at ? ` title="Проверено ${esc(fmtDate(a.login_checked_at))}${a.login_error?': '+esc(a.login_error):''}"` : '';
  if(a.secret_broken || a.login_status==='secret_broken')
    return `<span class="tag err"${when}>пароль не читается</span>`;
  if(!a.has_password) return '<span class="tag warn">нет пароля</span>';
  switch(a.login_status){
    case 'ok': return `<span class="tag ok"${when}>✓ пароль верный</span>`;
    case 'auth_error': return `<span class="tag err"${when}>✗ неверный пароль</span>`;
    case 'conn_error': return `<span class="tag warn"${when}>нет связи</span>`;
    case 'no_password': return `<span class="tag warn"${when}>нет пароля</span>`;
    default: return '<span class="tag" title="Вход в ящик ещё не проверялся — нажмите «Проверить пароли»">не проверялся</span>';
  }
}

/** Даты резервных копий ящика — для колонки «Резервные копии». */
function backupCell(a){
  if(accNoBackup(a)){
    const lr=a.last_run;
    const tail = lr && lr.status==='failed' ? `<div class="small" style="color:var(--danger)">попытка ${esc(fmtDate(lr.finished_at||lr.started_at))} не удалась</div>` : '';
    return `<span class="tag warn">копий нет</span>${tail}`;
  }
  const st=a.last_backup_status||'';
  const stale = accStale(a) ? ` <span class="tag warn" title="Удачной копии не было больше ${ACC_STALE_HOURS} ч">давно</span>` : '';
  const lr=a.last_run;
  const failedLater = lr && lr.status==='failed' && (!a.last_backup_at || (lr.started_at||'')>a.last_backup_at)
    ? `<div class="small" style="color:var(--danger)" title="${esc(lr.detail||'')}">последняя попытка ${esc(fmtDate(lr.finished_at||lr.started_at))} — ошибка</div>` : '';
  return `<div class="small">последняя: <b>${esc(fmtDate(a.last_backup_at))}</b>${st==='partial'?' <span class="badge partial">частично</span>':''}${stale}</div>
    <div class="small muted">первая: ${esc(fmtDate(a.first_backup_at))} · писем ${fmtNum(a.messages)} (${esc(a.bytes_h||'')})</div>${failedLater}`;
}

async function viewAccounts(c){
  const accs=await api('/accounts'); State.accounts=accs;
  c.innerHTML='';
  const isAdmin=State.user&&State.user.role==='admin';
  const enabledCnt=accs.filter(a=>a.enabled).length;
  const disabledCnt=accs.length-enabledCnt;
  const noPwCnt=accs.filter(a=>!a.has_password).length;
  const head=h(`<div class="section-title"><h2 style="margin:0">Почтовые ящики</h2>
    <span class="tag" title="Всего ящиков в системе">всего: ${accs.length}${disabledCnt?` · выключено: ${disabledCnt}`:''}</span>
    <div class="spacer"></div>
    ${isAdmin?`<button class="btn" id="pwCheck" title="Войти в каждый ящик и сразу выйти — найти ящики с неверным паролем">🔐 Проверить пароли</button>
    <button class="btn" id="pwImport" title="Массово проставить пароли ящикам из файла «адрес — пароль»">🔑 Загрузить пароли</button>
    <button class="btn" id="bkAll" ${enabledCnt?'':'disabled'} title="Поставить в очередь резервное копирование всех включённых ящиков">💾 Копия всех ящиков сейчас</button>
    <button class="btn primary" id="add">+ Добавить ящик</button>`:''}</div>`);
  c.appendChild(head);
  if(isAdmin){
    $('#add',c).onclick=()=>accountModal();
    $('#bkAll',c).onclick=()=>backupAllNow();
    $('#pwImport',c).onclick=()=>passwordImportModal();
    $('#pwCheck',c).onclick=()=>checkLoginsModal(accs);
  }
  const progress=h('<div id="pwProgress" style="display:none" class="hint"></div>');
  c.appendChild(progress);
  if(State.checkLoginsJob) watchCheckLogins(State.checkLoginsJob);
  if(!accs.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">📭</div>Пока нет ни одного ящика.<br>Нажмите «Добавить ящик», чтобы начать.</div></div>')); return; }

  // Отбор: ящиков бывает несколько сотен (их заводит синхронизация сотрудников),
  // и найти среди них выключенные, оставшиеся без пароля или без копий глазами нереально.
  const cnt=(fn)=>accs.filter(fn).length;
  const opts=[['','Все ящики'+` (${accs.length})`],
              ['enabled','Только включённые'+` (${enabledCnt})`],
              ['disabled','Только выключенные'+` (${disabledCnt})`],
              ['ready','Готовые к копированию'+` (${cnt(a=>a.enabled&&a.has_password)})`],
              ['badpw','С неправильным паролем'+` (${cnt(accBadPassword)})`],
              ['nopassword','Без пароля'+` (${noPwCnt})`],
              ['connerr','Нет связи при проверке входа'+` (${cnt(a=>a.login_status==='conn_error')})`],
              ['unchecked','Пароль не проверялся'+` (${cnt(a=>!a.login_status&&a.has_password)})`],
              ['nobackup','Без резервных копий'+` (${cnt(accNoBackup)})`],
              ['stale',`Копия старше ${ACC_STALE_HOURS} ч`+` (${cnt(accStale)})`],
              ['failed','Последняя копия с ошибкой'+` (${cnt(accFailed)})`],
              ['dismissed','Уволенные сотрудники'+` (${cnt(a=>!!a.dismissed_at)})`],
              ['hold','Архив удерживается'+` (${cnt(a=>a.on_hold)})`],
              ['holdexpired','Удержание истекло'+` (${cnt(a=>a.hold_expired)})`]];
  const bar=h(`<div class="an-toolbar">
    <input type="text" id="accQ" placeholder="Поиск по названию, логину, серверу…" style="flex:0 1 320px;min-width:200px" value="${esc(Acc.query)}">
    <select id="accF">${opts.map(([v,t])=>`<option value="${v}" ${v===Acc.filter?'selected':''}>${esc(t)}</option>`).join('')}</select>
    <button class="btn small ghost" id="accReset" ${(Acc.query||Acc.filter)?'':'style="display:none"'}>Сбросить</button>
    <div class="spacer"></div><span class="muted small" id="accCount"></span></div>`);
  c.appendChild(bar);

  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Название и сервер</th><th>Логин</th><th>Вход</th><th>Резервные копии</th><th>Статус</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  const render=()=>{
    tb.innerHTML='';
    const shown=accs.filter(accountMatches);
    $('#accCount',bar).textContent=`показано ${Math.min(shown.length, Acc.shown)} из ${shown.length}`
      + (shown.length!==accs.length?` (всего ${accs.length})`:'');
    $('#accReset',bar).style.display=(Acc.query||Acc.filter)?'':'none';
    if(!shown.length){
      tb.appendChild(h('<tr><td colspan="6" class="empty">Под отбор не попал ни один ящик</td></tr>'));
      return;
    }
    const page=shown.slice(0, Acc.shown);
    page.forEach(a=>{
      // «Копируется» — только если копирование действительно может пройти:
      // включённый ящик без пароля или с отвергнутым паролем не копируется.
      const state = !a.enabled ? '<span class="tag">выключен</span>'
        : (!a.has_password || a.secret_broken) ? '<span class="tag warn" title="Ящик включён, но без пароля копирование не пойдёт">не копируется</span>'
        : a.login_status==='auth_error' ? '<span class="tag err" title="Сервер отверг пароль — исправьте его">не копируется</span>'
        : '<span class="tag ok">копируется</span>';
      const hold = holdTags(a);
      const tr=h(`<tr>
        <td><strong>${esc(a.name)}</strong><div class="small muted nowrap">${esc(a.host)}:${a.port} · ${esc(a.security)}${a.auth_type==='oauth2'?' · OAuth2':(a.auth_type==='master'?' · через администратора':'')}</div></td>
        <td class="small">${esc(a.username)}</td>
        <td>${loginCell(a)}</td>
        <td>${backupCell(a)} <a href="javascript:void(0)" class="small" data-runs>история</a></td>
        <td>${state}${hold?'<div style="margin-top:4px">'+hold+'</div>':''}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm" data-test>Проверить</button>
          <button class="btn sm primary" data-bk ${a.enabled?'':'disabled'} title="${a.enabled?'Сделать резервную копию этого ящика сейчас':'Ящик выключен — включите его, чтобы делать копии'}">💾 Копия сейчас</button>
          <button class="btn sm" data-menu>⋯</button>
        </td></tr>`);
      tr.querySelector('[data-test]').onclick=()=>testAccount(a.id);
      tr.querySelector('[data-bk]').onclick=()=>backupNow(a.id, a.name);
      tr.querySelector('[data-menu]').onclick=()=>accountMenu(a);
      tr.querySelector('[data-runs]').onclick=()=>runsModal(a);
      tb.appendChild(tr);
    });
    if(shown.length>page.length){
      const rest=shown.length-page.length;
      const more=h(`<tr><td colspan="6" style="text-align:center">
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
  c.appendChild(wrap);
  render();
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
async function quarantineModal(a){
  let d;
  try{ d=await api(`/accounts/${a.id}/quarantines`); }catch(e){ return toastErr(e); }
  const list=d.quarantines||[];
  const rows=list.map(q=>`<tr><td class="mono small">${esc(q.name)}</td>
    <td class="small">${fmtNum(q.files)}</td><td class="small">${esc(q.bytes_h)}</td>
    <td style="text-align:right"><button class="btn danger sm" data-del="${esc(q.path)}">Удалить</button></td></tr>`).join('');
  const m=modal(`Прежние копии: ${a.name}`, list.length ? `
    <p class="muted">При пересоздании копии «с нуля» прежние письма не удаляются, а переносятся
      в карантин — на случай, если что-то пойдёт не так. Когда новая копия проверена, карантин
      можно удалить и освободить место.</p>
    <div class="table-wrap"><table class="tbl">
      <thead><tr><th>Каталог</th><th>Файлов</th><th>Размер</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>`
    : '<div class="empty">Карантинных копий нет — на диске только актуальная копия.</div>',
    {wide:true});
  m.body.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
    const ok=await confirmDlg('Удалить карантинную копию?',
      'Каталог с прежними письмами будет удалён безвозвратно. Убедитесь, что новая копия в порядке.',
      {okText:'Удалить', okClass:'danger'});
    if(!ok) return;
    try{
      await api(`/accounts/${a.id}/quarantines/delete`,{method:'POST',body:{path:b.dataset.del}});
      toast('Карантинная копия удалена'); m.close(); quarantineModal(a);
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
    <label class="form-row check" style="align-items:flex-start;gap:10px;margin-top:10px">
      <input type="radio" name="rb" value="full" style="margin-top:4px">
      <span><b>Полностью с нуля</b><br>
        <span class="muted small">Стирает локальную копию ящика — и файлы писем, и записи индекса —
        и скачивает всё с сервера заново.</span></span></label>
    <div class="hint" style="margin-top:12px;border-left:3px solid var(--danger)">
      <b>Режим «с нуля» необратим.</b> Письма, которых уже нет на почтовом сервере, есть только
      в этой копии — после стирания их не вернуть. Выбирайте его, только если архив испорчен
      и нужен именно чистый лист.</div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Запустить</button>'});
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const mode=(m.body.querySelector('input[name=rb]:checked')||{}).value||'missing';
    if(mode==='full'){
      const ok=await confirmDlg('Стереть локальную копию и скачать заново?',
        `Все скачанные письма ящика «${a.name}» будут удалены с диска, а затем заново скачаны с сервера. `+
        `Письма, которых на сервере уже нет, будут потеряны безвозвратно. Продолжить?`,
        {okText:'Да, стереть и скачать заново', okClass:'danger'});
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
        <button class="btn small" id="diagExclude">🚫 Больше не копировать эти папки</button>
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
    <button class="btn" data-a="quarantine">🧺 Прежние копии (карантин)</button>
    <button class="btn" data-a="logout" title="Сотрудник, вошедший в интерфейс по паролю этого ящика, будет разлогинен">🚪 Завершить сеансы сотрудника</button>
    <button class="btn" data-a="hold" title="Пока действует удержание, письма ящика не удаляются по сроку хранения">🔒 Удержание архива${a.on_hold?' (до '+esc(holdDate(a.hold_until))+')':''}</button>
    <button class="btn danger" data-a="del">🗑️ Удалить ящик</button>
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
    else if(act==='purge') purgeModal(a);
    else if(act==='logout'){ try{ const r=await api(`/accounts/${a.id}/logout-sessions`,{method:'POST'}); toast('Готово', r.closed?`Завершено сеансов: ${r.closed}`:'Открытых сеансов не было'); }catch(e){toastErr(e);} }
    else if(act==='verify'){ try{await api(`/accounts/${a.id}/verify`,{method:'POST'}); toast('Запущено','Проверка целостности в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} }
    else if(act==='del'){ if(await confirmDlg('Удалить ящик?', `Ящик «${a.name}» и его настройки будут удалены. Локальные копии писем на диске останутся.${a.on_hold?' Архив ящика удерживается до '+holdDate(a.hold_until)+' — файлы писем не трогаются.':''} Продолжить?`)){ try{await api(`/accounts/${a.id}`,{method:'DELETE'}); toast('Удалено'); route();}catch(e){toastErr(e);} } }
  });
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
        <option value="master"${a.auth_type==='master'?' selected':''}>Через администратора почты (без пароля ящика)</option></select></div>
    </div>
    <div class="form-row"><label>Логин ${H('username')}</label><input id="f-user" type="text" value="${esc(a.username)}" placeholder="user@example.ru"></div>
    <div class="form-row" id="pwrow" style="display:${a.auth_type==='password'?'block':'none'}"><label>Пароль ${H('password')}</label><input id="f-pass" type="password" placeholder="${id?'оставьте пустым, чтобы не менять':'пароль или пароль приложения'}"></div>
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
  g('#f-auth').onchange=()=>{ const v=g('#f-auth').value; m.body.querySelector('#oauthBox').style.display=v==='oauth2'?'block':'none'; m.body.querySelector('#pwrow').style.display=v==='password'?'block':'none'; };
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
     oauth_refresh_token:g('#f-ort')?g('#f-ort').value:'',oauth_token_url:g('#f-otu')?g('#f-otu').value:''});
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
  let cur = a.retention_days;
  if(cur===undefined){ try{ cur=(await api(`/accounts/${a.id}`)).retention_days; }catch(e){ cur=-1; } }
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
      <input id="ret-custom" type="number" min="0" placeholder="напр. 30"></div>`,
    {footer:`<button class="btn ghost" data-c>Закрыть</button><button class="btn primary" data-save>Сохранить свой срок</button>`});
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
  const upd=()=>{ const e=engines.find(x=>x.name===g('#e-engine').value); g('#e-desc').innerHTML=e?esc(e.desc)+(e.experimental?' <strong style="color:var(--warn)">Экспериментально — проверьте результат в своём Outlook.</strong>':''):''; const isPst=e&&e.fmt==='pst'; g('#e-pst').closest('.form-row').style.display=isPst?'block':'none'; };
  g('#e-engine').onchange=upd; upd();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const e=engines.find(x=>x.name===g('#e-engine').value);
    const body={engine:g('#e-engine').value, format:e?e.fmt:'pst', pst_format:g('#e-pst').value,
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
function importModal(a){
  const m=modal(`Импорт .pst: ${a.name}`, `
    <p class="muted small">Файл .pst будет разобран и письма зальются на IMAP-сервер в папку с указанным префиксом. Требуется установленный на сервере <code>readpst</code> (пакет pst-utils).</p>
    <div class="form-row"><label>Файл .pst</label><input id="i-file" type="file" accept=".pst"></div>
    <div class="form-row"><label>Префикс папки назначения</label><input id="i-prefix" type="text" value="Импорт PST"></div>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Загрузить и импортировать</button>`});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const f=m.body.querySelector('#i-file').files[0]; if(!f) return toast('Выберите файл','','warn');
    const fd=new FormData(); fd.append('file', f); fd.append('target_prefix', m.body.querySelector('#i-prefix').value);
    const btn=m.foot.querySelector('[data-go]');
    btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Загрузка…';
    try{
      await api(`/accounts/${a.id}/import-pst`,{method:'POST',body:fd}); toast('Импорт запущен'); m.close(); location.hash='#/jobs';
    }catch(e){
      // Без возврата кнопки диалог после ошибки приходилось закрывать и
      // открывать заново — кнопка оставалась серой со спиннером.
      toastErr(e); btn.disabled=false; btn.textContent='Загрузить и импортировать';
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
      <td class="muted">${j.id}</td>
      <td>${esc(j.type_label)}</td>
      <td class="small">${esc(j.account_name||accName(j.account_id))||'—'}</td>
      <td><span class="badge ${j.status}">${esc(j.status_label)}</span></td>
      <td style="min-width:160px" data-prog>${prog}</td>
      <td class="small muted">${fmtDate(j.created_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm" data-info>Детали</button>
        ${(j.status==='running'||j.status==='queued') && canTouchJob(j)?`<button class="btn danger sm" data-cancel>Отмена</button>`:''}
        ${(j.status==='failed'||j.status==='cancelled'||j.status==='partial') && canTouchJob(j)?`<button class="btn sm" data-retry>Повторить</button>`:''}
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

async function jobDetails(id){
  const j=await api(`/jobs/${id}`); const ev=await api(`/jobs/${id}/events`);
  // Непрочитанные папки показываем отдельным блоком с кнопкой: так их можно
  // сразу перестать копировать, а не искать ящик и править список руками.
  const skipped=(j.result&&j.result.skipped_folders)||[];
  const resEntries=j.result?Object.entries(j.result).filter(([k])=>k!=='skipped_folders'):[];
  const res=resEntries.map(([k,v])=>`<div class="kv"><div class="k">${esc(k)}</div><div>${esc(typeof v==='object'?JSON.stringify(v):v)}</div></div>`).join('');
  const evs=ev.map(e=>`<div class="l-${e.level}">${fmtDate(e.ts)} [${e.level}] ${esc(e.message)}</div>`).join('')||'<span class="muted">нет событий</span>';
  const skipBlock = skipped.length ? `
    <div class="card" style="margin:14px 0;border-color:var(--warn)">
      <b>⚠️ Копия неполная: не удалось прочитать папки (${skipped.length})</b>
      <div class="mono small" style="margin:8px 0;word-break:break-all">${skipped.map(esc).join('<br>')}</div>
      <div class="muted small">Причина у каждой папки — в журнале событий ниже. Если папку на почтовом
        сервере не восстановить, добавьте её в «Пропускать папки» — тогда копия перестанет считаться
        неполной из-за неё.</div>
      ${(j.account_id && !isMailbox())?'<button class="btn small" id="jobExclude" style="margin-top:10px">🚫 Больше не копировать эти папки</button>':''}
    </div>` : '';
  const m=modal(`Задание #${j.id} — ${j.type_label}`, `
    <div class="kv"><div class="k">Статус</div><div><span class="badge ${j.status}">${esc(j.status_label)}</span></div></div>
    <div class="kv"><div class="k">Создано / завершено</div><div>${fmtDate(j.created_at)} → ${fmtDate(j.finished_at)}</div></div>
    <div class="kv"><div class="k">Попыток</div><div>${j.attempts}/${j.max_attempts}</div></div>
    ${j.error?`<div class="kv"><div class="k">Ошибка</div><div style="color:var(--danger)">${esc(j.error)}</div></div>`:''}
    ${res}
    ${skipBlock}
    <h3 style="margin-top:16px">Журнал событий</h3><div class="log-view">${evs}</div>`, {wide:true});
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
  c.innerHTML=''; c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Расписания</h2><div class="spacer"></div><button class="btn primary" id="add" ${accs.length?'':'disabled'}>+ Добавить расписание</button></div>`));
  $('#add',c).onclick=()=>scheduleModal(null, accs);
  if(!accs.length){ c.appendChild(h('<div class="card"><div class="empty">Сначала добавьте хотя бы один ящик.</div></div>')); return; }
  if(!sch.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">⏰</div>Нет расписаний. Добавьте, чтобы бэкап шёл автоматически.</div></div>')); return; }
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Ящик</th><th>Задание</th><th>Когда</th><th>Последний</th><th>Следующий</th><th>Вкл</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  sch.forEach(s=>{
    const when=s.kind==='cron'?`cron: <code>${esc(s.cron_expr)}</code>`:`каждые ${Math.round(s.interval_seconds/60)} мин`;
    const tr=h(`<tr>
      <td>${esc(accName(s.account_id))}</td>
      <td><span class="tag">${esc(JOBLBL[s.job_type]||s.job_type)}</span></td>
      <td class="small">${when}</td>
      <td class="small muted">${fmtDate(s.last_run)}</td>
      <td class="small muted">${fmtDate(s.next_run)}</td>
      <td><span class="badge ${s.enabled?'success':'queued'}">${s.enabled?'да':'нет'}</span></td>
      <td style="text-align:right;white-space:nowrap"><button class="btn sm" data-edit>✏️</button><button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-edit]').onclick=()=>scheduleModal(s, accs);
    tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить расписание?','')){ try{await api(`/schedules/${s.id}`,{method:'DELETE'});toast('Удалено');route();}catch(e){toastErr(e);} } };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}
function scheduleModal(s, accs){
  const H=(k)=>helpIcon(fieldHelp('schedule',k));
  s=s||{account_id:accs[0].id,kind:'cron',job_type:'backup',cron_expr:'0 3 * * *',interval_seconds:21600,enabled:true};
  const accOpts=accs.map(a=>`<option value="${a.id}" ${a.id===s.account_id?'selected':''}>${esc(a.name)}</option>`).join('');
  const body=`
    <div class="form-row"><label>Ящик</label><select id="s-acc">${accOpts}</select></div>
    <div class="grid cols-2">
      <div class="form-row"><label>Задание ${H('job_type')}</label><select id="s-job">
        <option value="backup"${s.job_type==='backup'?' selected':''}>Резервное копирование</option>
        <option value="retention"${s.job_type==='retention'?' selected':''}>Очистка (ретеншн)</option>
        <option value="verify"${s.job_type==='verify'?' selected':''}>Проверка целостности</option>
        ${s.job_type==='restore'?'<option value="restore" selected>Восстановление</option>':''}</select></div>
      <div class="form-row"><label>Тип ${H('kind')}</label><select id="s-kind">
        <option value="cron"${s.kind==='cron'?' selected':''}>По времени (cron)</option>
        <option value="interval"${s.kind==='interval'?' selected':''}>Через интервал</option></select></div>
    </div>
    <div class="form-row" id="s-cronrow" style="display:${s.kind==='interval'?'none':'block'}"><label>Cron-выражение ${H('cron_expr')}</label><input id="s-cron" type="text" value="${esc(s.cron_expr||'0 3 * * *')}">
      <div class="hint">Примеры: <code>0 3 * * *</code> — ежедневно в 03:00; <code>0 */6 * * *</code> — каждые 6 часов; <code>30 2 * * 1</code> — по понедельникам в 02:30.</div></div>
    <div class="form-row" id="s-introw" style="display:${s.kind==='interval'?'block':'none'}"><label>Интервал (минут) ${H('interval_seconds')}</label><input id="s-int" type="number" min="1" value="${Math.round((s.interval_seconds||21600)/60)}"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="s-en" ${s.enabled?'checked':''}><span class="track"></span></span><label>Расписание включено</label></div>`;
  const m=modal(s.id?'Редактирование расписания':'Новое расписание', body, {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=x=>m.body.querySelector(x);
  g('#s-kind').onchange=()=>{ g('#s-cronrow').style.display=g('#s-kind').value==='cron'?'block':'none'; g('#s-introw').style.display=g('#s-kind').value==='interval'?'block':'none'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=async()=>{
    // options не отправляем: при правке сервер сохраняет прежние параметры
    // задания (раньше каждое сохранение их обнуляло).
    const body={account_id:parseInt(g('#s-acc').value),job_type:g('#s-job').value,kind:g('#s-kind').value,
      cron_expr:g('#s-cron').value,interval_seconds:parseInt(g('#s-int').value||'60')*60,enabled:g('#s-en').checked};
    if(!s.id) body.options={};
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
  'export.default_engine':[['auto','Авто'],['aspose','Aspose PST'],['native','Встроенный PST (эксперим.)'],['mbox','MBOX'],['eml','EML']],
  'export.pst_format':[['unicode','Unicode (Outlook 2003+)'],['ansi','ANSI (Outlook 97–2002)']],
  'export.outlook_target':[['2016+','2016+'],['2013','2013'],['2010','2010'],['2007','2007'],['2003','2003'],['2002','2002 и старше'],['365','365']],
  'notifications.smtp_security':[['starttls','STARTTLS'],['ssl','SSL'],['none','Без шифрования']],
  'employees.source_type':[['file','Файл на сервере'],['url','Адрес (URL)']],
  'employees.source_url_format':[['auto','Определять автоматически'],['csv','CSV'],['xlsx','Excel (XLSX)']],
  'employees.account_security':[['ssl','SSL/TLS'],['starttls','STARTTLS'],['plain','Без шифрования']],
  'replica.target':[['dir','Сетевая папка или диск'],['rsync','Другой сервер по SSH (rsync)'],['s3','S3-хранилище']],
  'mailadmin.mode':[['sasl_plain','SASL PLAIN с authzid (стандарт)'],['separator','Логин с разделителем (ящик*админ)']],
  'employees.dismissed_action':[['final_backup_disable','Последняя копия и выключить копирование'],['keep','Продолжать копировать']],
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
  'mailadmin.host':      v=>!!v['mailadmin.enabled'],
  'mailadmin.user':      v=>!!v['mailadmin.enabled'],
  'mailadmin.password':  v=>!!v['mailadmin.enabled'],
  'mailadmin.mode':      v=>!!v['mailadmin.enabled'],
  'mailadmin.separator': v=>!!v['mailadmin.enabled'] && v['mailadmin.mode']==='separator',
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
      if(Object.keys(changed).some(k=>k.startsWith('storage.'))) route();
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
async function viewUsers(c){
  const users=await api('/users');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Пользователи</h2><div class="spacer"></div><button class="btn primary" id="add">+ Добавить</button></div>'));
  $('#add',c).onclick=()=>userModal();
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Имя</th><th>Роль</th><th>2FA</th><th>Создан</th><th>Вход</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  users.forEach(u=>{
    const tr=h(`<tr><td><strong>${esc(u.username)}</strong>${u.disabled?' <span class="tag">выключен</span>':''}</td><td><span class="tag">${esc(u.role)}</span></td>
      <td>${u.totp_enabled?'<span class="tag ok">🔐 вкл.</span>':'<span class="tag">нет</span>'}</td>
      <td class="small muted">${fmtDate(u.created_at)}</td><td class="small muted">${fmtDate(u.last_login)}</td>
      <td style="text-align:right;white-space:nowrap"><button class="btn sm" data-pw>Пароль</button><button class="btn sm" data-logout title="Завершить все открытые сеансы этого пользователя (его текущие cookie перестанут действовать)">Завершить сеансы</button>${u.totp_enabled && u.id!==State.user.id?'<button class="btn sm" data-reset2fa title="Отключить двухфакторный вход (например, пользователь потерял телефон)">Сбросить 2FA</button>':''}<button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-pw]').onclick=()=>{ const m=modal(`Новый пароль: ${u.username}`,`<div class="form-row"><label>Пароль</label><input id="np" type="password"></div><div class="hint">После смены пароля все открытые сеансы этого пользователя будут завершены.</div>`,{footer:`<button class="btn primary" data-ok>Сохранить</button>`}); m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api(`/users/${u.id}/password`,{method:'PUT',body:{password:m.body.querySelector('#np').value}});toast('Пароль изменён, сеансы завершены');m.close();}catch(e){toastErr(e);}}; };
    const r2=tr.querySelector('[data-reset2fa]');
    if(r2) r2.onclick=async()=>{ if(await confirmDlg('Сбросить двухфакторный вход?',`Пользователь «${u.username}» сможет войти по одному паролю и должен будет заново привязать приложение. Все его сеансы будут завершены.`)){ try{await api(`/users/${u.id}/2fa/reset`,{method:'POST'});toast('2FA сброшена');route();}catch(e){toastErr(e);} } };
    tr.querySelector('[data-logout]').onclick=async()=>{ if(await confirmDlg('Завершить все сеансы?',`Пользователю «${u.username}» придётся войти заново на всех устройствах.`)){ try{const r=await api(`/users/${u.id}/logout-all`,{method:'POST'});toast(`Завершено сеансов: ${r.closed}`);}catch(e){toastErr(e);} } };
    tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить пользователя?','')){ try{await api(`/users/${u.id}`,{method:'DELETE'});toast('Удалено');route();}catch(e){toastErr(e);} } };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}
function userModal(){
  const m=modal('Новый пользователь',`<div class="form-row"><label>Имя</label><input id="u-name" type="text"></div><div class="form-row"><label>Пароль</label><input id="u-pass" type="password"></div>`,{footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Создать</button>`});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api('/users',{method:'POST',body:{username:m.body.querySelector('#u-name').value,password:m.body.querySelector('#u-pass').value}});toast('Создан');m.close();route();}catch(e){toastErr(e);}};
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
  user_disable:'пользователь отключён', user_create:'создан пользователь', user_delete:'удалён пользователь',
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
const JOBLBL = {backup:'Резервное копирование',restore:'Восстановление',export:'Экспорт',import_pst:'Импорт PST',test:'Проверка подключения',retention:'Очистка (ретеншн)',verify:'Проверка целостности',analyze:'Глубокий анализ писем',sync_employees:'Синхронизация сотрудников',storage_convert:'Шифрование копии',check_logins:'Проверка паролей',replicate:'Копия вне сервера',db_snapshot:'Снимок базы',search_index:'Индексация поиска',dedup_report:'Отчёт об одинаковых вложениях'};
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

  // Одинаковые вложения: отчёт догружается отдельно (его подсчёт — фоновое задание)
  if(!scope){
    const ddWrap=h('<div id="dedupWrap"></div>'); c.appendChild(ddWrap);
    loadDedup(ddWrap, redraws);
  }

  // Расписания и аудит
  const botGrid=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⏰ Ближайшие запуски</h3><div id="upNext"></div></div>
    <div class="card"><h3>🛡️ Топ действий (аудит)</h3><div class="an-chart" id="auditBars"></div></div></div>`);
  c.appendChild(botGrid);
  const up=d.schedules.upcoming;
  $('#upNext',botGrid).innerHTML = up.length? `<table class="an-mini-table">${up.map(u=>`<tr><td>${esc(accName(u.account_id)||('#'+u.account_id))}</td><td>${esc(JOBLBL[u.job_type]||u.job_type)}</td><td>${fmtDate(u.next_run)}</td></tr>`).join('')}</table>` : '<div class="an-empty-hint">Нет включённых расписаний</div>';
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
