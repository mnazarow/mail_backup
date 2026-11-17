/* ===================================================================
   Языки интерфейса (1.24.0): русский (исходный) и английский.

   Строки интерфейса написаны по-русски прямо в разметке разделов. Перевод — каталог
   «русская строка → английская» (i18n-en.js): ключ — текст узла или подсказки целиком,
   «{}» — подстановка (число, имя). Переводится то, что появляется на странице: наблюдатель
   изменений DOM переводит новые текстовые узлы и атрибуты placeholder/title/aria-label/alt.
   Не переводится: редактируемый текст (окно письма), поля ввода и всё внутри
   translate="no" (письма, имена, адреса).

   Язык: выбор пользователя (этот браузер: localStorage и cookie ma_lang) → язык по
   умолчанию сервиса (server.ui_language: ru, en или auto — по языку браузера).
   =================================================================== */
"use strict";

const I18N = { lang: 'ru', exact: null, pats: null, observer: null };
//: Локаль для дат и чисел (toLocaleString).
let UI_LOCALE = 'ru-RU';
const I18N_CYR = /[А-Яа-яЁё]/;
const I18N_ATTRS = ['placeholder', 'title', 'aria-label', 'alt', 'data-tip'];
//: Чей текст не переводится: письма, ввод пользователя, имена (translate="no").
const I18N_SKIP = '[contenteditable="true"],[contenteditable=""],[translate="no"],textarea,script,style,.no-i18n';
//: …а подсказки (placeholder, title) не переводятся только у translate="no" и внутри редактируемого текста.
const I18N_ATTR_SKIP = '[translate="no"],script,style,.no-i18n';

function uiLang(){ return I18N.lang; }

/** Язык, выбранный в этом браузере ('' — не выбирали). */
function i18nChosen(){
  let l='';
  try{ l=localStorage.getItem('ma-lang')||''; }catch(_){}
  if(!l){ const m=document.cookie.match(/(?:^|;\s*)ma_lang=(ru|en)(?:;|$)/); if(m) l=m[1]; }
  return (l==='en'||l==='ru')?l:'';
}

/** Выбрать язык по умолчанию сервиса (если пользователь не выбирал сам). */
function i18nInit(serviceDefault){
  let l=i18nChosen();
  if(!l){
    const d=String(serviceDefault||'ru');
    l = d==='auto' ? (/^(ru|uk|be|kk)\b/i.test(navigator.language||'') ? 'ru' : 'en') : (d==='en'?'en':'ru');
  }
  i18nApplyLang(l);
}

function i18nApplyLang(l){
  if(l===I18N.lang && (l==='ru' || I18N.observer)) return;
  I18N.lang = l==='en' ? 'en' : 'ru';
  UI_LOCALE = I18N.lang==='en' ? 'en-GB' : 'ru-RU';
  document.documentElement.setAttribute('lang', I18N.lang);
  if(I18N.lang==='en'){
    i18nBuild();
    i18nTree(document.documentElement);
    if(!I18N.observer && typeof MutationObserver!=='undefined'){
      I18N.observer=new MutationObserver(i18nMutations);
      I18N.observer.observe(document.documentElement, {subtree:true, childList:true, characterData:true,
                                                        attributes:true, attributeFilter:I18N_ATTRS});
    }
  }
}

/** Сменить язык (запоминается в этом браузере) и перезагрузить страницу. */
function setLang(l){
  l = l==='en' ? 'en' : 'ru';
  try{ localStorage.setItem('ma-lang', l); }catch(_){}
  document.cookie=`ma_lang=${l}; path=/; max-age=31536000; SameSite=Lax`;
  location.reload();
}

function i18nBuild(){
  if(I18N.exact) return;
  const cat=(typeof I18N_EN!=='undefined')?I18N_EN:{};
  I18N.exact=new Map(); I18N.pats=[]; I18N.cache=new Map();
  for(let [k, v] of Object.entries(cat)){
    if(v==null) continue;
    if(k.indexOf('{}')<0){
      I18N.exact.set(k, v);
      // часть строки, которая в коде шла после «·» или «—»: «· уволено: {}» — и без них
      const a=/^[·—–]\s+(.+)$/su.exec(k);
      if(a && cat[a[1]]==null) I18N.exact.set(a[1], v.replace(/^[·—–]\s+/, ''));
      continue;
    }
    // «{} {}» подряд не разделить однозначно — одна подстановка (её текст переводится целиком)
    while(k.includes('{} {}') && v.includes('{} {}')){ k=k.replace('{} {}', '{}'); v=v.replace('{} {}', '{}'); }
    const lead=/^[·—–]\s+(.+)$/su.exec(k);
    i18nAddPattern(k, v);
    // часть строки, которая в коде шла после «·» или «—», — и без них
    if(lead && cat[lead[1]]==null) i18nAddPattern(lead[1], v.replace(/^[·—–]\s+/, ''));
  }

  // сначала длинные шаблоны: «Писем: {} — в архиве они остались.» раньше, чем «Писем: {}»
  I18N.pats.sort((a, b)=>b.fixed-a.fixed);
  // Шаблонов больше тысячи (1.27.0): строка сверяется только с теми, чьё начало (или конец) с ней совпадает, —
  // корзины по первому знаку начала, по последнему знаку конца и без того и другого. Порядок «длинные раньше»
  // сохраняется: в каждой корзине он тот же, побеждает совпадение с наименьшим номером.
  I18N.byFirst=new Map(); I18N.byLast=new Map(); I18N.rest=[];
  I18N.pats.forEach((p, i)=>{
    p.idx=i;
    let arr;
    if(p.pre){ arr=I18N.byFirst.get(p.pre[0]); if(!arr) I18N.byFirst.set(p.pre[0], arr=[]); }
    else if(p.suf){ const c=p.suf[p.suf.length-1]; arr=I18N.byLast.get(c); if(!arr) I18N.byLast.set(c, arr=[]); }
    else arr=I18N.rest;
    arr.push(p);
  });
}

function i18nAddPattern(k, v){
  const lit=k.split('{}');
  const parts=lit.map(p=>p.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/[{}]/g, '\\$&'));
  const mid=lit.slice(1, -1).reduce((a, x)=>x.length>a.length?x:a, '');
  // какие подстановки в кавычках («{}», “{}”): там — имена, темы, папки, и непереведённый текст в них — норма
  const quoted=lit.slice(0, -1).map((x, i)=>/[«“"']$/.test(x) && /^[»”"']/.test(lit[i+1]));
  I18N.pats.push({re:new RegExp('^'+parts.join('(.*?)')+'$', 's'), out:v, fixed:k.length-2*(lit.length-1),
                  pre:lit[0], suf:lit[lit.length-1], mid, quoted});
}

/** Первый (в порядке «длинные раньше») шаблон, под который подходит строка: {p, m} или null. */
function i18nMatch(s){
  let best=null;
  const scan=arr=>{
    if(!arr) return;
    for(const p of arr){
      if(best && p.idx>=best.p.idx) return;
      if(s.length<p.fixed || !s.startsWith(p.pre) || !s.endsWith(p.suf) || (p.mid && s.indexOf(p.mid)<0)) continue;
      const m=p.re.exec(s);
      if(m){ best={p, m}; return; }
    }
  };
  scan(I18N.byFirst.get(s[0]));
  scan(I18N.byLast.get(s[s.length-1]));
  scan(I18N.rest);
  return best;
}

/** Перевод строки целиком (null — перевода нет). Подстановки шаблона тоже переводятся, если это строки
 *  каталога: «Писем: {}» + «3 письма», «{} {}» + предложение-шаблон. Ответы запоминаются: одни и те же
 *  строки (состояния в списке из сотен ящиков) повторяются тысячи раз. */
function i18nLookup(s, depth=0){
  if(I18N.lang!=='en' || !s) return null;
  i18nBuild();
  const hit=I18N.exact.get(s);
  if(hit!=null) return hit;
  if(!I18N_CYR.test(s)) return null;
  if(!depth){
    const c=I18N.cache.get(s);
    if(c!==undefined) return c;
  }
  // сначала — строка каталога целиком, с числом или значком впереди («24 писем», «🏢 Вход через LDAP»): иначе её
  // перехватывал короткий шаблон («{} ч{}», «{} в {}») и выходила смесь языков
  let res=i18nWord(s);
  if(res==null && depth<4){
    // «… (3)» и «— …», «· …» — перевод самой фразы, раньше шаблонов: иначе короткий шаблон («{} в {}») брал строку
    // вместе с хвостом и переводил её кусками
    const c=/^(.*\S)\s*(\(\d[\d\s.,]*\))$/su.exec(s);
    if(c){ const t=i18nLookup(c[1], depth+1); if(t!=null) res=t+' '+c[2]; }
    const d=res==null ? /^([—–·•]\s+)(\S.*)$/su.exec(s) : null;
    if(d){ const t=i18nLookup(d[2], depth+1); if(t!=null) res=d[1]+t; }
  }
  const found=res==null ? i18nMatch(s) : null;
  if(found){
    const {p, m}=found;
    const vals=[];
    for(let j=1; j<m.length; j++){
      const v=m[j]==null ? '' : m[j], core=v.trim();
      let t=null;
      if(core && I18N_CYR.test(core)){
        t=depth<4 ? i18nLookup(core, depth+1) : null;
        // Непереведённый текст в подстановке без кавычек, начатый строчным русским словом, — это не имя, а кусок
        // фразы, которой нет в каталоге («Есть в архиве», «через Microsoft Graph»): шаблон к ней не подходит.
        // Самые общие шаблоны («{} в {}», «из {}», «Тип {}» — короче пяти знаков) — только для чисел, дат и
        // переведённого текста: «Упрутся» с заглавной в «{} в {}» — не имя, а начало фразы.
        if(t==null && !p.quoted[j-1] && (p.fixed<5 || /^[^А-Яа-яЁё]*[а-яё]/.test(core))){ vals.length=0; break; }
      }
      vals.push(t!=null ? v.replace(core, t) : v);
    }
    if(vals.length===m.length-1){ let i=0; res=p.out.replace(/\{\}/g, ()=>vals[i++]); }
  }
  if(res==null && depth<4){
    // Строки, склеенные в коде из фразы и значения («активных: 5», «ср. письмо 551 Б»), и строки из частей
    // через «·» (1.27.0).
    res=i18nPrefixed(s, depth);
    if(res==null) res=i18nSegments(s, depth);
  }
  if(!depth){
    if(I18N.cache.size>20000) I18N.cache.clear();
    I18N.cache.set(s, res);
  }
  return res;
}

/** Слово из каталога, в том числе с числом впереди («24 писем», «1.6 КБ», «📁 Входящие»), с тире впереди
 *  или точкой в конце («— всё: ящики, письма…»). */
function i18nWord(s){
  const hit=I18N.exact.get(s);
  if(hit!=null) return hit;
  const m=/^([+−-]?\d[\d\s.,]*|[^\wА-Яа-яЁё\s"«(]{1,3})\s+(\S.*)$/su.exec(s);
  if(m){ const w=I18N.exact.get(m[2]); if(w!=null) return m[1]+' '+w; }
  const c=/^(.*\S)\s*(\(\d[\d\s.,]*\))$/su.exec(s);          // «Мои сертификаты получателей (0)»
  if(c){ const w=I18N.exact.get(c[1]); if(w!=null) return w+' '+c[2]; }
  const d=/^([—–·•]\s+)?(.*?)([.:;]?)$/su.exec(s);
  if(d && (d[1] || d[3]) && d[2]){
    const w=I18N.exact.get(d[2]);
    if(w!=null) return (d[1]||'')+w+(d[3] && !w.endsWith(d[3]) ? d[3] : '');
  }
  return null;
}

/** Значение после фразы каталога: без русских букв — как есть; русское слово со строчной — должно быть строкой
 *  каталога; иначе (число с единицей, имя) — переводятся только отдельные слова каталога («551 Б» → «551 B»). */
function i18nValue(v, depth){
  if(!I18N_CYR.test(v)) return v;
  if(/^[^А-Яа-яЁё]*[а-яё]/.test(v)) return i18nLookup(v, depth+1);
  return v.replace(/[А-Яа-яЁё][А-Яа-яЁё.]*/g, w=>{ const t=I18N.exact.get(w); return t!=null ? t : w; });
}

/** «фраза каталога» + значение: «активных: 5», «ср. письмо 551 Б», «Недоступно: <строка каталога>». */
function i18nPrefixed(s, depth){
  for(let p=s.lastIndexOf(' '); p>0; p=s.lastIndexOf(' ', p-1)){
    const head=s.slice(0, p).trim(), rest=s.slice(p+1).trim();
    const h=I18N.exact.get(head);
    if(h==null || !rest) continue;
    const r=i18nValue(rest, depth);
    if(r!=null) return h+' '+r;
  }
  return null;
}

/** Строка из частей через «·»: каждая часть переводится сама; часть, которой нет в каталоге, допустима, только
 *  если это имя (с заглавной) или значение без русских слов. */
function i18nSegments(s, depth){
  if(s.indexOf(' · ')<0) return null;
  const out=[];
  let done=0;
  for(const part of s.split(' · ')){
    const core=part.trim();
    if(!core || !I18N_CYR.test(core)){ out.push(part); continue; }
    const t=i18nLookup(core, depth+1);
    if(t==null && /^[^А-Яа-яЁё]*[а-яё]/.test(core)) return null;
    if(t!=null) done++;
    out.push(t!=null ? part.replace(core, t) : part);
  }
  return done ? out.join(' · ') : null;       // ни одна часть не переведена — это не перевод
}

/** Строка интерфейса на текущем языке: t('Писем: {}', n). */
function t(s, ...args){
  let out=String(s==null?'':s);
  if(I18N.lang==='en'){
    const tr=i18nLookup(out);
    if(tr!=null) out=tr;
  }
  if(args.length){ let i=0; out=out.replace(/\{\}/g, ()=>i<args.length?String(args[i++]):'{}'); }
  return out;
}

function i18nSkip(el){ return !!(el && el.closest && el.closest(I18N_SKIP)); }

function i18nText(node){
  const v=node.nodeValue;
  if(!v || !I18N_CYR.test(v) || i18nSkip(node.parentElement)) return;
  const core=v.replace(/\s+/g, ' ').trim();
  const tr=i18nLookup(core);
  if(tr==null || tr===core) return;
  const lead=v.match(/^\s*/)[0], trail=v.match(/\s*$/)[0];
  node.nodeValue=lead+tr+trail;
}

function i18nAttrs(el){
  if(!el.getAttribute) return;
  for(const a of I18N_ATTRS){
    const v=el.getAttribute(a);
    if(!v || !I18N_CYR.test(v)) continue;
    if(el.closest(I18N_ATTR_SKIP) || (el.parentElement && el.parentElement.closest('[contenteditable="true"],[contenteditable=""]'))) continue;
    const tr=i18nLookup(v.replace(/\s+/g, ' ').trim());
    if(tr!=null && tr!==v) el.setAttribute(a, tr);
  }
}

function i18nTree(rootNode){
  if(I18N.lang!=='en' || !rootNode) return;
  if(rootNode.nodeType===3){ i18nText(rootNode); return; }
  if(rootNode.nodeType!==1 && rootNode.nodeType!==9 && rootNode.nodeType!==11) return;
  if(rootNode.nodeType===1){ i18nAttrs(rootNode); if(i18nSkip(rootNode)) return; }
  const walker=document.createTreeWalker(rootNode, NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT, {
    acceptNode(n){
      if(n.nodeType!==1) return NodeFilter.FILTER_ACCEPT;
      if(!n.matches(I18N_SKIP)) return NodeFilter.FILTER_ACCEPT;
      i18nAttrs(n);                     // подсказки поля ввода и окна письма — да, их содержимое — нет
      return NodeFilter.FILTER_REJECT;
    }});
  let n;
  while((n=walker.nextNode())){
    if(n.nodeType===3) i18nText(n); else i18nAttrs(n);
  }
}

function i18nMutations(list){
  for(const m of list){
    if(m.type==='childList') m.addedNodes.forEach(i18nTree);
    else if(m.type==='characterData') i18nText(m.target);
    else if(m.type==='attributes') i18nAttrs(m.target);
  }
}

/** Ссылки «Русский · English» (вход, профиль). */
function langSwitchHtml(){
  const cur=uiLang();
  return `<span class="lang-switch" translate="no">${cur==='ru'?'<b>Русский</b>':'<a href="#" data-set-lang="ru">Русский</a>'} · ${cur==='en'?'<b>English</b>':'<a href="#" data-set-lang="en">English</a>'}</span>`;
}

document.addEventListener('click', e=>{
  const a=e.target.closest && e.target.closest('[data-set-lang]');
  if(!a) return;
  e.preventDefault();
  setLang(a.getAttribute('data-set-lang'));
});

// язык, выбранный в этом браузере, — сразу (до ответа сервера)
if(i18nChosen()) i18nApplyLang(i18nChosen());
