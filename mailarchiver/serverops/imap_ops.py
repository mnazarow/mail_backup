"""
Действия с папками и письмами в ящиках по IMAP — для любого почтового сервера.

В ящик входят так же, как копирование: паролем ящика, входом администратора
почты или OAuth2. Без ящика в архиве (и, значит, без пароля) по IMAP войти
нечем — такие ящики пропускаются.

Удаление писем и папок — только при полном архиве:

* сначала ящик копируется заново (на время копирования и удаления он занят
  заданием — другое копирование его не трогает);
* каждое удаляемое письмо должно быть в архиве — в той же папке, с тем же
  UIDVALIDITY и UID, — а его файл должен лежать на диске; хоть одного нет —
  папка не трогается;
* удаляются только эти письма: при расширении UIDPLUS — командой UID EXPUNGE,
  без него — обычным EXPUNGE и только если других писем с отметкой «удалено»
  в папке нет (иначе стёрлись бы и они).

Ящики, которые копируются через FTP Backup Axigen, так проверить нельзя (их
письма в архиве не связаны с номерами IMAP): для них есть очистка через Axigen.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from ..bulk import Param
from ..errors import DiskSpaceError, JobCancelled, MailArchiverError, ValidationError
from ..imap.client import FolderInfo, ImapConnection, folder_matches
from ..logging_setup import get_logger
from ..models import Account, AuthType, JobStatus
from ..util import human_size
from . import base
from . import targets as tg
from .base import Ctx, ServerAction

log = get_logger("serverops.imap")

DELETED = b"\\Deleted"
SEEN = b"\\Seen"

TRASH_NAMES = ("trash", "deleted items", "deleted messages", "deleted", "корзина", "удаленные", "удалённые",
               "удаленные элементы", "удалённые элементы")
JUNK_NAMES = ("junk", "spam", "junk e-mail", "junk email", "bulk mail", "спам", "нежелательная почта",
              "нежелательные")
SENT_NAMES = ("sent", "sent items", "sent messages", "sent mail", "отправленные", "отправленные элементы")
DRAFT_NAMES = ("drafts", "draft", "черновики")
OUTBOX_NAMES = ("outbox", "исходящие")
#: Флаги особых папок (RFC 6154): их не удаляем и не переименовываем.
SPECIAL_FLAGS = ("\\sent", "\\drafts", "\\trash", "\\junk", "\\archive", "\\all", "\\flagged", "\\important")
#: Те же папки по привычному имени — у серверов без SPECIAL-USE (старый Exchange, Courier «INBOX.Sent»).
SPECIAL_NAMES = TRASH_NAMES + JUNK_NAMES + SENT_NAMES + DRAFT_NAMES + OUTBOX_NAMES


# ---------------------------------------------------------------------------
#  Общее
# ---------------------------------------------------------------------------
def split_list(text: str) -> List[str]:
    """Папки списком: по строкам, а если строка одна — через запятую."""
    text = text or ""
    pattern = r"\n+" if "\n" in text.strip() else r"[,;]+"
    out: List[str] = []
    for chunk in re.split(pattern, text):
        item = chunk.strip()
        if item and item not in out:
            out.append(item)
    return out


def leaf(name: str, delimiter: str) -> str:
    if delimiter and delimiter in name:
        return name.rsplit(delimiter, 1)[1]
    return name


def _has_flag(f: FolderInfo, flag: str) -> bool:
    return flag.lower() in [x.strip().lower() for x in f.flags]


def _top_level(f: FolderInfo) -> bool:
    """Папка в корне ящика или прямо во «Входящих» (Courier, Cyrus: «INBOX.Trash»)."""
    d = f.delimiter
    if not d or d not in f.name:
        return True
    return f.name.rsplit(d, 1)[0].upper() == "INBOX"


def special_folder(folders: Sequence[FolderInfo], flag: str, names: Sequence[str],
                   explicit: str = "") -> Tuple[Optional[FolderInfo], str]:
    """Особая папка ящика (корзина, спам) → (папка, почему не найдена).

    Только настоящая особая папка: с отметкой сервера SPECIAL-USE (``\\Trash``) или с привычным именем в
    корне ящика (или прямо во «Входящих»). Указанная явно папка тоже должна быть такой: очистка корзины
    не требует, чтобы архив хранил всё, и не должна становиться «удалением старых писем» из INBOX или из
    «Проекты/Trash».
    """
    live = [f for f in folders if f.selectable]
    if explicit:
        f = next((x for x in live if x.name == explicit), None) or \
            next((x for x in live if x.name.lower() == explicit.lower()), None)
        if f is None:
            return None, f"папки «{explicit}» нет"
        if _has_flag(f, flag) or (_top_level(f) and leaf(f.name, f.delimiter).strip().lower() in names):
            return f, ""
        return None, (f"«{f.name}» — не похожа на эту особую папку (нет отметки сервера {flag}, имя не из привычных "
                      f"или папка вложенная); другие папки очищает «Удалить старые письма (IMAP)»")
    for f in live:
        if _has_flag(f, flag):
            return f, ""
    candidates = [f for f in live if _top_level(f) and leaf(f.name, f.delimiter).strip().lower() in names]
    candidates.sort(key=lambda f: f.name.count(f.delimiter or "/"))
    if candidates:
        return candidates[0], ""
    return None, "папка не найдена"


def is_special(f: FolderInfo) -> bool:
    """«Входящие» и особые папки сервера — по отметке SPECIAL-USE или, у серверов без неё, по имени."""
    return (f.name.upper() == "INBOX" or any(x.strip().lower() in SPECIAL_FLAGS for x in f.flags)
            or (_top_level(f) and leaf(f.name, f.delimiter).strip().lower() in SPECIAL_NAMES))


def has_children(live: Sequence[FolderInfo], f: FolderInfo, delimiter: str, bad_names: Sequence[str] = ()) -> bool:
    """Есть ли у папки вложенные — по полному списку папок (LIST "" "*"), а не отдельным запросом.

    Отдельный LIST по шаблону не годится для имён со знаками «*» и «%» и молча давал «вложенных нет»
    при ошибке — и папка с вложенными удалялась бы. Вложенная папка с именем в неверной кодировке в
    список не попадает — её выдают отметка сервера ``\\HasChildren`` и список таких имён.
    """
    if any(x.strip().lower() == "\\haschildren" for x in f.flags):
        return True
    d = f.delimiter or delimiter
    if not d:
        return False
    prefix = f.name + d
    return any(x.name.startswith(prefix) for x in live if x.name != f.name) or \
        any(str(b).startswith(prefix) for b in bad_names)


def copied_to_archive(svc, acc: Account, folder: FolderInfo) -> bool:
    """Копируется ли папка в архив (настройки ящика и общие «Копировать/Пропускать папки»)."""
    include = list(acc.folder_include or []) + list(svc.rt("backup", "folder_include") or [])
    exclude = list(acc.folder_exclude or []) + list(svc.rt("backup", "folder_exclude") or [])
    if include and not folder_matches(folder.name, include, folder.delimiter):
        return False
    if exclude and folder_matches(folder.name, exclude, folder.delimiter):
        return False
    return True


def connect(ctx: Ctx, acc: Account) -> ImapConnection:
    conn = ImapConnection(acc, ctx.svc.connect_options())
    conn.connect()
    return conn


def cutoff(days: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).date()


def older_than(conn: ImapConnection, days: int, extra: Sequence = ()) -> List[int]:
    """UID писем открытой папки старше ``days`` дней (0 — все)."""
    criteria: List = list(extra)
    if days and int(days) > 0:
        criteria += ["BEFORE", cutoff(days)]
    return conn.search(criteria or ["ALL"])


def missing_in_archive(ctx: Ctx, acc: Account, folder: str, uidvalidity: int, uids: Sequence[int]) -> List[int]:
    """UID писем, которых нет в архиве (или файл письма пропал с диска).

    Без настоящего UIDVALIDITY (сервер его не сообщил — в архиве 0) письмо нельзя опознать по номеру:
    тогда «нет в архиве» все.
    """
    if int(uidvalidity or 0) <= 0:
        return list(uids)
    rows = ctx.db.query("SELECT uid, stored_path FROM messages WHERE account_id=? AND folder=? AND uidvalidity=?",
                        (acc.id, folder, int(uidvalidity)))
    paths = {int(r["uid"]): r["stored_path"] for r in rows}
    store = ctx.svc.store
    missing = []
    for uid in uids:
        rel = paths.get(int(uid))
        if not rel:
            missing.append(uid)
            continue
        try:
            if not os.path.isfile(store.message_path(acc.id, rel)):
                missing.append(uid)
        except MailArchiverError:
            missing.append(uid)
    return missing


def remove_messages(conn: ImapConnection, uids: List[int]) -> Tuple[int, str]:
    """Стереть письма открытой (на запись) папки. → (сколько стёрто, почему не стёрто)."""
    if not uids:
        return 0, ""
    uidplus = conn.has_capability("UIDPLUS")
    marked_before: set = set()
    if not uidplus:
        deleted = set(conn.search(["DELETED"]))
        marked_before = deleted & set(uids)
        others = deleted - set(uids)
        if others:
            return 0, (f"в папке есть другие письма с отметкой «удалено» ({len(others)}), а сервер не умеет стирать "
                       f"выборочно (нет UIDPLUS) — стёрлись бы и они; папка не тронута")
    conn.add_flags(uids, [DELETED])
    if uidplus:
        conn.expunge_uids(uids)
    else:
        # ещё раз прямо перед EXPUNGE: другой клиент мог пометить «удалено» чужое письмо
        others = set(conn.search(["DELETED"])) - set(uids)
        if others:
            # снимаем только свою отметку: письма, которые сотрудник пометил «удалено» сам, остаются помеченными
            ours = [u for u in uids if u not in marked_before]
            if ours:
                conn.remove_flags(ours, [DELETED])
            return 0, (f"пока шло удаление, в папке появились другие письма с отметкой «удалено» ({len(others)}), "
                       f"а сервер не умеет стирать выборочно (нет UIDPLUS) — отметка снята, папка не тронута")
        conn.expunge_all()
    left = conn.uids_present(uids)
    if left is None:
        return len(uids), "проверить, что письма стёрты, не удалось"
    if left:
        return len(uids) - len(left), f"сервер не стёр {len(left)} писем"
    return len(uids), ""


class _Hold:
    """Занять ящик заданием: пока идут копия и удаление, другое копирование его не трогает."""

    def __init__(self, ctx: Ctx, acc: Account) -> None:
        from ..axigen.jobs import _Acquire
        self.inner = _Acquire(ctx.job, acc.id) if ctx.job is not None else None

    def __enter__(self):
        if self.inner is not None:
            self.inner.__enter__()
        return self

    def __exit__(self, *exc):
        if self.inner is not None:
            self.inner.__exit__(*exc)


def fresh_copy(ctx: Ctx, acc: Account, emit) -> Tuple[bool, str]:
    """Скопировать ящик сейчас (внутри _Hold). → (удалось, почему нет)."""
    from ..queue.jobs import _backup_account
    try:
        res = _backup_account(ctx.job, acc, progress_cb=lambda *a, **k: None,
                              event_cb=lambda level, text: emit(level, text) if level != "INFO" else None)
    except JobCancelled:
        raise
    except MailArchiverError as exc:
        return False, exc.message
    if res.get("final_status") not in (JobStatus.SUCCESS, JobStatus.PARTIAL):
        return False, (res.get("summary") or "копия не удалась")[:300]
    return True, ""


def _emit(ctx: Ctx, t: tg.Target):
    def emit(level: str, text: str) -> None:
        if ctx.job is not None:
            ctx.job.event(level, f"{t.email}: {text}"[:600])
    return emit


def _cancelled(ctx: Ctx) -> None:
    if ctx.job is not None and ctx.job.is_cancelled():
        raise JobCancelled("Задание отменено.")


def _done(ctx: Ctx, t: tg.Target, action_key: str, text: str) -> None:
    """Удаление на сервере — сразу в журнал задания и в аудит, по папке.

    Не дожидаясь итога по ящику: если следующая папка упадёт или задание отменят, уже сделанное
    всё равно останется записанным.
    """
    if ctx.job is not None:
        ctx.job.event("INFO", f"{t.email}: {text}"[:600])
    ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + action_key, f"{t.email}: {text}"[:300])


FTP_REASON = ("ящик копируется через FTP Backup Axigen — письма в архиве не связаны с номерами IMAP; удаляйте "
              "действием «Удалить старые письма (Axigen)» (в нём можно указать папки, например Trash)")


class _ImapAction(ServerAction):
    needs = "imap"
    #: действие удаляет письма или папки (нужен полный архив)
    deletes = False


class _DeleteAction(_ImapAction):
    """Общее у удаления писем: условия, свежая копия, сверка с архивом и удаление по папкам."""

    group = "cleanup"
    danger = 2
    deletes = True
    require_keep_all = False

    def base_check(self, ctx, t):
        # Причина «FTP Backup» — раньше «нет пароля для IMAP»: с паролем удалять по IMAP всё равно нельзя,
        # и подсказка о действии через Axigen нужнее.
        if t.mailbox is not None and t.mailbox.auth_type == AuthType.AXIGEN_FTP:
            return FTP_REASON
        return super().base_check(ctx, t)

    def check(self, ctx, t):
        acc = t.mailbox
        if self.require_keep_all:
            keeps, why = ctx.keeps(acc)
            if not keeps:
                return why
        # Другое задание по ящику (копирование) — не повод пропускать: задание дождётся его
        # (ящик занимается на время копии и удаления).
        return ""

    def folders_for(self, ctx, conn, acc) -> Tuple[List[FolderInfo], List[str]]:
        """Папки для удаления и что не найдено."""
        raise NotImplementedError

    def execute(self, ctx, t):
        acc = t.mailbox
        emit = _emit(ctx, t)
        days = int(ctx.params.get("days") or 0)
        what = f"старше {days} дн." if days else "все"
        done, skipped = [], []
        total = 0

        def summary() -> str:
            parts = []
            if done:
                parts.append(f"удалено писем ({what}, все были в архиве): {total} — " + ", ".join(done))
            if skipped:
                parts.append("пропущено: " + "; ".join(skipped))
            return "; ".join(parts) or "удалять нечего"

        with _Hold(ctx, acc):
            ok, why = fresh_copy(ctx, acc, emit)
            if not ok:
                return "skip", f"свежая копия не удалась — с сервера ничего не удалено: {why}"
            acc = ctx.db.get_account(acc.id) or acc
            conn = connect(ctx, acc)
            try:
                folders, notes = self.folders_for(ctx, conn, acc)
                skipped.extend(notes)
                for f in folders:
                    _cancelled(ctx)
                    if not copied_to_archive(ctx.svc, acc, f):
                        skipped.append(f"«{f.name}»: папка не копируется в архив (исключена в настройках) — не "
                                       f"тронута")
                        continue
                    info = conn.select(f.name, readonly=False)
                    uids = older_than(conn, days)
                    if not uids:
                        continue
                    missing = missing_in_archive(ctx, acc, f.name, info["uidvalidity"], uids)
                    if missing:
                        skipped.append(f"«{f.name}»: {len(missing)} из {len(uids)} писем нет в архиве (пришли после "
                                       f"копии, больше лимита размера или файл пропал) — папка не тронута")
                        continue
                    count, problem = remove_messages(conn, uids)
                    total += count
                    if count:
                        done.append(f"{f.name}: {count}")
                        _done(ctx, t, self.key, f"«{f.name}»: удалено писем ({what}, все были в архиве): {count}")
                    if problem:
                        skipped.append(f"«{f.name}»: {problem}")
            except (JobCancelled, DiskSpaceError):
                raise
            except MailArchiverError as exc:
                if not done:
                    raise
                # часть папок уже очищена — это должно остаться в итоге, а не пропасть вместе с ошибкой
                return "fail", f"{summary()}; затем ошибка: {exc.message}"
            finally:
                conn.close()
        return ("ok" if done or not skipped else "skip"), summary()


class TrashAction(_DeleteAction):
    key = "imap_trash"
    label = "Очистить «Корзину»"
    icon = "🗑️"
    flag = "\\Trash"
    names = TRASH_NAMES
    what = "корзины"
    desc = ("Удалить из «Корзины» ящиков письма (все или старше срока) — только если каждое из них есть в архиве. "
            "Сначала ящик копируется заново; если хоть одного письма корзины нет в архиве (например, корзина "
            "исключена из копирования), корзина не трогается. Папка находится по отметке сервера (\\Trash) или по "
            "имени в корне ящика: Trash, Deleted Items, Корзина, Удалённые.")
    note = ("Письма корзины, удалённые с сервера, остаются в архиве (на срок хранения ящика) — их можно вернуть "
            "«Восстановить на сервер».")
    params = (
        Param("days", "Удалить письма старше, дней", "int", 30, min=0, max=36500, help="0 — все письма корзины."),
        Param("folder", "Папка", "text", "", placeholder="пусто — найти самой",
              help="Только сама корзина: с отметкой сервера \\Trash или с привычным именем в корне ящика. "
                   "Другие папки очищает «Удалить старые письма (IMAP)»."),
    )

    def plan(self, ctx, t):
        days = int(ctx.params.get("days") or 0)
        folder = ctx.params.get("folder") or f"папка {self.what} (найдётся на сервере)"
        return (f"свежая копия; {folder}: удалятся письма {f'старше {days} дн.' if days else 'все'}, если все они "
                f"есть в архиве")

    def folders_for(self, ctx, conn, acc):
        found, why = special_folder(conn.list_folders(), self.flag, self.names, ctx.params.get("folder") or "")
        if found is None:
            return [], [f"папка {self.what}: {why}"]
        return [found], []


class JunkAction(TrashAction):
    key = "imap_junk"
    label = "Очистить «Спам»"
    icon = "🚫"
    flag = "\\Junk"
    names = JUNK_NAMES
    what = "спама"
    desc = ("Удалить из папки «Спам» ящиков письма (все или старше срока) — только если каждое из них есть в "
            "архиве. Сначала ящик копируется заново; если хоть одного письма папки нет в архиве (например, спам "
            "исключён из копирования), папка не трогается. Папка находится по отметке сервера (\\Junk) или по "
            "имени в корне ящика: Junk, Spam, Спам, Нежелательная почта.")
    note = "Если «Спам» не копируется в архив, очистка его пропустит — это и есть защита «только при полном архиве»."
    params = (
        Param("days", "Удалить письма старше, дней", "int", 30, min=0, max=36500, help="0 — все письма папки."),
        Param("folder", "Папка", "text", "", placeholder="пусто — найти самой",
              help="Только сама папка спама: с отметкой сервера \\Junk или с привычным именем в корне ящика."),
    )


class PurgeOldAction(_DeleteAction):
    key = "imap_purge"
    label = "Удалить старые письма (IMAP)"
    icon = "🧹"
    require_keep_all = True
    desc = ("Освободить место на почтовом сервере: удалить в папках ящиков письма старше срока — только если каждое "
            "из них есть в архиве. Сначала ящик копируется заново, затем каждая папка сверяется с архивом; хоть "
            "одного письма нет — папка не трогается. Только для ящиков, архив которых хранит всё («хранить всё» "
            "или бессрочное удержание): иначе письма со временем пропали бы и из архива.")
    note = "Удалённое с сервера вернуть можно только из архива («Восстановить на сервер»)."
    params = (
        Param("days", "Удалить письма старше, дней", "int", 365, min=30, max=36500),
        Param("folders", "Папки", "textarea", "", placeholder="пусто — все папки, которые копируются в архив",
              help="По одной в строке, как на сервере (INBOX, Sent, Архив/2020). Вложенные папки указывайте отдельно."),
    )

    def plan(self, ctx, t):
        folders = ", ".join(split_list(ctx.params.get("folders") or "")) or "все копируемые папки"
        return f"свежая копия, сверка с архивом; удалятся письма старше {ctx.params.get('days')} дн.: {folders}"

    def folders_for(self, ctx, conn, acc):
        live = [f for f in conn.list_folders() if f.selectable]
        wanted = split_list(ctx.params.get("folders") or "")
        if not wanted:
            return live, []
        out, notes = [], []
        for name in wanted:
            f = _find_name(live, name, conn.delimiter)
            if f is None:
                notes.append(f"«{name}»: такой папки нет")
            elif f not in out:
                out.append(f)
        return out, notes


# ---------------------------------------------------------------------------
#  Папки и письма (без удаления данных)
# ---------------------------------------------------------------------------
def _find(live: Sequence[FolderInfo], name: str) -> Optional[FolderInfo]:
    return next((x for x in live if x.name == name), None) or \
        next((x for x in live if x.name.lower() == name.lower()), None)


def _server_name(name: str, delimiter: str) -> str:
    """Путь «Архив/2024» — с разделителем сервера («Архив.2024» на серверах с точкой)."""
    if delimiter and delimiter != "/":
        return name.replace("/", delimiter)
    return name


def _find_name(live: Sequence[FolderInfo], name: str, delimiter: str) -> Optional[FolderInfo]:
    """Папка по имени, как его ввели: как есть на сервере или «Архив/2020» с разделителем сервера."""
    return _find(live, name) or _find(live, _server_name(name, delimiter or "/"))


class MoveOldAction(_ImapAction):
    key = "imap_move"
    label = "Перенести старые письма в папку"
    group = "folders"
    icon = "📦"
    danger = 1
    desc = ("Разгрузить «Входящие»: переложить письма старше срока в другую папку того же ящика — например, "
            "«Архив» или «Архив/{year}» (по году письма). Письма остаются на сервере; нужная папка создаётся.")
    note = ("Архив увидит перенесённые письма в новой папке как новые и скопирует их ещё раз (старые копии не "
            "удаляются) — архив вырастет на их объём.")
    params = (
        Param("folders", "Из папок", "text", "INBOX", help="Через запятую; уровни — через «/»."),
        Param("days", "Письма старше, дней", "int", 365, min=1, max=36500),
        Param("target", "В папку", "text", "Архив/{year}",
              help="{year} — год письма. Уровни папок — через «/» (заменится разделителем сервера)."),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        target = (ctx.params.get("target") or "").strip()
        if not target:
            raise ValidationError("Укажите папку, куда переносить.")
        if target.upper() == "INBOX":
            raise ValidationError("Переносить во «Входящие» (INBOX) нельзя.")
        if not split_list(ctx.params.get("folders") or ""):
            raise ValidationError("Укажите, из каких папок переносить.")

    def plan(self, ctx, t):
        return (f"письма старше {ctx.params.get('days')} дн. из {', '.join(split_list(ctx.params['folders']))} → "
                f"«{ctx.params.get('target')}»")

    def execute(self, ctx, t):
        days = int(ctx.params.get("days") or 1)
        template = (ctx.params.get("target") or "").strip()
        conn = connect(ctx, t.mailbox)
        try:
            live = [f for f in conn.list_folders() if f.selectable]
            delim = conn.delimiter or "/"
            can_move = conn.has_capability("MOVE")
            uidplus = conn.has_capability("UIDPLUS")
            moved, notes = [], []
            for name in split_list(ctx.params.get("folders") or ""):
                _cancelled(ctx)
                f = _find_name(live, name, delim)
                if f is None:
                    notes.append(f"«{name}»: такой папки нет")
                    continue
                conn.select(f.name, readonly=False)
                uids = older_than(conn, days)
                if not uids:
                    continue
                if not can_move and not uidplus and set(conn.search(["DELETED"])) - set(uids):
                    notes.append(f"«{f.name}»: в папке есть письма с отметкой «удалено», а сервер не умеет MOVE и "
                                 f"UIDPLUS — перенос стёр бы и их; папка не тронута")
                    continue
                groups: Dict[str, List[int]] = {}
                if "{year}" in template:
                    dates = conn.fetch_internaldates(uids)
                    for uid in uids:
                        when = dates.get(uid)
                        year = datetime.fromtimestamp(when, timezone.utc).year if when else "без даты"
                        groups.setdefault(template.replace("{year}", str(year)), []).append(uid)
                else:
                    groups[template] = uids
                for target, part in groups.items():
                    dest = _server_name(target, delim)
                    if dest == f.name:
                        continue
                    conn.ensure_folder(dest)
                    try:
                        conn.subscribe_folder(dest)
                    except MailArchiverError:
                        pass
                    conn.select(f.name, readonly=False)
                    if can_move:
                        conn.move_uids(part, dest)
                    else:
                        conn.copy_uids(part, dest)
                        count, problem = remove_messages(conn, part)
                        if problem:
                            notes.append(f"«{f.name}» → «{dest}»: письма скопированы, но из исходной папки "
                                         f"не стёрты: {problem}")
                    moved.append(f"{f.name} → {dest}: {len(part)}")
                    _done(ctx, t, self.key, f"перенесено писем {f.name} → {dest}: {len(part)}")
        finally:
            conn.close()
        parts = []
        if moved:
            parts.append("перенесено: " + ", ".join(moved))
        if notes:
            parts.append("; ".join(notes))
        return ("ok" if moved or not notes else "skip"), ("; ".join(parts) or "переносить нечего")


class MarkReadAction(_ImapAction):
    key = "imap_seen"
    label = "Пометить прочитанными"
    group = "folders"
    icon = "✔️"
    desc = "Снять с писем отметку «не прочитано» — во всех папках или в указанных, все или старше срока."
    params = (
        Param("folders", "Папки", "text", "", placeholder="пусто — все папки"),
        Param("days", "Только письма старше, дней", "int", 0, min=0, max=36500, help="0 — все непрочитанные."),
    )

    def plan(self, ctx, t):
        folders = ", ".join(split_list(ctx.params.get("folders") or "")) or "все папки"
        days = int(ctx.params.get("days") or 0)
        return f"{folders}: непрочитанные{f' старше {days} дн.' if days else ''} станут прочитанными"

    def execute(self, ctx, t):
        days = int(ctx.params.get("days") or 0)
        conn = connect(ctx, t.mailbox)
        total, per, notes = 0, [], []
        try:
            live = [f for f in conn.list_folders() if f.selectable]
            delim = conn.delimiter or "/"
            wanted = split_list(ctx.params.get("folders") or "")
            folders = live if not wanted else [f for f in (_find_name(live, n, delim) for n in wanted) if f is not None]
            notes = [f"«{n}»: такой папки нет" for n in wanted if _find_name(live, n, delim) is None]
            for f in folders:
                _cancelled(ctx)
                conn.select(f.name, readonly=False)
                uids = older_than(conn, days, ["UNSEEN"])
                if uids:
                    conn.add_flags(uids, [SEEN])
                    total += len(uids)
                    per.append(f"{f.name}: {len(uids)}")
        finally:
            conn.close()
        text = f"помечено прочитанными: {total}" + (f" ({', '.join(per[:20])})" if per else "")
        if notes:
            text += "; " + "; ".join(notes)
        return "ok", text


class CreateFoldersAction(_ImapAction):
    key = "imap_mkdir"
    label = "Создать папки"
    group = "folders"
    icon = "📁"
    desc = "Создать в ящиках одинаковые папки (например, «Проекты/2026»), если их ещё нет, и подписать на них."
    params = (
        Param("folders", "Папки", "textarea", "", placeholder="Проекты\nПроекты/2026",
              help="По одной в строке. Уровни — через «/» (заменится разделителем сервера)."),
        Param("subscribe", "Подписать на новые папки (чтобы их видели почтовые программы)", "bool", True),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        names = split_list(ctx.params.get("folders") or "")
        if not names:
            raise ValidationError("Перечислите папки.")
        for name in names:
            if any(ord(c) < 32 for c in name) or len(name) > 200:
                raise ValidationError(f"Недопустимое имя папки: «{name[:60]}».")

    def plan(self, ctx, t):
        return "будут созданы (если их нет): " + ", ".join(split_list(ctx.params["folders"]))

    def execute(self, ctx, t):
        conn = connect(ctx, t.mailbox)
        created, existed = [], []
        try:
            conn.list_folders()
            delim = conn.delimiter or "/"
            for name in split_list(ctx.params.get("folders") or ""):
                dest = _server_name(name, delim)
                if conn.folder_exists(dest):
                    existed.append(dest)
                    continue
                conn.ensure_folder(dest)
                if ctx.params.get("subscribe", True):
                    try:
                        conn.subscribe_folder(dest)
                    except MailArchiverError:
                        pass
                created.append(dest)
        finally:
            conn.close()
        if not created:
            return "skip", "все папки уже есть"
        text = "созданы: " + ", ".join(created)
        if existed:
            text += f"; уже были: {', '.join(existed)}"
        return "ok", text


class RenameFolderAction(_ImapAction):
    key = "imap_rename"
    label = "Переименовать папку"
    group = "folders"
    icon = "✏️"
    danger = 1
    desc = ("Переименовать папку во всех выбранных ящиках, где она есть. «Входящие» и особые папки (Отправленные, "
            "Корзина…) не переименовываются.")
    note = ("Архив увидит переименованную папку как новую и скопирует её письма ещё раз (под новым именем); копии под "
            "старым именем остаются.")
    params = (
        Param("old", "Папка", "text", "", placeholder="Старое имя"),
        Param("new", "Новое имя", "text", ""),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        old, new = (ctx.params.get("old") or "").strip(), (ctx.params.get("new") or "").strip()
        if not old or not new:
            raise ValidationError("Укажите и прежнее, и новое имя папки.")
        if old.upper() == "INBOX" or new.upper() == "INBOX":
            raise ValidationError("«Входящие» (INBOX) переименовывать нельзя.")
        if old == new:
            raise ValidationError("Новое имя совпадает с прежним.")

    def plan(self, ctx, t):
        return f"«{ctx.params['old']}» → «{ctx.params['new']}» (если папка есть)"

    def execute(self, ctx, t):
        conn = connect(ctx, t.mailbox)
        try:
            live = conn.list_folders()
            delim = conn.delimiter or "/"
            old = _server_name(ctx.params["old"].strip(), delim)
            new = _server_name(ctx.params["new"].strip(), delim)
            f = _find(live, old)
            if f is None:
                return "skip", f"папки «{old}» нет"
            if is_special(f):
                return "skip", f"«{f.name}» — особая папка сервера, её не переименовываем"
            if _find(live, new) is not None:
                return "skip", f"папка «{new}» уже есть"
            conn.rename_folder(f.name, new)
            if not conn.folder_exists(new):
                return "fail", f"сервер принял команду, но папки «{new}» нет"
            try:
                conn.subscribe_folder(new)
            except MailArchiverError:
                pass
        finally:
            conn.close()
        return "ok", f"«{f.name}» → «{new}»"


class DeleteFolderAction(_ImapAction):
    key = "imap_rmdir"
    label = "Удалить папки"
    group = "cleanup"
    icon = "🗂️"
    danger = 2
    deletes = True
    desc = ("Удалить папки в ящиках: пустые — сразу; с письмами — только если включено «удалять и с письмами» и "
            "каждое письмо папки есть в архиве (сначала ящик копируется заново, архив должен хранить всё): "
            "стираются ровно проверенные письма, и папка удаляется, только если после этого пуста. «Входящие», "
            "особые папки (Отправленные, Черновики, Корзина, Спам — по отметке сервера или по имени) и папки с "
            "вложенными папками не удаляются.")
    params = (
        Param("folders", "Папки", "textarea", "", placeholder="Старые проекты\nРассылки/2019",
              help="По одной в строке. Уровни — через «/»."),
        Param("with_mail", "Удалять и папки с письмами (если все письма в архиве)", "bool", False),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        if not split_list(ctx.params.get("folders") or ""):
            raise ValidationError("Перечислите папки.")
        if any(n.upper() == "INBOX" for n in split_list(ctx.params["folders"])):
            raise ValidationError("«Входящие» (INBOX) удалять нельзя.")

    def check(self, ctx, t):
        acc = t.mailbox
        if ctx.params.get("with_mail"):
            if acc.auth_type == AuthType.AXIGEN_FTP:
                return ("ящик копируется через FTP Backup — письма в архиве не связаны с номерами IMAP; удалять "
                        "папки с письмами нельзя (пустые можно, сняв отметку)")
            keeps, why = ctx.keeps(acc)
            if not keeps:
                return why
        return ""

    def plan(self, ctx, t):
        how = "пустые, а с письмами — если все письма в архиве (после свежей копии)" if ctx.params.get("with_mail") \
            else "только пустые"
        return f"{', '.join(split_list(ctx.params['folders']))}: {how}"

    def execute(self, ctx, t):
        acc = t.mailbox
        emit = _emit(ctx, t)
        with_mail = bool(ctx.params.get("with_mail"))
        done, removed, skipped = [], [], []

        def summary() -> str:
            parts = []
            if done:
                parts.append("удалены папки: " + ", ".join(done))
            if removed:
                parts.append("удалено писем (все были в архиве): " + ", ".join(removed))
            if skipped:
                parts.append("пропущено: " + "; ".join(skipped))
            return "; ".join(parts) or "удалять нечего"

        with _Hold(ctx, acc):
            if with_mail:
                # свежая копия — до первого удаления (и пустых папок тоже): итог «ничего не удалено»
                # при неудачной копии должен быть правдой
                ok, why = fresh_copy(ctx, acc, emit)
                if not ok:
                    return "skip", f"свежая копия не удалась — ничего не удалено: {why}"
                acc = ctx.db.get_account(acc.id) or acc
            conn = connect(ctx, acc)
            try:
                live = conn.list_folders()
                delim = conn.delimiter or "/"
                for raw in split_list(ctx.params.get("folders") or ""):
                    _cancelled(ctx)
                    f = _find_name(live, raw, delim)
                    if f is None:
                        skipped.append(f"«{raw}»: такой папки нет")
                        continue
                    if is_special(f):
                        skipped.append(f"«{f.name}»: особая папка сервера — не удаляется")
                        continue
                    if has_children(live, f, delim, getattr(conn, "bad_folder_names", ())):
                        skipped.append(f"«{f.name}»: в ней есть вложенные папки — не удаляется")
                        continue
                    if f.selectable:
                        info = conn.select(f.name, readonly=True)
                        uids = conn.search(["ALL"]) if info.get("exists") else []
                        if uids:
                            if not with_mail:
                                skipped.append(f"«{f.name}»: в папке писем {len(uids)} — удаляются только пустые")
                                continue
                            if not copied_to_archive(ctx.svc, acc, f):
                                skipped.append(f"«{f.name}»: папка не копируется в архив — не удаляется")
                                continue
                            missing = missing_in_archive(ctx, acc, f.name, info["uidvalidity"], uids)
                            if missing:
                                skipped.append(f"«{f.name}»: {len(missing)} из {len(uids)} писем нет в архиве — "
                                               f"папка не удалена")
                                continue
                            # DELETE стёр бы и письмо, пришедшее после сверки, — поэтому сначала стираем ровно
                            # проверенные письма, а папку удаляем, только если после этого она пуста
                            again = conn.select(f.name, readonly=False)
                            if int(again.get("uidvalidity") or 0) != int(info.get("uidvalidity") or 0):
                                skipped.append(f"«{f.name}»: папку пересоздали во время проверки — не тронута")
                                continue
                            count, problem = remove_messages(conn, uids)
                            if count:
                                removed.append(f"{f.name}: {count}")
                                _done(ctx, t, self.key, f"«{f.name}»: удалено писем (все были в архиве): {count}")
                            if problem or count != len(uids):
                                skipped.append(f"«{f.name}»: {problem or 'стёрты не все письма'} — папка не удалена")
                                continue
                        # удаляемую папку не держим открытой (часть серверов иначе отказывает)
                        conn.select("INBOX", readonly=True)
                        left = conn.messages_now(f.name)
                        if left != 0:
                            skipped.append(f"«{f.name}»: " + (f"в папке появились новые письма ({left})" if left else
                                                              "сервер не сообщил, пуста ли папка") +
                                           " — папка не удалена")
                            continue
                    conn.delete_folder(f.name)
                    if conn.folder_exists(f.name):
                        skipped.append(f"«{f.name}»: сервер принял команду, но папка осталась")
                        continue
                    done.append(f.name)
                    _done(ctx, t, self.key, f"удалена папка «{f.name}»")
            except (JobCancelled, DiskSpaceError):
                raise
            except MailArchiverError as exc:
                if not done and not removed:
                    raise
                return "fail", f"{summary()}; затем ошибка: {exc.message}"
            finally:
                conn.close()
        return ("ok" if done or removed else "skip"), summary()


class SubscribeAction(_ImapAction):
    key = "imap_subscribe"
    label = "Подписать на все папки"
    group = "folders"
    icon = "🔔"
    desc = ("Подписать ящики на все их папки — тогда почтовые программы (Outlook, Thunderbird, телефоны) покажут "
            "папки, которые «пропали» из списка, хотя на сервере есть.")

    def plan(self, ctx, t):
        return "подписка на папки, на которые ящик ещё не подписан"

    def execute(self, ctx, t):
        conn = connect(ctx, t.mailbox)
        try:
            live = [f for f in conn.list_folders() if f.selectable]
            have = set(conn.subscribed_folders())
            added = []
            for f in live:
                if f.name not in have:
                    conn.subscribe_folder(f.name)
                    added.append(f.name)
        finally:
            conn.close()
        if not added:
            return "skip", "уже подписан на все папки"
        return "ok", f"подписан на папки ({len(added)}): {', '.join(added[:15])}{' …' if len(added) > 15 else ''}"


class LoginCheckAction(_ImapAction):
    key = "imap_login"
    label = "Проверить вход по IMAP"
    group = "reports"
    icon = "🔐"
    read_only = True
    desc = ("Войти в каждый ящик по IMAP и сразу выйти: подходит ли пароль, отвечает ли сервер. Итог — в задании и "
            "в проверке пароля ящика («Почтовые ящики»); у ящиков не на Axigen — и в колонке «Вход».")

    def plan(self, ctx, t):
        return "вход и выход"

    def execute(self, ctx, t):
        from ..imap.client import login_failure_kind
        acc = t.mailbox
        try:
            conn = connect(ctx, acc)
        except MailArchiverError as exc:
            kind = login_failure_kind(exc)
            ctx.db.set_login_status(acc.id, kind, exc.message[:500])
            return "fail", exc.message
        try:
            caps = conn.capabilities()
        finally:
            conn.close()
        ctx.db.set_login_status(acc.id, "ok", "")
        extras = [c for c in ("UIDPLUS", "MOVE", "QUOTA", "SPECIAL-USE") if c in {x.upper() for x in caps}]
        return "ok", "вход удался" + (f"; сервер умеет: {', '.join(extras)}" if extras else "")


base.register(TrashAction(), JunkAction(), PurgeOldAction(), MoveOldAction(), MarkReadAction(),
              CreateFoldersAction(), RenameFolderAction(), DeleteFolderAction(), SubscribeAction(),
              LoginCheckAction())
