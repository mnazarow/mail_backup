# -*- coding: utf-8 -*-
"""Потоковый просмотр и скачивание писем (версия 1.3.0).

Крупные письма разбираются построчно, без загрузки в память. Главное
требование — номера и содержимое вложений совпадают с обычным разбором
модулем email, иначе по ссылке «скачать вложение №2» пришёл бы чужой файл.
"""
import base64
import gzip
import io
import os

import pytest

from mailarchiver import mailview, mimestream


def _both(raw):
    small = mailview.summarize_source(lambda: io.BytesIO(raw), len(raw), max_bytes=0)
    large = mailview.summarize_source(lambda: io.BytesIO(raw), len(raw), max_bytes=1)
    return small, large


def _att_bytes(raw, idx, max_bytes):
    att = mailview.attachment_from_source(lambda: io.BytesIO(raw), len(raw), idx, max_bytes=max_bytes)
    return None if att is None else (att[0], att[1], b"".join(att[2]))


def _mixed(parts, boundary="B"):
    out = [b"Subject: t\r\nMIME-Version: 1.0\r\n",
           f'Content-Type: multipart/mixed; boundary="{boundary}"\r\n\r\n'.encode()]
    for p in parts:
        out.append(f"--{boundary}\r\n".encode() + p + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out)


BLOB = os.urandom(50_000)
B64 = base64.encodebytes(BLOB).replace(b"\n", b"\r\n")

CASES = {
    "два вложения": _mixed([
        "Content-Type: text/plain; charset=utf-8\r\n\r\nтекст письма".encode(),
        b'Content-Type: application/pdf; name="a.pdf"\r\nContent-Disposition: attachment; filename="a.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\n" + B64,
        b'Content-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\n' + B64,
    ]),
    "вложенное письмо": _mixed([
        b"Content-Type: text/plain\r\n\r\nfwd",
        b'Content-Type: message/rfc822\r\nContent-Disposition: attachment; filename="f.eml"\r\n\r\n'
        b'Subject: inner\r\nContent-Type: multipart/mixed; boundary="I"\r\n\r\n'
        b'--I\r\nContent-Type: application/zip; name="in.zip"\r\n\r\nZIP\r\n--I--\r\n',
        b'Content-Type: application/zip; name="after.zip"\r\n\r\nAFTER',
    ]),
    "отчёт о недоставке": (
        b"Subject: DSN\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="R"\r\n\r\n'
        b"--R\r\nContent-Type: text/plain\r\n\r\nfailed\r\n"
        b"--R\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; x\r\n\r\nStatus: 5.1.1\r\n"
        b"--R\r\nContent-Type: message/rfc822\r\n\r\nSubject: orig\r\n\r\nbody\r\n--R--\r\n"),
    "без закрывающей границы": (
        b'Subject: x\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="N"\r\n\r\n'
        b"--N\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        b"--N\r\nContent-Type: application/octet-stream; name=a.bin\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + B64),
    "письмо = вложение": (
        b'Subject: only\r\nMIME-Version: 1.0\r\nContent-Type: application/pdf; name="s.pdf"\r\n'
        b'Content-Disposition: attachment; filename="s.pdf"\r\nContent-Transfer-Encoding: base64\r\n\r\n' + B64),
}


@pytest.mark.parametrize("name", list(CASES))
def test_stream_and_email_agree(name):
    raw = CASES[name]
    small, large = _both(raw)
    key = lambda r: [(a["index"], a["filename"], a["content_type"]) for a in r["attachments"]]
    assert key(small) == key(large), name
    assert small["text"].strip() == large["text"].strip()
    for idx, _fn, ctype in key(small):
        a1 = _att_bytes(raw, idx, 0)
        a2 = _att_bytes(raw, idx, 1)
        assert a1[:2] == a2[:2]
        if ctype == "message/rfc822":
            assert a1[2].replace(b"\r\n", b"\n").strip() == a2[2].replace(b"\r\n", b"\n").strip()
        else:
            assert a1[2] == a2[2], f"{name}: вложение №{idx} различается"


def test_base64_stream_matches_email_rules():
    dec = mimestream.Base64Stream()
    data = b"SGVsbG8s!IHdv\r\ncmxkIQ"      # мусор и отсутствие паддинга
    out = b"".join(dec.feed(data[i:i + 3]) for i in range(0, len(data), 3)) + dec.close()
    assert out == b"Hello, world!"
    dec = mimestream.Base64Stream()
    assert dec.feed(b"YQ==YQ==") + dec.close() == b"a"      # данные после паддинга отбрасываются


def test_quoted_printable_stream():
    import quopri
    src = "Строка = с «кавычками» и длинным текстом ".encode("utf-8") * 20
    enc = quopri.encodestring(src)
    dec = mimestream.QuotedPrintableStream()
    out = b"".join(dec.feed(enc[i:i + 50]) for i in range(0, len(enc), 50)) + dec.close()
    assert out == src


def test_large_path_uses_little_memory(tmp_path):
    """Вложение из крупного письма отдаётся целиком и без чтения письма в память."""
    blob = os.urandom(3 * 1024 * 1024)
    raw = _mixed([b"Content-Type: text/plain\r\n\r\nhi",
                  b'Content-Type: application/octet-stream; name="big.bin"\r\n'
                  b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(blob)])
    path = tmp_path / "m.eml.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(raw)
    reads = []

    class Spy(io.RawIOBase):
        def __init__(self):
            self.inner = gzip.open(path, "rb")

        def readable(self):
            return True

        def readline(self, size=-1):
            line = self.inner.readline(size)
            reads.append(len(line))
            return line

        def read(self, size=-1):
            data = self.inner.read(size)
            reads.append(len(data))
            return data

        def close(self):
            self.inner.close()
            super().close()

    name, ctype, chunks, length = mailview.attachment_from_source(Spy, len(raw), 0, max_bytes=1024)
    got = b"".join(chunks)
    assert got == blob and name == "big.bin" and length is None
    assert max(reads) <= 64 * 1024, "письмо не должно читаться одним куском"


def _store_message(client, raw):
    svc = client.app.state.services
    aid = client.post("/api/accounts", json={"name": "Я", "host": "h", "port": 993,
                                             "username": "u", "password": "p"}).json()["id"]
    rel, sha, size = svc.store.store_message(aid, "INBOX", ".", 1, raw)
    svc.db.add_message_index_batch([(aid, "INBOX", 1, 1, "<1@x>", size, "2026-09-01T00:00:00+00:00",
                                     "", rel, sha, "t", "a@b", 1)])
    pk = svc.db.list_messages(aid)[0]["id"]
    return aid, pk


def _login(client):
    client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
    client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})


@pytest.mark.parametrize("threshold", [0, 1024])
def test_api_view_and_download(client, monkeypatch, threshold):
    """Через API: список вложений, скачивание вложения и .eml — на обоих путях."""
    if threshold:
        monkeypatch.setattr(mailview, "MAX_PARSE_BYTES", threshold)
    _login(client)
    raw = CASES["два вложения"]
    aid, pk = _store_message(client, raw)
    view = client.get(f"/api/accounts/{aid}/messages/{pk}").json()
    assert [a["filename"] for a in view["attachments"]] == ["a.pdf", "attachment_1.png"]
    assert view["text"].strip() == "текст письма"
    got = client.get(f"/api/accounts/{aid}/messages/{pk}/attachment/0")
    assert got.status_code == 200 and got.content == BLOB
    assert "a.pdf" in got.headers["content-disposition"]
    assert client.get(f"/api/accounts/{aid}/messages/{pk}/attachment/5").status_code == 404
    eml = client.get(f"/api/accounts/{aid}/messages/{pk}/raw")
    assert eml.status_code == 200 and eml.content == raw


def test_raw_download_error_is_reported_before_headers(client):
    """Нет ключа шифрования: /raw отвечает понятной ошибкой, а не «200 и обрыв»."""
    from mailarchiver.storage import crypto
    _login(client)
    svc = client.app.state.services
    svc.store.cipher = crypto.StorageCipher(os.urandom(32))
    svc.store.encrypt = True
    try:
        aid, pk = _store_message(client, CASES["два вложения"])
        svc.store.cipher = None
        got = client.get(f"/api/accounts/{aid}/messages/{pk}/raw")
        assert got.status_code == 400
        assert "ключ" in got.json()["message"].lower()
    finally:
        svc.store.cipher = None
        svc.store.encrypt = False


def test_open_source_stream_closes_file():
    closed = []

    class Fh(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    chunks = mailview.open_source_stream(lambda: Fh(b"x" * 100), chunk=30)
    assert b"".join(chunks) == b"x" * 100 and closed

    class Broken(io.BytesIO):
        def read(self, *_a):
            raise OSError("диск")

        def close(self):
            closed.append("broken")
            super().close()

    with pytest.raises(OSError):
        mailview.open_source_stream(lambda: Broken(b""))
    assert closed[-1] == "broken"


def test_active_content_types_are_not_served_inline(client):
    """HTML/SVG из вложения не должны отдаваться с «живым» типом."""
    _login(client)
    raw = _mixed([b"Content-Type: text/plain\r\n\r\nx",
                  b'Content-Type: image/svg+xml; name="p.svg"\r\nContent-Disposition: attachment; filename="p.svg"\r\n\r\n'
                  b"<svg onload=alert(1)></svg>"])
    aid, pk = _store_message(client, raw)
    got = client.get(f"/api/accounts/{aid}/messages/{pk}/attachment/0")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("application/octet-stream")
    assert got.headers["content-disposition"].startswith("attachment")


def test_stored_path_traversal_rejected(services):
    from mailarchiver.errors import StorageError
    with pytest.raises(StorageError):
        services.store.message_path(1, "../../../etc/passwd")
