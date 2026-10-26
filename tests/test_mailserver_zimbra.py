"""Подключение Zimbra / Carbonio: учебный SOAP администратора (JSON) и IMAP входом администратора «от имени».

Учебный сервер разбирает настоящие конверты JSON SOAP (Header.context.authToken, Body.<Имя>Request с _jsns
urn:zimbraAdmin), отвечает <Имя>Response или Fault с кодом 500 — как Zimbra. Проверяется весь путь: подключение и
проверка, сверка (страницы поиска, занятое место, последний вход), истечение токена, действия раздела через драйвер,
удаление только при полном архиве и передача адреса коллеге, создание учётных записей и то, что ни пароль
администратора, ни токен не попадают в журнал подключения.
"""
import secrets as _secrets

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp, connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, Cap, NotFound, ServerAuthError, ServerError
from mailarchiver.mailservers.drivers import zimbra as zmod
from mailarchiver.mailservers.drivers.zimbra import ZimbraDriver, gentime_iso, parse_host
from mailarchiver.mailservers.http import TOKENS, ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
ADMIN_LOGIN = "backup-admin@corp.ru"
ADMIN_PW = "Zm-Adm1n~Secret"
GB = 1024 ** 3


def _v(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("_content")
    return "" if value is None else str(value)


def ok(name, body):
    return 200, {"Header": {"context": {"_jsns": "urn:zimbra"}},
                 "Body": {f"{name}Response": dict(body, _jsns="urn:zimbraAdmin")}, "_jsns": "urn:zimbraSoap"}, \
        {"Content-Type": "text/javascript; charset=utf-8"}


def fault(code, text):
    return 500, {"Header": {"context": {"_jsns": "urn:zimbra"}},
                 "Body": {"Fault": {"Code": {"Value": "soap:Sender"}, "Reason": {"Text": text},
                                    "Detail": {"Error": {"Code": code, "Trace": "qtp1-42:1700000000000",
                                                         "_jsns": "urn:zimbra"}},
                                    "_jsns": "urn:zimbraSoap"}}}, {"Content-Type": "text/javascript; charset=utf-8"}


class Zimbra:
    """Состояние учебного сервера Zimbra (каталог LDAP + ящики)."""

    STATUSES = ("active", "locked", "closed", "maintenance", "pending", "lockout")

    def __init__(self):
        self.accounts = {}
        self.domains = ["corp.ru"]
        self.tokens = set()
        self.expired = set()
        self.always_expired = False
        self.auth_calls = 0
        self.requests = []
        self.passwords = {}
        self.version = "8.8.15_GA_4581"
        self.license = True
        self.too_many = False
        self.denied = set()
        self.next_id = 1
        self.add("ivanov@corp.ru", "Иван", "Иванов", used=GB, zimbraMailAlias=["ivan@corp.ru"],
                 zimbraMailQuota=str(2 * GB), zimbraLastLogonTimestamp="20261020080000Z", ou="Продажи",
                 title="Менеджер", telephoneNumber="+7 495 000-00-01")
        self.add("petrova@corp.ru", "Анна", "Петрова", used=10 * 1024 * 1024, zimbraMailQuota="0")
        self.add("sidorov@corp.ru", "Пётр", "Сидоров", zimbraAccountStatus="locked")
        self.add(ADMIN_LOGIN, "Архив", "Админ", zimbraIsAdminAccount="TRUE")
        self.add("galsync@corp.ru", "", "", zimbraIsSystemResource="TRUE")

    # -- каталог ----------------------------------------------------------------------------
    def add(self, email, first, last, *, used=0, **attrs):
        zid = f"00000000-0000-0000-0000-{self.next_id:012d}"
        self.next_id += 1
        base_attrs = {"zimbraId": [zid], "zimbraAccountStatus": ["active"], "zimbraMailHost": ["mail.corp.ru"],
                      "zimbraCreateTimestamp": ["20240101000000Z"], "zimbraFeatureMailForwardingEnabled": ["TRUE"],
                      "zimbraFeatureOutOfOfficeReplyEnabled": ["TRUE"]}
        if first:
            base_attrs["givenName"] = [first]
        if last:
            base_attrs["sn"] = [last]
        if first or last:
            base_attrs["displayName"] = [f"{first} {last}".strip()]
        for k, v in attrs.items():
            base_attrs[k] = list(v) if isinstance(v, list) else [v]
        self.accounts[zid] = {"name": email, "id": zid, "attrs": base_attrs, "used": used}
        return self.accounts[zid]

    def by_name(self, name):
        name = name.lower()
        for acc in self.accounts.values():
            if acc["name"] == name or name in acc["attrs"].get("zimbraMailAlias", []):
                return acc
        return None

    def primary(self, name):
        return next((a for a in self.accounts.values() if a["name"] == name.lower()), None)

    def attr(self, email, name):
        return (self.primary(email)["attrs"].get(name) or [""])[0]

    def view(self, acc, wanted=None):
        items = []
        for k, values in acc["attrs"].items():
            if wanted is not None and k not in wanted:
                continue
            if k in self.denied:
                items.append({"n": k, "pd": True})
                continue
            items.extend({"n": k, "_content": v} for v in values)
        return {"name": acc["name"], "id": acc["id"], "a": items}

    def taken(self, address):
        return any(a["name"] == address or address in a["attrs"].get("zimbraMailAlias", [])
                   for a in self.accounts.values())

    # -- запросы ----------------------------------------------------------------------------
    def do_GetVersionInfo(self, r):
        return ok("GetVersionInfo", {"info": [{"version": self.version, "release": "20260901120000",
                                               "buildDate": "20260901-1200", "host": "mail.corp.ru",
                                               "majorversion": "8", "minorversion": "8", "microversion": "15",
                                               "platform": "UBUNTU20_64", "type": "NETWORK"}]})

    def do_GetLicense(self, r):
        if not self.license:
            return fault("service.UNKNOWN_DOCUMENT", "unknown document: GetLicenseRequest")
        return ok("GetLicense", {
            "license": [{"a": [{"n": "AccountsLimit", "_content": "100"},
                               {"n": "ValidUntil", "_content": "20271231000000Z"}]}],
            "activation": [{"a": [{"n": "ActivationId", "_content": "act-1"}]}],
            "info": [{"a": [{"n": "TotalAccounts", "_content": "4"}, {"n": "Status", "_content": "OK"},
                            {"n": "Version", "_content": "2"}]}]})

    def do_GetAllServers(self, r):
        return ok("GetAllServers", {"server": [
            {"name": "mail.corp.ru", "id": "srv-1", "a": [{"n": "zimbraServiceEnabled", "_content": "mailbox"},
                                                         {"n": "zimbraServiceEnabled", "_content": "mta"}]},
            {"name": "ldap.corp.ru", "id": "srv-2", "a": [{"n": "zimbraServiceEnabled", "_content": "ldap"}]}]})

    def do_GetMailQueueInfo(self, r):
        name = r["server"]["name"]
        assert name == "mail.corp.ru"          # только серверы со службой mta
        return ok("GetMailQueueInfo", {"server": [{"name": name, "queue": [
            {"name": "deferred", "n": 3}, {"name": "incoming", "n": 0}, {"name": "active", "n": 1},
            {"name": "hold", "n": 0}, {"name": "corrupt", "n": 0}]}]})

    def do_GetAllDomains(self, r):
        return ok("GetAllDomains", {"domain": [{"name": d, "id": f"dom-{d}"} for d in self.domains]})

    def _paged(self, r, items):
        limit, offset = int(r.get("limit") or 0), int(r.get("offset") or 0)
        page = items[offset:offset + limit] if limit else items[offset:]
        return page, offset + len(page) < len(items)

    def _in(self, r, acc):
        return not r.get("domain") or acc["name"].endswith("@" + r["domain"])

    def do_SearchDirectory(self, r):
        assert r.get("types") == "accounts"
        if self.too_many and not r.get("domain"):
            return fault("account.TOO_MANY_SEARCH_RESULTS", "too many search results returned")
        items = sorted((a for a in self.accounts.values() if self._in(r, a)), key=lambda a: a["name"])
        wanted = set(r["attrs"].split(",")) if r.get("attrs") else None
        page, more = self._paged(r, items)
        return ok("SearchDirectory", {"account": [self.view(a, wanted) for a in page], "more": more,
                                      "searchTotal": len(items), "num": len(page)})

    def do_GetQuotaUsage(self, r):
        items = sorted((a for a in self.accounts.values() if self._in(r, a)), key=lambda a: -a["used"])
        page, more = self._paged(r, items)
        return ok("GetQuotaUsage", {"account": [{"name": a["name"], "id": a["id"], "used": a["used"],
                                                 "limit": int((a["attrs"].get("zimbraMailQuota") or ["0"])[0])}
                                                for a in page], "more": more, "searchTotal": len(items)})

    def do_GetAccount(self, r):
        acc = self.by_name(_v(r["account"]))
        if acc is None:
            return fault("account.NO_SUCH_ACCOUNT", f"no such account: {_v(r['account'])}")
        wanted = set(r["attrs"].split(",")) if r.get("attrs") else None
        return ok("GetAccount", {"account": [self.view(acc, wanted)]})

    def do_GetMailbox(self, r):
        acc = self.accounts.get(r["mbox"]["id"])
        if acc is None:
            return fault("account.NO_SUCH_ACCOUNT", f"no such account: {r['mbox']['id']}")
        return ok("GetMailbox", {"mbox": [{"mbxid": int(acc["id"][-4:]), "s": acc["used"]}]})

    def _by_id(self, r):
        return self.accounts.get(_v(r.get("id")))

    def do_ModifyAccount(self, r):
        acc = self._by_id(r)
        if acc is None:
            return fault("account.NO_SUCH_ACCOUNT", f"no such account: {_v(r.get('id'))}")
        changes = {}
        for item in r["a"]:
            changes.setdefault(item["n"], []).append(item.get("_content", ""))
        status = changes.get("zimbraAccountStatus")
        if status and status[0] not in self.STATUSES:
            return fault("service.INVALID_REQUEST", f"invalid request: zimbraAccountStatus {status[0]}")
        for name, values in changes.items():
            values = [v for v in values if v != ""]
            if values:
                acc["attrs"][name] = values
            else:
                acc["attrs"].pop(name, None)
        return ok("ModifyAccount", {"account": [self.view(acc)]})

    def do_SetPassword(self, r):
        acc = self._by_id(r)
        self.passwords[acc["name"]] = _v(r["newPassword"])
        return ok("SetPassword", {})

    def do_AddAccountAlias(self, r):
        acc, alias = self._by_id(r), _v(r["alias"]).lower()
        if alias.split("@")[1] not in self.domains:
            return fault("account.NO_SUCH_DOMAIN", f"no such domain: {alias.split('@')[1]}")
        if self.taken(alias):
            return fault("account.ALIAS_EXISTS", f"email address already exists: {alias}")
        acc["attrs"].setdefault("zimbraMailAlias", []).append(alias)
        return ok("AddAccountAlias", {})

    def do_RemoveAccountAlias(self, r):
        acc, alias = self._by_id(r), _v(r["alias"]).lower()
        if alias not in acc["attrs"].get("zimbraMailAlias", []):
            return fault("account.NO_SUCH_ALIAS", f"no such alias: {alias}")
        acc["attrs"]["zimbraMailAlias"].remove(alias)
        if not acc["attrs"]["zimbraMailAlias"]:
            acc["attrs"].pop("zimbraMailAlias")          # в LDAP пустой атрибут не хранится
        return ok("RemoveAccountAlias", {})

    def do_CreateAccount(self, r):
        name = _v(r["name"]).lower()
        if name.split("@")[1] not in self.domains:
            return fault("account.NO_SUCH_DOMAIN", f"no such domain: {name.split('@')[1]}")
        if self.taken(name):
            return fault("account.ACCOUNT_EXISTS", f"email address already exists: {name}")
        attrs = {}
        for item in r.get("a") or []:
            attrs.setdefault(item["n"], []).append(item["_content"])
        acc = self.add(name, "", "", **attrs)
        self.passwords[name] = _v(r["password"])
        return ok("CreateAccount", {"account": [self.view(acc)]})

    def do_DeleteAccount(self, r):
        if self.accounts.pop(_v(r["id"]), None) is None:
            return fault("account.NO_SUCH_ACCOUNT", f"no such account: {_v(r['id'])}")
        return ok("DeleteAccount", {})


@pytest.fixture()
def zimbra(monkeypatch):
    TOKENS._tokens.clear()
    z = Zimbra()
    fake = FakeHttp()

    @fake.route("POST", r"/service/admin/soap")
    def soap(req):
        env = req.json()
        assert env["Header"]["context"]["_jsns"] == "urn:zimbra"
        name = next(k for k in env["Body"] if k.endswith("Request"))[:-len("Request")]
        r = env["Body"][name + "Request"]
        assert r.pop("_jsns") == "urn:zimbraAdmin"
        z.requests.append(name)
        if name == "Auth":
            z.auth_calls += 1
            if _v(r.get("name")) != ADMIN_LOGIN or _v(r.get("password")) != ADMIN_PW:
                return fault("account.AUTH_FAILED", f"authentication failed for [{_v(r.get('name'))}]")
            token = "0_" + _secrets.token_hex(24)
            z.tokens.add(token)
            return ok("Auth", {"authToken": [{"_content": token}], "lifetime": 43199998})
        token = _v(env["Header"]["context"].get("authToken"))
        if token not in z.tokens:
            return fault("service.AUTH_REQUIRED", "no valid authtoken present")
        if z.always_expired or token in z.expired:
            return fault("service.AUTH_EXPIRED", "auth credentials have expired")
        handler = getattr(z, "do_" + name, None)
        if handler is None:
            return fault("service.UNKNOWN_DOCUMENT", f"unknown document: {name}Request")
        return handler(r)

    z.fake = fake
    yield z
    fake.close()
    TOKENS._tokens.clear()


def connect(svc, z, **extra):
    values = {"type": "zimbra", "name": "Корп Zimbra", "host": z.fake.url, "admin": ADMIN_LOGIN,
              "admin_password": ADMIN_PW, "imap_host": "mail.corp.ru", "allow_changes": True}
    values.update(extra)
    return mss.save(svc, "tester", values)


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    return HANDLERS[job_type](JobContext(svc, job_id, job_type, None, params))


def act(svc, action, keys, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": None})
    return res, {i["key"]: i for i in res["items"]}


def transcripts(svc, sid):
    return "\n".join(msstore.get_log(svc.db, row["id"])["transcript"] for row in msstore.list_log(svc.db, sid))


def assert_no_secrets(svc, sid, z, *extra):
    text = transcripts(svc, sid)
    assert text
    assert ADMIN_PW not in text
    for token in z.tokens:
        assert token not in text and token[2:] not in text
    for value in extra:
        assert value not in text


@pytest.fixture()
def zm(services, zimbra, monkeypatch):
    monkeypatch.setattr(zmod, "PAGE", 2)              # страницы поиска — по две учётные записи
    sid = connect(services, zimbra)
    res = mss.sync(services, sid, user="tester")
    return type("Z", (), {"svc": services, "z": zimbra, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@corp.ru", "pe": f"ms:{sid}:petrova@corp.ru",
                          "sd": f"ms:{sid}:sidorov@corp.ru"})


# ---------------------------------------------------------------------------
#  Подключение
# ---------------------------------------------------------------------------
def test_parse_host_and_settings():
    assert parse_host("mail.corp.ru") == ("https", "mail.corp.ru", 7071)
    assert parse_host("mail.corp.ru:8443", 7071) == ("https", "mail.corp.ru", 8443)
    assert parse_host("https://mail.corp.ru:7071/service/admin/soap/") == ("https", "mail.corp.ru", 7071)
    for bad in ("", "ftp://mail.corp.ru", "https://mail.corp.ru/other", "https://u:p@mail.corp.ru"):
        with pytest.raises(ValidationError):
            parse_host(bad)
    assert gentime_iso("20261020080000Z") == "2026-10-20T08:00:00Z"
    assert gentime_iso("20261020080000.123Z") == "2026-10-20T08:00:00Z" and gentime_iso("") == ""
    with pytest.raises(ValidationError, match="без шифрования"):
        ZimbraDriver.validate_settings({"host": "http://mail.corp.ru"}, {})
    assert ZimbraDriver.validate_settings({"host": "http://mail.corp.ru", "allow_plain": True}, {}) == []
    # http на чужой адрес без разрешения — отказ ещё до запроса
    drv = ZimbraDriver(connection("zimbra", {"host": "http://mail.corp.ru:7071", "admin": ADMIN_LOGIN},
                                  {"admin_password": ADMIN_PW}), ApiLog())
    with pytest.raises(ServerError, match="без шифрования"):
        drv.test()
    assert ZimbraDriver.can(Cap.IMAP_ADMIN) and ZimbraDriver.can(Cap.MUST_CHANGE)


def test_check_reads_version_license_and_queue(services, zimbra):
    sid = connect(services, zimbra)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["version"] == "Zimbra 8.8.15_GA_4581"
    assert st["seats_used"] == 4 and st["seats_total"] == 100 and st["queue"] == 4
    assert "Учётных записей: 5" in st["details"] and any("2027-12-31" in d for d in st["details"])
    assert not st["problems"]
    assert "GetMailQueueInfo" in zimbra.requests
    assert msstore.get_server(services.db, sid).last_check_ok is True
    assert_no_secrets(services, sid, zimbra)


def test_carbonio_and_foss_have_no_license(services, zimbra):
    zimbra.version = "25.9.0_ZEXTRAS_202509"
    sid = connect(services, zimbra)
    out = mss.check(services, sid, user="tester")
    assert out["ok"] and out["status"]["version"].startswith("Carbonio 25.9.0")
    assert "GetLicense" not in zimbra.requests and out["status"]["seats_total"] is None
    # Zimbra FOSS: запроса лицензии нет — это не ошибка
    zimbra.version, zimbra.license = "8.8.15_GA_4581", False
    out = mss.check(services, sid, user="tester")
    assert out["ok"] and "Лицензия: нет (Zimbra FOSS)" in out["status"]["details"]
    assert not out["status"]["problems"]


def test_wrong_password_is_explained_and_not_logged(services, zimbra):
    sid = connect(services, zimbra, admin_password="Wrong-Pass~77")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "не принял логин или пароль администратора" in out["error"]
    assert "authentication failed" in out["error"]
    text = transcripts(services, sid)
    assert "Wrong-Pass~77" not in text and ADMIN_PW not in text
    assert "500" in text                       # ответ сервера (Fault) в журнале есть, тело входа — нет


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_normalizes_accounts(zm):
    rows = {r["email"]: r for r in msstore.list_accounts(zm.svc.db, zm.sid)}
    assert set(rows) == {"ivanov@corp.ru", "petrova@corp.ru", "sidorov@corp.ru", ADMIN_LOGIN, "galsync@corp.ru"}
    iv = rows["ivanov@corp.ru"]
    assert iv["aliases"] == ["ivan@corp.ru"] and iv["login_blocked"] is False
    assert iv["quota_kb"] == 2 * GB // 1024 and iv["used_bytes"] == GB and iv["messages"] is None
    assert iv["last_login"] == "2026-10-20T08:00:00Z" and iv["department"] == "Продажи"
    assert iv["title"] == "Менеджер" and iv["phone"] == "+7 495 000-00-01" and iv["first_name"] == "Иван"
    assert iv["kind"] == "user" and iv["info"]["status"] == "active" and iv["native_id"]
    assert rows["petrova@corp.ru"]["quota_kb"] is None and rows["petrova@corp.ru"]["last_login"] == ""
    assert rows["sidorov@corp.ru"]["login_blocked"] is True and rows["sidorov@corp.ru"]["info"]["status"] == "locked"
    assert rows[ADMIN_LOGIN]["kind"] == "admin" and rows["galsync@corp.ru"]["kind"] == "system"
    assert zm.sync["incomplete"] is False and not zm.sync["problems"]
    assert zm.z.requests.count("SearchDirectory") == 3           # пять учётных записей по две на страницу
    assert zm.z.auth_calls == 1
    assert_no_secrets(zm.svc, zm.sid, zm.z)


def test_sync_falls_back_to_domains_and_reports_denied_attributes(services, zimbra):
    zimbra.too_many = True
    zimbra.denied = {"zimbraLastLogonTimestamp"}
    sid = connect(services, zimbra)
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 5 and "GetAllDomains" in zimbra.requests
    assert any("zimbraLastLogonTimestamp" in p for p in res["problems"])
    row = msstore.get_account(services.db, sid, "ivanov@corp.ru")
    assert row["last_login"] == ""


def test_expired_token_relogin_once(zm):
    svc, z = zm.svc, zm.z
    conn = msstore.get_server(svc.db, zm.sid)
    assert z.auth_calls == 1
    # токен в кэше — новая сессия не входит заново
    assert mss.refresh_account(svc, conn, "ivanov@corp.ru")["used_bytes"] == GB
    assert z.auth_calls == 1
    # сервер счёл токен истёкшим — вход заново и повтор запроса, один раз
    z.expired |= set(z.tokens)
    assert mss.refresh_account(svc, conn, "ivanov@corp.ru") is not None
    assert z.auth_calls == 2
    z.always_expired = True
    with pytest.raises(ServerAuthError, match="токен администратора"):
        mss.refresh_account(svc, conn, "ivanov@corp.ru")
    assert z.auth_calls == 3                   # без бесконечных повторов
    assert "токен администратора истёк" in transcripts(svc, zm.sid)
    assert_no_secrets(svc, zm.sid, z)


def test_faults_become_russian_errors(zm):
    drv = mss.driver(zm.svc, msstore.get_server(zm.svc.db, zm.sid))
    with pytest.raises(NotFound, match="нет такой учётной записи или псевдонима"):
        drv.remove_alias("ivanov@corp.ru", "nope@corp.ru")
    with pytest.raises(ServerError, match="такого домена на сервере нет"):
        drv.create_account("x@unknown.ru", first_name="", last_name="", password="Good-Pass1")
    with pytest.raises(AlreadyExists, match="уже есть"):
        drv.create_account("ivan@corp.ru", first_name="", last_name="", password="Good-Pass1")
    assert drv.get_account("ivan@corp.ru") is None             # псевдоним — не учётная запись
    assert drv.get_account("nobody@corp.ru") is None
    with pytest.raises(NotFound):
        drv.set_quota("nobody@corp.ru", GB)


# ---------------------------------------------------------------------------
#  Действия раздела
# ---------------------------------------------------------------------------
def test_block_password_quota_contact_unblock(zm):
    svc, z = zm.svc, zm.z
    _res, items = act(svc, "ax_block", [zm.pe], final_copy=False, keep_receiving=True)
    assert items[zm.pe]["status"] == "success", items
    assert z.attr("petrova@corp.ru", "zimbraAccountStatus") == "locked" and "locked" in items[zm.pe]["detail"]
    assert msstore.get_account(svc.db, zm.sid, "petrova@corp.ru")["login_blocked"] is True
    # пароль — случайный, с требованием сменить при входе
    _res, items = act(svc, "ax_password", [zm.iv], mode="random_hidden", must_change=True)
    assert items[zm.iv]["status"] == "success", items
    assert len(z.passwords["ivanov@corp.ru"]) == 20
    assert z.attr("ivanov@corp.ru", "zimbraPasswordMustChange") == "TRUE"
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["must_change"] is True
    _res, items = act(svc, "ax_must_change", [zm.iv], value="no")
    assert z.attr("ivanov@corp.ru", "zimbraPasswordMustChange") == "FALSE"
    # квота
    _res, items = act(svc, "ax_quota", [zm.pe], quota_mb=4096)
    assert items[zm.pe]["status"] == "success", items
    assert z.attr("petrova@corp.ru", "zimbraMailQuota") == str(4 * GB)
    assert msstore.get_account(svc.db, zm.sid, "petrova@corp.ru")["quota_kb"] == 4 * GB // 1024
    # ФИО и отдел (отдел — атрибут ou)
    _res, items = act(svc, "ax_contact", [zm.pe], source="fixed", company="ООО Корп", department="ИТ",
                      office="Москва, 5 этаж", overwrite=True, name_order="last_first", phone=False)
    assert items[zm.pe]["status"] == "success", items
    assert z.attr("petrova@corp.ru", "ou") == "ИТ" and z.attr("petrova@corp.ru", "company") == "ООО Корп"
    assert z.attr("petrova@corp.ru", "physicalDeliveryOfficeName") == "Москва, 5 этаж"
    assert "проверьте" not in items[zm.pe]["detail"]
    _res, items = act(svc, "ax_unblock", [zm.pe])
    assert items[zm.pe]["status"] == "success" and z.attr("petrova@corp.ru", "zimbraAccountStatus") == "active"
    assert_no_secrets(svc, zm.sid, z, z.passwords["ivanov@corp.ru"])


def test_aliases_add_and_remove(zm):
    svc, z = zm.svc, zm.z
    _res, items = act(svc, "ax_alias_add", [zm.pe], template="{f}.{last}")
    assert items[zm.pe]["status"] == "success", items
    assert z.attr("petrova@corp.ru", "zimbraMailAlias") == "a.petrova@corp.ru"
    assert msstore.get_account(svc.db, zm.sid, "petrova@corp.ru")["aliases"] == ["a.petrova@corp.ru"]
    _res, items = act(svc, "ax_alias_remove", [zm.iv], which="list", aliases="ivan")
    assert items[zm.iv]["status"] == "success", items
    assert "zimbraMailAlias" not in z.primary("ivanov@corp.ru")["attrs"]
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["aliases"] == []


def test_autoreply_and_forwarding(zm):
    svc, z = zm.svc, zm.z
    z.primary("ivanov@corp.ru")["attrs"]["zimbraFeatureOutOfOfficeReplyEnabled"] = ["FALSE"]
    _res, items = act(svc, "srv_autoreply", [zm.iv], mode="on", subject="Нет на месте",
                      text="Я в отпуске.\nПишите petrova@corp.ru", start="2026-11-01", end="2026-11-10")
    assert items[zm.iv]["status"] == "success", items
    assert "автоответ был запрещён" in items[zm.iv]["detail"]
    assert z.attr("ivanov@corp.ru", "zimbraFeatureOutOfOfficeReplyEnabled") == "TRUE"
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeExternalReplyEnabled") == "FALSE"
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeReplyEnabled") == "TRUE"
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeReply") == \
        "Нет на месте\n\nЯ в отпуске.\nПишите petrova@corp.ru"
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeFromDate") == "20261101000000Z"
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeUntilDate") == "20261110235959Z"
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["autoreply"] is True
    drv = mss.driver(svc, msstore.get_server(svc.db, zm.sid))
    assert drv.get_autoreply("ivanov@corp.ru")["end"] == "2026-11-10"
    _res, items = act(svc, "srv_autoreply", [zm.iv], mode="off")
    assert z.attr("ivanov@corp.ru", "zimbraPrefOutOfOfficeReplyEnabled") == "FALSE"
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["autoreply"] is False
    # пересылка без копии; запрет пересылки в учётной записи снимается
    z.primary("ivanov@corp.ru")["attrs"]["zimbraFeatureMailForwardingEnabled"] = ["FALSE"]
    _res, items = act(svc, "srv_forwarding", [zm.iv], mode="on", addresses="petrova@corp.ru, boss@other.ru",
                      keep_copy=False)
    assert items[zm.iv]["status"] == "success", items
    assert z.attr("ivanov@corp.ru", "zimbraPrefMailForwardingAddress") == "petrova@corp.ru,boss@other.ru"
    assert z.attr("ivanov@corp.ru", "zimbraPrefMailLocalDeliveryDisabled") == "TRUE"
    assert z.attr("ivanov@corp.ru", "zimbraFeatureMailForwardingEnabled") == "TRUE"
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["forward_to"] == ["petrova@corp.ru",
                                                                                   "boss@other.ru"]
    assert drv.get_forwarding("ivanov@corp.ru") == {"addresses": ["petrova@corp.ru", "boss@other.ru"],
                                                    "keep_copy": False}
    _res, items = act(svc, "srv_forwarding", [zm.iv], mode="off", addresses="", keep_copy=True)
    assert "zimbraPrefMailForwardingAddress" not in z.primary("ivanov@corp.ru")["attrs"]
    assert z.attr("ivanov@corp.ru", "zimbraPrefMailLocalDeliveryDisabled") == "FALSE"   # письма снова в ящик


# ---------------------------------------------------------------------------
#  IMAP, удаление и передача адреса, создание
# ---------------------------------------------------------------------------
def imap_world(monkeypatch):
    world = ImapWorld()
    for email in ("petrova@corp.ru", "ivanov@corp.ru"):
        world.mailbox(email, "unknown-user-pass")
        world.add(email, "INBOX", raw_message(f"Письмо для {email}"))
    world.admins[ADMIN_LOGIN] = ADMIN_PW
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return world


def test_delete_with_full_archive_then_transfer_address(zm, monkeypatch):
    svc, z = zm.svc, zm.z
    world = imap_world(monkeypatch)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [zm.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@corp.ru")
    assert acc.auth_type == AuthType.SERVER and acc.host == "mail.corp.ru" and acc.password == ""
    # вход не закрыт — удалять нельзя
    pv = base.run(svc, ADMIN, "ax_delete", [zm.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "сначала «Заблокировать вход»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_block", [zm.pe], final_copy=True)
    assert items[zm.pe]["status"] == "success" and "последняя копия сделана" in items[zm.pe]["detail"], items
    assert world.last_login == ("plain_authzid", ADMIN_LOGIN, "petrova@corp.ru")
    _res, items = act(svc, "ax_delete", [zm.pe])
    assert items[zm.pe]["status"] == "success", items
    assert z.primary("petrova@corp.ru") is None and "резервной копии" in items[zm.pe]["detail"]
    acc = svc.db.get_account(acc.id)
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    assert msstore.get_account(svc.db, zm.sid, "petrova@corp.ru")["deleted_at"]
    # адрес удалённой — коллеге
    _res, items = act(svc, "ax_forward", [zm.pe], to="ivanov@corp.ru", with_aliases=True)
    assert items[zm.pe]["status"] == "success", items
    assert z.primary("ivanov@corp.ru")["attrs"]["zimbraMailAlias"] == ["ivan@corp.ru", "petrova@corp.ru"]
    assert "petrova@corp.ru" in msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["aliases"]
    pv = base.run(svc, ADMIN, "ax_forward", [zm.pe], {"to": "ivanov@corp.ru"}, preview=True)
    assert "уже переданы" in pv["results"][0]["detail"]
    assert_no_secrets(svc, zm.sid, z)


def test_imap_auth_goes_only_to_connection_host(zm):
    drv = mss.driver(zm.svc, msstore.get_server(zm.svc.db, zm.sid))
    auth = drv.imap_auth("Petrova@corp.ru")
    assert (auth.mech, auth.user, auth.authzid, auth.host) == ("plain_authzid", ADMIN_LOGIN, "petrova@corp.ru",
                                                               "mail.corp.ru")
    assert drv.imap_server() == ("mail.corp.ru", 993, "ssl")


def test_create_accounts_and_already_exists(zm, monkeypatch):
    svc, z = zm.svc, zm.z
    params = {"server_id": zm.sid, "text": "new.user@corp.ru; Новый; Сотрудник\nivanov@corp.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 1024, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@corp.ru"]["status"] == "ok"
    assert by["ivanov@corp.ru"]["status"] == "skip" and "уже есть" in by["ivanov@corp.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert not pv["warnings"]
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    # пока задание ждало, учётную запись завели вручную — пропуск «уже есть», пароль из файла убран
    manual = z.add("new.user@corp.ru", "Кто-то", "Успел")
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["skipped"] == 1 and "уже есть" in out["items"][0]["detail"], out
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 0
    z.accounts.pop(manual["id"])
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = z.primary("new.user@corp.ru")
    assert new["attrs"]["displayName"] == ["Новый Сотрудник"] and new["attrs"]["givenName"] == ["Новый"]
    assert new["attrs"]["zimbraPasswordMustChange"] == ["TRUE"]
    assert new["attrs"]["zimbraMailQuota"] == [str(GB)]
    row = msstore.get_account(svc.db, zm.sid, "new.user@corp.ru")
    assert row is not None and row["first_name"] == "Новый" and row["quota_kb"] == GB // 1024
    assert any(a.username == "new.user@corp.ru" and a.auth_type == AuthType.SERVER for a in svc.db.list_accounts())
    assert secrets.count(svc.db, res["op_id"]) == 1
    assert_no_secrets(svc, zm.sid, z, z.passwords["new.user@corp.ru"])


# ---------------------------------------------------------------------------
#  Права на псевдонимы, долгое удаление, повтор только чтений
# ---------------------------------------------------------------------------
def test_unreadable_aliases_keep_previous_snapshot(zm):
    svc, z = zm.svc, zm.z
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["aliases"] == ["ivan@corp.ru"]
    z.denied = {"zimbraMailAlias"}
    res = mss.sync(svc, zm.sid, user="tester")
    assert any("псевдонимы — как при прошлой сверке" in p for p in res["problems"]), res["problems"]
    # «нет права читать» — не «псевдонимов нет»: прежние в снимке остаются
    assert msstore.get_account(svc.db, zm.sid, "ivanov@corp.ru")["aliases"] == ["ivan@corp.ru"]
    drv = mss.driver(svc, msstore.get_server(svc.db, zm.sid))
    assert "aliases" not in drv.get_account("ivanov@corp.ru")
    assert drv.add_alias("ivanov@corp.ru", "i.ivanov@corp.ru") is None        # список после изменения неизвестен
    assert "i.ivanov@corp.ru" in z.primary("ivanov@corp.ru")["attrs"]["zimbraMailAlias"]
    assert drv.remove_alias("ivanov@corp.ru", "i.ivanov@corp.ru") is None
    z.denied = set()
    assert drv.add_alias("ivanov@corp.ru", "i.ivanov@corp.ru") == ["ivan@corp.ru", "i.ivanov@corp.ru"]


def test_delete_waits_long_and_only_reads_are_repeatable(zimbra, monkeypatch):
    seen = []
    real = zmod.HttpClient.request

    def spy(self, method, path, **kw):
        name = next(iter(kw["json_body"]["Body"]))[:-len("Request")]
        seen.append((name, kw.get("timeout"), kw.get("idempotent")))
        return real(self, method, path, **kw)
    monkeypatch.setattr(zmod.HttpClient, "request", spy)
    drv = ZimbraDriver(connection("zimbra", {"host": zimbra.fake.url, "admin": ADMIN_LOGIN},
                                  {"admin_password": ADMIN_PW}), ApiLog())
    assert drv.get_account("petrova@corp.ru") is not None
    drv.delete_account("petrova@corp.ru")
    assert zimbra.primary("petrova@corp.ru") is None
    flags = {name: (timeout, idem) for name, timeout, idem in seen}
    # удаление большого ящика Zimbra делает до ответа — ждём долго и не повторяем после 502/504
    assert flags["DeleteAccount"] == (zmod.DELETE_TIMEOUT_S, False)
    assert flags["GetAccount"] == (None, True) and flags["GetMailbox"] == (None, True)
    assert flags["Auth"][1] is False
