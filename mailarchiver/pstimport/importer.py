"""
Импорт .pst: письма файла — на сервер IMAP или в локальную копию ящика.

Источник писем — встроенный читатель (:mod:`.reader` + :mod:`.eml`): файл
читается потоком, письмо за письмом, без распаковки на диск. Запасной
источник — внешняя утилита ``readpst`` (libpst): он остаётся на случай файла,
который встроенный читатель не понял, и включается явно.

Цели две:

* **сервер IMAP** — письма заливаются командой APPEND в папки с указанным
  префиксом, с флагами («прочитано», «отвечено», «помечено», «черновик») и
  датой получения. Письма, которые уже есть в папке назначения (по
  Message-ID), не заливаются второй раз — прерванный импорт можно повторить;
* **локальная копия** — письма записываются прямо в архив ящика и попадают в
  индекс как исторические, под особым UIDVALIDITY
  (:data:`IMPORTED_UIDVALIDITY`): копирование по IMAP их не трогает и не
  скачивает заново, а в просмотре, поиске, выгрузке и восстановлении они
  видны, как любые другие. Уже сохранённое в ту же папку письмо узнаётся по
  SHA-256 (сборка письма детерминирована), письма старше срока хранения
  ящика не записываются — их удалила бы ближайшая ночная очистка.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..errors import (DiskSpaceError, ImapConnectionError, ImapTimeoutError, JobCancelled,
                      MailArchiverError, ValidationError)
from ..logging_setup import get_logger
from ..util import sha256_hex
from . import eml as emlmod
from .reader import PstReader, PstReadError, PstUnsupportedError

log = get_logger("pstimport")

#: UIDVALIDITY писем, импортированных в локальную копию. У сервера IMAP это
#: число всегда положительное; -1 занято письмами, возвращёнными из прежних копий.
IMPORTED_UIDVALIDITY = -2
DEFAULT_PREFIX = "Импорт PST"
TARGETS = ("imap", "local")
ENGINES = ("builtin", "readpst")
#: Кодировки строк старых файлов ANSI, которые можно выбрать при импорте.
ANSI_CODECS = {
    "cp1251": "Windows-1251 (кириллица)",
    "cp1252": "Windows-1252 (Западная Европа)",
    "cp1250": "Windows-1250 (Центральная Европа)",
    "cp1257": "Windows-1257 (Балтия)",
    "cp866": "DOS 866 (кириллица)",
}
#: Сколько раз пробовать залить одно письмо, если связь рвётся.
MAX_APPEND_ATTEMPTS = 3
#: Сколько строк индекса копить перед записью в базу.
INDEX_BATCH = 200
MAX_ERROR_DETAILS = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

EventCB = Callable[[str, str], None]
ProgressCB = Callable[[int, int, str], None]
CancelCB = Callable[[], bool]

#: Чем кончается чтение повреждённого файла: файл чужой, и любая из этих ошибок —
#: признак порчи, а не сбой службы.
_MESSAGE_ERRORS = (PstReadError, struct.error, IndexError, KeyError, ValueError, OverflowError,
                   RecursionError, LookupError)


@dataclass
class ImportItem:
    """Одно письмо от источника: готовое к заливке, пропущенное или нечитаемое."""
    folder: Tuple[str, ...]
    label: str                 # как назвать письмо в журнале задания (с темой)
    raw: bytes = b""
    flags: List[str] = field(default_factory=list)
    internaldate: Optional[float] = None
    message_id: str = ""
    error: str = ""            # письмо не прочитано
    not_mail: bool = False     # контакт, встреча, задача…
    notes: List[str] = field(default_factory=list)     # пропущено по устройству формата
    lost: List[str] = field(default_factory=list)      # не прочитано из-за повреждения
    #: как назвать письмо в журнале СЛУЖБЫ: без темы — журнал читает и оператор,
    #: которому содержимое писем видеть не положено
    log_label: str = ""


@dataclass
class ImportResult:
    target: str = "imap"
    engine: str = "builtin"
    skip_existing: bool = True
    description: str = ""      # формат файла
    total: int = 0
    imported: int = 0
    imported_bytes: int = 0
    duplicates: int = 0        # уже были в папке назначения или в архиве
    not_mail: int = 0
    outdated: int = 0          # старше срока хранения ящика (только локальная копия)
    errors: int = 0
    noted: int = 0             # писем, в которых пропущено непереносимое (объект OLE, вложение-ссылка)
    lossy: int = 0             # писем, перенесённых с потерями (нечитаемое вложение или тело)
    damaged: int = 0           # блоков файла с неверной контрольной суммой
    reconnects: int = 0
    unchecked: int = 0         # залито без проверки на повтор (проверка не сработала)
    retention_days: int = 0
    stopped: str = ""          # почему импорт остановлен досрочно
    #: после остановки файл стоит сохранить для повтора (сервер недоступен, кончилась квота)
    keep_file: bool = False
    last_error: str = ""
    error_details: List[str] = field(default_factory=list)

    def add_error(self, text: str) -> None:
        self.errors += 1
        self.last_error = text
        if len(self.error_details) < MAX_ERROR_DETAILS:
            self.error_details.append(text)

    @property
    def incomplete(self) -> bool:
        """Перенесено не всё, что должно было: итог задания — «частично»."""
        return bool(self.errors or self.stopped or self.lossy or self.damaged)

    @property
    def left(self) -> int:
        """Писем, до которых дело не дошло (импорт остановлен досрочно)."""
        return max(0, self.total - self.imported - self.duplicates - self.not_mail - self.outdated - self.errors)


def clean_prefix(value: Optional[str]) -> str:
    """Префикс папки назначения из формы: без управляющих знаков и пробелов по краям."""
    return _CONTROL.sub("", str(value or "")).strip()[:200]


def folder_name(prefix: str, path: Tuple[str, ...], delimiter: str) -> str:
    """Имя папки назначения: префикс и путь папки в .pst через разделитель сервера."""
    delimiter = delimiter or "/"
    parts = []
    for part in path:
        part = _CONTROL.sub("", str(part or "")).replace(delimiter, "_").strip()[:200]
        parts.append(part or "_")
    if prefix:
        parts.insert(0, prefix)
    return delimiter.join(parts) or DEFAULT_PREFIX


# ---------------------------------------------------------------------------
#  Источники писем
# ---------------------------------------------------------------------------
def builtin_source(reader: PstReader,
                   check: Optional[Callable[[], None]] = None) -> Tuple[int, Iterator[ImportItem]]:
    """Письма файла через встроенный читатель: (сколько всего, перебор).

    Перебор не бросает исключений из-за содержимого файла: что бы ни случилось
    при чтении одного письма, это ошибка этого письма.
    """
    folders = reader.folders(check)
    total = sum(folder.count for folder in folders)

    def items() -> Iterator[ImportItem]:
        for folder in folders:
            where = "/".join(folder.path) or "корень"
            for nid in reader.message_nids(folder.nid):
                label = log_label = f"письмо {nid:#x} в папке «{where}»"
                try:
                    message = reader.open_message(nid)
                    if not emlmod.is_mail_class(message.message_class):
                        yield ImportItem(folder.path, label, not_mail=True, log_label=log_label)
                        continue
                    built = emlmod.build_eml(message)
                except Exception as exc:  # noqa: BLE001 — файл чужой: любая ошибка здесь — порча письма
                    text = str(exc) if isinstance(exc, PstReadError) else f"{type(exc).__name__}: {exc}"
                    yield ImportItem(folder.path, label, error=text[:300], log_label=log_label)
                    continue
                if built.subject:
                    label = f"«{built.subject[:120]}» в папке «{where}»"
                yield ImportItem(folder.path, label, built.raw, built.flags, built.internaldate,
                                 built.message_id, notes=built.notes, lost=built.lost, log_label=log_label)

    return total, items()


def _header_date(raw: bytes) -> Optional[float]:
    """Дата письма из заголовка Date (у писем от readpst другой даты нет)."""
    from email.utils import parsedate_to_datetime
    from ..imap.client import header_value
    head = raw[:256 * 1024]
    end = head.find(b"\r\n\r\n")
    if end < 0:
        end = head.find(b"\n\n")
    value = header_value(head[:end] if end >= 0 else head, "Date")
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def run_readpst(pst_path: str, out_dir: str, cancelled: CancelCB) -> None:
    """readpst с проверкой отмены: на большом .pst он работает до часа."""
    if shutil.which("readpst") is None:
        raise MailArchiverError("Утилита readpst (пакет pst-utils) не установлена на сервере.",
                                code="dependency_missing",
                                hint="Выберите встроенный разбор файла — ему readpst не нужен. Либо установите "
                                     "пакет pst-utils (Debian/Ubuntu) или libpst (RHEL).")
    # -e: .eml по файлу; -o: каталог.
    # -j 0: без параллельных процессов. В параллельном режиме (по умолчанию)
    # readpst изредка теряет письма: дочерние процессы выбирают одинаковое
    # имя файла, и одно письмо затирает другое. Проверено на libpst 0.6.76:
    # из 300 писем в разных запусках извлекалось то 300, то 299 — без
    # единой ошибки. Для импорта архива это молчаливая потеря писем.
    proc = subprocess.Popen(["readpst", "-j", "0", "-e", "-o", out_dir, pst_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 6 * 3600
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if cancelled():
                    proc.kill()
                    proc.communicate()
                    raise JobCancelled("Импорт отменён пользователем.")
                if time.time() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise MailArchiverError("readpst работает дольше 6 часов — прерван.", code="import_error")
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        raise
    if proc.returncode != 0:
        raise MailArchiverError(f"readpst завершился с ошибкой: {out} {err}"[:400], code="import_error")


def readpst_source(out_dir: str) -> Tuple[int, Iterator[ImportItem]]:
    """Письма, которые readpst разложил по каталогам: (сколько всего, перебор)."""
    from ..imap.backup import BackupEngine
    files: List[Tuple[Tuple[str, ...], str]] = []
    for root, _dirs, names in os.walk(out_dir):
        rel = os.path.relpath(root, out_dir)
        path = tuple(p for p in re.split(r"[\\/]+", rel) if p and p != ".")
        for name in sorted(names):
            if name.lower().endswith(".eml"):
                files.append((path, os.path.join(root, name)))

    def items() -> Iterator[ImportItem]:
        for path, full in files:
            label = f"файл «{os.path.basename(full)}» в папке «{'/'.join(path) or 'корень'}»"
            try:
                with open(full, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                yield ImportItem(path, label, error=f"не удалось прочитать: {exc}", log_label=label)
                continue
            if not raw:
                yield ImportItem(path, label, error="пустой файл", log_label=label)
                continue
            yield ImportItem(path, label, raw, [], _header_date(raw),
                             BackupEngine._extract_message_id(raw), log_label=label)  # noqa: SLF001

    return len(files), items()


# ---------------------------------------------------------------------------
#  Цели
# ---------------------------------------------------------------------------
class _Stop(Exception):
    """Дальше заливать бессмысленно (сервер недоступен, кончилась квота или место)."""

    def __init__(self, text: str, keep_file: bool) -> None:
        super().__init__(text)
        #: стоит ли сохранить загруженный файл, чтобы импорт можно было повторить
        self.keep_file = keep_file


class _ImapTarget:
    """Заливка на сервер IMAP с переподключением и проверкой повторов."""

    name = "imap"

    def __init__(self, svc, account, prefix: str, skip_existing: bool, result: ImportResult,
                 event: EventCB, check_cancel: Callable[[], None]) -> None:
        from ..imap.client import ReconnectingSession
        from ..imap.restore import _DuplicateIndex
        self.prefix = prefix
        self.skip_existing = skip_existing
        self.result = result
        self.event = event
        self.session = ReconnectingSession(account, svc.connect_options(), event, check_cancel)
        self.session.open()
        try:
            # list_folders() заодно сообщает разделитель иерархии сервера;
            # подстраховываемся на случай, если сервер его не прислал —
            # иначе None попал бы в имя папки строкой "None".
            self.session.conn.list_folders()
        except BaseException:
            self.session.close(force=True)
            raise
        self.delimiter = self.session.conn.delimiter or "/"
        self.dups = _DuplicateIndex(self.session.conn, event)
        self.ensured: set = set()
        self.failed: set = set()

    def folder(self, path: Tuple[str, ...]) -> str:
        return folder_name(self.prefix, path, self.delimiter)

    def put(self, folder: str, item: ImportItem) -> str:
        """Залить письмо. → imported | duplicate | error. Бросает :class:`_Stop`."""
        from ..imap.restore import _is_quota_error, sanitize_flags_for_append
        if folder in self.failed:
            self.result.add_error(f"{item.label}: папка «{folder}» недоступна")
            return "error"
        flags = sanitize_flags_for_append(item.flags)
        moment = datetime.fromtimestamp(item.internaldate, tz=timezone.utc) if item.internaldate else None
        checked: Optional[bool] = None            # None — проверка на повтор ещё не делалась
        for _attempt in range(MAX_APPEND_ATTEMPTS):
            conn = self.session.conn
            try:
                if folder not in self.ensured:
                    try:
                        conn.ensure_folder(folder)
                    except (ImapConnectionError, ImapTimeoutError):
                        raise
                    except MailArchiverError as exc:
                        # папку создать не удалось — письма этой папки
                        # пропускаем, но весь импорт не роняем
                        self.failed.add(folder)
                        self.result.add_error(f"Папка «{folder}» недоступна: {exc.message}")
                        self.event("ERROR", f"Папка «{folder}» недоступна: {exc.message}. Письма этой папки "
                                            f"пропускаются.")
                        return "error"
                    self.ensured.add(folder)
                if self.skip_existing and item.message_id:
                    known = self.dups.is_duplicate(folder, item.message_id)
                    if known:
                        return "duplicate"
                    checked = known is not None
                try:
                    conn.append(folder, item.raw, flags=flags, msg_time=moment)
                except (ImapConnectionError, ImapTimeoutError):
                    raise
                except MailArchiverError as exc:
                    keywords = [flag for flag in flags if not flag.startswith("\\")]
                    if not keywords or _is_quota_error(exc):
                        raise
                    # Сервер без пользовательских меток (так бывает у Exchange) отвергает
                    # APPEND с «$Forwarded» — письмо важнее метки: заливаем без неё.
                    flags = [flag for flag in flags if flag.startswith("\\")]
                    conn.append(folder, item.raw, flags=flags, msg_time=moment)
                self.dups.remember(folder, item.message_id)
                self.session.progressed()
                if self.skip_existing and not checked:
                    self.result.unchecked += 1
                return "imported"
            except (ImapConnectionError, ImapTimeoutError) as exc:
                try:
                    self.session.reconnect(exc)
                except (ImapConnectionError, ImapTimeoutError) as final:
                    # Сервер недоступен: сообщаем, сколько успели, а не
                    # теряем итог в общей «ошибке задания».
                    raise _Stop(f"связь с сервером потеряна ({final.message})", True) from final
                self.ensured.clear()
                self.dups.rebind(self.session.conn, folder)
            except MailArchiverError as exc:
                if _is_quota_error(exc):
                    raise _Stop(f"на сервере кончилось место в ящике ({exc.message})", True) from exc
                self.result.add_error(f"{item.label}: {exc.message}")
                return "error"
        self.result.add_error(f"{item.label}: связь обрывалась при каждой попытке")
        return "error"

    def finish(self) -> None:
        pass

    def close(self) -> None:
        self.result.reconnects = self.session.reconnects
        self.session.close()


class _LocalTarget:
    """Запись в локальную копию ящика (архив) — без сервера."""

    name = "local"
    delimiter = "/"

    def __init__(self, svc, account, prefix: str, skip_existing: bool, result: ImportResult,
                 event: EventCB) -> None:
        from ..queue.jobs import effective_retention_days
        self.svc = svc
        self.account = account
        self.prefix = prefix
        self.skip_existing = skip_existing
        self.result = result
        self.rows: List[tuple] = []
        self.pending_hashes: set = set()          # (папка, SHA-256) писем, ещё не записанных в индекс
        self.next_uid: Dict[str, int] = {}
        days = effective_retention_days(svc, account)
        result.retention_days = days
        self.cutoff = (time.time() - days * 86400) if days > 0 else None
        if self.cutoff is not None:
            event("WARNING", f"У ящика срок хранения локальных копий {days} дн.: письма старше него в архив не "
                             f"записываются — их удалила бы ближайшая ночная очистка. Чтобы импортировать "
                             f"весь файл, поставьте архив на удержание или задайте ящику «хранить всё».")

    def folder(self, path: Tuple[str, ...]) -> str:
        return folder_name(self.prefix, path, self.delimiter)

    def put(self, folder: str, item: ImportItem) -> str:
        """Записать письмо. → imported | duplicate | outdated | error. Бросает :class:`_Stop`."""
        from ..imap.backup import BackupEngine
        db, store, acc = self.svc.db, self.svc.store, self.account
        if self.cutoff is not None and item.internaldate and item.internaldate < self.cutoff:
            return "outdated"
        # Повтор — то же содержимое в той же папке: письмо, скопированное в файле в
        # две папки, остаётся в обеих, а повторный импорт того же файла ничего не удваивает.
        digest = sha256_hex(item.raw)
        if self.skip_existing and ((folder, digest) in self.pending_hashes
                                   or db.hash_exists_in_folder(acc.id, folder, digest)):
            return "duplicate"
        if folder not in self.next_uid:
            self.next_uid[folder] = db.max_uid(acc.id, folder, IMPORTED_UIDVALIDITY)
        uid = self.next_uid[folder] + 1
        try:
            rel, sha, size = store.store_message(acc.id, folder, self.delimiter, uid, item.raw,
                                                 flags=item.flags, internaldate=item.internaldate)
        except DiskSpaceError as exc:
            # файл не сохраняем: он часто лежит на том же диске, где только что кончилось место
            raise _Stop(f"на диске архива кончилось место ({exc.message})", False) from exc
        except MailArchiverError as exc:
            self.result.add_error(f"{item.label}: {exc.message}")
            return "error"
        self.next_uid[folder] = uid
        subject, from_addr, has_attach = BackupEngine._extract_meta(item.raw)  # noqa: SLF001
        message_id = BackupEngine._extract_message_id(item.raw)  # noqa: SLF001
        self.rows.append((acc.id, folder, IMPORTED_UIDVALIDITY, uid, message_id, size,
                          BackupEngine._epoch_to_iso(item.internaldate) if item.internaldate else "",  # noqa: SLF001
                          ",".join(item.flags), rel, sha, subject[:500], from_addr[:300],
                          1 if has_attach else 0))
        self.pending_hashes.add((folder, digest))
        if len(self.rows) >= INDEX_BATCH:
            self.flush()
        return "imported"

    def flush(self) -> None:
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        self.pending_hashes.clear()
        inserted = self.svc.db.add_message_index_batch(rows)
        if inserted != len(rows):
            # Письма записаны на диск, а строк в индексе меньше: номера заняты кем-то ещё.
            # Очередь не запускает два задания над архивом одного ящика, так что это сбой —
            # молчать о нём нельзя: в просмотре и поиске таких писем не будет.
            raise MailArchiverError(
                f"В индекс записано {inserted} писем из {len(rows)}: номера писем в архиве ящика уже заняты.",
                code="import_error",
                hint="Запустите у ящика «Проверить целостность копии» и повторите импорт.")

    def finish(self) -> None:
        """Дописать индекс. Сбой здесь — сбой задания: без строк индекса писем в архиве не видно."""
        self.flush()

    def close(self) -> None:
        try:
            self.flush()                     # путь ошибки и отмены: сохраняем, что успели
        finally:
            if self.result.imported:
                from ..analytics import invalidate_mail_analytics_cache
                invalidate_mail_analytics_cache()


# ---------------------------------------------------------------------------
#  Импорт
# ---------------------------------------------------------------------------
def run_import(svc, account, pst_path: str, *, target: str = "imap", prefix: str = DEFAULT_PREFIX,
               skip_existing: bool = True, engine: str = "builtin", ansi_codec: str = "cp1251",
               event: EventCB, progress: ProgressCB, cancelled: CancelCB,
               max_events: int = 100) -> ImportResult:
    """Импортировать .pst в ящик ``account``. Итог — :class:`ImportResult`.

    Не бросает исключений из-за отдельных писем: нечитаемое письмо считается
    ошибкой и импорт идёт дальше. Бросает, если файл не открывается вовсе
    (:class:`ValidationError`), цель недоступна с самого начала или задание
    отменено (:class:`JobCancelled`).
    """
    if target not in TARGETS:
        raise ValidationError("Неизвестная цель импорта.", hint="Выберите «на почтовый сервер» или "
                                                                 "«в локальную копию».")
    if engine not in ENGINES:
        raise ValidationError("Неизвестный способ разбора файла .pst.")
    if ansi_codec not in ANSI_CODECS:
        ansi_codec = "cp1251"
    result = ImportResult(target=target, engine=engine, skip_existing=bool(skip_existing))
    shown = [0]

    def report(level: str, text: str, log_text: str) -> None:
        """Сообщение о письме: в журнал задания — с темой, в журнал службы — без неё."""
        shown[0] += 1
        if shown[0] <= max_events:
            event(level, text)
        elif shown[0] == max_events + 1:
            event("WARNING", "Сообщений об отдельных письмах больше — остальные только в итоге и в журнале службы.")
        log.warning("import_pst %s: %s", account.name, log_text)

    def check_cancel() -> None:
        if cancelled():
            raise JobCancelled(f"Импорт отменён пользователем (перенесено {result.imported} из {result.total}).")

    reader: Optional[PstReader] = None
    tmp_dir = ""
    sink = None
    try:
        if engine == "readpst":
            tmp_dir = tempfile.mkdtemp(prefix="pstimport_", dir=svc.cfg.tmp_dir)
            event("INFO", "Файл разбирает внешняя утилита readpst…")
            run_readpst(pst_path, tmp_dir, cancelled)
            result.total, items = readpst_source(tmp_dir)
            result.description = "разобран утилитой readpst"
        else:
            try:
                reader = PstReader(pst_path, ansi_codec=ansi_codec)
                result.total, items = builtin_source(reader, check_cancel)
            except PstUnsupportedError as exc:
                raise ValidationError(f"Файл не разобран: {exc}.",
                                      hint="Проверьте, что это файл данных Outlook (.pst). Если файл открывается "
                                           "в Outlook, попробуйте способ разбора «readpst».") from exc
            except _MESSAGE_ERRORS as exc:
                raise ValidationError(f"Файл .pst повреждён и не читается: {exc}.",
                                      hint="Проверьте файл средством восстановления Outlook (scanpst.exe) и "
                                           "загрузите его заново. Если файл открывается в Outlook, попробуйте "
                                           "способ разбора «readpst».") from exc
            result.description = reader.describe()
            name = reader.store_name()
            event("INFO", f"Файл: {result.description}" + (f", хранилище «{name}»" if name else "") + ".")
        event("INFO", f"В файле найдено писем и других элементов: {result.total}.")

        if target == "local":
            sink = _LocalTarget(svc, account, prefix, skip_existing, result, event)
        else:
            sink = _ImapTarget(svc, account, prefix, skip_existing, result, event, check_cancel)

        done = 0
        for item in items:
            check_cancel()
            done += 1
            if item.not_mail:
                result.not_mail += 1
            elif item.error:
                result.add_error(f"{item.label}: {item.error}")
                report("ERROR", f"Не прочитано {item.label}: {item.error}",
                       f"не прочитано {item.log_label}: {item.error}")
            else:
                before = result.errors
                try:
                    outcome = sink.put(sink.folder(item.folder), item)
                except _Stop as exc:
                    result.stopped = str(exc)
                    result.keep_file = exc.keep_file
                    break
                if outcome == "imported":
                    result.imported += 1
                    result.imported_bytes += len(item.raw)
                    if item.lost:
                        result.lossy += 1
                        report("ERROR", f"Перенесено с потерями {item.label}: " + "; ".join(item.lost[:5]),
                               f"перенесено с потерями {item.log_label}: потерь {len(item.lost)}")
                    elif item.notes:
                        result.noted += 1
                        report("WARNING", f"Перенесено не целиком {item.label}: " + "; ".join(item.notes[:5]),
                               f"перенесено не целиком {item.log_label}: замечаний {len(item.notes)}")
                elif outcome == "duplicate":
                    result.duplicates += 1
                elif outcome == "outdated":
                    result.outdated += 1
                elif result.errors > before:
                    report("ERROR", result.last_error, f"не перенесено {item.log_label}")
            if done % 20 == 0 or done == result.total:
                progress(done, result.total, f"Импорт {done}/{result.total}")
        if reader is not None:
            result.damaged = reader.damaged
        sink.finish()
        return result
    finally:
        if sink is not None:
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001 — итог импорта важнее сбоя при закрытии
                log.warning("import_pst %s: при завершении: %s", account.name, exc)
        if reader is not None:
            reader.close()
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def summarize(result: ImportResult) -> str:
    """Итог импорта одной строкой — для карточки задания."""
    where = "в локальную копию" if result.target == "local" else "на сервер"
    text = f"Импортировано из PST {where}: {result.imported} из {result.total}"
    if result.imported_bytes:
        from ..util import human_size
        text += f" ({human_size(result.imported_bytes)})"
    text += "."
    if result.duplicates:
        text += (f" Уже были в архиве и не записаны повторно: {result.duplicates}." if result.target == "local"
                 else f" Уже были в папках назначения и не залиты повторно: {result.duplicates}.")
    if result.not_mail:
        text += f" Не письма (контакты, календарь, задачи, заметки) — пропущено: {result.not_mail}."
    if result.outdated:
        text += (f" Старше срока хранения ящика ({result.retention_days} дн.) — не записано: {result.outdated}; "
                 f"чтобы импортировать и их, поставьте архив на удержание или задайте «хранить всё».")
    if result.errors:
        text += f" Ошибок: {result.errors}."
    if result.lossy:
        text += (f" Перенесено с потерями (не прочиталось вложение или тело письма): {result.lossy} — письма "
                 f"названы в журнале задания.")
    if result.noted:
        text += f" Писем, из которых не перенесены объекты OLE или вложения-ссылки: {result.noted}."
    if result.unchecked:
        text += (f" Без проверки на повтор залито: {result.unchecked} (у письма нет Message-ID или сервер не "
                 f"дал список писем папки).")
    if result.stopped:
        text += f" Импорт остановлен: {result.stopped} — не перенесено {result.left}."
        if not result.skip_existing:
            text += " При повторной загрузке файла уже перенесённые письма запишутся ещё раз."
        elif result.target == "local":
            text += " Импорт можно повторить: уже записанные письма второй раз не запишутся."
        else:
            text += " Импорт можно повторить: письма, которые уже есть в папках назначения, второй раз не зальются."
    if result.reconnects:
        text += f" Переподключений после обрыва связи: {result.reconnects}."
    if result.damaged:
        text += (f" В файле блоков с неверной контрольной суммой: {result.damaged} — файл повреждён, письма из "
                 f"таких блоков могли исказиться.")
    return text
