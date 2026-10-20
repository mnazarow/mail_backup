"""Раздел «Ящики на сервере», 1.13.0: повтор для неудачных, уволенные с открытым входом, передача адреса
коллеге, действия по расписанию (только по согласию), динамика занятого места и прогноз до квоты.

Axigen — учебные CLI и FTP Backup (tests/axigen_fake.py), IMAP — учебный сервер в памяти (tests/imap_fake.py).
"""
from datetime import date, timedelta

import pytest

from axigen_fake import FakeAxigenCLI, FakeAxigenFTP, FtpWorld, World, message
from imap_fake import ImapWorld, raw_message
from mailarchiver import models
from mailarchiver.axigen import monitor
from mailarchiver.axigen import store as axstore
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.models import AuthType, JobStatus, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, schedules, usage
from mailarchiver.serverops import targets as tg

ADMIN = {"username": "tester", "role": "admin"}
IV, PE = "ax:ivanov@company.ru", "ax:petrov@company.ru"


def ftp_tree():
    return {"domains": {"company.ru": {
        "ivanov": {"Inbox": {"1.eml": message("Первое")}},
        "petrov": {"Inbox": {"p.eml": message("Петрову")}},
        "postmaster": {"Inbox": {}},
    }}}


@pytest.fixture()
def ax(services):
    world = World()
    world.domains["company.ru"].accounts["ivanov"].folders = {"INBOX": (1, 1024)}
    world.domains["company.ru"].accounts["petrov"].folders = {"INBOX": (1, 1024)}
    with FakeAxigenCLI(world) as cli, FakeAxigenFTP(FtpWorld(ftp_tree())) as ftp:
        services.db.set_settings_many({
            "axigen.enabled": True, "axigen.host": "127.0.0.1", "axigen.cli_port": cli.port,
            "axigen.ftp_port": ftp.port, "axigen.admin_user": "admin", "axigen.admin_password": "Adm1nPass",
            "axigen.timeout_s": 5, "axigen.imap_host": "mx.company.ru", "axigen.allow_changes": True})
        services.apply_runtime_settings()
        run_job(services, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
        yield type("Ax", (), {"world": world, "svc": services})


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    ctx = JobContext(svc, job_id, job_type, None, params)
    return HANDLERS[job_type](ctx)


def act(svc, action, keys, op_id=None, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": op_id})
    return res, {i["key"]: i for i in res["items"]}


def ftp_mailbox(svc, username, **kw):
    data = dict(name=username, host="mx.company.ru", username=username, password="",
                auth_type=AuthType.AXIGEN_FTP, retention_days=0)
    data.update(kw)
    return svc.db.create_account(models.Account(**data))


def launch(svc, action, keys, params=None, monkeypatch=None):
    """Запуск как из интерфейса: проверка, запись в историю, задание (выполняется сразу)."""
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    pv = base.run(svc, ADMIN, action, keys, params or {}, preview=True)
    res = base.run(svc, ADMIN, action, keys, params or {}, preview=False, confirm=pv["confirm_value"])
    job_id = svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    # в истории — номер задания, которое выполнилось на самом деле
    svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                   (f'"job_id": {job_id}', res["op_id"]))
    ctx = JobContext(svc, job_id, JobType.SERVER_ACTION, None, queued[0][2])
    out = HANDLERS[JobType.SERVER_ACTION](ctx)
    svc.db.finish_job(job_id, out["final_status"], out)
    return res["op_id"], out


# ---------------------------------------------------------------------------
#  Повтор для неудачных
# ---------------------------------------------------------------------------
def test_retry_info_lists_failed_and_unprocessed_mailboxes(ax, monkeypatch):
    from mailarchiver.axigen import actions as axactions
    real = axactions.set_account_flag

    def flaky(cli, email, attr, value):
        if email.startswith("petrov"):
            raise ValidationError("сервер отказал")
        return real(cli, email, attr, value)
    monkeypatch.setattr(axactions, "set_account_flag", flaky)
    op_id, out = launch(ax.svc, "ax_must_change", [IV, PE, "ax:postmaster@company.ru"], {"value": "yes"},
                        monkeypatch)
    assert out["ok"] == 2 and out["failed"] == 1
    info = base.retry_info(ax.svc, op_id)
    assert info["action"] == "ax_must_change" and info["keys"] == [PE] and info["params"] == {"value": "yes"}


def test_retry_info_drops_secrets_and_includes_unprocessed_after_cancel(ax, monkeypatch):
    queued = []
    monkeypatch.setattr(ax.svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    res = base.run(ax.svc, ADMIN, "ax_password", [IV, PE], {"mode": "same", "password": "Temp12345"},
                   preview=False)
    params = queued[0][2]
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, params, 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    ctx = JobContext(ax.svc, job_id, JobType.SERVER_ACTION, None, params)
    calls = []
    monkeypatch.setattr(ctx, "is_cancelled", lambda: calls.append(1) or len(calls) > 1)
    monkeypatch.setattr(ax.svc.db, "is_cancel_requested", lambda _id: True)
    out = HANDLERS[JobType.SERVER_ACTION](ctx)
    ax.svc.db.finish_job(job_id, out["final_status"], out)
    info = base.retry_info(ax.svc, res["op_id"])
    assert info["keys"] == [PE]                       # отменено до него
    assert info["params"]["password"] == "" and "Temp12345" not in str(info)


def test_retry_info_for_account_creation_keeps_names(ax, monkeypatch):
    from mailarchiver.serverops import create
    queued = []
    monkeypatch.setattr(ax.svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    res = create.run(ax.svc, ADMIN, {"text": "new1@company.ru; Анна; Смирнова\nnew2@company.ru; Олег; Орлов",
                                     "password_mode": "random_file"}, preview=False)
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    from mailarchiver.axigen import actions as axactions
    real = axactions.create_account

    def flaky(cli, email, *a, **k):
        if email.startswith("new1"):
            raise ValidationError("сервер отказал")
        return real(cli, email, *a, **k)
    monkeypatch.setattr(axactions, "create_account", flaky)
    ctx = JobContext(ax.svc, job_id, JobType.SERVER_ACTION, None, queued[0][2])
    out = HANDLERS[JobType.SERVER_ACTION](ctx)
    ax.svc.db.finish_job(job_id, out["final_status"], out)
    info = base.retry_info(ax.svc, res["op_id"])
    assert info["action"] == create.KEY and info["lines"] == "new1@company.ru; Анна; Смирнова"
    assert info["params"]["password_mode"] == "random_file" and info["blank"] == []


def test_retry_after_service_restart_offers_only_unfinished_mailboxes(ax, monkeypatch):
    """Служба упала посреди задания: итога нет, но сделанные ящики не предлагаются снова."""
    from mailarchiver.errors import JobCancelled
    from mailarchiver.serverops import axigen_ops
    queued = []
    monkeypatch.setattr(ax.svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    keys = [IV, PE, "ax:postmaster@company.ru"]
    res = base.run(ax.svc, ADMIN, "ax_password", keys, {"mode": "random_file"}, preview=False)
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    real = axigen_ops.PasswordAction.execute

    def dies_on_petrov(self, ctx, t):
        if t.key == PE:
            raise JobCancelled("служба останавливается")            # остановка службы, не отмена
        return real(self, ctx, t)
    monkeypatch.setattr(axigen_ops.PasswordAction, "execute", dies_on_petrov)
    ctx = JobContext(ax.svc, job_id, JobType.SERVER_ACTION, None, queued[0][2])
    with pytest.raises(JobCancelled):
        HANDLERS[JobType.SERVER_ACTION](ctx)
    ax.svc.db.finish_job(job_id, JobStatus.FAILED, error="Задание прервано перезапуском службы")
    info = base.retry_info(ax.svc, res["op_id"])
    assert IV not in info["keys"]                                  # ему пароль уже выдан
    assert info["keys"] == [PE, "ax:postmaster@company.ru"] and info["uncertain"] == [PE]
    full = base.history_item_full(ax.svc, res["op_id"])
    assert full["retry"] == 2


def test_retry_of_old_job_without_marks_refuses_when_result_is_lost(ax, monkeypatch):
    queued = []
    monkeypatch.setattr(ax.svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    res = base.run(ax.svc, ADMIN, "ax_must_change", [IV, PE], {"value": "yes"}, preview=False)
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    ax.svc.db.finish_job(job_id, JobStatus.FAILED, error="прервано")          # как у задания 1.12 после сбоя
    ax.svc.db.execute("DELETE FROM server_op_items WHERE op_id=?", (res["op_id"],))   # операция 1.12 — без отметок
    with pytest.raises(ValidationError, match="неизвестно"):
        base.retry_info(ax.svc, res["op_id"])
    assert base.history_item_full(ax.svc, res["op_id"])["retry"] == 0


def test_retry_keeps_flags_and_full_long_params(ax, monkeypatch):
    queued = []
    monkeypatch.setattr(ax.svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    res = base.run(ax.svc, ADMIN, "ax_block", [IV], {"random_password": True, "final_copy": False}, preview=False)
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    ax.svc.db.finish_job(job_id, JobStatus.CANCELLED, {"summary": "отменено", "items": []})
    from mailarchiver.serverops import progress
    progress.started(ax.svc.db, res["op_id"])
    info = base.retry_info(ax.svc, res["op_id"])
    assert info["params"]["random_password"] is True and info["blank"] == []
    # длинный список папок: в истории укорочен, в повтор идёт полный — из задания
    folders = "\n".join(f"Проекты/Клиент-{i:02d}" for i in range(30))
    queued.clear()
    world = ImapWorld()
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    acc = ax.svc.db.create_account(models.Account(name="u1", host="imap.test", username="u1@test.ru", password="pw",
                                                  auth_type=AuthType.PASSWORD))
    res = base.run(ax.svc, ADMIN, "imap_mkdir", [f"mb:{acc}"], {"folders": folders}, preview=False)
    assert base.history_item(ax.svc, res["op_id"])["params"]["folders"].endswith("…")
    job_id = ax.svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    ax.svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                      (f'"job_id": {job_id}', res["op_id"]))
    ax.svc.db.finish_job(job_id, JobStatus.CANCELLED, {"summary": "отменено", "items": []})
    progress.started(ax.svc.db, res["op_id"])
    info = base.retry_info(ax.svc, res["op_id"])
    assert info["keys"] == [f"mb:{acc}"] and info["params"]["folders"] == folders


# ---------------------------------------------------------------------------
#  Уволенные с открытым входом
# ---------------------------------------------------------------------------
def test_dismissed_employees_with_open_login_are_flagged(ax):
    from mailarchiver import employees
    emp = ax.svc.db.create_employee(full_name="Петров Пётр", email="petrov@company.ru")
    employees.dismiss_employee(ax.svc, emp, reason="тест")
    rows = {r["key"]: r for r in tg.overview(ax.svc)["rows"]}
    assert rows[PE]["dismissed_at"] and not rows[IV]["dismissed_at"]
    probs = {p["key"]: p for p in monitor.problems(ax.svc)}
    assert "dismissed_open" in probs and "petrov@company.ru" in probs["dismissed_open"]["text"]
    # вход закрыт — предупреждения больше нет
    res, items = act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    assert items[PE]["status"] == "success", items[PE]
    assert "dismissed_open" not in {p["key"] for p in monitor.problems(ax.svc)}


# ---------------------------------------------------------------------------
#  Передать адрес коллеге
# ---------------------------------------------------------------------------
def test_delete_with_transfer_makes_address_an_alias_of_colleague(ax):
    ftp_mailbox(ax.svc, "petrov@company.ru")
    act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    pv = base.run(ax.svc, ADMIN, "ax_delete", [PE], {"forward_to": "IVANOV@company.ru"}, preview=True)
    assert pv["counts"]["ok"] == 1 and "→ ivanov@company.ru" in pv["results"][0]["detail"]
    res, items = act(ax.svc, "ax_delete", [PE], forward_to="ivanov@company.ru", forward_aliases=True)
    assert items[PE]["status"] == "success", items[PE]
    assert "petrov" not in ax.world.domains["company.ru"].accounts
    assert "petrov" in ax.world.domains["company.ru"].accounts["ivanov"].aliases
    assert "petrov@company.ru" in axstore.get_account(ax.svc.db, "ivanov@company.ru")["aliases"]


def test_transfer_is_refused_for_other_domain_or_unknown_colleague(ax):
    ftp_mailbox(ax.svc, "petrov@company.ru")
    with pytest.raises(ValidationError, match="нет на сервере"):
        base.run(ax.svc, ADMIN, "ax_delete", [PE], {"forward_to": "nobody@company.ru"}, preview=True)
    # коллега не заблокирован — его самого не удалят, передавать можно
    pv = base.run(ax.svc, ADMIN, "ax_delete", [PE, IV], {"forward_to": "ivanov@company.ru"}, preview=True)
    assert pv["counts"]["skip"] == 2
    ftp_mailbox(ax.svc, "ivanov@company.ru")
    act(ax.svc, "ax_block", [PE, IV], final_copy=False, keep_receiving=True)
    pv = base.run(ax.svc, ADMIN, "ax_delete", [PE, IV], {"forward_to": "ivanov@company.ru"}, preview=True)
    res = {r["key"]: r for r in pv["results"]}
    assert res[PE]["status"] == "ok" and res[IV]["status"] == "skip"
    assert "коллега, которому передаются адреса" in res[IV]["detail"]
    # коллега в другом домене — псевдоним бывает только в своём домене
    from axigen_fake import FakeAccount, FakeDomain
    ax.world.domains["other.ru"] = FakeDomain(accounts={"boss": FakeAccount()})
    run_job(ax.svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
    pv = base.run(ax.svc, ADMIN, "ax_delete", [PE], {"forward_to": "boss@other.ru"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "в другом домене" in pv["results"][0]["detail"]


def test_transferred_address_does_not_make_colleague_dismissed(ax):
    from mailarchiver import employees
    emp = ax.svc.db.create_employee(full_name="Петров Пётр", email="petrov@company.ru")
    employees.dismiss_employee(ax.svc, emp, reason="тест")
    pe_box = ftp_mailbox(ax.svc, "petrov@company.ru")
    act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    res, items = act(ax.svc, "ax_delete", [PE], forward_to="ivanov@company.ru", forward_aliases=True)
    assert items[PE]["status"] == "success", items[PE]
    rows = {r["key"]: r for r in tg.overview(ax.svc)["rows"]}
    assert not rows[IV]["dismissed_at"]                         # коллега не уволен
    assert not rows[IV]["mailbox"]                              # и ящик уволенного к нему не «переехал»
    assert (rows[PE]["mailbox"] or {}).get("id") == pe_box
    assert "dismissed_open" not in {p["key"] for p in monitor.problems(ax.svc)}


def test_delete_is_refused_when_colleague_disappeared_after_sync(ax):
    ftp_mailbox(ax.svc, "petrov@company.ru")
    act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    del ax.world.domains["company.ru"].accounts["ivanov"]          # удалили в WebAdmin, сверки не было
    res, items = act(ax.svc, "ax_delete", [PE], forward_to="ivanov@company.ru", forward_aliases=True)
    assert items[PE]["status"] == "skipped" and "адрес передать некому" in items[PE]["detail"]
    assert "petrov" in ax.world.domains["company.ru"].accounts      # учётная запись не удалена


def test_failed_transfer_after_delete_is_audited_and_retried_as_forward(ax, monkeypatch):
    ftp_mailbox(ax.svc, "petrov@company.ru")
    act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    ax.world.fail["ADD Alias"] = "Internal error"
    op_id, out = launch(ax.svc, "ax_delete", [PE], {"forward_to": "ivanov@company.ru"}, monkeypatch)
    item = out["items"][0]
    assert item["status"] == "failed" and "адрес коллеге НЕ передан" in item["detail"]
    assert "petrov" not in ax.world.domains["company.ru"].accounts
    audit = [r["detail"] for r in ax.svc.db.query("SELECT detail FROM audit WHERE action='srv_ax_delete'")]
    assert any("petrov@company.ru: учётная запись удалена" in d for d in audit)
    info = base.retry_info(ax.svc, op_id)
    assert info["action"] == "ax_forward" and info["keys"] == [PE]
    assert info["params"] == {"to": "ivanov@company.ru", "with_aliases": True}
    del ax.world.fail["ADD Alias"]
    res, items = act(ax.svc, "ax_forward", [PE], **info["params"])
    assert items[PE]["status"] == "success", items[PE]
    assert "petrov" in ax.world.domains["company.ru"].accounts["ivanov"].aliases


def test_forward_action_for_account_already_deleted_on_server(ax):
    pv = base.run(ax.svc, ADMIN, "ax_forward", [PE], {"to": "ivanov@company.ru"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "ещё есть на сервере" in pv["results"][0]["detail"]
    # учётную запись удалили вручную в WebAdmin; сверка это заметила
    del ax.world.domains["company.ru"].accounts["petrov"]
    run_job(ax.svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
    res, items = act(ax.svc, "ax_forward", [PE], to="ivanov@company.ru", with_aliases=True)
    assert items[PE]["status"] == "success", items[PE]
    assert "petrov" in ax.world.domains["company.ru"].accounts["ivanov"].aliases
    res, items = act(ax.svc, "ax_forward", [PE], to="ivanov@company.ru", with_aliases=True)
    assert items[PE]["status"] == "skipped" and "уже переданы" in items[PE]["detail"]


# ---------------------------------------------------------------------------
#  По расписанию — только по согласию
# ---------------------------------------------------------------------------
def test_when_to_cron():
    assert schedules.when_to_cron({"freq": "daily", "time": "2:05"}) == "5 2 * * *"
    assert schedules.when_to_cron({"freq": "weekly", "time": "23:00", "dow": 7}) == "0 23 * * 0"
    assert schedules.when_to_cron({"freq": "monthly", "time": "03:30", "day": 1}) == "30 3 1 * *"
    for bad in ({"freq": "hourly", "time": "1:00"}, {"freq": "daily", "time": "25:00"},
                {"freq": "monthly", "time": "01:00", "day": 31}):
        with pytest.raises(ValidationError):
            schedules.when_to_cron(bad)


def test_schedule_needs_consent_confirmation_and_allowed_action(ax):
    when = {"freq": "monthly", "time": "03:00", "day": 1}
    with pytest.raises(ValidationError, match="по расписанию не выполняется"):
        schedules.create(ax.svc, ADMIN, action_key="ax_password", keys=[IV], params={}, when=when, consent=True)
    with pytest.raises(ValidationError, match="Подтвердите"):
        schedules.create(ax.svc, ADMIN, action_key="imap_trash", keys=[IV], params={"days": 30}, when=when,
                         consent=False)
    with pytest.raises(ValidationError, match="введите число"):
        schedules.create(ax.svc, ADMIN, action_key="imap_trash", keys=[IV], params={"days": 30}, when=when,
                         consent=True, confirm="")
    out = schedules.create(ax.svc, ADMIN, action_key="imap_trash", keys=[IV, PE], params={"days": 30}, when=when,
                           consent=True, confirm="2")
    items = schedules.list_all(ax.svc)
    assert [i["id"] for i in items] == [out["id"]] and items[0]["count"] == 2
    assert items[0]["cron"] == "0 3 1 * *" and items[0]["consent_by"] == "tester"
    audit = " ".join(r["detail"] for r in ax.svc.db.query("SELECT detail FROM audit WHERE action LIKE 'srv_schedule%'"))
    assert f"расписание №{out['id']}" in audit and "согласие" in audit
    schedules.set_enabled(ax.svc, ADMIN, out["id"], False)
    assert schedules.list_all(ax.svc)[0]["enabled"] is False
    schedules.delete(ax.svc, ADMIN, out["id"])
    assert schedules.list_all(ax.svc) == []


def test_scheduled_run_goes_to_history_and_rechecks_conditions(ax):
    out = schedules.create(ax.svc, ADMIN, action_key="report", keys=[IV], params={},
                           when={"freq": "weekly", "time": "06:00", "dow": 1}, consent=True)
    import json
    params = json.loads(ax.svc.db.get_schedule(out["id"])["options"])
    res = run_job(ax.svc, JobType.SERVER_ACTION, params)            # так его ставит планировщик
    assert res["ok"] == 1
    hist = base.history(ax.svc)
    assert hist[0]["user"] == f"расписание №{out['id']}" and hist[0]["action"] == "report"
    launch_rows = base.history_item(ax.svc, hist[0]["id"])["results"]
    assert launch_rows[0]["username"] == "ivanov@company.ru" and launch_rows[0]["name"] == "Иван Иванов"
    # расписание без отметки согласия (старое или испорченное) не выполняется
    params.pop("schedule")
    with pytest.raises(ValidationError, match="согласия"):
        run_job(ax.svc, JobType.SERVER_ACTION, params)


# ---------------------------------------------------------------------------
#  Динамика места
# ---------------------------------------------------------------------------
def test_usage_trend_and_forecast(services):
    db = services.db
    today = date.today()
    mb = 1024 * 1024
    for i, days_ago in enumerate((60, 30, 0)):
        usage.record(db, "ax:a@x.ru", (100 + 30 * i) * mb, 400 * mb, day=(today - timedelta(days=days_ago)).isoformat())
    usage.record(db, "ax:b@x.ru", 10 * mb, 400 * mb, day=today.isoformat())       # одна точка — без прогноза
    t = usage.trends(db)
    assert set(t) == {"ax:a@x.ru"}
    assert abs(t["ax:a@x.ru"]["per_month"] - 30 * mb) < mb and t["ax:a@x.ru"]["days_to_quota"] == 240
    usage.record(db, "ax:a@x.ru", 395 * mb, 400 * mb, day=today.isoformat())
    assert "ax:a@x.ru" in usage.soon_full(db)
    old = (today - timedelta(days=usage.KEEP_DAYS + 5)).isoformat()
    usage.record(db, "ax:a@x.ru", mb, None, day=old)
    assert usage.purge_old(db) == 1
    # почти не растёт — прогноза «на миллиарды дней» нет; занято больше квоты — 0, даже без роста
    for days_ago in (30, 0):
        usage.record(db, "ax:c@x.ru", 100 * mb, 400 * mb, day=(today - timedelta(days=days_ago)).isoformat())
        usage.record(db, "ax:d@x.ru", 500 * mb, 400 * mb, day=(today - timedelta(days=days_ago)).isoformat())
    t = usage.trends(db)
    assert t["ax:c@x.ru"]["days_to_quota"] is None and t["ax:d@x.ru"]["days_to_quota"] == 0
    # SQL-суммы совпадают с прямым расчётом по точкам
    pts = usage.history(db, "ax:a@x.ru", usage.WINDOW_DAYS)
    assert abs(usage._trend(pts)["per_day"] - t["ax:a@x.ru"]["per_day"]) < 1e-6


def test_sync_records_usage_and_rows_show_trend(ax):
    first = ax.svc.db.query("SELECT key, used_bytes FROM server_usage ORDER BY key")
    assert {r["key"] for r in first} >= {IV, PE}
    # история за прошлые недели — рост виден в строке раздела
    for weeks in (2, 1):
        ax.svc.db.execute("INSERT OR REPLACE INTO server_usage(key, day, used_bytes, quota_bytes) VALUES(?,?,?,?)",
                          (IV, (date.today() - timedelta(weeks=weeks)).isoformat(), 1, 1024 * 1024 * 1024))
    rows = {r["key"]: r for r in tg.overview(ax.svc)["rows"]}
    assert rows[IV]["trend"] and rows[IV]["trend"]["per_month"] > 0 and rows[PE]["trend"] is None


def test_report_records_usage_of_imap_mailbox(services, monkeypatch):
    world = ImapWorld()
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    world.mailbox("u1@test.ru")
    world.quota["u1@test.ru"] = (2048, 10240)
    world.add("u1@test.ru", "INBOX", raw_message("x"))
    acc = services.db.create_account(models.Account(name="u1", host="imap.test", username="u1@test.ru", password="pw",
                                                    auth_type=AuthType.PASSWORD))
    res, items = act(services, "report", [f"mb:{acc}"])
    assert items[f"mb:{acc}"]["status"] == "success", items
    row = services.db.query_one("SELECT used_bytes, quota_bytes FROM server_usage WHERE key=?", (f"mb:{acc}",))
    assert row["used_bytes"] == 2048 * 1024 and row["quota_bytes"] == 10240 * 1024


# ---------------------------------------------------------------------------
#  Второй разбор: повтор без единого тронутого ящика, псевдонимы уволенных
# ---------------------------------------------------------------------------
def queue_only(svc, action, keys, params, monkeypatch):
    """Запуск как из интерфейса, но задание только поставлено в очередь (не выполнено)."""
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 77)
    res = base.run(svc, ADMIN, action, keys, params, preview=False,
                   confirm=base.run(svc, ADMIN, action, keys, params, preview=True)["confirm_value"])
    job_id = svc.db.enqueue_job(JobType.SERVER_ACTION, None, queued[0][2], 5, 1, "tester")
    svc.db.execute("UPDATE bulk_ops SET results=replace(results, '\"job_id\": 77', ?) WHERE id=?",
                   (f'"job_id": {job_id}', res["op_id"]))
    return res["op_id"], job_id, queued[0][2]


def test_retry_after_cancel_in_queue_or_failed_prepare_offers_all(ax, monkeypatch):
    op_id, job_id, _params = queue_only(ax.svc, "ax_must_change", [IV, PE], {"value": "yes"}, monkeypatch)
    ax.svc.db.finish_job(job_id, JobStatus.CANCELLED, error="Отменено пользователем")       # отменено в очереди
    assert base.retry_info(ax.svc, op_id)["keys"] == [IV, PE]
    op_id, job_id, params = queue_only(ax.svc, "ax_must_change", [IV, PE], {"value": "yes"}, monkeypatch)
    ax.svc.db.set_settings_many({"axigen.allow_changes": False})                              # запретили до запуска
    ax.svc.apply_runtime_settings()
    with pytest.raises(ValidationError):
        HANDLERS[JobType.SERVER_ACTION](JobContext(ax.svc, job_id, JobType.SERVER_ACTION, None, params))
    ax.svc.db.finish_job(job_id, JobStatus.FAILED, error="изменения запрещены")
    assert base.retry_count(ax.svc, op_id) == (2, "")


def test_cancel_in_the_middle_of_a_mailbox_is_uncertain(ax, monkeypatch):
    from mailarchiver.axigen import actions as axactions
    from mailarchiver.errors import JobCancelled
    op_id, job_id, params = queue_only(ax.svc, "ax_must_change", [IV, PE], {"value": "yes"}, monkeypatch)
    real = axactions.set_account_flag

    def applied_then_cancelled(cli, email, attr, value):
        real(cli, email, attr, value)                       # на сервере изменено…
        raise JobCancelled("Задание отменено.")             # …и тут же отмена
    monkeypatch.setattr(axactions, "set_account_flag", applied_then_cancelled)
    monkeypatch.setattr(ax.svc.db, "is_cancel_requested", lambda _id: True)
    ctx = JobContext(ax.svc, job_id, JobType.SERVER_ACTION, None, params)
    monkeypatch.setattr(ctx, "is_cancelled", lambda: False)          # отмену заметили внутри ящика
    out = HANDLERS[JobType.SERVER_ACTION](ctx)
    ax.svc.db.finish_job(job_id, out["final_status"], out)
    assert out["items"][0]["status"] == "failed" and "часть изменений могла" in out["items"][0]["detail"]
    info = base.retry_info(ax.svc, op_id)
    assert info["keys"] == [IV, PE] and info["uncertain"] == [IV]


def test_alias_of_dismissed_account_passed_to_colleague_is_not_his(ax):
    from mailarchiver import employees
    ax.world.domains["company.ru"].accounts["petrov"].aliases = ["p.petrov"]
    run_job(ax.svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
    ftp_mailbox(ax.svc, "petrov@company.ru")
    alias_box = ftp_mailbox(ax.svc, "p.petrov@company.ru", name="p.petrov")       # ящик, что входит по псевдониму
    emp = ax.svc.db.create_employee(full_name="Петров Пётр", email="p.petrov@company.ru")
    employees.dismiss_employee(ax.svc, emp, reason="тест")
    act(ax.svc, "ax_block", [PE], final_copy=False, keep_receiving=True)
    res, items = act(ax.svc, "ax_delete", [PE], forward_to="ivanov@company.ru", forward_aliases=True)
    assert items[PE]["status"] == "success", items[PE]
    assert "p.petrov@company.ru" in axstore.get_account(ax.svc.db, "ivanov@company.ru")["aliases"]
    rows = {r["key"]: r for r in tg.overview(ax.svc)["rows"]}
    assert not rows[IV]["dismissed_at"] and not rows[IV]["mailbox"]
    assert "dismissed_open" not in {p["key"] for p in monitor.problems(ax.svc)}
    assert f"mb:{alias_box}" in rows                       # ящик по псевдониму к коллеге не прилип


def test_deleted_account_keeps_mailbox_linked_by_alias(ax):
    ax.world.domains["company.ru"].accounts["petrov"].aliases = ["p.petrov"]
    run_job(ax.svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
    box = ftp_mailbox(ax.svc, "p.petrov@company.ru", name="p.petrov")
    assert ({r["key"]: r for r in tg.overview(ax.svc)["rows"]}[PE]["mailbox"] or {}).get("id") == box
    del ax.world.domains["company.ru"].accounts["petrov"]
    run_job(ax.svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})
    rows = {r["key"]: r for r in tg.overview(ax.svc)["rows"]}
    assert (rows[PE]["mailbox"] or {}).get("id") == box and f"mb:{box}" not in rows
    assert "gone" in {p["key"] for p in monitor.problems(ax.svc)}
