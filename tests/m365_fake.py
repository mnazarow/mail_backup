"""Учебный Microsoft Graph и вход Microsoft (для тестов драйвера Microsoft 365 и стенда интерфейса)."""
import copy

from mailserver_fake import FakeHttp

SECRET = "s3cret-Value~1"
TENANT = "11111111-2222-3333-4444-555555555555"


def user(uid, mail, first, last, *, enabled=True, aliases=(), licensed=True, kind="Member", last_login=""):
    return {"id": uid, "userPrincipalName": mail, "mail": mail, "displayName": f"{first} {last}",
            "givenName": first, "surname": last, "accountEnabled": enabled,
            "proxyAddresses": [f"SMTP:{mail}"] + [f"smtp:{a}" for a in aliases], "department": "Продажи",
            "jobTitle": "", "businessPhones": [], "mobilePhone": None, "officeLocation": None, "companyName": None,
            "userType": kind, "assignedLicenses": [{"skuId": "sku-1"}] if licensed else [],
            "createdDateTime": "2024-01-01T00:00:00Z", "onPremisesSyncEnabled": None,
            "signInActivity": {"lastSignInDateTime": last_login or "0001-01-01T00:00:00Z"}}


class Graph:
    """Состояние учебного клиента Microsoft 365."""

    def __init__(self):
        self.users = {
            "u1": user("u1", "ivanov@contoso.ru", "Иван", "Иванов", aliases=["ivan@contoso.ru"],
                       last_login="2026-10-20T08:00:00Z"),
            "u2": user("u2", "petrova@contoso.ru", "Анна", "Петрова"),
            "u3": user("u3", "guest@other.ru", "Гость", "Внешний", kind="Guest"),
            "u4": user("u4", "noreply@contoso.ru", "Без", "Лицензии", licensed=False, enabled=False),
        }
        self.settings = {}
        self.rules = {}
        self.no_p1 = False
        self.concealed = False
        self.page = 2
        self.deleted = []
        self.skus = [{"skuId": "sku-1", "skuPartNumber": "O365_BUSINESS_PREMIUM", "appliesTo": "User",
                      "capabilityStatus": "Enabled", "consumedUnits": 3,
                      "prepaidUnits": {"enabled": 10, "warning": 0, "suspended": 0}}]
        self.tokens_issued = []
        #: ответ assignLicense — ошибка (например, страна пользователя не задана)
        self.assign_error = None

    def find(self, key):
        key = key.lower()
        for u in self.users.values():
            if u["id"] == key or u["userPrincipalName"].lower() == key:
                return u
        return None


def start(g=None):
    """Запустить учебный сервер → (состояние, FakeHttp). Адреса драйвера подставляет вызывающий."""
    g = g or Graph()
    fake = FakeHttp()

    @fake.route("POST", rf"/{TENANT}/oauth2/v2.0/token")
    def token(req):
        form = req.form()
        if form.get("client_secret") != SECRET:
            return 401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret."}
        scope = form["scope"]
        tok = "tok-graph" if "graph" in scope else "tok-imap"
        g.tokens_issued.append(scope)
        return 200, {"token_type": "Bearer", "expires_in": 3599, "access_token": tok}

    def authed(req):
        return req.headers.get("authorization") == "Bearer tok-graph"

    @fake.route("GET", r"/v1.0/organization")
    def org(req):
        return 200, {"value": [{"displayName": "Контосо", "verifiedDomains": [{"name": "contoso.ru"}]}]}

    @fake.route("GET", r"/v1.0/subscribedSkus")
    def skus(req):
        return 200, {"value": g.skus}

    @fake.route("GET", r"/v1.0/users")
    def users(req):
        if not authed(req):
            return 401, {"error": {"code": "InvalidAuthenticationToken", "message": "no"}}
        flt = req.q("$filter")
        if flt.startswith("mail eq "):
            want = flt[len("mail eq '"):-1]
            return 200, {"value": [u for u in g.users.values() if u["mail"] == want]}
        if "signInActivity" in req.q("$select") and g.no_p1:
            return 403, {"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
                                   "message": "Neither tenant is B2C or tenant doesn't have premium license"}}
        items = [copy.deepcopy(u) for u in g.users.values()]
        if "signInActivity" not in req.q("$select"):
            for u in items:
                u.pop("signInActivity", None)
        start = int(req.q("$skiptoken") or 0)
        chunk = items[start:start + g.page]
        body = {"value": chunk}
        if start + g.page < len(items):
            body["@odata.nextLink"] = f"{fake.url}/v1.0/users?$skiptoken={start + g.page}"
        return 200, body

    @fake.route("GET", r"/v1.0/users/([^/]+)")
    def one(req):
        u = g.find(req.group(1))
        if u is None:
            return 404, {"error": {"code": "Request_ResourceNotFound", "message": "Resource does not exist."}}
        out = copy.deepcopy(u)
        out.pop("signInActivity", None)
        return 200, out

    @fake.route("PATCH", r"/v1.0/users/([^/]+)")
    def patch(req):
        u = g.find(req.group(1))
        body = req.json()
        if "proxyAddresses" in body:
            return 400, {"error": {"code": "Request_BadRequest", "message": "read-only"}}
        for k, v in body.items():
            if k == "passwordProfile":
                g.settings.setdefault(u["id"], {})["password"] = dict(v)
            else:
                u[k] = v
        return 204, b""

    @fake.route("POST", r"/v1.0/users/([^/]+)/revokeSignInSessions")
    def revoke(req):
        return 200, {"value": True}

    @fake.route("POST", r"/v1.0/users")
    def create_user(req):
        body = req.json()
        if g.find(body["userPrincipalName"]):
            return 400, {"error": {"code": "Request_BadRequest",
                                   "message": "Another object with the same value for property userPrincipalName "
                                              "already exists."}}
        uid = f"n{len(g.users) + 1}"
        u = user(uid, body["userPrincipalName"], body.get("givenName", ""), body.get("surname", ""), licensed=False)
        u["usageLocation"] = body.get("usageLocation")
        g.users[uid] = u
        g.settings.setdefault(uid, {})["password"] = body["passwordProfile"]
        # как Graph: свойства по умолчанию, mail — null (ящика ещё нет), proxyAddresses не приходит
        shown = {k: u[k] for k in ("id", "userPrincipalName", "displayName", "givenName", "surname", "jobTitle",
                                   "mobilePhone", "officeLocation", "businessPhones")}
        shown["mail"] = None
        return 201, shown

    @fake.route("POST", r"/v1.0/users/([^/]+)/assignLicense")
    def assign(req):
        if g.assign_error:
            return 400, {"error": {"code": "Request_BadRequest", "message": g.assign_error}}
        u = g.find(req.group(1))
        u["assignedLicenses"] = [{"skuId": x["skuId"]} for x in req.json()["addLicenses"]]
        return 200, u

    @fake.route("DELETE", r"/v1.0/users/([^/]+)")
    def delete(req):
        u = g.find(req.group(1))
        g.deleted.append(g.users.pop(u["id"]))
        return 204, b""

    @fake.route("GET", r"/v1.0/users/([^/]+)/mailboxSettings/automaticRepliesSetting")
    def get_auto(req):
        return 200, g.settings.get(req.group(1), {}).get("auto", {"status": "disabled"})

    @fake.route("PATCH", r"/v1.0/users/([^/]+)/mailboxSettings")
    def set_auto(req):
        g.settings.setdefault(req.group(1), {})["auto"] = req.json()["automaticRepliesSetting"]
        return 200, req.json()

    @fake.route("GET", r"/v1.0/users/([^/]+)/mailFolders/inbox/messageRules")
    def rules(req):
        return 200, {"value": list(g.rules.get(req.group(1), {}).values())}

    @fake.route("POST", r"/v1.0/users/([^/]+)/mailFolders/inbox/messageRules")
    def add_rule(req):
        rule = dict(req.json(), id=f"r{len(g.rules) + 1}")
        g.rules.setdefault(req.group(1), {})[rule["id"]] = rule
        return 201, rule

    @fake.route("PATCH", r"/v1.0/users/([^/]+)/mailFolders/inbox/messageRules/([^/]+)")
    def patch_rule(req):
        rule = g.rules.get(req.group(1), {}).get(req.group(2))
        if rule is None:
            return 404, {"error": {"code": "ErrorItemNotFound", "message": "The specified object was not found."}}
        for key, value in req.json().items():
            if isinstance(value, dict) and isinstance(rule.get(key), dict):
                rule[key] = dict(rule[key], **value)      # как Graph: чего нет в запросе, остаётся (и во вложенных)
            else:
                rule[key] = value
        return 200, rule

    @fake.route("DELETE", r"/v1.0/users/([^/]+)/mailFolders/inbox/messageRules/([^/]+)")
    def del_rule(req):
        g.rules[req.group(1)].pop(req.match.group(2))
        return 204, b""

    @fake.route("GET", r"/v1.0/reports/getMailboxUsageDetail\(period='D7'\)")
    def report(req):
        return 302, b"", {"Location": f"{fake.url}/download/report.csv"}

    @fake.route("GET", r"/download/report.csv")
    def csv_file(req):
        assert "authorization" not in req.headers      # ссылка уже с доступом — токен туда не уходит
        head = ("Report Refresh Date,User Principal Name,Display Name,Is Deleted,Deleted Date,Created Date,"
                "Last Activity Date,Item Count,Storage Used (Byte),Issue Warning Quota (Byte),"
                "Prohibit Send Quota (Byte),Prohibit Send/Receive Quota (Byte),Deleted Item Count,"
                "Deleted Item Size (Byte),Deleted Item Quota (Byte),Has Archive,Report Period\n")
        name = "1F2E3D4C5B6A79881F2E3D4C5B6A7988" if g.concealed else "ivanov@contoso.ru"
        rows = (f"2026-10-25,{name},Иван,False,,2024-01-01,2026-10-24,1200,5368709120,96636764160,"
                f"100931731456,107374182400,0,0,32212254720,False,7\n")
        return 200, ("﻿" + head + rows).encode("utf-8"), {"Content-Type": "application/octet-stream"}

    g.fake = fake
    return g, fake
