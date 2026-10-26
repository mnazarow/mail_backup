#!/usr/bin/env python3
"""Мини-Markdown → XML WordprocessingML в оформлении руководства MailArchiver.

Документ собран генератором с прямым форматированием (без стилей знаков), поэтому
новые абзацы, списки, таблицы, блоки кода и врезки строятся теми же XML-шаблонами,
что уже есть в файле. Шаблоны сверены с существующими элементами побайтно
(это и проверяет сборка: неизменные главы после неё — побайтно прежние).

Что умеет разбор: заголовки ## и ###, абзацы, маркированные списки (два уровня),
нумерованные списки, блоки кода ``` (в том числе внутри пункта списка), таблицы
(«\\|» в ячейке — знак |), цитаты > (врезки, со списками внутри), картинки
(только как метка: сам рисунок берётся из документа), **жирный**, `код`, ссылки.
"""
import html
import re
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
#  Главы руководства: файл Markdown → номер и название главы (chapters.py)
# ---------------------------------------------------------------------------
from chapters import CHAPTERS, MD_NUM, TABLE_WIDTH  # noqa: E402


def section_mapper(fname, chapters=None):
    """Номера разделов файла fname («17.5») → номера разделов главы руководства («6.5»)."""
    chapters = chapters or CHAPTERS
    if fname not in MD_NUM or fname not in chapters:
        return lambda t: t
    md, doc = MD_NUM[fname], chapters[fname][0]
    rx = re.compile(rf"(?<![\d.]){md}\.(\d+)(?![\d]|\.\d)")
    return lambda t: rx.sub(lambda m: f"{doc}.{m.group(1)}", t)



def text(x):
    """Текст элемента: теги убраны, сущности XML раскрыты."""
    return html.unescape(re.sub(r"<[^>]+>", "", x))


# ---------------------------------------------------------------------------
#  Ссылки
# ---------------------------------------------------------------------------
LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def slug(title):
    """Якорь заголовка, как его строит GitHub: строчные, без знаков, пробелы → дефисы."""
    t = re.sub(r"[`*]", "", title).strip().lower()
    t = re.sub(r"[^\w\- ]", "", t, flags=re.U)
    return t.replace(" ", "-")


def make_link_resolver(headings=(), own=None, chapters=None):
    """Замена ссылки Markdown простым текстом — по правилам, принятым в руководстве.

    [NN-имя.md](NN-имя.md), раздел «…»   → глава N, раздел «…»
    [NN-имя.md](NN-имя.md), раздел X.Y   → глава N, раздел N.Y
    [NN-имя.md](NN-имя.md)               → глава N «Название главы»
    Глава документации [NN-имя.md](…)    → Глава документации «Название» (в руководстве — глава N)
    [NN-имя.md, раздел X.Y](NN-имя.md)   → раздел N.Y (гл. N)
    [X.Y](NN-имя.md)                     → разд. N.Y
    [текст](NN-имя.md)                   → текст (гл. N)
    [ниже](#якорь)                       → раздел «Заголовок» ниже
    остальное                            → текст ссылки

    own — файл, из которого текст: его собственные номера разделов («17.5») становятся номерами
    разделов главы («6.5») — в тексте вне ссылок.
    """
    chapters = chapters or CHAPTERS
    anchors = {slug(h): re.sub(r"[`*]", "", h).strip() for h in headings}
    own_map = section_mapper(own, chapters) if own else (lambda t: t)
    TAIL = re.compile(r",\s+раздел\s+(\d+\.\d+)")

    def resolve(md):
        out, pos = [], 0
        for m in LINK.finditer(md):
            if m.start() < pos:
                continue
            out.append(own_map(md[pos:m.start()]))
            label, target = m.group(1), m.group(2)
            rest = md[m.end():]
            before = "".join(out)
            fname = target.split("#")[0].split("/")[-1]
            pos = m.end()
            if target.startswith("#"):
                title = anchors.get(target[1:])
                if title and label.strip().lower() in ("ниже", "выше"):
                    out.append(f"раздел «{title}» {label.strip()}")
                else:
                    out.append(own_map(label))
                continue
            if fname == "05-parameters-reference.md" and label == fname:
                case = "приложении" if re.search(r"(^|\s)в\s*$", before) else "приложение"
                out.append(f"{case} «Справочник параметров»")
                continue
            if fname in chapters:
                num, title = chapters[fname]
                plain = label.replace("docs/ru/", "")
                sm = re.fullmatch(rf"{re.escape(fname)},\s+раздел\s+(\d+\.\d+)", plain)
                if plain == fname:
                    t = TAIL.match(rest)
                    if t:
                        out.append(f"глава {num}, раздел {section_mapper(fname, chapters)(t.group(1))}")
                        pos = m.end() + t.end()
                    elif re.match(r",\s+(раздел\s+)?«", rest):
                        out.append(f"глава {num}")
                    elif re.search(r"[Гг]лава документации\s*$", before):
                        out.append(f"«{title}» (в руководстве — глава {num})")
                    else:
                        out.append(f"глава {num} «{title}»")
                elif sm:
                    out.append(f"раздел {section_mapper(fname, chapters)(sm.group(1))} (гл. {num})")
                elif re.fullmatch(r"\d+\.\d+", plain):
                    out.append(f"разд. {section_mapper(fname, chapters)(plain)}")
                elif re.search(r"\d+-[\w-]+\.md", plain):          # [роли — 8-security.md](08-security.md)
                    out.append(f"глава {num} «{title}»")
                elif re.search(r"\d+\.\d+", plain):                 # [сотрудники, 7.8](07-employees.md)
                    out.append(section_mapper(fname, chapters)(label))
                else:
                    out.append(f"{label} (гл. {num})")
                continue
            out.append(label)
        out.append(own_map(md[pos:]))
        return "".join(out)
    return resolve


def plain_links(md):
    return LINK.sub(r"\1", md)


# ---------------------------------------------------------------------------
#  Шаблоны
# ---------------------------------------------------------------------------
CODE_FONT = '<w:rFonts w:ascii="Consolas" w:cs="Consolas" w:eastAsia="Consolas" w:hAnsi="Consolas"/>'


SYMBOL_FONT = ('<w:rFonts w:ascii="Segoe UI Symbol" w:cs="Segoe UI Symbol" w:eastAsia="Segoe UI Symbol" '
               'w:hAnsi="Segoe UI Symbol"/>')
# Значки ✓ ✗ ⚠ и подобные (U+2600–U+27BF): в Calibri их нет. Word берёт их из Segoe UI Symbol сам,
# а LibreOffice при подстановке шрифта внутри общего прогона путает знаки (вместо ✓ выходит ⚠)
# и наезжает ими на соседний текст. Поэтому значок — отдельный прогон с названным шрифтом.
SYMBOLS = re.compile("([\u2600-\u27bf]+)")


def run(t, sz=21, bold=False, code=False, color=None, symbols=False):
    if not t:
        return ""
    if symbols and not code and SYMBOLS.search(t):
        return "".join(run(part, sz, bold, False, color, symbols=False) if not SYMBOLS.fullmatch(part)
                       else _run(part, sz, bold, color, SYMBOL_FONT) for part in SYMBOLS.split(t) if part)
    props = ""
    if code:
        props += CODE_FONT + '<w:color w:val="24408F"/>'
        sz -= 3 if sz >= 21 else 2
    elif bold:
        props += "<w:b/><w:bCs/>"
    if color and not code:
        props += f'<w:color w:val="{color}"/>'
    return (f'<w:r><w:rPr>{props}<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')


def _run(t, sz, bold, color, font):
    props = font + ("<w:b/><w:bCs/>" if bold else "") + (f'<w:color w:val="{color}"/>' if color else "")
    return (f'<w:r><w:rPr>{props}<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>'
            f'<w:t xml:space="preserve">{escape(t)}</w:t></w:r>')


class Renderer:
    """Отрисовка блоков; хранит разрешение ссылок и счётчик нумерованных списков."""

    def __init__(self, resolve=plain_links, first_num_id=30, symbol_font=False, keep_lead_in=False):
        """symbol_font — значки отдельным прогоном со шрифтом Segoe UI Symbol;
        keep_lead_in — абзац-подводка («…:») не отрывается от блока кода, таблицы или списка за ним.
        Исходный генератор не делал ни того, ни другого: для сверки шаблонов оба выключены."""
        self.resolve = resolve
        self.next_num_id = first_num_id
        self.used_num_ids = []
        self.symbol_font = symbol_font
        self.keep_lead_in = keep_lead_in
        self.continue_lists = False        # абзац с отступом внутри нумерованного списка не начинает список заново
        self.image = None                  # image(alt, path) → XML абзаца с рисунком (иначе — метка <!--IMG-->)

    # -- строчная разметка --------------------------------------------------
    def inline(self, md, sz=21, bold=False, color=None, resolved=False):
        if not resolved:
            md = self.resolve(md)
        out = []
        for part in re.split(r"(\*\*.+?\*\*|`[^`]+`)", md):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                out.append(self.inline(part[2:-2], sz, True, color, resolved=True))
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append(run(part[1:-1], sz, code=True))
            else:
                out.append(run(part, sz, bold, color=color, symbols=self.symbol_font))
        return "".join(out)

    # -- абзацы и заголовки -------------------------------------------------
    def para(self, md, keep_next=False):
        keep = "<w:keepNext/>" if keep_next else ""
        return f'<w:p><w:pPr>{keep}<w:spacing w:after="120" w:line="276"/></w:pPr>{self.inline(md)}</w:p>'

    @staticmethod
    def spacer(after):
        return f'<w:p><w:pPr><w:spacing w:after="{after}"/></w:pPr></w:p>'

    def heading(self, level, title, page_break=False):
        color, sz = {1: ("1C2430", 32), 2: ("2F6FED", 26), 3: ("1C2430", 22)}[level]
        t = re.sub(r"[`*]", "", self.resolve(title)).strip()
        brk = "<w:pageBreakBefore/>" if page_break else ""
        return (f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/>{brk}</w:pPr><w:r><w:rPr><w:b/><w:bCs/>'
                f'<w:color w:val="{color}"/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>'
                f'<w:t xml:space="preserve">{escape(t)}</w:t></w:r></w:p>')

    def list_item(self, md, level=0, num_id=2):
        return ('<w:p><w:pPr><w:pStyle w:val="ListParagraph"/><w:numPr>'
                f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
                f'<w:spacing w:after="60" w:line="268"/></w:pPr>{self.inline(md)}</w:p>')

    def new_num_id(self):
        n = self.next_num_id
        self.next_num_id += 1
        self.used_num_ids.append(n)
        return n

    # -- блок кода ----------------------------------------------------------
    @staticmethod
    def code_block(lines):
        border = "".join(f'<w:{side} w:val="single" w:color="0D1017" w:sz="4"/>'
                         for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
        body = ""
        for line in lines:
            body += ('<w:p><w:pPr><w:spacing w:after="0" w:line="250"/></w:pPr><w:r><w:rPr>' + CODE_FONT +
                     '<w:color w:val="E7ECF3"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr>'
                     f'<w:t xml:space="preserve">{escape(line) if line.strip() else " "}</w:t></w:r></w:p>')
        return (f'<w:tbl><w:tblPr><w:tblW w:type="dxa" w:w="{TABLE_WIDTH}"/><w:tblBorders>{border}</w:tblBorders>'
                f'</w:tblPr><w:tblGrid><w:gridCol w:w="{TABLE_WIDTH}"/></w:tblGrid><w:tr><w:tc><w:tcPr>'
                f'<w:tcW w:type="dxa" w:w="{TABLE_WIDTH}"/><w:shd w:fill="0D1017" w:color="auto" w:val="clear"/>'
                '<w:tcMar><w:top w:type="dxa" w:w="110"/><w:left w:type="dxa" w:w="160"/>'
                '<w:bottom w:type="dxa" w:w="110"/><w:right w:type="dxa" w:w="160"/></w:tcMar></w:tcPr>'
                f'{body}</w:tc></w:tr></w:tbl>')

    # -- таблица данных -----------------------------------------------------
    TCMAR = ('<w:tcMar><w:top w:type="dxa" w:w="60"/><w:left w:type="dxa" w:w="100"/>'
             '<w:bottom w:type="dxa" w:w="60"/><w:right w:type="dxa" w:w="100"/></w:tcMar><w:vAlign w:val="center"/>')

    def table_row(self, cells, widths, shaded):
        shd = '<w:shd w:fill="F7F9FC" w:color="auto" w:val="clear"/>' if shaded else ""
        out = '<w:tr><w:trPr><w:cantSplit/></w:trPr>'
        for cell, width in zip(cells, widths):
            out += (f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{width}"/>{shd}{self.TCMAR}</w:tcPr>'
                    f'<w:p><w:pPr><w:spacing w:line="252"/></w:pPr>{self.inline(cell.strip(), 18)}</w:p></w:tc>')
        return out + "</w:tr>"

    def table(self, header, rows, widths):
        assert sum(widths) == TABLE_WIDTH and len(widths) == len(header), (widths, header)
        assert all(len(r) == len(header) for r in rows), [r for r in rows if len(r) != len(header)]
        border = "".join(f'<w:{side} w:val="single" w:color="D9DEE8" w:sz="4"/>'
                         for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
        out = (f'<w:tbl><w:tblPr><w:tblW w:type="dxa" w:w="{TABLE_WIDTH}"/><w:tblBorders>{border}</w:tblBorders>'
               '</w:tblPr><w:tblGrid>' + "".join(f'<w:gridCol w:w="{w}"/>' for w in widths) + '</w:tblGrid>')
        out += '<w:tr><w:trPr><w:tblHeader/></w:trPr>'
        for cell, width in zip(header, widths):
            head = re.sub(r"[`*]", "", self.resolve(cell)).strip()
            out += (f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{width}"/><w:shd w:fill="2F6FED" w:color="auto" '
                    f'w:val="clear"/>{self.TCMAR}</w:tcPr><w:p>'
                    + (f'<w:r><w:rPr><w:b/><w:bCs/><w:color w:val="FFFFFF"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
                       f'</w:rPr><w:t xml:space="preserve">{escape(head)}</w:t></w:r>' if head else "")
                    + '</w:p></w:tc>')
        out += "</w:tr>"
        for n, row in enumerate(rows):
            out += self.table_row(row, widths, n % 2 == 1)
        return out + "</w:tbl>"

    # -- врезка -------------------------------------------------------------
    def callout(self, title, paragraphs):
        border = ('<w:top w:val="single" w:color="EDEDED" w:sz="4"/><w:left w:val="single" w:color="2F6FED" w:sz="24"/>'
                  '<w:bottom w:val="single" w:color="EDEDED" w:sz="4"/><w:right w:val="single" w:color="EDEDED" '
                  'w:sz="4"/><w:insideH w:val="single" w:color="EDEDED" w:sz="4"/><w:insideV w:val="single" '
                  'w:color="EDEDED" w:sz="4"/>')
        body = ""
        if title:
            body += f'<w:p><w:pPr><w:spacing w:after="60"/></w:pPr>{run(title, 21, True, color="2F6FED")}</w:p>'
        for line in paragraphs:
            body += f'<w:p><w:pPr><w:spacing w:after="40" w:line="268"/></w:pPr>{self.inline(line)}</w:p>'
        return (f'<w:tbl><w:tblPr><w:tblW w:type="dxa" w:w="{TABLE_WIDTH}"/><w:tblBorders>{border}</w:tblBorders>'
                f'</w:tblPr><w:tblGrid><w:gridCol w:w="{TABLE_WIDTH}"/></w:tblGrid><w:tr><w:tc><w:tcPr>'
                f'<w:tcW w:type="dxa" w:w="{TABLE_WIDTH}"/><w:shd w:fill="ECF2FE" w:color="auto" w:val="clear"/>'
                '<w:tcMar><w:top w:type="dxa" w:w="80"/><w:left w:type="dxa" w:w="160"/>'
                '<w:bottom w:type="dxa" w:w="80"/><w:right w:type="dxa" w:w="160"/></w:tcMar></w:tcPr>'
                f'{body}</w:tc></w:tr></w:tbl>')

    def quote(self, lines):
        """Цитата Markdown → врезка: жирное начало — заголовок, пункты списка — отдельные абзацы с «– »."""
        paragraphs, current = [], None
        for line in lines:
            if not line.strip():
                if current is not None:
                    paragraphs.append(current)
                    current = None
                continue
            m = re.match(r"^\s*[-*] (.*)$", line)
            if m:
                if current is not None:
                    paragraphs.append(current)
                current = "– " + m.group(1).strip()
            elif current is not None:
                current += " " + line.strip()
            else:
                current = line.strip()
        if current is not None:
            paragraphs.append(current)
        title = None
        m = re.match(r"^\*\*(.+?)\*\*\s*(.*)$", paragraphs[0]) if paragraphs else None
        if m:
            title = plain_links(m.group(1)).rstrip(".").strip()
            title = re.sub(r"`", "", title)
            paragraphs = ([m.group(2)] if m.group(2) else []) + paragraphs[1:]
        return self.callout(title, paragraphs)

    # -- блоки → элементы ---------------------------------------------------
    def render(self, blocks, widths_for=None):
        """Список блоков → список XML-элементов верхнего уровня (с пустыми абзацами-отступами).

        widths_for(header, rows) → ширины колонок таблицы.
        """
        out = []
        num_id = None                      # текущий нумерованный список
        n = len(blocks)
        for i, b in enumerate(blocks):
            kind = b["kind"]
            nxt = blocks[i + 1] if i + 1 < n else None
            in_list_next = nxt is not None and (nxt["kind"] == "li" or (nxt["kind"] == "code" and nxt.get("indent")))
            if kind == "li":
                if b["ordered"] and b["level"] == 0:
                    if num_id is None:
                        num_id = self.new_num_id()
                    out.append(self.list_item(b["text"], 0, num_id))
                elif b["ordered"]:
                    out.append(self.list_item(b["text"], b["level"], num_id or self.new_num_id()))
                else:
                    out.append(self.list_item(b["text"], b["level"], 2))
                if not in_list_next:
                    out.append(self.spacer(60))
                    if not (self.continue_lists and nxt is not None and nxt["kind"] == "p" and nxt.get("indent")):
                        num_id = None
                continue
            if kind == "code":
                out.append(self.code_block(b["lines"]))
                out.append(self.spacer(100))
                if b.get("indent") and not in_list_next:
                    num_id = None
                continue
            if not (kind == "p" and b.get("indent") and self.continue_lists):
                num_id = None                  # абзац-продолжение пункта (с отступом) нумерацию не прерывает
            if kind in ("h2", "h3"):
                out.append(self.heading(int(kind[1]), b["text"]))
            elif kind == "p":
                lead = (self.keep_lead_in and b["text"].rstrip().endswith(":") and nxt is not None
                        and nxt["kind"] in ("code", "table", "li"))
                out.append(self.para(b["text"], keep_next=lead))
            elif kind == "table":
                widths = widths_for(b["header"], b["rows"]) if widths_for else auto_widths(b["header"], b["rows"])
                out.append(self.table(b["header"], b["rows"], widths))
                out.append(self.spacer(120))
            elif kind == "quote":
                out.append(self.quote(b["lines"]))
                out.append(self.spacer(100))
            elif kind == "img":
                # рисунок: если задан image(), — абзац с рисунком; иначе метка для сопоставления с документом
                out.append(self.image(b["alt"], b["path"]) if self.image else f'<w:p><!--IMG:{escape(b["alt"])}--></w:p>')
                out.append('<w:p><w:pPr><w:spacing w:after="160"/><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:i/>'
                           '<w:iCs/><w:color w:val="5A6675"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>'
                           f'<w:t xml:space="preserve">{escape(b["alt"])}</w:t></w:r></w:p>')
            elif kind == "h1":
                out.append(self.heading(1, b["text"], page_break=True))
            else:
                raise ValueError(kind)
        return out


# ---------------------------------------------------------------------------
#  Разбор Markdown на блоки
# ---------------------------------------------------------------------------
def cells_of(line):
    """Строка таблицы Markdown → ячейки; «\\|» внутри ячейки — это знак |."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    parts = re.split(r"(?<!\\)\|", line)
    return [p.replace("\\|", "|").strip() for p in parts]


LIST_RE = re.compile(r"^(\s*)([-*]|\d+\.) +(.*)$")
FENCE_RE = re.compile(r"^(\s*)```")


def parse(md):
    """Markdown → список блоков (dict с ключом kind и исходными строками raw)."""
    lines = md.split("\n")
    blocks = []
    i, n = 0, len(lines)
    list_indent = None                      # отступ продолжения текущего пункта списка
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        start = i
        m = FENCE_RE.match(line)
        if m:
            indent = len(m.group(1))
            i += 1
            code = []
            while i < n and not FENCE_RE.match(lines[i]):
                code.append(lines[i][indent:] if lines[i][:indent].strip() == "" else lines[i].lstrip())
                i += 1
            i += 1                           # закрывающая ```
            blocks.append({"kind": "code", "lines": code, "indent": indent, "raw": lines[start:i]})
            continue
        if not line.startswith(" "):
            list_indent = None
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            i += 1                           # горизонтальная черта: в руководстве её нет
            continue
        m = re.match(r"^(#{1,3}) (.*)$", line)
        if m:
            blocks.append({"kind": f"h{len(m.group(1))}", "text": m.group(2).strip(), "raw": [line]})
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(cells_of(lines[i]))
                i += 1
            assert len(rows) >= 2 and all(re.fullmatch(r":?-+:?", c) for c in rows[1]), rows[:2]
            blocks.append({"kind": "table", "header": rows[0], "rows": rows[2:], "raw": lines[start:i]})
            continue
        if line.startswith(">"):
            quote = []
            while i < n and lines[i].startswith(">"):
                quote.append(lines[i][2:] if lines[i].startswith("> ") else lines[i][1:])
                i += 1
            blocks.append({"kind": "quote", "lines": quote, "raw": lines[start:i]})
            continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if m:
            blocks.append({"kind": "img", "alt": m.group(1), "path": m.group(2), "raw": [line]})
            i += 1
            continue
        m = LIST_RE.match(line)
        if m:
            indent = len(m.group(1))
            item = [m.group(3).strip()]
            i += 1
            while i < n and lines[i].strip() and lines[i].startswith(" ") and not LIST_RE.match(lines[i]) \
                    and not FENCE_RE.match(lines[i]):
                item.append(lines[i].strip())
                i += 1
            blocks.append({"kind": "li", "text": " ".join(item), "level": 1 if indent >= 2 else 0,
                           "ordered": m.group(2)[0].isdigit(), "raw": lines[start:i]})
            continue
        para = [line.strip()]
        indent = len(line) - len(line.lstrip())
        i += 1
        while i < n and lines[i].strip() and not lines[i].startswith(("|", "#", ">", "![")) \
                and not LIST_RE.match(lines[i]) and not FENCE_RE.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"kind": "p", "text": " ".join(para), "indent": indent, "raw": lines[start:i]})
    return blocks


def sections(blocks):
    """Блоки главы → [(заголовок H2 или None для вступления, блоки раздела вместе с заголовком)]."""
    out = [(None, [])]
    for b in blocks:
        if b["kind"] == "h1":
            continue
        if b["kind"] == "h2":
            out.append((b["text"], [b]))
        else:
            out[-1][1].append(b)
    return out


# ---------------------------------------------------------------------------
#  Ширины колонок
# ---------------------------------------------------------------------------
def _plain(cell):
    return re.sub(r"[`*]", "", plain_links(cell)).replace("\\|", "|")


def _len16(t):
    """Длина в единицах UTF-16 (так считал исходный генератор: значок-эмодзи — два знака)."""
    return len(t.encode("utf-16-le")) // 2


def auto_widths(header, rows, total=TABLE_WIDTH, lo=10, hi=70):
    """Ширины колонок по правилу исходного генератора документа.

    Вес колонки — длина её самой длинной ячейки, зажатая в [lo, hi]; ширины — доли весов.
    Так короткие ключи получают узкую первую колонку, а две «текстовые» колонки делят
    таблицу пополам. Проверено на таблицах документа: 183 → [2305, 7333], 958 → [4565, 5073].
    """
    ncol = len(header)
    cols = [[header[c]] + [r[c] for r in rows] for c in range(ncol)]
    weights = [min(max(max(_len16(_plain(x)) for x in col), lo), hi) for col in cols]
    widths = [round(total * w / sum(weights)) for w in weights]      # округление — как у исходного генератора
    widths[-1] += total - sum(widths)
    return widths
