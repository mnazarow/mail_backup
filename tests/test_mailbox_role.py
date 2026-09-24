# -*- coding: utf-8 -*-
"""Права сотрудника, вошедшего по своему ящику (роль «mailbox»).

Архив ведётся для организации: сотрудник читает, выгружает и восстанавливает
СВОЮ почту, но не может уменьшить или исказить архив — поменять срок хранения,
выключить копирование через расписания, отменить копирование по расписанию,
удалить чужую выгрузку, загрузить .pst.
"""
import pytest
from fastapi.testclient import TestClient

from mailarchiver.security import sign_value
from mailarchiver.web.auth import COOKIE_NAME

HDR = {"X-Requested-With": "fetch"}


@pytest.fixture()
def setup(client):
    client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
    client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})
    aid = client.post("/api/accounts", json={"name": "Иванов", "host": "h", "port": 993,
                                             "username": "ivanov@x.ru", "password": "p"}).json()["id"]
    other = client.post("/api/accounts", json={"name": "Петров", "host": "h", "port": 993,
                                               "username": "petrov@x.ru", "password": "p"}).json()["id"]
    svc = client.app.state.services
    svc.db.create_session("mbtoken", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    mb = TestClient(client.app, headers=HDR)
    mb.cookies.set(COOKIE_NAME, sign_value("mbtoken", svc.cfg.secret_key()))
    assert mb.get("/api/me").json()["role"] == "mailbox"
    return client, mb, aid, other, svc


def test_mailbox_cannot_change_retention(setup):
    admin, mb, aid, _other, svc = setup
    r = mb.post(f"/api/accounts/{aid}/retention", json={"days": 3})
    assert r.status_code == 403
    assert svc.db.get_account(aid).retention_days == -1
    assert not [s for s in svc.db.list_schedules(aid) if s["job_type"] == "retention"]
    # администратор — может
    assert admin.post(f"/api/accounts/{aid}/retention", json={"days": 0}).status_code == 200


def test_mailbox_cannot_manage_schedules(setup):
    admin, mb, aid, _other, svc = setup
    body = {"account_id": aid, "job_type": "backup", "kind": "cron", "cron_expr": "0 3 * * *"}
    assert mb.post("/api/schedules", json=body).status_code == 403
    sid = admin.post("/api/schedules", json=body).json()["id"]
    assert mb.put(f"/api/schedules/{sid}", json=dict(body, enabled=False)).status_code == 403
    assert mb.delete(f"/api/schedules/{sid}").status_code == 403
    assert svc.db.get_schedule(sid) is not None
    # просмотр своих расписаний остаётся
    assert mb.get("/api/schedules").status_code == 200


def test_mailbox_cannot_cancel_scheduled_backup(setup):
    admin, mb, aid, _other, svc = setup
    jid = svc.db.enqueue_job("backup", aid, {}, created_by="scheduler")
    r = mb.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 403
    assert not svc.db.get_job(jid)["cancel_requested"]
    # своё задание — можно
    own = svc.db.enqueue_job("export", aid, {}, created_by="ivanov@x.ru")
    assert mb.post(f"/api/jobs/{own}/cancel").status_code == 200


def test_mailbox_cannot_import_pst(setup):
    _admin, mb, aid, _other, _svc = setup
    r = mb.post(f"/api/accounts/{aid}/import-pst", files={"file": ("a.pst", b"!BDN....", "application/octet-stream")})
    assert r.status_code == 403


def test_mailbox_export_limit_and_foreign_export_delete(setup):
    admin, mb, aid, _other, svc = setup
    svc.queue.stop()          # задания должны остаться в очереди, а не выполниться мгновенно
    # чужая (администраторская) выгрузка — удалить нельзя
    admin_job = svc.db.enqueue_job("export", aid, {}, created_by="adm")
    eid = svc.db.create_export(aid, "eml", "eml", "/nonexistent/x.zip", {}, admin_job)
    listing = mb.get("/api/exports").json()
    assert listing[0]["can_delete"] is False and "path" not in listing[0]
    assert mb.delete(f"/api/exports/{eid}").status_code == 403
    assert svc.db.get_export(eid) is not None
    # пока идёт чья-то выгрузка этого ящика — новая отклоняется
    assert mb.post(f"/api/accounts/{aid}/export", json={"format": "eml"}).status_code == 400
    svc.db.execute("UPDATE jobs SET status='success' WHERE id=?", (admin_job,))
    # вторая выгрузка при уже активной — отклоняется
    first = mb.post(f"/api/accounts/{aid}/export", json={"format": "eml"})
    assert first.status_code == 200
    second = mb.post(f"/api/accounts/{aid}/export", json={"format": "eml"})
    assert second.status_code == 400


def test_mailbox_isolated_from_other_accounts(setup):
    _admin, mb, _aid, other, _svc = setup
    assert mb.get(f"/api/accounts/{other}").status_code == 403
    assert mb.get(f"/api/accounts/{other}/messages").status_code == 403
    assert mb.post(f"/api/accounts/{other}/backup", json={}).status_code == 403


# ---------------------------------------------------------------------------
#  Вход по паролю ящика: пределы проверок на IMAP-сервере
# ---------------------------------------------------------------------------
def _patch_imap_check(monkeypatch, result=None):
    from mailarchiver.web import auth
    calls = []

    def fake(services, acc, password):
        calls.append((acc.username, password))
        return acc if result == "ok" and password == "верный" else None

    monkeypatch.setattr(auth, "_try_mailbox_login", fake)
    ip = {"v": "10.1.0.1"}
    monkeypatch.setattr(auth, "client_ip", lambda request: ip["v"])
    return calls, ip


def test_mailbox_login_checks_are_limited_per_mailbox(setup, monkeypatch):
    client, _mb, _aid, _other, svc = setup
    calls, ip = _patch_imap_check(monkeypatch)
    msgs = []
    for n in range(15):
        ip["v"] = f"10.1.1.{n}"
        c = TestClient(client.app, headers=HDR)
        msgs.append(c.post("/api/login", json={"username": "ivanov@x.ru", "password": f"p{n}"}).json()["message"])
    # 5 попыток × 2 = 10 проверок на почтовом сервере, дальше — отказ без обращения к нему
    assert len(calls) == 10, calls
    assert "Слишком много неудачных попыток входа в этот ящик" in msgs[-1]
    # другой ящик при этом доступен
    ip["v"] = "10.1.2.1"
    c = TestClient(client.app, headers=HDR)
    c.post("/api/login", json={"username": "petrov@x.ru", "password": "x"})
    assert len(calls) == 11


def test_mailbox_login_global_limit(setup, monkeypatch):
    client, _mb, _aid, _other, svc = setup
    calls, ip = _patch_imap_check(monkeypatch)
    names = [f"user{n}@x.ru" for n in range(70)]
    for name in names:
        svc.db.execute("INSERT INTO accounts(name, host, port, username, auth_type, enabled, created_at) "
                       "VALUES(?,?,?,?,?,1,'2026-01-01')", (name, "h", 993, name, "password"))
    msgs = []
    for n, name in enumerate(names):
        ip["v"] = f"10.2.{n // 200}.{n % 200}"
        c = TestClient(client.app, headers=HDR)
        msgs.append(c.post("/api/login", json={"username": name, "password": "x"}).json()["message"])
    assert len(calls) == 60, len(calls)
    assert "временно приостановлен" in msgs[-1]


def test_mailbox_login_can_be_disabled(setup, monkeypatch):
    client, _mb, _aid, _other, svc = setup
    calls, ip = _patch_imap_check(monkeypatch, result="ok")
    assert client.put("/api/settings", json={"values": {"security.mailbox_login": False}}).status_code == 200
    c = TestClient(client.app, headers=HDR)
    r = c.post("/api/login", json={"username": "ivanov@x.ru", "password": "верный"})
    assert r.status_code == 400 and not calls
    assert client.put("/api/settings", json={"values": {"security.mailbox_login": True}}).status_code == 200
    r = c.post("/api/login", json={"username": "ivanov@x.ru", "password": "верный"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "mailbox"
    # успешный вход не оставляет записи о неудаче
    assert svc.db.count_recent_failures("ivanov@x.ru", "2000-01-01", kind="imap") == 0
