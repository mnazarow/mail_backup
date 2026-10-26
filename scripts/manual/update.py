#!/usr/bin/env python3
"""Обновить руководство .docx до текущей версии: правки на месте, тем же оформлением.

Запускается из build.sh над распакованным руководством прежнего выпуска:

    python3 update.py UNPACKED_DIR NEW_ROOT OLD_ROOT [журнал]

NEW_ROOT — проект с новой документацией (VERSION, docs/ru, CHANGELOG.md), OLD_ROOT — docs/ru и CHANGELOG.md
того выпуска, из которого собрано руководство в UNPACKED_DIR. Что поменять, сборка находит сама:

  0. перенумерация глав (renumber.py), если в chapters.py их номера сдвинулись (вставлена новая глава);
  A. версия и дата: обложка, колонтитул, свойства документа, подпись в конце;
  B. глава «Что нового» — заново из записи CHANGELOG.md о новой версии; «глава «Название»» в тексте
     записи получает номер главы;
  C. главы из изменившихся файлов docs/ru — трёхсторонним слиянием (merge3.py): переносится только то,
     что изменилось в Markdown, остальное остаётся побайтно; новые снимки — по образцу прежних;
  F. снимки экрана, файл которых изменился (а Markdown — нет): в пакете заменяется сам рисунок;
  E. новые главы (есть в chapters.py, нет в документе) — целиком, на своё место, со снимками;
  P. приложение «Справочник параметров» — если изменился docs/ru/05-parameters-reference.md;
  D. numbering.xml — определения новых нумерованных списков.
Итог — manifest.json рядом с UNPACKED_DIR (для verify.py) и журнал. Неожиданное (глава в документе,
которой нет в chapters.py; слияние наткнулось на ручную правку документа) — код возврата не 0.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appendix  # noqa: E402
import merge3  # noqa: E402
import renumber  # noqa: E402
from chapters import (APPENDIX_FILE, APPENDIX_TITLE, CHAPTERS, CONTINUE_LISTS, MD_NUM, NOT_IN_MANUAL,  # noqa: E402
                      REFS, SHOT_CX, TABLE_WIDTHS)
from docunits import Doc, classify, units  # noqa: E402
from mdrender import Renderer, auto_widths, escape, make_link_resolver, parse, plain_links, text  # noqa: E402

UNPACKED, NEW_ROOT, OLD_ROOT = sys.argv[1:4]
LOG = open(sys.argv[4], "w", encoding="utf-8") if len(sys.argv) > 4 else sys.stdout
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
DATE_RE = re.compile(r"\d{1,2} (?:%s) \d{4} г\." % "|".join(MONTHS))
TITLES = {"Добавлено": "Добавлено", "Исправлено": "Исправлено", "Изменено": "Изменено",
          "Изменено (проверьте после обновления)": "Проверьте после обновления", "Ограничения": "Ограничения"}
IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", flags=re.M)


def log(*a):
    print(*a, file=LOG)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)


def fail(msg):
    log("ОШИБКА: " + msg)
    sys.exit(msg)


def md_path(root, fname):
    return os.path.join(root, "docs", "ru", fname)


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


DOCXML = f"{UNPACKED}/word/document.xml"
s0 = read(DOCXML)

# ---------------------------------------------------------------------------
#  Что изменилось
# ---------------------------------------------------------------------------
NEW = read(os.path.join(NEW_ROOT, "VERSION")).strip()
doc0 = Doc(DOCXML)
cover = [m for m in (re.fullmatch(r"Версия (\d+\.\d+\.\d+)", doc0.text(i).strip()) for i in range(12)) if m]
if not cover:
    fail("на обложке документа нет «Версия X.Y.Z» — это руководство MailArchiver?")
OLD = cover[0].group(1)
changelog = read(os.path.join(NEW_ROOT, "CHANGELOG.md"))
_m = re.search(rf"^## \[{re.escape(NEW)}\] — (\d{{4}})-(\d\d)-(\d\d)\s*$", changelog, flags=re.M)
if not _m:
    fail(f"в CHANGELOG.md нет строки «## [{NEW}] — ГГГГ-ММ-ДД»")
NEW_DATE = f"{int(_m.group(3))} {MONTHS[int(_m.group(2)) - 1]} {_m.group(1)} г."
NEW_ISO = f"{_m.group(1)}-{_m.group(2)}-{_m.group(3)}T00:00:00.000Z"
log(f"руководство {OLD} → {NEW} ({NEW_DATE})")

# главы в документе: «N. Название» → файл (по названию из chapters.py)
by_title = {t: f for f, (_n, t) in CHAPTERS.items()}
OLD_NUM = {}
for i in range(len(doc0.els)):
    if doc0.heading_level(i) != 1:
        continue
    h = doc0.text(i).strip()
    if h.startswith("Что нового в верси") or h == APPENDIX_TITLE:
        continue
    m = re.fullmatch(r"(\d+)\. (.+)", h)
    if not m or m.group(2) not in by_title:
        fail(f"в документе глава «{h}», которой нет в chapters.py (переименована или удалена?) — поправьте "
             f"chapters.py или документ вручную")
    OLD_NUM[by_title[m.group(2)]] = int(m.group(1))
numbers = sorted(n for n, _t in CHAPTERS.values())
if numbers != list(range(1, len(numbers) + 1)):
    fail(f"номера глав в chapters.py не 1…{len(numbers)} подряд: {numbers}")
MAPPING = {OLD_NUM[f]: CHAPTERS[f][0] for f in OLD_NUM if OLD_NUM[f] != CHAPTERS[f][0]}
NEW_FILES = sorted((f for f in CHAPTERS if f not in OLD_NUM), key=lambda f: CHAPTERS[f][0])
for f in CHAPTERS:
    if not os.path.exists(md_path(NEW_ROOT, f)):
        fail(f"в chapters.py есть {f}, а в docs/ru такого файла нет")
changed = []
for p in sorted(glob.glob(md_path(NEW_ROOT, "*.md"))):
    f = os.path.basename(p)
    if f in NOT_IN_MANUAL or f == APPENDIX_FILE:
        continue
    old = md_path(OLD_ROOT, f)
    if not os.path.exists(old) or read(old) != read(p):
        if f not in CHAPTERS:
            fail(f"docs/ru/{f} изменился (или новый), а в chapters.py его нет — впишите главу (или в NOT_IN_MANUAL)")
        changed.append(f)
FILES = [f for f in changed if f in OLD_NUM]
APPENDIX_CHANGED = read(md_path(NEW_ROOT, APPENDIX_FILE)) != read(md_path(OLD_ROOT, APPENDIX_FILE))
log(f"изменившиеся главы: {FILES}; новые главы: {NEW_FILES}; перенумерация: {MAPPING or 'нет'}; "
    f"приложение: {'меняется' if APPENDIX_CHANGED else 'прежнее'}")

# ---------------------------------------------------------------------------
#  0. Перенумерация глав и ссылок на них
# ---------------------------------------------------------------------------
renum_log = []
if MAPPING:
    s0, renum_log = renumber.renumber(s0, MAPPING)
    write(DOCXML, s0)
    log("0. перенумерация глав " + ", ".join(f"{a}→{b}" for a, b in sorted(MAPPING.items())) + ":")
    for i, old, new, t in renum_log:
        k = t.find(old.replace(" …", ""))
        log(f"   элемент {i}: «{old}» → «{new}»   …{t[max(0, k - 50):k + 60]}…")

doc = Doc(DOCXML)
s, els = doc.s, doc.els
edits = []          # (начало, конец, новый XML) — применяются с конца


def replace_range(i, j, xml):
    """Заменить элементы [i, j) на xml (i == j — вставка перед элементом i)."""
    edits.append((els[i][1], els[j][1] if j < len(els) else els[-1][2], xml))


def replace_text(i, old, new):
    x = doc.xml(i)
    assert x.count(old) == 1, (i, old, x.count(old))
    edits.append((els[i][1], els[i][2], x.replace(old, new)))


def cells(tr):
    return tuple(text(c).strip() for c in re.findall(r"<w:tc>.*?</w:tc>", tr, flags=re.S))


def md_cell(c):
    return re.sub(r"[`*]", "", plain_links(c)).replace("\\|", "|").strip()


def make_widths_for(D):
    """Ширины колонок таблицы Markdown: та же таблица уже есть в документе — её колонки (среди таблиц с тем же
    заголовком — та, у которой больше всего общих строк), иначе chapters.TABLE_WIDTHS, иначе — по содержимому."""
    def widths_for(header, rows):
        head = tuple(md_cell(h) for h in header)
        firsts = {md_cell(r[0]) for r in rows}
        best, score = None, 0
        for kind, _key, xml in D:
            if kind != "TABLE":
                continue
            trs = re.findall(r"<w:tr[ >].*?</w:tr>", xml, flags=re.S)
            if cells(trs[0]) != head:
                continue
            sc = len({cells(tr)[0] for tr in trs[1:]} & firsts)
            if sc > score:
                best, score = [int(w) for w in re.findall(r'<w:gridCol w:w="(\d+)"/>', xml)], sc
        return best or TABLE_WIDTHS.get(head) or auto_widths(header, rows)
    return widths_for


# ---------------------------------------------------------------------------
#  A. Версия и дата
# ---------------------------------------------------------------------------
i = doc.find(lambda i: doc.text(i) == f"Версия {OLD}")
j = doc.find(lambda i: DATE_RE.fullmatch(doc.text(i).strip()) is not None, i + 1, i + 4)   # строка даты под версией
old_date = doc.text(j).strip()
if OLD != NEW:
    replace_text(i, f"Версия {OLD}", f"Версия {NEW}")
if old_date != NEW_DATE:
    replace_text(j, old_date, NEW_DATE)
closing = doc.find(lambda i: doc.text(i).startswith(f"MailArchiver {OLD}. Полная интерактивная"))
if OLD != NEW:
    replace_text(closing, f"MailArchiver {OLD}.", f"MailArchiver {NEW}.")
log(f"A. обложка: «Версия {OLD}» → «Версия {NEW}», дата «{old_date}» → «{NEW_DATE}»; подпись в конце "
    f"(элемент {closing})")
for part in ("word/header1.xml", "docProps/core.xml"):
    data = read(f"{UNPACKED}/{part}")
    assert data.count(f"MailArchiver {OLD} — руководство") == 1, part
    data = data.replace(f"MailArchiver {OLD} — руководство", f"MailArchiver {NEW} — руководство")
    if part.endswith("core.xml"):
        data, n = re.subn(r"(<dcterms:modified[^>]*>)[^<]+(</dcterms:modified>)", rf"\g<1>{NEW_ISO}\g<2>", data)
        assert n == 1
    write(f"{UNPACKED}/{part}", data)
    log(f"A. {part}: MailArchiver {NEW} — руководство" + (f"; dcterms:modified {NEW_ISO}" if "core" in part else ""))
for part in sorted(glob.glob(f"{UNPACKED}/word/*.xml") + glob.glob(f"{UNPACKED}/docProps/*.xml")):
    if part.endswith(("document.xml", "header1.xml", "core.xml")):
        continue
    assert OLD not in read(part) or OLD == NEW, part        # номера версии там нет — менять нечего


# ---------------------------------------------------------------------------
#  B. «Что нового»
# ---------------------------------------------------------------------------
def chapter_refs(entry):
    """«глава «Почтовые серверы»» → «глава 16 «Почтовые серверы»» (название — начало названия главы)."""
    found = []

    def sub(m):
        word, name = m.group(1), m.group(2)
        hits = [f for f, (_n, t) in CHAPTERS.items() if t == name or t.startswith(name + ":")
                or t.startswith(name + " ")]
        if len(hits) != 1:
            return m.group(0)
        new = f"{word} {CHAPTERS[hits[0]][0]} «{name}»"
        found.append((" ".join(m.group(0).split()), new, hits[0]))
        return new
    out = re.sub(r"(?<![\w])((?:[Гг]лав[аеуыой]{1,2}))\s+«([^«»\n]+)»", sub, entry)
    for phrase, fname in REFS.items():
        rx = r"\s+".join(map(re.escape, phrase.split())) + r"(?!\s*\(гл\.)"

        def add(m, fname=fname):
            new = f"{m.group(0)} (гл. {CHAPTERS[fname][0]})"
            found.append((" ".join(m.group(0).split()), " ".join(new.split()), fname))
            return new
        out = re.sub(rx, add, out)
    return out, found


a = changelog.index(f"## [{NEW}]")
nxt = changelog.find("\n## [", a + 1)
entry = changelog[a:nxt if nxt >= 0 else len(changelog)].split("\n", 1)[1]       # без строки «## [X] — дата»


def retitle(m):
    if m.group(1).strip() not in TITLES:
        fail(f"CHANGELOG.md, запись {NEW}: неожиданный подраздел «### {m.group(1).strip()}» (известны: "
             f"{', '.join(TITLES)})")
    return "## " + TITLES[m.group(1).strip()]


entry = re.sub(r"^### (.+)$", retitle, entry, flags=re.M)
entry, ENTRY_REFS = chapter_refs(entry)
for words, new_words, fname in ENTRY_REFS:
    log(f"B. ссылка на главу: «{words}» → «{new_words}» ({fname})")
if ".md" in entry:
    fail("в записи CHANGELOG.md есть ссылки на файлы docs/ru — в «Что нового» пишите главы словами")
news = Renderer(make_link_resolver(), first_num_id=1000, symbol_font=True, keep_lead_in=True)
blocks = parse(entry)
h2s = [b["text"] for b in blocks if b["kind"] == "h2"]
assert h2s == [t for t in TITLES.values() if t in h2s], h2s          # порядок подразделов — как у прежних записей
assert blocks[0]["kind"] == "p", "запись начинается не с абзаца-вступления"
start = doc.find(lambda i: doc.heading_level(i) == 1 and doc.text(i).startswith("Что нового в верси"))
next_h1 = doc.find(lambda i: doc.heading_level(i) == 1, start + 1)
assert "<w:pageBreakBefore/>" in doc.xml(next_h1)[:200]      # следующая глава — с новой страницы (остаётся как есть)
last = next_h1
while 'w:br w:type="page"' in doc.xml(last - 1):            # разрыв страницы перед следующей главой остаётся
    last -= 1
assert not any("<w:drawing" in doc.xml(i) or "<w:pict" in doc.xml(i) for i in range(start, last))
h1 = doc.xml(start)
assert h1.count(escape(text(h1))) == 1
whats_new = [h1.replace(escape(text(h1)), f"Что нового в версии {NEW}")] + news.render(blocks)
if classify(whats_new[-1])[0] == "SP":                       # следующая глава — с новой страницы: отступ не нужен
    whats_new.pop()
replace_range(start, last, "".join(whats_new))
log(f"\nB. «Что нового»: элементы {start}..{last - 1} ({last - start}) заменены на {len(whats_new)}; блоков "
    f"Markdown: {len(blocks)}; подразделы: {h2s}")
next_num_id = news.next_num_id

# ---------------------------------------------------------------------------
#  C. Главы из изменившихся файлов
# ---------------------------------------------------------------------------
changed_sections = []
total = {}
table_report = []
tmpl_i = doc.find(lambda i: "<w:drawing" in doc.xml(i) and f'<wp:extent cx="{SHOT_CX}"' in doc.xml(i))
TMPL = doc.xml(tmpl_i)               # образец: абзац со снимком экрана той же ширины, что в документе
assert "<w:keepNext/>" in TMPL[:200], "у образца рисунка нет keepNext"
pending_images = {}                  # номер метки → (подпись, путь): рисунок регистрируется, только если попал в документ


def picture(alt, path, rid, docpr):
    w, h = png_size(md_path(NEW_ROOT, path))
    cy = round(SHOT_CX * h / w)
    x = TMPL
    ocx, ocy = re.search(r'<wp:extent cx="(\d+)" cy="(\d+)"/>', x).groups()
    assert x.count(f'cx="{ocx}" cy="{ocy}"') == 2
    x = x.replace(f'cx="{ocx}" cy="{ocy}"', f'cx="{SHOT_CX}" cy="{cy}"')
    x, n = re.subn(r'r:embed="rId\d+"', f'r:embed="{rid}"', x)
    assert n == 1
    x, n = re.subn(r'<wp:docPr id="\d+"', f'<wp:docPr id="{docpr}"', x)
    assert n == 1
    return x


def lazy_image(alt, path):
    """Абзац с рисунком по образцу; связь и номер docPr — метки, их подставит шаг «рисунки» после слияния."""
    k = len(pending_images)
    pending_images[k] = (alt, path)
    return picture(alt, path, f"@@RID{k}@@", f"@@DPR{k}@@")


for fname in FILES:
    num, title = CHAPTERS[fname]
    old_md, new_md = read(md_path(OLD_ROOT, fname)), read(md_path(NEW_ROOT, fname))
    a, b = doc.chapter_range(f"{num}. ")
    assert doc.text(a).strip() == f"{num}. {title}", (doc.text(a), num, title)
    D = units([doc.xml(i) for i in range(a + 1, b)])
    widths_for = make_widths_for(D)

    def rendered(md, first_num_id, with_images=False, fname=fname):
        blocks = [b for b in parse(md) if b["kind"] != "h1"]
        r = Renderer(make_link_resolver([b["text"] for b in blocks if b["kind"] in ("h2", "h3")], own=fname),
                     first_num_id, symbol_font=True, keep_lead_in=True)
        r.continue_lists = fname in CONTINUE_LISTS
        if with_images:
            r.image = lazy_image
        return units(r.render(blocks, widths_for)), r, blocks

    O, _, old_blocks = rendered(old_md, 5000)
    N, rn, new_blocks = rendered(new_md, next_num_id, with_images=True)
    next_num_id = rn.next_num_id
    log(f"\nC. {fname} → глава {num} «{title}»: элементы {a + 1}..{b - 1}; единиц O/D/N: {len(O)}/{len(D)}/{len(N)}")

    def dlog(msg, a=a):
        log(re.sub(r"D\[(\d+)(?=[\]:])", lambda m: f"D[{int(m.group(1)) + a + 1}", msg))

    merged, stats = merge3.merge(O, D, N, log=dlog, allow_new_images=True)
    merged = merge3.fix_ordered_lists(merged, classify)
    log(f"   итог: {stats}")
    if stats["conflict"]:
        fail(f"{fname}: документ расходился с прежним Markdown в изменившемся месте (ручная правка .docx?) — "
             f"подробности в журнале")
    for k, v in stats.items():
        total[k] = total.get(k, 0) + v

    # заменённые таблицы: сколько строк осталось побайтно прежними (полосы не в счёт)
    SHD = '<w:shd w:fill="F7F9FC" w:color="auto" w:val="clear"/>'

    def rows_of(xml, strip_shd=True):
        rows = re.findall(r"<w:tr[ >].*?</w:tr>", xml, flags=re.S)
        return [r.replace(SHD, "") for r in rows] if strip_shd else rows
    kept = {idx for src, idx, _ in merged if src == "D"}
    gone = [D[i][2] for i in range(len(D)) if i not in kept and D[i][0] == "TABLE"]
    for src, idx, xml in merged:
        if src == "N" and N[idx][0] == "TABLE":
            new_rows = rows_of(xml)
            for old_xml in gone:
                old_rows = rows_of(old_xml)
                if old_rows[0] == new_rows[0]:
                    same = sum(1 for r in new_rows[1:] if r in old_rows)
                    grid_same = re.findall(r'<w:gridCol w:w="(\d+)"/>', xml) == \
                        re.findall(r'<w:gridCol w:w="(\d+)"/>', old_xml)
                    msg = (f"   таблица «{text(new_rows[0])}»: строк было {len(old_rows) - 1}, стало "
                           f"{len(new_rows) - 1}; побайтно прежних {same}, новых и изменённых {len(new_rows) - 1 - same}"
                           f", убрано {sum(1 for r in old_rows[1:] if r not in new_rows)}; ширины прежние: {grid_same}")
                    log(msg)
                    table_report.append(msg.strip())
                    if not grid_same:
                        fail(f"{fname}: у таблицы «{text(new_rows[0])}» изменились ширины колонок")

    # правки: всё, что между оставленными единицами документа
    pos, pending = 0, []
    for src, idx, xml in merged + [("D", len(D), "")]:
        if src == "N":
            assert "<!--IMG" not in xml
            pending.append(xml)
            continue
        assert idx >= pos, "единицы документа переставлены"
        if idx > pos or pending:
            replace_range(a + 1 + pos, a + 1 + idx, "".join(pending))
        pos, pending = idx + 1, []

    def by_section(blocks):
        out, cur = {}, None
        for blk in blocks:
            if blk["kind"] == "h2":
                cur = re.sub(r"[`*]", "", blk["text"]).strip()
            out.setdefault(cur, []).append(" ".join(" ".join(blk["raw"]).split()))
        return out
    so, sn = by_section(old_blocks), by_section(new_blocks)
    for sec in sn:
        if so.get(sec) != sn[sec]:
            changed_sections.append((fname, sec, sec in so))
    for sec in [sec for sec in so if sec not in sn]:
        log(f"   раздел прежнего Markdown «{sec}» в новом не найден (переименован?)")
        changed_sections.append((fname, sec, "removed"))

log(f"\nC. всего: {total}")

# ---------------------------------------------------------------------------
#  P. Приложение «Справочник параметров»
# ---------------------------------------------------------------------------
appendix_changes = []
if APPENDIX_CHANGED:
    old_ref = appendix.parse(read(md_path(OLD_ROOT, APPENDIX_FILE)))
    new_ref = appendix.parse(read(md_path(NEW_ROOT, APPENDIX_FILE)))
    a = doc.find_heading(1, APPENDIX_TITLE)
    b = closing                                                      # подпись в конце — после приложения
    dsec = {doc.text(i).strip(): i for i in range(a + 1, b) if doc.heading_level(i) == 2}
    old_titles, new_titles = [t for t, _ in old_ref], [t for t, _ in new_ref]
    if old_titles != list(dsec):
        fail(f"разделы приложения в документе не те, что в прежнем {APPENDIX_FILE}")
    if [t for t in new_titles if t in old_titles] != old_titles:
        fail("порядок прежних разделов приложения изменился или раздел убран — так сборка не умеет")
    for k, (title, params) in enumerate(new_ref):
        if title in dsec:
            i = dsec[title]
            assert doc.xml(i) == appendix.heading(title) and doc.xml(i + 2) == appendix.SPACER
            old_params = dict(old_ref)[title]
            if old_params == params:
                continue
            if doc.xml(i + 1) != appendix.table(old_params):
                fail(f"таблица приложения «{title}» в документе не из прежнего Markdown (ручная правка?)")
            replace_range(i + 1, i + 2, appendix.table(params))
            on, nn = [p["name"] for p in old_params], [p["name"] for p in params]
            added = [n for n in nn if n not in on]
            changed_p = [p["name"] for p in params if p["name"] in on and p != old_params[on.index(p["name"])]]
            removed = [n for n in on if n not in nn]
            appendix_changes.append((title, added, changed_p, removed))
            log(f"\nP. приложение, «{title}»: параметров было {len(on)}, стало {len(nn)}; добавлено {added}; "
                f"изменено {changed_p}; убрано {removed}")
        else:
            nxt_t = next((t for t in new_titles[k + 1:] if t in dsec), None)
            at = dsec[nxt_t] if nxt_t else b
            replace_range(at, at, appendix.section(title, params))
            appendix_changes.append((title, [p["name"] for p in params], [], []))
            log(f"\nP. приложение: новый раздел «{title}» ({len(params)} параметров) — перед «{nxt_t or 'подписью'}»")

# ---------------------------------------------------------------------------
#  применяем с конца, проверив, что правки не пересекаются
# ---------------------------------------------------------------------------
edits.sort(key=lambda e: (e[0], e[1]))
for (a1, b1, _x), (a2, b2, _y) in zip(edits, edits[1:]):
    assert b1 <= a2 and not (a1 == b1 == a2 == b2), "правки пересекаются"
for a, b, xml in reversed(edits):
    s = s[:a] + xml + s[b:]
n_edits = len(edits)

# ---------------------------------------------------------------------------
#  Рисунки: новые снимки изменившихся глав — файл в word/media, связь, номер docPr
# ---------------------------------------------------------------------------
rels_path = f"{UNPACKED}/word/_rels/document.xml.rels"
rels = read(rels_path)
next_rid = max(int(x) for x in re.findall(r'Id="rId(\d+)"', rels)) + 1
next_docpr = max(int(x) for x in re.findall(r'<wp:docPr id="(\d+)"', s)) + 1


def add_media(path):
    """PNG из docs/ru → word/media (имя — SHA-1 содержимого) и связь; → (rId, путь в пакете)."""
    global next_rid, rels
    data = open(md_path(NEW_ROOT, path), "rb").read()
    media = f"media/{hashlib.sha1(data).hexdigest()}.png"
    if os.path.exists(f"{UNPACKED}/word/{media}"):
        fail(f"снимок {path} уже есть в пакете ({media}) — один снимок дважды?")
    shutil.copyfile(md_path(NEW_ROOT, path), f"{UNPACKED}/word/{media}")
    rid = f"rId{next_rid}"
    next_rid += 1
    assert f'Id="{rid}"' not in rels
    rels = rels.replace("</Relationships>", f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/'
                        f'officeDocument/2006/relationships/image" Target="{media}"/></Relationships>')
    return rid, media


images = []
for k in sorted({int(x) for x in re.findall(r"@@RID(\d+)@@", s)}):
    alt, path = pending_images[k]
    assert s.count(f"@@RID{k}@@") == 1 and s.count(f"@@DPR{k}@@") == 1, k
    rid, media = add_media(path)
    s = s.replace(f"@@RID{k}@@", rid).replace(f"@@DPR{k}@@", str(next_docpr))
    images.append((alt, path, media, rid, next_docpr))
    log(f"рисунок {path} {png_size(md_path(NEW_ROOT, path))} → word/{media}, {rid}, docPr {next_docpr}; «{alt}»")
    next_docpr += 1
assert "@@" not in s
write(rels_path, rels)
assert 'Extension="png"' in read(f"{UNPACKED}/[Content_Types].xml")

# ---------------------------------------------------------------------------
#  F. Обновлённые снимки (Markdown прежний, файл другой): рисунок в пакете — по совпадению пикселей с прежним
# ---------------------------------------------------------------------------
from PIL import Image, ImageChops  # noqa: E402


def same_pixels(p1, p2):
    a_, b_ = Image.open(p1), Image.open(p2)
    return a_.size == b_.size and ImageChops.difference(a_.convert("RGB"), b_.convert("RGB")).getbbox() is None


replaced = []
shots = sorted({path for f in CHAPTERS if f in OLD_NUM for _alt, path in IMG_RE.findall(read(md_path(NEW_ROOT, f)))})
media_files = sorted(glob.glob(f"{UNPACKED}/word/media/*.png"))
for path in shots:
    old_png, new_png = md_path(OLD_ROOT, path), md_path(NEW_ROOT, path)
    if not os.path.exists(old_png) or same_pixels(old_png, new_png):
        continue
    hits = [m for m in media_files if same_pixels(m, old_png)]
    if not hits:
        log(f"F. {path}: снимок изменился, но прежнего в пакете нет (в руководстве не используется?) — пропуск")
        continue
    if len(hits) != 1:
        fail(f"снимок {path}: прежний рисунок найден в пакете {len(hits)} раз")
    if png_size(new_png) != png_size(old_png):
        fail(f"{path}: размер снимка изменился {png_size(old_png)} → {png_size(new_png)} — так сборка не умеет "
             f"(нужен и новый размер рисунка в документе)")
    media = os.path.relpath(hits[0], f"{UNPACKED}/word")
    assert rels.count(f'Target="{media}"') == 1, media
    shutil.copyfile(new_png, hits[0])
    replaced.append((path, media))
    log(f"F. {path}: word/{media} заменён новым снимком")
write(DOCXML, s)

# ---------------------------------------------------------------------------
#  E. Новые главы — каждая перед первой главой с бо́льшим номером (или перед приложением)
# ---------------------------------------------------------------------------
new_images = []
new_chapters = []
for fname in NEW_FILES:
    doc = Doc(DOCXML)
    s = doc.s
    num, title = CHAPTERS[fname]
    blocks = parse(read(md_path(NEW_ROOT, fname)))
    want_h1 = f"{MD_NUM[fname]}. {title}" if fname in MD_NUM else title
    if blocks[0]["kind"] != "h1" or re.sub(r"^\d+\. ", "", blocks[0]["text"]) != title:
        fail(f"{fname}: первая строка — не «# {want_h1}» (название должно совпадать с chapters.py)")
    blocks = blocks[1:]
    used_docpr = {int(x) for x in re.findall(r'<wp:docPr id="(\d+)"', s)}
    next_docpr = max(used_docpr) + 1

    def image(alt, path):
        global next_docpr
        rid, media = add_media(path)
        x = picture(alt, path, rid, next_docpr)
        new_images.append((alt, path, media, rid, next_docpr))
        next_docpr += 1
        return x

    r = Renderer(make_link_resolver([b["text"] for b in blocks if b["kind"] in ("h2", "h3")], own=fname),
                 next_num_id, symbol_font=True, keep_lead_in=True)
    r.continue_lists = fname in CONTINUE_LISTS
    r.image = image
    chapter = [r.heading(1, f"{num}. {title}", page_break=True)] + r.render(blocks, make_widths_for([]))
    next_num_id = r.next_num_id
    assert "<!--" not in "".join(chapter)

    def later(i, num=num):
        if doc.heading_level(i) != 1:
            return False
        t = doc.text(i).strip()
        m = re.match(r"(\d+)\. ", t)
        return t == APPENDIX_TITLE or (m is not None and int(m.group(1)) > num)
    at = doc.find(later)
    s = s[:doc.els[at][1]] + "".join(chapter) + s[doc.els[at][1]:]
    write(DOCXML, s)
    h2 = [text(x) for x in chapter if 'Heading2' in x[:60]]
    new_chapters.append((fname, f"{num}. {title}"))
    log(f"\nE. новая глава «{num}. {title}» перед «{doc.text(at)}»: элементов {len(chapter)}, разделов H2 {len(h2)}")
write(rels_path, rels)
for alt, path, media, rid, dpr in new_images:
    log(f"   рисунок {path} → word/{media}, {rid}, docPr {dpr}; подпись «{alt}»")

# ---------------------------------------------------------------------------
#  D. Нумерованные списки: новые numId подряд, определения — в numbering.xml
# ---------------------------------------------------------------------------
s = read(DOCXML)
numbering = read(f"{UNPACKED}/word/numbering.xml")
known = {int(n) for n in re.findall(r'<w:num w:numId="(\d+)"', numbering)}
first_free = max(known) + 1
order = []
for m in re.finditer(r'<w:numId w:val="(\d+)"/>', s):
    n = int(m.group(1))
    if n not in known and n not in order:
        order.append(n)
assert all(n >= 1000 for n in order), order
renum = {n: first_free + k for k, n in enumerate(order)}
if renum:
    s = re.sub(r'<w:numId w:val="(\d+)"/>',
               lambda m: f'<w:numId w:val="{renum.get(int(m.group(1)), int(m.group(1)))}"/>', s)
    decimal = re.search(r'<w:abstractNum w:abstractNumId="(\d+)"[^>]*>(?:(?!</w:abstractNum>).)*?<w:lvl w:ilvl="0"'
                        r'(?:(?!</w:lvl>).)*?<w:numFmt w:val="decimal"/>', numbering, flags=re.S).group(1)
    defs = "".join(f'<w:num w:numId="{n}"><w:abstractNumId w:val="{decimal}"/><w:lvlOverride w:ilvl="0">'
                   f'<w:startOverride w:val="1"/></w:lvlOverride></w:num>' for n in sorted(renum.values()))
    assert numbering.count("</w:numbering>") == 1
    write(f"{UNPACKED}/word/numbering.xml", numbering.replace("</w:numbering>", defs + "</w:numbering>"))
log(f"\nD. новых нумерованных списков: {len(renum)}")

assert "<!--" not in s
write(DOCXML, s)
t = text(s)
log(f"\nправок: {n_edits}; «{OLD}» в тексте: {t.count(OLD)}; «{NEW}»: {t.count(NEW)}")
print(f"{OLD} → {NEW}: правок {n_edits}; изменившихся глав {len(FILES)}, новых {len(NEW_FILES)}; слияние: {total}; "
      f"новых рисунков {len(images)} + {len(new_images)}; заменено снимков {len(replaced)}; приложение: "
      f"{len(appendix_changes)} разд.; новых нумерованных списков {len(renum)}")
json.dump({"old": OLD, "new": NEW, "files": FILES, "new_files": NEW_FILES, "mapping": MAPPING,
           "appendix_changed": APPENDIX_CHANGED, "appendix": appendix_changes, "entry_refs": ENTRY_REFS,
           "changed_sections": changed_sections, "tables": table_report,
           "images": images, "new_chapter_images": new_images, "replaced": replaced},
          open(f"{UNPACKED}/../manifest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
