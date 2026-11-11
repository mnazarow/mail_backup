"""Очередь заданий и расписания в отдельном процессе (``server.worker_process: true``).

* веб-процесс запускает дочерний процесс, задания ставятся записью в базу и выполняются там;
* журнал дочернего процесса попадает в журнал службы (раздел «Логи») с видом задания;
* отмена, «Повторить», занятые ящики и «Планировщик активен» спрашиваются у дочернего процесса;
* упавший дочерний процесс запускается снова, его задания возвращаются в очередь;
* остановка службы останавливает и его.
"""
import os
import signal
import sys
import time

import pytest

from mailarchiver.models import JobStatus, JobType

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="процессы и сигналы POSIX")


def _wait(pred, timeout=30.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


@pytest.fixture()
def worker_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    data = tmp_path / "madata"
    data.mkdir()
    conf = tmp_path / "config.yaml"
    conf.write_text(f'paths:\n  data_dir: "{data}"\nserver:\n  worker_process: true\n', encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVER_CONFIG", str(conf))
    monkeypatch.delenv("MAILARCHIVER_DATA", raising=False)
    from mailarchiver.web.app import create_app
    app = create_app()
    with TestClient(app, headers={"X-Requested-With": "fetch"}) as c:
        yield c


def test_jobs_run_in_child_process(worker_client):
    from mailarchiver.logging_setup import memory_handler
    from mailarchiver.worker import QueueProxy, SchedulerProxy
    svc = worker_client.app.state.services
    assert isinstance(svc.queue, QueueProxy) and isinstance(svc.scheduler, SchedulerProxy)
    assert svc.worker.alive() and svc.worker.pid and svc.worker.pid != os.getpid()
    # журнал дочернего процесса — в журнале службы
    assert _wait(lambda: any("Очередь запущена" in r["message"] for r in memory_handler.tail(500)), 10)
    jid = svc.queue.enqueue(JobType.DB_SNAPSHOT, None, {}, priority=3, created_by="t")
    row = _wait(lambda: (lambda r: r if r["status"] not in ("queued", "running") else None)(svc.db.get_job(jid)), 60)
    assert row["status"] == JobStatus.SUCCESS, (row["status"], row["error"])
    # записи задания помечены его видом и тогда, когда пришли из другого процесса
    assert _wait(lambda: any(r.get("job_type") == JobType.DB_SNAPSHOT for r in memory_handler.tail(2000)), 5)
    # «Планировщик активен», расписания и занятые ящики — из дочернего процесса
    assert svc.scheduler.running() is True
    svc.scheduler.reload()
    assert svc.queue.held_accounts() == {}
    assert svc.queue.acquire_account(5, -5) is True
    assert svc.queue.held_accounts() == {5: -5}
    svc.queue.release_account(5, -5)
    assert svc.queue.held_accounts() == {}
    # живой снимок (WebSocket / запасной опрос) работает
    assert worker_client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    assert worker_client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    live = worker_client.get("/api/live").json()
    assert live["scheduler_running"] is True


def test_cancel_and_retry_go_through_child(worker_client):
    svc = worker_client.app.state.services
    db = svc.db
    jid = db.enqueue_job(JobType.DB_SNAPSHOT, None, {}, 5, 1, "t")
    db.execute("UPDATE jobs SET run_after=? WHERE id=?", ("2999-01-01T00:00:00+00:00", jid))   # не начнётся
    svc.queue.cancel(jid)
    assert db.get_job(jid)["status"] == JobStatus.CANCELLED
    svc.queue.retry(jid)                                                 # снова в очередь — и выполнится
    row = _wait(lambda: (lambda r: r if r["status"] not in ("queued", "running") else None)(db.get_job(jid)), 60)
    assert row["status"] == JobStatus.SUCCESS


def test_crashed_child_is_restarted(worker_client):
    svc = worker_client.app.state.services
    first = svc.worker.pid
    os.kill(first, signal.SIGKILL)
    assert _wait(lambda: svc.worker.alive() and svc.worker.pid != first, 30)
    assert svc.worker.restarts == 1
    jid = svc.queue.enqueue(JobType.DB_SNAPSHOT, None, {}, priority=3, created_by="t")
    row = _wait(lambda: (lambda r: r if r["status"] not in ("queued", "running") else None)(svc.db.get_job(jid)), 60)
    assert row["status"] == JobStatus.SUCCESS


def test_hung_child_does_not_stall_requests(worker_client):
    """Зависший процесс заданий: занятые ящики — по базе, и следующие вопросы не ждут срока ответа."""
    from mailarchiver import models
    svc = worker_client.app.state.services
    db = svc.db
    aid = db.create_account(models.Account(name="A", host="h", port=993, username="u", password="p"))
    jid = db.enqueue_job(JobType.BACKUP, aid, {}, 5, 1, "t")
    db.execute("UPDATE jobs SET status='running', run_after=? WHERE id=?", ("2999-01-01T00:00:00+00:00", jid))
    os.kill(svc.worker.pid, signal.SIGSTOP)
    try:
        started = time.monotonic()
        assert svc.queue.held_accounts() == {aid: jid}            # не ответил за 5 с — по базе
        assert 4 <= time.monotonic() - started < 8
        started = time.monotonic()
        assert svc.queue.acquire_account(aid, -aid) is False      # ящик занят заданием в базе
        assert svc.scheduler.next_run_for(1) is None
        assert time.monotonic() - started < 1                     # не ждали снова
    finally:
        os.kill(svc.worker.pid, signal.SIGCONT)
    svc.worker._unresponsive_until = 0                              # noqa: SLF001
    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))
    assert _wait(lambda: svc.queue.held_accounts() == {}, 10)


def test_stopping_the_service_stops_the_child(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    data = tmp_path / "d"
    data.mkdir()
    conf = tmp_path / "c.yaml"
    conf.write_text(f'paths:\n  data_dir: "{data}"\nserver:\n  worker_process: true\n', encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVER_CONFIG", str(conf))
    monkeypatch.delenv("MAILARCHIVER_DATA", raising=False)
    from mailarchiver.web.app import create_app
    app = create_app()
    with TestClient(app) as c:
        proc = c.app.state.services.worker._proc                        # noqa: SLF001
        assert proc.is_alive()
    assert not proc.is_alive() and proc.exitcode == 0


def test_inline_mode_is_the_default(services):
    from mailarchiver.queue.manager import QueueManager
    from mailarchiver.worker import worker_process_enabled
    assert worker_process_enabled(services.cfg) is False
    assert isinstance(services.queue, QueueManager) and services.worker is None


def test_environment_variable_overrides_config(cfg, monkeypatch):
    from mailarchiver.worker import WORKER_ENV, worker_process_enabled
    cfg.server["worker_process"] = False
    monkeypatch.setenv(WORKER_ENV, "1")
    assert worker_process_enabled(cfg) is True
    cfg.server["worker_process"] = True
    monkeypatch.setenv(WORKER_ENV, "0")
    assert worker_process_enabled(cfg) is False
    monkeypatch.setenv(WORKER_ENV, "")
    assert worker_process_enabled(cfg) is True


def test_settings_show_actual_mode(worker_client):
    assert worker_client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    assert worker_client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"}).status_code == 200
    data = worker_client.get("/api/settings").json()
    assert data["values"]["server"]["worker_process"] is True
    assert "server.worker_process" in data["readonly"]
