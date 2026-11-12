"""API-токены и уведомления в Telegram и на вебхук (1.20.0)."""
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from mailarchiver import notify

PW = "Sw0rdfish!"


def _login(client, user="admin", pw=PW):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": user, "password": pw})


def _bare(client):
    """Клиент без cookie и без X-Requested-With — как внешний скрипт."""
    from fastapi.testclient import TestClient
    return TestClient(client.app)


def test_token_lifecycle(client):
    _login(client)
    r = client.post("/api/tokens", json={"name": "Zabbix", "role": "operator", "days": 30})
    assert r.status_code == 200, r.text
    token, tid = r.json()["token"], r.json()["id"]
    assert token.startswith("ma_")
    bare = _bare(client)
    auth = {"Authorization": f"Bearer {token}"}
    assert bare.get("/api/state").status_code == 401
    assert bare.get("/api/state", headers=auth).status_code == 200
    # оператор: настройки и чтение писем — нет; токены, пароль и пользователи — нет никогда
    assert bare.get("/api/settings", headers=auth).status_code == 403
    assert bare.get("/api/tokens", headers=auth).status_code == 403
    me = bare.get("/api/me", headers=auth).json()                # «кто я» — можно
    assert me["via_token"] and me["role"] == "operator"
    assert bare.post("/api/me/2fa/setup", headers=auth).status_code == 403
    # меняющий запрос без Origin и без X-Requested-With проходит с токеном (это не форма браузера)
    acc = client.post("/api/accounts", json={"name": "Ящик", "host": "imap.example.com", "port": 993,
                                             "username": "u@example.com", "password": "secret", "security": "ssl",
                                             "auth_type": "password"}).json()["id"]
    r = bare.post(f"/api/accounts/{acc}/backup", headers=auth)
    assert r.status_code in (200, 400), r.text
    items = client.get("/api/tokens").json()["items"]
    assert items[0]["last_used_at"] and items[0]["hint"].startswith("ma_")
    assert token not in client.get("/api/tokens").text and token[10:30] not in client.get("/api/tokens").text
    # отзыв действует сразу
    assert client.delete(f"/api/tokens/{tid}").status_code == 200
    assert bare.get("/api/state", headers=auth).status_code == 401
    assert client.delete("/api/tokens/9999").status_code == 404
    actions = {r["action"] for r in client.get("/api/audit").json()}
    assert {"token_create", "token_revoke"} <= actions


def test_admin_token_and_bad_tokens(client):
    _login(client)
    token = client.post("/api/tokens", json={"name": "CI", "role": "admin", "days": 0}).json()["token"]
    bare = _bare(client)
    assert bare.get("/api/settings", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert bare.get("/api/state", headers={"Authorization": "Bearer ma_wrong"}).status_code == 401
    assert client.post("/api/tokens", json={"name": "", "role": "admin"}).status_code == 400
    assert client.post("/api/tokens", json={"name": "x", "role": "mailbox"}).status_code == 400
    # действия с токеном подписаны в аудите
    bare.post("/api/search/reindex", headers={"Authorization": f"Bearer {token}"})
    users = {r["user"] for r in client.get("/api/audit").json()}
    assert any("токен «CI»" in u for u in users)


class _Hook(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Hook.got.append((self.path, dict(self.headers), body))
        payload = b'{"ok": true}' if "/sendMessage" in self.path else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture()
def hook():
    _Hook.got = []
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_webhook_json_and_signature(services, hook, monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    svc = services
    svc.set_rt("notifications", "webhook_url", hook + "/hook")
    svc.set_rt("notifications", "webhook_secret", "s3cret")
    res = svc.notifier.send_all("Тема", "Текст")
    assert res == {"webhook": ""}
    path, headers, body = _Hook.got[-1]
    doc = json.loads(body)
    assert doc["subject"] == "Тема" and doc["text"] == "Текст" and doc["event"] == "notification"
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert {k.lower(): v for k, v in headers.items()}.get("x-mailarchiver-signature") == "sha256=" + sig
    svc.set_rt("notifications", "webhook_format", "slack")
    svc.notifier.send("Тема", "Текст")
    assert json.loads(_Hook.got[-1][2]) == {"text": "*Тема*\nТекст"}


def test_telegram_and_failures(services, hook, monkeypatch):
    svc = services
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    svc.set_rt("notifications", "telegram_bot_token", "123:ABC")
    svc.set_rt("notifications", "telegram_chat_id", "-100500")
    # адрес API только https — локальный http отвергается, а токен не попадает в текст ошибки
    svc.set_rt("notifications", "telegram_api_url", hook)
    res = svc.notifier.send_all("Тема", "x" * 5000)
    assert res["telegram"] and "123:ABC" not in res["telegram"]
    # отправка «как в Telegram» через подменённый адрес: проверяем запрос и обрезку длинного текста
    monkeypatch.setattr(notify, "_post", lambda url, payload, headers, secret_in_url="": (
        _Hook.got.append((url, headers, payload)) or b'{"ok": true}'))
    svc.set_rt("notifications", "telegram_api_url", "https://api.telegram.org")
    assert svc.notifier.send_all("Тема", "x" * 5000) == {"telegram": ""}
    url, _, payload = _Hook.got[-1]
    doc = json.loads(payload)
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage" and doc["chat_id"] == "-100500"
    assert len(doc["text"]) <= notify.TELEGRAM_MAX
    with pytest.raises(RuntimeError):
        svc.notifier.send_email("x", "y")                    # SMTP не настроен


def test_notifications_test_endpoint(client):
    _login(client)
    r = client.post("/api/notifications/test")
    assert r.status_code == 200 and r.json()["results"] == {}
