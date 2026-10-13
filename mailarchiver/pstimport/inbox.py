"""
Каталог импорта: файлы .pst и .ost, которые уже лежат на сервере.

Через веб-интерфейс файл больше предела загрузки не передать, поэтому большой
архив администратор кладёт на сервер сам — в каталог ``<каталог данных>/import``
— и выбирает его в окне импорта по имени.

Имя приходит из браузера, поэтому путь к файлу строится ТОЛЬКО здесь
(:func:`resolve`): годится лишь обычный файл, лежащий прямо в каталоге импорта.
Ни путь («../x.pst», «/etc/passwd»), ни символическая ссылка не проходят — иначе
импорт читал бы любой файл, доступный службе.

Файлы каталога принадлежат администратору: служба их только читает, не удаляет
и не меняет (:func:`protected` — проверка для уборки временных файлов).

Задание импорта помнит не путь, а имя файла и его приметы на момент выбора
(:func:`fingerprint`): файл с тем же именем, но другой — подменённый или ещё
не докопированный — импортирован не будет (:func:`changed`).
"""
from __future__ import annotations

import os
import re
import shlex
import stat
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..errors import ValidationError
from ..util import human_size

#: Расширения файлов данных Outlook, которые понимает импорт.
EXTENSIONS = (".pst", ".ost")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Сколько знаков имени показывать в сообщении об ошибке (имя приходит от клиента).
_SHOWN = 100


def service_user() -> str:
    """Пользователь, от имени которого работает служба — для подсказки про ``chown``."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, AttributeError, KeyError, OSError):
        return "mailarchiver"


def _name_problem(name: str) -> Optional[str]:
    """Почему имя не годится (``None`` — годится). Файловую систему не трогает."""
    if not name:
        return "empty"
    separators = {"/", "\\", os.sep, os.altsep or "/"}
    if any(sep in name for sep in separators) or _CONTROL.search(name) or name.startswith("."):
        return "name"
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # имя не в UTF-8 (на диске — произвольные байты): его не передать ни в JSON, ни обратно
        return "name"
    if not name.lower().endswith(EXTENSIONS):
        return "extension"
    return None


def _readable(path: str) -> bool:
    try:
        return os.access(path, os.R_OK)
    except (OSError, ValueError):
        return False


def available(directory: str) -> bool:
    """Может ли служба читать каталог импорта: он есть и открыт её пользователю.

    Каталог, созданный командой от root, или ссылка на отключённый диск дают
    пустой список файлов — интерфейсу нужно отличать это от «файлов нет».
    """
    try:
        return os.path.isdir(directory) and os.access(directory, os.R_OK | os.X_OK)
    except (OSError, ValueError):
        return False


def problem(cfg) -> Tuple[str, str]:
    """Почему из каталога импорта сейчас нельзя брать файлы: (вид, причина словами).

    ("", "") — можно. Вид нужен окну импорта, чтобы совет подходил к причине
    (ссылке в никуда не поможет ``mkdir``): ``overlap`` — каталог пересёкся с
    каталогом временных файлов, ``dangling`` — ссылка в никуда, ``missing`` —
    каталога нет, ``not_dir`` — на его месте файл, ``denied`` — закрыт путь к
    каталогу, ``closed`` — закрыт сам каталог, ``loop`` и ``error`` — прочее.
    """
    directory = cfg.import_dir
    overlap = cfg.import_overlap_text()
    if overlap:
        return "overlap", overlap
    found = cfg.dir_problem(directory, "Каталог импорта")
    if found:
        return found.kind, str(found)
    if not os.path.isdir(directory):
        return "missing", f"Каталога импорта {directory} нет"
    if not available(directory):
        return "closed", (f"Каталог импорта {directory} закрыт для службы — она работает от имени "
                          f"пользователя {service_user()}")
    return "", ""


def list_files(directory: str) -> List[Dict[str, object]]:
    """Файлы .pst/.ost прямо в каталоге импорта: имя, размер, время изменения (UTC, ISO).

    Вложенные каталоги, ссылки, скрытые и пустые файлы не показываются. Файл,
    который служба не может прочитать (чужой владелец), остаётся в списке с
    ``readable: False`` — иначе непонятно, почему скопированного файла «нет».
    """
    out: List[Dict[str, object]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if _name_problem(entry.name):
                    continue
                try:
                    if entry.is_symlink():
                        continue
                    info = entry.stat(follow_symlinks=False)
                    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat()
                except (OSError, ValueError, OverflowError):
                    continue          # файл исчез, пока читали каталог, или о нём ничего не узнать
                if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                    continue
                out.append({"name": entry.name, "size": int(info.st_size), "modified": modified,
                            "readable": _readable(entry.path)})
    except (OSError, ValueError):
        pass                          # каталога ещё нет или он недоступен — показывать нечего
    out.sort(key=lambda item: (str(item["name"]).lower(), str(item["name"])))
    return out


def shown(name: str) -> str:
    """Имя файла для сообщения: длинное обрезается (имя приходит от клиента)."""
    return name if len(name) <= _SHOWN else name[:_SHOWN] + "…"


def locate(directory: str, name: str, *, disclose: bool = True) -> Tuple[str, os.stat_result]:
    """Файл каталога импорта по имени, выбранному пользователем: полный путь и его ``lstat``.

    Бросает :class:`ValidationError`, если это не имя обычного непустого файла
    .pst/.ost, лежащего прямо в каталоге. Сам каталог импорта может быть ссылкой
    (на другой диск) — проверяется, что файл лежит в том каталоге, куда она ведёт.

    ``disclose=False`` — подсказки без путей и команд: так спрашивает задание,
    журнал которого виден и сотруднику, вошедшему по своему ящику, а ему знать
    каталоги сервера незачем.
    """
    name = name if isinstance(name, str) else ""
    problem = _name_problem(name)
    if problem == "empty":
        raise ValidationError("Файл на сервере не выбран.",
                              hint="Выберите файл из списка каталога импорта.")
    if problem == "name":
        raise ValidationError("Недопустимое имя файла.",
                              hint="Нужно только имя файла из каталога импорта — без пути, без точки в начале "
                                   "и без служебных знаков.")
    if problem == "extension":
        raise ValidationError("Ожидается файл .pst или .ost.",
                              hint="Из каталога импорта берутся только файлы данных Outlook с расширением "
                                   ".pst или .ost.")
    label = shown(name)
    root = os.path.realpath(directory)
    path = os.path.join(root, name)
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        raise ValidationError(
            f"Файл «{label}» не найден в каталоге импорта.",
            hint=(f"Каталог импорта на сервере: {directory}. Файл могли убрать или переименовать — положите "
                  f"его на место и повторите." if disclose else
                  "Файл могли убрать или переименовать: положите его в каталог импорта на сервере и запустите "
                  "импорт заново.")) from None
    if stat.S_ISLNK(info.st_mode):
        raise ValidationError(f"«{label}» — символическая ссылка, а не файл.",
                              hint="В каталог импорта нужно положить сам файл (скопировать или переместить). "
                                   "Если файлы лежат на другом диске, ссылкой можно сделать сам каталог импорта — "
                                   "но не на каталог в /home, /root или /tmp: служба под systemd их не видит.")
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"«{label}» — не файл.",
                              hint="Из каталога импорта берутся только обычные файлы .pst и .ost.")
    # Имя без разделителей и не ссылка — файл обязан лежать прямо в каталоге.
    # Проверка оставлена намеренно: от неё зависит, что прочитает импорт.
    if os.path.dirname(os.path.realpath(path)) != root:
        raise ValidationError(f"Файл «{label}» лежит вне каталога импорта.",
                              hint=(f"Положите сам файл в каталог импорта на сервере: {directory}." if disclose
                                    else "Положите сам файл в каталог импорта на сервере."))
    if info.st_size <= 0:
        raise ValidationError(f"Файл «{label}» пуст.",
                              hint="Возможно, копирование файла на сервер ещё не закончилось или оборвалось.")
    if not _readable(path):
        raise ValidationError(
            f"Служба не может прочитать файл «{label}»: нет прав.",
            hint=(f"Отдайте файл пользователю службы — выполните на сервере: "
                  f"sudo chown {service_user()}: {shlex.quote(path)}" if disclose else
                  "Отдайте файл пользователю, от имени которого работает служба (sudo chown), — точную команду "
                  "показывает окно «Импорт .pst» → «Взять с сервера»."))
    return path, info


def resolve(directory: str, name: str) -> str:
    """Полный путь к файлу каталога импорта по имени (см. :func:`locate`)."""
    return locate(directory, name)[0]


def fingerprint(info: os.stat_result) -> Dict[str, int]:
    """Приметы файла для параметров задания: размер, время изменения, устройство и номер файла.

    Задание привязано к ним, а не только к имени: почти все файлы называются
    «Outlook.pst», и замена файла между выбором и запуском (или «Повторить» через
    месяц) залила бы в ящик чужой архив.
    """
    return {"size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns),
            "dev": int(info.st_dev), "ino": int(info.st_ino)}


def changed(saved: object, info: os.stat_result) -> List[str]:
    """Чем файл отличается от запомненного при постановке задания (пусто — тот же файл).

    Примет нет у заданий, поставленных до их появления, — такие не проверяются.
    """
    if not isinstance(saved, dict):
        return []

    def was(key: str) -> Optional[int]:
        value = saved.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    out: List[str] = []
    size = was("size")
    if size is not None and size != info.st_size:
        before, now = human_size(size), human_size(info.st_size)
        if before == now:
            before, now = f"{size} байт", f"{info.st_size} байт"
        out.append(f"размер (был {before}, стал {now})")
    mtime = was("mtime_ns")
    if mtime is not None and mtime != info.st_mtime_ns:
        out.append("время изменения")
    # Номер файла сравним только на том же устройстве: после перезагрузки или нового
    # подключения диска номер устройства бывает другим, хотя файл тот же самый, —
    # отказывать из-за этого в «Повторить» незачем, размер и время уже сверены.
    dev, ino = was("dev"), was("ino")
    if dev is not None and ino is not None and dev == info.st_dev and ino != info.st_ino:
        out.append("сам файл — под этим именем лежит уже другой")
    return out


def protected(directory: str, path: str) -> bool:
    """Нельзя ли уборке трогать ``path`` из-за каталога импорта.

    Нельзя, если ``path`` лежит ПРЯМО в каталоге импорта (ссылка в нём — тоже его
    содержимое), если это сам каталог импорта или каталог, внутри которого он
    находится. Проверка намеренно не «где-то внутри»: каталог импорта бывает
    ссылкой на диск, где лежат и данные службы, и тогда «внутри» оказывался весь
    каталог временных файлов — уборка переставала работать совсем.
    """
    if not directory or not path or not isinstance(path, str):
        return False
    try:
        root = os.path.realpath(directory)
        full = os.path.abspath(path)
        real = os.path.realpath(full)
        parent = os.path.realpath(os.path.dirname(full))
    except (OSError, ValueError):
        return False
    return parent == root or real == root or root.startswith(real.rstrip(os.sep) + os.sep)
