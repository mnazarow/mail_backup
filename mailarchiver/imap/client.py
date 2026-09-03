"""
Обёртка над IMAPClient с надёжной обработкой ошибок, таймаутами, повторными
подключениями, поддержкой SSL/STARTTLS/без шифрования, входом по паролю и по
OAuth2 (XOAUTH2).
"""
from __future__ import annotations

import imaplib
import socket
import ssl
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from ..errors import (
    ImapAuthError,
    ImapConnectionError,
    ImapProtocolError,
    ImapTimeoutError,
)
from ..logging_setup import get_logger
from ..models import Account, AuthType, Security
from .oauth import refresh_access_token

log = get_logger("imap")


@dataclass
class ConnectOptions:
    connect_timeout_s: int = 30
    socket_timeout_s: int = 120
    verify_ssl: bool = True
    fetch_batch_size: int = 200


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
    if isinstance(exc, imaplib.IMAP4.error):
        text = str(exc).lower()
        if "auth" in text or "login" in text or "credential" in text:
            return ImapAuthError(f"IMAP-сервер отклонил аутентификацию: {exc}", cause=exc)
        return ImapProtocolError(f"Ошибка протокола IMAP: {exc}", cause=exc)
    return ImapProtocolError(f"Неожиданная ошибка IMAP: {exc}", cause=exc)


class ImapConnection:
    """Одно соединение с IMAP-ящиком. Используйте как контекстный менеджер."""

    def __init__(self, account: Account, options: Optional[ConnectOptions] = None) -> None:
        self.account = account
        self.opt = options or ConnectOptions()
        self.client: Optional[IMAPClient] = None
        self._delimiter: str = "/"

    # -- контекстный менеджер -----------------------------------------------
    def __enter__(self) -> "ImapConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- подключение ---------------------------------------------------------
    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.opt.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def connect(self) -> None:
        acc = self.account
        socket.setdefaulttimeout(self.opt.connect_timeout_s)
        try:
            if acc.security == Security.SSL:
                client = IMAPClient(acc.host, port=acc.port or 993, ssl=True,
                                    ssl_context=self._ssl_context(), timeout=self.opt.socket_timeout_s)
            else:
                client = IMAPClient(acc.host, port=acc.port or 143, ssl=False,
                                    timeout=self.opt.socket_timeout_s)
                if acc.security == Security.STARTTLS:
                    client.starttls(self._ssl_context())
            self.client = client
            self._login()
            log.info("Подключение к ящику «%s» (%s:%s) установлено", acc.name, acc.host, acc.port)
        except (LoginError, ImapAuthError):
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise _map_exception(exc) from exc
        finally:
            socket.setdefaulttimeout(None)

    def _login(self) -> None:
        acc = self.account
        assert self.client is not None
        try:
            if acc.auth_type == AuthType.OAUTH2:
                access, _exp = refresh_access_token(
                    acc.oauth_token_url, acc.oauth_client_id, acc.oauth_client_secret,
                    acc.oauth_refresh_token, timeout=self.opt.connect_timeout_s,
                )
                self.client.oauth2_login(acc.username, access)
            else:
                self.client.login(acc.username, acc.password)
        except LoginError as exc:
            raise ImapAuthError(
                "Не удалось войти в почтовый ящик: сервер отклонил учётные данные.",
                hint="Проверьте логин/пароль. Возможно, нужен «пароль приложения» или включение IMAP в настройках почты.",
                cause=exc,
            ) from exc

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:  # noqa: BLE001
                try:
                    self.client.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self.client = None

    # -- операции ------------------------------------------------------------
    def capabilities(self) -> List[str]:
        try:
            return [c.decode() if isinstance(c, bytes) else str(c) for c in self.client.capabilities()]
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def list_folders(self) -> List[FolderInfo]:
        try:
            raw = self.client.list_folders()
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        result: List[FolderInfo] = []
        for flags, delimiter, name in raw:
            deli = delimiter.decode() if isinstance(delimiter, bytes) else (delimiter or "/")
            self._delimiter = deli or self._delimiter
            flag_list = [f.decode() if isinstance(f, bytes) else str(f) for f in (flags or ())]
            selectable = "\\Noselect" not in flag_list and "\\NonExistent" not in flag_list
            result.append(FolderInfo(name=name, delimiter=deli or "/", flags=flag_list, selectable=selectable))
        return result

    @property
    def delimiter(self) -> str:
        return self._delimiter

    def select(self, folder: str, readonly: bool = True) -> Dict[str, int]:
        try:
            info = self.client.select_folder(folder, readonly=readonly)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc
        return {
            "uidvalidity": int(info.get(b"UIDVALIDITY", 0) or 0),
            "uidnext": int(info.get(b"UIDNEXT", 0) or 0),
            "exists": int(info.get(b"EXISTS", 0) or 0),
        }

    def search_all_uids(self) -> List[int]:
        try:
            return list(self.client.search(["ALL"]))
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def search_since(self, date) -> List[int]:
        try:
            return list(self.client.search(["SINCE", date]))
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def fetch_messages(self, uids: List[int]) -> Iterator[dict]:
        """Скачать письма по списку UID (по батчам). Не помечает как прочитанные."""
        if not uids:
            return
        batch = max(1, int(self.opt.fetch_batch_size))
        for start in range(0, len(uids), batch):
            chunk = uids[start:start + batch]
            try:
                data = self.client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS", b"INTERNALDATE", b"RFC822.SIZE"])
            except Exception as exc:  # noqa: BLE001
                raise _map_exception(exc) from exc
            for uid in chunk:
                item = data.get(uid)
                if not item:
                    continue
                raw = item.get(b"BODY[]") or item.get(b"RFC822") or b""
                if not raw:
                    continue
                internaldate = item.get(b"INTERNALDATE")
                epoch = internaldate.timestamp() if internaldate else None
                flags = [f.decode() if isinstance(f, bytes) else str(f) for f in (item.get(b"FLAGS") or ())]
                yield {
                    "uid": uid,
                    "raw": raw,
                    "flags": flags,
                    "internaldate": epoch,
                    "size": int(item.get(b"RFC822.SIZE", len(raw)) or len(raw)),
                }

    # -- запись (для восстановления) ----------------------------------------
    def ensure_folder(self, folder: str) -> None:
        try:
            if not self.client.folder_exists(folder):
                self.client.create_folder(folder)
        except Exception as exc:  # noqa: BLE001
            # некоторые серверы бросают ошибку, если папка уже есть — игнорируем
            if "exist" not in str(exc).lower():
                raise _map_exception(exc) from exc

    def append(self, folder: str, raw: bytes, flags=(), msg_time=None) -> None:
        try:
            self.client.append(folder, raw, flags=list(flags or ()), msg_time=msg_time)
        except Exception as exc:  # noqa: BLE001
            raise _map_exception(exc) from exc

    def search_header_messageid(self, message_id: str) -> List[int]:
        if not message_id:
            return []
        try:
            return list(self.client.search(["HEADER", "Message-ID", message_id]))
        except Exception:  # noqa: BLE001
            return []


def probe_account(account: Account, options: Optional[ConnectOptions] = None) -> dict:
    """
    Проверить подключение к ящику. Возвращает структуру для интерфейса:
    {ok, error, hint, capabilities, folders:[{name, selectable, messages?}]}.
    Никогда не бросает исключение — всё упаковывается в результат.
    """
    result = {"ok": False, "error": None, "hint": None, "capabilities": [], "folders": []}
    try:
        with ImapConnection(account, options) as conn:
            result["capabilities"] = conn.capabilities()
            for fi in conn.list_folders():
                result["folders"].append({
                    "name": fi.name,
                    "delimiter": fi.delimiter,
                    "selectable": fi.selectable,
                    "special": [f for f in fi.flags if f not in ("\\HasNoChildren", "\\HasChildren")],
                })
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        from ..errors import MailArchiverError
        if isinstance(exc, MailArchiverError):
            result["error"] = exc.message
            result["hint"] = exc.hint
        else:
            result["error"] = str(exc)
    return result
