"""Подключение mailcow: учебный REST API mailcow (/api/v1, X-API-Key) поверх FakeHttp и IMAP с мастер-пользователем.

Учебный сервер ведёт себя как mailcow: изменения отвечают 200 и списком {type, log, msg} (отказ — тоже 200),
пустой список — ``{}``, одни ящики отдаются с числами, другие — со строками (как в примерах OpenAPI), квота при
записи — в МБ, при чтении — в байтах, ``active`` 0/1/2, ключ «только чтение» на изменения получает 403.
Проверяется весь путь: проверка подключения, сверка, действия раздела через драйвер, удаление только при полном
архиве и передача адреса удалённой учётной записи коллеге, создание учётных записей, вход по IMAP
«ящик*мастер@mailcow.local» и то, что ключ API и пароли не попадают в журнал подключения.
"""
import copy
import json
import re
import urllib.parse

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import FakeHttp, connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import NotFound, ServerError
from mailarchiver.mailservers.drivers.mailcow import FILTER_DESC, MailcowDriver
from mailarchiver.mailservers.http import ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
RW_KEY = "A1B2C3-D4E5F6-0A1B2C-3D4E5F-6A7B8C"
RO_KEY = "ROROR0-ONLY00-READ00-KEY000-111111"
MASTER_PASS = "MasterPass2026x"
MIB = 1024 * 1024


def mailbox(user, name, *, active=1, quota_mib=3072, used=0, messages=0, last_imap=0, last_smtp=0,
            authsource="mailcow", force_pw=False, strings=False, tags=None):
    """Ящик как его отдаёт get/mailbox (текущий код mailcow; strings=True — как в примерах OpenAPI)."""
    local, domain = user.split("@")
    quota = quota_mib * MIB
    m = {"username": user, "active": active, "active_int": active, "domain": domain, "name": name,
         "local_part": local, "quota": quota, "messages": messages,
         "attributes": {"force_pw_update": "1" if force_pw else "0", "force_tfa": "0", "tls_enforce_in": "0",
                        "tls_enforce_out": "0", "sogo_access": "1", "imap_access": "1", "pop3_access": "0",
                        "smtp_access": "1", "sieve_access": "1", "eas_access": "1", "dav_access": "1",
                        "relayhost": "0", "passwd_update": "2026-01-02 10:11:12", "mailbox_format": "maildir:",
                        "quarantine_notification": "hourly", "quarantine_category": "reject", "attribute_hash": ""},
         "custom_attributes": {}, "quota_used": used, "percent_in_use": round(100 * used / quota) if quota else "- ",
         "created": "2025-11-01 09:00:00", "modified": "2026-01-02 10:11:12", "authsource": authsource,
         "percent_class": "success" if quota else "info", "last_imap_login": last_imap, "last_smtp_login": last_smtp,
         "last_pop3_login": 0, "last_sso_login": 0, "max_new_quota": 10737418240, "spam_aliases": 0,
         "pushover_active": 0, "rl": False, "rl_scope": "domain", "is_relayed": 0, "sender_acl": []}
    if tags:
        m["tags"] = list(tags)
    m["_strings"] = strings
    return m


def served(m):
    out = {k: v for k, v in m.items() if not k.startswith("_")}
    out["active_int"] = int(out["active"])
    if m.get("_strings"):
        for key in ("active", "quota", "messages", "quota_used"):
            out[key] = str(out[key])
    return out


def ok(category, action, data, *msg):
    return {"type": "success", "log": [category, action, category, data, None], "msg": list(msg)}


def danger(category, action, data, *msg):
    return {"type": "danger", "log": [category, action, category, data, None],
            "msg": list(msg) if len(msg) > 1 else msg[0]}


class Mailcow:
    """Состояние учебного mailcow."""

    def __init__(self):
        self.mailboxes = {m["username"]: m for m in (
            mailbox("ivanov@company.ru", "Иван Иванов", used=100 * MIB, messages=1532, last_imap=1761470000,
                    last_smtp=1761480000, tags=["office"]),
            mailbox("petrova@company.ru", "Анна Петрова", quota_mib=1024, used=50 * MIB, messages=20, strings=True),
            mailbox("sidorov@company.ru", "Пётр Сидоров", active=2, quota_mib=0, force_pw=True),
            mailbox("kozlov@company.ru", "", authsource="ldap"),
        )}
        self.passwords = {u: "Old-Pass-123" for u in self.mailboxes}
        self.domains = ["company.ru"]
        self.aliases = {}
        self.next_id = 10
        for address, goto, active in (("ivan@company.ru", "ivanov@company.ru", "1"),
                                      ("anna@company.ru", "petrova@company.ru", 1),
                                      ("sales@company.ru", "ivanov@company.ru,petrova@company.ru", 1),
                                      ("@company.ru", "ivanov@company.ru", 1)):
            self.new_alias(address, goto, active)
        self.filters = {}
        self.containers = {f"{n}-mailcow": {"type": "info", "container": f"{n}-mailcow", "state": "running",
                                            "started_at": "2026-10-20T08:00:00Z", "image": f"mailcow/{n}:1.0"}
                           for n in ("dovecot", "postfix", "mysql", "nginx", "php-fpm", "redis", "rspamd", "sogo",
                                     "watchdog")}
        self.containers["watchdog-mailcow"]["state"] = "exited"
        self.mailq = [{"arrival_time": 1761470000, "message_size": 1848, "queue_id": "B98C6260CA1",
                       "queue_name": "deferred", "recipients": ["x@far.away (connection timed out)"],
                       "sender": "ivanov@company.ru"}]
        self.deny_ip = False
        #: mailcow сам чистит псевдонимы удалённого ящика? (не документировано — по умолчанию нет)
        self.clean_aliases_on_delete = False
        self.posts = []
        self.keys_seen = set()

    def new_alias(self, address, goto, active=1):
        self.next_id += 1
        self.aliases[self.next_id] = {"id": self.next_id, "domain": address.split("@")[1], "address": address,
                                      "goto": goto, "is_catch_all": 1 if address.startswith("@") else 0,
                                      "active": active, "active_int": int(active), "sogo_visible": 1,
                                      "in_primary_domain": "", "created": "2026-01-01 10:00:00", "modified": None,
                                      "private_comment": None, "public_comment": None}
        return self.next_id

    def alias(self, address):
        return next((a for a in self.aliases.values() if a["address"] == address), None)

    def mine(self, user):
        return [f for f in self.filters.values() if f["username"] == user]

    # -- маршруты ------------------------------------------------------------------------------
    def auth(self, req, write):
        key = re.sub(r"[^a-zA-Z0-9-]", "", req.headers.get("x-api-key", ""))
        self.keys_seen.add(key)
        if key not in (RW_KEY, RO_KEY):
            return 401, {"type": "error", "msg": "authentication failed"}
        if self.deny_ip:
            return 401, {"type": "error", "msg": "api access denied for ip 127.0.0.1"}
        if write and key == RO_KEY:
            return 403, {"type": "error", "msg": "API read/write access denied"}
        return None

    @staticmethod
    def listing(items):
        return 200, (items if items else {})

    def get(self, req):
        denied = self.auth(req, False)
        if denied:
            return denied
        parts = urllib.parse.unquote(req.group(1)).split("/")
        action, category, obj = parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""
        extra = parts[3] if len(parts) > 3 else ""
        if action != "get":
            return 405, {"type": "error", "msg": "only POST method is allowed"}
        if category == "status" and obj == "version":
            return 200, {"version": "2026-09"}
        if category == "status" and obj == "containers":
            return 200, self.containers
        if category == "status" and obj == "vmail":
            return 200, {"type": "info", "disk": "/dev/mapper/mail--vg-root", "used": "11G", "total": "41G",
                         "used_percent": "28%"}
        if category == "mailq" and obj == "all":
            return 200, self.mailq                      # пустая очередь — [] (исключение из правила {})
        if category == "domain" and obj == "all":
            return self.listing([{"domain_name": d, "active": 1, "mboxes_in_domain": sum(
                1 for m in self.mailboxes.values() if m["domain"] == d), "max_quota_for_mbox": 10737418240}
                for d in self.domains])
        if category == "mailbox":
            if obj == "all":
                wanted = extra.split(",") if extra else None
                return self.listing([served(m) for m in self.mailboxes.values()
                                     if wanted is None or m["domain"] in wanted])
            m = self.mailboxes.get(obj)
            return 200, served(m) if m else {}
        if category == "alias" and obj == "all":
            return self.listing([copy.deepcopy(a) for a in self.aliases.values()])
        if category == "filters":
            if obj == "all":
                return self.listing([dict(f) for f in self.filters.values()])
            return self.listing([dict(f) for f in self.mine(obj)])
        return 404, {"type": "error", "msg": "route not found"}

    def post(self, req):
        parts = urllib.parse.unquote(req.group(1)).split("/")
        action, category = parts[0], parts[1] if len(parts) > 1 else ""
        denied = self.auth(req, action in ("add", "edit", "delete"))
        if denied:
            return denied
        if "application/json" not in req.headers.get("content-type", ""):
            return 200, {"type": "error", "msg": "Cannot find attributes in post data"}
        try:
            body = req.json()
        except ValueError:
            return 400, {"type": "error", "msg": "Request body doesn't contain valid json!"}
        self.posts.append((f"{action}/{category}", body))
        handler = getattr(self, f"{action}_{category}", None)
        if handler is None:
            return 404, {"type": "error", "msg": "route not found"}
        if action == "edit" and (not isinstance(body, dict) or "items" not in body or "attr" not in body):
            return 200, {"type": "error", "msg": "Cannot find attributes in post data"}
        out = handler(body)
        if not out:
            return 200, {"type": "error", "msg": f"Cannot {action} item"}
        return 200, out

    # -- ящики ----------------------------------------------------------------------------------
    def add_mailbox(self, attr):
        user = f"{attr['local_part']}@{attr['domain']}"
        shown = dict(attr, password="*", password2="*")
        if attr["domain"] not in self.domains:
            return [danger("mailbox", "add", shown, "domain_not_found", attr["domain"])]
        if user in self.mailboxes:
            return [danger("mailbox", "add", shown, "object_exists", user)]
        if self.alias(user):
            return [danger("mailbox", "add", shown, "is_alias", user)]
        if attr.get("password") != attr.get("password2"):
            return [danger("mailbox", "add", shown, "password_mismatch")]
        if len(attr.get("password") or "") < 8:
            return [danger("mailbox", "add", shown, "password_complexity")]
        m = mailbox(user, attr.get("name") or "", active=int(attr.get("active", 1)),
                    quota_mib=int(attr["quota"]) if "quota" in attr else 3072,
                    force_pw=str(attr.get("force_pw_update")) == "1")
        self.mailboxes[user] = m
        self.passwords[user] = attr["password"]
        return [ok("mailbox", "add", shown, "mailbox_added", user)]

    def edit_mailbox(self, body):
        out = []
        for user in body["items"]:
            attr = body["attr"]
            shown = dict(attr, **({"password": "*", "password2": "*"} if "password" in attr else {}))
            m = self.mailboxes.get(user)
            if m is None:
                out.append(danger("mailbox", "edit", shown, "username_invalid", user))
                continue
            if "active" in attr:
                m["active"] = int(attr["active"])
            if attr.get("password"):
                if attr["password"] != attr.get("password2"):
                    out.append(danger("mailbox", "edit", shown, "password_mismatch"))
                    continue
                if m["authsource"] == "mailcow":
                    self.passwords[user] = attr["password"]
            if "force_pw_update" in attr:
                m["attributes"]["force_pw_update"] = str(int(attr["force_pw_update"]))
            if attr.get("quota", "") != "":
                mib = int(attr["quota"])
                if mib > 10240:
                    out.append(danger("mailbox", "edit", shown, "mailbox_quota_exceeded", 10240))
                    continue
                m["quota"] = mib * MIB
            if attr.get("name"):
                m["name"] = attr["name"]
            out.append(ok("mailbox", "edit", shown, "mailbox_modified", user))
        return out

    def delete_mailbox(self, users):
        out = []
        for user in users:
            if self.mailboxes.pop(user, None) is None:
                out.append(danger("mailbox", "delete", users, "access_denied"))
                continue
            if self.clean_aliases_on_delete:
                for a in list(self.aliases.values()):
                    if a["goto"] == user:
                        self.aliases.pop(a["id"])
            for f in self.mine(user):
                self.filters.pop(f["id"])
            out.append(ok("mailbox", "delete", users, "mailbox_removed", user))
        return out

    # -- псевдонимы -----------------------------------------------------------------------------
    def add_alias(self, attr):
        address, goto = attr["address"], attr.get("goto", "")
        if not goto:
            return [danger("alias", "add", attr, "goto_empty")]
        if self.alias(address) or address in self.mailboxes:
            return [danger("alias", "add", attr, "is_alias_or_mailbox", address)]
        if address.split("@")[1] not in self.domains:
            return [danger("alias", "add", attr, "domain_not_found", address.split("@")[1])]
        new_id = self.new_alias(address, goto, attr.get("active", "1"))
        return [ok("alias", "add", attr, "alias_added", address, new_id)]

    def edit_alias(self, body):
        out = []
        for item in body["items"]:
            a = self.aliases.get(int(item))
            if a is None:
                out.append(danger("alias", "edit", body["attr"], "access_denied"))
                continue
            for key in ("goto", "active"):
                if key in body["attr"]:
                    a[key] = body["attr"][key]
            out.append(ok("alias", "edit", body["attr"], "alias_modified", a["address"]))
        return out

    def delete_alias(self, items):
        out = []
        for item in items:
            a = self.aliases.get(int(item)) if str(item).isdigit() else self.alias(item)
            if a is None:
                out.append(danger("alias", "delete", items, "access_denied"))
                continue
            self.aliases.pop(a["id"])
            out.append(ok("alias", "delete", items, "alias_removed", a["address"]))
        return out

    # -- фильтры Sieve --------------------------------------------------------------------------
    def _activate(self, f):
        for other in self.mine(f["username"]):
            if other["id"] != f["id"] and other["filter_type"] == f["filter_type"]:
                other["active"] = 0

    def add_filter(self, attr):
        if not (attr.get("script_data") or "").strip():
            return [danger("filter", "add", attr, "script_empty")]
        if "require" not in attr["script_data"]:
            return [danger("filter", "add", attr, "sieve_error", "line 1: unknown command")]
        self.next_id += 1
        f = {"id": self.next_id, "username": attr["username"], "filter_type": attr["filter_type"],
             "script_desc": attr["script_desc"], "script_data": attr["script_data"],
             "active": 1 if str(attr.get("active")) == "1" else 0}
        self.filters[f["id"]] = f
        if f["active"]:
            self._activate(f)
        return [ok("filter", "add", attr, "mailbox_modified", attr["username"])]

    def edit_filter(self, body):
        out = []
        for item in body["items"]:
            f = self.filters[int(item)]
            for key in ("script_desc", "script_data", "filter_type"):
                if key in body["attr"]:
                    f[key] = body["attr"][key]
            if "active" in body["attr"]:
                f["active"] = 1 if str(body["attr"]["active"]) == "1" else 0
                if f["active"]:
                    self._activate(f)
            out.append(ok("filter", "edit", body["attr"], "mailbox_modified", f["username"]))
        return out

    def delete_filter(self, items):
        for item in items:
            self.filters.pop(int(item))
        return [ok("filter", "delete", items, "delete_filter", str(i)) for i in items]


@pytest.fixture()
def mc():
    m = Mailcow()
    fake = FakeHttp()
    fake.route("GET", r"/api/v1/(.+)")(m.get)
    fake.route("POST", r"/api/v1/(.+)")(m.post)
    m.fake = fake
    yield m
    fake.close()


def connect(svc, mc, **extra):
    values = {"type": "mailcow", "name": "Почта компании", "host": mc.fake.url, "api_key": RW_KEY,
              "master_user": "archmaster", "master_password": MASTER_PASS, "imap_host": "mail.company.ru",
              "allow_changes": True}
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


@pytest.fixture()
def cow(services, mc):
    sid = connect(services, mc)
    res = mss.sync(services, sid, user="tester")
    key = lambda email: f"ms:{sid}:{email}"  # noqa: E731
    return type("C", (), {"svc": services, "mc": mc, "sid": sid, "sync": res, "key": staticmethod(key),
                          "iv": key("ivanov@company.ru"), "pe": key("petrova@company.ru"),
                          "si": key("sidorov@company.ru"), "ko": key("kozlov@company.ru")})


def imap_world(monkeypatch, *emails):
    world = ImapWorld()
    for email in emails:
        world.mailbox(email, "nopass")
        world.add(email, "INBOX", raw_message(f"Отчёт {email}"))
    world.admins["archmaster@mailcow.local"] = MASTER_PASS
    world.master_separator = "*"
    monkeypatch.setattr(client_mod, "IMAPClient", world.client_class())
    return world


# ---------------------------------------------------------------------------
#  Подключение и проверка
# ---------------------------------------------------------------------------
def test_check_reads_version_domains_containers_queue(services, mc):
    sid = connect(services, mc)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["version"] == "mailcow 2026-09" and st["queue"] == 1
    assert any("company.ru" in d for d in st["details"]) and any("Ящиков: 4" in d for d in st["details"])
    assert any("работают 8 из 9" in d for d in st["details"])
    assert any("archmaster@mailcow.local" in d for d in st["details"])
    problems = {p[0]: p for p in st["problems"]}
    assert problems["container_watchdog"][1] == "warning" and "exited" in problems["container_watchdog"][2]
    assert "api_read_only" not in problems
    # ключ — только в заголовке X-API-Key, не в адресе; в журнал не попадает
    assert RW_KEY in mc.keys_seen and all(RW_KEY not in c.path for c in mc.fake.calls)
    text = transcripts(services, sid)
    assert RW_KEY not in text and MASTER_PASS not in text
    # остановился dovecot — ошибка, а не предупреждение
    mc.containers["dovecot-mailcow"]["state"] = "restarting"
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and any(p[0] == "container_dovecot" and p[1] == "error" for p in out["status"]["problems"])


def test_wrong_key_and_ip_are_explained_and_not_logged(services, mc):
    sid = connect(services, mc, api_key="Wrong-Key-0000")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "не принял ключ API" in out["error"] and "netfilter" in out["hint"]
    sid2 = connect(services, mc, name="Второй")
    mc.deny_ip = True
    out = mss.check(services, sid2, user="tester")
    assert not out["ok"] and "не принимает запросы к API с этого адреса" in out["error"]
    assert "Разрешённые IP" in out["hint"]
    for s in (sid, sid2):
        text = transcripts(services, s)
        assert "Wrong-Key-0000" not in text and RW_KEY not in text and MASTER_PASS not in text


def test_read_only_key_reads_but_does_not_write(services, mc):
    sid = connect(services, mc, api_key=RO_KEY)
    out = mss.check(services, sid, user="tester")
    assert out["ok"] and any(p[0] == "api_read_only" and "только для чтения" in p[2] for p in out["status"]["problems"])
    mss.sync(services, sid, user="tester")
    _res, items = act(services, "ax_block", [f"ms:{sid}:ivanov@company.ru"], final_copy=False)
    item = items[f"ms:{sid}:ivanov@company.ru"]
    assert item["status"] == "failed" and "только для чтения" in item["detail"], item
    assert mc.mailboxes["ivanov@company.ru"]["active"] == 1


def test_settings_validation_and_plain_http():
    with pytest.raises(ValidationError, match="латинские буквы"):
        MailcowDriver.validate_settings({"host": "mail.company.ru"}, {"api_key": "ключ с пробелом"})
    with pytest.raises(ValidationError, match="пароль мастер-пользователя"):
        MailcowDriver.validate_settings({"host": "mail.company.ru", "master_user": "archmaster"}, {"api_key": RW_KEY})
    with pytest.raises(ValidationError, match="адрес указан неверно"):
        MailcowDriver.validate_settings({"host": "https://mail.company.ru/?x=1"}, {"api_key": RW_KEY})
    # http на другую машину — только если разрешено; https и путь /api/v1 в адресе — как есть
    drv = MailcowDriver(connection("mailcow", {"host": "http://10.0.0.5"}, {"api_key": RW_KEY}), ApiLog())
    with pytest.raises(ServerError, match="без шифрования"):
        _ = drv.http
    drv = MailcowDriver(connection("mailcow", {"host": "http://10.0.0.5", "allow_plain": True}, {"api_key": RW_KEY}),
                        ApiLog())
    assert drv.http.url("/get/status/version") == "http://10.0.0.5:80/api/v1/get/status/version"
    drv = MailcowDriver(connection("mailcow", {"host": "https://mail.company.ru:8443/api/v1/"}, {"api_key": RW_KEY}),
                        ApiLog())
    assert drv.http.url("/x") == "https://mail.company.ru:8443/api/v1/x"
    assert drv.imap_server() == ("mail.company.ru", 993, "ssl")


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def test_sync_normalizes_numbers_strings_aliases_and_logins(cow):
    rows = {r["email"]: r for r in msstore.list_accounts(cow.svc.db, cow.sid)}
    assert set(rows) == {"ivanov@company.ru", "petrova@company.ru", "sidorov@company.ru", "kozlov@company.ru"}
    iv = rows["ivanov@company.ru"]
    # свой псевдоним — да; общий (на два ящика) и catch-all — нет
    assert iv["aliases"] == ["ivan@company.ru"] and iv["info"]["shared_aliases"] == ["sales@company.ru"]
    assert iv["login_blocked"] is False and iv["quota_kb"] == 3072 * 1024 and iv["used_bytes"] == 100 * MIB
    assert iv["messages"] == 1532 and iv["first_name"] == "Иван" and iv["last_name"] == "Иванов"
    assert iv["display_name"] == "Иван Иванов"
    assert iv["last_login"] == "2025-10-26T12:00:00Z"          # самый поздний из IMAP/SMTP/POP3/SSO
    assert iv["autoreply"] is False and iv["forward_to"] == [] and iv["must_change"] is False
    assert "pop3" in iv["info"]["protocols_off"] and iv["info"]["tags"] == ["office"]
    pe = rows["petrova@company.ru"]                              # значения строками, как в примерах OpenAPI
    assert pe["quota_kb"] == 1024 * 1024 and pe["used_bytes"] == 50 * MIB and pe["messages"] == 20
    assert pe["login_blocked"] is False and pe["aliases"] == ["anna@company.ru"] and pe["last_login"] == ""
    si = rows["sidorov@company.ru"]
    assert si["login_blocked"] is True and si["quota_kb"] is None and si["must_change"] is True
    assert si["info"]["state"] == "вход запрещён, письма принимаются"
    assert rows["kozlov@company.ru"]["info"]["authsource"] == "ldap"
    assert cow.sync["incomplete"] is False and not cow.sync["problems"]
    # фильтров нет — mailcow отдаёт {} вместо [] (то же с псевдонимами)
    assert len(cow.mc.fake.calls_to("GET", r"/api/v1/get/filters/all")) == 1
    cow.mc.aliases.clear()
    mss.sync(cow.svc, cow.sid, user="tester")
    assert msstore.get_account(cow.svc.db, cow.sid, "ivanov@company.ru")["aliases"] == []
    # ни одного ящика (например, ключ без доступа) — не значит, что все удалены
    saved = dict(cow.mc.mailboxes)
    cow.mc.mailboxes.clear()
    res = mss.sync(cow.svc, cow.sid, user="tester")
    assert res["incomplete"] is True and res["problems"]
    assert not msstore.get_account(cow.svc.db, cow.sid, "ivanov@company.ru")["deleted_at"]
    cow.mc.mailboxes.update(saved)


def test_address_with_plus_is_read_through_domain_list(mc):
    mc.mailboxes["sales+eu@company.ru"] = mailbox("sales+eu@company.ru", "Продажи Европа")
    drv = MailcowDriver(connection("mailcow", {"host": mc.fake.url}, {"api_key": RW_KEY}), ApiLog())
    acc = drv.get_account("Sales+EU@company.ru")
    assert acc["email"] == "sales+eu@company.ru" and acc["display_name"] == "Продажи Европа"
    assert all("+" not in c.path for c in mc.fake.calls)
    assert drv.get_account("nobody@company.ru") is None          # нет ящика — {} с кодом 200


# ---------------------------------------------------------------------------
#  Действия раздела
# ---------------------------------------------------------------------------
def test_block_with_and_without_receiving_then_unblock(cow):
    svc, mc = cow.svc, cow.mc
    _res, items = act(svc, "ax_block", [cow.iv], final_copy=False, keep_receiving=True)
    assert items[cow.iv]["status"] == "success", items
    assert mc.mailboxes["ivanov@company.ru"]["active"] == 2 and "продолжают приниматься" in items[cow.iv]["detail"]
    assert msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")["login_blocked"] is True
    _res, items = act(svc, "ax_unblock", [cow.iv])
    assert items[cow.iv]["status"] == "success" and mc.mailboxes["ivanov@company.ru"]["active"] == 1
    _res, items = act(svc, "ax_block", [cow.pe], final_copy=False, keep_receiving=False)
    assert items[cow.pe]["status"] == "success", items
    assert mc.mailboxes["petrova@company.ru"]["active"] == 0 and "не принимаются" in items[cow.pe]["detail"]
    sent = [b for path, b in mc.posts if path == "edit/mailbox"]
    assert {"items": ["petrova@company.ru"], "attr": {"active": "0"}} in sent


def test_password_must_change_and_ldap_account(cow):
    svc, mc = cow.svc, cow.mc
    _res, items = act(svc, "ax_password", [cow.iv], mode="random_hidden", must_change=True)
    assert items[cow.iv]["status"] == "success", items
    password = mc.passwords["ivanov@company.ru"]
    assert len(password) == 20 and mc.mailboxes["ivanov@company.ru"]["attributes"]["force_pw_update"] == "1"
    assert msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")["must_change"] is True
    assert password not in transcripts(svc, cow.sid)
    _res, items = act(svc, "ax_must_change", [cow.iv], value="no")
    assert items[cow.iv]["status"] == "success" and mc.mailboxes["ivanov@company.ru"]["attributes"][
        "force_pw_update"] == "0"
    # вход через LDAP — пароль не в mailcow: пропуск с причиной, ничего не отправлено
    before = len(mc.posts)
    _res, items = act(svc, "ax_password", [cow.ko], mode="random_hidden", must_change=False)
    assert items[cow.ko]["status"] == "skipped" and "не в mailcow" in items[cow.ko]["detail"], items
    assert len(mc.posts) == before


def test_quota_in_mib_and_refusal(cow):
    svc, mc = cow.svc, cow.mc
    _res, items = act(svc, "ax_quota", [cow.pe], quota_mb=4096, only_raise=False, threshold=0)
    assert items[cow.pe]["status"] == "success", items
    assert mc.mailboxes["petrova@company.ru"]["quota"] == 4096 * MIB
    assert ("edit/mailbox", {"items": ["petrova@company.ru"], "attr": {"quota": "4096"}}) in mc.posts
    assert msstore.get_account(svc.db, cow.sid, "petrova@company.ru")["quota_kb"] == 4096 * 1024
    # больше предела домена — отказ mailcow (200 + danger) понятным текстом
    _res, items = act(svc, "ax_quota", [cow.pe], quota_mb=20480, only_raise=False, threshold=0)
    assert items[cow.pe]["status"] == "failed" and "не больше 10240 МБ" in items[cow.pe]["detail"], items


def test_alias_add_and_remove(cow):
    svc, mc = cow.svc, cow.mc
    pv = base.run(svc, ADMIN, "ax_alias_add", [cow.iv], {"template": "{first}"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "уже есть" in pv["results"][0]["detail"]      # ivan@ — уже его
    _res, items = act(svc, "ax_alias_add", [cow.pe], template="{f}.{last}")
    assert items[cow.pe]["status"] == "success", items
    created = mc.alias("a.petrova@company.ru")
    assert created and created["goto"] == "petrova@company.ru"
    assert set(msstore.get_account(svc.db, cow.sid, "petrova@company.ru")["aliases"]) == {"anna@company.ru",
                                                                                        "a.petrova@company.ru"}
    _res, items = act(svc, "ax_alias_remove", [cow.pe], which="list", aliases="a.petrova")
    assert items[cow.pe]["status"] == "success", items
    assert mc.alias("a.petrova@company.ru") is None and mc.alias("anna@company.ru") is not None
    assert msstore.get_account(svc.db, cow.sid, "petrova@company.ru")["aliases"] == ["anna@company.ru"]


def test_alias_taken_by_other_mailbox_is_refused(mc):
    drv = MailcowDriver(connection("mailcow", {"host": mc.fake.url}, {"api_key": RW_KEY}), ApiLog())
    with pytest.raises(ServerError, match="уже занят — это псевдоним, письма с него идут на ivanov@company.ru"):
        drv.add_alias("petrova@company.ru", "ivan@company.ru")
    with pytest.raises(ServerError, match="уже занят"):
        drv.add_alias("petrova@company.ru", "sidorov@company.ru")      # это ящик — отказ mailcow
    with pytest.raises(NotFound):
        drv.remove_alias("petrova@company.ru", "nosuch@company.ru")
    assert drv.add_alias("ivanov@company.ru", "ivan@company.ru") == ["ivan@company.ru"]   # уже его — ничего не меняем


def test_contact_full_name_from_employees(cow):
    svc, mc = cow.svc, cow.mc
    svc.db.create_employee(full_name="Козлов Пётр Ильич", email="kozlov@company.ru", department="ИТ")
    _res, items = act(svc, "ax_contact", [cow.ko], source="employees", name_order="last_first", overwrite=False,
                      phone=False)
    assert items[cow.ko]["status"] == "success", items
    assert mc.mailboxes["kozlov@company.ru"]["name"] == "Пётр Козлов"
    row = msstore.get_account(svc.db, cow.sid, "kozlov@company.ru")
    assert row["first_name"] == "Пётр" and row["last_name"] == "Козлов"


def test_autoreply_and_forwarding_share_one_prefilter(cow):
    svc, mc = cow.svc, cow.mc
    _res, items = act(svc, "srv_autoreply", [cow.iv], mode="on", subject="Нет на месте",
                      text="Я в отпуске.\nПишите \"Анне\": petrova@company.ru", start="2026-11-01", end="2026-11-10")
    assert items[cow.iv]["status"] == "success", items
    mine = mc.mine("ivanov@company.ru")
    assert len(mine) == 1 and mine[0]["filter_type"] == "prefilter" and mine[0]["active"] == 1
    assert mine[0]["script_desc"] == FILTER_DESC
    script = mine[0]["script_data"]
    assert 'require ["vacation", "date", "relational"];' in script
    assert 'currentdate :value "le" "date" "2026-11-10"' in script and '\\"Анне\\"' in script
    assert ':addresses ["ivanov@company.ru", "ivan@company.ru"]' in script
    assert msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")["autoreply"] is True
    _res, items = act(svc, "srv_forwarding", [cow.iv], mode="on", addresses="boss@partner.ru", keep_copy=False)
    assert items[cow.iv]["status"] == "success", items
    mine = mc.mine("ivanov@company.ru")
    assert len(mine) == 1 and 'redirect "boss@partner.ru";' in mine[0]["script_data"] and "stop;" in \
        mine[0]["script_data"] and "vacation" in mine[0]["script_data"]
    row = msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")
    assert row["forward_to"] == ["boss@partner.ru"] and row["autoreply"] is True
    drv = MailcowDriver(connection("mailcow", {"host": mc.fake.url}, {"api_key": RW_KEY}), ApiLog())
    assert drv.get_forwarding("ivanov@company.ru") == {"addresses": ["boss@partner.ru"], "keep_copy": False}
    assert drv.get_autoreply("ivanov@company.ru")["end"] == "2026-11-10"
    _res, items = act(svc, "srv_forwarding", [cow.iv], mode="off", addresses="", keep_copy=True)
    assert "redirect" not in mc.mine("ivanov@company.ru")[0]["script_data"]
    _res, items = act(svc, "srv_autoreply", [cow.iv], mode="off")
    assert items[cow.iv]["status"] == "success" and mc.mine("ivanov@company.ru") == []
    assert msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")["autoreply"] is False
    # у ящика свой включённый prefilter — mailcow держит один, чужой не выключаем
    mc.filters[900] = {"id": 900, "username": "petrova@company.ru", "filter_type": "prefilter",
                       "script_desc": "Спам в папку", "script_data": "require [\"fileinto\"];", "active": 1}
    _res, items = act(svc, "srv_autoreply", [cow.pe], mode="on", subject="", text="Отпуск", start="", end="")
    assert items[cow.pe]["status"] == "failed" and "Спам в папку" in items[cow.pe]["detail"], items
    assert mc.filters[900]["active"] == 1 and len(mc.mine("petrova@company.ru")) == 1


def test_imap_copy_with_master_user(cow, monkeypatch):
    svc = cow.svc
    world = imap_world(monkeypatch, "petrova@company.ru")
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [cow.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.auth_type == AuthType.SERVER and acc.server_id == cow.sid and acc.host == "mail.company.ru"
    assert acc.password == ""
    from mailarchiver.imap.client import ImapConnection
    conn = ImapConnection(acc, svc.connect_options())
    conn.connect()
    try:
        assert world.last_login == ("separator", "archmaster@mailcow.local", "petrova@company.ru")
    finally:
        conn.close()


def test_delete_only_with_full_archive_then_transfer_address(cow, monkeypatch):
    svc, mc = cow.svc, cow.mc
    world = imap_world(monkeypatch, "petrova@company.ru", "ivanov@company.ru")
    base.run(svc, ADMIN, "ax_add_mailboxes", [cow.pe], {}, preview=False)
    pv = base.run(svc, ADMIN, "ax_delete", [cow.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "сначала «Заблокировать вход»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_block", [cow.pe], final_copy=True, keep_receiving=True)
    assert items[cow.pe]["status"] == "success" and "последняя копия сделана" in items[cow.pe]["detail"], items
    assert mc.mailboxes["petrova@company.ru"]["active"] == 2
    assert world.last_login == ("separator", "archmaster@mailcow.local", "petrova@company.ru")
    # письмо пришло после копии — в архиве его нет: учётная запись не удаляется
    real_fresh = __import__("mailarchiver.serverops.imap_ops", fromlist=["fresh_copy"]).fresh_copy

    def copy_then_new_mail(ctx, acc, emit):
        out = real_fresh(ctx, acc, emit)
        world.add("petrova@company.ru", "INBOX", raw_message("Пришло после копии"))
        return out
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", copy_then_new_mail)
    _res, items = act(svc, "ax_delete", [cow.pe])
    assert items[cow.pe]["status"] == "skipped" and "НЕ удалена" in items[cow.pe]["detail"], items
    assert "petrova@company.ru" in mc.mailboxes
    monkeypatch.setattr("mailarchiver.serverops.imap_ops.fresh_copy", real_fresh)
    _res, items = act(svc, "ax_delete", [cow.pe])
    assert items[cow.pe]["status"] == "success", items
    assert "petrova@company.ru" not in mc.mailboxes and "_garbage" in items[cow.pe]["detail"]
    # mailcow оставил псевдонимы удалённого ящика — драйвер их убрал: свой — удалён, общий — без неё
    assert mc.alias("anna@company.ru") is None
    assert mc.alias("sales@company.ru")["goto"] == "ivanov@company.ru"
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    row = msstore.get_account(svc.db, cow.sid, "petrova@company.ru")
    assert row["deleted_at"] and row["aliases"] == ["anna@company.ru"]
    # передать адрес коллеге: письма на petrova@ и anna@ теперь приходят Иванову
    pv = base.run(svc, ADMIN, "ax_forward", [cow.pe], {"to": "ivanov@company.ru", "with_aliases": True},
                  preview=True)
    assert pv["counts"]["ok"] == 1, pv
    _res, items = act(svc, "ax_forward", [cow.pe], to="ivanov@company.ru", with_aliases=True)
    assert items[cow.pe]["status"] == "success", items
    assert mc.alias("petrova@company.ru")["goto"] == "ivanov@company.ru"
    assert mc.alias("anna@company.ru")["goto"] == "ivanov@company.ru"
    assert {"petrova@company.ru", "anna@company.ru"} <= set(
        msstore.get_account(svc.db, cow.sid, "ivanov@company.ru")["aliases"])
    pv = base.run(svc, ADMIN, "ax_forward", [cow.pe], {"to": "ivanov@company.ru"}, preview=True)
    assert "уже переданы" in pv["results"][0]["detail"]


def test_create_accounts_and_already_exists(cow, monkeypatch):
    svc, mc = cow.svc, cow.mc
    params = {"server_id": cow.sid, "text": "new.user@company.ru; Новый; Сотрудник\nivanov@company.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 2048, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@company.ru"]["status"] == "ok"
    assert by["ivanov@company.ru"]["status"] == "skip" and "уже есть" in by["ivanov@company.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert not pv["warnings"]
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = mc.mailboxes["new.user@company.ru"]
    assert new["name"] == "Новый Сотрудник" and new["quota"] == 2048 * MIB
    assert new["attributes"]["force_pw_update"] == "1" and len(mc.passwords["new.user@company.ru"]) == 20
    row = msstore.get_account(svc.db, cow.sid, "new.user@company.ru")
    assert row["first_name"] == "Новый" and row["last_name"] == "Сотрудник" and row["quota_kb"] == 2048 * 1024
    assert any(a.username == "new.user@company.ru" and a.auth_type == AuthType.SERVER for a in svc.db.list_accounts())
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1
    assert mc.passwords["new.user@company.ru"] not in transcripts(svc, cow.sid)
    sent = next(b for path, b in mc.posts if path == "add/mailbox")
    assert sent["quota"] == "2048" and sent["password"] == sent["password2"]
    # то же задание ещё раз: учётная запись уже есть — пропуск (AlreadyExists), а не ошибка
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["skipped"] == 1 and "уже есть" in out["items"][0]["detail"], out


def test_actions_only_axigen_and_report_by_api(cow):
    svc = cow.svc
    with pytest.raises(ValidationError):
        base.run(svc, ADMIN, "ax_account_type", [cow.iv], {}, preview=True)
    pv = base.run(svc, ADMIN, "report", [cow.iv], {}, preview=True)
    assert pv["counts"]["ok"] == 1 and "через API mailcow" in pv["results"][0]["detail"]
    desc = MailcowDriver.describe()
    assert desc["cloud"] is False and {"verify_tls", "cert_sha256", "allow_plain", "imap_host"} <= {
        f["key"] for f in desc["fields"]}
    assert [f for f in desc["fields"] if f["key"] in ("api_key", "master_password")] and all(
        f["secret"] for f in desc["fields"] if f["key"] in ("api_key", "master_password"))
    assert json.dumps(desc, ensure_ascii=False)


# -- удаление: что бы ни случилось с псевдонимами, удалённый ящик — удалён --------------------------------
def _after_delete(mc, *, get_alias=None, change_alias=None):
    """Учебный mailcow, который после удаления ящика не отдаёт псевдонимы или отказывает в их изменении."""
    state = {"deleted": False}
    real_get, real_post = mc.get, mc.post

    def get(req):
        if state["deleted"] and get_alias is not None and req.path.endswith("/get/alias/all"):
            return get_alias
        return real_get(req)

    def post(req):
        if state["deleted"] and change_alias is not None and req.path.endswith(("/edit/alias", "/delete/alias")):
            return change_alias
        out = real_post(req)
        if req.path.endswith("/delete/mailbox"):
            state["deleted"] = True
        return out
    mc.fake.routes.clear()
    mc.fake.route("GET", r"/api/v1/(.+)")(get)
    mc.fake.route("POST", r"/api/v1/(.+)")(post)


def test_delete_counts_even_if_aliases_are_not_readable_afterwards(cow, monkeypatch):
    svc, mc = cow.svc, cow.mc
    imap_world(monkeypatch, "petrova@company.ru", "ivanov@company.ru")
    base.run(svc, ADMIN, "ax_add_mailboxes", [cow.pe], {}, preview=False)
    _res, items = act(svc, "ax_block", [cow.pe], final_copy=True, keep_receiving=True)
    assert items[cow.pe]["status"] == "success", items
    _after_delete(mc, get_alias=(503, {"type": "error", "msg": "temporarily unavailable"}, {"Retry-After": "0"}))
    _res, items = act(svc, "ax_delete", [cow.pe])
    assert items[cow.pe]["status"] == "success", items
    assert "petrova@company.ru" not in mc.mailboxes
    assert "псевдонимы, которые вели в удалённый ящик, не проверены" in items[cow.pe]["detail"]
    # удаление засчитано: архив — на бессрочном удержании, копирование выключено, в снимке — удалена
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@company.ru")
    assert acc.hold_until == "9999-12-31" and acc.enabled is False
    assert msstore.get_account(svc.db, cow.sid, "petrova@company.ru")["deleted_at"]


def test_delete_reports_aliases_it_could_not_fix(cow):
    mc = cow.mc
    _after_delete(mc, change_alias=(200, [danger("alias", "edit", [], "access_denied")]))
    drv = MailcowDriver(connection("mailcow", {"host": mc.fake.url}, {"api_key": RW_KEY}), ApiLog())
    text = drv.delete_account("petrova@company.ru")                  # не исключение: ящик уже удалён
    assert "petrova@company.ru" not in mc.mailboxes and text.startswith("учётная запись удалена")
    assert "не исправлены" in text and "sales@company.ru" in text and "anna@company.ru" in text
    assert mc.alias("anna@company.ru") is not None                   # mailcow отказал — псевдоним остался
