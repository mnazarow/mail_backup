#!/usr/bin/env python3
"""Элементы тела документа → «единицы» с видом и ключом текста — для сопоставления с отрисовкой Markdown."""
import re
import bodyindex
from mdrender import text


def classify(xml, prev_kind=None):
    """XML элемента верхнего уровня → (вид, ключ). Ключ — текст без пробелов; у врезок — ещё и без тире."""
    if xml.startswith("<w:tbl>"):
        if 'w:fill="0D1017"' in xml:
            kind = "CODE"
        elif 'w:fill="ECF2FE"' in xml:
            kind = "CALLOUT"
        elif 'w:fill="2F6FED"' in xml:
            kind = "TABLE"
        else:
            kind = "TBL"
    elif xml.startswith("<w:p"):
        m = re.search(r'<w:pStyle w:val="(\w+)"/>', xml[:120])
        style = m.group(1) if m else ""
        if "<!--IMG:" in xml:
            return "IMG", "IMG"
        if "<w:drawing" in xml or "<w:pict" in xml:
            return "IMG", "IMG"
        if style.startswith("Heading"):
            kind = "H" + style[-1]
        elif "<w:numPr>" in xml:
            num = int(re.search(r'<w:numId w:val="(\d+)"', xml).group(1))
            lvl = re.search(r'<w:ilvl w:val="(\d+)"', xml).group(1)
            kind = ("OL" if num >= 3 else "UL") + lvl
        elif not text(xml).strip():
            if 'w:type="page"' in xml:
                return "PB", "PB"
            return "SP", "SP:" + (prev_kind or "")
        elif '<w:jc w:val="center"/>' in xml[:120] and "<w:i/>" in xml:
            kind = "CAP"
        else:
            kind = "P"
    else:
        kind = xml[3:xml.index(">")].split()[0]
    t = re.sub(r"\s+", "", text(xml))
    if kind == "CALLOUT":
        t = re.sub(r"[-–]", "", t)
    if kind == "CODE":                       # в коде пробелы значимы: строки склеиваем через \n
        t = "\n".join(text(p) for p in re.findall(r"<w:p>.*?</w:p>", xml, flags=re.S))
    return kind, kind + ":" + t


def units(xmls):
    """Список XML → список (вид, ключ, xml); отступ получает в ключ ключ предыдущего элемента."""
    out = []
    prev = None
    for x in xmls:
        kind, key = classify(x, prev)
        out.append((kind, key, x))
        if kind != "SP":
            prev = key                      # отступ привязан к элементу перед ним: ключи отступов не повторяются
    return out


class Doc:
    def __init__(self, path):
        self.path = path
        self.s = open(path, encoding="utf-8").read()
        self.els = bodyindex.index(self.s)

    def xml(self, i):
        return self.s[self.els[i][1]:self.els[i][2]]

    def text(self, i):
        return text(self.xml(i))

    def heading_level(self, i):
        x = self.xml(i)
        m = re.match(r'<w:p><w:pPr><w:pStyle w:val="Heading(\d)"/>', x)
        return int(m.group(1)) if m else 0

    def find(self, pred, after=0, before=None):
        for i in range(after, before if before is not None else len(self.els)):
            if pred(i):
                return i
        raise LookupError("элемент не найден")

    def find_heading(self, level, title, after=0, before=None, prefix=False):
        def pred(i):
            if self.heading_level(i) != level:
                return False
            t = self.text(i).strip()
            return t.startswith(title) if prefix else t == title
        return self.find(pred, after, before)

    def chapter_range(self, title_prefix):
        """(индекс заголовка H1, индекс следующего H1)."""
        a = self.find_heading(1, title_prefix, prefix=True)
        b = self.find(lambda i: self.heading_level(i) == 1, a + 1)
        return a, b

    def h2_sections(self, a, b):
        """Разделы главы [a, b): [(заголовок или None, первый индекс, индекс за последним)]."""
        out = []
        start, title = a + 1, None
        for i in range(a + 1, b):
            if self.heading_level(i) == 2:
                out.append((title, start, i))
                start, title = i, self.text(i).strip()
        out.append((title, start, b))
        return out
