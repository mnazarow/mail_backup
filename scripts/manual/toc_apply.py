#!/usr/bin/env python3
"""Переписать статичное оглавление (список внутри <w:sdtContent>) по файлу от toc.py.

    python3 toc_apply.py toc_new.json unpacked/word/document.xml
"""
import json
import re
import sys
from xml.sax.saxutils import escape

toc = json.load(open(sys.argv[1], encoding="utf-8"))
doc = sys.argv[2]
s = open(doc, encoding="utf-8").read()
assert s.count("<w:sdtContent>") == 1
a = s.index("<w:sdtContent>") + len("<w:sdtContent>")
b = s.index("</w:sdtContent>")
old = s[a:b]
first = old[:old.index("</w:p>") + 6]                      # абзац с началом поля TOC
tail = old[old.rindex("<w:p>"):]                           # абзац с концом поля
assert 'fldCharType="begin"' in first and 'fldCharType="end"' in tail, (first[:200], tail[:200])
TABS = '<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="9630"/></w:tabs>'
rows = []
for level, title, page in toc:
    if level == 1:
        ppr = TABS + '<w:spacing w:before="100" w:after="30"/>'
        rpr = '<w:b/><w:bCs/><w:sz w:val="21"/><w:szCs w:val="21"/>'
    else:
        ppr = TABS + '<w:spacing w:after="30"/><w:ind w:left="360"/>'
        rpr = '<w:sz w:val="19"/><w:szCs w:val="19"/>'
    rows.append(f'<w:p><w:pPr>{ppr}</w:pPr><w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(title)}</w:t>'
                f'<w:tab/><w:t>{page}</w:t></w:r></w:p>')
# самопроверка: прежние строки оглавления собраны тем же шаблоном
old_rows = re.findall(r"<w:p><w:pPr><w:tabs>.*?</w:p>", old, flags=re.S)
shape = lambda r: re.sub(r">[^<]+<", "><", r)
assert first + "".join(old_rows) + tail == old, "в оглавлении есть что-то кроме строк"
assert {shape(r) for r in old_rows} == {shape(r) for r in rows}, "шаблон строк оглавления не совпал"
changed = sum(1 for x, y in zip(old_rows, rows) if x != y) + abs(len(old_rows) - len(rows))
print(f"оглавление: строк было {len(old_rows)}, стало {len(rows)}, изменено {changed}")
open(doc, "w", encoding="utf-8").write(s[:a] + first + "".join(rows) + tail + s[b:])
