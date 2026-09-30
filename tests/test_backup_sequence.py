"""Копирование всех ящиков по очереди: порядок, по одному ящику, окно, продолжение, расписание."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from mailarchiver import models
from mailarchiver.errors import ImapConnectionError, JobCancelled
from mailarchiver.models import JobStatus, JobType
from mailarchiver.queue import jobs as jobs_mod
from mailarchiver.queue.jobs import JobContext, handle_backup_all

from test_imap_folders import FakeIMAP, _messages, _patch_connection

PW = "Sw0rdfish!"


def _acc(svc, name, **kw):
    data = dict(name=name, host="mx.example.ru", port=993, username=f"{name.lower()}@example.ru", password="p")
    data.update(kw)
    return svc.db.create_account(models.Account(**data))


def _ctx(svc, params=None, job_type=JobType.BACKUP_ALL):
    job_id = svc.db.enqueue_job(job_type, None, params or {}, 5, 1, "tester")
    return JobContext(svc, job_id, job_type, None, params or {})


def _events(svc, job_id):
    return [r["message"] for r in svc.db.list_job_events(job_id)]


def test_all_enabled_accounts_are_copied_one_by_one_oldest_first(services, monkeypatch):
    fake = FakeIMAP([((), "/", "INBOX"), ((), "/", "Sent")], messages=_messages("INBOX", "Sent"))
    _patch_connection(monkeypatch, fake)
    newer = _acc(services, "Newer")
    older = _acc(services, "Older")
    never = _acc(services, "Never")
    _acc(services, "Off", enabled=False)
    nopw = _acc(services, "NoPw", password="")
    services.db.execute("UPDATE accounts SET last_backup_at=? WHERE id=?", ("2026-09-20T00:00:00+00:00", newer))
    services.db.execute("UPDATE accounts SET last_backup_at=? WHERE id=?", ("2026-09-01T00:00:00+00:00", older))
    order, held_during = [], []
    real = jobs_mod._backup_account

    def spy(ctx, acc, **kw):
        order.append(acc.name)
        held_during.append(dict(services.queue.held_accounts()))
        return real(ctx, acc, **kw)

    monkeypatch.setattr(jobs_mod, "_backup_account", spy)
    ctx = _ctx(services)
    res = handle_backup_all(ctx)
    assert order == ["Never", "Older", "Newer"]                   # никогда не копировался — первым
    # в каждый момент занят ровно один ящик — тот, что копируется
    assert [list(h) for h in held_during] == [[never], [older], [newer]]
    assert services.queue.held_accounts() == {}
    assert res["final_status"] == JobStatus.SUCCESS
    assert res["ok"] == 3 and res["skipped"] == 1 and res["messages"] == 12
    assert res["skip_reasons"] == {"не задан пароль": 1}
    assert {it["id"] for it in res["items"] if it["status"] == "success"} == {never, older, newer}
    assert services.db.count_messages(never) == 4
    assert all(services.db.get_account(i).last_backup_at for i in (never, older, newer))
    runs = services.db.list_runs(never)
    assert runs and runs[0]["job_id"] == ctx.job_id               # история копий ящика ведётся как обычно
    assert services.db.get_meta(f"backup_all_state_{ctx.job_id}") is None
    assert nopw not in {it["id"] for it in res["items"] if it["status"] != "skipped"}


def test_explicit_selection_includes_disabled_as_skipped(services, monkeypatch):
    a = _acc(services, "A")
    off = _acc(services, "Off", enabled=False)
    seen = []
    monkeypatch.setattr(jobs_mod, "_backup_account",
                        lambda ctx, acc, **kw: seen.append(acc.id) or {"final_status": "success", "messages_new": 1,
                                                                        "bytes_new": 10, "summary": ""})
    res = handle_backup_all(_ctx(services, {"account_ids": [a, off]}))
    assert seen == [a] and res["skip_reasons"] == {"копирование выключено": 1}


def test_busy_account_is_deferred_to_the_end(services, monkeypatch):
    _acc(services, "A1")
    busy = _acc(services, "B2")
    _acc(services, "C3")
    queue = services.queue
    # по ящику B2 «выполняется» своё задание
    queue._running[4242] = {"account_id": busy, "type": JobType.EXPORT, "started": 0}
    order = []

    def fake(ctx, acc, **kw):
        order.append(acc.name)
        if acc.name == "C3":
            queue._running.pop(4242, None)                     # экспорт закончился
        return {"final_status": "success", "messages_new": 0, "bytes_new": 0, "summary": ""}

    monkeypatch.setattr(jobs_mod, "_backup_account", fake)
    res = handle_backup_all(_ctx(services, {"order": "name"}))
    assert order == ["A1", "C3", "B2"] and res["ok"] == 3


def test_account_still_busy_at_the_end_is_skipped(services, monkeypatch):
    _acc(services, "A1")
    busy = _acc(services, "B2")
    services.queue._running[4243] = {"account_id": busy, "type": JobType.BACKUP, "started": 0}
    monkeypatch.setattr(jobs_mod, "_backup_account",
                        lambda ctx, acc, **kw: {"final_status": "success", "messages_new": 0, "bytes_new": 0,
                                                "summary": ""})
    res = handle_backup_all(_ctx(services, {"order": "name"}))
    services.queue._running.pop(4243, None)
    assert res["ok"] == 1 and res["skip_reasons"] == {"по ящику выполнялось другое задание": 1}
    assert res["final_status"] == JobStatus.SUCCESS


def test_time_window_stops_taking_new_accounts(services, monkeypatch):
    _acc(services, "A1"), _acc(services, "B2")
    monkeypatch.setattr(jobs_mod, "_sequence_deadline",
                        lambda svc, stop_at, started: datetime.now(timezone.utc) - timedelta(minutes=1))
    called = []
    monkeypatch.setattr(jobs_mod, "_backup_account", lambda ctx, acc, **kw: called.append(acc) or {})
    ctx = _ctx(services, {"stop_at": "07:00"})
    res = handle_backup_all(ctx)
    assert not called and res["left"] == 2 and res["final_status"] == JobStatus.PARTIAL
    assert "в следующий раз они будут первыми" in res["summary"]


def test_deadline_is_next_occurrence_of_stop_time(services):
    services.db.set_setting("scheduler.timezone", "Europe/Moscow")
    services.apply_runtime_settings()
    started = datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)            # 23:00 по Москве
    deadline = jobs_mod._sequence_deadline(services, "07:00", started)
    assert deadline.isoformat().startswith("2026-09-26T07:00:00+03:00")
    same_day = jobs_mod._sequence_deadline(services, "23:30", started)
    assert same_day.isoformat().startswith("2026-09-25T23:30:00+03:00")
    assert jobs_mod._sequence_deadline(services, "", started) is None


def test_interrupted_pass_resumes_where_it_stopped(services, monkeypatch):
    a, b, c = _acc(services, "A1"), _acc(services, "B2"), _acc(services, "C3")
    ctx = _ctx(services, {"order": "name"})
    services.db.set_meta(f"backup_all_state_{ctx.job_id}", json.dumps({
        "done": [a], "stats": {"ok": 1, "messages": 5, "bytes": 50}, "items": [
            {"id": a, "name": "A1", "status": "success", "new": 5, "bytes": 50, "detail": ""}],
        "started": datetime.now(timezone.utc).isoformat(), "deadline": ""}))
    seen = []
    monkeypatch.setattr(jobs_mod, "_backup_account",
                        lambda ctx, acc, **kw: seen.append(acc.id) or {"final_status": "success", "messages_new": 1,
                                                                        "bytes_new": 1, "summary": ""})
    res = handle_backup_all(ctx)
    assert seen == [b, c] and res["ok"] == 3 and res["messages"] == 7
    assert any("Продолжение прерванного прохода" in e for e in _events(services, ctx.job_id))


def test_unreachable_server_stops_the_pass_and_keeps_progress(services, monkeypatch):
    ids = [_acc(services, f"A{i}") for i in range(8)]

    def down(ctx, acc, **kw):
        raise ImapConnectionError("Нет связи с сервером")

    monkeypatch.setattr(jobs_mod, "_backup_account", down)
    ctx = _ctx(services, {"order": "name"})
    with pytest.raises(ImapConnectionError) as info:
        handle_backup_all(ctx)
    assert "подряд не удалось подключиться" in info.value.message and info.value.retryable
    state = json.loads(services.db.get_meta(f"backup_all_state_{ctx.job_id}"))
    assert len(state["done"]) == jobs_mod.SEQUENCE_MAX_CONN_FAILS
    assert set(state["done"]) <= set(ids)


def test_user_cancel_forgets_progress_but_shutdown_keeps_it(services, monkeypatch):
    _acc(services, "A1"), _acc(services, "B2")

    def cancelled(ctx, acc, **kw):
        raise JobCancelled("Бэкап отменён пользователем.")

    monkeypatch.setattr(jobs_mod, "_backup_account", cancelled)
    ctx = _ctx(services)
    services.db.request_cancel(ctx.job_id)
    with pytest.raises(JobCancelled):
        handle_backup_all(ctx)
    assert services.db.get_meta(f"backup_all_state_{ctx.job_id}") is None
    ctx2 = _ctx(services)
    services.queue._shutting_down.set()
    try:
        with pytest.raises(JobCancelled):
            handle_backup_all(ctx2)
    finally:
        services.queue._shutting_down.clear()
    assert services.db.get_meta(f"backup_all_state_{ctx2.job_id}") is not None


def test_failed_accounts_make_the_pass_partial(services, monkeypatch):
    from mailarchiver.errors import ImapAuthError
    _acc(services, "A1"), _acc(services, "B2")

    def mixed(ctx, acc, **kw):
        if acc.name == "B2":
            raise ImapAuthError("Неверный пароль")
        return {"final_status": "success", "messages_new": 2, "bytes_new": 20, "summary": ""}

    monkeypatch.setattr(jobs_mod, "_backup_account", mixed)
    res = handle_backup_all(_ctx(services, {"order": "name"}))
    assert res["final_status"] == JobStatus.PARTIAL and res["failed"] == 1 and res["ok"] == 1
    assert "с ошибкой: 1 (B2)" in res["summary"]


def test_queue_hold_blocks_and_releases_accounts(services):
    queue = services.queue
    assert queue.acquire_account(7, 100)
    assert queue.acquire_account(7, 100)                 # повторно — тем же заданием можно
    assert not queue.acquire_account(7, 101)             # другим — нет
    queue.release_account(7, 101)                        # чужое освобождение ничего не меняет
    assert queue.held_accounts() == {7: 100}
    queue._running[100] = {"account_id": None, "type": JobType.BACKUP_ALL, "started": 0}

    class _Done:
        def exception(self):
            return None

    queue._on_done(100, _Done())                         # задание завершилось — ящики освобождены
    assert queue.held_accounts() == {}
    queue._running[5] = {"account_id": 8, "type": JobType.VERIFY, "started": 0}
    assert not queue.acquire_account(8, 200)
    queue._running.pop(5)


def test_held_account_jobs_are_not_claimed(services):
    from mailarchiver.queue.manager import LOCAL_JOB_TYPES
    a = _acc(services, "A")
    services.queue.enqueue(JobType.BACKUP, a, {})
    assert services.queue.acquire_account(a, 555)
    busy = sorted(set(services.queue.held_accounts()))
    assert services.db.claim_next_job_filtered("w", busy_accounts=busy, local_types=LOCAL_JOB_TYPES) is None
    services.queue.release_account(a, 555)
    assert services.db.claim_next_job_filtered("w", busy_accounts=[], local_types=LOCAL_JOB_TYPES) is not None


def test_final_copy_by_admin_disables_only_after_success(services, monkeypatch):
    a, b = _acc(services, "A"), _acc(services, "B")
    monkeypatch.setattr(jobs_mod, "_handle_backup",
                        lambda ctx: {"final_status": "success" if ctx.account_id == a else "failed", "summary": "x"})
    for acc_id in (a, b):
        job_id = services.db.enqueue_job(JobType.BACKUP, acc_id, {"disable_after": True}, 5, 1, "t")
        jobs_mod.handle_backup(JobContext(services, job_id, JobType.BACKUP, acc_id, {"disable_after": True}))
    assert not services.db.get_account(a).enabled
    assert services.db.get_account(b).enabled


# ---------------------------------------------------------------------------
#  Расписание «все ящики по очереди» и кнопка «копия всех ящиков»
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_global_schedule_via_api_and_scheduler(client):
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    a = _acc(svc, "A")
    r = client.post("/api/schedules", json={"job_type": "backup_all", "kind": "cron", "cron_expr": "0 1 * * *",
                                            "account_id": a, "options": {"pause_seconds": 5, "stop_at": "7:00"}})
    assert r.status_code == 200
    row = svc.db.get_schedule(r.json()["id"])
    assert row["account_id"] is None                        # ящик у общего расписания не хранится
    assert json.loads(row["options"]) == {"pause_seconds": 5, "stop_at": "07:00", "order": "oldest"}
    listed = client.get("/api/schedules").json()
    assert listed[0]["job_type"] == "backup_all" and listed[0]["next_run"]
    bad = client.post("/api/schedules", json={"job_type": "backup_all", "cron_expr": "0 1 * * *",
                                              "options": {"stop_at": "25:00"}})
    assert bad.status_code == 400
    no_acc = client.post("/api/schedules", json={"job_type": "backup", "cron_expr": "0 1 * * *"})
    assert no_acc.status_code == 400 and "Выберите ящик" in no_acc.json()["message"]
    svc.scheduler._fire(row["id"])
    queued = [j for j in svc.db.active_jobs() if j["type"] == JobType.BACKUP_ALL]
    assert len(queued) == 1 and json.loads(queued[0]["params"])["pause_seconds"] == 5
    assert queued[0]["created_by"] == "scheduler"
    svc.scheduler._fire(row["id"])                           # прежний проход ещё в очереди — второй не ставим
    assert len([j for j in svc.db.active_jobs() if j["type"] == JobType.BACKUP_ALL]) == 1
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["account_name"] == "все включённые ящики, по очереди"
    # правка расписания: общий проход можно превратить в копирование одного ящика
    up = client.put(f"/api/schedules/{row['id']}", json={"job_type": "backup", "account_id": a, "kind": "cron",
                                                         "cron_expr": "0 2 * * *", "enabled": True})
    assert up.status_code == 200 and svc.db.get_schedule(row["id"])["account_id"] == a


def test_backup_all_button_sequential_mode(client):
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    _acc(svc, "A"), _acc(svc, "B")
    r = client.post("/api/accounts/backup-all", json={"sequential": True, "pause_seconds": 2}).json()
    assert r["sequential"] and r["total_enabled"] == 2
    again = client.post("/api/accounts/backup-all", json={"sequential": True}).json()
    assert again["already"] and again["job_id"] == r["job_id"]
    parallel = client.post("/api/accounts/backup-all").json()
    assert len(parallel["started"]) == 2


def test_schedules_table_becomes_nullable_on_upgrade(tmp_path):
    """База 1.4.0: schedules.account_id NOT NULL — после обновления общие расписания разрешены."""
    from mailarchiver.database import Database
    from mailarchiver.security import SecretBox
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, host TEXT NOT NULL,
            port INTEGER NOT NULL DEFAULT 993, username TEXT NOT NULL, password_enc TEXT,
            auth_type TEXT NOT NULL DEFAULT 'password', security TEXT NOT NULL DEFAULT 'ssl',
            enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'cron', job_type TEXT NOT NULL DEFAULT 'backup', cron_expr TEXT DEFAULT '',
            interval_seconds INTEGER DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, options TEXT DEFAULT '{}',
            last_run TEXT, next_run TEXT, created_at TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE);
        INSERT INTO accounts(name, host, username) VALUES('A', 'h', 'u');
        INSERT INTO schedules(account_id, cron_expr, last_run) VALUES(1, '0 3 * * *', '2026-09-01');
    """)
    conn.commit()
    conn.close()
    db = Database(path, SecretBox(b"k" * 32))
    db.init_schema()
    info = {r[1]: r[3] for r in db.query("PRAGMA table_info(schedules)")}
    assert info["account_id"] == 0
    rows = db.list_schedules()
    assert len(rows) == 1 and rows[0]["cron_expr"] == "0 3 * * *" and rows[0]["last_run"] == "2026-09-01"
    sid = db.create_schedule(None, "cron", JobType.BACKUP_ALL, "0 1 * * *")
    assert db.get_schedule(sid)["account_id"] is None
    db.execute("DELETE FROM accounts WHERE id=1")            # каскад по-прежнему работает
    assert [r["id"] for r in db.list_schedules()] == [sid]
    db.init_schema()                                          # повторный запуск ничего не ломает
    assert len(db.list_schedules()) == 1


def test_crontab_weekdays_follow_cron_numbering():
    """В crontab 1 — понедельник, 0 и 7 — воскресенье (APScheduler сам считает 0 понедельником)."""
    from zoneinfo import ZoneInfo
    from mailarchiver.cronutil import apscheduler_day_of_week, crontab_trigger
    tz = ZoneInfo("Europe/Moscow")
    start = datetime(2026, 9, 27, 12, 0, tzinfo=tz)               # воскресенье

    def days(expr, n=6):
        trig, t, out = crontab_trigger(expr, tz), start, []
        for _ in range(n):
            t = trig.get_next_fire_time(None, t)
            out.append(t.strftime("%a"))
            t += timedelta(minutes=1)
        return out

    assert days("0 2 * * 1-5") == ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon"]
    assert days("0 8 * * 1", 2) == ["Mon", "Mon"]
    assert days("0 2 * * 0,6", 4) == ["Sat", "Sun", "Sat", "Sun"]
    assert days("0 2 * * 7", 2) == ["Sun", "Sun"]
    assert apscheduler_day_of_week("1-7") == "*" and apscheduler_day_of_week("mon-fri") == "mon,tue,wed,thu,fri"
    assert apscheduler_day_of_week("*/2") == "tue,thu,sat,sun"
    with pytest.raises(ValueError):
        crontab_trigger("0 2 * * 8")


def test_describe_cron_is_human_readable():
    from mailarchiver.cronutil import describe_cron
    assert describe_cron("0 2 * * *") == "ежедневно в 02:00"
    assert describe_cron("15 23 * * 1-5") == "по будням в 23:15"
    assert describe_cron("10 0 * * 2-6") == "со вторника по субботу в 00:10"   # разнос «по будням» за полночь
    assert describe_cron("0 8 * * 1") == "по понедельникам в 08:00"
    assert describe_cron("5 3 * * 7") == describe_cron("5 3 * * 0") == "по воскресеньям в 03:05"
    assert describe_cron("0 */6 * * *") == "cron «0 */6 * * *»"
    assert describe_cron("75 2 * * *") == "cron «75 2 * * *»"


def test_scheduler_builds_weekday_triggers_correctly(services):
    a = _acc(services, "A")
    sid = services.db.create_schedule(a, "cron", JobType.BACKUP, "0 2 * * 1-5")
    trig = services.scheduler._build_trigger(services.db.get_schedule(sid))
    from zoneinfo import ZoneInfo
    nxt = trig.get_next_fire_time(None, datetime(2026, 9, 26, 12, 0, tzinfo=ZoneInfo("UTC")))   # суббота
    assert nxt.weekday() == 0                                     # понедельник


def test_selection_pass_does_not_suppress_the_global_pass(client):
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    a = _acc(svc, "A")
    svc.queue.enqueue(JobType.BACKUP_ALL, None, {"account_ids": [a]})         # проход по выбранным
    sid = svc.db.create_schedule(None, "cron", JobType.BACKUP_ALL, "0 1 * * *")
    svc.scheduler._fire(sid)
    passes = [j for j in svc.db.active_jobs() if j["type"] == JobType.BACKUP_ALL]
    assert len(passes) == 2                                       # общий встал в очередь следом
    r = client.post("/api/accounts/backup-all", json={"sequential": True}).json()
    assert r["already"] and json.loads(svc.db.get_job(r["job_id"])["params"]).get("account_ids") is None


def test_order_is_by_last_attempt_so_failing_mailboxes_go_last(services, monkeypatch):
    ok_old, failing = _acc(services, "OkOld"), _acc(services, "Failing")
    _acc(services, "Never")
    services.db.execute("UPDATE accounts SET last_backup_at=?, login_checked_at=? WHERE id=?",
                        ("2026-09-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", ok_old))
    # «Failing» давно не копировался удачно, но попытка была вчера
    services.db.execute("UPDATE accounts SET last_backup_at=?, login_checked_at=? WHERE id=?",
                        ("2026-08-01T00:00:00+00:00", "2026-09-24T00:00:00+00:00", failing))
    order = []
    monkeypatch.setattr(jobs_mod, "_backup_account",
                        lambda ctx, acc, **kw: order.append(acc.name) or {"final_status": "success",
                                                                          "messages_new": 0, "bytes_new": 0,
                                                                          "summary": ""})
    handle_backup_all(_ctx(services))
    assert order == ["Never", "OkOld", "Failing"]


def test_dead_server_is_skipped_without_stopping_the_pass(services, monkeypatch):
    for i in range(7):
        _acc(services, f"Dead{i}", host="old.example.ru")
    good = [_acc(services, f"Good{i}", host="mx.example.ru") for i in range(3)]
    seen = []

    def fake(ctx, acc, **kw):
        seen.append(acc.name)
        if acc.host == "old.example.ru":
            raise ImapConnectionError("Нет связи")
        return {"final_status": "success", "messages_new": 1, "bytes_new": 1, "summary": ""}

    monkeypatch.setattr(jobs_mod, "_backup_account", fake)
    res = handle_backup_all(_ctx(services, {"order": "name"}))
    dead_tried = [n for n in seen if n.startswith("Dead")]
    assert len(dead_tried) == jobs_mod.SEQUENCE_MAX_CONN_FAILS       # остальные ящики сервера пропущены
    assert res["ok"] == len(good) and res["final_status"] == JobStatus.PARTIAL
    assert res["hosts_down"] == {"old.example.ru": 2} and "не отвечал" in res["summary"]


def test_deadline_counts_from_when_the_pass_was_queued(services, monkeypatch):
    _acc(services, "A")
    ctx = _ctx(services, {"stop_at": "07:00"})
    queued = datetime.now(timezone.utc) - timedelta(hours=30)
    services.db.execute("UPDATE jobs SET created_at=? WHERE id=?", (queued.isoformat(), ctx.job_id))
    called = []
    monkeypatch.setattr(jobs_mod, "_backup_account", lambda c, acc, **kw: called.append(acc) or {})
    res = handle_backup_all(ctx)
    assert not called and res["left"] == 1                     # время прохода давно вышло — ничего не начато


def test_pause_happens_only_after_real_copies(services, monkeypatch):
    for i in range(3):
        _acc(services, f"NoPw{i}", password="")
    _acc(services, "Real1"), _acc(services, "Real2")
    monkeypatch.setattr(jobs_mod, "_backup_account",
                        lambda ctx, acc, **kw: {"final_status": "success", "messages_new": 0, "bytes_new": 0,
                                                "summary": ""})
    slept = []
    monkeypatch.setattr(jobs_mod.time, "sleep", lambda s: slept.append(s))
    clock = [1000.0]

    def fake_time():
        clock[0] += 0.5
        return clock[0]

    monkeypatch.setattr(jobs_mod.time, "time", fake_time)
    handle_backup_all(_ctx(services, {"pause_seconds": 2, "order": "name"}))
    # одна пауза: между двумя настоящими копиями (после ящиков без пароля — нет)
    assert 1 <= len(slept) <= 4 and sum(slept) <= 2.5


def test_accountless_local_jobs_are_claimed_while_accounts_are_held(services):
    from mailarchiver.queue.manager import LOCAL_JOB_TYPES
    a = _acc(services, "A")
    services.queue.enqueue(JobType.ANALYZE, None, {})
    row = services.db.claim_next_job_filtered("w", busy_accounts=[a], local_types=LOCAL_JOB_TYPES)
    assert row is not None and row["type"] == JobType.ANALYZE


def test_manual_retry_restarts_the_time_window(services, monkeypatch):
    """«Повторить» прохода с пределом «не начинать после» считает предел от времени повтора:
    иначе повтор в 09:00 ночного прохода с пределом 07:00 сразу заканчивался ничем."""
    first = _acc(services, "A")
    second = _acc(services, "B")
    ctx = _ctx(services, {"stop_at": "07:00"})
    queued = datetime.now(timezone.utc) - timedelta(hours=30)
    services.db.execute("UPDATE jobs SET created_at=? WHERE id=?", (queued.isoformat(), ctx.job_id))
    # прошлая попытка успела скопировать ящик A и упала; её предел давно прошёл
    services.db.set_meta(f"backup_all_state_{ctx.job_id}", json.dumps({
        "done": [first], "stats": {"ok": 1}, "items": [{"id": first, "name": "A", "status": "success"}],
        "started": queued.isoformat(), "deadline": (queued + timedelta(hours=6)).isoformat()}))
    services.db.finish_job(ctx.job_id, JobStatus.FAILED, error="сбой")
    services.queue.retry(ctx.job_id)
    row = services.db.get_job(ctx.job_id)
    assert row["status"] == JobStatus.QUEUED and row["finished_at"] is None
    state = json.loads(services.db.get_meta(f"backup_all_state_{ctx.job_id}"))
    assert "deadline" not in state and state["done"] == [first]
    assert services.db.get_meta(f"backup_all_retry_at_{ctx.job_id}")
    called = []
    monkeypatch.setattr(jobs_mod, "_backup_account", lambda c, acc, **kw: called.append(acc.id) or {
        "final_status": JobStatus.SUCCESS, "messages_new": 0, "bytes_new": 0, "summary": "ok"})
    res = handle_backup_all(JobContext(services, ctx.job_id, JobType.BACKUP_ALL, None, {"stop_at": "07:00"}))
    assert called == [second]                                   # A уже скопирован этим проходом
    assert res["left"] == 0
    assert services.db.get_meta(f"backup_all_retry_at_{ctx.job_id}") is None


def test_manual_retry_window_survives_state_cleanup(services, monkeypatch):
    """Состояние прохода стёр другой проход (чистка чужих следов) — предел всё равно от времени повтора."""
    _acc(services, "A")
    ctx = _ctx(services, {"stop_at": "07:00"})
    queued = datetime.now(timezone.utc) - timedelta(hours=30)
    services.db.execute("UPDATE jobs SET created_at=? WHERE id=?", (queued.isoformat(), ctx.job_id))
    services.db.finish_job(ctx.job_id, JobStatus.FAILED, error="сбой")
    services.queue.retry(ctx.job_id)
    services.db.execute("DELETE FROM meta WHERE key=?", (f"backup_all_state_{ctx.job_id}",))
    called = []
    monkeypatch.setattr(jobs_mod, "_backup_account", lambda c, acc, **kw: called.append(acc.id) or {
        "final_status": JobStatus.SUCCESS, "messages_new": 0, "bytes_new": 0, "summary": "ok"})
    res = handle_backup_all(JobContext(services, ctx.job_id, JobType.BACKUP_ALL, None, {"stop_at": "07:00"}))
    assert len(called) == 1 and res["left"] == 0
