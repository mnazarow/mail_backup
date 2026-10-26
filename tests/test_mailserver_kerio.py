"""Подключение Kerio Connect: учебный API администрирования (JSON-RPC 2.0 поверх tests/mailserver_fake.py).

Проверяется весь путь: вход (токен — в X-Token, cookie SESSION_CONNECT_WEBADMIN — даже когда Set-Cookie у входа
два), выход, истёкший сеанс (-32001 → вход заново, но один раз), проверка подключения (версия, лицензии, очередь,
предупреждения сервера), сверка страницами по доменам с личными контактами, действия раздела «Ящики на сервере»
через драйвер, ошибки внутри ответа 200 (``error`` и ``result.errors``), создание учётных записей и то, что пароль,
токен и cookie не попадают в журнал подключения.
"""
import copy
from datetime import datetime, timezone

import pytest

from mailserver_fake import FakeHttp, connection
from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, ServerError
from mailarchiver.mailservers.drivers import kerio as kmod
from mailarchiver.mailservers.http import ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create
from mailarchiver.serverops import targets as tg

ADMIN = {"username": "tester", "role": "admin"}
LOGIN = "mailarchiver"
PASSWORD = "Adm1n-Pass~42"
IMAP_HOST = "mail.company.ru"
LAST_LOGIN = int(datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc).timestamp())
EXPIRED = {"code": -32001, "message": "Session expired.",
           "data": {"messageParameters": {"positionalParameters": [], "plurality": 1}}}
UNITS = ("Bytes", "KiloBytes", "MegaBytes", "GigaBytes", "TeraBytes", "PetaBytes")
MODES = ("UForwardNone", "UForwardYes", "UForwardDeliver")


def kid(kind, n):
    return f"keriodb://{kind}/{n:08d}-1c2d-4e5f-8a9b-0c1d2e3f4a5b"


D1, D2 = kid("domain", 1), kid("domain", 2)


def user(n, domain_id, login, full, *, enabled=True, aliases=(), quota_mb=0, used_kb=0, items=0, last=0,
         forward=None, source="DSInternalSource", auth="UInternalAuth"):
    return {"id": kid("user", n), "domainId": domain_id, "loginName": login, "fullName": full, "description": "",
            "isEnabled": enabled, "itemSource": source, "authType": auth,
            "role": {"userRole": "UserRole", "publicFolderRight": False, "archiveFolderRight": False},
            "emailAddresses": list(aliases),
            "emailForwarding": forward or {"mode": "UForwardNone", "emailAddresses": []},
            "diskSizeLimit": {"isActive": bool(quota_mb), "limit": {"value": quota_mb, "units": "MegaBytes"}},
            "consumedSize": {"value": used_kb, "units": "KiloBytes"}, "consumedItems": items,
            "lastLoginInfo": {"dateTime": last, "protocol": "IMAP" if last else ""}}


def contact(first, last, **extra):
    out = {"commonName": f"{first} {last}", "firstName": first, "middleName": "", "surName": last,
           "titleBefore": "", "titleAfter": "", "nickName": "", "phoneNumberWorkVoice": "", "phoneNumberMobile": "",
           "urlWork": "", "companyName": "", "departmentName": "", "profession": "", "managerName": "",
           "assistantName": "", "comment": "", "IMAddress": "", "isReadOnly": False, "emailAddresses": []}
    out.update(extra)
    return out


class Kerio:
    """Состояние учебного сервера Kerio Connect."""

    def __init__(self):
        self.domains = [{"id": D1, "name": "company.ru", "aliasList": ["company.local"], "isPrimary": True,
                         "description": ""},
                        {"id": D2, "name": "filial.ru", "aliasList": [], "isPrimary": False, "description": ""}]
        users = [
            user(1, D1, "ivanov", "Иван Иванов", aliases=["ivan", "i.ivanov"], quota_mb=10240,
                 used_kb=5 * 1024 * 1024, items=1200, last=LAST_LOGIN),
            user(2, D1, "petrova", "Анна Петрова"),
            user(3, D1, "sidorov", "Пётр Сидоров", enabled=False, aliases=["s.sidorov"],
                 forward={"mode": "UForwardDeliver", "emailAddresses": ["boss@company.ru"]}),
            user(4, D1, "kozlov", "Олег Козлов"),
            user(5, D2, "ad.user", "Пользователь Каталога", source="DSLDAPSource", auth="UWindowsNTAuth"),
        ]
        self.users = {u["id"]: u for u in users}
        self.contacts = {
            kid("user", 1): contact("Иван", "Иванов", departmentName="Продажи", profession="Менеджер",
                                    phoneNumberWorkVoice="+7 495 000-00-01"),
            kid("user", 2): contact("Анна", "Петрова"),
            kid("user", 3): contact("Пётр", "Сидоров"),
            kid("user", 4): contact("Олег", "Козлов"),
            kid("user", 5): contact("Пользователь", "Каталога", isReadOnly=True),
        }
        self.passwords = {}
        self.sessions = {}            # токен → cookie сеанса
        self.logins = 0
        self.logouts = 0
        self.expire_next = None       # через сколько вызовов сеанс «истечёт» (один раз)
        self.expire_all = False       # сеанс истекает сразу после входа (сломанный сервер)
        self.role = "FullAdmin"
        self.domains_denied = False   # администратору домена список доменов закрыт
        self.hidden = set()          # логины, которых Users.get не видит (гонка при создании)
        self.removed = []
        self.calls = []               # (метод, параметры, заголовки)
        self.queue = 3
        self.sub_days = 20
        self.alerts = [{"alertName": "StorageSpace", "alertType": "Warning", "currentValue": "8 GB",
                        "criticalValue": "10 GB"}]
        self.n = 100

    def by_login(self, domain_id, login):
        return next((u for u in self.users.values() if u["domainId"] == domain_id and u["loginName"] == login), None)

    def methods(self, name):
        return [c for c in self.calls if c[0] == name]


def _page(items, query):
    start = int(query.get("start") or 0)
    limit = int(query.get("limit") if query.get("limit") is not None else -1)
    chunk = items[start:] if limit == -1 else items[start:start + limit]
    return {"list": copy.deepcopy(chunk), "totalItems": len(items)}


def _complete_size_limit(value):
    limit = (value or {}).get("limit") or {}
    return isinstance(value.get("isActive"), bool) and isinstance(limit.get("value"), int) and \
        limit.get("units") in UNITS


def install(k: Kerio, fake: FakeHttp):
    @fake.route("POST", r"/admin/api/jsonrpc/")
    def rpc(req):
        body = req.json()
        method, params, rid = body.get("method"), body.get("params") or {}, body.get("id")
        k.calls.append((method, copy.deepcopy(params), dict(req.headers)))
        assert req.headers.get("content-type", "").startswith("application/json-rpc")

        def ok(result):
            return 200, {"jsonrpc": "2.0", "id": rid, "result": result}

        def fail(err, status=200):
            return status, {"jsonrpc": "2.0", "id": rid, "error": err}

        def item_error(index, code, message, *pos):
            return {"inputIndex": index, "code": code, "message": message,
                    "messageParameters": {"positionalParameters": list(pos), "plurality": 1}}

        if method == "Session.login":
            app = params.get("application") or {}
            assert app.get("name") and app.get("vendor") and app.get("version")
            if params.get("userName") != LOGIN or params.get("password") != PASSWORD:
                return fail({"code": 1000, "message": "Invalid user name or password."})
            k.logins += 1
            token = f"c5rf70ken{k.logins:04d}aa55aa55"
            session = f"5e5510n{k.logins:04d}bb66bb66"
            k.sessions[token] = session
            # два Set-Cookie: у HttpClient в словаре заголовков остался бы только последний
            return 200, {"jsonrpc": "2.0", "id": rid, "result": {"token": token}}, {
                "Set-Cookie": f"SESSION_CONNECT_WEBADMIN={session}; path=/admin/; secure; HttpOnly",
                "set-cookie": f"TOKEN_CONNECT_WEBADMIN=t{k.logins:04d}; path=/admin/; secure"}
        token = req.headers.get("x-token", "")
        if token not in k.sessions or f"SESSION_CONNECT_WEBADMIN={k.sessions[token]}" not in \
                req.headers.get("cookie", ""):
            return fail(EXPIRED, 500)
        if k.expire_all:
            k.sessions.pop(token, None)
            return fail(EXPIRED, 500)
        if k.expire_next is not None:
            k.expire_next -= 1
            if k.expire_next < 0:
                k.expire_next = None
                k.sessions.pop(token, None)
                return fail(EXPIRED, 500)
        writable = k.role in ("FullAdmin", "AccountAdmin")

        if method == "Session.logout":
            k.logouts += 1
            k.sessions.pop(token, None)
            return ok({"redirectUrl": "https://mail.company.ru:4040/admin/login"})
        if method == "Session.whoAmI":
            return ok({"userDetails": {"id": kid("user", 99), "domainId": D1, "loginName": LOGIN,
                                       "fullName": "Служба архива",
                                       "effectiveRole": {"userRole": k.role, "publicFolderRight": False,
                                                         "archiveFolderRight": False}}})
        if method == "Server.getProductInfo":
            return ok({"info": {"productName": "Kerio Connect", "version": "10.0.6", "buildNumber": "8504",
                                "osName": "Debian GNU/Linux 12", "os": "Linux", "releaseType": "Final",
                                "deployedType": "DeployedStandalone"}})
        if method == "Server.getAboutInfo":
            return ok({"aboutInfo": {"currentUsers": 25, "allowedUsers": {"isUnlimited": False, "allowedUsers": 50},
                                     "serverSoftware": "Kerio Connect 10.0.6",
                                     "subscription": {"isUnlimited": False, "showAlert": True,
                                                      "remainingDays": k.sub_days}}})
        if method == "ProductRegistration.getFullStatus":
            return ok({"status": {"regType": "rsProductRegistered", "Id": "1A2B-3C4D", "company": "ООО Компания",
                                  "users": 50, "expirations": [
                                      {"type": "License", "isUnlimited": True, "remainingDays": 0},
                                      {"type": "Subscription", "isUnlimited": False, "remainingDays": k.sub_days}]}})
        if method == "Queue.get":
            return ok({"list": [{"id": "q1", "from": "a@company.ru", "to": "b@other.ru", "status": "Retry"}],
                       "totalItems": k.queue, "volume": {"value": 12, "units": "KiloBytes"}})
        if method == "Server.getAlertList":
            return ok({"alerts": k.alerts})
        if method == "Domains.get":
            if k.domains_denied:
                return fail({"code": 1004, "message": "Access denied."})
            return ok(_page(sorted(k.domains, key=lambda d: d["name"]), params["query"]))
        if method == "Session.getDomain":
            return ok({"domain": copy.deepcopy(k.domains[0])})
        if method == "Users.get":
            items = [u for u in k.users.values() if u["domainId"] == params["domainId"]
                     and u["loginName"] not in k.hidden]
            for cond in params["query"].get("conditions") or []:
                assert cond["comparator"] == "Eq" and cond["fieldName"] == "loginName"
                items = [u for u in items if u["loginName"] == cond["value"]]
            return ok(_page(sorted(items, key=lambda u: u["loginName"]), params["query"]))
        if method == "Users.getPersonalContact":
            errors, out = [], []
            for i, uid in enumerate(params["userIds"]):
                if uid in k.contacts:
                    out.append(copy.deepcopy(k.contacts[uid]))
                else:
                    errors.append(item_error(i, 1002, "User %1 does not exist.", uid))
            return ok({"errors": errors, "contacts": out})
        if method == "Users.setPersonalContact":
            errors = []
            for i, uid in enumerate(params["userIds"]):
                if not writable:
                    errors.append(item_error(i, 1004, "Access denied."))
                elif k.contacts.get(uid, {}).get("isReadOnly"):
                    errors.append(item_error(i, 1003, "Contact is read-only."))
                else:
                    k.contacts[uid] = dict(copy.deepcopy(params["contact"]), isReadOnly=False)   # заменяется целиком
            return ok({"errors": errors})
        if method == "Users.set":
            pattern = params["pattern"]
            if "diskSizeLimit" in pattern and not _complete_size_limit(pattern["diskSizeLimit"]):
                return fail({"code": -32602, "message": "Invalid params: diskSizeLimit must be complete."})
            if "emailForwarding" in pattern:
                fwd = pattern["emailForwarding"]
                if fwd.get("mode") not in MODES or not isinstance(fwd.get("emailAddresses"), list):
                    return fail({"code": -32602, "message": "Invalid params: emailForwarding must be complete."})
            errors = []
            for i, uid in enumerate(params["userIds"]):
                if not writable:
                    errors.append(item_error(i, 1004, "You do not have sufficient rights to change user %1.",
                                             k.users[uid]["loginName"]))
                    continue
                for key, value in pattern.items():
                    if key == "password":
                        k.passwords[uid] = value
                    else:
                        k.users[uid][key] = copy.deepcopy(value)
            return ok({"errors": errors})
        if method == "Users.create":
            errors, result = [], []
            for i, new in enumerate(params["users"]):
                assert new.get("domainId") and new.get("loginName")
                if k.by_login(new["domainId"], new["loginName"]):
                    errors.append(item_error(i, 1001, "User %1 already exists.", new["loginName"]))
                    continue
                k.n += 1
                u = user(k.n, new["domainId"], new["loginName"], new.get("fullName", ""))
                if new.get("diskSizeLimit"):
                    assert _complete_size_limit(new["diskSizeLimit"])
                    u["diskSizeLimit"] = new["diskSizeLimit"]
                k.users[u["id"]] = u
                k.passwords[u["id"]] = new.get("password")
                k.contacts[u["id"]] = contact("", "")
                result.append({"inputIndex": i, "id": u["id"]})
            return ok({"errors": errors, "result": result})
        if method == "Users.remove":
            errors = []
            for i, request in enumerate(params["requests"]):
                assert request["method"] in ("UDeleteUser", "UDeleteFolder", "UMoveFolder")
                assert request["mode"] in ("DSModeDeactivate", "DSModeDelete")
                if request["userId"] not in k.users:
                    errors.append(item_error(i, 1002, "User does not exist."))
                    continue
                k.removed.append(dict(request, login=k.users.pop(request["userId"])["loginName"]))
            return ok({"errors": errors})
        return fail({"code": -32601, "message": f"Method {method} not found."})


@pytest.fixture()
def kerio(monkeypatch):
    k = Kerio()
    fake = FakeHttp()
    install(k, fake)
    monkeypatch.setattr(kmod, "PAGE", 2)            # страницы — и на маленьком списке
    k.fake = fake
    yield k
    fake.close()


def connect(svc, k, **extra):
    values = {"type": "kerio", "name": "Kerio офис", "host": k.fake.url, "username": LOGIN, "password": PASSWORD,
              "imap_host": IMAP_HOST, "allow_changes": True}
    values.update(extra)
    return mss.save(svc, "tester", values)


def run_job(svc, job_type, params):
    job_id = svc.db.enqueue_job(job_type, None, params, 5, 1, "tester")
    ctx = JobContext(svc, job_id, job_type, None, params)
    return HANDLERS[job_type](ctx)


def act(svc, action, keys, **options):
    res = run_job(svc, JobType.SERVER_ACTION, {"action": action, "keys": keys, "options": options,
                                               "requested_by": "tester", "op_id": None})
    return res, {i["key"]: i for i in res["items"]}


def transcripts(svc, sid):
    return "\n".join(msstore.get_log(svc.db, row["id"])["transcript"] for row in msstore.list_log(svc.db, sid))


@pytest.fixture()
def srv(services, kerio):
    sid = connect(services, kerio)
    res = mss.sync(services, sid, user="tester")
    return type("K", (), {"svc": services, "k": kerio, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@company.ru", "pe": f"ms:{sid}:petrova@company.ru",
                          "si": f"ms:{sid}:sidorov@company.ru", "ad": f"ms:{sid}:ad.user@filial.ru"})


# -- вход, проверка, журнал --------------------------------------------------------------------------
def test_check_reads_version_licences_queue_and_alerts(services, kerio):
    sid = connect(services, kerio)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["version"] == "Kerio Connect 10.0.6 (сборка 8504)"
    assert st["seats_used"] == 25 and st["seats_total"] == 50 and st["queue"] == 3
    assert any("company.ru, filial.ru" in d for d in st["details"])
    assert any("полные права" in d for d in st["details"])
    problems = {p[0]: p for p in st["problems"]}
    assert "через 20 дн." in problems["expire_Subscription"][2]          # подписка скоро кончится
    assert problems["alert_StorageSpace"][1] == "warning"
    # каждый вызов после входа — с токеном в X-Token и cookie сеанса (из первого из двух Set-Cookie)
    authed = [c for c in kerio.calls if c[0] != "Session.login"]
    assert authed and all(c[2].get("x-token", "").startswith("c5rf70ken") for c in authed)
    assert all("SESSION_CONNECT_WEBADMIN=5e5510n0001bb66bb66" in c[2].get("cookie", "") for c in authed)
    # сеанс закрыт
    assert kerio.logouts == 1 and not kerio.sessions
    # ни пароль, ни токен, ни cookie не попали в журнал подключения
    text = transcripts(services, sid)
    assert "Session.login" in text and "Users.get" not in text
    assert PASSWORD not in text and "c5rf70ken0001aa55aa55" not in text and "5e5510n0001bb66bb66" not in text


def test_wrong_password_is_explained_and_not_logged(services, kerio):
    sid = connect(services, kerio, password="Wrong-Pass~1")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"]
    assert "вход администратора «mailarchiver» отклонён" in out["error"] and "Invalid user name" in out["error"]
    assert "пароль приложения" in out["hint"]
    text = transcripts(services, sid)
    assert "Wrong-Pass~1" not in text and PASSWORD not in text


def test_read_only_admin_is_warned(services, kerio):
    kerio.role = "Auditor"
    sid = connect(services, kerio)
    out = mss.check(services, sid, user="tester")
    assert out["ok"] and any("только на чтение" in p[2] for p in out["status"]["problems"])


def test_plain_http_only_when_allowed(services, kerio):
    sid = connect(services, kerio, host="http://10.20.30.40")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "без шифрования (http) не разрешено" in out["error"]
    with pytest.raises(ValidationError):
        connect(services, kerio, host="https://mail.company.ru/admin")       # путь в адресе — ошибка
    drv = kmod.KerioDriver(connection("kerio", {"host": "mail.company.ru"}), ApiLog())
    assert drv.base_url() == "https://mail.company.ru:4040"
    assert drv.imap_server() == ("mail.company.ru", 993, "ssl")
    assert drv.seen_fingerprint == ""


def test_session_expiry_relogins_once(services, kerio):
    sid = connect(services, kerio)
    kerio.expire_next = 3                         # сеанс истекает посреди сверки
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 5 and not res["incomplete"]
    assert kerio.logins == 2
    assert "сеанс Kerio Connect истёк — вход заново" in transcripts(services, sid)
    # сервер, который обрывает сеанс сразу, — ошибка, а не вечный цикл входов
    kerio.expire_all = True
    kerio.logins = 0
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "-32001" in out["error"] and "Session expired" in out["error"]
    assert kerio.logins == 2


# -- сверка --------------------------------------------------------------------------------------
def test_sync_all_domains_paged_with_contacts(srv):
    rows = {r["email"]: r for r in msstore.list_accounts(srv.svc.db, srv.sid)}
    assert set(rows) == {"ivanov@company.ru", "petrova@company.ru", "sidorov@company.ru", "kozlov@company.ru",
                         "ad.user@filial.ru"}
    iv = rows["ivanov@company.ru"]
    assert iv["aliases"] == ["ivan@company.ru", "i.ivanov@company.ru"] and iv["login_blocked"] is False
    assert iv["quota_kb"] == 10240 * 1024 and iv["used_bytes"] == 5 * 1024 ** 3
    # consumedItems — все элементы (контакты, события…), а не письма: в «писем» не идут, только в карточку
    assert iv["messages"] is None and iv["info"]["items"] == 1200
    assert iv["last_login"] == "2026-10-20T08:00:00Z" and iv["info"]["last_login_protocol"] == "IMAP"
    assert (iv["first_name"], iv["last_name"], iv["display_name"]) == ("Иван", "Иванов", "Иван Иванов")
    assert (iv["department"], iv["title"], iv["phone"]) == ("Продажи", "Менеджер", "+7 495 000-00-01")
    si = rows["sidorov@company.ru"]
    assert si["login_blocked"] is True and si["forward_to"] == ["boss@company.ru"]
    assert rows["petrova@company.ru"]["last_login"] == "" and rows["petrova@company.ru"]["quota_kb"] is None
    assert rows["ad.user@filial.ru"]["info"]["source"] == "служба каталогов"
    assert srv.sync["incomplete"] is False and not srv.sync["problems"]
    # страницы: в company.ru 4 пользователя по 2 на страницу; контакты — одним запросом на страницу
    pages = [c for c in srv.k.methods("Users.get") if c[1]["domainId"] == D1]
    assert [c[1]["query"]["start"] for c in pages] == [0, 2]
    assert len(srv.k.methods("Users.getPersonalContact")) == 3


def test_sync_capped_pages_is_incomplete(services, kerio, monkeypatch):
    sid = connect(services, kerio)
    mss.sync(services, sid, user="tester")
    del kerio.users[kid("user", 4)]
    monkeypatch.setattr(kmod, "MAX_PAGES", 1)
    res = mss.sync(services, sid, user="tester")
    assert res["incomplete"] is True
    # не дочитанные учётные записи удалёнными не считаются
    assert not msstore.get_account(services.db, sid, "sidorov@company.ru")["deleted_at"]


def test_domain_admin_reads_own_domain(services, kerio):
    kerio.domains_denied = True
    sid = connect(services, kerio)
    res = mss.sync(services, sid, user="tester")
    emails = {r["email"] for r in msstore.list_accounts(services.db, sid)}
    assert emails == {"ivanov@company.ru", "petrova@company.ru", "sidorov@company.ru", "kozlov@company.ru"}
    assert res["accounts"] == 4 and kerio.methods("Session.getDomain")


def test_targets_password_mailbox_and_no_password_free_copy(srv):
    svc = srv.svc
    linked = svc.db.create_account(models.Account(name="Петрова", host=IMAP_HOST, username="petrova@company.ru",
                                                  password="pw"))
    items = {t.key: t for t in tg.load(svc)}
    assert items[srv.pe].mailbox is not None and items[srv.pe].mailbox.id == linked
    row = tg.row_dict(svc, items[srv.iv], {}, {})
    assert row["server"]["type"] == "kerio" and "block" in row["server"]["caps"]
    assert "imap_admin" in row["server"]["caps"]
    # мастер-пароль не задан — вход без паролей не настроен, ящики не заводятся
    pv = base.run(svc, ADMIN, "ax_add_mailboxes", [srv.iv], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "не задан мастер-пароль" in pv["results"][0]["detail"]
    acc = models.Account(name="Иванов", host=IMAP_HOST, username="ivanov@company.ru", password="",
                         auth_type=AuthType.SERVER)
    acc.server_id = srv.sid
    with pytest.raises(ServerError, match="не задан мастер-пароль"):
        mss.imap_auth_for(svc, acc)


def test_master_authentication_copies_without_passwords(srv, monkeypatch):
    from imap_fake import ImapWorld, raw_message
    from mailarchiver.imap import client as client_mod
    from mailarchiver.imap.client import ImapConnection
    svc = srv.svc
    world = ImapWorld()
    world.mailbox("ivanov@company.ru", "x")
    world.add("ivanov@company.ru", "INBOX", raw_message("Письмо"))
    world.kerio_master = "Master-Pass9"
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    mss.save(svc, "tester", {"master_password": "Master-Pass9"}, srv.sid)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [srv.iv], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "ivanov@company.ru")
    assert acc.auth_type == AuthType.SERVER and acc.host == IMAP_HOST
    conn = ImapConnection(acc, svc.connect_options())
    conn.connect()
    try:
        assert world.last_login == ("kerio_master", "master", "ivanov@company.ru")
    finally:
        conn.close()
    # мастер-пароль не попадает в журнал подключения
    for row in msstore.list_log(svc.db, srv.sid):
        assert "Master-Pass9" not in (msstore.get_log(svc.db, row["id"])["transcript"] or "")


def test_capabilities_skip_with_reason(srv):
    svc = srv.svc
    pv = base.run(svc, ADMIN, "srv_autoreply", [srv.iv], {"mode": "on", "text": "Нет на месте"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "автоответ — этот сервер так не умеет" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_must_change", [srv.iv], {"value": "yes"}, preview=True)
    assert "смена пароля при входе — этот сервер так не умеет" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "report", [srv.iv], {}, preview=True)
    assert pv["counts"]["ok"] == 1 and "через API Kerio Connect" in pv["results"][0]["detail"]


# -- действия ------------------------------------------------------------------------------------
def test_block_password_quota_unblock(srv):
    svc, k = srv.svc, srv.k
    _res, items = act(svc, "ax_block", [srv.pe], final_copy=False, keep_receiving=True)
    assert items[srv.pe]["status"] == "success", items
    assert k.users[kid("user", 2)]["isEnabled"] is False and "не будет" in items[srv.pe]["detail"]
    assert msstore.get_account(svc.db, srv.sid, "petrova@company.ru")["login_blocked"] is True
    # пароль: случайный; «сменить при входе» Kerio не умеет — так и сказано
    _res, items = act(svc, "ax_password", [srv.iv], mode="random_hidden", must_change=True)
    assert items[srv.iv]["status"] == "success", items
    assert len(k.passwords[kid("user", 1)]) == 20
    assert "смену пароля при входе Kerio Connect не требует" in items[srv.iv]["detail"]
    # квота: SizeLimit целиком, в самых крупных единицах
    _res, items = act(svc, "ax_quota", [srv.iv], quota_mb=20480, only_raise=True, threshold=0)
    assert items[srv.iv]["status"] == "success", items
    assert k.users[kid("user", 1)]["diskSizeLimit"] == {"isActive": True, "limit": {"value": 20, "units": "GigaBytes"}}
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["quota_kb"] == 20480 * 1024
    _res, items = act(svc, "ax_unblock", [srv.pe])
    assert items[srv.pe]["status"] == "success" and k.users[kid("user", 2)]["isEnabled"] is True
    assert PASSWORD not in transcripts(svc, srv.sid)


def test_password_of_directory_user_is_refused(srv):
    _res, items = act(srv.svc, "ax_password", [srv.ad], mode="random_hidden", must_change=False)
    assert items[srv.ad]["status"] == "failed" and "служба каталогов" in items[srv.ad]["detail"], items
    assert kid("user", 5) not in srv.k.passwords


def test_rights_error_inside_200_response(services, kerio):
    sid = connect(services, kerio)
    mss.sync(services, sid, user="tester")
    kerio.role = "Auditor"
    key = f"ms:{sid}:petrova@company.ru"
    _res, items = act(services, "ax_block", [key], final_copy=False)
    assert items[key]["status"] == "failed", items
    assert "недостаточно прав (Users.set)" in items[key]["detail"] and "change user petrova" in items[key]["detail"]
    assert kerio.users[kid("user", 2)]["isEnabled"] is True


def test_aliases_add_and_remove(srv):
    svc, k = srv.svc, srv.k
    _res, items = act(svc, "ax_alias_add", [srv.pe], template="{f}.{last}")
    assert items[srv.pe]["status"] == "success", items
    assert k.users[kid("user", 2)]["emailAddresses"] == ["a.petrova"]          # как хранит сервер — без домена
    assert msstore.get_account(svc.db, srv.sid, "petrova@company.ru")["aliases"] == ["a.petrova@company.ru"]
    _res, items = act(svc, "ax_alias_remove", [srv.iv], which="list", aliases="i.ivanov")
    assert items[srv.iv]["status"] == "success", items
    assert k.users[kid("user", 1)]["emailAddresses"] == ["ivan"]
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["aliases"] == ["ivan@company.ru"]


def test_contact_keeps_other_fields(srv):
    svc, k = srv.svc, srv.k
    _res, items = act(svc, "ax_contact", [srv.iv], source="fixed", company="ООО Компания", department="ИТ",
                      office="", overwrite=True, name_order="last_first", phone=False)
    assert items[srv.iv]["status"] == "success", items
    c = k.contacts[kid("user", 1)]
    assert c["companyName"] == "ООО Компания" and c["departmentName"] == "ИТ"
    assert c["phoneNumberWorkVoice"] == "+7 495 000-00-01" and c["profession"] == "Менеджер"   # не потеряны
    row = msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")
    assert row["company"] == "ООО Компания" and row["department"] == "ИТ"
    # личный контакт из каталога — только для чтения: понятная ошибка, а не тихий пропуск
    drv = mss.driver(svc, msstore.get_server(svc.db, srv.sid))
    try:
        with pytest.raises(ServerError, match="только для чтения"):
            drv.set_contact("ad.user@filial.ru", {"department": "ИТ"})
        drv.set_contact("petrova@company.ru", {"display_name": "Петрова Анна"})
        assert k.users[kid("user", 2)]["fullName"] == "Петрова Анна"
    finally:
        drv.close()


def test_forwarding_modes(srv):
    svc, k = srv.svc, srv.k
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="on", addresses="petrova@company.ru", keep_copy=True)
    assert items[srv.iv]["status"] == "success", items
    assert k.users[kid("user", 1)]["emailForwarding"] == {"mode": "UForwardDeliver",
                                                          "emailAddresses": ["petrova@company.ru"]}
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["forward_to"] == ["petrova@company.ru"]
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="on", addresses="boss@company.ru\nhr@company.ru",
                      keep_copy=False)
    assert k.users[kid("user", 1)]["emailForwarding"]["mode"] == "UForwardYes"
    assert "не доставляются" in items[srv.iv]["detail"]
    drv = mss.driver(svc, msstore.get_server(svc.db, srv.sid))
    try:
        assert drv.get_forwarding("ivanov@company.ru") == {"addresses": ["boss@company.ru", "hr@company.ru"],
                                                           "keep_copy": False}
    finally:
        drv.close()
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="off", addresses="", keep_copy=True)
    assert k.users[kid("user", 1)]["emailForwarding"] == {"mode": "UForwardNone", "emailAddresses": []}
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["forward_to"] == []


def test_delete_needs_password_free_copy_and_driver_deletes_folder(srv):
    svc, k = srv.svc, srv.k
    svc.db.create_account(models.Account(name="Петрова", host=IMAP_HOST, username="petrova@company.ru",
                                         password="pw"))
    msstore.update_fields(svc.db, srv.sid, "petrova@company.ru", login_blocked=True)
    pv = base.run(svc, ADMIN, "ax_delete", [srv.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "по паролю сотрудника" in pv["results"][0]["detail"]
    assert kid("user", 2) in k.users
    # сам вызов удаления (для времени, когда копирование без паролей появится)
    conn = msstore.get_server(svc.db, srv.sid)
    with mss.session(svc, conn, user="tester", kind="write") as drv:
        text = drv.delete_account("petrova@company.ru")
        with pytest.raises(ServerError, match="службы каталогов"):
            drv.delete_account("ad.user@filial.ru")
    assert "папка с письмами удалены" in text
    assert k.removed == [{"userId": kid("user", 2), "method": "UDeleteFolder", "removeReferences": True,
                          "targetUserId": "", "mode": "DSModeDelete", "login": "petrova"}]
    assert kid("user", 5) in k.users


def test_transfer_address_after_deletion_in_webadmin(srv):
    svc, k = srv.svc, srv.k
    del k.users[kid("user", 3)]                   # Сидорова удалили в Webadmin
    res = mss.sync(svc, srv.sid, user="tester")
    assert res["changes"]["gone"] == ["sidorov@company.ru"]
    pv = base.run(svc, ADMIN, "ax_forward", [srv.si], {"to": "petrova@company.ru", "with_aliases": True},
                  preview=True)
    assert pv["counts"]["ok"] == 1, pv
    _res, items = act(svc, "ax_forward", [srv.si], to="petrova@company.ru", with_aliases=True)
    assert items[srv.si]["status"] == "success", items
    assert k.users[kid("user", 2)]["emailAddresses"] == ["sidorov", "s.sidorov"]
    row = msstore.get_account(svc.db, srv.sid, "petrova@company.ru")
    assert row["aliases"] == ["sidorov@company.ru", "s.sidorov@company.ru"]


# -- создание ------------------------------------------------------------------------------------
def test_create_accounts_on_connection(srv, monkeypatch):
    svc, k = srv.svc, srv.k
    params = {"server_id": srv.sid, "text": "new.user@company.ru; Новый; Сотрудник\nivanov@company.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 1024, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@company.ru"]["status"] == "ok"
    assert by["ivanov@company.ru"]["status"] == "skip" and "уже есть" in by["ivanov@company.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert any("смену пароля при первом входе сервер не требует" in w for w in pv["warnings"])
    assert any("не задан мастер-пароль" in w for w in pv["warnings"])
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **kw: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = k.by_login(D1, "new.user")
    assert new["fullName"] == "Новый Сотрудник" and new["isEnabled"] is True
    assert new["diskSizeLimit"] == {"isActive": True, "limit": {"value": 1, "units": "GigaBytes"}}
    assert len(k.passwords[new["id"]]) == 20
    assert k.contacts[new["id"]]["firstName"] == "Новый" and k.contacts[new["id"]]["surName"] == "Сотрудник"
    row = msstore.get_account(svc.db, srv.sid, "new.user@company.ru")
    assert row is not None and row["first_name"] == "Новый" and row["quota_kb"] == 1024 * 1024
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1
    assert k.passwords[new["id"]] not in transcripts(svc, srv.sid)


def test_create_existing_account_is_already_exists(srv):
    conn = msstore.get_server(srv.svc.db, srv.sid)
    drv = mss.driver(srv.svc, conn)
    try:
        with pytest.raises(AlreadyExists):
            drv.create_account("petrova@company.ru", first_name="Анна", last_name="Петрова", password="Zx9-qwerty-Lp")
        # учётная запись появилась между проверкой и созданием — сервер отвечает ошибкой 1001 в result.errors
        srv.k.hidden.add("kozlov")
        with pytest.raises(AlreadyExists, match="User kozlov already exists"):
            drv.create_account("kozlov@company.ru", first_name="", last_name="", password="Zx9-qwerty-Lp")
    finally:
        drv.close()


# -- мелочи --------------------------------------------------------------------------------------
def test_helpers():
    assert kmod.size_limit(2048 * 1024 ** 2) == {"isActive": True, "limit": {"value": 2, "units": "GigaBytes"}}
    assert kmod.size_limit(1536 * 1024 ** 2) == {"isActive": True, "limit": {"value": 1536, "units": "MegaBytes"}}
    assert kmod.size_limit(None)["isActive"] is False
    assert kmod.size_bytes({"value": 3, "units": "MegaBytes"}) == 3 * 1024 ** 2
    assert kmod.size_bytes({"value": 3, "units": "Strange"}) is None
    assert kmod.stamp_iso(LAST_LOGIN) == kmod.stamp_iso(LAST_LOGIN * 1000) == "2026-10-20T08:00:00Z"
    assert kmod.stamp_iso(0) == "" and kmod.stamp_iso(None) == ""
    assert kmod.session_cookies(["SESSION_CONNECT_WEBADMIN=ab; path=/admin/; secure; HttpOnly",
                                 "TOKEN_CONNECT_WEBADMIN=cd; path=/admin/"]) == \
        {"SESSION_CONNECT_WEBADMIN": "ab", "TOKEN_CONNECT_WEBADMIN": "cd"}
    assert kmod.error_text({"code": 1001, "message": "User %1 in %2 exists.",
                            "messageParameters": {"positionalParameters": ["jdoe", "company.ru"]}}) == \
        "User jdoe in company.ru exists."


def test_remove_waits_long_and_only_reads_are_repeatable(srv, monkeypatch):
    from mailarchiver.mailservers.http import HttpClient
    seen = []
    real = HttpClient.request

    def spy(self, method, path, **kw):
        seen.append((kw["json_body"].get("method"), kw.get("timeout"), kw.get("idempotent")))
        return real(self, method, path, **kw)
    monkeypatch.setattr(HttpClient, "request", spy)
    conn = msstore.get_server(srv.svc.db, srv.sid)
    with mss.session(srv.svc, conn, user="tester", kind="write") as drv:
        drv.delete_account("petrova@company.ru")
    assert kid("user", 2) not in srv.k.users
    flags = {}
    for method, timeout, idem in seen:
        flags.setdefault(method, (timeout, idem))
    # папка с письмами удаляется до ответа — ждём долго и не повторяем после 502/504
    assert flags["Users.remove"] == (kmod.DELETE_TIMEOUT_S, False)
    assert flags["Users.get"] == (None, True) and flags["Domains.get"] == (None, True)
    assert flags["Session.login"][1] is False and flags["Session.logout"][1] is False
