/* MailArchiver — веб-интерфейс: «Почта»: папки, список писем, письмо, новое письмо.
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Почта: почтовый клиент поверх архива (1.17.0)
// ===================================================================
// Письма показываются из архива (мгновенно, даже без связи с сервером), а действия — флаги, перенос,
// удаление, папки, отправка — идут на почтовый сервер и тут же отражаются в архиве (/api/webmail/...).
// picked — выбранные письма: ключ «ящик:id» → {acc, id, subject}
const Mail = { acc:null, folder:null, offset:0, total:0, limit:mailPrefs().limit, msg:null, search:null, openAfter:null, picked:new Map(),
  // номера последних запросов: ответ на прежний запрос (щёлкнули письмо А, потом Б, а А ответило позже)
  // не перерисовывает то, что уже показано по новому
  seqFolders:0, seqList:0, seqMsg:0,
  st:null, folders:[], special:{}, sort:'date_desc', filter:'all', showGone:false, items:[], cur:null,
  drafts:[], timer:null, view:'folder', goneCount:0, syncing:false, tags:[], extras:null, inboxUnseen:null, baseTitle:'',
  searches:[], quota:null, pushSeq:0 };
const MAIL_ICONS={inbox:'📥', drafts:'📝', sent:'📤', archive:'🗄️', junk:'🚫', trash:'🗑️', all:'🗂️'};
const MAIL_NAMES={inbox:'Входящие', drafts:'Черновики', sent:'Отправленные', archive:'Архив', junk:'Спам', trash:'Корзина', all:'Вся почта'};
/** Вид «Почты» в этом браузере: писем на странице, компактный список, уведомления о новой почте. */
const MAIL_PREFS_KEY='ma.mail.prefs';
function mailPrefs(){
  const def={limit:50, compact:false, notify:false, threads:false};
  try{ return Object.assign(def, JSON.parse(localStorage.getItem(MAIL_PREFS_KEY)||'{}')||{}); }catch(e){ return def; }
}
function mailSavePrefs(p){ try{ localStorage.setItem(MAIL_PREFS_KEY, JSON.stringify(p)); }catch(e){ /* приватное окно: до перезагрузки страницы */ } }

function mailCanAct(){ return !!(Mail.st && Mail.st.can_act); }
function mailCanSend(){ return !!(Mail.st && Mail.st.can_send); }
function mailOnline(){ return !!(Mail.st && Mail.st.online); }
function mailWhen(iso){
  if(!iso) return '';
  try{
    const d=new Date(iso), now=new Date();
    if(d.toDateString()===now.toDateString()) return d.toLocaleTimeString(UI_LOCALE,{hour:'2-digit',minute:'2-digit'});
    if(d.getFullYear()===now.getFullYear()) return d.toLocaleDateString(UI_LOCALE,{day:'numeric',month:'short'});
    return d.toLocaleDateString(UI_LOCALE,{day:'2-digit',month:'2-digit',year:'2-digit'});
  }catch(e){ return ''; }
}
function mailFolderInfo(name){ return Mail.folders.find(f=>f.name===name)||null; }
function mailFolderLabel(f){
  if(!f) return '';
  if(f.special && MAIL_NAMES[f.special]) return MAIL_NAMES[f.special];
  const d=f.delimiter||'';
  return d && f.name.includes(d) ? f.name.slice(f.name.lastIndexOf(d)+d.length) : f.name;
}
function mailFolderDepth(f){
  if(f.special || !f.delimiter) return 0;
  let name=f.name;
  const inboxPrefix='INBOX'+f.delimiter;
  if(name.toUpperCase().startsWith(inboxPrefix.toUpperCase())) name=name.slice(inboxPrefix.length);   // Courier: «INBOX.Проекты»
  return Math.min(6, name.split(f.delimiter).length-1);
}
/** Папка «Отправленные» или «Черновики»: в списке вместо отправителя — получатель. */
function mailIsOutFolder(name){ const f=mailFolderInfo(name); return !!(f && (f.special==='sent' || f.special==='drafts')); }

async function viewMail(c){
  c.innerHTML='';
  // щелчок по уведомлению о новой почте: «#/mail?open=ящик.письмо»
  const po=/[?&]open=(\d+)\.(\d+)/.exec(location.hash);
  if(po){ history.replaceState(null, '', '#/mail'); const a=parseInt(po[1]); State.mailAccount=a; Mail.folder=null; Mail.offset=0; Mail.search=null; Mail.openAfter={acc:a, id:parseInt(po[2])}; }
  const mine=myAccounts();
  let accId = isMailbox()
    ? ((State.mailAccount && mine.find(a=>a.id===State.mailAccount)) ? State.mailAccount : mine[0].id)
    : (State.mailAccount||null);
  let accounts = [];
  const head=h(`<div class="section-title mail-head"><h2 style="margin:0">📧 Почта</h2>
    <span class="btn-row" id="mailTools"></span><div class="spacer"></div><span id="mailAccWrap" class="btn-row"></span></div>`);
  c.appendChild(head);
  if(isMailbox() && accId){
    State.mailAccount=accId;
    // У сотрудника может быть несколько ящиков (все ящики его карточки) — переключатель между ними.
    if(mine.length>1){
      const sel=h(`<select id="mailAcc" aria-label="Ящик" style="width:auto;padding:7px 12px">${mine.map(a=>`<option value="${a.id}" ${a.id===accId?'selected':''}>${esc(a.name)}${a.rights==='read'?' (только чтение)':(a.rights==='full'?' (открыт вам)':'')}</option>`).join('')}</select>`);
      sel.onchange=()=>{ State.mailAccount=parseInt(sel.value); Mail.folder=null; Mail.offset=0; Mail.picked.clear(); viewMail(c); };
      head.querySelector('#mailAccWrap').appendChild(sel);
    }
    // Сотруднику — выгрузка и восстановление своего ящика прямо отсюда: меню
    // ящика в разделе «Почтовые ящики» ему недоступно.
    const cur=mine.find(a=>a.id===accId)||{id:accId, name:'мой ящик'};
    const acts=h(`<span class="btn-row"><button class="btn sm ghost" id="mbExport" title="Выгрузить ящик в PST или EML">📤 Выгрузить</button><button class="btn sm ghost" id="mbRestore" title="Вернуть письма из архива в ящик на сервере">♻️ Восстановить в ящик</button></span>`);
    acts.querySelector('#mbExport').onclick=()=>exportModal(cur);
    acts.querySelector('#mbRestore').onclick=()=>restoreModal(cur);
    head.querySelector('#mailAccWrap').appendChild(acts);
  }
  if(!isMailbox()){
    accounts = await api('/accounts'); State.accounts=accounts;
    if(!accounts.length){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">📭</div>Нет ящиков. Добавьте ящик и сделайте бэкап.</div></div>')); return; }
    if(!accId || !accounts.find(a=>a.id===accId)) accId=accounts[0].id;
    State.mailAccount=accId;
    const sel=h(`<select id="mailAcc" aria-label="Ящик" style="width:auto;padding:7px 12px">${accounts.map(a=>`<option value="${a.id}" ${a.id===accId?'selected':''}>${esc(a.name)}</option>`).join('')}</select>`);
    sel.onchange=()=>{ State.mailAccount=parseInt(sel.value); Mail.folder=null; Mail.offset=0; Mail.picked.clear(); viewMail(c); };
    head.querySelector('#mailAccWrap').appendChild(sel);
  }
  if(Mail.acc!==accId && !(Mail.search && Mail.search.scope==='all')) Mail.picked.clear();
  if(Mail.acc!==accId){ Mail.view='folder'; Mail.cur=null; Mail.inboxUnseen=null; Mail.filter='all'; }
  Mail.acc=accId;
  const seq=State.routeSeq;
  try{ Mail.st=await api(`/webmail/status?account_id=${accId}`); }
  catch(e){ Mail.st={enabled:false, online:false, can_act:false, can_send:false, act_problem:e.message, mark_read:false, auto_refresh_s:0}; }
  if(seq!==State.routeSeq) return;
  const tools=head.querySelector('#mailTools');
  if(Mail.st.enabled){
    tools.appendChild(h(`<button class="btn sm primary" id="mailCompose" ${mailCanSend()?'':'disabled'} title="${esc(mailCanSend()?'Новое письмо (c)':(Mail.st.send_problem||''))}">✏️ Написать</button>`));
    tools.appendChild(h(`<button class="btn sm" id="mailSync" ${mailOnline()?'':'disabled'} title="Проверить почту на сервере">🔄 Проверить почту</button>`));
    tools.appendChild(h(`<button class="btn sm ghost" id="mailBook" title="Адресная книга ящика">📒</button>`));
    tools.appendChild(h(`<button class="btn sm ghost" id="mailCal" title="Календарь почтового сервера (CalDAV)">📅</button>`));
    tools.appendChild(h(`<button class="btn sm ghost" id="mailIdent" title="Настройки клиента: отправитель и подпись${isAdmin()?', сервер отправки':''}, правила, автоответ, метки, шаблоны, вид">⚙️</button>`));
    $('#mailCompose',tools).onclick=()=>mailCompose('new');
    $('#mailSync',tools).onclick=()=>mailSync(true);
    $('#mailIdent',tools).onclick=()=>mailSettingsDialog(accId);
    $('#mailBook',tools).onclick=()=>mailSettingsDialog(accId, 'book');
    $('#mailCal',tools).onclick=async()=>{ let d={}; try{ d=await api(`/webmail/dav?account_id=${accId}`); }catch(e){ /* покажем подсказку */ }
      if(d.calendar) mailCalendarDlg(accId); else toast('Календарь не подключён', 'Подключите календарь сервера: ⚙ Настройки ящика → «Календарь и контакты».', 'warn', 7000); };
    mailArButton(accId);
    try{ Mail.tags=(await api(`/webmail/tags?account_id=${accId}`, {bg:true})).items||[]; }catch(e){ Mail.tags=[]; }
  }else Mail.tags=[];
  const why = !Mail.st.enabled ? '' : (!Mail.st.online ? Mail.st.online_problem : (!Mail.st.can_act ? Mail.st.act_problem : ''));
  if(why) c.appendChild(h(`<div class="hint mail-ro">🔒 ${esc(why)} Письма показываются из архива.</div>`));
  const sbar=h(`<div class="mail-search">
    <input type="search" id="msq" placeholder="Поиск по письмам: слова, «фраза в кавычках», тема:, от:, вложение:  (клавиша /)" value="${esc(Mail.search?Mail.search.q:'')}">
    ${isMailbox()&&mine.length<2?'':`<select id="msScope" aria-label="Где искать"><option value="account">в этом ящике</option><option value="all" ${Mail.search&&Mail.search.scope==='all'?'selected':''}>${isMailbox()?'во всех моих ящиках':'во всех ящиках'}</option></select>`}
    <label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="msAttach" ${Mail.search&&Mail.search.attach?'checked':''}> с вложениями</label>
    <button class="btn sm primary" id="msGo">🔎 Найти</button>
    <button class="btn sm ghost" id="msClear" ${Mail.search?'':'style="display:none"'}>✕ Сбросить</button>
    <button class="btn sm ghost" id="msSave" ${Mail.search?'':'style="display:none"'} title="Сохранить этот поиск — он появится под папками">☆ Сохранить поиск</button>
    <span class="small muted" id="msInfo"></span></div>`);
  c.appendChild(sbar);
  const runSearch=()=>{
    const q=$('#msq',sbar).value.trim();
    if(!q){ Mail.search=null; $('#msClear',sbar).style.display='none'; loadMailMessages(accId); return; }
    Mail.search={q, scope:($('#msScope',sbar)||{}).value||'account', attach:$('#msAttach',sbar).checked, offset:0};
    $('#msClear',sbar).style.display=''; $('#msSave',sbar).style.display='';
    loadSearchResults(accId);
  };
  $('#msGo',sbar).onclick=runSearch;
  $('#msq',sbar).onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); runSearch(); } if(e.key==='Escape'){ e.target.blur(); } };
  $('#msClear',sbar).onclick=()=>{ $('#msq',sbar).value=''; Mail.search=null; $('#msClear',sbar).style.display='none'; $('#msSave',sbar).style.display='none'; loadMailMessages(accId); };
  $('#msSave',sbar).onclick=()=>mailSaveSearch(accId);
  api('/search/status',{bg:true}).then(st=>{
    const info=$('#msInfo',sbar); if(!info) return;
    if(!st.available) info.textContent='Поиск только по теме и отправителю (SQLite без FTS5).';
    else if(st.pending>0) info.textContent=`Проиндексировано ${fmtNum(st.indexed)} из ${fmtNum(st.total)} писем — остальные индексируются в фоне.`;
    else if(!st.bodies) info.textContent='Поиск по теме, адресам и вложениям (текст писем не индексируется).';
  }).catch(()=>{});
  // Выбранные письма: действия над ними (прочитано, перенос, удаление…) и «Восстановить выбранные».
  c.appendChild(h('<div id="mpick" class="mail-pick" role="status" aria-live="polite" hidden></div>'));
  renderPickBar();
  const prefs=mailPrefs(); Mail.limit=prefs.limit||50;
  const layout=h(`<div class="mail-layout ${prefs.compact?'compact':''}">
    <div class="mail-folders" id="mfolders"><div class="empty" style="padding:20px"><div class="spinner"></div></div></div>
    <div class="mail-list" id="mlist"><div class="empty">Выберите папку</div></div>
    <div class="mail-reader" id="mreader"><div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div></div>
  </div>`);
  c.appendChild(layout);
  await loadMailFolders(accId);
  mailStartTimer();
  mailPushLoop(accId);
}

/** Проверка почты по таймеру, пока раздел открыт и вкладка видна. */
function mailStartTimer(){
  if(Mail.timer){ clearInterval(Mail.timer); Mail.timer=null; }
  const every=(Mail.st&&Mail.st.auto_refresh_s)||0;
  if(!every || !mailOnline()) return;
  const seq=State.routeSeq;
  Mail.timer=setInterval(()=>{
    if(seq!==State.routeSeq || State.view!=='mail'){ clearInterval(Mail.timer); Mail.timer=null; return; }
    const inbox=Mail.special.inbox;
    if(inbox && Mail.folder!==inbox) mailSyncInbox();
    if(document.hidden || Mail.search || Mail.view!=='folder') return;
    mailSync(false);
  }, Math.max(30, every)*1000);
}

/** Проверить открытую папку на сервере: новые письма — в архив и в список, удалённые — убрать. */
async function mailSync(manual){
  if(!mailOnline() || !Mail.folder || Mail.syncing) return;
  const fi=mailFolderInfo(Mail.folder);
  if(fi && fi.on_server===false){ if(manual) toast('Папки нет на сервере','Её письма есть только в архиве.','warn'); return; }
  Mail.syncing=true;
  const btn=$('#mailSync'); if(btn && manual){ btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Проверяю…'; }
  const acc=Mail.acc, folder=Mail.folder;
  try{
    const r=await api('/webmail/sync',{method:'POST', body:{account_id:acc, folder, folders:!!manual, manual:!!manual}, bg:!manual});
    if(Mail.acc!==acc) return;
    if(r.skipped) return;
    if(r.uidvalidity_changed && manual) toast('Папка на сервере пересоздана','Запущено копирование ящика — письма сверятся с архивом.','warn',7000);
    if(manual){
      const parts=[];
      if(r.new) parts.push(`новых писем: ${fmtNum(r.new)}`);
      if(r.gone) parts.push(`удалено на сервере: ${fmtNum(r.gone)}`);
      if(r.pending) parts.push(`ещё ${fmtNum(r.pending)} скачает копирование`);
      if(r.backup_running) parts.push('идёт копирование ящика — новые письма появятся после него');
      toast('Почта проверена', parts.join(', ')||'Нового нет.');
    }
    const rl=r.rules||{};
    if(rl.moved || rl.flagged){ if(manual) toast('Сработали правила', `Писем: ${fmtNum(rl.matched)}${rl.moved?`, перенесено ${fmtNum(rl.moved)}`:''}`); }
    if(r.new || r.gone || r.flags || manual || rl.moved || rl.flagged){
      await loadMailFolders(acc, {quiet:true});
      if(folder===Mail.folder && !Mail.search && Mail.view==='folder') await loadMailMessages(acc, {keep:true});
    }else{
      const f=mailFolderInfo(folder); if(f && (f.unseen!==r.unseen || f.total!==r.total)){ f.unseen=r.unseen; f.total=r.total; renderMailFolders(acc); }
    }
  }catch(e){ if(manual) toastErr(e); }
  finally{ Mail.syncing=false; if(btn && manual){ btn.disabled=false; btn.innerHTML='🔄 Проверить почту'; } }
}

/** Возвращать письма на сервер может администратор и сам сотрудник (оператор писем не видит). */
function canRestoreMessages(){ return isAdmin() || isMailbox(); }

/** Флажок выбора письма в списке. */
function pickBox(acc, id, subject){
  if(!canRestoreMessages()) return '';
  return `<input type="checkbox" class="msg-pick" data-pick ${Mail.picked.has(acc+':'+id)?'checked':''} aria-label="Выбрать письмо «${esc(trunc(subject||'без темы',60))}»">`;
}
function wirePick(el, acc, id, subject, ids){
  const cb=el.querySelector('input[data-pick]'); if(!cb) return;
  // щелчок по флажку не открывает письмо
  cb.onclick=(e)=>e.stopPropagation();
  cb.onkeydown=(e)=>e.stopPropagation();
  cb.onchange=()=>{
    const k=acc+':'+id;
    if(cb.checked) Mail.picked.set(k, ids&&ids.length>1?{acc, id, subject, ids}:{acc, id, subject}); else Mail.picked.delete(k);
    el.classList.toggle('picked', cb.checked);
    renderPickBar();
    const list=el.parentElement; if(list) list.dispatchEvent(new Event('pickchange'));
  };
}
function mailPickedIds(){ return [...Mail.picked.values()].filter(p=>p.acc===Mail.acc).flatMap(p=>p.ids||[p.id]); }
/** Выбранные письма для «Восстановить»: беседа раскрывается во все её письма. */
function mailPickedItems(){ return [...Mail.picked.values()].flatMap(p=>(p.ids||[p.id]).map(id=>({acc:p.acc, id, subject:p.subject}))); }
function mailClearPicked(){
  Mail.picked.clear(); renderPickBar();
  document.querySelectorAll('#mlist input[data-pick], #mlist input[data-pickall]').forEach(x=>{ x.checked=false; x.indeterminate=false; });
  document.querySelectorAll('#mlist .msg-item.picked').forEach(x=>x.classList.remove('picked'));
}
/** Полоса «Выбрано: N» с действиями над выбранными письмами. */
function renderPickBar(){
  const bar=$('#mpick'); if(!bar) return;
  const n=mailPickedItems().length;
  bar.hidden=!n;
  if(!n){ bar.innerHTML=''; return; }
  const accs=new Set([...Mail.picked.values()].map(p=>p.acc));
  const here=accs.size===1 && accs.has(Mail.acc) && mailCanAct();
  bar.innerHTML=`<span>Выбрано: <b>${fmtNum(n)}</b> ${plural(n,'письмо','письма','писем')}${accs.size>1?` из ${accs.size} ${plural(accs.size,'ящика','ящиков','ящиков')}`:''}</span>
    ${here?`<button class="btn sm" data-a="read" title="Отметить прочитанными">✉️ Прочитано</button>
      <button class="btn sm" data-a="unread" title="Отметить непрочитанными">📩 Не прочитано</button>
      <button class="btn sm" data-a="star" title="Пометить">⭐</button>
      <button class="btn sm" data-a="move" title="Переместить в папку">📁 Переместить</button>
      <button class="btn sm" data-a="archive" title="В архив (e)">🗄 В архив</button>
      <button class="btn sm" data-a="tag" title="Поставить или снять метку">🏷 Метка</button>
      <button class="btn sm" data-a="snooze" title="Убрать из папки и вернуть непрочитанными в назначенное время">⏰ Напомнить</button>
      <button class="btn sm" data-a="junk" title="В спам">🚫 Спам</button>
      <button class="btn sm danger" data-a="delete" title="Удалить (Delete)">🗑 Удалить</button>
      ${mailCanSend()?'<button class="btn sm" data-a="fwdatt" title="Переслать выбранные письма файлами .eml">↪ Переслать вложением</button>':''}`:''}
    ${accs.size===1&&accs.has(Mail.acc)?'<button class="btn sm" data-a="zip" title="Скачать выбранные письма архивом .zip (файлы .eml)">⬇ .zip</button>':''}
    <button class="btn sm ${here?'ghost':'primary'}" data-go>♻️ Восстановить</button>
    <button class="btn sm ghost" data-clear>✕ Снять выбор</button>
    ${mailSelectAllLink()}`;
  bar.querySelector('[data-go]').onclick=()=>messagesRestoreModal(mailPickedItems(), mailClearPicked);
  bar.querySelector('[data-clear]').onclick=mailClearPicked;
  bar.querySelectorAll('[data-a]').forEach(b=>b.onclick=(e)=>{
    const ids=mailPickedIds(); const a=b.dataset.a;
    if(a==='read') mailAct('flags', ids, {add:['\\Seen']});
    else if(a==='unread') mailAct('flags', ids, {remove:['\\Seen']});
    else if(a==='star') mailAct('flags', ids, {add:['\\Flagged']});
    else if(a==='junk') mailAct('junk', ids, {spam:(mailFolderInfo(Mail.folder)||{}).special!=='junk'});
    else if(a==='delete') mailDelete(ids);
    else if(a==='move') mailMovePicker(e.currentTarget, ids);
    else if(a==='tag') mailTagPicker(e.currentTarget, ids);
    else if(a==='archive') mailAct('archive', ids);
    else if(a==='snooze') mailSnoozePicker(e.currentTarget, ids);
    else if(a==='fwdatt') mailCompose('forward_attach', null, {ref_ids:ids.slice(0,50)});
    else if(a==='zip') mailZip(ids);
  });
  const all=bar.querySelector('[data-allfolder]');
  if(all) all.onclick=async(e)=>{ e.preventDefault(); await mailPickWholeFolder(); };
}

/** Действие над письмами на сервере; после него — список и счётчики папок заново. */
async function mailAct(kind, ids, extra={}, {quiet=false}={}){
  if(!ids.length) return null;
  const acc=Mail.acc;
  let r;
  try{ r=await api('/webmail/'+kind, {method:'POST', body:Object.assign({account_id:acc, ids}, extra)}); }
  catch(e){ toastErr(e); return null; }
  if(r && r.missing && !quiet) toast('Часть писем уже удалена на сервере', `Писем: ${fmtNum(r.missing)} — в архиве они остались.`, 'warn');
  if(kind!=='flags') ids.forEach(id=>Mail.picked.delete(acc+':'+id));
  renderPickBar();
  if(!quiet && r && (kind==='move' || kind==='junk' || kind==='archive' || (kind==='delete' && !extra.permanent))){
    const n=r.moved!=null?r.moved:r.trashed;
    const title=kind==='archive'?'Перенесено в архив':kind==='delete'?'Перенесено в корзину':(kind==='junk'?(extra.spam===false?'Перенесено во «Входящие»':'Перенесено в спам'):`Перенесено в «${mailFolderLabel(mailFolderInfo(extra.folder))||extra.folder}»`);
    if(n && r.undo && r.undo.length) toastAction(title, `Писем: ${fmtNum(n)}`, 'Отменить', ()=>mailUndo(acc, r.undo));
    else if(n) toast(title, `Писем: ${fmtNum(n)}`);
  }
  if(kind==='flags'){
    const add=extra.add||[], rem=extra.remove||[];
    Mail.items.forEach(m=>{ if(!ids.includes(m.id)) return;
      if(add.includes('\\Seen')) m.seen=true; if(rem.includes('\\Seen')) m.seen=false;
      if(add.includes('\\Flagged')) m.flagged=true; if(rem.includes('\\Flagged')) m.flagged=false; });
    renderMailList(acc);
    if(add.includes('\\Seen')||rem.includes('\\Seen')) loadMailFolders(acc, {quiet:true});
    if(Mail.cur && ids.includes(Mail.cur)) mailReaderFlags();
  }else{
    if(Mail.cur && ids.includes(Mail.cur)){ Mail.cur=null; const rd=$('#mreader'); if(rd) rd.innerHTML='<div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div>'; }
    await loadMailFolders(acc, {quiet:true});
    if(!Mail.search) await loadMailMessages(acc, {keep:true}); else loadSearchResults(acc);
  }
  return r;
}

async function mailDelete(ids, {permanent=false}={}){
  if(!ids.length) return;
  const f=mailFolderInfo(Mail.folder);
  const inTrash=f && f.special==='trash';
  const noTrash=!Mail.special.trash;
  if(permanent || inTrash || noTrash){
    const ok=await confirmDlg('Удалить с сервера насовсем?', `${fmtNum(ids.length)} ${plural(ids.length,'письмо','письма','писем')} будут удалены из ящика на сервере без корзины. Копии в архиве останутся — их можно будет вернуть «Восстановить».`, {okText:'Удалить'});
    if(!ok) return;
    const r=await mailAct('delete', ids, {permanent:true});
    if(r) toast('Удалено с сервера', `Писем: ${fmtNum(r.deleted)}. Копии остались в архиве.`);
    return;
  }
  await mailAct('delete', ids, {permanent:false});
}

/** Выбор папки для переноса — всплывающий список у кнопки. */
function mailMovePicker(anchor, ids){
  document.querySelectorAll('.mail-pop').forEach(x=>x.remove());
  const targets=Mail.folders.filter(f=>f.on_server!==false && f.selectable && f.name!==Mail.folder);
  const pop=h(`<div class="mail-pop" role="menu"><input type="text" placeholder="Найти папку" aria-label="Найти папку"><div class="mail-pop-list"></div></div>`);
  const list=pop.querySelector('.mail-pop-list'), q=pop.querySelector('input');
  const fill=()=>{ const s=q.value.trim().toLowerCase();
    list.innerHTML=targets.filter(f=>!s || f.name.toLowerCase().includes(s) || mailFolderLabel(f).toLowerCase().includes(s)).slice(0,200)
      .map(f=>`<button class="mail-pop-item" data-f="${esc(f.name)}" style="padding-left:${10+mailFolderDepth(f)*14}px">${MAIL_ICONS[f.special]||'📁'} ${esc(mailFolderLabel(f))}</button>`).join('')||'<div class="muted small" style="padding:8px">Папок нет</div>';
    list.querySelectorAll('[data-f]').forEach(b=>b.onclick=async()=>{ pop.remove(); await mailAct('move', ids, {folder:b.dataset.f}); });
  };
  fill(); q.oninput=fill;
  document.body.appendChild(pop);
  const r=anchor.getBoundingClientRect();
  pop.style.top=Math.min(window.innerHeight-320, r.bottom+4+window.scrollY)+'px';
  pop.style.left=Math.max(8, Math.min(window.innerWidth-280, r.left+window.scrollX))+'px';
  q.focus();
  const off=(e)=>{ if(!pop.contains(e.target) && e.target!==anchor){ pop.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(()=>document.addEventListener('mousedown', off), 0);
  q.onkeydown=(e)=>{ if(e.key==='Escape'){ pop.remove(); } };
}

/** Восстановить отдельные письма: в тот же ящик или (администратору) в другой.
 *  items — [{acc, id, subject}], письма могут быть из разных ящиков (поиск во всех ящиках). */
async function messagesRestoreModal(items, onDone){
  if(!items.length) return;
  const groups=new Map();
  items.forEach(it=>{ if(!groups.has(it.acc)) groups.set(it.acc, []); groups.get(it.acc).push(it.id); });
  const names=new Map((isMailbox()?myAccounts():(State.accounts||[])).map(a=>[a.id, a.name]));
  if(isAdmin() && !(State.accounts||[]).length){
    try{ State.accounts=await api('/accounts'); (State.accounts||[]).forEach(a=>names.set(a.id, a.name)); }catch(e){ /* список для «в другой ящик» — необязателен */ }
  }
  // srcPlain — для confirmDlg и toast (они экранируют сами), srcText — для разметки окна
  const srcPlain=groups.size===1
    ? `из ящика «${names.get(items[0].acc)||''}»`
    : `из ${groups.size} ${plural(groups.size,'ящика','ящиков','ящиков')}`;
  const srcText=esc(srcPlain);
  const one=items.length===1;
  const body=`
    <p style="margin-top:0">${one?`Письмо «${esc(trunc(items[0].subject||'без темы',90))}»`:`Писем: <b>${fmtNum(items.length)}</b>`} ${srcText} ${one?'будет снова загружено':'будут снова загружены'} на почтовый сервер${isAdmin()?'':' — в тот же ящик'}.</p>
    ${isAdmin()?`<fieldset class="form-row" style="border:0;padding:0;margin:0 0 12px">
      <legend style="font-weight:600;margin-bottom:6px">Куда</legend>
      <label class="small" style="display:flex;gap:8px;align-items:center"><input type="radio" name="rm-where" value="same" checked> ${groups.size===1?'В тот же ящик':'Каждое — в свой ящик'}</label>
      <label class="small" style="display:flex;gap:8px;align-items:center;margin-top:4px"><input type="radio" name="rm-where" value="other"> В другой ящик (например, коллеге уволенного сотрудника)</label>
      <div id="rm-otherrow" class="form-row" style="display:none;margin:8px 0 0 24px">
        <input id="rm-filter" type="text" placeholder="Найти ящик по названию или адресу" aria-label="Найти ящик" style="margin-bottom:6px">
        <select id="rm-target" size="6" aria-label="Ящик, в который вернуть письма"></select>
        <div class="hint">Переписка одного человека окажется в ящике другого — действие записывается в аудит.</div></div>
    </fieldset>`:''}
    <div class="form-row"><label for="rm-mode">В какую папку</label><select id="rm-mode">
      <option value="original">${one?'В ту же папку, где оно лежало':'В те же папки, где письма лежали'}</option>
      <option value="single">Все в одну папку</option>
      <option value="prefixed">В папки с префиксом (повторяют исходные)</option></select></div>
    <div class="form-row" id="rm-singlerow" style="display:none"><label for="rm-single">Имя папки</label><input id="rm-single" type="text" value="Восстановлено"></div>
    <div class="form-row" id="rm-prefixrow" style="display:none"><label for="rm-prefix">Префикс</label><input id="rm-prefix" type="text" value="Восстановлено">
      <div class="hint">Например, письмо из INBOX попадёт в папку «<span id="rm-prefixex">Восстановлено</span>/INBOX».</div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="rm-dup" checked><span class="track"></span></span><label for="rm-dup">Не загружать письма, которые уже есть в папке</label></div>
    <div class="hint">Восстановление идёт заданием в очереди; ход и итог — в «Очереди и заданиях».</div>`;
  const m=modal(one?'Восстановить письмо':'Восстановить выбранные письма', body,
    {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Восстановить</button>`});
  const g=x=>m.body.querySelector(x);
  const fillTargets=()=>{
    const sel=g('#rm-target'); if(!sel) return;
    const q=(g('#rm-filter').value||'').trim().toLowerCase();
    const list=(State.accounts||[]).filter(a=>!q || (a.name||'').toLowerCase().includes(q) || (a.username||'').toLowerCase().includes(q)).slice(0,200);
    const keep=sel.value;
    sel.innerHTML=list.map(a=>`<option value="${a.id}">${esc(a.name)} — ${esc(a.username||'')}${a.employee_name?' · '+esc(a.employee_name):''}</option>`).join('');
    if(keep && list.find(a=>String(a.id)===keep)) sel.value=keep;
  };
  if(g('#rm-target')){ fillTargets(); g('#rm-filter').oninput=fillTargets; }
  m.body.querySelectorAll('input[name=rm-where]').forEach(r=>r.onchange=()=>{
    const other=m.body.querySelector('input[name=rm-where]:checked').value==='other';
    const row=g('#rm-otherrow'); if(row) row.style.display=other?'block':'none';
    // в чужом ящике удобнее отдельная папка: по умолчанию — с префиксом
    if(other && g('#rm-mode').value==='original'){ g('#rm-mode').value='prefixed'; g('#rm-mode').onchange(); }
  });
  g('#rm-mode').onchange=()=>{ const v=g('#rm-mode').value; g('#rm-singlerow').style.display=v==='single'?'block':'none'; g('#rm-prefixrow').style.display=v==='prefixed'?'block':'none'; };
  g('#rm-prefix').oninput=()=>{ g('#rm-prefixex').textContent=g('#rm-prefix').value.trim()||'…'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const where=(m.body.querySelector('input[name=rm-where]:checked')||{}).value||'same';
    const mode=g('#rm-mode').value;
    const req={target_mode:mode, target_folder:g('#rm-single').value.trim(), target_prefix:g('#rm-prefix').value.trim(),
               check_duplicates:g('#rm-dup').checked};
    if(mode==='single' && !req.target_folder) return toastErr({message:'Укажите имя папки.'});
    if(mode==='prefixed' && !req.target_prefix) return toastErr({message:'Укажите префикс папок.'});
    let target=null;
    if(where==='other'){
      target=parseInt((g('#rm-target')||{}).value||'0');
      if(!target) return toastErr({message:'Выберите ящик, в который вернуть письма.'});
      const tname=names.get(target)||((State.accounts||[]).find(a=>a.id===target)||{}).name||'';
      if(!await confirmDlg('Вернуть письма в другой ящик?',
          `${fmtNum(items.length)} ${plural(items.length,'письмо','письма','писем')} ${srcPlain} появятся в ящике «${tname}». Действие записывается в аудит.`,
          {okText:'Вернуть', okClass:'primary'})) return;
    }
    const btn=m.foot.querySelector('[data-go]'); btn.disabled=true;
    const jobs=[];
    try{
      for(const [acc, ids] of groups){
        const b=Object.assign({message_ids:ids}, req);
        if(target && target!==acc) b.target_account_id=target;
        const r=await api(`/accounts/${acc}/restore-messages`, {method:'POST', body:b});
        jobs.push(r.job_id);
      }
      m.close();
      toast('Восстановление запущено', `${fmtNum(items.length)} ${plural(items.length,'письмо','письма','писем')} — ${jobs.length>1?'задания':'задание'} №${jobs.join(', №')}; ход — в «Очереди и заданиях».`, 'success', 8000);
      if(onDone) onDone();
    }catch(e){
      btn.disabled=false;
      toastErr(jobs.length ? {message:`${e.message} (уже запущено: №${jobs.join(', №')})`, hint:e.hint} : e);
    }
  };
}

// ---------- Папки ----------
async function loadMailFolders(accId, {refresh=false, quiet=false}={}){
  const box=$('#mfolders'); if(!box) return;
  const seq=++Mail.seqFolders;
  let data;
  try{ data=await api(`/webmail/folders?account_id=${accId}${refresh?'&refresh=true':''}`, {bg:quiet}); }
  catch(e){ if(seq===Mail.seqFolders && !quiet) box.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  if(seq!==Mail.seqFolders || Mail.acc!==accId) return;
  Mail.folders=data.folders||[]; Mail.special=data.special||{};
  mailNotifyNew();
  if(data.error && !quiet && mailOnline()) toast('Список папок с сервера не получен', data.error.message+(data.error.hint?' '+data.error.hint:''), 'warn', 7000);
  try{ const d=await api(`/webmail/drafts?account_id=${accId}`, {bg:true}); Mail.drafts=d.items||[]; }catch(e){ Mail.drafts=[]; }
  if(!quiet){ await mailLoadSearches(); if(!Mail.quota || Mail.quota._acc!==accId){ Mail.quota={_acc:accId}; mailLoadQuota(accId); } }
  if(seq!==Mail.seqFolders) return;
  if(!Mail.folders.length){ box.innerHTML='<div class="empty small" style="padding:16px">Нет папок.<br>Сделайте бэкап ящика.</div>'; return; }
  const first=Mail.folders.find(f=>f.special==='inbox') || Mail.folders.find(f=>f.selectable) || Mail.folders[0];
  if(!Mail.folder || !Mail.folders.find(f=>f.name===Mail.folder)) Mail.folder=first.name;
  renderMailFolders(accId);
  if(!quiet){
    if(Mail.view==='drafts') mailShowDrafts(accId);
    else if(Mail.search) loadSearchResults(accId); else loadMailMessages(accId);
  }
  // письмо, открытое из отчёта аналитики
  if(Mail.openAfter && Mail.openAfter.acc===accId){ const id=Mail.openAfter.id; Mail.openAfter=null; loadMailMessage(accId, id); }
  // ссылка mailto:, открытая этим клиентом (обработчик ссылок почты в браузере)
  const mt=/[?&]compose=([^&]+)/.exec(location.hash);
  if(mt){ history.replaceState(null, '', '#/mail'); let u=''; try{ u=decodeURIComponent(mt[1]); }catch(e){ u=''; } if(u) setTimeout(()=>mailComposeFromMailto(u), 300); }
}

/** Новое письмо по ссылке mailto: (адрес, копия, тема, текст). */
function mailComposeFromMailto(url){
  const m=/^mailto:([^?]*)(?:\?(.*))?$/i.exec(String(url||'').trim()); if(!m) return;
  const dec=(v)=>{ try{ return decodeURIComponent(String(v||'').replace(/\+/g,' ')); }catch(e){ return String(v||''); } };
  const p=new URLSearchParams(m[2]||'');
  const get=(k)=>{ for(const [kk,v] of p.entries()) if(kk.toLowerCase()===k) return v; return ''; };
  const to=[dec(m[1]), get('to')].filter(Boolean).join(', ');
  const body=get('body');
  if(!mailCanSend()) return toast('Отправка недоступна', (Mail.st&&Mail.st.send_problem)||'Откройте ящик, из которого можно отправлять письма.', 'warn', 7000);
  mailCompose('new', null, {to_addrs:to.slice(0,5000), cc_addrs:get('cc').slice(0,5000), subject:get('subject').slice(0,998),
    body:body?esc(body).replace(/\r?\n/g,'<br>'):''});
}

function renderMailFolders(accId){
  const box=$('#mfolders'); if(!box) return;
  box.innerHTML='';
  const head=h(`<div class="mail-folders-head"><span class="muted small">Папки</span>
    ${mailCanAct()?'<button class="btn ghost sm" data-new title="Новая папка">＋</button>':''}
    ${mailOnline()?'<button class="btn ghost sm" data-ref title="Обновить список папок с сервера">↻</button>':''}</div>`);
  const nb=head.querySelector('[data-new]'); if(nb) nb.onclick=()=>mailFolderDialog('create', '');
  const rb=head.querySelector('[data-ref]'); if(rb) rb.onclick=()=>loadMailFolders(accId, {refresh:true});
  box.appendChild(head);
  if(Mail.drafts.length || Mail.view==='drafts'){
    const el=h(`<div class="mail-folder ${Mail.view==='drafts'?'active':''}" tabindex="0" role="button" title="Письма, начатые здесь и ещё не отправленные"><span>✏️</span><span class="fname">Неотправленные</span><span class="cnt">${Mail.drafts.length}</span></div>`);
    el.onclick=()=>{ Mail.view='drafts'; Mail.search=null; renderMailFolders(accId); mailShowDrafts(accId); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  }
  if(isMailbox() && myAccounts().length>1){
    // «Все входящие»: «Входящие» всех своих и открытых вам ящиков одним списком (1.22.0)
    const el=h(`<div class="mail-folder ${Mail.view==='unified'?'active':''}" tabindex="0" role="button" title="«Входящие» всех ваших ящиков одним списком"><span>📥 Все входящие</span></div>`);
    el.onclick=()=>{ Mail.view='unified'; Mail.search=null; renderMailFolders(accId); mailShowUnified(); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  }
  let archHead=false;
  Mail.folders.forEach(f=>{
    if(f.on_server===false && !archHead){
      archHead=true;
      box.appendChild(h(`<div class="mail-folders-sep" title="Этих папок на сервере больше нет (удалены или переименованы) — письма остались в архиве">🗄 Только в архиве</div>`));
    }
    const label=mailFolderLabel(f), depth=mailFolderDepth(f);
    const unseen=(f.unseen!=null && f.on_server!==false && f.special!=='drafts' && f.special!=='sent')?f.unseen:0;
    const total=(f.total!=null && f.on_server!==false)?f.total:f.archived;
    const icon=MAIL_ICONS[f.special]||(f.on_server===false?'🗄':'📁');
    const active=Mail.view==='folder' && f.name===Mail.folder;
    const el=h(`<div class="mail-folder ${active?'active':''} ${f.on_server===false?'gone':''} ${f.selectable?'':'noselect'}" tabindex="0" role="button" title="${esc(f.name)}" style="padding-left:${9+depth*14}px" data-fname="${esc(f.name)}">
      <span>${icon}</span><span class="fname">${esc(label)}</span>
      ${unseen?`<span class="cnt unseen" title="Непрочитанных">${fmtNum(unseen)}</span>`:`<span class="cnt" title="${f.on_server===false?'Писем в архиве':'Писем'}">${fmtNum(total||0)}</span>`}
      ${mailCanAct()&&f.on_server!==false&&f.selectable?'<button class="fmenu" title="Папка: прочитать всё, очистить, создать вложенную, переименовать, переместить, удалить" aria-label="Действия с папкой">⋯</button>':''}</div>`);
    if(f.selectable){
      el.onclick=(e)=>{ if(e.target.classList.contains('fmenu')) return;
        Mail.view='folder'; Mail.folder=f.name; Mail.offset=0; Mail.search=null; Mail.showGone=false;
        const q=$('#msq'); if(q) q.value=''; const cl=$('#msClear'); if(cl) cl.style.display='none';
        renderMailFolders(accId); loadMailMessages(accId);
        if(mailOnline() && f.on_server!==false) setTimeout(()=>mailSync(false), 300); };
      el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    }
    const fm=el.querySelector('.fmenu');
    if(fm) fm.onclick=(e)=>{ e.stopPropagation(); mailFolderMenu(fm, f); };
    // перенос писем перетаскиванием на папку
    if(mailCanAct() && f.on_server!==false && f.selectable){
      el.addEventListener('dragover', (e)=>{ if(e.dataTransfer.types.includes('text/x-mail-ids')){ e.preventDefault(); el.classList.add('drop'); } });
      el.addEventListener('dragleave', ()=>el.classList.remove('drop'));
      el.addEventListener('drop', async(e)=>{ e.preventDefault(); el.classList.remove('drop');
        let ids=[]; try{ ids=JSON.parse(e.dataTransfer.getData('text/x-mail-ids')||'[]'); }catch(_){ ids=[]; }
        if(ids.length && f.name!==Mail.folder) await mailAct('move', ids, {folder:f.name}); });
    }
    box.appendChild(el);
  });
  mailRenderTagList(accId, box);
  mailRenderSaved(accId, box);
  mailRenderQuota(box);
}

function mailFolderMenu(anchor, f){
  document.querySelectorAll('.mail-pop').forEach(x=>x.remove());
  const own=!f.special && f.name.toUpperCase()!=='INBOX';
  const bin=f.special==='trash' || f.special==='junk';
  const pop=h(`<div class="mail-pop" role="menu"><div class="mail-pop-list">
    ${f.unseen?'<button class="mail-pop-item" data-a="readall">✉️ Отметить все прочитанными</button>':''}
    ${bin?`<button class="mail-pop-item danger" data-a="empty">🧹 Очистить «${esc(mailFolderLabel(f))}»</button>`:''}
    ${bin?'':'<button class="mail-pop-item" data-a="create">＋ Создать вложенную папку</button>'}
    ${own?'<button class="mail-pop-item" data-a="rename">✏️ Переименовать</button><button class="mail-pop-item" data-a="moveto">📂 Переместить в другую папку</button>':''}
    <button class="mail-pop-item" data-a="rules">⚙️ Применить правила к письмам папки</button>
    <button class="mail-pop-item" data-a="zip">⬇ Скачать письма папки (.zip)</button>
    ${own?'<button class="mail-pop-item danger" data-a="delete">🗑 Удалить папку</button>':''}</div></div>`);
  document.body.appendChild(pop);
  const r=anchor.getBoundingClientRect();
  pop.style.top=(r.bottom+4+window.scrollY)+'px'; pop.style.left=Math.max(8, Math.min(window.innerWidth-280, r.left+window.scrollX-200))+'px';
  pop.querySelectorAll('[data-a]').forEach(b=>b.onclick=()=>{ pop.remove(); mailFolderAction(b.dataset.a, f); });
  const off=(e)=>{ if(!pop.contains(e.target)){ pop.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(()=>document.addEventListener('mousedown', off), 0);
}

async function mailFolderDialog(kind, name){
  const acc=Mail.acc;
  const f=mailFolderInfo(name);
  if(kind==='delete'){
    const n=f&&f.total!=null?f.total:0;
    const ok=await confirmDlg('Удалить папку на сервере?', `Папка «${name}»${n?` и ${fmtNum(n)} ${plural(n,'письмо','письма','писем')} в ней`:''} будут удалены из ящика на сервере. Копии писем в архиве останутся (папка — в «Только в архиве»).`, {okText:'Удалить папку'});
    if(!ok) return;
    try{ await api('/webmail/folders/delete',{method:'POST', body:{account_id:acc, folder:name}}); toast('Папка удалена', name);
      if(Mail.folder===name) Mail.folder=null; loadMailFolders(acc); }catch(e){ toastErr(e); }
    return;
  }
  const title=kind==='rename'?'Переименовать папку':(kind==='moveto'?`Переместить папку «${mailFolderLabel(f)}»`:(name?`Новая папка в «${mailFolderLabel(f)}»`:'Новая папка'));
  const d=(f&&f.delimiter)||'/';
  const moveTargets=kind==='moveto'?Mail.folders.filter(x=>x.on_server!==false && x.name!==name && !x.name.startsWith(name+d) && x.special!=='trash' && x.special!=='junk'):[];
  const m=modal(title, `<div class="form-row" ${kind==='moveto'?'style="display:none"':''}><label for="mf-name">Имя папки</label><input id="mf-name" type="text" maxlength="200" value="${kind==='rename'||kind==='moveto'?esc(mailFolderLabel(f)):''}"></div>
    ${kind==='moveto'?`<div class="form-row"><label for="mf-parent">Куда</label><select id="mf-parent"><option value="">— на верхний уровень —</option>${moveTargets.map(x=>`<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('')}</select><div class="hint">Письма папки переездут вместе с ней; копии в архиве переименуются.</div></div>`:''}
    ${kind==='create'&&!name?`<div class="form-row"><label for="mf-parent">Внутри папки</label><select id="mf-parent"><option value="">— в корне ящика —</option>${Mail.folders.filter(x=>x.on_server!==false&&x.special!=='trash'&&x.special!=='junk').map(x=>`<option value="${esc(x.name)}">${esc(x.name)}</option>`).join('')}</select></div>`:''}`,
    {footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Сохранить</button>'});
  const inp=m.body.querySelector('#mf-name'); if(kind==='moveto') m.body.querySelector('#mf-parent').focus(); else { inp.focus(); inp.select(); }
  m.foot.querySelector('[data-c]').onclick=m.close;
  const go=async()=>{
    const v=inp.value.trim(); if(!v) return toastErr({message:'Укажите имя папки.'});
    const btn=m.foot.querySelector('[data-ok]'); btn.disabled=true;
    try{
      let r;
      if(kind==='rename') r=await api('/webmail/folders/rename',{method:'POST', body:{account_id:acc, folder:name, name:v}});
      else if(kind==='moveto') r=await api('/webmail/folders/rename',{method:'POST', body:{account_id:acc, folder:name, name:mailFolderLabel(f), parent:(m.body.querySelector('#mf-parent')||{}).value||''}});
      else r=await api('/webmail/folders/create',{method:'POST', body:{account_id:acc, name:v, parent:name||((m.body.querySelector('#mf-parent')||{}).value||'')}});
      m.close(); toast(kind==='rename'?'Папка переименована':(kind==='moveto'?'Папка перемещена':'Папка создана'), r.folder);
      if((kind==='rename'||kind==='moveto') && Mail.folder===name) Mail.folder=r.folder;
      loadMailFolders(acc);
    }catch(e){ btn.disabled=false; toastErr(e); }
  };
  m.foot.querySelector('[data-ok]').onclick=go;
  inp.onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); go(); } };
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
    box.appendChild(h(`<div class="mail-list-bar muted">
      Найдено: ${data.results.length}${data.more?'+':''} · ${S.scope==='all'?'во всех ящиках':'в этом ящике'}</div>`));
  }
  const old=box.querySelector('#msMore'); if(old) old.remove();
  if(!data.results.length && !append){ box.appendChild(h('<div class="empty">Ничего не найдено</div>')); return; }
  data.results.forEach(r=>{
    const el=h(`<div class="msg-item" tabindex="0" role="button">
      ${S.scope==='all'?`<div class="msg-acc">${esc(r.account_name||'')} · ${esc(r.folder)}</div>`:`<div class="msg-acc">${esc(r.folder)}</div>`}
      <div class="msg-top">${pickBox(r.account_id, r.id, r.subject)}<span class="msg-from">${esc(r.from||'—')}</span><span>${r.has_attach?'📎':''}</span><span class="msg-date">${esc(mailWhen(r.date))}</span></div>
      <div class="msg-subj">${esc(r.subject||'(без темы)')}</div>
      ${r.snippet?`<div class="msg-snip">${markSnippet(r.snippet)}</div>`:''}</div>`);
    wirePick(el, r.account_id, r.id, r.subject);
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

// ---------- Список писем ----------
async function loadMailMessages(accId, {keep=false}={}){
  const box=$('#mlist'); if(!box) return;
  Mail.view='folder';
  const scroll=keep?box.scrollTop:0;
  if(!keep) box.innerHTML='<div class="empty" style="padding:20px"><div class="spinner"></div></div>';
  const seq=++Mail.seqList;
  const fi=mailFolderInfo(Mail.folder);
  const showGone=Mail.showGone || !mailOnline() || (fi && fi.on_server===false);
  let data;
  const qs=new URLSearchParams({account_id:String(accId), folder:Mail.folder||'', offset:String(Mail.offset), limit:String(Mail.limit),
    sort:Mail.sort, filter:Mail.filter, show_gone:showGone?'true':'false', threads:mailPrefs().threads?'true':'false'});
  try{ data=await api('/webmail/messages?'+qs.toString(), {bg:keep}); }
  catch(e){ if(seq===Mail.seqList) box.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  if(seq!==Mail.seqList || Mail.acc!==accId) return;
  if(data.offset>0 && !data.messages.length && data.total){ Mail.offset=Math.max(0, Math.floor((data.total-1)/Mail.limit)*Mail.limit); return loadMailMessages(accId, {keep}); }
  Mail.total=data.total; Mail.goneCount=data.gone||0; Mail.items=data.messages;
  renderMailList(accId);
  box.scrollTop=scroll;
}

function renderMailList(accId){
  const box=$('#mlist'); if(!box) return;
  const items=Mail.items;
  const fi=mailFolderInfo(Mail.folder);
  const archiveOnly=fi && fi.on_server===false;
  const out=mailIsOutFolder(Mail.folder);
  box.innerHTML='';
  const bar=h(`<div class="mail-list-bar">
    <div class="row1">${canRestoreMessages()&&items.length?'<input type="checkbox" class="msg-pick" data-pickall aria-label="Выбрать все письма на странице" title="Выбрать все на странице">':''}
    <span class="muted">${fmtNum(Mail.total)} ${mailPrefs().threads?plural(Mail.total||0,'беседа','беседы','бесед'):plural(Mail.total||0,'письмо','письма','писем')}</span>
    <label class="small muted mail-threads-sw" title="Письма одной переписки — одной строкой"><input type="checkbox" data-threads ${mailPrefs().threads?'checked':''}> беседами</label><span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" ${Mail.offset<=0?'disabled':''} data-prev aria-label="Предыдущая страница">←</button>
    <span class="muted">${Math.floor(Mail.offset/Mail.limit)+1}/${Math.max(1,Math.ceil(Mail.total/Mail.limit))}</span>
    <button class="btn ghost sm" ${Mail.offset+Mail.limit>=Mail.total?'disabled':''} data-next aria-label="Следующая страница">→</button></div>
    <div class="row2">
      <select data-sort aria-label="Порядок" ${mailPrefs().threads?'disabled title="В беседах — сначала новые"':''}><option value="date_desc">Сначала новые</option><option value="date_asc">Сначала старые</option><option value="from">По ${out?'получателю':'отправителю'}</option><option value="subject">По теме</option><option value="size">Крупные сначала</option></select>
      <select data-filter aria-label="Отбор"><option value="all">Все</option><option value="unread">Непрочитанные</option><option value="flagged">Помеченные</option><option value="attach">С вложениями</option><option value="answered">Отвеченные</option>${Mail.tags.length?`<optgroup label="С меткой">${Mail.tags.map(t=>`<option value="tag:${esc(t.keyword)}">🏷 ${esc(t.name)}</option>`).join('')}</optgroup>`:''}</select>
      ${mailOnline()&&!archiveOnly&&(Mail.goneCount||Mail.showGone)?`<label class="small muted" title="Письма, которых в этой папке на сервере больше нет (удалены, перенесены) — их копии в архиве"><input type="checkbox" data-gone ${Mail.showGone?'checked':''}> удалённые с сервера (${fmtNum(Mail.goneCount)})</label>`:''}
    </div></div>`);
  box.appendChild(bar);
  bar.querySelector('[data-sort]').value=Mail.sort; bar.querySelector('[data-filter]').value=Mail.filter;
  if(bar.querySelector('[data-filter]').value!==Mail.filter){ Mail.filter='all'; bar.querySelector('[data-filter]').value='all'; }
  bar.querySelector('[data-sort]').onchange=(e)=>{ Mail.sort=e.target.value; Mail.offset=0; loadMailMessages(accId); };
  bar.querySelector('[data-threads]').onchange=(e)=>{ const p=mailPrefs(); p.threads=e.target.checked; mailSavePrefs(p); Mail.offset=0; loadMailMessages(accId); };
  bar.querySelector('[data-filter]').onchange=(e)=>{ Mail.filter=e.target.value; Mail.offset=0; loadMailMessages(accId); };
  const gcb=bar.querySelector('[data-gone]'); if(gcb) gcb.onchange=()=>{ Mail.showGone=gcb.checked; Mail.offset=0; loadMailMessages(accId); };
  bar.querySelector('[data-prev]').onclick=()=>{ Mail.offset=Math.max(0,Mail.offset-Mail.limit); loadMailMessages(accId); };
  bar.querySelector('[data-next]').onclick=()=>{ Mail.offset+=Mail.limit; loadMailMessages(accId); };
  if(archiveOnly) box.appendChild(h('<div class="hint" style="margin:8px">🗄 Этой папки на сервере нет — письма только в архиве. Вернуть их в ящик — «♻️ Восстановить».</div>'));
  if(!items.length){ box.appendChild(h(`<div class="empty">${Mail.filter!=='all'?'Писем по этому отбору нет':'Папка пуста'}</div>`)); return; }
  const all=bar.querySelector('[data-pickall]');
  // список (#mlist) живёт между страницами: прежний обработчик снимаем, иначе они копились бы
  if(box._pickSync){ box.removeEventListener('pickchange', box._pickSync); box._pickSync=null; }
  if(all){
    const sync=()=>{ const n=items.filter(m=>Mail.picked.has(accId+':'+m.id)).length;
      all.checked=n===items.length; all.indeterminate=n>0&&n<items.length; };
    sync();
    all.onchange=()=>{
      items.forEach(m=>{ const k=accId+':'+m.id; if(all.checked) Mail.picked.set(k, m.thread_ids&&m.thread_ids.length>1?{acc:accId,id:m.id,subject:m.subject,ids:m.thread_ids}:{acc:accId,id:m.id,subject:m.subject}); else Mail.picked.delete(k); });
      box.querySelectorAll('input[data-pick]').forEach(x=>{ x.checked=all.checked; const it=x.closest('.msg-item'); if(it) it.classList.toggle('picked', all.checked); });
      renderPickBar();
    };
    box._pickSync=sync;
    box.addEventListener('pickchange', sync);
  }
  const act=mailCanAct();
  items.forEach(m=>{
    const who=out?(m.to?'Кому: '+m.to:(m.from||'—')):(m.from||'—');
    const picked=Mail.picked.has(accId+':'+m.id);
    const el=h(`<div class="msg-item ${m.seen?'':'unread'} ${m.gone?'gone':''} ${m.id===Mail.cur?'active':''} ${picked?'picked':''}" data-id="${m.id}" tabindex="0" role="button" title="${esc(m.size_h)}" ${act&&!m.gone?'draggable="true"':''}>
      <div class="msg-top">${pickBox(accId, m.id, m.subject)}
        <button class="msg-star ${m.flagged?'on':''}" ${act&&!m.gone?'':'disabled'} title="${m.flagged?'Снять пометку':'Пометить'} (s)" aria-label="Пометка">${m.flagged?'★':'☆'}</button>
        <span class="msg-from">${esc(who)}</span>${m.thread_count>1?`<span class="msg-cnt" title="Писем в беседе: ${m.thread_count}${m.thread_unread?`, непрочитанных: ${m.thread_unread}`:''}">${m.thread_count}</span>`:''}<span>${m.has_attach?'📎':''}</span><span class="msg-date" title="${esc(fmtDate(m.date))}">${esc(mailWhen(m.date))}</span></div>
      <div class="msg-subj">${m.answered?'<span class="msg-ic" title="Отвечено">↩</span>':''}${m.forwarded?'<span class="msg-ic" title="Переслано">↪</span>':''}${esc(m.subject)}</div>
      ${mailTagChips(m.keywords)}
      ${m.gone?'<div class="msg-meta"><span class="rp-tag" title="Письма в этой папке на сервере больше нет — копия в архиве">нет на сервере</span></div>':''}</div>`);
    wirePick(el, accId, m.id, m.subject, m.thread_ids);
    const star=el.querySelector('.msg-star');
    star.onclick=(e)=>{ e.stopPropagation(); if(star.disabled) return; mailAct('flags', [m.id], m.flagged?{remove:['\\Flagged']}:{add:['\\Flagged']}); };
    el.onclick=()=>{ box.querySelectorAll('.msg-item').forEach(x=>x.classList.remove('active')); el.classList.add('active'); loadMailMessage(accId, m.id); };
    el.onkeydown=(e)=>{
      if(e.key==='Enter'){ e.preventDefault(); el.click(); }
      else if(e.key===' ' && !e.target.closest('input')){ e.preventDefault(); const cb=el.querySelector('input[data-pick]'); if(cb){ cb.checked=!cb.checked; cb.onchange(); } }
      else if(e.key==='ArrowDown' && el.nextElementSibling){ e.preventDefault(); el.nextElementSibling.focus(); }
      else if(e.key==='ArrowUp' && el.previousElementSibling && el.previousElementSibling.classList.contains('msg-item')){ e.preventDefault(); el.previousElementSibling.focus(); }
    };
    el.addEventListener('dragstart', (e)=>{
      const ids=Mail.picked.has(accId+':'+m.id)?mailPickedIds():(m.thread_ids||[m.id]);
      e.dataTransfer.setData('text/x-mail-ids', JSON.stringify(ids)); e.dataTransfer.effectAllowed='move';
    });
    box.appendChild(el);
  });
}

// ---------- Неотправленные (черновики клиента) ----------
/** «Все входящие» (1.22.0): письма «Входящих» всех ящиков сотрудника, новые сверху; клик открывает письмо в его ящике. */
async function mailShowUnified(offset){
  const box=$('#mlist'); if(!box) return;
  Mail.view='unified'; offset=offset||0;
  let r; try{ r=await api(`/webmail/unified?offset=${offset}&limit=50`); }catch(e){ toastErr(e); return; }
  if(!offset){ box.innerHTML='';
    box.appendChild(h(`<div class="mail-list-bar muted">Все входящие: ${fmtNum(r.total)} ${plural(r.total,'письмо','письма','писем')} в ${fmtNum(myAccounts().length)} ${plural(myAccounts().length,'ящике','ящиках','ящиках')} (из архива — новые письма появятся после проверки почты в каждом ящике).</div>`)); }
  else box.querySelector('[data-more]')?.remove();
  if(!r.items.length && !offset){ box.appendChild(h('<div class="empty">Писем нет</div>')); return; }
  r.items.forEach(m=>{
    const el=h(`<div class="msg-item ${m.seen?'':'unread'}" tabindex="0" role="button">
      <div class="msg-top"><span class="msg-from">${esc(m.from||'')}</span><span>${m.has_attach?'📎':''}</span><span class="msg-date">${esc(mailWhen(m.date))}</span></div>
      <div class="msg-subj">${esc(m.subject||'(без темы)')}</div>
      <div class="msg-meta"><span class="rp-tag" title="Ящик">${esc(m.account)}</span></div></div>`);
    el.onclick=()=>openMailMessage(m.account_id, m.id, m.folder);
    el.onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  });
  if(r.more){ const b=h('<div style="text-align:center;margin:10px"><button class="btn" data-more>Показать ещё</button></div>'); b.querySelector('button').onclick=()=>mailShowUnified(offset+r.items.length); box.appendChild(b); }
}

async function mailShowDrafts(accId){
  const box=$('#mlist'); if(!box) return;
  Mail.view='drafts';
  try{ const d=await api(`/webmail/drafts?account_id=${accId}`); Mail.drafts=d.items||[]; }catch(e){ toastErr(e); return; }
  box.innerHTML='';
  box.appendChild(h(`<div class="mail-list-bar muted">Неотправленные письма: ${fmtNum(Mail.drafts.length)}. Хранятся в архиве, пока их не отправят или не удалят.</div>`));
  if(!Mail.drafts.length){ box.appendChild(h('<div class="empty">Неотправленных писем нет</div>')); return; }
  Mail.drafts.forEach(d=>{
    const el=h(`<div class="msg-item" tabindex="0" role="button">
      <div class="msg-top"><span class="msg-from">${esc(d.to_addrs?'Кому: '+d.to_addrs:'(без получателя)')}</span><span>${d.attachments.length?'📎':''}</span><span class="msg-date">${esc(mailWhen(d.updated_at))}</span></div>
      <div class="msg-subj">${esc(d.subject||'(без темы)')}</div>
      ${d.error?`<div class="small rp-red">Не отправлено: ${esc(trunc(d.error,140))}</div>`:''}
      ${d.status==='scheduled'?`<div class="small mail-sched">🕓 Будет отправлено ${esc(fmtDate(d.send_at))}</div>`:''}
      <div class="btn-row" style="margin-top:6px"><button class="btn sm" data-open>✏️ ${d.status==='scheduled'?'Изменить':'Продолжить'}</button>${d.status==='scheduled'?'<button class="btn sm" data-unsched>⏹ Отменить отправку</button>':''}<button class="btn sm ghost" data-del>🗑 Удалить</button></div></div>`);
    el.querySelector('[data-open]').onclick=(e)=>{ e.stopPropagation(); composeWindow(d); };
    const us=el.querySelector('[data-unsched]');
    if(us) us.onclick=async(e)=>{ e.stopPropagation(); try{ await api(`/webmail/drafts/${d.id}/unschedule`,{method:'POST'}); toast('Отправка отменена', 'Письмо осталось в «Неотправленных».'); mailShowDrafts(accId); }catch(err){ toastErr(err); } };
    el.querySelector('[data-del]').onclick=async(e)=>{ e.stopPropagation();
      if(!await confirmDlg('Удалить черновик?', `«${d.subject||'без темы'}» и его вложения будут удалены.`, {okText:'Удалить'})) return;
      try{ await api(`/webmail/drafts/${d.id}`,{method:'DELETE'}); mailShowDrafts(accId); renderMailFolders(accId); }catch(err){ toastErr(err); } };
    el.onclick=()=>composeWindow(d);
    el.onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); composeWindow(d); } };
    box.appendChild(el);
  });
  renderMailFolders(accId);
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

function fmtDateShort(iso){ if(!iso) return ''; try{ const d=new Date(iso); return d.toLocaleDateString(UI_LOCALE,{day:'2-digit',month:'2-digit',year:'2-digit'}); }catch(e){ return ''; } }

// ---------- Письмо ----------
async function loadMailMessage(accId, pk){
  const box=$('#mreader'); if(!box) return;
  box.innerHTML='<div class="empty" style="padding:40px"><div class="spinner"></div></div>';
  // на узком экране письмо стоит под списком: без прокрутки нажатие на письмо выглядело как «ничего не произошло»
  if(window.matchMedia && matchMedia('(max-width:1100px)').matches) box.scrollIntoView({block:'start', behavior:'smooth'});
  const seq=++Mail.seqMsg;
  let m;
  try{ m=await api(`/accounts/${accId}/messages/${pk}`); }catch(e){ if(seq===Mail.seqMsg) box.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
  if(seq!==Mail.seqMsg) return;
  Mail.cur=pk; Mail.msg=m; Mail.msgAcc=accId;
  const item=Mail.items.find(x=>x.id===pk) || {id:pk, seen:(m.flags||[]).includes('\\Seen'), flagged:(m.flags||[]).includes('\\Flagged'), gone:!!m.gone, date:m.date};
  const gone=!!(item.gone || m.gone);
  const here=accId===Mail.acc;
  const act=here && mailCanAct() && !gone;
  const send=here && mailCanSend() && !gone;
  const inDrafts=(mailFolderInfo(m.folder)||{}).special==='drafts';
  const hd=m.headers||{};
  // Картинки, уже показанные в тексте письма (cid:), в списке вложений не дублируем.
  const visibleAtts=(m.attachments||[]).filter(a=>!a.inline);
  const attUrl=a=>`/api/accounts/${accId}/messages/${pk}/attachment/${a.index}`;
  const atts=visibleAtts.map(a=>{
    const img=/^image\/(png|jpe?g|gif|webp)$/i.test(a.content_type||'') && (a.size||0)<5*1024*1024;
    const prev=mailPreviewKind(a);
    return `<span class="attach-wrap"><a class="attach-chip ${img?'img':''}" href="${attUrl(a)}" download ${prev?`data-prev="${a.index}"`:''} title="${prev?'Открыть':'Скачать'} «${esc(a.filename)}»">${img?`<img src="${attUrl(a)}" alt="" loading="lazy">`:'📎'} ${esc(a.filename)} <span class="muted">(${fmtBytes(a.size)})</span></a>${prev?`<a class="attach-dl" href="${attUrl(a)}" download title="Скачать «${esc(a.filename)}»" aria-label="Скачать «${esc(a.filename)}»">⬇</a>`:''}</span>`; }).join('');
  const hasText=!!(m.text&&m.text.trim()), hasHtml=!!(m.html&&m.html.trim());
  const sendTip=send?'':esc((Mail.st&&Mail.st.send_problem)||(gone?'Письма на сервере больше нет':'Отправка недоступна'));
  box.innerHTML='';
  const el=h(`<div>
    <div class="mail-tb">
      ${inDrafts&&act?`<button class="btn sm primary" data-c="edit" ${send?'':'disabled'} title="${sendTip||'Продолжить черновик'}">✏️ Продолжить</button>`:''}
      <button class="btn sm ${inDrafts?'':'primary'}" data-c="reply" ${send?'':'disabled'} title="${sendTip||'Ответить (r)'}">Ответить</button>
      <button class="btn sm" data-c="reply_all" ${send?'':'disabled'} title="${sendTip||'Ответить всем (a)'}">Всем</button>
      <button class="btn sm" data-c="forward" ${send?'':'disabled'} title="${sendTip||'Переслать (f)'}">Переслать</button>
      <button class="btn sm ghost" data-c="star" ${act?'':'disabled'} title="Пометить (s)">☆</button>
      <button class="btn sm ghost" data-c="unread" ${act?'':'disabled'} title="Отметить непрочитанным (u)">📩</button>
      <button class="btn sm ghost" data-c="move" ${act?'':'disabled'} title="Переместить в папку (v)">📁</button>
      <button class="btn sm ghost" data-c="archive" ${act?'':'disabled'} title="В архив (e)">🗄</button>
      <button class="btn sm ghost" data-c="tag" ${act?'':'disabled'} title="Метка (l)">🏷</button>
      <button class="btn sm ghost" data-c="snooze" ${act?'':'disabled'} title="Напомнить позже (b)">⏰</button>
      <button class="btn sm ghost danger" data-c="delete" ${act?'':'disabled'} title="Удалить (Delete)">🗑</button>
      <button class="btn sm ghost mail-tb-more" data-c="more" title="Ещё: скачать, заголовки, печать, переслать вложением, правило, блокировка, восстановить" aria-label="Ещё">⋯</button>
    </div>
    <div class="rhead">
      <div class="rsubj">${esc(hd.subject||'(без темы)')}<span id="mprio"></span></div>
      <div id="mtags">${mailTagChips(item.keywords)}</div>
      <div class="rmeta"><b>От:</b> ${esc(hd.from||'—')} <span id="mfromtags"></span></div>
      <div class="rmeta"><b>Кому:</b> ${esc(hd.to||'—')}</div>
      ${hd.cc?`<div class="rmeta"><b>Копия:</b> ${esc(hd.cc)}</div>`:''}
      <div class="rmeta"><b>Дата:</b> <span title="${esc(hd.date||'')}">${esc(item.date?fmtDate(item.date):(hd.date||''))}</span> · <span class="muted">📁 ${esc(mailFolderLabel(mailFolderInfo(m.folder))||m.folder||'')}</span>${gone?' · <span class="rp-tag" title="Письма в этой папке на сервере больше нет — копия в архиве">нет на сервере</span>':''}</div>
      ${m.journal?`<div class="rmeta small muted" title="Письмо принято по журналированию (SMTP)">📨 Из журнала: ${esc(fmtDate(m.journal.received_at))}, с ${esc(m.journal.ip)}${m.journal.helo?` (${esc(m.journal.helo)})`:''}${m.journal.auth_user?`, вход «${esc(m.journal.auth_user)}»`:''}${m.journal.client_cert?`, сертификат ${esc(m.journal.client_cert)}`:''}${m.journal.tls?', TLS':', без шифрования'}</div>`:''}
    </div>
    <div id="mthread"></div>
    ${(m.truncated||m.notice)?`<div class="hint" style="margin-bottom:12px;border-color:var(--warn)">📦 ${esc(m.notice||'Письмо слишком большое для показа в браузере.')}</div>`:''}
    <div id="mextras"></div>
    ${atts?`<div class="mail-atts"><div class="muted small" style="margin-bottom:4px">Вложения (${visibleAtts.length})${visibleAtts.length>1?` · <a href="/api/webmail/attachments_zip?account_id=${accId}&id=${pk}" download>⬇ все одним архивом .zip</a>`:''}:</div>${atts}</div>`:''}
    ${(hasText&&hasHtml)?`<div class="body-tabs"><button class="btn sm primary" data-tab="text">Текст</button><button class="btn sm" data-tab="html">HTML</button></div>`:''}
    <div id="mbody"></div>
  </div>`);
  box.appendChild(el);
  const tb=el.querySelector('.mail-tb');
  const go=(c, e)=>{
    if(c==='reply'||c==='reply_all'||c==='forward') return mailCompose(c, pk);
    if(c==='edit') return mailCompose('edit', pk);
    if(c==='star') return mailAct('flags', [pk], item.flagged?{remove:['\\Flagged']}:{add:['\\Flagged']});
    if(c==='unread') return mailAct('flags', [pk], item.seen?{remove:['\\Seen']}:{add:['\\Seen']});
    if(c==='move') return mailMovePicker(e.currentTarget, [pk]);
    if(c==='tag') return mailTagPicker(e.currentTarget, [pk]);
    if(c==='archive') return mailAct('archive', [pk]);
    if(c==='snooze') return mailSnoozePicker(e.currentTarget, [pk]);
    if(c==='junk') return mailAct('junk', [pk], {spam:(mailFolderInfo(m.folder)||{}).special!=='junk'});
    if(c==='delete') return mailDelete([pk]);
    if(c==='more') return mailMoreMenu(e.currentTarget, accId, pk, hd, gone);
  };
  tb.querySelectorAll('[data-c]').forEach(b=>b.onclick=(e)=>go(b.dataset.c, e));
  Mail.readerGo=go;
  mailReaderFlags();
  el.querySelectorAll('[data-prev]').forEach(a=>a.onclick=(e)=>{ e.preventDefault();
    const att=visibleAtts.find(x=>String(x.index)===a.dataset.prev); if(att) mailPreview(att, attUrl(att), visibleAtts.filter(mailPreviewKind), attUrl); });
  Mail.extras=null;
  api(`/webmail/extras?account_id=${accId}&id=${pk}`, {bg:true}).then(x=>{ if(seq===Mail.seqMsg) mailRenderExtras(el, accId, pk, x, {act, send}); }).catch(()=>{});
  if(send && !inDrafts && !mailIsOutFolder(m.folder)) mailQuickReply(el, accId, pk);
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
    // обратиться к API. allow-popups — чтобы ссылки открывались в новой вкладке.
    f.setAttribute('sandbox','allow-popups allow-popups-to-escape-sandbox');
    f.setAttribute('referrerpolicy','no-referrer');
    f.srcdoc=doc.srcdoc; bodyBox.appendChild(f);
  };
  // HTML-письмо показываем как HTML: у многих писем текстовая часть — пустая заглушка
  if(m.truncated){ /* тело не разбиралось — показываем только шапку и подсказку */ }
  else if(hasHtml){ showHtml(); const t=el.querySelectorAll('[data-tab]'); t.forEach(x=>x.className='btn sm'+(x.dataset.tab==='html'?' primary':'')); }
  else showText();
  el.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{ el.querySelectorAll('[data-tab]').forEach(x=>x.className='btn sm'); b.className='btn sm primary'; b.dataset.tab==='html'?showHtml():showText(); });
  // переписка
  api(`/webmail/thread?account_id=${accId}&id=${pk}`, {bg:true}).then(t=>{
    if(seq!==Mail.seqMsg || !t.items || t.items.length<2) return;
    const th=el.querySelector('#mthread');
    th.appendChild(h(`<details class="mail-thread" ${t.items.length<=6?'open':''}><summary>🧵 Переписка: ${t.items.length} ${plural(t.items.length,'письмо','письма','писем')}</summary>
      ${t.items.map(x=>`<div class="mail-thread-item ${x.current?'cur':''} ${x.seen?'':'unread'} ${x.gone?'gone':''}" data-id="${x.id}" tabindex="0" role="button"><span class="d">${esc(mailWhen(x.date))}</span><span class="f">${esc(trunc(x.from,40))}</span><span class="s">${esc(trunc(x.subject||'(без темы)',80))}</span><span class="muted small">${esc(x.folder)}</span></div>`).join('')}</details>`));
    th.querySelectorAll('[data-id]').forEach(r=>{ const id=parseInt(r.dataset.id); if(id===pk) return;
      r.onclick=()=>loadMailMessage(accId, id); r.onkeydown=(e)=>{ if(e.key==='Enter') loadMailMessage(accId, id); }; });
  }).catch(()=>{});
  // прочитано: сотрудник в своём ящике — как в любом почтовом клиенте (администратор отметок не меняет)
  if(Mail.st && Mail.st.mark_read && here && !item.seen && !gone){
    setTimeout(()=>{ if(Mail.cur===pk && seq===Mail.seqMsg) mailAct('flags', [pk], {add:['\\Seen']}, {quiet:true}); }, 1200);
  }
}

/** Пометка и «прочитано» в панели письма — по текущему состоянию письма в списке. */
function mailReaderFlags(){
  const rd=$('#mreader'); if(!rd || !Mail.cur) return;
  const item=Mail.items.find(x=>x.id===Mail.cur); if(!item) return;
  const star=rd.querySelector('[data-c="star"]'); if(star){ star.textContent=item.flagged?'★':'☆'; star.classList.toggle('on', !!item.flagged); star.title=item.flagged?'Снять пометку (s)':'Пометить (s)'; }
  const un=rd.querySelector('[data-c="unread"]'); if(un){ un.textContent=item.seen?'📩':'✉️'; un.title=item.seen?'Отметить непрочитанным (u)':'Отметить прочитанным (u)'; }
}

function mailMoreMenu(anchor, accId, pk, hd, gone){
  document.querySelectorAll('.mail-pop').forEach(x=>x.remove());
  const X=(Mail.cur===pk && Mail.extras)||{};
  const act=mailCanAct() && !gone && accId===Mail.acc;
  const pop=h(`<div class="mail-pop" role="menu"><div class="mail-pop-list">
    <a class="mail-pop-item" href="/api/accounts/${accId}/messages/${pk}/raw" download>⬇ Скачать .eml</a>
    <button class="mail-pop-item" data-a="headers">📋 Заголовки письма</button>
    <button class="mail-pop-item" data-a="print">🖨 Печать</button>
    ${mailCanSend()&&accId===Mail.acc?'<button class="mail-pop-item" data-a="fwdatt">↪ Переслать как вложение (.eml)</button>':''}
    ${X.unsubscribe&&(X.unsubscribe.url||X.unsubscribe.mailto)?'<button class="mail-pop-item" data-a="unsub">✋ Отписаться от рассылки</button>':''}
    ${act&&X.sender&&!X.in_book?'<button class="mail-pop-item" data-a="book">👤 Добавить отправителя в адресную книгу</button>':''}
    ${act?'<button class="mail-pop-item" data-a="rule">⚙️ Создать правило по этому письму</button>':''}
    ${act&&X.sender&&!X.blocked?'<button class="mail-pop-item" data-a="block">⛔ Заблокировать отправителя</button>':''}
    ${act&&Mail.special.junk?`<button class="mail-pop-item" data-a="junk">🚫 ${(mailFolderInfo(Mail.msg&&Mail.msg.folder)||{}).special==='junk'?'Не спам — во «Входящие»':'В спам'}</button>`:''}
    ${act?'<button class="mail-pop-item danger" data-a="phish">🎣 Сообщить о фишинге</button>':''}
    ${canRestoreMessages()?'<button class="mail-pop-item" data-a="restore">♻️ Восстановить письмо</button>':''}
    ${mailCanAct()&&!gone&&accId===Mail.acc?'<button class="mail-pop-item danger" data-a="erase">🗑 Удалить с сервера насовсем</button>':''}</div></div>`);
  document.body.appendChild(pop);
  const r=anchor.getBoundingClientRect();
  pop.style.top=(r.bottom+4+window.scrollY)+'px'; pop.style.left=Math.max(8, Math.min(window.innerWidth-290, r.right+window.scrollX-280))+'px';
  const close=()=>pop.remove();
  pop.querySelectorAll('a').forEach(a=>a.addEventListener('click', ()=>setTimeout(close, 0)));
  pop.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
    close();
    const a=b.dataset.a;
    if(a==='restore') messagesRestoreModal([{acc:accId, id:pk, subject:hd.subject||''}]);
    else if(a==='fwdatt') mailCompose('forward_attach', null, {ref_ids:[pk]});
    else if(a==='unsub') mailUnsubscribe(X.unsubscribe);
    else if(a==='book') mailAddToBook(accId, hd.from||X.sender);
    else if(a==='rule') mailRuleEditor(accId, null, {name:trunc(hd.subject||X.sender||'Правило',60), conditions:[{field:'from', op:'equals', value:X.sender||''}], actions:[{type:'move', folder:''}]});
    else if(a==='block') mailBlockSender(accId, X.sender, [pk]);
    else if(a==='phish') mailReportPhish(accId, [pk]);
    else if(a==='junk') mailAct('junk', [pk], {spam:(mailFolderInfo(Mail.msg&&Mail.msg.folder)||{}).special!=='junk'});
    else if(a==='erase') mailDelete([pk], {permanent:true});
    else if(a==='print') window.print();
    else if(a==='headers'){
      try{ const d=await api(`/webmail/headers?account_id=${accId}&id=${pk}`);
        modal('Заголовки письма', `<div class="muted small" style="margin-bottom:6px">Папка «${esc(d.folder)}», UID ${esc(String(d.uid))}${d.gone?' · на сервере письма больше нет':''}</div><pre class="mail-headers">${esc(d.headers)}</pre>`, {wide:true});
      }catch(e){ toastErr(e); }
    }
  });
  const off=(e)=>{ if(!pop.contains(e.target)){ close(); document.removeEventListener('mousedown', off); } };
  setTimeout(()=>document.addEventListener('mousedown', off), 0);
}

// ---------- Новое письмо, ответ, пересылка ----------
async function mailCompose(mode, refId, extra={}){
  if(!mailCanSend()) return toast('Отправка недоступна', (Mail.st&&Mail.st.send_problem)||'', 'warn', 7000);
  let d;
  try{ d=await api('/webmail/drafts', {method:'POST', body:Object.assign({account_id:Mail.acc, mode, ref_id:refId||null}, extra)}); }
  catch(e){ return toastErr(e); }
  Mail.drafts.unshift(d);
  composeWindow(d);
}

/** Окно письма: адреса с подсказкой, тема, редактор, вложения, автосохранение, отправка. */
async function composeWindow(draft){
  const existing=document.querySelector(`.compose[data-draft="${draft.id}"]`);
  if(existing){ existing.classList.remove('min'); existing.hidden=false; const b=existing.querySelector('.compose-body'); if(b) b.focus(); return; }
  if(draft.status==='scheduled'){
    if(!await confirmDlg('Письмо запланировано к отправке', `Оно уйдёт ${fmtDate(draft.send_at)}. Чтобы изменить письмо, отправку нужно отменить — потом её можно запланировать снова.`, {okText:'Отменить отправку и изменить', okClass:'primary'})) return;
    try{ draft=await api(`/webmail/drafts/${draft.id}/unschedule`, {method:'POST'}); }catch(e){ return toastErr(e); }
  }
  document.querySelectorAll('.compose').forEach(x=>x.classList.add('min'));   // одно окно открыто, остальные свёрнуты
  const accs=isMailbox()?myAccounts():(State.accounts||[]);
  const fromAcc=accs.find(a=>a.id===draft.account_id)||{id:draft.account_id, name:''};
  const fromOpts=isMailbox()&&accs.length>1
    ? `<select data-f="account_id" aria-label="От">${accs.map(a=>`<option value="${a.id}" ${a.id===draft.account_id?'selected':''}>${esc(a.name)}</option>`).join('')}</select>`
    : `<span class="compose-from">${esc(fromAcc.name||'')}</span>`;
  const titles={new:'Новое письмо', reply:'Ответ', reply_all:'Ответ всем', forward:'Пересылка', edit:'Черновик'};
  const choices=(draft.account_id===Mail.acc && Mail.st && Mail.st.identity && Mail.st.identity.from_choices)||[];
  const aliasSel=choices.length>1?`<select data-f="from_addr" aria-label="Адрес отправителя" title="С какого адреса отправить">${choices.map(a=>`<option value="${esc(a)}" ${(draft.from_addr||choices[0]).toLowerCase()===a.toLowerCase()?'selected':''}>${esc(a)}</option>`).join('')}</select>`:'';
  const w=h(`<div class="compose" data-draft="${draft.id}" role="dialog" aria-label="Письмо">
    <div class="compose-head"><span class="t">${esc(titles[draft.mode]||'Письмо')}${draft.subject?': '+esc(trunc(draft.subject,50)):''}</span>
      <span class="compose-state muted small" aria-live="polite"></span>
      <button class="x" data-min title="Свернуть" aria-label="Свернуть">▁</button><button class="x" data-max title="Развернуть" aria-label="Развернуть">⤢</button><button class="x" data-close title="Закрыть (черновик сохранится)" aria-label="Закрыть">×</button></div>
    <div class="compose-inner">
      <div class="compose-row"><label>От</label>${fromOpts}${aliasSel}</div>
      <div class="compose-row"><label for="cw-to-${draft.id}">Кому</label><input id="cw-to-${draft.id}" data-f="to_addrs" type="text" autocomplete="off" placeholder="адрес или имя" value="${esc(draft.to_addrs||'')}">
        <span class="compose-links"><a href="#" data-show="cc">Копия</a> <a href="#" data-show="bcc">Скрытая</a></span></div>
      <div class="compose-row" data-row="cc" ${draft.cc_addrs?'':'hidden'}><label for="cw-cc-${draft.id}">Копия</label><input id="cw-cc-${draft.id}" data-f="cc_addrs" type="text" autocomplete="off" value="${esc(draft.cc_addrs||'')}"></div>
      <div class="compose-row" data-row="bcc" ${draft.bcc_addrs?'':'hidden'}><label for="cw-bcc-${draft.id}">Скрытая</label><input id="cw-bcc-${draft.id}" data-f="bcc_addrs" type="text" autocomplete="off" value="${esc(draft.bcc_addrs||'')}"></div>
      <div class="compose-row"><label for="cw-subj-${draft.id}">Тема</label><input id="cw-subj-${draft.id}" data-f="subject" type="text" maxlength="998" value="${esc(draft.subject||'')}"></div>
      <div class="compose-toolbar" role="toolbar" aria-label="Оформление">
        <button data-cmd="bold" title="Жирный (Ctrl+B)"><b>Ж</b></button><button data-cmd="italic" title="Курсив (Ctrl+I)"><i>К</i></button><button data-cmd="underline" title="Подчёркнутый (Ctrl+U)"><u>Ч</u></button><button data-cmd="strikeThrough" title="Зачёркнутый"><s>З</s></button>
        <span class="sep"></span><button data-cmd="insertUnorderedList" title="Список">• ≡</button><button data-cmd="insertOrderedList" title="Нумерованный список">1. ≡</button><button data-cmd="quote" title="Цитата">❝</button>
        <span class="sep"></span><button data-cmd="link" title="Ссылка">🔗</button><button data-cmd="removeFormat" title="Убрать оформление">⌫</button>
        <span class="sep"></span><button data-tpl title="Вставить шаблон или сохранить письмо как шаблон">📄 Шаблон</button>
        <span class="spacer" style="flex:1"></span><button class="btn ghost sm" data-archived title="Приложить письма из архива (.eml)">🗄 Из архива</button><label class="btn ghost sm compose-attach" title="Прикрепить файлы">📎 Прикрепить<input type="file" multiple hidden></label></div>
      <div class="compose-body" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Текст письма" spellcheck="true" lang="ru"></div>
      <div class="compose-atts"></div>
      <div class="compose-smime small" hidden></div>
      <div class="compose-foot"><span class="compose-sendgrp"><button class="btn primary" data-send title="Отправить (Ctrl+Enter)">📨 Отправить</button><button class="btn primary" data-later title="Отправить позже — в назначенное время, даже если клиент закрыт" aria-label="Отправить позже">🕓</button></span>
        <button class="btn ghost sm" data-save>💾 Сохранить</button>
        <label class="compose-opt" title="Письмо придёт с пометкой «Важное»"><input type="checkbox" data-opt="priority" ${draft.priority?'checked':''}> ❗ Важное</label>
        <label class="compose-opt" title="Попросить получателя прислать уведомление о прочтении"><input type="checkbox" data-opt="receipt" ${draft.receipt?'checked':''}> ✅ Уведомить о прочтении</label>
        <label class="compose-opt" data-smime hidden title="Подписать письмо сертификатом ящика (S/MIME): получатель увидит, что письмо от вас и не изменено"><input type="checkbox" data-opt="smime_sign" ${draft.smime_sign?'checked':''}> 🔏 Подписать</label>
        <label class="compose-opt" title="Зашифровать письмо (S/MIME): прочитать его смогут только получатели со своим сертификатом"><input type="checkbox" data-opt="smime_encrypt" ${draft.smime_encrypt?'checked':''}> 🔒 Зашифровать</label>
        <span class="spacer" style="flex:1"></span>
        <button class="btn ghost sm danger" data-discard title="Удалить черновик">🗑</button></div>
    </div>
    <div class="compose-ac" hidden></div></div>`);
  document.body.appendChild(w);
  const g=s=>w.querySelector(s);
  const body=g('.compose-body');
  body.innerHTML=draft.body||'';               // сервер уже очистил HTML (sanitize_html), CSP не даст выполнить код
  const state=g('.compose-state');
  let atts=(draft.attachments||[]).slice();
  let dirty=false, saving=null, timer=null, closed=false, sending=false;
  const maxMb=(Mail.st&&Mail.st.max_attachments_mb)||25;
  const renderAtts=()=>{
    const used=atts.reduce((s,a)=>s+(a.size||0),0);
    g('.compose-atts').innerHTML=atts.map(a=>`<span class="attach-chip">📎 ${esc(a.name)} <span class="muted">(${fmtBytes(a.size)})</span>${a.uploading?' <span class="spinner"></span>':`<button class="x" data-n="${a.n}" title="Убрать вложение" aria-label="Убрать вложение «${esc(a.name)}»">×</button>`}</span>`).join('')
      + (atts.length?`<span class="muted small">всего ${fmtBytes(used)} из ${maxMb} МБ</span>`:'');
    g('.compose-atts').querySelectorAll('[data-n]').forEach(b=>b.onclick=async()=>{
      try{ await api(`/webmail/drafts/${draft.id}/attachments/${b.dataset.n}`, {method:'DELETE'}); atts=atts.filter(x=>String(x.n)!==b.dataset.n); renderAtts(); }catch(e){ toastErr(e); } });
  };
  renderAtts();
  // S/MIME: чьими сертификатами зашифруется письмо — с отпечатками; нет сертификата или он недавно сменился —
  // видно до отправки (1.21.0)
  const smimeHint=async()=>{
    const box=g('.compose-smime'), on=g('[data-opt="smime_encrypt"]');
    if(!on || !on.checked){ box.hidden=true; return; }
    const emails=['to_addrs','cc_addrs','bcc_addrs'].map(f=>((g(`[data-f="${f}"]`)||{}).value||'').trim()).filter(Boolean).join(', ');
    let r; try{ r=await api(`/webmail/smime/recipients?account_id=${draft.account_id}&emails=${encodeURIComponent(emails)}`, {bg:true}); }catch(e){ return; }
    const certs=(r.certs||[]).map(c=>`<span class="attach-chip" title="${esc(c.name||'')} · до ${esc(fmtDateShort(c.valid_until))}">🔒 ${esc(c.email)} <span class="mono muted">${esc(c.fingerprint)}</span>${c.changed_at?` <b style="color:var(--warn-text)">⚠ сертификат сменился ${esc(fmtDateShort(c.changed_at))}</b>`:''}</span>`).join(' ');
    const miss=(r.missing||[]).length?`<div style="color:var(--danger)">Нет сертификата: ${esc(r.missing.join(', '))} — такое письмо не отправится зашифрованным.</div>`:'';
    const bcc=((g('[data-f="bcc_addrs"]')||{}).value||'').trim()?'<div class="muted">Скрытым адресатам уйдут отдельные зашифрованные копии — в основном письме их не видно.</div>':'';
    box.innerHTML=(certs?`<div>Шифруется для: ${certs}</div>`:'')+miss+bcc; box.hidden=!(certs||miss||bcc);
  };
  g('[data-opt="smime_encrypt"]').addEventListener('change', smimeHint);
  ['to_addrs','cc_addrs','bcc_addrs'].forEach(f=>{ const el=g(`[data-f="${f}"]`); if(el) el.addEventListener('change', smimeHint); });
  smimeHint();
  // S/MIME: «Подписать» — только если у ящика есть сертификат с ключом
  api(`/webmail/smime?account_id=${draft.account_id}`, {bg:true}).then(r=>{ const k=r.key||{}; const lab=g('[data-smime]'); if(lab && k.present && !k.expired) lab.hidden=false; }).catch(()=>{});
  g('[data-archived]').onclick=async(e)=>{ e.preventDefault();
    const ids=await archivePickerDlg(draft.account_id); if(!ids||!ids.length) return;
    try{ const r=await api(`/webmail/drafts/${draft.id}/archived`, {method:'POST', body:{message_ids:ids}}); atts.push(...r.items); renderAtts(); toast('Приложено', `Писем: ${r.items.length}`); }
    catch(err){ toastErr(err); } };
  const fields=()=>{ const o={body:body.innerHTML}; w.querySelectorAll('[data-f]').forEach(x=>{ o[x.dataset.f]=x.dataset.f==='account_id'?parseInt(x.value):x.value; });
    w.querySelectorAll('[data-opt]').forEach(x=>{ o[x.dataset.opt]=x.checked; }); return o; };
  let serverDirty=false;
  const save=async(manual, toServer)=>{
    if(closed && !manual) return;
    if(saving) await saving;
    if(!dirty && !manual && !(toServer && serverDirty)) return;
    dirty=false;
    state.textContent='Сохраняю…';
    const body=fields(); if(toServer && serverDirty){ body.to_server=true; serverDirty=false; }
    saving=api(`/webmail/drafts/${draft.id}`, {method:'PUT', body, bg:!manual})
      .then(d=>{ const srv=d.server; delete d.server; Object.assign(draft, d); state.textContent='Сохранено '+new Date().toLocaleTimeString(UI_LOCALE,{hour:'2-digit',minute:'2-digit'})+(srv&&srv.saved?' (и на сервере)':'');
        if(srv && srv.warning && manual) toast('Черновик сохранён только здесь', srv.warning, 'warn', 7000); })
      .catch(e=>{ dirty=true; state.textContent='Не сохранено'; if(manual) toastErr(e); });
    await saving; saving=null;
  };
  const touch=()=>{ dirty=true; serverDirty=true; state.textContent='Изменено'; clearTimeout(timer); timer=setTimeout(()=>save(false), 3000); };
  w.querySelectorAll('[data-f],[data-opt]').forEach(x=>{ x.addEventListener('input', touch); x.addEventListener('change', touch); });
  g('[data-tpl]').onclick=(e)=>{ e.preventDefault(); composeTemplateMenu(e.currentTarget, w, body, touch); };
  body.addEventListener('input', touch);
  // вставка: только безопасная разметка; картинки из буфера — внутрь письма
  body.addEventListener('paste', (e)=>{
    const cd=e.clipboardData; if(!cd) return;
    const files=[...cd.files||[]].filter(f=>/^image\/(png|jpe?g|gif|webp)$/.test(f.type));
    if(files.length){ e.preventDefault(); files.forEach(f=>composeInsertImage(f)); return; }
    const html=cd.getData('text/html');
    if(html){ e.preventDefault(); document.execCommand('insertHTML', false, composeClean(html)); }
  });
  const composeInsertImage=(f)=>{
    if(f.size>3*1024*1024) return toast('Картинка слишком большая', 'Вставлять в текст можно картинки до 3 МБ — крупные прикрепите файлом.', 'warn');
    const rd=new FileReader(); rd.onload=()=>{ body.focus(); document.execCommand('insertHTML', false, `<img src="${rd.result}" alt="" style="max-width: 100%">`); touch(); }; rd.readAsDataURL(f);
  };
  g('.compose-toolbar').querySelectorAll('[data-cmd]').forEach(b=>b.onclick=(e)=>{
    e.preventDefault(); body.focus();
    const c=b.dataset.cmd;
    if(c==='link'){ const url=window.prompt(t('Адрес ссылки (https://…)'), 'https://'); if(url && /^(https?:|mailto:)/i.test(url.trim())) document.execCommand('createLink', false, url.trim()); }
    else if(c==='quote') document.execCommand('formatBlock', false, 'blockquote');
    else document.execCommand(c, false, null);
    touch();
  });
  // адреса: Копия, Скрытая, подсказка
  w.querySelectorAll('[data-show]').forEach(a=>a.onclick=(e)=>{ e.preventDefault(); const row=g(`[data-row="${a.dataset.show}"]`); row.hidden=false; row.querySelector('input').focus(); a.hidden=true; });
  if(draft.cc_addrs){ const a=g('[data-show="cc"]'); if(a) a.hidden=true; }
  if(draft.bcc_addrs){ const a=g('[data-show="bcc"]'); if(a) a.hidden=true; }
  composeAutocomplete(w, draft);
  // вложения: кнопка и перетаскивание файлов на окно
  const upload=async(files)=>{
    for(const f of files){
      const used=atts.reduce((s,a)=>s+(a.size||0),0);
      if(used+f.size>maxMb*1024*1024){ toast('Вложения слишком большие', `Вместе — не больше ${maxMb} МБ.`, 'warn'); break; }
      const tmp={n:'u'+Math.random(), name:f.name, size:f.size, uploading:true}; atts.push(tmp); renderAtts();
      const fd=new FormData(); fd.append('file', f);
      try{ const r=await api(`/webmail/drafts/${draft.id}/attachments`, {method:'POST', body:fd}); Object.assign(tmp, r); tmp.uploading=false; }
      catch(e){ atts=atts.filter(x=>x!==tmp); toastErr(e); }
      renderAtts();
    }
  };
  const fileInp=g('.compose-attach input');
  fileInp.onchange=()=>{ upload([...fileInp.files]); fileInp.value=''; };
  w.addEventListener('dragover', (e)=>{ if(e.dataTransfer.types.includes('Files')){ e.preventDefault(); w.classList.add('drop'); } });
  w.addEventListener('dragleave', (e)=>{ if(!w.contains(e.relatedTarget)) w.classList.remove('drop'); });
  w.addEventListener('drop', (e)=>{ if(!e.dataTransfer.files.length) return; e.preventDefault(); w.classList.remove('drop');
    const imgs=[...e.dataTransfer.files]; if(e.target.closest('.compose-body') && imgs.every(f=>/^image\//.test(f.type) && f.size<3*1024*1024)) imgs.forEach(composeInsertImage); else upload(imgs); });
  const close=async()=>{ if(closed) return; clearTimeout(timer); await save(false, true); closed=true; w.remove(); if(Mail.view==='drafts' && Mail.acc) mailShowDrafts(Mail.acc); else if(Mail.acc) loadMailFolders(Mail.acc, {quiet:true}); };
  g('[data-close]').onclick=close;
  g('[data-min]').onclick=()=>w.classList.toggle('min');
  g('[data-max]').onclick=()=>w.classList.toggle('max');
  g('.compose-head').ondblclick=()=>w.classList.toggle('min');
  g('[data-save]').onclick=()=>{ serverDirty=true; save(true, true); };
  g('[data-discard]').onclick=async()=>{
    if(!await confirmDlg('Удалить черновик?', 'Письмо и его вложения будут удалены.', {okText:'Удалить'})) return;
    clearTimeout(timer); closed=true;
    try{ await api(`/webmail/drafts/${draft.id}`, {method:'DELETE'}); }catch(e){ toastErr(e); }
    w.remove(); Mail.drafts=Mail.drafts.filter(x=>x.id!==draft.id); if(Mail.view==='drafts' && Mail.acc) mailShowDrafts(Mail.acc); else if(Mail.acc) renderMailFolders(Mail.acc);
  };
  // Проверки перед отправкой (и перед «Отправить позже»): получатели, тема, забытое вложение
  const preflight=async()=>{
    if(atts.some(a=>a.uploading)){ toast('Вложения ещё загружаются', 'Дождитесь окончания загрузки.', 'warn'); return null; }
    const f=fields();
    if(!(f.to_addrs||'').trim() && !(f.cc_addrs||'').trim() && !(f.bcc_addrs||'').trim()){ toastErr({message:'Укажите хотя бы одного получателя.'}); return null; }
    if(!(f.subject||'').trim() && !await confirmDlg('Отправить без темы?', 'У письма нет темы.', {okText:'Отправить', okClass:'primary'})) return null;
    if(!atts.length && composeMentionsAttachment(body)
       && !await confirmDlg('Забыли вложение?', 'В письме сказано о вложении, но файлов к нему не прикреплено.', {okText:'Отправить без вложения', okClass:'primary'})) return null;
    return f;
  };
  g('[data-later]').onclick=async(e)=>{
    if(sending) return;
    const anchor=e.currentTarget;
    if(!await preflight()) return;
    mailWhenPicker(anchor, 'Отправить', async(iso, label)=>{
      clearTimeout(timer); dirty=true;
      try{ await save(true);
        let d;
        try{ d=await api(`/webmail/drafts/${draft.id}/schedule`, {method:'POST', body:{send_at:iso}}); }
        catch(err){
          if(!(err.status===409 && err.data && err.data.code==='presend_warn')) throw err;
          if(!await mailPresendConfirm(err.data.items||[])) return;
          d=await api(`/webmail/drafts/${draft.id}/schedule`, {method:'POST', body:{send_at:iso, confirm:true}});
        }
        Object.assign(draft, d); closed=true; w.remove();
        toast('Отправка запланирована', `${label}. Письмо — в «Неотправленных»; уйдёт, даже если клиент закрыт.`, 'success', 7000);
        if(Mail.acc) loadMailFolders(Mail.acc, {quiet:true});
      }catch(err){ toastErr(err); }
    });
  };
  const doSend=async()=>{
    if(sending) return;
    const f=await preflight(); if(!f) return;
    sending=true; const btn=g('[data-send]'); btn.disabled=true; btn.innerHTML='<span class="spinner"></span> Отправляю…';
    clearTimeout(timer); dirty=true;
    try{
      await save(true);
      // «Отменить отправку»: письмо несколько секунд ждёт; окно прячется, а по «Отменить» возвращается
      const undoS=(Mail.st&&Mail.st.undo_send_s)||0;
      if(undoS>0){
        w.hidden=true;
        if(await mailUndoWait(undoS, f.subject)){ w.hidden=false; sending=false; btn.disabled=false; btn.innerHTML='📨 Отправить'; body.focus(); return; }
      }
      let r;
      try{ r=await api(`/webmail/drafts/${draft.id}/send`, {method:'POST'}); }
      catch(err){
        if(!(err.status===409 && err.data && err.data.code==='presend_warn')) throw err;
        w.hidden=false;
        if(!await mailPresendConfirm(err.data.items||[])){ sending=false; btn.disabled=false; btn.innerHTML='📨 Отправить'; return; }
        r=await api(`/webmail/drafts/${draft.id}/send`, {method:'POST', body:{confirm:true}});
      }
      closed=true; w.remove();
      Mail.drafts=Mail.drafts.filter(x=>x.id!==draft.id);
      toast('Письмо отправлено', `Получателей: ${fmtNum(r.recipients)}${r.saved_to?` · копия в «${r.saved_to}»`:''}`);
      (r.warnings||[]).forEach(t=>toast('Обратите внимание', t, 'warn', 8000));
      if(Mail.acc){ await loadMailFolders(Mail.acc, {quiet:true}); if(Mail.view==='drafts') mailShowDrafts(Mail.acc); else if(!Mail.search) loadMailMessages(Mail.acc, {keep:true}); }
    }catch(e){ w.hidden=false; sending=false; btn.disabled=false; btn.innerHTML='📨 Отправить'; toastErr(e); }
  };
  g('[data-send]').onclick=doSend;
  w.addEventListener('keydown', (e)=>{
    if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ e.preventDefault(); doSend(); }
    else if((e.ctrlKey||e.metaKey) && (e.key==='s'||e.key==='ы')){ e.preventDefault(); save(true); }
    else if(e.key==='Escape' && !g('.compose-ac').hidden){ g('.compose-ac').hidden=true; }
    e.stopPropagation();
  });
  if(draft.error) toast('Письмо не было отправлено', draft.error, 'warn', 8000);
  // курсор — в «Кому», если пусто, иначе в начало текста
  const to=g('[data-f="to_addrs"]');
  if(!to.value.trim()) to.focus();
  else{ body.focus(); try{ const r=document.createRange(); r.setStart(body, 0); r.collapse(true); const s=window.getSelection(); s.removeAllRanges(); s.addRange(r); }catch(e){ /* пустое тело */ } }
}

/** Предупреждения проверки перед отправкой: отправить всё равно или вернуться к письму. */
async function mailPresendConfirm(items){
  const icon={external:'🌐', many:'👥', sensitive:'🛡'};
  return confirmDlg('Проверьте письмо перед отправкой', `<ul class="presend">${items.map(i=>`<li>${icon[i.code]||'⚠️'} ${esc(i.text)}</li>`).join('')}</ul>
    <div class="small muted">Если всё верно — отправьте письмо. Отправка с конфиденциальными данными записывается в аудит.</div>`,
    {okText:'Всё верно, отправить', okClass:'primary', html:true});
}

/** Выбрать письма из архива (поиск по своим ящикам) — для вложения в письмо. */
async function archivePickerDlg(accId){
  return new Promise(resolve=>{
    let done=false; const chosen=new Map();
    const m=modal('Приложить письма из архива', `<div style="display:flex;gap:8px"><input type="search" id="apq" placeholder="Поиск: тема, отправитель, текст…" style="flex:1"><button class="btn" id="apgo">Найти</button></div>
      <div class="small muted" style="margin:6px 0">Поиск — во всех ваших ящиках. Выбранные письма приложатся файлами .eml (до 20).</div>
      <div id="apres" style="max-height:50vh;overflow:auto;border:1px solid var(--border);border-radius:var(--radius-sm)"><div class="empty small">Введите запрос</div></div>`,
      {wide:true, footer:'<span class="small muted" id="apcnt"></span><div class="spacer" style="flex:1"></div><button class="btn" data-c>Отмена</button><button class="btn primary" data-ok>Приложить</button>',
       onClose:()=>{ if(!done){ done=true; resolve(null); } }});
    const res=m.body.querySelector('#apres'), cnt=m.foot.querySelector('#apcnt');
    const upd=()=>{ cnt.textContent=chosen.size?`выбрано: ${chosen.size}`:''; };
    const go=async()=>{
      const q=m.body.querySelector('#apq').value.trim(); if(!q) return;
      res.innerHTML='<div class="empty"><div class="spinner"></div></div>';
      try{
        const r=await api(`/search?q=${encodeURIComponent(q)}&scope=all&limit=50`+(accId?`&account_id=${accId}`:''));
        res.innerHTML=(r.results||[]).map(x=>`<label style="display:flex;gap:8px;align-items:flex-start;padding:6px 10px;border-bottom:1px solid var(--border)"><input type="checkbox" data-id="${x.id}" ${chosen.has(x.id)?'checked':''}>
          <span style="flex:1"><b>${esc(x.subject||'(без темы)')}</b><div class="small muted">${esc(x.from)} · ${esc(fmtDate(x.date))} · ${esc(x.account_name)}</div></span></label>`).join('') || '<div class="empty small">Ничего не найдено</div>';
      }catch(e){ res.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; }
    };
    m.body.querySelector('#apgo').onclick=go;
    m.body.querySelector('#apq').onkeydown=(e)=>{ if(e.key==='Enter'){ e.preventDefault(); go(); } };
    res.onchange=(e)=>{ const id=Number(e.target.dataset.id); if(!id) return; if(e.target.checked){ if(chosen.size>=20){ e.target.checked=false; return toast('Не больше 20 писем','','warn'); } chosen.set(id,true); } else chosen.delete(id); upd(); };
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    m.foot.querySelector('[data-ok]').onclick=()=>{ done=true; resolve(Array.from(chosen.keys())); m.close(); };
  });
}

/** Настройки S/MIME ящика: свой сертификат с ключом и сертификаты получателей. */
async function mailSmimeSection(pane, accId){
  let d; try{ d=await api(`/webmail/smime?account_id=${accId}`); }catch(e){ return; }
  const k=d.key||{};
  const sec=h(`<div class="mail-sieve"><h3 class="mset-h">🔏 Подпись и шифрование (S/MIME)</h3>
    <p class="small muted" style="margin-top:0">Сертификат ящика с закрытым ключом (.p12 или .pfx — его выдаёт удостоверяющий центр или служба ИБ) позволяет подписывать письма и читать зашифрованные. Ключ хранится на сервере архива зашифрованным. Сертификаты получателей собираются сами из писем с верной подписью.</p>
    ${k.present?`<div class="hint" style="${k.expired?'border-color:var(--danger)':''}">${k.expired?'⛔ Срок сертификата истёк. ':'✔ '}Сертификат: <b>${esc(k.name||'')}</b> ${esc((k.emails||[]).join(', '))}${k.issuer?`, выдан: ${esc(k.issuer)}`:''}, действует до ${esc(fmtDateShort(k.valid_until))}.</div>`:'<div class="hint">Сертификат ящика не загружен.</div>'}
    <div class="grid2"><div class="form-row"><label for="sm-file">Файл .p12 / .pfx</label><input id="sm-file" type="file" accept=".p12,.pfx"></div>
      <div class="form-row"><label for="sm-pass">Пароль файла</label><input id="sm-pass" type="password" autocomplete="new-password"></div></div>
    <div class="btn-row"><button class="btn primary" data-up>${k.present?'Заменить сертификат':'Загрузить сертификат'}</button>${k.present?'<button class="btn danger" data-del>Удалить сертификат</button>':''}
      <span class="spacer" style="flex:1"></span><button class="btn ghost" data-certs>${d.personal?'Мои сертификаты получателей':'Сертификаты получателей'} (${fmtNum(d.certs||0)})</button></div></div>`);
  sec.querySelector('[data-up]').onclick=async()=>{
    const f=sec.querySelector('#sm-file').files[0]; if(!f) return toast('Выберите файл .p12 или .pfx','','warn');
    const fd=new FormData(); fd.append('account_id', accId); fd.append('password', sec.querySelector('#sm-pass').value); fd.append('file', f);
    try{ await api('/webmail/smime/key', {method:'POST', body:fd}); toast('Сертификат загружен'); sec.remove(); mailSmimeSection(pane, accId); }catch(e){ toastErr(e); }
  };
  const del=sec.querySelector('[data-del]');
  if(del) del.onclick=async()=>{ if(!await confirmDlg('Удалить сертификат ящика?', 'Подписывать письма и читать зашифрованные будет нельзя, пока не загрузите сертификат снова.', {okText:'Удалить'})) return;
    try{ await api(`/webmail/smime/key?account_id=${accId}`, {method:'DELETE'}); toast('Удалено'); sec.remove(); mailSmimeSection(pane, accId); }catch(e){ toastErr(e); } };
  sec.querySelector('[data-certs]').onclick=()=>mailSmimeCerts(d.personal?accId:null);
  pane.appendChild(sec);
}
async function mailSmimeCerts(accId){
  // Сотрудник видит и пополняет только свой список (для своего ящика), общий справочник — у администратора (1.21.0)
  const own=accId!=null, qs=own?`&account_id=${accId}`:'';
  const m=modal(own?'Мои сертификаты получателей':'Сертификаты получателей', `<div style="display:flex;gap:8px;align-items:center"><input type="search" id="sc-q" placeholder="Адрес или имя" style="flex:1">
    ${own||isAdmin()?'<label class="btn sm">⬆ Загрузить .cer<input type="file" id="sc-file" accept=".cer,.crt,.pem,.der" hidden></label>':''}</div>
    <p class="small muted" style="margin:6px 0 0">${own?'Сертификаты, загруженные сюда, шифруют только письма из этого ящика. Общий справочник ведёт администратор; сертификаты из писем с доверенной подписью попадают в него сами.':'Сертификат из письма запоминается, только если подпись доверенная и адрес отправителя есть в сертификате; загруженный здесь сертификат письмом не заменяется. Сверяйте отпечаток с владельцем.'}</p>
    <div id="sc-list" style="margin-top:8px;max-height:55vh;overflow:auto"></div>`, {wide:true});
  const list=m.body.querySelector('#sc-list');
  const SRC={signed:'из письма с подписью', upload:'загружен администратором', own:'сертификат ящика', personal:'загружен в ящик'};
  const canDel=own||isAdmin();
  const load=async()=>{
    const q=m.body.querySelector('#sc-q').value.trim();
    let r; try{ r=await api('/webmail/smime/certs?q='+encodeURIComponent(q)+qs); }catch(e){ list.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
    list.innerHTML=r.items.length?`<table class="tbl"><thead><tr><th>Адрес</th><th>Владелец</th><th>Отпечаток SHA-256</th><th>До</th><th>Откуда</th>${canDel?'<th></th>':''}</tr></thead><tbody>${r.items.map(c=>`<tr><td>${esc(c.email)}${c.changed_at?`<div class="small" style="color:var(--warn-text)" title="Сертификат этого адреса сменился">⚠ сменился ${esc(fmtDateShort(c.changed_at))}</div>`:''}</td><td class="small">${esc(c.name)}<div class="muted">${esc(c.issuer)}</div></td><td class="small mono">${esc(c.fingerprint||'')}</td><td class="small">${c.expired?'<span class="tag err">истёк</span>':esc(fmtDateShort(c.valid_until))}</td><td class="small">${esc(SRC[c.source]||c.source)}</td>${canDel?`<td><button class="btn ghost sm" data-del="${esc(c.email)}" title="Удалить">✕</button></td>`:''}</tr>`).join('')}</tbody></table>`
      :`<div class="empty small">${own?'Своих сертификатов пока нет.':'Сертификатов пока нет: они появятся, когда придут письма с доверенной подписью S/MIME.'}</div>`;
  };
  list.onclick=async(e)=>{ const b=e.target.closest('[data-del]'); if(!b) return; try{ await api('/webmail/smime/certs?email='+encodeURIComponent(b.dataset.del)+qs, {method:'DELETE'}); load(); }catch(err){ toastErr(err); } };
  m.body.querySelector('#sc-q').oninput=()=>load();
  const file=m.body.querySelector('#sc-file');
  if(file) file.onchange=async(e)=>{ const f=e.target.files[0]; if(!f) return; const fd=new FormData(); fd.append('file', f); if(own) fd.append('account_id', accId);
    try{ const r=await api('/webmail/smime/certs', {method:'POST', body:fd}); toast('Сертификат добавлен', `${r.emails.join(', ')} · отпечаток ${r.fingerprint}`); load(); }catch(err){ toastErr(err); } e.target.value=''; };
  load();
}

/** Вставленный HTML — без скриптов, обработчиков, стилей страницы и внешних картинок. */
function composeClean(html){
  const ok=new Set(['A','B','STRONG','I','EM','U','S','STRIKE','DEL','BR','P','DIV','SPAN','UL','OL','LI','BLOCKQUOTE','H1','H2','H3','H4','PRE','CODE','TABLE','THEAD','TBODY','TR','TD','TH','HR','SUB','SUP','IMG']);
  let doc;
  try{ doc=new DOMParser().parseFromString(String(html||''), 'text/html'); }catch(e){ return esc(String(html||'')); }
  const walk=(node)=>{
    [...node.childNodes].forEach(ch=>{
      if(ch.nodeType===1){
        if(['SCRIPT','STYLE','IFRAME','OBJECT','EMBED','NOSCRIPT','TEMPLATE','HEAD','TITLE','META','LINK','SVG','MATH','FORM','INPUT','BUTTON','SELECT','TEXTAREA'].includes(ch.tagName)){ ch.remove(); return; }
        walk(ch);
        if(!ok.has(ch.tagName)){ ch.replaceWith(...ch.childNodes); return; }
        [...ch.attributes].forEach(a=>{
          const n=a.name.toLowerCase(), v=a.value||'';
          const keep=(n==='href' && ch.tagName==='A' && /^(https?:|mailto:)/i.test(v.trim()))
            || (n==='src' && ch.tagName==='IMG' && /^data:image\/(png|jpe?g|gif|webp);base64,/i.test(v.trim()))
            || ['colspan','rowspan','alt'].includes(n);
          if(!keep) ch.removeAttribute(a.name);
        });
        if(ch.tagName==='IMG' && !ch.getAttribute('src')) ch.remove();
      }else if(ch.nodeType!==3){ ch.remove(); }
    });
  };
  walk(doc.body);
  return doc.body.innerHTML;
}

/** Подсказка адресов в полях «Кому», «Копия», «Скрытая»: последний адрес после запятой. */
function composeAutocomplete(w, draft){
  const ac=w.querySelector('.compose-ac');
  let target=null, items=[], idx=-1, t=null;
  const hide=()=>{ ac.hidden=true; items=[]; idx=-1; };
  const pick=(i)=>{
    const it=items[i]; if(!it || !target) return;
    const parts=target.value.split(/[,;]/); parts.pop();
    target.value=(parts.map(s=>s.trim()).filter(Boolean).concat([it.group?it.members:it.label]).join(', '))+', ';
    target.dispatchEvent(new Event('input')); hide(); target.focus();
  };
  const show=()=>{
    if(!items.length || !target){ hide(); return; }
    const r=target.getBoundingClientRect(), wr=w.getBoundingClientRect();
    ac.style.left=(r.left-wr.left)+'px'; ac.style.top=(r.bottom-wr.top+2)+'px'; ac.style.width=r.width+'px';
    ac.innerHTML=items.map((it,i)=>`<div class="compose-ac-item ${i===idx?'on':''}" data-i="${i}">${esc(it.label)}</div>`).join('');
    ac.querySelectorAll('[data-i]').forEach(d=>d.onmousedown=(e)=>{ e.preventDefault(); pick(parseInt(d.dataset.i)); });
    ac.hidden=false;
  };
  w.querySelectorAll('[data-f="to_addrs"],[data-f="cc_addrs"],[data-f="bcc_addrs"]').forEach(inp=>{
    inp.addEventListener('input', ()=>{
      target=inp; clearTimeout(t);
      const q=(inp.value.split(/[,;]/).pop()||'').trim();
      if(q.length<2){ hide(); return; }
      t=setTimeout(async()=>{
        try{ const r=await api(`/webmail/contacts?account_id=${draft.account_id}&q=${encodeURIComponent(q)}`, {bg:true}); items=(r.items||[]).slice(0,8); idx=items.length?0:-1; show(); }catch(e){ hide(); }
      }, 200);
    });
    inp.addEventListener('keydown', (e)=>{
      if(ac.hidden) return;
      if(e.key==='ArrowDown'){ e.preventDefault(); idx=Math.min(items.length-1, idx+1); show(); }
      else if(e.key==='ArrowUp'){ e.preventDefault(); idx=Math.max(0, idx-1); show(); }
      else if((e.key==='Enter'||e.key==='Tab') && idx>=0){ e.preventDefault(); pick(idx); }
      else if(e.key==='Escape'){ e.preventDefault(); hide(); }
    });
    inp.addEventListener('blur', ()=>setTimeout(hide, 150));
  });
}

// ---------- Отправитель, подпись, сервер отправки ----------
/** Вкладка «Отправитель и подпись» окна настроек клиента. */
async function mailIdentityPane(pane, accId){
  let d; try{ d=await api(`/webmail/identity?account_id=${accId}`); }catch(e){ pane.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
  const adm=d.can_edit_smtp;
  const auto=d.auto||{};
  const st=d.stored||{};
  pane.innerHTML=`
    <div class="form-row"><label for="mi-name">Имя отправителя</label><input id="mi-name" type="text" maxlength="200" value="${esc(d.display_name||'')}"><div class="hint">Так получатели увидят отправителя: «${esc(d.display_name||'')} &lt;${esc(d.address||'адрес')}&gt;».</div></div>
    <div class="form-row"><label for="mi-reply">Ответы — на адрес <span class="muted small">(необязательно)</span></label><input id="mi-reply" type="text" maxlength="300" value="${esc(d.reply_to||'')}" placeholder="например, sales@company.ru"></div>
    <div class="form-row"><label>Подпись</label><div class="compose-body sig" id="mi-sig" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Подпись"></div><div class="hint">Добавляется в каждое новое письмо, ответ и пересылку.</div></div>
    ${adm?`<details class="mail-smtp" ${d.send_problem?'open':''}><summary><b>Отправка (SMTP)</b> — ${esc(d.smtp_host)}:${esc(String(d.smtp_port))}, ${esc(d.smtp_security)}</summary>
      <div class="form-row"><label for="mi-addr">Адрес отправителя</label><input id="mi-addr" type="text" inputmode="email" autocomplete="off" value="${esc(st.address||'')}" placeholder="${esc(d.address||'')}"><div class="hint">Пусто — логин ящика (если это адрес) или адрес сотрудника.</div></div>
      <div class="grid2"><div class="form-row"><label for="mi-host">Сервер отправки</label><input id="mi-host" type="text" value="${esc(st.smtp_host||'')}" placeholder="${esc(auto.host||'')}"></div>
        <div class="form-row"><label for="mi-port">Порт</label><input id="mi-port" type="number" min="1" max="65535" value="${st.smtp_port||''}" placeholder="${esc(String(auto.port||''))}"></div></div>
      <div class="grid2"><div class="form-row"><label for="mi-sec">Защита</label><select id="mi-sec"><option value="">как подобрано (${esc(auto.security||'')})</option><option value="ssl">SSL (порт 465)</option><option value="starttls">STARTTLS (порт 587)</option><option value="none">без шифрования</option></select></div>
        <div class="form-row"><label for="mi-auth">Вход</label><select id="mi-auth"><option value="account">как в ящик (пароль, OAuth)</option><option value="custom">отдельный логин и пароль</option><option value="none">без входа (ретранслятор)</option></select></div></div>
      <div class="grid2" id="mi-custom"><div class="form-row"><label for="mi-user">Логин SMTP</label><input id="mi-user" type="text" value="${esc(d.smtp_username||'')}" autocomplete="off"></div>
        <div class="form-row"><label for="mi-pass">Пароль SMTP</label><input id="mi-pass" type="password" autocomplete="new-password" placeholder="${d.smtp_password_set?'задан — пусто, чтобы не менять':''}"></div></div>
      <div class="form-row"><label for="mi-save">Копия в «Отправленные»</label><select id="mi-save"><option value="">как в настройках</option><option value="auto">auto — кроме Gmail и Microsoft 365</option><option value="always">всегда</option><option value="never">никогда (сервер кладёт сам)</option></select></div>
      <div class="form-row"><label for="mi-alias">Другие адреса отправителя (псевдонимы)</label><input id="mi-alias" type="text" value="${esc((d.aliases||[]).join(', '))}" placeholder="например, sales@company.ru, info@company.ru"><div class="hint">В новом письме можно выбрать, с какого адреса отправить. Сервер отправки должен разрешать ящику эти адреса.</div></div>
      ${d.send_problem?`<div class="hint" style="border-color:var(--warn)">⚠️ ${esc(d.send_problem)}</div>`:'<div class="hint">Пустые поля подбираются по серверу IMAP ящика.</div>'}
    </details>`:(d.send_problem?`<div class="hint" style="border-color:var(--warn)">⚠️ ${esc(d.send_problem)}</div>`:'')}
    ${!adm&&(d.aliases||[]).length?`<div class="hint">Можно отправлять и с адресов: ${esc(d.aliases.join(', '))}.</div>`:''}
    <div class="btn-row mset-foot"><button class="btn primary" data-ok>Сохранить</button></div>`;
  const g=s=>pane.querySelector(s);
  g('#mi-sig').innerHTML=d.signature||'';
  g('#mi-sig').addEventListener('paste', (e)=>{ const html=e.clipboardData&&e.clipboardData.getData('text/html'); if(html){ e.preventDefault(); document.execCommand('insertHTML', false, composeClean(html)); } });
  if(adm){
    g('#mi-sec').value=st.smtp_security||''; g('#mi-auth').value=d.smtp_auth||'account'; g('#mi-save').value=st.save_sent||'';
    const sync=()=>{ g('#mi-custom').style.display=g('#mi-auth').value==='custom'?'':'none'; }; sync(); g('#mi-auth').onchange=sync;
  }
  g('[data-ok]').onclick=async()=>{
    const body={account_id:accId, display_name:g('#mi-name').value, reply_to:g('#mi-reply').value, signature:g('#mi-sig').innerHTML};
    if(adm){
      Object.assign(body, {address:g('#mi-addr').value.trim(), smtp_host:g('#mi-host').value.trim(), smtp_port:parseInt(g('#mi-port').value||'0')||0,
        smtp_security:g('#mi-sec').value, smtp_auth:g('#mi-auth').value, smtp_username:g('#mi-user').value.trim(), save_sent:g('#mi-save').value,
        aliases:g('#mi-alias').value});
      if(g('#mi-pass').value) body.smtp_password=g('#mi-pass').value;
    }
    try{ await api('/webmail/identity', {method:'PUT', body}); toast('Сохранено', 'Отправитель и подпись ящика');
      try{ Mail.st=await api(`/webmail/status?account_id=${accId}`); }catch(e){ /* обновится при следующем входе */ }
      const cb=$('#mailCompose'); if(cb){ cb.disabled=!mailCanSend(); cb.title=mailCanSend()?'Новое письмо (c)':(Mail.st.send_problem||''); }
    }catch(e){ toastErr(e); }
  };  mailSmimeSection(pane, accId);
  mailDavSection(pane, accId);
  mailAppSection(pane);
}

/** Календарь и контакты почтового сервера (CalDAV, CardDAV). */
async function mailDavSection(pane, accId){
  let d; try{ d=await api(`/webmail/dav?account_id=${accId}`); }catch(e){ return; }
  const found=d.found||{calendars:[], addressbooks:[]};
  const opts=(list, cur)=>list.map(x=>`<option value="${esc(x.href)}" ${x.href===cur?'selected':''}>${esc(x.name||x.href)}</option>`).join('');
  const sec=h(`<div class="mail-sieve"><h3 class="mset-h">📅 Календарь и контакты (CalDAV, CardDAV)</h3>
    <p class="small muted" style="margin-top:0">Календарь и адресная книга почтового сервера (Nextcloud, SOGo, Zimbra, Яндекс, Mail.ru, Dovecot с Radicale и другие): контакты сервера попадают в подсказку адресов, ближайшие события видны в окне «📅», принятое приглашение ложится в календарь. Вход — логином и паролем ящика.</p>
    ${d.supported?'':'<div class="hint">Доступно ящикам с паролем.</div>'}
    ${d.url?`<div class="hint">✔ Подключено: ${esc(d.url)}${d.calendar?' · календарь':''}${d.addressbook?' · контакты':''}${d.contacts_synced_at?` · контакты загружены ${esc(fmtDate(d.contacts_synced_at))} (${fmtNum(d.contacts||0)})`:''}</div>`:''}
    ${d.can_configure?`<div class="grid2"><div class="form-row"><label for="dv-url">Адрес сервера</label><input id="dv-url" type="url" value="${esc(d.url||'')}" placeholder="https://mail.company.ru"></div>
      <div class="form-row"><label for="dv-user">Логин <span class="muted small">(если другой)</span></label><input id="dv-user" type="text" value="${esc(d.username||'')}" autocomplete="off"></div></div>
      ${found.calendars.length?`<div class="form-row"><label for="dv-cal">Календарь</label><select id="dv-cal">${opts(found.calendars, d.calendar)}</select></div>`:''}
      ${found.addressbooks.length?`<div class="form-row"><label for="dv-book">Адресная книга</label><select id="dv-book">${opts(found.addressbooks, d.addressbook)}</select></div>`:''}`:''}
    <div class="btn-row mset-foot">${d.can_configure&&d.supported?'<button class="btn primary" data-dv>Найти и подключить</button>':''}
      ${d.addressbook?'<button class="btn" data-sync>⟳ Загрузить контакты</button>':''}${d.calendar?'<button class="btn" data-cal>📅 Календарь</button>':''}</div></div>`);
  const g=s=>sec.querySelector(s);
  const save=g('[data-dv]');
  if(save) save.onclick=async()=>{ save.disabled=true;
    try{ const body={account_id:accId, url:g('#dv-url').value.trim(), username:g('#dv-user').value.trim(), calendar:g('#dv-cal')?g('#dv-cal').value:'', addressbook:g('#dv-book')?g('#dv-book').value:''};
      const r=await api('/webmail/dav', {method:'PUT', body}); const f=r.found||{calendars:[],addressbooks:[]};
      toast(r.url?'Подключено':'Отключено', r.url?`Календарей: ${f.calendars.length}, адресных книг: ${f.addressbooks.length}`:''); sec.remove(); mailDavSection(pane, accId); }
    catch(e){ toastErr(e); } finally{ save.disabled=false; } };
  const sy=g('[data-sync]');
  if(sy) sy.onclick=async()=>{ sy.disabled=true; try{ const r=await api('/webmail/dav/contacts', {method:'POST', body:{account_id:accId}}); toast('Контакты загружены', `Новых: ${r.added}, обновлено: ${r.updated}`); sec.remove(); mailDavSection(pane, accId); }catch(e){ toastErr(e); } finally{ sy.disabled=false; } };
  const cl=g('[data-cal]'); if(cl) cl.onclick=()=>mailCalendarDlg(accId);
  pane.appendChild(sec);
}

/** Ближайшие события календаря сервера и новое событие. */
async function mailCalendarDlg(accId){
  // Список ближайших событий и вид «Месяц»; события можно изменить и удалить (1.21.0)
  const m=modal('📅 Календарь', `<div class="btn-row"><button class="btn sm primary" data-new>＋ Событие</button>
      <span class="seg" role="tablist"><button class="btn sm" data-view="list" aria-pressed="true">Список</button><button class="btn sm ghost" data-view="month" aria-pressed="false">Месяц</button></span>
      <span class="spacer" style="flex:1"></span><span data-nav hidden><button class="btn sm ghost" data-prev title="Предыдущий месяц">‹</button> <b data-title></b> <button class="btn sm ghost" data-next title="Следующий месяц">›</button></span>
      <button class="btn sm ghost" data-reload>↻ Обновить</button></div>
    <div data-form hidden class="card" style="margin:8px 0"><div class="form-row"><label>Название</label><input data-f="summary" maxlength="500"></div>
      <div class="grid2"><div class="form-row"><label>Начало</label><input type="datetime-local" data-f="start"></div><div class="form-row"><label>Конец</label><input type="datetime-local" data-f="end"></div></div>
      <div class="form-row"><label>Где</label><input data-f="location" maxlength="500"></div>
      <div class="form-row"><label>Описание</label><textarea data-f="description" rows="2" maxlength="4000"></textarea></div>
      <label class="check"><input type="checkbox" data-f="all_day"> Весь день</label>
      <div class="btn-row"><button class="btn primary sm" data-add>Добавить в календарь</button><button class="btn ghost sm" data-cancel>Отмена</button></div></div>
    <div data-list><div class="empty"><div class="spinner"></div></div></div>`, {wide:true});
  const g=s=>m.body.querySelector(s);
  const list=g('[data-list]'), form=g('[data-form]');
  let view='list', month=new Date(); month.setDate(1); month.setHours(0,0,0,0);
  let editing=null, items=[];
  const local=(x)=>new Date(x-x.getTimezoneOffset()*60e3).toISOString().slice(0,16);
  const fmtWhen=(e)=>{ const s=new Date(e.start); if(e.all_day) return s.toLocaleDateString(UI_LOCALE,{weekday:'short', day:'2-digit', month:'long'})+' · весь день';
    const en=e.end?new Date(e.end):null; return s.toLocaleString(UI_LOCALE,{weekday:'short', day:'2-digit', month:'long', hour:'2-digit', minute:'2-digit'})+(en?' – '+en.toLocaleTimeString(UI_LOCALE,{hour:'2-digit', minute:'2-digit'}):''); };
  const acts=(e,i)=>e.rrule?'<span class="small muted" title="Повторяющееся событие меняйте в программе календаря">🔁</span>':`<button class="btn ghost sm" data-edit="${i}" title="Изменить">✏️</button><button class="btn ghost sm" data-del="${i}" title="Удалить">🗑</button>`;
  const renderList=()=>{
    list.innerHTML=items.length?items.map((e,i)=>`<div class="mail-event"><div class="w">${esc(fmtWhen(e))}</div><div class="s"><b>${esc(e.summary||'(без названия)')}</b>${e.location?` <span class="muted">· ${esc(e.location)}</span>`:''}${e.description?`<div class="small muted">${esc(e.description.slice(0,200))}</div>`:''}</div><div class="a">${acts(e,i)}</div></div>`).join('')
      :'<div class="empty small">На ближайшие 30 дней событий нет.</div>';
  };
  const renderMonth=()=>{
    const first=new Date(month), start=new Date(first); start.setDate(1-((first.getDay()+6)%7));
    const days=[]; for(let i=0;i<42;i++){ const d=new Date(start); d.setDate(start.getDate()+i); days.push(d); }
    const key=d=>`${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    const by={}; items.forEach((e,i)=>{ const d=new Date(e.start); (by[key(d)]=by[key(d)]||[]).push(i); });
    const today=key(new Date());
    list.innerHTML=`<div class="cal-grid">${['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(x=>`<div class="cal-h">${x}</div>`).join('')}${days.map(d=>{
      const evs=(by[key(d)]||[]).map(i=>{ const e=items[i]; const t=e.all_day?'':new Date(e.start).toLocaleTimeString(UI_LOCALE,{hour:'2-digit',minute:'2-digit'})+' ';
        return `<button class="cal-ev" ${e.rrule?'':`data-edit="${i}"`} title="${esc(fmtWhen(e))}${e.location?' · '+esc(e.location):''}">${esc(t)}${esc(e.summary||'(без названия)')}</button>`; }).join('');
      return `<div class="cal-d ${d.getMonth()!==month.getMonth()?'out':''} ${key(d)===today?'today':''}"><div class="n">${d.getDate()}</div>${evs}</div>`; }).join('')}</div>`;
    g('[data-title]').textContent=month.toLocaleDateString(UI_LOCALE,{month:'long', year:'numeric'});
  };
  const load=async()=>{
    list.innerHTML='<div class="empty"><div class="spinner"></div></div>';
    let q=`/webmail/calendar?account_id=${accId}&days=30`;
    if(view==='month'){ const a=new Date(month); a.setDate(a.getDate()-7); const b=new Date(month); b.setMonth(b.getMonth()+1); b.setDate(b.getDate()+14);
      const d=x=>`${x.getFullYear()}-${String(x.getMonth()+1).padStart(2,'0')}-${String(x.getDate()).padStart(2,'0')}`; q+=`&since=${d(a)}&until=${d(b)}`; }
    let r; try{ r=await api(q); }catch(e){ list.innerHTML=`<div class="empty small">${esc(e.message)}</div>`; return; }
    items=r.items||[]; view==='month'?renderMonth():renderList();
  };
  const openForm=(e)=>{
    editing=e||null; form.hidden=false; const v=s=>form.querySelector(`[data-f="${s}"]`);
    if(e){ v('summary').value=e.summary||''; v('location').value=e.location||''; v('description').value=e.description||''; v('all_day').checked=!!e.all_day;
      v('start').value=local(new Date(e.start)); v('end').value=e.end?local(new Date(e.end)):''; }
    else{ const d=new Date(Date.now()+3600e3); d.setMinutes(0,0,0); ['summary','location','description'].forEach(k=>v(k).value=''); v('all_day').checked=false;
      v('start').value=local(d); v('end').value=local(new Date(d.getTime()+3600e3)); }
    g('[data-add]').textContent=e?'Сохранить':'Добавить в календарь'; v('summary').focus();
  };
  list.onclick=async(ev)=>{
    const ed=ev.target.closest('[data-edit]'), del=ev.target.closest('[data-del]');
    if(ed) return openForm(items[Number(ed.dataset.edit)]);
    if(del){ const e=items[Number(del.dataset.del)];
      if(!await confirmDlg('Удалить событие?', `«${esc(e.summary||'')}» — ${esc(fmtWhen(e))}. Событие удалится из календаря на сервере (и на телефоне).`, {okText:'Удалить', html:true})) return;
      try{ await api(`/webmail/calendar?account_id=${accId}&href=${encodeURIComponent(e.href)}&etag=${encodeURIComponent(e.etag||'')}`, {method:'DELETE'}); toast('Событие удалено'); load(); }catch(err){ toastErr(err); } }
  };
  m.body.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>{ view=b.dataset.view;
    m.body.querySelectorAll('[data-view]').forEach(x=>{ const on=x.dataset.view===view; x.classList.toggle('ghost',!on); x.setAttribute('aria-pressed', on); });
    g('[data-nav]').hidden=view!=='month'; load(); });
  g('[data-prev]').onclick=()=>{ month.setMonth(month.getMonth()-1); load(); };
  g('[data-next]').onclick=()=>{ month.setMonth(month.getMonth()+1); load(); };
  g('[data-reload]').onclick=load;
  g('[data-new]').onclick=()=>openForm(null);
  g('[data-cancel]').onclick=()=>{ form.hidden=true; editing=null; };
  g('[data-add]').onclick=async(ev)=>{
    const b=ev.currentTarget; b.disabled=true; const v=s=>form.querySelector(`[data-f="${s}"]`);
    const toIso=(x)=>x?new Date(x).toISOString():'';
    const body={account_id:accId, summary:v('summary').value, start:toIso(v('start').value), end:toIso(v('end').value), location:v('location').value, description:v('description').value, all_day:v('all_day').checked};
    try{
      if(editing){ await api('/webmail/calendar', {method:'PUT', body:{...body, href:editing.href, etag:editing.etag||''}}); toast('Событие изменено'); }
      else{ await api('/webmail/calendar', {method:'POST', body}); toast('Событие добавлено'); }
      form.hidden=true; editing=null; load(); }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
  load();
}

/** Клиент как приложение: установка и ссылки mailto:. */
function mailAppSection(pane){
  const secure=window.isSecureContext;
  const sec=h(`<div class="mail-sieve"><h3 class="mset-h">📱 Как приложение</h3>
    <p class="small muted" style="margin-top:0">Клиент можно установить как отдельное приложение: в Chrome, Edge и Яндекс Браузере — меню браузера → «Установить приложение» (на телефоне — «Добавить на главный экран»). Тогда он открывается в своём окне, как почтовая программа.</p>
    <div class="btn-row"><button class="btn" data-mailto ${secure&&navigator.registerProtocolHandler?'':'disabled'}>Открывать ссылки mailto: здесь</button></div>
    ${secure?'':'<div class="small muted">Нужно открытие по https:// (или с этой же машины по localhost).</div>'}</div>`);
  const b=sec.querySelector('[data-mailto]');
  b.onclick=()=>{ try{ navigator.registerProtocolHandler('mailto', location.origin+'/#/mail?compose=%s'); toast('Готово', 'Браузер спросит разрешения: после этого ссылки mailto: будут открывать новое письмо в этом клиенте.'); }catch(e){ toastErr(e); } };
  pane.appendChild(sec);
}

// ---------- Почтовый клиент 1.18.0: метки, «напомнить позже», архивы, отмена, правила, адресная книга ----------
/** Тост с кнопкой действия («Отменить»). */
function toastAction(title, message, btnText, onClick, ttl=8000){
  const t=h(`<div class="toast success"><div class="tt">${esc(title)}</div>${message?`<div class="tm">${esc(message)}</div>`:''}<button class="btn sm toast-act" type="button">${esc(btnText)}</button></div>`);
  $('#toasts').appendChild(t);
  let done=false;
  const hide=()=>{ if(done) return; done=true; t.style.opacity='0'; setTimeout(()=>t.remove(),200); };
  t.querySelector('.toast-act').onclick=()=>{ hide(); onClick(); };
  setTimeout(hide, ttl);
  return {hide};
}
/** Вернуть перенесённые письма туда, откуда их унесли. */
async function mailUndo(acc, undo){
  let n=0;
  try{ for(const u of undo){ const r=await api('/webmail/move', {method:'POST', body:{account_id:acc, ids:u.ids, folder:u.folder}}); n+=r.moved||0; } }
  catch(e){ toastErr(e); }
  if(n) toast('Отменено', `Писем возвращено: ${fmtNum(n)}`);
  if(Mail.acc===acc){ await loadMailFolders(acc, {quiet:true}); if(!Mail.search && Mail.view==='folder') loadMailMessages(acc, {keep:true}); }
}
/** «Отменить отправку»: ждать sec секунд; true — отправку отменили. */
function mailUndoWait(sec, subject){
  return new Promise(res=>{
    let left=sec, done=false;
    const t=h(`<div class="toast"><div class="tt">Отправка через <span data-left>${left}</span> с</div><div class="tm">${esc(trunc(subject||'(без темы)',60))}</div><button class="btn sm toast-act" type="button">Отменить</button></div>`);
    $('#toasts').appendChild(t);
    const finish=(cancel)=>{ if(done) return; done=true; clearInterval(iv); t.remove(); res(cancel); };
    t.querySelector('.toast-act').onclick=()=>finish(true);
    const iv=setInterval(()=>{ left--; const s=t.querySelector('[data-left]'); if(s) s.textContent=String(left); if(left<=0) finish(false); }, 1000);
  });
}
function mailClosePops(){ document.querySelectorAll('.mail-pop').forEach(x=>x.remove()); }
function mailPopAt(pop, anchor, width=280){
  document.body.appendChild(pop);
  const r=anchor.getBoundingClientRect();
  pop.style.top=Math.max(8, Math.min(window.innerHeight+window.scrollY-pop.offsetHeight-8, r.bottom+4+window.scrollY))+'px';
  pop.style.left=Math.max(8, Math.min(window.innerWidth-width-8, r.left+window.scrollX))+'px';
  const off=(e)=>{ if(!pop.contains(e.target) && e.target!==anchor){ pop.remove(); document.removeEventListener('mousedown', off); } };
  setTimeout(()=>document.addEventListener('mousedown', off), 0);
  pop.addEventListener('keydown', (e)=>{ if(e.key==='Escape'){ pop.remove(); anchor.focus(); } });
}

// --- метки
function mailTagChips(keywords){
  const kws=keywords||[]; if(!kws.length || !Mail.tags.length) return '';
  const chips=Mail.tags.filter(t=>kws.includes(t.keyword)).map(t=>`<span class="mtag" style="--tag:${esc(t.color)}">${esc(t.name)}</span>`).join('');
  return chips?`<div class="mtags">${chips}</div>`:'';
}
function mailRenderTagList(accId, box){
  if(!Mail.tags.length || !Mail.st || !Mail.st.enabled) return;
  box.appendChild(h('<div class="mail-folders-sep">🏷 Метки</div>'));
  Mail.tags.forEach(t=>{
    const active=Mail.view==='folder' && Mail.filter==='tag:'+t.keyword;
    const el=h(`<div class="mail-folder mail-tagrow ${active?'active':''}" tabindex="0" role="button" title="Письма папки с меткой «${esc(t.name)}»"><span class="mdot" style="--tag:${esc(t.color)}"></span><span class="fname">${esc(t.name)}</span></div>`);
    el.onclick=()=>{ Mail.view='folder'; Mail.search=null; Mail.offset=0; Mail.filter=active?'all':'tag:'+t.keyword; renderMailFolders(accId); loadMailMessages(accId); };
    el.onkeydown=(e)=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); el.click(); } };
    box.appendChild(el);
  });
}
function mailTagPicker(anchor, ids){
  mailClosePops();
  const items=Mail.items.filter(m=>ids.includes(m.id));
  const has=(kw)=>items.length && items.every(m=>(m.keywords||[]).includes(kw));
  const pop=h(`<div class="mail-pop" role="menu"><div class="mail-pop-list">
    ${Mail.tags.map(t=>`<button class="mail-pop-item" data-kw="${esc(t.keyword)}"><span class="mdot" style="--tag:${esc(t.color)}"></span> ${has(t.keyword)?'✓ ':''}${esc(t.name)}</button>`).join('')||'<div class="muted small" style="padding:8px">Меток нет</div>'}
    <button class="mail-pop-item muted" data-cfg>⚙️ Настроить метки…</button></div></div>`);
  pop.querySelectorAll('[data-kw]').forEach(b=>b.onclick=async()=>{
    pop.remove();
    const kw=b.dataset.kw, on=!has(kw);
    try{ await api('/webmail/tag', {method:'POST', body:{account_id:Mail.acc, ids, keyword:kw, on}}); }catch(e){ return toastErr(e); }
    Mail.items.forEach(m=>{ if(!ids.includes(m.id)) return; m.keywords=(m.keywords||[]).filter(k=>k!==kw); if(on) m.keywords.push(kw); });
    renderMailList(Mail.acc);
    const tg=$('#mtags'); if(tg && Mail.cur && ids.includes(Mail.cur)){ const it=Mail.items.find(x=>x.id===Mail.cur); tg.innerHTML=mailTagChips(it&&it.keywords); }
  });
  pop.querySelector('[data-cfg]').onclick=()=>{ pop.remove(); mailSettingsDialog(Mail.acc, 'tags'); };
  mailPopAt(pop, anchor);
}

// --- когда: «напомнить позже» и «отправить позже»
function mailWhenOptions(){
  const now=new Date(), at=(d,h,m=0)=>{ const x=new Date(d); x.setHours(h,m,0,0); return x; };
  const tomorrow=new Date(now); tomorrow.setDate(now.getDate()+1);
  const monday=new Date(now); monday.setDate(now.getDate()+((8-now.getDay())%7||7));
  const opts=[];
  if(now.getHours()<17) opts.push(['Сегодня в 18:00', at(now,18)]);
  opts.push(['Через 3 часа', new Date(now.getTime()+3*3600e3)]);
  opts.push(['Завтра в 9:00', at(tomorrow,9)]);
  opts.push(['В понедельник в 9:00', at(monday,9)]);
  const week=new Date(now); week.setDate(now.getDate()+7); opts.push(['Через неделю', at(week,9)]);
  return opts;
}
function mailWhenPicker(anchor, verb, onPick){
  mailClosePops();
  const fmt=(d)=>d.toLocaleString(UI_LOCALE,{weekday:'short', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'});
  const opts=mailWhenOptions();
  const local=(d)=>{ const z=new Date(d.getTime()-d.getTimezoneOffset()*60000); return z.toISOString().slice(0,16); };
  const pop=h(`<div class="mail-pop mail-when" role="menu"><div class="muted small" style="padding:4px 8px">${esc(verb)}:</div><div class="mail-pop-list">
    ${opts.map((o,i)=>`<button class="mail-pop-item" data-i="${i}">${esc(o[0])} <span class="muted small">${esc(fmt(o[1]))}</span></button>`).join('')}</div>
    <div class="mail-when-own"><input type="datetime-local" aria-label="Своё время" value="${local(opts[Math.min(2,opts.length-1)][1])}"><button class="btn sm primary" data-own>OK</button></div></div>`);
  pop.querySelectorAll('[data-i]').forEach(b=>b.onclick=()=>{ pop.remove(); const o=opts[parseInt(b.dataset.i)]; onPick(o[1].toISOString(), `${o[0]} (${fmt(o[1])})`); });
  pop.querySelector('[data-own]').onclick=()=>{ const v=pop.querySelector('input').value; if(!v) return; const d=new Date(v);
    if(isNaN(d.getTime()) || d<=new Date()) return toastErr({message:'Выберите время в будущем.'}); pop.remove(); onPick(d.toISOString(), fmt(d)); };
  mailPopAt(pop, anchor, 300);
}
function mailSnoozePicker(anchor, ids){
  mailWhenPicker(anchor, 'Напомнить', async(iso, label)=>{
    let r; try{ r=await api('/webmail/snooze', {method:'POST', body:{account_id:Mail.acc, ids, until:iso}}); }catch(e){ return toastErr(e); }
    ids.forEach(id=>Mail.picked.delete(Mail.acc+':'+id)); renderPickBar();
    toast('Напомню позже', `Писем: ${fmtNum(r.snoozed)} — вернутся во «Входящие» непрочитанными: ${label}. Пока — в папке «${r.folder}».`, 'success', 7000);
    if(Mail.cur && ids.includes(Mail.cur)){ Mail.cur=null; const rd=$('#mreader'); if(rd) rd.innerHTML='<div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div>'; }
    await loadMailFolders(Mail.acc, {quiet:true}); if(!Mail.search) loadMailMessages(Mail.acc, {keep:true});
  });
}

// --- выбор всей папки, архив .zip
function mailSelectAllLink(){
  if(Mail.view!=='folder' || Mail.search || !Mail.items.length || Mail.total<=Mail.items.length) return '';
  const pageAll=Mail.items.every(m=>Mail.picked.has(Mail.acc+':'+m.id));
  return pageAll?`<a href="#" data-allfolder>Выбрать все ${fmtNum(Mail.total)} ${plural(Mail.total,'письмо','письма','писем')} папки</a>`:'';
}
async function mailPickWholeFolder(){
  const fi=mailFolderInfo(Mail.folder);
  const showGone=Mail.showGone || !mailOnline() || (fi && fi.on_server===false);
  let r; try{ r=await api(`/webmail/ids?${new URLSearchParams({account_id:String(Mail.acc), folder:Mail.folder, filter:Mail.filter, show_gone:showGone?'true':'false'})}`); }catch(e){ return toastErr(e); }
  r.ids.forEach(id=>{ const k=Mail.acc+':'+id; if(!Mail.picked.has(k)) Mail.picked.set(k, {acc:Mail.acc, id, subject:''}); });
  renderPickBar();
  if(r.more) toast('Выбраны не все', `За раз — не больше ${fmtNum(r.ids.length)} писем.`, 'warn');
}
async function mailZip(ids){
  if(!ids.length) return;
  toast('Готовлю архив', `${fmtNum(ids.length)} ${plural(ids.length,'письмо','письма','писем')} — скачивание начнётся само.`);
  try{ await apiDownload('/webmail/zip', {account_id:Mail.acc, ids}, 'Письма.zip'); }catch(e){ toastErr(e); }
}
async function mailFolderAction(a, f){
  const acc=Mail.acc;
  if(a==='readall'){
    try{ const r=await api('/webmail/folders/read_all', {method:'POST', body:{account_id:acc, folder:f.name}}); toast('Прочитано', `Писем: ${fmtNum(r.marked)}`); }catch(e){ return toastErr(e); }
    await loadMailFolders(acc, {quiet:true}); if(Mail.folder===f.name && !Mail.search) loadMailMessages(acc, {keep:true}); return;
  }
  if(a==='empty'){
    const n=f.total||0;
    if(!await confirmDlg(`Очистить «${mailFolderLabel(f)}»?`, `${n?`${fmtNum(n)} ${plural(n,'письмо','письма','писем')} будут удалены`:'Все письма папки будут удалены'} из ящика на сервере насовсем. Копии в архиве останутся.`, {okText:'Очистить'})) return;
    try{ const r=await api('/webmail/folders/empty', {method:'POST', body:{account_id:acc, folder:f.name}}); toast('Папка очищена', `Удалено с сервера: ${fmtNum(r.deleted)}`); }catch(e){ return toastErr(e); }
    await loadMailFolders(acc, {quiet:true}); if(Mail.folder===f.name && !Mail.search) loadMailMessages(acc, {keep:true}); return;
  }
  if(a==='rules'){
    try{ const r=await api('/webmail/rules/apply', {method:'POST', body:{account_id:acc, folder:f.name}});
      toast('Правила применены', `Проверено писем: ${fmtNum(r.checked)}, совпало: ${fmtNum(r.matched)}, перенесено: ${fmtNum(r.moved)}`);
      (r.errors||[]).forEach(t=>toast('Не всё получилось', t, 'warn', 8000));
    }catch(e){ return toastErr(e); }
    await loadMailFolders(acc, {quiet:true}); if(!Mail.search) loadMailMessages(acc, {keep:true}); return;
  }
  if(a==='zip'){
    let r; try{ r=await api(`/webmail/ids?${new URLSearchParams({account_id:String(acc), folder:f.name})}`); }catch(e){ return toastErr(e); }
    if(!r.ids.length) return toast('Папка пуста', '', 'warn');
    return mailZip(r.ids.slice(0, 2000));
  }
  return mailFolderDialog(a, f.name);
}

// --- просмотр вложений: картинки, PDF, текст
function mailPreviewKind(a){
  const t=(a.content_type||'').toLowerCase(), n=(a.filename||'').toLowerCase(), size=a.size||0;
  if(/^image\/(png|jpe?g|gif|webp|bmp)$/.test(t) && size<25*1024*1024) return 'image';
  if((t==='application/pdf' || n.endsWith('.pdf')) && size<50*1024*1024) return 'pdf';
  if((/^text\/(plain|csv)$/.test(t) || /\.(txt|log|csv)$/.test(n)) && size<2*1024*1024) return 'text';
  return '';
}
async function mailPreview(att, url, list, urlOf){
  let idx=Math.max(0, list.findIndex(x=>x.index===att.index));
  const m=modal(att.filename||'Вложение', '<div class="mail-preview"><div class="spinner"></div></div>', {wide:true,
    footer:`${list.length>1?'<button class="btn ghost" data-prev>←</button><button class="btn ghost" data-next>→</button>':''}<span class="spacer" style="flex:1"></span><a class="btn" data-dl download>⬇ Скачать</a><button class="btn primary" data-c>Закрыть</button>`});
  m.el.querySelector('.modal').classList.add('preview');
  let blobUrl=null;
  const show=async()=>{
    const a=list[idx], u=urlOf(a), kind=mailPreviewKind(a);
    m.el.querySelector('.modal-head h2').textContent=a.filename||'Вложение';
    m.foot.querySelector('[data-dl]').href=u;
    const box=m.body.querySelector('.mail-preview'); box.innerHTML='<div class="spinner"></div>';
    try{
      const res=await fetch(u, {headers:{'X-Requested-With':'fetch'}}); if(!res.ok) throw new Error('Ошибка '+res.status);
      const buf=await res.arrayBuffer();
      if(blobUrl){ URL.revokeObjectURL(blobUrl); blobUrl=null; }
      if(kind==='text'){
        let text; try{ text=new TextDecoder('utf-8',{fatal:true}).decode(buf); }catch(e){ text=new TextDecoder('windows-1251').decode(buf); }
        box.innerHTML=''; const pre=document.createElement('pre'); pre.className='mail-preview-text'; pre.textContent=text; box.appendChild(pre);
      }else{
        blobUrl=URL.createObjectURL(new Blob([buf], {type:kind==='pdf'?'application/pdf':(a.content_type||'image/png')}));
        box.innerHTML=kind==='pdf'?`<iframe class="mail-preview-pdf" title="${esc(a.filename||'PDF')}" src="${blobUrl}"></iframe>`:`<img src="${blobUrl}" alt="${esc(a.filename||'')}">`;
      }
    }catch(e){ box.innerHTML=`<div class="empty small">Не удалось открыть: ${esc(e.message)}</div>`; }
  };
  const p=m.foot.querySelector('[data-prev]'), n=m.foot.querySelector('[data-next]');
  if(p) p.onclick=()=>{ idx=(idx-1+list.length)%list.length; show(); };
  if(n) n.onclick=()=>{ idx=(idx+1)%list.length; show(); };
  m.foot.querySelector('[data-c]').onclick=m.close;
  const cleanup=new MutationObserver(()=>{ if(!document.body.contains(m.el)){ if(blobUrl) URL.revokeObjectURL(blobUrl); cleanup.disconnect(); } });
  cleanup.observe(document.body, {childList:true});
  show();
}

// --- сведения о письме: важность, уведомление о прочтении, приглашение, отписка
function mailRenderExtras(el, accId, pk, x, {act, send}){
  Mail.extras=x;
  const pr=el.querySelector('#mprio');
  if(pr && x.priority==='high') pr.innerHTML=' <span class="rp-tag hot" title="Отправитель отметил письмо как важное">❗ Важное</span>';
  else if(pr && x.priority==='low') pr.innerHTML=' <span class="rp-tag" title="Отправитель отметил письмо как неважное">↓ неважное</span>';
  const box=el.querySelector('#mextras'); if(!box){ mailRenderTrust(el, accId, pk, x, act); return; }
  box.innerHTML='';
  if(x.receipt && act && send){
    const b=h(`<div class="hint mail-banner">📬 Отправитель просит уведомить его о прочтении письма (${esc(x.receipt.to)}).
      <span class="btn-row"><button class="btn sm" data-y>Отправить уведомление</button><button class="btn sm ghost" data-n>Не отправлять</button></span></div>`);
    const go=async(sendIt)=>{ try{ await api('/webmail/receipt', {method:'POST', body:{account_id:accId, id:pk, send:sendIt}}); b.remove(); if(sendIt) toast('Уведомление отправлено', x.receipt.to); }catch(e){ toastErr(e); } };
    b.querySelector('[data-y]').onclick=()=>go(true); b.querySelector('[data-n]').onclick=()=>go(false);
    box.appendChild(b);
  }
  if(x.invite){
    const v=x.invite;
    const st={ACCEPTED:'вы приняли', TENTATIVE:'вы ответили «под вопросом»', DECLINED:'вы отклонили', 'NEEDS-ACTION':'ещё не ответили'}[v.my_status]||'';
    const c=h(`<div class="mail-invite"><div class="mail-invite-t">📅 ${esc(v.method==='CANCEL'?'Встреча отменена: ':'Приглашение: ')}${esc(v.summary||'(без названия)')}</div>
      <div class="small">${v.start?`<b>Когда:</b> ${esc(v.start)}${v.end?' — '+esc(v.end):''}<br>`:''}${v.location?`<b>Где:</b> ${esc(v.location)}<br>`:''}${v.organizer?`<b>Организатор:</b> ${esc(v.organizer_name?v.organizer_name+' <'+v.organizer+'>':v.organizer)}<br>`:''}${v.attendees&&v.attendees.length?`<b>Участники:</b> ${esc(v.attendees.slice(0,8).map(a=>a.name||a.email).join(', '))}${v.attendees.length>8?` и ещё ${v.attendees.length-8}`:''}<br>`:''}${st?`<span class="muted">Сейчас: ${esc(st)}</span>`:''}</div>
      <div class="btn-row" style="margin-top:8px">${v.can_reply&&send?'<button class="btn sm primary" data-p="ACCEPTED">✓ Принять</button><button class="btn sm" data-p="TENTATIVE">? Под вопросом</button><button class="btn sm" data-p="DECLINED">✗ Отклонить</button>':''}
        <a class="btn sm ghost" href="/api/webmail/invite.ics?account_id=${accId}&id=${pk}" download>📅 Добавить в календарь (.ics)</a></div></div>`);
    c.querySelectorAll('[data-p]').forEach(b=>b.onclick=async()=>{
      c.querySelectorAll('[data-p]').forEach(x=>x.disabled=true);
      try{ const r=await api('/webmail/invite', {method:'POST', body:{account_id:accId, id:pk, partstat:b.dataset.p}}); toast('Ответ отправлен', `${b.textContent.trim()} — организатору ${r.to}${r.calendar?'. Событие добавлено в календарь.':''}`); }
      catch(e){ toastErr(e); c.querySelectorAll('[data-p]').forEach(x=>x.disabled=false); }
    });
    box.appendChild(c);
  }
  if(x.unsubscribe && (x.unsubscribe.url || x.unsubscribe.mailto)){
    const u=h(`<div class="mail-unsub small muted">Это рассылка. <a href="#" data-u>✋ Отписаться</a></div>`);
    u.querySelector('[data-u]').onclick=(e)=>{ e.preventDefault(); mailUnsubscribe(x.unsubscribe); };
    box.appendChild(u);
  }
  mailRenderTrust(el, accId, pk, x, act);
}
function mailUnsubscribe(u){
  if(!u) return;
  if(u.url){
    window.open(u.url, '_blank', 'noopener,noreferrer');
    toast('Страница отписки открыта', 'Если сайт попросит подтвердить — подтвердите там. Письма рассылки можно и заблокировать: «⋯ → Заблокировать отправителя».', 'success', 8000);
    return;
  }
  const mm=/^mailto:([^?]+)(?:\?(.*))?$/i.exec(u.mailto||''); if(!mm) return;
  const q=new URLSearchParams(mm[2]||'');
  let to=mm[1]; try{ to=decodeURIComponent(to); }catch(e){ /* адрес без %-кодирования */ }
  mailCompose('new', null, {to_addrs:to.slice(0,300), subject:(q.get('subject')||'unsubscribe').slice(0,200), body:q.get('body')?`<p>${esc(q.get('body').slice(0,2000))}</p>`:''});
}
async function mailAddToBook(accId, from){
  const mm=/^(.*?)\s*<([^>]+)>\s*$/.exec(from||'');
  const name=mm?mm[1].replace(/^"|"$/g,'').trim():'', email=(mm?mm[2]:from||'').trim();
  if(!email) return;
  try{ await api('/webmail/book', {method:'POST', body:{account_id:accId, name, email}}); toast('Добавлено в адресную книгу', name?`${name} <${email}>`:email); if(Mail.extras) Mail.extras.in_book=true; }
  catch(e){ toastErr(e); }
}
async function mailBlockSender(accId, addr, ids){
  if(!addr) return;
  if(!await confirmDlg(`Заблокировать ${addr}?`, 'Это письмо и все следующие от этого адреса будут уходить в «Спам» (нет «Спама» — в корзину). Разблокировать — «⚙️ → Правила».', {okText:'Заблокировать'})) return;
  try{ const r=await api('/webmail/block', {method:'POST', body:{account_id:accId, address:addr, ids:ids||[]}}); toast('Отправитель заблокирован', r.address); }
  catch(e){ return toastErr(e); }
  if(Mail.cur && (ids||[]).includes(Mail.cur)){ Mail.cur=null; const rd=$('#mreader'); if(rd) rd.innerHTML='<div class="empty" style="padding:40px 20px"><div class="big">✉️</div>Выберите письмо для просмотра</div>'; }
  await loadMailFolders(accId, {quiet:true}); if(!Mail.search) loadMailMessages(accId, {keep:true});
}
/** Быстрый ответ под письмом. */
function mailQuickReply(el, accId, pk){
  const q=h(`<div class="mail-quick"><textarea rows="2" placeholder="Быстрый ответ отправителю… (Ctrl+Enter — отправить)" aria-label="Быстрый ответ"></textarea>
    <div class="btn-row"><button class="btn sm primary" data-send>📨 Ответить</button><button class="btn sm ghost" data-full>Открыть в редакторе</button></div></div>`);
  el.appendChild(q);
  const ta=q.querySelector('textarea');
  ta.addEventListener('focus', ()=>q.classList.add('open'));
  ta.addEventListener('keydown', (e)=>{ e.stopPropagation(); if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); q.querySelector('[data-send]').click(); } });
  const lead=()=>ta.value.trim()?`<p>${esc(ta.value.trim()).replace(/\n/g,'<br>')}</p>`:'';
  q.querySelector('[data-full]').onclick=async()=>{
    let d; try{ d=await api('/webmail/drafts', {method:'POST', body:{account_id:accId, mode:'reply', ref_id:pk}}); }catch(e){ return toastErr(e); }
    if(lead()){ try{ d=await api(`/webmail/drafts/${d.id}`, {method:'PUT', body:{body:lead()+d.body}}); }catch(e){ /* текст останется в поле */ } }
    ta.value=''; q.classList.remove('open'); Mail.drafts.unshift(d); composeWindow(d);
  };
  q.querySelector('[data-send]').onclick=async()=>{
    if(!ta.value.trim()) return ta.focus();
    const btn=q.querySelector('[data-send]'); btn.disabled=true;
    try{
      let d=await api('/webmail/drafts', {method:'POST', body:{account_id:accId, mode:'reply', ref_id:pk}});
      d=await api(`/webmail/drafts/${d.id}`, {method:'PUT', body:{body:lead()+d.body}});
      const undoS=(Mail.st&&Mail.st.undo_send_s)||0;
      if(undoS>0 && await mailUndoWait(undoS, d.subject)){ await api(`/webmail/drafts/${d.id}`, {method:'DELETE'}).catch(()=>{}); btn.disabled=false; return; }
      const r=await api(`/webmail/drafts/${d.id}/send`, {method:'POST'});
      ta.value=''; q.classList.remove('open');
      toast('Ответ отправлен', `Получателей: ${fmtNum(r.recipients)}`);
      (r.warnings||[]).forEach(t=>toast('Обратите внимание', t, 'warn', 8000));
      const it=Mail.items.find(x=>x.id===pk); if(it){ it.answered=true; renderMailList(accId); }
    }catch(e){ toastErr(e); }
    btn.disabled=false;
  };
}

// --- окно письма: шаблоны, забытое вложение
function composeMentionsAttachment(body){
  const clone=body.cloneNode(true);
  clone.querySelectorAll('blockquote,.ma-signature').forEach(x=>x.remove());
  const text=(clone.textContent||'').toLowerCase();
  return /(во вложени|прилага|прикрепл|прикладыва|см\. файл|в файле|attached|attachment|enclosed)/.test(text);
}
async function composeTemplateMenu(anchor, w, body, touch){
  let list=[]; try{ list=(await api('/webmail/templates')).items||[]; }catch(e){ return toastErr(e); }
  mailClosePops();
  const pop=h(`<div class="mail-pop" role="menu"><div class="mail-pop-list">
    ${list.map(t=>`<button class="mail-pop-item" data-t="${t.id}" title="${esc(t.subject||'')}">📄 ${esc(t.name)}</button>`).join('')||'<div class="muted small" style="padding:8px">Шаблонов пока нет</div>'}
    <button class="mail-pop-item" data-save>💾 Сохранить это письмо как шаблон…</button>
    <button class="mail-pop-item muted" data-cfg>⚙️ Все шаблоны…</button></div></div>`);
  pop.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>{
    pop.remove();
    const t=list.find(x=>String(x.id)===b.dataset.t); if(!t) return;
    const subj=w.querySelector('[data-f="subject"]');
    if(subj && !subj.value.trim() && t.subject) subj.value=t.subject;
    body.focus();
    const sig=body.querySelector('.ma-signature'), quote=body.querySelector('blockquote');
    const holder=document.createElement('div'); holder.innerHTML=t.body||'';
    const before=sig||quote;
    if(!body.textContent.trim() || !before){ if(before) before.before(...holder.childNodes); else body.append(...holder.childNodes); }
    else document.execCommand('insertHTML', false, t.body||'');
    touch();
  });
  pop.querySelector('[data-save]').onclick=async()=>{
    pop.remove();
    const subj=(w.querySelector('[data-f="subject"]')||{}).value||'';
    const clone=body.cloneNode(true); clone.querySelectorAll('blockquote,.ma-signature').forEach(x=>x.remove());
    mailTemplateEditor({name:subj||'Шаблон', subject:subj, body:clone.innerHTML});
  };
  pop.querySelector('[data-cfg]').onclick=()=>{ pop.remove(); mailSettingsDialog(Mail.acc, 'templates'); };
  mailPopAt(pop, anchor);
}

// --- новая почта: уведомления браузера, число непрочитанных во вкладке
function mailInboxUnseen(){ const f=Mail.folders.find(x=>x.special==='inbox'); return f && f.unseen!=null ? f.unseen : null; }
function mailUpdateTitle(){
  if(!Mail.baseTitle) Mail.baseTitle=document.title.replace(/^\(\d+\)\s*/, '');
  const n=State.view==='mail'?mailInboxUnseen():null;
  document.title=n?`(${n}) ${Mail.baseTitle}`:Mail.baseTitle;
}
function mailResetTitle(){ if(Mail.baseTitle) document.title=Mail.baseTitle; }
function mailNotifyNew(){
  const n=mailInboxUnseen();
  const prev=Mail.inboxUnseen; Mail.inboxUnseen=n;
  mailUpdateTitle();
  if(prev==null || n==null || n<=prev) return;
  if(!mailPrefs().notify || !('Notification' in window) || Notification.permission!=='granted') return;
  if(!document.hidden) return;
  try{ const nt=new Notification('Новая почта', {body:`Непрочитанных во «Входящих»: ${n}`, tag:'ma-mail', icon:'/static/favicon.svg'});
    nt.onclick=()=>{ window.focus(); nt.close(); }; }catch(e){ /* уведомления недоступны */ }
}
/** Проверить «Входящие», когда открыта другая папка (для счётчика и уведомлений). */
async function mailSyncInbox(){
  const inbox=Mail.special.inbox, acc=Mail.acc; if(!inbox || !mailOnline()) return;
  try{ const r=await api('/webmail/sync', {method:'POST', body:{account_id:acc, folder:inbox}, bg:true});
    if(Mail.acc===acc && (r.new || r.flags || r.gone)) loadMailFolders(acc, {quiet:true}); }catch(e){ /* проверим в следующий раз */ }
}

