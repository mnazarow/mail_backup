#!/usr/bin/env python3
"""Приложение «Справочник параметров» ← docs/ru/05-parameters-reference.md.

В руководстве каждый раздел «## 🌐 Сервер и сеть» — заголовок H2 без значка, одна таблица без строки
заголовков (на параметр — строка: слева название и ключ, справа описание, 💡 рекомендация, пример и
значение по умолчанию) и пустой абзац-отступ. Шаблон сверен с документом побайтно (python3 appendix.py OLD_MD DOCXML).
"""
import re
import sys
from xml.sax.saxutils import escape

CODE = '<w:rFonts w:ascii="Consolas" w:cs="Consolas" w:eastAsia="Consolas" w:hAnsi="Consolas"/><w:color w:val="24408F"/>'
TCMAR = ('<w:tcMar><w:top w:type="dxa" w:w="60"/><w:left w:type="dxa" w:w="100"/><w:bottom w:type="dxa" w:w="60"/>'
         '<w:right w:type="dxa" w:w="100"/></w:tcMar><w:vAlign w:val="center"/>')
WIDTHS = (2750, 6888)
SPACER = '<w:p><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>'


def _r(t, rpr):
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(t)}</w:t></w:r>'


def _code(t):
    return _r(t, CODE + '<w:sz w:val="16"/><w:szCs w:val="16"/>')


def _unq(v):
    v = v.strip()
    return v[1:-1] if len(v) > 1 and v.startswith("`") and v.endswith("`") else v


def parse(md):
    """→ [(заголовок раздела без значка, [параметр])]; параметр — dict name, key, desc, rec, example, default."""
    out = []
    for chunk in re.split(r"\n(?=## )", md):
        if not chunk.startswith("## "):
            continue
        head, _, body = chunk.partition("\n")
        title = re.sub(r"^## \S+\s+", "", head).strip()          # «## 🌐 Сервер и сеть» → «Сервер и сеть»
        params = []
        for p in re.split(r"\n(?=### )", body):
            if not p.startswith("### "):
                continue
            lines = p.split("\n")
            name = lines[0][4:].strip()
            key = None
            m = re.fullmatch(r"(.+?) \(`([^`]+)`\)", name)
            if m:                                                # «### Название (`name`)»
                name, key = m.group(1), m.group(2)
            rest = [l for l in lines[1:]]
            if key is None and rest and re.fullmatch(r"`[^`]+`", rest[0].strip()):
                key = rest[0].strip()[1:-1]
                rest = rest[1:]
            desc, fields = [], {}
            for l in rest:
                mm = re.match(r"- \*\*(.+?):\*\*\s*(.*)$", l)
                if mm:
                    fields[mm.group(1)] = mm.group(2).strip()
                elif l.strip():
                    assert not fields, (name, l)
                    desc.append(l.strip())
            assert set(fields) <= {"Рекомендация", "Пример", "По умолчанию"}, (name, fields)
            params.append({"name": name, "key": key, "desc": " ".join(desc), "rec": fields.get("Рекомендация"),
                           "example": fields.get("Пример"), "default": fields.get("По умолчанию")})
        out.append((title, params))
    return out


def row(p):
    left = (f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{WIDTHS[0]}"/><w:shd w:fill="F5F7FB" w:color="auto" w:val="clear"/>'
            f'{TCMAR}</w:tcPr><w:p><w:pPr><w:spacing w:after="20"/></w:pPr>'
            + _r(p["name"], '<w:b/><w:bCs/><w:sz w:val="19"/><w:szCs w:val="19"/>') + '</w:p>'
            + (f'<w:p>{_code(p["key"])}</w:p>' if p["key"] else "") + '</w:tc>')
    right = f'<w:tc><w:tcPr><w:tcW w:type="dxa" w:w="{WIDTHS[1]}"/>{TCMAR}</w:tcPr>'
    right += ('<w:p><w:pPr><w:spacing w:after="40" w:line="252"/></w:pPr>'
              + _r(p["desc"], '<w:sz w:val="19"/><w:szCs w:val="19"/>') + '</w:p>')
    if p["rec"]:
        right += ('<w:p><w:pPr><w:spacing w:after="40" w:line="252"/></w:pPr>'
                  + _r("💡 " + p["rec"], '<w:color w:val="1F9D55"/><w:sz w:val="18"/><w:szCs w:val="18"/>') + '</w:p>')
    tail = []
    label = '<w:color w:val="5A6675"/><w:sz w:val="18"/><w:szCs w:val="18"/>'
    if p["example"] is not None:
        tail.append(_r("Пример: ", label) + _code(_unq(p["example"])))
    if p["default"] is not None:
        tail.append(_r("По умолчанию: ", label) + _code(_unq(p["default"])))
    if tail:
        right += '<w:p>' + _r("    ", '<w:sz w:val="18"/><w:szCs w:val="18"/>').join(tail) + '</w:p>'
    right += '</w:tc>'
    return '<w:tr><w:trPr><w:cantSplit/></w:trPr>' + left + right + '</w:tr>'


def table(params):
    border = "".join(f'<w:{side} w:val="single" w:color="D9DEE8" w:sz="4"/>'
                     for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    return (f'<w:tbl><w:tblPr><w:tblW w:type="dxa" w:w="9638"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>'
            f'<w:tblGrid><w:gridCol w:w="{WIDTHS[0]}"/><w:gridCol w:w="{WIDTHS[1]}"/></w:tblGrid>'
            + "".join(row(p) for p in params) + '</w:tbl>')


def heading(title):
    return ('<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:rPr><w:b/><w:bCs/><w:color w:val="2F6FED"/>'
            f'<w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr><w:t xml:space="preserve">{escape(title)}</w:t></w:r></w:p>')


def section(title, params):
    return heading(title) + table(params) + SPACER


if __name__ == "__main__":
    # самопроверка: прежний Markdown → те же таблицы, что в документе
    import bodyindex
    md = open(sys.argv[1], encoding="utf-8").read()
    s = open(sys.argv[2], encoding="utf-8").read()
    els = bodyindex.index(s)
    xs = [s[a:b] for _, a, b in els]
    start = next(i for i, x in enumerate(xs) if 'Heading1' in x[:60] and "Справочник параметров" in x)
    same = rows_same = rows_all = 0
    secs = parse(md)
    for title, params in secs:
        i = next(k for k in range(start, len(xs)) if xs[k] == heading(title))
        doc = xs[i] + xs[i + 1] + xs[i + 2]
        mine = section(title, params)
        dr = re.findall(r"<w:tr>.*?</w:tr>", xs[i + 1], flags=re.S)
        nr = re.findall(r"<w:tr>.*?</w:tr>", table(params), flags=re.S)
        rows_all += len(dr)
        rows_same += sum(1 for a, b in zip(dr, nr) if a == b)
        if doc == mine:
            same += 1
        else:
            for a, b in zip(dr, nr):
                if a != b:
                    k = next(n for n in range(min(len(a), len(b))) if a[n] != b[n])
                    print(f"  {title}: doc …{a[max(0, k-120):k+120]!r}\n          new …{b[max(0, k-120):k+120]!r}")
                    break
    print(f"разделов {len(secs)}, побайтно равных {same}; строк {rows_all}, побайтно равных {rows_same}")
