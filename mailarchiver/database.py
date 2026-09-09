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
from typing import Any, Dict, List, Optional

from . import models
from .security import SecretBox
from .util import utcnow_iso

SCHEMA_VERSION = 1


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
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def executemany(self, sql: str, seq_params) -> None:
        conn = self.connect()
        with self._write_lock:
            conn.executemany(sql, seq_params)
            conn.commit()

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
            conn.executescript(_SCHEMA_SQL)
            cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()
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
            except sqlite3.OperationalError:
                pass
        if changed:
            conn.commit()

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

    def count_recent_failures(self, username: str, since_iso: str) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM login_attempts WHERE username=? COLLATE NOCASE AND success=0 AND ts>=?",
                (username, since_iso),
            )
            or 0
        )

    def clear_login_failures(self, username: str) -> None:
        self.execute("DELETE FROM login_attempts WHERE username=? COLLATE NOCASE", (username,))

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

    def claim_next_job_filtered(self, worker_id: str, skip_accounts) -> Optional[sqlite3.Row]:
        """Захватить следующее задание, пропуская аккаунты из skip_accounts."""
        skip_accounts = list(skip_accounts or [])
        with self._write_lock:
            conn = self.connect()
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

    def add_job_event(self, job_id: int, level: str, message: str) -> None:
        self.execute(
            "INSERT INTO job_events(job_id, ts, level, message) VALUES(?,?,?,?)",
            (job_id, utcnow_iso(), level, message[:1000]),
        )

    def list_job_events(self, job_id: int, limit: int = 500) -> List[sqlite3.Row]:
        return self.query(
            "SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        )

    def purge_old_jobs(self, keep: int) -> int:
        """Оставить последние keep завершённых заданий, остальные удалить."""
        ids = self.query(
            "SELECT id FROM jobs WHERE status NOT IN (?,?) ORDER BY id DESC LIMIT -1 OFFSET ?",
            (models.JobStatus.QUEUED, models.JobStatus.RUNNING, keep),
        )
        removed = 0
        for r in ids:
            self.execute("DELETE FROM job_events WHERE job_id=?", (r["id"],))
            self.execute("DELETE FROM jobs WHERE id=?", (r["id"],))
            removed += 1
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
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
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

    def daily_series(self, days: int = 30) -> List[sqlite3.Row]:
        return self.query(
            """SELECT day, SUM(messages) AS messages, SUM(bytes) AS bytes, SUM(jobs) AS jobs, SUM(errors) AS errors
               FROM stats_daily GROUP BY day ORDER BY day DESC LIMIT ?""",
            (days,),
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

    def jobs_type_status_counts(self) -> List[sqlite3.Row]:
        return self.query("SELECT type, status, COUNT(*) AS c FROM jobs GROUP BY type, status")

    def jobs_duration_by_type(self) -> List[sqlite3.Row]:
        """Средняя и максимальная длительность завершённых заданий по типам (сек)."""
        return self.query(
            "SELECT type, COUNT(*) AS c, "
            "AVG((julianday(finished_at)-julianday(started_at))*86400.0) AS avg_s, "
            "MAX((julianday(finished_at)-julianday(started_at))*86400.0) AS max_s "
            "FROM jobs WHERE started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND finished_at >= started_at GROUP BY type"
        )

    def runs_totals(self, account_id: Optional[int] = None) -> sqlite3.Row:
        base = ("SELECT COUNT(*) AS runs, COALESCE(SUM(messages_new),0) AS msgs, "
                "COALESCE(SUM(bytes_new),0) AS bytes, COALESCE(SUM(errors),0) AS errors FROM runs")
        if account_id is None:
            return self.query_one(base)
        return self.query_one(base + " WHERE account_id=?", (account_id,))

    def runs_type_status_counts(self) -> List[sqlite3.Row]:
        return self.query("SELECT type, status, COUNT(*) AS c FROM runs GROUP BY type, status")

    def exports_stats(self) -> List[sqlite3.Row]:
        return self.query(
            "SELECT format, engine, status, COUNT(*) AS c, COALESCE(SUM(size),0) AS bytes "
            "FROM exports GROUP BY format, engine, status")

    def restores_totals(self) -> sqlite3.Row:
        return self.query_one(
            "SELECT COUNT(*) AS c, COALESCE(SUM(restored),0) AS restored, "
            "COALESCE(SUM(errors),0) AS errors FROM restores")

    def audit_action_counts(self, limit: int = 15) -> List[sqlite3.Row]:
        return self.query(
            "SELECT action, COUNT(*) AS c FROM audit GROUP BY action ORDER BY c DESC LIMIT ?", (limit,))

    def count_active_sessions(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", (utcnow_iso(),)) or 0)

    def count_login_failures_since(self, since_iso: str) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) FROM login_attempts WHERE success=0 AND ts>=?", (since_iso,)) or 0)

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
