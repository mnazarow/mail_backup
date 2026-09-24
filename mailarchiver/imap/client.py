"""
Обёртка над IMAPClient с надёжной обработкой ошибок, таймаутами, повторными
подключениями, поддержкой SSL/STARTTLS/без шифрования, входом по паролю и по
OAuth2 (XOAUTH2).
"""
from __future__ import annotations

import imaplib
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Set

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from ..errors import (
    ImapAuthError,
    ImapConnectionError,
    ImapProtocolError,
    ImapTimeoutError,
    MailArchiverError,
)
from ..logging_setup import get_logger
from ..models import Account, AuthType, Security
from .oauth import refresh_access_token

log = get_logger("imap")

# imaplib отвергает строки ответа длиннее _MAXLINE (1 МБ). Ответ `* SEARCH 1 2 3 …`
# приходит ОДНОЙ строкой, и на папке примерно от 125–160 тысяч писем он
# длиннее: раньше это роняло копирование всего ящика («got more than 1000000
# bytes»). Поднимаем предел; сам поиск на больших папках к тому же идёт
# диапазонами UID (см. ImapConnection.search_uids).
_MAXLINE_WANTED = 256 * 1024 * 1024
if getattr(imaplib, "_MAXLINE", 0) < _MAXLINE_WANTED:
    imaplib._MAXLINE = _MAXLINE_WANTED  # noqa: SLF001

#: Папки больше этого числа писем ищутся диапазонами UID, а не одним SEARCH ALL.
SEARCH_RANGE_THRESHOLD = 100_000
#: На сколько диапазонов делить пространство UID (не меньше 100 000 UID в диапазоне).
SEARCH_RANGE_PARTS = 50

#: Коды ответа LOGIN, означающие временный отказ (RFC 5530): стоит повторить позже.
_TEMPORARY_LOGIN_CODES = ("[unavailable]", "[inuse]", "[limit]", "[serverbug]", "[contactadmin]")

# Целевой СУММАРНЫЙ объём одной порции FETCH (байты). Порция набирается по
# размеру писем, а не по их количеству: батч из 200 писем с вложениями по
# 20-30 МБ забирал бы в память несколько гигабайт за один запрос (OOM).
FETCH_CHUNK_TARGET_BYTES = 64 * 1024 * 1024
# Сколько UID спрашивать за один запрос размеров: ответ на (RFC822.SIZE)
# крошечный, поэтому порция здесь заметно крупнее порции загрузки. Но не
# больше 1000: IMAPClient перечисляет номера через запятую, и строка команды
# на 1000 семизначных UID — около 8 КБ (столько советует не превышать RFC 7162;
# часть серверов длинные команды отвергает).
SIZE_PROBE_BATCH_SIZE = 1000
# Во что оценивать письмо, размер которого сервер не сообщил. Нужно только
# для набора порции: без оценки такие письма считались бы «нулевыми» и порция
# опять набиралась бы одним лишь количеством.
UNKNOWN_SIZE_ASSUMPTION = 256 * 1024

# Признаки ответа «папка уже существует». Подстрока «exist» для этого не
# годится: ответ вида `NO [TRYCREATE] Mailbox doesn't exist` — это ОТКАЗ.
_ALREADY_EXISTS_MARKERS = ("alreadyexists", "already exist", "duplicate folder", "duplicate mailbox")

# Флаги LIST, означающие «эту папку открыть нельзя» (контейнер или уже
# несуществующая запись). Сравниваем В НИЖНЕМ РЕГИСТРЕ: серверы пишут флаги
# по-разному (\Noselect, \NoSelect, \NOSELECT), а точное сравнение молча
# пропускало бы такие папки в бэкап — и каждая давала бы «ошибку» SELECT.
_UNSELECTABLE_FLAGS = ("\\noselect", "\\nonexistent")

# Сколько ВСЕГО попыток открыть папку и пауза между ними. Часть отказов
# SELECT/EXAMINE временные: ящик занят другой сессией, сервер держит блокировку,
# кратковременная перегрузка. Одна повторная попытка дешевле, чем потерянная
# из копии папка. Значения модульные — их подменяют тесты.
SELECT_ATTEMPTS = 2
SELECT_RETRY_DELAY_S = 1.5

# Общая формулировка библиотеки вокруг ответа сервера: imapclient формирует
# сообщение как "<команда> failed: <ответ сервера>". Разворачиваем её, чтобы
# в лог попали именно слова сервера.
_LIB_WRAPPER_RE = re.compile(r"^\s*(?:[a-z_]+ failed|[A-Z]+ command error):\s*(?P<reply>.+)$", re.S)

# Untagged-строки, в которых серверы объясняют отказ.
_SERVER_NOTICE_KEYS = ("NO", "BAD", "ALERT", "BYE")


@dataclass
class ConnectOptions:
    connect_timeout_s: int = 30
    socket_timeout_s: int = 120
    verify_ssl: bool = True
    fetch_batch_size: int = 200
    #: вызывается, когда сервер OAuth2 выдал новый refresh-токен: (ящик, токен)
    on_refresh_token: Optional[Callable] = None
    #: вход администратора почты в чужой ящик (ящики с auth_type=master):
    #: {"host", "user", "password", "mode": sasl_plain|separator, "separator"}
    master: Optional[dict] = None


@dataclass
class FolderInfo:
    name: str
    delimiter: str = "/"
    flags: List[str] = field(default_factory=list)
    selectable: bool = True


def _map_exception(exc: BaseException) -> Exception:
    """Привести низкоуровневую ошибку к нашему типу с понятным сообщением."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ImapTimeoutError("Превышено время ожидания IMAP-сервера.",
                                hint="Увеличьте backup.socket_timeout_s или проверьте сеть/файрвол.", cause=exc)
    if isinstance(exc, ssl.SSLError):
        return ImapConnectionError(f"Ошибка TLS/SSL: {exc}",
                                   hint="Проверьте порт и режим шифрования (SSL vs STARTTLS) и валидность сертификата.",
                                   cause=exc)
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)):
        return ImapConnectionError(f"Не удалось соединиться с IMAP-сервером: {exc}",
                                   hint="Проверьте адрес хоста, порт и доступность сервера.", cause=exc)
    if isinstance(exc, LoginError):
        return ImapAuthError("Аутентификация IMAP не удалась (неверные логин/пароль или требуется пароль приложения).",
                             hint="Для Gmail/Mail.ru/Яндекс включите доступ по IMAP и используйте пароль приложения, либо OAuth2.",
                             cause=exc)
    if isinstance(exc, imaplib.IMAP4.abort):
        # Сервер закрыл соединение (EOF, «* BYE», сброс). imaplib сообщает это
        # подклассом IMAP4.error, и раньше обрыв превращался в «ошибку
        # протокола» — то есть в отказ КОНКРЕТНОЙ папки: исправные папки
        # копили неудачи, а повтор задания не срабатывал.
        return ImapConnectionError(f"Соединение с IMAP-сервером прервано: {_server_reply(exc) or exc}",
                                   hint="Сервер закрыл сеанс. Если это повторяется, проверьте нагрузку "
                                        "и лимиты сеансов на почтовом сервере.", cause=exc)
    if isinstance(exc, imaplib.IMAP4.error):
        text = str(exc).lower()
        if "auth" in text or "login" in text or "credential" in text:
            return ImapAuthError(f"IMAP-сервер отклонил аутентификацию: {exc}", cause=exc)
        return ImapProtocolError(f"Ошибка протокола IMAP: {exc}", cause=exc)
    return ImapProtocolError(f"Неожиданная ошибка IMAP: {exc}", cause=exc)


def _is_already_exists_error(exc: BaseException) -> bool:
    """
    Отличить ответ «папка уже существует» от любого другого отказа CREATE.

    Опираемся на код ответа ALREADYEXISTS (RFC 5530) и явные формулировки
    серверов. Ответы вроде `NO [TRYCREATE] Mailbox doesn't exist` под это
    условие НЕ подпадают — глушить их нельзя, иначе все последующие APPEND в
    эту папку падают без внятной причины.
    """
    text = str(exc).lower()
    return any(marker in text for marker in _ALREADY_EXISTS_MARKERS)


_BYTES_REPR_RE = re.compile(r"""^b(['"])(?P<body>.*)\1$""", re.S)


def _as_text(value) -> str:
    """Привести кусок ответа сервера (bytes/str/что угодно) к строке.

    IMAPClient кладёт в текст ошибки входа ``str(bytes)`` — «b'[ALERT] …'»;
    такую обёртку снимаем, чтобы администратор видел слова сервера как есть.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    text = str(value)
    match = _BYTES_REPR_RE.match(text.strip())
    if match:
        body = match.group("body")
        try:
            import ast
            decoded = ast.literal_eval("b" + match.group(1) + body + match.group(1))
            return decoded.decode("utf-8", "replace")
        except (ValueError, SyntaxError):
            return body
    return text


def _server_reply(exc: BaseException) -> str:
    """
    Достать ФАКТИЧЕСКИЙ ответ сервера из исключения imapclient/imaplib.

    Текст ответа лежит в args исключения (иногда как bytes), обычно завёрнутый
    в общую формулировку библиотеки вида «select failed: <ответ>». Её мы
    разворачиваем: по «select failed» администратор не поймёт ничего, а по
    словам сервера — поймёт, отказано ли в доступе, занята ли папка или её нет.
    """
    parts: List[str] = []
    for arg in getattr(exc, "args", ()) or ():
        text = _as_text(arg).strip()
        if not text:
            continue
        match = _LIB_WRAPPER_RE.match(text)
        if match:
            text = match.group("reply").strip()
        if text and text not in parts:
            parts.append(text)
    return "; ".join(parts)


def _server_notices(client) -> str:
    """
    Незапрошенные (untagged) строки последнего обмена: `* NO …`, `* BAD …`,
    `[ALERT] …`, `* BYE …`.

    Часть серверов (в том числе Axigen) отдаёт в теге короткое «failed», а
    причину пишет именно в untagged-строке. Best-effort: на другой версии
    imaplib этой структуры может не быть — тогда просто вернём пустую строку.
    """
    try:
        responses = client._imap.untagged_responses  # noqa: SLF001
        out: List[str] = []
        for key in _SERVER_NOTICE_KEYS:
            for value in (responses.get(key) or ()):
                text = _as_text(value).strip()
                if text:
                    out.append(f"{key}: {text}")
    except Exception:  # noqa: BLE001
        return ""
    return "; ".join(out)


def header_value(raw_header, name: str) -> str:
    """Значение заголовка из блока заголовков, со склейкой свёрнутых строк.

    ``Message-ID:\r\n <id@host>`` — законная запись (RFC 5322 «folding»):
    раньше в таком случае значение получалось пустым, и проверка дублей при
    восстановлении для таких писем не работала.
    """
    if not raw_header:
        return ""
    if isinstance(raw_header, (bytes, bytearray)):
        text = bytes(raw_header).decode("latin-1", "ignore")
    else:
        text = str(raw_header)
    want = name.lower() + ":"
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            break                      # конец блока заголовков
        if line.lower().startswith(want):
            value = line.split(":", 1)[1]
            j = i + 1
            while j < len(lines) and lines[j][:1] in (" ", "\t"):
                value += " " + lines[j].strip()
                j += 1
            return value.strip()
    return ""


def _header_message_id(raw_header) -> str:
    """Достать значение Message-ID из куска заголовков, отданного сервером."""
    return header_value(raw_header, "Message-ID")


def uid_set(uids) -> str:
    """Свернуть список UID в компактный набор IMAP: [1,2,3,5,7,8] → «1:3,5,7:8».

    Для SEARCH: там IMAPClient передаёт набор как есть, а перечисление номеров
    через запятую занимало бы килобайты одной строкой. (В FETCH так нельзя:
    IMAPClient сверяет ответ со списком номеров и строку-набор не понимает.)
    """
    ordered = sorted(set(int(u) for u in uids))
    parts: List[str] = []
    i = 0
    while i < len(ordered):
        start = end = ordered[i]
        while i + 1 < len(ordered) and ordered[i + 1] == end + 1:
            i += 1
            end = ordered[i]
        parts.append(str(start) if start == end else f"{start}:{end}")
        i += 1
    return ",".join(parts)


def folder_matches(name: str, patterns, delimiter: str) -> bool:
    """Подходит ли папка под шаблоны include/exclude.

    Шаблон «Архив» задаёт и саму папку, и всё, что вложено в неё («Архив/2020»).
    Регистр не важен. Одна и та же функция используется копированием и
    диагностикой папок — иначе диагностика показывала бы «потерянными» папки,
    которые копирование намеренно пропускает.
    """
    delimiter = delimiter or "/"
    low = name.lower()
    for p in patterns or ():
        p = str(p or "").strip()
        if not p:
            continue
        pl = p.lower()
        if low == pl or low.startswith(pl + delimiter.lower()):
            return True
    return False


def _plan_size_chunks(uids: List[int], sizes: Dict[int, int], max_count: int,
                      target_bytes: int) -> Iterator[List[int]]:
    """
    Разбить UID на порции по СУММАРНОМУ размеру писем.

    Порция закрывается, когда добавление следующего письма перевалило бы за
    ``target_bytes``, либо когда в ней уже ``max_count`` писем. Письмо, которое
    само больше целевого объёма, из-за этого попадает в порцию в одиночку.
    """
    chunk: List[int] = []
    chunk_bytes = 0
    for uid in uids:
        size = int(sizes.get(uid) or UNKNOWN_SIZE_ASSUMPTION)
        if chunk and (len(chunk) >= max_count or chunk_bytes + size > target_bytes):
            yield chunk
            chunk, chunk_bytes = [], 0
        chunk.append(uid)
        chunk_bytes += size
    if chunk:
        yield chunk


class ImapConnection:
    """Одно соединение с IMAP-ящиком. Используйте как контекстный менеджер."""

    def __init__(self, account: Account, options: Optional[ConnectOptions] = None) -> None:
        self.account = account
        self.opt = options or ConnectOptions()
        self.client: Optional[IMAPClient] = None
        self._delimiter: str = "/"
        #: имена папок, которые сервер прислал в неверной кодировке (пропущены)
        self.bad_folder_names: List[str] = []

    # -- контекстный менеджер -----------------------------------------------
    def __enter__(self) -> "ImapConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Если выходим из-за ошибки, соединение может быть уже мёртвым —
        # не тратим время на logout по такому сокету.
        self.close(force=exc_type is not None)

    # -- подключение ---------------------------------------------------------
    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.opt.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _timeout(self):
        """
        Таймауты для самого IMAPClient. Глобальный socket.setdefaulttimeout()
        использовать нельзя: он меняет таймаут для всего процесса (SMTP, OAuth,
        HTTP), а подключаемся мы из рабочих потоков.
        """
        try:
            from imapclient import SocketTimeout  # есть не во всех версиях
            return SocketTimeout(self.opt.connect_timeout_s, self.opt.socket_timeout_s)
        except ImportError:  # старая/другая версия imapclient — один общий таймаут
            return self.opt.socket_timeout_s

    def connect(self) -> None:
        acc = self.account
        timeout = self._timeout()
        try:
            if acc.security == Security.SSL:
                client = IMAPClient(acc.host, port=acc.port or 993, ssl=True,
                                    ssl_context=self._ssl_context(), timeout=timeout)
            else:
                client = IMAPClient(acc.host, port=acc.port or 143, ssl=False, timeout=timeout)
            # Запоминаем клиента СРАЗУ после конструктора: если STARTTLS (или
            # логин) упадёт, close() всё равно закроет уже открытый сокет.
            self.client = client
            # Даты — с часовым поясом. По умолчанию IMAPClient переводит их в
            # «наивное» местное время текущего смещения, а .timestamp() потом
            # применяет правила пояса НА ДАТУ ПИСЬМА: письма из периода с другим
            # смещением (летнее время, Москва 2011–2014) сдвигались на час.
            try:
                client.normalise_times = False
            except Exception:  # noqa: BLE001
                pass
            if acc.security == Security.STARTTLS:
                client.starttls(self._ssl_context())
            self._login()
            log.info("Подключение к ящику «%s» (%s:%s) установлено", acc.name, acc.host, acc.port)
        except (LoginError, ImapAuthError):
            self.close()
            raise
        except MailArchiverError:
            # уже понятная ошибка (временный отказ входа, сбой сервера токенов
            # OAuth2) — пробрасываем как есть, не превращая в «неожиданную»
            self.close(force=True)
            raise
        except Exception as exc:  # noqa: BLE001
            # ошибка уровня сети/TLS — сокет наверняка непригоден, рвём сразу
            self.close(force=True)
            raise _map_exception(exc) from exc

    def _login(self) -> None:
        acc = self.account
        assert self.client is not None
        try:
            if acc.auth_type == AuthType.OAUTH2:
                access, _exp, new_refresh = refresh_access_token(
                    acc.oauth_token_url, acc.oauth_client_id, acc.oauth_client_secret,
                    acc.oauth_refresh_token, timeout=self.opt.connect_timeout_s,
                    with_refresh=True,
                )
                if new_refresh and new_refresh != acc.oauth_refresh_token:
                    # Microsoft 365 выдаёт новый refresh-токен при каждом обновлении,
                    # а старый со временем истекает: без сохранения копирование через
                    # несколько месяцев переставало бы входить в ящик.
                    acc.oauth_refresh_token = new_refresh
                    if self.opt.on_refresh_token is not None:
                        try:
                            self.opt.on_refresh_token(acc, new_refresh)
                        except Exception as cb_exc:  # noqa: BLE001
                            log.warning("Не удалось сохранить новый refresh-токен ящика «%s»: %s",
                                        acc.name, cb_exc)
                self.client.oauth2_login(acc.username, access)
            elif acc.auth_type == AuthType.MASTER:
                self._master_login()
            else:
                self.client.login(acc.username, acc.password)
        except LoginError as exc:
            if acc.auth_type == AuthType.MASTER:
                reply = _server_reply(exc)
                text = "Вход через учётную запись администратора почты отклонён сервером."
                if reply:
                    text += f" Ответ сервера: «{reply}»."
                raise ImapAuthError(
                    text,
                    hint="Проверьте логин и пароль администратора почты и способ входа («Настройки → Вход "
                         "через администратора почты»). Сервер должен разрешать администратору вход в чужие "
                         "ящики (Dovecot master users, SASL PLAIN с authzid в Cyrus и Zimbra). Axigen такой вход "
                         "не документирует — проверьте кнопкой «Проверить вход администратора».",
                    cause=exc,
                ) from exc
            reply = _server_reply(exc)
            low = reply.lower()
            if any(code in low for code in _TEMPORARY_LOGIN_CODES):
                # Временный отказ («слишком много сеансов», сервер перегружен)
                # — не повод проваливать ночное задание без повтора.
                raise ImapConnectionError(
                    f"Сервер временно не пускает в ящик: {reply}",
                    hint="Это временный отказ сервера (лимит сеансов, перегрузка); задание повторится.",
                    cause=exc,
                ) from exc
            text = "Не удалось войти в почтовый ящик: сервер отклонил учётные данные."
            if reply:
                text += f" Ответ сервера: «{reply}»."
            raise ImapAuthError(
                text,
                hint="Проверьте логин/пароль. Возможно, нужен «пароль приложения» или включение IMAP в настройках почты. "
                     "Если в ответе сервера сказано, что учётная запись отключена или пароль устарел, — это "
                     "решается на почтовом сервере.",
                cause=exc,
            ) from exc

    def _master_login(self) -> None:
        """Войти в ящик учётной записью администратора почты."""
        acc = self.account
        master = self.opt.master or {}
        if not master.get("user") or not master.get("password"):
            raise ImapAuthError(
                f"Ящик «{acc.name}» копируется входом администратора почты, а этот вход не настроен.",
                hint="Заполните «Настройки → Вход через администратора почты» или задайте ящику пароль.")
        allowed = str(master.get("host") or "").strip().lower()
        if not allowed or allowed != (acc.host or "").strip().lower():
            # Пароль администратора почты отправляется ТОЛЬКО на её собственный сервер:
            # ящик с опечаткой (или злонамеренно изменённым) адресом его не получит.
            raise ImapAuthError(
                f"Вход администратора разрешён только для сервера «{master.get('host') or '—'}», "
                f"а у ящика «{acc.name}» указан «{acc.host}».",
                hint="Исправьте сервер ящика или адрес сервера в настройках входа администратора.")
        if str(master.get("mode") or "sasl_plain") == "separator":
            sep = str(master.get("separator") or "*")
            self.client.login(f"{acc.username}{sep}{master['user']}", master["password"])
        else:
            self.client.plain_login(master["user"], master["password"],
                                    authorization_identity=acc.username)

    _CLOSE_TIMEOUT_S = 5

    def close(self, *, force: bool = False) -> None:
        """
        Закрыть соединение.

        При force=True (или при любой ошибке logout) сокет рвётся сразу через
        shutdown(), без ожидания ответа сервера. Иначе на разорванном соединении
        logout() висел бы до socket_timeout_s (до 120 с) при каждом обрыве —
        поэтому перед logout мы ещё и укорачиваем таймаут сокета.
        """
        client = self.client
        self.client = None
        if client is None:
            return
        if not force:
            self._shorten_socket_timeout(client)
            try:
                client.logout()
                return
            except Exception:  # noqa: BLE001
                pass  # сервер не ответил или сокет мёртв — закрываем принудительно
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def _shorten_socket_timeout(cls, client) -> None:
        """Ограничить ожидание ответа на LOGOUT (best-effort, зависит от версии)."""
        try:
            sock = client._imap.sock  # noqa: SLF001
            if sock is not None:
                sock.settimeout(cls._CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass

    # -- операции ------------------------------------------------------------
    def capabilities(self) -> List[str]:
        try:
            return [c.decode() if isinstance(c, bytes) else str(c) for c in self.client.capabilities()]
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def list_folders(self) -> List[FolderInfo]:
        try:
            raw = self.client.list_folders()
        except UnicodeError:
            # Сервер прислал имя в неверной кодировке IMAP UTF-7 (например «R&D»
            # без экранирования «&»). IMAPClient декодирует список целиком, и
            # одно такое имя роняло копирование ВСЕГО ящика. Берём список без
            # декодирования и разбираем имена по одному.
            raw = self._list_folders_tolerant()
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        result: List[FolderInfo] = []
        for flags, delimiter, name in raw:
            deli = _as_text(delimiter) if delimiter else ""
            self._delimiter = deli or self._delimiter
            flag_list = [_as_text(f) for f in (flags or ())]
            # Регистр флага значения не имеет: \Noselect, \NoSelect и \NOSELECT —
            # один и тот же запрет открывать папку.
            selectable = not any(f.strip().lower() in _UNSELECTABLE_FLAGS for f in flag_list)
            result.append(FolderInfo(name=_as_text(name), delimiter=deli or "/",
                                     flags=flag_list, selectable=selectable))
        return result

    def _list_folders_tolerant(self):
        """LIST без общего декодирования: плохие имена помечаются, а не роняют всё."""
        from imapclient import imap_utf7
        client = self.client
        prev = getattr(client, "folder_encode", True)
        try:
            client.folder_encode = False
            raw = client.list_folders()
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        finally:
            try:
                client.folder_encode = prev
            except Exception:  # noqa: BLE001
                pass
        out = []
        for flags, delimiter, name in raw:
            name_bytes = name if isinstance(name, (bytes, bytearray)) else str(name).encode("utf-8", "replace")
            try:
                decoded = imap_utf7.decode(bytes(name_bytes))
            except Exception:  # noqa: BLE001
                shown = bytes(name_bytes).decode("utf-8", "replace")
                log.error("Сервер вернул папку с именем в неверной кодировке IMAP UTF-7: %r — "
                          "папка пропущена (её нельзя открыть по имени).", shown)
                self.bad_folder_names.append(shown)
                continue
            out.append((flags, delimiter, decoded))
        return out

    @property
    def delimiter(self) -> str:
        return self._delimiter

    def select(self, folder: str, readonly: bool = True) -> Dict[str, int]:
        r"""
        Открыть папку: EXAMINE при readonly, иначе SELECT.

        Делаем до ``SELECT_ATTEMPTS`` попыток с паузой: часть отказов временная
        (ящик занят другой сессией, блокировка, кратковременная перегрузка).
        Если EXAMINE так и не прошёл, пробуем ОДИН раз обычный SELECT: часть
        серверов (замечено на Axigen) отдаёт «failed EXAMINE» на папку, которую
        при этом нормально открывает на запись. Читать это безопасно — письма
        всё равно скачиваются через ``BODY.PEEK``, флаг ``\Seen`` не ставится.
        Если открыть не удалось ничем — в сообщение кладём ФАКТИЧЕСКИЙ ответ
        сервера, его untagged-уведомления и данные STATUS (сколько писем сервер
        видит в этой папке): без них в журнале остаётся лишь общий текст
        библиотеки («select failed: …»), по которому нельзя понять ни причину,
        ни того, потеряно ли вообще что-нибудь.
        """
        attempts = max(1, int(SELECT_ATTEMPTS))
        attempt = 0
        while True:
            attempt += 1
            try:
                info = self.client.select_folder(folder, readonly=readonly)
            except Exception as exc:  # noqa: BLE001
                mapped = _map_exception(exc)
                if isinstance(mapped, (ImapConnectionError, ImapTimeoutError)):
                    # Связь потеряна — это не беда папки: повторять SELECT и
                    # собирать диагностику по мёртвому соединению бессмысленно.
                    raise mapped from exc
                if attempt >= attempts:
                    fallback_error = None
                    if readonly:
                        fallback, fallback_error = self._select_readwrite_fallback(folder)
                        if fallback is not None:
                            return fallback
                    raise self._select_error(folder, exc, attempt, fallback_error) from exc
                log.warning("Папка «%s» не открылась (%s). Повтор попытки %d из %d через %.1f с…",
                            folder, _server_reply(exc) or exc, attempt + 1, attempts, SELECT_RETRY_DELAY_S)
                time.sleep(max(0.0, float(SELECT_RETRY_DELAY_S)))
                continue
            return {
                "uidvalidity": int(info.get(b"UIDVALIDITY", 0) or 0),
                "uidnext": int(info.get(b"UIDNEXT", 0) or 0),
                "exists": int(info.get(b"EXISTS", 0) or 0),
            }

    def _select_readwrite_fallback(self, folder: str):
        """Последняя попытка открыть папку обычным SELECT вместо EXAMINE.

        :returns: ``(данные папки, None)`` при успехе либо ``(None, текст
            отказа)`` — отказ нужен отчёту об ошибке: администратор должен
            видеть, что пробовали и этот путь тоже.
        """
        try:
            info = self.client.select_folder(folder, readonly=False)
        except Exception as exc:  # noqa: BLE001
            reply = _server_reply(exc) or str(exc)
            log.debug("Папка «%s» не открылась и обычным SELECT: %s", folder, reply)
            return None, reply
        log.warning("Папка «%s» не открылась по EXAMINE, но открылась обычным SELECT — "
                    "читаем её так (письма скачиваются через BODY.PEEK, флаги не меняются).", folder)
        return {
            "uidvalidity": int(info.get(b"UIDVALIDITY", 0) or 0),
            "uidnext": int(info.get(b"UIDNEXT", 0) or 0),
            "exists": int(info.get(b"EXISTS", 0) or 0),
        }, None

    def folder_status(self, folder: str) -> Optional[Dict[str, int]]:
        """Спросить о папке командой STATUS, не открывая её.

        STATUS часто отвечает по папке, которую сервер отказывается открыть, —
        и тогда сразу видно главное: есть ли в ней письма. Папка с нулём писем,
        которая не открывается, копию неполной не делает.

        :returns: словарь с ключами ``messages``, ``uidnext``, ``uidvalidity``
            либо ``None``, если сервер не ответил и на STATUS. Диагностика не
            должна ронять копирование, поэтому исключения не пробрасываются.
        """
        try:
            raw = self.client.folder_status(folder, [b"MESSAGES", b"UIDNEXT", b"UIDVALIDITY"])
        except Exception as exc:  # noqa: BLE001
            log.debug("STATUS по папке «%s» не прошёл: %s", folder, exc)
            return None
        data = {_as_text(k).upper(): v for k, v in (raw or {}).items()}

        def _int(key: str) -> int:
            try:
                return int(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return {"messages": _int("MESSAGES"), "uidnext": _int("UIDNEXT"),
                "uidvalidity": _int("UIDVALIDITY")}

    # Символы, которые в имени папки почти всегда означают беду: их не видно
    # глазом, но сервер считает такое имя ДРУГИМ именем. Ради них и печатается
    # имя папки посимвольно, когда открыть её не удаётся ничем.
    _INVISIBLE_CHARS = {
        " ": "NBSP (неразрывный пробел)",
        " ": "figure space",
        "​": "zero-width space",
        "‌": "zero-width non-joiner",
        "‍": "zero-width joiner",
        " ": "line separator",
        " ": "paragraph separator",
        "﻿": "BOM",
    }

    def _name_warnings(self, folder: str) -> List[str]:
        """Что не так с самим ИМЕНЕМ папки (невидимые символы, пробелы по краям)."""
        problems: List[str] = []
        for char, title in self._INVISIBLE_CHARS.items():
            if char in folder:
                problems.append(f"невидимый символ {title} (U+{ord(char):04X})")
        if any(ord(c) < 0x20 for c in folder):
            problems.append("управляющий символ")
        if folder != folder.strip():
            problems.append("пробел в начале или в конце имени")
        return problems

    def _folder_is_listed(self, folder: str) -> Optional[bool]:
        """Показывает ли сервер эту папку, если спросить её ИМЕНЕМ ЦЕЛИКОМ.

        Решающая проверка: если LIST по точному имени папку не находит, значит
        сервер считает, что такого имени у него нет, — мы шлём не то имя
        (кодировка, невидимый символ, регистр). Если находит, а открыть не даёт
        — папка битая на самой почтовой системе, и чинить надо там.

        :returns: True/False, либо None — проверку выполнить не удалось.
        """
        if "*" in folder or "%" in folder:   # спецсимволы LIST: точной проверки не выйдет
            return None
        try:
            raw = self.client.list_folders(directory="", pattern=folder)
        except Exception as exc:  # noqa: BLE001
            log.debug("LIST по точному имени «%s» не прошёл: %s", folder, exc)
            return None
        for _flags, _deli, name in raw or ():
            if _as_text(name) == folder:
                return True
        return False

    def folder_children(self, folder: str, delimiter: str = "") -> List[str]:
        r"""Вложенные папки этой папки (по данным LIST).

        Папка, у которой есть вложенные, но которая сама не открывается, —
        это КОНТЕЙНЕР: своих писем она не хранит, а её содержимое лежит во
        вложенных папках, и они копируются отдельно. Некоторые серверы
        забывают пометить такую папку флагом ``\Noselect``, и без этой
        проверки она выглядела бы как потеря писем.
        """
        if "*" in folder or "%" in folder:
            return []
        delim = delimiter or self.delimiter or "/"
        try:
            raw = self.client.list_folders(directory="", pattern=f"{folder}{delim}*")
        except Exception as exc:  # noqa: BLE001
            log.debug("LIST вложенных папок «%s» не прошёл: %s", folder, exc)
            return []
        names = []
        for _flags, _deli, name in raw or ():
            text = _as_text(name)
            if text != folder:
                names.append(text)
        return names

    def probe_select(self, folder: str):
        """Одна попытка открыть папку — для диагностики, без повторов и пауз.

        :returns: ``(данные папки, None)`` либо ``(None, текст отказа)``.
        """
        try:
            info = self.client.select_folder(folder, readonly=True)
        except Exception as exc:  # noqa: BLE001
            mapped = _map_exception(exc)
            if isinstance(mapped, (ImapConnectionError, ImapTimeoutError)):
                # связь потеряна — дальше каждая папка «не открывалась» бы
                raise mapped from exc
            return None, (_server_reply(exc) or str(exc))
        return {
            "uidvalidity": int(info.get(b"UIDVALIDITY", 0) or 0),
            "uidnext": int(info.get(b"UIDNEXT", 0) or 0),
            "exists": int(info.get(b"EXISTS", 0) or 0),
        }, None

    def _folder_facts(self, folder: str) -> dict:
        """Собрать факты о папке, которую не удалось открыть.

        Всё, что можно спросить у сервера, НЕ открывая папку: STATUS (сколько
        писем), LIST по точному имени (знает ли сервер такое имя), вложенные
        папки и разбор самого имени. По этим фактам строятся и короткая строка
        для журнала задания, и подробное объяснение.
        """
        status = self.folder_status(folder)
        return {
            "status_messages": status["messages"] if status is not None else None,
            "listed": self._folder_is_listed(folder),
            "children": self.folder_children(folder),
            "name_warnings": self._name_warnings(folder),
        }

    @staticmethod
    def _folder_verdict(facts: dict) -> str:
        """Одна фраза: чья это беда — имени или самой папки на сервере."""
        if facts.get("name_warnings"):
            return "в имени папки есть " + ", ".join(facts["name_warnings"])
        if facts.get("children"):
            return f"похоже на папку-контейнер (вложенных папок: {len(facts['children'])})"
        if facts.get("listed") is False:
            return "сервер не признаёт это имя своим (LIST по точному имени не находит папку)"
        if facts.get("listed") is True:
            return "имя верное, нерабочая сама папка на сервере"
        return ""

    def _folder_diagnosis(self, folder: str, facts: Optional[dict] = None) -> str:
        """Подробное объяснение по фактам :meth:`_folder_facts`.

        Без него администратор видит лишь «failed EXAMINE» и не может понять,
        кто виноват и потеряно ли что-то.
        """
        facts = self._folder_facts(folder) if facts is None else facts
        parts: List[str] = []
        if facts["status_messages"] is not None:
            parts.append(f"по команде STATUS сервер сообщает: писем {facts['status_messages']}")
        else:
            parts.append("на команду STATUS сервер тоже не ответил")

        listed = facts["listed"]
        if listed is True:
            parts.append("в списке папок сервера это имя есть (LIST по точному имени находит его) — "
                         "значит имя верное, а сама папка на сервере нерабочая")
        elif listed is False:
            parts.append("LIST по точному имени эту папку НЕ находит — сервер считает, что такого "
                         "имени у него нет: проверьте кодировку и точное написание имени")

        children = facts["children"]
        if children:
            shown = ", ".join(children[:5]) + (" и др." if len(children) > 5 else "")
            parts.append(f"у папки есть вложенные папки ({len(children)}): {shown} — "
                         f"похоже, это папка-контейнер, своих писем она не хранит, "
                         f"а вложенные копируются отдельно")

        if facts["name_warnings"]:
            parts.append("в имени папки: " + ", ".join(facts["name_warnings"]))
        return "; ".join(parts)

    def _select_error(self, folder: str, exc: BaseException, attempts: int,
                      fallback_error: Optional[str] = None) -> Exception:
        """
        Собрать ошибку открытия папки так, чтобы администратор увидел ПРИЧИНУ:
        какую папку не удалось открыть, что именно пробовали (EXAMINE, обычный
        SELECT, STATUS, LIST по точному имени), что ответил сервер и что он
        прислал в untagged-строках.
        """
        mapped = _map_exception(exc)
        reply = _server_reply(exc)
        notices = _server_notices(self.client)
        tried = [f"EXAMINE — отказ (попыток: {attempts})"]
        if fallback_error is not None:
            tried.append(f"обычный SELECT — отказ ({fallback_error})")
        facts = self._folder_facts(folder)
        diagnosis = self._folder_diagnosis(folder, facts)
        parts = [f"Не удалось открыть папку «{folder}»: {mapped.message}",
                 "Что пробовали: " + "; ".join(tried)]
        if diagnosis:
            parts.append("Дополнительно: " + diagnosis)
        if reply:
            # Дублирование с текстом библиотеки допускаем осознанно: так в логе
            # всегда есть строка «ответ сервера», которую можно показать
            # администратору почтового сервера как есть.
            parts.append(f"Ответ сервера: «{reply}»")
        if notices:
            parts.append(f"Уведомления сервера: {notices}")
        message = ". ".join(parts) + "."
        hint = getattr(mapped, "hint", None) or (
            "Что проверить на сервере: существует ли папка и открывается ли она (не контейнер ли "
            "это); права (ACL) этой учётной записи на папку; не заблокирован ли ящик другим "
            "процессом; имя папки и его кодировка (IMAP UTF-7). Если папку не восстановить, "
            "добавьте её в «Пропускать папки» в карточке ящика — тогда копия перестанет "
            "считаться неполной из-за неё."
        )
        error = type(mapped)(message, hint=hint, cause=exc)
        # Разобранные части нужны журналу задания: там строка должна быть
        # КОРОТКОЙ, иначе полный текст упирается в предел длины события и
        # обрезается ровно на самом полезном месте.
        error.server_reply = reply or mapped.message
        error.tried = "; ".join(tried)
        error.diagnosis = diagnosis
        error.verdict = self._folder_verdict(facts)
        error.status_messages = facts["status_messages"]
        error.children = facts["children"]
        error.listed = facts["listed"]
        return error

    def search_all_uids(self) -> List[int]:
        try:
            return list(self.client.search(["ALL"]))
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def search_uids(self, info: Optional[Dict[str, int]] = None) -> List[int]:
        """Все UID открытой папки.

        На больших папках (по данным SELECT) — несколькими запросами по
        диапазонам UID: ответ на один «SEARCH ALL» у папки в сотни тысяч писем
        занимает мегабайты одной строкой.
        """
        exists = int((info or {}).get("exists") or 0)
        uidnext = int((info or {}).get("uidnext") or 0)
        if exists <= SEARCH_RANGE_THRESHOLD or uidnext <= 1:
            return self.search_all_uids()
        step = max(100_000, -(-uidnext // SEARCH_RANGE_PARTS))
        found: List[int] = []
        lo = 1
        while lo < uidnext:
            hi = min(uidnext - 1, lo + step - 1)
            try:
                found.extend(self.client.search(["UID", f"{lo}:{hi}"]))
            except Exception as exc:  # noqa: BLE001
                raise _map_exception(exc) from exc
            lo = hi + 1
        # письма, пришедшие после SELECT (UID ≥ UIDNEXT на момент открытия)
        try:
            found.extend(u for u in self.client.search(["UID", f"{uidnext}:*"]) if u >= uidnext)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        return sorted(set(int(u) for u in found))

    def uids_present(self, uids: List[int]) -> Optional[Set[int]]:
        """Какие из этих UID ещё есть в открытой папке (None — проверить не удалось).

        Нужно, чтобы отличить письмо, удалённое на сервере во время копирования
        (это нормально), от письма, которое сервер просто не отдаёт (это потеря).
        """
        if not uids:
            return set()
        found: Set[int] = set()
        try:
            for start in range(0, len(uids), 500):
                part = uids[start:start + 500]
                found.update(int(u) for u in self.client.search(["UID", uid_set(part)]))
        except Exception as exc:  # noqa: BLE001
            mapped = _map_exception(exc)
            if isinstance(mapped, (ImapConnectionError, ImapTimeoutError)):
                raise mapped from exc
            return None
        return found & set(int(u) for u in uids)

    def search_since(self, date) -> List[int]:
        try:
            return list(self.client.search(["SINCE", date]))
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def fetch_sizes(self, uids: List[int]) -> Dict[int, int]:
        """
        Спросить у сервера ТОЛЬКО размеры писем: `UID FETCH <uids> (RFC822.SIZE)`.

        Запрос дешёвый (тела не передаются), поэтому его можно сделать до
        загрузки и заранее отсеять слишком крупные письма. Возвращает
        {uid: размер}; UID, по которым сервер размер не сообщил, в результат
        не попадают.
        """
        sizes: Dict[int, int] = {}
        if not uids:
            return sizes
        batch = max(1, int(SIZE_PROBE_BATCH_SIZE))
        for start in range(0, len(uids), batch):
            chunk = uids[start:start + batch]
            try:
                data = self.client.fetch(chunk, [b"RFC822.SIZE"])
            except Exception as exc:  # noqa: BLE001
                raise _map_exception(exc) from exc
            for uid, item in (data or {}).items():
                raw_size = (item or {}).get(b"RFC822.SIZE")
                if raw_size is None:
                    continue
                try:
                    sizes[int(uid)] = int(raw_size)
                except (TypeError, ValueError):
                    continue
        return sizes

    def fetch_messages(self, uids: List[int], *, skip_larger_than: int = 0,
                       on_skipped: Optional[Callable[[int, int], None]] = None) -> Iterator[dict]:
        """
        Скачать письма по списку UID. Не помечает их как прочитанные.

        Порядок работы (окнами по ``SIZE_PROBE_BATCH_SIZE`` писем, чтобы не
        держать в памяти размеры всей папки и не молчать до первого письма):
          1. дёшево спросить у сервера только размеры (RFC822.SIZE);
          2. письма больше ``skip_larger_than`` отсеять СРАЗУ, не скачивая —
             о каждом сообщается вызовом ``on_skipped(uid, size)``;
          3. остальные качать порциями, которые набираются по СУММАРНОМУ
             размеру (≈``FETCH_CHUNK_TARGET_BYTES``) и не длиннее
             ``fetch_batch_size`` писем.

        Контракт наружу прежний: генератор словарей
        {uid, raw, flags, internaldate, size}.
        """
        if not uids:
            return
        limit = max(0, int(skip_larger_than or 0))
        batch = max(1, int(self.opt.fetch_batch_size))
        target_bytes = max(1, int(FETCH_CHUNK_TARGET_BYTES))
        window = max(batch, int(SIZE_PROBE_BATCH_SIZE))
        for wstart in range(0, len(uids), window):
            window_uids = uids[wstart:wstart + window]
            sizes = self.fetch_sizes(window_uids)
            wanted: List[int] = []
            for uid in window_uids:
                size = sizes.get(uid)
                if limit and size is not None and size > limit:
                    # Ни трафика, ни памяти на заведомо слишком большое письмо.
                    log.info("Письмо UID %s (%d Б) не скачивается: больше лимита %d Б.", uid, size, limit)
                    if on_skipped:
                        on_skipped(uid, size)
                    continue
                wanted.append(uid)
            for chunk in _plan_size_chunks(wanted, sizes, batch, target_bytes):
                yield from self._fetch_chunk(chunk)

    def _fetch_chunk(self, chunk: List[int]) -> Iterator[dict]:
        """Скачать одну готовую порцию UID и отдать письма по одному.

        Если сервер отказал в выдаче порции (одно письмо повреждено на сервере,
        письмо удалено во время FETCH — ``NO [EXPUNGEISSUED]``), порция делится
        пополам и так до одного письма: раньше один отказ бросал всю папку, и
        письма после «битого» не копировались больше никогда. Для письма,
        которое сервер не отдаёт и поодиночке, возвращается запись с ключом
        ``error`` — вызывающий учтёт её как ошибку конкретного письма.
        """
        if not chunk:
            return
        try:
            data = self.client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE"])
        except Exception as exc:  # noqa: BLE001
            mapped = _map_exception(exc)
            if isinstance(mapped, (ImapConnectionError, ImapTimeoutError)):
                raise mapped from exc
            if len(chunk) > 1:
                half = len(chunk) // 2
                yield from self._fetch_chunk(chunk[:half])
                yield from self._fetch_chunk(chunk[half:])
                return
            yield {"uid": chunk[0], "error": _server_reply(exc) or mapped.message}
            return
        missing: List[int] = []
        for uid in chunk:
            # pop, а не get: отданное письмо сразу перестаёт удерживаться
            # словарём ответа и освобождает память, не дожидаясь конца порции.
            item = data.pop(uid, None)
            if not item:
                missing.append(uid)
                continue
            raw = item.get(b"BODY[]") or item.get(b"RFC822") or b""
            if not raw:
                missing.append(uid)
                continue
            internaldate = item.get(b"INTERNALDATE")
            try:
                epoch = internaldate.timestamp() if internaldate else None
            except (OverflowError, OSError, ValueError):
                epoch = None
            # Флаги — атомы ASCII, но серверы присылают и ключевые слова в
            # cp1251: строгое .decode() роняло весь прогон (UnicodeDecodeError).
            flags = [f.decode("latin-1") if isinstance(f, bytes) else str(f) for f in (item.get(b"FLAGS") or ())]
            yield {
                "uid": uid,
                "raw": raw,
                "flags": flags,
                "internaldate": epoch,
                "size": int(item.get(b"RFC822.SIZE", len(raw)) or len(raw)),
            }
        if missing:
            # Молча терять письма нельзя: сервер мог их удалить между SEARCH и
            # FETCH, но так же выглядит и сбой выдачи. Раньше это попадало
            # только в лог службы; теперь вызывающий получает отметку по
            # каждому такому письму и показывает их в журнале задания.
            shown = ", ".join(str(u) for u in missing[:20])
            tail = f" и ещё {len(missing) - 20}" if len(missing) > 20 else ""
            log.warning("FETCH не вернул %d из %d писем (пропущены): UID %s%s",
                        len(missing), len(chunk), shown, tail)
            for uid in missing:
                yield {"uid": uid, "missing": True}

    # -- запись (для восстановления) ----------------------------------------
    def ensure_folder(self, folder: str) -> None:
        """
        Создать папку, если её ещё нет.

        Ответ «папка уже существует» — не ошибка (см. :func:`_is_already_exists_error`),
        всё остальное ошибка настоящая. После CREATE наличие папки
        перепроверяется: сервер мог ответить отказом с непривычной
        формулировкой (хотя папка есть), а мог ответить OK и папку не создать
        (нет прав, недопустимое имя) — тогда все последующие APPEND падали бы
        без внятной причины.
        """
        try:
            if self.client.folder_exists(folder):
                return
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

        create_error: Optional[BaseException] = None
        try:
            self.client.create_folder(folder)
        except Exception as exc:  # noqa: BLE001
            if not _is_already_exists_error(exc):
                create_error = exc  # вердикт вынесем после перепроверки

        exists: Optional[bool] = None
        try:
            exists = bool(self.client.folder_exists(folder))
        except Exception:  # noqa: BLE001
            exists = None  # перепроверить не удалось — не мешаем работе

        if exists:
            return
        if create_error is not None:
            raise _map_exception(create_error) from create_error
        if exists is False:
            raise ImapProtocolError(
                f"Папка «{folder}» не создана: сервер принял команду CREATE, но папки на сервере нет.",
                hint="Проверьте права на создание папок, допустимость имени и разделитель иерархии.",
            )

    def append(self, folder: str, raw: bytes, flags=(), msg_time=None) -> None:
        try:
            self.client.append(folder, raw, flags=list(flags or ()), msg_time=msg_time)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def fetch_existing_message_ids(self, folder: str) -> Set[str]:
        """
        Множество Message-ID писем, уже лежащих в папке — ОДНИМ запросом
        `FETCH 1:* BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]`.

        Нужно проверке дублей при восстановлении: иначе на каждое письмо
        приходится SELECT + SEARCH (на 100 000 писем — сотни тысяч обращений
        к серверу). Ошибку НЕ глушим: «не удалось проверить» и «дублей нет» —
        разные вещи, и решать, что с этим делать, должен вызывающий код.
        """
        info = self.select(folder, readonly=True)
        if not info.get("exists"):
            return set()  # пустая папка: `FETCH 1:*` часть серверов считает ошибкой
        try:
            data = self.client.fetch(["1:*"], [b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        found: Set[str] = set()
        for item in (data or {}).values():
            for key, value in (item or {}).items():
                if not isinstance(key, bytes) or b"HEADER.FIELDS" not in key:
                    continue
                mid = _header_message_id(value)
                if mid:
                    found.add(mid)
        return found

    def search_header_messageid(self, message_id: str) -> List[int]:
        """
        Поиск письма по Message-ID (поштучный, медленный — запасной путь).

        Ошибку поиска НЕ подменяем пустым списком: пустой ответ означает
        «дублей нет», и на сбое SEARCH письмо заливалось бы повторно.
        """
        if not message_id:
            return []
        try:
            return list(self.client.search(["HEADER", "Message-ID", message_id]))
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc


#: Сколько раз подряд можно переподключаться, если между обрывами не удалось
#: продвинуться ни на одно письмо или папку (сервер лежит — ждать бесполезно).
MAX_RECONNECTS_WITHOUT_PROGRESS = 3
#: Пауза перед переподключением, с (модульная — её подменяют тесты).
RECONNECT_DELAY_S = 5.0


class ReconnectingSession:
    """IMAP-соединение одного прогона с переподключением после обрыва.

    Раньше обрыв связи на любой папке валил весь прогон (или, хуже, считался
    отказом ЭТОЙ папки — и исправные папки копили «неудачи»). Теперь движок
    переподключается и продолжает. Предел — несколько обрывов ПОДРЯД без
    продвижения: если между ними не сохранено ни одного письма и не пройдено
    ни одной папки, сервер, видимо, лежит, и лучше отдать задание очереди на
    повтор позже.
    """

    def __init__(self, account: Account, options: ConnectOptions,
                 emit: Callable[[str, str], None], check_cancel: Callable[[], None]) -> None:
        self.account = account
        self.options = options
        self.emit = emit
        self.check_cancel = check_cancel
        self.conn: Optional["ImapConnection"] = None
        self.reconnects = 0
        self._fails_in_row = 0

    def open(self) -> "ImapConnection":
        conn = ImapConnection(self.account, self.options)
        conn.connect()
        self.conn = conn
        return conn

    def progressed(self) -> None:
        """Продвинулись (сохранено письмо, пройдена папка) — счётчик обрывов с нуля."""
        self._fails_in_row = 0

    def reconnect(self, exc) -> "ImapConnection":
        """Переподключиться после обрыва или бросить ``exc``, если предел исчерпан."""
        self._fails_in_row += 1
        self.close(force=True)
        if self._fails_in_row > MAX_RECONNECTS_WITHOUT_PROGRESS:
            raise exc
        self.reconnects += 1
        text = getattr(exc, "message", None) or str(exc)
        self.emit("WARNING", f"Связь с сервером прервалась: {text} Переподключение "
                             f"(попытка {self._fails_in_row} из {MAX_RECONNECTS_WITHOUT_PROGRESS})…")
        # Пауза — по кусочкам, чтобы кнопка «Отмена» срабатывала и здесь.
        deadline = time.time() + max(0.0, float(RECONNECT_DELAY_S))
        while time.time() < deadline:
            self.check_cancel()
            time.sleep(min(0.5, max(0.0, deadline - time.time())))
        return self.open()

    def reset(self) -> "ImapConnection":
        """Открыть соединение заново без счёта обрывов (после сбоя разбора ответа)."""
        self.close(force=True)
        return self.open()

    def close(self, *, force: bool = False) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close(force=force)
            except Exception:  # noqa: BLE001
                pass


def probe_account(account: Account, options: Optional[ConnectOptions] = None) -> dict:
    """
    Проверить подключение к ящику. Возвращает структуру для интерфейса:
    {ok, error, hint, capabilities, duplicate_folders:[имя],
     folders:[{name, delimiter, selectable, special, flags, duplicate}]}.

    ``duplicate`` — сервер вернул эту папку в LIST повторно; ``flags`` — её
    флаги как есть (по ним видно, почему папка не открывается: \\Noselect).
    Никогда не бросает исключение — всё упаковывается в результат.
    """
    result = {"ok": False, "error": None, "hint": None, "capabilities": [], "folders": [],
              "duplicate_folders": [], "kind": ""}
    try:
        with ImapConnection(account, options) as conn:
            result["capabilities"] = conn.capabilities()
            # Отдельно помечаем ПОВТОРЫ в ответе LIST: сервер иногда возвращает
            # одну и ту же папку дважды, и без такой пометки причину «папка
            # скопирована дважды» на боевом сервере не увидеть. Проверка
            # бесплатная — SELECT для неё не нужен.
            # Признак «папка реально открывается» здесь НЕ проверяем намеренно:
            # это потребовало бы SELECT/EXAMINE на каждую папку (отдельный
            # запрос к серверу на каждую из десятков папок) — слишком дорого
            # для кнопки «Проверить подключение».
            seen: Dict[str, int] = {}
            for fi in conn.list_folders():
                seen[fi.name] = seen.get(fi.name, 0) + 1
                duplicate = seen[fi.name] > 1
                if duplicate and fi.name not in result["duplicate_folders"]:
                    result["duplicate_folders"].append(fi.name)
                result["folders"].append({
                    "name": fi.name,
                    "delimiter": fi.delimiter,
                    "selectable": fi.selectable,
                    "special": [f for f in fi.flags if f not in ("\\HasNoChildren", "\\HasChildren")],
                    "flags": list(fi.flags),
                    "duplicate": duplicate,
                })
            if result["duplicate_folders"]:
                log.warning("Сервер вернул повторяющиеся папки в LIST: %s",
                            ", ".join(result["duplicate_folders"]))
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["kind"] = login_failure_kind(exc)
        if isinstance(exc, MailArchiverError):
            result["error"] = exc.message
            result["hint"] = exc.hint
        else:
            result["error"] = str(exc)
    return result


def login_failure_kind(exc: BaseException) -> str:
    """Чем кончилась попытка входа: auth_error (сервер отверг логин/пароль) или conn_error."""
    if isinstance(exc, (ImapAuthError, LoginError)):
        return "auth_error"
    return "conn_error"


def check_login(account: Account, options: Optional[ConnectOptions] = None) -> tuple:
    """Только войти в ящик и выйти — проверка пароля. ``(статус, текст ошибки)``.

    Статусы: ok | auth_error | conn_error. Папки не запрашиваются: на сотнях
    ящиков важна скорость, и лишние команды серверу ни к чему.
    """
    conn = ImapConnection(account, options)
    try:
        conn.connect()
    except Exception as exc:  # noqa: BLE001
        text = exc.message if isinstance(exc, MailArchiverError) else str(exc)
        return login_failure_kind(exc), text
    conn.close()
    return "ok", ""


def diagnose_folders(account: Account, options: Optional[ConnectOptions] = None,
                     max_folders: int = 500, *, global_include: Optional[List[str]] = None,
                     global_exclude: Optional[List[str]] = None) -> dict:
    """Проверить КАЖДУЮ папку ящика и сказать по каждой, что с ней.

    Отвечает на вопрос «почему копия неполная», не заставляя администратора
    читать журнал задания: по каждой папке видно, открывается ли она, сколько
    в ней писем и что делать. В отличие от :func:`probe_account` здесь на
    каждую папку идёт запрос к серверу (EXAMINE, при отказе — STATUS), поэтому
    вызывается только по кнопке, а не при обычной проверке подключения.

    Правила те же, что у копирования: те же списки «Копировать только» и
    «Пропускать» (ящика и общие), папка-контейнер — только если сервер сам
    пометил её \\Noselect или по STATUS в ней 0 писем.

    Вердикты (поле ``verdict``):
      * ``ok`` — папка открылась, письма доступны;
      * ``container`` — не открывается, писем в ней 0, есть вложенные папки:
        содержимое копируется через вложенные;
      * ``noselect`` — сервер сам пометил папку как неоткрываемую;
      * ``empty_broken`` — не открывается, по STATUS писем 0: терять нечего;
      * ``broken`` — не открывается, а письма в ней есть (или сервер не
        говорит, сколько их): ЭТО потеря;
      * ``excluded`` — папка исключена настройками и не копируется.

    Исключения наружу не пробрасываются — всё упаковано в результат.
    """
    result: dict = {"ok": False, "error": None, "hint": None, "folders": [],
                    "counts": {"ok": 0, "container": 0, "noselect": 0, "empty_broken": 0,
                               "broken": 0, "excluded": 0},
                    "broken_folders": [], "messages_lost": 0, "checked": 0, "truncated": False,
                    "bad_names": []}
    include = [str(x).strip() for x in list(account.folder_include or []) + list(global_include or [])
               if str(x).strip()]
    exclude = [str(x).strip() for x in list(account.folder_exclude or []) + list(global_exclude or [])
               if str(x).strip()]
    try:
        with ImapConnection(account, options) as conn:
            infos = conn.list_folders()
            result["bad_names"] = list(conn.bad_folder_names)
            all_names = [fi.name for fi in infos]
            result["truncated"] = len(infos) > max_folders
            seen: Set[str] = set()
            for fi in infos[:max_folders]:
                if fi.name in seen:        # сервер повторил папку в LIST
                    continue
                seen.add(fi.name)
                row = {"name": fi.name, "flags": list(fi.flags), "children": 0,
                       "messages": None, "verdict": "", "detail": ""}
                if include and not folder_matches(fi.name, include, fi.delimiter):
                    row["verdict"] = "excluded"
                    row["detail"] = "не входит в список «Копировать только папки»"
                elif exclude and folder_matches(fi.name, exclude, fi.delimiter):
                    row["verdict"] = "excluded"
                    row["detail"] = "папка в списке «Пропускать папки» (или вложена в такую)"
                elif not fi.selectable:
                    row["verdict"] = "noselect"
                    row["detail"] = "сервер пометил папку как неоткрываемую (контейнер)"
                else:
                    info, error = conn.probe_select(fi.name)
                    result["checked"] += 1
                    if info is not None:
                        row["verdict"] = "ok"
                        row["messages"] = info["exists"]
                        row["detail"] = "открывается, письма доступны"
                    else:
                        children = [n for n in all_names
                                    if n != fi.name and n.startswith(fi.name + (fi.delimiter or "/"))]
                        row["children"] = len(children)
                        status = conn.folder_status(fi.name)
                        msgs = status["messages"] if status is not None else None
                        row["messages"] = msgs
                        if msgs == 0 and children:
                            row["verdict"] = "container"
                            row["detail"] = (f"не открывается, писем в ней 0, есть вложенные папки "
                                             f"({len(children)}): своих писем не хранит")
                        elif msgs == 0:
                            row["verdict"] = "empty_broken"
                            row["detail"] = f"не открывается ({error}), но писем в ней 0 — терять нечего"
                        else:
                            row["verdict"] = "broken"
                            if msgs is None:
                                row["detail"] = (f"не открывается ({error}); STATUS тоже не отвечает — "
                                                 f"сколько в ней писем, неизвестно")
                                if children:
                                    row["detail"] += (f"; у папки есть вложенные ({len(children)}) — "
                                                      f"возможно, это контейнер, но проверить нельзя")
                            else:
                                row["detail"] = f"не открывается ({error}); по STATUS писем {msgs}"
                                if children:
                                    row["detail"] += (f"; вложенные папки ({len(children)}) копируются, "
                                                      f"а письма самой папки — нет")
                                result["messages_lost"] += int(msgs)
                            result["broken_folders"].append(fi.name)
                result["counts"][row["verdict"]] = result["counts"].get(row["verdict"], 0) + 1
                result["folders"].append(row)
            for bad in result["bad_names"]:
                result["folders"].append({"name": bad, "flags": [], "children": 0, "messages": None,
                                          "verdict": "broken",
                                          "detail": "имя папки прислано в неверной кодировке IMAP UTF-7 — "
                                                    "открыть её по имени нельзя; переименуйте папку на сервере"})
                result["counts"]["broken"] += 1
                result["broken_folders"].append(bad)
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        from ..errors import MailArchiverError
        if isinstance(exc, MailArchiverError):
            result["error"] = exc.message
            result["hint"] = exc.hint
        else:
            result["error"] = str(exc)
    return result
