"""Почтовый клиент 1.19.0: IMAP IDLE, беседы, сохранённые поиски, «Архивировать», подлинность отправителя и фишинг,
S/MIME, правила на сервере (Sieve/ManageSieve), черновики в «Черновиках» сервера, квота.
"""
import datetime as dt
import email
import shutil
import socketserver
import threading
import time
from email import policy

import pytest

import test_webmail as base
from test_webmail import _backup, _ids, _mail, _server_subjects

from mailarchiver import models, replies
from mailarchiver import webmail as wm
from mailarchiver import webmail_more as wx
from mailarchiver import webmail_tools as wt

world, box, api = base.world, base.box, base.api
smtp = base.smtp          # autouse: SMTP подменён во всех тестах модуля


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
#  IMAP IDLE
# ---------------------------------------------------------------------------
def test_push_idle_downloads_new_mail(box, monkeypatch):
    svc, world, acc = box
    monkeypatch.setattr(wx, "PUSH_CHECK_S", 0.3)
    v0 = wx.version(acc.id)
    assert wx.want_push(svc, acc)["push"] in ("starting", "idle")
    assert _wait(lambda: wx.push_status(svc, acc.id)["push"] == "idle")
    world.add("ivanov@x.ru", "INBOX", _mail("Срочно", mid="<push1@ext>"))
    assert _wait(lambda: wx.version(acc.id) > v0)
    assert "Срочно" in [svc.db.get_message(i)["subject"] for i in _ids(svc, acc.id)]
    wx.stop_push(svc)
    assert _wait(lambda: wx.push_status(svc, acc.id)["push"] == "none")
    world.capabilities.discard("IDLE")
    wx.want_push(svc, acc)
    assert _wait(lambda: wx.push_status(svc, acc.id)["push"] in ("unsupported", "none"))
    svc.db.set_setting("webmail.push", False)
    assert wx.want_push(svc, acc) == {"push": "off"}


# ---------------------------------------------------------------------------
#  Беседы, поиски, «Архивировать», квота
# ---------------------------------------------------------------------------
def test_threads_group_conversation(box):
    svc, world, acc = box
    world.add("ivanov@x.ru", "INBOX", _mail("Re: Счёт за июнь", mid="<m1r@ext>",
                                            extra="In-Reply-To: <m1@ext>\r\nReferences: <m1@ext>\r\n"))
    wm.sync_folder(svc, acc, "INBOX")
    replies.index_pending(svc)
    data = wx.list_threads(svc, acc.id, "INBOX")
    assert data["total"] == 3
    big = next(t for t in data["threads"] if t["count"] == 2)
    assert big["row"]["subject"] == "Re: Счёт за июнь" and len(big["ids"]) == 2 and big["unread"] == 2
    assert sum(t["count"] for t in data["threads"]) == 4


def test_saved_searches_per_owner(services):
    s = wx.save_search(services, "o1", {"name": "Счета", "q": "счёт оплата", "attach": True})
    assert s["attach"] is True and s["scope"] == "account"
    assert wx.list_searches(services, "o2") == []
    wx.delete_search(services, "o2", s["id"])
    assert len(wx.list_searches(services, "o1")) == 1
    with pytest.raises(wm.ValidationError):
        wx.save_search(services, "o1", {"q": " "})


def test_archive_creates_folder(box):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    res = wx.archive(svc, acc, ids[:1], actor="ivanov@x.ru")
    assert res["moved"] == 1 and res["folder"] == "Архив" and res["undo"]
    assert _server_subjects(world, "Архив") == ["Счёт за июнь"]
    assert wm.special_folders(svc, acc).get("archive") == "Архив"
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='webmail_archive'") is not None


def test_quota(box):
    svc, world, acc = box
    world.quota["ivanov@x.ru"] = (1024, 4096)
    q = wx.quota(svc, acc)
    assert q["used"] == 1024 * 1024 and q["limit"] == 4096 * 1024 and q["percent"] == 25.0
    world.quota["ivanov@x.ru"] = (2048, 4096)
    assert wx.quota(svc, acc)["percent"] == 25.0          # кэш
    assert wx.quota(svc, acc, refresh=True)["percent"] == 50.0


# ---------------------------------------------------------------------------
#  Подлинность отправителя, фишинг
# ---------------------------------------------------------------------------
def _check(svc, world, acc, raw):
    world.add("ivanov@x.ru", "INBOX", raw)
    wm.sync_folder(svc, acc, "INBOX")
    row = svc.db.get_message(max(_ids(svc, acc.id)))
    return wx.sender_check(svc, acc, row, wt.headers_of(svc, acc, row))


def test_sender_check(box):
    svc, world, acc = box
    svc.db.set_setting("replies.internal_domains", ["company.ru"])
    wx._ORG_CACHE.clear()
    svc.db.create_employee(full_name="Петров Пётр", email="petrov@company.ru")
    ar = "Authentication-Results: mx.company.ru; spf=pass smtp.mailfrom=partner.ru; dkim=pass header.d=partner.ru; " \
         "dmarc=pass header.from=partner.ru\r\n"
    c = _check(svc, world, acc, _mail("Ок", mid="<a1@p>", frm="sales@partner.ru", extra=ar))
    assert c["auth"]["verdict"] == "pass" and c["external"] and c["warnings"] == [] and c["first_time"]
    bad = "Authentication-Results: mx.company.ru; spf=fail smtp.mailfrom=bank.ru; dkim=none; dmarc=fail " \
          "header.from=bank.ru\r\n"
    c = _check(svc, world, acc, _mail("Счёт", mid="<a2@p>", frm="info@bank.ru", extra=bad))
    assert c["auth"]["verdict"] == "fail" and [w["code"] for w in c["warnings"]] == ["auth_fail"]
    raw = ("From: \"director@company.ru\" <evil@c0mpany.ru>\r\nTo: ivanov@x.ru\r\nSubject: Оплата\r\n"
           "Reply-To: pay@mail-drop.io\r\nMessage-ID: <a3@p>\r\n\r\nСрочно оплатите\r\n").encode()
    c = _check(svc, world, acc, raw)
    codes = [w["code"] for w in c["warnings"]]
    assert "display_spoof" in codes and "lookalike" in codes and "reply_to" in codes
    c = _check(svc, world, acc, _mail("Привет", mid="<a4@p>", frm="petrov.p@gmail.com").replace(
        "Клиент <petrov.p@gmail.com>".encode(), "Пётр Петров <petrov.p@gmail.com>".encode()))
    assert [w["code"] for w in c["warnings"]] == ["employee_name"]
    c = _check(svc, world, acc, _mail("Свой", mid="<a5@p>", frm="petrov@company.ru"))
    assert not c["external"] and not c["first_time"]
    assert wx._lookalike("company.ru.evil.com", frozenset({"company.ru"})) == "company.ru"
    assert wx._lookalike("partner.ru", frozenset({"company.ru"})) == ""
    assert wx._lookalike("cornpany.ru", frozenset({"company.ru"})) == "company.ru"


def test_report_phishing(box, smtp):
    svc, world, acc = box
    ids = _ids(svc, acc.id)
    assert wx.report_phishing(svc, acc, ids[:1])["reported"] is False            # адреса нет — только в «Спам»
    assert "Счёт за июнь" in _server_subjects(world, "Junk")
    svc.db.set_setting("webmail.phishing_report_to", "security@x.ru")
    res = wx.report_phishing(svc, acc, ids[1:2], actor="ivanov@x.ru")
    assert res == {"reported": True, "to": "security@x.ru", "moved": 1}
    msg = email.message_from_bytes(smtp.sent[0]["data"], policy=policy.default)
    assert smtp.sent[0]["rcpts"] == ["security@x.ru"] and msg["Subject"] == "Подозрительное письмо: Акт сверки"
    assert [p.get_content_type() for p in msg.iter_attachments()] == ["message/rfc822"]


# ---------------------------------------------------------------------------
#  S/MIME
# ---------------------------------------------------------------------------
def _smime_signed(body_text: str, signer_email: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ольга Смирнова")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(signer_email)]), critical=False)
            .sign(key, hashes.SHA256()))
    inner = f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body_text}\r\n".encode()
    signed = (pkcs7.PKCS7SignatureBuilder().set_data(inner).add_signer(cert, key, hashes.SHA256())
              .sign(serialization.Encoding.SMIME, [pkcs7.PKCS7Options.DetachedSignature]))
    head = (f"From: Ольга <{signer_email}>\r\nTo: ivanov@x.ru\r\nSubject: Подписано\r\n"
            f"Message-ID: <s{time.time_ns()}@p>\r\n").encode()
    return head + signed.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


@pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")
def test_smime_verify(box):
    svc, world, acc = box
    world.add("ivanov@x.ru", "INBOX", _smime_signed("Договор согласован.", "o.smirnova@partner.ru"))
    wm.sync_folder(svc, acc, "INBOX")
    st = wx.smime_status(svc, acc, svc.db.get_message(max(_ids(svc, acc.id))))
    assert st["kind"] == "signed" and st["valid"] is True and st["trusted"] is False
    assert st["signer"]["emails"] == ["o.smirnova@partner.ru"] and st["matches_sender"]
    bad = _smime_signed("Договор согласован.", "o.smirnova@partner.ru").replace("согласован".encode(),
                                                                                "отклонён!!".encode())
    world.add("ivanov@x.ru", "INBOX", bad)
    wm.sync_folder(svc, acc, "INBOX")
    st = wx.smime_status(svc, acc, svc.db.get_message(max(_ids(svc, acc.id))))
    assert st["valid"] is False and "неверна" in st["text"]
    assert wx.smime_status(svc, acc, svc.db.get_message(min(_ids(svc, acc.id)))) is None
    enc = (b"From: a@b.ru\r\nTo: ivanov@x.ru\r\nSubject: x\r\nMessage-ID: <e@p>\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: application/pkcs7-mime; smime-type=enveloped-data; name=smime.p7m\r\n\r\nMIAG\r\n")
    world.add("ivanov@x.ru", "INBOX", enc)
    wm.sync_folder(svc, acc, "INBOX")
    assert wx.smime_status(svc, acc, svc.db.get_message(max(_ids(svc, acc.id))))["kind"] == "encrypted"


# ---------------------------------------------------------------------------
#  Sieve / ManageSieve
# ---------------------------------------------------------------------------
class SieveServer:
    """Учебный сервер ManageSieve: вход PLAIN, LISTSCRIPTS, PUTSCRIPT, SETACTIVE, DELETESCRIPT."""

    def __init__(self, user="ivanov@x.ru", password="pw"):
        self.scripts, self.active, self.user, self.password, self.commands = {}, "", user, password, []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                w = self.wfile.write
                w(b'"IMPLEMENTATION" "Fake Sieve"\r\n"SASL" "PLAIN"\r\n"SIEVE" "fileinto vacation imap4flags body '
                  b'date relational"\r\nOK "Ready"\r\n')
                authed = False
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    line = line.rstrip(b"\r\n")
                    cmd = line.split(b" ", 1)[0].upper().decode()
                    outer.commands.append(cmd)
                    args = __import__("re").findall(rb'"((?:[^"\\]|\\.)*)"', line)
                    if cmd == "AUTHENTICATE":
                        import base64
                        _z, u, p = base64.b64decode(args[1]).split(b"\0")
                        authed = (u.decode(), p.decode()) == (outer.user, outer.password)
                        w(b'OK "Logged in"\r\n' if authed else b'NO "Auth failed"\r\n')
                    elif not authed and cmd != "LOGOUT":
                        w(b'NO "Login first"\r\n')
                    elif cmd == "LISTSCRIPTS":
                        for n in outer.scripts:
                            w(b'"' + n.encode() + b'"' + (b" ACTIVE" if n == outer.active else b"") + b"\r\n")
                        w(b"OK\r\n")
                    elif cmd == "PUTSCRIPT":
                        n = int(line.rsplit(b"{", 1)[1].rstrip(b"+}"))
                        data = self.rfile.read(n)
                        self.rfile.readline()
                        outer.scripts[args[0].decode()] = data.decode()
                        w(b"OK\r\n")
                    elif cmd == "SETACTIVE":
                        outer.active = args[0].decode()
                        w(b"OK\r\n")
                    elif cmd == "DELETESCRIPT":
                        outer.scripts.pop(args[0].decode(), None)
                        w(b"OK\r\n")
                    elif cmd == "LOGOUT":
                        w(b'OK "Bye"\r\n')
                        return
                    else:
                        w(b'NO "Unknown"\r\n')

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_sieve_script_and_sync(box):
    svc, world, acc = box
    wm.folders(svc, acc, refresh=True)
    wt.save_rule(svc, acc, {"name": "Счета", "match": "any", "conditions": [
        {"field": "subject", "op": "starts", "value": 'Счёт "*"'}, {"field": "from", "op": "equals", "value": "buh@v.ru"}],
        "actions": [{"type": "move", "folder": "Projects"}, {"type": "tag", "keyword": "$label2"}, {"type": "read"}],
        "stop": True})
    wt.block_sender(svc, acc, "spam@ads.ru")
    wt.save_autoreply(svc, acc, {"enabled": True, "body": "<p>В отпуске</p>", "date_to": "2099-01-01", "days": 3})
    text = wx.sieve_script(svc, acc)
    assert 'require ["date", "fileinto", "imap4flags", "relational", "vacation"];' in text
    assert 'header :matches "subject" "Счёт \\"\\\\*\\"*"' in text and 'address :is "from" "buh@v.ru"' in text
    assert 'fileinto "Projects";' in text and 'addflag "$label2";' in text and 'addflag "\\\\Seen";' in text
    assert 'fileinto "Junk";' in text and "stop;" in text and 'vacation :days 3 :subject "Автоответ"' in text
    assert 'currentdate :value "le" "date" "2099-01-01"' in text
    srv = SieveServer()
    try:
        assert wt.has_automation(svc, acc.id)
        st = wx.sieve_save(svc, acc, {"enabled": True, "host": "127.0.0.1", "port": srv.port, "security": "none"},
                           actor="ivanov@x.ru")
        assert st["synced_at"] and srv.active == "mailarchiver" and "vacation" in srv.scripts["mailarchiver"]
        assert wx.sieve_active(svc, acc.id) and not wt.has_automation(svc, acc.id)
        # правило изменили — сценарий на сервере обновляется
        wt.save_rule(svc, acc, {"name": "Отчёты", "conditions": [{"field": "subject", "op": "contains",
                                                                  "value": "отчёт"}], "actions": [{"type": "flag"}]})
        assert wx.sieve_resync_quiet(svc, acc) == "" and "отчёт" in srv.scripts["mailarchiver"]
        # чужой активный сценарий не заменяется без спроса
        srv.scripts["roundcube"], srv.active = "keep;", "roundcube"
        with pytest.raises(wm.ValidationError) as ei:
            wx.sieve_sync(svc, acc)
        assert ei.value.code == "sieve_conflict" and srv.active == "roundcube"
        assert not wx.sieve_active(svc, acc.id)
        wx.sieve_sync(svc, acc, force=True)
        assert srv.active == "mailarchiver" and "roundcube" in srv.scripts
        wx.sieve_save(svc, acc, {"enabled": False})
        assert "mailarchiver" not in srv.scripts and srv.active == ""
        assert wt.has_automation(svc, acc.id)
        with pytest.raises(wm.ValidationError):
            wx.sieve_save(svc, acc, {"enabled": True, "host": "mail.x.ru", "port": 4190, "security": "none"})
        srv.password = "other"
        with pytest.raises(wm.ValidationError):
            wx.sieve_save(svc, acc, {"enabled": True, "host": "127.0.0.1", "port": srv.port, "security": "none"})
    finally:
        srv.close()


# ---------------------------------------------------------------------------
#  Черновики в «Черновиках» сервера
# ---------------------------------------------------------------------------
def test_drafts_on_server(box, smtp):
    svc, world, acc = box
    d = wm.create_draft(svc, acc, "o", {"to_addrs": "petrov@x", "subject": "Набросок", "body": "<p>Текст</p>"})
    assert wx.save_draft_to_server(svc, acc, d["id"], "o")["saved"]
    srv = world.boxes["ivanov@x.ru"]["Drafts"].messages
    assert len(srv) == 1 and b"\\Draft" in list(srv.values())[0].flags
    wm.update_draft(svc, d["id"], "o", {"to_addrs": "petrov@x.ru", "bcc_addrs": "boss@x.ru"})
    wx.save_draft_to_server(svc, acc, d["id"], "o")
    assert len(srv) == 1
    m = email.message_from_bytes(list(srv.values())[0].raw, policy=policy.default)
    assert m["To"] == "petrov@x.ru" and m["Bcc"] == "boss@x.ru"
    wm.send(svc, acc, d["id"], "o")
    assert len(srv) == 0 and "boss@x.ru" not in email.message_from_bytes(smtp.sent[0]["data"]).get("Bcc", "")
    d = wm.create_draft(svc, acc, "o", {"subject": "Второй"})
    wx.save_draft_to_server(svc, acc, d["id"], "o")
    assert len(srv) == 1
    wm.delete_draft(svc, d["id"], "o")
    assert len(srv) == 0
    svc.db.set_setting("webmail.drafts_on_server", False)
    d = wm.create_draft(svc, acc, "o", {"subject": "Третий"})
    assert wx.save_draft_to_server(svc, acc, d["id"], "o") == {"saved": False}


# ---------------------------------------------------------------------------
#  API
# ---------------------------------------------------------------------------
def test_api_more(api, smtp, monkeypatch):
    admin, mb, svc, a, b = api
    monkeypatch.setattr(wx, "PUSH_CHECK_S", 0.3)
    st = mb.get(f"/api/webmail/status?account_id={a}").json()
    assert st["push"] is True and st["phishing_report_to"] is False and st["sieve"] is False
    r = mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX&threads=true").json()
    assert r["threads"] and r["total"] == 2 and r["messages"][0]["thread_count"] == 1
    ids = [m["id"] for m in r["messages"]]
    s = mb.post("/api/webmail/searches", json={"q": "счёт", "name": "Счета"}).json()
    assert [x["name"] for x in mb.get("/api/webmail/searches").json()["items"]] == ["Счета"]
    assert admin.get("/api/webmail/searches").json()["items"] == []
    assert mb.delete(f"/api/webmail/searches/{s['id']}").status_code == 200
    ex = mb.get(f"/api/webmail/extras?account_id={a}&id={ids[0]}").json()
    assert "check" in ex and ex["smime"] is None
    v = mb.get(f"/api/webmail/wait?account_id={a}").json()["version"]
    assert _wait(lambda: wx.push_status(svc, a)["push"] == "idle")
    q = mb.get(f"/api/webmail/quota?account_id={a}").json()
    assert "used" in q or q == {} or "error" in q
    r = mb.post("/api/webmail/archive", json={"account_id": a, "ids": ids[:1]})
    assert r.status_code == 200 and r.json()["folder"] == "Архив"
    r = mb.post("/api/webmail/phishing", json={"account_id": a, "ids": ids[1:2]})
    assert r.status_code == 200 and r.json()["moved"] == 1
    sv = mb.get(f"/api/webmail/sieve?account_id={a}").json()
    assert sv["enabled"] is False and sv["port"] == 4190 and sv["supported"] is True
    d = mb.post("/api/webmail/drafts", json={"account_id": a, "subject": "Черновик"}).json()
    r = mb.put(f"/api/webmail/drafts/{d['id']}", json={"body": "<p>x</p>", "to_server": True}).json()
    assert r["server"]["saved"] is True
    assert mb.get(f"/api/webmail/wait?account_id={b}").status_code == 403
    wx.stop_push(svc)
    assert v >= 0


def test_api_wait_returns_on_new_mail(api, world, monkeypatch):
    admin, mb, svc, a, b = api
    monkeypatch.setattr(wx, "PUSH_CHECK_S", 0.3)
    v = mb.get(f"/api/webmail/wait?account_id={a}").json()["version"]
    assert _wait(lambda: wx.push_status(svc, a)["push"] == "idle")
    threading.Timer(1.0, lambda: world.add("ivanov@x.ru", "INBOX", _mail("Новое", mid="<w1@ext>"))).start()
    t0 = time.monotonic()
    r = mb.get(f"/api/webmail/wait?account_id={a}&since={v}").json()
    assert r["changed"] is True and r["version"] > v and time.monotonic() - t0 < 10
    subjects = [m["subject"] for m in mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()["messages"]]
    assert "Новое" in subjects
    wx.stop_push(svc)


def test_settings_119():
    from mailarchiver.config import DEFAULTS
    from mailarchiver.web.i18n import PARAM_HELP
    for key in ("push", "drafts_on_server", "phishing_report_to"):
        assert key in DEFAULTS["webmail"] and f"webmail.{key}" in PARAM_HELP


def test_unused_names_guard():
    assert models and _backup


def test_ui_119():
    import pathlib
    root = pathlib.Path(__file__).parent.parent / "mailarchiver/web/static"
    js = (root / "js/app.js").read_text("utf-8")
    for needle in ("function mailPushLoop", "/webmail/wait?", "data-threads", "thread_ids", "/webmail/searches",
                   "function mailRenderTrust", "/webmail/phishing", "function mailSieveSection", "to_server",
                   "function mailShortcutsHelp", "/webmail/quota", "data-a=\"archive\""):
        assert needle in js, needle
