"""
Смена UIDVALIDITY (переезд почты, пересозданная папка) без повторного скачивания
и отсев писем старше срока хранения ящика — на заглушке IMAP с настоящими БД и
хранилищем.
"""
import os
from datetime import date, datetime, timedelta, timezone

from imapclient.exceptions import IMAPClientError

from mailarchiver import models
from mailarchiver.imap import backup as backup_mod
from mailarchiver.imap import client as client_mod
from mailarchiver.models import JobType
from mailarchiver.queue import jobs as jobs_mod
from mailarchiver.queue.jobs import JobContext

from test_imap_folders import FakeIMAP, _patch_connection


class DatedIMAP(FakeIMAP):
    """Заглушка с датами писем, поиском SINCE и выдачей заголовка Message-ID."""

    def __init__(self, *args, dates=None, since_fails=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates = dict(dates or {})          # {(папка, uid): datetime}
        self.since_fails = since_fails
        self.since_calls = []
        self.header_fetches = 0
        self.date_fetches = 0
        self.since_glitch = False                # сервер вернул пустой ответ без ошибки

    def _date(self, uid):
        return self.dates.get((self.current, uid)) or datetime.now()

    def search(self, criteria):
        crit = list(criteria) if isinstance(criteria, (list, tuple)) else [criteria]
        if "SINCE" in crit:
            i = crit.index("SINCE")
            since = crit[i + 1]
            self.since_calls.append((since, crit[:i]))
            if self.since_fails:
                raise IMAPClientError("SEARCH failed: SINCE is not supported")
            if self.since_glitch:
                return []
            base = super().search(crit[:i] or ["ALL"])
            return [u for u in base if self._date(u).date() >= since]
        return super().search(criteria)

    def fetch(self, uids, fields):
        if list(fields) == [b"INTERNALDATE"]:
            self.date_fetches += 1
            msgs = self.messages.get(self.current, {})
            return {uid: {b"INTERNALDATE": self._date(uid)} for uid in self._expand(uids) if uid in msgs}
        if any(b"HEADER.FIELDS" in f for f in fields):
            self.header_fetches += 1
            msgs = self.messages.get(self.current, {})
            out = {}
            for uid in self._expand(uids):
                raw = msgs.get(uid)
                if raw is None:
                    continue
                head = raw.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
                picked = [line for line in head if line.lower().startswith((b"message-id:", b"date:"))]
                out[uid] = {b"RFC822.SIZE": len(raw),
                            b"BODY[HEADER.FIELDS (MESSAGE-ID DATE)]": (b"\r\n".join(picked) + b"\r\n\r\n")
                            if picked else b"\r\n"}
            return out
        out = super().fetch(uids, fields)
        if b"BODY.PEEK[]" in fields:
            for uid, item in out.items():
                item[b"INTERNALDATE"] = self.dates.get((self.current, uid))
        return out


def _raw(n, mid=True):
    head = b"From: a@example.com\r\nSubject: msg %d\r\nDate: Tue, %02d Sep 2026 10:00:00 +0300\r\n" % (n, 1 + n % 28)
    if mid:
        head += b"Message-ID: <m%d@example.com>\r\n" % n
    return head + b"\r\nbody %d\r\n" % n


def _account(svc, **kw):
    data = dict(name="Переезд", host="mx.example.ru", port=993, username="move@example.ru", password="p")
    data.update(kw)
    return svc.db.get_account(svc.db.create_account(models.Account(**data)))


def _run(svc, acc, **engine_kw):
    engine = backup_mod.BackupEngine(svc.db, svc.store, client_mod.ConnectOptions(), **engine_kw)
    events = []
    res = engine.run(acc, event_cb=lambda level, msg: events.append((level, msg)))
    return res, events


def _index(svc, acc):
    return [(r["uidvalidity"], r["uid"], r["stored_path"]) for r in svc.db.query(
        "SELECT uidvalidity, uid, stored_path FROM messages WHERE account_id=? ORDER BY uidvalidity, uid",
        (acc.id,))]


def _files(svc, acc):
    root = svc.store.account_dir(acc.id)
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _dirs, names in os.walk(root) for f in names if "/cur" in d or "\\cur" in d)


# ---------------------------------------------------------------------------
#  Смена UIDVALIDITY: уже скачанное узнаётся по Message-ID, размеру и дате
# ---------------------------------------------------------------------------
def test_uidvalidity_change_relinks_known_messages(services, monkeypatch):
    a, b, c, d = _raw(1), _raw(2), _raw(3), _raw(4, mid=False)
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: a, 2: b, 3: c, 4: d}}, uidvalidity=1000)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    first, _ = _run(services, acc)
    assert first.messages_new == 4 and first.messages_relinked == 0
    assert fake.header_fetches == 0                  # без смены UIDVALIDITY сверки нет
    before = _index(services, acc)
    files_before = _files(services, acc)

    # Переезд: новая нумерация; письмо 3 сервер пересобрал (другой размер); пришло новое письмо.
    c2 = c.replace(b"Subject:", b"X-Moved: yes\r\nSubject:")
    fake.uidvalidity = 2000
    fake.messages = {"INBOX": {101: a, 102: b, 103: c2, 104: d, 105: _raw(5)}}
    second, events = _run(services, acc)
    assert second.messages_relinked == 2
    assert second.messages_new == 3                  # пересобранное, без Message-ID и новое
    assert second.status_label == "success"
    after = _index(services, acc)
    assert [(v, u) for v, u, _p in after] == [(1000, 3), (1000, 4), (2000, 101), (2000, 102),
                                              (2000, 103), (2000, 104), (2000, 105)]
    # перепривязанные записи указывают на ТЕ ЖЕ файлы — ничего не скачано и не переписано
    old_paths = {u: p for _v, u, p in before}
    new_paths = {u: p for _v, u, p in after if _v == 2000}
    assert new_paths[101] == old_paths[1] and new_paths[102] == old_paths[2]
    assert set(files_before) <= set(_files(services, acc)) and len(_files(services, acc)) == 7
    assert any(lvl == "INFO" and "уже есть в архиве (совпали Message-ID, размер и дата), — 2:" in m and "скачать осталось 3" in m
               for lvl, m in events)
    assert any(lvl == "WARNING" and "повторно не скачаются" in m for lvl, m in events)

    # Следующий прогон — обычный: ничего не качается и не сверяется повторно.
    third, _ = _run(services, acc)
    assert third.messages_new == 0 and third.messages_relinked == 0
    assert fake.header_fetches == 1


def test_uidvalidity_change_without_matches_downloads_everything(services, monkeypatch):
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2)}}, uidvalidity=10)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    _run(services, acc)
    fake.uidvalidity = 11
    fake.messages = {"INBOX": {1: _raw(7), 2: _raw(8)}}      # папку пересоздали с другими письмами
    res, events = _run(services, acc)
    assert res.messages_relinked == 0 and res.messages_new == 2
    assert any("не найдено" in m for _l, m in events)
    assert len(_index(services, acc)) == 4                   # прежние записи остались историческими


def test_rekey_messages_reports_only_updated_rows(services):
    acc = _account(services)
    db = services.db
    db.add_message_index(acc.id, "INBOX", 5, 1, "<x@y>", 10, "2026-01-01T00:00:00+00:00", "", "cur/a", "0" * 64)
    db.add_message_index(acc.id, "INBOX", 6, 7, "<z@y>", 10, "2026-01-01T00:00:00+00:00", "", "cur/b", "1" * 64)
    ids = {r["uid"]: r["id"] for r in db.query("SELECT id, uid FROM messages WHERE account_id=?", (acc.id,))}
    # UID 7 под UIDVALIDITY 6 уже занят — такую пару пропускаем, не роняя остальные
    done = db.rekey_messages(6, [(ids[1], 7), (ids[1], 9)])
    assert done == [(ids[1], 9)]
    assert db.rekey_messages(6, []) == []


# ---------------------------------------------------------------------------
#  Срок хранения ящика: старые письма не скачиваются
# ---------------------------------------------------------------------------
def test_messages_older_than_retention_are_not_downloaded(services, monkeypatch):
    now = datetime.now()
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2), 3: _raw(3)}},
                     dates={("INBOX", 1): now - timedelta(days=40), ("INBOX", 2): now - timedelta(days=2),
                            ("INBOX", 3): now})
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    res, events = _run(services, acc, retention_days=7)
    assert res.messages_new == 2 and res.messages_outside_retention == 1
    assert [u for _v, u, _p in _index(services, acc)] == [2, 3]
    since, scope = fake.since_calls[0]
    assert since == date.today() - timedelta(days=8)          # срок + сутки запаса
    assert scope == ["UID", "1:3"]                            # только по диапазону новых писем
    assert any("старше срока хранения ящика (7 дн.): 1" in m for _l, m in events)
    assert services.db.count_retired(acc.id) == 1

    # Следующие прогоны старое письмо не перепроверяют и ошибкой не считают.
    fake.since_calls.clear()
    again, _ = _run(services, acc, retention_days=7)
    assert again.messages_new == 0 and again.messages_outside_retention == 0
    assert again.status_label == "success" and fake.since_calls == []

    # Срок хранения увеличили до «хранить всё» — письмо скачивается.
    services.db.clear_retired(acc.id)
    full, _ = _run(services, acc, retention_days=0)
    assert full.messages_new == 1 and [u for _v, u, _p in _index(services, acc)] == [1, 2, 3]


def test_retention_filter_falls_back_to_full_download(services, monkeypatch):
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2)}},
                     dates={("INBOX", 1): datetime.now() - timedelta(days=90)}, since_fails=True)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    res, _ = _run(services, acc, retention_days=3)
    # сервер не выполнил поиск по дате — качаем всё, как раньше (очистка уберёт лишнее)
    assert res.messages_new == 2 and res.messages_outside_retention == 0 and res.errors == 0


def test_no_retention_means_no_date_search(services, monkeypatch):
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1)}},
                     dates={("INBOX", 1): datetime.now() - timedelta(days=900)})
    _patch_connection(monkeypatch, fake)
    res, _ = _run(services, _account(services))
    assert res.messages_new == 1 and fake.since_calls == []


# ---------------------------------------------------------------------------
#  Задание копирования: срок хранения ящика передаётся, итог пишется
# ---------------------------------------------------------------------------
def test_backup_job_passes_mailbox_retention_and_reports_counters(services, monkeypatch):
    seen = []

    class SpyEngine:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs.get("retention_days"))

        def run(self, acc, **kwargs):
            res = backup_mod.BackupResult(folders_total=1, folders_read=1)
            res.messages_relinked, res.messages_outside_retention = 5, 7
            return res

    monkeypatch.setattr(jobs_mod, "BackupEngine", SpyEngine)
    acc = _account(services, name="Короткий", retention_days=3)
    job_id = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "tester")
    ctx = JobContext(services, job_id, JobType.BACKUP, acc.id, {})
    out = jobs_mod._backup_account(ctx, acc)
    assert seen == [3]
    assert out["messages_relinked"] == 5 and out["messages_outside_retention"] == 7
    assert "узнано в архиве писем — 5," in out["summary"]
    assert "старше срока хранения ящика (3 дн.) не скачано: 7" in out["summary"]

    # Архив под удержанием — сроки хранения не действуют, качается всё.
    until = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    services.db.set_account_hold(acc.id, until, "manual")
    jobs_mod._backup_account(ctx, services.db.get_account(acc.id))
    assert seen[-1] == 0


def test_bad_date_search_reply_does_not_lose_new_mail(services, monkeypatch):
    """Пустой (но «успешный») ответ SEARCH SINCE не должен навсегда отсечь новые письма."""
    now = datetime.now()
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2), 3: _raw(3)}},
                     dates={("INBOX", 1): now - timedelta(days=40), ("INBOX", 2): now, ("INBOX", 3): now})
    fake.since_glitch = True
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    res, _ = _run(services, acc, retention_days=7)
    # даты перепроверены по INTERNALDATE: отсеяно только действительно старое письмо
    assert fake.date_fetches == 1
    assert res.messages_new == 2 and res.messages_outside_retention == 1
    assert services.db.count_retired(acc.id) == 1


def test_ambiguous_message_ids_are_downloaded_not_relinked(services, monkeypatch):
    """Одинаковые Message-ID и размер у разных писем: сопоставлять «по порядку» нельзя."""
    def scan(day):
        return (b"From: scanner@example.ru\r\nSubject: scan\r\nMessage-ID: <scan@device>\r\n\r\npage %02d\r\n" % day)

    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {d: scan(d) for d in range(1, 11)}},
                     uidvalidity=500)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    first, _ = _run(services, acc)
    assert first.messages_new == 10
    # переезд: на новом сервере дни 5–12 (8 писем с тем же Message-ID и размером)
    fake.uidvalidity = 501
    fake.messages = {"INBOX": {100 + d: scan(d) for d in range(5, 13)}}
    res, events = _run(services, acc)
    assert res.messages_relinked == 0 and res.messages_new == 8        # 11 и 12 — не потеряны
    stored = {r["uid"] for r in services.db.query(
        "SELECT uid FROM messages WHERE account_id=? AND uidvalidity=501", (acc.id,))}
    assert stored == {100 + d for d in range(5, 13)}


def test_relink_only_unique_pairs(services, monkeypatch):
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2), 3: _raw(2)}},
                     uidvalidity=700)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    _run(services, acc)
    fake.uidvalidity = 701
    fake.messages = {"INBOX": {11: _raw(1), 12: _raw(2), 13: _raw(2)}}
    res, events = _run(services, acc)
    # письмо 1 однозначно — привязано; два одинаковых письма 2 — скачаны заново
    assert res.messages_relinked == 1 and res.messages_new == 2
    assert any("однозначно не сопоставить (одинаковые Message-ID, размер и дата у нескольких писем), — 2" in m
               for _l, m in events)


def test_retention_filter_runs_before_relink(services, monkeypatch):
    """Ящик со сроком хранения после переезда: старые письма не сверяются с архивом и не скачиваются."""
    now = datetime.now()
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2)}},
                     dates={("INBOX", 1): now - timedelta(days=30), ("INBOX", 2): now}, uidvalidity=900)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    _run(services, acc)                                   # без срока хранения — скачано всё
    fake.uidvalidity = 901
    fake.messages = {"INBOX": {21: _raw(1), 22: _raw(2)}}
    fake.dates = {("INBOX", 21): now - timedelta(days=30), ("INBOX", 22): now}
    res, _ = _run(services, acc, retention_days=7)
    assert res.messages_outside_retention == 1 and res.messages_relinked == 1 and res.messages_new == 0


def test_hold_makes_retired_mail_downloadable_again(client):
    """Удержание архива (вручную, групповым действием, при увольнении) снимает отсев по сроку хранения."""
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    a = _account(svc, name="Короткий", retention_days=3)
    b = _account(svc, name="Второй", username="b@example.ru", retention_days=3)
    for acc in (a, b):
        svc.db.retire_uids(acc.id, "INBOX", 1, [1, 2, 3])
    r = client.post(f"/api/accounts/{a.id}/hold", json={"until": "forever"})
    assert r.status_code == 200 and svc.db.count_retired(a.id) == 0
    assert svc.db.count_retired(b.id) == 3
    until = (datetime.now(timezone.utc) + timedelta(days=90)).date().isoformat()
    r = client.post("/api/accounts/bulk", json={"action": "hold_set", "ids": [b.id], "params": {"until": until},
                                                "preview": False, "confirm": ""}).json()
    assert r["counts"]["ok"] == 1 and svc.db.count_retired(b.id) == 0


def test_same_message_id_and_size_but_other_date_is_downloaded(services, monkeypatch):
    """Ежедневный отчёт: тот же Message-ID и размер, другой день — это другое письмо."""
    def report(day):
        return (b"From: robot@example.ru\r\nSubject: daily\r\nDate: Mon, %02d Sep 2026 06:00:00 +0300\r\n"
                b"Message-ID: <daily@robot>\r\n\r\nreport\r\n" % day)

    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: report(9)}}, uidvalidity=40)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    _run(services, acc)
    fake.uidvalidity = 41
    fake.messages = {"INBOX": {7: report(10)}}
    res, events = _run(services, acc)
    assert res.messages_relinked == 0 and res.messages_new == 1
    # то же письмо (тот же Date) — привязывается
    fake.uidvalidity = 42
    fake.messages = {"INBOX": {3: report(10)}}
    res, _ = _run(services, acc)
    assert res.messages_relinked == 1 and res.messages_new == 0


def test_backup_of_held_account_forgets_retired_mail(services, monkeypatch):
    """Удержание поставили во время копирования: следующее копирование скачает всё, что есть на сервере."""
    class SpyEngine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, acc, **kwargs):
            return backup_mod.BackupResult(folders_total=1, folders_read=1)

    monkeypatch.setattr(jobs_mod, "BackupEngine", SpyEngine)
    acc = _account(services, name="Удержание", retention_days=3)
    services.db.retire_uids(acc.id, "INBOX", 1, [1, 2])
    until = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
    services.db.execute("UPDATE accounts SET hold_until=?, hold_reason='manual' WHERE id=?", (until, acc.id))
    job_id = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "tester")
    jobs_mod._backup_account(JobContext(services, job_id, JobType.BACKUP, acc.id, {}), services.db.get_account(acc.id))
    assert services.db.count_retired(acc.id) == 0
    # все папки прочитаны — отметка полного копирования для прежних копий
    assert services.db.complete_backup_at(acc.id)



def test_messages_without_date_header_are_downloaded_not_relinked(services, monkeypatch):
    """Без заголовка Date письма одного Message-ID и размера не различить — скачиваем."""
    def report(day):
        return b"From: robot@example.ru\r\nSubject: daily\r\nMessage-ID: <nodate@robot>\r\n\r\nday %02d\r\n" % day

    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: report(9)}}, uidvalidity=60)
    _patch_connection(monkeypatch, fake)
    acc = _account(services)
    _run(services, acc)
    fake.uidvalidity = 61
    fake.messages = {"INBOX": {5: report(10)}}
    res, _ = _run(services, acc)
    assert res.messages_relinked == 0 and res.messages_new == 1


def test_backup_that_read_no_folders_is_not_complete(services, monkeypatch):
    class EmptyEngine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, acc, **kwargs):
            return backup_mod.BackupResult(folders_total=0, folders_read=0)

    monkeypatch.setattr(jobs_mod, "BackupEngine", EmptyEngine)
    acc = _account(services, name="Пустой")
    job_id = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "tester")
    out = jobs_mod._backup_account(JobContext(services, job_id, JobType.BACKUP, acc.id, {}), acc)
    assert out["final_status"] == "success"
    assert services.db.get_meta(f"backup_complete_at:{acc.id}") is None


def test_forget_retired_mail_per_mailbox(client, monkeypatch):
    """«Забыть отсеянные письма»: список отсеянных по сроку забывается только у этого ящика,
    и следующее копирование заново проверяет даты (часы сервера архива уходили вперёд)."""
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
    svc = client.app.state.services
    svc.queue.stop(timeout=2)
    svc.queue._shutting_down.clear()
    a = _account(svc, name="Сбились часы", retention_days=7)
    b = _account(svc, name="Соседний", username="b@example.ru", retention_days=7)
    svc.db.retire_uids(a.id, "INBOX", 1, [1, 2])
    svc.db.retire_uids(b.id, "INBOX", 1, [5])
    assert client.get(f"/api/accounts/{a.id}").json()["retired"] == 2
    r = client.post(f"/api/accounts/{a.id}/retired/clear")
    assert r.status_code == 200 and r.json()["cleared"] == 2
    assert svc.db.count_retired(a.id) == 0 and svc.db.count_retired(b.id) == 1
    assert any(x["action"] == "account_retired_clear" for x in client.get("/api/audit").json())
    assert client.post("/api/accounts/999999/retired/clear").status_code in (400, 404)

    # следующее копирование: свежее письмо скачивается, старое снова отсеивается
    now = datetime.now()
    fake = DatedIMAP([((), "/", "INBOX")], messages={"INBOX": {1: _raw(1), 2: _raw(2)}},
                     dates={("INBOX", 1): now - timedelta(days=40), ("INBOX", 2): now - timedelta(days=1)},
                     uidvalidity=1)
    _patch_connection(monkeypatch, fake)
    res, _ = _run(svc, svc.db.get_account(a.id), retention_days=7)
    assert res.messages_new == 1 and res.messages_outside_retention == 1
    assert svc.db.count_retired(a.id) == 1
