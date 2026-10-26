"""Общая часть подключений к почтовым серверам: HTTP-клиент, снимок сверки, сопоставление с ящиками архива,
действия раздела «Ящики на сервере» для учётных записей подключений (на учебном драйвере в памяти — со всеми
возможностями, которых нет у Microsoft 365: квота, псевдонимы, передача адреса коллеге), мониторинг."""
import sys
import types

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp
from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import match as msmatch
from mailarchiver.mailservers import monitor
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, ServerAuthError,
                                           ServerError, Status, imap_fields, tls_fields)
from mailarchiver.mailservers.http import ApiLog, HttpClient
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base
from mailarchiver.serverops import targets as tg

ADMIN = {"username": "tester", "role": "admin"}
GB = 1024 ** 3


# ---------------------------------------------------------------------------
#  HTTP-клиент
# ---------------------------------------------------------------------------
@pytest.fixture()
def fake():
    f = FakeHttp()
    yield f
    f.close()


def test_http_refuses_plain_http_to_other_hosts():
    with pytest.raises(ServerError, match="без шифрования"):
        HttpClient("http://mail.company.ru:8080/api")
    HttpClient("http://mail.company.ru:8080/api", allow_plain=True)
    HttpClient("http://127.0.0.1:9/api")


def test_http_errors_retry_and_redaction(fake, monkeypatch):
    monkeypatch.setattr("mailarchiver.mailservers.http.time.sleep", lambda s: None)
    hits = []

    @fake.route("GET", r"/busy")
    def busy(req):
        hits.append(1)
        return (429, {"error": "slow down"}, {"Retry-After": "1"}) if len(hits) < 3 else (200, {"ok": True})

    @fake.route("GET", r"/denied")
    def denied(req):
        return 403, {"error": {"code": "Forbidden", "message": "нет права User.Read.All"}}

    @fake.route("GET", r"/missing")
    def missing(req):
        return 404, {"message": "no such user"}

    @fake.route("POST", r"/login")
    def login(req):
        return 200, {"result": {"token": "SESSION-TOKEN-123", "user": "admin"}}

    api = ApiLog(secrets=["Adm1n-Pass"])
    client = HttpClient(fake.url, api=api, server_label="Тест")
    assert client.get_json("/busy") == {"ok": True} and len(hits) == 3
    with pytest.raises(ServerAuthError, match="нет права User.Read.All"):
        client.get_json("/denied")
    with pytest.raises(NotFound, match="no such user"):
        client.get_json("/missing")
    client.post_json("/login", {"user": "admin", "password": "Adm1n-Pass", "apiKey": "K-1"})
    with pytest.raises(ServerError, match="чужому адресу"):
        client.get_json("http://example.org/x")
    text = api.transcript()
    assert "Adm1n-Pass" not in text and "SESSION-TOKEN-123" not in text and "K-1" not in text
    assert "сервер попросил подождать" in text


def test_http_unreachable_is_russian(fake):
    port = fake.server.server_address[1]
    fake.close()
    with pytest.raises(ServerError, match="нет связи"):
        HttpClient(f"http://127.0.0.1:{port}", timeout=2).get_json("/")
    fake.server = types.SimpleNamespace(shutdown=lambda: None, server_close=lambda: None)


# ---------------------------------------------------------------------------
#  Учебный драйвер в памяти
# ---------------------------------------------------------------------------
class Mem:
    accounts = {}
    fail_test = False


def _acc(email, first, last, *, aliases=(), quota=None, used=None, blocked=False):
    return {"email": email, "first_name": first, "last_name": last, "aliases": list(aliases), "quota_bytes": quota,
            "used_bytes": used, "messages": 10 if used else None, "login_blocked": blocked, "kind": "user"}


class MemDriver(Driver):
    type = "memsrv"
    label = "Учебный сервер"
    summary = "учебный сервер в памяти"
    fields = (Field("host", "Адрес", required=True), Field("admin", "Администратор", required=True),
              Field("admin_password", "Пароль администратора", "password", secret=True, required=True),
              *imap_fields(), *tls_fields())
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.BLOCK, Cap.PASSWORD, Cap.QUOTA, Cap.ALIASES, Cap.CONTACT, Cap.CREATE,
                      Cap.DELETE, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "department", "company", "office")

    def test(self):
        if Mem.fail_test:
            raise ServerError("Учебный сервер: нет связи")
        return Status(version="1.0", details=["учебный"], seats_used=len(Mem.accounts), seats_total=100)

    def list_accounts(self):
        return [dict(a) for a in Mem.accounts.values()]

    def get_account(self, email):
        a = Mem.accounts.get(email)
        return dict(a) if a else None

    def _a(self, email):
        if email not in Mem.accounts:
            raise NotFound(f"нет {email}")
        return Mem.accounts[email]

    def set_login(self, email, enabled, *, keep_receiving=True):
        self._a(email)["login_blocked"] = not enabled
        return ""

    def set_password(self, email, password, *, must_change=False):
        self._a(email)["password"] = password
        return ""

    def set_quota(self, email, quota_bytes):
        self._a(email)["quota_bytes"] = quota_bytes
        return ""

    def add_alias(self, email, alias):
        for other in Mem.accounts.values():
            if alias == other["email"] or alias in other["aliases"]:
                raise ServerError(f"адрес {alias} занят")
        self._a(email)["aliases"].append(alias)
        return list(self._a(email)["aliases"])

    def remove_alias(self, email, alias):
        self._a(email)["aliases"].remove(alias)
        return list(self._a(email)["aliases"])

    def set_contact(self, email, values):
        self._a(email).update(values)
        return ""

    def create_account(self, email, *, first_name, last_name, password, must_change=True, quota_bytes=None,
                       extra=None):
        if email in Mem.accounts:
            raise AlreadyExists(f"{email} уже есть")
        Mem.accounts[email] = _acc(email, first_name, last_name, quota=quota_bytes)
        return dict(Mem.accounts[email])

    def delete_account(self, email):
        Mem.accounts.pop(email)
        return "удалена"

    def imap_auth(self, email):
        return ImapAuth(mech="plain_authzid", user=self.setting("admin"), secret=self.secret("admin_password"),
                        authzid=email, host=self.imap_server()[0])


@pytest.fixture()
def mem(services, monkeypatch):
    mod = types.ModuleType("mem_driver_for_tests")
    mod.DRIVER = MemDriver
    monkeypatch.setitem(sys.modules, "mem_driver_for_tests", mod)
    monkeypatch.setitem(mss.DRIVER_MODULES, "memsrv", "mem_driver_for_tests")
    Mem.fail_test = False
    Mem.accounts = {
        "ivanov@corp.ru": _acc("ivanov@corp.ru", "Иван", "Иванов", aliases=["ivan@corp.ru"], quota=2 * GB, used=GB),
        "petrova@corp.ru": _acc("petrova@corp.ru", "Анна", "Петрова", quota=GB, used=GB - 10),
        "sidorov@corp.ru": _acc("sidorov@corp.ru", "Пётр", "Сидоров", aliases=["support@corp.ru"]),
        "boss@other.ru": _acc("boss@other.ru", "Шеф", "Главный"),
    }
    sid = mss.save(services, "tester", {"type": "memsrv", "name": "Учебный", "host": "mail.corp.ru", "admin": "root",
                                        "admin_password": "R00t-pass", "imap_host": "imap.corp.ru",
                                        "allow_changes": True})
    mss.sync(services, sid, user="tester")
    world = ImapWorld()
    for email in Mem.accounts:
        world.mailbox(email, "x")
        world.add(email, "INBOX", raw_message(f"Письмо для {email}"))
    world.admins["root"] = "R00t-pass"
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return types.SimpleNamespace(svc=services, sid=sid, world=world, key=lambda e: f"ms:{sid}:{e}")


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    return HANDLERS[job_type](JobContext(svc, job_id, job_type, None, params))


def act(svc, action, keys, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": None})
    return res, {i["key"]: i for i in res["items"]}


def preview(svc, action, keys, params=None):
    res = base.run(svc, ADMIN, action, keys, params or {}, preview=True)
    return res, {i["key"]: i for i in res["results"]}


# ---------------------------------------------------------------------------
#  Снимок и сопоставление
# ---------------------------------------------------------------------------
def test_snapshot_new_gone_back(mem):
    db, sid = mem.svc.db, mem.sid
    gone = Mem.accounts.pop("boss@other.ru")
    res = mss.sync(mem.svc, sid)
    assert res["changes"]["gone"] == ["boss@other.ru"]
    assert msstore.get_account(db, sid, "boss@other.ru")["deleted_at"]
    Mem.accounts["boss@other.ru"] = gone
    res = mss.sync(mem.svc, sid)
    assert res["changes"]["back"] == ["boss@other.ru"] and not msstore.get_account(db, sid, "boss@other.ru")["deleted_at"]
    # неполный список — пропавшие не удалёнными
    changes = msstore.save_snapshot(db, sid, [], complete=False)
    assert changes["gone"] == []


def test_match_prefers_explicit_connection_then_host():
    rows = {1: [{"email": "a@x.ru", "aliases": []}], 2: [{"email": "a@x.ru", "aliases": []}]}
    via2 = models.Account(id=10, name="a", host="imap.x.ru", username="a@x.ru", auth_type=AuthType.SERVER)
    via2.server_id = 2
    plain = models.Account(id=11, name="a", host="imap.x.ru", username="a@x.ru", password="p")
    out = msmatch.match(rows, [via2, plain], {1: "imap.x.ru", 2: "other"})
    assert out[(2, "a@x.ru")].id == 10 and out[(1, "a@x.ru")].id == 11
    # ящик через FTP Backup Axigen — не этого сервера
    ftp = models.Account(id=12, name="a", host="imap.x.ru", username="b@x.ru", auth_type=AuthType.AXIGEN_FTP)
    assert msmatch.match({1: [{"email": "b@x.ru", "aliases": []}]}, [ftp], {1: "imap.x.ru"}) == {}
    # псевдоним живой учётной записи, переданный от удалённой, — архив остаётся за удалённой
    rows = {1: [{"email": "new@x.ru", "aliases": ["old@x.ru"]},
                {"email": "old@x.ru", "aliases": [], "deleted_at": "2026-01-01"}]}
    old = models.Account(id=13, name="old", host="imap.x.ru", username="old@x.ru", password="p")
    out = msmatch.match(rows, [old], {1: "imap.x.ru"})
    assert out == {(1, "old@x.ru"): old}


def test_save_validation_and_bound_secrets(mem):
    svc, sid = mem.svc, mem.sid
    with pytest.raises(ValidationError, match="Заполните «Адрес»"):
        mss.save(svc, "tester", {"type": "memsrv", "admin": "root", "admin_password": "x"})
    with pytest.raises(ValidationError, match="Неизвестный вид"):
        mss.save(svc, "tester", {"type": "nosuch"})
    # адрес сервера меняется — пароль администратора нужно ввести заново
    with pytest.raises(ValidationError, match="введите заново"):
        mss.save(svc, "tester", {"host": "evil.example.org", "admin": "root"}, sid)
    mss.save(svc, "tester", {"host": "mail2.corp.ru", "admin": "root", "admin_password": "New-pass1"}, sid)
    conn = msstore.get_server(svc.db, sid)
    assert conn.settings["host"] == "mail2.corp.ru" and conn.secrets["admin_password"] == "New-pass1"
    assert "New-pass1" not in str(conn.public())
    # подключение с ящиками архива не удаляется
    base.run(svc, ADMIN, "ax_add_mailboxes", [mem.key("ivanov@corp.ru")], {}, preview=False)
    with pytest.raises(ValidationError, match="копируются ящики"):
        mss.remove(svc, "tester", sid)


# ---------------------------------------------------------------------------
#  Действия раздела
# ---------------------------------------------------------------------------
def test_quota_rules(mem):
    svc = mem.svc
    iv, pe = mem.key("ivanov@corp.ru"), mem.key("petrova@corp.ru")
    _r, items = preview(svc, "ax_quota", [iv, pe], {"quota_mb": 512, "only_raise": False})
    assert "больше новой квоты" in items[iv]["detail"] and items[iv]["status"] == "skip"
    _r, items = preview(svc, "ax_quota", [iv, pe], {"quota_mb": 1536, "only_raise": True})
    assert items[iv]["status"] == "skip" and "не меньше новой" in items[iv]["detail"]
    assert items[pe]["status"] == "ok" and "1.0 ГБ → 1.5 ГБ" in items[pe]["detail"]
    _r, items = act(svc, "ax_quota", [pe], quota_mb=1536, only_raise=True, threshold=0)
    assert items[pe]["status"] == "success" and Mem.accounts["petrova@corp.ru"]["quota_bytes"] == 1536 * 1024 ** 2
    assert msstore.get_account(svc.db, mem.sid, "petrova@corp.ru")["quota_kb"] == 1536 * 1024


def test_alias_template_collisions(mem):
    svc = mem.svc
    iv, sd = mem.key("ivanov@corp.ru"), mem.key("sidorov@corp.ru")
    Mem.accounts["sidorov@corp.ru"]["first_name"] = "Иван"
    mss.sync(svc, mem.sid)
    _r, items = preview(svc, "ax_alias_add", [iv, sd], {"template": "{first}"})
    # «ivan» уже псевдоним Иванова, а Сидорову получился бы тот же адрес
    assert items[iv]["status"] == "skip" and "уже есть" in items[iv]["detail"]
    assert items[sd]["status"] == "skip" and "занят" in items[sd]["detail"]
    _r, items = act(svc, "ax_alias_add", [iv], template="{f}.{last}")
    assert items[iv]["status"] == "success" and "i.ivanov@corp.ru" in Mem.accounts["ivanov@corp.ru"]["aliases"]
    _r, items = act(svc, "ax_alias_remove", [iv], which="list", aliases="ivan")
    assert items[iv]["status"] == "success" and Mem.accounts["ivanov@corp.ru"]["aliases"] == ["i.ivanov@corp.ru"]
    row = msstore.get_account(svc.db, mem.sid, "ivanov@corp.ru")
    assert row["aliases"] == ["i.ivanov@corp.ru"]


def test_delete_with_transfer_to_colleague(mem):
    svc = mem.svc
    pe, iv, boss = mem.key("petrova@corp.ru"), mem.key("ivanov@corp.ru"), mem.key("boss@other.ru")
    base.run(svc, ADMIN, "ax_add_mailboxes", [pe, boss], {}, preview=False)
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@corp.ru")
    assert acc.auth_type == AuthType.SERVER and acc.host == "imap.corp.ru"
    _r, items = act(svc, "ax_block", [pe, boss], final_copy=True)
    assert items[pe]["status"] == "success", items
    assert mem.world.last_login[0] == "plain_authzid"
    # коллега — в другом домене: не удаляем (письма на адрес иначе пошли бы в отказ)
    _r, items = preview(svc, "ax_delete", [pe], {"forward_to": "boss@other.ru"})
    assert items[pe]["status"] == "skip" and "в другом домене" in items[pe]["detail"]
    _r, items = preview(svc, "ax_delete", [pe], {"forward_to": "nobody@corp.ru"})
    assert "нет на сервере" in items[pe]["detail"]
    res = base.run(svc, ADMIN, "ax_delete", [pe], {"forward_to": "ivanov@corp.ru"}, preview=True)
    assert res["counts"]["ok"] == 1 and "petrova@corp.ru → ivanov@corp.ru" in res["results"][0]["detail"]
    _r, items = act(svc, "ax_delete", [pe], forward_to="ivanov@corp.ru", forward_aliases=True)
    assert items[pe]["status"] == "success", items
    assert "petrova@corp.ru" not in Mem.accounts and "petrova@corp.ru" in Mem.accounts["ivanov@corp.ru"]["aliases"]
    acc = svc.db.get_account(acc.id)
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    # архив удалённой остаётся за ней, а не за коллегой с её адресом-псевдонимом
    mss.sync(svc, mem.sid)
    items = {t.key: t for t in tg.load(svc)}
    assert items[pe].deleted and items[pe].mailbox is not None and items[pe].mailbox.id == acc.id
    assert items[iv].mailbox is None
    # повторно передать уже нечего
    _r, items = preview(svc, "ax_forward", [pe], {"to": "ivanov@corp.ru"})
    assert "уже переданы" in items[pe]["detail"]


def test_forward_after_deletion_and_failed_transfer_offers_followup(mem, monkeypatch):
    svc = mem.svc
    sd = mem.key("sidorov@corp.ru")
    base.run(svc, ADMIN, "ax_add_mailboxes", [sd], {}, preview=False)
    act(svc, "ax_block", [sd], final_copy=True)

    def broken(self, email, alias):
        raise ServerError("сервер занят")
    original = MemDriver.add_alias
    monkeypatch.setattr(MemDriver, "add_alias", broken)
    _r, items = act(svc, "ax_delete", [sd], forward_to="ivanov@corp.ru", forward_aliases=True)
    assert items[sd]["status"] == "failed" and items[sd]["next_action"] == "ax_forward"
    assert "уже удалена" in items[sd]["detail"] and "sidorov@corp.ru" not in Mem.accounts
    monkeypatch.setattr(MemDriver, "add_alias", original)
    _r, items = act(svc, "ax_forward", [sd], to="ivanov@corp.ru", with_aliases=True)
    assert items[sd]["status"] == "success", items
    assert {"sidorov@corp.ru", "support@corp.ru"} <= set(Mem.accounts["ivanov@corp.ru"]["aliases"])


def test_axigen_disabled_still_allows_connection_actions(mem):
    svc = mem.svc
    desc = {a["key"]: a for a in base.describe_actions(svc)["actions"]}
    assert desc["ax_block"]["unavailable"] == "" and desc["srv_autoreply"]["unavailable"] == ""
    assert "Axigen" in desc["ax_account_type"]["unavailable"]           # только Axigen
    assert {g["label"] for g in base.describe_actions(svc)["groups"]} >= {"Вход и пароли", "Учётные записи"}
    # ящик архива без учётной записи на сервере — понятная причина
    acc_id = svc.db.create_account(models.Account(name="x", host="imap.elsewhere.ru", username="x@elsewhere.ru",
                                                  password="p"))
    _r, items = preview(svc, "ax_block", [f"mb:{acc_id}"], {"final_copy": False})
    assert "ни на Axigen, ни на подключённом" in items[f"mb:{acc_id}"]["detail"]
    # автоответ этот учебный сервер не умеет
    _r, items = preview(svc, "srv_autoreply", [mem.key("ivanov@corp.ru")], {"mode": "off"})
    assert "автоответ — этот сервер так не умеет" in items[mem.key("ivanov@corp.ru")]["detail"]


def test_create_on_connection_and_already_exists(mem, monkeypatch):
    svc = mem.svc
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    from mailarchiver.serverops import create
    params = {"server_id": mem.sid, "text": "new@corp.ru; Новый; Человек\nivanov@corp.ru", "password_mode": "same",
              "password": "Common-Pass1", "must_change": False, "quota_mb": 100, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    assert [i["status"] for i in pv["items"]] == ["ok", "skip"]
    create.run(svc, ADMIN, params, preview=False)
    Mem.accounts["new@corp.ru"] = _acc("new@corp.ru", "Кто-то", "Успел")      # создали вручную, пока ждали
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["skipped"] == 1 and "уже есть" in out["items"][0]["detail"]
    Mem.accounts.pop("new@corp.ru")
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1 and Mem.accounts["new@corp.ru"]["quota_bytes"] == 100 * 1024 ** 2
    assert any(a.username == "new@corp.ru" and a.auth_type == AuthType.SERVER for a in svc.db.list_accounts())


def test_mailbox_host_must_match_connection(mem):
    svc = mem.svc
    acc = models.Account(name="x", host="evil.example.org", username="ivanov@corp.ru", auth_type=AuthType.SERVER)
    acc.server_id = mem.sid
    acc.id = svc.db.create_account(acc)
    with pytest.raises(ServerError, match="входят только на imap.corp.ru"):
        mss.imap_auth_for(svc, svc.db.get_account(acc.id))


def test_monitor_problems_and_metrics(mem):
    svc, sid = mem.svc, mem.sid
    base.run(svc, ADMIN, "ax_add_mailboxes", [mem.key("petrova@corp.ru")], {}, preview=False)
    # вход закрыт, а ящик копируется по паролю сотрудника
    svc.db.create_account(models.Account(name="Сидоров", host="imap.corp.ru", username="sidorov@corp.ru",
                                         password="p"))
    Mem.accounts["sidorov@corp.ru"]["login_blocked"] = True
    mss.sync(svc, sid)
    kinds = {p["kind"]: p for p in monitor.problems(svc)}
    assert "quota" in kinds and "petrova@corp.ru" in kinds["quota"]["text"]        # занято > 90 %
    assert "blocked_imap" in kinds and "sidorov@corp.ru" in kinds["blocked_imap"]["text"]
    assert "unarchived" in kinds
    # удалена на сервере, а копирование включено
    Mem.accounts.pop("petrova@corp.ru")
    mss.sync(svc, sid)
    kinds = {p["kind"]: p for p in monitor.problems(svc)}
    assert "gone" in kinds
    # сервер не отвечает две проверки подряд
    Mem.fail_test = True
    mss.check(svc, sid)
    mss.check(svc, sid)
    kinds = {p["kind"]: p for p in monitor.problems(svc)}
    assert "unreachable" in kinds and "нет связи" in kinds["unreachable"]["text"]
    names = {m[0] for m in monitor.metrics(svc)}
    assert "mailarchiver_mailserver_up" in names and "mailarchiver_mailserver_problems" in names
    # без проверки сертификата и без отпечатка — предупреждение
    conn = msstore.get_server(svc.db, sid)
    msstore.set_setting(svc.db, sid, "verify_tls", False)
    assert any(p["kind"] == "tls_unverified" for p in monitor.problems(svc))


def test_sync_add_new_creates_mailboxes(mem):
    svc, sid = mem.svc, mem.sid
    conn = msstore.get_server(svc.db, sid)
    msstore.update_server(svc.db, sid, name=conn.name, settings=conn.settings, secrets=conn.secrets,
                          sync_cron=conn.sync_cron, enabled=True, allow_changes=True, sync_enabled=True,
                          sync_add_new=True, monitor=True)
    Mem.accounts["fresh@corp.ru"] = _acc("fresh@corp.ru", "Свежий", "Сотрудник")
    res = run_job(svc, JobType.SERVER_SYNC, {"server_id": sid, "requested_by": "tester"})
    assert res["new"] == 1 and res["added"] == 1
    acc = next(a for a in svc.db.list_accounts() if a.username == "fresh@corp.ru")
    assert acc.auth_type == AuthType.SERVER and acc.server_id == sid and acc.name == "Свежий Сотрудник"


def test_employee_mailboxes_use_matching_connection(mem):
    from mailarchiver import employees
    svc = mem.svc
    svc.db.set_settings_many({"employees.account_host": "imap.corp.ru", "employees.create_accounts": True})
    svc.apply_runtime_settings()
    tpl = employees.account_template(svc)
    assert tpl["server_id"] == mem.sid
    emp = svc.db.create_employee(full_name="Новый Сотрудник", email="newbie@corp.ru")
    acc_id, action = employees.ensure_account_for_employee(svc, emp, "Новый Сотрудник", "newbie@corp.ru", tpl=tpl)
    acc = svc.db.get_account(acc_id)
    assert action == "created" and acc.auth_type == AuthType.SERVER and acc.server_id == mem.sid
    # другой сервер — по паролю, как раньше
    svc.db.set_settings_many({"employees.account_host": "imap.other.ru"})
    svc.apply_runtime_settings()
    assert employees.account_template(svc)["server_id"] is None


# ---------------------------------------------------------------------------
#  Найденное при проверке: повторы, частичные изменения, удаление, сверка, IMAP
# ---------------------------------------------------------------------------
def test_http_does_not_repeat_writes_on_gateway_errors(fake, monkeypatch):
    monkeypatch.setattr("mailarchiver.mailservers.http.time.sleep", lambda s: None)
    hits = {"POST": 0, "GET": 0, "RPC": 0}

    @fake.route("POST", r"/users")
    def create(req):
        hits["POST"] += 1
        return 504, {"error": "gateway timeout"}

    @fake.route("GET", r"/users")
    def read(req):
        hits["GET"] += 1
        return (504, {"error": "gateway timeout"}) if hits["GET"] < 2 else (200, {"ok": True})

    @fake.route("POST", r"/rpc")
    def rpc(req):
        hits["RPC"] += 1
        return (503, {"error": "busy"}) if hits["RPC"] < 2 else (200, {"ok": True})

    client = HttpClient(fake.url, api=ApiLog(), server_label="Тест")
    with pytest.raises(ServerError):
        client.post_json("/users", {"email": "a@b.ru"})
    assert hits["POST"] == 1                                     # создание не повторяется: сервер мог его выполнить
    assert client.get_json("/users") == {"ok": True} and hits["GET"] == 2
    assert client.post_json("/rpc", {"method": "Users.get"}, idempotent=True) == {"ok": True}


def test_log_masks_escaped_secrets_and_markup(fake):
    @fake.route("POST", r"/xml")
    def xml(req):
        return 200, {"ok": True}

    secret = "Secret&Pass<1>"
    api = ApiLog(secrets=[secret])
    client = HttpClient(fake.url, api=api, server_label="Тест")
    body = ("<CreateUser><Password>Secret&amp;Pass&lt;1&gt;</Password><Other>x</Other></CreateUser>"
            "{ Password = \"Another1\"; RealName = \"Иван\"; }").encode()
    client.request("POST", "/xml", data=body, log_url="https://example/xml (ссылка скрыта)")
    text = api.transcript()
    assert "Secret&amp;Pass" not in text and "Another1" not in text and "Иван" in text
    assert "(ссылка скрыта)" in text and fake.url not in text


def test_flags_from_strings_are_not_true(mem):
    mss.save(mem.svc, "tester", {"allow_changes": "false", "admin": "root"}, mem.sid)
    assert msstore.get_server(mem.svc.db, mem.sid).allow_changes is False


def test_sync_empty_or_halved_list_is_not_mass_deletion(mem):
    svc, sid = mem.svc, mem.sid
    saved = dict(Mem.accounts)
    Mem.accounts.clear()
    res = mss.sync(svc, sid)
    assert res["incomplete"] and res["changes"]["gone"] == [] and "вместо 4" in res["summary"]
    assert all(not r.get("deleted_at") for r in msstore.list_accounts(svc.db, sid))
    # вторая сверка с тем же итогом — правда: все удалены
    res = mss.sync(svc, sid)
    assert not res["incomplete"] and len(res["changes"]["gone"]) == 4
    Mem.accounts.update(saved)
    mss.sync(svc, sid)


def test_reread_ignores_other_account_found_by_alias(mem, monkeypatch):
    svc = mem.svc
    original = MemDriver.get_account

    def by_alias(self, email):
        for a in Mem.accounts.values():
            if email in a["aliases"]:
                return dict(a)
        return original(self, email)
    monkeypatch.setattr(MemDriver, "get_account", by_alias)
    pe = mem.key("petrova@corp.ru")
    Mem.accounts.pop("petrova@corp.ru")
    Mem.accounts["ivanov@corp.ru"]["aliases"].append("petrova@corp.ru")
    _r, items = act(svc, "ax_refresh", [pe])
    assert "больше нет" in items[pe]["detail"]
    assert msstore.get_account(svc.db, mem.sid, "petrova@corp.ru")["deleted_at"]
    assert msstore.get_account(svc.db, mem.sid, "ivanov@corp.ru")["email"] == "ivanov@corp.ru"


def test_block_then_password_failure_is_reported_and_audited(mem, monkeypatch):
    svc = mem.svc
    pe = mem.key("petrova@corp.ru")

    def broken(self, email, password, *, must_change=False):
        raise ServerError("пароль не принят политикой")
    monkeypatch.setattr(MemDriver, "set_password", broken)
    _r, items = act(svc, "ax_block", [pe], final_copy=False, random_password=True)
    assert items[pe]["status"] == "failed"
    assert "вход закрыт" in items[pe]["detail"] and "пароль не принят" in items[pe]["detail"]
    assert Mem.accounts["petrova@corp.ru"]["login_blocked"] is True
    audit = [a["detail"] for a in svc.db.list_audit(limit=20) if "petrova@corp.ru" in (a["detail"] or "")]
    assert any("частично" in d for d in audit)


def test_password_not_saved_into_mailbox_of_another_server(mem):
    svc = mem.svc
    pe = mem.key("petrova@corp.ru")
    old = models.Account(name="Петрова (старый сервер)", host="old.corp.ru", username="petrova@corp.ru",
                         password="OldPass-1")
    old.id = svc.db.create_account(old)
    _r, items = act(svc, "ax_password", [pe], mode="random_hidden")
    assert items[pe]["status"] == "success" and "НЕ сохранён" in items[pe]["detail"]
    assert svc.db.get_account(old.id).password == "OldPass-1"
    # и удалять по ящику со старого сервера нельзя: полноту архива проверить нечем
    Mem.accounts["petrova@corp.ru"]["login_blocked"] = True
    mss.sync(svc, mem.sid)
    _r, items = preview(svc, "ax_delete", [pe])
    assert items[pe]["status"] == "skip"


def _blocked_with_mailbox(mem, email):
    svc = mem.svc
    key = mem.key(email)
    base.run(svc, ADMIN, "ax_add_mailboxes", [key], {}, preview=False)
    _r, items = act(svc, "ax_block", [key], final_copy=True)
    assert items[key]["status"] == "success", items
    return key, next(a for a in svc.db.list_accounts() if a.username == email)


def test_delete_stops_when_mail_arrives_after_coverage(mem, monkeypatch):
    from mailarchiver.serverops import server_accounts as sa
    svc = mem.svc
    pe, acc = _blocked_with_mailbox(mem, "petrova@corp.ru")
    original = sa.imap_coverage

    def coverage_then_new_mail(ctx, account, emit, state=None):
        out = original(ctx, account, emit, state)
        mem.world.add("petrova@corp.ru", "INBOX", raw_message("Пришло во время сверки"))
        return out
    monkeypatch.setattr(sa, "imap_coverage", coverage_then_new_mail)
    _r, items = act(svc, "ax_delete", [pe])
    assert items[pe]["status"] == "skipped" and "новые письма" in items[pe]["detail"]
    assert "petrova@corp.ru" in Mem.accounts
    assert svc.db.get_account(acc.id).hold_until != "9999-12-31"        # удержание вернулось


def test_delete_error_after_server_deleted_counts_as_deleted(mem, monkeypatch):
    svc = mem.svc
    pe, acc = _blocked_with_mailbox(mem, "petrova@corp.ru")

    def deleted_but_504(self, email):
        Mem.accounts.pop(email)
        raise ServerError("ошибка сервера (504)")
    monkeypatch.setattr(MemDriver, "delete_account", deleted_but_504)
    _r, items = act(svc, "ax_delete", [pe])
    assert items[pe]["status"] == "success" and "больше нет" in items[pe]["detail"]
    fresh = svc.db.get_account(acc.id)
    assert fresh.hold_until == "9999-12-31" and fresh.enabled is False
    assert msstore.get_account(svc.db, mem.sid, "petrova@corp.ru")["deleted_at"]


def test_delete_unknown_result_keeps_permanent_hold(mem, monkeypatch):
    svc = mem.svc
    pe, acc = _blocked_with_mailbox(mem, "petrova@corp.ru")
    state = {"deleting": False}
    original = MemDriver.get_account

    def delete_lost(self, email):
        state["deleting"] = True
        raise ServerError("нет ответа")

    def get_after(self, email):
        if state["deleting"]:
            raise ServerError("нет связи")
        return original(self, email)
    monkeypatch.setattr(MemDriver, "delete_account", delete_lost)
    monkeypatch.setattr(MemDriver, "get_account", get_after)
    _r, items = act(svc, "ax_delete", [pe])
    assert items[pe]["status"] == "failed" and "результат неизвестен" in items[pe]["detail"]
    assert svc.db.get_account(acc.id).hold_until == "9999-12-31"


def test_transfer_refused_while_account_still_exists(mem):
    svc = mem.svc
    sd = mem.key("sidorov@corp.ru")
    # снимок отстал: считает учётную запись удалённой, а на сервере она есть
    msstore.note_deleted(svc.db, mem.sid, "sidorov@corp.ru")
    _r, items = act(svc, "ax_forward", [sd], to="ivanov@corp.ru", with_aliases=True)
    assert items[sd]["status"] == "skipped" and "есть" in items[sd]["detail"]
    assert "sidorov@corp.ru" not in Mem.accounts["ivanov@corp.ru"]["aliases"]
    assert not msstore.get_account(svc.db, mem.sid, "sidorov@corp.ru")["deleted_at"]


def test_autoreply_rejects_impossible_date_and_forwarding_off_always_allowed(mem):
    svc = mem.svc
    iv = mem.key("ivanov@corp.ru")
    with pytest.raises(ValidationError, match="Такой даты нет"):
        base.run(svc, ADMIN, "srv_autoreply", [iv], {"mode": "on", "text": "В отпуске", "start": "2026-02-30"},
                 preview=True)
    # снимок без пересылки ([] может значить «неизвестно») — выключить всё равно можно
    msstore.update_fields(svc.db, mem.sid, "ivanov@corp.ru", forward_to=[])
    _r, items = preview(svc, "srv_forwarding", [iv], {"mode": "off"})
    assert "пересылки и так нет" not in items[iv]["detail"]


def test_plain_imap_and_untrusted_certificate_do_not_get_admin_secret(mem):
    svc = mem.svc
    acc = models.Account(name="x", host="imap.corp.ru", username="ivanov@corp.ru", auth_type=AuthType.SERVER,
                         security="plain", port=143)
    acc.server_id = mem.sid
    with pytest.raises(ServerError, match="без шифрования"):
        mss.imap_auth_for(svc, acc)
    assert "без шифрования" in mss.imap_problem(svc, acc)
    # сертификат: по настройкам подключения, а не по общей настройке; отпечаток — для того же адреса
    tls = mss.imap_tls_for(svc, acc)
    assert tls == {"verify": True, "pin": ""}
    msstore.set_setting(svc.db, mem.sid, "cert_sha256", "ab" * 32)
    msstore.set_setting(svc.db, mem.sid, "imap_host", "mail.corp.ru")
    acc.host = "mail.corp.ru"
    assert mss.imap_tls_for(svc, acc)["pin"] == "ab" * 32
    conn = client_mod.ImapConnection(acc, client_mod.ConnectOptions(server_tls=lambda a: {"verify": True,
                                                                                          "pin": "ab" * 32}))
    ctx = conn._ssl_context()
    import ssl
    assert ctx.verify_mode == ssl.CERT_NONE                       # проверка — по отпечатку после рукопожатия
    conn.client = types.SimpleNamespace(_imap=types.SimpleNamespace(
        sock=types.SimpleNamespace(getpeercert=lambda binary_form=True: b"other-cert")))
    from mailarchiver.errors import ImapConnectionError
    with pytest.raises(ImapConnectionError, match="не тот"):
        conn._check_pin()


def test_add_mailboxes_skips_system_accounts(mem):
    svc = mem.svc
    Mem.accounts["spam.x@corp.ru"] = dict(_acc("spam.x@corp.ru", "", ""), kind="system")
    mss.sync(svc, mem.sid)
    from mailarchiver.mailservers.mailboxes import add_mailboxes
    items = add_mailboxes(svc, msstore.get_server(svc.db, mem.sid), ["spam.x@corp.ru"], user="tester")
    assert items[0]["status"] == "skip" and "служебная" in items[0]["text"]


def test_employee_template_with_two_connections_on_same_imap_host(mem):
    from mailarchiver import employees
    svc = mem.svc
    sid2 = mss.save(svc, "tester", {"type": "memsrv", "name": "Второй клиент", "host": "mail.other.ru",
                                    "admin": "root", "admin_password": "R00t-pass", "imap_host": "imap.corp.ru"})
    mss.sync(svc, sid2)          # в снимке второго — boss@other.ru и прочие из учебного сервера
    msstore.save_snapshot(svc.db, sid2, [_acc("boss2@second.ru", "Шеф", "Второй")], complete=True)
    svc.db.set_settings_many({"employees.account_host": "imap.corp.ru", "employees.create_accounts": True})
    svc.apply_runtime_settings()
    tpl = employees.account_template(svc)
    assert set(tpl["server_ids"]) == {mem.sid, sid2}
    emp = svc.db.create_employee(full_name="Новый", email="newbie@second.ru")
    acc_id, _a = employees.ensure_account_for_employee(svc, emp, "Новый", "newbie@second.ru", tpl=tpl)
    assert svc.db.get_account(acc_id).server_id == sid2
    emp = svc.db.create_employee(full_name="Непонятный", email="x@nowhere.ru")
    acc_id, _a = employees.ensure_account_for_employee(svc, emp, "Непонятный", "x@nowhere.ru", tpl=tpl)
    assert svc.db.get_account(acc_id).auth_type == AuthType.PASSWORD      # не угадываем подключение
