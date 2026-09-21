"""
Загрузка и валидация конфигурации MailArchiver.

Конфигурация читается из YAML-файла и накладывается поверх значений по
умолчанию (:data:`DEFAULTS`). Часть параметров можно менять «на лету» через
веб-интерфейс — они хранятся в БД (таблица ``settings``) и переопределяют
значения из файла (см. :mod:`mailarchiver.database`).

Путь к файлу конфигурации определяется в порядке приоритета:
  1. аргумент ``--config`` / параметр функции :func:`load_config`;
  2. переменная окружения ``MAILARCHIVER_CONFIG``;
  3. ``/etc/mailarchiver/config.yaml``;
  4. ``<корень проекта>/config/config.yaml``.
"""
from __future__ import annotations

import copy
import os
import secrets
from typing import Any, Dict, Optional

import yaml

from .errors import ConfigError
from .util import ensure_dir, atomic_write_text

# ---------------------------------------------------------------------------
# Значения по умолчанию — полный список настраиваемых параметров.
# Каждый параметр подробно описан в docs/ru/05-parameters-reference.md.
# ---------------------------------------------------------------------------
DEFAULTS: Dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",          # адрес прослушивания веб-интерфейса
        "port": 8493,                  # порт веб-интерфейса
        "public_url": "",             # внешний URL (для ссылок), напр. https://mail.example.ru
        "workers": 1,                  # процессов uvicorn (обычно 1 из-за общего планировщика)
        "behind_proxy": False,         # доверять заголовкам X-Forwarded-* от reverse proxy
    },
    "security": {
        "auth_enabled": True,          # требовать вход в веб-интерфейс
        "session_ttl_hours": 12,       # срок жизни сессии
        "session_idle_minutes": 60,    # авто-выход при бездействии
        "min_password_length": 8,      # минимальная длина пароля пользователя
        "max_login_attempts": 5,       # блокировка после N неудачных попыток
        "lockout_minutes": 15,         # длительность блокировки
        "allow_password_login": True,
        "secure_cookie": False,        # True — только по HTTPS (ставьте за reverse proxy c TLS)
        "imap_ssl_verify": True,       # проверять TLS-сертификат IMAP-сервера
    },
    "paths": {
        "data_dir": "",               # корень данных; если пусто — определяется автоматически
    },
    "database": {
        "busy_timeout_ms": 10000,      # ожидание блокировки SQLite
        "wal": True,                   # режим WAL (быстрее и надёжнее при конкурентном доступе)
    },
    "storage": {
        "layout": "maildir",          # формат локального хранилища: maildir (по умолчанию)
        "compress": False,             # сжимать .eml на лету (gzip) — экономит место, чуть медленнее
        "fsync": True,                 # принудительный сброс на диск после записи письма
        "min_free_space_mb": 500,      # не начинать бэкап, если свободно меньше
        "verify_after_write": True,    # сверять размер/хеш после записи письма
    },
    "backup": {
        "max_concurrent_jobs": 2,      # сколько заданий выполняется одновременно
        "per_account_concurrency": 1,  # параллельных подключений к одному ящику
        "fetch_batch_size": 200,       # сколько писем запрашивать за раз (UID FETCH)
        "connect_timeout_s": 30,       # таймаут установления соединения
        "socket_timeout_s": 120,       # таймаут операций сокета
        "retry_attempts": 4,           # число повторов при временных сбоях
        "retry_initial_delay_s": 2,    # начальная задержка между повторами
        "retry_backoff": 2.0,          # множитель роста задержки
        "idle_reconnect_min": 30,      # переподключаться к IMAP не реже, чем раз в N минут
        "download_flags": True,        # сохранять флаги писем (\Seen, \Flagged и т.п.)
        "skip_larger_than_mb": 0,      # пропускать письма крупнее (0 — не пропускать)
        "folder_include": [],          # белый список папок (пусто — все)
        "folder_exclude": [],          # чёрный список папок (напр. ["[Gmail]/Spam", "Корзина"])
    },
    "retention": {
        "enabled": False,              # включить авто-очистку старых копий
        "keep_days": 0,                # хранить письма не старше N дней (0 — бессрочно)
        "keep_last_runs": 30,          # сколько записей истории бэкапов хранить
        "delete_removed_from_server": False,  # удалять локально письма, удалённые на сервере
    },
    "export": {
        "default_engine": "auto",     # auto | aspose | native | mbox | eml | msg
        "pst_format": "unicode",      # unicode (Outlook 2003+) | ansi (Outlook 97–2002)
        "pst_split_size_mb": 0,        # разбивать .pst на части по размеру (0 — не разбивать)
        "aspose_license_path": "",    # путь к файлу лицензии Aspose.Email (.lic)
        "outlook_target": "2016+",    # целевая версия Outlook (влияет на формат по умолчанию)
        "include_attachments": True,
        "tmp_dir": "",                # каталог для временных файлов экспорта
    },
    "scheduler": {
        "enabled": True,
        "timezone": "Europe/Moscow",  # часовой пояс для расписаний
        "misfire_grace_time_s": 3600,  # допуск на пропуск запуска
        "coalesce": True,              # объединять пропущенные запуски в один
    },
    "logging": {
        "level": "INFO",              # DEBUG | INFO | WARNING | ERROR
        "to_stdout": True,
    },
    "notifications": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_security": "starttls",  # starttls | ssl | none
        "smtp_user": "",
        "smtp_password": "",
        "mail_from": "",
        "mail_to": [],
        "on_success": False,
        "on_failure": True,
    },
    "employees": {
        "sync_enabled": False,         # синхронизировать справочник сотрудников по расписанию
        "source_type": "file",        # откуда берём список: file (файл на сервере) | url
        "source_file": "",            # путь к файлу CSV/XLSX на сервере (выгрузка из кадровой системы)
        "source_url": "",             # адрес выгрузки http(s):// (когда source_type = url)
        "source_url_user": "",        # логин HTTP Basic, если адрес закрыт авторизацией
        "source_url_password": "",    # пароль HTTP Basic (хранится в БД зашифрованным)
        "source_url_verify_ssl": True,  # проверять сертификат сервера-источника
        "source_url_timeout_s": 60,    # сколько ждать ответа от сервера-источника, секунды
        "source_url_format": "auto",  # чем разбирать ответ: auto | csv | xlsx
        "cron": "0 5 * * *",          # когда синхронизировать (минуты часы день месяц день_недели)
        "create_accounts": True,       # заводить почтовые ящики для новых сотрудников
        # --- шаблон настроек создаваемых ящиков ---
        # Всё, что ниже, применяется к ящику, который заводится автоматически
        # при появлении сотрудника. В шаблонах строк доступны подстановки
        # {email} {local} {domain} {full_name} {position} {department}
        # {external_id} (см. mailarchiver/employees.py: render_account_field).
        "account_host": "",           # IMAP-сервер создаваемых ящиков
        "account_port": 993,           # порт создаваемых ящиков
        "account_security": "ssl",    # шифрование создаваемых ящиков: ssl | starttls | plain
        "account_name_template": "{full_name}",  # название ящика в списке
        "account_username_template": "{email}",  # логин для входа в почту
        "account_notes_template": "Создан автоматически для сотрудника.",  # заметка в карточке ящика
        "account_enabled": False,      # включать созданный ящик сразу (без пароля копирование не пойдёт)
        "account_folder_include": [],  # какие папки копировать (пусто = все)
        "account_folder_exclude": [],  # какие папки пропускать
        "account_retention_days": -1,  # срок хранения: -1 = как в общих настройках, 0 = вечно, N = дней
        "account_schedule_enabled": False,  # заводить расписание копирования новому ящику
        "account_schedule_cron": "0 2 * * *",  # расписание копирования (минуты часы день месяц день_недели)
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Рекурсивно слить override поверх base (не мутируя base)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        elif value is None and isinstance(result.get(key), dict):
            # Пустая секция в YAML («storage:» без тела) даёт None — не затираем
            # ею словарь значений по умолчанию, иначе запуск падает с AttributeError.
            continue
        else:
            result[key] = value
    return result


def _default_data_dir() -> str:
    """Определить каталог данных по умолчанию."""
    for candidate in (
        os.environ.get("MAILARCHIVER_DATA"),
        "/var/lib/mailarchiver",
    ):
        if candidate:
            parent = os.path.dirname(candidate.rstrip("/"))
            if os.path.isdir(candidate) or (parent and os.access(parent, os.W_OK)):
                return candidate
    # запасной вариант — рядом с проектом
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")


def _candidate_config_paths() -> list[str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        os.environ.get("MAILARCHIVER_CONFIG", ""),
        "/etc/mailarchiver/config.yaml",
        os.path.join(root, "config", "config.yaml"),
    ]


class Config:
    """Объект конфигурации с доступом к секциям и производным путям."""

    def __init__(self, data: Dict[str, Any], source_path: Optional[str] = None) -> None:
        self.data = data
        self.source_path = source_path

        data_dir = data["paths"].get("data_dir") or _default_data_dir()
        self.data_dir = os.path.abspath(data_dir)

        self.db_path = os.path.join(self.data_dir, "mailarchiver.db")
        self.log_dir = os.path.join(self.data_dir, "logs")
        self.secret_key_path = os.path.join(self.data_dir, "secret.key")
        self.mail_root = os.path.join(self.data_dir, "mailboxes")
        self.exports_dir = os.path.join(self.data_dir, "exports")
        self.tmp_dir = data["export"].get("tmp_dir") or os.path.join(self.data_dir, "tmp")

        self._secret_key: Optional[bytes] = None

    # -- доступ к секциям ----------------------------------------------------
    def section(self, name: str) -> Dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    @property
    def server(self) -> Dict[str, Any]:
        return self.data["server"]

    @property
    def security(self) -> Dict[str, Any]:
        return self.data["security"]

    @property
    def storage(self) -> Dict[str, Any]:
        return self.data["storage"]

    @property
    def backup(self) -> Dict[str, Any]:
        return self.data["backup"]

    @property
    def retention(self) -> Dict[str, Any]:
        return self.data["retention"]

    @property
    def export(self) -> Dict[str, Any]:
        return self.data["export"]

    @property
    def scheduler(self) -> Dict[str, Any]:
        return self.data["scheduler"]

    @property
    def logging_cfg(self) -> Dict[str, Any]:
        return self.data["logging"]

    @property
    def notifications(self) -> Dict[str, Any]:
        return self.data["notifications"]

    # -- инициализация каталогов и секрета -----------------------------------
    def ensure_dirs(self) -> None:
        ensure_dir(self.data_dir, 0o700)
        ensure_dir(self.log_dir, 0o700)
        ensure_dir(self.mail_root, 0o700)
        ensure_dir(self.exports_dir, 0o700)
        ensure_dir(self.tmp_dir, 0o700)

    def secret_key(self) -> bytes:
        """Загрузить или сгенерировать секретный ключ (для подписи cookie и шифрования)."""
        if self._secret_key is not None:
            return self._secret_key
        if os.path.exists(self.secret_key_path):
            with open(self.secret_key_path, "rb") as fh:
                key = fh.read().strip()
            if len(key) < 32:
                raise ConfigError(
                    "Файл секретного ключа повреждён (слишком короткий).",
                    hint=f"Удалите {self.secret_key_path}, чтобы сгенерировать новый (это разлогинит пользователей).",
                )
        else:
            key = secrets.token_bytes(48)
            ensure_dir(self.data_dir, 0o700)
            atomic_write_text(self.secret_key_path, key.hex(), mode=0o600)
            key = key.hex().encode()
        self._secret_key = key
        return key

    def validate(self) -> None:
        port = self.server.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ConfigError(f"Некорректный порт сервера: {port!r}", hint="Укажите число 1–65535 в server.port")
        level = str(self.logging_cfg.get("level", "INFO")).upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"Некорректный уровень логирования: {level}")
        if self.export.get("pst_format") not in {"unicode", "ansi"}:
            raise ConfigError("export.pst_format должен быть 'unicode' или 'ansi'")
        if int(self.backup.get("max_concurrent_jobs", 1)) < 1:
            raise ConfigError("backup.max_concurrent_jobs должен быть >= 1")


def load_config(path: Optional[str] = None, *, create_dirs: bool = True) -> Config:
    """Загрузить конфигурацию из файла (или значения по умолчанию)."""
    source = None
    file_data: Dict[str, Any] = {}
    candidates = [path] if path else []
    candidates += _candidate_config_paths()
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            source = candidate
            try:
                with open(candidate, "r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                if not isinstance(loaded, dict):
                    raise ConfigError(f"Файл конфигурации {candidate} должен содержать YAML-объект (ключ: значение).")
                file_data = loaded
            except yaml.YAMLError as exc:
                raise ConfigError(
                    f"Ошибка синтаксиса YAML в {candidate}: {exc}",
                    hint="Проверьте отступы и двоеточия. Можно сверить синтаксис на yamllint.com",
                ) from exc
            break

    merged = _deep_merge(DEFAULTS, file_data)
    cfg = Config(merged, source_path=source)
    cfg.validate()
    if create_dirs:
        cfg.ensure_dirs()
    return cfg


def render_example_config() -> str:
    """Сгенерировать пример config.yaml с комментариями (для config.example.yaml)."""
    # Пример собирается вручную, чтобы сохранить порядок и комментарии.
    return EXAMPLE_CONFIG_YAML


EXAMPLE_CONFIG_YAML = """\
# ============================================================================
#  MailArchiver — файл конфигурации
#  Скопируйте в /etc/mailarchiver/config.yaml (или config/config.yaml)
#  и отредактируйте под себя. Все параметры имеют разумные значения по
#  умолчанию — можно менять только нужное. Подробности по каждому параметру:
#  docs/ru/05-parameters-reference.md
# ============================================================================

server:
  host: "127.0.0.1"     # 0.0.0.0 — слушать на всех интерфейсах (за reverse proxy)
  port: 8493
  public_url: ""         # напр. "https://mail-backup.example.ru"
  behind_proxy: false    # true, если стоите за nginx/traefik с TLS

security:
  auth_enabled: true
  session_ttl_hours: 12
  session_idle_minutes: 60
  min_password_length: 8
  max_login_attempts: 5
  lockout_minutes: 15
  secure_cookie: false   # true при работе только по HTTPS

paths:
  data_dir: ""           # пусто = /var/lib/mailarchiver (или ./data при запуске из исходников)

storage:
  layout: "maildir"
  compress: false        # true — сжимать письма gzip (экономия места)
  min_free_space_mb: 500
  verify_after_write: true

backup:
  max_concurrent_jobs: 2
  per_account_concurrency: 1
  fetch_batch_size: 200
  connect_timeout_s: 30
  socket_timeout_s: 120
  retry_attempts: 4
  download_flags: true
  skip_larger_than_mb: 0
  folder_exclude: []     # напр. ["[Gmail]/Корзина", "Спам"]

retention:
  enabled: false
  keep_days: 0           # 0 = хранить вечно
  keep_last_runs: 30
  delete_removed_from_server: false

export:
  default_engine: "auto"   # auto|aspose|native|mbox|eml|msg
  pst_format: "unicode"    # unicode (Outlook 2003+) | ansi (Outlook 97–2002)
  outlook_target: "2016+"
  pst_split_size_mb: 0
  aspose_license_path: ""

scheduler:
  enabled: true
  timezone: "Europe/Moscow"

logging:
  level: "INFO"

notifications:
  enabled: false
  smtp_host: ""
  smtp_port: 587
  smtp_security: "starttls"
  smtp_user: ""
  smtp_password: ""
  mail_from: ""
  mail_to: []
  on_failure: true
  on_success: false

employees:
  sync_enabled: false        # true — синхронизировать справочник по расписанию
  source_file: ""            # напр. "/var/lib/mailarchiver/hr/employees.csv"
  cron: "0 5 * * *"          # ежедневно в 05:00
  create_accounts: true      # заводить ящики новым сотрудникам (создаются ВЫКЛЮЧЕННЫМИ)
  account_host: ""           # напр. "imap.example.ru"
  account_port: 993
  account_security: "ssl"    # ssl | starttls | plain
"""
