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
    # копирование, поставленное в очередь подготовкой, должно закончиться: пока оно идёт, tick «Входящие» не
    # проверяет (иначе тест зависит от того, успела ли очередь)
    import time
    deadline = time.monotonic() + 30
    while svc.db.scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')") and time.monotonic() < deadline:
        time.sleep(0.05)


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


# ---------------------------------------------------------------------------
#  Разбор 16.11 (1.25.0): уведомления прекращаются вместе с доступом
# ---------------------------------------------------------------------------
def _subscribe(mb, push, n=1):
    sub = {"endpoint": push.base + f"/sub/{n}", "keys": {"p256dh": push.p256dh, "auth": push.auth}}
    r = mb.post("/api/push/subscribe", json=sub)
    assert r.status_code == 200, r.text
    return sub


def _new_mail(svc, world, subject, mid):
    world.add("ivanov@x.ru", "INBOX", base._mail(subject, mid=mid))  # noqa: SLF001
    webpush._LAST_POLL.clear()  # noqa: SLF001
    return webpush.tick(svc)


def test_dismissed_employee_gets_no_push(api, push, world):
    """Уволенному — ни уведомлений, ни сеансов; подписаться снова нельзя."""
    from mailarchiver.employees import dismiss_employee
    admin, mb, svc, a, b = api
    _enable(svc)
    svc.db.set_setting("employees.dismissed_action", "keep")          # копирование продолжается
    sub = _subscribe(mb, push)
    emp = int(svc.db.get_account(a).employee_id)
    res = dismiss_employee(svc, emp, by="admin", reason="тест")
    assert res["devices_off"] == 1 and res["sessions_closed"] >= 1
    assert mb.get(f"/api/webmail/status?account_id={a}").status_code == 401
    _new_mail(svc, world, "После увольнения", "<d1@ext>")
    assert push.got == []
    detail = svc.db.query_one("SELECT detail FROM audit WHERE action='employee_dismissed'")["detail"]
    assert "уведомления выключены на устройствах: 1" in detail
    # подписка уволенного, оставшаяся в базе (например, прежней версии), тоже не срабатывает
    svc.db.execute("INSERT INTO push_subscriptions(endpoint, p256dh, auth, login_account_id, account_ids, created_at, "
                   "employee_id, seen_at) VALUES(?,?,?,?,?,?,?,?)",
                   (sub["endpoint"], push.p256dh, push.auth, a, json.dumps([a]), "2026-11-16T00:00:00+00:00", emp,
                    "2026-11-16T00:00:00+00:00"))
    _new_mail(svc, world, "Ещё одно", "<d2@ext>")
    assert push.got == [] and svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0


def _mb_client(admin, svc, account_id, token):
    from fastapi.testclient import TestClient

    from mailarchiver.security import sign_value
    from mailarchiver.web.auth import COOKIE_NAME
    svc.db.create_session(token, 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=account_id)
    c = TestClient(admin.app, headers=base.HDR)
    c.cookies.set(COOKIE_NAME, sign_value(token, svc.cfg.secret_key()))
    return c


def test_relinked_mailbox_and_logout_sessions_stop_push(api, push, world):
    admin, mb, svc, a, b = api
    _enable(svc)
    _subscribe(mb, push)
    r = admin.post(f"/api/accounts/{a}/logout-sessions").json()
    assert r["closed"] >= 1 and r["devices"] == 1
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0
    assert "уведомления выключены на устройствах: 1" in svc.db.query_one(
        "SELECT detail FROM audit WHERE action='account_logout_sessions'")["detail"]
    # снова вошёл и подписался — ящик передан другому сотруднику
    mb2 = _mb_client(admin, svc, a, "mbtok2")
    _subscribe(mb2, push, 2)
    other = svc.db.create_employee(full_name="Сидоров Сидор", email="sidorov@x.ru")
    svc.db.link_employee_account(other, a)
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0
    _new_mail(svc, world, "Уже для Сидорова", "<r1@ext>")
    assert push.got == []
    # отвязка ящика — то же
    mb3 = _mb_client(admin, svc, a, "mbtok3")
    _subscribe(mb3, push, 3)
    svc.db.unlink_employee_account(a)
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0


def test_push_owner_checked_on_every_send(api, push, world):
    """Ящик передан в обход ``link_employee_account`` (прямо в базе) — подписка всё равно не срабатывает."""
    admin, mb, svc, a, b = api
    _enable(svc)
    _subscribe(mb, push)
    other = svc.db.create_employee(full_name="Сидоров Сидор", email="sidorov@x.ru")
    svc.db.execute("UPDATE accounts SET employee_id=? WHERE id=?", (other, a))
    _new_mail(svc, world, "Чужое", "<o1@ext>")
    assert push.got == [] and svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0


def test_push_idle_device_expires_and_login_refreshes(api, push, world):
    admin, mb, svc, a, b = api
    _enable(svc)
    _subscribe(mb, push)
    svc.db.execute("UPDATE push_subscriptions SET seen_at='2026-01-01T00:00:00+00:00'")
    # вход сотрудника паролем ящика обновляет отметку — подписка жива
    from mailarchiver.web.auth import create_session
    from starlette.requests import Request
    from starlette.responses import Response
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1),
                   "scheme": "http", "query_string": b"", "server": ("t", 80)})
    create_session(svc, Response(), 0, req, role="mailbox", account_id=a)
    assert svc.db.scalar("SELECT seen_at FROM push_subscriptions") > "2026-11-01"
    assert _new_mail(svc, world, "Свежее", "<i1@ext>")["messages"] == 1 and len(push.got) == 1
    # давно не входил — удалена
    svc.db.execute("UPDATE push_subscriptions SET seen_at='2026-01-01T00:00:00+00:00'")
    _new_mail(svc, world, "Позже", "<i2@ext>")
    assert len(push.got) == 1 and svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 0
    # 0 — по сроку не удаляется
    svc.db.set_setting("webmail.web_push_idle_days", 0)
    _subscribe(mb, push, 4)
    svc.db.execute("UPDATE push_subscriptions SET seen_at='2020-01-01T00:00:00+00:00'")
    _new_mail(svc, world, "Совсем позже", "<i3@ext>")
    assert len(push.got) > 1 and push.decode(push.got[-1])["body"].startswith("Совсем позже")


def test_disabled_mailbox_and_broken_key_keep_data(api, push, world):
    admin, mb, svc, a, b = api
    _enable(svc)
    _subscribe(mb, push)
    # ключ VAPID не расшифровывается — подписки и ключ остаются, ошибка понятная
    stored = svc.db.get_meta("webpush_vapid")
    svc.db.set_meta("webpush_vapid", "gAAAAAbroken")
    st = mb.post("/api/push/state", json={"endpoint": ""})
    assert st.status_code == 400 and "не расшифровывается" in st.json()["message"]
    assert svc.db.scalar("SELECT COUNT(*) FROM push_subscriptions") == 1
    assert svc.db.get_meta("webpush_vapid") == "gAAAAAbroken"
    svc.db.set_meta("webpush_vapid", stored)
    # создание ключа заново — подписки удаляются, ключ новый
    import types
    assert webpush.reset_key(types.SimpleNamespace(db=svc.db)) == 1
    assert svc.db.get_meta("webpush_vapid") not in (stored, "gAAAAAbroken")
