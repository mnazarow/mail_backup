"""Картинки cid: в просмотрщике писем и журнал безопасности (блокировки, снятие, уведомления)."""
import base64

from mailarchiver.mailview import MAX_INLINE_IMAGE, parse_message
from mailarchiver.web import auth as auth_mod

PW = "Sw0rdfish!1"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a2e40000000049454e44ae426082")


def _related(html, parts):
    body = ["From: a@x\r\nTo: b@x\r\nSubject: cid\r\nMIME-Version: 1.0\r\n"
            'Content-Type: multipart/related; boundary="R"\r\n\r\n'
            f"--R\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{html}\r\n"]
    for ctype, cid, data, disp in parts:
        body.append(f"--R\r\nContent-Type: {ctype}\r\nContent-ID: <{cid}>\r\n{disp}"
                    f"Content-Transfer-Encoding: base64\r\n\r\n{base64.b64encode(data).decode()}\r\n")
    body.append("--R--\r\n")
    return "".join(body).encode()


def test_cid_images_are_embedded():
    raw = _related('<img src="cid:logo@x"><img src=cid:none@x>'
                   '<div style="background:url(\'cid:LOGO@X\')"></div>'
                   '<img src="cid:vector@x"><img src="cid:big@x">',
                   [("image/png", "logo@x", PNG, 'Content-Disposition: inline; filename="logo.png"\r\n'),
                    ("image/svg+xml", "vector@x", b"<svg onload='alert(1)'/>", ""),
                    ("image/png", "big@x", b"\0" * (MAX_INLINE_IMAGE + 10), 'Content-Disposition: inline; filename="big.png"\r\n')])
    m = parse_message(raw)
    assert m["inline_images"] == 1
    assert '<img src="data:image/png;base64,' in m["html"]
    assert "url(data:image/png;base64," in m["html"]              # без кавычек — style="…" не рвётся
    assert "cid:none@x" in m["html"] and "cid:vector@x" in m["html"] and "cid:big@x" in m["html"]
    names = {a["filename"]: a["inline"] for a in m["attachments"]}
    assert names["logo.png"] is True and names["big.png"] is False


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_blocks_are_listed_and_can_be_lifted(client):
    _login(client)
    svc = client.app.state.services
    db = svc.db
    for _ in range(5):
        db.record_login_attempt("victim", False, "10.0.0.7")
    for i in range(50):
        db.record_login_attempt(f"user{i}", False, "10.0.0.66")
    for _ in range(11):
        db.record_login_attempt("admin2", False, "10.0.0.8", kind="otp")
    for _ in range(11):
        db.record_login_attempt("ivanov@example.ru", False, "10.0.0.9", kind="imap")
    data = client.get("/api/security").json()
    kinds = {(b["kind"], b["username"], b["ip"]) for b in data["blocks"]}
    assert ("pair", "victim", "10.0.0.7") in kinds
    assert ("ip", "", "10.0.0.66") in kinds
    assert ("otp", "admin2", "") in kinds
    assert ("imap", "ivanov@example.ru", "") in kinds
    assert data["top_ips"][0]["ip"] == "10.0.0.66"
    r = client.post("/api/security/unblock", json={"kind": "pair", "username": "VICTIM", "ip": "10.0.0.7"}).json()
    assert r["removed"] == 5
    kinds = {(b["kind"], b["username"]) for b in client.get("/api/security").json()["blocks"]}
    assert ("pair", "victim") not in kinds and ("otp", "admin2") in kinds
    assert client.post("/api/security/unblock", json={"kind": "weird"}).status_code in (400, 401)
    assert any(e["action"] == "security_unblock" for e in client.get("/api/security").json()["events"])


def test_otp_attack_notice_is_not_repeated_after_restart(services, monkeypatch):
    sent = []
    monkeypatch.setattr(services.notifier, "notify_job_async", lambda *a: sent.append(a))
    auth_mod._notify_otp_attack(services, "Admin", "10.0.0.1")
    auth_mod._notify_otp_attack(services, "admin", "10.0.0.2")
    assert len(sent) == 1
    from mailarchiver.service import Services
    restarted = Services(services.cfg)            # «после перезапуска» — та же база
    monkeypatch.setattr(restarted.notifier, "notify_job_async", lambda *a: sent.append(a))
    auth_mod._notify_otp_attack(restarted, "admin", "10.0.0.3")
    assert len(sent) == 1
