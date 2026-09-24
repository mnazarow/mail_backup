"""Поиск по письмам: разбор запроса, извлечение текста, индекс FTS5, права и аудит."""
from datetime import datetime, timedelta, timezone

import pytest

from mailarchiver import models
from mailarchiver.search import (build_match, extract, fts_available, index_pending, reset_index, search,
                                 status, strip_quotes)

PW = "Sw0rdfish!1"


def _msg(subject, body, *, sender="Иванов Иван <ivanov@example.ru>", to="petrov@example.ru", html=False,
         attach_name=None):
    ctype = "text/html" if html else "text/plain"
    if attach_name:
        return (f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
                f'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
                f"--B\r\nContent-Type: {ctype}; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n{body}\r\n"
                f"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
                f"filename*=UTF-8''{attach_name}\r\nContent-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n"
                f"--B--\r\n").encode("utf-8")
    return (f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: {ctype}; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n{body}\r\n").encode("utf-8")


def _add(svc, acc_id, uid, raw, subject, sender="ivanov@example.ru", days_ago=1, attach=0, folder="INBOX"):
    rel, sha, size = svc.store.store_message(acc_id, folder, "/", uid, raw)
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    svc.db.add_message_index_batch([(acc_id, folder, 1, uid, f"<{acc_id}.{uid}@x>", size, when, "", rel, sha,
                                     subject, sender, attach)])


def _accounts(svc):
    a = svc.db.create_account(models.Account(name="Бухгалтерия", host="h", username="buh@x", password="p"))
    b = svc.db.create_account(models.Account(name="Продажи", host="h", username="sales@x", password="p"))
    return a, b


# ---------------------------------------------------------------------------
#  Разбор запроса и текста
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,expected", [
    ("счёт оплата", '"счет" * AND "оплата" *'),
    ('"акт сверки"', '"акт сверки"'),
    ("тема:договор", 'subject : "договор" *'),
    ("от:ivanov@example.ru", 'addrs : "ivanov example ru" *'),
    ("вложение:scan", 'attach : "scan" *'),
    ('" OR 1=1 --', '"or" * AND "1 1" *'),             # операторы FTS5 — просто слова в кавычках
    ("NEAR(a b) ^x", '"near a" * AND "b" * AND "x" *'),
    ("   ", ""),
])
def test_build_match(query, expected):
    assert build_match(query) == expected


def test_strip_quotes_drops_history():
    text = ("Добрый день!\nПрошу оплатить счёт.\n\n> старая цитата\n"
            "От: Петров\nОтправлено: 1 сентября\nТема: Re: счёт\nстарая переписка")
    out = strip_quotes(text)
    assert "Прошу оплатить" in out and "старая" not in out


def test_extract_html_and_attachments():
    raw = _msg("Счёт №12", "<p>Оплатите&nbsp;<b>счёт</b></p><style>.x{}</style>", html=True,
               attach_name="%D0%A1%D1%87%D1%91%D1%82.pdf")
    import io
    fields = extract(lambda: io.BytesIO(raw), with_body=True, body_cap_chars=1000)
    assert "счет" in fields["body"].lower() and ".x{}" not in fields["body"]
    assert fields["attach"] == "Счет.pdf"                 # ё приведена к е
    assert "ivanov@example.ru" in fields["addrs"] and "petrov@example.ru" in fields["addrs"]
    assert fields["subject"] == "Счет №12"


# ---------------------------------------------------------------------------
#  Индекс и поиск
# ---------------------------------------------------------------------------
def test_index_and_search(services):
    svc = services
    assert fts_available(svc.db)
    a, b = _accounts(svc)
    _add(svc, a, 1, _msg("Счёт на оплату", "Прошу оплатить счёт номер 1234-56 до пятницы."), "Счёт на оплату")
    _add(svc, a, 2, _msg("Акт сверки", "Направляю акт сверки взаиморасчётов.", attach_name="akt.pdf"),
         "Акт сверки", attach=1, days_ago=40)
    _add(svc, b, 3, _msg("Коммерческое предложение", "Цены на трубы и фитинги",
                         sender="Сидоров <sidorov@client.ru>"), "Коммерческое предложение", "sidorov@client.ru")
    assert status(svc)["pending"] == 3
    res = index_pending(svc)
    assert res["indexed"] == 3 and res["errors"] == 0 and status(svc)["pending"] == 0

    def ids(query, **kw):
        return [r["id"] for r in search(svc, query, **kw)["results"]]
    assert len(ids("счет")) == 1                           # ё/е и начало слова
    assert len(ids("1234-56")) == 1                        # номер через дефис
    assert len(ids("пятниц")) == 1                         # поиск по тексту
    assert len(ids("вложение:akt")) == 1
    assert len(ids("от:sidorov")) == 1
    assert len(ids("трубы", account_ids=[a])) == 0         # чужой ящик не виден
    assert len(ids("трубы", account_ids=[b])) == 1
    assert len(ids("акт", with_attachments=True)) == 1
    recent = (datetime.now().date() - timedelta(days=10)).isoformat()
    assert len(ids("акт", date_from=recent)) == 0          # письмо 40-дневной давности отсечено
    first = search(svc, "оплатить")["results"][0]
    assert "\x02" in first["snippet"] and first["account_name"] == "Бухгалтерия"


def test_deleted_messages_leave_the_index(services):
    svc = services
    a, b = _accounts(svc)
    _add(svc, a, 1, _msg("Уникальное слово", "абракадабра"), "Уникальное слово")
    _add(svc, b, 2, _msg("Другое", "абракадабра"), "Другое")
    index_pending(svc)
    assert len(search(svc, "абракадабра")["results"]) == 2
    svc.db.execute("DELETE FROM messages WHERE account_id=?", (a,))
    assert len(search(svc, "абракадабра")["results"]) == 1
    svc.db.execute("DELETE FROM accounts WHERE id=?", (b,))      # каскадное удаление ящика
    assert svc.db.scalar("SELECT COUNT(*) FROM mail_fts") == 0


def test_bodies_not_indexed_when_encrypted(services, tmp_path):
    from mailarchiver.storage import crypto
    svc = services
    key = tmp_path / "k"
    crypto.generate_key_file(str(key))
    svc.set_rt("storage", "encryption_key_file", str(key))
    svc.set_rt("storage", "encrypt", True)
    svc.apply_runtime_settings()
    a, _b = _accounts(svc)
    _add(svc, a, 1, _msg("Тема видна", "секретный текст письма"), "Тема видна")
    index_pending(svc)
    assert not search(svc, "секретный")["results"]           # текст в индекс не попал
    assert search(svc, "тема")["results"]                     # заголовки — да
    assert status(svc)["bodies_blocked_by_encryption"]
    svc.set_rt("search", "index_bodies_encrypted", True)
    reset_index(svc.db)
    index_pending(svc)
    assert search(svc, "секретный")["results"]                # явно разрешили — ищется (файл расшифрован)


def test_reindex_generation_guard(services):
    svc = services
    a, _b = _accounts(svc)
    for uid in range(1, 5):
        _add(svc, a, uid, _msg(f"Письмо {uid}", "текст"), f"Письмо {uid}")
    calls = {"n": 0}

    def progress(done, total, text):
        calls["n"] += 1
        if calls["n"] == 1:
            reset_index(svc.db)              # «Перестроить индекс» посреди прохода
    from mailarchiver import search as search_mod
    old = search_mod.BATCH
    search_mod.BATCH = 2
    try:
        index_pending(svc, progress=progress)
    finally:
        search_mod.BATCH = old
    # порция, собранная до перестройки, не записана поверх очищенного индекса
    assert status(svc)["pending"] == 4
    index_pending(svc)
    assert status(svc)["pending"] == 0 and len(search(svc, "письмо")["results"]) == 4


def test_fallback_without_fts(services):
    svc = services
    a, _b = _accounts(svc)
    _add(svc, a, 1, _msg("Договор поставки", "текст"), "Договор поставки", "ivanov@example.ru")
    svc.db.execute("DROP TRIGGER messages_fts_ad")
    svc.db.execute("DROP TABLE mail_fts")
    res = search(svc, "договор")
    assert not res["fts"] and len(res["results"]) == 1
    assert index_pending(svc)["available"] is False


# ---------------------------------------------------------------------------
#  Веб
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": PW})
    client.post("/api/login", json={"username": "admin", "password": PW})


def test_search_api_scope_and_audit(client):
    _login(client)
    svc = client.app.state.services
    a, b = _accounts(svc)
    _add(svc, a, 1, _msg("Счёт", "оплата счёта"), "Счёт")
    _add(svc, b, 2, _msg("Счёт", "оплата счёта"), "Счёт")
    index_pending(svc)
    all_ = client.get("/api/search", params={"q": "оплата", "scope": "all"}).json()
    assert len(all_["results"]) == 2
    one = client.get("/api/search", params={"q": "оплата", "account_id": a}).json()
    assert [r["account_id"] for r in one["results"]] == [a]
    assert client.get("/api/search", params={"q": "оплата"}).status_code == 400    # ящик не указан
    audit = [r for r in svc.db.list_audit() if r["action"] == "search"]
    assert any("все ящики" in r["detail"] for r in audit) and any(f"ящик #{a}" in r["detail"] for r in audit)
    st = client.get("/api/search/status").json()
    assert st["available"] and st["total"] == 2
    job = client.post("/api/search/reindex").json()
    assert job["job_id"]


def test_mailbox_user_searches_only_own_mailbox(client):
    _login(client)
    svc = client.app.state.services
    a, b = _accounts(svc)
    _add(svc, a, 1, _msg("Своё", "уникальный текст"), "Своё")
    _add(svc, b, 2, _msg("Чужое", "уникальный текст"), "Чужое")
    index_pending(svc)
    from mailarchiver.security import sign_value
    svc.db.create_session("mbx-token", 0, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                          role="mailbox", account_id=a)
    cookie = sign_value("mbx-token", svc.cfg.secret_key())
    client.cookies.clear()
    client.cookies.set("ma_session", cookie)
    res = client.get("/api/search", params={"q": "уникальный", "scope": "all", "account_id": b}).json()
    assert [r["account_id"] for r in res["results"]] == [a]
    st = client.get("/api/search/status").json()
    assert st["total"] == 0                                  # общих счётчиков архива сотруднику не видно
