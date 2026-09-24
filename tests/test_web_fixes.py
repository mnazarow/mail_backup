# -*- coding: utf-8 -*-
"""Веб-слой 1.3.0: исправления по итогам ревизии (формы, расписания, сессии, ошибки)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

HDR = {"X-Requested-With": "fetch"}
PW = "Sw0rdfish!"


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def _account(client, **extra):
    body = {"name": "Box", "host": "imap.example.com", "port": 993,
            "username": "u@example.com", "password": "secret"}
    body.update(extra)
    r = client.post("/api/accounts", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_account_update_keeps_fields_not_sent(client):
    """Поля, которых нет в запросе, не обнуляются (срок хранения, папки, заметки, OAuth2)."""
    _login(client)
    aid = _account(client, auth_type="oauth2", oauth_client_id="CLIENT-123",
                   oauth_token_url="https://oauth2.googleapis.com/token",
                   folder_include=["INBOX"], notes="важный ящик", retention_days=14)
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["oauth_client_id"] == "CLIENT-123" and acc["oauth_token_url"].startswith("https://")
    # правка «старым» клиентом: только основные поля
    r = client.put(f"/api/accounts/{aid}", json={"name": "Box 2", "host": "imap.example.com", "port": 993,
                                                  "username": "u@example.com", "auth_type": "oauth2"})
    assert r.status_code == 200, r.text
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["name"] == "Box 2" and acc["retention_days"] == 14
    assert acc["folder_include"] == ["INBOX"] and acc["notes"] == "важный ящик"
    assert acc["oauth_client_id"] == "CLIENT-123", "Client ID не должен стираться при сохранении"
    # пустые OAuth-поля у ящика OAuth2 тоже не затирают сохранённые
    r = client.put(f"/api/accounts/{aid}", json={"name": "Box 2", "host": "imap.example.com", "port": 993,
                                                  "username": "u@example.com", "auth_type": "oauth2",
                                                  "oauth_client_id": "", "oauth_token_url": ""})
    acc = client.get(f"/api/accounts/{aid}").json()
    assert acc["oauth_client_id"] == "CLIENT-123" and acc["oauth_token_url"]


def test_account_values_are_validated(client):
    _login(client)
    for bad in ({"port": 70000}, {"retention_days": -5}, {"retention_days": 10 ** 9}, {"name": "x" * 300}):
        body = {"name": "Box", "host": "h", "port": 993, "username": "u", "password": "p"}
        body.update(bad)
        assert client.post("/api/accounts", json=body).status_code == 400, bad


def test_huge_numbers_give_400_not_500(client):
    _login(client)
    for path in ("/api/jobs/100000000000000000000", "/api/accounts/100000000000000000000",
                 "/api/exports/100000000000000000000/download",
                 "/api/accounts/1/messages?offset=100000000000000000000",
                 "/api/jobs?limit=100000000000000000000", "/api/audit?limit=100000000000000000000"):
        r = client.get(path)
        assert r.status_code in (200, 400, 404), (path, r.status_code)
    r = client.post("/api/accounts/1/retention", json={"days": 10 ** 20})
    assert r.status_code in (400, 404)


def test_create_user_strips_name(client):
    _login(client)
    assert client.post("/api/users", json={"username": "   ", "password": "ДлинныйПароль1"}).status_code == 400
    assert client.post("/api/users", json={"username": " admin", "password": "ДлинныйПароль1"}).status_code == 400
    r = client.post("/api/users", json={"username": " second ", "password": "ДлинныйПароль1"})
    assert r.status_code == 200
    assert any(u["username"] == "second" for u in client.get("/api/users").json())


def test_long_login_is_rejected_with_russian_message(client):
    r = client.post("/api/login", json={"username": "x" * 5000, "password": "p"})
    assert r.status_code == 422
    assert "слишком длинное" in r.json()["message"]
    # в аудит и журнал попыток такой логин не попал
    svc = client.app.state.services
    assert svc.db.scalar("SELECT COUNT(*) FROM login_attempts WHERE length(username) > 400") == 0


def test_schedule_rules(client):
    _login(client)
    svc = client.app.state.services
    a1, a2 = _account(client), _account(client, name="Второй", username="v@example.com")
    # нельзя: выгрузка по расписанию и backup с «пересозданием»
    bad = client.post("/api/schedules", json={"account_id": a1, "job_type": "export", "cron_expr": "0 3 * * *"})
    assert bad.status_code == 400
    r = client.post("/api/schedules", json={"account_id": a1, "job_type": "backup", "cron_expr": "0 3 * * *",
                                            "options": {"rebuild": "full"}})
    sid = r.json()["id"]
    assert svc.db.get_schedule(sid)["options"] in ("{}", "", None)
    # восстановление по расписанию: параметры сохраняются при правке без options
    r = client.post("/api/schedules", json={"account_id": a1, "job_type": "restore", "cron_expr": "0 4 * * *",
                                            "options": {"target_mode": "prefixed", "target_prefix": "Восст"}})
    rid = r.json()["id"]
    r = client.put(f"/api/schedules/{rid}", json={"account_id": a2, "job_type": "restore", "kind": "cron",
                                                  "cron_expr": "0 5 * * *", "enabled": False})
    assert r.status_code == 200, r.text
    row = svc.db.get_schedule(rid)
    assert row["account_id"] == a2, "смена ящика в форме должна сохраняться"
    assert "Восст" in row["options"] and row["cron_expr"] == "0 5 * * *" and not row["enabled"]
    audit = [a["action"] for a in client.get("/api/audit").json()]
    assert "schedule_update" in audit
    # интервал больше года — отказ
    assert client.post("/api/schedules", json={"account_id": a1, "kind": "interval", "job_type": "backup",
                                               "interval_seconds": 400 * 86400}).status_code == 400


def test_live_endpoint_and_background_requests_do_not_extend_session(client):
    _login(client)
    svc = client.app.state.services
    live = client.get("/api/live", headers={"X-MA-Background": "1"})
    assert live.status_code == 200 and "active_jobs" in live.json() and "job_counts" in live.json()
    token = svc.db.scalar("SELECT token FROM sessions ORDER BY rowid DESC LIMIT 1")
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    svc.db.execute("UPDATE sessions SET last_seen=? WHERE token=?", (old, token))
    assert client.get("/api/live", headers={"X-MA-Background": "1"}).status_code == 200
    assert svc.db.scalar("SELECT last_seen FROM sessions WHERE token=?", (token,)) == old
    assert client.get("/api/me").status_code == 200
    assert svc.db.scalar("SELECT last_seen FROM sessions WHERE token=?", (token,)) != old


def test_settings_readonly_and_new_keys(client):
    _login(client)
    data = client.get("/api/settings").json()
    assert "server.host" in data["readonly"] and "server.port" in data["readonly"]
    assert "enabled" in data["values"]["retention"] and "cron" in data["values"]["retention"]
    assert "keep_days" in data["values"]["export"]
    assert "per_account_concurrency" not in data["values"]["backup"]
    assert "outlook_target" not in data["values"]["export"]
    assert client.put("/api/settings", json={"values": {"backup.max_concurrent_jobs": 32}}).status_code == 400


def test_employee_sync_is_not_duplicated(client, tmp_path):
    _login(client)
    src = tmp_path / "emp.csv"
    src.write_text("ФИО;Email\nИванов;i@x.ru\n", encoding="utf-8")
    svc = client.app.state.services
    svc.queue.stop()                      # задания остаются в очереди
    assert client.put("/api/settings", json={"values": {"employees.source_file": str(src)}}).status_code == 200
    first = client.post("/api/employees/sync").json()
    second = client.post("/api/employees/sync").json()
    assert second.get("already") and second["job_id"] == first["job_id"]


def test_mailbox_user_does_not_see_admin_notes(client):
    _login(client)
    svc = client.app.state.services
    aid = _account(client, notes="секретная заметка")
    svc.db.create_session("mbtok", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    from mailarchiver.security import sign_value
    from mailarchiver.web.auth import COOKIE_NAME
    mb = TestClient(client.app, headers=HDR)
    mb.cookies.set(COOKIE_NAME, sign_value("mbtok", svc.cfg.secret_key()))
    for acc in mb.get("/api/accounts").json() + mb.get("/api/state").json()["accounts"]:
        assert "notes" not in acc and "oauth_client_id" not in acc
    assert "notes" not in mb.get(f"/api/accounts/{aid}").json()


def test_dns_rebinding_guard_for_setup(client):
    """Первичная настройка с чужого имени (DNS rebinding) отклоняется, по IP — проходит."""
    body = {"username": "admin", "password": PW}
    evil = client.post("/api/setup", json=body, headers={"Host": "evil.example.com",
                                                         "Origin": "http://evil.example.com"})
    assert evil.status_code == 421 and evil.json()["code"] == "bad_host"
    assert client.get("/api/needs-setup").json()["needs_setup"] is True
    ok = client.post("/api/setup", json=body, headers={"Host": "192.168.1.10:8493",
                                                       "Origin": "http://192.168.1.10:8493"})
    assert ok.status_code == 200


def test_host_is_trusted_rules():
    from mailarchiver.web.proxy import host_is_trusted
    assert host_is_trusted("127.0.0.1:8493") and host_is_trusted("[::1]:8493")
    assert host_is_trusted("localhost:8493") and host_is_trusted("backup")
    assert not host_is_trusted("evil.example.com")
    assert host_is_trusted("backup.example.ru", "https://backup.example.ru")
    assert not host_is_trusted("evil.example.com", "https://backup.example.ru")


def test_no_auth_mode_rejects_foreign_host(client):
    _login(client)
    assert client.put("/api/settings", json={"values": {"security.auth_enabled": False}}).status_code == 200
    try:
        assert client.get("/api/state", headers={"Host": "evil.example.com"}).status_code == 421
        assert client.get("/api/state").status_code == 200
    finally:
        client.put("/api/settings", json={"values": {"security.auth_enabled": True}})


def test_admin_can_close_mailbox_sessions(client):
    _login(client)
    svc = client.app.state.services
    aid = _account(client)
    svc.db.create_session("mb1", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    r = client.post(f"/api/accounts/{aid}/logout-sessions")
    assert r.status_code == 200 and r.json()["closed"] == 1
    # новый пароль ящика тоже завершает сеансы сотрудника
    svc.db.create_session("mb2", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    acc = client.get(f"/api/accounts/{aid}").json()
    body = {k: acc[k] for k in ("name", "host", "port", "username", "security", "auth_type")}
    body["password"] = "новый-пароль"
    assert client.put(f"/api/accounts/{aid}", json=body).json()["sessions_closed"] == 1
