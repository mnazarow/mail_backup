"""Исправления по разбору 12.11.2026 (1.21.0): аудит между процессами, S/MIME и скрытая копия,
предел отправки, перенаправления CalDAV, журналирование, LDAP, просмотр писем в аудите, дела."""
import os
import subprocess
import sys
import textwrap

import pytest

import test_webmail as base
from test_webmail_secure import _cert

from mailarchiver import webmail as wm
from mailarchiver import webmail_secure as wsec
from mailarchiver.errors import ValidationError

world, box, api = base.world, base.box, base.api
smtp = base.smtp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_audit_chain_two_processes(tmp_path):
    """Две службы (задания в отдельном процессе, CLI) пишут аудит одновременно — цепочка цела."""
    script = textwrap.dedent("""
        import os, sys, multiprocessing as mp
        os.environ["MAILARCHIVER_DATA"] = sys.argv[1]
        def work(n):
            from mailarchiver.config import load_config
            from mailarchiver.service import Services
            svc = Services(load_config())
            for i in range(150):
                svc.db.add_audit(f"p{n}", "race", str(i))
        if __name__ == "__main__":
            from mailarchiver.config import load_config
            from mailarchiver.service import Services
            svc = Services(load_config()); svc.setup()
            ps = [mp.Process(target=work, args=(k,)) for k in range(3)]
            [p.start() for p in ps]; [p.join() for p in ps]
            r = svc.db.verify_audit()
            print("RESULT", r["total"], len(r["broken"]))
    """)
    path = tmp_path / "race.py"
    path.write_text(script)
    env = dict(os.environ, PYTHONPATH=ROOT)
    out = subprocess.run([sys.executable, str(path), str(tmp_path / "data")], capture_output=True, text=True,
                         env=env, timeout=300, check=False)
    line = [x for x in out.stdout.splitlines() if x.startswith("RESULT")]
    assert line, out.stderr[-2000:]
    total, broken = map(int, line[0].split()[1:])
    assert total >= 450 and broken == 0


def test_encrypted_bcc_gets_separate_copy(box, smtp):
    from cryptography.hazmat.primitives.serialization import pkcs7
    svc, world, acc = box
    _k, _c, p12, _pem = _cert("ivanov@x.ru", "Иван Иванов")
    wsec.upload_key(svc, acc, p12, "secret", "adm")
    pk, pc, _p, ppem = _cert("petrov@x.ru", "Пётр")
    sk, sc, _p2, spem = _cert("secret@x.ru", "Скрытый")
    wsec.import_cert(svc, ppem, "adm")
    wsec.import_cert(svc, spem, "adm")
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "petrov@x.ru", "bcc_addrs": "secret@x.ru",
                                                  "subject": "Тайно", "body": "<p>Текст</p>"})
    wm.update_draft(svc, d["id"], "admin:adm", {"smime_encrypt": True})
    smtp.sent.clear()
    res = wm.send(svc, acc, d["id"], "admin:adm")
    assert res["recipients"] == 2 and [m["rcpts"] for m in smtp.sent] == [["petrov@x.ru"], ["secret@x.ru"]]
    main, copy = smtp.sent[0]["data"], smtp.sent[1]["data"]
    assert pkcs7.pkcs7_decrypt_smime(main, pc, pk, [])
    with pytest.raises(ValueError):
        pkcs7.pkcs7_decrypt_smime(main, sc, sk, [])          # скрытого адресата в основном письме нет
    assert pkcs7.pkcs7_decrypt_smime(copy, sc, sk, [])


def test_send_limit_per_hour(box, smtp):
    svc, world, acc = box
    svc.set_rt("webmail", "send_limit_hour", 3)
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "a@x.ru, b@x.ru", "subject": "1", "body": "x"})
    wm.send(svc, acc, d["id"], "admin:adm")
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "c@x.ru, d@x.ru", "subject": "2", "body": "x"})
    with pytest.raises(ValidationError) as ei:
        wm.send(svc, acc, d["id"], "admin:adm")
    assert ei.value.code == "send_limit"
    with pytest.raises(ValidationError):
        wm.send(svc, acc, d["id"], "admin:adm")
    assert len(svc.db.query("SELECT 1 FROM audit WHERE action='webmail_send_limit'")) == 1   # раз в час
    svc.set_rt("webmail", "send_limit_hour", 0)
    assert wm.send(svc, acc, d["id"], "admin:adm")["ok"]


def test_dav_redirect_to_other_server_refused(box, monkeypatch):
    """Перенаправление на другой сервер — без пароля ящика: ошибка с новым адресом."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mailarchiver import webmail_dav as wdav
    seen = []

    class Other(BaseHTTPRequestHandler):
        def do_PROPFIND(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass
    other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{other.server_address[1]}/dav/"

    class Redir(BaseHTTPRequestHandler):
        def do_PROPFIND(self):  # noqa: N802
            self.send_response(301)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *a):
            pass
    first = ThreadingHTTPServer(("127.0.0.1", 0), Redir)
    threading.Thread(target=first.serve_forever, daemon=True).start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    svc, world, acc = box
    try:
        with pytest.raises(ValidationError) as ei:
            wdav.discover(acc, f"http://127.0.0.1:{first.server_address[1]}/")
        assert ei.value.code == "dav_redirect" and target in ei.value.message
        assert seen == []                                    # другой сервер пароля не получил
    finally:
        other.shutdown()
        first.shutdown()


def test_staff_message_view_audited(api):
    admin, mb, svc, a, b = api
    pk = svc.db.query_one("SELECT id FROM messages WHERE account_id=? LIMIT 1", (a,))["id"]
    assert admin.get(f"/api/accounts/{a}/messages/{pk}").status_code == 200
    assert admin.get(f"/api/accounts/{a}/messages/{pk}").status_code == 200          # повтор в течение часа
    assert admin.get(f"/api/accounts/{a}/messages/{pk}/raw").status_code == 200
    assert mb.get(f"/api/accounts/{a}/messages/{pk}").status_code == 200              # сотрудник — свой ящик
    rows = svc.db.query("SELECT user, detail FROM audit WHERE action='message_view' ORDER BY id")
    assert [r["detail"].split(" — ")[1] for r in rows] == ["просмотр", "скачан исходный файл .eml"]
    assert all(r["user"] == "adm" or r["user"].startswith("adm") for r in rows)


def test_attachment_time_limit(monkeypatch):
    """Разбор вложения дольше предела — прерывается; у PDF остаётся прочитанное начало."""
    from mailarchiver import attachtext
    calls = {"n": 0}
    real = attachtext._page_text

    def slow(content, fonts, budget):
        calls["n"] += 1
        import time as _t
        _t.sleep(0.15)
        return real(content, fonts, budget)
    monkeypatch.setattr(attachtext, "_page_text", slow)
    pages = []
    for i in range(20):
        stream = f"BT /F1 12 Tf 10 10 Td (Page{i}) Tj ET".encode()
        pages.append(stream)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(20)) + b"] /Count 20 >>"]
    for i, st in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(st)).encode() + b" >>\nstream\n" + st + b"\nendstream")
    pdf = b"%PDF-1.4\n" + b"".join(f"{n + 1} 0 obj\n".encode() + o + b"\nendobj\n" for n, o in enumerate(objs)) + b"%%EOF"
    text = attachtext.extract("a.pdf", "application/pdf", pdf, 65536, time_s=0.5)
    assert "Page0" in text and "Page19" not in text and calls["n"] < 20


def test_attachment_backfill(box):
    """Архив, проиндексированный без текста вложений, дочитывается без перестройки индекса."""
    from mailarchiver import search
    svc, world, acc = box
    svc.set_rt("search", "index_attachments", False)
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "x@ext.ru", "ivanov@x.ru", "Счёт", "<att1@ext>"
    m.set_content("см. вложение")
    m.add_attachment("Поставка арматуры по договору".encode("utf-8"), maintype="text", subtype="plain",
                     filename="счёт.txt")
    world.add("ivanov@x.ru", "INBOX", m.as_bytes())
    base._backup(svc, acc.id)
    search.index_pending(svc)
    assert not search.search(svc, "арматуры", account_ids=[acc.id])["results"]
    svc.set_rt("search", "index_attachments", True)
    assert search.attach_backfill_pending(svc.db) >= 1
    res = search.backfill_attachments(svc)
    assert res["indexed"] >= 1 and res["pending"] == 0
    assert search.search(svc, "арматуры", account_ids=[acc.id])["results"]
    # новые письма: отметка идёт вместе с индексом, дочитывать нечего
    world.add("ivanov@x.ru", "INBOX", m.as_bytes().replace(b"<att1@ext>", b"<att2@ext>"))
    base._backup(svc, acc.id)
    search.index_pending(svc)
    assert search.attach_backfill_pending(svc.db) == 0


def test_audit_forwarded_to_syslog(services):
    """Записи аудита уходят на сервер журналов (RFC 5424, TCP с длиной кадра)."""
    import socket as _s
    import threading
    import time
    from mailarchiver import auditlog
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def accept():
        buf = b""
        for _ in range(2):                                  # пробная запись и пересылка — разные соединения
            conn, _ = srv.accept()
            conn.settimeout(5)
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if b"user_create" in buf:
                        break
            except OSError:
                pass
        got.append(buf)
    threading.Thread(target=accept, daemon=True).start()
    svc = services
    svc.set_rt("audit", "syslog_host", "127.0.0.1")
    svc.set_rt("audit", "syslog_port", srv.getsockname()[1])
    svc.set_rt("audit", "syslog_proto", "tcp")
    assert auditlog.test_send(svc)["ok"] is True
    svc.db.add_audit("adm", "user_create", "ivanov: оператор")
    for _ in range(50):
        if got:
            break
        time.sleep(0.1)
    data = got[0]
    assert data.split(b" ", 1)[0].isdigit()                       # RFC 6587: длина кадра
    assert b"<85>1 " in data and b'action="user_create"' in data and "ivanov: оператор".encode() in data
    svc.set_rt("audit", "syslog_host", "")
    srv.close()
    rec = {"id": 1, "ts": "2026-01-01T00:00:00+00:00", "user": 'a"b', "action": "x]", "detail": "d", "hash": "h"}
    assert b'user="a\\"b" action="x\\]"' in auditlog.format_rfc5424(rec, "host")


def test_restore_drill(box):
    from mailarchiver import drill
    svc, world, acc = box
    base._backup(svc, acc.id)
    res = drill.run(svc)
    assert res["checked"] >= 1 and res["bad"] == 0
    row = svc.db.query_one("SELECT id, stored_path FROM messages WHERE account_id=? AND stored_path<>'' LIMIT 1",
                           (acc.id,))
    svc.db.execute("UPDATE messages SET sha256='00' WHERE account_id=?", (acc.id,))   # «повреждение»
    svc.set_rt("drill", "sample", 1000)
    res = drill.run(svc)
    assert res["bad"] == res["checked"] and "SHA-256" in res["problems"][0] and not res["ok"]
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='restore_drill'")
    assert row is not None


def test_ocr_with_fake_tesseract(tmp_path, monkeypatch):
    """OCR: картинка и скан в PDF читаются tesseract/pdftoppm, если они есть (здесь — заглушки)."""
    from mailarchiver import attachtext
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tesseract").write_text("#!/bin/sh\ncat >/dev/null\necho 'Договор поставки № 77'\n")
    (bin_dir / "pdftoppm").write_text("#!/bin/sh\nfor last; do :; done\nprintf x > \"$last-1.png\"\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    assert attachtext.kind_of("скан.jpg", "image/jpeg") == ""
    assert attachtext.kind_of("скан.jpg", "image/jpeg", ocr=True) == ".jpg"
    ocr = {"lang": "rus+eng", "max_pages": 2}
    assert "Договор поставки" in attachtext.extract("скан.jpg", "image/jpeg", b"\xff\xd8jpeg", ocr=ocr)
    scan = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
    assert "№ 77" in attachtext.extract("scan.pdf", "application/pdf", scan, ocr=ocr)
    assert attachtext.extract("scan.pdf", "application/pdf", scan) == ""      # без OCR — пусто


def test_empty_address_parts_tolerated(api):
    """«a@b, , c@d» (после подсказки адресов) — оба адреса, а не «ни одного» (строгий разбор Python 3.13)."""
    admin, mb, svc, a, b = api
    assert [x for _n, x in wm._addresses("petrov@x.ru, , sidorov@x.ru, , ")] == ["petrov@x.ru", "sidorov@x.ru"]
    assert wm._addresses('"Иванов, Иван" <iv@x.ru>, , c@x.ru')[0] == ("Иванов, Иван", "iv@x.ru")
    r = mb.get(f"/api/webmail/smime/recipients?account_id={a}&emails=petrova%40x.ru%2C%20%2C%20").json()
    assert r["missing"] == ["petrova@x.ru"]
