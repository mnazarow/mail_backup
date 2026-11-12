"""Раздел «Дела» (1.20.0): удержание по делу, письма дела, выгрузка с описью, права и API."""
import csv
import hashlib
import io
import os
import time
import zipfile
from datetime import datetime, timezone

import pytest

from mailarchiver import cases, models, search
from mailarchiver.employees import HOLD_FOREVER
from mailarchiver.errors import ValidationError
from mailarchiver.models import JobType

NOW = int(time.time())


def _raw(subject, body="Текст", frm="client@partner.ru", to="ivanov@company.ru"):
    return (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nMessage-ID: <{subject}@x>\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode("utf-8")


class Box:
    uid = 0

    def __init__(self, svc):
        self.svc = svc

    def add(self, acc, raw, *, folder="INBOX", ts=NOW, subject=""):
        Box.uid += 1
        rel, sha, size = self.svc.store.store_message(acc, folder, "/", Box.uid, raw)
        when = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        self.svc.db.add_message_index_batch([(acc, folder, 1, Box.uid, f"<{subject}@x>", size, when, "", rel, sha,
                                              subject, "client@partner.ru", 0)])
        return int(self.svc.db.scalar("SELECT MAX(id) FROM messages"))


@pytest.fixture()
def world(services):
    svc = services
    a = svc.db.create_account(models.Account(name="Иванов", host="h", username="ivanov@company.ru", password="p"))
    b = svc.db.create_account(models.Account(name="Петрова", host="h", username="petrova@company.ru", password="p"))
    box = Box(svc)
    ids = {"a1": box.add(a, _raw("Договор поставки", "сумма контракта"), subject="Договор поставки"),
           "a2": box.add(a, _raw("Обед"), folder="Личное", subject="Обед"),
           "a3": box.add(a, _raw("Старое"), ts=NOW - 400 * 86400, subject="Старое"),
           "b1": box.add(b, _raw("Договор аренды", "контракт"), subject="Договор аренды")}
    search.index_pending(svc)
    return svc, a, b, ids


def test_hold_follows_open_cases(world):
    svc, a, b, _ = world
    svc.db.set_account_hold(a, "2030-01-01", "manual")
    c1 = cases.create_case(svc, "admin", name="Проверка", account_ids=[a, b])
    acc_a, acc_b = svc.db.get_account(a), svc.db.get_account(b)
    assert acc_a.hold_until == HOLD_FOREVER and acc_a.hold_reason == "case"
    assert acc_b.on_hold() and acc_b.hold_reason == "case"
    # ручное снятие (групповое действие, увольнение) удержание дела не трогает — оно вернётся после дела
    svc.db.set_account_hold(a, "", "")
    assert svc.db.get_account(a).hold_reason == "case"
    c2 = cases.create_case(svc, "admin", name="Второе", account_ids=[b])
    cases.close_case(svc, "admin", c1)
    assert svc.db.get_account(a).hold_until == ""             # вернулось значение, поставленное за время дела
    assert svc.db.get_account(b).hold_reason == "case"        # Петрову держит второе дело
    cases.close_case(svc, "admin", c2)
    assert not svc.db.get_account(b).on_hold()
    cases.close_case(svc, "admin", c1, reopen=True)
    assert svc.db.get_account(a).hold_reason == "case"
    cases.update_case(svc, "admin", c1, {"hold": False})
    assert svc.db.get_account(a).hold_until == ""
    actions = [r["action"] for r in svc.db.query("SELECT action FROM audit WHERE action LIKE 'case_%'")]
    assert "case_create" in actions and "case_close" in actions and "case_reopen" in actions


def test_previous_hold_restored(world):
    svc, a, _, _ = world
    svc.db.set_account_hold(a, "2031-05-05", "dismissed")
    cid = cases.create_case(svc, "admin", name="Дело", account_ids=[a])
    cases.set_custodians(svc, "admin", cid, remove=[a])
    acc = svc.db.get_account(a)
    assert acc.hold_until == "2031-05-05" and acc.hold_reason == "dismissed"


def test_case_messages_query_and_period(world):
    svc, a, b, ids = world
    cid = cases.create_case(svc, "admin", name="Договоры", query="договор", account_ids=[a, b])
    got = {r["id"] for r in cases.list_messages(svc, cid)["results"]}
    assert got == {ids["a1"], ids["b1"]}
    cases.update_case(svc, "admin", cid, {"query": "", "date_from": datetime.fromtimestamp(
        NOW - 30 * 86400).date().isoformat()})
    got = {r["id"] for r in cases.list_messages(svc, cid)["results"]}
    assert got == {ids["a1"], ids["a2"], ids["b1"]}           # старое письмо — вне периода
    cases.set_custodians(svc, "admin", cid, remove=[b])
    assert cases.count_messages(svc, cases.get_case(svc, cid))["count"] == 2
    with pytest.raises(ValidationError):
        cases.update_case(svc, "admin", cid, {"date_from": "2030-01-02", "date_to": "2030-01-01"})


def test_case_export_zip(world):
    from mailarchiver.queue.jobs import HANDLERS, JobContext
    svc, a, b, ids = world
    cid = cases.create_case(svc, "admin", name="Договоры/2026", query="договор", account_ids=[a, b])
    job_id = svc.db.enqueue_job(JobType.CASE_EXPORT, None, {"case_id": cid}, 4, 1, "admin")
    res = HANDLERS[JobType.CASE_EXPORT](JobContext(svc, job_id, JobType.CASE_EXPORT, None, {"case_id": cid}))
    assert res["count"] == 2 and not res["errors"]
    path = res["path"]
    assert os.path.dirname(path) == svc.cfg.exports_dir
    data = open(path, "rb").read()
    assert hashlib.sha256(data).hexdigest() == res["sha256"]
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        base = names[0].split("/")[0]
        manifest = zf.read(f"{base}/manifest.csv")
        assert zf.read(f"{base}/manifest.sha256").decode().startswith(hashlib.sha256(manifest).hexdigest())
        rows = list(csv.DictReader(io.StringIO(manifest.decode("utf-8-sig"))))
        assert len(rows) == 2
        for row in rows:
            body = zf.read(f"{base}/{row['file']}")
            assert hashlib.sha256(body).hexdigest() == row["sha256"]
            assert b"Subject: \xd0\x94\xd0\xbe\xd0\xb3\xd0\xbe\xd0\xb2\xd0\xbe\xd1\x80" in body
        report = zf.read(f"{base}/report.txt").decode()
        assert "Договоры/2026" in report and "Выгружено писем: 2" in report
    exp = cases.case_dict(svc, cases.get_case(svc, cid), full=True)["export_list"][0]
    assert exp["status"] == "success" and exp["sha256"] == res["sha256"] and exp["ready"]


def test_delete_requires_closed(world):
    svc, a, _, _ = world
    cid = cases.create_case(svc, "admin", name="Дело", account_ids=[a])
    with pytest.raises(ValidationError):
        cases.delete_case(svc, "admin", cid)
    cases.close_case(svc, "admin", cid)
    cases.delete_case(svc, "admin", cid)
    assert not svc.db.query("SELECT * FROM cases")
    assert not svc.db.get_account(a).on_hold()


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_api_cases_and_hold_guard(client):
    _login(client)
    aid = client.post("/api/accounts", json={"name": "Ящик", "host": "imap.example.com", "port": 993,
                                             "username": "u@example.com", "password": "secret", "security": "ssl",
                                             "auth_type": "password"}).json()["id"]
    r = client.post("/api/cases", json={"name": "Спор с поставщиком", "account_ids": [aid], "hold": True})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    got = client.get(f"/api/cases/{cid}").json()
    assert got["custodians"] == 1 and got["accounts"][0]["hold_reason"] == "case"
    # ручное снятие удержания и удаление ящика — отказ
    r = client.post(f"/api/accounts/{aid}/hold", json={"until": ""})
    assert r.status_code == 400 and "дел" in r.json()["message"]
    assert client.delete(f"/api/accounts/{aid}").status_code == 400
    assert client.post(f"/api/cases/{cid}/export").status_code == 400    # писем нет
    assert client.get("/api/cases/999").status_code == 404
    assert client.post(f"/api/cases/{cid}/close").status_code == 200
    assert client.delete(f"/api/accounts/{aid}").status_code == 200
    assert client.get("/api/cases").json()["items"][0]["status"] == "closed"


def test_cases_admin_only(client):
    _login(client)
    client.post("/api/users", json={"username": "op", "password": "Sw0rdfish!2", "role": "operator"})
    client.post("/api/logout")
    client.post("/api/login", json={"username": "op", "password": "Sw0rdfish!2"})
    assert client.get("/api/cases").status_code == 403
    assert client.post("/api/cases", json={"name": "x"}).status_code == 403


def test_ui_has_cases_section():
    js = open(os.path.join(os.path.dirname(__file__), "..", "mailarchiver", "web", "static", "js", "app.js"),
              encoding="utf-8").read()
    for needle in ("{id:'cases'", "async function viewCases", "accountPickerDlg", "/exports/${x.id}/download",
                   "cases:viewCases", "🔒 по делу"):
        assert needle in js, needle
