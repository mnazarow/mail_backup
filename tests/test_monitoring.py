"""Мониторинг: /metrics (Prometheus/Zabbix), состояние ящиков, еженедельная сводка, плашка копии."""
import json
import re
from datetime import datetime, timedelta, timezone

from mailarchiver import models
from mailarchiver.monitoring import (account_problems, metrics_access, render_prometheus, reset_cache,
                                     weekly_summary)

PW = "Sw0rdfish!1"
TOKEN = "0123456789abcdef0123456789abcdef"
_SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="(\\.|[^"\\])*",?)*\})? -?[0-9.eE+-]+$')


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed(svc):
    db = svc.db
    ok = db.create_account(models.Account(name='Бухгалтерия "главная"', host="h", username="buh@x", password="p"))
    db.execute("UPDATE accounts SET last_backup_at=?, first_backup_at=?, login_status='ok' WHERE id=?",
               (_iso(2), _iso(200), ok))
    db.add_message_index_batch([(ok, "INBOX", 1, 1, "<1@x>", 1000, _iso(3), "", "INBOX/cur/1", "s", "t", "a", 0)])
    bad = db.create_account(models.Account(name="Неверный\\пароль", host="h", username="bad@x", password="p"))
    db.set_login_status(bad, "auth_error", "AUTHENTICATIONFAILED")
    stale = db.create_account(models.Account(name="Давно", host="h", username="old@x", password="p"))
    db.execute("UPDATE accounts SET last_backup_at=? WHERE id=?", (_iso(24 * 5), stale))
    db.add_message_index_batch([(stale, "INBOX", 1, 1, "<2@x>", 500, _iso(200), "", "INBOX/cur/2", "s", "t", "a", 0)])
    never = db.create_account(models.Account(name="Новый", host="h", username="new@x", password="p"))
    failed = db.create_account(models.Account(name="Сбой", host="h", username="fail@x", password="p"))
    db.execute("UPDATE accounts SET last_backup_at=? WHERE id=?", (_iso(1), failed))
    db.add_message_index_batch([(failed, "INBOX", 1, 1, "<3@x>", 700, _iso(5), "", "INBOX/cur/3", "s", "t", "a", 0)])
    run = db.start_run(failed, "backup", None)
    db.finish_run(run, "failed", detail="Нет связи")
    off = db.create_account(models.Account(name="Выключен", host="h", username="off@x", password="p", enabled=False))
    return {"ok": ok, "bad": bad, "stale": stale, "never": never, "failed": failed, "off": off}


def test_account_problems_follow_dashboard_rules(services):
    ids = _seed(services)
    reset_cache()
    p = account_problems(services)
    names = {k: {i["id"] for i in p[k]} for k in ("badpw", "nobackup", "stale", "failed")}
    assert names["badpw"] == {ids["bad"]}
    assert ids["never"] in names["nobackup"] and ids["bad"] in names["nobackup"]
    assert names["stale"] == {ids["stale"]}
    assert names["failed"] == {ids["failed"]}
    assert p["total"] == 6 and p["enabled"] == 5
    assert ids["off"] not in set().union(*names.values())


def test_prometheus_output_is_valid(services):
    _seed(services)
    reset_cache()
    text = render_prometheus(services)
    samples = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert samples and all(_SAMPLE.match(line) for line in samples), [l for l in samples if not _SAMPLE.match(l)]
    assert 'mailarchiver_accounts{state="bad_password"} 1' in text
    assert 'mailarchiver_accounts{state="stale"} 1' in text
    assert 'account="Бухгалтерия \\"главная\\""' in text          # кавычки экранированы
    assert 'account="Неверный\\\\пароль"' in text                    # обратная косая тоже
    assert "mailarchiver_account_last_backup_timestamp_seconds" in text
    brief = render_prometheus(services, per_account=False)
    assert "mailarchiver_account_" not in brief


def test_metrics_access_rules(services):
    svc = services
    assert metrics_access(svc, "127.0.0.1", "") == (False, 404)          # выключено
    svc.set_rt("monitoring", "metrics_enabled", True)
    assert metrics_access(svc, "127.0.0.1", "")[0] is True               # свой сервер — можно
    assert metrics_access(svc, "10.1.2.3", "") == (False, 403)
    svc.set_rt("monitoring", "metrics_allowed_ips", ["10.1.0.0/16"])
    assert metrics_access(svc, "10.1.2.3", "")[0] is True
    svc.set_rt("monitoring", "metrics_token", TOKEN)
    assert metrics_access(svc, "192.0.2.1", "Bearer " + TOKEN)[0] is True
    assert metrics_access(svc, "192.0.2.1", "Bearer wrong") == (False, 401)
    assert metrics_access(svc, "not-an-ip", "") == (False, 401)


def test_metrics_endpoint_and_settings(client):
    _login(client)
    assert client.get("/metrics").status_code == 404
    short = client.put("/api/settings", json={"values": {"monitoring.metrics_enabled": True,
                                                        "monitoring.metrics_token": "short"}})
    assert short.status_code == 400
    bad_ip = client.put("/api/settings", json={"values": {"monitoring.metrics_allowed_ips": ["10.0.0.300"]}})
    assert bad_ip.status_code == 400
    r = client.put("/api/settings", json={"values": {"monitoring.metrics_enabled": True,
                                                    "monitoring.metrics_token": TOKEN}})
    assert r.status_code == 200, r.text
    anon = client.get("/metrics")                 # cookie входа тут не помогает
    assert anon.status_code in (401, 403)
    ok = client.get("/metrics", headers={"Authorization": "Bearer " + TOKEN})
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/plain")
    assert "mailarchiver_info{version=" in ok.text
    settings = client.get("/api/settings").json()
    assert settings["values"]["monitoring"]["metrics_token"] == ""
    assert settings["secrets_set"]["monitoring.metrics_token"] is True


def test_weekly_summary_text(services):
    ids = _seed(services)
    subject, body = weekly_summary(services)
    assert "требуют внимания" in subject
    assert "Неверный пароль" in body and "bad@x" in body
    assert "Нет удачной копии дольше 72 ч" in body and "Давно" in body
    assert "Копия вне сервера не настроена" in body
    assert "Выключен" not in body
    for acc_id in ids.values():
        services.db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    subject, body = weekly_summary(services)
    assert "всё в порядке" in subject


def test_summary_send_endpoint(client, monkeypatch):
    _login(client)
    assert client.post("/api/monitoring/summary").status_code == 400      # уведомления выключены
    client.put("/api/settings", json={"values": {"notifications.enabled": True,
                                                "notifications.smtp_host": "smtp.example.ru",
                                                "notifications.mail_to": ["admin@example.ru"]}})
    svc = client.app.state.services
    sent = []
    monkeypatch.setattr(svc.notifier, "send", lambda subject, body: sent.append((subject, body)))
    r = client.post("/api/monitoring/summary")
    assert r.status_code == 200 and r.json()["ok"] and sent
    preview = client.get("/api/monitoring/summary").json()
    assert preview["subject"] and preview["last_sent"] and preview["next_run"]


def test_replica_alert_on_dashboard(client):
    _login(client)
    svc = client.app.state.services
    assert client.get("/api/state").json()["replica_alert"] == ""
    svc.set_rt("replica", "enabled", True)
    svc.db.set_meta("replica_last_run", json.dumps({"status": "failed", "message": "Бакет не найден"}))
    assert "Бакет не найден" in client.get("/api/state").json()["replica_alert"]
    svc.db.set_meta("replica_last_run", json.dumps({"status": "success", "message": "ok"}))
    svc.db.set_meta("replica_last_ok", _iso(72))
    assert "48 ч" in client.get("/api/state").json()["replica_alert"]
    svc.db.set_meta("replica_last_ok", _iso(1))
    assert client.get("/api/state").json()["replica_alert"] == ""
