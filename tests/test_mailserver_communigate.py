"""Подключение CommuniGate Pro: учебный сервер CLI (PWD) на TCP 127.0.0.1 и IMAP входом администратора «от имени».

Учебный сервер говорит как CommuniGate: приветствие 200 с меткой APOP, вход APOP или USER/PASS (300), режим INLINE
(данные в строке 201) или многострочные данные после «200 data follow», отказы 5xx с текстом; аргументы команд он
разбирает своим разборщиком формата CommuniGate — так проверяется и кодирование в драйвере. Проверяется весь путь:
проверка подключения (в том числе TLS с самоподписанным сертификатом и отпечатком), сверка, действия раздела,
удаление при полном архиве и передача адреса коллеге, создание, и что пароли не попадают в журнал подключения.
"""
import datetime as _dt
import hashlib
import ipaddress
import re
import socketserver
import ssl
import threading

import pytest

from imap_fake import ImapWorld, raw_message
from mailserver_fake import connection
from mailarchiver.errors import ValidationError
from mailarchiver.imap import client as client_mod
from mailarchiver.mailservers import service as mss
from mailarchiver.mailservers import store as msstore
from mailarchiver.mailservers.base import Cap, NotFound, ServerAuthError, ServerError
from mailarchiver.mailservers.drivers import communigate as cgmod
from mailarchiver.mailservers.drivers.communigate import (CgpFormatError, CliClient, CommuniGateDriver,
                                                          cgp_time_iso, encode, login_blocked, parse, parse_size,
                                                          quote)
from mailarchiver.mailservers.http import ApiLog
from mailarchiver.models import AuthType, JobType
from mailarchiver.queue.jobs import HANDLERS, JobContext
from mailarchiver.serverops import base, create

ADMIN = {"username": "tester", "role": "admin"}
ADMIN_LOGIN = "postmaster@corp.ru"
ADMIN_PW = "Cgp-Adm1n~Secret"
STAMP = "<1.1761480000@mail.corp.ru>"
GB = 1024 ** 3
MB = 1024 ** 2


# ---------------------------------------------------------------------------
#  Учебный сервер: свой (независимый от драйвера) формат данных
# ---------------------------------------------------------------------------
class Raw(str):
    """Токен как есть: время #T…, адрес #I[…]."""


def enc(value):
    if isinstance(value, Raw):
        return str(value)
    if value is None:
        return "#NULL#"
    if isinstance(value, int):
        return f"#{value}"
    if isinstance(value, dict):
        return "{" + "".join(f"{enc(str(k))}={enc(v)};" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "(" + ",".join(enc(v) for v in value) + ")"
    if value and re.fullmatch(r"[A-Za-z0-9._@-]+", value):      # атомы — и с точкой, «@», «-»
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\e") + '"'


class Args:
    """Разбор аргументов команды CLI (строки, атомы, массивы, словари)."""

    def __init__(self, text):
        self.s, self.i = text, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def all(self):
        out = []
        self.ws()
        while self.i < len(self.s):
            out.append(self.value())
            self.ws()
        return out

    def value(self):
        self.ws()
        c = self.s[self.i]
        if c == '"':
            out, self.i = [], self.i + 1
            while self.s[self.i] != '"':
                if self.s[self.i] == "\\":
                    nxt = self.s[self.i + 1]
                    if nxt.isdigit():
                        out.append(chr(int(self.s[self.i + 1:self.i + 4])))
                        self.i += 4
                        continue
                    out.append({"e": "\n", "n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt))
                    self.i += 2
                    continue
                out.append(self.s[self.i])
                self.i += 1
            self.i += 1
            return "".join(out)
        if c in "({":
            close = ")" if c == "(" else "}"
            self.i += 1
            items = [] if c == "(" else {}
            while True:
                self.ws()
                if self.s[self.i] == close:
                    self.i += 1
                    return items
                if c == "(":
                    items.append(self.value())
                    self.ws()
                    if self.s[self.i] == ",":
                        self.i += 1
                else:
                    key = self.value()
                    self.ws()
                    assert self.s[self.i] == "=", self.s
                    self.i += 1
                    items[key] = self.value()
                    self.ws()
                    assert self.s[self.i] == ";", self.s
                    self.i += 1
        start = self.i
        while self.i < len(self.s) and self.s[self.i] not in " ,;=)}":
            self.i += 1
        word = self.s[start:self.i]
        return int(word[1:]) if re.fullmatch(r"#-?\d+", word) else word


class Cgp:
    """Состояние учебного CommuniGate Pro."""

    DEFAULTS = {"AccessModes": "All", "MaxAccountSize": "unlimited", "PWDAllowed": "YES"}

    def __init__(self):
        self.domains = ["corp.ru"]
        self.accounts = {}
        self.add("ivanov@corp.ru", {"RealName": "Иван Иванов", "FirstName": "Иван", "FamilyName": "Иванов",
                                    "ou": "Продажи", "title": "Менеджер", "MaxAccountSize": "2048M"},
                 aliases=["ivan"], info={"LastLogin": Raw("#T20-10-2026_08:00:00"),
                                         "LastAddress": Raw("#I[10.0.0.5]:53122"), "StorageUsed": GB})
        self.add("petrova@corp.ru", {"RealName": "Анна Петрова", "FirstName": "Анна", "FamilyName": "Петрова"},
                 info={"StorageUsed": 10 * MB})
        self.add("sidorov@corp.ru", {"RealName": "Пётр Сидоров", "AccessModes": ["27", "Mail"]})
        self.add(ADMIN_LOGIN, {"RealName": "Postmaster"})
        self.accounts["ivanov@corp.ru"]["subsets"]["RepliedAddresses"] = ["old@other.ru"]

    def add(self, email, settings, *, aliases=(), info=None):
        self.accounts[email] = {"settings": dict(settings), "aliases": list(aliases), "info": dict(info or {}),
                                "rules": [], "subsets": {}, "password": "Initial-Pass1"}
        return self.accounts[email]

    def find(self, name):
        name = name.lower()
        if name in self.accounts:
            return self.accounts[name]
        local, _, domain = name.partition("@")
        for email, acc in self.accounts.items():          # имя псевдонима ведёт в учётную запись
            if email.endswith("@" + domain) and local in acc["aliases"]:
                return acc
        return None

    def taken(self, local, domain, but=None):
        for email, acc in self.accounts.items():
            if acc is not but and email.endswith("@" + domain) and (email.split("@")[0] == local
                                                                     or local in acc["aliases"]):
                return True
        return False

    def effective(self, email):
        return dict(self.DEFAULTS, **self.accounts[email]["settings"])


class CgpFake:
    """Сервер CLI: поток на соединение, режим INLINE (или многострочные данные), TLS по желанию."""

    def __init__(self, *, inline=True, tls=None, stamp=True):
        self.state = Cgp()
        self.inline, self.tls, self.stamp = inline, tls, stamp
        self.lines = []
        self.garble = set()            # команды, перед ответом на которые сервер шлёт непонятную строку
        self.show_passwords = False    # настройки учётной записи — с ключом Password (журнал его скрывает)
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                fake.session(self)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

            def get_request(self):
                sock, addr = super().get_request()
                if fake.tls is not None:
                    sock = fake.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
                return sock, addr

            def handle_error(self, request, client_address):
                pass                                  # обрыв клиентом (проверка отпечатка) — не ошибка теста

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def commands(self, verb):
        return [line for line in self.lines if line.split(" ", 1)[0].upper() == verb]

    def session(self, h):
        if self.tls is not None:
            try:
                h.request.do_handshake()
            except (ssl.SSLError, OSError):
                return
        st = {"user": None, "pending": None, "inline": False}

        def send(text):
            h.wfile.write(text.encode("utf-8") + b"\r\n")
            h.wfile.flush()
        send("200 mail.corp.ru CommuniGate Pro PWD Server 6.3.12 ready" + (f" {STAMP}" if self.stamp else ""))
        while True:
            line = h.rfile.readline()
            if not line:
                return
            text = line.decode("utf-8").rstrip("\r\n")
            assert "\r" not in text and "\n" not in text
            self.lines.append(text)
            verb, _sep, rest = text.partition(" ")
            try:
                out = self.dispatch(st, verb.upper(), rest)
            except Exception as exc:  # noqa: BLE001 — ошибка учебного сервера видна в ответе
                out = f"512 fake error {type(exc).__name__}: {exc}"
            if out is None:
                send("200 CommuniGate Pro PWD connection closed")
                return
            if verb.upper() in self.garble:
                send("<<garbled>>")
            send(out)

    def data(self, st, value):
        if st["inline"]:
            return "201 " + enc(value)
        if isinstance(value, dict):                       # многострочно, как CommuniGate без INLINE
            return "200 data follow\r\n{\r\n" + "".join(f"  {enc(str(k))}={enc(v)};\r\n"
                                                        for k, v in value.items()) + "}"
        return "200 data follow\r\n" + enc(value)

    def dispatch(self, st, verb, rest):
        s = self.state
        if verb == "QUIT":
            return None
        if verb == "USER":
            st["pending"] = rest
            return "300 please send the PASS"
        if verb == "PASS":
            if st["pending"] == ADMIN_LOGIN and rest == ADMIN_PW:
                st["user"] = ADMIN_LOGIN
                return "200 login OK, proceed"
            return "500 incorrect password or account name"
        if verb == "APOP":
            name, digest = rest.split(" ")
            if name == ADMIN_LOGIN and digest == hashlib.md5((STAMP + ADMIN_PW).encode()).hexdigest():
                st["user"] = ADMIN_LOGIN
                return "200 login OK"
            return "500 incorrect password or account name"
        if not st["user"]:
            return "500 please log in first"
        if verb == "INLINE":
            if not self.inline:
                return "500 Unknown command"
            st["inline"] = True
            return "200 OK"
        args = Args(rest).all()
        if verb == "GETVERSION":
            return self.data(st, "6.3.12")
        if verb == "LISTDOMAINS":
            return self.data(st, list(s.domains))
        if verb == "LISTACCOUNTS":
            return self.data(st, {e.split("@")[0]: "macnt" for e in sorted(s.accounts) if e.endswith("@" + args[0])})
        if verb == "CREATEACCOUNT":
            name = args[0].lower()
            local, domain = name.split("@")
            if domain not in s.domains:
                return "512 Unknown domain name"
            if s.taken(local, domain):
                return "501 Account with this name already exists"
            settings = args[1] if len(args) > 1 else {}
            acc = s.add(name, {k: v for k, v in settings.items() if k != "Password"})
            acc["password"] = settings.get("Password", "")
            return "200 OK"
        acc = s.find(args[0]) if args else None
        if acc is None:
            return "512 Unknown account"
        email = next(e for e, a in s.accounts.items() if a is acc)
        domain = email.split("@")[1]
        if verb == "GETACCOUNTSETTINGS":
            return self.data(st, dict(acc["settings"], Password=acc["password"]) if self.show_passwords
                             else acc["settings"])
        if verb == "GETACCOUNTEFFECTIVESETTINGS":
            return self.data(st, dict(s.effective(email), Password=acc["password"]) if self.show_passwords
                             else s.effective(email))
        if verb == "UPDATEACCOUNTSETTINGS":
            for key, value in args[1].items():
                if value == "default":
                    acc["settings"].pop(key, None)
                else:
                    acc["settings"][key] = value
            return "200 OK"
        if verb == "SETACCOUNTPASSWORD":
            assert args[1].upper() in ("PASSWORD", "TO")        # 6.x — PASSWORD, старые клиенты — TO
            acc["password"] = args[2]
            return "200 OK"
        if verb == "GETACCOUNTALIASES":
            return self.data(st, list(acc["aliases"]))
        if verb == "SETACCOUNTALIASES":
            for alias in args[1]:
                if alias not in acc["aliases"] and s.taken(alias, domain, but=acc):
                    return "501 Alias already exists"
            acc["aliases"] = list(args[1])
            return "200 OK"
        if verb == "DELETEACCOUNT":
            del s.accounts[email]
            return "200 OK"
        if verb == "GETACCOUNTINFO":
            return self.data(st, {k: acc["info"].get(k, "") for k in args[1]})
        if verb == "GETACCOUNTMAILRULES":
            return self.data(st, acc["rules"])
        if verb == "UPDATEACCOUNTMAILRULE":
            if args[1] == "DELETE":
                acc["rules"] = [r for r in acc["rules"] if r[1] != args[2]]
            else:
                rule = args[1]
                acc["rules"] = [r for r in acc["rules"] if r[1] != rule[1]] + [rule]
            return "200 OK"
        if verb == "REMOVEACCOUNTSUBSET":
            assert args[1] == "SUBSET"
            if acc["subsets"].pop(args[2], None) is None:
                return "512 Unknown subset"
            return "200 OK"
        return "501 Unknown command"


@pytest.fixture()
def cgp():
    fake = CgpFake()
    yield fake
    fake.close()


def connect(svc, fake, **extra):
    values = {"type": "communigate", "name": "Корп CGP", "host": "127.0.0.1", "port": fake.port,
              "security": "plain", "admin": ADMIN_LOGIN, "admin_password": ADMIN_PW, "imap_host": "mail.corp.ru",
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


def assert_no_secrets(svc, sid, *extra):
    text = transcripts(svc, sid)
    assert text and ADMIN_PW not in text
    assert hashlib.md5((STAMP + ADMIN_PW).encode()).hexdigest() not in text
    for value in extra:
        assert value not in text


@pytest.fixture()
def cg(services, cgp):
    sid = connect(services, cgp)
    res = mss.sync(services, sid, user="tester")
    return type("C", (), {"svc": services, "f": cgp, "s": cgp.state, "sid": sid, "sync": res,
                          "iv": f"ms:{sid}:ivanov@corp.ru", "pe": f"ms:{sid}:petrova@corp.ru"})


# ---------------------------------------------------------------------------
#  Формат данных и защита от подстановки команд
# ---------------------------------------------------------------------------
def test_data_format_encode_parse():
    assert quote("ok") == "ok" and quote("") == '""' and quote("a.b@c.ru") == '"a.b@c.ru"'
    assert quote('Иван "Ваня"\\') == '"Иван \\"Ваня\\"\\\\"'
    assert quote("строка 1\nстрока 2\tконец") == '"строка 1\\eстрока 2\\tконец"'
    for bad in ("a\rb", "x\x00", "esc\x1b[0m", "del\x7f"):
        with pytest.raises(ValidationError, match="управляющий знак"):
            quote(bad)
    assert encode({"RealName": "Иван Иванов", "AccessModes": ["Mail"], "N": 5, "X": None, "B": b"Hi"}) == \
        '{RealName="Иван Иванов";AccessModes=(Mail);N=#5;X=#NULL#;B=[SGk=];}'
    text = ('{a=b; "c d"=("x", #12, #-0x10, #NULL#, [SGVsbG8=], ()); e = "q\\"\\\\\\e\\010\\u\'263A\'" "-tail";'
            ' /* комментарий */ f=#T20-10-2026_08:00:00; g = {h=#I[10.0.0.5]:25;}; }')
    assert parse(text) == {"a": "b", "c d": ["x", 12, -16, None, b"Hello", []], "e": 'q"\\\n\n☺-tail',
                           "f": "#T20-10-2026_08:00:00", "g": {"h": "#I[10.0.0.5]:25"}}
    assert parse(encode({"t": "строка 1\nстрока 2", "k": ["#Vacation", "2"]})) == \
        {"t": "строка 1\nстрока 2", "k": ["#Vacation", "2"]}
    for bad in ("{a=b", '"abc', "(a b)", "{a b;}", "(a,b) лишнее"):
        with pytest.raises(CgpFormatError):
            parse(bad)
    assert parse("(27, Mail, POP)") == ["27", "Mail", "POP"]
    assert login_blocked(["27", "Mail"]) is True and login_blocked("All") is False
    assert login_blocked("None") is True and login_blocked(["Mail", "IMAP"]) is False and login_blocked(None) is None
    assert parse_size("100M") == 100 * MB and parse_size("10240K") == 10 * MB and parse_size("unlimited") is None
    assert parse_size("#1048576") == MB and parse_size(-1) is None and parse_size("2G") == 2 * GB
    assert cgp_time_iso("#T20-10-2026_08:00:00") == "2026-10-20T08:00:00Z" and cgp_time_iso("#TPAST") == ""


def test_no_command_injection(cg):
    drv = mss.driver(cg.svc, msstore.get_server(cg.svc.db, cg.sid))
    try:
        with pytest.raises(ValidationError):
            drv.add_alias("ivanov@corp.ru", "x\r\nDELETEACCOUNT petrova@corp.ru")
        with pytest.raises(ValidationError, match="одной строкой"):
            drv.set_autoreply("ivanov@corp.ru", enabled=True, subject="Тема\r\nBcc: all@corp.ru", text="текст")
        with pytest.raises(ValidationError, match="перевода строки"):
            drv.cli.command("GETVERSION\r\nDELETEACCOUNT petrova@corp.ru")
        with pytest.raises(ValidationError):
            drv.set_password("ivanov@corp.ru", "Pass\x00word-1")
        assert drv.run("GETVERSION", data=True) == "6.3.12"        # соединение живо, лишнего не ушло
    finally:
        drv.close()
    assert not cg.f.commands("DELETEACCOUNT") and "petrova@corp.ru" in cg.s.accounts


# ---------------------------------------------------------------------------
#  Подключение
# ---------------------------------------------------------------------------
def test_check_with_apop_and_secrets_not_logged(services, cgp):
    sid = connect(services, cgp)
    out = mss.check(services, sid, user="tester")
    assert out["ok"], out
    st = out["status"]
    assert st["version"] == "CommuniGate Pro 6.3.12" and "Домены: corp.ru" in st["details"]
    assert "Учётных записей: 4" in st["details"] and "CLI: без шифрования" in st["details"]
    assert not st["problems"]                                  # без шифрования, но на этой же машине
    text = transcripts(services, sid)
    assert f"APOP {ADMIN_LOGIN} ••••" in text and "INLINE" in text
    assert "CLI cgp://127.0.0.1:" in text
    assert cgp.commands("QUIT")                                # сессия закрыта (после записи журнала)
    assert not cgp.commands("USER") and not cgp.commands("PASS")     # пароль по сети не шёл
    assert_no_secrets(services, sid)


def test_wrong_password_is_explained_and_not_logged(services, cgp):
    cgp.stamp = False                                          # без метки APOP — вход USER/PASS
    sid = connect(services, cgp, admin_password="Wrong-Pass~77")
    out = mss.check(services, sid, user="tester")
    assert not out["ok"] and "не принял логин или пароль администратора" in out["error"]
    assert "incorrect password" in out["error"]
    text = transcripts(services, sid)
    assert "Wrong-Pass~77" not in text and ADMIN_PW not in text and "PASS ••••" in text


def test_plain_tcp_refused_for_other_hosts():
    with pytest.raises(ValidationError, match="без шифрования"):
        CommuniGateDriver.validate_settings({"host": "mail.corp.ru", "security": "plain"}, {})
    assert CommuniGateDriver.validate_settings({"host": "mail.corp.ru", "security": "plain",
                                                "allow_plain": True}, {}) == []
    with pytest.raises(ValidationError, match="указан неверно"):
        CommuniGateDriver.validate_settings({"host": "https://mail.corp.ru"}, {})
    drv = CommuniGateDriver(connection("communigate", {"host": "mail.corp.ru", "security": "plain",
                                                       "admin": ADMIN_LOGIN}, {"admin_password": ADMIN_PW}),
                            ApiLog())
    with pytest.raises(ServerError, match="без шифрования не разрешено"):
        drv.test()
    assert not CommuniGateDriver.can(Cap.MUST_CHANGE) and CommuniGateDriver.can(Cap.IMAP_ADMIN)


def _tls_context(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mail.corp.ru")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                         serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "cert.pem"), str(tmp_path / "key.pem"))
    return ctx, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_tls_self_signed_fingerprint_and_pin(services, tmp_path):
    ctx, fingerprint = _tls_context(tmp_path)
    fake = CgpFake(tls=ctx)
    try:
        sid = connect(services, fake, security="tls")
        out = mss.check(services, sid, user="tester")
        assert not out["ok"] and "защищённое соединение" in out["error"]
        assert out["fingerprint"] == fingerprint                 # показать отпечаток для «Доверять»
        assert not fake.commands("USER")
        mss.trust_certificate(services, sid, fingerprint, "tester")
        out = mss.check(services, sid, user="tester")
        assert out["ok"], out
        text = transcripts(services, sid)
        assert f"USER {ADMIN_LOGIN}" in text and "PASS ••••" in text and "CLI: TLS" in out["status"]["details"]
        assert_no_secrets(services, sid)
        mss.trust_certificate(services, sid, "ab" * 32, "tester")
        out = mss.check(services, sid, user="tester")
        assert not out["ok"] and "не тот, которому разрешено доверять" in out["error"]
    finally:
        fake.close()


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def _check_rows(svc, sid):
    rows = {r["email"]: r for r in msstore.list_accounts(svc.db, sid)}
    assert set(rows) == {"ivanov@corp.ru", "petrova@corp.ru", "sidorov@corp.ru", ADMIN_LOGIN}
    iv = rows["ivanov@corp.ru"]
    assert iv["aliases"] == ["ivan@corp.ru"] and iv["login_blocked"] is False
    assert iv["quota_kb"] == 2048 * 1024 and iv["used_bytes"] == GB and iv["messages"] is None
    assert iv["last_login"] == "2026-10-20T08:00:00Z" and iv["department"] == "Продажи" and iv["title"] == "Менеджер"
    assert iv["first_name"] == "Иван" and iv["last_name"] == "Иванов" and iv["display_name"] == "Иван Иванов"
    assert iv["info"]["type"] == "macnt" and iv["info"]["last_address"] == "#I[10.0.0.5]:53122"
    assert iv["info"]["services"] == "все"
    pe = rows["petrova@corp.ru"]
    assert pe["quota_kb"] is None and pe["last_login"] == "" and pe["used_bytes"] == 10 * MB
    assert rows["sidorov@corp.ru"]["login_blocked"] is True             # (27, Mail): служб входа нет
    return rows


def test_sync_normalizes_accounts(cg):
    _check_rows(cg.svc, cg.sid)
    assert cg.sync["incomplete"] is False and not cg.sync["problems"]
    assert_no_secrets(cg.svc, cg.sid)


def test_sync_without_inline_mode(services):
    fake = CgpFake(inline=False)                               # данные — отдельными строками после «200»
    try:
        sid = connect(services, fake)
        res = mss.sync(services, sid, user="tester")
        assert res["accounts"] == 4 and not res["problems"]
        _check_rows(services, sid)
        assert "режим INLINE не включён" in transcripts(services, sid)
    finally:
        fake.close()


# ---------------------------------------------------------------------------
#  Действия раздела
# ---------------------------------------------------------------------------
def test_block_password_quota_contact_unblock(cg):
    svc, s = cg.svc, cg.s
    _res, items = act(svc, "ax_block", [cg.pe], final_copy=False, keep_receiving=True)
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["petrova@corp.ru"]["settings"]["AccessModes"] == ["Mail"]
    assert "письма принимаются" in items[cg.pe]["detail"]
    assert msstore.get_account(svc.db, cg.sid, "petrova@corp.ru")["login_blocked"] is True
    # пароль — случайный; смену пароля при входе CommuniGate через CLI не требует
    _res, items = act(svc, "ax_password", [cg.iv], mode="random_hidden", must_change=True)
    assert items[cg.iv]["status"] == "success", items
    password = s.accounts["ivanov@corp.ru"]["password"]
    assert len(password) == 20 and "не требует" in items[cg.iv]["detail"]
    pv = base.run(svc, ADMIN, "ax_must_change", [cg.iv], {"value": "yes"}, preview=True)
    assert pv["counts"]["skip"] == 1 and "этот сервер так не умеет" in pv["results"][0]["detail"]
    # квота
    _res, items = act(svc, "ax_quota", [cg.pe], quota_mb=4096)
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["petrova@corp.ru"]["settings"]["MaxAccountSize"] == "4096M"
    assert msstore.get_account(svc.db, cg.sid, "petrova@corp.ru")["quota_kb"] == 4096 * 1024
    # отдел (организацию и офис CommuniGate через CLI не принимает — их нет в полях)
    _res, items = act(svc, "ax_contact", [cg.pe], source="fixed", company="ООО Корп", department="ИТ",
                      office="Москва", overwrite=True, name_order="last_first", phone=False)
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["petrova@corp.ru"]["settings"]["ou"] == "ИТ"
    assert "company" not in s.accounts["petrova@corp.ru"]["settings"]
    _res, items = act(svc, "ax_unblock", [cg.pe])
    assert items[cg.pe]["status"] == "success", items
    assert "AccessModes" not in s.accounts["petrova@corp.ru"]["settings"]     # снова как у домена
    assert msstore.get_account(svc.db, cg.sid, "petrova@corp.ru")["login_blocked"] is False
    text = transcripts(svc, cg.sid)
    assert 'SETACCOUNTPASSWORD "ivanov@corp.ru" PASSWORD ••••' in text
    assert_no_secrets(svc, cg.sid, password)


def test_block_without_receiving_and_custom_services(cg):
    s = cg.s
    s.accounts["petrova@corp.ru"]["settings"]["AccessModes"] = ["Mail", "IMAP", "WebMail"]
    _res, items = act(cg.svc, "ax_block", [cg.pe], final_copy=False, keep_receiving=False)
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["petrova@corp.ru"]["settings"]["AccessModes"] == "None"
    assert "приём писем остановлен" in items[cg.pe]["detail"] and "imap, mail, webmail" in items[cg.pe]["detail"]


def test_aliases_add_and_remove(cg):
    svc, s = cg.svc, cg.s
    _res, items = act(svc, "ax_alias_add", [cg.pe], template="{f}.{last}")
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["petrova@corp.ru"]["aliases"] == ["a.petrova"]
    assert msstore.get_account(svc.db, cg.sid, "petrova@corp.ru")["aliases"] == ["a.petrova@corp.ru"]
    _res, items = act(svc, "ax_alias_remove", [cg.iv], which="list", aliases="ivan")
    assert items[cg.iv]["status"] == "success", items
    assert s.accounts["ivanov@corp.ru"]["aliases"] == []
    drv = mss.driver(svc, msstore.get_server(svc.db, cg.sid))
    try:
        with pytest.raises(ValidationError, match="только в домене"):
            drv.add_alias("ivanov@corp.ru", "ivan@other.ru")
        with pytest.raises(NotFound):
            drv.remove_alias("ivanov@corp.ru", "nope@corp.ru")
        assert drv.get_account("a.petrova@corp.ru") is None             # псевдоним — не учётная запись
        assert drv.get_account("nobody@corp.ru") is None
    finally:
        drv.close()


def test_autoreply_and_forwarding_rules(cg):
    svc, s = cg.svc, cg.s
    _res, items = act(svc, "srv_autoreply", [cg.iv], mode="on", subject="Нет на месте",
                      text="Я в отпуске.\nПишите petrova@corp.ru", start="2026-11-01", end="2026-11-10")
    assert items[cg.iv]["status"] == "success", items
    rule = next(r for r in s.accounts["ivanov@corp.ru"]["rules"] if r[1] == "#Vacation")
    assert rule[0] == "2"
    assert ["Human Generated", "---"] in rule[2] and ["From", "not in", "#RepliedAddresses"] in rule[2]
    assert ["Current Date", "greater than", "01 Nov 2026 00:00:00"] in rule[2]
    assert ["Current Date", "less than", "10 Nov 2026 23:59:59"] in rule[2]
    assert rule[3][0] == ["Reply with", "+Subject: Нет на месте\nIn-Reply-To: ^I\n\nЯ в отпуске.\n"
                                        "Пишите petrova@corp.ru"]
    assert rule[3][1] == ["Remember 'From' in", "RepliedAddresses"]
    assert "RepliedAddresses" not in s.accounts["ivanov@corp.ru"]["subsets"]     # новый ответ — всем
    assert msstore.get_account(svc.db, cg.sid, "ivanov@corp.ru")["autoreply"] is True
    drv = mss.driver(svc, msstore.get_server(svc.db, cg.sid))
    try:
        assert drv.get_autoreply("ivanov@corp.ru") == {"enabled": True, "subject": "Нет на месте",
                                                       "text": "Я в отпуске.\nПишите petrova@corp.ru",
                                                       "start": "2026-11-01", "end": "2026-11-10"}
    finally:
        drv.close()
    # повторное включение: списка «уже ответили» нет — это не ошибка
    _res, items = act(svc, "srv_autoreply", [cg.iv], mode="on", subject="", text="Отпуск", start="", end="")
    assert items[cg.iv]["status"] == "success", items
    _res, items = act(svc, "srv_autoreply", [cg.iv], mode="off")
    assert items[cg.iv]["status"] == "success" and not s.accounts["ivanov@corp.ru"]["rules"]
    assert msstore.get_account(svc.db, cg.sid, "ivanov@corp.ru")["autoreply"] is False
    # пересылка без копии — правило «#Redirect» с Mirror to и Discard
    _res, items = act(svc, "srv_forwarding", [cg.iv], mode="on", addresses="petrova@corp.ru, boss@other.ru",
                      keep_copy=False)
    assert items[cg.iv]["status"] == "success", items
    assert s.accounts["ivanov@corp.ru"]["rules"] == [["1", "#Redirect", [],
                                                      [["Mirror to", "petrova@corp.ru,boss@other.ru"], ["Discard"]]]]
    assert msstore.get_account(svc.db, cg.sid, "ivanov@corp.ru")["forward_to"] == ["petrova@corp.ru",
                                                                                   "boss@other.ru"]
    drv = mss.driver(svc, msstore.get_server(svc.db, cg.sid))
    try:
        assert drv.get_forwarding("ivanov@corp.ru") == {"addresses": ["petrova@corp.ru", "boss@other.ru"],
                                                        "keep_copy": False}
    finally:
        drv.close()
    _res, items = act(svc, "srv_forwarding", [cg.iv], mode="off", addresses="", keep_copy=True)
    assert not s.accounts["ivanov@corp.ru"]["rules"]
    assert msstore.get_account(svc.db, cg.sid, "ivanov@corp.ru")["forward_to"] == []


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


def test_delete_with_full_archive_then_transfer_address(cg, monkeypatch):
    svc, s = cg.svc, cg.s
    world = imap_world(monkeypatch)
    res = base.run(svc, ADMIN, "ax_add_mailboxes", [cg.pe], {}, preview=False)
    assert res["counts"]["ok"] == 1, res
    acc = next(a for a in svc.db.list_accounts() if a.username == "petrova@corp.ru")
    assert acc.auth_type == AuthType.SERVER and acc.host == "mail.corp.ru" and acc.password == ""
    pv = base.run(svc, ADMIN, "ax_delete", [cg.pe], {}, preview=True)
    assert pv["counts"]["skip"] == 1 and "сначала «Заблокировать вход»" in pv["results"][0]["detail"]
    _res, items = act(svc, "ax_block", [cg.pe], final_copy=True)
    assert items[cg.pe]["status"] == "success" and "последняя копия сделана" in items[cg.pe]["detail"], items
    assert world.last_login == ("plain_authzid", ADMIN_LOGIN, "petrova@corp.ru")
    _res, items = act(svc, "ax_delete", [cg.pe])
    assert items[cg.pe]["status"] == "success", items
    assert "petrova@corp.ru" not in s.accounts and "вместе с ящиком" in items[cg.pe]["detail"]
    acc = svc.db.get_account(acc.id)
    assert acc.enabled is False and acc.hold_until == "9999-12-31"
    assert msstore.get_account(svc.db, cg.sid, "petrova@corp.ru")["deleted_at"]
    # адрес удалённой — коллеге (псевдоним в том же домене)
    _res, items = act(svc, "ax_forward", [cg.pe], to="ivanov@corp.ru", with_aliases=True)
    assert items[cg.pe]["status"] == "success", items
    assert s.accounts["ivanov@corp.ru"]["aliases"] == ["ivan", "petrova"]
    assert "petrova@corp.ru" in msstore.get_account(svc.db, cg.sid, "ivanov@corp.ru")["aliases"]
    pv = base.run(svc, ADMIN, "ax_forward", [cg.pe], {"to": "ivanov@corp.ru"}, preview=True)
    assert "уже переданы" in pv["results"][0]["detail"]
    assert_no_secrets(svc, cg.sid)


def test_imap_auth_goes_only_to_connection_host(cg):
    drv = mss.driver(cg.svc, msstore.get_server(cg.svc.db, cg.sid))
    auth = drv.imap_auth("Petrova@corp.ru")
    assert (auth.mech, auth.user, auth.secret, auth.authzid, auth.host) == \
        ("plain_authzid", ADMIN_LOGIN, ADMIN_PW, "petrova@corp.ru", "mail.corp.ru")
    assert drv._cli is None                                    # для входа в ящик CLI не нужен


def test_create_accounts_and_already_exists(cg, monkeypatch):
    svc, s = cg.svc, cg.s
    params = {"server_id": cg.sid, "text": "new.user@corp.ru; Новый; Сотрудник\nivanov@corp.ru\nx@unknown.ru",
              "password_mode": "random_file", "must_change": True, "quota_mb": 1024, "add_to_archive": True}
    pv = create.run(svc, ADMIN, params, preview=True)
    by = {i["email"]: i for i in pv["items"]}
    assert by["new.user@corp.ru"]["status"] == "ok"
    assert by["ivanov@corp.ru"]["status"] == "skip" and "уже есть" in by["ivanov@corp.ru"]["text"]
    assert "домена unknown.ru нет" in by["x@unknown.ru"]["text"]
    assert any("смену пароля при первом входе сервер не требует" in w for w in pv["warnings"])
    queued = []
    monkeypatch.setattr(svc.queue, "enqueue", lambda *a, **k: queued.append(a) or 1)
    res = create.run(svc, ADMIN, params, preview=False)
    s.add("new.user@corp.ru", {"RealName": "Кто-то Успел"})        # завели вручную, пока задание ждало
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["skipped"] == 1 and "уже есть" in out["items"][0]["detail"], out
    from mailarchiver.serverops import secrets
    assert secrets.count(svc.db, res["op_id"]) == 0
    del s.accounts["new.user@corp.ru"]
    out = run_job(svc, JobType.SERVER_ACTION, queued[0][2])
    assert out["ok"] == 1, out
    new = s.accounts["new.user@corp.ru"]
    assert new["settings"] == {"RealName": "Новый Сотрудник", "FirstName": "Новый", "FamilyName": "Сотрудник",
                               "MaxAccountSize": "1024M"}
    assert len(new["password"]) == 20
    row = msstore.get_account(svc.db, cg.sid, "new.user@corp.ru")
    assert row is not None and row["first_name"] == "Новый" and row["quota_kb"] == 1024 * 1024
    assert any(a.username == "new.user@corp.ru" and a.auth_type == AuthType.SERVER for a in svc.db.list_accounts())
    assert secrets.count(svc.db, res["op_id"]) == 1
    text = transcripts(svc, cg.sid)
    assert "Password=••••" in text
    assert_no_secrets(svc, cg.sid, new["password"])


def test_cli_client_reads_multiline_data_directly():
    fake = CgpFake(inline=False)
    try:
        api = ApiLog()
        cli = CliClient("127.0.0.1", fake.port, api=api, use_tls=False)
        cli.open(ADMIN_LOGIN, ADMIN_PW)
        assert cli.command('GETACCOUNTEFFECTIVESETTINGS "ivanov@corp.ru"', data=True)["MaxAccountSize"] == "2048M"
        with pytest.raises(NotFound, match="Unknown account"):
            cli.command('DELETEACCOUNT "nobody@corp.ru"')
        cli.close()
        assert cli.sock is None and fake.commands("QUIT")
        assert all(c.method == "CLI" for c in api.calls if c.method != "NOTE")
    finally:
        fake.close()


# ---------------------------------------------------------------------------
#  Соединение после непонятного ответа, вход без шифрования, пароли в ответах
# ---------------------------------------------------------------------------
def _driver(fake):
    return CommuniGateDriver(connection("communigate", {"host": "127.0.0.1", "port": fake.port, "security": "plain",
                                                        "admin": ADMIN_LOGIN}, {"admin_password": ADMIN_PW}),
                             ApiLog([ADMIN_PW]))


def test_garbled_reply_drops_connection_and_next_command_relogins(cgp):
    drv = _driver(cgp)
    cgp.garble = {"GETACCOUNTSETTINGS"}
    with pytest.raises(ServerError, match="непонятный ответ"):
        drv.run("GETACCOUNTSETTINGS", quote("ivanov@corp.ru"), data=True)
    assert drv._cli.sock is None and not drv._cli.ready                  # хвост ответа не читаем как чужой
    cgp.garble = set()
    # следующая команда — в новом соединении (вход заново) и получает свой ответ, а не предыдущий
    assert drv.run("GETACCOUNTSETTINGS", quote("petrova@corp.ru"), data=True)["RealName"] == "Анна Петрова"
    with pytest.raises(NotFound, match="Unknown account"):                 # отказ — отказ, не «успех»
        drv.run("DELETEACCOUNT", quote("nobody@corp.ru"))
    assert len(cgp.commands("APOP")) == 2 and len(cgp.commands("INLINE")) == 2
    drv.close()


def test_unparsed_data_drops_connection(cgp, monkeypatch):
    drv = _driver(cgp)
    assert drv.run("GETVERSION", data=True) == "6.3.12"
    monkeypatch.setattr(cgmod, "parse", lambda text: (_ for _ in ()).throw(cgmod.CgpFormatError("сломано")))
    with pytest.raises(ServerError, match="не разобран"):
        drv.run("GETVERSION", data=True)
    assert drv._cli.sock is None and not drv._cli.ready
    drv.close()


def test_plain_cli_to_other_host_needs_apop(cgp, monkeypatch):
    monkeypatch.setattr(cgmod, "_is_loopback", lambda host: False)     # «другая машина» без шифрования
    cgp.stamp = False
    api = ApiLog([ADMIN_PW])
    cli = CliClient("127.0.0.1", cgp.port, api=api, use_tls=False, allow_plain=True)
    with pytest.raises(ServerAuthError, match="метку APOP"):
        cli.open(ADMIN_LOGIN, ADMIN_PW)
    assert not cgp.commands("USER") and not cgp.commands("PASS")          # пароль открытым текстом не ушёл
    assert cli.sock is None and ADMIN_PW not in api.transcript()
    cgp.stamp = True                                                       # с меткой — APOP, пароль по сети не идёт
    cli.open(ADMIN_LOGIN, ADMIN_PW)
    assert cli.ready and len(cgp.commands("APOP")) == 1 and not cgp.commands("PASS")
    cli.close()


@pytest.mark.parametrize("inline", [True, False])
def test_passwords_in_settings_replies_are_not_logged(services, inline):
    fake = CgpFake(inline=inline)
    fake.show_passwords = True
    try:
        sid = connect(services, fake)
        res = mss.sync(services, sid, user="tester")
        assert res["accounts"] == 4 and not res["problems"]
        _res, items = act(services, "ax_block", [f"ms:{sid}:petrova@corp.ru"], final_copy=False)
        assert items[f"ms:{sid}:petrova@corp.ru"]["status"] == "success", items
        text = transcripts(services, sid)
        assert "GETACCOUNTEFFECTIVESETTINGS" in text and "GETACCOUNTSETTINGS" in text
        assert "Initial-Pass1" not in text and "Password=••••" in text
    finally:
        fake.close()


def test_delete_waits_long(cg, monkeypatch):
    seen = []
    real = CliClient.command

    def spy(self, line, **kw):
        seen.append((line.split(" ", 1)[0], kw.get("timeout")))
        return real(self, line, **kw)
    monkeypatch.setattr(CliClient, "command", spy)
    conn = msstore.get_server(cg.svc.db, cg.sid)
    with mss.session(cg.svc, conn, user="tester", kind="write") as drv:
        drv.delete_account("petrova@corp.ru")
    assert "petrova@corp.ru" not in cg.s.accounts
    assert ("DELETEACCOUNT", cgmod.DELETE_TIMEOUT_S) in seen
