/* MailArchiver — веб-интерфейс: «Сервер Axigen» и групповые действия с учётными записями на сервере.
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

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
    <div class="kv"><span class="k">Последняя сверка</span><span>${ls?`${esc(fmtDate(ls.finished_at))} — <span class="badge ${esc(ls.status)}">${esc(STATUS_LBL[ls.status]||ls.status)}</span> <a href="#" data-job="${ls.job_id}" class="small">задание №${ls.job_id}</a><div class="small muted">${esc(ls.summary||'')}</div>`:'не было'}</span></div>
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
    selInfo.innerHTML=selected.length?`Отмечено: <b>${fmtNum(selected.length)}</b> (с ящиком в архиве — ${fmtNum(withBox)}, без ящика — ${fmtNum(noBox)}) · <a href="#" data-clear>снять отметки</a>`:'Отметьте учётные записи флажками, чтобы завести им ящики или выполнить действие.';
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
  if(q('[data-mail]')) q('[data-mail]').onclick=()=>{ m.close(); openMailbox(b.id); };
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
  const m=modal(title, `<p class="small">Новый пароль учётной записи <b>${esc(email)}</b>. Он показывается <b>один раз</b> и нигде не сохраняется (кроме карточки ящика, который копируется по IMAP) — передайте его сотруднику.</p>
    <input type="text" class="mono" readonly value="${esc(password)}" style="width:100%" aria-label="Новый пароль">`);
  const inp=m.body.querySelector('input'); inp.onclick=()=>inp.select();
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
  if((data.running||[]).length) c.appendChild(h(`<div class="hint warnbox" style="margin-bottom:10px">⚙️ Идёт действие с ящиками на сервере: ${data.running.map(j=>`<a href="#" data-job="${j.id}">задание №${j.id}</a>`).join(', ')}. Новое встанет в очередь за ним.</div>`));
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

