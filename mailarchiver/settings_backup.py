"""
Резервная копия настроек (1.26.0): все настройки службы одним файлом — и восстановление из него.

Разделы копии (:data:`SECTIONS`):

* ``settings`` — параметры раздела «Настройки»: действующие значения (из базы и config.yaml);
* ``mail_servers`` — подключения к почтовым серверам;
* ``employees`` — справочник сотрудников;
* ``accounts`` — ящики, их расписания, доступ к чужим ящикам;
* ``users`` — учётные записи интерфейса (хеши паролей, двухфакторный вход) и API-токены;
* ``webmail`` — личные настройки почтового клиента: отправитель и подпись, правила, автоответ, метки,
  шаблоны, адресная книга, сохранённые поиски, сертификаты S/MIME;
* ``config`` — текст config.yaml (при восстановлении не применяется — его можно извлечь в файл).

Писем, индекса, истории заданий, аудита и дел в копии нет — для них снимки базы и копия вне сервера.
Расписания действий на почтовом сервере (удаление старых писем и т. п.) тоже не переносятся: их создают
заново с подтверждением.

Без пароля в файл не попадают секреты (пароли ящиков, подключений и почты для уведомлений, токены,
закрытые ключи S/MIME) и разделы ``users`` и ``config``. С паролем файл целиком зашифрован (scrypt +
AES-256-GCM) и содержит всё: секреты внутри — открытым текстом под шифрованием файла, поэтому копия
переносится на другой сервер со своим secret.key.

Восстановление ничего не удаляет. Режим ``add`` добавляет то, чего нет (параметры — только те, что здесь не
меняли), ``update`` — ещё и переписывает существующее значениями из копии. Перед восстановлением служба
сохраняет копию нынешних настроек (зашифрована ключом из secret.key, :data:`AUTO_DIR`) — к ней можно
вернуться тем же восстановлением. Предпросмотр (``dry_run``) выполняет то же самое и откатывает транзакцию:
что показано, то и будет сделано.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .errors import MailArchiverError, ValidationError
from .logging_setup import get_logger
from .util import atomic_write_bytes, ensure_dir, utcnow_iso
from .version import __version__

log = get_logger("settings_backup")

FORMAT = "mailarchiver-settings"
FORMAT_VERSION = 1
#: Предел файла копии (загрузка в интерфейсе) и распакованного содержимого (защита от «бомбы»).
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024 * 1024
MIN_PASSWORD = 10
#: Копии, которые служба делает сама перед восстановлением: каталог в каталоге данных и сколько хранить.
AUTO_DIR = "settings-backups"
AUTO_KEEP = 10
#: before-restore — перед восстановлением; daily — ежедневная копия с паролем копий настроек (уходит в копию вне
#: сервера), daily-local — ежедневная без пароля, ключом secret.key (только на сервере); 1.27.0.
_AUTO_RE = re.compile(r"^(before-restore|daily-local|daily)-(\d{8}-\d{6})(?:-(\d{1,3}))?\.json$")
DAILY_KINDS = ("daily", "daily-local")
#: Ежедневная копия — не чаще, чем раз в столько секунд (её делают снимок базы и копия вне сервера).
DAILY_EVERY_S = 20 * 3600
#: scrypt: ~32 МБ памяти и доля секунды на попытку — перебор паролей дорог.
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
NOTES_LIMIT = 40

SECTIONS: Dict[str, str] = {
    "settings": "Параметры (раздел «Настройки»)",
    "mail_servers": "Подключения к почтовым серверам",
    "employees": "Сотрудники",
    "accounts": "Ящики, их расписания и доступ к чужим ящикам",
    "users": "Пользователи и API-токены",
    "webmail": "Личные настройки почтового клиента",
    "config": "Файл config.yaml",
}
#: Только в копии с паролем: хеши паролей, токены, файл конфигурации (в нём бывают пароли).
PROTECTED_ONLY = ("users", "config")
MODES = ("add", "update")


class BackupError(ValidationError):
    """Файл не копия, повреждён, неверный пароль — понятная ошибка для интерфейса и командной строки."""


# =====================================================================
#  Описание таблиц
# =====================================================================
@dataclass
class Col:
    db: str                      # столбец в базе
    name: str = ""               # имя в копии ('' — как в базе)
    kind: str = "plain"          # plain | secret (зашифрован ключом службы) | ref (номер → ключ) | owner
    ref: str = ""                # для ref: на что ссылается (accounts | employees | mail_servers | users)
    compare: bool = True         # False — только при добавлении (даты создания и т. п.)

    def __post_init__(self):
        if not self.name:
            self.name = self.db


@dataclass
class Spec:
    table: str
    section: str
    title: str
    cols: List[Col]
    key: Callable[[Dict[str, Any]], Optional[tuple]]
    pk: Tuple[str, ...] = ("id",)
    protected: bool = False      # строки — только в копии с паролем
    required: Tuple[str, ...] = ()   # ссылки, без которых строка не имеет смысла (имена в копии)
    stamps: Tuple[str, ...] = ()     # столбцы «сейчас» при добавлении, если в копии их нет
    where: str = ""              # отбор строк при выгрузке (SQL)

    def col(self, name: str) -> Optional[Col]:
        return next((c for c in self.cols if c.name == name), None)


def _low(value) -> str:
    return str(value or "").strip().lower()


def _ref(value) -> Optional[tuple]:
    if value is None or isinstance(value, dict):
        return None
    if isinstance(value, (list, tuple)):
        return tuple(_ref(v) if isinstance(v, (list, tuple)) else (v if isinstance(v, (str, int, float, bool))
                                                                    or v is None else str(v)) for v in value)
    return (value,)


def _employee_key(r):
    if str(r.get("external_id") or "").strip():
        return ("ext", str(r["external_id"]).strip())
    if _low(r.get("email")):
        return ("email", _low(r.get("email")))
    if _low(r.get("full_name")):
        return ("name", _low(r.get("full_name")))
    return None


def _owner_key(value) -> Optional[tuple]:
    if isinstance(value, dict):
        for kind in ("employee", "account"):
            if kind in value:
                return (kind, _ref(value[kind]))
        if "user" in value:
            return ("user", _low(value["user"]))
    return None


SPECS: List[Spec] = [
    Spec("mail_servers", "mail_servers", "Подключения к почтовым серверам", [
        Col("type"), Col("name"), Col("settings"), Col("secrets_enc", "secrets", "secret"), Col("enabled"),
        Col("allow_changes"), Col("sync_enabled"), Col("sync_cron"), Col("sync_add_new"), Col("monitor"),
        Col("created_at", compare=False)],
        key=lambda r: (_low(r.get("type")), str(r.get("name") or "").strip()), stamps=("created_at", "updated_at")),
    Spec("employees", "employees", "Сотрудники", [
        Col("external_id"), Col("full_name"), Col("email"), Col("position"), Col("department"), Col("phone"),
        Col("status"), Col("notes"), Col("source"), Col("had_account"), Col("dismissed_at"), Col("known_logins"),
        Col("created_at", compare=False)],
        key=_employee_key, stamps=("created_at", "updated_at")),
    Spec("accounts", "accounts", "Ящики", [
        Col("name"), Col("host"), Col("port"), Col("username"), Col("password_enc", "password", "secret"),
        Col("auth_type"), Col("security"), Col("enabled"), Col("folder_include"), Col("folder_exclude"),
        Col("oauth_client_id"), Col("oauth_client_secret_enc", "oauth_client_secret", "secret"),
        Col("oauth_refresh_token_enc", "oauth_refresh_token", "secret"), Col("oauth_token_url"), Col("notes"),
        Col("retention_days"), Col("hold_until"), Col("hold_reason"), Col("dismissed_at"), Col("auto_disabled"),
        Col("server_id", "server", "ref", "mail_servers"), Col("employee_id", "employee", "ref", "employees"),
        Col("created_at", compare=False)],
        key=lambda r: (_low(r.get("host")), _low(r.get("username"))), stamps=("created_at", "updated_at")),
    Spec("schedules", "accounts", "Расписания", [
        Col("account_id", "account", "ref", "accounts"), Col("kind"), Col("job_type"), Col("cron_expr"),
        Col("interval_seconds"), Col("enabled"), Col("options"), Col("created_at", compare=False)],
        key=lambda r: None, stamps=("created_at",), where="job_type <> 'server_action'"),
    Spec("mailbox_delegations", "accounts", "Доступ к чужим ящикам", [
        Col("account_id", "account", "ref", "accounts"), Col("employee_id", "employee", "ref", "employees"),
        Col("rights"), Col("until"), Col("note"), Col("granted_by", compare=False), Col("granted_at", compare=False)],
        key=lambda r: (_ref(r.get("account")), _ref(r.get("employee"))), required=("account", "employee"),
        stamps=("granted_at",)),
    Spec("users", "users", "Пользователи", [
        Col("username"), Col("password_hash"), Col("role"), Col("disabled"), Col("disabled_by"),
        Col("auth_source"), Col("totp_enabled"), Col("totp_secret_enc", "totp_secret", "secret"),
        Col("totp_recovery"), Col("sso_subject"), Col("last_sso_at", compare=False),
        Col("created_at", compare=False)],
        key=lambda r: (_low(r.get("username")),), protected=True, stamps=("created_at",)),
    Spec("api_tokens", "users", "API-токены", [
        Col("user_id", "user", "ref", "users"), Col("name"), Col("token_hash"), Col("hint"), Col("role"),
        Col("expires_at"), Col("revoked_at"), Col("created_by", compare=False), Col("created_at", compare=False)],
        key=lambda r: (str(r.get("token_hash") or ""),), protected=True, required=("user",),
        stamps=("created_at",)),
    Spec("webmail_identities", "webmail", "Отправитель и подпись", [
        Col("account_id", "account", "ref", "accounts"), Col("display_name"), Col("address"), Col("signature"),
        Col("reply_to"), Col("smtp_host"), Col("smtp_port"), Col("smtp_security"), Col("smtp_auth"),
        Col("smtp_username"), Col("smtp_password", "smtp_password", "secret"), Col("save_sent"),
        Col("updated_at", compare=False)],
        key=lambda r: (_ref(r.get("account")),), pk=("account_id",), required=("account",), stamps=("updated_at",)),
    Spec("webmail_rules", "webmail", "Правила", [
        Col("account_id", "account", "ref", "accounts"), Col("name"), Col("kind"), Col("enabled"), Col("position"),
        Col("match"), Col("conditions"), Col("actions"), Col("stop"), Col("created_by", compare=False),
        Col("created_at", compare=False)],
        key=lambda r: (_ref(r.get("account")), str(r.get("kind") or ""), str(r.get("name") or "")),
        required=("account",), stamps=("created_at",)),
    Spec("webmail_autoreply", "webmail", "Автоответ", [
        Col("account_id", "account", "ref", "accounts"), Col("enabled"), Col("date_from"), Col("date_to"),
        Col("subject"), Col("body"), Col("days"), Col("updated_by", compare=False), Col("updated_at", compare=False)],
        key=lambda r: (_ref(r.get("account")),), pk=("account_id",), required=("account",), stamps=("updated_at",)),
    Spec("webmail_tags", "webmail", "Метки", [
        Col("account_id", "account", "ref", "accounts"), Col("keyword"), Col("name"), Col("color"),
        Col("position")],
        key=lambda r: (_ref(r.get("account")), str(r.get("keyword") or "")), required=("account",)),
    Spec("webmail_templates", "webmail", "Шаблоны", [
        Col("owner", "owner", "owner"), Col("name"), Col("subject"), Col("body"), Col("updated_at", compare=False)],
        key=lambda r: (_owner_key(r.get("owner")), str(r.get("name") or "")), required=("owner",),
        stamps=("updated_at",)),
    Spec("webmail_book", "webmail", "Адресная книга", [
        Col("account_id", "account", "ref", "accounts"), Col("email"), Col("name"), Col("phone"), Col("company"),
        Col("note"), Col("groups"), Col("updated_at", compare=False)],
        key=lambda r: (_ref(r.get("account")), _low(r.get("email"))), required=("account",), stamps=("updated_at",)),
    Spec("webmail_searches", "webmail", "Сохранённые поиски", [
        Col("owner", "owner", "owner"), Col("name"), Col("q"), Col("scope"), Col("attach"),
        Col("created_at", compare=False)],
        key=lambda r: (_owner_key(r.get("owner")), str(r.get("name") or "")), required=("owner",),
        stamps=("created_at",)),
    Spec("webmail_smime_certs", "webmail", "Сертификаты получателей S/MIME", [
        Col("email"), Col("cert_pem"), Col("name"), Col("issuer"), Col("valid_until"), Col("source"),
        Col("fingerprint"), Col("prev_fingerprint"), Col("changed_at", compare=False),
        Col("updated_at", compare=False)],
        key=lambda r: (_low(r.get("email")),), pk=("email",), stamps=("updated_at",)),
    Spec("webmail_smime_user_certs", "webmail", "Личные сертификаты получателей S/MIME", [
        Col("account_id", "account", "ref", "accounts"), Col("email"), Col("cert_pem"), Col("name"), Col("issuer"),
        Col("valid_until"), Col("fingerprint"), Col("updated_at", compare=False)],
        key=lambda r: (_ref(r.get("account")), _low(r.get("email"))), pk=("account_id", "email"),
        required=("account",), stamps=("updated_at",)),
    Spec("webmail_smime_keys", "webmail", "Ключи S/MIME ящиков", [
        Col("account_id", "account", "ref", "accounts"), Col("cert_pem"), Col("chain_pem"),
        Col("key_enc", "key_pem", "secret"), Col("name"), Col("emails"), Col("issuer"), Col("valid_until"),
        Col("uploaded_by", compare=False), Col("uploaded_at", compare=False)],
        key=lambda r: (_ref(r.get("account")),), pk=("account_id",), protected=True, required=("account",),
        stamps=("uploaded_at",)),
]
SPEC_BY_TABLE = {s.table: s for s in SPECS}
#: Без этих полей строку не добавить (NOT NULL в базе, пустые — бессмысленны).
NEED = {"mail_servers": ("type", "name"), "employees": ("full_name",), "accounts": ("name", "host", "username"),
        "users": ("username", "role"), "api_tokens": ("name", "token_hash"), "webmail_rules": ("name",),
        "webmail_tags": ("name", "keyword"), "webmail_templates": ("name",), "webmail_book": ("email",),
        "webmail_searches": ("name", "q"), "webmail_smime_certs": ("email", "cert_pem"),
        "webmail_smime_user_certs": ("email", "cert_pem"), "webmail_smime_keys": ("cert_pem",),
        "mailbox_delegations": ("rights",)}
#: Таблицы, на строки которых ссылаются другие: номер строки ↔ ключ.
REF_TABLES = ("mail_servers", "employees", "accounts", "users")


# =====================================================================
#  Чтение базы
# =====================================================================
def _table_columns(db, table: str) -> set:
    try:
        if getattr(db, "is_pg", False):
            rows = db.query("SELECT column_name FROM information_schema.columns WHERE table_schema = "
                            "current_schema() AND table_name = ?", (table,))
            return {str(r[0]) for r in rows}
        return {str(r[1]) for r in db.query(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001 - таблицы нет в очень старой базе
        return set()


class _Refs:
    """Номер строки ↔ ключ для таблиц, на которые ссылаются (по одной базе)."""

    def __init__(self):
        self.key_of: Dict[str, Dict[int, tuple]] = {t: {} for t in REF_TABLES}
        self.id_of: Dict[str, Dict[tuple, int]] = {t: {} for t in REF_TABLES}

    def add(self, table: str, row_id: int, key: Optional[tuple]) -> None:
        if key is None:
            return
        self.key_of[table][int(row_id)] = key
        self.id_of[table].setdefault(key, int(row_id))


def _decrypt(box, value) -> Tuple[str, bool]:
    if not value:
        return "", True
    try:
        return box.decrypt(str(value)), True
    except Exception:  # noqa: BLE001 - ключ сменился: секрет не переносится
        return "", False


def _owner_form(value: str, refs: _Refs):
    text = str(value or "")
    m = re.fullmatch(r"mailbox:(emp|acc):(\d+)", text)
    if m:
        table = "employees" if m.group(1) == "emp" else "accounts"
        key = refs.key_of[table].get(int(m.group(2)))
        return {("employee" if table == "employees" else "account"): list(_jsonable(key))} if key else None
    return {"user": text} if text else None


def _jsonable(value):
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    return value


def _form(spec: Spec, row, refs: _Refs, box, *, secrets_too: bool,
          broken: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """Строка базы → строка копии (ссылки — ключами, секреты — расшифрованы)."""
    out: Dict[str, Any] = {}
    keys = set(row.keys())
    for col in spec.cols:
        if col.db not in keys:
            continue
        value = row[col.db]
        if col.kind == "secret":
            if not secrets_too:
                continue
            plain, ok = _decrypt(box, value)
            if not ok and broken is not None:
                broken.append(f"{spec.title}: секрет «{col.name}» не расшифровывается ключом secret.key — не скопирован")
                continue
            out[col.name] = plain
        elif col.kind == "ref":
            key = refs.key_of[col.ref].get(int(value)) if value is not None else None
            out[col.name] = list(_jsonable(key)) if key is not None else None
        elif col.kind == "owner":
            out[col.name] = _owner_form(value, refs)
        else:
            out[col.name] = value
    return out


def _rows(db, spec: Spec) -> list:
    if not _table_columns(db, spec.table):
        return []
    where = f" WHERE {spec.where}" if spec.where else ""
    return db.query(f"SELECT * FROM {spec.table}{where}")


def _build_refs(db, box) -> _Refs:
    refs = _Refs()
    # порядок важен: ящик ссылается на подключение и сотрудника
    for table in REF_TABLES:
        spec = SPEC_BY_TABLE[table]
        for row in _rows(db, spec):
            form = _form(spec, row, refs, box, secrets_too=False)
            refs.add(table, int(row["id"]), spec.key(form))
    return refs


# =====================================================================
#  Создание копии
# =====================================================================
def editable_settings() -> List[str]:
    from .config import DEFAULTS, FILE_ONLY_SETTINGS, UNSUPPORTED_SETTINGS
    out = []
    for section, values in DEFAULTS.items():
        if not isinstance(values, dict):
            continue
        for key in values:
            full = f"{section}.{key}"
            if full not in FILE_ONLY_SETTINGS and full not in UNSUPPORTED_SETTINGS:
                out.append(full)
    return out


def _rt(svc):
    """Действующее значение параметра: у службы — её ``rt``, у командной строки — база, затем config.yaml."""
    rt = getattr(svc, "rt", None)
    if callable(rt):
        return rt

    def read(section, key):
        value = svc.db.get_setting(f"{section}.{key}", None)
        return value if value is not None else svc.cfg.get(section, key)
    return read


def _secret_settings() -> set:
    from .database import _ENCRYPTED_SETTINGS
    return set(_ENCRYPTED_SETTINGS)


def collect(svc, *, sections: Optional[Iterable[str]] = None, protected: bool = False) -> Dict[str, Any]:
    """Содержимое копии: ``{"sections": {...}, "notes": [...], "counts": {...}}``."""
    chosen = _sections(sections, protected=protected)
    db, box = svc.db, svc.db.secret
    refs = _build_refs(db, box)
    notes: List[str] = []
    out: Dict[str, Any] = {}
    counts: Dict[str, int] = {}
    if "settings" in chosen:
        rt = _rt(svc)
        secret_keys = _secret_settings()
        values = {}
        for full in editable_settings():
            if full in secret_keys and not protected:
                continue
            section, key = full.split(".", 1)
            try:
                value = rt(section, key)
            except Exception:  # noqa: BLE001
                continue
            if full in secret_keys and not value:
                continue
            values[full] = value
        out["settings"] = values
        counts["settings"] = len(values)
    for spec in SPECS:
        if spec.section not in chosen or (spec.protected and not protected):
            continue
        rows = []
        for row in _rows(db, spec):
            form = _form(spec, row, refs, box, secrets_too=protected, broken=notes)
            if form is not None:
                rows.append(form)
        rows.sort(key=lambda r: json.dumps(_jsonable(spec.key(r)) if spec.table != "schedules" else r,
                                           ensure_ascii=False, sort_keys=True, default=str))
        out.setdefault(spec.section, {})[spec.table] = rows
        counts[spec.table] = len(rows)
    if "config" in chosen:
        path = getattr(svc.cfg, "source_path", None)
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                out["config"] = {"path": path, "text": fh.read()}
        else:
            notes.append("Файла config.yaml нет — служба работает на значениях по умолчанию.")
    if not protected:
        notes.append("Копия без пароля: в ней нет паролей ящиков и подключений, токенов, ключей S/MIME, "
                     "учётных записей интерфейса и файла config.yaml.")
    return {"sections": out, "notes": notes[:NOTES_LIMIT], "counts": counts}


def _sections(sections: Optional[Iterable[str]], *, protected: bool) -> List[str]:
    if sections is None:
        chosen = list(SECTIONS)
    else:
        chosen = []
        for name in sections:
            name = str(name).strip()
            if not name:
                continue
            if name not in SECTIONS:
                raise ValidationError(f"Неизвестный раздел копии: «{name}».",
                                      hint="Разделы: " + ", ".join(SECTIONS) + ".")
            if name not in chosen:
                chosen.append(name)
    if not protected:
        chosen = [s for s in chosen if s not in PROTECTED_ONLY]
    if not chosen:
        raise ValidationError("Не выбрано ни одного раздела.",
                              hint="Пользователи и config.yaml сохраняются только в копии с паролем.")
    return chosen


def _gz(payload: Dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def _gunzip(data: bytes) -> Dict[str, Any]:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
        while True:
            chunk = gz.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            if out.tell() > MAX_PAYLOAD_BYTES:
                raise BackupError("Копия слишком большая после распаковки.")
    return json.loads(out.getvalue().decode("utf-8"))


def _aad(head: Dict[str, Any]) -> bytes:
    return json.dumps(head, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _password_key(password: str, kdf: Dict[str, Any]) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    if n not in (2 ** 14, 2 ** 15, 2 ** 16, 2 ** 17) or not 1 <= r <= 16 or not 1 <= p <= 4:
        raise BackupError("Копия повреждена: недопустимые параметры шифрования.")
    salt = base64.b64decode(kdf["salt"])
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _local_key(box) -> bytes:
    return box.derive("settings-backup-local-v1")


def _kid(key: bytes) -> str:
    return hashlib.sha256(b"mailarchiver-settings-kid:" + key).hexdigest()[:16]


def _source(svc) -> Dict[str, Any]:
    return {"host": socket.gethostname(), "database": "postgresql" if getattr(svc.db, "is_pg", False) else "sqlite"}


def build(svc, *, password: str = "", sections: Optional[Iterable[str]] = None, local: bool = False
          ) -> Tuple[bytes, Dict[str, Any]]:
    """Файл копии (байты JSON) и сводка. ``password`` — копия с паролем (всё, с секретами); ``local`` —
    зашифрована ключом из secret.key (копия перед восстановлением)."""
    protected = bool(password) or local
    if password and len(password) < MIN_PASSWORD:
        raise ValidationError(f"Пароль копии — не короче {MIN_PASSWORD} символов.",
                              hint="Без пароля не сохранить ни паролей, ни учётных записей; запишите пароль "
                                   "отдельно от копии.")
    content = collect(svc, sections=sections, protected=protected)
    created = utcnow_iso()
    head: Dict[str, Any] = {"format": FORMAT, "version": FORMAT_VERSION, "created_at": created,
                            "app_version": __version__}
    payload = {"sections": content["sections"], "source": _source(svc), "notes": content["notes"],
               "counts": content["counts"], "secrets": protected}
    if not protected:
        doc = dict(head, encrypted=False, **payload)
        data = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True, default=str).encode("utf-8")
    else:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if local:
            key = _local_key(svc.db.secret)
            kdf: Dict[str, Any] = {"name": "local", "kid": _kid(key)}
        else:
            kdf = dict(_SCRYPT, name="scrypt", salt=base64.b64encode(secrets.token_bytes(16)).decode())
            key = _password_key(password, kdf)
        nonce = secrets.token_bytes(12)
        head.update(encrypted=True, kdf=kdf, cipher="aes-256-gcm", nonce=base64.b64encode(nonce).decode())
        sealed = AESGCM(key).encrypt(nonce, _gz(payload), _aad(head))
        doc = dict(head, data=base64.b64encode(sealed).decode())
        data = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    summary = {"created_at": created, "encrypted": protected, "counts": content["counts"],
               "notes": content["notes"], "size": len(data),
               "filename": f"mailarchiver-settings-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"}
    return data, summary


# =====================================================================
#  Чтение копии
# =====================================================================
def describe(data: bytes) -> Dict[str, Any]:
    """Заголовок копии без расшифровки: когда, какой версией, с паролем ли."""
    doc = _parse(data)
    kdf = doc.get("kdf") or {}
    return {"created_at": doc.get("created_at", ""), "app_version": doc.get("app_version", ""),
            "encrypted": bool(doc.get("encrypted")), "local": kdf.get("name") == "local",
            "needs_password": bool(doc.get("encrypted")) and kdf.get("name") != "local"}


def _parse(data: bytes) -> Dict[str, Any]:
    if not data:
        raise BackupError("Файл пуст.")
    if len(data) > MAX_FILE_BYTES:
        raise BackupError("Файл слишком большой для копии настроек.")
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise BackupError("Это не копия настроек MailArchiver (файл не JSON).",
                          hint="Выберите файл mailarchiver-settings-….json.") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise BackupError("Это не копия настроек MailArchiver.", hint="Выберите файл mailarchiver-settings-….json.")
    try:
        version = int(doc.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != FORMAT_VERSION:
        raise BackupError(f"Копия сделана в формате {doc.get('version')!r}, эта версия понимает {FORMAT_VERSION}.",
                          hint="Восстановите копию в той версии MailArchiver, которой она сделана, или новее.")
    return doc


def open_backup(data: bytes, *, password: str = "", box=None) -> Dict[str, Any]:
    """Прочитать копию: ``{"head": …, "sections": …, "secrets": bool, "source": …}``."""
    doc = _parse(data)
    head = {k: doc.get(k) for k in ("format", "version", "created_at", "app_version")}
    if not doc.get("encrypted"):
        sections = doc.get("sections")
        if not isinstance(sections, dict):
            raise BackupError("Копия повреждена: нет разделов.")
        return {"head": head, "sections": sections, "secrets": False, "source": doc.get("source") or {},
                "notes": doc.get("notes") or []}
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kdf = doc.get("kdf") if isinstance(doc.get("kdf"), dict) else {}
    head.update(encrypted=True, kdf=kdf, cipher=doc.get("cipher"), nonce=doc.get("nonce"))
    if doc.get("cipher") != "aes-256-gcm":
        raise BackupError("Копия повреждена: неизвестный способ шифрования.")
    try:
        if kdf.get("name") == "local":
            if box is None:
                raise BackupError("Эта копия зашифрована ключом службы (secret.key), а он не загружен.")
            key = _local_key(box)
            if _kid(key) != kdf.get("kid"):
                raise BackupError("Эта копия зашифрована другим ключом secret.key — сделана на другом сервере "
                                  "или до смены ключа.", hint="Восстановите её там, где она сделана.")
        elif kdf.get("name") == "scrypt":
            if not password:
                raise BackupError("Копия защищена паролем — введите его.", code="password_required")
            key = _password_key(password, kdf)
        else:
            raise BackupError("Копия повреждена: неизвестный способ шифрования.")
        nonce = base64.b64decode(str(doc.get("nonce") or ""), validate=True)
        sealed = base64.b64decode(str(doc.get("data") or ""), validate=True)
    except (KeyError, TypeError, ValueError):
        raise BackupError("Копия повреждена: заголовок шифрования не читается.") from None
    try:
        raw = AESGCM(key).decrypt(nonce, sealed, _aad(head))
    except InvalidTag:
        if kdf.get("name") == "local":
            raise BackupError("Копия повреждена: проверка целостности не прошла.") from None
        raise BackupError("Неверный пароль копии (или файл повреждён).", code="password_wrong",
                          hint="Пароль задавали при создании копии; без него копию не прочитать.") from None
    try:
        payload = _gunzip(raw)
    except (OSError, EOFError, ValueError) as exc:
        raise BackupError(f"Копия повреждена: {exc}") from None
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, dict):
        raise BackupError("Копия повреждена: нет разделов.")
    return {"head": head, "sections": sections, "secrets": bool(payload.get("secrets")),
            "source": payload.get("source") or {}, "notes": payload.get("notes") or [],
            "local": kdf.get("name") == "local"}


# =====================================================================
#  Восстановление
# =====================================================================
@dataclass
class _Tally:
    title: str
    added: int = 0
    updated: int = 0
    same: int = 0
    kept: int = 0         # есть здесь, режим «только недостающее» — не трогали
    skipped: int = 0      # не восстановлено (нет ящика, сотрудника…)
    notes: List[str] = field(default_factory=list)
    names: List[str] = field(default_factory=list)   # что добавлено/изменено (первые)

    def note(self, text: str) -> None:
        if len(self.notes) < NOTES_LIMIT and text not in self.notes:
            self.notes.append(text)

    def name(self, text: str) -> None:
        if len(self.names) < NOTES_LIMIT:
            self.names.append(text)

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "added": self.added, "updated": self.updated, "same": self.same,
                "kept": self.kept, "skipped": self.skipped, "notes": self.notes, "names": self.names}


class _DryRun(Exception):
    pass


def _norm(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if value is None:
        return ""
    return value


def _label(spec: Spec, form: Dict[str, Any]) -> str:
    for name in ("username", "full_name", "name", "email", "keyword"):
        if form.get(name):
            text = str(form[name])
            acc = form.get("account")
            if acc and spec.table != "accounts":
                text += f" ({acc[-1] if isinstance(acc, list) and acc else acc})"
            return text
    acc = form.get("account")
    if acc:
        return str(acc[-1] if isinstance(acc, list) and acc else acc)
    return spec.title


class _Restorer:
    def __init__(self, svc, conn, data: Dict[str, Any], *, mode: str, actor: str):
        self.svc, self.conn, self.mode, self.actor = svc, conn, mode, actor
        self.box = svc.db.secret
        self.data = data
        self.secrets = bool(data.get("secrets"))
        self.refs = _build_refs(svc.db, self.box)
        self.tallies: Dict[str, _Tally] = {}
        self.sessions_users: set = set()
        self.mailbox_logout: set = set()
        self.push_forget: set = set()
        self.tags_seeded: set = set()
        self.dismissed: set = set()          # сотрудники, которых копия отметила уволенными

    # -- общее ---------------------------------------------------------------------------------------
    def tally(self, spec: Spec) -> _Tally:
        if spec.table not in self.tallies:
            self.tallies[spec.table] = _Tally(spec.title)
        return self.tallies[spec.table]

    def _resolve(self, spec: Spec, form: Dict[str, Any], t: _Tally) -> Optional[Dict[str, Any]]:
        """Ссылки копии → номера строк этой базы; ``None`` — строку пропустить."""
        values: Dict[str, Any] = {}
        for col in spec.cols:
            if col.name not in form:
                continue
            value = form[col.name]
            if col.kind == "ref":
                key = _ref(value)
                if key is None:
                    values[col.db] = None
                    continue
                target = self.refs.id_of[col.ref].get(key)
                if target is None:
                    what = {"accounts": "ящика", "employees": "сотрудника", "mail_servers": "подключения",
                            "users": "пользователя"}[col.ref]
                    shown = key[-1] if key else "?"
                    if col.name in spec.required or (spec.table == "accounts" and col.name == "server"
                                                      and form.get("auth_type") == "server"):
                        t.note(f"Нет {what} «{shown}» — «{_label(spec, form)}» пропущено.")
                        return None
                    t.note(f"Нет {what} «{shown}» — у «{_label(spec, form)}» связь не восстановлена.")
                    values[col.db] = None
                    continue
                values[col.db] = target
            elif col.kind == "owner":
                okey = _owner_key(value)
                owner = None
                if okey and okey[0] == "user":
                    owner = str(value.get("user") or "").strip()      # как было: регистр имени важен
                elif okey:
                    table = "employees" if okey[0] == "employee" else "accounts"
                    rid = self.refs.id_of[table].get(okey[1])
                    if rid is not None:
                        owner = f"mailbox:{'emp' if table == 'employees' else 'acc'}:{rid}"
                if not owner:
                    t.note(f"Нет владельца для «{_label(spec, form)}» — пропущено.")
                    return None
                values[col.db] = owner
            elif col.kind == "secret":
                if not self.secrets:
                    continue
                values[col.db] = self.box.encrypt(str(value)) if value else ""
            elif isinstance(value, (dict, list)):
                values[col.db] = json.dumps(value, ensure_ascii=False)      # в базе такие поля — текст JSON
            elif isinstance(value, bool):
                values[col.db] = int(value)
            else:
                values[col.db] = value
        return values

    def _insert(self, spec: Spec, values: Dict[str, Any], present: set) -> Optional[int]:
        now = utcnow_iso()
        for stamp in spec.stamps:
            if stamp in present and not values.get(stamp):
                values[stamp] = now
        cols = [c for c in values if c in present]
        sql = f"INSERT INTO {spec.table}({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"
        cur = self.conn.execute(sql, tuple(values[c] for c in cols))
        return int(cur.lastrowid) if "id" in spec.pk and cur.lastrowid is not None else None

    def _update(self, spec: Spec, where: Dict[str, Any], values: Dict[str, Any]) -> None:
        sets = ", ".join(f"{c}=?" for c in values)
        cond = " AND ".join(f"{c}=?" for c in where)
        self.conn.execute(f"UPDATE {spec.table} SET {sets} WHERE {cond}",
                          tuple(values.values()) + tuple(where.values()))

    # -- таблицы -------------------------------------------------------------------------------------
    def table(self, spec: Spec, rows: List[Dict[str, Any]]) -> None:
        t = self.tally(spec)
        present = _table_columns(self.svc.db, spec.table)
        if not present:
            t.note("Таблицы нет в этой базе — раздел пропущен.")
            t.skipped += len(rows)
            return
        existing: Dict[tuple, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for row in self.conn.execute(f"SELECT * FROM {spec.table}").fetchall():
            form = _form(spec, row, self.refs, self.box, secrets_too=self.secrets)
            key = spec.key(form)
            if key is not None and key not in existing:
                existing[key] = (form, {c: row[c] for c in spec.pk})
        for form in rows:
            if not isinstance(form, dict):
                t.skipped += 1
                continue
            try:
                key = spec.key(form)
                hash(key)
                if key is None or (spec.required and any(form.get(r) in (None, "", []) for r in spec.required)):
                    t.skipped += 1
                    t.note(f"Строка без обязательных полей — пропущена ({_label(spec, form)}).")
                    continue
                values = self._resolve(spec, form, t)
            except (TypeError, ValueError, AttributeError):
                t.skipped += 1
                t.note("Строка копии повреждена — пропущена.")
                continue
            if values is None:
                t.skipped += 1
                continue
            label = _label(spec, form)
            if key in existing:
                old, where = existing[key]
                if self.mode != "update":
                    t.kept += 1
                    continue
                changed = {}
                for col in spec.cols:
                    if col.db not in values or not col.compare or col.db not in present:
                        continue
                    if col.kind == "secret":
                        if not self.secrets or _norm(form.get(col.name)) == _norm(old.get(col.name)):
                            continue
                    elif col.kind in ("ref", "owner"):
                        if _ref(form.get(col.name)) == _ref(old.get(col.name)) or \
                                (col.kind == "owner" and _owner_key(form.get(col.name)) ==
                                 _owner_key(old.get(col.name))):
                            continue
                    elif _norm(form.get(col.name)) == _norm(old.get(col.name)):
                        continue
                    changed[col.db] = values[col.db]
                if not changed:
                    t.same += 1
                    continue
                if not self._before_update(spec, form, old, where, changed, t):
                    continue
                if spec.table in ("accounts", "employees", "mail_servers") and "updated_at" in present:
                    changed["updated_at"] = utcnow_iso()
                self._update(spec, where, changed)
                t.updated += 1
                t.name(f"изменено: {label}")
                self._after_update(spec, form, old, where, changed)
            else:
                missing = [f for f in NEED.get(spec.table, ()) if not str(form.get(f) or "").strip()]
                if missing:
                    t.skipped += 1
                    t.note(f"Нет обязательных полей ({', '.join(missing)}) — «{_label(spec, form)}» пропущено.")
                    continue
                if not self._before_insert(spec, form, values, t):
                    continue
                new_id = self._insert(spec, dict(values), present)
                t.added += 1
                t.name(f"добавлено: {label}")
                if spec.table in REF_TABLES and new_id is not None:
                    self.refs.add(spec.table, new_id, key)
                existing[key] = (form, {"id": new_id} if new_id is not None else
                                 {c: values.get(c) for c in spec.pk})
                if spec.table == "webmail_tags":
                    self.tags_seeded.add(values.get("account_id"))

    def _before_insert(self, spec: Spec, form, values, t: _Tally) -> bool:
        if spec.table == "users" and not values.get("password_hash"):
            t.skipped += 1
            t.note(f"У «{form.get('username')}» в копии нет пароля — не добавлен.")
            return False
        if spec.table == "accounts":
            values.setdefault("password_enc", "")
        return True

    def _before_update(self, spec: Spec, form, old, where, changed, t: _Tally) -> bool:
        if spec.table == "users" and _low(form.get("username")) == _low(self.actor):
            t.kept += 1
            t.note(f"Ваша учётная запись «{self.actor}» не изменена — восстановление не меняет того, кто его "
                   f"выполняет.")
            return False
        return True

    def _after_update(self, spec: Spec, form, old, where, changed) -> None:
        if spec.table == "users" and set(changed) & {"password_hash", "role", "disabled", "totp_enabled",
                                                     "totp_secret_enc", "auth_source"}:
            self.sessions_users.add(int(where["id"]))
            if "totp_secret_enc" in changed:
                self.conn.execute("UPDATE users SET totp_last_step=0, totp_pending_enc='' WHERE id=?",
                                  (int(where["id"]),))
        if spec.table == "employees" and "status" in changed and changed["status"] != "active":
            self.dismissed.add(int(where["id"]))
        if spec.table == "accounts":
            acc_id = int(where["id"])
            if set(changed) & {"password_enc", "auth_type", "enabled", "employee_id", "server_id"}:
                self.mailbox_logout.add(acc_id)
            if "employee_id" in changed:
                self.push_forget.add(acc_id)

    # -- расписания: группа «ящик + вид задания» ------------------------------------------------------
    def schedules(self, rows: List[Dict[str, Any]]) -> None:
        spec = SPEC_BY_TABLE["schedules"]
        t = self.tally(spec)
        present = _table_columns(self.svc.db, "schedules")

        def sig(form):
            try:
                options = json.dumps(json.loads(form.get("options") or "{}"), sort_keys=True)
            except (TypeError, ValueError):
                options = str(form.get("options") or "")
            try:
                interval = int(form.get("interval_seconds") or 0)
            except (TypeError, ValueError):
                interval = -1
            return (str(form.get("kind") or ""), str(form.get("cron_expr") or "").strip(), interval, options)

        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for form in rows:
            if not isinstance(form, dict) or form.get("job_type") == "server_action":
                t.skipped += 1
                continue
            groups.setdefault((_ref(form.get("account")), str(form.get("job_type") or "")), []).append(form)
        target: Dict[tuple, List[Tuple[int, Dict[str, Any]]]] = {}
        for row in self.conn.execute("SELECT * FROM schedules WHERE job_type <> 'server_action' ORDER BY id"
                                     ).fetchall():
            form = _form(spec, row, self.refs, self.box, secrets_too=False)
            target.setdefault((_ref(form.get("account")), str(form.get("job_type") or "")), []).append(
                (int(row["id"]), form))
        for (acc_key, job_type), forms in groups.items():
            values_list = []
            for form in forms:
                values = self._resolve(spec, form, t)
                if values is None or (acc_key is not None and values.get("account_id") is None):
                    if values is not None:
                        t.note(f"Нет ящика «{acc_key[-1]}» — его расписание пропущено.")
                    t.skipped += 1
                    continue
                problem = _schedule_problem(values)
                if problem:
                    t.skipped += 1
                    t.note(f"Расписание «{job_type}» ({acc_key[-1] if acc_key else 'общее'}): {problem} — пропущено.")
                    continue
                values_list.append((form, values))
            if not values_list:
                continue
            have = list(target.get((acc_key, job_type), []))
            left = []
            for form, values in values_list:
                match = next((h for h in have if sig(h[1]) == sig(form)), None)
                if match is None:
                    left.append((form, values))
                    continue
                have.remove(match)
                if self.mode == "update" and _norm(match[1].get("enabled")) != _norm(form.get("enabled")):
                    self._update(spec, {"id": match[0]}, {"enabled": values.get("enabled")})
                    t.updated += 1
                else:
                    t.same += 1
            who = acc_key[-1] if acc_key else "общее"
            if not left:
                continue
            if self.mode != "update" and target.get((acc_key, job_type)):
                t.kept += len(left)
                t.note(f"«{who}»: своё расписание «{job_type}» уже есть — расписание из копии не добавлено.")
                continue
            for (form, values), (sid, _old) in zip(left, have):
                self._update(spec, {"id": sid}, {k: v for k, v in values.items()
                                                 if k in ("kind", "cron_expr", "interval_seconds", "enabled",
                                                          "options")})
                t.updated += 1
                t.name(f"изменено: {who} — {job_type}")
            for form, values in left[len(have):]:
                self._insert(spec, dict(values), present)
                t.added += 1
                t.name(f"добавлено: {who} — {job_type}")

    # -- итог ----------------------------------------------------------------------------------------
    def finish(self) -> None:
        if "users" in self.tallies:
            admins = int(self.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0")
                         .fetchone()[0] or 0)
            if not admins:
                raise ValidationError("После восстановления не осталось ни одного включённого администратора.",
                                      hint="Ничего не изменено. Восстановите копию без раздела «Пользователи».")
        for uid in self.sessions_users:
            self.conn.execute("DELETE FROM sessions WHERE user_id=? AND role<>'mailbox'", (uid,))
        for acc_id in self.mailbox_logout | self.push_forget:
            self.conn.execute("DELETE FROM sessions WHERE role='mailbox' AND account_id=?", (acc_id,))
        if self.push_forget and _table_columns(self.svc.db, "push_subscriptions"):
            marks = ",".join("?" * len(self.push_forget))
            self.conn.execute(f"DELETE FROM push_subscriptions WHERE login_account_id IN ({marks})",
                              tuple(self.push_forget))
        for acc_id in self.tags_seeded:
            if acc_id is None:
                continue
            self.conn.execute("INSERT INTO webmail_kv(account_id, key, value) VALUES(?,?,?) "
                              "ON CONFLICT(account_id, key) DO UPDATE SET value=excluded.value",
                              (int(acc_id), "tags_seeded", "1"))


def _schedule_problem(values: Dict[str, Any]) -> str:
    """Расписание из копии — те же проверки, что при создании в интерфейсе ('' — в порядке). Параметры
    задания приводятся к проверенным."""
    from .web.api_common import ScheduleBody
    from .web.api_jobs import GLOBAL_SCHEDULE_JOB_TYPES, _validate_schedule
    if (values.get("job_type") in GLOBAL_SCHEDULE_JOB_TYPES) != (values.get("account_id") is None):
        return "не указан ящик" if values.get("account_id") is None else "общее расписание привязано к ящику"
    try:
        options = json.loads(values.get("options") or "{}")
        body = ScheduleBody(account_id=values.get("account_id"), kind=str(values.get("kind") or ""),
                            job_type=str(values.get("job_type") or ""),
                            cron_expr=str(values.get("cron_expr") or "").strip(),
                            interval_seconds=int(values.get("interval_seconds") or 0),
                            enabled=bool(values.get("enabled", 1)), options=options if isinstance(options, dict)
                            else None)
        checked = _validate_schedule(body)
    except MailArchiverError as exc:
        return exc.message
    except (TypeError, ValueError) as exc:
        return f"не разбирается ({exc})"
    values["options"] = json.dumps(checked, ensure_ascii=False)
    values["cron_expr"] = body.cron_expr
    return ""


# -- параметры ------------------------------------------------------------------------------------------
_CRON_KEYS = ("employees.cron", "employees.account_schedule_cron", "retention.cron", "replica.cron",
              "notifications.summary_cron", "axigen.sync_cron", "drill.cron")


def plan_settings(svc, values: Dict[str, Any], *, mode: str) -> Tuple[Dict[str, Any], _Tally]:
    """Какие параметры поменять: ``({ключ: значение}, сводка)``. Проверки — как при сохранении в интерфейсе."""
    from .config import DEFAULTS, FILE_ONLY_SETTINGS, UNSUPPORTED_SETTINGS, coerce_setting
    t = _Tally(SECTIONS["settings"])
    rt = _rt(svc)
    secret_keys = _secret_settings()
    editable = set(editable_settings())
    plan: Dict[str, Any] = {}
    if not isinstance(values, dict):
        t.note("Раздел повреждён — пропущен.")
        return plan, t
    for full, value in sorted(values.items()):
        full = str(full)
        if full in FILE_ONLY_SETTINGS:
            t.skipped += 1
            t.note(f"«{full}» задаётся только в config.yaml — пропущен.")
            continue
        if full in UNSUPPORTED_SETTINGS or full not in editable:
            t.skipped += 1
            t.note(f"«{full}» этой версии неизвестен — пропущен.")
            continue
        try:
            coerced = coerce_setting(full, value)
            if full in _CRON_KEYS and coerced:
                from .accountops import validate_cron
                validate_cron(str(coerced))
        except ValueError as exc:
            t.skipped += 1
            t.note(f"{exc} — пропущен.")
            continue
        except MailArchiverError as exc:
            t.skipped += 1
            t.note(f"«{full}»: {exc.message} — пропущен.")
            continue
        section, key = full.split(".", 1)
        current = rt(section, key)
        if full in secret_keys:
            if not coerced:
                continue
        if coerced == current or (full in secret_keys and coerced == (current or "")):
            t.same += 1
            continue
        if mode != "update" and current != DEFAULTS[section][key] and not (full in secret_keys and not current):
            t.kept += 1
            continue
        if full == "replica.enabled" and coerced and not current:
            # Копия другой службы (перенос настроек на тестовый сервер): эта служба писала бы в чужую копию
            # и удаляла бы в ней то, чего нет у себя. Включают вручную, проверив место копии.
            t.kept += 1
            t.note("Копия вне сервера не включена: убедитесь, что место копии — для этой службы, и включите её "
                   "в «Настройки → Копия вне сервера».")
            continue
        plan[full] = coerced
        t.updated += 1
        t.name(f"{full}" + (" (секрет)" if full in secret_keys else ""))
    _guard_bound_secrets(rt, plan, t)
    return plan, t


def _guard_bound_secrets(rt, plan: Dict[str, Any], t: _Tally) -> None:
    """Адрес, на который уходит сохранённый пароль, меняется, а пароля в копии нет — пароль сбрасывается
    (как и в интерфейсе: пароль не уходит на новый адрес без того, кто его знает)."""
    from .web.api_common import SECRET_BOUND_TO
    for secret, bound in SECRET_BOUND_TO.items():
        if bound not in plan or secret in plan or not rt(*secret.split(".", 1)):
            continue
        old_host = _low(rt(*bound.split(".", 1)))
        if old_host and _low(plan[bound]) != old_host:
            plan[secret] = ""
            t.note(f"Адрес «{bound}» меняется, а пароля «{secret}» в копии нет — сохранённый пароль сброшен, "
                   f"введите его заново.")
    try:
        from .axigen.common import connection_weakening
    except Exception:  # noqa: BLE001
        return
    weak = connection_weakening(lambda k: rt("axigen", k), plan)
    if not (weak["cli"] or weak["ftp"]):
        return
    ftp_own = bool(rt("axigen", "ftp_password"))
    if (weak["cli"] or (weak["ftp"] and not ftp_own)) and rt("axigen", "admin_password") \
            and "axigen.admin_password" not in plan:
        plan["axigen.admin_password"] = ""
        t.note("Защита соединения с Axigen меняется — пароль администратора Axigen сброшен, введите его заново.")
    if weak["ftp"] and ftp_own and "axigen.ftp_password" not in plan:
        plan["axigen.ftp_password"] = ""
        t.note("Защита соединения FTP Backup меняется — пароль FTP сброшен, введите его заново.")


def _save_settings(svc, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Записать параметры. У работающей службы — как кнопка «Сохранить» (с применением на лету и откатом при
    ошибке), у командной строки — только в базу (служба подхватит после перезапуска)."""
    if hasattr(svc, "apply_runtime_settings"):
        from .web.api_settings import save_settings_values
        return save_settings_values(svc, plan)
    snapshot = svc.db.raw_settings(plan.keys())
    svc.db.set_settings_many(plan)
    return {"changed": sorted(plan), "snapshot": snapshot, "enc_state": {}}


def _undo_settings(svc, snapshot: Dict[str, Any]) -> None:
    svc.db.restore_settings(snapshot)
    if hasattr(svc, "apply_runtime_settings"):
        try:
            svc.apply_runtime_settings()
            svc.scheduler.reload()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось применить прежние настройки после неудачного восстановления")


def restore(svc, data: Dict[str, Any], *, mode: str = "add", sections: Optional[Iterable[str]] = None,
            dry_run: bool = False, actor: str = "", auto_backup: bool = True) -> Dict[str, Any]:
    """Восстановить настройки из открытой копии (:func:`open_backup`). Возвращает отчёт по разделам."""
    if mode not in MODES:
        raise ValidationError(f"Неизвестный режим восстановления: {mode}.", hint="Режимы: add, update.")
    available = [s for s in SECTIONS if s in (data.get("sections") or {})]
    if sections is None:
        chosen = [s for s in available if s != "config"]
    else:
        chosen = [s for s in _sections(sections, protected=True) if s in available and s != "config"]
    if not chosen:
        raise ValidationError("В копии нет выбранных разделов.",
                              hint="В копии есть: " + (", ".join(SECTIONS[s] for s in available) or "ничего") + ".")
    body = data["sections"]
    report: Dict[str, Any] = {"mode": mode, "dry_run": dry_run, "sections": chosen, "tables": {},
                              "created_at": (data.get("head") or {}).get("created_at", ""),
                              "app_version": (data.get("head") or {}).get("app_version", ""),
                              "source": data.get("source") or {}, "secrets": bool(data.get("secrets")),
                              "has_config": "config" in body}
    plan: Dict[str, Any] = {}
    if "settings" in chosen:
        plan, tally = plan_settings(svc, body.get("settings") or {}, mode=mode)
        report["tables"]["settings"] = tally.as_dict()
    if not dry_run and auto_backup:
        report["auto_backup"] = save_auto_backup(svc)
    saved = None
    dismissed: set = set()
    if plan and not dry_run:
        saved = _save_settings(svc, plan)
        report["notice"] = _notice(saved)
    try:
        with svc.db.transaction() as conn:
            r = _Restorer(svc, conn, data, mode=mode, actor=actor)
            for spec in SPECS:
                if spec.section not in chosen:
                    continue
                part = body.get(spec.section)
                rows = part.get(spec.table) if isinstance(part, dict) else None
                if not isinstance(rows, list):
                    continue
                if spec.protected and not r.secrets:
                    continue
                if spec.table == "schedules":
                    r.schedules(rows)
                else:
                    r.table(spec, rows)
            r.finish()
            dismissed = set(r.dismissed)
            for table, tally in r.tallies.items():
                report["tables"][table] = tally.as_dict()
            if dry_run:
                raise _DryRun()
    except _DryRun:
        pass
    except MailArchiverError:
        if saved is not None:
            _undo_settings(svc, saved.get("snapshot") or {})
        raise
    except Exception as exc:  # noqa: BLE001 - ошибка базы на строке копии: всё откатилось
        if saved is not None:
            _undo_settings(svc, saved.get("snapshot") or {})
        log.exception("Восстановление настроек не удалось")
        raise BackupError(f"Копия не восстановлена: {exc}",
                          hint="Ничего не изменено (всё одной транзакцией). Если файл правили вручную — "
                               "проверьте его; иначе пришлите журнал службы разработчику.") from exc
    except BaseException:
        if saved is not None:
            _undo_settings(svc, saved.get("snapshot") or {})
        raise
    totals = {k: sum(int(t.get(k) or 0) for t in report["tables"].values())
              for k in ("added", "updated", "same", "kept", "skipped")}
    report["totals"] = totals
    if not dry_run and dismissed:
        # копия отметила сотрудника уволенным — как при увольнении: сеансы, уведомления, запланированные письма
        from .employees import revoke_employee_access
        for emp_id in sorted(dismissed):
            try:
                revoke_employee_access(svc, emp_id, reason="сотрудник отмечен уволенным при восстановлении настроек")
            except Exception:  # noqa: BLE001
                log.exception("Доступ сотрудника #%s после восстановления настроек не закрыт", emp_id)
    if not dry_run:
        if hasattr(svc, "scheduler") and (totals["added"] or totals["updated"]):
            try:
                svc.scheduler.reload()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось применить расписания после восстановления настроек")
        svc.db.add_audit(actor or "system", "settings_restore",
                         f"копия от {report['created_at'][:19]} (версия {report['app_version']}), режим "
                         f"{'обновить' if mode == 'update' else 'только недостающее'}, разделы: "
                         f"{', '.join(chosen)}; добавлено {totals['added']}, изменено {totals['updated']}, "
                         f"пропущено {totals['skipped']}"[:1000])
    return report


def _notice(saved: Dict[str, Any]) -> str:
    state = saved.get("enc_state") or {}
    if state.get("generated"):
        return (f"Создан ключ шифрования: {state.get('key_path')}. Сохраните его копию ОТДЕЛЬНО от каталога "
                f"данных — без него зашифрованные письма не прочитать.")
    return ""


# =====================================================================
#  Копии перед восстановлением
# =====================================================================
def auto_dir(cfg) -> str:
    return os.path.join(cfg.data_dir, AUTO_DIR)


def _write_auto(svc, kind: str, data: bytes, keep: int) -> str:
    folder = ensure_dir(auto_dir(svc.cfg), 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{kind}-{stamp}.json"
    n = 1
    while os.path.exists(os.path.join(folder, name)):
        n += 1
        name = f"{kind}-{stamp}-{n}.json"
    atomic_write_bytes(os.path.join(folder, name), data, mode=0o600)
    for old in [a for a in list_auto(svc.cfg) if a["kind"] == kind][max(1, keep):]:
        try:
            os.unlink(old["path"])
        except OSError:
            pass
    return name


def save_auto_backup(svc) -> str:
    """Копия нынешних настроек (всё, зашифровано ключом из secret.key) — перед восстановлением."""
    data, _summary = build(svc, local=True)
    return _write_auto(svc, "before-restore", data, AUTO_KEEP)


def _daily_password(svc) -> str:
    try:
        return str(_rt(svc)("settings_backup", "password") or "")
    except Exception:  # noqa: BLE001
        return ""


def save_daily(svc) -> Dict[str, Any]:
    """Ежедневная копия настроек (1.27.0): с паролем копий настроек (``settings_backup.password``) — читается
    им и без secret.key, поэтому годится для копии вне сервера; без пароля — ключом из secret.key."""
    password = _daily_password(svc)
    data, summary = build(svc, password=password) if password else build(svc, local=True)
    try:
        keep = int(_rt(svc)("settings_backup", "keep") or 14)
    except (TypeError, ValueError):
        keep = 14
    name = _write_auto(svc, "daily" if password else "daily-local", data, keep)
    log.info("Ежедневная копия настроек: %s (%s).", name, "с паролем" if password else "ключом secret.key")
    return {"name": name, "size": len(data), "password": bool(password), "counts": summary["counts"]}


def maybe_daily(svc, *, max_age_s: float = DAILY_EVERY_S) -> Optional[Dict[str, Any]]:
    """Сделать ежедневную копию, если она включена и последней больше ``max_age_s`` (снимок базы, копия вне
    сервера). Ошибка не мешает тому, кто вызвал: она в журнале и в возвращаемом ``error``."""
    try:
        if not bool(_rt(svc)("settings_backup", "enabled")):
            return None
        last = next((a for a in list_auto(svc.cfg) if a["kind"] in DAILY_KINDS), None)
        if last is not None:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])).total_seconds()
            if age < max_age_s:
                return None
        return save_daily(svc)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ежедневная копия настроек не сделана")
        return {"error": getattr(exc, "message", None) or str(exc)}


def daily_age_s(cfg) -> Optional[float]:
    last = next((a for a in list_auto(cfg) if a["kind"] in DAILY_KINDS), None)
    if last is None:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"])).total_seconds()


def list_auto(cfg) -> List[Dict[str, Any]]:
    folder = auto_dir(cfg)
    out = []
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return out
    for name in names:
        m = _AUTO_RE.match(name)
        if not m:
            continue
        path = os.path.join(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        created = datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        out.append({"name": name, "path": path, "size": size, "created_at": created.isoformat(),
                    "kind": m.group(1), "_o": (m.group(2), int(m.group(3) or 0))})
    out.sort(key=lambda x: x["_o"], reverse=True)
    for item in out:
        del item["_o"]
    return out


def read_auto(cfg, name: str) -> bytes:
    if not _AUTO_RE.match(str(name or "")):
        raise ValidationError("Нет такой копии.")
    path = os.path.join(auto_dir(cfg), name)
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise ValidationError("Нет такой копии.") from None


def report_text(report: Dict[str, Any]) -> str:
    """Отчёт для командной строки."""
    lines = []
    for table, t in report.get("tables", {}).items():
        lines.append(f"  {t['title']}: добавить {t['added']}, изменить {t['updated']}, без изменений {t['same']}"
                     + (f", оставлено как есть {t['kept']}" if t["kept"] else "")
                     + (f", пропущено {t['skipped']}" if t["skipped"] else ""))
        for note in t.get("notes", [])[:10]:
            lines.append(f"      ! {note}")
    return "\n".join(lines)


__all__ = ["SECTIONS", "PROTECTED_ONLY", "MODES", "BackupError", "build", "describe", "open_backup", "restore",
           "plan_settings", "save_auto_backup", "list_auto", "read_auto", "report_text", "collect",
           "MailArchiverError"]
