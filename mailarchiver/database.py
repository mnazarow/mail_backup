"""
Слой доступа к данным (SQLite).

Особенности:
  * потоко-безопасность: у каждого потока своя connection к одному файлу БД
    (WAL позволяет параллельные чтения и одну запись);
  * busy_timeout, чтобы конкурентные записи ждали, а не падали;
  * простая система версий схемы (миграции) на будущее;
  * все секреты ящиков хранятся в зашифрованном виде (см. security.SecretBox).

Класс :class:`Database` намеренно содержит все доменные операции — это делает
код остальных модулей коротким и единообразным.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import models
from .logging_setup import get_logger
from .security import SecretBox
from .util import chunked, utcnow_iso

SCHEMA_VERSION = 1

log = get_logger("db")

#: Настройки, значения которых хранятся в БД в зашифрованном виде
#: (шифруются в :meth:`Database.set_setting`, расшифровываются в
#: :meth:`Database.get_setting`).
_ENCRYPTED_SETTINGS = {"notifications.smtp_password", "employees.source_url_password",
                       "replica.s3_secret_key", "monitoring.metrics_token", "mailadmin.password"}


#: Выборка сотрудника вместе с названием и состоянием привязанного ящика:
#: интерфейсу нужны account_name/account_enabled, а отдельный запрос на строку
#: превратил бы список в N+1 обращений к БД.
_EMPLOYEE_SELECT = (
    "SELECT e.*, a.name AS account_name, a.enabled AS account_enabled, a.hold_until AS account_hold_until "
    "FROM employees e LEFT JOIN accounts a ON a.id = e.account_id"
)


def _row_str(row, key: str) -> str:
    """Значение колонки строкой ('' если колонки нет или там NULL)."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return ""
    return "" if value is None else str(value)


def _row_int(row, key: str) -> int:
    """Значение колонки числом (0, если колонки нет или там NULL)."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ma_lower(value):
    """LOWER() с поддержкой кириллицы (регистрируется в каждом соединении)."""
    return value.lower() if isinstance(value, str) else value


class Database:
    def __init__(self, path: str, secret_box: SecretBox, *, busy_timeout_ms: int = 10000, wal: bool = True) -> None:
        self.path = path
        self.secret = secret_box
        self.busy_timeout_ms = busy_timeout_ms
        self.wal = wal
        self._local = threading.local()
        self._write_lock = threading.RLock()

    # -- соединения ----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys=ON")
            # Встроенные LOWER()/LIKE в SQLite понимают только латиницу: поиск
            # «иванов» не нашёл бы «Иванов». Регистр приводим средствами Python.
            conn.create_function("ma_lower", 1, _ma_lower, deterministic=True)
            if self.wal:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- низкоуровневые помощники -------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = self.connect()
        with self._write_lock:
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
            except BaseException:
                # Без отката транзакция осталась бы открытой и держала writer-lock —
                # вся БД встала бы с «database is locked».
                self._safe_rollback(conn)
                raise

    def executemany(self, sql: str, seq_params) -> None:
        conn = self.connect()
        with self._write_lock:
            try:
                conn.executemany(sql, seq_params)
                conn.commit()
            except BaseException:
                self._safe_rollback(conn)
                raise

    @staticmethod
    def _safe_rollback(conn: sqlite3.Connection) -> None:
        """Откатить транзакцию, не маскируя исходную ошибку."""
        try:
            conn.rollback()
        except sqlite3.Error as exc:  # pragma: no cover - крайне редкий случай
            log.warning("Не удалось откатить транзакцию: %s", exc)

    @contextmanager
    def transaction(self):
        """Несколько операторов одной транзакцией: всё или ничего."""
        conn = self.connect()
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except BaseException:
                self._safe_rollback(conn)
                raise

    def query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = ()):
        row = self.query_one(sql, params)
        return row[0] if row is not None else None

    # -- схема ---------------------------------------------------------------
    def init_schema(self) -> None:
        conn = self.connect()
        with self._write_lock:
            try:
                conn.executescript(_SCHEMA_SQL)
                cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                row = cur.fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                conn.commit()
            except BaseException:
                self._safe_rollback(conn)
                raise
            self._migrate(conn)
            # Полнотекстовый поиск (FTS5): таблица и триггер удаления. Без FTS5
            # в сборке SQLite поиск работает по теме и отправителю.
            from .search import ensure_schema as _ensure_search_schema
            try:
                self.fts_available = _ensure_search_schema(conn)
                conn.commit()
            except BaseException:
                self._safe_rollback(conn)
                raise
            # Отпечатки вложений для отчёта «Одинаковые вложения» (таблица и триггер удаления).
            from .dedup import ensure_schema as _ensure_dedup_schema
            _ensure_dedup_schema(conn)
        self._encrypt_plain_settings()

    def _encrypt_plain_settings(self) -> None:
        """Зашифровать секреты-настройки, сохранённые прежними версиями открытым текстом.

        Пароль источника сотрудников (employees.source_url_password) до 1.3.0
        лежал в БД как есть, хотя интерфейс и документация обещали шифрование.
        Значение, похожее на токен Fernet, но не расшифровываемое, не трогаем:
        это зашифрованный секрет при сменившемся ключе, а не открытый текст.
        """
        for key in _ENCRYPTED_SETTINGS:
            row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
            if row is None:
                continue
            try:
                value = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            if not isinstance(value, str) or not value:
                continue
            try:
                self.secret.decrypt(value)
                continue                       # уже зашифровано
            except Exception:  # noqa: BLE001
                if value.startswith("gAAAAA"):
                    continue
            self.set_setting(key, value)
            log.info("Настройка %s зашифрована в базе (была сохранена открытым текстом).", key)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Безопасные миграции для БД, созданных прошлыми версиями (ADD COLUMN)."""
        def cols(table: str):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        migrations = [
            ("accounts", "retention_days", "INTEGER DEFAULT -1"),
            ("messages", "subject", "TEXT"),
            ("messages", "from_addr", "TEXT"),
            ("messages", "has_attach", "INTEGER DEFAULT 0"),
            ("sessions", "role", "TEXT DEFAULT 'admin'"),
            ("sessions", "account_id", "INTEGER"),
            # двухфакторный вход (1.3.0)
            ("users", "totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "totp_secret_enc", "TEXT DEFAULT ''"),
            ("users", "totp_pending_enc", "TEXT DEFAULT ''"),
            ("users", "totp_last_step", "INTEGER NOT NULL DEFAULT 0"),
            ("users", "totp_recovery", "TEXT DEFAULT '[]'"),
            # счётчик нечитаемых папок растёт раз в сутки (1.3.0)
            ("folder_problems", "counted_at", "TEXT"),
            # автор выгрузки хранится в ней самой: задания чистятся раньше (1.3.0)
            ("exports", "created_by", "TEXT DEFAULT ''"),
            # отложенный повтор хранится в БД, а не в таймере процесса (1.3.0)
            ("jobs", "run_after", "TEXT"),
            ("jobs", "restarts", "INTEGER DEFAULT 0"),
            # итог последней проверки входа и даты резервных копий (1.3.0)
            ("accounts", "login_status", "TEXT DEFAULT ''"),
            ("accounts", "login_checked_at", "TEXT"),
            ("accounts", "login_error", "TEXT DEFAULT ''"),
            ("accounts", "first_backup_at", "TEXT"),
            ("accounts", "last_backup_at", "TEXT"),
            ("accounts", "last_backup_status", "TEXT DEFAULT ''"),
            # вид попытки входа: пароль / код 2FA / проверка ящика по IMAP (1.3.0)
            ("login_attempts", "kind", "TEXT DEFAULT ''"),
            # у сотрудника уже был ящик: удалённый администратором ящик ночная
            # синхронизация больше не пересоздаёт (1.3.0)
            ("employees", "had_account", "INTEGER NOT NULL DEFAULT 0"),
            # удержание архива и увольнение сотрудника (1.4.0)
            ("accounts", "hold_until", "TEXT DEFAULT ''"),
            ("accounts", "hold_reason", "TEXT DEFAULT ''"),
            ("accounts", "dismissed_at", "TEXT"),
            ("accounts", "auto_disabled", "INTEGER NOT NULL DEFAULT 0"),
            ("employees", "dismissed_at", "TEXT"),
        ]
        changed = False
        # Индекс (account_id, folder) полностью покрыт уникальным ключом
        # (account_id, folder, uidvalidity, uid) и idx_messages_date — лишний
        # индекс только замедлял запись писем.
        try:
            conn.execute("DROP INDEX IF EXISTS idx_messages_acc")
            changed = True
        except sqlite3.OperationalError as exc:
            log.warning("Не удалось удалить лишний индекс idx_messages_acc: %s", exc)
        for table, col, decl in migrations:
            try:
                if col not in cols(table):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    changed = True
            except sqlite3.OperationalError as exc:
                # Не падаем на одной миграции, но и не молчим: иначе код позже
                # упадёт на отсутствующей колонке без всяких объяснений.
                self._safe_rollback(conn)
                log.error("Миграция не выполнена: %s.%s (%s) — %s", table, col, decl, exc)
        if changed:
            try:
                conn.commit()
            except BaseException:
                self._safe_rollback(conn)
                raise
        # Индекс по новой колонке — только после её появления в старой базе.
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_kind ON login_attempts(kind, ts)")
            conn.execute("UPDATE employees SET had_account=1 WHERE account_id IS NOT NULL AND had_account=0")
            # поиск ящика по логину — на каждой строке синхронизации сотрудников
            conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username COLLATE NOCASE)")
            conn.commit()
        except sqlite3.Error as exc:
            self._safe_rollback(conn)
            log.warning("Не удалось создать индекс idx_login_attempts_kind: %s", exc)
        # Даты резервных копий для базы прежней версии: первая — по самому
        # раннему сохранённому письму, последняя — по истории прогонов.
        try:
            if conn.execute("SELECT 1 FROM accounts WHERE first_backup_at IS NULL LIMIT 1").fetchone():
                conn.execute(
                    "UPDATE accounts SET first_backup_at=(SELECT MIN(backed_up_at) FROM messages m "
                    "WHERE m.account_id=accounts.id) WHERE first_backup_at IS NULL")
                conn.execute(
                    "UPDATE accounts SET last_backup_at=(SELECT MAX(finished_at) FROM runs r "
                    "WHERE r.account_id=accounts.id AND r.type='backup' AND r.status IN ('success','partial')) "
                    "WHERE last_backup_at IS NULL")
                conn.commit()
        except sqlite3.Error as exc:
            self._safe_rollback(conn)
            log.warning("Не удалось заполнить даты резервных копий ящиков: %s", exc)
        self._drop_legacy_retention_schedules(conn)

    #: Отметка в meta: устаревшие расписания очистки уже убраны (один раз).
    _LEGACY_RETENTION_MARK = "legacy_retention_schedules_removed"

    def _drop_legacy_retention_schedules(self, conn: sqlite3.Connection) -> None:
        """Убрать расписания очистки, которые версии до 1.3.0 заводили сами.

        При выборе срока хранения в меню ящика прежние версии создавали ящику
        расписание «очистка, cron 30 3 * * *, без параметров». Теперь их работу
        делает ежедневный обход (retention.cron), который ставит очистку только
        ящикам, где есть что удалять, а старые расписания ставили её КАЖДОМУ
        такому ящику каждую ночь — сотни пустых заданий в очереди и истории.
        Выполняется один раз: расписание, созданное вручную позже, не трогаем.
        """
        try:
            if conn.execute("SELECT 1 FROM meta WHERE key=?", (self._LEGACY_RETENTION_MARK,)).fetchone():
                return
            cur = conn.execute(
                "DELETE FROM schedules WHERE job_type='retention' AND kind='cron' "
                "AND TRIM(COALESCE(cron_expr, ''))='30 3 * * *' "
                "AND TRIM(COALESCE(options, '')) IN ('', '{}')")
            removed = max(0, cur.rowcount or 0)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                         (self._LEGACY_RETENTION_MARK, str(removed)))
            if removed:
                conn.execute(
                    "INSERT INTO audit(ts, user, action, detail) VALUES(?,?,?,?)",
                    (utcnow_iso(), "system", "schedules_cleanup",
                     f"удалено устаревших расписаний очистки ящиков: {removed} — очистку по сроку "
                     f"хранения теперь ставит ежедневный обход (retention.cron)"))
            conn.commit()
            if removed:
                log.info("Удалено устаревших расписаний очистки ящиков: %d — их работу выполняет "
                         "ежедневная очистка по срокам хранения (retention.cron).", removed)
        except sqlite3.Error as exc:
            self._safe_rollback(conn)
            log.warning("Не удалось убрать устаревшие расписания очистки: %s", exc)

    # ======================================================================
    #  Пользователи и вход
    # ======================================================================
    def count_users(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM users") or 0)

    def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        cur = self.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (username, password_hash, role, utcnow_iso()),
        )
        return int(cur.lastrowid)

    def create_first_user(self, username: str, password_hash: str, role: str = "admin") -> Optional[int]:
        """Создать ПЕРВОГО пользователя атомарно; None — если кто-то уже опередил.

        Проверка «пользователей ещё нет» и вставка выполняются одним оператором
        SQL (``INSERT ... SELECT ... WHERE NOT EXISTS``), поэтому два
        одновременных ``POST /api/setup`` на свежей установке не создадут двух
        администраторов: второй запрос не вставит ничего и получит None.
        Дополнительно защищает UNIQUE(username) — при совпадении имени вызов
        поднимет sqlite3.IntegrityError.
        """
        cur = self.execute(
            "INSERT INTO users(username, password_hash, role, created_at) "
            "SELECT ?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM users)",
            (username, password_hash, role, utcnow_iso()),
        )
        if not cur.rowcount:
            return None
        return int(cur.lastrowid)

    def get_user_by_name(self, username: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,))

    def get_user_by_id(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM users WHERE id=?", (user_id,))

    def list_users(self) -> List[sqlite3.Row]:
        return self.query("SELECT id, username, role, created_at, last_login, disabled, totp_enabled "
                          "FROM users ORDER BY id")

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        self.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        # Смена пароля обязана завершать все открытые сеансы: иначе сценарий
        # «нас взломали, меняю пароль» не работал — cookie злоумышленника
        # жила до конца session_ttl_hours. Заодно отзываются выданные по
        # старому паролю билеты второго шага входа.
        self.delete_user_sessions(user_id)
        self.drop_user_otp_challenges(user_id)

    def set_last_login(self, user_id: int) -> None:
        self.execute("UPDATE users SET last_login=? WHERE id=?", (utcnow_iso(), user_id))

    def set_user_disabled(self, user_id: int, disabled: bool) -> None:
        self.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        if disabled:
            self.delete_user_sessions(user_id)
            self.drop_user_otp_challenges(user_id)

    def delete_user(self, user_id: int) -> None:
        self.delete_user_sessions(user_id)
        self.drop_user_otp_challenges(user_id)
        self.execute("DELETE FROM users WHERE id=?", (user_id,))

    # ---- двухфакторный вход (TOTP) ----------------------------------------
    def totp_state(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Состояние 2FA пользователя: секреты расшифрованы (или пусты)."""
        row = self.query_one(
            "SELECT totp_enabled, totp_secret_enc, totp_pending_enc, totp_last_step, totp_recovery "
            "FROM users WHERE id=?", (user_id,))
        if row is None:
            return None
        broken = [False]
        try:
            recovery = json.loads(row["totp_recovery"] or "[]")
        except (TypeError, ValueError):
            recovery = []
        return {
            "enabled": bool(row["totp_enabled"]),
            "secret": self._decrypt_opt(row["totp_secret_enc"], broken),
            "pending": self._decrypt_opt(row["totp_pending_enc"], broken),
            "last_step": int(row["totp_last_step"] or 0),
            "recovery": recovery if isinstance(recovery, list) else [],
            "secret_broken": broken[0],
        }

    def set_totp_pending(self, user_id: int, secret_b32: str) -> None:
        self.execute("UPDATE users SET totp_pending_enc=? WHERE id=?",
                     (self.secret.encrypt(secret_b32) if secret_b32 else "", user_id))

    def enable_totp(self, user_id: int, secret_b32: str, recovery_hashes: List[str], step: int) -> None:
        self.execute(
            "UPDATE users SET totp_enabled=1, totp_secret_enc=?, totp_pending_enc='', "
            "totp_last_step=?, totp_recovery=? WHERE id=?",
            (self.secret.encrypt(secret_b32), int(step), json.dumps(recovery_hashes), user_id))

    def disable_totp(self, user_id: int) -> None:
        self.execute(
            "UPDATE users SET totp_enabled=0, totp_secret_enc='', totp_pending_enc='', "
            "totp_last_step=0, totp_recovery='[]' WHERE id=?", (user_id,))
        self.drop_user_otp_challenges(user_id)

    def claim_totp_step(self, user_id: int, step: int) -> bool:
        """Атомарно отметить шаг TOTP использованным.

        ``False`` — этот (или более поздний) шаг уже использован: код
        предъявлен повторно, например перехвачен. Проверка и запись — одним
        оператором, поэтому два одновременных входа с одним кодом не проходят.
        """
        cur = self.execute("UPDATE users SET totp_last_step=? WHERE id=? AND totp_last_step < ?",
                           (int(step), user_id, int(step)))
        return bool(cur.rowcount)

    def set_totp_recovery(self, user_id: int, recovery_hashes: List[str]) -> None:
        self.execute("UPDATE users SET totp_recovery=? WHERE id=?", (json.dumps(recovery_hashes), user_id))

    def consume_totp_recovery(self, user_id: int, used_hash: str) -> bool:
        """Вычеркнуть использованный резервный код (атомарно)."""
        conn = self.connect()
        with self._write_lock:
            try:
                row = conn.execute("SELECT totp_recovery FROM users WHERE id=?", (user_id,)).fetchone()
                items = json.loads((row[0] if row else "") or "[]")
                if used_hash not in items:
                    return False
                items.remove(used_hash)
                conn.execute("UPDATE users SET totp_recovery=? WHERE id=?", (json.dumps(items), user_id))
                conn.commit()
                return True
            except BaseException:
                self._safe_rollback(conn)
                raise

    def delete_mailbox_sessions(self, account_id: int) -> int:
        """Завершить сеансы сотрудника, вошедшего по паролю этого ящика."""
        cur = self.execute("DELETE FROM sessions WHERE role='mailbox' AND account_id=?", (int(account_id),))
        return int(cur.rowcount or 0)

    def delete_user_sessions(self, user_id: int) -> int:
        """Завершить все сеансы пользователя веб-интерфейса.

        Сеансы входа ПО ЯЩИКУ создаются с ``user_id = 0`` (у них нет записи в
        таблице users), поэтому они исключены явно: иначе опечатка в id —
        ``/api/users/0/disable`` — разом выкидывала бы из интерфейса всех, кто
        вошёл по учётным данным своего ящика.
        """
        if not user_id or int(user_id) <= 0:
            return 0
        cur = self.execute("DELETE FROM sessions WHERE user_id=? AND role IS NOT 'mailbox'", (user_id,))
        try:
            return int(cur.rowcount or 0)
        except Exception:  # noqa: BLE001
            return 0

    def record_login_attempt(self, username: str, success: bool, ip: str = "", kind: str = "") -> int:
        """Записать попытку входа; вернуть её id.

        ``kind``: '' — пароль, 'otp' — код второго шага, 'imap' — проверка
        пароля почтового ящика на IMAP-сервере.
        """
        cur = self.execute(
            "INSERT INTO login_attempts(username, ts, success, ip, kind) VALUES(?,?,?,?,?)",
            (username, utcnow_iso(), 1 if success else 0, ip, kind or ""),
        )
        return int(cur.lastrowid or 0)

    def delete_login_attempt(self, attempt_id: int) -> None:
        """Убрать записанную заранее «неудачу», если попытка оказалась успешной."""
        if attempt_id:
            self.execute("DELETE FROM login_attempts WHERE id=?", (int(attempt_id),))

    def count_recent_failures(self, username: str, since_iso: str, ip: Optional[str] = None,
                              kind: Optional[str] = None) -> int:
        """Неудачные попытки входа за период.

        ``ip`` задан → считаются только попытки с этого источника, то есть по
        ПАРЕ «имя пользователя + IP». Без него — по учётной записи целиком
        (прежнее поведение, используется для общесистемной защиты).
        ``kind`` задан → только попытки этого вида (см. :meth:`record_login_attempt`).
        """
        sql = "SELECT COUNT(*) FROM login_attempts WHERE username=? COLLATE NOCASE AND success=0 AND ts>=?"
        args: list = [username, since_iso]
        if ip is not None:
            sql += " AND ip=?"
            args.append(ip)
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        return int(self.scalar(sql, tuple(args)) or 0)

    def count_recent_failures_of_kind(self, kind: str, since_iso: str) -> int:
        """Неудачные попытки одного вида по ВСЕМ учётным записям (например, все
        проверки паролей ящиков на IMAP-сервере)."""
        return int(self.scalar("SELECT COUNT(*) FROM login_attempts WHERE kind=? AND success=0 AND ts>=?",
                               (kind, since_iso)) or 0)

    # ---- билеты второго шага входа ----------------------------------------
    def create_otp_challenge(self, nonce: str, user_id: int, expires: int, pw: str) -> None:
        now = int(time.time())
        with self.transaction() as conn:
            conn.execute("DELETE FROM otp_challenges WHERE expires < ?", (now,))
            conn.execute("INSERT INTO otp_challenges(nonce, user_id, expires, attempts, pw) VALUES(?,?,?,0,?)",
                         (nonce, int(user_id), int(expires), pw or ""))

    def take_otp_attempt(self, nonce: str, user_id: int, max_attempts: int) -> Optional[Tuple[str, int]]:
        """Засчитать попытку ввода кода по билету — ДО проверки кода.

        Возвращает ``(отпечаток пароля, с которым выдан билет; номер попытки)``
        или None: билета нет (использован, истёк, выдан до обновления) либо
        попытки по нему исчерпаны. Проверка и увеличение счётчика — одним
        оператором, поэтому залп параллельных запросов не получит лишних попыток.
        """
        cur = self.execute(
            "UPDATE otp_challenges SET attempts=attempts+1 "
            "WHERE nonce=? AND user_id=? AND expires>=? AND attempts<?",
            (nonce, int(user_id), int(time.time()), int(max_attempts)))
        if not cur.rowcount:
            return None
        row = self.query_one("SELECT pw, attempts FROM otp_challenges WHERE nonce=?", (nonce,))
        if row is None:
            return None
        return (row["pw"] or ""), int(row["attempts"] or 0)

    def drop_otp_challenge(self, nonce: str) -> None:
        self.execute("DELETE FROM otp_challenges WHERE nonce=?", (nonce,))

    def drop_user_otp_challenges(self, user_id: int) -> None:
        self.execute("DELETE FROM otp_challenges WHERE user_id=?", (int(user_id),))

    def purge_expired_otp_challenges(self) -> int:
        cur = self.execute("DELETE FROM otp_challenges WHERE expires < ?", (int(time.time()),))
        return int(cur.rowcount or 0)

    def count_recent_failures_by_ip(self, ip: str, since_iso: str) -> int:
        """Неудачные попытки входа с одного источника по ВСЕМ именам пользователей.

        Нужно против перебора имён с одного адреса: блокируется сам источник,
        а не чужие учётные записи.
        """
        if not ip:
            return 0
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM login_attempts WHERE ip=? AND success=0 AND ts>=?",
                (ip, since_iso),
            )
            or 0
        )

    def clear_login_failures(self, username: str, ip: Optional[str] = None) -> None:
        """Сбросить журнал неудач: по паре «имя пользователя + IP» либо (без ip)
        по учётной записи целиком — прежнее поведение."""
        if ip is None:
            self.execute("DELETE FROM login_attempts WHERE username=? COLLATE NOCASE", (username,))
            return
        self.execute("DELETE FROM login_attempts WHERE username=? COLLATE NOCASE AND ip=?", (username, ip))

    def purge_older_login(self, older_than_iso: str) -> int:
        return self.purge_old_login_attempts(older_than_iso)

    def purge_old_login_attempts(self, older_than_iso: str) -> int:
        """Удалить попытки входа старше указанной отметки времени (ISO-8601).

        Таблица иначе растёт бесконечно: её пишет каждый вход, а чистит только
        успешный вход конкретного пользователя.
        """
        cur = self.execute("DELETE FROM login_attempts WHERE ts < ?", (older_than_iso,))
        return int(cur.rowcount or 0)

    # ======================================================================
    #  Сессии
    # ======================================================================
    def create_session(self, token: str, user_id: int, expires_iso: str, ip: str = "", ua: str = "",
                       role: str = "admin", account_id: Optional[int] = None) -> None:
        self.execute(
            "INSERT INTO sessions(token, user_id, role, account_id, created_at, last_seen, expires_at, ip, user_agent) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (token, user_id, role, account_id, utcnow_iso(), utcnow_iso(), expires_iso, ip, ua[:300]),
        )

    def get_session(self, token: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM sessions WHERE token=?", (token,))

    def touch_session(self, token: str) -> None:
        self.execute("UPDATE sessions SET last_seen=? WHERE token=?", (utcnow_iso(), token))

    def delete_session(self, token: str) -> None:
        self.execute("DELETE FROM sessions WHERE token=?", (token,))

    def purge_expired_sessions(self) -> None:
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (utcnow_iso(),))

    # ======================================================================
    #  Аккаунты (почтовые ящики)
    # ======================================================================
    def _decrypt_opt(self, value, broken: List[bool]) -> str:
        """Расшифровать секрет; при неподходящем ключе вернуть пустую строку.

        Без этого потеря (или замена) secret.key убивала ВЕСЬ интерфейс:
        _account_from_row поднимал AuthError, и /api/state, /api/accounts и
        WebSocket отвечали 400 — до списка ящиков, где предлагается ввести
        пароль заново, добраться было нельзя.
        """
        if not value:
            return ""
        try:
            return self.secret.decrypt(value)
        except Exception:  # noqa: BLE001
            broken[0] = True
            return ""

    def _account_from_row(self, row: sqlite3.Row) -> models.Account:
        broken = [False]
        password = self._decrypt_opt(row["password_enc"], broken)
        oauth_secret = self._decrypt_opt(row["oauth_client_secret_enc"], broken)
        oauth_refresh = self._decrypt_opt(row["oauth_refresh_token_enc"], broken)
        if broken[0]:
            log.warning("Ящик «%s» (id=%s): сохранённый пароль не расшифровывается текущим "
                        "secret.key — введите пароль заново.", row["name"], row["id"])
        return models.Account(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            port=row["port"],
            username=row["username"],
            password=password,
            auth_type=row["auth_type"],
            security=row["security"],
            enabled=bool(row["enabled"]),
            folder_include=json.loads(row["folder_include"] or "[]"),
            folder_exclude=json.loads(row["folder_exclude"] or "[]"),
            oauth_client_id=row["oauth_client_id"] or "",
            oauth_client_secret=oauth_secret,
            oauth_refresh_token=oauth_refresh,
            oauth_token_url=row["oauth_token_url"] or "",
            notes=row["notes"] or "",
            retention_days=row["retention_days"] if row["retention_days"] is not None else -1,
            secret_broken=broken[0],
            login_status=_row_str(row, "login_status"),
            login_checked_at=_row_str(row, "login_checked_at"),
            login_error=_row_str(row, "login_error"),
            first_backup_at=_row_str(row, "first_backup_at"),
            last_backup_at=_row_str(row, "last_backup_at"),
            last_backup_status=_row_str(row, "last_backup_status"),
            hold_until=_row_str(row, "hold_until"),
            hold_reason=_row_str(row, "hold_reason"),
            dismissed_at=_row_str(row, "dismissed_at"),
            auto_disabled=bool(_row_int(row, "auto_disabled")),
        )

    def set_account_hold(self, account_id: int, hold_until: str, reason: str = "manual") -> None:
        """Удержание архива ящика: до даты (ГГГГ-ММ-ДД) очистка по сроку его не трогает."""
        self.execute("UPDATE accounts SET hold_until=?, hold_reason=?, updated_at=? WHERE id=?",
                     (hold_until or "", reason if hold_until else "", utcnow_iso(), account_id))

    def mark_account_dismissed(self, account_id: int, dismissed_at: Optional[str]) -> None:
        self.execute("UPDATE accounts SET dismissed_at=?, updated_at=? WHERE id=?",
                     (dismissed_at, utcnow_iso(), account_id))

    def set_account_auto_disabled(self, account_id: int, disabled: bool) -> None:
        """Выключить копирование «из-за увольнения» (или вернуть его)."""
        self.execute("UPDATE accounts SET enabled=?, auto_disabled=?, updated_at=? WHERE id=?",
                     (0 if disabled else 1, 1 if disabled else 0, utcnow_iso(), account_id))

    # ---- итог проверки входа и даты копий -------------------------------
    def set_login_status(self, account_id: int, status: str, error: str = "") -> None:
        """Запомнить, чем кончилась последняя попытка входа в ящик.

        ``status``: ok | auth_error | conn_error | no_password | secret_broken.
        Пишется проверкой паролей, кнопкой «Проверить» и каждым бэкапом —
        по этому полю в разделе «Почтовые ящики» отбираются ящики с неверным
        паролем.
        """
        self.execute("UPDATE accounts SET login_status=?, login_checked_at=?, login_error=? WHERE id=?",
                     (status, utcnow_iso(), (error or "")[:500], account_id))

    def note_backup_result(self, account_id: int, status: str) -> None:
        """Итог копирования: дата последней удачной копии и первой копии ящика."""
        now = utcnow_iso()
        if status in (models.JobStatus.SUCCESS, models.JobStatus.PARTIAL):
            self.execute(
                "UPDATE accounts SET last_backup_at=?, last_backup_status=?, "
                "first_backup_at=COALESCE(first_backup_at, (SELECT MIN(backed_up_at) FROM messages m "
                "WHERE m.account_id=accounts.id), ?) WHERE id=?",
                (now, status, now, account_id))
        else:
            self.execute("UPDATE accounts SET last_backup_status=? WHERE id=?", (status, account_id))

    def create_account(self, acc: models.Account) -> int:
        cur = self.execute(
            """INSERT INTO accounts(
                    name, host, port, username, password_enc, auth_type, security, enabled,
                    folder_include, folder_exclude,
                    oauth_client_id, oauth_client_secret_enc, oauth_refresh_token_enc, oauth_token_url,
                    notes, retention_days, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                acc.name, acc.host, acc.port, acc.username,
                self.secret.encrypt(acc.password) if acc.password else "",
                acc.auth_type, acc.security, 1 if acc.enabled else 0,
                json.dumps(acc.folder_include, ensure_ascii=False),
                json.dumps(acc.folder_exclude, ensure_ascii=False),
                acc.oauth_client_id,
                self.secret.encrypt(acc.oauth_client_secret) if acc.oauth_client_secret else "",
                self.secret.encrypt(acc.oauth_refresh_token) if acc.oauth_refresh_token else "",
                acc.oauth_token_url, acc.notes, acc.retention_days, utcnow_iso(), utcnow_iso(),
            ),
        )
        return int(cur.lastrowid)

    def update_account(self, acc: models.Account, *, update_password: bool = True,
                       update_oauth_secret: bool = True, update_oauth_token: bool = True) -> None:
        """Обновить ящик. Каждый секрет обновляется НЕЗАВИСИМО — пустое значение
        не затирает уже сохранённый секрет (если соответствующий флаг False)."""
        fields = [
            "name=?", "host=?", "port=?", "username=?", "auth_type=?", "security=?",
            "enabled=?", "folder_include=?", "folder_exclude=?",
            "oauth_client_id=?", "oauth_token_url=?", "notes=?", "retention_days=?", "updated_at=?",
        ]
        params: List[Any] = [
            acc.name, acc.host, acc.port, acc.username, acc.auth_type, acc.security,
            1 if acc.enabled else 0,
            json.dumps(acc.folder_include, ensure_ascii=False),
            json.dumps(acc.folder_exclude, ensure_ascii=False),
            acc.oauth_client_id, acc.oauth_token_url, acc.notes, acc.retention_days, utcnow_iso(),
        ]
        if update_password:
            fields.append("password_enc=?")
            params.append(self.secret.encrypt(acc.password) if acc.password else "")
        if update_oauth_secret:
            fields.append("oauth_client_secret_enc=?")
            params.append(self.secret.encrypt(acc.oauth_client_secret) if acc.oauth_client_secret else "")
        if update_oauth_token:
            fields.append("oauth_refresh_token_enc=?")
            params.append(self.secret.encrypt(acc.oauth_refresh_token) if acc.oauth_refresh_token else "")
        params.append(acc.id)
        self.execute(f"UPDATE accounts SET {', '.join(fields)} WHERE id=?", tuple(params))

    def get_account(self, account_id: int) -> Optional[models.Account]:
        row = self.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
        return self._account_from_row(row) if row else None

    def account_names(self) -> Dict[int, str]:
        """Имена ящиков по id — дёшево, без расшифровки секретов."""
        return {int(r["id"]): r["name"] for r in self.query("SELECT id, name FROM accounts")}

    def get_account_by_username(self, username: str) -> Optional[models.Account]:
        row = self.query_one("SELECT * FROM accounts WHERE username=? COLLATE NOCASE ORDER BY id LIMIT 1", (username,))
        return self._account_from_row(row) if row else None

    def list_accounts(self, only_enabled: bool = False) -> List[models.Account]:
        sql = "SELECT * FROM accounts"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY name COLLATE NOCASE"
        return [self._account_from_row(r) for r in self.query(sql)]

    def set_account_enabled(self, account_id: int, enabled: bool) -> None:
        self.execute("UPDATE accounts SET enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, utcnow_iso(), account_id))

    def set_account_retention(self, account_id: int, days: int) -> None:
        self.execute("UPDATE accounts SET retention_days=?, updated_at=? WHERE id=?", (days, utcnow_iso(), account_id))

    def set_account_oauth_refresh(self, account_id: int, refresh_token: str) -> None:
        """Сохранить новый refresh-токен OAuth2, выданный сервером при обновлении."""
        self.execute("UPDATE accounts SET oauth_refresh_token_enc=?, updated_at=? WHERE id=?",
                     (self.secret.encrypt(refresh_token) if refresh_token else "", utcnow_iso(),
                      account_id))

    def delete_account(self, account_id: int) -> None:
        self.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    # ======================================================================
    #  Сотрудники
    # ======================================================================
    #: Поля карточки сотрудника, которые можно изменять из интерфейса и при
    #: синхронизации. Белый список нужен, чтобы update_employee нельзя было
    #: заставить переписать служебные колонки (id, created_at и т.п.).
    EMPLOYEE_FIELDS = ("external_id", "full_name", "email", "position", "department",
                       "phone", "status", "account_id", "notes", "source", "last_seen_at", "dismissed_at")

    @staticmethod
    def _employee_filter(query: Optional[str], status: Optional[str]) -> tuple:
        """Собрать условие WHERE для списка и счётчика сотрудников."""
        conds: List[str] = []
        params: List[Any] = []
        if status:
            conds.append("e.status=?")
            params.append(status)
        text = (query or "").strip()
        if text:
            # % и _ внутри запроса — обычные символы, а не шаблон LIKE,
            # иначе поиск «100%» выдавал бы всё подряд.
            escaped = text.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            cols = ("full_name", "email", "position", "department", "phone", "external_id")
            conds.append("(" + " OR ".join(f"ma_lower(e.{c}) LIKE ? ESCAPE '\\'" for c in cols) + ")")
            params.extend([like] * len(cols))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return where, tuple(params)

    def create_employee(self, *, full_name: str, email: str = "", external_id: str = "",
                        position: str = "", department: str = "", phone: str = "",
                        status: str = "active", account_id: Optional[int] = None,
                        notes: str = "", source: str = "manual",
                        last_seen_at: Optional[str] = None) -> int:
        now = utcnow_iso()
        cur = self.execute(
            """INSERT INTO employees(external_id, full_name, email, position, department, phone,
                                     status, account_id, notes, source, created_at, updated_at, last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (external_id, full_name, email, position, department, phone,
             status or "active", account_id, notes, source, now, now, last_seen_at),
        )
        return int(cur.lastrowid)

    def update_employee(self, employee_id: int, **fields: Any) -> None:
        """Обновить карточку сотрудника. Изменяются ТОЛЬКО переданные поля."""
        sets: List[str] = []
        params: List[Any] = []
        for key, value in fields.items():
            if key not in self.EMPLOYEE_FIELDS:
                continue
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at=?")
        params.append(utcnow_iso())
        params.append(employee_id)
        self.execute(f"UPDATE employees SET {', '.join(sets)} WHERE id=?", tuple(params))

    def get_employee(self, employee_id: int) -> Optional[sqlite3.Row]:
        return self.query_one(f"{_EMPLOYEE_SELECT} WHERE e.id=?", (employee_id,))

    def get_employee_by_email(self, email: str) -> Optional[sqlite3.Row]:
        if not email:
            return None
        return self.query_one(
            f"{_EMPLOYEE_SELECT} WHERE e.email=? COLLATE NOCASE ORDER BY e.id LIMIT 1", (email,))

    def get_employee_by_external_id(self, external_id: str) -> Optional[sqlite3.Row]:
        if not external_id:
            return None
        return self.query_one(
            f"{_EMPLOYEE_SELECT} WHERE e.external_id=? ORDER BY e.id LIMIT 1", (external_id,))

    def get_employee_by_full_name(self, full_name: str, *, only_unidentified: bool = False) -> Optional[sqlite3.Row]:
        """Найти сотрудника по ФИО (без учёта регистра).

        ``only_unidentified`` — искать только среди карточек БЕЗ табельного
        номера и БЕЗ почты. В таком виде поиском пользуется синхронизация:
        строку файла, где нет ни номера, ни адреса, опознать больше нечем, а
        без этого каждый прогон плодил бы копии одного и того же человека.
        """
        if not full_name:
            return None
        sql = f"{_EMPLOYEE_SELECT} WHERE ma_lower(e.full_name)=ma_lower(?)"
        if only_unidentified:
            sql += " AND COALESCE(e.external_id,'')='' AND COALESCE(e.email,'')=''"
        return self.query_one(sql + " ORDER BY e.id LIMIT 1", (full_name,))

    def list_employees(self, query: Optional[str] = None, status: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> List[sqlite3.Row]:
        where, params = self._employee_filter(query, status)
        return self.query(
            f"{_EMPLOYEE_SELECT}{where} ORDER BY e.full_name COLLATE NOCASE, e.id LIMIT ? OFFSET ?",
            params + (limit, offset),
        )

    def count_employees(self, status: Optional[str] = None, query: Optional[str] = None) -> int:
        where, params = self._employee_filter(query, status)
        return int(self.scalar(f"SELECT COUNT(*) FROM employees e{where}", params) or 0)

    def employee_counts(self) -> Dict[str, int]:
        """Сводка для шапки раздела: по статусам и — среди работающих — по наличию ящика.

        «С ящиком» и «Без ящика» считаются только по работающим: плитки говорят
        о том, чья почта копируется сейчас, а ящик уволенного уже выключен.
        """
        row = self.query_one(
            """SELECT
                   COALESCE(SUM(CASE WHEN status='active'   THEN 1 ELSE 0 END), 0) AS active,
                   COALESCE(SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END), 0) AS archived,
                   COALESCE(SUM(CASE WHEN status='active' AND account_id IS NOT NULL THEN 1 ELSE 0 END), 0)
                       AS with_account,
                   COALESCE(SUM(CASE WHEN status='active' AND account_id IS NULL THEN 1 ELSE 0 END), 0)
                       AS without_account
               FROM employees"""
        )
        return {
            "active": int(row["active"] or 0),
            "archived": int(row["archived"] or 0),
            "with_account": int(row["with_account"] or 0),
            "without_account": int(row["without_account"] or 0),
        }

    def delete_employee(self, employee_id: int) -> None:
        """Удалить карточку сотрудника. Почтовый ящик и локальные копии писем
        при этом НЕ трогаются — они живут своей жизнью (см. delete_account)."""
        self.execute("DELETE FROM employees WHERE id=?", (employee_id,))

    def set_employee_account(self, employee_id: int, account_id: Optional[int]) -> None:
        """Привязать сотрудника к ящику (None — отвязать).

        Привязка отмечает «ящик у сотрудника был»: если его потом удалят или
        отвяжут, синхронизация не заведёт ящик заново.
        """
        if account_id is None:
            self.execute("UPDATE employees SET account_id=NULL, updated_at=? WHERE id=?",
                         (utcnow_iso(), employee_id))
            return
        self.execute("UPDATE employees SET account_id=?, had_account=1, updated_at=? WHERE id=?",
                     (account_id, utcnow_iso(), employee_id))

    # ======================================================================
    #  Состояние папок и индекс сообщений
    # ======================================================================
    def upsert_folder_state(self, account_id: int, folder: str, uidvalidity: int, last_uid: int, msg_count: int) -> None:
        self.execute(
            """INSERT INTO folders(account_id, folder, uidvalidity, last_uid, msg_count, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(account_id, folder) DO UPDATE SET
                   uidvalidity=excluded.uidvalidity,
                   last_uid=excluded.last_uid,
                   msg_count=excluded.msg_count,
                   updated_at=excluded.updated_at""",
            (account_id, folder, uidvalidity, last_uid, msg_count, utcnow_iso()),
        )

    def get_folder_state(self, account_id: int, folder: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM folders WHERE account_id=? AND folder=?", (account_id, folder))

    def list_folder_states(self, account_id: int) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM folders WHERE account_id=? ORDER BY folder", (account_id,))

    # ---- папки, которые сервер не даёт открыть -------------------------
    #: Счётчик «сколько прогонов подряд папка не открывается» растёт не чаще раза
    #: в столько часов: повторы одного ночного задания (до 4 попыток при сбоях)
    #: — это один и тот же прогон, а не четыре дня подряд.
    FOLDER_PROBLEM_COUNT_HOURS = 20

    def record_folder_problem(self, account_id: int, folder: str, error: str = "") -> int:
        """Отметить, что папка снова не открылась, и вернуть число неудач ПОДРЯД.

        Счётчик нужен, чтобы отличать свежую поломку (о ней надо кричать) от
        папки, которая не открывается на сервере неделями: бесконечное «копия
        неполная» приучает не читать предупреждения, и настоящая пропажа писем
        теряется среди них. Считаются СУТКИ, а не попытки: иначе три сетевых
        сбоя за одну ночь превращали свежую поломку в «известную» к утру.
        """
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        threshold = (now_dt - timedelta(hours=self.FOLDER_PROBLEM_COUNT_HOURS)).isoformat()
        self.execute(
            """INSERT INTO folder_problems(account_id, folder, fails, first_failed, last_failed,
                                           last_error, counted_at)
               VALUES(?,?,1,?,?,?,?)
               ON CONFLICT(account_id, folder) DO UPDATE SET
                   fails=folder_problems.fails + (CASE WHEN folder_problems.counted_at IS NULL
                                                        OR folder_problems.counted_at < ? THEN 1 ELSE 0 END),
                   counted_at=(CASE WHEN folder_problems.counted_at IS NULL
                                     OR folder_problems.counted_at < ? THEN excluded.counted_at
                                    ELSE folder_problems.counted_at END),
                   last_failed=excluded.last_failed,
                   last_error=excluded.last_error""",
            (account_id, folder, now, now, (error or "")[:1000], now, threshold, threshold),
        )
        row = self.query_one("SELECT fails FROM folder_problems WHERE account_id=? AND folder=?",
                             (account_id, folder))
        return int(row["fails"]) if row else 1

    def clear_folder_problem(self, account_id: int, folder: str) -> None:
        """Папка снова открылась (или исчезла) — забыть её историю неудач."""
        self.execute("DELETE FROM folder_problems WHERE account_id=? AND folder=?",
                     (account_id, folder))

    def get_folder_problem(self, account_id: int, folder: str) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM folder_problems WHERE account_id=? AND folder=?",
                              (account_id, folder))

    def list_folder_problems(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        if account_id is None:
            return self.query("SELECT * FROM folder_problems ORDER BY account_id, folder")
        return self.query("SELECT * FROM folder_problems WHERE account_id=? ORDER BY folder",
                          (account_id,))

    def add_message_index(
        self, account_id: int, folder: str, uidvalidity: int, uid: int,
        message_id: str, size: int, internaldate: str, flags: str, stored_path: str, sha256: str,
        subject: str = "", from_addr: str = "", has_attach: int = 0,
    ) -> None:
        self.execute(
            """INSERT OR IGNORE INTO messages(
                    account_id, folder, uidvalidity, uid, message_id, size, internaldate,
                    flags, stored_path, sha256, subject, from_addr, has_attach, backed_up_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, folder, uidvalidity, uid, message_id, size, internaldate, flags,
             stored_path, sha256, subject[:500], from_addr[:300], 1 if has_attach else 0, utcnow_iso()),
        )

    def add_message_index_batch(self, rows: Sequence[tuple]) -> int:
        """Записать пачку писем в индекс ОДНОЙ транзакцией.

        По письму на транзакцию SQLite держит общий на процесс замок записи:
        ящик на 200 000 писем — это столько же отдельных коммитов, и воркеры
        упираются в базу, а не в сеть. Кортеж — в порядке колонок
        :meth:`add_message_index`.
        """
        if not rows:
            return 0
        now = utcnow_iso()
        # Нормализуем так же, как одиночная вставка: иначе через пачку в базу
        # попадали бы нерезаные subject/from_addr и has_attach=None, из-за
        # которого письмо выпадало бы из фильтра «с вложениями».
        payload = []
        for row in rows:
            row = list(row)
            row[10] = (row[10] or "")[:500]
            row[11] = (row[11] or "")[:300]
            row[12] = 1 if row[12] else 0
            payload.append((*row, now))
        conn = self.connect()
        with self._write_lock:
            try:
                cur = conn.executemany(
                    """INSERT OR IGNORE INTO messages(
                            account_id, folder, uidvalidity, uid, message_id, size, internaldate,
                            flags, stored_path, sha256, subject, from_addr, has_attach, backed_up_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    payload,
                )
                inserted = int(cur.rowcount or 0)
                conn.commit()
            except BaseException:
                # BaseException, а не Exception: при Ctrl+C или остановке службы
                # незакрытая транзакция держала бы writer-lock, и вся база
                # вставала бы с «database is locked».
                self._safe_rollback(conn)
                raise
        return inserted

    def set_message_headers(self, pk: int, subject: str, from_addr: str, has_attach: int) -> None:
        self.execute("UPDATE messages SET subject=?, from_addr=?, has_attach=? WHERE id=?",
                     (subject[:500], from_addr[:300], 1 if has_attach else 0, pk))

    def get_message(self, pk: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM messages WHERE id=?", (pk,))

    def account_folders(self, account_id: int) -> List[str]:
        rows = self.query("SELECT DISTINCT folder FROM messages WHERE account_id=? ORDER BY folder", (account_id,))
        return [r["folder"] for r in rows]

    def message_exists(self, account_id: int, folder: str, uidvalidity: int, uid: int) -> bool:
        return self.query_one(
            "SELECT 1 FROM messages WHERE account_id=? AND folder=? AND uidvalidity=? AND uid=?",
            (account_id, folder, uidvalidity, uid),
        ) is not None

    def existing_uids(self, account_id: int, folder: str, uidvalidity: int) -> set:
        """UID писем, которые копировать заново НЕ нужно.

        Это и письма из индекса, и письма, вычищенные по сроку хранения
        (``retired_uids``): без вторых ретеншн и бэкап работали бы друг против
        друга — удалённое ночью по сроку следующей ночью скачивалось бы снова.
        """
        rows = self.query(
            "SELECT uid FROM messages WHERE account_id=? AND folder=? AND uidvalidity=? "
            "UNION SELECT uid FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity=?",
            (account_id, folder, uidvalidity, account_id, folder, uidvalidity),
        )
        return {r["uid"] for r in rows}

    # ---- письма, вычищенные по сроку хранения ---------------------------
    def retire_message_indexes(self, ids: Sequence[int]) -> int:
        """Убрать письма из индекса, запомнив их UID как «вычищенные по сроку».

        Запись о UID остаётся, чтобы бэкап не скачал письмо снова, пока оно
        лежит на сервере. Всё остальное (списки, экспорт, счётчики, аналитика)
        вычищенных писем не видит — их просто нет в ``messages``.
        """
        ids = [int(i) for i in ids]
        done = 0
        now = utcnow_iso()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            with self.transaction() as conn:
                conn.execute(
                    f"INSERT OR IGNORE INTO retired_uids(account_id, folder, uidvalidity, uid, retired_at) "
                    f"SELECT account_id, folder, uidvalidity, uid, ? FROM messages WHERE id IN ({marks})",
                    (now, *chunk))
                cur = conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", tuple(chunk))
                done += int(cur.rowcount or 0)
        return done

    def prune_retired_uids(self, account_id: int, folder: str, uidvalidity: int, server_uids) -> int:
        """Забыть вычищенные UID, которых на сервере больше нет.

        В пределах одного UIDVALIDITY сервер номера не переиспользует, поэтому
        такое письмо уже никогда не появится — помнить его незачем.
        """
        rows = self.query("SELECT uid FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity=?",
                          (account_id, folder, uidvalidity))
        if not rows:
            return 0
        present = set(int(u) for u in server_uids)
        gone = [int(r["uid"]) for r in rows if int(r["uid"]) not in present]
        for start in range(0, len(gone), 500):
            chunk = gone[start:start + 500]
            marks = ",".join("?" * len(chunk))
            self.execute(f"DELETE FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity=? "
                         f"AND uid IN ({marks})", (account_id, folder, uidvalidity, *chunk))
        # UID прежних UIDVALIDITY этой папки тоже больше не нужны
        self.execute("DELETE FROM retired_uids WHERE account_id=? AND folder=? AND uidvalidity<>?",
                     (account_id, folder, uidvalidity))
        return len(gone)

    def count_retired(self, account_id: Optional[int] = None) -> int:
        if account_id is None:
            return int(self.scalar("SELECT COUNT(*) FROM retired_uids") or 0)
        return int(self.scalar("SELECT COUNT(*) FROM retired_uids WHERE account_id=?", (account_id,)) or 0)

    def clear_retired(self, account_id: int) -> int:
        """Забыть вычищенные письма ящика (например, срок хранения увеличили)."""
        cur = self.execute("DELETE FROM retired_uids WHERE account_id=?", (account_id,))
        return int(cur.rowcount or 0)

    def hash_exists(self, account_id: int, sha256: str) -> Optional[str]:
        row = self.query_one(
            "SELECT stored_path FROM messages WHERE account_id=? AND sha256=? LIMIT 1", (account_id, sha256)
        )
        return row["stored_path"] if row else None

    def count_messages(self, account_id: Optional[int] = None) -> int:
        if account_id is None:
            return int(self.scalar("SELECT COUNT(*) FROM messages") or 0)
        return int(self.scalar("SELECT COUNT(*) FROM messages WHERE account_id=?", (account_id,)) or 0)

    def count_folder_messages(self, account_id: int, folder: Optional[str] = None) -> int:
        if folder:
            return int(self.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND folder=?",
                                   (account_id, folder)) or 0)
        return self.count_messages(account_id)

    def sum_message_bytes(self, account_id: Optional[int] = None) -> int:
        if account_id is None:
            return int(self.scalar("SELECT COALESCE(SUM(size),0) FROM messages") or 0)
        return int(self.scalar("SELECT COALESCE(SUM(size),0) FROM messages WHERE account_id=?", (account_id,)) or 0)

    def list_messages(self, account_id: int, folder: Optional[str] = None, limit: int = 500, offset: int = 0) -> List[sqlite3.Row]:
        # Сортировка с добавочным «id»: без него у писем с одинаковым (или
        # пустым) internaldate порядок между запросами не гарантирован, и
        # постраничный обход мог пропустить часть писем или выдать их дважды.
        if folder:
            return self.query(
                "SELECT * FROM messages WHERE account_id=? AND folder=? "
                "ORDER BY internaldate DESC, id DESC LIMIT ? OFFSET ?",
                (account_id, folder, limit, offset),
            )
        return self.query(
            "SELECT * FROM messages WHERE account_id=? ORDER BY internaldate DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (account_id, limit, offset),
        )

    def messages_after_id(self, account_id: int, after_id: int = 0, folder: Optional[str] = None,
                          limit: int = 5000) -> List[sqlite3.Row]:
        """Страница индекса по первичному ключу — устойчивый обход.

        В отличие от :meth:`list_messages` (сортировка по дате + OFFSET) этот
        обход не сбивается, когда во время прохода в индекс добавляются или
        из него удаляются письма.
        """
        if folder:
            return self.query(
                "SELECT * FROM messages WHERE account_id=? AND folder=? AND id > ? "
                "ORDER BY id LIMIT ?", (account_id, folder, after_id, limit))
        return self.query(
            "SELECT * FROM messages WHERE account_id=? AND id > ? ORDER BY id LIMIT ?",
            (account_id, after_id, limit))

    @staticmethod
    def _export_filter(account_id: int, folder: Optional[str], since: Optional[str],
                       until: Optional[str]):
        conds, params = ["account_id=?"], [account_id]
        if folder:
            conds.append("folder=?")
            params.append(folder)
        if since or until:
            # письма без даты в период не попадают: неизвестно, к какому он дню
            conds.append("internaldate IS NOT NULL AND internaldate<>''")
        if since:
            conds.append("internaldate>=?")
            params.append(since)
        if until:
            conds.append("internaldate<?")
            params.append(until)
        return " AND ".join(conds), params

    def export_rows(self, account_id: int, folder: Optional[str], since: Optional[str],
                    until: Optional[str], after_id: int, limit: int) -> List[sqlite3.Row]:
        where, params = self._export_filter(account_id, folder, since, until)
        return self.query(f"SELECT * FROM messages WHERE {where} AND id>? ORDER BY id LIMIT ?",
                          (*params, after_id, limit))

    def count_export_rows(self, account_id: int, folder: Optional[str], since: Optional[str],
                          until: Optional[str]) -> int:
        where, params = self._export_filter(account_id, folder, since, until)
        return int(self.scalar(f"SELECT COUNT(*) FROM messages WHERE {where}", tuple(params)) or 0)

    def delete_message_index(self, message_id_pk: int) -> None:
        self.execute("DELETE FROM messages WHERE id=?", (message_id_pk,))

    def folder_stored_paths(self, account_id: int, folder: str) -> List[str]:
        rows = self.query("SELECT stored_path FROM messages WHERE account_id=? AND folder=?", (account_id, folder))
        return [r["stored_path"] for r in rows if r["stored_path"]]

    def purge_folder_index(self, account_id: int, folder: str) -> None:
        """Удалить из индекса все записи папки (при смене UIDVALIDITY)."""
        self.execute("DELETE FROM messages WHERE account_id=? AND folder=?", (account_id, folder))

    # ---- пересоздание копии ящика «с нуля» ------------------------------
    def iter_message_paths(self, account_id: int, batch: int = 5000):
        """Пройти индекс ящика порциями: ``(id, папка, путь к файлу)``.

        Порциями — потому что у большого ящика сотни тысяч записей, и читать их
        одним списком в память при проверке файлов на диске не стоит.
        """
        # Обход по id (а не OFFSET): на сотнях тысяч писем OFFSET квадратичен
        # и сбивается, если во время прохода записи удаляются.
        last_id = 0
        while True:
            rows = self.query(
                "SELECT id, folder, stored_path FROM messages WHERE account_id=? AND id>? "
                "ORDER BY id LIMIT ?",
                (account_id, last_id, batch),
            )
            if not rows:
                return
            for row in rows:
                yield int(row["id"]), row["folder"], (row["stored_path"] or "")
            last_id = int(rows[-1]["id"])

    def delete_message_indexes(self, ids: Sequence[int]) -> int:
        """Удалить записи индекса по списку id. Возвращает число удалённых."""
        removed = 0
        ids = list(ids)
        for start in range(0, len(ids), 500):       # SQLite не любит огромные IN
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            self.execute(f"DELETE FROM messages WHERE id IN ({marks})", tuple(chunk))
            removed += len(chunk)
        return removed

    def purge_account_index(self, account_id: int) -> int:
        """Стереть весь индекс писем ящика и состояние его папок.

        Нужно для копии «с нуля»: пока запись о письме есть в индексе, сервис
        считает письмо уже скачанным и заново его не запрашивает.
        """
        count = self.count_messages(account_id)
        self.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
        self.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
        self.execute("DELETE FROM folder_problems WHERE account_id=?", (account_id,))
        return int(count)

    def reset_folder_states(self, account_id: int) -> None:
        """Забыть состояние папок ящика (UIDVALIDITY и последний UID)."""
        self.execute("DELETE FROM folders WHERE account_id=?", (account_id,))

    def message_totals_by_account(self) -> Dict[int, Dict[str, int]]:
        """Число писем и суммарный размер по всем ящикам ОДНИМ запросом.

        Дашборд обновляется постоянно и у каждого открытого окна: на 500 ящиках
        отдельные count/sum на ящик давали полторы тысячи запросов к той же
        базе, в которую в это время пишет копирование.
        """
        rows = self.query("SELECT account_id, COUNT(*) AS cnt, COALESCE(SUM(size),0) AS bytes "
                          "FROM messages GROUP BY account_id")
        return {int(r["account_id"]): {"messages": int(r["cnt"]), "bytes": int(r["bytes"])}
                for r in rows}

    def last_runs_by_account(self) -> Dict[int, sqlite3.Row]:
        """Последний прогон каждого ящика одним запросом."""
        rows = self.query(
            "SELECT r.* FROM runs r JOIN (SELECT account_id, MAX(id) AS last_id FROM runs "
            "GROUP BY account_id) m ON m.last_id = r.id")
        return {int(r["account_id"]): r for r in rows}

    def folders_summary(self, account_id: int) -> List[sqlite3.Row]:
        return self.query(
            """SELECT folder, COUNT(*) AS cnt, COALESCE(SUM(size),0) AS bytes
               FROM messages WHERE account_id=? GROUP BY folder ORDER BY folder""",
            (account_id,),
        )

    # ======================================================================
    #  Очередь заданий
    # ======================================================================
    def enqueue_job(self, job_type: str, account_id: Optional[int], params: Dict[str, Any], priority: int = 5,
                    max_attempts: int = 1, created_by: str = "") -> int:
        cur = self.execute(
            """INSERT INTO jobs(type, account_id, status, priority, params, created_at,
                                attempts, max_attempts, created_by, progress_current, progress_total)
               VALUES(?,?,?,?,?,?,?,?,?,0,0)""",
            (job_type, account_id, models.JobStatus.QUEUED, priority,
             json.dumps(params, ensure_ascii=False), utcnow_iso(), 0, max_attempts, created_by),
        )
        return int(cur.lastrowid)

    def claim_next_job(self, worker_id: str) -> Optional[sqlite3.Row]:
        """Атомарно взять следующее задание из очереди (по приоритету и времени)."""
        with self._write_lock:
            conn = self.connect()
            try:
                row = conn.execute(
                    """SELECT * FROM jobs WHERE status=? ORDER BY priority ASC, id ASC LIMIT 1""",
                    (models.JobStatus.QUEUED,),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE jobs SET status=?, started_at=?, worker_id=?, attempts=attempts+1 WHERE id=?",
                    (models.JobStatus.RUNNING, utcnow_iso(), worker_id, row["id"]),
                )
                conn.commit()
                return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            except BaseException:
                self._safe_rollback(conn)
                raise

    def claim_next_job_filtered(self, worker_id: str, skip_accounts=(), *, busy_accounts=(),
                                local_types=(), skip_types=()) -> Optional[sqlite3.Row]:
        """Захватить следующее задание очереди.

        * ``skip_accounts`` — ящики, по которым не брать НИЧЕГО;
        * ``busy_accounts`` + ``local_types`` — по этим ящикам уже идёт работа с
          локальной копией: второе такое задание (бэкап, очистка, перешифровка,
          экспорт…) не берём — иначе они работали бы с одними файлами наперегонки;
        * ``skip_types`` — типы, которые сейчас брать нельзя (одиночные задания);
        * задания с отложенным повтором (run_after в будущем) ждут своего времени.
        """
        conds = ["status=?", "(run_after IS NULL OR run_after<=?)"]
        params: List[Any] = [models.JobStatus.QUEUED, utcnow_iso()]
        skip_accounts = list(skip_accounts or [])
        if skip_accounts:
            conds.append(f"(account_id IS NULL OR account_id NOT IN ({','.join('?' * len(skip_accounts))}))")
            params += skip_accounts
        busy_accounts = list(busy_accounts or [])
        local_types = list(local_types or [])
        if busy_accounts and local_types:
            conds.append(f"NOT (account_id IN ({','.join('?' * len(busy_accounts))}) "
                         f"AND type IN ({','.join('?' * len(local_types))}))")
            params += busy_accounts + local_types
        skip_types = list(skip_types or [])
        if skip_types:
            conds.append(f"type NOT IN ({','.join('?' * len(skip_types))})")
            params += skip_types
        sql = f"SELECT * FROM jobs WHERE {' AND '.join(conds)} ORDER BY priority ASC, id ASC LIMIT 1"
        with self._write_lock:
            conn = self.connect()
            try:
                row = conn.execute(sql, tuple(params)).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE jobs SET status=?, started_at=?, worker_id=?, attempts=attempts+1, run_after=NULL "
                    "WHERE id=?",
                    (models.JobStatus.RUNNING, utcnow_iso(), worker_id, row["id"]),
                )
                conn.commit()
                return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            except BaseException:
                self._safe_rollback(conn)
                raise

    def get_job(self, job_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def list_jobs(self, status: Optional[str] = None, job_type: Optional[str] = None,
                  account_id: Optional[int] = None, limit: int = 100, offset: int = 0) -> List[sqlite3.Row]:
        conds, params = [], []
        if status:
            conds.append("status=?"); params.append(status)
        if job_type:
            conds.append("type=?"); params.append(job_type)
        if account_id is not None:
            conds.append("account_id=?"); params.append(account_id)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        params += [limit, offset]
        return self.query(f"SELECT * FROM jobs{where} ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params))

    def active_jobs(self) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM jobs WHERE status IN (?,?) ORDER BY status DESC, priority ASC, id ASC",
            (models.JobStatus.RUNNING, models.JobStatus.QUEUED),
        )

    def count_waiting_jobs(self, exclude_types: Sequence[str] = ()) -> int:
        """Сколько заданий ждут свободного воркера прямо сейчас (без отложенных повторов)."""
        sql = "SELECT COUNT(*) FROM jobs WHERE status=? AND (run_after IS NULL OR run_after<=?)"
        params: List[Any] = [models.JobStatus.QUEUED, utcnow_iso()]
        exclude_types = list(exclude_types or [])
        if exclude_types:
            sql += f" AND type NOT IN ({','.join('?' * len(exclude_types))})"
            params += exclude_types
        return int(self.scalar(sql, tuple(params)) or 0)

    def count_jobs_by_status(self) -> Dict[str, int]:
        rows = self.query("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def update_job_progress(self, job_id: int, current: int, total: int, message: str = "",
                            bytes_done: int = 0, speed: float = 0.0) -> None:
        self.execute(
            """UPDATE jobs SET progress_current=?, progress_total=?, progress_message=?,
                              bytes_done=?, speed=?, updated_at=? WHERE id=?""",
            (current, total, message[:500], bytes_done, speed, utcnow_iso(), job_id),
        )

    def finish_job(self, job_id: int, status: str, result: Optional[Dict[str, Any]] = None, error: str = "") -> None:
        self.execute(
            "UPDATE jobs SET status=?, finished_at=?, result=?, error=?, updated_at=? WHERE id=?",
            (status, utcnow_iso(), json.dumps(result or {}, ensure_ascii=False), error[:2000], utcnow_iso(), job_id),
        )

    def requeue_job(self, job_id: int, *, run_after: Optional[str] = None, refund_attempt: bool = False,
                    reset_attempts: bool = False, clear_cancel: bool = False) -> None:
        """Вернуть задание в очередь.

        * ``run_after`` — не раньше этого времени (отложенный повтор хранится в
          БД: пока задание ждёт, оно в статусе «в очереди», и кнопка «Отмена»
          на нём работает — раньше таймер повтора отмену не замечал);
        * ``refund_attempt`` — попытка не засчитывается (прервано перезапуском);
        * ``reset_attempts`` — ручной «Повторить»: снова все попытки;
        * ``clear_cancel`` — снять флаг отмены (только по явному действию
          пользователя: автоматический повтор флаг, выставленный пользователем,
          больше не стирает).
        """
        sets = ["status=?", "worker_id=NULL", "started_at=NULL", "run_after=?"]
        params: List[Any] = [models.JobStatus.QUEUED, run_after]
        if refund_attempt:
            sets.append("attempts=MAX(0, attempts-1)")
        if reset_attempts:
            sets.append("attempts=0")
            sets.append("error=''")
        if clear_cancel:
            sets.append("cancel_requested=0")
        self.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*params, job_id))

    def request_cancel(self, job_id: int) -> None:
        self.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))

    def is_cancel_requested(self, job_id: int) -> bool:
        row = self.query_one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        return bool(row and row["cancel_requested"])

    #: Сколько раз подряд задание может прерываться перезапуском службы: если
    #: больше — вероятно, оно само и роняет службу (например, нехваткой памяти).
    MAX_JOB_RESTARTS = 3

    def reset_orphan_jobs(self) -> int:
        """При старте: задания в статусе RUNNING прерваны перезапуском — вернуть в очередь.

        Попытка при этом не засчитывается: задание не провалилось, его прервали.
        Раньше задание с одной попыткой (экспорт, очистка) после обновления
        службы объявлялось проваленным.
        """
        rows = self.query("SELECT id, restarts, cancel_requested FROM jobs WHERE status=?",
                          (models.JobStatus.RUNNING,))
        count = 0
        for r in rows:
            restarts = int(r["restarts"] or 0) + 1
            if r["cancel_requested"]:
                self.finish_job(r["id"], models.JobStatus.CANCELLED, error="Отменено пользователем")
            elif restarts > self.MAX_JOB_RESTARTS:
                self.finish_job(r["id"], models.JobStatus.FAILED,
                                error=f"Задание прерывалось перезапуском службы {restarts} раз подряд — "
                                      f"возможно, оно само приводит к сбою службы (например, нехватка памяти).")
            else:
                self.execute("UPDATE jobs SET restarts=? WHERE id=?", (restarts, r["id"]))
                self.requeue_job(r["id"], refund_attempt=True)
                self.add_job_event(r["id"], "WARNING", "Задание было прервано перезапуском службы и "
                                                       "продолжится (попытка не засчитана).")
            count += 1
        return count

    def reset_orphan_runs(self) -> int:
        """При старте: закрыть записи прогонов, оставшиеся в статусе «выполняется».

        Прогон закрывается вместе с заданием, но при жёстком перезапуске (или
        после ошибки в старых версиях) запись оставалась открытой навсегда — и
        карточка ящика вечно показывала «копирование идёт».
        """
        cur = self.execute(
            "UPDATE runs SET status=?, finished_at=?, detail=? WHERE status=?",
            (models.JobStatus.FAILED, utcnow_iso(), "Прервано при перезапуске сервиса",
             models.JobStatus.RUNNING),
        )
        return int(cur.rowcount or 0)

    #: Предел длины одного события задания. Обрезка нужна, чтобы одна запись не
    #: раздула базу, но она должна быть ВИДНА: раньше хвост сообщения молча
    #: пропадал, а вместе с ним — самая полезная часть подсказки.
    JOB_EVENT_MAX_CHARS = 2000

    def add_job_event(self, job_id: int, level: str, message: str) -> None:
        text = message or ""
        if len(text) > self.JOB_EVENT_MAX_CHARS:
            text = text[:self.JOB_EVENT_MAX_CHARS - 24].rstrip() + "… (сообщение обрезано)"
        self.execute(
            "INSERT INTO job_events(job_id, ts, level, message) VALUES(?,?,?,?)",
            (job_id, utcnow_iso(), level, text),
        )

    def list_job_events(self, job_id: int, limit: int = 500) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        )

    # ---- служебная чистка (см. mailarchiver/maintenance.py) ---------------
    def purge_jobs_by_age(self, ok_before_iso: str, failed_before_iso: str, keep_max: int = 100000) -> int:
        """Удалить завершённые задания по возрасту: удачные — раньше, с ошибками — позже.

        Раньше хранились последние 300 заданий: за ночь на 580 ящиков ошибки
        первой половины ночи вместе с журналами исчезали до утра.
        """
        ids = [r["id"] for r in self.query(
            "SELECT id FROM jobs WHERE status IN (?,?) AND COALESCE(finished_at, created_at) < ? "
            "UNION SELECT id FROM jobs WHERE status IN (?,?) AND COALESCE(finished_at, created_at) < ?",
            (models.JobStatus.SUCCESS, models.JobStatus.CANCELLED, ok_before_iso,
             models.JobStatus.FAILED, models.JobStatus.PARTIAL, failed_before_iso))]
        ids += [r["id"] for r in self.query(
            "SELECT id FROM jobs WHERE status NOT IN (?,?) ORDER BY id DESC LIMIT -1 OFFSET ?",
            (models.JobStatus.QUEUED, models.JobStatus.RUNNING, int(keep_max)))]
        return self._delete_jobs(sorted(set(ids)))

    def _delete_jobs(self, ids: Sequence[int]) -> int:
        removed = 0
        for batch in chunked(list(ids), 500):
            ph = ",".join("?" * len(batch))
            self.execute(f"DELETE FROM job_events WHERE job_id IN ({ph})", tuple(batch))
            cur = self.execute(f"DELETE FROM jobs WHERE id IN ({ph})", tuple(batch))
            removed += int(cur.rowcount or 0)
        return removed

    def purge_runs(self, keep_per_account: int) -> int:
        """История прогонов: последние N на ящик и ничего — от удалённых ящиков."""
        cur = self.execute(
            "DELETE FROM runs WHERE account_id NOT IN (SELECT id FROM accounts) OR id IN ("
            " SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY id DESC) AS rn"
            " FROM runs) WHERE rn > ?)", (max(1, int(keep_per_account)),))
        return int(cur.rowcount or 0)

    def purge_older(self, table: str, column: str, before: str) -> int:
        if table not in ("audit", "stats_daily", "restores", "login_attempts"):
            raise ValueError(table)
        cur = self.execute(f"DELETE FROM {table} WHERE {column} < ?", (before,))
        return int(cur.rowcount or 0)

    def exports_older_than(self, before_iso: str) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM exports WHERE created_at < ? AND status <> 'pending'", (before_iso,))

    def fail_interrupted_artifacts(self) -> int:
        """При старте: выгрузки и восстановления, оставшиеся «в работе», — прерваны."""
        n = 0
        for table in ("exports", "restores"):
            cur = self.execute(f"UPDATE {table} SET status=?, error=? WHERE status='pending'",
                               (models.JobStatus.FAILED, "Прервано перезапуском службы"))
            n += int(cur.rowcount or 0)
        return n

    def purge_old_jobs(self, keep: int) -> int:
        """Оставить последние keep завершённых заданий, остальные удалить."""
        rows = self.query(
            "SELECT id FROM jobs WHERE status NOT IN (?,?) ORDER BY id DESC LIMIT -1 OFFSET ?",
            (models.JobStatus.QUEUED, models.JobStatus.RUNNING, keep),
        )
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        removed = 0
        # Два массовых DELETE вместо пары запросов на каждое задание: быстрее и
        # не оставляет «висячих» событий. Режем на части, чтобы не упереться в
        # ограничение SQLite на число параметров запроса.
        for batch in chunked(ids, 500):
            ph = ",".join("?" * len(batch))
            self.execute(f"DELETE FROM job_events WHERE job_id IN ({ph})", tuple(batch))
            cur = self.execute(f"DELETE FROM jobs WHERE id IN ({ph})", tuple(batch))
            removed += int(cur.rowcount) if cur.rowcount and cur.rowcount > 0 else len(batch)
        return removed

    # ======================================================================
    #  История прогонов (runs)
    # ======================================================================
    def start_run(self, account_id: int, run_type: str, job_id: Optional[int]) -> int:
        """Открыть запись прогона. Повторная попытка того же задания продолжает ЕГО
        запись, а не заводит новую: одно ночное копирование с тремя повторами —
        это один прогон, а не четыре."""
        with self.transaction() as conn:
            if job_id is not None:
                row = conn.execute("SELECT id FROM runs WHERE job_id=? AND type=? AND account_id=? "
                                   "ORDER BY id DESC LIMIT 1", (job_id, run_type, account_id)).fetchone()
                if row is not None:
                    conn.execute("UPDATE runs SET status=?, finished_at=NULL WHERE id=?",
                                 (models.JobStatus.RUNNING, row["id"]))
                    return int(row["id"])
            cur = conn.execute(
                "INSERT INTO runs(account_id, type, job_id, status, started_at) VALUES(?,?,?,?,?)",
                (account_id, run_type, job_id, models.JobStatus.RUNNING, utcnow_iso()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, *, messages_new: int = 0, bytes_new: int = 0,
                   messages_total: int = 0, errors: int = 0, detail: str = "") -> None:
        self.execute(
            """UPDATE runs SET status=?, finished_at=?, messages_new=?, bytes_new=?,
                              messages_total=?, errors=?, detail=? WHERE id=?""",
            (status, utcnow_iso(), messages_new, bytes_new, messages_total, errors, detail[:1000], run_id),
        )

    def list_runs(self, account_id: Optional[int] = None, limit: int = 50) -> List[sqlite3.Row]:
        if account_id is not None:
            return self.query("SELECT * FROM runs WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit))
        return self.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def purge_old_runs(self, account_id: int, keep: int) -> None:
        self.execute(
            "DELETE FROM runs WHERE account_id=? AND id NOT IN (SELECT id FROM runs WHERE account_id=? ORDER BY id DESC LIMIT ?)",
            (account_id, account_id, keep),
        )

    # ======================================================================
    #  Расписания
    # ======================================================================
    def create_schedule(self, account_id: int, kind: str, job_type: str, cron_expr: str = "",
                        interval_seconds: int = 0, enabled: bool = True, options: Optional[Dict] = None) -> int:
        cur = self.execute(
            """INSERT INTO schedules(account_id, kind, job_type, cron_expr, interval_seconds, enabled, options, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (account_id, kind, job_type, cron_expr, interval_seconds, 1 if enabled else 0,
             json.dumps(options or {}, ensure_ascii=False), utcnow_iso()),
        )
        return int(cur.lastrowid)

    def update_schedule(self, schedule_id: int, **fields) -> None:
        if not fields:
            return
        cols, params = [], []
        for k, v in fields.items():
            if k == "options":
                v = json.dumps(v or {}, ensure_ascii=False)
            if k == "enabled":
                v = 1 if v else 0
            cols.append(f"{k}=?"); params.append(v)
        params.append(schedule_id)
        self.execute(f"UPDATE schedules SET {', '.join(cols)} WHERE id=?", tuple(params))

    def get_schedule(self, schedule_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM schedules WHERE id=?", (schedule_id,))

    def list_schedules(self, account_id: Optional[int] = None, only_enabled: bool = False) -> List[sqlite3.Row]:
        conds, params = [], []
        if account_id is not None:
            conds.append("account_id=?"); params.append(account_id)
        if only_enabled:
            conds.append("enabled=1")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return self.query(f"SELECT * FROM schedules{where} ORDER BY id", tuple(params))

    def delete_schedule(self, schedule_id: int) -> None:
        self.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    def set_schedule_runtimes(self, schedule_id: int, last_run: Optional[str] = None, next_run: Optional[str] = None) -> None:
        if last_run is not None:
            self.execute("UPDATE schedules SET last_run=? WHERE id=?", (last_run, schedule_id))
        if next_run is not None:
            self.execute("UPDATE schedules SET next_run=? WHERE id=?", (next_run, schedule_id))

    # ======================================================================
    #  Экспорты и восстановления (артефакты)
    # ======================================================================
    def create_export(self, account_id: int, engine: str, fmt: str, path: str, params: Dict,
                      job_id: Optional[int], created_by: str = "") -> int:
        cur = self.execute(
            "INSERT INTO exports(account_id, engine, format, path, params, job_id, status, created_at, created_by) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (account_id, engine, fmt, path, json.dumps(params, ensure_ascii=False), job_id, "pending",
             utcnow_iso(), created_by or ""),
        )
        return int(cur.lastrowid)

    def update_export(self, export_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE exports SET {cols} WHERE id=?", (*fields.values(), export_id))

    def get_export(self, export_id: int) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM exports WHERE id=?", (export_id,))

    def list_exports(self, account_id: Optional[int] = None, limit: int = 100) -> List[sqlite3.Row]:
        if account_id is not None:
            return self.query("SELECT * FROM exports WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit))
        return self.query("SELECT * FROM exports ORDER BY id DESC LIMIT ?", (limit,))

    def delete_export(self, export_id: int) -> None:
        self.execute("DELETE FROM exports WHERE id=?", (export_id,))

    def create_restore(self, account_id: int, params: Dict, job_id: Optional[int]) -> int:
        cur = self.execute(
            "INSERT INTO restores(account_id, params, job_id, status, created_at) VALUES(?,?,?,?,?)",
            (account_id, json.dumps(params, ensure_ascii=False), job_id, "pending", utcnow_iso()),
        )
        return int(cur.lastrowid)

    def update_restore(self, restore_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE restores SET {cols} WHERE id=?", (*fields.values(), restore_id))

    def list_restores(self, account_id: Optional[int] = None, limit: int = 100) -> List[sqlite3.Row]:
        if account_id is not None:
            return self.query("SELECT * FROM restores WHERE account_id=? ORDER BY id DESC LIMIT ?", (account_id, limit))
        return self.query("SELECT * FROM restores ORDER BY id DESC LIMIT ?", (limit,))

    # ======================================================================
    #  Настройки, статистика, аудит
    # ======================================================================
    # ---- служебные отметки (таблица meta) ----------------------------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ---- копия вне сервера: что уже отправлено ------------------------------
    def replica_clear(self) -> None:
        self.execute("DELETE FROM replica_files")

    def replica_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM replica_files") or 0)

    def replica_count_prefix(self, prefix: str) -> int:
        # Диапазон по первичному ключу вместо LIKE: LIKE не использует индекс
        # и спотыкается о «_» и «%» в путях.
        return int(self.scalar("SELECT COUNT(*) FROM replica_files WHERE path >= ? AND path < ?",
                               (prefix, prefix + "\U0010ffff")) or 0)

    def replica_groups(self) -> List[str]:
        return [r[0] for r in self.query("SELECT DISTINCT grp FROM replica_files")]

    def replica_state_group(self, grp: str) -> Dict[str, Tuple[int, int]]:
        return {r["path"]: (int(r["size"]), int(r["mtime_ns"]))
                for r in self.query("SELECT path, size, mtime_ns FROM replica_files WHERE grp=?", (grp,))}

    def replica_upsert(self, rows) -> None:
        """rows: (путь, группа, размер, mtime_ns)."""
        now = utcnow_iso()
        self.executemany(
            "INSERT INTO replica_files(path, grp, size, mtime_ns, done_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET grp=excluded.grp, size=excluded.size, "
            "mtime_ns=excluded.mtime_ns, done_at=excluded.done_at",
            [(p, g, int(sz), int(mt), now) for p, g, sz, mt in rows])

    def replica_replace_group(self, grp: str, rows) -> None:
        """Заменить сведения о группе целиком (после полной сверки с копией)."""
        now = utcnow_iso()
        with self.transaction() as conn:
            conn.execute("DELETE FROM replica_files WHERE grp=?", (grp,))
            conn.executemany("INSERT OR REPLACE INTO replica_files(path, grp, size, mtime_ns, done_at) "
                             "VALUES(?,?,?,?,?)",
                             [(p, g, int(sz), int(mt), now) for p, g, sz, mt in rows])

    def replica_delete(self, paths) -> None:
        paths = list(paths)
        for start in range(0, len(paths), 500):
            chunk = paths[start:start + 500]
            self.execute(f"DELETE FROM replica_files WHERE path IN ({','.join('?' * len(chunk))})",
                         tuple(chunk))

    def count_encrypted_messages(self) -> Dict[str, int]:
        """Сколько писем в индексе хранится зашифрованными и открытыми."""
        row = self.query_one(
            "SELECT SUM(CASE WHEN stored_path LIKE '%.enc' THEN 1 ELSE 0 END) AS enc, COUNT(*) AS total "
            "FROM messages")
        enc = int((row["enc"] if row else 0) or 0)
        total = int((row["total"] if row else 0) or 0)
        return {"encrypted": enc, "plain": total - enc, "total": total}

    def set_message_path(self, pk: int, stored_path: str) -> None:
        self.execute("UPDATE messages SET stored_path=? WHERE id=?", (stored_path, pk))

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            value = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            value = row["value"]
        if key in _ENCRYPTED_SETTINGS and isinstance(value, str) and value:
            try:
                return self.secret.decrypt(value)
            except Exception:  # noqa: BLE001
                # Обратная совместимость: значение сохранено прошлой версией
                # открытым текстом (или ключ шифрования сменился) — отдаём как есть.
                return value
        return value

    def set_setting(self, key: str, value: Any) -> None:
        stored: Any = value
        if key in _ENCRYPTED_SETTINGS and isinstance(value, str) and value:
            # Секрет не должен лежать в БД открытым текстом (см. _ENCRYPTED_SETTINGS).
            stored = self.secret.encrypt(value)
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(stored, ensure_ascii=False)),
        )

    def _encode_setting(self, key: str, value: Any) -> str:
        stored: Any = value
        if key in _ENCRYPTED_SETTINGS and isinstance(value, str) and value:
            stored = self.secret.encrypt(value)
        return json.dumps(stored, ensure_ascii=False)

    def set_settings_many(self, values: Dict[str, Any]) -> None:
        """Записать несколько настроек ОДНОЙ транзакцией: всё или ничего."""
        with self.transaction() as conn:
            for key, value in values.items():
                conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, self._encode_setting(key, value)))

    def restore_settings(self, snapshot: Dict[str, Any]) -> None:
        """Вернуть настройки к снимку: значение None — «переопределения не было»."""
        with self.transaction() as conn:
            for key, value in snapshot.items():
                if value is None:
                    conn.execute("DELETE FROM settings WHERE key=?", (key,))
                else:
                    conn.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (key, value))

    def raw_settings(self, keys) -> Dict[str, Any]:
        """Сырые (как в БД) значения настроек — для снимка перед изменением."""
        out: Dict[str, Any] = {}
        for key in keys:
            row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
            out[key] = row["value"] if row is not None else None
        return out

    def all_settings(self) -> Dict[str, Any]:
        out = {}
        for r in self.query("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                out[r["key"]] = r["value"]
        return out

    def local_day(self) -> str:
        """Сегодняшняя дата по часам ПОЛЬЗОВАТЕЛЯ (Services подставляет свой часовой пояс).

        По UTC ночной бэкап в 02:30 по Москве записывался в «Активность»
        предыдущим днём.
        """
        provider = getattr(self, "day_provider", None)
        if provider is not None:
            try:
                return str(provider())
            except Exception:  # noqa: BLE001
                pass
        return utcnow_iso()[:10]

    def bump_daily_stats(self, account_id: int, *, messages: int = 0, bytes_: int = 0, jobs: int = 0, errors: int = 0) -> None:
        day = self.local_day()
        self.execute(
            """INSERT INTO stats_daily(day, account_id, messages, bytes, jobs, errors)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(day, account_id) DO UPDATE SET
                   messages=messages+excluded.messages,
                   bytes=bytes+excluded.bytes,
                   jobs=jobs+excluded.jobs,
                   errors=errors+excluded.errors""",
            (day, account_id, messages, bytes_, jobs, errors),
        )

    def daily_series(self, days: int = 30, account_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Активность по дням — СПЛОШНОЙ ряд за последние ``days`` дней (новые первыми).

        Раньше брались «последние N дней, в которых были данные»: дни простоя
        (сервер лежал, бэкапов не было) просто выпадали из графика.
        """
        from datetime import date as _date
        try:
            today = _date.fromisoformat(self.local_day())
        except ValueError:
            today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=max(1, int(days)) - 1)
        conds, params = ["day>=?"], [start.isoformat()]
        if account_id is not None:
            conds.append("account_id=?")
            params.append(account_id)
        rows = self.query(
            "SELECT day, SUM(messages) AS messages, SUM(bytes) AS bytes, SUM(jobs) AS jobs, "
            f"SUM(errors) AS errors FROM stats_daily WHERE {' AND '.join(conds)} GROUP BY day",
            tuple(params))
        by_day = {r["day"]: r for r in rows}
        out: List[Dict[str, Any]] = []
        for i in range(max(1, int(days))):
            day = (today - timedelta(days=i)).isoformat()
            r = by_day.get(day)
            out.append({"day": day, "messages": int(r["messages"] or 0) if r else 0,
                        "bytes": int(r["bytes"] or 0) if r else 0,
                        "jobs": int(r["jobs"] or 0) if r else 0,
                        "errors": int(r["errors"] or 0) if r else 0})
        return out

    def add_audit(self, user: str, action: str, detail: str = "") -> None:
        # Имя пользователя тоже обрезаем: при входе оно приходит от анонима.
        self.execute(
            "INSERT INTO audit(ts, user, action, detail) VALUES(?,?,?,?)",
            (utcnow_iso(), str(user or "")[:320], str(action or "")[:64], str(detail or "")[:1000]),
        )

    def list_audit(self, limit: int = 200) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ======================================================================
    #  Агрегаты для раздела «Аналитика»
    # ======================================================================
    ANALYTICS_COLS = "account_id, folder, size, internaldate, flags, has_attach, subject, from_addr"

    def index_rows_for_analytics(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Лёгкая выборка полей индекса писем для расчёта аналитики (без тел).

        Оставлена для совместимости; для больших архивов используйте
        :meth:`iter_index_rows_for_analytics` — она не держит весь индекс в
        памяти (200 000 строк списком — это около 140 МБ).
        """
        cols = self.ANALYTICS_COLS
        if account_id is None:
            return self.query(f"SELECT {cols} FROM messages")
        return self.query(f"SELECT {cols} FROM messages WHERE account_id=?", (account_id,))

    def iter_index_rows_for_analytics(self, account_id: Optional[int] = None, batch: int = 5000):
        """Итератор по тем же полям: читает порциями, ничего не материализует.

        Каждая порция читается ОТДЕЛЬНЫМ коротким запросом по первичному ключу.
        Держать один открытый курсор всё время расчёта нельзя: при
        ``database.wal: false`` он блокирует запись, и идущий в это же время
        бэкап (или запись сессии при входе) падает с «database is locked» —
        а расчёт на большом архиве идёт минуты. Обход по ``id`` вместо OFFSET
        ещё и устойчив к параллельным вставкам и удалениям.
        """
        cols = self.ANALYTICS_COLS
        last_id = 0
        while True:
            if account_id is None:
                chunk = self.query(
                    f"SELECT id, {cols} FROM messages WHERE id > ? ORDER BY id LIMIT ?",
                    (last_id, batch))
            else:
                chunk = self.query(
                    f"SELECT id, {cols} FROM messages WHERE account_id=? AND id > ? ORDER BY id LIMIT ?",
                    (account_id, last_id, batch))
            if not chunk:
                return
            for row in chunk:
                yield row
            last_id = chunk[-1]["id"]
            if len(chunk) < batch:
                return

    def largest_messages(self, account_id: Optional[int] = None, limit: int = 10) -> List[sqlite3.Row]:
        base = "SELECT account_id, folder, subject, from_addr, size, internaldate FROM messages"
        if account_id is None:
            return self.query(base + " ORDER BY size DESC LIMIT ?", (limit,))
        return self.query(base + " WHERE account_id=? ORDER BY size DESC LIMIT ?", (account_id, limit))

    def distinct_folder_count(self, account_id: Optional[int] = None) -> int:
        if account_id is None:
            return int(self.scalar(
                "SELECT COUNT(*) FROM (SELECT DISTINCT account_id, folder FROM messages)") or 0)
        return int(self.scalar(
            "SELECT COUNT(DISTINCT folder) FROM messages WHERE account_id=?", (account_id,)) or 0)

    def jobs_type_status_counts(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Счётчики заданий по типу и статусу; при заданном ящике — только его задания."""
        if account_id is None:
            return self.query("SELECT type, status, COUNT(*) AS c FROM jobs GROUP BY type, status")
        return self.query(
            "SELECT type, status, COUNT(*) AS c FROM jobs WHERE account_id=? GROUP BY type, status",
            (account_id,),
        )

    def jobs_duration_by_type(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Средняя и максимальная длительность завершённых заданий по типам (сек)."""
        where = ("WHERE started_at IS NOT NULL AND finished_at IS NOT NULL "
                 "AND finished_at >= started_at")
        params: tuple = ()
        if account_id is not None:
            where += " AND account_id=?"
            params = (account_id,)
        return self.query(
            "SELECT type, COUNT(*) AS c, "
            "AVG((julianday(finished_at)-julianday(started_at))*86400.0) AS avg_s, "
            "MAX((julianday(finished_at)-julianday(started_at))*86400.0) AS max_s "
            f"FROM jobs {where} GROUP BY type",
            params,
        )

    def runs_totals(self, account_id: Optional[int] = None) -> sqlite3.Row:
        base = ("SELECT COUNT(*) AS runs, COALESCE(SUM(messages_new),0) AS msgs, "
                "COALESCE(SUM(bytes_new),0) AS bytes, COALESCE(SUM(errors),0) AS errors FROM runs")
        if account_id is None:
            return self.query_one(base)
        return self.query_one(base + " WHERE account_id=?", (account_id,))

    def runs_type_status_counts(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        if account_id is None:
            return self.query("SELECT type, status, COUNT(*) AS c FROM runs GROUP BY type, status")
        return self.query(
            "SELECT type, status, COUNT(*) AS c FROM runs WHERE account_id=? GROUP BY type, status",
            (account_id,),
        )

    def exports_stats(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        base = ("SELECT format, engine, status, COUNT(*) AS c, COALESCE(SUM(size),0) AS bytes "
                "FROM exports")
        if account_id is None:
            return self.query(base + " GROUP BY format, engine, status")
        return self.query(base + " WHERE account_id=? GROUP BY format, engine, status", (account_id,))

    def restores_totals(self, account_id: Optional[int] = None) -> sqlite3.Row:
        base = ("SELECT COUNT(*) AS c, COALESCE(SUM(restored),0) AS restored, "
                "COALESCE(SUM(errors),0) AS errors FROM restores")
        if account_id is None:
            return self.query_one(base)
        return self.query_one(base + " WHERE account_id=?", (account_id,))

    def audit_action_counts(self, limit: int = 15, account_id: Optional[int] = None,
                            account_name: Optional[str] = None) -> List[sqlite3.Row]:
        """Топ действий аудита; при заданном ящике — только записи о нём.

        Колонки account_id в таблице audit нет: принадлежность записи к ящику
        видна лишь по тексту detail — это либо маркер ``account=<id>``
        (retention, аналитика, вход по ящику), либо ровно имя ящика
        (создание/изменение/удаление). По ним и отбираем.
        """
        if account_id is None:
            return self.query(
                "SELECT action, COUNT(*) AS c FROM audit GROUP BY action ORDER BY c DESC LIMIT ?", (limit,))
        marker = f"account={int(account_id)}"
        conds = ["detail=?", "detail LIKE ?", "detail LIKE ?", "detail LIKE ?"]
        params: List[Any] = [marker, f"{marker} %", f"% {marker}", f"% {marker} %"]
        if account_name:
            # точное сравнение (а не LIKE): имя ящика может содержать % и _
            conds.append("detail=?")
            params.append(account_name)
        params.append(limit)
        return self.query(
            f"SELECT action, COUNT(*) AS c FROM audit WHERE {' OR '.join(conds)} "
            f"GROUP BY action ORDER BY c DESC LIMIT ?",
            tuple(params),
        )

    def count_active_sessions(self, account_id: Optional[int] = None) -> int:
        """Действующие сессии; при заданном ящике — только входы в этот ящик."""
        if account_id is None:
            return int(self.scalar("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (utcnow_iso(),)) or 0)
        return int(self.scalar(
            "SELECT COUNT(*) FROM sessions WHERE expires_at > ? AND account_id=?",
            (utcnow_iso(), account_id)) or 0)

    def count_login_failures_since(self, since_iso: str, username: Optional[str] = None) -> int:
        """Неудачные входы с момента; при заданном username — только по нему
        (для ящика это его собственный логин)."""
        if username is None:
            return int(self.scalar(
                "SELECT COUNT(*) FROM login_attempts WHERE success=0 AND ts>=?", (since_iso,)) or 0)
        return int(self.scalar(
            "SELECT COUNT(*) FROM login_attempts WHERE success=0 AND ts>=? AND username=? COLLATE NOCASE",
            (since_iso, username)) or 0)

    def users_by_role(self) -> List[sqlite3.Row]:
        return self.query("SELECT role, COUNT(*) AS c FROM users GROUP BY role")


# ---------------------------------------------------------------------------
#  DDL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'admin',
    created_at       TEXT,
    last_login       TEXT,
    disabled         INTEGER NOT NULL DEFAULT 0,
    totp_enabled     INTEGER NOT NULL DEFAULT 0,
    totp_secret_enc  TEXT DEFAULT '',
    totp_pending_enc TEXT DEFAULT '',
    totp_last_step   INTEGER NOT NULL DEFAULT 0,
    totp_recovery    TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    ts       TEXT,
    success  INTEGER,
    ip       TEXT,
    -- '' — пароль; 'otp' — код второго шага; 'imap' — проверка пароля ящика
    kind     TEXT DEFAULT ''
);

-- «Билеты» второго шага входа (2FA): одноразовые, с лимитом попыток и
-- привязкой к паролю — смена пароля отзывает выданные билеты.
CREATE TABLE IF NOT EXISTS otp_challenges (
    nonce    TEXT PRIMARY KEY,
    user_id  INTEGER NOT NULL,
    expires  INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    pw       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username, ts);
-- запросы по логину идут с COLLATE NOCASE — им нужен индекс с той же сортировкой,
-- иначе SQLite делает полный скан таблицы
CREATE INDEX IF NOT EXISTS idx_login_attempts_nc ON login_attempts(username COLLATE NOCASE, ts);
-- блокировка перебора считается по паре «логин + источник», плюс отдельно по
-- самому источнику (перебор имён с одного IP) — для этого нужен индекс по ip
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip, ts);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    role       TEXT DEFAULT 'admin',
    account_id INTEGER,
    created_at TEXT,
    last_seen  TEXT,
    expires_at TEXT,
    ip         TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    name                      TEXT NOT NULL,
    host                      TEXT NOT NULL,
    port                      INTEGER NOT NULL DEFAULT 993,
    username                  TEXT NOT NULL,
    password_enc              TEXT,
    auth_type                 TEXT NOT NULL DEFAULT 'password',
    security                  TEXT NOT NULL DEFAULT 'ssl',
    enabled                   INTEGER NOT NULL DEFAULT 1,
    folder_include            TEXT DEFAULT '[]',
    folder_exclude            TEXT DEFAULT '[]',
    oauth_client_id           TEXT DEFAULT '',
    oauth_client_secret_enc   TEXT DEFAULT '',
    oauth_refresh_token_enc   TEXT DEFAULT '',
    oauth_token_url           TEXT DEFAULT '',
    notes                     TEXT DEFAULT '',
    retention_days            INTEGER DEFAULT -1,
    created_at                TEXT,
    updated_at                TEXT,
    login_status              TEXT DEFAULT '',   -- итог последней попытки входа
    login_checked_at          TEXT,
    login_error               TEXT DEFAULT '',
    first_backup_at           TEXT,              -- первая удачная резервная копия
    last_backup_at            TEXT,              -- последняя удачная резервная копия
    last_backup_status        TEXT DEFAULT '',
    hold_until                TEXT DEFAULT '',   -- удержание архива до даты (ГГГГ-ММ-ДД)
    hold_reason               TEXT DEFAULT '',   -- dismissed | manual
    dismissed_at              TEXT,              -- когда сотрудник уволен
    auto_disabled             INTEGER NOT NULL DEFAULT 0  -- копирование выключено увольнением
);

-- Сотрудники организации. Ящик (accounts) необязателен: сотрудник может
-- существовать без почты, а удаление ящика НЕ удаляет карточку сотрудника
-- (ON DELETE SET NULL — связь просто обнуляется).
CREATE TABLE IF NOT EXISTS employees (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT,
    full_name    TEXT NOT NULL,
    email        TEXT,
    position     TEXT,
    department   TEXT,
    phone        TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    account_id   INTEGER,
    notes        TEXT DEFAULT '',
    source       TEXT DEFAULT 'manual',
    created_at   TEXT,
    updated_at   TEXT,
    last_seen_at TEXT,
    had_account  INTEGER NOT NULL DEFAULT 0,
    dismissed_at TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE SET NULL
);
-- поиск сотрудника по почте идёт с COLLATE NOCASE — индексу нужна та же сортировка
CREATE INDEX IF NOT EXISTS idx_employees_email ON employees(email COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_employees_ext ON employees(external_id);
CREATE INDEX IF NOT EXISTS idx_employees_acc ON employees(account_id);

CREATE TABLE IF NOT EXISTS folders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    folder      TEXT NOT NULL,
    uidvalidity INTEGER,
    last_uid    INTEGER DEFAULT 0,
    msg_count   INTEGER DEFAULT 0,
    updated_at  TEXT,
    UNIQUE(account_id, folder),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS folder_problems (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL,
    folder        TEXT NOT NULL,
    fails         INTEGER DEFAULT 0,   -- сколько прогонов подряд папка не открывается
    first_failed  TEXT,                -- когда перестала открываться впервые
    last_failed   TEXT,                -- когда пробовали в последний раз
    last_error    TEXT,                -- ответ сервера (для интерфейса и поддержки)
    counted_at    TEXT,                -- когда счётчик fails увеличивался в последний раз
    UNIQUE(account_id, folder),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    folder       TEXT NOT NULL,
    uidvalidity  INTEGER NOT NULL,
    uid          INTEGER NOT NULL,
    message_id   TEXT,
    size         INTEGER DEFAULT 0,
    internaldate TEXT,
    flags        TEXT,
    stored_path  TEXT,
    sha256       TEXT,
    subject      TEXT,
    from_addr    TEXT,
    has_attach   INTEGER DEFAULT 0,
    backed_up_at TEXT,
    UNIQUE(account_id, folder, uidvalidity, uid),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(account_id, sha256);
-- под горячий ORDER BY internaldate DESC в списках писем
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(account_id, folder, internaldate);
-- список «все папки», ретеншн, фильтр дат экспорта — по ящику и дате
CREATE INDEX IF NOT EXISTS idx_messages_acc_date ON messages(account_id, internaldate);
-- счётчики и суммы размеров по ящику (дашборд), крупнейшие письма
CREATE INDEX IF NOT EXISTS idx_messages_acc_size ON messages(account_id, size);

-- Письма, вычищенные по сроку хранения: их UID помнятся, чтобы бэкап не
-- скачивал их снова, пока они лежат на сервере.
CREATE TABLE IF NOT EXISTS retired_uids (
    account_id  INTEGER NOT NULL,
    folder      TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    retired_at  TEXT,
    PRIMARY KEY(account_id, folder, uidvalidity, uid),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    type             TEXT NOT NULL,
    account_id       INTEGER,
    status           TEXT NOT NULL DEFAULT 'queued',
    priority         INTEGER NOT NULL DEFAULT 5,
    params           TEXT DEFAULT '{}',
    created_at       TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    updated_at       TEXT,
    attempts         INTEGER DEFAULT 0,
    max_attempts     INTEGER DEFAULT 1,
    worker_id        TEXT,
    progress_current INTEGER DEFAULT 0,
    progress_total   INTEGER DEFAULT 0,
    progress_message TEXT DEFAULT '',
    bytes_done       INTEGER DEFAULT 0,
    speed            REAL DEFAULT 0,
    cancel_requested INTEGER DEFAULT 0,
    created_by       TEXT DEFAULT '',
    result           TEXT DEFAULT '{}',
    error            TEXT DEFAULT '',
    run_after        TEXT,
    restarts         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority, id);

CREATE TABLE IF NOT EXISTS job_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL,
    ts      TEXT,
    level   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_events ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL,
    type           TEXT NOT NULL,
    job_id         INTEGER,
    status         TEXT,
    started_at     TEXT,
    finished_at    TEXT,
    messages_new   INTEGER DEFAULT 0,
    bytes_new      INTEGER DEFAULT 0,
    messages_total INTEGER DEFAULT 0,
    errors         INTEGER DEFAULT 0,
    detail         TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_acc ON runs(account_id, id);

CREATE TABLE IF NOT EXISTS schedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'cron',
    job_type         TEXT NOT NULL DEFAULT 'backup',
    cron_expr        TEXT DEFAULT '',
    interval_seconds INTEGER DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    options          TEXT DEFAULT '{}',
    last_run         TEXT,
    next_run         TEXT,
    created_at       TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    engine     TEXT,
    format     TEXT,
    path       TEXT,
    size       INTEGER DEFAULT 0,
    params     TEXT DEFAULT '{}',
    job_id     INTEGER,
    status     TEXT DEFAULT 'pending',
    error      TEXT DEFAULT '',
    created_at TEXT,
    created_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS restores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    params     TEXT DEFAULT '{}',
    job_id     INTEGER,
    status     TEXT DEFAULT 'pending',
    restored   INTEGER DEFAULT 0,
    errors     INTEGER DEFAULT 0,
    error      TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS stats_daily (
    day        TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    messages   INTEGER DEFAULT 0,
    bytes      INTEGER DEFAULT 0,
    jobs       INTEGER DEFAULT 0,
    errors     INTEGER DEFAULT 0,
    PRIMARY KEY(day, account_id)
);

-- Копия вне сервера: какие файлы уже отправлены (путь в копии, размер и время
-- изменения на момент отправки). По ней прогон отправляет только новое.
CREATE TABLE IF NOT EXISTS replica_files (
    path     TEXT PRIMARY KEY,
    grp      TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    done_at  TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_replica_grp ON replica_files(grp);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT,
    user   TEXT,
    action TEXT,
    detail TEXT
);
"""
