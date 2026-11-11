"""Сотрудник с несколькими ящиками и восстановление отдельных писем (1.15.0).

* Вошедший паролем любого своего ящика видит архив всех ящиков своей карточки — пока числится
  работающим; отвязанный ящик закрывается сразу. Чужие ящики — по-прежнему 403.
* «Восстановить письмо / выбранные»: письма из архива возвращаются в тот же ящик (в исходные папки
  или в отдельную), повтор не дублирует. Администратор может вернуть их в ДРУГОЙ ящик — с записью в
  аудите; сотрудник — только в тот же. Владелец исходного ящика такое задание администратора не видит.
"""
import time

import pytest
from fastapi.testclient import TestClient

from imap_fake import ImapWorld, raw_message
from mailarchiver import models
from mailarchiver.imap import client as client_mod
from mailarchiver.models import JobType
from mailarchiver.queue.jobs import JobContext, handle_backup
from mailarchiver.security import sign_value
from mailarchiver.web.auth import COOKIE_NAME

HDR = {"X-Requested-With": "fetch"}
PW = "СложныйПароль1"


@pytest.fixture()
def world(monkeypatch):
    w = ImapWorld()
    monkeypatch.setattr(client_mod, "IMAPClient", w.client_class())
    return w


def _box(svc, world, login, n, *, name=None, folder="INBOX"):
    world.mailbox(login, "pw", {"Sent": []})
    for i in range(n):
        world.add(login, folder, raw_message(f"{login} письмо {i}", mid=f"<{login}.{i}@x>"))
    aid = svc.db.create_account(models.Account(name=name or login, host="imap.x.ru", username=login, password="pw"))
    job = svc.db.enqueue_job(JobType.BACKUP, aid, {}, 5, 1, "t")
    handle_backup(JobContext(svc, job, JobType.BACKUP, aid, {}))
    return aid


def _ids(svc, aid, folder="INBOX"):
    return [int(r["id"]) for r in svc.db.query("SELECT id FROM messages WHERE account_id=? AND folder=? ORDER BY id",
                                                (aid, folder))]


def _wait_job(svc, jid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = svc.db.get_job(jid)
        if row["status"] in ("success", "partial", "failed", "cancelled"):
            return row
        time.sleep(0.05)
    raise AssertionError(f"задание {jid} не завершилось: {svc.db.get_job(jid)['status']}")


@pytest.fixture()
def env(client, world):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200
    svc = client.app.state.services
    a = _box(svc, world, "ivanov@x.ru", 4, name="Иванов")
    b = _box(svc, world, "old.ivanov@x.ru", 3, name="Иванов (старый адрес)")
    c = _box(svc, world, "petrov@x.ru", 2, name="Петров")
    emp = svc.db.create_employee(full_name="Иванов Иван", email="ivanov@x.ru")
    svc.db.link_employee_account(emp, a)
    svc.db.link_employee_account(emp, b)
    svc.db.create_session("mbtoken", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=b)
    mb = TestClient(client.app, headers=HDR)
    mb.cookies.set(COOKIE_NAME, sign_value("mbtoken", svc.cfg.secret_key()))
    return client, mb, svc, emp, a, b, c


def test_employee_sees_all_own_mailboxes(env):
    admin, mb, svc, emp, a, b, c = env
    me = mb.get("/api/me").json()
    assert me["account_id"] == b and [x["id"] for x in me["accounts"]] == [b, a]   # вошедший — первым
    assert {x["id"] for x in mb.get("/api/accounts").json()} == {a, b}
    assert mb.get(f"/api/accounts/{a}/messages?folder=INBOX").status_code == 200
    assert mb.get(f"/api/accounts/{c}/messages?folder=INBOX").status_code == 403
    state = mb.get("/api/state").json()
    assert {x["id"] for x in state["accounts"]} == {a, b} and state["totals"]["messages"] == 7
    from mailarchiver.search import index_pending
    index_pending(svc)
    hits = mb.get("/api/search?q=письмо&scope=all").json()["results"]
    assert {h["account_id"] for h in hits} == {a, b}
    assert {h["account_id"] for h in mb.get(f"/api/search?q=письмо&account_id={c}").json()["results"]} == {b}
    # задание, запущенное под одним логином, — «своё» и при входе другим
    jid = svc.db.enqueue_job("export", a, {}, created_by="ivanov@x.ru")
    jobs = {j["id"]: j for j in mb.get("/api/jobs").json()}
    assert jid in jobs and jobs[jid]["created_by"] == "ivanov@x.ru"
    assert mb.post(f"/api/jobs/{jid}/cancel").status_code == 200
    # отвязали ящик — доступ закрыт сразу; уволен — остаётся только ящик, которым вошёл
    svc.db.unlink_employee_account(a)
    assert mb.get(f"/api/accounts/{a}/messages?folder=INBOX").status_code == 403
    svc.db.link_employee_account(emp, a)
    assert mb.get(f"/api/accounts/{a}/messages?folder=INBOX").status_code == 200
    svc.db.update_employee(emp, status="archived")
    assert [x["id"] for x in mb.get("/api/me").json()["accounts"]] == [b]
    assert mb.get(f"/api/accounts/{a}/messages?folder=INBOX").status_code == 403


def test_restore_chosen_messages_into_same_mailbox(env, world):
    admin, mb, svc, emp, a, b, c = env
    picked = _ids(svc, a)[:2]
    for uid in list(world.uids("ivanov@x.ru", "INBOX"))[:2]:
        world.boxes["ivanov@x.ru"]["INBOX"].messages.pop(uid)          # письма пропали из ящика
    assert len(world.uids("ivanov@x.ru", "INBOX")) == 2
    r = mb.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked})
    assert r.status_code == 200, r.text
    job = _wait_job(svc, r.json()["job_id"])
    assert job["status"] == "success", job["error"]
    assert len(world.uids("ivanov@x.ru", "INBOX")) == 4           # вернулись в исходную папку
    jobs = {j["id"]: j for j in mb.get("/api/jobs").json()}
    assert jobs[job["id"]]["type_label"] == "Восстановление писем (2)"
    # повтор — без дублей (письма уже на месте)
    job = _wait_job(svc, mb.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked}).json()["job_id"])
    assert job["status"] == "success" and len(world.uids("ivanov@x.ru", "INBOX")) == 4
    # в отдельную папку
    r = admin.post(f"/api/accounts/{a}/restore-messages",
                   json={"message_ids": picked[:1], "target_mode": "single", "target_folder": "Из архива"})
    assert _wait_job(svc, r.json()["job_id"])["status"] == "success"
    assert len(world.uids("ivanov@x.ru", "Из архива")) == 1


def test_restore_into_other_mailbox_is_admin_only(env, world):
    admin, mb, svc, emp, a, b, c = env
    picked = _ids(svc, a)[:3]
    # сотрудник — только в тот же ящик (и в свой другой ящик — тоже нет)
    for target in (c, b):
        r = mb.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked, "target_account_id": target})
        assert r.status_code == 403
    # письма другого ящика под видом своего — отказ
    r = mb.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": _ids(svc, c)[:1]})
    assert r.status_code == 400 and "нет в архиве" in r.json()["message"]
    assert mb.post(f"/api/accounts/{c}/restore-messages", json={"message_ids": _ids(svc, c)[:1]}).status_code == 403
    # администратор — переписку Иванова в ящик Петрова, в папки с префиксом
    r = admin.post(f"/api/accounts/{a}/restore-messages",
                   json={"message_ids": picked, "target_account_id": c, "target_mode": "prefixed",
                         "target_prefix": "Иванов"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    assert _wait_job(svc, jid)["status"] == "success"
    assert len(world.uids("petrov@x.ru", "Иванов/INBOX")) == 3
    assert len(world.uids("ivanov@x.ru", "INBOX")) == 4                          # в исходном ящике — без изменений
    audit = [x for x in admin.get("/api/audit").json() if x["action"] == "restore_messages"][0]
    assert "«Иванов»" in audit["detail"] and "petrov@x.ru" in audit["detail"]
    admin_view = {j["id"]: j for j in admin.get("/api/jobs").json()}[jid]
    assert admin_view["account_name"] == "Иванов → Петров"
    # сотрудник (владелец исходного ящика) такое задание не видит вовсе
    assert jid not in {j["id"] for j in mb.get("/api/jobs").json()}
    assert mb.get(f"/api/jobs/{jid}").status_code == 403
    assert mb.get(f"/api/jobs/{jid}/events").status_code == 403


def test_restore_messages_checks(env):
    admin, mb, svc, emp, a, b, c = env
    picked = _ids(svc, a)[:1]
    assert admin.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": []}).status_code == 422
    r = admin.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked, "target_mode": "single",
                                                                "target_folder": " "})
    assert r.status_code == 400
    # в ящик без пароля — отказ сразу, а не упавшее задание
    nopw = svc.db.create_account(models.Account(name="Без пароля", host="h", username="np@x.ru", password=""))
    r = admin.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked, "target_account_id": nopw})
    assert r.status_code == 400 and "не задан пароль" in r.json()["message"]
    # оператор писем не видит — и вернуть их не может
    assert admin.post("/api/users", json={"username": "op", "password": PW, "role": "operator"}).status_code == 200
    op = TestClient(admin.app, headers=HDR)
    assert op.post("/api/login", json={"username": "op", "password": PW}).status_code == 200
    assert op.post(f"/api/accounts/{a}/restore-messages", json={"message_ids": picked}).status_code == 403
    # письмо, удалённое из архива после выбора, — в итоге задания, остальные восстанавливаются
    ids = _ids(svc, a)[:2]
    svc.queue.stop()
    jid = admin.post(f"/api/accounts/{a}/restore-messages",
                     json={"message_ids": ids, "target_mode": "single", "target_folder": "Возврат"}).json()["job_id"]
    svc.db.execute("DELETE FROM messages WHERE id=?", (ids[0],))
    svc.queue.start()
    job = _wait_job(svc, jid)
    assert job["status"] == "partial"
    assert any("в архиве уже нет 1" in e["message"]
               for e in admin.get(f"/api/jobs/{jid}/events").json())


def test_interrupted_message_restore_continues(services, world):
    """Прерванное перезапуском службы восстановление выбранных писем продолжается со следующего письма."""
    import json as _json

    from mailarchiver.errors import JobCancelled
    from mailarchiver.queue.jobs import handle_restore
    svc = services
    a = _box(svc, world, "ivanov@x.ru", 5)
    picked = _ids(svc, a)[1:4]
    params = {"message_ids": picked, "target_mode": "single", "target_folder": "Возврат", "check_duplicates": False}
    jid = svc.db.enqueue_job(JobType.RESTORE, a, params, 5, 1, "t")
    sent = {"n": 0}
    orig = client_mod.IMAPClient.append

    def counting(self, folder, msg, flags=(), msg_time=None):
        sent["n"] += 1
        return orig(self, folder, msg, flags=flags, msg_time=msg_time)

    client_mod.IMAPClient.append = counting
    try:
        ctx = JobContext(svc, jid, JobType.RESTORE, a, params)
        ctx.is_cancelled = lambda: sent["n"] >= 1               # служба останавливается после первого письма
        with pytest.raises(JobCancelled):
            handle_restore(ctx)
        assert _json.loads(svc.db.get_meta(f"restore_ckpt:{jid}"))["last_id"] == picked[0]
        res = handle_restore(JobContext(svc, jid, JobType.RESTORE, a, params))
    finally:
        client_mod.IMAPClient.append = orig
    assert res["restored"] == 3 and sent["n"] == 3                  # без проверки дублей — и всё равно без повторов
    assert len(world.uids("ivanov@x.ru", "Возврат")) == 3


def test_disabled_sibling_mailbox_is_read_only(env):
    """Выключенный администратором ящик сотрудник, вошедший другим своим ящиком, только читает: проверка,
    копирование и восстановление — 403 (повторные неудачные входы на сервер заблокировали бы адрес архива)."""
    admin, mb, svc, emp, a, b, c = env
    acc = svc.db.get_account(a)
    acc.enabled = False
    svc.db.update_account(acc, update_password=False, update_oauth_secret=False, update_oauth_token=False)
    assert mb.get(f"/api/accounts/{a}/messages?folder=INBOX").status_code == 200
    for path, body in ((f"/api/accounts/{a}/test", None), (f"/api/accounts/{a}/backup", None),
                       (f"/api/accounts/{a}/restore-messages", {"message_ids": _ids(svc, a)[:1]})):
        r = mb.post(path, json=body) if body is not None else mb.post(path)
        assert r.status_code == 403 and "выключен" in r.json()["detail"], (path, r.text)
    assert mb.post(f"/api/accounts/{b}/test").status_code == 200          # включённый — как прежде


def test_moving_mailbox_closes_its_sessions(env):
    """Перенос ящика другому сотруднику завершает сеансы, открытые его паролем: иначе прежний владелец сразу
    видел бы архив нового."""
    admin, mb, svc, emp, a, b, c = env
    other = svc.db.create_employee(full_name="Петров", email="petrov@x.ru")
    svc.db.link_employee_account(other, c)
    assert mb.get("/api/me").status_code == 200
    r = admin.post(f"/api/employees/{other}/link-account?account_id={b}&move=true")
    assert r.status_code == 200
    assert mb.get("/api/me").status_code == 401
    # привязка нового ящика к тому же сотруднику сеансы других ящиков не трогает
    svc.db.create_session("mbtoken2", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=a)
    mb.cookies.set(COOKIE_NAME, sign_value("mbtoken2", svc.cfg.secret_key()))
    d = svc.db.create_account(models.Account(name="Ещё", host="imap.x.ru", username="i2@x.ru", password="pw"))
    svc.db.link_employee_account(emp, d)
    assert {x["id"] for x in mb.get("/api/me").json()["accounts"]} == {a, d}


def test_hidden_admin_restore_does_not_block_employee(services, world):
    """Задание администратора, возвращающее письма сотрудника в чужой ящик, сотруднику не видно — и не мешает
    его собственному восстановлению (раньше — «такое задание уже выполняется» про невидимое задание)."""
    from mailarchiver.errors import ValidationError
    from mailarchiver.web import api as api_mod
    from mailarchiver.web.auth import mailbox_user
    svc = services
    a = _box(svc, world, "ivanov@x.ru", 2)
    c = _box(svc, world, "petrov@x.ru", 1)
    emp = svc.db.create_employee(full_name="Иванов", email="ivanov@x.ru")
    svc.db.link_employee_account(emp, a)
    user = mailbox_user(svc, svc.db.get_account(a))
    svc.db.enqueue_job(JobType.RESTORE, a, {"message_ids": _ids(svc, a)[:1], "target_account_id": c},
                                5, 1, "adm")
    api_mod._mailbox_flood_guard(svc, user, JobType.RESTORE, a)          # очередь не запущена: задание ждёт
    svc.db.enqueue_job(JobType.RESTORE, a, {"message_ids": _ids(svc, a)[:1]}, 5, 1, "adm")
    with pytest.raises(ValidationError):                                 # своё (в тот же ящик) — по-прежнему
        api_mod._mailbox_flood_guard(svc, user, JobType.RESTORE, a)


def test_export_of_sibling_mailbox_can_be_deleted_from_list(env):
    admin, mb, svc, emp, a, b, c = env
    jid = svc.db.enqueue_job(JobType.EXPORT, a, {}, 5, 1, "ivanov@x.ru")          # запущено под другим логином
    eid = svc.db.create_export(a, "builtin", "pst", "/nonexistent/x.pst", {}, jid)
    row = next(x for x in mb.get("/api/exports").json() if x["id"] == eid)
    assert row["can_delete"] is True


def test_new_card_does_not_report_someone_elses_mailbox(admin_client):
    svc = admin_client.app.state.services
    svc.db.set_settings_many({"employees.account_host": "imap.x.ru"})
    owner = svc.db.create_employee(full_name="Склад", email="sklad.chief@x.ru")
    sid = svc.db.create_account(models.Account(name="Склад", host="h", username="sklad@x.ru", password="p"))
    svc.db.link_employee_account(owner, sid)
    r = admin_client.post("/api/employees", json={"full_name": "Иванов", "email": "sklad@x.ru",
                                                  "create_account": True}).json()
    assert r["account_id"] is None and "Склад" in r["warning"]
    assert svc.db.get_account(sid).employee_id == owner


@pytest.fixture()
def admin_client(client):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200
    return client
