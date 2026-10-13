"""
Клиент CLI Axigen — текстового интерфейса администрирования (обычно порт 7000).

Протокол: после подключения сервер присылает приветствие и приглашение
``<login>``; клиент отвечает ``USER <логин>``, на приглашение ``<password>`` —
паролем. Дальше каждая команда — одна строка, ответ кончается строкой итога
(``+OK…`` или ``-ERR…``) и приглашением с текущим контекстом: ``<#>`` — корень,
``<domain#>`` — внутри ``UPDATE Domain``, ``<domain-account#>`` — внутри
``UPDATE Account`` и т. д. Из контекста выходят ``BACK`` (без сохранения),
``COMMIT`` (сохранить объект) или ``DONE`` (сохранить вложенный контекст).

Что важно для надёжности:

* конец ответа — приглашение в конце принятого текста; если сразу перед ним
  нет строки итога, ответ дочитывается, пока сервер молчит дольше
  ``QUIET_S`` (так кусок данных, случайно похожий на приглашение, не обрывает
  ответ);
* управляющие последовательности telnet (IAC) и цветовые коды вырезаются;
* значения подставляются только проверенные: имена доменов, учётных записей и
  папок — без кавычек, перевода строки и управляющих знаков (иначе одно поле
  могло бы дописать к команде вторую);
* пароль в журнале команд никогда не пишется — вместо него «••••».
"""
from __future__ import annotations

import re
import select
import socket
import time
import unicodedata
from typing import Callable, Iterable, List, Optional, Sequence

from ..logging_setup import get_logger
from .common import (AxigenAuthError, AxigenCommandError, AxigenConnectionError, AxigenError, AxigenSettings,
                     TlsPolicy, open_socket, require_encryption, wrap_tls)
from .parse import Reply, split_reply

log = get_logger("axigen.cli")

#: Сколько ждать продолжения ответа, если перед приглашением нет итога команды.
QUIET_S = 0.4
#: То же, когда сервер в этой сессии уже присылал итоги («+OK»/«-ERR»): строка,
#: похожая на приглашение, без итога перед ней — скорее данные (папка «<#>»),
#: чем конец ответа, поэтому ждём дольше.
QUIET_STATUSFUL_S = 3.0
#: Сколько ждать приветствия CLI сразу после подключения.
FIRST_REPLY_S = 20.0
#: Предел одного ответа (защита памяти от сервера, который не замолкает).
MAX_REPLY_BYTES = 64 * 1024 * 1024
#: Предел длины журнала одной сессии (символов) — длинные списки обрезаются.
MAX_TRANSCRIPT = 400_000

_PROMPT = re.compile(r"(?:^|\n)<([^<>\r\n]{0,120})>[ \t]*$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._@+\-=/]+$")
_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\"\\]")


def has_control(text: str) -> bool:
    """Управляющие знаки и разделители строк Unicode (в команду CLI их пускать нельзя)."""
    return any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text)
MASK = "••••"


def strip_telnet(data: bytes) -> bytes:
    """Вырезать управляющие последовательности telnet (IAC …) из принятых байтов."""
    if b"\xff" not in data:
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        if byte != 0xFF:
            out.append(byte)
            i += 1
            continue
        if i + 1 >= n:
            break
        cmd = data[i + 1]
        if cmd == 0xFF:                       # экранированный байт 255
            out.append(0xFF)
            i += 2
        elif cmd in (0xFB, 0xFC, 0xFD, 0xFE):  # WILL / WONT / DO / DONT + опция
            i += 3
        elif cmd == 0xFA:                       # подсогласование до IAC SE
            end = data.find(b"\xff\xf0", i + 2)
            i = n if end < 0 else end + 2
        else:
            i += 2
    return bytes(out)


def quote(value: str, what: str = "значение") -> str:
    """Значение для команды CLI: как есть, если безопасно, иначе в двойных кавычках.

    Кавычка, обратная косая черта, перевод строки и управляющие знаки не
    допускаются вовсе: экранирование в CLI Axigen не описано, а такие знаки
    позволили бы одному полю изменить смысл команды.
    """
    text = str(value if value is not None else "")
    if not text:
        raise AxigenError(f"Пустое {what} для команды Axigen.")
    if _FORBIDDEN.search(text) or has_control(text):
        # само значение в сообщение не попадает, если это пароль (сообщение пишется в журнал)
        shown = MASK if "пароль" in what.lower() else text[:60]
        raise AxigenError(f"{what.capitalize()} «{shown}» содержит недопустимые знаки "
                          f"(кавычки, «\\», перевод строки) — такую команду Axigen отправлять нельзя.")
    if len(text) > 255:
        raise AxigenError(f"{what.capitalize()} длиннее 255 знаков.")
    return text if _SAFE_VALUE.match(text) else f'"{text}"'


def quote_list(values: Iterable[str], what: str = "значение") -> str:
    """Список для ``SET … (a b "c d")``."""
    return "(" + " ".join(quote(v, what) for v in values) + ")"


class AxigenCLI:
    """Одна сессия CLI. Использовать как контекстный менеджер::

        with AxigenCLI.open(settings) as cli:
            reply = cli.run("LIST Domains")
    """

    def __init__(self, host: str, port: int = 7000, *, user: str = "admin", password: str = "",
                 tls: bool = False, policy: Optional[TlsPolicy] = None, timeout: float = 60.0,
                 on_line: Optional[Callable[[str, str], None]] = None) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.tls = bool(tls)
        self.policy = policy or TlsPolicy()
        self.timeout = float(timeout)
        self.sock = None
        self.prompt = ""
        self.banner = ""
        self.version = ""
        self.fingerprint = ""
        #: журнал сессии: строки «> команда» и ответы (пароли замаскированы)
        self.transcript: List[str] = []
        self._transcript_len = 0
        self._secrets: List[str] = [password] if password else []
        self._buffer = b""
        self.on_line = on_line
        self.commands = 0
        #: сервер присылает итоги команд («+OK»/«-ERR») — см. :data:`QUIET_STATUSFUL_S`
        self._statusful = False

    # -- открытие ---------------------------------------------------------------
    @classmethod
    def open(cls, st: AxigenSettings, *, on_line=None, login: bool = True) -> "AxigenCLI":
        st.require()
        require_encryption(st, st.cli_tls == "tls", "CLI Axigen", setting="CLI через SSL")
        cli = cls(st.host, st.cli_port, user=st.admin_user, password=st.admin_password,
                  tls=st.cli_tls == "tls", policy=st.cli_policy, timeout=st.timeout_s, on_line=on_line)
        cli.connect()
        if login:
            try:
                cli.login()
            except BaseException:
                cli.close()
                raise
        return cli

    def __enter__(self) -> "AxigenCLI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> str:
        what = f"CLI Axigen {self.host}:{self.port}"
        raw = open_socket(self.host, self.port, self.timeout, what)
        if self.tls:
            self.sock, self.fingerprint = wrap_tls(raw, self.host, self.policy, what)
        else:
            self.sock = raw
        self.sock.settimeout(self.timeout)
        try:
            text = self._read_reply(first=True, limit_s=min(self.timeout, FIRST_REPLY_S))
        except AxigenConnectionError as exc:
            self.close()
            raise AxigenConnectionError(
                f"{what}: сервер не прислал приглашение CLI Axigen ({exc.message}).",
                hint="Проверьте порт (по умолчанию 7000) и что на нём слушает CLI, а не другая служба. Если это "
                     "SSL-слушатель — включите «CLI через SSL»; если обычный — выключите.", cause=exc) from exc
        self.banner = text
        self._note("<", text)
        if self.prompt.lower() not in ("login", "password", "#") and not self.prompt.endswith("#"):
            self.close()
            raise AxigenConnectionError(
                f"{what}: сервер ответил не как CLI Axigen (приглашение «<{self.prompt}>»).",
                hint="Проверьте порт (по умолчанию 7000) и что на нём слушает CLI, а не другая служба.")
        return text

    def get_version(self) -> str:
        """``GET VERSION`` — работает и до входа (номер бывает и в строке итога)."""
        reply = self.run("GET VERSION", check=False)
        for line in list(reply.lines) + [reply.status]:
            if re.search(r"\d+\.\d+", line or ""):
                self.version = re.sub(r"^\s*(version\s*[:=]?\s*)", "", line.strip(), flags=re.I)[:120]
                break
        return self.version

    def login(self) -> None:
        if self.prompt.lower() == "#":
            return
        if self.prompt.lower() != "login":
            raise AxigenConnectionError(f"CLI Axigen: неожиданное приглашение «<{self.prompt}>» вместо «<login>».")
        if not self.version:
            try:
                self.get_version()
            except AxigenError:
                pass
        first = self.run(f"USER {quote(self.user, 'имя администратора')}", check=False)
        if first.ok is False:
            raise AxigenAuthError(f"CLI Axigen не принял имя администратора: {first.status or 'отказ'}.")
        if self.prompt.lower() != "password":
            raise AxigenAuthError(f"CLI Axigen не спросил пароль (приглашение «<{self.prompt}>»).")
        second = self._send_secret(self.password)
        if second.ok is False or self.prompt.lower() in ("login", "password"):
            raise AxigenAuthError(
                f"CLI Axigen не принял логин и пароль администратора «{self.user}»"
                + (f": {second.status}" if second.status else "") + ".",
                hint="Проверьте логин и пароль в «Настройки → Сервер Axigen». Подойдёт и отдельный "
                     "администратор Axigen с нужными правами (WebAdmin → Administrative Accounts).")

    # -- команды --------------------------------------------------------------
    def run(self, command: str, *, check: bool = True, strict: bool = False,
            secrets: Sequence[str] = ()) -> Reply:
        """Выполнить команду.

        ``check`` — бросить :class:`AxigenCommandError` на ``-ERR``; ``strict`` —
        и на ответ без итога: команда, которая меняет сервер, считается
        выполненной, только если сервер ответил «+OK».
        """
        if self.sock is None:
            raise AxigenConnectionError("CLI Axigen: соединение закрыто.")
        if "\n" in command or "\r" in command or has_control(command):
            raise AxigenError("Команда CLI не может содержать перевод строки и управляющие знаки.")
        for secret in secrets:
            if secret and secret not in self._secrets:
                self._secrets.append(secret)
        shown = self._mask(command)
        self._note(">", shown)
        try:
            self.sock.sendall(command.encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise AxigenConnectionError(f"CLI Axigen: связь оборвалась при отправке команды ({exc}).",
                                        cause=exc) from exc
        text = self._read_reply()
        self.commands += 1
        reply = self._make_reply(shown, text, command)
        if check and reply.ok is False:
            raise AxigenCommandError(
                f"Axigen отказал в команде «{shown}»: {reply.status or 'ошибка'}.",
                command=shown, reply=reply.status)
        if strict and reply.ok is not True:
            raise AxigenCommandError(
                f"Axigen не подтвердил команду «{shown}» (нет ответа «+OK») — изменение могло не примениться.",
                command=shown, reply=reply.status or "",
                hint="Проверьте учётную запись в WebAdmin и ответ сервера в журнале команд.")
        return reply

    def run_many(self, commands: Iterable[str], *, check: bool = True) -> List[Reply]:
        return [self.run(cmd, check=check) for cmd in commands]

    def _send_secret(self, secret: str) -> Reply:
        self._note(">", MASK)
        try:
            self.sock.sendall(str(secret).encode("utf-8") + b"\r\n")
        except OSError as exc:
            raise AxigenConnectionError(f"CLI Axigen: связь оборвалась ({exc}).", cause=exc) from exc
        text = self._read_reply()
        return self._make_reply(MASK, text, "")

    def _make_reply(self, shown: str, text: str, sent: str) -> Reply:
        body = text
        m = _PROMPT.search(body)
        if m:
            body = body[:m.start()]
        lines_text = body
        # эхо команды (если сервер его присылает) — не часть ответа
        first, _, rest = lines_text.lstrip("\r\n").partition("\n")
        if sent and first.strip() == sent.strip():
            lines_text = rest
        data, ok, status = split_reply(lines_text)
        reply = Reply(command=shown, lines=data, ok=ok, status=self._mask(status), prompt=self.prompt,
                      raw=self._mask(text))
        self._note("<", reply.raw)
        return reply

    # -- контексты ---------------------------------------------------------------
    @property
    def at_root(self) -> bool:
        return self.prompt == "#"

    def to_root(self) -> None:
        """Вернуться в корень без сохранения (BACK, пока приглашение не станет «#»)."""
        for _ in range(8):
            if self.at_root or not self.prompt:
                return
            self.run("BACK", check=False)
        if not self.at_root:
            raise AxigenError(f"CLI Axigen: не удалось вернуться в корень (приглашение «<{self.prompt}>»).")

    # -- закрытие ---------------------------------------------------------------
    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.sendall(b"QUIT\r\n")
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # -- журнал -------------------------------------------------------------------
    def _mask(self, text: str) -> str:
        out = text or ""
        for secret in self._secrets:
            if secret and len(secret) >= 1:
                out = out.replace(secret, MASK)
        return out

    def _note(self, direction: str, text: str) -> None:
        text = self._mask(text or "").replace("\r\n", "\n").rstrip("\n")
        if not text:
            return
        if self._transcript_len < MAX_TRANSCRIPT:
            entry = (f"{direction} {text}" if direction == ">" else text)
            self.transcript.append(entry[:MAX_TRANSCRIPT - self._transcript_len])
            self._transcript_len += len(entry)
            if self._transcript_len >= MAX_TRANSCRIPT:
                self.transcript.append("… (журнал сессии обрезан)")
        if self.on_line:
            try:
                self.on_line(direction, text)
            except Exception:  # noqa: BLE001
                pass

    def transcript_text(self) -> str:
        return "\n".join(self.transcript)

    # -- чтение ответа -----------------------------------------------------------
    def _read_reply(self, first: bool = False, limit_s: Optional[float] = None) -> str:
        """Читать до приглашения в конце ответа (см. описание модуля)."""
        buf = self._buffer
        self._buffer = b""
        limit = limit_s if limit_s is not None else self.timeout
        deadline = time.monotonic() + limit
        quiet_since: Optional[float] = None
        while True:
            text = self._decode(buf)
            m = _PROMPT.search(text)
            if m:
                before = text[:m.start()].rstrip()
                last = before.rsplit("\n", 1)[-1].strip() if before else ""
                prompt = m.group(1).strip()
                status = last.startswith(("+OK", "-ERR", "+ok", "-err"))
                complete = first or prompt.lower() in ("login", "password") or status
                if complete:
                    if status:
                        self._statusful = True
                    self.prompt = prompt
                    return text
                if quiet_since is None:
                    quiet_since = time.monotonic()
                if time.monotonic() - quiet_since >= (QUIET_STATUSFUL_S if self._statusful else QUIET_S):
                    self.prompt = prompt
                    return text
            else:
                quiet_since = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AxigenConnectionError(
                    f"CLI Axigen не закончил ответ за {int(limit)} с.",
                    hint="Сервер перегружен или команда слишком долгая; увеличьте «Таймаут» в настройках Axigen.")
            wait = min(remaining, QUIET_S if m else remaining)
            try:
                ready, _, _ = select.select([self.sock], [], [], wait)
            except (OSError, ValueError) as exc:
                raise AxigenConnectionError(f"CLI Axigen: связь оборвалась ({exc}).", cause=exc) from exc
            if not ready and not getattr(self.sock, "pending", lambda: 0)():
                continue
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout as exc:
                raise AxigenConnectionError(f"CLI Axigen не ответил за {int(self.timeout)} с.", cause=exc) from exc
            except OSError as exc:
                raise AxigenConnectionError(f"CLI Axigen: связь оборвалась ({exc}).", cause=exc) from exc
            if not chunk:
                text = self._decode(buf)
                if text.strip():
                    self._note("<", text)
                raise AxigenConnectionError("CLI Axigen закрыл соединение"
                                            + (f": {text.strip()[-200:]}" if text.strip() else "."))
            buf += strip_telnet(chunk)
            if len(buf) > MAX_REPLY_BYTES:
                raise AxigenError(f"CLI Axigen прислал ответ больше {MAX_REPLY_BYTES // (1024 * 1024)} МБ — прервано.")

    @staticmethod
    def _decode(buf: bytes) -> str:
        text = buf.decode("utf-8", "replace")
        return _ANSI.sub("", text).replace("\r\n", "\n")


def session(svc_or_settings, *, on_line=None) -> AxigenCLI:
    """Открыть сессию CLI по настройкам службы (или готовым :class:`AxigenSettings`)."""
    from .common import load_settings
    st = svc_or_settings if isinstance(svc_or_settings, AxigenSettings) else load_settings(svc_or_settings)
    return AxigenCLI.open(st, on_line=on_line)
