"""
Обработчики заданий. Каждый обработчик получает :class:`JobContext` и
возвращает словарь-результат. Исключения означают сбой (обрабатывает
:class:`~mailarchiver.queue.manager.QueueManager`).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from ..errors import ExportError, JobCancelled, MailArchiverError, RestoreError, ValidationError
from ..imap.backup import BackupEngine
from ..imap.client import ImapConnection, probe_account
from ..imap.restore import RestoreEngine
from ..export import resolve_engine
from ..export.base import MailItem, zip_directory
from ..logging_setup import get_logger
from ..models import JobStatus, JobType
from ..util import human_size, safe_filename, utcnow_iso

log = get_logger("jobs")

# Размер страницы при обходе индекса писем. Один большой limit брать нельзя:
# list_messages сортирует по дате (ORDER BY internaldate DESC), поэтому в
# выборку попадали бы только самые новые письма, а самые старые — те, что и
# должен удалить ретеншн, — вообще не рассматривались.
MESSAGE_PAGE_SIZE = 2000


def _iter_all_messages(db, account_id: int, page_size: int = MESSAGE_PAGE_SIZE):
    """Постранично обойти ВСЕ письма ящика (без ограничения сверху)."""
    offset = 0
    while True:
        rows = db.list_messages(account_id, limit=page_size, offset=offset)
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < page_size:
            return
        offset += page_size


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


def _page_older_than(db, account_id: int, cutoff_iso: str, limit: int) -> list:
    """Порция САМЫХ СТАРЫХ писем ящика старше cutoff (для ретеншна)."""
    return db.query(
        "SELECT id, stored_path, size FROM messages WHERE account_id=? AND internaldate IS NOT NULL "
        "AND internaldate<>'' AND internaldate<? ORDER BY internaldate ASC LIMIT ?",
        (account_id, cutoff_iso, limit),
    )


class JobContext:
    def __init__(self, services, job_id: int, job_type: str, account_id: Optional[int], params: Dict) -> None:
        self.services = services
        self.db = services.db
        self.job_id = job_id
        self.job_type = job_type
        self.account_id = account_id
        self.params = params or {}
        self._last_progress = 0.0

    def progress(self, current: int, total: int, message: str = "", bytes_done: int = 0, speed: float = 0.0) -> None:
        now = time.time()
        # не чаще ~2 раз в секунду, чтобы не грузить БД
        if now - self._last_progress < 0.5 and current != total:
            return
        self._last_progress = now
        self.db.update_job_progress(self.job_id, current, total, message, bytes_done, speed)

    def event(self, level: str, message: str) -> None:
        self.db.add_job_event(self.job_id, level, message)

    def is_cancelled(self) -> bool:
        return self.db.is_cancel_requested(self.job_id)


# ---------------------------------------------------------------------------
#  BACKUP
# ---------------------------------------------------------------------------
def handle_backup(ctx: JobContext) -> Dict:
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    ctx.event("INFO", f"Старт резервного копирования ящика «{acc.name}».")
    run_id = ctx.db.start_run(acc.id, JobType.BACKUP, ctx.job_id)

    engine = BackupEngine(
        ctx.db, svc.store, svc.connect_options(),
        skip_larger_than_mb=int(svc.rt("backup", "skip_larger_than_mb") or 0),
        download_flags=bool(svc.rt("backup", "download_flags")),
        global_exclude=svc.rt("backup", "folder_exclude") or [],
        global_include=svc.rt("backup", "folder_include") or [],
    )
    try:
        res = engine.run(acc, progress_cb=ctx.progress, cancel_cb=ctx.is_cancelled, event_cb=ctx.event)
    except JobCancelled:
        ctx.db.finish_run(run_id, JobStatus.CANCELLED, detail="Отменено пользователем")
        raise

    ctx.db.finish_run(run_id, res.status_label, messages_new=res.messages_new, bytes_new=res.bytes_new,
                      messages_total=res.messages_total, errors=res.errors,
                      detail="; ".join(res.error_details[:5]))
    ctx.db.bump_daily_stats(acc.id, messages=res.messages_new, bytes_=res.bytes_new, jobs=1, errors=res.errors)

    summary = (f"Ящик «{acc.name}»: новых писем {res.messages_new} ({human_size(res.bytes_new)}), "
               f"папок {res.folders_processed}/{res.folders_total}, ошибок {res.errors}.")
    if res.messages_skipped:
        # письма, не скачанные из-за лимита размера, иначе «потерялись» бы без объяснений
        summary += f" Пропущено по лимиту размера: {res.messages_skipped}."
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
    svc.notifier.notify_job(JobType.BACKUP, res.status_label,
                            f"[MailArchiver] Бэкап «{acc.name}»: {res.status_label}", summary)
    # авто-ретеншн истории прогонов
    keep_runs = int(svc.rt("retention", "keep_last_runs") or 30)
    if keep_runs:
        ctx.db.purge_old_runs(acc.id, keep_runs)
    return {"final_status": res.status_label, "summary": summary,
            "messages_new": res.messages_new, "bytes_new": res.bytes_new, "errors": res.errors,
            "skipped_folders": res.skipped_folders,
            "empty_unreadable_folders": res.empty_unreadable_folders}


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
            target_mode=p.get("target_mode", "original"),
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

    ctx.db.update_restore(restore_id, status=res.status_label, restored=res.restored, errors=res.errors,
                          error="; ".join(res.error_details[:5]))
    summary = f"Восстановлено {res.restored}, пропущено {res.skipped}, ошибок {res.errors}."
    if res.dup_check_unavailable:
        summary += f" Без проверки дублей залито писем: {res.dup_check_unavailable} (возможны повторы)."
    svc.notifier.notify_job(JobType.RESTORE, res.status_label,
                            f"[MailArchiver] Восстановление «{acc.name}»: {res.status_label}", summary)
    return {"final_status": res.status_label, "summary": summary,
            "restored": res.restored, "skipped": res.skipped, "errors": res.errors, "dry_run": res.dry_run}


# ---------------------------------------------------------------------------
#  EXPORT
# ---------------------------------------------------------------------------
def handle_export(ctx: JobContext) -> Dict:
    svc = ctx.services
    acc = svc.require_account(ctx.account_id)
    p = ctx.params
    engine_name = p.get("engine", "auto")
    fmt = p.get("format", "pst")
    folders = p.get("folders") or None
    ctx.event("INFO", f"Старт экспорта ящика «{acc.name}» (движок: {engine_name}, формат: {fmt}).")

    engine = resolve_engine(engine_name, fmt)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base_name = f"{safe_filename(acc.name)}_{engine.fmt}_{ts}"
    total = svc.count_mail_items(acc.id, folders)
    if total == 0:
        raise ExportError("Нет писем для экспорта.",
                          hint="Сначала выполните резервное копирование ящика или измените фильтр папок/дат.")

    export_id = ctx.db.create_export(acc.id, engine.name, engine.fmt, "", p, ctx.job_id)
    options = {
        "pst_format": p.get("pst_format", svc.rt("export", "pst_format")),
        "aspose_license_path": p.get("aspose_license_path", svc.rt("export", "aspose_license_path")),
        "tmp_dir": svc.cfg.tmp_dir,
        "outlook_target": p.get("outlook_target", svc.rt("export", "outlook_target")),
    }

    items = svc.iter_mail_items(acc.id, folders, p.get("date_from"), p.get("date_to"), int(p.get("limit", 0) or 0))

    if engine.fmt in ("eml", "mbox"):
        work_dir = os.path.join(svc.cfg.tmp_dir, base_name)
        shutil.rmtree(work_dir, ignore_errors=True)
        res = engine.export(items, work_dir, options=options, progress_cb=ctx.progress,
                            cancel_cb=ctx.is_cancelled, total_hint=total)
        if ctx.is_cancelled():
            shutil.rmtree(work_dir, ignore_errors=True)
            ctx.db.update_export(export_id, status=JobStatus.CANCELLED)
            raise JobCancelled("Экспорт отменён пользователем.")
        final_path = os.path.join(svc.cfg.exports_dir, base_name + ".zip")
        ctx.event("INFO", "Упаковка результата в ZIP-архив…")
        size = zip_directory(work_dir, final_path, arc_root=base_name)
        shutil.rmtree(work_dir, ignore_errors=True)
    else:
        final_path = os.path.join(svc.cfg.exports_dir, base_name + "." + engine.fmt)
        res = engine.export(items, final_path, options=options, progress_cb=ctx.progress,
                            cancel_cb=ctx.is_cancelled, total_hint=total)
        if ctx.is_cancelled():
            # Файл отменённого экспорта в БД не попадает, поэтому в интерфейсе он
            # не виден и удалить его оттуда нельзя — убираем сами, как это уже
            # делается с рабочим каталогом в ветке eml/mbox.
            if os.path.exists(final_path):
                try:
                    os.unlink(final_path)
                except OSError as exc:
                    log.warning("Не удалось удалить файл отменённого экспорта %s: %s", final_path, exc)
            ctx.db.update_export(export_id, status=JobStatus.CANCELLED)
            raise JobCancelled("Экспорт отменён пользователем.")
        size = os.path.getsize(final_path) if os.path.exists(final_path) else 0

    status = JobStatus.PARTIAL if res.errors else JobStatus.SUCCESS
    ctx.db.update_export(export_id, status=status, path=final_path, size=size,
                         error="; ".join(res.error_details[:5]))
    if res.warning:
        ctx.event("WARNING", res.warning)
    summary = (f"Экспортировано писем: {res.count}, файл: {os.path.basename(final_path)} "
               f"({human_size(size)}), ошибок: {res.errors}.")
    ctx.event("INFO", summary)
    return {"final_status": status, "summary": summary, "path": final_path,
            "count": res.count, "size": size, "warning": res.warning, "export_id": export_id}


# ---------------------------------------------------------------------------
#  IMPORT PST (через readpst) -> в локальную копию или на IMAP
# ---------------------------------------------------------------------------
def handle_import_pst(ctx: JobContext) -> Dict:
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
    ctx.event("INFO", f"Импорт PST «{os.path.basename(pst_path)}» (цель: {target}).")

    tmp = tempfile.mkdtemp(prefix="pstimport_", dir=svc.cfg.tmp_dir)
    try:
        # -e: .eml по файлу; -o: каталог; -D: включая удалённые
        proc = subprocess.run(["readpst", "-e", "-o", tmp, pst_path],
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise MailArchiverError(f"readpst завершился с ошибкой: {proc.stdout} {proc.stderr}"[:400],
                                    code="import_error")
        eml_files = []
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                if fn.lower().endswith(".eml"):
                    rel_folder = os.path.relpath(root, tmp)
                    eml_files.append((rel_folder, os.path.join(root, fn)))
        total = len(eml_files)
        ctx.event("INFO", f"В PST найдено писем: {total}.")

        imported = 0
        errors = 0
        if target == "imap":
            acc = svc.require_account(ctx.account_id)
            prefix = p.get("target_prefix", "Импорт PST")
            with ImapConnection(acc, svc.connect_options()) as conn:
                # list_folders() заодно сообщает разделитель иерархии сервера;
                # подстраховываемся на случай, если сервер его не прислал —
                # иначе None попал бы в имя папки строкой "None".
                conn.list_folders()
                delimiter = conn.delimiter or "/"
                ensured = set()
                failed_folders = set()
                for i, (rel_folder, fp) in enumerate(eml_files, 1):
                    if ctx.is_cancelled():
                        raise JobCancelled("Импорт отменён пользователем.")
                    # разделитель каталогов ОС переводим в разделитель IMAP
                    rel = _rel_to_imap_folder(rel_folder, delimiter)
                    folder = f"{prefix}{delimiter}{rel}" if rel else prefix
                    if folder in failed_folders:
                        errors += 1
                        continue
                    if folder not in ensured:
                        try:
                            conn.ensure_folder(folder)
                        except MailArchiverError as exc:
                            # папку создать не удалось — письма этой папки
                            # пропускаем, но весь импорт не роняем
                            failed_folders.add(folder)
                            errors += 1
                            ctx.event("ERROR", f"Папка «{folder}» недоступна: {exc.message}")
                            continue
                        ensured.add(folder)
                    try:
                        with open(fp, "rb") as fh:
                            conn.append(folder, fh.read())
                        imported += 1
                    except MailArchiverError as exc:
                        errors += 1
                        ctx.event("ERROR", f"Ошибка заливки: {exc.message}")
                    except OSError as exc:
                        errors += 1
                        ctx.event("ERROR", f"Не удалось прочитать файл «{os.path.basename(fp)}»: {exc}")
                    if i % 20 == 0:
                        ctx.progress(i, total, f"Импорт {i}/{total}")
        else:
            # локально: складываем в спец-«ящик»-каталог импорта (как отдельный аккаунт не создаём)
            raise ValidationError("Импорт PST в локальную копию пока выполняется только на IMAP-сервер.",
                                  hint="Выберите цель «на IMAP-сервер».")
        summary = f"Импортировано из PST: {imported} писем, ошибок: {errors}."
        ctx.progress(total, total, "Готово")
        status = JobStatus.PARTIAL if errors else JobStatus.SUCCESS
        return {"final_status": status, "summary": summary, "imported": imported, "errors": errors}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # загруженный .pst больше не нужен: без удаления он навсегда оставался
        # бы в каталоге временных файлов
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
    svc = ctx.services
    global_days = int(svc.rt("retention", "keep_days") or 0)
    removed = 0
    freed = 0
    accounts = [svc.require_account(ctx.account_id)] if ctx.account_id else svc.db.list_accounts()
    # Сначала план: по каким ящикам и сколько писем подлежит удалению —
    # это даёт корректный прогресс и не требует держать выборку в памяти.
    plan = []
    for acc in accounts:
        # приоритет у настройки ящика: -1 = наследовать глобальную; 0 = хранить всё; N = N дней
        eff_days = acc.retention_days if acc.retention_days is not None and acc.retention_days >= 0 else global_days
        if not eff_days or eff_days <= 0:
            continue
        cutoff = datetime.now(timezone.utc).timestamp() - eff_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        plan.append((acc, eff_days, cutoff_iso, _count_older_than(svc.db, acc.id, cutoff_iso)))
    total = sum(p[3] for p in plan)
    ctx.progress(0, total, f"К удалению писем: {total}")
    for acc, eff_days, cutoff_iso, planned in plan:
        ctx.event("INFO", f"Ящик «{acc.name}»: хранение {eff_days} дн., к удалению писем {planned}.")
        # Выбираем ровно то, что подлежит удалению, порциями от самых старых.
        # Каждая порция удаляется целиком, поэтому следующий запрос снова
        # возвращает самые старые из оставшихся (смещение не нужно).
        while True:
            rows = _page_older_than(svc.db, acc.id, cutoff_iso, MESSAGE_PAGE_SIZE)
            if not rows:
                break
            for row in rows:
                if ctx.is_cancelled():
                    ctx.event("WARNING", f"Очистка прервана: удалено {removed} ({human_size(freed)}).")
                    raise JobCancelled("Очистка отменена пользователем.")
                try:
                    svc.store.delete_message(acc.id, row["stored_path"])
                except Exception:  # noqa: BLE001
                    pass
                svc.db.delete_message_index(row["id"])
                removed += 1
                freed += row["size"] or 0
                if removed % 50 == 0:
                    ctx.progress(removed, total, f"Удалено {removed}/{total} ({human_size(freed)})")
            if len(rows) < MESSAGE_PAGE_SIZE:
                break
    ctx.progress(total, total, "Готово")
    ctx.event("INFO", f"Ретеншн: удалено писем {removed} ({human_size(freed)}).")
    return {"final_status": JobStatus.SUCCESS, "summary": f"Удалено {removed} писем, освобождено {human_size(freed)}.",
            "removed": removed, "freed": freed}


# ---------------------------------------------------------------------------
#  VERIFY (проверка целостности локальной копии)
# ---------------------------------------------------------------------------
def handle_verify(ctx: JobContext) -> Dict:
    svc = ctx.services
    from ..util import sha256_hex
    acc = svc.require_account(ctx.account_id)
    # обходим ВСЕ письма ящика постранично (см. _iter_all_messages)
    total = svc.db.count_messages(acc.id)
    missing = 0
    corrupt = 0
    ok = 0
    i = 0
    for row in _iter_all_messages(svc.db, acc.id):
        i += 1
        if ctx.is_cancelled():
            raise JobCancelled("Проверка отменена пользователем.")
        path = os.path.join(svc.store.account_dir(acc.id), row["stored_path"])
        if not os.path.exists(path):
            missing += 1
            ctx.event("ERROR", f"Отсутствует файл: {row['stored_path']}")
        else:
            try:
                data = svc.store.read_message(acc.id, row["stored_path"])
                if row["sha256"] and sha256_hex(data) != row["sha256"]:
                    corrupt += 1
                    ctx.event("ERROR", f"Хеш не совпал: {row['stored_path']}")
                else:
                    ok += 1
            except Exception:  # noqa: BLE001
                corrupt += 1
        if i % 50 == 0:
            ctx.progress(i, total, f"Проверка {i}/{total}")
    ctx.progress(total, total, "Готово")
    status = JobStatus.SUCCESS if (missing == 0 and corrupt == 0) else JobStatus.PARTIAL
    summary = f"Проверено {total}: целых {ok}, отсутствуют {missing}, повреждены {corrupt}."
    ctx.event("INFO", summary)
    return {"final_status": status, "summary": summary, "ok": ok, "missing": missing, "corrupt": corrupt}


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
    ctx.event("INFO", summary)
    svc.db.add_audit("system", "employees_sync", summary[:500])
    status = JobStatus.SUCCESS if not problems else JobStatus.PARTIAL
    return {"final_status": status, "summary": summary,
            "created": result["created"], "updated": result["updated"],
            "accounts_created": result["accounts_created"],
            "accounts_linked": result["accounts_linked"],
            "total_rows": result["total_rows"], "problems": problems[:200]}


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
}
