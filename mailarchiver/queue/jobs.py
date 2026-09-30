"""
Обработчики заданий. Каждый обработчик получает :class:`JobContext` и
возвращает словарь-результат. Исключения означают сбой (обрабатывает
:class:`~mailarchiver.queue.manager.QueueManager`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ..accountops import hold_label
from ..errors import ExportError, JobCancelled, MailArchiverError, ValidationError
from ..imap.backup import BackupEngine
from ..imap.client import probe_account
from ..imap.restore import RestoreEngine
from ..export import resolve_engine
from ..export.base import zip_directory, zip_files
from ..logging_setup import get_logger
from ..models import JobStatus, JobType
from ..analytics import invalidate_mail_analytics_cache
from ..util import human_size, safe_filename
from ..version import __version__

log = get_logger("jobs")

# Размер страницы при обходе индекса писем.
MESSAGE_PAGE_SIZE = 2000


def _rel_to_imap_folder(rel_folder: str, delimiter: str) -> str:
    """
    Перевести относительный путь каталога (как его отдал readpst) в имя
    IMAP-папки: разделители ОС («/» и «\\») заменяются на разделитель иерархии
    сервера, пустые сегменты и «.» отбрасываются.
    """
    parts = [p for p in re.split(r"[\\/]+", rel_folder or "") if p and p != "."]
    return (delimiter or "/").join(parts)


def _count_older_than(db, account_id: int, cutoff_iso: str) -> int:
    return int(db.scalar(
        "SELECT COUNT(*) FROM messages WHERE account_id=? AND internaldate IS NOT NULL "
        "AND internaldate<>'' AND internaldate<?",
        (account_id, cutoff_iso),
    ) or 0)


def _page_older_than(db, account_id: int, cutoff_iso: str, after_id: int, limit: int) -> list:
    """Порция писем ящика старше cutoff (для ретеншна), по возрастанию id.

    Обход по id, а не «самые старые сначала»: письмо, файл которого удалить не
    удалось, остаётся в индексе, и выборка «самых старых» возвращала бы его
    снова и снова.
    """
    return db.query(
        "SELECT id, stored_path, size FROM messages WHERE account_id=? AND internaldate IS NOT NULL "
        "AND internaldate<>'' AND internaldate<? AND id>? ORDER BY id LIMIT ?",
        (account_id, cutoff_iso, after_id, limit),
    )


def effective_retention_days(svc, acc) -> int:
    """Срок хранения ящика в днях (0 — хранить всё).

    Свой срок ящика действует всегда. Ящик со сроком «как в общих настройках»
    чистится по retention.keep_days, только если общая очистка ВКЛЮЧЕНА
    (retention.enabled): раньше этот переключатель ни на что не влиял, а общий
    срок применялся лишь к ящикам, у которых случайно было своё расписание очистки.
    """
    if acc.on_hold():
        # Архив удерживается (уволенный сотрудник, решение администратора):
        # сроки хранения не действуют, пока не истечёт удержание.
        return 0
    if acc.retention_days is not None and acc.retention_days >= 0:
        return int(acc.retention_days)
    if not bool(svc.rt("retention", "enabled")):
        return 0
    try:
        return max(0, int(svc.rt("retention", "keep_days") or 0))
    except (TypeError, ValueError):
        return 0


def retention_cutoff_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc).timestamp() - int(days) * 86400
    return datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()


class JobContext:
    def __init__(self, services, job_id: int, job_type: str, account_id: Optional[int], params: Dict) -> None:
        self.services = services
        self.db = services.db
        self.job_id = job_id
        self.job_type = job_type
        self.account_id = account_id
        self.params = params or {}
        self._last_progress = 0.0
        # Кеш проверки отмены: см. is_cancelled().
        self._cancel_checked = 0.0
        self._cancel_cached = False

    def progress(self, current: int, total: int, message: str = "", bytes_done: int = 0, speed: float = 0.0) -> None:
        now = time.time()
        # не чаще ~2 раз в секунду, чтобы не грузить БД
        if now - self._last_progress < 0.5 and current != total:
            return
        self._last_progress = now
        self.db.update_job_progress(self.job_id, current, total, message, bytes_done, speed)

    def event(self, level: str, message: str) -> None:
        self.db.add_job_event(self.job_id, level, message)
        # Событие — признак «этап сменился»: сбрасываем кеш отмены, чтобы
        # реакция на кнопку «Отмена» не задерживалась на границах этапов.
        self._cancel_checked = 0.0

    def is_cancelled(self) -> bool:
        """Запрошена ли отмена задания.

        Ответ кешируется на полсекунды: проверка вызывается на КАЖДОЕ письмо, а
        это отдельный запрос к базе — на ящике в 200 000 писем набегает столько
        же лишних обращений. Полсекунды задержки при отмене незаметны.
        """
        queue = getattr(self.services, "queue", None)
        if queue is not None and queue.is_shutting_down():
            # служба останавливается: задание прерывается и вернётся в очередь
            return True
        now = time.time()
        if self._cancel_checked and now - self._cancel_checked < 0.5:
            return self._cancel_cached
        self._cancel_cached = bool(self.db.is_cancel_requested(self.job_id))
        self._cancel_checked = now
        return self._cancel_cached

    def stop_reason(self) -> str:
        queue = getattr(self.services, "queue", None)
        if queue is not None and queue.is_shutting_down():
            return "Прервано остановкой службы (продолжится после запуска)"
        return "Отменено пользователем"


# ---------------------------------------------------------------------------
#  BACKUP
# ---------------------------------------------------------------------------
#: «Докачка потерянных» отказывается работать, если пропала бОльшая доля файлов:
#: так выглядит не потеря писем, а несмонтированный или подменённый диск.
REBUILD_MISSING_MAX_SHARE = 0.5


def _rebuild_missing(ctx: JobContext, acc) -> int:
    """Убрать из индекса письма, файлов которых на диске больше нет.

    После этого обычная копия скачает их заново. Нужно, когда файлы потеряны
    (сбой диска, чужая уборка, оборванный прогон): пока запись в индексе есть,
    письмо считается скачанным и сервер о нём больше не спрашивают.

    Защита от недоступного хранилища: если каталога ящика нет вовсе или
    «пропало» больше половины файлов, это почти наверняка не смонтированный
    диск, а не потеря писем. Раньше в таком случае из индекса удалялось ВСЁ —
    и записи о письмах, уже удалённых на сервере, пропадали безвозвратно
    (после возврата диска их файлы становились невидимыми «сиротами»).
    """
    store = ctx.services.store
    acc_dir = store.account_dir(acc.id)
    total = ctx.db.count_messages(acc.id)
    if total and not os.path.isdir(acc_dir):
        raise ValidationError(
            f"Каталог копии ящика не найден: {acc_dir}. Индекс не трогаем.",
            hint="Похоже, хранилище писем не смонтировано или перенесено. Верните каталог на место. "
                 "Если копия действительно утрачена целиком, используйте «Пересоздать копию с нуля».")
    lost, checked = [], 0
    for msg_id, _folder, rel in ctx.db.iter_message_paths(acc.id):
        checked += 1
        if ctx.is_cancelled():
            raise JobCancelled("Докачка отменена пользователем. Индекс не изменён.")
        if not rel:
            lost.append(msg_id)
            continue
        try:
            path = store.message_path(acc.id, rel)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                lost.append(msg_id)
        except (OSError, MailArchiverError):
            lost.append(msg_id)
        if checked % 20000 == 0:
            ctx.progress(0, 1, f"Проверено записей: {checked}")
    if checked >= 20 and len(lost) > checked * REBUILD_MISSING_MAX_SHARE:
        raise ValidationError(
            f"Файлов нет у {len(lost)} из {checked} писем — больше половины. Индекс не трогаем: "
            f"так выглядит недоступный диск, а не потеря отдельных писем.",
            hint="Проверьте, что хранилище писем смонтировано и доступно службе. Если копия "
                 "действительно утрачена, используйте «Пересоздать копию с нуля».")
    if lost:
        ctx.db.delete_message_indexes(lost)
    ctx.event("INFO", f"Проверено записей индекса: {checked}; потерянных файлов: {len(lost)}."
                      + (" Эти письма будут скачаны заново (если они ещё есть на сервере)."
                         if lost else " Всё на месте."))
    return len(lost)


def _rebuild_full(ctx: JobContext, acc) -> Dict[str, int]:
    """Стереть локальную копию ящика целиком и начать её заново.

    Порядок здесь важнее удобства:

    1. СНАЧАЛА проверяем, что сервер доступен и ящик открывается. Стирать копию
       раньше нельзя: при недоступном сервере или сменившемся пароле архив
       оказался бы уничтожен, а скачать взамен нечего;
    2. каталог с письмами не удаляем, а ПЕРЕИМЕНОВЫВАЕМ в карантин
       ``<ящик>_old_<дата>``. Если прогон оборвётся посередине, файлы ещё на
       диске — администратор сможет вернуть их руками;
    3. и только потом чистим индекс.

    Делается ТОЛЬКО по явной команде администратора: письма, которых уже нет на
    почтовом сервере, после этого не восстановить.
    """
    svc = ctx.services
    ctx.event("INFO", "Проверяем подключение к ящику перед стиранием копии…")
    probe = probe_account(acc, svc.connect_options())
    if not probe.get("ok"):
        raise ValidationError(
            f"Копию не стираем: ящик недоступен — {probe.get('error') or 'нет связи с сервером'}.",
            hint=probe.get("hint") or "Проверьте пароль и доступность почтового сервера, "
                                      "затем запустите пересоздание копии заново.")

    quarantine, files, freed = svc.store.quarantine_account_files(acc.id)
    # Возврат писем из ЛЮБОЙ прежней копии ждёт полного копирования после этого
    # момента: имя каталога хранит время лишь с точностью до секунды и только своё.
    from .. import quarantine as qmod
    from ..util import utcnow_iso
    ctx.db.set_meta(f"{qmod.REBUILD_META}{acc.id}", utcnow_iso())
    removed_index = ctx.db.purge_account_index(acc.id)
    invalidate_mail_analytics_cache()
    where = f" в «{os.path.basename(quarantine)}»" if quarantine else ""
    ctx.event("WARNING", f"Прежняя копия ящика убрана в карантин{where}: файлов {files} ({human_size(freed)}), "
                         f"записей индекса очищено {removed_index}. Скачиваем всё заново.")
    ctx.db.add_audit("system", "backup_rebuild_full",
                     f"{acc.name}: индекс {removed_index}, файлов {files}, карантин={quarantine or '—'}")
    return {"index": removed_index, "files": files, "bytes": freed,
            "quarantine": quarantine or ""}


#: Отметка «копия «с нуля» этим заданием уже начата»: meta-ключ + «<задание>:<ящик>».
_REBUILD_DONE = "rebuild_full_done:"


def _rebuild_full_once(ctx: JobContext, acc, emit) -> Dict:
    """Убрать прежнюю копию в карантин — ровно один раз на задание.

    Задание, прерванное перезапуском службы или упавшее и поставленное на
    повтор, приходит сюда снова с тем же «с нуля». Без отметки оно убрало бы в
    карантин уже скачанную часть НОВОЙ копии и начало бы всё заново — а с
    очередным перезапуском ещё раз.
    """
    key = f"{_REBUILD_DONE}{ctx.job_id}:{acc.id}"
    raw = ctx.db.get_meta(key)
    if raw:
        try:
            info = json.loads(raw)
        except (TypeError, ValueError):
            info = None
        if isinstance(info, dict):
            qname = os.path.basename(info.get("quarantine") or "")
            emit("INFO", "Копия «с нуля» этим заданием уже начата" + (f" (прежняя копия — в «{qname}»)" if qname else "")
                 + ": продолжаем скачивание, уже скачанное повторно не качается.")
            return dict(info, resumed=True)
    # Отметки заданий, которые так и не закончились, больше не нужны.
    active = {int(j["id"]) for j in ctx.db.active_jobs()}
    for row in ctx.db.query("SELECT key FROM meta WHERE key LIKE ?", (_REBUILD_DONE + "%",)):
        try:
            other = int(row["key"][len(_REBUILD_DONE):].split(":", 1)[0])
        except ValueError:
            continue
        if other != ctx.job_id and other not in active:
            ctx.db.delete_meta(row["key"])
    info = _rebuild_full(ctx, acc)
    ctx.db.set_meta(key, json.dumps(info, ensure_ascii=False))
    return info


def _compare_after_rebuild(ctx: JobContext, acc, rebuild_info: Dict, res, emit) -> str:
    """После удачной копии «с нуля» поставить в очередь сравнение прежней копии с новой.

    Отдельным заданием, а не здесь же: сравнение читает каждый файл прежней
    копии и на большом ящике идёт долго. Если бы оно шло внутри задания
    копирования, перезапуск службы в это время вернул бы в очередь САМО задание
    «с нуля» — и оно пересоздало бы уже готовую новую копию ещё раз. Новую копию
    с ошибками не сравниваем: непрочитанные папки выглядели бы «потерями».
    """
    path = rebuild_info.get("quarantine") or ""
    if not path:
        return ""
    if res.status_label != JobStatus.SUCCESS:
        return ("Прежнюю копию сравните с новой, когда копирование прочитает все папки ящика: меню ящика → "
                "«Прежние копии» → «Сравнить».")
    params = {"paths": [path], "after_rebuild": ctx.job_id}
    try:
        queue = getattr(ctx.services, "queue", None)
        if queue is not None:
            job_id = queue.enqueue(JobType.QUARANTINE_CHECK, acc.id, params, priority=6, created_by="system")
        else:
            job_id = ctx.db.enqueue_job(JobType.QUARANTINE_CHECK, acc.id, params, 6, 1, "system")
    except Exception as exc:  # noqa: BLE001 — сравнение не должно портить итог самой копии
        log.exception("Не удалось поставить сравнение прежней копии %s", path)
        emit("WARNING", f"Поставить сравнение прежней копии с новой не удалось: {getattr(exc, 'message', None) or exc}")
        return ""
    rebuild_info["check_job"] = job_id
    text = (f"Прежняя копия сравнится с новой отдельным заданием №{job_id} — итог будет в «Прежних копиях» "
            f"ящика.")
    emit("INFO", text)
    return text


def _note_login(db, acc, status: str, error: str = "") -> None:
    """Запомнить у ящика итог попытки входа (для отбора «неверный пароль»)."""
    if db is None or not getattr(acc, "id", None):
        return
    try:
        db.set_login_status(int(acc.id), status, error)
    except Exception:  # noqa: BLE001
        log.debug("Не удалось записать итог входа ящика %s", acc.id, exc_info=True)


def _require_credentials(acc, db=None) -> None:
    """Не ходить на сервер с пустым паролем.

    Ящики, заведённые синхронизацией сотрудников, создаются без пароля. Попытка
    входа с пустым паролем — это неудачный LOGIN на почтовом сервере с адреса
    архива каждую ночь, а fail2ban за такое банит IP целиком (и тогда падают
    бэкапы ВСЕХ ящиков).
    """
    from ..models import AuthType
    if acc.secret_broken:
        _note_login(db, acc, "secret_broken", "Пароль не расшифровывается текущим ключом (secret.key).")
        raise ValidationError(
            f"Пароль ящика «{acc.name}» не расшифровывается текущим ключом (secret.key).",
            hint="Откройте ящик и введите пароль заново.")
    if acc.auth_type == AuthType.OAUTH2:
        if not acc.oauth_refresh_token:
            _note_login(db, acc, "no_password", "Не задан refresh-токен OAuth2.")
            raise ValidationError(f"У ящика «{acc.name}» не задан refresh-токен OAuth2.",
                                  hint="Укажите refresh_token в карточке ящика.")
        return
    if acc.auth_type == AuthType.MASTER:
        # Пароль ящика не нужен: входит учётная запись администратора почты
        # (проверяется при подключении — там же понятная ошибка, если вход не настроен).
        if not (acc.host or "").strip():
            raise ValidationError(f"У ящика «{acc.name}» не указан адрес IMAP-сервера.",
                                  hint="Укажите сервер в карточке ящика.")
        return
    if not acc.password:
        _note_login(db, acc, "no_password", "Пароль не задан.")
        raise ValidationError(f"У ящика «{acc.name}» не задан пароль — на сервер не подключаемся.",
                              hint="Введите пароль в карточке ящика или загрузите файл паролей "
                                   "(раздел «Сотрудники»).")
    if not (acc.host or "").strip():
        raise ValidationError(f"У ящика «{acc.name}» не указан адрес IMAP-сервера.",
                              hint="Укажите сервер в карточке ящика.")


def handle_backup(ctx: JobContext) -> Dict:
    """Резервное копирование ящика.

    Параметр ``final`` — последняя копия уволенного сотрудника: после неё
    копирование ящика выключается (даже если она не удалась — ящик на сервере
    могли уже удалить). ``disable_after`` — «последняя копия» по команде
    администратора (групповые действия): копирование выключается, только если
    копия получилась, иначе ящик остаётся включённым, чтобы её можно было повторить.
    """
    if ctx.params.get("disable_after") and not ctx.params.get("final"):
        return _backup_then_disable(ctx)
    if not ctx.params.get("final"):
        return _handle_backup(ctx)
    try:
        result = _handle_backup(ctx)
    except JobCancelled:
        raise                    # отмена или остановка службы — решение ещё не принято
    except BaseException:
        _finish_dismissed(ctx, failed=True)
        raise
    _finish_dismissed(ctx, failed=False)
    result["summary"] = result.get("summary", "") + " Это последняя копия уволенного сотрудника — " \
                                                    "копирование ящика выключено."
    return result


def _backup_then_disable(ctx: JobContext) -> Dict:
    """Последняя копия по команде администратора: удалась — копирование выключается."""
    try:
        result = _handle_backup(ctx)
    except JobCancelled:
        raise
    except BaseException:
        ctx.event("WARNING", "Последняя копия не удалась — копирование ящика НЕ выключено. "
                             "Исправьте ошибку и повторите задание.")
        raise
    if result.get("final_status") in (JobStatus.SUCCESS, JobStatus.PARTIAL):
        acc = ctx.db.get_account(ctx.account_id)
        if acc is not None and acc.enabled:
            ctx.db.set_account_enabled(acc.id, False)
            ctx.db.add_audit("system", "account_disabled_after_final", acc.name)
        ctx.event("INFO", "Последняя копия сделана — копирование ящика выключено.")
        result["summary"] = result.get("summary", "") + " Это была последняя копия — копирование ящика выключено."
    else:
        ctx.event("WARNING", "Последняя копия не удалась — копирование ящика НЕ выключено.")
    return result


def _finish_dismissed(ctx: JobContext, failed: bool) -> None:
    acc = ctx.db.get_account(ctx.account_id)
    if acc is None or not acc.dismissed_at:
        return                   # сотрудника успели вернуть на работу
    ctx.db.set_account_auto_disabled(acc.id, True)
    ctx.event("WARNING" if failed else "INFO",
              "Сотрудник уволен: копирование ящика выключено" + (" (последняя копия не удалась — ящик мог быть "
                                                                  "уже удалён на сервере)." if failed else "."))
    ctx.db.add_audit("system", "account_auto_disabled", f"{acc.name}: последняя копия уволенного сотрудника"
                     + (" не удалась" if failed else ""))


def _handle_backup(ctx: JobContext) -> Dict:
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    # rebuild: "" — обычная копия; "missing" — вернуть потерянные файлы;
    # "full" — стереть локальную копию и скачать всё заново.
    rebuild = str(ctx.params.get("rebuild") or "").strip().lower()
    return _backup_account(ctx, acc, rebuild=rebuild)


def _backup_account(ctx: JobContext, acc, *, rebuild: str = "",
                    progress_cb: Optional[Callable] = None,
                    event_cb: Optional[Callable[[str, str], None]] = None) -> Dict:
    """Скопировать один ящик в рамках задания ``ctx``.

    Отдельно от обработчика задания — чтобы тем же кодом (с записью прогона,
    датами копий, статистикой и итогом входа) пользовалось копирование всех
    ящиков по очереди, где одно задание проходит сотни ящиков. ``progress_cb`` и
    ``event_cb`` позволяют такому заданию показывать ход по-своему.
    """
    svc = ctx.services
    emit = event_cb or ctx.event
    progress = progress_cb or ctx.progress
    label = {"missing": " (докачка потерянных писем)", "full": " (полностью заново)"}.get(rebuild, "")
    emit("INFO", f"Старт резервного копирования ящика «{acc.name}»{label}. "
                 f"MailArchiver {__version__}.")
    if rebuild == "full" and acc.on_hold():
        # Архив под удержанием (увольнение, проверка): стереть его ради копии
        # «с нуля» значило бы потерять письма, которых уже нет на сервере.
        raise ValidationError(
            f"Архив ящика «{acc.name}» удерживается ({hold_label(acc.hold_until)}) — копию «с нуля» не делаем.",
            hint="Если удержание больше не нужно, снимите его (меню ящика → «Удержание архива»). "
                 "Потерянные письма можно вернуть режимом «Докачать потерянные».")
    _require_credentials(acc, ctx.db)
    if acc.on_hold() and ctx.db.count_retired(acc.id):
        # Удержание могли поставить, пока шло прошлое копирование со сроком хранения:
        # оно успело бы запомнить старые письма как «не скачивать». Под удержанием
        # срок не действует — скачивается всё, что ещё лежит на сервере.
        ctx.db.clear_retired(acc.id)
    if svc.store.encryption_blocked:
        # Шифрование включено, а ключа нет: качать письма, которые всё равно
        # нельзя сохранить (открытым текстом — запрещено), бессмысленно.
        from ..errors import StorageError
        raise StorageError("Копирование не начато: " + svc.store.encryption_blocked,
                           hint="Верните файл ключа шифрования или выключите шифрование в "
                                "«Настройки → Хранилище».")
    run_id = ctx.db.start_run(acc.id, JobType.BACKUP, ctx.job_id)

    rebuild_info: Dict[str, int] = {}
    try:
        if rebuild == "missing":
            emit("INFO", "Сверяем индекс с файлами на диске…")
            rebuild_info["restored"] = _rebuild_missing(ctx, acc)
        elif rebuild == "full":
            rebuild_info = _rebuild_full_once(ctx, acc, emit)

        engine = BackupEngine(
            ctx.db, svc.store, svc.connect_options(),
            skip_larger_than_mb=int(svc.rt("backup", "skip_larger_than_mb") or 0),
            download_flags=bool(svc.rt("backup", "download_flags")),
            global_exclude=svc.rt("backup", "folder_exclude") or [],
            global_include=svc.rt("backup", "folder_include") or [],
            unreadable_grace_runs=int(svc.rt("backup", "unreadable_folder_grace_runs") or 0),
            # Письма старше срока хранения ящика не скачиваем: ночная очистка
            # всё равно удалила бы их, а ящик со сроком «3 дня» иначе при первом
            # копировании (и после «с нуля») тянул бы всю многолетнюю историю.
            retention_days=effective_retention_days(svc, acc),
        )
        res = engine.run(acc, progress_cb=progress, cancel_cb=ctx.is_cancelled, event_cb=emit)
    except JobCancelled:
        ctx.db.finish_run(run_id, JobStatus.CANCELLED, detail=ctx.stop_reason())
        raise
    except MailArchiverError as exc:
        # Любая другая ошибка (обрыв связи, отказ авторизации) тоже должна
        # закрыть запись прогона: иначе карточка ящика вечно показывает
        # «выполняется», а дневная статистика теряет этот день.
        ctx.db.finish_run(run_id, JobStatus.FAILED, detail=exc.message[:500])
        from ..errors import ImapAuthError
        if isinstance(exc, ImapAuthError):
            _note_login(ctx.db, acc, "auth_error", exc.message)
        ctx.db.note_backup_result(acc.id, JobStatus.FAILED)
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.db.finish_run(run_id, JobStatus.FAILED, detail=str(exc)[:500])
        ctx.db.note_backup_result(acc.id, JobStatus.FAILED)
        raise
    _note_login(ctx.db, acc, "ok")
    ctx.db.note_backup_result(acc.id, res.status_label)
    if not res.skipped_folders and not res.cancelled and res.folders_read > 0:
        # Все папки ящика прочитаны: по этой отметке прежние копии решают, можно ли
        # возвращать из них письма (ошибки отдельных писем полноту не нарушают).
        # Прогон, не прочитавший ни одной папки (пустой список папок на сервере,
        # «Копировать только» с опечаткой), полным не считается.
        ctx.db.note_complete_backup(acc.id)

    ctx.db.finish_run(run_id, res.status_label, messages_new=res.messages_new, bytes_new=res.bytes_new,
                      messages_total=res.messages_total, errors=res.errors,
                      detail="; ".join(res.error_details[:5]))
    ctx.db.bump_daily_stats(acc.id, messages=res.messages_new, bytes_=res.bytes_new, jobs=1, errors=res.errors)
    # Индекс изменился — готовая «Аналитика писем» устарела.
    invalidate_mail_analytics_cache()

    # Показываем ПРОЧИТАННЫЕ папки, а не только те, где нашлись новые письма:
    # на обычном прогоне последних ноль, и сводка «папок 0/50» выглядела так,
    # будто копирование ничего не проверило.
    summary = (f"Ящик «{acc.name}»: новых писем {res.messages_new} ({human_size(res.bytes_new)}), "
               f"папок прочитано {res.folders_read}/{res.folders_total}, ошибок {res.errors}.")
    if rebuild == "missing":
        summary += f" Докачка потерянных: возвращено в очередь {rebuild_info.get('restored', 0)} писем."
    elif rebuild == "full":
        qname = os.path.basename(rebuild_info.get("quarantine") or "")
        if rebuild_info.get("resumed"):
            summary += (f" Продолжение копии «с нуля»: прежние файлы — в карантине"
                        f"{f' «{qname}»' if qname else ''}, повторно не переносились.")
        else:
            summary += (f" Копия пересоздана с нуля: прежние файлы ({rebuild_info.get('files', 0)}) — в карантине"
                        f"{f' «{qname}»' if qname else ''}, записей индекса очищено {rebuild_info.get('index', 0)}.")
        compared = _compare_after_rebuild(ctx, acc, rebuild_info, res, emit)
        if compared:
            summary += " " + compared
    if res.messages_relinked:
        summary += (f" Сервер сменил нумерацию писем (UIDVALIDITY): узнано в архиве писем — "
                    f"{res.messages_relinked}, повторно они не скачивались.")
    if res.messages_outside_retention:
        summary += (f" Писем старше срока хранения ящика ({effective_retention_days(svc, acc)} дн.) "
                    f"не скачано: {res.messages_outside_retention}.")
    if res.messages_skipped:
        # письма, не скачанные из-за лимита размера, иначе «потерялись» бы без объяснений
        summary += f" Пропущено по лимиту размера: {res.messages_skipped}."
    if res.messages_failed:
        summary += f" Сервер не отдал писем: {res.messages_failed} (повторим при следующем прогоне)."
    if res.reconnects:
        summary += f" Переподключений после обрыва связи: {res.reconnects}."
    if res.known_unreadable_folders:
        shown = ", ".join(res.known_unreadable_folders[:10])
        more = len(res.known_unreadable_folders) - 10
        summary += (f" Известные нечитаемые папки ({len(res.known_unreadable_folders)}): {shown}"
                    f"{f' и ещё {more}' if more > 0 else ''} — сервер не открывает их давно.")
    if res.container_folders:
        summary += (f" Папок-контейнеров, которые сервер не открывает "
                    f"({len(res.container_folders)}): своих писем они не хранят.")
    if res.empty_unreadable_folders:
        # Пустые папки, которые сервер не даёт открыть, копию неполной не делают:
        # писем в них нет. Но администратор должен знать, что на сервере мусор.
        shown = ", ".join(res.empty_unreadable_folders[:10])
        more = len(res.empty_unreadable_folders) - 10
        summary += (f" Пустых папок, которые сервер не даёт открыть "
                    f"({len(res.empty_unreadable_folders)}): {shown}"
                    f"{f' и ещё {more}' if more > 0 else ''} — писем в них нет.")
    if res.skipped_folders:
        # Непрочитанные папки означают НЕПОЛНУЮ копию ящика — это должно быть
        # видно в карточке задания и в письме-уведомлении, а не только в логе.
        shown = ", ".join(res.skipped_folders[:10])
        more = len(res.skipped_folders) - 10
        summary += (f" КОПИЯ НЕПОЛНАЯ: не удалось прочитать папки "
                    f"({len(res.skipped_folders)}): {shown}{f' и ещё {more}' if more > 0 else ''}.")
    # авто-ретеншн истории прогонов
    keep_runs = int(svc.rt("retention", "keep_last_runs") or 30)
    if keep_runs:
        ctx.db.purge_old_runs(acc.id, keep_runs)
    return {"final_status": res.status_label, "summary": summary,
            "messages_new": res.messages_new, "bytes_new": res.bytes_new, "errors": res.errors,
            "skipped_folders": res.skipped_folders,
            "empty_unreadable_folders": res.empty_unreadable_folders,
            "container_folders": res.container_folders,
            "known_unreadable_folders": res.known_unreadable_folders,
            "messages_failed": res.messages_failed, "messages_vanished": res.messages_vanished,
            "messages_relinked": res.messages_relinked,
            "messages_outside_retention": res.messages_outside_retention,
            "reconnects": res.reconnects,
            "rebuild": rebuild or "", **({"rebuild_info": rebuild_info} if rebuild_info else {})}


# ---------------------------------------------------------------------------
#  RESTORE
# ---------------------------------------------------------------------------
def handle_restore(ctx: JobContext) -> Dict:
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    p = ctx.params
    ctx.event("INFO", f"Старт восстановления ящика «{acc.name}» (режим: {p.get('target_mode','original')}).")
    restore_id = ctx.db.create_restore(acc.id, p, ctx.job_id)

    engine = RestoreEngine(ctx.db, svc.store, svc.connect_options())
    try:
        res = engine.run(
            acc,
            folders=p.get("folders") or None,
            # Умолчание — БЕЗОПАСНЫЙ режим. Раньше здесь стояло "original":
            # задание без явных параметров (например из расписания, заведённого
            # старой версией) заливало архив прямо в рабочие папки ящика.
            target_mode=p.get("target_mode") or "prefixed",
            target_folder=p.get("target_folder", ""),
            target_prefix=p.get("target_prefix", "Восстановлено"),
            check_duplicates=bool(p.get("check_duplicates", True)),
            dry_run=bool(p.get("dry_run", False)),
            limit=int(p.get("limit", 0) or 0),
            progress_cb=ctx.progress, cancel_cb=ctx.is_cancelled, event_cb=ctx.event,
        )
    except JobCancelled:
        ctx.db.update_restore(restore_id, status=JobStatus.CANCELLED)
        raise
    except MailArchiverError as exc:
        # Иначе строка в таблице restores навсегда оставалась бы в статусе
        # "pending": пользователь видит вечно «выполняется».
        ctx.db.update_restore(restore_id, status=JobStatus.FAILED, error=exc.message[:500])
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.db.update_restore(restore_id, status=JobStatus.FAILED, error=str(exc)[:500])
        raise

    ctx.db.update_restore(restore_id, status=res.status_label, restored=res.restored, errors=res.errors,
                          error="; ".join(res.error_details[:5]))
    summary = f"Восстановлено {res.restored}, пропущено {res.skipped}, ошибок {res.errors}."
    if res.stopped:
        summary += f" Остановлено досрочно: {res.stopped}"
    if res.reconnects:
        summary += f" Переподключений после обрыва связи: {res.reconnects}."
    if res.dup_check_unavailable:
        summary += f" Без проверки дублей залито писем: {res.dup_check_unavailable} (возможны повторы)."
    return {"final_status": res.status_label, "summary": summary,
            "restored": res.restored, "skipped": res.skipped, "errors": res.errors, "dry_run": res.dry_run}


# ---------------------------------------------------------------------------
#  EXPORT
# ---------------------------------------------------------------------------
def handle_export(ctx: JobContext) -> Dict:
    """Выгрузка ящика в mbox/eml (zip) или .pst.

    Любой сбой (кончилось место, ошибка движка) и отмена наводят порядок:
    запись выгрузки получает итоговый статус, рабочий каталог (полная
    незашифрованная копия писем!) и недописанный архив удаляются. Раньше запись
    навсегда оставалась «в работе», а мусор — на диске.
    """
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    p = ctx.params
    engine_name = p.get("engine", "auto")
    fmt = p.get("format", "pst")
    folders = p.get("folders") or None
    date_from, date_to = p.get("date_from") or None, p.get("date_to") or None
    ctx.event("INFO", f"Старт экспорта ящика «{acc.name}» (движок: {engine_name}, формат: {fmt}).")

    engine = resolve_engine(engine_name, fmt)
    if svc.store.cipher is None:
        enc = int(svc.db.scalar("SELECT COUNT(*) FROM messages WHERE account_id=? AND stored_path LIKE '%.enc'",
                                (acc.id,)) or 0)
        if enc:
            raise ExportError(f"В копии ящика {enc} зашифрованных писем, а ключ шифрования не загружен — "
                              f"выгрузка была бы неполной.",
                              hint="Верните файл ключа (storage.encryption_key_file) и повторите экспорт.")
    total = svc.count_mail_items(acc.id, folders, date_from, date_to)
    if total == 0:
        raise ExportError("Нет писем для экспорта" + (" за выбранный период." if (date_from or date_to) else "."),
                          hint="Сначала выполните резервное копирование ящика или измените фильтр папок/дат.")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Номер задания в имени: две выгрузки ящиков с одинаковым названием
    # (однофамильцы), начатые в одну секунду, писали в один каталог и один
    # архив — почта разных людей смешивалась.
    base_name = f"{safe_filename(acc.name)}_{engine.fmt}_{ts}_{ctx.job_id}"
    job_row = ctx.db.get_job(ctx.job_id)
    author = (job_row["created_by"] if job_row is not None else "") or ""
    export_id = ctx.db.create_export(acc.id, engine.name, engine.fmt, "", p, ctx.job_id, created_by=author)
    options = {
        "pst_format": p.get("pst_format") or svc.rt("export", "pst_format"),
        "aspose_license_path": svc.rt("export", "aspose_license_path"),
        "tmp_dir": svc.cfg.tmp_dir,
        "pst_split_size_mb": p.get("pst_split_size_mb") or svc.rt("export", "pst_split_size_mb"),
    }
    skipped: List[str] = []

    def on_skip(row, text: str) -> None:
        skipped.append(f"{row['folder']}: {text}")
        if len(skipped) <= 20:
            ctx.event("ERROR", f"Письмо не выгружено ({row['folder']}, UID {row['uid']}): {text}")

    items = svc.iter_mail_items(acc.id, folders, date_from, date_to, int(p.get("limit", 0) or 0),
                                on_skip=on_skip)
    work_dir = ""
    final_path = ""
    produced: List[str] = []
    ok = False
    try:
        if engine.fmt in ("eml", "mbox"):
            work_dir = tempfile.mkdtemp(prefix="export_", dir=svc.cfg.tmp_dir)
            res = engine.export(items, work_dir, options=options, progress_cb=ctx.progress,
                                cancel_cb=ctx.is_cancelled, total_hint=total)
            if ctx.is_cancelled():
                raise JobCancelled("Экспорт отменён пользователем.")
            final_path = os.path.join(svc.cfg.exports_dir, base_name + ".zip")
            ctx.event("INFO", "Упаковка результата в ZIP-архив…")
            size = zip_directory(work_dir, final_path, arc_root=base_name, cancel_cb=ctx.is_cancelled)
        else:
            final_path = os.path.join(svc.cfg.exports_dir, base_name + "." + engine.fmt)
            res = engine.export(items, final_path, options=options, progress_cb=ctx.progress,
                                cancel_cb=ctx.is_cancelled, total_hint=total)
            produced = list(res.parts or []) + [final_path]
            if ctx.is_cancelled():
                raise JobCancelled("Экспорт отменён пользователем.")
            if len(res.parts) > 1:
                # .pst разбит на части (предел формата ANSI) — отдаём одним архивом
                ctx.event("INFO", f"Файл разбит на {len(res.parts)} частей, упаковка в ZIP-архив…")
                zip_path = os.path.join(svc.cfg.exports_dir, base_name + ".zip")
                produced.append(zip_path)
                zip_files(res.parts, zip_path, arc_root=base_name, cancel_cb=ctx.is_cancelled)
                for part in res.parts:
                    _unlink_quiet(part)
                final_path = zip_path
            size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
        ok = True
    except JobCancelled:
        ctx.db.update_export(export_id, status=JobStatus.CANCELLED, error="Отменено пользователем")
        raise
    except MailArchiverError as exc:
        ctx.db.update_export(export_id, status=JobStatus.FAILED, error=exc.message[:500])
        raise
    except Exception as exc:  # noqa: BLE001
        ctx.db.update_export(export_id, status=JobStatus.FAILED, error=str(exc)[:500])
        raise
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not ok:
            for path in produced + ([final_path] if final_path else []):
                _unlink_quiet(path)

    errors = res.errors + len(skipped)
    status = JobStatus.PARTIAL if errors else JobStatus.SUCCESS
    details = list(res.error_details[:5]) + skipped[:5]
    ctx.db.update_export(export_id, status=status, path=final_path, size=size,
                         error="; ".join(details)[:1000])
    if res.warning:
        ctx.event("WARNING", res.warning)
    summary = (f"Экспортировано писем: {res.count}, файл: {os.path.basename(final_path)} "
               f"({human_size(size)}), ошибок: {errors}.")
    if skipped:
        summary += f" Не удалось прочитать из копии писем: {len(skipped)} — они НЕ попали в выгрузку."
    ctx.event("INFO" if not errors else "WARNING", summary)
    return {"final_status": status, "summary": summary, "path": final_path,
            "count": res.count, "size": size, "warning": res.warning, "export_id": export_id,
            "errors": errors}


def _unlink_quiet(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError as exc:
        log.warning("Не удалось удалить %s: %s", path, exc)


# ---------------------------------------------------------------------------
#  IMPORT PST (через readpst) -> в локальную копию или на IMAP
# ---------------------------------------------------------------------------
def _eml_date(raw: bytes):
    """Дата письма из заголовка Date (для APPEND). None — если её нет или она кривая."""
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
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _run_readpst(ctx: JobContext, pst_path: str, out_dir: str) -> None:
    """readpst с проверкой отмены: на большом .pst он работает до часа."""
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
                if ctx.is_cancelled():
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


def handle_import_pst(ctx: JobContext) -> Dict:
    """Импорт .pst на IMAP-сервер (через readpst).

    Дата каждого письма передаётся в APPEND из его заголовка Date: без неё весь
    импортированный архив «приходил сегодня» — Outlook сортирует по дате
    получения, а следующий бэкап записывал в индекс дату импорта (неверные
    аналитика, фильтр дат экспорта и ретеншн). Обрыв связи не проваливает
    остаток импорта: движок переподключается и продолжает с того же письма.
    """
    from ..errors import ImapConnectionError, ImapTimeoutError
    from ..imap.client import ReconnectingSession
    from ..imap.restore import MAX_MESSAGE_EVENTS
    svc = ctx.services
    p = ctx.params
    pst_path = p.get("pst_path")
    if not pst_path or not os.path.isfile(pst_path):
        raise ValidationError("Файл .pst для импорта не найден.", hint="Загрузите файл заново.")
    if shutil.which("readpst") is None:
        raise MailArchiverError("Утилита readpst (пакет pst-utils) не установлена на сервере.",
                                code="dependency_missing",
                                hint="Установите пакет pst-utils (Debian/Ubuntu) или libpst (RHEL).")
    target = p.get("target", "imap")  # imap | local
    if target != "imap":
        raise ValidationError("Импорт PST в локальную копию пока выполняется только на IMAP-сервер.",
                              hint="Выберите цель «на IMAP-сервер».")
    acc = svc.require_account(ctx.account_id)
    ctx.event("INFO", f"Импорт PST «{os.path.basename(pst_path)}» (цель: {target}).")

    tmp = tempfile.mkdtemp(prefix="pstimport_", dir=svc.cfg.tmp_dir)
    session = None
    try:
        _run_readpst(ctx, pst_path, tmp)
        eml_files = []
        for root, _dirs, files in os.walk(tmp):
            for fn in sorted(files):
                if fn.lower().endswith(".eml"):
                    rel_folder = os.path.relpath(root, tmp)
                    eml_files.append((rel_folder, os.path.join(root, fn)))
        total = len(eml_files)
        ctx.event("INFO", f"В PST найдено писем: {total}.")

        imported = 0
        errors = 0
        shown = [0]

        def report(text: str) -> None:
            shown[0] += 1
            if shown[0] <= MAX_MESSAGE_EVENTS:
                ctx.event("ERROR", text)
            elif shown[0] == MAX_MESSAGE_EVENTS + 1:
                ctx.event("WARNING", "Ошибок больше — остальные только в итоге и в журнале службы.")
            log.warning("import_pst %s: %s", acc.name, text)

        prefix = p.get("target_prefix", "Импорт PST")

        def check_cancel() -> None:
            if ctx.is_cancelled():
                raise JobCancelled("Импорт отменён пользователем.")

        session = ReconnectingSession(acc, svc.connect_options(), ctx.event, check_cancel)
        session.open()
        # list_folders() заодно сообщает разделитель иерархии сервера;
        # подстраховываемся на случай, если сервер его не прислал —
        # иначе None попал бы в имя папки строкой "None".
        session.conn.list_folders()
        delimiter = session.conn.delimiter or "/"
        ensured = set()
        failed_folders = set()
        lost_link = ""
        for i, (rel_folder, fp) in enumerate(eml_files, 1):
            if lost_link:
                break
            if ctx.is_cancelled():
                raise JobCancelled(f"Импорт отменён пользователем (залито {imported} из {total}).")
            # разделитель каталогов ОС переводим в разделитель IMAP
            rel = _rel_to_imap_folder(rel_folder, delimiter)
            folder = f"{prefix}{delimiter}{rel}" if rel else prefix
            if folder in failed_folders:
                errors += 1
                continue
            try:
                with open(fp, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                errors += 1
                report(f"Не удалось прочитать файл «{os.path.basename(fp)}»: {exc}")
                continue
            for _attempt in range(3):
                try:
                    if folder not in ensured:
                        try:
                            session.conn.ensure_folder(folder)
                        except (ImapConnectionError, ImapTimeoutError):
                            raise
                        except MailArchiverError as exc:
                            # папку создать не удалось — письма этой папки
                            # пропускаем, но весь импорт не роняем
                            failed_folders.add(folder)
                            errors += 1
                            report(f"Папка «{folder}» недоступна: {exc.message}")
                            break
                        ensured.add(folder)
                    session.conn.append(folder, raw, msg_time=_eml_date(raw))
                    imported += 1
                    session.progressed()
                    break
                except (ImapConnectionError, ImapTimeoutError) as exc:
                    try:
                        session.reconnect(exc)
                    except (ImapConnectionError, ImapTimeoutError) as final:
                        # Сервер недоступен: сообщаем, сколько успели, а не
                        # теряем итог в общей «ошибке задания».
                        lost_link = final.message
                        break
                    ensured.clear()
                except MailArchiverError as exc:
                    errors += 1
                    report(f"Ошибка заливки: {exc.message}")
                    break
            else:
                errors += 1
                report(f"Письмо «{os.path.basename(fp)}» не залито: связь обрывалась при каждой попытке.")
            if lost_link:
                break
            if i % 20 == 0:
                ctx.progress(i, total, f"Импорт {i}/{total}")
        summary = f"Импортировано из PST: {imported} писем, ошибок: {errors}."
        if lost_link:
            left = total - imported - errors
            errors += left
            summary = (f"Импортировано из PST: {imported} из {total} писем. Связь с сервером потеряна "
                       f"({lost_link}) — остальные {left} писем НЕ залиты. Загрузите файл снова, когда "
                       f"сервер будет доступен (уже залитые письма при этом повторятся).")
            ctx.event("ERROR", summary)
        if session.reconnects:
            summary += f" Переподключений после обрыва связи: {session.reconnects}."
        ctx.progress(total, total, "Готово")
        status = JobStatus.PARTIAL if errors else JobStatus.SUCCESS
        return {"final_status": status, "summary": summary, "imported": imported, "errors": errors}
    finally:
        if session is not None:
            session.close()
        shutil.rmtree(tmp, ignore_errors=True)
        # загруженный .pst больше не нужен: без удаления он навсегда оставался
        # бы в каталоге временных файлов. Но если задание прервано остановкой
        # службы, оно вернётся в очередь — тогда файл ещё понадобится.
        queue = getattr(svc, "queue", None)
        if not (queue is not None and queue.is_shutting_down()):
            try:
                os.unlink(pst_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
#  TEST CONNECTION
# ---------------------------------------------------------------------------
def handle_test(ctx: JobContext) -> Dict:
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    ctx.event("INFO", f"Проверка подключения к «{acc.name}»…")
    res = probe_account(acc, svc.connect_options())
    if res["ok"]:
        ctx.event("INFO", f"Успех. Папок: {len(res['folders'])}.")
        return {"final_status": JobStatus.SUCCESS, "summary": f"Подключение успешно, папок: {len(res['folders'])}.",
                "folders": res["folders"], "capabilities": res["capabilities"]}
    ctx.event("ERROR", f"Ошибка: {res['error']}")
    return {"final_status": JobStatus.FAILED, "summary": res["error"], "hint": res.get("hint"),
            "error_result": res["error"]}


# ---------------------------------------------------------------------------
#  RETENTION (очистка по политике хранения)
# ---------------------------------------------------------------------------
def handle_retention(ctx: JobContext) -> Dict:
    """Удалить из локальной копии письма старше срока хранения.

    Запись индекса удаляется ТОЛЬКО вместе с файлом: если файл удалить не
    удалось (раздел только для чтения, права), письмо остаётся и в индексе, а
    задание получает статус «частично» — раньше строка исчезала, файл навсегда
    оставался на диске невидимым, а отчёт писал «освобождено».

    UID удалённых писем запоминаются (retired_uids): иначе следующий бэкап
    скачал бы их с сервера заново, и ретеншн с бэкапом работали бы по кругу.
    """
    svc = ctx.services
    removed = 0
    freed = 0
    failed = 0
    accounts = [svc.require_account(ctx.account_id)] if ctx.account_id else svc.db.list_accounts()
    # Сначала план: по каким ящикам и сколько писем подлежит удалению —
    # это даёт корректный прогресс и не требует держать выборку в памяти.
    plan = []
    for acc in accounts:
        eff_days = effective_retention_days(svc, acc)
        if eff_days <= 0:
            continue
        cutoff_iso = retention_cutoff_iso(eff_days)
        plan.append((acc, eff_days, cutoff_iso, _count_older_than(svc.db, acc.id, cutoff_iso)))
    total = sum(p[3] for p in plan)
    ctx.progress(0, total, f"К удалению писем: {total}")
    if not plan:
        ctx.event("INFO", "Срок хранения не задан — удалять нечего.")
    for acc, eff_days, cutoff_iso, planned in plan:
        ctx.event("INFO", f"Ящик «{acc.name}»: хранение {eff_days} дн., к удалению писем {planned}.")
        last_id = 0
        while True:
            fresh = svc.db.get_account(acc.id)
            if fresh is None or effective_retention_days(svc, fresh) != eff_days:
                # Поставили удержание архива или поменяли срок хранения — прежний план
                # больше не годится: письма, которые теперь надо хранить, не трогаем.
                ctx.event("WARNING", f"Ящик «{acc.name}»: срок хранения или удержание архива изменились во "
                                     f"время очистки — очистка ящика остановлена.")
                break
            rows = _page_older_than(svc.db, acc.id, cutoff_iso, last_id, MESSAGE_PAGE_SIZE)
            if not rows:
                break
            done_ids: List[int] = []
            try:
                for row in rows:
                    last_id = row["id"]
                    if ctx.is_cancelled():
                        ctx.event("WARNING", f"Очистка прервана: удалено {removed + len(done_ids)} "
                                             f"({human_size(freed)}).")
                        raise JobCancelled("Очистка отменена пользователем.")
                    if svc.store.delete_message(acc.id, row["stored_path"] or ""):
                        done_ids.append(row["id"])
                        freed += row["size"] or 0
                    else:
                        failed += 1
                        if failed <= 20:
                            ctx.event("ERROR", f"Не удалось удалить файл {row['stored_path']} — "
                                               f"письмо оставлено в копии.")
                    if (removed + len(done_ids)) % 50 == 0:
                        ctx.progress(removed + len(done_ids), total,
                                     f"Удалено {removed + len(done_ids)}/{total} ({human_size(freed)})")
            finally:
                if done_ids:
                    svc.db.retire_message_indexes(done_ids)
                    removed += len(done_ids)
            if len(rows) < MESSAGE_PAGE_SIZE:
                break
    ctx.progress(total, total, "Готово")
    if failed:
        ctx.event("WARNING", f"Не удалось удалить файлов: {failed} — эти письма оставлены в копии "
                             f"(проверьте права на каталог с почтой и не смонтирован ли он только для чтения).")
    summary = f"Удалено {removed} писем, освобождено {human_size(freed)}."
    if failed:
        summary += f" Не удалось удалить: {failed}."
    ctx.event("INFO", f"Ретеншн: {summary}")
    if removed:
        invalidate_mail_analytics_cache()
    status = JobStatus.PARTIAL if failed else JobStatus.SUCCESS
    return {"final_status": status, "summary": summary, "removed": removed, "freed": freed,
            "failed": failed}


# ---------------------------------------------------------------------------
#  VERIFY (проверка целостности локальной копии)
# ---------------------------------------------------------------------------
#: Сколько сообщений об отдельных письмах писать в журнал проверки.
VERIFY_MAX_EVENTS = 100


def handle_verify(ctx: JobContext) -> Dict:
    """Проверка целостности: каждый файл есть, читается и совпадает по SHA-256.

    Письма читаются потоком (на письме в сотни мегабайт read_message держал бы
    в памяти несколько его копий), индекс обходится по id (устойчиво к записи
    идущего параллельно бэкапа). «Нет ключа шифрования» — отдельный итог, а не
    «повреждено»: иначе администратор мог решить, что архив испорчен, и
    запустить пересоздание копии с нуля.
    """
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    store = svc.store
    total = svc.db.count_messages(acc.id)
    missing = corrupt = unreadable = no_key = ok = 0
    shown = 0
    i = 0

    def report(level: str, text: str) -> None:
        nonlocal shown
        shown += 1
        if shown <= VERIFY_MAX_EVENTS:
            ctx.event(level, text)
        elif shown == VERIFY_MAX_EVENTS + 1:
            ctx.event("WARNING", f"Проблемных писем больше {VERIFY_MAX_EVENTS} — остальные только "
                                 f"в итоге и в журнале службы.")
            log.warning("verify %s: %s", acc.name, text)
        else:
            log.warning("verify %s: %s", acc.name, text)

    last_id = 0
    while True:
        rows = svc.db.messages_after_id(acc.id, last_id, limit=MESSAGE_PAGE_SIZE)
        if not rows:
            break
        for row in rows:
            last_id = row["id"]
            i += 1
            if ctx.is_cancelled():
                raise JobCancelled("Проверка отменена пользователем.")
            rel = row["stored_path"] or ""
            try:
                path = store.message_path(acc.id, rel)
            except MailArchiverError as exc:
                corrupt += 1
                report("ERROR", f"Недопустимый путь в индексе: {rel} ({exc.message})")
                continue
            if not os.path.exists(path):
                missing += 1
                report("ERROR", f"Отсутствует файл: {rel}")
            elif path.endswith(".enc") and store.cipher is None:
                no_key += 1
            else:
                try:
                    digest, _size = store.hash_message(acc.id, rel)
                    if row["sha256"] and digest != row["sha256"]:
                        corrupt += 1
                        report("ERROR", f"Содержимое изменено (хеш не совпал): {rel}")
                    else:
                        ok += 1
                except Exception as exc:  # noqa: BLE001
                    text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
                    if "ключ" in text.lower() and "не подходит" in text.lower():
                        no_key += 1
                    else:
                        unreadable += 1
                        report("ERROR", f"Файл не читается: {rel} — {text}")
            if i % 50 == 0:
                ctx.progress(i, max(total, i), f"Проверка {i}/{total}")
    ctx.progress(i, max(total, i), "Готово")
    bad = missing + corrupt + unreadable
    status = JobStatus.SUCCESS if not bad and not no_key else JobStatus.PARTIAL
    summary = (f"Проверено {i}: целых {ok}, отсутствуют {missing}, повреждены {corrupt}"
               + (f", не читаются {unreadable}" if unreadable else "") + ".")
    if no_key:
        summary += (f" Не проверено {no_key} зашифрованных писем: ключ шифрования не загружен или "
                    f"не подходит — это не повреждение, верните файл ключа и повторите проверку.")
    ctx.event("INFO" if not bad else "WARNING", summary)
    return {"final_status": status, "summary": summary, "ok": ok, "missing": missing,
            "corrupt": corrupt, "unreadable": unreadable, "no_key": no_key}


# ---------------------------------------------------------------------------
#  ANALYZE (глубокий анализ содержимого писем для раздела «Аналитика писем»)
# ---------------------------------------------------------------------------
def handle_analyze(ctx: JobContext) -> Dict:
    from ..analytics import deep_scan, save_deep
    svc = ctx.services
    account_id = ctx.account_id  # None = по всем ящикам
    scope = "все ящики"
    if account_id is not None:
        acc = svc.require_account(account_id)
        scope = f"«{acc.name}»"
    ctx.event("INFO", f"Старт глубокого анализа писем: {scope}.")
    result = deep_scan(svc, account_id,
                       progress_cb=ctx.progress, cancel_cb=ctx.is_cancelled, event_cb=ctx.event)
    save_deep(svc, account_id, result)
    ctx.progress(result["total"], result["total"], "Готово")
    summary = (f"Проанализировано писем: {result['scanned']}/{result['total']}, "
               f"вложений: {result['attachments']['count']}, ошибок: {result['errors']}.")
    return {"final_status": JobStatus.SUCCESS, "summary": summary,
            "scanned": result["scanned"], "attachments": result["attachments"]["count"],
            "errors": result["errors"]}


# ---------------------------------------------------------------------------
#  SYNC_EMPLOYEES (синхронизация справочника сотрудников с файлом-выгрузкой)
# ---------------------------------------------------------------------------
def handle_sync_employees(ctx: JobContext) -> Dict:
    """Прочитать список сотрудников из источника и применить его к справочнику.

    Задание общесистемное (account_id=None). Источник задаётся настройкой
    ``employees.source_type``: файл на сервере (``employees.source_file``) или
    адрес выгрузки (``employees.source_url``). В параметрах задания источник
    можно переопределить — ключами ``source_type``, ``path`` и ``url``; этим
    пользуется ручной запуск из интерфейса.
    """
    from ..employees import load_employee_source, sync_employees
    svc = ctx.services
    source_type = str(ctx.params.get("source_type")
                      or svc.rt("employees", "source_type") or "file").strip().lower()
    if source_type not in ("file", "url"):
        source_type = "file"

    create_accounts = ctx.params.get("create_accounts")
    if create_accounts is None:
        create_accounts = bool(svc.rt("employees", "create_accounts"))

    rows, problems, origin = load_employee_source(
        source_type=source_type,
        path=str(ctx.params.get("path") or svc.rt("employees", "source_file") or ""),
        url=str(ctx.params.get("url") or svc.rt("employees", "source_url") or ""),
        username=str(svc.rt("employees", "source_url_user") or ""),
        password=str(svc.rt("employees", "source_url_password") or ""),
        verify_ssl=bool(svc.rt("employees", "source_url_verify_ssl")),
        timeout_s=int(svc.rt("employees", "source_url_timeout_s") or 60),
        fmt=str(svc.rt("employees", "source_url_format") or "auto"),
    )
    ctx.event("INFO", f"Синхронизация сотрудников, источник: {origin}.")
    ctx.event("INFO", f"Разобрано строк: {len(rows)}; проблемных строк: {len(problems)}.")
    ctx.progress(0, len(rows) or 1, "Разбор списка завершён")

    result = sync_employees(svc, rows, create_accounts=bool(create_accounts), progress_cb=ctx.progress)
    problems = problems + list(result["problems"])
    for problem in problems[:50]:   # в журнал пишем разумную выборку, не весь файл
        ctx.event("WARNING", f"Строка {problem['row']}: {problem['reason']}")
    if len(problems) > 50:
        ctx.event("WARNING", f"…и ещё {len(problems) - 50} проблемных строк.")

    ctx.progress(len(rows), len(rows) or 1, "Готово")
    summary = (f"Сотрудников добавлено {result['created']}, обновлено {result['updated']}; "
               f"ящиков создано {result['accounts_created']}, привязано {result['accounts_linked']}; "
               f"строк в источнике {result['total_rows']}, проблемных {len(problems)}.")
    if result.get("duplicate_rows"):
        shown = ", ".join(result["duplicate_emails"][:5])
        more = len(result["duplicate_emails"]) - 5
        summary += (f" Адресов, повторяющихся в файле: {len(result['duplicate_emails'])} "
                    f"({result['duplicate_rows']} строк) — {shown}{f' и ещё {more}' if more > 0 else ''}; "
                    f"на один адрес заводится одна карточка.")
    if result.get("skipped_inactive"):
        summary += (f" Пропущено помеченных в файле как не работающие: "
                    f"{result['skipped_inactive']}.")
    if result.get("dismissed"):
        summary += (f" Отмечено уволенными: {result['dismissed']} — архив их ящиков удерживается, "
                    f"копирование выключается после последней копии.")
    if result.get("rehired"):
        summary += f" Снова работают (удержание снято): {result['rehired']}."
    for warning in result.get("warnings", []):
        ctx.event("WARNING", warning)
        summary += " " + warning
    ctx.event("INFO", summary)
    svc.db.add_audit("system", "employees_sync", summary[:500])
    status = JobStatus.SUCCESS if not (problems or result.get("warnings")) else JobStatus.PARTIAL
    return {"final_status": status, "summary": summary,
            "created": result["created"], "updated": result["updated"],
            "accounts_created": result["accounts_created"],
            "accounts_linked": result["accounts_linked"],
            "total_rows": result["total_rows"], "problems": problems[:200],
            "skipped_inactive": result.get("skipped_inactive", 0),
            "dismissed": result.get("dismissed", 0), "rehired": result.get("rehired", 0),
            "duplicate_rows": result.get("duplicate_rows", 0),
            "duplicate_emails": result.get("duplicate_emails", [])[:50]}


# ---------------------------------------------------------------------------
#  STORAGE_CONVERT (зашифровать/расшифровать уже сохранённые письма ящика)
# ---------------------------------------------------------------------------
def _twin_of(rel: str) -> str:
    return rel[:-4] if rel.endswith(".enc") else rel + ".enc"


def _digest_or_none(store, account_id: int, rel: str) -> Optional[str]:
    try:
        return store.hash_message(account_id, rel)[0]
    except Exception:  # noqa: BLE001
        return None


def _matches_index(store, account_id: int, rel: str, sha: str) -> bool:
    """Файл письма читается и совпадает с индексом по SHA-256."""
    digest = _digest_or_none(store, account_id, rel)
    return digest is not None and ((not sha) or digest == sha)


def _same_message(store, account_id: int, rel_a: str, rel_b: str, sha: str) -> bool:
    """Оба файла читаются и содержат одно и то же письмо (и совпадают с индексом)."""
    a = _digest_or_none(store, account_id, rel_a)
    b = _digest_or_none(store, account_id, rel_b)
    return a is not None and a == b and ((not sha) or a == sha)


def handle_storage_convert(ctx: JobContext) -> Dict:
    """Зашифровать или расшифровать уже сохранённые письма ящика.

    Главное правило — никогда не удалять единственную копию письма. «Двойник»
    (X ↔ X.enc), оставшийся от прерванного прогона, удаляется, только если
    письмо из индекса существует, читается и совпадает с индексом по SHA-256,
    а сам двойник — тоже. Если файла из индекса нет, а двойник есть (индекс и
    каталог рассинхронизированы: восстановление из бэкапов разного времени,
    сбой питания посреди перешифровки), индекс ПЕРЕПРИВЯЗЫВАЕТСЯ к двойнику.
    """
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    mode = str(ctx.params.get("mode") or "encrypt")
    encrypt = mode == "encrypt"
    if svc.store.cipher is None:
        raise ValidationError("Ключ шифрования не загружен.",
                              hint="Включите шифрование в настройках хранилища или верните файл ключа.")
    total = svc.db.count_messages(acc.id)
    ctx.event("INFO", f"{'Шифрование' if encrypt else 'Расшифровка'} писем ящика «{acc.name}»: {total}.")
    done = converted = skipped = errors = leftovers = relinked = 0
    last_id = 0
    store = svc.store

    def fail(rel: str, text: str) -> None:
        nonlocal errors
        errors += 1
        if errors <= 50:
            ctx.event("ERROR", f"{rel}: {text}")
        elif errors == 51:
            ctx.event("WARNING", "Ошибок больше 50 — остальные только в итоге и в журнале службы.")
        log.warning("storage_convert %s: %s: %s", acc.name, rel, text)

    while True:
        rows = svc.db.messages_after_id(acc.id, last_id, limit=500)
        if not rows:
            break
        for row in rows:
            last_id = row["id"]
            done += 1
            if ctx.is_cancelled():
                raise JobCancelled("Перешифровка отменена пользователем. Уже обработанные письма "
                                   "остаются в новом виде — архив при этом полностью читается.")
            rel = row["stored_path"] or ""
            sha = row["sha256"] or ""
            try:
                path = store.message_path(acc.id, rel)
                twin = _twin_of(rel)
                twin_path = store.message_path(acc.id, twin)
                if not os.path.exists(path):
                    if os.path.exists(twin_path) and _matches_index(store, acc.id, twin, sha):
                        # Индекс указывает на файл, которого нет, а письмо лежит
                        # рядом в другом виде: переводим индекс на него.
                        svc.db.set_message_path(row["id"], twin)
                        relinked += 1
                        rel, twin = twin, rel
                        path, twin_path = twin_path, path
                    else:
                        fail(rel, "файла копии нет на диске" + (
                            " (а файл рядом не совпадает с индексом)" if os.path.exists(twin_path) else ""))
                        continue
                new_rel = store.convert_message(acc.id, rel, encrypt=encrypt)
                if new_rel is None:
                    skipped += 1
                    # письмо уже в нужном виде; «двойник» от прерванного прогона
                    # убираем, только если ОБА файла целы и совпадают с индексом
                    if os.path.exists(twin_path):
                        if _same_message(store, acc.id, rel, twin, sha):
                            store.drop_converted(acc.id, twin)
                            leftovers += 1
                        else:
                            fail(rel, "рядом лежит второй вариант письма, но сверить их не удалось — "
                                      "оставлены оба (запустите проверку целостности)")
                    continue
                # сначала индекс, потом удаление старого файла: при сбое между
                # ними индекс указывает на существующий файл
                svc.db.set_message_path(row["id"], new_rel)
                store.drop_converted(acc.id, rel)
                converted += 1
            except JobCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 — одно битое письмо не должно обрывать весь ящик
                text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
                fail(rel, text)
            if done % 50 == 0:
                ctx.progress(done, total, f"Обработано {done}/{total}")
    ctx.progress(total, total, "Готово")
    status = JobStatus.PARTIAL if errors else JobStatus.SUCCESS
    summary = (f"Ящик «{acc.name}»: {'зашифровано' if encrypt else 'расшифровано'} {converted}, "
               f"уже были в нужном виде {skipped}, ошибок {errors}"
               + (f", убрано остатков прерванного прогона {leftovers}" if leftovers else "")
               + (f", индекс переведён на существующий файл: {relinked}" if relinked else "") + ".")
    if encrypt:
        quarantines = store.list_quarantines(acc.id)
        if quarantines:
            files = sum(q[1] for q in quarantines)
            summary += (f" Внимание: в карантинных копиях ящика ({len(quarantines)}, файлов {files}) "
                        f"письма остаются НЕЗАШИФРОВАННЫМИ — удалите их в карточке ящика, когда "
                        f"убедитесь, что они не нужны.")
    ctx.event("INFO" if not errors else "WARNING", summary)
    return {"final_status": status, "summary": summary, "converted": converted,
            "skipped": skipped, "errors": errors, "relinked": relinked, "leftovers": leftovers}


# ---------------------------------------------------------------------------
#  CHECK_LOGINS (проверить пароли сразу многих ящиков)
# ---------------------------------------------------------------------------
#: Сколько ящиков проверять одновременно. Немного: сотни неудачных входов разом
#: с одного адреса — повод для защиты почтового сервера от перебора (fail2ban).
CHECK_LOGINS_PARALLEL = 4


def handle_check_logins(ctx: JobContext) -> Dict:
    """Войти в каждый ящик (и сразу выйти) — найти ящики с неверным паролем.

    Итог каждого ящика сохраняется в его карточке (login_status), по нему в
    разделе «Почтовые ящики» работает отбор «С неправильным паролем».
    Параметры: ``account_ids`` — проверить только эти ящики; ``only_enabled`` —
    только включённые.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..imap.client import check_login
    from ..models import account_has_credentials
    svc = ctx.services
    ids = ctx.params.get("account_ids")
    only_enabled = bool(ctx.params.get("only_enabled"))
    accounts = svc.db.list_accounts(only_enabled=only_enabled)
    if ids:
        wanted = {int(i) for i in ids}
        accounts = [a for a in accounts if a.id in wanted]
    total = len(accounts)
    ctx.event("INFO", f"Проверка входа в ящики: {total}.")
    counts = {"ok": 0, "auth_error": 0, "conn_error": 0, "no_password": 0, "secret_broken": 0}
    bad: List[str] = []
    to_check = []
    for acc in accounts:
        if acc.secret_broken:
            svc.db.set_login_status(acc.id, "secret_broken", "Пароль не расшифровывается текущим ключом.")
            counts["secret_broken"] += 1
        elif not account_has_credentials(acc):
            svc.db.set_login_status(acc.id, "no_password", "Пароль не задан.")
            counts["no_password"] += 1
        elif not (acc.host or "").strip():
            svc.db.set_login_status(acc.id, "conn_error", "Не указан IMAP-сервер.")
            counts["conn_error"] += 1
        else:
            to_check.append(acc)
    opts = svc.connect_options()
    opts.connect_timeout_s = min(int(opts.connect_timeout_s or 30), 20)
    opts.socket_timeout_s = min(int(opts.socket_timeout_s or 120), 30)
    done = total - len(to_check)
    ctx.progress(done, total, f"Проверено {done}/{total}")
    with ThreadPoolExecutor(max_workers=CHECK_LOGINS_PARALLEL, thread_name_prefix="login") as pool:
        futures = {}
        it = iter(to_check)
        # подаём ящики порциями: отмена срабатывает, не дожидаясь всего списка
        for acc in it:
            futures[pool.submit(check_login, acc, opts)] = acc
            if len(futures) >= CHECK_LOGINS_PARALLEL * 2:
                break
        while futures:
            for fut in as_completed(list(futures)):
                acc = futures.pop(fut)
                try:
                    status, error = fut.result()
                except Exception as exc:  # noqa: BLE001
                    status, error = "conn_error", str(exc)
                svc.db.set_login_status(acc.id, status, error)
                counts[status] = counts.get(status, 0) + 1
                if status == "auth_error":
                    bad.append(acc.name)
                    if len(bad) <= 50:
                        ctx.event("WARNING", f"«{acc.name}» ({acc.username}): неверный логин или пароль — "
                                             f"{error[:200]}")
                done += 1
                ctx.progress(done, total, f"Проверено {done}/{total}")
                if ctx.is_cancelled():
                    for other in futures:
                        other.cancel()
                    raise JobCancelled(f"Проверка паролей отменена (проверено {done} из {total}).")
                nxt = next(it, None)
                if nxt is not None:
                    futures[pool.submit(check_login, nxt, opts)] = nxt
                break
    summary = (f"Проверено ящиков: {total}. Пароль верный: {counts['ok']}, неверный: {counts['auth_error']}, "
               f"без пароля: {counts['no_password']}, нет связи с сервером: {counts['conn_error']}"
               + (f", пароль не читается: {counts['secret_broken']}" if counts["secret_broken"] else "") + ".")
    ctx.event("INFO" if not counts["auth_error"] else "WARNING", summary)
    svc.db.add_audit("system", "check_logins", summary[:500])
    status = JobStatus.SUCCESS if not (counts["auth_error"] or counts["conn_error"]) else JobStatus.PARTIAL
    return {"final_status": status, "summary": summary, "counts": counts, "bad": bad[:200]}


# ---------------------------------------------------------------------------
#  Копия вне сервера и снимок базы
# ---------------------------------------------------------------------------
def handle_replicate(ctx: JobContext) -> Dict:
    from ..replica.runner import ReplicaRunner
    res = ReplicaRunner(ctx.services, ctx, allow_mass_delete=bool(ctx.params.get("allow_mass_delete")),
                        force_verify=bool(ctx.params.get("verify"))).run()
    ctx.db.add_audit(ctx.params.get("by") or "system", "replica_run", res["message"][:900])
    return {"final_status": res["status"], "summary": res["message"],
            **{k: v for k, v in res.items() if k not in ("status", "message")}}


def handle_db_snapshot(ctx: JobContext) -> Dict:
    from ..replica import snapshots
    ctx.event("INFO", "Снимок базы данных…")
    info = snapshots.make_snapshot(ctx.services)
    summary = (f"Снимок базы {info['name']}: {human_size(info['size'])} (база {human_size(info['db_size'])}), "
               f"{info['seconds']} с." + (" Снимок зашифрован ключом шифрования копии." if info["encrypted"] else ""))
    return {"final_status": JobStatus.SUCCESS, "summary": summary, "snapshot": info["name"],
            "size": info["size"]}


#: Сколько секунд одно задание индексации держит слот очереди: большой архив
#: индексируется частями, между ними успевают пройти копирования ящиков.
SEARCH_INDEX_SLICE_S = 600


def handle_search_index(ctx: JobContext) -> Dict:
    from ..search import index_pending
    res = index_pending(ctx.services, progress=ctx.progress, cancelled=ctx.is_cancelled, event=ctx.event,
                        max_seconds=float(ctx.params.get("max_seconds") or SEARCH_INDEX_SLICE_S))
    if not res.get("available"):
        return {"final_status": JobStatus.SUCCESS,
                "summary": "Полнотекстовый поиск недоступен: SQLite собран без FTS5."}
    summary = (f"Проиндексировано писем: {res['indexed']}"
               + (f", осталось {res['pending']} (продолжится автоматически)" if res["pending"] else "")
               + (f"; только по заголовкам (файл не прочитан): {res['errors']}" if res["errors"] else "")
               + ("" if res.get("bodies") else "; текст писем не индексируется") + ".")
    if ctx.is_cancelled():
        raise JobCancelled(ctx.stop_reason() + ". " + summary)
    return {"final_status": JobStatus.SUCCESS, "summary": summary, **res}


def handle_search_reindex(ctx: JobContext) -> Dict:
    """Переиндексировать поиск по выбранным ящикам (групповое действие).

    Параметр ``account_ids`` — какие ящики. Остальной индекс не трогается.
    """
    from .. import search as search_mod
    svc = ctx.services
    names = {a.id: a.name for a in svc.db.list_accounts()}
    ids = [int(i) for i in (ctx.params.get("account_ids") or []) if int(i) in names]
    if not ids:
        return {"final_status": JobStatus.SUCCESS, "summary": "Ящиков для переиндексации нет.", "items": []}
    items: List[Dict] = []

    def on_account(account_id: int, stat: Dict[str, int]) -> None:
        errs = int(stat.get("errors") or 0)
        items.append({"id": account_id, "name": names.get(account_id, f"#{account_id}"),
                      "status": JobStatus.PARTIAL if errs else JobStatus.SUCCESS,
                      "new": int(stat.get("indexed") or 0),
                      "detail": f"не перечитано (файл не прочитан, в индексе — прежняя запись): {errs}" if errs else ""})

    ctx.event("INFO", f"Переиндексация поиска: ящиков {len(ids)}"
                      + ("" if search_mod.bodies_enabled(svc) else "; текст писем не индексируется (так настроено)")
                      + ".")
    res = search_mod.reindex_accounts(svc, ids, progress=ctx.progress, cancelled=ctx.is_cancelled,
                                      event=ctx.event, on_account=on_account)
    if not res.get("available"):
        return {"final_status": JobStatus.SUCCESS,
                "summary": "Полнотекстовый поиск недоступен: SQLite собран без FTS5.", "items": []}
    if not res["total"]:
        summary = ("Писем этих ящиков в индексе поиска пока нет — их проиндексирует общий проход "
                   "«Индексация поиска».")
    else:
        summary = (f"Переиндексировано писем: {res['indexed']} из {res['total']}, ящиков: {len(items)} из {len(ids)}"
                   + (f"; не перечитано (файл не прочитан, в индексе осталась прежняя запись): {res['errors']}"
                      if res["errors"] else "")
                   + ("" if res.get("bodies") else "; текст писем не индексируется") + ".")
    if res.get("stopped") == "reset":
        summary += (" Индекс поиска перестроили целиком во время работы — остальные ящики проиндексирует "
                    "общий проход.")
    if ctx.is_cancelled():
        raise JobCancelled(ctx.stop_reason() + ". " + summary)
    ctx.event("INFO" if not res["errors"] else "WARNING", summary)
    status = JobStatus.PARTIAL if res["errors"] else JobStatus.SUCCESS
    return {"final_status": status, "summary": summary, "indexed": res["indexed"], "errors": res["errors"],
            "items": items}


# ---------------------------------------------------------------------------
#  FOLDERS_CHECK (проверить папки многих ящиков на сервере)
# ---------------------------------------------------------------------------
#: Сколько проблемных ящиков расписывать в журнале задания (остальные — в таблице итога).
FOLDERS_CHECK_MAX_EVENTS = 100


def _broken_folders_text(broken: List[Dict]) -> str:
    parts = []
    for row in broken[:5]:
        msgs = row.get("messages")
        parts.append(f"«{row.get('name')}» — " + (f"писем {msgs}" if msgs is not None else "писем сколько — неизвестно"))
    more = len(broken) - 5
    return (f"не открываются папки ({len(broken)}): " + ", ".join(parts)
            + (f" и ещё {more}" if more > 0 else ""))


def handle_folders_check(ctx: JobContext) -> Dict:
    """Проверить папки выбранных ящиков на сервере — по одному ящику, письма не скачиваются.

    По каждому ящику — открываются ли все его папки и сколько писем в тех, что
    не открываются (то есть не попадают в копию). Это та же проверка, что кнопка
    «Проверить папки» в карточке ящика, только сразу для многих. Сервер, который
    не отвечает несколько ящиков подряд, дальше не проверяется — чтобы не ждать
    тайм-аут на каждом его ящике.
    """
    from ..accountops import credential_problem
    from ..imap.client import diagnose_folders
    svc = ctx.services
    ids = ctx.params.get("account_ids")
    accounts = svc.db.list_accounts()
    if ids:
        wanted = {int(i) for i in ids}
        accounts = [a for a in accounts if a.id in wanted]
    accounts.sort(key=lambda a: (a.name or "").lower())
    total = len(accounts)
    ctx.event("INFO", f"Проверка папок на сервере: ящиков {total}, по одному; письма не скачиваются.")
    opts = svc.connect_options()
    include = svc.rt("backup", "folder_include") or []
    exclude = svc.rt("backup", "folder_exclude") or []
    counts = {"ok": 0, "with_problems": 0, "failed": 0, "skipped": 0}
    lost_total = 0
    reported = 0
    items: List[Dict] = []
    host_fails: Dict[str, int] = {}
    down: Dict[str, int] = {}
    for n, acc in enumerate(accounts):
        if ctx.is_cancelled():
            raise JobCancelled(f"Проверка папок отменена (проверено ящиков {n} из {total}).")
        ctx.progress(n, max(total, 1), f"{n + 1}/{total} «{acc.name}»")
        base = {"id": acc.id, "name": acc.name}
        problem = credential_problem(acc)
        host = (acc.host or "").strip().lower()
        if problem:
            counts["skipped"] += 1
            items.append({**base, "status": "skipped", "detail": problem})
            continue
        if host in down:
            down[host] += 1
            counts["skipped"] += 1
            items.append({**base, "status": "skipped", "detail": f"сервер {acc.host} не отвечает"})
            continue
        res = diagnose_folders(acc, opts, global_include=include, global_exclude=exclude)
        if not res.get("ok"):
            counts["failed"] += 1
            error = str(res.get("error") or "не удалось подключиться")
            items.append({**base, "status": "failed", "new": 0, "detail": error[:300]})
            if res.get("error_type") in ("ImapConnectionError", "ImapTimeoutError"):
                host_fails[host] = host_fails.get(host, 0) + 1
                if host_fails[host] >= SEQUENCE_MAX_CONN_FAILS and host not in down:
                    down[host] = 0
                    ctx.event("WARNING", f"Сервер {acc.host} не отвечает: {host_fails[host]} ящиков подряд — "
                                         f"остальные его ящики не проверяются.")
            else:
                host_fails[host] = 0
            continue
        host_fails[host] = 0
        folders = res.get("folders") or []
        broken = [r for r in folders if r.get("verdict") == "broken"]
        if broken:
            counts["with_problems"] += 1
            lost = int(res.get("messages_lost") or 0)
            lost_total += lost
            detail = _broken_folders_text(broken)
            items.append({**base, "status": "partial", "new": len(broken), "detail": detail[:300]})
            reported += 1
            if reported <= FOLDERS_CHECK_MAX_EVENTS:
                ctx.event("WARNING", f"«{acc.name}»: {detail}")
        else:
            counts["ok"] += 1
            empty = int((res.get("counts") or {}).get("empty_broken") or 0)
            detail = f"папок {len(folders)}, все открываются"
            if empty:
                detail += f"; пустых, которые не открываются: {empty} (писем в них нет)"
            items.append({**base, "status": "success", "new": 0, "detail": detail})
    checked = counts["ok"] + counts["with_problems"]
    summary = (f"Проверено ящиков: {checked} из {total}. Все папки открываются: {counts['ok']}; "
               f"есть папки, которые не открываются: {counts['with_problems']}"
               + (f" (писем в них по данным сервера: {lost_total})" if lost_total else "")
               + (f"; не удалось подключиться: {counts['failed']}" if counts["failed"] else "")
               + (f"; пропущено: {counts['skipped']}" if counts["skipped"] else "") + ".")
    if counts["with_problems"]:
        summary += (" Письма из неоткрывающихся папок в копию не попадают: папку чинят (или удаляют) на почтовом "
                    "сервере либо добавляют в «Пропускать папки» ящика.")
    ctx.progress(total, max(total, 1), "Готово")
    ctx.event("INFO" if not (counts["with_problems"] or counts["failed"]) else "WARNING", summary)
    if counts["failed"] and not checked:
        status = JobStatus.FAILED
    elif counts["with_problems"] or counts["failed"]:
        status = JobStatus.PARTIAL
    else:
        status = JobStatus.SUCCESS
    return {"final_status": status, "summary": summary, "total": total, **counts,
            "messages_lost": lost_total, "items": items[-SEQUENCE_MAX_ITEMS:]}


# ---------------------------------------------------------------------------
#  DEDUP_REPORT (отчёт «Одинаковые вложения»: сколько места сэкономило бы
#  хранение одной копии; формат хранения не меняется)
# ---------------------------------------------------------------------------
#: Сколько минимум держать слот очереди, прежде чем уступить его ждущим
#: заданиям (копированиям ящиков): первый подсчёт читает весь архив — часы.
DEDUP_MIN_SLICE_S = 300
#: Не дольше этого одним заданием, даже если очередь пуста.
DEDUP_MAX_SLICE_S = 3 * 3600


def handle_dedup_report(ctx: JobContext) -> Dict:
    from .. import dedup
    svc = ctx.services
    if ctx.params.get("full"):
        dedup.reset(svc.db)
        ctx.event("INFO", "Подсчёт начат заново: прежние отпечатки вложений удалены.")
    st = dedup.status(svc)
    if st["pending"]:
        ctx.event("INFO", f"Читаю письма: не разобрано {st['pending']} из {st['total']}.")
    res = dedup.process_pending(
        svc, progress=ctx.progress, cancelled=ctx.is_cancelled, event=ctx.event,
        min_seconds=float(ctx.params.get("min_seconds") or DEDUP_MIN_SLICE_S),
        should_yield=lambda: svc.db.count_waiting_jobs(exclude_types=[JobType.DEDUP_REPORT]) > 0,
        max_seconds=float(ctx.params.get("max_seconds") or DEDUP_MAX_SLICE_S))
    queue = getattr(svc, "queue", None)
    if queue is not None and queue.is_shutting_down():
        raise JobCancelled(ctx.stop_reason())          # отчёт досчитается после запуска службы
    report = dedup.build_report(svc)
    dedup.save_report(svc, report)
    summary = dedup.summary_text(report)
    if ctx.is_cancelled():
        raise JobCancelled(ctx.stop_reason() + ". Отчёт сохранён по прочитанной части. " + summary)
    if res["pending"] and res["processed"] and queue is not None:
        # Слот очереди отдаём копированиям ящиков — продолжим следующим заданием
        # (у него низкий приоритет: оно пойдёт, когда остальные дела сделаны).
        next_id = queue.enqueue(JobType.DEDUP_REPORT, None,
                                {"continued": int(ctx.params.get("continued") or 0) + 1},
                                priority=9, created_by="dedup")
        summary += f" Продолжение — задание №{next_id}."
    return {"final_status": JobStatus.SUCCESS, "summary": summary, **res,
            "savings_bytes": report["savings"]["bytes"]}


# ---------------------------------------------------------------------------
#  BACKUP_ALL (все включённые ящики по очереди, по одному)
# ---------------------------------------------------------------------------
#: После стольких ящиков подряд без связи с сервером проход останавливается:
#: иначе при лежащем сервере задание часами ждало бы тайм-аут на каждом из сотен ящиков.
SEQUENCE_MAX_CONN_FAILS = 5
#: Раз в столько удачных ящиков в журнал задания пишется строка «готово N из M».
SEQUENCE_REPORT_EVERY = 25
#: Сколько сообщений от самих ящиков (предупреждения, ошибки) переносить в журнал задания.
SEQUENCE_MAX_EVENTS = 300
#: Сколько строк итога по ящикам хранить в результате задания.
SEQUENCE_MAX_ITEMS = 2000
_SEQ_STATE = "backup_all_state_"
#: Время ручного «Повторить» прохода (meta): от него считается «не начинать после».
#: Отдельный ключ, а не поле состояния: состояние чужих незаконченных проходов
#: чистится при старте каждого прохода, и повтор не должен от этого зависеть.
_SEQ_RETRY = "backup_all_retry_at_"


def is_global_pass(job) -> bool:
    """Задание — проход по ВСЕМ включённым ящикам (а не по выбранным)."""
    try:
        params = json.loads(job["params"] or "{}") or {}
    except (TypeError, ValueError, KeyError, IndexError):
        params = {}
    return job["type"] == JobType.BACKUP_ALL and not params.get("account_ids")


def _last_attempt(acc, last_runs: Dict[int, object]) -> str:
    """Когда ящик пытались копировать в последний раз (удачно или нет); '' — никогда.

    Порядок прохода строится по ПОПЫТКЕ, а не по удаче: иначе ящики на
    мёртвом сервере навсегда оставались бы «самыми давними» и каждую ночь
    шли первыми, съедая проход ещё до исправных ящиков.
    """
    run = last_runs.get(acc.id)
    stamps = [acc.last_backup_at or "", acc.login_checked_at or ""]
    if run is not None:
        stamps.append(run["started_at"] or "")
    return max(stamps)


def _sequence_deadline(svc, stop_at: str, started: datetime) -> Optional[datetime]:
    """Когда перестать брать новые ящики: ближайшее «ЧЧ:ММ» после начала прохода."""
    stop_at = (stop_at or "").strip()
    if not stop_at:
        return None
    try:
        hh, mm = (int(x) for x in stop_at.split(":", 1))
    except ValueError:
        return None
    local = started.astimezone(svc.local_tz())
    target = local.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
    if target <= local:
        from datetime import timedelta
        target += timedelta(days=1)
    return target


def _duration_text(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


def handle_backup_all(ctx: JobContext) -> Dict:
    """Копирование всех включённых ящиков (или перечисленных) СТРОГО ПО ОЧЕРЕДИ.

    Одно задание проходит ящики один за другим — сам проход никогда не
    копирует два ящика сразу, сколько бы ни было свободных слотов в очереди
    (другие задания копирования — свои расписания ящиков, кнопки — могут идти
    параллельно с ним). Порядок — сначала ящики, копировавшиеся давнее
    всего (никогда — первыми): если проход не успел до «не начинать после» или
    прервался, следующий начнёт с тех, до кого очередь не дошла.

    Пока задание работает с ящиком, очередь не запускает по нему других
    заданий (ящик «занят»), а ящик, по которому уже идёт своё задание,
    откладывается в конец прохода. Ход сохраняется в базе: после перезапуска
    службы проход продолжается с того же места, а не начинается заново.
    """
    import json as _json
    from ..accountops import credential_problem
    from ..errors import DiskSpaceError, ImapAuthError, ImapConnectionError, ImapTimeoutError, StorageError

    svc = ctx.services
    p = ctx.params
    queue = getattr(svc, "queue", None)
    started = datetime.now(timezone.utc)
    if svc.store.encryption_blocked:
        raise StorageError("Копирование не начато: " + svc.store.encryption_blocked,
                           hint="Верните файл ключа шифрования или выключите шифрование в "
                                "«Настройки → Хранилище».")
    try:
        pause = max(0, min(3600, int(p.get("pause_seconds") or 0)))
    except (TypeError, ValueError):
        pause = 0
    order = str(p.get("order") or "oldest")
    only_ids = p.get("account_ids")
    wanted = {int(i) for i in only_ids} if only_ids else None

    state_key = f"{_SEQ_STATE}{ctx.job_id}"
    # Следы прежних проходов, которые так и не закончились (упали без повтора),
    # убираем: они больше никому не нужны.
    active_ids = {int(j["id"]) for j in ctx.db.active_jobs()}
    for row in ctx.db.query("SELECT key FROM meta WHERE key LIKE ?", (_SEQ_STATE + "%",)):
        try:
            other = int(row["key"][len(_SEQ_STATE):])
        except ValueError:
            continue
        if other != ctx.job_id and other not in active_ids:
            ctx.db.execute("DELETE FROM meta WHERE key=?", (row["key"],))
    try:
        state = _json.loads(ctx.db.get_meta(state_key) or "{}") or {}
    except (TypeError, ValueError):
        state = {}
    resumed = bool(state.get("done"))
    done = set(int(i) for i in state.get("done") or [])
    stats = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0, "messages": 0, "bytes": 0}
    stats.update(state.get("stats") or {})
    items: List[Dict] = list(state.get("items") or [])
    skip_reasons: Dict[str, int] = dict(state.get("skip_reasons") or {})
    if state.get("started"):
        try:
            started = datetime.fromisoformat(state["started"])
        except ValueError:
            pass
    if state.get("deadline"):
        try:
            deadline = datetime.fromisoformat(state["deadline"])
        except ValueError:
            deadline = None
    else:
        # «Не начинать после» отсчитывается от времени запуска по расписанию, а
        # не от фактического старта: проход, поставленный в 01:00 с пределом
        # 07:00, но дождавшийся свободного места в очереди лишь к 07:30, не
        # должен идти весь рабочий день до 07:00 завтрашнего. Ручной «Повторить»
        # — от времени повтора (см. JobQueue.retry): иначе повтор после 07:00
        # сразу заканчивался ничем.
        base = started
        job_row = ctx.db.get_job(ctx.job_id)
        retry_base = str(ctx.db.get_meta(f"{_SEQ_RETRY}{ctx.job_id}") or state.get("retry_base") or "")
        if retry_base:
            try:
                base = datetime.fromisoformat(retry_base)
            except ValueError:
                base = started
        elif job_row is not None and job_row["created_at"]:
            try:
                base = datetime.fromisoformat(job_row["created_at"])
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
            except ValueError:
                base = started
        deadline = _sequence_deadline(svc, str(p.get("stop_at") or ""), base)

    # Без списка — все включённые ящики; со списком — ровно перечисленные
    # (выключенные из них будут пропущены с понятной причиной).
    accounts = [a for a in svc.db.list_accounts() if (a.id in wanted if wanted is not None else a.enabled)]
    if order == "name":
        accounts.sort(key=lambda a: (a.name or "").lower())
    else:
        last_runs = svc.db.last_runs_by_account()
        accounts.sort(key=lambda a: (_last_attempt(a, last_runs), (a.name or "").lower()))
    total = len(accounts)

    def save_state() -> None:
        ctx.db.set_meta(state_key, _json.dumps({
            "done": sorted(done), "stats": stats, "items": items[-SEQUENCE_MAX_ITEMS:],
            "skip_reasons": skip_reasons, "started": started.isoformat(),
            "deadline": deadline.isoformat() if deadline else ""}, ensure_ascii=False))

    def note(acc, status: str, detail: str = "", new: int = 0, size: int = 0) -> None:
        items.append({"id": acc.id, "name": acc.name, "status": status, "new": new, "bytes": size,
                      "detail": detail[:300]})

    head = (f"Копирование ящиков по очереди: {total} "
            f"{'выбранных' if wanted is not None else 'включённых'} ящиков, по одному"
            + (", сначала те, что копировались давнее всего" if order != "name" else ", по алфавиту"))
    if pause:
        head += f"; пауза между ящиками {pause} с"
    if deadline:
        head += f"; новые ящики не начинаются после {deadline.astimezone(svc.local_tz()).strftime('%H:%M')}"
    ctx.event("INFO", head + "." + (f" Продолжение прерванного прохода: уже готово {len(done)}." if resumed else ""))
    if not total:
        ctx.event("INFO", "Включённых ящиков нет — копировать нечего.")

    forwarded = [0]
    # Ящики подряд без связи — по каждому серверу отдельно: лежащий старый
    # сервер не должен останавливать копирование ящиков на исправном.
    host_fails: Dict[str, int] = {}
    down_hosts: Dict[str, int] = {}          # сервер -> сколько ящиков пропущено
    stopped_by_deadline = False
    deferred: List = []
    since_report = [0]

    def host_of(acc) -> str:
        return (acc.host or "").strip().lower()

    def run_one(acc, second_try: bool) -> bool:
        """Обработать ящик. True — было копирование (после него — пауза)."""
        fresh = ctx.db.get_account(acc.id)
        if fresh is None or not fresh.enabled:
            stats["skipped"] += 1
            reason = "ящик удалён" if fresh is None else "копирование выключено"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            note(acc, "skipped", reason)
            done.add(acc.id)
            return False
        problem = credential_problem(fresh)
        if problem:
            stats["skipped"] += 1
            skip_reasons[problem] = skip_reasons.get(problem, 0) + 1
            note(fresh, "skipped", problem)
            done.add(acc.id)
            return False
        if host_of(fresh) in down_hosts:
            # Сервер не отвечает: ящик НЕ отмечается сделанным — при повторе
            # задания или в следующий проход до него дойдёт очередь.
            down_hosts[host_of(fresh)] += 1
            return False
        if queue is not None and not queue.acquire_account(acc.id, ctx.job_id):
            if not second_try:
                deferred.append(acc)          # по ящику идёт своё задание — вернёмся к нему в конце
                return False
            reason = "по ящику выполнялось другое задание"
            stats["skipped"] += 1
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            note(fresh, "skipped", reason)
            ctx.event("INFO", f"«{fresh.name}» пропущен: {reason}.")
            done.add(acc.id)
            return False
        index = len(done)

        def emit(level: str, message: str) -> None:
            if level == "INFO":
                return                       # подробности ящика — в его истории копий
            forwarded[0] += 1
            if forwarded[0] <= SEQUENCE_MAX_EVENTS:
                ctx.event(level, f"«{fresh.name}»: {message}")
            elif forwarded[0] == SEQUENCE_MAX_EVENTS + 1:
                ctx.event("WARNING", "Сообщений от ящиков слишком много — дальше только итог по каждому "
                                     "ящику (он в результате задания и в истории копий ящика).")

        def progress(cur, tot, label="", bytes_done=0, speed=0.0) -> None:
            ctx.progress(index, max(total, 1), f"{index + 1}/{total} «{fresh.name}»: {label}",
                         int(stats["bytes"]) + int(bytes_done or 0), speed)

        host = host_of(fresh)
        try:
            res = _backup_account(ctx, fresh, progress_cb=progress, event_cb=emit)
            host_fails[host] = 0
            status = res.get("final_status")
            stats["messages"] += int(res.get("messages_new") or 0)
            stats["bytes"] += int(res.get("bytes_new") or 0)
            if status == JobStatus.SUCCESS:
                stats["ok"] += 1
            elif status == JobStatus.PARTIAL:
                stats["partial"] += 1
                ctx.event("WARNING", f"«{fresh.name}»: частично — {res.get('summary', '')[:400]}")
            else:
                stats["failed"] += 1
                ctx.event("ERROR", f"«{fresh.name}»: {res.get('summary', '')[:400]}")
            note(fresh, status or "success", "" if status == JobStatus.SUCCESS else _problem_text(res),
                 int(res.get("messages_new") or 0), int(res.get("bytes_new") or 0))
        except (JobCancelled, DiskSpaceError):
            raise
        except MailArchiverError as exc:
            stats["failed"] += 1
            ctx.db.bump_daily_stats(fresh.id, jobs=1, errors=1)
            text = exc.message + (f" {exc.hint}" if exc.hint else "")
            ctx.event("ERROR", f"«{fresh.name}»: {text[:500]}")
            note(fresh, "failed", exc.message)
            if isinstance(exc, (ImapConnectionError, ImapTimeoutError)) and not isinstance(exc, ImapAuthError):
                host_fails[host] = host_fails.get(host, 0) + 1
                if host_fails[host] >= SEQUENCE_MAX_CONN_FAILS and host not in down_hosts:
                    down_hosts[host] = 0
                    ctx.event("WARNING", f"Сервер {fresh.host} не отвечает: {host_fails[host]} ящиков подряд без "
                                         f"связи — остальные его ящики в этом проходе пропускаются, чтобы не ждать "
                                         f"тайм-аут на каждом.")
            else:
                host_fails[host] = 0
        except Exception as exc:  # noqa: BLE001
            log.exception("Копирование ящика «%s» в проходе по очереди упало", fresh.name)
            stats["failed"] += 1
            ctx.db.bump_daily_stats(fresh.id, jobs=1, errors=1)
            ctx.event("ERROR", f"«{fresh.name}»: {type(exc).__name__}: {exc}"[:500])
            note(fresh, "failed", f"{type(exc).__name__}: {exc}")
            host_fails[host] = 0
        finally:
            if queue is not None:
                queue.release_account(acc.id, ctx.job_id)
        done.add(acc.id)
        since_report[0] += 1
        if since_report[0] >= SEQUENCE_REPORT_EVERY:
            since_report[0] = 0
            ctx.event("INFO", f"Готово ящиков: {len(done)} из {total} (новых писем {stats['messages']}, "
                              f"{human_size(stats['bytes'])}).")
        return True

    def summary_text(stopped: str = "") -> str:
        processed = stats["ok"] + stats["partial"] + stats["failed"]
        text = (f"Скопировано ящиков по очереди: {stats['ok'] + stats['partial']} из {total}"
                + (f" (частично: {stats['partial']})" if stats["partial"] else "")
                + f"; новых писем {stats['messages']} ({human_size(stats['bytes'])})")
        if stats["failed"]:
            names = [it["name"] for it in items if it["status"] == "failed"][:10]
            more = stats["failed"] - len(names)
            text += f"; с ошибкой: {stats['failed']} ({', '.join(names)}{f' и ещё {more}' if more > 0 else ''})"
        if stats["skipped"]:
            reasons = ", ".join(f"{k} — {v}" for k, v in sorted(skip_reasons.items(), key=lambda kv: -kv[1]))
            text += f"; пропущено: {stats['skipped']} ({reasons})"
        text += f". Обработано {processed + stats['skipped']} из {total} за {_duration_text((datetime.now(timezone.utc) - started).total_seconds())}."
        for host, skipped in down_hosts.items():
            if skipped:
                text += (f" Сервер {host} не отвечал — не скопировано его ящиков: {skipped} (они будут первыми "
                         f"в следующий раз).")
        if stopped:
            text += " " + stopped
        return text

    def wait_pause() -> None:
        end = time.time() + pause
        while time.time() < end:
            if ctx.is_cancelled():
                return
            time.sleep(min(1.0, max(0.0, end - time.time())))

    try:
        pending = [a for a in accounts if a.id not in done]
        for n, acc in enumerate(pending):
            if ctx.is_cancelled():
                raise JobCancelled(ctx.stop_reason())
            if deadline is not None and datetime.now(timezone.utc) >= deadline:
                stopped_by_deadline = True
                break
            copied = run_one(acc, second_try=False)
            save_state()
            if copied and pause and n < len(pending) - 1:
                wait_pause()
        if not stopped_by_deadline:
            for acc in list(deferred):
                if ctx.is_cancelled():
                    raise JobCancelled(ctx.stop_reason())
                if deadline is not None and datetime.now(timezone.utc) >= deadline:
                    stopped_by_deadline = True
                    break
                run_one(acc, second_try=True)
                save_state()
        if down_hosts and not (stats["ok"] + stats["partial"]):
            # Ни одного удачного ящика и сервер не отвечает — это сбой прохода:
            # очередь повторит задание позже, и оно продолжится с того же места.
            hosts = ", ".join(down_hosts)
            raise ImapConnectionError(
                f"Почтовый сервер недоступен ({hosts}): {SEQUENCE_MAX_CONN_FAILS} ящиков подряд не удалось "
                f"подключиться — проход остановлен, чтобы не ждать тайм-аут на каждом ящике.",
                hint="Проверьте доступность почтового сервера. Проход продолжится с того же места при повторе "
                     "задания.")
    except JobCancelled as exc:
        if queue is not None and queue.is_shutting_down() and not ctx.db.is_cancel_requested(ctx.job_id):
            save_state()                      # служба останавливается — продолжим после запуска
            raise
        ctx.db.execute("DELETE FROM meta WHERE key IN (?, ?)", (state_key, f"{_SEQ_RETRY}{ctx.job_id}"))
        raise JobCancelled(f"{exc.message if hasattr(exc, 'message') else exc}. " + summary_text()) from None
    except MailArchiverError:
        save_state()                           # повтор задания продолжит с того же места
        raise
    ctx.db.execute("DELETE FROM meta WHERE key IN (?, ?)", (state_key, f"{_SEQ_RETRY}{ctx.job_id}"))
    left = total - len(done)
    # До кого очередь не дошла (время вышло, сервер не отвечал) — поимённо для письма-итога.
    not_reached = [a.name for a in accounts if a.id not in done][:SEQUENCE_MAX_ITEMS]
    stopped = ""
    if stopped_by_deadline and left > 0:
        stopped = (f"Время прохода вышло ({deadline.astimezone(svc.local_tz()).strftime('%H:%M')}): "
                   f"не дошла очередь до {left} ящиков — в следующий раз они будут первыми.")
        ctx.event("WARNING", stopped)
    ctx.progress(total, max(total, 1), "Готово")
    summary = summary_text(stopped)
    ctx.event("INFO" if not stats["failed"] else "WARNING", summary)
    good = stats["ok"] + stats["partial"]
    if stats["failed"] and not good:
        final = JobStatus.FAILED
    elif stats["failed"] or stats["partial"] or (stopped_by_deadline and left > 0) or any(down_hosts.values()):
        final = JobStatus.PARTIAL
    else:
        final = JobStatus.SUCCESS
    return {"final_status": final, "summary": summary, "total": total, **stats,
            "left": left if (stopped_by_deadline or down_hosts) else 0, "skip_reasons": skip_reasons,
            "hosts_down": {h: n for h, n in down_hosts.items() if n}, "items": items[-SEQUENCE_MAX_ITEMS:],
            **({"not_reached": not_reached} if (stopped_by_deadline or down_hosts) and not_reached else {})}


def _problem_text(res: Dict) -> str:
    """Коротко — что не так с копией ящика: для списка ящиков прохода и письма-итога.

    Полный итог ящика длинный («новых писем …, папок прочитано …»), и главное —
    какие папки не прочитаны — в обрезанной строке терялось.
    """
    parts: List[str] = []
    skipped = list(res.get("skipped_folders") or [])
    if skipped:
        more = len(skipped) - 5
        parts.append(f"не прочитаны папки ({len(skipped)}): {', '.join(skipped[:5])}"
                     + (f" и ещё {more}" if more > 0 else ""))
    if res.get("messages_failed"):
        parts.append(f"сервер не отдал писем: {res['messages_failed']}")
    if res.get("errors") and not parts:
        parts.append(f"ошибок: {res['errors']}")
    return "; ".join(parts) or str(res.get("summary") or "")


#: Сколько ящиков каждой группы перечислять в письме-итоге прохода.
REPORT_MAX_NAMES = 60


def backup_all_report(result: Dict) -> str:
    """Подробности прохода «все ящики по очереди» для письма-уведомления.

    В итоге задания — только числа и первые имена; администратору, который
    читает письмо утром, нужны поимённо ящики с ошибкой, частичные, пропущенные
    и те, до кого очередь не дошла, — чтобы не открывать веб-интерфейс.
    """
    items = [it for it in (result.get("items") or []) if isinstance(it, dict)]
    lines: List[str] = []
    for key, title in (("failed", "С ошибкой"), ("partial", "Скопированы частично"), ("skipped", "Пропущены")):
        rows = [it for it in items if it.get("status") == key]
        if not rows:
            continue
        lines.append(f"{title} ({len(rows)}):")
        for it in rows[:REPORT_MAX_NAMES]:
            detail = " ".join(str(it.get("detail") or "").split())
            lines.append(f"  • {it.get('name')}" + (f" — {detail[:240]}" if detail else ""))
        if len(rows) > REPORT_MAX_NAMES:
            lines.append(f"  … и ещё {len(rows) - REPORT_MAX_NAMES} (полный список — в карточке задания).")
        lines.append("")
    not_reached = list(result.get("not_reached") or [])
    if not_reached:
        shown = ", ".join(not_reached[:REPORT_MAX_NAMES])
        more = len(not_reached) - REPORT_MAX_NAMES
        lines.append(f"Не дошла очередь ({len(not_reached)}) — в следующий раз они будут первыми:")
        lines.append(f"  {shown}" + (f" и ещё {more}" if more > 0 else ""))
        lines.append("")
    for host, skipped in sorted((result.get("hosts_down") or {}).items()):
        lines.append(f"Сервер {host} не отвечал: пропущено его ящиков {skipped}.")
    top = sorted((it for it in items if int(it.get("new") or 0) > 0), key=lambda it: -int(it.get("new") or 0))[:10]
    if top:
        if lines and lines[-1]:
            lines.append("")
        lines.append("Больше всего новых писем:")
        for it in top:
            lines.append(f"  • {it.get('name')} — {int(it['new'])} ({human_size(int(it.get('bytes') or 0))})")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
#  QUARANTINE_CHECK / QUARANTINE_RESCUE (прежние копии ящика)
# ---------------------------------------------------------------------------
def handle_quarantine(ctx: JobContext) -> Dict:
    """Сравнить прежние копии ящика с новой или вернуть из них недостающие письма."""
    from .. import quarantine as qmod
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    rescue = ctx.job_type == JobType.QUARANTINE_RESCUE
    known = svc.store.quarantine_paths(acc.id)
    wanted = {os.path.normpath(p) for p in (ctx.params.get("paths") or [])}
    paths = [p for p in known if not wanted or os.path.normpath(p) in wanted]
    if not paths:
        return {"final_status": JobStatus.SUCCESS, "summary": f"Ящик «{acc.name}»: прежних копий нет.",
                "items": []}
    lines: List[str] = []
    items: List[Dict] = []
    problems = 0
    attention = 0          # после «с нуля»: в прежней копии нашлись письма, которых нет в новой
    for n, path in enumerate(paths, start=1):
        name = os.path.basename(path)

        def prog(cur: int, total: int, message: str, _name=name, _n=n) -> None:
            ctx.progress(cur, max(total, 1), f"{_n}/{len(paths)} «{_name}»: {message}")

        if rescue:
            ctx.event("INFO", f"«{name}»: ищем письма, которых нет в новой копии, и возвращаем их в архив…")
            try:
                r = qmod.rescue(svc, acc, path, progress=prog, cancelled=ctx.is_cancelled)
            except ValidationError as exc:
                text = f"«{name}»: {exc.message}"
                items.append({"name": name, "status": "skipped", "new": 0, "detail": exc.message[:300]})
                problems += 1
                ctx.event("WARNING", text + (f" {exc.hint}" if exc.hint else ""))
                lines.append(text)
                continue
            after = r["after"]
            text = f"«{name}»: возвращено в архив писем {r['rescued']} ({human_size(r['rescued_bytes'])})"
            if r["skipped_old"]:
                text += (f"; старше срока хранения ящика ({r['retention_days']} дн.) не возвращено "
                         f"{r['skipped_old']} — их удалила бы ночная очистка")
            if r["skipped_unreadable"]:
                text += (f"; из папок, которые сейчас не открываются на сервере, не возвращено "
                         f"{r['skipped_unreadable']} — вернутся, когда папки снова начнут копироваться")
            if r["failed"]:
                text += f"; не удалось вернуть {r['failed']} (подробности — в журнале службы)"
                problems += 1
            text += f". Осталось только в прежней копии: {after.get('unique', 0)}."
            items.append({"name": name, "status": "partial" if r["failed"] else "success",
                          "new": r["rescued"], "bytes": r["rescued_bytes"], "detail": qmod.check_label(after)})
            level = "WARNING" if r["failed"] else "INFO"
        else:
            ctx.event("INFO", f"«{name}»: сравниваем с новой копией…")
            check = qmod.compare(svc, acc.id, path, progress=prog, cancelled=ctx.is_cancelled)
            text = f"«{name}»: {qmod.check_label(check)}."
            safe, why = qmod.is_safe_to_delete(check)
            if safe:
                text += " Все её письма есть в новой копии — её можно удалить."
            elif check.get("unique"):
                text += (f" Писем, которых нет в новой копии, — {check['unique']}: они удалены на сервере, лежат в "
                         f"исключённых или неоткрывающихся папках либо больше лимита размера.")
                if int(check.get("unique_unreadable") or 0) >= int(check["unique"]):
                    text += " Это письма папок, которые сейчас не открываются на сервере, — сравните ещё раз, когда они начнут копироваться."
                else:
                    text += " Вернуть их в архив: меню ящика → «Прежние копии» → «Вернуть»."
                if not check.get("new_copy_complete"):
                    text += (" Новая копия после «с нуля» ещё не прочитала все папки — сравните ещё раз после "
                             "полного копирования.")
            if check.get("unreadable"):
                problems += 1
            status = "partial" if check.get("unreadable") else "success"
            if check.get("unique") and ctx.params.get("after_rebuild"):
                attention += 1
            items.append({"name": name, "status": status, "new": int(check.get("unique") or 0),
                          "bytes": int(check.get("unique_bytes") or 0), "detail": qmod.check_label(check)})
            level = "WARNING" if (check.get("unique") or check.get("unreadable")) else "INFO"
        ctx.event(level, text)
        lines.append(text)
    summary = f"Ящик «{acc.name}»: " + " ".join(lines)
    # Сравнение, которое поставила копия «с нуля», нашло письма только в прежней
    # копии — это требует внимания администратора (и уведомления об «ошибках»).
    return {"final_status": JobStatus.PARTIAL if (problems or attention) else JobStatus.SUCCESS,
            "summary": summary, "items": items, **({"notify": True} if attention else {})}


# ---------------------------------------------------------------------------
#  CLEANUP (удаление архивов ящиков и прежних копий — групповые действия)
# ---------------------------------------------------------------------------
def handle_cleanup(ctx: JobContext) -> Dict:
    """Удалить с диска архивы ящиков (вместе с самими ящиками) или их прежние копии.

    Выполняется фоновым заданием: удаление сотен тысяч файлов занимает минуты,
    и в веб-запросе оно упиралось бы в тайм-аут. Перед каждым ящиком ещё раз
    проверяется, что его архив не удерживается и по нему не идёт задание —
    за время ожидания в очереди многое могло измениться.
    """
    from .. import quarantine as qmod
    svc = ctx.services
    store = svc.store
    queue = getattr(svc, "queue", None)
    items = list(ctx.params.get("items") or [])
    total = len(items)
    job_row = ctx.db.get_job(ctx.job_id)
    author = (job_row["created_by"] if job_row is not None else "") or "system"
    purged = quarantines = files_total = 0
    freed = 0
    problems: List[str] = []
    ctx.progress(0, max(total, 1), "Подготовка…")

    def skip(name: str, reason: str) -> None:
        problems.append(f"«{name}»: {reason}")
        ctx.event("WARNING", f"«{name}» пропущен: {reason}.")

    for i, item in enumerate(items):
        if ctx.is_cancelled():
            raise JobCancelled(f"{ctx.stop_reason()}: обработано {i} из {total}, "
                               f"освобождено {human_size(freed)}.")
        try:
            acc_id = int(item.get("account_id") or 0)
        except (TypeError, ValueError):
            continue
        what = str(item.get("what") or "")
        name = str(item.get("name") or f"№{acc_id}")
        acc = ctx.db.get_account(acc_id)
        # Пока ящик обрабатывается, очередь не запустит по нему других заданий
        # (копирование, очистку, проверку) — они ждут, как при своём задании ящика.
        held = False
        try:
            if acc is not None and queue is not None:
                if not queue.acquire_account(acc_id, ctx.job_id):
                    skip(name, "по ящику выполняется задание")
                    continue
                held = True
            running = [j for j in ctx.db.active_jobs()
                       if j["account_id"] == acc_id and j["id"] != ctx.job_id and j["status"] == JobStatus.RUNNING]
            if running:
                skip(name, "по ящику выполняется задание")
                continue
            if what == "purge":
                if acc is None:
                    ctx.event("INFO", f"«{name}»: ящик уже удалён.")
                    continue
                if acc.on_hold():
                    skip(name, f"архив удерживается ({hold_label(acc.hold_until)})")
                    continue
                # задания, ждущие своей очереди по удаляемому ящику, всё равно
                # упали бы с «ящик не найден» — снимаем их заранее
                if queue is not None:
                    for job in ctx.db.active_jobs():
                        if job["account_id"] == acc_id and job["status"] == JobStatus.QUEUED:
                            queue.cancel(int(job["id"]))
                count, size = store.delete_account_files(acc_id)
                shutil.rmtree(store.account_dir(acc_id), ignore_errors=True)
                q_count = 0
                if item.get("with_quarantine"):
                    for path, n_files, q_size in store.list_quarantines(acc_id):
                        store.drop_quarantine(path)
                        qmod.forget(ctx.db, path)
                        count += n_files
                        size += q_size
                        q_count += 1
                ctx.db.delete_account(acc_id)
                purged += 1
                quarantines += q_count
                files_total += count
                freed += size
                ctx.db.add_audit(author, "account_purge",
                                 f"{name}: удалено файлов {count} ({human_size(size)})"
                                 + (f", прежних копий {q_count}" if q_count else "") + " — групповое действие")
                ctx.event("INFO", f"«{name}»: ящик и архив удалены ({count} файлов, {human_size(size)}).")
            elif what == "quarantine":
                if acc is not None and acc.on_hold():
                    skip(name, f"архив удерживается ({hold_label(acc.hold_until)})")
                    continue
                # Только каталоги, показанные администратору при проверке: карантин,
                # созданный позже (новая копия «с нуля»), не трогаем.
                allowed = set(item.get("paths") or [])
                n_dirs = 0
                for path, n_files, q_size in store.list_quarantines(acc_id):
                    if path not in allowed:
                        continue
                    if item.get("only_safe"):
                        # Сравниваем заново прямо перед удалением: после прежнего сравнения
                        # могли пропасть файлы новой копии или измениться срок хранения, а
                        # сохранённый итог «можно удалять» тогда уже неверен.
                        if acc is None:
                            skip(name, f"«{os.path.basename(path)}»: ящика нет — сравнить не с чем")
                            continue
                        ctx.event("INFO", f"«{name}»: перед удалением сравниваем «{os.path.basename(path)}» с "
                                          f"новой копией…")
                        try:
                            fresh = qmod.compare(svc, acc_id, path, cancelled=ctx.is_cancelled)
                        except JobCancelled:
                            raise
                        except MailArchiverError as exc:
                            skip(name, f"«{os.path.basename(path)}»: сравнить не удалось — {exc.message}")
                            continue
                        safe, why = qmod.is_safe_to_delete(fresh, effective_retention_days(svc, acc))
                        if not safe:
                            skip(name, f"«{os.path.basename(path)}»: {why}")
                            continue
                    store.drop_quarantine(path)
                    qmod.forget(ctx.db, path)
                    n_dirs += 1
                    files_total += n_files
                    freed += q_size
                quarantines += n_dirs
                if n_dirs:
                    ctx.db.add_audit(author, "quarantine_delete", f"{name}: прежних копий {n_dirs} — групповое действие")
                    ctx.event("INFO", f"«{name}»: удалено прежних копий {n_dirs}.")
        except MailArchiverError as exc:
            problems.append(f"«{name}»: {exc.message}")
            ctx.event("ERROR", f"«{name}»: {exc.message}")
        except OSError as exc:
            problems.append(f"«{name}»: {exc}")
            ctx.event("ERROR", f"«{name}»: {exc}")
        finally:
            if held and queue is not None:
                queue.release_account(acc_id, ctx.job_id)
            ctx.progress(i + 1, max(total, 1), f"Обработано {i + 1} из {total} ({human_size(freed)})")
    if purged:
        invalidate_mail_analytics_cache()
    summary = (f"Удалено ящиков с архивом: {purged}; прежних копий: {quarantines}; "
               f"файлов: {files_total}; освобождено {human_size(freed)}.")
    if problems:
        summary += f" Не обработано: {len(problems)} ({'; '.join(problems[:5])}{' …' if len(problems) > 5 else ''})."
    ctx.event("INFO" if not problems else "WARNING", summary)
    return {"final_status": JobStatus.PARTIAL if problems else JobStatus.SUCCESS, "summary": summary,
            "purged": purged, "quarantines": quarantines, "files": files_total, "freed": freed,
            "problems": problems[:200]}


HANDLERS: Dict[str, Callable[[JobContext], Dict]] = {
    JobType.BACKUP: handle_backup,
    JobType.RESTORE: handle_restore,
    JobType.EXPORT: handle_export,
    JobType.IMPORT_PST: handle_import_pst,
    JobType.TEST: handle_test,
    JobType.RETENTION: handle_retention,
    JobType.VERIFY: handle_verify,
    JobType.ANALYZE: handle_analyze,
    JobType.SYNC_EMPLOYEES: handle_sync_employees,
    JobType.STORAGE_CONVERT: handle_storage_convert,
    JobType.CHECK_LOGINS: handle_check_logins,
    JobType.REPLICATE: handle_replicate,
    JobType.DB_SNAPSHOT: handle_db_snapshot,
    JobType.SEARCH_INDEX: handle_search_index,
    JobType.DEDUP_REPORT: handle_dedup_report,
    JobType.BACKUP_ALL: handle_backup_all,
    JobType.CLEANUP: handle_cleanup,
    JobType.QUARANTINE_CHECK: handle_quarantine,
    JobType.QUARANTINE_RESCUE: handle_quarantine,
    JobType.FOLDERS_CHECK: handle_folders_check,
    JobType.SEARCH_REINDEX: handle_search_reindex,
}
