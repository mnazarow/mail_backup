/* MailArchiver — веб-интерфейс: Встроенная справка и «Журнал безопасности».
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Документация — встроенная справка внутри интерфейса
// ===================================================================
//: Глава справки для каждого раздела: «📖 Справка» в шапке открывает её.
const DOC_ANCHORS={dashboard:'dashboard', analytics:'analytics', mailanalytics:'mailanalytics', replies:'replies', cases:'cases', mail:'mailview',
  accounts:'mailboxes', employees:'employees', jobs:'jobs', exports:'export', schedules:'schedules', axigen:'axigen',
  mailservers:'mailservers', server:'serverbulk', logs:'jobs', settings:'settings', users:'roles',
  security:'secjournal', audit:'audit'};
function docsHref(anchor){ return '#/docs'+(anchor?'?a='+encodeURIComponent(anchor):''); }
/** Отпечаток файлов интерфейса (из адреса app.js): справка после обновления не берётся из кеша браузера. */
function assetTag(){
  const sc=document.querySelector('script[src*="/static/js/app.js"]');
  const m=sc && /[?&]v=([^&]+)/.exec(sc.getAttribute('src')||'');
  return m?m[1]:'';
}
//: Подсветка найденного — во фрейме справки.
const DOC_EMBED_CSS=`mark.ma-hit{background:#ffe58a;color:inherit;border-radius:3px;padding:0 1px}
mark.ma-hit.cur{background:#ffb35c;box-shadow:0 0 0 2px #ff9f43}`;
//: Тёмная тема справки (сама страница справки — светлая): те же переменные, что у неё в :root.
const DOC_DARK_CSS=`:root{--bg:#0f141b;--soft:#19212c;--bd:#2a3442;--tx:#e3e9f1;--dim:#9aa7b8;--pri:#7aa7ff}
body{background:var(--bg)} .note{background:#132340} .warn{background:#33280f} .tip{background:#10291c}
.card{background:var(--soft)} .pname code{background:var(--bg)} img.shot{box-shadow:none}
.badge.ok{background:#10291c} .badge.exp{background:#33280f}
mark.ma-hit{background:#8a6d12;color:#fff} mark.ma-hit.cur{background:#c2610c;box-shadow:0 0 0 2px #c2610c}`;

/** Найти в справке все вхождения текста (без учёта регистра, ё = е) и подсветить их → список подсветок. */
function docFind(doc, query){
  doc.querySelectorAll('mark.ma-hit').forEach(m=>{ const p=m.parentNode; p.replaceChild(doc.createTextNode(m.textContent), m); p.normalize(); });
  const norm=t=>t.toLowerCase().replace(/ё/g,'е');
  const needle=norm((query||'').trim());
  if(needle.length<2) return [];
  const root=doc.querySelector('main')||doc.body;
  const walker=doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes=[]; while(walker.nextNode()) nodes.push(walker.currentNode);
  const marks=[];
  for(const node of nodes){
    if(!node.parentElement || node.parentElement.closest('script,style')) continue;
    const low=norm(node.nodeValue);
    if(low.length!==node.nodeValue.length) continue;          // редкие буквы, у которых строчная длиннее
    let idx=low.indexOf(needle), cur=node, done=0;
    while(idx>=0 && marks.length<1000){
      const hit=cur.splitText(idx-done);
      cur=hit.splitText(needle.length);
      const m=doc.createElement('mark'); m.className='ma-hit';
      hit.parentNode.replaceChild(m, hit); m.appendChild(hit);
      marks.push(m);
      done=idx+needle.length;
      idx=low.indexOf(needle, done);
    }
  }
  return marks;
}

async function viewDocs(c){
  const params=new URLSearchParams(location.hash.split('?')[1]||'');
  const anchor=(params.get('a')||'').replace(/[^a-z0-9_-]/gi,'');
  c.innerHTML='';
  const bar=h(`<div class="docs-bar">
    <select id="docToc" aria-label="Глава документации"><option value="">Оглавление…</option></select>
    <input id="docQ" type="search" placeholder="Поиск по документации" aria-label="Поиск по документации" autocomplete="off">
    <span id="docCnt" class="muted small" aria-live="polite"></span>
    <button class="btn ghost sm" id="docPrev" title="Предыдущее (Shift+Enter)" aria-label="Предыдущее совпадение" disabled>↑</button>
    <button class="btn ghost sm" id="docNext" title="Следующее (Enter)" aria-label="Следующее совпадение" disabled>↓</button>
    <span class="spacer" style="flex:1"></span>
    <a class="btn ghost sm" id="docNew" target="_blank" rel="noopener" title="Открыть справку отдельной страницей">↗ <span class="hide-narrow">В новой вкладке</span></a></div>`);
  const frame=h('<iframe class="docs-frame" title="Документация MailArchiver"></iframe>');
  c.appendChild(bar); c.appendChild(frame);
  const page='/static/docs/index.html';
  const v=assetTag();
  const setNew=(a)=>{ $('#docNew',bar).href=page+(a?'#'+a:''); };
  setNew(anchor);
  frame.src=page+(v?'?v='+encodeURIComponent(v):'')+(anchor?'#'+anchor:'');
  const toc=$('#docToc',bar), q=$('#docQ',bar), cnt=$('#docCnt',bar), prev=$('#docPrev',bar), next=$('#docNext',bar);
  let hits=[], cur=-1;
  const show=(i)=>{
    if(!hits.length) return;
    if(hits[cur]) hits[cur].classList.remove('cur');
    cur=(i+hits.length)%hits.length;
    hits[cur].classList.add('cur');
    hits[cur].scrollIntoView({block:'center'});
    cnt.textContent=`${cur+1} из ${hits.length}${hits.length>=1000?'+':''}`;
  };
  frame.onload=()=>{
    let doc; try{ doc=frame.contentDocument; }catch(e){ doc=null; }
    if(!doc || !doc.body) return;
    const style=doc.createElement('style'); style.id='ma-embed';
    doc.head.appendChild(style);
    State.docTheme=()=>{ style.textContent=DOC_EMBED_CSS+(document.documentElement.getAttribute('data-theme')==='dark'?DOC_DARK_CSS:''); };
    State.docTheme();
    // оглавление — из оглавления самой справки (на узком экране оно у неё скрыто)
    const seen=new Set();
    doc.querySelectorAll('.toc a[href^="#"]').forEach(a=>{
      const id=a.getAttribute('href').slice(1), text=a.textContent.trim();
      if(!id || seen.has(id+text)) return; seen.add(id+text);
      const o=document.createElement('option'); o.value=id; o.textContent=text; toc.appendChild(o);
    });
    if(anchor) toc.value=anchor;
    const go=(id)=>{
      const el=id && doc.getElementById(id);
      if(el) el.scrollIntoView({block:'start'});
      history.replaceState(null, '', docsHref(id));
      setNew(id);
    };
    toc.onchange=()=>{ if(toc.value) go(toc.value); };
    // переход по оглавлению внутри справки — адрес раздела запоминает главу (перезагрузка, «Назад»)
    try{ frame.contentWindow.addEventListener('hashchange', ()=>{
      const id=(frame.contentWindow.location.hash||'').slice(1);
      if(State.view!=='docs') return;
      history.replaceState(null, '', docsHref(id)); setNew(id);
      if([...toc.options].some(o=>o.value===id)) toc.value=id;
    }); }catch(e){}
    let t=null;
    q.oninput=()=>{ clearTimeout(t); t=setTimeout(()=>{
      hits=docFind(doc, q.value); cur=-1;
      prev.disabled=next.disabled=!hits.length;
      cnt.textContent = q.value.trim().length<2 ? '' : (hits.length ? '' : 'не найдено');
      if(hits.length) show(0);
    }, 250); };
    q.onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); show(cur+(e.shiftKey?-1:1)); } };
    prev.onclick=()=>show(cur-1); next.onclick=()=>show(cur+1);
    if(params.get('q')){ q.value=params.get('q'); q.oninput(); }
  };
}

const AUDIT_PAGE=200;
async function viewAudit(c){
  // Поиск по пользователю, действию и подробностям и «Показать ещё»: раньше были видны только последние
  // 200 записей без отбора — найти, кто и когда удалил ящик месяц назад, было нельзя.
  let q='', seq=0;
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Аудит действий</h2></div>'));
  const bar=h(`<div class="an-toolbar"><input id="auQ" type="search" aria-label="Поиск в аудите" placeholder="Поиск: пользователь, действие, ящик, адрес…"
    style="width:360px;padding:8px 11px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--bg-elev);color:var(--text)">
    <span class="spacer" style="flex:1"></span><button class="btn sm" id="auVerify" title="Проверить подписи всех записей аудита">🔏 Проверить целостность</button>
    <a class="btn sm ghost" id="auCsv" href="/api/audit/export.csv" download>⬇ CSV</a></div>`);
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th></tr></thead><tbody></tbody></table></div>');
  const more=h('<div style="margin-top:10px;text-align:center"><button class="btn" hidden>Показать ещё</button></div>');
  const tb=wrap.querySelector('tbody'), btn=more.querySelector('button');
  c.appendChild(bar); c.appendChild(wrap); c.appendChild(more);
  const load=async(append)=>{
    const my=++seq;
    const last=append ? tb.querySelector('tr[data-id]:last-child') : null;
    const params=new URLSearchParams({limit:AUDIT_PAGE});
    if(q) params.set('q', q);
    if(last) params.set('before_id', last.dataset.id);
    const rows=await api('/audit?'+params.toString());
    if(my!==seq) return;
    if(!append) tb.innerHTML='';
    rows.forEach(r=>tb.appendChild(h(`<tr data-id="${r.id}"><td class="small muted">${fmtDate(r.ts)}</td><td>${esc(r.user)}</td><td><span class="tag">${esc(r.action)}</span></td><td class="small">${esc(r.detail)}</td></tr>`)));
    if(!tb.children.length) tb.innerHTML=`<tr><td colspan="4" class="empty">${q?'Ничего не найдено':'Записей нет'}</td></tr>`;
    btn.hidden=rows.length<AUDIT_PAGE;
  };
  let t=null;
  bar.querySelector('#auQ').oninput=(e)=>{ clearTimeout(t); t=setTimeout(()=>{ q=e.target.value.trim(); load(false).catch(toastErr); }, 350); };
  btn.onclick=()=>load(true).catch(toastErr);
  bar.querySelector('#auVerify').onclick=async(ev)=>{
    const b=ev.currentTarget; b.disabled=true;
    try{ auditVerifyResult(await api('/audit/verify')); }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
  const csv=bar.querySelector('#auCsv');
  csv.onclick=()=>{ csv.href='/api/audit/export.csv'+(q?'?q='+encodeURIComponent(q):''); };
  await load(false);
}

/** Итог проверки цепочки подписей аудита. */
function auditVerifyResult(r){
  const bad=r.broken||[];
  modal(r.ok?'Аудит цел':'Аудит изменён', `
    ${r.ok?'<div class="hint" style="border-color:var(--success)">✅ Цепочка подписей не нарушена: ни одна запись не изменена, не удалена и не вставлена задним числом.</div>'
      :`<div class="hint" style="border-color:var(--danger)">⛔ Нарушений: ${bad.length}${bad.length>=200?' (показаны первые 200)':''}. Записи аудита меняли в обход приложения.</div>`}
    <div class="kv"><span class="k">Записей</span><span>${fmtNum(r.total)}, проверено подписей: ${fmtNum(r.verified)}</span></div>
    ${r.legacy?`<div class="kv"><span class="k">Без подписи</span><span>${fmtNum(r.legacy)} — записаны версией до 1.20.0</span></div>`:''}
    ${r.other_key?`<div class="kv"><span class="k">Другим ключом</span><span>${fmtNum(r.other_key)} — подписаны прежним файлом секретного ключа, проверить нельзя</span></div>`:''}
    ${r.purged_upto?`<div class="kv"><span class="k">Удалены по сроку</span><span>записи до #${r.purged_upto} (аудит хранится год)</span></div>`:''}
    <div class="kv"><span class="k">Последняя запись</span><span>#${r.head_id} · <code class="small" style="word-break:break-all">${esc(r.head_hash||'—')}</code></span></div>
    <p class="small muted">Сохраняйте подпись последней записи вне сервера (например, в журнале проверок): по ней видно, если записи потом удалят с конца.</p>
    ${bad.length?`<div class="table-wrap"><table class="tbl"><thead><tr><th>Запись</th><th>Время</th><th>Что не так</th></tr></thead><tbody>${bad.map(x=>`<tr><td>#${x.id}</td><td class="small">${x.ts?fmtDate(x.ts):'—'}</td><td>${esc(x.reason)}</td></tr>`).join('')}</tbody></table></div>`:''}`, {wide:true});
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

