#!/usr/bin/env python3
"""Проверка вёрстки по PDF.

    python3 layout_check.py out.pdf unpacked/word/document.xml [--pages N-M]

* заголовок (H1–H3) или абзац-подводка «…:» последней строкой страницы — оторван от текста;
* слова правее поля страницы (таблица или код вылезли за край);
* подпись рисунка не на той странице, что сам рисунок (по тексту подписи: в начале страницы без рисунка).
Ненулевой код — если что-то нашлось.
"""
import html
import re
import subprocess
import sys

pdf, docxml = sys.argv[1:3]
s = open(docxml, encoding="utf-8").read()
body = s[s.index("</w:sdt>"):]


def text(x):
    return html.unescape(re.sub(r"<[^>]+>", "", x))


heads = [re.sub(r"\s+", " ", text(m.group(0))).strip()
         for m in re.finditer(r'<w:p><w:pPr><w:pStyle w:val="Heading[123]"/>.*?</w:p>', body, flags=re.S)]
caps = [re.sub(r"\s+", " ", text(m.group(0))).strip()
        for m in re.finditer(r'<w:p><w:pPr><w:spacing w:after="160"/><w:jc w:val="center"/></w:pPr>.*?</w:p>', body,
                             flags=re.S)]

xml = subprocess.run(["pdftotext", "-bbox-layout", pdf, "-"], capture_output=True, text=True).stdout
pages = re.findall(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', xml, flags=re.S)
problems = 0
right_edge = None
for n, (w, h, content) in enumerate(pages, 1):
    w, h = float(w), float(h)
    right_edge = w - 1134 / 20 + 3            # поле 1134 twip = 56.7 pt, допуск 3 pt
    lines = []
    for lm in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>',
                          content, flags=re.S):
        words = re.findall(r'<word xMin="[\d.]+" yMin="[\d.]+" xMax="([\d.]+)" yMax="[\d.]+">([^<]*)</word>', lm.group(5))
        t = " ".join(html.unescape(x) for _, x in words)
        lines.append((float(lm.group(2)), float(lm.group(1)), float(lm.group(3)), t))
        for xmax, word in words:
            if float(xmax) > right_edge:
                problems += 1
                print(f"стр. {n}: слово за правым полем ({float(xmax):.0f} > {right_edge:.0f} pt): {html.unescape(word)!r}")
    lines.sort()
    body_lines = [l for l in lines if not re.fullmatch(r"MailArchiver [\d.]+ — руководство", l[3])
                  and not re.fullmatch(r"Стр\. \d+ из \d+", l[3])]
    if not body_lines or n <= 3:
        continue
    # последняя строка страницы (и две последние вместе — заголовок в две строки)
    last = body_lines[-1][3].strip()
    last2 = (body_lines[-2][3] + " " + last).strip() if len(body_lines) > 1 else last
    for hd in heads:
        if hd in (last, last2) or (len(last) > 12 and hd.endswith(last) and hd.startswith(body_lines[-2][3].strip())):
            problems += 1
            print(f"стр. {n}: заголовок внизу страницы: «{hd}»")
    if last.endswith(":") and len(body_lines) > 3 and n < len(pages):
        print(f"стр. {n}: (к сведению) последняя строка страницы кончается двоеточием: «{last[-80:]}»")
    first = body_lines[0][3].strip()
    top = body_lines[0][0]
    for cp in caps:
        # подпись первой строкой текста и у самого верха — значит, над ней на этой странице рисунка нет
        if (cp == first or (len(first) > 15 and cp.startswith(first))) and top < 1134 / 20 + 60:
            problems += 1
            print(f"стр. {n}: подпись рисунка в начале страницы (рисунок на предыдущей?): «{cp}»")
print(f"страниц {len(pages)}, заголовков {len(heads)}, подписей {len(caps)}; найдено: {problems}")
sys.exit(1 if problems else 0)
