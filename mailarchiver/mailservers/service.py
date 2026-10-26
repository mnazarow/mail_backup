"""
Подключения к почтовым серверам: виды серверов, настройка, сессии работы с API,
проверка, сверка учётных записей и вход в ящики без паролей сотрудников.

Каждая работа с сервером — «сессия» (:func:`session`): драйвер с журналом
вызовов, который по окончании записывается в журнал подключения (кто, что,
успешно ли, все запросы и ответы без секретов).
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type

from ..errors import MailArchiverError, ValidationError
from ..logging_setup import get_logger
from . import store
from .base import Cap, Driver, ImapAuth, ServerError, Status, normalize_account
from .http import ApiLog

log = get_logger("mailservers")

#: Виды серверов: ключ → модуль драйвера (класс ``DRIVER`` в модуле).
DRIVER_MODULES = {
    "m365": "mailarchiver.mailservers.drivers.m365",
    "google": "mailarchiver.mailservers.drivers.google",
    "yandex360": "mailarchiver.mailservers.drivers.yandex360",
    "vkworkspace": "mailarchiver.mailservers.drivers.vkworkspace",
    "zimbra": "mailarchiver.mailservers.drivers.zimbra",
    "communigate": "mailarchiver.mailservers.drivers.communigate",
    "kerio": "mailarchiver.mailservers.drivers.kerio",
    "mdaemon": "mailarchiver.mailservers.drivers.mdaemon",
    "mailcow": "mailarchiver.mailservers.drivers.mailcow",
    "dovecot": "mailarchiver.mailservers.drivers.dovecot",
}
#: Порядок в списке «Подключить сервер».
TYPE_ORDER = ("m365", "google", "yandex360", "vkworkspace", "zimbra", "communigate", "kerio", "mdaemon",
              "mailcow", "dovecot")

#: Ключ цели в разделе «Ящики на сервере»: ms:<номер подключения>:<адрес>
KEY_PREFIX = "ms:"


def target_key(server_id: int, email: str) -> str:
    return f"{KEY_PREFIX}{int(server_id)}:{(email or '').strip().lower()}"


def parse_key(key: str) -> Optional[Tuple[int, str]]:
    if not key.startswith(KEY_PREFIX):
        return None
    rest = key[len(KEY_PREFIX):]
    num, sep, email = rest.partition(":")
    if not sep or not num.isdigit() or "@" not in email:
        return None
    return int(num), email.strip().lower()


def driver_class(type_: str) -> Type[Driver]:
    module = DRIVER_MODULES.get(type_ or "")
    if module is None:
        raise ValidationError(f"Неизвестный вид почтового сервера: «{type_}».")
    try:
        return importlib.import_module(module).DRIVER
    except (ImportError, AttributeError) as exc:
        raise ValidationError(f"Драйвер сервера «{type_}» недоступен: {exc}") from exc


def available_types() -> List[Type[Driver]]:
    out = []
    for type_ in TYPE_ORDER:
        try:
            out.append(driver_class(type_))
        except ValidationError:
            continue
    return out


def describe_types() -> List[Dict[str, Any]]:
    return [cls.describe() for cls in available_types()]


# ---------------------------------------------------------------------------
#  Настройка подключения
# ---------------------------------------------------------------------------
def _split(cls: Type[Driver], values: Dict[str, Any], old: Optional[store.Connection]):
    """Значения формы → (настройки, секреты). Пустой секрет — оставить прежний."""
    settings: Dict[str, Any] = {}
    secrets: Dict[str, str] = dict(old.secrets) if old is not None else {}
    for f in cls.fields:
        raw = values.get(f.key)
        if f.secret:
            if raw not in (None, ""):
                secrets[f.key] = f.coerce(raw)
            continue
        value = f.coerce(raw) if raw is not None else (old.settings.get(f.key) if old is not None else f.default)
        if value is None and f.default is not None:
            value = f.default
        settings[f.key] = value
    # прочее, что драйвер хранит сам (например, отпечаток после «Доверять»), не теряем
    if old is not None:
        for key, value in old.settings.items():
            settings.setdefault(key, value)
    return settings, secrets


def _flag(value: Any) -> bool:
    """Флаг из формы или API: строки «false», «0», «нет» — ложь (а не «непустая строка — истина»)."""
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(value)


#: Смена этих настроек отправила бы сохранённые секреты на другой адрес — секреты нужно ввести заново.
BOUND_KEYS = ("host", "base_url", "port", "tenant_id", "domain", "org_id", "allow_plain", "imap_host", "imap_port",
              "imap_security")


def save(svc, user: str, values: Dict[str, Any], server_id: Optional[int] = None) -> int:
    """Создать или изменить подключение (проверка полей; секреты — зашифрованными)."""
    return save_ex(svc, user, values, server_id)[0]


def save_ex(svc, user: str, values: Dict[str, Any], server_id: Optional[int] = None) -> Tuple[int, List[str]]:
    """Как :func:`save`, но ещё и предупреждения драйвера о настройках (для формы)."""
    old = store.get_server(svc.db, server_id) if server_id else None
    if server_id and old is None:
        raise ValidationError("Подключение не найдено.")
    type_ = old.type if old is not None else str(values.get("type") or "")
    cls = driver_class(type_)
    name = str(values.get("name") or "").strip() or cls.label
    if len(name) > 100:
        raise ValidationError("Название — не длиннее 100 знаков.")
    settings, secrets = _split(cls, values, old)
    if old is not None:
        moved = [k for k in BOUND_KEYS if str(old.settings.get(k) or "") != str(settings.get(k) or "")]
        given = [f.key for f in cls.fields if f.secret and values.get(f.key)]
        missing = [f for f in cls.fields if f.secret and old.secrets.get(f.key) and f.key not in given]
        if moved and missing:
            raise ValidationError(
                f"Меняется адрес подключения ({', '.join(moved)}) — введите заново: "
                f"{', '.join('«' + f.label + '»' for f in missing)}.",
                hint="Сохранённые секреты не уходят на новый адрес без подтверждения.")
    for f in cls.fields:
        if not f.required:
            continue
        if f.show_if and not all(str(settings.get(k)) in [str(v) for v in vals] for k, vals in f.show_if.items()):
            continue
        present = secrets.get(f.key) if f.secret else settings.get(f.key)
        if present in (None, ""):
            raise ValidationError(f"Заполните «{f.label}».")
    warnings = cls.validate_settings(settings, secrets)
    from ..accountops import validate_cron
    cron = str(values.get("sync_cron") or (old.sync_cron if old else "") or "50 4 * * *").strip()
    validate_cron(cron)
    flags = {k: _flag(values.get(k, getattr(old, k) if old is not None else d))
             for k, d in (("enabled", True), ("allow_changes", False), ("sync_enabled", True),
                          ("sync_add_new", False), ("monitor", True))}
    if old is None:
        server_id = store.create_server(svc.db, type=type_, name=name, settings=settings, secrets=secrets,
                                        sync_cron=cron, **flags)
        svc.db.add_audit(user, "mailserver_create", f"{cls.label} «{name}» (№{server_id})")
    else:
        store.update_server(svc.db, server_id, name=name, settings=settings, secrets=secrets, sync_cron=cron,
                            **flags)
        changed = [k for k in flags if flags[k] != getattr(old, k)]
        svc.db.add_audit(user, "mailserver_update", f"{cls.label} «{name}» (№{server_id})"
                         + (f": {', '.join(changed)}" if changed else ""))
    for w in warnings:
        log.warning("Подключение %s: %s", name, w)
    _reload_scheduler(svc)
    return int(server_id), list(warnings or [])


def remove(svc, user: str, server_id: int) -> None:
    conn = store.get_server(svc.db, server_id)
    if conn is None:
        raise ValidationError("Подключение не найдено.")
    from ..models import AuthType
    using = [a.name for a in svc.db.list_accounts()
             if a.auth_type == AuthType.SERVER and getattr(a, "server_id", None) == conn.id]
    if using:
        raise ValidationError(
            f"Через это подключение копируются ящики ({len(using)}): {', '.join(using[:5])}"
            f"{' …' if len(using) > 5 else ''}.",
            hint="Сначала переведите эти ящики на другой способ входа или выключите и удалите их.")
    store.delete_server(svc.db, conn.id)
    svc.db.add_audit(user, "mailserver_delete", f"«{conn.name}» (№{conn.id})")
    _reload_scheduler(svc)


def _reload_scheduler(svc) -> None:
    scheduler = getattr(svc, "scheduler", None)
    if scheduler is not None:
        try:
            scheduler.reload()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  Сессии
# ---------------------------------------------------------------------------
def get(svc, server_id: int) -> store.Connection:
    conn = store.get_server(svc.db, server_id)
    if conn is None:
        raise ValidationError(f"Подключения к почтовому серверу №{server_id} нет.")
    return conn


def driver(svc, conn: store.Connection, api: Optional[ApiLog] = None) -> Driver:
    if conn.secret_broken:
        raise ServerError(f"«{conn.name}»: сохранённые секреты подключения не расшифровываются (сменился "
                          f"secret.key).", hint="Откройте подключение и введите секреты заново.")
    cls = driver_class(conn.type)
    return cls(conn, api or ApiLog(secrets=[v for v in conn.secrets.values() if isinstance(v, str)]))


@contextmanager
def session(svc, conn: store.Connection, *, user: str = "system", kind: str = "read",
            summary: str = "") -> Iterator[Driver]:
    """Работа с сервером: драйвер, а по окончании — запись в журнал подключения."""
    api = ApiLog(secrets=[v for v in conn.secrets.values() if isinstance(v, str)])
    drv = driver(svc, conn, api)
    ok = True
    try:
        yield drv
    except BaseException as exc:
        ok = False
        api.note(f"ошибка: {getattr(exc, 'message', exc)}")
        raise
    finally:
        # сначала выход из сеанса API (его вызов тоже попадает в журнал), потом — журнал; неудачный выход
        # итог работы не портит
        ok = ok and api.ok
        try:
            close = getattr(drv, "close", None)
            if close:
                close()
        except Exception:  # noqa: BLE001
            pass
        if api.calls:
            try:
                store.add_log(svc.db, conn.id, username=user, kind=kind, summary=summary, ok=ok,
                              calls=sum(1 for c in api.calls if c.method != "NOTE"), transcript=api.transcript())
            except Exception:  # noqa: BLE001
                log.warning("Не удалось записать журнал подключения %s", conn.id, exc_info=True)


def check(svc, server_id: int, user: str = "admin", *, kind: str = "check") -> Dict[str, Any]:
    """«Проверить подключение»: связь, права, версия. Итог — в подключение (для мониторинга)."""
    conn = get(svc, server_id)
    seen_fp = ""
    try:
        with session(svc, conn, user=user, kind=kind, summary="Проверка подключения") as drv:
            try:
                status = drv.test()
            finally:
                seen_fp = getattr(drv, "seen_fingerprint", "") or ""
    except MailArchiverError as exc:
        status = Status(ok=False, problems=[("unreachable", "error", exc.message)])
        out = {"ok": False, "error": exc.message, "hint": exc.hint, "fingerprint": seen_fp,
               "status": status.to_dict()}
        store.note_check(svc.db, conn.id, False, status.to_dict())
        return out
    store.note_check(svc.db, conn.id, status.ok, status.to_dict())
    return {"ok": status.ok, "status": status.to_dict(), "fingerprint": seen_fp}


def trust_certificate(svc, server_id: int, fingerprint: str, user: str) -> None:
    from .http import norm_fingerprint
    fp = norm_fingerprint(fingerprint)
    if len(fp) != 64:
        raise ValidationError("Отпечаток SHA-256 — 64 шестнадцатеричных знака.")
    conn = get(svc, server_id)
    store.set_setting(svc.db, conn.id, "cert_sha256", fp)
    svc.db.add_audit(user, "mailserver_trust", f"«{conn.name}»: сертификат SHA-256 {fp}")


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def sync(svc, server_id: int, *, user: str = "system", progress=None, event=None) -> Dict[str, Any]:
    """Прочитать все учётные записи и записать снимок; занятое место — точкой в историю."""
    conn = get(svc, server_id)
    with session(svc, conn, user=user, kind="sync", summary="Сверка учётных записей") as drv:
        raw = drv.list_accounts()
        incomplete = bool(getattr(drv, "incomplete", False))
        problems = list(getattr(drv, "sync_problems", []) or [])
    accounts = [normalize_account(a) for a in raw if a and a.get("email")]
    if not incomplete:
        drop = _suspicious_drop(svc, conn, len(accounts))
        if drop:
            incomplete = True
            problems.append(drop)
    changes = store.save_snapshot(svc.db, conn.id, accounts, complete=not incomplete)
    try:
        from ..serverops.usage import record, today
        day = today(svc)
        for a in accounts:
            if a.get("used_bytes") is not None:
                record(svc.db, target_key(conn.id, a["email"]), a.get("used_bytes"), a.get("quota_bytes"),
                       a.get("messages"), day=day)
    except Exception:  # noqa: BLE001
        log.warning("Не удалось записать занятое место учётных записей в историю", exc_info=True)
    added: List[Dict[str, Any]] = []
    if conn.sync_add_new and changes["new"]:
        from .mailboxes import add_mailboxes
        added = [i for i in add_mailboxes(svc, conn, changes["new"], user="system", event=event)
                 if i["status"] == "ok"]
    summary = (f"{conn.name}: учётных записей {len(accounts)}; новых {len(changes['new'])}, удалено на сервере "
               f"{len(changes['gone'])}, вернулись {len(changes['back'])}.")
    if added:
        summary += f" Заведено ящиков в архиве: {len(added)}."
    if incomplete:
        summary += " Список прочитан не полностью — пропавшие удалёнными не считаются."
        for p in problems:
            if p.startswith(DROP_PREFIX):
                summary += " " + p
    store.note_sync(svc.db, conn.id, "partial" if (incomplete or problems) else "success", summary)
    svc.db.add_audit(user, "mailserver_sync", summary[:300])
    return {"summary": summary, "accounts": len(accounts), "changes": changes, "added": len(added),
            "incomplete": incomplete, "problems": problems}


#: Начало текста проблемы «список резко короче прежнего».
DROP_PREFIX = "Сервер вернул"


def _suspicious_drop(svc, conn: store.Connection, count: int) -> str:
    """Пустой список (или вдвое короче прежнего) — скорее сбой API, чем увольнение половины компании.

    Пропавшие удалёнными не считаются, пока следующая сверка не вернёт столько же: тогда это правда."""
    before = sum(1 for _ in store.list_accounts(svc.db, conn.id, include_deleted=False))
    seen = conn.settings.get("sync_drop_seen")
    if before == 0 or (count > 0 and (before < 10 or count * 2 > before)):
        if seen is not None:
            store.set_setting(svc.db, conn.id, "sync_drop_seen", None)
        return ""
    if seen == count:
        store.set_setting(svc.db, conn.id, "sync_drop_seen", None)
        return ""
    store.set_setting(svc.db, conn.id, "sync_drop_seen", count)
    return (f"{DROP_PREFIX} {count} учётных записей вместо {before} — пропавшие пока не считаются удалёнными; если "
            f"это правда, следующая сверка это подтвердит.")


def refresh_account(svc, conn: store.Connection, email: str, *, user: str = "system") -> Optional[Dict[str, Any]]:
    """Перечитать одну учётную запись с сервера и обновить снимок (None — её нет на сервере)."""
    from .base import norm_email
    email = norm_email(email)
    with session(svc, conn, user=user, kind="read", summary=f"Перечитать {email}") as drv:
        raw = drv.get_account(email)
    if raw is not None and norm_email(raw.get("email")) != email:
        # адрес нашёлся как псевдоним другой учётной записи — самой её на сервере нет
        raw = None
    if raw is None:
        if store.get_account(svc.db, conn.id, email) is not None:
            store.note_deleted(svc.db, conn.id, email)
        return None
    acc = normalize_account(raw)
    store.upsert_account(svc.db, conn.id, acc)
    if acc.get("used_bytes") is not None:
        try:
            from ..serverops.usage import record, today
            record(svc.db, target_key(conn.id, acc["email"]), acc.get("used_bytes"), acc.get("quota_bytes"),
                   acc.get("messages"), day=today(svc))
        except Exception:  # noqa: BLE001
            pass
    return store.get_account(svc.db, conn.id, acc["email"])


# ---------------------------------------------------------------------------
#  Вход в ящики без паролей сотрудников
# ---------------------------------------------------------------------------
def _hostname(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    import urllib.parse
    try:
        return (urllib.parse.urlsplit(raw if "://" in raw else "//" + raw).hostname or "").lower()
    except ValueError:
        return raw.lower()


def imap_target_problem(drv: Driver, host: str, security: str, *, allowed: str = "") -> str:
    """Можно ли отправить секрет подключения (пароль администратора, токен) на этот IMAP-сервер (пусто — можно).

    Только на IMAP-сервер подключения (``allowed`` — адрес, который назвал драйвер) и только с шифрованием
    (без него — лишь если в подключении явно разрешено или сервер на этой же машине)."""
    from .http import _is_loopback
    want = (allowed or drv.imap_server()[0] or "").strip().lower()
    host = (host or "").strip().lower()
    name = drv.conn.name
    if not want:
        return f"у подключения «{name}» не задан IMAP-сервер для копирования ящиков"
    if host != want:
        return f"через подключение «{name}» входят только на {want}, а у ящика указан «{host or '—'}»"
    if (security or "ssl") == "plain" and not (bool(drv.setting("allow_plain")) or _is_loopback(want)):
        return ("IMAP без шифрования: пароль администратора почты (или токен приложения) так не отправляется — "
                "включите SSL/TLS или STARTTLS")
    return ""


def imap_tls_for(svc, account) -> Dict[str, Any]:
    """Проверка сертификата IMAP-сервера для ящика «через подключение» — по настройкам подключения, а не по общей
    настройке: секрет администратора уходит только серверу, которому подключение доверяет. Отпечаток
    (самоподписанный сертификат) годится, если IMAP-сервер — тот же адрес, что и API."""
    from .http import norm_fingerprint
    server_id = getattr(account, "server_id", None)
    conn = store.get_server(svc.db, server_id) if server_id else None
    if conn is None:
        return {"verify": True, "pin": ""}
    drv = driver_class(conn.type)(conn, None)
    imap_host = (drv.imap_server()[0] or "").strip().lower()
    api_host = _hostname(drv.setting("base_url") or drv.setting("host"))
    pin = norm_fingerprint(str(drv.setting("cert_sha256") or "")) if api_host and api_host == imap_host else ""
    verify = drv.setting("verify_tls", True) not in (False, "false", "0", 0)
    return {"verify": verify, "pin": pin}


def imap_auth_for(svc, account) -> ImapAuth:
    """Как войти в ящик архива, который копируется «через подключение к почтовому серверу»."""
    server_id = getattr(account, "server_id", None)
    conn = store.get_server(svc.db, server_id) if server_id else None
    if conn is None:
        raise ServerError(f"Ящик «{account.name}» копируется через подключение к почтовому серверу, а подключения "
                          f"(№{server_id or '—'}) нет.", hint="Выберите подключение в карточке ящика.")
    if not conn.enabled:
        raise ServerError(f"Подключение «{conn.name}» выключено — войти в ящик «{account.name}» нечем.",
                          hint="Включите подключение в разделе «Почтовые серверы».")
    cls = driver_class(conn.type)
    if not cls.can(Cap.IMAP_ADMIN):
        raise ServerError(f"{cls.label}: вход в ящики без паролей сотрудников этот сервер не поддерживает.",
                          hint="Задайте ящику пароль (способ входа «логин и пароль»).")
    drv = driver(svc, conn)
    # секрет администратора (или токен приложения) — только на сервер этого подключения и только шифрованно
    problem = imap_target_problem(drv, account.host, getattr(account, "security", "ssl"))
    if problem:
        raise ServerError(f"Ящик «{account.name}»: {problem}.", hint="Исправьте сервер и шифрование в карточке ящика.")
    auth = drv.imap_auth(account.username)
    if auth.host:
        problem = imap_target_problem(drv, account.host, getattr(account, "security", "ssl"), allowed=auth.host)
        if problem:
            raise ServerError(f"Ящик «{account.name}»: {problem}.", hint="Исправьте сервер ящика.")
    return auth


def imap_problem(svc, account) -> str:
    """Почему в ящик «через подключение» не войти (пусто — можно) — без обращения к серверу."""
    server_id = getattr(account, "server_id", None)
    conn = store.get_server(svc.db, server_id) if server_id else None
    if conn is None:
        return "подключение к почтовому серверу для этого ящика не выбрано (или удалено)"
    if not conn.enabled:
        return f"подключение «{conn.name}» выключено"
    try:
        cls = driver_class(conn.type)
    except ValidationError as exc:
        return exc.message
    if not cls.can(Cap.IMAP_ADMIN):
        return f"{cls.label}: вход в ящики без паролей сотрудников не поддерживается"
    if conn.secret_broken:
        return f"секреты подключения «{conn.name}» не расшифровываются — введите заново"
    try:
        drv = cls(conn, None)
        problem = drv.imap_admin_problem() or imap_target_problem(drv, account.host,
                                                                  getattr(account, "security", "ssl"))
    except Exception as exc:  # noqa: BLE001 — неполные настройки
        problem = getattr(exc, "message", str(exc))
    return f"«{conn.name}»: {problem}" if problem else ""
