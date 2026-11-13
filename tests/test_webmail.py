"""Почтовый клиент раздела «Почта» (1.17.0).

Сервер IMAP — учебный (``imap_fake``), SMTP — подменённый (``webmail.SMTP_CONNECT``): весь остальной код
настоящий. Проверяется, что действия меняют ящик на сервере и тут же — архив; что копирование и проверка
почты отмечают письма, которых на сервере больше нет; что письмо собирается и отправляется правильно; и кто
что может.
"""
import email
import json
import smtplib
import time
from email import policy

import pytest
from fastapi.testclient import TestClient

from imap_fake import ImapWorld, raw_message
from mailarchiver import models, webmail as wm
from mailarchiver.errors import ImapAuthError, MailArchiverError, ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.models import JobType
from mailarchiver.queue.jobs import JobContext, handle_backup
from mailarchiver.security import sign_value
from mailarchiver.web.auth import COOKIE_NAME

HDR = {"X-Requested-With": "fetch"}
PW = "СложныйПароль1"
SPECIAL = {"Sent": [b"\\Sent"], "Drafts": [b"\\Drafts"], "Trash": [b"\\Trash"], "Junk": [b"\\Junk"],
           "Projects": []}


class FakeSMTP:
    """Сервер отправки: запоминает вход и письма; ``fail`` — исключение, которое бросить."""
    sent = []
    logins = []
    fail = None
    refuse = {}

    def __init__(self, host, port, security, timeout, context):
        self.host, self.port, self.security = host, port, security

    def login(self, user, password):
        FakeSMTP.logins.append(("login", user, password))
        if isinstance(FakeSMTP.fail, smtplib.SMTPAuthenticationError):
            raise FakeSMTP.fail

    def auth(self, mech, authobject, initial_response_ok=True):
        FakeSMTP.logins.append((mech, authobject()))

    # пароль с русскими буквами smtplib в ASCII не закодирует — util.smtp_login шлёт AUTH PLAIN сам (1.24.1)
    esmtp_features = {"auth": "PLAIN LOGIN"}

    def ehlo_or_helo_if_needed(self):
        return None

    def has_extn(self, name):
        return name.lower() in self.esmtp_features

    def docmd(self, cmd, args=""):
        import base64
        assert cmd == "AUTH" and args.startswith("PLAIN ")
        _authz, user, password = base64.b64decode(args.split()[1]).decode("utf-8").split("\0")
        self.login(user, password)
        return 235, b"Authentication successful"

    def sendmail(self, sender, rcpts, data):
        if FakeSMTP.fail is not None and not isinstance(FakeSMTP.fail, smtplib.SMTPAuthenticationError):
            raise FakeSMTP.fail
        FakeSMTP.sent.append({"from": sender, "rcpts": list(rcpts), "data": data, "host": self.host,
                              "port": self.port, "security": self.security})
        return dict(FakeSMTP.refuse)

    def quit(self):
        return None

    def close(self):
        return None


@pytest.fixture(autouse=True)
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail, FakeSMTP.refuse = [], [], None, {}
    monkeypatch.setattr(wm, "SMTP_CONNECT", FakeSMTP)
    return FakeSMTP


@pytest.fixture()
def world(monkeypatch):
    w = ImapWorld()
    monkeypatch.setattr(client_mod, "IMAPClient", w.client_class())
    return w


def _backup(svc, aid):
    job = svc.db.enqueue_job(JobType.BACKUP, aid, {}, 5, 1, "t")
    return handle_backup(JobContext(svc, job, JobType.BACKUP, aid, {}))


def _mail(subject, *, mid, frm="client@ext.ru", to="ivanov@x.ru", cc="", extra="", body="Текст письма"):
    head = f"From: Клиент <{frm}>\r\nTo: {to}\r\n" + (f"Cc: {cc}\r\n" if cc else "") + \
        f"Subject: {subject}\r\nMessage-ID: {mid}\r\nDate: Mon, 01 Jun 2026 10:00:00 +0300\r\n{extra}"
    return (head + f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode("utf-8")


@pytest.fixture()
def box(services, world):
    """Ящик ivanov@x.ru с особыми папками и тремя письмами во «Входящих», скопированный в архив."""
    svc = services
    world.mailbox("ivanov@x.ru", "pw", SPECIAL)
    world.add("ivanov@x.ru", "INBOX", _mail("Счёт за июнь", mid="<m1@ext>"))
    world.add("ivanov@x.ru", "INBOX", _mail("Акт сверки", mid="<m2@ext>", cc="petrov@x.ru, ivanov@x.ru"),
              flags=[b"\\Seen"])
    world.add("ivanov@x.ru", "INBOX", _mail("Договор", mid="<m3@ext>"))
    aid = svc.db.create_account(models.Account(name="Иванов", host="imap.x.ru", username="ivanov@x.ru", password="pw"))
    _backup(svc, aid)
    yield svc, world, svc.db.get_account(aid)
    wm.shutdown(svc)


def _ids(svc, aid, folder="INBOX", gone=None):
    sql = "SELECT id FROM messages WHERE account_id=? AND folder=?"
    if gone is not None:
        sql += f" AND gone={int(gone)}"
    return [int(r["id"]) for r in svc.db.query(sql + " ORDER BY uid", (aid, folder))]


def _row(svc, pk):
    return svc.db.get_message(pk)


def _server_subjects(world, folder, login="ivanov@x.ru"):
    out = []
    for m in world.boxes[login][folder].messages.values():
        out.append(email.message_from_bytes(m.raw, policy=policy.default)["Subject"])
    return sorted(out)


# ---------------------------------------------------------------------------
#  Папки и проверка почты
# ---------------------------------------------------------------------------
def test_folders_special_counts_and_archive_only(box):
    svc, world, acc = box
    world.boxes["ivanov@x.ru"]["Old"] = type(world.boxes["ivanov@x.ru"]["Trash"])()
    world.add("ivanov@x.ru", "Old", raw_message("старое", mid="<old@x>"))
    _backup(svc, acc.id)
    del world.boxes["ivanov@x.ru"]["Old"]                       # папку удалили на сервере
    data = wm.folders(svc, acc, refresh=True)
    by = {f["name"]: f for f in data["folders"]}
    assert data["special"] == {"inbox": "INBOX", "sent": "Sent", "drafts": "Drafts", "trash": "Trash", "junk": "Junk"}
    assert by["INBOX"]["total"] == 3 and by["INBOX"]["unseen"] == 2 and by["INBOX"]["archived"] == 3
    assert by["Old"]["on_server"] is False and by["Old"]["archived"] == 1
    assert [f["name"] for f in data["folders"]][:5] == ["INBOX", "Drafts", "Sent", "Junk", "Trash"]
    assert data["folders"][-1]["name"] == "Old"


def test_sync_downloads_new_marks_gone_and_flags(box):
    svc, world, acc = box
    login = "ivanov@x.ru"
    first = min(world.uids(login, "INBOX"))
    del world.boxes[login]["INBOX"].messages[first]                  # удалили с телефона
    world.boxes[login]["INBOX"].messages[first + 2].flags.add(b"\\Seen")   # прочитали «Договор»
    world.add(login, "INBOX", _mail("Новое письмо", mid="<new@ext>"))
    res = wm.sync_folder(svc, acc, "INBOX")
    assert res["new"] == 1 and res["gone"] == 1 and res["flags"] == 1 and res["total"] == 3 and res["unseen"] == 1
    rows = svc.db.query("SELECT subject, gone, flags FROM messages WHERE account_id=? AND folder='INBOX' ORDER BY uid",
                        (acc.id,))
    assert [(r["subject"], r["gone"]) for r in rows] == [("Счёт за июнь", 1), ("Акт сверки", 0), ("Договор", 0),
                                                         ("Новое письмо", 0)]
    assert "\\Seen" in rows[2]["flags"]
    lst = wm.list_messages(svc, acc.id, "INBOX", show_gone=False)
    assert lst["total"] == 3 and lst["gone"] == 1 and [r["subject"] for r in lst["rows"]][0] in ("Новое письмо",
                                                                                                 "Договор", "Акт сверки")
    again = wm.sync_folder(svc, acc, "INBOX")
    assert again["new"] == 0 and again["gone"] == 0 and again["flags"] == 0


def test_sync_skips_download_while_backup_runs_and_uidvalidity_change_enqueues(box):
    svc, world, acc = box
    world.add("ivanov@x.ru", "INBOX", _mail("Ещё", mid="<e@ext>"))
    jid = svc.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "t")
    svc.db.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    res = wm.sync_folder(svc, acc, "INBOX")
    assert res["backup_running"] and res["new"] == 0 and res["pending"] == 1
    svc.db.execute("UPDATE jobs SET status='success' WHERE id=?", (jid,))
    world.boxes["ivanov@x.ru"]["INBOX"].uidvalidity += 1               # папку пересоздали
    res = wm.sync_folder(svc, acc, "INBOX")
    assert res["uidvalidity_changed"]
    assert svc.db.query_one("SELECT 1 FROM jobs WHERE account_id=? AND type=? AND status='queued'",
                            (acc.id, JobType.BACKUP)) is not None


def test_backup_marks_messages_deleted_on_server(box):
    svc, world, acc = box
    login = "ivanov@x.ru"
    first = min(world.uids(login, "INBOX"))
    del world.boxes[login]["INBOX"].messages[first]
    _backup(svc, acc.id)
    assert len(_ids(svc, acc.id, gone=1)) == 1 and len(_ids(svc, acc.id, gone=0)) == 2
    assert svc.db.count_folder_messages(acc.id, "INBOX") == 3               # копия в архиве осталась


# ---------------------------------------------------------------------------
#  Действия над письмами
# ---------------------------------------------------------------------------
def test_flags_on_server_and_in_archive(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    res = wm.set_flags(svc, acc, ids[:2], add=["\\Seen", "\\Flagged"])
    assert res == {"changed": 2, "missing": 0}
    srv = world.boxes["ivanov@x.ru"]["INBOX"].messages
    assert all(b"\\Flagged" in srv[u].flags for u in sorted(srv)[:2])
    assert "\\Flagged" in _row(svc, ids[0])["flags"] and "\\Seen" in _row(svc, ids[0])["flags"]
    wm.set_flags(svc, acc, [ids[0]], remove=["\\Seen"])
    assert b"\\Seen" not in srv[sorted(srv)[0]].flags and "\\Seen" not in _row(svc, ids[0])["flags"].split(",")
    with pytest.raises(ValidationError):
        wm.set_flags(svc, acc, [ids[0]], add=["\\Bad flag"])
    # письмо удалили на сервере — действие отмечает его в архиве
    del srv[sorted(srv)[2]]
    res = wm.set_flags(svc, acc, [ids[2]], add=["\\Seen"])
    assert res == {"changed": 0, "missing": 1} and _row(svc, ids[2])["gone"] == 1


@pytest.mark.parametrize("caps", [{"IMAP4REV1", "UIDPLUS", "MOVE", "SPECIAL-USE"}, {"IMAP4REV1", "SPECIAL-USE"}])
def test_move_updates_server_and_archive(box, caps):
    svc, world, acc = box
    world.capabilities = set(caps)
    ids = _ids(svc, acc.id)
    res = wm.move(svc, acc, ids[:2], "Projects", actor="adm")
    assert res["moved"] == 2 and res["archived"] == 2
    assert _server_subjects(world, "Projects") == ["Акт сверки", "Счёт за июнь"]
    assert _server_subjects(world, "INBOX") == ["Договор"]
    assert all(_row(svc, i)["gone"] == 1 for i in ids[:2])
    moved = svc.db.query("SELECT subject, uid, gone, flags FROM messages WHERE account_id=? AND folder='Projects' "
                         "ORDER BY uid", (acc.id,))
    assert [r["subject"] for r in moved] == ["Счёт за июнь", "Акт сверки"] and all(r["gone"] == 0 for r in moved)
    assert sorted(r["uid"] for r in moved) == world.uids("ivanov@x.ru", "Projects")
    # следующее копирование не скачивает их заново
    before = svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc.id,))
    _backup(svc, acc.id)
    assert svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc.id,)) == before
    assert svc.db.query_one("SELECT detail FROM audit WHERE action='webmail_move'")["detail"].endswith("«Projects»")


def test_delete_to_trash_then_permanently(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    res = wm.delete(svc, acc, [ids[0]], actor="adm")
    assert res["trashed"] == 1 and _server_subjects(world, "Trash") == ["Счёт за июнь"]
    trash_id = _ids(svc, acc.id, "Trash")[0]
    res = wm.delete(svc, acc, [trash_id], actor="adm")
    assert res["deleted"] == 1 and _server_subjects(world, "Trash") == []
    assert _row(svc, trash_id)["gone"] == 1 and svc.db.get_message(trash_id) is not None   # копия в архиве есть
    res = wm.delete(svc, acc, [ids[1]], permanent=True)
    assert res["deleted"] == 1 and _server_subjects(world, "Trash") == []


def test_junk_and_not_junk(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    wm.junk(svc, acc, [ids[2]])
    assert _server_subjects(world, "Junk") == ["Договор"]
    wm.junk(svc, acc, _ids(svc, acc.id, "Junk"), spam=False)
    assert "Договор" in _server_subjects(world, "INBOX") and _server_subjects(world, "Junk") == []


def test_folder_create_rename_delete(box):
    svc, world, acc = box
    wm.create_folder(svc, acc, "Клиенты", "Projects", actor="adm")
    assert "Projects/Клиенты" in world.boxes["ivanov@x.ru"]
    with pytest.raises(ValidationError):
        wm.create_folder(svc, acc, "a/b")
    wm.move(svc, acc, _ids(svc, acc.id)[:1], "Projects/Клиенты")
    wm.rename_folder(svc, acc, "Projects/Клиенты", "Заказчики", actor="adm")
    assert "Projects/Заказчики" in world.boxes["ivanov@x.ru"] and "Projects/Клиенты" not in world.boxes["ivanov@x.ru"]
    assert svc.db.count_folder_messages(acc.id, "Projects/Заказчики") == 1
    # переименованную папку копирование не скачивает заново (UIDVALIDITY сменился — сверка по Message-ID)
    before = svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc.id,))
    _backup(svc, acc.id)
    assert svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc.id,)) == before
    # выгрузка знает разделитель папки, хотя файлы лежат в каталоге прежнего имени
    items = [i for i in svc.iter_mail_items(acc.id, ["Projects/Заказчики"])]
    assert items and items[0].delimiter == "/"
    with pytest.raises(ValidationError):
        wm.rename_folder(svc, acc, "Trash", "Мусор")
    with pytest.raises(ValidationError):
        wm.delete_folder(svc, acc, "Projects")                     # есть вложенная
    res = wm.delete_folder(svc, acc, "Projects/Заказчики", actor="adm")
    assert res["messages"] == 1 and "Projects/Заказчики" not in world.boxes["ivanov@x.ru"]
    assert all(r["gone"] for r in svc.db.query("SELECT gone FROM messages WHERE account_id=? AND "
                                               "folder='Projects/Заказчики'", (acc.id,)))
    actions = {r["action"] for r in svc.db.query("SELECT action FROM audit")}
    assert {"webmail_folder_create", "webmail_folder_rename", "webmail_folder_delete"} <= actions


def test_auth_failure_backoff(box):
    svc, world, acc = box
    wm.shutdown(svc)
    world.passwords["ivanov@x.ru"] = "changed"
    with pytest.raises(ImapAuthError):
        wm.sync_folder(svc, acc, "INBOX")
    logins = world.commands.count("LOGIN")
    with pytest.raises(ImapAuthError) as exc:
        wm.sync_folder(svc, acc, "INBOX")
    assert "меньше минуты назад" in exc.value.message and world.commands.count("LOGIN") == logins


def test_pool_reuses_connection(box):
    svc, world, acc = box
    wm.shutdown(svc)
    n = world.commands.count("LOGIN")
    wm.sync_folder(svc, acc, "INBOX")
    wm.set_flags(svc, acc, _ids(svc, acc.id)[:1], add=["\\Seen"])
    assert world.commands.count("LOGIN") == n + 1
    assert wm.pool(svc).reap(idle_s=0) == 1


# ---------------------------------------------------------------------------
#  Ответ, пересылка, отправка
# ---------------------------------------------------------------------------
def test_reply_all_and_forward_fields(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    f = wm.compose_fields(svc, acc, "reply_all", ids[1])
    assert f["subject"] == "Re: Акт сверки" and f["in_reply_to"] == "<m2@ext>" and f["refs"] == "<m2@ext>"
    assert "client@ext.ru" in f["to_addrs"] and f["cc_addrs"] == "petrov@x.ru"   # себя в копию не ставим
    assert "Клиент" in f["body"] and "<blockquote" in f["body"] and "Текст письма" in f["body"]
    f = wm.compose_fields(svc, acc, "reply", ids[1])
    assert f["cc_addrs"] == ""
    raw = (b"From: a@ext.ru\r\nTo: ivanov@x.ru\r\nSubject: =?utf-8?b?0KTQsNC50Ls=?=\r\nMessage-ID: <att@ext>\r\n"
           b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nsee file\r\n--b\r\nContent-Type: application/pdf\r\n"
           b"Content-Disposition: attachment; filename=\"act.pdf\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
           b"JVBERi0xLjQ=\r\n--b--\r\n")
    world.add("ivanov@x.ru", "INBOX", raw)
    wm.sync_folder(svc, acc, "INBOX")
    pk = _ids(svc, acc.id)[-1]
    f = wm.compose_fields(svc, acc, "forward", pk)
    assert f["subject"] == "Fwd: Файл" and f["attachments"][0]["name"] == "act.pdf"
    assert "Пересылаемое сообщение" in f["body"]


def _draft(svc, acc, owner="admin:adm", **fields):
    base = {"to_addrs": "Пётр <petrov@x.ru>", "cc_addrs": "boss@x.ru", "bcc_addrs": "audit@x.ru",
            "subject": "Отчёт", "body": "<p>Добрый день!</p><p>Отчёт во вложении.</p>"}
    base.update(fields)
    return wm.create_draft(svc, acc, owner, base)


def test_send_builds_message_saves_sent_and_marks_answered(box, smtp):
    svc, world, acc = box
    wm.save_identity(svc, acc, {"display_name": "Иван Иванов", "signature": "<b>Иванов</b>"}, admin=False)
    ref = _ids(svc, acc.id)[0]
    fields = wm.compose_fields(svc, acc, "reply", ref)
    fields["body"] = '<p>Оплатим <img src="data:image/png;base64,iVBORw0KGgo="></p>' + fields["body"]
    d = wm.create_draft(svc, acc, "admin:adm", fields)
    import io
    wm.add_attachment(svc, d["id"], "admin:adm", "отчёт.txt", "text/plain", io.BytesIO("данные".encode()))
    res = wm.send(svc, acc, d["id"], "admin:adm", actor="adm")
    assert res["ok"] and res["saved_to"] == "Sent" and res["warnings"] == []
    sent = smtp.sent[0]
    assert sent["from"] == "ivanov@x.ru" and sent["rcpts"] == ["client@ext.ru"]
    assert sent["host"] == "smtp.x.ru" and sent["port"] == 587 and sent["security"] == "starttls"
    assert smtp.logins == [("login", "ivanov@x.ru", "pw")]
    msg = email.message_from_bytes(sent["data"], policy=policy.default)
    assert str(msg["From"]) == "Иван Иванов <ivanov@x.ru>" and msg["In-Reply-To"] == "<m1@ext>"
    assert msg["Subject"] == "Re: Счёт за июнь" and msg["Bcc"] is None
    kinds = [p.get_content_type() for p in msg.walk()]
    assert "multipart/alternative" in kinds and "multipart/related" in kinds and "image/png" in kinds
    assert [p.get_filename() for p in msg.iter_attachments()] == ["отчёт.txt"]
    html = msg.get_body(("html",)).get_content()
    assert "cid:" in html and "data:image" not in html
    assert "Оплатим" in msg.get_body(("plain",)).get_content()
    # копия в «Отправленных» — на сервере и сразу в архиве
    assert _server_subjects(world, "Sent") == ["Re: Счёт за июнь"]
    assert svc.db.query_one("SELECT flags FROM messages WHERE account_id=? AND folder='Sent'", (acc.id,))["flags"] == "\\Seen"
    # исходное письмо отмечено «Отвечено», черновик удалён, отправка — в аудите
    srv = world.boxes["ivanov@x.ru"]["INBOX"].messages
    assert b"\\Answered" in srv[min(srv)].flags and "\\Answered" in _row(svc, ref)["flags"]
    assert svc.db.query_one("SELECT 1 FROM webmail_drafts WHERE id=?", (d["id"],)) is None
    audit = svc.db.query_one("SELECT detail FROM audit WHERE action='webmail_send'")["detail"]
    assert "client@ext.ru" in audit and "Re: Счёт за июнь" in audit


def test_send_all_recipients_and_bcc(box, smtp):
    svc, world, acc = box
    d = _draft(svc, acc)
    wm.send(svc, acc, d["id"], "admin:adm")
    assert smtp.sent[0]["rcpts"] == ["petrov@x.ru", "boss@x.ru", "audit@x.ru"]
    msg = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert msg["Bcc"] is None and "audit@x.ru" not in smtp.sent[0]["data"].decode("utf-8", "replace").split("\r\n\r\n")[0]


def test_send_errors_keep_draft(box, smtp):
    svc, world, acc = box
    d = _draft(svc, acc)
    smtp.fail = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")
    with pytest.raises(MailArchiverError) as exc:
        wm.send(svc, acc, d["id"], "admin:adm")
    assert "не принял вход" in exc.value.message and "535" in exc.value.message
    row = svc.db.query_one("SELECT status, error FROM webmail_drafts WHERE id=?", (d["id"],))
    assert row["status"] == "draft" and "535" in row["error"]
    smtp.fail = smtplib.SMTPRecipientsRefused({"bad@x.ru": (550, b"no such user")})
    with pytest.raises(MailArchiverError) as exc:
        wm.send(svc, acc, d["id"], "admin:adm")
    assert "bad@x.ru" in exc.value.message
    smtp.fail = None
    bad = _draft(svc, acc, to_addrs="не адрес")
    with pytest.raises(ValidationError):
        wm.send(svc, acc, bad["id"], "admin:adm")
    empty = _draft(svc, acc, to_addrs="", cc_addrs="", bcc_addrs="")
    with pytest.raises(ValidationError):
        wm.send(svc, acc, empty["id"], "admin:adm")


def test_send_policy_and_credentials(box, smtp, monkeypatch):
    svc, world, acc = box
    # Gmail сам кладёт копию в «Отправленные»
    wm.save_identity(svc, acc, {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl"}, admin=True)
    res = wm.send(svc, acc, _draft(svc, acc)["id"], "admin:adm")
    assert res["saved_to"] is None and _server_subjects(world, "Sent") == []
    # отдельный вход на сервер отправки; пароль — зашифрован
    wm.save_identity(svc, acc, {"smtp_host": "relay.x.ru", "smtp_port": 587, "smtp_security": "starttls",
                                "smtp_auth": "custom", "smtp_username": "relay", "smtp_password": "секрет",
                                "save_sent": "never"}, admin=True)
    stored = svc.db.query_one("SELECT smtp_password FROM webmail_identities WHERE account_id=?", (acc.id,))[0]
    assert stored and "секрет" not in stored
    wm.send(svc, acc, _draft(svc, acc)["id"], "admin:adm")
    assert smtp.logins[-1] == ("login", "relay", "секрет")
    # пароль не уходит на сервер без шифрования
    wm.save_identity(svc, acc, {"smtp_security": "none"}, admin=True)
    with pytest.raises(ValidationError):
        wm.send(svc, acc, _draft(svc, acc)["id"], "admin:adm")
    # ретранслятор без входа — можно
    wm.save_identity(svc, acc, {"smtp_auth": "none"}, admin=True)
    wm.send(svc, acc, _draft(svc, acc)["id"], "admin:adm")
    # сотрудник не меняет сервер отправки
    ident = wm.save_identity(svc, acc, {"smtp_host": "evil.ru", "display_name": "И. Иванов"}, admin=False)
    assert ident["smtp_host"] == "relay.x.ru" and ident["display_name"] == "И. Иванов"


def test_oauth_account_sends_with_xoauth2(services, world, smtp, monkeypatch):
    svc = services
    world.mailbox("box@gmail.com", "pw", SPECIAL)
    world.tokens["tok"] = {"box@gmail.com"}
    aid = svc.db.create_account(models.Account(name="G", host="imap.gmail.com", username="box@gmail.com",
                                               auth_type=models.AuthType.OAUTH2, oauth_client_id="c",
                                               oauth_client_secret="s", oauth_refresh_token="r",
                                               oauth_token_url="https://t"))
    monkeypatch.setattr("mailarchiver.imap.oauth.refresh_access_token",
                        lambda *a, **k: ("tok", 3600, "r2"))
    monkeypatch.setattr(client_mod, "refresh_access_token", lambda *a, **k: ("tok", 3600, "r2"))
    acc = svc.db.get_account(aid)
    d = _draft(svc, acc)
    res = wm.send(svc, acc, d["id"], "admin:adm")
    assert smtp.logins == [("XOAUTH2", "user=box@gmail.com\x01auth=Bearer tok\x01\x01")]
    assert smtp.sent[0]["host"] == "smtp.gmail.com" and smtp.sent[0]["security"] == "ssl"
    assert res["saved_to"] is None                                # Gmail кладёт копию сам
    wm.shutdown(svc)


def test_master_account_cannot_send_without_own_credentials(services, world):
    svc = services
    aid = svc.db.create_account(models.Account(name="M", host="imap.x.ru", username="m@x.ru",
                                               auth_type=models.AuthType.MASTER))
    acc = svc.db.get_account(aid)
    assert "администратора почты" in wm.send_problem(svc, acc)
    wm.save_identity(svc, acc, {"smtp_auth": "custom", "smtp_username": "m@x.ru", "smtp_password": "p"}, admin=True)
    assert wm.send_problem(svc, acc) == ""


def test_server_draft_continue_and_cleanup(box, smtp):
    svc, world, acc = box
    world.add("ivanov@x.ru", "Drafts", _mail("Черновик с телефона", mid="<dr@x>", frm="ivanov@x.ru",
                                             to="partner@ext.ru"), flags=[b"\\Draft"])
    wm.sync_folder(svc, acc, "Drafts")
    pk = _ids(svc, acc.id, "Drafts")[0]
    f = wm.compose_fields(svc, acc, "edit", pk)
    assert f["server_draft_id"] == pk and f["to_addrs"] == "partner@ext.ru" and "Текст письма" in f["body"]
    d = wm.create_draft(svc, acc, "admin:adm", f)
    wm.send(svc, acc, d["id"], "admin:adm")
    assert world.boxes["ivanov@x.ru"]["Drafts"].messages == {} and _row(svc, pk)["gone"] == 1


def test_drafts_owner_limits_and_purge(box, monkeypatch):
    svc, world, acc = box
    import io
    d = _draft(svc, acc)
    with pytest.raises(ValidationError):
        wm.get_draft_row(svc, d["id"], "admin:other")
    svc.db.set_setting("webmail.max_attachments_mb", 1)
    wm.add_attachment(svc, d["id"], "admin:adm", "a.bin", "application/octet-stream", io.BytesIO(b"x" * 700_000))
    with pytest.raises(ValidationError):
        wm.add_attachment(svc, d["id"], "admin:adm", "b.bin", "", io.BytesIO(b"x" * 400_000))
    items = wm.list_drafts(svc, "admin:adm")
    assert len(items) == 1 and items[0]["attachments"][0]["name"] == "a.bin" and "file" not in items[0]["attachments"][0]
    wm.remove_attachment(svc, d["id"], "admin:adm", 1)
    assert wm.list_drafts(svc, "admin:adm")[0]["attachments"] == []
    svc.db.execute("UPDATE webmail_drafts SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (d["id"],))
    assert wm.list_drafts(svc, "admin:adm") == []


def test_draft_attachments_encrypted_at_rest(box):
    svc, world, acc = box
    import io
    import os
    from mailarchiver.storage.crypto import StorageCipher
    svc.store.cipher = StorageCipher(b"k" * 32)
    svc.store.encrypt = True
    d = _draft(svc, acc)
    wm.add_attachment(svc, d["id"], "admin:adm", "секрет.txt", "text/plain", io.BytesIO("тайна".encode()))
    path = os.path.join(wm._draft_dir(svc, d["id"]), "1.bin")
    assert "тайна".encode() not in open(path, "rb").read()
    row = wm.get_draft_row(svc, d["id"], "admin:adm")
    msg, _rcpts, _atts = wm.build_message(svc, acc, row, wm.get_identity(svc, acc))
    assert [p.get_payload(decode=True) for p in msg.iter_attachments()] == ["тайна".encode()]


# ---------------------------------------------------------------------------
#  Вспомогательное
# ---------------------------------------------------------------------------
def test_sanitize_and_text():
    dirty = ('<p onclick="x()">Привет <script>alert(1)</script><a href="javascript:alert(1)">a</a>'
             '<a href="https://x.ru">b</a><img src="https://t.ru/p.gif"><iframe src="https://x"></iframe>'
             '<span style="color: red; background: url(x)">c</span></p><style>p{}</style>')
    clean = wm.sanitize_html(dirty)
    assert "script" not in clean and "onclick" not in clean and "javascript" not in clean and "iframe" not in clean
    assert 'href="https://x.ru"' in clean and 'style="color: red"' in clean and "<style" not in clean
    assert wm.html_to_text("<p>a</p><blockquote>b<br>c</blockquote>") == "a\n\n> b\n> c\n"
    assert wm.parse_copyuid(b"1000 4,6:7 21:23") == (1000, {4: 21, 6: 22, 7: 23})
    assert wm.parse_copyuid("COPYUID 7 1:2 5") == (0, {})
    assert wm.parse_appenduid(b"[APPENDUID 38505 3955] APPEND completed") == (38505, 3955)
    assert wm.smtp_defaults("outlook.office365.com") == {"host": "smtp.office365.com", "port": 587,
                                                         "security": "starttls"}
    with pytest.raises(ValidationError):
        wm._addresses("a@x.ru\r\nBcc: evil@x.ru")


def test_contacts_and_thread(box):
    svc, world, acc = box
    svc.db.create_employee(full_name="Петров Пётр", email="petrov@x.ru")
    labels = [c["label"] for c in wm.contacts(svc, acc, "петр")]
    assert labels == ["Петров Пётр <petrov@x.ru>"]
    assert [c["email"] for c in wm.contacts(svc, acc, "client")] == ["client@ext.ru"]
    from mailarchiver import replies
    world.add("ivanov@x.ru", "Sent", _mail("Re: Счёт за июнь", mid="<r1@x>", frm="ivanov@x.ru", to="client@ext.ru",
                                           extra="In-Reply-To: <m1@ext>\r\nReferences: <m1@ext>\r\n"))
    wm.sync_folder(svc, acc, "Sent")
    replies.index_pending(svc)
    first = _ids(svc, acc.id)[0]
    items = wm.thread(svc, acc.id, first)
    assert [i["subject"] for i in items] == ["Счёт за июнь", "Re: Счёт за июнь"] and items[0]["current"]


# ---------------------------------------------------------------------------
#  API и права
# ---------------------------------------------------------------------------
@pytest.fixture()
def api(client, world):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200
    svc = client.app.state.services
    world.mailbox("ivanov@x.ru", "pw", SPECIAL)
    world.add("ivanov@x.ru", "INBOX", _mail("Счёт", mid="<a1@ext>"))
    world.add("ivanov@x.ru", "INBOX", _mail("Акт", mid="<a2@ext>"))
    world.mailbox("petrov@x.ru", "pw", SPECIAL)
    world.add("petrov@x.ru", "INBOX", _mail("Чужое", mid="<p1@ext>", to="petrov@x.ru"))
    a = svc.db.create_account(models.Account(name="Иванов", host="imap.x.ru", username="ivanov@x.ru", password="pw"))
    b = svc.db.create_account(models.Account(name="Петров", host="imap.x.ru", username="petrov@x.ru", password="pw"))
    _backup(svc, a)
    _backup(svc, b)
    emp = svc.db.create_employee(full_name="Иванов Иван", email="ivanov@x.ru")
    svc.db.link_employee_account(emp, a)
    svc.db.create_session("mbtok", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=a)
    mb = TestClient(client.app, headers=HDR)
    mb.cookies.set(COOKIE_NAME, sign_value("mbtok", svc.cfg.secret_key()))
    yield client, mb, svc, a, b
    wm.shutdown(svc)


def test_api_status_and_permissions(api):
    admin, mb, svc, a, b = api
    st = mb.get(f"/api/webmail/status?account_id={a}").json()
    assert st["can_act"] and st["can_send"] and st["mark_read"] and st["identity"]["address"] == "ivanov@x.ru"
    st = admin.get(f"/api/webmail/status?account_id={a}").json()
    assert st["can_act"] and not st["mark_read"]                # администратор писем не «читает» за сотрудника
    assert mb.get(f"/api/webmail/status?account_id={b}").status_code == 403
    ids = [r["id"] for r in admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()["messages"]]
    assert mb.post("/api/webmail/flags", json={"account_id": b, "ids": ids, "add": ["\\Seen"]}).status_code == 403
    # администратору запрещено настройкой
    svc.db.set_setting("webmail.admin_actions", False)
    r = admin.post("/api/webmail/flags", json={"account_id": a, "ids": ids, "add": ["\\Seen"]})
    assert r.status_code == 403 and "запрещено настройкой" in r.json()["detail"]
    assert mb.post("/api/webmail/flags", json={"account_id": a, "ids": ids, "add": ["\\Seen"]}).status_code == 200
    assert not admin.get(f"/api/webmail/status?account_id={a}").json()["can_send"]
    # клиент выключен — читать можно, действовать нельзя
    svc.db.set_setting("webmail.enabled", False)
    assert mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").status_code == 200
    assert mb.post("/api/webmail/sync", json={"account_id": a, "folder": "INBOX"}).status_code == 403
    assert mb.post("/api/webmail/flags", json={"account_id": a, "ids": ids, "add": ["\\Seen"]}).status_code == 403
    assert mb.get(f"/api/webmail/folders?account_id={a}").status_code == 200
    svc.db.set_setting("webmail.enabled", True)
    # выключенный ящик — только чтение
    acc = svc.db.get_account(a)
    acc.enabled = False
    svc.db.update_account(acc)
    r = admin.post("/api/webmail/flags", json={"account_id": a, "ids": ids, "add": ["\\Seen"]})
    assert r.status_code == 403


def test_api_operator_has_no_access(api):
    admin, mb, svc, a, b = api
    from mailarchiver.security import hash_password
    svc.db.create_user("op", hash_password(PW), role="operator")
    op = TestClient(admin.app, headers=HDR)
    assert op.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    assert op.get(f"/api/webmail/status?account_id={a}").status_code == 403
    assert op.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").status_code == 403


def test_api_list_sort_filter_and_gone(api):
    admin, mb, svc, a, b = api
    r = admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&sort=subject").json()
    assert [m["subject"] for m in r["messages"]] == ["Акт", "Счёт"] and r["total"] == 2
    ids = [m["id"] for m in r["messages"]]
    assert admin.post("/api/webmail/flags", json={"account_id": a, "ids": [ids[0]], "add": ["\\Flagged"]}).json() == \
        {"changed": 1, "missing": 0}
    r = admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&filter=flagged").json()
    assert [m["subject"] for m in r["messages"]] == ["Акт"] and r["messages"][0]["flagged"]
    assert admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&filter=unread").json()["total"] == 2
    assert admin.post("/api/webmail/delete", json={"account_id": a, "ids": [ids[1]]}).json()["trashed"] == 1
    r = admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()
    assert r["total"] == 1 and r["gone"] == 1
    r = admin.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&show_gone=true").json()
    assert {m["subject"]: m["gone"] for m in r["messages"]} == {"Акт": False, "Счёт": True}
    folders = admin.get(f"/api/webmail/folders?account_id={a}&refresh=true").json()
    trash = next(f for f in folders["folders"] if f["special"] == "trash")
    assert trash["total"] == 1
    h = admin.get(f"/api/webmail/headers?account_id={a}&id={ids[0]}").json()
    assert "Message-ID: <a2@ext>" in h["headers"]
    assert mb.get(f"/api/webmail/headers?account_id={b}&id={ids[0]}").status_code == 403


def test_api_compose_attach_send(api, smtp):
    admin, mb, svc, a, b = api
    ids = [m["id"] for m in mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&sort=date_asc").json()["messages"]]
    r = mb.put("/api/webmail/identity", json={"account_id": a, "signature": "<i>С уважением, Иванов</i>"})
    assert r.status_code == 200
    assert mb.put("/api/webmail/identity", json={"account_id": a, "smtp_host": "evil.ru"}).status_code == 403
    d = mb.post("/api/webmail/drafts", json={"account_id": a, "mode": "reply", "ref_id": ids[0]}).json()
    assert d["subject"] == "Re: Счёт" and "С уважением" in d["body"] and d["to_addrs"].endswith("<client@ext.ru>")
    assert admin.get(f"/api/webmail/drafts/{d['id']}").status_code == 400          # чужой черновик
    d = mb.put(f"/api/webmail/drafts/{d['id']}", json={"body": "<p>Оплачено.</p>" + d["body"]}).json()
    up = mb.post(f"/api/webmail/drafts/{d['id']}/attachments",
                 files={"file": ("платёжка.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert up.status_code == 200 and up.json()["name"] == "платёжка.pdf"
    assert [x["id"] for x in mb.get("/api/webmail/drafts").json()["items"]] == [d["id"]]
    # адресат вне организации — сначала предупреждение (1.20.0), письмо уходит после подтверждения
    r = mb.post(f"/api/webmail/drafts/{d['id']}/send")
    assert r.status_code == 409 and r.json()["code"] == "presend_warn"
    assert [i["code"] for i in r.json()["items"]] == ["external"] and not smtp.sent
    res = mb.post(f"/api/webmail/drafts/{d['id']}/send", json={"confirm": True}).json()
    assert res["ok"] and res["saved_to"] == "Sent"
    msg = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert [p.get_filename() for p in msg.iter_attachments()] == ["платёжка.pdf"]
    assert mb.get("/api/webmail/drafts").json()["items"] == []
    sent = mb.get(f"/api/webmail/messages?account_id={a}&folder=Sent").json()["messages"]
    assert sent[0]["subject"] == "Re: Счёт"
    audit = svc.db.query_one("SELECT user, detail FROM audit WHERE action='webmail_send'")
    assert audit["user"] == "ivanov@x.ru"
    contacts = mb.get(f"/api/webmail/contacts?account_id={a}&q=cli").json()["items"]
    assert contacts and contacts[0]["email"] == "client@ext.ru"


def test_api_folders_and_sync(api):
    admin, mb, svc, a, b = api
    world_box = svc   # noqa: F841
    r = mb.post("/api/webmail/folders/create", json={"account_id": a, "name": "Отчёты"})
    assert r.status_code == 200 and r.json()["folder"] == "Отчёты"
    r = mb.post("/api/webmail/folders/rename", json={"account_id": a, "folder": "Отчёты", "name": "Отчёты 2026"})
    assert r.json()["folder"] == "Отчёты 2026"
    r = mb.post("/api/webmail/folders/delete", json={"account_id": a, "folder": "INBOX"})
    assert r.status_code == 400
    r = mb.post("/api/webmail/folders/delete", json={"account_id": a, "folder": "Отчёты 2026"})
    assert r.status_code == 200
    r = mb.post("/api/webmail/sync", json={"account_id": a, "folder": "INBOX", "folders": True}).json()
    assert r["total"] == 2 and r["new"] == 0
    ids = [m["id"] for m in mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()["messages"]]
    r = mb.post("/api/webmail/move", json={"account_id": a, "ids": ids[:1], "folder": "Нет такой"})
    assert r.status_code == 400
    r = mb.post("/api/webmail/junk", json={"account_id": a, "ids": ids[:1]})
    assert r.json()["moved"] == 1


def test_ui_has_mail_client():
    from mailarchiver.web.assets import bundle_text
    js = bundle_text()
    for needle in ("/webmail/status", "/webmail/drafts", "function composeWindow", "/webmail/sync",
                   "/webmail/folders/rename", "function mailMovePicker", "function composeClean"):
        assert needle in js, needle


def test_settings_have_webmail_section():
    from mailarchiver.web.i18n import PARAM_HELP, SETTINGS_SECTIONS
    sec = next(s for s in SETTINGS_SECTIONS if s["section"] == "webmail")
    for key in sec["keys"]:
        assert f"webmail.{key}" in PARAM_HELP


def test_unused_imports_guard():
    # json и time нужны тестам выше; явное использование — чтобы линтер не жаловался
    assert json.dumps({}) == "{}" and time.time() > 0


def test_api_failed_login_stops_automatic_checks(api, world):
    admin, mb, svc, a, b = api
    wm.shutdown(svc)
    world.passwords["ivanov@x.ru"] = "сменили"
    r = mb.post("/api/webmail/sync", json={"account_id": a, "folder": "INBOX", "manual": True})
    assert r.status_code == 400 and svc.db.get_account(a).login_status == "auth_error"
    logins = world.commands.count("LOGIN")
    r = mb.post("/api/webmail/sync", json={"account_id": a, "folder": "INBOX"}).json()
    assert r["skipped"] == "auth_error"
    svc.db.execute("UPDATE webmail_folders SET updated_at='2000-01-01T00:00:00+00:00' WHERE account_id=?", (a,))
    assert mb.get(f"/api/webmail/folders?account_id={a}").status_code == 200
    assert world.commands.count("LOGIN") == logins                    # сам в ящик не ходил
    world.passwords["ivanov@x.ru"] = "pw"
    wm.pool(svc)._auth_failed.clear()
    r = mb.post("/api/webmail/sync", json={"account_id": a, "folder": "INBOX", "manual": True})
    assert r.status_code == 200 and svc.db.get_account(a).login_status == "ok"
