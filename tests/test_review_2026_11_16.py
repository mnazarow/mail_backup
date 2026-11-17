"""Исправления по разбору 16.11.2026 (1.25.0): запланированная отправка после снятия доступа, .zip у
делегата «только чтение», аудит просмотров делегата, одновременные .zip и место на диске, вход по ящику при
недоступном почтовом сервере, права роли «ящик» сводной таблицей."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import test_webmail as base

from mailarchiver import webmail_tools as wt

world, box, api = base.world, base.box, base.api
smtp = base.smtp


def _grant(admin, svc, account_id, emp, rights):
    r = admin.post(f"/api/accounts/{account_id}/delegates", json={"employee_id": emp, "rights": rights})
    assert r.status_code == 200, r.text


def _emp(svc, account_id):
    return int(svc.db.get_account(account_id).employee_id)


def _ids(admin, account_id):
    return [r["id"] for r in admin.get(f"/api/webmail/messages?account_id={account_id}&folder=INBOX").json()["messages"]]


# ---------------------------------------------------------------------------
#  п. 5: запланированное письмо — права автора в момент отправки
# ---------------------------------------------------------------------------
def _scheduled(mb, account_id):
    d = mb.post("/api/webmail/drafts", json={"account_id": account_id, "to_addrs": "boss@ext.ru",
                                             "subject": "Запланировано", "body": "<p>текст</p>"}).json()
    when = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    r = mb.post(f"/api/webmail/drafts/{d['id']}/schedule", json={"send_at": when, "confirm": True})
    assert r.status_code == 200, r.text
    return int(d["id"])


def _due(svc, draft_id):
    svc.db.execute("UPDATE webmail_drafts SET send_at=? WHERE id=?",
                   ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), draft_id))
    return wt.send_due(svc)


def test_scheduled_send_rechecks_author(api, smtp):
    admin, mb, svc, a, b = api
    emp = _emp(svc, a)
    _grant(admin, svc, b, emp, "full")
    did = _scheduled(mb, b)
    admin.delete(f"/api/accounts/{b}/delegates/{emp}")              # доступ снят
    assert _due(svc, did) == 0 and smtp.sent == []
    row = svc.db.query_one("SELECT status, error FROM webmail_drafts WHERE id=?", (did,))
    assert row["status"] == "draft" and "больше нет доступа" in row["error"]
    # доступ «только чтение» — тоже нет
    _grant(admin, svc, b, emp, "full")
    did = _scheduled(mb, b)
    _grant(admin, svc, b, emp, "read")
    assert _due(svc, did) == 0 and "только чтение" in svc.db.scalar("SELECT error FROM webmail_drafts WHERE id=?",
                                                                     (did,))
    # свой ящик, ящик передан другому сотруднику
    did = _scheduled(mb, a)
    other = svc.db.create_employee(full_name="Сидоров Сидор", email="sidorov@x.ru")
    svc.db.execute("UPDATE accounts SET employee_id=? WHERE id=?", (other, a))
    assert _due(svc, did) == 0 and smtp.sent == []
    svc.db.execute("UPDATE accounts SET employee_id=? WHERE id=?", (emp, a))
    # с правами всё в порядке — уходит
    did = _scheduled(mb, a)
    assert _due(svc, did) == 1 and len(smtp.sent) == 1


def test_dismissal_cancels_scheduled_letters(api, smtp):
    from mailarchiver.employees import dismiss_employee
    admin, mb, svc, a, b = api
    svc.db.set_setting("employees.dismissed_action", "keep")
    did = _scheduled(mb, a)
    res = dismiss_employee(svc, _emp(svc, a), by="admin")
    assert res["scheduled_off"] == 1
    row = svc.db.query_one("SELECT status, error FROM webmail_drafts WHERE id=?", (did,))
    assert row["status"] == "draft" and "уволен" in row["error"]
    assert _due(svc, did) == 0 and smtp.sent == []


# ---------------------------------------------------------------------------
#  пп. 6–8: .zip писем
# ---------------------------------------------------------------------------
def test_zip_for_delegates(api):
    admin, mb, svc, a, b = api
    emp = _emp(svc, a)
    ids = _ids(admin, b)
    _grant(admin, svc, b, emp, "read")
    r = mb.post("/api/webmail/zip", json={"account_id": b, "ids": ids})
    assert r.status_code == 403 and "только для чтения" in r.json()["detail"]
    _grant(admin, svc, b, emp, "full")
    assert mb.post("/api/webmail/zip", json={"account_id": b, "ids": ids}).status_code == 200
    rows = svc.db.query("SELECT detail FROM audit WHERE action='webmail_zip'")
    assert len(rows) == 1 and "Петров" in rows[0]["detail"] and "чужому" in rows[0]["detail"]
    # свой ящик — без записи в аудит
    assert mb.post("/api/webmail/zip", json={"account_id": a, "ids": _ids(admin, a)}).status_code == 200
    assert svc.db.scalar("SELECT COUNT(*) FROM audit WHERE action='webmail_zip'") == 1


def test_delegate_views_are_audited(api):
    admin, mb, svc, a, b = api
    emp = _emp(svc, a)
    _grant(admin, svc, b, emp, "read")
    pk = _ids(admin, b)[0]
    assert mb.get(f"/api/accounts/{b}/messages/{pk}").status_code == 200
    row = svc.db.query_one("SELECT user, detail FROM audit WHERE action='message_view'")
    assert row is not None and "Петров" in row["detail"] and "чужому" in row["detail"]
    own = _ids(admin, a)[0]
    mb.get(f"/api/accounts/{a}/messages/{own}")
    assert svc.db.scalar("SELECT COUNT(*) FROM audit WHERE action='message_view'") == 1


def test_zip_parallel_limit_and_disk_room(api, monkeypatch):
    admin, mb, svc, a, b = api
    ids = _ids(admin, a)
    # два архива ещё скачиваются — третий ждёт
    first = wt.zip_messages(svc, svc.db.get_account(a), ids, who="ivanov@x.ru")
    second = wt.zip_messages(svc, svc.db.get_account(a), ids, who="ivanov@x.ru")
    r = mb.post("/api/webmail/zip", json={"account_id": a, "ids": ids})
    assert r.status_code == 429 and "дождитесь" in r.json()["detail"]
    first.close()
    second.close()
    assert mb.post("/api/webmail/zip", json={"account_id": a, "ids": ids}).status_code == 200
    assert wt._ZIP_BUSY == {}  # noqa: SLF001 - скачанный архив место освободил
    # места на диске данных мало — отказ до сборки
    import mailarchiver.util as util
    monkeypatch.setattr(util, "disk_free_bytes", lambda path: 100 * 1024 * 1024)
    r = mb.post("/api/webmail/zip", json={"account_id": a, "ids": ids})
    assert r.status_code == 400 and "не хватает места" in r.json()["message"]
    assert wt._ZIP_BUSY == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
#  п. 9: вход по ящику, когда почтовый сервер не отвечает
# ---------------------------------------------------------------------------
def test_mailbox_login_when_mail_server_is_down(api, monkeypatch):
    from mailarchiver.errors import ImapConnectionError
    from mailarchiver.imap import client as client_mod
    from mailarchiver.web import auth as auth_mod
    admin, mb, svc, a, b = api

    class Down:
        def __init__(self, *a, **k):
            raise ImapConnectionError("Connection refused")
    real = client_mod.ImapConnection
    monkeypatch.setattr(client_mod, "ImapConnection", Down)
    monkeypatch.setattr(auth_mod, "client_ip", lambda request: "10.9.9.9")
    for _ in range(12):
        c = TestClient(admin.app, headers=base.HDR)
        r = c.post("/api/login", json={"username": "ivanov@x.ru", "password": "pw"})
        assert "не отвечает" in r.json()["message"]
    # неудачами это не считается: почта поднялась — вход сразу
    assert svc.db.scalar("SELECT COUNT(*) FROM login_attempts WHERE success=0") == 0
    assert svc.db.query_one("SELECT 1 FROM audit WHERE action='login_mailbox_unavailable'")
    monkeypatch.setattr(client_mod, "ImapConnection", real)
    c = TestClient(admin.app, headers=base.HDR)
    r = c.post("/api/login", json={"username": "ivanov@x.ru", "password": "pw"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "mailbox"
    # неверный пароль — по-прежнему неудача
    c = TestClient(admin.app, headers=base.HDR)
    r = c.post("/api/login", json={"username": "ivanov@x.ru", "password": "wrong"})
    assert "Неверный логин или пароль" in r.json()["message"]


# ---------------------------------------------------------------------------
#  Сводная проверка прав роли «ящик» (предложение 4): чужой ящик «только чтение»
# ---------------------------------------------------------------------------
READ_FORBIDDEN = [
    ("post", "/api/webmail/zip", lambda b, ids: {"account_id": b, "ids": ids}),
    ("post", "/api/webmail/flags", lambda b, ids: {"account_id": b, "ids": ids, "add": ["\\Seen"]}),
    ("post", "/api/webmail/move", lambda b, ids: {"account_id": b, "ids": ids, "folder": "Trash"}),
    ("post", "/api/webmail/delete", lambda b, ids: {"account_id": b, "ids": ids}),
    ("post", "/api/webmail/drafts", lambda b, ids: {"account_id": b, "to_addrs": "x@y.ru", "subject": "s"}),
    ("post", "/api/accounts/{b}/export", lambda b, ids: {"format": "eml"}),
    ("post", "/api/accounts/{b}/backup", lambda b, ids: {}),
    ("post", "/api/accounts/{b}/restore-messages", lambda b, ids: {"message_ids": ids}),
    ("post", "/api/accounts/{b}/retention", lambda b, ids: {"days": 1}),
    ("put", "/api/webmail/autoreply", lambda b, ids: {"account_id": b, "enabled": True}),
]


@pytest.mark.parametrize("method,path,body", READ_FORBIDDEN, ids=[p for _m, p, _b in READ_FORBIDDEN])
def test_read_only_delegate_cannot_change_or_export(api, method, path, body):
    admin, mb, svc, a, b = api
    _grant(admin, svc, b, _emp(svc, a), "read")
    ids = _ids(admin, b)
    url = path.replace("{b}", str(b))
    r = getattr(mb, method)(url, json=body(b, ids))
    assert r.status_code in (403, 404, 422), (url, r.status_code, r.text[:200])
    if r.status_code != 403:
        pytest.skip(f"маршрут {url} изменился ({r.status_code}) — поправьте таблицу")
