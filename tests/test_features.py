"""Тесты новых функций: просмотр писем, ретеншн на ящик, разбор письма."""
import os

import pytest

from mailarchiver import models
from mailarchiver.mailview import parse_message, get_attachment


SAMPLE_HTML = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: bob@example.com\r\n"
    b"Subject: =?utf-8?B?0J/RgNC40LLQtdGC?=\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"\xd0\xa2\xd0\xb5\xd0\xba\xd1\x81\xd1\x82 \xd0\xbf\xd0\xb8\xd1\x81\xd1\x8c\xd0\xbc\xd0\xb0\r\n"
    b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>HTML</p>\r\n"
    b'--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="doc.pdf"\r\n\r\n'
    b"%PDF-1.4 fake\r\n--B--\r\n"
)


def test_parse_message():
    m = parse_message(SAMPLE_HTML)
    assert m["headers"]["subject"] == "Привет"
    assert "Текст письма" in m["text"]
    assert "<p>HTML</p>" in m["html"]
    assert len(m["attachments"]) == 1
    assert m["attachments"][0]["filename"] == "doc.pdf"


def test_get_attachment():
    att = get_attachment(SAMPLE_HTML, 0)
    assert att is not None
    filename, ctype, data = att
    assert filename == "doc.pdf" and ctype == "application/pdf"
    assert data.startswith(b"%PDF")
    assert get_attachment(SAMPLE_HTML, 5) is None


def test_retention_per_account(services):
    acc = models.Account(name="R", host="h", port=993, username="u", password="p", retention_days=3)
    aid = services.db.create_account(acc)
    got = services.db.get_account(aid)
    assert got.retention_days == 3
    services.db.set_account_retention(aid, 7)
    assert services.db.get_account(aid).retention_days == 7


def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})


def test_retention_endpoint(client):
    _login(client)
    aid = client.post("/api/accounts", json={
        "name": "Box", "host": "imap.example.com", "port": 993,
        "username": "u@example.com", "password": "secret", "security": "ssl", "auth_type": "password",
    }).json()["id"]
    r = client.post(f"/api/accounts/{aid}/retention", json={"days": 3, "run_now": False})
    assert r.status_code == 200 and r.json()["days"] == 3 and r.json()["sweep_cron"]
    assert client.get(f"/api/accounts/{aid}").json()["retention_days"] == 3
    # отдельное расписание не нужно: ежедневная очистка обходит все ящики
    assert client.post(f"/api/accounts/{aid}/retention", json={"days": -5}).status_code == 400


def test_legacy_retention_schedules_removed_once(services):
    """Расписания очистки, которые до 1.3.0 заводились ящикам автоматически,
    убираются при обновлении один раз; прочие расписания не трогаются."""
    from mailarchiver.models import JobType, ScheduleKind
    db = services.db
    a1 = db.create_account(models.Account(name="A", host="h", port=993, username="a", password="p",
                                          retention_days=3))
    a2 = db.create_account(models.Account(name="B", host="h", port=993, username="b", password="p"))
    legacy = db.create_schedule(a1, ScheduleKind.CRON, JobType.RETENTION, cron_expr="30 3 * * *")
    legacy_off = db.create_schedule(a2, ScheduleKind.CRON, JobType.RETENTION, cron_expr="30 3 * * *",
                                    enabled=False)
    custom = db.create_schedule(a2, ScheduleKind.CRON, JobType.RETENTION, cron_expr="0 5 * * 0")
    backup = db.create_schedule(a1, ScheduleKind.CRON, JobType.BACKUP, cron_expr="30 3 * * *")
    # база «прежней версии»: отметки о выполненной чистке ещё нет
    db.execute("DELETE FROM meta WHERE key=?", (db._LEGACY_RETENTION_MARK,))
    db.init_schema()
    left = {row["id"] for row in db.list_schedules()}
    assert legacy not in left and legacy_off not in left
    assert {custom, backup} <= left
    assert any(row["action"] == "schedules_cleanup" for row in db.list_audit())
    # повторный запуск службы не трогает расписание, созданное вручную позже
    again = db.create_schedule(a1, ScheduleKind.CRON, JobType.RETENTION, cron_expr="30 3 * * *")
    db.init_schema()
    assert again in {row["id"] for row in db.list_schedules()}


def test_longer_retention_forgets_retired_uids(client):
    """Срок хранения увеличили — вычищенные по старому сроку письма снова скачаются."""
    _login(client)
    svc = client.app.state.services
    aid = client.post("/api/accounts", json={
        "name": "Box", "host": "imap.example.com", "port": 993,
        "username": "u@example.com", "password": "secret"}).json()["id"]
    client.post(f"/api/accounts/{aid}/retention", json={"days": 3, "run_now": False})
    svc.db.execute("INSERT INTO retired_uids(account_id, folder, uidvalidity, uid, retired_at) "
                   "VALUES(?,?,?,?,?)", (aid, "INBOX", 1, 5, "2026-01-01"))
    client.post(f"/api/accounts/{aid}/retention", json={"days": 1, "run_now": False})
    assert svc.db.count_retired(aid) == 1, "срок сократили — «надгробия» остаются"
    client.post(f"/api/accounts/{aid}/retention", json={"days": 0, "run_now": False})
    assert svc.db.count_retired(aid) == 0
    # то же через форму ящика
    client.post(f"/api/accounts/{aid}/retention", json={"days": 3, "run_now": False})
    svc.db.execute("INSERT INTO retired_uids(account_id, folder, uidvalidity, uid, retired_at) "
                   "VALUES(?,?,?,?,?)", (aid, "INBOX", 1, 6, "2026-01-01"))
    acc = client.get(f"/api/accounts/{aid}").json()
    body = {k: acc[k] for k in ("name", "host", "port", "username", "security", "auth_type")}
    body.update(password="", retention_days=30)
    assert client.put(f"/api/accounts/{aid}", json=body).status_code == 200
    assert svc.db.count_retired(aid) == 0


def test_mail_viewer_endpoints(client):
    _login(client)
    svc = client.app.state.services
    acc = models.Account(name="MailBox", host="h", port=993, username="u", password="p")
    aid = svc.db.create_account(acc)
    # положим письмо в хранилище и индекс напрямую
    raw = (b"From: Alice <a@ex.com>\r\nTo: b@ex.com\r\nSubject: Hi\r\n\r\n"
           b"\xd0\xa2\xd0\xb5\xd0\xbb\xd0\xbe")  # "Тело"
    rel, digest, size = svc.store.store_message(aid, "INBOX", "/", 1, raw, flags=["\\Seen"], internaldate=1700000000)
    svc.db.add_message_index(aid, "INBOX", 1, 1, "<x@ex.com>", size, "2023-11-14T22:13:20+00:00",
                             "\\Seen", rel, digest, subject="Hi", from_addr="Alice <a@ex.com>")
    # папки
    folders = client.get(f"/api/accounts/{aid}/mailfolders").json()
    assert folders["total"] == 1 and folders["folders"][0]["folder"] == "INBOX"
    # список
    lst = client.get(f"/api/accounts/{aid}/messages?folder=INBOX").json()
    assert lst["total"] == 1
    pk = lst["messages"][0]["id"]
    assert lst["messages"][0]["subject"] == "Hi" and lst["messages"][0]["seen"] is True
    # чтение
    msg = client.get(f"/api/accounts/{aid}/messages/{pk}").json()
    assert "Тело" in msg["text"]
    # .eml
    eml = client.get(f"/api/accounts/{aid}/messages/{pk}/raw")
    assert eml.status_code == 200 and b"Subject: Hi" in eml.content


def test_backup_all_accounts(client):
    """Кнопка «копия всех ящиков»: ставит задание на каждый включённый ящик."""
    _login(client)
    svc = client.app.state.services
    a1 = svc.db.create_account(models.Account(name="A1", host="h", port=993, username="u1", password="p"))
    a2 = svc.db.create_account(models.Account(name="A2", host="h", port=993, username="u2", password="p"))
    off = svc.db.create_account(models.Account(name="Off", host="h", port=993, username="u3",
                                               password="p", enabled=False))
    r = client.post("/api/accounts/backup-all")
    assert r.status_code == 200
    data = r.json()
    started = {s["account_id"] for s in data["started"]}
    # включённые попали в очередь, выключенный — нет
    assert a1 in started and a2 in started
    assert off not in started
    assert data["total_enabled"] == len(started)
    # повторное нажатие не удваивает работу: ящики уже в очереди
    again = client.post("/api/accounts/backup-all").json()
    assert len(again["started"]) == 0
    assert {s["account_id"] for s in again["skipped"]} == started


# ---------------------------------------------------------------------------
#  Пересоздание копии: докачка потерянных файлов и полная очистка
# ---------------------------------------------------------------------------
def test_rebuild_missing_drops_index_rows_without_files(services, tmp_path):
    """Файла на диске нет — запись индекса убираем, чтобы письмо скачалось заново."""
    from mailarchiver.models import Account
    from mailarchiver.queue.jobs import _rebuild_missing

    acc_id = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    acc = services.db.get_account(acc_id)
    acc_dir = services.store.account_dir(acc_id)
    os.makedirs(os.path.join(acc_dir, "INBOX", "cur"), exist_ok=True)
    alive = os.path.join("INBOX", "cur", "alive.eml")
    with open(os.path.join(acc_dir, alive), "wb") as fh:
        fh.write("From: a@b\r\n\r\nтело".encode("utf-8"))
    empty = os.path.join("INBOX", "cur", "empty.eml")
    open(os.path.join(acc_dir, empty), "wb").close()      # файл есть, но пустой

    for uid, rel in ((1, alive), (2, empty), (3, os.path.join("INBOX", "cur", "gone.eml"))):
        services.db.add_message_index(acc_id, "INBOX", 1000, uid, f"<{uid}@x>", 10,
                                      "2026-09-01T00:00:00+00:00", "", rel, "sha")
    assert services.db.count_messages(acc_id) == 3

    events = []

    class _Ctx:
        def __init__(self):
            self.db = services.db
            self.services = services

        def event(self, level, message):
            events.append((level, message))

        def progress(self, *_a, **_k):
            pass

        def is_cancelled(self):
            return False

    lost = _rebuild_missing(_Ctx(), acc)
    assert lost == 2                                   # пустой и пропавший
    assert services.db.count_messages(acc_id) == 1     # целое письмо осталось
    assert any("потерянных файлов: 2" in m for _lvl, m in events)


def test_rebuild_missing_refuses_on_unmounted_storage(services):
    """Каталога ящика нет или пропала бОльшая часть файлов — индекс не трогаем.

    Так выглядит несмонтированный диск, а не потеря писем: раньше из индекса
    удалялось всё, и записи о письмах, уже удалённых на сервере, пропадали.
    """
    import shutil
    from mailarchiver.errors import ValidationError
    from mailarchiver.models import Account
    from mailarchiver.queue.jobs import _rebuild_missing

    acc_id = services.db.create_account(Account(name="Диск", host="h", username="u", password="p"))
    acc = services.db.get_account(acc_id)
    acc_dir = services.store.account_dir(acc_id)
    os.makedirs(os.path.join(acc_dir, "INBOX", "cur"), exist_ok=True)
    for uid in range(1, 31):
        rel = os.path.join("INBOX", "cur", f"{uid}.eml")
        if uid <= 5:
            with open(os.path.join(acc_dir, rel), "wb") as fh:
                fh.write(b"From: a@b\r\n\r\nx")
        services.db.add_message_index(acc_id, "INBOX", 1000, uid, f"<{uid}@x>", 10,
                                      "2026-09-01T00:00:00+00:00", "", rel, "sha")

    class _Ctx:
        db = services.db

        def __init__(self):
            self.services = services

        def event(self, *_a):
            pass

        def progress(self, *_a, **_k):
            pass

        def is_cancelled(self):
            return False

    import pytest
    with pytest.raises(ValidationError, match="больше половины"):
        _rebuild_missing(_Ctx(), acc)
    assert services.db.count_messages(acc_id) == 30        # индекс цел

    shutil.rmtree(acc_dir)
    with pytest.raises(ValidationError, match="не найден"):
        _rebuild_missing(_Ctx(), acc)
    assert services.db.count_messages(acc_id) == 30


def test_rebuild_full_wipes_index_and_files(services):
    """«С нуля» удаляет и записи индекса, и файлы писем ящика."""
    from mailarchiver.models import Account

    acc_id = services.db.create_account(Account(name="Я2", host="h", username="u", password="p"))
    rel, _sha, _size = services.store.store_message(
        acc_id, "INBOX", "/", 1, "From: a@b\r\n\r\nтело".encode("utf-8"))
    services.db.add_message_index(acc_id, "INBOX", 1000, 1, "<1@x>", 10,
                                  "2026-09-01T00:00:00+00:00", "", rel, "sha")
    assert os.path.isfile(os.path.join(services.store.account_dir(acc_id), rel))

    removed = services.db.purge_account_index(acc_id)
    files, freed = services.store.delete_account_files(acc_id)
    assert removed == 1 and files == 1 and freed > 0
    assert services.db.count_messages(acc_id) == 0
    assert not os.path.isfile(os.path.join(services.store.account_dir(acc_id), rel))
    # каталог ящика воссоздан пустым — следующая копия пишет туда же
    assert os.path.isdir(services.store.account_dir(acc_id))


# ---------------------------------------------------------------------------
#  Разбор заголовков писем (экспорт)
# ---------------------------------------------------------------------------
def test_decode_mime_header_handles_8bit_and_rfc2047():
    """8-битная кириллица без RFC2047 раньше превращалась в «????» в .pst и .eml."""
    import email as _email
    from mailarchiver.util import decode_mime_header

    cases = {
        "cp1251 без кодирования": ("Тестовая тема".encode("cp1251"), "Тестовая тема"),
        "utf-8 без кодирования": ("Отчёт за сентябрь".encode("utf-8"), "Отчёт за сентябрь"),
        "RFC2047 utf-8": (b"=?utf-8?B?0J/RgNC40LLQtdGC?=", "Привет"),
        "RFC2047 windows-1251": (b"=?windows-1251?Q?=D2=E5=EC=E0?=", "Тема"),
        "ascii": (b"Hello there", "Hello there"),
    }
    for name, (raw_subject, expected) in cases.items():
        msg = _email.message_from_bytes(b"Subject: " + raw_subject + b"\r\n\r\n")
        assert decode_mime_header(msg.get("Subject")) == expected, name
    assert decode_mime_header("") == ""
    assert decode_mime_header(None) == ""


def test_export_engines_use_the_same_decoder():
    from mailarchiver.export import eml_engine, pst_native

    raw = (b"Subject: " + "Тестовая тема".encode("cp1251") + b"\r\nFrom: "
           + "Иванов".encode("cp1251") + b" <i@x.ru>\r\n\r\n" + "тело".encode("cp1251"))
    import email as _email
    msg = _email.message_from_bytes(raw)
    assert pst_native._decode_hdr(msg.get("Subject")) == "Тестовая тема"
    assert pst_native._decode_hdr(msg.get("From")) == "Иванов <i@x.ru>"
    assert eml_engine.extract_subject(raw) == "Тестовая тема"


def test_native_pst_reports_ansi_limit():
    """Предел 2 ГБ — понятная ошибка, а не struct.error через час работы."""
    from mailarchiver.errors import PstEngineError
    from mailarchiver.export.pst_native import ANSI_PST_LIMIT_BYTES, PstWriter

    import tempfile

    assert ANSI_PST_LIMIT_BYTES < 2 * 1024 ** 3
    with tempfile.TemporaryDirectory() as tmp:
        writer = PstWriter()
        writer.open(os.path.join(tmp, "big.pst"))
        try:
            # подменяем сборку блока: «узел» сразу переполняет файл
            writer._block_bytes = lambda data, bid, ib: b"x" * (ANSI_PST_LIMIT_BYTES + 1)
            with pytest.raises(PstEngineError) as err:
                writer.add_node(1, 0, b"x" * 64)
            assert "ANSI" in str(err.value)
        finally:
            writer.close()


# ---------------------------------------------------------------------------
#  Карантин прежней копии ящика
# ---------------------------------------------------------------------------
def test_quarantine_keeps_files_and_recreates_dir(services):
    from mailarchiver.models import Account

    acc_id = services.db.create_account(Account(name="Я", host="h", username="u", password="p"))
    rel, _sha, _size = services.store.store_message(acc_id, "INBOX", "/", 1,
                                                    "From: a@b\r\n\r\nтело".encode("utf-8"))
    quarantine, files, freed = services.store.quarantine_account_files(acc_id)

    assert files == 1 and freed > 0
    assert os.path.isdir(quarantine) and os.path.isfile(os.path.join(quarantine, rel))
    assert os.path.isdir(services.store.account_dir(acc_id))       # каталог создан заново
    assert not os.path.exists(os.path.join(services.store.account_dir(acc_id), rel))

    listed = services.store.list_quarantines(acc_id)
    assert [p for p, _f, _b in listed] == [quarantine]


def test_quarantine_name_never_collides(services):
    """Два пересоздания в одну секунду раньше падали с «Directory not empty»."""
    from mailarchiver.models import Account

    acc_id = services.db.create_account(Account(name="Я2", host="h", username="u", password="p"))
    paths = []
    for _ in range(3):
        services.store.store_message(acc_id, "INBOX", "/", 1, b"From: a@b\r\n\r\nx")
        path, files, _bytes = services.store.quarantine_account_files(acc_id)
        assert files == 1
        paths.append(path)
    assert len(set(paths)) == 3
    assert len(services.store.list_quarantines(acc_id)) == 3


def test_drop_quarantine_refuses_foreign_paths(services, tmp_path):
    from mailarchiver.errors import StorageError
    from mailarchiver.models import Account

    acc_id = services.db.create_account(Account(name="Я3", host="h", username="u", password="p"))
    services.store.store_message(acc_id, "INBOX", "/", 1, b"From: a@b\r\n\r\nx")
    path, _f, _b = services.store.quarantine_account_files(acc_id)

    outsider = tmp_path / "чужой"
    outsider.mkdir()
    with pytest.raises(StorageError):
        services.store.drop_quarantine(str(outsider))
    assert outsider.exists()

    services.store.drop_quarantine(path)
    assert not os.path.exists(path)


def test_batch_index_normalizes_like_single_insert(services):
    """Через пачку не должны проходить нерезаные строки и has_attach=None."""
    from mailarchiver.models import Account

    acc_id = services.db.create_account(Account(name="Я4", host="h", username="u", password="p"))
    inserted = services.db.add_message_index_batch([
        (acc_id, "INBOX", 1000, 1, "<1@x>", 10, "2026-09-01T00:00:00+00:00", "",
         "cur/1.eml", "sha", "т" * 900, "a" * 600, None),
    ])
    assert inserted == 1
    row = services.db.list_messages(acc_id)[0]
    assert len(row["subject"]) == 500 and len(row["from_addr"]) == 300
    assert row["has_attach"] == 0
    # повторная пачка тех же писем ничего не добавляет
    assert services.db.add_message_index_batch([
        (acc_id, "INBOX", 1000, 1, "<1@x>", 10, "2026-09-01T00:00:00+00:00", "",
         "cur/1.eml", "sha", "тема", "a@b", 1),
    ]) == 0
