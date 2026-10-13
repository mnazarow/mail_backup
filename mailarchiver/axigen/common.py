"""
Общее для работы с сервером Axigen: настройки, ошибки, шифрованное соединение.

Сервер Axigen управляется через CLI — текстовый протокол поверх TCP (обычно
порт 7000, тот же, что у ``telnet``), а письма любого ящика администратор
читает через службу FTP Backup & Restore. Оба канала требуют пароля
администратора Axigen, поэтому:

* пароль хранится в базе зашифрованным и уходит только на адрес из настроек;
* соединение можно (и через сеть нужно) шифровать — SSL-слушатель CLI и FTPS;
* самоподписанному сертификату сервера можно доверять по отпечатку SHA-256:
  отпечаток показывает проверка подключения, администратор сверяет его с
  сервером и сохраняет — тогда подмена сервера обнаруживается так же, как при
  проверке по цепочке доверия.
"""
from __future__ import annotations

import hashlib
import re
import socket
import ssl
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import MailArchiverError

#: Что значит «учётная запись заблокирована»: у неё нет ни одной службы входа.
#: Названия служб Axigen сверяются без учёта регистра; незнакомые не мешают.
LOGIN_SERVICES = ("pop3", "imap", "webmail", "activesync", "mobility", "outlookconnector", "caldav", "carddav",
                  "webdav", "eas", "mapi")
#: Служба приёма писем: остаётся у заблокированной учётной записи, если почту ей
#: нужно продолжать принимать (письма уволенному доходят и попадают в архив).
RECEIVE_SERVICES = ("localdelivery",)
#: Отправка писем наружу — тоже вход (по паролю, SMTP AUTH): у заблокированной
#: учётной записи её быть не должно.
SEND_SERVICES = ("remotedelivery", "smtp", "smtpauth", "smtpsubmission")

TLS_MODES_CLI = ("none", "tls")
TLS_MODES_FTP = ("none", "explicit", "implicit")
NAME_ENCODINGS = ("auto", "utf7", "utf8")

#: Папки ящика, которые не копируются через FTP Backup по умолчанию: это не почта
#: (календарь, контакты, задачи), а IMAP их и не показывает.
DEFAULT_FTP_EXCLUDE = ["Calendar", "Contacts", "Tasks", "Notes", "Journal",
                       "Календарь", "Контакты", "Задачи", "Заметки", "Дневник"]


class AxigenError(MailArchiverError):
    """Сбой при работе с сервером Axigen."""

    code = "axigen_error"


class AxigenNotConfigured(AxigenError):
    code = "axigen_not_configured"


class AxigenConnectionError(AxigenError):
    """Нет связи с сервером (сеть, таймаут, TLS) — операцию можно повторить."""

    code = "axigen_connection"
    retryable = True


class AxigenAuthError(AxigenError):
    """Сервер не принял логин и пароль администратора."""

    code = "axigen_auth"


class AxigenCommandError(AxigenError):
    """Сервер ответил на команду отказом (-ERR)."""

    code = "axigen_command"

    def __init__(self, message: str, *, command: str = "", reply: str = "", **kw) -> None:
        super().__init__(message, **kw)
        self.command = command
        self.reply = reply


class AxigenCertError(AxigenConnectionError):
    """Сертификат сервера не прошёл проверку (в том числе по отпечатку)."""

    code = "axigen_cert"
    retryable = False

    def __init__(self, message: str, *, fingerprint: str = "", **kw) -> None:
        super().__init__(message, **kw)
        self.fingerprint = fingerprint


_HEX = re.compile(r"[^0-9a-f]")


def normalize_fingerprint(value: str) -> str:
    """Отпечаток SHA-256 в виде 64 шестнадцатеричных знаков (двоеточия и регистр не важны)."""
    text = _HEX.sub("", str(value or "").lower())
    return text if len(text) == 64 else ""


def pretty_fingerprint(value: str) -> str:
    fp = normalize_fingerprint(value)
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper() if fp else ""


def cert_fingerprint(sock) -> str:
    """Отпечаток SHA-256 сертификата сервера на установленном TLS-соединении."""
    try:
        der = sock.getpeercert(binary_form=True)
    except (ValueError, AttributeError, ssl.SSLError):
        der = None
    return hashlib.sha256(der).hexdigest() if der else ""


@dataclass
class TlsPolicy:
    """Как проверять сертификат сервера."""

    verify: bool = True        # проверять цепочку доверия и имя сервера
    fingerprint: str = ""      # доверять сертификату с этим отпечатком (вместо цепочки)

    def context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if normalize_fingerprint(self.fingerprint) or not self.verify:
            # Отпечаток сверяется после рукопожатия (check); цепочка тогда не нужна:
            # у Axigen во внутренней сети сертификат обычно самоподписанный.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def check(self, sock, what: str) -> str:
        """Сверить отпечаток. Возвращает отпечаток сертификата сервера."""
        actual = cert_fingerprint(sock)
        wanted = normalize_fingerprint(self.fingerprint)
        if wanted and actual != wanted:
            raise AxigenCertError(
                f"{what}: сертификат сервера не тот, которому разрешено доверять "
                f"(отпечаток {pretty_fingerprint(actual) or 'неизвестен'}, ожидался {pretty_fingerprint(wanted)}).",
                fingerprint=actual,
                hint="Если сертификат на сервере Axigen заменили — сверьте новый отпечаток с сервером и "
                     "сохраните его в «Настройки → Сервер Axigen». Иначе соединение могли перехватить.")
        return actual


def wrap_tls(raw_sock: socket.socket, host: str, policy: TlsPolicy, what: str):
    """Обернуть сокет в TLS по правилам ``policy``. Возвращает (сокет, отпечаток)."""
    ctx = policy.context()
    try:
        tls = ctx.wrap_socket(raw_sock, server_hostname=host or None)
    except ssl.SSLCertVerificationError as exc:
        try:
            raw_sock.close()
        except OSError:
            pass
        raise AxigenCertError(
            f"{what}: сертификат сервера не прошёл проверку ({exc.verify_message or exc}).",
            hint="У сервера во внутренней сети сертификат часто самоподписанный. Нажмите «Проверить "
                 "подключение» в «Настройки → Сервер Axigen», сверьте отпечаток с сервером и нажмите "
                 "«Доверять этому сертификату».", cause=exc) from exc
    except (ssl.SSLError, OSError) as exc:
        try:
            raw_sock.close()
        except OSError:
            pass
        raise AxigenConnectionError(
            f"{what}: не удалось установить шифрованное соединение ({exc}).",
            hint="Проверьте, что на этом порту слушает SSL-слушатель (а не обычный).", cause=exc) from exc
    return tls, policy.check(tls, what)


def is_loopback(host: str) -> bool:
    """Адрес — эта же машина: 127.0.0.0/8, ::1 или ``localhost``.

    Имена не разрешаются через DNS: ответ мог бы смениться между проверкой и
    подключением, и пароль ушёл бы по сети без шифрования. Сервер Axigen на
    этой же машине указывайте как 127.0.0.1.
    """
    import ipaddress
    text = (host or "").strip().strip("[]").lower()
    if not text:
        return False
    if text in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def tls_unverified(st: "AxigenSettings") -> List[str]:
    """Шифрованные каналы, у которых сертификат сервера не проверяется ничем.

    Соединение шифруется, но подмену сервера никто не заметит: пароль
    администратора получит тот, кто встал посередине.
    """
    if st.tls_verify or is_loopback(st.host):
        return []
    out = []
    if st.cli_tls == "tls" and not normalize_fingerprint(st.cli_cert_sha256):
        out.append("CLI")
    if st.ftp_tls != "none" and not normalize_fingerprint(st.ftp_cert_sha256):
        out.append("FTP Backup")
    return out


def require_encryption(st: "AxigenSettings", encrypted: bool, what: str, *, setting: str) -> None:
    """Пароль администратора Axigen по сети — только в шифрованном соединении.

    Без шифрования можно только к этой же машине (127.0.0.1) или если
    администратор явно разрешил это в настройках («Разрешить без шифрования»).
    """
    if encrypted or st.allow_plaintext or is_loopback(st.host):
        return
    raise AxigenNotConfigured(
        f"{what}: соединение с {st.host} без шифрования — пароль администратора Axigen по сети так не "
        f"отправляется.",
        hint=f"Включите шифрование («{setting}» в «Настройки → Сервер Axigen») и SSL-слушатель на сервере "
             f"Axigen (WebAdmin → Services → нужная служба → Listeners). Если сервер архива и Axigen в одной "
             f"доверенной сети и шифровать нельзя — включите «Разрешить без шифрования» (пароль будет виден в "
             f"сети).")


def open_socket(host: str, port: int, timeout: float, what: str) -> socket.socket:
    try:
        return socket.create_connection((host, int(port)), timeout=timeout)
    except socket.timeout as exc:
        raise AxigenConnectionError(f"{what}: сервер {host}:{port} не ответил за {int(timeout)} с.",
                                    hint="Проверьте адрес, порт и что сервер архива пускают на этот порт "
                                         "(правила доступа слушателя Axigen, межсетевой экран).",
                                    cause=exc) from exc
    except OSError as exc:
        raise AxigenConnectionError(f"{what}: нет соединения с {host}:{port} ({exc.strerror or exc}).",
                                    hint="Проверьте адрес и порт. У слушателя Axigen по умолчанию адрес "
                                         "127.0.0.1 — с другой машины к нему не подключиться: добавьте "
                                         "слушатель на адресе сервера и разрешите в нём адрес сервера архива.",
                                    cause=exc) from exc


@dataclass
class AxigenSettings:
    """Настройки раздела ``axigen`` (см. :data:`mailarchiver.config.DEFAULTS`)."""

    enabled: bool = False
    host: str = ""
    cli_port: int = 7000
    cli_tls: str = "none"
    admin_user: str = "admin"
    admin_password: str = ""
    tls_verify: bool = True
    cli_cert_sha256: str = ""
    imap_host: str = ""
    domains: List[str] = field(default_factory=list)
    allow_changes: bool = False
    ftp_port: int = 21
    ftp_tls: str = "none"
    ftp_cert_sha256: str = ""
    ftp_user: str = ""
    ftp_password: str = ""
    ftp_account_path: str = ""
    ftp_name_encoding: str = "auto"
    ftp_exclude_folders: List[str] = field(default_factory=lambda: list(DEFAULT_FTP_EXCLUDE))
    timeout_s: int = 60
    #: разрешить вход без шифрования к другой машине (пароль будет виден в сети)
    allow_plaintext: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.host and self.admin_user and self.admin_password)

    @property
    def cli_policy(self) -> TlsPolicy:
        return TlsPolicy(verify=self.tls_verify, fingerprint=self.cli_cert_sha256)

    @property
    def ftp_policy(self) -> TlsPolicy:
        return TlsPolicy(verify=self.tls_verify, fingerprint=self.ftp_cert_sha256)

    @property
    def ftp_login(self) -> str:
        return self.ftp_user or self.admin_user

    @property
    def ftp_secret(self) -> str:
        return self.ftp_password or self.admin_password

    def domain_allowed(self, domain: str) -> bool:
        wanted = {d.strip().lower() for d in self.domains if d.strip()}
        return not wanted or (domain or "").strip().lower() in wanted

    def require(self) -> "AxigenSettings":
        if not self.enabled:
            raise AxigenNotConfigured("Работа с сервером Axigen выключена.",
                                      hint="Включите и заполните «Настройки → Сервер Axigen».")
        if not self.host:
            raise AxigenNotConfigured("Не указан адрес сервера Axigen.",
                                      hint="Заполните «Настройки → Сервер Axigen → Адрес сервера».")
        if not self.admin_user or not self.admin_password:
            raise AxigenNotConfigured("Не заданы логин и пароль администратора Axigen.",
                                      hint="Заполните «Настройки → Сервер Axigen».")
        return self


_FIELDS = [f for f in AxigenSettings.__dataclass_fields__]


def load_settings(svc) -> AxigenSettings:
    """Настройки из конфигурации и раздела «Настройки» (БД перекрывает config.yaml)."""
    values = {}
    for name in _FIELDS:
        try:
            value = svc.rt("axigen", name)
        except Exception:  # noqa: BLE001 — у заглушек в тестах rt может не знать раздела
            value = None
        if value is not None:
            values[name] = value
    st = AxigenSettings(**values)
    st.host = str(st.host or "").strip()
    st.admin_user = str(st.admin_user or "").strip()
    st.imap_host = str(st.imap_host or "").strip()
    st.ftp_user = str(st.ftp_user or "").strip()
    st.cli_tls = st.cli_tls if st.cli_tls in TLS_MODES_CLI else "none"
    st.ftp_tls = st.ftp_tls if st.ftp_tls in TLS_MODES_FTP else "none"
    st.ftp_name_encoding = st.ftp_name_encoding if st.ftp_name_encoding in NAME_ENCODINGS else "auto"
    try:
        st.cli_port = int(st.cli_port)
        st.ftp_port = int(st.ftp_port)
        st.timeout_s = max(5, int(st.timeout_s))
    except (TypeError, ValueError):
        st.cli_port, st.ftp_port, st.timeout_s = 7000, 21, 60
    return st


def split_email(address: str) -> tuple:
    """«ivanov@company.ru» → («ivanov», «company.ru»); без «@» — (адрес, «»)."""
    text = (address or "").strip()
    local, at, domain = text.rpartition("@")
    if not at:
        return text, ""
    return local, domain.lower()


def services_blocked(services: Optional[List[str]], *, blocked_by_us: bool = False) -> Optional[bool]:
    """Заблокирован ли вход при таком списке служб (None — не понять).

    * есть служба входа или отправки — вход открыт (False);
    * остались только службы приёма писем — закрыт (True);
    * список пуст — закрыт, только если его закрыл MailArchiver (``blocked_by_us``):
      пустой список Axigen может показывать и у учётной записи, которая
      наследует службы домена;
    * незнакомые названия служб — None: по ним не понять, открыт ли вход, и
      действовать по такому ответу нельзя.
    """
    if services is None:
        return None
    low = {s.strip().lower() for s in services if s.strip()}
    if low & (set(LOGIN_SERVICES) | set(SEND_SERVICES)):
        return False
    if not low:
        return True if blocked_by_us else None
    if low <= set(RECEIVE_SERVICES):
        return True
    return None


def blocked_services(services: List[str], keep_receiving: bool) -> List[str]:
    """Список служб заблокированной учётной записи: без входа (и, если надо, с приёмом писем).

    «Продолжать принимать письма» оставляет приём, если он был; включать приём,
    которого у учётной записи не было, блокировка не должна.
    """
    if not keep_receiving:
        return []
    return [s for s in services if s.strip().lower() in RECEIVE_SERVICES]


def connection_weakening(old: Callable[[str], Any], new_values: Dict[str, Any]) -> Dict[str, List[str]]:
    """Изменения настроек, после которых сохранённый пароль ушёл бы по более слабому или другому каналу.

    ``old(key)`` — нынешнее значение ``axigen.<key>``, ``new_values`` — сохраняемые
    значения (полные ключи). Порт (другая служба), выключенное шифрование или
    проверка сертификата, «Разрешить без шифрования», другой отпечаток сертификата —
    всё это, как и смена адреса, требует ввести пароль заново. Усиление защиты
    (включить SSL, включить проверку) пароля не требует.
    """
    def new(key: str):
        return new_values.get("axigen." + key, old(key))

    def changed(key: str) -> bool:
        return ("axigen." + key) in new_values and new_values["axigen." + key] != old(key)

    out: Dict[str, List[str]] = {"cli": [], "ftp": []}
    if changed("cli_port"):
        out["cli"].append("порт CLI")
    if changed("ftp_port"):
        out["ftp"].append("порт FTP Backup")
    if changed("cli_tls") and old("cli_tls") == "tls" and new("cli_tls") != "tls":
        out["cli"].append("CLI без SSL")
    if changed("ftp_tls") and new("ftp_tls") == "none" and (old("ftp_tls") or "none") != "none":
        out["ftp"].append("FTP Backup без шифрования")
    if changed("tls_verify") and old("tls_verify") and not new("tls_verify"):
        out["cli"].append("проверка сертификата выключается")
        out["ftp"].append("проверка сертификата выключается")
    if changed("allow_plaintext") and not old("allow_plaintext") and new("allow_plaintext"):
        out["cli"].append("разрешено без шифрования")
        out["ftp"].append("разрешено без шифрования")
    for chan, key, label in (("cli", "cli_cert_sha256", "CLI"), ("ftp", "ftp_cert_sha256", "FTP Backup")):
        if changed(key):
            before, after = normalize_fingerprint(old(key) or ""), normalize_fingerprint(new(key) or "")
            if after != before and (after or not new("tls_verify")):
                out[chan].append(f"отпечаток сертификата {label}")
    return out


def passwords_to_reenter(old: Callable[[str], Any], new_values: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(что меняется, какие сохранённые пароли нужно ввести заново в том же сохранении)."""
    weak = connection_weakening(old, new_values)
    what = list(dict.fromkeys(weak["cli"] + weak["ftp"]))
    if not what:
        return [], []
    need: List[str] = []
    ftp_own = bool(old("ftp_password"))       # у FTP Backup свой пароль — по FTP уходит он
    if (weak["cli"] or (weak["ftp"] and not ftp_own)) and old("admin_password") \
            and "axigen.admin_password" not in new_values:
        need.append("пароль администратора Axigen")
    if weak["ftp"] and ftp_own and "axigen.ftp_password" not in new_values:
        need.append("пароль FTP Backup")
    return what, need
