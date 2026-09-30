"""
Прежние копии ящиков (карантин после копирования «с нуля»): сравнение с новой
копией и возврат в архив писем, которых в новой копии нет.

Зачем. Копия «с нуля» не стирает прежние файлы, а переименовывает каталог
ящика в ``account_N_old_<дата>``. Письма, которых на почтовом сервере уже нет,
после этого лежат ТОЛЬКО там: в просмотре, поиске и выгрузке их не видно.
Карантин занимает столько же места, сколько весь архив ящика, и рано или поздно
его захочется удалить — прежде надо знать, нет ли в нём таких писем, и уметь
вернуть их в архив.

Как сравниваем. У каждого файла прежней копии читается только блок заголовков
(Message-ID), а начало SHA-256 содержимого берётся из имени файла (его пишет
:meth:`MaildirStore.store_message`). Письмо «есть в новой копии», если в
индексе ящика есть письмо с тем же содержимым (SHA-256) и его файл на месте.
Тот же Message-ID, тема, отправитель и дата при другом содержимом — «другой
вариант» того же письма (сервер пересобрал заголовки); другое содержимое под
тем же Message-ID (сканеры и рассылки его повторяют) — «только в прежней
копии». Если Message-ID и начало SHA-256 из имени файла не совпали, письмо
сверяется по полному SHA-256 содержимого.

Возврат. Письма «только в прежней копии» записываются в текущую копию ящика
(в те же папки) и попадают в индекс как исторические — под особым
UIDVALIDITY (:data:`RESCUED_UIDVALIDITY`), которого у настоящего сервера не
бывает: копирование их не трогает и заново не скачивает. Возврат возможен
только после того, как копирование хотя бы раз прочитало все папки ящика; письма
старше срока хранения ящика и письма папок, которые сейчас не открываются на
сервере, не возвращаются.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import DiskSpaceError, JobCancelled, MailArchiverError, ValidationError
from .logging_setup import get_logger
from .util import human_size

log = get_logger("quarantine")

#: UIDVALIDITY для писем, возвращённых из прежней копии. У IMAP-сервера это
#: число всегда положительное — отрицательное ни с чем не совпадёт.
RESCUED_UIDVALIDITY = -1
#: Сколько писем «только в прежней копии» показывать списком.
SAMPLE_LIMIT = 50
#: Итог сравнения хранится в meta под этим префиксом + имя каталога прежней копии.
META_PREFIX = "quarantine_check:"
#: Когда ящик последний раз пересоздавался «с нуля» (meta, ISO UTC): ставит копирование.
REBUILD_META = "last_rebuild_at:"

_NAME_RE = re.compile(r"^(\d+)\.M(\d+)Q\d+P\d+\.([0-9a-f]{8})\.")
_HEAD_LIMIT = 256 * 1024


@dataclass
class QFile:
    path: str          # абсолютный путь файла
    folder_rel: str    # каталог папки относительно прежней копии (как на диске)
    epoch: int         # дата письма из имени файла (0 — неизвестна)
    uid: int
    digest8: str       # начало SHA-256 содержимого из имени файла («» — имя незнакомое)
    flags: str         # буквы флагов Maildir
    disk_size: int


def _parse_name(name: str) -> Tuple[int, int, str, str]:
    base = name
    for suffix in (".enc", ".gz"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    flags = base.split(":2,", 1)[1] if ":2," in base else ""
    m = _NAME_RE.match(base)
    if not m:
        return 0, 0, "", flags
    return int(m.group(1)), int(m.group(2)), m.group(3), flags


def iter_files(qpath: str):
    """Файлы писем прежней копии (подкаталоги cur/ и new/ каждой папки)."""
    for root, dirs, files in os.walk(qpath):
        dirs.sort()
        if os.path.basename(root) not in ("cur", "new"):
            continue
        folder_rel = os.path.relpath(os.path.dirname(root), qpath)
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            path = os.path.join(root, fn)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            epoch, uid, digest8, flags = _parse_name(fn)
            yield QFile(path, folder_rel, epoch, uid, digest8, flags, size)


def meta_key(qpath: str) -> str:
    return META_PREFIX + os.path.basename(os.path.normpath(qpath))


def stored_check(db, qpath: str) -> Optional[Dict[str, Any]]:
    """Итог последнего сравнения этой прежней копии (None — не сравнивалась)."""
    raw = db.get_meta(meta_key(qpath))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def overview(svc) -> Dict[str, Any]:
    """Все прежние копии разом — для еженедельной сводки и метрик.

    Каталоги не обходятся (на копии в сотни тысяч файлов это минуты): размер и
    число писем «только здесь» берутся из итога последнего сравнения, а копии,
    которые ещё не сравнивались, считаются отдельно.
    """
    names = svc.db.account_names()
    items: List[Dict[str, Any]] = []
    for account_id in sorted(svc.store.quarantine_counts()):
        for qpath in svc.store.quarantine_paths(account_id):
            check = stored_check(svc.db, qpath)
            items.append({
                "account_id": account_id,
                "account": names.get(account_id) or f"№{account_id} (ящик удалён)",
                "path": qpath, "name": os.path.basename(qpath),
                "created_at": svc.store.quarantine_created_at(qpath),
                "checked": check is not None,
                "bytes": int(check.get("disk_bytes") or 0) if check else 0,
                "unique": int(check.get("unique") or 0) if check else 0,
                "unique_bytes": int(check.get("unique_bytes") or 0) if check else 0,
            })
    created = [i["created_at"] for i in items if i["created_at"]]
    return {
        "count": len(items),
        "accounts": len({i["account_id"] for i in items}),
        "checked": sum(1 for i in items if i["checked"]),
        "unchecked": sum(1 for i in items if not i["checked"]),
        "bytes": sum(i["bytes"] for i in items),
        "with_unique": sum(1 for i in items if i["unique"]),
        "unique": sum(i["unique"] for i in items),
        "unique_bytes": sum(i["unique_bytes"] for i in items),
        "oldest_created_at": min(created) if created else None,
        "items": items,
    }


def forget(db, qpath: str) -> None:
    """Прежнюю копию удалили — её итог сравнения больше не нужен."""
    try:
        db.delete_meta(meta_key(qpath))
    except Exception:  # noqa: BLE001
        log.debug("Не удалось удалить итог сравнения %s", qpath, exc_info=True)


def is_safe_to_delete(check: Optional[Dict[str, Any]],
                      retention_days: Optional[int] = None) -> Tuple[bool, str]:
    """Можно ли удалять прежнюю копию без потери писем: ``(да/нет, почему нет)``.

    ``retention_days`` — срок хранения ящика сейчас: если часть писем при
    сравнении сочли «старше срока хранения», а срок с тех пор изменился (например,
    на «хранить всё»), прежний итог больше не годится.
    """
    if not check:
        return False, "прежняя копия не сравнивалась с новой — сначала «Сравнить»"
    if (retention_days is not None and int(check.get("outside_retention") or 0)
            and int(check.get("retention_days") or 0) != int(retention_days)):
        return False, "срок хранения ящика изменился после сравнения — сравните прежнюю копию ещё раз"
    unique = int(check.get("unique") or 0)
    unreadable = int(check.get("unreadable") or 0)
    if unique:
        blocked = int(check.get("unique_unreadable") or 0)
        if blocked and blocked >= unique:
            return False, (f"в прежней копии {unique} писем из папок, которые сейчас не открываются на сервере, — "
                           f"когда папки снова начнут копироваться, сравните ещё раз")
        return False, (f"в прежней копии {unique} писем, которых нет в новой, — "
                       f"сначала «Вернуть письма из прежних копий»")
    if unreadable:
        return False, f"{unreadable} файлов прежней копии не удалось прочитать при сравнении"
    return True, ""


def check_label(check: Optional[Dict[str, Any]]) -> str:
    """Итог сравнения одной строкой — для списков и групповых действий."""
    if not check:
        return "не сравнивалась с новой копией"
    parts = [f"есть в новой копии {int(check.get('identical') or 0)}"]
    if check.get("other_version"):
        parts.append(f"другой вариант {int(check['other_version'])}")
    unique = int(check.get("unique") or 0)
    parts.append(f"только в прежней {unique}" + (f" ({check.get('unique_bytes_h')})" if unique else ""))
    if check.get("outside_retention"):
        parts.append(f"старше срока хранения {int(check['outside_retention'])}")
    if check.get("unreadable"):
        parts.append(f"не прочитано {int(check['unreadable'])}")
    return ", ".join(parts)


#: Блок заголовков читаем порциями — до первой пустой строки, а не 256 КБ каждого письма.
_HEAD_CHUNK = 16 * 1024


def _read_head(store, path: str) -> bytes:
    fh = store.open_quarantine_file(path)
    head = b""
    try:
        while len(head) < _HEAD_LIMIT:
            chunk = fh.read(min(_HEAD_CHUNK, _HEAD_LIMIT - len(head)))
            if not chunk:
                break
            head += chunk
            if b"\n\r\n" in head or b"\n\n" in head or head[:2] == b"\r\n" or head[:1] == b"\n":
                break
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass
    from .imap.backup import BackupEngine
    return BackupEngine._header_block(head)  # noqa: SLF001 — тот же разбор, что при копировании


def _sample(head: bytes, qf: QFile, folder: str) -> Dict[str, Any]:
    from .imap.backup import BackupEngine
    subject, from_addr, _att = BackupEngine._extract_meta(head)  # noqa: SLF001
    date = (datetime.fromtimestamp(qf.epoch, tz=timezone.utc).isoformat() if qf.epoch else "")
    return {"folder": folder, "subject": (subject or "")[:200], "from": (from_addr or "")[:200], "date": date,
            "size": qf.disk_size}


def _require_quarantine(store, account_id: int, qpath: str) -> str:
    norm = os.path.normpath(qpath or "")
    for path in store.quarantine_paths(account_id):
        if os.path.normpath(path) == norm:
            return path
    raise ValidationError("Такой прежней копии у этого ящика нет.", hint="Обновите список прежних копий.")


def _iso_epoch(value: str) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _same_letter_key(subject: str, from_addr: str) -> int:
    """Отпечаток «то же письмо»: тема и отправитель (без различий в пробелах)."""
    return hash((" ".join((subject or "").split())[:500], " ".join((from_addr or "").split())[:300]))


#: Письма с одним Message-ID, темой и отправителем — одно письмо, если даты
#: расходятся не больше чем на сутки (сервер мог пересобрать заголовки).
_SAME_LETTER_S = 86400 + 60


def _problem_folder_rels(svc, account_id: int) -> set:
    """Каталоги на диске папок, которые сейчас не открываются на сервере, но
    копирование их по-прежнему пробует (не исключены настройками).

    Письма таких папок не возвращаются из прежней копии: когда папка заработает,
    они скачаются, и в архиве их стало бы два. Папку, исключённую из
    копирования, никто больше не скачает — её письма возвращать можно.
    """
    from .imap.client import folder_matches
    out = set()
    try:
        rows = svc.db.list_folder_problems(account_id)
    except Exception:  # noqa: BLE001
        return out
    account = svc.db.get_account(account_id)
    include = [str(x).strip() for x in list(getattr(account, "folder_include", None) or [])
               + list(svc.rt("backup", "folder_include") or []) if str(x).strip()]
    exclude = [str(x).strip() for x in list(getattr(account, "folder_exclude", None) or [])
               + list(svc.rt("backup", "folder_exclude") or []) if str(x).strip()]
    # Разделитель папок сервера копирование запоминает вместе с нечитаемой
    # папкой (с 1.7.0). Для записей постарше — по вложенным папкам, которые уже
    # есть в копии (угадывать по имени нельзя: «Trash.old» на сервере с «/» — не
    # «Trash/old»), а каталог на диске тогда блокируется при любом разделителе.
    guessed: Optional[str] = None
    for row in rows:
        name = row["folder"]
        known = str(_row_value(row, "delimiter") or "")
        if not known and guessed is None:
            seen: Dict[str, int] = {}
            for folder_name, folder_delim in _folder_map(svc, account_id).values():
                if any(d in folder_name for d in _DELIMITERS):
                    seen[folder_delim] = seen.get(folder_delim, 0) + 1
            guessed = max(seen, key=seen.get) if seen else "/"
        delim = known or guessed or "/"
        if (include and not folder_matches(name, include, delim)) or (exclude and folder_matches(name, exclude, delim)):
            continue
        for variant in ((known,) if known else _DELIMITERS):
            try:
                out.add(os.path.normpath(svc.store.folder_relpath(name, variant)))
            except Exception:  # noqa: BLE001
                continue
    return out


def _row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def new_copy_complete(svc, account_id: int, qpath: str) -> bool:
    """Прошло ли ПОЛНОЕ копирование ящика (все папки прочитаны) после последней
    копии «с нуля» — и этой прежней копии, и более поздних.

    Пока нет, «только в прежней копии» окажутся и письма папок, которые новая
    копия ещё не прочитала, — вернуть их значило бы получить их дважды. Ошибки
    отдельных писем (сервер не отдаёт одно письмо) полноту не нарушают: иначе
    одно битое письмо на сервере навсегда запрещало бы возврат.
    """
    since = [svc.store.quarantine_created_at(qpath), _iso_epoch(svc.db.get_meta(f"{REBUILD_META}{account_id}") or "")]
    since = [x for x in since if x is not None]
    done = _iso_epoch(svc.db.complete_backup_at(account_id))
    if done is None:
        return False
    return not since or done > max(since)


def _body_digest(fh) -> str:
    """SHA-256 тела письма (после блока заголовков); переводы строк приводятся к LF."""
    h = hashlib.sha256()
    head = b""
    rest = b""
    while True:
        chunk = fh.read(65536)
        if not chunk:
            break
        head += chunk
        m = re.search(rb"\r?\n\r?\n", head)
        if m:
            rest = head[m.end():]
            break
        if len(head) > 4 * _HEAD_LIMIT:
            break                                  # заголовков больше мегабайта — тела нет
    carry = b""
    data = rest
    while True:
        data = carry + data
        carry = b""
        if data.endswith(b"\r"):
            carry, data = b"\r", data[:-1]
        h.update(data.replace(b"\r\n", b"\n"))
        data = fh.read(65536)
        if not data:
            break
    h.update(carry)
    return h.hexdigest()


def _digest_of(open_fn) -> str:
    fh = open_fn()
    try:
        return _body_digest(fh)
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass


def compare(svc, account_id: int, qpath: str, *, collect: bool = False,
            progress: Optional[Callable[[int, int, str], None]] = None,
            cancelled: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Сравнить прежнюю копию с текущей копией ящика.

    Письмо прежней копии:

    * «есть в новой копии» — в новой копии есть письмо с тем же содержимым
      (SHA-256) и его файл на месте. Одних записей индекса мало: если файл
      новой копии пропал, удалять прежнюю копию нельзя;
    * «другой вариант» — тот же Message-ID, тема, отправитель, дата (± сутки)
      и то же тело письма, но другие заголовки: сервер пересобрал заголовки
      того же письма;
    * «только в прежней копии» — всё остальное, в том числе письма с чужим
      содержимым под тем же Message-ID (сканеры и рассылки повторяют его);
    * «старше срока хранения» — «только в прежней», но старше срока хранения
      ящика: ночная очистка удалила бы их из архива и так.

    Итог сохраняется в meta (его показывают список прежних копий и групповые
    действия). ``collect=True`` дополнительно возвращает в ``unique_files``
    файлы, которых нет в новой копии (для возврата).
    """
    from .imap.backup import BackupEngine
    from .imap.client import header_value
    from .queue.jobs import effective_retention_days
    store = svc.store
    qpath = _require_quarantine(store, account_id, qpath)
    account = svc.db.get_account(account_id)
    days = effective_retention_days(svc, account) if account is not None else 0
    cutoff = (datetime.now(timezone.utc).timestamp() - days * 86400) if days > 0 else None
    by_mid: Dict[str, List[Tuple[str, str, int, Optional[float]]]] = {}
    by_sha: Dict[str, str] = {}
    for row in svc.db.iter_message_digests(account_id):
        sha, path = row["sha256"], row["stored_path"]
        if sha and sha not in by_sha:
            by_sha[sha] = path
        if row["message_id"]:
            by_mid.setdefault(row["message_id"], []).append(
                (sha, path, _same_letter_key(row["subject"], row["from_addr"]), _iso_epoch(row["internaldate"])))
    present: Dict[str, bool] = {}
    bodies: Dict[str, str] = {}          # путь файла новой копии -> SHA-256 его тела

    def on_disk(rel: str) -> bool:
        if rel not in present:
            try:
                present[rel] = bool(rel) and os.path.isfile(store.message_path(account_id, rel))
            except MailArchiverError:
                present[rel] = False
        return present[rel]

    blocked_rels = _problem_folder_rels(svc, account_id)
    files = list(iter_files(qpath))
    total = len(files)
    identical = other = unique = unreadable = old = unique_blocked = 0
    unique_bytes = disk_bytes = 0
    samples: List[Dict[str, Any]] = []
    unique_files: List[QFile] = []
    for i, qf in enumerate(files, start=1):
        if cancelled is not None and i % 100 == 0 and cancelled():
            raise JobCancelled("Сравнение прервано.")
        disk_bytes += qf.disk_size
        try:
            head = _read_head(store, qf.path)
            mid = header_value(head, "Message-ID")[:250]
            cands = [c for c in by_mid.get(mid, ()) if on_disk(c[1])] if mid else []
            if cands and qf.digest8 and any(c[0].startswith(qf.digest8) for c in cands):
                # Тот же Message-ID и то же начало SHA-256 — то же письмо (ошибиться можно
                # лишь при совпадении 32 бит у писем с одним Message-ID).
                verdict = "identical"
            else:
                # Всё остальное — по полному SHA-256 содержимого: Message-ID в индексе мог
                # записаться иначе (старые версии брали его из первых 8 КБ заголовков), а
                # решение «только здесь» ведёт к возврату письма и запрету удаления.
                sha, _size = store.hash_quarantine_file(qf.path)
                if on_disk(by_sha.get(sha, "")):
                    verdict = "identical"
                elif cands:
                    # Тот же Message-ID, но другое содержимое: пересобранные заголовки
                    # того же письма — или другое письмо (сканеры повторяют Message-ID
                    # у каждого скана с той же темой). Решает тело письма.
                    subject, from_addr, _att = BackupEngine._extract_meta(head)  # noqa: SLF001
                    key = _same_letter_key(subject, from_addr)
                    near = [c for c in cands if c[2] == key and c[3] is not None and qf.epoch
                            and abs(c[3] - qf.epoch) <= _SAME_LETTER_S]
                    same = False
                    if near:
                        body = _digest_of(lambda: store.open_quarantine_file(qf.path))
                        for c in near:
                            if c[1] not in bodies:
                                bodies[c[1]] = _digest_of(lambda rel=c[1]: store.open_message(account_id, rel))
                            if bodies[c[1]] == body:
                                same = True
                                break
                    verdict = "other" if same else "unique"
                else:
                    verdict = "unique"
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - нет ключа шифрования, битый файл
            unreadable += 1
            if unreadable <= 3:
                log.warning("Прежняя копия %s: не прочитан %s: %s", qpath, qf.path,
                            getattr(exc, "message", None) or exc)
            continue
        if verdict == "unique" and cutoff is not None and qf.epoch and qf.epoch < cutoff:
            verdict = "old"
        if verdict == "identical":
            identical += 1
        elif verdict == "other":
            other += 1
        elif verdict == "old":
            old += 1
        else:
            unique += 1
            unique_bytes += qf.disk_size
            if os.path.normpath(qf.folder_rel) in blocked_rels:
                unique_blocked += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(_sample(head, qf, qf.folder_rel.replace(os.sep, "/")))
            if collect:
                unique_files.append(qf)
        if progress is not None and (i % 200 == 0 or i == total):
            progress(i, total, f"Сравнено файлов: {i} из {total}")
    previous = stored_check(svc.db, qpath) or {}
    result: Dict[str, Any] = {
        "path": qpath, "name": os.path.basename(qpath),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "files": total, "disk_bytes": disk_bytes, "identical": identical, "other_version": other,
        "unique": unique, "unique_bytes": unique_bytes, "unique_bytes_h": human_size(unique_bytes),
        "unique_unreadable": unique_blocked, "outside_retention": old, "retention_days": days,
        "unreadable": unreadable, "samples": samples,
        "new_copy_complete": new_copy_complete(svc, account_id, qpath),
        "rescued": int(previous.get("rescued") or 0),
    }
    svc.db.set_meta(meta_key(qpath), json.dumps(result, ensure_ascii=False))
    if collect:
        result["unique_files"] = unique_files
    return result


_DELIMITERS = ("/", ".", "\\", "|", ":")


def _folder_map(svc, account_id: int) -> Dict[str, Tuple[str, str]]:
    """Каталог папки на диске → ``(имя папки IMAP, разделитель)`` по путям уже сохранённых писем.

    Разделитель подбирается такой, при котором имя папки даёт тот же каталог:
    тогда возвращённое письмо ляжет рядом с остальными письмами этой папки.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for row in svc.db.folder_disk_paths(account_id):
        rel = os.path.normpath(os.path.dirname(os.path.dirname(row["stored_path"] or "")))
        if not rel or rel == "." or rel in out:
            continue
        name = row["folder"]
        for delim in _DELIMITERS:
            if os.path.normpath(svc.store.folder_relpath(name, delim)) == rel:
                out[rel] = (name, delim)
                break
    return out


def rescue(svc, account, qpath: str, *,
           progress: Optional[Callable[[int, int, str], None]] = None,
           cancelled: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Вернуть в текущую копию ящика письма, которые есть только в прежней копии.

    Только после того, как копирование хотя бы раз прочитало все папки ящика: иначе
    письма из ещё не прочитанных папок выглядели бы «только в прежней копии»,
    вернулись бы в архив, а потом скачались бы ещё раз. Письма из папок, которые
    сейчас не открываются на сервере, не возвращаются по той же причине.
    """
    from .imap.backup import BackupEngine
    from .analytics import invalidate_mail_analytics_cache
    from .storage.maildir_store import maildir_flags_to_imap
    store = svc.store
    db = svc.db
    qpath = _require_quarantine(store, account.id, qpath)
    if not new_copy_complete(svc, account.id, qpath):
        raise ValidationError(
            f"Письма из «{os.path.basename(qpath)}» не возвращаются: после копии «с нуля» копирование ещё не "
            f"прочитало все папки ящика.",
            hint="Письма из папок, которые новая копия ещё не прочитала, выглядели бы «только в прежней копии» и "
                 "попали бы в архив дважды. Дождитесь копирования, которое прочитает все папки (ошибки отдельных "
                 "писем не мешают), и повторите.")
    found = compare(svc, account.id, qpath, collect=True, progress=progress, cancelled=cancelled)
    files: List[QFile] = found.pop("unique_files")
    blocked_rels = _problem_folder_rels(svc, account.id)
    folders = _folder_map(svc, account.id)
    next_uid: Dict[str, int] = {}
    rows: List[tuple] = []
    rescued = skipped_blocked = failed = 0
    rescued_bytes = 0

    def flush() -> None:
        if rows:
            db.add_message_index_batch(rows)
            rows.clear()

    try:
        for i, qf in enumerate(files, start=1):
            if cancelled is not None and cancelled():
                raise JobCancelled("Возврат писем прерван.")
            if os.path.normpath(qf.folder_rel) in blocked_rels:
                skipped_blocked += 1
                continue
            # Папка есть в новой копии — письмо ляжет к её письмам; нет (папку удалили
            # на сервере) — папка с тем же именем, что и каталог прежней копии.
            name, delim = folders.get(os.path.normpath(qf.folder_rel)) or (qf.folder_rel.replace(os.sep, "/"), "/")
            try:
                raw = store.read_quarantine_file(qf.path)
                if name not in next_uid:
                    next_uid[name] = db.max_uid(account.id, name, RESCUED_UIDVALIDITY)
                next_uid[name] += 1
                uid = next_uid[name]
                flags = maildir_flags_to_imap(qf.flags)
                rel, sha, size = store.store_message(account.id, name, delim, uid, raw, flags=flags,
                                                     internaldate=qf.epoch or None)
                subject, from_addr, has_attach = BackupEngine._extract_meta(raw)  # noqa: SLF001
                message_id = BackupEngine._extract_message_id(raw)  # noqa: SLF001
                rows.append((account.id, name, RESCUED_UIDVALIDITY, uid, message_id, size,
                             BackupEngine._epoch_to_iso(qf.epoch) if qf.epoch else "",  # noqa: SLF001
                             ",".join(flags), rel, sha, subject[:500], from_addr[:300], 1 if has_attach else 0))
                del raw
            except DiskSpaceError:
                raise                              # место кончилось — дальше не запишется ни одно
            except MailArchiverError as exc:
                failed += 1
                log.warning("Не удалось вернуть письмо %s: %s", qf.path, exc.message)
                continue
            rescued += 1
            rescued_bytes += size
            if len(rows) >= 200:
                flush()
            if progress is not None and (i % 50 == 0 or i == len(files)):
                progress(i, len(files), f"Возвращено писем: {rescued} из {len(files)}")
    finally:
        flush()
        if rescued:
            invalidate_mail_analytics_cache()
    after = compare(svc, account.id, qpath)
    after["rescued"] = int(found.get("rescued") or 0) + rescued
    db.set_meta(meta_key(qpath), json.dumps(after, ensure_ascii=False))
    return {"rescued": rescued, "rescued_bytes": rescued_bytes,
            "skipped_old": int(found.get("outside_retention") or 0), "skipped_unreadable": skipped_blocked,
            "failed": failed, "retention_days": int(found.get("retention_days") or 0),
            "before": {k: v for k, v in found.items() if k != "samples"}, "after": after}
