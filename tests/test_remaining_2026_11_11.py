"""Доделки по разбору 11.11.2026.

* строгий CSP страницы интерфейса: без встроенных скриптов и ссылок ``javascript:``;
* счётчики писем по папкам: неудачное первое заполнение повторяется ежечасным обслуживанием;
* события чужого задания своего ящика сотрудник видит только предупреждениями и ошибками;
* «выполняющееся» в базе задание, которого нет в очереди, возвращается в очередь (без перезапуска);
* выгрузки — постранично;
* доступность: подписи полей связаны с полями, контраст кнопок и бейджей.
"""
import os
import re

import pytest
from fastapi.testclient import TestClient

from mailarchiver import models
from mailarchiver.models import JobStatus, JobType
from mailarchiver.security import sign_value
from mailarchiver.web.auth import COOKIE_NAME

PW = "СложныйПароль1"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "mailarchiver", "web", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def admin(client):
    assert client.post("/api/setup", json={"username": "adm", "password": PW}).status_code == 200
    assert client.post("/api/login", json={"username": "adm", "password": PW}).status_code == 200
    return client


# -- строгий CSP ---------------------------------------------------------------------------------
def test_index_page_has_strict_csp(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    script = next(d for d in csp.split(";") if d.strip().startswith("script-src")).split()[1:]
    assert script == ["'self'"]                                   # ни 'unsafe-inline', ни 'unsafe-eval'
    for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'", "form-action 'self'"):
        assert directive in csp
    # на странице нет встроенных скриптов и обработчиков в атрибутах — иначе CSP их бы заблокировал
    html = r.text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "встроенный <script> без src"
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "обработчик в атрибуте"
    assert "/static/js/theme.js" in html
    theme = client.get("/static/js/theme.js")
    assert theme.status_code == 200 and "prefers-color-scheme" in theme.text


def test_app_js_has_no_javascript_urls():
    js = _read(STATIC, "js", "app.js")
    assert not re.search(r"""href\s*=\s*\\?["']javascript:""", js)
    # обработчики событий не собираются строкой (on…="…" в разметке из шаблонов)
    assert not re.search(r"""\son(click|change|input|submit|load|error)\s*=\s*["']""", js)


# -- счётчики писем по папкам --------------------------------------------------------------------
def test_message_totals_retry_after_failed_first_fill(services, monkeypatch):
    db = services.db
    aid = db.create_account(models.Account(name="A", host="h", port=993, username="u", password="p"))
    conn = db.connect()
    for i in range(3):
        conn.execute("INSERT INTO messages(account_id, folder, uid, uidvalidity, message_id, size, stored_path, "
                     "subject, from_addr, internaldate) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (aid, "INBOX", i + 1, 1, f"<m{i}@x>", 100, f"p{i}", "s", "f", "2026-01-01"))
    # первое заполнение «не удалось»: отметки нет, счётчики пусты
    conn.execute("DELETE FROM folder_totals")
    conn.execute("DELETE FROM meta WHERE key=?", (db._TOTALS_MARK,))                   # noqa: SLF001
    conn.commit()
    from mailarchiver.maintenance import run_maintenance
    run_maintenance(services)
    assert db.ensure_message_totals() is True
    row = db.query_one("SELECT messages, bytes FROM folder_totals WHERE account_id=? AND folder='INBOX'", (aid,))
    assert (row["messages"], row["bytes"]) == (3, 300)
    assert db.message_totals_mismatch(aid) == []


# -- события чужих заданий -------------------------------------------------------------------------
def test_mailbox_sees_only_problems_in_foreign_job_events(admin):
    svc = admin.app.state.services
    aid = admin.post("/api/accounts", json={"name": "Иванов", "host": "h", "port": 993,
                                            "username": "ivanov@x.ru", "password": "p"}).json()["id"]
    svc.queue.stop()
    foreign = svc.db.enqueue_job(JobType.BACKUP, aid, {}, created_by="scheduler")
    own = svc.db.enqueue_job(JobType.EXPORT, aid, {}, created_by="ivanov@x.ru")
    for jid in (foreign, own):
        svc.db.add_job_event(jid, "INFO", "Папка INBOX: 10 писем")
        svc.db.add_job_event(jid, "WARNING", "Папка Спам не читается")
    svc.db.create_session("mbtoken", 0, "2099-01-01T00:00:00+00:00", "", "", role="mailbox", account_id=aid)
    mb = TestClient(admin.app, headers={"X-Requested-With": "fetch"})
    mb.cookies.set(COOKIE_NAME, sign_value("mbtoken", svc.cfg.secret_key()))
    assert [e["level"] for e in mb.get(f"/api/jobs/{foreign}/events").json()] == ["WARNING"]
    assert sorted(e["level"] for e in mb.get(f"/api/jobs/{own}/events").json()) == ["INFO", "WARNING"]
    assert sorted(e["level"] for e in admin.get(f"/api/jobs/{foreign}/events").json()) == ["INFO", "WARNING"]


# -- сверка выполняющихся заданий ------------------------------------------------------------------
def test_queue_requeues_running_job_it_does_not_run(services):
    from mailarchiver.queue.manager import QueueManager
    db = services.db
    jid = db.enqueue_job(JobType.DB_SNAPSHOT, None, {}, 5, 1, "t")
    db.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.RUNNING, jid))
    q = QueueManager(services)
    q._reconcile_running()                                        # noqa: SLF001 — первая сверка: только заметили
    assert db.get_job(jid)["status"] == JobStatus.RUNNING
    q._orphan_checked -= q.RECONCILE_EVERY_S                       # noqa: SLF001
    q._reconcile_running()                                        # noqa: SLF001 — вторая подряд: вернуть
    row = db.get_job(jid)
    assert row["status"] == JobStatus.QUEUED
    events = [e["message"] for e in db.list_job_events(jid, limit=10)]
    assert db.ORPHAN_REQUEUED_TEXT in events


def test_reconcile_leaves_jobs_the_queue_runs(services):
    from mailarchiver.queue.manager import QueueManager
    db = services.db
    jid = db.enqueue_job(JobType.DB_SNAPSHOT, None, {}, 5, 1, "t")
    db.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.RUNNING, jid))
    other = db.enqueue_job(JobType.DB_SNAPSHOT, None, {}, 5, 1, "t")
    db.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.RUNNING, other))
    q = QueueManager(services)
    q._running[jid] = {"account_id": None, "type": JobType.DB_SNAPSHOT, "started": 0}   # noqa: SLF001
    for _ in range(3):
        q._orphan_checked -= q.RECONCILE_EVERY_S                   # noqa: SLF001
        q._reconcile_running()                                    # noqa: SLF001
    assert db.get_job(jid)["status"] == JobStatus.RUNNING         # своё — не трогает
    assert db.get_job(other)["status"] == JobStatus.QUEUED
    # только выбранные: reset_orphan_jobs(only_ids) не трогает остальные «выполняющиеся»
    db.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.RUNNING, other))
    assert db.reset_orphan_jobs(only_ids=[other]) == 1
    assert db.get_job(jid)["status"] == JobStatus.RUNNING


# -- выгрузки постранично ------------------------------------------------------------------------
def test_exports_are_paged(admin):
    svc = admin.app.state.services
    aid = svc.db.create_account(models.Account(name="A", host="h", port=993, username="u", password="p"))
    ids = [svc.db.create_export(aid, "eml", "zip", f"/nonexistent/{i}.zip", {}, None, "adm") for i in range(7)]
    first = admin.get("/api/exports", params={"limit": 3}).json()
    assert [e["id"] for e in first] == ids[::-1][:3]
    second = admin.get("/api/exports", params={"limit": 3, "before_id": first[-1]["id"]}).json()
    assert [e["id"] for e in second] == ids[::-1][3:6]
    rest = admin.get("/api/exports", params={"limit": 3, "before_id": second[-1]["id"]}).json()
    assert [e["id"] for e in rest] == ids[:1]
    # предел страницы — 1…1000
    assert len(admin.get("/api/exports", params={"limit": 0}).json()) == 1
    assert len(admin.get("/api/exports", params={"limit": 100000}).json()) == 7
    assert "before_id" in _read(STATIC, "js", "app.js")


# -- доступность ---------------------------------------------------------------------------------
def _hex(c):
    c = c.lstrip("#")
    return [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _lum(c):
    ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in _hex(c)]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _vars(css, selector):
    block = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S).group(1)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", block))


def test_contrast_of_buttons_and_status_text():
    css = _read(STATIC, "css", "app.css")
    light = _vars(css, ":root")
    assert _contrast("#ffffff", light["--primary-strong"]) >= 4.5        # белый текст на основной кнопке
    for var in ("--success-text", "--warn-text"):
        assert _contrast(light[var], "#ffffff") >= 4.5, var              # текст статусов на светлом фоне
    assert ".btn.primary" in css and "var(--primary-strong)" in css


def test_labels_are_linked_to_fields():
    js = _read(STATIC, "js", "app.js")
    assert "function linkFormLabels" in js and "MutationObserver" in js
    assert 'aria-live="polite"' in _read(ROOT, "mailarchiver", "web", "templates", "index.html")
