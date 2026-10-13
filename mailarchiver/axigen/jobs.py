"""
Задания очереди для Axigen.

* ``axigen_sync`` — сверка учётных записей сервера с архивом (по кнопке или по
  расписанию). По настройке заводит в архиве ящики для новых учётных записей.
* ``axigen_action`` — изменения на сервере для выбранных ящиков (групповые
  действия раздела «Почтовые ящики»): заблокировать или разблокировать вход,
  сменить пароль, удалить старые письма с сервера, удалить учётную запись.
  Ящики обрабатываются по одному; каждое изменение — с проверкой до и после,
  записью в журнал команд и в аудит.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

from ..errors import DiskSpaceError, JobCancelled, MailArchiverError, ValidationError
from ..logging_setup import get_logger
from ..models import Account, AuthType, JobStatus
from ..util import human_size
from . import actions
from . import directory
from . import store as axstore
from .common import AxigenError, load_settings, services_blocked
from .service import logged_session

log = get_logger("axigen.jobs")

ACTIONS = ("block", "unblock", "password", "purge", "delete", "create")
ACTION_LABELS = {"block": "Заблокировать вход на сервере", "unblock": "Разблокировать вход на сервере",
                 "password": "Сменить пароль на сервере", "purge": "Удалить старые письма с сервера",
                 "delete": "Удалить учётную запись на сервере", "create": "Создать учётную запись на сервере"}
#: Сколько ждать, пока освободится ящик, по которому идёт другое задание.
ACQUIRE_WAIT_S = 30 * 60


# ---------------------------------------------------------------------------
#  Сверка
# ---------------------------------------------------------------------------
def handle_sync(ctx) -> Dict:
    svc = ctx.services
    st = load_settings(svc).require()
    who = ctx.params.get("requested_by") or "system"
    ctx.event("INFO", f"Сверка учётных записей сервера Axigen {st.host}…")
    with logged_session(svc, username=who, kind="read", summary="Сверка учётных записей", st=st) as cli:
        res = directory.fetch(cli, st, progress=lambda i, n, label: ctx.progress(i, n, label),
                              cancelled=ctx.is_cancelled)
    for domain, problem in res.failed_domains.items():
        ctx.event("WARNING", f"Домен {domain}: список учётных записей не прочитан ({problem}) — его учётные "
                             f"записи в этой сверке не обновлялись и удалёнными не считаются.")
    changes = axstore.save_snapshot(svc.db, res.accounts, res.domains)
    partial = [a for a in res.accounts if a.get("errors")]
    for acc in partial[:20]:
        ctx.event("WARNING", f"{acc['email']}: прочитано не всё — {acc['errors']}")
    added = []
    if bool(svc.rt("axigen", "sync_add_new")):
        items = add_mailboxes(svc, list(changes["new"]), user="system", event=ctx.event)
        added = [i for i in items if i["status"] == "ok"]
    summary = (f"Сверка с Axigen: доменов {len(res.domains)}, учётных записей {len(res.accounts)}; "
               f"новых {len(changes['new'])}, удалено на сервере {len(changes['gone'])}, "
               f"вернулись {len(changes['back'])}.")
    if added:
        summary += f" Заведено ящиков в архиве: {len(added)}."
    if changes["gone"]:
        ctx.event("WARNING", "Удалены на сервере (архив их писем сохранён): " + ", ".join(changes["gone"][:30]))
    if changes["new"]:
        ctx.event("INFO", "Новые учётные записи: " + ", ".join(changes["new"][:30]))
    try:
        from .monitor import notify_new, problems
        notify_new(svc, problems(svc, st))
    except Exception:  # noqa: BLE001
        log.debug("Не удалось проверить проблемы после сверки", exc_info=True)
    svc.db.add_audit(who, "axigen_sync", summary[:300])
    status = JobStatus.PARTIAL if (res.failed_domains or partial) else JobStatus.SUCCESS
    return {"final_status": status, "summary": summary, "domains": len(res.domains),
            "accounts": len(res.accounts), "new": len(changes["new"]), "gone": len(changes["gone"]),
            "added": len(added), "incomplete": len(partial), "commands": res.commands,
            "problems": [f"Домен {d}: {p}" for d, p in res.failed_domains.items()][:50]}


def mailbox_template(svc) -> Dict:
    """Шаблон новых ящиков — из настроек ящиков сотрудников (срок хранения, папки)."""
    rt = svc.rt
    try:
        return {"port": int(rt("employees", "account_port") or 993),
                "security": str(rt("employees", "account_security") or "ssl"),
                "folder_include": list(rt("employees", "account_folder_include") or []),
                "folder_exclude": list(rt("employees", "account_folder_exclude") or []),
                "retention_days": int(rt("employees", "account_retention_days")
                                      if rt("employees", "account_retention_days") is not None else -1)}
    except (TypeError, ValueError):
        return {}


def add_mailboxes(svc, emails: List[str], *, user: str, event: Optional[Callable[[str, str], None]] = None,
                  auth: Optional[str] = None, enable: bool = True, preview: bool = False) -> List[Dict]:
    """Завести ящики архива для учётных записей сервера (если их ещё нет)."""
    st = load_settings(svc)
    auth = auth or str(svc.rt("axigen", "new_mailbox_auth") or AuthType.AXIGEN_FTP)
    rows = {r["email"]: r for r in axstore.list_accounts(svc.db)}
    existing = directory.match(list(rows.values()), svc.db.list_accounts(), st)
    template = mailbox_template(svc)
    out = []
    for email in emails:
        email = (email or "").strip().lower()
        row = rows.get(email)
        if row is None:
            out.append({"email": email, "status": "skip", "text": "нет в последней сверке"})
            continue
        if row.get("deleted_at"):
            out.append({"email": email, "status": "skip", "text": "удалена на сервере"})
            continue
        if email in existing:
            out.append({"email": email, "status": "skip", "text": f"уже копируется: ящик «{existing[email].name}»"})
            continue
        acc = directory.new_mailbox(st, row, auth=auth, template=template)
        acc.enabled = enable
        if preview:
            out.append({"email": email, "status": "ok", "text": f"будет заведён ящик «{acc.name}»"})
            continue
        acc.id = svc.db.create_account(acc)
        out.append({"email": email, "status": "ok", "text": f"заведён ящик «{acc.name}»", "account_id": acc.id})
        svc.db.add_audit(user, "account_create", f"{acc.name} ({email}) — по учётной записи Axigen")
        if event:
            event("INFO", f"Заведён ящик «{acc.name}» ({email}).")
    if not preview and any(item["status"] == "ok" for item in out):
        try:
            from ..analytics import invalidate_mail_analytics_cache
            invalidate_mail_analytics_cache()
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
#  Изменения на сервере
# ---------------------------------------------------------------------------
def server_row(rows: List[Dict], acc: Account) -> Optional[Dict]:
    login = (acc.username or "").strip().lower()
    for row in rows:
        if row["email"] == login:
            return row
    for row in rows:
        if login in (row.get("aliases") or []):
            return row
    return None


class _Acquire:
    """Занять ящик на время копирования внутри задания (как копирование по очереди)."""

    def __init__(self, ctx, account_id: int) -> None:
        self.ctx = ctx
        self.account_id = account_id
        self.queue = getattr(ctx.services, "queue", None)

    def __enter__(self):
        if self.queue is None:
            return self
        deadline = time.monotonic() + ACQUIRE_WAIT_S
        while not self.queue.acquire_account(self.account_id, self.ctx.job_id):
            if self.ctx.is_cancelled():
                raise JobCancelled("Задание отменено.")
            if time.monotonic() > deadline:
                raise ValidationError("по ящику полчаса идёт другое задание — пропущен")
            time.sleep(5)
        return self

    def __exit__(self, *exc) -> None:
        if self.queue is not None:
            self.queue.release_account(self.account_id, self.ctx.job_id)


def _fresh_copy(ctx, acc: Account, emit) -> Dict:
    """Скопировать ящик сейчас (последняя копия перед изменением на сервере)."""
    from ..queue.jobs import _backup_account
    with _Acquire(ctx, acc.id):
        return _backup_account(ctx, acc, progress_cb=lambda *a, **k: None,
                               event_cb=lambda level, text: emit(level, text) if level != "INFO" else None)


def _copy_complete(res: Dict) -> Tuple[bool, str]:
    status = res.get("final_status")
    if status not in (JobStatus.SUCCESS, JobStatus.PARTIAL):
        return False, f"копия не удалась ({res.get('summary', '')[:200]})"
    if res.get("skipped_folders"):
        return False, f"копия неполная: не прочитаны папки {', '.join(res['skipped_folders'][:5])}"
    if res.get("messages_failed"):
        return False, f"сервер не отдал писем: {res['messages_failed']}"
    return True, ""


def handle_action(ctx) -> Dict:
    svc = ctx.services
    params = ctx.params
    action = params.get("action")
    if action not in ACTIONS or action == "create":
        raise ValidationError(f"Неизвестное действие с сервером Axigen: {action}.")
    st = load_settings(svc).require()
    actions.require_changes_allowed(st)
    who = params.get("requested_by") or "admin"
    options = params.get("options") or {}
    ids = [int(i) for i in params.get("account_ids") or []]
    label = ACTION_LABELS[action]
    ctx.event("INFO", f"{label}: ящиков {len(ids)}.")
    rows = axstore.list_accounts(svc.db)
    results: List[Dict] = []
    stats = {"ok": 0, "skip": 0, "fail": 0}

    def record(acc: Account, status: str, text: str) -> None:
        stats[status] += 1
        # вид строк — как у копирования по очереди: окно задания показывает их таблицей
        results.append({"account_id": acc.id, "name": acc.name, "status": _ITEM_STATUS[status],
                        "detail": text[:500]})
        level = {"ok": "INFO", "skip": "WARNING", "fail": "ERROR"}[status]
        ctx.event(level, f"«{acc.name}»: {text}"[:600])

    def open_cli(summary: str):
        # Сессия CLI — на одно изменение: долгая копия ящика перед ним не держит
        # сессию открытой (сервер мог бы закрыть её по бездействию).
        return logged_session(svc, username=who, kind="write", summary=summary, st=st)

    for index, account_id in enumerate(ids):
        if ctx.is_cancelled():
            raise JobCancelled("Задание отменено.")
        ctx.progress(index, len(ids), f"{index + 1}/{len(ids)}")
        acc = svc.db.get_account(account_id)
        if acc is None:
            continue
        row = server_row(rows, acc)
        if row is None:
            record(acc, "skip", "учётной записи нет в последней сверке с Axigen — запустите сверку")
            continue
        if not directory.is_axigen_mailbox(acc, st):
            record(acc, "skip", f"ящик копируется с другого сервера ({acc.host})")
            continue
        email = row["email"]

        def emit(level: str, text: str, _acc=acc) -> None:
            ctx.event(level, f"«{_acc.name}»: {text}"[:600])

        try:
            status, text = _ACTION_FUNCS[action](ctx, open_cli, acc, row, email, options, who, emit)
        except (JobCancelled, DiskSpaceError):
            raise
        except (AxigenError, MailArchiverError) as exc:
            status, text = "fail", exc.message + (f" ({exc.hint})" if exc.hint else "")
        except Exception as exc:  # noqa: BLE001
            log.exception("Действие Axigen «%s» с ящиком %s упало", action, acc.name)
            status, text = "fail", f"{type(exc).__name__}: {exc}"
        record(acc, status, text)
        if status == "ok":
            svc.db.add_audit(who, f"axigen_{action}", f"{acc.name} ({email}): {text}"[:300])
    ctx.progress(len(ids), len(ids), "Готово")
    summary = (f"{label}: сделано {stats['ok']}, пропущено {stats['skip']}, ошибок {stats['fail']} "
               f"(из {len(ids)}).")
    final = JobStatus.SUCCESS
    if stats["fail"]:
        final = JobStatus.PARTIAL if stats["ok"] else JobStatus.FAILED
    return {"final_status": final, "summary": summary, "ok": stats["ok"], "skipped": stats["skip"],
            "failed": stats["fail"], "items": results[:2000]}


_ITEM_STATUS = {"ok": "success", "skip": "skipped", "fail": "failed"}


def _do_block(ctx, open_cli, acc, row, email, options, who, emit) -> Tuple[str, str]:
    svc = ctx.services
    notes = []
    if options.get("final_copy", True) and acc.enabled:
        res = _fresh_copy(ctx, acc, emit)
        ok, why = _copy_complete(res)
        if not ok:
            return "skip", f"последняя копия перед блокировкой не удалась — вход НЕ заблокирован: {why}"
        notes.append(f"последняя копия: новых писем {res.get('messages_new', 0)}")
    with open_cli(f"Блокировка входа {email}") as cli:
        by_us = bool(row.get("blocked_at"))
        before, after = actions.block(cli, email, keep_receiving=bool(options.get("keep_receiving", True)),
                                      blocked_by_us=by_us)
        already = bool(services_blocked(before, blocked_by_us=by_us)) and before == after
        saved = row.get("saved_services")
        if not services_blocked(before, blocked_by_us=by_us):
            saved = before
        axstore.note_blocked(svc.db, email, saved_services=saved, user=who, services=after)
        if already:
            notes.insert(0, "вход уже был закрыт")
        else:
            notes.insert(0, f"вход закрыт (службы были: {actions.describe_services(before)}; "
                            f"стали: {actions.describe_services(after)})")
        if options.get("random_password"):
            notes.append(_change_password(svc, cli, acc, email))
    if acc.auth_type == AuthType.PASSWORD and acc.enabled:
        notes.append("ящик копируется по IMAP — со входом закрытым копирование не пройдёт; переведите его на "
                     "копирование через FTP Backup")
    return "ok", "; ".join(notes)


def _do_unblock(ctx, open_cli, acc, row, email, options, who, emit) -> Tuple[str, str]:
    svc = ctx.services
    saved = row.get("saved_services")
    with open_cli(f"Разблокировка входа {email}") as cli:
        if not saved:
            current = actions.get_services(cli, email)
            if services_blocked(current) is False:
                axstore.note_services(svc.db, email, current, False)
                return "skip", "вход и так открыт"
            return "fail", ("неизвестно, какие службы были до блокировки (блокировали не из MailArchiver) — "
                            "откройте вход в WebAdmin Axigen")
        after = actions.unblock(cli, email, saved)
    axstore.note_unblocked(svc.db, email, after)
    return "ok", f"вход открыт (службы: {actions.describe_services(after)})"


def _change_password(svc, cli, acc: Account, email: str) -> str:
    password = actions.random_password()
    actions.set_password(cli, email, password)
    if acc.auth_type == AuthType.PASSWORD or acc.password:
        fresh = svc.db.get_account(acc.id) or acc
        fresh.password = password
        svc.db.update_account(fresh, update_password=True, update_oauth_secret=False, update_oauth_token=False)
        svc.db.set_login_status(acc.id, "", "")
        return "пароль сменён на случайный и сохранён в карточке ящика (копирование по IMAP продолжится)"
    return "пароль сменён на случайный (нигде не сохранён: ящик копируется через FTP Backup)"


def _do_password(ctx, open_cli, acc, row, email, options, who, emit) -> Tuple[str, str]:
    with open_cli(f"Смена пароля {email}") as cli:
        return "ok", _change_password(ctx.services, cli, acc, email)


def _retention_without_hold(svc, acc: Account) -> int:
    """Срок хранения ящика без учёта удержания (что будет, когда удержание кончится)."""
    import copy
    from ..queue.jobs import effective_retention_days
    probe = copy.copy(acc)
    probe.hold_until = ""
    return effective_retention_days(svc, probe)


def archive_keeps_everything(svc, acc: Account) -> Tuple[bool, str]:
    """Хранит ли архив ящика все письма навсегда (условие, чтобы удалять их с сервера).

    Временное удержание не подходит: когда оно кончится, очистка по сроку удалит
    из архива письма, которых на сервере уже нет.
    """
    from ..employees import HOLD_FOREVER
    if acc.hold_until == HOLD_FOREVER:
        return True, ""
    keep = _retention_without_hold(svc, acc)
    if keep <= 0:
        return True, ""
    if acc.on_hold():
        return False, (f"архив удерживается только до {acc.hold_until}, а потом хранит последние {keep} дн. — "
                       f"удалённое с сервера пропало бы и из архива. Задайте ящику «хранить всё» или бессрочное "
                       f"удержание")
    return False, (f"архив ящика хранит только последние {keep} дн. — письма старше этого срока не в архиве. "
                   f"Задайте ящику «хранить всё» или бессрочное удержание")


def _do_purge(ctx, open_cli, acc, row, email, options, who, emit) -> Tuple[str, str]:
    svc = ctx.services
    days = int(options.get("days") or 0)
    if days < 30:
        return "fail", "срок — не меньше 30 дней"
    if acc.auth_type != AuthType.AXIGEN_FTP:
        return "skip", "очистка сервера — только для ящиков, которые копируются через FTP Backup (по ним видно, " \
                       "что каждое письмо папки уже в архиве)"
    keeps, why = archive_keeps_everything(svc, acc)
    if not keeps:
        return "skip", why
    res = _fresh_copy(ctx, acc, emit)
    ok, why = _copy_complete(res)
    if not ok:
        return "skip", f"свежая копия не удалась — с сервера ничего не удалено: {why}"
    _local, domain = actions.parse_email(email)
    return _purge_folders(ctx, open_cli, acc, email, domain, days, options, who, emit)


def _purge_folders(ctx, open_cli, acc, email, domain, days, options, who, emit) -> Tuple[str, str]:
    """Очистить папки по одной: сверка папки с архивом — и сразу команда очистки."""
    from .ftpbackup import verify_folder
    svc = ctx.services
    st = load_settings(svc)
    with open_cli(f"Папки к очистке {email}") as cli:
        allowed = actions.purge_allowed_folders(cli, domain)
        server = actions.server_folders(cli, email)
    if allowed is None:
        return "skip", (f"не удалось прочитать, какие папки домену {domain} разрешено очищать (adminLimits → "
                        f"purgeFolders) — ничего не удалено. Разрешите папки в WebAdmin или проверьте «Диагностику»")
    wanted = [f.strip() for f in str(options.get("folders") or "").replace(";", "\n").replace(",", "\n").split("\n")
              if f.strip()]
    if not wanted:
        if not allowed:
            return "skip", (f"в домене {domain} не задано, какие папки разрешено очищать (adminLimits → "
                            f"purgeFolders) — разрешите их в WebAdmin")
        wanted = [name for name in server if any(name.lower() == a.lower() for a in allowed)]
    done, skipped, failed = [], [], []
    recopied = False
    for folder in wanted:
        if ctx.is_cancelled():
            raise JobCancelled("Задание отменено.")
        name = actions.match_server_folder(folder, server)
        if name is None:
            skipped.append(f"«{folder}»: на сервере такой папки нет")
            continue
        if not any(name.lower() == a.lower() for a in allowed):
            skipped.append(f"«{name}»: домен не разрешает очищать эту папку (adminLimits → purgeFolders)")
            continue
        try:
            good, problem, unseen = verify_folder(svc, st, acc, name)
            if not good and unseen and not recopied:
                # в папке есть письма, пришедшие после свежей копии, — докопировать и сверить ещё раз
                recopied = True
                res = _fresh_copy(ctx, acc, emit)
                fresh_ok, why = _copy_complete(res)
                if fresh_ok:
                    good, problem, unseen = verify_folder(svc, st, acc, name)
                else:
                    problem = f"повторная копия не удалась: {why}"
        except (AxigenError, MailArchiverError) as exc:
            failed.append(f"«{name}»: сверка с архивом не удалась ({exc.message}) — папка не очищена")
            continue
        if not good:
            skipped.append(f"«{name}»: {problem}")
            continue
        try:
            with open_cli(f"Очистка папки {name} ({email}) старше {days} дн.") as cli:
                before, after = actions.purge_folder(cli, email, name, days)
        except (AxigenError, MailArchiverError) as exc:
            failed.append(f"«{name}»: {exc.message}")
            continue
        counts = f" (писем было {before}, стало {after})" if before is not None and after is not None else ""
        done.append(f"{name}{counts}")
    parts = []
    if done:
        parts.append(f"удалены письма старше {days} дн. в папках: {', '.join(done)}")
    if failed:
        parts.append("не очищены из-за ошибки: " + "; ".join(failed))
    if skipped:
        parts.append("пропущено: " + "; ".join(skipped))
    text = "; ".join(parts) or "ничего не удалено"
    if failed:
        if done:
            # часть папок уже очищена — это должно остаться в аудите, хотя итог — ошибка
            svc.db.add_audit(who, "axigen_purge", f"{acc.name} ({email}): {text}"[:300])
        return "fail", text
    return ("ok" if done else "skip"), text


def _do_delete(ctx, open_cli, acc, row, email, options, who, emit) -> Tuple[str, str]:
    """Удалить учётную запись — только если архив ящика полон (или администратор принял потери).

    Порядок: вход уже закрыт → приём писем на время удаления останавливается (иначе
    письмо, пришедшее между сверкой и удалением, пропало бы) → бессрочное удержание
    архива → последняя копия → сверка всех папок с архивом → удаление. Отказ или сбой
    на любом шаге до удаления возвращает приём писем и удержание как было.
    """
    from ..accountops import after_hold_set
    from ..employees import HOLD_FOREVER
    from .common import RECEIVE_SERVICES
    svc = ctx.services
    allow_loss = bool(options.get("allow_loss"))
    by_us = bool(row.get("blocked_at"))
    with open_cli(f"Проверка перед удалением {email}") as cli:
        current = actions.get_services(cli, email)
    if services_blocked(current, blocked_by_us=by_us) is not True:
        return "skip", "вход на сервере не закрыт (или по списку служб этого не понять) — сначала «Заблокировать " \
                       "вход на сервере»"
    if acc.auth_type != AuthType.AXIGEN_FTP:
        if not allow_loss:
            return "skip", ("ящик копируется по IMAP — полноту архива перед удалением проверить нельзя. Переведите "
                            "его на копирование через FTP Backup (письма, уже взятые по IMAP, второй раз не "
                            "запишутся) и повторите")
        problem = _imap_last_copy_problem(svc, acc, row)
        if problem:
            return "skip", problem
    elif not options.get("hold", True) or acc.hold_until == HOLD_FOREVER:
        keeps, why = archive_keeps_everything(svc, acc)
        if not keeps and not allow_loss:
            return "skip", f"{why} — учётная запись НЕ удалена"

    notes: List[str] = []
    previous_hold = (acc.hold_until, acc.hold_reason)
    hold_set = False
    receiving = [s for s in (current or []) if s.strip().lower() in RECEIVE_SERVICES]
    stopped = False
    deleted = False
    try:
        if receiving:
            with open_cli(f"Остановка приёма писем {email} на время удаления") as cli:
                actions.set_services(cli, email, [])
            stopped = True
        if acc.auth_type == AuthType.AXIGEN_FTP:
            if options.get("hold", True) and acc.hold_until != HOLD_FOREVER:
                # удержание — ДО последней копии: со сроком хранения копия не скачала бы старые письма
                svc.db.set_account_hold(acc.id, HOLD_FOREVER, "manual")
                acc = svc.db.get_account(acc.id) or acc
                hold_set = True
            status, text = _final_copy_and_coverage(ctx, acc, emit, allow_loss)
            if status != "ok":
                return status, text
            if text:
                notes.append(text)
        else:
            notes.append("полнота архива не проверялась (ящик копируется по IMAP; так решил администратор)")
        with open_cli(f"Удаление учётной записи {email}") as cli:
            now = actions.get_services(cli, email)
            if services_blocked(now, blocked_by_us=by_us or stopped) is not True:
                return "skip", "пока шла последняя копия, вход на сервере открыли — учётная запись не удалена"
            actions.remove_account(cli, email)
        deleted = True
    finally:
        if not deleted:
            if hold_set:
                svc.db.set_account_hold(acc.id, previous_hold[0], previous_hold[1] or "manual")
            if stopped:
                try:
                    with open_cli(f"Возврат приёма писем {email}") as cli:
                        actions.set_services(cli, email, list(current or []))
                except (AxigenError, MailArchiverError) as exc:
                    emit("ERROR", f"Учётная запись не удалена, но приём писем вернуть не удалось ({exc.message}) — "
                                  f"верните службу {', '.join(receiving)} в WebAdmin Axigen.")
    axstore.note_deleted(svc.db, email)
    notes.insert(0, "учётная запись удалена на сервере")
    if acc.enabled:
        svc.db.set_account_enabled(acc.id, False)
        notes.append("копирование ящика выключено")
    if hold_set:
        after_hold_set(svc, acc.id)
        notes.append("архив поставлен на бессрочное удержание")
    elif options.get("hold", True) and acc.hold_until != HOLD_FOREVER:
        svc.db.set_account_hold(acc.id, HOLD_FOREVER, "manual")
        after_hold_set(svc, acc.id)
        notes.append("архив поставлен на бессрочное удержание")
    stats = svc.db.message_totals_by_account().get(acc.id) or {}
    notes.append(f"в архиве писем: {int(stats.get('messages', 0))} ({human_size(int(stats.get('bytes', 0)))})")
    return "ok", "; ".join(notes)


def _final_copy_and_coverage(ctx, acc: Account, emit, allow_loss: bool) -> Tuple[str, str]:
    """Последняя копия и сверка всех папок с архивом: ("ok", заметка) или ("skip", почему)."""
    from .ftpbackup import describe_gaps, mailbox_coverage
    svc = ctx.services
    st = load_settings(svc)
    cov = None
    for attempt in range(2):
        res = _fresh_copy(ctx, acc, emit)
        ok, why = _copy_complete(res)
        if not ok:
            return "skip", f"последняя копия не удалась — учётная запись НЕ удалена: {why}"
        cov = mailbox_coverage(svc, st, acc)
        # пробел только из писем, которых копия ещё не видела (пришли во время копии), — докопировать раз
        if cov["ok"] or not cov.get("only_unseen"):
            break
    if cov["ok"]:
        return "ok", ""
    gaps = describe_gaps(cov)
    if not allow_loss:
        return "skip", (f"не все письма ящика есть в архиве — учётная запись НЕ удалена: {gaps}. Включите копирование "
                        f"этих папок или, если эти письма не нужны, повторите с «Удалить, даже если часть писем не в "
                        f"архиве»")
    return "ok", f"удалено, хотя в архиве нет части писем (так решил администратор): {gaps}"


def _imap_last_copy_problem(svc, acc: Account, row: Dict) -> str:
    """Для ящика IMAP (полноту не проверить): нужна удачная копия незадолго до блокировки или после неё."""
    from datetime import datetime, timedelta, timezone
    last = svc.db.last_successful_backup_at(acc.id)
    if not last:
        return "у ящика нет ни одной удачной копии — учётная запись не удалена"
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return f"не понять, когда была последняя удачная копия ({last}) — учётная запись не удалена"
    blocked = row.get("blocked_at") or ""
    if blocked:
        try:
            ref = datetime.fromisoformat(str(blocked).replace("Z", "+00:00"))
            if ref.tzinfo is None:
                ref = ref.replace(tzinfo=timezone.utc)
        except ValueError:
            ref = datetime.now(timezone.utc)
    else:
        ref = datetime.now(timezone.utc)
    if last_dt < ref - timedelta(days=1):
        return (f"последняя удачная копия ящика — {str(last)[:16].replace('T', ' ')}, раньше блокировки — "
                f"письма после неё не в архиве; учётная запись не удалена")
    return ""


_ACTION_FUNCS = {"block": _do_block, "unblock": _do_unblock, "password": _do_password, "purge": _do_purge,
                 "delete": _do_delete}


def create_account(svc, *, user: str, email: str, password: str, first_name: str = "", last_name: str = "",
                   add_to_archive: bool = True) -> Dict:
    """Создать учётную запись на сервере (по кнопке в разделе «Сервер Axigen»)."""
    st = load_settings(svc).require()
    actions.require_changes_allowed(st)
    email = (email or "").strip().lower()
    local, domain = actions.parse_email(email)
    if not st.domain_allowed(domain):
        raise ValidationError(f"Домен {domain} не входит в домены из настроек Axigen.")
    actions.check_password(password)
    with logged_session(svc, username=user, kind="write", summary=f"Создание учётной записи {email}", st=st) as cli:
        actions.create_account(cli, email, password, first_name=first_name.strip(), last_name=last_name.strip())
        services = None
        try:
            services = actions.get_services(cli, email)
        except AxigenError:
            pass
    axstore.upsert_created(svc.db, email=email, domain=domain, name=local, first_name=first_name.strip(),
                           last_name=last_name.strip(), services=services)
    svc.db.add_audit(user, "axigen_create", email)
    out = {"ok": True, "email": email}
    if add_to_archive:
        added = add_mailboxes(svc, [email], user=user)
        out["mailbox"] = added[0] if added else None
    return out


def set_account_password(svc, *, user: str, email: str, password: str) -> Dict:
    """Задать учётной записи пароль (по кнопке в карточке учётной записи)."""
    st = load_settings(svc).require()
    actions.require_changes_allowed(st)
    email = (email or "").strip().lower()
    actions.parse_email(email)
    actions.check_password(password)
    with logged_session(svc, username=user, kind="write", summary=f"Пароль учётной записи {email}", st=st) as cli:
        actions.set_password(cli, email, password)
    saved = False
    row = axstore.get_account(svc.db, email)
    for acc in svc.db.list_accounts():
        if row is not None and server_row([row], acc) is not None and directory.is_axigen_mailbox(acc, st) \
                and acc.auth_type == AuthType.PASSWORD:
            acc.password = password
            svc.db.update_account(acc, update_password=True, update_oauth_secret=False, update_oauth_token=False)
            svc.db.set_login_status(acc.id, "", "")
            saved = True
    svc.db.add_audit(user, "axigen_password", email)
    return {"ok": True, "saved_in_mailbox": saved}
