#!/usr/bin/env python3
"""Проверка собранного руководства (запускается из build.sh после сборки).

    python3 verify.py ORIG_UNPACKED OUT_UNPACKED NEW_ROOT OLD_ROOT

Что изменилось, проверка выясняет сама — из docs/ru нового и прежнего выпуска и из chapters.py, — а с
manifest.json от update.py только сверяется:

1. Версия: обложка, дата, колонтитул, свойства документа, подпись в конце, оглавление — новая версия.
2. Состав: заголовки H1 по порядку — «Что нового», главы из chapters.py, приложение; у изменившихся и новых
   глав заголовки H2 и H3 — как в Markdown (номер файла в номере раздела → номер главы); новые главы — с новой
   страницы и на своём месте; рисунки этих глав — с подписями, файлы — пиксель в пиксель снимки из docs/ru.
3. Текст: каждая строка изменившихся разделов Markdown, новых глав и записи CHANGELOG.md есть в своей главе
   (разметка снимается, пробелы не учитываются, ссылки — по правилам руководства); строки кода — точно;
   нумерованные списки — столько же пунктов и списков, сколько в Markdown; следов разметки нет.
4. Неизменное: главы из неизменившихся файлов и неизменившиеся разделы изменившихся глав — побайтно тот же XML,
   что в исходном документе после перенумерации; приложение — тоже (или его изменившиеся таблицы — точно из
   нового Markdown); части пакета меняются только ожидаемые.
Любое расхождение — код возврата 1.
"""
import glob
import hashlib
import html
import json
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appendix  # noqa: E402
import bodyindex  # noqa: E402
import renumber  # noqa: E402
from chapters import APPENDIX_FILE, APPENDIX_TITLE, CHAPTERS, MD_NUM, NOT_IN_MANUAL, REFS  # noqa: E402

ORIG, OUT, NEW_ROOT, OLD_ROOT = sys.argv[1:5]
manifest = json.load(open(os.path.join(os.path.dirname(os.path.abspath(OUT)), "manifest.json"), encoding="utf-8"))
errors = []
counts = {}
IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", flags=re.M)
TITLES = {"Добавлено": "Добавлено", "Исправлено": "Исправлено", "Изменено": "Изменено",
          "Изменено (проверьте после обновления)": "Проверьте после обновления", "Ограничения": "Ограничения"}


def err(msg):
    errors.append(msg)
    print("  ✗ " + msg)


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def md(root, fname):
    return read(os.path.join(root, "docs", "ru", fname))


def text(x):
    return html.unescape(re.sub(r"<[^>]+>", "", x))


class D:
    def __init__(self, s):
        self.s = s
        self.els = bodyindex.index(self.s)
        self.x = [self.s[a:b] for _, a, b in self.els]
        self.h = []
        for x in self.x:
            m = re.match(r'<w:p><w:pPr><w:pStyle w:val="Heading(\d)"/>', x)
            self.h.append(int(m.group(1)) if m else 0)

    def chapter(self, prefix):
        a = next(i for i, x in enumerate(self.x) if self.h[i] == 1 and text(x).startswith(prefix))
        b = next((i for i in range(a + 1, len(self.x)) if self.h[i] == 1), len(self.x) - 1)   # sectPr — последний
        return a, b

    def sections(self, a, b):
        out, cur, start = {}, None, a + 1
        for i in range(a + 1, b + 1):
            if i == b or self.h[i] == 2:
                out[cur] = "".join(self.x[start:i])
                if i < b:
                    cur, start = text(self.x[i]).strip(), i
        return out


orig_s = read(f"{ORIG}/word/document.xml")
orig = D(orig_s)
out = D(read(f"{OUT}/word/document.xml"))
OLD = next(m.group(1) for m in (re.fullmatch(r"Версия (\d+\.\d+\.\d+)", text(x).strip()) for x in orig.x[:12]) if m)
NEW = read(os.path.join(NEW_ROOT, "VERSION")).strip()

# что изменилось — заново, независимо от update.py
old_num = {}
by_title = {t: f for f, (_n, t) in CHAPTERS.items()}
for i, x in enumerate(orig.x):
    if orig.h[i] == 1:
        m = re.fullmatch(r"(\d+)\. (.+)", text(x).strip())
        if m and m.group(2) in by_title:
            old_num[by_title[m.group(2)]] = int(m.group(1))
mapping = {old_num[f]: CHAPTERS[f][0] for f in old_num if old_num[f] != CHAPTERS[f][0]}
NEW_FILES = sorted((f for f in CHAPTERS if f not in old_num), key=lambda f: CHAPTERS[f][0])
FILES = sorted(f for f in old_num if md(OLD_ROOT, f) != md(NEW_ROOT, f))
UNCHANGED = sorted(f for f in old_num if f not in FILES)
APP_CHANGED = md(OLD_ROOT, APPENDIX_FILE) != md(NEW_ROOT, APPENDIX_FILE)
for key, mine in (("old", OLD), ("new", NEW), ("files", sorted(FILES)), ("new_files", NEW_FILES),
                  ("appendix_changed", APP_CHANGED)):
    theirs = manifest[key] if key != "files" else sorted(manifest[key])
    if theirs != mine:
        err(f"сборка и проверка разошлись в «{key}»: {theirs} / {mine}")
if {int(k): v for k, v in manifest["mapping"].items()} != mapping:
    err(f"сборка и проверка разошлись в перенумерации: {manifest['mapping']} / {mapping}")
for p in glob.glob(os.path.join(NEW_ROOT, "docs", "ru", "*.md")):
    f = os.path.basename(p)
    if f not in CHAPTERS and f not in NOT_IN_MANUAL and f != APPENDIX_FILE:
        err(f"docs/ru/{f} не входит в руководство (нет в chapters.py)")
base = D(renumber.renumber(orig_s, mapping)[0])     # исходный документ после перенумерации — образец неизменного
print(f"{OLD} → {NEW}: изменились {FILES}; новые главы {NEW_FILES}; перенумерация {mapping or 'нет'}; "
      f"приложение {'меняется' if APP_CHANGED else 'прежнее'}")


def sect_map(t, fname):
    if not fname or fname not in MD_NUM:
        return t
    mdn, docn = MD_NUM[fname], CHAPTERS[fname][0]
    return re.sub(rf"(?<![\d.]){mdn}\.(\d+)(?![\d]|\.\d)", lambda m: f"{docn}.{m.group(1)}", t)


def ch_title(f):
    return f"{CHAPTERS[f][0]}. {CHAPTERS[f][1]}"


# ---------------------------------------------------------------------------
print("1. Версия и дата")
cover = [text(x) for x in out.x[:9]]
if f"Версия {NEW}" not in cover:
    err(f"на обложке нет «Версия {NEW}»")
if OLD != NEW and any(OLD in t for t in cover):
    err(f"на обложке осталось «{OLD}»")
changelog = read(os.path.join(NEW_ROOT, "CHANGELOG.md"))
y, mo, d = re.search(rf"^## \[{re.escape(NEW)}\] — (\d{{4}})-(\d\d)-(\d\d)", changelog, flags=re.M).groups()
MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
date = f"{int(d)} {MONTHS[int(mo) - 1]} {y} г."
if date not in cover:
    err(f"на обложке нет даты «{date}»")
if not any(text(x).startswith(f"MailArchiver {NEW}. Полная интерактивная") for x in out.x[-4:]):
    err("подпись в конце документа не обновлена")
for part in sorted(glob.glob(f"{OUT}/word/header*.xml") + glob.glob(f"{OUT}/word/footer*.xml") +
                   glob.glob(f"{OUT}/docProps/*.xml")):
    if OLD != NEW and OLD in read(part):
        err(f"{os.path.relpath(part, OUT)}: осталось «{OLD}»")
for part in ("word/header1.xml", "docProps/core.xml"):
    if f"MailArchiver {NEW} — руководство" not in read(f"{OUT}/{part}"):
        err(f"{part}: нет «MailArchiver {NEW} — руководство»")
if not re.search(rf"<dcterms:modified[^>]*>{y}-{mo}-{d}T", read(f"{OUT}/docProps/core.xml")):
    err("docProps/core.xml: дата изменения не обновлена")
H1 = [f"Что нового в версии {NEW}"] + [ch_title(f) for f in sorted(CHAPTERS, key=lambda f: CHAPTERS[f][0])] + \
     [APPENDIX_TITLE]
toc_i = next(i for i, x in enumerate(out.x) if "<w:sdt>" in x)
toc_text = text(out.x[toc_i])
for h in H1:
    if h not in toc_text:
        err(f"оглавление: нет «{h}»")
if OLD != NEW and f"Что нового в версии {OLD}" in toc_text:
    err("оглавление: строка «Что нового» не обновлена")
body = text("".join(out.x[toc_i + 1:]))
print(f"  обложка: «Версия {NEW}», «{date}»; «{NEW}» в тексте: {body.count(NEW)}; «{OLD}» (исторические): "
      f"{body.count(OLD) if OLD != NEW else '—'}")
if OLD != NEW:
    for m in re.finditer(re.escape(OLD), body):
        ctx = body[max(0, m.start() - 70):m.end() + 25]
        if "Полная интерактивная" in ctx or "Что нового в версии" in ctx:
            err(f"«{OLD}» как текущая версия: …{ctx}…")

# ---------------------------------------------------------------------------
print("2. Состав глав, заголовки и рисунки")
h1 = [text(x).strip() for i, x in enumerate(out.x) if out.h[i] == 1]
if h1 != H1:
    err(f"заголовки H1: {h1}")
wa, wb = out.chapter(f"Что нового в версии {NEW}")
if 'w:type="page"' not in out.x[wa - 1]:
    err("перед «Что нового» нет разрыва страницы")
if "<w:pageBreakBefore/>" not in out.x[wb][:200]:
    err("глава после «Что нового» не с новой страницы")


def md_headings(src, level, fname):
    lines, fence = [], False
    for line in src.split("\n"):
        if re.match(r"^\s*```", line):
            fence = not fence
        elif not fence and line.startswith("#" * level + " "):
            lines.append(sect_map(re.sub(r"[`*]", "", line[level + 1:]).strip(), fname))
    return lines


rels = read(f"{OUT}/word/_rels/document.xml.rels")
rmap = dict(re.findall(r'Id="(rId\d+)" Type="[^"]+/image" Target="([^"]+)"', rels))
ref_cx = {re.search(r'<wp:extent cx="(\d+)"', x).group(1) for x in orig.x if "<w:drawing" in x}
try:
    from PIL import Image, ImageChops
except ImportError:              # без Pillow рисунки сверяются побайтно (снимки, записанные на Mac, — не совпадут)
    Image = None


def same_png(data_path, src_path):
    if open(data_path, "rb").read() == open(src_path, "rb").read():
        return True
    if Image is None:
        return False
    a_, b_ = Image.open(data_path), Image.open(src_path)
    return a_.size == b_.size and ImageChops.difference(a_.convert("RGB"), b_.convert("RGB")).getbbox() is None


new_shots = 0
for f in FILES + NEW_FILES:
    src = md(NEW_ROOT, f)
    a, b = out.chapter(f"{CHAPTERS[f][0]}. ")
    if text(out.x[a]).strip() != ch_title(f):
        err(f"глава «{ch_title(f)}» не найдена")
        continue
    for lvl in (2, 3):
        want = md_headings(src, lvl, f)
        have = [text(out.x[i]).strip() for i in range(a, b) if out.h[i] == lvl]
        if have != want:
            err(f"гл. {CHAPTERS[f][0]}: заголовки H{lvl} {have}, в Markdown {want}")
    shots = IMG_RE.findall(src)
    drawings = [i for i in range(a, b) if "<w:drawing" in out.x[i]]
    if f in NEW_FILES:
        pairs = list(zip(shots, drawings))
        if len(drawings) != len(shots):
            err(f"гл. {CHAPTERS[f][0]}: рисунков {len(drawings)}, в Markdown {len(shots)}")
    else:
        # в прежних главах есть и рисунки не из Markdown (схемы), и снимки прежних версий: проверяются новые
        old_paths = {p for _a, p in IMG_RE.findall(md(OLD_ROOT, f))}
        ba, bb = base.chapter(f"{CHAPTERS[f][0]}. ")
        was = sum(1 for i in range(ba, bb) if "<w:drawing" in base.x[i])
        fresh = [(alt, p) for alt, p in shots if p not in old_paths]
        if len(drawings) != was + len(fresh):
            err(f"гл. {CHAPTERS[f][0]}: рисунков {len(drawings)}, было {was}, новых в Markdown {len(fresh)}")
        caps = {text(out.x[i + 1]): i for i in drawings}
        pairs = []
        for alt, p in fresh:
            if alt in caps:
                pairs.append(((alt, p), caps[alt]))
            else:
                err(f"гл. {CHAPTERS[f][0]}: нет нового рисунка «{alt}» ({p})")
    for (alt, path), i in pairs:
        x = out.x[i]
        rid = re.search(r'r:embed="(rId\d+)"', x).group(1)
        cx, cy = map(int, re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', x).groups())
        src_png = os.path.join(NEW_ROOT, "docs", "ru", path)
        if not same_png(f"{OUT}/word/{rmap[rid]}", src_png):
            err(f"рисунок {path}: в документе другая картинка")
        w, h = struct.unpack(">II", open(src_png, "rb").read()[16:24])
        if str(cx) not in ref_cx or abs(cy - cx * h / w) > 1:
            err(f"рисунок {path}: размер {cx}×{cy} EMU (ширина не как у снимков документа или нарушены пропорции)")
        cap = out.x[i + 1]
        if text(cap) != alt or '<w:jc w:val="center"/>' not in cap or "<w:i/>" not in cap:
            err(f"рисунок {path}: подпись «{text(cap)}» вместо «{alt}»")
        if "<w:keepNext/>" not in x[:120]:
            err(f"рисунок {path}: абзац рисунка без keepNext (подпись может уйти на другую страницу)")
    if f in NEW_FILES:
        new_shots += len(shots)
        if "<w:pageBreakBefore/>" not in out.x[a][:200]:
            err(f"глава «{ch_title(f)}» не с новой страницы")
    else:
        new_shots += len({p for _a, p in shots} - {p for _a, p in IMG_RE.findall(md(OLD_ROOT, f))})
all_docpr = re.findall(r'<wp:docPr id="(\d+)"', out.s)
orig_docpr = re.findall(r'<wp:docPr id="(\d+)"', orig.s)
new_ids = sorted(set(all_docpr) - set(orig_docpr), key=int)
if len(all_docpr) != len(orig_docpr) + new_shots or len(new_ids) != new_shots or \
        any(all_docpr.count(i) != 1 for i in new_ids):
    err(f"рисунков в документе {len(all_docpr)}, было {len(orig_docpr)}, новых снимков {new_shots}; docPr новых "
        f"{new_ids} (должны быть свои у каждого)")
print(f"  рисунков: было {len(orig_docpr)}, стало {len(all_docpr)}; новых {new_shots}")
for rid, target in re.findall(r'<Relationship Id="(rId\d+)" [^>]*Target="([^"]+)"',
                              read(f"{ORIG}/word/_rels/document.xml.rels")):
    if f'Id="{rid}"' not in rels or target not in rels:
        err(f"document.xml.rels: пропала связь {rid} → {target}")


# ---------------------------------------------------------------------------
#  3. Текст
# ---------------------------------------------------------------------------
def split_sections(src):
    out_, cur, fence = {None: []}, None, False
    for line in src.split("\n"):
        if re.match(r"^\s*```", line):
            fence = not fence
        if not fence and line.startswith("## "):
            cur = re.sub(r"[`*]", "", line[3:]).strip()
            out_[cur] = []
        if not fence and line.startswith("# "):
            continue
        out_[cur].append(line)
    return out_


LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def norm(t):
    return re.sub(r"\s+", "", t.replace("**", "").replace("`", "").replace("\\|", "|"))


def check_lines(lines, chapter_xml, label, own=None):
    """Строки Markdown → (кусков текста, ссылок, строк кода)."""
    hay = norm(text(chapter_xml))
    code_paras = {text(p) for t in re.findall(r"<w:tbl>.*?</w:tbl>", chapter_xml, flags=re.S)
                  if 'w:fill="0D1017"' in t for p in re.findall(r"<w:p>.*?</w:p>", t, flags=re.S)}
    n = nl = nc = 0
    fence = None
    # строки Markdown → логические строки: абзац, пункт списка, строка таблицы, цитата — целиком
    joined, in_fence = [], False
    for raw in lines:
        if re.match(r"^\s*```", raw):
            in_fence = not in_fence
            joined.append(raw)
            continue
        st = raw.strip()
        starts = (in_fence or not st or re.match(r"^([-*]|\d+\.) ", st) or st.startswith(("|", "#", ">", "!["))
                  or re.fullmatch(r"-{3,}", st))
        prev = joined[-1].strip() if joined else ""
        if not starts and prev and not prev.startswith(("|", "#", "```", "![")) and not re.fullmatch(r"-{3,}", prev) \
                and not (prev.startswith(">") and not raw.startswith(">")):
            joined[-1] = joined[-1].rstrip() + " " + st
        elif raw.startswith(">") and prev.startswith(">") and st[1:].strip() and not re.match(r"^>\s*[-*] ", st):
            joined[-1] = joined[-1].rstrip() + " " + st[1:].strip()
        else:
            joined.append(raw)
    for raw in joined:
        m = re.match(r"^(\s*)```", raw)
        if m:
            fence = None if fence is not None else len(m.group(1))
            continue
        if fence is not None:
            line = raw[fence:] if raw[:fence].strip() == "" else raw.lstrip()
            if line.strip():
                nc += 1
                if line.rstrip() not in code_paras and line.rstrip() + " " not in code_paras:
                    err(f"{label}: нет строки кода {line.rstrip()!r}")
            continue
        line = raw.strip()
        if not line or re.fullmatch(r"\|?[\s:|-]+\|?", line) or re.fullmatch(r"-{3,}", line):
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if m:                                                    # рисунок: подпись проверяется отдельно (п. 2)
            n += 1
            if norm(m.group(1)) not in hay:
                err(f"{label}: нет подписи рисунка {m.group(1)!r}")
            continue
        pieces = []
        if line.startswith(">"):
            line = line[1:].strip()
            m = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", line)
            if m and m.group(1).rstrip().endswith("."):
                pieces.append(m.group(1).rstrip().rstrip("."))
                line = m.group(2)
        line = re.sub(r"^#{1,3} ", "", line)
        line = re.sub(r"^([-*]|\d+\.) ", "", line)
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))] if line.startswith("|") else [line]
        for cell in cells:
            prev_target = None
            parts = LINK.split(cell)            # текст, подпись, цель, текст, …
            for k in range(0, len(parts), 3):
                piece = parts[k]
                if prev_target:
                    piece = sect_map(piece, prev_target)          # «, раздел 19.6» после ссылки на 19-axigen.md
                piece = sect_map(piece, own) if own else piece
                if k + 1 < len(parts):
                    lab, target = parts[k + 1], parts[k + 2]
                    fname = target.split("#")[0].split("/")[-1]
                    prev_target = fname
                    nl += 1
                    if fname in CHAPTERS:
                        num = CHAPTERS[fname][0]
                        plain = lab.replace("docs/ru/", "")
                        m2 = re.fullmatch(r"\d\d-[\w-]+\.md,\s+раздел\s+(\d+\.\d+)", plain)
                        if plain == fname:
                            want = f"глава {num}"
                        elif m2:
                            want = f"раздел {sect_map(m2.group(1), fname)} (гл. {num})"
                        elif re.fullmatch(r"\d+\.\d+", plain):
                            want = f"разд. {sect_map(plain, fname)}"
                        elif re.search(r"\.md", plain):
                            want = f"глава {num}"
                        elif re.search(r"\d+\.\d+", plain):
                            want = sect_map(lab, fname)
                        else:
                            want = f"{lab} (гл. {num})"
                        if norm(want) not in hay:
                            err(f"{label}: ссылка {lab!r} → нет «{want}»")
                        before = norm(piece)[-12:]
                        if before and norm(before + want) not in hay and norm(before + "(" + want) not in hay \
                                and not re.search(r"[Гг]лавадокументации$", norm(piece)):
                            err(f"{label}: ссылка {lab!r} стоит не на своём месте: …{piece[-40:]}[ссылка]")
                    elif fname == APPENDIX_FILE:
                        if norm("«Справочник параметров»") not in hay:
                            err(f"{label}: ссылка на справочник параметров не стала «Справочник параметров»")
                    else:
                        want = sect_map(lab, own) if (own and target.startswith("#")) else lab
                        if norm(want) not in hay:
                            err(f"{label}: нет подписи ссылки {lab!r} (ожидалось «{want}»)")
                pieces.append(piece)
        for piece in pieces:
            p = norm(piece)
            if len(p) < 2:
                continue
            n += 1
            if p not in hay:
                err(f"{label}: нет текста {piece.strip()[:120]!r}")
    return n, nl, nc


numbering = read(f"{OUT}/word/numbering.xml")
abstract_fmt = {m.group(1): re.search(r'<w:lvl w:ilvl="0".*?<w:numFmt w:val="(\w+)"/>', m.group(0), flags=re.S).group(1)
                for m in re.finditer(r'<w:abstractNum w:abstractNumId="(\d+)".*?</w:abstractNum>', numbering, flags=re.S)}
DECIMAL = {n for n, a in re.findall(r'<w:num w:numId="(\d+)"><w:abstractNumId w:val="(\d+)"/>', numbering)
           if abstract_fmt.get(a) == "decimal"}


def lists_in_md(lines):
    """Нумерованные списки верхнего уровня: [число пунктов каждого]."""
    out_, fence = [], False
    for line in lines:
        if re.match(r"^\s*```", line):
            fence = not fence
            continue
        m = None if fence else re.match(r"^(\d+)\. ", line)
        if m:
            if m.group(1) == "1" or not out_:
                out_.append(0)
            out_[-1] += 1
    return out_


def lists_in_doc(xml):
    ids = [re.search(r'<w:numId w:val="(\d+)"', p).group(1) for p in re.findall(r"<w:p>.*?</w:p>", xml, flags=re.S)
           if "<w:numPr>" in p and '<w:ilvl w:val="0"/>' in p
           and re.search(r'<w:numId w:val="(\d+)"', p).group(1) in DECIMAL]
    out_ = []
    for k, n in enumerate(ids):
        if k == 0 or n != ids[k - 1]:
            out_.append(0)
        out_[-1] += 1
    return out_


print("3. Текст изменившихся и новых разделов")
totals = [0, 0, 0]
unchanged_secs = {}
for f in FILES + NEW_FILES:
    num = CHAPTERS[f][0]
    so = split_sections(md(OLD_ROOT, f)) if f in FILES else {}
    sn = split_sections(md(NEW_ROOT, f))
    a, b = out.chapter(f"{num}. ")
    secs_out = out.sections(a, b)
    unchanged_secs[f] = set()
    changed_list = []
    for sec, lines in sn.items():
        if so.get(sec) is not None and [x.rstrip() for x in so[sec]] == [x.rstrip() for x in lines]:
            unchanged_secs[f].add(sec)
            continue
        changed_list.append(sec)
        label = f"гл. {num} «{sec or 'вступление'}»"
        r = check_lines(lines, "".join(out.x[a:b]), label, own=f)
        totals = [t + x for t, x in zip(totals, r)]
        counts[label] = r[0]
        doc_sec = secs_out.get(None if sec is None else sect_map(sec, f))
        if doc_sec is None:
            err(f"{label}: раздела нет в документе")
        elif lists_in_md(lines) != lists_in_doc(doc_sec):
            err(f"{label}: нумерованные списки {lists_in_doc(doc_sec)} (пунктов в каждом), в Markdown "
                f"{lists_in_md(lines)}")
    t = text("".join(out.x[a:b]))
    if f in MD_NUM and re.search(rf"(?<![\d.]){MD_NUM[f]}\.\d", t) and MD_NUM[f] != num:
        err(f"гл. {num}: остался номер раздела {MD_NUM[f]}.N (номер файла)")
    print(f"  гл. {num} ({f}{', новая' if f in NEW_FILES else ''}): изменившихся разделов {len(changed_list)}")

# запись CHANGELOG.md → «Что нового»
a_ = changelog.index(f"## [{NEW}]")
nxt = changelog.find("\n## [", a_ + 1)
entry = changelog[a_:nxt if nxt >= 0 else len(changelog)].split("\n")[1:]
entry = [("### " + TITLES.get(x[4:].strip(), x[4:].strip())) if x.startswith("### ") else x for x in entry]
joined_entry = "\n".join(entry)
refs = []


def _ref(m):
    word, name = m.group(1), m.group(2)
    hits = [f for f, (_n, t) in CHAPTERS.items() if t == name or t.startswith((name + ":", name + " "))]
    if len(hits) != 1:
        return m.group(0)
    refs.append(f"{word} {CHAPTERS[hits[0]][0]} «{name}»")
    return refs[-1]


joined_entry = re.sub(r"(?<![\w])([Гг]лав[аеуыой]{1,2})\s+«([^«»\n]+)»", _ref, joined_entry)
for phrase, fname in REFS.items():
    def _add(m, fname=fname):
        refs.append(" ".join(f"{m.group(0)} (гл. {CHAPTERS[fname][0]})".split()))
        return f"{m.group(0)} (гл. {CHAPTERS[fname][0]})"
    joined_entry = re.sub(r"\s+".join(map(re.escape, phrase.split())) + r"(?!\s*\(гл\.)", _add, joined_entry)
entry = joined_entry.split("\n")
a, b = out.chapter(f"Что нового в версии {NEW}")
r = check_lines(entry, "".join(out.x[a:b]), "«Что нового»")
totals = [t + x for t, x in zip(totals, r)]
counts["«Что нового»"] = r[0]
h2 = [text(out.x[i]) for i in range(a, b) if out.h[i] == 2]
want_wn = [x[4:].strip() for x in entry if x.startswith("### ")]
if h2 != want_wn:
    err(f"«Что нового»: заголовки H2 {h2}, в записи {want_wn}")
for ref in refs:
    if norm(ref) not in norm(text("".join(out.x[a:b]))):
        err(f"«Что нового»: нет «{ref}»")
if sorted(x[1] for x in manifest["entry_refs"]) != sorted(refs):
    err(f"«Что нового»: ссылки на главы у сборки {manifest['entry_refs']}, у проверки {refs}")
print(f"  проверено кусков текста: {totals[0]}, ссылок: {totals[1]}, строк кода: {totals[2]}")
for k, v in counts.items():
    print(f"    {k}: {v}")

# следов разметки Markdown быть не должно
for prefix in ["Что нового", APPENDIX_TITLE] + [f"{CHAPTERS[f][0]}. " for f in FILES + NEW_FILES]:
    a, b = out.chapter(prefix)
    t = text("".join(out.x[a:b]))
    for mark in ("](", "**", "```", "\\|", ".md)", ".md]", "docs/ru"):
        if mark in t:
            err(f"глава «{prefix}»: в тексте осталась разметка {mark!r}: …{t[max(0, t.index(mark) - 60):t.index(mark) + 40]}…")
    for mm in re.finditer(r"\b\d\d-[a-z]+\.md\b", t):
        err(f"глава «{prefix}»: имя файла вместо ссылки на главу: …{t[max(0, mm.start() - 50):mm.end() + 10]}…")

# ---------------------------------------------------------------------------
print("4. Неизменное")
same_ch = 0
for f in UNCHANGED:
    num = CHAPTERS[f][0]
    ba, bb = base.chapter(f"{num}. ")
    na_, nb_ = out.chapter(f"{num}. ")
    if "".join(base.x[ba:bb]) != "".join(out.x[na_:nb_]):
        err(f"глава {num} ({f}): XML отличается от исходного")
        continue
    same_ch += 1
print(f"  глав из неизменившихся файлов, побайтно равных исходным: {same_ch} из {len(UNCHANGED)}")
same_sec = 0
for f in FILES:
    num = CHAPTERS[f][0]
    bs = base.sections(*base.chapter(f"{num}. "))
    ns_ = out.sections(*out.chapter(f"{num}. "))
    for sec, xml in bs.items():
        key = sec if sec is None else re.sub(r"[`*]", "", sec)
        mdkey = None if sec is None else next((k for k in unchanged_secs[f] if k is not None and
                                               sect_map(k, f) == key), key)
        if mdkey not in unchanged_secs[f]:
            continue
        if ns_.get(sec) != xml:
            err(f"гл. {num}: раздел «{sec}» не менялся в Markdown, а в документе изменился")
        else:
            same_sec += 1
print(f"  разделов изменившихся глав без изменений в Markdown, побайтно равных исходным: {same_sec}")
ba, bb = base.chapter(APPENDIX_TITLE)
bsec = base.sections(ba, bb)
asec = out.sections(*out.chapter(APPENDIX_TITLE))
closing = (f"MailArchiver {OLD}. Полная интерактивная", f"MailArchiver {NEW}. Полная интерактивная")
new_ref = dict(appendix.parse(md(NEW_ROOT, APPENDIX_FILE)))
old_ref = dict(appendix.parse(md(OLD_ROOT, APPENDIX_FILE)))
same_app = app_new = 0
for title in [None] + list(new_ref):
    have = asec.get(title)
    if have is None:
        err(f"приложение: нет раздела «{title}»")
        continue
    if title is not None and new_ref[title] != old_ref.get(title):
        if appendix.section(title, new_ref[title]) not in have:
            err(f"приложение: таблица «{title}» не из нового Markdown")
        app_new += 1
        continue
    if have != bsec.get(title, "").replace(*closing):
        err(f"приложение: раздел «{title}» изменился, хотя его Markdown прежний")
    else:
        same_app += 1
if len(asec) != len(new_ref) + 1:
    err(f"приложение: разделов {len(asec) - 1}, в {APPENDIX_FILE} — {len(new_ref)}")
print(f"  приложение: разделов без изменений, побайтно равных исходным: {same_app}; из нового Markdown: {app_new}")
cover_lines = (0, 5), (7, 9)
if any(orig.x[a:b] != out.x[a:b] for a, b in cover_lines):
    err("обложка изменилась не только в строках версии и даты")

parts = sorted(os.path.relpath(p, ORIG) for p in glob.glob(f"{ORIG}/**/*", recursive=True) if os.path.isfile(p))
parts_o = sorted(os.path.relpath(p, OUT) for p in glob.glob(f"{OUT}/**/*", recursive=True) if os.path.isfile(p))
added = sorted(set(parts_o) - set(parts))
if set(parts) - set(parts_o):
    err(f"из пакета пропали части: {sorted(set(parts) - set(parts_o))}")
if len(added) != new_shots or not all(re.fullmatch(r"word/media/[0-9a-f]{40}\.png", p) for p in added):
    err(f"добавлены части {added}, а новых снимков {new_shots}")
diff_parts = [p for p in parts if open(f"{ORIG}/{p}", "rb").read() != open(f"{OUT}/{p}", "rb").read()]
replaced = [media for _path, media in manifest["replaced"]]
for path, media in manifest["replaced"]:
    if hashlib.sha1(open(f"{OUT}/word/{media}", "rb").read()).digest() != \
            hashlib.sha1(open(os.path.join(NEW_ROOT, "docs", "ru", path), "rb").read()).digest():
        err(f"снимок {path}: в пакете не заменён")
allowed = {"docProps/core.xml", "word/_rels/document.xml.rels", "word/document.xml", "word/header1.xml",
           "word/numbering.xml"} | {f"word/{m}" for m in replaced}
if not set(diff_parts) <= allowed or not {"word/document.xml", "docProps/core.xml"} <= set(diff_parts):
    err(f"изменились части пакета: {sorted(diff_parts)}")
print(f"  частей пакета было {len(parts)}, добавлено {len(added)}; изменены: {', '.join(sorted(diff_parts))}")

print(f"\nошибок: {len(errors)}")
sys.exit(1 if errors else 0)
