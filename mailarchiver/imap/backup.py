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

Ошибки на уровне отдельной папки не прерывают весь бэкап — они логируются и
учитываются в счётчике ошибок, остальные папки продолжают копироваться.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from ..errors import JobCancelled, MailArchiverError
from ..logging_setup import get_logger
from ..models import Account
from ..storage import MaildirStore
from .client import ConnectOptions, ImapConnection

log = get_logger("backup")

ProgressCB = Callable[[int, int, str, int, float], None]
CancelCB = Callable[[], bool]
EventCB = Callable[[str, str], None]  # (level, message)


# Сколько имён пропущенных папок показывать в итоговой строке целиком.
MAX_SKIPPED_FOLDERS_IN_SUMMARY = 10


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
    cancelled: bool = False

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


def _folder_matches(name: str, patterns: List[str], delimiter: str) -> bool:
    for p in patterns or ():
        if not p:
            continue
        if name == p or name.lower() == p.lower():
            return True
        if name.startswith(p + delimiter) or name.lower().startswith((p + delimiter).lower()):
            return True
    return False


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
        """Папка открылась — забыть её историю неудач."""
        try:
            self.db.clear_folder_problem(account.id, folder)
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

    def run(self, account: Account, *, progress_cb: Optional[ProgressCB] = None,
            cancel_cb: Optional[CancelCB] = None, event_cb: Optional[EventCB] = None) -> BackupResult:
        result = BackupResult()
        started = time.time()
        # Подробное объяснение про нечитаемые папки выводим один раз за прогон.
        self._explained_unreadable = False

        def emit(level: str, msg: str) -> None:
            log.log({"INFO": 20, "WARNING": 30, "ERROR": 40}.get(level, 20), "[%s] %s", account.name, msg)
            if event_cb:
                event_cb(level, msg)

        def check_cancel() -> None:
            if cancel_cb and cancel_cb():
                raise JobCancelled("Бэкап отменён пользователем.")

        emit("INFO", f"Подключение к ящику «{account.name}» ({account.host})…")
        with ImapConnection(account, self.options) as conn:
            folders = self._select_folders(conn, account, emit)
            result.folders_total = len(folders)
            emit("INFO", f"Найдено папок к копированию: {len(folders)}")

            # --- Фаза планирования ---
            plan: List[Dict] = []
            total_new = 0
            for f in folders:
                check_cancel()
                try:
                    info = conn.select(f.name, readonly=True)
                except MailArchiverError as exc:
                    # Папка не открылась. Прежде чем объявлять копию неполной,
                    # спрашиваем сервер командой STATUS: сколько писем он вообще
                    # видит в этой папке. Пустая папка, которую не открыть, —
                    # это мусор на сервере, а не потеря писем.
                    status = conn.folder_status(f.name)
                    twin = _duplicate_hint(f.name, [x.name for x in folders])
                    children = conn.folder_children(f.name, f.delimiter)
                    if children:
                        # Папка-контейнер: письма лежат во вложенных папках, а
                        # они копируются сами по себе. Ошибкой это не считаем —
                        # иначе каждый ящик с деревом папок вечно числился бы
                        # скопированным частично.
                        self._forget_unreadable(account, f.name)
                        result.container_folders.append(f.name)
                        emit("INFO",
                             f"Папка «{f.name}» не открывается, но у неё есть вложенные папки "
                             f"({len(children)}) — это папка-контейнер, своих писем она не хранит; "
                             f"вложенные папки копируются отдельно.")
                        continue
                    if status is not None and status.get("messages") == 0:
                        self._forget_unreadable(account, f.name)
                        result.empty_unreadable_folders.append(f.name)
                        emit("WARNING",
                             f"Папка «{f.name}» не открывается, но по данным сервера она ПУСТАЯ "
                             f"(0 писем) — копировать нечего, потерь нет.{twin}")
                        continue
                    # Письма этой папки в копию НЕ попадут. В журнал пишем
                    # КОРОТКУЮ строку: длинное объяснение упирается в предел
                    # длины события и обрезается ровно на счётчике прогонов,
                    # то есть на самом нужном. Подробности выводятся ниже, один
                    # раз за прогон.
                    detail = exc.message + (f" {exc.hint}" if exc.hint else "")
                    reply = getattr(exc, "server_reply", "") or exc.message
                    verdict = getattr(exc, "verdict", "")
                    msgs = getattr(exc, "status_messages", None)
                    fails = self._note_unreadable(account, f.name, exc.message)
                    where = (f"писем в ней по данным сервера: {msgs}" if msgs
                             else "сколько в ней писем, сервер не сообщает")
                    body = f"не открывается: {reply}; {where}"
                    if verdict:
                        body += f"; {verdict}"
                    if self.unreadable_grace_runs and fails > self.unreadable_grace_runs:
                        # Папка не открывается уже давно — это состояние сервера,
                        # а не новость. Продолжаем пробовать и сообщать, но не
                        # помечаем задание неуспешным: иначе «копия неполная»
                        # висит вечно и перестаёт что-либо значить.
                        result.known_unreadable_folders.append(f.name)
                        since = self._unreadable_since(account, f.name)
                        emit("WARNING",
                             f"Папка «{f.name}» {body}. Не открывается {fails}-й прогон "
                             f"подряд{since} — ошибкой больше не считаем.{twin}")
                        self._explain_unreadable(emit, exc, duplicate=bool(twin))
                        continue
                    result.errors += 1
                    result.skipped_folders.append(f.name)
                    result.error_details.append(f"Папка «{f.name}»: {detail}")
                    left = (self.unreadable_grace_runs - fails + 1) if self.unreadable_grace_runs else 0
                    tail = (f" Не открывается {fails}-й прогон подряд; ещё {left} — и папка перейдёт "
                            f"в «известные нечитаемые» (задание перестанет помечаться неполным)."
                            if left > 0 else "")
                    emit("WARNING", f"Пропуск папки «{f.name}»: {body}.{twin}{tail}")
                    self._explain_unreadable(emit, exc, duplicate=bool(twin))
                    continue
                result.folders_read += 1
                self._forget_unreadable(account, f.name)
                uidvalidity = info["uidvalidity"]
                state = self.db.get_folder_state(account.id, f.name)
                old_uidvalidity = int(state["uidvalidity"]) if state else 0
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
                # existing_uids запрашиваем с НОВЫМ uidvalidity: при смене набор
                # окажется пустым и папка скачается целиком — это и требуется.
                existing = self.db.existing_uids(account.id, f.name, uidvalidity)
                all_uids = conn.search_all_uids()
                new_uids = [u for u in all_uids if u not in existing]
                new_uids.sort()
                if new_uids:
                    plan.append({"folder": f, "uidvalidity": uidvalidity, "uids": new_uids, "server_count": info["exists"]})
                    total_new += len(new_uids)
                # обновим общее число писем в папке (даже если новых нет)
                self.db.upsert_folder_state(account.id, f.name, uidvalidity,
                                            max(all_uids) if all_uids else 0, info["exists"])

            result.messages_total = sum(len(p["uids"]) for p in plan)
            emit("INFO", f"Новых писем к загрузке: {total_new}")
            if progress_cb:
                progress_cb(0, total_new, "Планирование завершено", 0, 0.0)

            # --- Фаза загрузки ---
            done = 0
            bytes_done = 0

            def on_skipped(uid: int, size: int) -> None:
                """
                Письмо отсеяно по размеру ещё ДО скачивания (см.
                ImapConnection.fetch_messages). Учитываем его в done, иначе
                прогресс никогда не дойдёт до 100 %.
                """
                nonlocal done
                result.messages_skipped += 1
                done += 1
                emit("WARNING", f"Письмо UID {uid} пропущено (больше лимита размера: {size} Б).")

            planned_done = 0  # сколько писем «прошло» по плану (для прогресса)
            for p in plan:
                check_cancel()
                f = p["folder"]
                planned_done += len(p["uids"])
                emit("INFO", f"Папка «{f.name}»: загрузка {len(p['uids'])} писем…")
                try:
                    sel = conn.select(f.name, readonly=True)
                    cur_uidvalidity = sel["uidvalidity"]
                    if cur_uidvalidity and cur_uidvalidity != p["uidvalidity"]:
                        # UIDVALIDITY сменился между планированием и загрузкой:
                        # запланированные UID теперь указывают на ЧУЖИЕ письма.
                        msg = (f"Папка «{f.name}»: UIDVALIDITY изменился между планированием и загрузкой "
                               f"({p['uidvalidity']} → {cur_uidvalidity}) — папка пропущена, "
                               f"будет скачана при следующем запуске.")
                        result.errors += 1
                        result.error_details.append(msg)
                        emit("ERROR", msg)
                        continue
                    max_uid = 0
                    # Лимит размера передаём в клиент: слишком крупные письма
                    # отсеиваются по ответу (RFC822.SIZE) и вообще не качаются.
                    for msg in conn.fetch_messages(p["uids"], skip_larger_than=self.skip_larger_than,
                                                   on_skipped=on_skipped):
                        check_cancel()
                        uid = msg["uid"]
                        raw = msg["raw"]
                        if self.skip_larger_than and len(raw) > self.skip_larger_than:
                            # Подстраховка: сервер мог не сообщить RFC822.SIZE
                            # или сообщить заниженный размер.
                            result.messages_skipped += 1
                            done += 1
                            emit("WARNING", f"Письмо UID {uid} пропущено (больше лимита размера).")
                            continue
                        flags = msg["flags"] if self.download_flags else []
                        try:
                            relpath, digest, size = self.store.store_message(
                                account.id, f.name, f.delimiter, uid, raw,
                                flags=flags, internaldate=msg["internaldate"],
                            )
                        except MailArchiverError as exc:
                            result.errors += 1
                            result.error_details.append(f"UID {uid} в «{f.name}»: {exc.message}")
                            emit("ERROR", f"Ошибка сохранения UID {uid}: {exc.message}")
                            # письмо обработано (пусть и с ошибкой) — иначе
                            # прогресс-бар не дойдёт до 100 %
                            done += 1
                            continue
                        subject, from_addr, has_attach = self._extract_meta(raw)
                        self.db.add_message_index(
                            account.id, f.name, p["uidvalidity"], uid,
                            self._extract_message_id(raw), size,
                            self._epoch_to_iso(msg["internaldate"]),
                            ",".join(flags), relpath, digest,
                            subject=subject, from_addr=from_addr, has_attach=has_attach,
                        )
                        result.messages_new += 1
                        result.bytes_new += size
                        bytes_done += size
                        done += 1
                        max_uid = max(max_uid, uid)
                        if progress_cb and (done % 10 == 0 or done == total_new):
                            elapsed = max(0.001, time.time() - started)
                            progress_cb(done, total_new, f"«{f.name}»: {done}/{total_new}", bytes_done, bytes_done / elapsed)
                    if max_uid:
                        st = self.db.get_folder_state(account.id, f.name)
                        cur_max = st["last_uid"] if st else 0
                        self.db.upsert_folder_state(account.id, f.name, p["uidvalidity"],
                                                    max(cur_max, max_uid), p["server_count"])
                    result.folders_processed += 1
                except JobCancelled:
                    raise
                except MailArchiverError as exc:
                    result.errors += 1
                    result.error_details.append(f"Папка «{f.name}»: {exc.message}")
                    emit("ERROR", f"Ошибка в папке «{f.name}»: {exc.message}")
                finally:
                    # Папка пройдена — целиком, с ошибкой или пропущена: её
                    # запланированные письма больше не «в работе». Без этого
                    # прогресс-бар застревал бы ниже 100 %.
                    done = max(done, planned_done)

            if progress_cb:
                elapsed = max(0.001, time.time() - started)
                progress_cb(done, total_new, "Готово", bytes_done, bytes_done / elapsed)

        summary = (f"Бэкап завершён: новых писем {result.messages_new}, "
                   f"пропущено по размеру {result.messages_skipped}, ошибок {result.errors}.")
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
                        f"({len(result.container_folders)}): {shown}{tail} — своих писем они не "
                        f"хранят, вложенные папки скопированы.")
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
        emit("WARNING" if result.skipped_folders else "INFO", summary)
        return result

    @staticmethod
    def _extract_message_id(raw: bytes) -> str:
        # Быстрый поиск заголовка Message-ID без полного парсинга письма
        try:
            head = raw[:8192].decode("latin-1", "ignore")
        except Exception:  # noqa: BLE001
            return ""
        for line in head.splitlines():
            if line.lower().startswith("message-id:"):
                return line.split(":", 1)[1].strip()[:250]
        return ""

    @staticmethod
    def _extract_meta(raw: bytes):
        """Извлечь тему, отправителя и признак вложений (для списка писем)."""
        subject, from_addr, has_attach = "", "", 0
        try:
            from email import policy
            from email.parser import BytesHeaderParser
            from email.utils import parseaddr
            # policy.default корректно декодирует как RFC 2047, так и «сырые» UTF-8 заголовки
            hdrs = BytesHeaderParser(policy=policy.default).parsebytes(raw)
            subject = str(hdrs.get("Subject", "") or "")
            from_raw = str(hdrs.get("From", "") or "")
            _name, addr = parseaddr(from_raw)
            # в БД нужен нормализованный адрес; сырой заголовок — только если
            # адрес выделить не удалось
            from_addr = addr or from_raw
            ctype = (str(hdrs.get("Content-Type", "")) or "").lower()
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
