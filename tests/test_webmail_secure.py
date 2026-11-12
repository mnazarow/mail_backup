"""Почтовый клиент 1.20.0: подпись и шифрование S/MIME, расшифровка, проверки перед отправкой, письма из архива."""
import datetime as dt
import email
import io
import os
import shutil
import subprocess
from email import policy

import pytest

import test_webmail as base
from test_webmail import _ids

from mailarchiver import webmail as wm
from mailarchiver import webmail_more as wx
from mailarchiver import webmail_secure as wsec
from mailarchiver.errors import ValidationError

world, box, api = base.world, base.box, base.api
smtp = base.smtp


def _cert(email_addr, name="Тест"):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email_addr)]), False)
            .sign(key, hashes.SHA256()))
    p12 = pkcs12.serialize_key_and_certificates(b"t", key, cert, None,
                                                serialization.BestAvailableEncryption(b"secret"))
    return key, cert, p12, cert.public_bytes(serialization.Encoding.PEM)


def test_sign_encrypt_and_decrypt(box, smtp):
    from cryptography.hazmat.primitives.serialization import pkcs7
    svc, world, acc = box
    _k, _c, p12, _pem = _cert("ivanov@x.ru", "Иван Иванов")
    with pytest.raises(ValidationError):
        wsec.upload_key(svc, acc, p12, "wrong", "adm")
    info = wsec.upload_key(svc, acc, p12, "secret", "adm")
    assert info["present"] and info["emails"] == ["ivanov@x.ru"]
    # подпись
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "petrov@x.ru", "subject": "Подписано", "body": "<p>Текст</p>",
                                                  "smime_sign": True})
    wm.update_draft(svc, d["id"], "admin:adm", {"smime_sign": True})
    wm.send(svc, acc, d["id"], "admin:adm")
    raw = smtp.sent[-1]["data"]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/signed" and msg["Subject"] == "Подписано"
    if shutil.which("openssl"):
        tmp = os.path.join(svc.cfg.tmp_dir, "s.eml")
        os.makedirs(svc.cfg.tmp_dir, exist_ok=True)
        open(tmp, "wb").write(raw)
        r = subprocess.run(["openssl", "smime", "-verify", "-noverify", "-in", tmp, "-out", os.devnull],
                           capture_output=True, check=False)
        assert r.returncode == 0, r.stderr
    # шифрование: без сертификата получателя — нельзя
    pk, pc, _p, ppem = _cert("petrov@x.ru", "Пётр Петров")
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "petrov@x.ru", "subject": "Секрет", "body": "<p>Пароль 123</p>"})
    wm.update_draft(svc, d["id"], "admin:adm", {"smime_encrypt": True, "smime_sign": True})
    with pytest.raises(ValidationError) as ei:
        wm.send(svc, acc, d["id"], "admin:adm")
    assert ei.value.code == "smime_missing" and "petrov@x.ru" in ei.value.message
    wsec.import_cert(svc, ppem, "adm")
    wm.send(svc, acc, d["id"], "admin:adm")
    raw = smtp.sent[-1]["data"]
    msg = email.message_from_bytes(raw, policy=policy.default)
    assert msg.get_content_type() == "application/pkcs7-mime" and msg["Subject"] == "Секрет"
    assert b"123" not in raw
    inner = pkcs7.pkcs7_decrypt_smime(raw, pc, pk, [])            # получатель читает
    assert b"multipart/signed" in inner
    mine = wsec.decrypt(svc, acc, raw)                            # и отправитель — свою копию
    parsed = email.message_from_bytes(mine, policy=policy.default)
    assert parsed["Subject"] == "Секрет" and parsed.get_content_type() == "multipart/signed"
    # отправленное зашифрованное лежит в «Отправленных» архива — и расшифровывается в клиенте
    sent_row = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND folder='Sent' ORDER BY id DESC",
                                (acc.id,))
    st = wx.smime_status(svc, acc, svc.db.get_message(sent_row["id"]))
    assert st["kind"] == "encrypted" and st["can_decrypt"]


def test_signer_certificate_remembered(box):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7
    if not shutil.which("openssl"):
        pytest.skip("нет openssl")
    svc, world, acc = box
    key, cert, _p, _pem = _cert("client@ext.ru", "Клиент")
    entity = "Content-Type: text/plain; charset=utf-8\r\n\r\nПодписанный текст\r\n".encode("utf-8")
    signed = pkcs7.PKCS7SignatureBuilder().set_data(entity).add_signer(cert, key, hashes.SHA256()).sign(
        serialization.Encoding.SMIME, [pkcs7.PKCS7Options.DetachedSignature])
    raw = b"From: client@ext.ru\r\nTo: ivanov@x.ru\r\nSubject: Signed\r\nMessage-ID: <sg@ext>\r\n" + \
        signed.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    world.add("ivanov@x.ru", "INBOX", raw)
    base._backup(svc, acc.id)
    pk = [i for i in _ids(svc, acc.id) if svc.db.get_message(i)["subject"] == "Signed"][0]
    st = wx.smime_status(svc, acc, svc.db.get_message(pk))
    assert st["valid"]
    assert [c["email"] for c in wsec.list_certs(svc)] == ["client@ext.ru"]
    found, missing = wsec.certs_for(svc, ["client@ext.ru", "nobody@ext.ru"])
    assert list(found) == ["client@ext.ru"] and missing == ["nobody@ext.ru"]


def test_find_sensitive():
    assert wsec.find_sensitive("Карта 4111 1111 1111 1111, срок 12/27") == {"card": 1}
    assert wsec.find_sensitive("Номер заказа 4111 1111 1111 1112") == {}            # не проходит Луна
    assert wsec.find_sensitive("Телефон 8 800 555 35 35 и счёт 40702810900000012345") == {}
    assert wsec.find_sensitive("СНИЛС 112-233-445 95") == {"snils": 1}
    assert wsec.find_sensitive("СНИЛС 112-233-445 96") == {}
    assert wsec.find_sensitive("Паспорт: серия 45 06 номер 123456") == {"passport": 1}
    assert wsec.find_sensitive("ИНН 7707083893, КПП 773601001") == {"inn": 1}
    assert wsec.find_sensitive("ИНН 7707083894") == {}


def test_presend_checks_warn_and_block(box, smtp):
    svc, world, acc = box
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "client@ext.ru", "subject": "Оплата",
                                                  "body": "<p>Карта 4111 1111 1111 1111</p>"})
    with pytest.raises(wm.PresendWarning) as ei:
        wm.send(svc, acc, d["id"], "admin:adm", actor="adm", confirmed=False)
    codes = [i["code"] for i in ei.value.items]
    assert codes == ["external", "sensitive"] and not smtp.sent
    wm.send(svc, acc, d["id"], "admin:adm", actor="adm", confirmed=True)
    assert smtp.sent and svc.db.query_one("SELECT 1 FROM audit WHERE action='webmail_dlp_override'")
    # запрет наружу; внутри организации — только предупреждение
    svc.db.set_setting("webmail.dlp_mode", "block_external")
    att_doc = "Паспорт 45 06 123456".encode()
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "client@ext.ru", "subject": "Анкета", "body": "<p>см.</p>"})
    wm.add_attachment(svc, d["id"], "admin:adm", "анкета.txt", "text/plain", io.BytesIO(att_doc))
    with pytest.raises(ValidationError) as ei:
        wm.send(svc, acc, d["id"], "admin:adm", confirmed=True)
    assert ei.value.code == "presend_block" and "анкета.txt" in ei.value.message
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "petrov@x.ru", "subject": "Анкета", "body": "<p>Паспорт 45 06 123456</p>"})
    with pytest.raises(wm.PresendWarning):
        wm.send(svc, acc, d["id"], "admin:adm", confirmed=False)
    # много адресатов в «Кому»
    svc.db.set_setting("webmail.dlp_mode", "off")
    svc.db.set_setting("webmail.warn_recipients", 2)
    d = wm.create_draft(svc, acc, "admin:adm", {"to_addrs": "a@x.ru, b@x.ru, c@x.ru", "subject": "Всем", "body": "x"})
    with pytest.raises(wm.PresendWarning) as ei:
        wm.send(svc, acc, d["id"], "admin:adm", confirmed=False)
    assert [i["code"] for i in ei.value.items] == ["many"]


def test_api_archived_smime_and_decrypt(api, smtp):
    admin, mb, svc, a, b = api
    own = [m["id"] for m in mb.get(f"/api/webmail/messages?account_id={a}&folder=INBOX").json()["messages"]]
    other = [r["id"] for r in svc.db.query("SELECT id FROM messages WHERE account_id=?", (b,))]
    d = mb.post("/api/webmail/drafts", json={"account_id": a, "mode": "new"}).json()
    r = mb.post(f"/api/webmail/drafts/{d['id']}/archived", json={"message_ids": own[:1]})
    assert r.status_code == 200 and r.json()["items"][0]["name"].endswith(".eml")
    assert mb.post(f"/api/webmail/drafts/{d['id']}/archived", json={"message_ids": other[:1]}).status_code == 404
    mb.put(f"/api/webmail/drafts/{d['id']}", json={"to_addrs": "petrov@x.ru", "subject": "Пересылаю"})
    assert mb.post(f"/api/webmail/drafts/{d['id']}/send").json()["ok"]
    msg = email.message_from_bytes(smtp.sent[-1]["data"], policy=policy.default)
    att = list(msg.iter_attachments())[0]
    assert att.get_content_type() == "message/rfc822"
    # сертификат ящика: загрузка сотрудником, состояние, расшифровка через API
    _k, _c, p12, _pem = _cert("ivanov@x.ru")
    r = mb.post("/api/webmail/smime/key", data={"account_id": str(a), "password": "secret"},
                files={"file": ("me.p12", p12, "application/x-pkcs12")})
    assert r.status_code == 200 and r.json()["present"]
    assert mb.get(f"/api/webmail/smime?account_id={b}").status_code == 403
    st = mb.get(f"/api/webmail/smime/recipients?account_id={a}&emails=petrov@x.ru, ivanov@x.ru").json()
    assert st["can_sign"] and st["missing"] == ["petrov@x.ru"]
    d = mb.post("/api/webmail/drafts", json={"account_id": a, "mode": "new"}).json()
    mb.put(f"/api/webmail/drafts/{d['id']}", json={"to_addrs": "ivanov@x.ru", "subject": "Себе", "body": "<p>Тайна</p>",
                                                   "smime_encrypt": True})
    assert mb.post(f"/api/webmail/drafts/{d['id']}/send").json()["ok"]
    sent = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND folder='Sent' ORDER BY id DESC", (a,))
    got = mb.get(f"/api/webmail/decrypted?account_id={a}&id={sent['id']}").json()
    assert "Тайна" in (got.get("html") or "") + (got.get("text") or "")
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='webmail_decrypt'")
    assert mb.delete("/api/webmail/smime/certs?email=x@y.ru").status_code == 403


def test_ui_needles():
    js = open(os.path.join(os.path.dirname(__file__), "..", "mailarchiver", "web", "static", "js", "app.js"),
              encoding="utf-8").read()
    for needle in ('data-opt="smime_sign"', 'data-opt="smime_encrypt"', "mailPresendConfirm", "archivePickerDlg",
                   "mailSmimeSection", "mailShowDecrypted", "registerProtocolHandler", "mailComposeFromMailto"):
        assert needle in js, needle
    html = open(os.path.join(os.path.dirname(__file__), "..", "mailarchiver", "web", "templates", "index.html"),
                encoding="utf-8").read()
    assert 'rel="manifest"' in html
