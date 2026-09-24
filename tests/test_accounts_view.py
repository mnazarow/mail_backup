"""Раздел «Почтовые ящики»: проверка паролей всех ящиков, отбор и даты резервных копий."""
import time

from mailarchiver.errors import ImapAuthError, ImapConnectionError
from mailarchiver.imap import client as client_mod


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def _account(client, name, username, password="pw", enabled=True):
    r = client.post("/api/accounts", json={
        "name": name, "host": "mx.example.ru", "port": 993, "username": username,
        "password": password, "security": "ssl", "enabled": enabled})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _wait_job(client, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("success", "failed", "partial", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("задание не завершилось")


class _FakeConn:
    """Вместо IMAP: пароль «good» подходит, «bad» — нет, у сервера «down» нет связи."""

    def __init__(self, account, options=None):
        self.account = account

    def connect(self):
        if self.account.password == "bad":
            raise ImapAuthError("Не удалось войти в почтовый ящик: сервер отклонил учётные данные.")
        if self.account.password == "down":
            raise ImapConnectionError("Не удалось соединиться с IMAP-сервером")

    def close(self, *, force=False):
        pass


def test_check_logins_marks_wrong_passwords(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(client_mod, "ImapConnection", _FakeConn)
    good = _account(client, "Хороший", "good@x.ru", "good")
    bad = _account(client, "Плохой", "bad@x.ru", "bad")
    down = _account(client, "Без связи", "down@x.ru", "down")
    nopw = _account(client, "Без пароля", "nopw@x.ru", "")

    r = client.post("/api/accounts/check-logins", json={})
    assert r.status_code == 200
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "partial"
    counts = job["result"]["counts"]
    assert counts["ok"] == 1 and counts["auth_error"] == 1
    assert counts["conn_error"] == 1 and counts["no_password"] == 1

    accs = {a["id"]: a for a in client.get("/api/accounts").json()}
    assert accs[good]["login_status"] == "ok"
    assert accs[bad]["login_status"] == "auth_error" and accs[bad]["login_checked_at"]
    assert accs[down]["login_status"] == "conn_error"
    assert accs[nopw]["login_status"] == "no_password"


def test_check_logins_only_selected_accounts(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(client_mod, "ImapConnection", _FakeConn)
    a = _account(client, "А", "a@x.ru", "bad")
    b = _account(client, "Б", "b@x.ru", "bad")
    r = client.post("/api/accounts/check-logins", json={"account_ids": [a]})
    _wait_job(client, r.json()["job_id"])
    accs = {x["id"]: x for x in client.get("/api/accounts").json()}
    assert accs[a]["login_status"] == "auth_error"
    assert accs[b]["login_status"] == ""                # не проверялся


def test_accounts_list_has_backup_dates(client):
    _login(client)
    svc = client.app.state.services
    with_copy = _account(client, "С копией", "c@x.ru")
    without = _account(client, "Без копии", "n@x.ru")
    svc.db.add_message_index(with_copy, "INBOX", 1, 1, "<1@x>", 100, "2026-09-01T00:00:00+00:00",
                             "", "INBOX/cur/1", "sha")
    run = svc.db.start_run(with_copy, "backup", None)
    svc.db.finish_run(run, "success", messages_new=1)
    svc.db.note_backup_result(with_copy, "success")

    accs = {a["id"]: a for a in client.get("/api/accounts").json()}
    assert accs[with_copy]["messages"] == 1
    assert accs[with_copy]["last_backup_at"] and accs[with_copy]["first_backup_at"]
    assert accs[with_copy]["last_backup_status"] == "success"
    assert accs[without]["messages"] == 0 and not accs[without]["last_backup_at"]

    runs = client.get(f"/api/accounts/{with_copy}/runs").json()
    assert len(runs) == 1 and runs[0]["status"] == "success" and runs[0]["messages_new"] == 1


def test_check_logins_is_admin_only(client):
    _login(client)
    _account(client, "Ящик", "worker@x.ru")
    from fastapi.testclient import TestClient
    from tests.conftest import API_HEADERS
    anon = TestClient(client.app, headers=API_HEADERS)
    assert anon.post("/api/accounts/check-logins", json={}).status_code in (401, 403)
