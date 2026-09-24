"""
Движок восстановления писем из локальной копии обратно на IMAP-сервер.

Источник писем — локальный индекс (БД) + файлы Maildir. Для каждого письма:
  * определяется целевая папка (та же, что была; либо с префиксом; либо одна
    общая папка — по выбору пользователя);
  * при необходимости папка создаётся на сервере;
  * по желанию проверяется дубликат по Message-ID, чтобы не заливать письмо
    повторно (набор уже лежащих в папке Message-ID берётся ОДНИМ запросом на
    папку и дальше сверяется в памяти);
  * письмо добавляется командой APPEND с сохранением флагов и даты получения.

Поддерживается «сухой прогон» (dry-run) — подсчёт без реальной заливки.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from ..errors import (ImapConnectionError, ImapTimeoutError, JobCancelled, MailArchiverError,
                      ValidationError)
from ..logging_setup import get_logger
from ..models import Account
from ..storage import MaildirStore
from .client import ConnectOptions, ImapConnection, ReconnectingSession

log = get_logger("restore")

# Системные флаги, которые сервер назначает сам и НЕ принимает в APPEND.
_NON_SETTABLE_FLAGS = {"\\Recent", "\\*"}
# Разрешённые к установке системные флаги IMAP.
# \Deleted намеренно НЕ входит: иначе восстановленные письма сразу помечаются
# удалёнными и исчезают при ближайшем EXPUNGE.
_SETTABLE_SYSTEM_FLAGS = {"\\Seen", "\\Answered", "\\Flagged", "\\Draft"}


def sanitize_flags_for_append(flags) -> List[str]:
    """
    Оставить только флаги, которые сервер примет в команде APPEND:
    разрешённые системные флаги (\\Seen и т.п.) и пользовательские ключевые
    слова (без обратного слэша). Флаги вроде \\Recent отбрасываются.
    """
    out: List[str] = []
    for fl in flags or ():
        fl = fl.strip()
        if not fl:
            continue
        if fl in _NON_SETTABLE_FLAGS:
            continue
        if fl.startswith("\\") and fl not in _SETTABLE_SYSTEM_FLAGS:
            continue  # неизвестный/системный read-only флаг — пропускаем
        if not fl.isascii() or any(ch in fl for ch in ' (){%*"]'):
            # Флаг IMAP — ASCII-атом. Ключевое слово в cp1251, сохранённое с
            # другого сервера, APPEND не примет и провалит заливку всего письма.
            continue
        out.append(fl)
    return out


ProgressCB = Callable[[int, int, str, int, float], None]
CancelCB = Callable[[], bool]
EventCB = Callable[[str, str], None]

#: Сколько сообщений об отдельных письмах писать в журнал задания за прогон.
MAX_MESSAGE_EVENTS = 100
#: Сколько раз пробовать залить одно письмо, если связь рвётся.
MAX_APPEND_ATTEMPTS = 3

#: Признаки ответа «кончилась квота ящика»: дальше не примется ни одно письмо.
_QUOTA_MARKERS = ("overquota", "over quota", "quota exceeded", "quota exceed", "mailbox is full",
                  "mailbox full", "exceeded storage", "превышен")


def _is_quota_error(exc: BaseException) -> bool:
    text = (getattr(exc, "message", "") or str(exc)).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


def _normalize_message_id(value: str) -> str:
    """Привести Message-ID к сравнимому виду: без пробелов и угловых скобок."""
    mid = (value or "").strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1].strip()
    return mid


class _DuplicateIndex:
    """
    Проверка дублей при заливке — с одним запросом на папку.

    Message-ID писем, уже лежащих в папке, запрашиваются ОДИН раз
    (``FETCH 1:* BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]``) и дальше сверяются в
    памяти. Прежний вариант делал SELECT + SEARCH на каждое письмо: на
    100 000 писем это 200 000 обращений к серверу.

    Сбой получения набора НЕ считается ответом «дублей нет»: папка переводится
    на прежнюю поштучную проверку, а если не работает и она — проверка для
    папки честно помечается недоступной (об этом сообщается в лог/событие).
    """

    def __init__(self, conn: ImapConnection, emit: EventCB) -> None:
        self._conn = conn
        self._emit = emit
        self._known: Dict[str, Set[str]] = {}      # папка -> Message-ID на сервере
        self._appended: Dict[str, Set[str]] = {}   # папка -> залитое в этом прогоне
        self._single_mode: Set[str] = set()        # папки на поштучной проверке
        self._off: Set[str] = set()                # папки, где проверка не работает

    def rebind(self, conn: ImapConnection, folder: Optional[str] = None) -> None:
        """Новое соединение после обрыва. Набор Message-ID папки ``folder`` забываем:
        письмо, на котором оборвалась связь, могло успеть лечь на сервер."""
        self._conn = conn
        if folder is not None:
            self._known.pop(folder, None)

    def remember(self, folder: str, message_id: str) -> None:
        """Запомнить письмо, только что залитое в папку."""
        mid = _normalize_message_id(message_id)
        if mid:
            self._appended.setdefault(folder, set()).add(mid)

    def is_duplicate(self, folder: str, message_id: str) -> Optional[bool]:
        """True — дубль; False — точно не дубль; None — проверить не удалось."""
        mid = _normalize_message_id(message_id)
        if not mid:
            return False
        if mid in self._appended.get(folder, ()):
            return True
        if folder in self._off:
            return None
        known = self._folder_ids(folder)
        if known is not None:
            return mid in known
        # Запасной путь: поштучный поиск по серверу — медленно, но лучше, чем
        # заливать вслепую.
        try:
            self._conn.select(folder, readonly=True)
            return bool(self._conn.search_header_messageid(message_id))
        except (ImapConnectionError, ImapTimeoutError):
            raise
        except MailArchiverError as exc:
            self._off.add(folder)
            self._emit("WARNING", f"Папка «{folder}»: проверка дублей недоступна ({exc.message}). "
                                  f"Письма будут залиты без проверки — возможны повторы.")
            return None

    def _folder_ids(self, folder: str) -> Optional[Set[str]]:
        cached = self._known.get(folder)
        if cached is not None:
            return cached
        if folder in self._single_mode:
            return None
        try:
            raw_ids = self._conn.fetch_existing_message_ids(folder)
        except (ImapConnectionError, ImapTimeoutError):
            raise
        except MailArchiverError as exc:
            self._single_mode.add(folder)
            self._emit("WARNING", f"Папка «{folder}»: не удалось получить список Message-ID одним запросом "
                                  f"({exc.message}). Дубли будут проверяться по одному письму — это медленнее.")
            return None
        ids = {_normalize_message_id(v) for v in raw_ids}
        ids.discard("")
        self._known[folder] = ids
        self._emit("INFO", f"Папка «{folder}»: для проверки дублей получено Message-ID: {len(ids)}.")
        return ids


@dataclass
class RestoreResult:
    restored: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    dup_check_unavailable: int = 0  # залито без работающей проверки дублей
    error_details: List[str] = field(default_factory=list)
    cancelled: bool = False
    dry_run: bool = False
    #: почему восстановление остановлено досрочно (квота ящика и т.п.)
    stopped: str = ""
    reconnects: int = 0

    def add_error(self, text: str) -> None:
        self.errors += 1
        if len(self.error_details) < 200:
            self.error_details.append(text)

    @property
    def status_label(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.errors and self.restored:
            return "partial"
        if self.errors and not self.restored:
            return "failed"
        return "success"


class RestoreEngine:
    def __init__(self, db, store: MaildirStore, options: ConnectOptions) -> None:
        self.db = db
        self.store = store
        self.options = options

    def run(self, account: Account, *, folders: Optional[List[str]] = None,
            target_mode: str = "original", target_folder: str = "",
            target_prefix: str = "", check_duplicates: bool = True,
            dry_run: bool = False, limit: int = 0,
            progress_cb: Optional[ProgressCB] = None, cancel_cb: Optional[CancelCB] = None,
            event_cb: Optional[EventCB] = None) -> RestoreResult:
        result = RestoreResult(dry_run=dry_run)
        started = time.time()

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[restore %s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Восстановление отменено пользователем.")

        # Письма берём из индекса ПОТОКОМ (генератор с постраничным обходом):
        # прежняя выборка list_messages(limit=1 000 000) материализовала весь
        # индекс в память (~190 МБ на 200 000 писем) и молча теряла всё, что
        # не поместилось в миллион самых новых писем.
        result.total = self._count_messages(account.id, folders, limit)
        messages = self._iter_messages(account.id, folders, limit)
        emit("INFO", f"К восстановлению отобрано писем: {result.total}"
                     + (" (сухой прогон)" if dry_run else ""))
        if progress_cb:
            progress_cb(0, result.total, "Подготовка", 0, 0.0)

        if not result.total:
            return result

        per_message_events = [0]

        def emit_msg(level: str, msg: str) -> None:
            per_message_events[0] += 1
            if per_message_events[0] <= MAX_MESSAGE_EVENTS:
                emit(level, msg)
            elif per_message_events[0] == MAX_MESSAGE_EVENTS + 1:
                emit("WARNING", f"Сообщений об отдельных письмах больше {MAX_MESSAGE_EVENTS} — дальше "
                                f"они пишутся только в журнал службы; итог — в конце задания.")
            else:
                log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[restore %s] %s",
                        account.name, msg)

        session = ReconnectingSession(account, self.options, emit, check_cancel)
        session.open()
        try:
            conn = session.conn
            conn.list_folders()
            delimiter = conn.delimiter
            ensured: set = set()
            failed_targets: Dict[str, int] = {}     # папки, которые не удалось создать
            dup_index = _DuplicateIndex(conn, emit)

            done = 0
            bytes_done = 0
            for row in messages:
                check_cancel()
                src_folder = row["folder"]
                target = self._resolve_target(src_folder, delimiter, target_mode, target_folder, target_prefix)

                if target in failed_targets:
                    # Папку создать не удалось — каждое её письмо заново пыталось
                    # бы создать папку (201 команда CREATE на 200 писем).
                    failed_targets[target] += 1
                    result.add_error(f"UID {row['uid']} -> «{target}»: папка назначения недоступна")
                    done += 1
                    continue

                try:
                    raw = self.store.read_message(account.id, row["stored_path"])
                except MailArchiverError as exc:
                    result.add_error(f"{row['stored_path']}: {exc.message}")
                    emit_msg("ERROR", f"Файл копии недоступен: {exc.message}")
                    # письмо обработано (пусть и с ошибкой) — иначе прогресс-бар
                    # не дойдёт до 100 %
                    done += 1
                    continue

                if dry_run:
                    result.restored += 1
                    done += 1
                    if progress_cb and done % 20 == 0:
                        progress_cb(done, result.total, f"Проверка {done}/{result.total}", bytes_done, 0.0)
                    continue

                flags = sanitize_flags_for_append((row["flags"] or "").split(","))
                msg_time = self._iso_to_dt(row["internaldate"])
                outcome = None
                for _attempt in range(MAX_APPEND_ATTEMPTS):
                    try:
                        outcome = self._restore_one(session.conn, dup_index, ensured, failed_targets,
                                                    target, row, raw, flags, msg_time,
                                                    check_duplicates, result, emit_msg)
                        break
                    except (ImapConnectionError, ImapTimeoutError) as exc:
                        # Обрыв: переподключаемся и повторяем ЭТО ЖЕ письмо. Набор
                        # Message-ID папки перечитываем — если APPEND успел лечь на
                        # сервер до обрыва, проверка дублей это увидит.
                        session.reconnect(exc)
                        dup_index.rebind(session.conn, target)
                        ensured.discard(target)
                if outcome is None:
                    # связь рвалась на каждой попытке — письмо не залито
                    result.add_error(f"UID {row['uid']} -> «{target}»: связь с сервером обрывалась "
                                     f"при каждой попытке заливки")
                    emit_msg("ERROR", f"Письмо UID {row['uid']} не залито в «{target}»: связь с "
                                      f"сервером обрывалась при каждой попытке.")
                elif outcome == "restored":
                    result.restored += 1
                    bytes_done += row["size"] or len(raw)
                    session.progressed()
                elif outcome == "quota":
                    result.stopped = ("Ящик на сервере переполнен (превышена квота): дальше сервер не "
                                      "примет ни одного письма.")
                    emit("ERROR", result.stopped + " Восстановление остановлено — освободите место в "
                                                   "ящике или увеличьте квоту и запустите его снова "
                                                   "(с проверкой дублей уже залитое повторно не "
                                                   "зальётся).")
                    break
                elif outcome == "skipped":
                    session.progressed()
                done += 1
                if done > result.total:
                    # индекс пополнился во время прогона: не даём прогрессу
                    # уйти за 100 % и показываем настоящее число
                    result.total = done
                if progress_cb and (done % 10 == 0 or done == result.total):
                    elapsed = max(0.001, time.time() - started)
                    progress_cb(done, result.total, f"Восстановлено {result.restored}/{result.total}",
                                bytes_done, bytes_done / elapsed)
            for folder, count in failed_targets.items():
                if count:
                    emit("ERROR", f"В папку «{folder}» не залито писем: {count} — папку на сервере "
                                  f"не удалось создать.")
        finally:
            result.reconnects = session.reconnects
            session.close()

        emit("INFO", f"Восстановление завершено: залито {result.restored}, пропущено {result.skipped}, "
                     f"ошибок {result.errors}." + (f" Остановлено досрочно: {result.stopped}"
                                                   if result.stopped else ""))
        if result.dup_check_unavailable:
            emit("WARNING", f"Для {result.dup_check_unavailable} писем проверка дублей была недоступна — "
                            f"эти письма залиты без проверки, возможны повторы.")
        if progress_cb:
            progress_cb(result.total, result.total, "Готово", bytes_done, 0.0)
        return result

    def _restore_one(self, conn: ImapConnection, dup_index: "_DuplicateIndex", ensured: set,
                     failed_targets: Dict[str, int], target: str, row, raw: bytes, flags: List[str],
                     msg_time, check_duplicates: bool, result: RestoreResult,
                     emit_msg: EventCB) -> str:
        """Залить одно письмо. Возвращает restored | skipped | error | quota.

        Ошибки соединения пробрасываются — их обрабатывает вызывающий
        (переподключение и повтор этого же письма).
        """
        if target not in ensured:
            try:
                conn.ensure_folder(target)
            except (ImapConnectionError, ImapTimeoutError):
                raise
            except MailArchiverError as exc:
                failed_targets[target] = 0
                result.add_error(f"Папка «{target}»: {exc.message}")
                emit_msg("ERROR", f"Не удалось создать папку «{target}»: {exc.message}. Письма этой "
                                  f"папки пропускаются.")
                return "error"
            ensured.add(target)

        if check_duplicates and not row["message_id"]:
            # У письма нет Message-ID — проверить дубль нечем. Раньше такие
            # письма молча проходили мимо проверки и при повторном запуске
            # восстановления дублировались, и в отчёте об этом не было ни слова.
            result.dup_check_unavailable += 1
        if check_duplicates and row["message_id"]:
            is_dup = dup_index.is_duplicate(target, row["message_id"])
            if is_dup:
                result.skipped += 1
                return "skipped"
            if is_dup is None:
                # Проверить не удалось. Это НЕ «дублей нет»: письмо заливаем
                # (чтобы восстановление не встало), но факт учитываем в итоге.
                result.dup_check_unavailable += 1
        try:
            conn.append(target, raw, flags=flags, msg_time=msg_time)
        except (ImapConnectionError, ImapTimeoutError):
            raise
        except MailArchiverError as exc:
            if _is_quota_error(exc):
                result.add_error(f"UID {row['uid']} -> «{target}»: {exc.message}")
                return "quota"
            result.add_error(f"UID {row['uid']} -> «{target}»: {exc.message}")
            emit_msg("ERROR", f"Ошибка заливки письма: {exc.message}")
            return "error"
        dup_index.remember(target, row["message_id"])
        return "restored"

    # -- helpers -------------------------------------------------------------
    #: Размер страницы при обходе индекса.
    _PAGE = 5000

    @staticmethod
    def _folder_targets(folders: Optional[List[str]]) -> List[Optional[str]]:
        """Список папок без повторов и пустых имён (или [None] — весь ящик).

        Дубликаты обязательно убираем: одна и та же папка, переданная дважды,
        дала бы двойную заливку на сервер при выключенной проверке дублей.
        Пустые имена тоже отбрасываем: list_messages(folder="") вернул бы
        письма ВСЕГО ящика.
        """
        if not folders:
            return [None]
        seen: Set[str] = set()
        out: List[Optional[str]] = []
        for fld in folders:
            if not fld or fld in seen:
                continue
            seen.add(fld)
            out.append(fld)
        return out

    def _count_messages(self, account_id: int, folders: Optional[List[str]], limit: int) -> int:
        total = 0
        for fld in self._folder_targets(folders):
            total += self.db.count_folder_messages(account_id, fld)
        if limit and limit > 0:
            total = min(total, limit)
        return total

    def _iter_messages(self, account_id: int, folders: Optional[List[str]], limit: int):
        """Генератор строк индекса: постранично по ``id``, без материализации.

        Обход именно по первичному ключу, а не по OFFSET. Прежний вариант
        сортировал по ``internaldate DESC``: письма, пришедшие во время
        восстановления, вставали в НАЧАЛО выборки и сдвигали окно — часть писем
        выдавалась повторно (и при снятой галке «Пропускать дубли» заливалась
        на сервер дважды), а при удалении по ретеншну часть терялась.
        """
        sent = 0
        for fld in self._folder_targets(folders):
            last_id = 0
            while True:
                chunk = self.db.messages_after_id(account_id, last_id, folder=fld, limit=self._PAGE)
                if not chunk:
                    break
                for row in chunk:
                    yield row
                    sent += 1
                    if limit and limit > 0 and sent >= limit:
                        return
                last_id = chunk[-1]["id"]
                if len(chunk) < self._PAGE:
                    break

    @staticmethod
    def _resolve_target(src_folder: str, delimiter: str, mode: str, target_folder: str, prefix: str) -> str:
        """Целевая папка на сервере.

        Пустой префикс/имя папки здесь НЕ допускаются: раньше они молча
        сводились к режиму «в исходные папки», то есть архив заливался прямо
        в рабочий INBOX живого ящика. Проверка стоит и в API (start_restore),
        здесь — второй рубеж.
        """
        if mode == "single":
            name = (target_folder or "").strip()
            if not name:
                raise ValidationError("Не указана папка назначения для режима «в одну папку».")
            return name
        if mode == "prefixed":
            pref = (prefix or "").strip()
            if not pref:
                raise ValidationError("Не указан префикс папок для режима «в папки с префиксом».")
            return f"{pref}{delimiter}{src_folder}"
        return src_folder

    @staticmethod
    def _iso_to_dt(iso: str) -> Optional[datetime]:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return None
