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
  I18N.exact=new Map(); I18N.pats=[];
  for(let [k, v] of Object.entries(cat)){
    if(v==null) continue;
    if(k.indexOf('{}')<0){ I18N.exact.set(k, v); continue; }
    // «{} {}» подряд не разделить однозначно — одна подстановка (её текст переводится целиком)
    while(k.includes('{} {}') && v.includes('{} {}')){ k=k.replace('{} {}', '{}'); v=v.replace('{} {}', '{}'); }
    const parts=k.split('{}').map(p=>p.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/[{}]/g, '\\$&'));
    I18N.pats.push({re:new RegExp('^'+parts.join('(.*?)')+'$', 's'), out:v, fixed:k.length-2*(parts.length-1)});
  }
  // сначала длинные шаблоны: «Писем: {} — в архиве они остались.» раньше, чем «Писем: {}»
  I18N.pats.sort((a, b)=>b.fixed-a.fixed);
}

/** Перевод строки целиком (null — перевода нет). Подстановки шаблона тоже переводятся, если это строки
 *  каталога: «Писем: {}» + «3 письма», «{} {}» + предложение-шаблон. */
function i18nLookup(s, depth=0){
  if(I18N.lang!=='en' || !s) return null;
  i18nBuild();
  const hit=I18N.exact.get(s);
  if(hit!=null) return hit;
  if(!I18N_CYR.test(s)) return null;
  for(const p of I18N.pats){
    const m=p.re.exec(s);
    if(!m) continue;
    let i=1;
    return p.out.replace(/\{\}/g, ()=>{
      const v=m[i++]; if(v==null) return '';
      const core=v.trim();
      const t=core && I18N_CYR.test(core) && depth<4 ? i18nLookup(core, depth+1) : null;
      return t!=null ? v.replace(core, t) : v;
    });
  }
  return i18nWord(s);
}

/** Слово из каталога, в том числе с числом впереди («24 писем», «1.6 КБ», «📁 Входящие»). */
function i18nWord(s){
  const hit=I18N.exact.get(s);
  if(hit!=null) return hit;
  const m=/^([+−-]?\d[\d\s.,]*|[^\wА-Яа-яЁё\s"«(]{1,3})\s+(\S.*)$/su.exec(s);
  if(m){ const w=I18N.exact.get(m[2]); if(w!=null) return m[1]+' '+w; }
  const c=/^(.*\S)\s*(\(\d[\d\s.,]*\))$/su.exec(s);          // «Мои сертификаты получателей (0)»
  if(c){ const w=I18N.exact.get(c[1]); if(w!=null) return w+' '+c[2]; }
  return null;
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
