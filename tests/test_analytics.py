# -*- coding: utf-8 -*-
"""Тесты раздела «Аналитика» и «Аналитика писем»."""
from email.message import EmailMessage
from email.utils import formatdate

from mailarchiver import models
from mailarchiver import analytics as A


def _seed_index(svc, aid, folder, uid, subject, from_addr, size, iso, flags="", attach=0):
    svc.db.add_message_index(aid, folder, 1000, uid, f"<{uid}@ex.com>", size, iso,
                             flags, f"{folder}/cur/{uid}", "hash%d" % uid,
                             subject=subject, from_addr=from_addr, has_attach=attach)


def test_mail_analytics_metadata(services):
    svc = services
    svc.set_rt("scheduler", "timezone", "UTC")     # часы ниже — по UTC
    aid = svc.db.create_account(models.Account(name="A", host="h", port=993, username="u", password="p"))
    # 4 письма: 2 понедельника (2024-01-01 = Пн), разные отправители/домены/размеры
    _seed_index(svc, aid, "INBOX", 1, "Отчёт за январь", "Иван <ivan@aa.ru>", 5000,
                "2024-01-01T09:30:00+00:00", "\\Seen", 0)
    _seed_index(svc, aid, "INBOX", 2, "Re: Отчёт за январь", "Пётр <petr@bb.com>", 250000,
                "2024-01-01T14:00:00+00:00", "\\Seen,\\Answered", 1)
    _seed_index(svc, aid, "Работа", 3, "Договор", "Иван <ivan@aa.ru>", 1500000,
                "2024-01-02T11:00:00+00:00", "", 0)
    _seed_index(svc, aid, "Работа", 4, "Fwd: Договор", "Анна <anna@aa.ru>", 800,
                "2024-01-03T20:00:00+00:00", "\\Flagged", 0)

    ma = A.mail_analytics(svc, account_id=aid)
    o = ma["overview"]
    assert o["messages"] == 4
    assert o["unique_senders"] == 3
    assert o["unique_domains"] == 2            # aa.ru, bb.com
    assert o["with_attach"] == 1
    assert o["seen"] == 2 and o["unseen"] == 2
    assert o["reply"] == 1 and o["forward"] == 1
    assert o["folders"] == 2
    # домены: aa.ru встречается 3 раза
    top = {d["label"]: d["value"] for d in ma["top_domains"]}
    assert top["aa.ru"] == 3 and top["bb.com"] == 1
    # размерная гистограмма покрывает все 4 письма
    assert sum(b["value"] for b in ma["size_hist"]) == 4
    # день недели: две записи на понедельник (01.01.2024)
    wd = {w["label"]: w["value"] for w in ma["by_weekday"]}
    assert wd["Пн"] == 2
    # час 09,11,14,20 присутствуют
    hours = {h["label"]: h["value"] for h in ma["by_hour"]}
    assert hours["09"] == 1 and hours["14"] == 1 and hours["20"] == 1
    # частые слова темы (стоп-слова/короткие отброшены)
    words = {w["label"] for w in ma["subject_words"]}
    assert "отчёт" in words and "договор" in words
    # тепловая карта 7×24
    assert len(ma["heatmap"]["matrix"]) == 7 and len(ma["heatmap"]["matrix"][0]) == 24
    # крупнейшее письмо — договор 1.5 МБ
    assert ma["largest"][0]["size"] == 1500000


def test_system_analytics(services):
    svc = services
    aid = svc.db.create_account(models.Account(name="Sys", host="h", port=993, username="u", password="p"))
    _seed_index(svc, aid, "INBOX", 1, "s", "a@a.ru", 1000, "2024-05-01T10:00:00+00:00", "\\Seen")
    _seed_index(svc, aid, "INBOX", 2, "s2", "b@a.ru", 2000, "2024-05-02T10:00:00+00:00", "")
    # задание + прогон
    jid = svc.db.enqueue_job(models.JobType.BACKUP, aid, {})
    svc.db.finish_job(jid, models.JobStatus.SUCCESS, {"ok": 1})
    rid = svc.db.start_run(aid, models.JobType.BACKUP, jid)
    svc.db.finish_run(rid, models.JobStatus.SUCCESS, messages_new=2, bytes_new=3000)

    sa = A.system_analytics(svc)
    o = sa["overview"]
    assert o["messages"] == 2 and o["bytes"] == 3000
    assert o["accounts_total"] == 1
    assert o["jobs_total"] == 1 and o["jobs_success_rate"] == 100.0
    assert sa["runs"]["total"] == 1 and sa["runs"]["messages_new"] == 2
    # разрез по одному ящику
    sa1 = A.system_analytics(svc, account_id=aid)
    assert sa1["overview"]["messages"] == 2


def test_deep_scan_and_cache(services):
    svc = services
    aid = svc.db.create_account(models.Account(name="Deep", host="h", port=993, username="u", password="p"))
    # реальное письмо с русским текстом и PDF-вложением
    msg = EmailMessage()
    msg["From"] = "Иван <ivan@aa.ru>"
    msg["To"] = "Пётр <petr@bb.com>, sales@cc.io"
    msg["Subject"] = "Договор"
    msg["Date"] = formatdate(1704106800, localtime=False)
    msg.set_content("Добрый день! Направляю договор во вложении на согласование.")
    msg.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="dogovor.pdf")
    raw = msg.as_bytes()
    rel, digest, size = svc.store.store_message(aid, "INBOX", "/", 1, raw, flags=["\\Seen"], internaldate=1704106800)
    svc.db.add_message_index(aid, "INBOX", 1000, 1, "<1@x>", size, "2024-01-01T11:00:00+00:00",
                             "\\Seen", rel, digest, subject="Договор", from_addr="Иван <ivan@aa.ru>", has_attach=1)

    deep = A.deep_scan(svc, account_id=aid)
    assert deep["scanned"] == 1 and deep["errors"] == 0
    assert deep["attachments"]["count"] == 1
    exts = {e["label"]: e["value"] for e in deep["attachments"]["by_ext"]}
    assert exts.get("pdf") == 1
    to_doms = {d["label"] for d in deep["recipients"]["top_domains"]}
    assert "bb.com" in to_doms and "cc.io" in to_doms
    langs = {l["label"] for l in deep["body"]["languages"]}
    assert "Русский" in langs
    # кэш: сохранить/прочитать
    A.save_deep(svc, aid, deep)
    assert A.load_deep(svc, aid)["scanned"] == 1


def test_analytics_endpoints_admin_only(client):
    # без входа — 401
    assert client.get("/api/analytics/system").status_code == 401
    # админ
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
    aid = client.post("/api/accounts", json={
        "name": "Box", "host": "imap.example.com", "port": 993,
        "username": "u@example.com", "password": "secret", "security": "ssl", "auth_type": "password",
    }).json()["id"]
    r = client.get("/api/analytics/system")
    assert r.status_code == 200 and "overview" in r.json()
    r = client.get(f"/api/analytics/mail?account_id={aid}")
    assert r.status_code == 200 and r.json()["overview"]["messages"] == 0
    # запуск глубокого анализа ставит задание в очередь
    r = client.post(f"/api/analytics/mail/scan?account_id={aid}")
    assert r.status_code == 200 and r.json()["ok"] is True
    d = client.get(f"/api/analytics/mail/deep?account_id={aid}")
    assert d.status_code == 200 and "available" in d.json()


def test_mail_analytics_uses_local_time(services):
    """Часы и дни недели — по часовому поясу пользователя, а не по UTC."""
    svc = services
    svc.set_rt("scheduler", "timezone", "Europe/Moscow")
    aid = svc.db.create_account(models.Account(name="TZ", host="h", port=993, username="u", password="p"))
    # 14.09.2026 22:30 UTC = 15.09.2026 01:30 МСК (вторник)
    _seed_index(svc, aid, "INBOX", 1, "Ночное", "Иван <ivan@aa.ru>", 100,
                "2026-09-14T22:30:00+00:00", "", 0)
    ma = A.mail_analytics(svc, account_id=aid, use_cache=False)
    hours = {h["label"]: h["value"] for h in ma["by_hour"]}
    assert hours["01"] == 1 and hours["22"] == 0
    wd = {w["label"]: w["value"] for w in ma["by_weekday"]}
    assert wd["Вт"] == 1 and wd["Пн"] == 0


def test_daily_series_has_no_gaps(services):
    """В ряду «Активность» дни без заданий — нули, а не пропуски."""
    svc = services
    aid = svc.db.create_account(models.Account(name="D", host="h", port=993, username="u", password="p"))
    svc.db.bump_daily_stats(aid, messages=5, jobs=1)
    series = svc.db.daily_series(days=7)
    assert len(series) == 7
    assert series[0]["messages"] == 5 and all(r["messages"] == 0 for r in series[1:])


def _tricky_index(svc):
    """Индекс с неудобными для расчёта группами случаями: пустые и NULL-поля, разные виды дат и заголовков From,
    флаги-«почти совпадения», равные частоты (порядок первого появления), даты до 1920 года."""
    import random
    rnd = random.Random(7)
    aid = svc.db.create_account(models.Account(name="T", host="h", port=993, username="u", password="p"))
    aid2 = svc.db.create_account(models.Account(name="T2", host="h", port=993, username="u2", password="p"))
    froms = ["Иван <ivan@aa.ru>", "ivan@aa.ru", "IVAN@AA.RU", "\"Пётр, отдел\" <petr@bb.com>", "", None,
             "кто-то без адреса", "Анна <anna@aa.ru>", "Анна Иванова <anna@aa.ru>", "x@y", "<z@q.org>"]
    subjects = ["Отчёт", "Re: Отчёт", "RE[2]: Отчёт", "Fwd: Договор", "Пересл: договор", "", None, "(без темы)",
                "  Счёт 2024 оплата  ", "оплата", "Отв: счёт", "fw: test", "abc def ghi"]
    flags = ["", None, "\\Seen", "\\Seen,\\Flagged", "\\SeenX", "\\Answered,\\Draft", "x,\\Seen", "\\Flagged "]
    dates = ["2024-01-01T09:30:00+00:00", "2024-03-31T00:59:59+00:00", "2024-03-31T01:00:00+00:00",
             "2024-10-27T00:30:00+00:00", "1900-01-01T10:15:00+00:00", "1918-06-01T12:00:00+00:00",
             "2025-06-15T23:59:59Z", "2025-06-15T23:59:59.250000+00:00", "2025-06-15T10:00:00+03:00",
             "2025-06-15T10:00:00", "garbage", "", None, "2024-02-30T10:00:00+00:00",
             "2026-12-31T23:30:00+00:00", "1969-12-31T23:59:59+00:00", "2024-01-01T1a:00:00+00:00",
             "2024-01-01T10:00:00+00:0Z", "２０２４-01-01T10:00:00+00:00"]
    if not svc.db.is_pg:
        # минуты и секунды даты обычного вида PostgreSQL не проверяет (их пишет только isoformat)
        dates += ["2024-01-01T10:61:00+00:00", "2024-01-01T10:5x:00+00:00"]
    folders = ["INBOX", "Работа", "", None, "—", "Sent"]
    sizes = [None, 0, -5, 10 * 1024 - 1, 10 * 1024, 1024 * 1024, 11 * 1024 * 1024, 300]
    uid = 0
    for _ in range(400):
        uid += 1
        svc.db.add_message_index(rnd.choice([aid, aid, aid2]), rnd.choice(folders) or "", 1000, uid,
                                 f"<{uid}@ex>", rnd.choice(sizes), rnd.choice(dates), rnd.choice(flags) or "",
                                 f"x/{uid}", f"h{uid}", subject=rnd.choice(subjects) or "",
                                 from_addr=rnd.choice(froms) or "", has_attach=rnd.choice([0, 1]))
    # поля, которые индекс не пишет сам: NULL в теме, отправителе, флагах и размере
    svc.db.execute("UPDATE messages SET subject=NULL, from_addr=NULL WHERE uid % 13 = 0")
    svc.db.execute("UPDATE messages SET flags=NULL, size=NULL WHERE uid % 11 = 0")
    return aid


def test_mail_analytics_matches_rowwise_calculation(services, monkeypatch):
    """1.27.0: аналитика писем считается в базе группами и порциями по id — итог тот же, что у прежнего
    построчного расчёта (tests/analytics_rowwise.py), в том числе в поясах со сменой времени и смещением
    в полчаса, при равных частотах и при порции меньше числа писем."""
    from zoneinfo import ZoneInfo
    import analytics_rowwise
    svc = services
    aid = _tricky_index(svc)
    for chunk in (7, 1000):
        monkeypatch.setattr(A, "_AGG_CHUNK_IDS", chunk)
        monkeypatch.setattr(A, "_AGG_CHUNK_IDS_PG", chunk)
        for tz in ("UTC", "Europe/Moscow", "Europe/Berlin", "Asia/Kolkata", "America/St_Johns"):
            monkeypatch.setattr(svc, "local_tz", lambda tz=tz: ZoneInfo(tz))
            for acc in (None, aid):
                got = A.mail_analytics(svc, account_id=acc, use_cache=False)
                want = analytics_rowwise.rowwise_mail_analytics(svc, account_id=acc)
                got.pop("generated_at"), want.pop("generated_at")
                assert got == want, (chunk, tz, acc)
                assert got["overview"]["messages"] == svc.db.count_messages(acc)
    # крупнейшие письма: без размера — в конце (в PostgreSQL NULL при DESC шёл первым), равные — по id
    largest = A.mail_analytics(svc, use_cache=False)["largest"]
    assert largest[0]["size"] == 11 * 1024 * 1024 and all(x["size"] for x in largest)
