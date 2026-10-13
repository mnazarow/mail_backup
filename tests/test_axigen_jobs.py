"""Задания и групповые действия Axigen: сверка, изменения на сервере по кнопке, планировщик, сотрудники, настройки.

Обе службы Axigen — учебные (tests/axigen_fake.py): CLI для учётных записей и FTP Backup для писем.
Любое изменение на сервере проверяется по состоянию учебного сервера, а не по ответу клиента.
"""
import json

import pytest

from axigen_fake import FakeAccount, FakeAxigenCLI, FakeAxigenFTP, FtpWorld, World, message
from mailarchiver import bulk, models
from mailarchiver.axigen import jobs as axjobs
from mailarchiver.axigen import store as axstore
from mailarchiver.config import coerce_setting
from mailarchiver.errors import ValidationError
from mailarchiver.models import AuthType, JobStatus, JobType
from mailarchiver.queue.jobs import JobContext

ADMIN = {"username": "tester", "role": "admin"}
ADMIN_PW = "Adm1nPass"


def ftp_tree():
    return {"domains": {"company.ru": {
        "ivanov": {"Inbox": {"1.eml": message("Первое"), "2.eml": message("Второе")},
                   "Trash": {"t1.eml": message("Удалённое", date="Mon, 01 Jan 2024 10:00:00 +0000")}},
        "petrov": {"Inbox": {"p.eml": message("Петрову")}},
        "postmaster": {"Inbox": {}},
    }}}


@pytest.fixture()
def ax(services):
    world = World()
    world.domains["company.ru"].accounts["ivanov"].folders = {"INBOX": (2, 2048), "Trash": (1, 1024)}
    fworld = FtpWorld(ftp_tree())
    with FakeAxigenCLI(world) as cli, FakeAxigenFTP(fworld) as ftp:
        services.db.set_settings_many({
            "axigen.enabled": True, "axigen.host": "127.0.0.1", "axigen.cli_port": cli.port,
            "axigen.ftp_port": ftp.port, "axigen.admin_user": "admin", "axigen.admin_password": ADMIN_PW,
            "axigen.timeout_s": 5, "axigen.imap_host": "mx.company.ru", "axigen.allow_changes": True})
        services.apply_runtime_settings()
        yield type("Ax", (), {"world": world, "fworld": fworld, "cli": cli, "ftp": ftp, "svc": services})


def run_job(svc, job_type, params, account_id=None):
    job_id = svc.db.enqueue_job(job_type, account_id, params, 5, 1, "tester")
    ctx = JobContext(svc, job_id, job_type, account_id, params)
    from mailarchiver.queue.jobs import HANDLERS
    res = HANDLERS[job_type](ctx)
    return res, [r["message"] for r in svc.db.list_job_events(job_id)]


def sync(svc):
    return run_job(svc, JobType.AXIGEN_SYNC, {"requested_by": "tester"})


def mailbox(svc, username, **kw):
    data = dict(name=username.split("@")[0].capitalize(), host="mx.company.ru", username=username, password="",
                auth_type=AuthType.AXIGEN_FTP, retention_days=0)
    data.update(kw)
    return svc.db.create_account(models.Account(**data))


def action(svc, name, ids, **options):
    return run_job(svc, JobType.AXIGEN_ACTION, {"action": name, "account_ids": ids, "options": options,
                                                "requested_by": "tester"})


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_saves_snapshot_and_reports(ax):
    res, events = sync(ax.svc)
    assert res["final_status"] == JobStatus.SUCCESS
    assert res["accounts"] == 3 and res["new"] == 3 and res["added"] == 0
    row = axstore.get_account(ax.svc.db, "ivanov@company.ru")
    assert row["first_name"] == "Иван" and row["aliases"] == ["i.ivanov@company.ru"]
    assert any("Новые учётные записи" in e for e in events)
    audit = ax.svc.db.query_one("SELECT action FROM audit ORDER BY id DESC LIMIT 1")
    assert audit["action"] == "axigen_sync"


def test_sync_can_add_mailboxes_for_new_accounts(ax):
    ax.svc.db.set_settings_many({"axigen.sync_add_new": True})
    ax.svc.apply_runtime_settings()
    existing = mailbox(ax.svc, "petrov@company.ru", auth_type=AuthType.PASSWORD, password="p")
    res, _ = sync(ax.svc)
    assert res["added"] == 2
    boxes = {a.username: a for a in ax.svc.db.list_accounts()}
    assert boxes["ivanov@company.ru"].auth_type == AuthType.AXIGEN_FTP
    assert boxes["ivanov@company.ru"].name == "Иван Иванов"
    assert boxes["ivanov@company.ru"].host == "mx.company.ru"
    assert boxes["petrov@company.ru"].id == existing                # уже был — второй не заводится
    res, _ = sync(ax.svc)
    assert res["added"] == 0 and len(ax.svc.db.list_accounts()) == 3


def test_sync_is_partial_when_a_domain_fails(ax):
    from axigen_fake import FakeDomain
    ax.world.domains["filial.ru"] = FakeDomain(accounts={"x": FakeAccount()})
    ax.world.fail["UPDATE Domain name filial.ru"] = "Access denied"
    res, events = sync(ax.svc)
    assert res["final_status"] == JobStatus.PARTIAL
    assert any("filial.ru" in e and "удалёнными не считаются" in e for e in events)


def test_add_mailboxes_preview_and_skips(ax):
    sync(ax.svc)
    items = axjobs.add_mailboxes(ax.svc, ["ivanov@company.ru", "nobody@company.ru"], user="t", preview=True)
    assert [i["status"] for i in items] == ["ok", "skip"]
    assert not ax.svc.db.list_accounts()
    items = axjobs.add_mailboxes(ax.svc, ["ivanov@company.ru"], user="t", auth=AuthType.PASSWORD, enable=False)
    acc = ax.svc.db.get_account(items[0]["account_id"])
    assert acc.auth_type == AuthType.PASSWORD and not acc.enabled
    again = axjobs.add_mailboxes(ax.svc, ["ivanov@company.ru"], user="t")
    assert again[0]["status"] == "skip" and "уже копируется" in again[0]["text"]


# ---------------------------------------------------------------------------
#  Изменения на сервере
# ---------------------------------------------------------------------------
def test_block_makes_a_final_copy_first_and_unblock_restores(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, events = action(ax.svc, "block", [acc_id])
    assert res["final_status"] == JobStatus.SUCCESS and res["ok"] == 1
    server = ax.world.domains["company.ru"].accounts["ivanov"]
    assert server.services == ["localDelivery"]
    assert ax.svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc_id,)) == 3
    row = axstore.get_account(ax.svc.db, "ivanov@company.ru")
    assert row["login_blocked"] and row["saved_services"][:2] == ["localDelivery", "remoteDelivery"]
    assert row["blocked_by"] == "tester"
    assert any("вход закрыт" in e for e in events)
    res, _ = action(ax.svc, "unblock", [acc_id])
    assert res["ok"] == 1
    assert server.services == ["localDelivery", "remoteDelivery", "pop3", "imap", "webmail"]
    assert not axstore.get_account(ax.svc.db, "ivanov@company.ru")["login_blocked"]


def test_block_is_not_done_when_the_final_copy_fails(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    ax.fworld.deny["/domains/company.ru/ivanov/Inbox"] = "Permission denied"
    res, _ = action(ax.svc, "block", [acc_id])
    assert res["skipped"] == 1 and res["ok"] == 0
    assert "pop3" in ax.world.domains["company.ru"].accounts["ivanov"].services
    assert "НЕ заблокирован" in res["items"][0]["detail"]


def test_block_with_random_password_keeps_imap_copying(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "petrov@company.ru", auth_type=AuthType.PASSWORD, password="old")
    res, _ = action(ax.svc, "block", [acc_id], final_copy=False, random_password=True)
    assert res["ok"] == 1
    new_pw = ax.world.domains["company.ru"].accounts["petrov"].password
    assert new_pw != "Secret123" and ax.svc.db.get_account(acc_id).password == new_pw
    # пароль сохранён в карточке, но со входом закрытым IMAP всё равно не пустит — об этом сказано
    assert "переведите его на копирование через FTP Backup" in res["items"][0]["detail"]
    log = axstore.get_log(ax.svc.db, axstore.list_log(ax.svc.db, kind="write")[0]["id"])
    assert new_pw not in log["transcript"]


def test_password_action_for_ftp_mailbox_is_not_stored(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, _ = action(ax.svc, "password", [acc_id])
    assert res["ok"] == 1 and "нигде не сохранён" in res["items"][0]["detail"]
    assert ax.svc.db.get_account(acc_id).password == ""


def test_purge_only_verified_and_allowed_folders(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, _ = action(ax.svc, "purge", [acc_id], days=365, folders="")
    assert res["ok"] == 1
    assert ax.world.domains["company.ru"].accounts["ivanov"].purges == [("Trash", "365d")]
    assert ax.cli.executed("PURGE") == ["PURGE account folders Trash BEFORE 365d"]
    # INBOX домену очищать не разрешено — даже если попросить явно
    res, _ = action(ax.svc, "purge", [acc_id], days=365, folders="INBOX")
    assert res["skipped"] == 1 and "не разрешает" in res["items"][0]["detail"]


def test_purge_refuses_mailboxes_whose_archive_is_not_complete(ax):
    sync(ax.svc)
    limited = mailbox(ax.svc, "ivanov@company.ru", retention_days=90)
    imap = mailbox(ax.svc, "petrov@company.ru", auth_type=AuthType.PASSWORD, password="p")
    res, _ = action(ax.svc, "purge", [limited, imap], days=365)
    texts = [r["detail"] for r in res["items"]]
    assert res["skipped"] == 2 and "90 дн." in texts[0] and "FTP Backup" in texts[1]
    res, _ = action(ax.svc, "purge", [limited], days=10)
    assert res["failed"] == 1
    assert not ax.cli.executed("PURGE")


def test_delete_requires_blocked_login_then_keeps_the_archive(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["skipped"] == 1 and "сначала «Заблокировать" in res["items"][0]["detail"]
    action(ax.svc, "block", [acc_id])
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["ok"] == 1
    assert "ivanov" not in ax.world.domains["company.ru"].accounts
    acc = ax.svc.db.get_account(acc_id)
    assert not acc.enabled and acc.on_hold()
    assert axstore.get_account(ax.svc.db, "ivanov@company.ru")["deleted_at"]
    assert ax.svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc_id,)) == 3


def test_delete_holds_the_archive_before_the_final_copy(ax):
    # срок хранения 90 дней: без удержания последняя копия не скачала бы старые письма — и они пропали бы
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru", retention_days=90)
    action(ax.svc, "block", [acc_id])
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["ok"] == 1, res
    assert ax.svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (acc_id,)) == 3
    assert ax.svc.db.get_account(acc_id).hold_until == "9999-12-31"


def test_delete_refuses_when_mail_is_not_archived(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru", folder_exclude=["Trash"])
    action(ax.svc, "block", [acc_id])
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["skipped"] == 1 and "Trash" in res["items"][0]["detail"]
    assert "ivanov" in ax.world.domains["company.ru"].accounts
    assert ax.svc.db.get_account(acc_id).hold_until == ""          # удержание, поставленное для копии, снято
    # приём писем на время удаления останавливался — и вернулся
    assert ax.world.domains["company.ru"].accounts["ivanov"].services == ["localDelivery"]
    assert ax.cli.executed("SET services ()")
    res, _ = action(ax.svc, "delete", [acc_id], allow_loss=True)
    assert res["ok"] == 1 and "так решил администратор" in res["items"][0]["detail"]
    assert "ivanov" not in ax.world.domains["company.ru"].accounts


def test_delete_of_imap_mailbox_needs_explicit_consent(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "petrov@company.ru", auth_type=AuthType.PASSWORD, password="p")
    action(ax.svc, "block", [acc_id], final_copy=False)
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["skipped"] == 1 and "по IMAP" in res["items"][0]["detail"]
    # и с согласием на потери — не без единой удачной копии
    res, _ = action(ax.svc, "delete", [acc_id], allow_loss=True)
    assert res["skipped"] == 1 and "удачной копии" in res["items"][0]["detail"]
    assert "petrov" in ax.world.domains["company.ru"].accounts


def test_delete_failure_midway_undoes_hold_and_receiving(ax, monkeypatch):
    from mailarchiver.axigen import ftpbackup
    from mailarchiver.axigen.common import AxigenConnectionError
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    action(ax.svc, "block", [acc_id])

    def broken(*a, **k):
        raise AxigenConnectionError("FTP Backup: связь оборвалась")
    monkeypatch.setattr(ftpbackup, "mailbox_coverage", broken)
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["failed"] == 1 and "оборвалась" in res["items"][0]["detail"]
    assert "ivanov" in ax.world.domains["company.ru"].accounts
    assert ax.svc.db.get_account(acc_id).hold_until == ""
    assert ax.world.domains["company.ru"].accounts["ivanov"].services == ["localDelivery"]


def test_password_with_unicode_line_separator_is_refused_without_echo(ax):
    sync(ax.svc)
    with pytest.raises(ValidationError) as exc:
        axjobs.set_account_password(ax.svc, user="t", email="ivanov@company.ru", password="Leak\u2028Pass9")
    assert "Leak" not in exc.value.message
    assert all("Leak" not in (r["summary"] or "") + (r["transcript"] or "")
               for r in ax.svc.db.query("SELECT summary, transcript FROM axigen_log"))


def test_purge_refuses_a_temporary_hold(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru", retention_days=90)
    ax.svc.db.set_account_hold(acc_id, "2099-01-01", "manual")
    res, _ = action(ax.svc, "purge", [acc_id], days=365)
    assert res["skipped"] == 1 and "удерживается только до" in res["items"][0]["detail"]
    assert not ax.cli.executed("PURGE")
    ax.svc.db.set_account_hold(acc_id, "9999-12-31", "manual")       # бессрочно — можно
    res, _ = action(ax.svc, "purge", [acc_id], days=365)
    assert res["ok"] == 1


def test_purge_failure_in_the_middle_keeps_what_was_done_in_audit(ax):
    acc = ax.world.domains["company.ru"].accounts["ivanov"]
    acc.folders["Spam"] = (1, 512)
    ax.fworld.tree["domains"]["company.ru"]["ivanov"]["Spam"] = {"s.eml": message("Спам")}
    ax.world.fail["PURGE account folders Spam"] = "storage error"
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, _ = action(ax.svc, "purge", [acc_id], days=365, folders="Trash, Spam")
    detail = res["items"][0]["detail"]
    assert res["failed"] == 1 and "Trash" in detail and "storage error" in detail
    assert acc.purges == [("Trash", "365d")]
    audit = ax.svc.db.query_one("SELECT action, detail FROM audit WHERE action='axigen_purge'")
    assert audit is not None and "Trash" in audit["detail"]


def test_change_without_server_confirmation_is_not_trusted(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru", auth_type=AuthType.PASSWORD, password="old")
    ax.world.no_status.append("SET password")
    res, _ = action(ax.svc, "password", [acc_id])
    assert res["failed"] == 1 and "не подтвердил" in res["items"][0]["detail"]
    # пароль в карточке ящика не заменён тем, который сервер, может быть, не принял
    assert ax.svc.db.get_account(acc_id).password == "old"


def test_unknown_service_list_is_not_treated_as_blocked(ax):
    from mailarchiver.axigen.common import services_blocked
    assert services_blocked([]) is None and services_blocked([], blocked_by_us=True) is True
    assert services_blocked(["localDelivery"]) is True and services_blocked(["localDelivery", "remoteDelivery"]) is False
    assert services_blocked(["someNewService"]) is None
    ax.world.domains["company.ru"].accounts["ivanov"].services = ["someNewService"]
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    res, _ = action(ax.svc, "block", [acc_id], final_copy=False)
    assert res["failed"] == 1 and "не понять" in res["items"][0]["detail"]
    res, _ = action(ax.svc, "delete", [acc_id])
    assert res["skipped"] == 1 and "ivanov" in ax.world.domains["company.ru"].accounts


def test_server_action_job_is_not_resumed_after_restart(services):
    job_id = services.db.enqueue_job(JobType.AXIGEN_ACTION, None, {"action": "block", "account_ids": [1]}, 4, 1, "t")
    services.db.execute("UPDATE jobs SET status=? WHERE id=?", (JobStatus.RUNNING, job_id))
    services.db.reset_orphan_jobs()
    job = services.db.get_job(job_id)
    assert job["status"] == JobStatus.FAILED and "само не продолжается" in (job["error"] or "")


def test_connection_weakening_needs_the_password_again():
    from mailarchiver.axigen.common import passwords_to_reenter
    current = {"cli_port": 7443, "ftp_port": 990, "cli_tls": "tls", "ftp_tls": "implicit", "tls_verify": True,
               "allow_plaintext": False, "cli_cert_sha256": "", "ftp_cert_sha256": "", "admin_password": "x",
               "ftp_password": ""}
    old = current.get
    assert passwords_to_reenter(old, {"axigen.cli_tls": "none"})[1] == ["пароль администратора Axigen"]
    assert passwords_to_reenter(old, {"axigen.allow_plaintext": True})[1]
    assert passwords_to_reenter(old, {"axigen.tls_verify": False})[1]
    assert passwords_to_reenter(old, {"axigen.ftp_port": 2121})[1]
    assert passwords_to_reenter(old, {"axigen.cli_cert_sha256": "ab" * 32})[1]
    # с паролем в том же сохранении — можно; усиление защиты пароля не требует
    assert not passwords_to_reenter(old, {"axigen.cli_tls": "none", "axigen.admin_password": "y"})[1]
    weak = dict(current, cli_tls="none", ftp_tls="none", tls_verify=False)
    assert not passwords_to_reenter(weak.get, {"axigen.cli_tls": "tls", "axigen.ftp_tls": "explicit",
                                               "axigen.tls_verify": True})[1]
    # у FTP Backup свой пароль — при ослаблении FTP вводится он
    own = dict(current, ftp_password="f")
    assert passwords_to_reenter(own.get, {"axigen.ftp_tls": "none"})[1] == ["пароль FTP Backup"]


def test_changes_need_permission(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "ivanov@company.ru")
    ax.svc.db.set_settings_many({"axigen.allow_changes": False})
    ax.svc.apply_runtime_settings()
    with pytest.raises(ValidationError):
        action(ax.svc, "block", [acc_id])
    with pytest.raises(ValidationError):
        bulk.run(ax.svc, ADMIN, "axigen_block", [acc_id], {}, preview=True)
    with pytest.raises(ValidationError):
        axjobs.create_account(ax.svc, user="t", email="new@company.ru", password="Passw0rd1")
    assert ax.world.domains["company.ru"].accounts["ivanov"].services[0] == "localDelivery"
    assert not ax.cli.executed("SET services")


def test_mailbox_from_another_server_is_skipped(ax):
    sync(ax.svc)
    other = mailbox(ax.svc, "ivanov@company.ru", host="imap.other.ru", auth_type=AuthType.PASSWORD, password="p")
    res, _ = action(ax.svc, "block", [other])
    assert res["skipped"] == 1 and "другого сервера" in res["items"][0]["detail"]


# ---------------------------------------------------------------------------
#  Групповые действия
# ---------------------------------------------------------------------------
def test_bulk_preview_then_one_job_for_all(ax):
    sync(ax.svc)
    a = mailbox(ax.svc, "ivanov@company.ru")
    b = mailbox(ax.svc, "petrov@company.ru")
    c = mailbox(ax.svc, "ghost@company.ru")
    res = bulk.run(ax.svc, ADMIN, "axigen_block", [a, b, c], {}, preview=True)
    assert res["counts"]["ok"] == 2 and res["counts"]["skip"] == 1
    assert not [j for j in ax.svc.db.active_jobs() if j["type"] == JobType.AXIGEN_ACTION]
    res = bulk.run(ax.svc, ADMIN, "axigen_block", [a, b, c], {}, preview=False, confirm="2")
    jobs = [j for j in ax.svc.db.active_jobs() if j["type"] == JobType.AXIGEN_ACTION]
    assert len(jobs) == 1
    params = json.loads(jobs[0]["params"])
    assert params["action"] == "block" and params["account_ids"] == [a, b]
    assert params["options"]["final_copy"] is True and params["options"]["keep_receiving"] is True


def test_bulk_switch_to_ftp_backup_and_back(ax):
    a = mailbox(ax.svc, "ivanov@company.ru", auth_type=AuthType.PASSWORD, password="p")
    b = mailbox(ax.svc, "ivanov", name="Без адреса", auth_type=AuthType.PASSWORD, password="p")
    res = bulk.run(ax.svc, ADMIN, "axigen_use_ftp", [a, b], {"to": AuthType.AXIGEN_FTP}, preview=False)
    assert res["counts"]["ok"] == 1 and res["counts"]["skip"] == 1
    acc = ax.svc.db.get_account(a)
    assert acc.auth_type == AuthType.AXIGEN_FTP and acc.password == "p"      # пароль не удаляется
    bulk.run(ax.svc, ADMIN, "axigen_use_ftp", [a], {"to": AuthType.PASSWORD}, preview=False)
    assert ax.svc.db.get_account(a).auth_type == AuthType.PASSWORD


def test_bulk_needs_a_sync_first(ax):
    a = mailbox(ax.svc, "ivanov@company.ru")
    with pytest.raises(ValidationError) as err:
        bulk.run(ax.svc, ADMIN, "axigen_block", [a], {}, preview=True)
    assert "Сверки" in err.value.message


def test_dangerous_bulk_actions_need_the_count(ax):
    sync(ax.svc)
    a = mailbox(ax.svc, "ivanov@company.ru")
    axstore.note_blocked(ax.svc.db, "ivanov@company.ru", saved_services=["imap"], user="t", services=[])
    with pytest.raises(ValidationError) as err:
        bulk.run(ax.svc, ADMIN, "axigen_delete", [a], {}, preview=False)
    assert "введите число" in err.value.message
    assert "ivanov" in ax.world.domains["company.ru"].accounts


# ---------------------------------------------------------------------------
#  Учётные записи по одной
# ---------------------------------------------------------------------------
def test_create_account_and_add_it_to_the_archive(ax):
    res = axjobs.create_account(ax.svc, user="t", email="Sidorov@company.ru", password="Passw0rd1",
                                first_name="Сидор", last_name="Сидоров")
    assert res["ok"] and res["mailbox"]["status"] == "ok"
    assert ax.world.domains["company.ru"].accounts["sidorov"].password == "Passw0rd1"
    acc = ax.svc.db.get_account(res["mailbox"]["account_id"])
    assert acc.username == "sidorov@company.ru" and acc.auth_type == AuthType.AXIGEN_FTP
    from mailarchiver.errors import MailArchiverError
    with pytest.raises(MailArchiverError) as err:
        axjobs.create_account(ax.svc, user="t", email="x@other.ru", password="Passw0rd1")
    assert "other.ru" in err.value.message
    ax.svc.db.set_settings_many({"axigen.domains": ["company.ru"]})
    ax.svc.apply_runtime_settings()
    with pytest.raises(ValidationError):
        axjobs.create_account(ax.svc, user="t", email="x@other.ru", password="Passw0rd1")


def test_set_password_updates_imap_mailbox_card(ax):
    sync(ax.svc)
    acc_id = mailbox(ax.svc, "petrov@company.ru", auth_type=AuthType.PASSWORD, password="old")
    res = axjobs.set_account_password(ax.svc, user="t", email="petrov@company.ru", password="Br4ndNew1")
    assert res["saved_in_mailbox"]
    assert ax.svc.db.get_account(acc_id).password == "Br4ndNew1"
    assert ax.world.domains["company.ru"].accounts["petrov"].password == "Br4ndNew1"


# ---------------------------------------------------------------------------
#  Планировщик, сотрудники, настройки
# ---------------------------------------------------------------------------
def test_scheduled_sync_is_not_doubled(ax):
    from mailarchiver.scheduler.scheduler import SchedulerService
    sched = SchedulerService(ax.svc)
    first = sched._fire_axigen_sync()
    assert first and sched._fire_axigen_sync() is None
    assert [j["type"] for j in ax.svc.db.active_jobs()] == [JobType.AXIGEN_SYNC]


def test_employee_mailboxes_can_copy_through_ftp_backup(services):
    from mailarchiver.employees import account_template
    services.db.set_settings_many({"employees.account_host": "mx.company.ru", "employees.account_use_axigen_ftp": True})
    services.apply_runtime_settings()
    assert not account_template(services)["use_axigen_ftp"]          # раздел Axigen выключен
    services.db.set_settings_many({"axigen.enabled": True, "axigen.imap_host": "MX.company.ru"})
    services.apply_runtime_settings()
    assert account_template(services)["use_axigen_ftp"]
    services.db.set_settings_many({"employees.account_host": "imap.other.ru"})
    services.apply_runtime_settings()
    assert not account_template(services)["use_axigen_ftp"]


@pytest.mark.parametrize("key,value,expected", [
    ("axigen.host", " mx.company.ru ", "mx.company.ru"),
    ("axigen.cli_cert_sha256", "ab" * 32, ":".join(["AB"] * 32)),
    ("axigen.domains", "Company.RU, filial.company.ru", ["company.ru", "filial.company.ru"]),
    ("axigen.ftp_account_path", "domains/{domain}/{local}", "domains/{domain}/{local}"),
    ("axigen.ftp_tls", "implicit", "implicit"),
])
def test_settings_are_validated(key, value, expected):
    assert coerce_setting(key, value) == expected


@pytest.mark.parametrize("key,value", [("axigen.host", "https://mx.company.ru"), ("axigen.host", "mx company"),
                                       ("axigen.cli_cert_sha256", "abc"), ("axigen.domains", "not a domain"),
                                       ("axigen.ftp_account_path", "../{local}"), ("axigen.ftp_account_path", "x/y"),
                                       ("axigen.ftp_account_path", "{evil}/{local}"), ("axigen.ftp_tls", "ssl"),
                                       ("axigen.cli_port", 0)])
def test_bad_settings_are_refused(key, value):
    with pytest.raises(ValueError):
        coerce_setting(key, value)


def test_secrets_are_encrypted_in_the_database(services):
    services.db.set_settings_many({"axigen.admin_password": "TopSecret1"})
    raw = services.db.scalar("SELECT value FROM settings WHERE key='axigen.admin_password'")
    assert "TopSecret1" not in str(raw)
    assert services.rt("axigen", "admin_password") == "TopSecret1"
