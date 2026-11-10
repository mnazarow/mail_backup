"""Задания, прерванные на полпути.

* Восстановление, прерванное перезапуском службы, продолжается со следующего письма, а не заливает уже
  залитое в ящик заново (без проверки дублей — всё, с ней — письма без Message-ID).
* Задание, итог которого не удалось записать в базу (нет места, база занята), не остаётся навсегда
  «выполняется»: очередь закрывает его, как только база снова отвечает.
"""
import json
import sqlite3
import time

import pytest

from imap_fake import ImapWorld, raw_message
from mailarchiver import models
from mailarchiver.errors import ImapConnectionError, JobCancelled
from mailarchiver.imap import client as client_mod
from mailarchiver.models import JobStatus, JobType
from mailarchiver.queue import jobs as jobs_mod
from mailarchiver.queue.jobs import JobContext, handle_backup, handle_restore

LOGIN = "ivanov@x.ru"


@pytest.fixture()
def world(monkeypatch):
    w = ImapWorld()
    monkeypatch.setattr(client_mod, "IMAPClient", w.client_class())
    return w


def _archived(services, world, n=12):
    """Ящик с n письмами, уже скопированный в архив; половина писем — без Message-ID."""
    world.mailbox(LOGIN, "pw")
    for i in range(n):
        raw = raw_message(f"Письмо {i}", mid=f"<m{i}@x.ru>")
        if i % 2:
            raw = b"\r\n".join(line for line in raw.split(b"\r\n") if not line.lower().startswith(b"message-id"))
        world.add(LOGIN, "INBOX", raw)
    aid = services.db.create_account(models.Account(name="Иванов", host="imap.x.ru", username=LOGIN, password="pw"))
    job = services.db.enqueue_job(JobType.BACKUP, aid, {}, 5, 1, "t")
    handle_backup(JobContext(services, job, JobType.BACKUP, aid, {}))
    assert services.db.count_messages(aid) == n
    return aid


def _restore(services, aid, job, params, stop_after=None):
    ctx = JobContext(services, job, JobType.RESTORE, aid, params)
    if stop_after is not None:
        appended = {"n": 0}
        orig = client_mod.IMAPClient.append

        def counting(self, folder, msg, flags=(), msg_time=None):
            if appended["n"] >= stop_after:
                raise AssertionError("после остановки службы заливка не должна продолжаться")
            appended["n"] += 1
            return orig(self, folder, msg, flags=flags, msg_time=msg_time)

        client_mod.IMAPClient.append = counting
        ctx.is_cancelled = lambda: appended["n"] >= stop_after      # «служба останавливается»
        try:
            with pytest.raises(JobCancelled):
                handle_restore(ctx)
        finally:
            client_mod.IMAPClient.append = orig
        return None
    return handle_restore(ctx)


@pytest.mark.parametrize("check_duplicates", [False, True])
def test_interrupted_restore_continues_without_duplicates(services, world, check_duplicates):
    aid = _archived(services, world)
    params = {"target_mode": "prefixed", "target_prefix": "Восстановлено", "check_duplicates": check_duplicates}
    job = services.db.enqueue_job(JobType.RESTORE, aid, params, 5, 1, "t")
    _restore(services, aid, job, params, stop_after=5)
    target = "Восстановлено/INBOX"
    assert len(world.uids(LOGIN, target)) == 5
    mark = json.loads(services.db.get_meta(f"restore_ckpt:{job}"))
    assert mark["done"] == 5 and mark["restored"] == 5

    res = _restore(services, aid, job, params)                     # служба запустилась — задание вернулось
    assert len(world.uids(LOGIN, target)) == 12, "уже залитые письма не должны заливаться повторно"
    assert res["restored"] == 12 and res["errors"] == 0 and res["final_status"] == "success"
    assert services.db.get_meta(f"restore_ckpt:{job}") is None
    rows = services.db.list_restores(aid)
    assert len(rows) == 1 and rows[0]["status"] == "success" and rows[0]["restored"] == 12
    events = " ".join(e["message"] for e in services.db.query("SELECT message FROM job_events WHERE job_id=?", (job,)))
    assert "Продолжение прерванного восстановления" in events


def test_restore_cancelled_by_user_starts_over(services, world):
    aid = _archived(services, world, n=6)
    params = {"target_mode": "prefixed", "target_prefix": "В", "check_duplicates": True}
    job = services.db.enqueue_job(JobType.RESTORE, aid, params, 5, 1, "t")
    services.db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job,))
    _restore(services, aid, job, params, stop_after=2)
    assert services.db.get_meta(f"restore_ckpt:{job}") is None     # «Отмена» — не «продолжить потом»


def test_dry_run_leaves_no_mark(services, world):
    aid = _archived(services, world, n=4)
    params = {"target_mode": "prefixed", "target_prefix": "В", "dry_run": True}
    job = services.db.enqueue_job(JobType.RESTORE, aid, params, 5, 1, "t")
    res = _restore(services, aid, job, params)
    assert res["restored"] == 4 and services.db.get_meta(f"restore_ckpt:{job}") is None
    assert "В/INBOX" not in world.boxes[LOGIN]


def _wait(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_job_whose_result_could_not_be_written_is_closed_later(services, monkeypatch):
    db = services.db
    aid = db.create_account(models.Account(name="a", host="h", username="u@x.ru", password="p"))

    def boom(ctx):
        raise ImapConnectionError("сервер недоступен")

    monkeypatch.setitem(jobs_mod.HANDLERS, JobType.BACKUP, boom)
    broken = {"on": True}
    orig_event, orig_finish = db.add_job_event, db.finish_job

    def event(job_id, level, message):
        if broken["on"]:
            raise sqlite3.OperationalError("database or disk is full")
        return orig_event(job_id, level, message)

    def finish(job_id, status, **kw):
        if broken["on"]:
            raise sqlite3.OperationalError("database or disk is full")
        return orig_finish(job_id, status, **kw)

    monkeypatch.setattr(db, "add_job_event", event)
    monkeypatch.setattr(db, "finish_job", finish)
    services.queue.start()
    try:
        jid = services.queue.enqueue(JobType.BACKUP, aid, {}, max_attempts=1)
        assert _wait(lambda: jid in services.queue._unsettled), "очередь должна запомнить незаписанный итог"
        assert db.get_job(jid)["status"] == JobStatus.RUNNING and jid not in services.queue.running_ids()
        broken["on"] = False                                       # место появилось
        assert _wait(lambda: db.get_job(jid)["status"] == JobStatus.FAILED)
        assert "не удалось записать" in db.get_job(jid)["error"]
        assert _wait(lambda: not services.queue._unsettled)
        # ящик снова свободен для расписания: новое задание по нему ставится и выполняется
        jid2 = services.queue.enqueue(JobType.BACKUP, aid, {}, max_attempts=1)
        assert _wait(lambda: db.get_job(jid2)["status"] == JobStatus.FAILED)
        assert db.get_job(jid2)["error"] == "сервер недоступен"
    finally:
        services.queue.stop(timeout=3)
