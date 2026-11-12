/* MailArchiver — веб-интерфейс: «Почта»: настройки клиента (правила, автоответ, метки, шаблоны, адресная книга, уведомления), мгновенная почта, беседы, S/MIME, Sieve.
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// --- окно настроек клиента
const MAIL_SET_TABS=[['ident','✉️ Отправитель'],['rules','⚙️ Правила'],['autoreply','🏖 Автоответ'],['tags','🏷 Метки'],['templates','📄 Шаблоны'],['book','📒 Адресная книга'],['notify','🔔 Уведомления'],['view','👁 Вид']];
function mailSettingsDialog(accId, tab='ident'){
  const m=modal('Настройки почтового клиента', `<div class="tabs mset-tabs" role="tablist">${MAIL_SET_TABS.map(([k,t])=>`<a href="#" role="tab" data-t="${k}">${t}</a>`).join('')}</div><div class="mset-pane"></div>`,
    {wide:true, onClose:()=>{ if(Mail.acc===accId && State.view==='mail'){ api(`/webmail/tags?account_id=${accId}`, {bg:true}).then(r=>{ Mail.tags=r.items||[]; renderMailFolders(accId); renderMailList(accId); }).catch(()=>{}); } }});
  m.el.querySelector('.modal').classList.add('mset');
  const pane=m.body.querySelector('.mset-pane');
  const open=(k)=>{
    m.body.querySelectorAll('[data-t]').forEach(a=>{ a.classList.toggle('active', a.dataset.t===k); a.setAttribute('aria-selected', a.dataset.t===k?'true':'false'); });
    pane.innerHTML='<div class="empty" style="padding:30px"><div class="spinner"></div></div>';
    const fn={ident:mailIdentityPane, rules:mailRulesPane, autoreply:mailAutoreplyPane, tags:mailTagsPane, templates:mailTemplatesPane, book:mailBookPane, notify:mailNotifyPane, view:mailViewPane}[k];
    Promise.resolve().then(()=>fn(pane, accId, m)).catch(e=>{ pane.innerHTML=`<div class="empty small">${esc(e.message||String(e))}</div>`; });
  };
  m.body.querySelectorAll('[data-t]').forEach(a=>a.onclick=(e)=>{ e.preventDefault(); open(a.dataset.t); });
  open(tab);
  return m;
}
/** Кнопка «🏖 Автоответ» у «Проверить почту» — пока автоответ действует. */
function mailArButton(accId){
  const tools=$('#mailTools'); if(!tools) return;
  let b=$('#mailAr', tools);
  if(!(Mail.st && Mail.st.autoreply_active)){ if(b) b.remove(); return; }
  if(b) return;
  b=h(`<button class="btn sm ghost mail-ar-on" id="mailAr" title="Автоответ включён — на новые письма уходит ответ «меня нет на месте»">🏖 Автоответ</button>`);
  b.onclick=()=>mailSettingsDialog(accId, 'autoreply');
  tools.appendChild(b);
}
function mailRoNote(){ return mailCanAct()?'':`<div class="hint mail-ro">🔒 ${esc((Mail.st&&Mail.st.act_problem)||'Менять настройки ящика нельзя.')}</div>`; }

// правила
const RULE_FIELDS=[['from','От'],['to','Кому'],['cc','Копия'],['to_cc','Кому или копия'],['subject','Тема'],['body','Текст письма'],['attach','Есть вложения'],['size','Размер письма, МБ'],['list','Это рассылка'],['priority','Отмечено «Важное»']];
const RULE_OPS=[['contains','содержит'],['not_contains','не содержит'],['equals','равно'],['not_equals','не равно'],['starts','начинается с'],['ends','заканчивается на']];
const RULE_ACTS=[['move','Переместить в папку'],['read','Отметить прочитанным'],['flag','Пометить звёздочкой'],['tag','Поставить метку'],['junk','В спам'],['delete','Удалить (в корзину)']];
function ruleSummary(r){
  const f=Object.fromEntries(RULE_FIELDS), o=Object.fromEntries(RULE_OPS);
  const conds=r.conditions.map(c=>{
    if(['attach','list','priority'].includes(c.field)) return (c.op==='yes'?'':'не ')+f[c.field].toLowerCase();
    if(c.field==='size') return `размер ${c.op==='gt'?'больше':'меньше'} ${c.value} МБ`;
    return `${f[c.field].toLowerCase()} ${o[c.op]} «${c.value}»`; }).join(r.match==='any'?' или ':' и ');
  const tagName=(kw)=>((Mail.tags.find(t=>t.keyword===kw)||{}).name||kw);
  const acts=r.actions.map(a=>a.type==='move'?`в «${a.folder}»`:a.type==='tag'?`метка «${tagName(a.keyword)}»`:(Object.fromEntries(RULE_ACTS)[a.type]||a.type).toLowerCase()).join(', ');
  return `Если ${conds} → ${acts}${r.stop?'; дальше не проверять':''}`;
}
async function mailRulesPane(pane, accId){
  const d=await api(`/webmail/rules?account_id=${accId}`);
  const act=mailCanAct();
  const iv=d.status.interval_min;
  pane.innerHTML=`${mailRoNote()}
    <p class="small muted" style="margin-top:0">Правила раскладывают новые письма «Входящих»: переносят, помечают, ставят метки. ${iv?`Работают и без открытого клиента — клиент проверяет «Входящие» раз в ${iv} мин.`:'Работают, пока открыт клиент (проверка без клиента выключена в настройках сервиса).'}
      ${d.status.last_run_at?`Последняя проверка: ${esc(fmtDate(d.status.last_run_at))}.`:''}</p>
    ${d.status.last_error?`<div class="hint" style="border-color:var(--warn)">⚠️ ${esc(d.status.last_error)}</div>`:''}
    <div class="mail-rules">${d.items.map((r,i)=>`<div class="mail-rule ${r.enabled?'':'off'}" data-id="${r.id}">
      <label class="switch" title="${r.enabled?'Выключить':'Включить'}"><input type="checkbox" data-en ${r.enabled?'checked':''} ${act?'':'disabled'}><span class="track"></span></label>
      <div class="mail-rule-t"><b>${esc(r.name)}</b><div class="small muted">${esc(ruleSummary(r))}</div><div class="small muted">Сработало: ${fmtNum(r.hits)}${r.last_hit_at?`, последний раз ${esc(fmtDate(r.last_hit_at))}`:''}</div></div>
      <span class="btn-row">${act?`<button class="btn sm ghost" data-up ${i?'':'disabled'} title="Выше">↑</button><button class="btn sm ghost" data-down ${i<d.items.length-1?'':'disabled'} title="Ниже">↓</button><button class="btn sm ghost" data-ed title="Изменить">✏️</button><button class="btn sm ghost danger" data-del title="Удалить">🗑</button>`:''}</span></div>`).join('')||'<div class="empty small">Правил пока нет</div>'}</div>
    ${act?'<div class="btn-row mset-foot"><button class="btn primary" data-new>＋ Правило</button><button class="btn" data-apply title="Применить включённые правила к письмам, которые уже лежат во «Входящих»">Применить к «Входящим» сейчас</button></div>':''}
    <h3 class="mset-h">⛔ Заблокированные отправители</h3>
    <div class="mail-rules">${d.blocked.map(r=>`<div class="mail-rule" data-id="${r.id}"><div class="mail-rule-t">${esc((r.conditions[0]||{}).value||r.name)}<div class="small muted">${r.actions[0]&&r.actions[0].type==='junk'?'в «Спам»':'в корзину'} · писем: ${fmtNum(r.hits)}</div></div>${act?'<button class="btn sm ghost" data-del>Разблокировать</button>':''}</div>`).join('')||'<div class="small muted">Никого. Заблокировать отправителя — «⋯» у письма.</div>'}</div>`;
  const reload=()=>mailRulesPane(pane, accId);
  pane.querySelectorAll('.mail-rule').forEach(row=>{
    const id=parseInt(row.dataset.id), r=d.items.find(x=>x.id===id)||d.blocked.find(x=>x.id===id);
    const en=row.querySelector('[data-en]');
    if(en) en.onchange=async()=>{ try{ mailSieveWarn(await api(`/webmail/rules/${id}`, {method:'PUT', body:Object.assign({account_id:accId}, r, {enabled:en.checked})})); reload(); }catch(e){ en.checked=!en.checked; toastErr(e); } };
    const ed=row.querySelector('[data-ed]'); if(ed) ed.onclick=()=>mailRuleEditor(accId, r, null, reload);
    const del=row.querySelector('[data-del]'); if(del) del.onclick=async()=>{
      if(r.kind!=='block' && !await confirmDlg('Удалить правило?', `«${r.name}»`, {okText:'Удалить'})) return;
      try{ mailSieveWarn(await api(`/webmail/rules/${id}?account_id=${accId}`, {method:'DELETE'})); reload(); }catch(e){ toastErr(e); } };
    const mv=async(delta)=>{ const ids=d.items.map(x=>x.id); const i=ids.indexOf(id); ids.splice(i,1); ids.splice(i+delta,0,id);
      try{ await api('/webmail/rules/order', {method:'POST', body:{account_id:accId, ids}}); reload(); }catch(e){ toastErr(e); } };
    const up=row.querySelector('[data-up]'); if(up) up.onclick=()=>mv(-1);
    const dn=row.querySelector('[data-down]'); if(dn) dn.onclick=()=>mv(1);
  });
  const nb=pane.querySelector('[data-new]'); if(nb) nb.onclick=()=>mailRuleEditor(accId, null, null, reload);
  mailSieveSection(pane, accId);
  const ap=pane.querySelector('[data-apply]'); if(ap) ap.onclick=()=>mailFolderAction('rules', {name:Mail.special.inbox||'INBOX'});
}
function mailRuleEditor(accId, rule, preset, onSaved){
  const r=JSON.parse(JSON.stringify(rule||Object.assign({name:'', match:'all', conditions:[{field:'from', op:'contains', value:''}], actions:[{type:'move', folder:''}], stop:false, enabled:true}, preset||{})));
  const folders=Mail.folders.filter(f=>f.on_server!==false && f.selectable);
  const m=modal(rule?'Правило':'Новое правило', `
    <div class="form-row"><label for="re-name">Название</label><input id="re-name" type="text" maxlength="100" value="${esc(r.name)}" placeholder="например, Счета поставщиков"></div>
    <div class="form-row"><label for="re-match">Когда письмо подходит</label><select id="re-match"><option value="all">выполнены все условия</option><option value="any">выполнено любое условие</option></select></div>
    <div class="form-row"><label>Условия</label><div class="re-conds"></div><button class="btn sm ghost" data-addc>＋ условие</button></div>
    <div class="form-row"><label>Что сделать</label><div class="re-acts"></div><button class="btn sm ghost" data-adda>＋ действие</button></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="re-stop" ${r.stop?'checked':''}><span class="track"></span></span><label for="re-stop">Если правило сработало, следующие правила к письму не применять</label></div>
    ${rule?'':'<div class="form-row check"><span class="switch"><input type="checkbox" id="re-now"><span class="track"></span></span><label for="re-now">Сразу применить к письмам во «Входящих»</label></div>'}`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Сохранить</button>'});
  const g=s=>m.body.querySelector(s);
  g('#re-match').value=r.match||'all';
  const opsFor=(field)=>['attach','list','priority'].includes(field)?[['yes','да'],['no','нет']]:(field==='size'?[['gt','больше'],['lt','меньше']]:RULE_OPS);
  const drawConds=()=>{
    const box=g('.re-conds'); box.innerHTML='';
    r.conditions.forEach((c,i)=>{
      const ops=opsFor(c.field); if(!ops.find(o=>o[0]===c.op)) c.op=ops[0][0];
      const noVal=['attach','list','priority'].includes(c.field);
      const row=h(`<div class="re-row"><select data-k="field" aria-label="Поле">${RULE_FIELDS.map(([k,t])=>`<option value="${k}" ${k===c.field?'selected':''}>${t}</option>`).join('')}</select>
        <select data-k="op" aria-label="Сравнение">${ops.map(([k,t])=>`<option value="${k}" ${k===c.op?'selected':''}>${t}</option>`).join('')}</select>
        ${noVal?'<span></span>':`<input data-k="value" type="${c.field==='size'?'number':'text'}" ${c.field==='size'?'min="0" step="0.1"':''} value="${esc(c.value||'')}" aria-label="Значение">`}
        <button class="btn sm ghost" data-rm title="Убрать условие" ${r.conditions.length>1?'':'disabled'}>✕</button></div>`);
      row.querySelector('[data-k="field"]').onchange=(e)=>{ c.field=e.target.value; c.value=''; drawConds(); };
      row.querySelector('[data-k="op"]').onchange=(e)=>{ c.op=e.target.value; };
      const v=row.querySelector('[data-k="value"]'); if(v) v.oninput=()=>{ c.value=v.value; };
      row.querySelector('[data-rm]').onclick=()=>{ r.conditions.splice(i,1); drawConds(); };
      box.appendChild(row);
    });
  };
  const drawActs=()=>{
    const box=g('.re-acts'); box.innerHTML='';
    r.actions.forEach((a,i)=>{
      const row=h(`<div class="re-row"><select data-k="type" aria-label="Действие">${RULE_ACTS.map(([k,t])=>`<option value="${k}" ${k===a.type?'selected':''}>${t}</option>`).join('')}</select>
        ${a.type==='move'?`<select data-k="folder" aria-label="Папка"><option value="">— выберите папку —</option>${folders.map(f=>`<option value="${esc(f.name)}" ${f.name===a.folder?'selected':''}>${esc(f.name)}</option>`).join('')}</select>`:''}
        ${a.type==='tag'?`<select data-k="keyword" aria-label="Метка">${Mail.tags.map(t=>`<option value="${esc(t.keyword)}" ${t.keyword===a.keyword?'selected':''}>${esc(t.name)}</option>`).join('')}</select>`:''}
        <button class="btn sm ghost" data-rm title="Убрать действие" ${r.actions.length>1?'':'disabled'}>✕</button></div>`);
      row.querySelector('[data-k="type"]').onchange=(e)=>{ a.type=e.target.value; if(a.type==='tag' && !a.keyword && Mail.tags[0]) a.keyword=Mail.tags[0].keyword; drawActs(); };
      const f=row.querySelector('[data-k="folder"]'); if(f) f.onchange=()=>{ a.folder=f.value; };
      const t=row.querySelector('[data-k="keyword"]'); if(t){ if(!a.keyword) a.keyword=t.value; t.onchange=()=>{ a.keyword=t.value; }; }
      row.querySelector('[data-rm]').onclick=()=>{ r.actions.splice(i,1); drawActs(); };
      box.appendChild(row);
    });
  };
  drawConds(); drawActs();
  g('[data-addc]').onclick=()=>{ r.conditions.push({field:'subject', op:'contains', value:''}); drawConds(); };
  g('[data-adda]').onclick=()=>{ r.actions.push({type:'read'}); drawActs(); };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{
    const body={account_id:accId, name:g('#re-name').value.trim()||'Правило', match:g('#re-match').value, conditions:r.conditions, actions:r.actions, stop:g('#re-stop').checked, enabled:r.enabled!==false};
    try{
      const saved=await api(rule?`/webmail/rules/${rule.id}`:'/webmail/rules', {method:rule?'PUT':'POST', body});
      m.close(); toast('Правило сохранено', saved.name); mailSieveWarn(saved);
      if(g('#re-now') && g('#re-now').checked) mailFolderAction('rules', {name:Mail.special.inbox||'INBOX'});
      if(onSaved) onSaved();
    }catch(e){ toastErr(e); }
  };
}

// автоответ
async function mailAutoreplyPane(pane, accId){
  const d=await api(`/webmail/autoreply?account_id=${accId}`);
  const act=mailCanAct();
  pane.innerHTML=`${mailRoNote()}
    <p class="small muted" style="margin-top:0">Пока вас нет на месте, на новые письма уходит ответ. Одному отправителю — не чаще раза в несколько дней; рассылкам, роботам и автоматическим письмам — никогда.
      ${Mail.st&&Mail.st.sieve?'Автоответ выполняет <b>почтовый сервер</b> (Sieve, вкладка «Правила»).':d.interval_min?`Работает и без открытого клиента (проверка «Входящих» — раз в ${d.interval_min} мин.).`:'<b>Работает, только пока открыт клиент</b> — проверка без клиента выключена в настройках сервиса.'}</p>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="ar-on" ${d.enabled?'checked':''}><span class="track"></span></span><label for="ar-on">Автоответ включён${d.enabled&&!d.active?' <span class="muted small">(сейчас не действует — вне выбранных дат)</span>':''}</label></div>
    <div class="grid2"><div class="form-row"><label for="ar-from">С даты <span class="muted small">(необязательно)</span></label><input id="ar-from" type="date" value="${esc(d.date_from||'')}"></div>
      <div class="form-row"><label for="ar-to">По дату включительно</label><input id="ar-to" type="date" value="${esc(d.date_to||'')}"></div></div>
    <div class="form-row"><label for="ar-subj">Тема ответа</label><input id="ar-subj" type="text" maxlength="300" value="${esc(d.subject||'')}" placeholder="Пусто — «Автоответ: тема письма»"></div>
    <div class="form-row"><label>Текст ответа</label><div class="compose-body sig" id="ar-body" contenteditable="${act?'true':'false'}" role="textbox" aria-multiline="true" aria-label="Текст автоответа"></div></div>
    <div class="form-row"><label for="ar-days">Одному отправителю — не чаще раза в, дней</label><input id="ar-days" type="number" min="1" max="30" value="${d.days||4}" style="max-width:120px"></div>
    ${act?'<div class="btn-row mset-foot"><button class="btn primary" data-ok>Сохранить</button></div>':''}`;
  const g=s=>pane.querySelector(s);
  g('#ar-body').innerHTML=d.body||'<p>Здравствуйте! Меня нет на месте до … По срочным вопросам пишите …</p>';
  if(!act) pane.querySelectorAll('input').forEach(x=>x.disabled=true);
  const ok=g('[data-ok]'); if(ok) ok.onclick=async()=>{
    const body={account_id:accId, enabled:g('#ar-on').checked, date_from:g('#ar-from').value, date_to:g('#ar-to').value, subject:g('#ar-subj').value, body:g('#ar-body').innerHTML, days:parseInt(g('#ar-days').value||'4')||4};
    try{ const r=await api('/webmail/autoreply', {method:'PUT', body}); toast(r.enabled?'Автоответ включён':'Автоответ выключен', r.enabled&&!r.active?'Начнёт действовать с выбранной даты.':''); mailSieveWarn(r);
      if(Mail.st && Mail.acc===accId){ Mail.st.autoreply_active=r.active; mailArButton(accId); }
      mailAutoreplyPane(pane, accId);
    }catch(e){ toastErr(e); }
  };
}

// метки
async function mailTagsPane(pane, accId){
  const d=await api(`/webmail/tags?account_id=${accId}`);
  const act=mailCanAct();
  pane.innerHTML=`${mailRoNote()}<p class="small muted" style="margin-top:0">Метки хранятся на почтовом сервере (ключевыми словами IMAP): их видно и в других программах — первые пять совпадают с метками Thunderbird.</p>
    <div class="mail-tagedit">${d.items.map(t=>`<div class="re-row" data-id="${t.id}"><input type="color" value="${esc(t.color)}" aria-label="Цвет" ${act?'':'disabled'}><input type="text" maxlength="40" value="${esc(t.name)}" aria-label="Название" ${act?'':'disabled'}><span class="muted small mono">${esc(t.keyword)}</span>${act?'<button class="btn sm" data-save>Сохранить</button><button class="btn sm ghost danger" data-del title="Удалить метку">🗑</button>':''}</div>`).join('')}</div>
    ${act?`<div class="re-row mset-foot"><input type="color" value="#0d9488" id="tg-color" aria-label="Цвет новой метки"><input type="text" id="tg-name" maxlength="40" placeholder="Новая метка" aria-label="Название новой метки"><button class="btn primary" data-add>＋ Добавить</button></div>`:''}`;
  pane.querySelectorAll('[data-id]').forEach(row=>{
    const id=row.dataset.id;
    const sv=row.querySelector('[data-save]'); if(sv) sv.onclick=async()=>{ try{ await api(`/webmail/tags/${id}`, {method:'PUT', body:{account_id:accId, name:row.querySelector('input[type=text]').value, color:row.querySelector('input[type=color]').value}}); toast('Метка сохранена'); }catch(e){ toastErr(e); } };
    const dl=row.querySelector('[data-del]'); if(dl) dl.onclick=async()=>{ if(!await confirmDlg('Удалить метку?', 'С писем она не снимается — просто перестанет показываться.', {okText:'Удалить'})) return;
      try{ await api(`/webmail/tags/${id}?account_id=${accId}`, {method:'DELETE'}); mailTagsPane(pane, accId); }catch(e){ toastErr(e); } };
  });
  const add=pane.querySelector('[data-add]'); if(add) add.onclick=async()=>{
    try{ await api('/webmail/tags', {method:'POST', body:{account_id:accId, name:pane.querySelector('#tg-name').value, color:pane.querySelector('#tg-color').value}}); mailTagsPane(pane, accId); }catch(e){ toastErr(e); } };
}

// шаблоны
async function mailTemplatesPane(pane){
  const d=await api('/webmail/templates');
  pane.innerHTML=`<p class="small muted" style="margin-top:0">Шаблоны — ваши (их не видят другие пользователи). Вставить шаблон — кнопка «📄 Шаблон» в окне письма.</p>
    <div class="mail-rules">${d.items.map(t=>`<div class="mail-rule" data-id="${t.id}"><div class="mail-rule-t"><b>${esc(t.name)}</b><div class="small muted">${esc(t.subject||'без темы')} · ${esc(trunc((h('<div>'+t.body+'</div>').textContent||'').trim(),90))}</div></div>
      <span class="btn-row"><button class="btn sm ghost" data-ed>✏️</button><button class="btn sm ghost danger" data-del>🗑</button></span></div>`).join('')||'<div class="empty small">Шаблонов пока нет</div>'}</div>
    <div class="btn-row mset-foot"><button class="btn primary" data-new>＋ Шаблон</button></div>`;
  const reload=()=>mailTemplatesPane(pane);
  pane.querySelectorAll('[data-id]').forEach(row=>{
    const t=d.items.find(x=>String(x.id)===row.dataset.id);
    row.querySelector('[data-ed]').onclick=()=>mailTemplateEditor(t, reload);
    row.querySelector('[data-del]').onclick=async()=>{ if(!await confirmDlg('Удалить шаблон?', `«${t.name}»`, {okText:'Удалить'})) return;
      try{ await api(`/webmail/templates/${t.id}`, {method:'DELETE'}); reload(); }catch(e){ toastErr(e); } };
  });
  pane.querySelector('[data-new]').onclick=()=>mailTemplateEditor(null, reload);
}
function mailTemplateEditor(t, onSaved){
  const m=modal(t&&t.id?'Шаблон':'Новый шаблон', `<div class="form-row"><label for="te-name">Название</label><input id="te-name" type="text" maxlength="100" value="${esc((t&&t.name)||'')}"></div>
    <div class="form-row"><label for="te-subj">Тема <span class="muted small">(подставляется, если у письма темы нет)</span></label><input id="te-subj" type="text" maxlength="998" value="${esc((t&&t.subject)||'')}"></div>
    <div class="form-row"><label>Текст</label><div class="compose-body sig" id="te-body" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Текст шаблона"></div></div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Сохранить</button>'});
  const g=s=>m.body.querySelector(s);
  g('#te-body').innerHTML=(t&&t.body)||'';
  g('#te-body').addEventListener('paste', (e)=>{ const html=e.clipboardData&&e.clipboardData.getData('text/html'); if(html){ e.preventDefault(); document.execCommand('insertHTML', false, composeClean(html)); } });
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{
    const body={name:g('#te-name').value.trim(), subject:g('#te-subj').value, body:g('#te-body').innerHTML};
    if(!body.name) return toastErr({message:'Укажите название шаблона.'});
    try{ await api(t&&t.id?`/webmail/templates/${t.id}`:'/webmail/templates', {method:t&&t.id?'PUT':'POST', body}); m.close(); toast('Шаблон сохранён', body.name); if(onSaved) onSaved(); }
    catch(e){ toastErr(e); }
  };
}

// адресная книга
async function mailBookPane(pane, accId, m, state={q:'', group:''}){
  const d=await api(`/webmail/book?${new URLSearchParams({account_id:String(accId), q:state.q, group:state.group})}`);
  const act=mailCanAct();
  pane.innerHTML=`${mailRoNote()}
    <div class="mail-book-bar"><input type="search" data-q placeholder="Найти: имя, адрес, организация" value="${esc(state.q)}" aria-label="Поиск в адресной книге">
      <select data-g aria-label="Группа"><option value="">Все группы</option>${d.groups.map(g=>`<option value="${esc(g.name)}" ${g.name===state.group?'selected':''}>${esc(g.name)} (${g.count})</option>`).join('')}</select>
      ${act?'<button class="btn sm primary" data-new>＋ Контакт</button><label class="btn sm" title="Файл .vcf (vCard) или .csv: Outlook, Google, Thunderbird">⬆ Импорт<input type="file" accept=".vcf,.vcard,.csv,text/vcard,text/csv" hidden data-imp></label>':''}
      <a class="btn sm ghost" href="/api/webmail/book/export?account_id=${accId}&fmt=vcf" download>⬇ vCard</a><a class="btn sm ghost" href="/api/webmail/book/export?account_id=${accId}&fmt=csv" download>⬇ CSV</a></div>
    ${state.group&&d.items.length&&mailCanSend()?`<div class="btn-row" style="margin:6px 0"><button class="btn sm" data-wgroup>✉️ Написать группе «${esc(state.group)}» (${d.items.length})</button></div>`:''}
    <div class="mail-book">${d.items.map(c=>`<div class="mail-book-row" data-id="${c.id}"><div><b>${esc(c.name||c.email)}</b>${c.name?` <span class="muted small">${esc(c.email)}</span>`:''}
        <div class="small muted">${[c.company, c.phone, c.groups?'группы: '+c.groups:''].filter(Boolean).map(esc).join(' · ')}</div></div>
      <span class="btn-row">${mailCanSend()?'<button class="btn sm ghost" data-w title="Написать">✉️</button>':''}${act?'<button class="btn sm ghost" data-ed title="Изменить">✏️</button><button class="btn sm ghost danger" data-del title="Удалить">🗑</button>':''}</span></div>`).join('')||'<div class="empty small">Адресов нет. Добавьте вручную, импортируйте файл или «⋯ → Добавить отправителя» у письма.</div>'}</div>`;
  const reload=()=>mailBookPane(pane, accId, m, state);
  const q=pane.querySelector('[data-q]'); let t=null;
  q.oninput=()=>{ clearTimeout(t); t=setTimeout(()=>{ state.q=q.value.trim(); reload().then(()=>{ const n=pane.querySelector('[data-q]'); if(n){ n.focus(); n.setSelectionRange(n.value.length, n.value.length); } }); }, 300); };
  pane.querySelector('[data-g]').onchange=(e)=>{ state.group=e.target.value; reload(); };
  const write=(to)=>{ if(m) m.close(); mailCompose('new', null, {to_addrs:to}); };
  const wg=pane.querySelector('[data-wgroup]'); if(wg) wg.onclick=()=>write(d.items.map(c=>c.name?`${c.name} <${c.email}>`:c.email).join(', '));
  pane.querySelectorAll('[data-id]').forEach(row=>{
    const c=d.items.find(x=>String(x.id)===row.dataset.id);
    const w=row.querySelector('[data-w]'); if(w) w.onclick=()=>write(c.name?`${c.name} <${c.email}>`:c.email);
    const ed=row.querySelector('[data-ed]'); if(ed) ed.onclick=()=>mailContactEditor(accId, c, reload);
    const dl=row.querySelector('[data-del]'); if(dl) dl.onclick=async()=>{ if(!await confirmDlg('Удалить из адресной книги?', c.email, {okText:'Удалить'})) return;
      try{ await api('/webmail/book/delete', {method:'POST', body:{account_id:accId, ids:[c.id]}}); reload(); }catch(e){ toastErr(e); } };
  });
  const nb=pane.querySelector('[data-new]'); if(nb) nb.onclick=()=>mailContactEditor(accId, null, reload);
  const imp=pane.querySelector('[data-imp]'); if(imp) imp.onchange=async()=>{
    const f=imp.files[0]; if(!f) return;
    const fd=new FormData(); fd.append('account_id', String(accId)); fd.append('file', f);
    try{ const r=await api('/webmail/book/import', {method:'POST', body:fd}); toast('Импорт завершён', `Добавлено: ${fmtNum(r.added)}, обновлено: ${fmtNum(r.updated)}${r.skipped?`, пропущено: ${fmtNum(r.skipped)}`:''}`); reload(); }
    catch(e){ toastErr(e); }
  };
}
function mailContactEditor(accId, c, onSaved){
  const v=c||{name:'', email:'', phone:'', company:'', groups:'', note:''};
  const m=modal(c?'Контакт':'Новый контакт', `<div class="grid2"><div class="form-row"><label for="ce-name">Имя</label><input id="ce-name" type="text" maxlength="200" value="${esc(v.name)}"></div>
      <div class="form-row"><label for="ce-mail">Адрес</label><input id="ce-mail" type="text" inputmode="email" maxlength="300" value="${esc(v.email)}"></div></div>
    <div class="grid2"><div class="form-row"><label for="ce-phone">Телефон</label><input id="ce-phone" type="text" maxlength="100" value="${esc(v.phone)}"></div>
      <div class="form-row"><label for="ce-org">Организация</label><input id="ce-org" type="text" maxlength="200" value="${esc(v.company)}"></div></div>
    <div class="form-row"><label for="ce-groups">Группы <span class="muted small">(через запятую)</span></label><input id="ce-groups" type="text" maxlength="1000" value="${esc(v.groups)}" placeholder="например, Поставщики, Отдел продаж"><div class="hint">Имя группы в поле «Кому» подставляет все её адреса.</div></div>
    <div class="form-row"><label for="ce-note">Заметка</label><textarea id="ce-note" rows="2" maxlength="2000">${esc(v.note)}</textarea></div>`,
    {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Сохранить</button>'});
  const g=s=>m.body.querySelector(s);
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{
    const body={account_id:accId, name:g('#ce-name').value.trim(), email:g('#ce-mail').value.trim(), phone:g('#ce-phone').value.trim(), company:g('#ce-org').value.trim(), groups:g('#ce-groups').value, note:g('#ce-note').value};
    try{ await api(c?`/webmail/book/${c.id}`:'/webmail/book', {method:c?'PUT':'POST', body}); m.close(); toast('Сохранено', body.email); if(onSaved) onSaved(); }catch(e){ toastErr(e); }
  };
}

// вид
async function mailViewPane(pane, accId){
  const p=mailPrefs();
  pane.innerHTML=`<p class="small muted" style="margin-top:0">Эти настройки — только для этого браузера.</p>
    <div class="form-row"><label for="mv-limit">Писем на странице</label><select id="mv-limit" style="max-width:160px">${[25,50,100,200].map(n=>`<option value="${n}" ${n===p.limit?'selected':''}>${n}</option>`).join('')}</select></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="mv-compact" ${p.compact?'checked':''}><span class="track"></span></span><label for="mv-compact">Компактный список писем</label></div>
    <div class="hint">Число непрочитанных писем «Входящих» видно в заголовке вкладки. Уведомления о новой почте — на вкладке «🔔 Уведомления».</div>`;
  const g=s=>pane.querySelector(s);
  const apply=()=>{ mailSavePrefs(p); Mail.limit=p.limit; Mail.offset=0; const lay=$('.mail-layout'); if(lay) lay.classList.toggle('compact', !!p.compact); if(Mail.acc===accId && !Mail.search && Mail.view==='folder') loadMailMessages(accId); };
  g('#mv-limit').onchange=(e)=>{ p.limit=parseInt(e.target.value)||50; apply(); };
  g('#mv-compact').onchange=(e)=>{ p.compact=e.target.checked; apply(); };
}

// ---------- Уведомления о новой почте (1.22.0): во вкладке и на устройство (Web Push) ----------
function mailPushSupported(){ return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window) && window.isSecureContext; }
function b64uBytes(s){ s=String(s||'').replace(/-/g,'+').replace(/_/g,'/'); s+='='.repeat((4-s.length%4)%4); const b=atob(s); return Uint8Array.from(b, ch=>ch.charCodeAt(0)); }
function b64uOf(buf){ if(!buf) return ''; const b=new Uint8Array(buf); let t=''; b.forEach(x=>{ t+=String.fromCharCode(x); }); return btoa(t).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,''); }
async function mailPushCurrent(){
  if(!mailPushSupported()) return null;
  try{ const reg=await navigator.serviceWorker.getRegistration('/'); return reg ? await reg.pushManager.getSubscription() : null; }catch(e){ return null; }
}
/** Подписать это устройство (или обновить список ящиков). */
async function mailPushOn(publicKey, ids){
  if(Notification.permission!=='granted'){
    const r=await Notification.requestPermission().catch(()=>'denied');
    if(r!=='granted') throw new Error('Браузер не разрешил уведомления для этого сайта — разрешите их в настройках сайта (значок замка в адресной строке).');
  }
  const reg=await navigator.serviceWorker.register('/sw.js', {scope:'/'});
  await navigator.serviceWorker.ready;
  let sub=await reg.pushManager.getSubscription();
  // подписка на прежний ключ сервера (ключ сменился) не годится — переподписаться
  if(sub && b64uOf(sub.options && sub.options.applicationServerKey)!==publicKey){ await sub.unsubscribe().catch(()=>{}); sub=null; }
  if(!sub) sub=await reg.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:b64uBytes(publicKey)});
  const j=sub.toJSON();
  return api('/push/subscribe', {method:'POST', body:{endpoint:j.endpoint, keys:j.keys, account_ids:ids}});
}
async function mailPushOff(){
  const sub=await mailPushCurrent(); if(!sub) return;
  try{ await api('/push/unsubscribe', {method:'POST', body:{endpoint:sub.endpoint}, bg:true}); }catch(e){ /* сервер недоступен — браузер всё равно отпишется */ }
  await sub.unsubscribe().catch(()=>{});
}
async function mailNotifyPane(pane, accId){
  const p=mailPrefs();
  const perm=('Notification' in window)?Notification.permission:'unsupported';
  pane.innerHTML=`<h4 style="margin-top:0">Пока «Почта» открыта</h4>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="mv-notify" ${p.notify&&perm==='granted'?'checked':''} ${perm==='unsupported'||perm==='denied'?'disabled':''}><span class="track"></span></span><label for="mv-notify">Уведомлять о новой почте, когда вкладка в фоне</label></div>
    ${perm==='denied'?'<div class="hint">Уведомления для этого сайта запрещены в браузере — разрешите их в настройках сайта (значок замка в адресной строке).</div>':''}
    ${perm==='unsupported'?'<div class="hint">Этот браузер не умеет показывать уведомления (или страница открыта не по https).</div>':''}
    <div class="hint">Новая почта проверяется раз в ${fmtNum((Mail.st&&Mail.st.auto_refresh_s)||0)} с (или сразу, если сервер умеет IMAP IDLE), пока открыта «Почта».</div>
    <h4>На этом устройстве, даже когда «Почта» закрыта</h4><div id="wp-box"><div class="spinner"></div></div>`;
  pane.querySelector('#mv-notify').onchange=async(e)=>{
    if(e.target.checked && Notification.permission!=='granted'){
      const r=await Notification.requestPermission().catch(()=>'denied');
      if(r!=='granted'){ e.target.checked=false; toast('Уведомления не разрешены', 'Браузер не дал разрешения.', 'warn'); return; }
    }
    p.notify=e.target.checked; mailSavePrefs(p);
  };
  const box=pane.querySelector('#wp-box');
  if(!isMailbox()){ box.innerHTML='<div class="hint">Уведомления о новых письмах на компьютер и телефон включает сам сотрудник в своей «Почте».</div>'; return; }
  const draw=async()=>{
    const sub=await mailPushCurrent();
    let st;
    try{ st=await api('/push/state', {method:'POST', body:{endpoint:sub?sub.endpoint:''}}); }
    catch(e){ box.innerHTML=`<div class="hint">${esc(e.message)}</div>`; return; }
    if(!st.enabled){ box.innerHTML='<div class="hint">Выключено администратором («Настройки → Почтовый клиент → Уведомления на компьютер и телефон»).</div>'; return; }
    if(!mailPushSupported()){
      box.innerHTML=`<div class="hint">${window.isSecureContext?'Этот браузер не умеет получать уведомления сайтов в фоне. На iPhone и iPad: «Поделиться → На экран «Домой»», затем откройте «Почту» с экрана «Домой».':'Нужен адрес https:// — по http браузер уведомления в фоне не принимает (спросите администратора).'}</div>`; return;
    }
    const mine=myAccounts();
    const chosen=st.subscribed?new Set(st.account_ids):new Set(mine.filter(a=>a.rights!=='read').map(a=>a.id));
    box.innerHTML=`<div class="form-row check"><span class="switch"><input type="checkbox" id="wp-on" ${st.subscribed?'checked':''}><span class="track"></span></span><label for="wp-on">Уведомлять о новых письмах на этом устройстве</label></div>
      ${mine.length>1?`<div class="form-row"><label>Ящики</label><div class="wp-accs">${mine.map(a=>`<label><input type="checkbox" class="wp-acc" value="${a.id}" ${chosen.has(a.id)?'checked':''}> ${esc(a.name)}${a.rights==='read'?' <span class="muted small">(только чтение)</span>':''}</label>`).join('')}</div></div>`:''}
      <div class="hint">Уведомление приходит о непрочитанном письме во «Входящих»${st.preview?' и показывает отправителя и тему':' (без отправителя и темы — так настроено администратором)'}. Щелчок открывает письмо.</div>
      <div class="btn-row" style="margin-top:8px">${st.subscribed?'<button class="btn sm ghost" id="wp-test">Прислать проверочное</button>':''}</div>
      ${st.devices.length?`<h4>Устройства с уведомлениями</h4><table class="tbl small"><tbody>${st.devices.map(d=>`<tr><td>${esc(d.device)}${d.this?' <span class="badge">это устройство</span>':''}</td><td class="muted">${d.last_error?'⚠️ '+esc(d.last_error):(d.last_ok_at?'последнее — '+fmtDate(d.last_ok_at):'с '+fmtDate(d.created_at))}</td><td>${d.this?'':`<button class="btn sm ghost" data-forget="${d.id}">Забыть</button>`}</td></tr>`).join('')}</tbody></table>`:''}`;
    const ids=()=>mine.length>1?[...box.querySelectorAll('.wp-acc:checked')].map(x=>parseInt(x.value)):mine.map(a=>a.id);
    const on=box.querySelector('#wp-on');
    on.onchange=async()=>{
      on.disabled=true;
      try{
        if(on.checked){ await mailPushOn(st.public_key, ids()); toast('Уведомления включены', 'О новых письмах это устройство узнает, даже когда «Почта» закрыта.'); }
        else{ await mailPushOff(); toast('Уведомления выключены', 'На это устройство уведомления больше не придут.'); }
      }catch(e){ toastErr(e); }
      draw();
    };
    box.querySelectorAll('.wp-acc').forEach(cb=>cb.onchange=async()=>{
      if(!on.checked) return;
      if(!ids().length){ cb.checked=true; return toast('Нужен хотя бы один ящик', 'Чтобы не получать уведомлений совсем, выключите их переключателем.', 'warn'); }
      try{ await mailPushOn(st.public_key, ids()); }catch(e){ toastErr(e); }
    });
    const t=box.querySelector('#wp-test');
    if(t) t.onclick=async()=>{ t.disabled=true; try{ await api('/push/test', {method:'POST', body:{endpoint:sub.endpoint}}); toast('Отправлено', 'Уведомление должно появиться через несколько секунд.'); }catch(e){ toastErr(e); } t.disabled=false; };
    box.querySelectorAll('[data-forget]').forEach(b=>b.onclick=async()=>{ try{ await api(`/push/devices/${b.dataset.forget}`, {method:'DELETE'}); }catch(e){ toastErr(e); } draw(); });
  };
  draw();
}
/** Щелчок по уведомлению при открытом интерфейсе: обработчик уведомлений присылает адрес письма. */
function mailPushListen(){
  if(!('serviceWorker' in navigator)) return;
  try{
    navigator.serviceWorker.addEventListener('message', (e)=>{
      const d=e.data||{};
      if(d.type==='ma-open' && typeof d.url==='string' && d.url.startsWith('/#/')) location.hash=d.url.slice(1);
    });
    navigator.serviceWorker.startMessages();
  }catch(e){ /* браузер без обработчиков */ }
}
mailPushListen();

// ---------- Почтовый клиент 1.19.0: новая почта сразу, поиски, квота, подлинность, фишинг, Sieve ----------
/** Ждать новую почту: сервер держит IMAP IDLE и отвечает, как только письмо пришло (или через 25 с). */
async function mailPushLoop(accId){
  if(!Mail.st || !Mail.st.push || !mailOnline()) return;
  const token=++Mail.pushSeq, seq=State.routeSeq;
  let since=-1, fails=0;
  while(token===Mail.pushSeq && seq===State.routeSeq && State.view==='mail' && Mail.acc===accId){
    let r;
    try{ r=await api(`/webmail/wait?account_id=${accId}&since=${since}`, {bg:true}); fails=0; }
    catch(e){ if(++fails>5 || e.status===401 || e.status===403) return; await new Promise(res=>setTimeout(res, 10000)); continue; }
    if(token!==Mail.pushSeq || Mail.acc!==accId) return;
    if(['off','unsupported','limit'].includes(r.push)) return;      // остаётся проверка по таймеру
    if(r.push==='error'){ await new Promise(res=>setTimeout(res, 30000)); }
    if(r.changed && since>=0){
      await loadMailFolders(accId, {quiet:true});
      const inbox=Mail.special.inbox;
      if(Mail.folder===inbox && !Mail.search && Mail.view==='folder') loadMailMessages(accId, {keep:true});
    }
    since=r.version;
  }
}

// сохранённые поиски
async function mailSaveSearch(accId){
  if(!Mail.search) return;
  const name=window.prompt(t('Название поиска'), Mail.search.q.slice(0,60)); if(name===null) return;
  try{ await api('/webmail/searches', {method:'POST', body:{name:name.trim(), q:Mail.search.q, scope:Mail.search.scope||'account', attach:!!Mail.search.attach}});
    toast('Поиск сохранён', 'Он — под папками, в «Поисках».'); await mailLoadSearches(); renderMailFolders(accId); }
  catch(e){ toastErr(e); }
}
async function mailLoadSearches(){ try{ Mail.searches=(await api('/webmail/searches', {bg:true})).items||[]; }catch(e){ Mail.searches=[]; } }
function mailRenderSaved(accId, box){
  if(!Mail.searches.length) return;
  box.appendChild(h('<div class="mail-folders-sep">🔎 Поиски</div>'));
  Mail.searches.forEach(s=>{
    const active=!!(Mail.search && Mail.search.q===s.q);
    const el=h(`<div class="mail-folder ${active?'active':''}" tabindex="0" role="button" title="${esc(s.q)}${s.scope==='all'?' — во всех ящиках':''}${s.attach?' — с вложениями':''}"><span>🔎</span><span class="fname">${esc(s.name)}</span><button class="fmenu" data-del title="Удалить поиск" aria-label="Удалить поиск">×</button></div>`);
    el.onclick=(e)=>{ if(e.target.closest('[data-del]')) return;
      const q=$('#msq'); if(!q) return; q.value=s.q;
      const sc=$('#msScope'); if(sc) sc.value=s.scope; const at=$('#msAttach'); if(at) at.checked=!!s.attach;
      $('#msGo').click(); renderMailFolders(accId); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); el.click(); } };
    el.querySelector('[data-del]').onclick=async(e)=>{ e.stopPropagation();
      try{ await api(`/webmail/searches/${s.id}`, {method:'DELETE'}); await mailLoadSearches(); renderMailFolders(accId); }catch(err){ toastErr(err); } };
    box.appendChild(el);
  });
}
// квота
async function mailLoadQuota(accId){
  if(!mailOnline()) return;
  try{ const q=await api(`/webmail/quota?account_id=${accId}`, {bg:true}); if(Mail.acc===accId){ Mail.quota=Object.assign(q||{}, {_acc:accId}); renderMailFolders(accId); } }catch(e){ /* квота — необязательна */ }
}
function mailRenderQuota(box){
  const q=Mail.quota; if(!q || !q.limit) return;
  const pct=Math.min(100, q.percent||0);
  box.appendChild(h(`<div class="mail-quota ${pct>=90?'full':pct>=75?'warn':''}" title="Квота ящика на почтовом сервере"><div class="bar"><span style="width:${pct}%"></span></div>
    <div class="small muted">Занято ${esc(fmtBytes(q.used))} из ${esc(fmtBytes(q.limit))} (${fmtNum(Math.round(pct))}%)</div></div>`));
}

// подлинность отправителя, S/MIME
function mailRenderTrust(el, accId, pk, x, act){
  const tags=el.querySelector('#mfromtags'), box=el.querySelector('#mextras');
  const c=x.check||{}, a=c.auth||{}, sm=x.smime;
  const parts=[];
  if(a.verdict==='pass') parts.push(`<span class="rp-tag ok" title="Почтовый сервер подтвердил отправителя: SPF ${esc(a.spf||'—')}, DKIM ${esc(a.dkim||'—')}, DMARC ${esc(a.dmarc||'—')}">✔ подтверждён</span>`);
  else if(a.verdict==='fail') parts.push(`<span class="rp-tag hot" title="SPF ${esc(a.spf||'—')}, DKIM ${esc(a.dkim||'—')}, DMARC ${esc(a.dmarc||'—')}">⚠ не подтверждён</span>`);
  if(c.external) parts.push('<span class="rp-tag ext" title="Письмо пришло не с адресов вашей организации">внешний</span>');
  if(c.first_time) parts.push('<span class="rp-tag" title="Раньше писем с этого адреса в ящике не было">впервые пишет</span>');
  if(sm && sm.kind==='signed') parts.push(`<span class="rp-tag ${sm.valid&&sm.trusted?'ok':sm.valid?'':'hot'}" title="${esc(sm.text||'')}">${sm.valid?'🔏 подпись верна':sm.valid===false?'🔏 подпись неверна':'🔏 подписано'}</span>`);
  if(sm && sm.kind==='encrypted') parts.push(`<span class="rp-tag" title="${esc(sm.text||'')}">🔒 зашифровано</span>`);
  if(tags) tags.innerHTML=parts.join(' ');
  if(!box) return;
  const warns=c.warnings||[];
  if(warns.length){
    const danger=warns.some(w=>w.level==='danger');
    const b=h(`<div class="mail-trust ${danger?'danger':''}"><div class="mail-trust-t">${danger?'⚠️ Письмо может быть поддельным':'⚠️ Осторожно'}</div>
      <ul>${warns.map(w=>`<li>${esc(w.text)}</li>`).join('')}</ul>
      <div class="small muted">Не переходите по ссылкам, не открывайте вложения и не отвечайте, пока не убедитесь, что письмо настоящее (например, позвонив отправителю).</div>
      ${act?`<div class="btn-row" style="margin-top:8px"><button class="btn sm danger" data-phish>🎣 Сообщить о фишинге</button></div>`:''}</div>`);
    const ph=b.querySelector('[data-phish]'); if(ph) ph.onclick=()=>mailReportPhish(accId, [pk]);
    box.prepend(b);
  }
  if(sm && sm.kind==='signed' && sm.signer && sm.signer.name){
    box.appendChild(h(`<div class="small muted mail-smime">🔏 ${esc(sm.text||'')} Подписал: ${esc(sm.signer.name)}${(sm.signer.emails||[]).length?` &lt;${esc(sm.signer.emails.join(', '))}&gt;`:''}${sm.signer.issuer?`, выдан: ${esc(sm.signer.issuer)}`:''}${sm.signer.expired?' — сертификат истёк':''}.${sm.signer.fingerprint?` <span class="mono" title="Отпечаток SHA-256 сертификата">${esc(sm.signer.fingerprint.slice(0,16).toUpperCase().replace(/(.{4})(?=.)/g,'$1 '))}</span>`:''}${sm.matches_sender===false?' <b style="color:var(--warn-text)">⚠ Адрес отправителя не совпадает с сертификатом.</b>':''}</div>`));
  }else if(sm && sm.text && sm.kind!=='signed'){
    const b=h(`<div class="small muted mail-smime">🔒 ${esc(sm.text)}${sm.can_decrypt?' <button class="btn sm" data-decrypt>🔓 Расшифровать</button>':''}</div>`);
    const dbtn=b.querySelector('[data-decrypt]'); if(dbtn) dbtn.onclick=()=>mailShowDecrypted(el, accId, pk, dbtn);
    box.appendChild(b);
  }
}
/** Расшифровать письмо S/MIME (на сервере, без сохранения) и показать его текст и вложения. */
async function mailShowDecrypted(el, accId, pk, btn){
  if(btn) btn.disabled=true;
  let m; try{ m=await api(`/webmail/decrypted?account_id=${accId}&id=${pk}`); }catch(e){ if(btn) btn.disabled=false; return toastErr(e); }
  const body=el.querySelector('#mbody'); if(!body) return;
  const atts=(m.attachments||[]).filter(a=>!a.inline);
  const url=a=>`/api/webmail/decrypted/attachment?account_id=${accId}&id=${pk}&index=${a.index}`;
  body.innerHTML='';
  body.appendChild(h(`<div class="hint" style="margin-bottom:8px">🔓 Расшифровано для просмотра. На сервере расшифрованный текст не сохраняется.</div>`));
  if(atts.length) body.appendChild(h(`<div class="mail-atts">${atts.map(a=>`<a class="attach-chip" href="${url(a)}" download>📎 ${esc(a.filename)} <span class="muted">(${fmtBytes(a.size)})</span></a>`).join('')}</div>`));
  if(m.html && m.html.trim()){
    const f=document.createElement('iframe'); f.className='mail-body-frame';
    f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox'); f.setAttribute('referrerpolicy','no-referrer');
    f.srcdoc=mailSrcdoc(m.html, false).srcdoc; body.appendChild(f);
  }else body.appendChild(h(`<div class="mail-body-text">${esc(m.text||'(пустое тело)')}</div>`));
  if(btn) btn.remove();
}
async function mailReportPhish(accId, ids){
  const to=Mail.st && Mail.st.phishing_report_to;
  if(!await confirmDlg('Сообщить о фишинге?', `${to?'Письмо целиком будет отправлено в службу безопасности и ':'Письмо '}перенесено в «Спам».`, {okText:'Сообщить', okClass:'danger'})) return;
  let r; try{ r=await api('/webmail/phishing', {method:'POST', body:{account_id:accId, ids}}); }catch(e){ return toastErr(e); }
  toast('Спасибо', `${r.reported?`Письмо отправлено на ${r.to}. `:''}${r.moved?'Перенесено в «Спам».':''}`);
  if(Mail.cur && ids.includes(Mail.cur)){ Mail.cur=null; const rd=$('#mreader'); if(rd) rd.innerHTML='<div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div>'; }
  await loadMailFolders(accId, {quiet:true}); if(!Mail.search) loadMailMessages(accId, {keep:true});
}

// справка по клавишам
function mailShortcutsHelp(){
  const rows=[['j / k','следующее / предыдущее письмо'],['Enter','открыть письмо'],['пробел','выбрать письмо в списке'],['/','поиск'],
    ['c','новое письмо'],['r / a / f','ответить / всем / переслать'],['s','пометить звёздочкой'],['u','прочитано / не прочитано'],
    ['e','в архив'],['v','переместить в папку'],['l','метка'],['b','напомнить позже'],['Delete, #','удалить'],
    ['Ctrl+Enter','отправить (в окне письма)'],['Ctrl+S','сохранить черновик'],['?','эта справка']];
  modal('Горячие клавиши почты', `<table class="t mail-keys">${rows.map(([k,t])=>`<tr><td>${k.split(' / ').map(x=>`<kbd>${esc(x)}</kbd>`).join(' / ')}</td><td>${esc(t)}</td></tr>`).join('')}</table>
    <div class="hint">Клавиши работают и в русской раскладке (например, «у» — в архив).</div>`);
}

// правила на сервере (Sieve)
async function mailSieveSection(pane, accId){
  let d; try{ d=await api(`/webmail/sieve?account_id=${accId}`); }catch(e){ return; }
  const act=mailCanAct();
  const sec=h(`<div class="mail-sieve"><h3 class="mset-h">🖥 Правила на почтовом сервере (Sieve)</h3>
    <p class="small muted" style="margin-top:0">Правила и автоответ можно отдать самому почтовому серверу (Dovecot, Cyrus, Stalwart и другие с ManageSieve): письма будут раскладываться сразу при получении — и в почте на телефоне, даже если служба архива остановлена. Exchange, Gmail и Яндекс Sieve не поддерживают.</p>
    ${d.supported?'':'<div class="hint">Доступно ящикам с паролем: ящику с OAuth или входом через администратора почты сервер Sieve не откроет.</div>'}
    ${d.error?`<div class="hint" style="border-color:var(--warn)">⚠️ ${esc(d.error)}</div>`:''}
    ${d.enabled&&d.synced_at?`<div class="hint">✔ Правила выполняет почтовый сервер (загружены ${esc(fmtDate(d.synced_at))}); служба архива их сама не выполняет. Изменили правило — сценарий на сервере обновится сам.</div>`:''}
    <div class="form-row check"><span class="switch"><input type="checkbox" id="sv-on" ${d.enabled?'checked':''} ${act&&d.supported?'':'disabled'}><span class="track"></span></span><label for="sv-on">Выполнять правила и автоответ на почтовом сервере</label></div>
    <div class="grid2"><div class="form-row"><label for="sv-host">Сервер ManageSieve</label><input id="sv-host" type="text" value="${esc(d.host||'')}" ${act?'':'disabled'}></div>
      <div class="form-row"><label for="sv-port">Порт</label><input id="sv-port" type="number" min="1" max="65535" value="${d.port||4190}" ${act?'':'disabled'}></div></div>
    <div class="form-row"><label for="sv-sec">Защита</label><select id="sv-sec" ${act?'':'disabled'}><option value="starttls">STARTTLS</option><option value="ssl">SSL</option><option value="none">без шифрования (только localhost)</option></select></div>
    <details><summary class="small">Сценарий Sieve, который будет загружен</summary><pre class="mail-headers">${esc(d.script||'')}</pre></details>
    ${act&&d.supported?'<div class="btn-row mset-foot"><button class="btn primary" data-sv>Сохранить</button></div>':''}</div>`);
  sec.querySelector('#sv-sec').value=d.security||'starttls';
  const btn=sec.querySelector('[data-sv]');
  const send=async(force)=>{
    const body={account_id:accId, enabled:sec.querySelector('#sv-on').checked, host:sec.querySelector('#sv-host').value.trim(),
      port:parseInt(sec.querySelector('#sv-port').value||'4190')||4190, security:sec.querySelector('#sv-sec').value, force:!!force};
    try{ const r=await api('/webmail/sieve', {method:'PUT', body}); toast(r.enabled?'Правила загружены на сервер':'Правила на сервере выключены', r.enabled?`${r.host}:${r.port}`:'');
      if(Mail.st) Mail.st.sieve=!!(r.enabled&&r.synced_at); mailRulesPane(pane, accId); }
    catch(e){
      if(e.data && e.data.code==='sieve_conflict' && await confirmDlg('Заменить сценарий на сервере?', e.message+' Сценарий клиента станет активным, прежний останется на сервере неактивным.', {okText:'Заменить', okClass:'primary'})) return send(true);
      toastErr(e);
    }
  };
  if(btn) btn.onclick=()=>send(false);
  pane.appendChild(sec);
}
function mailSieveWarn(r){ if(r && r.sieve_error) toast('Правила на сервере не обновлены', r.sieve_error, 'warn', 8000); }
// ---------- Горячие клавиши «Почты» ----------
document.addEventListener('keydown', (e)=>{ if(e.key==='Escape') document.querySelectorAll('.mail-pop').forEach(x=>x.remove()); });
document.addEventListener('keydown', (e)=>{
  if(State.view!=='mail' || e.ctrlKey || e.metaKey || e.altKey) return;
  const t=e.target;
  if(t && (t.closest('input,textarea,select,[contenteditable="true"],.compose,.modal-back'))) return;
  if(document.querySelector('.modal-back')) return;
  const k=e.key;
  const list=$('#mlist');
  if(k==='/'){ const q=$('#msq'); if(q){ e.preventDefault(); q.focus(); } return; }
  if(k==='?' || k===','){ e.preventDefault(); mailShortcutsHelp(); return; }
  if(k==='c' || k==='с'){ if(mailCanSend()){ e.preventDefault(); mailCompose('new'); } return; }
  if((k==='j'||k==='о'||k==='k'||k==='л') && list){
    const items=[...list.querySelectorAll('.msg-item')]; if(!items.length) return;
    const cur=items.findIndex(x=>x.classList.contains('active'));
    const next=(k==='j'||k==='о')?Math.min(items.length-1, cur+1):Math.max(0, cur<0?0:cur-1);
    e.preventDefault(); items[next].focus(); items[next].click(); return;
  }
  if(!Mail.cur || !Mail.readerGo) return;
  const map={r:'reply', 'к':'reply', a:'reply_all', 'ф':'reply_all', f:'forward', 'а':'forward', s:'star', 'ы':'star', u:'unread', 'г':'unread', '#':'delete', Delete:'delete', v:'move', 'м':'move', l:'tag', 'д':'tag', b:'snooze', 'и':'snooze', e:'archive', 'у':'archive'};
  const c=map[k]; if(!c) return;
  const rd=$('#mreader'); const btn=rd && rd.querySelector(`[data-c="${c}"]`);
  if(!btn || btn.disabled) return;
  e.preventDefault();
  Mail.readerGo(c, {currentTarget:btn});
});

