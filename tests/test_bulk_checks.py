"""
Групповые проверки 1.6.0: папки многих ящиков на сервере, переиндексация поиска
по выбранным ящикам и письмо-итог прохода «все ящики по очереди».
"""
import json

from imapclient.exceptions import IMAPClientError

from mailarchiver import models, search as search_mod
from mailarchiver.models import JobStatus, JobType
from mailarchiver.queue import jobs as jobs_mod
from mailarchiver.queue.jobs import JobContext, backup_all_report, handle_folders_check, handle_search_reindex

from test_imap_folders import RAW, FakeIMAP, _patch_connection

PW = "Sw0rdfish!"


def _acc(svc, name, **kw):
    data = dict(name=name, host="mx.example.ru", port=993, username=f"{name.lower()}@example.ru", password="p")
    data.update(kw)
    return svc.db.create_account(models.Account(**data))


def _ctx(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    return JobContext(svc, job_id, job_type, None, params)


# ---------------------------------------------------------------------------
#  Проверка папок многих ящиков
# ---------------------------------------------------------------------------
def test_folders_check_reports_broken_folders_per_mailbox(services, monkeypatch):
    refused = IMAPClientError("select failed: NO [NONEXISTENT] Unknown folder")
    fake = FakeIMAP(
        folders=[((b"\\HasNoChildren",), b"/", "INBOX"), ((b"\\HasNoChildren",), b"/", "Архив"),
                 ((b"\\HasNoChildren",), b"/", "Пустая")],
        messages={"INBOX": {1: RAW}},
        select_errors={"Архив": [refused] * 20, "Пустая": [refused] * 20},
        status_messages={"Архив": 12, "Пустая": 0})
    _patch_connection(monkeypatch, fake)
    a = _acc(services, "Анна")
    nopw = _acc(services, "Безпароля", password="")
    ctx = _ctx(services, JobType.FOLDERS_CHECK, {"account_ids": [a, nopw]})
    res = handle_folders_check(ctx)
    assert res["final_status"] == JobStatus.PARTIAL
    assert res["with_problems"] == 1 and res["skipped"] == 1 and res["messages_lost"] == 12
    items = {it["name"]: it for it in res["items"]}
    assert items["Анна"]["status"] == "partial" and items["Анна"]["new"] == 1
    assert "«Архив» — писем 12" in items["Анна"]["detail"]
    assert items["Безпароля"]["status"] == "skipped"
    assert "писем в них по данным сервера: 12" in res["summary"]
    # письма не скачивались и индекс не менялся
    assert services.db.count_messages(a) == 0


def test_folders_check_stops_querying_a_dead_server(services, monkeypatch):
    from mailarchiver.imap import client as client_mod
    calls = []

    def dead(acc, *args, **kwargs):
        calls.append(acc.name)
        if acc.host == "mx.alive.ru":
            return {"ok": True, "folders": [{"name": "INBOX", "verdict": "ok", "messages": 3}],
                    "counts": {"ok": 1}, "messages_lost": 0}
        return {"ok": False, "error": "тайм-аут подключения", "error_type": "ImapTimeoutError"}

    monkeypatch.setattr(client_mod, "diagnose_folders", dead)
    ids = [_acc(services, f"Ящик{i:02d}") for i in range(7)] + [_acc(services, "Живой", host="mx.alive.ru")]
    res = handle_folders_check(_ctx(services, JobType.FOLDERS_CHECK, {"account_ids": ids}))
    assert len([c for c in calls if c != "Живой"]) == jobs_mod.SEQUENCE_MAX_CONN_FAILS
    assert "Живой" in calls and res["ok"] == 1
    assert res["failed"] == jobs_mod.SEQUENCE_MAX_CONN_FAILS and res["skipped"] == 2
    assert res["final_status"] == JobStatus.PARTIAL


# ---------------------------------------------------------------------------
#  Переиндексация поиска по выбранным ящикам
# ---------------------------------------------------------------------------
def _mail(svc, account_id, uid, word):
    raw = (f"From: Отправитель <s@example.ru>\r\nTo: r@example.ru\r\nSubject: Письмо {uid}\r\n"
           f"Message-ID: <{account_id}-{uid}@example.ru>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
           f"Текст со словом {word}\r\n").encode("utf-8")
    rel, digest, size = svc.store.store_message(account_id, "INBOX", "/", uid, raw)
    svc.db.add_message_index(account_id, "INBOX", 1, uid, f"<{account_id}-{uid}@example.ru>", size,
                             "2026-09-01T10:00:00+00:00", "", rel, digest, f"Письмо {uid}", "s@example.ru", 0)


def _body_hits(svc, word):
    return {int(r["rowid"]) for r in svc.db.query(f"SELECT rowid FROM {search_mod.FTS_TABLE} "
                                                  f"WHERE {search_mod.FTS_TABLE} MATCH ?", (f"body : {word}",))}


def test_search_reindex_refreshes_only_selected_mailboxes(services):
    a, b = _acc(services, "Альфа"), _acc(services, "Бета")
    _mail(services, a, 1, "контрабас")
    _mail(services, b, 1, "виолончель")
    search_mod.index_pending(services)
    assert _body_hits(services, "контрабас") and _body_hits(services, "виолончель")
    # записи обоих ящиков в индексе «устарели» (например, текст раньше не индексировался)
    services.db.execute(f"DELETE FROM {search_mod.FTS_TABLE}")
    _mail(services, a, 2, "контрабас")          # новое письмо — его проиндексирует общий проход
    res = handle_search_reindex(_ctx(services, JobType.SEARCH_REINDEX, {"account_ids": [a]}))
    assert res["final_status"] == JobStatus.SUCCESS and res["indexed"] == 1
    assert [(it["name"], it["new"], it["status"]) for it in res["items"]] == [("Альфа", 1, "success")]
    ids_a = {int(r["id"]) for r in services.db.query("SELECT id FROM messages WHERE account_id=?", (a,))}
    assert _body_hits(services, "контрабас") == {min(ids_a)}   # новое письмо не тронуто
    assert not _body_hits(services, "виолончель")              # другой ящик не тронут
    assert search_mod.pending_count(services.db) == 1


def test_search_reindex_counts_unreadable_files(services):
    import os
    a = _acc(services, "Альфа")
    _mail(services, a, 1, "литавры")
    search_mod.index_pending(services)
    path = services.db.query_one("SELECT stored_path FROM messages WHERE account_id=?", (a,))["stored_path"]
    os.remove(os.path.join(services.store.account_dir(a), path))
    res = handle_search_reindex(_ctx(services, JobType.SEARCH_REINDEX, {"account_ids": [a]}))
    assert res["final_status"] == JobStatus.PARTIAL and res["errors"] == 1
    assert "не перечитано" in res["items"][0]["detail"]
    # по теме письмо по-прежнему находится
    assert services.db.query(f"SELECT rowid FROM {search_mod.FTS_TABLE} WHERE {search_mod.FTS_TABLE} MATCH ?",
                             ("subject : письмо",))


# ---------------------------------------------------------------------------
#  Групповые действия: одно общее задание на выбранные ящики
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def _bulk(client, action, ids, params=None, preview=True):
    return client.post("/api/accounts/bulk", json={"action": action, "ids": ids, "params": params or {},
                                                   "preview": preview, "confirm": ""}).json()


def test_bulk_folders_check_and_search_reindex_create_one_job(client):
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    a, b = _acc(svc, "A"), _acc(svc, "B")
    nopw = _acc(svc, "NoPw", password="")
    _mail(svc, a, 1, "гобой")

    r = _bulk(client, "folders_check", [a, b, nopw], preview=False)
    jobs = [j for j in svc.db.query("SELECT * FROM jobs") if j["type"] == JobType.FOLDERS_CHECK]
    assert len(jobs) == 1 and json.loads(jobs[0]["params"]) == {"account_ids": sorted([a, b])}
    assert r["counts"]["ok"] == 2 and r["counts"]["skip"] == 1
    again = _bulk(client, "folders_check", [a], preview=True)       # проверка уже стоит в очереди
    assert again["results"][0]["status"] == "skip" and "уже идёт" in again["results"][0]["detail"]

    prev = _bulk(client, "search_reindex", [a, b], preview=True)
    by = {x["id"]: x for x in prev["results"]}
    assert by[a]["status"] == "ok" and "писем: 1" in by[a]["detail"]
    assert by[b]["status"] == "skip" and by[b]["detail"] == "в копии нет писем"
    _bulk(client, "search_reindex", [a, b], preview=False)
    jobs = [j for j in svc.db.query("SELECT * FROM jobs") if j["type"] == JobType.SEARCH_REINDEX]
    assert len(jobs) == 1 and json.loads(jobs[0]["params"]) == {"account_ids": [a]}
    listed = client.get("/api/jobs?limit=20").json()
    scope = {j["type"]: j.get("account_name") for j in listed}
    assert scope.get(JobType.SEARCH_REINDEX) == "ящиков: 1"


# ---------------------------------------------------------------------------
#  Письмо-итог прохода «все ящики по очереди»
# ---------------------------------------------------------------------------
def _pass_result():
    items = [{"id": 1, "name": "Бухгалтерия", "status": "success", "new": 120, "bytes": 5_000_000, "detail": ""},
             {"id": 2, "name": "Склад", "status": "failed", "new": 0, "bytes": 0,
              "detail": "Неверный логин или пароль"},
             {"id": 3, "name": "Директор", "status": "partial", "new": 3, "bytes": 3000,
              "detail": "не прочитаны папки (1): Архив/2019"},
             {"id": 4, "name": "Стажёр", "status": "skipped", "detail": "не задан пароль"}]
    return {"final_status": "partial", "summary": "Скопировано ящиков по очереди: 2 из 6", "total": 6,
            "ok": 1, "partial": 1, "failed": 1, "skipped": 1, "items": items,
            "not_reached": ["Юристы", "Охрана"], "hosts_down": {}}


def test_backup_all_report_lists_problem_mailboxes_by_name():
    text = backup_all_report(_pass_result())
    assert "С ошибкой (1):\n  • Склад — Неверный логин или пароль" in text
    assert "Скопированы частично (1):\n  • Директор — не прочитаны папки (1): Архив/2019" in text
    assert "Пропущены (1):\n  • Стажёр — не задан пароль" in text
    assert "Не дошла очередь (2)" in text and "Юристы, Охрана" in text
    assert "Больше всего новых писем:\n  • Бухгалтерия — 120" in text
    assert backup_all_report({"items": []}) == ""


def test_backup_all_notification_has_names_and_counts_in_subject(services):
    sent = []

    class Capture:
        def notify_job_async(self, job_type, status, subject, body):
            sent.append((subject, body))

    services.notifier = Capture()
    job = {"id": 77, "type": JobType.BACKUP_ALL, "account_id": None}
    services.queue._after_final(job, JobStatus.PARTIAL, "Скопировано ящиков по очереди: 2 из 6", None,
                                _pass_result())
    subject, body = sent[0]
    assert subject.endswith("— 2 из 6, с ошибкой 1")
    assert "• Склад — Неверный логин или пароль" in body and body.rstrip().endswith("Задание №77.")
    # остальные задания — как раньше, без подробностей прохода
    services.queue._after_final({"id": 78, "type": JobType.BACKUP, "account_id": None}, JobStatus.FAILED,
                                "Сбой", None, {"items": []})
    assert "Больше всего" not in sent[1][1] and " из " not in sent[1][0]
