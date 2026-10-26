"""
Что требует внимания на подключённых почтовых серверах — и проверка состояния по расписанию.

Проверка (раз в 15 минут для подключений с включённым мониторингом) вызывает
``status()`` драйвера: версия, лицензии, очередь, проблемы, которые видит сам
сервер. Вместе с последней сверкой это даёт список проблем подключения — он
виден в разделе «Почтовые серверы», на дашборде, в /metrics и в еженедельной
сводке; о новой проблеме администраторы получают письмо (не чаще раза в сутки
на каждую).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..logging_setup import get_logger
from . import service, store

log = get_logger("mailservers.monitor")

ALERTED_KEY = "mailservers_alerted"
UNREACHABLE_AFTER = 2
ALERT_REPEAT_HOURS = 24
#: «У предела квоты» — как отбор в разделе «Ящики на сервере».
QUOTA_PERCENT = 90
PROBLEM_KINDS = ("unreachable", "server", "quota", "unarchived", "gone", "blocked_imap", "tls_unverified",
                 "dismissed_open", "quota_soon", "sync")


def _names(items: List[str]) -> str:
    return ", ".join(items[:10]) + (" …" if len(items) > 10 else "")


def problems(svc, server_id: Optional[int] = None) -> List[Dict]:
    """Проблемы подключений: [{key, server_id, kind, level, text}]."""
    out: List[Dict] = []
    conns = [c for c in store.list_servers(svc.db) if c.enabled and (server_id is None or c.id == server_id)]
    if not conns:
        return out
    try:
        from ..serverops import targets as tg
        items = [t for t in tg.load(svc) if t.server is not None]
    except Exception:  # noqa: BLE001
        log.debug("Не удалось собрать учётные записи для проверки", exc_info=True)
        items = []
    try:
        from ..serverops.targets import dismissed_map
        dmap = dismissed_map(svc)
    except Exception:  # noqa: BLE001
        dmap = {}
    try:
        from ..serverops.usage import SOON_DAYS, soon_full
        soon = set(soon_full(svc.db))
    except Exception:  # noqa: BLE001
        SOON_DAYS, soon = 30, set()
    for conn in conns:
        def add(kind: str, level: str, text: str) -> None:
            out.append({"key": f"srv{conn.id}_{kind}", "server_id": conn.id, "kind": kind, "level": level,
                        "text": f"«{conn.name}»: {text}"})
        status = conn.status or {}
        if conn.fails >= UNREACHABLE_AFTER:
            err = next((p[2] for p in status.get("problems") or [] if p and p[0] == "unreachable"), "нет ответа")
            add("unreachable", "error", f"сервер недоступен ({conn.fails} проверок подряд): {err}")
        elif conn.last_check_ok:
            for key, level, text in [tuple(p) for p in status.get("problems") or [] if p and len(p) == 3]:
                if key != "unreachable":
                    add("server", level if level in ("warning", "error") else "warning", text)
        if conn.last_sync_status == "partial":
            add("sync", "warning", f"последняя сверка прочитала не всё: {conn.last_sync_summary}")
        cls = None
        try:
            cls = service.driver_class(conn.type)
        except Exception:  # noqa: BLE001
            pass
        if cls is not None and not cls.cloud and not conn.settings.get("verify_tls", True) \
                and not conn.settings.get("cert_sha256"):
            add("tls_unverified", "warning", "сертификат сервера не проверяется и отпечаток не сохранён — подмену "
                                             "сервера никто не заметит")
        mine = [t for t in items if t.server_id == conn.id]
        live = [t for t in mine if not t.deleted]
        full = [t.email for t in live if t.server.get("quota_kb") and t.server.get("used_bytes") is not None
                and 100.0 * t.server["used_bytes"] / (t.server["quota_kb"] * 1024) >= QUOTA_PERCENT]
        if full:
            add("quota", "warning", f"ящики у предела квоты (≥ {QUOTA_PERCENT} %): {len(full)} — {_names(full)}")
        missing = [t.email for t in live if t.mailbox is None and (t.server.get("kind") or "user") in ("user", "")]
        if missing and conn.settings.get("alert_unarchived", True):
            add("unarchived", "warning", f"учётные записи без копии в архиве: {len(missing)} — {_names(missing)}")
        gone = [t.email for t in mine if t.deleted and t.mailbox is not None and t.mailbox.enabled]
        if gone:
            add("gone", "warning", f"удалены на сервере, а копирование ящика включено: {len(gone)} — {_names(gone)}. "
                                   f"Выключите копирование (архив сохранится).")
        from ..models import AuthType
        stuck = [t.email for t in live if t.server.get("login_blocked") and t.mailbox is not None
                 and t.mailbox.enabled and t.mailbox.auth_type == AuthType.PASSWORD]
        if stuck:
            add("blocked_imap", "warning", f"вход закрыт, а ящик копируется по паролю сотрудника — копирование не "
                                           f"пройдёт: {len(stuck)} — {_names(stuck)}")
        own = set()
        for t in mine:
            own.add(t.email)
            if t.deleted:
                own.update(str(a).lower() for a in t.server.get("aliases") or [])
        still_open = []
        for t in live:
            if t.server.get("login_blocked") is not False:
                continue
            addresses = [t.email] + [a for a in t.server.get("aliases") or [] if str(a).lower() not in own]
            if (t.mailbox is not None and t.mailbox.dismissed_at) or any(dmap.get(str(a).lower()) for a in addresses):
                still_open.append(t.email)
        if still_open:
            add("dismissed_open", "warning", f"сотрудники уволены, а вход на сервере открыт: {len(still_open)} — "
                                             f"{_names(still_open)}. Закрыть вход: «Ящики на сервере» → отбор "
                                             f"«Уволены, вход открыт» → «Заблокировать вход».")
        near = [t.email for t in live if t.key in soon]
        if near:
            add("quota_soon", "warning", f"при нынешнем росте упрутся в квоту за {SOON_DAYS} дн.: {len(near)} — "
                                         f"{_names(near)}")
    return out


def tick(svc) -> int:
    """Проверка по расписанию: состояние каждого подключения с мониторингом, письма о новых проблемах."""
    checked = 0
    for conn in store.list_servers(svc.db, enabled_only=True):
        if not conn.monitor:
            continue
        try:
            service.check(svc, conn.id, user="system", kind="monitor")
            checked += 1
        except Exception:  # noqa: BLE001
            log.warning("Проверка подключения «%s» не удалась", conn.name, exc_info=True)
    if checked:
        notify_new(svc, problems(svc))
    return checked


def notify_new(svc, found: List[Dict]) -> List[str]:
    try:
        alerted = json.loads(svc.db.get_meta(ALERTED_KEY) or "{}")
    except (TypeError, ValueError):
        alerted = {}
    now = datetime.now(timezone.utc)
    fresh = []
    for item in found:
        last = alerted.get(item["key"])
        try:
            when = datetime.fromisoformat(last) if last else None
        except ValueError:
            when = None
        if when is None or now - when >= timedelta(hours=ALERT_REPEAT_HOURS):
            fresh.append(item)
    keys = {item["key"] for item in found}
    alerted = {k: v for k, v in alerted.items() if k in keys}
    sent: List[str] = []
    notifier = getattr(svc, "notifier", None)
    if fresh and notifier is not None and notifier.enabled():
        body = ("Почтовые серверы требуют внимания:\n\n" + "\n".join(f"• {i['text']}" for i in fresh) +
                "\n\nПодробности — в разделе «Почтовые серверы» MailArchiver.")
        try:
            notifier.send("MailArchiver: почтовые серверы требуют внимания", body)
            sent = [i["key"] for i in fresh]
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить письмо о почтовых серверах: %s", exc)
    for key in sent:
        alerted[key] = now.isoformat()
    svc.db.set_meta(ALERTED_KEY, json.dumps(alerted))
    return sent


def metrics(svc) -> List[tuple]:
    conns = [c for c in store.list_servers(svc.db) if c.enabled]
    if not conns:
        return []
    counts = store.counts(svc.db)
    found = problems(svc)
    up, seats, accounts, probs = [], [], [], []
    for conn in conns:
        labels = {"server": conn.name, "type": conn.type}
        if conn.last_check_at:
            up.append((labels, 1 if conn.last_check_ok else 0))
        st = conn.status or {}
        if st.get("seats_total") is not None:
            seats.append((dict(labels, state="total"), st.get("seats_total")))
        if st.get("seats_used") is not None:
            seats.append((dict(labels, state="used"), st.get("seats_used")))
        c = counts.get(conn.id) or {}
        accounts += [(dict(labels, state="total"), c.get("live", 0)), (dict(labels, state="blocked"), c.get("blocked", 0)),
                     (dict(labels, state="deleted"), c.get("total", 0) - c.get("live", 0))]
        mine = {p["kind"] for p in found if p["server_id"] == conn.id}
        probs += [(dict(labels, problem=k), 1 if k in mine else 0) for k in PROBLEM_KINDS]
    return [
        ("mailarchiver_mailserver_up", "Последняя проверка почтового сервера удалась (1/0)", "gauge", up),
        ("mailarchiver_mailserver_licenses", "Лицензии почтового сервера (всего, занято)", "gauge", seats),
        ("mailarchiver_mailserver_accounts", "Учётные записи почтового сервера по последней сверке", "gauge", accounts),
        ("mailarchiver_mailserver_problems", "Что требует внимания на почтовом сервере (1 — есть)", "gauge", probs),
    ]


def summary_lines(svc) -> List[str]:
    lines: List[str] = []
    found = problems(svc)
    for conn in store.list_servers(svc.db):
        if not conn.enabled:
            continue
        try:
            label = service.driver_class(conn.type).label
        except Exception:  # noqa: BLE001
            label = conn.type
        state = "проверок не было"
        if conn.last_check_at:
            state = "доступен" if conn.last_check_ok else "НЕДОСТУПЕН"
        version = (conn.status or {}).get("version") or ""
        lines.append(f"{label} «{conn.name}»: {state}{f', версия {version}' if version else ''}"
                     f"{', сверка ' + conn.last_sync_at[:16].replace('T', ' ') + ' UTC' if conn.last_sync_at else ''}.")
        for item in found:
            if item["server_id"] == conn.id:
                lines.append(f"  • {item['text']}")
    return lines
