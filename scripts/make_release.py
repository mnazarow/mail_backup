#!/usr/bin/env python3
"""Собрать архив выпуска ``mailarchiver-<версия>.tar.gz``.

    python scripts/make_release.py            # архив в корень проекта
    python scripts/make_release.py /tmp/out   # архив в другой каталог
    python scripts/make_release.py --wheels КАТАЛОГ [/tmp/out]
                                              # архив для установки без интернета

С ``--wheels`` в архив, в каталог ``mail_backup/wheels/``, добавляются пакеты
``.whl`` из указанного каталога (библиотеки и само приложение), а архив получает
имя ``mailarchiver-<версия>-offline-<python>-<платформа>.tar.gz``: такие пакеты
годятся только для своей версии Python и архитектуры. install.sh и update.sh,
найдя каталог ``wheels``, ставят всё из него и в сеть не ходят. Каталог обычно
готовит ``scripts/make_offline_bundle.sh`` — он же вызывает этот скрипт.
Без ``--wheels`` каталог ``wheels/`` из корня проекта в архив не попадает, как и
файлы ``.whl``, лежащие в проекте где-либо ещё.

Архив собирается не командой ``tar``, а здесь, потому что права и владельцы в
нём не должны зависеть от машины сборки. Архив 1.9.0 был собран из каталога,
где у всех файлов были права 0600, а у каталогов 0700; после распаковки от root
служба (она работает от своего пользователя) не могла войти в рабочий каталог,
и обновление заканчивалось откатом со строкой «Служба не запустилась».

В архиве: каталоги 0755, файлы 0644, скрипты 0755, владелец root. Не попадают:
``.git``, прежние архивы, руководство ``.docx``, выгрузки, кэши, данные и
секреты. В каталоге с git берутся файлы, которые git отслеживает или не
игнорирует (неотслеживаемые перечисляются при сборке — проверьте, что среди
них нет лишнего); без git — всё, кроме перечисленного ниже.

Перед записью архив проверяется: права, обязательные файлы и то, что номер
версии одинаков в ``VERSION``, ``pyproject.toml`` и ``mailarchiver/version.py``.
"""
from __future__ import annotations

import fnmatch
import gzip
import os
import re
import subprocess
import sys
import sysconfig
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP = "mail_backup"
WHEELS = "wheels"          # каталог пакетов в автономном архиве (его ищут install.sh и update.sh)

# каталоги, которые не входят в выпуск, где бы ни лежали
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "venv", ".venv",
             ".idea", ".vscode", "node_modules"}
# каталоги верхнего уровня: рабочие данные, остатки сборки и рабочие снимки экрана
# (tests/data и подобное — не они)
# (wheels — пакеты для установки без сети: в архив они попадают только через --wheels)
SKIP_TOP_DIRS = {"exports", "data", "tmp", "build", "dist", "wheels", "Claude outputs"}
# файлы по маске имени
# (*.whl — пакеты для установки без сети, где бы в проекте они ни лежали: каталог,
# названный не wheels/ — например «--wheels-dir wheelhouse», — попадал в архив
# второй раз. Пакеты, заданные через --wheels, добавляются отдельно.)
SKIP_FILES = ("*.tar.gz", "*.part", "*.docx", "*.pyc", "*.pyo", "*.pst", "*.ost", "*.mbox", "*.db",
              "*.db-wal", "*.db-shm", "*.db-journal", "*.log", "*.whl", "secret.key", "storage.key",
              ".DS_Store")
# файлы по пути от корня проекта
SKIP_PATHS = {"config/config.yaml"}
# без этих файлов архив не годится для установки
REQUIRED = ("VERSION", "requirements.txt", "pyproject.toml", "mailarchiver/__init__.py",
            "mailarchiver/version.py", "scripts/install.sh", "scripts/update.sh", "scripts/common.sh",
            "systemd/mailarchiver.service", "config/config.example.yaml")


def _skipped(rel: str) -> bool:
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] in SKIP_TOP_DIRS:
        return True
    if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts[:-1]):
        return True
    if rel in SKIP_PATHS:
        return True
    return any(fnmatch.fnmatch(parts[-1], mask) for mask in SKIP_FILES)


def _git(*args: str) -> list[str] | None:
    try:
        out = subprocess.run(["git", "-C", ROOT, *args, "-z"], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        reason = (getattr(exc, "stderr", b"") or str(exc).encode()).decode("utf-8", "replace")
        print("ВНИМАНИЕ: git не ответил (%s) — беру все файлы каталога, кроме исключённых."
              % (reason.strip().splitlines() or ["причина неизвестна"])[-1], file=sys.stderr)
        return None
    return [name for name in out.decode("utf-8", "surrogateescape").split("\0") if name]


def _walk_files() -> list[str]:
    found = []
    for base, dirs, files in os.walk(ROOT):
        top = base == ROOT
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")
                         and not (top and d in SKIP_TOP_DIRS))
        for name in files:
            found.append(os.path.relpath(os.path.join(base, name), ROOT).replace(os.sep, "/"))
    return found


def release_files() -> tuple[list[str], list[str]]:
    """→ (файлы выпуска, те из них, которых git ещё не отслеживает)."""
    names, untracked = None, []
    if os.path.exists(os.path.join(ROOT, ".git")):
        tracked = _git("ls-files")
        extra = _git("ls-files", "-o", "--exclude-standard") if tracked is not None else None
        if tracked is not None and extra is not None:
            names, untracked = tracked + extra, extra
    if names is None:
        names = _walk_files()

    def usable(name: str) -> bool:
        path = os.path.join(ROOT, name)
        return not _skipped(name) and os.path.isfile(path) and not os.path.islink(path)
    return sorted({n for n in names if usable(n)}), sorted(n for n in untracked if usable(n))


def _mode(rel: str, path: str) -> int:
    """0755 — скриптам и тому, что помечено исполняемым; остальному 0644."""
    if rel.endswith(".sh") or os.stat(path).st_mode & 0o100:
        return 0o755
    return 0o644


def _entry(name: str, *, mode: int, mtime: float, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = mode
    info.mtime = int(mtime)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def _versions() -> dict[str, str]:
    found = {}
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as fh:
        found["VERSION"] = fh.read().strip()
    for rel, pattern in (("pyproject.toml", r'(?m)^version\s*=\s*"([^"]+)"'),
                         ("mailarchiver/version.py", r'(?m)^__version__\s*=\s*"([^"]+)"')):
        path = os.path.join(ROOT, rel)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                match = re.search(pattern, fh.read())
            if match:
                found[rel] = match.group(1)
    return found


def wheel_files(wheels_dir: str, version: str) -> list[str]:
    """Имена пакетов ``.whl`` из каталога — то, что войдёт в автономный архив.

    Отказ, если каталог для установки без сети не годится: нет пакета самого
    приложения нужной версии или в каталоге лежат архивы исходников (их pip без
    сети, как правило, собрать не может — для сборки он скачивает setuptools).
    """
    if not os.path.isdir(wheels_dir):
        raise SystemExit(f"Каталог с пакетами не найден: {wheels_dir}")
    names = sorted(os.listdir(wheels_dir))
    sdists = [n for n in names if n.endswith((".tar.gz", ".tar.bz2", ".zip"))]
    if sdists:
        raise SystemExit("В каталоге пакетов есть архивы исходников: %s.\nБез сети pip их не соберёт. "
                         "Получите готовые пакеты: pip wheel -r requirements.txt -w КАТАЛОГ" % ", ".join(sdists))
    wheels = [n for n in names if n.endswith(".whl") and os.path.isfile(os.path.join(wheels_dir, n))
              and not os.path.islink(os.path.join(wheels_dir, n))]
    # пакеты приложения других версий (остались от прежней сборки) в архиве не нужны
    stale = [n for n in wheels if n.startswith("mailarchiver-") and not n.startswith(f"mailarchiver-{version}-")]
    for name in stale:
        print(f"Пропущен пакет приложения другой версии: {name}")
    wheels = [n for n in wheels if n not in stale]
    if not any(n.startswith(f"mailarchiver-{version}-") for n in wheels):
        raise SystemExit(f"В каталоге {wheels_dir} нет пакета приложения mailarchiver-{version}-*.whl.\n"
                         "Соберите его: pip wheel . --no-deps -w КАТАЛОГ (или scripts/make_offline_bundle.sh).")
    return wheels


def _platform_family(tag: str) -> str:
    """``manylinux_2_17_x86_64`` → ``linux_x86_64``, ``musllinux_1_2_aarch64`` → ``musllinux_aarch64``."""
    match = re.fullmatch(r"(manylinux|musllinux|linux|macosx)(?:_?\d+(?:_\d+)?)?_(.+)", tag)
    if not match:
        return tag
    family = "linux" if match.group(1) in ("manylinux", "linux") else match.group(1)
    return f"{family}_{match.group(2)}"


def bundle_tags(wheels: list[str]) -> tuple[str, str]:
    """Метки Python и платформы для имени автономного архива.

    Берутся из имён пакетов (``имя-версия-python-abi-платформа.whl``):

    * Python — метка ``cpXY`` пакетов, собранных под конкретную версию
      интерпретатора (ABI не ``none`` и не ``abi3``), например ``cp312``;
    * платформа — семейство и архитектура платформенных пакетов без версии
      glibc: ``manylinux_2_17_x86_64`` и ``manylinux2014_x86_64`` дают
      ``linux_x86_64``.

    Если таких пакетов нет (все — «чистый Python» или abi3), метка берётся у
    интерпретатора, которым запущен сборщик: набор библиотек подбирался под
    него (``cp313``; ``sysconfig.get_platform()`` с заменой ``-`` и ``.`` на
    ``_``, например ``linux_x86_64``). Если пакеты собраны под несколько версий
    или платформ, метки перечисляются через ``_``.
    """
    pythons, platforms = set(), set()
    for name in wheels:
        parts = name[:-len(".whl")].split("-")
        if len(parts) < 5:
            continue                      # имя не по правилам — о платформе ничего не говорит
        py, abi, plat = parts[-3:]
        if not set(abi.split(".")) & {"none", "abi3"}:
            pythons.update(tag for tag in py.split(".") if re.fullmatch(r"cp\d+", tag))
        platforms.update(_platform_family(tag) for tag in plat.split(".") if tag != "any")
    pytag = "_".join(sorted(pythons)) or "cp%d%d" % sys.version_info[:2]
    platform = "_".join(sorted(platforms)) or re.sub(r"[-.]", "_", sysconfig.get_platform())
    return pytag, platform


def build(out_dir: str, wheels_dir: str | None = None) -> str:
    versions = _versions()
    version = versions["VERSION"]
    if len(set(versions.values())) != 1:
        raise SystemExit("Номер версии расходится: " + ", ".join(f"{k} — {v}" for k, v in versions.items()))
    files, untracked = release_files()
    missing = [name for name in REQUIRED if name not in files]
    if missing:
        raise SystemExit("В выпуск не попали обязательные файлы: " + ", ".join(missing))
    if untracked:
        print("В архив включены файлы, которых git ещё не отслеживает (%d):" % len(untracked))
        for name in untracked:
            print("    " + name)
    # путь в архиве → файл на диске
    sources = {name: os.path.join(ROOT, name) for name in files}
    archive = f"mailarchiver-{version}.tar.gz"
    wheels: list[str] = []
    if wheels_dir:
        wheels = wheel_files(wheels_dir, version)
        sources.update({f"{WHEELS}/{name}": os.path.join(wheels_dir, name) for name in wheels})
        archive = "mailarchiver-%s-offline-%s-%s.tar.gz" % ((version,) + bundle_tags(wheels))
    files = sorted(sources)
    target = os.path.join(out_dir, archive)
    tmp = target + ".part"
    dirs = {""}
    for name in files:
        while "/" in name:
            name = name.rsplit("/", 1)[0]
            dirs.add(name)
    mtimes = {name: os.stat(path).st_mtime for name, path in sources.items()}
    newest = max(mtimes.values())
    with open(tmp, "wb") as raw:
        # mtime=0 и пустое имя: один и тот же набор файлов даёт один и тот же архив
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed:
            with tarfile.open(fileobj=packed, mode="w", format=tarfile.GNU_FORMAT) as tar:
                entries = [(d, True) for d in dirs] + [(f, False) for f in files]
                for rel, is_dir in sorted(entries, key=lambda item: item[0].split("/")):
                    name = f"{TOP}/{rel}" if rel else TOP
                    if is_dir:
                        # время каталога — по самому свежему файлу в нём, а не по часам сборки
                        inside = [m for f, m in mtimes.items() if not rel or f.startswith(rel + "/")]
                        tar.addfile(_entry(name, mode=0o755, mtime=max(inside or [newest]), directory=True))
                        continue
                    path = sources[rel]
                    # пакеты — обычные файлы, какие бы права ни были у них в каталоге сборки
                    mode = 0o644 if rel.startswith(WHEELS + "/") else _mode(rel, path)
                    info = _entry(name, mode=mode, mtime=mtimes[rel])
                    info.size = os.path.getsize(path)
                    with open(path, "rb") as fh:
                        tar.addfile(info, fh)
    check(tmp, [f"{WHEELS}/{name}" for name in wheels])
    os.replace(tmp, target)
    return target


def check(path: str, extra: list[str] | tuple[str, ...] = ()) -> None:
    """Архив читается, в нём есть всё нужное и нет ни одного файла, закрытого для службы."""
    names = set()
    with tarfile.open(path) as tar:
        for member in tar:
            names.add(member.name)
            wanted = 0o755 if member.isdir() else 0o644
            if member.mode & wanted != wanted or member.mode & 0o022 or member.uid or member.gid:
                raise SystemExit(f"Неверные права в архиве: {member.name} {oct(member.mode)} "
                                 f"{member.uid}:{member.gid}")
    missing = [name for name in tuple(REQUIRED) + tuple(extra) if f"{TOP}/{name}" not in names]
    if missing:
        raise SystemExit("В архиве нет обязательных файлов: " + ", ".join(missing))
    if not extra and any(name.startswith(f"{TOP}/{WHEELS}/") for name in names):
        raise SystemExit(f"В обычный архив попал каталог {WHEELS}/ — пакеты добавляются только через --wheels.")


def _arguments(argv: list[str]) -> tuple[str, str | None]:
    """→ (каталог для архива, каталог с пакетами или None)."""
    out_dir, wheels_dir, args, i = None, None, argv[1:], 0

    def refuse(text: str):
        print(text, file=sys.stderr)
        print("Использование: python scripts/make_release.py [--wheels КАТАЛОГ] [КУДА_ПОЛОЖИТЬ_АРХИВ]", file=sys.stderr)
        raise SystemExit(2)
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            raise SystemExit(0)
        if arg == "--wheels" or arg.startswith("--wheels="):
            if "=" in arg:
                wheels_dir = arg.split("=", 1)[1]
            elif i + 1 < len(args):
                i += 1
                wheels_dir = args[i]
            if not wheels_dir:
                refuse("После --wheels нужен каталог с пакетами .whl.")
        elif arg.startswith("-"):
            refuse(f"Неизвестный параметр: {arg}")
        elif out_dir is None:
            out_dir = arg
        else:
            refuse(f"Лишний параметр: {arg}")
        i += 1
    return os.path.abspath(out_dir) if out_dir else ROOT, os.path.abspath(wheels_dir) if wheels_dir else None


def main(argv: list[str]) -> int:
    out_dir, wheels_dir = _arguments(argv)
    os.makedirs(out_dir, exist_ok=True)
    target = build(out_dir, wheels_dir)
    with tarfile.open(target) as tar:
        count = sum(1 for member in tar if member.isfile())
    print(f"{target}: {count} файлов, {os.path.getsize(target)} байт")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
