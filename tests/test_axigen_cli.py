"""Сервер Axigen через CLI: протокол, разбор ответов, сверка учётных записей, действия, консоль, мониторинг.

Настоящего Axigen здесь нет: тесты идут против учебного сервера (tests/axigen_fake.py), который отвечает
в нескольких видах — клиент обязан понимать все, а не один, подогнанный под себя.
"""
import json
import socket
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from axigen_fake import FakeAccount, FakeAxigenCLI, Style, World, make_cert
from mailarchiver import models
from mailarchiver.axigen import actions, directory, monitor, parse, service
from mailarchiver.axigen import store as axstore
from mailarchiver.axigen.cli import MASK, AxigenCLI, quote, quote_list, strip_telnet
from mailarchiver.axigen.common import (AxigenAuthError, AxigenCertError, AxigenCommandError, AxigenConnectionError,
                                        AxigenError, AxigenNotConfigured, AxigenSettings, blocked_services,
                                        is_loopback, normalize_fingerprint, pretty_fingerprint, services_blocked)
from mailarchiver.errors import ValidationError

ADMIN_PW = "Adm1nPass"


def settings(port, **kw):
    data = dict(enabled=True, host="127.0.0.1", cli_port=port, admin_user="admin", admin_password=ADMIN_PW,
                timeout_s=5, allow_changes=True)
    data.update(kw)
    return AxigenSettings(**data)


@pytest.fixture()
def world():
    return World()


@pytest.fixture()
def srv(world):
    with FakeAxigenCLI(world) as server:
        yield server


def configure(svc, port, **extra):
    values = {"axigen.enabled": True, "axigen.host": "127.0.0.1", "axigen.cli_port": port,
              "axigen.admin_user": "admin", "axigen.admin_password": ADMIN_PW, "axigen.timeout_s": 5,
              "axigen.imap_host": "mx.company.ru"}
    values.update(extra)
    svc.db.set_settings_many(values)
    svc.apply_runtime_settings()


# ---------------------------------------------------------------------------
#  Разбор ответов
# ---------------------------------------------------------------------------
def test_reply_status_is_found_wherever_the_server_puts_it():
    assert parse.split_reply("a\nb\n+OK: command successful\n") == (["a", "b"], True, "command successful")
    assert parse.split_reply("+OK: done\na\nb\n") == (["a", "b"], True, "done")
    assert parse.split_reply("-ERR: Domain x not found") == ([], False, "Domain x not found")
    data, ok, status = parse.split_reply("text only")
    assert data == ["text only"] and ok is None and status == ""


def test_table_with_separator_keeps_multi_word_columns_and_values():
    lines = ["The list of domains:", "name                 total size   used size",
             "-----------------------------------------------", "company.ru           200000 MB    3 MB",
             "filial.company.ru    1 GB         0 MB"]
    table = parse.parse_table(lines)
    assert table.columns == ["name", "total_size", "used_size"]
    assert table.rows[0] == {"name": "company.ru", "total_size": "200000 MB", "used_size": "3 MB"}
    assert table.column("used") == "used_size"
    assert [r["name"] for r in table.rows] == ["company.ru", "filial.company.ru"]


def test_table_without_separator_is_one_value_per_line_and_skips_the_title():
    table = parse.parse_table(["The list of accounts for domain company.ru:", "ivanov", "petrov", ""])
    assert [r["name"] for r in table.rows] == ["ivanov", "petrov"]


def test_table_columns_by_position_when_values_have_single_spaces():
    lines = ["name             messages   size", "----------------------------------",
             "Sent Items       12         3 KB", "INBOX            7          100 KB"]
    rows = parse.parse_table(lines).rows
    assert rows[0] == {"name": "Sent Items", "messages": "12", "size": "3 KB"}


def test_show_pairs_in_several_spellings():
    values = parse.parse_show(['name = "user1"', "firstName: Иван", "services = (localDelivery pop3)", "{",
                               'quoted = "a \\"b\\""', "}"])
    assert values["name"] == "user1" and values["firstName"] == "Иван"
    assert parse.parse_list_value(values["services"]) == ["localDelivery", "pop3"]
    assert values["quoted"] == 'a "b"'
    assert parse.get(values, "FIRSTNAME") == "Иван"


@pytest.mark.parametrize("text,items", [("(a b c)", ["a", "b", "c"]), ("(a, b, c)", ["a", "b", "c"]),
                                         ('("Sent Items" Trash)', ["Sent Items", "Trash"]), ("[x,y]", ["x", "y"]),
                                         ("()", []), ("one", ["one"]), ("Sent Items, Trash", ["Sent Items", "Trash"]),
                                         ('"a, b", c', ["a, b", "c"])])
def test_list_values(text, items):
    assert parse.parse_list_value(text) == items


@pytest.mark.parametrize("text,size", [("12 MB", 12 * 1024 ** 2), ("1.5GB", int(1.5 * 1024 ** 3)), ("2048", 2048),
                                       ("3 КБ", 3072), ("0,5 kb", 512), ("много", None), ("", None)])
def test_sizes(text, size):
    assert parse.parse_size(text) == size


@pytest.mark.parametrize("text,day", [("2027-01-31", date(2027, 1, 31)), ("31.01.2027", date(2027, 1, 31)),
                                      ("Jan 31 2027", date(2027, 1, 31)), ("Sun Jan 31 10:00:00 2027", date(2027, 1, 31)),
                                      ("never", None)])
def test_dates(text, day):
    assert parse.parse_date(text) == day


def test_license_expiry_and_limits():
    values = {"license type": "Business", "expiration date": "2027-01-31", "max accounts": "250",
              "max domains": "5", "used accounts": "17"}
    info = parse.parse_license(values, [])
    assert info["expires"] == "2027-01-31" and info["accounts_limit"] == 250 and info["domains_limit"] == 5
    assert info["type"] == "Business"


def test_version_number():
    assert parse.parse_version("10.6.30|Linux|x86_64") == "10.6.30"
    assert parse.version_tuple("10.6.30") == (10, 6, 30)


# ---------------------------------------------------------------------------
#  Протокол
# ---------------------------------------------------------------------------
def test_values_that_could_change_the_command_are_refused():
    assert quote("ivanov") == "ivanov"
    assert quote("Sent Items") == '"Sent Items"'
    assert quote("Иван") == '"Иван"'
    for bad in ('a"b', "a\\b", "a\nb", "a\rb", "a\x00", "a\u2028b", "a\x85b", "a\x7fb"):
        with pytest.raises(AxigenError):
            quote(bad)
    with pytest.raises(AxigenError):
        quote("")
    assert quote_list(["localDelivery", "pop3"]) == "(localDelivery pop3)"


def test_telnet_negotiation_is_stripped():
    assert strip_telnet(b"\xff\xfb\x01hello\xff\xff!\xff\xfa\x18\x01\xff\xf0x") == b"hello\xff!x"


def test_login_version_and_masked_password(srv, world):
    with AxigenCLI.open(settings(srv.port)) as cli:
        assert cli.at_root
        assert cli.version.startswith("10.6.30")
        cli.run("LIST Domains")
    text = cli.transcript_text()
    assert ADMIN_PW not in text and MASK in text
    assert "> LIST Domains" in text


def test_wrong_admin_password_is_an_auth_error(srv):
    with pytest.raises(AxigenAuthError):
        AxigenCLI.open(settings(srv.port, admin_password="wrong"))


def test_command_refusal_and_newline_guard(srv):
    with AxigenCLI.open(settings(srv.port)) as cli:
        with pytest.raises(AxigenCommandError) as err:
            cli.run("UPDATE Domain name nosuch.ru")
        assert "not found" in err.value.reply
        reply = cli.run("UPDATE Domain name nosuch.ru", check=False)
        assert reply.ok is False
        with pytest.raises(AxigenError):
            cli.run("LIST Domains\nREMOVE Account name x")


def test_service_that_is_not_cli_is_reported_quickly():
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)

    def serve():
        conn, _ = lsock.accept()
        conn.sendall(b"220 smtp.example ESMTP\r\n")
        try:
            conn.recv(100)
        except OSError:
            pass
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    import mailarchiver.axigen.cli as cli_mod
    old = cli_mod.FIRST_REPLY_S
    cli_mod.FIRST_REPLY_S = 1.0
    try:
        with pytest.raises(AxigenConnectionError) as err:
            AxigenCLI.open(settings(lsock.getsockname()[1]))
    finally:
        cli_mod.FIRST_REPLY_S = old
        lsock.close()
    assert "приглашение" in err.value.message


STYLES = [Style(), Style(separator=False, title=False), Style(status_first=True), Style(echo=True),
          Style(iac=True, chunked=True), Style(newline="\n", show_sep=": ", quote_values=False, list_commas=True),
          Style(folder_columns=False)]


@pytest.mark.parametrize("style", STYLES, ids=["default", "plain", "status-first", "echo", "iac-chunked", "colon",
                                               "folders-plain"])
def test_directory_reads_the_same_in_every_reply_style(style):
    world = World()
    world.style = style
    with FakeAxigenCLI(world) as srv, AxigenCLI.open(settings(srv.port)) as cli:
        assert directory.list_domains(cli) == ["company.ru"]
        assert directory.list_account_names(cli, "company.ru") == ["ivanov", "petrov", "postmaster"]
        info = directory.account_details(cli, "company.ru", "ivanov")
        assert cli.at_root
    assert info["errors"] == ""
    assert info["first_name"] == "Иван" and info["last_name"] == "Иванов"
    assert info["aliases"] == ["i.ivanov@company.ru"]
    assert info["services"] == ["localDelivery", "remoteDelivery", "pop3", "imap", "webmail"]
    assert info["login_blocked"] is False
    assert info["quota_kb"] == 1048576
    assert info["folders"] == 2
    if style.folder_columns and style.separator:
        assert info["messages"] == 4 and info["used_bytes"] == 32 * 1024
    else:
        assert info["messages"] is None and info["used_bytes"] is None
    assert info["last_login"].startswith("2026-09-30T10:15")


def test_one_unreadable_field_does_not_lose_the_others(srv, world):
    world.fail["CONFIG Quotas"] = "Access denied"
    with AxigenCLI.open(settings(srv.port)) as cli:
        info = directory.account_details(cli, "company.ru", "ivanov")
        assert cli.at_root
    assert "квота: Access denied" in info["errors"]
    assert info["quota_kb"] is None and info["first_name"] == "Иван"


# ---------------------------------------------------------------------------
#  Шифрование и сертификаты
# ---------------------------------------------------------------------------
def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1") and is_loopback("[::1]")
    assert not is_loopback("10.0.0.5") and not is_loopback("") and not is_loopback("no-such-host.invalid")
    # имена не разрешаются: ответ DNS мог бы смениться между проверкой и подключением
    assert not is_loopback("evil.localhost") and not is_loopback("mx.company.ru")


def test_password_is_not_sent_over_the_network_without_tls(monkeypatch):
    called = []
    monkeypatch.setattr("mailarchiver.axigen.cli.open_socket", lambda *a, **k: called.append(a))
    with pytest.raises(AxigenNotConfigured) as err:
        AxigenCLI.open(settings(7000, host="10.11.12.13"))
    assert "без шифрования" in err.value.message and not called
    # явное разрешение — подключаемся
    with pytest.raises(Exception):
        AxigenCLI.open(settings(7000, host="10.11.12.13", allow_plaintext=True))
    assert called


def test_cli_over_tls_with_pinned_self_signed_certificate(tmp_path, world):
    cert = make_cert(str(tmp_path))
    with FakeAxigenCLI(world, tls=cert) as srv:
        with AxigenCLI.open(settings(srv.port, cli_tls="tls", cli_cert_sha256=pretty_fingerprint(cert[2]))) as cli:
            assert cli.fingerprint == cert[2]
            assert directory.list_domains(cli) == ["company.ru"]
        # самоподписанный сертификат без отпечатка — не доверяем
        with pytest.raises(AxigenCertError):
            AxigenCLI.open(settings(srv.port, cli_tls="tls"))
        # чужой отпечаток — тем более
        with pytest.raises(AxigenCertError) as err:
            AxigenCLI.open(settings(srv.port, cli_tls="tls", cli_cert_sha256="ab" * 32))
        assert err.value.fingerprint == cert[2]
        assert service.cli_fingerprint(settings(srv.port, cli_tls="tls")) == cert[2]


def test_fingerprint_normalization():
    fp = "AB:" * 31 + "AB"
    assert normalize_fingerprint(fp) == "ab" * 32
    assert pretty_fingerprint("ab" * 32) == fp
    assert normalize_fingerprint("short") == ""


# ---------------------------------------------------------------------------
#  Сверка и снимок
# ---------------------------------------------------------------------------
def test_fetch_and_snapshot_track_new_gone_and_back(services, srv, world):
    st = settings(srv.port)
    with AxigenCLI.open(st) as cli:
        res = directory.fetch(cli, st)
    assert res.domains == ["company.ru"] and len(res.accounts) == 3
    changes = axstore.save_snapshot(services.db, res.accounts, res.domains)
    assert sorted(changes["new"]) == ["ivanov@company.ru", "petrov@company.ru", "postmaster@company.ru"]
    del world.domains["company.ru"].accounts["petrov"]
    world.domains["company.ru"].accounts["ivanov"].services = ["localDelivery"]
    with AxigenCLI.open(st) as cli:
        res = directory.fetch(cli, st)
    changes = axstore.save_snapshot(services.db, res.accounts, res.domains)
    assert changes["gone"] == ["petrov@company.ru"] and changes["changed"] == ["ivanov@company.ru"]
    row = axstore.get_account(services.db, "ivanov@company.ru")
    assert row["login_blocked"] is True and row["services"] == ["localDelivery"]
    assert axstore.get_account(services.db, "petrov@company.ru")["deleted_at"]
    # домен, список которого не прочитался, не «удаляет» свои учётные записи
    changes = axstore.save_snapshot(services.db, [], [])
    assert changes["gone"] == []
    world.domains["company.ru"].accounts["petrov"] = FakeAccount()
    with AxigenCLI.open(st) as cli:
        res = directory.fetch(cli, st)
    assert axstore.save_snapshot(services.db, res.accounts, res.domains)["back"] == ["petrov@company.ru"]


def test_fetch_reports_domains_that_failed_or_are_missing(srv, world):
    world.domains["filial.ru"] = world.domains["company.ru"].__class__(accounts={"x": FakeAccount()})
    world.fail["UPDATE Domain name filial.ru"] = "Access denied"
    st = settings(srv.port, domains=["company.ru", "filial.ru", "absent.ru"])
    with AxigenCLI.open(st) as cli:
        res = directory.fetch(cli, st)
        assert cli.at_root
    assert res.domains == ["company.ru"]
    assert res.failed_domains["filial.ru"] == "Access denied"
    assert "нет на сервере" in res.failed_domains["absent.ru"]


def test_match_by_login_then_alias_and_orphans(services):
    st = AxigenSettings(enabled=True, host="10.0.0.5", imap_host="mx.company.ru")
    db = services.db
    a = db.create_account(models.Account(name="Иванов", host="mx.company.ru", username="i.ivanov@company.ru",
                                         password="p"))
    b = db.create_account(models.Account(name="Петров", host="mx.company.ru", username="petrov@company.ru",
                                         password="p"))
    db.create_account(models.Account(name="Гость", host="mx.company.ru", username="guest@company.ru", password="p"))
    db.create_account(models.Account(name="Чужой", host="imap.other.ru", username="x@other.ru", password="p"))
    axstore.save_snapshot(db, [
        {"email": "ivanov@company.ru", "domain": "company.ru", "name": "ivanov", "aliases": ["i.ivanov@company.ru"],
         "quota_kb": 1024, "used_bytes": 1024 * 1000},
        {"email": "petrov@company.ru", "domain": "company.ru", "name": "petrov"},
        {"email": "new@company.ru", "domain": "company.ru", "name": "new"},
    ], ["company.ru"])
    data = directory.overview(services, st)
    by = {r["email"]: r for r in data["accounts"]}
    assert by["ivanov@company.ru"]["mailbox"]["id"] == a
    assert by["petrov@company.ru"]["mailbox"]["id"] == b
    assert by["new@company.ru"]["mailbox"] is None
    assert by["ivanov@company.ru"]["quota_percent"] == pytest.approx(97.7, abs=0.1)
    assert [o["name"] for o in data["orphans"]] == ["Гость"]


# ---------------------------------------------------------------------------
#  Действия на сервере
# ---------------------------------------------------------------------------
def test_block_keeps_receiving_and_unblock_restores(srv, world):
    acc = world.domains["company.ru"].accounts["ivanov"]
    with AxigenCLI.open(settings(srv.port)) as cli:
        before, after = actions.block(cli, "ivanov@company.ru", keep_receiving=True)
        assert cli.at_root
        assert after == ["localDelivery"] and acc.services == ["localDelivery"]
        assert services_blocked(after)
        again = actions.block(cli, "ivanov@company.ru")
        assert again == (["localDelivery"], ["localDelivery"])
        restored = actions.unblock(cli, "ivanov@company.ru", before)
    assert restored == before == acc.services


def test_block_without_receiving_and_without_inventing_services(srv, world):
    acc = world.domains["company.ru"].accounts["petrov"]
    acc.services = ["pop3", "imap"]           # приёма писем не было — блокировка его не включает
    with AxigenCLI.open(settings(srv.port)) as cli:
        _before, after = actions.block(cli, "petrov@company.ru", keep_receiving=True)
    assert after == [] and acc.services == []
    assert blocked_services(["localDelivery", "imap"], keep_receiving=False) == []


def test_password_change_is_masked_in_the_transcript(srv, world):
    with AxigenCLI.open(settings(srv.port)) as cli:
        actions.set_password(cli, "ivanov@company.ru", "N3wPassw0rd")
    assert world.domains["company.ru"].accounts["ivanov"].password == "N3wPassw0rd"
    assert "N3wPassw0rd" not in cli.transcript_text()
    with pytest.raises(ValidationError):
        actions.check_password("с пробелом 1")
    with pytest.raises(ValidationError):
        actions.check_password("short")
    pw = actions.random_password()
    assert len(pw) == 20 and pw.isalnum()


def test_create_and_remove_account(srv, world):
    dom = world.domains["company.ru"]
    with AxigenCLI.open(settings(srv.port)) as cli:
        actions.create_account(cli, "sidorov@company.ru", "Passw0rd1", first_name="Сидор", last_name="Сидоров")
        assert cli.at_root
        assert dom.accounts["sidorov"].first_name == "Сидор"
        with pytest.raises(ValidationError):
            actions.create_account(cli, "sidorov@company.ru", "Passw0rd1")
        actions.remove_account(cli, "sidorov@company.ru")
        assert "sidorov" not in dom.accounts
        assert not actions.account_exists(cli, "sidorov@company.ru")
    assert "Passw0rd1" not in cli.transcript_text()


def test_purge_folder_commands_and_allowed_folders(srv, world):
    acc = world.domains["company.ru"].accounts["ivanov"]
    acc.folders["Trash"] = (5, 1000)
    with AxigenCLI.open(settings(srv.port)) as cli:
        assert actions.purge_allowed_folders(cli, "company.ru") == ["Trash", "Spam"]
        assert "Trash" in actions.server_folders(cli, "ivanov@company.ru")
        actions.purge_folder(cli, "ivanov@company.ru", "Trash", 365)
        with pytest.raises(ValidationError):
            actions.purge_folder(cli, "ivanov@company.ru", "Trash", 0)
        with pytest.raises(AxigenCommandError):
            actions.purge_folder(cli, "ivanov@company.ru", "INBOX", 30)    # домен не разрешает очищать INBOX
        assert cli.at_root
    assert acc.purges == [("Trash", "365d")]
    assert actions.match_server_folder("inbox", ["INBOX", "Sent"]) == "INBOX"


def test_failure_inside_a_nested_context_returns_to_root(srv, world):
    world.fail_in_context["domain-account-account-limits#"] = "locked"
    with AxigenCLI.open(settings(srv.port)) as cli:
        with pytest.raises(AxigenCommandError):
            actions.set_services(cli, "ivanov@company.ru", ["localDelivery"])
        assert cli.at_root
        assert directory.list_domains(cli) == ["company.ru"]


def test_bad_addresses_are_rejected_before_any_command(srv, world):
    with AxigenCLI.open(settings(srv.port)) as cli:
        for bad in ("ivanov", "iva nov@company.ru", 'a"b@company.ru', "x@-bad-.ru"):
            with pytest.raises(ValidationError):
                actions.block(cli, bad)
    assert not srv.executed("UPDATE Account")


# ---------------------------------------------------------------------------
#  Служба: проверка, консоль, диагностика, журнал
# ---------------------------------------------------------------------------
def test_check_connection_reads_version_license_domains_and_queue(services, srv):
    configure(services, srv.port)
    res = service.check_connection(services, username="admin", ftp=False)
    assert res["ok"] and res["cli"]["ok"]
    cli = res["cli"]
    assert cli["version"].startswith("10.6.30")
    assert cli["license"]["expires"] == "2027-01-31"
    assert [d["name"] for d in cli["domains"]] == ["company.ru"]
    assert cli["queue"] == 1
    assert ADMIN_PW not in json.dumps(res)
    log_items = axstore.list_log(services.db)
    assert log_items[0]["kind"] == "check" and log_items[0]["ok"] == 1


def test_check_connection_explains_missing_settings(services):
    services.db.set_settings_many({"axigen.enabled": True})
    services.apply_runtime_settings()
    res = service.check_connection(services, username="admin")
    assert not res.get("ok") and "адрес" in res["error"].lower()


@pytest.mark.parametrize("command", ["LIST Domains", "SHOW LicenseInfo", "UPDATE Domain name company.ru",
                                     "UPDATE Account name ivanov", "CONFIG Limits", "ENTER QUEUE", "BACK", "HELP",
                                     "GET VERSION", "SEARCH Object ivanov@company.ru"])
def test_console_allows_reading(command):
    assert service.console_allowed(command) is None


@pytest.mark.parametrize("command", ["SET password x", "COMMIT", "REMOVE Account name x", "PURGE account folders Inbox",
                                     "ADD Account name x password y", "SAVE CONFIG", "ENTER AACL", "UPDATE Listener 1",
                                     "DELETE 0x1a", "COMPACT All", "", "LIST Domains\x00", "QUERY remove x",
                                     "ENTER JSONQUERY", "LIST Domains\x7f", "LIST\u2028Domains"])
def test_console_refuses_changes(command):
    assert service.console_allowed(command)


def test_console_runs_one_session_and_logs_it(services, srv, world):
    configure(services, srv.port)
    res = service.run_console(services, username="admin", commands=["UPDATE Domain name company.ru", "LIST Accounts"])
    assert res["replies"][1]["ok"] and "ivanov" in res["replies"][1]["text"]
    with pytest.raises(ValidationError):
        service.run_console(services, username="admin", commands=["LIST Domains", "COMMIT"])
    assert ("domain#", "LIST Accounts") in world.commands
    entry = axstore.list_log(services.db)[0]
    assert entry["kind"] == "console"
    full = axstore.get_log(services.db, entry["id"])
    assert "> LIST Accounts" in full["transcript"] and ADMIN_PW not in full["transcript"]


def test_diagnostics_show_raw_replies(services, srv):
    configure(services, srv.port)
    res = service.diagnostics(services, username="admin", email="ivanov@company.ru")
    titles = [s["title"] for s in res["sections"]]
    assert "Лицензия" in titles and "Папки" in titles and "Limits" in titles and "Очередь" in titles
    folders = next(s for s in res["sections"] if s["title"] == "Папки")
    assert "INBOX" in folders["text"]


def test_queue_listing(services, srv):
    configure(services, srv.port)
    res = service.read_queue(services, username="admin")
    assert res["count"] == 1 and res["rows"][0]["id"] == "0x1a"


def test_log_keeps_people_actions_longer_than_scheduled_checks(services, monkeypatch):
    monkeypatch.setattr(axstore, "LOG_KEEP", {"write": 5, "console": 5, "monitor": 2})
    monkeypatch.setattr(axstore, "LOG_KEEP_OTHER", 3)
    for i in range(4):
        axstore.add_log(services.db, username="a", kind="write", summary=f"w{i}", ok=True, commands=1, transcript="")
    for i in range(10):
        axstore.add_log(services.db, username="system", kind="monitor", summary=f"m{i}", ok=True, commands=1,
                        transcript="")
    kinds = [r["kind"] for r in axstore.list_log(services.db, 100)]
    assert kinds.count("write") == 4 and kinds.count("monitor") == 2
    people = axstore.list_log(services.db, 100, kind="people")
    assert {r["kind"] for r in people} == {"write"}


# ---------------------------------------------------------------------------
#  Мониторинг
# ---------------------------------------------------------------------------
def test_status_refresh_and_problems(services, srv, world, monkeypatch):
    configure(services, srv.port, **{"axigen.alert_queue": 1, "axigen.alert_license_days": 4000,
                                     "axigen.alert_quota_percent": 50})
    status = monitor.refresh(services, username="admin")
    assert status["ok"] and status["queue"] == 1 and status["failures"] == 0
    axstore.save_snapshot(services.db, [
        {"email": "ivanov@company.ru", "domain": "company.ru", "name": "ivanov", "quota_kb": 100,
         "used_bytes": 90 * 1024, "login_blocked": True, "services": ["localDelivery"]},
        {"email": "petrov@company.ru", "domain": "company.ru", "name": "petrov"},
    ], ["company.ru"])
    services.db.create_account(models.Account(name="Иванов", host="mx.company.ru", username="ivanov@company.ru",
                                              password="p"))
    keys = {p["key"]: p for p in monitor.problems(services)}
    assert {"queue", "license", "quota", "unarchived", "blocked_imap"} <= set(keys)
    assert "petrov@company.ru" in keys["unarchived"]["text"]
    assert keys["license"]["level"] == "warning"


def test_unreachable_after_two_failed_checks(services, srv):
    configure(services, srv.port)
    monitor.refresh(services)
    services.db.set_settings_many({"axigen.cli_port": 1})
    services.apply_runtime_settings()
    first = monitor.refresh(services)
    assert not first["ok"] and first["failures"] == 1
    assert "unreachable" not in {p["key"] for p in monitor.problems(services)}
    monitor.refresh(services)
    assert "unreachable" in {p["key"] for p in monitor.problems(services)}
    # прежние сведения (версия, домены) не теряются, пока сервер недоступен
    assert monitor.cached(services)["version"].startswith("10.6.30")


def test_alerts_are_sent_once_a_day_per_problem(services, monkeypatch):
    sent = []

    class Notifier:
        def enabled(self):
            return True

        def send(self, subject, body):
            sent.append(body)
    monkeypatch.setattr(services, "notifier", Notifier())
    found = [{"key": "queue", "level": "warning", "text": "очередь"}]
    assert monitor.notify_new(services, found) == ["queue"]
    assert monitor.notify_new(services, found) == []
    alerted = json.loads(services.db.get_meta(monitor.ALERTED_KEY))
    alerted["queue"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    services.db.set_meta(monitor.ALERTED_KEY, json.dumps(alerted))
    assert monitor.notify_new(services, found) == ["queue"]
    monitor.notify_new(services, [])                       # проблема ушла — её забыли
    assert monitor.notify_new(services, found) == ["queue"]
    assert len(sent) == 3


def test_tick_does_nothing_when_disabled(services):
    assert monitor.tick(services) is None


def test_metrics_and_summary(services, srv):
    from mailarchiver.monitoring import render_prometheus
    configure(services, srv.port)
    monitor.refresh(services)
    text = render_prometheus(services)
    assert "mailarchiver_axigen_up 1" in text
    assert "mailarchiver_axigen_queue_messages 1" in text
    assert 'mailarchiver_axigen_problems{problem="queue"}' in text
    lines = monitor.summary_lines(services)
    assert lines and "доступен" in lines[0]


def test_license_days_left():
    assert monitor.license_days_left({"license": {"expires": "2026-10-20"}}, today=date(2026, 10, 13)) == 7
    assert monitor.license_days_left({}) is None


def test_unverified_certificate_is_a_problem(services):
    from mailarchiver.axigen import monitor
    configure(services, 7443, **{"axigen.host": "10.0.0.5", "axigen.cli_tls": "tls", "axigen.tls_verify": False})
    keys = [p["key"] for p in monitor.problems(services, status={})]
    assert "tls_unverified" in keys
    services.db.set_settings_many({"axigen.cli_cert_sha256": "ab" * 32})
    services.apply_runtime_settings()
    assert "tls_unverified" not in [p["key"] for p in monitor.problems(services, status={})]


def test_prompt_like_data_line_does_not_end_the_reply_early(srv, world):
    # папка с именем «<#>» в конце списка: у сервера, который присылает итоги, такая строка без итога — данные
    world.domains["company.ru"].accounts["ivanov"].folders["<#>"] = (0, 0)
    world.style.folder_columns = False            # строка — ровно «<#>», как приглашение
    with AxigenCLI.open(settings(srv.port)) as cli:
        cli.run("UPDATE Domain name company.ru")
        cli.run("UPDATE Account name ivanov")
        reply = cli.run("LIST Folders")
        assert reply.ok is True and "<#>" in reply.text
        cli.to_root()
