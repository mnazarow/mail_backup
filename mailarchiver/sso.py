"""
Общее для единого входа (OpenID Connect с 1.22.0, SAML 2.0 с 1.23.0): учётная запись архива для пользователя
поставщика, одноразовые отметки (защита от повтора ответа поставщика).

Учётная запись заводится сама (``users.auth_source`` — ``oidc`` или ``saml``); локальный пользователь с тем же
логином через единый вход не входит, отключённый администратором — тоже. Пользователь, у которого у поставщика
больше нет групп архива, отключается (``users.disabled_by`` — способ входа) и включается сам, когда права
вернут. Второй фактор — у поставщика: «Обязательный двухфакторный вход» архива к таким входам не применяется.

С 1.25.0 учётная запись привязывается к постоянному идентификатору пользователя у поставщика
(``users.sso_subject``: издатель + ``sub`` OIDC или NameID SAML): тот же логин у другого человека (переименование,
повторная выдача) или у другого поставщика в чужую учётную запись не входит. Запись, которая не входила через
поставщика ``security.sso_inactive_days`` дней, отключается вместе с её API-токенами (:func:`disable_idle`):
отключённый у поставщика сотрудник иначе сохранял бы доступ по токену; войдёт снова — включится.
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
    from .util import utcnow_iso
    label = SOURCE_LABEL.get(source, source)
    username, role = info["username"], info["role"]
    subject = str(info.get("subject") or "")[:500]
    existing = svc.db.get_user_by_name(username)
    if existing is not None and _row_get(existing, "auth_source", "local") not in SSO_SOURCES:
        raise error(f"в архиве уже есть локальный пользователь {username} — через единый вход он не входит")
    bound = _row_get(existing, "sso_subject", "") if existing is not None else ""
    if bound and subject and bound != subject:
        # тот же логин — другой человек у поставщика (или другой поставщик): в чужую учётную запись не пускаем
        svc.db.add_audit("system", "sso_subject_mismatch",
                         f"{username}: вход через {label} другим пользователем поставщика — отказ")
        raise error(f"учётная запись архива {username} привязана к другому пользователю поставщика (логин "
                    f"переименован или выдан заново?) — администратор: удалите её в «Пользователях», при "
                    f"следующем входе она заведётся заново")
    if role not in ("admin", "operator"):
        if existing is not None and not existing["disabled"]:
            svc.db.set_user_disabled(int(existing["id"]), True, by=source)
            svc.db.add_audit("system", "user_disable", f"{username}: у поставщика больше нет прав на архив ({label})")
        return None
    now = utcnow_iso()
    if existing is None:
        uid = svc.db.create_user(username, hash_password(new_token(24)), role)
        svc.db.execute("UPDATE users SET auth_source=?, sso_subject=?, last_sso_at=? WHERE id=?",
                       (source, subject, now, uid))
        svc.db.add_audit("system", "user_create", f"{username}: из единого входа ({label}), роль {role}")
        return dict(svc.db.get_user_by_id(uid))
    if existing["disabled"] and _row_get(existing, "disabled_by", "") not in ("", *SSO_SOURCES):
        raise error(f"{username} отключён администратором")
    if existing["role"] != role or existing["disabled"] or _row_get(existing, "auth_source", "") != source:
        svc.db.execute("UPDATE users SET role=?, disabled=0, disabled_by='', auth_source=? WHERE id=?",
                       (role, source, existing["id"]))
        if existing["role"] != role or existing["disabled"]:
            svc.db.add_audit("system", "user_role", f"{username}: роль из единого входа ({label}) — {role}")
    svc.db.execute("UPDATE users SET last_sso_at=?, sso_subject=CASE WHEN sso_subject='' THEN ? ELSE sso_subject END "
                   "WHERE id=?", (now, subject, existing["id"]))
    return dict(svc.db.get_user_by_id(int(existing["id"])))


def idle_days(svc) -> int:
    try:
        return max(0, int(svc.rt("security", "sso_inactive_days") or 0))
    except (TypeError, ValueError):
        return 0


def idle_cutoff(svc) -> str:
    """Время (ISO, UTC), раньше которого последний вход через поставщика — «давно»; '' — проверка выключена."""
    from datetime import datetime, timedelta, timezone
    days = idle_days(svc)
    if not days:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def is_idle(svc, user) -> bool:
    """Учётная запись единого входа давно не входила через поставщика (её токены не действуют)."""
    from .web.auth import _row_get
    if _row_get(user, "auth_source", "local") not in SSO_SOURCES:
        return False
    cutoff = idle_cutoff(svc)
    last = _row_get(user, "last_sso_at", "") or _row_get(user, "created_at", "") or ""
    return bool(cutoff) and bool(last) and str(last) < cutoff


def disable_idle(svc) -> int:
    """Раз в час (1.25.0): отключить учётные записи единого входа, не входившие через поставщика
    ``security.sso_inactive_days`` дней, — с сеансами и API-токенами. У поставщика человек мог быть уже
    отключён: без этого его токен действовал бы до своего срока. Войдёт снова — включится сам."""
    cutoff = idle_cutoff(svc)
    if not cutoff:
        return 0
    marks = ",".join("?" * len(SSO_SOURCES))
    rows = svc.db.query(f"SELECT id, username, auth_source, last_sso_at, created_at FROM users WHERE disabled=0 "
                        f"AND auth_source IN ({marks})", SSO_SOURCES)
    n = 0
    for r in rows:
        last = r["last_sso_at"] or r["created_at"] or ""
        if not last or str(last) >= cutoff:
            continue
        if svc.db.set_user_disabled(int(r["id"]), True, by=r["auth_source"]):
            n += 1
            svc.db.add_audit("system", "user_disable",
                             f"{r['username']}: не входил через {SOURCE_LABEL.get(r['auth_source'], '')} больше "
                             f"{idle_days(svc)} дн. — отключён вместе с API-токенами; войдёт снова — включится")
    return n


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
