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
от другого пользователя); в остальных случаях они пропускаются.
"""
from __future__ import annotations

import grp
import os
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap

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


USER, GROUP = _service_user()
_READY = (sys.platform == "linux" and hasattr(os, "geteuid") and os.geteuid() == 0
          and USER and shutil.which("bash") and shutil.which("runuser"))
PYTHON = _python_for(USER) if _READY else None

needs_root = pytest.mark.skipif(not (_READY and PYTHON),
                                reason="нужны Linux, root, runuser и пользователь nobody")


# ---------------------------------------------------------------------------
#  Подделки
# ---------------------------------------------------------------------------
FAKE_SYSTEMCTL = r'''#!@PYTHON@
"""Подделка systemctl: одна служба, запуск как в systemd (User=, WorkingDirectory=, ExecStart=)."""
import grp, os, pwd, shlex, signal, sys, time

STATE = os.environ["FAKE_SYSTEMD_STATE"]
UNIT = os.environ.get("MA_UNIT_FILE", "/etc/systemd/system/mailarchiver.service")
JOURNAL = os.path.join(STATE, "journal")
NAMES = {200: "200/CHDIR", 203: "203/EXEC", 217: "217/USER"}


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
    conf = {"Environment": []}
    with open(UNIT, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if key == "Environment":
                    conf["Environment"].append(value)
                else:
                    conf[key] = value
    return conf


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
        try:
            user = pwd.getpwnam(conf.get("User", "root"))
            gid = grp.getgrnam(conf["Group"]).gr_gid if conf.get("Group") else user.pw_gid
            os.setgroups([])
            os.setgid(gid)
            os.setuid(user.pw_uid)
        except (KeyError, OSError) as exc:
            say("Failed at step USER spawning %s: %s" % (argv[0], exc))
            os._exit(217)
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
FAKE_PIP = r'''#!@PYTHON@
import os, shutil, sys
SITE = "@SITE@"
args = sys.argv[1:]
with open("@CALLS@", "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")
if args[:1] != ["install"]:
    sys.exit(0)
dirs = [a for a in args[1:] if os.path.isdir(a)]
if not dirs:
    sys.exit(0)
if os.environ.get("FAKE_PIP_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_PIP_SLEEP"]))
src, dst = os.path.join(dirs[0], "mailarchiver"), os.path.join(SITE, "mailarchiver")
shutil.rmtree(dst, ignore_errors=True)
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
        self.data_dir = val("data_dir", "/var/lib/mailarchiver")
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
    cfg = load_config(args.config or os.environ.get("MAILARCHIVER_CONFIG"))
    if args.command == "check-config":
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
    if version != __version__:
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


def _write(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        self.wrapper = os.path.join(base, "usrbin", "mailarchiver")
        self.db = os.path.join(self.data, "mailarchiver.db")
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
        for name in ("apt-get", "dnf", "yum", "zypper", "pacman"):
            _write(os.path.join(self.bin, name), "#!/bin/sh\nexit 0\n", 0o755)
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
        }
        env.update(extra)
        return {key: value for key, value in env.items() if value is not None}

    def start(self, script: str, src: str, **extra: str):
        """Запустить скрипт в фоне, в своей группе процессов (для сигналов)."""
        return subprocess.Popen(["bash", os.path.join(src, "scripts", script)], env=self.env(**extra), cwd=src,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, start_new_session=True)

    def health_hits(self, version: str) -> int:
        try:
            with open(os.path.join(self.data, "health_hits"), encoding="utf-8") as fh:
                return fh.read().split().count(version)
        except OSError:
            return 0

    def run(self, script: str, src: str, timeout: int = 120, **extra: str):
        # как в документации: из каталога с исходниками новой версии
        return subprocess.run(["bash", os.path.join(src, "scripts", script)], env=self.env(**extra), cwd=src,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def systemctl(self, *args: str):
        return subprocess.run([os.path.join(self.bin, "systemctl"), *args], env=self.env(),
                              capture_output=True, text=True, timeout=30)

    def stop(self) -> None:
        try:
            self.systemctl("stop", "mailarchiver")
        except Exception:  # noqa: BLE001
            pass

    # -- исходники ---------------------------------------------------------
    def sources(self, version: str, name: str, *, broken_import: bool = False) -> str:
        """Каталог с исходниками: настоящие скрипты и unit, учебный пакет."""
        src = os.path.join(self.base, name, "mail_backup")
        for rel in ("scripts", "systemd"):
            shutil.copytree(os.path.join(ROOT, rel), os.path.join(src, rel),
                            ignore=shutil.ignore_patterns("__pycache__"))
        for rel, text in MINI.items():
            _write(os.path.join(src, "mailarchiver", rel), text.replace("@VERSION@", version))
        if broken_import:
            _write(os.path.join(src, "mailarchiver", "web", "app.py"), "import no_such_module_in_tests\n")
        _write(os.path.join(src, "VERSION"), version + "\n")
        _write(os.path.join(src, "requirements.txt"), "# пусто\n")
        _write(os.path.join(src, "config", "config.example.yaml"),
               CONFIG.replace("@HOST@", "127.0.0.1").replace("@PORT@", "8493")
               .replace("@DATA@", "/var/lib/mailarchiver"))
        return src

    def install_old(self) -> None:
        """Работающая установка прежней версии — исходное состояние для update.sh."""
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
@pytest.mark.parametrize("signum, group", [(15, False), (2, True), (1, False)])
def test_update_interrupted_by_signal_rolls_back(sandbox, signum, group):
    """Ctrl-C, kill и обрыв сессии посреди обновления: откат, а не полуобновлённая установка."""
    import signal
    import time
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
    proc = sandbox.run("update.sh", sandbox.sources(NEW, "src_new"), MA_START_TIMEOUT="13")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, _report(proc)
    assert "http://127.0.0.1:%d/health" % sandbox.port in out and "MA_START_TIMEOUT" in out, _report(proc)
    assert "Откат" in out
    assert sandbox.wait_version() == OLD


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
    }
    for rel, text in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    shutil.copy(os.path.join(ROOT, "scripts", "make_release.py"), src / "scripts" / "make_release.py")
    os.chmod(src / "scripts" / "update.sh", 0o755)
    return src


def _build_release(src, out):
    out.mkdir(exist_ok=True)
    return subprocess.run([sys.executable, str(src / "scripts" / "make_release.py"), str(out)],
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
                      "mail_backup/data/"):
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
    assert not [n for n in names if n.endswith((".docx", ".tar.gz", ".pyc", "secret.key"))]


def test_unit_template_placeholders_are_known():
    """Все подстановки unit-файла заполняются скриптами (иначе в unit попадёт «@ИМЯ@»)."""
    import re
    with open(os.path.join(ROOT, "systemd", "mailarchiver.service"), encoding="utf-8") as fh:
        placeholders = set(re.findall(r"@[A-Z]+@", fh.read()))
    with open(os.path.join(ROOT, "scripts", "common.sh"), encoding="utf-8") as fh:
        known = set(re.findall(r"s\|(@[A-Z]+@)\|", fh.read()))
    assert placeholders and placeholders <= known, placeholders - known
