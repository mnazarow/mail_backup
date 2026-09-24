"""Копия архива вне сервера: подпись S3, сетевая папка, S3, rsync, снимки базы, восстановление."""
import os
import shutil
import sqlite3

import pytest

from mailarchiver.errors import ReplicaError
from mailarchiver.replica import snapshots
from mailarchiver.replica.runner import ReplicaRunner, build_target, check_target, prepare_target, pull, status
from mailarchiver.replica.s3 import EMPTY_SHA256, S3Client, S3Config, sign_request, uri_encode
from mailarchiver.replica.targets import MARKER_NAME, README_NAME, escape_component, unescape_component

import fake_s3

AK, SK, D = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "20130524T000000Z"
HOST = "examplebucket.s3.amazonaws.com"


# ---------------------------------------------------------------------------
#  Подпись: эталонные примеры из документации AWS
# ---------------------------------------------------------------------------
def _sig(auth: str) -> str:
    return auth.rsplit("Signature=", 1)[1]


def test_sigv4_get_object_vector():
    auth = sign_request("GET", "/test.txt", {}, {"Host": HOST, "Range": "bytes=0-9",
                                                  "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": D},
                        EMPTY_SHA256, access_key=AK, secret_key=SK, region="us-east-1", amz_date=D)
    assert _sig(auth) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth


def test_sigv4_put_object_vector():
    payload = "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072"
    auth = sign_request("PUT", "/" + uri_encode("test$file.text", encode_slash=False), {},
                        {"Date": "Fri, 24 May 2013 00:00:00 GMT", "Host": HOST, "x-amz-date": D,
                         "x-amz-storage-class": "REDUCED_REDUNDANCY", "x-amz-content-sha256": payload},
                        payload, access_key=AK, secret_key=SK, region="us-east-1", amz_date=D)
    assert _sig(auth) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


@pytest.mark.parametrize("query,expected", [
    ({"lifecycle": ""}, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
    ({"max-keys": "2", "prefix": "J"}, "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"),
])
def test_sigv4_bucket_vectors(query, expected):
    auth = sign_request("GET", "/", query, {"Host": HOST, "x-amz-date": D, "x-amz-content-sha256": EMPTY_SHA256},
                        EMPTY_SHA256, access_key=AK, secret_key=SK, region="us-east-1", amz_date=D)
    assert _sig(auth) == expected


@pytest.mark.parametrize("name", ["1695000000.M12Q1P3.ab.mailarchiver.host:2,S", "100%.eml", "точка.",
                                  'a<b>c:"d"|e?f*g\\h', "plain-name"])
def test_dir_names_are_escaped_reversibly(name):
    escaped = escape_component(name)
    assert not set(escaped) & set('<>:"\\|?*')
    assert not escaped.endswith((".", " "))
    assert unescape_component(escaped) == name


# ---------------------------------------------------------------------------
#  Подготовка архива
# ---------------------------------------------------------------------------
MSG = (b"From: a@example.com\r\nTo: b@example.com\r\nSubject: test\r\n"
       b"Message-ID: <%d@x>\r\n\r\nbody %d\r\n")


def _fill(svc, account_id: int, count: int, folder: str = "INBOX", start: int = 1):
    paths = []
    for uid in range(start, start + count):
        rel, _sha, _size = svc.store.store_message(account_id, folder, "/", uid, MSG % (uid, uid), flags=["\\Seen"])
        paths.append(rel)
    return paths


def _use_dir(svc, path: str, **extra):
    svc.set_rt("replica", "target", "dir")
    svc.set_rt("replica", "dir_path", path)
    for key, value in extra.items():
        svc.set_rt("replica", key, value)


def _target_files(root: str):
    out = set()
    for dirpath, _d, files in os.walk(root):
        for fn in files:
            out.add(os.path.relpath(os.path.join(dirpath, fn), root))
    return out


# ---------------------------------------------------------------------------
#  Сетевая папка
# ---------------------------------------------------------------------------
def test_dir_replica_full_cycle(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest))
    _fill(svc, 1, 5)
    _fill(svc, 2, 3, folder="Отправленные")
    res = ReplicaRunner(svc).run()
    assert res["status"] == "success"
    assert res["files_up"] >= 8 and res["snapshot"]
    files = _target_files(str(dest))
    assert MARKER_NAME in files and README_NAME in files
    mails = [f for f in files if f.startswith("mailboxes")]
    assert len(mails) == 8
    assert all(":" not in f for f in mails) and any("%3A2,S" in f for f in mails)
    assert any("Отправленные" in f for f in mails)
    assert any(f.startswith("db" + os.sep + "mailarchiver-") for f in files)
    # второй прогон — отправлять нечего
    again = ReplicaRunner(svc).run()
    assert again["files_up"] == 0 and again["status"] == "success"
    # новое письмо и удалённое локально (удаляем одно из уже отправленных)
    victim = next(p for p in svc.store.iter_messages(1))[1]
    _fill(svc, 1, 1, start=100)
    os.unlink(svc.store.message_path(1, victim))
    third = ReplicaRunner(svc).run()
    assert third["files_up"] == 1 and third["files_deleted"] == 1
    assert len([f for f in _target_files(str(dest)) if f.startswith("mailboxes")]) == 8


def test_dir_replica_restores_names_on_pull(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest))
    originals = {rel for rel in _fill(svc, 3, 4)}
    ReplicaRunner(svc).run()
    out = tmp_path / "restored"
    target = build_target(svc)
    res = pull(target, str(out))
    assert res["files"] >= 4
    restored = {os.path.relpath(os.path.join(dp, fn), out / "mailboxes" / "account_3")
                for dp, _d, fns in os.walk(out / "mailboxes" / "account_3") for fn in fns}
    assert restored == originals                    # «:» в именах вернулись на место
    assert any(n.startswith("mailarchiver-") for n in os.listdir(out / "snapshots"))


def test_dir_replica_refuses_unmounted_or_foreign_place(services, tmp_path):
    svc = services
    _fill(svc, 1, 2)
    _use_dir(svc, str(tmp_path / "нет-такой-папки"))
    with pytest.raises(ReplicaError, match="не найдена"):
        ReplicaRunner(svc).run()
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "чужой-файл.txt").write_text("x")
    _use_dir(svc, str(busy))
    with pytest.raises(ReplicaError, match="метки MailArchiver нет"):
        ReplicaRunner(svc).run()
    assert prepare_target(svc)["action"] == "not_empty"
    assert prepare_target(svc, force=True)["action"] == "created"
    assert ReplicaRunner(svc).run()["status"] == "success"
    assert (busy / "чужой-файл.txt").exists()        # чужое не трогаем


def test_dir_replica_stops_when_its_marker_vanishes(services, tmp_path):
    """Сетевой диск отключился: точка монтирования пуста. Копия не должна начаться туда заново."""
    svc = services
    mount = tmp_path / "mnt-backup"
    mount.mkdir()
    _use_dir(svc, str(mount))
    _fill(svc, 1, 3)
    assert ReplicaRunner(svc).run()["status"] == "success"
    shutil.rmtree(mount)
    mount.mkdir()                                       # «отмонтировали» — осталась пустая папка
    with pytest.raises(ReplicaError, match="пропала"):
        ReplicaRunner(svc).run()
    assert os.listdir(mount) == []                      # на системный диск ничего не записано
    chk = check_target(svc)
    assert chk["marker"] == "lost" and not all(c["ok"] for c in chk["checks"])
    # место действительно новое — администратор подтверждает явно
    assert prepare_target(svc)["action"] == "created"
    res = ReplicaRunner(svc).run()
    assert res["status"] == "success" and res["files_up"] >= 3


def test_dir_replica_foreign_marker_and_adoption(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest))
    _fill(svc, 1, 3)
    ReplicaRunner(svc).run()
    # «Другой сервер» (или восстановленная база): метка есть, наш id другой
    svc.db.set_meta("replica_marker_id", "someone-else")
    with pytest.raises(ReplicaError, match="ДРУГОГО архива"):
        ReplicaRunner(svc).run()
    assert prepare_target(svc)["action"] == "adopted"
    res = ReplicaRunner(svc).run()
    assert res["verified"] and res["files_up"] == 0       # сверка узнала уже лежащие файлы


def test_mass_deletion_is_blocked_until_allowed(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest), max_delete_percent=30)
    _fill(svc, 1, 150)
    ReplicaRunner(svc).run()
    shutil.rmtree(svc.store.account_dir(1))          # «диск с почтой отвалился»
    res = ReplicaRunner(svc).run()
    assert res["status"] == "partial" and res["deletions_blocked"] == 150 and res["files_deleted"] == 0
    assert len([f for f in _target_files(str(dest)) if f.startswith("mailboxes")]) == 150
    assert "УДАЛЕНИЕ ПРИОСТАНОВЛЕНО" in res["message"]
    allowed = ReplicaRunner(svc, allow_mass_delete=True).run()
    assert allowed["files_deleted"] == 150
    assert not [f for f in _target_files(str(dest)) if f.startswith("mailboxes")]


def test_verify_finds_files_lost_on_the_replica_side(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest))
    _fill(svc, 1, 4)
    ReplicaRunner(svc).run()
    lost = next(os.path.join(dp, fn) for dp, _d, fns in os.walk(dest / "mailboxes") for fn in fns)
    os.unlink(lost)
    assert ReplicaRunner(svc).run()["files_up"] == 0            # без сверки не видно
    res = ReplicaRunner(svc, force_verify=True).run()
    assert res["files_up"] == 1 and res["verified"]
    assert os.path.exists(lost)


def test_kept_remote_when_mirroring_is_off(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest), mirror_deletions=False)
    rels = _fill(svc, 1, 3)
    ReplicaRunner(svc).run()
    os.unlink(svc.store.message_path(1, rels[0]))
    res = ReplicaRunner(svc).run()
    assert res["kept_remote"] == 1 and res["files_deleted"] == 0
    assert len([f for f in _target_files(str(dest)) if f.startswith("mailboxes")]) == 3


def test_quarantine_dirs_are_not_replicated(services, tmp_path):
    svc = services
    dest = tmp_path / "replica"
    dest.mkdir()
    _use_dir(svc, str(dest))
    _fill(svc, 1, 2)
    svc.store.quarantine_account_files(1)
    _fill(svc, 1, 1, start=50)
    ReplicaRunner(svc).run()
    names = _target_files(str(dest))
    assert not any("_old_" in n for n in names)
    assert len([n for n in names if n.startswith("mailboxes")]) == 1


# ---------------------------------------------------------------------------
#  S3
# ---------------------------------------------------------------------------
@pytest.fixture()
def s3():
    server = fake_s3.FakeS3()
    yield server
    server.close()


def _use_s3(svc, server, prefix="archive-copy/"):
    for key, value in {"target": "s3", "s3_endpoint": server.endpoint, "s3_bucket": fake_s3.BUCKET,
                       "s3_access_key": fake_s3.ACCESS, "s3_secret_key": fake_s3.SECRET,
                       "s3_region": fake_s3.REGION, "s3_prefix": prefix, "s3_path_style": True}.items():
        svc.set_rt("replica", key, value)


def test_s3_replica_cycle_with_cyrillic_and_pagination(services, s3):
    svc = services
    s3.page_size = 3                                    # постраничная выдача списков
    _use_s3(svc, s3)
    _fill(svc, 1, 4, folder="Входящие/Счета & акты")
    _fill(svc, 2, 2)
    res = ReplicaRunner(svc).run()
    assert res["status"] == "success", res
    keys = set(s3.objects)
    assert "archive-copy/.mailarchiver-replica" in keys and "archive-copy/README-MailArchiver.txt" in keys
    mails = [k for k in keys if k.startswith("archive-copy/mailboxes/")]
    assert len(mails) == 6 and any("Счета & акты" in k for k in mails) and any(":2,S" in k for k in mails)
    # все подписи и суммы сошлись (404 — ожидаемый ответ на чтение ещё не записанной метки)
    assert not [e for e in s3.errors if e[0] != 404]
    # удаление и полная сверка с постраничным списком
    victim = next(p for p in svc.store.iter_messages(2))[1]
    os.unlink(svc.store.message_path(2, victim))
    res = ReplicaRunner(svc, force_verify=True).run()
    assert res["files_deleted"] == 1 and res["verified"] and res["files_up"] == 0
    assert len([k for k in s3.objects if k.startswith("archive-copy/mailboxes/")]) == 5
    assert status(svc)["last_run"]["status"] == "success"


def test_s3_multipart_and_retry(services, s3, monkeypatch):
    from mailarchiver.replica import s3 as s3mod
    monkeypatch.setattr(s3mod, "PART_SIZE", 64 * 1024)
    monkeypatch.setattr(s3mod.time, "sleep", lambda _s: None)
    svc = services
    _use_s3(svc, s3)
    big = b"From: a@b\r\nSubject: big\r\n\r\n" + os.urandom(200 * 1024)
    svc.store.store_message(1, "INBOX", "/", 1, big)
    s3.fail_next = 2                                     # два ответа 503 — клиент повторяет
    res = ReplicaRunner(svc).run()
    assert res["status"] == "success"
    stored = [v for k, v in s3.objects.items() if k.startswith("archive-copy/mailboxes/")]
    assert stored == [big]
    assert any("uploads" in path for _m, path in s3.requests)


def test_s3_wrong_secret_is_explained(services, s3):
    svc = services
    _use_s3(svc, s3)
    svc.set_rt("replica", "s3_secret_key", "wrong")
    result = check_target(svc)
    assert not result["ok"] and "отказало в доступе" in (result["error"] or "")


def test_s3_client_list_prefixes(s3):
    client = S3Client(S3Config(endpoint=s3.endpoint, bucket=fake_s3.BUCKET, access_key=fake_s3.ACCESS,
                               secret_key=fake_s3.SECRET, region=fake_s3.REGION))
    for key in ("p/mailboxes/account_1/a", "p/mailboxes/account_1/b", "p/mailboxes/account_22/c", "p/db/x"):
        client.put_bytes(key, b"1")
    assert sorted(client.list_prefixes("p/mailboxes/")) == ["p/mailboxes/account_1/", "p/mailboxes/account_22/"]
    assert client.head("p/db/x") == 1 and client.head("p/db/none") is None
    assert client.delete_many(["p/db/x", "p/mailboxes/account_1/a"]) == []
    assert sorted(k for k, _s in client.list("p/")) == ["p/mailboxes/account_1/b", "p/mailboxes/account_22/c"]


# ---------------------------------------------------------------------------
#  rsync (если установлен)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync не установлен")
def test_rsync_replica_to_local_path(services, tmp_path):
    svc = services
    svc.replica_allow_local_rsync = True
    dest = tmp_path / "rsync-dest"
    svc.set_rt("replica", "target", "rsync")
    svc.set_rt("replica", "rsync_dest", str(dest))
    rels = _fill(svc, 1, 3)
    res = ReplicaRunner(svc).run()
    assert res["status"] == "success", res
    assert (dest / MARKER_NAME).exists() and (dest / README_NAME).exists()
    copied = _target_files(str(dest / "mailboxes"))
    assert len(copied) == 3 and all(":" in c for c in copied)
    assert os.listdir(dest / "db")
    os.unlink(svc.store.message_path(1, rels[0]))
    res = ReplicaRunner(svc).run()
    assert res["files_deleted"] == 1
    assert len(_target_files(str(dest / "mailboxes"))) == 2


# ---------------------------------------------------------------------------
#  Снимки базы
# ---------------------------------------------------------------------------
def test_snapshot_roundtrip(services, tmp_path):
    svc = services
    from mailarchiver import models
    svc.db.create_account(models.Account(name="Снимок", host="h", username="u@x", password="p"))
    info = snapshots.make_snapshot(svc, keep=2)
    assert info["name"].endswith(".db.gz") and not info["encrypted"]
    dest = tmp_path / "restored.db"
    snapshots.restore_snapshot(info["path"], str(dest))
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT name FROM accounts").fetchone()[0] == "Снимок"
    conn.close()
    for _ in range(3):
        snapshots.make_snapshot(svc, keep=2)
    assert len(snapshots.list_snapshots(svc.cfg)) == 2


def test_encrypted_snapshot_needs_the_key(services, tmp_path):
    from mailarchiver.errors import StorageError
    from mailarchiver.storage import crypto
    svc = services
    key_path = tmp_path / "storage.key"
    crypto.generate_key_file(str(key_path))
    svc.set_rt("storage", "encryption_key_file", str(key_path))
    svc.set_rt("storage", "encrypt", True)
    svc.apply_runtime_settings()
    info = snapshots.make_snapshot(svc)
    assert info["encrypted"] and info["name"].endswith(".db.gz.enc")
    with open(info["path"], "rb") as fh:
        assert fh.read(6) == b"MAENC2"
    with pytest.raises(StorageError, match="ключ"):
        snapshots.restore_snapshot(info["path"], str(tmp_path / "x.db"))
    cipher = crypto.StorageCipher(crypto.load_key_file(str(key_path)))
    snapshots.restore_snapshot(info["path"], str(tmp_path / "x.db"), cipher)
    assert sqlite3.connect(tmp_path / "x.db").execute("SELECT COUNT(*) FROM meta").fetchone()[0] >= 1


def test_cli_restore_snapshot(services, tmp_path, capsys):
    from mailarchiver.__main__ import main
    svc = services
    info = snapshots.make_snapshot(svc)
    assert main(["restore-snapshot", info["name"]]) == 1          # база есть — без --force нельзя
    assert main(["restore-snapshot", info["name"], "--force"]) == 0
    out = capsys.readouterr().out
    assert "База восстановлена" in out
    assert any(n.startswith("mailarchiver.db.before-restore-") for n in os.listdir(svc.cfg.data_dir))


# ---------------------------------------------------------------------------
#  Веб-интерфейс
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!1"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!1"})


def test_replica_api(client, tmp_path):
    import time
    _login(client)
    dest = tmp_path / "copy"
    dest.mkdir()
    r = client.put("/api/settings", json={"values": {"replica.target": "dir", "replica.dir_path": str(dest),
                                                    "replica.enabled": True}})
    assert r.status_code == 200, r.text
    bad = client.put("/api/settings", json={"values": {"replica.dir_path": "relative/path"}})
    assert bad.status_code == 400
    check = client.post("/api/replica/check").json()
    assert check["ok"] and check["marker"] == "none_empty"
    assert client.post("/api/replica/prepare", json={}).json()["action"] == "created"
    job_id = client.post("/api/replica/run", json={}).json()["job_id"]
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert job["status"] == "success", job
    st = client.get("/api/replica").json()
    assert st["last_run"]["status"] == "success" and st["snapshots"]
    assert st["next_run"]
    # секрет S3 наружу не отдаётся
    client.put("/api/settings", json={"values": {"replica.s3_secret_key": "top-secret"}})
    values = client.get("/api/settings").json()
    assert values["values"]["replica"]["s3_secret_key"] == ""
    assert values["secrets_set"]["replica.s3_secret_key"] is True
