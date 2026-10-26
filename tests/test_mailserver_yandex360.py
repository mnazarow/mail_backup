"""Подключение «Яндекс 360 для бизнеса»: учебный API 360 (два хоста и сервер OAuth) и IMAP с токеном сотрудника.

Учебный сервер повторяет документированные методы: api360.yandex.net (сотрудники, домены, организации, правила
автоответа и пересылки, завершение сеансов, сервисные приложения; ошибки ``{"code","message","details"}``),
cloud-api.yandex.net (поиск сотрудника и подразделения Directory v2, псевдонимы и контакты Directory v1 с ошибками
``{"error","description","message","details"}``, настройки почты организации) и oauth.yandex.ru (обмен токена
сервисного приложения на токен сотрудника). Проверяется весь путь: подключение и проверка, сверка, действия раздела
«Ящики на сервере», создание, копирование ящика без пароля сотрудника и что секреты не попадают в журнал.
"""
import copy

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp, connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, NotFound, NotSupported
from mailarchiver.mailservers.drivers.yandex360 import RULE_AUTOREPLY, RULE_FORWARD, Yandex360Driver
from mailarchiver.mailservers.http import TOKENS, ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
ORG = 4242
TOKEN = "y0_AgAAAAadmin-Token-1"
CLIENT_ID = "svc-app-client-77"
CLIENT_SECRET = "svc-Secret~42"
DOMAIN = "romashka.ru"
DENIED = {"code": 7, "message": "Permission denied", "details": [{"@type": "type.googleapis.com/google.rpc.Help"}]}


def ycontact(type_, value, *, main=False, alias=False, synthetic=False, label=""):
    return {"type": type_, "value": value, "main": main, "alias": alias, "synthetic": synthetic, "label": label}


def yuser(uid, nick, first, last, *, dep=5, position="", aliases=(), enabled=True, admin=False, robot=False,
          dismissed=False, phone=""):
    email = f"{nick}@{DOMAIN}"
    contacts = [ycontact("email", email, main=True, synthetic=True)]
    contacts += [ycontact("email", f"{a}@{DOMAIN}", alias=True, synthetic=True) for a in aliases]
    if phone:
        contacts.append(ycontact("phone", phone, label="рабочий"))
    contacts.append(ycontact("site", "https://romashka.ru"))
    return {"id": uid, "nickname": nick, "departmentId": dep, "email": email,
            "name": {"first": first, "last": last, "middle": "Сергеевна" if first == "Анна" else ""},
            "gender": "", "position": position, "avatarId": "", "about": "", "birthday": "", "contacts": contacts,
            "aliases": list(aliases), "groups": [], "externalId": "", "isAdmin": admin, "isRobot": robot,
            "isDismissed": dismissed, "isEnabled": enabled, "timezone": "Europe/Moscow", "language": "ru",
            "createdAt": "2025-01-01T00:00:00Z", "updatedAt": "2025-01-01T00:00:00Z", "displayName": ""}


def v2(user):
    """Сотрудник в виде Directory v2 (snake_case, id — число)."""
    out = {"id": int(user["id"]), "nickname": user["nickname"], "department_id": user["departmentId"],
           "email": user["email"], "default_email": user["email"], "name": dict(user["name"]),
           "display_name": user.get("displayName", ""), "position": user["position"],
           "contacts": copy.deepcopy(user["contacts"]), "aliases": list(user["aliases"]),
           "is_admin": user["isAdmin"], "is_robot": user["isRobot"], "is_dismissed": user["isDismissed"],
           "is_2fa_enabled": False, "is_enabled": user["isEnabled"], "created_at": "2026-02-26T12:29:38.659354Z",
           "updated_at": "2026-02-26T15:29:38.659354+03:00"}
    return out


class Api360:
    """Состояние учебной организации Яндекс 360."""

    def __init__(self):
        self.users = {u["id"]: u for u in (
            yuser("1130000000000001", "ivanov", "Иван", "Иванов", position="Менеджер", aliases=["ivan"],
                  phone="+7 900 000-00-01"),
            yuser("1130000000000002", "petrova", "Анна", "Петрова"),
            yuser("1130000000000003", "admin", "Админ", "Главный", dep=1, admin=True),
            yuser("1130000000000004", "robot-tracker", "Робот", "Трекера", robot=True),
            yuser("1130000000000005", "old", "Бывший", "Сотрудник", dismissed=True),
            yuser("1130000000000006", "blocked", "Закрытый", "Вход", enabled=False),
        )}
        self.domains = [{"name": DOMAIN, "country": "ru", "mx": True, "delegated": True, "master": True,
                         "verified": True},
                        {"name": "romashka.com", "country": "ru", "mx": True, "delegated": False, "master": False,
                         "verified": True}]
        self.departments = {1: "Все сотрудники", 5: "Продажи", 7: "ИТ"}
        self.rules = {}
        self.rule_seq = 100
        self.passwords = {}
        self.logouts = []
        self.apps = [{"id": CLIENT_ID, "scopes": ["mail:imap_full"]}]
        self.owner = True
        self.enable_imap = True
        self.v2_search = True
        #: поиск v2 по адресу находит и владельца псевдонима (как может отвечать настоящий API)
        self.v2_alias_match = False
        self.exchanges = []
        self.per_page = 1000

    def by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    def taken(self, local):
        return any(u["nickname"] == local or local in u["aliases"] for u in self.users.values())


@pytest.fixture()
def y360(monkeypatch):
    TOKENS._tokens.clear()
    g = Api360()
    fake = FakeHttp()
    monkeypatch.setattr(Yandex360Driver, "API_URL", fake.url + "/api360")
    monkeypatch.setattr(Yandex360Driver, "CLOUD_URL", fake.url + "/cloud")
    monkeypatch.setattr(Yandex360Driver, "OAUTH_URL", fake.url + "/oauth")

    def guard(req, org=None):
        if req.headers.get("authorization") != f"OAuth {TOKEN}":
            return 401, {"code": 16, "message": "Unauthenticated", "details": []}
        if org is not None and int(org) != ORG:
            return 403, DENIED
        return None

    def user_or_404(uid):
        u = g.users.get(uid)
        if u is None:
            return None, (404, {"code": 5, "message": "Not found", "details": []})
        return u, None

    # -- api360.yandex.net ------------------------------------------------------------------
    @fake.route("GET", r"/api360/directory/v1/org")
    def orgs(req):
        return guard(req) or (200, {"organizations": [
            {"id": ORG, "name": "Ромашка", "email": "info@romashka.ru", "phone": "", "fax": "", "language": "ru",
             "subscriptionPlan": "business"},
            {"id": 77, "name": "Другая", "email": "", "phone": "", "fax": "", "language": "ru",
             "subscriptionPlan": "free"}], "nextPageToken": ""})

    @fake.route("GET", r"/api360/directory/v1/org/(\d+)/users")
    def users(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        per = min(int(req.q("perPage", "10")), g.per_page)
        page = int(req.q("page", "1"))
        items = [copy.deepcopy(u) for u in g.users.values()]
        pages = max(1, -(-len(items) // per))
        return 200, {"users": items[(page - 1) * per:page * per], "page": page, "pages": pages, "perPage": per,
                     "total": len(items)}

    @fake.route("GET", r"/api360/directory/v1/org/(\d+)/users/(\d+)")
    def one(req):
        bad = guard(req, req.group(1))
        u, err = user_or_404(req.group(2))
        return bad or err or (200, copy.deepcopy(u))

    @fake.route("PATCH", r"/api360/directory/v1/org/(\d+)/users/(\d+)")
    def patch(req):
        bad = guard(req, req.group(1))
        u, err = user_or_404(req.group(2))
        if bad or err:
            return bad or err
        body = req.json()
        for key, value in body.items():
            if key in ("password", "passwordChangeRequired"):
                g.passwords.setdefault(u["id"], {})[key] = value
            elif key == "name":
                if not value.get("first") or not value.get("last"):
                    return 400, {"code": 3, "message": "name.first and name.last are required", "details": []}
                u["name"] = value
            elif key in ("isEnabled", "displayName", "position", "departmentId"):
                if key == "departmentId" and value not in g.departments:
                    return 400, {"code": 3, "message": "department not found", "details": []}
                u[key] = value
            else:
                return 400, {"code": 3, "message": f"unexpected field {key}", "details": []}
        return 200, copy.deepcopy(u)

    @fake.route("POST", r"/api360/directory/v1/org/(\d+)/users")
    def create_user(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        body = req.json()
        for need in ("nickname", "password", "departmentId", "name"):
            if need not in body:
                return 400, {"code": 3, "message": f"{need} is required", "details": []}
        if g.taken(body["nickname"]):
            return 409, {"code": 6, "message": "user with this nickname already exists", "details": []}
        uid = str(1130000000000100 + len(g.users))
        u = yuser(uid, body["nickname"], body["name"]["first"], body["name"]["last"], dep=body["departmentId"])
        g.users[uid] = u
        g.passwords[uid] = {"password": body["password"], "passwordChangeRequired": body.get("passwordChangeRequired")}
        return 200, copy.deepcopy(u)

    @fake.route("GET", r"/api360/directory/v1/org/(\d+)/domains")
    def domains(req):
        bad = guard(req, req.group(1))
        per = min(int(req.q("perPage", "10")), 10)
        page = int(req.q("page", "1"))
        return bad or (200, {"domains": g.domains[(page - 1) * per:page * per], "page": page,
                             "pages": max(1, -(-len(g.domains) // per)), "perPage": per, "total": len(g.domains)})

    rules_rx = r"/api360/admin/v1/org/(\d+)/mail/users/(\d+)/settings/user_rules"

    @fake.route("GET", rules_rx)
    def get_rules(req):
        bad = guard(req, req.group(1))
        _u, err = user_or_404(req.group(2))
        if bad or err:
            return bad or err
        r = g.rules.setdefault(req.group(2), {"autoreplies": [], "forwards": []})
        return 200, copy.deepcopy(r)

    @fake.route("POST", rules_rx)
    def add_rule(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        body = req.json()
        r = g.rules.setdefault(req.group(2), {"autoreplies": [], "forwards": []})
        g.rule_seq += 1
        if "autoreply" in body:
            r["autoreplies"].append(dict(body["autoreply"], ruleId=g.rule_seq))
        if "forward" in body:
            fwd = body["forward"]
            if fwd["address"].split("@")[1] not in [d["name"] for d in g.domains]:
                return 400, {"code": 3, "message": "forward to external domain is not allowed", "details": []}
            r["forwards"].append(dict(fwd, ruleId=g.rule_seq))
        return 200, {"ruleId": g.rule_seq}

    @fake.route("DELETE", rules_rx + r"/(\d+)")
    def del_rule(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        r = g.rules.get(req.group(2)) or {"autoreplies": [], "forwards": []}
        rid = int(req.match.group(3))
        before = len(r["autoreplies"]) + len(r["forwards"])
        r["autoreplies"] = [x for x in r["autoreplies"] if x["ruleId"] != rid]
        r["forwards"] = [x for x in r["forwards"] if x["ruleId"] != rid]
        if before == len(r["autoreplies"]) + len(r["forwards"]):
            return 404, {"code": 5, "message": "rule not found", "details": []}
        return 200, {}

    @fake.route("PUT", r"/api360/security/v1/org/(\d+)/domain_sessions/users/(\d+)/logout")
    def logout(req):
        bad = guard(req, req.group(1))
        g.logouts.append(req.group(2))
        return bad or (200, {})

    @fake.route("GET", r"/api360/security/v1/org/(\d+)/service_applications")
    def apps(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        if not g.owner:
            return 403, {"code": 7, "message": "only organization owner can manage service applications",
                         "details": []}
        return 200, {"applications": g.apps}

    # -- cloud-api.yandex.net -----------------------------------------------------------------
    @fake.route("GET", r"/cloud/v1/directory/organizations/(\d+)/users")
    def v2_users(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        want = req.q("email")
        items = [v2(u) for u in g.users.values() if g.v2_search and (
            not want or u["email"] == want
            or (g.v2_alias_match and want.split("@")[0] in u["aliases"] and want.endswith("@" + DOMAIN)))]
        return 200, {"limit": 20, "offset": 0, "total": len(items), "items": items}

    @fake.route("GET", r"/cloud/v1/directory/organizations/(\d+)/departments")
    def v2_departments(req):
        bad = guard(req, req.group(1))
        if bad:
            return bad
        limit, offset = 2, int(req.q("offset", "0"))           # сервер отдаёт меньше, чем просили
        items = [{"id": k, "name": v, "parent_id": 1, "label": "", "description": "", "email": "", "aliases": [],
                  "members_count": 0, "removed": False, "created_at": "2025-01-01T00:00:00Z"}
                 for k, v in sorted(g.departments.items())]
        items.append({"id": 99, "name": "Старый отдел", "removed": True})
        return 200, {"limit": limit, "offset": offset, "total": len(items), "items": items[offset:offset + limit]}

    def v1_error(status, code, text):
        return status, {"error": code, "description": text, "message": text, "details": {}}

    @fake.route("POST", r"/cloud/v1/api360/directory/org/(\d+)/users/(\d+)/aliases")
    def add_alias(req):
        bad = guard(req, req.group(1))
        u, err = user_or_404(req.group(2))
        if bad or err:
            return bad or v1_error(404, "not_found", "Ресурс не найден")
        alias = req.json()["alias"]
        if "@" in alias or not alias:
            return v1_error(400, "invalid_argument", "Неверные параметры запроса")
        if g.taken(alias):
            return v1_error(409, "already_exists", "Конфликт - ресурс уже существует")
        u["aliases"].append(alias)
        u["contacts"].append(ycontact("email", f"{alias}@{DOMAIN}", alias=True, synthetic=True))
        out = copy.deepcopy(u)
        out.update({"id": int(u["id"]), "defaultEmail": u["email"], "is2faEnabled": False,
                    "isAgreementAccepted": True})
        return 200, out

    @fake.route("DELETE", r"/cloud/v1/api360/directory/org/(\d+)/users/(\d+)/aliases/([^/]+)")
    def del_alias(req):
        bad = guard(req, req.group(1))
        u, err = user_or_404(req.group(2))
        alias = req.group(3)
        if bad or err or alias not in u["aliases"]:
            return bad or v1_error(404, "not_found", "Ресурс не найден")
        u["aliases"].remove(alias)
        u["contacts"] = [c for c in u["contacts"] if c["value"] != f"{alias}@{DOMAIN}"]
        return 200, {"alias": alias, "removed": True}

    @fake.route("PUT", r"/cloud/v1/api360/directory/org/(\d+)/users/(\d+)/contacts")
    def put_contacts(req):
        bad = guard(req, req.group(1))
        u, err = user_or_404(req.group(2))
        if bad or err:
            return bad or v1_error(404, "not_found", "Ресурс не найден")
        given = req.json()["contacts"]
        for c in given:
            if set(c) - {"type", "value", "label", "isForCompletionKeyLink"}:
                return v1_error(400, "invalid_argument", "Неверные параметры запроса")
        auto = [c for c in u["contacts"] if c.get("synthetic")]
        u["contacts"] = auto + [ycontact(c["type"], c["value"], label=c.get("label", "")) for c in given]
        return 200, copy.deepcopy(u)

    @fake.route("GET", r"/cloud/v1/admin/org/(\d+)/mail-settings")
    def mail_settings(req):
        bad = guard(req, req.group(1))
        return bad or (200, {"enable_imap": g.enable_imap, "enable_pop": True, "force_default_display_name": False,
                             "friendly_org_users_is_external": True, "lock_employees_default_mail_domain": False,
                             "show_external_contact_notification_on_compose": True,
                             "show_external_contact_notification_on_read": True, "disable_disk_attachments": False})

    # -- oauth.yandex.ru ------------------------------------------------------------------------
    @fake.route("POST", r"/oauth/token")
    def exchange(req):
        form = req.form()
        g.exchanges.append(form)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return 400, {"error": "invalid_client", "error_description": "Client not found"}
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:token-exchange" or \
                form.get("subject_token_type") != "urn:yandex:params:oauth:token-type:email":
            return 400, {"error": "unsupported_grant_type", "error_description": "bad grant"}
        if g.by_email(form.get("subject_token")) is None:
            return 400, {"error": "invalid_grant", "error_description": "user not found"}
        return 200, {"access_token": f"ytok-{form['subject_token']}", "token_type": "bearer", "expires_in": 3600}

    g.fake = fake
    yield g
    fake.close()
    TOKENS._tokens.clear()


def connect(svc, **extra):
    values = {"type": "yandex360", "name": "Ромашка", "org_id": str(ORG), "token": TOKEN, "client_id": CLIENT_ID,
              "client_secret": CLIENT_SECRET, "allow_changes": True}
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
def ya(services, y360):
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    return type("Y", (), {"svc": services, "g": y360, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@{DOMAIN}", "pe": f"ms:{sid}:petrova@{DOMAIN}"})


def drv_for(**settings):
    conn = connection("yandex360", dict({"org_id": str(ORG)}, **settings),
                      {"token": TOKEN, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
    return Yandex360Driver(conn, ApiLog())


def imap_world(monkeypatch):
    world = ImapWorld()
    world.mailbox(f"petrova@{DOMAIN}", "nopass")
    world.add(f"petrova@{DOMAIN}", "INBOX", raw_message("Счёт от поставщика"))
    world.tokens[f"ytok-petrova@{DOMAIN}"] = {f"petrova@{DOMAIN}"}
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return world


# ---------------------------------------------------------------------------
#  Подключение и проверка
# ---------------------------------------------------------------------------
def test_check_reads_org_domains_and_service_app(services, y360):
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    details = "\n".join(st["details"])
    assert "Ромашка" in details and "Тариф: business" in details and "romashka.ru (основной)" in details
    assert "Сотрудников: 6" in details and "Сервисное приложение зарегистрировано: mail:imap_full" in details
    assert "Токен сотрудника для IMAP получен" in details
    assert not st["problems"], st["problems"]
    # каждый метод — на том хосте, который указан в его документации
    fake = y360.fake
    assert fake.calls_to("GET", rf"/api360/directory/v1/org/{ORG}/domains")
    assert fake.calls_to("GET", rf"/cloud/v1/admin/org/{ORG}/mail-settings")
    assert y360.exchanges and y360.exchanges[0]["subject_token"].endswith("@" + DOMAIN)
    text = transcripts(services, sid)
    assert TOKEN not in text and CLIENT_SECRET not in text and "ytok-" not in text


def test_wrong_token_is_explained_and_not_logged(services, y360):
    sid = connect(services, token="wrong-Token-9")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "OAuth-токен администратора не принят" in out["error"], out
    assert "Unauthenticated" in out["error"]
    text = transcripts(services, sid)
    assert "wrong-Token-9" not in text and TOKEN not in text and CLIENT_SECRET not in text


def test_wrong_org_lists_organizations_of_token(services, y360):
    sid = connect(services, org_id="99")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "организации 99 нет" in out["error"] and f"{ORG} — Ромашка" in out["error"]


def test_settings_are_validated(services, y360):
    with pytest.raises(ValidationError, match="число"):
        connect(services, org_id="romashka")
    with pytest.raises(ValidationError, match="ClientID, и Client secret"):
        connect(services, client_secret="")


def test_check_reports_service_app_and_imap_problems(services, y360):
    y360.apps = [{"id": "other-app", "scopes": ["cloud_api:disk.read"]}]
    y360.enable_imap = False
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    problems = {p[0]: p for p in out["status"]["problems"]}
    assert not out["ok"] and problems["service_app"][1] == "error"
    assert "не зарегистрировано" in problems["service_app"][2]
    assert "доступ по IMAP выключен" in problems["imap_off"][2]
    # список приложений видит только владелец — тогда проверяется сам обмен токена
    y360.apps = [{"id": CLIENT_ID, "scopes": ["mail:imap_full"]}]
    y360.owner = False
    y360.enable_imap = True
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    assert any("Токен сотрудника для IMAP получен" in d for d in out["status"]["details"])
    # без сервисного приложения — предупреждение
    sid2 = connect(services, name="Без приложения", client_id="", client_secret="")
    msstore.update_server(services.db, sid2, name="Без приложения", settings=msstore.get_server(services.db, sid2)
                          .settings, secrets={"token": TOKEN}, sync_cron="50 4 * * *", enabled=True,
                          allow_changes=True, sync_enabled=True, sync_add_new=False, monitor=True)
    out = mss.check(services, sid2, user="tester")
    assert out["ok"] and any(p[0] == "service_app" and p[1] == "warning" for p in out["status"]["problems"])


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_normalizes_employees(ya):
    rows = {r["email"]: r for r in msstore.list_accounts(ya.svc.db, ya.sid)}
    # роботы и уволенные — не ящики сотрудников
    assert set(rows) == {f"ivanov@{DOMAIN}", f"petrova@{DOMAIN}", f"admin@{DOMAIN}", f"blocked@{DOMAIN}"}
    iv = rows[f"ivanov@{DOMAIN}"]
    assert iv["aliases"] == [f"ivan@{DOMAIN}"] and iv["login_blocked"] is False
    assert iv["department"] == "Продажи" and iv["title"] == "Менеджер" and iv["phone"] == "+7 900 000-00-01"
    assert iv["native_id"] == "1130000000000001" and iv["kind"] == "user" and iv["first_name"] == "Иван"
    assert iv["used_bytes"] is None and iv["quota_kb"] is None and iv["last_login"] == ""
    assert rows[f"admin@{DOMAIN}"]["kind"] == "admin" and rows[f"admin@{DOMAIN}"]["department"] == "Все сотрудники"
    assert rows[f"blocked@{DOMAIN}"]["login_blocked"] is True
    assert ya.sync["incomplete"] is False and not ya.sync["problems"]
    # подразделения — Directory v2 на актуальном хосте, постранично по тому, сколько отдал сервер
    assert len(ya.g.fake.calls_to("GET", rf"/cloud/v1/directory/organizations/{ORG}/departments")) == 2
    assert TOKEN not in transcripts(ya.svc, ya.sid)


def test_sync_reads_all_pages(services, y360):
    y360.per_page = 2
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 4 and not res["incomplete"]
    assert len(y360.fake.calls_to("GET", rf"/api360/directory/v1/org/{ORG}/users")) == 3


def test_get_account_without_known_id_uses_directory_v2(y360):
    drv = drv_for()
    acc = drv.get_account(f"ivanov@{DOMAIN}")
    assert acc["native_id"] == "1130000000000001" and acc["aliases"] == [f"ivan@{DOMAIN}"]
    assert acc["department"] == "Продажи" and acc["autoreply"] is False and acc["forward_to"] == []
    assert acc["info"]["2fa"] is False
    assert y360.fake.calls_to("GET", rf"/cloud/v1/directory/organizations/{ORG}/users")
    # поиск v2 ничего не дал — полный список
    y360.v2_search = False
    assert drv_for().get_account(f"petrova@{DOMAIN}")["first_name"] == "Анна"
    assert drv_for().get_account(f"robot-tracker@{DOMAIN}") is None
    assert drv_for().get_account(f"nobody@{DOMAIN}") is None


@pytest.mark.parametrize("v2_search, v2_alias_match", [(True, True), (True, False), (False, False)])
def test_alias_address_never_resolves_to_its_owner(y360, v2_search, v2_alias_match):
    """ivan@ — псевдоним ivanov@: ни чтение, ни изменения по нему не доходят до ivanov (поиск v2, полный список)."""
    y360.v2_search, y360.v2_alias_match = v2_search, v2_alias_match
    drv = drv_for()
    assert drv.get_account(f"ivan@{DOMAIN}") is None
    for call in (lambda: drv.set_login(f"ivan@{DOMAIN}", False),
                 lambda: drv.set_password(f"ivan@{DOMAIN}", "Qwerty-12345x"),
                 lambda: drv.set_contact(f"ivan@{DOMAIN}", {"title": "Директор"}),
                 lambda: drv.set_forwarding(f"ivan@{DOMAIN}", addresses=[]),
                 lambda: drv.add_alias(f"ivan@{DOMAIN}", f"ivan2@{DOMAIN}")):
        with pytest.raises(NotFound):
            call()
    iv = y360.users["1130000000000001"]
    assert iv["isEnabled"] is True and iv["position"] == "Менеджер" and iv["aliases"] == ["ivan"]
    assert "1130000000000001" not in y360.passwords and not y360.logouts
    # основной адрес — находится как прежде
    assert drv.get_account(f"ivanov@{DOMAIN}")["native_id"] == "1130000000000001"


def test_known_id_with_other_address_is_not_used(y360):
    """Номер из сверки, у которого теперь другой основной адрес, — не тот сотрудник."""
    drv = drv_for()
    drv.list_accounts()
    y360.users["1130000000000002"]["email"] = f"sidorova@{DOMAIN}"         # адрес сменили в консоли
    y360.users["1130000000000002"]["aliases"] = ["petrova"]
    assert drv.get_account(f"petrova@{DOMAIN}") is None
    with pytest.raises(NotFound):
        drv.set_login(f"petrova@{DOMAIN}", False)
    assert y360.users["1130000000000002"]["isEnabled"] is True


# ---------------------------------------------------------------------------
#  Действия раздела «Ящики на сервере»
# ---------------------------------------------------------------------------
def test_block_password_must_change_unblock(ya):
    svc, g = ya.svc, ya.g
    _res, items = act(svc, "ax_block", [ya.pe], final_copy=False, keep_receiving=False)
    assert items[ya.pe]["status"] == "success", items
    assert g.users["1130000000000002"]["isEnabled"] is False and g.logouts == ["1130000000000002"]
    assert "сеансы на всех устройствах завершены" in items[ya.pe]["detail"]
    assert "приём писем API 360 при закрытом входе не останавливает" in items[ya.pe]["detail"]
    assert msstore.get_account(svc.db, ya.sid, f"petrova@{DOMAIN}")["login_blocked"] is True
    _res, items = act(svc, "ax_password", [ya.iv], mode="random_hidden", must_change=True)
    assert items[ya.iv]["status"] == "success", items
    pw = g.passwords["1130000000000001"]
    assert pw["passwordChangeRequired"] is True and len(pw["password"]) == 20
    # общий пароль, который Яндекс 360 не примет, — пропуск с причиной
    pv = base.run(svc, ADMIN, "ax_password", [ya.iv], {"mode": "same", "password": "simplepass"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "все три вида" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_password", [ya.iv], {"mode": "same", "password": "Пароль-2026!"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "латинские буквы" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_must_change", [ya.pe], value="no")
    assert items[ya.pe]["status"] == "success" and g.passwords["1130000000000002"]["passwordChangeRequired"] is False
    _res, items = act(svc, "ax_unblock", [ya.pe])
    assert items[ya.pe]["status"] == "success" and g.users["1130000000000002"]["isEnabled"] is True
    assert pw["password"] not in transcripts(svc, ya.sid)


def test_contact_from_employees_and_fixed(ya):
    svc, g = ya.svc, ya.g
    svc.db.create_employee(full_name="Петрова Анна", email=f"petrova@{DOMAIN}", position="Бухгалтер",
                           department="ИТ", phone="+7 900 111-22-33")
    _res, items = act(svc, "ax_contact", [ya.pe], source="employees", overwrite=True, name_order="last_first",
                      phone=True)
    assert items[ya.pe]["status"] == "success", items
    assert "сервер показал другое" not in items[ya.pe]["detail"]
    pe = g.users["1130000000000002"]
    assert pe["position"] == "Бухгалтер" and pe["departmentId"] == 7
    phones = [c["value"] for c in pe["contacts"] if c["type"] == "phone"]
    assert phones == ["+7 900 111-22-33"]
    # контакт «сайт» остался, автоматические (основной адрес) — не трогались
    assert any(c["type"] == "site" for c in pe["contacts"]) and any(c.get("main") for c in pe["contacts"])
    row = msstore.get_account(svc.db, ya.sid, f"petrova@{DOMAIN}")
    assert row["department"] == "ИТ" and row["title"] == "Бухгалтер" and row["phone"] == "+7 900 111-22-33"
    # отдел из фиксированного значения; такого подразделения нет — пропуск с причиной, ничего не меняется
    _res, items = act(svc, "ax_contact", [ya.iv], source="fixed", department="Склад", overwrite=True)
    assert items[ya.iv]["status"] == "failed" and "подразделения «Склад» в организации нет" in items[ya.iv]["detail"]
    assert g.users["1130000000000001"]["departmentId"] == 5


def test_contact_names_keep_middle_name(y360):
    drv = drv_for()
    drv.set_contact(f"petrova@{DOMAIN}", {"last_name": "Сидорова", "display_name": "Анна С."})
    pe = y360.users["1130000000000002"]
    assert pe["name"] == {"first": "Анна", "last": "Сидорова", "middle": "Сергеевна"}
    assert pe["displayName"] == "Анна С."
    with pytest.raises(ValidationError, match="имя и фамилия обязательны"):
        drv.set_contact(f"petrova@{DOMAIN}", {"first_name": ""})


def test_aliases_add_and_remove(ya):
    svc, g = ya.svc, ya.g
    _res, items = act(svc, "ax_alias_add", [ya.pe], template="{f}.{last}")
    assert items[ya.pe]["status"] == "success", items
    assert g.users["1130000000000002"]["aliases"] == ["a.petrova"]       # в API — имя без домена
    assert msstore.get_account(svc.db, ya.sid, f"petrova@{DOMAIN}")["aliases"] == [f"a.petrova@{DOMAIN}"]
    assert g.fake.calls_to("POST", rf"/cloud/v1/api360/directory/org/{ORG}/users/1130000000000002/aliases")
    _res, items = act(svc, "ax_alias_remove", [ya.iv], which="list", aliases="ivan")
    assert items[ya.iv]["status"] == "success", items
    assert g.users["1130000000000001"]["aliases"] == []
    assert msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")["aliases"] == []
    drv = drv_for()
    with pytest.raises(AlreadyExists, match="уже занят"):
        drv.add_alias(f"ivanov@{DOMAIN}", f"petrova@{DOMAIN}")
    with pytest.raises(ValidationError, match="только в домене учётной записи"):
        drv.add_alias(f"ivanov@{DOMAIN}", "ivan@romashka.com")


def test_autoreply_and_forwarding(ya):
    svc, g = ya.svc, ya.g
    uid = "1130000000000001"
    g.rules[uid] = {"autoreplies": [{"ruleId": 1, "ruleName": "моё", "text": "Старый"}],
                    "forwards": [{"ruleId": 2, "ruleName": "моя", "address": f"admin@{DOMAIN}", "withStore": True}]}
    _res, items = act(svc, "srv_autoreply", [ya.iv], mode="on", subject="Нет на месте",
                      text="Я больше не работаю.\nПишите petrova@romashka.ru", start="", end="")
    assert items[ya.iv]["status"] == "success", items
    auto = g.rules[uid]["autoreplies"]
    assert len(auto) == 1 and auto[0]["ruleName"] == RULE_AUTOREPLY
    assert auto[0]["text"].startswith("Нет на месте\n\nЯ больше не работаю.")
    assert "тема — первой строкой" in items[ya.iv]["detail"]
    assert msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")["autoreply"] is True
    # период API 360 не задаёт — пропуск с причиной, ничего не меняется
    _res, items = act(svc, "srv_autoreply", [ya.iv], mode="on", subject="", text="Отпуск", start="2026-11-01",
                      end="2026-11-10")
    assert items[ya.iv]["status"] == "skipped" and "на период" in items[ya.iv]["detail"]
    assert g.rules[uid]["autoreplies"][0]["text"].startswith("Нет на месте")
    # пересылка — только на домены организации
    _res, items = act(svc, "srv_forwarding", [ya.iv], mode="on", addresses=f"petrova@{DOMAIN}", keep_copy=True)
    assert items[ya.iv]["status"] == "success", items
    fwd = g.rules[uid]["forwards"]
    assert [(f["address"], f["withStore"], f["ruleName"]) for f in fwd] == [(f"petrova@{DOMAIN}", True, RULE_FORWARD)]
    assert msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")["forward_to"] == [f"petrova@{DOMAIN}"]
    _res, items = act(svc, "srv_forwarding", [ya.iv], mode="on", addresses="boss@gmail.com", keep_copy=True)
    assert items[ya.iv]["status"] == "skipped" and "только на адреса доменов организации" in items[ya.iv]["detail"]
    assert g.rules[uid]["forwards"][0]["address"] == f"petrova@{DOMAIN}"
    drv = drv_for()
    assert drv.get_forwarding(f"ivanov@{DOMAIN}") == {"addresses": [f"petrova@{DOMAIN}"], "keep_copy": True}
    assert drv.get_autoreply(f"ivanov@{DOMAIN}")["enabled"] is True
    _res, items = act(svc, "srv_forwarding", [ya.iv], mode="off", addresses="", keep_copy=True)
    _res, items = act(svc, "srv_autoreply", [ya.iv], mode="off")
    assert items[ya.iv]["status"] == "success"
    assert g.rules[uid] == {"autoreplies": [], "forwards": []}
    row = msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")
    assert row["autoreply"] is False and row["forward_to"] == []


def test_capabilities_skip_with_reason(ya):
    svc = ya.svc
    pv = base.run(svc, ADMIN, "ax_quota", [ya.iv], {"quota_mb": 2048}, preview=True)
    assert pv["counts"]["skip"] == 1 and "квота ящика — этот сервер так не умеет" in pv["results"][0]["detail"]
    msstore.update_fields(svc.db, ya.sid, f"petrova@{DOMAIN}", login_blocked=True)
    pv = base.run(svc, ADMIN, "ax_delete", [ya.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "удаление — этот сервер так не умеет" in pv["results"][0]["detail"]


# ---------------------------------------------------------------------------
#  Копирование без пароля сотрудника, удаление в консоли и передача адреса коллеге
# ---------------------------------------------------------------------------
def test_add_mailbox_then_copy_with_service_app_token(ya, monkeypatch):
    svc, g = ya.svc, ya.g
    world = imap_world(monkeypatch)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [ya.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == f"petrova@{DOMAIN}")
    assert acc.auth_type == AuthType.SERVER and acc.server_id == ya.sid
    assert acc.host == "imap.yandex.ru" and acc.port == 993 and acc.password == ""
    from mailarchiver.imap.client import ImapConnection
    for _ in range(2):
        conn = ImapConnection(acc, svc.connect_options())
        conn.connect()
        try:
            assert world.last_login == ("xoauth2", f"petrova@{DOMAIN}", f"petrova@{DOMAIN}")
        finally:
            conn.close()
    # токен сотрудника действует час — второй вход без нового обмена
    assert [e["subject_token"] for e in g.exchanges] == [f"petrova@{DOMAIN}"]
    assert g.exchanges[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    # токен — только на IMAP-сервер подключения
    acc.host = "imap.evil.ru"
    with pytest.raises(Exception, match="входят только на imap.yandex.ru"):
        mss.imap_auth_for(svc, acc)


def test_imap_token_errors_are_explained(y360):
    conn = connection("yandex360", {"org_id": str(ORG)}, {"token": TOKEN, "client_id": CLIENT_ID,
                                                          "client_secret": "wrong-Secret"})
    drv = Yandex360Driver(conn, ApiLog())
    with pytest.raises(Exception, match="сервисного приложения") as exc:
        drv.imap_auth(f"petrova@{DOMAIN}")
    assert "invalid_client" in str(exc.value.message)
    no_app = Yandex360Driver(connection("yandex360", {"org_id": str(ORG)}, {"token": TOKEN}), ApiLog())
    with pytest.raises(Exception, match="сервисное приложение не задано"):
        no_app.imap_auth(f"petrova@{DOMAIN}")


def test_no_service_app_means_no_password_free_mailboxes(services, y360):
    """Без ClientID и Client secret сервисного приложения ящики через подключение не заводятся (и не сверяются)."""
    assert drv_for().imap_admin_problem() == ""
    half = Yandex360Driver(connection("yandex360", {"org_id": str(ORG)}, {"token": TOKEN, "client_id": CLIENT_ID}),
                           ApiLog())
    assert "сервисное приложение" in half.imap_admin_problem()
    sid = connect(services, client_id="", client_secret="")
    assert not msstore.get_server(services.db, sid).secrets.get("client_id")
    mss.sync(services, sid, user="tester")
    pv = base.run(services, ADMIN, "ax_add_mailboxes", [f"ms:{sid}:petrova@{DOMAIN}"], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "не задано сервисное приложение" in pv["results"][0]["detail"], pv
    from mailarchiver.mailservers.mailboxes import add_mailboxes
    items = add_mailboxes(services, msstore.get_server(services.db, sid), [f"petrova@{DOMAIN}"], user="tester")
    assert items[0]["status"] == "skip" and "не задано сервисное приложение" in items[0]["text"]
    assert not any(a.username == f"petrova@{DOMAIN}" for a in services.db.list_accounts())


def test_deleted_in_console_then_address_to_colleague(ya, monkeypatch):
    svc, g = ya.svc, ya.g
    # удаляют в консоли admin.yandex.ru (API этого не умеет); сверка помечает учётную запись удалённой
    g.users.pop("1130000000000002")
    mss.sync(svc, ya.sid, user="tester")
    assert msstore.get_account(svc.db, ya.sid, f"petrova@{DOMAIN}")["deleted_at"]
    pv = base.run(svc, ADMIN, "ax_forward", [ya.pe], {"to": f"ivanov@{DOMAIN}"}, preview=True)
    assert pv["counts"]["ok"] == 1, pv
    _res, items = act(svc, "ax_forward", [ya.pe], to=f"ivanov@{DOMAIN}", with_aliases=True)
    assert items[ya.pe]["status"] == "success", items
    assert "petrova" in g.users["1130000000000001"]["aliases"]
    row = msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")
    assert f"petrova@{DOMAIN}" in row["aliases"]
    # «Перечитать» удалённую — её адрес теперь псевдоним коллеги, но это не она: остаётся удалённой
    _res, items = act(svc, "ax_refresh", [ya.pe])
    assert "больше нет" in items[ya.pe]["detail"], items
    assert msstore.get_account(svc.db, ya.sid, f"petrova@{DOMAIN}")["deleted_at"]


def test_address_moved_to_colleague_before_sync_is_not_changed(ya):
    """Удалили в консоли и сразу отдали адрес коллеге, сверки ещё не было: действия по старой строке не трогают
    коллегу."""
    svc, g = ya.svc, ya.g
    g.users.pop("1130000000000002")
    g.users["1130000000000001"]["aliases"].append("petrova")
    g.v2_alias_match = True
    _res, items = act(svc, "ax_block", [ya.pe], final_copy=False, keep_receiving=True)
    assert items[ya.pe]["status"] != "success" and "petrova@romashka.ru нет" in items[ya.pe]["detail"], items
    _res, items = act(svc, "ax_password", [ya.pe], mode="random_hidden")
    assert items[ya.pe]["status"] != "success", items
    assert g.users["1130000000000001"]["isEnabled"] is True and not g.logouts
    assert "1130000000000001" not in g.passwords
    assert msstore.get_account(svc.db, ya.sid, f"ivanov@{DOMAIN}")["login_blocked"] is False


# ---------------------------------------------------------------------------
#  Создание учётных записей
# ---------------------------------------------------------------------------
def test_create_accounts_on_connection(ya, monkeypatch):
    svc, g = ya.svc, ya.g
    world = imap_world(monkeypatch)
    params = {"server_id": ya.sid, "text": f"new.user@{DOMAIN}; Новый; Сотрудник\nivanov@{DOMAIN}\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 100, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by[f"new.user@{DOMAIN}"]["status"] == "ok"
    assert by[f"ivanov@{DOMAIN}"]["status"] == "skip" and "уже есть" in by[f"ivanov@{DOMAIN}"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert any("квоту через API не задать" in w for w in pv["warnings"])
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = g.by_email(f"new.user@{DOMAIN}")
    assert new["nickname"] == "new.user" and new["departmentId"] == 1
    assert new["name"] == {"first": "Новый", "last": "Сотрудник", "middle": ""}
    assert g.passwords[new["id"]]["passwordChangeRequired"] is True
    row = msstore.get_account(svc.db, ya.sid, f"new.user@{DOMAIN}")
    assert row is not None and row["first_name"] == "Новый" and row["native_id"] == new["id"]
    acc = next(a for a in svc.db.list_accounts() if a.username == f"new.user@{DOMAIN}")
    assert acc.auth_type == AuthType.SERVER and acc.host == "imap.yandex.ru"
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1
    assert world.boxes                                  # ящик в архиве заведён — вход по токену сотрудника


def test_create_refuses_existing_alias_domain_and_missing_names(y360):
    drv = drv_for()
    with pytest.raises(AlreadyExists, match="уже есть"):
        drv.create_account(f"ivanov@{DOMAIN}", first_name="И", last_name="И", password="Xx-12345678")
    # адрес — псевдоним другого сотрудника: занят, с именем владельца
    for search in (True, False):
        y360.v2_search = search
        with pytest.raises(AlreadyExists, match=f"псевдоним учётной записи ivanov@{DOMAIN}"):
            drv_for().create_account(f"ivan@{DOMAIN}", first_name="И", last_name="И", password="Xx-12345678")
    y360.v2_search = True
    # поиск не нашёл, но сервер ответил «уже есть» (409) — тоже AlreadyExists
    y360.v2_search = False
    y360.users["1130000000000001"]["email"] = "renamed@romashka.ru"   # список не найдёт по адресу
    with pytest.raises(AlreadyExists):
        drv_for().create_account(f"ivanov@{DOMAIN}", first_name="И", last_name="И", password="Xx-12345678")
    with pytest.raises(ValidationError, match="основном домене romashka.ru"):
        drv.create_account("new@romashka.com", first_name="А", last_name="Б", password="Xx-12345678")
    with pytest.raises(ValidationError, match="нужны имя и фамилия"):
        drv.create_account(f"nameless@{DOMAIN}", first_name="", last_name="", password="Xx-12345678")
    with pytest.raises(ValidationError, match="все три вида"):
        drv.create_account(f"weak@{DOMAIN}", first_name="А", last_name="Б", password="onlyletters")


def test_driver_contract(y360):
    desc = Yandex360Driver.describe()
    assert desc["cloud"] and desc["imap_default"] == ["imap.yandex.ru", 993, "ssl"]
    assert {"usage", "quota", "last_login", "delete"}.isdisjoint(desc["caps"])
    assert {"block", "password", "must_change", "aliases", "autoreply", "forward", "imap_admin", "create"} <= \
        set(desc["caps"])
    secret_fields = {f["key"] for f in desc["fields"] if f["secret"]}
    assert secret_fields == {"token", "client_id", "client_secret"}
    assert Yandex360Driver.API_URL.endswith("/api360") and Yandex360Driver.CLOUD_URL.endswith("/cloud")
    # IMAP-сервер можно выбрать только из серверов Яндекса
    drv = drv_for(imap_host="imap.yandex.com")
    assert drv.imap_server() == ("imap.yandex.com", 993, "ssl")
    assert drv.imap_auth(f"petrova@{DOMAIN}").host == "imap.yandex.com"
    with pytest.raises(NotSupported):
        drv.set_quota(f"petrova@{DOMAIN}", 1024)
