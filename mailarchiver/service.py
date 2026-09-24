"""
Центральный объект приложения :class:`Services` — связывает конфигурацию, БД,
хранилище, очередь заданий, планировщик и уведомления. Используется веб-слоем,
CLI и фоновыми воркерами.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from .config import Config
from .database import Database
from .errors import ValidationError
from .imap.client import ConnectOptions
from .logging_setup import get_logger
from .models import Account
from .notify import Notifier
from .queue.manager import QueueManager
from .scheduler.scheduler import SchedulerService
from .security import SecretBox
from .storage import MaildirStore
from .export.base import MailItem

log = get_logger("service")


class Services:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.secret_box = SecretBox(cfg.secret_key())
        self.db = Database(
            cfg.db_path, self.secret_box,
            busy_timeout_ms=int(cfg.get("database", "busy_timeout_ms", 10000)),
            wal=bool(cfg.get("database", "wal", True)),
        )
        self.store = MaildirStore(
            cfg.mail_root,
            compress=bool(cfg.storage.get("compress", False)),
            fsync=bool(cfg.storage.get("fsync", True)),
            min_free_mb=int(cfg.storage.get("min_free_space_mb", 500)),
            verify_after_write=bool(cfg.storage.get("verify_after_write", True)),
        )
        # «Сегодня» для дневной статистики — по часам пользователя, а не UTC
        self.db.day_provider = lambda: datetime.now(self.local_tz()).date().isoformat()
        self.notifier = Notifier(self)
        self.queue = QueueManager(self)
        self.scheduler = SchedulerService(self)
        self._started = False

    # -- жизненный цикл ------------------------------------------------------
    def setup(self, *, generate_key: bool = False) -> None:
        """Подготовить каталоги, схему БД и настройки.

        ``generate_key`` — можно ли СОЗДАТЬ ключ шифрования, если шифрование
        включено, а ключа ещё нет. Разрешено только самой службе (serve):
        консольные команды обычно запускают от root, и созданный ими ключ
        (root:root 0600) служба потом не смогла бы прочитать.
        """
        self.cfg.ensure_dirs()
        self.db.init_schema()
        # Настройки из раздела «Настройки» (БД) перекрывают config.yaml. Раньше
        # параметры хранилища брались только из файла, и переключатели «Сжимать
        # письма», fsync, «Минимум свободного места», «Проверять после записи»
        # в интерфейсе сохранялись, но ни на что не влияли.
        self.apply_runtime_settings(generate_key=generate_key)

    # -- применение настроек на лету ---------------------------------------
    def storage_key_path(self) -> str:
        path = str(self.rt("storage", "encryption_key_file") or "").strip()
        return os.path.abspath(path) if path else os.path.join(self.cfg.data_dir, "storage.key")

    def apply_runtime_settings(self, *, generate_key: bool = False, strict: bool = False) -> dict:
        """Применить к работающему сервису настройки, которые можно менять без перезапуска.

        Возвращает состояние шифрования (для интерфейса). При ``strict``
        исключение, если включить шифрование нельзя (потерян ключ, которым уже
        шифровали, нет прав создать ключ).
        """
        store = self.store
        store.compress = bool(self.rt("storage", "compress"))
        store.fsync = bool(self.rt("storage", "fsync"))
        store.verify_after_write = bool(self.rt("storage", "verify_after_write"))
        try:
            store.min_free_bytes = int(self.rt("storage", "min_free_space_mb") or 0) * 1024 * 1024
        except (TypeError, ValueError):
            pass
        # уровень журнала — тоже на лету (вывод в stdout — только после перезапуска)
        try:
            from .logging_setup import set_level
            set_level(str(self.rt("logging", "level") or "INFO"))
        except Exception:  # noqa: BLE001
            pass
        return self._apply_encryption(strict=strict, allow_generate=generate_key)

    def _apply_encryption(self, strict: bool, allow_generate: bool = False) -> dict:
        from .errors import StorageError
        from .storage import crypto
        want = bool(self.rt("storage", "encrypt"))
        path = self.storage_key_path()
        recorded = self.db.get_meta("storage_key_id")
        state = {"requested": want, "active": False, "key_path": path,
                 "key_id": None, "recorded_key_id": recorded, "error": None, "generated": False,
                 "warnings": []}
        cipher = None
        if os.path.exists(path):
            try:
                cipher = crypto.StorageCipher(crypto.load_key_file(path))
                state["key_id"] = cipher.key_id_hex
                state["warnings"] = crypto.key_file_warnings(path)
            except (OSError, StorageError) as exc:
                state["error"] = f"Файл ключа шифрования не читается: {getattr(exc, 'message', exc)}"
        elif want and not recorded and not allow_generate:
            # Ключ создаёт только сама служба (при запуске или при сохранении
            # настроек), а не просмотр состояния и не консольная команда.
            state["error"] = (f"Шифрование включено, но ключа ещё нет ({path}). Он будет создан при "
                              f"запуске службы или при сохранении настроек хранилища.")
        elif want and not recorded:
            # Первое включение: ключа ещё нет и никогда не было — создаём.
            try:
                cipher = crypto.StorageCipher(crypto.generate_key_file(path))
                state["key_id"] = cipher.key_id_hex
                state["generated"] = True
                log.warning("Создан ключ шифрования локальной копии: %s. СОХРАНИТЕ ЕГО В РЕЗЕРВНОЙ "
                            "КОПИИ ОТДЕЛЬНО ОТ КАТАЛОГА ДАННЫХ — без него зашифрованные письма не прочитать.",
                            path)
            except (OSError, StorageError) as exc:
                state["error"] = f"Не удалось создать ключ шифрования {path}: {exc}"
        elif want or recorded:
            state["error"] = (f"Файл ключа шифрования не найден: {path}. Письма, уже зашифрованные "
                              f"им, прочитать нельзя; новый ключ автоматически НЕ создаётся.")
        if cipher is not None and recorded and recorded != cipher.key_id_hex:
            state["error"] = (f"Ключ {path} не тот, которым шифровался архив "
                              f"(ожидался отпечаток {recorded}, а у файла — {cipher.key_id_hex}).")
            cipher = None
        self.store.cipher = cipher
        self.store.encrypt = bool(want and cipher is not None)
        # Шифрование включено, а ключа нет — писать письма открытым текстом
        # нельзя: бэкап откажется сохранять их (см. MaildirStore.store_message).
        self.store.encryption_blocked = (state["error"] or "Ключ шифрования недоступен.") \
            if (want and cipher is None) else ""
        state["active"] = self.store.encrypt
        state["blocked"] = bool(self.store.encryption_blocked)
        for warning in state["warnings"]:
            log.warning("Шифрование копии: %s", warning)
        if self.store.encrypt and not recorded:
            self.db.set_meta("storage_key_id", cipher.key_id_hex)
            state["recorded_key_id"] = cipher.key_id_hex
        if state["error"]:
            log.error("Шифрование копии: %s", state["error"])
            if strict and want:
                raise StorageError(state["error"],
                                   hint="Верните файл ключа из резервной копии или укажите его путь "
                                        "в storage.encryption_key_file.")
        if self.store.encrypt and path.startswith(os.path.abspath(self.cfg.data_dir) + os.sep):
            log.warning("Ключ шифрования лежит внутри каталога данных (%s): он защищает письма, "
                        "только если копию каталога писем уносят без него. Надёжнее хранить ключ "
                        "отдельно (storage.encryption_key_file) и копировать его в другое место.", path)
        return state

    def encryption_status(self) -> dict:
        """Состояние шифрования для интерфейса и консоли. Ключ здесь НЕ создаётся."""
        state = self._apply_encryption(strict=False, allow_generate=False)
        state.update(self.db.count_encrypted_messages())
        inside = state["key_path"].startswith(os.path.abspath(self.cfg.data_dir) + os.sep)
        state["key_inside_data_dir"] = inside
        return state

    def start(self) -> None:
        if self._started:
            return
        orphans = self.db.reset_orphan_jobs()
        stale_runs = self.db.reset_orphan_runs()
        try:
            from .maintenance import startup_cleanup
            startup_cleanup(self)
        except Exception:  # noqa: BLE001
            log.exception("Уборка после прошлого запуска не удалась")
        if stale_runs:
            log.info("Закрыто незавершённых записей прогонов: %d.", stale_runs)
        if orphans:
            log.warning("Обнаружено прерванных заданий при старте: %s (возвращены в очередь/помечены)", orphans)
        self.queue.start()
        self.scheduler.start()
        self._started = True
        log.info("Сервис запущен.")

    def stop(self) -> None:
        if not self._started:
            return
        self.scheduler.stop()
        self.queue.stop()
        self._started = False
        log.info("Сервис остановлен.")

    # -- настройки (БД переопределяет конфиг) --------------------------------
    def rt(self, section: str, key: str):
        """Значение настройки: переопределение из БД или значение из конфига."""
        override = self.db.get_setting(f"{section}.{key}", None)
        if override is not None:
            return override
        return self.cfg.get(section, key)

    def set_rt(self, section: str, key: str, value) -> None:
        self.db.set_setting(f"{section}.{key}", value)

    # -- параметры подключения ----------------------------------------------
    def connect_options(self) -> ConnectOptions:
        return ConnectOptions(
            connect_timeout_s=int(self.rt("backup", "connect_timeout_s")),
            socket_timeout_s=int(self.rt("backup", "socket_timeout_s")),
            verify_ssl=bool(self.rt("security", "imap_ssl_verify")),
            fetch_batch_size=int(self.rt("backup", "fetch_batch_size")),
            on_refresh_token=self._save_refresh_token,
            master=self.master_credentials(),
        )

    def master_credentials(self) -> Optional[dict]:
        """Учётная запись администратора почты для ящиков «вход через администратора»."""
        if not bool(self.rt("mailadmin", "enabled")):
            return None
        return {"host": str(self.rt("mailadmin", "host") or "").strip(),
                "user": str(self.rt("mailadmin", "user") or "").strip(),
                "password": str(self.rt("mailadmin", "password") or ""),
                "mode": str(self.rt("mailadmin", "mode") or "sasl_plain"),
                "separator": str(self.rt("mailadmin", "separator") or "*")}

    def _save_refresh_token(self, account: Account, token: str) -> None:
        """Сервер OAuth2 выдал новый refresh-токен — сохранить его (Microsoft 365
        меняет токен при каждом обновлении, и старый со временем перестаёт работать)."""
        if account.id:
            self.db.set_account_oauth_refresh(int(account.id), token)
            log.info("Ящик «%s»: сохранён новый refresh-токен OAuth2.", account.name)

    # -- источник писем для экспорта/восстановления -------------------------
    def local_tz(self):
        """Часовой пояс пользователя (scheduler.timezone) — для границ дат и графиков."""
        from zoneinfo import ZoneInfo
        try:
            return ZoneInfo(str(self.rt("scheduler", "timezone") or "UTC"))
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def day_bounds_utc(self, date_from: Optional[str], date_to: Optional[str]):
        """Границы периода в UTC по датам в МЕСТНОМ времени пользователя.

        Даты писем хранятся в UTC, а «с 15.09 по 15.09» пользователь в Москве
        понимает по своим часам: письмо, пришедшее 15.09 в 01:30 по Москве
        (14.09 в 22:30 UTC), должно попасть в выгрузку за 15-е. Возвращает
        ``(начало, конец)`` — ISO-строки UTC или None, конец не включается.
        """
        from datetime import timedelta
        from .util import parse_day
        tz = self.local_tz()
        start = end = None
        d_from = parse_day(date_from) if date_from else None
        d_to = parse_day(date_to) if date_to else None
        if d_from and d_to and d_from > d_to:
            raise ValidationError("Начало периода позже его конца.", hint="Проверьте даты «с» и «по».")
        if d_from:
            start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=tz).astimezone(timezone.utc).isoformat()
        if d_to:
            nxt = d_to + timedelta(days=1)
            end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(timezone.utc).isoformat()
        return start, end

    def iter_mail_items(self, account_id: int, folders: Optional[List[str]] = None,
                        date_from: Optional[str] = None, date_to: Optional[str] = None,
                        limit: int = 0, on_skip=None) -> Iterator[MailItem]:
        """Письма ящика для экспорта — постранично по id (устойчиво к параллельной записи).

        Письмо, которое не удалось прочитать (нет файла, нет ключа шифрования),
        раньше молча пропускалось с одной строкой в журнале службы, и экспорт
        сообщал «успешно, ошибок 0». Теперь о каждом таком письме сообщается
        вызовом ``on_skip(строка_индекса, текст_ошибки)``.
        """
        since, until = self.day_bounds_utc(date_from, date_to)
        page = 2000
        count = 0
        targets = list(dict.fromkeys(f for f in (folders or []) if f)) or [None]
        for fld in targets:
            last_id = 0
            while True:
                rows = self.db.export_rows(account_id, fld, since, until, last_id, page)
                if not rows:
                    break
                for row in rows:
                    last_id = row["id"]
                    try:
                        raw = self.store.read_message(account_id, row["stored_path"])
                    except Exception as exc:  # noqa: BLE001
                        text = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
                        log.warning("Письмо не прочитано при экспорте (%s): %s", row["stored_path"], text)
                        if on_skip is not None:
                            on_skip(row, text)
                        continue
                    idate = row["internaldate"] or ""
                    epoch = None
                    if idate:
                        try:
                            epoch = datetime.fromisoformat(idate).timestamp()
                        except (ValueError, TypeError):
                            epoch = None
                    yield MailItem(folder=row["folder"], raw=raw,
                                   flags=[f for f in (row["flags"] or "").split(",") if f],
                                   internaldate=epoch, message_id=row["message_id"] or "",
                                   size=row["size"] or len(raw))
                    count += 1
                    if limit and count >= limit:
                        return
                if len(rows) < page:
                    break

    def count_mail_items(self, account_id: int, folders: Optional[List[str]] = None,
                         date_from: Optional[str] = None, date_to: Optional[str] = None) -> int:
        """Сколько писем попадёт в экспорт — с учётом папок И периода."""
        since, until = self.day_bounds_utc(date_from, date_to)
        targets = list(dict.fromkeys(f for f in (folders or []) if f)) or [None]
        return sum(self.db.count_export_rows(account_id, fld, since, until) for fld in targets)

    # -- удобные фабрики -----------------------------------------------------
    def require_account(self, account_id: int, *, need_secret: bool = False) -> Account:
        acc = self.db.get_account(account_id)
        if acc is None:
            raise ValidationError(f"Ящик с id={account_id} не найден.")
        if need_secret and acc.secret_broken:
            raise ValidationError(
                f"Пароль ящика «{acc.name}» не расшифровывается текущим ключом шифрования.",
                hint="Файл secret.key в каталоге данных заменён или утрачен. "
                     "Откройте ящик и введите пароль заново.")
        return acc

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
