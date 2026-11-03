"""Живые данные при длинной очереди: когда расписания ставят в очередь сотни ящиков разом, снимок для вкладок
(раз в 1,5 с) и дашборд несут все идущие задания и только первые LIVE_QUEUED_LIMIT ждущих (+ сколько скрыто);
имена ящиков читаются один раз на снимок, а не на каждое задание. Пользователь-ящик своё задание видит, даже
если оно в очереди далеко за этой границей."""
import types

import pytest

pytest.importorskip("fastapi")

from mailarchiver import models  # noqa: E402
from mailarchiver.web import api, ws  # noqa: E402


def _accounts(db, n):
    return [db.create_account(models.Account(name=f"Ящик {k}", host="imap.x.ru", username=f"u{k}@x.ru",
                                             password="Secret-1")) for k in range(n)]


def _fill_queue(db, aids, running=2):
    jids = [db.enqueue_job("backup", aid, {}) for aid in aids]
    for jid in jids[:running]:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
    return jids


def _count_names(monkeypatch, db):
    calls = []
    orig = db.account_names
    monkeypatch.setattr(db, "account_names", lambda: calls.append(1) or orig())
    return calls


def test_cap_keeps_all_running_and_first_queued():
    jobs = ([{"id": i, "status": "queued"} for i in range(1, 8)] + [{"id": 100, "status": "running"}]
            + [{"id": 200}])                                       # без статуса — как прочие, остаётся
    out, hidden = api.cap_active_jobs(jobs, limit=3)
    assert [j["id"] for j in out] == [1, 2, 3, 100, 200] and hidden == 4
    assert api.cap_active_jobs([], limit=3) == ([], 0)


def test_snapshot_is_capped_per_viewer_and_names_read_once(services, monkeypatch):
    db = services.db
    limit = api.LIVE_QUEUED_LIMIT
    aids = _accounts(db, limit + 12)
    _fill_queue(db, aids)
    calls = _count_names(monkeypatch, db)
    snap = ws._collect_snapshot(services)
    assert len(calls) == 1                                         # а не по разу на каждое из 62 заданий
    assert len(snap["active_jobs"]) == limit + 12                  # общий снимок — полный: из него берёт ящик

    for role in ("admin", "operator"):
        out = ws._personal_snapshot(snap, {"id": 1, "role": role})
        st = [j["status"] for j in out["active_jobs"]]
        assert st.count("running") == 2 and st.count("queued") == limit
        assert out["queued_hidden"] == 10
        assert out["job_counts"] == {"running": 2, "queued": limit + 10}   # счётчики — по всей очереди

    last = aids[-1]                                                # его задание — последнее в очереди
    own = ws._personal_snapshot(snap, {"id": 2, "role": "mailbox", "account_id": last})
    assert [j["account_id"] for j in own["active_jobs"]] == [last]
    assert own["queued_hidden"] == 0 and own["job_counts"] == {"running": 0, "queued": 1}


def test_operator_redaction_applies_to_capped_list(services):
    db = services.db
    aids = _accounts(db, api.LIVE_QUEUED_LIMIT + 3)
    jids = _fill_queue(db, aids, running=1)
    db.execute("UPDATE jobs SET type='export' WHERE id=?", (jids[0],))
    db.update_job_progress(jids[0], 1, 3, "Папка «Личное»: письмо «Счёт»")
    out = ws._personal_snapshot(ws._collect_snapshot(services), {"id": 1, "role": "operator"})
    assert out["queued_hidden"] == 2                               # 1 идёт, 52 ждут, показано 50
    assert [j["progress_message"] for j in out["active_jobs"] if j["id"] == jids[0]] == [""]


def test_dashboard_state_capped_for_staff_full_for_own_mailbox(services, monkeypatch):
    db = services.db
    limit = api.LIVE_QUEUED_LIMIT
    aids = _accounts(db, limit + 5)
    _fill_queue(db, aids, running=1)
    services.scheduler.running = lambda: True
    req = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(services=services)))
    calls = _count_names(monkeypatch, db)
    s = api.state(req, {"id": 1, "username": "adm", "role": "admin"})
    assert len(calls) == 1
    assert len(s["active_jobs"]) == 1 + limit and s["queued_hidden"] == 4
    assert s["job_counts"] == {"running": 1, "queued": limit + 4}
    assert all(j["account_name"].startswith("Ящик ") for j in s["active_jobs"])

    m = api.state(req, {"id": 2, "username": "u", "role": "mailbox", "account_id": aids[-1]})
    assert [j["account_id"] for j in m["active_jobs"]] == [aids[-1]] and m["queued_hidden"] == 0
    assert m["job_counts"] == {"running": 0, "queued": 1}
