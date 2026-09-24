"""
Куда кладётся копия архива: сетевая папка или диск, другой сервер по SSH
(rsync), S3-совместимое хранилище.

Все пути внутри копии — относительные, с «/»: ``mailboxes/account_12/INBOX/cur/…``,
``db/mailarchiver-….db.gz``. В корне копии лежит метка ``.mailarchiver-replica``:
по ней служба понимает, что пишет именно в свою копию (а не в пустую точку
монтирования отвалившегося сетевого диска или в чужую папку).
"""
from __future__ import annotations

import errno
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..errors import ReplicaError
from ..logging_setup import get_logger
from .s3 import S3Client, S3Config

log = get_logger("replica")

MARKER_NAME = ".mailarchiver-replica"
README_NAME = "README-MailArchiver.txt"

#: Символы, недопустимые в именах файлов Windows/SMB и exFAT/NTFS-дисков.
#: В именах писем Maildir всегда есть «:» («…:2,S»), поэтому в сетевой папке
#: имена кодируются обратимо: «:» → «%3A», «%» → «%25» (см. :func:`escape_component`).
_WIN_BAD = set('<>:"\\|?*%')


def escape_component(name: str) -> str:
    out = []
    for ch in name:
        if ch in _WIN_BAD or ord(ch) < 32:
            out.append("%%%02X" % ord(ch))
        else:
            out.append(ch)
    text = "".join(out)
    # Windows не допускает точку или пробел в конце имени.
    if text.endswith((".", " ")):
        text = text[:-1] + "%%%02X" % ord(text[-1])
    return text


_ESC_RE = re.compile(r"%([0-9A-Fa-f]{2})")


def unescape_component(name: str) -> str:
    return _ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), name)


def _marker_ok(data) -> bool:
    return isinstance(data, dict) and data.get("app") == "MailArchiver" and bool(data.get("id"))


# ---------------------------------------------------------------------------
#  Базовый класс
# ---------------------------------------------------------------------------
class ReplicaTarget:
    kind = ""
    #: можно ли загружать и удалять файлы по одному (для rsync — нет: он синхронизирует каталоги)
    per_file = True

    def describe(self) -> str:
        raise NotImplementedError

    def read_marker(self) -> Optional[dict]:
        raise NotImplementedError

    def write_marker(self, data: dict) -> None:
        raise NotImplementedError

    def is_empty(self) -> bool:
        raise NotImplementedError

    def put(self, rel: str, local_path: str) -> int:
        raise NotImplementedError

    def delete(self, rels: List[str]) -> List[str]:
        raise NotImplementedError

    def list(self, prefix: str) -> Iterator[Tuple[str, int]]:
        raise NotImplementedError

    def list_dirs(self, prefix: str) -> List[str]:
        """Имена подкаталогов первого уровня под префиксом (``prefix`` оканчивается «/»)."""
        raise NotImplementedError

    def get(self, rel: str, dest: str) -> int:
        raise NotImplementedError

    def exists(self, rel: str) -> bool:
        raise NotImplementedError

    def free_bytes(self) -> Optional[int]:
        return None

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
#  Сетевая папка или диск
# ---------------------------------------------------------------------------
class DirTarget(ReplicaTarget):
    kind = "dir"

    def __init__(self, root: str) -> None:
        root = (root or "").strip()
        if not root:
            raise ReplicaError("Не указана папка для копии.",
                               hint="Укажите путь к подключённой сетевой папке или диску, например "
                                    "/mnt/backup/mailarchiver.")
        if not os.path.isabs(root):
            raise ReplicaError(f"Путь к папке для копии должен быть абсолютным: «{root}».",
                               hint="Например /mnt/backup/mailarchiver.")
        self.root = os.path.normpath(root)

    def describe(self) -> str:
        return self.root

    def _abs(self, rel: str) -> str:
        parts = [escape_component(p) for p in rel.split("/") if p not in ("", ".", "..")]
        return os.path.join(self.root, *parts)

    def _require_root(self) -> None:
        if not os.path.isdir(self.root):
            hint = "Проверьте, что сетевая папка или диск подключены (смонтированы) и путь указан верно."
            if self.root == "/home" or self.root.startswith(("/home/", "/root/", "/run/user/")):
                # systemd-служба запущена с ProtectHome=true: домашних каталогов она не видит
                hint = ("Служба MailArchiver не видит домашние каталоги (/home, /root — защита ProtectHome "
                        "в systemd). Подключите папку в другое место, например /mnt/backup, или разрешите "
                        "доступ: sudo systemctl edit mailarchiver → [Service] ProtectHome=read-only и "
                        f"ReadWritePaths={self.root}.")
            raise ReplicaError(f"Папка для копии не найдена: {self.root}.", hint=hint, retryable=True)

    def read_marker(self) -> Optional[dict]:
        self._require_root()
        path = os.path.join(self.root, MARKER_NAME)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ReplicaError(f"Метка копии в {self.root} не читается: {exc}") from exc
        return data if _marker_ok(data) else {"app": "?", "id": "", "raw": True}

    def write_marker(self, data: dict) -> None:
        self._require_root()
        path = os.path.join(self.root, MARKER_NAME)
        tmp = path + ".part"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except PermissionError as exc:
            raise ReplicaError(
                f"Нет прав на запись в папку {self.root}.",
                hint="Служба работает от имени пользователя mailarchiver: дайте ему права на папку "
                     "(sudo chown -R mailarchiver: ПУТЬ), а сетевую папку подключайте с параметрами "
                     "uid=mailarchiver,gid=mailarchiver.") from exc
        except OSError as exc:
            if exc.errno == errno.EROFS:
                raise ReplicaError(f"Папка {self.root} подключена только для чтения.",
                                   hint="Подключите сетевую папку или диск с правом записи.") from exc
            raise ReplicaError(f"Не удалось записать в папку {self.root}: {exc}") from exc

    def is_empty(self) -> bool:
        self._require_root()
        return not [n for n in os.listdir(self.root) if n not in ("lost+found",)]

    def put(self, rel: str, local_path: str) -> int:
        dest = self._abs(rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        st = os.stat(local_path)
        try:
            shutil.copyfile(local_path, tmp)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass                               # сетевые ФС не всегда дают менять время — не страшно
        return st.st_size

    def delete(self, rels: List[str]) -> List[str]:
        failed = []
        for rel in rels:
            try:
                os.unlink(self._abs(rel))
            except FileNotFoundError:
                pass
            except OSError:
                failed.append(rel)
        return failed

    def list(self, prefix: str) -> Iterator[Tuple[str, int]]:
        base = self._abs(prefix.rstrip("/")) if prefix.strip("/") else self.root
        if not os.path.isdir(base):
            return
        for root, _dirs, files in os.walk(base):
            rel_root = os.path.relpath(root, self.root)
            parts = [] if rel_root == "." else [unescape_component(p) for p in rel_root.split(os.sep)]
            for fn in files:
                if fn.endswith(".part") or (not parts and fn in (MARKER_NAME,)):
                    continue
                try:
                    size = os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
                yield "/".join(parts + [unescape_component(fn)]), size

    def list_dirs(self, prefix: str) -> List[str]:
        base = self._abs(prefix.rstrip("/"))
        try:
            return [unescape_component(n) for n in os.listdir(base) if os.path.isdir(os.path.join(base, n))]
        except FileNotFoundError:
            return []

    def get(self, rel: str, dest: str) -> int:
        src = self._abs(rel)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        try:
            st = os.stat(src)
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
        return os.path.getsize(dest)

    def exists(self, rel: str) -> bool:
        return os.path.exists(self._abs(rel))

    def free_bytes(self) -> Optional[int]:
        try:
            st = os.statvfs(self.root)
            return st.f_bavail * st.f_frsize
        except (OSError, AttributeError):
            return None


# ---------------------------------------------------------------------------
#  S3-совместимое хранилище
# ---------------------------------------------------------------------------
class S3Target(ReplicaTarget):
    kind = "s3"

    def __init__(self, cfg: S3Config, prefix: str = "") -> None:
        self.client = S3Client(cfg)
        prefix = (prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        self.prefix = prefix

    def describe(self) -> str:
        return f"{self.client.describe()}/{self.prefix}".rstrip("/")

    def _key(self, rel: str) -> str:
        return self.prefix + rel.lstrip("/")

    def read_marker(self) -> Optional[dict]:
        data = self.client.get_bytes(self._key(MARKER_NAME))
        if data is None:
            return None
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"app": "?", "id": "", "raw": True}
        return parsed if _marker_ok(parsed) else {"app": "?", "id": "", "raw": True}

    def write_marker(self, data: dict) -> None:
        self.client.put_bytes(self._key(MARKER_NAME),
                              json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def is_empty(self) -> bool:
        for _key, _size in self.client.list(self.prefix):
            return False
        return True

    def put(self, rel: str, local_path: str) -> int:
        return self.client.put_file(self._key(rel), local_path)

    def delete(self, rels: List[str]) -> List[str]:
        keys = [self._key(r) for r in rels]
        failed = set(self.client.delete_many(keys))
        return [r for r, k in zip(rels, keys) if k in failed]

    def list(self, prefix: str) -> Iterator[Tuple[str, int]]:
        full = self._key(prefix)
        cut = len(self.prefix)
        for key, size in self.client.list(full):
            rel = key[cut:]
            if rel == MARKER_NAME:
                continue
            yield rel, size

    def list_dirs(self, prefix: str) -> List[str]:
        # Запрос с разделителем «/» отдаёт только каталоги первого уровня —
        # не нужно перебирать миллионы ключей писем, чтобы узнать имена ящиков.
        full = self._key(prefix)
        out = []
        for sub in self.client.list_prefixes(full):
            name = sub[len(full):].strip("/")
            if name:
                out.append(name)
        return out

    def get(self, rel: str, dest: str) -> int:
        return self.client.get_to_file(self._key(rel), dest)

    def exists(self, rel: str) -> bool:
        return self.client.head(self._key(rel)) is not None

    def close(self) -> None:
        self.client.close()


# ---------------------------------------------------------------------------
#  Другой сервер по SSH (rsync)
# ---------------------------------------------------------------------------
_DEST_RE = re.compile(r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):(?P<path>/.*)$")
_STATS_PATTERNS = {
    "files": re.compile(r"Number of regular files transferred:\s*([\d,.\s]+)"),
    "bytes": re.compile(r"Total transferred file size:\s*([\d,.\s]+)"),
    "deleted": re.compile(r"Number of deleted files:\s*([\d,.\s]+)"),
}


def _stat_number(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text.split(" (")[0])
    return int(digits) if digits else 0


def parse_rsync_stats(output: str) -> Dict[str, int]:
    stats = {}
    for name, pattern in _STATS_PATTERNS.items():
        match = pattern.search(output)
        stats[name] = _stat_number(match.group(1)) if match else 0
    return stats


class RsyncTarget(ReplicaTarget):
    kind = "rsync"
    per_file = False

    def __init__(self, dest: str, *, ssh_port: int = 22, ssh_key_file: str = "", known_hosts: str = "",
                 timeout_s: int = 120, bwlimit_kbps: int = 0, local_ok: bool = False) -> None:
        dest = (dest or "").strip()
        if not dest:
            raise ReplicaError("Не указан сервер для копии.",
                               hint="Формат: пользователь@сервер:/путь/к/папке, например "
                                    "backup@nas.local:/srv/mailarchiver")
        match = _DEST_RE.match(dest)
        if match is None and not (local_ok and os.path.isabs(dest)):
            raise ReplicaError(f"Адрес копии по SSH указан неверно: «{dest}».",
                               hint="Формат: пользователь@сервер:/путь/к/папке, например "
                                    "backup@nas.local:/srv/mailarchiver")
        self.remote = match is not None
        self.dest = dest.rstrip("/") or "/"
        self.ssh_port = int(ssh_port or 22)
        self.ssh_key_file = (ssh_key_file or "").strip()
        if self.ssh_key_file and ("'" in self.ssh_key_file or '"' in self.ssh_key_file):
            raise ReplicaError("Путь к SSH-ключу не должен содержать кавычек.")
        self.known_hosts = known_hosts
        self.timeout_s = max(5, int(timeout_s or 120))
        self.bwlimit_kbps = max(0, int(bwlimit_kbps or 0))
        self._tmp: Optional[str] = None

    def describe(self) -> str:
        return self.dest

    # -- запуск rsync -------------------------------------------------------------
    @staticmethod
    def require_tools(remote: bool = True) -> None:
        if shutil.which("rsync") is None:
            raise ReplicaError("На сервере не установлен rsync.",
                               hint="Установите: sudo apt install rsync (или dnf install rsync). "
                                    "rsync нужен и на сервере-получателе.")
        if remote and shutil.which("ssh") is None:
            raise ReplicaError("На сервере не установлен клиент SSH.",
                               hint="Установите: sudo apt install openssh-client.")

    def ssh_command(self) -> str:
        parts = ["ssh", "-p", str(self.ssh_port), "-o", "BatchMode=yes",
                 "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
                 "-o", "StrictHostKeyChecking=accept-new"]
        if self.known_hosts:
            parts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.ssh_key_file:
            parts += ["-i", self.ssh_key_file, "-o", "IdentitiesOnly=yes"]
        return " ".join(shlex.quote(p) for p in parts)

    def base_args(self) -> List[str]:
        args = ["rsync", f"--timeout={self.timeout_s}"]
        if self.remote:
            args += ["-e", self.ssh_command()]
        if self.bwlimit_kbps:
            args.append(f"--bwlimit={self.bwlimit_kbps}")
        return args

    def remote_path(self, rel: str = "") -> str:
        rel = rel.strip("/")
        return self.dest + ("/" + rel if rel else "")

    def run(self, args: List[str], *, cancel: Optional[Callable[[], bool]] = None,
            what: str = "rsync") -> Tuple[int, str]:
        """Запустить rsync, вернуть (код выхода, вывод). Отмена — через ``cancel``."""
        self.require_tools(self.remote)
        cmd = self.base_args() + args
        log.debug("Запуск: %s", " ".join(shlex.quote(a) for a in cmd))
        env = dict(os.environ)
        env["LC_ALL"] = "C"                      # статистика rsync — на английском, её разбираем
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env=env)
            while True:
                try:
                    code = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        out.seek(0)
                        return -1, out.read().decode("utf-8", "replace")
            out.seek(0)
            text = out.read().decode("utf-8", "replace")
        return code, text

    @staticmethod
    def error_text(code: int, output: str) -> str:
        lines = [line for line in output.strip().splitlines()
                 if line.strip() and not line.startswith("Warning: Permanently added")]
        tail = "\n".join(lines[-6:])
        return f"rsync завершился с кодом {code}: {tail}" if tail else f"rsync завершился с кодом {code}"

    #: Известные сбои: (признак в выводе, короткое описание, подсказка).
    _KNOWN_FAILURES = (
        (("remote host identification has changed", "host key verification failed"),
         "ключ сервера-получателя изменился — возможна подмена сервера",
         "Если сервер-получатель переустанавливали, удалите его строку из файла известных узлов службы "
         "(replica_known_hosts в каталоге данных) — при следующем подключении ключ запомнится заново."),
        (("permission denied (publickey", "permission denied, please try again"),
         "сервер-получатель не принял SSH-ключ службы",
         "Добавьте открытый ключ службы в ~/.ssh/authorized_keys пользователя на сервере-получателе "
         "(кнопка «SSH-ключ службы» в настройках копии покажет его)."),
        (("could not resolve hostname", "name or service not known"),
         "имя сервера-получателя не находится в DNS", "Проверьте адрес сервера."),
        (("connection refused", "connection timed out", "no route to host", "network is unreachable"),
         "сервер-получатель недоступен по SSH", "Проверьте адрес, порт SSH и сетевой доступ."),
        (("rsync: command not found", "rsync: not found", "connection unexpectedly closed"),
         "на сервере-получателе не запускается rsync",
         "Установите rsync на сервере-получателе (sudo apt install rsync)."),
        (("no space left",), "на сервере-получателе закончилось место", "Освободите место на сервере-получателе."),
    )

    def _fail(self, code: int, output: str, what: str) -> ReplicaError:
        low = output.lower()
        for needles, short, hint in self._KNOWN_FAILURES:
            if any(n in low for n in needles):
                log.warning("rsync (%s): %s", what, self.error_text(code, output))
                return ReplicaError(f"Копия по SSH ({what}): {short}.", hint=hint,
                                    retryable=code in (10, 12, 30, 35, 255) and "ключ" not in short)
        return ReplicaError(f"Копия по SSH ({what}): {self.error_text(code, output)}",
                            retryable=code in (10, 12, 30, 35, 255))

    # -- метка --------------------------------------------------------------------------
    def _tmpdir(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="mareplica_")
        return self._tmp

    def read_marker(self) -> Optional[dict]:
        local = os.path.join(self._tmpdir(), "marker.json")
        code, out = self.run([self.remote_path(MARKER_NAME), local], what="чтение метки")
        if code == 23 and ("no such file" in out.lower() or "failed: no such" in out.lower()
                           or "change_dir" in out.lower() or "link_stat" in out.lower()):
            return None
        if code != 0:
            raise self._fail(code, out, "чтение метки")
        try:
            with open(local, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"app": "?", "id": "", "raw": True}
        return data if _marker_ok(data) else {"app": "?", "id": "", "raw": True}

    def write_marker(self, data: dict) -> None:
        # Каталог копии может ещё не существовать: rsync пустого каталога создаёт
        # его (один уровень — родитель должен быть), не требуя --mkpath из
        # новых версий rsync.
        empty = os.path.join(self._tmpdir(), "empty")
        os.makedirs(empty, exist_ok=True)
        code, out = self.run(["-d", empty + "/", self.remote_path() + "/"], what="создание папки")
        if code != 0:
            raise self._fail(code, out, "создание папки")
        local = os.path.join(self._tmpdir(), MARKER_NAME)
        with open(local, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        code, out = self.run([local, self.remote_path(MARKER_NAME)], what="запись метки")
        if code != 0:
            raise self._fail(code, out, "запись метки")

    def is_empty(self) -> bool:
        code, out = self.run(["--list-only", self.remote_path() + "/"], what="просмотр папки")
        if code == 23:
            return True                      # папки ещё нет — rsync её создаст
        if code != 0:
            raise self._fail(code, out, "просмотр папки")
        names = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5 and parts[4] not in (".", ".."):
                names.append(parts[4])
        return not names

    def sync_dir(self, src_dir: str, rel: str, *, delete: bool = False, max_delete: int = 0,
                 excludes: Optional[List[str]] = None, cancel=None, what: str = "") -> Tuple[int, str]:
        args = ["-a", "--partial", "--stats", "--human-readable"]
        if delete:
            args += ["--delete", "--delete-after"]
            if max_delete > 0:
                args.append(f"--max-delete={max_delete}")
        for pattern in excludes or []:
            args.append(f"--exclude={pattern}")
        args += [src_dir.rstrip("/") + "/", self.remote_path(rel) + "/"]
        return self.run(args, cancel=cancel, what=what or rel)

    def put(self, rel: str, local_path: str) -> int:
        code, out = self.run(["-t", local_path, self.remote_path(rel)], what=rel)
        if code != 0:
            raise self._fail(code, out, rel)
        return os.path.getsize(local_path)

    def close(self) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
