"""
Учебные серверы для тестов работы с Axigen: CLI (порт 7000) и FTP Backup.

Настоящего Axigen в среде разработки нет, а формат его ответов описан в
документации не полностью. Поэтому учебный CLI умеет отвечать в нескольких
видах (``Style``): таблица с чертой и без, итог до данных, эхо команд, telnet-
согласование, ответ кусками. Тесты прогоняют клиент на всех видах — разбор
должен быть терпимым, а не подогнанным под один.

Учебный FTP Backup отдаёт дерево из словаря: каталоги — словари, файлы —
байты (или ``(байты, время)``). Имена — в UTF-7 или UTF-8 (как настройка
службы «Folder name encoding»), список — MLSD, LIST (unix или DOS) или
только NLST, письма — отдельными файлами или пакетом ``messages.tar``.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import io
import os
import socket
import socketserver
import ssl
import tarfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
#  Сертификат для TLS
# ---------------------------------------------------------------------------
def make_cert(directory: str, cn: str = "axigen.test") -> Tuple[str, str, str]:
    """Самоподписанный сертификат: (cert.pem, key.pem, отпечаток SHA-256 hex)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(directory, f"{cn}.crt")
    key_path = os.path.join(directory, f"{cn}.key")
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_path, key_path, fp


def server_context(cert: Tuple[str, str, str]) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert[0], cert[1])
    return ctx


# ---------------------------------------------------------------------------
#  Данные «сервера»
# ---------------------------------------------------------------------------
@dataclass
class FakeAccount:
    password: str = "Secret123"
    services: List[str] = field(default_factory=lambda: ["localDelivery", "remoteDelivery", "pop3", "imap",
                                                         "webmail"])
    quota_kb: int = 1048576
    first_name: str = ""
    last_name: str = ""
    aliases: List[str] = field(default_factory=list)
    folders: Dict[str, Tuple[int, int]] = field(default_factory=lambda: {"INBOX": (3, 30720), "Sent": (1, 2048)})
    last_login: str = "2026-09-30 10:15:00"
    purges: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class FakeDomain:
    accounts: Dict[str, FakeAccount] = field(default_factory=dict)
    purge_folders: List[str] = field(default_factory=lambda: ["Trash", "Spam"])


@dataclass
class Style:
    """Вид ответов CLI."""
    separator: bool = True          # таблица LIST: строка заголовков и черта
    title: bool = True              # подпись «The list of …:» перед таблицей
    status_first: bool = False      # строка итога до данных
    echo: bool = False              # сервер повторяет команду
    iac: bool = False               # telnet-согласование в начале
    chunked: bool = False           # ответ мелкими кусками с паузами
    newline: str = "\r\n"
    show_sep: str = " = "           # «имя = значение» или «имя: значение»
    quote_values: bool = True       # значения в кавычках
    list_commas: bool = False       # services = (a, b, c)
    folder_columns: bool = True     # LIST Folders: с колонками messages/size


class World:
    """Состояние учебного сервера Axigen (общее для всех сессий)."""

    def __init__(self) -> None:
        self.admin_user = "admin"
        self.admin_password = "Adm1nPass"
        self.version = "10.6.30|Linux|x86_64"
        self.license = {"license type": "Business", "expiration date": "2027-01-31", "max accounts": "250",
                        "max domains": "5"}
        self.domains: Dict[str, FakeDomain] = {
            "company.ru": FakeDomain(accounts={
                "ivanov": FakeAccount(first_name="Иван", last_name="Иванов", aliases=["i.ivanov"]),
                "petrov": FakeAccount(first_name="Пётр", last_name="Петров", quota_kb=0),
                "postmaster": FakeAccount(),
            }),
        }
        self.queue: List[Dict[str, str]] = [
            {"id": "0x1a", "from": "a@company.ru", "to": "x@remote.org", "status": "deferred"},
        ]
        self.style = Style()
        #: команды после входа: (контекст, команда как пришла)
        self.commands: List[Tuple[str, str]] = []
        #: команда (начало, без учёта регистра) → текст ошибки
        self.fail: Dict[str, str] = {}
        self.sessions = 0
        self.lock = threading.RLock()
        #: контекст, в котором надо отказать (проверка возврата в корень)
        self.fail_in_context: Dict[str, str] = {}
        #: команды (начало, без учёта регистра), на которые сервер не присылает итога «+OK»/«-ERR»
        self.no_status: List[str] = []


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------
def _tokens(line: str) -> List[str]:
    out, cur, quoted, had = [], [], False, False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            had = True
            continue
        if ch.isspace() and not quoted:
            if cur or had:
                out.append("".join(cur))
            cur, had = [], False
            continue
        cur.append(ch)
    if cur or had:
        out.append("".join(cur))
    return out


def _list_value(items: List[str], style: Style) -> str:
    def q(x):
        return f'"{x}"' if (" " in x) else x
    return "(" + (", " if style.list_commas else " ").join(q(i) for i in items) + ")"


class _CliHandler(socketserver.BaseRequestHandler):
    world: World = None   # type: ignore[assignment]
    tls: Optional[ssl.SSLContext] = None

    def setup(self) -> None:
        if self.tls is not None:
            self.request = self.tls.wrap_socket(self.request, server_side=True)
        self.request.settimeout(10)
        self.stack: List[Tuple[str, Dict]] = []
        self.state = "login"
        self.buf = b""
        with self.world.lock:
            self.world.sessions += 1

    # -- ввод/вывод ---------------------------------------------------------
    def send(self, text: str) -> None:
        data = text.replace("\n", self.world.style.newline).encode("utf-8")
        if self.world.style.chunked:
            for i in range(0, len(data), 7):
                self.request.sendall(data[i:i + 7])
                time.sleep(0.002)
        else:
            self.request.sendall(data)

    def readline(self) -> Optional[str]:
        while b"\n" not in self.buf:
            try:
                chunk = self.request.recv(4096)
            except (OSError, ssl.SSLError):
                return None
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.rstrip(b"\r").decode("utf-8", "replace")

    @property
    def prompt(self) -> str:
        if self.state in ("login", "password"):
            return self.state
        if not self.stack:
            return "#"
        return "-".join(kind.split(":")[0] for kind, _ in self.stack) + "#"

    def reply(self, lines: List[str], ok: bool = True, status: str = "command successful") -> None:
        st = self.world.style
        status_line = ("+OK: " if ok else "-ERR: ") + status
        body = list(lines)
        out = ([status_line] + body) if st.status_first else (body + [status_line])
        if getattr(self, "_silent", False):
            out = body or [""]
        self.send("\n".join(out) + "\n" + f"<{self.prompt}> ")

    def handle(self) -> None:
        st = self.world.style
        if st.iac:
            self.request.sendall(b"\xff\xfb\x01\xff\xfb\x03\xff\xfd\x18")
        self.send("Welcome to AXIGEN's Command Line Interface\nYou must login first. For a list of available "
                  "commands, type HELP\n<login> ")
        while True:
            line = self.readline()
            if line is None:
                return
            if st.echo:
                self.send(line + "\n")
            if self.state == "password":
                self.state = "#" if line == self.world.admin_password and self._user == self.world.admin_user \
                    else "login"
                if self.state == "#":
                    self.reply([], True, "Authentication successful")
                else:
                    self.reply([], False, "Authentication failed")
                continue
            cmd = line.strip()
            if not cmd:
                self.send(f"<{self.prompt}> ")
                continue
            if cmd.upper() in ("QUIT", "EXIT"):
                self.send("+OK: have a nice day\n")
                return
            if self.state == "login":
                self.login_cmd(cmd)
                continue
            with self.world.lock:
                self.world.commands.append((self.prompt, cmd))
                failure = next((text for prefix, text in self.world.fail.items()
                                if cmd.lower().startswith(prefix.lower())), None)
                ctx_fail = self.world.fail_in_context.get(self.prompt)
            if failure is not None or (ctx_fail is not None and not cmd.upper().startswith(("BACK", "DONE"))):
                self.reply([], False, failure or ctx_fail)
                continue
            self._silent = any(cmd.lower().startswith(prefix.lower()) for prefix in self.world.no_status)
            try:
                with self.world.lock:
                    self.command(cmd)
            except _Err as exc:
                self.reply([], False, str(exc))
            finally:
                self._silent = False

    def login_cmd(self, cmd: str) -> None:
        words = cmd.split()
        verb = words[0].upper()
        if verb == "USER" and len(words) == 2:
            self._user = words[1]
            self.state = "password"
            self.send(f"<{self.prompt}> ")
        elif verb == "GET" and len(words) == 2 and words[1].upper() == "VERSION":
            self.reply([self.world.version])
        elif verb == "HELP":
            self.reply(["USER <user>", "GET VERSION"])
        else:
            self.reply([], False, "You must login first")

    # -- разбор команд -----------------------------------------------------------
    def command(self, cmd: str) -> None:
        t = _tokens(cmd)
        verb = t[0].upper()
        kind = self.stack[-1][0] if self.stack else "root"
        w = self.world
        if verb == "BACK":
            if self.stack:
                self.stack.pop()
            return self.reply([])
        if verb == "DONE":
            if not self.stack or not self.stack[-1][0].startswith("account-"):
                if self.stack and self.stack[-1][0] in ("adminLimits",):
                    self.stack.pop()
                    return self.reply([])
                raise _Err("DONE is not available in this context")
            sub, data = self.stack.pop()
            self.stack[-1][1].setdefault("pending", {}).update(data.get("pending", {}))
            return self.reply([])
        if kind == "root":
            return self.root(t, verb)
        if kind == "domain":
            return self.domain(t, verb)
        if kind in ("account", "account:create"):
            return self.account(t, verb)
        if kind.startswith("account-"):
            return self.account_sub(t, verb)
        if kind == "adminLimits":
            if verb == "SHOW":
                dom = w.domains[self.stack[0][1]["name"]]
                return self.reply(self.show_lines({"purgeFolders": _list_value(dom.purge_folders, w.style),
                                                   "maxAccounts": "0"}, raw=("purgeFolders",)))
            raise _Err("unknown command")
        if kind == "queue":
            if verb == "LIST":
                return self.reply(self.table(["id", "from", "to", "status"],
                                             [[q["id"], q["from"], q["to"], q["status"]] for q in w.queue],
                                             "The list of queue entries:"))
            raise _Err("unknown command")
        raise _Err("unknown context")

    def table(self, header: List[str], rows: List[List[str]], title: str) -> List[str]:
        st = self.world.style
        widths = [max([len(h)] + [len(str(r[i])) for r in rows]) + 3 for i, h in enumerate(header)]
        out = [title] if st.title else []
        if st.separator:
            out.append("".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip())
            out.append("-" * sum(widths))
            for r in rows:
                out.append("".join(str(v).ljust(widths[i]) for i, v in enumerate(r)).rstrip())
        else:
            for r in rows:
                out.append(str(r[0]))
        return out

    def show_lines(self, values: Dict[str, str], raw: Tuple[str, ...] = ()) -> List[str]:
        st = self.world.style
        out = []
        for k, v in values.items():
            shown = v if (k in raw or not st.quote_values) else f'"{v}"'
            out.append(f"{k}{st.show_sep}{shown}")
        return out

    def root(self, t: List[str], verb: str) -> None:
        w = self.world
        up = [x.upper() for x in t]
        if verb == "LIST" and len(t) >= 2 and up[1] == "DOMAINS":
            rows = [[name, "200000 MB", f"{len(d.accounts)} MB"] for name, d in w.domains.items()]
            return self.reply(self.table(["name", "total size", "used size"], rows, "The list of domains:"))
        if verb == "UPDATE" and len(t) >= 3 and up[1] == "DOMAIN":
            name = t[3] if len(t) >= 4 and up[2] == "NAME" else t[2]
            if name.lower() not in w.domains:
                raise _Err(f"Domain {name} not found")
            self.stack.append(("domain", {"name": name.lower()}))
            return self.reply([])
        if verb == "SHOW" and len(t) >= 2 and up[1] == "LICENSEINFO":
            return self.reply(self.show_lines(w.license))
        if verb == "ENTER" and len(t) == 2 and up[1] == "QUEUE":
            self.stack.append(("queue", {}))
            return self.reply([])
        if verb == "GET" and up[1:] == ["VERSION"]:
            return self.reply([w.version])
        raise _Err("unknown command")

    def _dom(self) -> FakeDomain:
        return self.world.domains[self.stack[0][1]["name"]]

    def domain(self, t: List[str], verb: str) -> None:
        up = [x.upper() for x in t]
        dom = self._dom()
        if verb == "LIST" and len(t) >= 2 and up[1] == "ACCOUNTS":
            title = f"The list of accounts for domain {self.stack[0][1]['name']}:"
            return self.reply(self.table(["name"], [[n] for n in sorted(dom.accounts)], title))
        if verb in ("UPDATE", "ADD", "REMOVE") and len(t) >= 3 and up[1] == "ACCOUNT":
            rest = t[2:]
            if rest and rest[0].lower() == "name":
                rest = rest[1:]
            name = rest[0] if rest else ""
            if "@" in name:
                name = name.split("@", 1)[0]
            if verb == "UPDATE":
                if name not in dom.accounts:
                    raise _Err(f"Account {name} not found")
                self.stack.append(("account", {"name": name, "pending": {}}))
                return self.reply([])
            if verb == "REMOVE":
                if name not in dom.accounts:
                    raise _Err(f"Account {name} not found")
                del dom.accounts[name]
                return self.reply([])
            # ADD Account name X password P
            if name in dom.accounts:
                raise _Err("Account already exists")
            if len(rest) < 3 or rest[1].lower() != "password":
                raise _Err("password is required")
            self.stack.append(("account:create", {"name": name, "pending": {"password": rest[2]}}))
            return self.reply([])
        if verb == "CONFIG" and len(t) == 2 and up[1] == "ADMINLIMITS":
            self.stack.append(("adminLimits", {}))
            return self.reply([])
        if verb == "COMMIT":
            self.stack.pop()
            return self.reply([])
        if verb == "SHOW":
            return self.reply(self.show_lines({"name": self.stack[0][1]["name"]}))
        raise _Err("unknown command")

    def _acc_name(self) -> str:
        return self.stack[1][1]["name"]

    def account(self, t: List[str], verb: str) -> None:
        w = self.world
        up = [x.upper() for x in t]
        dom = self._dom()
        creating = self.stack[-1][0] == "account:create"
        acc = None if creating else dom.accounts.get(self._acc_name())
        pending = self.stack[-1][1].setdefault("pending", {})
        if verb == "CONFIG" and len(t) == 2:
            sub = t[1].lower()
            names = {"limits": "account-limits", "quotas": "account-quotas", "contactinfo": "account-contactinfo"}
            if sub not in names:
                raise _Err("unknown context")
            self.stack.append((names[sub], {"pending": {}}))
            return self.reply([])
        if verb == "SET" and len(t) >= 3 and t[1].lower() == "password":
            pending["password"] = t[2]
            return self.reply([])
        if verb == "COMMIT":
            name = self._acc_name()
            if creating:
                acc = FakeAccount(password=pending.get("password", ""), first_name="", last_name="", aliases=[],
                                  folders={"INBOX": (0, 0)}, last_login="")
                dom.accounts[name] = acc
            for key, value in pending.items():
                setattr(acc, key, value)
            self.stack.pop()
            return self.reply([])
        if creating:
            raise _Err("not available while creating")
        if verb == "LIST" and len(t) == 2 and up[1] == "FOLDERS":
            if w.style.folder_columns:
                rows = [[n, str(c), f"{s // 1024} KB"] for n, (c, s) in acc.folders.items()]
                return self.reply(self.table(["name", "messages", "size"], rows, "The list of folders:"))
            return self.reply(self.table(["name"], [[n] for n in acc.folders], "The list of folders:"))
        if verb == "LIST" and len(t) == 2 and up[1] == "ALIASES":
            return self.reply(self.table(["alias"], [[a] for a in acc.aliases], "The list of aliases:"))
        if verb == "SHOW" and len(t) == 2 and up[1] == "REGISTRYINFORMATION":
            values = {"creation date": "2024-01-10 09:00:00"}
            if acc.last_login:
                values["last login date"] = acc.last_login
            return self.reply(self.show_lines(values))
        if verb == "SHOW" and len(t) == 1:
            return self.reply(self.show_lines({"name": self._acc_name(), "accountType": "basic"}))
        if verb == "PURGE" and len(up) >= 4 and up[1] == "ACCOUNT" and up[2] == "FOLDERS":
            if "BEFORE" not in up:
                raise _Err("test server: purge without condition is refused")
            i = up.index("BEFORE")
            folders = " ".join(t[3:i]).split(",")
            for folder in folders:
                folder = folder.strip()
                if folder not in acc.folders:
                    raise _Err(f"Folder {folder} not found")
                if folder.lower() not in [f.lower() for f in dom.purge_folders]:
                    raise _Err(f"Folder {folder} is not purgeable")
                acc.purges.append((folder, t[i + 1]))
            return self.reply([])
        raise _Err("unknown command")

    def account_sub(self, t: List[str], verb: str) -> None:
        w = self.world
        sub = self.stack[-1][0]
        creating = self.stack[1][0] == "account:create"
        acc = None if creating else self._dom().accounts[self._acc_name()]
        pending = self.stack[-1][1]["pending"]
        if verb == "SHOW":
            if creating:
                return self.reply([])
            if sub == "account-limits":
                values = {"services": _list_value(acc.services, w.style), "imapConnectionCount": "10"}
                return self.reply(self.show_lines(values, raw=("services",)))
            if sub == "account-quotas":
                return self.reply(self.show_lines({"mboxCount": "0", "totalMessageSize": str(acc.quota_kb)}))
            return self.reply(self.show_lines({"firstName": acc.first_name, "lastName": acc.last_name}))
        if verb == "SET" and len(t) >= 3:
            key = t[1]
            if sub == "account-limits" and key.lower() == "services":
                text = " ".join(t[2:]).strip()
                if not (text.startswith("(") and text.endswith(")")):
                    raise _Err("services must be a list")
                items = [x for x in text[1:-1].replace(",", " ").split() if x]
                pending["services"] = items
                return self.reply([])
            if sub == "account-contactinfo" and key.lower() in ("firstname", "lastname"):
                pending["first_name" if key.lower() == "firstname" else "last_name"] = t[2]
                return self.reply([])
            if sub == "account-quotas" and key.lower() == "totalmessagesize":
                pending["quota_kb"] = int(t[2])
                return self.reply([])
        raise _Err("unknown command")


class _Err(Exception):
    pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeAxigenCLI:
    """Учебный CLI Axigen на 127.0.0.1 (``with FakeAxigenCLI() as srv: srv.port``)."""

    def __init__(self, world: Optional[World] = None, *, tls: Optional[Tuple[str, str, str]] = None) -> None:
        self.world = world or World()
        handler = type("Handler", (_CliHandler,), {"world": self.world,
                                                   "tls": server_context(tls) if tls else None})
        self.server = _Server(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeAxigenCLI":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    def executed(self, prefix: str = "") -> List[str]:
        return [c for _ctx, c in self.world.commands if c.lower().startswith(prefix.lower())]


# ---------------------------------------------------------------------------
#  FTP Backup
# ---------------------------------------------------------------------------
def utf7_encode(text: str) -> str:
    """Изменённый UTF-7 IMAP (RFC 3501)."""
    out, buf = [], []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
            buf.clear()
    for ch in text:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


MTIME = _dt.datetime(2026, 9, 1, 12, 0, tzinfo=_dt.timezone.utc).timestamp()


@dataclass
class FtpOptions:
    user: str = "admin"
    password: str = "Adm1nPass"
    encoding: str = "utf7"          # utf7 | utf8 — как служба пишет имена
    mlsd: bool = True
    feat: bool = True
    list_style: str = "unix"        # unix | dos | broken (не разобрать — клиент перейдёт на NLST)
    tar: bool = False               # письма папки — пакетом messages.tar
    tls: str = "none"               # none | explicit | implicit
    welcome: str = "220 Axigen FTP Backup service ready"


class FtpWorld:
    def __init__(self, tree: Optional[Dict] = None, options: Optional[FtpOptions] = None) -> None:
        self.tree = tree if tree is not None else {}
        self.opt = options or FtpOptions()
        self.commands: List[str] = []
        self.retrieved: List[str] = []
        self.stored: Dict[str, bytes] = {}
        self.created: List[str] = []
        #: служба «принимает» файл, но письмом его не показывает (проверка контроля после заливки)
        self.swallow_uploads = False
        #: служба переименовывает залитые файлы (как хранилище со своими именами)
        self.rename_uploads = False
        self.lock = threading.RLock()
        self.logins = 0
        #: путь (как у людей) → сколько раз отказать в RETR обрывом связи
        self.drop_on_retr: Dict[str, int] = {}
        #: путь → код ошибки 550 на LIST/MLSD (папка «не читается»)
        self.deny: Dict[str, str] = {}

    # -- дерево --------------------------------------------------------------
    def wire(self, name: str) -> bytes:
        if self.opt.encoding == "utf7":
            return utf7_encode(name).encode("ascii")
        return name.encode("utf-8")

    def unwire(self, raw: bytes) -> str:
        text = raw.decode("utf-8", "replace")
        if self.opt.encoding == "utf7":
            from mailarchiver.axigen.ftp import utf7_decode
            decoded = utf7_decode(text)
            return decoded if decoded is not None else text
        return text

    def lookup(self, parts: List[str]):
        node = self.tree
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def _file_data(node) -> Tuple[bytes, float]:
    if isinstance(node, tuple):
        return node[0], node[1]
    return node, MTIME


def make_tar(files: Dict[str, object]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(files):
            data, mtime = _file_data(files[name])
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(mtime)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FtpHandler(socketserver.BaseRequestHandler):
    world: FtpWorld = None   # type: ignore[assignment]
    tls: Optional[ssl.SSLContext] = None

    def setup(self) -> None:
        self.sock = self.request
        self.sock.settimeout(15)
        self.cwd: List[str] = []
        self.authed = False
        self.user = ""
        self.pasv: Optional[socket.socket] = None
        self.prot_p = False
        self.buf = b""

    def send(self, line: str) -> None:
        self.sock.sendall(line.encode("utf-8") + b"\r\n")

    def readline(self) -> Optional[bytes]:
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(4096)
            except (OSError, ssl.SSLError):
                return None
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.rstrip(b"\r")

    def handle(self) -> None:
        opt = self.world.opt
        if opt.tls == "implicit":
            self.sock = self.tls.wrap_socket(self.sock, server_side=True)
            self.prot_p = False
        self.send(opt.welcome)
        while True:
            raw = self.readline()
            if raw is None:
                return
            verb, _, arg_raw = raw.partition(b" ")
            verb = verb.decode("ascii", "replace").upper()
            with self.world.lock:
                self.world.commands.append(verb + (" " + arg_raw.decode("utf-8", "replace") if arg_raw else ""))
            try:
                if not self.dispatch(verb, arg_raw):
                    return
            except (OSError, ssl.SSLError):
                return

    # -- пути ----------------------------------------------------------------
    def parts(self, arg: bytes) -> List[str]:
        if not arg:
            return list(self.cwd)
        base = [] if arg.startswith(b"/") else list(self.cwd)
        for piece in arg.split(b"/"):
            if not piece or piece == b".":
                continue
            if piece == b"..":
                if base:
                    base.pop()
                continue
            base.append(self.world.unwire(piece))
        return base

    def human(self, parts: List[str]) -> str:
        return "/" + "/".join(parts)

    def listing(self, parts: List[str]) -> Optional[List[Tuple[str, bool, int, float, bytes]]]:
        """(имя, каталог?, размер, время, имя в проводе) — содержимое каталога."""
        node = self.world.lookup(parts)
        if not isinstance(node, dict):
            return None
        out = []
        files = {k: v for k, v in node.items() if not isinstance(v, dict)}
        if self.world.opt.tar and files and parts:
            data = make_tar(files)
            out.append(("messages.tar", False, len(data), max(_file_data(v)[1] for v in files.values()),
                        b"messages.tar"))
        else:
            for name, value in files.items():
                data, mtime = _file_data(value)
                out.append((name, False, len(data), mtime, self.world.wire(name)))
        for name, value in node.items():
            if isinstance(value, dict):
                out.append((name, True, 0, MTIME, self.world.wire(name)))
        return out

    # -- данные --------------------------------------------------------------
    def open_data(self) -> Optional[socket.socket]:
        if self.pasv is None:
            self.send("425 Use PASV first")
            return None
        self.pasv.settimeout(10)
        try:
            conn, _ = self.pasv.accept()
        finally:
            self.pasv.close()
            self.pasv = None
        if self.prot_p:
            conn = self.tls.wrap_socket(conn, server_side=True)
        return conn

    def send_data(self, payload: bytes, *, drop: bool = False) -> None:
        self.send("150 Opening data connection")
        conn = self.open_data()
        if conn is None:
            return
        try:
            if drop:
                conn.sendall(payload[: max(1, len(payload) // 2)])
                conn.close()
                # обрыв связи: закрываем и управляющее соединение
                raise OSError("dropped")
            conn.sendall(payload)
        finally:
            try:
                if isinstance(conn, ssl.SSLSocket):
                    conn.unwrap()
            except (OSError, ssl.SSLError, ValueError):
                pass
            conn.close()
        self.send("226 Transfer complete")

    # -- команды -------------------------------------------------------------
    def dispatch(self, verb: str, arg: bytes) -> bool:
        w = self.world
        opt = w.opt
        if verb == "AUTH" and opt.tls == "explicit":
            self.send("234 AUTH TLS successful")
            self.sock = self.tls.wrap_socket(self.sock, server_side=True)
            return True
        if verb == "AUTH":
            self.send("502 AUTH not supported")
            return True
        if verb == "USER":
            self.user = arg.decode("utf-8", "replace")
            self.send("331 Password required")
            return True
        if verb == "PASS":
            if self.user == opt.user and arg.decode("utf-8", "replace") == opt.password:
                self.authed = True
                with w.lock:
                    w.logins += 1
                self.send("230 Logged in")
            else:
                self.send("530 Login incorrect")
            return True
        if verb == "QUIT":
            self.send("221 Bye")
            return False
        if not self.authed:
            self.send("530 Please login")
            return True
        if verb == "PBSZ":
            self.send("200 PBSZ=0")
        elif verb == "PROT":
            self.prot_p = arg.upper() == b"P"
            self.send("200 Protection level set")
        elif verb == "SYST":
            self.send("215 UNIX Type: L8")
        elif verb == "FEAT":
            if not opt.feat:
                self.send("500 Unknown command")
            else:
                feats = [" MLST type*;size*;modify*;"] if opt.mlsd else []
                self.sock.sendall(("211-Features:\r\n" + "".join(f + "\r\n" for f in feats) + "211 End\r\n")
                                  .encode())
        elif verb == "TYPE":
            self.send("200 Type set")
        elif verb == "PWD":
            self.sock.sendall(b'257 "/' + b"/".join(w.wire(p) for p in self.cwd) + b'" is current directory\r\n')
        elif verb == "CWD":
            parts = self.parts(arg)
            if isinstance(w.lookup(parts), dict):
                self.cwd = parts
                self.send("250 OK")
            else:
                self.send("550 No such directory")
        elif verb == "PASV":
            self.pasv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.pasv.bind(("127.0.0.1", 0))
            self.pasv.listen(1)
            port = self.pasv.getsockname()[1]
            self.send(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})")
        elif verb in ("LIST", "MLSD", "NLST"):
            self.list_cmd(verb, arg)
        elif verb == "RETR":
            self.retr(arg)
        elif verb == "STOR":
            self.stor(arg)
        elif verb == "MKD":
            parts = self.parts(arg)
            parent = w.lookup(parts[:-1])
            if not isinstance(parent, dict) or not parts or parts[-1] in parent:
                self.send("550 Cannot create directory")
            else:
                with w.lock:
                    parent[parts[-1]] = {}
                    w.created.append(self.human(parts))
                self.sock.sendall(b'257 "' + arg + b'" created\r\n')
        elif verb == "NOOP":
            self.send("200 OK")
        elif verb == "OPTS":
            self.send("200 OK")
        else:
            self.send("502 Command not implemented")
        return True

    def list_cmd(self, verb: str, arg: bytes) -> None:
        w = self.world
        if verb == "MLSD" and not w.opt.mlsd:
            self.send("500 Unknown command MLSD")
            return
        if arg.startswith(b"-"):
            arg = b""
        parts = self.parts(arg)
        if self.human(parts) in w.deny:
            self.send(f"550 {w.deny[self.human(parts)]}")
            return
        items = self.listing(parts)
        if items is None:
            self.send("550 No such directory")
            return
        lines: List[bytes] = []
        for name, is_dir, size, mtime, wire in sorted(items, key=lambda x: x[0]):
            stamp = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
            if verb == "MLSD":
                facts = (f"type={'dir' if is_dir else 'file'};" + ("" if is_dir else f"size={size};") +
                         f"modify={stamp:%Y%m%d%H%M%S};").encode()
                lines.append(facts + b" " + wire)
            elif verb == "NLST":
                lines.append(wire)
            elif w.opt.list_style == "dos":
                lines.append(f"{stamp:%m-%d-%y  %I:%M%p}       {'<DIR>' if is_dir else str(size).rjust(14)} "
                             .encode() + wire)
            elif w.opt.list_style == "broken":
                lines.append(b"?? " + wire)
            else:
                perm = "drwxr-xr-x" if is_dir else "-rw-r--r--"
                lines.append(f"{perm}   1 axigen   axigen   {size:>10} {stamp:%b %d %H:%M} ".encode() + wire)
        if verb == "MLSD":
            lines.insert(0, f"type=cdir;modify={_dt.datetime.fromtimestamp(MTIME, tz=_dt.timezone.utc):%Y%m%d%H%M%S}; ."
                         .encode())
        self.send_data(b"".join(ln + b"\r\n" for ln in lines))

    def retr(self, arg: bytes) -> None:
        w = self.world
        parts = self.parts(arg)
        human = self.human(parts)
        node = w.lookup(parts)
        if node is None and parts and parts[-1] == "messages.tar" and w.opt.tar:
            folder = w.lookup(parts[:-1])
            if isinstance(folder, dict):
                files = {k: v for k, v in folder.items() if not isinstance(v, dict)}
                node = (make_tar(files), MTIME)
        if node is None or isinstance(node, dict):
            self.send("550 File not found")
            return
        with w.lock:
            w.retrieved.append(human)
            drop = w.drop_on_retr.get(human, 0) > 0
            if drop:
                w.drop_on_retr[human] -= 1
        data, _mtime = _file_data(node)
        self.send_data(data, drop=drop)

    def stor(self, arg: bytes) -> None:
        w = self.world
        parts = self.parts(arg)
        folder = w.lookup(parts[:-1])
        if not isinstance(folder, dict):
            self.send("550 No such directory")
            return
        self.send("150 Ready to receive")
        conn = self.open_data()
        if conn is None:
            return
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        with contextlib.suppress(OSError, ssl.SSLError, ValueError):
            if isinstance(conn, ssl.SSLSocket):
                conn.unwrap()
        conn.close()
        data = b"".join(chunks)
        with w.lock:
            w.stored[self.human(parts)] = data
            if not w.swallow_uploads:
                name = parts[-1] if not w.rename_uploads else f"{len(w.stored):08x}.msg"
                folder[name] = (data, time.time())
        self.send("226 Transfer complete")


class FakeAxigenFTP:
    """Учебная служба FTP Backup на 127.0.0.1."""

    def __init__(self, world: Optional[FtpWorld] = None, *, cert: Optional[Tuple[str, str, str]] = None) -> None:
        self.world = world or FtpWorld()
        handler = type("FtpHandler", (_FtpHandler,), {"world": self.world,
                                                      "tls": server_context(cert) if cert else None})
        self.server = _Server(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeAxigenFTP":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def message(subject: str, *, sender: str = "boss@company.ru", date: str = "Mon, 01 Sep 2025 10:00:00 +0300",
            received: Optional[str] = None, body: str = "Текст письма", msgid: Optional[str] = None) -> bytes:
    """Простое письмо RFC 5322 (байты)."""
    lines = []
    if received is not None:
        lines.append(f"Received: from mx.remote.org by mx.company.ru; {received}")
    lines += [f"From: {sender}", "To: ivanov@company.ru", f"Subject: {subject}", f"Date: {date}",
              f"Message-ID: <{msgid or hashlib.md5(subject.encode()).hexdigest()}@company.ru>",
              "MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8", "", body, ""]
    return "\r\n".join(lines).encode("utf-8")
