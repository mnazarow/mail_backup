#!/usr/bin/env python3
"""Перенумерация глав, когда в руководство вставлена новая глава (номера следующих сдвигаются).

renumber(s, mapping) → (новый document.xml, список изменений); mapping — {прежний номер главы: новый}
(только меняющиеся). Меняется:
  * заголовки H1 этих глав и пронумерованные разделы H2 (16.N. → 17.N.), если такие есть;
  * ссылки на эти главы в тексте: «глава 17», «главе 18», «гл. 17», «раздел 16.3», «п. 16.3», «см. 16.3»;
  * ссылки вида «раздел X.Y (гл. N)», где X не совпадает с N (ошибка прежней сборки: «раздел 15.5 (гл. 6)»
    вместо «раздел 6.5 (гл. 6)»), — номер раздела берётся из номера главы.
Оглавление (sdt) не трогается: оно собирается заново. Ссылка, разорванная между прогонами текста, — ошибка.
"""
import html
import re

import bodyindex

CHAP = re.compile(r"(?<![\w.])(глава|главе|главу|главы|главой|гл\.)(\s+)(\d+)(?![\d.]*\d)")
SECT = re.compile(r"(?<![\w.])(раздел|разделе|разделы|разд\.|п\.|пункт|см\.)(\s+)(\d+)\.(\d+)(?![\d])")
WRONG = re.compile(r"раздел (\d+)\.(\d+) \(гл\. (\d+)\)")


def text(x):
    return html.unescape(re.sub(r"<[^>]+>", "", x))


MAPPING = {}


def _bump(n):
    return str(MAPPING.get(int(n), int(n)))


def fix_text(t):
    """Текст → (новый текст, [(было, стало)])."""
    changes = []

    def wrong(m):
        if m.group(1) != m.group(3):
            new = f"раздел {m.group(3)}.{m.group(2)} (гл. {m.group(3)})"
            changes.append((m.group(0), new))
            return new
        return m.group(0)
    t = WRONG.sub(wrong, t)

    def chap(m):
        new = m.group(1) + m.group(2) + _bump(m.group(3))
        if new != m.group(0):
            changes.append((m.group(0), new))
        return new

    def sect(m):
        new = m.group(1) + m.group(2) + _bump(m.group(3)) + "." + m.group(4)
        if new != m.group(0):
            changes.append((m.group(0), new))
        return new
    t = CHAP.sub(chap, t)
    t = SECT.sub(sect, t)
    return t, changes


def renumber(s, mapping):
    global MAPPING
    MAPPING = {int(k): int(v) for k, v in mapping.items() if int(k) != int(v)}
    if not MAPPING:
        return s, []
    els = bodyindex.index(s)
    sdt_end = s.index("</w:sdt>")
    edits, log = [], []
    for i, (tag, a, b) in enumerate(els):
        if b <= sdt_end:
            continue                       # обложка и оглавление
        x = s[a:b]
        m = re.match(r'<w:p><w:pPr><w:pStyle w:val="Heading([12])"/>', x)
        whole_before = text(x)
        new_x, n_changes = x, []
        if m and m.group(1) == "1":
            hm = re.search(r'(<w:t xml:space="preserve">)(\d+)(\. )', x)
            if hm and int(hm.group(2)) in MAPPING:
                new_x = x[:hm.start(2)] + _bump(hm.group(2)) + x[hm.end(2):]
                n_changes.append((f"{hm.group(2)}. …", f"{_bump(hm.group(2))}. …"))
        elif m and m.group(1) == "2":
            hm = re.search(r'(<w:t xml:space="preserve">)(\d+)\.(\d+)\. ', x)
            if hm and int(hm.group(2)) in MAPPING:
                new_x = x[:hm.start(2)] + _bump(hm.group(2)) + x[hm.end(2):]
                n_changes.append((f"{hm.group(2)}.{hm.group(3)}.", f"{_bump(hm.group(2))}.{hm.group(3)}."))
        else:
            # ссылки в тексте: правим каждый <w:t> по отдельности
            def per_t(mm):
                t2, ch = fix_text(html.unescape(mm.group(2)))
                n_changes.extend(ch)
                from xml.sax.saxutils import escape
                return mm.group(1) + (escape(t2) if ch else mm.group(2)) + mm.group(3)
            new_x = re.sub(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)", per_t, x)
            # ссылка, разорванная между прогонами, отдельным <w:t> не нашлась бы — сверяем с цельным текстом
            _, whole = fix_text(whole_before)
            assert len(whole) == len(n_changes), (i, whole, n_changes)
        if new_x != x:
            edits.append((a, b, new_x))
            for old, new in n_changes:
                t = whole_before
                log.append((i, old, new, t))
    for a, b, x in reversed(edits):
        s = s[:a] + x + s[b:]
    return s, log
