"""
Общее для единого входа (OpenID Connect с 1.22.0, SAML 2.0 с 1.23.0): учётная запись архива для пользователя
поставщика, одноразовые отметки (защита от повтора ответа поставщика).

Учётная запись заводится сама (``users.auth_source`` — ``oidc`` или ``saml``); локальный пользователь с тем же
логином через единый вход не входит, отключённый администратором — тоже. Пользователь, у которого у поставщика
больше нет групп архива, отключается (``users.disabled_by`` — способ входа) и включается сам, когда права
вернут. Второй фактор — у поставщика: «Обязательный двухфакторный вход» архива к таким входам не применяется.
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

SSO_SOURCES = ("oidc", "saml")
SOURCE_LABEL = {"oidc": "OIDC", "saml": "SAML"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sso_state (
    kind   TEXT NOT NULL,              -- saml_assertion (уже принятые утверждения), saml_ticket (вход ждёт браузер)
    key    TEXT NOT NULL,
    data   TEXT NOT NULL DEFAULT '',
    until  REAL NOT NULL,              -- время Unix, после которого запись не нужна
    PRIMARY KEY (kind, key)
);
"""


class SsoError(Exception):
    """Единый вход не удался — текст для журнала и аудита (пользователю — общий ответ)."""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)


def values(v) -> List[str]:
    """Значения утверждения или атрибута списком строк (строка через запятую или «;» — тоже список)."""
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def role_from(have: List[str], admin_values, operator_values, employee_login: bool, email: str) -> str:
    """Роль по группам у поставщика: admin | operator | mailbox (сотрудник по email) | '' (нельзя)."""
    got = {x.lower() for x in have}
    if got & {x.lower() for x in values(admin_values)}:
        return "admin"
    if got & {x.lower() for x in values(operator_values)}:
        return "operator"
    if employee_login and email:
        return "mailbox"
    return ""


def ensure_user(svc, info: Dict, source: str, error=SsoError) -> Optional[Dict]:
    """Учётная запись архива для пользователя поставщика. None — прав на архив нет (запись отключена)."""
    from .security import hash_password, new_token
    from .web.auth import _row_get
    label = SOURCE_LABEL.get(source, source)
    username, role = info["username"], info["role"]
    existing = svc.db.get_user_by_name(username)
    if existing is not None and _row_get(existing, "auth_source", "local") not in SSO_SOURCES:
        raise error(f"в архиве уже есть локальный пользователь {username} — через единый вход он не входит")
    if role not in ("admin", "operator"):
        if existing is not None and not existing["disabled"]:
            svc.db.set_user_disabled(int(existing["id"]), True, by=source)
            svc.db.add_audit("system", "user_disable", f"{username}: у поставщика больше нет прав на архив ({label})")
        return None
    if existing is None:
        uid = svc.db.create_user(username, hash_password(new_token(24)), role)
        svc.db.execute("UPDATE users SET auth_source=? WHERE id=?", (source, uid))
        svc.db.add_audit("system", "user_create", f"{username}: из единого входа ({label}), роль {role}")
        return dict(svc.db.get_user_by_id(uid))
    if existing["disabled"] and _row_get(existing, "disabled_by", "") not in ("", *SSO_SOURCES):
        raise error(f"{username} отключён администратором")
    if existing["role"] != role or existing["disabled"] or _row_get(existing, "auth_source", "") != source:
        svc.db.execute("UPDATE users SET role=?, disabled=0, disabled_by='', auth_source=? WHERE id=?",
                       (role, source, existing["id"]))
        if existing["role"] != role or existing["disabled"]:
            svc.db.add_audit("system", "user_role", f"{username}: роль из единого входа ({label}) — {role}")
    return dict(svc.db.get_user_by_id(int(existing["id"])))


# ---------------------------------------------------------------------------
#  Одноразовые отметки
# ---------------------------------------------------------------------------
def _purge(svc) -> None:
    svc.db.execute("DELETE FROM sso_state WHERE until < ?", (time.time(),))


def remember_once(svc, kind: str, key: str, ttl_s: float, data: Optional[Dict] = None) -> bool:
    """Запомнить ключ на ``ttl_s`` секунд. False — такой уже был (повтор)."""
    _purge(svc)
    cur = svc.db.execute("INSERT INTO sso_state(kind, key, data, until) VALUES(?,?,?,?) "
                         "ON CONFLICT(kind, key) DO NOTHING",
                         (kind, str(key)[:300], json.dumps(data or {}, ensure_ascii=False), time.time() + ttl_s))
    return bool(int(cur.rowcount or 0))


def take_once(svc, kind: str, key: str) -> Optional[Dict]:
    """Забрать запись (одноразово): данные или None, если её нет или срок вышел."""
    row = svc.db.query_one("SELECT data, until FROM sso_state WHERE kind=? AND key=?", (kind, str(key)[:300]))
    if row is None:
        return None
    cur = svc.db.execute("DELETE FROM sso_state WHERE kind=? AND key=?", (kind, str(key)[:300]))
    if not int(cur.rowcount or 0) or float(row["until"]) < time.time():
        return None
    try:
        return json.loads(row["data"] or "{}")
    except ValueError:
        return None
