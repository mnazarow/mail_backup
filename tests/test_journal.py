"""Приём журналирования по SMTP (1.20.0): Postfix-копии, отчёты Exchange, повторы, доступ, размер, STARTTLS."""
import datetime as dt
import smtplib
import socket
import ssl
from email.message import EmailMessage

import pytest

from mailarchiver import journal, models, search


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _msg(subject="Договор", frm="client@ext.ru", to="ivanov@x.ru", cc="", mid="<j1@ext>", body="Текст"):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Message-ID"] = mid
    m["Date"] = "Mon, 01 Jun 2026 10:00:00 +0300"
    m.set_content(body)
    return m


@pytest.fixture()
def accs(services):
    svc = services
    a = svc.db.create_account(models.Account(name="Иванов", host="h", username="ivanov@x.ru", password="p"))
    b = svc.db.create_account(models.Account(name="Петров", host="h", username="petrov@x.ru", password="p"))
    c = svc.db.create_account(models.Account(name="Скрытый", host="h", username="audit-box@x.ru", password="p"))
    emp = svc.db.create_employee(full_name="Сидорова Анна", email="sidorova@x.ru")
    d = svc.db.create_account(models.Account(name="Сидорова", host="h", username="anna.s@x.ru", password="p"))
    svc.db.link_employee_account(emp, d)
    svc.set_rt("journal", "address", "journal@x.ru")
    return svc, a, b, c, d


def _folder_ids(svc, aid, folder="Журнал"):
    return [r["id"] for r in svc.db.query("SELECT id FROM messages WHERE account_id=? AND folder=?", (aid, folder))]


def test_store_plain_copy_and_duplicates(accs):
    svc, a, b, c, d = accs
    raw = _msg(cc="petrov@x.ru, sidorova@x.ru").as_bytes()
    res = journal.store(svc, raw, "client@ext.ru", ["journal@x.ru"])
    assert res["accounts"] == sorted([a, b, d]) and res["stored"] == 3
    assert len(_folder_ids(svc, a)) == 1 and len(_folder_ids(svc, d)) == 1
    row = svc.db.get_message(_folder_ids(svc, a)[0])
    assert row["subject"] == "Договор" and svc.store.read_message(a, row["stored_path"]) == raw
    again = journal.store(svc, raw, "client@ext.ru", ["journal@x.ru"])
    assert again["stored"] == 0 and again["duplicates"] == 3
    other = journal.store(svc, _msg(to="nobody@ext.ru", mid="<j2@ext>").as_bytes(), "client@ext.ru", ["journal@x.ru"])
    assert other["accounts"] == []
    st = journal.status(svc)
    assert st["unmatched"] >= 1 and st["in_archive"] == 3
    search.index_pending(svc)
    assert search.search(svc, "договор", account_ids=[a])["results"]


def test_exchange_journal_report_unwrapped(accs):
    svc, a, b, c, d = accs
    original = _msg(subject="Секретно", frm="ivanov@x.ru", to="partner@ext.ru", mid="<x1@x.ru>").as_bytes()
    report = EmailMessage()
    report["From"] = "Microsoft Exchange <journal@x.ru>"
    report["To"] = "journal@x.ru"
    report["Subject"] = "Секретно"
    report["X-MS-Journal-Report"] = ""
    report.set_content("Sender: ivanov@x.ru\r\nSubject: Секретно\r\nMessage-Id: <x1@x.ru>\r\n"
                       "To: partner@ext.ru\r\nBcc: audit-box@x.ru\r\n")
    report.add_attachment(_msg(subject="Секретно", frm="ivanov@x.ru", to="partner@ext.ru", mid="<x1@x.ru>"))
    res = journal.store(svc, report.as_bytes(), "", ["journal@x.ru"])
    assert res["accounts"] == sorted([a, c])                     # скрытый адресат — из текста отчёта
    stored = svc.db.get_message(_folder_ids(svc, c)[0])
    data = svc.store.read_message(c, stored["stored_path"])
    assert b"X-MS-Journal-Report" not in data and b"Sender: ivanov@x.ru" not in data
    assert original.split(b"\n\n", 1)[1].strip() in data


def test_smtp_server_end_to_end(accs):
    svc, a, b, c, d = accs
    port = _free_port()
    svc.set_rt("journal", "enabled", True)
    svc.set_rt("journal", "port", port)
    svc.set_rt("journal", "max_size_mb", 1)
    try:
        st = journal.ensure(svc)
        assert st["running"] and st["listen"].endswith(str(port))
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
            s.ehlo()
            assert s.has_extn("size") and not s.has_extn("starttls")
            s.sendmail("client@ext.ru", ["journal@x.ru"], _msg(mid="<smtp1@ext>").as_bytes())
            big = _msg(mid="<big@ext>", body="x" * (2 * 1024 * 1024)).as_bytes()
            with pytest.raises(smtplib.SMTPException):
                s.sendmail("client@ext.ru", ["journal@x.ru"], big)
        assert len(_folder_ids(svc, a)) == 1
        # не из разрешённой сети — отказ сразу
        svc.set_rt("journal", "allowed_ips", ["10.0.0.0/8"])
        with pytest.raises(smtplib.SMTPException):
            with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
                s.ehlo()
        assert journal.status(svc)["rejected"] >= 1
        svc.set_rt("journal", "enabled", False)
        assert journal.ensure(svc)["running"] is False
    finally:
        journal.stop()


def _cert_files(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "c.pem", tmp_path / "k.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return str(cp), str(kp)


def test_starttls_required(accs, tmp_path):
    svc, a, b, c, d = accs
    port = _free_port()
    cert, key = _cert_files(tmp_path)
    for k, v in {"enabled": True, "port": port, "tls_cert": cert, "tls_key": key, "require_tls": True}.items():
        svc.set_rt("journal", k, v)
    try:
        assert journal.ensure(svc)["tls"]
        with smtplib.SMTP("127.0.0.1", port, timeout=10) as s:
            s.ehlo()
            assert s.has_extn("starttls")
            code, _ = s.mail("client@ext.ru")
            assert code == 530
            s.rset()
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s.starttls(context=ctx)
            s.ehlo()
            s.sendmail("client@ext.ru", ["journal@x.ru"], _msg(mid="<tls1@ext>").as_bytes())
        assert len(_folder_ids(svc, a)) == 1
    finally:
        journal.stop()
