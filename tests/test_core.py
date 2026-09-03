"""Юнит-тесты ядра: конфигурация, безопасность, БД, хранилище."""
import os

import pytest

from mailarchiver import models
from mailarchiver.security import (
    hash_password, verify_password, SecretBox, sign_value, unsign_value, check_password_policy,
)
from mailarchiver.util import human_size, human_duration, safe_filename, sha256_hex


def test_config_defaults(cfg):
    assert cfg.server["port"] == 8493
    assert cfg.export["pst_format"] == "unicode"
    cfg.validate()


def test_password_hashing():
    h = hash_password("Secret123!")
    assert verify_password("Secret123!", h)
    assert not verify_password("wrong", h)
    assert not verify_password("Secret123!", "garbage")


def test_password_policy():
    assert check_password_policy("short", 8) is not None
    assert check_password_policy("longenough", 8) is None
    assert check_password_policy("password", 8) is not None  # слишком простой


def test_secretbox_roundtrip():
    box = SecretBox(b"unit-test-secret-key-0123456789abcdef")
    enc = box.encrypt("my-imap-password")
    assert enc != "my-imap-password"
    assert box.decrypt(enc) == "my-imap-password"
    assert box.decrypt("") == ""


def test_sign_unsign():
    key = b"key123"
    signed = sign_value("token", key)
    assert unsign_value(signed, key) == "token"
    assert unsign_value(signed, b"other") is None
    assert unsign_value("tampered.abc", key) is None


def test_util_formatters():
    assert human_size(1536) == "1.5 КБ"
    assert human_size(500) == "500 Б"
    assert "мин" in human_duration(3725)
    assert safe_filename("Привет / мир?") != ""
    assert sha256_hex(b"abc") == sha256_hex(b"abc")


def test_db_accounts(services):
    db = services.db
    acc = models.Account(name="Тест", host="imap.example.com", port=993,
                         username="u@example.com", password="p@ss", security="ssl")
    aid = db.create_account(acc)
    got = db.get_account(aid)
    assert got.name == "Тест"
    assert got.password == "p@ss"  # расшифровано
    assert db.list_accounts()
    db.set_account_enabled(aid, False)
    assert db.get_account(aid).enabled is False
    db.delete_account(aid)
    assert db.get_account(aid) is None


def test_db_jobs(services):
    db = services.db
    jid = db.enqueue_job(models.JobType.BACKUP, None, {"k": "v"}, priority=3)
    claimed = db.claim_next_job("w1")
    assert claimed["id"] == jid and claimed["status"] == models.JobStatus.RUNNING
    db.update_job_progress(jid, 5, 10, "half")
    j = db.get_job(jid)
    assert j["progress_current"] == 5
    db.finish_job(jid, models.JobStatus.SUCCESS, {"ok": True})
    assert db.get_job(jid)["status"] == models.JobStatus.SUCCESS
    assert db.count_jobs_by_status().get("success") == 1


def test_db_settings(services):
    db = services.db
    db.set_setting("backup.fetch_batch_size", 150)
    assert db.get_setting("backup.fetch_batch_size") == 150
    assert services.rt("backup", "fetch_batch_size") == 150  # override поверх конфига


def test_storage_roundtrip(services):
    store = services.store
    raw = b"From: a@b.com\r\nSubject: T\r\n\r\n\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82"
    rel, digest, size = store.store_message(1, "INBOX", "/", 42, raw, flags=["\\Seen"], internaldate=1700000000)
    assert size == len(raw)
    assert store.read_message(1, rel) == raw
    assert "\\Seen" in store.flags_from_relpath(rel)
    cnt, total = store.account_disk_usage(1)
    assert cnt == 1 and total > 0


def test_storage_compressed(cfg):
    from mailarchiver.storage import MaildirStore
    store = MaildirStore(os.path.join(cfg.data_dir, "mb_gz"), compress=True)
    raw = b"X" * 5000
    rel, digest, size = store.store_message(1, "INBOX", "/", 1, raw)
    assert rel.endswith(".gz")
    assert store.read_message(1, rel) == raw  # прозрачная распаковка
