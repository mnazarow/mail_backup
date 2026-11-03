"""Счётчики писем по ящикам и папкам (folder_totals): их ведут триггеры, и они всегда равны COUNT/SUM по
messages — при записи пачкой и по одному, пропуске повторов, удалении писем, папки и ящика, переносе письма.
Плюс: первый запуск новой версии на старой базе заполняет счётчики, последние прогоны и расшифровка секретов —
быстрыми путями, сессия продлевается не на каждый запрос."""
from datetime import datetime, timedelta, timezone

from mailarchiver import models


def _acc(db, name):
    return db.create_account(models.Account(name=name, host="imap.x.ru", username=f"{name}@x.ru", password="Secret-1"))


def _row(aid, folder, uid, size):
    return (aid, folder, 1, uid, f"<{aid}.{folder}.{uid}@x>", size, "2026-01-01T00:00:00+00:00", "",
            f"p/{aid}/{uid}", f"{aid:08x}{uid:056x}", "тема", "a@x.ru", 0)


def _real(db, aid=None):
    where, params = ("", ()) if aid is None else (" WHERE account_id=?", (aid,))
    return (int(db.scalar("SELECT COUNT(*) FROM messages" + where, params)),
            int(db.scalar("SELECT COALESCE(SUM(size),0) FROM messages" + where, params)))


def _check(db, *aids):
    assert db.message_totals_mismatch() == []
    assert (db.count_messages(), db.sum_message_bytes()) == _real(db)
    for aid in aids:
        assert (db.count_messages(aid), db.sum_message_bytes(aid)) == _real(db, aid)


def test_counters_follow_every_change(services):
    db = services.db
    a, b = _acc(db, "a"), _acc(db, "b")
    db.add_message_index_batch([_row(a, "INBOX", u, 100 * u) for u in range(1, 11)])
    db.add_message_index_batch([_row(a, "INBOX", 1, 999), _row(a, "Sent", 1, 50)])     # повтор не считается
    db.add_message_index(b, "INBOX", 1, 7, "<m>", 70, "2026-01-01", "", "p", "h" * 64)
    _check(db, a, b)
    assert db.message_totals_by_account() == {a: {"messages": 11, "bytes": 5550}, b: {"messages": 1, "bytes": 70}}
    assert [(r["folder"], r["cnt"], r["bytes"]) for r in db.folders_summary(a)] == [("INBOX", 10, 5500),
                                                                                       ("Sent", 1, 50)]
    assert db.count_folder_messages(a, "Sent") == 1 and db.distinct_folder_count(a) == 2
    assert db.distinct_folder_count() == 3 and db.folder_counts_by_account() == {a: 2, b: 1}
    assert db.account_folders(a) == ["INBOX", "Sent"]

    pk = db.scalar("SELECT id FROM messages WHERE account_id=? AND uid=3", (a,))
    db.delete_message_index(pk) if hasattr(db, "delete_message_index") else \
        db.execute("DELETE FROM messages WHERE id=?", (pk,))
    db.execute("UPDATE messages SET stored_path='x' WHERE account_id=?", (a,))                 # размер не меняется
    db.execute("UPDATE messages SET folder='Archive', size=size+1 WHERE account_id=? AND uid=4", (a,))
    _check(db, a, b)
    db.execute("DELETE FROM messages WHERE account_id=? AND folder='Sent'", (a,))
    _check(db, a, b)
    assert db.account_folders(a) == ["Archive", "INBOX"]              # пустая папка в списках не числится
    db.delete_account(a)                                              # каскад удаляет письма
    _check(db, b)
    assert db.scalar("SELECT COUNT(*) FROM folder_totals WHERE account_id=?", (a,)) == 0
    assert db.message_totals_by_account() == {b: {"messages": 1, "bytes": 70}}


def test_first_start_on_old_database_fills_counters(services):
    db = services.db
    a = _acc(db, "old")
    db.add_message_index_batch([_row(a, f"F{u % 3}", u, u) for u in range(1, 301)])
    # база прежней версии: ни триггеров, ни счётчиков, ни отметки
    conn = db.connect()
    for name in db._TOTALS_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.execute("DELETE FROM folder_totals")
    conn.execute("DELETE FROM meta WHERE key=?", (db._TOTALS_MARK,))
    conn.commit()
    assert db.count_messages(a) == 0 and db.message_totals_mismatch()
    db.init_schema()
    _check(db, a)
    db.add_message_index_batch([_row(a, "F0", 1000, 5)])             # и дальше их ведут триггеры
    _check(db, a)
    # разошлись (ручная правка базы) — пересчёт
    db.execute("UPDATE folder_totals SET messages = messages + 5")
    assert db.message_totals_mismatch(a)
    db.recount_message_totals()
    _check(db, a)


def test_last_runs_and_active_job_counts(services):
    db = services.db
    a, b = _acc(db, "r1"), _acc(db, "r2")
    for k in range(3):
        rid = db.start_run(a, "backup", None)
        db.finish_run(rid, "success" if k < 2 else "failed")
    last = db.last_runs_by_account()
    assert set(last) == {a} and last[a]["status"] == "failed"
    db.enqueue_job("backup", a, {})
    jid = db.enqueue_job("backup", b, {})
    db.finish_job(jid, "failed")
    assert db.count_jobs_by_status(active_only=True) == {"queued": 1}
    assert db.count_jobs_by_status()["failed"] == 1


def test_secret_cache_returns_new_password_after_change(services):
    db = services.db
    a = _acc(db, "s")
    assert [x.password for x in db.list_accounts()] == ["Secret-1"]
    acc = db.get_account(a)
    acc.password = "Secret-2"
    db.update_account(acc, update_password=True, update_oauth_secret=False, update_oauth_token=False)
    assert [x.password for x in db.list_accounts()] == ["Secret-2"]


def test_session_is_extended_at_most_once_a_minute(services):
    import pytest
    pytest.importorskip("fastapi")
    from mailarchiver.web import auth as auth_mod
    db = services.db
    db.execute("INSERT INTO users(id, username, password_hash, role) VALUES(7,'u','x','admin')")
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=10)).isoformat()
    db.execute("INSERT INTO sessions(token, user_id, role, created_at, last_seen, expires_at) VALUES(?,?,?,?,?,?)",
               ("tok", 7, "admin", recent, recent, (now + timedelta(hours=1)).isoformat()))
    assert auth_mod.user_from_token(services, "tok")["username"] == "u"
    assert db.scalar("SELECT last_seen FROM sessions WHERE token='tok'") == recent      # недавно — без записи
    old = (now - timedelta(minutes=5)).isoformat()
    db.execute("UPDATE sessions SET last_seen=? WHERE token='tok'", (old,))
    assert auth_mod.user_from_token(services, "tok") is not None
    assert db.scalar("SELECT last_seen FROM sessions WHERE token='tok'") != old
