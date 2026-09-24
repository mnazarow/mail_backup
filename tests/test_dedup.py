"""Отчёт «Одинаковые вложения»: отпечатки вложений, экономия, инкрементальный подсчёт, задание и API."""
import base64
import io
import os
import quopri
from datetime import datetime, timedelta, timezone

import pytest

from mailarchiver import dedup, models
from mailarchiver.errors import JobCancelled
from mailarchiver.queue import jobs as jobs_mod

PW = "Sw0rdfish!1"
PDF = os.urandom(60_000)             # «прайс», разосланный всем
LOGO = os.urandom(5_000)             # логотип в подписи
OTHER = os.urandom(30_000)


def _b64(data: bytes, line: int = 76, eol: str = "\r\n") -> str:
    text = base64.b64encode(data).decode()
    return eol.join(text[i:i + line] for i in range(0, len(text), line))


def _mail(parts, *, subject="Письмо", eol="\r\n"):
    """parts: [(имя или None, тип, данные, способ: base64/qp, disposition или None)]."""
    out = [f"From: a@example.ru{eol}To: b@example.ru{eol}Subject: {subject}{eol}MIME-Version: 1.0{eol}"
           f'Content-Type: multipart/mixed; boundary="X"{eol}{eol}'
           f"--X{eol}Content-Type: text/plain; charset=utf-8{eol}{eol}Текст письма{eol}"]
    for name, ctype, data, cte, disp in parts:
        head = f"--X{eol}Content-Type: {ctype}{eol}"
        if disp:
            head += f"Content-Disposition: {disp}" + (f'; filename="{name}"' if name else "") + eol
        head += f"Content-Transfer-Encoding: {cte}{eol}{eol}"
        body = _b64(data, eol=eol) if cte == "base64" else quopri.encodestring(data).decode().replace("\n", eol)
        out.append(head + body + eol)
    out.append(f"--X--{eol}")
    return "".join(out).encode("utf-8")


def _add(svc, acc_id, uid, raw, folder="INBOX", days_ago=1):
    rel, sha, size = svc.store.store_message(acc_id, folder, "/", uid, raw)
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    svc.db.add_message_index_batch([(acc_id, folder, 1, uid, f"<{acc_id}.{uid}.{folder}@x>", size, when, "", rel, sha,
                                     "тема", "a@example.ru", 1)])
    return svc.db.scalar("SELECT id FROM messages WHERE account_id=? AND folder=? AND uid=?", (acc_id, folder, uid))


def _accounts(svc, n=3):
    return [svc.db.create_account(models.Account(name=f"Сотрудник {i}", host="h", username=f"u{i}@x", password="p"))
            for i in range(n)]


# ---------------------------------------------------------------------------
#  Разбор письма
# ---------------------------------------------------------------------------
def test_scan_message_hashes_decoded_content():
    raw = _mail([("прайс.pdf", "application/pdf", PDF, "base64", "attachment"),
                 ("tiny.png", "image/png", b"\x89PNG" + b"0" * 100, "base64", "inline"),
                 (None, "image/png", LOGO, "base64", None),                  # картинка без имени — тоже вложение
                 ("отчёт.csv", "text/csv", b"a;b\n" * 400, "quoted-printable", "attachment")])
    items = dedup.scan_message(io.BytesIO(raw))
    by_idx = {i[0]: i for i in items}
    assert sorted(by_idx) == [0, 2, 3]                  # крошечное вложение (№1) не учитывается, но номер занимает
    assert by_idx[0][2] == len(PDF) and by_idx[0][3] > len(PDF) * 4 // 3 - 10    # в письме — base64, на треть больше
    assert by_idx[0][4] == "application/pdf" and by_idx[0][5] == "прайс.pdf"
    assert by_idx[2][2] == len(LOGO)
    # то же содержимое, закодированное иначе (строки по 60 символов, переводы строк LF), — тот же отпечаток
    other = dedup.scan_message(io.BytesIO(_mail([("copy.pdf", "application/octet-stream", PDF, "base64",
                                                  "attachment")], eol="\n")))
    assert other[0][1] == by_idx[0][1]


def test_scan_message_can_be_cancelled_inside_big_attachment():
    raw = _mail([("big.bin", "application/octet-stream", os.urandom(6_000_000), "base64", "attachment")])
    with pytest.raises(JobCancelled):
        dedup.scan_message(io.BytesIO(raw), cancelled=lambda: True)


# ---------------------------------------------------------------------------
#  Подсчёт и отчёт
# ---------------------------------------------------------------------------
def _fill(svc):
    a, b, c = _accounts(svc)
    mass = _mail([("Прайс.pdf", "application/pdf", PDF, "base64", "attachment")], subject="Прайс")
    for acc in (a, b, c):                                       # рассылка всем троим
        _add(svc, acc, 1, mass)
    _add(svc, a, 2, _mail([("price_copy.pdf", "application/pdf", PDF, "base64", "attachment"),
                           ("logo.png", "image/png", LOGO, "base64", "inline")]))     # тот же PDF ещё раз в ящике «a»
    _add(svc, b, 2, _mail([("logo.png", "image/png", LOGO, "base64", "inline")]))
    _add(svc, c, 2, _mail([("unique.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            OTHER, "base64", "attachment")]))
    _add(svc, c, 3, b"From: x@y\r\nSubject: no attach\r\n\r\ntext\r\n")
    return a, b, c


def test_report_counts_savings(services):
    svc = services
    a, b, c = _fill(svc)
    res = dedup.process_pending(svc)
    assert res["processed"] == 7 and res["errors"] == 0 and res["pending"] == 0
    rep = dedup.build_report(svc)
    assert rep["attachments"]["count"] == 7                    # 4×PDF + 2×логотип + docx
    assert rep["unique"]["count"] == 3
    assert rep["duplicates"] == {"copies": 4, "groups": 2}
    rows = svc.db.query("SELECT hash, enc_size FROM attach_hashes")
    by_hash = {}
    for r in rows:
        by_hash.setdefault(r["hash"], []).append(r["enc_size"])
    expected = sum(sum(v) - max(v) for v in by_hash.values())
    assert rep["savings"]["bytes"] == expected and expected > 3 * len(PDF)
    # внутри одного ящика повторяется только PDF в ящике «a»
    pdf_enc = max(by_hash[max(by_hash, key=lambda h: len(by_hash[h]))])
    assert abs(rep["savings"]["within_mailbox"] - pdf_enc) < 200
    top = rep["top"][0]
    assert top["copies"] == 4 and top["mailboxes"] == 3 and top["names"] == 2 and top["type_label"] == "PDF"
    assert top["example"]["account_id"] == a and top["example"]["folder"] == "INBOX"
    assert rep["by_type"][0]["label"] == "PDF"
    assert {x["account_id"] for x in rep["by_account"]} == {a, b, c}
    assert rep["coverage"]["complete"] and rep["savings"]["percent_of_archive"] > 30
    assert rep["savings"]["separate_store"] > rep["savings"]["bytes"]
    assert "освободило бы" in dedup.summary_text(rep)


def test_incremental_and_deletions(services):
    svc = services
    a, b, c = _fill(svc)
    dedup.process_pending(svc)
    new_id = _add(svc, b, 3, _mail([("Прайс.pdf", "application/pdf", PDF, "base64", "attachment")]))
    assert dedup.status(svc)["pending"] == 1
    res = dedup.process_pending(svc)
    assert res["processed"] == 1 and res["attachments"] == 1
    assert dedup.build_report(svc)["duplicates"]["copies"] == 5
    svc.db.execute("DELETE FROM messages WHERE id=?", (new_id,))              # ретеншн удалил письмо
    assert dedup.build_report(svc)["duplicates"]["copies"] == 4
    svc.db.execute("DELETE FROM accounts WHERE id=?", (a,))                   # удалили ящик целиком
    rep = dedup.build_report(svc)
    assert rep["attachments"]["count"] == 4 and rep["duplicates"]["copies"] == 1     # остался PDF в «b» и «c»


def test_whole_message_duplicates(services):
    svc = services
    (a,) = _accounts(svc, 1)
    raw = _mail([("a.pdf", "application/pdf", OTHER, "base64", "attachment")])
    _add(svc, a, 1, raw, folder="INBOX")
    _add(svc, a, 2, raw, folder="Проекты")                                  # то же письмо во второй папке
    dedup.process_pending(svc)
    rep = dedup.build_report(svc)
    assert rep["whole_messages"]["copies"] == 1 and rep["whole_messages"]["bytes"] == len(raw)
    assert rep["duplicates"]["copies"] == 1


def test_encrypted_copy_and_unreadable_files(services, tmp_path):
    from mailarchiver.storage import crypto
    svc = services
    key = tmp_path / "k"
    crypto.generate_key_file(str(key))
    svc.set_rt("storage", "encryption_key_file", str(key))
    svc.set_rt("storage", "encrypt", True)
    svc.apply_runtime_settings()
    a, b = _accounts(svc, 2)
    _add(svc, a, 1, _mail([("x.pdf", "application/pdf", PDF, "base64", "attachment")]))
    broken = _add(svc, b, 1, _mail([("x.pdf", "application/pdf", PDF, "base64", "attachment")]))
    rel = svc.db.scalar("SELECT stored_path FROM messages WHERE id=?", (broken,))
    os.remove(svc.store.message_path(b, rel))                                  # файл письма пропал
    events = []
    res = dedup.process_pending(svc, event=lambda lvl, msg: events.append(msg))
    assert res["processed"] == 2 and res["errors"] == 1 and res["pending"] == 0
    assert any(f"#{broken}" in e for e in events)
    rep = dedup.build_report(svc)
    assert rep["attachments"]["count"] == 1 and rep["coverage"]["errors"] == 1 and rep["encrypted"]


def test_reset_generation_guard(services, monkeypatch):
    svc = services
    _fill(svc)
    monkeypatch.setattr(dedup, "BATCH", 2)
    calls = {"n": 0}

    def progress(done, total, text):
        calls["n"] += 1
        if calls["n"] == 1:
            dedup.reset(svc.db)              # «Посчитать заново» посреди прохода
    res = dedup.process_pending(svc, progress=progress)
    assert res["stopped"] == "reset"
    assert dedup.status(svc)["pending"] == 7 and dedup.status(svc)["attachments"] == 0
    dedup.process_pending(svc)
    assert dedup.build_report(svc)["attachments"]["count"] == 7


# ---------------------------------------------------------------------------
#  Задание
# ---------------------------------------------------------------------------
def test_job_yields_to_waiting_backups_and_continues(services, monkeypatch):
    svc = services
    a, _b, _c = _fill(svc)
    monkeypatch.setattr(dedup, "BATCH", 2)
    svc.queue.enqueue(models.JobType.BACKUP, a, {})                          # копирование ждёт слота
    ctx = jobs_mod.JobContext(svc, 1, models.JobType.DEDUP_REPORT, None, {"min_seconds": 0.000001})
    res = jobs_mod.handle_dedup_report(ctx)
    assert res["processed"] == 2 and res["pending"] == 5
    assert "Продолжение" in res["summary"]
    cont = [j for j in svc.db.active_jobs() if j["type"] == models.JobType.DEDUP_REPORT]
    assert len(cont) == 1 and cont[0]["priority"] == 9
    assert dedup.load_report(svc)["coverage"]["complete"] is False          # отчёт по прочитанной части уже есть
    # очередь свободна — задание дочитывает всё за раз
    for j in svc.db.active_jobs():
        svc.db.finish_job(j["id"], models.JobStatus.CANCELLED)
    res = jobs_mod.handle_dedup_report(jobs_mod.JobContext(svc, 2, models.JobType.DEDUP_REPORT, None, {}))
    assert res["pending"] == 0 and "Продолжение" not in res["summary"]
    assert dedup.load_report(svc)["coverage"]["complete"] is True
    # «Посчитать заново»
    res = jobs_mod.handle_dedup_report(jobs_mod.JobContext(svc, 3, models.JobType.DEDUP_REPORT, None, {"full": True}))
    assert res["processed"] == 7


# ---------------------------------------------------------------------------
#  API
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_dedup_api(client):
    _login(client)
    svc = client.app.state.services
    empty = client.get("/api/analytics/dedup").json()
    assert empty["report"] is None and empty["status"]["total"] == 0
    _fill(svc)
    r = client.post("/api/analytics/dedup/scan", json={}).json()
    assert r["job_id"]
    import time
    for _ in range(200):
        job = svc.db.get_job(r["job_id"])
        if job["status"] not in models.JobStatus.ACTIVE:
            break
        time.sleep(0.05)
    assert job["status"] == models.JobStatus.SUCCESS, job["error"]
    data = client.get("/api/analytics/dedup").json()
    assert data["report"]["duplicates"]["copies"] == 4 and not data["running"]
    assert any(a["action"] == "dedup_report" for a in svc.db.list_audit())


def test_long_office_types_keep_their_label():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    items = dedup.scan_message(io.BytesIO(_mail([("Договор.docx", docx, OTHER, "base64", "attachment")])))
    assert items[0][4] == docx                                  # тип не обрезан
    assert dedup._type_label(items[0][4]) == "Word (.docx)"
    assert dedup._type_label("image/x-icon") == "Картинки X-ICON" and dedup._type_label("") == "(тип не указан)"
