/* MailArchiver — веб-интерфейс: Графики, «Аналитика», «Ответы на письма», «Аналитика писем».
   Часть общего скрипта (порядок — mailarchiver/web/assets.py). */
"use strict";

// ===================================================================
//  Аналитика — набор графиков на canvas (без внешних библиотек)
// ===================================================================
const CHART_COLORS = ['#2f6fed','#1f9d55','#d98a00','#d64545','#2b8ca6','#7c5cff','#e0567f','#2bb0a6','#b0862e','#8e6bd8','#3aa0a0','#c76b3a'];
const STATUS_LBL = {queued:'в очереди',running:'выполняется',success:'успешно',failed:'ошибка',cancelled:'отменено',partial:'частично'};
// Задания, которые меняли почтовый сервер: «Повторить» у них нет — повтор вслепую изменил бы ещё раз и
// уже изменённые ящики. Их запускают заново из раздела, с проверкой.
const NO_RETRY_JOBS = new Set(['axigen_action','server_action']);
const JOBLBL = {backup:'Резервное копирование',restore:'Восстановление',export:'Экспорт',import_pst:'Импорт PST',test:'Проверка подключения',retention:'Очистка (ретеншн)',verify:'Проверка целостности',analyze:'Глубокий анализ писем',sync_employees:'Синхронизация сотрудников',storage_convert:'Шифрование копии',check_logins:'Проверка паролей',replicate:'Копия вне сервера',db_snapshot:'Снимок базы',search_index:'Индексация поиска',dedup_report:'Отчёт об одинаковых вложениях',cleanup:'Удаление архивов и прежних копий',backup_all:'Копирование всех ящиков по очереди',quarantine_check:'Сравнение прежней копии с новой',quarantine_rescue:'Возврат писем из прежней копии',folders_check:'Проверка папок на сервере',search_reindex:'Переиндексация поиска',axigen_sync:'Сверка с сервером Axigen',axigen_action:'Действия на сервере Axigen',server_action:'Действия с ящиками на сервере',server_sync:'Сверка с почтовым сервером',reply_index:'Индекс ответов на письма',case_export:'Выгрузка дела',search_attach:'Чтение текста вложений',restore_drill:'Проверка восстановления'};
function cvar(n,f){ const v=getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v||f; }
function fmtNum(n){ return Number(n||0).toLocaleString('ru-RU'); }
function trunc(s,n){ s=String(s==null?'':s); return s.length>n? s.slice(0,n-1)+'…':s; }
function hexA(hex,a){ hex=String(hex).replace('#',''); if(hex.length===3)hex=hex.split('').map(c=>c+c).join(''); const r=parseInt(hex.slice(0,2),16),g=parseInt(hex.slice(2,4),16),b=parseInt(hex.slice(4,6),16); return `rgba(${r||0},${g||0},${b||0},${a})`; }
function niceStep(v){ const p=Math.pow(10,Math.floor(Math.log10(v))); const n=v/p; let m; if(n<=1)m=1;else if(n<=2)m=2;else if(n<=2.5)m=2.5;else if(n<=5)m=5;else m=10; return m*p; }
/** Верх оси для 4 делений с «круглым» ЦЕЛЫМ шагом: при малых значениях
 *  прежний расчёт давал подписи «1, 1, 1, 0» (0,75 и 0,5 округлялись до 1). */
function niceMax(v){ v=Math.max(1,v); const step=Math.max(1, Math.ceil(niceStep(v/4))); return step*4; }
function rr(ctx,x,y,w,h,r){ if(h<0){y+=h;h=-h;} r=Math.max(0,Math.min(r,h/2,w/2)); ctx.beginPath(); ctx.moveTo(x+r,y); ctx.arcTo(x+w,y,x+w,y+h,r); ctx.arcTo(x+w,y+h,x,y+h,r); ctx.arcTo(x,y+h,x,y,r); ctx.arcTo(x,y,x+w,y,r); ctx.closePath(); }
function cEmpty(ctx,W,H){ ctx.fillStyle=cvar('--text-dim','#888'); ctx.textAlign='center'; ctx.font='13px sans-serif'; ctx.fillText('Нет данных',W/2,H/2); }
function drawInto(container, height, fn){
  container.innerHTML=''; const cv=document.createElement('canvas'); container.appendChild(cv);
  const dpr=window.devicePixelRatio||1;
  const cssW=Math.max(220, container.clientWidth||container.parentElement.clientWidth||600), cssH=height;
  cv.style.width='100%'; cv.style.height=cssH+'px'; cv.width=Math.round(cssW*dpr); cv.height=Math.round(cssH*dpr);
  const ctx=cv.getContext('2d'); ctx.scale(dpr,dpr);
  fn(ctx, cssW, cssH);
}
function chartVBars(container, items, opts={}){
  const H=opts.height||220, color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const padL=40,padR=10,padT=12,padB=opts.rotate?52:26, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=niceMax(Math.max(1,...items.map(d=>d.value)));
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(fmtNum(Math.round(max*(4-i)/4)),padL-5,y+3); }
    const bw=plotW/n;
    items.forEach((d,i)=>{
      const bh=plotH*(d.value/max), x=padL+i*bw+bw*0.15, w=Math.max(1,bw*0.7), y=padT+plotH-bh;
      ctx.fillStyle=opts.colorByIndex?CHART_COLORS[i%CHART_COLORS.length]:color; rr(ctx,x,y,w,bh,3); ctx.fill();
      ctx.fillStyle=dim; ctx.textAlign='center'; ctx.font='10px sans-serif';
      if(opts.rotate){ ctx.save(); ctx.translate(x+w/2,H-padB+11); ctx.rotate(-Math.PI/4); ctx.textAlign='right'; ctx.fillText(trunc(d.label,10),0,0); ctx.restore(); }
      else { const step=Math.max(1,Math.ceil(n/(W>560?24:8))); if(i%step===0||n<=12) ctx.fillText(d.label,x+w/2,H-padB+14); }
    });
  });
}
function chartHBars(container, items, opts={}){
  const rowH=opts.rowH||27;
  // Потолок высоты: холст выше ~8000 CSS-px после умножения на devicePixelRatio
  // упирается в ограничение браузера и не рисуется вовсе.
  const H=Math.min(8000, opts.height||(items.length*rowH+14));
  const color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const dim=cvar('--text-dim','#888'), txt=cvar('--text','#222');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const max=Math.max(1,...items.map(d=>d.value)); const chars=opts.labelChars||24;
    ctx.font='12px sans-serif'; let labelW=0; items.forEach(d=>labelW=Math.max(labelW,ctx.measureText(trunc(d.label,chars)).width));
    labelW=Math.min(labelW+6, opts.labelW||210); const valW=opts.valW||52, barX=labelW+8, barW=Math.max(20,W-barX-valW-4);
    items.forEach((d,i)=>{
      const y=7+i*rowH, bh=rowH*0.6, by=y+(rowH-bh)/2;
      ctx.fillStyle=dim; ctx.textAlign='left'; ctx.font='12px sans-serif'; ctx.fillText(trunc(d.label,chars),0,by+bh*0.72);
      ctx.fillStyle=opts.colorByIndex?CHART_COLORS[i%CHART_COLORS.length]:color; const w=Math.max(2,barW*(d.value/max)); rr(ctx,barX,by,w,bh,3); ctx.fill();
      ctx.fillStyle=txt; ctx.font='11px sans-serif'; ctx.textAlign='left'; ctx.fillText(opts.fmt?opts.fmt(d):fmtNum(d.value), barX+w+5, by+bh*0.72);
    });
  });
}
function chartLine(container, items, opts={}){
  const H=opts.height||230, color=opts.color||cvar('--primary','#2f6fed');
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const padL=44,padR=12,padT=12,padB=28, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=niceMax(Math.max(1,...items.map(d=>d.value)));
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(opts.yfmt?opts.yfmt(max*(4-i)/4):fmtNum(Math.round(max*(4-i)/4)),padL-5,y+3); }
    const xat=i=> n<=1?padL+plotW/2:padL+plotW*i/(n-1), yat=v=>padT+plotH*(1-v/max);
    ctx.beginPath(); ctx.moveTo(xat(0),yat(items[0].value)); items.forEach((d,i)=>ctx.lineTo(xat(i),yat(d.value))); ctx.lineTo(xat(n-1),padT+plotH); ctx.lineTo(xat(0),padT+plotH); ctx.closePath(); ctx.fillStyle=color; ctx.globalAlpha=.13; ctx.fill(); ctx.globalAlpha=1;
    ctx.beginPath(); items.forEach((d,i)=>{ const x=xat(i),y=yat(d.value); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }); ctx.strokeStyle=color; ctx.lineWidth=2; ctx.stroke();
    if(n<=40){ items.forEach((d,i)=>{ ctx.beginPath(); ctx.arc(xat(i),yat(d.value),2.4,0,7); ctx.fillStyle=color; ctx.fill(); }); }
    ctx.fillStyle=dim; ctx.font='10px sans-serif'; const step=Math.max(1,Math.ceil(n/(W>620?12:6)));
    items.forEach((d,i)=>{ if(i%step===0||i===n-1){ ctx.textAlign = i===0?'left':(i===n-1?'right':'center'); ctx.fillText(d.label,xat(i),H-padB+15); } });
  });
}
function chartDonut(container, slices, opts={}){
  const H=opts.height||200;
  drawInto(container,H,(ctx,W)=>{
    const total=slices.reduce((s,d)=>s+(d.value||0),0);
    const cx=W/2, cy=H/2, r=Math.min(W,H)/2-10, ri=r*0.6;
    if(!total){ cEmpty(ctx,W,H); return; }
    let a=-Math.PI/2;
    slices.forEach((d,i)=>{ const ang=2*Math.PI*(d.value||0)/total; if(ang>0){ ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,r,a,a+ang); ctx.closePath(); ctx.fillStyle=d.color||CHART_COLORS[i%CHART_COLORS.length]; ctx.fill(); } a+=ang; });
    ctx.beginPath(); ctx.arc(cx,cy,ri,0,7); ctx.fillStyle=cvar('--bg-elev','#fff'); ctx.fill();
    ctx.fillStyle=cvar('--text','#222'); ctx.textAlign='center'; ctx.font='700 19px sans-serif'; ctx.fillText(fmtNum(total),cx,cy+1);
    ctx.fillStyle=cvar('--text-dim','#888'); ctx.font='11px sans-serif'; ctx.fillText(opts.centerLabel||'всего',cx,cy+17);
  });
}
function donutLegend(slices){ const tot=slices.reduce((s,d)=>s+(d.value||0),0)||1; return `<div class="an-legend">${slices.map((d,i)=>`<span class="lg"><span class="dot" style="background:${d.color||CHART_COLORS[i%CHART_COLORS.length]}"></span>${esc(d.label)} <b>${fmtNum(d.value)}</b> · ${Math.round(100*(d.value||0)/tot)}%</span>`).join('')}</div>`; }
function chartHeat(container, matrix, rowLabels, opts={}){
  const rows=matrix.length, cols=(matrix[0]||[]).length, H=opts.height||(rows*22+30);
  drawInto(container,H,(ctx,W)=>{
    const dim=cvar('--text-dim','#888'), base=opts.color||cvar('--primary','#2f6fed');
    if(!rows||!cols){ cEmpty(ctx,W,H); return; }
    const padL=34,padT=6,padB=16, gw=(W-padL-6)/cols, gh=(H-padT-padB)/rows;
    let max=opts.max||1; matrix.forEach(r=>r.forEach(v=>{ if(v>max)max=v; }));
    for(let r=0;r<rows;r++){
      for(let c=0;c<cols;c++){ const v=matrix[r][c]; ctx.fillStyle= v? hexA(base,0.14+0.86*(v/max)) : cvar('--bg-soft','#eee'); ctx.fillRect(padL+c*gw+1,padT+r*gh+1,gw-2,gh-2); }
      ctx.fillStyle=dim; ctx.textAlign='right'; ctx.font='10px sans-serif'; ctx.fillText(rowLabels[r],padL-4,padT+r*gh+gh*0.66);
    }
    ctx.fillStyle=dim; ctx.textAlign='center'; ctx.font='9px sans-serif';
    for(let c=0;c<cols;c+=3){ ctx.fillText(String(c).padStart(2,'0'),padL+c*gw+gw/2,H-4); }
  });
}
function wordCloud(items, opts={}){
  if(!items.length) return '<div class="an-empty-hint">Нет данных</div>';
  const max=Math.max(...items.map(w=>w.value)), min=Math.min(...items.map(w=>w.value));
  const lo=opts.min||13, hi=opts.max||30;
  return `<div class="an-cloud">${items.map((w,i)=>{ const t=max===min?1:(w.value-min)/(max-min); const sz=(lo+(hi-lo)*t).toFixed(1); const col=CHART_COLORS[i%CHART_COLORS.length]; return `<span class="w" title="${fmtNum(w.value)}" style="font-size:${sz}px;color:${hexA(col,0.55+0.45*t)};font-weight:${400+Math.round(t*3)*100}">${esc(w.label)}</span>`; }).join('')}</div>`;
}
// реестр перерисовки при resize/смене темы
function registerCharts(list){ State.redraw=()=>list.forEach(fn=>{ try{fn();}catch(e){} }); }

// ===================================================================
//  Раздел «Аналитика» (система)
// ===================================================================
async function viewAnalytics(c){
  const accs = (State.accounts&&State.accounts.length)?State.accounts:await api('/accounts'); State.accounts=accs;
  const scope=State.anScope||''; const days=State.anDays||90;
  let d; try{ d=await api('/analytics/system'+(scope?('?account_id='+scope+'&days='+days):('?days='+days))); }catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить аналитику</div></div>'; return; }
  c.innerHTML='';
  // Панель управления
  const bar=h(`<div class="an-toolbar">
    <label class="muted small">Область:</label>
    <select id="anScope"><option value="">Все ящики</option>${accs.map(a=>`<option value="${a.id}" ${String(a.id)===String(scope)?'selected':''}>${esc(a.name)}</option>`).join('')}</select>
    <label class="muted small">Период активности:</label>
    <select id="anDays">${[[30,'30 дней'],[90,'90 дней'],[180,'полгода'],[365,'год']].map(([v,t])=>`<option value="${v}" ${v===days?'selected':''}>${t}</option>`).join('')}</select>
    <span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" id="anRefresh">↻ Обновить</button></div>`);
  c.appendChild(bar);
  $('#anScope',bar).onchange=e=>{ State.anScope=e.target.value; viewAnalytics(c); };
  $('#anDays',bar).onchange=e=>{ State.anDays=parseInt(e.target.value); viewAnalytics(c); };
  $('#anRefresh',bar).onclick=()=>viewAnalytics(c);

  const o=d.overview;
  const kpi=(label,val,sub,accent)=>`<div class="kpi ${accent?'accent':''}"><div class="k-label">${label}</div><div class="k-value">${val}</div><div class="k-sub">${sub||''}</div></div>`;
  c.appendChild(h(`<div class="an-kpis">
    ${kpi('✉️ Писем в архиве',fmtNum(o.messages),o.bytes_h,true)}
    ${kpi('💾 Объём копий',o.bytes_h,'ср. письмо '+o.avg_message_h)}
    ${kpi('📬 Ящиков',o.accounts_total,'активных: '+o.accounts_enabled)}
    ${kpi('🗂️ Папок',fmtNum(o.folders),'уникальных')}
    ${kpi('⚙️ Заданий',fmtNum(o.jobs_total),o.jobs_success_rate!=null?('успех '+o.jobs_success_rate+'%'):'—')}
    ${kpi('🔄 Прогонов',fmtNum(d.runs.total),'нов. писем '+fmtNum(d.runs.messages_new))}
    ${kpi('📤 Экспортов',fmtNum(o.exports),d.exports.bytes_h)}
    ${kpi('⏰ Расписаний',fmtNum(o.schedules),'вкл: '+o.schedules_enabled)}
    ${kpi('💽 Свободно',o.disk_free_h,'на диске')}
    ${kpi('🗄️ База',o.db_size_h,'файл БД')}
    ${kpi('👥 Пользователей',fmtNum(o.users),'сессий: '+o.active_sessions)}
    ${kpi('🚦 Планировщик',o.scheduler_running?'вкл':'выкл','воркеров: '+o.workers)}
  </div>`));

  const redraws=[];
  const reg=(fn)=>{ fn(); redraws.push(fn); };

  // Активность по дням (с переключателем метрики)
  const actCard=h(`<div class="card an-card"><h3>📈 Активность по дням <span class="h-sub">— последние ${d.activity.length} дн.</span><span class="spacer" style="flex:1"></span>
    <span class="btn-row" id="actMetric" style="gap:6px"></span></h3><div class="an-chart" id="actChart"></div></div>`);
  c.appendChild(actCard);
  const METRICS=[['messages','Письма',cvar('--primary','#2f6fed')],['bytes','Объём',cvar('--info','#2b8ca6')],['jobs','Задания',cvar('--success','#1f9d55')],['errors','Ошибки',cvar('--danger','#d64545')]];
  let actMetric='messages';
  const mbtns=$('#actMetric',actCard);
  METRICS.forEach(([k,t])=>{ const b=h(`<button class="btn sm ${k==='messages'?'primary':''}" data-m="${k}">${t}</button>`); b.onclick=()=>{ actMetric=k; mbtns.querySelectorAll('button').forEach(x=>x.className='btn sm'); b.className='btn sm primary'; drawAct(); }; mbtns.appendChild(b); });
  const drawAct=()=>{ const meta=METRICS.find(m=>m[0]===actMetric); const isB=actMetric==='bytes';
    const items=d.activity.map(a=>({label:(a.day||'').slice(5), value:isB?(a.bytes/1048576):a[actMetric]}));
    chartLine($('#actChart',actCard), items, {height:230, color:meta[2], yfmt:isB?(v=>v.toFixed(0)+'М'):null}); };
  reg(drawAct);

  // Задания: статусы (donut) + типы (hbars)
  const jobsCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⚙️ Задания по статусам</h3><div class="an-chart" id="jobStatus"></div><div id="jobStatusLeg"></div></div>
    <div class="card"><h3>🧩 Задания по типам</h3><div class="an-chart" id="jobType"></div></div></div>`);
  c.appendChild(jobsCard);
  const jStatus=d.jobs.by_status.map((s,i)=>({label:s.label,value:s.value,color:({success:cvar('--success','#1f9d55'),failed:cvar('--danger','#d64545'),running:cvar('--primary','#2f6fed'),queued:cvar('--text-dim','#888'),partial:cvar('--warn','#d98a00'),cancelled:cvar('--warn','#d98a00')})[s.key]||CHART_COLORS[i]}));
  reg(()=>{ chartDonut($('#jobStatus',jobsCard), jStatus, {centerLabel:'заданий'}); $('#jobStatusLeg',jobsCard).innerHTML=donutLegend(jStatus); });
  reg(()=>chartHBars($('#jobType',jobsCard), d.jobs.by_type, {colorByIndex:true, height:Math.max(90,d.jobs.by_type.length*30)}));

  // Длительность + прогоны
  if(d.jobs.durations.length){
    const durRows=d.jobs.durations.map(x=>`<tr><td>${esc(x.type_label)}</td><td>${x.count}</td><td>${x.avg_s} с</td><td>${x.max_s} с</td></tr>`).join('');
    c.appendChild(h(`<div class="card an-card"><h3>⏱️ Длительность заданий</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Тип</th><th>Кол-во</th><th>Средняя</th><th>Максимум</th></tr></thead><tbody>${durRows}</tbody></table></div></div>`));
  }

  // Экспорт: форматы (donut) + движки (hbars); восстановление
  if(d.exports.total){
    const exCard=h(`<div class="grid cols-2 an-card">
      <div class="card"><h3>📤 Экспорт по форматам</h3><div class="an-chart" id="exFmt"></div><div id="exFmtLeg"></div></div>
      <div class="card"><h3>🔧 Экспорт по движкам</h3><div class="an-chart" id="exEng"></div></div></div>`);
    c.appendChild(exCard);
    const exF=d.exports.by_format.map((x,i)=>({label:x.label,value:x.value}));
    reg(()=>{ chartDonut($('#exFmt',exCard),exF,{centerLabel:'файлов'}); $('#exFmtLeg',exCard).innerHTML=donutLegend(exF); });
    reg(()=>chartHBars($('#exEng',exCard),d.exports.by_engine,{colorByIndex:true,height:Math.max(80,d.exports.by_engine.length*30)}));
  }

  // Хранилище по ящикам
  if(d.accounts.length){
    const rows=d.accounts.map(a=>`<tr><td><strong>${esc(a.name)}</strong>${a.enabled?'':' <span class="tag">выкл</span>'}</td><td>${fmtNum(a.messages)}</td><td>${esc(a.bytes_h)}</td><td>${a.folders}</td><td>${a.last_run?`<span class="badge ${esc(a.last_run.status)}">${esc(STATUS_LBL[a.last_run.status]||a.last_run.status)}</span>`:'<span class="muted small">—</span>'}</td></tr>`).join('');
    const stCard=h(`<div class="grid cols-2 an-card">
      <div class="card"><h3>🗄️ Хранилище по ящикам</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Ящик</th><th>Писем</th><th>Объём</th><th>Папок</th><th>Последний</th></tr></thead><tbody>${rows}</tbody></table></div></div>
      <div class="card"><h3>📊 Писем по ящикам</h3><div class="an-chart" id="accBars"></div></div></div>`);
    c.appendChild(stCard);
    // Только первые 20 ящиков: на 500 холст вырастал до 16 000 CSS-пикселей
    // (с учётом devicePixelRatio — за предел canvas в Safari, график просто
    // не рисовался). Полный список всё равно есть в разделе «Почтовые ящики».
    const accTop=[...d.accounts].sort((a,b)=>(b.messages||0)-(a.messages||0)).slice(0,20);
    reg(()=>chartHBars($('#accBars',stCard), accTop.map(a=>({label:a.name,value:a.messages})), {colorByIndex:true, height:Math.max(90,accTop.length*32), labelChars:20}));
    if(d.accounts.length>accTop.length){
      stCard.appendChild(h(`<div class="muted small">Показаны 20 самых крупных ящиков из ${fmtNum(d.accounts.length)}.</div>`));
    }
  }

  // Одинаковые вложения: отчёт догружается отдельно (его подсчёт — фоновое задание).
  // Он читает письма — только администратору.
  if(!scope && isAdmin()){
    const ddWrap=h('<div id="dedupWrap"></div>'); c.appendChild(ddWrap);
    loadDedup(ddWrap, redraws);
  }

  // Расписания и аудит
  const botGrid=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⏰ Ближайшие запуски</h3><div id="upNext"></div></div>
    <div class="card"><h3>🛡️ Топ действий (аудит)</h3><div class="an-chart" id="auditBars"></div></div></div>`);
  c.appendChild(botGrid);
  const up=d.schedules.upcoming;
  $('#upNext',botGrid).innerHTML = up.length? `<table class="an-mini-table">${up.map(u=>`<tr><td>${u.account_id?esc(accName(u.account_id)||('#'+u.account_id)):'все ящики по очереди'}</td><td>${esc(JOBLBL[u.job_type]||u.job_type)}</td><td>${fmtDate(u.next_run)}</td></tr>`).join('')}</table>` : '<div class="an-empty-hint">Нет включённых расписаний</div>';
  if(d.audit_top.length) reg(()=>chartHBars($('#auditBars',botGrid), d.audit_top, {colorByIndex:true, labelChars:22, height:Math.max(80,d.audit_top.length*24)}));
  else $('#auditBars',botGrid).innerHTML='<div class="an-empty-hint">Нет записей аудита</div>';

  registerCharts(redraws);
}

// ---- Отчёт «Одинаковые вложения» (сколько места сэкономит хранение одной копии) ----
async function loadDedup(wrap, redraws){
  let d; try{ d=await api('/analytics/dedup',{bg:true}); }catch(e){ return; }
  if(!wrap.isConnected) return;
  const r=d.report, st=d.status, job=(d.running||[])[0];
  wrap.innerHTML='';
  const card=h(`<div class="card an-card">
    <div class="section-title" style="flex-wrap:wrap;gap:8px"><h3 style="margin:0">🧬 Одинаковые вложения <span class="h-sub">— сколько места освободится, если хранить одну копию</span></h3>
      <span class="spacer" style="flex:1"></span>
      ${r&&!job?'<button class="btn sm ghost" id="ddFull" title="Забыть прочитанное и прочитать весь архив заново">Посчитать заново</button>':''}
      <button class="btn sm primary" id="ddRun" ${job?'disabled':''}>${job?'<span class="spinner"></span> Идёт подсчёт…':(r?'↻ Обновить':'▶ Посчитать')}</button></div>
    <div class="muted small" style="margin-bottom:10px">Рассылки всем сотрудникам, логотипы в подписях, пересланные по цепочке договоры лежат в каждом письме отдельной копией. Отчёт читает письма и находит вложения с одинаковым содержимым (имя файла может отличаться). <b>Формат хранения не меняется</b> — это только подсчёт.</div>
    <div id="ddProgress"></div><div id="ddBody"></div></div>`);
  wrap.appendChild(card);
  const start=async(full)=>{
    if(full && !await confirmDlg('Посчитать заново?','Прочитанное забудется, и весь архив будет прочитан снова — на большом архиве это часы (копирования ящиков при этом идут своим чередом).',{okText:'Посчитать заново', okClass:'primary'})) return;
    try{ const res=await api('/analytics/dedup/scan',{method:'POST',body:{full:!!full}});
      toast('Подсчёт запущен', res.already_running?'Уже выполняется':'Письма читаются в фоне; отчёт обновится здесь сам.');
      loadDedup(wrap, redraws);
    }catch(e){ toastErr(e); }
  };
  $('#ddRun',card).onclick=()=>start(false);
  const fb=$('#ddFull',card); if(fb) fb.onclick=()=>start(true);
  const prog=$('#ddProgress',card);
  const drawProgress=(s)=>{
    const pct=s.total?Math.floor(100*s.scanned/s.total):100;
    prog.innerHTML=`<div class="progress running" style="margin:4px 0 6px"><span style="width:${pct}%"></span></div>
      <div class="muted small" style="margin-bottom:10px">Прочитано писем: ${fmtNum(s.scanned)} из ${fmtNum(s.total)} (${pct}%) · учтено вложений: ${fmtNum(s.attachments)}. Большой архив читается частями — между ними идут копирования ящиков.</div>`;
  };
  if(job){
    drawProgress(st);
    const t=setInterval(async()=>{
      if(!wrap.isConnected || State.view!=='analytics'){ clearInterval(t); return; }
      let x; try{ x=await api('/analytics/dedup',{bg:true}); }catch(e){ clearInterval(t); return; }
      if(!(x.running||[]).length){ clearInterval(t); loadDedup(wrap, redraws); return; }
      drawProgress(x.status);
    }, 4000);
  }
  const body=$('#ddBody',card);
  if(!r){
    body.innerHTML=job?'':`<div class="an-empty-hint">Отчёт ещё не считался. Нажмите «Посчитать»: первый подсчёт читает весь архив (на большом архиве — часы), следующие — только новые письма.</div>`;
    return;
  }
  renderDedup(body, r, st, redraws);
}

function renderDedup(body, r, st, redraws){
  const s=r.savings, a=r.attachments, cov=r.coverage;
  const kpi=(l,v,sub,ac)=>`<div class="kpi ${ac?'accent':''}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${sub||''}</div></div>`;
  const notes=[];
  if(!cov.complete) notes.push(`⏳ Прочитано ${cov.percent}% писем (${fmtNum(cov.scanned)} из ${fmtNum(cov.total)}) — цифры вырастут, когда подсчёт дойдёт до конца.`);
  else if(st && st.pending>0) notes.push(`С момента отчёта в архив добавилось писем: ${fmtNum(st.pending)} — нажмите «Обновить», чтобы учесть их (прочитаются только новые).`);
  if(s.separate_store>s.bytes) notes.push(`Если хранить вложения отдельно от писем и без кодировки base64 (она увеличивает файлы примерно на треть), освободится до <b>${esc(s.separate_store_h)}</b> (${s.separate_store_percent}% архива).`);
  if(r.compressed) notes.push('Сжатие копии включено — на диске экономия будет примерно на четверть меньше указанной.');
  if(r.whole_messages && r.whole_messages.copies) notes.push(`Писем-двойников целиком: ${fmtNum(r.whole_messages.copies)} (${esc(r.whole_messages.bytes_h)}) — одно и то же письмо лежит в нескольких папках; их вложения уже учтены выше.`);
  if(cov.errors) notes.push(`Не удалось прочитать писем: ${fmtNum(cov.errors)} (подробности — в событиях задания в разделе «Задания»).`);
  notes.push(`Вложения меньше ${esc(r.min_size_h)} не учитываются. Отчёт от ${esc(fmtDate(r.generated_at))}.`);
  const when=(t)=>{ const a=fmtDateShort(t.first), z=fmtDateShort(t.last); return a&&z&&a!==z?a+' — '+z:(a||z); };
  const top=(r.top||[]).map((t,i)=>`<tr>
      <td><strong>${esc(trunc(t.filename,48))}</strong>${t.names>1?`<div class="muted small">и ещё ${t.names-1} ${plural(t.names-1,'имя','имени','имён')}</div>`:''}</td>
      <td class="small" title="${esc(t.ctype||'')}">${esc(trunc(t.type_label,28))}</td><td class="nowrap">${esc(t.size_h)}</td><td>${fmtNum(t.copies)}</td><td>${fmtNum(t.mailboxes)}</td>
      <td class="nowrap"><strong>${esc(t.wasted_h)}</strong></td>
      <td class="small muted">${esc(when(t))}</td>
      <td>${t.example?`<button class="btn ghost sm" data-ex="${i}" title="Открыть одно из писем с этим вложением">✉️</button>`:''}</td></tr>`).join('');
  const box=h(`<div>
    <div class="an-kpis">
      ${kpi('💾 Освободится',esc(s.bytes_h),s.percent_of_archive+'% архива',true)}
      ${kpi('📎 Вложения',esc(a.bytes_h),fmtNum(a.count)+' шт. · '+a.percent_of_archive+'% архива')}
      ${kpi('🔁 Лишних копий',fmtNum(r.duplicates.copies),fmtNum(r.duplicates.groups)+' '+plural(r.duplicates.groups||0,'файл','файла','файлов')+' повторяются')}
      ${kpi('📬 Только внутри ящиков',esc(s.within_mailbox_h),'если не объединять копии разных сотрудников')}
    </div>
    <div class="muted small" style="margin:6px 0 12px;line-height:1.55">${notes.map(n=>'<div>'+n+'</div>').join('')}</div>
    ${top?`<h4 style="margin:6px 0">Чаще всего повторяются</h4><div class="table-wrap"><table class="tbl"><thead><tr><th>Файл</th><th>Тип</th><th class="nowrap">Размер</th><th class="nowrap">Копий</th><th class="nowrap">Ящиков</th><th class="nowrap">Лишнее место</th><th>Когда</th><th></th></tr></thead><tbody>${top}</tbody></table></div>`:'<div class="an-empty-hint">Одинаковых вложений не найдено.</div>'}
    ${top?`<div class="grid cols-2" style="margin-top:12px">
      <div><h4 style="margin:6px 0">Лишнее место по типам файлов</h4><div class="an-chart" id="ddTypes"></div></div>
      <div><h4 style="margin:6px 0">Ящики с повторяющимися вложениями</h4><div class="an-chart" id="ddBoxes"></div></div></div>`:''}
  </div>`);
  body.innerHTML=''; body.appendChild(box);
  box.querySelectorAll('[data-ex]').forEach(b=>{ b.onclick=()=>{ const ex=r.top[parseInt(b.dataset.ex)].example; openMailMessage(ex.account_id, ex.message_id, ex.folder); }; });
  if(top){
    const add=(fn)=>{ fn(); redraws.push(fn); };
    add(()=>chartHBars($('#ddTypes',box), r.by_type, {colorByIndex:true, labelChars:22, valW:70, fmt:x=>x.value_h, height:Math.max(80,r.by_type.length*27+14)}));
    add(()=>chartHBars($('#ddBoxes',box), r.by_account, {colorByIndex:true, labelChars:22, valW:70, fmt:x=>x.value_h, height:Math.max(80,r.by_account.length*27+14)}));
    registerCharts(redraws);
  }
}

// ===================================================================
//  Раздел «Ответы на письма» (1.16.0): кто, как быстро и на что отвечает
// ===================================================================
// Фильтры раздела живут, пока открыта вкладка: возврат в раздел показывает тот же отбор.
const RP = {period:'30', from:'', to:'', acc:'', dep:'', scope:'all', kinds:'0', addressed:'all', incoming:'all',
  sla:'', slaBiz:'', heat:'count', accSort:'count', accDir:-1, accText:'', peerTab:'senders', domTab:'domains',
  list:'overdue', order:'old', text:'', meta:null};
const RP_PERIODS=[['7','7 дней'],['30','30 дней'],['90','90 дней'],['180','полгода'],['365','год'],['0','всё время'],['custom','свой период…']];
const RP_KINDS=[['0','Только личные'],['0,4','Личные и исключённые'],['0,2','Личные и рассылки'],['0,1,2,3,4','Все письма']];

/** Длительность по-человечески: «2 ч 15 мин», «3 д 4 ч». */
function fmtDur(s){
  if(s==null || isNaN(s)) return '—';
  s=Math.max(0,Math.round(s));
  if(s<60) return s+' с';
  if(s<3600) return Math.round(s/60)+' мин';
  if(s<86400){ const hh=Math.floor(s/3600), mm=Math.round((s%3600)/60); return hh+' ч'+(mm?' '+mm+' мин':''); }
  const d=Math.floor(s/86400), hh=Math.round((s%86400)/3600); return d+' д'+(hh?' '+hh+' ч':'');
}
function fmtPct(v){ return v==null?'—':(Number(v).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%'); }
/** Изменение к прошлому периоду: стрелка и цвет — «хорошо» или «плохо» (better: 'up' | 'down'). */
function rpDelta(cur, prev, better, kind){
  if(cur==null || prev==null) return '';
  const diff=cur-prev; if(Math.abs(diff)<1e-9) return '<span class="rp-delta same">= как в прошлом периоде</span>';
  const good = better==='up' ? diff>0 : diff<0;
  const cls = better==='none' ? 'same' : (good?'good':'bad');
  let txt;
  if(kind==='pct') txt=(diff>0?'+':'−')+Math.abs(diff).toLocaleString('ru-RU',{maximumFractionDigits:1})+' п.п.';
  else if(kind==='dur') txt=(diff>0?'+':'−')+fmtDur(Math.abs(diff));
  else { const rel=prev? Math.round(100*diff/prev):null; txt=(diff>0?'+':'−')+fmtNum(Math.abs(diff))+(rel!=null&&isFinite(rel)?` (${diff>0?'+':'−'}${Math.abs(rel)}%)`:''); }
  return `<span class="rp-delta ${cls}" title="К прошлому такому же периоду">${diff>0?'▲':'▼'} ${txt}</span>`;
}
function rpParams(extra){
  const p=new URLSearchParams();
  if(RP.period==='custom'){ if(RP.from) p.set('date_from',RP.from); if(RP.to) p.set('date_to',RP.to); }
  else p.set('days', RP.period);
  if(RP.acc) p.set('account_id', RP.acc);
  if(RP.dep && !RP.acc) p.set('department', RP.dep);
  p.set('kinds', RP.kinds); p.set('scope', RP.scope); p.set('addressed', RP.addressed); p.set('incoming', RP.incoming);
  if(RP.sla) p.set('sla', RP.sla);
  if(RP.slaBiz!=='') p.set('sla_business', RP.slaBiz);
  Object.entries(extra||{}).forEach(([k,v])=>p.set(k,v));
  return p.toString();
}

// ---- графики раздела: составные столбцы, несколько линий, подсказки при наведении ----
function chartTipAttach(container, find){
  const cv=container.querySelector('canvas'); if(!cv) return;
  let tip=container.querySelector('.chart-tip');
  if(!tip){ tip=document.createElement('div'); tip.className='chart-tip'; tip.hidden=true; container.appendChild(tip); }
  cv.onmousemove=(e)=>{
    const r=cv.getBoundingClientRect(); const html=find(e.clientX-r.left, e.clientY-r.top);
    if(!html){ tip.hidden=true; return; }
    tip.innerHTML=html; tip.hidden=false;
    const w=tip.offsetWidth, x=e.clientX-r.left;
    tip.style.left=Math.max(0,Math.min(r.width-w, x+12))+'px'; tip.style.top=Math.max(0,e.clientY-r.top-tip.offsetHeight-10)+'px';
  };
  cv.onmouseleave=()=>{ tip.hidden=true; };
}
/** Составные столбцы: items — [{label, values:[…]}], series — [{name, color}]. */
function chartStack(container, items, series, opts={}){
  const H=opts.height||240;
  let geo=null;
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=items.length; if(!n){ cEmpty(ctx,W,H); return; }
    const padL=44,padR=10,padT=12,padB=28, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=niceMax(Math.max(1,...items.map(d=>d.values.reduce((a,b)=>a+(b||0),0))));
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(fmtNum(Math.round(max*(4-i)/4)),padL-5,y+3); }
    const bw=plotW/n;
    items.forEach((d,i)=>{
      let y=padT+plotH; const x=padL+i*bw+bw*0.14, w=Math.max(1,bw*0.72);
      d.values.forEach((v,j)=>{ if(!v) return; const bh=plotH*(v/max); y-=bh; ctx.fillStyle=series[j].color; ctx.fillRect(x,y,w,bh); });
      ctx.fillStyle=dim; ctx.textAlign='center'; ctx.font='10px sans-serif';
      const step=Math.max(1,Math.ceil(n/(W>620?16:7))); if(i%step===0||n<=10) ctx.fillText(d.label,x+w/2,H-padB+14);
    });
    geo={padL,bw,n};
  });
  chartTipAttach(container,(x)=>{ if(!geo) return ''; const i=Math.floor((x-geo.padL)/geo.bw); if(i<0||i>=geo.n) return '';
    const d=items[i]; const tot=d.values.reduce((a,b)=>a+(b||0),0);
    return `<b>${esc(d.title||d.label)}</b> · ${fmtNum(tot)}<br>`+series.map((s,j)=>`<span class="dot" style="background:${s.color}"></span>${esc(s.name)}: <b>${fmtNum(d.values[j]||0)}</b>`).join('<br>')+(d.extra?'<br>'+d.extra:''); });
}
/** Несколько линий: labels — подписи оси X, series — [{name, color, values:[…], dash}]. */
function chartMulti(container, labels, series, opts={}){
  const H=opts.height||230, yfmt=opts.yfmt||(v=>fmtNum(Math.round(v)));
  let geo=null;
  drawInto(container,H,(ctx,W)=>{
    const grid=cvar('--border','#ddd'), dim=cvar('--text-dim','#888');
    const n=labels.length; const all=series.flatMap(s=>s.values.filter(v=>v!=null));
    if(!n || !all.length){ cEmpty(ctx,W,H); return; }
    const padL=54,padR=12,padT=12,padB=28, plotW=W-padL-padR, plotH=H-padT-padB;
    const max=opts.max || (Math.max(...all)*1.08 || 1);
    ctx.font='11px sans-serif'; ctx.lineWidth=1;
    for(let i=0;i<=4;i++){ const y=padT+plotH*i/4; ctx.strokeStyle=grid; ctx.globalAlpha=.55; ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(W-padR,y); ctx.stroke(); ctx.globalAlpha=1; ctx.fillStyle=dim; ctx.textAlign='right'; ctx.fillText(yfmt(max*(4-i)/4),padL-5,y+3); }
    const xat=i=> n<=1?padL+plotW/2:padL+plotW*i/(n-1), yat=v=>padT+plotH*(1-Math.min(v,max)/max);
    series.forEach(s=>{
      ctx.beginPath(); let started=false;
      s.values.forEach((v,i)=>{ if(v==null){ started=false; return; } const x=xat(i),y=yat(v); if(started) ctx.lineTo(x,y); else { ctx.moveTo(x,y); started=true; } });
      ctx.strokeStyle=s.color; ctx.lineWidth=s.width||2; ctx.setLineDash(s.dash?[5,4]:[]); ctx.stroke(); ctx.setLineDash([]);
      if(n<=45) s.values.forEach((v,i)=>{ if(v==null) return; ctx.beginPath(); ctx.arc(xat(i),yat(v),2.3,0,7); ctx.fillStyle=s.color; ctx.fill(); });
    });
    ctx.fillStyle=dim; ctx.font='10px sans-serif'; const step=Math.max(1,Math.ceil(n/(W>620?12:6)));
    labels.forEach((l,i)=>{ if(i%step===0||i===n-1){ ctx.textAlign = i===0?'left':(i===n-1?'right':'center'); ctx.fillText(l,xat(i),H-padB+15); } });
    geo={padL,plotW,n};
  });
  chartTipAttach(container,(x)=>{ if(!geo) return ''; const i=geo.n<=1?0:Math.round((x-geo.padL)/(geo.plotW/(geo.n-1))); if(i<0||i>=geo.n) return '';
    return `<b>${esc(labels[i])}</b><br>`+series.map(s=>`<span class="dot" style="background:${s.color}"></span>${esc(s.name)}: <b>${s.values[i]==null?'—':yfmt(s.values[i],true)}</b>`).join('<br>'); });
}
function rpLegend(series){ return `<div class="an-legend">${series.map(s=>`<span class="lg"><span class="dot" style="background:${s.color}${s.dash?';opacity:.55':''}"></span>${esc(s.name)}</span>`).join('')}</div>`; }
/** Тепловая карта со своей палитрой и подсказкой. */
function chartHeatRp(container, matrix, rowLabels, opts={}){
  chartHeat(container, matrix, rowLabels, opts);
  const rows=matrix.length, cols=(matrix[0]||[]).length; if(!rows||!cols) return;
  chartTipAttach(container,(x,y)=>{
    const cv=container.querySelector('canvas'); const W=cv.clientWidth, H=cv.clientHeight;
    const padL=34,padT=6,padB=16, gw=(W-padL-6)/cols, gh=(H-padT-padB)/rows;
    const c=Math.floor((x-padL)/gw), r=Math.floor((y-padT)/gh); if(c<0||c>=cols||r<0||r>=rows) return '';
    return `<b>${esc(rowLabels[r])}, ${String(c).padStart(2,'0')}:00–${String(c+1).padStart(2,'0')}:00</b><br>${opts.fmt?opts.fmt(matrix[r][c],r,c):fmtNum(matrix[r][c])}`;
  });
}
function rpBar(v, max, color){ const w=max? Math.max(2, Math.round(100*(v||0)/max)) : 0; return `<span class="rp-bar"><span style="width:${w}%;background:${color||'var(--primary)'}"></span></span>`; }

async function viewReplies(c){
  c.innerHTML='';
  if(!RP.meta){ try{ RP.meta=await api('/replies/meta'); }catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось открыть раздел</div></div>'; return; } }
  const meta=RP.meta;
  if(RP.acc && !meta.accounts.find(a=>String(a.id)===String(RP.acc))) RP.acc='';
  const opt=(list,cur)=>list.map(([v,l])=>`<option value="${esc(v)}" ${String(v)===String(cur)?'selected':''}>${esc(l)}</option>`).join('');
  const bar=h(`<div class="card rp-filters">
    <div class="rp-frow">
      <label>Период <select id="rpPeriod">${opt(RP_PERIODS,RP.period)}</select></label>
      <span id="rpDates" ${RP.period==='custom'?'':'hidden'}><input type="date" id="rpFrom" value="${esc(RP.from)}" aria-label="С"> — <input type="date" id="rpTo" value="${esc(RP.to)}" aria-label="По"></span>
      <label>Ящик <select id="rpAcc"><option value="">${meta.is_admin?'Все ящики':'Все мои ящики'}</option>${meta.accounts.map(a=>`<option value="${a.id}" ${String(a.id)===String(RP.acc)?'selected':''}>${esc(a.name)}</option>`).join('')}</select></label>
      ${meta.is_admin&&meta.departments.length?`<label>Отдел <select id="rpDep"><option value="">Все отделы</option>${meta.departments.map(d=>`<option ${d===RP.dep?'selected':''}>${esc(d)}</option>`).join('')}</select></label>`:''}
      <span class="spacer" style="flex:1"></span>
      <a class="btn ghost sm" href="${docsHref('replies')}">📖 Как считается</a>
      <button class="btn ghost sm" id="rpRefresh">↻ Обновить</button>
    </div>
    <div class="rp-frow">
      <label>Собеседники <select id="rpScope">${opt([['all','Все'],['external','Внешние'],['internal','Свои (внутренние)']],RP.scope)}</select></label>
      <label>Письма <select id="rpKinds">${opt(RP_KINDS,RP.kinds)}</select></label>
      <label>Ящик среди адресатов <select id="rpAddr">${opt([['all','Не важно'],['to_cc','В «Кому» или «Копии»'],['to','Только в «Кому»']],RP.addressed)}</select></label>
      <label>Входящие <select id="rpInc">${opt([['all','Все'],['new','Только новые обращения']],RP.incoming)}</select></label>
      <label>Срок ответа, ч <input type="number" id="rpSla" min="0.25" max="8760" step="0.25" style="width:80px" value="${esc(RP.sla||meta.settings.sla_hours)}"></label>
      <label class="rp-check"><input type="checkbox" id="rpBiz" ${(RP.slaBiz!==''?RP.slaBiz==='true':meta.settings.sla_business)?'checked':''}> в рабочих часах</label>
    </div></div>`);
  c.appendChild(bar);
  const reload=()=>viewReplies(c);
  $('#rpPeriod',bar).onchange=e=>{ RP.period=e.target.value; if(RP.period==='custom'){ $('#rpDates',bar).hidden=false; return; } reload(); };
  [['#rpFrom','from'],['#rpTo','to']].forEach(([s,k])=>{ $(s,bar).onchange=e=>{ RP[k]=e.target.value; if(RP.from||RP.to) reload(); }; });
  $('#rpAcc',bar).onchange=e=>{ RP.acc=e.target.value; reload(); };
  if($('#rpDep',bar)) $('#rpDep',bar).onchange=e=>{ RP.dep=e.target.value; RP.acc=''; reload(); };
  $('#rpScope',bar).onchange=e=>{ RP.scope=e.target.value; reload(); };
  $('#rpKinds',bar).onchange=e=>{ RP.kinds=e.target.value; reload(); };
  $('#rpAddr',bar).onchange=e=>{ RP.addressed=e.target.value; reload(); };
  $('#rpInc',bar).onchange=e=>{ RP.incoming=e.target.value; reload(); };
  $('#rpSla',bar).onchange=e=>{ const v=parseFloat(String(e.target.value).replace(',','.')); if(!(v>=0.25&&v<=8760)){ toast('Срок ответа','От 0,25 до 8760 часов','warn'); return; } RP.sla=String(v); reload(); };
  $('#rpBiz',bar).onchange=e=>{ RP.slaBiz=e.target.checked?'true':'false'; reload(); };
  $('#rpRefresh',bar).onclick=()=>{ RP.meta=null; reload(); };
  if(RP.period==='custom' && !RP.from && !RP.to){ c.appendChild(h('<div class="card"><div class="empty">Выберите даты периода.</div></div>')); return; }

  const statusBox=h('<div></div>'); c.appendChild(statusBox);
  const body=h('<div><div class="card"><div class="empty"><span class="spinner"></span> Считаем ответы… На большом архиве за долгий период — до минуты.</div></div></div>');
  c.appendChild(body);
  const seq=State.routeSeq;
  let d;
  try{ d=await api('/replies/summary?'+rpParams()); }
  catch(e){ if(seq!==State.routeSeq) return; toastErr(e); body.innerHTML='<div class="card"><div class="empty">Не удалось посчитать ответы</div></div>'; return; }
  if(seq!==State.routeSeq || !body.isConnected) return;
  rpStatus(statusBox, d.index, meta, c);
  body.innerHTML='';
  rpRender(body, d, meta);
}

function rpStatus(box, st, meta, c){
  box.innerHTML='';
  if(!st) return;
  const running=!!st.job_status;
  const part=st.total? Math.round(100*st.indexed/st.total):100;
  if(!st.enabled && !st.indexed){ box.appendChild(h(`<div class="deep-banner"><div>⏸️ <strong>Индекс ответов выключен</strong><div class="muted small">Включите «Настройки → Ответы на письма → Вести индекс ответов».</div></div></div>`)); return; }
  if(st.pending<=0 && !running) return;
  const b=h(`<div class="deep-banner">
    <div style="min-width:240px;flex:1"><strong>${running?'<span class="spinner"></span> ':''}Индекс ответов: разобрано ${fmtNum(st.indexed)} из ${fmtNum(st.total)} писем (${part}%)</strong>
      <div class="rp-progress"><span style="width:${part}%"></span></div>
      <div class="muted small">${st.pending<=0?'Идёт разбор новых писем.':[
        st.since?`Разобраны письма с ${esc(new Date(st.since).toLocaleDateString('ru-RU'))} по сегодня — за этот срок цифры полные; более давние разбираются в фоне (раз в 10 минут, порциями, от новых к старым).`:'',
        (st.readdress||[]).length?(st.readdress.length===1?`У ящика «${esc(st.readdress[0])}» поменялись адреса — его письма разбираются заново, цифры по нему пока неполные.`:`У ящиков ${st.readdress.slice(0,3).map(n=>'«'+esc(n)+'»').join(', ')}${st.readdress.length>3?' и ещё '+fmtNum(st.readdress.length-3):''} поменялись адреса — их письма разбираются заново, цифры по ним пока неполные.`):'',
        !st.since&&!(st.readdress||[]).length?'Остальные письма разбираются в фоне (раз в 10 минут, порциями) — цифры раздела пока неполные.':''].filter(Boolean).join(' ')}</div></div>
    ${meta.is_admin&&!running?'<button class="btn sm" id="rpIdxRun">▶ Разобрать сейчас</button>':''}</div>`);
  box.appendChild(b);
  const run=$('#rpIdxRun',b);
  if(run) run.onclick=async()=>{ try{ await api('/replies/update',{method:'POST'}); toast('Разбор писем запущен','Ход — в «Очереди и заданиях»'); viewReplies(c); }catch(e){ toastErr(e); } };
}

function rpRender(body, d, meta){
  const s=d.summary, p=d.prev, prm=d.params;
  const redraws=[]; const reg=fn=>{ fn(); redraws.push(fn); };
  const C={ontime:cvar('--success','#1f9d55'), late:cvar('--warn','#d98a00'), flag:'#7c5cff', waiting:cvar('--info','#2b8ca6'), overdue:cvar('--danger','#d64545'), primary:cvar('--primary','#2f6fed'), dim:cvar('--text-dim','#888')};
  const slaTxt=`${String(prm.sla_hours).replace('.',',')} ч${prm.sla_business?' рабочих':''}`;
  const span=`${new Date(d.period.since*1000).toLocaleDateString('ru-RU')} — ${new Date((d.period.until-1)*1000).toLocaleDateString('ru-RU')}`;
  if(!s.in_total && !s.out_total){
    body.appendChild(h(`<div class="card"><div class="empty"><div class="big">↩️</div>За ${esc(span)} писем по этому отбору нет.<br><span class="muted small">Попробуйте период длиннее или другие фильтры. Если архив только что обновлён — дождитесь разбора писем (полоса выше).</span></div></div>`));
    registerCharts(redraws); return;
  }
  const kpi=(l,v,sub,delta,ac,cls)=>`<div class="kpi ${ac?'accent':''} ${cls||''}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${sub||''}</div>${delta?`<div class="k-sub">${delta}</div>`:''}</div>`;
  const pv=k=>p?p[k]:null;
  body.appendChild(h(`<div><div class="rp-period muted small">Период: <b>${esc(span)}</b>${p?` · сравнение с ${esc(new Date(p.since*1000).toLocaleDateString('ru-RU'))} — ${esc(new Date((p.until-1)*1000).toLocaleDateString('ru-RU'))}`:''} · срок ответа ${esc(slaTxt)} · рабочее время ${esc(prm.work_hours)}, ${esc(prm.timezone)}</div>
  <div class="an-kpis">
    ${kpi('📥 Входящих',fmtNum(s.in_total),'ждут ответа сейчас: '+fmtNum(s.waiting),rpDelta(s.in_total,pv('in_total'),'none'),true)}
    ${kpi('✅ Отвечено',fmtPct(s.answered_pct),fmtNum(s.answered)+' из '+fmtNum(s.in_total)+(s.by_subject_in?` · <span title="Ответ написан новым письмом — найден по теме и собеседнику">по теме: ${fmtNum(s.by_subject_in)}</span>`:''),rpDelta(s.answered_pct,pv('answered_pct'),'up','pct'))}
    ${kpi('⏱️ Медиана ответа',fmtDur(s.median_s),'среднее '+fmtDur(s.mean_s),rpDelta(s.median_s,pv('median_s'),'down','dur'))}
    ${kpi('📈 90% ответов быстрее',fmtDur(s.p90_s),'75% — быстрее '+fmtDur(s.p75_s),rpDelta(s.p90_s,pv('p90_s'),'down','dur'))}
    ${kpi('🎯 В срок',fmtPct(s.sla_pct),'срок '+esc(slaTxt)+' · позже срока: '+fmtNum(s.late),rpDelta(s.sla_pct,pv('sla_pct'),'up','pct'))}
    ${kpi('🚨 Без ответа, срок вышел',fmtNum(s.overdue),'всего без ответа: '+fmtNum(s.unanswered)+' ('+fmtPct(s.unanswered_pct)+')'+(s.in_followups_unanswered&&prm.incoming!=='new'?`; из них ответы на наши письма: ${fmtNum(s.in_followups_unanswered)} — <a href="#" id="rpOnlyNew">не считать</a>`:''),rpDelta(s.overdue,pv('overdue'),'down'),false,s.overdue?'kpi-bad':'')}
    ${kpi('🕘 В рабочих часах',fmtDur(s.median_business_s),'медиана без ночей и выходных · 90%: '+fmtDur(s.p90_business_s),rpDelta(s.median_business_s,pv('median_business_s'),'down','dur'))}
    ${kpi('🤝 Ответили коллеги',fmtNum(s.colleague),'отмечено «отвечено» без ответа в архиве: '+fmtNum(s.flag))}
    ${kpi('📤 Исходящих',fmtNum(s.out_total),'из них ответов: '+fmtNum(s.out_answers)+' · новых: '+fmtNum(s.out_new),rpDelta(s.out_total,pv('out_total'),'none'))}
    ${kpi('💬 Нам ответили',fmtPct(s.out_replied_pct),fmtNum(s.out_replied)+' из '+fmtNum(s.out_total)+' исходящих'+(s.by_subject_out?` · <span title="Ответ написан новым письмом — найден по теме и собеседнику">по теме: ${fmtNum(s.by_subject_out)}</span>`:''),rpDelta(s.out_replied_pct,pv('out_replied_pct'),'up','pct'))}
    ${kpi('⌛ Ждём ответа (медиана)',fmtDur(s.out_median_wait_s),'90% — быстрее '+fmtDur(s.out_p90_wait_s),rpDelta(s.out_median_wait_s,pv('out_median_wait_s'),'down','dur'))}
    ${kpi('🔇 Исходящие без ответа',fmtNum(s.out_noreply),'на них не ответили (автоответы не в счёт)',rpDelta(s.out_noreply,pv('out_noreply'),'down'))}
  </div></div>`));

  const onlyNew=$('#rpOnlyNew',body); if(onlyNew) onlyNew.onclick=e=>{ e.preventDefault(); RP.incoming='new'; route(); };
  // ---- Динамика ----
  const series=[{name:'Ответ в срок',color:C.ontime},{name:'Ответ позже срока',color:C.late},{name:'Отмечено «отвечено»',color:C.flag},{name:'Ждёт ответа',color:C.waiting},{name:'Без ответа, срок вышел',color:C.overdue}];
  const tItems=d.trend.map(t=>({label:t.label, values:[t.ontime,t.late,t.flag,t.waiting,t.overdue], extra:`Отвечено: <b>${fmtPct(t.answered_pct)}</b> · медиана ${fmtDur(t.median_s)}`}));
  const bucketName={day:'по дням',week:'по неделям',month:'по месяцам'}[d.period.bucket]||'';
  const trCard=h(`<div class="card an-card"><h3>📊 Входящие и ответы ${bucketName} <span class="h-sub">— наведите на столбец</span></h3><div class="an-chart" id="rpTrend"></div>${rpLegend(series)}</div>`);
  body.appendChild(trCard);
  reg(()=>chartStack($('#rpTrend',trCard), tItems, series, {height:250}));
  const labels=d.trend.map(t=>t.label);
  const prevAligned=(key)=>{ const tp=d.trend_prev||[]; const off=tp.length-labels.length; return labels.map((_,i)=>{ const x=tp[i+off]; return x? x[key]:null; }); };
  const hr=v=>v==null?null:Math.round(v/360)/10;
  const mlCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⏱️ Время ответа ${bucketName}</h3><div class="an-chart" id="rpMed"></div><div id="rpMedLeg"></div></div>
    <div class="card"><h3>✅ Доля отвеченных ${bucketName}</h3><div class="an-chart" id="rpRate"></div><div id="rpRateLeg"></div></div></div>`);
  body.appendChild(mlCard);
  const medS=[{name:'Медиана',color:C.primary,values:d.trend.map(t=>hr(t.median_s))},{name:'90% быстрее',color:C.late,values:d.trend.map(t=>hr(t.p90_s)),width:1.5}];
  if(p) medS.push({name:'Медиана, прошлый период',color:C.dim,values:prevAligned('median_s').map(hr),dash:true,width:1.5});
  const rateS=[{name:'Отвечено входящих',color:C.ontime,values:d.trend.map(t=>t.answered_pct)},{name:'Ответили на исходящие',color:C.primary,values:d.trend.map(t=>t.out_replied_pct)}];
  if(p) rateS.push({name:'Отвечено, прошлый период',color:C.dim,values:prevAligned('answered_pct'),dash:true,width:1.5});
  reg(()=>{ chartMulti($('#rpMed',mlCard), labels, medS, {height:220, yfmt:(v,t)=>t?fmtDur(v*3600):(Math.round(v*10)/10)+' ч'}); $('#rpMedLeg',mlCard).innerHTML=rpLegend(medS); });
  reg(()=>{ chartMulti($('#rpRate',mlCard), labels, rateS, {height:220, max:100, yfmt:v=>Math.round(v)+'%'}); $('#rpRateLeg',mlCard).innerHTML=rpLegend(rateS); });

  // ---- Распределения ----
  const how=d.how_answered.map(x=>({...x, color:{own:C.ontime,colleague:'#2bb0a6',flag:C.flag,waiting:C.waiting,overdue:C.overdue}[x.key]}));
  const kinds=s.kinds.map((x,i)=>({...x, color:[C.primary,'#e0567f',C.late,C.dim,'#8e6bd8'][i]}));
  const dCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>⏳ Через сколько отвечают</h3><div class="an-chart" id="rpHist"></div></div>
    <div class="card"><h3>🧭 Как ответили на входящие</h3><div class="an-chart" id="rpHow"></div><div id="rpHowLeg"></div></div></div>`);
  body.appendChild(dCard);
  reg(()=>chartVBars($('#rpHist',dCard), d.delay_hist, {height:230, color:C.primary}));
  reg(()=>{ chartDonut($('#rpHow',dCard), how, {height:190, centerLabel:'входящих'}); $('#rpHowLeg',dCard).innerHTML=donutLegend(how); });
  const aCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>📭 Сколько ждут письма без ответа</h3><div class="an-chart" id="rpAge"></div></div>
    <div class="card"><h3>📨 Что приходит <span class="h-sub">— все входящие периода</span></h3><div class="an-chart" id="rpKindsC"></div><div id="rpKindsLeg"></div></div></div>`);
  body.appendChild(aCard);
  reg(()=>chartHBars($('#rpAge',aCard), d.age_hist, {height:Math.max(120,d.age_hist.length*30), labelChars:18, color:C.overdue}));
  reg(()=>{ chartDonut($('#rpKindsC',aCard), kinds, {height:190, centerLabel:'писем'}); $('#rpKindsLeg',aCard).innerHTML=donutLegend(kinds); });

  // ---- Тепловая карта ----
  const heatModes={count:['Сколько писем приходит','писем',C.primary,v=>fmtNum(v)+' '+plural(v,'письмо','письма','писем')],
    unanswered_pct:['Доля писем без ответа','%',C.overdue,v=>fmtPct(v)+' без ответа'],
    median_h:['Медиана времени ответа','ч',C.late,v=>'медиана '+fmtDur(v*3600)]};
  const hCard=h(`<div class="card an-card"><h3>🔥 День недели × час получения <span class="h-sub">— чем ярче, тем больше</span>
    <span class="spacer" style="flex:1"></span><select id="rpHeatSel" style="width:auto">${Object.entries(heatModes).map(([k,v])=>`<option value="${k}" ${RP.heat===k?'selected':''}>${v[0]}</option>`).join('')}</select></h3><div class="an-chart" id="rpHeat"></div></div>`);
  body.appendChild(hCard);
  const drawHeat=()=>{ const m=heatModes[RP.heat]||heatModes.count; chartHeatRp($('#rpHeat',hCard), d.heat[RP.heat]||d.heat.count, d.heat.rows, {height:7*24+30, color:m[2], fmt:m[3]}); };
  reg(drawHeat);
  $('#rpHeatSel',hCard).onchange=e=>{ RP.heat=e.target.value; drawHeat(); };
  const hwCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>🕐 Медиана ответа по часу получения</h3><div class="an-chart" id="rpByHour"></div></div>
    <div class="card"><h3>📆 Отвечено по дню недели</h3><div class="an-chart" id="rpByWd"></div></div></div>`);
  body.appendChild(hwCard);
  reg(()=>chartVBars($('#rpByHour',hwCard), d.by_hour.map(x=>({label:x.label, value:Math.round((x.median_s||0)/60)})), {height:210, color:C.late}));
  reg(()=>chartVBars($('#rpByWd',hwCard), d.by_weekday.map(x=>({label:x.label, value:Math.round(x.answered_pct||0)})), {height:210, color:C.ontime}));
  chartTipAttach($('#rpByHour',hwCard),(x)=>{ const cv=$('#rpByHour canvas',hwCard); const i=Math.floor((x-40)/((cv.clientWidth-50)/24)); const r=d.by_hour[i]; return r?`<b>${r.label}:00–${String(i+1).padStart(2,'0')}:00</b><br>писем: ${fmtNum(r.count)}<br>медиана ответа: <b>${fmtDur(r.median_s)}</b><br>отвечено: ${fmtPct(r.answered_pct)}`:''; });
  chartTipAttach($('#rpByWd',hwCard),(x)=>{ const cv=$('#rpByWd canvas',hwCard); const i=Math.floor((x-40)/((cv.clientWidth-50)/7)); const r=d.by_weekday[i]; return r?`<b>${r.label}</b><br>писем: ${fmtNum(r.count)}<br>отвечено: <b>${fmtPct(r.answered_pct)}</b><br>медиана ответа: ${fmtDur(r.median_s)}`:''; });

  // ---- Сравнения ----
  const cmpRows=[['🕘 Когда пришло',d.work_split],['🏢 От кого',d.internal_split],['🎯 Ящик среди адресатов',d.addressed_split],['🧵 Что за письмо',d.thread_split||[]]].filter(r=>r[1].length);
  const cmpCard=h(`<div class="card an-card"><h3>⚖️ Сравнения</h3><div class="table-wrap"><table class="tbl rp-cmp"><thead><tr><th></th><th>Писем</th><th>Отвечено</th><th>В срок</th><th>Медиана ответа</th><th>В рабочих часах</th><th>90% быстрее</th><th>Без ответа</th></tr></thead><tbody>
    ${cmpRows.map(([title,rows])=>`<tr class="rp-group"><td colspan="8">${title}</td></tr>`+rows.map(r=>`<tr><td>${esc(r.label)}</td><td>${fmtNum(r.count)}</td><td>${fmtPct(r.answered_pct)}</td><td>${fmtPct(r.sla_pct)}</td><td>${fmtDur(r.median_s)}</td><td>${r.median_business_s!=null?fmtDur(r.median_business_s):'—'}</td><td>${fmtDur(r.p90_s)}</td><td>${fmtNum(r.unanswered)}${r.overdue?` <span class="rp-red">(срок вышел: ${fmtNum(r.overdue)})</span>`:''}</td></tr>`).join('')).join('')}
  </tbody></table></div></div>`);
  body.appendChild(cmpCard);

  // ---- Ящики ----
  rpAccounts(body, d, meta, reg, C);
  if(d.departments.length>1) rpDepartments(body, d, reg, C);
  rpPeers(body, d, reg, C);
  rpOutgoing(body, d, reg, C);
  rpThreads(body, d, reg, C);
  const lists=h('<div></div>'); body.appendChild(lists);
  rpLists(lists, meta);
  if(meta.is_admin) rpIndexFooter(body, d.index);
  registerCharts(redraws);
}

/** Внизу раздела (администратору): состояние индекса ответов, «Разобрать новые» и «Пересобрать». */
function rpIndexFooter(body, st){
  if(!st) return;
  const f=h(`<div class="deep-banner"><div><strong>🗂️ Индекс ответов на письма</strong><div class="muted small">разобрано ${fmtNum(st.indexed)} из ${fmtNum(st.total)} писем${st.pending>0?` · ждут разбора: ${fmtNum(st.pending)}`:''}${st.enabled?' · новые письма разбираются сами раз в 10 минут':' · ведение индекса выключено в настройках'}. Если поменялись адреса ящика или его сотрудника, письма этого ящика переразберутся сами; пересобрать индекс целиком нужно, только если цифры вызывают сомнения.</div></div>
    <span class="spacer"></span>
    <button class="btn sm" id="rpUpd" ${st.job_status?'disabled':''}>▶ Разобрать новые</button>
    <button class="btn ghost sm" id="rpRebuild" ${st.job_status?'disabled':''}>↺ Пересобрать индекс</button></div>`);
  body.appendChild(f);
  $('#rpUpd',f).onclick=async()=>{ try{ await api('/replies/update',{method:'POST'}); toast('Разбор писем запущен','Ход — в «Очереди и заданиях»'); $('#rpUpd',f).disabled=true; }catch(e){ toastErr(e); } };
  $('#rpRebuild',f).onclick=async()=>{
    if(!await confirmDlg('Пересобрать индекс ответов?', 'Индекс будет очищен, и все письма архива разберутся заново (в фоне, порциями). Пока разбор не закончится, цифры раздела будут неполными.', {okText:'Пересобрать', okClass:'primary'})) return;
    try{ await api('/replies/rebuild',{method:'POST'}); toast('Индекс пересобирается','Ход — в «Очереди и заданиях»'); route(); }catch(e){ toastErr(e); }
  };
}

const RP_ACC_COLS=[['name','Ящик'],['count','Входящих'],['answered_pct','Отвечено'],['sla_pct','В срок'],['median_s','Медиана'],['p90_s','90% быстрее'],['median_business_s','В рабочих ч'],['unanswered','Без ответа'],['overdue','Срок вышел'],['colleague','Ответили коллеги'],['out','Исходящих'],['out_replied_pct','Нам ответили'],['out_median_wait_s','Ждём ответа']];
function rpAccounts(body, d, meta, reg, C){
  const card=h(`<div class="card an-card"><h3>📬 Сравнение ящиков <span class="h-sub">— щелчок по строке оставляет только этот ящик</span><span class="spacer" style="flex:1"></span>
    <input type="text" id="rpAccQ" placeholder="Найти ящик, сотрудника, отдел…" value="${esc(RP.accText)}" style="width:240px"></h3>
    <div class="grid cols-2"><div><div class="muted small">Медиана ответа, ч — самые медленные</div><div class="an-chart" id="rpAccSlow"></div></div><div><div class="muted small">Без ответа, срок вышел — больше всего</div><div class="an-chart" id="rpAccOver"></div></div></div>
    <div class="table-wrap" style="margin-top:12px"><table class="tbl rp-sort" id="rpAccT"></table></div></div>`);
  body.appendChild(card);
  const slow=d.accounts.filter(a=>a.median_s!=null).sort((a,b)=>b.median_s-a.median_s).slice(0,12);
  const over=d.accounts.filter(a=>a.overdue).sort((a,b)=>b.overdue-a.overdue).slice(0,12);
  reg(()=>chartHBars($('#rpAccSlow',card), slow.map(a=>({label:a.name, value:Math.round(a.median_s/360)/10})), {labelChars:26, color:C.late, fmt:x=>String(x.value).replace('.',',')+' ч'}));
  reg(()=>chartHBars($('#rpAccOver',card), over.map(a=>({label:a.name, value:a.overdue})), {labelChars:26, color:C.overdue}));
  const maxIn=Math.max(1,...d.accounts.map(a=>a.count));
  const draw=()=>{
    const q=RP.accText.trim().toLowerCase();
    let rows=d.accounts.filter(a=>!q || [a.name,a.username,a.employee,a.department].join(' ').toLowerCase().includes(q));
    const k=RP.accSort; rows=rows.slice().sort((a,b)=>{ const x=a[k], y=b[k]; if(k==='name') return RP.accDir*String(x||'').localeCompare(String(y||''),'ru'); return RP.accDir*(((x==null)?-1:x)-((y==null)?-1:y)); });
    const fmt={count:v=>fmtNum(v), answered_pct:fmtPct, sla_pct:fmtPct, median_s:fmtDur, p90_s:fmtDur, median_business_s:fmtDur, unanswered:fmtNum, overdue:fmtNum, colleague:fmtNum, out:fmtNum, out_replied_pct:fmtPct, out_median_wait_s:fmtDur};
    $('#rpAccT',card).innerHTML=`<thead><tr>${RP_ACC_COLS.map(([k,l])=>`<th data-k="${k}" class="${RP.accSort===k?'sorted':''}">${l}${RP.accSort===k?(RP.accDir<0?' ▼':' ▲'):''}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,500).map(a=>`<tr data-id="${a.id}" class="rp-click"><td><b>${esc(a.name)}</b><div class="muted small">${esc([a.employee&&a.employee!==a.name?a.employee:'',a.department].filter(Boolean).join(' · ')||a.username||'')}</div></td>${RP_ACC_COLS.slice(1).map(([k])=>`<td class="${k==='overdue'&&a.overdue?'rp-red':''}">${k==='count'?rpBar(a.count,maxIn)+' ':''}${fmt[k](a[k])}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${RP_ACC_COLS.length}" class="muted">Нет ящиков</td></tr>`}</tbody>`;
    $$('#rpAccT th',card).forEach(th=>th.onclick=()=>{ const k=th.dataset.k; if(RP.accSort===k) RP.accDir=-RP.accDir; else { RP.accSort=k; RP.accDir=k==='name'?1:-1; } draw(); });
    $$('#rpAccT tr[data-id]',card).forEach(tr=>tr.onclick=()=>{ RP.acc=tr.dataset.id; RP.dep=''; route(); });
  };
  draw();
  $('#rpAccQ',card).oninput=e=>{ RP.accText=e.target.value; draw(); };
}
function rpDepartments(body, d, reg, C){
  const card=h(`<div class="card an-card"><h3>🏢 Сравнение отделов</h3>
    <div class="grid cols-2"><div><div class="muted small">Отвечено, %</div><div class="an-chart" id="rpDepRate"></div></div><div><div class="muted small">Медиана ответа, ч</div><div class="an-chart" id="rpDepMed"></div></div></div>
    <div class="table-wrap" style="margin-top:12px"><table class="tbl"><thead><tr><th>Отдел</th><th>Ящиков</th><th>Входящих</th><th>Отвечено</th><th>В срок</th><th>Медиана</th><th>90% быстрее</th><th>Без ответа</th><th>Срок вышел</th><th>Исходящих</th><th>Нам ответили</th></tr></thead><tbody>
    ${d.departments.map(x=>`<tr class="rp-click" data-dep="${esc(x.name)}"><td><b>${esc(x.name)}</b></td><td>${fmtNum(x.accounts)}</td><td>${fmtNum(x.count)}</td><td>${fmtPct(x.answered_pct)}</td><td>${fmtPct(x.sla_pct)}</td><td>${fmtDur(x.median_s)}</td><td>${fmtDur(x.p90_s)}</td><td>${fmtNum(x.unanswered)}</td><td class="${x.overdue?'rp-red':''}">${fmtNum(x.overdue)}</td><td>${fmtNum(x.out)}</td><td>${fmtPct(x.out_replied_pct)}</td></tr>`).join('')}
    </tbody></table></div></div>`);
  body.appendChild(card);
  const deps=d.departments.slice(0,15);
  reg(()=>chartHBars($('#rpDepRate',card), deps.map(x=>({label:x.name, value:Math.round(x.answered_pct||0)})), {colorByIndex:true, fmt:x=>x.value+'%'}));
  reg(()=>chartHBars($('#rpDepMed',card), deps.map(x=>({label:x.name, value:Math.round((x.median_s||0)/360)/10})), {color:C.late, fmt:x=>String(x.value).replace('.',',')+' ч'}));
  $$('tr[data-dep]',card).forEach(tr=>tr.onclick=()=>{ if(!RP.meta||!RP.meta.is_admin) return; RP.dep=tr.dataset.dep; RP.acc=''; route(); });
}
/** Строки таблицы: первые 10, остальные — по «Показать все». */
function rpRows(rows, render, cols){
  if(!rows.length) return `<tr><td colspan="${cols}" class="muted">Нет данных</td></tr>`;
  return rows.map((r,i)=>render(r).replace('<tr', `<tr${i>=10?' class="rp-more" hidden':''}`)).join('')
    + (rows.length>10?`<tr class="rp-more-row"><td colspan="${cols}"><a href="#" class="rp-more-link">Показать все (${rows.length})</a></td></tr>`:'');
}
function rpMoreLinks(root){ $$('.rp-more-link',root).forEach(a=>a.onclick=e=>{ e.preventDefault(); const tb=a.closest('tbody'); $$('tr.rp-more',tb).forEach(tr=>tr.hidden=false); a.closest('tr').remove(); }); }
function rpPeerTable(rows, key, label){
  return `<div class="table-wrap"><table class="tbl"><thead><tr><th>${label}</th><th>Писем</th><th>Отвечено</th><th>Медиана ответа</th><th>Без ответа</th><th>Срок вышел</th></tr></thead><tbody>${rpRows(rows, r=>`<tr><td>${esc(trunc(r[key],42))}</td><td>${fmtNum(r.count)}</td><td>${fmtPct(r.answered_pct)}</td><td>${fmtDur(r.median_s)}</td><td>${fmtNum(r.unanswered)}</td><td class="${r.overdue?'rp-red':''}">${fmtNum(r.overdue)}</td></tr>`, 6)}</tbody></table></div>`;
}
function rpPeers(body, d, reg, C){
  const sTabs={senders:['Пишут чаще всего',d.senders],senders_ignored:['Больше всего без ответа',d.senders_ignored]};
  const dTabs={domains:['Пишут чаще всего',d.domains],domains_slow:['Отвечаем медленнее всего',d.domains_slow],domains_fast:['Отвечаем быстрее всего',d.domains_fast]};
  const card=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>👤 Отправители <span class="h-sub">— всего ${fmtNum(d.senders_total)}</span></h3><div class="tabs" id="rpSTabs">${Object.entries(sTabs).map(([k,v])=>`<a href="#" data-t="${k}" class="${RP.peerTab===k?'active':''}">${v[0]}</a>`).join('')}</div><div id="rpSBody"></div></div>
    <div class="card"><h3>🌐 Домены отправителей <span class="h-sub">— всего ${fmtNum(d.domains_total)}</span></h3><div class="tabs" id="rpDTabs">${Object.entries(dTabs).map(([k,v])=>`<a href="#" data-t="${k}" class="${RP.domTab===k?'active':''}">${v[0]}</a>`).join('')}</div><div id="rpDBody"></div></div></div>`);
  body.appendChild(card);
  const drawS=()=>{ if(!sTabs[RP.peerTab]) RP.peerTab='senders'; $('#rpSBody',card).innerHTML=rpPeerTable(sTabs[RP.peerTab][1],'peer','Отправитель'); rpMoreLinks($('#rpSBody',card)); $$('#rpSTabs a',card).forEach(a=>a.classList.toggle('active',a.dataset.t===RP.peerTab)); };
  const drawD=()=>{ if(!dTabs[RP.domTab]) RP.domTab='domains'; $('#rpDBody',card).innerHTML=rpPeerTable(dTabs[RP.domTab][1],'domain','Домен'); rpMoreLinks($('#rpDBody',card)); $$('#rpDTabs a',card).forEach(a=>a.classList.toggle('active',a.dataset.t===RP.domTab)); };
  $$('#rpSTabs a',card).forEach(a=>a.onclick=e=>{ e.preventDefault(); RP.peerTab=a.dataset.t; drawS(); });
  $$('#rpDTabs a',card).forEach(a=>a.onclick=e=>{ e.preventDefault(); RP.domTab=a.dataset.t; drawD(); });
  drawS(); drawD();
}
function rpOutgoing(body, d, reg, C){
  const t=d.trend;
  const card=h(`<div class="card an-card"><h3>📤 Исходящие: отвечают ли нам</h3>
    <div class="grid cols-2"><div><div class="muted small">Отправлено и получен ответ</div><div class="an-chart" id="rpOutTr"></div><div id="rpOutLeg"></div></div>
    <div><div class="muted small">Через сколько нам отвечают</div><div class="an-chart" id="rpWait"></div></div></div>
    <div class="grid cols-2" style="margin-top:12px">
      <div><h4>Кому пишем чаще всего</h4>${rpOutTable(d.out_domains)}</div>
      <div><h4>Отвечают реже всего <span class="h-sub">(от 3 писем)</span></h4>${rpOutTable(d.out_domains_silent)}</div></div></div>`);
  body.appendChild(card);
  rpMoreLinks(card);
  const outS=[{name:'Отправлено',color:C.primary},{name:'Без ответа',color:C.dim}];
  reg(()=>{ chartStack($('#rpOutTr',card), t.map(x=>({label:x.label, values:[x.out_replied, x.out-x.out_replied], extra:`Ответили: <b>${fmtPct(x.out_replied_pct)}</b> · медиана ожидания ${fmtDur(x.out_median_wait_s)}`})), [{name:'Получен ответ',color:C.ontime},{name:'Без ответа',color:C.dim}], {height:210}); $('#rpOutLeg',card).innerHTML=rpLegend([{name:'Получен ответ',color:C.ontime},outS[1]]); });
  reg(()=>chartVBars($('#rpWait',card), d.wait_hist, {height:210, color:C.primary}));
}
function rpOutTable(rows){
  return `<div class="table-wrap"><table class="tbl"><thead><tr><th>Домен получателя</th><th>Писем</th><th>Ответили</th><th>Медиана ожидания</th><th>Без ответа</th></tr></thead><tbody>${rpRows(rows, r=>`<tr><td>${esc(trunc(r.domain,40))}</td><td>${fmtNum(r.count)}</td><td>${fmtPct(r.replied_pct)}</td><td>${fmtDur(r.median_wait_s)}</td><td>${fmtNum(r.noreply)}</td></tr>`, 5)}</tbody></table></div>`;
}
function rpThreads(body, d, reg, C){
  const th=d.threads;
  const card=h(`<div class="card an-card"><h3>🧵 Цепочки переписки <span class="h-sub">— письма одной переписки по заголовку References</span></h3>
    <div class="an-kpis" style="margin-bottom:8px">
      <div class="kpi"><div class="k-label">Цепочек</div><div class="k-value">${fmtNum(th.total)}</div><div class="k-sub">с ответами: ${fmtNum(th.with_replies)}</div></div>
      <div class="kpi"><div class="k-label">Писем в цепочке</div><div class="k-value">${th.avg_len==null?'—':String(th.avg_len).replace('.',',')}</div><div class="k-sub">в среднем; в цепочках с ответами — ${th.avg_len_multi==null?'—':String(th.avg_len_multi).replace('.',',')}</div></div>
      <div class="kpi"><div class="k-label">Самая длинная</div><div class="k-value">${fmtNum(th.max_len)}</div><div class="k-sub">писем</div></div></div>
    <div class="grid cols-2"><div><div class="muted small">Сколько писем в цепочке</div><div class="an-chart" id="rpThH"></div></div>
    <div><div class="muted small">Самые длинные (щелчок — открыть последнее письмо)</div><div class="table-wrap"><table class="tbl"><thead><tr><th>Тема</th><th>Писем</th><th>Длилась</th><th>Последнее</th></tr></thead><tbody>
      ${th.longest.map((x,i)=>`<tr class="rp-click" data-i="${i}"><td>${esc(trunc(x.subject||'(без темы)',60))}<div class="muted small">входящих ${fmtNum(x.incoming)} · исходящих ${fmtNum(x.outgoing)} · ящиков ${fmtNum(x.mailboxes)}</div></td><td>${fmtNum(x.messages)}</td><td>${fmtDur(x.duration_s)}</td><td class="small muted">${fmtDate(new Date(x.last_ts*1000).toISOString())}</td></tr>`).join('')||'<tr><td colspan="4" class="muted">Нет цепочек</td></tr>'}
    </tbody></table></div></div></div></div>`);
  body.appendChild(card);
  reg(()=>chartVBars($('#rpThH',card), th.hist, {height:220, color:'#7c5cff'}));
  $$('tr[data-i]',card).forEach(tr=>tr.onclick=()=>{ const x=th.longest[parseInt(tr.dataset.i)]; if(x&&x.account_id) openMailMessage(x.account_id, x.message_id, x.folder); });
}

// ---- Списки писем: без ответа, просроченные, отвеченные с опозданием, исходящие без ответа ----
function rpLists(wrap, meta){
  const card=h(`<div class="card an-card" id="rpListCard"><h3>📋 Письма</h3>
    <div class="tabs" id="rpLTabs">${meta.lists.map(l=>`<a href="#" data-m="${l.id}" class="${RP.list===l.id?'active':''}">${esc(l.label)}</a>`).join('')}</div>
    <div class="an-toolbar">
      <input type="text" id="rpLQ" placeholder="Отправитель, получатель или тема…" value="${esc(RP.text)}" style="flex:1;min-width:200px">
      <select id="rpLOrder"><option value="old" ${RP.order==='old'?'selected':''}>Сначала давние</option><option value="new" ${RP.order==='new'?'selected':''}>Сначала новые</option></select>
      <a class="btn sm" id="rpCsv" href="#" download>⬇ CSV</a>
      <span class="muted small" id="rpLTotal"></span></div>
    <div class="table-wrap"><table class="tbl"><thead><tr><th>Дата</th><th>Ящик</th><th>Собеседник</th><th>Тема</th><th id="rpLAgeH">Ждёт</th></tr></thead><tbody id="rpLBody"></tbody></table></div>
    <div style="text-align:center;margin-top:10px"><button class="btn ghost sm" id="rpLMore" hidden>Показать ещё</button></div></div>`);
  wrap.appendChild(card);
  let offset=0, total=0, items=[];
  const csvHref=()=>'/api/replies/list.csv?'+rpParams({mode:RP.list, order:RP.order, text:RP.text});
  const load=async(append)=>{
    const seq=State.routeSeq;
    let r; try{ r=await api('/replies/list?'+rpParams({mode:RP.list, order:RP.order, text:RP.text, offset:String(offset), limit:'100'})); }catch(e){ toastErr(e); return; }
    if(seq!==State.routeSeq || !card.isConnected) return;
    total=r.total; items=append?items.concat(r.items):r.items; offset=items.length;
    const answered=['late','answered','out_replied'].includes(RP.list);
    $('#rpLAgeH',card).textContent=answered?'Ответ через':'Ждёт';
    $('#rpLTotal',card).textContent=`${fmtNum(total)} ${plural(total,'письмо','письма','писем')}`;
    $('#rpLBody',card).innerHTML=items.map((x,i)=>`<tr class="rp-click" data-i="${i}"><td class="small">${fmtDate(x.date)}</td><td>${esc(trunc(x.account_name,28))}<div class="muted small">${esc(trunc(x.folder,28))}</div></td><td>${esc(trunc(x.peer||x.from,40))}${x.kind?`<div class="muted small">${esc(x.kind_label)}</div>`:''}</td><td>${esc(trunc(x.subject||'(без темы)',70))}${x.is_reply?' <span class="muted small" title="Это ответ на другое письмо">↩</span>':''}</td><td class="${answered?'':'rp-red'}">${fmtDur(answered?x.delay_s:x.age_s)}${x.by_subject?' <span class="rp-tag" title="Ответ написан новым письмом — найден по теме и собеседнику">по теме</span>':''}</td></tr>`).join('')||'<tr><td colspan="5" class="muted">Писем нет — отлично.</td></tr>';
    $('#rpLMore',card).hidden=items.length>=total;
    $$('#rpLBody tr[data-i]',card).forEach(tr=>tr.onclick=()=>{ const x=items[parseInt(tr.dataset.i)]; if(x) openMailMessage(x.account_id, x.id, x.folder); });
    $('#rpCsv',card).href=csvHref();
  };
  $$('#rpLTabs a',card).forEach(a=>a.onclick=e=>{ e.preventDefault(); RP.list=a.dataset.m; $$('#rpLTabs a',card).forEach(x=>x.classList.toggle('active',x===a)); offset=0; load(false); });
  let qT=null;
  $('#rpLQ',card).oninput=e=>{ RP.text=e.target.value; clearTimeout(qT); qT=setTimeout(()=>{ offset=0; load(false); }, 350); };
  $('#rpLOrder',card).onchange=e=>{ RP.order=e.target.value; offset=0; load(false); };
  $('#rpLMore',card).onclick=()=>load(true);
  $('#rpCsv',card).href=csvHref();
  load(false);
}

/** Открыть ящик в разделе «Почта» с первой папки и первой страницы (раньше — с папки и страницы,
 *  открытых в прежнем ящике: чужая папка подменялась первой, а номер страницы оставался). */
function openMailbox(accId){
  State.mailAccount=accId; Mail.folder=null; Mail.offset=0; Mail.search=null;
  if(location.hash==='#/mail') route(); else location.hash='#/mail';
}

/** Открыть письмо в разделе «Почта» (из отчётов аналитики). */
function openMailMessage(accId, msgId, folder){
  State.mailAccount=accId; Mail.folder=folder||null; Mail.offset=0; Mail.search=null; Mail.openAfter={acc:accId, id:msgId};
  if(location.hash==='#/mail') route(); else location.hash='#/mail';
}

// ===================================================================
//  Раздел «Аналитика писем» (содержание)
// ===================================================================
async function viewMailAnalytics(c){
  const accs=(State.accounts&&State.accounts.length)?State.accounts:await api('/accounts'); State.accounts=accs;
  const scope=State.maScope||'';
  let d; try{ d=await api('/analytics/mail'+(scope?('?account_id='+scope):'')); }catch(e){ toastErr(e); c.innerHTML='<div class="card"><div class="empty">Не удалось загрузить аналитику писем</div></div>'; return; }
  c.innerHTML='';
  const bar=h(`<div class="an-toolbar">
    <label class="muted small">Ящик:</label>
    <select id="maScope"><option value="">Все ящики</option>${accs.map(a=>`<option value="${a.id}" ${String(a.id)===String(scope)?'selected':''}>${esc(a.name)}</option>`).join('')}</select>
    <span class="spacer" style="flex:1"></span>
    <button class="btn ghost sm" id="maRefresh">↻ Обновить</button></div>`);
  c.appendChild(bar);
  $('#maScope',bar).onchange=e=>{ State.maScope=e.target.value; viewMailAnalytics(c); };
  $('#maRefresh',bar).onclick=()=>viewMailAnalytics(c);

  const o=d.overview;
  if(!o.messages){ c.appendChild(h('<div class="card"><div class="empty"><div class="big">🔎</div>Нет локальных копий для анализа.<br>Сделайте резервное копирование ящика.</div></div>')); return; }
  const kpi=(l,v,s,ac)=>`<div class="kpi ${ac?'accent':''}"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s||''}</div></div>`;
  const span=o.date_from?`${(o.date_from||'').slice(0,10)} — ${(o.date_to||'').slice(0,10)}`:'—';
  c.appendChild(h(`<div class="an-kpis">
    ${kpi('✉️ Писем',fmtNum(o.messages),span,true)}
    ${kpi('💾 Объём',o.bytes_h,'ср. '+o.avg_size_h+' · медиана '+o.median_size_h)}
    ${kpi('👤 Отправителей',fmtNum(o.unique_senders),'доменов: '+o.unique_domains)}
    ${kpi('📎 С вложениями',o.with_attach_pct+'%',fmtNum(o.with_attach)+' '+plural(o.with_attach||0,'письмо','письма','писем'))}
    ${kpi('📬 Непрочитанных',o.unseen_pct+'%',fmtNum(o.unseen)+' из '+fmtNum(o.messages))}
    ${kpi('↩️ Ответы / Пересылки',fmtNum(o.reply)+' / '+fmtNum(o.forward),'⭐ важных: '+fmtNum(o.flagged))}
    ${kpi('🗂️ Папок',fmtNum(o.folders),'без темы: '+fmtNum(o.empty_subject))}
    ${kpi('📅 В среднем/день',fmtNum(o.avg_per_day),'за '+fmtNum(o.span_days)+' дн.')}
  </div>`));

  const redraws=[]; const reg=(fn)=>{ fn(); redraws.push(fn); };

  // Динамика по месяцам
  const mCard=h(`<div class="card an-card"><h3>📈 Динамика по месяцам</h3><div class="an-chart" id="maMonth"></div></div>`);
  c.appendChild(mCard);
  reg(()=>chartLine($('#maMonth',mCard), d.by_month, {height:230}));

  // Год + день недели
  const ywCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>🗓️ По годам</h3><div class="an-chart" id="maYear"></div></div>
    <div class="card"><h3>📆 По дням недели</h3><div class="an-chart" id="maWeek"></div></div></div>`);
  c.appendChild(ywCard);
  reg(()=>chartVBars($('#maYear',ywCard), d.by_year, {height:220}));
  reg(()=>chartVBars($('#maWeek',ywCard), d.by_weekday, {height:220, color:cvar('--info','#2b8ca6')}));

  // Часы
  const hCard=h(`<div class="card an-card"><h3>🕐 Распределение по часам суток</h3><div class="an-chart" id="maHour"></div></div>`);
  c.appendChild(hCard);
  reg(()=>chartVBars($('#maHour',hCard), d.by_hour, {height:210, color:cvar('--success','#1f9d55')}));

  // Тепловая карта день×час
  const heatCard=h(`<div class="card an-card"><h3>🔥 Активность: день недели × час <span class="h-sub">— чем ярче, тем больше писем</span></h3><div class="an-chart" id="maHeat"></div></div>`);
  c.appendChild(heatCard);
  reg(()=>chartHeat($('#maHeat',heatCard), d.heatmap.matrix, d.heatmap.rows, {max:d.heatmap.max, height:7*24+30}));

  // Размеры + состояния (donuts)
  const szCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>📐 Размеры писем</h3><div class="an-chart" id="maSize"></div></div>
    <div class="card"><h3>👁️ Прочитанность и вложения</h3>
      <div class="grid cols-2"><div><div class="an-chart" id="maRead"></div><div id="maReadLeg"></div></div>
      <div><div class="an-chart" id="maAtt"></div><div id="maAttLeg"></div></div></div></div></div>`);
  c.appendChild(szCard);
  reg(()=>chartHBars($('#maSize',szCard), d.size_hist, {height:Math.max(120,d.size_hist.length*30), labelChars:14, color:cvar('--warn','#d98a00')}));
  const readS=d.read_state.map((s,i)=>({...s,color:i===0?cvar('--success','#1f9d55'):cvar('--text-dim','#888')}));
  const attS=d.attach_state.map((s,i)=>({...s,color:i===0?cvar('--primary','#2f6fed'):cvar('--text-dim','#888')}));
  reg(()=>{ chartDonut($('#maRead',szCard),readS,{height:170,centerLabel:'писем'}); $('#maReadLeg',szCard).innerHTML=donutLegend(readS); });
  reg(()=>{ chartDonut($('#maAtt',szCard),attS,{height:170,centerLabel:'писем'}); $('#maAttLeg',szCard).innerHTML=donutLegend(attS); });

  // Отправители + домены
  const sdCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>👤 Топ отправителей</h3><div class="an-chart" id="maSenders"></div></div>
    <div class="card"><h3>🌐 Топ доменов</h3><div class="an-chart" id="maDomains"></div></div></div>`);
  c.appendChild(sdCard);
  reg(()=>chartHBars($('#maSenders',sdCard), d.top_senders.slice(0,15), {colorByIndex:true, labelChars:30, labelW:250, height:Math.max(90,Math.min(15,d.top_senders.length)*28)}));
  reg(()=>chartHBars($('#maDomains',sdCard), d.top_domains, {colorByIndex:true, labelChars:26, height:Math.max(90,d.top_domains.length*28)}));

  // Папки + слова темы
  const ffCard=h(`<div class="grid cols-2 an-card">
    <div class="card"><h3>🗂️ Папки</h3><div class="an-chart" id="maFolders"></div></div>
    <div class="card"><h3>🔤 Частые слова в темах</h3><div id="maWords"></div></div></div>`);
  c.appendChild(ffCard);
  reg(()=>chartHBars($('#maFolders',ffCard), d.folders, {colorByIndex:true, labelChars:20, height:Math.max(90,d.folders.length*28), fmt:x=>fmtNum(x.value)+' · '+x.bytes_h, valW:120}));
  $('#maWords',ffCard).innerHTML=wordCloud(d.subject_words);

  // Крупнейшие письма
  if(d.largest.length){
    const rows=d.largest.map(m=>`<tr><td>${esc(trunc(m.subject,48))}<div class="muted small">${esc(trunc(m.from,42))}</div></td><td>${esc(m.folder)}</td><td>${esc(m.size_h)}</td><td class="small muted">${fmtDateShort(m.date)}</td></tr>`).join('');
    c.appendChild(h(`<div class="card an-card"><h3>🏋️ Крупнейшие письма</h3><div class="table-wrap"><table class="tbl"><thead><tr><th>Тема / отправитель</th><th>Папка</th><th>Размер</th><th>Дата</th></tr></thead><tbody>${rows}</tbody></table></div></div>`));
  }

  // ---- Глубокий анализ содержимого (чтение .eml) ----
  const deepWrap=h(`<div id="deepWrap"></div>`); c.appendChild(deepWrap);
  registerCharts(redraws);   // регистрируем то, что уже есть; глубокий догрузим отдельно
  loadDeep(deepWrap, scope, redraws);
}

async function loadDeep(wrap, scope, redraws){
  let dd; try{ dd=await api('/analytics/mail/deep'+(scope?('?account_id='+scope):'')); }catch(e){ return; }
  const running = dd.running&&dd.running.length;
  const when = dd.data? fmtDate(dd.data.generated_at):null;
  wrap.innerHTML='';
  const banner=h(`<div class="deep-banner">
    <div><strong>🔬 Глубокий анализ содержимого</strong><div class="muted small">${dd.available?('обновлён: '+when+' · разобрано '+fmtNum(dd.data.scanned)+' '+plural(dd.data.scanned||0,'письмо','письма','писем')):'Разбирает сами письма (.eml): типы вложений, домены получателей, текст/HTML, язык, частые слова тела.'}</div></div>
    <span class="spacer" style="flex:1"></span>
    <button class="btn primary sm" id="deepRun" ${running?'disabled':''}>${running?'<span class="spinner"></span> Идёт анализ…':(dd.available?'↻ Пересчитать':'▶ Запустить анализ')}</button></div>`);
  wrap.appendChild(banner);
  $('#deepRun',banner).onclick=async()=>{
    try{ const r=await api('/analytics/mail/scan'+(scope?('?account_id='+scope):''),{method:'POST'});
      toast('Анализ запущен', r.already_running?'Уже выполняется':'Следите за прогрессом; результат появится здесь автоматически');
      const b=$('#deepRun',banner); b.disabled=true; b.innerHTML='<span class="spinner"></span> Идёт анализ…';
      pollDeep(wrap, scope, redraws, r.job_id);
    }catch(e){ toastErr(e); }
  };
  if(running){ pollDeep(wrap, scope, redraws, dd.running[0].id); }
  if(dd.available && dd.data) renderDeep(wrap, dd.data, redraws);
}
function pollDeep(wrap, scope, redraws, jobId){
  let n=0;
  const t=setInterval(async()=>{
    n++; if(n>120){ clearInterval(t); return; }
    if(State.view!=='mailanalytics'){ clearInterval(t); return; }
    try{ const j=await api('/jobs/'+jobId,{bg:true}); if(j.status && ['success','failed','cancelled','partial'].includes(j.status)){ clearInterval(t); if(State.view==='mailanalytics') loadDeep(wrap, scope, redraws); } }catch(e){ clearInterval(t); }
  }, 2500);
}
function renderDeep(wrap, dp, redraws){
  const at=dp.attachments, rc=dp.recipients, bd=dp.body;
  const box=h(`<div>
    <div class="an-kpis">
      <div class="kpi accent"><div class="k-label">📎 Вложений</div><div class="k-value">${fmtNum(at.count)}</div><div class="k-sub">${at.bytes_h} · в ${fmtNum(at.messages_with_attach)} письмах</div></div>
      <div class="kpi"><div class="k-label">🌐 Доменов получателей</div><div class="k-value">${fmtNum(rc.unique_domains)}</div><div class="k-sub">уникальных</div></div>
      <div class="kpi"><div class="k-label">📝 Ср. длина текста</div><div class="k-value">${fmtNum(bd.avg_text_len)}</div><div class="k-sub">символов</div></div>
      <div class="kpi"><div class="k-label">🔎 Разобрано</div><div class="k-value">${fmtNum(dp.scanned)}</div><div class="k-sub">ошибок: ${fmtNum(dp.errors)}</div></div>
    </div>
    <div class="grid cols-2 an-card">
      <div class="card"><h3>📎 Типы вложений (расширение)</h3><div class="an-chart" id="dpExt"></div></div>
      <div class="card"><h3>🧾 Типы вложений (MIME)</h3><div class="an-chart" id="dpType"></div></div></div>
    <div class="grid cols-2 an-card">
      <div class="card"><h3>📤 Домены получателей</h3><div class="an-chart" id="dpTo"></div></div>
      <div class="card"><h3>🧬 Формат тела и язык</h3><div class="grid cols-2"><div><div class="an-chart" id="dpBody"></div><div id="dpBodyLeg"></div></div><div><div class="an-chart" id="dpLang"></div><div id="dpLangLeg"></div></div></div></div></div>
    <div class="card an-card"><h3>🔤 Частые слова в тексте писем</h3><div id="dpWords"></div></div>
  </div>`);
  wrap.appendChild(box);
  const add=(fn)=>{ fn(); redraws.push(fn); };
  add(()=>chartHBars($('#dpExt',box), at.by_ext, {colorByIndex:true, labelChars:14, height:Math.max(80,at.by_ext.length*26)}));
  add(()=>chartHBars($('#dpType',box), at.by_type, {colorByIndex:true, labelChars:34, labelW:260, height:Math.max(80,at.by_type.length*26)}));
  add(()=>chartHBars($('#dpTo',box), rc.top_domains, {colorByIndex:true, labelChars:26, height:Math.max(80,rc.top_domains.length*26)}));
  add(()=>{ chartDonut($('#dpBody',box), bd.kinds, {height:170,centerLabel:'писем'}); $('#dpBodyLeg',box).innerHTML=donutLegend(bd.kinds); });
  add(()=>{ chartDonut($('#dpLang',box), bd.languages, {height:170,centerLabel:'писем'}); $('#dpLangLeg',box).innerHTML=donutLegend(bd.languages); });
  $('#dpWords',box).innerHTML=wordCloud(bd.top_words, {min:13, max:34});
  registerCharts(redraws);
}

