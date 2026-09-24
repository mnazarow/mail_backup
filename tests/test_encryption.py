# -*- coding: utf-8 -*-
"""Шифрование локальной копии писем (версия 1.3.0)."""
import base64
import os

import pytest

from mailarchiver.errors import StorageError
from mailarchiver.storage import crypto

RAW = ("From: Иван <i@x.ru>\r\nSubject: Тест шифрования\r\nMIME-Version: 1.0\r\n"
       "Content-Type: text/plain; charset=utf-8\r\n\r\nсекретный текст письма\r\n").encode("utf-8")


def _cipher():
    return crypto.StorageCipher(os.urandom(32))


@pytest.mark.parametrize("compress", [False, True])
def test_store_roundtrip_encrypted(tmp_path, compress):
    from mailarchiver.storage import MaildirStore
    store = MaildirStore(str(tmp_path), compress=compress, cipher=_cipher(), encrypt=True)
    rel, sha, size = store.store_message(1, "INBOX", ".", 7, RAW, flags=["\\Seen"])
    assert rel.endswith(".enc") and (".gz.enc" in rel) == compress
    on_disk = open(os.path.join(store.account_dir(1), rel), "rb").read()
    assert "секретный".encode() not in on_disk, "текст письма не должен лежать на диске открыто"
    assert store.read_message(1, rel) == RAW
    with store.open_message(1, rel) as fh:
        assert fh.readline().startswith(b"From:") and fh.read().endswith(b"\r\n")
    assert store.message_size(1, rel) == len(RAW)
    assert store.flags_from_relpath(rel) == ["\\Seen"]


def test_encrypted_file_without_key_gives_clear_error(tmp_path):
    from mailarchiver.storage import MaildirStore
    store = MaildirStore(str(tmp_path), cipher=_cipher(), encrypt=True)
    rel, _sha, _size = store.store_message(1, "INBOX", ".", 1, RAW)
    store.cipher = None
    with pytest.raises(StorageError) as exc:
        store.read_message(1, rel)
    assert "ключ" in exc.value.message.lower()
    store.cipher = _cipher()          # чужой ключ
    with pytest.raises(StorageError) as exc:
        store.read_message(1, rel)
    assert "не подходит" in exc.value.message


def test_streaming_view_of_encrypted_large_message(tmp_path):
    from mailarchiver import mailview
    from mailarchiver.storage import MaildirStore
    blob = os.urandom(400_000)
    raw = (b'Subject: big\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="B"\r\n\r\n'
           b"--B\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
           b'--B\r\nContent-Type: application/octet-stream; name="x.bin"\r\nContent-Transfer-Encoding: base64\r\n\r\n'
           + base64.encodebytes(blob) + b"--B--\r\n")
    store = MaildirStore(str(tmp_path), compress=True, cipher=_cipher(), encrypt=True)
    rel, _sha, size = store.store_message(1, "INBOX", ".", 1, raw)
    opener = lambda: store.open_message(1, rel)  # noqa: E731
    view = mailview.summarize_source(opener, store.message_size(1, rel), max_bytes=1024)
    assert [a["filename"] for a in view["attachments"]] == ["x.bin"] and view["text"].strip() == "hello"
    name, _ctype, chunks, _len = mailview.attachment_from_source(opener, size, 0, max_bytes=1024)
    assert b"".join(chunks) == blob


def _login(client):
    client.post("/api/setup", json={"username": "adm", "password": "СложныйПароль1"})
    client.post("/api/login", json={"username": "adm", "password": "СложныйПароль1"})


def _store_plain_messages(svc, aid, n=5):
    rows = []
    for i in range(1, n + 1):
        rel, sha, size = svc.store.store_message(aid, "INBOX", ".", i, RAW + str(i).encode())
        rows.append((aid, "INBOX", 1, i, f"<{i}@x>", size, "2026-09-01T00:00:00+00:00", "",
                     rel, sha, "t", "a@b", 0))
    svc.db.add_message_index_batch(rows)


def test_enable_encryption_generates_key_and_converts(client):
    _login(client)
    svc = client.app.state.services
    aid = client.post("/api/accounts", json={"name": "Я", "host": "h", "port": 993,
                                             "username": "u", "password": "p"}).json()["id"]
    _store_plain_messages(svc, aid)
    r = client.put("/api/settings", json={"values": {"storage.encrypt": True}})
    assert r.status_code == 200 and "notice" in r.json()
    key_path = os.path.join(svc.cfg.data_dir, "storage.key")
    assert os.path.exists(key_path) and oct(os.stat(key_path).st_mode & 0o777) == "0o600"
    st = client.get("/api/storage/encryption").json()
    assert st["active"] and st["encrypted"] == 0 and st["plain"] == 5 and st["key_inside_data_dir"]
    # зашифровать существующие
    jobs = client.post("/api/storage/convert", json={"mode": "encrypt"}).json()["jobs"]
    _wait_jobs(svc, jobs)
    st = client.get("/api/storage/encryption").json()
    assert st["encrypted"] == 5 and st["plain"] == 0
    for row in svc.db.list_messages(aid):
        assert row["stored_path"].endswith(".enc")
        assert svc.store.read_message(aid, row["stored_path"]).startswith(RAW)
    # в каталоге ящика не осталось открытых копий
    leftovers = [f for _d, _s, fs in os.walk(svc.store.account_dir(aid)) for f in fs if not f.endswith(".enc")]
    assert leftovers == []
    # выключаем и расшифровываем обратно
    assert client.put("/api/settings", json={"values": {"storage.encrypt": False}}).status_code == 200
    jobs = client.post("/api/storage/convert", json={"mode": "decrypt"}).json()["jobs"]
    _wait_jobs(svc, jobs)
    assert client.get("/api/storage/encryption").json()["encrypted"] == 0


def _wait_jobs(svc, job_ids, timeout=20):
    import time
    end = time.time() + timeout
    while time.time() < end:
        states = [svc.db.get_job(j)["status"] for j in job_ids]
        if all(s in ("success", "failed", "partial", "cancelled") for s in states):
            assert all(s == "success" for s in states), states
            return
        time.sleep(0.2)
    raise AssertionError("задания не завершились")


def test_lost_key_is_not_silently_replaced(client):
    _login(client)
    svc = client.app.state.services
    assert client.put("/api/settings", json={"values": {"storage.encrypt": True}}).status_code == 200
    key_path = os.path.join(svc.cfg.data_dir, "storage.key")
    recorded = svc.db.get_meta("storage_key_id")
    os.unlink(key_path)
    state = svc.apply_runtime_settings()
    assert not state["active"] and "не найден" in state["error"]
    assert not os.path.exists(key_path), "новый ключ взамен потерянного создаваться не должен"
    assert svc.db.get_meta("storage_key_id") == recorded
    # подсунули другой ключ — тоже отказ
    crypto.generate_key_file(key_path)
    state = svc.apply_runtime_settings()
    assert not state["active"] and "не тот" in state["error"]


def test_bad_key_path_rejected_and_reverted(client):
    _login(client)
    svc = client.app.state.services
    client.put("/api/settings", json={"values": {"storage.encrypt": True}})
    r = client.put("/api/settings", json={"values": {"storage.encryption_key_file": "/nonexistent/k.key"}})
    assert r.status_code == 400
    assert not svc.rt("storage", "encryption_key_file")
    assert svc.store.encrypt, "шифрование должно продолжать работать со старым ключом"
    r = client.put("/api/settings", json={"values": {"storage.encryption_key_file": "relative/k.key"}})
    assert r.status_code == 400


def test_storage_settings_apply_without_restart(client):
    """Раньше переключатели раздела «Хранилище» сохранялись, но не действовали."""
    _login(client)
    svc = client.app.state.services
    assert svc.store.compress is False
    client.put("/api/settings", json={"values": {"storage.compress": True, "storage.min_free_space_mb": 123}})
    assert svc.store.compress is True and svc.store.min_free_bytes == 123 * 1024 * 1024


def test_cli_storage_key(data_dir, tmp_path, capsys):
    from mailarchiver.__main__ import main
    path = str(tmp_path / "k.key")
    assert main(["storage-key", "--generate", path]) == 0
    assert os.path.exists(path) and "отпечаток" in capsys.readouterr().out
    assert main(["storage-key", "--generate", path]) == 1      # существующий не перезаписываем
    assert main(["storage-key"]) == 0
