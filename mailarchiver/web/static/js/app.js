/* ===================================================================
   MailArchiver — веб-интерфейс (SPA на чистом JS, без внешних зависимостей).

   С 1.22.0 интерфейс разбит на файлы — обычные (не модульные) скрипты с общей глобальной областью,
   которые подключаются по порядку списком mailarchiver/web/assets.py (JS_FILES). Этот файл — основа:
   запросы к API, окна и подсказки, вход, оболочка и переходы между разделами; остальные — разделы.
   Код, выполняемый при загрузке файла, может обращаться только к своему и предыдущим файлам;
   интерфейс запускается (boot) в конце последнего.
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
  let res;
  try{ res = await fetch('/api'+path, o); }
  catch(e){
    // сеть пропала или сервер перезапускается: у fetch на это английское «Failed to fetch»
    const err=new Error('Нет связи с сервером. Проверьте сеть и повторите.'); err.network=true; err.bg=bg; throw err;
  }
  let data=null; const ct=res.headers.get('content-type')||'';
  if(ct.includes('application/json')) data=await res.json();
  // HTTPException с объектом в detail: поднимаем поля наверх (message/code/hint)
  if(data && data.detail && typeof data.detail==='object' && !Array.isArray(data.detail)){
    data = Object.assign({}, data.detail, {detail: data.detail.message||''});
  }
  if(!res.ok){
    const msg = (data && (data.message||(typeof data.detail==='string'?data.detail:''))) || ('Ошибка '+res.status);
    const err = new Error(msg); err.data=data; err.status=res.status; err.bg=bg;
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
// Ошибка запроса, которую обработчик кнопки не перехватил (окно «Детали», окна ящика и экспорта и т. п.):
// раньше при сбое сети такие кнопки молча ничего не делали. Фоновые запросы и конец сеанса — без сообщения.
window.addEventListener('unhandledrejection', (ev)=>{
  const e=ev.reason;
  if(!e || e.bg || e.status===401 || e.name==='AbortError' || State.sessionGone) return;
  if(!(e instanceof Error)) return;
  toastErr(e);
});

/** Сессия закончилась (тайм-аут, выход в другой вкладке, смена пароля):
 *  останавливаем живое обновление и возвращаемся к форме входа. Раньше раздел
 *  навсегда оставался со спиннером, а вкладка продолжала опрашивать сервер. */
function sessionGone(){
  if(State.sessionGone) return;
  State.sessionGone=true;
  State.leaveGuard=null;          // сеанс кончился — сохранить уже нельзя, вопрос при перезагрузке не нужен
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
  if(State.view==='docs' && State.docTheme){ try{ State.docTheme(); }catch(e){} }
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
function confirmDlg(title, message, {okText='Подтвердить', okClass='danger', html=false}={}){
  return new Promise(res=>{
    // Промис обязан разрешиться при ЛЮБОМ закрытии: иначе «await confirmDlg»
    // висел вечно, и кнопка выглядела сломанной.
    // html — сообщение уже собрано из экранированных частей (список предупреждений)
    const m=modal(title, html?message:`<p>${esc(message)}</p>`,
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
    catch(e){ State.sso={oidc:ns.oidc||{}, saml:ns.saml||{}}; renderLogin(State.sso); }
  }catch(e){ root().innerHTML=`<div class="auth-wrap"><div class="card auth-card"><h2>Сервис недоступен</h2><p class="muted">${esc(e.message)}</p></div></div>`; }
}

function renderLogin(sso){
  sso=sso||State.sso||{};
  const oidc=sso.oidc||{}, saml=sso.saml||{};
  const ssoBtns=[oidc.enabled?`<a class="btn" id="lsso" href="/api/auth/oidc/start" style="width:100%;justify-content:center">🔑 ${esc(oidc.button||'Войти через единый вход')}</a>`:'',
    saml.enabled?`<a class="btn" id="lsaml" href="/api/auth/saml/start" style="width:100%;justify-content:center${oidc.enabled?';margin-top:8px':''}">🪪 ${esc(saml.button||'Войти через SAML')}</a>`:''].join('');
  root().innerHTML='';
  const ssoFailed=/[?&]sso_error=/.test(location.search);
  const card=h(`<div class="auth-wrap"><div class="card auth-card">
    <div class="brand"><div class="logo">📥</div><div><div class="name">MailArchiver</div><div class="ver muted">Резервное копирование почты</div></div></div>
    <h3 style="text-align:center">Вход в систему</h3>
    <div class="form-row"><label>Пользователь</label><input id="lu" type="text" autocomplete="username" autofocus></div>
    <div class="form-row"><label>Пароль</label><input id="lp" type="password" autocomplete="current-password"></div>
    <button class="btn primary" id="lb" style="width:100%;justify-content:center">Войти</button>
    ${ssoBtns?`<div class="auth-or small muted">или</div>${ssoBtns}`:''}
    ${ssoFailed?'<div class="hint" style="margin-top:10px;border-color:var(--danger)">Единый вход не удался. Попробуйте ещё раз; если не помогает — сообщите администратору (подробности — в аудите архива).</div>':''}
  </div></div>`);
  root().appendChild(card);
  if(ssoFailed) history.replaceState(null, '', location.pathname+location.hash);
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
  {id:'replies', icon:'↩️', title:'Ответы на письма', roles:'am'},
  {id:'cases', icon:'⚖️', title:'Дела', roles:'a'},
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
  {id:'docs', icon:'📖', title:'Документация', roles:'aom'},
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
/** Ящики сотрудника (вход по ящику): все ящики его карточки, вошедший — первым. */
function myAccounts(){
  if(!isMailbox()) return [];
  const list=State.user.accounts||[];
  return list.length ? list : [{id:State.user.account_id, name:State.user.account_name||'мой ящик'}];
}
function canTouchJob(j){
  // своё задание — запущенное под логином любого из своих ящиков: войти можно паролем любого
  if(isMailbox()) return !!j && (State.user.logins||[State.user.username]).includes(j.created_by);
  if(isOperator()) return !!j && !!j.operator_can_manage;
  return true;
}

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
  // «?» внутри подписи, связанной с полем: щелчок открывает подсказку, а не переключает флажок и не ставит
  // курсор в поле
  if(ic){ placeHelp(ic); ic.classList.toggle('show'); if(ic.closest('label')) e.preventDefault(); }
});
// Подписи полей форм. Формы собираются из разметки «.form-row > label + поле», и у большинства подпись не
// была связана с полем: экранный диктор читал «поле ввода» без названия, а щелчок по подписи не ставил курсор.
// Связываем сами: поле без id и без aria-label получает id, подпись — for.
let fieldSeq=0;
function linkFormLabels(root){
  if(!root || !root.querySelectorAll) return;
  const rows = root.matches && root.matches('.form-row') ? [root] : root.querySelectorAll('.form-row');
  rows.forEach(row=>{
    const label=row.querySelector(':scope > label');
    if(!label || label.htmlFor || label.querySelector('input,select,textarea')) return;
    const fields=row.querySelectorAll('input:not([type=hidden]),select,textarea');
    if(fields.length!==1) return;
    const f=fields[0];
    if(f.getAttribute('aria-label') || f.getAttribute('aria-labelledby')) return;
    if(!f.id) f.id='fld-'+(++fieldSeq);
    label.htmlFor=f.id;
  });
}
new MutationObserver(list=>{ for(const m of list) m.addedNodes.forEach(n=>{ if(n.nodeType===1) linkFormLabels(n); }); })
  .observe(document.documentElement, {childList:true, subtree:true});
// Ссылки-кнопки (href="#") никуда не переходят: без этого щелчок менял адрес на «#» и открывал дашборд.
// Раньше в href у них был код («javascript:» с void(0)), но такие ссылки запрещает политика безопасности страницы.
document.addEventListener('click', (e)=>{
  const a=e.target && e.target.closest ? e.target.closest('a[href="#"]') : null;
  if(a) e.preventDefault();
}, true);
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
        <a class="btn ghost sm" id="docsBtn" href="#/docs" title="Справка к этому разделу">📖 <span class="hide-narrow">Справка</span></a>
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
  $('#nav').addEventListener('click', ()=>setNav(false));   // выбрали раздел — меню закрылось
  window.addEventListener('hashchange', ()=>setNav(false));
  $('#themeBtn').onclick=toggleTheme;
  // «📖 Справка» открывает в разделе «Документация» главу про тот раздел, где сейчас человек
  $('#docsBtn').onclick=(e)=>{ e.preventDefault(); if(State.view!=='docs') location.hash=docsHref(DOC_ANCHORS[State.view]||''); };
  $('#logout').onclick=async(e)=>{ e.preventDefault(); if(isMailbox()) await mailPushOff().catch(()=>{}); try{await api('/logout',{method:'POST'});}catch(_){} location.reload(); };
  $('#profileLink').onclick=(e)=>{ e.preventDefault(); if(isMailbox()) return toast('Вход по ящику','Защита такого входа — пароль самого почтового ящика.','warn'); twoFactorModal({}); };
  window.addEventListener('hashchange', route);
  // закрытие вкладки или перезагрузка при несохранённых настройках — стандартный вопрос браузера
  window.addEventListener('beforeunload', (e)=>{ if(State.leaveGuard && State.leaveGuard()){ e.preventDefault(); e.returnValue=''; } });
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
  // Уход из раздела с несохранёнными изменениями (настройки) — только после подтверждения: раньше они
  // молча пропадали. Отказ возвращает адрес прежнего раздела, не перерисовывая его.
  if(State.leaveGuard && State.view && State.view!==view){
    const warn=State.leaveGuard();
    if(warn && !window.confirm(warn)){ history.replaceState(null, '', '#/'+State.view); setActiveNav(State.view); return; }
  }
  State.leaveGuard=null;
  State.view=view; setActiveNav(view); State.redraw=null;
  if(view!=='mail') mailResetTitle();
  const content=$('#content');
  // почтовому клиенту нужна вся ширина экрана — три колонки в 1240px тесно
  content.classList.toggle('wide', view==='mail' || view==='docs');
  // Каждый переход рисует в СВОЙ контейнер. Медленный ответ прежнего раздела
  // (аналитика, поиск) раньше дорисовывался поверх нового: заголовок «Логи»,
  // а на экране аналитика. Теперь он пишет в уже отцепленный узел.
  content.innerHTML='';
  const c=h('<div class="view-host"><div class="empty"><div class="spinner"></div></div></div>');
  content.appendChild(c);
  State.routeSeq=(State.routeSeq||0)+1;
  const map={dashboard:viewDashboard,analytics:viewAnalytics,mailanalytics:viewMailAnalytics,replies:viewReplies,cases:viewCases,mail:viewMail,accounts:viewAccounts,employees:viewEmployees,jobs:viewJobs,exports:viewExports,schedules:viewSchedules,axigen:viewAxigen,mailservers:viewMailservers,server:viewServer,logs:viewLogs,settings:viewSettings,users:viewUsers,security:viewSecurity,audit:viewAudit,docs:viewDocs};
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

