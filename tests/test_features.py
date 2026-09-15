"""Тесты новых функций: просмотр писем, ретеншн на ящик, разбор письма."""
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
    assert r.status_code == 200 and r.json()["days"] == 3
    assert client.get(f"/api/accounts/{aid}").json()["retention_days"] == 3
    # автосоздание расписания ежедневной очистки
    sch = client.get("/api/schedules").json()
    assert any(s["job_type"] == "retention" and s["account_id"] == aid for s in sch)


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
