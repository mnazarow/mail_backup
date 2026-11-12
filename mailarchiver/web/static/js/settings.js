/* MailArchiver — веб-интерфейс: «Настройки», «Пользователи», «Аудит», «Дела».
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Настройки (с подсказками к каждому параметру)
// ===================================================================
const ENUM_OPTS = {
  'logging.level':[['DEBUG','DEBUG — максимум подробностей'],['INFO','INFO — обычный'],['WARNING','WARNING — предупреждения'],['ERROR','ERROR — только ошибки']],
  'export.default_engine':[['auto','Авто'],['aspose','Aspose PST'],['native','Встроенный PST'],['mbox','MBOX'],['eml','EML']],
  'export.pst_format':[['unicode','Unicode (Outlook 2003+)'],['ansi','ANSI (Outlook 97–2002)']],
  'export.outlook_target':[['2016+','2016+'],['2013','2013'],['2010','2010'],['2007','2007'],['2003','2003'],['2002','2002 и старше'],['365','365']],
  'notifications.smtp_security':[['starttls','STARTTLS'],['ssl','SSL'],['none','Без шифрования']],
  'notifications.webhook_format':[['json','JSON (свой сервис)'],['slack','Slack, Mattermost, Rocket.Chat']],
  'employees.source_type':[['file','Файл на сервере'],['url','Адрес (URL)']],
  'employees.source_url_format':[['auto','Определять автоматически'],['csv','CSV'],['xlsx','Excel (XLSX)']],
  'employees.account_security':[['ssl','SSL/TLS'],['starttls','STARTTLS'],['plain','Без шифрования']],
  'replica.target':[['dir','Сетевая папка или диск'],['rsync','Другой сервер по SSH (rsync)'],['s3','S3-хранилище']],
  'mailadmin.mode':[['sasl_plain','SASL PLAIN с authzid (стандарт)'],['separator','Логин с разделителем (ящик*админ)']],
  'employees.dismissed_action':[['final_backup_disable','Последняя копия и выключить копирование'],['keep','Продолжать копировать']],
  'axigen.cli_tls':[['none','Без шифрования (только эта же машина)'],['tls','SSL-слушатель CLI']],
  'axigen.ftp_tls':[['none','Без шифрования (только эта же машина)'],['implicit','TLS сразу (SSL-слушатель, implicit)'],['explicit','AUTH TLS на обычном порту (explicit)']],
  'axigen.ftp_name_encoding':[['auto','Определять'],['utf7','UTF-7 (по умолчанию у Axigen)'],['utf8','UTF-8']],
  'axigen.new_mailbox_auth':[['axigen_ftp','Через FTP Backup (без пароля)'],['password','По IMAP с паролем']],
  'webmail.dlp_mode':[['warn','Предупреждать'],['block_external','Не отправлять наружу, внутри — предупреждать'],['off','Не проверять']],
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
  'notifications.telegram_bot_token': v=>!!v['notifications.enabled'],
  'notifications.telegram_chat_id':   v=>!!v['notifications.enabled'],
  'notifications.telegram_api_url':   v=>!!v['notifications.enabled'],
  'notifications.webhook_url':        v=>!!v['notifications.enabled'],
  'notifications.webhook_format':     v=>!!v['notifications.enabled'],
  'notifications.webhook_secret':     v=>!!v['notifications.enabled'] && !!v['notifications.webhook_url'],
  'ldap.url': v=>!!v['ldap.enabled'],
  'journal.listen': v=>!!v['journal.enabled'],
  'journal.port': v=>!!v['journal.enabled'],
  'journal.allowed_ips': v=>!!v['journal.enabled'],
  'journal.address': v=>!!v['journal.enabled'],
  'journal.folder': v=>!!v['journal.enabled'],
  'journal.unwrap_exchange': v=>!!v['journal.enabled'],
  'journal.tls_cert': v=>!!v['journal.enabled'],
  'journal.tls_key': v=>!!v['journal.enabled'] && !!v['journal.tls_cert'],
  'journal.require_tls': v=>!!v['journal.enabled'] && !!v['journal.tls_cert'],
  'journal.max_size_mb': v=>!!v['journal.enabled'],
  'ldap.starttls': v=>!!v['ldap.enabled'] && !/^ldaps:/i.test(v['ldap.url']||''),
  'ldap.verify_ssl': v=>!!v['ldap.enabled'],
  'ldap.bind_dn': v=>!!v['ldap.enabled'],
  'ldap.bind_password': v=>!!v['ldap.enabled'],
  'ldap.base_dn': v=>!!v['ldap.enabled'],
  'ldap.user_filter': v=>!!v['ldap.enabled'],
  'ldap.admin_group': v=>!!v['ldap.enabled'],
  'ldap.operator_group': v=>!!v['ldap.enabled'],
  'ldap.nested_groups': v=>!!v['ldap.enabled'],
  'ldap.employee_login': v=>!!v['ldap.enabled'],
  'ldap.timeout_s': v=>!!v['ldap.enabled'],
  'employees.account_use_master':  v=>!!v['employees.create_accounts'],
  'employees.account_use_axigen_ftp': v=>!!v['employees.create_accounts'],
  'mailadmin.host':      v=>!!v['mailadmin.enabled'],
  'mailadmin.user':      v=>!!v['mailadmin.enabled'],
  'mailadmin.password':  v=>!!v['mailadmin.enabled'],
  'mailadmin.mode':      v=>!!v['mailadmin.enabled'],
  'mailadmin.separator': v=>!!v['mailadmin.enabled'] && v['mailadmin.mode']==='separator',
  'axigen.sync_cron':     v=>!!v['axigen.enabled'] && !!v['axigen.sync_enabled'],
  'axigen.alert_queue':   v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.alert_license_days': v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.alert_quota_percent': v=>!!v['axigen.enabled'] && !!v['axigen.monitor_enabled'],
  'axigen.cli_cert_sha256': v=>!!v['axigen.enabled'] && v['axigen.cli_tls']==='tls',
  'axigen.ftp_cert_sha256': v=>!!v['axigen.enabled'] && v['axigen.ftp_tls']!=='none',
  'axigen.tls_verify':      v=>!!v['axigen.enabled'] && (v['axigen.cli_tls']==='tls' || v['axigen.ftp_tls']!=='none'),
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
  // Раздел Axigen: всё, кроме переключателя, показываем только когда он включён.
  data.sections.filter(sec=>sec.section==='axigen').forEach(sec=>sec.keys.forEach(k=>{
    const full='axigen.'+k; if(k==='enabled' || SETTING_DEPS[full]) return;
    SETTING_DEPS[full]=v=>!!v['axigen.enabled'];
  }));
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
    if(sec.section==='ldap') ldapCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('ldap.')));
    if(sec.section==='journal') journalCard(card);
    if(sec.section==='drill') drillCard(card);
    if(sec.section==='oidc') oidcCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('oidc.')));
    if(sec.section==='audit') auditForwardCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('audit.syslog')));
    if(sec.section==='axigen') axigenSettingsCard(card, ()=>Object.keys(changed).some(k=>k.startsWith('axigen.')));
  });
  applyDeps();
  // grid layout
  $$('.settings-grid',c).forEach(g=>{ g.style.display='grid'; g.style.gap='4px 24px'; g.style.gridTemplateColumns='repeat(auto-fill,minmax(300px,1fr))'; });
  State.leaveGuard=()=>Object.keys(changed).length
    ? `Несохранённые изменения настроек (${Object.keys(changed).length}) пропадут. Уйти без сохранения?` : '';
  $('#saveAll').onclick=async()=>{
    if(!Object.keys(changed).length) return toast('Нет изменений','','warn');
    try{
      const r=await api('/settings',{method:'PUT',body:{values:changed}});
      toast('Сохранено', `Изменено параметров: ${r.changed.length}`);
      if(r.notice) toast('Важно', r.notice, 'warn', 15000);
      if('axigen.enabled' in changed && State.user){ State.user.features=Object.assign({}, State.user.features, {axigen:!!changed['axigen.enabled']}); State.bulkActions=null; renderNav(); }
      if(Object.keys(changed).some(k=>k.startsWith('axigen.'))) State.bulkActions=null;
      const storageChanged=Object.keys(changed).some(k=>k.startsWith('storage.'));
      // сохранённое больше не «несохранённое»: кнопки проверки в карточках снова доступны
      Object.keys(changed).forEach(k=>delete changed[k]);
      if(storageChanged) route();
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
      <div class="small muted" style="margin-top:4px">${st.bodies?('Ищется и по тексту писем'+(st.attachments?' и вложений (PDF, Word, Excel, PowerPoint, OpenDocument, RTF)':'')+'.'):'Текст писем не индексируется — поиск по теме, адресам и вложениям.'}${st.bodies_blocked_by_encryption?' Текст не индексируется, потому что включено шифрование копии.':''}</div>
      ${st.attachments&&st.attach_messages?`<div class="small muted" style="margin-top:4px">Писем с вложениями: ${fmtNum(st.attach_messages)}; текста вложений в индексе — до ${fmtNum(st.attach_text_kb)} КБ на письмо, то есть база может вырасти до ≈ ${fmtBytes(st.attach_messages*st.attach_text_kb*1024*1.3)} (обычно в разы меньше: у большинства вложений текста немного). Сейчас база занимает ${fmtBytes(st.db_size||0)}.</div>`:''}
      ${st.ocr?`<div class="small muted" style="margin-top:4px">Распознавание сканов и картинок: ${st.ocr_tools&&st.ocr_tools.tesseract?('включено (tesseract'+(st.ocr_tools.pdftoppm?', pdftoppm':' — без pdftoppm сканы в PDF не читаются')+')'):'<b style="color:var(--warn-text)">включено, но на сервере нет программы tesseract</b> — установите пакеты tesseract-ocr и tesseract-ocr-rus'}.</div>`:''}
      ${st.attachments&&st.attach_backfill?`<div class="hint" style="margin-top:8px">У ${fmtNum(st.attach_backfill)} писем, проиндексированных раньше, текст вложений ещё не прочитан. ${st.attach_job?`Читается — задание #${st.attach_job}.`:'Его можно дочитать без перестройки индекса.'}</div>`:''}
      <div class="btn-row" style="margin-top:10px">${st.attachments&&st.attach_backfill&&!st.attach_job?'<button class="btn sm primary" data-att>📎 Дочитать вложения</button>':''}<button class="btn sm" data-re>Перестроить индекс</button></div>`
    :'<div class="hint" style="border-color:var(--warn)">SQLite на этом сервере собран без FTS5 — поиск работает только по теме и отправителю.</div>'}</div>`);
  card.appendChild(box);
  const att=box.querySelector('[data-att]');
  if(att) att.onclick=async()=>{
    try{ const r=await api('/search/attachments',{method:'POST'}); toast('Запущено', `Задание #${r.job_id}: читаются вложения ${fmtNum(st.attach_backfill)} писем.`); box.remove(); searchCard(card); }catch(e){ toastErr(e); }
  };
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
      <button class="btn sm" data-send>Отправить сейчас</button></div>
    <h4 style="margin:14px 0 8px">🔔 Каналы</h4>
    <div class="small muted">Проверочное сообщение уходит во все настроенные каналы: почту, Telegram и вебхук.</div>
    <div class="btn-row" style="margin-top:10px"><button class="btn sm" data-test>Отправить проверочное уведомление</button></div></div>`);
  card.appendChild(box);
  box.querySelector('[data-test]').onclick=async(ev)=>{
    if(unsaved()) return toast('Сначала сохраните настройки','Проверка использует сохранённые настройки.','warn');
    const b=ev.currentTarget; b.disabled=true;
    try{
      const r=await api('/notifications/test',{method:'POST'});
      const NAMES={email:'E-mail', telegram:'Telegram', webhook:'Вебхук'};
      modal('Проверка каналов уведомлений', Object.keys(r.results).length?`<div class="table-wrap"><table class="tbl"><tbody>${Object.entries(r.results).map(([k,v])=>`<tr><td>${NAMES[k]||esc(k)}</td><td>${v?'<span class="tag err">не отправлено</span> '+esc(v):'<span class="tag ok">отправлено</span>'}</td></tr>`).join('')}</tbody></table></div>`
        :'<div class="hint">Не настроен ни один канал: заполните SMTP и получателей, Telegram или адрес вебхука.</div>');
    }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
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

/** Блок «Приём журналирования»: работает ли SMTP-приёмник и сколько писем принято. */
/** Блок «Единый вход»: проверка поставщика и адрес возврата для регистрации приложения (1.22.0). */
function oidcCard(card, unsaved){
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🔑 Проверка</h4>
    <div class="small muted">Проверяется описание поставщика (issuer) и его ключи подписи. Адрес возврата регистрируется у поставщика точно таким.</div>
    <div class="btn-row" style="margin-top:8px"><button class="btn sm" data-t>Проверить поставщика</button></div><div data-r class="small" style="margin-top:8px"></div></div>`);
  box.querySelector('[data-t]').onclick=async()=>{
    if(unsaved()) return toast('Сначала сохраните настройки','Проверка идёт с сохранёнными значениями.','warn');
    const out=box.querySelector('[data-r]'); out.innerHTML='<div class="spinner"></div>';
    try{ const r=await api('/oidc/check',{method:'POST'});
      out.innerHTML=(r.ok?`<span class="tag ok">поставщик отвечает</span> ${esc(r.issuer)} · ключей подписи: ${fmtNum(r.keys)}${r.algs&&r.algs.length?` · алгоритмы: ${esc(r.algs.join(', '))}`:''}${r.pkce?'':' · <b style="color:var(--warn-text)">PKCE S256 не заявлен</b>'}`:`<span class="tag err">ошибка</span> ${esc(r.error)}`)
        +`<div style="margin-top:6px">Адрес возврата: <code style="user-select:all">${esc(r.redirect_uri)}</code></div>`;
    }catch(e){ out.textContent=''; toastErr(e); }
  };
  card.appendChild(box);
}

/** Блок «Проверка восстановления»: последний результат и запуск сейчас (1.21.0). */
async function drillCard(card){
  let st; try{ st=await api('/drill'); }catch(e){ return; }
  const l=st.last;
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🧪 Последняя проверка</h4>
    <div class="small">${l?`${l.finished_at?fmtDate(l.finished_at):'идёт'} · <span class="tag ${l.status==='success'?'ok':(l.status==='failed'?'err':'warn')}">${esc(STATUS_LBL[l.status]||l.status)}</span> ${esc(l.summary||'')} <a href="#/jobs">#${l.id}</a>`:'Ещё не проводилась.'}</div>
    <div class="btn-row" style="margin-top:8px"><button class="btn sm" data-run ${st.active_job?'disabled':''}>${st.active_job?`Идёт — задание #${st.active_job}`:'Проверить сейчас'}</button></div></div>`);
  box.querySelector('[data-run]').onclick=async()=>{ try{ const r=await api('/drill/run',{method:'POST'}); toast('Проверка запущена', `Задание #${r.job_id}.`); box.remove(); drillCard(card); }catch(e){ toastErr(e); } };
  card.appendChild(box);
}

/** Блок «Пересылка в syslog / SIEM»: состояние и пробная запись (1.21.0). */
async function auditForwardCard(card, unsaved){
  let st; try{ st=await api('/audit/forward'); }catch(e){ return; }
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">📤 Пересылка аудита</h4>
    <div class="small">${st.enabled?`<span class="tag ok">включена</span> отправлено записей с запуска службы: ${fmtNum(st.sent)}${st.queued?`, в очереди ${fmtNum(st.queued)}`:''}${st.dropped?`, <b style="color:var(--danger)">отброшено ${fmtNum(st.dropped)}</b> (очередь переполнялась)`:''}`:'<span class="tag">выключена</span> — укажите сервер журналов'}</div>
    ${st.last_error?`<div class="hint" style="margin-top:6px;border-color:var(--warn)">Последняя ошибка: ${esc(st.last_error)}</div>`:''}
    <div class="btn-row" style="margin-top:8px"><button class="btn sm" data-t>Отправить пробную запись</button></div></div>`);
  box.querySelector('[data-t]').onclick=async()=>{
    if(unsaved()) return toast('Сначала сохраните настройки','Проверка идёт с сохранёнными значениями.','warn');
    try{ const r=await api('/audit/forward/test',{method:'POST'}); r.ok?toast('Запись отправлена','Проверьте, что она пришла на сервер журналов (action=audit_syslog_test).'):toast('Не отправлено', r.error, 'err'); }catch(e){ toastErr(e); }
  };
  card.appendChild(box);
}

async function journalCard(card){
  let st; try{ st=await api('/journal/status'); }catch(e){ return; }
  card.appendChild(h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">📨 Состояние приёма</h4>
    <div class="small">${st.running?`<span class="tag ok">принимает</span> SMTP на <span class="mono">${esc(st.listen)}</span>${st.tls?' · STARTTLS':''}`:(st.enabled?'<span class="tag err">не запущен</span>':'<span class="tag">выключен</span>')}
      ${st.last_error?`<div class="hint" style="margin-top:6px;border-color:var(--danger)">${esc(st.last_error)}</div>`:''}</div>
    <div class="small muted" style="margin-top:6px">С запуска службы: принято писем ${fmtNum(st.received)}, сохранено копий ${fmtNum(st.stored)}, повторов ${fmtNum(st.duplicates)}, без ящика ${fmtNum(st.unmatched)}, отклонено подключений ${fmtNum(st.rejected)}${st.errors?`, ошибок ${fmtNum(st.errors)}`:''}. В архиве из журнала: ${fmtNum(st.in_archive)} писем.${st.last_at?' Последнее: '+fmtDate(st.last_at)+'.':''}</div></div>`));
}

/** Блок «Проверить вход через LDAP»: служебная учётная запись, поиск, группы и роль пользователя. */
function ldapCard(card, unsaved){
  const ROLE={admin:'администратор', operator:'оператор', mailbox:'сотрудник (архив своих ящиков)'};
  const box=h(`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
    <h4 style="margin:0 0 8px">🏢 Проверка</h4>
    <div class="small muted">Без логина — проверяется подключение служебной учётной записью; с логином — поиск пользователя, его группы и роль в архиве; с паролем — и сам вход.</div>
    <div class="grid2" style="margin-top:8px"><div class="form-row"><label>Логин</label><input type="text" data-u autocomplete="off" placeholder="ivanov"></div>
      <div class="form-row"><label>Пароль (необязательно)</label><input type="password" data-p autocomplete="new-password"></div></div>
    <div class="btn-row"><button class="btn sm" data-t>Проверить</button></div></div>`);
  card.appendChild(box);
  box.querySelector('[data-t]').onclick=async(ev)=>{
    if(unsaved()) return toast('Сначала сохраните настройки','Проверка использует сохранённые настройки.','warn');
    const b=ev.currentTarget; b.disabled=true;
    try{
      const r=await api('/ldap/test',{method:'POST',body:{username:box.querySelector('[data-u]').value.trim(), password:box.querySelector('[data-p]').value}});
      if(!r.ok) modal('Проверка LDAP', `<div class="hint" style="border-color:var(--danger)">${esc(r.error||r.reason||'Пользователь не найден')}</div>`);
      else if(r.bind_only) toast('Подключение работает', 'Служебная учётная запись входит в каталог.');
      else modal('Проверка LDAP', `<div class="kv"><span class="k">DN</span><span class="small">${esc(r.dn)}</span></div>
        <div class="kv"><span class="k">Имя</span><span>${esc(r.name||'—')}</span></div><div class="kv"><span class="k">Почта</span><span>${esc(r.mail||'—')}</span></div>
        <div class="kv"><span class="k">Роль в архиве</span><span>${r.role?'<span class="tag ok">'+esc(ROLE[r.role]||r.role)+'</span>':'<span class="tag err">нет — вход запрещён</span>'}</span></div>
        <details><summary class="small">Группы (${(r.groups||[]).length})</summary><div class="small mono">${(r.groups||[]).map(esc).join('<br>')}</div></details>`, {wide:true});
    }catch(e){ toastErr(e); } finally{ b.disabled=false; }
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
/** Что может роль — для подсказок на странице пользователей. */
const ROLE_HELP = {
  admin:'всё: ящики, письма, настройки, пользователи, удаление',
  operator:'следит за копированием и запускает его: копия ящиков, «Докачать потерянные», проверки входа, папок и целостности, отмена и повтор этих заданий, журнал. Писем не читает, настроек не меняет, ничего не удаляет и не выгружает',
};
async function viewUsers(c){
  const users=await api('/users');
  c.innerHTML=''; c.appendChild(h('<div class="section-title"><h2 style="margin:0">Пользователи</h2><div class="spacer"></div><button class="btn primary" id="add">+ Добавить</button></div>'));
  $('#add',c).onclick=()=>userModal();
  c.appendChild(h(`<div class="hint" style="margin-bottom:12px"><b>Администратор</b> — ${esc(ROLE_HELP.admin)}.<br>
    <b>Оператор</b> — ${esc(ROLE_HELP.operator)}.<br>
    Сотрудники входят в свой ящик по email и паролю ящика — отдельные пользователи для этого не нужны.</div>`));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>Имя</th><th>Роль</th><th>2FA</th><th>Создан</th><th>Вход</th><th></th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  users.forEach(u=>{
    const self=u.id===State.user.id;
    const roleCell = self ? `<span class="tag ${u.role==='admin'?'ok':''}" title="Свою роль поменять нельзя">${esc(ROLE_LABELS[u.role]||u.role)}</span>`
      : `<select data-role aria-label="Роль пользователя ${esc(u.username)}" title="Сменить роль (сеансы пользователя будут завершены)">
          <option value="admin" ${u.role==='admin'?'selected':''}>администратор</option>
          <option value="operator" ${u.role==='operator'?'selected':''}>оператор</option></select>`;
    const tr=h(`<tr><td><strong>${esc(u.username)}</strong>${u.auth_source==='ldap'?' <span class="tag" title="Входит доменным паролем; роль — по группе каталога LDAP">🏢 из каталога</span>':''}${u.auth_source==='oidc'?' <span class="tag" title="Входит через единый вход (OpenID Connect); роль — по группам у поставщика">🔑 единый вход</span>':''}${u.disabled?(u.disabled_by==='ldap'?' <span class="tag" title="Отключён сверкой с каталогом: нет в каталоге, отключён в нём или вне групп архива. Включится сам при входе, если права вернут.">выключен каталогом</span>':u.disabled_by==='oidc'?' <span class="tag" title="У поставщика единого входа больше нет групп архива. Включится сам при входе, если права вернут.">выключен единым входом</span>':' <span class="tag" title="Отключён администратором: вход через каталог его не включит">выключен</span>'):''}</td><td>${roleCell}</td>
      <td>${u.totp_enabled?'<span class="tag ok">🔐 вкл.</span>':'<span class="tag">нет</span>'}</td>
      <td class="small muted">${fmtDate(u.created_at)}</td><td class="small muted">${fmtDate(u.last_login)}</td>
      <td style="text-align:right;white-space:nowrap"><button class="btn sm" data-pw>Пароль</button><button class="btn sm" data-logout title="Завершить все открытые сеансы этого пользователя (его текущие cookie перестанут действовать)">Завершить сеансы</button>${u.totp_enabled && u.id!==State.user.id?'<button class="btn sm" data-reset2fa title="Отключить двухфакторный вход (например, пользователь потерял телефон)">Сбросить 2FA</button>':''}<button class="btn danger sm" data-del>✕</button></td></tr>`);
    tr.querySelector('[data-pw]').onclick=()=>{ const m=modal(`Новый пароль: ${u.username}`,`<div class="form-row"><label>Пароль</label><input id="np" type="password"></div><div class="hint">После смены пароля все открытые сеансы этого пользователя будут завершены.</div>`,{footer:`<button class="btn primary" data-ok>Сохранить</button>`}); m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api(`/users/${u.id}/password`,{method:'PUT',body:{password:m.body.querySelector('#np').value}});toast('Пароль изменён, сеансы завершены');m.close();}catch(e){toastErr(e);}}; };
    const r2=tr.querySelector('[data-reset2fa]');
    if(r2) r2.onclick=async()=>{ if(await confirmDlg('Сбросить двухфакторный вход?',`Пользователь «${u.username}» сможет войти по одному паролю и должен будет заново привязать приложение. Все его сеансы будут завершены.`)){ try{await api(`/users/${u.id}/2fa/reset`,{method:'POST'});toast('2FA сброшена');route();}catch(e){toastErr(e);} } };
    tr.querySelector('[data-logout]').onclick=async()=>{ if(await confirmDlg('Завершить все сеансы?',`Пользователю «${u.username}» придётся войти заново на всех устройствах.`)){ try{const r=await api(`/users/${u.id}/logout-all`,{method:'POST'});toast(`Завершено сеансов: ${r.closed}`);}catch(e){toastErr(e);} } };
    tr.querySelector('[data-del]').onclick=async()=>{ if(await confirmDlg('Удалить пользователя?','')){ try{await api(`/users/${u.id}`,{method:'DELETE'});toast('Удалено');route();}catch(e){toastErr(e);} } };
    const rs=tr.querySelector('[data-role]');
    if(rs) rs.onchange=async()=>{
      const role=rs.value;
      const ok=await confirmDlg('Сменить роль?', `«${u.username}» станет: ${ROLE_LABELS[role]||role}. `+
        (role==='operator'?'Оператор не читает письма, не меняет настроек и ничего не удаляет. ':'Администратору доступно всё. ')+
        'Открытые сеансы пользователя будут завершены.', {okText:'Сменить', okClass:'primary'});
      if(!ok){ rs.value=u.role; return; }
      try{ await api(`/users/${u.id}/role`,{method:'POST',body:{role}}); toast('Роль изменена'); route(); }
      catch(e){ rs.value=u.role; toastErr(e); }
    };
    tb.appendChild(tr);
  });
  c.appendChild(wrap);
  await apiTokensCard(c, users);
}

/** API-токены для скриптов и интеграций (1.20.0). */
async function apiTokensCard(c, users){
  const d=await api('/tokens');
  const STATE={active:'<span class="tag ok">действует</span>', expired:'<span class="tag warn">истёк</span>', revoked:'<span class="tag">отозван</span>'};
  const card=h(`<div class="card" style="margin-top:14px"><div style="display:flex;align-items:center;gap:8px"><h3 style="margin:0">🔑 API-токены</h3><div class="spacer" style="flex:1"></div>
    <a class="btn ghost sm" href="${docsHref('apitokens')}">📖 Как пользоваться</a><button class="btn sm primary" data-new>＋ Выпустить токен</button></div>
    <div class="small muted" style="margin:6px 0 10px">Для скриптов, мониторинга и интеграций: запрос с заголовком <span class="mono">Authorization: Bearer ma_…</span> выполняется от имени владельца с ролью токена. Пароль, двухфакторный вход, пользователи и токены с токеном недоступны.</div>
    <div class="table-wrap"><table class="tbl"><thead><tr><th>Имя</th><th>Владелец</th><th>Роль</th><th>Токен</th><th>Действует до</th><th>Последнее использование</th><th>Состояние</th><th></th></tr></thead><tbody></tbody></table></div></div>`);
  const tb=card.querySelector('tbody');
  (d.items||[]).forEach(t=>{
    const tr=h(`<tr><td><b>${esc(t.name)}</b><div class="small muted">выпустил ${esc(t.created_by)} ${fmtDate(t.created_at)}</div></td><td>${esc(t.owner)}</td><td>${esc(ROLE_LABELS[t.role]||t.role)}</td>
      <td class="mono small">${esc(t.hint)}</td><td class="small">${t.expires_at?fmtDate(t.expires_at):'бессрочно'}</td>
      <td class="small">${t.last_used_at?fmtDate(t.last_used_at)+'<div class="muted">'+esc(t.last_ip||'')+'</div>':'—'}</td><td>${STATE[t.state]||esc(t.state)}</td>
      <td>${t.state==='active'?'<button class="btn danger sm" data-revoke>Отозвать</button>':''}</td></tr>`);
    const rv=tr.querySelector('[data-revoke]');
    if(rv) rv.onclick=async()=>{ if(!await confirmDlg('Отозвать токен?', `Скрипты с токеном «${t.name}» сразу перестанут работать.`, {okText:'Отозвать'})) return;
      try{ await api('/tokens/'+t.id,{method:'DELETE'}); toast('Токен отозван'); route(); }catch(e){ toastErr(e); } };
    tb.appendChild(tr);
  });
  if(!tb.children.length) tb.innerHTML='<tr><td colspan="8" class="empty">Токенов нет</td></tr>';
  card.querySelector('[data-new]').onclick=()=>{
    const m=modal('Выпустить API-токен', `<div class="form-row"><label>Имя — для чего токен</label><input id="tk-name" maxlength="100" placeholder="Например: Zabbix, скрипт ночной выгрузки"></div>
      <div class="form-row"><label>Владелец</label><select id="tk-user">${users.filter(u=>!u.disabled).map(u=>`<option value="${u.id}" ${u.id===State.user.id?'selected':''}>${esc(u.username)} (${esc(ROLE_LABELS[u.role]||u.role)})</option>`).join('')}</select></div>
      <div class="form-row"><label>Роль токена</label><select id="tk-role"><option value="operator">Оператор — смотреть состояние и запускать копирование</option><option value="admin">Администратор — всё, что может владелец</option></select></div>
      <div class="form-row"><label>Срок действия</label><select id="tk-days"><option value="30">30 дней</option><option value="90">90 дней</option><option value="365" selected>1 год</option><option value="0">Бессрочно</option></select></div>`,
      {footer:'<button class="btn" data-c>Отмена</button><button class="btn primary" data-ok>Выпустить</button>'});
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    m.foot.querySelector('[data-ok]').onclick=async(ev)=>{
      const b=ev.currentTarget; b.disabled=true;
      try{
        const r=await api('/tokens',{method:'POST',body:{name:m.body.querySelector('#tk-name').value, role:m.body.querySelector('#tk-role').value,
          days:Number(m.body.querySelector('#tk-days').value), user_id:Number(m.body.querySelector('#tk-user').value)}});
        m.close();
        const shown=modal('Токен выпущен', `<div class="hint" style="border-color:var(--warn)">Скопируйте токен сейчас — больше он показан не будет. В базе хранится только его отпечаток.</div>
          <div class="form-row"><input id="tk-val" class="mono" readonly value="${esc(r.token)}" style="width:100%"></div>
          <div class="small muted">Пример: <span class="mono">curl -H "Authorization: Bearer ${esc(r.token.slice(0,10))}…" ${esc(location.origin)}/api/state</span></div>`,
          {footer:'<button class="btn" data-copy>Скопировать</button><button class="btn primary" data-done>Готово</button>', onClose:()=>route()});
        shown.foot.querySelector('[data-copy]').onclick=async()=>{ try{ await navigator.clipboard.writeText(r.token); toast('Скопировано'); }catch(e){ shown.body.querySelector('#tk-val').select(); } };
        shown.foot.querySelector('[data-done]').onclick=()=>shown.close();
      }catch(e){ toastErr(e); } finally{ b.disabled=false; }
    };
  };
  c.appendChild(card);
}
function userModal(){
  const m=modal('Новый пользователь',`<div class="form-row"><label>Имя</label><input id="u-name" type="text" autocomplete="off"></div>
    <div class="form-row"><label>Пароль</label><input id="u-pass" type="password" autocomplete="new-password"></div>
    <div class="form-row"><label>Роль</label><select id="u-role">
      <option value="operator">Оператор — следит за копированием и запускает его</option>
      <option value="admin">Администратор — всё</option></select></div>
    <div class="hint small" id="u-role-help"></div>`,{footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-ok>Создать</button>`});
  const help=()=>{ const t=ROLE_HELP[m.body.querySelector('#u-role').value]||'';
    m.body.querySelector('#u-role-help').textContent=t?t.charAt(0).toUpperCase()+t.slice(1)+'.':''; };
  m.body.querySelector('#u-role').onchange=help; help();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-ok]').onclick=async()=>{try{await api('/users',{method:'POST',body:{username:m.body.querySelector('#u-name').value,password:m.body.querySelector('#u-pass').value,role:m.body.querySelector('#u-role').value}});toast('Создан');m.close();route();}catch(e){toastErr(e);}};
}
// ===================================================================
//  Раздел «Дела» — юридическое удержание и выгрузка писем (1.20.0)
// ===================================================================
const CASE_ACTIONS={case_create:'дело создано', case_update:'дело изменено', case_custodians:'изменён состав ящиков',
  case_close:'дело закрыто', case_reopen:'дело открыто заново', case_delete:'дело удалено', case_export:'запущена выгрузка',
  case_export_done:'выгрузка готова', case_download:'скачана выгрузка', case_view:'просмотр писем дела'};
const CASE_EXPORT_STATUS={pending:'ждёт', running:'идёт', success:'готова', partial:'готова с ошибками', failed:'ошибка', cancelled:'отменена'};
async function viewCases(c){
  if(State.caseId){ return caseDetail(c, State.caseId); }
  const d=await api('/cases');
  c.innerHTML='';
  c.appendChild(h(`<div class="section-title"><h2 style="margin:0">⚖️ Дела</h2><div class="spacer"></div>
    <a class="btn ghost sm" href="${docsHref('cases')}">📖 Как это работает</a> <button class="btn ghost sm" id="csKey" title="Открытый ключ, которым подписывается опись выгрузок">🔏 Ключ подписи</button> <button class="btn primary" id="csNew">＋ Новое дело</button></div>`));
  c.appendChild(h(`<div class="hint" style="margin-bottom:12px">Дело собирает переписку для проверки, спора или запроса: ящики сотрудников, период и
    поисковый запрос. Пока дело открыто, архив его ящиков удерживается — письма не удаляются ни по сроку хранения, ни вручную.
    Найденные письма выгружаются в ZIP с описью и контрольными суммами SHA-256.</div>`));
  const wrap=h('<div class="card table-wrap"><table class="tbl"><thead><tr><th>№</th><th>Дело</th><th>Ящиков</th><th>Удержание</th><th>Выгрузок</th><th>Создано</th><th>Состояние</th></tr></thead><tbody></tbody></table></div>');
  const tb=wrap.querySelector('tbody');
  (d.items||[]).forEach(x=>{
    const tr=h(`<tr class="clickable" data-id="${x.id}" tabindex="0"><td>${x.id}</td><td><b>${esc(x.name)}</b>${x.query?`<div class="small muted">запрос: ${esc(x.query)}</div>`:''}</td>
      <td>${x.custodians}</td><td>${x.hold&&x.status==='open'?'<span class="tag ok">🔒 удерживается</span>':'<span class="tag">нет</span>'}</td>
      <td>${x.exports}</td><td class="small">${fmtDate(x.created_at)}<div class="muted">${esc(x.created_by)}</div></td>
      <td>${x.status==='open'?'<span class="tag warn">открыто</span>':'<span class="tag">закрыто '+esc(fmtDateShort(x.closed_at))+'</span>'}</td></tr>`);
    const open=()=>{ State.caseId=x.id; viewCases(c).catch(toastErr); };
    tr.onclick=open; tr.onkeydown=(e)=>{ if(e.key==='Enter') open(); };
    tb.appendChild(tr);
  });
  if(!tb.children.length) tb.innerHTML='<tr><td colspan="7" class="empty">Дел пока нет</td></tr>';
  c.appendChild(wrap);
  $('#csNew',c).onclick=()=>caseForm(null, (id)=>{ State.caseId=id; viewCases(c).catch(toastErr); });
  $('#csKey',c).onclick=()=>caseKeyDlg();
}

/** Выбор ящиков: поиск по названию, логину и сотруднику, флажки. Возвращает выбранные id (или null). */
async function accountPickerDlg(title, preselected){
  const accs=await api('/accounts'); State.accounts=accs;
  const chosen=new Set((preselected||[]).map(Number));
  return new Promise(resolve=>{
    let done=false;
    const m=modal(title, `<input type="search" id="apQ" placeholder="Поиск: ящик, логин, сотрудник…" style="width:100%">
      <div class="small muted" style="margin:6px 0" id="apInfo"></div>
      <div id="apList" style="max-height:52vh;overflow:auto;border:1px solid var(--border);border-radius:var(--radius-sm)"></div>`,
      {wide:true, footer:'<button class="btn ghost" data-all>Отметить найденные</button><button class="btn ghost" data-none>Снять все</button><div class="spacer" style="flex:1"></div><button class="btn" data-c>Отмена</button><button class="btn primary" data-ok>Готово</button>',
       onClose:()=>{ if(!done){ done=true; resolve(null); } }});
    const list=m.body.querySelector('#apList'), info=m.body.querySelector('#apInfo');
    let shown=[];
    const draw=()=>{
      const q=m.body.querySelector('#apQ').value.trim().toLowerCase();
      shown=accs.filter(a=>!q || [a.name,a.username,a.employee_name].some(v=>String(v||'').toLowerCase().includes(q)));
      list.innerHTML=shown.slice(0,500).map(a=>`<label class="ap-row" style="display:flex;gap:8px;align-items:center;padding:5px 10px;border-bottom:1px solid var(--border)">
        <input type="checkbox" data-id="${a.id}" ${chosen.has(a.id)?'checked':''}><span style="flex:1"><b>${esc(a.name)}</b> <span class="muted small">${esc(a.username||'')}</span></span>
        <span class="small muted">${esc(a.employee_name||'')}</span><span class="small muted">${fmtNum(a.messages)} писем</span></label>`).join('')
        || '<div class="empty">Ничего не найдено</div>';
      info.textContent=`Выбрано: ${chosen.size}. Найдено: ${shown.length}${shown.length>500?' (показаны первые 500)':''}.`;
    };
    list.onchange=(e)=>{ const id=Number(e.target.dataset.id); if(!id) return; if(e.target.checked) chosen.add(id); else chosen.delete(id); draw(); };
    m.body.querySelector('#apQ').oninput=draw;
    m.foot.querySelector('[data-all]').onclick=()=>{ shown.forEach(a=>chosen.add(a.id)); draw(); };
    m.foot.querySelector('[data-none]').onclick=()=>{ chosen.clear(); draw(); };
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    m.foot.querySelector('[data-ok]').onclick=()=>{ done=true; resolve(Array.from(chosen)); m.close(); };
    draw();
  });
}

function caseForm(cs, onSaved){
  const isNew=!cs; let picked=[];
  const m=modal(isNew?'Новое дело':'Изменить дело', `
    <div class="form-row"><label>Название</label><input id="cfName" maxlength="200" value="${esc(cs?cs.name:'')}" placeholder="Например: Проверка закупок 2026"></div>
    <div class="form-row"><label>Описание</label><textarea id="cfDesc" rows="3" maxlength="4000" placeholder="Основание, номер запроса, ответственный">${esc(cs?cs.description:'')}</textarea></div>
    <div class="form-row"><label>Поисковый запрос</label><input id="cfQuery" maxlength="1000" value="${esc(cs?cs.query:'')}" placeholder="пусто — все письма ящиков за период">
      <div class="small muted">Как в поиске писем: слова через пробел, «фраза в кавычках», поля тема:, от:, кому:, вложение:, текст:.</div></div>
    <div class="form-row" style="display:flex;gap:12px;flex-wrap:wrap"><label style="flex:1">С даты<input type="date" id="cfFrom" value="${esc(cs?cs.date_from:'')}"></label>
      <label style="flex:1">По дату<input type="date" id="cfTo" value="${esc(cs?cs.date_to:'')}"></label></div>
    <label class="check"><input type="checkbox" id="cfHold" ${!cs||cs.hold?'checked':''}> Удерживать архив ящиков, пока дело открыто</label>
    <div class="form-row" style="margin:6px 0 0 24px"><label for="cfScope">Что удерживать</label><select id="cfScope">
      <option value="all" ${!cs||cs.hold_scope!=='matching'?'selected':''}>Весь архив ящиков — сроки хранения не действуют</option>
      <option value="matching" ${cs&&cs.hold_scope==='matching'?'selected':''}>Только письма по условиям дела — остальные чистятся по сроку</option></select>
      <div class="small muted" style="margin-top:4px">В обоих случаях архив ящика нельзя удалить или пересоздать «с нуля», а копирование скачивает все письма.</div></div>
    ${isNew?`<div class="form-row" style="margin-top:10px"><label>Ящики</label><div style="display:flex;gap:8px;align-items:center"><button class="btn sm" type="button" id="cfPick">Выбрать ящики…</button><span class="small muted" id="cfPicked">не выбраны</span></div></div>`:''}`,
    {footer:'<button class="btn" data-c>Отмена</button><button class="btn primary" data-ok>'+(isNew?'Создать дело':'Сохранить')+'</button>'});
  m.foot.querySelector('[data-c]').onclick=()=>m.close();
  const pick=m.body.querySelector('#cfPick');
  if(pick) pick.onclick=async()=>{ const ids=await accountPickerDlg('Ящики дела', picked); if(ids){ picked=ids; m.body.querySelector('#cfPicked').textContent=`выбрано: ${ids.length}`; } };
  m.foot.querySelector('[data-ok]').onclick=async(ev)=>{
    const b=ev.currentTarget; b.disabled=true;
    const body={name:m.body.querySelector('#cfName').value.trim(), description:m.body.querySelector('#cfDesc').value,
      query:m.body.querySelector('#cfQuery').value.trim(), date_from:m.body.querySelector('#cfFrom').value, date_to:m.body.querySelector('#cfTo').value,
      hold:m.body.querySelector('#cfHold').checked, hold_scope:m.body.querySelector('#cfScope').value};
    try{
      if(isNew){ body.account_ids=picked; const r=await api('/cases',{method:'POST',body}); toast('Дело создано'); m.close(); onSaved&&onSaved(r.id); }
      else{ await api('/cases/'+cs.id,{method:'PUT',body}); toast('Сохранено'); m.close(); onSaved&&onSaved(cs.id); }
    }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
}

/** Выгрузка дела: формат (EML — письма без изменений, PST — для Outlook) и пароль архива (1.21.0). */
function caseExportDlg(cs, id, reload){
  const m=modal('📦 Выгрузить письма дела', `<p class="small" style="margin-top:0">${fmtNum(cs.count)} писем (${fmtBytes(cs.size)}). В архив попадут опись с SHA-256 каждого письма, подпись описи ключом сервера и отчёт. Выгрузка идёт заданием очереди; скачивание файла попадёт в аудит.</p>
    <div class="form-row"><label>Формат</label>
      <label class="check"><input type="radio" name="cxf" value="eml" checked> EML — письма ровно в том виде, в каком хранятся в архиве (для экспертизы)</label>
      <label class="check"><input type="radio" name="cxf" value="pst"> PST — один файл для Outlook (письма преобразуются; суммы — по исходным письмам)</label></div>
    <div class="form-row"><label class="check"><input type="checkbox" id="cxPw"> Зашифровать архив паролем (AES-256)</label>
      <div id="cxPwBox" hidden><div class="grid2"><input type="password" id="cxP1" autocomplete="new-password" placeholder="Пароль, не короче 8 знаков"><input type="password" id="cxP2" autocomplete="new-password" placeholder="Ещё раз"></div>
      <div class="small muted" style="margin-top:4px">Пароль нигде не сохраняется — передайте его получателю отдельно. Архив открывают 7-Zip, WinRAR, WinZip, Keka (встроенные средства Windows и macOS — нет).</div></div></div>
    <div class="btn-row"><button class="btn primary" id="cxGo">Выгрузить</button><button class="btn ghost" id="cxCancel">Отмена</button></div>`);
  const g=s=>m.body.querySelector(s);
  g('#cxPw').onchange=()=>{ g('#cxPwBox').hidden=!g('#cxPw').checked; if(g('#cxPw').checked) g('#cxP1').focus(); };
  g('#cxCancel').onclick=()=>m.close();
  g('#cxGo').onclick=async()=>{
    const body={format:(m.body.querySelector('input[name="cxf"]:checked')||{}).value||'eml'};
    if(g('#cxPw').checked){
      const p1=g('#cxP1').value, p2=g('#cxP2').value;
      if(p1.length<8) return toast('Пароль — не короче 8 знаков','','warn');
      if(p1!==p2) return toast('Пароли не совпадают','','warn');
      body.password=p1;
    }
    const b=g('#cxGo'); b.disabled=true;
    try{ const r=await api(`/cases/${id}/export`,{method:'POST',body}); m.close(); toast('Выгрузка запущена', `Задание #${r.job_id}. Когда оно закончится, нажмите «↻ Обновить».`); reload(); }catch(e){ toastErr(e); } finally{ b.disabled=false; }
  };
}

/** Открытый ключ подписи выгрузок дел: отпечаток сообщают получателям (1.21.0). */
async function caseKeyDlg(){
  let k; try{ k=await api('/cases/signing-key'); }catch(e){ return toastErr(e); }
  const m=modal('🔏 Ключ подписи выгрузок', `<p class="small" style="margin-top:0">Опись каждой выгрузки дела (manifest.csv) подписывается этим ключом (Ed25519). Сообщите отпечаток получателям выгрузок заранее, по другому каналу: совпадение отпечатка в отчёте выгрузки с этим доказывает, что опись сформировал ваш архив и её не меняли.</p>
    <div class="kv"><span class="k">Отпечаток</span><span><code>${esc(k.fingerprint)}</code></span></div>
    <pre class="small" style="white-space:pre-wrap;user-select:all">${esc(k.pem)}</pre>
    <p class="small muted">Проверка (OpenSSL 3): <code>openssl pkeyutl -verify -pubin -inkey signing_key.pem -rawin -in manifest.csv -sigfile manifest.sig</code></p>
    <div class="btn-row"><button class="btn" id="ckDl">⬇ Скачать signing_key.pem</button></div>`);
  m.body.querySelector('#ckDl').onclick=()=>{ const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([k.pem],{type:'application/x-pem-file'})); a.download='signing_key.pem'; a.click(); setTimeout(()=>URL.revokeObjectURL(a.href), 5000); };
}

async function caseDetail(c, id){
  let cs; try{ cs=await api('/cases/'+id); }catch(e){ State.caseId=null; if(e.status===404) return viewCases(c); throw e; }
  const open=cs.status==='open';
  c.innerHTML='';
  const head=h(`<div class="section-title"><button class="btn ghost sm" id="cdBack">← Все дела</button>
    <h2 style="margin:0 0 0 8px">⚖️ ${esc(cs.name)}</h2> ${open?'<span class="tag warn">открыто</span>':'<span class="tag">закрыто</span>'}
    <div class="spacer"></div>
    ${open?'<button class="btn sm" id="cdEdit">✏️ Изменить</button> <button class="btn sm" id="cdAdd">＋ Ящики</button> <button class="btn sm primary" id="cdExport">📦 Выгрузить</button> <button class="btn sm" id="cdClose">Закрыть дело</button>'
      :'<button class="btn sm" id="cdReopen">Открыть заново</button> <button class="btn sm danger" id="cdDelete">Удалить дело</button>'}</div>`);
  c.appendChild(head);
  const info=h(`<div class="card"><div class="kv"><span class="k">Описание</span><span>${esc(cs.description||'—')}</span></div>
    <div class="kv"><span class="k">Запрос</span><span>${cs.query?'<code>'+esc(cs.query)+'</code>':'все письма ящиков'}</span></div>
    <div class="kv"><span class="k">Период</span><span>${cs.date_from?esc(cs.date_from):'с начала'} — ${cs.date_to?esc(cs.date_to):'по сегодня'}</span></div>
    <div class="kv"><span class="k">Удержание архива</span><span>${cs.hold?(open?(cs.hold_scope==='matching'?'<span class="tag ok">🔒 действует</span> письма по условиям дела не удаляются, остальные чистятся по сроку хранения':'<span class="tag ok">🔒 действует</span> письма ящиков не удаляются'):'снято вместе с закрытием дела'):'не включено'}</span></div>
    <div class="kv"><span class="k">Писем по условиям</span><span><b>${fmtNum(cs.count)}</b> (${fmtBytes(cs.size)}) в ${cs.custodians} ящ.</span></div>
    <div class="kv"><span class="k">Создано</span><span>${fmtDate(cs.created_at)} · ${esc(cs.created_by)}${cs.closed_at?` · закрыто ${fmtDate(cs.closed_at)} · ${esc(cs.closed_by)}`:''}</span></div>
    ${cs.index_pending?`<div class="hint" style="margin-top:8px;border-color:var(--warn)">Индекс поиска ещё не охватывает ${fmtNum(cs.index_pending)} новейших писем архива — подходящие под запрос среди них появятся в деле после индексации.</div>`:''}
    ${cs.query && !cs.fts?'<div class="hint" style="margin-top:8px;border-color:var(--warn)">SQLite без FTS5: запрос ищется только по теме и отправителю.</div>':''}</div>`);
  c.appendChild(info);
  // Ящики
  const accCard=h(`<div class="card" style="margin-top:12px"><h3 style="margin-top:0">Ящики дела (${cs.accounts.length})</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Ящик</th><th>Сотрудник</th><th>Писем</th><th>Удержание</th>${open?'<th></th>':''}</tr></thead><tbody></tbody></table></div></div>`);
  const atb=accCard.querySelector('tbody');
  cs.accounts.forEach(a=>{
    const tr=h(`<tr><td><b>${esc(a.name)}</b> <span class="small muted">${esc(a.username)}</span>${a.dismissed?' <span class="tag">уволен</span>':''}</td><td>${esc(a.employee||'—')}</td><td>${fmtNum(a.messages)}</td>
      <td>${a.hold_reason==='case'?'<span class="tag ok">🔒 по делу</span>':esc(a.hold)}</td>${open?'<td><button class="btn ghost sm" data-rm="'+a.id+'" title="Убрать из дела">✕</button></td>':''}</tr>`);
    atb.appendChild(tr);
  });
  if(!atb.children.length) atb.innerHTML=`<tr><td colspan="5" class="empty">Ящиков нет — добавьте их кнопкой «＋ Ящики»</td></tr>`;
  atb.onclick=async(e)=>{
    const b=e.target.closest('[data-rm]'); if(!b) return;
    if(!await confirmDlg('Убрать ящик из дела?', 'Если ящик не держит другое дело, ему вернётся удержание, которое было до дела.', {okText:'Убрать'})) return;
    try{ await api(`/cases/${id}/custodians`,{method:'POST',body:{remove:[Number(b.dataset.rm)]}}); caseDetail(c,id).catch(toastErr); }catch(err){ toastErr(err); }
  };
  c.appendChild(accCard);
  // Выгрузки
  const expCard=h(`<div class="card" style="margin-top:12px"><div style="display:flex;align-items:center"><h3 style="margin:0">Выгрузки</h3><div class="spacer" style="flex:1"></div><button class="btn ghost sm" id="cdRefresh">↻ Обновить</button></div>
    <div class="table-wrap" style="margin-top:8px"><table class="tbl case-exp"><thead><tr><th>Когда</th><th>Кто</th><th>Состояние</th><th>Писем</th><th>Размер</th><th></th></tr></thead><tbody></tbody></table></div></div>`);
  const etb=expCard.querySelector('tbody');
  cs.export_list.forEach(x=>{
    etb.appendChild(h(`<tr><td class="small">${fmtDate(x.created_at)}</td><td class="small">${esc(x.created_by||'')}</td>
      <td><span class="tag ${x.status==='success'?'ok':(x.status==='partial'?'warn':(x.status==='failed'?'err':''))}">${esc(CASE_EXPORT_STATUS[x.status]||x.status)}</span>${x.job_id?` <a class="small" href="#/jobs">#${x.job_id}</a>`:''}${x.error?`<div class="small muted">${esc(x.error)}</div>`:''}</td>
      <td>${x.count?fmtNum(x.count):'—'}${x.format==='pst'?' <span class="tag">PST</span>':''}${x.protected?' <span class="tag" title="Архив зашифрован паролем (AES-256)">🔐 пароль</span>':''}</td><td>${x.size?fmtBytes(x.size):'—'}</td>
      <td style="text-align:right">${x.ready?`<a class="btn sm" href="/api/cases/${id}/exports/${x.id}/download" download>⬇ Скачать</a>`:''}</td></tr>`));
    if(x.sha256) etb.appendChild(h(`<tr class="case-sha"><td colspan="6" class="small muted">SHA-256 архива: <code style="word-break:break-all">${esc(x.sha256)}</code></td></tr>`));
  });
  if(!etb.children.length) etb.innerHTML='<tr><td colspan="7" class="empty">Выгрузок ещё не было</td></tr>';
  c.appendChild(expCard);
  // Письма
  const msgCard=h(`<div class="card" style="margin-top:12px"><h3 style="margin-top:0">Письма дела</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Дата</th><th>Ящик</th><th>От</th><th>Тема</th></tr></thead><tbody></tbody></table></div>
    <div style="margin-top:10px;text-align:center"><button class="btn" hidden data-more>Показать ещё</button></div></div>`);
  const mtb=msgCard.querySelector('tbody'), more=msgCard.querySelector('[data-more]');
  let offset=0;
  const snip=(s)=>esc(s||'').replace(/\u0002/g,'<mark>').replace(/\u0003/g,'</mark>');
  const loadMsgs=async()=>{
    const r=await api(`/cases/${id}/messages?offset=${offset}`);
    r.results.forEach(x=>{
      const tr=h(`<tr class="clickable"><td class="small">${fmtDate(x.date)}</td><td class="small">${esc(x.account_name)}<div class="muted">${esc(x.folder)}</div></td>
        <td class="small">${esc(x.from)}</td><td>${x.has_attach?'📎 ':''}${esc(x.subject||'(без темы)')}${x.gone?' <span class="tag" title="Письма уже нет на почтовом сервере — копия в архиве">нет на сервере</span>':''}${x.snippet?`<div class="small muted">${snip(x.snippet)}</div>`:''}</td></tr>`);
      tr.onclick=()=>openMailMessage(x.account_id, x.id, x.folder);
      mtb.appendChild(tr);
    });
    offset+=r.results.length; more.hidden=!r.more;
    if(!mtb.children.length) mtb.innerHTML='<tr><td colspan="4" class="empty">Писем по условиям дела нет</td></tr>';
  };
  more.onclick=()=>loadMsgs().catch(toastErr);
  c.appendChild(msgCard);
  // История
  const hist=h(`<div class="card" style="margin-top:12px"><h3 style="margin-top:0">История дела</h3><div class="table-wrap"><table class="tbl"><tbody>${(cs.history||[]).map(r=>`<tr><td class="small muted">${fmtDate(r.ts)}</td><td class="small">${esc(r.user)}</td><td class="small">${esc(CASE_ACTIONS[r.action]||r.action)}</td><td class="small">${esc(r.detail.replace(/^case=\d+\s*/,''))}</td></tr>`).join('')||'<tr><td class="empty">Записей нет</td></tr>'}</tbody></table></div></div>`);
  c.appendChild(hist);
  const reload=()=>caseDetail(c,id).catch(toastErr);
  $('#cdBack',c).onclick=()=>{ State.caseId=null; viewCases(c).catch(toastErr); };
  $('#cdRefresh',c).onclick=reload;
  const on=(sel,fn)=>{ const el=$(sel,c); if(el) el.onclick=fn; };
  on('#cdEdit', ()=>caseForm(cs, reload));
  on('#cdAdd', async()=>{ const ids=await accountPickerDlg('Добавить ящики в дело', []); if(!ids||!ids.length) return;
    try{ const r=await api(`/cases/${id}/custodians`,{method:'POST',body:{add:ids}}); toast('Готово', `Добавлено ящиков: ${r.added}`); reload(); }catch(e){ toastErr(e); } });
  on('#cdExport', ()=>caseExportDlg(cs, id, reload));
  on('#cdClose', async()=>{
    if(!await confirmDlg('Закрыть дело?', 'Удержание архива по этому делу будет снято: ящикам вернётся удержание, которое было до дела (если их не держит другое открытое дело). Выгрузки и история сохранятся.', {okText:'Закрыть дело'})) return;
    try{ await api(`/cases/${id}/close`,{method:'POST'}); toast('Дело закрыто'); reload(); }catch(e){ toastErr(e); } });
  on('#cdReopen', async()=>{ try{ await api(`/cases/${id}/reopen`,{method:'POST'}); toast('Дело открыто'); reload(); }catch(e){ toastErr(e); } });
  on('#cdDelete', async()=>{
    if(!await confirmDlg('Удалить дело?', 'Дело, его выгрузки (файлы ZIP) и состав ящиков будут удалены. Записи аудита останутся. Письма в архиве не удаляются.', {okText:'Удалить'})) return;
    try{ await api(`/cases/${id}`,{method:'DELETE'}); toast('Дело удалено'); State.caseId=null; viewCases(c).catch(toastErr); }catch(e){ toastErr(e); } });
  await loadMsgs();
}

