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
import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Sequence

from . import models
from .logging_setup import get_logger
from .security import SecretBox
from .util import chunked, utcnow_iso

SCHEMA_VERSION = 1

log = get_logger("db")

#: Настройки, значения которых хранятся в БД в зашифрованном виде
#: (шифруются в :meth:`Database.set_setting`, расшифровываются в
#: :meth:`Database.get_setting`).
_ENCRYPTED_SETTINGS = {"notifications.smtp_password"}


#: Выборка сотрудника вместе с названием и состоянием привязанного ящика:
#: интерфейсу нужны account_name/account_enabled, а отдельный запрос на строку
#: превратил бы список в N+1 обращений к БД.
_EMPLOYEE_SELECT = (
    "SELECT e.*, a.name AS account_name, a.enabled AS account_enabled "
    "FROM employees e LEFT JOIN accounts a ON a.id = e.account_id"
)


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
        ]
        changed = False
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
        return self.query("SELECT id, username, role, created_at, last_login, disabled FROM users ORDER BY id")

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        self.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

    def set_last_login(self, user_id: int) -> None:
        self.execute("UPDATE users SET last_login=? WHERE id=?", (utcnow_iso(), user_id))

    def set_user_disabled(self, user_id: int, disabled: bool) -> None:
        self.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))

    def delete_user(self, user_id: int) -> None:
        self.execute("DELETE FROM users WHERE id=?", (user_id,))

    def record_login_attempt(self, username: str, success: bool, ip: str = "") -> None:
        self.execute(
            "INSERT INTO login_attempts(username, ts, success, ip) VALUES(?,?,?,?)",
            (username, utcnow_iso(), 1 if success else 0, ip),
        )

    def count_recent_failures(self, username: str, since_iso: str, ip: Optional[str] = None) -> int:
        """Неудачные попытки входа за период.

        ``ip`` задан → считаются только попытки с этого источника, то есть по
        ПАРЕ «имя пользователя + IP». Без него — по учётной записи целиком
        (прежнее поведение, используется для общесистемной защиты).
        """
        if ip is None:
            return int(
                self.scalar(
                    "SELECT COUNT(*) FROM login_attempts WHERE username=? COLLATE NOCASE AND success=0 AND ts>=?",
                    (username, since_iso),
                )
                or 0
            )
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM login_attempts "
                "WHERE username=? COLLATE NOCASE AND ip=? AND success=0 AND ts>=?",
                (username, ip, since_iso),
            )
            or 0
        )

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
    def _account_from_row(self, row: sqlite3.Row) -> models.Account:
        return models.Account(
            id=row["id"],
            name=row["name"],
            host=row["host"],
            port=row["port"],
            username=row["username"],
            password=self.secret.decrypt(row["password_enc"]) if row["password_enc"] else "",
            auth_type=row["auth_type"],
            security=row["security"],
            enabled=bool(row["enabled"]),
            folder_include=json.loads(row["folder_include"] or "[]"),
            folder_exclude=json.loads(row["folder_exclude"] or "[]"),
            oauth_client_id=row["oauth_client_id"] or "",
            oauth_client_secret=self.secret.decrypt(row["oauth_client_secret_enc"]) if row["oauth_client_secret_enc"] else "",
            oauth_refresh_token=self.secret.decrypt(row["oauth_refresh_token_enc"]) if row["oauth_refresh_token_enc"] else "",
            oauth_token_url=row["oauth_token_url"] or "",
            notes=row["notes"] or "",
            retention_days=row["retention_days"] if row["retention_days"] is not None else -1,
        )

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

    def delete_account(self, account_id: int) -> None:
        self.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    # ======================================================================
    #  Сотрудники
    # ======================================================================
    #: Поля карточки сотрудника, которые можно изменять из интерфейса и при
    #: синхронизации. Белый список нужен, чтобы update_employee нельзя было
    #: заставить переписать служебные колонки (id, created_at и т.п.).
    EMPLOYEE_FIELDS = ("external_id", "full_name", "email", "position", "department",
                       "phone", "status", "account_id", "notes", "source", "last_seen_at")

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
        """Сводка для шапки раздела: по статусам и по наличию ящика."""
        row = self.query_one(
            """SELECT
                   COALESCE(SUM(CASE WHEN status='active'   THEN 1 ELSE 0 END), 0) AS active,
                   COALESCE(SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END), 0) AS archived,
                   COALESCE(SUM(CASE WHEN account_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS with_account,
                   COALESCE(SUM(CASE WHEN account_id IS NULL THEN 1 ELSE 0 END), 0) AS without_account
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
        """Привязать сотрудника к ящику (None — отвязать)."""
        self.execute("UPDATE employees SET account_id=?, updated_at=? WHERE id=?",
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
    def record_folder_problem(self, account_id: int, folder: str, error: str = "") -> int:
        """Отметить, что папка снова не открылась, и вернуть число неудач ПОДРЯД.

        Счётчик нужен, чтобы отличать свежую поломку (о ней надо кричать) от
        папки, которая не открывается на сервере неделями: бесконечное «копия
        неполная» приучает не читать предупреждения, и настоящая пропажа писем
        теряется среди них.
        """
        now = utcnow_iso()
        self.execute(
            """INSERT INTO folder_problems(account_id, folder, fails, first_failed, last_failed, last_error)
               VALUES(?,?,1,?,?,?)
               ON CONFLICT(account_id, folder) DO UPDATE SET
                   fails=folder_problems.fails+1,
                   last_failed=excluded.last_failed,
                   last_error=excluded.last_error""",
            (account_id, folder, now, now, (error or "")[:1000]),
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
        rows = self.query(
            "SELECT uid FROM messages WHERE account_id=? AND folder=? AND uidvalidity=?",
            (account_id, folder, uidvalidity),
        )
        return {r["uid"] for r in rows}

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
        if folder:
            return self.query(
                "SELECT * FROM messages WHERE account_id=? AND folder=? ORDER BY internaldate DESC LIMIT ? OFFSET ?",
                (account_id, folder, limit, offset),
            )
        return self.query(
            "SELECT * FROM messages WHERE account_id=? ORDER BY internaldate DESC LIMIT ? OFFSET ?",
            (account_id, limit, offset),
        )

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
        offset = 0
        while True:
            rows = self.query(
                "SELECT id, folder, stored_path FROM messages WHERE account_id=? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (account_id, batch, offset),
            )
            if not rows:
                return
            for row in rows:
                yield int(row["id"]), row["folder"], (row["stored_path"] or "")
            offset += len(rows)

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

    def claim_next_job_filtered(self, worker_id: str, skip_accounts) -> Optional[sqlite3.Row]:
        """Захватить следующее задание, пропуская аккаунты из skip_accounts."""
        skip_accounts = list(skip_accounts or [])
        with self._write_lock:
            conn = self.connect()
            try:
                if skip_accounts:
                    ph = ",".join("?" * len(skip_accounts))
                    sql = (f"SELECT * FROM jobs WHERE status=? AND (account_id IS NULL OR account_id NOT IN ({ph})) "
                           f"ORDER BY priority ASC, id ASC LIMIT 1")
                    row = conn.execute(sql, (models.JobStatus.QUEUED, *skip_accounts)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM jobs WHERE status=? ORDER BY priority ASC, id ASC LIMIT 1",
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

    def requeue_job(self, job_id: int) -> None:
        self.execute(
            "UPDATE jobs SET status=?, worker_id=NULL, started_at=NULL, error='' WHERE id=?",
            (models.JobStatus.QUEUED, job_id),
        )

    def request_cancel(self, job_id: int) -> None:
        self.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))

    def is_cancel_requested(self, job_id: int) -> bool:
        row = self.query_one("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        return bool(row and row["cancel_requested"])

    def reset_orphan_jobs(self) -> int:
        """При старте: задания в статусе RUNNING считаем прерванными → в очередь или ошибка."""
        rows = self.query("SELECT id, attempts, max_attempts FROM jobs WHERE status=?", (models.JobStatus.RUNNING,))
        count = 0
        for r in rows:
            if r["attempts"] < r["max_attempts"]:
                self.requeue_job(r["id"])
            else:
                self.finish_job(r["id"], models.JobStatus.FAILED, error="Прервано при перезапуске сервиса")
            count += 1
        return count

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
        cur = self.execute(
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
    def create_export(self, account_id: int, engine: str, fmt: str, path: str, params: Dict, job_id: Optional[int]) -> int:
        cur = self.execute(
            "INSERT INTO exports(account_id, engine, format, path, params, job_id, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (account_id, engine, fmt, path, json.dumps(params, ensure_ascii=False), job_id, "pending", utcnow_iso()),
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

    def all_settings(self) -> Dict[str, Any]:
        out = {}
        for r in self.query("SELECT key, value FROM settings"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except (json.JSONDecodeError, TypeError):
                out[r["key"]] = r["value"]
        return out

    def bump_daily_stats(self, account_id: int, *, messages: int = 0, bytes_: int = 0, jobs: int = 0, errors: int = 0) -> None:
        day = utcnow_iso()[:10]
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

    def daily_series(self, days: int = 30, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Активность по дням; при заданном ящике — только его строки."""
        where = " WHERE account_id=?" if account_id is not None else ""
        params: tuple = (account_id, days) if account_id is not None else (days,)
        return self.query(
            "SELECT day, SUM(messages) AS messages, SUM(bytes) AS bytes, SUM(jobs) AS jobs, "
            f"SUM(errors) AS errors FROM stats_daily{where} GROUP BY day ORDER BY day DESC LIMIT ?",
            params,
        )

    def add_audit(self, user: str, action: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO audit(ts, user, action, detail) VALUES(?,?,?,?)",
            (utcnow_iso(), user, action, detail[:1000]),
        )

    def list_audit(self, limit: int = 200) -> List[sqlite3.Row]:
        return self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ======================================================================
    #  Агрегаты для раздела «Аналитика»
    # ======================================================================
    def index_rows_for_analytics(self, account_id: Optional[int] = None) -> List[sqlite3.Row]:
        """Лёгкая выборка полей индекса писем для расчёта аналитики (без тел)."""
        cols = "account_id, folder, size, internaldate, flags, has_attach, subject, from_addr"
        if account_id is None:
            return self.query(f"SELECT {cols} FROM messages")
        return self.query(f"SELECT {cols} FROM messages WHERE account_id=?", (account_id,))

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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin',
    created_at    TEXT,
    last_login    TEXT,
    disabled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    ts       TEXT,
    success  INTEGER,
    ip       TEXT
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
    updated_at                TEXT
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
CREATE INDEX IF NOT EXISTS idx_messages_acc ON messages(account_id, folder);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(account_id, sha256);
-- под горячий ORDER BY internaldate DESC в списках писем
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(account_id, folder, internaldate);

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
    error            TEXT DEFAULT ''
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
    created_at TEXT
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

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT,
    user   TEXT,
    action TEXT,
    detail TEXT
);
"""
