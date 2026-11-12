"""
API-токены для скриптов и интеграций (1.20.0).

Токен выпускает администратор (раздел «Пользователи»): с именем, ролью («администратор»
или «оператор» — не выше роли владельца) и сроком действия. Сам токен показывается один
раз; в базе хранится только его SHA-256 и первые знаки для узнавания. Запрос с заголовком
``Authorization: Bearer ma_…`` выполняется от имени владельца с ролью токена; в аудите
такие действия подписаны «<владелец> [токен «имя»]».

Токен не открывает раздел «о себе» (пароль, двухфакторный вход) и управление самими
токенами; отключение или удаление владельца, истечение срока и отзыв действуют сразу.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .errors import ValidationError
from .roles import ROLE_ADMIN, ROLE_OPERATOR
from .util import utcnow_iso

PREFIX = "ma_"
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    hint         TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'operator',
    created_at   TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    expires_at   TEXT NOT NULL DEFAULT '',
    last_used_at TEXT NOT NULL DEFAULT '',
    last_ip      TEXT NOT NULL DEFAULT '',
    revoked_at   TEXT NOT NULL DEFAULT ''
);
"""
MAX_DAYS = 3650
#: Отметку «последнее использование» обновляем не чаще раза в минуту.
TOUCH_EVERY_S = 60


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(svc, actor: dict, *, name: str, role: str, days: int, user_id: Optional[int] = None) -> Dict:
    name = (name or "").strip()[:100]
    if not name:
        raise ValidationError("Укажите имя токена — для чего он (например «Zabbix» или «скрипт выгрузки»).")
    if role not in (ROLE_ADMIN, ROLE_OPERATOR):
        raise ValidationError("Роль токена — администратор или оператор.")
    owner_id = int(user_id or actor["id"])
    owner = svc.db.get_user_by_id(owner_id)
    if owner is None or owner["disabled"]:
        raise ValidationError("Владелец токена не найден или отключён.")
    if owner["role"] == ROLE_OPERATOR and role == ROLE_ADMIN:
        raise ValidationError("Роль токена не может быть выше роли владельца.")
    days = int(days or 0)
    if days < 0 or days > MAX_DAYS:
        raise ValidationError(f"Срок действия — от 1 до {MAX_DAYS} дней (0 — бессрочно).")
    token = PREFIX + secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else ""
    cur = svc.db.execute("INSERT INTO api_tokens(user_id, name, token_hash, hint, role, created_at, created_by, "
                         "expires_at) VALUES(?,?,?,?,?,?,?,?)",
                         (owner_id, name, _hash(token), token[:7] + "…" + token[-4:], role, utcnow_iso(),
                          actor["username"], expires))
    svc.db.add_audit(actor["username"], "token_create",
                     f"«{name}» для {owner['username']}, роль {role}, " + (f"до {expires[:10]}" if expires else "бессрочно"))
    return {"id": int(cur.lastrowid), "token": token, "expires_at": expires}


def revoke(svc, actor: dict, token_id: int) -> None:
    row = svc.db.query_one("SELECT * FROM api_tokens WHERE id=?", (int(token_id),))
    if row is None:
        raise ValidationError("Токен не найден.", code="not_found")
    if row["revoked_at"]:
        return
    svc.db.execute("UPDATE api_tokens SET revoked_at=? WHERE id=?", (utcnow_iso(), int(token_id)))
    svc.db.add_audit(actor["username"], "token_revoke", f"«{row['name']}» ({row['hint']})")


def list_tokens(svc) -> List[Dict]:
    now = utcnow_iso()
    out = []
    for r in svc.db.query("SELECT t.*, u.username FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id "
                          "ORDER BY (t.revoked_at = '') DESC, t.id DESC"):
        state = "revoked" if r["revoked_at"] else ("expired" if r["expires_at"] and r["expires_at"] < now else "active")
        out.append({"id": int(r["id"]), "name": r["name"], "owner": r["username"] or "—", "role": r["role"],
                    "hint": r["hint"], "created_at": r["created_at"], "created_by": r["created_by"],
                    "expires_at": r["expires_at"], "last_used_at": r["last_used_at"], "last_ip": r["last_ip"],
                    "revoked_at": r["revoked_at"], "state": state})
    return out


def user_for(svc, token: str, ip: str = "") -> Optional[Dict]:
    """Пользователь по токену (или None): владелец с ролью токена."""
    if not token.startswith(PREFIX) or len(token) > 200:
        return None
    try:
        row = svc.db.query_one("SELECT * FROM api_tokens WHERE token_hash=?", (_hash(token),))
    except Exception:  # noqa: BLE001 - таблицы ещё нет
        return None
    if row is None or row["revoked_at"]:
        return None
    now = datetime.now(timezone.utc)
    if row["expires_at"]:
        try:
            if datetime.fromisoformat(row["expires_at"]) < now:
                return None
        except ValueError:
            return None
    owner = svc.db.get_user_by_id(row["user_id"])
    if owner is None or owner["disabled"]:
        return None
    role = row["role"]
    if owner["role"] == ROLE_OPERATOR:
        role = ROLE_OPERATOR                    # владельца понизили — токен не выше его роли
    elif owner["role"] not in (ROLE_ADMIN, ROLE_OPERATOR):
        return None
    last = row["last_used_at"]
    try:
        stale = not last or (now - datetime.fromisoformat(last)).total_seconds() > TOUCH_EVERY_S
    except ValueError:
        stale = True
    if stale:
        try:
            svc.db.execute("UPDATE api_tokens SET last_used_at=?, last_ip=? WHERE id=?",
                           (now.isoformat(), (ip or "")[:64], row["id"]))
        except Exception:  # noqa: BLE001 - отметка не должна мешать запросу
            pass
    return {"id": owner["id"], "username": f"{owner['username']} [токен «{row['name']}»]", "role": role,
            "totp_enabled": True, "via_token": int(row["id"]), "owner": owner["username"]}
