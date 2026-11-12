"""Уведомления о новой почте на устройства сотрудника (Web Push, 1.22.0)."""
import base64
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import test_webmail as base

from mailarchiver import webpush

world, box, api = base.world, base.box, base.api
smtp = base.smtp


def _b64d(t):
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


def test_encryption_matches_rfc8291_example():
    """Пример из RFC 8291, раздел 5: те же ключи и соль дают тот же байт в байт результат."""
    from cryptography.hazmat.primitives.asymmetric import ec
    as_key = ec.derive_private_key(int.from_bytes(_b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                   ec.SECP256R1())
    out = webpush.encrypt("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                          "BTBZMqHH6r4Tts7J_aSIgg", b"When I grow up, I want to be a watermelon",
                          salt=_b64d("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_key)
    assert webpush._b64e(out) == (  # noqa: SLF001
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBr"
        "u3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


class PushService:
    """Служба доставки браузера: принимает уведомления, проверяет подпись VAPID и расшифровывает."""

    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.ua = ec.generate_private_key(ec.SECP256R1())
        self.auth = webpush._b64e(b"0123456789abcdef")  # noqa: SLF001
        self.got, self.status = [], 201
        svc = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                svc.got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                self.send_response(svc.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def p256dh(self):
        return webpush._b64e(webpush._raw_public(self.ua.public_key()))  # noqa: SLF001

    def decode(self, item):
        """Проверить заголовки и подпись, вернуть JSON уведомления."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        _path, headers, body = item
        assert headers["content-encoding"] == "aes128gcm" and int(headers["ttl"]) > 0
        auth = headers["authorization"]
        assert auth.startswith("vapid t=")
        token, k = auth[8:].split(", k=")
        head, claims, sig = token.split(".")
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _b64d(k))
        raw = _b64d(sig)
        pub.verify(utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        assert json.loads(_b64d(claims))["aud"] == self.base
        salt, idlen = body[:16], body[20]
        keyid, ct = body[21:21 + idlen], body[21 + idlen:]
        shared = self.ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid))
        hm = lambda key, data: hmac.new(key, data, hashlib.sha256).digest()  # noqa: E731
        ikm = hm(hm(_b64d(self.auth), shared), b"WebPush: info\x00" + _b64d(self.p256dh) + keyid + b"\x01")
        prk = hm(salt, ikm)
        plain = AESGCM(hm(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
            hm(prk, b"Content-Encoding: nonce\x00\x01")[:12], ct, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def push(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    p = PushService()
    yield p
    p.stop()


def _enable(svc):
    svc.db.set_setting("webmail.web_push", True)
    svc.db.set_setting("webmail.web_push_hosts", ["127.0.0.1"])
    svc.db.set_setting("webmail.push", False)           # без IDLE: «Входящие» проверяются по таймеру
    webpush._LAST_POLL.clear()  # noqa: SLF001


def test_subscribe_notify_and_cleanup(api, push, world):
    admin, mb, svc, a, b = api
    sub = {"endpoint": push.base + "/sub/1", "keys": {"p256dh": push.p256dh, "auth": push.auth}}
    # выключено администратором
    assert mb.post("/api/push/state", json={"endpoint": ""}).json()["enabled"] is False
    assert mb.post("/api/push/subscribe", json=sub).status_code == 400
    _enable(svc)
    st = mb.post("/api/push/state", json={"endpoint": ""}).json()
    assert st["enabled"] and len(_b64d(st["public_key"])) == 65 and not st["subscribed"]
    # чужая служба, не https, администратор — отказ
    bad = dict(sub, endpoint="https://evil.example/push")
    assert "не входит в разрешённые" in mb.post("/api/push/subscribe", json=bad).json()["message"]
    assert mb.post("/api/push/subscribe", json=dict(sub, endpoint="http://10.0.0.1/x")).status_code == 400
    assert admin.post("/api/push/subscribe", json=sub).status_code == 403
    assert mb.post("/api/push/subscribe", json=dict(sub, keys={"p256dh": "AAAA", "auth": push.auth})).status_code == 400
    r = mb.post("/api/push/subscribe", json=sub, headers={"User-Agent": "Mozilla/5.0 (Android 14) Chrome/130.0"})
    assert r.status_code == 200 and r.json()["subscribed"] and r.json()["account_ids"] == [a]
    assert r.json()["devices"][0]["device"] == "Chrome, Android"
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='webpush_subscribe'")
    # старые письма не объявляются
    webpush.tick(svc)
    assert push.got == []
    # новое письмо во «Входящих» — одно уведомление с отправителем и темой; прочитанное — нет
    w = world
    w.add("ivanov@x.ru", "INBOX", base._mail("Срочно: счёт", mid="<n1@ext>"))  # noqa: SLF001
    w.add("ivanov@x.ru", "INBOX", base._mail("Уже прочитано", mid="<n2@ext>"), flags=[b"\\Seen"])  # noqa: SLF001
    webpush._LAST_POLL.clear()  # noqa: SLF001
    assert webpush.tick(svc)["messages"] == 1
    assert len(push.got) == 1
    note = push.decode(push.got[0])
    pk = svc.db.query_one("SELECT id FROM messages WHERE account_id=? AND message_id='<n1@ext>'", (a,))["id"]
    assert note["title"] == "client@ext.ru" and note["body"].startswith("Срочно: счёт")
    assert note["url"] == f"/#/mail?open={a}.{pk}"
    webpush._LAST_POLL.clear()  # noqa: SLF001
    webpush.tick(svc)
    assert len(push.got) == 1                                   # повторно не объявляется
    # без отправителя и темы
    svc.db.set_setting("webmail.web_push_preview", False)
    w.add("ivanov@x.ru", "INBOX", base._mail("Тайна", mid="<n3@ext>"))  # noqa: SLF001
    webpush._LAST_POLL.clear()  # noqa: SLF001
    webpush.tick(svc)
    note = push.decode(push.got[-1])
    assert note["title"] == "Новое письмо" and "Тайна" not in json.dumps(note, ensure_ascii=False)
    # проверочное уведомление
    assert mb.post("/api/push/test", json={"endpoint": sub["endpoint"]}).status_code == 200
    assert "работают" in push.decode(push.got[-1])["body"]
    # общий ящик: уведомления, пока доступ открыт
    emp = int(svc.db.get_account(a).employee_id)
    admin.post(f"/api/accounts/{b}/delegates", json={"employee_id": emp, "rights": "read"})
    r = mb.post("/api/push/subscribe", json=dict(sub, account_ids=[a, b, 999]))
    assert sorted(r.json()["account_ids"]) == sorted([a, b])
    admin.delete(f"/api/accounts/{b}/delegates/{emp}")
    w.add("petrov@x.ru", "INBOX", base._mail("Для Петрова", mid="<p9@ext>", to="petrov@x.ru"))  # noqa: SLF001
    n = len(push.got)
    webpush._LAST_POLL.clear()  # noqa: SLF001
    webpush.tick(svc)
    assert len(push.got) == n                                   # доступ снят — молчим
    # служба ответила 410 — подписка удалена
    push.status = 410
    assert mb.post("/api/push/test", json={"endpoint": sub["endpoint"]}).status_code == 400
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0
    # выключение на устройстве
    push.status = 201
    mb.post("/api/push/subscribe", json=sub)
    assert mb.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).json()["subscribed"] is False
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0


def test_service_worker_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "showNotification" in r.text and "notificationclick" in r.text
