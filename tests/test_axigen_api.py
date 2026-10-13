"""REST API раздела «Сервер Axigen»: права, настройки, проверка, сверка, учётные записи, консоль, журнал.

Нужен настоящий FastAPI (фикстура client); серверы Axigen — учебные (tests/axigen_fake.py).
"""
import time

import pytest

from axigen_fake import FakeAxigenCLI, FakeAxigenFTP, FtpWorld, World, message
from mailarchiver import models
from mailarchiver.models import AuthType

PW = "Sw0rdfish!"
ADMIN_PW = "Adm1nPass"


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


@pytest.fixture()
def servers():
    world = World()
    fworld = FtpWorld({"domains": {"company.ru": {
        "ivanov": {"Inbox": {"1.eml": message("Первое")}}, "petrov": {"Inbox": {}}, "postmaster": {"Inbox": {}}}}})
    with FakeAxigenCLI(world) as cli, FakeAxigenFTP(fworld) as ftp:
        yield type("S", (), {"world": world, "fworld": fworld, "cli": cli, "ftp": ftp})


def _configure(client, s, **extra):
    values = {"axigen.enabled": True, "axigen.host": "127.0.0.1", "axigen.cli_port": s.cli.port,
              "axigen.ftp_port": s.ftp.port, "axigen.admin_user": "admin", "axigen.admin_password": ADMIN_PW,
              "axigen.timeout_s": 5, "axigen.imap_host": "mx.company.ru"}
    values.update(extra)
    r = client.put("/api/settings", json={"values": values})
    assert r.status_code == 200, r.text


def _wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("success", "partial", "failed", "cancelled"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"задание {job_id} не завершилось")


def test_section_is_admin_only_and_hidden_until_enabled(client):
    assert client.get("/api/axigen/status").status_code == 401
    _login(client)
    assert client.get("/api/me").json()["features"]["axigen"] is False
    st = client.get("/api/axigen/status").json()
    assert st["enabled"] is False and st["problems"] == []
    assert client.post("/api/axigen/sync").status_code == 400


def test_settings_validation_and_secrets(client, servers):
    _login(client)
    bad = client.put("/api/settings", json={"values": {"axigen.host": "https://mx.company.ru"}})
    assert bad.status_code == 400
    _configure(client, servers)
    assert client.get("/api/me").json()["features"]["axigen"] is True
    values = client.get("/api/settings").json()
    assert values["values"]["axigen"]["admin_password"] == ""
    assert values["secrets_set"]["axigen.admin_password"]
    # пароль администратора не уходит на новый адрес без повторного ввода
    moved = client.put("/api/settings", json={"values": {"axigen.host": "10.9.9.9"}})
    assert moved.status_code == 400 and "введите пароль заново" in moved.json()["message"]
    ok = client.put("/api/settings", json={"values": {"axigen.host": "10.9.9.9", "axigen.admin_password": "x"}})
    assert ok.status_code == 200


def test_check_sync_accounts_and_mailboxes(client, servers):
    _login(client)
    _configure(client, servers)
    check = client.post("/api/axigen/check").json()
    assert check["ok"] and check["cli"]["version"].startswith("10.6.30") and check["ftp"]["ok"]
    assert ADMIN_PW not in str(check)
    started = client.post("/api/axigen/sync").json()
    job = _wait_job(client, started["job_id"])
    assert job["status"] == "success", job
    data = client.get("/api/axigen/accounts").json()
    assert sorted(a["email"] for a in data["accounts"]) == ["ivanov@company.ru", "petrov@company.ru",
                                                            "postmaster@company.ru"]
    pv = client.post("/api/axigen/mailboxes", json={"emails": ["ivanov@company.ru"], "preview": True}).json()
    assert pv["would_create"] == 1 and pv["created"] == 0
    done = client.post("/api/axigen/mailboxes", json={"emails": ["ivanov@company.ru"], "preview": False}).json()
    assert done["created"] == 1
    acc = next(a for a in client.get("/api/accounts").json() if a["username"] == "ivanov@company.ru")
    assert acc["auth_type"] == AuthType.AXIGEN_FTP and acc["has_password"] and not acc["imap_login"]
    info = client.get(f"/api/axigen/mailbox/{acc['id']}").json()
    assert info["row"]["email"] == "ivanov@company.ru"
    test = client.post(f"/api/accounts/{acc['id']}/test").json()
    assert test["ok"] and test["via"] == "axigen_ftp" and test["path"] == "/domains/company.ru/ivanov"
    xlsx = client.get("/api/axigen/accounts/export?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    status = client.get("/api/axigen/status").json()
    assert status["counts"]["accounts"] == 3 and status["last_sync"]["status"] == "success"


def test_console_diagnostics_queue_and_log(client, servers):
    _login(client)
    _configure(client, servers)
    res = client.post("/api/axigen/console", json={"commands": ["LIST Domains"]}).json()
    assert "company.ru" in res["replies"][0]["text"]
    refused = client.post("/api/axigen/console", json={"commands": ["UPDATE Domain name company.ru", "COMMIT"]})
    assert refused.status_code == 400
    diag = client.get("/api/axigen/diagnostics?email=ivanov@company.ru").json()
    assert any(s["title"] == "Папки" for s in diag["sections"])
    assert client.get("/api/axigen/queue").json()["count"] == 1
    items = client.get("/api/axigen/log?kind=console").json()["items"]
    assert items and items[0]["kind"] == "console"
    full = client.get(f"/api/axigen/log/{items[0]['id']}").json()
    assert "> LIST Domains" in full["transcript"]
    assert client.get("/api/axigen/log?kind=nonsense").status_code == 400


def test_server_changes_need_permission(client, servers):
    _login(client)
    _configure(client, servers)
    r = client.post("/api/axigen/accounts/create", json={"email": "new@company.ru", "generate": True})
    assert r.status_code == 400 and "не разрешены" in r.json()["message"]
    _configure(client, servers, **{"axigen.allow_changes": True})
    created = client.post("/api/axigen/accounts/create",
                          json={"email": "new@company.ru", "generate": True, "first_name": "Новый"}).json()
    assert created["ok"] and len(created["password"]) == 20
    assert servers.world.domains["company.ru"].accounts["new"].password == created["password"]
    pw = client.post("/api/axigen/accounts/password", json={"email": "new@company.ru", "password": "Br4ndNew1"}).json()
    assert pw["ok"] and servers.world.domains["company.ru"].accounts["new"].password == "Br4ndNew1"
    audit = [a["action"] for a in client.get("/api/audit").json()]
    assert "axigen_create" in audit and "axigen_password" in audit


def test_restore_through_ftp_needs_admin_and_permission(client, servers):
    _login(client)
    _configure(client, servers)
    svc = client.app.state.services
    acc = svc.db.create_account(models.Account(name="Иванов", host="mx.company.ru", username="ivanov@company.ru",
                                               password="", auth_type=AuthType.AXIGEN_FTP))
    body = {"target_mode": "prefixed", "target_prefix": "Восстановлено", "via": "ftp"}
    r = client.post(f"/api/accounts/{acc}/restore", json=body)
    assert r.status_code == 400 and "не разрешены" in r.json()["message"]
    _configure(client, servers, **{"axigen.allow_changes": True})
    r = client.post(f"/api/accounts/{acc}/restore", json=body)
    assert r.status_code == 200, r.text
    assert client.post(f"/api/accounts/{acc}/restore", json={**body, "via": "smtp"}).status_code == 400


def test_trust_checks_the_certificate_again(client, servers):
    _login(client)
    _configure(client, servers)
    r = client.post("/api/axigen/trust", json={"which": "cli", "fingerprint": "ab" * 32})
    assert r.status_code == 400 and "пароль" in r.json()["message"]
    r = client.post("/api/axigen/trust", json={"which": "cli", "fingerprint": "ab" * 32, "password": ADMIN_PW})
    assert r.status_code == 400 and "SSL" in r.json()["message"]
    assert client.post("/api/axigen/trust", json={"which": "x", "fingerprint": "ab" * 32,
                                                  "password": ADMIN_PW}).status_code == 400


def test_weakening_the_connection_needs_the_password_again(client, servers):
    _login(client)
    _configure(client, servers, **{"axigen.cli_tls": "tls", "axigen.host": "10.9.9.9"})
    r = client.put("/api/settings", json={"values": {"axigen.cli_tls": "none", "axigen.allow_plaintext": True}})
    assert r.status_code == 400 and "введите пароль администратора Axigen" in r.json()["message"]
    r = client.put("/api/settings", json={"values": {"axigen.cli_tls": "none", "axigen.allow_plaintext": True,
                                                     "axigen.admin_password": "new"}})
    assert r.status_code == 200
