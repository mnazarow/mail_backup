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
import math
import os
import re
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
        # Адреса обратных прокси, которым можно верить (список или строка через запятую,
        # допустимы сети вида 10.0.0.0/8). Настоящий адрес клиента берётся как ПЕРВЫЙ
        # СПРАВА недоверенный хоп X-Forwarded-For. «*» отключает проверку (небезопасно).
        "trusted_proxies": "127.0.0.1, ::1",
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
        "require_2fa": False,          # True — администраторы обязаны включить двухфакторный вход
        "mailbox_login": True,         # вход сотрудников в веб-интерфейс по email и паролю своего ящика
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
        # Шифровать новые письма в локальной копии (AES-256-GCM). Ключ — отдельный
        # файл; без него зашифрованные письма не прочитать. Храните его копию
        # ОТДЕЛЬНО от каталога данных.
        "encrypt": False,
        "encryption_key_file": "",    # путь к ключу; пусто — <каталог данных>/storage.key
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
        "unreadable_folder_grace_runs": 3,  # после скольких прогонов подряд нечитаемая папка перестаёт считаться ошибкой
        "skip_larger_than_mb": 0,      # пропускать письма крупнее (0 — не пропускать)
        "folder_include": [],          # белый список папок (пусто — все)
        "folder_exclude": [],          # чёрный список папок (напр. ["[Gmail]/Spam", "Корзина"])
    },
    "retention": {
        # включить ежедневную очистку по ОБЩЕМУ сроку keep_days для ящиков, у
        # которых срок «как в общих настройках» (свой срок ящика действует всегда)
        "enabled": False,
        "cron": "30 4 * * *",          # когда запускать ежедневную очистку (минуты часы день месяц день_недели)
        "keep_days": 0,                # хранить письма не старше N дней (0 — бессрочно)
        "keep_last_runs": 30,          # сколько записей истории бэкапов хранить
        "delete_removed_from_server": False,  # удалять локально письма, удалённые на сервере
    },
    "export": {
        "default_engine": "auto",     # auto | aspose | native | mbox | eml
        "pst_format": "unicode",      # unicode (Outlook 2003+) | ansi (Outlook 97–2002)
        "pst_split_size_mb": 0,        # разбивать .pst на части по размеру (0 — не разбивать)
        "keep_days": 30,               # удалять готовые выгрузки старше N дней (0 — не удалять)
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
        "summary_enabled": True,       # еженедельная сводка администраторам (если уведомления включены)
        "summary_cron": "0 8 * * 1",  # когда отправлять сводку (по умолчанию понедельник 08:00)
    },
    "search": {
        "enabled": True,               # индексировать письма для поиска (раздел «Почта» → поиск)
        "index_bodies": True,          # искать и по тексту писем (иначе — тема, адреса, вложения)
        "index_bodies_encrypted": False,  # индексировать текст, даже если копия шифруется (текст ляжет в базу открыто)
        "body_max_kb": 16,             # сколько текста письма индексировать, КБ (цитаты прошлых писем отбрасываются)
    },
    "mailadmin": {
        # Вход в ящики учётной записью администратора почты — без паролей сотрудников.
        # Работает, если почтовый сервер это умеет: Dovecot (master users), Cyrus и
        # Zimbra (SASL PLAIN с authzid). Axigen такой вход не документирует.
        "enabled": False,
        "host": "",                   # IMAP-сервер, которому можно отправлять пароль администратора
        "user": "",                   # логин администратора почты
        "password": "",               # пароль (хранится в БД зашифрованным)
        "mode": "sasl_plain",         # sasl_plain (AUTHENTICATE PLAIN с authzid) | separator (ящик*админ)
        "separator": "*",             # разделитель для режима separator (Dovecot: auth_master_user_separator)
    },
    "monitoring": {
        "metrics_enabled": False,      # отдавать метрики по адресу /metrics (Prometheus, Zabbix)
        "metrics_token": "",          # токен доступа (Authorization: Bearer …); хранится в БД зашифрованным
        "metrics_allowed_ips": ["127.0.0.1", "::1"],  # с каких адресов пускать без токена
        "per_account_metrics": True,   # метрики по каждому ящику (дата последней копии и т. п.)
    },
    "replica": {
        # Копия архива ВНЕ сервера: письма и снимки базы уезжают в сетевую папку,
        # на другой сервер по SSH (rsync) или в S3-совместимое хранилище.
        "enabled": False,              # делать копию по расписанию
        "target": "dir",              # куда: dir (сетевая папка/диск) | rsync (сервер по SSH) | s3
        "cron": "0 6 * * *",          # когда (минуты часы день месяц день_недели)
        "include_db": True,            # класть в копию снимки базы
        "db_snapshot_keep": 3,         # сколько снимков базы хранить на сервере (0 — не делать)
        "mirror_deletions": True,      # удалять в копии письма, удалённые из архива (очистка по сроку)
        "max_delete_percent": 50,      # защита: не удалять за прогон больше N % файлов копии
        "parallel": 4,                 # сколько файлов отправлять одновременно (папка, S3)
        "verify_every_days": 7,        # раз в N дней сверять копию полностью (0 — не сверять)
        "timeout_s": 120,              # таймаут сетевых операций, секунды
        "dir_path": "",               # папка: путь к смонтированной сетевой папке или диску
        "rsync_dest": "",             # SSH: пользователь@сервер:/путь
        "ssh_port": 22,                # SSH: порт
        "ssh_key_file": "",           # SSH: закрытый ключ (пусто — ключ службы replica_ssh_key)
        "bwlimit_kbps": 0,             # SSH: ограничение скорости, КБ/с (0 — без ограничения)
        "s3_endpoint": "",            # S3: адрес хранилища, напр. https://storage.yandexcloud.net
        "s3_region": "ru-central1",   # S3: регион
        "s3_bucket": "",              # S3: бакет
        "s3_prefix": "mailarchiver/",  # S3: папка (префикс) внутри бакета
        "s3_access_key": "",          # S3: идентификатор ключа доступа
        "s3_secret_key": "",          # S3: секретный ключ (хранится в БД зашифрованным)
        "s3_path_style": True,         # S3: адресация bucket в пути (нужна MinIO; Yandex понимает обе)
        "s3_storage_class": "",       # S3: класс хранилища (пусто — по умолчанию; COLD — холодное у Yandex)
        "s3_verify_ssl": True,         # S3: проверять сертификат хранилища
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
        "account_use_master": False,   # заводить ящики с входом через администратора почты (без паролей)
        # --- уволенные сотрудники ---
        "dismiss_from_file": True,     # отмечать уволенными тех, кто помечен так в выгрузке
        "dismiss_missing_days": 0,     # уволен, если не встречается в выгрузке N дней (0 — не считать)
        "dismissed_action": "final_backup_disable",  # final_backup_disable | keep
        "dismissed_keep_years": 5,     # сколько лет хранить архив уволенного (0 — бессрочно)
    },
}



#: Допустимые диапазоны числовых параметров: «секция.ключ» → (минимум, максимум).
#: Нужны и при старте (Config.validate), и при сохранении настроек из веб-интерфейса.
#: Без них, например, отрицательный security.session_ttl_hours выдавал cookie с
#: Max-Age в прошлом: вход «успешен», но каждая следующая страница — 401, и
#: починить это через интерфейс уже нельзя.
VALUE_RANGES: Dict[str, tuple] = {
    # server
    "server.port": (1, 65535),
    "server.workers": (1, 64),
    # security
    "security.session_ttl_hours": (1, 8760),
    "security.session_idle_minutes": (1, 10080),
    "security.min_password_length": (4, 128),
    "security.max_login_attempts": (1, 1000),
    "security.lockout_minutes": (1, 1440),
    # storage
    "storage.min_free_space_mb": (0, 10_000_000),
    # backup
    "backup.max_concurrent_jobs": (1, 16),
    "backup.per_account_concurrency": (1, 16),
    "backup.fetch_batch_size": (1, 10000),
    "backup.connect_timeout_s": (1, 3600),
    "backup.socket_timeout_s": (1, 86400),
    "backup.retry_attempts": (1, 50),
    "backup.retry_initial_delay_s": (0, 3600),
    "backup.retry_backoff": (1.0, 10.0),
    "backup.idle_reconnect_min": (1, 1440),
    "backup.unreadable_folder_grace_runs": (0, 1000),
    "backup.skip_larger_than_mb": (0, 100000),
    # retention
    "retention.keep_days": (0, 36500),
    "retention.keep_last_runs": (1, 100000),
    # export
    "export.pst_split_size_mb": (0, 1_000_000),
    "export.keep_days": (0, 3650),
    # scheduler
    "scheduler.misfire_grace_time_s": (1, 604800),
    # employees
    "employees.source_url_timeout_s": (1, 3600),
    "employees.account_port": (1, 65535),
    "employees.account_retention_days": (-1, 36500),
    "employees.dismiss_missing_days": (0, 3650),
    "employees.dismissed_keep_years": (0, 100),
    # replica
    "search.body_max_kb": (1, 1024),
    "replica.db_snapshot_keep": (0, 365),
    "replica.max_delete_percent": (0, 100),
    "replica.parallel": (1, 32),
    "replica.verify_every_days": (0, 365),
    "replica.timeout_s": (5, 3600),
    "replica.ssh_port": (1, 65535),
    "replica.bwlimit_kbps": (0, 10_000_000),
    # notifications / database / logging
    "notifications.smtp_port": (1, 65535),
    "database.busy_timeout_ms": (100, 600000),
}


def check_value_range(full_key: str, value):
    """Проверить число по таблице :data:`VALUE_RANGES`.

    Возвращает текст ошибки или None. Не-числа и неизвестные ключи пропускает:
    тип проверяется отдельно, по значению по умолчанию.
    """
    bounds = VALUE_RANGES.get(full_key)
    if isinstance(value, float) and not math.isfinite(value):
        return f"Параметр «{full_key}» должен быть конечным числом."
    if not bounds or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    low, high = bounds
    if value < low or value > high:
        return f"Параметр «{full_key}» должен быть в диапазоне {low}\u2013{high} (получено {value})."
    return None


#: Допустимые значения параметров-перечислений. Без проверки, например,
#: scheduler.timezone «Mars/Olympus» молча превращался в UTC, и все расписания
#: съезжали на 3 часа, а неизвестный движок экспорта ронял каждый экспорт.
VALUE_CHOICES: Dict[str, tuple] = {
    "logging.level": ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    "export.default_engine": ("auto", "aspose", "native", "mbox", "eml"),
    "export.pst_format": ("unicode", "ansi"),
    "notifications.smtp_security": ("starttls", "ssl", "none"),
    "employees.source_type": ("file", "url"),
    "employees.source_url_format": ("auto", "csv", "xlsx"),
    "employees.account_security": ("ssl", "starttls", "plain"),
    "replica.target": ("dir", "rsync", "s3"),
    "mailadmin.mode": ("sasl_plain", "separator"),
    "employees.dismissed_action": ("final_backup_disable", "keep"),
}

#: Параметры, которые задаются ТОЛЬКО в config.yaml: они нужны до запуска
#: веб-сервера (адрес, порт, вывод журнала) или касаются самого хранилища.
#: Из интерфейса их не меняем: неверный порт, сохранённый через веб, сделал бы
#: интерфейс недоступным после перезапуска, а исправить это из него же нельзя.
FILE_ONLY_SETTINGS = frozenset({
    "server.host", "server.port", "server.workers", "logging.to_stdout",
    "paths.data_dir", "database.busy_timeout_ms", "database.wal", "export.tmp_dir",
})

#: Параметры, которые больше ничего не делают (оставлены в DEFAULTS, чтобы
#: старые config.yaml не давали ошибок). Через интерфейс не сохраняются.
UNSUPPORTED_SETTINGS = frozenset({
    "security.allow_password_login", "storage.layout", "backup.idle_reconnect_min",
    "backup.per_account_concurrency",
    "export.outlook_target", "export.include_attachments",
    "retention.delete_removed_from_server",
})

_TRUE_WORDS = {"true", "1", "yes", "on", "да", "вкл"}
_FALSE_WORDS = {"false", "0", "no", "off", "нет", "выкл", ""}


def coerce_setting(full_key: str, value):
    """Привести значение параметра к типу его значения по умолчанию — СТРОГО.

    Возвращает приведённое значение или бросает ``ValueError`` с понятным
    текстом. Раньше ``bool("false")`` давал True (строка «false» ВКЛЮЧАЛА
    шифрование), NaN проходил проверку диапазона и ломал раздел «Настройки»,
    а строка вместо списка папок превращалась в список отдельных букв.
    """
    section, _, key = full_key.partition(".")
    if key not in DEFAULTS.get(section, {}):
        raise ValueError(f"Неизвестный параметр «{full_key}».")
    default = DEFAULTS[section][key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            out = value
        elif isinstance(value, int) and value in (0, 1):
            out = bool(value)
        elif isinstance(value, str) and value.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
            out = value.strip().lower() in _TRUE_WORDS
        else:
            raise ValueError(f"Параметр «{full_key}» должен быть «да» или «нет» (true/false).")
    elif isinstance(default, int):
        if isinstance(value, bool):
            raise ValueError(f"Параметр «{full_key}» должен быть целым числом.")
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            value = int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            value = int(value.strip())
        if not isinstance(value, int):
            raise ValueError(f"Параметр «{full_key}» должен быть целым числом.")
        out = value
    elif isinstance(default, float):
        if isinstance(value, bool):
            raise ValueError(f"Параметр «{full_key}» должен быть числом.")
        try:
            out = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Параметр «{full_key}» должен быть числом.") from None
        if not math.isfinite(out):
            raise ValueError(f"Параметр «{full_key}» должен быть конечным числом.")
    elif isinstance(default, list):
        if isinstance(value, str):
            items = [part for chunk in value.splitlines() for part in chunk.split(",")]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise ValueError(f"Параметр «{full_key}» должен быть списком.")
        out = []
        for item in items:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise ValueError(f"Параметр «{full_key}»: элементы списка должны быть строками.")
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    else:
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"Параметр «{full_key}» должен быть строкой.")
        out = value.strip()
        if len(out) > 4096:
            raise ValueError(f"Параметр «{full_key}» слишком длинный.")
    choices = VALUE_CHOICES.get(full_key)
    if choices:
        match = next((c for c in choices if c.lower() == str(out).lower()), None)
        if match is None:
            raise ValueError(f"Параметр «{full_key}»: допустимые значения — {', '.join(choices)}.")
        out = match
    if full_key == "scheduler.timezone":
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(out)
        except Exception:  # noqa: BLE001
            raise ValueError(f"Неизвестный часовой пояс «{out}». Пример: Europe/Moscow.") from None
    if full_key == "storage.encryption_key_file" and out and not os.path.isabs(out):
        raise ValueError("Путь к файлу ключа должен быть абсолютным (например /etc/mailarchiver/storage.key).")
    if full_key == "server.public_url" and out and not out.lower().startswith(("http://", "https://")):
        raise ValueError("Внешний URL должен начинаться с http:// или https://.")
    if full_key in ("replica.dir_path", "replica.ssh_key_file") and out and not os.path.isabs(out):
        raise ValueError(f"Параметр «{full_key}»: нужен абсолютный путь (начинается с «/»).")
    if full_key == "replica.s3_endpoint" and out and not out.lower().startswith(("http://", "https://")):
        raise ValueError("Адрес хранилища S3 должен начинаться с https:// (или http://).")
    if full_key == "replica.rsync_dest" and out and not re.match(
            r"^(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):/", out):
        raise ValueError("Адрес для rsync: пользователь@сервер:/путь, например backup@nas.local:/srv/mailarchiver.")
    if full_key == "replica.s3_storage_class" and out and not re.match(r"^[A-Z0-9_]{1,40}$", out):
        raise ValueError("Класс хранилища S3 пишется заглавными буквами, например COLD или STANDARD_IA.")
    if full_key == "replica.s3_prefix" and (".." in out.split("/") or out.startswith("/")):
        raise ValueError("Префикс S3 не должен начинаться с «/» или содержать «..».")
    if full_key == "mailadmin.separator" and (not out or len(out) > 4 or any(c.isspace() for c in out)):
        raise ValueError("Разделитель — 1–4 символа без пробелов, например «*».")
    if full_key == "monitoring.metrics_allowed_ips":
        import ipaddress
        for item in out:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"«{item}» — не IP-адрес и не сеть (пример: 10.0.0.5 или 10.0.0.0/24).") from None
    if full_key == "monitoring.metrics_token" and out and len(out) < 16:
        raise ValueError("Токен метрик — не короче 16 символов (нажмите «Создать токен»).")
    if full_key == "notifications.mail_to":
        bad = [addr for addr in out if "@" not in addr]
        if bad:
            raise ValueError(f"Не похоже на адрес почты: {', '.join(bad[:3])}.")
    err = check_value_range(full_key, out)
    if err:
        raise ValueError(err)
    return out


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
        elif isinstance(result.get(key), dict) and not isinstance(value, dict):
            # Скаляр на месте секции («server: 5» вместо вложенных ключей) —
            # обычно потерянный перенос строки или отступ. Раньше это давало
            # голый AttributeError: 'int' object has no attribute 'get'.
            raise ConfigError(
                f"Секция «{key}» должна содержать набор «ключ: значение», "
                f"а не одиночное значение ({value!r}).",
                hint="Проверьте отступы в config.yaml: вложенные параметры пишутся с отступом под именем секции.")
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

    def __init__(self, data: Dict[str, Any], source_path: Optional[str] = None,
                 file_data: Optional[Dict[str, Any]] = None) -> None:
        self.data = data
        self.source_path = source_path
        #: то, что задано именно в файле (для проверки ключей и типов)
        self.file_data = file_data or {}
        #: замечания к файлу конфигурации (неизвестные параметры и т. п.)
        self.warnings: list = []

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
            # Права файла. Ключ, приехавший из бэкапа с 0644, читает любой
            # пользователь машины — а это подпись cookie и расшифровка всех
            # паролей ящиков. Чиним молча, если можем, и предупреждаем, если нет.
            try:
                mode = os.stat(self.secret_key_path).st_mode & 0o777
                if mode & 0o077:
                    os.chmod(self.secret_key_path, 0o600)
                    _log_secret_perm_fixed(self.secret_key_path, mode)
            except OSError as exc:
                # Чаще всего файл принадлежит другому пользователю (перенос
                # каталога данных, восстановление из бэкапа). Молчать нельзя:
                # ключом подписываются cookie и шифруются пароли всех ящиков.
                _log_secret_perm_failed(self.secret_key_path, exc)
        else:
            key = secrets.token_bytes(48)
            ensure_dir(self.data_dir, 0o700)
            atomic_write_text(self.secret_key_path, key.hex(), mode=0o600)
            key = key.hex().encode()
        self._secret_key = key
        return key

    def validate(self) -> None:
        # Сначала привести и проверить то, что задано в файле: иначе проверки
        # ниже видели сырые строки — «max_concurrent_jobs: abc» давал английское
        # «invalid literal for int()» без имени параметра, а «port: "8493"» или
        # «pst_format: Unicode» отвергались, хотя это допустимые значения.
        self._check_file_values()
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
        trusted, _any = _check_trusted_proxies(self.server.get("trusted_proxies", ""))
        if not trusted and not _any and self.server.get("behind_proxy"):
            raise ConfigError("server.behind_proxy включён, но server.trusted_proxies пуст.",
                              hint="Укажите адрес(а) обратного прокси, например «127.0.0.1» или «10.0.0.0/8».")
        for full_key in VALUE_RANGES:
            section, key = full_key.split(".", 1)
            if key not in self.data.get(section, {}):
                continue
            err = check_value_range(full_key, self.data[section][key])
            if err:
                raise ConfigError(err, hint="Исправьте значение в config.yaml или в разделе «Настройки».")

    #: Параметры, которые в файле законно бывают разного вида (строка или список).
    _LENIENT_FILE_KEYS = frozenset({"server.trusted_proxies"})

    def _check_file_values(self) -> None:
        """Проверить значения, заданные в самом файле.

        Раньше опечатка в имени параметра молча игнорировалась, строка вместо
        числа роняла службу сырым ValueError уже при работе, а неизвестный
        часовой пояс незаметно превращался в UTC (расписания съезжали на 3 часа).
        Неверные типы, перечисления и часовой пояс — ошибка; неизвестные
        параметры и прочие сомнительные значения — предупреждение.
        """
        hint = f"Исправьте значение в {self.source_path or 'config.yaml'}."
        for section, values in (self.file_data or {}).items():
            if section not in DEFAULTS:
                self.warnings.append(f"Неизвестный раздел «{section}» — он ни на что не влияет (опечатка?).")
                continue
            if not isinstance(values, dict):
                raise ConfigError(f"Раздел «{section}» должен быть набором «параметр: значение».", hint=hint)
            for key, value in values.items():
                full_key = f"{section}.{key}"
                if key not in DEFAULTS[section]:
                    self.warnings.append(f"Неизвестный параметр «{full_key}» — он ни на что не влияет (опечатка?).")
                    continue
                if full_key in self._LENIENT_FILE_KEYS or full_key in UNSUPPORTED_SETTINGS:
                    continue
                try:
                    coerced = coerce_setting(full_key, value)
                except ValueError as exc:
                    default = DEFAULTS[section][key]
                    strict = (isinstance(default, (bool, int, float)) or full_key in VALUE_CHOICES
                              or full_key == "scheduler.timezone")
                    if strict:
                        raise ConfigError(str(exc), hint=hint) from None
                    self.warnings.append(str(exc))
                    continue
                self.data[section][key] = coerced




def _log_secret_perm_fixed(path: str, mode: int) -> None:
    """Сообщить, что права на secret.key были слишком открытыми и исправлены."""
    try:
        from .logging_setup import get_logger
        get_logger("config").warning(
            "Права на %s были %o (файл читался посторонними) — исправлены на 0600. "
            "Если ключ мог утечь, смените его и введите пароли ящиков заново.", path, mode)
    except Exception:  # noqa: BLE001
        pass


def _log_secret_perm_failed(path: str, exc) -> None:
    """Сообщить, что права на secret.key исправить не удалось."""
    try:
        from .logging_setup import get_logger
        get_logger("config").warning(
            "Права на %s слишком открыты, и исправить их не удалось (%s). "
            "Файл читается посторонними — им подписываются cookie и шифруются пароли ящиков. "
            "Выполните: chown mailarchiver %s && chmod 600 %s", path, exc, path, path)
    except Exception:  # noqa: BLE001
        pass


def _check_trusted_proxies(value):
    """Разобрать server.trusted_proxies → (список сетей, «доверять всем»)."""
    from .web.proxy import parse_trusted
    try:
        return parse_trusted(value)
    except Exception:  # noqa: BLE001
        return [], False


def load_config(path: Optional[str] = None, *, create_dirs: bool = True) -> Config:
    """Загрузить конфигурацию из файла (или значения по умолчанию)."""
    source = None
    file_data: Dict[str, Any] = {}
    if path and not os.path.isfile(path):
        # Явно указанный файл обязан существовать: раньше опечатка в пути
        # молча давала значения по умолчанию и «Конфигурация корректна ✓».
        raise ConfigError(f"Файл конфигурации не найден: {path}",
                          hint="Проверьте путь в параметре -c / --config или в MAILARCHIVER_CONFIG.")
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
    cfg = Config(merged, source_path=source, file_data=file_data)
    cfg.validate()
    if create_dirs:
        cfg.ensure_dirs()
    return cfg


