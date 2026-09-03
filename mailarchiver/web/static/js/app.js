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

const State = { user:null, help:null, view:null, ws:null, wsTimer:null, engines:[], accounts:[] };

async function api(path, opts={}){
  const o = Object.assign({headers:{}}, opts);
  if(o.body && !(o.body instanceof FormData)){ o.headers['Content-Type']='application/json'; o.body=JSON.stringify(o.body); }
  o.headers['X-Requested-With']='fetch';
  const res = await fetch('/api'+path, o);
  let data=null; const ct=res.headers.get('content-type')||'';
  if(ct.includes('application/json')) data=await res.json();
  if(!res.ok){
    const msg = (data && (data.message||data.detail)) || ('Ошибка '+res.status);
    const err = new Error(msg); err.data=data; err.status=res.status; throw err;
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

// ---------- Тема ----------
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme');
  const next = cur==='dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try{ localStorage.setItem('ma-theme', next); }catch(e){}
}

// ---------- Подсказка по параметру ----------
function helpIcon(entry){
  if(!entry) return '';
  const ex = entry.example ? `<div class="ex">Пример: <code>${esc(entry.example)}</code></div>` : '';
  const rec = entry.recommend ? `<div class="rec">💡 ${esc(entry.recommend)}</div>` : '';
  const df = entry.default ? `<div class="ex">По умолчанию: <code>${esc(entry.default)}</code></div>` : '';
  return `<span class="help-ic">?<span class="help-pop"><div class="t">${esc(entry.title||'')}</div><div>${esc(entry.help||'')}</div>${rec}${ex}${df}</span></span>`;
}

// ---------- Модалки ----------
function modal(title, bodyHtml, {wide=false, footer=''}={}){
  const back = h(`<div class="modal-back"><div class="modal ${wide?'wide':''}">
    <div class="modal-head"><h2>${esc(title)}</h2><span class="x">×</span></div>
    <div class="modal-body">${bodyHtml}</div>
    ${footer?`<div class="modal-foot">${footer}</div>`:''}</div></div>`);
  const onKey=(e)=>{ if(e.key==='Escape') close(); };
  const close=()=>{ back.remove(); document.removeEventListener('keydown', onKey); };
  back.querySelector('.x').onclick=close;
  back.onclick=(e)=>{ if(e.target===back) close(); };
  document.addEventListener('keydown', onKey);
  document.body.appendChild(back);
  return { el:back, close, body:back.querySelector('.modal-body'), foot:back.querySelector('.modal-foot') };
}
function confirmDlg(title, message){
  return new Promise(res=>{
    const m=modal(title, `<p>${esc(message)}</p>`, {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn danger" data-ok>Подтвердить</button>`});
    m.foot.querySelector('[data-c]').onclick=()=>{m.close();res(false);};
    m.foot.querySelector('[data-ok]').onclick=()=>{m.close();res(true);};
  });
}

// ===================================================================
//  Аутентификация / первичная настройка
// ===================================================================
async function boot(){
  try{
    const ns = await api('/needs-setup');
    if(ns.needs_setup) return renderSetup();
    if(!ns.auth_enabled){ State.user={username:'admin',role:'admin'}; return startApp(); }
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
  const submit=async()=>{ try{ const r=await api('/login',{method:'POST',body:{username:$('#lu').value,password:$('#lp').value}}); State.user=r.user; startApp(); }catch(e){ toastErr(e); } };
  $('#lb').onclick=submit;
  card.addEventListener('keydown',e=>{ if(e.key==='Enter') submit(); });
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
      State.user=r.user; toast('Готово','Администратор создан'); startApp();
    }catch(e){ toastErr(e); }
  };
}

// ===================================================================
//  Оболочка приложения
// ===================================================================
const NAV = [
  {id:'dashboard', icon:'📊', title:'Дашборд', mb:true},
  {id:'mail', icon:'📧', title:'Почта', mb:true},
  {id:'accounts', icon:'📬', title:'Почтовые ящики'},
  {id:'jobs', icon:'⚙️', title:'Очередь и задания', mb:true},
  {id:'exports', icon:'📤', title:'Экспорт (PST)', mb:true},
  {id:'schedules', icon:'⏰', title:'Расписания'},
  {sep:true},
  {id:'logs', icon:'📋', title:'Логи'},
  {id:'settings', icon:'🔧', title:'Настройки'},
  {id:'users', icon:'👥', title:'Пользователи', admin:true},
  {id:'audit', icon:'🛡️', title:'Аудит', admin:true},
];
function isMailbox(){ return State.user && State.user.role === 'mailbox'; }

async function startApp(){
  try{ State.help = await api('/help'); }catch(e){ State.help={params:{},account:{},export:{},restore:{},schedule:{}}; }
  root().innerHTML='';
  const shell=h(`<div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="logo">📥</div><div><div class="name">MailArchiver</div><div class="ver">v${esc(window.MA_VERSION||'')}</div></div></div>
      <nav class="nav" id="nav"></nav>
      <div class="sidebar-foot"><span>${esc(State.user.username)}</span><a href="#" id="logout" title="Выход">Выход ⎋</a></div>
    </aside>
    <main class="main">
      <header class="topbar">
        <div class="page-title" id="pageTitle">Дашборд</div>
        <div class="spacer"></div>
        <span id="schedInd" class="small muted"></span>
        <button class="btn ghost sm" id="themeBtn" title="Сменить тему">🌓</button>
        <a class="btn ghost sm" href="/static/docs/index.html" target="_blank" title="Документация">📖 Документация</a>
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
    const a=h(`<a data-view="${item.id}"><span class="ic">${item.icon}</span><span>${esc(item.title)}</span></a>`);
    a.onclick=(e)=>{ e.preventDefault(); location.hash='#/'+item.id; };
    nav.appendChild(a);
  });
  if(isMailbox() && State.user.account_id){ State.mailAccount = State.user.account_id; }
  $('#themeBtn').onclick=toggleTheme;
  $('#logout').onclick=async(e)=>{ e.preventDefault(); try{await api('/logout',{method:'POST'});}catch(_){} location.reload(); };
  window.addEventListener('hashchange', route);
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
  State.view=view; setActiveNav(view);
  const c=$('#content'); c.innerHTML='<div class="empty"><div class="spinner"></div></div>';
  const map={dashboard:viewDashboard,mail:viewMail,accounts:viewAccounts,jobs:viewJobs,exports:viewExports,schedules:viewSchedules,logs:viewLogs,settings:viewSettings,users:viewUsers,audit:viewAudit};
  (map[view]||viewDashboard)(c).catch(toastErr);
}

// ---------- Живое обновление (WebSocket + запасной опрос) ----------
function startLive(){
  try{
    const proto = location.protocol==='https:'?'wss':'ws';
    const ws=new WebSocket(`${proto}://${location.host}/ws`);
    State.ws=ws;
    ws.onmessage=(ev)=>{ try{ const d=JSON.parse(ev.data); onLive(d); }catch(e){} };
    ws.onclose=()=>{ State.ws=null; if(!State.wsTimer) State.wsTimer=setInterval(pollLive, 3500); setTimeout(()=>{ if(!State.ws) startLive(); }, 8000); };
    ws.onerror=()=>{ try{ws.close();}catch(e){} };
  }catch(e){ if(!State.wsTimer) State.wsTimer=setInterval(pollLive,3500); }
}
async function pollLive(){ try{ const s=await api('/state'); onLive({active_jobs:s.active_jobs, job_counts:s.job_counts, scheduler_running:s.scheduler_running}); }catch(e){} }
function onLive(d){
  const ind=$('#schedInd'); if(ind) ind.innerHTML = d.scheduler_running ? '<span class="status-dot on"></span> Планировщик активен' : '<span class="status-dot off"></span> Планировщик выключен';
  if(State.view==='dashboard') updateDashboardLive(d);
  if(State.view==='jobs') updateJobsLive(d);
  if(State.view==='logs' && d.logs) renderLogLines(d.logs);
}

// ===================================================================
//  Дашборд
// ===================================================================
async function viewDashboard(c){
  const s=await api('/state'); State.engines=s.engines; State.accounts=s.accounts;
  c.innerHTML='';
  const stats=h(`<div class="grid cols-4" style="margin-bottom:16px">
    <div class="card stat-card"><div class="label">📬 Ящиков</div><div class="value">${s.totals.accounts}</div><div class="sub">под резервным копированием</div></div>
    <div class="card stat-card"><div class="label">✉️ Писем в архиве</div><div class="value">${s.totals.messages.toLocaleString('ru-RU')}</div><div class="sub">${esc(s.totals.bytes_h)}</div></div>
    <div class="card stat-card"><div class="label">⚙️ В очереди / работе</div><div class="value" id="dq">${(s.job_counts.queued||0)+(s.job_counts.running||0)}</div><div class="sub">воркеров: ${s.workers}</div></div>
    <div class="card stat-card"><div class="label">💽 Свободно на диске</div><div class="value">${esc(s.disk.free_h)}</div><div class="sub">каталог копий</div></div>
  </div>`);
  c.appendChild(stats);

  const live=h(`<div class="card" style="margin-bottom:16px"><div class="section-title"><h3>🔴 Текущие операции</h3><div class="spacer"></div><span class="muted small">обновляется в реальном времени</span></div><div id="liveJobs"></div></div>`);
  c.appendChild(live);
  updateDashboardLive({active_jobs:s.active_jobs, job_counts:s.job_counts});

  const grid=h(`<div class="grid cols-2">
    <div class="card"><div class="section-title"><h3>📈 Активность (30 дней)</h3></div><div class="chart-box"><canvas id="chart" height="220"></canvas></div></div>
    <div class="card"><div class="section-title"><h3>📬 Ящики</h3><div class="spacer"></div><button class="btn sm primary" id="addAcc">+ Добавить</button></div><div id="accList"></div></div>
  </div>`);
  c.appendChild(grid);
  $('#addAcc').onclick=()=>accountModal();
  renderAccountMini(s.accounts, $('#accList'));

  try{ const st=await api('/stats?days=30'); drawChart($('#chart'), st.series); }catch(e){}
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
        <span class="muted small">#${j.id}${accName(j.account_id)?(' · '+esc(accName(j.account_id))):''}</span>
        <span class="spacer" style="flex:1"></span>
        <span class="muted small">${esc(j.progress_message||'')}</span>
        ${j.status==='running'||j.status==='queued'?`<button class="btn danger sm" data-cancel="${j.id}">Отменить</button>`:''}
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
  accounts.forEach(a=>{
    const st=a.last_run?`<span class="badge ${a.last_run.status}">${esc(a.last_run.status)}</span>`:'<span class="tag">нет копий</span>';
    const row=h(`<div class="kv" style="align-items:center"><div style="flex:1"><strong>${esc(a.name)}</strong><div class="muted small">${esc(a.username)} · ${a.messages} писем · ${esc(a.bytes_h)}</div></div>${st}
      <button class="btn sm primary" data-bk="${a.id}">Копировать</button></div>`);
    row.querySelector('[data-bk]').onclick=async()=>{ try{ await api(`/accounts/${a.id}/backup`,{method:'POST'}); toast('Запущено','Резервное копирование добавлено в очередь'); location.hash='#/jobs'; }catch(e){toastErr(e);} };
    box.appendChild(row);
  });
}

// ---------- Простой график на canvas (без библиотек) ----------
function drawChart(canvas, series){
  if(!canvas) return; const ctx=canvas.getContext('2d');
  const W=canvas.width=canvas.clientWidth*2, H=canvas.height=440; ctx.scale(1,1);
  ctx.clearRect(0,0,W,H);
  const data=series||[]; if(!data.length){ ctx.fillStyle=getVar('--text-dim'); ctx.font='24px sans-serif'; ctx.fillText('Пока нет данных',20,40); return; }
  const vals=data.map(d=>d.messages);
  const max=Math.max(1,...vals); const pad=40, bw=(W-pad*2)/data.length;
  ctx.strokeStyle=getVar('--border'); ctx.lineWidth=1;
  for(let i=0;i<=4;i++){ const y=pad+(H-pad*2)*i/4; ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(W-pad,y); ctx.stroke(); ctx.fillStyle=getVar('--text-dim'); ctx.font='18px sans-serif'; ctx.fillText(Math.round(max*(4-i)/4),4,y+6); }
  data.forEach((d,i)=>{
    const barH=(H-pad*2)*(d.messages/max); const x=pad+i*bw+bw*0.15; const w=bw*0.7; const y=H-pad-barH;
    ctx.fillStyle=getVar('--primary'); roundRect(ctx,x,y,w,barH,4); ctx.fill();
    if(i%Math.ceil(data.length/8||1)===0){ ctx.fillStyle=getVar('--text-dim'); ctx.font='16px sans-serif'; ctx.save(); ctx.translate(x+w/2,H-pad+18); ctx.fillText((d.day||'').slice(5),-14,0); ctx.restore(); }
  });
}
function roundRect(ctx,x,y,w,hh,r){ if(hh<1)hh=1; ctx.beginPath(); ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+hh,r); ctx.arcTo(x+w,y+hh,x,y+hh,r); ctx.arcTo(x,y+hh,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath(); }
function getVar(n){ return getComputedStyle(document.documentElement).getPropertyValue(n).trim()||'#888'; }

// ===================================================================
//  Ящики
// ===================================================================
async function viewAccounts(c){
  const accs=await api('/accounts'); State.accounts=accs;
  c.innerHTML='';
  const head=h(`<div class="section-title"><h2 style="margin:0">Почтовые ящики</h2><div class="spacer"></div><button class="btn primary" id="add">+ Добавить ящик</button></div>`);
  c.appendChild(head); $('#add',c).onclick=()=>accountModal();
  if(!accs.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">📭</div>Пока нет ни одного ящика.<br>Нажмите «Добавить ящик», чтобы начать.</div></div>')); return; }
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Название</th><th>Сервер</th><th>Логин</th><th>Статус</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  accs.forEach(a=>{
    const tr=h(`<tr>
      <td><strong>${esc(a.name)}</strong>${a.enabled?'':' <span class="tag">выключен</span>'}</td>
      <td class="mono small">${esc(a.host)}:${a.port} <span class="tag">${a.security}</span></td>
      <td class="small">${esc(a.username)}</td>
      <td><span class="tag">${a.auth_type==='oauth2'?'OAuth2':'пароль'}</span></td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm" data-test>Проверить</button>
        <button class="btn sm primary" data-bk>Копировать</button>
        <button class="btn sm" data-menu>⋯</button>
      </td></tr>`);
    tr.querySelector('[data-test]').onclick=()=>testAccount(a.id);
    tr.querySelector('[data-bk]').onclick=async()=>{ try{await api(`/accounts/${a.id}/backup`,{method:'POST'}); toast('Запущено','Копирование в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} };
    tr.querySelector('[data-menu]').onclick=()=>accountMenu(a);
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
}

function accountMenu(a){
  const m=modal(`Ящик: ${a.name}`, `<div class="btn-row" style="flex-direction:column;align-items:stretch;gap:10px">
    <button class="btn primary" data-a="mail">📧 Просмотр писем</button>
    <button class="btn" data-a="edit">✏️ Редактировать</button>
    <button class="btn" data-a="export">📤 Экспорт в PST / EML / MBOX</button>
    <button class="btn" data-a="restore">♻️ Восстановить на сервер</button>
    <button class="btn" data-a="import">📥 Импорт из .pst</button>
    <button class="btn" data-a="retention">🗓️ Хранение копий (3 дня / неделя)</button>
    <button class="btn" data-a="verify">🔍 Проверить целостность копии</button>
    <button class="btn danger" data-a="del">🗑️ Удалить ящик</button>
  </div>`);
  m.body.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
    const act=b.dataset.a; m.close();
    if(act==='mail'){ State.mailAccount=a.id; location.hash='#/mail'; }
    else if(act==='edit') accountModal(a.id);
    else if(act==='export') exportModal(a);
    else if(act==='restore') restoreModal(a);
    else if(act==='import') importModal(a);
    else if(act==='retention') retentionModal(a);
    else if(act==='verify'){ try{await api(`/accounts/${a.id}/verify`,{method:'POST'}); toast('Запущено','Проверка целостности в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} }
    else if(act==='del'){ if(await confirmDlg('Удалить ящик?', `Ящик «${a.name}» и его настройки будут удалены. Локальные копии писем на диске останутся. Продолжить?`)){ try{await api(`/accounts/${a.id}`,{method:'DELETE'}); toast('Удалено'); route();}catch(e){toastErr(e);} } }
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
        <option value="oauth2"${a.auth_type==='oauth2'?' selected':''}>OAuth2 (Gmail / Microsoft 365)</option></select></div>
    </div>
    <div class="form-row"><label>Логин ${H('username')}</label><input id="f-user" type="text" value="${esc(a.username)}" placeholder="user@example.ru"></div>
    <div class="form-row" id="pwrow"><label>Пароль ${H('password')}</label><input id="f-pass" type="password" placeholder="${id?'оставьте пустым, чтобы не менять':'пароль или пароль приложения'}"></div>
    <div id="oauthBox" style="display:${a.auth_type==='oauth2'?'block':'none'}">
      <div class="form-row"><label>OAuth2 Client ID ${H('oauth_client_id')}</label><input id="f-ocid" type="text" value="${esc(a.oauth_client_id||'')}"></div>
      <div class="form-row"><label>OAuth2 Client Secret ${H('oauth_client_secret')}</label><input id="f-ocs" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Refresh Token ${H('oauth_refresh_token')}</label><input id="f-ort" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Token URL ${H('oauth_token_url')}</label><input id="f-otu" type="text" value="${esc(a.oauth_token_url||'')}" placeholder="https://oauth2.googleapis.com/token"></div>
    </div>
    <div class="form-row"><label>Исключить папки ${H('folder_exclude')}</label><input id="f-exc" type="text" value="${esc((a.folder_exclude||[]).join(', '))}" placeholder="Спам, Корзина"></div>
    <div class="form-row"><label>Хранение локальных копий</label><select id="f-ret">
      <option value="-1"${(a.retention_days??-1)===-1?' selected':''}>По глобальной настройке</option>
      <option value="0"${a.retention_days===0?' selected':''}>Хранить всё (бессрочно)</option>
      <option value="3"${a.retention_days===3?' selected':''}>Последние 3 дня</option>
      <option value="7"${a.retention_days===7?' selected':''}>Последняя неделя</option>
      <option value="30"${a.retention_days===30?' selected':''}>Последние 30 дней</option></select>
      <div class="hint">Копии старше срока удаляются ежедневно (на письма на сервере не влияет).</div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="f-en" ${a.enabled?'checked':''}><span class="track"></span></span><label>Ящик включён (участвует в бэкапе)</label></div>`;
  const m=modal(id?'Редактирование ящика':'Новый почтовый ящик', body, {wide:true,
    footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn" data-test>Проверить подключение</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=(x)=>m.body.querySelector(x);
  g('#f-auth').onchange=()=>{ m.body.querySelector('#oauthBox').style.display=g('#f-auth').value==='oauth2'?'block':'none'; m.body.querySelector('#pwrow').style.display=g('#f-auth').value==='oauth2'?'none':'block'; };
  const collect=()=>({name:g('#f-name').value,host:g('#f-host').value,port:parseInt(g('#f-port').value||'993'),username:g('#f-user').value,
     password:g('#f-pass').value,security:g('#f-sec').value,auth_type:g('#f-auth').value,enabled:g('#f-en').checked,
     folder_exclude:g('#f-exc').value.split(',').map(s=>s.trim()).filter(Boolean),
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
    <div class="grid cols-2">
      <div class="form-row"><label>Формат PST ${H('pst_format')}</label><select id="e-pst"><option value="unicode">Unicode — Outlook 2003 и новее</option><option value="ansi">ANSI — Outlook 97–2002 (до 2 ГБ)</option></select></div>
      <div class="form-row"><label>Целевая версия Outlook</label><select id="e-target"><option>2016+</option><option>2013</option><option>2010</option><option>2007</option><option>2003</option><option value="2002">2002 и старше</option><option>365</option></select></div>
    </div>
    <div class="grid cols-2">
      <div class="form-row"><label>Дата с ${H('date_from')}</label><input id="e-df" type="date"></div>
      <div class="form-row"><label>Дата по ${H('date_to')}</label><input id="e-dt" type="date"></div>
    </div>
    <div class="form-row"><label>Только папки (через запятую, пусто = все) ${H('folders')}</label><input id="e-folders" type="text" placeholder="INBOX, Отправленные"></div>
    <div class="hint">Файл будет доступен для скачивания в разделе «Экспорт» после завершения.</div>`;
  const m=modal(`Экспорт: ${a.name}`, body, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Начать экспорт</button>`});
  const g=x=>m.body.querySelector(x);
  const upd=()=>{ const e=engines.find(x=>x.name===g('#e-engine').value); g('#e-desc').innerHTML=e?esc(e.desc)+(e.experimental?' <strong style="color:var(--warn)">Экспериментально — проверьте результат в своём Outlook.</strong>':''):''; const isPst=e&&e.fmt==='pst'; g('#e-pst').closest('.form-row').style.display=isPst?'block':'none'; };
  g('#e-engine').onchange=upd; upd();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const e=engines.find(x=>x.name===g('#e-engine').value);
    const body={engine:g('#e-engine').value, format:e?e.fmt:'pst', pst_format:g('#e-pst').value, outlook_target:g('#e-target').value,
      date_from:g('#e-df').value||null, date_to:g('#e-dt').value||null,
      folders:g('#e-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
    try{ await api(`/accounts/${a.id}/export`,{method:'POST',body}); toast('Экспорт запущен','Следите за прогрессом в очереди'); m.close(); location.hash='#/jobs'; }catch(e){toastErr(e);}
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
    const body={target_mode:g('#r-mode').value,target_prefix:g('#r-prefix').value,target_folder:g('#r-single').value,
      check_duplicates:g('#r-dup').checked,dry_run:g('#r-dry').checked,
      folders:g('#r-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
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
    try{ const btn=m.foot.querySelector('[data-go]'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Загрузка…';
      await api(`/accounts/${a.id}/import-pst`,{method:'POST',body:fd}); toast('Импорт запущен'); m.close(); location.hash='#/jobs';
    }catch(e){toastErr(e);}
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
  renderJobsRows(jobs);
}
function renderJobsRows(jobs){
  const tb=$('#jobsBody'); if(!tb) return; tb.innerHTML='';
  if(!jobs.length){ tb.innerHTML='<tr><td colspan="7" class="empty">Заданий пока нет</td></tr>'; return; }
  jobs.forEach(j=>{
    const prog=j.progress_total?`<div class="progress ${j.status}"><span style="width:${j.percent}%"></span></div><div class="muted small">${j.percent}% ${j.progress_message?('· '+esc(j.progress_message)):''}</div>`:(j.status==='running'?'<span class="muted small">выполняется…</span>':'—');
    const tr=h(`<tr>
      <td class="muted">${j.id}</td>
      <td>${esc(j.type_label)}</td>
      <td class="small">${esc(accName(j.account_id))||'—'}</td>
      <td><span class="badge ${j.status}">${esc(j.status_label)}</span></td>
      <td style="min-width:160px">${prog}</td>
      <td class="small muted">${fmtDate(j.created_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm" data-info>Детали</button>
        ${(j.status==='running'||j.status==='queued')?`<button class="btn danger sm" data-cancel>Отмена</button>`:''}
        ${(j.status==='failed'||j.status==='cancelled'||j.status==='partial')?`<button class="btn sm" data-retry>Повторить</button>`:''}
      </td></tr>`);
    tr.querySelector('[data-info]').onclick=()=>jobDetails(j.id);
    const cb=tr.querySelector('[data-cancel]'); if(cb) cb.onclick=async()=>{try{await api(`/jobs/${j.id}/cancel`,{method:'POST'});toast('Отмена запрошена');}catch(e){toastErr(e);}};
    const rb=tr.querySelector('[data-retry]'); if(rb) rb.onclick=async()=>{try{await api(`/jobs/${j.id}/retry`,{method:'POST'});toast('Возвращено в очередь');route();}catch(e){toastErr(e);}};
    tb.appendChild(tr);
  });
}
async function updateJobsLive(){ try{ const jobs=await api('/jobs?limit=80'); renderJobsRows(jobs); }catch(e){} }

async function jobDetails(id){
  const j=await api(`/jobs/${id}`); const ev=await api(`/jobs/${id}/events`);
  const res=j.result?Object.entries(j.result).map(([k,v])=>`<div class="kv"><div class="k">${esc(k)}</div><div>${esc(typeof v==='object'?JSON.stringify(v):v)}</div></div>`).join(''):'';
  const evs=ev.map(e=>`<div class="l-${e.level}">${fmtDate(e.ts)} [${e.level}] ${esc(e.message)}</div>`).join('')||'<span class="muted">нет событий</span>';
  modal(`Задание #${j.id} — ${j.type_label}`, `
    <div class="kv"><div class="k">Статус</div><div><span class="badge ${j.status}">${esc(j.status_label)}</span></div></div>
    <div class="kv"><div class="k">Создано / завершено</div><div>${fmtDate(j.created_at)} → ${fmtDate(j.finished_at)}</div></div>
    <div class="kv"><div class="k">Попыток</div><div>${j.attempts}/${j.max_attempts}</div></div>
    ${j.error?`<div class="kv"><div class="k">Ошибка</div><div style="color:var(--danger)">${esc(j.error)}</div></div>`:''}
    ${res}
    <h3 style="margin-top:16px">Журнал событий</h3><div class="log-view">${evs}</div>`, {wide:true});
}

// ===================================================================
//  Экспорты (список готовых файлов)
// ===================================================================
async function viewExports(c){
  const list=await api('/exports');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Готовые экспорты</h2></div>'));
  if(!list.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">📤</div>Экспортов пока нет.<br>Откройте ящик → «Экспорт в PST».</div></div>')); return; }
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Ящик</th><th>Формат</th><th>Размер</th><th>Статус</th><th>Создан</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  list.forEach(e=>{
    const tr=h(`<tr>
      <td class="mono small">${esc(e.filename||'—')}</td>
      <td class="small">${esc(accName(e.account_id))||'—'}</td>
      <td><span class="tag">${esc(e.format)} · ${esc(e.engine)}</span></td>
      <td>${esc(e.size_h)}</td>
      <td><span class="badge ${e.status}">${esc(e.status)}</span></td>
      <td class="small muted">${fmtDate(e.created_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        ${e.exists?`<a class="btn sm primary" href="/api/exports/${e.id}/download">⬇ Скачать</a>`:'<span class="muted small">файл удалён</span>'}
        <button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить экспорт?','Файл будет удалён с диска.')){ try{await api(`/exports/${e.id}`,{method:'DELETE'});toast('Удалено');route();}catch(err){toastErr(err);} } };
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
      <td><span class="tag">${esc(s.job_type)}</span></td>
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
        <option value="verify"${s.job_type==='verify'?' selected':''}>Проверка целостности</option></select></div>
      <div class="form-row"><label>Тип ${H('kind')}</label><select id="s-kind">
        <option value="cron"${s.kind==='cron'?' selected':''}>По времени (cron)</option>
        <option value="interval"${s.kind==='interval'?' selected':''}>Через интервал</option></select></div>
    </div>
    <div class="form-row" id="s-cronrow"><label>Cron-выражение ${H('cron_expr')}</label><input id="s-cron" type="text" value="${esc(s.cron_expr||'0 3 * * *')}">
      <div class="hint">Примеры: <code>0 3 * * *</code> — ежедневно в 03:00; <code>0 */6 * * *</code> — каждые 6 часов; <code>30 2 * * 1</code> — по понедельникам в 02:30.</div></div>
    <div class="form-row" id="s-introw" style="display:none"><label>Интервал (минут) ${H('interval_seconds')}</label><input id="s-int" type="number" value="${Math.round((s.interval_seconds||21600)/60)}"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="s-en" ${s.enabled?'checked':''}><span class="track"></span></span><label>Расписание включено</label></div>`;
  const m=modal(s.id?'Редактирование расписания':'Новое расписание', body, {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=x=>m.body.querySelector(x);
  g('#s-kind').onchange=()=>{ g('#s-cronrow').style.display=g('#s-kind').value==='cron'?'block':'none'; g('#s-introw').style.display=g('#s-kind').value==='interval'?'block':'none'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=async()=>{
    const body={account_id:parseInt(g('#s-acc').value),job_type:g('#s-job').value,kind:g('#s-kind').value,
      cron_expr:g('#s-cron').value,interval_seconds:parseInt(g('#s-int').value||'60')*60,enabled:g('#s-en').checked,options:{}};
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
  const load=async()=>{ try{ const lv=$('#logLevel').value; const logs=await api('/logs?limit=400'+(lv?('&level='+lv):'')); renderLogLines(logs); }catch(e){toastErr(e);} };
  $('#logLevel').onchange=load; await load();
}
function renderLogLines(logs){
  const v=$('#logView'); if(!v) return;
  v.innerHTML=logs.map(l=>{ const d=new Date((l.ts||0)*1000).toLocaleTimeString('ru-RU'); return `<div class="l-${l.level}">${d} [${l.level}] ${esc(l.message)}</div>`; }).join('');
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
};
async function viewSettings(c){
  const data=await api('/settings');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Настройки</h2><div class="spacer"></div><button class="btn primary" id="saveAll">💾 Сохранить всё</button></div>'));
  const help=data.help.params||{};
  const changed={};
  data.sections.forEach(sec=>{
    const card=h(`<div class="card" style="margin-bottom:16px"><h3>${sec.icon||''} ${esc(sec.title)}</h3><div class="settings-grid"></div></div>`);
    const grid=card.querySelector('.settings-grid');
    sec.keys.forEach(key=>{
      const full=sec.section+'.'+key; const val=(data.values[sec.section]||{})[key];
      const hp=help[full]; const label=`${esc(hp?hp.title:key)} ${helpIcon(hp)}`;
      let input;
      if(typeof val==='boolean'){
        input=h(`<div class="form-row check"><span class="switch"><input type="checkbox" ${val?'checked':''}><span class="track"></span></span><label>${label}</label></div>`);
        input.querySelector('input').onchange=e=>changed[full]=e.target.checked;
      } else if(ENUM_OPTS[full]){
        const o=ENUM_OPTS[full].map(([v,t])=>`<option value="${v}" ${String(v)===String(val)?'selected':''}>${esc(t)}</option>`).join('');
        input=h(`<div class="form-row"><label>${label}</label><select>${o}</select></div>`);
        input.querySelector('select').onchange=e=>changed[full]=e.target.value;
      } else if(Array.isArray(val)){
        input=h(`<div class="form-row"><label>${label}</label><input type="text" value="${esc(val.join(', '))}"></div>`);
        input.querySelector('input').oninput=e=>changed[full]=e.target.value.split(',').map(s=>s.trim()).filter(Boolean);
      } else if(typeof val==='number'){
        input=h(`<div class="form-row"><label>${label}</label><input type="number" value="${val}" step="${full.includes('backoff')?'0.1':'1'}"></div>`);
        input.querySelector('input').oninput=e=>changed[full]=full.includes('backoff')?parseFloat(e.target.value):parseInt(e.target.value);
      } else {
        const isPw=key.includes('password')||key.includes('secret');
        input=h(`<div class="form-row"><label>${label}</label><input type="${isPw?'password':'text'}" value="${esc(val==null?'':val)}" ${isPw?'placeholder="без изменений"':''}></div>`);
        input.querySelector('input').oninput=e=>changed[full]=e.target.value;
      }
      grid.appendChild(input);
    });
    c.appendChild(card);
  });
  // grid layout
  $$('.settings-grid',c).forEach(g=>{ g.style.display='grid'; g.style.gap='4px 24px'; g.style.gridTemplateColumns='repeat(auto-fill,minmax(300px,1fr))'; });
  $('#saveAll').onclick=async()=>{
    if(!Object.keys(changed).length) return toast('Нет изменений','','warn');
    try{ const r=await api('/settings',{method:'PUT',body:{values:changed}}); toast('Сохранено', `Изменено параметров: ${r.changed.length}`); }catch(e){toastErr(e);}
  };
}

// ===================================================================
//  Пользователи и аудит (админ)
// ===================================================================
async function viewUsers(c){
  const users=await api('/users');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Пользователи</h2><div class="spacer"></div><button class="btn primary" id="add">+ Добавить</button></div>'));
  $('#add',c).onclick=()=>userModal();
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Имя</th><th>Роль</th><th>Создан</th><th>Вход</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  users.forEach(u=>{
    const tr=h(`<tr><td><strong>${esc(u.username)}</strong>${u.disabled?' <span class="tag">выключен</span>':''}</td><td><span class="tag">${esc(u.role)}</span></td>
      <td class="small muted">${fmtDate(u.created_at)}</td><td class="small muted">${fmtDate(u.last_login)}</td>
      <td style="text-align:right"><button class="btn sm" data-pw>Пароль</button><button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-pw]').onclick=()=>{ const m=modal(`Новый пароль: ${u.username}`,`<div class="form-row"><label>Пароль</label><input id="np" type="password"></div>`,{footer:`<button class="btn primary" data-ok>Сохранить</button>`}); m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api(`/users/${u.id}/password`,{method:'PUT',body:{password:m.body.querySelector('#np').value}});toast('Пароль изменён');m.close();}catch(e){toastErr(e);}}; };
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
//  Почта (просмотр писем)
// ===================================================================
const Mail = { acc:null, folder:null, offset:0, total:0, limit:50, msg:null };

async function viewMail(c){
  c.innerHTML='';
  let accId = isMailbox() ? State.user.account_id : (State.mailAccount||null);
  let accounts = [];
  const head=h(`<div class="section-title"><h2 style="margin:0">📧 Почта</h2><div class="spacer"></div><span id="mailAccWrap"></span></div>`);
  c.appendChild(head);
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
    const el=h(`<div class="mail-folder ${f.folder===Mail.folder?'active':''}"><span>📁</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.folder)}</span><span class="cnt">${f.count}</span></div>`);
    el.onclick=()=>{ Mail.folder=f.folder; Mail.offset=0; box.querySelectorAll('.mail-folder').forEach(x=>x.classList.remove('active')); el.classList.add('active'); loadMailMessages(accId); };
    box.appendChild(el);
  });
  loadMailMessages(accId);
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
    <span>${data.total} писем</span><span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" ${Mail.offset<=0?'disabled':''} data-prev>←</button>
    <span>${Math.floor(Mail.offset/Mail.limit)+1}/${Math.max(1,Math.ceil(data.total/Mail.limit))}</span>
    <button class="btn ghost sm" ${Mail.offset+Mail.limit>=data.total?'disabled':''} data-next>→</button></div>`);
  box.appendChild(bar);
  bar.querySelector('[data-prev]').onclick=()=>{ Mail.offset=Math.max(0,Mail.offset-Mail.limit); loadMailMessages(accId); };
  bar.querySelector('[data-next]').onclick=()=>{ Mail.offset+=Mail.limit; loadMailMessages(accId); };
  if(!data.messages.length){ box.appendChild(h('<div class="empty">Папка пуста</div>')); return; }
  data.messages.forEach(m=>{
    const el=h(`<div class="msg-item ${m.seen?'':'unread'}" data-id="${m.id}">
      <div class="msg-top"><span class="msg-from">${m.flagged?'⭐ ':''}${m.answered?'↩ ':''}${esc(m.from||'—')}</span><span>${m.has_attach?'📎':''}</span><span>${esc(fmtDateShort(m.date))}</span></div>
      <div class="msg-subj">${esc(m.subject)}</div>
      <div class="muted small">${esc(m.size_h)}</div></div>`);
    el.onclick=()=>{ box.querySelectorAll('.msg-item').forEach(x=>x.classList.remove('active')); el.classList.add('active'); el.classList.remove('unread'); loadMailMessage(accId, m.id); };
    box.appendChild(el);
  });
}
function fmtDateShort(iso){ if(!iso) return ''; try{ const d=new Date(iso); return d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit',year:'2-digit'}); }catch(e){ return ''; } }

async function loadMailMessage(accId, pk){
  const box=$('#mreader'); if(!box) return;
  box.innerHTML='<div class="empty" style="padding:40px"><div class="spinner"></div></div>';
  let m;
  try{ m=await api(`/accounts/${accId}/messages/${pk}`); }catch(e){ box.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
  const hd=m.headers||{};
  const atts=(m.attachments||[]).map(a=>`<a class="attach-chip" href="/api/accounts/${accId}/messages/${pk}/attachment/${a.index}" download>📎 ${esc(a.filename)} <span class="muted">(${fmtBytes(a.size)})</span></a>`).join('');
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
    ${atts?`<div style="margin-bottom:14px"><div class="muted small" style="margin-bottom:4px">Вложения (${m.attachments.length}):</div>${atts}</div>`:''}
    ${(hasText&&hasHtml)?`<div class="body-tabs"><button class="btn sm primary" data-tab="text">Текст</button><button class="btn sm" data-tab="html">HTML</button></div>`:''}
    <div id="mbody"></div>
  </div>`);
  box.appendChild(el);
  const bodyBox=el.querySelector('#mbody');
  const showText=()=>{ bodyBox.innerHTML=''; bodyBox.appendChild(h(`<div class="mail-body-text">${esc(m.text||'(пустое тело)')}</div>`)); };
  const showHtml=()=>{ bodyBox.innerHTML=''; const f=document.createElement('iframe'); f.className='mail-body-frame'; f.setAttribute('sandbox',''); f.srcdoc=m.html; bodyBox.appendChild(f); };
  if(hasText) showText(); else if(hasHtml) showHtml(); else showText();
  el.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{ el.querySelectorAll('[data-tab]').forEach(x=>x.className='btn sm'); b.className='btn sm primary'; b.dataset.tab==='html'?showHtml():showText(); });
}

// ---------- Старт ----------
boot();
