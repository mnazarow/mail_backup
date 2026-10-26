"""
CommuniGate Pro — через CLI по протоколу PWD (TCP, обычно порт 106).

CLI — построчный протокол: команда — одна строка, ответ — код (200 — готово,
201 — готово, данные в той же строке, 300 — нужен следующий шаг, 5xx — отказ с
текстом). Данные — в формате CommuniGate («Data Formats»): строки, массивы
``(a,b)``, словари ``{k=v;}``; здесь свой кодировщик и разборщик. После входа
включается режим INLINE: данные приходят в строке ответа ``201``.

Шифрования STARTTLS у PWD нет: TLS — только на отдельном порту, который
администратор сервера настроил приёмником (Listener) с «Init SSL/TLS».
Без шифрования — только на эту же машину или если в подключении это явно
разрешено; тогда вход — только APOP (пароль по сети не идёт): если сервер не
прислал метку в приветствии, вход не выполняется (USER/PASS открытым текстом —
только на эту же машину).

Журнал: пароли в командах заменены на «••••», в ответах (словари настроек) —
значения ключей вида «…Password». Ответ, который не разобран (или не дочитан),
сбрасывает соединение: следующая команда входит заново, а не читает чужой ответ.

Учётные записи — LISTDOMAINS / LISTACCOUNTS, настройки —
GETACCOUNTEFFECTIVESETTINGS (имя, службы AccessModes, квота MaxAccountSize),
состояние — GETACCOUNTINFO (StorageUsed, LastLogin), псевдонимы —
GETACCOUNTALIASES / SETACCOUNTALIASES. Закрыть вход — выключить службы входа
(AccessModes), автоответ и пересылка — правила «#Vacation» и «#Redirect»
(UPDATEACCOUNTMAILRULE), как их делает веб-интерфейс CommuniGate.

Копирование ящиков — по IMAP: SASL PLAIN, логин и пароль администратора,
authzid — адрес ящика (право CanImpersonate).
"""
from __future__ import annotations

import base64
import hashlib
import re
import socket
import ssl
import time
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...errors import ValidationError
from ..base import (AlreadyExists, Cap, Driver, Field, ImapAuth, NotFound, ServerAuthError, ServerError,
                    ServerUnreachable, Status, check_email, imap_fields, norm_email, split_email, tls_fields)
from ..http import ApiLog, Call, _is_loopback, cert_sha256, norm_fingerprint, redact_markup

DEFAULT_PORT = 106
TIMEOUT_S = 60.0
#: Сколько ждать ответа на DELETEACCOUNT: ящик удаляется до ответа — у большого это минуты.
DELETE_TIMEOUT_S = 600.0
#: Предел строки ответа (список учётных записей большого домена — одной строкой).
MAX_LINE = 64 * 1024 * 1024
#: Предел строк многострочного ответа (без режима INLINE).
MAX_BLOCK_LINES = 1_000_000
#: Сколько ответа показывать в журнале.
LOG_RESPONSE = 3000
VACATION = "#Vacation"
REDIRECT = "#Redirect"
#: Службы, через которые входит пользователь (AccessModes). Mail — приём и отправка почты, не вход.
LOGIN_SERVICES = {"pop", "imap", "mapi", "airsync", "sip", "xmpp", "webmail", "ximss", "ftp", "pwd", "ldap",
                  "radius", "webcal", "website", "pbx", "http"}
#: Поля «ФИО, отдел, должность» → настройки учётной записи.
CONTACT_KEYS = {"first_name": "FirstName", "last_name": "FamilyName", "display_name": "RealName",
                "department": "ou", "title": "title"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_RESPONSE = re.compile(r"^(\d{3})(?: (.*))?$", re.S)
_TIME = re.compile(r"^#T(\d{2})-(\d{2})-(\d{4})(?:_(\d{2}):(\d{2}):(\d{2}))?$")
_RULE_DATE = re.compile(r"^(\d{1,2}) ([A-Za-z]{3}) (\d{4})")
_HOST = re.compile(r"^[A-Za-z0-9.\-:]+$")


# ---------------------------------------------------------------------------
#  Формат данных CommuniGate
# ---------------------------------------------------------------------------
class CgpFormatError(ValueError):
    """Ответ сервера не разбирается как данные CommuniGate."""


_BARE = re.compile(r"[A-Za-z0-9]+")
_DELIMS = set(" \t\r\n,;=(){}\"")
_NUMBER = re.compile(r"#(-?)(0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)")


def quote(text: Any) -> str:
    """Строка для команды: буквы и цифры — как есть, прочее — в кавычках с экранированием.

    Перевод строки в значении (текст автоответа) пишется как ``\\e``, табуляция — ``\\t``; другие управляющие знаки
    не принимаются — в команде CLI не бывает ни перевода строки, ни лишней команды.
    """
    text = str(text)
    if _BARE.fullmatch(text):
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\e")
        elif ch == "\t":
            out.append("\\t")
        elif code < 32 or code == 127:
            raise ValidationError(f"В значении для CommuniGate Pro недопустимый управляющий знак (код {code}).")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def encode(value: Any) -> str:
    """Значение → текст в формате CommuniGate (одной строкой)."""
    if value is None:
        return "#NULL#"
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, int):
        return f"#{value}"
    if isinstance(value, (bytes, bytearray)):
        return "[" + base64.b64encode(bytes(value)).decode("ascii") + "]"
    if isinstance(value, dict):
        return "{" + "".join(f"{quote(k)}={encode(v)};" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "(" + ",".join(encode(v) for v in value) + ")"
    return quote(value)


class _Parser:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.n = len(text)

    def fail(self, what: str) -> CgpFormatError:
        return CgpFormatError(f"{what} (позиция {self.i}: {self.s[self.i:self.i + 30]!r})")

    def ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n:
            if s[self.i] in " \t\r\n":
                self.i += 1
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i + 2)
                self.i = n if end < 0 else end + 2
            elif s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end + 1
            else:
                return

    def value(self) -> Any:
        self.ws()
        if self.i >= self.n:
            raise self.fail("данные оборвались")
        c = self.s[self.i]
        if c == "{":
            return self.dictionary()
        if c == "(":
            return self.array()
        if c == '"':
            return self.string()
        if c == "[":
            return self.datablock()
        word = self.word()
        if word == "#NULL#":
            return None
        m = _NUMBER.fullmatch(word)
        if m:
            digits = m.group(2)
            base = {"0x": 16, "0o": 8, "0b": 2}.get(digits[:2].lower(), 10)
            number = int(digits[2:] if base != 10 else digits, base)
            return -number if m.group(1) else number
        return word                         # атом, #T… (время), #I[…] (адрес) — строкой

    def word(self) -> str:
        start = self.i
        while self.i < self.n and self.s[self.i] not in _DELIMS:
            self.i += 1
        if self.i == start:
            raise self.fail("неожиданный знак")
        return self.s[start:self.i]

    def string(self) -> str:
        out: List[str] = []
        while True:
            self.i += 1
            s = self.s
            while True:
                if self.i >= self.n:
                    raise self.fail("строка не закрыта")
                c = s[self.i]
                if c == '"':
                    self.i += 1
                    break
                if c != "\\":
                    out.append(c)
                    self.i += 1
                    continue
                nxt = s[self.i + 1:self.i + 2]
                if nxt in ("e", "n"):
                    out.append("\n")
                    self.i += 2
                elif nxt == "r":
                    out.append("\r")
                    self.i += 2
                elif nxt == "t":
                    out.append("\t")
                    self.i += 2
                elif re.fullmatch(r"\d{3}", s[self.i + 1:self.i + 4]):
                    out.append(chr(int(s[self.i + 1:self.i + 4])))
                    self.i += 4
                elif nxt == "u" and s[self.i + 2:self.i + 3] == "'":
                    end = s.find("'", self.i + 3)
                    try:
                        out.append(chr(int(s[self.i + 3:end], 16)))
                    except ValueError as exc:
                        raise self.fail("неверный знак \\u") from exc
                    self.i = end + 1
                elif nxt:
                    out.append(nxt)
                    self.i += 2
                else:
                    raise self.fail("строка не закрыта")
            # строки подряд через пробел — одна строка
            save = self.i
            self.ws()
            if self.i < self.n and self.s[self.i] == '"':
                continue
            self.i = save
            return "".join(out)

    def datablock(self) -> bytes:
        end = self.s.find("]", self.i)
        if end < 0:
            raise self.fail("блок данных не закрыт")
        raw = re.sub(r"\s+", "", self.s[self.i + 1:end])
        self.i = end + 1
        try:
            return base64.b64decode(raw)
        except ValueError as exc:
            raise self.fail("блок данных — не base64") from exc

    def array(self) -> List[Any]:
        self.i += 1
        out: List[Any] = []
        self.ws()
        if self.i < self.n and self.s[self.i] == ")":
            self.i += 1
            return out
        while True:
            out.append(self.value())
            self.ws()
            if self.i < self.n and self.s[self.i] == ",":
                self.i += 1
                continue
            if self.i < self.n and self.s[self.i] == ")":
                self.i += 1
                return out
            raise self.fail("в массиве нет «,» или «)»")

    def dictionary(self) -> Dict[str, Any]:
        self.i += 1
        out: Dict[str, Any] = {}
        while True:
            self.ws()
            if self.i >= self.n:
                raise self.fail("словарь не закрыт")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self.value()
            self.ws()
            if self.i >= self.n or self.s[self.i] != "=":
                raise self.fail("в словаре нет «=»")
            self.i += 1
            value = self.value()
            self.ws()
            if self.i < self.n and self.s[self.i] == ";":
                self.i += 1
            elif not (self.i < self.n and self.s[self.i] == "}"):
                raise self.fail("в словаре нет «;»")
            out[str(key)] = value


def parse(text: str) -> Any:
    """Текст в формате CommuniGate → строки, int, None, bytes, списки, словари."""
    p = _Parser(text or "")
    value = p.value()
    p.ws()
    if p.i < p.n:
        raise p.fail("лишнее после данных")
    return value


def cgp_time_iso(value: Any) -> str:
    """``#T20-10-2026_08:00:00`` (GMT) → ``2026-10-20T08:00:00Z``; «никогда» и прочее — пусто."""
    m = _TIME.match(str(value or "").strip())
    if not m:
        return ""
    d, mo, y, hh, mi, ss = m.groups()
    return f"{y}-{mo}-{d}T{hh or '00'}:{mi or '00'}:{ss or '00'}Z"


def parse_size(value: Any) -> Optional[int]:
    """``MaxAccountSize``: ``100M``, ``10240K``, байты, ``unlimited`` → байты (None — без предела)."""
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip().lower()
    m = re.fullmatch(r"#?(\d+)\s*([kmgt]?)b?", text)
    if not m:
        return None
    number = int(m.group(1)) * 1024 ** " kmgt".index(m.group(2) or " ")
    return number or None


def size_text(value: int) -> str:
    mb = 1024 * 1024
    if value % mb == 0:
        return f"{value // mb}M"
    if value % 1024 == 0:
        return f"{value // 1024}K"
    return str(value)


def _number(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"#?(\d+)", str(value or "").strip())
    return int(m.group(1)) if m else None


def services(modes: Any) -> Tuple[bool, set]:
    """``AccessModes`` → (все службы, набор служб в нижнем регистре); число в начале (старый формат) пропускается."""
    if isinstance(modes, str):
        low = modes.strip().lower()
        if low == "all":
            return True, set()
        return False, set() if low in ("none", "") else {low}
    if isinstance(modes, list):
        return False, {str(x).strip().lower() for x in modes
                       if isinstance(x, str) and not re.fullmatch(r"#?-?\d+", x.strip())}
    return False, set()


def login_blocked(modes: Any) -> Optional[bool]:
    if modes is None:
        return None
    every, names = services(modes)
    return False if every else not (names & LOGIN_SERVICES)


def _modes_text(modes: Any) -> str:
    every, names = services(modes)
    if every:
        return "все"
    return ", ".join(sorted(names)) if names else "никаких"


def rule_date(day: str, end: bool = False) -> str:
    """``2026-11-01`` → ``01 Nov 2026 00:00:00`` (условие правила «Current Date»)."""
    try:
        d = date.fromisoformat(day)
    except ValueError as exc:
        raise ValidationError(f"Дата указана неверно: «{day}» (нужно ГГГГ-ММ-ДД).") from exc
    return f"{d.day:02d} {_MONTHS[d.month - 1]} {d.year} {'23:59:59' if end else '00:00:00'}"


def _rule_day(value: Any) -> str:
    m = _RULE_DATE.match(str(value or "").strip())
    if not m or m.group(2).title() not in _MONTHS:
        return ""
    return f"{m.group(3)}-{_MONTHS.index(m.group(2).title()) + 1:02d}-{int(m.group(1)):02d}"


# ---------------------------------------------------------------------------
#  Сеанс CLI
# ---------------------------------------------------------------------------
class CliClient:
    """Сеанс CLI CommuniGate Pro (PWD): TLS или открытый TCP, вход, режим INLINE, команды с журналом."""

    def __init__(self, host: str, port: int, *, api: Optional[ApiLog] = None, use_tls: bool = True,
                 verify_tls: bool = True, cert_sha256: str = "", allow_plain: bool = False,
                 timeout: float = TIMEOUT_S, label: str = "CommuniGate Pro") -> None:
        if not use_tls and not allow_plain and not _is_loopback(host):
            raise ServerError(f"Подключение к {host} без шифрования не разрешено: пароль администратора, команды и "
                              f"новые пароли учётных записей шли бы по сети открытым текстом.",
                              hint="Настройте на сервере приёмник (Listener) CLI/PWD с TLS и выберите «TLS» или, "
                                   "только для доверенной сети, включите «Разрешить без шифрования».")
        self.host = host
        self.port = int(port)
        self.api = api or ApiLog()
        self.use_tls = use_tls
        self.verify_tls = verify_tls
        self.pin = norm_fingerprint(cert_sha256)
        self.timeout = timeout
        self.label = label
        self.sock: Optional[socket.socket] = None
        self.rfile = None
        self.banner = ""
        self.ready = False
        #: отпечаток сертификата, который прислал сервер (для «Доверять этому сертификату»)
        self.seen_fingerprint = ""

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"cgp://{host}:{self.port}"

    def _log(self, shown: str, code: int, started: float, response: str = "", error: str = "") -> None:
        # значения ключей «…Password» (настройки учётной записи могут нести пароль) — «••••» и в команде, и в ответе
        self.api.add(Call("CLI", self.url, code, int((time.monotonic() - started) * 1000), redact_markup(shown or ""),
                          redact_markup(response or "")[:LOG_RESPONSE], error))

    # -- соединение ---------------------------------------------------------------------------
    def _context(self, verify: bool) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _peek_fingerprint(self) -> str:
        """Отпечаток сертификата без проверки — только показать администратору (ничего не отправляется)."""
        try:
            with socket.create_connection((self.host, self.port), timeout=min(self.timeout, 10)) as raw:
                with self._context(False).wrap_socket(raw, server_hostname=self.host) as tls:
                    der = tls.getpeercert(binary_form=True)
                    return cert_sha256(der) if der else ""
        except (OSError, ssl.SSLError):
            return ""

    def connect(self) -> None:
        started = time.monotonic()
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            self._log("(соединение)", 0, started, error=str(exc) or type(exc).__name__)
            raise ServerUnreachable(
                f"{self.label}: нет связи с {self.host}:{self.port} ({exc or type(exc).__name__}).",
                hint="Проверьте адрес и порт CLI (PWD), межсетевой экран и что служба PWD на сервере включена.",
                retryable=True, cause=exc) from exc
        sock: socket.socket = raw
        if self.use_tls:
            try:
                sock = self._context(self.verify_tls and not self.pin).wrap_socket(raw, server_hostname=self.host)
            except (ssl.SSLError, OSError) as exc:
                raw.close()
                if isinstance(exc, ssl.SSLCertVerificationError):
                    self.seen_fingerprint = self._peek_fingerprint()
                self._log("(соединение TLS)", 0, started, error=str(exc) or type(exc).__name__)
                raise ServerUnreachable(
                    f"{self.label}: защищённое соединение с {self.host}:{self.port} не установлено ({exc}).",
                    hint="Нужен порт CLI (PWD) с TLS — приёмник (Listener) с «Init SSL/TLS»; на обычном порту 106 "
                         "TLS нет. Если у сервера самоподписанный сертификат — «Проверить подключение» покажет его "
                         "отпечаток, им можно разрешить доверие.", cause=exc) from exc
            der = sock.getpeercert(binary_form=True)
            if der:
                self.seen_fingerprint = cert_sha256(der)
            if self.pin and self.seen_fingerprint != self.pin:
                sock.close()
                self._log("(соединение TLS)", 0, started, error="сертификат не совпал")
                raise ServerUnreachable(
                    f"Сертификат {self.host} не тот, которому разрешено доверять (SHA-256 "
                    f"{self.seen_fingerprint or 'неизвестен'}).",
                    hint="Если сертификат на сервере меняли — сверьте новый отпечаток и доверьтесь ему заново в "
                         "подключении; если нет — соединение могли перехватить.")
        self.sock = sock
        self.rfile = sock.makefile("rb")
        try:
            code, text = self._read()
        except ServerError as exc:
            self._log("(соединение)", 0, started, error=exc.message)
            self._drop()
            raise
        self._log("(соединение)", code, started, text)
        if code != 200:
            self._drop()
            raise ServerError(f"{self.label}: сервер не готов принять команды ({code} {text}).")
        self.banner = text

    def _drop(self) -> None:
        self.ready = False
        for item in (self.rfile, self.sock):
            try:
                if item is not None:
                    item.close()
            except OSError:
                pass
        self.rfile = self.sock = None

    def _readline(self) -> str:
        if self.rfile is None:
            raise ServerUnreachable(f"{self.label}: соединения с сервером нет.", retryable=True)
        try:
            line = self.rfile.readline(MAX_LINE + 1)
        except (OSError, ssl.SSLError) as exc:
            raise ServerUnreachable(f"{self.label}: связь с {self.host}:{self.port} прервалась "
                                    f"({exc or type(exc).__name__}).", retryable=True, cause=exc) from exc
        if not line:
            raise ServerUnreachable(f"{self.label}: сервер закрыл соединение.", retryable=True)
        if len(line) > MAX_LINE:
            raise ServerError(f"{self.label}: ответ сервера длиннее {MAX_LINE // 1048576} МБ — не принят.")
        return line.decode("utf-8", errors="replace").rstrip("\r\n")

    def _read(self) -> Tuple[int, str]:
        text = self._readline()
        m = _RESPONSE.match(text)
        if not m:
            raise ServerError(f"{self.label}: непонятный ответ сервера: {text[:200]!r}")
        return int(m.group(1)), m.group(2) or ""

    def _send(self, line: str) -> None:
        if any(ch in line for ch in "\r\n\x00"):
            raise ValidationError("В команде CLI не может быть перевода строки.")
        if self.sock is None:
            raise ServerUnreachable(f"{self.label}: соединения с сервером нет.", retryable=True)
        try:
            self.sock.sendall(line.encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise ServerUnreachable(f"{self.label}: связь с {self.host}:{self.port} прервалась ({exc}).",
                                    retryable=True, cause=exc) from exc

    def _block(self) -> str:
        """Данные после ``200`` без режима INLINE: до строки, что кончается скобкой, парной к первой."""
        first = self._readline().strip()
        if not first or first[0] not in "({":
            return first
        closing = ")" if first[0] == "(" else "}"
        parts, line = [first], first
        for _ in range(MAX_BLOCK_LINES):
            if line.endswith(closing):
                return " ".join(parts)
            line = self._readline().strip()
            parts.append(line)
        raise ServerError(f"{self.label}: ответ сервера слишком длинный — не принят.")

    @staticmethod
    def _error(label: str, verb: str, code: int, text: str) -> ServerError:
        low = text.lower()
        message = f"{label}: команда {verb} отклонена ({code}): {text or 'без описания'}"
        if "already exist" in low:
            return AlreadyExists(message)
        if any(s in low for s in ("unknown account", "unknown user", "not found", "unknown alias", "no such")):
            return NotFound(message)
        if any(s in low for s in ("not allowed", "permission", "access denied", "no rights", "insufficient",
                                  "not authorized", "prohibited")):
            return ServerAuthError(message, hint="Выдайте администратору нужные права в CommuniGate Pro (раздел "
                                                 "«Что настроить на сервере»).")
        return ServerError(message)

    def command(self, line: str, *, shown: Optional[str] = None, data: bool = False,
                timeout: Optional[float] = None) -> Any:
        """Команда → текст ответа (или данные, если ``data``); отказ — исключение с текстом сервера.

        Ответ не прочитан или не разобран (обрыв, тайм-аут, непонятная строка, слишком длинный ответ, данные не
        в формате CommuniGate) — соединение сбрасывается: где в нём конец этого ответа, неизвестно, и следующая
        команда прочитала бы его хвост как свой ответ. Следующая команда драйвера войдёт заново.
        """
        verb = line.split(" ", 1)[0].upper()
        shown = line if shown is None else shown
        started = time.monotonic()
        try:
            if timeout and self.sock is not None:
                self.sock.settimeout(timeout)
            self._send(line)
            code, text = self._read()
            payload: Optional[str] = None
            if code == 201:
                payload = text
            elif code == 200 and data:
                payload = self._block()
        except ServerError as exc:
            self._log(shown, 0, started, error=exc.message)
            self._drop()
            raise
        finally:
            if timeout and self.sock is not None:
                self.sock.settimeout(self.timeout)
        self._log(shown, code, started, payload if payload is not None else text)
        if code not in (200, 201):
            raise self._error(self.label, verb, code, text)
        if not data:
            return text
        try:
            return parse(payload or "")
        except CgpFormatError as exc:
            self._drop()
            raise ServerError(f"{self.label}: ответ на {verb} не разобран: {exc}") from exc

    # -- вход и выход ---------------------------------------------------------------------------
    def _step(self, line: str, shown: str) -> Tuple[int, str]:
        started = time.monotonic()
        try:
            self._send(line)
            code, text = self._read()
        except ServerError as exc:
            self._log(shown, 0, started, error=exc.message)
            self._drop()
            raise
        self._log(shown, code, started, text)
        return code, text

    def login(self, user: str, password: str) -> None:
        if not user or any(ch.isspace() or ord(ch) < 32 for ch in user):
            raise ValidationError("Имя администратора CommuniGate Pro — без пробелов и управляющих знаков.")
        if not password or any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
            raise ValidationError("Пароль администратора CommuniGate Pro задан неверно (управляющие знаки).")
        self.api.secret(password)
        stamp = re.search(r"<[^<>\s]+@[^<>\s]+>", self.banner)
        if not self.use_tls and stamp:
            # APOP: по сети идёт только MD5 (метка из приветствия + пароль), пароль — нет
            digest = hashlib.md5((stamp.group(0) + password).encode("utf-8")).hexdigest()
            code, text = self._step(f"APOP {user} {digest}", f"APOP {user} ••••")
            how = "APOP"
        elif not self.use_tls and not _is_loopback(self.host):
            # без метки — только USER/PASS, то есть пароль администратора открытым текстом по сети; метку мог
            # убрать и перехватчик, чтобы получить пароль, — не входим
            self.api.note("сервер без шифрования не прислал метку APOP — вход паролем открытым текстом не выполнен")
            raise ServerAuthError(
                f"{self.label}: сервер {self.host} (без шифрования) не прислал в приветствии метку APOP — вход "
                f"паролем открытым текстом (USER/PASS) MailArchiver не выполняет.",
                hint="Подключайтесь по TLS (приёмник CLI/PWD с «Init SSL/TLS»). Без шифрования вход — только APOP: "
                     "приветствие сервера должно содержать метку <…@…>.")
        else:
            code, text = self._step(f"USER {user}", f"USER {user}")
            if code == 300:
                code, text = self._step(f"PASS {password}", "PASS ••••")
            how = "USER/PASS"
        if code != 200:
            hint = "Проверьте администратора и пароль."
            if how == "APOP":
                hint += (" Без TLS вход идёт по APOP — он работает, только если пароль администратора хранится на "
                         "сервере открыто (не U-crpt); иначе подключайтесь по TLS.")
            raise ServerAuthError(f"{self.label}: сервер не принял логин или пароль администратора ({code} {text}).",
                                  hint=hint)

    def open(self, user: str, password: str) -> None:
        self.connect()
        try:
            self.login(user, password)
            try:
                self.command("INLINE")
            except ServerUnreachable:
                raise
            except ServerError:
                if self.sock is None:           # ответ не разобран — соединение уже сброшено
                    raise
                self.api.note("режим INLINE не включён — данные читаются отдельными строками")
        except BaseException:
            self._drop()
            raise
        self.ready = True

    def close(self) -> None:
        if self.sock is None:
            return
        if self.ready:
            started = time.monotonic()
            try:
                self.sock.settimeout(5)
                self._send("QUIT")
                code, text = self._read()
                self._log("QUIT", code, started, text)
            except (ServerError, ValidationError, OSError):
                pass
        self._drop()


# ---------------------------------------------------------------------------
#  Драйвер
# ---------------------------------------------------------------------------
class CommuniGateDriver(Driver):
    type = "communigate"
    label = "CommuniGate Pro"
    summary = ("CLI (PWD): учётные записи, вход (службы AccessModes), пароли, квота, псевдонимы, ФИО, создание и "
               "удаление, автоответ и пересылка правилами; копирование ящиков по IMAP входом администратора "
               "«от имени» ящика (CanImpersonate).")
    cloud = False
    fields = (
        Field("host", "Адрес сервера CommuniGate Pro", required=True, placeholder="mail.example.ru"),
        Field("port", "Порт CLI (PWD)", "int", default=DEFAULT_PORT,
              help="106 — обычный порт PWD, без шифрования. Для TLS — порт приёмника (Listener) с «Init SSL/TLS», "
                   "который вы настроили на сервере."),
        Field("security", "Шифрование CLI", "select", default="tls",
              options=(("tls", "TLS (защищённый порт)"), ("plain", "без шифрования (доверенная сеть)"))),
        Field("admin", "Администратор", required=True, placeholder="postmaster@example.ru",
              help="Учётная запись с правами администратора сервера или доменов (и правом CanImpersonate для "
                   "копирования ящиков без паролей сотрудников)."),
        Field("admin_password", "Пароль администратора", "password", secret=True, required=True),
        Field("domains", "Домены", placeholder="example.ru, example.com", advanced=True,
              help="Через запятую — работать только с этими доменами (обязательно для администратора домена). "
                   "Пусто — все домены сервера (LISTDOMAINS, нужны права администратора сервера)."),
        Field("allow_plain", "Разрешить без шифрования", "bool", default=False, advanced=True,
              help="Только для доверенной сети: команды и новые пароли учётных записей пойдут открытым текстом. "
                   "Пароль администратора — нет: без шифрования вход только по APOP, а если сервер не прислал метку "
                   "APOP, MailArchiver не входит."),
        *imap_fields(),
        *tls_fields(),
    )
    caps = frozenset({Cap.READ, Cap.USAGE, Cap.LAST_LOGIN, Cap.BLOCK, Cap.PASSWORD, Cap.QUOTA, Cap.ALIASES,
                      Cap.CONTACT, Cap.CREATE, Cap.DELETE, Cap.AUTOREPLY, Cap.FORWARD, Cap.IMAP_ADMIN, Cap.STATUS})
    contact_fields = ("first_name", "last_name", "display_name", "department", "title")
    password_rule = "от 8 до 64 знаков без пробелов"
    limits = (
        "«Требовать смену пароля при входе» не поддерживается: ключ этой настройки в документации CLI не назван.",
        "Закрыть вход — выключить службы входа (AccessModes): остаётся только Mail (письма принимаются) или ничего "
        "(письма ждут в очереди, затем отклоняются). Открыть вход — вернуть службы по умолчанию для домена; если у "
        "учётной записи был свой набор служб, он показан в итоге действия при закрытии.",
        "Перед удалением архив сверяется с ящиком по IMAP после закрытия входа, а вход администратора «от имени» "
        "(CanImpersonate) по документации требует включённой у ящика службы IMAP. Если сервер не пустит — удаление "
        "остановится на последней копии, ничего не удалив.",
        "Телефоны не записываются: имена их настроек зависят от интеграции с каталогом (Directory Integration).",
        "Число писем в ящике CLI не отдаёт — в сверке только занятое место (StorageUsed) и квота (MaxAccountSize).",
        "Лицензии и очередь писем по CLI не читаются (команды для них не документированы) — в мониторинге только "
        "версия, домены и число учётных записей.",
        "Автоответ и пересылка — правила «#Vacation» и «#Redirect» (как в веб-интерфейсе); даты автоответа — по "
        "времени сервера. Пересылка — действие «Mirror to», без копии — ещё «Discard».",
        "Псевдоним — только в домене самой учётной записи.",
        "Без шифрования (CLI на другой машине) администратор входит только по APOP: пароль администратора должен "
        "храниться на сервере открыто (не U-crpt), а приветствие — содержать метку APOP; иначе подключайтесь по TLS. "
        "Паролем открытым текстом (USER/PASS) MailArchiver входит только по TLS или на эту же машину.",
        "Удаление необратимо: учётная запись удаляется вместе с ящиком.",
    )
    setup = (
        "Заведите администратора для MailArchiver (или используйте postmaster): права администратора сервера или "
        "доменов (CanCreateAccounts, CanCreateAliases, управление правилами), для копирования ящиков без паролей — "
        "право CanImpersonate: SETACCOUNTRIGHTS backup-admin@example.ru (CanImpersonate, …) — сначала прочитайте "
        "прежние права GETACCOUNTRIGHTS, SET их заменяет.",
        "Шифрование: в WebAdmin → Settings → Services → PWD добавьте приёмник (Listener) на отдельном порту с "
        "«Init SSL/TLS» и сертификатом домена — его порт укажите в подключении. Без TLS (порт 106) — только в "
        "доверенной сети: выберите «без шифрования» и включите «Разрешить без шифрования»; вход тогда — только по "
        "APOP (пароль администратора на сервере хранится открыто, не U-crpt).",
        "Разрешите вход в CLI с адреса MailArchiver: ограничения адресов приёмника PWD и список Client IP Addresses.",
        "IMAP: порт 993 (IMAPS) должен быть доступен, у учётных записей включена служба IMAP; вход без шифрования "
        "не включайте.",
        "Самоподписанный сертификат: «Проверить подключение» покажет его отпечаток — нажмите «Доверять этому "
        "сертификату».",
    )

    def __init__(self, conn, api) -> None:
        super().__init__(conn, api)
        self._cli: Optional[CliClient] = None
        self._no_info = False
        self.incomplete = False
        self.sync_problems: List[str] = []

    # -- настройки и соединение ------------------------------------------------------------------
    @staticmethod
    def _host(raw: Any) -> str:
        host = str(raw or "").strip().strip("[]")
        if not host or not _HOST.fullmatch(host):
            raise ValidationError(f"Адрес сервера CommuniGate Pro указан неверно: «{raw}».",
                                  hint="Только имя или IP-адрес, без http:// и порта (порт — отдельным полем).")
        return host

    @staticmethod
    def _port(raw: Any) -> int:
        try:
            port = int(raw or DEFAULT_PORT)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Порт CLI указан неверно: «{raw}».") from exc
        if not 0 < port < 65536:
            raise ValidationError(f"Порт CLI указан неверно: {port}.")
        return port

    @staticmethod
    def _split_domains(value: Any) -> List[str]:
        return [d.strip().lower() for d in re.split(r"[\s,;]+", str(value or "")) if d.strip()]

    @classmethod
    def validate_settings(cls, settings: Dict[str, Any], secrets: Dict[str, str]) -> List[str]:
        host = cls._host(settings.get("host"))
        cls._port(settings.get("port"))
        for d in cls._split_domains(settings.get("domains")):
            check_email(f"x@{d}", "Домен")
        if (settings.get("security") or "tls") == "plain" and not settings.get("allow_plain") \
                and not _is_loopback(host):
            raise ValidationError("CLI без шифрования: включите «Разрешить без шифрования» (только для доверенной "
                                  "сети) или выберите TLS.")
        return []

    @property
    def use_tls(self) -> bool:
        return str(self.setting("security") or "tls") != "plain"

    @property
    def cli(self) -> CliClient:
        if self._cli is None:
            self._cli = CliClient(self._host(self.setting("host")), self._port(self.setting("port")), api=self.api,
                                  use_tls=self.use_tls, verify_tls=bool(self.setting("verify_tls", True)),
                                  cert_sha256=str(self.setting("cert_sha256") or ""),
                                  allow_plain=bool(self.setting("allow_plain")), label=self.label)
        if not self._cli.ready:
            admin = str(self.setting("admin") or "").strip()
            password = self.secret("admin_password")
            if not admin or not password:
                raise ServerAuthError(f"{self.label}: не заданы администратор и его пароль.")
            self._cli.open(admin, password)
        return self._cli

    @property
    def seen_fingerprint(self) -> str:
        return self._cli.seen_fingerprint if self._cli is not None else ""

    def close(self) -> None:
        if self._cli is not None:
            self._cli.close()

    def run(self, verb: str, *args: str, data: bool = False, shown: Optional[Sequence[str]] = None,
            timeout: Optional[float] = None) -> Any:
        """Команда CLI: аргументы уже закодированы (:func:`quote`, :func:`encode`); ``shown`` — для журнала.
        Соединение сброшено после непонятного ответа — здесь оно открывается заново (вход и INLINE)."""
        line = " ".join((verb,) + args)
        return self.cli.command(line, shown=None if shown is None else " ".join((verb, *shown)), data=data,
                                timeout=timeout)

    @staticmethod
    def _q(email: str) -> str:
        return quote(check_email(email))

    # -- чтение ---------------------------------------------------------------------------------
    def _domains(self) -> List[str]:
        configured = self._split_domains(self.setting("domains"))
        if configured:
            return configured
        try:
            out = self.run("LISTDOMAINS", data=True)
        except ServerUnreachable:
            raise
        except ServerError as exc:
            admin = norm_email(self.setting("admin"))
            if "@" in admin:
                self.api.note(f"список доменов не прочитан ({exc.message}) — только домен администратора")
                return [admin.split("@", 1)[1]]
            raise ServerAuthError(f"{self.label}: список доменов не прочитан ({exc.message}).",
                                  hint="Перечислите домены в подключении («Домены») или дайте администратору права "
                                       "администратора сервера.") from exc
        if not isinstance(out, list):
            raise ServerError(f"{self.label}: LISTDOMAINS вернул не список.")
        return [str(d).lower() for d in out if isinstance(d, str) and d]

    def _names(self, domain: str) -> Dict[str, Any]:
        out = self.run("LISTACCOUNTS", quote(domain), data=True)
        if not isinstance(out, dict):
            raise ServerError(f"{self.label}: LISTACCOUNTS {domain} вернул не словарь.")
        return out

    def _aliases(self, email: str) -> List[str]:
        out = self.run("GETACCOUNTALIASES", self._q(email), data=True)
        return [str(a).lower() for a in out if isinstance(a, str) and a] if isinstance(out, list) else []

    def _info(self, email: str) -> Dict[str, Any]:
        if self._no_info:
            return {}
        try:
            out = self.run("GETACCOUNTINFO", self._q(email), encode(["LastLogin", "LastAddress", "StorageUsed"]),
                           data=True)
        except ServerAuthError as exc:
            self._no_info = True
            self.sync_problems.append(f"занятое место и последний вход не прочитаны — не хватает прав: {exc.message}")
            return {}
        if not isinstance(out, dict):
            return {}
        return {k.lstrip("#"): v for k, v in out.items()}

    def _account(self, email: str, kind: str = "", *, full: bool = False) -> Optional[Dict[str, Any]]:
        email = norm_email(email)
        local, domain = split_email(email)
        settings = self.run("GETACCOUNTEFFECTIVESETTINGS", self._q(email), data=True)
        if not isinstance(settings, dict):
            raise ServerError(f"{self.label}: настройки {email} не прочитаны (ответ не словарь).")
        aliases = self._aliases(email)
        if local in aliases:
            return None                     # это псевдоним другой учётной записи
        info = self._info(email)

        def text(key: str) -> str:
            value = settings.get(key)
            return value if isinstance(value, str) else ""
        modes = settings.get("AccessModes")
        acc: Dict[str, Any] = {
            "email": email, "native_id": "", "first_name": text("FirstName"), "last_name": text("FamilyName"),
            "display_name": text("RealName"), "aliases": [f"{a}@{domain}" for a in aliases],
            "login_blocked": login_blocked(modes), "quota_bytes": parse_size(settings.get("MaxAccountSize")),
            "used_bytes": _number(info.get("StorageUsed")), "last_login": cgp_time_iso(info.get("LastLogin")),
            "department": text("ou"), "title": text("title"), "kind": "user",
            "info": {"type": kind, "services": _modes_text(modes)},
        }
        if info.get("LastAddress"):
            acc["info"]["last_address"] = str(info["LastAddress"])
        if full:
            rules = self._rules(email)
            vacation = self._find(rules, VACATION)
            acc["autoreply"] = bool(vacation and self._prio(vacation) > 0)
            acc["forward_to"] = self._targets(self._find(rules, REDIRECT))
        return acc

    def list_accounts(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        failed: List[str] = []
        for domain in self._domains():
            for name, kind in sorted(self._names(domain).items()):
                email = f"{name}@{domain}".lower()
                try:
                    acc = self._account(email, str(kind or ""))
                except NotFound:
                    continue                # удалили, пока шла сверка
                except (ServerUnreachable, ServerAuthError):
                    raise
                except (ServerError, ValidationError) as exc:
                    failed.append(f"{email} ({getattr(exc, 'message', exc)})")
                    continue
                if acc is not None:
                    out.append(acc)
        if failed:
            self.incomplete = True
            self.sync_problems.append(f"не прочитаны учётные записи ({len(failed)}): " + "; ".join(failed[:5]) +
                                      (" …" if len(failed) > 5 else ""))
        return out

    def get_account(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            return self._account(email, full=True)
        except NotFound:
            return None

    # -- изменения --------------------------------------------------------------------------------
    def _update(self, email: str, settings: Dict[str, Any]) -> None:
        self.run("UPDATEACCOUNTSETTINGS", self._q(email), encode(settings))

    def set_login(self, email: str, enabled: bool, *, keep_receiving: bool = True) -> str:
        if enabled:
            self._update(email, {"AccessModes": "default"})
            return "вход открыт: службы — как по умолчанию для домена"
        explicit = self.run("GETACCOUNTSETTINGS", self._q(email), data=True)
        before = explicit.get("AccessModes") if isinstance(explicit, dict) else None
        self._update(email, {"AccessModes": ["Mail"] if keep_receiving else "None"})
        text = "вход закрыт: службы входа выключены; " + ("письма принимаются (служба Mail)" if keep_receiving else
                                                          "приём писем остановлен (ждут в очереди, затем отказ)")
        if before is not None and before != "default" and login_blocked(before) is False and not services(before)[0]:
            text += (f"; у учётной записи был свой набор служб ({_modes_text(before)}) — при открытии входа будут "
                     f"службы по умолчанию для домена")
        return text

    def set_password(self, email: str, password: str, *, must_change: bool = False) -> str:
        self.check_password(password)
        self.api.secret(password)
        q = self._q(email)
        self.run("SETACCOUNTPASSWORD", q, "PASSWORD", quote(password), shown=(q, "PASSWORD", "••••"))
        return "пароль сменён" + ("; смену пароля при входе CommuniGate Pro через CLI не требует" if must_change
                                  else "")

    def set_quota(self, email: str, quota_bytes: Optional[int]) -> str:
        if quota_bytes is not None and int(quota_bytes) < 0:
            raise ValidationError("Квота не может быть отрицательной.")
        self._update(email, {"MaxAccountSize": size_text(int(quota_bytes)) if quota_bytes else "unlimited"})
        return "" if quota_bytes else "квота снята (без предела)"

    def _set_aliases(self, email: str, names: List[str]) -> List[str]:
        domain = split_email(email)[1]
        self.run("SETACCOUNTALIASES", self._q(email), encode(names))
        return [f"{n}@{domain}" for n in names]

    def add_alias(self, email: str, alias: str) -> List[str]:
        email, alias = check_email(email), check_email(alias, "Псевдоним")
        name, domain = split_email(alias)
        if domain != split_email(email)[1]:
            raise ValidationError(f"Псевдоним CommuniGate Pro — только в домене учётной записи "
                                  f"({split_email(email)[1]}), а не {domain}.")
        have = self._aliases(email)
        return self._set_aliases(email, have if name in have else have + [name])

    def remove_alias(self, email: str, alias: str) -> List[str]:
        email = check_email(email)
        name, domain = split_email(alias)
        have = self._aliases(email)
        if domain != split_email(email)[1] or name not in have:
            raise NotFound(f"{self.label}: у {email} нет псевдонима {norm_email(alias)}.")
        return self._set_aliases(email, [a for a in have if a != name])

    def set_contact(self, email: str, values: Dict[str, str]) -> str:
        # пустое значение — «default»: настройка убирается
        settings = {CONTACT_KEYS[k]: (v or "default") for k, v in values.items() if k in CONTACT_KEYS}
        if settings:
            self._update(email, settings)
        return ""

    def create_account(self, email: str, *, first_name: str, last_name: str, password: str,
                       must_change: bool = True, quota_bytes: Optional[int] = None,
                       extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        email = check_email(email)
        self.check_password(password)
        self.api.secret(password)
        local, _domain = split_email(email)
        display = " ".join(x for x in (first_name, last_name) if x) or local
        settings: Dict[str, Any] = {"Password": password, "RealName": display}
        if first_name:
            settings["FirstName"] = first_name
        if last_name:
            settings["FamilyName"] = last_name
        if quota_bytes:
            settings["MaxAccountSize"] = size_text(int(quota_bytes))
        q = self._q(email)
        try:
            self.run("CREATEACCOUNT", q, encode(settings), shown=(q, encode(dict(settings, Password="••••"))))
        except AlreadyExists as exc:
            raise AlreadyExists(f"{self.label}: учётная запись {email} уже есть.") from exc
        return {"email": email, "first_name": first_name, "last_name": last_name, "display_name": display,
                "quota_bytes": quota_bytes or None, "login_blocked": False, "kind": "user"}

    def delete_account(self, email: str) -> str:
        self.run("DELETEACCOUNT", self._q(email), timeout=DELETE_TIMEOUT_S)
        return "учётная запись удалена вместе с ящиком"

    # -- правила: автоответ и пересылка --------------------------------------------------------
    def _rules(self, email: str) -> List[List[Any]]:
        out = self.run("GETACCOUNTMAILRULES", self._q(email), data=True)
        return [r for r in out if isinstance(r, list) and len(r) >= 2] if isinstance(out, list) else []

    @staticmethod
    def _find(rules: List[List[Any]], name: str) -> Optional[List[Any]]:
        return next((r for r in rules if str(r[1]) == name), None)

    @staticmethod
    def _prio(rule: List[Any]) -> int:
        value = _number(rule[0]) if rule else None
        return value or 0

    @staticmethod
    def _actions(rule: Optional[List[Any]]) -> List[List[Any]]:
        if not rule or len(rule) < 4 or not isinstance(rule[3], list):
            return []
        return [a for a in rule[3] if isinstance(a, list) and a]

    def _targets(self, rule: Optional[List[Any]]) -> List[str]:
        if not rule or self._prio(rule) <= 0:
            return []
        out: List[str] = []
        for action in self._actions(rule):
            if str(action[0]) in ("Mirror to", "Redirect to", "Forward to") and len(action) > 1:
                out.extend(norm_email(a) for a in re.split(r"[\s,;]+", str(action[1])) if a.strip())
        return out

    def get_autoreply(self, email: str) -> Dict[str, Any]:
        rule = self._find(self._rules(email), VACATION)
        out = {"enabled": bool(rule and self._prio(rule) > 0), "subject": "", "text": "", "start": "", "end": ""}
        for action in self._actions(rule):
            if str(action[0]) == "Reply with" and len(action) > 1:
                reply = str(action[1]).replace("\r\n", "\n")
                if reply.startswith("+") and "\n\n" in reply:
                    head, reply = reply[1:].split("\n\n", 1)
                    for line in head.split("\n"):
                        if line.lower().startswith("subject:"):
                            out["subject"] = line.split(":", 1)[1].strip()
                out["text"] = reply
        for cond in (rule[2] if rule and len(rule) > 2 and isinstance(rule[2], list) else []):
            if isinstance(cond, list) and len(cond) >= 3 and str(cond[0]) == "Current Date":
                out["start" if str(cond[1]) == "greater than" else "end"] = _rule_day(cond[2])
        return out

    def set_autoreply(self, email: str, *, enabled: bool, subject: str = "", text: str = "",
                      start: str = "", end: str = "") -> str:
        q = self._q(email)
        if not enabled:
            self.run("UPDATEACCOUNTMAILRULE", q, "DELETE", quote(VACATION))
            return "автоответ выключен (правило «#Vacation» удалено)"
        subject = (subject or "").strip()
        if any(ch in subject for ch in "\r\n"):
            raise ValidationError("Тема автоответа — одной строкой.")
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            raise ValidationError("Текст автоответа пуст.")
        # «+» в начале — сначала заголовки ответа, после пустой строки — текст
        reply = f"+Subject: {subject or 'Re: ^S'}\nIn-Reply-To: ^I\n\n{body}"
        conditions: List[Any] = [["Human Generated", "---"], ["From", "not in", "#RepliedAddresses"]]
        if start:
            conditions.append(["Current Date", "greater than", rule_date(start)])
        if end:
            conditions.append(["Current Date", "less than", rule_date(end, True)])
        rule = ["2", VACATION, conditions, [["Reply with", reply], ["Remember 'From' in", "RepliedAddresses"]]]
        line = encode(rule)
        try:
            # список «уже ответили» — заново, чтобы новый автоответ получили все
            self.run("REMOVEACCOUNTSUBSET", q, "SUBSET", "RepliedAddresses")
        except ServerUnreachable:
            raise
        except ServerError:
            pass
        self.run("UPDATEACCOUNTMAILRULE", q, line)
        period = f" ({start or '…'} — {end or '…'}, по времени сервера)" if start or end else ""
        return f"автоответ включён{period} (правило «#Vacation»)"

    def get_forwarding(self, email: str) -> Dict[str, Any]:
        rule = self._find(self._rules(email), REDIRECT)
        return {"addresses": self._targets(rule),
                "keep_copy": not any(str(a[0]) == "Discard" for a in self._actions(rule))}

    def set_forwarding(self, email: str, *, addresses, keep_copy: bool = True) -> str:
        q = self._q(email)
        targets = [check_email(a, "Адрес пересылки") for a in addresses or []]
        if not targets:
            self.run("UPDATEACCOUNTMAILRULE", q, "DELETE", quote(REDIRECT))
            return "пересылка выключена (правило «#Redirect» удалено)"
        actions: List[Any] = [["Mirror to", ",".join(targets)]]
        if not keep_copy:
            actions.append(["Discard"])
        self.run("UPDATEACCOUNTMAILRULE", q, encode(["1", REDIRECT, [], actions]))
        return (f"пересылка на {', '.join(targets)} (правило «#Redirect»); " +
                ("копия остаётся в ящике" if keep_copy else "в ящике письма не остаются"))

    # -- IMAP и состояние ----------------------------------------------------------------------
    def imap_auth(self, email: str) -> ImapAuth:
        admin = str(self.setting("admin") or "").strip()
        password = self.secret("admin_password")
        if not admin or not password:
            raise ServerAuthError(f"{self.label}: не заданы администратор и его пароль.")
        return ImapAuth(mech="plain_authzid", user=admin, secret=password, authzid=norm_email(email),
                        host=self.imap_server()[0])

    def test(self) -> Status:
        st = Status()
        cli = self.cli
        version = self.run("GETVERSION", data=True)
        if not isinstance(version, str) or not version:
            m = re.search(r"CommuniGate Pro.*?(\d+(?:\.\d+)+)", cli.banner)
            version = m.group(1) if m else ""
        st.version = f"CommuniGate Pro {version}".strip()
        domains = self._domains()
        st.details.append("Домены: " + (", ".join(domains[:10]) + (" …" if len(domains) > 10 else "") or "нет"))
        total = sum(len(self._names(d)) for d in domains)
        st.details.append(f"Учётных записей: {total}")
        st.details.append("CLI: " + ("TLS" if self.use_tls else "без шифрования"))
        if not self.use_tls and not _is_loopback(cli.host):
            st.problems.append(("plain", "warning", "CLI без шифрования: команды и новые пароли учётных записей идут "
                                                    "по сети открытым текстом"))
        st.ok = not any(p[1] == "error" for p in st.problems)
        return st


DRIVER = CommuniGateDriver
