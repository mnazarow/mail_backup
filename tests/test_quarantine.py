"""Прежние копии (карантин после копии «с нуля»): сравнение с новой копией и возврат писем (1.6.0)."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from mailarchiver import models, quarantine as qmod
from mailarchiver.models import JobStatus, JobType
from mailarchiver.storage import crypto

PW = "Sw0rdfish!"


def _raw(subject, body="text", mid=None, date=None):
    head = f"From: Иван <i@x.ru>\r\nTo: p@x.ru\r\nSubject: {subject}\r\n"
    if mid:
        head += f"Message-ID: <{mid}>\r\n"
    return (head + "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body + "\r\n").encode()


def _put(svc, acc, folder, uid, raw, uidv=100, epoch=None):
    from mailarchiver.imap.backup import BackupEngine
    rel, sha, size = svc.store.store_message(acc, folder, "/", uid, raw, flags=["\\Seen"], internaldate=epoch)
    subject, frm, att = BackupEngine._extract_meta(raw)
    svc.db.add_message_index_batch([(acc, folder, uidv, uid, BackupEngine._extract_message_id(raw), size,
                                     BackupEngine._epoch_to_iso(epoch or time.time()), "\\Seen", rel, sha,
                                     subject, frm, att)])
    return rel


def _acc(svc, name="Q", **kw):
    data = dict(name=name, host="mx.example.ru", port=993, username=f"{name.lower()}@example.ru", password="p")
    data.update(kw)
    return svc.db.create_account(models.Account(**data))


def _rebuild(svc, acc):
    """Как копия «с нуля»: прежние файлы — в карантин, индекс ящика — очистить."""
    q, _files, _bytes = svc.store.quarantine_account_files(acc)
    svc.db.purge_account_index(acc)
    return q


def _ok_run(svc, acc):
    """Копирование ящика прошло без ошибок (после копии «с нуля»)."""
    run = svc.db.start_run(acc, JobType.BACKUP, None)
    svc.db.finish_run(run, JobStatus.SUCCESS)


def _setup(svc, ok=True):
    acc = _acc(svc)
    kept1, kept2 = _raw("Оставлено 1", mid="k1@x"), _raw("Оставлено 2", mid="k2@x")
    gone_inbox = _raw("Удалено на сервере", mid="gone1@x")
    gone_archive = _raw("Старый архив", mid="gone2@x")
    no_mid = _raw("Без Message-ID")
    changed_old = _raw("Пересобрано сервером", body="то же тело", mid="chg@x")
    for uid, raw in enumerate((kept1, kept2, gone_inbox, no_mid, changed_old), start=1):
        _put(svc, acc, "INBOX", uid, raw)
    _put(svc, acc, "Архив/2019", 1, gone_archive)
    q = _rebuild(svc, acc)
    # новая копия: то, что осталось на сервере (те же байты), и «пересобранное» письмо
    _put(svc, acc, "INBOX", 11, kept1, uidv=200)
    _put(svc, acc, "INBOX", 12, kept2, uidv=200)
    _put(svc, acc, "INBOX", 13, no_mid, uidv=200)
    # сервер пересобрал заголовки того же письма: тело то же, байты другие
    _put(svc, acc, "INBOX", 14, changed_old.replace(b"Subject:", b"X-Rebuilt: yes\r\nSubject:"), uidv=200)
    if ok:
        _ok_run(svc, acc)
    return acc, q


def test_compare_classifies_prior_copy(services):
    acc, q = _setup(services)
    assert qmod.stored_check(services.db, q) is None
    assert qmod.is_safe_to_delete(None)[0] is False
    res = qmod.compare(services, acc, q)
    assert res["files"] == 6
    assert res["identical"] == 3           # два письма с Message-ID и одно без — по полному SHA-256
    assert res["other_version"] == 1       # тот же Message-ID и тело, другие заголовки
    assert res["unique"] == 2 and res["unique_bytes"] > 0
    subjects = sorted(x["subject"] for x in res["samples"])
    assert subjects == ["Старый архив", "Удалено на сервере"]
    folders = sorted(x["folder"] for x in res["samples"])
    assert folders == ["INBOX", "Архив/2019"]
    stored = qmod.stored_check(services.db, q)
    assert stored["unique"] == 2 and "только в прежней 2" in qmod.check_label(stored)
    safe, why = qmod.is_safe_to_delete(stored)
    assert not safe and "2 писем" in why


def test_rescue_returns_only_missing_messages_and_is_idempotent(services):
    acc, q = _setup(services)
    before = services.db.count_messages(acc)
    r = qmod.rescue(services, services.db.get_account(acc), q)
    assert r["rescued"] == 2 and r["failed"] == 0 and r["skipped_old"] == 0
    rows = services.db.query("SELECT folder, uidvalidity, uid, subject, stored_path, message_id FROM messages "
                             "WHERE account_id=? AND uidvalidity=?", (acc, qmod.RESCUED_UIDVALIDITY))
    assert sorted((r["folder"], r["subject"]) for r in rows) == [("INBOX", "Удалено на сервере"),
                                                                ("Архив/2019", "Старый архив")]
    assert services.db.count_messages(acc) == before + 2
    for row in rows:
        # письмо читается из новой копии и лежит рядом с письмами своей папки
        raw = services.store.read_message(acc, row["stored_path"])
        assert row["message_id"].strip("<>") in raw.decode()
    inbox_dirs = {os.path.dirname(os.path.dirname(r["stored_path"])) for r in
                  services.db.query("SELECT stored_path FROM messages WHERE account_id=? AND folder='INBOX'", (acc,))}
    assert len(inbox_dirs) == 1
    after = qmod.stored_check(services.db, q)
    assert after["unique"] == 0 and after["rescued"] == 2
    assert qmod.is_safe_to_delete(after)[0] is True
    again = qmod.rescue(services, services.db.get_account(acc), q)
    assert again["rescued"] == 0 and services.db.count_messages(acc) == before + 2
    # копирование эти письма не трогает: их UIDVALIDITY у сервера не бывает
    assert services.db.existing_uids(acc, "INBOX", 200) == {11, 12, 13, 14}


def test_rescue_skips_messages_older_than_retention(services):
    acc = _acc(services, retention_days=30)
    old_epoch = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
    _put(services, acc, "INBOX", 1, _raw("Старое", mid="old@x"), epoch=old_epoch)
    _put(services, acc, "INBOX", 2, _raw("Свежее", mid="new@x"))
    q = _rebuild(services, acc)
    _ok_run(services, acc)
    r = qmod.rescue(services, services.db.get_account(acc), q)
    assert r["rescued"] == 1 and r["skipped_old"] == 1 and r["retention_days"] == 30
    after = qmod.stored_check(services.db, q)
    # старое письмо ночная очистка удалила бы и так: «только в прежней» его не считаем
    assert after["unique"] == 0 and after["outside_retention"] == 1
    assert "старше срока хранения 1" in qmod.check_label(after) and qmod.is_safe_to_delete(after)[0]


def test_compare_reads_encrypted_and_compressed_prior_copies(services):
    services.store.cipher = crypto.StorageCipher(os.urandom(32))
    services.store.encrypt = True
    services.store.compress = True
    acc, q = _setup(services)
    names = [fn for _r, _d, files in os.walk(q) for fn in files]
    assert names and all(fn.endswith(".gz.enc") for fn in names)
    res = qmod.compare(services, acc, q)
    assert (res["identical"], res["other_version"], res["unique"], res["unreadable"]) == (3, 1, 2, 0)
    services.store.cipher = None                                     # ключа нет — файлы не читаются
    res = qmod.compare(services, acc, q)
    assert res["unreadable"] == 6 and not qmod.is_safe_to_delete(res)[0]


def test_quarantine_file_access_is_limited_to_prior_copies(services, tmp_path):
    from mailarchiver.errors import StorageError
    acc = _acc(services)
    rel = _put(services, acc, "INBOX", 1, _raw("x", mid="x@x"))
    with pytest.raises(StorageError):
        services.store.read_quarantine_file(services.store.message_path(acc, rel))   # живая копия — нельзя
    outside = tmp_path / "evil_old_1.eml"
    outside.write_bytes(b"x")
    with pytest.raises(StorageError):
        services.store.read_quarantine_file(str(outside))


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_api_and_bulk_flow(client):
    from mailarchiver.queue.jobs import JobContext, handle_cleanup, handle_quarantine
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    acc, q = _setup(svc)
    lst = client.get(f"/api/accounts/{acc}/quarantines").json()
    assert lst["quarantines"][0]["check"] is None and lst["quarantines"][0]["safe"] is False
    # групповое удаление по умолчанию не трогает несравнённые прежние копии
    pv = client.post("/api/accounts/bulk", json={"action": "quarantine_delete", "ids": [acc], "preview": True}).json()
    assert pv["counts"]["skip"] == 1 and "не сравнивалась" in pv["results"][0]["detail"]
    # сравнение — фоновым заданием
    r = client.post(f"/api/accounts/{acc}/quarantines/check", json={"path": q}).json()
    job = svc.db.get_job(r["job_id"])
    assert job["type"] == JobType.QUARANTINE_CHECK and job["account_id"] == acc
    again = client.post(f"/api/accounts/{acc}/quarantines/check", json={}).json()
    assert again["already"] and again["job_id"] == r["job_id"]
    res = handle_quarantine(JobContext(svc, r["job_id"], JobType.QUARANTINE_CHECK, acc, json.loads(job["params"])))
    assert "только в прежней 2" in res["summary"] and res["items"][0]["new"] == 2
    svc.db.finish_job(r["job_id"], JobStatus.SUCCESS, res)
    pv = client.post("/api/accounts/bulk", json={"action": "quarantine_delete", "ids": [acc], "preview": True}).json()
    assert pv["counts"]["skip"] == 1 and "2 писем" in pv["results"][0]["detail"]
    # возврат писем групповым действием
    rr = client.post("/api/accounts/bulk", json={"action": "quarantine_rescue", "ids": [acc], "preview": False,
                                                 "params": {}}).json()
    assert rr["counts"]["ok"] == 1
    rjob = svc.db.get_job(rr["jobs"][0])
    res = handle_quarantine(JobContext(svc, rjob["id"], JobType.QUARANTINE_RESCUE, acc, json.loads(rjob["params"])))
    assert "возвращено в архив писем 2" in res["summary"]
    svc.db.finish_job(rjob["id"], JobStatus.SUCCESS, res)
    lst = client.get(f"/api/accounts/{acc}/quarantines").json()["quarantines"][0]
    assert lst["safe"] is True and lst["check"]["rescued"] == 2
    # теперь удаление разрешено; итог сравнения удаляется вместе с прежней копией
    dr = client.post("/api/accounts/bulk", json={"action": "quarantine_delete", "ids": [acc], "preview": False,
                                                 "confirm": "1"}).json()
    assert dr["counts"]["ok"] == 1
    cjob = [j for j in svc.db.query("SELECT * FROM jobs WHERE type=?", (JobType.CLEANUP,))][-1]
    out = handle_cleanup(JobContext(svc, cjob["id"], JobType.CLEANUP, None, json.loads(cjob["params"])))
    assert out["quarantines"] == 1 and not os.path.exists(q)
    assert qmod.stored_check(svc.db, q) is None


def test_cleanup_rechecks_safety_at_run_time(services):
    """Проверка перед удалением прошла, а потом новое сравнение нашло потери — задание прежнюю копию не удалит."""
    from mailarchiver.queue.jobs import JobContext, handle_cleanup
    acc, q = _setup(services)
    qmod.rescue(services, services.db.get_account(acc), q)
    params = {"items": [{"account_id": acc, "name": "Q", "what": "quarantine", "paths": [q], "only_safe": True}]}
    # между проверкой и выполнением письма «пропали» из новой копии
    services.db.execute("DELETE FROM messages WHERE account_id=? AND uidvalidity=?", (acc, qmod.RESCUED_UIDVALIDITY))
    qmod.compare(services, acc, q)
    job_id = services.db.enqueue_job(JobType.CLEANUP, None, params)
    out = handle_cleanup(JobContext(services, job_id, JobType.CLEANUP, None, params))
    assert out["quarantines"] == 0 and os.path.isdir(q)


def test_single_delete_is_guarded_and_runs_as_cleanup_job(client):
    from mailarchiver.queue.jobs import JobContext, handle_cleanup
    _login(client)
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    acc, q = _setup(svc)
    qmod.compare(svc, acc, q)                                       # в прежней копии 2 письма, которых нет в новой
    r = client.post(f"/api/accounts/{acc}/quarantines/delete", json={"path": q})
    assert r.status_code == 400 and "не удаляем" in r.json()["message"]
    # пока по ящику идёт задание с прежними копиями — удалять нельзя даже «с потерей»
    busy = svc.db.enqueue_job(JobType.QUARANTINE_RESCUE, acc, {"paths": [q]})
    r = client.post(f"/api/accounts/{acc}/quarantines/delete", json={"path": q, "force": True})
    assert r.status_code == 400 and "выполняется задание" in r.json()["message"]
    svc.db.finish_job(busy, JobStatus.CANCELLED)
    r = client.post(f"/api/accounts/{acc}/quarantines/delete", json={"path": q, "force": True}).json()
    assert r["forced"] is True and os.path.isdir(q)                  # удаление — фоновым заданием
    job = svc.db.get_job(r["job_id"])
    assert job["type"] == JobType.CLEANUP
    params = json.loads(job["params"])
    assert params["items"][0]["only_safe"] is False and params["items"][0]["paths"] == [q]
    lst = client.get(f"/api/accounts/{acc}/quarantines").json()
    assert lst["job"]["id"] == r["job_id"]                           # окно показывает задание удаления
    handle_cleanup(JobContext(svc, job["id"], JobType.CLEANUP, None, params))
    assert not os.path.exists(q) and qmod.stored_check(svc.db, q) is None


def test_rebuild_queues_comparison_only_after_success(services):
    from mailarchiver.queue.jobs import JobContext, _compare_after_rebuild
    acc, q = _setup(services)
    job_id = services.db.enqueue_job(JobType.BACKUP, acc, {"rebuild": "full"})
    ctx = JobContext(services, job_id, JobType.BACKUP, acc, {"rebuild": "full"})
    events = []

    class Res:
        status_label = JobStatus.SUCCESS

    info = {"quarantine": q}
    text = _compare_after_rebuild(ctx, services.db.get_account(acc), info, Res(),
                                  lambda level, msg: events.append((level, msg)))
    check_job = services.db.get_job(info["check_job"])
    assert check_job["type"] == JobType.QUARANTINE_CHECK and check_job["account_id"] == acc
    assert json.loads(check_job["params"]) == {"paths": [q], "after_rebuild": job_id}
    assert f"заданием №{check_job['id']}" in text
    # само задание копирования прежнюю копию не читало: перезапуск службы во время
    # долгого сравнения не вернёт в очередь копию «с нуля»
    assert qmod.stored_check(services.db, q) is None

    class Partial:
        status_label = JobStatus.PARTIAL

    info2 = {"quarantine": q}
    text = _compare_after_rebuild(ctx, services.db.get_account(acc), info2, Partial(), lambda *_a: None)
    assert "check_job" not in info2 and "Сравнить" in text          # неполную новую копию не сравниваем


def test_full_rebuild_happens_once_per_job(services, monkeypatch):
    """Задание «с нуля», прерванное перезапуском службы, не убирает в карантин уже скачанную новую копию."""
    from mailarchiver.queue import jobs as jobs_mod
    from mailarchiver.queue.jobs import JobContext, _rebuild_full_once
    monkeypatch.setattr(jobs_mod, "probe_account", lambda *a, **k: {"ok": True})
    acc = _acc(services)
    _put(services, acc, "INBOX", 1, _raw("Старое", mid="o@x"))
    job_id = services.db.enqueue_job(JobType.BACKUP, acc, {"rebuild": "full"})
    ctx = JobContext(services, job_id, JobType.BACKUP, acc, {"rebuild": "full"})
    events = []
    first = _rebuild_full_once(ctx, services.db.get_account(acc), lambda lvl, msg: events.append(msg))
    assert first["quarantine"] and first["files"] == 1
    _put(services, acc, "INBOX", 7, _raw("Новое", mid="n@x"), uidv=300)   # новая копия успела начаться
    again = _rebuild_full_once(ctx, services.db.get_account(acc), lambda lvl, msg: events.append(msg))
    assert again == dict(first, resumed=True) and len(services.store.quarantine_paths(acc)) == 1
    assert services.db.count_messages(acc) == 1 and any("уже начата" in m for m in events)
    # другое задание «с нуля» — снова карантин (отметка прежнего задания не мешает)
    other = services.db.enqueue_job(JobType.BACKUP, acc, {"rebuild": "full"})
    time.sleep(1.1)                                                      # имя карантина — с точностью до секунды
    third = _rebuild_full_once(JobContext(services, other, JobType.BACKUP, acc, {}), services.db.get_account(acc),
                               lambda *_a: None)
    assert third["quarantine"] != first["quarantine"] and len(services.store.quarantine_paths(acc)) == 2


def test_same_message_id_with_other_content_is_not_counted_as_present(services):
    """Сканеры повторяют Message-ID: другое письмо с тем же Message-ID — «только в прежней копии»."""
    acc = _acc(services)
    day = 86400
    now = time.time()
    scan1 = _raw("Скан 1", body="страница 1", mid="scan@device")
    scan2 = _raw("Скан 2", body="страница 2", mid="scan@device")
    _put(services, acc, "INBOX", 1, scan1, epoch=now - 3 * day)
    _put(services, acc, "INBOX", 2, scan2, epoch=now - 2 * day)
    q = _rebuild(services, acc)
    _put(services, acc, "INBOX", 5, scan1, uidv=200, epoch=now - 3 * day)
    res = qmod.compare(services, acc, q)
    assert (res["identical"], res["other_version"], res["unique"]) == (1, 0, 1)
    assert not qmod.is_safe_to_delete(res)[0]


def test_missing_file_in_new_copy_is_not_counted_as_present(services):
    acc, q = _setup(services)
    row = services.db.query_one("SELECT stored_path FROM messages WHERE account_id=? AND uid=11", (acc,))
    os.remove(services.store.message_path(acc, row["stored_path"]))    # файл новой копии пропал
    res = qmod.compare(services, acc, q)
    assert res["identical"] == 2 and res["unique"] == 3


def test_rescue_waits_for_a_clean_backup_and_skips_unreadable_folders(services):
    from mailarchiver.errors import ValidationError
    acc, q = _setup(services, ok=False)
    assert qmod.compare(services, acc, q)["new_copy_complete"] is False
    with pytest.raises(ValidationError):
        qmod.rescue(services, services.db.get_account(acc), q)
    assert services.db.count_messages(acc) == 4                         # ничего не возвращено
    _ok_run(services, acc)
    services.db.record_folder_problem(acc, "Архив/2019", "NO [CANNOT] folder is broken")
    check = qmod.compare(services, acc, q)
    assert check["new_copy_complete"] is True and check["unique"] == 2 and check["unique_unreadable"] == 1
    r = qmod.rescue(services, services.db.get_account(acc), q)
    assert r["rescued"] == 1 and r["skipped_unreadable"] == 1
    after = qmod.stored_check(services.db, q)
    safe, why = qmod.is_safe_to_delete(after)
    assert not safe and "не открываются на сервере" in why


def test_scanner_burst_with_same_headers_but_other_body_is_unique(services):
    """Сканер: один Message-ID, тема и отправитель, разница в минутах — но это разные письма."""
    acc = _acc(services)
    now = time.time()
    scan_a = _raw("Сообщение из KM_C250i", body="скан страницы A", mid="km@scanner")
    scan_b = _raw("Сообщение из KM_C250i", body="скан страницы B", mid="km@scanner")
    _put(services, acc, "INBOX", 1, scan_a, epoch=now - 600)
    _put(services, acc, "INBOX", 2, scan_b, epoch=now)
    q = _rebuild(services, acc)
    _put(services, acc, "INBOX", 5, scan_a, uidv=200, epoch=now - 600)
    res = qmod.compare(services, acc, q)
    assert (res["identical"], res["other_version"], res["unique"]) == (1, 0, 1)
    assert not qmod.is_safe_to_delete(res)[0]


def test_rescue_waits_for_complete_copy_after_the_latest_rebuild(services):
    """Прежняя копия №1 и незаконченная новая копия после «с нуля» №2: возвращать нельзя ни из одной."""
    from mailarchiver.errors import ValidationError
    from mailarchiver.util import utcnow_iso
    acc, q = _setup(services)                                        # полная копия после «с нуля» №1
    assert qmod.new_copy_complete(services, acc, q)
    time.sleep(0.01)
    services.db.set_meta(f"{qmod.REBUILD_META}{acc}", utcnow_iso())  # «с нуля» №2 — копия ещё идёт
    assert not qmod.new_copy_complete(services, acc, q)
    with pytest.raises(ValidationError):
        qmod.rescue(services, services.db.get_account(acc), q)
    # копирование, которое прочитало все папки (пусть и с ошибками отдельных писем), снимает запрет
    run = services.db.start_run(acc, JobType.BACKUP, None)
    services.db.finish_run(run, JobStatus.PARTIAL)
    assert not qmod.new_copy_complete(services, acc, q)
    services.db.note_complete_backup(acc)
    assert qmod.new_copy_complete(services, acc, q)


def test_cleanup_compares_again_before_deleting(services):
    """Итог «можно удалять» устарел: файл новой копии пропал после сравнения — прежнюю копию не удаляем."""
    from mailarchiver.queue.jobs import JobContext, handle_cleanup
    acc, q = _setup(services)
    qmod.rescue(services, services.db.get_account(acc), q)
    assert qmod.is_safe_to_delete(qmod.stored_check(services.db, q))[0]
    row = services.db.query_one("SELECT stored_path FROM messages WHERE account_id=? AND uid=11", (acc,))
    os.remove(services.store.message_path(acc, row["stored_path"]))
    params = {"items": [{"account_id": acc, "name": "Q", "what": "quarantine", "paths": [q], "only_safe": True}]}
    job_id = services.db.enqueue_job(JobType.CLEANUP, None, params)
    out = handle_cleanup(JobContext(services, job_id, JobType.CLEANUP, None, params))
    assert out["quarantines"] == 0 and os.path.isdir(q)
    assert "только в прежней" not in out["summary"] and "писем, которых нет в новой" in out["summary"]


def test_safe_verdict_expires_when_retention_changes(services):
    acc = _acc(services, retention_days=30)
    old_epoch = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
    _put(services, acc, "INBOX", 1, _raw("Старое", mid="old@x"), epoch=old_epoch)
    q = _rebuild(services, acc)
    check = qmod.compare(services, acc, q)
    assert check["outside_retention"] == 1 and qmod.is_safe_to_delete(check, 30)[0]
    safe, why = qmod.is_safe_to_delete(check, 0)                     # теперь «хранить всё»
    assert not safe and "срок хранения ящика изменился" in why


def test_after_rebuild_comparison_with_mail_only_in_old_copy_notifies(services):
    from mailarchiver.queue.jobs import JobContext, handle_quarantine
    acc, q = _setup(services)
    params = {"paths": [q], "after_rebuild": 1}
    job_id = services.db.enqueue_job(JobType.QUARANTINE_CHECK, acc, params)
    res = handle_quarantine(JobContext(services, job_id, JobType.QUARANTINE_CHECK, acc, params))
    assert res["final_status"] == JobStatus.PARTIAL and res.get("notify") is True
    sent = []

    class Capture:
        def notify_job_async(self, job_type, status, subject, body):
            sent.append(subject)

    services.notifier = Capture()
    services.queue._after_final({"id": job_id, "type": JobType.QUARANTINE_CHECK, "account_id": acc},
                                res["final_status"], res["summary"], None, res)
    assert len(sent) == 1
    # сравнение по кнопке — как раньше, без письма
    params = {"paths": [q]}
    job_id = services.db.enqueue_job(JobType.QUARANTINE_CHECK, acc, params)
    res = handle_quarantine(JobContext(services, job_id, JobType.QUARANTINE_CHECK, acc, params))
    assert res["final_status"] == JobStatus.SUCCESS and "notify" not in res
    services.queue._after_final({"id": job_id, "type": JobType.QUARANTINE_CHECK, "account_id": acc},
                                res["final_status"], res["summary"], None, res)
    assert len(sent) == 1


def test_excluded_broken_folder_mail_can_be_rescued(services):
    """Папку, которую не починить, исключили из копирования — её письма из прежней копии можно вернуть."""
    acc, q = _setup(services)
    services.db.record_folder_problem(acc, "Архив/2019", "NO [CANNOT] folder is broken")
    assert qmod.compare(services, acc, q)["unique_unreadable"] == 1
    services.db.execute("UPDATE accounts SET folder_exclude=? WHERE id=?", (json.dumps(["Архив"]), acc))
    check = qmod.compare(services, acc, q)
    assert check["unique_unreadable"] == 0
    r = qmod.rescue(services, services.db.get_account(acc), q)
    assert r["rescued"] == 2 and r["skipped_unreadable"] == 0



def test_broken_folder_is_not_mistaken_for_an_excluded_one(services):
    """«Trash.old» на сервере с разделителем «/» — не вложенная папка «Trash»: исключение её не касается."""
    acc, q = _setup(services)                          # в копии есть «Архив/2019» — разделитель «/»
    services.db.record_folder_problem(acc, "Trash.old", "NO [CANNOT]")
    services.db.execute("UPDATE accounts SET folder_exclude=? WHERE id=?", (json.dumps(["Trash"]), acc))
    rels = qmod._problem_folder_rels(services, acc)
    assert any("Trash" in r for r in rels)


def test_problem_folder_uses_recorded_delimiter(services):
    """Разделитель, записанный копированием, точно указывает каталог нечитаемой папки на диске."""
    import os
    acc, q = _setup(services)
    services.db.record_folder_problem(acc, "Trash.old", "NO [CANNOT]", delimiter="/")
    assert qmod._problem_folder_rels(services, acc) == {
        os.path.normpath(services.store.folder_relpath("Trash.old", "/"))}
    # сервер с разделителем «.»: та же строка — вложенная папка old внутри Trash
    services.db.record_folder_problem(acc, "Trash.old", "NO [CANNOT]", delimiter=".")
    assert qmod._problem_folder_rels(services, acc) == {
        os.path.normpath(services.store.folder_relpath("Trash.old", "."))}
    # следующая неудача без сведений о разделителе его не стирает
    services.db.record_folder_problem(acc, "Trash.old", "NO [CANNOT]")
    assert services.db.get_folder_problem(acc, "Trash.old")["delimiter"] == "."
    # запись прежних версий (без разделителя) — блокируются все варианты, как раньше
    services.db.execute("UPDATE folder_problems SET delimiter='' WHERE account_id=?", (acc,))
    assert len(qmod._problem_folder_rels(services, acc)) >= 2


def test_retry_of_finished_rebuild_starts_over(services):
    from mailarchiver.queue.jobs import _REBUILD_DONE
    acc = _acc(services)
    job_id = services.db.enqueue_job(JobType.BACKUP, acc, {"rebuild": "full"})
    services.db.set_meta(f"{_REBUILD_DONE}{job_id}:{acc}", json.dumps({"quarantine": "x"}))
    services.db.finish_job(job_id, JobStatus.FAILED, error="сбой")
    services.queue.retry(job_id)                                    # прерванное сбоем — продолжается
    assert services.db.get_meta(f"{_REBUILD_DONE}{job_id}:{acc}") is not None
    services.db.finish_job(job_id, JobStatus.SUCCESS, {})
    services.queue.retry(job_id)                                    # законченное — заново
    assert services.db.get_meta(f"{_REBUILD_DONE}{job_id}:{acc}") is None
