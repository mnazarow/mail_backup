"""Аудит с цепочкой подписей (1.20.0): правка, удаление, вставка, срок хранения, другой ключ, API."""
from datetime import datetime, timedelta, timezone


def _fill(db, n=5):
    for i in range(n):
        db.add_audit("admin", "test", f"запись {i}")


def test_chain_ok_and_edit_detected(services):
    db = services.db
    _fill(db)
    res = db.verify_audit()
    assert res["ok"] and res["verified"] >= 5 and res["head_hash"]
    target = db.scalar("SELECT id FROM audit WHERE detail='запись 2'")
    db.execute("UPDATE audit SET detail='подмена' WHERE id=?", (target,))
    res = db.verify_audit()
    assert not res["ok"] and res["broken"][0]["id"] == target and "изменена" in res["broken"][0]["reason"]


def test_delete_and_insert_detected(services):
    db = services.db
    _fill(db, 6)
    mid = db.scalar("SELECT id FROM audit WHERE detail='запись 3'")
    db.execute("DELETE FROM audit WHERE id=?", (mid,))
    res = db.verify_audit()
    assert not res["ok"] and any("удалена" in b["reason"] for b in res["broken"])
    db.execute("INSERT INTO audit(ts, user, action, detail) VALUES('2026-01-01', 'x', 'y', 'вставка')")
    assert any(b["reason"] == "запись без подписи" for b in db.verify_audit()["broken"])


def test_tail_deletion_detected(services):
    db = services.db
    _fill(db, 4)
    last = db.scalar("SELECT MAX(id) FROM audit")
    db.execute("DELETE FROM audit WHERE id=?", (last,))
    res = db.verify_audit()
    assert not res["ok"] and "последние" in res["broken"][-1]["reason"]


def test_purge_by_age_is_not_tampering(services):
    db = services.db
    _fill(db, 3)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    first_two = [r["id"] for r in db.query("SELECT id FROM audit ORDER BY id LIMIT 2")]
    for i in first_two:
        db.execute("UPDATE audit SET ts=? WHERE id=?", (old, i))
    # подпись первых записей теперь не сходится — но они удаляются по сроку
    db.purge_older("audit", "ts", (datetime.now(timezone.utc) - timedelta(days=365)).isoformat())
    res = db.verify_audit()
    assert res["ok"], res["broken"]
    assert res["purged_upto"] == max(first_two)
    # а удаление следующих записей вручную — нарушение
    nxt = db.scalar("SELECT MIN(id) FROM audit")
    db.execute("DELETE FROM audit WHERE id=?", (nxt,))
    assert not db.verify_audit()["ok"]


def test_legacy_and_other_key(services):
    db = services.db
    db.execute("INSERT INTO audit(ts, user, action, detail) VALUES('2025-01-01', 'old', 'login', '')")
    _fill(db, 2)
    res = db.verify_audit()
    assert res["ok"] and res["legacy"] >= 1
    db.execute("UPDATE audit SET kid='000000000000' WHERE detail='запись 0'")
    res = db.verify_audit()
    assert res["ok"] and res["other_key"] == 1


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_api_verify_and_csv(client):
    _login(client)
    r = client.get("/api/audit/verify")
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = client.get("/api/audit").json()
    assert rows and all(x["signed"] for x in rows)
    csv_text = client.get("/api/audit/export.csv").text
    assert csv_text.startswith("﻿id,ts_utc,user,action,detail,prev_hash,hash,kid")
    assert "audit_verify" in csv_text
