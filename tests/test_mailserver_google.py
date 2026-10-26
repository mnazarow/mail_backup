"""Подключение Google Workspace: учебные Admin SDK, Reports API, Gmail API и сервер токенов (tests/mailserver_fake.py)
и IMAP с токеном сервисного аккаунта (tests/imap_fake.py).

Сервер токенов проверяет подпись JWT открытым ключом сервисного аккаунта и выдаёт токены только с областями из
«Делегирования домена»; каждый API проверяет область и от чьего имени токен. Проверяется весь путь: подключение и
проверка (с понятными причинами для недостающих областей), сверка (пользователи, псевдонимы, приостановка, последний
вход, занятое место из отчёта), действия раздела через драйвер, удаление только при полном архиве и передача адреса
коллеге псевдонимом, создание учётных записей и то, что ключ и токены не попадают в журнал подключения.
"""
import base64
import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp, connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, Cap, NotSupported, ServerError
from mailarchiver.mailservers.drivers.google import (SCOPE_BASIC, SCOPE_CUSTOMER, SCOPE_MAIL, SCOPE_REPORTS,
                                                     SCOPE_SHARING, SCOPE_USER, SCOPE_USER_RO, GoogleDriver)
from mailarchiver.mailservers.http import TOKENS, ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
SA_EMAIL = "archiver@archiver-test.iam.gserviceaccount.com"
CLIENT_ID = "104857600000000000001"
CUSTOMER_ID = "C03az79cb"
MB = 1024 * 1024
FULL = {SCOPE_USER, SCOPE_CUSTOMER, SCOPE_REPORTS, SCOPE_BASIC, SCOPE_SHARING, SCOPE_MAIL}
GMAIL_READ = {SCOPE_BASIC, SCOPE_MAIL, "https://www.googleapis.com/auth/gmail.modify",
              "https://www.googleapis.com/auth/gmail.readonly"}
PST = timezone(timedelta(hours=-8))


def make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("ascii")
    return key, pem


KEY, PEM = make_key()
_OTHER_KEY, OTHER_PEM = make_key()


def sa_json(pem=PEM):
    return json.dumps({"type": "service_account", "project_id": "archiver-test", "private_key_id": "kid-1",
                       "private_key": pem, "client_email": SA_EMAIL, "client_id": CLIENT_ID,
                       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                       "token_uri": "https://oauth2.googleapis.com/token"}, indent=2)


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def err(code, message, reason):
    status = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
              409: "ALREADY_EXISTS"}.get(code, "UNKNOWN")
    return code, {"error": {"code": code, "message": message, "status": status,
                            "errors": [{"domain": "global", "reason": reason, "message": message}]}}


def guser(uid, email, first, last, *, admin=False, suspended=False, aliases=(), last_login="1970-01-01T00:00:00.000Z",
          orgs=(), phones=()):
    local, domain = email.split("@")
    out = {"kind": "admin#directory#user", "id": uid, "etag": '"e1"', "primaryEmail": email,
           "name": {"givenName": first, "familyName": last, "fullName": f"{first} {last}"},
           "isAdmin": admin, "isDelegatedAdmin": False, "lastLoginTime": last_login,
           "creationTime": "2024-01-15T10:00:00.000Z", "agreedToTerms": True, "suspended": suspended,
           "archived": False, "changePasswordAtNextLogin": False, "ipWhitelisted": False,
           "emails": [{"address": email, "primary": True}] + [{"address": a} for a in aliases],
           "aliases": list(aliases), "nonEditableAliases": [f"{local}@{domain}.test-google-a.com"],
           "organizations": [dict(o) for o in orgs], "phones": [dict(p) for p in phones],
           "customerId": CUSTOMER_ID, "orgUnitPath": "/", "isMailboxSetup": True, "isEnrolledIn2Sv": False,
           "isEnforcedIn2Sv": False, "includeInGlobalAddressList": True}
    if suspended:
        out["suspensionReason"] = "ADMIN"
    return out


def shown(user):
    """Как Google отдаёт пользователя: пустые списки не приходят."""
    out = copy.deepcopy(user)
    for key in ("aliases", "organizations", "phones"):
        if not out.get(key):
            out.pop(key, None)
    return out


class Google:
    """Состояние учебной организации Google Workspace."""

    def __init__(self):
        self.domains = ["company.ru", "filial.ru"]
        self.users = {u["id"]: u for u in (
            guser("100", "admin@company.ru", "Анна", "Админова", admin=True, last_login="2026-10-25T07:00:00.000Z"),
            guser("101", "ivanov@company.ru", "Иван", "Иванов", aliases=["ivan@company.ru"],
                  last_login="2026-10-20T08:12:44.000Z",
                  orgs=[{"title": "Менеджер", "department": "Продажи", "primary": True, "type": "work"}],
                  phones=[{"value": "+7 495 000-00-01", "type": "work", "primary": True},
                          {"value": "+7 900 000-00-01", "type": "mobile"}]),
            guser("102", "petrova@company.ru", "Анна", "Петрова", aliases=["anna@company.ru"]),
            guser("103", "sidorov@company.ru", "Пётр", "Сидоров", suspended=True,
                  last_login="2026-09-01T10:00:00.000Z"),
            guser("104", "smirnov@filial.ru", "Олег", "Смирнов", last_login="2026-10-24T12:00:00.000Z"),
        )}
        #: области «Делегирования домена» для сервисного аккаунта
        self.dwd = set(FULL)
        self.tokens = {}                            # токен → (от чьего имени, область)
        self.issued = []
        self.jwts = []                              # (заголовок, утверждения) принятых JWT
        self.passwords = {}                         # id → (значение, hashFunction)
        self.vacation = {}
        self.autofwd = {}
        self.fwd = {}                               # адрес → {куда: статус}
        self.usage = {"admin@company.ru": (10, 20, 30720, 10), "ivanov@company.ru": (1500, 2048, 30720, 548),
                      "petrova@company.ru": (300, 300, 30720, 0), "sidorov@company.ru": (50, 60, 30720, 10),
                      "smirnov@filial.ru": (5, 5, 30720, 0)}
        #: за какой день (PST) отчёт уже готов
        self.ready = (datetime.now(PST).date() - timedelta(days=2)).isoformat()
        self.page = 2
        self.imap_for_suspended = True
        self.deleted = []
        self.n = 0

    def find(self, key):
        key = (key or "").lower()
        for u in self.users.values():
            if u["id"] == key or u["primaryEmail"] == key or key in u.get("aliases", []):
                return u
        return None

    def taken(self, address):
        return any(address == u["primaryEmail"] or address in u.get("aliases", []) for u in self.users.values())


class LiveTokens(dict):
    """Токены XOAUTH2 для ImapWorld: пускают в ящик того, от чьего имени выданы (приостановленного — по настройке)."""

    def __init__(self, g):
        super().__init__()
        self.g = g

    def get(self, token, default=None):
        sub, scope = self.g.tokens.get(token, (None, None))
        if scope != SCOPE_MAIL:
            return default
        user = self.g.find(sub)
        if user is None or (user["suspended"] and not self.g.imap_for_suspended):
            return default
        return {sub}


@pytest.fixture()
def gw(monkeypatch):
    TOKENS._tokens.clear()
    g = Google()
    fake = FakeHttp()
    monkeypatch.setattr(GoogleDriver, "TOKEN_URL", fake.url + "/token")
    monkeypatch.setattr(GoogleDriver, "DIRECTORY_URL", fake.url + "/admin/directory/v1")
    monkeypatch.setattr(GoogleDriver, "REPORTS_URL", fake.url + "/admin/reports/v1")
    monkeypatch.setattr(GoogleDriver, "GMAIL_URL", fake.url + "/gmail/v1")
    public = KEY.public_key()

    @fake.route("POST", r"/token")
    def token(req):
        form = req.form()
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return 400, {"error": "unsupported_grant_type", "error_description": "Invalid grant_type"}
        head_b, claims_b, sig_b = form["assertion"].split(".")
        try:
            public.verify(b64d(sig_b), f"{head_b}.{claims_b}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
        head, claims = json.loads(b64d(head_b)), json.loads(b64d(claims_b))
        if head.get("alg") != "RS256" or head.get("typ") != "JWT":
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT header"}
        if claims.get("aud") != fake.url + "/token" or claims.get("iss") != SA_EMAIL:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT: aud or iss"}
        if not 0 < claims["exp"] - claims["iat"] <= 3600:
            return 400, {"error": "invalid_grant", "error_description": "Invalid JWT: Token must be a short-lived "
                                                                       "token"}
        scope = claims.get("scope", "")
        if "," in scope:
            return 400, {"error": "invalid_scope", "error_description": "Invalid OAuth scope or ID token audience"}
        if any(s not in g.dwd for s in scope.split()):
            return 401, {"error": "unauthorized_client",
                         "error_description": "Client is unauthorized to retrieve access tokens using this method, or "
                                              "client not authorized for any of the scopes requested."}
        sub = (claims.get("sub") or "").lower()
        if g.find(sub) is None or g.find(sub)["primaryEmail"] != sub:
            return 400, {"error": "invalid_grant", "error_description": "Invalid email or User ID"}
        g.jwts.append((head, claims))
        g.n += 1
        tok = f"ya29.fake-{g.n:04d}"
        g.tokens[tok] = (sub, scope)
        g.issued.append((sub, scope))
        return 200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"}

    def bearer(req):
        return g.tokens.get((req.headers.get("authorization") or "").removeprefix("Bearer "), (None, None))

    def dir_auth(req, scopes):
        sub, scope = bearer(req)
        if sub is None:
            return err(401, "Invalid Credentials", "authError")
        if scope not in scopes:
            return err(403, "Request had insufficient authentication scopes.", "insufficientPermissions")
        if not g.find(sub)["isAdmin"]:
            return err(403, "Not Authorized to access this resource/api", "forbidden")
        return None

    READ, WRITE = {SCOPE_USER, SCOPE_USER_RO}, {SCOPE_USER}

    @fake.route("GET", r"/admin/directory/v1/customers/([^/]+)")
    def customer(req):
        bad = dir_auth(req, {SCOPE_CUSTOMER})
        if bad:
            return bad
        if req.group(1) not in ("my_customer", CUSTOMER_ID):
            return err(404, "Resource Not Found: customerKey", "notFound")
        return 200, {"kind": "admin#directory#customer", "id": CUSTOMER_ID, "etag": '"c"',
                     "customerDomain": "company.ru", "alternateEmail": "it@other.ru",
                     "customerCreationTime": "2015-10-21T20:42:35.224Z", "language": "ru",
                     "postalAddress": {"organizationName": "ООО Компания", "countryCode": "RU"}}

    @fake.route("GET", r"/admin/directory/v1/users")
    def users(req):
        bad = dir_auth(req, READ)
        if bad:
            return bad
        if int(req.q("maxResults", "100")) > 500:
            return err(400, "Invalid Input: maxResults", "invalid")
        items = sorted(g.users.values(), key=lambda u: u["primaryEmail"])
        if req.q("domain"):
            if req.q("domain") not in g.domains:
                return err(400, "Domain not found.", "badRequest")
            items = [u for u in items if u["primaryEmail"].endswith("@" + req.q("domain"))]
        elif req.q("customer") not in ("my_customer", CUSTOMER_ID):
            return err(400, "Bad Request", "badRequest")
        size = min(int(req.q("maxResults", "100")), g.page)
        start = int(req.q("pageToken") or 0)
        body = {"kind": "admin#directory#users", "etag": '"l"', "users": [shown(u) for u in items[start:start + size]]}
        if start + size < len(items):
            body["nextPageToken"] = str(start + size)
        return 200, body

    @fake.route("GET", r"/admin/directory/v1/users/([^/]+)")
    def one(req):
        bad = dir_auth(req, READ)
        if bad:
            return bad
        u = g.find(req.group(1))
        if u is None:
            return err(404, "Resource Not Found: userKey", "notFound")
        return 200, shown(u)

    def check_password(body):
        if "password" not in body:
            return None
        value, fn = body["password"], body.get("hashFunction")
        if fn == "SHA-1" and not re.fullmatch(r"[0-9a-f]{40}", value):
            return err(400, "Invalid Password", "invalid")
        if fn is None and not 8 <= len(value) <= 100:
            return err(400, "Invalid Password", "invalid")
        return None

    @fake.route("PATCH", r"/admin/directory/v1/users/([^/]+)")
    def patch(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        u = g.find(req.group(1))
        if u is None:
            return err(404, "Resource Not Found: userKey", "notFound")
        body = req.json()
        bad = check_password(body)
        if bad:
            return bad
        for k, v in body.items():
            if k == "password":
                g.passwords[u["id"]] = (v, body.get("hashFunction"))
            elif k == "hashFunction":
                continue
            elif isinstance(v, list) and not v:
                continue                            # patch не очищает повторяющиеся поля
            elif k == "name":
                u["name"].update(v)
            else:
                u[k] = v
        return 200, shown(u)

    @fake.route("PUT", r"/admin/directory/v1/users/([^/]+)")
    def update(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        u = g.find(req.group(1))
        body = req.json()
        for k, v in body.items():
            if k == "name":
                u["name"] = dict(v, fullName=f"{v.get('givenName')} {v.get('familyName')}")
            else:
                u[k] = v                            # списки — целиком, в том числе пустые
        return 200, shown(u)

    @fake.route("POST", r"/admin/directory/v1/users")
    def insert(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        body = req.json()
        email = body["primaryEmail"].lower()
        name = body.get("name") or {}
        if not name.get("givenName") or not name.get("familyName"):
            return err(400, "Invalid Given/Family Name: FamilyName", "invalid")
        if email.split("@")[1] not in g.domains:
            return err(400, "Domain not found.", "notFound")
        if g.taken(email):
            return err(409, "Entity already exists.", "duplicate")
        bad = check_password(body)
        if bad:
            return bad
        uid = str(200 + len(g.users))
        u = guser(uid, email, name["givenName"], name["familyName"])
        u["changePasswordAtNextLogin"] = bool(body.get("changePasswordAtNextLogin"))
        u["orgUnitPath"] = body.get("orgUnitPath") or "/"
        g.users[uid] = u
        g.passwords[uid] = (body["password"], body.get("hashFunction"))
        return 200, shown(u)

    @fake.route("DELETE", r"/admin/directory/v1/users/([^/]+)")
    def delete(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        u = g.find(req.group(1))
        if u is None:
            return err(404, "Resource Not Found: userKey", "notFound")
        g.deleted.append(g.users.pop(u["id"]))
        return 204, b""

    @fake.route("POST", r"/admin/directory/v1/users/([^/]+)/aliases")
    def add_alias(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        u = g.find(req.group(1))
        alias = req.json()["alias"].lower()
        if alias.split("@")[1] not in g.domains:
            return err(400, "Invalid Input: alias domain is not verified", "invalid")
        if g.taken(alias):
            return err(409, "Entity already exists.", "duplicate")
        if len(u["aliases"]) >= 30:
            return err(400, "Too many aliases", "limitExceeded")
        u["aliases"].append(alias)
        return 201, {"kind": "admin#directory#alias", "id": u["id"], "primaryEmail": u["primaryEmail"],
                     "alias": alias}

    @fake.route("DELETE", r"/admin/directory/v1/users/([^/]+)/aliases/([^/]+)")
    def del_alias(req):
        bad = dir_auth(req, WRITE)
        if bad:
            return bad
        u = g.find(req.group(1))
        alias = req.group(2).lower()
        if alias not in u["aliases"]:
            return err(404, "Resource Not Found: alias", "notFound")
        u["aliases"].remove(alias)
        return 204, b""

    @fake.route("GET", r"/admin/reports/v1/usage/users/([^/]+)/dates/([0-9-]+)")
    def usage(req):
        bad = dir_auth(req, {SCOPE_REPORTS})
        if bad:
            return bad
        day = req.group(2)
        if day > g.ready:
            return err(400, f"Data for dates later than {g.ready} is not yet available. Please check back later",
                       "invalid")
        who = req.group(1)
        emails = sorted(g.usage) if who == "all" else [who.lower()]
        wanted = set(req.q("parameters").split(","))
        reports = []
        for email in emails:
            u = g.find(email)
            if email not in g.usage or u is None:
                continue
            values = dict(zip(("accounts:gmail_used_quota_in_mb", "accounts:used_quota_in_mb",
                               "accounts:total_quota_in_mb", "accounts:drive_used_quota_in_mb"), g.usage[email]))
            reports.append({"kind": "admin#reports#usageReport", "date": day, "etag": '"u"',
                            "entity": {"type": "USER", "customerId": CUSTOMER_ID, "userEmail": email,
                                       "profileId": u["id"]},
                            "parameters": [{"name": k, "intValue": str(v)} for k, v in values.items() if k in wanted]})
        start = int(req.q("pageToken") or 0)
        body = {"kind": "admin#reports#usageReports", "etag": '"r"', "usageReports": reports[start:start + 2]}
        if start + 2 < len(reports):
            body["nextPageToken"] = str(start + 2)
        return 200, body

    def gmail_auth(req, scopes):
        sub, scope = bearer(req)
        if sub is None:
            return err(401, "Invalid Credentials", "authError")
        if scope not in scopes:
            return err(403, "Request had insufficient authentication scopes.", "insufficientPermissions")
        if req.group(1).lower() not in ("me", sub):
            return err(403, f"Delegation denied for {sub}", "forbidden")
        return None

    @fake.route("GET", r"/gmail/v1/users/([^/]+)/settings/vacation")
    def get_vacation(req):
        return gmail_auth(req, GMAIL_READ) or (200, g.vacation.get(req.group(1).lower(), {"enableAutoReply": False}))

    @fake.route("PUT", r"/gmail/v1/users/([^/]+)/settings/vacation")
    def put_vacation(req):
        bad = gmail_auth(req, {SCOPE_BASIC})
        if bad:
            return bad
        body = req.json()
        if body.get("enableAutoReply") and not (body.get("responseSubject") or body.get("responseBodyPlainText")):
            return err(400, "Invalid vacation settings", "invalidArgument")
        if body.get("startTime") and body.get("endTime") and int(body["startTime"]) >= int(body["endTime"]):
            return err(400, "Invalid vacation settings: start must precede end", "invalidArgument")
        g.vacation[req.group(1).lower()] = body
        return 200, body

    @fake.route("GET", r"/gmail/v1/users/([^/]+)/settings/autoForwarding")
    def get_fwd(req):
        return gmail_auth(req, GMAIL_READ) or (200, g.autofwd.get(req.group(1).lower(), {"enabled": False}))

    @fake.route("PUT", r"/gmail/v1/users/([^/]+)/settings/autoForwarding")
    def put_fwd(req):
        bad = gmail_auth(req, {SCOPE_SHARING})
        if bad:
            return bad
        body, who = req.json(), req.group(1).lower()
        if body.get("enabled"):
            if g.fwd.get(who, {}).get(body.get("emailAddress")) != "accepted":
                return err(400, "Forwarding address is not verified", "failedPrecondition")
            if body.get("disposition") in (None, "dispositionUnspecified"):
                return err(400, "Invalid disposition", "invalidArgument")
        g.autofwd[who] = body
        return 200, body

    @fake.route("GET", r"/gmail/v1/users/([^/]+)/settings/forwardingAddresses")
    def list_fwd(req):
        bad = gmail_auth(req, GMAIL_READ)
        if bad:
            return bad
        items = g.fwd.get(req.group(1).lower(), {})
        return 200, {"forwardingAddresses": [{"forwardingEmail": k, "verificationStatus": v} for k, v in items.items()]}

    @fake.route("POST", r"/gmail/v1/users/([^/]+)/settings/forwardingAddresses")
    def add_fwd(req):
        bad = gmail_auth(req, {SCOPE_SHARING})
        if bad:
            return bad
        target, who = req.json()["forwardingEmail"].lower(), req.group(1).lower()
        if target in g.fwd.get(who, {}):
            return err(409, "Already exists", "duplicate")
        status = "accepted" if target.split("@")[1] in g.domains else "pending"
        g.fwd.setdefault(who, {})[target] = status
        return 200, {"forwardingEmail": target, "verificationStatus": status}

    g.fake = fake
    yield g
    fake.close()
    TOKENS._tokens.clear()


def connect(svc, **extra):
    values = {"type": "google", "name": "Компания", "sa_key": sa_json(), "admin_email": "admin@company.ru",
              "customer": "", "allow_changes": True}
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
    return [msstore.get_log(svc.db, row["id"])["transcript"] for row in msstore.list_log(svc.db, sid, 1000)]


def assert_no_secrets(svc, sid, g):
    pem_line = PEM.splitlines()[5]
    for text in transcripts(svc, sid):
        assert pem_line not in text and OTHER_PEM.splitlines()[5] not in text
        assert "eyJhbGci" not in text                       # подписанный JWT (assertion)
        assert not any(tok in text for tok in g.tokens)


def driver(g, **settings):
    values = {"admin_email": "admin@company.ru", "customer": "", "usage_report": True}
    values.update(settings)
    return GoogleDriver(connection("google", values, {"sa_key": sa_json()}), ApiLog())


@pytest.fixture()
def google(services, gw):
    sid = connect(services)
    res = mss.sync(services, sid, user="tester")
    return type("G", (), {"svc": services, "g": gw, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@company.ru", "pe": f"ms:{sid}:petrova@company.ru"})


# ---------------------------------------------------------------------------------------------------------------
#  Подключение и проверка
# ---------------------------------------------------------------------------------------------------------------
def test_check_reads_customer_and_signs_jwt(services, gw):
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert not st["problems"], st["problems"]
    assert any("ООО Компания" in d for d in st["details"]) and any("company.ru" in d for d in st["details"])
    assert any(CUSTOMER_ID in d for d in st["details"]) and any(SA_EMAIL in d for d in st["details"])
    assert st["seats_total"] is None
    # JWT: подписан ключом, одна область, от имени администратора, на час, kid из ключа
    head, claims = gw.jwts[0]
    assert head == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    assert claims["sub"] == "admin@company.ru" and claims["scope"] == SCOPE_USER and claims["iss"] == SA_EMAIL
    assert claims["exp"] - claims["iat"] == 3600
    # области проверены токенами от имени администратора; повторная проверка берёт токены из кэша
    assert {s for _sub, s in gw.issued} == FULL and {sub for sub, _s in gw.issued} == {"admin@company.ru"}
    issued = len(gw.issued)
    assert mss.check(services, sid, user="tester")["ok"]
    assert len(gw.issued) == issued
    assert msstore.get_server(services.db, sid).last_check_ok is True
    assert_no_secrets(services, sid, gw)


def test_wrong_key_is_explained_and_not_logged(services, gw):
    sid = connect(services, sa_key=sa_json(OTHER_PEM))
    out = mss.check(services, sid, user="tester")
    assert not out["ok"]
    assert "ключ сервисного аккаунта не принят" in out["error"] and "Invalid JWT Signature" in out["error"]
    assert_no_secrets(services, sid, gw)


def test_unknown_admin_and_admin_without_rights(services, gw):
    sid = connect(services, admin_email="nobody@company.ru")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "пользователя nobody@company.ru в Google Workspace нет" in out["error"]
    sid = connect(services, name="Не админ", admin_email="ivanov@company.ru")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "нет прав" in out["error"] and "ivanov@company.ru" in out["error"]


def test_missing_scopes_reported_in_russian(services, gw):
    gw.dwd = {SCOPE_USER_RO, SCOPE_MAIL}                    # «только чтение», и то не всё
    sid = connect(services)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out                                   # каталог читается — остальное предупреждения
    problems = {p[0]: p[2] for p in out["status"]["problems"]}
    assert set(problems) == {"customer", "directory_write", "usage", "gmail_basic", "gmail_sharing"}
    assert SCOPE_SHARING in problems["gmail_sharing"] and "пересылку не настроить" in problems["gmail_sharing"]
    assert "Делегировании домена" in problems["usage"] and CLIENT_ID in problems["usage"]
    # каталог недоступен вовсе — подключение не работает
    gw.dwd = {SCOPE_MAIL}
    TOKENS._tokens.clear()
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "Делегировании домена" in out["error"] and "admin.directory.user" in out["error"]


def test_settings_are_validated(services, gw):
    with pytest.raises(ValidationError, match="не читается"):
        connect(services, sa_key="не json")
    with pytest.raises(ValidationError, match="не ключ сервисного аккаунта"):
        connect(services, sa_key=json.dumps({"installed": {"client_id": "x"}}))
    with pytest.raises(ValidationError, match="private_key"):
        broken = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----"
        connect(services, sa_key=json.dumps({"type": "service_account", "client_email": SA_EMAIL,
                                             "private_key": broken}))
    with pytest.raises(ValidationError, match="администратора"):
        connect(services, admin_email="admin")
    with pytest.raises(ValidationError, match="Идентификатор клиента"):
        connect(services, customer="C0 1!")
    with pytest.raises(ValidationError, match="Подразделение"):
        connect(services, org_unit="Сотрудники")
    desc = GoogleDriver.describe()
    assert "quota" not in desc["caps"] and {"aliases", "imap_admin", "usage"} <= set(desc["caps"])
    assert desc["imap_default"] == ["imap.gmail.com", 993, "ssl"]
    assert any("Квоту ящика" in line for line in desc["limits"])


# ---------------------------------------------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------------------------------------------
def test_sync_users_usage_and_last_login(google):
    rows = {r["email"]: r for r in msstore.list_accounts(google.svc.db, google.sid)}
    assert set(rows) == {"admin@company.ru", "ivanov@company.ru", "petrova@company.ru", "sidorov@company.ru",
                         "smirnov@filial.ru"}
    iv = rows["ivanov@company.ru"]
    assert iv["aliases"] == ["ivan@company.ru"]              # *.test-google-a.com — только в сведениях
    assert iv["info"]["non_editable_aliases"] == ["ivanov@company.ru.test-google-a.com"]
    assert iv["login_blocked"] is False and iv["last_login"] == "2026-10-20T08:12:44.000Z"
    assert (iv["department"], iv["title"], iv["phone"], iv["mobile"]) == \
        ("Продажи", "Менеджер", "+7 495 000-00-01", "+7 900 000-00-01")
    assert iv["first_name"] == "Иван" and iv["display_name"] == "Иван Иванов" and iv["native_id"] == "101"
    assert iv["used_bytes"] == 1500 * MB and iv["quota_kb"] is None   # квоты почты у Google нет
    assert iv["info"]["storage_total_mb"] == 30720 and iv["info"]["usage_date"] == google.g.ready
    assert rows["petrova@company.ru"]["last_login"] == ""             # «никогда» — не 1970 год
    assert rows["sidorov@company.ru"]["login_blocked"] is True
    assert rows["sidorov@company.ru"]["info"]["suspension_reason"] == "ADMIN"
    assert rows["admin@company.ru"]["kind"] == "admin" and iv["kind"] == "user"
    assert google.sync["incomplete"] is False and not google.sync["problems"]
    fake = google.g.fake
    assert len(fake.calls_to("GET", r"/admin/directory/v1/users")) == 3            # страницы по nextPageToken
    reports = fake.calls_to("GET", r"/admin/reports/v1/usage/users/all/dates/.*")
    yesterday = (datetime.now(PST).date() - timedelta(days=1)).isoformat()
    assert reports[0].path.endswith(yesterday)              # за вчера отчёта ещё нет — день назад
    assert all(r.q("parameters").startswith("accounts:gmail_used_quota_in_mb") for r in reports)
    assert_no_secrets(google.svc, google.sid, google.g)


def test_sync_without_reports_scope_and_one_domain(services, gw):
    gw.dwd.discard(SCOPE_REPORTS)
    sid = connect(services, customer="filial.ru")
    res = mss.sync(services, sid, user="tester")
    rows = {r["email"]: r for r in msstore.list_accounts(services.db, sid)}
    assert set(rows) == {"smirnov@filial.ru"} and rows["smirnov@filial.ru"]["used_bytes"] is None
    assert any(SCOPE_REPORTS in p for p in res["problems"])
    assert gw.fake.calls_to("GET", r"/admin/directory/v1/users")[0].q("domain") == "filial.ru"
    # идентификатор клиента — все домены организации
    sid = connect(services, name="Все", customer=CUSTOMER_ID, usage_report=False)
    mss.sync(services, sid, user="tester")
    assert len(msstore.list_accounts(services.db, sid)) == 5
    assert gw.fake.calls_to("GET", r"/admin/directory/v1/users")[-1].q("customer") == CUSTOMER_ID


def test_archived_user_has_login_closed(services, gw):
    """Учётная запись в архиве (Archived User) — вход закрыт, хотя она не приостановлена."""
    gw.users["104"]["archived"] = True
    sid = connect(services)
    mss.sync(services, sid, user="tester")
    row = msstore.get_account(services.db, sid, "smirnov@filial.ru")
    assert row["login_blocked"] is True and row["kind"] == "archived" and row["info"]["archived"] is True
    assert driver(gw).get_account("smirnov@filial.ru")["login_blocked"] is True
    # «Заблокировать вход» ей не нужно
    pv = base.run(services, ADMIN, "ax_block", [f"ms:{sid}:smirnov@filial.ru"], {"final_copy": False}, preview=True)
    assert pv["counts"]["skip"] == 1 and "вход уже закрыт" in pv["results"][0]["detail"]
    assert any("Archived User" in line for line in GoogleDriver.describe()["limits"])


def test_read_only_connection_uses_read_only_scopes(services, gw):
    gw.dwd = {SCOPE_USER_RO, SCOPE_CUSTOMER, SCOPE_REPORTS, SCOPE_MAIL}
    sid = connect(services, allow_changes=False)
    assert mss.check(services, sid, user="tester")["ok"]
    mss.sync(services, sid, user="tester")
    conn = msstore.get_server(services.db, sid)
    with mss.session(services, conn) as drv:
        acc = drv.get_account("ivanov@company.ru")
    assert acc["autoreply"] is False and acc["forward_to"] == []       # настройки Gmail — областью mail.google.com
    assert all(scope != SCOPE_USER for _sub, scope in gw.issued)


def test_denied_scope_is_not_requested_in_every_session(services, gw):
    gw.dwd = {SCOPE_USER, SCOPE_MAIL}                       # делегирована только полная область каталога
    sid = connect(services, allow_changes=False, usage_report=False)
    mss.sync(services, sid, user="tester")
    mss.sync(services, sid, user="tester")
    asked = [r for r in gw.fake.calls_to("POST", r"/token")]
    assert len(asked) == 2                                  # .readonly — отказ один раз, затем полная (из кэша)
    assert len(msstore.list_accounts(services.db, sid)) == 5
    # «Проверить подключение» спрашивает все области заново
    out = mss.check(services, sid, user="tester")
    assert out["ok"] and len(gw.fake.calls_to("POST", r"/token")) > 2


# ---------------------------------------------------------------------------------------------------------------
#  IMAP
# ---------------------------------------------------------------------------------------------------------------
def imap_world(monkeypatch, g):
    world = ImapWorld()
    world.mailbox("petrova@company.ru", "nopass")
    world.add("petrova@company.ru", "INBOX", raw_message("Отчёт за квартал"))
    world.tokens = LiveTokens(g)
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return world


def test_add_mailbox_then_copy_with_service_account_token(google, monkeypatch):
    svc = google.svc
    world = imap_world(monkeypatch, google.g)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [google.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.auth_type == AuthType.SERVER and acc.server_id == google.sid
    assert acc.host == "imap.gmail.com" and acc.port == 993 and acc.password == ""
    from mailarchiver.imap.client import ImapConnection
    conn = ImapConnection(acc, svc.connect_options())
    conn.connect()
    try:
        assert world.last_login == ("xoauth2", "petrova@company.ru", "petrova@company.ru")
    finally:
        conn.close()
    assert ("petrova@company.ru", SCOPE_MAIL) in google.g.issued           # токен — от имени владельца ящика
    again = base.run(svc, ADMIN, "ax_add_mailboxes", [google.pe], {}, preview=True)
    assert again["counts"]["skip"] == 1 and "уже копируется" in again["results"][0]["detail"]


# ---------------------------------------------------------------------------------------------------------------
#  Действия
# ---------------------------------------------------------------------------------------------------------------
def test_capabilities_skip_with_reason(google):
    pv = base.run(google.svc, ADMIN, "ax_quota", [google.iv], {"quota_mb": 2048}, preview=True)
    assert pv["counts"]["skip"] == 1 and "квота ящика — этот сервер так не умеет" in pv["results"][0]["detail"]
    drv = driver(google.g)
    with pytest.raises(NotSupported):
        drv.set_quota("ivanov@company.ru", 1024)


def test_changes_need_permission_per_connection(services, gw):
    sid = connect(services, allow_changes=False)
    mss.sync(services, sid, user="tester")
    pv = base.run(services, ADMIN, "ax_block", [f"ms:{sid}:ivanov@company.ru"], {"final_copy": False}, preview=True)
    assert pv["counts"]["skip"] == 1 and "изменения на сервере «Компания» не разрешены" in pv["results"][0]["detail"]


def test_block_password_contact_unblock(google):
    svc, g = google.svc, google.g
    _res, items = act(svc, "ax_block", [google.pe], final_copy=False, keep_receiving=True)
    assert items[google.pe]["status"] == "success", items
    assert g.users["102"]["suspended"] is True and "приостановлен" in items[google.pe]["detail"]
    assert "не доставляет" in items[google.pe]["detail"]
    assert msstore.get_account(svc.db, google.sid, "petrova@company.ru")["login_blocked"] is True
    _res, items = act(svc, "ax_unblock", [google.pe])
    assert items[google.pe]["status"] == "success" and g.users["102"]["suspended"] is False
    # пароль — случайный, хэшем SHA-1 (как рекомендует Google), с требованием сменить
    _res, items = act(svc, "ax_password", [google.iv], mode="random_hidden", must_change=True)
    assert items[google.iv]["status"] == "success", items
    value, fn = g.passwords["101"]
    assert fn == "SHA-1" and re.fullmatch(r"[0-9a-f]{40}", value)
    assert g.users["101"]["changePasswordAtNextLogin"] is True
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["must_change"] is True
    assert not any(value in t for t in transcripts(svc, google.sid))
    pv = base.run(svc, ADMIN, "ax_password", [google.iv], {"mode": "same", "password": "кириллица-123"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "латинские буквы" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_must_change", [google.iv], value="no")
    assert items[google.iv]["status"] == "success" and g.users["101"]["changePasswordAtNextLogin"] is False
    # организация: поля основной записи organizations[] — остальное в ней сохраняется
    _res, items = act(svc, "ax_contact", [google.iv, google.pe], source="fixed", company="ООО Компания",
                      department="ИТ", office="Москва, офис 5", overwrite=True, name_order="last_first", phone=False)
    assert items[google.iv]["status"] == "success", items
    assert g.users["101"]["organizations"] == [{"title": "Менеджер", "department": "ИТ", "primary": True,
                                                "type": "work", "name": "ООО Компания", "location": "Москва, офис 5"}]
    assert g.users["102"]["organizations"] == [{"primary": True, "type": "work", "department": "ИТ",
                                                "name": "ООО Компания", "location": "Москва, офис 5"}]
    row = msstore.get_account(svc.db, google.sid, "petrova@company.ru")
    assert (row["company"], row["department"], row["office"]) == ("ООО Компания", "ИТ", "Москва, офис 5")
    assert "после записи сервер показал другое" not in items[google.pe]["detail"]


def test_contact_phones_and_names_replace_lists(gw):
    drv = driver(gw)
    drv.set_contact("ivanov@company.ru", {"phone": "+7 495 111-11-11", "mobile": "", "title": "Директор",
                                          "last_name": "Иванов-Петров"})
    u = gw.users["101"]
    assert u["phones"] == [{"value": "+7 495 111-11-11", "type": "work", "primary": True}]
    assert u["name"]["familyName"] == "Иванов-Петров" and u["name"]["givenName"] == "Иван"
    drv.set_contact("ivanov@company.ru", {"phone": ""})
    assert u["phones"] == []                                # update (PUT) очищает список, patch бы не смог
    acc = drv.get_account("ivanov@company.ru")
    assert acc["phone"] == "" and acc["title"] == "Директор" and acc["last_name"] == "Иванов-Петров"
    with pytest.raises(ValidationError, match="нельзя оставить пустыми"):
        drv.set_contact("ivanov@company.ru", {"first_name": ""})


def test_aliases_add_and_remove(google):
    svc, g = google.svc, google.g
    _res, items = act(svc, "ax_alias_add", [google.iv], template="{f}.{last}")
    assert items[google.iv]["status"] == "success", items
    assert "i.ivanov@company.ru" in g.users["101"]["aliases"]
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["aliases"] == \
        ["ivan@company.ru", "i.ivanov@company.ru"]
    _res, items = act(svc, "ax_alias_remove", [google.iv], which="list", aliases="ivan")
    assert items[google.iv]["status"] == "success", items
    assert g.users["101"]["aliases"] == ["i.ivanov@company.ru"]
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["aliases"] == ["i.ivanov@company.ru"]
    drv = driver(g)
    with pytest.raises(AlreadyExists, match="уже занят"):
        drv.add_alias("ivanov@company.ru", "anna@company.ru")      # псевдоним Петровой
    assert drv.remove_alias("ivanov@company.ru", "i.ivanov@company.ru") == []


def test_autoreply(google):
    svc, g = google.svc, google.g
    _res, items = act(svc, "srv_autoreply", [google.iv], mode="on", subject="Нет на месте",
                      text="Я больше не работаю.\nПишите petrova@company.ru", start="", end="")
    assert items[google.iv]["status"] == "success", items
    vac = g.vacation["ivanov@company.ru"]
    assert vac["enableAutoReply"] is True and vac["responseSubject"] == "Нет на месте"
    assert vac["responseBodyPlainText"].endswith("petrova@company.ru") and "startTime" not in vac
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["autoreply"] is True
    assert ("ivanov@company.ru", SCOPE_BASIC) in g.issued                  # от имени владельца ящика
    _res, items = act(svc, "srv_autoreply", [google.iv], mode="on", subject="", text="Отпуск", start="2026-11-01",
                      end="2026-11-10")
    vac = g.vacation["ivanov@company.ru"]
    assert vac["startTime"] == str(int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000))
    assert vac["endTime"] == str(int(datetime(2026, 11, 11, tzinfo=timezone.utc).timestamp() * 1000))
    assert "с 2026-11-01 по 2026-11-10" in items[google.iv]["detail"]
    got = driver(g).get_autoreply("ivanov@company.ru")
    assert (got["enabled"], got["start"], got["end"], got["text"]) == (True, "2026-11-01", "2026-11-10", "Отпуск")
    _res, items = act(svc, "srv_autoreply", [google.iv], mode="off")
    assert items[google.iv]["status"] == "success" and g.vacation["ivanov@company.ru"] == {"enableAutoReply": False}
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["autoreply"] is False
    # автоответ с прошедшей датой окончания — уже не действует
    g.vacation["ivanov@company.ru"] = {"enableAutoReply": True, "responseSubject": "x", "endTime": "1000"}
    assert driver(g).get_account("ivanov@company.ru")["autoreply"] is False


def test_forwarding_accepted_pending_and_off(google):
    svc, g = google.svc, google.g
    _res, items = act(svc, "srv_forwarding", [google.iv], mode="on", addresses="petrova@company.ru", keep_copy=True)
    assert items[google.iv]["status"] == "success", items
    assert g.autofwd["ivanov@company.ru"] == {"enabled": True, "emailAddress": "petrova@company.ru",
                                              "disposition": "leaveInInbox"}
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["forward_to"] == ["petrova@company.ru"]
    # внешний адрес — ждёт подтверждения владельцем: пересылка не включается (ошибка, а не «сделано»), прежняя
    # остаётся — и в снимке тоже
    res, items = act(svc, "srv_forwarding", [google.iv], mode="on", addresses="boss@gmail.com", keep_copy=True)
    detail = items[google.iv]["detail"]
    assert items[google.iv]["status"] == "failed" and res["ok"] == 0, items
    assert "НЕ включена" in detail and "ждёт подтверждения" in detail and "прежняя пересылка" in detail, detail
    assert g.fwd["ivanov@company.ru"]["boss@gmail.com"] == "pending"
    assert g.autofwd["ivanov@company.ru"]["emailAddress"] == "petrova@company.ru"
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["forward_to"] == ["petrova@company.ru"]
    with pytest.raises(ServerError, match="ждёт подтверждения"):
        driver(g).set_forwarding("ivanov@company.ru", addresses=["boss@gmail.com"])
    # владелец подтвердил — повтор включает (адрес второй раз не заводится)
    g.fwd["ivanov@company.ru"]["boss@gmail.com"] = "accepted"
    created = len(g.fake.calls_to("POST", r"/gmail/v1/users/.*/forwardingAddresses"))
    _res, items = act(svc, "srv_forwarding", [google.iv], mode="on", addresses="boss@gmail.com", keep_copy=False)
    assert items[google.iv]["status"] == "success" and "«Корзину»" in items[google.iv]["detail"], items
    assert g.autofwd["ivanov@company.ru"]["disposition"] == "trash"
    assert len(g.fake.calls_to("POST", r"/gmail/v1/users/.*/forwardingAddresses")) == created
    assert driver(g).get_forwarding("ivanov@company.ru") == {"addresses": ["boss@gmail.com"], "keep_copy": False}
    _res, items = act(svc, "srv_forwarding", [google.iv], mode="off", addresses="", keep_copy=True)
    assert items[google.iv]["status"] == "success" and g.autofwd["ivanov@company.ru"] == {"enabled": False}
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["forward_to"] == []
    # на несколько адресов Gmail не пересылает
    _res, items = act(svc, "srv_forwarding", [google.iv], mode="on", addresses="a@company.ru\nb@company.ru",
                      keep_copy=True)
    assert items[google.iv]["status"] != "success" and "только на один адрес" in items[google.iv]["detail"]


def test_delete_only_with_full_archive_then_transfer_address(google, monkeypatch):
    svc, g = google.svc, google.g
    world = imap_world(monkeypatch, g)
    base.run(svc, ADMIN, "ax_add_mailboxes", [google.pe], {}, preview=False)
    pv = base.run(svc, ADMIN, "ax_delete", [google.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "сначала «Заблокировать вход»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_block", [google.pe], final_copy=True)
    assert items[google.pe]["status"] == "success" and "последняя копия сделана" in items[google.pe]["detail"], items
    # письмо пришло после копии — в архиве его нет: учётная запись не удаляется
    real_fresh = __import__("mailarchiver.serverops.imap_ops", fromlist=["fresh_copy"]).fresh_copy

    def copy_then_new_mail(ctx, acc, emit):
        out = real_fresh(ctx, acc, emit)
        world.add("petrova@company.ru", "INBOX", raw_message("Пришло после копии"))
        return out
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", copy_then_new_mail)
    _res, items = act(svc, "ax_delete", [google.pe])
    assert items[google.pe]["status"] == "skipped" and "НЕ удалена" in items[google.pe]["detail"], items
    assert "102" in g.users
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", real_fresh)
    _res, items = act(svc, "ax_delete", [google.pe])
    assert items[google.pe]["status"] == "success", items
    assert "102" not in g.users and "20 дней" in items[google.pe]["detail"]
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    assert msstore.get_account(svc.db, google.sid, "petrova@company.ru")["deleted_at"]
    # передать адрес можно только коллеге в том же подключении и домене
    pv = base.run(svc, ADMIN, "ax_forward", [google.pe], {"to": "smirnov@filial.ru"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "в другом домене" in pv["results"][0]["detail"]
    pv = base.run(svc, ADMIN, "ax_forward", [google.pe], {"to": "outsider@company.ru"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "нет на сервере «Компания»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_forward", [google.pe], to="ivanov@company.ru", with_aliases=True)
    assert items[google.pe]["status"] == "success", items
    assert "теперь приходят ivanov@company.ru" in items[google.pe]["detail"]
    assert {"petrova@company.ru", "anna@company.ru"} <= set(g.users["101"]["aliases"])
    row = msstore.get_account(svc.db, google.sid, "ivanov@company.ru")
    assert {"petrova@company.ru", "anna@company.ru"} <= set(row["aliases"])
    # перечитать удалённую: её адрес теперь псевдоним Иванова — это не она, учётная запись остаётся удалённой
    _res, items = act(svc, "ax_refresh", [google.pe])
    assert "больше нет" in items[google.pe]["detail"], items
    assert msstore.get_account(svc.db, google.sid, "petrova@company.ru")["deleted_at"]
    assert msstore.get_account(svc.db, google.sid, "ivanov@company.ru")["first_name"] == "Иван"


def test_delete_refused_when_suspended_mailbox_cannot_be_opened(google, monkeypatch):
    svc, g = google.svc, google.g
    g.imap_for_suspended = False                            # Google не пускает в ящик приостановленного
    imap_world(monkeypatch, g)
    base.run(svc, ADMIN, "ax_add_mailboxes", [google.pe], {}, preview=False)
    _res, items = act(svc, "ax_block", [google.pe], final_copy=True)
    assert items[google.pe]["status"] == "success", items       # последняя копия — до приостановки
    _res, items = act(svc, "ax_delete", [google.pe])
    assert items[google.pe]["status"] == "skipped", items
    assert "последняя копия не удалась — учётная запись НЕ удалена" in items[google.pe]["detail"]
    assert "102" in g.users
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.hold_until != "9999-12-31"                   # удержание снято обратно


def test_create_accounts_on_connection(google, monkeypatch):
    svc, g = google.svc, google.g
    conn = msstore.get_server(svc.db, google.sid)
    msstore.update_server(svc.db, google.sid, name=conn.name, settings=dict(conn.settings, org_unit="/Сотрудники"),
                          secrets=conn.secrets, sync_cron=conn.sync_cron, enabled=True, allow_changes=True,
                          sync_enabled=True, sync_add_new=False, monitor=True)
    g.users["300"] = guser("300", "late@company.ru", "Поздно", "Созданный")    # есть на сервере, но не в сверке
    params = {"server_id": google.sid, "text": "new.user@company.ru; Новый; Сотрудник\nivanov@company.ru\n"
                                               "x@unknown.ru\nlate@company.ru; Поздно; Созданный",
              "password_mode": "random_file", "must_change": True, "quota_mb": 0, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@company.ru"]["status"] == "ok" and by["late@company.ru"]["status"] == "ok"
    assert by["ivanov@company.ru"]["status"] == "skip" and "уже есть" in by["ivanov@company.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    items = {i["key"]: i for i in out["items"]}
    assert out["ok"] == 1 and items["new.user@company.ru"]["status"] == "success", out
    assert items["late@company.ru"]["status"] == "skipped" and "уже есть" in items["late@company.ru"]["detail"]
    new = g.find("new.user@company.ru")
    assert new["name"]["givenName"] == "Новый" and new["changePasswordAtNextLogin"] is True
    assert new["orgUnitPath"] == "/Сотрудники" and g.passwords[new["id"]][1] == "SHA-1"
    row = msstore.get_account(svc.db, google.sid, "new.user@company.ru")
    assert row is not None and row["first_name"] == "Новый" and row["native_id"] == new["id"]
    assert any(a.username == "new.user@company.ru" and a.auth_type == AuthType.SERVER and a.host == "imap.gmail.com"
               for a in svc.db.list_accounts())
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1         # пароль «уже существующей» не остался в файле


def test_create_needs_names_and_reports_existing(gw):
    drv = driver(gw)
    with pytest.raises(ValidationError, match="имя и фамилию"):
        drv.create_account("noname@company.ru", first_name="", last_name="", password="Aa1-aaaaaaaa")
    with pytest.raises(AlreadyExists, match="уже есть"):
        drv.create_account("ivanov@company.ru", first_name="Иван", last_name="Иванов", password="Aa1-aaaaaaaa")
    with pytest.raises(AlreadyExists):
        drv.create_account("ivan@company.ru", first_name="Иван", last_name="Второй", password="Aa1-aaaaaaaa")
    assert not gw.fake.calls_to("POST", r"/admin/directory/v1/users")[0].json().get("password", "").startswith("Aa1")
    acc = drv.create_account("new@filial.ru", first_name="Новая", last_name="Сотрудница", password="Aa1-aaaaaaaa",
                             must_change=False)
    assert acc["email"] == "new@filial.ru" and acc["login_blocked"] is False and acc["must_change"] is False


def test_report_by_api_when_no_imap(google):
    svc = google.svc
    pv = base.run(svc, ADMIN, "report", [google.iv], {}, preview=True)
    assert pv["counts"]["ok"] == 1 and "через API Google Workspace" in pv["results"][0]["detail"]
    _res, items = act(svc, "report", [google.iv])
    assert items[google.iv]["status"] == "success", items
    report = items[google.iv]["report"]
    assert report["size"] == 1500 * MB and report["quota"] is None and report["source"].startswith("API Google")
    # одна учётная запись — отчётом за уже известный готовый день, одним запросом
    calls = google.g.fake.calls_to("GET", r"/admin/reports/v1/usage/users/ivanov@company.ru/dates/.*")
    assert calls and all(c.path.endswith(google.g.ready) for c in calls)


def test_server_capabilities_in_targets(google):
    from mailarchiver.serverops import targets as tg
    items = {t.key: t for t in tg.load(google.svc)}
    row = tg.row_dict(google.svc, items[google.iv], {}, {})
    assert row["server"]["type"] == "google"
    assert {"block", "aliases", "autoreply", "forward"} <= set(row["server"]["caps"])
    assert Cap.QUOTA not in GoogleDriver.caps
