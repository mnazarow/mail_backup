/* MailArchiver — веб-интерфейс: Групповые действия с ящиками.
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Групповые действия с ящиками
// ===================================================================
//: Состояние раздела «Групповые действия» (живёт между перерисовками).
const Bulk = { source:'selected', filter:'', query:'', list:'', action:null, params:{}, showTargets:false };

/** Описание действий с сервера (один раз за сеанс). */
async function bulkMeta(key){
  if(!State.bulkActions) State.bulkActions=await api('/accounts/bulk/actions');
  if(!key) return State.bulkActions;
  return State.bulkActions.actions.find(a=>a.key===key) || {key, label:key, params:[], desc:'', danger:0};
}

/** Разбор списка адресов: ящик ищется по логину, затем по названию. */
function bulkParseList(text, accs){
  const byLogin=new Map(), byName=new Map();
  accs.forEach(a=>{ byLogin.set(String(a.username||'').toLowerCase(), a); byName.set(String(a.name||'').toLowerCase(), a); });
  const found=[], missing=[], seen=new Set();
  const tokens=[];
  String(text||'').split(/[\n,;\t]+/).forEach(raw=>{
    const t=raw.trim(); if(!t) return;
    const inAngle=t.match(/<([^<>\s]+@[^<>\s]+)>/g);
    if(inAngle) inAngle.forEach(x=>tokens.push(x.slice(1,-1)));          // «Иванов Иван <ivanov@…>»
    else if(t.includes('@') && /\s/.test(t)) t.split(/\s+/).filter(x=>x.includes('@')).forEach(x=>tokens.push(x));  // «Иванов Иван ivanov@…», несколько адресов
    else tokens.push(t);                                                  // адрес или название ящика
  });
  tokens.map(s=>s.trim()).filter(Boolean).forEach(item=>{
    const key=item.toLowerCase();
    const a=byLogin.get(key)||byName.get(key);
    if(!a){ if(!missing.includes(item)) missing.push(item); return; }
    if(!seen.has(a.id)){ seen.add(a.id); found.push(a); }
  });
  return {found, missing};
}

/** Ящики, над которыми будет выполнено действие, по выбранному источнику. */
function bulkTargets(accs){
  switch(Bulk.source){
    case 'all': return accs.slice();
    case 'enabled': return accs.filter(a=>a.enabled);
    case 'filter': return accs.filter(a=>accountMatchesBy(a, Bulk.filter, Bulk.query));
    case 'list': return bulkParseList(Bulk.list, accs).found;
    default: return accs.filter(a=>Acc.sel.has(a.id));
  }
}

async function viewBulk(c, accs){
  let meta;
  State.bulkActions=null;     // список ящиков-образцов и умолчания могли измениться
  try{ meta=await bulkMeta(); }catch(e){ c.appendChild(h(`<div class="card"><div class="empty">${esc(e.message)}</div></div>`)); return; }
  const opts=accFilterOptions(accs);
  const targetsCard=h(`<div class="card bulk-card"><h3>1. Какие ящики</h3>
    <div class="bulk-sources">
      <label class="radio"><input type="radio" name="bsrc" value="selected"> Отмеченные в списке — <b>${fmtNum(Acc.sel.size)}</b>
        ${Acc.sel.size?'':'<span class="muted small">(отметьте ящики флажками на вкладке «Список ящиков»)</span>'}</label>
      <label class="radio"><input type="radio" name="bsrc" value="enabled"> Все включённые — <b>${fmtNum(accs.filter(a=>a.enabled).length)}</b></label>
      <label class="radio"><input type="radio" name="bsrc" value="all"> Все ящики — <b>${fmtNum(accs.length)}</b></label>
      <label class="radio"><input type="radio" name="bsrc" value="filter"> По отбору:</label>
      <div class="bulk-sub" data-sub="filter">
        <select id="bF">${opts.map(([v,t])=>`<option value="${v}" ${v===Bulk.filter?'selected':''}>${esc(t)}</option>`).join('')}</select>
        <input type="text" id="bQ" placeholder="и поиск по названию, логину, серверу…" value="${esc(Bulk.query)}">
      </div>
      <label class="radio"><input type="radio" name="bsrc" value="list"> По списку адресов (вставьте из Excel или письма):</label>
      <div class="bulk-sub" data-sub="list">
        <textarea id="bL" rows="4" placeholder="ivanov@company.ru&#10;petrova@company.ru">${esc(Bulk.list)}</textarea>
        <div class="small muted" id="bLinfo"></div>
      </div>
    </div>
    <div class="bulk-total"><span id="bTotal"></span> <a href="#" id="bShow" class="small"></a>
      <span class="spacer"></span>
      <button class="btn sm" id="bExport" title="Выгрузить список этих ящиков в Excel">📊 Список в Excel</button></div>
    <div id="bList" class="bulk-list" style="display:none"></div></div>`);
  c.appendChild(targetsCard);

  const actionsCard=h(`<div class="card bulk-card"><h3>2. Что сделать</h3><div class="bulk-groups"></div></div>`);
  const groupsBox=actionsCard.querySelector('.bulk-groups');
  meta.groups.forEach(g=>{
    const items=meta.actions.filter(a=>a.group===g.key);
    if(!items.length && g.key!=='manage') return;
    const box=h(`<div class="bulk-group"><div class="bg-title">${esc(g.label)}</div><div class="bulk-tiles"></div></div>`);
    const tiles=box.querySelector('.bulk-tiles');
    items.forEach(a=>{
      const t=h(`<button class="bulk-tile ${a.danger>=2?'danger':(a.danger===1?'warn':'')}" data-act="${esc(a.key)}" title="${esc(a.desc)}">
        <span class="ic">${a.icon}</span><span>${esc(a.label)}</span></button>`);
      tiles.appendChild(t);
    });
    if(g.key==='manage'){
      if(isAdmin()){
        tiles.appendChild(h(`<button class="bulk-tile" data-client="create" title="Завести много ящиков сразу из списка адресов"><span class="ic">➕</span><span>Добавить ящики списком</span></button>`));
        tiles.appendChild(h(`<button class="bulk-tile" data-client="passwords" title="Массово проставить пароли из файла «адрес — пароль»"><span class="ic">🔑</span><span>Загрузить пароли из файла</span></button>`));
      }
      tiles.appendChild(h(`<button class="bulk-tile" data-client="excel" title="Выгрузить список ящиков из пункта 1 в Excel"><span class="ic">📊</span><span>Список ящиков в Excel</span></button>`));
    }
    groupsBox.appendChild(box);
  });
  if(isOperator()) actionsCard.appendChild(h('<div class="muted small" style="margin-top:8px">Оператору доступны копирование и проверки. Остальные групповые действия — у администратора.</div>'));
  c.appendChild(actionsCard);

  const paramsCard=h(`<div class="card bulk-card" id="bParams" style="display:none"></div>`);
  c.appendChild(paramsCard);
  const histCard=h(`<div class="card bulk-card"><div class="section-title" style="margin-bottom:8px"><h3 style="margin:0">История групповых действий</h3><div class="spacer"></div><button class="btn sm ghost" id="bHistR">↻ Обновить</button></div><div id="bHist"><div class="empty"><span class="spinner"></span></div></div></div>`);
  c.appendChild(histCard);

  const radios=$$('input[name=bsrc]', targetsCard);
  const updateTargets=()=>{
    radios.forEach(r=>{ r.checked=(r.value===Bulk.source); });
    $$('[data-sub]', targetsCard).forEach(el=>el.classList.toggle('on', el.dataset.sub===Bulk.source));
    const list=bulkTargets(accs);
    $('#bTotal',targetsCard).innerHTML=`Будет обработано: <b>${fmtNum(list.length)}</b> ${plural(list.length,'ящик','ящика','ящиков')}`;
    if(Bulk.source==='list'){
      const r=bulkParseList(Bulk.list, accs);
      $('#bLinfo',targetsCard).innerHTML = Bulk.list.trim()
        ? `Найдено: <b>${r.found.length}</b>` + (r.missing.length?` · не найдено: <b style="color:var(--danger)">${r.missing.length}</b> — ${esc(r.missing.slice(0,12).join(', '))}${r.missing.length>12?' …':''}`:'')
        : 'По одному адресу (логину) или названию ящика в строке; подойдут и через запятую.';
    }
    const show=$('#bShow',targetsCard);
    show.textContent = list.length ? (Bulk.showTargets?'скрыть список':'показать список') : '';
    const box=$('#bList',targetsCard);
    box.style.display = Bulk.showTargets && list.length ? '' : 'none';
    if(Bulk.showTargets && list.length){
      const max=300;
      box.innerHTML=`<table class="tbl"><tbody>${list.slice(0,max).map(a=>`<tr><td>${esc(a.name)}</td><td class="small muted">${esc(a.username)}</td><td>${a.enabled?'<span class="tag ok">включён</span>':'<span class="tag">выключен</span>'}</td></tr>`).join('')}</tbody></table>`
        + (list.length>max?`<div class="muted small" style="padding:6px 10px">… и ещё ${fmtNum(list.length-max)}</div>`:'');
    }
    const go=$('#bGo', paramsCard); if(go) go.disabled=!list.length;
  };
  radios.forEach(r=>r.onchange=()=>{ Bulk.source=r.value; updateTargets(); });
  $('#bF',targetsCard).onchange=e=>{ Bulk.filter=e.target.value; Bulk.source='filter'; updateTargets(); };
  let qT=null;
  $('#bQ',targetsCard).oninput=e=>{ Bulk.query=e.target.value; Bulk.source='filter'; clearTimeout(qT); qT=setTimeout(updateTargets,200); };
  let lT=null;
  $('#bL',targetsCard).oninput=e=>{ Bulk.list=e.target.value; Bulk.source='list'; clearTimeout(lT); lT=setTimeout(updateTargets,250); };
  $('#bShow',targetsCard).onclick=()=>{ Bulk.showTargets=!Bulk.showTargets; updateTargets(); };
  $('#bExport',targetsCard).onclick=()=>{ const ids=bulkTargets(accs).map(a=>a.id); if(!ids.length) return toast('Не выбрано ни одного ящика','','warn'); exportAccountList(ids); };

  const selectAction=(key)=>{
    Bulk.action=key;
    $$('.bulk-tile', actionsCard).forEach(t=>t.classList.toggle('active', t.dataset.act===key));
    const a=meta.actions.find(x=>x.key===key);
    if(!a){ paramsCard.style.display='none'; return; }
    const saved=(Bulk.params[key]||{});
    const tabParams=a.params.filter(p=>!p.on_warning);
    paramsCard.style.display='';
    paramsCard.innerHTML=`<h3>3. ${a.icon} ${esc(a.label)}</h3>
      <p class="muted" style="margin-top:-4px">${esc(a.desc)}</p>
      ${a.note?`<div class="hint" style="margin-bottom:12px">💡 ${esc(a.note)}</div>`:''}
      ${tabParams.length?`<div class="bulk-form" data-form>${bulkFormHtml(tabParams, saved)}</div>`:''}
      <div class="btn-row" style="margin-top:6px">
        <button class="btn ${a.danger>=2?'danger':'primary'}" id="bGo">Проверить и запустить…</button>
        <span class="muted small" style="align-self:center">Сначала покажем, что будет сделано с каждым ящиком, — без изменений.</span></div>`;
    const form=paramsCard.querySelector('[data-form]');
    if(form) bulkFormWire(form, tabParams);
    $('#bGo',paramsCard).onclick=async()=>{
      const values=form?bulkFormRead(form, tabParams):{};
      Bulk.params[key]=values;
      const ids=bulkTargets(accs).map(x=>x.id);
      if(!ids.length) return toast('Не выбрано ни одного ящика','Укажите ящики в пункте 1','warn');
      const done=await bulkDialog(key, ids, values, {onDone:()=>{ Bulk.action=null; }});
      if(done) loadBulkHistory($('#bHist',histCard));
    };
    updateTargets();
    paramsCard.scrollIntoView({behavior:'smooth', block:'nearest'});
  };
  $$('.bulk-tile', actionsCard).forEach(t=>t.onclick=()=>{
    if(t.dataset.client==='passwords') return passwordImportModal();
    if(t.dataset.client==='create') return bulkCreateModal();
    if(t.dataset.client==='excel'){ const ids=bulkTargets(accs).map(a=>a.id); return ids.length?exportAccountList(ids):toast('Не выбрано ни одного ящика','Укажите ящики в пункте 1','warn'); }
    selectAction(t.dataset.act);
  });
  $('#bHistR',histCard).onclick=()=>loadBulkHistory($('#bHist',histCard));
  updateTargets();
  if(Bulk.action) selectAction(Bulk.action);
  loadBulkHistory($('#bHist',histCard));
}

// ---------- Форма параметров действия (по описанию с сервера) ----------
function bulkFormHtml(params, values){
  return params.map(p=>{
    const v = (values && p.key in values) ? values[p.key] : p.default;
    const help = p.help?`<div class="hint">${esc(p.help)}</div>`:'';
    const warnOnly = p.on_warning ? ' data-onwarn style="display:none"' : '';
    const attrs=`data-p="${esc(p.key)}"`;
    let input;
    switch(p.kind){
      case 'bool':
        return `<div class="form-row check" data-row="${esc(p.key)}"${warnOnly}><span class="switch"><input type="checkbox" ${attrs} ${v?'checked':''}><span class="track"></span></span><label>${esc(p.label)}</label>${help}</div>`;
      case 'select':
        input=`<select ${attrs}>${p.options.map(o=>`<option value="${esc(o.value)}" ${String(o.value)===String(v)?'selected':''}>${esc(o.label)}</option>`).join('')}</select>`; break;
      case 'int':
        input=`<input type="number" ${attrs} value="${v==null?'':esc(v)}" ${p.min!=null?`min="${p.min}"`:''} ${p.max!=null?`max="${p.max}"`:''} placeholder="${esc(p.placeholder||'')}">`; break;
      case 'textarea':
        input=`<textarea ${attrs} rows="3" placeholder="${esc(p.placeholder||'')}">${esc(v||'')}</textarea>`; break;
      case 'date': input=`<input type="date" ${attrs} value="${esc(v||'')}">`; break;
      case 'time': input=`<input type="time" ${attrs} value="${esc(v||'')}">`; break;
      default: input=`<input type="text" ${attrs} value="${esc(v==null?'':v)}" placeholder="${esc(p.placeholder||'')}">`;
    }
    return `<div class="form-row" data-row="${esc(p.key)}"${warnOnly}><label>${esc(p.label)}</label>${input}${help}</div>`;
  }).join('');
}
function bulkFormRead(root, params){
  const out={};
  params.forEach(p=>{
    const el=root.querySelector(`[data-p="${p.key}"]`); if(!el) return;
    if(p.kind==='bool') out[p.key]=el.checked;
    else if(p.kind==='int') out[p.key]=el.value===''?null:parseInt(el.value,10);
    else out[p.key]=el.value;
  });
  return out;
}
/** Показывать поля, зависящие от других (show_if). */
function bulkFormWire(root, params){
  const apply=()=>{
    const vals=bulkFormRead(root, params);
    params.forEach(p=>{
      if(!p.show_if) return;
      const row=root.querySelector(`[data-row="${p.key}"]`); if(!row) return;
      const ok=Object.entries(p.show_if).every(([k,list])=>list.some(x=>String(x)===String(vals[k])));
      row.style.display=ok?'':'none';
    });
  };
  root.addEventListener('change', apply);
  apply();
}

// ---------- Проверка, подтверждение и запуск ----------
const BULK_STATUS = {ok:['ok','будет сделано','сделано'], skip:['','пропуск','пропуск'], fail:['err','ошибка','ошибка']};

/** Итог по ящикам: плитки, причины пропусков, таблица с отбором. */
function bulkResultHtml(r, preview){
  const c=r.counts||{};
  const reasons=Object.entries(r.reasons||{}).sort((a,b)=>b[1]-a[1]);
  const disk=(r.extra&&r.extra.disk)||null;
  return `<div class="an-kpis" style="margin:4px 0 12px">
      <div class="kpi accent"><div class="k-label">${preview?'✅ Будет обработано':'✅ Выполнено'}</div><div class="k-value">${fmtNum(c.ok||0)}</div><div class="k-sub">из ${fmtNum(r.total)}</div></div>
      <div class="kpi"><div class="k-label">⏭️ Пропущено</div><div class="k-value">${fmtNum(c.skip||0)}</div><div class="k-sub">${(c.skip||0)?'причины ниже':'нет'}</div></div>
      <div class="kpi"><div class="k-label">⚠️ Ошибок</div><div class="k-value">${fmtNum(c.fail||0)}</div><div class="k-sub">${(c.fail||0)?'см. таблицу':'нет'}</div></div>
      ${disk?`<div class="kpi"><div class="k-label">💽 Место</div><div class="k-value" style="font-size:1.05rem">${esc(disk.need_h)}</div><div class="k-sub">нужно · свободно ${esc(disk.free_h)}</div></div>`:''}
      ${(r.jobs&&r.jobs.length)?`<div class="kpi"><div class="k-label">⚙️ Заданий</div><div class="k-value">${fmtNum(r.jobs.length)}</div><div class="k-sub">в очереди</div></div>`:''}
    </div>
    ${(r.warnings||[]).map(w=>`<div class="hint warnbox">⚠️ ${esc(w)}</div>`).join('')}
    ${reasons.length?`<div class="bulk-reasons"><b>Почему пропущены:</b> ${reasons.map(([t,n])=>`<span class="tag">${esc(t)} — ${fmtNum(n)}</span>`).join(' ')}</div>`:''}
    <div class="bulk-chips" data-chips>
      <button class="chip active" data-f="">Все (${fmtNum(r.results.length)})</button>
      <button class="chip" data-f="ok">${preview?'Будут обработаны':'Выполнены'} (${fmtNum(c.ok||0)})</button>
      <button class="chip" data-f="skip">Пропущены (${fmtNum(c.skip||0)})</button>
      ${(c.fail||0)?`<button class="chip" data-f="fail">Ошибки (${fmtNum(c.fail)})</button>`:''}
    </div>
    <div class="table-wrap bulk-results"><table class="tbl"><thead><tr><th>Ящик</th><th>Итог</th><th>Подробности</th></tr></thead><tbody data-rows></tbody></table></div>`;
}
function bulkWireResults(root, r, preview){
  const rows=root.querySelector('[data-rows]');
  const draw=(f)=>{
    const list=r.results.filter(x=>!f||x.status===f);
    const max=500;
    rows.innerHTML=list.slice(0,max).map(x=>{
      const [cls,pv,done]=BULK_STATUS[x.status]||['',x.status,x.status];
      const js=x.job_status?` <span class="badge ${esc(x.job_status)}">${esc(STATUS_LBL[x.job_status]||x.job_status)}</span>`:'';
      return `<tr><td><b>${esc(x.name)}</b><div class="small muted">${esc(x.username||'')}</div></td>
        <td><span class="tag ${cls}">${esc(preview?pv:done)}</span>${js}</td>
        <td class="small">${esc(x.detail||'')}${x.job_id?` <a href="#" class="small" data-job="${x.job_id}">задание №${x.job_id}</a>`:''}</td></tr>`;
    }).join('') + (list.length>max?`<tr><td colspan="3" class="muted small">… показаны первые ${max} из ${fmtNum(list.length)}</td></tr>`:'')
      + (list.length?'':'<tr><td colspan="3" class="empty">Нет строк</td></tr>');
    rows.querySelectorAll('[data-job]').forEach(a=>a.onclick=()=>jobDetails(parseInt(a.dataset.job,10)));
  };
  root.querySelectorAll('[data-chips] .chip').forEach(b=>b.onclick=()=>{
    root.querySelectorAll('[data-chips] .chip').forEach(x=>x.classList.toggle('active', x===b));
    draw(b.dataset.f);
  });
  draw('');
}

/** Окно групповой операции: проверка → подтверждение → запуск → итог.
 *  Возвращает промис, который разрешается true, если операция выполнена. */
function bulkDialog(actionKey, ids, params={}, opts={}){
  // opts.ep / opts.idsKey / opts.meta — то же окно для раздела «Ящики на сервере»
  // (свой адрес API, ключи ящиков вместо номеров, описание действия уже загружено).
  const ep=opts.ep||'/accounts/bulk', idsKey=opts.idsKey||'ids';
  return new Promise(async resolve=>{
    let meta;
    try{ meta=opts.meta||await bulkMeta(actionKey); }catch(e){ toastErr(e); return resolve(false); }
    // Параметры, которых не передали, настраиваются прямо в окне.
    const inline=(meta.params||[]).filter(p=>!(p.key in params));
    let finished=false;
    const m=modal(opts.title||`${meta.icon||''} ${meta.label}`, `
      ${opts.intro||`<p class="muted" style="margin-top:0">${esc(meta.desc||'')}</p>`}
      ${meta.note&&!opts.intro?`<div class="hint" style="margin-bottom:12px">💡 ${esc(meta.note)}</div>`:''}
      ${inline.length?`<div class="bulk-form boxed" data-form>${bulkFormHtml(inline, opts.values||{})}</div>`:''}
      <div data-preview><div class="empty"><span class="spinner"></span> Проверяем ${fmtNum(ids.length)} ${plural(ids.length,'ящик','ящика','ящиков')}…</div></div>
      <div data-confirm></div>`,
      {wide:true, onClose:()=>resolve(finished),
       footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go disabled>Выполнить</button>`});
    const form=m.body.querySelector('[data-form]');
    if(form) bulkFormWire(form, inline);
    // Поля «только при предупреждении» (например, «запустить, даже если места мало»)
    // показываются, лишь когда проверка о чём-то предупредила.
    const syncForm=(warnings)=>{
      if(!form) return;
      form.querySelectorAll('[data-onwarn]').forEach(row=>{ row.style.display=(warnings||[]).length?'':'none'; });
      form.style.display=[...form.querySelectorAll('[data-row]')].some(row=>row.style.display!=='none')?'':'none';
    };
    syncForm([]);
    const go=m.foot.querySelector('[data-go]');
    const current=()=>Object.assign({}, params, form?bulkFormRead(form, inline):{});
    let pv=null, seq=0;
    const box=m.body.querySelector('[data-preview]');
    const conf=m.body.querySelector('[data-confirm]');
    const refresh=async()=>{
      const my=++seq;
      go.disabled=true;
      try{ pv=await api(ep,{method:'POST',body:{action:actionKey, [idsKey]:ids, params:current(), preview:true}}); }
      catch(e){ if(my!==seq) return; box.innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}${e.data&&e.data.hint?'<br><span class="small">'+esc(e.data.hint)+'</span>':''}</div>`; conf.innerHTML=''; return; }
      if(my!==seq) return;
      syncForm(pv.warnings);
      box.innerHTML=bulkResultHtml(pv, true);
      bulkWireResults(box, pv, true);
      const n=(pv.counts&&pv.counts.ok)||0;
      go.className='btn '+(pv.danger>=2?'danger':(pv.danger===1?'danger':'primary'));
      go.textContent=n?`Выполнить для ${fmtNum(n)} ${plural(n,'ящика','ящиков','ящиков')}`:'Нечего выполнять';
      if(pv.danger>=2 && n){
        conf.innerHTML=`<div class="form-row confirm-row"><label>Это действие трудно или невозможно отменить. Для подтверждения введите число ящиков: <b>${fmtNum(n)}</b></label>
          <input type="text" id="bConfirm" inputmode="numeric" autocomplete="off" placeholder="${n}"></div>`;
        const inp=conf.querySelector('#bConfirm');
        const check=()=>{ go.disabled=inp.value.replace(/\s/g,'')!==String(n); };
        inp.oninput=check; check();
      }else{ conf.innerHTML=''; go.disabled=!n; }
    };
    if(form){
      let t=null;
      form.addEventListener('change', ()=>{ clearTimeout(t); t=setTimeout(refresh, 250); });
      form.addEventListener('input', (e)=>{ if(e.target.type==='checkbox') return; clearTimeout(t); t=setTimeout(refresh, 700); });
    }
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    go.onclick=async()=>{
      if(go.disabled||!pv) return;
      const confirmValue=(conf.querySelector('#bConfirm')||{}).value||'';
      go.disabled=true; go.innerHTML='<span class="spinner"></span> Выполняется…';
      let r;
      try{ r=await api(ep,{method:'POST',body:{action:actionKey, [idsKey]:ids, params:current(), preview:false, confirm:confirmValue.replace(/\s/g,'')}}); }
      catch(e){ toastErr(e); go.disabled=false; go.textContent='Выполнить'; return; }
      finished=true;
      if(opts.onDone) try{ opts.onDone(r); }catch(_){}
      if(form) form.remove();
      conf.innerHTML='';
      box.innerHTML=`<div class="hint okbox">✅ ${esc(r.summary)}${r.op_id?` Операция №${r.op_id} — в ${opts.historyName||'истории групповых действий'}.`:''}</div>`+bulkResultHtml(r, false);
      bulkWireResults(box, r, false);
      m.foot.innerHTML=`${(r.jobs&&r.jobs.length)?'<button class="btn" data-jobs>⚙️ Открыть очередь заданий</button>':''}<button class="btn primary" data-close>Готово</button>`;
      const jb=m.foot.querySelector('[data-jobs]'); if(jb) jb.onclick=()=>{ m.close(); location.hash='#/jobs'; };
      m.foot.querySelector('[data-close]').onclick=()=>{ m.close(); if(State.view==='accounts' || (opts.refreshView && State.view===opts.refreshView)) route(); };
      toast('Готово', r.summary, (r.counts.fail?'warn':'success'), 7000);
    };
    refresh();
  });
}

// ---------- История групповых действий ----------
async function loadBulkHistory(box){
  if(!box) return;
  let d;
  try{ d=await api('/accounts/bulk/history?limit=30',{bg:true}); }catch(e){ box.innerHTML=`<div class="empty">${esc(e.message)}</div>`; return; }
  const items=d.items||[];
  if(!items.length){ box.innerHTML='<div class="empty">Групповых действий ещё не было.</div>'; return; }
  box.innerHTML=`<div class="table-wrap"><table class="tbl"><thead><tr><th>№</th><th>Когда</th><th>Кто</th><th>Действие</th><th>Ящиков</th><th>Итог</th><th></th></tr></thead><tbody>${items.map(o=>`<tr>
      <td class="muted nowrap">${o.id}</td><td class="small nowrap">${esc(fmtDate(o.created_at))}</td><td class="small">${esc(o.user)}</td>
      <td>${esc(o.label)}</td><td class="small">${fmtNum(o.total)}</td>
      <td class="small"><span class="tag ok">выполнено ${fmtNum(o.ok)}</span>${o.skipped?` <span class="tag">пропущено ${fmtNum(o.skipped)}</span>`:''}${o.failed?` <span class="tag err">ошибок ${fmtNum(o.failed)}</span>`:''}${o.jobs?` <span class="tag">заданий ${fmtNum(o.jobs)}</span>`:''}</td>
      <td style="text-align:right"><button class="btn sm" data-op="${o.id}">Подробнее</button></td></tr>`).join('')}</tbody></table></div>`;
  box.querySelectorAll('[data-op]').forEach(b=>b.onclick=()=>bulkOpModal(parseInt(b.dataset.op,10), box));
}

async function bulkOpModal(opId, historyBox){
  let o;
  try{ o=await api(`/accounts/bulk/history/${opId}`); }catch(e){ return toastErr(e); }
  const pr=o.job_progress||{};
  const jobsTotal=Object.values(pr).reduce((s,n)=>s+n,0);
  const doneJobs=(pr.success||0)+(pr.partial||0)+(pr.failed||0)+(pr.cancelled||0);
  const params=Object.entries(o.params||{}).filter(([k,v])=>v!==''&&v!==null&&v!==undefined)
    .map(([k,v])=>`<span class="tag">${esc(k)}: ${esc(typeof v==='object'?JSON.stringify(v):String(v))}</span>`).join(' ');
  const r={total:o.total, counts:{ok:o.ok, skip:o.skipped, fail:o.failed}, results:o.results||[], reasons:{}, warnings:[], jobs:[]};
  (o.results||[]).forEach(x=>{ if(x.status==='skip') r.reasons[x.detail]=(r.reasons[x.detail]||0)+1; });
  const m=modal(`Групповое действие №${o.id}: ${o.label}`, `
    <div class="kv"><div class="k">Когда и кто</div><div>${esc(fmtDate(o.created_at))} · ${esc(o.user)}</div></div>
    ${params?`<div class="kv"><div class="k">Параметры</div><div>${params}</div></div>`:''}
    <div class="kv"><div class="k">Итог</div><div>${esc(o.summary)}</div></div>
    ${jobsTotal?`<div class="card" style="margin:12px 0">
      <b>Задания операции: ${fmtNum(doneJobs)} из ${fmtNum(jobsTotal)} завершены</b>
      <div class="progress" style="margin:8px 0"><span style="width:${Math.round(100*doneJobs/Math.max(1,jobsTotal))}%"></span></div>
      <div class="small">${Object.entries(pr).map(([s,n])=>`<span class="badge ${esc(s)}">${esc(STATUS_LBL[s]||s)}: ${fmtNum(n)}</span>`).join(' ')}</div>
      ${o.jobs_active?`<button class="btn danger sm" id="opCancel" style="margin-top:10px">⏹️ Отменить незавершённые (${fmtNum(o.jobs_active)})</button>`:''}
    </div>`:''}
    <div data-res>${bulkResultHtml(r, false)}</div>`, {wide:true});
  bulkWireResults(m.body.querySelector('[data-res]'), r, false);
  const cb=$('#opCancel', m.el);
  if(cb) cb.onclick=async()=>{
    if(!await confirmDlg('Отменить незавершённые задания?', `Задания этой операции, которые ещё ждут очереди или выполняются (${o.jobs_active}), будут отменены. Уже сделанное останется.`, {okText:'Отменить задания'})) return;
    try{ const res=await api(`/accounts/bulk/history/${o.id}/cancel`,{method:'POST'}); toast('Готово', `Отменено заданий: ${res.cancelled}`); m.close(); bulkOpModal(opId, historyBox); if(historyBox) loadBulkHistory(historyBox); }
    catch(e){ toastErr(e); }
  };
}

// ---------- Добавление ящиков списком ----------
function bulkCreateModal(){
  State.bulkActions=null;
  bulkMeta().then(meta=>{
    const d=meta.create_defaults||{};
    const m=modal('Добавить ящики списком', `
      <p class="muted" style="margin-top:0">По одному ящику в строке: <b>адрес;пароль</b>. Пароль можно не указывать —
        такой ящик заведётся выключенным, а пароли загрузите потом файлом («Загрузить пароли»). Подойдут и две колонки,
        скопированные из Excel, и строки вида «Иванов Иван &lt;ivanov@company.ru&gt;;пароль». Если адрес есть в
        справочнике сотрудников, ящик получит ФИО и сразу свяжется с карточкой.</p>
      <div class="form-row"><label>Список ящиков</label><textarea id="c-text" rows="7" class="mono" placeholder="ivanov@company.ru;Пароль1&#10;petrova@company.ru"></textarea></div>
      <div class="grid cols-2">
        <div class="form-row"><label>IMAP-сервер</label><input type="text" id="c-host" value="${esc(d.host||'')}" placeholder="imap.company.ru"></div>
        <div class="form-row"><label>Порт</label><input type="number" id="c-port" min="1" max="65535" value="${esc(d.port||993)}"></div>
      </div>
      <div class="grid cols-2">
        <div class="form-row"><label>Шифрование</label><select id="c-sec">
          <option value="ssl" ${d.security!=='starttls'&&d.security!=='plain'?'selected':''}>SSL/TLS (порт 993)</option>
          <option value="starttls" ${d.security==='starttls'?'selected':''}>STARTTLS (порт 143)</option>
          <option value="plain" ${d.security==='plain'?'selected':''}>Без шифрования</option></select></div>
        <div class="form-row"><label>Вход в ящики</label><select id="c-auth"><option value="password">по паролю ящика</option>
          ${d.master?'<option value="master">через администратора почты</option>':''}</select></div>
      </div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="c-en" checked><span class="track"></span></span><label>Сразу включить ящики, у которых есть пароль</label></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="c-sch" ${d.global_schedule?'':'checked'}><span class="track"></span></span><label>Завести каждому своё расписание копирования</label></div>
      <div class="form-row" id="c-timerow"><label>Время копирования (каждый день)</label><input type="time" id="c-time" value="02:00"></div>
      ${d.global_schedule?'<div class="hint" style="margin-bottom:12px">🔁 Включено копирование всех ящиков по очереди — новые ящики попадут в него и без своего расписания.</div>':''}
      <div data-preview></div>`,
      {wide:true, footer:'<button class="btn ghost" data-c>Отмена</button><button class="btn" data-check>Проверить</button><button class="btn primary" data-go disabled>Добавить</button>'});
    const g=x=>m.body.querySelector(x);
    const box=g('[data-preview]'), go=m.foot.querySelector('[data-go]');
    const syncTime=()=>{ g('#c-timerow').style.display=g('#c-sch').checked?'':'none'; };
    g('#c-sch').addEventListener('change', syncTime); syncTime();
    const body=(preview)=>({text:g('#c-text').value, host:g('#c-host').value.trim(), port:parseInt(g('#c-port').value||'993',10),
      security:g('#c-sec').value, auth_type:g('#c-auth').value, enable:g('#c-en').checked,
      schedule_time:g('#c-sch').checked?(g('#c-time').value||''):'', preview});
    let seq=0;
    const check=async()=>{
      const my=++seq; go.disabled=true;
      if(!g('#c-text').value.trim()){ box.innerHTML=''; return; }
      box.innerHTML='<div class="empty"><span class="spinner"></span> Проверяем список…</div>';
      try{
        const r=await api('/accounts/bulk-create',{method:'POST',body:body(true)});
        if(my!==seq) return;
        box.innerHTML=bulkResultHtml(r, true); bulkWireResults(box, r, true);
        go.disabled=!r.counts.ok; go.textContent=r.counts.ok?`Добавить ${fmtNum(r.counts.ok)} ${plural(r.counts.ok,'ящик','ящика','ящиков')}`:'Добавлять нечего';
      }catch(e){ if(my!==seq) return; box.innerHTML=`<div class="hint warnbox">⚠️ ${esc(e.message)}</div>`; }
    };
    let t=null;
    m.body.addEventListener('input', ()=>{ clearTimeout(t); t=setTimeout(check, 700); });
    m.body.addEventListener('change', ()=>{ clearTimeout(t); t=setTimeout(check, 200); });
    m.foot.querySelector('[data-check]').onclick=check;
    m.foot.querySelector('[data-c]').onclick=()=>m.close();
    go.onclick=async()=>{
      go.disabled=true; go.innerHTML='<span class="spinner"></span> Добавляем…';
      try{
        const r=await api('/accounts/bulk-create',{method:'POST',body:body(false)});
        box.innerHTML=`<div class="hint okbox">✅ ${esc(r.summary)}</div>`+bulkResultHtml(r, false); bulkWireResults(box, r, false);
        m.foot.innerHTML='<button class="btn primary" data-close>Готово</button>';
        m.foot.querySelector('[data-close]').onclick=()=>{ m.close(); route(); };
        g('#c-text').value='';
        toast('Ящики добавлены', r.summary);
      }catch(e){ toastErr(e); go.disabled=false; go.textContent='Добавить'; }
    };
  }).catch(toastErr);
}

// ---------- Кнопки на странице ящиков ----------
/** «Копия всех ящиков с нуля»: групповое действие над всеми включёнными ящиками. */
function rebuildAllModal(){
  const accs=State.accounts||[];
  const ids=accs.filter(a=>a.enabled).map(a=>a.id);
  if(!ids.length) return toast('Нет включённых ящиков','','warn');
  bulkDialog('rebuild_full', ids, {}, {title:'🧨 Копия всех ящиков с нуля', intro:`
    <p style="margin-top:0">Для каждого включённого ящика прежняя локальная копия будет перенесена в <b>карантин</b>
      («Прежние копии»), а все письма — скачаны с почтового сервера заново.</p>
    <ul class="small" style="margin:0 0 12px;padding-left:20px;line-height:1.6">
      <li>Письма, которых на сервере уже нет, останутся только в карантине: в просмотре, поиске и экспорте их не будет.</li>
      <li>Карантин занимает место на диске, пока вы его не удалите (Групповые действия → «Удалить прежние копии»).</li>
      <li>Все письма скачиваются заново — это долго и нагружает почтовый сервер; ящики идут через общую очередь.</li>
      <li>Ящики с удержанием архива, выключенные и без пароля пропускаются.</li>
      <li>Если нужно лишь вернуть пропавшие файлы, безопаснее «Докачать потерянные письма» — ничего не удаляется.</li>
    </ul>`});
}

/** Список ящиков файлом Excel. */
async function exportAccountList(ids){
  try{
    await apiDownload('/accounts/export-list', {ids: ids&&ids.length?ids:null, format:'xlsx'}, 'mailboxes.xlsx');
    toast('Список выгружен', ids&&ids.length?`Ящиков: ${fmtNum(ids.length)}`:'Все ящики');
  }catch(e){ toastErr(e); }
}

/** Скачать файл, который сервер отдаёт на POST-запрос. */
async function apiDownload(path, body, fallbackName){
  const res=await fetch('/api'+path, {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'fetch'}, body:JSON.stringify(body||{})});
  if(!res.ok){
    let msg='Ошибка '+res.status;
    try{ const d=await res.json(); msg=d.message||(typeof d.detail==='string'?d.detail:'')||msg; }catch(_){}
    if(res.status===401) sessionGone();
    throw new Error(msg);
  }
  const blob=await res.blob();
  const cd=res.headers.get('content-disposition')||'';
  const mm=/filename="([^"]+)"/.exec(cd);
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=(mm&&mm[1])||fallbackName;
  document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 1500);
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
        <option value="oauth2"${a.auth_type==='oauth2'?' selected':''}>OAuth2 (Gmail / Microsoft 365)</option>
        <option value="master"${a.auth_type==='master'?' selected':''}>Через администратора почты (без пароля ящика)</option>
        ${(a.auth_type==='axigen_ftp'||((State.user||{}).features||{}).axigen)?`<option value="axigen_ftp"${a.auth_type==='axigen_ftp'?' selected':''}>Через FTP Backup Axigen (без пароля ящика)</option>`:''}
        ${(a.auth_type==='server'||((State.user||{}).features||{}).mailservers)?`<option value="server"${a.auth_type==='server'?' selected':''}>Через подключение к почтовому серверу (без пароля ящика)</option>`:''}</select></div>
    </div>
    <div class="form-row"><label>Логин ${H('username')}</label><input id="f-user" type="text" value="${esc(a.username)}" placeholder="user@example.ru"></div>
    <div class="hint" id="axftpHint" style="display:${a.auth_type==='axigen_ftp'?'block':'none'};margin-bottom:8px">Письма берёт администратор Axigen через службу FTP Backup («Настройки → Сервер Axigen») — пароль сотрудника не нужен. Логин — полный адрес. Пароль ниже необязателен: он нужен только для восстановления по IMAP и входа сотрудника в архив.</div>
    <div id="srvAuthBox" style="display:${a.auth_type==='server'?'block':'none'}">
      <div class="form-row"><label>Подключение</label><select id="f-srv"><option value="">— загружается… —</option></select>
        <div class="hint" id="f-srv-hint">В ящик входит само подключение (токен приложения или администратор почты) — пароль сотрудника не нужен. Сервер, порт и шифрование подставляются из подключения.</div></div>
    </div>
    <div class="form-row" id="pwrow" style="display:${(a.auth_type==='password'||a.auth_type==='axigen_ftp')?'block':'none'}"><label>Пароль ${H('password')}</label><input id="f-pass" type="password" placeholder="${id?'оставьте пустым, чтобы не менять':(a.auth_type==='axigen_ftp'?'необязательно':'пароль или пароль приложения')}"></div>
    <div id="axBox"></div>
    <div id="oauthBox" style="display:${a.auth_type==='oauth2'?'block':'none'}">
      <div class="form-row"><label>OAuth2 Client ID ${H('oauth_client_id')}</label><input id="f-ocid" type="text" value="${esc(a.oauth_client_id||'')}"></div>
      <div class="form-row"><label>OAuth2 Client Secret ${H('oauth_client_secret')}</label><input id="f-ocs" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Refresh Token ${H('oauth_refresh_token')}</label><input id="f-ort" type="password" placeholder="${id?'без изменений':''}"></div>
      <div class="form-row"><label>OAuth2 Token URL ${H('oauth_token_url')}</label><input id="f-otu" type="text" value="${esc(a.oauth_token_url||'')}" placeholder="https://oauth2.googleapis.com/token"></div>
    </div>
    <div class="form-row"><label>Копировать только папки ${H('folder_include')}</label><input id="f-inc" type="text" value="${esc((a.folder_include||[]).join(', '))}" placeholder="пусто — все папки"></div>
    <div class="form-row"><label>Исключить папки ${H('folder_exclude')}</label><input id="f-exc" type="text" value="${esc((a.folder_exclude||[]).join(', '))}" placeholder="Спам, Корзина"></div>
    <div class="form-row"><label>Заметки</label><input id="f-notes" type="text" value="${esc(a.notes||'')}" placeholder="например: кто владелец ящика"></div>
    <div class="form-row"><label>Хранение локальных копий</label><select id="f-ret">
      <option value="-1"${(a.retention_days??-1)===-1?' selected':''}>По глобальной настройке</option>
      <option value="0"${a.retention_days===0?' selected':''}>Хранить всё (бессрочно)</option>
      <option value="3"${a.retention_days===3?' selected':''}>Последние 3 дня</option>
      <option value="7"${a.retention_days===7?' selected':''}>Последняя неделя</option>
      <option value="30"${a.retention_days===30?' selected':''}>Последние 30 дней</option>
      ${(a.retention_days>0 && ![3,7,30].includes(a.retention_days))?`<option value="${a.retention_days}" selected>Свой срок: ${a.retention_days} дн.</option>`:''}</select>
      <div class="hint">Копии старше срока удаляются ежедневно (на письма на сервере не влияет). Свой срок в днях задаётся в меню ящика → «Хранение копий».</div></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="f-en" ${a.enabled?'checked':''}><span class="track"></span></span><label>Ящик включён (участвует в бэкапе)</label></div>`;
  const m=modal(id?'Редактирование ящика':'Новый почтовый ящик', body, {wide:true,
    footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn" data-test>Проверить подключение</button><button class="btn primary" data-save>Сохранить</button>`});
  const g=(x)=>m.body.querySelector(x);
  let msConns=[];
  const srvFill=()=>{ const sel=g('#f-srv'), cur=msConns.find(x=>String(x.id)===sel.value);
    if(cur){ g('#f-host').value=cur.imap_host||''; g('#f-port').value=cur.imap_port||993; g('#f-sec').value=cur.imap_security||'ssl'; } };
  const srvLoad=async()=>{
    if(msConns.length || !(((State.user||{}).features||{}).mailservers)) return;
    try{ msConns=((await api('/mailservers/brief',{bg:true})).items||[]).filter(x=>x.imap_admin && (x.enabled || x.id===a.server_id)); }catch(e){ msConns=[]; }
    const sel=g('#f-srv');
    sel.innerHTML=msConns.length?msConns.map(x=>`<option value="${x.id}"${x.id===a.server_id?' selected':''}>${esc(x.label)} «${esc(x.name)}» — ${esc(x.imap_host||'')}</option>`).join('')
      :'<option value="">нет подключений, которые входят в ящики без паролей</option>';
    if(!a.server_id || !msConns.some(x=>x.id===a.server_id)) srvFill();
  };
  g('#f-srv').onchange=srvFill;
  g('#f-auth').onchange=()=>{ const v=g('#f-auth').value; m.body.querySelector('#oauthBox').style.display=v==='oauth2'?'block':'none'; m.body.querySelector('#pwrow').style.display=(v==='password'||v==='axigen_ftp')?'block':'none'; m.body.querySelector('#axftpHint').style.display=v==='axigen_ftp'?'block':'none';
    m.body.querySelector('#srvAuthBox').style.display=v==='server'?'block':'none'; if(v==='server') srvLoad().then(srvFill); };
  if(a.auth_type==='server') srvLoad();
  if(id && ((State.user||{}).features||{}).axigen) axigenMailboxInfo(id, g('#axBox'));
  const collect=()=>({name:g('#f-name').value,host:g('#f-host').value,port:parseInt(g('#f-port').value||'993'),username:g('#f-user').value,
     password:g('#f-pass').value,security:g('#f-sec').value,auth_type:g('#f-auth').value,enabled:g('#f-en').checked,
     folder_exclude:g('#f-exc').value.split(',').map(s=>s.trim()).filter(Boolean),
     // Эти поля обязательно отправляем: сервер сохраняет их всегда, и раньше
     // «Сохранить» (и даже «Проверить подключение») молча обнуляло белый список
     // папок и заметки у ящиков, заведённых по шаблону сотрудников.
     folder_include:g('#f-inc').value.split(',').map(s=>s.trim()).filter(Boolean),
     notes:g('#f-notes').value,
     retention_days:parseInt(g('#f-ret').value),
     oauth_client_id:g('#f-ocid')?g('#f-ocid').value:'',oauth_client_secret:g('#f-ocs')?g('#f-ocs').value:'',
     oauth_refresh_token:g('#f-ort')?g('#f-ort').value:'',oauth_token_url:g('#f-otu')?g('#f-otu').value:'',
     server_id:g('#f-auth').value==='server'&&g('#f-srv').value?parseInt(g('#f-srv').value,10):null});
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

/** Дата удержания для людей: «бессрочно» вместо 9999-12-31. */
function holdDate(until){ return until==='9999-12-31' ? 'бессрочно' : fmtDateShort(until+'T00:00:00'); }

/** Метки «уволен», «удерживается до …», «удержание истекло». */
function holdTags(a){
  const out=[];
  if(a.dismissed_at) out.push(`<span class="tag" title="Сотрудник уволен ${esc(fmtDate(a.dismissed_at))}${a.auto_disabled?'; копирование выключено автоматически':''}">уволен</span>`);
  if(a.on_hold) out.push(a.hold_reason==='case'?'<span class="tag ok" title="Архив удерживается открытым делом (раздел «Дела»): письма не удаляются, пока дело не закрыто">🔒 по делу</span>'
    :`<span class="tag ok" title="Письма ящика не удаляются по сроку хранения${a.hold_reason==='dismissed'?' (архив уволенного сотрудника)':''}">🔒 ${a.hold_until==='9999-12-31'?'бессрочно':'до '+esc(holdDate(a.hold_until))}</span>`);
  else if(a.hold_expired) out.push(`<span class="tag warn" title="Срок удержания архива прошёл — архив можно удалить (меню ящика)">удержание истекло</span>`);
  return out.join(' ');
}

function holdModal(a){
  if(a.hold_reason==='case'){
    modal(`Удержание архива: ${a.name}`, `<p>Архив ящика удерживается <b>открытым делом</b>: письма не удаляются ни по сроку хранения, ни вручную.
      Удержание снимается, когда дело закрывают или убирают из него ящик.</p>${isAdmin()?'<p><a href="#/cases">Открыть раздел «Дела»</a></p>':''}`);
    return;
  }
  const cur=a.hold_until||'';
  const m=modal(`Удержание архива: ${a.name}`, `
    <p class="small">Пока действует удержание, письма ящика <b>не удаляются</b> очисткой по сроку хранения — даже если у ящика
    срок «3 дня». Удобно для архива уволенного сотрудника, проверок и споров.${a.hold_reason==='dismissed'?' Сейчас архив удерживается потому, что сотрудник уволен.':''}</p>
    <div class="form-row"><label>Удерживать до</label><input type="date" id="h-date" value="${cur && cur!=='9999-12-31'?esc(cur):''}"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="h-forever" ${cur==='9999-12-31'?'checked':''}><span class="track"></span></span><label>Бессрочно</label></div>`,
    {footer:`${cur?'<button class="btn ghost" data-clear>Снять удержание</button>':''}<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-save>Сохранить</button>`});
  const send=async(until)=>{ try{ await api(`/accounts/${a.id}/hold`,{method:'POST',body:{until}}); toast('Сохранено', until?'Удержание установлено':'Удержание снято'); m.close(); route(); }catch(e){ toastErr(e); } };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-save]').onclick=()=>{ const f=m.body.querySelector('#h-forever').checked; const d=m.body.querySelector('#h-date').value; if(!f&&!d) return toast('Укажите дату или «Бессрочно»','','warn'); send(f?'forever':d); };
  const cl=m.foot.querySelector('[data-clear]'); if(cl) cl.onclick=async()=>{ if(await confirmDlg('Снять удержание?', 'Письма ящика снова будут удаляться по сроку хранения (если он задан).', {okText:'Снять', okClass:'danger'})) send(''); };
}

function purgeModal(a){
  const m=modal(`Удалить вместе с архивом: ${a.name}`, `
    <div class="hint" style="border-color:var(--danger)">⛔ Будут удалены ящик, его настройки, индекс и <b>все файлы писем на диске</b>
    (писем: ${fmtNum(a.messages)}, ${esc(a.bytes_h||'')}). Это необратимо; письма, которых уже нет на почтовом сервере, не восстановить.
    Копия вне сервера удалит их у себя при следующем прогоне.</div>
    <div class="form-row" style="margin-top:10px"><label>Для подтверждения введите название ящика: <b>${esc(a.name)}</b></label><input type="text" id="p-name" autocomplete="off"></div>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn danger" data-go>Удалить навсегда</button>`});
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    try{ const r=await api(`/accounts/${a.id}/purge`,{method:'POST',body:{confirm_name:m.body.querySelector('#p-name').value}});
      toast('Удалено', `Файлов писем удалено: ${fmtNum(r.files)} (${fmtBytes(r.bytes)})`); m.close(); route(); }
    catch(e){ toastErr(e); }
  };
}

async function retentionModal(a){
  let cur = a.retention_days, retired = 0;
  try{ const full=await api(`/accounts/${a.id}`); cur=full.retention_days; retired=full.retired||0; }
  catch(e){ if(cur===undefined) cur=-1; }
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
      <input id="ret-custom" type="number" min="0" placeholder="напр. 30"></div>
    ${retired?`<div class="hint" style="margin-top:12px">Отсеяно по сроку хранения: <b>${fmtNum(retired)}</b> ${plural(retired,'письмо','письма','писем')} —
      копирование их не скачивает (они старше срока) или очистка уже удалила их из архива, а на почтовом сервере они ещё лежат.
      <div style="margin-top:8px"><button class="btn sm" data-forget>Забыть отсеянные письма</button></div>
      <div class="small muted" style="margin-top:6px">Следующее копирование заново проверит даты этих писем на сервере: моложе срока — скачаются,
      старше — снова будут пропущены. Нужно, если часы сервера архива уходили вперёд или у писем на почтовом сервере были неверные даты.</div></div>`:''}`,
    {footer:`<button class="btn ghost" data-c>Закрыть</button><button class="btn primary" data-save>Сохранить свой срок</button>`});
  const fg=m.body.querySelector('[data-forget]');
  if(fg) fg.onclick=async()=>{
    if(!await confirmDlg('Забыть отсеянные письма?', `Следующее копирование ящика «${a.name}» заново проверит даты ${fmtNum(retired)} ${plural(retired,'письма','писем','писем')} на сервере. Письма моложе срока хранения скачаются.`, {okText:'Забыть', okClass:'primary'})) return;
    try{ const r=await api(`/accounts/${a.id}/retired/clear`,{method:'POST'}); toast('Готово', `Забыто: ${fmtNum(r.cleared)}. Проверка — при следующем копировании.`); m.close(); }
    catch(e){ toastErr(e); }
  };
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

// ---------- Экспорт ----------
async function exportModal(a){
  const engines=State.engines.length?State.engines:await api('/export/engines');
  const H=(k)=>helpIcon(fieldHelp('export',k));
  const opts=engines.map(e=>`<option value="${e.name}" ${e.available?'':'disabled'}>${esc(e.title)}${e.available?'':' — недоступен'}${e.experimental?' ⚗️':''}</option>`).join('');
  const body=`
    <div class="form-row"><label>Формат / движок ${H('engine')}</label><select id="e-engine">${opts}</select>
      <div class="hint" id="e-desc"></div></div>
    <div class="form-row"><label>Формат PST ${H('pst_format')}</label><select id="e-pst"><option value="unicode">Unicode — Outlook 2003 и новее</option><option value="ansi">ANSI — Outlook 97–2002 (до 2 ГБ)</option></select></div>
    <div class="grid cols-2">
      <div class="form-row"><label>Дата с ${H('date_from')}</label><input id="e-df" type="date"></div>
      <div class="form-row"><label>Дата по ${H('date_to')}</label><input id="e-dt" type="date"></div>
    </div>
    <div class="form-row"><label>Только папки (через запятую, пусто = все) ${H('folders')}</label><input id="e-folders" type="text" placeholder="INBOX, Отправленные"></div>
    <div class="hint">Файл будет доступен для скачивания в разделе «Экспорт» после завершения.</div>`;
  const m=modal(`Экспорт: ${a.name}`, body, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Начать экспорт</button>`});
  const g=x=>m.body.querySelector(x);
  // значения по умолчанию — из раздела «Настройки» → «Экспорт»
  try{
    const d=await api('/export/defaults');
    const pick=(sel,val)=>{ const el=g(sel); if(el && [...el.options].some(o=>o.value===val && !o.disabled)) el.value=val; };
    pick('#e-engine', d.engine); pick('#e-pst', d.pst_format);
  }catch(_){}
  // Встроенный движок пишет только Unicode PST — выбор формата ему не нужен.
  const upd=()=>{ const e=engines.find(x=>x.name===g('#e-engine').value); g('#e-desc').innerHTML=e?esc(e.desc)+(e.experimental?' <strong style="color:var(--warn)">Новый движок — перед массовой выгрузкой откройте пробный файл в своём Outlook.</strong>':''):''; const isPst=e&&e.fmt==='pst'&&e.name!=='native'; g('#e-pst').closest('.form-row').style.display=isPst?'block':'none'; };
  g('#e-engine').onchange=upd; upd();
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const e=engines.find(x=>x.name===g('#e-engine').value);
    const body={engine:g('#e-engine').value, format:e?e.fmt:'pst', pst_format:(e&&e.name==='native')?'unicode':g('#e-pst').value,
      date_from:g('#e-df').value||null, date_to:g('#e-dt').value||null,
      folders:g('#e-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
    const btn=m.foot.querySelector('[data-go]'); if(btn.disabled) return; btn.disabled=true;
    try{ await api(`/accounts/${a.id}/export`,{method:'POST',body}); toast('Экспорт запущен','Следите за прогрессом в очереди'); m.close(); location.hash='#/jobs'; }catch(e){toastErr(e);}
    finally{ btn.disabled=false; }
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
    ${a.auth_type==='axigen_ftp'?`<div class="form-row"><label>Как заливать</label><select id="r-via">
      <option value="ftp"${a.imap_login?'':' selected'}>Через FTP Backup Axigen — без пароля ящика (без отметок «прочитано» и «важное»)</option>
      <option value="imap"${a.imap_login?' selected':''}${a.imap_login?'':' disabled'}>По IMAP с паролем ящика${a.imap_login?'':' — пароль не задан'}</option></select>
      <div class="hint">Через FTP Backup письма заливает администратор Axigen: нужно «Разрешить изменения на сервере» в настройках Axigen. В конце MailArchiver проверяет, что сервер показывает залитые письма.</div></div>`:''}
    <div class="grid cols-2">
      <div class="form-row check"><span class="switch"><input type="checkbox" id="r-dup" checked><span class="track"></span></span><label>Пропускать дубли ${H('check_duplicates')}</label></div>
      <div class="form-row check"><span class="switch"><input type="checkbox" id="r-dry"><span class="track"></span></span><label>Пробный прогон ${H('dry_run')}</label></div>
    </div>`;
  const m=modal(`Восстановление: ${a.name}`, body, {wide:true, footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Восстановить</button>`});
  const g=x=>m.body.querySelector(x);
  g('#r-mode').onchange=()=>{ g('#r-prefixrow').style.display=g('#r-mode').value==='prefixed'?'block':'none'; g('#r-singlerow').style.display=g('#r-mode').value==='single'?'block':'none'; };
  m.foot.querySelector('[data-c]').onclick=m.close;
  m.foot.querySelector('[data-go]').onclick=async()=>{
    const mode=g('#r-mode').value;
    const body={target_mode:mode,target_prefix:g('#r-prefix').value.trim(),target_folder:g('#r-single').value.trim(),
      check_duplicates:g('#r-dup').checked,dry_run:g('#r-dry').checked,
      folders:g('#r-folders').value.split(',').map(s=>s.trim()).filter(Boolean)};
    if(!body.folders.length) body.folders=null;
    if(g('#r-via')) body.via=g('#r-via').value;
    // Пустой префикс/имя папки — это НЕ «в исходные папки»: раньше очистка
    // поля молча заливала архив прямо в рабочий INBOX. Сервер такое тоже
    // отклоняет, здесь — понятное сообщение до отправки.
    if(mode==='prefixed' && !body.target_prefix){ toastErr({message:'Укажите префикс папки — иначе письма попадут прямо в рабочие папки ящика.'}); return; }
    if(mode==='single' && !body.target_folder){ toastErr({message:'Укажите имя папки назначения.'}); return; }
    if(mode==='original' && !body.dry_run){
      const okGo=await confirmDlg('Залить в ИСХОДНЫЕ папки?',
        `Письма из архива будут добавлены прямо в рабочие папки ящика «${a.name}». Отменить это нельзя.`);
      if(!okGo) return;
    }
    try{ await api(`/accounts/${a.id}/restore`,{method:'POST',body}); toast('Восстановление запущено', body.dry_run?'Пробный прогон':'Заливка на сервер'); m.close(); location.hash='#/jobs'; }catch(e){toastErr(e);}
  };
}

// ---------- Импорт PST ----------
/** Имя или путь для команды на сервере — как shlex.quote: без кавычек путь с пробелом
 *  или кавычкой из подсказки, вставленный в терминал, сделал бы не то, что написано. */
function shq(s){ s=String(s==null?'':s); return /^[A-Za-z0-9_@%+=:,.\/-]+$/.test(s) ? s : "'"+s.replace(/'/g,"'\\''")+"'"; }
/** Текст без точки в конце: к сообщению сервера точку ставит сама фраза (иначе выходило «..»). */
function noDot(s){ return String(s==null?'':s).replace(/[.\s]+$/,''); }

function importModal(a){
  const H=(k)=>helpIcon(fieldHelp('import',k));
  const m=modal(`Импорт .pst: ${a.name}`, `
    <p class="muted small">Письма из файла Outlook (.pst или .ost) переносятся в ящик — на почтовый сервер или сразу в локальную копию. Файл разбирает сам MailArchiver: внешние программы не нужны. Переносятся папки, письма с вложениями, флаги («прочитано», «отвечено», «помечено») и даты получения; контакты, календарь и задачи пропускаются.</p>
    <div class="form-row"><label>Откуда взять файл ${H('source')}</label><select id="i-src">
      <option value="upload">Загрузить с компьютера</option>
      <option value="server">Взять с сервера (файл уже лежит в каталоге импорта)</option></select></div>
    <div class="form-row" id="i-uprow"><label>Файл .pst или .ost</label><input id="i-file" type="file" accept=".pst,.ost">
      <div class="hint" id="i-uphint"></div></div>
    <div id="i-srvrow" style="display:none">
      <div class="form-row"><label>Файл на сервере</label><select id="i-sfile"><option value="">загружается список…</option></select></div>
      <div class="hint" id="i-srvhint" style="margin-bottom:15px"></div>
    </div>
    <div class="form-row"><label>Куда импортировать ${H('target')}</label><select id="i-target">
      <option value="imap">На почтовый сервер (IMAP)</option>
      <option value="local">В локальную копию ящика (архив)</option></select></div>
    <div class="hint" id="i-localhint" style="display:none">Письма будут записаны только в архив MailArchiver: на почтовый сервер ничего не заливается. Письма старше срока хранения ящика не записываются — если импортируете старый архив, сначала поставьте архив ящика на удержание или задайте «хранить всё».</div>
    <div class="form-row"><label>Префикс папки назначения ${H('target_prefix')}</label><input id="i-prefix" type="text" value="Импорт PST" placeholder="пусто — папки как в файле"></div>
    <div class="form-row check"><span class="switch"><input type="checkbox" id="i-skip" checked><span class="track"></span></span><label>Пропускать письма, которые уже есть ${H('skip_existing')}</label></div>
    <details style="margin-top:8px"><summary class="small">Дополнительно</summary>
      <div class="form-row" style="margin-top:10px"><label>Способ разбора файла ${H('engine')}</label><select id="i-engine">
        <option value="builtin">Встроенный (без внешних программ)</option>
        <option value="readpst">readpst (запасной, если установлен на сервере)</option></select></div>
      <div class="form-row"><label>Кодировка старых файлов (ANSI) ${H('ansi_codepage')}</label><select id="i-cp">
        <option value="cp1251">Windows-1251 (кириллица)</option>
        <option value="cp1252">Windows-1252 (Западная Европа)</option>
        <option value="cp1250">Windows-1250 (Центральная Европа)</option>
        <option value="cp1257">Windows-1257 (Балтия)</option>
        <option value="cp866">DOS 866 (кириллица)</option></select></div>
    </details>`,
    {footer:`<button class="btn ghost" data-c>Отмена</button><button class="btn primary" data-go>Загрузить и импортировать</button>`});
  const g=x=>m.body.querySelector(x);
  g('#i-target').onchange=()=>{ g('#i-localhint').style.display=g('#i-target').value==='local'?'block':'none'; };
  const goBtn=m.foot.querySelector('[data-go]');
  const fromServer=()=>g('#i-src').value==='server';
  // Имя выбранного файла — прямо на кнопке: видно, что именно уйдёт в импорт (длинное — сокращается).
  const goText=()=>!fromServer() ? 'Загрузить и импортировать'
    : chosen ? `Импортировать «${chosen.length>30 ? chosen.slice(0,29)+'…' : chosen}»` : 'Импортировать';
  const showGo=()=>{ if(goBtn.disabled) return; goBtn.textContent=goText(); goBtn.title=(fromServer()&&chosen)?`Файл на сервере: ${chosen}`:''; };
  // Каталог импорта на сервере: файл больше предела загрузки кладут туда сами и выбирают по имени.
  let srv=null;
  // Предел загрузки, пока сервер не назвал свой (то же число — MAX_PST_UPLOAD_BYTES в api.py).
  // Раньше при неудачном запросе списка пропадали и подсказка о пределе, и проверка размера.
  let maxUpload=2*1024**3;
  // Что выбрал пользователь. Хранится отдельно от списка: список перечитывается (в том
  // числе после ошибки), и выбранным молча оказывался первый файл — уже не тот, что выбирали.
  let chosen='', lost='', loadSeq=0;
  const sel=g('#i-sfile'), code=x=>`<code>${esc(x)}</code>`;
  const showUploadLimit=()=>{ g('#i-uphint').textContent=`Через браузер загружается файл не больше ${fmtBytes(maxUpload)}. Файл крупнее положите на сервер и выберите «Взять с сервера».`; };
  const SANDBOX=' Учтите: служба под systemd не видит каталоги <code>/home</code>, <code>/root</code> и <code>/tmp</code> (в её unit-файле ProtectHome=true и PrivateTmp=true) — что бы ни лежало там и какие бы права ни стояли, для службы там пусто или закрыто. Держите файлы в другом месте, например в <code>/srv</code> или <code>/mnt</code>.';
  // Совет под причину, по которой каталог импорта недоступен: причину (problem, reason) называет сервер.
  const unavailableHtml=(owner)=>{
    const dir=srv.directory||'', real=srv.link_target||'';      // куда ведёт каталог импорта, если он ссылка
    const who=owner?` ${code(owner)}`:'', chown=owner?` && sudo chown ${shq(owner)}: ${shq(dir)}`:'';
    let advice='';
    if(srv.problem==='dangling') advice='Если каталог лежит на отдельном диске — подключите диск (<code>findmnt</code>, <code>sudo mount -a</code>).'+SANDBOX;
    else if(srv.problem==='missing') advice=`Создайте его на сервере: ${code(`sudo mkdir -p ${shq(dir)}${chown}`)}.`;
    else if(srv.problem==='not_dir') advice=`На месте каталога лежит файл: уберите или переименуйте его и создайте каталог: ${code(`sudo mkdir ${shq(dir)}${chown}`)}.`;
    else if(srv.problem==='closed') advice= !owner ? 'Проверьте права на каталог.'
      : real ? `Каталог импорта — ссылка на ${code(real)}. Дайте пользователю службы право читать этот каталог, например: ${code(`sudo setfacl -m u:${shq(owner)}:rx ${shq(real)}`)}. Отдавать его целиком (<code>chown</code>) стоит, только если он нужен одному импорту.`
      : `Отдайте каталог пользователю службы: ${code(`sudo chown ${shq(owner)}: ${shq(dir)}`)}.`;
    else if(srv.problem==='denied') advice=`Каждый каталог на пути к нему должен пускать пользователя службы${who}. Права по всему пути покажет ${code(`namei -l ${shq(real||dir)}`)}.`+(real?SANDBOX:'');
    else if(srv.problem==='overlap') advice= real
      ? `Сейчас каталог импорта — ссылка на ${code(real)}, а там лежат и файлы самой службы. Направьте ссылку на отдельный каталог, где будут только файлы .pst и .ost: ${code(`sudo mkdir -p ${shq(real+'/pst-import')}${owner?` && sudo chown ${shq(owner)}: ${shq(real+'/pst-import')}`:''} && sudo ln -sfn ${shq(real+'/pst-import')} ${shq(dir)}`)}.`+SANDBOX
      : 'Параметр <code>export.tmp_dir</code> в config.yaml направляет временные файлы службы в каталог импорта: задайте для них другой каталог (или уберите параметр) и перезапустите службу.';
    else if(srv.problem==='loop') advice='Пересоздайте ссылку так, чтобы она вела на настоящий каталог: куда ведёт каждая, покажет <code>ls -l</code>.'+SANDBOX;
    else advice=`Проверьте на сервере, что каталог существует и открыт пользователю службы${who}.`;
    return `${esc(noDot(srv.reason)||`Служба не может прочитать каталог импорта ${dir}`)}. ${advice} Затем нажмите «Обновить список».`;
  };
  const renderServerFiles=(error)=>{
    const hint=g('#i-srvhint');
    const refresh=' <button class="btn small" type="button" data-refresh>Обновить список</button>';
    // у root доступ есть к любому файлу — про chown подсказывать незачем
    const owner=(srv && srv.user && srv.user!=='root') ? srv.user : '';
    if(!srv){
      sel.innerHTML='<option value="">список не получен</option>';
      hint.innerHTML=`Не удалось получить список файлов на сервере${error?': '+esc(noDot(error)):''}. Загрузить файл с компьютера можно и без него.${refresh}`;
    }else if(srv.available===false){
      // каталог создан не службой (командой от root), это ссылка на отключённый диск и т. п.
      sel.innerHTML='<option value="">каталог импорта недоступен</option>';
      hint.innerHTML=unavailableHtml(owner)+refresh;
    }else{
      const files=srv.files||[], dir=srv.directory||'';
      const locked=files.filter(f=>f.readable===false);
      // Первой идёт пустая строка: файл выбирает человек, а не порядок в списке.
      sel.innerHTML=files.length
        ? '<option value="">— выберите файл —</option>'+files.map(f=>`<option value="${esc(f.name)}" ${f.readable===false?'disabled':''}>${esc(f.name)} — ${esc(fmtBytes(f.size))}, изменён ${esc(fmtDate(f.modified))}${f.readable===false?' — нет доступа у службы':''}</option>`).join('')
        : '<option value="">в каталоге импорта нет файлов .pst и .ost</option>';
      if(chosen && !files.some(f=>f.name===chosen && f.readable!==false)){ lost=chosen; chosen=''; }
      sel.value=chosen;
      hint.innerHTML=(lost ? `<span data-lost style="color:var(--danger)">Файла «${esc(lost)}», который был выбран, в каталоге импорта больше нет (его убрали, переименовали или закрыли для службы) — выберите файл заново.</span> ` : '')
        + (files.length
          ? `Файлы лежат на сервере в каталоге импорта ${code(dir)}. Дождитесь, пока файл скопируется на сервер целиком, — и только потом запускайте импорт.`
          : `В каталоге импорта на сервере пока нет файлов .pst и .ost. Скопируйте файл на сервер (например, командой <code>scp</code>), положите его в каталог ${code(dir)}${owner?` и отдайте его пользователю службы: ${code(`sudo chown ${shq(owner)}: ${shq(dir+'/файл.pst')}`)}`:''} — затем нажмите «Обновить список».`)
        + (locked.length && owner ? ` Файл с пометкой «нет доступа» служба прочитать не может — выполните на сервере: ${code(`sudo chown ${shq(owner)}: ${shq(dir+'/'+locked[0].name)}`)}${locked.length>1?' (и так же для остальных)':''}.` : '')
        + ' После импорта файл остаётся на месте — удалите его сами, когда он больше не нужен.' + refresh;
    }
    const again=hint.querySelector('[data-refresh]'); if(again) again.onclick=loadServerFiles;
    showUploadLimit(); showGo();
  };
  sel.onchange=sel.oninput=()=>{ chosen=sel.value; lost=''; const note=g('#i-srvhint [data-lost]'); if(note) note.remove(); showGo(); };
  async function loadServerFiles(){
    const seq=++loadSeq;
    let data=null, error='';
    try{ data=await api('/import-files'); }catch(e){ error=(e&&e.message)||String(e); }
    // Список обновляет только последний запрос: ответ на более ранний мог прийти позже
    // и вернуть в окно уже устаревший набор файлов.
    if(seq!==loadSeq) return;
    srv=data;
    if(srv && srv.max_upload_bytes) maxUpload=srv.max_upload_bytes;
    renderServerFiles(error);
  }
  g('#i-src').onchange=()=>{
    g('#i-uprow').style.display=fromServer()?'none':'block';
    g('#i-srvrow').style.display=fromServer()?'block':'none';
    showGo();
  };
  showUploadLimit();
  loadServerFiles();
  m.foot.querySelector('[data-c]').onclick=m.close;
  goBtn.onclick=async()=>{
    const server=fromServer();
    let f=null, serverFile='';
    if(server){
      serverFile=chosen=sel.value;
      if(!serverFile){
        // сам файл не выбирается никогда: что импортировать, решает только пользователь
        if(!srv) return toast('Список файлов на сервере не получен', 'Нажмите «Обновить список» под полем «Файл на сервере».', 'warn', 7000);
        if(srv.available===false) return toast('Каталог импорта на сервере недоступен', 'Причина и что с ней делать — под полем «Файл на сервере».', 'warn', 7000);
        if(!(srv.files||[]).length) return toast('В каталоге импорта нет файлов', 'Скопируйте файл .pst или .ost в каталог импорта на сервере и нажмите «Обновить список».', 'warn', 7000);
        return toast('Файл на сервере не выбран', 'Выберите файл в списке «Файл на сервере».', 'warn', 7000);
      }
    }else{
      f=g('#i-file').files[0]; if(!f) return toast('Выберите файл','','warn');
      // Не ждать загрузки гигабайтов ради отказа: предел известен заранее.
      if(f.size>maxUpload) return toast('Файл слишком большой', `Через браузер загружается не больше ${fmtBytes(maxUpload)}. Положите файл в каталог импорта на сервере и выберите «Взять с сервера».`, 'warn', 9000);
    }
    const fileName=server?serverFile:f.name;
    const target=g('#i-target').value, prefix=g('#i-prefix').value.trim();
    if(!prefix){
      // Пустой префикс — письма лягут прямо в рабочие папки ящика («Входящие» из
      // файла — во «Входящие» сотрудника или архива). Отменить это нельзя.
      const okGo=await confirmDlg('Импортировать без префикса?',
        `Папки из файла «${fileName}» будут созданы ${target==='local'?'в архиве ящика':'в ящике'} «${a.name}» под своими именами, а письма папок с совпадающими именами смешаются с ${target==='local'?'уже сохранёнными':'текущей почтой'}. Отменить это нельзя.`);
      if(!okGo) return;
    }
    // Файл с сервера передаётся именем: сам файл никуда не копируется.
    const fd=new FormData(); if(server) fd.append('server_file', serverFile); else fd.append('file', f);
    // «Без префикса» — отдельным полем: пустое значение сервер принял бы за «не указано».
    if(prefix) fd.append('target_prefix', prefix); else fd.append('no_prefix', '1');
    fd.append('target', target); fd.append('skip_existing', g('#i-skip').checked?'1':'0');
    fd.append('engine', g('#i-engine').value); fd.append('ansi_codepage', g('#i-cp').value);
    goBtn.disabled=true; goBtn.innerHTML=`<span class="spinner"></span> ${server?'Запуск…':'Загрузка…'}`;
    try{
      await api(`/accounts/${a.id}/import-pst`,{method:'POST',body:fd});
      toast('Импорт запущен', `${server?'Файл с сервера':'Файл'}: «${fileName}»`); m.close(); location.hash='#/jobs';
    }catch(e){
      // Без возврата кнопки диалог после ошибки приходилось закрывать и
      // открывать заново — кнопка оставалась серой со спиннером.
      toastErr(e); goBtn.disabled=false; showGo();
      // файл на сервере могли убрать или переименовать — список стоит перечитать
      // (выбор при этом остаётся прежним или сбрасывается на пустую строку)
      if(server) loadServerFiles();
    }
  };
}

