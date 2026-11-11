"""Несколько почтовых ящиков у сотрудника (1.15.0).

* Связь хранится у ящика (accounts.employee_id): у сотрудника сколько угодно ящиков, ящик — у одного.
* Первый запуск 1.15.0 переносит прежние привязки (employees.account_id) — общий ящик остаётся за тем,
  чей адрес совпадает с логином.
* В выгрузке кадров несколько адресов пишутся в одну ячейку через «;»: на каждый заводится ящик,
  удалённый администратором ящик ночью не появляется снова, чужой ящик не отбирается.
* Увольнение и возврат действуют на все ящики сотрудника.
* Карточка сотрудника в API: список ящиков, «завести ещё», «привязать», «отвязать», перенос чужого.
"""
import pytest

from mailarchiver import models
from mailarchiver.employees import (apply_passwords, dismiss_employee, parse_employee_file, rehire_employee,
                                    split_emails, sync_employees)
from mailarchiver.serverops.targets import dismissed_map


def _acc(db, login, name=None, enabled=True, password="p"):
    return db.create_account(models.Account(name=name or login, host="imap.x.ru", username=login,
                                            password=password, enabled=enabled))


def _template(svc, **extra):
    values = {"employees.account_host": "imap.x.ru", "employees.create_accounts": True}
    values.update(extra)
    svc.db.set_settings_many(values)


def test_split_emails():
    assert split_emails(" Ivanov@X.ru ; i.ivanov@x.ru,ivanov@x.ru\nold@x.ru ") == (
        ["ivanov@x.ru", "i.ivanov@x.ru", "old@x.ru"], [])
    assert split_emails("ivanov@x.ru; нет-адреса") == (["ivanov@x.ru"], ["нет-адреса"])
    assert split_emails("") == ([], [])


def test_employee_has_many_mailboxes_and_mailbox_has_one_owner(services):
    db = services.db
    e1 = db.create_employee(full_name="Иванов Иван", email="ivanov@x.ru")
    e2 = db.create_employee(full_name="Петров Пётр", email="petrov@x.ru")
    old, main = _acc(db, "old@x.ru"), _acc(db, "ivanov@x.ru")
    db.link_employee_account(e1, old)
    db.link_employee_account(e1, main)
    assert [a["id"] for a in db.employee_accounts(e1)] == [main, old]      # основной (логин = адрес) — первым
    row = db.get_employee(e1)
    assert row["account_id"] == main and row["account_count"] == 2
    assert db.employee_counts()["with_account"] == 1 and db.employee_counts()["without_account"] == 1
    # ящик — у одного сотрудника: привязка к другому его переносит
    assert db.link_employee_account(e2, old) == e1
    assert [a["id"] for a in db.employee_accounts(e1)] == [main]
    assert db.employee_of_account(old)["id"] == e2
    assert db.employees_by_account()[old]["full_name"] == "Петров Пётр"
    # удаление сотрудника ящики не удаляет — только связь
    db.delete_employee(e2)
    assert db.get_account(old) is not None and db.get_account(old).employee_id is None
    # адрес дополнительного ящика — тоже его (для шаблонов на сервере), а опознание при синхронизации — нет
    db.link_employee_account(e1, old)
    assert db.employee_by_address("OLD@x.ru")["id"] == e1
    assert db.get_employee_by_email("old@x.ru") is None


def test_first_start_moves_old_links(services):
    db = services.db
    e1 = db.create_employee(full_name="Склад (Иванов)", email="ivanov@x.ru")
    e2 = db.create_employee(full_name="Склад", email="sklad@x.ru")
    e3 = db.create_employee(full_name="Без ящика", email="none@x.ru")
    shared, own = _acc(db, "sklad@x.ru"), _acc(db, "ivanov@x.ru")
    conn = db.connect()
    # база прежней версии: связь в employees.account_id, общий ящик — у двух карточек
    conn.execute("UPDATE accounts SET employee_id=NULL")
    conn.execute("UPDATE employees SET known_logins='', had_account=0")
    conn.execute("UPDATE employees SET account_id=?, had_account=1 WHERE id=?", (shared, e1))
    conn.execute("UPDATE employees SET account_id=?, had_account=1 WHERE id=?", (shared, e2))
    conn.execute("UPDATE employees SET had_account=1 WHERE id=?", (e3,))     # ящик был, его удалили
    conn.execute("DELETE FROM meta WHERE key=?", (db._EMPLOYEE_ACCOUNTS_MARK,))
    conn.commit()
    db.init_schema()
    assert db.get_account(shared).employee_id == e2          # адрес совпадает с логином ящика
    assert db.get_account(own).employee_id is None
    assert db.employee_known_logins(db.get_employee(e1)) == {"ivanov@x.ru", "sklad@x.ru"}
    assert db.employee_known_logins(db.get_employee(e3)) == {"none@x.ru"}
    db.init_schema()                                          # второй запуск ничего не трогает
    assert db.get_account(shared).employee_id == e2


def test_sync_creates_a_mailbox_per_address_once(services):
    _template(services)
    rows, problems = parse_employee_file(
        'ФИО,E-mail\nИванов Иван,"ivanov@x.ru; i.ivanov@x.ru"\nПетров Пётр,"petrov@x.ru; плохой"\n'.encode(),
        "list.csv")
    assert rows[0]["email"] == "ivanov@x.ru" and rows[0]["extra_emails"] == ["i.ivanov@x.ru"]
    assert any("плохой" in p["reason"] for p in problems)
    res = sync_employees(services, rows, create_accounts=True)
    assert res["accounts_created"] == 3
    db = services.db
    emp = db.get_employee_by_email("ivanov@x.ru")
    boxes = db.employee_accounts(emp["id"])
    assert [b["username"] for b in boxes] == ["ivanov@x.ru", "i.ivanov@x.ru"]
    assert boxes[1]["name"] == "Иванов Иван (i.ivanov@x.ru)"           # второй ящик — с адресом в названии
    # повтор — без новых ящиков; удалённый администратором ящик не появляется снова
    assert sync_employees(services, rows, create_accounts=True)["accounts_created"] == 0
    db.delete_account(boxes[1]["id"])
    assert sync_employees(services, rows, create_accounts=True)["accounts_created"] == 0
    assert [b["username"] for b in db.employee_accounts(emp["id"])] == ["ivanov@x.ru"]
    # новый адрес в выгрузке — новый ящик
    rows[0]["extra_emails"] = ["i.ivanov@x.ru", "ivan@x.ru"]
    assert sync_employees(services, rows, create_accounts=True)["accounts_created"] == 1


def test_sync_does_not_take_someone_elses_mailbox(services):
    _template(services)
    db = services.db
    owner = db.create_employee(full_name="Склад", email="sklad.boss@x.ru")     # общий ящик — за начальником
    db.link_employee_account(owner, _acc(db, "sklad@x.ru"))
    rows = [{"row": 2, "full_name": "Иванов Иван", "email": "ivanov@x.ru", "extra_emails": ["sklad@x.ru"]}]
    res = sync_employees(services, rows, create_accounts=True)
    assert res["accounts_created"] == 1 and res["accounts_linked"] == 0
    assert any("sklad@x.ru" in w and "другими сотрудниками" in w for w in res["warnings"])
    assert [a["username"] for a in db.employee_accounts(owner)] == ["sklad@x.ru"]


def test_dismiss_and_rehire_cover_all_mailboxes(services):
    db = services.db
    emp = db.create_employee(full_name="Иванов", email="ivanov@x.ru")
    a = _acc(db, "ivanov@x.ru")
    b = _acc(db, "old@x.ru", password="")            # без пароля: последнюю копию не снять — сразу выключается
    for aid in (a, b):
        db.link_employee_account(emp, aid)
    services.db.set_settings_many({"employees.dismissed_action": "final_backup_disable"})

    class _Queue:
        def __init__(self):
            self.jobs = []

        def enqueue(self, *args, **kw):
            self.jobs.append(args)
            return len(self.jobs)

    services.queue = _Queue()
    out = dismiss_employee(services, emp, by="adm")
    assert [i["action"] for i in out["accounts"]] == ["final_backup", "disabled"] and out["action"] == "mixed"
    assert out["account"] == "ivanov@x.ru, old@x.ru" and len(services.queue.jobs) == 1
    for aid in (a, b):
        acc = db.get_account(aid)
        assert acc.hold_reason == "dismissed" and acc.dismissed_at
    assert dismissed_map(services).keys() >= {"ivanov@x.ru", "old@x.ru"}
    audit = db.list_audit(5)[0]["detail"]
    assert "«ivanov@x.ru»" in audit and "«old@x.ru»" in audit
    back = rehire_employee(services, emp, by="adm")
    assert back["enabled"] and len(back["accounts"]) == 2
    for aid in (a, b):
        acc = db.get_account(aid)
        assert not acc.hold_until and not acc.dismissed_at and not acc.auto_disabled


def test_password_file_is_not_applied_when_employee_has_several_mailboxes(services):
    db = services.db
    emp = db.create_employee(full_name="Иванов", email="ivanov@x.ru")
    a = _acc(db, "ivanov", password="")
    db.link_employee_account(emp, a)
    assert apply_passwords(services, [{"email": "ivanov@x.ru", "password": "Новый-1"}])["updated"] == 1
    db.link_employee_account(emp, _acc(db, "i.ivanov", password=""))
    res = apply_passwords(services, [{"email": "ivanov@x.ru", "password": "Новый-2"}])
    assert res["updated"] == 0 and res["not_found"] == ["ivanov@x.ru"]
    assert db.get_account(a).password == "Новый-1"


@pytest.fixture()
def admin(client):
    assert client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    return client


def test_employee_card_api(admin):
    svc = admin.app.state.services
    _template(svc)
    eid = admin.post("/api/employees", json={"full_name": "Иванов Иван", "email": "ivanov@x.ru",
                                             "create_account": True}).json()["id"]
    r = admin.post(f"/api/employees/{eid}/accounts", json={"email": "i.ivanov@x.ru"})
    assert r.status_code == 200 and r.json()["action"] == "created"
    card = admin.get(f"/api/employees/{eid}").json()
    assert [a["username"] for a in card["accounts"]] == ["ivanov@x.ru", "i.ivanov@x.ru"]
    assert card["account_count"] == 2 and card["accounts"][0]["enabled"] is False
    listing = admin.get("/api/employees").json()["employees"][0]
    assert len(listing["accounts"]) == 2
    # чужой ящик: без подтверждения — отказ, с move — перенос (и запись в аудите)
    other = admin.post("/api/employees", json={"full_name": "Петров", "email": "petrov@x.ru"}).json()["id"]
    sid = admin.post("/api/accounts", json={"name": "Склад", "host": "h", "port": 993, "username": "sklad@x.ru",
                                            "password": "p"}).json()["id"]
    assert admin.post(f"/api/employees/{other}/link-account?account_id={sid}").status_code == 200
    r = admin.post(f"/api/employees/{eid}/link-account?account_id={sid}")
    assert r.status_code == 400 and "уже принадлежит" in r.json()["message"]
    assert admin.post(f"/api/employees/{eid}/link-account?account_id={sid}&move=true").status_code == 200
    assert svc.db.get_account(sid).employee_id == eid
    assert any(a["action"] == "employee_account_moved" for a in admin.get("/api/audit").json())
    # владелец виден в списке ящиков
    names = {a["username"]: a["employee_name"] for a in admin.get("/api/accounts").json()}
    assert names["sklad@x.ru"] == "Иванов Иван"
    # отвязать один ящик; чужой ящик через этот путь — 404
    assert admin.delete(f"/api/employees/{eid}/accounts/{sid}").status_code == 200
    assert svc.db.get_account(sid).employee_id is None
    assert admin.delete(f"/api/employees/{other}/accounts/{card['accounts'][0]['id']}").status_code == 404
    assert admin.post(f"/api/employees/{eid}/accounts", json={"email": "не-адрес"}).status_code == 400


def test_sync_extra_address_never_hands_over_mail(services):
    """Строка выгрузки — не решение администратора: дополнительный адрес не привязывает уже заведённый
    ничей ящик и не забирает основной адрес другого сотрудника (опечатка в файле отдала бы чужую почту)."""
    _template(services)
    db = services.db
    director = _acc(db, "director@x.ru")                                  # ящик есть, ни за кем не числится
    rows = [{"row": 2, "full_name": "Клерк", "email": "clerk@x.ru", "extra_emails": ["director@x.ru"]},
            {"row": 3, "full_name": "Иванов", "email": "ivanov@x.ru", "extra_emails": ["petrov@x.ru"]},
            {"row": 4, "full_name": "Петров", "email": "petrov@x.ru"}]
    res = sync_employees(services, rows, create_accounts=True)
    assert db.get_account(director).employee_id is None
    assert any("director@x.ru" in w and "ни за кем не числятся" in w for w in res["warnings"])
    assert any("petrov@x.ru" in w and "основные адреса других сотрудников" in w for w in res["warnings"])
    petrov = db.get_employee_by_email("petrov@x.ru")
    assert [a["username"] for a in db.employee_accounts(petrov["id"])] == ["petrov@x.ru"]
    ivanov = db.get_employee_by_email("ivanov@x.ru")
    assert [a["username"] for a in db.employee_accounts(ivanov["id"])] == ["ivanov@x.ru"]
    # основной адрес по-прежнему находит уже заведённый ящик (как до 1.15.0)
    boss = _acc(db, "boss@x.ru")
    sync_employees(services, [{"row": 2, "full_name": "Босс", "email": "boss@x.ru"}], create_accounts=True)
    assert db.get_account(boss).employee_id == db.get_employee_by_email("boss@x.ru")["id"]


def test_extra_mailbox_login_when_template_ignores_address(admin):
    """Шаблон логина без адреса ({last_name}) дал бы второму ящику логин первого — тогда логин — сам адрес."""
    svc = admin.app.state.services
    _template(svc, **{"employees.account_username_template": "{last_name}"})
    rows = [{"row": 2, "full_name": "Иванов Иван", "email": "ivanov@x.ru", "extra_emails": ["i.ivanov@x.ru"]}]
    assert sync_employees(svc, rows, create_accounts=True)["accounts_created"] == 2
    emp = svc.db.get_employee_by_email("ivanov@x.ru")
    assert [a["username"] for a in svc.db.employee_accounts(emp["id"])] == ["Иванов", "i.ivanov@x.ru"]
    r = admin.post(f"/api/employees/{emp['id']}/accounts", json={"email": "ivan@x.ru"})
    assert r.json()["action"] == "created"
    assert svc.db.get_account(r.json()["account_id"]).username == "ivan@x.ru"
    # свой ящик ещё раз — «уже у сотрудника»; основной адрес другого — отказ
    assert admin.post(f"/api/employees/{emp['id']}/accounts", json={"email": "ivan@x.ru"}).json()["action"] == \
        "owned_already"
    svc.db.create_employee(full_name="Петров", email="petrov@x.ru")
    r = admin.post(f"/api/employees/{emp['id']}/accounts", json={"email": "petrov@x.ru"})
    assert r.status_code == 400 and "основной адрес сотрудника «Петров»" in r.json()["message"]


def test_rehire_keeps_hold_of_mailbox_from_another_dismissal(services):
    """Ящик уволенного, переданный коллеге, остаётся на удержании, даже когда сам коллега возвращается."""
    db = services.db
    services.db.set_settings_many({"employees.dismissed_action": "keep"})
    e1 = db.create_employee(full_name="Ушедший", email="ex@x.ru")
    e2 = db.create_employee(full_name="Коллега", email="col@x.ru")
    x, y = _acc(db, "ex@x.ru"), _acc(db, "col@x.ru")
    db.link_employee_account(e1, x)
    db.link_employee_account(e2, y)
    dismiss_employee(services, e1, by="adm")
    held = db.get_account(x).hold_until
    assert held
    db.link_employee_account(e2, x)                                         # переписку передали коллеге
    import time as _time
    _time.sleep(0.01)
    dismiss_employee(services, e2, by="adm")
    rehire_employee(services, e2, by="adm")
    assert db.get_account(y).hold_until == "" and not db.get_account(y).dismissed_at
    assert db.get_account(x).hold_until == held and db.get_account(x).hold_reason == "dismissed"
