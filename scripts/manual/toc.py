#!/usr/bin/env python3
"""Номера страниц заголовков 1–2 уровня из PDF → файл для toc_apply.py.

    python3 toc.py out.pdf unpacked/word/document.xml toc_new.json

Заголовки берутся из document.xml (по порядку), страницы — из закладок PDF: LibreOffice
пишет в них каждый заголовок с точной страницей. Для сверки те же заголовки ищутся в
тексте страниц (как делал прежний toc.py); расхождения печатаются.
"""
import json
import re
import subprocess
import sys

import pypdf

from mdrender import text

pdf, docxml, out_path = sys.argv[1:4]
s = open(docxml, encoding="utf-8").read()
body = s[s.index("</w:sdt>"):]
heads = [(int(m.group(1)), text(m.group(0)))
         for m in re.finditer(r'<w:p><w:pPr><w:pStyle w:val="Heading([12])"/>.*?</w:p>', body, flags=re.S)]

reader = pypdf.PdfReader(pdf)
npages = len(reader.pages)


def walk(items, depth, acc):
    for x in items:
        if isinstance(x, list):
            walk(x, depth + 1, acc)
        else:
            acc.append((depth + 1, x.title, reader.get_destination_page_number(x) + 1))
    return acc


marks = [m for m in walk(reader.outline, 0, []) if m[0] <= 2]


def norm(t):
    return re.sub(r"\s+", " ", t).strip()


if [(lv, norm(t)) for lv, t in heads] != [(lv, norm(t)) for lv, t, _ in marks]:
    for h, m in zip(heads, marks):
        if (h[0], norm(h[1])) != (m[0], norm(m[1])):
            sys.exit(f"закладки PDF расходятся с заголовками документа: {h} / {m}")
    sys.exit(f"заголовков в документе {len(heads)}, закладок в PDF {len(marks)}")
toc = [(lv, t, page) for (lv, t), (_, _, page) in zip(heads, marks)]

# сверка поиском по тексту страниц
pages = []
for n in range(1, npages + 1):
    t = subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), "-layout", pdf, "-"],
                       capture_output=True, text=True).stdout
    pages.append([norm(line) for line in t.split("\n")])
bad = 0
for lv, title, page in toc:
    want = norm(title)
    lines = pages[page - 1]
    if not any(line == want or (line and len(line) > 25 and want.startswith(line)) for line in lines):
        bad += 1
        print(f"  ! «{title}»: на стр. {page} такой строки нет", file=sys.stderr)
mono = all(a[2] <= b[2] for a, b in zip(toc, toc[1:]))
print(f"заголовков {len(toc)} (глав {sum(1 for t in toc if t[0] == 1)}), страниц {npages}, "
      f"не подтверждено текстом страницы: {bad}, страницы по возрастанию: {mono}", file=sys.stderr)
if bad or not mono:
    sys.exit(1)
json.dump(toc, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
