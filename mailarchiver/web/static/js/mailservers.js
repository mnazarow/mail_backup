/* MailArchiver — веб-интерфейс: «Почтовые серверы» и запуск интерфейса.
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

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

// ---------- Старт: все файлы интерфейса загружены ----------
boot();
