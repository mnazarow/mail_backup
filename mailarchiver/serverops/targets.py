"""
Ящики на сервере — то, над чем выполняются групповые действия раздела.

Ящик на сервере бывает двух видов:

* **учётная запись Axigen** (ключ ``ax:<адрес>``) — из последней сверки с
  сервером (таблица ``axigen_accounts``); с ней может быть связан ящик архива
  (по логину или псевдониму);
* **ящик архива на любом сервере** (ключ ``mb:<номер>``) — ящик MailArchiver,
  которому не нашлось учётной записи Axigen (другой сервер или Axigen не
  настроен). С ним работают по IMAP — паролем ящика или входом администратора.

Ключ, а не номер строки: список строится заново при каждой проверке и в
задании, и ящик, который за это время удалили или пересоздали, не подменится
другим.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from ..models import Account, AuthType

KEY_AX = "ax:"
KEY_MB = "mb:"
#: Сколько ящиков принимаем за одну операцию.
MAX_TARGETS = 20000


@dataclass
class Target:
    key: str
    email: str
    name: str
    host: str
    #: строка снимка учётной записи Axigen (None — ящик не на Axigen или Axigen не настроен)
    axigen: Optional[Dict] = None
    #: связанный ящик архива (None — в архиве ящика нет)
    mailbox: Optional[Account] = None

    @property
    def kind(self) -> str:
        return "axigen" if self.axigen is not None else "imap"

    @property
    def deleted(self) -> bool:
        return bool(self.axigen and self.axigen.get("deleted_at"))


def imap_problem(svc, acc: Optional[Account]) -> str:
    """Почему в ящик нельзя войти по IMAP (пусто — можно)."""
    if acc is None:
        return "ящика нет в архиве — войти по IMAP нечем (пароля нет)"
    if acc.secret_broken:
        return "сохранённый пароль ящика не расшифровывается (сменился secret.key) — введите пароль заново"
    if acc.auth_type == AuthType.PASSWORD:
        return "" if acc.password else "у ящика нет сохранённого пароля"
    if acc.auth_type == AuthType.MASTER:
        master = svc.master_credentials()
        if not master:
            return "вход администратора почты не настроен («Настройки → Вход через администратора почты»)"
        if (master.get("host") or "").strip().lower() != (acc.host or "").strip().lower():
            return f"вход администратора почты разрешён только для сервера {master.get('host') or '—'}"
        return ""
    if acc.auth_type == AuthType.OAUTH2:
        return "" if acc.oauth_refresh_token else "у ящика нет токена OAuth2 — войдите заново"
    if acc.auth_type == AuthType.AXIGEN_FTP:
        return "" if acc.password else ("ящик копируется через FTP Backup Axigen без пароля — по IMAP в него не "
                                        "войти; задайте пароль в карточке ящика")
    return f"неизвестный способ входа: {acc.auth_type}"


def _axigen_settings(svc):
    from ..axigen.common import load_settings
    try:
        return load_settings(svc)
    except Exception:  # noqa: BLE001
        return None


def load(svc, *, include_deleted: bool = True) -> List[Target]:
    """Все ящики на сервере: учётные записи Axigen и ящики архива без учётной записи."""
    accounts = svc.db.list_accounts()
    st = _axigen_settings(svc)
    out: List[Target] = []
    linked = set()
    if st is not None and st.enabled:
        from ..axigen import directory
        from ..axigen import store as axstore
        rows = axstore.list_accounts(svc.db)
        matched = directory.match(rows, accounts, st)
        host = st.imap_host or st.host
        for row in rows:
            acc = matched.get(row["email"])
            if acc is not None:
                linked.add(acc.id)
            if row.get("deleted_at") and not include_deleted:
                continue
            full = " ".join(x for x in (row.get("first_name") or "", row.get("last_name") or "") if x).strip()
            out.append(Target(key=KEY_AX + row["email"], email=row["email"], name=full or (acc.name if acc else "")
                              or row["email"], host=host or "", axigen=row, mailbox=acc))
    for acc in accounts:
        if acc.id in linked:
            continue
        out.append(Target(key=f"{KEY_MB}{acc.id}", email=(acc.username or "").strip(), name=acc.name or acc.username,
                          host=acc.host or "", mailbox=acc))
    return out


def resolve(svc, keys: Sequence[str]) -> Dict[str, Target]:
    """Ящики по ключам (свежие данные). Неизвестных ключей в ответе нет."""
    wanted = set(keys)
    return {t.key: t for t in load(svc) if t.key in wanted}


def refresh(svc, t: Target) -> Optional[Target]:
    """Тот же ящик с данными, прочитанными заново (None — его больше нет)."""
    if t.key.startswith(KEY_AX):
        from ..axigen import store as axstore
        row = axstore.get_account(svc.db, t.email)
        if row is None:
            return None
        acc = svc.db.get_account(t.mailbox.id) if t.mailbox is not None else None
        return Target(key=t.key, email=t.email, name=t.name, host=t.host, axigen=row, mailbox=acc)
    acc = svc.db.get_account(int(t.key[len(KEY_MB):]))
    if acc is None:
        return None
    return Target(key=t.key, email=(acc.username or "").strip(), name=acc.name or acc.username, host=acc.host or "",
                  mailbox=acc)


def clean_keys(keys: Optional[Sequence]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in keys or []:
        key = str(raw or "").strip()
        if not key or len(key) > 400 or key in seen:
            continue
        if not (key.startswith(KEY_AX) or key.startswith(KEY_MB)):
            continue
        if key.startswith(KEY_MB) and not key[len(KEY_MB):].isdigit():
            continue
        seen.add(key)
        out.append(key)
    if len(out) > MAX_TARGETS:
        from ..errors import ValidationError
        raise ValidationError(f"Слишком много ящиков за одну операцию (больше {MAX_TARGETS}).")
    return out


AUTH_SHORT = {AuthType.PASSWORD: "по паролю", AuthType.MASTER: "вход администратора почты",
              AuthType.OAUTH2: "OAuth2", AuthType.AXIGEN_FTP: "FTP Backup Axigen"}


def row_dict(svc, t: Target, totals: Dict[int, Dict[str, int]], keeps: Dict[int, bool]) -> Dict:
    """Строка таблицы раздела."""
    ax = t.axigen or {}
    acc = t.mailbox
    item = {"key": t.key, "email": t.email, "name": t.name, "host": t.host, "kind": t.kind,
            "deleted": t.deleted}
    if t.axigen is not None:
        quota = ax.get("quota_kb")
        used = ax.get("used_bytes")
        item.update({
            "login_blocked": ax.get("login_blocked"), "blocked_at": ax.get("blocked_at") or "",
            "saved_services": bool(ax.get("saved_services")), "services": ax.get("services"),
            "quota_kb": quota, "used_bytes": used, "messages_server": ax.get("messages"),
            "quota_percent": (round(100.0 * used / (quota * 1024), 1) if quota and used is not None and quota > 0
                              else None),
            "last_login": ax.get("last_login") or "", "aliases": ax.get("aliases") or [],
            "first_name": ax.get("first_name") or "", "last_name": ax.get("last_name") or "",
            "errors": ax.get("errors") or "", "deleted_at": ax.get("deleted_at") or ""})
    if acc is not None:
        tot = totals.get(acc.id) or {}
        problem = imap_problem(svc, acc)
        item["mailbox"] = {"id": acc.id, "name": acc.name, "enabled": acc.enabled, "auth_type": acc.auth_type,
                           "auth": AUTH_SHORT.get(acc.auth_type, acc.auth_type), "host": acc.host,
                           "imap": not problem, "imap_problem": problem,
                           "messages": int(tot.get("messages", 0)), "bytes": int(tot.get("bytes", 0)),
                           "last_backup_at": acc.last_backup_at, "last_backup_status": acc.last_backup_status,
                           "login_status": acc.login_status, "keeps_all": bool(keeps.get(acc.id)),
                           "hold_until": acc.hold_until, "dismissed_at": acc.dismissed_at}
    else:
        item["mailbox"] = None
    return item


def keeps_everything_map(svc, accounts: Sequence[Account]) -> Dict[int, bool]:
    """Хранит ли архив ящика все письма навсегда (для отбора и подсказок)."""
    from ..axigen.jobs import archive_keeps_everything
    out = {}
    for acc in accounts:
        try:
            out[acc.id] = archive_keeps_everything(svc, acc)[0]
        except Exception:  # noqa: BLE001
            out[acc.id] = False
    return out


def overview(svc) -> Dict:
    """Всё для раздела «Ящики на сервере»: строки таблицы и состояние Axigen."""
    import json as _json
    from ..models import JobType
    items = load(svc)
    accounts = [t.mailbox for t in items if t.mailbox is not None]
    totals = svc.db.message_totals_by_account()
    keeps = keeps_everything_map(svc, accounts)
    rows = [row_dict(svc, t, totals, keeps) for t in items]
    st = _axigen_settings(svc)
    ax = {"enabled": bool(st and st.enabled), "configured": bool(st and st.configured),
          "allow_changes": bool(st and st.allow_changes), "host": (st.host if st else "") or "",
          "last_sync": None, "sync_running": False}
    if st is not None and st.enabled:
        last = svc.db.query_one(
            "SELECT id, status, finished_at, result FROM jobs WHERE type=? AND status IN ('success','partial','failed') "
            "ORDER BY id DESC LIMIT 1", (JobType.AXIGEN_SYNC,))
        if last is not None:
            try:
                summary = (_json.loads(last["result"] or "{}") or {}).get("summary", "")
            except (TypeError, ValueError):
                summary = ""
            ax["last_sync"] = {"job_id": last["id"], "status": last["status"], "finished_at": last["finished_at"],
                               "summary": summary}
        ax["sync_running"] = any(j["type"] == JobType.AXIGEN_SYNC for j in svc.db.active_jobs())
    running = [dict(id=j["id"], status=j["status"]) for j in svc.db.active_jobs() if j["type"] == JobType.SERVER_ACTION]
    return {"rows": rows, "axigen": ax, "running": running,
            "master": svc.master_credentials() is not None}
