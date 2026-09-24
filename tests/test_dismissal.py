"""Уволенные сотрудники: удержание архива, последняя копия, выключение, возврат, выгрузка."""
from datetime import date, datetime, timedelta, timezone

from mailarchiver import models
from mailarchiver.employees import (HOLD_FOREVER, dismiss_employee, parse_employee_file, rehire_employee,
                                    sync_employees)
from mailarchiver.queue import jobs as jobs_mod

PW = "Sw0rdfish!1"


def _employee_with_account(svc, name="Иванов Иван", email="ivanov@example.ru", password="p", enabled=True,
                           host="127.0.0.1"):
    # порт 9 закрыт: если очередь всё же запустит копирование, оно сразу получит отказ в соединении
    acc_id = svc.db.create_account(models.Account(name=name, host=host, port=9, username=email, password=password,
                                                  enabled=enabled, retention_days=3, security="plain"))
    emp_id = svc.db.create_employee(full_name=name, email=email, source="file",
                                    last_seen_at=datetime.now(timezone.utc).isoformat())
    svc.db.set_employee_account(emp_id, acc_id)
    return emp_id, acc_id


def test_dismissal_holds_archive_and_queues_final_backup(services):
    svc = services
    emp_id, acc_id = _employee_with_account(svc)
    res = dismiss_employee(svc, emp_id, by="admin")
    assert res["action"] == "final_backup" and res["job_id"]
    acc = svc.db.get_account(acc_id)
    years = int(svc.rt("employees", "dismissed_keep_years"))
    assert acc.hold_until == date.today().replace(year=date.today().year + years).isoformat()
    assert acc.hold_reason == "dismissed" and acc.dismissed_at and acc.on_hold()
    job = svc.db.get_job(res["job_id"])
    assert job["type"] == "backup" and '"final": true' in job["params"]
    assert svc.db.get_employee(emp_id)["status"] == "archived"
    # срок хранения «3 дня» не действует, пока архив удерживается
    assert jobs_mod.effective_retention_days(svc, acc) == 0
    assert any(a["action"] == "employee_dismissed" for a in svc.db.list_audit())


def test_final_backup_disables_account_even_if_it_fails(services):
    svc = services
    emp_id, acc_id = _employee_with_account(svc, host="")          # сервера нет — копия упадёт
    dismiss_employee(svc, emp_id)
    ctx = jobs_mod.JobContext(svc, 1, "backup", acc_id, {"final": True})
    try:
        jobs_mod.handle_backup(ctx)
    except Exception:  # noqa: BLE001
        pass
    acc = svc.db.get_account(acc_id)
    assert not acc.enabled and acc.auto_disabled


def test_rehire_restores_everything(services):
    svc = services
    emp_id, acc_id = _employee_with_account(svc, password="")      # без пароля — выключается сразу
    res = dismiss_employee(svc, emp_id)
    assert res["action"] == "disabled"
    assert not svc.db.get_account(acc_id).enabled
    back = rehire_employee(svc, emp_id)
    acc = svc.db.get_account(acc_id)
    assert back["enabled"] and acc.enabled and not acc.auto_disabled
    assert acc.hold_until == "" and not acc.dismissed_at
    assert svc.db.get_employee(emp_id)["status"] == "active"
    assert jobs_mod.effective_retention_days(svc, acc) == 3


def test_manual_hold_is_kept_and_keep_action(services):
    svc = services
    svc.set_rt("employees", "dismissed_action", "keep")
    svc.set_rt("employees", "dismissed_keep_years", 0)
    emp_id, acc_id = _employee_with_account(svc)
    res = dismiss_employee(svc, emp_id)
    acc = svc.db.get_account(acc_id)
    assert res["action"] == "kept" and acc.enabled and acc.hold_until == HOLD_FOREVER
    emp2, acc2 = _employee_with_account(svc, name="Петров", email="petrov@example.ru")
    svc.db.set_account_hold(acc2, "2200-01-01", "manual")
    svc.set_rt("employees", "dismissed_keep_years", 1)
    dismiss_employee(svc, emp2)
    assert svc.db.get_account(acc2).hold_until == "2200-01-01"      # длинное ручное удержание не сокращается
    rehire_employee(svc, emp2)
    assert svc.db.get_account(acc2).hold_until == "2200-01-01"      # и не снимается при возврате


def test_expired_hold_allows_retention_again(services):
    svc = services
    _emp, acc_id = _employee_with_account(svc)
    svc.db.set_account_hold(acc_id, (date.today() - timedelta(days=1)).isoformat(), "dismissed")
    acc = svc.db.get_account(acc_id)
    assert not acc.on_hold() and acc.redacted()["hold_expired"]
    assert jobs_mod.effective_retention_days(svc, acc) == 3


def _sync(svc, text):
    rows, problems = parse_employee_file(text.encode("utf-8"), "e.csv")
    assert not problems, problems
    return sync_employees(svc, rows, create_accounts=False)


def test_sync_dismisses_by_file_and_rehires(services):
    svc = services
    emp_id, acc_id = _employee_with_account(svc)
    for i in range(10):
        svc.db.create_employee(full_name=f"Работник {i}", email=f"w{i}@example.ru", source="file")
    res = _sync(svc, "ФИО;E-mail;Уволен\nИванов Иван;ivanov@example.ru;да\n")
    assert res["dismissed"] == 1
    assert svc.db.get_account(acc_id).on_hold() and svc.db.get_employee(emp_id)["status"] == "archived"
    res = _sync(svc, "ФИО;E-mail;Уволен\nИванов Иван;ivanov@example.ru;нет\n")
    assert res["rehired"] == 1 and svc.db.get_employee(emp_id)["status"] == "active"
    assert not svc.db.get_account(acc_id).on_hold()


def test_sync_guard_against_broken_export(services):
    svc = services
    lines = ["ФИО;E-mail;Уволен"]
    for i in range(12):
        svc.db.create_employee(full_name=f"Сотрудник {i}", email=f"s{i}@example.ru", source="file")
        lines.append(f"Сотрудник {i};s{i}@example.ru;да")
    res = _sync(svc, "\n".join(lines) + "\n")
    assert res["dismissed"] == 0 and res["dismiss_blocked"] == 12
    assert any("испорченная выгрузка" in w for w in res["warnings"])
    assert svc.db.employee_counts()["archived"] == 0


def test_sync_dismisses_missing_employees(services):
    svc = services
    svc.set_rt("employees", "dismiss_missing_days", 30)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    gone, gone_acc = _employee_with_account(svc, name="Ушёл", email="gone@example.ru")
    svc.db.update_employee(gone, last_seen_at=old)
    for i in range(10):
        svc.db.create_employee(full_name=f"Работник {i}", email=f"w{i}@example.ru", source="file",
                               last_seen_at=datetime.now(timezone.utc).isoformat())
    manual = svc.db.create_employee(full_name="Вручную", email="manual@example.ru", source="manual",
                                    last_seen_at=old)
    res = _sync(svc, "ФИО;E-mail\nРаботник 1;w1@example.ru\n")
    assert res["dismissed"] == 1
    assert svc.db.get_employee(gone)["status"] == "archived" and svc.db.get_account(gone_acc).on_hold()
    assert svc.db.get_employee(manual)["status"] == "active"       # заведённых вручную не трогаем


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_dismissal_hold_and_purge_api(client):
    _login(client)
    svc = client.app.state.services
    emp_id, acc_id = _employee_with_account(svc)
    svc.store.store_message(acc_id, "INBOX", "/", 1, b"Subject: x\r\n\r\nbody\r\n")
    body = {"full_name": "Иванов Иван", "email": "ivanov@example.ru", "status": "archived"}
    r = client.put(f"/api/employees/{emp_id}", json=body).json()
    assert r["dismissed"]["action"] == "final_backup"
    accs = {a["id"]: a for a in client.get("/api/accounts").json()}
    assert accs[acc_id]["on_hold"] and accs[acc_id]["dismissed_at"]
    emps = client.get("/api/employees").json()["employees"]
    assert emps[0]["dismissed_at"] and emps[0]["account_hold_until"]
    # удалить архив, пока он удерживается, нельзя
    import time
    for _ in range(100):
        active = svc.db.active_jobs()
        if not active:
            break
        for job in active:
            svc.queue.cancel(job["id"])
        time.sleep(0.05)
    refused = client.post(f"/api/accounts/{acc_id}/purge", json={"confirm_name": "Иванов Иван"})
    assert refused.status_code == 400 and "удерживается" in refused.json()["message"]
    assert client.post(f"/api/accounts/{acc_id}/hold", json={"until": "2001-01-01"}).status_code == 400
    assert client.post(f"/api/accounts/{acc_id}/hold", json={"until": "forever"}).json()["hold_until"] == HOLD_FOREVER
    assert client.post(f"/api/accounts/{acc_id}/hold", json={"until": ""}).json()["hold_until"] == ""
    wrong = client.post(f"/api/accounts/{acc_id}/purge", json={"confirm_name": "не то"})
    assert wrong.status_code == 400
    ok = client.post(f"/api/accounts/{acc_id}/purge", json={"confirm_name": "Иванов Иван"}).json()
    assert ok["files"] == 1
    assert svc.db.get_account(acc_id) is None
    import os
    assert not os.path.exists(svc.store.account_dir(acc_id))
