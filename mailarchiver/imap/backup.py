"""
Движок резервного копирования почтового ящика по IMAP.

Алгоритм (инкрементальный):
  1. Подключиться к ящику, получить список папок, отфильтровать по правилам.
  2. Фаза планирования: для каждой папки определить, какие письма новые.
     Признак новизны — пара (UIDVALIDITY, UID). Если UIDVALIDITY у папки
     изменился, прежние UID недействительны (так устроен протокол IMAP), и папка
     перекачивается заново — но УЖЕ СКАЧАННОЕ НЕ УДАЛЯЕТСЯ: письма просто
     сохраняются заново под новым UIDVALIDITY (он входит в уникальный ключ
     индекса), а старые записи остаются как исторические. Так обрыв связи или
     «похудевший» после восстановления ящик не уничтожает локальную копию.
  3. Фаза загрузки: скачать новые письма батчами, сохранить в Maildir, занести
     в индекс БД, обновлять прогресс.
  4. Обновить состояние папок и статистику.

Ошибки на уровне отдельной папки (не открылась, сервер отказал в поиске)
и отдельного письма (сервер его не отдаёт, не удалось сохранить) не прерывают
весь бэкап — они учитываются в счётчике ошибок, остальное копируется.

Обрыв связи — не ошибка папки: движок переподключается и продолжает с того
места, где остановился. Сдаётся он, только если переподключения идут одно за
другим без всякого продвижения (сервер лежит) — тогда задание целиком
повторяется очередью позже.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from ..errors import (DiskSpaceError, ImapConnectionError, ImapTimeoutError, JobCancelled,
                      MailArchiverError)
from ..logging_setup import get_logger
from ..models import Account
from ..storage import MaildirStore
from .client import (ConnectOptions, ImapConnection, ReconnectingSession, folder_matches,
                     header_value)

log = get_logger("backup")

ProgressCB = Callable[[int, int, str, int, float], None]
CancelCB = Callable[[], bool]
EventCB = Callable[[str, str], None]  # (level, message)


# Сколько имён пропущенных папок показывать в итоговой строке целиком.
MAX_SKIPPED_FOLDERS_IN_SUMMARY = 10

#: По сколько писем записывать в индекс одной транзакцией.
INDEX_BATCH = 200

#: Сколько сообщений об отдельных письмах писать в журнал задания за прогон.
#: Без предела 5000 одинаковых ошибок превращались в 5000 строк журнала.
MAX_MESSAGE_EVENTS = 100
#: Сколько подробностей об ошибках держать в результате задания.
MAX_ERROR_DETAILS = 200
#: Столько ошибок сохранения подряд в одной папке — и папка откладывается до
#: следующего прогона (раз не пишется ни одно письмо, дело не в письмах).
MAX_CONSECUTIVE_STORE_ERRORS = 20
#: Предел блока заголовков, который разбираем ради темы и отправителя. Полный
#: разбор письма в 120 МБ съедал больше гигабайта памяти.
HEADER_PARSE_LIMIT = 256 * 1024


@dataclass
class BackupResult:
    messages_new: int = 0
    bytes_new: int = 0
    messages_total: int = 0
    messages_skipped: int = 0  # не скачаны: больше лимита размера
    folders_processed: int = 0
    folders_total: int = 0
    folders_read: int = 0      # папок, которые сервер дал открыть (SELECT прошёл)
    errors: int = 0
    error_details: List[str] = field(default_factory=list)
    # Папки, которые не удалось открыть (SELECT/EXAMINE отклонён сервером).
    # Их письма в копию НЕ попали — список нужен, чтобы неполнота копии была
    # видна и в итоге задания, и в его результате, а не только в счётчике ошибок.
    skipped_folders: List[str] = field(default_factory=list)
    # Папки, которые сервер не даёт открыть, но по данным STATUS они ПУСТЫЕ.
    # Терять в них нечего, поэтому ошибкой это не считается и копия остаётся
    # полной — иначе каждый прогон навсегда помечался бы «частично выполнен»
    # из-за битых пустых папок, которые на сервере уже не восстановить.
    empty_unreadable_folders: List[str] = field(default_factory=list)
    # Папки-контейнеры: сервер их не открывает, но у них есть вложенные папки.
    # Своих писем такие папки не хранят (сервер просто забыл пометить их
    # флагом \Noselect), поэтому потерей это не является.
    container_folders: List[str] = field(default_factory=list)
    # Папки, которые не открываются уже несколько прогонов подряд. Ошибкой их
    # больше не считаем (см. BackupEngine.unreadable_grace_runs), но в итоге
    # задания перечисляем — чтобы они не исчезли из виду.
    known_unreadable_folders: List[str] = field(default_factory=list)
    # Письма, которые сервер отказался отдать (FETCH ответил отказом) — потеря.
    messages_failed: int = 0
    # Письма, пропавшие между поиском и загрузкой: удалены на сервере во время
    # копирования. Это не ошибка, но число полезно видеть.
    messages_vanished: int = 0
    # Сколько раз пришлось переподключаться из-за обрыва связи.
    reconnects: int = 0
    cancelled: bool = False

    def add_error(self, text: str) -> None:
        self.errors += 1
        if len(self.error_details) < MAX_ERROR_DETAILS:
            self.error_details.append(text)

    @property
    def status_label(self) -> str:
        if self.cancelled:
            return "cancelled"
        # «Частично» — когда часть работы всё же сделана: что-то скачано ИЛИ
        # хотя бы часть папок прочитана. Второе условие важно для прогона, в
        # котором новых писем не было, но пара папок не открылась: раньше такой
        # прогон объявлялся полным провалом («failed»), хотя остальной ящик
        # проверен успешно.
        if self.errors and (self.messages_new or self.folders_read):
            return "partial"
        if self.errors:
            return "failed"
        return "success"


def _duplicate_hint(name: str, all_names: List[str]) -> str:
    """Подсказать, что папка похожа на дубликат, созданный самим сервером.

    Axigen при конфликте имён (например «s2022» и «S2022» — в файловой системе
    это одно и то же) заводит вторую папку с суффиксом ``_000``. Такие папки
    обычно и не открываются. Без этой подсказки администратор ищет причину в
    правах доступа, хотя чинить нужно сами папки на сервере.
    """
    def _key(value: str) -> str:
        return re.sub(r"_\d{3}$", "", value).lower()

    twins = [other for other in all_names if other != name and _key(other) == _key(name)]
    if not twins:
        return ""
    return f" Похоже на дубликат папки «{twins[0]}», созданный самим сервером."


#: Пояснение про такие дубликаты — длинное, поэтому выводится один раз за
#: прогон вместе с остальными подробностями, а не в строке про каждую папку.
DUPLICATE_EXPLANATION = (
    "Пара папок с именами, различающимися только регистром или суффиксом вида «_000», — "
    "это дубликат, который почтовый сервер создал сам при конфликте имён (в файловой системе "
    "«s2022» и «S2022» — одно и то же). Такие папки обычно и не открываются: их удаляют или "
    "переименовывают на сервере."
)


#: Совпадение имени папки с шаблоном include/exclude (общая логика с диагностикой).
_folder_matches = folder_matches


#: Соединение прогона с переподключением (общее для бэкапа и восстановления).
_Session = ReconnectingSession


class BackupEngine:
    def __init__(self, db, store: MaildirStore, options: ConnectOptions,
                 *, skip_larger_than_mb: int = 0, download_flags: bool = True,
                 global_exclude: Optional[List[str]] = None, global_include: Optional[List[str]] = None,
                 unreadable_grace_runs: int = 3) -> None:
        self.db = db
        self.store = store
        self.options = options
        self.skip_larger_than = int(skip_larger_than_mb) * 1024 * 1024
        self.download_flags = download_flags
        self.global_exclude = global_exclude or []
        self.global_include = global_include or []
        # Сколько прогонов подряд нечитаемая папка считается ОШИБКОЙ. Дальше она
        # переходит в «известные нечитаемые»: пробовать продолжаем, сообщать
        # продолжаем, но задание перестаёт быть неуспешным.
        self.unreadable_grace_runs = max(0, int(unreadable_grace_runs))
        self._explained_unreadable = False
        self._problem_folders: Optional[Set[str]] = None

    def _explain_unreadable(self, emit: EventCB, exc: BaseException, *, duplicate: bool = False) -> None:
        """Один раз за прогон объяснить подробно, что значит «папка не открывается».

        Подробности выводятся отдельным событием, а не в строке про папку:
        у ящика с несколькими битыми папками один и тот же абзац повторялся бы
        для каждой, а в строке про папку он упирался бы в предел длины события
        и обрезался бы ровно на счётчике прогонов.
        """
        if self._explained_unreadable:
            return
        self._explained_unreadable = True
        parts = []
        tried = getattr(exc, "tried", "")
        if tried:
            parts.append(f"Что пробовали: {tried}")
        diagnosis = getattr(exc, "diagnosis", "")
        if diagnosis:
            parts.append(f"Что известно о папке: {diagnosis}")
        if duplicate:
            parts.append(DUPLICATE_EXPLANATION)
        hint = getattr(exc, "hint", "") or ""
        if hint:
            parts.append(hint)
        if parts:
            emit("INFO", "Подробности по папкам, которые сервер не даёт открыть. "
                         + ". ".join(part.rstrip(". ") for part in parts) + ".")

    def _note_unreadable(self, account: Account, folder: str, error: str) -> int:
        """Запомнить очередную неудачу и вернуть, сколько их подряд."""
        try:
            return int(self.db.record_folder_problem(account.id, folder, error))
        except Exception as exc:  # noqa: BLE001
            # История неудач — вспомогательная вещь: если БД её не приняла,
            # копирование всё равно должно продолжаться.
            log.debug("Не удалось записать историю папки «%s»: %s", folder, exc)
            return 1

    def _forget_unreadable(self, account: Account, folder: str) -> None:
        """Папка открылась — забыть её историю неудач.

        Сначала сверяемся со списком проблемных папок, прочитанным один раз за
        прогон: без этого на каждую исправную папку уходила пишущая транзакция,
        которая ничего не удаляла (500 ящиков × 50 папок — 25 000 транзакций).
        """
        if self._problem_folders is not None and folder not in self._problem_folders:
            return
        try:
            self.db.clear_folder_problem(account.id, folder)
            if self._problem_folders is not None:
                self._problem_folders.discard(folder)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось очистить историю папки «%s»: %s", folder, exc)

    def _unreadable_since(self, account: Account, folder: str) -> str:
        """« с 21.09.2026» — когда папка перестала открываться (для журнала)."""
        try:
            row = self.db.get_folder_problem(account.id, folder)
        except Exception:  # noqa: BLE001
            return ""
        first = (row["first_failed"] if row else "") or ""
        return f", с {first[:10]}" if first else ""

    def _select_folders(self, conn: ImapConnection, account: Account,
                        emit: Optional[EventCB] = None) -> List:
        """
        Папки к копированию: выкинуть неоткрываемые (\\Noselect/\\NonExistent),
        применить include/exclude и СНЯТЬ ДУБЛИ.

        Дубли — не теория: сервер (замечено на Axigen) возвращает одну и ту же
        папку в ответе LIST дважды — повтор записи или пересечение пространств
        имён. Без дедупликации папка планируется и качается ДВА раза: её письма
        дважды считаются в «Новых писем к загрузке», дважды скачиваются с
        сервера и дважды пишутся в Maildir (в индексе БД остаётся первая
        запись, второй файл становится «сиротой»).

        Дедупликация идёт по ТОЧНОМУ имени и сохраняет порядок сервера.
        Регистр намеренно НЕ игнорируем: на одних серверах «Отправленные» и
        «отправленные» — одна папка, на других (регистрозависимое хранилище)
        это ДВЕ РАЗНЫЕ папки, и их склейка молча потеряла бы письма. Совпадение
        без учёта регистра только сообщаем — как повод проверить ящик руками.
        """
        include = list(account.folder_include or []) + list(self.global_include or [])
        exclude = list(account.folder_exclude or []) + list(self.global_exclude or [])

        def notice(msg: str) -> None:
            # emit пишет и в журнал задания, и в лог; без него (прямой вызов
            # из кода/тестов) остаётся обычный лог.
            if emit:
                emit("WARNING", msg)
            else:
                log.warning("%s", msg)

        result = []
        seen: Set[str] = set()
        seen_lower: Dict[str, str] = {}
        for f in conn.list_folders():
            if not f.selectable:
                # Контейнер (\\Noselect) или уже несуществующая запись: писем в
                # ней нет, а SELECT по ней гарантированно даст «ошибку».
                log.debug("Папка «%s» не копируется: не открывается, флаги: %s",
                          f.name, ", ".join(f.flags) or "—")
                continue
            if include and not _folder_matches(f.name, include, f.delimiter):
                continue
            if exclude and _folder_matches(f.name, exclude, f.delimiter):
                continue
            if f.name in seen:
                notice(f"Сервер вернул папку «{f.name}» в списке повторно — вторая запись "
                       f"пропущена (иначе папка копировалась бы дважды и удваивала счётчики).")
                continue
            seen.add(f.name)
            twin = seen_lower.setdefault(f.name.lower(), f.name)
            if twin != f.name:
                notice(f"Сервер вернул две папки, различающиеся только регистром: «{twin}» и "
                       f"«{f.name}». Копируем обе как разные папки — проверьте, так ли это "
                       f"на сервере.")
            result.append(f)
        return result

    # ------------------------------------------------------------------
    #  Нечитаемые папки
    # ------------------------------------------------------------------
    def _register_loss(self, account: Account, name: str, body: str, detail: str,
                       result: BackupResult, emit: EventCB, *, twin: str = "",
                       exc: Optional[BaseException] = None, error_text: str = "") -> None:
        """Папку прочитать не удалось, и письма в ней (возможно) есть.

        Первые ``unreadable_grace_runs`` суток это ошибка («КОПИЯ НЕПОЛНАЯ»),
        дальше — «известная нечитаемая папка»: сообщаем, но задание неудачным
        больше не помечаем, иначе предупреждение висит вечно и перестаёт что-либо
        значить.
        """
        fails = self._note_unreadable(account, name, error_text or detail)
        if self.unreadable_grace_runs and fails > self.unreadable_grace_runs:
            result.known_unreadable_folders.append(name)
            since = self._unreadable_since(account, name)
            emit("WARNING", f"Папка «{name}» {body}. Не открывается {fails}-й прогон "
                            f"подряд{since} — ошибкой больше не считаем.{twin}")
            if exc is not None:
                self._explain_unreadable(emit, exc, duplicate=bool(twin))
            return
        result.add_error(f"Папка «{name}»: {detail}")
        if name not in result.skipped_folders:
            result.skipped_folders.append(name)
        left = (self.unreadable_grace_runs - fails + 1) if self.unreadable_grace_runs else 0
        tail = (f" Не открывается {fails}-й прогон подряд; ещё {left} — и папка перейдёт "
                f"в «известные нечитаемые» (задание перестанет помечаться неполным)."
                if left > 0 else "")
        emit("WARNING", f"Пропуск папки «{name}»: {body}.{twin}{tail}")
        if exc is not None:
            self._explain_unreadable(emit, exc, duplicate=bool(twin))

    def _unreadable_folder(self, conn: ImapConnection, account: Account, f, folders: List,
                           exc: MailArchiverError, result: BackupResult, emit: EventCB) -> None:
        """Папка не открылась: решить, потеря это или нет, и сообщить.

        Решает STATUS — сколько писем сервер видит в папке. Ноль — терять нечего
        (битая пустая папка или контейнер с вложенными). Иначе это потеря, даже
        если у папки есть вложенные: в IMAP папка может одновременно хранить
        письма и содержать подпапки, и раньше такие письма молча объявлялись
        «папкой-контейнером» при статусе «Успешно».
        """
        msgs = getattr(exc, "status_messages", None)
        children = getattr(exc, "children", None)
        if children is None:
            children = conn.folder_children(f.name, f.delimiter)
        twin = _duplicate_hint(f.name, [x.name for x in folders])
        if msgs == 0:
            self._forget_unreadable(account, f.name)
            if children:
                result.container_folders.append(f.name)
                emit("INFO",
                     f"Папка «{f.name}» не открывается, но писем в ней по данным сервера 0, а "
                     f"вложенных папок {len(children)} — это папка-контейнер, своих писем она "
                     f"не хранит; вложенные папки копируются отдельно.")
            else:
                result.empty_unreadable_folders.append(f.name)
                emit("WARNING",
                     f"Папка «{f.name}» не открывается, но по данным сервера она ПУСТАЯ "
                     f"(0 писем) — копировать нечего, потерь нет.{twin}")
            return
        # Письма этой папки в копию НЕ попадут. В журнал пишем КОРОТКУЮ строку:
        # длинное объяснение упирается в предел длины события и обрезается
        # ровно на счётчике прогонов. Подробности выводятся один раз за прогон.
        detail = exc.message + (f" {exc.hint}" if exc.hint else "")
        reply = getattr(exc, "server_reply", "") or exc.message
        verdict = getattr(exc, "verdict", "")
        if msgs is not None:
            where = f"писем в ней по данным сервера: {msgs}"
        else:
            where = "сколько в ней писем, сервер не сообщает"
        if children:
            where += (f"; вложенных папок {len(children)} копируются отдельно, "
                      f"а письма самой папки — нет" if msgs else
                      f"; у неё есть вложенные папки ({len(children)}) — возможно, это "
                      f"контейнер, но проверить нельзя")
        body = f"не открывается: {reply}; {where}"
        if verdict and not children:
            body += f"; {verdict}"
        self._register_loss(account, f.name, body, detail, result, emit, twin=twin, exc=exc,
                            error_text=exc.message)

    # ------------------------------------------------------------------
    #  Прогон
    # ------------------------------------------------------------------
    def run(self, account: Account, *, progress_cb: Optional[ProgressCB] = None,
            cancel_cb: Optional[CancelCB] = None, event_cb: Optional[EventCB] = None) -> BackupResult:
        result = BackupResult()
        started = time.time()
        # Подробное объяснение про нечитаемые папки выводим один раз за прогон.
        self._explained_unreadable = False
        try:
            self._problem_folders = {row["folder"] for row in self.db.list_folder_problems(account.id)}
        except Exception:  # noqa: BLE001 — у заглушек БД метода может не быть
            self._problem_folders = None

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        # Сообщения об ОТДЕЛЬНЫХ письмах — с пределом на прогон: 5000 одинаковых
        # строк в журнале задания никому не помогают, а базу раздувают.
        per_message_events = [0]

        def emit_msg(level: str, msg: str) -> None:
            per_message_events[0] += 1
            if per_message_events[0] <= MAX_MESSAGE_EVENTS:
                emit(level, msg)
            elif per_message_events[0] == MAX_MESSAGE_EVENTS + 1:
                emit("WARNING", f"Сообщений об отдельных письмах больше {MAX_MESSAGE_EVENTS} — "
                                f"дальше они пишутся только в журнал службы; итог — в конце задания.")
                log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s",
                        account.name, msg)
            else:
                log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s",
                        account.name, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Бэкап отменён пользователем.")

        # Пачка записей индекса: пишем по INDEX_BATCH штук одной транзакцией.
        pending: List[tuple] = []

        def flush_index() -> None:
            """Сбросить накопленные записи индекса в базу."""
            if not pending:
                return
            rows = list(pending)
            pending.clear()
            try:
                self.db.add_message_index_batch(rows)
            except AttributeError:   # у заглушек БД в тестах метода может не быть
                for row in rows:
                    self.db.add_message_index(
                        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                        row[8], row[9], subject=row[10], from_addr=row[11], has_attach=row[12])

        emit("INFO", f"Подключение к ящику «{account.name}» ({account.host})…")
        session = _Session(account, self.options, emit, check_cancel)
        try:
            session.open()
            try:
                self._run_session(session, account, result, emit, emit_msg, check_cancel,
                                  pending, flush_index, progress_cb, started)
            finally:
                # Что бы ни случилось (обрыв, отмена, кончилось место, чужое
                # исключение), уже скачанное ОБЯЗАНО попасть в индекс: иначе
                # файлы остаются «сиротами», а письма качаются снова и снова.
                flush_index()
        finally:
            result.reconnects = session.reconnects
            session.close()

        # folders_processed считает только папки, в которых БЫЛИ новые письма:
        # на обычном прогоне это 0 из 50, что выглядит как «ничего не проверено».
        # Для итога берём число реально прочитанных папок.
        summary = (f"Бэкап завершён: новых писем {result.messages_new}, "
                   f"пропущено по размеру {result.messages_skipped}, ошибок {result.errors}.")
        if result.messages_failed:
            summary += (f" Сервер не отдал писем: {result.messages_failed} — они не скопированы, "
                        f"попытка повторится при следующем прогоне.")
        if result.messages_vanished:
            summary += (f" Удалено на сервере во время копирования: {result.messages_vanished} "
                        f"(это не ошибка).")
        if result.reconnects:
            summary += f" Переподключений после обрыва связи: {result.reconnects}."
        if result.known_unreadable_folders:
            shown = ", ".join(result.known_unreadable_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            rest = len(result.known_unreadable_folders) - MAX_SKIPPED_FOLDERS_IN_SUMMARY
            tail = f" и ещё {rest}" if rest > 0 else ""
            summary += (f" Известные нечитаемые папки ({len(result.known_unreadable_folders)}): "
                        f"{shown}{tail} — сервер не открывает их давно, ошибкой не считаем.")
        if result.container_folders:
            shown = ", ".join(result.container_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            rest = len(result.container_folders) - MAX_SKIPPED_FOLDERS_IN_SUMMARY
            tail = f" и ещё {rest}" if rest > 0 else ""
            summary += (f" Папок-контейнеров, которые сервер не открывает "
                        f"({len(result.container_folders)}): {shown}{tail} — писем в них 0, "
                        f"вложенные папки скопированы.")
        if result.empty_unreadable_folders:
            # Про такие папки сообщаем, но копию неполной не объявляем: писем в
            # них нет, и администратору важно лишь знать, что на сервере мусор.
            shown = ", ".join(result.empty_unreadable_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            rest = len(result.empty_unreadable_folders) - MAX_SKIPPED_FOLDERS_IN_SUMMARY
            tail = f" и ещё {rest}" if rest > 0 else ""
            summary += (f" Пустых папок, которые сервер не даёт открыть "
                        f"({len(result.empty_unreadable_folders)}): {shown}{tail} — писем в них нет.")
        if result.skipped_folders:
            # Неполная копия должна быть ВИДНА: из «ошибок 2» не понять, что
            # именно не скопировано, поэтому перечисляем сами папки.
            shown = ", ".join(result.skipped_folders[:MAX_SKIPPED_FOLDERS_IN_SUMMARY])
            rest = len(result.skipped_folders) - MAX_SKIPPED_FOLDERS_IN_SUMMARY
            tail = f" и ещё {rest}" if rest > 0 else ""
            summary += (f" КОПИЯ НЕПОЛНАЯ: не удалось прочитать папки "
                        f"({len(result.skipped_folders)}): {shown}{tail}.")
        emit("WARNING" if (result.skipped_folders or result.messages_failed) else "INFO", summary)
        return result

    def _run_session(self, session: _Session, account: Account, result: BackupResult,
                     emit: EventCB, emit_msg: EventCB, check_cancel: Callable[[], None],
                     pending: List[tuple], flush_index: Callable[[], None],
                     progress_cb: Optional[ProgressCB], started: float) -> None:
        folders = self._select_folders(session.conn, account, emit)
        result.folders_total = len(folders)
        emit("INFO", f"Найдено папок к копированию: {len(folders)}")
        for bad in list(session.conn.bad_folder_names):
            # Имя в неверной кодировке нельзя даже передать серверу в SELECT —
            # письма такой папки не копируются, и это надо показать.
            self._register_loss(
                account, bad,
                "имя прислано сервером в неверной кодировке IMAP UTF-7, открыть папку по имени нельзя",
                "Имя папки прислано сервером в неверной кодировке (IMAP UTF-7) — папку нельзя "
                "открыть. Переименуйте её на почтовом сервере.", result, emit)

        # --- Фаза планирования ---
        plan: List[Dict] = []
        total_new = 0
        for f in folders:
            check_cancel()
            item = None
            for attempt in (1, 2):
                try:
                    item = self._plan_folder(session.conn, account, f, folders, result, emit)
                    session.progressed()
                    break
                except (ImapConnectionError, ImapTimeoutError) as exc:
                    if attempt == 2:
                        # Эта же папка второй раз подряд рвёт связь или «висит»
                        # дольше таймаута — дело в ней, а не в сети. Откладываем
                        # её и идём дальше по новому соединению.
                        self._register_loss(
                            account, f.name,
                            f"сервер обрывает связь или не отвечает при открытии папки ({exc.message})",
                            f"Сервер обрывает связь или не отвечает при открытии папки: {exc.message}",
                            result, emit, error_text=exc.message)
                        session.reconnect(exc)
                        break
                    session.reconnect(exc)
            if item:
                plan.append(item)
                total_new += len(item["uids"])

        result.messages_total = total_new
        emit("INFO", f"Новых писем к загрузке: {total_new}")
        if progress_cb:
            progress_cb(0, total_new, "Планирование завершено", 0, 0.0)

        # --- Фаза загрузки ---
        state = {"done": 0, "bytes": 0}

        def report_progress(label: str) -> None:
            if progress_cb:
                elapsed = max(0.001, time.time() - started)
                progress_cb(state["done"], total_new, label, state["bytes"], state["bytes"] / elapsed)

        planned_done = 0  # сколько писем «прошло» по плану (для прогресса)
        for p in plan:
            check_cancel()
            f = p["folder"]
            planned_done += len(p["uids"])
            emit("INFO", f"Папка «{f.name}»: загрузка {len(p['uids'])} писем…")
            handled: Set[int] = set()
            try:
                stalls = 0
                while True:
                    remaining = [u for u in p["uids"] if u not in handled]
                    if not remaining:
                        break
                    before = len(handled)
                    try:
                        self._load_folder(session, account, p, remaining, handled, result, emit_msg,
                                          check_cancel, pending, flush_index, state, total_new,
                                          report_progress)
                        break
                    except (ImapConnectionError, ImapTimeoutError) as exc:
                        flush_index()
                        stalls = stalls + 1 if len(handled) == before else 0
                        # Продолжаем эту же папку с того места, где оборвалось.
                        session.reconnect(exc)
                        if stalls >= 2:
                            # Дважды подряд связь рвётся на этой папке, а ни одного
                            # письма получить не удаётся (например, письмо, которое
                            # сервер не успевает отдать за таймаут). Откладываем
                            # папку — иначе она навсегда блокировала бы все следующие.
                            raise MailArchiverError(
                                f"сервер дважды подряд оборвал связь при загрузке писем этой папки, "
                                f"не отдав ни одного ({exc.message}) — папка отложена до следующего "
                                f"прогона.") from exc
                result.folders_processed += 1
                session.progressed()
            except (JobCancelled, DiskSpaceError, ImapConnectionError, ImapTimeoutError):
                raise
            except MailArchiverError as exc:
                flush_index()    # уже скачанные письма обязаны попасть в индекс
                self._folder_load_failed(f.name, exc.message, result, emit)
            except Exception as exc:  # noqa: BLE001
                # Чужое исключение (сбой разбора ответа сервера и т.п.): папку
                # откладываем, соединение открываем заново — поток ответа мог
                # остаться в неизвестном состоянии.
                flush_index()
                log.exception("[%s] Сбой при загрузке папки «%s»", account.name, f.name)
                self._folder_load_failed(f.name, f"{type(exc).__name__}: {exc}", result, emit)
                session.reset()
            finally:
                # Папка пройдена — целиком, с ошибкой или пропущена: её
                # запланированные письма больше не «в работе». Без этого
                # прогресс-бар застревал бы ниже 100 %.
                state["done"] = max(state["done"], planned_done)

        flush_index()
        report_progress("Готово")

    @staticmethod
    def _folder_load_failed(name: str, message: str, result: BackupResult, emit: EventCB) -> None:
        # Папку запоминаем: её письма в копию не попали, и без этого «КОПИЯ
        # НЕПОЛНАЯ» не появлялась именно в худшем случае — когда сбой
        # случился уже во время загрузки.
        result.add_error(f"Папка «{name}»: {message}")
        if name not in result.skipped_folders:
            result.skipped_folders.append(name)
        emit("ERROR", f"Ошибка в папке «{name}»: {message}")

    def _plan_folder(self, conn: ImapConnection, account: Account, f, folders: List,
                     result: BackupResult, emit: EventCB) -> Optional[Dict]:
        """Открыть папку и выяснить, какие письма в ней новые.

        Ошибки соединения пробрасываются (их обрабатывает вызывающий:
        переподключение). Отказ сервера по самой папке — её ошибка, а не всего
        ящика: раньше отказ SEARCH в одной папке ронял копирование всех.
        """
        try:
            info = conn.select(f.name, readonly=True)
        except (ImapConnectionError, ImapTimeoutError):
            raise
        except MailArchiverError as exc:
            self._unreadable_folder(conn, account, f, folders, exc, result, emit)
            return None
        uidvalidity = info["uidvalidity"]
        state = self.db.get_folder_state(account.id, f.name)
        old_uidvalidity = int(state["uidvalidity"] or 0) if state else 0
        if not uidvalidity:
            # Сервер не сообщил UIDVALIDITY. Считать это сменой нельзя —
            # иначе папка перекачивалась бы на каждом прогоне.
            emit("WARNING", f"Папка «{f.name}»: сервер не сообщил UIDVALIDITY — "
                            f"проверка смены пропущена, работаем по прежнему значению "
                            f"({old_uidvalidity}).")
            uidvalidity = old_uidvalidity
        elif state is not None and old_uidvalidity != uidvalidity:
            # Настоящая смена UIDVALIDITY. Ничего не удаляем: ни файлы, ни
            # индекс. Письма будут скачаны заново под новым uidvalidity
            # (он входит в UNIQUE(account_id, folder, uidvalidity, uid)),
            # а прежние записи останутся как исторические.
            if old_uidvalidity:
                emit("WARNING", f"UIDVALIDITY папки «{f.name}» изменился "
                                f"({old_uidvalidity} → {uidvalidity}): письма будут перекачаны заново под "
                                f"новым UIDVALIDITY. Ранее скачанные файлы и записи индекса сохранены "
                                f"как исторические — ничего не удаляется.")
            else:
                emit("WARNING", f"Папка «{f.name}»: сервер впервые сообщил UIDVALIDITY ({uidvalidity}) — "
                                f"письма будут переписаны под ним; ранее скачанное сохранено.")
        try:
            all_uids = conn.search_uids(info)
        except (ImapConnectionError, ImapTimeoutError):
            raise
        except MailArchiverError as exc:
            self._register_loss(account, f.name,
                                f"открылась, но сервер отказал в поиске писем (SEARCH): {exc.message}",
                                f"Сервер отказал в поиске писем (SEARCH): {exc.message}",
                                result, emit, error_text=exc.message)
            return None
        result.folders_read += 1
        self._forget_unreadable(account, f.name)
        # existing_uids запрашиваем с НОВЫМ uidvalidity: при смене набор
        # окажется пустым и папка скачается целиком — это и требуется.
        # В набор входят и письма, вычищенные по сроку хранения: иначе
        # удалённое ретеншном каждую ночь скачивалось бы заново.
        existing = self.db.existing_uids(account.id, f.name, uidvalidity)
        new_uids = sorted(u for u in all_uids if u not in existing)
        prune = getattr(self.db, "prune_retired_uids", None)
        if prune is not None:
            try:
                # UID, которых на сервере больше нет, помнить незачем: в пределах
                # одного UIDVALIDITY сервер номера не переиспользует.
                prune(account.id, f.name, uidvalidity, all_uids)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось почистить список вычищенных UID «%s»: %s", f.name, exc)
        # обновим общее число писем в папке (даже если новых нет)
        self.db.upsert_folder_state(account.id, f.name, uidvalidity,
                                    max(all_uids) if all_uids else 0, info["exists"])
        if not new_uids:
            return None
        return {"folder": f, "uidvalidity": uidvalidity, "uids": new_uids, "server_count": info["exists"]}

    def _load_folder(self, session: _Session, account: Account, p: Dict, remaining: List[int],
                     handled: Set[int], result: BackupResult, emit_msg: EventCB,
                     check_cancel: Callable[[], None], pending: List[tuple],
                     flush_index: Callable[[], None], state: Dict[str, int], total_new: int,
                     report_progress: Callable[[str], None]) -> None:
        """Скачать письма ``remaining`` одной папки. Обрыв связи пробрасывается."""
        f = p["folder"]
        conn = session.conn
        sel = conn.select(f.name, readonly=True)
        cur_uidvalidity = sel["uidvalidity"]
        if cur_uidvalidity and cur_uidvalidity != p["uidvalidity"]:
            # UIDVALIDITY сменился между планированием и загрузкой (или за время
            # переподключения): запланированные UID теперь указывают на ЧУЖИЕ письма.
            raise MailArchiverError(
                f"UIDVALIDITY изменился между планированием и загрузкой "
                f"({p['uidvalidity']} → {cur_uidvalidity}) — папка пропущена, будет скачана "
                f"при следующем запуске.")

        def on_skipped(uid: int, size: int) -> None:
            """Письмо отсеяно по размеру ещё ДО скачивания: учитываем в прогрессе."""
            handled.add(uid)
            result.messages_skipped += 1
            state["done"] += 1
            emit_msg("WARNING", f"Письмо UID {uid} в «{f.name}» пропущено (больше лимита размера: {size} Б).")

        max_uid = 0
        missing: List[int] = []
        store_errors_in_row = 0
        # Лимит размера передаём в клиент: слишком крупные письма
        # отсеиваются по ответу (RFC822.SIZE) и вообще не качаются.
        for msg in conn.fetch_messages(remaining, skip_larger_than=self.skip_larger_than,
                                       on_skipped=on_skipped):
            check_cancel()
            uid = msg["uid"]
            handled.add(uid)
            if msg.get("error") is not None:
                # Сервер отказался отдать именно это письмо (повреждено на
                # сервере и т.п.). Остальные письма папки при этом копируются.
                result.messages_failed += 1
                result.add_error(f"UID {uid} в «{f.name}»: сервер не отдаёт письмо ({msg['error']})")
                emit_msg("ERROR", f"Письмо UID {uid} в «{f.name}»: сервер отказался его отдать — "
                                  f"{msg['error']}")
                state["done"] += 1
                continue
            if msg.get("missing"):
                missing.append(uid)
                state["done"] += 1
                continue
            raw = msg["raw"]
            if self.skip_larger_than and len(raw) > self.skip_larger_than:
                # Подстраховка: сервер мог не сообщить RFC822.SIZE
                # или сообщить заниженный размер.
                result.messages_skipped += 1
                state["done"] += 1
                emit_msg("WARNING", f"Письмо UID {uid} в «{f.name}» пропущено (больше лимита размера).")
                continue
            flags = msg["flags"] if self.download_flags else []
            try:
                relpath, digest, size = self.store.store_message(
                    account.id, f.name, f.delimiter, uid, raw,
                    flags=flags, internaldate=msg["internaldate"],
                )
                subject, from_addr, has_attach = self._extract_meta(raw)
                message_id = self._extract_message_id(raw)
            except DiskSpaceError:
                # Места нет — дальше не сохранится ни одно письмо. Качать весь
                # ящик впустую (и писать по ошибке на каждое) бессмысленно.
                raise
            except Exception as exc:  # noqa: BLE001
                text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
                result.add_error(f"UID {uid} в «{f.name}»: {text}")
                emit_msg("ERROR", f"Ошибка сохранения UID {uid} в «{f.name}»: {text}")
                # письмо обработано (пусть и с ошибкой) — иначе
                # прогресс-бар не дойдёт до 100 %
                state["done"] += 1
                store_errors_in_row += 1
                if store_errors_in_row >= MAX_CONSECUTIVE_STORE_ERRORS:
                    raise MailArchiverError(
                        f"подряд {store_errors_in_row} писем не удалось сохранить на диск "
                        f"(последняя ошибка: {text}) — папка отложена до следующего прогона.")
                continue
            store_errors_in_row = 0
            del raw
            # Копим пачку: одна транзакция на INDEX_BATCH писем вместо отдельной на каждое.
            pending.append((
                account.id, f.name, p["uidvalidity"], uid, message_id, size,
                self._epoch_to_iso(msg["internaldate"]),
                ",".join(flags), relpath, digest,
                subject[:500], from_addr[:300], 1 if has_attach else 0,
            ))
            if len(pending) >= INDEX_BATCH:
                flush_index()
            result.messages_new += 1
            result.bytes_new += size
            state["bytes"] += size
            state["done"] += 1
            session.progressed()
            max_uid = max(max_uid, uid)
            if state["done"] % 10 == 0 or state["done"] == total_new:
                report_progress(f"«{f.name}»: {state['done']}/{total_new}")

        if missing:
            self._report_missing(conn, f.name, missing, result, emit_msg)
        # Индекс папки фиксируем ДО обновления её состояния: иначе при обрыве
        # last_uid ушёл бы вперёд, а писем в индексе не было бы.
        flush_index()
        if max_uid:
            st = self.db.get_folder_state(account.id, f.name)
            cur_max = int(st["last_uid"] or 0) if st else 0
            self.db.upsert_folder_state(account.id, f.name, p["uidvalidity"],
                                        max(cur_max, max_uid), p["server_count"])

    @staticmethod
    def _report_missing(conn: ImapConnection, folder: str, missing: List[int],
                        result: BackupResult, emit_msg: EventCB) -> None:
        """Письма, которые FETCH не вернул: удалены во время копирования или потеряны?

        Раньше это попадало только в журнал службы, и письмо, повреждённое на
        сервере, тихо не попадало в копию никогда. Спрашиваем сервер: если UID
        всё ещё в папке — сервер его не отдаёт (ошибка), иначе письмо просто
        удалили, пока шло копирование.
        """
        present = conn.uids_present(missing)
        shown = ", ".join(str(u) for u in missing[:20]) + (f" и ещё {len(missing) - 20}"
                                                            if len(missing) > 20 else "")
        if present is None:
            result.messages_failed += len(missing)
            result.add_error(f"«{folder}»: сервер не вернул писем {len(missing)} (UID {shown})")
            emit_msg("WARNING", f"Папка «{folder}»: сервер не вернул писем {len(missing)} (UID {shown}); "
                                f"проверить, удалены ли они, не удалось — повторим при следующем прогоне.")
            return
        lost = [u for u in missing if u in present]
        vanished = len(missing) - len(lost)
        result.messages_vanished += vanished
        if vanished:
            emit_msg("INFO", f"Папка «{folder}»: писем удалено на сервере во время копирования: {vanished}.")
        if lost:
            result.messages_failed += len(lost)
            shown_lost = ", ".join(str(u) for u in lost[:20]) + (f" и ещё {len(lost) - 20}"
                                                                  if len(lost) > 20 else "")
            result.add_error(f"«{folder}»: сервер не отдаёт писем {len(lost)} (UID {shown_lost})")
            emit_msg("ERROR", f"Папка «{folder}»: сервер не отдаёт писем {len(lost)} (UID {shown_lost}), "
                              f"хотя они в папке есть — возможно, повреждены на сервере.")

    @staticmethod
    def _header_block(raw: bytes) -> bytes:
        """Блок заголовков письма (до первой пустой строки), не длиннее HEADER_PARSE_LIMIT."""
        head = raw[:HEADER_PARSE_LIMIT]
        if head[:2] == b"\r\n" or head[:1] == b"\n":
            return b""                              # письмо без заголовков
        match = re.search(rb"\r?\n\r?\n", head)
        return head[:match.end()] if match else head

    @classmethod
    def _extract_message_id(cls, raw: bytes) -> str:
        """Message-ID из блока заголовков — целиком, а не из первых 8 КБ.

        У писем Exchange Online с ARC/DKIM/антиспамом до Message-ID бывает
        10–15 КБ заголовков, а само значение нередко свёрнуто на следующую
        строку. Пустой Message-ID в индексе ломал проверку дублей при
        восстановлении.
        """
        try:
            return header_value(cls._header_block(raw), "Message-ID")[:250]
        except Exception:  # noqa: BLE001
            return ""

    @classmethod
    def _extract_meta(cls, raw: bytes):
        """Извлечь тему, отправителя и признак вложений (для списка писем).

        Разбираем ТОЛЬКО блок заголовков: полный разбор письма ради двух полей
        стоил в 9–12 раз больше памяти, чем само письмо.
        """
        subject, from_addr, has_attach = "", "", 0
        try:
            from email import policy
            from email.parser import BytesHeaderParser
            from email.utils import parseaddr
            # policy.default корректно декодирует как RFC 2047, так и «сырые» UTF-8 заголовки
            hdrs = BytesHeaderParser(policy=policy.default).parsebytes(cls._header_block(raw))
            try:
                subject = str(hdrs.get("Subject", "") or "")
            except Exception:  # noqa: BLE001 — кривой заголовок не должен терять остальное
                subject = header_value(cls._header_block(raw), "Subject")
            try:
                from_raw = str(hdrs.get("From", "") or "")
            except Exception:  # noqa: BLE001
                from_raw = header_value(cls._header_block(raw), "From")
            _name, addr = parseaddr(from_raw)
            # в БД нужен нормализованный адрес; сырой заголовок — только если
            # адрес выделить не удалось
            from_addr = addr or from_raw
            try:
                ctype = (str(hdrs.get("Content-Type", "")) or "").lower()
            except Exception:  # noqa: BLE001
                ctype = header_value(cls._header_block(raw), "Content-Type").lower()
            low = raw[:40000].lower()
            if "multipart/mixed" in ctype or b"content-disposition: attachment" in low or b"filename=" in low:
                has_attach = 1
        except Exception:  # noqa: BLE001
            pass
        return subject, from_addr, has_attach

    @staticmethod
    def _epoch_to_iso(epoch: Optional[float]) -> str:
        if not epoch:
            return ""
        from datetime import datetime, timezone
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
