#!/usr/bin/env python3
"""Индекс элементов верхнего уровня <w:body>: список (тег, начало, конец) — смещения в строке document.xml."""
import re

TOP = re.compile(r"<(/?)w:(p|tbl|sdt|sectPr|bookmarkStart|bookmarkEnd)([ >/])")


def text(x):
    return re.sub(r"<[^>]+>", "", x)


def index(s):
    """Обход прямых потомков <w:body> с учётом вложенности (таблицы в таблицах, абзацы в sdt)."""
    a = s.index("<w:body>") + len("<w:body>")
    end = s.rindex("</w:body>")
    els = []
    pos = a
    while pos < end:
        m = re.compile(r"<w:([A-Za-z]+)([ >/])").match(s, pos)
        if not m:
            raise ValueError(f"неожиданное содержимое на {pos}: {s[pos:pos+80]!r}")
        tag = m.group(1)
        # самозакрывающийся элемент?
        close = s.index(">", pos)
        if s[close - 1] == "/":
            els.append((tag, pos, close + 1))
            pos = close + 1
            continue
        # ищем парный закрывающий тег с учётом вложенных одноимённых
        depth = 0
        scan = re.compile(rf"<(/?)w:{tag}(?=[ >/])([^>]*)>")
        for mm in scan.finditer(s, pos):
            if mm.group(1):
                depth -= 1
            elif mm.group(2).endswith("/"):
                continue
            else:
                depth += 1
            if depth == 0:
                els.append((tag, pos, mm.end()))
                pos = mm.end()
                break
        else:
            raise ValueError(f"нет закрывающего тега для {tag} на {pos}")
    assert pos == end, (pos, end)
    return els


if __name__ == "__main__":
    import sys
    from collections import Counter
    s = open(sys.argv[1], encoding="utf-8").read()
    els = index(s)
    print(len(els), Counter(t for t, _, _ in els))
    # самопроверка: элементы идут встык
    for (t1, a1, b1), (t2, a2, b2) in zip(els, els[1:]):
        assert b1 == a2
