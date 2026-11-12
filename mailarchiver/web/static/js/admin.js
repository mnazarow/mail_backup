/* MailArchiver — веб-интерфейс: Дашборд, «Почтовые ящики», «Сотрудники».
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

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
    <div class="card stat-card"><div class="label">✉️ Писем в архиве</div><div class="value">${s.totals.messages.toLocaleString(UI_LOCALE)}</div><div class="sub">${esc(s.totals.bytes_h)}</div></div>
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
  updateDashboardLive({active_jobs:s.active_jobs, job_counts:s.job_counts, queued_hidden:s.queued_hidden});

  const grid=h(`<div class="grid cols-2">
    <div class="card"><div class="section-title"><h3>📈 Активность</h3><span class="muted small">новые письма за 30 дней</span></div><div class="an-chart" id="chartBox"></div></div>
    <div class="card"><div class="section-title"><h3>📬 ${isMailbox()?(myAccounts().length>1?'Мои ящики':'Мой ящик'):'Ящики'}</h3>${isMailbox()?'':`<span class="tag">${s.totals.accounts}</span><div class="spacer"></div>${isAdmin()?'<button class="btn sm primary" id="addAcc">+ Добавить</button>':''}`}</div><div id="accList"></div></div>
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
  // Ждущих сервер присылает первые 50: когда расписания ставят в очередь сотни ящиков разом, весь список
  // каждые полторы секунды и тормозил вкладку, и не помещался на экран — он есть в «Очереди и заданиях».
  if(d.queued_hidden>0){
    box.appendChild(h(`<div class="kv" style="justify-content:center"><a href="#/jobs" class="muted small">…и ещё ${fmtNum(d.queued_hidden)} ${plural(d.queued_hidden,'задание','задания','заданий')} в очереди — открыть «Очередь и задания»</a></div>`));
  }
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
const Emp = { query:'', status:'', offset:0, limit:100, seq:0 };

async function viewEmployees(c){
  let d;
  const seq=++Emp.seq;      // ответ на прежний запрос (быстрый ввод в поиске) список не перерисовывает
  const q = new URLSearchParams({limit:Emp.limit, offset:Emp.offset});
  if(Emp.query) q.set('query', Emp.query);
  if(Emp.status) q.set('status', Emp.status);
  try{ d = await api('/employees?'+q.toString()); }
  catch(e){ if(seq!==Emp.seq) return; toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить список сотрудников</div></div>'; return; }
  // Сведения об источнике не критичны: если запрос не прошёл, список всё равно показываем.
  let src=null; try{ src=(await api('/employees/source')).source; }catch(e){}
  if(seq!==Emp.seq) return;
  const cnt = d.counts||{};
  // Раздел перерисовывается целиком: поле поиска, в котором человек печатает, сохраняет фокус и всё, что
  // успели напечатать, пока шёл запрос (раньше фокус пропадал после каждой паузы в наборе).
  const oldQ=$('#empQ',c), hadFocus=!!oldQ && document.activeElement===oldQ, typed=oldQ?oldQ.value:'';
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
  const qi=$('#empQ',bar);
  qi.oninput=(e)=>{ clearTimeout(t); t=setTimeout(()=>{ Emp.query=e.target.value.trim(); Emp.offset=0; viewEmployees(c); }, 400); };
  $('#empStatus',bar).onchange=(e)=>{ Emp.status=e.target.value; Emp.offset=0; viewEmployees(c); };
  if(hadFocus){
    qi.value=typed; qi.focus(); qi.setSelectionRange(typed.length, typed.length);
    if(typed.trim()!==Emp.query) qi.oninput({target:qi});      // допечатали, пока шёл запрос
  }

  if(!d.employees.length){
    c.appendChild(h(`<div class="card"><div class="empty"><div class="big">🧑‍💼</div>
      ${Emp.query||Emp.status?'Ничего не найдено по заданному условию.':'Справочник пуст.<br>Добавьте сотрудника вручную или загрузите список из файла.'}</div></div>`));
    return;
  }

  const wrap=h(`<div class="card table-wrap"><table class="tbl"><thead><tr>
    <th>ФИО</th><th>E-mail</th><th>Должность</th><th>Отдел</th><th>Почтовые ящики</th><th>Статус</th><th></th>
  </tr></thead><tbody></tbody></table></div>`);
  const tb=wrap.querySelector('tbody');
  d.employees.forEach(e=>{
    // У сотрудника может быть несколько ящиков: показываем первые два и «ещё N».
    const boxes=e.accounts||[];
    const box = boxes.length
      ? boxes.slice(0,2).map(a=>`<div class="nowrap"><span class="tag" title="${esc(a.name)} — ${esc(a.username)}">${esc(trunc(a.name||'ящик',20))}</span>
         <span class="badge ${a.enabled?'success':'queued'}" title="${a.enabled?'Ящик участвует в копировании':'Ящик выключен: задайте пароль и включите его в разделе «Почтовые ящики»'}">${a.enabled?'вкл':'выкл'}</span></div>`).join('')
        + (boxes.length>2?`<div class="muted small">и ещё ${boxes.length-2}</div>`:'')
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
      '<div id="e-boxes" style="margin-top:6px"></div>'}`;
  const m=modal(isNew?'Новый сотрудник':`Сотрудник: ${e.full_name}`, body,
    {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=x=>m.body.querySelector(x);
  if(!isNew) employeeBoxes(e, g('#e-boxes'));
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
          const d=r.dismissed, many=(d.accounts||[]).length>1;
          const what=({final_backup:`последняя копия ${many?'ящиков':'ящика'} поставлена в очередь, затем копирование выключится`, disabled:`копирование ${many?'ящиков':'ящика'} выключено`, kept:`копирование ${many?'ящиков':'ящика'} продолжается`, already_disabled:`копирование ${many?'ящиков':'ящика'} уже было выключено`, mixed:'копирование ящиков выключается после последней копии (подробности — в аудите)'})[d.action]||'';
          toast('Сотрудник уволен', `Архив ${many?'ящиков':'ящика'} «${d.account}» удерживается ${d.hold_until==='9999-12-31'?'бессрочно':'до '+holdDate(d.hold_until)}; ${what}.`, 'success', 9000);
        } else if(r.rehired && r.rehired.account){
          toast('Сотрудник снова работает', `Удержание снято${r.rehired.enabled?', копирование ящика снова включено':''}.`);
        } else toast('Сохранено');
      }
      m.close(); route();
    }catch(err){ toastErr(err); }
  };
}

/** Ящики сотрудника в его карточке: список, «отвязать», «привязать существующий», «завести ещё один». */
// Перенос ящика: вошедший паролем ящика видит все ящики владельца — открытые сеансы этого ящика
// завершаются, а пароль, который знает прежний владелец, лучше сменить на почтовом сервере.
const MOVE_NOTE='Открытые сеансы этого ящика будут завершены. Кто знает его пароль, увидит архив всех ящиков нового владельца — если ящиком пользовался прежний владелец, смените пароль на почтовом сервере.';
async function employeeBoxes(e, box){
  if(!box) return;
  box.innerHTML='<div class="muted small">Загрузка ящиков…</div>';
  let emp;
  try{ emp=await api(`/employees/${e.id}`); }catch(err){ box.innerHTML=`<div class="hint warnbox">${esc(err.message)}</div>`; return; }
  const boxes=emp.accounts||[];
  box.innerHTML=`<h4 style="margin:8px 0 6px">📬 Почтовые ящики сотрудника (${boxes.length})</h4>
    ${boxes.length?`<div class="table-wrap"><table class="tbl"><tbody>${boxes.map(a=>`<tr>
      <td><strong>${esc(a.name)}</strong><div class="small muted">${esc(a.username)}</div></td>
      <td><span class="badge ${a.enabled?'success':'queued'}">${a.enabled?'включён':'выключен'}</span>${a.hold_until?`<div class="small muted">🔒 ${a.hold_until==='9999-12-31'?'бессрочно':'до '+esc(holdDate(a.hold_until))}</div>`:''}</td>
      <td class="small muted">${a.last_backup_at?'копия '+esc(fmtDate(a.last_backup_at)):'копий ещё нет'}</td>
      <td style="text-align:right"><button class="btn sm ghost" data-unlink="${a.id}" title="Ящик и его архив остаются; он просто перестаёт числиться за сотрудником">Отвязать</button></td></tr>`).join('')}</tbody></table></div>`
      :'<div class="muted small">Ящиков нет — почта сотрудника не копируется.</div>'}
    <div class="btn-row" style="margin-top:8px">
      <button class="btn sm" data-add>+ Завести ящик</button>
      <button class="btn sm ghost" data-link>🔗 Привязать существующий</button></div>
    <div class="hint" style="margin-top:6px">Сотрудник, вошедший паролем любого своего ящика, видит архив всех ящиков из этого списка. Не привязывайте ящик, пароль которого знает кто-то ещё (общий ящик отдела): тот увидит и остальные.</div>`;
  const reload=()=>employeeBoxes(e, box);
  box.querySelectorAll('[data-unlink]').forEach(b=>b.onclick=async()=>{
    const a=boxes.find(x=>String(x.id)===b.dataset.unlink)||{};
    if(!await confirmDlg('Отвязать ящик?', `Ящик «${a.name}» перестанет числиться за сотрудником «${e.full_name}». Сам ящик и его архив остаются, синхронизация не заведёт его заново.`, {okText:'Отвязать'})) return;
    try{ await api(`/employees/${e.id}/accounts/${a.id}`, {method:'DELETE'}); toast('Ящик отвязан'); reload(); }catch(err){ toastErr(err); }
  });
  box.querySelector('[data-add]').onclick=()=>{
    const m=modal('Завести ящик сотруднику', `
      <div class="form-row"><label for="na-mail">Адрес ящика</label><input id="na-mail" type="text" placeholder="i.ivanov@example.ru" value="${boxes.length?'':esc(e.email||'')}"></div>
      <div class="hint">Ящик заводится по шаблону ящиков сотрудников (сервер, логин, папки, расписание) — обычно выключенным и без пароля. Если ящик с таким логином уже есть, он будет привязан.</div>`,
      {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Завести</button>`});
    m.foot.querySelector('[data-c]').onclick=m.close;
    const go=async(move)=>{
      const email=m.body.querySelector('#na-mail').value.trim();
      if(!email) return toastErr({message:'Укажите адрес ящика.'});
      try{
        const r=await api(`/employees/${e.id}/accounts`, {method:'POST', body:{email, move:!!move}});
        m.close();
        toast(r.action==='created'?'Ящик заведён':r.action==='owned_already'?'Этот ящик уже у сотрудника':'Ящик привязан', r.action==='created'?'Задайте пароль и включите его в разделе «Почтовые ящики».':''); reload();
      }catch(err){
        if(!move && /уже принадлежит/.test(err.message||'') && await confirmDlg('Перенести ящик?', `${err.message} Перенести его этому сотруднику? ${MOVE_NOTE}`, {okText:'Перенести', okClass:'primary'})) return go(true);
        toastErr(err);
      }
    };
    m.foot.querySelector('[data-go]').onclick=()=>go(false);
  };
  box.querySelector('[data-link]').onclick=async()=>{
    let accs;
    try{ accs=await api('/accounts'); State.accounts=accs; }catch(err){ return toastErr(err); }
    const mine=new Set(boxes.map(a=>a.id));
    const m=modal('Привязать ящик', `
      <div class="form-row"><input id="la-q" type="text" placeholder="Найти ящик по названию или адресу" aria-label="Найти ящик" style="margin-bottom:8px">
      <select id="la-sel" size="8" aria-label="Ящик"></select></div>
      <div class="hint">Ящик принадлежит одному сотруднику: ящик, уже числящийся за другим, будет перенесён (с подтверждением).</div>`,
      {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Привязать</button>`});
    const q=m.body.querySelector('#la-q'), sel=m.body.querySelector('#la-sel');
    const fill=()=>{ const t=q.value.trim().toLowerCase();
      sel.innerHTML=accs.filter(a=>!mine.has(a.id) && (!t || (a.name||'').toLowerCase().includes(t) || (a.username||'').toLowerCase().includes(t))).slice(0,300)
        .map(a=>`<option value="${a.id}">${esc(a.name)} — ${esc(a.username)}${a.employee_name?' · сейчас: '+esc(a.employee_name):''}</option>`).join(''); };
    fill(); q.oninput=fill;
    m.foot.querySelector('[data-c]').onclick=m.close;
    const go=async(move)=>{
      const id=parseInt(sel.value||'0'); if(!id) return toastErr({message:'Выберите ящик.'});
      try{ await api(`/employees/${e.id}/link-account?account_id=${id}${move?'&move=true':''}`, {method:'POST'}); m.close(); toast('Ящик привязан'); reload(); }
      catch(err){
        if(!move && /уже принадлежит/.test(err.message||'') && await confirmDlg('Перенести ящик?', `${err.message} Перенести его сотруднику «${e.full_name}»? ${MOVE_NOTE}`, {okText:'Перенести', okClass:'primary'})) return go(true);
        toastErr(err);
      }
    };
    m.foot.querySelector('[data-go]').onclick=()=>go(false);
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
  if(uiLang()==='en') return Math.abs(Number(n))===1 ? one : many;   // «письмо/писем» переводит каталог
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
function fmtDay(iso){ if(!iso) return '—'; try{ return new Date(iso).toLocaleDateString(UI_LOCALE,{day:'2-digit',month:'2-digit',year:'numeric'}); }catch(e){ return iso; } }
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
        <td><strong>${esc(a.name)}</strong><div class="small muted nowrap">${esc(a.host)}:${a.port} · ${esc(a.security)}${a.auth_type==='oauth2'?' · OAuth2':(a.auth_type==='master'?' · через администратора':'')}</div>${a.employee_name?`<div class="small muted" title="Сотрудник, которому принадлежит ящик">👤 ${esc(a.employee_name)}</div>`:''}</td>
        <td class="small login">${esc(a.username).replace('@','<wbr>@')}</td>
        <td>${loginCell(a)}</td>
        <td class="bk">${backupCell(a, '<a href="#" data-runs>история</a>')}</td>
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
      <a href="#" data-jobinfo="${job.id}">задание №${job.id}</a>. Итог появится здесь.</div>` : '';
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
    <button class="btn" data-a="delegates" title="Открыть ящик другим сотрудникам: секретарю, отделу (общий ящик)">👥 Доступ других сотрудников</button>
    <button class="btn" data-a="logout" title="Сотрудник, вошедший в интерфейс по паролю этого ящика, будет разлогинен">🚪 Завершить сеансы сотрудника</button>
    <button class="btn" data-a="hold" title="Пока действует удержание, письма ящика не удаляются по сроку хранения">🔒 Удержание архива${a.on_hold?' (до '+esc(holdDate(a.hold_until))+')':''}</button>
    ${axFeature()?'<button class="btn" data-a="axigen" title="Учётная запись этого ящика на сервере Axigen: вход, квота, действия">📮 На сервере Axigen</button>':''}
    <button class="btn danger" data-a="del" ${a.on_hold?'disabled title="Архив удерживается — удалить ящик нельзя"':''}>🗑️ Удалить ящик</button>
    <button class="btn danger" data-a="purge" ${a.on_hold?'disabled title="Архив удерживается — удалить нельзя"':''}>🧹 Удалить ящик вместе с архивом писем</button>
  </div>`);
  m.body.querySelectorAll('[data-a]').forEach(b=>b.onclick=async()=>{
    const act=b.dataset.a; m.close();
    if(act==='backup') backupNow(a.id, a.name);
    else if(act==='mail'){ openMailbox(a.id); }
    else if(act==='edit') accountModal(a.id);
    else if(act==='export') exportModal(a);
    else if(act==='restore') restoreModal(a);
    else if(act==='import') importModal(a);
    else if(act==='retention') retentionModal(a);
    else if(act==='folders') folderDiagnoseModal(a);
    else if(act==='rebuild') rebuildModal(a);
    else if(act==='quarantine') quarantineModal(a);
    else if(act==='hold') holdModal(a);
    else if(act==='delegates') delegatesModal(a);
    else if(act==='axigen') axigenForMailbox(a);
    else if(act==='purge') purgeModal(a);
    else if(act==='logout'){ try{ const r=await api(`/accounts/${a.id}/logout-sessions`,{method:'POST'}); toast('Готово', r.closed?`Завершено сеансов: ${r.closed}`:'Открытых сеансов не было'); }catch(e){toastErr(e);} }
    else if(act==='verify'){ try{await api(`/accounts/${a.id}/verify`,{method:'POST'}); toast('Запущено','Проверка целостности в очереди'); location.hash='#/jobs';}catch(e){toastErr(e);} }
    else if(act==='del'){ if(await confirmDlg('Удалить ящик?', `Ящик «${a.name}» и его настройки будут удалены. Локальные копии писем на диске останутся.${a.on_hold?' Архив ящика удерживается до '+holdDate(a.hold_until)+' — файлы писем не трогаются.':''} Продолжить?`)){ try{await api(`/accounts/${a.id}`,{method:'DELETE'}); toast('Удалено'); route();}catch(e){toastErr(e);} } }
  });
}

/** Доступ других сотрудников к ящику (общие и делегированные ящики, 1.22.0). */
async function delegatesModal(a){
  const m=modal(`👥 Доступ к ящику: ${a.name}`, `<p class="small muted" style="margin-top:0">Сотрудник увидит этот ящик рядом со своими (войдя паролем любого своего ящика): архив, поиск и «Почту». «Только чтение» — читать письма и вложения; «полный доступ» — как свой ящик, в том числе письма от его имени, выгрузка и копирование. Действия — в аудите под логином сотрудника.</p>
    <div data-list><div class="spinner"></div></div>
    <div class="card" style="margin-top:10px"><div class="grid2"><div class="form-row"><label>Сотрудник</label><input type="search" data-q placeholder="ФИО или адрес" autocomplete="off"><div data-found class="small"></div></div>
      <div class="form-row"><label>Права</label><select data-r><option value="read">только чтение</option><option value="full">полный доступ</option></select></div></div>
      <div class="grid2"><div class="form-row"><label>До (необязательно)</label><input type="date" data-u></div><div class="form-row"><label>Примечание</label><input data-n maxlength="300" placeholder="например, на время отпуска"></div></div>
      <div class="btn-row"><button class="btn primary sm" data-add disabled>Открыть доступ</button></div></div>`, {wide:true});
  const g=s=>m.body.querySelector(s); let pick=null;
  const render=(items)=>{ g('[data-list]').innerHTML=items.length?`<table class="tbl"><thead><tr><th>Сотрудник</th><th>Права</th><th>До</th><th>Кто открыл</th><th></th></tr></thead><tbody>${items.map(d=>`<tr><td>${esc(d.employee)}<div class="small muted">${esc(d.email)}${d.note?' · '+esc(d.note):''}</div></td><td>${esc(d.rights_label)}${d.active?'':' <span class="tag">не действует</span>'}</td><td class="small">${d.until?esc(d.until):'бессрочно'}</td><td class="small">${esc(d.granted_by)}<div class="muted">${esc(fmtDate(d.granted_at))}</div></td><td><button class="btn ghost sm" data-del="${d.employee_id}" title="Снять доступ">✕</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty small">Ящик открыт только его владельцу.</div>'; };
  const load=async()=>{ try{ render((await api(`/accounts/${a.id}/delegates`)).items); }catch(e){ g('[data-list]').innerHTML=`<div class="empty small">${esc(e.message)}</div>`; } };
  g('[data-list]').onclick=async(e)=>{ const b=e.target.closest('[data-del]'); if(!b) return;
    try{ render((await api(`/accounts/${a.id}/delegates/${b.dataset.del}`,{method:'DELETE'})).items); toast('Доступ снят'); }catch(err){ toastErr(err); } };
  let t=null;
  g('[data-q]').oninput=()=>{ clearTimeout(t); pick=null; g('[data-add]').disabled=true; t=setTimeout(async()=>{
    const q=g('[data-q]').value.trim(); if(q.length<2){ g('[data-found]').innerHTML=''; return; }
    let r; try{ r=await api(`/employees?query=${encodeURIComponent(q)}&status=active&limit=8`); }catch(e){ return; }
    const items=r.items||r.employees||[];
    g('[data-found]').innerHTML=items.map(e=>`<a href="#" data-e="${e.id}" data-name="${esc(e.full_name)}">${esc(e.full_name)}</a> <span class="muted">${esc(e.email||'')}</span>`).join('<br>')||'<span class="muted">не найдено</span>'; }, 250); };
  g('[data-found]').onclick=(e)=>{ const x=e.target.closest('[data-e]'); if(!x) return; e.preventDefault(); pick=Number(x.dataset.e); g('[data-q]').value=x.dataset.name; g('[data-found]').innerHTML=''; g('[data-add]').disabled=false; };
  g('[data-add]').onclick=async()=>{ if(!pick) return;
    try{ render((await api(`/accounts/${a.id}/delegates`,{method:'POST',body:{employee_id:pick, rights:g('[data-r]').value, until:g('[data-u]').value, note:g('[data-n]').value}})).items); toast('Доступ открыт'); g('[data-q]').value=''; pick=null; g('[data-add]').disabled=true; }catch(e){ toastErr(e); } };
  load();
}

