"""Подключение MDaemon: учебный XML API (/MdMgmtWS поверх tests/mailserver_fake.py).

Проверяется весь путь: вход (HTTP Basic — только у Logon, дальше токен в X-MDAPI-TOKEN), Logoff, токен, который
перестал приниматься (вход заново, но один раз), Status с ошибкой и не-XML ответ, сверка по доменам (служебная строка
MailboxRoot пропускается), действия раздела через драйвер (точные имена элементов — сервер их различает по
регистру), создание учётных записей и то, что пароли и токен не попадают в журнал подключения.
"""
import base64
import copy
import re
import xml.etree.ElementTree as ET

import pytest

from mailserver_fake import FakeHttp, connection
from mailarchiver import models
from mailarchiver.errors import ValidationError
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import AlreadyExists, ServerError
from mailarchiver.mailservers.drivers import mdaemon as mdmod
from mailarchiver.mailservers.http import ApiLog
from mailarchiver.models import JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
LOGIN = "admin@company.ru"
PASSWORD = "Md-Admin~Pass7"
IMAP_HOST = "mail.company.ru"
NO = "0000-00-00T00:00:00.000Z"
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
#: какие элементы сервер понимает (регистр важен) — остальное «не найдено»
ALLOWED = {"Parameters": {"Domain", "Mailbox", "Get", "Details", "Quotas", "Forwarding", "AutoResponder"},
           "Get": {"Users", "All"}, "Users": {"Quotas", "Details", "Aliases"},
           "Details": {"Password", "FirstName", "LastName"},
           "Quotas": {"ApplyQuotas", "MaxDiskSpace", "MaxMessageCount"},
           "Forwarding": {"Enabled", "SendAs", "RetainMail", "Addresses", "Schedule"}, "Addresses": {"Address"},
           "Schedule": {"Start", "End", "DaysActive"},
           "AutoResponder": {"Enabled", "Start", "End", "Response", "DaysActive"}}


def box(mailbox, domain, display, first, last, *, apply=False, max_mb=0, max_msgs=0, items=0, aliases=()):
    return {"mailbox": mailbox, "domain": domain, "display": display, "first": first, "last": last,
            "password": "Init-pass-1", "apply": apply, "max_mb": max_mb, "max_msgs": max_msgs, "items": items,
            "aliases": list(aliases),
            "forward": {"enabled": False, "retain": True, "addresses": [], "start": NO, "end": NO, "mask": "0x00"},
            "auto": {"enabled": False, "start": NO, "end": NO, "response": ""}}


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


class MD:
    """Состояние учебного сервера MDaemon."""

    def __init__(self):
        self.domains = ["company.ru", "filial.ru"]
        boxes = [
            box("ivanov", "company.ru", "Иван Иванов", "Иван", "Иванов", apply=True, max_mb=10240, items=1200,
                aliases=["ivan@company.ru", "i.ivanov@company.ru", "iv?nov@company.ru"]),
            box("petrova", "company.ru", "Анна Петрова", "Анна", "Петрова", max_msgs=5000, items=10),
            box("sidorov", "company.ru", "Пётр Сидоров", "Пётр", "Сидоров", apply=True, max_mb=2048, items=50),
            box("info", "filial.ru", "Филиал", "", ""),
        ]
        self.boxes = {f"{b['mailbox']}@{b['domain']}": b for b in boxes}
        self.tokens = set()
        self.logons = 0
        self.logoffs = 0
        self.expire_next = None
        self.expire_all = False
        self.broken_domain = None
        self.broken_box = None
        self.garbage = False
        self.reply_passwords = False  # сервер показывает пароль в Details (журнал его скрывает)
        self.fail_once = {}          # операция → код ответа шлюза (502…) один раз
        self.requests = []           # (операция, version, Parameters, заголовки)

    def ops(self, name):
        return [r for r in self.requests if r[0] == name]


def reply(status_id=0, value="0x00000000", message="The operation completed successfully.", result=None):
    root = ET.Element("MDaemon")
    api = sub(root, "API", productversion="25.0.2", serviceversion="25.0.2.1")
    resp = sub(api, "Response", version="25.0", et="0.0123", session="063FD3E1")
    sub(resp, "Status", id=status_id, value=value, message=message)
    if result is not None:
        resp.append(result)
    return 200, '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode"), \
        {"Content-Type": "text/xml; charset=utf-8"}


def user_el(b, sections, md=None):
    el = ET.Element("User", {"id": b["mailbox"], "displayname": b["display"], "domain": b["domain"]})
    if "Quotas" in sections:
        q = sub(el, "Quotas")
        sub(q, "ApplyQuotas", "Yes" if b["apply"] else "No")
        sub(q, "MaxDiskSpace", b["max_mb"])
        sub(q, "MaxMessageCount", b["max_msgs"])
        sub(sub(q, "Usage"), "Items", b["items"])
    if "Details" in sections:
        d = sub(el, "Details")
        sub(d, "FirstName", b["first"])
        sub(d, "LastName", b["last"])
        if md is not None and md.reply_passwords:
            sub(d, "Password", b["password"])
    if "Aliases" in sections:
        a = sub(el, "Aliases")
        for alias in b["aliases"]:
            sub(a, "Alias", alias)
    if "Forwarding" in sections:
        f, fwd = sub(el, "Forwarding"), b["forward"]
        sub(f, "Enabled", "true" if fwd["enabled"] else "false")
        sub(f, "RetainMail", "true" if fwd["retain"] else "false")
        addrs = sub(f, "Addresses")
        for address in fwd["addresses"]:
            sub(addrs, "Address", address)
    if "AutoResponder" in sections:
        r, auto = sub(el, "AutoResponder"), b["auto"]
        sub(r, "Enabled", "Yes" if auto["enabled"] else "No")
        sub(r, "Start", auto["start"])
        sub(r, "End", auto["end"])
        sub(r, "Response", auto["response"])
    return el


def unknown_element(el):
    allowed = ALLOWED.get(el.tag)
    for child in el:
        if allowed is not None and child.tag not in allowed:
            return child.tag
        bad = unknown_element(child)
        if bad:
            return bad
    return None


def install(md: MD, fake: FakeHttp):
    @fake.route("POST", r"/MdMgmtWS")
    def api(req):
        assert req.headers.get("content-type", "").startswith("text/xml")
        if md.garbage:
            return 200, "Service Unavailable", {"Content-Type": "text/plain"}
        root = ET.fromstring(req.body)
        assert root.tag == "MDaemon"
        request = root.find("API/Request")
        op = request.findtext("Operation")
        params = request.find("Parameters")
        md.requests.append((op, request.get("version"), copy.deepcopy(params), dict(req.headers)))
        if op in md.fail_once:
            return md.fail_once.pop(op), "Bad Gateway", {"Content-Type": "text/plain", "Retry-After": "0"}
        if op == "Logon":
            auth = req.headers.get("authorization", "")
            if not auth.startswith("Basic "):
                return reply(1, "0xC1420010", "Authorization required.")
            user, _sep, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            if (user, password) != (LOGIN, PASSWORD):
                return reply(1, "0xC1420011", "Logon failed: the user name or password is incorrect.")
            md.logons += 1
            token = f"T0KEN{md.logons:04d}QWERTY"
            md.tokens.add(token)
            result = ET.Element("Result")
            sub(result, "Token", token)
            sub(result, "Timeout", 1800)
            return reply(result=result)
        assert "authorization" not in req.headers          # Basic — только у Logon
        token = req.headers.get("x-mdapi-token", "")
        if token not in md.tokens:
            return reply(1, "0xC142000A", "Invalid or expired session token.")
        if md.expire_all or (md.expire_next is not None and md.expire_next <= 0):
            md.expire_next = None
            md.tokens.discard(token)
            return reply(1, "0xC142000A", "Invalid or expired session token.")
        if md.expire_next is not None:
            md.expire_next -= 1
        bad = unknown_element(params) if params is not None else None
        if bad:
            return reply(1, "0xC1420001", f"The XML is malformed: element '{bad}' was not found.")
        domain = (params.findtext("Domain") or "") if params is not None else ""
        key = f"{params.findtext('Mailbox') or ''}@{domain}".lower() if params is not None else ""

        if op == "Logoff":
            md.logoffs += 1
            md.tokens.discard(token)
            return reply()
        if op == "GetDomainList":
            result = ET.Element("Result")
            listed = sub(result, "Domains")
            for name in md.domains:
                sub(listed, "Domain", id=name)
            return reply(result=result)
        if op == "GetDomainInfo":
            if domain == md.broken_domain:
                return reply(1, "0xC1420050", f"Access denied to domain {domain}.")
            sections = [s.tag for s in params.find("Get/Users")]
            result = ET.Element("Result")
            users = sub(sub(result, "Domain", id=domain), "Users")
            sub(sub(users, "User", id=""), "MailboxRoot", f"D:\\MDaemon\\Users\\{domain}\\")
            for b in md.boxes.values():
                if b["domain"] == domain:
                    users.append(user_el(b, sections, md))
            return reply(result=result)
        if op == "GetUserInfo":
            assert params.find("Get/All") is not None
            if key == md.broken_box:
                return reply(1, "0xC1420099", "Internal error while reading the account.")
            if key not in md.boxes:
                return reply(1, "0xC1420084", "The mailbox specified is invalid.")
            result = ET.Element("Result")
            result.append(user_el(md.boxes[key], ("Quotas", "Details", "Aliases", "Forwarding", "AutoResponder"), md))
            return reply(result=result)
        if op == "CreateUser":
            if key in md.boxes:
                return reply(1, "0xC1420083", "The account already exists.")
            details = params.find("Details")
            b = box(params.findtext("Mailbox"), domain, "", details.findtext("FirstName") or "",
                    details.findtext("LastName") or "")
            b["display"] = " ".join(x for x in (b["first"], b["last"]) if x)
            b["password"] = details.findtext("Password")
            md.boxes[key] = b
            return reply()
        if op == "UpdateUser":
            if key not in md.boxes:
                return reply(1, "0xC1420084", "The mailbox specified is invalid.")
            b = md.boxes[key]
            quotas = params.find("Quotas")
            if quotas is not None:
                b["apply"] = quotas.findtext("ApplyQuotas") == "Yes"
                if quotas.find("MaxDiskSpace") is not None:
                    b["max_mb"] = int(quotas.findtext("MaxDiskSpace"))
            fwd = params.find("Forwarding")
            if fwd is not None:
                addresses = [a.text for a in fwd.findall("Addresses/Address")]
                if any(a.endswith("@blocked.ru") or "," in a for a in addresses):
                    return reply(1, "0xC1420082", "The Email address specified is invalid")
                b["forward"]["enabled"] = fwd.findtext("Enabled") == "true"
                if fwd.find("RetainMail") is not None:
                    b["forward"]["retain"] = fwd.findtext("RetainMail") == "true"
                if fwd.find("Addresses") is not None:
                    b["forward"]["addresses"] = addresses
                schedule = fwd.find("Schedule")
                if schedule is not None:
                    assert DATE_RX.match(schedule.findtext("Start")) and DATE_RX.match(schedule.findtext("End"))
                    b["forward"]["mask"] = schedule.find("DaysActive").get("mask")
            auto = params.find("AutoResponder")
            if auto is not None:
                b["auto"]["enabled"] = auto.findtext("Enabled") == "Yes"
                for name in ("Start", "End"):
                    if auto.find(name) is not None:
                        if not DATE_RX.match(auto.findtext(name)):
                            return reply(1, "0xC1420090", f"Invalid date in {name}.")
                        b["auto"][name.lower()] = auto.findtext(name)
                if auto.find("Response") is not None:
                    b["auto"]["response"] = auto.findtext("Response")
            return reply()
        return reply(1, "0xC1420002", f"Operation {op} not found.")


@pytest.fixture()
def mdaemon():
    md = MD()
    fake = FakeHttp()
    install(md, fake)
    md.fake = fake
    yield md
    fake.close()


def connect(svc, md, **extra):
    values = {"type": "mdaemon", "name": "MDaemon офис", "base_url": md.fake.url, "username": LOGIN,
              "password": PASSWORD, "imap_host": IMAP_HOST, "allow_changes": True}
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
def srv(services, mdaemon):
    sid = connect(services, mdaemon)
    res = mss.sync(services, sid, user="tester")
    return type("M", (), {"svc": services, "md": mdaemon, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@company.ru", "pe": f"ms:{sid}:petrova@company.ru",
                          "si": f"ms:{sid}:sidorov@company.ru"})


# -- вход, проверка, журнал --------------------------------------------------------------------------
def test_check_reads_version_and_domains_and_logs_off(services, mdaemon):
    sid = connect(services, mdaemon)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["version"] == "MDaemon 25.0.2" and st["seats_total"] is None and st["queue"] is None
    assert any("company.ru, filial.ru" in d for d in st["details"]) and any("1800 с" in d for d in st["details"])
    logon, listing = mdaemon.requests[0], mdaemon.requests[1]
    assert logon[0] == "Logon" and logon[1] == mdmod.DEFAULT_VERSION
    assert listing[0] == "GetDomainList" and listing[1] == "25.0"            # версия — по ответу сервера
    assert listing[3]["x-mdapi-token"] == "T0KEN0001QWERTY"
    assert mdaemon.logoffs == 1 and not mdaemon.tokens
    text = transcripts(services, sid)
    basic = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
    assert "GetDomainList" in text and PASSWORD not in text and basic not in text and "T0KEN0001QWERTY" not in text


def test_wrong_password_is_explained_and_not_logged(services, mdaemon):
    sid = connect(services, mdaemon, password="Wrong~Pass1")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"]
    assert "вход администратора «admin@company.ru» отклонён" in out["error"] and "incorrect" in out["error"]
    assert "Address Restrictions" in out["hint"]
    assert "Wrong~Pass1" not in transcripts(services, sid)


def test_not_xml_and_not_mdaemon_answers(services, mdaemon):
    sid = connect(services, mdaemon)
    mdaemon.garbage = True
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "не XML" in out["error"] and "MdMgmtWS" in out["hint"]
    with pytest.raises(ValidationError):
        connect(services, mdaemon, base_url="https://mail.company.ru:444/?x=1")
    drv = mdmod.MDaemonDriver(connection("mdaemon", {"base_url": "mail.company.ru:444"}), ApiLog())
    assert mdmod.MDaemonDriver.parse_url("mail.company.ru:444") == ("https://mail.company.ru:444", "/MdMgmtWS")
    assert mdmod.MDaemonDriver.parse_url("https://web.company.ru/MDMgmtWS/") == ("https://web.company.ru",
                                                                                 "/MDMgmtWS")
    assert drv.imap_server() == ("mail.company.ru", 993, "ssl")
    out = mss.check(services, connect(services, mdaemon, base_url="http://10.1.2.3:1000"), user="tester")
    assert not out["ok"] and "без шифрования (http) не разрешено" in out["error"]


def test_token_expiry_relogons_once(services, mdaemon):
    sid = connect(services, mdaemon)
    mdaemon.expire_next = 2                        # токен перестаёт приниматься посреди сверки
    res = mss.sync(services, sid, user="tester")
    assert res["accounts"] == 4 and not res["incomplete"]
    assert mdaemon.logons == 2 and "сеанс XML API истёк — вход заново" in transcripts(services, sid)
    mdaemon.expire_all = True
    mdaemon.logons = 0
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "0xC142000A" in out["error"] and mdaemon.logons == 2


# -- сверка --------------------------------------------------------------------------------------
def test_sync_domains_quota_messages_aliases(srv):
    rows = {r["email"]: r for r in msstore.list_accounts(srv.svc.db, srv.sid)}
    assert set(rows) == {"ivanov@company.ru", "petrova@company.ru", "sidorov@company.ru", "info@filial.ru"}
    iv = rows["ivanov@company.ru"]
    assert iv["aliases"] == ["ivan@company.ru", "i.ivanov@company.ru"]           # без подстановочных
    assert iv["quota_kb"] == 10240 * 1024 and iv["messages"] == 1200 and iv["used_bytes"] is None
    assert (iv["display_name"], iv["first_name"], iv["last_name"]) == ("Иван Иванов", "Иван", "Иванов")
    assert iv["login_blocked"] is None and iv["last_login"] == ""
    pe = rows["petrova@company.ru"]
    assert pe["quota_kb"] is None and pe["info"] == {"apply_quotas": False, "max_messages": 5000}
    assert srv.sync["incomplete"] is False and not srv.sync["problems"]
    sections = [[s.tag for s in r[2].find("Get/Users")] for r in srv.md.ops("GetDomainInfo")]
    assert sections == [["Quotas", "Details", "Aliases"]] * 2


def test_sync_with_failing_domain_is_incomplete(services, mdaemon):
    sid = connect(services, mdaemon)
    mss.sync(services, sid, user="tester")
    mdaemon.broken_domain = "filial.ru"
    res = mss.sync(services, sid, user="tester")
    assert res["incomplete"] is True
    assert any("домен filial.ru не прочитан" in p and "Access denied" in p for p in res["problems"])
    assert not msstore.get_account(services.db, sid, "info@filial.ru")["deleted_at"]


def test_refresh_marks_deleted_only_when_really_gone(srv):
    svc, md = srv.svc, srv.md
    md.broken_box = "sidorov@company.ru"            # ошибка чтения — не повод считать учётную запись удалённой
    _res, items = act(svc, "ax_refresh", [srv.si])
    assert items[srv.si]["status"] == "failed" and "0xC1420099" in items[srv.si]["detail"], items
    assert not msstore.get_account(svc.db, srv.sid, "sidorov@company.ru")["deleted_at"]
    md.broken_box = None
    del md.boxes["sidorov@company.ru"]
    _res, items = act(svc, "ax_refresh", [srv.si])
    assert items[srv.si]["status"] == "success" and "больше нет" in items[srv.si]["detail"], items
    assert msstore.get_account(svc.db, srv.sid, "sidorov@company.ru")["deleted_at"]


def test_capabilities_skip_with_reason(srv):
    svc = srv.svc
    for action, params, reason in (
            ("ax_block", {"final_copy": False}, "закрыть и открыть вход — этот сервер так не умеет"),
            ("ax_password", {"mode": "random_hidden"}, "пароли — этот сервер так не умеет"),
            ("ax_alias_add", {"template": "{f}.{last}"}, "псевдонимы — этот сервер так не умеет"),
            ("ax_contact", {"source": "fixed", "company": "ООО"}, "ФИО, отдел, должность — этот сервер так не умеет"),
            ("ax_add_mailboxes", {}, "копирование без паролей сотрудников не поддерживается")):
        pv = base.run(svc, ADMIN, action, [srv.iv], params, preview=True)
        assert pv["counts"]["skip"] == 1 and reason in pv["results"][0]["detail"], (action, pv["results"])
    svc.db.create_account(models.Account(name="Иванов", host=IMAP_HOST, username="ivanov@company.ru", password="pw"))
    pv = base.run(svc, ADMIN, "ax_delete", [srv.iv], {}, preview=True)
    assert "удаление — этот сервер так не умеет" in pv["results"][0]["detail"]


# -- действия ------------------------------------------------------------------------------------
def test_quota_exact_elements(srv):
    svc, md = srv.svc, srv.md
    _res, items = act(svc, "ax_quota", [srv.pe], quota_mb=2048, only_raise=True, threshold=0)
    assert items[srv.pe]["status"] == "success", items
    assert "предел числа писем (5000)" in items[srv.pe]["detail"] and "не проверено" in items[srv.pe]["detail"]
    sent = md.ops("UpdateUser")[-1][2]
    assert [el.tag for el in sent] == ["Domain", "Mailbox", "Quotas"]
    assert sent.findtext("Mailbox") == "petrova" and sent.findtext("Domain") == "company.ru"
    assert [(el.tag, el.text) for el in sent.find("Quotas")] == [("ApplyQuotas", "Yes"), ("MaxDiskSpace", "2048")]
    assert md.boxes["petrova@company.ru"]["apply"] is True and md.boxes["petrova@company.ru"]["max_mb"] == 2048
    assert msstore.get_account(svc.db, srv.sid, "petrova@company.ru")["quota_kb"] == 2048 * 1024


def test_autoreply_on_with_dates_and_off(srv):
    svc, md = srv.svc, srv.md
    text = "Я больше не работаю в компании.\nПишите petrova@company.ru <отдел продаж> & партнёрам"
    _res, items = act(svc, "srv_autoreply", [srv.iv], mode="on", subject="Нет на месте", text=text,
                      start="2026-11-01", end="2026-11-10")
    assert items[srv.iv]["status"] == "success", items
    assert "тему автоответа XML API MDaemon не задаёт" in items[srv.iv]["detail"]
    auto = md.boxes["ivanov@company.ru"]["auto"]
    assert auto == {"enabled": True, "start": "2026-11-01T00:00:00.000Z", "end": "2026-11-10T23:59:59.000Z",
                    "response": text}
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["autoreply"] is True
    # без дат — прежнее расписание не остаётся
    act(svc, "srv_autoreply", [srv.iv], mode="on", subject="", text="Отпуск", start="", end="")
    assert md.boxes["ivanov@company.ru"]["auto"]["start"] == NO and md.boxes["ivanov@company.ru"]["auto"]["end"] == NO
    drv = mss.driver(svc, msstore.get_server(svc.db, srv.sid))
    try:
        assert drv.get_autoreply("ivanov@company.ru") == {"enabled": True, "text": "Отпуск", "start": "", "end": ""}
    finally:
        drv.close()
    _res, items = act(svc, "srv_autoreply", [srv.iv], mode="off")
    assert items[srv.iv]["status"] == "success" and md.boxes["ivanov@company.ru"]["auto"]["enabled"] is False
    sent = md.ops("UpdateUser")[-1][2].find("AutoResponder")
    assert [(el.tag, el.text) for el in sent] == [("Enabled", "No")]
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["autoreply"] is False


def test_forwarding_one_address_element_each(srv):
    svc, md = srv.svc, srv.md
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="on", addresses="petrova@company.ru\nboss@company.ru",
                      keep_copy=False)
    assert items[srv.iv]["status"] == "success", items
    sent = md.ops("UpdateUser")[-1][2].find("Forwarding")
    assert [a.text for a in sent.findall("Addresses/Address")] == ["petrova@company.ru", "boss@company.ru"]
    assert sent.findtext("Enabled") == "true" and sent.findtext("RetainMail") == "false"
    assert sent.find("Schedule/DaysActive").get("mask") == "0x7F"
    fwd = md.boxes["ivanov@company.ru"]["forward"]
    assert fwd["enabled"] is True and fwd["retain"] is False and fwd["addresses"] == ["petrova@company.ru",
                                                                                      "boss@company.ru"]
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["forward_to"] == ["petrova@company.ru",
                                                                                       "boss@company.ru"]
    # ошибка сервера в Status — понятный отказ с текстом и кодом сервера, пересылка не меняется
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="on", addresses="someone@blocked.ru", keep_copy=True)
    assert items[srv.iv]["status"] == "failed", items
    assert "0xC1420082" in items[srv.iv]["detail"] and "The Email address specified is invalid" in \
        items[srv.iv]["detail"]
    assert fwd["addresses"] == ["petrova@company.ru", "boss@company.ru"]
    _res, items = act(svc, "srv_forwarding", [srv.iv], mode="off", addresses="", keep_copy=True)
    assert items[srv.iv]["status"] == "success" and fwd["enabled"] is False
    assert msstore.get_account(svc.db, srv.sid, "ivanov@company.ru")["forward_to"] == []


# -- создание ------------------------------------------------------------------------------------
def test_create_accounts_on_connection(srv, monkeypatch):
    svc, md = srv.svc, srv.md
    params = {"server_id": srv.sid, "text": "new.user@company.ru; Новый; Сотрудник\nivanov@company.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 1024, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@company.ru"]["status"] == "ok"
    assert by["ivanov@company.ru"]["status"] == "skip" and "уже есть" in by["ivanov@company.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert any("смену пароля при первом входе сервер не требует" in w for w in pv["warnings"])
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **kw: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = md.boxes["new.user@company.ru"]
    assert (new["first"], new["last"], len(new["password"])) == ("Новый", "Сотрудник", 20)
    assert new["apply"] is True and new["max_mb"] == 1024
    sent = md.ops("CreateUser")[-1][2]
    assert [el.tag for el in sent] == ["Domain", "Mailbox", "Details"]
    assert [el.tag for el in sent.find("Details")] == ["Password", "FirstName", "LastName"]
    row = msstore.get_account(svc.db, srv.sid, "new.user@company.ru")
    assert row is not None and row["first_name"] == "Новый" and row["quota_kb"] == 1024 * 1024
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 1
    assert new["password"] not in transcripts(svc, srv.sid)


def test_create_existing_account_is_already_exists(srv):
    drv = mss.driver(srv.svc, msstore.get_server(srv.svc.db, srv.sid))
    try:
        with pytest.raises(AlreadyExists):
            drv.create_account("petrova@company.ru", first_name="Анна", last_name="Петрова", password="Zx9-qwerty-Lp")
        assert not srv.md.ops("CreateUser")          # проверено до CreateUser
        with pytest.raises(ServerError, match="домена other.ru на сервере нет"):
            drv.create_account("x@other.ru", first_name="", last_name="", password="Zx9-qwerty-Lp")
    finally:
        drv.close()


def test_helpers():
    block = ET.fromstring("<Aliases><Alias>A@Company.ru</Alias><Item value='b@company.ru'/><Alias>*@company.ru</Alias>"
                          "<Alias>me@company.ru</Alias></Aliases>")
    assert mdmod.aliases_block(block, "me@company.ru") == ["a@company.ru", "b@company.ru"]
    assert mdmod.MDaemonDriver._quotas(1536 * 1024 * 1024 + 1).findtext("MaxDiskSpace") == "1537"
    assert mdmod.autoresponder_block(None) is None
    with pytest.raises(ValidationError):
        mdmod.MDaemonDriver.validate_settings({"base_url": "https://mail.company.ru", "api_version": "двадцать"}, {})


# -- журнал: пароли в XML ---------------------------------------------------------------------------
def test_create_password_with_xml_specials_is_not_logged(mdaemon):
    api = ApiLog()
    api.secret = lambda value: None             # маскирует сам драйвер, а не поиск известного пароля в журнале
    drv = mdmod.MDaemonDriver(connection("mdaemon", {"base_url": mdaemon.fake.url, "username": LOGIN},
                                         {"password": PASSWORD}), api)
    password = "Pa&ss<w>rd1\"x'"
    drv.create_account("new.user@company.ru", first_name="Новый", last_name="Сотрудник", password=password)
    assert mdaemon.boxes["new.user@company.ru"]["password"] == password          # на сервер ушёл как есть
    text = api.transcript()
    for form in (password, "Pa&amp;ss&lt;w&gt;rd1", "Pa&ss<w>rd1"):
        assert form not in text
    assert "CreateUser: запрос (пароль скрыт)" in text and "<Password>••••</Password>" in text
    assert "<FirstName>Новый</FirstName>" in text                               # остальное видно
    assert 'CreateUser: ответ — Status id="0"' in text
    note = next(i for i, c in enumerate(api.calls) if c.method == "NOTE" and "CreateUser: запрос" in c.response)
    create_call = api.calls[note + 1]                                          # сам запрос — без тела
    assert create_call.method == "POST" and create_call.request == create_call.response == "(не показывается)"


def test_passwords_in_replies_are_not_logged(services, mdaemon):
    mdaemon.reply_passwords = True
    sid = connect(services, mdaemon)
    mss.sync(services, sid, user="tester")
    conn = msstore.get_server(services.db, sid)
    assert mss.refresh_account(services, conn, "ivanov@company.ru") is not None
    text = transcripts(services, sid)
    assert "GetUserInfo" in text and "Init-pass-1" not in text and "<Password>••••</Password>" in text


def test_only_reads_are_repeated_after_gateway_error(mdaemon):
    drv = mdmod.MDaemonDriver(connection("mdaemon", {"base_url": mdaemon.fake.url, "username": LOGIN},
                                         {"password": PASSWORD}), ApiLog())
    mdaemon.fail_once = {"GetDomainList": 502}
    assert drv._domains() == ["company.ru", "filial.ru"]                       # чтение — повторено
    assert len(mdaemon.ops("GetDomainList")) == 2
    # изменение после 502 не повторяется: сервер мог его уже выполнить
    mdaemon.fail_once = {"CreateUser": 502}
    with pytest.raises(ServerError, match="502"):
        drv.create_account("new.user@company.ru", first_name="", last_name="", password="Strong-Pass-1")
    assert len(mdaemon.ops("CreateUser")) == 1
