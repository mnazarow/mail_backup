"""Скрипты установки и обновления в «песочнице» с поддельным systemd.

Настоящие ``scripts/install.sh`` и ``scripts/update.sh`` запускаются целиком,
но против маленькой учебной службы и подменённых ``systemctl``/``journalctl``/
``pip``: так проверяется именно логика скриптов — права на файлы, ожидание
запуска, показ причины отказа и откат, — и не нужны ни сеть, ни systemd.

Поддельный ``systemctl`` запускает службу так же, как настоящий: от имени
пользователя из unit-файла и из его рабочего каталога. Поэтому отказ «служба не
может войти в рабочий каталог» (status=200/CHDIR), из-за которого однажды
сорвалось обновление с архива с правами 0700/0600, воспроизводится честно.

Тесты требуют Linux и root (скрипты меняют владельцев файлов и запускают службу
от другого пользователя); в остальных случаях они пропускаются. Там, где пропуск
недопустим (CI), задайте ``MA_REQUIRE_SCRIPT_TESTS=1``: если условий нет, модуль
не «пройдёт» пропусками, а откажет уже при сборе тестов.
"""
from __future__ import annotations

import base64
import grp
import hashlib
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import threading
import time
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD, NEW = "1.2.0", "9.9.9"


def _service_user():
    """Непривилегированный пользователь, от имени которого пойдёт учебная служба."""
    try:
        pw = pwd.getpwnam("nobody")
        return pw.pw_name, grp.getgrgid(pw.pw_gid).gr_name
    except KeyError:
        return None, None


def _python_for(user: str):
    """Интерпретатор, который доступен пользователю службы (не из домашнего каталога root)."""
    for exe in (sys.executable, "/usr/bin/python3", shutil.which("python3") or ""):
        if not exe or not os.path.exists(exe):
            continue
        probe = subprocess.run(["runuser", "-u", user, "--", exe, "-c", "import venv"],
                               capture_output=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        if probe.returncode == 0:
            return exe
    return None


def _missing_prerequisites(user) -> list:
    """Чего не хватает, чтобы запускать скрипты в песочнице (пусто — всё есть)."""
    missing = []
    if sys.platform != "linux":
        missing.append("Linux")
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        missing.append("права root")
    if not user:
        missing.append("пользователь nobody")
    missing += [tool for tool in ("bash", "runuser") if not shutil.which(tool)]
    return missing


USER, GROUP = _service_user()
_MISSING = _missing_prerequisites(USER)
PYTHON = None if _MISSING else _python_for(USER)
if not _MISSING and not PYTHON:
    _MISSING.append("Python с модулем venv, доступный пользователю %s" % USER)

# В CI пропуск всех тестов выглядел бы как успех. С MA_REQUIRE_SCRIPT_TESTS=1
# отсутствие условий — отказ при сборе тестов, с названием того, чего не хватает.
if _MISSING and os.environ.get("MA_REQUIRE_SCRIPT_TESTS") == "1":
    raise RuntimeError("MA_REQUIRE_SCRIPT_TESTS=1: тесты скриптов установки обязаны выполняться, "
                       "но здесь это невозможно — не хватает: %s." % ", ".join(_MISSING))

needs_root = pytest.mark.skipif(bool(_MISSING),
                                reason="нужны Linux, root, runuser и пользователь nobody (не хватает: %s)"
                                       % ", ".join(_MISSING))


# ---------------------------------------------------------------------------
#  Подделки
# ---------------------------------------------------------------------------
FAKE_SYSTEMCTL = r'''#!@PYTHON@
"""Подделка systemctl: одна служба, запуск как в systemd (User=, WorkingDirectory=, ExecStart=)."""
import grp, os, pwd, shlex, signal, sys, time

STATE = os.environ["FAKE_SYSTEMD_STATE"]
UNIT = os.environ.get("MA_UNIT_FILE", "/etc/systemd/system/mailarchiver.service")
JOURNAL = os.path.join(STATE, "journal")
NAMES = {200: "200/CHDIR", 203: "203/EXEC", 217: "217/USER", 226: "226/NAMESPACE"}


def log(text):
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(time.strftime("%b %d %H:%M:%S") + " host " + text + "\n")


def read(name, default=""):
    try:
        with open(os.path.join(STATE, name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default


def write(name, value):
    with open(os.path.join(STATE, name), "w", encoding="utf-8") as fh:
        fh.write(str(value))


def forget(*names):
    for name in names:
        try:
            os.unlink(os.path.join(STATE, name))
        except OSError:
            pass


def unit():
    """Unit и дополнения из <unit>.d/*.conf — в том же порядке, что у systemd (по имени файла)."""
    conf = {"Environment": []}
    paths = [UNIT]
    try:
        paths += [os.path.join(UNIT + ".d", name) for name in sorted(os.listdir(UNIT + ".d"))
                  if name.endswith(".conf")]
    except OSError:
        pass
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            if path == UNIT:
                raise
            continue                              # дополнение-ссылка в никуда: systemd его пропускает
        for line in lines:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if key == "Environment":
                    conf["Environment"] = conf["Environment"] + [value] if value else []
                else:
                    conf[key] = value
    return conf


def needs_namespace(conf):
    """Настройки, для которых systemd создаёт пространство имён монтирования."""
    return (conf.get("ProtectProc", "default") not in ("", "default")
            or conf.get("PrivateDevices", "no").lower() in ("true", "yes", "on", "1"))


def alive():
    pid = read("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def state():
    if alive():
        return "active", "running"
    code = read("exit")
    if code in ("", "0") or read("stopped") == "1":
        return "inactive", "dead"
    return "activating", "auto-restart"          # Restart=on-failure ждёт RestartSec


def start():
    if alive():
        return
    conf = unit()
    forget("exit", "stopped", "pid")
    write("nrestarts", 0)
    if os.fork():
        for _ in range(200):                      # дождаться, пока служба запустится или откажет
            if read("pid") or read("exit") != "":
                break
            time.sleep(0.01)
        return
    # надзиратель: отвязан от терминала и каналов вызвавшего скрипта
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    argv = shlex.split(conf.get("ExecStart", "/bin/false"))
    child = os.fork()
    if child == 0:
        out = os.open(JOURNAL, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(out, 1)
        os.dup2(out, 2)

        def say(text):
            os.write(2, (time.strftime("%b %d %H:%M:%S") + " host systemd[1]: mailarchiver.service: "
                         + text + "\n").encode())
        # Каталога из ReadWritePaths нет (в том числе ссылка в никуда — диск с
        # данными не подключён): systemd не может собрать окружение службы.
        # Текст — как в журнале настоящего сервера.
        # FAKE_SYSTEMD_STYLE: old — systemd до 246 (RHEL 8, Debian 10) пути не
        # называет; unit-root — путь изнутри собираемого окружения.
        style = os.environ.get("FAKE_SYSTEMD_STYLE", "")
        for path in conf.get("ReadWritePaths", "").split():
            if not path.startswith("-") and not os.path.isdir(path):
                target = os.readlink(path) if os.path.islink(path) else path
                if style == "old":
                    say("Failed to set up mount namespacing: No such file or directory")
                elif style == "unit-root":
                    say("Failed to set up mount namespacing: /run/systemd/unit-root%s: No such file or directory"
                        % target)
                else:
                    say("Failed to set up mount namespacing: /.%s: No such file or directory" % target)
                say("Failed at step NAMESPACE spawning %s: No such file or directory" % argv[0])
                os._exit(226)
        try:
            user = pwd.getpwnam(conf.get("User", "root"))
            gid = grp.getgrnam(conf["Group"]).gr_gid if conf.get("Group") else user.pw_gid
            os.setgroups([])
            os.setgid(gid)
            os.setuid(user.pw_uid)
        except (KeyError, OSError) as exc:
            say("Failed at step USER spawning %s: %s" % (argv[0], exc))
            os._exit(217)
        # контейнер без вложенности: пространство имён монтирования создать нельзя
        if os.environ.get("FAKE_NAMESPACE_FAIL") == "1" and needs_namespace(conf):
            say("Failed to set up mount namespacing: /run/systemd/unit-root/proc: Permission denied")
            say("Failed at step NAMESPACE spawning %s: Permission denied" % argv[0])
            os._exit(226)
        try:
            os.chdir(conf.get("WorkingDirectory", "/"))
        except OSError as exc:
            say("Changing to the requested working directory failed: %s" % exc.strerror)
            say("Failed at step CHDIR spawning %s: %s" % (argv[0], exc.strerror))
            os._exit(200)
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
        for item in conf["Environment"]:
            key, value = item.split("=", 1)
            env[key] = value
        try:
            os.execve(argv[0], argv, env)
        except OSError as exc:
            say("Failed at step EXEC spawning %s: %s" % (argv[0], exc.strerror))
            os._exit(203)
    write("pid", child)
    _pid, status = os.waitpid(child, 0)
    code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)
    if read("stopped") != "1":
        log("systemd[1]: mailarchiver.service: Main process exited, code=exited, status=%s"
            % NAMES.get(code, "%d/n/a" % code))
        if code:
            log("systemd[1]: mailarchiver.service: Failed with result 'exit-code'.")
            write("nrestarts", int(read("nrestarts", "0") or 0) + 1)
            log("systemd[1]: mailarchiver.service: Scheduled restart job, restart counter is at %s."
                % read("nrestarts"))
    write("exit", code)
    os._exit(0)


def stop():
    write("stopped", "1")
    pid = read("pid")
    if pid and alive():
        os.kill(int(pid), signal.SIGTERM)
        for _ in range(300):
            if not alive():
                break
            time.sleep(0.01)
        else:
            os.kill(int(pid), signal.SIGKILL)
            time.sleep(0.05)


def main():
    args = sys.argv[1:]
    with open(os.path.join(STATE, "calls"), "a", encoding="utf-8") as fh:
        fh.write(" ".join(args) + "\n")
    if not args:
        return 0
    cmd = args[0]
    if cmd in ("daemon-reload", "enable", "disable", "reset-failed"):
        return 0
    if cmd == "start":
        start()
        return 0
    if cmd == "stop":
        stop()
        return 0
    if cmd == "restart":
        stop()
        start()
        return 0
    if cmd == "is-active":
        active, _sub = state()
        if "--quiet" not in args and "-q" not in args:
            print(active)
        return 0 if active == "active" else 3
    if cmd == "show":
        active, sub = state()
        data = {"ActiveState": active, "SubState": sub, "NRestarts": read("nrestarts", "0") or "0",
                "ExecMainStatus": read("exit", "0") or "0", "MainPID": read("pid", "0") if alive() else "0"}
        wanted, value_only, i = [], False, 1
        while i < len(args):
            if args[i] in ("-p", "--property") and i + 1 < len(args):
                wanted += args[i + 1].split(",")
                i += 1
            elif args[i].startswith("--property="):
                wanted += args[i].split("=", 1)[1].split(",")
            elif args[i] == "--value":
                value_only = True
            i += 1
        for key in wanted or sorted(data):
            if key in data:
                print(data[key] if value_only else "%s=%s" % (key, data[key]))
        return 0
    if cmd == "status":
        active, sub = state()
        print("* mailarchiver.service - MailArchiver")
        print("     Active: %s (%s)" % (active, sub))
        return 0 if active == "active" else 3
    return 0


sys.exit(main())
'''

FAKE_JOURNALCTL = r'''#!@PYTHON@
"""Подделка journalctl: последние строки журнала поддельной службы."""
import os, sys
count = 50
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "-n" and i + 1 < len(args):
        count = int(args[i + 1])
    elif arg.startswith("-n") and arg[2:].isdigit():
        count = int(arg[2:])
try:
    with open(os.path.join(os.environ["FAKE_SYSTEMD_STATE"], "journal"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
except OSError:
    lines = []
print("\n".join(lines[-count:]))
'''

# Подделка pip: «устанавливает» пакет копированием, как настоящий pip, — файлы
# создаются заново с правами по umask, права исходников не переносятся.
# Без сети («--no-index --find-links КАТАЛОГ … mailarchiver==X») пакет берётся
# из файла .whl (это zip-архив); собрать его из исходников без индекса нельзя,
# как и настоящему pip: для сборки тот скачивает setuptools.
# «--dry-run» ничего не устанавливает; FAKE_PIP_NO_DRY_RUN — pip старше 22.2,
# который этого параметра не знает; FAKE_PIP_DRY_RUN_FAIL — пакеты не подошли.
# «config list» показывает индекс из FAKE_PIP_INDEX_URL (как pip.conf).
FAKE_PIP = r'''#!@PYTHON@
import os, shutil, sys, zipfile
SITE = "@SITE@"
args = sys.argv[1:]
with open("@CALLS@", "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")
if [a for a in args if not a.startswith("-")][:2] == ["config", "list"]:
    if os.environ.get("FAKE_PIP_INDEX_URL"):
        print("global.index-url='%s'" % os.environ["FAKE_PIP_INDEX_URL"])
    sys.exit(0)
if args[:1] != ["install"]:
    sys.exit(0)


def fail(text):
    sys.stderr.write("ERROR: %s\n" % text)
    sys.exit(1)


dry_run = "--dry-run" in args
if dry_run and os.environ.get("FAKE_PIP_NO_DRY_RUN"):
    sys.stderr.write("\nUsage:\n  pip install [options] <requirement specifier> ...\n\nno such option: --dry-run\n")
    sys.exit(2)
if dry_run and os.environ.get("FAKE_PIP_DRY_RUN_FAIL"):
    fail("Could not find a version that satisfies the requirement cryptography>=44.0 (from versions: none)\n"
         "ERROR: No matching distribution found for cryptography>=44.0")

no_index, links, targets, i = False, [], [], 1
while i < len(args):
    arg = args[i]
    if arg in ("--find-links", "-f", "-r", "--requirement"):
        i += 1
        if arg in ("--find-links", "-f"):
            links.append(args[i])
    elif arg == "--no-index":
        no_index = True
    elif not arg.startswith("-"):
        targets.append(arg)
    i += 1
source = wheel = None
for target in targets:
    if os.path.isdir(target):
        if no_index:
            fail("Could not find a version that satisfies the requirement setuptools>=68 (from versions: none)")
        source = target
    elif target.startswith("mailarchiver=="):
        version = target.split("==", 1)[1]
        found = [os.path.join(link, name) for link in links if no_index and os.path.isdir(link)
                 for name in sorted(os.listdir(link))
                 if name.startswith("mailarchiver-%s-" % version) and name.endswith(".whl")]
        if not found:
            fail("Could not find a version that satisfies the requirement %s (from versions: none)\n"
                 "ERROR: No matching distribution found for %s" % (target, target))
        wheel = found[0]
if dry_run or (not source and not wheel):
    sys.exit(0)
if os.environ.get("FAKE_PIP_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_PIP_SLEEP"]))
if os.environ.get("FAKE_PIP_RUN"):
    import subprocess
    subprocess.run(["sh", "-c", os.environ.pop("FAKE_PIP_RUN")], check=False)
dst = os.path.join(SITE, "mailarchiver")
shutil.rmtree(dst, ignore_errors=True)
if wheel:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("mailarchiver/") and name.endswith(".py"):
                path = os.path.join(SITE, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(archive.read(name))
else:
    src = os.path.join(source, "mailarchiver")
    for base, _dirs, files in os.walk(src):
        rel = os.path.relpath(base, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            if name.endswith(".py"):
                shutil.copyfile(os.path.join(base, name), os.path.join(target, name))
entry = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "mailarchiver")
with open(entry, "w", encoding="utf-8") as fh:
    fh.write("#!%s\nimport sys\nfrom mailarchiver.__main__ import main\nsys.exit(main())\n"
             % os.path.join(os.path.dirname(entry), "python"))
os.chmod(entry, 0o755)
'''

# Подделка python3 для install.sh: «python3 -m venv DIR» создаёт окружение без
# сети и кладёт в него поддельный pip; всё остальное выполняет настоящий Python.
FAKE_PYTHON3 = r'''#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  # FAKE_REAL_VENV=1 — настоящее окружение с настоящим pip (тест установки без сети)
  if [ -n "$FAKE_REAL_VENV" ]; then exec "@PYTHON@" -m venv "$3"; fi
  exec "@PYTHON@" "@MAKE_VENV@" "$3"
fi
exec "@PYTHON@" "$@"
'''

MAKE_VENV = r'''
import os, subprocess, sys
venv = sys.argv[1]
subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv], check=True)
py = os.path.join(venv, "bin", "python")
site = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                      check=True, capture_output=True, text=True).stdout.strip()
for name in ("fastapi", "uvicorn", "jinja2", "yaml", "apscheduler", "imapclient", "cryptography"):
    open(os.path.join(site, name + ".py"), "w").close()
pip = os.path.join(venv, "bin", "pip")
with open(pip, "w", encoding="utf-8") as fh:
    fh.write(open("@FAKE_PIP@", encoding="utf-8").read().replace("@SITE@", site))
os.chmod(pip, 0o755)
'''

# Учебная служба: тот же набор команд и то же поведение при запуске, что нужны скриптам.
MINI = {
    "__init__.py": "",
    "version.py": '__version__ = "@VERSION@"\nAPP_TITLE = "MailArchiver"\n',
    "config.py": r'''
import os, re


class Config:
    def __init__(self, path):
        self.source_path = path
        with open(path, encoding="utf-8") as fh:
            text = fh.read()

        def val(key, default):
            m = re.search(r'^\s+%s:\s*"?([^"\n#]*?)"?\s*(?:#.*)?$' % key, text, re.M)
            return m.group(1).strip() if m and m.group(1).strip() else default
        self.server = {"host": val("host", "127.0.0.1"), "port": int(val("port", "8493"))}
        self.data_dir = os.path.abspath(val("data_dir", "/var/lib/mailarchiver"))    # как у настоящей программы
        self.db_path = os.path.join(self.data_dir, "mailarchiver.db")
        self.warnings = []


def load_config(path=None, create_dirs=True):
    return Config(path or os.environ["MAILARCHIVER_CONFIG"])
''',
    "__main__.py": r'''
import argparse, os, sys, time
from .config import load_config
from .version import __version__


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config")
    parser.add_argument("command")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config or os.environ.get("MAILARCHIVER_CONFIG"))
    except Exception as exc:
        print("ОШИБКА конфигурации: %s" % exc, file=sys.stderr)
        return 1
    if args.command == "check-config":
        # Как настоящая программа: ссылка в никуда или файл на месте каталога данных —
        # ошибка; каталога просто нет — только замечание (код 0).
        if os.path.lexists(cfg.data_dir) and not os.path.isdir(cfg.data_dir):
            print("ОШИБКА: Каталог данных %s — ссылка в никуда или не каталог." % cfg.data_dir, file=sys.stderr)
            return 1
        print("MailArchiver", __version__)
        print("Веб-интерфейс      : http://%s:%s" % (cfg.server["host"], cfg.server["port"]))
        print("Конфигурация корректна ✓")
        return 0
    if args.command != "serve":
        return 2
    try:
        with open(os.path.join(cfg.data_dir, "behaviour"), encoding="utf-8") as fh:
            kind, _, rest = fh.read().strip().partition(":")
    except OSError:
        kind, rest = "", ""
    version, _, param = rest.partition(":")
    if version not in (__version__, "*"):
        kind = ""
    with open(os.path.join(cfg.data_dir, "cwd"), "w", encoding="utf-8") as fh:
        fh.write(os.getcwd())
    # «миграция»: каждая версия оставляет в базе свою отметку
    with open(cfg.db_path, "a", encoding="utf-8") as fh:
        fh.write("opened-by-%s\n" % __version__)
    if kind == "crash":
        # как SQLite, убитый посреди записи: остались журналы WAL и отката
        for suffix in ("-wal", "-journal"):
            with open(cfg.db_path + suffix, "w", encoding="utf-8") as fh:
                fh.write("%s-of-%s\n" % (suffix, __version__))
        print("Traceback (most recent call last):", file=sys.stderr)
        print("sqlite3.OperationalError: no such column: accounts.color", file=sys.stderr)
        print("ERROR:    Application startup failed. Exiting.", file=sys.stderr)
        return 3
    if kind == "slow":
        time.sleep(float(param or 5))
    if kind == "busy":                       # как построение индексов большой базы: процессор занят
        end = time.time() + float(param or 5)
        while time.time() < end:
            pass
    if kind == "hang":
        time.sleep(3600)
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                with open(os.path.join(cfg.data_dir, "health_hits"), "a", encoding="utf-8") as fh:
                    fh.write(__version__ + "\n")
            body = b'{"status":"ok"}' if self.path == "/health" else b"{}"
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    HTTPServer((cfg.server["host"], cfg.server["port"]), Handler).serve_forever()
    return 0
''',
    "web/__init__.py": "",
    "web/app.py": "app = object()\n",
}

CONFIG = '''server:
  host: "@HOST@"
  port: @PORT@
paths:
  data_dir: "@DATA@"
'''

PYPROJECT = '''[project]
name = "mailarchiver"
version = "@VERSION@"
requires-python = "@REQUIRES@"
'''

# Подделка df: свободное место задаёт тест (FAKE_DF_FREE_KB), иначе — настоящий df.
FAKE_DF = '''#!/bin/sh
if [ -n "$FAKE_DF_FREE_KB" ]; then
  echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
  echo "fake 99999999 0 $FAKE_DF_FREE_KB 0% /"
  exit 0
fi
exec @DF@ "$@"
'''

# Подделка lsblk: настоящие диски машины тесты видеть не должны. «Диски» задаёт тест
# файлом $FAKE_SYSTEMD_STATE/lsblk (строки lsblk -P) и lsblk_children (строки
# «УСТРОЙСТВО ДОЧЕРНЕЕ» — что открыто или собрано поверх устройства).
FAKE_LSBLK = '''#!/bin/sh
state="$FAKE_SYSTEMD_STATE"
case " $* " in
  *" -P "*) cat "$state/lsblk" 2>/dev/null; exit 0 ;;
esac
for dev in "$@"; do last="$dev"; done
echo "$last"
if [ -f "$state/lsblk_children" ]; then
  awk -v d="$last" '$1 == d { print $2 }' "$state/lsblk_children"
fi
exit 0
'''

# Подделка systemd-detect-virt: тип контейнера задаёт тест (FAKE_VIRT), иначе «не контейнер».
FAKE_DETECT_VIRT = '''#!/bin/sh
if [ -n "$FAKE_VIRT" ]; then echo "$FAKE_VIRT"; exit 0; fi
echo none
exit 1
'''


def build_wheel(directory: str, name: str, version: str, files: dict, *, requires: tuple = (),
                script: str = "") -> str:
    """Настоящий пакет .whl (его принимает и настоящий pip): файлы, METADATA, WHEEL, RECORD."""
    os.makedirs(directory, exist_ok=True)
    info = "%s-%s.dist-info/" % (name, version)
    files = dict(files)
    files[info + "METADATA"] = "Metadata-Version: 2.1\nName: %s\nVersion: %s\n%s" % (
        name, version, "".join("Requires-Dist: %s\n" % item for item in requires))
    files[info + "WHEEL"] = "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if script:
        files[info + "entry_points.txt"] = "[console_scripts]\n%s\n" % script
    record = []
    for rel, text in files.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append("%s,sha256=%s,%d" % (rel, digest, len(data)))
    files[info + "RECORD"] = "\n".join(record + [info + "RECORD,,"]) + "\n"
    path = os.path.join(directory, "%s-%s-py3-none-any.whl" % (name, version))
    with zipfile.ZipFile(path, "w") as archive:
        for rel, text in files.items():
            archive.writestr(rel, text)
    os.chmod(path, 0o644)
    return path


def make_wheel(directory: str, version: str, *, requires: tuple = ()) -> str:
    """Пакет учебной службы; с отметкой «установлено из пакета», которой нет в исходниках."""
    files = {"mailarchiver/" + rel: text.replace("@VERSION@", version) for rel, text in MINI.items()}
    files["mailarchiver/from_wheel.py"] = "ORIGIN = 'wheel'\n"
    return build_wheel(directory, "mailarchiver", version, files, requires=requires,
                       script="mailarchiver = mailarchiver.__main__:main")


def _write(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_INDEX = {}


def _index_url() -> str:
    """Адрес «индекса пакетов» для проверки связи: маленький сервер в этом же процессе.

    Скрипт обновления перед установкой из сети опрашивает индекс (MA_INDEX_URL);
    настоящий PyPI в тестах трогать нельзя.
    """
    if not _INDEX:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Index(BaseHTTPRequestHandler):
            def do_HEAD(self):
                _INDEX["hits"] = _INDEX.get("hits", 0) + 1
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_HEAD

            def log_message(self, *a):
                pass
        server = HTTPServer(("127.0.0.1", 0), Index)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _INDEX["url"] = "http://127.0.0.1:%d/simple/" % server.server_address[1]
    return _INDEX["url"]


def _restrict(tree: str) -> None:
    """Права как после распаковки архива, собранного с umask 077: 0700 каталоги, 0600 файлы."""
    for base, dirs, files in os.walk(tree):
        os.chmod(base, 0o700)
        for name in files:
            path = os.path.join(base, name)
            os.chmod(path, 0o700 if os.access(path, os.X_OK) else 0o600)


class Sandbox:
    """Каталоги «сервера», подделки в PATH и исходники новой версии."""

    def __init__(self, base: str, host: str = "127.0.0.1") -> None:
        self.base = base
        self.host = host
        self.port = _free_port()
        self.install_dir = os.path.join(base, "opt")
        self.app_dir = os.path.join(self.install_dir, "app")
        self.venv = os.path.join(self.install_dir, "venv")
        self.config_dir = os.path.join(base, "etc")
        self.data = os.path.join(base, "data")
        self.state = os.path.join(base, "state")
        self.bin = os.path.join(base, "bin")
        self.unit = os.path.join(base, "unit", "mailarchiver.service")
        self.dropin = os.path.join(self.unit + ".d", "50-relax-sandbox.conf")
        self.wrapper = os.path.join(base, "usrbin", "mailarchiver")
        self.db = os.path.join(self.data, "mailarchiver.db")
        self.stashes: list = []          # «отключённые диски» вне песочницы (unplug)
        for path in (self.config_dir, self.state, self.bin, os.path.dirname(self.unit),
                     os.path.dirname(self.wrapper)):
            os.makedirs(path)
        os.chmod(base, 0o755)
        fake_pip = os.path.join(self.state, "fake_pip.py")
        _write(fake_pip, FAKE_PIP.replace("@PYTHON@", PYTHON)
               .replace("@CALLS@", os.path.join(self.state, "pip_calls")))
        make_venv = os.path.join(self.state, "make_venv.py")
        _write(make_venv, MAKE_VENV.replace("@FAKE_PIP@", fake_pip))
        self.make_venv = make_venv
        _write(os.path.join(self.bin, "systemctl"), FAKE_SYSTEMCTL.replace("@PYTHON@", PYTHON), 0o755)
        _write(os.path.join(self.bin, "journalctl"), FAKE_JOURNALCTL.replace("@PYTHON@", PYTHON), 0o755)
        _write(os.path.join(self.bin, "python3"),
               FAKE_PYTHON3.replace("@PYTHON@", PYTHON).replace("@MAKE_VENV@", make_venv), 0o755)
        # менеджеры пакетов ничего не ставят: тест не должен менять машину
        # (userdel — на случай uninstall.sh: пользователь службы здесь — системный nobody)
        for name in ("apt-get", "dnf", "yum", "zypper", "pacman", "userdel"):
            _write(os.path.join(self.bin, name),
                   '#!/bin/sh\necho "%s $*" >> "$FAKE_SYSTEMD_STATE/pkg_calls"\nexit ${FAKE_PKG_EXIT:-0}\n' % name,
                   0o755)
        _write(os.path.join(self.bin, "df"), FAKE_DF.replace("@DF@", shutil.which("df") or "/bin/df"), 0o755)
        _write(os.path.join(self.bin, "systemd-detect-virt"), FAKE_DETECT_VIRT, 0o755)
        _write(os.path.join(self.bin, "lsblk"), FAKE_LSBLK, 0o755)
        # свой «/etc/fstab»: настоящий тесты менять не могут, а от его содержимого зависеть не должны
        self.fstab_file = os.path.join(self.state, "fstab")
        self.fstab()
        _write(os.path.join(self.config_dir, "config.yaml"),
               CONFIG.replace("@HOST@", host).replace("@PORT@", str(self.port)).replace("@DATA@", self.data))

    # -- окружение и запуск ------------------------------------------------
    def env(self, **extra) -> dict:
        env = {
            "PATH": self.bin + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/root", "LANG": "C.UTF-8",
            "FAKE_SYSTEMD_STATE": self.state,
            "MA_INSTALL_DIR": self.install_dir, "MA_CONFIG_DIR": self.config_dir,
            "MA_DATA_DIR": self.data, "MA_USER": USER, "MA_GROUP": GROUP,
            "MA_UNIT_FILE": self.unit, "MA_CLI_WRAPPER": self.wrapper,
            "MA_HOST": self.host, "MA_PORT": str(self.port),
            "MA_FSTAB": self.fstab_file, "MA_INDEX_URL": _index_url(),
            # где ещё искать перенесённую базу: не в настоящих /srv, /mnt … машины
            "MA_DATA_SEARCH": os.path.join(self.base, ".no-search"),
        }
        env.update(extra)
        return {key: value for key, value in env.items() if value is not None}

    def fstab(self, *mountpoints: str) -> None:
        """Содержимое «/etc/fstab»: корень (он подключён) и перечисленные точки монтирования."""
        lines = ["# тестовый fstab", "UUID=0000-0000 / ext4 defaults 0 1", "UUID=0000-0001 none swap sw 0 0"]
        lines += ["UUID=1111-%04d %s ext4 defaults,nofail 0 2" % (i, path.replace(" ", "\\040"))
                  for i, path in enumerate(mountpoints)]
        _write(self.fstab_file, "\n".join(lines) + "\n")

    def start(self, script: str, src: str, **extra: str):
        """Запустить скрипт в фоне, в своей группе процессов (для сигналов)."""
        return subprocess.Popen(["bash", os.path.join(src, "scripts", script)], env=self.env(**extra), cwd=src,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)

    def unplug(self, path: str) -> str:
        """«Отключить диск»: убрать каталог из песочницы туда, где его не найдёт и поиск перенесённых данных.

        Рядом с песочницей, прямо в /tmp: /tmp целиком скрипты не просматривают.
        """
        stash = tempfile.mkdtemp(prefix="ma-unplugged-")
        self.stashes.append(stash)
        target = os.path.join(stash, "disk")
        os.rename(path, target)
        return target

    def plug(self, stash: str, path: str) -> None:
        """Вернуть «отключённый» каталог на место."""
        os.rename(stash, path)

    def health_hits(self, version: str) -> int:
        try:
            with open(os.path.join(self.data, "health_hits"), encoding="utf-8") as fh:
                return fh.read().split().count(version)
        except OSError:
            return 0

    def run(self, script: str, src: str, timeout: int = 120, args: tuple = (), **extra: str):
        # как в документации: из каталога с исходниками новой версии
        return subprocess.run(["bash", os.path.join(src, "scripts", script), *args], env=self.env(**extra),
                              cwd=src, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def systemctl(self, *args: str):
        return subprocess.run([os.path.join(self.bin, "systemctl"), *args], env=self.env(),
                              capture_output=True, text=True, timeout=30)

    def stop(self) -> None:
        try:
            self.systemctl("stop", "mailarchiver")
        except Exception:  # noqa: BLE001
            pass

    # -- исходники ---------------------------------------------------------
    def sources(self, version: str, name: str, *, broken_import: bool = False, rejects_config: bool = False,
                syntax_error: bool = False, requires_python: str = ">=3.8,<4", wheels: bool = False,
                requirements: str = "# пусто\n", needs_library: bool = False) -> str:
        """Каталог с исходниками: настоящие скрипты и unit, учебный пакет.

        ``rejects_config`` — эта версия не принимает действующую конфигурацию;
        ``syntax_error`` — в ней есть модуль, который Python окружения не разберёт;
        ``wheels`` — рядом лежит каталог wheels с пакетом приложения (автономный архив);
        ``needs_library`` — чтение конфигурации требует библиотеки, которой в окружении ещё нет.
        """
        src = os.path.join(self.base, name, "mail_backup")
        for rel in ("scripts", "systemd"):
            shutil.copytree(os.path.join(ROOT, rel), os.path.join(src, rel),
                            ignore=shutil.ignore_patterns("__pycache__"))
        for rel, text in MINI.items():
            _write(os.path.join(src, "mailarchiver", rel), text.replace("@VERSION@", version))
        if broken_import:
            _write(os.path.join(src, "mailarchiver", "web", "app.py"), "import no_such_module_in_tests\n")
        if rejects_config:
            with open(os.path.join(src, "mailarchiver", "config.py"), "a", encoding="utf-8") as fh:
                fh.write("\n\ndef load_config(path=None, create_dirs=True):\n"
                         "    raise ValueError('параметр server.legacy_mode больше не поддерживается')\n")
        if needs_library:
            with open(os.path.join(src, "mailarchiver", "config.py"), "a", encoding="utf-8") as fh:
                fh.write("\n\ndef load_config(path=None, create_dirs=True):\n"
                         "    import library_added_in_new_version\n")
        if syntax_error:
            _write(os.path.join(src, "mailarchiver", "web", "future.py"), "def broken(:\n    pass\n")
        if wheels:
            make_wheel(os.path.join(src, "wheels"), version)
        _write(os.path.join(src, "VERSION"), version + "\n")
        _write(os.path.join(src, "pyproject.toml"),
               PYPROJECT.replace("@VERSION@", version).replace("@REQUIRES@", requires_python))
        _write(os.path.join(src, "requirements.txt"), requirements)
        _write(os.path.join(src, "config", "config.example.yaml"),
               CONFIG.replace("@HOST@", "127.0.0.1").replace("@PORT@", "8493")
               .replace("@DATA@", "/var/lib/mailarchiver"))
        return src

    def install_old(self, *, old_unit: bool = False) -> None:
        """Работающая установка прежней версии — исходное состояние для update.sh.

        ``old_unit`` — unit прежней версии ещё без ProtectProc и PrivateDevices
        (такой запускается и там, где пространство имён создать нельзя).
        """
        src = self.sources(OLD, "src_old")
        shutil.copytree(src, self.app_dir)
        subprocess.run([PYTHON, self.make_venv, self.venv], check=True, capture_output=True, cwd="/",
                       env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        subprocess.run([os.path.join(self.venv, "bin", "pip"), "install", "--no-deps", self.app_dir],
                       check=True, capture_output=True, cwd="/", env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        os.makedirs(self.data)
        _write(self.db, "created-by-%s\n" % OLD)
        subprocess.run(["chown", "-R", "%s:%s" % (USER, GROUP), self.data], check=True)
        os.chmod(self.data, 0o750)
        with open(os.path.join(src, "systemd", "mailarchiver.service"), encoding="utf-8") as fh:
            unit = fh.read()
        for key, value in (("@VENV@", self.venv), ("@APPDIR@", self.app_dir), ("@USER@", USER),
                           ("@GROUP@", GROUP), ("@DATADIR@", self.data),
                           ("@CONFIG@", os.path.join(self.config_dir, "config.yaml"))):
            unit = unit.replace(key, value)
        if old_unit:
            unit = "".join(line for line in unit.splitlines(True)
                           if not line.startswith(("ProtectProc=", "PrivateDevices=")))
        _write(self.unit, unit)
        self.systemctl("start", "mailarchiver")
        assert self.wait_version() == OLD, self.journal()

    # -- наблюдение --------------------------------------------------------
    def behaviour(self, text: str) -> None:
        _write(os.path.join(self.data, "behaviour"), text)

    def journal(self) -> str:
        try:
            with open(os.path.join(self.state, "journal"), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def active(self) -> bool:
        return self.systemctl("is-active", "--quiet", "mailarchiver").returncode == 0

    def substate(self) -> str:
        return self.systemctl("show", "mailarchiver", "-p", "SubState", "--value").stdout.strip()

    def wait_crashed(self, timeout: float = 15.0) -> bool:
        """Дождаться, пока служба упадёт (systemd ждёт перезапуска).

        Сразу после start/restart спрашивать нельзя: подделка, как и systemd,
        возвращается, едва процесс запущен, — упасть он может мгновением позже.
        """
        end = time.time() + timeout
        while time.time() < end and self.substate() != "auto-restart":
            time.sleep(0.05)
        return self.substate() == "auto-restart"

    def healthy(self) -> bool:
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open("http://%s:%d/health" % (self.host, self.port), timeout=2) as resp:
                return resp.status == 200
        except OSError:
            return False

    def wait_version(self, timeout: float = 15.0) -> str:
        """Версия пакета в окружении — когда служба поднялась и отвечает."""
        import time
        end = time.time() + timeout
        while time.time() < end and not (self.active() and self.healthy()):
            time.sleep(0.1)
        if not (self.active() and self.healthy()):
            return ""
        # -I и чистое окружение: иначе при запуске из каталога проекта импортируется он сам
        return subprocess.run([os.path.join(self.venv, "bin", "python"), "-I", "-c",
                               "from mailarchiver.version import __version__; print(__version__)"],
                              capture_output=True, text=True, cwd="/",
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}).stdout.strip()

    def db_text(self) -> str:
        with open(self.db, encoding="utf-8") as fh:
            return fh.read()

    def forget_calls(self) -> None:
        """Дальше в журналах вызовов (systemctl, pip, менеджеры пакетов) — только то, что сделает скрипт."""
        for name in ("calls", "pip_calls", "pkg_calls"):
            open(os.path.join(self.state, name), "w").close()

    def calls(self, name: str = "calls") -> list:
        """Вызовы поддельного systemctl (pip — name="pip_calls", apt и прочие — "pkg_calls") по строкам."""
        try:
            with open(os.path.join(self.state, name), encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    def changed_state(self) -> list:
        """Вызовы systemctl, которые меняют состояние службы."""
        return [c for c in self.calls() if c.split()[:1] and c.split()[0] in
                ("stop", "start", "restart", "daemon-reload", "enable", "disable", "reset-failed")]

    def site_file(self, rel: str) -> str:
        """Путь файла установленного пакета в окружении."""
        site = subprocess.run([os.path.join(self.venv, "bin", "python"), "-I", "-B", "-c",
                               "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, cwd="/",
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}).stdout.strip()
        return os.path.join(site, "mailarchiver", rel)

    def snapshot(self, *extra: str) -> dict:
        """Всё, что есть в каталогах «сервера» (и в extra): права, владелец, размер, время изменения."""
        found = {}
        for top in (self.install_dir, self.config_dir, self.data, os.path.dirname(self.unit),
                    os.path.dirname(self.wrapper), *extra):
            for base, dirs, files in os.walk(top):
                for name in dirs + files:
                    path = os.path.join(base, name)
                    if path == os.path.join(self.data, "health_hits"):
                        continue                      # эту отметку ставит сама служба на каждый опрос /health
                    st = os.lstat(path)
                    found[path] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid,
                                   st.st_size if stat.S_ISREG(st.st_mode) else 0,
                                   st.st_mtime_ns if stat.S_ISREG(st.st_mode) else 0)
        return found

    def as_user(self, *cmd: str) -> int:
        return subprocess.run(["runuser", "-u", USER, "--", *cmd], capture_output=True,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}).returncode


@pytest.fixture
def sandbox():
    # не tmp_path: каталог pytest закрыт для посторонних (0700), а служба идёт от nobody
    base = tempfile.mkdtemp(prefix="ma-scripts-")
    box = Sandbox(base)
    try:
        yield box
    finally:
        box.stop()
        shutil.rmtree(base, ignore_errors=True)
        for stash in box.stashes:
            shutil.rmtree(stash, ignore_errors=True)


def _report(proc) -> str:
    return "код %s\n--- stdout ---\n%s\n--- stderr ---\n%s" % (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
#  update.sh
# ---------------------------------------------------------------------------
@needs_root
def test_update_from_archive_with_restricted_permissions(sandbox):
    """Исходники с правами 0700/0600 (архив, собранный с umask 077) не ломают обновление."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    _restrict(os.path.dirname(src))
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert "Откат" not in proc.stdout + proc.stderr
    # служба может войти в свой рабочий каталог и читать его
    assert stat.S_IMODE(os.stat(sandbox.app_dir).st_mode) == 0o755
    assert sandbox.as_user("sh", "-c", 'cd "$1" && cat VERSION >/dev/null', "_", sandbox.app_dir) == 0
    with open(os.path.join(sandbox.data, "cwd"), encoding="utf-8") as fh:
        assert fh.read() == sandbox.app_dir
    for base, _dirs, files in os.walk(sandbox.app_dir):
        assert os.stat(base).st_mode & 0o005 == 0o005, base
        for name in files:
            assert os.stat(os.path.join(base, name)).st_mode & 0o004, os.path.join(base, name)
    # скрипты остались исполняемыми, обычные файлы — нет
    assert os.stat(os.path.join(sandbox.app_dir, "scripts", "update.sh")).st_mode & 0o111 == 0o111
    assert os.stat(os.path.join(sandbox.app_dir, "VERSION")).st_mode & 0o111 == 0


@needs_root
def test_update_with_root_umask_077(sandbox):
    """Строгий umask у root не делает установку недоступной службе."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    proc = subprocess.run(["bash", "-c", 'umask 077; exec bash "$1"', "_",
                           os.path.join(src, "scripts", "update.sh")], env=sandbox.env(),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert os.stat(sandbox.wrapper).st_mode & 0o005 == 0o005


@needs_root
def test_update_shows_reason_and_rolls_back_when_service_crashes(sandbox):
    sandbox.install_old()
    before = sandbox.db_text()
    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    # причина отказа видна в выводе скрипта, а не только в журнале сервера
    assert "Application startup failed" in out, _report(proc)
    assert "no such column" in out
    assert "Откат" in out
    # прежняя версия снова работает, и база — та, что была до попытки
    assert sandbox.wait_version() == OLD, _report(proc)
    text = sandbox.db_text()
    assert "opened-by-" + NEW not in text
    assert text.startswith(before)
    assert os.stat(sandbox.db).st_uid == pwd.getpwnam(USER).pw_uid
    # копия базы после отката не остаётся лежать в каталоге резервных копий
    backups = os.listdir(os.path.join(sandbox.install_dir, "backups"))
    assert not [name for name in backups if name.startswith("db_")], backups


@needs_root
def test_update_stops_before_touching_anything_when_directory_is_closed(sandbox):
    """Каталог над установкой закрыт для службы — отказ до остановки службы, без отката."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    # права самого $INSTALL_DIR скрипт чинит, поэтому закрываем то, что над ним, —
    # как на сервере с закрытым /opt
    os.chmod(sandbox.base, 0o700)
    try:
        proc = sandbox.run("update.sh", src)
    finally:
        os.chmod(sandbox.base, 0o755)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "не может войти" in out and sandbox.base in out and "chmod o+x" in out, _report(proc)
    # ничего не тронуто: службу не останавливали, откатывать нечего
    assert "Откат" not in out and "Останавливаю" not in out, _report(proc)
    with open(os.path.join(sandbox.state, "calls"), encoding="utf-8") as fh:
        assert "stop" not in fh.read().split()
    assert sandbox.wait_version() == OLD
    assert not os.path.exists(os.path.join(sandbox.install_dir, "backups")) \
        or not os.listdir(os.path.join(sandbox.install_dir, "backups"))


@needs_root
def test_update_reports_missing_service_user(sandbox):
    sandbox.install_old()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_USER="no-such-user-for-tests")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "нет пользователя службы" in out and "MA_USER" in out, _report(proc)
    assert "Откат" not in out
    assert sandbox.wait_version() == OLD


@needs_root
def test_update_takes_settings_from_unit_file(sandbox):
    """Нестандартные каталоги и пользователь берутся из unit-файла, MA_* повторять не нужно."""
    sandbox.install_old()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_INSTALL_DIR=None, MA_CONFIG_DIR=None,
                       MA_DATA_DIR=None, MA_USER=None, MA_GROUP=None)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    with open(sandbox.unit, encoding="utf-8") as fh:
        unit = fh.read()
    assert "User=%s\n" % USER in unit and "ReadWritePaths=%s\n" % sandbox.data in unit
    assert "WorkingDirectory=%s\n" % sandbox.app_dir in unit and "@" not in unit.split("[Service]")[1]


@needs_root
def test_reinstall_takes_settings_from_unit_file(sandbox):
    """Установка поверх (её советует update.sh, когда испорчено окружение) без MA_* не теряет
    нестандартные каталоги и пользователя: они берутся из unit-файла прежней установки."""
    sandbox.install_old()
    before = sandbox.db_text()
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"), MA_INSTALL_DIR=None, MA_CONFIG_DIR=None,
                       MA_DATA_DIR=None, MA_USER=None, MA_GROUP=None)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    with open(sandbox.unit, encoding="utf-8") as fh:
        unit = fh.read()
    assert "User=%s\n" % USER in unit and "ReadWritePaths=%s\n" % sandbox.data in unit
    assert "WorkingDirectory=%s\n" % sandbox.app_dir in unit
    assert "Environment=MAILARCHIVER_CONFIG=%s\n" % os.path.join(sandbox.config_dir, "config.yaml") in unit
    assert not os.path.exists("/var/lib/mailarchiver") or os.path.samefile("/var/lib/mailarchiver", sandbox.data)
    assert sandbox.db_text().startswith(before)


@needs_root
@pytest.mark.parametrize("signum, group", [(15, False), (2, True), (1, False)])
def test_update_interrupted_by_signal_rolls_back(sandbox, signum, group):
    """Ctrl-C, kill и обрыв сессии посреди обновления: откат, а не полуобновлённая установка."""
    import signal
    # Игнорируемый сигнал наследуется, и bash не может поставить на него ловушку:
    # под nohup (SIGHUP) или у фонового задания без управления заданиями (SIGINT)
    # проверять нечего — скрипт сигнала просто не получит.
    if signal.getsignal(signum) == signal.SIG_IGN:
        pytest.skip("сигнал %d игнорируется у самого теста (nohup или фоновый запуск): "
                    "скрипт унаследует это и ловушку поставить не сможет" % signum)
    sandbox.install_old()
    proc = sandbox.start("update.sh", sandbox.sources(NEW, "src_new"), FAKE_PIP_SLEEP="2")
    calls = os.path.join(sandbox.state, "pip_calls")
    end = time.time() + 30
    while time.time() < end:                 # дождаться установки пакета: файлы уже заменены
        try:
            with open(calls, encoding="utf-8") as fh:
                if fh.read().count("--no-deps") >= 2:
                    break
        except OSError:
            pass
        time.sleep(0.05)
    if group:
        os.killpg(proc.pid, signum)          # как Ctrl-C: сигнал получает и pip
    else:
        os.kill(proc.pid, signum)
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode == 128 + signum, out
    assert "Откат" in out and "Служба работает" in out, out
    assert sandbox.wait_version() == OLD, out
    assert "opened-by-" + NEW not in sandbox.db_text()


@needs_root
def test_update_restores_database_journals(sandbox):
    """При откате возвращаются и журналы базы, а журналы новой версии убираются."""
    sandbox.install_old()
    _write(sandbox.db + "-wal", "wal-of-old\n")
    subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.db + "-wal"], check=True)
    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode != 0 and "База данных возвращена" in proc.stdout, _report(proc)
    with open(sandbox.db + "-wal", encoding="utf-8") as fh:
        assert fh.read() == "wal-of-old\n"
    assert not os.path.exists(sandbox.db + "-journal")
    assert not [n for n in os.listdir(sandbox.data) if n.startswith(".ma-restore")]
    assert sandbox.wait_version() == OLD


@needs_root
def test_update_refuses_when_port_is_taken_by_another_process(sandbox):
    """Служба остановлена, а адрес проверки отвечает — на порту кто-то другой."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Stray(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Stray)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sandbox.install_old()
        url = "http://127.0.0.1:%d/health" % server.server_address[1]
        proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_HEALTH_URL=url)
    finally:
        server.shutdown()
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "порт занят" in out and "Откат" in out, _report(proc)
    assert "opened-by-" + NEW not in sandbox.db_text()      # новую версию не запускали
    assert sandbox.wait_version() == OLD


@needs_root
def test_update_keeps_newest_backups(sandbox):
    """Чистка старых копий оставляет последние (у копий окружения одинаковое время изменения)."""
    sandbox.install_old()
    backups = os.path.join(sandbox.install_dir, "backups")
    os.makedirs(backups)
    for year in range(2019, 2025):
        for prefix in ("app_", "venv_"):
            os.makedirs(os.path.join(backups, "%s%d0101_000000" % (prefix, year)))
            os.utime(os.path.join(backups, "%s%d0101_000000" % (prefix, year)), (1_000_000_000, 1_000_000_000))
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    left = sorted(os.listdir(backups))
    venvs = [n for n in left if n.startswith("venv_")]
    apps = [n for n in left if n.startswith("app_")]
    assert len(venvs) == 3 and venvs[:2] == ["venv_20230101_000000", "venv_20240101_000000"], left
    assert not venvs[2].startswith("venv_202401"), left          # третья — только что снятая
    assert len(apps) == 5 and apps[0] == "app_20210101_000000", left
    assert not [n for n in left if n.startswith("db_")], left


def _move_data_to_storage(sandbox) -> str:
    """Каталог данных — ссылка на каталог «другого диска». → каталог, где «смонтирован» диск."""
    sandbox.stop()
    mount = os.path.join(sandbox.base, "storage")
    os.makedirs(mount)
    os.rename(sandbox.data, os.path.join(mount, "mailarchiver"))
    os.symlink(os.path.join(mount, "mailarchiver"), sandbox.data)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD, sandbox.journal()      # через ссылку служба работает
    return mount


@needs_root
@pytest.mark.parametrize("args", [("--check",), ()])
def test_update_refuses_when_data_disk_is_not_mounted(sandbox, args):
    """Каталог данных — ссылка на диск, который не подключён: отказ до остановки службы.

    Так сорвалось обновление 10.10.2026: проверки проходили («[ -d ]» для ссылки
    в никуда ложно — и проверка каталога данных молча пропускалась), отказывал
    уже пробный импорт, а после отката прежняя версия не поднималась с
    «status=226/NAMESPACE … No such file or directory».
    """
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    sandbox.stop()
    stash = sandbox.unplug(mount)                                # диск «отключился»
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_crashed() and "No such file or directory" in sandbox.journal()
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("update.sh", src, args=args)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "ссылка на %s" % os.path.join(mount, "mailarchiver") in out, _report(proc)
    assert "не подключён диск" in out and "Неподключённых дисков lsblk не видит" in out, _report(proc)
    assert "Служба не работает уже сейчас, до обновления" in out, _report(proc)
    # ничего не тронуто
    assert "Останавливаю" not in out and "Откат" not in out, _report(proc)
    with open(os.path.join(sandbox.app_dir, "VERSION"), encoding="utf-8") as fh:
        assert fh.read().strip() == OLD
    assert not os.path.exists(os.path.join(sandbox.install_dir, "backups"))
    # диск вернули — служба поднимается, обновление проходит, база на месте
    sandbox.plug(stash, mount)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    with open(os.path.join(mount, "mailarchiver", "mailarchiver.db"), encoding="utf-8") as fh:
        assert "opened-by-" + NEW in fh.read()


@needs_root
@pytest.mark.parametrize("style", ["", "unit-root"])
def test_rollback_names_missing_data_directory(sandbox, style):
    """Диск с данными пропал посреди обновления: откат называет каталог, а не советует про контейнеры.

    Путь в журнале systemd бывает записан и как «/./каталог», и изнутри собираемого
    окружения — «/run/systemd/unit-root/каталог»; назван должен быть сам каталог.
    """
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    stash = os.path.join(tempfile.mkdtemp(prefix="ma-unplugged-"), "disk")
    sandbox.stashes.append(os.path.dirname(stash))
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), FAKE_SYSTEMD_STYLE=style,
                       FAKE_PIP_RUN='mv "%s" "%s"' % (mount, stash))
    sandbox.plug(stash, mount)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "ссылка на %s" % os.path.join(mount, "mailarchiver") in out, _report(proc)
    assert "Откат" in out and "служба НЕ поднялась" in out, _report(proc)
    assert "status=226/NAMESPACE" in out, _report(proc)
    # назван именно тот каталог, что в журнале systemd («/./…» приведено к обычному пути)
    assert "systemd не нашёл каталог %s — " % os.path.join(mount, "mailarchiver") in out, _report(proc)
    assert "не подключён диск" in out, _report(proc)
    assert "MA_RELAX_SANDBOX" not in out and "контейнер" not in out, _report(proc)
    # файлы возвращены: с диском на месте прежняя версия работает
    sandbox.systemctl("restart", "mailarchiver")
    assert sandbox.wait_version() == OLD


@needs_root
def test_rollback_says_service_was_down_before_update(sandbox):
    """Служба падала ещё до обновления — после отката это сказано прямо."""
    sandbox.install_old()
    sandbox.behaviour("crash:*")                    # падает любая версия
    sandbox.systemctl("restart", "mailarchiver")
    assert sandbox.wait_crashed(), sandbox.journal()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "Служба не работает уже сейчас, до обновления" in out, _report(proc)
    assert "Application startup failed" in out
    assert "служба НЕ поднялась" in out and "До обновления служба тоже не работала" in out, _report(proc)


@needs_root
def test_install_refuses_dangling_data_directory(sandbox):
    os.symlink(os.path.join(sandbox.base, "no-such-disk", "mailarchiver"), sandbox.data)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "ссылка на" in out and "не подключён диск" in out, _report(proc)
    assert not os.path.exists(sandbox.install_dir)          # ничего не установлено
    assert os.path.islink(sandbox.data)


@needs_root
def test_update_detects_broken_import_before_start(sandbox):
    sandbox.install_old()
    before = sandbox.db_text()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new", broken_import=True))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "no_such_module_in_tests" in out, _report(proc)
    assert "Откат" in out
    assert sandbox.wait_version() == OLD
    assert sandbox.db_text().startswith(before) and "opened-by-" + NEW not in sandbox.db_text()


@needs_root
def test_update_waits_for_slow_start(sandbox):
    """Долгий первый запуск (миграция базы) — не повод для отката."""
    sandbox.install_old()
    sandbox.behaviour("slow:%s:6" % NEW)
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW


@needs_root
def test_update_gives_up_on_hung_start(sandbox):
    sandbox.install_old()
    sandbox.behaviour("hang:" + NEW)
    # срок больше, чем запасной способ «прожила 10 секунд»: так проверяется именно опрос /health
    began = time.time()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_START_TIMEOUT="13")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "http://127.0.0.1:%d/health" % sandbox.port in out and "MA_START_TIMEOUT" in out, _report(proc)
    # процесс спит: срок не продлевается
    assert "уже ничего не делала" in out and "служба занята" not in out, _report(proc)
    assert "Откат" in out
    assert sandbox.wait_version() == OLD
    assert time.time() - began < 90, _report(proc)


@needs_root
def test_update_keeps_waiting_while_first_start_is_busy(sandbox):
    """Срок вышел, а новая версия всё ещё занята (строит индексы большой базы): ждём дальше, а не откатываем.

    Обновление с 1.2.0 на большой базе строит индексы при первом запуске, и за 180 секунд
    по умолчанию это не всегда успевает: откат выбросил бы сделанное, а следующая попытка
    начала бы заново.
    """
    sandbox.install_old()
    sandbox.behaviour("busy:%s:16" % NEW)
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_START_TIMEOUT="3", MA_PROGRESS_EVERY="4")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "Срок ожидания (3 с) вышел, но служба занята" in out, out
    assert "…жду ответа веб-интерфейса: прошло" in out and "служба прочитала" in out, out
    assert "Откат" not in out, out
    assert sandbox.wait_version() == NEW


@needs_root
def test_update_gives_up_on_busy_start_after_the_overall_limit(sandbox):
    """Занята дольше MA_START_TIMEOUT_MAX — откат, и сказано, какую переменную увеличить."""
    sandbox.install_old()
    sandbox.behaviour("busy:%s:600" % NEW)
    began = time.time()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_START_TIMEOUT="3",
                       MA_START_TIMEOUT_MAX="15")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert re.search(r"Служба занята уже \d+ с — дольше предела MA_START_TIMEOUT_MAX", out), _report(proc)
    assert "MA_START_TIMEOUT_MAX=14400" in out, _report(proc)
    assert "Откат" in out
    assert sandbox.wait_version() == OLD
    assert time.time() - began < 120, _report(proc)


@needs_root
def test_update_health_check_ignores_proxy_settings(sandbox):
    """http_proxy в окружении root (нужен pip) не должен уводить проверку /health на прокси."""
    sandbox.install_old()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"),
                       http_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
                       https_proxy="http://127.0.0.1:9", no_proxy="")
    assert proc.returncode == 0, _report(proc)
    assert sandbox.health_hits(NEW) >= 1          # скрипт действительно дождался ответа службы
    assert sandbox.wait_version() == NEW


@needs_root
def test_update_health_check_uses_configured_address():
    """Служба слушает не 127.0.0.1 — проверка идёт по адресу из конфигурации."""
    base = tempfile.mkdtemp(prefix="ma-scripts-")
    box = Sandbox(base, host="127.0.0.2")
    try:
        box.install_old()
        proc = box.run("update.sh", box.sources(NEW, "src_new"))
        assert proc.returncode == 0, _report(proc)
        assert box.health_hits(NEW) >= 1          # опрос шёл по адресу из конфигурации
        assert box.wait_version() == NEW
    finally:
        box.stop()
        shutil.rmtree(base, ignore_errors=True)
        for stash in box.stashes:
            shutil.rmtree(stash, ignore_errors=True)


@needs_root
def test_update_installs_package_before_requirements(sandbox):
    """Сначала пакет (без зависимостей), потом библиотеки — иначе pip пугает «конфликтом»."""
    sandbox.install_old()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    with open(os.path.join(sandbox.state, "pip_calls"), encoding="utf-8") as fh:
        calls = [line for line in fh.read().splitlines() if line.startswith(("install", "check"))]
    package = next(i for i, c in enumerate(calls) if "--no-deps" in c and sandbox.app_dir in c and i > 0)
    reqs = next(i for i, c in enumerate(calls) if "-r " in c)
    assert package < reqs, calls
    assert any(c.startswith("check") for c in calls[reqs:]), calls


# ---------------------------------------------------------------------------
#  update.sh --check: проверка без остановки службы и без изменений на диске
# ---------------------------------------------------------------------------
def _pip_changes(sandbox) -> list:
    """Вызовы pip, которые что-то меняют (вопрос о версии, чтение настроек и пробная установка — не меняют)."""
    harmless = ("--version", "--disable-pip-version-check config list", "install --dry-run ")
    return [call for call in sandbox.calls("pip_calls") if not call.startswith(harmless)]


def _untouched(sandbox, before, proc, *trees) -> None:
    """Скрипт ничего не остановил, не установил и не изменил на диске."""
    out = proc.stdout + proc.stderr
    assert "Откат" not in out and "Останавливаю" not in out and "прервано" not in out, _report(proc)
    assert sandbox.changed_state() == [], sandbox.calls()
    assert _pip_changes(sandbox) == [] and sandbox.calls("pkg_calls") == []
    after = sandbox.snapshot(*trees)
    assert after == before, sorted(set(after.items()) ^ set(before.items()))
    assert not os.path.exists(os.path.join(sandbox.install_dir, "backups"))


def _marks(proc, sign: str) -> list:
    """Строки вывода, отмеченные знаком (✓, ⚠ или ✗) сразу после названия скрипта."""
    return [line for line in (proc.stdout + proc.stderr).splitlines()
            if line.startswith("[MailArchiver ") and "] %s " % sign in line[:40]]


@needs_root
def test_check_mode_reports_and_changes_nothing(sandbox):
    """Проверка проходит по всем пунктам, но служба не останавливается и на диске ничего не меняется."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    pid = sandbox.systemctl("show", "mailarchiver", "-p", "MainPID", "--value").stdout.strip()
    # служба когда-то падала и была перезапущена, но сейчас отвечает — это не «не работает»
    _write(os.path.join(sandbox.state, "nrestarts"), "2")
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    rsync = shutil.which("rsync", path=sandbox.env()["PATH"])
    for wanted in ("Текущая версия: %s → новая версия: %s" % (OLD, NEW),
                   "Пользователь службы %s" % USER,
                   "Места для копии текущей установки хватает",
                   "Места для копии базы хватает",
                   "подходит новой версии (нужен >=3.8,<4)",
                   "файлов .py) подходят Python окружения",
                   "Новая версия принимает конфигурацию " + os.path.join(sandbox.config_dir, "config.yaml"),
                   "отвечает по адресу проверки: http://127.0.0.1:%d/health" % sandbox.port,
                   "библиотеки будут загружены из сети",
                   "Индекс пакетов отвечает: " + _index_url(),
                   # какой из двух способов копирования выбран — по тому, есть ли rsync
                   "rsync найден — файлы будут обновлены им." if rsync
                   else "rsync не найден — файлы будут скопированы командой cp",
                   "Проверка пройдена — можно обновлять"):
        assert wanted in out, (wanted, _report(proc))
    assert ("rsync найден" in out) != ("rsync не найден" in out), _report(proc)
    assert "--force" not in out, _report(proc)
    assert not _marks(proc, "✗") and not _marks(proc, "⚠"), _report(proc)
    assert "226/NAMESPACE" not in out                       # не контейнер — совета про него нет
    _untouched(sandbox, before, proc, src)
    # та же служба, тот же процесс
    assert sandbox.systemctl("show", "mailarchiver", "-p", "MainPID", "--value").stdout.strip() == pid
    assert sandbox.wait_version() == OLD


@needs_root
def test_check_mode_fails_on_what_would_break_the_update(sandbox):
    """Конфигурацию не принимает новая версия, модуль не разбирается, Python не тот — отказ до изменений."""
    sandbox.install_old()
    rejecting = sandbox.sources(NEW, "src_rejecting", rejects_config=True)
    unparsable = sandbox.sources(NEW, "src_unparsable", syntax_error=True)
    too_new = sandbox.sources(NEW, "src_too_new", requires_python=">=3.99")
    sandbox.forget_calls()
    trees = (rejecting, unparsable, too_new)
    before = sandbox.snapshot(*trees)

    proc = sandbox.run("update.sh", rejecting, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "не принимает конфигурацию" in out and "server.legacy_mode больше не поддерживается" in out, _report(proc)
    # знаков ✗ столько же, сколько отказов (и ещё один — в итоговой строке)
    assert "Проверка не пройдена: ошибок — 1" in out and len(_marks(proc, "✗")) == 2, _report(proc)
    _untouched(sandbox, before, proc, *trees)

    proc = sandbox.run("update.sh", unparsable, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "не подходит Python окружения" in out and "mailarchiver/web/future.py:1" in out, _report(proc)
    assert "принимает конфигурацию" in out                  # остальные проверки выполнены
    _untouched(sandbox, before, proc, *trees)

    proc = sandbox.run("update.sh", too_new, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "не подходит новой версии: ей нужен Python >=3.99" in out and "install.sh" in out, _report(proc)
    _untouched(sandbox, before, proc, *trees)
    assert sandbox.wait_version() == OLD


@needs_root
@pytest.mark.parametrize("flaw", ["rejects_config", "syntax_error", "requires_python"])
def test_update_runs_the_same_checks_before_stopping_the_service(sandbox, flaw):
    """Обычное обновление начинается с той же проверки: негодная версия не останавливает службу."""
    sandbox.install_old()
    options = {"requires_python": ">=3.99"} if flaw == "requires_python" else {flaw: True}
    src = sandbox.sources(NEW, "src_new", **options)
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src)
    assert proc.returncode != 0, _report(proc)
    assert "Проверка не пройдена" in proc.stderr and "Ничего не изменено" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


@needs_root
def test_check_mode_warnings_do_not_fail(sandbox):
    """Та же или более старая версия, молчащий адрес проверки, контейнер — предупреждения, код 0."""
    sandbox.install_old()
    same = sandbox.sources(OLD, "src_same")
    older = sandbox.sources("1.1.10", "src_older")
    newer = sandbox.sources("1.10.0", "src_newer")           # 1.10.0 новее 1.2.0 — сравнение по числам
    sandbox.forget_calls()
    trees = (same, older, newer)
    before = sandbox.snapshot(*trees)

    proc = sandbox.run("update.sh", same, args=("--check",))
    assert proc.returncode == 0 and "Версия не меняется (%s → %s)" % (OLD, OLD) in proc.stdout, _report(proc)
    assert "предупреждений — 1" in proc.stdout and len(_marks(proc, "⚠")) == 1, _report(proc)

    proc = sandbox.run("update.sh", older, args=("--check",))
    assert proc.returncode == 0 and "Понижение версии: установлена %s, в исходниках 1.1.10" % OLD in proc.stdout, \
        _report(proc)

    proc = sandbox.run("update.sh", newer, args=("--check",), FAKE_VIRT="lxc",
                       MA_HEALTH_URL="http://127.0.0.1:9/health")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "Текущая версия: %s → новая версия: 1.10.0" % OLD in out and "Понижение" not in out, _report(proc)
    assert "адрес проверки не отвечает: http://127.0.0.1:9/health" in out and "MA_HEALTH_URL=" in out, _report(proc)
    assert "контейнер (lxc)" in out and "226/NAMESPACE" in out and "MA_RELAX_SANDBOX=1" in out, _report(proc)
    assert not _marks(proc, "✗"), _report(proc)
    _untouched(sandbox, before, proc, *trees)


@needs_root
def test_check_mode_free_space(sandbox):
    """Нет места для копии установки — отказ; не хватает только на копию базы — предупреждение."""
    sandbox.install_old()
    with open(sandbox.db, "ab") as fh:                       # база на 60 МБ
        for _ in range(60):
            fh.write(os.urandom(1 << 20))
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    used = subprocess.run(["du", "-sk", sandbox.app_dir, sandbox.venv], capture_output=True, text=True, check=True)
    need = sum(int(line.split()[0]) for line in used.stdout.splitlines())

    # на копию установки (и запас 50 МБ) хватает, на копию базы после неё — уже нет
    proc = sandbox.run("update.sh", src, args=("--check",), FAKE_DF_FREE_KB=str(need + 51200 + 2048))
    assert proc.returncode == 0, _report(proc)
    assert "Для копии базы (60 МБ) места не хватит" in proc.stdout and "без копии базы" in proc.stdout, _report(proc)
    assert "Места для копии текущей установки хватает" in proc.stdout and not _marks(proc, "✗"), _report(proc)
    _untouched(sandbox, before, proc, src)

    proc = sandbox.run("update.sh", src, args=("--check",), FAKE_DF_FREE_KB=str(need + 51200 - 1024))
    assert proc.returncode != 0 and "Мало места" in proc.stderr and "Освободите место" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, src)

    # то же — отказ обычного обновления, до остановки службы
    proc = sandbox.run("update.sh", src, FAKE_DF_FREE_KB="1000")
    assert proc.returncode != 0 and "Мало места" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


@needs_root
def test_usage_and_unknown_arguments(sandbox):
    """Справка и опечатка в параметре — не «прерванное обновление»: без отката и без единого действия."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    for script in ("update.sh", "install.sh"):
        proc = sandbox.run(script, src, args=("--chek",))
        assert proc.returncode == 2, _report(proc)
        assert "Неизвестный параметр: --chek" in proc.stderr and "Использование:" in proc.stderr, _report(proc)
        assert proc.stdout == ""
        _untouched(sandbox, before, proc, src)
        for flag in ("-h", "--help"):
            proc = sandbox.run(script, src, args=(flag,))
            assert proc.returncode == 0 and proc.stderr == "", _report(proc)
            for wanted in ("Использование:", "MA_RELAX_SANDBOX", "MA_WHEELS", "MA_HEALTH_URL", "MA_INSTALL_DIR",
                           "MA_INDEX_URL",
                           # переменные пишут после sudo: перед ним они до скрипта не доходят
                           "Задаются ПОСЛЕ sudo", "sudo MA_", "bash scripts/" + script):
                assert wanted in proc.stdout, (script, wanted)
            assert "MA_CHECK_ONLY" not in proc.stdout and "MA_FSTAB" not in proc.stdout, script
            _untouched(sandbox, before, proc, src)
    # --check и --force есть только у update.sh; install.sh их не «проглатывает» молча
    usage = sandbox.run("update.sh", src, args=("--help",)).stdout
    assert "--check" in usage and "--force" in usage and "[--check] [--force]" in usage
    proc = sandbox.run("install.sh", src, args=("--force",))
    assert proc.returncode == 2 and "Неизвестный параметр: --force" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, src)
    proc = sandbox.run("install.sh", src, args=("--check",))
    assert proc.returncode == 2 and "Неизвестный параметр: --check" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


# ---------------------------------------------------------------------------
#  MA_RELAX_SANDBOX=1: ослабленная защита службы для контейнеров
# ---------------------------------------------------------------------------
def _dropin_keys(text: str) -> dict:
    keys, section = {}, ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif line and not line.startswith("#"):
            assert section == "[Service]", line
            key, value = line.split("=", 1)
            keys[key] = value
    return keys


@needs_root
def test_update_in_container_without_namespaces_names_the_remedy(sandbox):
    """status=226/NAMESPACE: откат и точный совет — повторить с MA_RELAX_SANDBOX=1."""
    sandbox.install_old(old_unit=True)
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("update.sh", src, FAKE_NAMESPACE_FAIL="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "status=226/NAMESPACE" in out and "Failed to set up mount namespacing" in out, _report(proc)
    assert "sudo MA_RELAX_SANDBOX=1 bash scripts/update.sh" in out and "nesting" in out, _report(proc)
    assert sandbox.dropin in out and "непривилегированного пользователя " + USER in out, _report(proc)
    assert "Откат" in out and "Служба работает" in out, _report(proc)
    # без явной просьбы защита не ослабляется
    assert not os.path.exists(os.path.dirname(sandbox.dropin))
    assert sandbox.wait_version() == OLD
    assert "opened-by-" + NEW not in sandbox.db_text()


@needs_root
def test_update_with_relaxed_sandbox(sandbox):
    sandbox.install_old(old_unit=True)
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    proc = sandbox.run("update.sh", src, FAKE_NAMESPACE_FAIL="1", MA_RELAX_SANDBOX="1")
    assert proc.returncode == 0, _report(proc)
    assert "защита службы ослаблена дополнением " + sandbox.dropin in proc.stdout, _report(proc)
    assert sandbox.wait_version() == NEW
    assert stat.S_IMODE(os.stat(sandbox.dropin).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(os.path.dirname(sandbox.dropin)).st_mode) == 0o755
    assert os.stat(sandbox.dropin).st_uid == 0
    with open(sandbox.dropin, encoding="utf-8") as fh:
        text = fh.read()
    keys = _dropin_keys(text)
    assert keys["ProtectProc"] == "default" and keys["PrivateDevices"] == "no" and keys["ReadWritePaths"] == ""
    assert text.startswith("# ") and "MA_RELAX_SANDBOX=1" in text and "daemon-reload" in text
    assert 'rm "%s"' % sandbox.dropin in text and USER in text
    # systemd перечитал настройки после появления дополнения и до запуска службы
    calls = sandbox.calls()
    assert "daemon-reload" in calls and calls.index("daemon-reload") < calls.index("start mailarchiver"), calls
    # сам unit остался полным: ослабление — отдельным файлом
    with open(sandbox.unit, encoding="utf-8") as fh:
        assert "ProtectProc=invisible" in fh.read()

    # следующее обновление — уже без переменной: файл не трогается, о нём напоминают
    stamp = os.stat(sandbox.dropin).st_mtime_ns
    proc = sandbox.run("update.sh", sandbox.sources("9.9.10", "src_next"), FAKE_NAMESPACE_FAIL="1")
    assert proc.returncode == 0, _report(proc)
    assert "Служба работает с ослабленной защитой: действует дополнение " + sandbox.dropin in proc.stdout, \
        _report(proc)
    assert os.stat(sandbox.dropin).st_mtime_ns == stamp
    assert sandbox.wait_version() == "9.9.10"


@needs_root
def test_rollback_removes_only_its_own_dropin(sandbox):
    """Дополнение, созданное неудачным запуском, откат убирает; существовавшее до него — нет."""
    sandbox.install_old(old_unit=True)
    src = sandbox.sources(NEW, "src_new")
    sandbox.behaviour("crash:" + NEW)
    sandbox.forget_calls()
    proc = sandbox.run("update.sh", src, MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Откат" in out, _report(proc)
    assert "защита службы ослаблена дополнением" in out and "созданное этим запуском, убрано" in out, _report(proc)
    assert not os.path.exists(sandbox.dropin) and not os.path.exists(os.path.dirname(sandbox.dropin))
    # после удаления файла systemd перечитал настройки, и только потом служба запущена снова
    calls = sandbox.calls()
    assert calls.count("daemon-reload") == 2, calls
    last = len(calls) - 1 - calls[::-1].index("daemon-reload")
    assert "restart mailarchiver" in calls[last:], calls
    assert sandbox.wait_version() == OLD

    # дополнение уже было (его правил администратор) — остаётся как есть и при MA_RELAX_SANDBOX=1
    mine = "# правка администратора\n[Service]\nProtectProc=default\nPrivateDevices=no\n"
    _write(sandbox.dropin, mine, 0o600)
    proc = sandbox.run("update.sh", src, MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Откат" in out, _report(proc)
    assert "уже включена: %s — файл оставлен как есть" % sandbox.dropin in out, _report(proc)
    assert "убрано" not in out
    with open(sandbox.dropin, encoding="utf-8") as fh:
        assert fh.read() == mine
    assert stat.S_IMODE(os.stat(sandbox.dropin).st_mode) == 0o600
    assert sandbox.wait_version() == OLD


@needs_root
def test_relaxed_sandbox_that_does_not_help_is_reported(sandbox):
    """Дополнение действует, а служба всё равно падает с 226 — совет «повторите с MA_RELAX_SANDBOX» неуместен."""
    sandbox.install_old(old_unit=True)
    _write(sandbox.dropin, "[Service]\nProtectProc=default\n")       # PrivateDevices не отключён
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), FAKE_NAMESPACE_FAIL="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "status=226/NAMESPACE" in out, _report(proc)
    assert "Ослабленная защита уже включена (%s)" % sandbox.dropin in out, _report(proc)
    assert "Повторите с MA_RELAX_SANDBOX=1" not in out, _report(proc)
    assert os.path.exists(sandbox.dropin)
    assert sandbox.wait_version() == OLD


@needs_root
def test_unknown_relax_value_does_not_loosen_anything(sandbox):
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    # о непонятом значении сказано уже на проверке; совета «повторите с MA_RELAX_SANDBOX=1»
    # для контейнера при этом нет — переменную и так задали, только неверно
    proc = sandbox.run("update.sh", src, args=("--check",), MA_RELAX_SANDBOX="yes", FAKE_VIRT="lxc")
    assert proc.returncode == 0, _report(proc)
    assert "MA_RELAX_SANDBOX=yes: значение не понято, защита службы меняться не будет." in proc.stdout, \
        _report(proc)
    assert "задайте MA_RELAX_SANDBOX=1" in proc.stdout and len(_marks(proc, "⚠")) == 1, _report(proc)
    assert "Сервер — контейнер" not in proc.stdout and "повторите обновление" not in proc.stdout, _report(proc)
    proc = sandbox.run("update.sh", src, MA_RELAX_SANDBOX="yes")
    assert proc.returncode == 0, _report(proc)
    assert "MA_RELAX_SANDBOX=yes: значение не понято" in proc.stdout, _report(proc)
    assert not os.path.exists(os.path.dirname(sandbox.dropin))


@needs_root
def test_signal_while_creating_dropin_directory_leaves_nothing(sandbox):
    """Сигнал между созданием каталога <unit>.d и записью дополнения: откат не оставляет пустой каталог."""
    sandbox.install_old(old_unit=True)
    src = sandbox.sources(NEW, "src_new")
    # «chmod» каталога дополнения прерывает скрипт: каталог уже создан, файла в нём ещё нет
    _write(os.path.join(sandbox.bin, "chmod"),
           '#!/bin/sh\ncase "$*" in *mailarchiver.service.d) kill -TERM "$PPID" ;; esac\nexec %s "$@"\n'
           % shutil.which("chmod"), 0o755)
    proc = sandbox.run("update.sh", src, MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 143 and "Откат" in out and "Служба работает" in out, _report(proc)
    assert "защита службы ослаблена" not in out, _report(proc)          # до записи файла дело не дошло
    assert not os.path.exists(os.path.dirname(sandbox.dropin))
    assert sandbox.wait_version() == OLD


@needs_root
def test_existing_dropin_path_that_is_a_link_is_left_alone(sandbox):
    """По пути дополнения лежит ссылка (так его отключают: ссылка на /dev/null) — её не трогают.

    Раньше скрипт писал «сквозь» ссылку (и менял права на том, куда она ведёт), а
    при откате удалял саму ссылку. Здесь ссылка ведёт в никуда — как и /dev/null,
    это не обычный файл, — чтобы ошибка в скрипте не испортила машину с тестами.
    """
    sandbox.install_old(old_unit=True)
    src = sandbox.sources(NEW, "src_new")
    target = os.path.join(sandbox.base, "masked")
    os.makedirs(os.path.dirname(sandbox.dropin))
    os.symlink(target, sandbox.dropin)
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",), MA_RELAX_SANDBOX="1")
    assert proc.returncode == 0, _report(proc)
    assert "по пути дополнения %s уже что-то лежит" % sandbox.dropin in proc.stdout, _report(proc)
    assert "ослаблена НЕ будет" in proc.stdout and len(_marks(proc, "⚠")) == 1, _report(proc)
    _untouched(sandbox, before, proc, src)

    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("update.sh", src, MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Откат" in out, _report(proc)
    assert ("MA_RELAX_SANDBOX=1: по пути дополнения %s уже лежит ссылка на %s — оставлено как есть, "
            "защита службы НЕ ослаблена." % (sandbox.dropin, target)) in out, _report(proc)
    assert "убрано" not in out and "защита службы ослаблена дополнением" not in out, _report(proc)
    assert os.path.islink(sandbox.dropin) and os.readlink(sandbox.dropin) == target
    assert not os.path.lexists(target)                       # «сквозь» ссылку ничего не записано
    assert sandbox.wait_version() == OLD

    # ссылка на действующий файл — это действующее дополнение: о нём напоминают, но не трогают
    conf = os.path.join(sandbox.base, "relax.conf")
    _write(conf, "[Service]\nProtectProc=default\nPrivateDevices=no\n", 0o600)
    os.remove(sandbox.dropin)
    os.symlink(conf, sandbox.dropin)
    sandbox.behaviour("")
    proc = sandbox.run("update.sh", src, FAKE_NAMESPACE_FAIL="1")
    assert proc.returncode == 0, _report(proc)
    assert "действует дополнение %s (ссылка на %s)" % (sandbox.dropin, conf) in proc.stdout, _report(proc)
    assert os.path.islink(sandbox.dropin) and stat.S_IMODE(os.stat(conf).st_mode) == 0o600
    assert sandbox.wait_version() == NEW


# настройки шаблона, которые к защите не относятся
UNIT_PLAIN = {"Type", "User", "Group", "Environment", "WorkingDirectory", "ExecStart", "Restart", "RestartSec",
              "TimeoutStopSec"}
# настройки, которым пространства имён не нужны: это лимиты cgroup, фильтр системных
# вызовов (seccomp) или флаги процесса — они работают и в контейнере без вложенности,
# поэтому дополнение их не трогает
UNIT_NO_NAMESPACE = {"MemoryMax", "MemoryHigh", "LimitNOFILE", "TasksMax", "NoNewPrivileges",
                     "RestrictSUIDSGID", "LockPersonality", "RestrictRealtime", "RemoveIPC",
                     "RestrictAddressFamilies", "UMask"}


def test_relax_dropin_covers_unit_template(tmp_path):
    """Каждая настройка шаблона unit либо заведомо не требует пространства имён, либо отключается дополнением.

    Новая строка в systemd/mailarchiver.service заставит решить, к какой группе она относится:
    иначе в контейнере без вложенности служба снова упадёт с 226/NAMESPACE и при MA_RELAX_SANDBOX=1.
    """
    if not shutil.which("bash"):
        pytest.skip("нет bash")
    unit = tmp_path / "systemd" / "mailarchiver.service"
    script = ('set -Eeuo pipefail; LOG_PREFIX=t; RED=; GRN=; YLW=; BLU=; NC=; APP_NAME=mailarchiver; '
              'APP_USER=svc; UNIT_FILE="$1"; . "$2"; export MA_RELAX_SANDBOX=1; apply_relax_sandbox; '
              'echo "created=$RELAX_CREATED"')
    proc = subprocess.run(["bash", "-c", script, "_", str(unit), os.path.join(ROOT, "scripts", "common.sh")],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and "created=1" in proc.stdout, _report(proc)
    dropin = tmp_path / "systemd" / "mailarchiver.service.d" / "50-relax-sandbox.conf"
    relaxed = _dropin_keys(dropin.read_text(encoding="utf-8"))

    template, section = {}, ""
    with open(os.path.join(ROOT, "systemd", "mailarchiver.service"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == "[Service]" and line and not line.startswith("#"):
                key, value = line.split("=", 1)
                template[key] = value
    assert {"ProtectProc", "PrivateDevices", "ProtectSystem", "ReadWritePaths"} <= set(template)   # разбор верен
    undecided = set(template) - UNIT_PLAIN - UNIT_NO_NAMESPACE - set(relaxed)
    assert not undecided, "в шаблоне unit есть настройки, которые дополнение не отключает: %s" % sorted(undecided)
    assert not set(relaxed) & (UNIT_PLAIN | UNIT_NO_NAMESPACE)
    # в дополнении нет строк, которых нет в шаблоне, и каждая действительно отключает настройку
    assert set(relaxed) <= set(template), set(relaxed) - set(template)
    off = {"ProtectProc": "default", "ReadWritePaths": ""}
    for key, value in relaxed.items():
        assert value == off.get(key, "no"), (key, value)
        assert template[key] != value, key


# ---------------------------------------------------------------------------
#  Обновление без доступа в интернет (каталог wheels)
# ---------------------------------------------------------------------------
def _installs(sandbox) -> list:
    """Вызовы «pip install», которые действительно устанавливают (без пробных — --dry-run)."""
    return [call for call in sandbox.calls("pip_calls")
            if call.startswith("install") and "--dry-run" not in call]


@needs_root
def test_update_offline_from_wheels_directory(sandbox):
    """Каталог wheels рядом с исходниками: pip не ходит в сеть, приложение ставится из своего пакета."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new", wheels=True)
    wheels = os.path.join(src, "wheels")
    # пакет под другой Python и другую архитектуру: проверка о нём предупредит
    open(os.path.join(wheels, "otherdep-1.0-cp27-cp27mu-manylinux1_i686.whl"), "w").close()
    open(os.path.join(wheels, "puredep-2.0-py2.py3-none-any.whl"), "w").close()
    sandbox.forget_calls()
    before = sandbox.snapshot(src)

    # pip старше 22.2 пробную установку не умеет — остаётся оценка по именам файлов
    proc = sandbox.run("update.sh", src, args=("--check",), FAKE_PIP_NO_DRY_RUN="1")
    assert proc.returncode == 0, _report(proc)
    assert "Обновление без сети: пакеты (3 шт.) будут взяты из " + wheels in proc.stdout, _report(proc)
    misfit = [line for line in proc.stdout.splitlines() if "нет пакетов под этот Python" in line]
    assert len(misfit) == 1 and misfit[0].rstrip().endswith("для: otherdep."), _report(proc)
    assert re.search(r"под этот Python \(cp3\d+ \w+\)", misfit[0]), misfit      # названо, под что нужны пакеты
    assert "не умеет пробную установку" in proc.stdout and len(_marks(proc, "⚠")) == 1, _report(proc)
    _untouched(sandbox, before, proc, src)
    # современный pip отвечает точно: лишний пакет под другой Python установке не мешает
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0 and "pip проверил пробной установкой" in proc.stdout, _report(proc)
    assert not _marks(proc, "⚠") and "нет пакетов под этот Python" not in proc.stdout, _report(proc)
    _untouched(sandbox, before, proc, src)

    sandbox.forget_calls()
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert "Обновление без сети: пакеты берутся из " + wheels in proc.stdout, _report(proc)
    installs = _installs(sandbox)
    # ни одного обращения к сети: ни обновления pip, ни сборки из исходников
    assert installs and all("--no-index --find-links %s " % wheels in call for call in installs), installs
    assert not [call for call in installs if "--upgrade pip" in call or sandbox.app_dir + " " in call + " "], installs
    package = next(i for i, call in enumerate(installs) if "mailarchiver==" + NEW in call)
    assert "--no-deps" in installs[package] and "--force-reinstall" in installs[package], installs
    reqs = next(i for i, call in enumerate(installs) if "-r %s/requirements.txt" % sandbox.app_dir in call)
    assert package < reqs and "--no-deps" not in installs[reqs], installs
    assert sandbox.calls("pip_calls")[-1] == "check"
    assert sandbox.calls("pkg_calls") == []                  # и менеджер пакетов без сети не вызывается
    assert "Индекс пакетов" not in proc.stdout               # и индекс не опрашивается
    # установлен именно пакет из каталога, а не копия исходников
    assert os.path.exists(sandbox.site_file("from_wheel.py"))
    assert not os.path.exists(os.path.join(sandbox.app_dir, "mailarchiver", "from_wheel.py"))
    assert not os.path.exists(os.path.join(sandbox.app_dir, "wheels"))     # пакеты в установку не копируются
    assert sandbox.wait_version() == NEW


@needs_root
def test_update_offline_refuses_unusable_wheels_before_touching_anything(sandbox):
    """Нет пакета приложения нужной версии, MA_WHEELS указывает в никуда или в пустой каталог."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    stale = os.path.join(sandbox.base, "stale")
    make_wheel(stale, "9.9.8")                                # пакет есть, но другой версии
    empty = os.path.join(sandbox.base, "empty")
    os.makedirs(empty)
    nowhere = os.path.join(sandbox.base, "no-such-dir")
    sandbox.forget_calls()
    before = sandbox.snapshot(src, stale, empty)
    for wheels, message in ((stale, "нет пакета приложения mailarchiver-%s-*.whl" % NEW),
                            (empty, "В каталоге MA_WHEELS=%s нет ни одного пакета .whl" % empty),
                            (nowhere, "Каталог пакетов MA_WHEELS=%s не найден" % nowhere)):
        for args in (("--check",), ()):
            proc = sandbox.run("update.sh", src, args=args, MA_WHEELS=wheels)
            assert proc.returncode != 0, _report(proc)
            assert message in proc.stderr and "Проверка не пройдена" in proc.stderr, _report(proc)
            _untouched(sandbox, before, proc, src, stale, empty)
    assert sandbox.wait_version() == OLD


@needs_root
def test_update_offline_rolls_back_when_pip_cannot_install(sandbox):
    """Пакет пропал уже после проверки (или pip его не принял) — обычный откат с понятной причиной."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new", wheels=True)
    # настоящий pip отказался бы так же, если бы пакет не подошёл этому Python
    os.rename(os.path.join(src, "wheels", "mailarchiver-%s-py3-none-any.whl" % NEW),
              os.path.join(src, "wheels", "mailarchiver-%s-py3-none-any.whl.part" % NEW))
    open(os.path.join(src, "wheels", "mailarchiver-%s-broken.whl" % NEW), "w").close()   # не zip
    proc = sandbox.run("update.sh", src)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "Не удалось установить пакет mailarchiver %s из %s" % (NEW, os.path.join(src, "wheels")) in out, \
        _report(proc)
    assert "Откат" in out and "Служба работает" in out, _report(proc)
    assert sandbox.wait_version() == OLD
    assert "opened-by-" + NEW not in sandbox.db_text()


# ---------------------------------------------------------------------------
#  Каталог данных: неподключённый диск, ссылки, права
# ---------------------------------------------------------------------------
def _mount_advice(mount: str) -> str:
    return 'файловая система %s из /etc/fstab не подключена: mount "%s"' % (mount, mount)


def _unmount_data(sandbox) -> str:
    """Диск с данными «отключился»: на месте каталога данных — пустая точка монтирования (root, 0755).

    → каталог, куда убраны настоящие данные.
    """
    sandbox.stop()
    aside = sandbox.data + ".disk"
    os.rename(sandbox.data, aside)
    os.mkdir(sandbox.data, 0o755)
    sandbox.fstab(sandbox.data)
    return aside


@needs_root
def test_update_refuses_data_dir_that_is_an_unmounted_mount_point(sandbox):
    """Каталог данных — точка монтирования без диска (пустой каталог root:root).

    Прежний совет «chown … && chmod 750» вёл к беде: обновление проходило, и новая
    версия заводила пустую базу на месте точки монтирования. Теперь названа
    файловая система, совета про права нет, и после «починки» прав — тоже отказ.
    """
    sandbox.install_old()
    aside = _unmount_data(sandbox)
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert _mount_advice(sandbox.data) in out, _report(proc)
    assert "chown" not in out and "chmod" not in out and "install -d" not in out, _report(proc)
    # один отказ (и итоговая строка): ни «нет базы», ни «конфигурация не принята» следом
    assert len(_marks(proc, "✗")) == 2 and "нет базы" not in out, _report(proc)
    assert "Конфигурация новой версией не проверялась" in out, _report(proc)
    _untouched(sandbox, before, proc, src)

    # администратор «починил» права — каталог пуст и доступен, но диск по-прежнему не подключён
    os.chown(sandbox.data, pwd.getpwnam(USER).pw_uid, grp.getgrnam(GROUP).gr_gid)
    os.chmod(sandbox.data, 0o750)
    before = sandbox.snapshot(src)
    for args in (("--check",), (), ("--force",)):
        proc = sandbox.run("update.sh", src, args=args)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, (args, _report(proc))
        assert _mount_advice(sandbox.data) in out and "каталог пуст" in out, (args, _report(proc))
        assert "chown" not in out and "понижен" not in out, (args, _report(proc))
        _untouched(sandbox, before, proc, src)
    assert os.listdir(sandbox.data) == []                    # новая база на месте точки монтирования не заведена

    # диск подключили — обновление проходит на настоящих данных
    os.rmdir(sandbox.data)
    os.rename(aside, sandbox.data)
    sandbox.fstab()
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert sandbox.db_text().startswith("created-by-" + OLD)


@needs_root
@pytest.mark.parametrize("shape", ["missing", "empty mount point"])
def test_update_never_advises_creating_data_dir_on_a_share_outside_fstab(sandbox, shape):
    """Данные на ресурсе, который подключают не через /etc/fstab (unit .mount, autofs, вручную).

    Ресурс не подключён: каталога данных нет или на его месте пустая закрытая точка
    монтирования. Совет «install -d …» или «chown …» тут вреден: служба, которую systemd
    перезапускает каждые 5 секунд, поднялась бы на пустом каталоге с новой базой.
    """
    sandbox.install_old()
    sandbox.stop()
    aside = sandbox.unplug(sandbox.data)                   # ресурс «не подключён»
    if shape == "empty mount point":
        os.mkdir(sandbox.data, 0o700)                      # root:root, пусто
    sandbox.fstab()                                         # в fstab ресурса нет
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    for args in (("--check",), ()):
        proc = sandbox.run("update.sh", src, args=args)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, (args, _report(proc))
        assert "install -d" not in out and "chown" not in out and "chmod" not in out, (args, _report(proc))
        assert "одключите его" in out and "данные были" in out, (args, _report(proc))
        assert "нашлась в другом месте" not in out, (args, _report(proc))
        assert ("Каталог не создавайте" if shape == "missing" else "Владельца и права не меняйте") in out, \
            (args, _report(proc))
        _untouched(sandbox, before, proc, src)
    # ресурс подключили — обновление проходит на настоящих данных
    if shape == "empty mount point":
        os.rmdir(sandbox.data)
    sandbox.plug(aside, sandbox.data)
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW and sandbox.db_text().startswith("created-by-" + OLD)


@needs_root
def test_install_still_offers_to_create_a_missing_data_dir(sandbox):
    """Новой установке совет создать каталог данных по-прежнему нужен (здесь он к месту)."""
    os.makedirs(os.path.dirname(sandbox.data), exist_ok=True)
    sandbox.fstab()
    run = subprocess.run(["bash", "-c", 'SCRIPT_NAME=install.sh; LOG_PREFIX=t; APP_USER=nobody; APP_GROUP=x; '
                          'DATA_DIR="$1"; DATA_DIR_FROM=""; APP_NAME=mailarchiver; . "$2"; '
                          'check_data_dir', "_", os.path.join(sandbox.base, "no-such-data"),
                          os.path.join(ROOT, "scripts", "common.sh")],
                         capture_output=True, text=True, env=sandbox.env())
    assert run.returncode == 1 and "install -d" in run.stderr, run.stderr


@needs_root
def test_update_names_unmounted_filesystem_above_the_data_directory(sandbox):
    """Точка монтирования — выше каталога данных: ссылка в никуда, каталога нет, каталог под ней не пуст."""
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)                   # data → <mount>/mailarchiver
    real = os.path.join(mount, "mailarchiver")
    sandbox.stop()
    os.rename(mount, mount + ".disk")
    os.mkdir(mount)                                          # пустая точка монтирования
    sandbox.fstab(mount)
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)

    proc = sandbox.run("update.sh", src, args=("--check",))  # ссылка в никуда
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and _mount_advice(mount) in out, _report(proc)
    assert "Каталог данных %s (%s) лежит на неподключённом диске" % (sandbox.data, real) in out, _report(proc)
    assert "mount -a" not in out and "chown" not in out and "install -d" not in out, _report(proc)
    _untouched(sandbox, before, proc, src)

    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=real)     # каталога просто нет
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and _mount_advice(mount) in out, _report(proc)
    assert "сейчас каталога нет" in out and "install -d" not in out, _report(proc)
    _untouched(sandbox, before, proc, src)

    # Под точкой монтирования уже лежат данные (служба успела поработать без диска):
    # это предупреждение, а не отказ, — возможно, строка в fstab просто устарела.
    shutil.copytree(os.path.join(mount + ".disk", "mailarchiver"), real)
    subprocess.run(["chown", "-R", "%s:%s" % (USER, GROUP), real], check=True)
    os.chmod(real, 0o750)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "лежит под точкой монтирования %s из /etc/fstab, а она сейчас не подключена" % mount in out, _report(proc)
    assert 'mount "%s"' % mount in out and len(_marks(proc, "⚠")) == 1 and not _marks(proc, "✗"), _report(proc)

    # строки в fstab нет (диск подключают иначе) — и замечаний нет
    sandbox.fstab()
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0 and not _marks(proc, "⚠"), _report(proc)


@needs_root
def test_mounted_filesystem_from_fstab_is_not_reported(sandbox):
    """Настоящее монтирование: объявленная в fstab и подключённая файловая система замечаний не вызывает."""
    sandbox.install_old()
    sandbox.stop()
    aside = sandbox.data + ".disk"
    os.rename(sandbox.data, aside)
    os.mkdir(sandbox.data, 0o755)
    sandbox.fstab(sandbox.data)
    src = sandbox.sources(NEW, "src_new")
    if subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=0750", "tmpfs", sandbox.data],
                      capture_output=True).returncode != 0:
        pytest.skip("монтировать здесь нельзя (нет права CAP_SYS_ADMIN)")
    try:
        # диск подключён, но пуст: от fstab замечаний нет, о базе — есть
        subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
        proc = sandbox.run("update.sh", src, args=("--check",))
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0 and "нет базы" in out and "не подключена" not in out, _report(proc)
        shutil.copy2(os.path.join(aside, "mailarchiver.db"), sandbox.db)
        subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.db], check=True)
        proc = sandbox.run("update.sh", src, args=("--check",))
        assert proc.returncode == 0 and not _marks(proc, "⚠") and not _marks(proc, "✗"), _report(proc)
    finally:
        subprocess.run(["umount", "-l", sandbox.data], capture_output=True)
    # диск отключили — та же проверка называет его
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode != 0 and _mount_advice(sandbox.data) in proc.stdout + proc.stderr, _report(proc)


@needs_root
def test_update_refuses_when_data_directory_has_no_database(sandbox):
    """Нет файла базы — отказ (не тот каталог?); намеренно пустой каталог пропускает только --force."""
    sandbox.install_old()
    sandbox.stop()
    for name in os.listdir(sandbox.data):
        os.remove(os.path.join(sandbox.data, name))
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    text = ("В каталоге данных нет базы (%s): служба ещё ни разу не запускалась или это не тот каталог "
            "(диск не подключён?)." % sandbox.db)
    for args in (("--check",), ()):
        proc = sandbox.run("update.sh", src, args=args)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, (args, _report(proc))
        assert text in proc.stderr and "sudo bash scripts/update.sh --force" in proc.stderr, (args, _report(proc))
        assert "без её копии" not in out, _report(proc)      # об одной беде — одно сообщение
        assert len(_marks(proc, "✗")) == 2, _report(proc)
        _untouched(sandbox, before, proc, src)

    proc = sandbox.run("update.sh", src, args=("--check", "--force"))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "--force: отказ понижен до предупреждения — " + text in proc.stdout, _report(proc)
    assert "--force: отказов, пониженных до предупреждений, — 1" in proc.stdout, _report(proc)
    assert not _marks(proc, "✗") and len(_marks(proc, "⚠")) == 1, _report(proc)
    _untouched(sandbox, before, proc, src)

    proc = sandbox.run("update.sh", src, args=("--force",))
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert sandbox.db_text() == "opened-by-%s\n" % NEW


def _set_unit_paths(sandbox, value: str) -> None:
    with open(sandbox.unit, encoding="utf-8") as fh:
        unit = fh.read()
    unit, count = re.subn(r"(?m)^ReadWritePaths=.*$", "ReadWritePaths=" + value.replace("\\", r"\\"), unit)
    assert count == 1
    _write(sandbox.unit, unit)


@needs_root
def test_update_finds_data_directory_despite_unit_file_quirks(sandbox):
    """ReadWritePaths со знаком «-» и с несколькими путями; каталог данных берётся из конфигурации."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    config = os.path.join(sandbox.config_dir, "config.yaml")
    accessible = "каталоги над установкой, конфигурация и каталог данных доступны"

    # «-» перед путём (нет каталога — не ошибка): раньше каталогом данных считался «-/…»
    _set_unit_paths(sandbox, "-" + sandbox.data)
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=None)
    assert proc.returncode == 0 and accessible in proc.stdout, _report(proc)
    assert "Каталог данных:" not in proc.stdout and not _marks(proc, "⚠"), _report(proc)

    # несколько путей, каталог данных — не первый: верный называет конфигурация установленной версии
    spool = os.path.join(sandbox.base, "spool")
    os.makedirs(spool)
    _set_unit_paths(sandbox, "+%s -%s" % (spool, sandbox.data))
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=None)
    assert proc.returncode == 0 and accessible in proc.stdout, _report(proc)
    assert ("Каталог данных: %s — из конфигурации %s (в unit-файле первым в ReadWritePaths указан %s)."
            % (sandbox.data, config, spool)) in proc.stdout, _report(proc)
    assert "Места для копии базы хватает" in proc.stdout, _report(proc)

    # Установленная версия ответить не может (старая: у load_config нет параметра create_dirs) —
    # остаётся unit-файл; знак и «/» на конце убраны. Путь к базе неизвестен — предупреждение, как раньше.
    _set_unit_paths(sandbox, "-%s/" % sandbox.data)
    installed = sandbox.site_file("config.py")
    with open(installed, encoding="utf-8") as fh:
        original = fh.read()
    _write(installed, original + "\n\ndef load_config(path=None):\n    return Config(path)\n")
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=None)
    assert proc.returncode == 0 and accessible in proc.stdout, _report(proc)
    assert "Файл базы не найден (путь узнать не удалось" in proc.stdout and "нет базы" not in proc.stdout, \
        _report(proc)
    assert not _marks(proc, "✗"), _report(proc)
    _write(installed, original)

    # и обновление с таким unit проходит; новый unit — уже по шаблону
    proc = sandbox.run("update.sh", src, MA_DATA_DIR=None)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    with open(sandbox.unit, encoding="utf-8") as fh:
        assert "ReadWritePaths=%s\n" % sandbox.data in fh.read()


@needs_root
def test_relative_data_dir_in_config_is_read_the_way_the_service_reads_it(sandbox):
    """paths.data_dir: "data" — служба считает путь от своего рабочего каталога; так же должен и скрипт.

    Иначе каталог данных и база искались бы от корня («/data/…»), и работающая
    установка получала бы отказ «в каталоге данных нет базы».
    """
    sandbox.install_old()
    sandbox.stop()
    inside = os.path.join(sandbox.app_dir, "data")
    os.rename(sandbox.data, inside)
    config = os.path.join(sandbox.config_dir, "config.yaml")
    with open(config, encoding="utf-8") as fh:
        text = fh.read()
    _write(config, text.replace('"%s"' % sandbox.data, '"data"'))
    _set_unit_paths(sandbox, inside)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD, sandbox.journal()
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=None)
    assert proc.returncode == 0 and "Места для копии базы хватает" in proc.stdout, _report(proc)
    assert not _marks(proc, "✗") and not _marks(proc, "⚠"), _report(proc)
    # unit-файл называет другой каталог — каталог данных всё равно найден: по конфигурации
    _set_unit_paths(sandbox, os.path.join(sandbox.base, "elsewhere"))
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=None)
    assert proc.returncode == 0 and "Каталог данных: %s — из конфигурации" % inside in proc.stdout, _report(proc)
    # и обновление проходит: копия базы снята с настоящего файла, новый unit называет настоящий каталог
    proc = sandbox.run("update.sh", src, MA_DATA_DIR=None, MA_KEEP_DB_BACKUP="1")
    assert proc.returncode == 0 and "Копия базы данных" in proc.stdout, _report(proc)
    assert sandbox.wait_version() == NEW
    with open(sandbox.unit, encoding="utf-8") as fh:
        assert "ReadWritePaths=%s\n" % inside in fh.read()
    with open(os.path.join(inside, "mailarchiver.db"), encoding="utf-8") as fh:
        assert "opened-by-" + NEW in fh.read()


@needs_root
def test_update_names_the_disk_that_was_mounted_by_hand(sandbox):
    """Ссылка ведёт в каталог диска, который подключали вручную (в /etc/fstab его нет), а сейчас он не подключён.

    Каталог, куда ведёт ссылка, — пустой каталог корневой файловой системы, в fstab только
    корень и подкачка. Совет «mount -a» тут ничего не даёт: скрипт называет пустой каталог,
    диски, которые не подключены, команду подключения и строку для /etc/fstab.
    """
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    sandbox.stop()
    stash = sandbox.unplug(os.path.join(mount, "mailarchiver"))                   # диск «отключился»
    root_dev = 'NAME="/dev/sda" TYPE="disk" FSTYPE="" MOUNTPOINT="" SIZE="40G" UUID="" LABEL=""'
    _write(os.path.join(sandbox.state, "lsblk"), "\n".join([
        root_dev,
        'NAME="/dev/sda1" TYPE="part" FSTYPE="ext4" MOUNTPOINT="/" SIZE="39G" UUID="4f38-root" LABEL=""',
        'NAME="/dev/sda2" TYPE="part" FSTYPE="swap" MOUNTPOINT="" SIZE="1G" UUID="sw-1" LABEL=""',
        'NAME="/dev/sdb" TYPE="disk" FSTYPE="" MOUNTPOINT="" SIZE="500G" UUID="" LABEL=""',
        'NAME="/dev/sdb1" TYPE="part" FSTYPE="ext4" MOUNTPOINT="" SIZE="500G" UUID="1234-abcd" LABEL="archive"',
        'NAME="/dev/sdc1" TYPE="part" FSTYPE="crypto_LUKS" MOUNTPOINT="" SIZE="1T" UUID="lu-1" LABEL=""',
        'NAME="/dev/loop0" TYPE="loop" FSTYPE="squashfs" MOUNTPOINT="" SIZE="60M" UUID="" LABEL=""',
    ]) + "\n")
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    target = os.path.join(mount, "mailarchiver")
    assert "ссылка на %s, а такого каталога сейчас нет" % target in out, _report(proc)
    assert "Каталог %s есть, но он пуст и к нему ничего не подключено" % mount in out, _report(proc)
    assert "/dev/sdb1 — ext4, 500G, метка «archive», UUID=1234-abcd" in out, _report(proc)
    assert 'mount /dev/sdb1 "%s"; ls -la "%s/"' % (mount, target) in out, _report(proc)
    assert "UUID=1234-abcd %s ext4 defaults,nofail 0 2" % mount in out, _report(proc)
    assert "/dev/sdc1 (crypto_LUKS)" in out, _report(proc)
    assert "/dev/loop0" not in out and "/dev/sda2" not in out and "mount -a" not in out, _report(proc)
    _untouched(sandbox, before, proc, src)

    # открытый зашифрованный раздел о себе не напоминает; дисков не видно — совет про сетевой ресурс
    _write(os.path.join(sandbox.state, "lsblk"),
           root_dev + '\nNAME="/dev/sdc1" TYPE="part" FSTYPE="crypto_LUKS" MOUNTPOINT="" SIZE="1T" UUID="" LABEL=""\n')
    _write(os.path.join(sandbox.state, "lsblk_children"), "/dev/sdc1 /dev/mapper/vault\n")
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert "Неподключённых дисков lsblk не видит" in out and "-prune -o -name mailarchiver.db -print" in out, \
        _report(proc)
    assert "нашлась в другом месте" not in out, _report(proc)
    assert "Подключите её" not in out and "crypto_LUKS" not in out, _report(proc)

    # диск «подключили» — проверка проходит
    sandbox.plug(stash, target)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0, _report(proc)


@needs_root
def test_update_names_zfs_pool_that_is_not_imported(sandbox):
    """Каталог данных — на пуле ZFS, который не импортирован (так было на сервере 19.10.2026).

    /var/lib/mailarchiver → /storage/storage/mailarchiver/; /storage и /storage/storage —
    пустые точки монтирования; в lsblk у sdb1 — zfs_member с меткой storage (имя пула);
    в /etc/fstab ZFS не записывают. Скрипт называет пул и команды zpool import, автоимпорт
    после перезагрузки и дополнение к unit-файлу, чтобы служба ждала ZFS.
    """
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    sandbox.stop()
    stash = sandbox.unplug(os.path.join(mount, "mailarchiver"))                   # пул «экспортирован»
    _write(os.path.join(sandbox.state, "lsblk"), "\n".join([
        'NAME="/dev/sda2" TYPE="part" FSTYPE="ext4" MOUNTPOINT="/" SIZE="465G" UUID="4f38" LABEL=""',
        'NAME="/dev/sdb" TYPE="disk" FSTYPE="" MOUNTPOINT="" SIZE="4T" UUID="" LABEL=""',
        'NAME="/dev/sdb1" TYPE="part" FSTYPE="zfs_member" MOUNTPOINT="" SIZE="4T" UUID="1614" LABEL="storage"',
        'NAME="/dev/sdb9" TYPE="part" FSTYPE="" MOUNTPOINT="" SIZE="8M" UUID="" LABEL=""',
        'NAME="/dev/sr0" TYPE="rom" FSTYPE="iso9660" MOUNTPOINT="" SIZE="3G" UUID="" LABEL="Ubuntu-Server"',
    ]) + "\n")
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    target = os.path.join(mount, "mailarchiver")

    # утилит ZFS нет — так и сказано
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "Пул ZFS «storage» (/dev/sdb1) не импортирован." in out, _report(proc)
    assert "apt install zfsutils-linux" in out, _report(proc)
    assert "так выглядит точка монтирования" in out and "вручную" not in out, _report(proc)
    assert "Неподключённых дисков lsblk не видит" not in out and "/dev/sr0" not in out, _report(proc)
    _untouched(sandbox, before, proc, src)

    # zpool есть, пул не импортирован — команды целиком
    _write(os.path.join(sandbox.bin, "zpool"),
           '#!/bin/sh\n[ "$1" = list ] && cat "$FAKE_SYSTEMD_STATE/zpools" 2>/dev/null\nexit 0\n', 0o755)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert 'zpool import storage; zfs mount -a; ls -la "%s/"' % target in out, _report(proc)
    assert "zpool set cachefile=/etc/zfs/zpool.cache storage" in out, _report(proc)
    assert "systemctl enable zfs-import-cache.service zfs-mount.service zfs.target" in out, _report(proc)
    assert 'mkdir -p "%s.d"' % sandbox.unit in out, _report(proc)
    assert ("printf '[Unit]\\nRequires=zfs-mount.service\\nAfter=zfs-mount.service\\n' > "
            '"%s.d/zfs.conf"; systemctl daemon-reload' % sandbox.unit) in out, _report(proc)

    # пул импортирован (а каталога всё равно нет) — о пуле не говорится
    _write(os.path.join(sandbox.state, "zpools"), "storage\n")
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Пул ZFS" not in out, _report(proc)

    # пул «импортировали» — служба поднимается, обновление проходит
    sandbox.plug(stash, target)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW


@needs_root
def test_update_finds_data_moved_under_a_mistyped_link(sandbox):
    """Каталог данных перенесли, а ссылку сделали с опечаткой: скрипт находит базу и называет команду.

    Служба после переноса работает дальше с открытым каталогом и падает только при
    перезапуске (обновлении). Совет «подключите диск» тут ложный: данные лежат рядом.
    """
    sandbox.install_old()
    sandbox.stop()
    storage = os.path.join(sandbox.base, "storage")
    os.makedirs(os.path.join(storage, "storage"))                 # тот каталог, что в ссылке, есть
    real = os.path.join(storage, "mailarchiver")
    os.rename(sandbox.data, real)
    _write(os.path.join(real, "secret.key"), "k" * 96)
    os.makedirs(os.path.join(real, "mailboxes"), exist_ok=True)
    old_copy = os.path.join(storage, "copy-2025", "mailarchiver")   # давняя копия без ключа
    _write(os.path.join(old_copy, "mailarchiver.db"), "old\n")
    os.utime(os.path.join(old_copy, "mailarchiver.db"), (1735732800, 1735732800))
    os.symlink(os.path.join(storage, "storage", "mailarchiver") + "/", sandbox.data)   # опечатка, «/» на конце
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_crashed() and "No such file or directory" in sandbox.journal()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src, storage)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "нашлась в другом месте — похоже, каталог данных перенесли" in out, _report(proc)
    found = [line.split("]", 1)[1].strip() for line in out.splitlines() if " — база " in line]
    assert len(found) == 2, _report(proc)
    assert found[0].strip().startswith(real + " — база ") and "есть secret.key и mailboxes/" in found[0], found
    assert found[1].strip().startswith(old_copy + " — база 4 Б, изменена 2025-01-01") and "НЕТ secret.key" in found[1]
    assert 'ln -sfn КАТАЛОГ "%s"' % sandbox.data in out, _report(proc)
    assert "Каталог %s не создавайте" % os.path.join(storage, "storage", "mailarchiver") in out, _report(proc)
    assert "не подключён диск" not in out and "mount -a" not in out, _report(proc)
    _untouched(sandbox, before, proc, src, storage)

    # одна находка — команда готова целиком; выполнили её — служба поднимается, обновление проходит
    shutil.rmtree(os.path.dirname(old_copy))
    proc = sandbox.run("update.sh", src, args=("--check",))
    command = 'ln -sfn "%s" "%s"' % (real, sandbox.data)
    assert command in proc.stderr, _report(proc)
    subprocess.run(["bash", "-c", command], check=True)
    assert os.path.realpath(sandbox.data) == real and sandbox.db_text().startswith("created-by-")
    sandbox.systemctl("reset-failed", "mailarchiver")
    sandbox.systemctl("restart", "mailarchiver")
    assert sandbox.wait_version() == OLD
    proc = sandbox.run("update.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW


@needs_root
def test_dangling_link_with_trailing_slash_and_through_other_links(sandbox):
    """MA_DATA_DIR с «/» на конце, ссылка относительная и ведёт через другую ссылку: названа конечная цель."""
    sandbox.install_old()
    sandbox.stop()
    os.rename(sandbox.data, sandbox.data + ".disk")
    final = os.path.join(sandbox.base, "nowhere", "mailarchiver")
    os.symlink(final, os.path.join(sandbox.base, "hop"))
    os.symlink("hop", sandbox.data)                          # относительная: data → hop → (нет)
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",), MA_DATA_DIR=sandbox.data + "/")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert ("Каталог данных %s — ссылка на %s (в самой ссылке записано: hop), а такого каталога сейчас нет."
            % (sandbox.data, final)) in out, _report(proc)
    assert "Нет каталога данных" not in out, _report(proc)
    # данные лежат рядом под другим именем — они найдены и названа команда
    assert "  %s.disk — база " % sandbox.data in out, _report(proc)
    assert 'ln -sfn "%s.disk" "%s"' % (sandbox.data, sandbox.data) in out, _report(proc)
    # Отказ один. Настоящая программа (и учебная) на таком каталоге не проходит и check-config —
    # но второго ✗ «исправьте конфигурацию» быть не должно: конфигурация ни при чём.
    assert "Конфигурация новой версией не проверялась" in out, _report(proc)
    assert "не принимает конфигурацию" not in out and "нет базы" not in out, _report(proc)
    assert len(_marks(proc, "✗")) == 2 and not _marks(proc, "⚠"), _report(proc)
    _untouched(sandbox, before, proc, src)
    # install.sh с тем же путём отказывает так же — до любых действий
    proc = sandbox.run("install.sh", src, MA_DATA_DIR=sandbox.data + "/", MA_INSTALL_DIR=sandbox.install_dir + "2")
    assert proc.returncode != 0 and "ссылка на %s (в самой ссылке записано: hop)" % final in proc.stderr, \
        _report(proc)
    assert not os.path.exists(sandbox.install_dir + "2")


@needs_root
def test_data_directory_access_messages(sandbox):
    """Закрытый каталог данных: названо, что закрыто, и чем исправить — по обе стороны ссылки."""
    sandbox.install_old()
    sandbox.stop()
    src = sandbox.sources(NEW, "src_new")
    owner = "%s:%s" % (USER, GROUP)
    uid, gid = pwd.getpwnam(USER).pw_uid, grp.getgrnam(GROUP).gr_gid

    def check(**extra):
        proc = sandbox.run("update.sh", src, args=("--check",), **extra)
        assert proc.returncode != 0 and len(_marks(proc, "✗")) == 2, _report(proc)
        return proc.stdout + proc.stderr

    # сам каталог данных root:root 0700 — путь назван один раз, совет есть
    os.chown(sandbox.data, 0, 0)
    os.chmod(sandbox.data, 0o700)
    out = check()
    assert "не может войти в каталог данных %s.\n" % sandbox.data in out, out
    assert 'Исправьте: chown %s "%s" && chmod 750 "%s"' % (owner, sandbox.data, sandbox.data) in out, out
    assert "(%s)" % sandbox.data not in out, out
    os.chmod(sandbox.data, 0o755)                            # войти может, писать — нет
    out = check()
    assert "не может писать в каталог данных %s.\n" % sandbox.data in out and "chown " + owner in out, out
    os.chown(sandbox.data, uid, gid)
    os.chmod(sandbox.data, 0o750)

    # закрыт каталог, в котором лежит ссылка на каталог данных
    private = os.path.join(sandbox.base, "private")
    os.mkdir(private, 0o700)
    link = os.path.join(private, "data")
    os.symlink(sandbox.data, link)
    out = check(MA_DATA_DIR=link)
    assert "не может войти в каталог данных %s (%s).\n" % (link, sandbox.data) in out, out
    assert "Закрыт каталог %s (права drwx------ root:" % private in out, out
    assert 'Исправьте: chmod o+x "%s"' % private in out and "chown" not in out, out

    # закрыт каталог на стороне цели ссылки
    os.chmod(private, 0o755)
    vault = os.path.join(sandbox.base, "vault")
    os.mkdir(vault, 0o700)
    os.rename(sandbox.data, os.path.join(vault, "mail"))
    os.symlink(os.path.join(vault, "mail"), sandbox.data)
    out = check()
    assert "не может войти в каталог данных %s (%s).\n" % (sandbox.data, os.path.join(vault, "mail")) in out, out
    assert "Закрыт каталог %s " % vault in out and 'chmod o+x "%s"' % vault in out, out

    # закрыта сама цель ссылки — чинить нужно её
    os.chmod(vault, 0o755)
    os.chown(os.path.join(vault, "mail"), 0, 0)
    out = check()
    assert 'Исправьте: chown %s "%s" && chmod 750 "%s"' % (owner, os.path.join(vault, "mail"),
                                                             os.path.join(vault, "mail")) in out, out

    # каталога нет: откуда взят путь — сказано, а про ReadWritePaths (unit-файла может и не быть) — нет
    absent = os.path.join(sandbox.base, "absent")
    os.remove(sandbox.unit)
    out = check(MA_DATA_DIR=absent)
    assert "Нет каталога данных %s: без него служба не запустится." % absent in out, out
    assert "Путь взят из переменной MA_DATA_DIR." in out and "ReadWritePaths" not in out, out
    # обновление: служба уже установлена — каталог создавать не советуем (см. диск вне fstab)
    assert "install -d" not in out and "Каталог не создавайте" in out, out


@needs_root
@pytest.mark.parametrize("linked", [False, True])
def test_update_leaves_owner_of_import_files_alone(sandbox, linked):
    """Чужие файлы в каталоге данных возвращаются службе — кроме содержимого import/ (их кладёт администратор)."""
    sandbox.install_old()
    real = os.path.join(_move_data_to_storage(sandbox), "mailarchiver") if linked else sandbox.data
    _write(os.path.join(real, "import", "2024", "archive.pst"), "x")
    _write(os.path.join(real, "exports", "made-by-root.zip"), "x")
    uid = pwd.getpwnam(USER).pw_uid
    assert os.stat(os.path.join(real, "import")).st_uid == 0
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert os.stat(os.path.join(real, "import")).st_uid == uid               # сам каталог — за службой
    assert os.stat(os.path.join(real, "import", "2024")).st_uid == 0
    assert os.stat(os.path.join(real, "import", "2024", "archive.pst")).st_uid == 0
    assert os.stat(os.path.join(real, "exports", "made-by-root.zip")).st_uid == uid
    assert os.stat(os.path.join(real, "exports")).st_uid == uid
    assert os.lstat(sandbox.data).st_uid == (0 if linked else uid)          # саму ссылку не трогают


#: Подделка find: обходы в поисках чужого владельца (в аргументах есть -user)
#: записывает вместе с состоянием службы в этот момент; с FAKE_FIND_SLEEP полный
#: обход (с -fprintf) сначала «думает» столько секунд — как на большом архиве;
#: FAKE_FIND_PLAIN=1 — find без расширений GNU (-fprintf, -ignore_readdir_race).
SPY_FIND = r"""#!/bin/sh
case " $* " in
  *" -fprintf "*|*" -ignore_readdir_race "*)
    if [ "${FAKE_FIND_PLAIN:-0}" = "1" ]; then echo "find: unrecognized: -fprintf" >&2; exit 1; fi ;;
esac
case " $* " in
  *" -user "*)
    echo "$(systemctl is-active mailarchiver 2>/dev/null) $PWD $*" >> "$FAKE_SYSTEMD_STATE/owner_finds"
    case " $* " in
      *" -fprintf "*)
        if [ -n "${FAKE_FIND_SLEEP:-}" ]; then
          echo $$ > "$FAKE_SYSTEMD_STATE/owner_find_pid"
          trap 'kill $! 2>/dev/null; exit 143' TERM
          sleep "$FAKE_FIND_SLEEP" &
          wait $!
          trap - TERM
        fi ;;
    esac ;;
esac
exec @FIND@ "$@"
"""


def _spy_find(sandbox) -> None:
    real = shutil.which("find", path="/usr/bin:/bin")
    _write(os.path.join(sandbox.bin, "find"), SPY_FIND.replace("@FIND@", real), 0o755)


def _owner_finds(sandbox) -> list:
    """Обходы каталога данных: (состояние службы, глубина или None — весь каталог).

    find запускается из каталога данных по «.»; проба выражения (-maxdepth 0) не в счёт.
    """
    found, data = [], os.path.realpath(sandbox.data)
    for line in sandbox.calls("owner_finds"):
        words = line.split()
        if words[1:3] != [data, "."]:
            continue                       # другие обходы (normalize_app_perms — по каталогу установки)
        depth = words[words.index("-maxdepth") + 1] if "-maxdepth" in words else None
        if depth != "0":
            found.append((words[0], depth))
    return found


def _foreign_mail(sandbox) -> tuple:
    """Письмо и каталог глубоко в архиве — от root (как после команд, запущенных от root)."""
    folder = os.path.join(sandbox.data, "mailboxes", "user@example.org", "INBOX")
    mail = os.path.join(folder, "1.eml")
    _write(mail, "Subject: x\n\nx\n")
    os.chown(os.path.join(sandbox.data, "mailboxes"), pwd.getpwnam(USER).pw_uid, grp.getgrnam(GROUP).gr_gid)
    return folder, mail


@needs_root
@pytest.mark.parametrize("skip", [False, True])
def test_update_checks_owners_of_the_whole_archive_before_stopping_the_service(sandbox, skip):
    """Весь архив обходится, пока служба работает; после остановки — только верхний уровень.

    На сервере с сотнями тысяч писем обход шёл после остановки службы, без
    единой строки на экране: обновление «висело» после «Конфигурация корректна»,
    а веб-интерфейс не открывался.
    """
    sandbox.install_old()
    _spy_find(sandbox)
    folder, mail = _foreign_mail(sandbox)
    extra = {"MA_SKIP_OWNER_SCAN": "1"} if skip else {}
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_PROGRESS_EVERY="1", FAKE_FIND_SLEEP="3",
                       **extra)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    out = proc.stdout
    uid = pwd.getpwnam(USER).pw_uid
    if skip:
        assert _owner_finds(sandbox) == [("inactive", "1")], sandbox.calls("owner_finds")
        assert "пропущена (MA_SKIP_OWNER_SCAN=1)" in out, out
        assert os.stat(mail).st_uid == 0 and os.stat(folder).st_uid == 0
        return
    assert _owner_finds(sandbox) == [("active", None), ("inactive", "1")], sandbox.calls("owner_finds")
    scan = out.index("Проверка владельца файлов в каталоге данных (служба пока работает")
    assert scan < out.index("Останавливаю службу"), out
    # обход сообщает о ходе работы и итоге
    assert "…проверено файлов:" in out[scan:out.index("Останавливаю службу")], out
    assert re.search(r"Владелец возвращён службе у файлов: 3 из \d+ ", out), out
    assert os.stat(mail).st_uid == uid and os.stat(folder).st_uid == uid
    assert os.stat(os.path.dirname(folder)).st_uid == uid


@needs_root
def test_ctrl_c_during_owner_scan_stops_it_and_leaves_service_running(sandbox):
    """Ctrl-C во время обхода: обход остановлен (фоновое задание само Ctrl-C не получает), служба не тронута."""
    import signal
    if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
        pytest.skip("SIGINT игнорируется у самого теста: скрипт поставить на него ловушку не сможет")
    sandbox.install_old()
    _spy_find(sandbox)
    _foreign_mail(sandbox)
    sandbox.forget_calls()
    proc = sandbox.start("update.sh", sandbox.sources(NEW, "src_new"), FAKE_FIND_SLEEP="60")
    marker = os.path.join(sandbox.state, "owner_find_pid")
    end = time.time() + 60
    while time.time() < end and not os.path.exists(marker):
        time.sleep(0.05)
    assert os.path.exists(marker), proc.communicate(timeout=60)[0]
    time.sleep(0.3)
    with open(marker, encoding="utf-8") as fh:
        pid = int(fh.read())
    os.killpg(proc.pid, signal.SIGINT)         # как Ctrl-C в терминале: сигнал всей группе
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 130, out
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "Откат" not in out, out
    assert sandbox.changed_state() == [], sandbox.calls()
    assert sandbox.wait_version() == OLD


@needs_root
def test_owner_fix_without_gnu_find(sandbox):
    """find без расширений GNU (busybox): владельца исправляет тот же обход, только без отчёта о ходе."""
    sandbox.install_old()
    _spy_find(sandbox)
    folder, mail = _foreign_mail(sandbox)
    _write(os.path.join(sandbox.data, "import", "2024", "archive.pst"), "x")
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), FAKE_FIND_PLAIN="1")
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert _owner_finds(sandbox) == [("active", None), ("inactive", "1")], sandbox.calls("owner_finds")
    uid = pwd.getpwnam(USER).pw_uid
    assert os.stat(mail).st_uid == uid and os.stat(folder).st_uid == uid
    assert os.stat(os.path.join(sandbox.data, "import", "2024", "archive.pst")).st_uid == 0


#: Подделка chown: файлы *.locked владельца не меняют (как неизменяемые, chattr +i),
#: *.gone «исчезли» между проверкой и chown (их убрала работающая служба).
FAKE_CHOWN = r"""#!@PYTHON@
import subprocess, sys
args = sys.argv[1:]
opts = [a for a in args if a.startswith("-")]
rest = [a for a in args if not a.startswith("-")]
owner, files, keep, rc = rest[0], rest[1:], [], 0
for name in files:
    if name.endswith(".locked"):
        print("chown: changing ownership of '%s': Operation not permitted" % name, file=sys.stderr)
        rc = 1
    elif name.endswith(".gone"):
        print("chown: cannot access '%s': No such file or directory" % name, file=sys.stderr)
        rc = 1
    else:
        keep.append(name)
if keep:
    rc = subprocess.run(["@CHOWN@", *opts, owner, *keep]).returncode or rc
sys.exit(rc)
"""


@needs_root
def test_owner_fix_failures_are_warnings_not_a_failed_update(sandbox):
    """chown отказал у части файлов — предупреждение с их именами, а не откат после остановки службы.

    Исчезнувший между проверкой и chown файл (его убрала служба) — вовсе не ошибка.
    """
    sandbox.install_old()
    real = shutil.which("chown", path="/usr/bin:/bin")
    _write(os.path.join(sandbox.bin, "chown"), FAKE_CHOWN.replace("@PYTHON@", PYTHON).replace("@CHOWN@", real),
           0o755)
    folder, mail = _foreign_mail(sandbox)
    _write(os.path.join(sandbox.data, "stuck.locked"), "x")              # верхний уровень: оба прохода
    _write(os.path.join(sandbox.data, "exports", "old.gone"), "x")
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert out.count("Владельца части файлов в ") == 2 and "stuck.locked" in out, out
    assert "old.gone" not in out, out
    assert "исправить удалось не все" in out and "Владелец возвращён службе" not in out, out
    uid = pwd.getpwnam(USER).pw_uid
    assert os.stat(mail).st_uid == uid and os.stat(folder).st_uid == uid


@needs_root
def test_import_is_left_alone_when_data_path_has_pattern_characters(sandbox):
    """Знаки [ ] * ? в пути каталога данных: содержимое import/ всё равно не трогается."""
    sandbox.install_old()
    sandbox.stop()
    mount = os.path.join(sandbox.base, "disk[1]*?")
    os.makedirs(mount)
    real = os.path.join(mount, "mailarchiver")
    os.rename(sandbox.data, real)
    os.symlink(real, sandbox.data)
    sandbox.systemctl("start", "mailarchiver")
    assert sandbox.wait_version() == OLD, sandbox.journal()
    _write(os.path.join(real, "import", "2024", "archive.pst"), "x")
    _write(os.path.join(real, "exports", "made-by-root.zip"), "x")
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    uid = pwd.getpwnam(USER).pw_uid
    assert os.stat(os.path.join(real, "import")).st_uid == uid
    assert os.stat(os.path.join(real, "import", "2024", "archive.pst")).st_uid == 0
    assert os.stat(os.path.join(real, "exports", "made-by-root.zip")).st_uid == uid


@needs_root
def test_reinstall_checks_owners_before_stopping_the_service(sandbox):
    """Установка поверх: весь каталог данных — до остановки службы, после — только верхний уровень."""
    sandbox.install_old()
    _spy_find(sandbox)
    folder, mail = _foreign_mail(sandbox)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert _owner_finds(sandbox) == [("active", None), ("inactive", "1")], sandbox.calls("owner_finds")
    out = proc.stdout
    assert out.index("Проверка владельца файлов в каталоге данных (служба пока работает") \
        < out.index("Обнаружена существующая установка"), out
    assert os.stat(mail).st_uid == pwd.getpwnam(USER).pw_uid


# ---------------------------------------------------------------------------
#  status=226/NAMESPACE: что говорит журнал
# ---------------------------------------------------------------------------
def _namespace_hint(sandbox, journal: str, **extra: str) -> str:
    """Совет service_namespace_hint для данного текста журнала (common.sh, без установки и systemd)."""
    script = ('set -Eeuo pipefail; LOG_PREFIX="[t]"; RED=; GRN=; YLW=; BLU=; NC=; APP_NAME=mailarchiver; '
              'APP_USER="$MA_USER"; APP_GROUP="$MA_GROUP"; UNIT_FILE="$MA_UNIT_FILE"; DATA_DIR="$MA_DATA_DIR"; '
              'SCRIPT_NAME=update.sh; . "$1"; service_namespace_hint "$2"')
    proc = subprocess.run(["bash", "-c", script, "_", os.path.join(ROOT, "scripts", "common.sh"), journal],
                          env=sandbox.env(**extra), capture_output=True, text=True, cwd="/")
    assert proc.returncode == 0, _report(proc)
    return proc.stdout + proc.stderr


@needs_root
def test_namespace_hint_follows_the_reason_in_the_journal(sandbox):
    """Причина отказа 226 — по тексту systemd; совет про MA_RELAX_SANDBOX — контейнеру или при «Permission denied»."""
    os.makedirs(sandbox.data)
    _write(sandbox.db, "x")
    subprocess.run(["chown", "-R", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
    head = "окт 10 12:00:00 host systemd[1]: mailarchiver.service: "

    def journal(first, reason):
        lines = [head + "Failed to set up mount namespacing: " + first] if first else []
        return "\n".join(lines + [head + "Failed at step NAMESPACE spawning /opt/venv/bin/mailarchiver: " + reason])

    gone = os.path.join(sandbox.base, "gone", "spool")
    # путь назван: с «/./» и изнутри собираемого окружения
    for shown in ("/." + gone + "/", "/run/systemd/unit-root" + gone):
        out = _namespace_hint(sandbox, journal(shown + ": No such file or directory", "No such file or directory"))
        assert "systemd не нашёл каталог %s — " % gone in out, out
        assert "Каталог данных %s на месте: сравните каталоги из ReadWritePaths" % sandbox.data in out, out
        assert "MA_RELAX_SANDBOX" not in out and "контейнер" not in out, out
    # …и он лежит на файловой системе, которая не подключена
    sandbox.fstab(os.path.dirname(gone))
    out = _namespace_hint(sandbox, journal("/." + gone + ": No such file or directory", "No such file or directory"))
    assert "Ф" + _mount_advice(os.path.dirname(gone))[1:] in out, out
    sandbox.fstab()

    # systemd до 246 пути не называет: это по-прежнему «нет каталога», а не «контейнер»
    old = journal("No such file or directory", "No such file or directory")
    for text in (old, journal("", "No such file or directory")):
        out = _namespace_hint(sandbox, text)
        assert "systemd не нашёл один из каталогов, нужных службе (ReadWritePaths)" in out, out
        assert "сравните каталоги из ReadWritePaths (systemctl cat mailarchiver)" in out, out
        assert "MA_RELAX_SANDBOX" not in out and "контейнер" not in out and "nesting" not in out, out
    assert "MA_RELAX_SANDBOX=1" in _namespace_hint(sandbox, old, FAKE_VIRT="lxc")     # в контейнере — возможно и это

    # «Permission denied» — пространство имён создать не дали: совет про контейнер, где бы ни запускали
    out = _namespace_hint(sandbox, journal("/run/systemd/unit-root/proc: Permission denied", "Permission denied"))
    assert "sudo MA_RELAX_SANDBOX=1 bash scripts/update.sh" in out and "nesting" in out, out
    assert "systemd не нашёл" not in out, out

    # иная причина: называются обе возможности; совет про MA_RELAX_SANDBOX — только контейнеру
    other = journal("Invalid argument", "Invalid argument")
    out = _namespace_hint(sandbox, other)
    assert "Причин у такого отказа обычно две" in out and "ReadWritePaths" in out, out
    assert "MA_RELAX_SANDBOX" not in out, out
    out = _namespace_hint(sandbox, other, FAKE_VIRT="lxc")
    assert "Этот сервер — контейнер (lxc)" in out and "MA_RELAX_SANDBOX=1" in out, out

    # «нет» каталога, который на самом деле есть (/dev внутри окружения), — не пропавший каталог
    out = _namespace_hint(sandbox, journal("/run/systemd/unit-root/dev: No such file or directory",
                                           "No such file or directory"))
    assert "systemd не нашёл" not in out and "Причин у такого отказа обычно две" in out, out

    # каталог данных — ссылка в никуда: об этом и сказано, какой бы ни была строка журнала
    shutil.rmtree(sandbox.data)
    os.symlink(os.path.join(sandbox.base, "no-disk", "mail"), sandbox.data)
    for text in (old, other):
        out = _namespace_hint(sandbox, text)
        assert "ссылка на %s" % os.path.join(sandbox.base, "no-disk", "mail") in out, out
        assert "MA_RELAX_SANDBOX" not in out, out


@needs_root
def test_old_systemd_message_without_path_is_not_taken_for_a_container(sandbox):
    """systemd до 246 (RHEL 8, Debian 10): «Failed to set up mount namespacing: No such file or directory».

    Пути в строке нет. На обычном сервере (не в контейнере) совет «повторите с
    MA_RELAX_SANDBOX=1» был бы ложным следом: не хватает каталога.
    """
    sandbox.install_old()
    # администратор добавил службе ещё один каталог (своим дополнением), а его нет
    _write(os.path.join(sandbox.unit + ".d", "10-paths.conf"),
           "[Service]\nReadWritePaths=%s %s\n" % (sandbox.data, os.path.join(sandbox.base, "spool")))
    sandbox.systemctl("restart", "mailarchiver")
    assert sandbox.wait_crashed(), sandbox.journal()
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), FAKE_SYSTEMD_STYLE="old")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "status=226/NAMESPACE" in out, _report(proc)
    assert "Failed to set up mount namespacing: No such file or directory" in out, _report(proc)
    assert "systemd не нашёл один из каталогов, нужных службе (ReadWritePaths)" in out, _report(proc)
    assert "Каталог данных %s на месте: сравните каталоги из ReadWritePaths" % sandbox.data in out, _report(proc)
    assert "MA_RELAX_SANDBOX" not in out and "nesting" not in out and "контейнер" not in out, _report(proc)
    assert "Откат" in out and "До обновления служба тоже не работала" in out, _report(proc)


# ---------------------------------------------------------------------------
#  Состояние службы до обновления
# ---------------------------------------------------------------------------
@needs_root
def test_rollback_does_not_blame_a_service_the_admin_had_stopped(sandbox):
    """Службу остановил администратор — исправную. «До обновления она тоже не работала» здесь неправда."""
    sandbox.install_old()
    sandbox.stop()
    sandbox.behaviour("crash:*")
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "служба НЕ поднялась" in out, _report(proc)
    assert "Служба не работает уже сейчас" not in out, _report(proc)
    assert "До обновления служба тоже не работала" not in out, _report(proc)


@needs_root
def test_check_notices_crash_loop_between_restarts(sandbox):
    """Служба в цикле перезапусков числится active/running, пока идёт попытка запуска, — её выдаёт счётчик."""
    sandbox.install_old()
    sandbox.behaviour("hang:*")                              # запускается, но веб-интерфейс не поднимает
    sandbox.systemctl("restart", "mailarchiver")
    _write(os.path.join(sandbox.state, "nrestarts"), "3")
    assert sandbox.active()
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert ("Служба не работает уже сейчас, до обновления (состояние active/running, но systemd перезапускал её "
            "уже 3 раз(а), а адрес проверки http://127.0.0.1:%d/health не отвечает)." % sandbox.port) in out, \
        _report(proc)
    # одно предупреждение, а не два об одном и том же
    assert "Служба работает, но адрес проверки не отвечает" not in out and len(_marks(proc, "⚠")) == 1, \
        _report(proc)
    # тот же счётчик у службы, которая отвечает, — не повод (см. test_check_mode_reports_and_changes_nothing)


# ---------------------------------------------------------------------------
#  Проверка перед обновлением: --check, --force и что именно проверяется
# ---------------------------------------------------------------------------
@needs_root
def test_check_mode_is_requested_only_by_the_flag(sandbox):
    """Переменной MA_CHECK_ONLY больше нет: с «MA_CHECK_ONLY=1 sudo …» sudo её отбрасывал, и шло обновление.

    Проверяется, что переменная не читается вовсе. Обновление при этом случиться не
    может: «свободного места» заведомо мало, и скрипт остановится на проверке.
    """
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, MA_CHECK_ONLY="1", FAKE_DF_FREE_KB="1000")
    assert proc.returncode != 0 and "Мало места" in proc.stderr, _report(proc)
    assert proc.stdout.splitlines()[0] == "[MailArchiver update] Проверка перед обновлением…", _report(proc)
    assert "служба не останавливается" not in proc.stdout, _report(proc)
    _untouched(sandbox, before, proc, src)
    proc = sandbox.run("update.sh", src, args=("--check",), FAKE_DF_FREE_KB="1000")
    assert proc.stdout.splitlines()[0].endswith(
        "Проверка перед обновлением (служба не останавливается, на диске ничего не меняется)…"), _report(proc)
    for name in sorted(os.listdir(os.path.join(ROOT, "scripts"))):
        if name.endswith(".sh"):
            with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as fh:
                assert "MA_CHECK_ONLY" not in fh.read(), name


@needs_root
def test_force_downgrades_only_what_is_checked_again_after_installation(sandbox):
    sandbox.install_old()
    rejecting = sandbox.sources(NEW, "src_rejecting", rejects_config=True)
    unparsable = sandbox.sources(NEW, "src_unparsable", syntax_error=True)
    too_new = sandbox.sources(NEW, "src_too_new", requires_python=">=3.99")
    sandbox.forget_calls()
    trees = (rejecting, unparsable, too_new)
    before = sandbox.snapshot(*trees)
    for src, text in ((rejecting, "Новая версия не принимает конфигурацию"),
                      (unparsable, "Часть модулей новой версии не подходит Python окружения"),
                      (too_new, "не подходит новой версии: ей нужен Python >=3.99")):
        proc = sandbox.run("update.sh", src, args=("--check", "--force"))
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, _report(proc)
        marked = [line for line in _marks(proc, "⚠") if "--force: отказ понижен до предупреждения — " in line]
        assert len(marked) == 1 and text in marked[0], _report(proc)
        assert "--force: отказов, пониженных до предупреждений, — 1" in out and not _marks(proc, "✗"), _report(proc)
        assert "Проверка пройдена, предупреждений — 1 (отмечены знаком ⚠). Обновление: sudo bash " \
               "scripts/update.sh --force" in out, _report(proc)
        _untouched(sandbox, before, proc, *trees)

    # то, что после установки не перепроверяется, --force не обходит
    proc = sandbox.run("update.sh", too_new, args=("--force",), FAKE_DF_FREE_KB="1000")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Мало места" in out and "ошибок — 1, предупреждений — 1" in out, _report(proc)
    assert len(_marks(proc, "✗")) == 2, _report(proc)
    _untouched(sandbox, before, proc, *trees)
    proc = sandbox.run("update.sh", too_new, args=("--check", "--force"), MA_USER="no-such-user-for-tests")
    assert proc.returncode != 0 and "нет пользователя службы" in proc.stderr, _report(proc)
    _untouched(sandbox, before, proc, *trees)

    # настоящее обновление с --force: конфигурация проверяется снова — уже установленной версией — и откат
    proc = sandbox.run("update.sh", rejecting, args=("--force",))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "Новая версия не приняла конфигурацию" in out and "Откат" in out and "Служба работает" in out, \
        _report(proc)
    assert sandbox.wait_version() == OLD


@needs_root
def test_check_config_that_could_not_run_is_not_a_rejected_configuration(sandbox):
    """Новой версии нужна библиотека, которой ещё нет: check-config из исходников не состоялся — это не ✗."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new", needs_library=True)
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "Проверить конфигурацию новой версией до установки не удалось" in out, _report(proc)
    assert "No module named 'library_added_in_new_version'" in out, _report(proc)
    assert "не принимает конфигурацию" not in out, _report(proc)
    assert not _marks(proc, "✗") and len(_marks(proc, "⚠")) == 1, _report(proc)
    _untouched(sandbox, before, proc, src)


@needs_root
def test_requires_python_is_read_from_the_project_table_only(sandbox):
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    pyproject = os.path.join(src, "pyproject.toml")
    project = '[project]\nname = "mailarchiver"\nversion = "%s"\n' % NEW
    _write(pyproject, '[tool.checker]\nrequires-python = ">=3.99"\n\n' + project
           + 'requires-python = ">=3.8"   # наш\n\n[tool.other]\nrequires-python = ">=4.5"\n')
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0 and "подходит новой версии (нужен >=3.8)" in proc.stdout, _report(proc)
    _write(pyproject, project + '\n[tool.checker]\nrequires-python = ">=3.99"\n')
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0 and "нет requires-python" in proc.stdout, _report(proc)
    assert "3.99" not in proc.stdout + proc.stderr, _report(proc)


@needs_root
def test_update_refuses_sources_inside_the_installation(sandbox):
    """Исходники распакованы внутрь каталога установки: rsync --delete стёр бы их посреди копирования."""
    sandbox.install_old()
    inside = os.path.join(sandbox.app_dir, "new", "mail_backup")
    shutil.copytree(sandbox.sources(NEW, "src_new"), inside)
    sandbox.forget_calls()
    before = sandbox.snapshot()
    for args in (("--check",), (), ("--force",)):
        proc = sandbox.run("update.sh", inside, args=args)
        assert proc.returncode != 0, (args, _report(proc))
        assert "Новые исходники (%s) лежат внутри каталога установки %s" % (inside, sandbox.app_dir) \
            in proc.stderr, (args, _report(proc))
        assert "Распакуйте архив новой версии в другой каталог" in proc.stderr, _report(proc)
        _untouched(sandbox, before, proc)
    assert sandbox.wait_version() == OLD


@needs_root
def test_update_refuses_when_pip_of_the_environment_does_not_start(sandbox):
    """Файл pip на месте и исполняемый, но его интерпретатора больше нет (Python в системе обновили)."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    pip = os.path.join(sandbox.venv, "bin", "pip")
    with open(pip, encoding="utf-8") as fh:
        body = fh.read().split("\n", 1)[1]
    _write(pip, "#!/usr/bin/python-removed-by-upgrade\n" + body, 0o755)
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    for args in (("--check",), ()):
        proc = sandbox.run("update.sh", src, args=args)
        assert proc.returncode != 0, _report(proc)
        assert "pip окружения не запускается (%s --version)" % pip in proc.stderr, _report(proc)
        assert "sudo bash scripts/install.sh" in proc.stderr, _report(proc)
        _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


@needs_root
def test_check_warns_when_package_index_does_not_answer(sandbox):
    """Обновление из сети, а индекс пакетов недоступен: предупреждение заранее, а не отказ pip после остановки."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    started = time.time()
    proc = sandbox.run("update.sh", src, args=("--check",), MA_INDEX_URL="http://user:secret@127.0.0.1:9/simple/")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "Индекс пакетов http://127.0.0.1:9/simple/ не отвечает" in out, _report(proc)
    assert "secret" not in out, _report(proc)                # пароль из адреса в вывод не попадает
    assert "make_offline_bundle.sh" in out and "MA_INDEX_URL=" in out, _report(proc)
    assert len(_marks(proc, "⚠")) == 1 and not _marks(proc, "✗"), _report(proc)
    assert time.time() - started < 30
    _untouched(sandbox, before, proc, src)

    # адрес из настроек pip (pip.conf), если переменная не задана
    hits = _INDEX.get("hits", 0)
    proc = sandbox.run("update.sh", src, args=("--check",), MA_INDEX_URL=None, FAKE_PIP_INDEX_URL=_index_url())
    assert proc.returncode == 0 and "Индекс пакетов отвечает: " + _index_url() in proc.stdout, _report(proc)
    assert _INDEX.get("hits", 0) > hits and not _marks(proc, "⚠"), _report(proc)
    assert "--disable-pip-version-check config list" in sandbox.calls("pip_calls")
    _untouched(sandbox, before, proc, src)

    # обновление без сети индекс не опрашивает
    offline = sandbox.sources(NEW, "src_offline", wheels=True)
    proc = sandbox.run("update.sh", offline, args=("--check",), MA_INDEX_URL="http://127.0.0.1:9/simple/")
    assert proc.returncode == 0 and "Индекс пакетов" not in proc.stdout + proc.stderr, _report(proc)
    assert not _marks(proc, "⚠"), _report(proc)


@needs_root
def test_offline_wheels_are_verified_by_pip_dry_run(sandbox):
    """Пакеты, которые pip не установит (архив под другой Python), — отказ на проверке, а не после остановки."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new", wheels=True)
    wheels = os.path.join(src, "wheels")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    proc = sandbox.run("update.sh", src, args=("--check",))
    assert proc.returncode == 0, _report(proc)
    assert "pip проверил пробной установкой, что в нём есть всё нужное" in proc.stdout, _report(proc)
    dry = [call for call in sandbox.calls("pip_calls") if call.startswith("install --dry-run ")]
    assert len(dry) == 1 and "--no-index --find-links %s " % wheels in dry[0], dry
    assert dry[0].endswith("-r %s/requirements.txt mailarchiver==%s" % (src, NEW)), dry
    _untouched(sandbox, before, proc, src)

    for args in (("--check",), (), ("--force",)):
        proc = sandbox.run("update.sh", src, args=args, FAKE_PIP_DRY_RUN_FAIL="1")
        assert proc.returncode != 0, (args, _report(proc))
        assert "Обновление без сети не пройдёт: pip не может установить пакеты из " + wheels in proc.stderr, \
            (args, _report(proc))
        assert "No matching distribution found for cryptography>=44.0" in proc.stderr, _report(proc)
        assert "bash scripts/make_offline_bundle.sh" in proc.stderr, _report(proc)
        assert "понижен" not in proc.stdout, _report(proc)   # --force этого не обходит
        _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


# ---------------------------------------------------------------------------
#  install.sh
# ---------------------------------------------------------------------------
@needs_root
def test_install_from_archive_with_restricted_permissions(sandbox):
    src = sandbox.sources(NEW, "src_new")
    _restrict(os.path.dirname(src))
    proc = sandbox.run("install.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert sandbox.wait_version() == NEW
    assert stat.S_IMODE(os.stat(sandbox.app_dir).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(sandbox.install_dir).st_mode) == 0o755
    assert sandbox.as_user("sh", "-c", 'cd "$1" && cat VERSION >/dev/null', "_", sandbox.app_dir) == 0
    assert os.stat(sandbox.data).st_uid == pwd.getpwnam(USER).pw_uid


@needs_root
def test_install_shows_reason_when_service_does_not_start(sandbox):
    os.makedirs(sandbox.data)
    subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "Application startup failed" in out, _report(proc)
    # чистая установка откатывается, данные и конфигурация остаются
    assert not os.path.exists(sandbox.install_dir)
    assert os.path.exists(sandbox.data)
    assert not sandbox.active()


@needs_root
def test_install_in_container_without_namespaces(sandbox):
    """226/NAMESPACE при установке: совет называет install.sh; с MA_RELAX_SANDBOX=1 служба запускается."""
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("install.sh", src, FAKE_NAMESPACE_FAIL="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "status=226/NAMESPACE" in out and "sudo MA_RELAX_SANDBOX=1 bash scripts/install.sh" in out, _report(proc)
    assert not os.path.exists(sandbox.install_dir) and not os.path.exists(sandbox.unit)
    assert not os.path.exists(os.path.dirname(sandbox.dropin))

    proc = sandbox.run("install.sh", src, FAKE_NAMESPACE_FAIL="1", MA_RELAX_SANDBOX="1")
    assert proc.returncode == 0, _report(proc)
    assert "защита службы ослаблена дополнением " + sandbox.dropin in proc.stdout, _report(proc)
    assert stat.S_IMODE(os.stat(sandbox.dropin).st_mode) == 0o644
    assert _dropin_keys(open(sandbox.dropin, encoding="utf-8").read())["PrivateDevices"] == "no"
    assert sandbox.wait_version() == NEW


@needs_root
def test_install_rollback_removes_created_dropin(sandbox):
    os.makedirs(sandbox.data)
    subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"), MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "защита службы ослаблена дополнением" in out and "созданное этим запуском, убрано" in out, _report(proc)
    assert not os.path.exists(os.path.dirname(sandbox.dropin))
    assert not os.path.exists(sandbox.unit) and not os.path.exists(sandbox.install_dir)
    calls = sandbox.calls()
    assert calls[-1] == "daemon-reload", calls              # после удаления unit и дополнения


@needs_root
def test_install_offline_with_ma_wheels(sandbox):
    """Установка без сети: каталог пакетов задан MA_WHEELS (здесь — относительным путём)."""
    src = sandbox.sources(NEW, "src_new")
    wheels = os.path.join(sandbox.base, "pkgs")
    make_wheel(wheels, NEW)
    proc = sandbox.run("install.sh", src, MA_WHEELS=os.path.join("..", "..", "pkgs"))
    assert proc.returncode == 0, _report(proc)
    assert "Установка без сети: пакеты (1 шт.) будут взяты из " + wheels in proc.stdout, _report(proc)
    # python3 с venv и ensurepip уже есть: на сервере без интернета менеджер пакетов только ждал бы репозитории
    assert "python3 с модулями venv и ensurepip уже есть — менеджер пакетов не вызывается" in proc.stdout, \
        _report(proc)
    assert sandbox.calls("pkg_calls") == [], sandbox.calls("pkg_calls")
    installs = _installs(sandbox)
    assert installs and all("--no-index --find-links %s " % wheels in call for call in installs), installs
    assert not [call for call in installs if "--upgrade pip" in call or "setuptools" in call], installs
    package = next(i for i, call in enumerate(installs) if "mailarchiver==" + NEW in call)
    reqs = next(i for i, call in enumerate(installs) if "-r %s/requirements.txt" % sandbox.app_dir in call)
    assert reqs < package and "--no-deps" in installs[package], installs
    assert os.path.exists(sandbox.site_file("from_wheel.py"))
    assert not os.path.exists(os.path.join(sandbox.app_dir, "mailarchiver", "from_wheel.py"))
    assert sandbox.wait_version() == NEW


@needs_root
def test_install_offline_refuses_unusable_wheels_before_touching_anything(sandbox):
    src = sandbox.sources(NEW, "src_new")
    stale = os.path.join(sandbox.base, "stale")
    make_wheel(stale, "9.9.8")
    nowhere = os.path.join(sandbox.base, "no-such-dir")
    for wheels, message in ((nowhere, "Каталог пакетов MA_WHEELS=%s не найден" % nowhere),
                            (stale, "нет пакета приложения mailarchiver-%s-*.whl" % NEW)):
        proc = sandbox.run("install.sh", src, MA_WHEELS=wheels)
        assert proc.returncode != 0 and message in proc.stderr, _report(proc)
        assert "откат" not in (proc.stdout + proc.stderr).lower(), _report(proc)
        # ничего не создано и не вызвано: ни каталогов, ни пользователя, ни пакетов, ни службы
        for path in (sandbox.install_dir, sandbox.data, sandbox.unit, sandbox.wrapper):
            assert not os.path.exists(path), path
        assert sandbox.calls() == [] and sandbox.calls("pip_calls") == [] and sandbox.calls("pkg_calls") == []


@needs_root
def test_install_offline_survives_unreachable_package_repository(sandbox):
    """Репозиторий дистрибутива недоступен: при установке из сети это отказ, без сети к нему и не обращаются."""
    online = sandbox.sources(NEW, "src_online")
    proc = sandbox.run("install.sh", online, FAKE_PKG_EXIT="100")
    assert proc.returncode != 0 and "Не удалось установить пакеты через apt" in proc.stderr, _report(proc)
    assert not os.path.exists(sandbox.install_dir)
    assert [call for call in sandbox.calls("pkg_calls") if call.startswith("apt-get update")]

    sandbox.forget_calls()
    offline = sandbox.sources(NEW, "src_offline", wheels=True)
    proc = sandbox.run("install.sh", offline, FAKE_PKG_EXIT="100")
    assert proc.returncode == 0, _report(proc)
    assert "менеджер пакетов не вызывается" in proc.stdout and "Не удалось установить пакеты" not in proc.stdout, \
        _report(proc)
    assert sandbox.calls("pkg_calls") == [], sandbox.calls("pkg_calls")
    assert os.path.exists(sandbox.site_file("from_wheel.py"))
    assert sandbox.wait_version() == NEW


@needs_root
def test_offline_install_and_update_with_real_pip(sandbox):
    """Те же команды — настоящему pip: окружение создаёт python -m venv, пакеты берутся из каталога, сети нет.

    Подделка pip проверяет, что скрипты вызывают; этот тест — что настоящий pip принимает именно такие
    параметры (--no-index --find-links … --no-deps --upgrade --force-reinstall mailarchiver==X) и
    ставит библиотеки из requirements.txt, не обращаясь к индексу.
    """
    if subprocess.run([PYTHON, "-c", "import ensurepip, venv"], capture_output=True).returncode != 0:
        pytest.skip("нет ensurepip: настоящий pip в окружение не поставить")
    env = {"FAKE_REAL_VENV": "1", "PIP_CONFIG_FILE": os.devnull, "PIP_NO_CACHE_DIR": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    # библиотеки, которые проверяет check_package: имя пакета → имя модуля
    libs = {"fastapi": "fastapi", "uvicorn": "uvicorn", "jinja2": "jinja2", "PyYAML": "yaml",
            "APScheduler": "apscheduler", "IMAPClient": "imapclient", "cryptography": "cryptography"}
    requirements = "".join("%s>=1\n" % name for name in libs)

    src = sandbox.sources(OLD, "src_first", wheels=True, requirements=requirements)
    for name, module in libs.items():
        build_wheel(os.path.join(src, "wheels"), name.lower(), "1.0", {module + ".py": ""})
    proc = sandbox.run("install.sh", src, **env)
    assert proc.returncode == 0, _report(proc)
    pip = subprocess.run([os.path.join(sandbox.venv, "bin", "pip"), "--version"], capture_output=True, text=True)
    assert pip.returncode == 0 and pip.stdout.startswith("pip "), pip.stdout       # не подделка
    assert sandbox.calls("pip_calls") == []
    assert os.path.exists(sandbox.site_file("from_wheel.py"))
    assert sandbox.wait_version() == OLD

    # Новая версия требует ещё одну библиотеку. В каталоге — только то, чего в окружении нет:
    # уже установленные библиотеки pip не ищет.
    newer = sandbox.sources(NEW, "src_second", requirements=requirements + "newlib>=2,<3\n")
    wheels = os.path.join(newer, "wheels")
    make_wheel(wheels, NEW, requires=tuple("%s>=1" % name for name in libs) + ("newlib>=2,<3",))
    build_wheel(wheels, "newlib", "2.1", {"newlib.py": "VALUE = 2\n"})
    # Проверка: настоящий pip принимает и параметры пробной установки (--dry-run), а она
    # ничего не ставит и не оставляет на диске — ни в окружении, ни рядом.
    before = sandbox.snapshot(newer)
    proc = sandbox.run("update.sh", newer, args=("--check",), **env)
    assert proc.returncode == 0, _report(proc)
    assert "Обновление без сети" in proc.stdout and "pip проверил пробной установкой" in proc.stdout, _report(proc)
    after = sandbox.snapshot(newer)
    assert after == before, sorted(set(after.items()) ^ set(before.items()))
    proc = sandbox.run("update.sh", newer, **env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, _report(proc)
    assert "несогласованных библиотеках" not in out, _report(proc)            # pip check доволен
    assert sandbox.wait_version() == NEW
    assert os.path.exists(os.path.join(os.path.dirname(os.path.dirname(sandbox.site_file("x"))), "newlib.py"))

    # Нет пакета библиотеки — pip говорит об этом уже на проверке (пробной установкой):
    # служба не останавливается, откатывать нечего.
    broken = sandbox.sources("9.9.10", "src_third", requirements=requirements + "absentlib>=1\n")
    make_wheel(os.path.join(broken, "wheels"), "9.9.10")
    for args in (("--check",), ()):
        proc = sandbox.run("update.sh", broken, args=args, **env)
        out = proc.stdout + proc.stderr
        assert proc.returncode != 0, _report(proc)
        assert "Обновление без сети не пройдёт" in out, _report(proc)
        assert "No matching distribution found for absentlib" in out, _report(proc)
        assert "Откат" not in out and "Останавливаю" not in out, _report(proc)
    assert sandbox.wait_version() == NEW


@needs_root
def test_install_refuses_data_dir_on_unmounted_filesystem(sandbox):
    """Каталог данных — точка монтирования без диска: отказ до любых действий, владелец каталога не меняется."""
    os.mkdir(sandbox.data, 0o755)
    sandbox.fstab(sandbox.data)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert _mount_advice(sandbox.data) in out and "chown" not in out and "install -d" not in out, _report(proc)
    for path in (sandbox.install_dir, sandbox.unit, sandbox.wrapper):
        assert not os.path.exists(path), path
    assert os.stat(sandbox.data).st_uid == 0 and os.listdir(sandbox.data) == []
    assert stat.S_IMODE(os.stat(sandbox.data).st_mode) == 0o755
    assert sandbox.calls() == [] and sandbox.calls("pkg_calls") == []


@needs_root
@pytest.mark.parametrize("linked", [False, True])
def test_install_leaves_owner_of_import_files_alone(sandbox, linked):
    """Каталог данных уже есть (в том числе ссылкой на другой диск): он отдаётся службе, кроме содержимого import/."""
    real = sandbox.data
    if linked:
        real = os.path.join(sandbox.base, "storage", "mailarchiver")
        os.makedirs(real)
        os.symlink(real, sandbox.data)
    _write(os.path.join(real, "import", "old-mail.pst"), "x")
    _write(os.path.join(real, "made-by-root"), "x")
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"))
    assert proc.returncode == 0, _report(proc)
    uid = pwd.getpwnam(USER).pw_uid
    assert os.stat(real).st_uid == uid and os.stat(os.path.join(real, "import")).st_uid == uid
    assert os.stat(os.path.join(real, "made-by-root")).st_uid == uid
    assert os.stat(os.path.join(real, "import", "old-mail.pst")).st_uid == 0
    assert os.lstat(sandbox.data).st_uid == (0 if linked else uid)
    assert sandbox.wait_version() == NEW


@needs_root
def test_install_into_existing_directory_rolls_back_what_it_created(sandbox):
    """Каталог установки был заранее (создан вручную, точка монтирования): откат убирает всё своё.

    Раньше для такого каталога откат не делал ничего: оставались unit службы в
    цикле перезапусков, команда mailarchiver и дополнение с ослабленной защитой.
    """
    os.makedirs(os.path.join(sandbox.install_dir, "lost+found"))
    os.makedirs(sandbox.data)
    subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
    sandbox.behaviour("crash:" + NEW)
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("install.sh", src, MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "Application startup failed" in out, _report(proc)
    assert "откат частичной установки" in out and "созданное этим запуском, убрано" in out, _report(proc)
    assert "Каталог %s был до установки и оставлен на месте" % sandbox.install_dir in out, _report(proc)
    for path in (sandbox.unit, sandbox.dropin, os.path.dirname(sandbox.dropin), sandbox.wrapper,
                 sandbox.app_dir, sandbox.venv):
        assert not os.path.exists(path), path
    assert os.listdir(sandbox.install_dir) == ["lost+found"]     # чужое — на месте
    calls = sandbox.calls()
    assert "stop mailarchiver" in calls and "disable mailarchiver" in calls and calls[-1] == "daemon-reload", calls
    assert not sandbox.active() and sandbox.substate() == "dead"  # не осталась в цикле перезапусков
    assert os.path.exists(os.path.join(sandbox.data, "behaviour"))
    # повторная установка не спотыкается о остатки
    sandbox.behaviour("")
    proc = sandbox.run("install.sh", src)
    assert proc.returncode == 0 and "Обнаружена существующая установка" not in proc.stdout, _report(proc)
    assert sandbox.wait_version() == NEW


@needs_root
def test_install_does_not_remove_what_was_there_before(sandbox):
    """Откат в существующем каталоге не трогает то, чего установка не создавала: unit, команду, окружение."""
    os.makedirs(sandbox.venv)
    _write(os.path.join(sandbox.venv, "note"), "прежнее окружение\n")
    _write(sandbox.unit, "[Service]\nExecStart=/bin/true\n")
    _write(sandbox.wrapper, "#!/bin/sh\n# другая программа\n", 0o755)
    _write(sandbox.dropin, "[Service]\nProtectProc=default\nPrivateDevices=no\n")
    os.makedirs(sandbox.data)
    subprocess.run(["chown", "%s:%s" % (USER, GROUP), sandbox.data], check=True)
    sandbox.behaviour("crash:" + NEW)
    proc = sandbox.run("install.sh", sandbox.sources(NEW, "src_new"), MA_RELAX_SANDBOX="1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and "откат частичной установки" in out, _report(proc)
    assert "убрано" not in out, _report(proc)
    assert not os.path.exists(sandbox.app_dir)
    for path in (sandbox.venv, sandbox.unit, sandbox.wrapper, sandbox.dropin):
        assert os.path.exists(path), path
    with open(sandbox.dropin, encoding="utf-8") as fh:
        assert fh.read() == "[Service]\nProtectProc=default\nPrivateDevices=no\n"
    assert "disable mailarchiver" not in sandbox.calls()


# ---------------------------------------------------------------------------
#  uninstall.sh
# ---------------------------------------------------------------------------
@needs_root
def test_uninstall_removes_unit_dropin_and_command(sandbox):
    """Удаление после установки с MA_RELAX_SANDBOX=1: unit, дополнение, команда и код — данные остаются."""
    src = sandbox.sources(NEW, "src_new")
    proc = sandbox.run("install.sh", src, FAKE_NAMESPACE_FAIL="1", MA_RELAX_SANDBOX="1")
    assert proc.returncode == 0 and os.path.exists(sandbox.dropin), _report(proc)
    assert sandbox.wait_version() == NEW
    sandbox.forget_calls()
    proc = sandbox.run("uninstall.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert proc.stderr == "", _report(proc)                  # ни одного «Ошибка на строке…»
    assert "Дополнение с ослабленной защитой удалено: " + sandbox.dropin in proc.stdout, _report(proc)
    for path in (sandbox.unit, sandbox.dropin, os.path.dirname(sandbox.dropin), sandbox.wrapper,
                 sandbox.install_dir):
        assert not os.path.exists(path), path
    assert not sandbox.active()
    calls = sandbox.calls()
    assert "stop mailarchiver" in calls and "daemon-reload" in calls, calls
    # без --purge данные, конфигурация и пользователь службы остаются
    assert os.path.exists(sandbox.db) and os.path.exists(os.path.join(sandbox.config_dir, "config.yaml"))
    assert sandbox.calls("pkg_calls") == []                  # userdel не вызывался


@needs_root
def test_uninstall_keeps_what_is_not_ours(sandbox):
    """Правки администратора в <unit>.d и чужая программа с именем mailarchiver остаются на месте."""
    src = sandbox.sources(NEW, "src_new")
    _write(sandbox.unit, "[Service]\nExecStart=/bin/true\n")
    _write(sandbox.dropin, "[Service]\nProtectProc=default\n")
    override = os.path.join(os.path.dirname(sandbox.dropin), "override.conf")
    _write(override, "[Service]\nMemoryMax=8G\n")
    _write(sandbox.wrapper, "#!/bin/sh\n# другая программа\n", 0o755)
    os.makedirs(sandbox.app_dir)
    proc = sandbox.run("uninstall.sh", src)
    assert proc.returncode == 0, _report(proc)
    assert not os.path.exists(sandbox.unit) and not os.path.exists(sandbox.dropin)
    assert not os.path.exists(sandbox.install_dir)
    assert os.path.exists(override) and "остались ваши дополнения" in proc.stdout, _report(proc)
    assert os.path.exists(sandbox.wrapper)
    assert "daemon-reload" in sandbox.calls()


@needs_root
def test_uninstall_rejects_unknown_arguments(sandbox):
    """«uninstall.sh --check» (по привычке от update.sh) не должен ничего удалять."""
    sandbox.install_old()
    src = sandbox.sources(NEW, "src_new")
    sandbox.forget_calls()
    before = sandbox.snapshot(src)
    for args in (("--check",), ("--purge", "--yes"), ("purge",)):
        proc = sandbox.run("uninstall.sh", src, args=args)
        assert proc.returncode == 2, (args, _report(proc))
        assert "Неизвестный параметр: " + args[-1] in proc.stderr and "Использование:" in proc.stderr, _report(proc)
        assert proc.stdout == ""
        _untouched(sandbox, before, proc, src)
    for flag in ("-h", "--help"):
        proc = sandbox.run("uninstall.sh", src, args=(flag,))
        assert proc.returncode == 0 and proc.stderr == "", _report(proc)
        for wanted in ("Использование:", "--purge", "--remove-user", "MA_DATA_DIR", "sudo MA_"):
            assert wanted in proc.stdout, wanted
        _untouched(sandbox, before, proc, src)
    assert sandbox.wait_version() == OLD


@needs_root
def test_uninstall_purge_follows_data_directory_link(sandbox):
    """--purge с каталогом данных-ссылкой: удаляются и каталог на «другом диске», и ссылка — оба названы."""
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    real = os.path.join(mount, "mailarchiver")
    proc = sandbox.run("uninstall.sh", sandbox.sources(NEW, "src_new"), args=("--purge",),
                       MA_DATA_DIR=sandbox.data + "/")
    assert proc.returncode == 0 and proc.stderr == "", _report(proc)
    assert "Каталог данных удалён: каталог %s и ссылка на него %s." % (real, sandbox.data) in proc.stdout, \
        _report(proc)
    assert "Каталог конфигурации удалён: %s." % sandbox.config_dir in proc.stdout, _report(proc)
    assert not os.path.lexists(sandbox.data) and not os.path.exists(real) and os.path.isdir(mount)
    assert not os.path.exists(sandbox.config_dir) and not os.path.exists(sandbox.install_dir)
    assert "userdel " + USER in sandbox.calls("pkg_calls")


@needs_root
def test_uninstall_purge_with_unmounted_data_disk(sandbox):
    """Диск с данными не подключён: удаляется только ссылка, и прямо сказано, что данные остались."""
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    real = os.path.join(mount, "mailarchiver")
    sandbox.stop()
    os.rename(mount, mount + ".unmounted")
    proc = sandbox.run("uninstall.sh", sandbox.sources(NEW, "src_new"), args=("--purge",))
    # удалено не всё: код 1, и пользователь службы остаётся — данные на том диске принадлежат ему
    assert proc.returncode == 1, _report(proc)
    assert ("Каталог данных %s — ссылка на %s, а такого каталога сейчас нет (диск не подключён?): "
            "удалена только ссылка." % (sandbox.data, real)) in proc.stderr, _report(proc)
    assert "НЕ удалено" in proc.stderr and 'rm -rf "%s"' % real in proc.stderr, _report(proc)
    assert "удалено не всё" in proc.stdout, _report(proc)
    assert "Данные и конфигурация удалены" not in proc.stdout, _report(proc)
    assert "userdel " + USER not in sandbox.calls("pkg_calls")
    assert not os.path.lexists(sandbox.data)
    assert os.path.exists(os.path.join(mount + ".unmounted", "mailarchiver", "mailarchiver.db"))
    assert not os.path.exists(sandbox.config_dir)


@needs_root
def test_uninstall_purge_refuses_directory_near_the_root(sandbox):
    """Каталог данных ведёт в каталог верхнего уровня (или в корень): rm -rf по такому пути — не удаление данных."""
    try:
        top = tempfile.mkdtemp(prefix="ma-purge-test-", dir="/")
    except OSError as exc:
        pytest.skip("в корне нельзя создать каталог: %s" % exc)
    try:
        _write(os.path.join(top, "keep"), "x")
        os.symlink(top, sandbox.data)
        os.makedirs(sandbox.app_dir)
        src = sandbox.sources(NEW, "src_new")
        proc = sandbox.run("uninstall.sh", src, args=("--purge",))
        assert proc.returncode == 1, _report(proc)
        assert ("Каталог данных НЕ удалён: %s ведёт в %s — это каталог верхнего уровня (%s)"
                % (sandbox.data, top, top)) in proc.stderr, _report(proc)
        assert os.path.exists(os.path.join(top, "keep")) and os.path.islink(sandbox.data)
        assert "удалено не всё" in proc.stdout, _report(proc)
        # остальное удалено; пользователь службы оставлен — его файлы на месте
        assert not os.path.exists(sandbox.config_dir) and not os.path.exists(sandbox.install_dir)
        assert sandbox.calls("pkg_calls") == []
        # тот же каталог, заданный прямо
        os.remove(sandbox.data)
        proc = sandbox.run("uninstall.sh", src, args=("--purge",), MA_DATA_DIR=top + "/")
        assert proc.returncode == 1, _report(proc)
        assert "Каталог данных НЕ удалён: %s — это каталог верхнего уровня" % top in proc.stderr, _report(proc)
        assert os.path.exists(os.path.join(top, "keep"))
    finally:
        shutil.rmtree(top, ignore_errors=True)


@needs_root
def test_uninstall_question_shows_what_will_really_be_deleted(sandbox):
    """Вопрос перед удалением данных (запуск в терминале) называет каталог, на который ведёт ссылка."""
    import pty
    sandbox.install_old()
    mount = _move_data_to_storage(sandbox)
    real = os.path.join(mount, "mailarchiver")
    src = sandbox.sources(NEW, "src_new")
    os.symlink("/", os.path.join(sandbox.base, "to-root"))

    def ask(answer: bytes, **extra):
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(["bash", os.path.join(src, "scripts", "uninstall.sh")],
                                    env=sandbox.env(**extra), cwd=src, stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            os.write(master, answer)
            out, _ = proc.communicate(timeout=60)
        finally:
            os.close(master)
            os.close(slave)
        return proc.returncode, out

    # ответ «нет» — ничего из данных не удаляется; корень удалять и не предлагается
    code, out = ask(b"n\n", MA_CONFIG_DIR=os.path.join(sandbox.base, "to-root"))
    assert code == 0, out
    assert "Данные:       %s — ссылка: будут удалены каталог %s и сама ссылка" % (sandbox.data, real) in out, out
    assert "Конфигурация: %s НЕ будет удалён: это корень файловой системы" \
        % os.path.join(sandbox.base, "to-root") in out, out
    assert "Данные сохранены: " + sandbox.data in out, out
    assert os.path.islink(sandbox.data) and os.path.exists(os.path.join(real, "mailarchiver.db"))
    assert os.path.exists(os.path.join(sandbox.config_dir, "config.yaml"))
    # ответ «да» — удаляется именно то, что было названо
    code, out = ask(b"y\n")
    assert code == 0, out
    assert "Каталог данных удалён: каталог %s и ссылка на него %s." % (real, sandbox.data) in out, out
    assert not os.path.lexists(sandbox.data) and not os.path.exists(real)
    assert not os.path.exists(sandbox.config_dir)


# ---------------------------------------------------------------------------
#  Архив выпуска
# ---------------------------------------------------------------------------
def test_scripts_have_valid_syntax():
    if not shutil.which("bash"):
        pytest.skip("нет bash")
    for name in sorted(os.listdir(os.path.join(ROOT, "scripts"))):
        if name.endswith(".sh"):
            proc = subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", name)],
                                  capture_output=True, text=True)
            assert proc.returncode == 0, "%s: %s" % (name, proc.stderr)


def _release_tree(tmp_path, version="7.7.7"):
    """Маленький проект с тем же устройством, что настоящий, и сборщиком архива."""
    src = tmp_path / "mail_backup"
    files = {
        "VERSION": version + "\n", "README.md": "x", "requirements.txt": "", "LICENSE": "x",
        "pyproject.toml": '[project]\nname = "mailarchiver"\nversion = "%s"\n' % version,
        "mailarchiver/__init__.py": "", "mailarchiver/version.py": '__version__ = "%s"\n' % version,
        "scripts/install.sh": "#!/bin/sh\n", "scripts/update.sh": "#!/bin/sh\n", "scripts/common.sh": "",
        "systemd/mailarchiver.service": "[Unit]\n", "config/config.example.yaml": "server:\n",
        "docs/ru/01.md": "x", "tests/test_x.py": "", "tests/data/fixture.eml": "x",
        # то, чего в выпуске быть не должно
        "docs/manual.docx": "x", "mailarchiver-1.0.0.tar.gz": "x", "mailarchiver-7.7.7.tar.gz.part": "x",
        "secret.key": "x", "config/config.yaml": "x", "exports/a.pst": "x", "data/mailarchiver.db": "x",
        "mailarchiver/__pycache__/x.pyc": "x",
        # пакеты прежней автономной сборки: в архив они попадают только через --wheels
        "wheels/leftover-1.0-py3-none-any.whl": "x",
    }
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for name in ("make_release.py", "make_offline_bundle.sh"):
        shutil.copy(os.path.join(ROOT, "scripts", name), src / "scripts" / name)
    os.chmod(src / "scripts" / "update.sh", 0o755)
    return src


def _build_release(src, out, *args):
    out.mkdir(exist_ok=True)
    return subprocess.run([sys.executable, str(src / "scripts" / "make_release.py"), *args, str(out)],
                          capture_output=True, text=True)


def test_release_archive_is_readable_by_everyone(tmp_path):
    """Права в архиве не зависят от umask и прав в каталоге сборки.

    Архив 1.9.0 был собран из каталога, где все файлы имели права 0600, а
    каталоги — 0700. После распаковки от root служба не могла войти в свой
    рабочий каталог, и обновление заканчивалось откатом.
    """
    src = _release_tree(tmp_path)
    _restrict(str(src))
    proc = _build_release(src, tmp_path / "out")
    assert proc.returncode == 0, _report(proc)
    archive = tmp_path / "out" / "mailarchiver-7.7.7.tar.gz"
    assert archive.exists(), os.listdir(tmp_path / "out")
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
    names = {m.name for m in members}
    for wanted in ("VERSION", "mailarchiver/__init__.py", "scripts/common.sh", "tests/data/fixture.eml",
                   "systemd/mailarchiver.service"):
        assert "mail_backup/" + wanted in names, wanted
    for forbidden in ("exports", "__pycache__", ".docx", ".tar.gz", "secret.key", "config/config.yaml",
                      "mail_backup/data/", "wheels", ".whl"):
        assert not [n for n in names if forbidden in n], forbidden
    for member in members:
        assert member.name == "mail_backup" or member.name.startswith("mail_backup/"), member.name
        assert member.uid == 0 and member.gid == 0, member.name
        if member.isdir() or member.name.endswith(".sh"):
            assert member.mode == 0o755, (member.name, oct(member.mode))
        else:
            assert member.mode == 0o644, (member.name, oct(member.mode))
    # один и тот же набор файлов даёт один и тот же архив
    first = archive.read_bytes()
    assert _build_release(src, tmp_path / "out").returncode == 0
    assert archive.read_bytes() == first


def test_release_refuses_inconsistent_version_and_missing_files(tmp_path):
    src = _release_tree(tmp_path)
    (src / "mailarchiver" / "version.py").write_text('__version__ = "7.7.6"\n', encoding="utf-8")
    proc = _build_release(src, tmp_path / "out")
    assert proc.returncode != 0 and "расходится" in proc.stderr, _report(proc)
    (src / "mailarchiver" / "version.py").write_text('__version__ = "7.7.7"\n', encoding="utf-8")
    os.remove(src / "scripts" / "common.sh")
    proc = _build_release(src, tmp_path / "out")
    assert proc.returncode != 0 and "scripts/common.sh" in proc.stderr, _report(proc)
    assert not os.listdir(tmp_path / "out")


@pytest.mark.skipif(not shutil.which("git"), reason="нет git")
def test_release_from_git_checkout(tmp_path):
    """В каталоге с git: отслеживаемые и новые файлы входят, игнорируемые — нет."""
    src = _release_tree(tmp_path)
    (src / ".gitignore").write_text("ignored.txt\nexports/\n", encoding="utf-8")
    (src / "ignored.txt").write_text("x", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull)
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(src), "-c", "safe.directory=*", *cmd], check=True,
                       capture_output=True, env=env)
    (src / "mailarchiver" / "new_module.py").write_text("", encoding="utf-8")     # ещё не в git
    proc = subprocess.run([sys.executable, str(src / "scripts" / "make_release.py"), str(tmp_path / "out")],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, _report(proc)
    assert "mailarchiver/new_module.py" in proc.stdout          # о новых файлах сказано
    with tarfile.open(tmp_path / "out" / "mailarchiver-7.7.7.tar.gz") as tar:
        names = set(tar.getnames())
    assert "mail_backup/mailarchiver/new_module.py" in names and "mail_backup/.gitignore" in names
    assert "mail_backup/ignored.txt" not in names
    assert not [n for n in names if n.endswith((".docx", ".tar.gz", ".pyc", "secret.key", ".whl"))]


def _wheel_dir(tmp_path, *names):
    """Каталог с «пакетами» (пустыми zip) и закрытыми правами — как после сборки с umask 077."""
    folder = tmp_path / "pkgs"
    folder.mkdir(mode=0o700)
    for name in names:
        with zipfile.ZipFile(folder / name, "w"):
            pass
        os.chmod(folder / name, 0o600)
    return folder


def test_release_with_wheels_for_offline_install(tmp_path):
    """--wheels: пакеты входят в архив (0644, root), а имя архива говорит, для какого Python и платформы."""
    src = _release_tree(tmp_path)
    wheels = _wheel_dir(tmp_path, "mailarchiver-7.7.7-py3-none-any.whl",
                        "mailarchiver-7.7.6-py3-none-any.whl",              # остался от прежней сборки
                        "PyYAML-6.0.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                        "cryptography-44.0.0-cp39-abi3-manylinux_2_28_x86_64.whl",
                        "jinja2-3.1.6-py3-none-any.whl")
    (wheels / "notes.txt").write_text("x", encoding="utf-8")
    proc = _build_release(src, tmp_path / "out", "--wheels", str(wheels))
    assert proc.returncode == 0, _report(proc)
    # cp312 — по пакету, собранному под конкретный Python (cp39-abi3 годится для любого);
    # linux_x86_64 — по платформенным пакетам, без версии glibc
    archive = tmp_path / "out" / "mailarchiver-7.7.7-offline-cp312-linux_x86_64.tar.gz"
    assert os.listdir(tmp_path / "out") == [archive.name], os.listdir(tmp_path / "out")
    assert str(archive) in proc.stdout
    with tarfile.open(archive) as tar:
        members = {m.name: m for m in tar.getmembers()}
    packed = sorted(n.split("/")[-1] for n in members if n.startswith("mail_backup/wheels/"))
    assert packed == ["PyYAML-6.0.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
                      "cryptography-44.0.0-cp39-abi3-manylinux_2_28_x86_64.whl",
                      "jinja2-3.1.6-py3-none-any.whl", "mailarchiver-7.7.7-py3-none-any.whl"], packed
    assert members["mail_backup/wheels"].isdir() and members["mail_backup/wheels"].mode == 0o755
    for name, member in members.items():
        assert member.uid == 0 and member.gid == 0, name
        if name.endswith(".whl"):
            assert member.mode == 0o644 and member.isfile(), (name, oct(member.mode))
    # остальное — как в обычном архиве
    for wanted in ("VERSION", "scripts/install.sh", "scripts/common.sh", "mailarchiver/__init__.py"):
        assert "mail_backup/" + wanted in members, wanted
    assert not [n for n in members if "leftover" in n or "secret.key" in n or "notes.txt" in n or "7.7.6" in n]
    # «=»-форма параметра и каталог архива по умолчанию (корень проекта)
    proc = subprocess.run([sys.executable, str(src / "scripts" / "make_release.py"), "--wheels=%s" % wheels],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and (src / archive.name).exists(), _report(proc)


def test_release_with_pure_python_wheels_is_named_after_the_interpreter(tmp_path):
    """Нет ни одного платформенного пакета — метки берутся у интерпретатора, которым собирали."""
    src = _release_tree(tmp_path)
    wheels = _wheel_dir(tmp_path, "mailarchiver-7.7.7-py3-none-any.whl", "jinja2-3.1.6-py3-none-any.whl")
    proc = _build_release(src, tmp_path / "out", "--wheels", str(wheels))
    assert proc.returncode == 0, _report(proc)
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    expected = "mailarchiver-7.7.7-offline-cp%d%d-%s.tar.gz" % (sys.version_info[0], sys.version_info[1], platform)
    assert os.listdir(tmp_path / "out") == [expected]


def test_release_refuses_unusable_wheels_and_bad_arguments(tmp_path):
    src = _release_tree(tmp_path)
    out = tmp_path / "out"
    wheels = _wheel_dir(tmp_path, "mailarchiver-7.7.6-py3-none-any.whl", "jinja2-3.1.6-py3-none-any.whl")
    proc = _build_release(src, out, "--wheels", str(wheels))
    assert proc.returncode != 0 and "нет пакета приложения mailarchiver-7.7.7-*.whl" in proc.stderr, _report(proc)
    os.rename(wheels / "mailarchiver-7.7.6-py3-none-any.whl", wheels / "mailarchiver-7.7.7-py3-none-any.whl")
    (wheels / "uvloop-0.21.0.tar.gz").write_text("x", encoding="utf-8")     # так выглядит pip download без пакета
    proc = _build_release(src, out, "--wheels", str(wheels))
    assert proc.returncode != 0 and "uvloop-0.21.0.tar.gz" in proc.stderr and "pip wheel" in proc.stderr, \
        _report(proc)
    proc = _build_release(src, out, "--wheels", str(tmp_path / "no-such-dir"))
    assert proc.returncode != 0 and "не найден" in proc.stderr, _report(proc)
    for args in (["--wheels"], ["--weels", "x"], ["a", "b"]):
        proc = subprocess.run([sys.executable, str(src / "scripts" / "make_release.py"), *args],
                              capture_output=True, text=True)
        assert proc.returncode == 2 and "Использование:" in proc.stderr, (args, _report(proc))
    assert not os.listdir(out)                                # ни архива, ни обрывка .part
    assert not [n for n in os.listdir(src) if n.endswith((".tar.gz", ".part"))
                and n != "mailarchiver-1.0.0.tar.gz" and n != "mailarchiver-7.7.7.tar.gz.part"]


# Подделки для make_offline_bundle.sh: интерпретатор, у которого «-m pip» выполняет
# подделка pip (сети в тестах нет), а всё остальное — настоящий Python.
BUNDLE_PYTHON = '#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n  shift 2\n' \
                '  exec "@PYTHON@" "@PIP@" "$@"\nfi\nexec "@PYTHON@" "$@"\n'
BUNDLE_PIP = r"""
import os, sys, zipfile
args = sys.argv[1:]
with open(os.environ["BUNDLE_PIP_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")
if os.environ.get("BUNDLE_NO_PIP"):
    sys.exit("No module named pip")
if args[:1] != ["wheel"]:
    sys.exit(0)
out = args[args.index("-w") + 1]


def wheel(name):
    with zipfile.ZipFile(os.path.join(out, name), "w"):
        pass


if "-r" in args:
    wheel("PyYAML-6.0.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    if os.environ.get("BUNDLE_PIP_FAIL"):                    # часть пакетов уже получена — и отказ
        sys.exit("ERROR: No matching distribution found for uvloop>=0.21")
    wheel("jinja2-3.1.6-py3-none-any.whl")
else:
    src = [a for a in args[1:] if os.path.isdir(a) and a != out][0]
    # пакет собирается из распакованного архива выпуска, а не из рабочего каталога
    if os.path.exists(os.path.join(src, "secret.key")) or not os.path.exists(os.path.join(src, "pyproject.toml")):
        sys.exit("не тот каталог: %s" % src)
    with open(os.path.join(src, "VERSION"), encoding="utf-8") as fh:
        wheel("mailarchiver-%s-py3-none-any.whl" % fh.read().strip())
"""


def _bundle(tmp_path, src, *args, **extra):
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "pip.py").write_text(BUNDLE_PIP, encoding="utf-8")
    python = tools / "python3"
    python.write_text(BUNDLE_PYTHON.replace("@PYTHON@", sys.executable).replace("@PIP@", str(tools / "pip.py")),
                      encoding="utf-8")
    os.chmod(python, 0o755)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(tmp_path / "tmp"), BUNDLE_PIP_CALLS=str(tmp_path / "pip_calls"),
               MA_PYTHON=str(python), **extra)
    return subprocess.run(["bash", str(src / "scripts" / "make_offline_bundle.sh"), *args],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path))


def _tree(path) -> list:
    return sorted(os.path.relpath(os.path.join(base, name), path)
                  for base, dirs, files in os.walk(path) for name in dirs + files)


@pytest.mark.skipif(not shutil.which("bash"), reason="нет bash")
def test_offline_bundle_script(tmp_path):
    """make_offline_bundle.sh: пакет приложения, пакеты библиотек и архив с именем по Python и платформе."""
    src = _release_tree(tmp_path)
    before = _tree(src)
    proc = _bundle(tmp_path, src, str(tmp_path / "out"))
    assert proc.returncode == 0, _report(proc)
    name = "mailarchiver-7.7.7-offline-cp312-linux_x86_64.tar.gz"
    assert os.listdir(tmp_path / "out") == [name]
    with tarfile.open(tmp_path / "out" / name) as tar:
        names = set(tar.getnames())
    for wheel in ("mailarchiver-7.7.7-py3-none-any.whl", "jinja2-3.1.6-py3-none-any.whl",
                  "PyYAML-6.0.3-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"):
        assert "mail_backup/wheels/" + wheel in names, wheel
    assert "mail_backup/scripts/update.sh" in names and not [n for n in names if "leftover" in n]
    # для чего собран архив и что с ним делать на сервере
    for wanted in ("Архив собирается для: Python %d.%d" % sys.version_info[:2], name,
                   "sudo bash scripts/update.sh --check", "sudo bash scripts/install.sh", "MA_WHEELS="):
        assert wanted in proc.stdout, (wanted, _report(proc))
    calls = (tmp_path / "pip_calls").read_text(encoding="utf-8").splitlines()
    assert calls[0] == "--version" and len(calls) == 3, calls
    assert calls[1].startswith("wheel ") and "--no-deps" in calls[1] and " -w " in calls[1], calls
    assert calls[2].startswith("wheel -r ") and "requirements.txt" in calls[2] and "--only-binary" not in calls[2]
    assert "--no-deps" not in calls[2], calls                # библиотеки — вместе с тем, что нужно им самим
    # каталог проекта не тронут, временные файлы убраны
    assert _tree(src) == before
    assert os.listdir(tmp_path / "tmp") == []

    # --only-binary и свой каталог для пакетов
    keep = tmp_path / "keep"
    proc = _bundle(tmp_path, src, "--only-binary", "--wheels-dir", str(keep), str(tmp_path / "out2"))
    assert proc.returncode == 0, _report(proc)
    assert "--only-binary=:all:" in (tmp_path / "pip_calls").read_text(encoding="utf-8").splitlines()[-1]
    assert len([n for n in os.listdir(keep) if n.endswith(".whl")]) == 3
    assert os.listdir(tmp_path / "out2") == [name]
    # непустой каталог пакетов: прежние пакеты попали бы в архив
    proc = _bundle(tmp_path, src, "--wheels-dir", str(keep), str(tmp_path / "out3"))
    assert proc.returncode != 0 and "не пуст" in proc.stderr, _report(proc)
    assert not (tmp_path / "out3").exists() or not os.listdir(tmp_path / "out3")
    assert os.listdir(tmp_path / "tmp") == []


@pytest.mark.skipif(not shutil.which("bash"), reason="нет bash")
def test_offline_bundle_script_refuses_without_pip(tmp_path):
    src = _release_tree(tmp_path)
    before = _tree(src)
    proc = _bundle(tmp_path, src, str(tmp_path / "out"), BUNDLE_NO_PIP="1")
    assert proc.returncode != 0 and "нет pip" in proc.stderr, _report(proc)
    assert not (tmp_path / "out").exists() and _tree(src) == before
    assert (tmp_path / "pip_calls").read_text(encoding="utf-8").splitlines() == ["--version"]
    # опечатка в параметре и справка
    proc = _bundle(tmp_path, src, "--only-binaries")
    assert proc.returncode == 2 and "Неизвестный параметр: --only-binaries" in proc.stderr, _report(proc)
    proc = _bundle(tmp_path, src, "--help")
    assert proc.returncode == 0 and "--only-binary" in proc.stdout and "--python" in proc.stdout, _report(proc)
    assert _tree(src) == before


def test_release_skips_wheels_kept_elsewhere_in_the_project(tmp_path):
    """Пакеты .whl в проекте вне wheels/ (например «--wheels-dir wheelhouse») в архив не попадают дважды."""
    src = _release_tree(tmp_path)
    house = src / "wheelhouse"
    house.mkdir()
    for name in ("mailarchiver-7.7.7-py3-none-any.whl", "jinja2-3.1.6-py3-none-any.whl"):
        with zipfile.ZipFile(house / name, "w"):
            pass
    (src / "docs" / "stray-1.0-py3-none-any.whl").write_text("x", encoding="utf-8")
    proc = _build_release(src, tmp_path / "out")
    assert proc.returncode == 0, _report(proc)
    with tarfile.open(tmp_path / "out" / "mailarchiver-7.7.7.tar.gz") as tar:
        names = tar.getnames()
    assert not [n for n in names if n.endswith(".whl") or "wheelhouse" in n], names
    assert "mail_backup/docs/ru/01.md" in names
    # с --wheels из этого же каталога пакеты входят — один раз, в wheels/
    proc = _build_release(src, tmp_path / "out2", "--wheels", str(house))
    assert proc.returncode == 0, _report(proc)
    (archive,) = os.listdir(tmp_path / "out2")
    with tarfile.open(tmp_path / "out2" / archive) as tar:
        names = tar.getnames()
    assert sorted(n for n in names if n.endswith(".whl")) == [
        "mail_backup/wheels/jinja2-3.1.6-py3-none-any.whl",
        "mail_backup/wheels/mailarchiver-7.7.7-py3-none-any.whl"], names
    assert not [n for n in names if "wheelhouse" in n], names


@pytest.mark.skipif(not shutil.which("bash"), reason="нет bash")
def test_offline_bundle_failure_does_not_leave_partial_wheels_directory(tmp_path):
    """Сборка сорвалась на полпути: каталог --wheels-dir не остаётся с обрывками (следующий запуск отказал бы)."""
    src = _release_tree(tmp_path)
    keep = tmp_path / "keep"
    # каталог создан скриптом — убирается целиком
    proc = _bundle(tmp_path, src, "--wheels-dir", str(keep), str(tmp_path / "out"), BUNDLE_PIP_FAIL="1")
    assert proc.returncode != 0 and "Не удалось получить пакеты библиотек" in proc.stderr, _report(proc)
    assert not keep.exists()
    # каталог был (пустой) — остаётся, но снова пустым
    keep.mkdir()
    proc = _bundle(tmp_path, src, "--wheels-dir", str(keep), str(tmp_path / "out"), BUNDLE_PIP_FAIL="1")
    assert proc.returncode != 0, _report(proc)
    assert keep.is_dir() and os.listdir(keep) == []
    assert os.listdir(tmp_path / "tmp") == []
    # и повторный запуск проходит
    proc = _bundle(tmp_path, src, "--wheels-dir", str(keep), str(tmp_path / "out"))
    assert proc.returncode == 0, _report(proc)
    assert len([n for n in os.listdir(keep) if n.endswith(".whl")]) == 3
    # чужое содержимое непустого каталога при отказе не трогается
    proc = _bundle(tmp_path, src, "--wheels-dir", str(keep), str(tmp_path / "out3"), BUNDLE_PIP_FAIL="1")
    assert proc.returncode != 0 and "не пуст" in proc.stderr, _report(proc)
    assert len(os.listdir(keep)) == 3


def test_required_script_tests_fail_at_collection_without_prerequisites():
    """MA_REQUIRE_SCRIPT_TESTS=1: нет условий для песочницы — модуль не «проходит» пропусками, а отказывает."""
    # с таким PATH не найти ни bash, ни runuser — как на машине, где тесты скриптов выполнить нельзя
    env = dict(os.environ, PATH=os.devnull, MA_REQUIRE_SCRIPT_TESTS="1")
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q",
               os.path.abspath(__file__)]
    proc = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "MA_REQUIRE_SCRIPT_TESTS=1: тесты скриптов установки обязаны выполняться" in out, _report(proc)
    assert "bash" in out and "runuser" in out, _report(proc)
    # без переменной те же условия — обычный пропуск
    del env["MA_REQUIRE_SCRIPT_TESTS"]
    proc = subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT)
    assert proc.returncode == 0, _report(proc)


def test_unit_template_placeholders_are_known():
    """Все подстановки unit-файла заполняются скриптами (иначе в unit попадёт «@ИМЯ@»)."""
    with open(os.path.join(ROOT, "systemd", "mailarchiver.service"), encoding="utf-8") as fh:
        placeholders = set(re.findall(r"@[A-Z]+@", fh.read()))
    with open(os.path.join(ROOT, "scripts", "common.sh"), encoding="utf-8") as fh:
        known = set(re.findall(r"s\|(@[A-Z]+@)\|", fh.read()))
    assert placeholders and placeholders <= known, placeholders - known
