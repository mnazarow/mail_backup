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


def test_config_file_errors_are_reported(tmp_path, monkeypatch):
    """Опечатки в config.yaml видны: неизвестный параметр — предупреждение,
    неверный тип и часовой пояс — ошибка, отсутствующий файл — ошибка."""
    from mailarchiver.config import load_config
    from mailarchiver.errors import ConfigError
    monkeypatch.setenv("MAILARCHIVER_DATA", str(tmp_path / "d"))
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "нет-такого.yaml"))
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text("backup:\n  max_concurent_jobs: 3\nsecurity:\n  lockout_minutes: '20'\n", encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert any("max_concurent_jobs" in w for w in cfg.warnings)
    assert cfg.security["lockout_minutes"] == 20           # строка «20» приведена к числу
    cfg_path.write_text("backup:\n  retry_attempts: много\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(cfg_path))
    cfg_path.write_text("scheduler:\n  timezone: Mars/Olympus\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(cfg_path))
    # Строка вместо числа — понятная ошибка с именем параметра, а не «invalid literal for int()».
    cfg_path.write_text("backup:\n  max_concurrent_jobs: abc\n", encoding="utf-8")
    with pytest.raises(ConfigError) as err:
        load_config(str(cfg_path))
    assert "backup.max_concurrent_jobs" in str(err.value)
    # Допустимые значения в «другом виде» принимаются: порт в кавычках, формат с заглавной.
    cfg_path.write_text('server:\n  port: "8500"\nexport:\n  pst_format: Unicode\n', encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg.server["port"] == 8500 and cfg.export["pst_format"] == "unicode"


def test_ensure_dir_keeps_existing_permissions(tmp_path):
    import os
    from mailarchiver.util import ensure_dir
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)
    ensure_dir(str(shared), 0o700)
    assert oct(os.stat(shared).st_mode & 0o7777) == "0o1777"
    fresh = tmp_path / "fresh"
    ensure_dir(str(fresh), 0o700)
    assert oct(os.stat(fresh).st_mode & 0o777) == "0o700"
