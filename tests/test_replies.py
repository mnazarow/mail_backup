"""Раздел «Ответы на письма» (1.16.0): индекс ответов, расчёт показателей, списки, API и права."""
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from mailarchiver import models, replies
from mailarchiver.models import JobType

PW = "СложныйПароль1"
NOW = int(time.time())
H = 3600
D = 86400


def _raw(*, frm, to, mid=None, cc="", subject="Вопрос", ts=None, irt=None, refs=None, extra=""):
    lines = [f"From: {frm}", f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    lines.append(f"Subject: {subject}")
    if mid:
        lines.append(f"Message-ID: <{mid}>")
    if irt:
        lines.append(f"In-Reply-To: <{irt}>")
    if refs:
        lines.append("References: " + " ".join(f"<{r}>" for r in refs))
    if ts is not None:
        lines.append("Date: " + format_datetime(datetime.fromtimestamp(ts, timezone.utc)))
    if extra:
        lines.append(extra)
    lines += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "", "Текст письма", ""]
    return "\r\n".join(lines).encode("utf-8")


class Box:
    """Помощник: письма в ящик с файлом копии и записью индекса."""
    uid = 0

    def __init__(self, svc):
        self.svc = svc

    def add(self, acc, raw, *, folder="INBOX", ts=None, flags="", subject="Вопрос", sender=""):
        Box.uid += 1
        rel, sha, size = self.svc.store.store_message(acc, folder, "/", Box.uid, raw)
        when = datetime.fromtimestamp(ts if ts is not None else NOW, timezone.utc).isoformat()
        self.svc.db.add_message_index_batch([(acc, folder, 1, Box.uid, "", size, when, flags, rel, sha,
                                              subject, sender, 0)])
        return int(self.svc.db.scalar("SELECT MAX(id) FROM messages"))


@pytest.fixture()
def world(services):
    svc = services
    db = svc.db
    a = db.create_account(models.Account(name="Иванов", host="h", username="ivanov@company.ru", password="p"))
    b = db.create_account(models.Account(name="Петрова", host="h", username="petrova@company.ru", password="p"))
    ea = db.create_employee(full_name="Иванов Иван", email="ivanov@company.ru", department="Продажи")
    eb = db.create_employee(full_name="Петрова Анна", email="petrova@company.ru", department="Бухгалтерия")
    db.link_employee_account(ea, a)
    db.link_employee_account(eb, b)
    box = Box(svc)
    t0 = NOW - 10 * D
    ids = {}
    # 1. клиент → Иванов; Иванов ответил через 2 ч; копия того же письма в «Архиве» — дубль
    ids["m1"] = box.add(a, _raw(frm="Клиент <client@partner.ru>", to="ivanov@company.ru", mid="m1@partner", ts=t0), ts=t0)
    ids["r1"] = box.add(a, _raw(frm="ivanov@company.ru", to="client@partner.ru", mid="r1@company", irt="m1@partner",
                                refs=["m1@partner"], ts=t0 + 2 * H), folder="Sent", ts=t0 + 2 * H)
    ids["m1dup"] = box.add(a, _raw(frm="client@partner.ru", to="ivanov@company.ru", mid="m1@partner", ts=t0),
                           folder="Archive", ts=t0)
    # 2. начальник партнёра → Иванову и Петровой (копия); ответила Петрова через 30 мин
    ids["m2a"] = box.add(a, _raw(frm="boss@partner.ru", to="ivanov@company.ru", cc="petrova@company.ru",
                                 mid="m2@partner", ts=t0 + D), ts=t0 + D)
    ids["m2b"] = box.add(b, _raw(frm="boss@partner.ru", to="ivanov@company.ru", cc="petrova@company.ru",
                                 mid="m2@partner", ts=t0 + D), ts=t0 + D)
    ids["r2"] = box.add(b, _raw(frm="petrova@company.ru", to="boss@partner.ru", mid="r2@company", irt="m2@partner",
                                ts=t0 + D + 1800), folder="Отправленные", ts=t0 + D + 1800)
    # 3. уведомление и рассылка — не личные
    ids["n1"] = box.add(a, _raw(frm="noreply@bank.ru", to="ivanov@company.ru", mid="n1@bank", ts=t0,
                                extra="Auto-Submitted: auto-generated"), ts=t0)
    ids["b1"] = box.add(a, _raw(frm="news@shop.ru", to="ivanov@company.ru", mid="b1@shop", ts=t0,
                                extra="List-Unsubscribe: <mailto:u@shop.ru>"), ts=t0)
    # 4. отмечено «отвечено» без ответа в архиве
    ids["f1"] = box.add(a, _raw(frm="client2@other.ru", to="ivanov@company.ru", mid="f1@other", ts=t0 + 2 * D),
                        ts=t0 + 2 * D, flags="\\Seen \\Answered")
    # 5. без ответа: старое (срок вышел) и свежее (ждёт)
    ids["u1"] = box.add(a, _raw(frm="client3@other.ru", to="ivanov@company.ru", mid="u1@other", ts=NOW - 3 * D),
                        ts=NOW - 3 * D)
    ids["u2"] = box.add(a, _raw(frm="client4@other.ru", to="ivanov@company.ru", mid="u2@other", ts=NOW - H),
                        ts=NOW - H)
    # 6. исходящие: на одно ответили через 5 ч, на другое пришёл только автоответ
    ids["o1"] = box.add(a, _raw(frm="ivanov@company.ru", to="x@ext.ru", mid="o1@company", ts=t0 + 3 * D),
                        folder="Sent", ts=t0 + 3 * D)
    ids["o1r"] = box.add(a, _raw(frm="x@ext.ru", to="ivanov@company.ru", mid="o1r@ext", irt="o1@company",
                                 ts=t0 + 3 * D + 5 * H), ts=t0 + 3 * D + 5 * H)
    ids["o2"] = box.add(a, _raw(frm="ivanov@company.ru", to="y@ext.ru", mid="o2@company", ts=t0 + 3 * D),
                        folder="Sent", ts=t0 + 3 * D)
    ids["o2auto"] = box.add(a, _raw(frm="y@ext.ru", to="ivanov@company.ru", mid="o2a@ext", irt="o2@company",
                                    subject="Автоматический ответ: Вопрос", ts=t0 + 3 * D + 60,
                                    extra="Auto-Submitted: auto-replied"), ts=t0 + 3 * D + 60)
    # 7. черновик — не в счёт; письмо без Message-ID; внутреннее письмо от Петровой с ответом через сутки
    ids["d1"] = box.add(a, _raw(frm="ivanov@company.ru", to="z@ext.ru", mid="d1@company", ts=t0), folder="Drafts",
                        ts=t0)
    ids["noid"] = box.add(a, _raw(frm="anon@other.ru", to="ivanov@company.ru", ts=t0 + 4 * D), ts=t0 + 4 * D)
    ids["i1"] = box.add(a, _raw(frm="petrova@company.ru", to="ivanov@company.ru", mid="i1@company", ts=t0 + 5 * D),
                        ts=t0 + 5 * D)
    ids["i1r"] = box.add(a, _raw(frm="ivanov@company.ru", to="petrova@company.ru", mid="i1r@company",
                                 irt="i1@company", ts=t0 + 6 * D), folder="Sent", ts=t0 + 6 * D)
    return {"svc": svc, "a": a, "b": b, "ids": ids, "t0": t0}


def _row(db, mid):
    return db.query_one("SELECT * FROM reply_msgs WHERE id=?", (mid,))


# ---------------------------------------------------------------------------
#  Разбор заголовков
# ---------------------------------------------------------------------------
def test_msg_ids_and_fingerprint():
    assert replies.msg_ids("<A@B> <c@d>") == ["a@b", "c@d"]
    assert replies.msg_ids("  x@y  ") == ["x@y"]
    assert replies.msg_ids("мусор") == []
    assert replies.fingerprint("a@b") == replies.fingerprint("a@b") > 0


@pytest.mark.parametrize("headers,sender,kind", [
    ("Auto-Submitted: auto-replied", "x@y.ru", replies.AUTOREPLY),
    ("Subject: Out of Office: vacation", "x@y.ru", replies.AUTOREPLY),
    ("Content-Type: multipart/report; report-type=delivery-status", "x@y.ru", replies.AUTOREPLY),
    ("Precedence: bulk", "x@y.ru", replies.BULK),
    ("List-Id: <team.lists.example>", "x@y.ru", replies.BULK),
    ("Auto-Submitted: auto-generated", "x@y.ru", replies.NOTIFY),
    ("X-Mailer: test", "no-reply@y.ru", replies.NOTIFY),
    ("X-Mailer: test", "noreply+tag@y.ru", replies.NOTIFY),
    ("Auto-Submitted: no", "anna@y.ru", replies.PERSONAL),
    ("X-Mailer: test", "notary@y.ru", replies.PERSONAL),
])
def test_classify(headers, sender, kind):
    import io
    msg = replies.read_headers(io.BytesIO((headers + "\r\nFrom: " + sender + "\r\n\r\nтело").encode()))
    assert replies.classify(msg, sender) == kind


def test_read_headers_stops_at_body():
    import io
    raw = b"Subject: a\r\nIn-Reply-To: <p@x>\r\n\r\nIn-Reply-To: <fake@x>\r\n"
    msg = replies.read_headers(io.BytesIO(raw))
    assert replies.msg_ids(msg.get("In-Reply-To")) == ["p@x"]


@pytest.mark.parametrize("folder,kind", [
    ("Sent", "sent"), ("INBOX.Sent", "sent"), ("Отправленные", "sent"), ("[Gmail]/Отправленные", "sent"),
    ("[Gmail]/Вся почта", "skip"), ("Drafts", "skip"), ("Спам", "skip"), ("INBOX", "other"), ("Archive", "other"),
])
def test_folder_kind(folder, kind):
    assert replies.folder_kind(folder) == kind


def test_message_ts_prefers_date_header_but_not_future():
    idt = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
    assert replies.message_ts("Fri, 09 Jan 2026 10:00:00 +0000", idt.isoformat()) == \
        int(datetime(2026, 1, 9, 10, tzinfo=timezone.utc).timestamp())
    assert replies.message_ts("Fri, 09 Jan 2099 10:00:00 +0000", idt.isoformat()) == int(idt.timestamp())
    assert replies.message_ts("кривая дата", idt.isoformat()) == int(idt.timestamp())


def test_excludes_and_work_schedule():
    m = replies.compile_excludes(["noreply@*", "@bank.ru", "robot@x.ru", "shop.ru"])
    assert m("noreply@any.ru") and m("info@bank.ru") and m("robot@x.ru") and m("a@mail.shop.ru")
    assert not m("person@x.ru") and not m("")
    from zoneinfo import ZoneInfo
    s = replies.Schedule(ZoneInfo("Europe/Moscow"), frozenset(range(1, 6)), 9 * 60, 18 * 60)
    mon9 = int(datetime(2026, 11, 9, 9, tzinfo=ZoneInfo("Europe/Moscow")).timestamp())     # понедельник
    assert s.in_hours(mon9) and not s.in_hours(mon9 - 60)
    assert s.business_seconds(mon9, mon9 + 2 * H) == 2 * H
    assert s.business_seconds(mon9 + 8 * H, mon9 + D + 1 * H) == 2 * H         # 17–18 и 9–10 следующего дня
    fri17 = mon9 + 4 * D + 8 * H
    assert s.business_seconds(fri17, fri17 + 3 * D) == 9 * H                     # пт 17–18 и пн 9–17
    assert replies.pct([1, 2, 3, 4], 0.5) == 2.5 and replies.pct([], 0.5) is None


# ---------------------------------------------------------------------------
#  Индекс
# ---------------------------------------------------------------------------
def test_index_directions_kinds_and_links(world):
    svc, ids, db = world["svc"], world["ids"], world["svc"].db
    res = replies.index_pending(svc)
    assert res["indexed"] == len(ids) and res["pending"] == 0 and res["errors"] == 0
    r = lambda k: _row(db, ids[k])  # noqa: E731
    assert r("m1")["dir"] == replies.IN and r("m1")["addressed"] == replies.ADDR_TO
    assert r("r1")["dir"] == replies.OUT and r("r1")["peer"] == "client@partner.ru"
    assert r("m1")["reply_id"] == ids["r1"] and r("m1")["reply_ts"] - r("m1")["ts"] == 2 * H
    assert r("m1dup")["dir"] == replies.SKIP                                    # то же письмо в другой папке
    assert r("m2a")["reply_ts"] is None and r("m2a")["other_ts"] == r("m2a")["ts"] + 1800   # ответила коллега
    assert r("m2b")["reply_id"] == ids["r2"] and r("m2b")["addressed"] == replies.ADDR_CC
    assert r("n1")["kind"] == replies.NOTIFY and r("b1")["kind"] == replies.BULK
    assert r("f1")["flag_ans"] == 1 and r("f1")["reply_ts"] is None
    assert r("o1")["reply_ts"] - r("o1")["ts"] == 5 * H
    assert r("o2")["reply_ts"] is None and r("o2auto")["kind"] == replies.AUTOREPLY   # автоответ — не ответ
    assert r("d1")["dir"] == replies.SKIP
    assert r("noid")["mid"] < 0 and r("noid")["dir"] == replies.IN
    assert r("i1")["domains"] == "company.ru" and r("i1")["reply_ts"] - r("i1")["ts"] == D


def test_reply_indexed_before_original_is_linked(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    box = Box(svc)
    reply = box.add(a, _raw(frm="a@c.ru", to="x@e.ru", mid="r@c", irt="o@e", ts=NOW - H), folder="Sent", ts=NOW - H)
    replies.index_pending(svc)
    orig = box.add(a, _raw(frm="x@e.ru", to="a@c.ru", mid="o@e", ts=NOW - 2 * H), ts=NOW - 2 * H)   # пришло позже
    replies.index_pending(svc)
    assert _row(db, orig)["reply_id"] == reply


def test_deleted_messages_leave_index_and_rebuild(world):
    svc, ids, db = world["svc"], world["ids"], world["svc"].db
    replies.index_pending(svc)
    db.execute("DELETE FROM messages WHERE id=?", (ids["u1"],))
    assert _row(db, ids["u1"]) is None
    replies.reset_index(db)
    assert db.scalar("SELECT COUNT(*) FROM reply_msgs") == 0 and replies.pending_count(db) == len(ids) - 1
    progress = []
    replies.index_pending(svc, progress=lambda d, t, m: progress.append((d, t)))
    assert db.scalar("SELECT COUNT(*) FROM reply_msgs") == len(ids) - 1 and progress[-1][0] == len(ids) - 1


def test_reset_during_pass_discards_stale_batch(world):
    svc, db = world["svc"], world["svc"].db
    calls = []

    def progress(done, total, text):
        calls.append(done)
        if len(calls) == 1:
            replies.reset_index(db)           # «Пересобрать» посреди прохода
    replies.BATCH, old = 5, replies.BATCH
    try:
        res = replies.index_pending(svc, progress=progress)
    finally:
        replies.BATCH = old
    assert res["stopped"] == "reset"
    assert db.scalar("SELECT COUNT(*) FROM reply_msgs") == 0     # устаревшая порция не записана


def test_unreadable_file_still_indexed(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    mid = Box(svc).add(a, _raw(frm="x@e.ru", to="a@c.ru", mid="q@e", ts=NOW - D), ts=NOW - D,
                       sender="Икс <x@e.ru>")
    db.execute("UPDATE messages SET stored_path='нет/такого.eml' WHERE id=?", (mid,))
    events = []
    res = replies.index_pending(svc, event=lambda lvl, msg: events.append(lvl))
    assert res["errors"] == 1 and events == ["WARNING"]
    row = _row(db, mid)
    assert row["dir"] == replies.IN and row["peer"] == "x@e.ru"


# ---------------------------------------------------------------------------
#  Показатели
# ---------------------------------------------------------------------------
def test_analyze_summary(world):
    svc = world["svc"]
    replies.index_pending(svc)
    d = replies.analyze(svc, since=NOW - 30 * D, until=NOW + 1, sla_hours=24, use_cache=False)
    s = d["summary"]
    # личные входящие: m1, m2a, m2b, f1, u1, u2, o1r, noid, i1 (дубль, черновик, рассылка, уведомление, автоответ — нет)
    assert s["in_total"] == 9
    assert (s["own"], s["colleague"], s["flag"], s["unanswered"]) == (3, 1, 1, 4)
    assert s["overdue"] == 3 and s["waiting"] == 1                   # u1, noid, o1r просрочены; u2 ждёт
    assert s["ontime"] == 4 and s["late"] == 0                       # i1 — ровно через сутки: в срок
    assert s["answered_pct"] == round(100 * 5 / 9, 1)
    assert s["out_total"] == 5 and s["out_replied"] == 1 and s["out_answers"] == 3
    assert s["out_median_wait_s"] == 5 * H
    kinds = {k["kind"]: k["value"] for k in s["kinds"]}
    assert kinds[replies.BULK] == 1 and kinds[replies.NOTIFY] == 1 and kinds[replies.AUTOREPLY] == 1
    acc = {r["name"]: r for r in d["accounts"]}
    assert acc["Петрова"]["own"] == 1 and acc["Петрова"]["department"] == "Бухгалтерия"
    assert acc["Иванов"]["colleague"] == 1 and acc["Иванов"]["out"] == 4
    deps = {r["name"] for r in d["departments"]}
    assert deps == {"Продажи", "Бухгалтерия"}
    assert sum(x["value"] for x in d["delay_hist"]) == 4
    assert sum(x["value"] for x in d["age_hist"]) == 4
    assert d["threads"]["total"] >= 1 and d["threads"]["max_len"] >= 2
    assert d["prev"] is not None and d["prev"]["in_total"] == 0
    assert sum(t["in"] for t in d["trend"]) == 9
    assert d["period"]["bucket"] == "day"
    hours = sum(h["count"] for h in d["by_hour"])
    assert hours == 9 and sum(sum(r) for r in d["heat"]["count"]) == 9


def test_analyze_filters(world):
    svc, a = world["svc"], world["a"]
    replies.index_pending(svc)
    common = dict(since=NOW - 30 * D, until=NOW + 1, sla_hours=24, use_cache=False)
    only_a = replies.analyze(svc, account_ids=[a], **common)["summary"]
    assert only_a["in_total"] == 8
    internal = replies.analyze(svc, scope="internal", **common)["summary"]
    assert internal["in_total"] == 1                                 # только i1 от Петровой
    external = replies.analyze(svc, scope="external", **common)["summary"]
    assert external["in_total"] == 8
    to_only = replies.analyze(svc, addressed="to", **common)["summary"]
    assert to_only["in_total"] == 8                                  # m2b — Петрова в копии
    all_kinds = replies.analyze(svc, kinds={0, 1, 2, 3, 4}, **common)["summary"]
    assert all_kinds["in_total"] == 12
    svc.db.set_setting("replies.exclude_senders", ["@other.ru"])
    excl = replies.analyze(svc, **common)["summary"]
    assert excl["in_total"] == 5                                     # без f1, u1, u2, noid (other.ru)
    new_only = replies.analyze(svc, incoming="new", **common)["summary"]
    assert new_only["in_total"] == 4                                 # без o1r — ответа на наше письмо
    svc.db.set_setting("replies.exclude_senders", [])
    svc.db.set_setting("replies.internal_domains", ["partner.ru"])
    assert replies.analyze(svc, scope="internal", **common)["summary"]["in_total"] == 4


def test_business_hours_sla(services):
    svc, db = services, services.db
    db.set_setting("scheduler.timezone", "UTC")
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    box = Box(svc)
    fri17 = int(datetime(2026, 10, 30, 17, tzinfo=timezone.utc).timestamp())   # пятница 17:00
    o = box.add(a, _raw(frm="x@e.ru", to="a@c.ru", mid="o@e", ts=fri17), ts=fri17)
    box.add(a, _raw(frm="a@c.ru", to="x@e.ru", mid="r@c", irt="o@e", ts=fri17 + 3 * D - 7 * H),   # пн 10:00
            folder="Sent", ts=fri17 + 3 * D - 7 * H)
    replies.index_pending(svc)
    common = dict(since=fri17 - D, until=fri17 + 5 * D, sla_hours=4, now=fri17 + 5 * D, use_cache=False)
    cal = replies.analyze(svc, **common)["summary"]
    assert cal["late"] == 1 and cal["median_s"] == 3 * D - 7 * H
    biz = replies.analyze(svc, sla_business=True, **common)["summary"]
    assert biz["ontime"] == 1 and biz["median_business_s"] == 2 * H    # 17–18 пт и 9–10 пн
    lst = replies.list_messages(svc, mode="late", since=fri17 - D, until=fri17 + 5 * D, sla_hours=4,
                                now=fri17 + 5 * D)
    assert [i["id"] for i in lst["items"]] == [o]


def test_list_modes_paging_and_csv(world):
    svc, ids = world["svc"], world["ids"]
    replies.index_pending(svc)
    common = dict(since=NOW - 30 * D, until=NOW + 1, sla_hours=24)
    un = replies.list_messages(svc, mode="unanswered", **common)
    assert {i["id"] for i in un["items"]} == {ids["u1"], ids["u2"], ids["noid"], ids["o1r"]}
    assert [i["id"] for i in un["items"]] == sorted([i["id"] for i in un["items"]],
                                                     key=lambda x: _row(svc.db, x)["ts"])
    over = replies.list_messages(svc, mode="overdue", **common)
    assert {i["id"] for i in over["items"]} == {ids["u1"], ids["noid"], ids["o1r"]}
    new = replies.list_messages(svc, mode="overdue", incoming="new", **common)
    assert {i["id"] for i in new["items"]} == {ids["u1"], ids["noid"]}
    out = replies.list_messages(svc, mode="out_noreply", **common)
    assert ids["o2"] in {i["id"] for i in out["items"]}
    page1 = replies.list_messages(svc, mode="unanswered", limit=3, order="new", **common)
    page2 = replies.list_messages(svc, mode="unanswered", limit=3, offset=3, order="new", **common)
    assert page1["total"] == 4 and len(page1["items"]) == 3 and len(page2["items"]) == 1
    assert page1["items"][0]["id"] == ids["u2"]
    q = replies.list_messages(svc, mode="unanswered", q="client3", **common)
    assert [i["id"] for i in q["items"]] == [ids["u1"]]
    text = replies.list_csv(svc, timezone.utc, un)
    assert text.startswith("﻿Дата;Ящик") and "client3@other.ru" in text
    with pytest.raises(ValueError):
        replies.list_messages(svc, mode="нет такого")


def test_csv_neutralizes_formulas():
    assert replies._csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and replies._csv_safe("Тема") == "Тема"


# ---------------------------------------------------------------------------
#  Задание и планировщик
# ---------------------------------------------------------------------------
def test_job_handler_and_scheduler_tick(world):
    svc = world["svc"]
    from mailarchiver.queue.jobs import HANDLERS
    from mailarchiver.queue.manager import SINGLETON_JOB_TYPES
    assert JobType.REPLY_INDEX in HANDLERS and JobType.REPLY_INDEX in SINGLETON_JOB_TYPES
    from mailarchiver.scheduler.scheduler import SchedulerService
    sch = SchedulerService(svc)
    jid = sch._reply_index_tick()                                        # noqa: SLF001
    assert jid and svc.db.get_job(jid)["type"] == JobType.REPLY_INDEX
    assert sch._reply_index_tick() is None                               # noqa: SLF001 — уже в очереди
    svc.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
    svc.db.set_setting("replies.enabled", False)
    assert sch._reply_index_tick() is None                               # noqa: SLF001 — выключено


# ---------------------------------------------------------------------------
#  API и права
# ---------------------------------------------------------------------------
def _admin(client):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200


def _seed(client):
    svc = client.app.state.services
    db = svc.db
    a = db.create_account(models.Account(name="Иванов", host="h", username="ivanov@company.ru", password="p"))
    b = db.create_account(models.Account(name="Петрова", host="h", username="petrova@company.ru", password="p"))
    e = db.create_employee(full_name="Иванов Иван", email="ivanov@company.ru", department="Продажи")
    db.link_employee_account(e, a)
    box = Box(svc)
    box.add(a, _raw(frm="c@p.ru", to="ivanov@company.ru", mid="x1@p", ts=NOW - 3 * D, subject="=SUM(1)"),
            ts=NOW - 3 * D, subject="=SUM(1)")
    box.add(b, _raw(frm="d@p.ru", to="petrova@company.ru", mid="x2@p", ts=NOW - 2 * D), ts=NOW - 2 * D)
    replies.index_pending(svc)
    return svc, a, b


def test_api_admin(client):
    _admin(client)
    svc, a, b = _seed(client)
    st = client.get("/api/replies/status").json()
    assert st["total"] == 2 and st["pending"] == 0 and st["enabled"] is True
    d = client.get("/api/replies/summary", params={"days": 30}).json()
    assert d["summary"]["in_total"] == 2 and len(d["accounts"]) == 2
    assert client.get("/api/replies/summary", params={"days": 30, "department": "Продажи"}).json()[
        "summary"]["in_total"] == 1
    assert client.get("/api/replies/summary", params={"date_from": "2000-01-01", "date_to": "2000-01-31"}).json()[
        "summary"]["in_total"] == 0
    assert client.get("/api/replies/summary", params={"days": 30, "kinds": "9"}).status_code == 400
    lst = client.get("/api/replies/list", params={"mode": "overdue", "days": 30}).json()
    assert lst["total"] == 2
    csv = client.get("/api/replies/list.csv", params={"mode": "overdue", "days": 30})
    assert csv.status_code == 200 and "text/csv" in csv.headers["content-type"]
    assert "attachment" in csv.headers["content-disposition"] and "'=SUM(1)" in csv.text
    assert any(r["action"] == "replies_export" for r in svc.db.list_audit())
    meta = client.get("/api/replies/meta").json()
    assert "Продажи" in meta["departments"] and meta["settings"]["sla_hours"] == 24
    svc.queue.stop()                                                   # задание не должно успеть выполниться
    job = client.post("/api/replies/rebuild").json()
    assert job["job_id"] and svc.db.scalar("SELECT COUNT(*) FROM reply_msgs") == 0
    assert client.post("/api/replies/update").json()["job_id"] == job["job_id"]       # уже в очереди
    assert any(r["action"] == "replies_rebuild" for r in svc.db.list_audit())


def test_api_mailbox_sees_only_own(client):
    _admin(client)
    svc, a, b = _seed(client)
    from mailarchiver.security import sign_value
    from mailarchiver.web.auth import COOKIE_NAME
    svc.db.create_session("mbx-r", 0, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                          role="mailbox", account_id=a)
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, sign_value("mbx-r", svc.cfg.secret_key()))
    d = client.get("/api/replies/summary", params={"days": 30, "account_id": b}).json()
    assert d["summary"]["in_total"] == 1 and [r["id"] for r in d["accounts"]] == [a]
    lst = client.get("/api/replies/list", params={"mode": "unanswered", "days": 30}).json()
    assert {i["account_id"] for i in lst["items"]} == {a}
    st = client.get("/api/replies/status").json()
    assert st["total"] == 1                                         # только свои ящики
    assert client.post("/api/replies/rebuild").status_code == 403
    assert client.get("/api/replies/meta").json()["departments"] == []


def test_api_operator_forbidden(client):
    _admin(client)
    svc = client.app.state.services
    from mailarchiver.security import hash_password
    svc.db.create_user("op", hash_password(PW), role="operator")
    client.post("/api/logout")
    assert client.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    assert client.get("/api/replies/summary").status_code == 403


def test_ui_has_section():
    from mailarchiver.web.assets import bundle_text
    js = bundle_text()
    assert "{id:'replies'" in js and "replies:viewReplies" in js and "reply_index:" in js


# ---------------------------------------------------------------------------
#  Доделки 1.16.0: связь по теме, порядок разбора, переразбор при смене адресов
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("subject", ["Re: Договор поставки", "RE: RE: Fwd: договор  поставки", "Отв: Договор поставки",
                                     "[EXT] Re[2]: Договор поставки", "AW: Договор поставки"])
def test_subject_key_strips_prefixes(subject):
    assert replies.subject_key(subject) == replies.subject_key("Договор поставки")
    assert replies.subject_key("Re: Re:") is None and replies.subject_key("") is None


def test_reply_without_in_reply_to_is_linked_by_subject(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    box = Box(svc)
    t = NOW - 20 * D
    x = box.add(a, _raw(frm="client@e.ru", to="a@c.ru", mid="x@e", subject="Счёт за ноябрь", ts=t), ts=t,
                subject="Счёт за ноябрь")
    box.add(a, _raw(frm="a@c.ru", to="client@e.ru", mid="r@c", subject="Re: Счёт за ноябрь", ts=t + 3 * H),
            folder="Sent", ts=t + 3 * H, subject="Re: Счёт за ноябрь")              # ответили новым письмом
    o = box.add(a, _raw(frm="a@c.ru", to="partner@p.ru", mid="o@c", subject="Акт сверки", ts=t), folder="Sent",
                ts=t, subject="Акт сверки")
    box.add(a, _raw(frm="partner@p.ru", to="a@c.ru", mid="or@p", subject="RE: Акт сверки", ts=t + D), ts=t + D,
            subject="RE: Акт сверки")
    # не ответы: другой собеседник; позже двух недель; ответ на другое письмо (с In-Reply-To)
    y = box.add(a, _raw(frm="other@e.ru", to="a@c.ru", mid="y@e", subject="Счёт за ноябрь", ts=t), ts=t)
    z = box.add(a, _raw(frm="late@e.ru", to="a@c.ru", mid="z@e", subject="Претензия", ts=t - 30 * D), ts=t - 30 * D)
    box.add(a, _raw(frm="a@c.ru", to="late@e.ru", mid="zr@c", subject="Re: Претензия", ts=t - 10 * D),
            folder="Sent", ts=t - 10 * D)
    replies.index_pending(svc)
    rx, ro = _row(db, x), _row(db, o)
    assert rx["reply_how"] == 2 and rx["reply_ts"] - rx["ts"] == 3 * H
    assert ro["reply_how"] == 2 and ro["reply_ts"] - ro["ts"] == D
    assert _row(db, y)["reply_id"] is None and _row(db, z)["reply_id"] is None
    common = dict(since=t - 40 * D, until=NOW + 1, sla_hours=24)
    s = replies.analyze(svc, use_cache=False, **common)["summary"]
    assert s["by_subject_in"] == 1 and s["by_subject_out"] == 1 and s["own"] == 1
    lst = replies.list_messages(svc, mode="answered", **common)
    assert [i["by_subject"] for i in lst["items"] if i["id"] == x] == [True]
    db.set_setting("replies.match_subject", False)                   # выключено — такие ответы не в счёт
    s = replies.analyze(svc, use_cache=False, **common)["summary"]
    assert s["own"] == 0 and s["out_replied"] == 0
    assert x in {i["id"] for i in replies.list_messages(svc, mode="unanswered", **common)["items"]}
    assert o in {i["id"] for i in replies.list_messages(svc, mode="out_noreply", **common)["items"]}


def test_header_reply_wins_over_subject(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    box = Box(svc)
    t = NOW - 5 * D
    x = box.add(a, _raw(frm="c@e.ru", to="a@c.ru", mid="x@e", subject="Вопрос по заказу", ts=t), ts=t)
    box.add(a, _raw(frm="a@c.ru", to="c@e.ru", mid="s@c", subject="Re: Вопрос по заказу", ts=t + H), folder="Sent",
            ts=t + H)
    replies.index_pending(svc)
    assert _row(db, x)["reply_how"] == 2
    hdr = box.add(a, _raw(frm="a@c.ru", to="c@e.ru", mid="h@c", irt="x@e", subject="Re: Вопрос по заказу",
                          ts=t + 2 * H), folder="Sent", ts=t + 2 * H)
    replies.index_pending(svc)
    row = _row(db, x)
    assert row["reply_how"] == 1 and row["reply_id"] == hdr


def test_backlog_goes_newest_first_and_new_mail_first(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="A", host="h", username="a@c.ru", password="p"))
    box = Box(svc)
    ids = [box.add(a, _raw(frm=f"c{i}@e.ru", to="a@c.ru", mid=f"m{i}@e", ts=NOW - (40 - i) * D), ts=NOW - (40 - i) * D)
           for i in range(12)]
    calls = []
    replies.BATCH, old = 4, replies.BATCH
    try:
        replies.index_pending(svc, cancelled=lambda: len(calls) >= 1, progress=lambda d, t, m: calls.append(d))
        done = [int(r[0]) for r in db.query("SELECT id FROM reply_msgs ORDER BY id")]
        assert done == ids[-4:]                                       # сначала самые новые давние письма
        st = replies.status(svc)
        assert st["pending"] == 8 and st["since"][:10] == datetime.fromtimestamp(NOW - 32 * D, timezone.utc).strftime("%Y-%m-%d")
        assert st["readdress"] == []
        fresh = box.add(a, _raw(frm="n@e.ru", to="a@c.ru", mid="new@e", ts=NOW), ts=NOW)
        calls.clear()
        replies.index_pending(svc, cancelled=lambda: len(calls) >= 1, progress=lambda d, t, m: calls.append(d))
        assert _row(db, fresh) is not None                           # новое письмо — раньше давних
        replies.index_pending(svc)
    finally:
        replies.BATCH = old
    assert db.scalar("SELECT COUNT(*) FROM reply_msgs") == 13 and replies.status(svc)["pending"] == 0
    assert replies.status(svc)["since"] is None


def test_changed_addresses_reindex_the_mailbox(services):
    svc, db = services, services.db
    a = db.create_account(models.Account(name="Общий ящик", host="h", username="info@c.ru", password="p"))
    box = Box(svc)
    m = box.add(a, _raw(frm="boss@c.ru", to="client@e.ru", mid="b@c", ts=NOW - D), ts=NOW - D)   # отправлено из INBOX
    replies.index_pending(svc)
    assert _row(db, m)["dir"] == replies.IN and not replies.needs_pass(svc)
    e = db.create_employee(full_name="Начальник", email="boss@c.ru")
    db.link_employee_account(e, a)                                   # теперь это адрес самого ящика
    assert replies.needs_pass(svc)
    assert replies.status(svc)["readdress"] == []                    # ещё не замечено — заметит проход
    res = replies.index_pending(svc, cancelled=lambda: True)         # заметил и сразу прерван
    assert res["readdressed"] == 1 and _row(db, m)["dir"] == replies.IN
    st = replies.status(svc)
    assert st["readdress"] == ["Общий ящик"] and st["pending"] == 1 and st["since"] is None
    replies.index_pending(svc)
    assert _row(db, m)["dir"] == replies.OUT and _row(db, m)["peer"] == "client@e.ru"
    assert not replies.needs_pass(svc) and replies.status(svc)["readdress"] == []


def test_thread_split_in_comparisons(world):
    svc = world["svc"]
    replies.index_pending(svc)
    d = replies.analyze(svc, since=NOW - 30 * D, until=NOW + 1, use_cache=False)
    split = {x["key"]: x["count"] for x in d["thread_split"]}
    assert split == {"new": 8, "followup": 1}


def test_schema_upgrade_from_first_build(tmp_path):
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE reply_msgs(id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, mid INTEGER NOT NULL, "
                 "parent INTEGER, root INTEGER, dir INTEGER NOT NULL, kind INTEGER NOT NULL DEFAULT 0, "
                 "addressed INTEGER NOT NULL DEFAULT 0, peer TEXT NOT NULL DEFAULT '', domains TEXT NOT NULL DEFAULT '', "
                 "rcpt INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL, flag_ans INTEGER NOT NULL DEFAULT 0, "
                 "reply_id INTEGER, reply_ts INTEGER, other_ts INTEGER)")
    conn.execute("INSERT INTO meta VALUES('replies_last_id', '100')")
    conn.commit()
    replies.ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reply_msgs)")}
    assert {"subj", "reply_how"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM meta WHERE key='replies_last_id'").fetchone()[0] == 0   # разбор заново
