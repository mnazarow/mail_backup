"""
Копия архива вне сервера: что и как синхронизируется.

В копию попадают:
  * ``mailboxes/`` — файлы писем всех ящиков (как лежат в каталоге данных:
    открытые, сжатые или зашифрованные). Карантинные каталоги прежних копий
    (``account_N_old_…``) не копируются;
  * ``db/`` — последние снимки базы (``replica.db_snapshot_keep``);
  * ``README-MailArchiver.txt`` — как восстановиться из этой копии;
  * ``.mailarchiver-replica`` — метка «это копия именно этого архива».

Ключи (``secret.key``, ключ шифрования писем) в копию НЕ попадают: иначе
укравший копию получил бы и замок, и ключ. Их нужно хранить отдельно.

Как понимаем, что уже скопировано. Для сетевой папки и S3 служба ведёт
таблицу ``replica_files`` (путь, размер, время изменения): каждый прогон
обходит локальные файлы и отправляет только новые и изменённые, а удалённые
локально — удаляет и в копии. Раз в ``replica.verify_every_days`` дней (и после
смены места копии) делается полная сверка со списком файлов в самой копии —
так обнаруживаются файлы, пропавшие на стороне копии. rsync сверяет всё сам.

Защита от «разрушительной синхронизации»: если прогон собирается удалить в
копии больше ``replica.max_delete_percent`` % файлов (например, отвалился диск с
почтой и локально «всё пропало»), удаление не выполняется, а администратор
получает предупреждение и кнопку «Разрешить удаление».
"""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..errors import JobCancelled, MailArchiverError, ReplicaError
from ..logging_setup import get_logger
from ..util import human_size
from ..version import __version__
from . import snapshots
from .s3 import S3Config
from .targets import (README_NAME, DirTarget, ReplicaTarget, RsyncTarget, S3Target,
                      parse_rsync_stats)

log = get_logger("replica")

ACCOUNT_DIR_RE = re.compile(r"^account_\d+$")
DB_GROUP = "db"
KNOWN_HOSTS_NAME = "replica_known_hosts"
SSH_KEY_NAME = "replica_ssh_key"
#: Сколько ошибок подробно показывать в журнале задания.
MAX_ERROR_EVENTS = 20
#: Нижняя граница «защиты от массового удаления»: мелкие чистки не блокируем.
MIN_DELETE_CAP = 100

_META_STATUS = "replica_last_run"
_META_OK = "replica_last_ok"
_META_VERIFY = "replica_last_verify"
_META_MARKER = "replica_marker_id"
_META_TARGET = "replica_target_fp"
#: Отпечаток места, где метка этого архива уже была (записана или найдена).
#: Если на таком месте метка пропала — это отключившийся диск (пустая точка
#: монтирования) или удалённая копия, а не новое место: сами метку не пишем.
_META_CONFIRMED = "replica_marker_fp"


# ---------------------------------------------------------------------------
#  Настройки и цель
# ---------------------------------------------------------------------------
def settings(svc) -> Dict:
    keys = ("enabled", "target", "cron", "include_db", "db_snapshot_keep", "mirror_deletions",
            "max_delete_percent", "parallel", "verify_every_days", "timeout_s", "dir_path",
            "rsync_dest", "ssh_port", "ssh_key_file", "bwlimit_kbps", "s3_endpoint", "s3_region",
            "s3_bucket", "s3_prefix", "s3_access_key", "s3_secret_key", "s3_path_style",
            "s3_storage_class", "s3_verify_ssl")
    return {key: svc.rt("replica", key) for key in keys}


def fingerprint(cfg: Dict) -> str:
    """Отпечаток МЕСТА копии: сменилось место — прежние сведения о копии недействительны."""
    kind = cfg.get("target")
    if kind == "dir":
        where = os.path.normpath(str(cfg.get("dir_path") or ""))
    elif kind == "rsync":
        where = str(cfg.get("rsync_dest") or "").rstrip("/")
    else:
        where = "|".join(str(cfg.get(k) or "") for k in ("s3_endpoint", "s3_bucket", "s3_prefix"))
    return f"{kind}:{where}"


def known_hosts_path(svc) -> str:
    return os.path.join(svc.cfg.data_dir, KNOWN_HOSTS_NAME)


def default_ssh_key_path(svc) -> str:
    return os.path.join(svc.cfg.data_dir, SSH_KEY_NAME)


def build_target(svc, cfg: Optional[Dict] = None) -> ReplicaTarget:
    cfg = cfg or settings(svc)
    kind = str(cfg.get("target") or "dir")
    timeout = int(cfg.get("timeout_s") or 120)
    if kind == "dir":
        return DirTarget(str(cfg.get("dir_path") or ""))
    if kind == "rsync":
        key = str(cfg.get("ssh_key_file") or "").strip()
        if not key and os.path.exists(default_ssh_key_path(svc)):
            key = default_ssh_key_path(svc)
        return RsyncTarget(str(cfg.get("rsync_dest") or ""), ssh_port=int(cfg.get("ssh_port") or 22),
                           ssh_key_file=key, known_hosts=known_hosts_path(svc), timeout_s=timeout,
                           bwlimit_kbps=int(cfg.get("bwlimit_kbps") or 0),
                           local_ok=bool(getattr(svc, "replica_allow_local_rsync", False)))
    if kind == "s3":
        s3 = S3Config(endpoint=str(cfg.get("s3_endpoint") or ""), bucket=str(cfg.get("s3_bucket") or ""),
                      access_key=str(cfg.get("s3_access_key") or ""),
                      secret_key=str(cfg.get("s3_secret_key") or ""),
                      region=str(cfg.get("s3_region") or ""), path_style=bool(cfg.get("s3_path_style")),
                      verify_ssl=bool(cfg.get("s3_verify_ssl")), timeout=float(timeout),
                      storage_class=str(cfg.get("s3_storage_class") or "").strip())
        return S3Target(s3, str(cfg.get("s3_prefix") or ""))
    raise ReplicaError(f"Неизвестный вид копии: «{kind}».", hint="Выберите: папка, rsync или S3.")


def readme_text(svc) -> str:
    host = socket.gethostname()
    enc = bool(svc.store.encrypt)
    lines = [
        "Копия архива почты MailArchiver",
        "================================",
        "",
        f"Сервер: {host}. Копию обновляет служба MailArchiver {__version__}.",
        "",
        "Что здесь лежит:",
        "  mailboxes/  — файлы писем всех ящиков (account_N — номер ящика в MailArchiver).",
        "                Письма *.enc зашифрованы ключом шифрования копии." if enc else
        "  mailboxes/  — файлы писем всех ящиков (account_N — номер ящика в MailArchiver).",
        "  db/         — снимки базы MailArchiver (индекс писем, ящики, настройки).",
        "                Снимки *.enc зашифрованы тем же ключом, что и письма.",
        "",
        "Ключей здесь НЕТ — и это правильно. Для восстановления понадобятся:",
        "  * secret.key из каталога данных (им зашифрованы пароли ящиков);",
        "  * файл ключа шифрования писем, если шифрование включено.",
        "Храните их отдельно от этой копии.",
        "",
        "Как восстановить архив на новом сервере:",
        "  1. Установите MailArchiver (scripts/install.sh) и остановите службу:",
        "       sudo systemctl stop mailarchiver",
        "  2. Верните файлы писем. Из сетевой папки или S3 удобнее всего командой",
        "       sudo mailarchiver replica-pull --to /var/lib/mailarchiver",
        "     (она же восстанавливает исходные имена файлов — в сетевой папке «:» в",
        "     именах писем записан как %3A). Копию по SSH верните rsync-ом в",
        "     /var/lib/mailarchiver/mailboxes/.",
        "  3. Верните базу из самого свежего снимка:",
        "       sudo mailarchiver restore-snapshot /var/lib/mailarchiver/snapshots/<снимок>",
        "  4. Положите secret.key в каталог данных (и ключ шифрования — по пути из",
        "     настроек), проверьте: sudo mailarchiver storage-key",
        "  5. Запустите службу: sudo systemctl start mailarchiver",
        "",
        "Подробно — в документации: docs/ru/13-replica.md.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
#  Метка копии
# ---------------------------------------------------------------------------
def _new_marker(svc, marker_id: str) -> Dict:
    return {"app": "MailArchiver", "id": marker_id, "host": socket.gethostname(),
            "created_at": datetime.now(timezone.utc).isoformat(), "version": __version__}


def prepare_target(svc, *, force: bool = False) -> Dict:
    """«Подготовить место»: записать метку или принять найденную прежнюю копию."""
    cfg = settings(svc)
    target = build_target(svc, cfg)
    try:
        marker = target.read_marker()
        mine = svc.db.get_meta(_META_MARKER)
        if marker and marker.get("id"):
            if marker["id"] != mine:
                # Копия этого же архива, например после переноса сервера или
                # восстановления базы: принимаем её и при следующем прогоне сверяем.
                svc.db.set_meta(_META_MARKER, marker["id"])
                svc.db.replica_clear()
                svc.db.set_meta(_META_TARGET, fingerprint(cfg))
                svc.db.set_meta(_META_CONFIRMED, fingerprint(cfg))
                svc.db.set_meta(_META_VERIFY, "")
                return {"ok": True, "action": "adopted", "target": target.describe(),
                        "message": "Найдена прежняя копия архива — она принята. Следующий прогон "
                                   "сверит её с архивом и докопирует недостающее."}
            svc.db.set_meta(_META_CONFIRMED, fingerprint(cfg))
            return {"ok": True, "action": "already", "target": target.describe(),
                    "message": "Место уже подготовлено."}
        if not target.is_empty() and not force:
            return {"ok": False, "action": "not_empty", "target": target.describe(),
                    "message": "В этом месте уже есть файлы, а метки MailArchiver нет. Укажите пустую "
                               "папку (префикс) или подтвердите, что это место можно использовать."}
        marker_id = mine or uuid.uuid4().hex
        target.write_marker(_new_marker(svc, marker_id))
        svc.db.set_meta(_META_MARKER, marker_id)
        svc.db.set_meta(_META_TARGET, fingerprint(cfg))
        svc.db.set_meta(_META_CONFIRMED, fingerprint(cfg))
        svc.db.replica_clear()
        svc.db.set_meta(_META_VERIFY, "")
        return {"ok": True, "action": "created", "target": target.describe(),
                "message": "Место для копии подготовлено."}
    finally:
        target.close()


def check_target(svc) -> Dict:
    """Проверить место копии: доступ, запись, метка, свободное место. Не бросает исключений."""
    cfg = settings(svc)
    result = {"ok": False, "target": "", "kind": cfg.get("target"), "checks": [], "marker": "",
              "free_bytes": None, "error": None, "hint": None}

    def note(ok: bool, text: str) -> None:
        result["checks"].append({"ok": ok, "text": text})

    target = None
    try:
        target = build_target(svc, cfg)
        result["target"] = target.describe()
        if isinstance(target, RsyncTarget):
            RsyncTarget.require_tools(target.remote)
            note(True, "rsync и ssh установлены")
        if isinstance(target, S3Target):
            target.client.head_bucket()
            note(True, f"Бакет «{target.client.bucket}» доступен")
        marker = target.read_marker()
        mine = svc.db.get_meta(_META_MARKER)
        if marker is None:
            empty = target.is_empty()
            if svc.db.get_meta(_META_CONFIRMED) == fingerprint(cfg):
                result["marker"] = "lost"
                note(False, "Метка копии пропала, хотя копия здесь уже была: похоже, сетевая папка или диск "
                            "не подключены, либо копию удалили. Если место действительно новое — нажмите "
                            "«Подготовить место».")
            else:
                result["marker"] = "none_empty" if empty else "none_busy"
                note(True, "Связь есть. Место пустое — метка будет записана при первом прогоне."
                     if empty else "Связь есть, но в месте уже есть чужие файлы — нажмите «Подготовить место».")
        elif marker.get("id") and marker.get("id") == mine:
            result["marker"] = "mine"
            svc.db.set_meta(_META_CONFIRMED, fingerprint(cfg))
            note(True, "Связь есть, это копия этого архива.")
        elif marker.get("id"):
            result["marker"] = "other"
            note(False, f"Здесь копия другого архива (сервер {marker.get('host') or '?'}). Если это ваша "
                        f"прежняя копия — нажмите «Подготовить место», она будет принята.")
        else:
            result["marker"] = "broken"
            note(False, "Метка копии повреждена или чужая.")
        # Пробная запись и удаление — права на запись проверяются по-настоящему.
        if target.per_file:
            probe = f".probe-{uuid.uuid4().hex[:8]}"
            with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
                fh.write(b"mailarchiver probe\n")
                local = fh.name
            try:
                target.put(probe, local)
                failed = target.delete([probe])
                note(not failed, "Запись и удаление работают" if not failed else "Файл записан, но не удаляется")
            finally:
                os.unlink(local)
        result["free_bytes"] = target.free_bytes()
        result["ok"] = all(c["ok"] for c in result["checks"])
    except MailArchiverError as exc:
        result["error"], result["hint"] = exc.message, exc.hint
        note(False, exc.message)
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        note(False, result["error"])
    finally:
        if target is not None:
            target.close()
    return result


def status(svc) -> Dict:
    """Состояние копии для интерфейса и мониторинга."""
    cfg = settings(svc)
    raw = svc.db.get_meta(_META_STATUS)
    try:
        last = json.loads(raw) if raw else None
    except ValueError:
        last = None
    snaps = snapshots.list_snapshots(svc.cfg)
    return {"enabled": bool(cfg.get("enabled")), "target": cfg.get("target"),
            "last_run": last, "last_ok": svc.db.get_meta(_META_OK) or "",
            "last_verify": svc.db.get_meta(_META_VERIFY) or "",
            "files_known": svc.db.replica_count(),
            "snapshots": [{k: s[k] for k in ("name", "size", "created_at", "encrypted")} for s in snaps],
            "ssh_public_key": _read_public_key(svc)}


def _read_public_key(svc) -> str:
    path = default_ssh_key_path(svc) + ".pub"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def generate_ssh_key(svc) -> str:
    """Создать ключ SSH для копии (ed25519, без пароля). Возвращает открытый ключ."""
    import shutil
    import subprocess
    if shutil.which("ssh-keygen") is None:
        raise ReplicaError("На сервере нет ssh-keygen.", hint="Установите: sudo apt install openssh-client.")
    path = default_ssh_key_path(svc)
    if os.path.exists(path):
        existing = _read_public_key(svc)
        if existing:
            return existing
        raise ReplicaError(f"Ключ {path} уже есть, но открытой части нет.",
                           hint=f"Восстановите {path}.pub или удалите ключ и создайте заново.")
    proc = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                           f"mailarchiver@{socket.gethostname()}", "-f", path],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise ReplicaError(f"ssh-keygen не создал ключ: {proc.stderr.strip() or proc.stdout.strip()}")
    os.chmod(path, 0o600)
    return _read_public_key(svc)


# ---------------------------------------------------------------------------
#  Прогон
# ---------------------------------------------------------------------------
class ReplicaRunner:
    def __init__(self, svc, ctx=None, *, allow_mass_delete: bool = False, force_verify: bool = False) -> None:
        self.svc = svc
        self.db = svc.db
        self.ctx = ctx
        self.cfg = settings(svc)
        self.allow_mass_delete = allow_mass_delete
        self.force_verify = force_verify
        self.mail_root = svc.cfg.mail_root
        try:
            self.parallel = max(1, min(32, int(self.cfg.get("parallel") or 4)))
        except (TypeError, ValueError):
            self.parallel = 4
        self.mirror = bool(self.cfg.get("mirror_deletions"))
        try:
            self.max_delete_pct = max(0, min(100, int(self.cfg.get("max_delete_percent"))))
        except (TypeError, ValueError):
            self.max_delete_pct = 50
        self.stats = {"files_up": 0, "bytes_up": 0, "files_deleted": 0, "errors": 0,
                      "deletions_blocked": 0, "kept_remote": 0, "files_total": 0, "verified": False,
                      "snapshot": ""}
        self._pending_deletes: List[Tuple[str, str]] = []   # (группа, путь)
        #: сколько файлов было в копии до прогона (база для «защиты от массового удаления»)
        self._delete_base = 0
        #: сколько файлов увидели в самой копии при полной сверке
        self._verify_seen = 0
        self._error_events = 0
        self._started = datetime.now(timezone.utc)

    # -- служебное --------------------------------------------------------------
    def _event(self, level: str, text: str) -> None:
        if self.ctx is not None:
            self.ctx.event(level, text)
        else:
            getattr(log, "warning" if level in ("WARNING", "ERROR") else "info")(text)

    def _progress(self, cur: int, total: int, text: str) -> None:
        if self.ctx is not None:
            self.ctx.progress(cur, total, text)

    def _cancelled(self) -> bool:
        return self.ctx is not None and self.ctx.is_cancelled()

    def _check_cancel(self) -> None:
        if self._cancelled():
            raise JobCancelled((self.ctx.stop_reason() if self.ctx else "Отменено") +
                               ". Уже скопированное сохранено — следующий прогон продолжит.")

    def _error(self, text: str) -> None:
        self.stats["errors"] += 1
        if self._error_events < MAX_ERROR_EVENTS:
            self._event("WARNING", text)
        elif self._error_events == MAX_ERROR_EVENTS:
            self._event("WARNING", "Ошибок больше — дальше они только подсчитываются.")
        self._error_events += 1

    # -- метка и место --------------------------------------------------------------
    def _ensure_marker(self, target: ReplicaTarget) -> bool:
        """Проверить метку. Возвращает True, если копию нужно сверить полностью."""
        fp = fingerprint(self.cfg)
        need_verify = False
        if self.db.get_meta(_META_TARGET) != fp:
            # Место копии сменилось: прежние сведения о загруженных файлах к нему не относятся.
            self.db.replica_clear()
            self.db.set_meta(_META_TARGET, fp)
            self.db.set_meta(_META_VERIFY, "")
            need_verify = True
        marker = target.read_marker()
        mine = self.db.get_meta(_META_MARKER)
        if marker is None:
            if self.db.get_meta(_META_CONFIRMED) == fp:
                # Здесь уже лежала наша копия, а метки нет: отключившийся сетевой
                # диск (пустая точка монтирования на системном диске) или удалённая
                # копия. Молча начинать всё заново нельзя — забили бы системный диск.
                raise ReplicaError(
                    f"Метка копии в {target.describe()} пропала, хотя копия здесь уже была.",
                    hint="Похоже, сетевая папка или диск не подключены (смонтированы), либо копию удалили. "
                         "Подключите место копии. Если оно действительно новое или очищено намеренно — "
                         "нажмите «Подготовить место» в настройках копии.", retryable=True)
            if not target.is_empty():
                raise ReplicaError(
                    f"В месте для копии ({target.describe()}) уже есть файлы, а метки MailArchiver нет.",
                    hint="Укажите пустую папку (префикс) или нажмите «Подготовить место» в настройках "
                         "копии, если это место действительно можно использовать. Так служба не станет "
                         "писать в пустую точку монтирования отключённого диска или в чужую папку.")
            marker_id = mine or uuid.uuid4().hex
            target.write_marker(_new_marker(self.svc, marker_id))
            self.db.set_meta(_META_MARKER, marker_id)
            self.db.set_meta(_META_CONFIRMED, fp)
            self._event("INFO", f"Место для копии подготовлено: {target.describe()}.")
            return need_verify
        if not marker.get("id"):
            raise ReplicaError(f"Метка копии в {target.describe()} повреждена или чужая.",
                               hint="Укажите другое место или удалите файл .mailarchiver-replica, "
                                    "если уверены, что место свободно.")
        if mine and marker["id"] != mine:
            raise ReplicaError(
                f"В {target.describe()} лежит копия ДРУГОГО архива (сервер {marker.get('host') or '?'}).",
                hint="Если это ваша прежняя копия (например, после переноса сервера), нажмите «Подготовить "
                     "место» — она будет принята и сверена. Иначе укажите другое место.")
        if not mine:
            self.db.set_meta(_META_MARKER, marker["id"])
            self.db.replica_clear()
            need_verify = True
        self.db.set_meta(_META_CONFIRMED, fp)
        return need_verify

    def _verify_due(self) -> bool:
        if self.force_verify:
            return True
        try:
            days = int(self.cfg.get("verify_every_days") or 0)
        except (TypeError, ValueError):
            days = 0
        last = self.db.get_meta(_META_VERIFY) or ""
        if not last:
            return self.db.replica_count() == 0 or days > 0
        if days <= 0:
            return False
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now(timezone.utc) - when >= timedelta(days=days)

    # -- главный вход ------------------------------------------------------------------
    def run(self) -> Dict:
        target = build_target(self.svc, self.cfg)
        final = "success"
        try:
            self._event("INFO", f"Копия вне сервера: {target.describe()}.")
            need_verify = self._ensure_marker(target)
            if bool(self.cfg.get("include_db")):
                self._snapshot()
            self._check_cancel()
            if isinstance(target, RsyncTarget):
                self._run_rsync(target)
            else:
                verify = need_verify or self._verify_due()
                self.stats["verified"] = verify
                if verify:
                    self._event("INFO", "Полная сверка с копией: список файлов в копии сравнивается "
                                        "с архивом.")
                self._sync_mailboxes(target, verify)
                self._sync_snapshots(target, verify)
                self._put_readme(target)
                self._apply_deletes(target)
                if verify:
                    self.db.set_meta(_META_VERIFY, datetime.now(timezone.utc).isoformat())
            if self.stats["errors"] or self.stats["deletions_blocked"]:
                final = "partial"
        except JobCancelled:
            final = "cancelled"
            self._save_status(target, final, "Прервано — продолжится при следующем прогоне.")
            raise
        except BaseException as exc:
            final = "failed"
            text = exc.message if isinstance(exc, MailArchiverError) else f"{type(exc).__name__}: {exc}"
            self._save_status(target, final, text)
            raise
        finally:
            target.close()
        message = self._summary(target)
        self._save_status(target, final, message)
        return {"status": final, "message": message, **self.stats, "target": target.describe()}

    def _summary(self, target: ReplicaTarget) -> str:
        s = self.stats
        parts = [f"Копия вне сервера ({target.describe()}): отправлено файлов {s['files_up']} "
                 f"({human_size(s['bytes_up'])})"]
        if s["files_deleted"]:
            parts.append(f"удалено в копии {s['files_deleted']}")
        if s["files_total"]:
            parts.append(f"всего в копии файлов писем {s['files_total']}")
        if s["snapshot"]:
            parts.append(f"снимок базы {s['snapshot']}")
        if s["verified"]:
            parts.append("выполнена полная сверка")
        if s["errors"]:
            parts.append(f"ошибок {s['errors']} (повторим при следующем прогоне)")
        text = ", ".join(parts) + "."
        if s["deletions_blocked"]:
            text += (f" УДАЛЕНИЕ ПРИОСТАНОВЛЕНО: прогон собирался удалить в копии {s['deletions_blocked']} "
                     f"файлов — больше допустимых {self.max_delete_pct}%. Если это ожидаемо (очистка по сроку, "
                     f"удаление ящиков), нажмите «Разрешить удаление» в настройках копии.")
        if s["kept_remote"]:
            text += f" Удалённых локально файлов оставлено в копии: {s['kept_remote']}."
        return text

    def _save_status(self, target: ReplicaTarget, final: str, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        data = {"started_at": self._started.isoformat(), "finished_at": now, "status": final,
                "target": target.describe(), "kind": target.kind, "message": message, **self.stats}
        try:
            self.db.set_meta(_META_STATUS, json.dumps(data, ensure_ascii=False))
            if final in ("success", "partial"):
                self.db.set_meta(_META_OK, now)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить состояние копии")

    # -- снимок базы -------------------------------------------------------------------------
    def _snapshot(self) -> None:
        try:
            info = snapshots.ensure_fresh(self.svc, 3600)
        except MailArchiverError as exc:
            self._error(f"Снимок базы не снят: {exc.message}")
            return
        if info:
            self.stats["snapshot"] = info["name"]
            self._event("INFO", f"Снят снимок базы {info['name']} ({human_size(info['size'])}).")
        else:
            snaps = snapshots.list_snapshots(self.svc.cfg)
            self.stats["snapshot"] = snaps[0]["name"] if snaps else ""

    # -- сетевая папка и S3 -------------------------------------------------------------------
    def _walk_account(self, name: str) -> Dict[str, Tuple[int, int, str]]:
        """Файлы писем ящика: путь в копии → (размер, mtime_ns, путь на диске)."""
        out: Dict[str, Tuple[int, int, str]] = {}
        base = os.path.join(self.mail_root, name)
        for root, _dirs, files in os.walk(base):
            if os.path.basename(root) != "cur":
                continue
            rel_root = os.path.relpath(root, self.mail_root).replace(os.sep, "/")
            for fn in files:
                path = os.path.join(root, fn)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rel = f"mailboxes/{rel_root}/{fn}"
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError:
                    self._error(f"Имя файла не в UTF-8, пропущено: {path!r}")
                    continue
                out[rel] = (st.st_size, st.st_mtime_ns, path)
        return out

    def _local_groups(self) -> List[str]:
        try:
            names = os.listdir(self.mail_root)
        except OSError as exc:
            raise ReplicaError(f"Каталог писем не читается: {exc}") from exc
        return sorted((n for n in names if ACCOUNT_DIR_RE.match(n)
                       and os.path.isdir(os.path.join(self.mail_root, n))),
                      key=lambda n: int(n.split("_")[1]))

    def _sync_mailboxes(self, target: ReplicaTarget, verify: bool) -> None:
        names = self._local_groups()
        known_before = self.db.replica_count()
        self._delete_base = known_before
        local_groups = set()
        for index, name in enumerate(names, 1):
            self._check_cancel()
            grp = f"mailboxes/{name}"
            local_groups.add(grp)
            local = self._walk_account(name)
            if verify:
                remote = dict(target.list(grp + "/"))
                matched = [(rel, grp, v[0], v[1]) for rel, v in local.items() if remote.get(rel) == v[0]]
                self.db.replica_replace_group(grp, matched)
                uploads = [rel for rel, v in local.items() if remote.get(rel) != v[0]]
                deletes = [rel for rel in remote if rel not in local]
                self._verify_seen += len(remote)
            else:
                state = self.db.replica_state_group(grp)
                uploads = [rel for rel, v in local.items() if state.get(rel) != (v[0], v[1])]
                deletes = [rel for rel in state if rel not in local]
            self._upload(target, grp, uploads, local)
            self._pending_deletes.extend((grp, rel) for rel in deletes)
            self._progress(index, len(names),
                           f"Ящиков {index}/{len(names)}: отправлено файлов {self.stats['files_up']} "
                           f"({human_size(self.stats['bytes_up'])})")
        # Ящики, каталогов которых больше нет локально (удалены с диска вручную).
        if verify:
            remote_groups = {f"mailboxes/{d}" for d in target.list_dirs("mailboxes/")}
        else:
            remote_groups = set(g for g in self.db.replica_groups() if g.startswith("mailboxes/"))
        for grp in sorted(remote_groups - local_groups):
            self._check_cancel()
            if verify:
                rels = [rel for rel, _size in target.list(grp + "/")]
            else:
                rels = list(self.db.replica_state_group(grp).keys())
            self._pending_deletes.extend((grp, rel) for rel in rels)
        self.stats["files_total"] = self.db.replica_count_prefix("mailboxes/")

    def _upload(self, target: ReplicaTarget, grp: str, rels: List[str],
                local: Dict[str, Tuple[int, int, str]]) -> None:
        if not rels:
            return
        done_rows: List[Tuple[str, str, int, int]] = []

        def work(rel: str):
            size, mtime, path = local[rel]
            try:
                target.put(rel, path)
                return rel, size, mtime, None
            except FileNotFoundError:
                return rel, size, mtime, "vanished"
            except MailArchiverError as exc:
                return rel, size, mtime, exc.message
            except Exception as exc:  # noqa: BLE001
                return rel, size, mtime, f"{type(exc).__name__}: {exc}"

        def collect(futures) -> None:
            for fut in futures:
                rel, size, mtime, err = fut.result()
                if err is None:
                    done_rows.append((rel, grp, size, mtime))
                    self.stats["files_up"] += 1
                    self.stats["bytes_up"] += size
                elif err != "vanished":         # письмо удалено очисткой во время прогона — не ошибка
                    self._error(f"Не отправлен {rel}: {err}")
            if len(done_rows) >= 500:
                self.db.replica_upsert(done_rows)
                done_rows.clear()

        with ThreadPoolExecutor(max_workers=self.parallel, thread_name_prefix="replica") as pool:
            pending = set()
            try:
                for rel in rels:
                    if self._cancelled():
                        break
                    pending.add(pool.submit(work, rel))
                    if len(pending) >= self.parallel * 4:
                        finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                        collect(finished)
                        self._progress(0, 0, f"Отправлено файлов {self.stats['files_up']} "
                                             f"({human_size(self.stats['bytes_up'])})")
            finally:
                if pending:
                    finished, _ = wait(pending)
                    collect(finished)
                if done_rows:
                    self.db.replica_upsert(done_rows)
        self._check_cancel()

    def _sync_snapshots(self, target: ReplicaTarget, verify: bool) -> None:
        local = {}
        for snap in snapshots.list_snapshots(self.svc.cfg):
            try:
                st = os.stat(snap["path"])
            except OSError:
                continue
            local[f"{DB_GROUP}/{snap['name']}"] = (st.st_size, st.st_mtime_ns, snap["path"])
        if verify:
            remote = dict(target.list(DB_GROUP + "/"))
            self.db.replica_replace_group(DB_GROUP, [(rel, DB_GROUP, v[0], v[1]) for rel, v in local.items()
                                                     if remote.get(rel) == v[0]])
            uploads = [rel for rel, v in local.items() if remote.get(rel) != v[0]]
            stale = [rel for rel in remote if rel not in local]
        else:
            state = self.db.replica_state_group(DB_GROUP)
            uploads = [rel for rel, v in local.items() if state.get(rel) != (v[0], v[1])]
            stale = [rel for rel in state if rel not in local]
        self._upload(target, DB_GROUP, uploads, local)
        if stale and local:
            # Старые снимки в копии чистим всегда (их столько же, сколько на сервере),
            # но только если свежий снимок уже есть — иначе копия осталась бы без базы.
            failed = set(target.delete(stale))
            self.db.replica_delete([r for r in stale if r not in failed])

    def _put_readme(self, target: ReplicaTarget) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as fh:
            fh.write(readme_text(self.svc))
            path = fh.name
        try:
            target.put(README_NAME, path)
        except (MailArchiverError, OSError) as exc:
            self._error(f"Файл с инструкцией не записан: {getattr(exc, 'message', exc)}")
        finally:
            os.unlink(path)

    def _apply_deletes(self, target: ReplicaTarget) -> None:
        pending = self._pending_deletes
        if not pending:
            return
        if not self.mirror:
            self.stats["kept_remote"] = len(pending)
            self.db.replica_delete([rel for _g, rel in pending])
            return
        base = max(self._delete_base, self._verify_seen, len(pending))
        cap = float("inf") if self.max_delete_pct >= 100 else max(MIN_DELETE_CAP,
                                                                   base * self.max_delete_pct / 100.0)
        if len(pending) > cap and not self.allow_mass_delete:
            self.stats["deletions_blocked"] = len(pending)
            self._event("WARNING", f"Удаление в копии приостановлено: {len(pending)} файлов из {base} "
                                   f"(> {self.max_delete_pct}%). Так выглядит отключённый диск с почтой или "
                                   f"ошибка — поэтому копию не трогаем. Если удаление ожидаемо, нажмите "
                                   f"«Разрешить удаление».")
            return
        rels = [rel for _g, rel in pending]
        for start in range(0, len(rels), 1000):
            self._check_cancel()
            batch = rels[start:start + 1000]
            try:
                failed = set(target.delete(batch))
            except MailArchiverError as exc:
                self._error(f"Удаление в копии не удалось: {exc.message}")
                return
            ok = [r for r in batch if r not in failed]
            self.db.replica_delete(ok)
            self.stats["files_deleted"] += len(ok)
            for rel in list(failed)[:5]:
                self._error(f"Не удалён в копии: {rel}")
        self.stats["files_total"] = self.db.replica_count_prefix("mailboxes/")

    # -- rsync ---------------------------------------------------------------------------------
    def _run_rsync(self, target: RsyncTarget) -> None:
        self.stats["verified"] = True
        max_delete = 0
        if self.mirror and not self.allow_mass_delete and self.max_delete_pct < 100:
            total = int(self.db.scalar("SELECT COUNT(*) FROM messages") or 0)
            max_delete = max(MIN_DELETE_CAP, int(total * self.max_delete_pct / 100))
        self._event("INFO", "Синхронизация писем (rsync)…")
        self._progress(0, 3, "Синхронизация писем (rsync)…")
        code, out = target.sync_dir(self.mail_root, "mailboxes", delete=self.mirror, max_delete=max_delete,
                                    excludes=["/account_*_old_*", "*.part"], cancel=self._cancelled,
                                    what="письма")
        if code == -1:
            self._check_cancel()
            raise JobCancelled("Прервано.")
        stats = parse_rsync_stats(out)
        self.stats["files_up"] += stats["files"]
        self.stats["bytes_up"] += stats["bytes"]
        self.stats["files_deleted"] += stats["deleted"]
        if code == 25:
            self.stats["deletions_blocked"] = max(1, stats["deleted"])
            self._event("WARNING", f"rsync остановил удаление после {max_delete} файлов — больше "
                                   f"допустимых {self.max_delete_pct}%. Если это ожидаемо, нажмите "
                                   f"«Разрешить удаление».")
        elif code in (23, 24):
            self._error(target.error_text(code, out))
        elif code != 0:
            raise target._fail(code, out, "письма")
        self._check_cancel()
        self._progress(1, 3, "Снимки базы (rsync)…")
        snap_dir = snapshots.snapshot_dir(self.svc.cfg)
        if bool(self.cfg.get("include_db")) and snapshots.list_snapshots(self.svc.cfg):
            code, out = target.sync_dir(snap_dir, DB_GROUP, delete=True, excludes=[".tmp-*"],
                                        cancel=self._cancelled, what="снимки базы")
            if code == -1:
                raise JobCancelled("Прервано.")
            if code not in (0, 24):
                self._error(target.error_text(code, out))
            else:
                stats = parse_rsync_stats(out)
                self.stats["files_up"] += stats["files"]
                self.stats["bytes_up"] += stats["bytes"]
        self._progress(2, 3, "Инструкция…")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as fh:
            fh.write(readme_text(self.svc))
            path = fh.name
        try:
            target.put(README_NAME, path)
        except MailArchiverError as exc:
            self._error(f"Файл с инструкцией не записан: {exc.message}")
        finally:
            os.unlink(path)
        self.stats["files_total"] = int(self.db.scalar("SELECT COUNT(*) FROM messages") or 0)
        self._progress(3, 3, "Готово")


# ---------------------------------------------------------------------------
#  Восстановление из копии (командная строка)
# ---------------------------------------------------------------------------
def pull(target: ReplicaTarget, dest_dir: str, *, echo=print, include_mail: bool = True) -> Dict:
    """Скачать копию (письма и снимки базы) в каталог ``dest_dir``.

    Раскладка — как в каталоге данных: ``mailboxes/…`` и ``snapshots/…``.
    Для сетевой папки восстанавливаются исходные имена файлов (``%3A`` → ``:``).
    Уже скачанные файлы того же размера пропускаются — команду можно
    перезапускать после обрыва.
    """
    if isinstance(target, RsyncTarget):
        raise ReplicaError(
            "Копию по SSH возвращайте самим rsync.",
            hint=f"Например: rsync -a {target.remote_path('mailboxes')}/ {os.path.join(dest_dir, 'mailboxes')}/ "
                 f"и rsync -a {target.remote_path(DB_GROUP)}/ {os.path.join(dest_dir, 'snapshots')}/")
    files = 0
    size_total = 0
    skipped = 0
    marker = target.read_marker()
    if marker is None:
        raise ReplicaError(f"В {target.describe()} нет копии MailArchiver (не найдена метка .mailarchiver-replica).",
                           hint="Проверьте путь (бакет, префикс) к копии.")
    plan = [(DB_GROUP + "/", snapshots.SNAP_DIRNAME)]
    if include_mail:
        plan.insert(0, ("mailboxes/", "mailboxes"))
    for prefix, local_sub in plan:
        for rel, size in target.list(prefix):
            sub = rel[len(prefix):]
            parts = [p for p in sub.split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            dest = os.path.join(dest_dir, local_sub, *parts)
            if os.path.exists(dest) and os.path.getsize(dest) == size:
                skipped += 1
                continue
            target.get(rel, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
            files += 1
            size_total += size
            if files % 1000 == 0:
                echo(f"  скачано файлов: {files} ({human_size(size_total)})")
    return {"files": files, "bytes": size_total, "skipped": skipped, "target": target.describe(),
            "marker": marker}
