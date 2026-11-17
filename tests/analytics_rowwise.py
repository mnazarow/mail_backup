# -*- coding: utf-8 -*-
"""Эталон для тестов: построчный расчёт «Аналитики писем», как он был до 1.27.0 (каждое письмо индекса —
в Python). С 1.27.0 :func:`mailarchiver.analytics.mail_analytics` считает в базе группами; итог должен совпадать
с этим расчётом полностью (tests/test_analytics.py)."""
import re
from array import array
from collections import Counter
from datetime import datetime, timezone

from mailarchiver import analytics as A


def rowwise_mail_analytics(svc, account_id=None):
    db = svc.db
    # Индекс обходим ПОТОКОМ: прежняя выборка списком занимала ~140 МБ на
    # 200 000 писем, и это до всех накопителей внутри цикла.
    rows = db.iter_index_rows_for_analytics(account_id)
    total = 0
    try:
        local_tz = svc.local_tz()
    except Exception:  # noqa: BLE001
        local_tz = timezone.utc

    senders: Counter = Counter()
    sender_names: dict = {}
    domains: Counter = Counter()
    by_folder_cnt: Counter = Counter()
    by_folder_bytes: Counter = Counter()
    by_year: Counter = Counter()
    by_month: Counter = Counter()
    by_weekday = [0] * 7
    by_hour = [0] * 24
    heat = [[0] * 24 for _ in range(7)]      # день недели × час
    size_buckets = [0] * len(A._SIZE_EDGES)
    subject_words: Counter = Counter()
    total_bytes = 0
    seen = flagged = answered = draft = with_attach = 0
    reply_cnt = forward_cnt = empty_subject = 0
    # Массив вместо списка: нужен только для медианы, а array("q") хранит
    # 8 байт на число вместо ~36 у списка Python-объектов.
    sizes = array("q")
    # Для дат достаточно минимума и максимума — список из сотен тысяч
    # datetime держать незачем.
    dmin_dt: object = None
    dmax_dt: object = None

    for r in rows:
        total += 1
        size = r["size"] or 0
        total_bytes += size
        sizes.append(size)
        # размер → корзина
        for i, (lo, hi, _label) in enumerate(A._SIZE_EDGES):
            if lo <= size < hi:
                size_buckets[i] += 1
                break
        # флаги
        fl = (r["flags"] or "").split(",")
        if "\\Seen" in fl:
            seen += 1
        if "\\Flagged" in fl:
            flagged += 1
        if "\\Answered" in fl:
            answered += 1
        if "\\Draft" in fl:
            draft += 1
        if r["has_attach"]:
            with_attach += 1
        # папка
        fld = r["folder"] or "—"
        by_folder_cnt[fld] += 1
        by_folder_bytes[fld] += size
        # отправитель / домен — один разбор адреса на письмо (с кэшем)
        frm = r["from_addr"] or ""
        em, name, dom = A._addr_parts(frm)
        if em:
            senders[em] += 1
            if name and em not in sender_names:
                sender_names[em] = name
        if dom:
            domains[dom] += 1
        # дата → год/месяц/день недели/час/тепловая карта
        dt = A._parse_dt(r["internaldate"])
        if dt:
            # Часы, дни недели и месяцы — по часам ПОЛЬЗОВАТЕЛЯ: в UTC рабочий
            # день 9–17 по Москве выглядел как 06–14.
            dt = dt.astimezone(local_tz)
            if dmin_dt is None or dt < dmin_dt:
                dmin_dt = dt
            if dmax_dt is None or dt > dmax_dt:
                dmax_dt = dt
            by_year[dt.strftime("%Y")] += 1
            by_month[dt.strftime("%Y-%m")] += 1
            wd = dt.weekday()
            by_weekday[wd] += 1
            by_hour[dt.hour] += 1
            heat[wd][dt.hour] += 1
        # тема → префиксы (Re/Fwd) и слова
        subj = (r["subject"] or "").strip()
        if not subj or subj == "(без темы)":
            empty_subject += 1
        else:
            if A._SUBJ_PREFIX_RE.match(subj):
                if re.match(r"^\s*(fwd?|переслать|пересл)", subj, re.IGNORECASE):
                    forward_cnt += 1
                else:
                    reply_cnt += 1
            for w in A._WORD_RE.findall(subj.lower()):
                if w in A.STOPWORDS or w.isdigit():
                    continue
                subject_words[w] += 1

    # Полный помесячный ряд (без «дыр») между первым и последним месяцем.
    month_series: list = []
    if by_month:
        keys = sorted(by_month.keys())
        y0, m0 = int(keys[0][:4]), int(keys[0][5:7])
        y1, m1 = int(keys[-1][:4]), int(keys[-1][5:7])
        # Не больше 400 ПОСЛЕДНИХ месяцев: одно письмо с датой 1970 года раньше
        # обрезало график на 2003 году, и свежих месяцев не было видно вовсе.
        if (y1 * 12 + m1) - (y0 * 12 + m0) >= 400:
            start = y1 * 12 + (m1 - 1) - 399
            y0, m0 = start // 12, start % 12 + 1
        y, m = y0, m0
        guard = 0
        while (y, m) <= (y1, m1) and guard < 400:
            key = f"{y:04d}-{m:02d}"
            month_series.append({"label": key, "value": int(by_month.get(key, 0))})
            m += 1
            if m > 12:
                m = 1
                y += 1
            guard += 1

    sorted_sizes = sorted(sizes)
    median = sorted_sizes[len(sorted_sizes) // 2] if sorted_sizes else 0
    del sorted_sizes
    dmin = dmin_dt.isoformat() if dmin_dt else None
    dmax = dmax_dt.isoformat() if dmax_dt else None
    span_days = ((dmax_dt - dmin_dt).days + 1) if (dmin_dt and dmax_dt) else 0
    avg_per_day = round(total / span_days, 1) if span_days else 0

    largest = [{"subject": r["subject"] or "(без темы)", "from": r["from_addr"] or "",
                "folder": r["folder"], "size": r["size"] or 0, "size_h": A.human_size(r["size"] or 0),
                "date": r["internaldate"]}
               for r in db.largest_messages(account_id, limit=10)]

    result = {
        "scope": {"account_id": account_id},
        "overview": {
            "messages": total, "bytes": total_bytes, "bytes_h": A.human_size(total_bytes),
            "avg_size": (total_bytes // total) if total else 0,
            "avg_size_h": A.human_size(total_bytes // total) if total else "—",
            "median_size": median, "median_size_h": A.human_size(median),
            "unique_senders": len(senders), "unique_domains": len(domains),
            "with_attach": with_attach,
            "with_attach_pct": round(100.0 * with_attach / total, 1) if total else 0,
            "seen": seen, "unseen": total - seen,
            "unseen_pct": round(100.0 * (total - seen) / total, 1) if total else 0,
            "flagged": flagged, "answered": answered, "draft": draft,
            "reply": reply_cnt, "forward": forward_cnt, "empty_subject": empty_subject,
            "date_from": dmin, "date_to": dmax, "span_days": span_days, "avg_per_day": avg_per_day,
            "folders": len(by_folder_cnt),
        },
        "top_senders": [{"label": (sender_names.get(k) and f"{sender_names[k]} <{k}>") or k,
                         "email": k, "value": int(v)} for k, v in senders.most_common(20)],
        "top_domains": A._top(domains, 15),
        "folders": [{"label": k, "value": int(v), "bytes": int(by_folder_bytes[k]),
                     "bytes_h": A.human_size(by_folder_bytes[k])}
                    for k, v in by_folder_cnt.most_common(20)],
        "by_year": [{"label": k, "value": int(v)} for k, v in sorted(by_year.items())],
        "by_month": month_series,
        "by_weekday": [{"label": A.WEEKDAYS_RU[i], "value": by_weekday[i]} for i in range(7)],
        "by_hour": [{"label": f"{i:02d}", "value": by_hour[i]} for i in range(24)],
        "heatmap": {"rows": A.WEEKDAYS_RU, "matrix": heat,
                    "max": max((max(r) for r in heat), default=0)},
        "size_hist": [{"label": A._SIZE_EDGES[i][2], "value": size_buckets[i]}
                      for i in range(len(A._SIZE_EDGES))],
        "subject_words": A._top(subject_words, 40),
        "largest": largest,
        "read_state": [{"label": "Прочитанные", "value": seen},
                       {"label": "Непрочитанные", "value": total - seen}],
        "attach_state": [{"label": "С вложениями", "value": with_attach},
                         {"label": "Без вложений", "value": total - with_attach}],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return result
