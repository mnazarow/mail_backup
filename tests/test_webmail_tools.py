"""Почтовый клиент 1.18.0: правила, автоответ, метки, шаблоны, адресная книга, «напомнить позже», отложенная
отправка, архивы .zip, уведомления о прочтении, приглашения.

Сервер IMAP — учебный, SMTP — подменённый (как в test_webmail.py).
"""
import email
import io
import zipfile
from datetime import datetime, timedelta, timezone
from email import policy

import test_webmail as base
from test_webmail import _ids, _mail, _server_subjects

from mailarchiver import webmail as wm
from mailarchiver import webmail_tools as wt

# Учебный IMAP, подменённый SMTP и ящики — те же, что в test_webmail.py (фикстуры берутся оттуда).
world, box, api = base.world, base.box, base.api
smtp = base.smtp          # autouse: SMTP подменён во всех тестах модуля


def _new(world, subject, *, mid, days_ago=0, **kw):
    return world.add("ivanov@x.ru", "INBOX", _mail(subject, mid=mid, **kw), days_ago=days_ago)


def _subjects(svc, aid, folder, gone=0):
    return sorted(svc.db.get_message(pk)["subject"] for pk in _ids(svc, aid, folder, gone=gone))


def _future(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
#  Правила сортировки
# ---------------------------------------------------------------------------
def test_rules_sort_new_mail_only_once(box):
    svc, world, acc = box
    wm.folders(svc, acc, refresh=True)
    wt.save_rule(svc, acc, {"name": "Отчёты", "conditions": [{"field": "subject", "op": "contains", "value": "отчёт"}],
                            "actions": [{"type": "move", "folder": "Projects"}, {"type": "flag"}]})
    wt.save_rule(svc, acc, {"name": "Начальник", "conditions": [{"field": "from", "op": "equals",
                                                                 "value": "boss@x.ru"}],
                            "actions": [{"type": "tag", "keyword": "$label1"}, {"type": "read"}]})
    # письма, которые уже были до правил, правила не трогают
    assert wt.process_new(svc, acc, sync=True)["checked"] == 0
    _new(world, "Отчёт за неделю", mid="<r1@ext>")
    _new(world, "Привет", mid="<r2@ext>", frm="boss@x.ru")
    _new(world, "Отчёт годовой", mid="<r3@ext>", days_ago=10)       # старое письмо, пришедшее копированием
    res = wt.process_new(svc, acc, sync=True)
    assert res["checked"] == 2 and res["matched"] == 2 and res["moved"] == 1
    assert _server_subjects(world, "Projects") == ["Отчёт за неделю"]
    projects = _ids(svc, acc.id, "Projects", gone=0)
    assert "\\Flagged" in svc.db.get_message(projects[0])["flags"]
    boss = next(m for m in world.boxes["ivanov@x.ru"]["INBOX"].messages.values() if b"boss@x.ru" in m.raw)
    assert {b"$label1", b"\\Seen"} <= boss.flags
    rules = wt.list_rules(svc, acc.id)
    assert [r["hits"] for r in rules] == [1, 1]
    # письмо вернули во «Входящие» — правило его второй раз не уносит
    wm.move(svc, acc, projects, "INBOX")
    assert wt.process_new(svc, acc, sync=True)["matched"] == 0
    assert "Отчёт за неделю" in _server_subjects(world, "INBOX")
    # «Применить к письмам папки» — уносит
    res = wt.apply_rules_to_folder(svc, acc, "INBOX", rule_ids=[rules[0]["id"]])
    assert res["moved"] == 2 and _server_subjects(world, "Projects") == ["Отчёт годовой", "Отчёт за неделю"]


def test_rule_conditions_and_validation(box):
    svc, world, acc = box
    wm.folders(svc, acc, refresh=True)
    m = wt._Msg(svc, acc, svc.db.get_message(_ids(svc, acc.id)[1]))       # «Акт сверки», Cc: petrov, ivanov
    ok = (lambda **c: wt._cond_ok(m, {"op": "contains", "value": "", **c}))  # noqa: E731
    assert ok(field="cc", value="petrov@")
    assert ok(field="to_cc", op="equals", value="ivanov@x.ru")
    assert not ok(field="from", op="equals", value="ext.ru")
    assert ok(field="from", op="ends", value="@ext.ru")
    assert ok(field="body", value="текст письма")
    assert ok(field="attach", op="no") and ok(field="size", op="lt", value="1")
    assert ok(field="list", op="no") and ok(field="subject", op="starts", value="акт")
    for bad in ({"conditions": [], "actions": [{"type": "read"}]},
                {"conditions": [{"field": "subject", "op": "contains", "value": "x"}], "actions": []},
                {"conditions": [{"field": "subject", "op": "contains", "value": "x"}],
                 "actions": [{"type": "move", "folder": "Нет"}]},
                {"conditions": [{"field": "subject", "op": "regex", "value": "x"}], "actions": [{"type": "read"}]},
                {"conditions": [{"field": "subject", "op": "contains", "value": "x"}],
                 "actions": [{"type": "tag", "keyword": "$nope"}]}):
        try:
            wt.save_rule(svc, acc, bad)
        except wm.ValidationError:
            continue
        raise AssertionError(bad)


def test_block_sender(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    res = wt.block_sender(svc, acc, "Клиент <client@ext.ru>", ids=ids[:1])
    assert res == {"address": "client@ext.ru", "moved": 1, "action": "junk"}
    assert wt.block_sender(svc, acc, "client@ext.ru")["moved"] == 0
    blocked = [r for r in wt.list_rules(svc, acc.id) if r["kind"] == "block"]
    assert len(blocked) == 1
    _new(world, "Снова реклама", mid="<b1@ext>")
    assert wt.process_new(svc, acc, sync=True)["moved"] == 1
    assert "Снова реклама" in _server_subjects(world, "Junk")
    try:
        wt.block_sender(svc, acc, "ivanov@x.ru")
    except wm.ValidationError:
        pass
    else:
        raise AssertionError("свой адрес заблокирован")


# ---------------------------------------------------------------------------
#  Автоответ
# ---------------------------------------------------------------------------
def test_autoreply(box, smtp):
    svc, world, acc = box
    try:
        wt.save_autoreply(svc, acc, {"enabled": True, "body": ""})
    except wm.ValidationError:
        pass
    else:
        raise AssertionError("пустой автоответ")
    wt.save_autoreply(svc, acc, {"enabled": True, "subject": "", "body": "<p>Я в отпуске до 20-го.</p>",
                                 "date_to": "2099-01-01"}, actor="ivanov@x.ru")
    assert wt.get_autoreply(svc, acc.id)["active"]
    _new(world, "Вопрос", mid="<q1@ext>")
    _new(world, "Ещё вопрос", mid="<q2@ext>")                               # тот же отправитель — один ответ
    _new(world, "Рассылка", mid="<q3@ext>", frm="news@shop.ru", extra="List-Id: <shop>\r\n")
    _new(world, "Робот", mid="<q4@ext>", frm="noreply@bank.ru")
    _new(world, "Не мне", mid="<q5@ext>", frm="other@ext.ru", to="all@x.ru")
    _new(world, "Автоматическое", mid="<q6@ext>", frm="auto@ext.ru", extra="Auto-Submitted: auto-replied\r\n")
    res = wt.process_new(svc, acc, sync=True)
    assert res["autoreplied"] == 1 and len(smtp.sent) == 1
    msg = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert smtp.sent[0]["rcpts"] == ["client@ext.ru"]
    assert msg["Auto-Submitted"] == "auto-replied" and msg["Subject"] == "Автоответ: Вопрос"
    assert msg["In-Reply-To"] == "<q1@ext>" and "в отпуске" in msg.get_body(("plain",)).get_content()
    _new(world, "Третий вопрос", mid="<q7@ext>")
    assert wt.process_new(svc, acc, sync=True)["autoreplied"] == 0               # раз в 4 дня
    wt.save_autoreply(svc, acc, {"enabled": True, "body": "x", "date_from": "2099-01-01"})
    assert not wt.get_autoreply(svc, acc.id)["active"]


# ---------------------------------------------------------------------------
#  Метки, шаблоны, адресная книга
# ---------------------------------------------------------------------------
def test_tags(box):
    svc, world, acc = box
    tags = wt.list_tags(svc, acc.id)
    assert [t["keyword"] for t in tags] == ["$label1", "$label2", "$label3", "$label4", "$label5"]
    t = wt.save_tag(svc, acc.id, {"name": "Срочно", "color": "#ff0000"})
    assert t["keyword"] == f"$MA{t['id']}"
    wt.delete_tag(svc, acc.id, tags[4]["id"])
    assert len(wt.list_tags(svc, acc.id)) == 5                             # удалённые по умолчанию не возвращаются
    ids = _ids(svc, acc.id)
    wt.tag_messages(svc, acc, ids[:2], t["keyword"])
    assert wm.list_messages(svc, acc.id, "INBOX", filt=f"tag:{t['keyword']}")["total"] == 2
    assert wm.list_ids(svc, acc.id, "INBOX", filt=f"tag:{t['keyword']}")["ids"] == sorted(ids[:2], reverse=True) or \
        len(wm.list_ids(svc, acc.id, "INBOX", filt=f"tag:{t['keyword']}")["ids"]) == 2
    wt.tag_messages(svc, acc, ids[:1], t["keyword"], on=False)
    assert wm.list_messages(svc, acc.id, "INBOX", filt=f"tag:{t['keyword']}")["total"] == 1
    server = [m.flags for m in world.boxes["ivanov@x.ru"]["INBOX"].messages.values()]
    assert sum(1 for f in server if t["keyword"].encode() in f) == 1
    for bad in ({"name": ""}, {"name": "x", "color": "red"}, {"name": "Срочно"}):
        try:
            wt.save_tag(svc, acc.id, bad)
        except wm.ValidationError:
            continue
        raise AssertionError(bad)


def test_templates_per_owner(services):
    svc = services
    t = wt.save_template(svc, "mailbox:emp:1", {"name": "Счёт", "subject": "Счёт на оплату",
                                                "body": "<p>Добрый день!</p><script>x</script>"})
    assert "<script" not in t["body"]
    assert [x["name"] for x in wt.list_templates(svc, "mailbox:emp:1")] == ["Счёт"]
    assert wt.list_templates(svc, "admin:adm") == []
    wt.delete_template(svc, "admin:adm", t["id"])                           # чужой — не удаляется
    assert wt.list_templates(svc, "mailbox:emp:1")
    wt.save_template(svc, "mailbox:emp:1", {"name": "Счёт 2"}, t["id"])
    assert wt.list_templates(svc, "mailbox:emp:1")[0]["name"] == "Счёт 2"


def test_address_book_import_export_groups(box):
    svc, world, acc = box
    vcf = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ольга Смирнова\r\nEMAIL;TYPE=work:o.smirnova@partner.ru\r\n"
           "TEL:+7 900 000-00-00\r\nORG:Партнёр\r\nCATEGORIES:Партнёры,Договоры\r\nEND:VCARD\r\n"
           "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Петров;Пётр;;;\r\nEMAIL:petrov@partner.ru\r\nEND:VCARD\r\n")
    res = wt.import_book(svc, acc.id, "contacts.vcf", vcf.encode())
    assert res["added"] == 2
    csv_text = "Имя;Email;Группы\r\nАнна;anna@x.ru;Отдел\r\nБез адреса;;\r\nСмирнова О.;o.smirnova@partner.ru;\r\n"
    res = wt.import_book(svc, acc.id, "c.csv", csv_text.encode("cp1251"))
    assert res["added"] == 1 and res["updated"] == 1
    book = {c["email"]: c for c in wt.list_book(svc, acc.id)}
    assert book["o.smirnova@partner.ru"]["name"] == "Смирнова О." and book["o.smirnova@partner.ru"]["company"] == "Партнёр"
    assert book["petrov@partner.ru"]["name"] == "Петров Пётр"
    assert {g["name"]: g["count"] for g in wt.book_groups(svc, acc.id)} == {"Договоры": 1, "Отдел": 1, "Партнёры": 1}
    sug = wm.contacts(svc, acc, "партн")
    group = next(s for s in sug if s.get("group"))
    assert group["members"] == "Смирнова О. <o.smirnova@partner.ru>"
    assert any(s["email"] == "o.smirnova@partner.ru" and s.get("book") for s in sug)       # по организации
    assert [s["email"] for s in wm.contacts(svc, acc, "петр")][:1] == ["petrov@partner.ru"]
    data, ctype, name = wt.export_book(svc, acc.id, "vcf")
    assert data.count(b"BEGIN:VCARD") == 3 and name == "contacts.vcf"
    again = wt._parse_vcards(data.decode())
    assert {c["email"] for c in again} == set(book) and any(c["groups"] == "Партнёры,Договоры" for c in again)
    data, _ct, _n = wt.export_book(svc, acc.id, "csv")
    assert "anna@x.ru" in data.decode("utf-8-sig")
    assert wt.delete_contacts(svc, acc.id, [book["anna@x.ru"]["id"]]) == 1
    try:
        wt.save_contact(svc, acc.id, {"email": "не адрес"})
    except wm.ValidationError:
        pass
    else:
        raise AssertionError("не адрес")


# ---------------------------------------------------------------------------
#  «Напомнить позже», отложенная отправка
# ---------------------------------------------------------------------------
def test_snooze_and_return(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    wm.set_flags(svc, acc, ids[:1], add=["\\Seen"])
    res = wt.snooze(svc, acc, ids[:1], _future(60))
    assert res["snoozed"] == 1 and res["folder"] == "Отложенные" and res["recorded"] == 1
    assert _server_subjects(world, "Отложенные") == ["Счёт за июнь"]
    items = wt.list_snoozed(svc, acc.id)
    assert items[0]["return_folder"] == "INBOX" and items[0]["message_pk"]
    assert wt.tick(svc)["unsnoozed"] == 0                                   # ещё рано
    svc.db.execute("UPDATE webmail_snooze SET until=?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    assert wt.tick(svc)["unsnoozed"] == 1
    assert world.boxes["ivanov@x.ru"]["Отложенные"].messages == {}
    back = [m for m in world.boxes["ivanov@x.ru"]["INBOX"].messages.values() if b"m1@ext" in m.raw]
    assert len(back) == 1 and b"\\Seen" not in back[0].flags
    assert wt.list_snoozed(svc, acc.id) == []
    for bad in ("вчера", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()):
        try:
            wt.snooze(svc, acc, ids[1:2], bad)
        except wm.ValidationError:
            continue
        raise AssertionError(bad)


def test_schedule_send(box, smtp):
    svc, world, acc = box
    owner = "mailbox:acc:1"
    d = wm.create_draft(svc, acc, owner, {"to_addrs": "client@ext.ru", "subject": "Позже", "body": "<p>Текст</p>"})
    d = wt.schedule_send(svc, acc, d["id"], owner, _future(30), actor="ivanov@x.ru", role="mailbox")
    assert d["status"] == "scheduled" and d["send_at"]
    try:
        wm.update_draft(svc, d["id"], owner, {"subject": "x"})
    except wm.ValidationError as exc:
        assert "отмените" in exc.message
    else:
        raise AssertionError("изменён запланированный")
    assert wt.send_due(svc) == 0
    d = wt.cancel_schedule(svc, d["id"], owner)
    assert d["status"] == "draft" and d["send_at"] is None
    wt.schedule_send(svc, acc, d["id"], owner, _future(30), actor="ivanov@x.ru", role="mailbox")
    svc.db.execute("UPDATE webmail_drafts SET send_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),))
    assert wt.tick(svc)["sent"] == 1 and len(smtp.sent) == 1
    assert wm.list_drafts(svc, owner) == []
    assert svc.db.query_one("SELECT user FROM audit WHERE action='webmail_send'")["user"] == "ivanov@x.ru"
    # от имени администратора, которому потом запретили писать, — не уходит
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "client@ext.ru", "subject": "Адм", "body": "x"})
    wt.schedule_send(svc, acc, d["id"], "admin:adm", _future(30), actor="adm", role="admin")
    svc.db.set_setting("webmail.admin_actions", False)
    svc.db.execute("UPDATE webmail_drafts SET send_at=?", ((datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),))
    assert wt.send_due(svc) == 0 and len(smtp.sent) == 1
    d = wm.list_drafts(svc, "admin:adm")[0]
    assert d["status"] == "draft" and "запрещено" in d["error"]
    for bad in ("2020-01-01T00:00:00+00:00", "x" * 12):
        try:
            wt.schedule_send(svc, acc, d["id"], "admin:adm", bad)
        except wm.ValidationError:
            continue
        raise AssertionError(bad)


# ---------------------------------------------------------------------------
#  Письмо: пересылка вложением, важность, уведомление, псевдонимы
# ---------------------------------------------------------------------------
def test_forward_attached_priority_receipt_alias(box, smtp):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    wm.save_identity(svc, acc, {"aliases": "sales@x.ru, info@x.ru"}, admin=True)
    ident = wm.get_identity(svc, acc)
    assert ident["from_choices"] == ["ivanov@x.ru", "sales@x.ru", "info@x.ru"]
    assert "sales@x.ru" in wm.own_addresses(svc, acc)
    try:
        wm.save_identity(svc, acc, {"aliases": "не адрес"}, admin=True)
    except wm.ValidationError:
        pass
    else:
        raise AssertionError("псевдоним не адрес")
    fields = wm.compose_fields(svc, acc, "forward_attach", None, ids[:2])
    assert fields["subject"] == "Fwd: писем 2" and [a["ctype"] for a in fields["attachments"]] == ["message/rfc822"] * 2
    d = wm.create_draft(svc, acc, "o", fields)
    wm.update_draft(svc, d["id"], "o", {"to_addrs": "boss@x.ru", "from_addr": "sales@x.ru", "priority": True,
                                         "receipt": True})
    res = wm.send(svc, acc, d["id"], "o")
    assert res["ok"] and smtp.sent[0]["from"] == "sales@x.ru"
    msg = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert msg["From"].addresses[0].addr_spec == "sales@x.ru" and msg["X-Priority"].startswith("1")
    assert msg["Importance"] == "High" and msg["Disposition-Notification-To"] == "sales@x.ru"
    parts = [p for p in msg.iter_attachments()]
    assert [p.get_content_type() for p in parts] == ["message/rfc822"] * 2
    assert parts[0].get_filename() == "Счёт за июнь.eml"
    assert parts[0].get_content()["Message-ID"] == "<m1@ext>"
    # псевдоним, которого нет в списке, не подставляется
    d = wm.create_draft(svc, acc, "o", {"to_addrs": "boss@x.ru", "from_addr": "ceo@x.ru", "subject": "x"})
    wm.send(svc, acc, d["id"], "o")
    assert smtp.sent[1]["from"] == "ivanov@x.ru"


def test_zip_messages_and_attachments(box):
    svc, world, acc = box
    raw = (b"From: a@b.ru\r\nTo: ivanov@x.ru\r\nSubject: =?utf-8?b?" +
           __import__("base64").b64encode("Файлы".encode()) + b"?=\r\nMessage-ID: <z@x>\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
           b"--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=a.pdf\r\n\r\n%PDF-1\r\n"
           b"--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=a.pdf\r\n\r\n%PDF-2\r\n"
           b"--b--\r\n")
    world.add("ivanov@x.ru", "INBOX", raw)
    wm.sync_folder(svc, acc, "INBOX")
    ids = _ids(svc, acc.id)
    fh = wt.zip_messages(svc, acc, ids)
    names = zipfile.ZipFile(io.BytesIO(fh.read())).namelist()
    assert len(names) == 4 and all(n.endswith(".eml") for n in names) and len(set(names)) == 4
    fh, name = wt.zip_attachments(svc, acc, svc.db.get_message(ids[-1]))
    z = zipfile.ZipFile(io.BytesIO(fh.read()))
    assert name == "Файлы.zip" and z.namelist() == ["a.pdf", "a (2).pdf"]
    assert z.read("a (2).pdf").startswith(b"%PDF-2")


INVITE = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nUID:ev-1@ext\r\nSEQUENCE:2\r\n"
          "DTSTART;TZID=Europe/Moscow:20261120T100000\r\nDTEND;TZID=Europe/Moscow:20261120T110000\r\n"
          "SUMMARY:Планёрка\\, отдел продаж\r\nLOCATION:Переговорная 3\r\n"
          "ORGANIZER;CN=Ольга:mailto:o.smirnova@partner.ru\r\n"
          "ATTENDEE;PARTSTAT=NEEDS-ACTION;CN=Иван:mailto:ivanov@x.ru\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def test_extras_receipt_invite(box, smtp):
    svc, world, acc = box
    extra = ("List-Unsubscribe: <mailto:unsub@shop.ru?subject=stop>, <https://shop.ru/u/1>\r\n"
             "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\nX-Priority: 1\r\n"
             "Disposition-Notification-To: Клиент <client@ext.ru>\r\n")
    world.add("ivanov@x.ru", "INBOX", _mail("Важное", mid="<e1@ext>", extra=extra))
    inv = ("From: Ольга <o.smirnova@partner.ru>\r\nTo: ivanov@x.ru\r\nSubject: Приглашение\r\nMessage-ID: <inv@x>\r\n"
           "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\n"
           "Content-Type: text/plain; charset=utf-8\r\n\r\nПриглашение\r\n--b\r\n"
           "Content-Type: text/calendar; charset=utf-8; method=REQUEST\r\n\r\n" + INVITE + "--b--\r\n")
    world.add("ivanov@x.ru", "INBOX", inv.encode())
    wm.sync_folder(svc, acc, "INBOX")
    ids = _ids(svc, acc.id)
    row = svc.db.get_message(ids[-2])
    ex = wt.message_extras(svc, acc, row, can_act=True)
    assert ex["priority"] == "high" and ex["receipt"] == {"to": "client@ext.ru"}
    assert ex["unsubscribe"] == {"mailto": "mailto:unsub@shop.ru?subject=stop", "url": "https://shop.ru/u/1",
                                 "one_click": True}
    assert ex["sender"] == "client@ext.ru" and ex["in_book"] is False and ex["blocked"] is False
    assert "receipt" not in wt.message_extras(svc, acc, row, can_act=False)
    wt.send_receipt(svc, acc, row)
    rep = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert rep.get_content_type() == "multipart/report" and smtp.sent[0]["rcpts"] == ["client@ext.ru"]
    assert "Original-Message-ID: <e1@ext>" in smtp.sent[0]["data"].decode()
    assert "receipt" not in wt.message_extras(svc, acc, svc.db.get_message(ids[-2]), can_act=True)   # $MDNSent
    row = svc.db.get_message(ids[-1])
    ex = wt.message_extras(svc, acc, row, can_act=True)
    inv = ex["invite"]
    assert inv["summary"] == "Планёрка, отдел продаж" and inv["start"] == "20.11.2026 10:00 (Europe/Moscow)"
    assert inv["can_reply"] and inv["my_status"] == "NEEDS-ACTION" and inv["location"] == "Переговорная 3"
    wt.invite_reply(svc, acc, row, "ACCEPTED", comment="Буду")
    rep = email.message_from_bytes(smtp.sent[1]["data"], policy=policy.default)
    assert smtp.sent[1]["rcpts"] == ["o.smirnova@partner.ru"] and rep["Subject"] == "Принято: Планёрка, отдел продаж"
    cal = next(p for p in rep.walk() if p.get_content_type() == "text/calendar")
    text = cal.get_content()
    assert "METHOD:REPLY" in text and "PARTSTAT=ACCEPTED" in text and "UID:ev-1@ext" in text and "SEQUENCE:2" in text
    assert wt.invite_ics(svc, acc, row).startswith(b"BEGIN:VCALENDAR")


def test_folder_tools_read_all_empty_move(box):
    svc, world, acc = box
    assert wm.mark_all_read(svc, acc, "INBOX")["marked"] == 2
    assert all(b"\\Seen" in m.flags for m in world.boxes["ivanov@x.ru"]["INBOX"].messages.values())
    ids = _ids(svc, acc.id)
    res = wm.delete(svc, acc, ids[:2])
    assert res["trashed"] == 2 and len(res["undo"][0]["ids"]) == 2 and res["undo"][0]["folder"] == "INBOX"
    wm.move(svc, acc, res["undo"][0]["ids"], res["undo"][0]["folder"])               # «Отменить»
    assert len(world.boxes["ivanov@x.ru"]["INBOX"].messages) == 3
    wm.delete(svc, acc, _ids(svc, acc.id, gone=0)[:1])
    try:
        wm.empty_folder(svc, acc, "INBOX")
    except wm.ValidationError:
        pass
    else:
        raise AssertionError("очищены «Входящие»")
    assert wm.empty_folder(svc, acc, "Trash", actor="adm")["deleted"] == 1
    assert world.boxes["ivanov@x.ru"]["Trash"].messages == {}
    wm.create_folder(svc, acc, "Архив 2025")
    res = wm.rename_folder(svc, acc, "Архив 2025", "Архив 2025", new_parent="Projects")
    assert res["folder"] == "Projects/Архив 2025" and "Projects/Архив 2025" in world.boxes["ivanov@x.ru"]


# ---------------------------------------------------------------------------
#  API и планировщик
# ---------------------------------------------------------------------------
def test_api_tools(api, smtp):
    admin, mb, svc, a, b = api
    ids = [m["id"] for m in mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()["messages"]]
    st = mb.get(f"/api/webmail/status?account_id={a}").json()
    assert st["undo_send_s"] == 5 and st["identity"]["from_choices"] == ["ivanov@x.ru"]
    tags = mb.get(f"/api/webmail/tags?account_id={a}").json()["items"]
    assert mb.post("/api/webmail/tag", json={"account_id": a, "ids": ids, "keyword": tags[0]["keyword"]}).status_code == 200
    msgs = mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&filter=tag:$label1").json()["messages"]
    assert len(msgs) == 2 and msgs[0]["keywords"] == ["$label1"]
    assert mb.get(f"/api/webmail/ids?account_id={a}&folder=INBOX").json() == {"ids": ids, "more": False}
    assert mb.get(f"/api/webmail/tags?account_id={b}").status_code == 403
    r = mb.post("/api/webmail/rules", json={"account_id": a, "name": "Счета", "conditions": [
        {"field": "subject", "op": "contains", "value": "счёт"}], "actions": [{"type": "flag"}]})
    assert r.status_code == 200
    rules = mb.get(f"/api/webmail/rules?account_id={a}").json()
    assert [x["name"] for x in rules["items"]] == ["Счета"] and rules["status"]["interval_min"] == 5
    assert mb.post("/api/webmail/rules/apply", json={"account_id": a, "folder": "INBOX"}).json()["flagged"] == 1
    assert mb.post("/api/webmail/block", json={"account_id": a, "address": "spam@ads.ru"}).json()["action"] == "junk"
    assert len(mb.get(f"/api/webmail/rules?account_id={a}").json()["blocked"]) == 1
    r = mb.put("/api/webmail/autoreply", json={"account_id": a, "enabled": True, "body": "<p>Нет на месте</p>"})
    assert r.status_code == 200 and r.json()["active"]
    t = mb.post("/api/webmail/templates", json={"name": "Ответ", "body": "<p>Спасибо</p>"}).json()
    assert admin.get("/api/webmail/templates").json()["items"] == []
    assert mb.delete(f"/api/webmail/templates/{t['id']}").status_code == 200
    c = mb.post("/api/webmail/book", json={"account_id": a, "name": "Анна", "email": "anna@x.ru", "groups": "Отдел"})
    assert c.status_code == 200
    up = mb.post("/api/webmail/book/import", data={"account_id": str(a)},
                 files={"file": ("c.csv", "email\r\nbob@x.ru\r\n".encode(), "text/csv")})
    assert up.json()["added"] == 1
    exp = mb.get(f"/api/webmail/book/export?account_id={a}&fmt=csv")
    assert exp.status_code == 200 and "bob@x.ru" in exp.content.decode("utf-8-sig")
    z = mb.post("/api/webmail/zip", json={"account_id": a, "ids": ids})
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert len(zipfile.ZipFile(io.BytesIO(z.content)).namelist()) == 2
    assert mb.post("/api/webmail/zip", json={"account_id": b, "ids": ids}).status_code == 403
    d = mb.post("/api/webmail/drafts", json={"account_id": a, "mode": "forward_attach", "ref_ids": ids}).json()
    assert len(d["attachments"]) == 2
    mb.put(f"/api/webmail/drafts/{d['id']}", json={"to_addrs": "boss@x.ru", "priority": True})
    r = mb.post(f"/api/webmail/drafts/{d['id']}/schedule", json={"send_at": _future(10)})
    assert r.status_code == 200 and r.json()["status"] == "scheduled"
    assert mb.post(f"/api/webmail/drafts/{d['id']}/unschedule").json()["status"] == "draft"
    d2 = mb.post("/api/webmail/drafts", json={"account_id": a, "to_addrs": "x@y.ru", "subject": "Т",
                                              "body": "<p>Из шаблона</p>"}).json()
    assert d2["body"].startswith("<p>Из шаблона</p>")
    r = mb.post("/api/webmail/snooze", json={"account_id": a, "ids": ids[:1], "until": _future(120)})
    assert r.status_code == 200 and r.json()["folder"] == "Отложенные"
    sn = mb.get(f"/api/webmail/snoozed?account_id={a}").json()["items"]
    assert mb.post("/api/webmail/unsnooze", json={"account_id": a, "ids": [sn[0]["id"]]}).json()["returned"] == 1
    assert mb.post("/api/webmail/folders/read_all", json={"account_id": a, "folder": "INBOX"}).status_code == 200
    assert mb.post("/api/webmail/folders/empty", json={"account_id": a, "folder": "INBOX"}).status_code == 400
    ex = mb.get(f"/api/webmail/extras?account_id={a}&id={ids[0]}").json()
    assert ex["sender"] == "client@ext.ru"
    # оператор — ничего; администратору с запретом писать — только чтение
    from fastapi.testclient import TestClient
    from mailarchiver.security import hash_password
    svc.db.create_user("op", hash_password("СложныйПароль1"), role="operator")
    op = TestClient(admin.app, headers={"X-Requested-With": "fetch"})
    assert op.post("/api/login", json={"username": "op", "password": "СложныйПароль1"}).status_code == 200
    for url in (f"/api/webmail/tags?account_id={a}", f"/api/webmail/rules?account_id={a}", "/api/webmail/templates",
                f"/api/webmail/book?account_id={a}", f"/api/webmail/extras?account_id={a}&id={ids[0]}"):
        assert op.get(url).status_code == 403, url
    svc.db.set_setting("webmail.admin_actions", False)
    assert admin.get(f"/api/webmail/rules?account_id={a}").status_code == 200
    assert admin.post("/api/webmail/rules", json={"account_id": a, "name": "x", "conditions": [
        {"field": "subject", "op": "contains", "value": "x"}], "actions": [{"type": "read"}]}).status_code == 403
    assert admin.post("/api/webmail/snooze", json={"account_id": a, "ids": ids[:1], "until": _future(5)}).status_code == 403


def test_scheduler_always_jobs():
    from mailarchiver.scheduler.scheduler import SchedulerService
    assert {"webmail", "reply_index"} <= set(SchedulerService.ALWAYS_JOBS)


def test_settings_new_keys():
    from mailarchiver.config import DEFAULTS
    from mailarchiver.web.i18n import PARAM_HELP
    for key in ("undo_send_s", "rules_interval_min"):
        assert key in DEFAULTS["webmail"] and f"webmail.{key}" in PARAM_HELP


def test_ui_has_client_tools():
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "mailarchiver/web/static"
    js = (root / "js/app.js").read_text("utf-8")
    for needle in ("function mailSettingsDialog", "function mailRuleEditor", "/webmail/snooze", "/webmail/zip",
                   "function mailUndoWait", "/schedule`", "/webmail/extras", "/webmail/invite", "/webmail/receipt",
                   "/webmail/book/import", "function composeMentionsAttachment", "function mailQuickReply",
                   "/webmail/folders/read_all", "/webmail/folders/empty", "function mailPreview", "data-allfolder",
                   "function mailNotifyNew", "forward_attach", "from_addr"):
        assert needle in js, needle
    css = (root / "css/app.css").read_text("utf-8")
    for needle in (".mtag", ".mail-invite", ".mail-quick", ".mail-layout.compact", ".compose[hidden]"):
        assert needle in css, needle
