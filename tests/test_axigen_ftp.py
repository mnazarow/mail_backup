"""Копирование ящиков через службу FTP Backup Axigen — без паролей ящиков.

Учебная служба (tests/axigen_fake.py) отдаёт дерево каталогов из словаря: каталоги — папки ящика, файлы —
письма. Проверяем клиент (списки MLSD/LIST/NLST, UTF-7 имена, TLS) и копирование: первое и последующие,
переход ящика с IMAP без удвоения архива, пакет messages.tar, срок хранения, обрывы связи, нечитаемые папки.
"""
import copy
import time

import pytest

from axigen_fake import MTIME, FakeAxigenFTP, FtpOptions, FtpWorld, make_cert, message, utf7_encode
from mailarchiver import models
from mailarchiver.axigen import store as axstore
from mailarchiver.axigen.common import AxigenAuthError, AxigenCertError, AxigenError, AxigenNotConfigured, \
    AxigenSettings, pretty_fingerprint
from mailarchiver.axigen.ftp import (AxigenFTP, decode_name, encode_name, parse_list_line, utf7_decode,
                                     walk_folders)
from mailarchiver.axigen.ftpbackup import FTP_UIDVALIDITY, FtpBackupEngine, looks_like_message, message_time, \
    probe, verify_folder
from mailarchiver.models import AuthType, JobStatus, JobType

ADMIN_PW = "Adm1nPass"


def tree():
    return {"domains": {"company.ru": {
        "ivanov": {
            "Inbox": {"1.eml": message("Первое"), "2.eml": message("Второе"),
                      "Проекты": {"3.eml": message("Вложенное")}},
            "Sent Items": {"4.eml": message("Отправленное")},
            "Calendar": {"e.ics": b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"},
        },
        "petrov": {"Inbox": {}},
    }}}


def ftp_settings(port, **kw):
    data = dict(enabled=True, host="127.0.0.1", ftp_port=port, admin_user="admin", admin_password=ADMIN_PW,
                timeout_s=5)
    data.update(kw)
    return AxigenSettings(**data)


@pytest.fixture()
def fworld():
    return FtpWorld(tree())


@pytest.fixture()
def fsrv(fworld):
    with FakeAxigenFTP(fworld) as server:
        yield server


def configure(svc, port, **extra):
    values = {"axigen.enabled": True, "axigen.host": "127.0.0.1", "axigen.ftp_port": port,
              "axigen.admin_user": "admin", "axigen.admin_password": ADMIN_PW, "axigen.timeout_s": 5,
              "axigen.imap_host": "mx.company.ru"}
    values.update(extra)
    svc.db.set_settings_many(values)
    svc.apply_runtime_settings()


def mailbox(svc, username="ivanov@company.ru", **kw):
    data = dict(name="Иванов", host="mx.company.ru", username=username, password="", auth_type=AuthType.AXIGEN_FTP)
    data.update(kw)
    return svc.db.get_account(svc.db.create_account(models.Account(**data)))


def engine(svc, st, **kw):
    return FtpBackupEngine(svc.db, svc.store, st, **kw)


def subjects(svc, account_id, folder=None):
    sql = "SELECT subject FROM messages WHERE account_id=?" + (" AND folder=?" if folder else "")
    args = (account_id, folder) if folder else (account_id,)
    return sorted(r["subject"] for r in svc.db.query(sql, args))


# ---------------------------------------------------------------------------
#  Имена и списки
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["Möm", "Отправленные", "a&b", "Входящие/Проекты 2024", "plain"])
def test_utf7_roundtrip(text):
    assert utf7_decode(utf7_encode(text)) == text


def test_decode_name_modes():
    assert decode_name("M&APY-m") == "Möm"
    assert decode_name("M&APY-m", "utf8") == "M&APY-m"
    wire = encode_name("Отправленные")
    assert decode_name(wire, "utf8") == "Отправленные" and decode_name(wire) == "Отправленные"
    assert utf7_decode("bad&#-") is None


def test_list_lines_unix_and_dos():
    e = parse_list_line("-rw-r--r--   1 axigen axigen       1234 Sep 01 12:00 1.eml")
    assert e.name == "1.eml" and not e.is_dir and e.size == 1234 and e.mtime
    d = parse_list_line("drwxr-xr-x   1 axigen axigen          0 Sep 01 2025 Sent Items")
    assert d.is_dir and d.name == "Sent Items"
    dos = parse_list_line("09-01-26  12:00PM       <DIR>          Inbox")
    assert dos.is_dir and dos.name == "Inbox"
    f = parse_list_line("09-01-26  12:00PM               42 a.eml")
    assert f.size == 42 and not f.is_dir
    assert parse_list_line("total 4") is None and parse_list_line("") is None


@pytest.mark.parametrize("options", [FtpOptions(), FtpOptions(mlsd=False), FtpOptions(mlsd=False, feat=False),
                                     FtpOptions(mlsd=False, list_style="dos"), FtpOptions(mlsd=False, list_style="broken"),
                                     FtpOptions(encoding="utf8")],
                         ids=["mlsd", "list", "list-nofeat", "dos", "nlst", "utf8"])
def test_folders_are_walked_the_same_whatever_the_listing(options):
    world = FtpWorld(tree(), options)
    with FakeAxigenFTP(world) as srv, AxigenFTP.open(ftp_settings(srv.port)) as client:
        root = client.find_account_dir("ivanov@company.ru")
        assert client.shown(root) == "/domains/company.ru/ivanov"
        seen = {name: sorted(f.name for f in files)
                for name, _path, files in walk_folders(client, root, excluded=lambda n: n == "Calendar",
                                                       on_error=lambda n, e: pytest.fail(str(e)))}
    assert seen == {"Inbox": ["1.eml", "2.eml"], "Inbox/Проекты": ["3.eml"], "Sent Items": ["4.eml"]}


@pytest.mark.parametrize("layout", ["root", "domains", "accounts"])
def test_account_directory_is_found_in_known_layouts(layout):
    box = {"Inbox": {"1.eml": message("x")}}
    trees = {"root": {"company.ru": {"ivanov": box}},
             "domains": {"domains": {"company.ru": {"ivanov": box}}},
             "accounts": {"domains": {"company.ru": {"accounts": {"ivanov": box}}}}}
    with FakeAxigenFTP(FtpWorld(trees[layout])) as srv, AxigenFTP.open(ftp_settings(srv.port)) as client:
        root = client.find_account_dir("Ivanov@Company.ru")
        assert client.shown(root).endswith("/ivanov")


def test_account_path_template_and_clear_errors(fsrv):
    st = ftp_settings(fsrv.port, ftp_account_path="domains/{domain}/{local}")
    with AxigenFTP.open(st) as client:
        assert client.find_account_dir("petrov@company.ru") == "/domains/company.ru/petrov"
        with pytest.raises(AxigenError) as err:
            client.find_account_dir("nobody@company.ru")
        assert "по шаблону" in err.value.message
    with AxigenFTP.open(ftp_settings(fsrv.port)) as client:
        with pytest.raises(AxigenError) as err:
            client.find_account_dir("nobody@company.ru")
        assert "ivanov/" in err.value.message                  # перечислено, что лежит в каталоге домена
        with pytest.raises(AxigenError):
            client.find_account_dir("x@other.ru")
        with pytest.raises(AxigenError):
            client.find_account_dir("no-domain")


def test_wrong_ftp_password(fsrv):
    with pytest.raises(AxigenAuthError):
        AxigenFTP.open(ftp_settings(fsrv.port, admin_password="bad"))
    # отдельный логин FTP
    with AxigenFTP.open(ftp_settings(fsrv.port, admin_password="cli-only", ftp_password=ADMIN_PW)):
        pass


def test_ftp_needs_encryption_over_the_network(monkeypatch):
    with pytest.raises(AxigenNotConfigured):
        AxigenFTP.open(ftp_settings(21, host="10.1.2.3"))


@pytest.mark.parametrize("mode", ["explicit", "implicit"])
def test_ftps_with_pinned_certificate(tmp_path, mode):
    cert = make_cert(str(tmp_path))
    world = FtpWorld(tree(), FtpOptions(tls=mode))
    with FakeAxigenFTP(world, cert=cert) as srv:
        st = ftp_settings(srv.port, ftp_tls=mode, ftp_cert_sha256=pretty_fingerprint(cert[2]))
        with AxigenFTP.open(st) as client:
            assert client.fingerprint == cert[2]
            root = client.find_account_dir("ivanov@company.ru")
            assert client.retr(root + "/Inbox/1.eml").startswith(b"From:")
        with pytest.raises(AxigenCertError):
            AxigenFTP.open(ftp_settings(srv.port, ftp_tls=mode))
        from mailarchiver.axigen.service import ftp_fingerprint
        assert ftp_fingerprint(ftp_settings(srv.port, ftp_tls=mode)) == cert[2]


def test_message_detection_and_time():
    assert looks_like_message(message("x"))
    assert not looks_like_message(b"BEGIN:VCARD\r\nEND:VCARD")
    assert not looks_like_message(b"\x00\x01binary")
    assert not looks_like_message(b"")
    raw = message("x", received="Tue, 02 Sep 2025 08:00:00 +0000", date="Mon, 01 Sep 2025 10:00:00 +0300")
    assert message_time(raw) == pytest.approx(1756800000)
    assert message_time(message("x", date="Mon, 01 Sep 2025 07:00:00 +0000")) == pytest.approx(1756710000)
    assert message_time(message("x", date="Sun, 01 Jan 2090 00:00:00 +0000")) is None


# ---------------------------------------------------------------------------
#  Копирование
# ---------------------------------------------------------------------------
def test_first_and_incremental_copy(services, fsrv, fworld):
    acc = mailbox(services)
    st = ftp_settings(fsrv.port)
    res = engine(services, st).run(acc)
    assert res.messages_new == 4 and not res.errors and not res.skipped_folders
    assert subjects(services, acc.id, "INBOX") == ["Второе", "Первое"]
    assert subjects(services, acc.id, "INBOX/Проекты") == ["Вложенное"]
    assert subjects(services, acc.id, "Sent Items") == ["Отправленное"]
    assert services.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND uidvalidity<>?",
                               (acc.id, FTP_UIDVALIDITY)) == 0
    assert axstore.ftp_path(services.db, acc.id) == "/domains/company.ru/ivanov"
    # письмо читается из архива целиком
    row = services.db.query_one("SELECT stored_path FROM messages WHERE account_id=? AND subject=?", (acc.id, "Первое"))
    with services.store.open_message(acc.id, row["stored_path"]) as fh:
        assert fh.read() == message("Первое")
    fworld.retrieved.clear()
    res = engine(services, st).run(acc)
    assert res.messages_new == 0 and fworld.retrieved == []           # ничего не качали второй раз
    # новое письмо и удалённое на сервере
    box = fworld.tree["domains"]["company.ru"]["ivanov"]
    box["Inbox"]["5.eml"] = message("Пятое")
    del box["Inbox"]["1.eml"]
    res = engine(services, st).run(acc)
    assert res.messages_new == 1 and fworld.retrieved == ["/domains/company.ru/ivanov/Inbox/5.eml"]
    # удалённое на сервере остаётся в архиве
    assert subjects(services, acc.id, "INBOX") == ["Второе", "Первое", "Пятое"]
    assert "1.eml" not in axstore.ftp_files(services.db, acc.id, "INBOX")


def test_switch_from_imap_does_not_duplicate_the_archive(services, fsrv, fworld):
    acc = mailbox(services, auth_type=AuthType.PASSWORD, password="p")
    raw = message("Первое")
    rel, sha, size = services.store.store_message(acc.id, "INBOX", "/", 7, raw, flags=["\\Seen"])
    services.db.add_message_index(acc.id, "INBOX", 1700000000, 7, "<x@company.ru>", size, "", "\\Seen", rel, sha,
                                  "Первое", "boss@company.ru", 0)
    res = engine(services, ftp_settings(fsrv.port)).run(acc)
    assert res.messages_recognized == 1 and res.messages_new == 3
    assert subjects(services, acc.id, "INBOX").count("Первое") == 1
    known = axstore.ftp_files(services.db, acc.id, "INBOX")["1.eml"]
    assert (known["uidvalidity"], known["uid"]) == (1700000000, 7)


def test_folder_rules_and_service_folders(services, fsrv):
    acc = mailbox(services, folder_exclude=["sent items"])
    res = engine(services, ftp_settings(fsrv.port)).run(acc)
    assert res.messages_new == 3 and "Sent Items" not in services.db.account_folders(acc.id)
    acc2 = mailbox(services, username="ivanov@company.ru", name="Только входящие", folder_include=["INBOX"])
    res = engine(services, ftp_settings(fsrv.port)).run(acc2)
    assert subjects(services, acc2.id) == ["Вложенное", "Второе", "Первое"]     # с вложенной «Проекты»


def test_non_mail_files_are_remembered_and_not_archived(services, fworld, fsrv):
    fworld.tree["domains"]["company.ru"]["ivanov"]["Inbox"]["folder.cfg"] = b"\x00\x01binary settings"
    acc = mailbox(services)
    res = engine(services, ftp_settings(fsrv.port)).run(acc)
    assert res.other_files == 1 and res.messages_new == 4
    fworld.retrieved.clear()
    engine(services, ftp_settings(fsrv.port)).run(acc)
    assert fworld.retrieved == []


def test_only_non_mail_files_is_an_error(services):
    world = FtpWorld({"company.ru": {"ivanov": {"Inbox": {"a.bin": b"\x00\x00", "b.bin": b"\x01"}}}})
    with FakeAxigenFTP(world) as srv:
        res = engine(services, ftp_settings(srv.port)).run(mailbox(services))
    assert res.errors and res.messages_new == 0


def test_size_limit_and_retention(services, fworld, fsrv):
    box = fworld.tree["domains"]["company.ru"]["ivanov"]
    box["Inbox"]["big.eml"] = message("Большое", body="x" * 3 * 1024 * 1024)
    old = message("Старое", date="Mon, 01 Jan 2018 10:00:00 +0000")
    box["Inbox"]["older.eml"] = (message("Древнее", date="Thu, 01 Jan 2015 10:00:00 +0000"), time.time())
    box["Inbox"]["old.eml"] = (old, time.time() - 9 * 365 * 86400)
    acc = mailbox(services)
    res = engine(services, ftp_settings(fsrv.port), skip_larger_than_mb=1, retention_days=3000).run(acc)
    # старше срока — и по времени файла (old.eml), и по дате письма (older.eml: файл свежий)
    assert res.messages_skipped == 1 and res.messages_outside_retention == 2
    assert "Древнее" not in subjects(services, acc.id)
    assert "Большое" not in subjects(services, acc.id) and "Старое" not in subjects(services, acc.id)
    # старое письмо помнится как «вычищенное по сроку» и не качается снова
    fworld.retrieved.clear()
    res = engine(services, ftp_settings(fsrv.port), skip_larger_than_mb=1, retention_days=3000).run(acc)
    assert "/domains/company.ru/ivanov/Inbox/old.eml" not in fworld.retrieved


def test_messages_tar_is_read_and_skipped_while_unchanged(services):
    world = FtpWorld(tree(), FtpOptions(tar=True))
    with FakeAxigenFTP(world) as srv:
        acc = mailbox(services)
        st = ftp_settings(srv.port)
        res = engine(services, st).run(acc)
        assert res.messages_new == 4
        assert subjects(services, acc.id, "INBOX") == ["Второе", "Первое"]
        world.retrieved.clear()
        res = engine(services, st).run(acc)
        assert res.messages_new == 0 and world.retrieved == []
        world.tree["domains"]["company.ru"]["ivanov"]["Inbox"]["9.eml"] = (message("Девятое"), MTIME + 60)
        res = engine(services, st).run(acc)
        assert res.messages_new == 1 and world.retrieved == ["/domains/company.ru/ivanov/Inbox/messages.tar"]
        assert "Девятое" in subjects(services, acc.id, "INBOX")


def test_connection_drop_is_retried(services, fworld, fsrv):
    fworld.drop_on_retr["/domains/company.ru/ivanov/Inbox/2.eml"] = 1
    acc = mailbox(services)
    res = engine(services, ftp_settings(fsrv.port)).run(acc)
    assert res.messages_new == 4 and res.reconnects >= 1 and not res.errors


def test_unreadable_folder_makes_the_copy_incomplete(services, fworld, fsrv):
    fworld.deny["/domains/company.ru/ivanov/Sent Items"] = "Permission denied"
    acc = mailbox(services)
    res = engine(services, ftp_settings(fsrv.port)).run(acc)
    assert res.messages_new == 3
    assert res.skipped_folders == ["Sent Items"]


def test_full_rebuild_downloads_again(services, fworld, fsrv):
    acc = mailbox(services)
    st = ftp_settings(fsrv.port)
    engine(services, st).run(acc)
    services.db.execute("DELETE FROM messages WHERE account_id=?", (acc.id,))     # как «с нуля»: индекс пуст
    fworld.retrieved.clear()
    res = engine(services, st).run(acc)
    assert res.messages_new == 4 and len(fworld.retrieved) == 4
    # номера писем внутри папки не переиспользуются: INBOX был 1–2, стал 3–4
    uids = [r["uid"] for r in services.db.query("SELECT uid FROM messages WHERE account_id=? AND folder='INBOX' "
                                                 "ORDER BY uid", (acc.id,))]
    assert uids == [3, 4]


# ---------------------------------------------------------------------------
#  Задание копирования, проверка подключения, проверка перед очисткой
# ---------------------------------------------------------------------------
def test_backup_job_uses_ftp_backup_for_such_mailboxes(services, fsrv):
    from mailarchiver.queue.jobs import JobContext, handle_backup
    configure(services, fsrv.port)
    acc = mailbox(services)
    job_id = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "tester")
    res = handle_backup(JobContext(services, job_id, JobType.BACKUP, acc.id, {}))
    assert res["final_status"] == JobStatus.SUCCESS and res["messages_new"] == 4
    assert "FTP Backup" in " ".join(r["message"] for r in services.db.list_job_events(job_id))


def test_backup_job_refuses_login_that_is_not_an_address(services, fsrv):
    from mailarchiver.errors import ValidationError
    from mailarchiver.queue.jobs import JobContext, handle_backup
    configure(services, fsrv.port)
    acc = mailbox(services, username="ivanov")
    job_id = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "tester")
    with pytest.raises(ValidationError):
        handle_backup(JobContext(services, job_id, JobType.BACKUP, acc.id, {}))


def test_probe_lists_folders(services, fsrv):
    configure(services, fsrv.port)
    acc = mailbox(services)
    res = probe(services, acc)
    assert res["ok"] and res["path"] == "/domains/company.ru/ivanov"
    assert {f["name"]: f["files"] for f in res["folders"]} == {"Inbox": 2, "Inbox/Проекты": 1, "Sent Items": 1}
    bad = probe(services, mailbox(services, username="nobody@company.ru"))
    assert not bad["ok"] and bad["kind"] == "conn_error" and "nobody" in bad["error"]


def test_verify_folder_before_purge(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    box = fworld.tree["domains"]["company.ru"]["ivanov"]
    ok, why, unseen = verify_folder(services, st, acc, "Inbox")
    assert not ok and unseen                      # копирование ещё не видело этих писем
    engine(services, st).run(acc)
    assert verify_folder(services, st, acc, "Inbox") == (True, "", False)
    # письмо перенесли в папку после копии: файл новый, а письмо старое — очищать нельзя
    box["Inbox"]["late.eml"] = (message("Перенесённое"), time.time() - 400 * 86400)
    ok, why, unseen = verify_folder(services, st, acc, "Inbox")
    assert not ok and "нет 1" in why and unseen
    # и письмо, пришедшее только что, — тоже: по времени файла не понять, старое ли письмо
    del box["Inbox"]["late.eml"]
    box["Inbox"]["fresh.eml"] = (message("Свежее"), time.time())
    ok, _why, unseen = verify_folder(services, st, acc, "Inbox")
    assert not ok and unseen
    engine(services, st).run(acc)
    assert verify_folder(services, st, acc, "Inbox")[0]
    # письмо, удалённое из архива (индекс), — очищать нельзя, и свежая копия тут не поможет
    services.db.execute("DELETE FROM messages WHERE account_id=? AND subject=?", (acc.id, "Первое"))
    ok, _why, unseen = verify_folder(services, st, acc, "Inbox")
    assert not ok and not unseen
    assert verify_folder(services, st, acc, "Нет такой") == (False, "папки нет на FTP Backup", False)


def test_verify_refuses_tar_folders(services):
    world = FtpWorld(tree(), FtpOptions(tar=True))
    with FakeAxigenFTP(world) as srv:
        st = ftp_settings(srv.port)
        acc = mailbox(services)
        engine(services, st).run(acc)
        ok, why, _unseen = verify_folder(services, st, acc, "Inbox")
    assert not ok and "messages.tar" in why


def test_mailbox_coverage_sees_excluded_and_large(services, fworld, fsrv):
    from mailarchiver.axigen.ftpbackup import describe_gaps, mailbox_coverage
    st = ftp_settings(fsrv.port)
    acc = mailbox(services, folder_exclude=["Sent Items"])
    engine(services, st).run(acc)
    cov = mailbox_coverage(services, st, acc)
    assert not cov["ok"] and cov["missing"] == 1 and "Sent Items" in describe_gaps(cov)
    acc.folder_exclude = []
    services.db.update_account(acc, update_password=False, update_oauth_secret=False, update_oauth_token=False)
    acc = services.db.get_account(acc.id)
    engine(services, st).run(acc)
    assert mailbox_coverage(services, st, acc)["ok"]
    # письмо больше лимита размера копирования в архив не попало — учётную запись удалять нельзя
    fworld.tree["domains"]["company.ru"]["ivanov"]["Inbox"]["big.eml"] = message("Большое", body="x" * 1_200_000)
    engine(services, st, skip_larger_than_mb=1).run(acc)
    cov = mailbox_coverage(services, st, acc)
    assert not cov["ok"] and cov["missing"] == 1
    # календарь и контакты — не почта: их отсутствие в архиве удалению не мешает
    fworld.tree["domains"]["company.ru"]["ivanov"]["Calendar"] = {"e.ics": b"BEGIN:VCALENDAR\r\n"}
    assert all(r["name"] != "Calendar" for r in mailbox_coverage(services, st, acc)["folders"])


def test_cached_path_must_belong_to_the_login(services, fworld, fsrv):
    from mailarchiver.axigen import store as axstore
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    assert axstore.ftp_path(services.db, acc.id) == "/domains/company.ru/ivanov"
    # логин сменили на другого человека — запомненный каталог Иванова больше не годится
    fworld.tree["domains"]["company.ru"]["petrov"] = {"Inbox": {"p.eml": message("Петрову")}}
    acc.username = "petrov@company.ru"
    services.db.update_account(acc, update_password=False, update_oauth_secret=False, update_oauth_token=False)
    acc = services.db.get_account(acc.id)
    engine(services, st).run(acc)
    assert axstore.ftp_path(services.db, acc.id) == "/domains/company.ru/petrov"
    assert "Петрову" in subjects(services, acc.id)


def test_account_dir_rejects_traversal_and_foreign_domains(services, fsrv):
    from mailarchiver.axigen.common import AxigenError
    from mailarchiver.axigen.ftp import AxigenFTP
    with AxigenFTP.open(ftp_settings(fsrv.port, ftp_account_path="domains/{domain}/{local}")) as client:
        with pytest.raises(AxigenError):
            client.find_account_dir("../other.ru/ceo@company.ru")
    with AxigenFTP.open(ftp_settings(fsrv.port, domains=["other.ru"])) as client:
        with pytest.raises(AxigenError) as exc:
            client.find_account_dir("ivanov@company.ru")
    assert "Домены" in exc.value.message


def test_dos_listing_keeps_leading_spaces_and_bad_utf7_names():
    from mailarchiver.axigen.ftp import decode_name, parse_list_line
    entry = parse_list_line("10-13-26  03:08AM                 1234    spaced.eml")
    assert entry.name == "   spaced.eml" and entry.size == 1234
    folder = parse_list_line("10-13-26  03:08AM       <DIR>          Inbox")
    assert folder.is_dir and folder.name == "Inbox"
    assert decode_name("&AC4ALg-") == "&AC4ALg-" and decode_name("&AC8-x") == "&AC8-x"
    from mailarchiver.axigen.ftp import utf7_encode
    assert decode_name(utf7_encode("Входящие")) == "Входящие"


# ---------------------------------------------------------------------------
#  Восстановление через FTP Backup
# ---------------------------------------------------------------------------
def _restore(services, st, acc, **kw):
    from mailarchiver.axigen.ftprestore import FtpRestoreEngine
    opts = dict(target_mode="prefixed", target_prefix="Восстановлено")
    opts.update(kw)
    return FtpRestoreEngine(services.db, services.store, st).run(acc, **opts)


def test_restore_into_prefixed_folders_and_repeat_without_duplicates(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    res = _restore(services, st, acc)
    assert res.restored == 4 and not res.errors
    box = fworld.tree["domains"]["company.ru"]["ivanov"]
    restored = box["Восстановлено"]
    assert set(restored) == {"INBOX", "Sent Items"} and set(restored["INBOX"]) >= {"Проекты"}
    files = [n for n, v in restored["INBOX"].items() if not isinstance(v, dict)]
    assert len(files) == 2 and all(n.startswith("ma") and n.endswith(".eml") for n in files)
    assert any(c.endswith("/" + utf7_encode("Восстановлено")) or "Восстановлено" in c for c in fworld.created)
    # имя новой папки — в кодировке сервера (UTF-7 по умолчанию)
    assert any(cmd == "MKD /domains/company.ru/ivanov/" + utf7_encode("Восстановлено") for cmd in fworld.commands)
    again = _restore(services, st, acc)
    assert again.restored == 0 and again.skipped == 4


def test_restore_into_original_folders_skips_what_is_already_there(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    box = fworld.tree["domains"]["company.ru"]["ivanov"]
    del box["Inbox"]["1.eml"]                                       # письмо удалили на сервере
    box["Inbox"]["x.eml"] = message("Пришло потом")                 # незнакомое архиву письмо
    res = _restore(services, st, acc, target_mode="original")
    assert res.restored == 1 and res.skipped == 3
    assert any(v[0] == message("Первое") for v in box["Inbox"].values() if isinstance(v, tuple))
    # незнакомый архиву файл прочитан ради Message-ID
    assert "/domains/company.ru/ivanov/Inbox/x.eml" in fworld.retrieved


def test_restore_dry_run_changes_nothing(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    before = copy.deepcopy(fworld.tree)
    res = _restore(services, st, acc, dry_run=True)
    assert res.restored == 4 and fworld.tree == before and not fworld.stored


def test_restore_reports_files_the_server_does_not_show(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    fworld.swallow_uploads = True
    res = _restore(services, st, acc, target_mode="single", target_folder="Из архива")
    assert res.restored == 0 and res.errors == 4 and res.status_label == "failed"


def test_restore_accepts_renamed_files(services, fworld, fsrv):
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    fworld.rename_uploads = True
    res = _restore(services, st, acc, target_mode="single", target_folder="Из архива")
    assert res.restored == 4 and not res.errors


def test_restore_job_picks_ftp_for_mailboxes_without_password(services, fworld, fsrv):
    from mailarchiver.errors import ValidationError
    from mailarchiver.queue.jobs import JobContext, handle_backup, handle_restore, restore_via_ftp
    configure(services, fsrv.port)
    acc = mailbox(services)
    assert restore_via_ftp(acc, {}) and not restore_via_ftp(acc, {"via": "imap"})
    assert not restore_via_ftp(mailbox(services, auth_type=AuthType.PASSWORD, password="p"), {"via": "ftp"})
    job = services.db.enqueue_job(JobType.BACKUP, acc.id, {}, 5, 1, "t")
    handle_backup(JobContext(services, job, JobType.BACKUP, acc.id, {}))
    params = {"target_mode": "prefixed", "target_prefix": "Восстановлено", "via": "auto"}
    job = services.db.enqueue_job(JobType.RESTORE, acc.id, params, 5, 1, "t")
    with pytest.raises(ValidationError):                     # изменения на сервере не разрешены
        handle_restore(JobContext(services, job, JobType.RESTORE, acc.id, params))
    services.db.set_settings_many({"axigen.allow_changes": True})
    services.apply_runtime_settings()
    # без подтверждения кнопкой администратора (расписание, задание из старых параметров) — отказ
    job = services.db.enqueue_job(JobType.RESTORE, acc.id, params, 5, 1, "t")
    with pytest.raises(ValidationError) as exc:
        handle_restore(JobContext(services, job, JobType.RESTORE, acc.id, params))
    assert "кнопкой" in exc.value.message
    confirmed = {**params, "ftp_confirmed_by": "admin"}
    job = services.db.enqueue_job(JobType.RESTORE, acc.id, confirmed, 5, 1, "t")
    res = handle_restore(JobContext(services, job, JobType.RESTORE, acc.id, confirmed))
    assert res["final_status"] == "success" and res["restored"] == 4


def test_restore_duplicate_check_survives_a_huge_unknown_file(services, fworld, fsrv, monkeypatch):
    from mailarchiver.axigen import ftp as axftp
    st = ftp_settings(fsrv.port)
    acc = mailbox(services)
    engine(services, st).run(acc)
    # на сервере лежит письмо, которого архив не знает, и оно «слишком большое» для проверки дублей
    fworld.tree["domains"]["company.ru"]["ivanov"]["Inbox"]["unknown.eml"] = message("Незнакомое")
    monkeypatch.setattr(axftp, "MAX_FILE_BYTES", 10)
    res = _restore(services, st, acc, target_mode="original", check_duplicates=True)
    # не упало целиком: письма архива на сервере уже есть (узнаны по индексу), незнакомое — пропущено в проверке
    assert res.errors == 0 and res.restored == 0


def test_restore_options_accept_the_way(services):
    from mailarchiver.accountops import validate_restore_options
    from mailarchiver.errors import ValidationError
    assert validate_restore_options({"target_prefix": "R"})["via"] == "auto"
    assert validate_restore_options({"target_prefix": "R", "via": "FTP"})["via"] == "ftp"
    with pytest.raises(ValidationError):
        validate_restore_options({"target_prefix": "R", "via": "smtp"})
    # подтверждение «нажал администратор» из тела запроса или расписания не проходит
    assert "ftp_confirmed_by" not in validate_restore_options({"target_prefix": "R", "ftp_confirmed_by": "x"})
