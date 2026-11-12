/* MailArchiver — веб-интерфейс: «Задания», «Выгрузки», «Расписания», «Логи».
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Очередь и задания
// ===================================================================
// Отбор по состоянию и «Показать ещё»: раньше были видны только последние 80 заданий, и после ночного
// копирования тысяч ящиков утренние отказы терялись среди удачных.
const JobsView={status:'', limit:80};
const JOBS_PAGE=80, JOBS_MAX=1000;
function jobsQuery(){ return `/jobs?limit=${JobsView.limit}${JobsView.status?'&status='+encodeURIComponent(JobsView.status):''}`; }
async function viewJobs(c){
  const jobs=await api(jobsQuery());
  c.innerHTML='';
  const opts=[['','Все'],['failed','С ошибкой'],['partial','Частично'],['running','Выполняются'],['queued','В очереди'],
    ['cancelled','Отменены'],['success','Успешные']];
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Очередь и задания</h2><div class="spacer"></div>
    <select id="jobsStatus" aria-label="Отбор по состоянию" style="width:auto;padding:6px 10px">${opts.map(([v,t])=>`<option value="${v}" ${v===JobsView.status?'selected':''}>${t}</option>`).join('')}</select>
    <span class="muted small">обновляется автоматически</span></div>`));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>#</th><th>Тип</th><th>Ящик</th><th>Статус</th><th>Прогресс</th><th>Создано</th><th></th></tr></thead><tbody id="jobsBody"></tbody></table></div>');
  c.appendChild(wrap);
  const more=h('<div style="margin-top:10px;text-align:center"><button class="btn" id="jobsMore">Показать ещё</button></div>');
  c.appendChild(more);
  const syncMore=(n)=>{ more.hidden = n<JobsView.limit || JobsView.limit>=JOBS_MAX; };
  $('#jobsStatus',c).onchange=(e)=>{ JobsView.status=e.target.value; JobsView.limit=JOBS_PAGE; jobsMissingSeen=new Set(); route(); };
  $('#jobsMore',c).onclick=async()=>{
    JobsView.limit=Math.min(JOBS_MAX, JobsView.limit+JOBS_PAGE);
    try{ const list=await api(jobsQuery()); jobsRefreshAt=Date.now(); renderJobsRows(list); syncMore(list.length); }catch(e){ toastErr(e); }
  };
  jobsRefreshAt = Date.now();
  renderJobsRows(jobs);
  syncMore(jobs.length);
}
function renderJobsRows(jobs){
  const tb=$('#jobsBody'); if(!tb) return; tb.innerHTML='';
  if(!jobs.length){ tb.innerHTML=`<tr><td colspan="7" class="empty">${JobsView.status?'Таких заданий нет':'Заданий пока нет'}</td></tr>`; return; }
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
let jobsMissingSeen = new Set();   // активные задания, которых нет среди последних 80 (уже перезапрашивали)
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
    // Строки нет — появилось новое задание: перечитаем список целиком, но один раз. Если и после этого
    // строки нет (в очереди сотни заданий, а в таблице — последние 80), не перечитываем на каждом такте.
    const missing=new Set();
    active.forEach(j=>{
      const row=tb.querySelector(`tr[data-job="${j.id}"]`);
      if(!row) { missing.add(j.id); return; }
      const cell=row.querySelector('[data-prog]');
      if(cell) cell.innerHTML = jobProgressHtml(j);
      const badge=row.querySelector('.badge');
      if(badge){ badge.className='badge '+j.status; badge.textContent=j.status_label; }
    });
    const fresh=[...missing].some(id=>!jobsMissingSeen.has(id));
    jobsMissingSeen=missing;
    if(!fresh) return;
  }
  try{ jobsRefreshAt=now; const jobs=await api(jobsQuery(),{bg:true}); if(State.view==='jobs') renderJobsRows(jobs); }catch(e){}
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
const EXPORTS_PAGE=200;
async function viewExports(c){
  const list=await api('/exports?limit='+EXPORTS_PAGE);
  // у сотрудника может быть несколько ящиков: выгрузить можно любой из них
  const mineList = isMailbox() && State.user.account_id ? myAccounts() : [];
  const mine = mineList.length ? mineList[0] : null;
  const mineLabel = mineList.length>1 ? 'Выгрузить ящик' : 'Выгрузить мой ящик';
  c.innerHTML=''; c.appendChild(h(`<div class="section-title"><h2 style="margin:0">Готовые экспорты</h2><div class="spacer"></div>
    ${mine?`<button class="btn primary" id="expMine">📤 ${mineLabel}</button>`:''}</div>`));
  if(mine) $('#expMine',c).onclick=()=>{
    if(mineList.length===1) return exportModal(mine);
    const m=modal('Какой ящик выгрузить', `<div class="form-row"><label for="xm-acc">Ящик</label><select id="xm-acc">
      ${mineList.map(a=>`<option value="${a.id}">${esc(a.name)}${a.username?' — '+esc(a.username):''}</option>`).join('')}</select></div>`,
      {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Далее</button>`});
    m.foot.querySelector('[data-c]').onclick=m.close;
    m.foot.querySelector('[data-go]').onclick=()=>{
      const id=parseInt(m.body.querySelector('#xm-acc').value); m.close();
      exportModal(mineList.find(a=>a.id===id)||mine);
    };
  };
  if(!list.length){
    const how = mine ? `Нажмите «${mineLabel}».` : 'Откройте «Почтовые ящики» → меню ящика (⋯) → «Экспорт».';
    c.appendChild(h(`<div class="card"><div class="empty"><div class="big">📤</div>Экспортов пока нет.<br>${how}</div></div>`)); return;
  }
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Ящик</th><th>Формат</th><th>Размер</th><th>Статус</th><th>Создан</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  const addRows=(rows)=>rows.forEach(e=>{
    const tr=h(`<tr data-id="${e.id}">
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
  addRows(list);
  c.appendChild(wrap);
  // «Показать ещё»: раньше были видны только последние 200 выгрузок
  const more=h('<div style="margin-top:10px;text-align:center"><button class="btn">Показать ещё</button></div>');
  more.hidden=list.length<EXPORTS_PAGE;
  c.appendChild(more);
  more.querySelector('button').onclick=async()=>{
    const last=tb.querySelector('tr[data-id]:last-child');
    try{
      const rows=await api(`/exports?limit=${EXPORTS_PAGE}&before_id=${last?last.dataset.id:''}`);
      addRows(rows); more.hidden=rows.length<EXPORTS_PAGE;
    }catch(e){ toastErr(e); }
  };
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

