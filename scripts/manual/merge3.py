#!/usr/bin/env python3
"""Трёхстороннее слияние главы: прежний Markdown (O), документ (D), новый Markdown (N).

Все три — последовательности «единиц» (абзац, пункт списка, таблица, блок кода, врезка,
пустой абзац-отступ) с ключом «вид + текст без пробелов». Изменения O→N переносятся в D;
всё, что в Markdown не менялось, остаётся в документе как есть — побайтно, включая
рисунки и те места, где документ и раньше расходился с Markdown (подписи ссылок).

Итог diff3 по участкам:
  stable            — O, D и N совпадают: остаётся D;
  doc-ahead         — D уже равен N: остаётся D;
  md-unchanged      — N равен O, D отличается (рисунок, своя подпись ссылки): остаётся D;
  apply             — D равен O, N отличается: ставится N;
  conflict          — отличаются все три: D сверяется с N напрямую, совпавшее остаётся,
                      остальное ставится из N (рисунок в заменяемом участке — ошибка).
"""
import difflib
import re

NUM = re.compile(r'<w:numId w:val="(\d+)"/>')


def _maps(a, b):
    m = {}
    for i, j, size in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(size):
            m[i + k] = j + k
    return m


def diff3_chunks(O, D, N):
    """Ключи O, D, N → [(вид, (o1, o2), (d1, d2), (n1, n2))]."""
    o2d, o2n = _maps(O, D), _maps(O, N)
    chunks = []
    io = idd = inn = 0
    while io < len(O) or idd < len(D) or inn < len(N):
        i = 0
        while io + i < len(O) and o2d.get(io + i) == idd + i and o2n.get(io + i) == inn + i:
            i += 1
        if i:
            chunks.append(("stable", (io, io + i), (idd, idd + i), (inn, inn + i)))
            io, idd, inn = io + i, idd + i, inn + i
            continue
        j = io
        while j < len(O) and not (j in o2d and j in o2n and o2d[j] >= idd and o2n[j] >= inn):
            j += 1
        if j < len(O):
            chunks.append(("unstable", (io, j), (idd, o2d[j]), (inn, o2n[j])))
            io, idd, inn = j, o2d[j], o2n[j]
        else:
            chunks.append(("unstable", (io, len(O)), (idd, len(D)), (inn, len(N))))
            break
    return chunks


def merge(O, D, N, log=print, protect=("IMG", "CAP"), allow_new_images=False):
    """O, D, N — списки единиц (вид, ключ, xml). → [(источник 'D'|'N', индекс, xml)] и счётчики.

    allow_new_images — новый Markdown отрисован с рисунками (абзац с <w:drawing>): новый рисунок и его
    подпись можно ставить из N. Рисунки и подписи документа по-прежнему не удаляются.
    """
    ko, kd, kn = [u[1] for u in O], [u[1] for u in D], [u[1] for u in N]
    out = []
    stats = {"stable": 0, "doc-ahead": 0, "md-unchanged": 0, "apply": 0, "conflict": 0}

    def short(key):
        return key if len(key) <= 110 else key[:110] + "…"

    def keep(d1, d2):
        out.extend(("D", i, D[i][2]) for i in range(d1, d2))

    def drop(d1, d2, pad="  "):
        for i in range(d1, d2):
            if D[i][0] in protect:
                raise RuntimeError(f"рисунок или подпись в удаляемом участке: D[{i}] {kd[i]}")
            log(f"{pad}- D[{i}] {short(kd[i])}")

    def take(n1, n2, base=(), pad="  "):
        for j in range(n1, n2):
            if N[j][0] in protect and not (allow_new_images and (N[j][0] == "CAP" or "<w:drawing" in N[j][2])):
                raise RuntimeError(f"новый рисунок в Markdown — отрисовать нечем: N[{j}] {kn[j]}")
            mark = "  (в Markdown не менялось!)" if kn[j] in base and N[j][0] != "SP" else ""
            log(f"{pad}+ N[{j}] {short(kn[j])}{mark}")
            out.append(("N", j, N[j][2]))

    for kind, (o1, o2), (d1, d2), (n1, n2) in diff3_chunks(ko, kd, kn):
        if kind == "stable":
            stats["stable"] += d2 - d1
            keep(d1, d2)
            continue
        po, pd, pn = ko[o1:o2], kd[d1:d2], kn[n1:n2]
        if pd == pn:
            stats["doc-ahead"] += d2 - d1
            keep(d1, d2)
            if pd:
                log(f"  = документ уже равен новому Markdown: {len(pd)} ед. с D[{d1}] ({short(pd[0])})")
        elif pn == po:
            stats["md-unchanged"] += d2 - d1
            keep(d1, d2)
            for i in range(d1, d2):
                if D[i][0] not in ("IMG", "CAP", "SP"):
                    log(f"  · Markdown не менялся, в документе свой текст — оставлен: D[{i}] {short(kd[i])}")
        elif pd == po:
            stats["apply"] += max(d2 - d1, n2 - n1)
            drop(d1, d2)
            take(n1, n2)
        else:
            # расходятся все три: сверяем D с N напрямую
            stats["conflict"] += max(d2 - d1, n2 - n1)
            log(f"  ! документ расходился с прежним Markdown, и Markdown изменился: D[{d1}:{d2}] ↔ N[{n1}:{n2}]")
            sm = difflib.SequenceMatcher(None, pd, pn, autojunk=False)
            for op, a1, a2, b1, b2 in sm.get_opcodes():
                da, db, na, nb = d1 + a1, d1 + a2, n1 + b1, n1 + b2
                if op == "equal":
                    keep(da, db)
                elif all(D[i][0] in protect for i in range(da, db)) and \
                        all(N[j][0] in protect for j in range(na, nb)):
                    keep(da, db)                 # рисунки документа остаются; меток из Markdown не ставим
                else:
                    drop(da, db, "    ")
                    take(na, nb, po, "    ")
    return out, stats


def fix_ordered_lists(out, classify):
    """Новый пункт нумерованного списка рядом с прежними пунктами получает их numId."""
    kinds = [classify(x)[0] for _, _, x in out]
    res = list(out)
    for i, (src, idx, xml) in enumerate(res):
        if src != "N" or kinds[i] != "OL0":
            continue
        donor = None
        k = i - 1
        while k >= 0 and kinds[k] in ("OL0", "OL1", "UL1", "CODE", "SP") and donor is None:
            if kinds[k] == "OL0":
                donor = NUM.search(res[k][2]).group(1)
            elif kinds[k] == "SP" and kinds[k - 1][:2] not in ("CO",):
                break
            k -= 1
        if donor is None:
            k = i + 1
            while k < len(res) and kinds[k] in ("OL0", "OL1", "UL1"):
                if kinds[k] == "OL0" and res[k][0] == "D":
                    donor = NUM.search(res[k][2]).group(1)
                    break
                k += 1
        if donor is not None:
            res[i] = (src, idx, NUM.sub(f'<w:numId w:val="{donor}"/>', xml))
    return res
