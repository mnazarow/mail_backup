"""
Раздел «Сотрудники»: разбор файла-выгрузки из кадровой системы (CSV/Excel) и
синхронизация карточек сотрудников с почтовыми ящиками.

Как это работает:
  1. администратор выгружает из 1С (или другой системы) список сотрудников в
     CSV или XLSX и загружает файл в интерфейсе (или указывает путь к файлу на
     сервере — тогда синхронизацию выполняет планировщик);
  2. :func:`parse_employee_file` приводит файл к списку словарей: заголовки
     распознаются по псевдонимам («ФИО», «Ф.И.О.», «full_name», …), кодировка и
     разделитель определяются автоматически;
  3. :func:`sync_employees` добавляет новых сотрудников и обновляет уже
     заведённых, при необходимости заводя для них ПОЧТОВЫЕ ЯЩИКИ.

Важные правила (согласованы с заказчиком и менять их нельзя без его решения):
  * синхронизация только ДОБАВЛЯЕТ и ОБНОВЛЯЕТ. Сотрудник, исчезнувший из
    файла (уволенный), не архивируется и не трогается: решение об увольнении
    принимает человек, а не выгрузка, в которой могла потеряться строка;
  * пустое значение в файле НЕ затирает заполненное поле в карточке;
  * ящики для новых сотрудников создаются ВЫКЛЮЧЕННЫМИ и без пароля —
    администратор задаёт пароль и включает ящик вручную.
"""
from __future__ import annotations

import base64
import csv
import io
import os
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import ValidationError
from .logging_setup import get_logger
from .models import Account, AuthType, JobType, ScheduleKind, Security
from .util import human_size, utcnow_iso
from .version import __version__

log = get_logger("employees")

#: Кодировки, которыми пробуем читать CSV (по очереди). utf-8 обязательно до
#: cp1251: cp1251 «читается» почти из любых байтов и молча даёт кракозябры.
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")

#: Разделители, которые распознаём в CSV (в порядке предпочтения при равенстве).
CSV_DELIMITERS = (",", ";", "\t")

#: Расширения, которые читаем через openpyxl.
EXCEL_EXTENSIONS = (".xlsx", ".xlsm")

#: Сколько байт максимум принимаем при загрузке выгрузки по URL. Совпадает с
#: лимитом загрузки файла через интерфейс: источник тот же, ограничение должно
#: быть одинаковым, иначе один и тот же файл проходил бы по ссылке и не
#: проходил кнопкой «Импорт из файла».
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

#: Таймаут ожидания ответа от сервера-источника по умолчанию, секунды.
DEFAULT_URL_TIMEOUT_S = 60

#: Псевдонимы заголовков: нормализованное имя колонки -> поле карточки.
#: Нормализация — нижний регистр без пробелов (см. :func:`_norm_header`).
COLUMN_ALIASES: Dict[str, str] = {}
for _field, _names in {
    "full_name": ("фио", "ф.и.о.", "имя", "сотрудник", "full_name", "name"),
    "email": ("email", "e-mail", "почта", "адрес", "mail"),
    "position": ("должность", "position", "title"),
    "department": ("отдел", "подразделение", "department"),
    "phone": ("телефон", "phone", "тел"),
    "external_id": ("id", "табельный", "табельный номер", "external_id", "код"),
}.items():
    for _name in _names:
        COLUMN_ALIASES[_name.replace(" ", "")] = _field

#: Поля, которые переносятся из файла в карточку сотрудника.
FILE_FIELDS = ("full_name", "email", "position", "department", "phone", "external_id")

#: Имя и содержимое файла-образца (отдаётся по GET /api/employees/template.csv).
#: Разделитель «;» — так выгрузку открывает Excel с русской локалью.
TEMPLATE_FILENAME = "sotrudniki-obrazec.csv"
TEMPLATE_CSV = (
    "Табельный номер;ФИО;E-mail;Должность;Отдел;Телефон\r\n"
    "1024;Иванов Иван Иванович;ivanov@example.ru;Менеджер;Продажи;+7 900 000-00-00\r\n"
)

_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;.]+$")


# ---------------------------------------------------------------------------
#  Мелкие помощники
# ---------------------------------------------------------------------------
def _norm_header(value: Any) -> str:
    """Привести заголовок к сравнимому виду: нижний регистр без пробелов."""
    return re.sub(r"\s+", "", str(value or "")).lower()


def _cell(value: Any) -> str:
    """Значение ячейки как строка. Excel отдаёт числа и даты, а табельный
    номер «1024» приходит как 1024.0 — приводим к «1024», а не к «1024.0»."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def normalize_email(value: Any) -> str:
    """Почта в каноническом виде: без пробелов и угловых скобок, в нижнем регистре."""
    text = _cell(value).strip().strip("<>").strip()
    return text.lower()


def looks_like_email(value: str) -> bool:
    """Похоже ли значение на адрес: есть «@» и точка в доменной части."""
    return bool(_EMAIL_RE.match(value or ""))


# ---------------------------------------------------------------------------
#  Чтение файла
# ---------------------------------------------------------------------------
def _as_bytes(path_or_bytes: Any) -> bytes:
    """Принять и путь к файлу, и уже прочитанное содержимое."""
    if isinstance(path_or_bytes, (bytes, bytearray, memoryview)):
        return bytes(path_or_bytes)
    path = str(path_or_bytes)
    if not os.path.isfile(path):
        raise ValidationError(f"Файл не найден: {path}",
                              hint="Проверьте путь в настройке «Файл-источник» раздела «Сотрудники».")
    with open(path, "rb") as fh:
        return fh.read()


def _decode_csv(data: bytes) -> str:
    """Расшифровать CSV, перебирая типовые кодировки выгрузок."""
    for enc in CSV_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    # Ни одна кодировка не подошла — читаем с заменой «битых» байтов, чтобы
    # администратор увидел хотя бы часть строк и понял, что с файлом не так.
    log.warning("Не удалось определить кодировку файла сотрудников, читаем с заменой символов.")
    return data.decode("utf-8", errors="replace")


def _detect_delimiter(text: str) -> str:
    """Определить разделитель по строке заголовков (считаем вхождения)."""
    header = ""
    for line in text.splitlines():
        if line.strip():
            header = line
            break
    best, best_count = CSV_DELIMITERS[0], 0
    for delim in CSV_DELIMITERS:
        count = header.count(delim)
        if count > best_count:
            best, best_count = delim, count
    if best_count:
        return best
    # Одна колонка (или экзотический разделитель) — пусть решает csv.Sniffer.
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters="".join(CSV_DELIMITERS)).delimiter
    except csv.Error:
        return CSV_DELIMITERS[0]


def _read_csv_table(data: bytes) -> List[List[str]]:
    text = _decode_csv(data)
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [[_cell(c) for c in row] for row in reader]


def _read_excel_table(data: bytes) -> List[List[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValidationError(
            "Для файлов Excel нужен пакет openpyxl",
            hint="Установите его в окружение сервиса: pip install openpyxl — "
                 "или сохраните выгрузку в формате CSV, он читается без дополнительных пакетов.",
        ) from exc
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl бросает свои типы ошибок
        raise ValidationError(f"Не удалось прочитать файл Excel: {exc}",
                              hint="Откройте файл в Excel и пересохраните как .xlsx (или как CSV).") from exc
    try:
        ws = wb.worksheets[0]   # первый лист
        return [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  Загрузка списка по URL
# ---------------------------------------------------------------------------
def _filename_from_disposition(value: str) -> str:
    """Достать имя файла из заголовка ``Content-Disposition``.

    Понимаем обе формы: ``filename="emp.csv"`` и ``filename*=UTF-8''emp.csv``
    (RFC 5987) — вторую отдают почти все современные выгрузки с кириллицей.
    """
    if not value:
        return ""
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.I)
    if match:
        raw = match.group(1).strip().strip('"')
        parts = raw.split("'", 2)
        if len(parts) == 3:
            charset, encoded = parts[0] or "utf-8", parts[2]
        else:
            charset, encoded = "utf-8", raw
        try:
            return urllib.parse.unquote(encoded, encoding=charset, errors="replace")
        except (LookupError, ValueError):
            return urllib.parse.unquote(encoded, errors="replace")
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if match:
        return match.group(1)
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if match:
        return match.group(1).strip()
    return ""


def _guess_source_name(fmt: str, disposition: str, content_type: str, url: str, data: bytes) -> str:
    """Подобрать имя файла для скачанной выгрузки — по нему выбирается парсер.

    Порядок: явная настройка формата → ``Content-Disposition`` → путь в URL →
    ``Content-Type``. В конце имя сверяется с содержимым: XLSX всегда начинается
    с ZIP-сигнатуры ``PK``, поэтому подмену расширения видно сразу. Это важно:
    выгрузка по адресу вида ``/export?type=xlsx`` часто приходит без всяких
    подсказок, а неверно выбранный парсер даёт невнятную ошибку разбора.
    """
    fmt = (fmt or "auto").strip().lower()
    if fmt == "csv":
        return "employees.csv"
    if fmt in ("xlsx", "excel"):
        return "employees.xlsx"

    name = os.path.basename(_filename_from_disposition(disposition)).strip().strip('"')
    if not name.lower().endswith(EXCEL_EXTENSIONS + (".csv", ".txt", ".tsv", ".xls")):
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path or "")
        candidate = os.path.basename(path).strip()
        if candidate.lower().endswith(EXCEL_EXTENSIONS + (".csv", ".txt", ".tsv", ".xls")):
            name = candidate
    if not name:
        ctype = (content_type or "").split(";", 1)[0].strip().lower()
        if "spreadsheetml" in ctype or ctype in ("application/vnd.ms-excel", "application/x-xls"):
            name = "employees.xlsx"
        else:
            name = "employees.csv"

    is_zip = data[:4] == b"PK\x03\x04"
    if is_zip and not name.lower().endswith(EXCEL_EXTENSIONS):
        name = "employees.xlsx"
    elif not is_zip and name.lower().endswith(EXCEL_EXTENSIONS):
        name = "employees.csv"
    return name


def _reject_html_page(data: bytes, url: str) -> None:
    """Отбить HTML-страницу вместо выгрузки.

    Самый частый случай отказа: адрес закрыт авторизацией, и сервер отвечает
    кодом 200, но отдаёт форму входа. Без этой проверки администратор увидел бы
    «в файле не найдены колонки ФИО и E-mail» и искал бы ошибку в кадровой
    системе.
    """
    head = data[:512].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<head" in head[:200]:
        raise ValidationError(
            f"По адресу {url} вернулась HTML-страница, а не файл со списком сотрудников.",
            hint="Обычно так отвечает страница входа: проверьте, что ссылка ведёт прямо на файл "
                 "и что заданы логин и пароль, если источник закрыт авторизацией.")


def fetch_employee_source(
    url: str,
    *,
    username: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout_s: int = DEFAULT_URL_TIMEOUT_S,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    fmt: str = "auto",
) -> Tuple[bytes, str]:
    """Скачать выгрузку сотрудников по HTTP(S).

    :param url: адрес файла (``http://`` или ``https://``);
    :param username, password: логин и пароль HTTP Basic, если источник закрыт;
    :param verify_ssl: проверять сертификат сервера (отключайте только для
        внутреннего сервера с самоподписанным сертификатом);
    :param timeout_s: таймаут соединения и чтения, секунды;
    :param max_bytes: сколько байт согласны принять — защита от того, что по
        адресу окажется не выгрузка, а многогигабайтный дамп;
    :param fmt: ``auto`` | ``csv`` | ``xlsx`` — чем разбирать ответ;
    :returns: ``(содержимое, имя файла)`` — имя нужно
        :func:`parse_employee_file`, чтобы выбрать парсер.

    Используется только стандартная библиотека: лишняя зависимость ради одного
    GET-запроса усложнила бы установку в закрытом контуре.
    """
    url = (url or "").strip()
    if not url:
        raise ValidationError("Не задан адрес выгрузки сотрудников.",
                              hint="Заполните «Адрес выгрузки (URL)» в настройках, раздел «Сотрудники».")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValidationError(f"Неподдерживаемый адрес: «{url}».",
                              hint="Адрес должен начинаться с http:// или https://. "
                                   "Для файла на диске выберите источник «Файл на сервере».")
    if not parts.netloc:
        raise ValidationError(f"В адресе «{url}» не указан сервер.",
                              hint="Пример правильного адреса: https://hr.example.ru/export/employees.csv")

    timeout = max(5, int(timeout_s or DEFAULT_URL_TIMEOUT_S))
    limit = max(1, int(max_bytes or MAX_DOWNLOAD_BYTES))

    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", f"MailArchiver/{__version__} (employee sync)")
    request.add_header("Accept", "text/csv, application/vnd.openxmlformats-officedocument."
                                 "spreadsheetml.sheet, text/plain, */*")
    if username:
        token = base64.b64encode(f"{username}:{password or ''}".encode("utf-8")).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")

    context = None
    if parts.scheme == "https":
        context = ssl.create_default_context()
        if not verify_ssl:
            # Внутренние кадровые сервисы часто живут с самоподписанным
            # сертификатом; отключение проверки — осознанный выбор администратора.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

    log.info("Загрузка списка сотрудников по адресу %s", url)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ValidationError(
                    f"Файл по адресу слишком большой: {human_size(int(declared))} "
                    f"при допустимых {human_size(limit)}.",
                    hint="Убедитесь, что ссылка ведёт на выгрузку сотрудников, а не на архив или дамп базы.")
            data = response.read(limit + 1)
            disposition = response.headers.get("Content-Disposition", "")
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl() or url
    except urllib.error.HTTPError as exc:
        reason = (getattr(exc, "reason", "") or "").strip()
        hint = "Проверьте адрес в настройках раздела «Сотрудники»."
        if exc.code in (401, 403):
            hint = ("Источник требует авторизации: заполните «Логин источника» и «Пароль источника» "
                    "в настройках, раздел «Сотрудники».")
        elif exc.code == 404:
            hint = "Файл по этому адресу не найден — уточните ссылку у администратора кадровой системы."
        elif exc.code >= 500:
            hint = "Ошибка на стороне сервера-источника. Повторите позже или сообщите его администратору."
        raise ValidationError(f"Сервер ответил ошибкой {exc.code}{': ' + reason if reason else ''}.",
                              hint=hint) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        hint = "Проверьте адрес, доступность сервера и настройки сети (прокси, firewall, DNS)."
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE" in text.upper():
            hint = ("Сертификат сервера не прошёл проверку. Если это внутренний сервер с "
                    "самоподписанным сертификатом, выключите «Проверять сертификат источника».")
        raise ValidationError(f"Не удалось подключиться к {url}: {text}", hint=hint) from exc
    except socket.timeout as exc:
        raise ValidationError(f"Сервер не ответил за {timeout} с.",
                              hint="Увеличьте «Таймаут загрузки» в настройках либо проверьте, "
                                   "не формируется ли выгрузка слишком долго.") from exc
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Не удалось загрузить {url}: {exc}",
                              hint="Проверьте адрес и доступность сервера-источника.") from exc

    if len(data) > limit:
        raise ValidationError(
            f"Файл по адресу больше допустимых {human_size(limit)}.",
            hint="Убедитесь, что ссылка ведёт на выгрузку сотрудников, а не на архив или дамп базы.")
    if not data:
        raise ValidationError(f"По адресу {url} вернулся пустой ответ.",
                              hint="Проверьте, формируется ли выгрузка на стороне кадровой системы.")
    _reject_html_page(data, url)

    name = _guess_source_name(fmt, disposition, content_type, final_url, data)
    log.info("Загружено %d байт, разбираем как «%s»", len(data), name)
    return data, name


def load_employee_source(
    *,
    source_type: str,
    path: str = "",
    url: str = "",
    username: str = "",
    password: str = "",
    verify_ssl: bool = True,
    timeout_s: int = DEFAULT_URL_TIMEOUT_S,
    fmt: str = "auto",
) -> Tuple[List[dict], List[dict], str]:
    """Получить строки сотрудников из выбранного источника (файл или URL).

    Единая точка входа для планировщика и кнопки «Синхронизировать»: обе должны
    вести себя одинаково, поэтому выбор источника живёт здесь, а не в каждом
    вызывающем месте.

    :returns: ``(rows, problems, origin)`` — ``origin`` описывает источник
        человеческим текстом для журнала задания.
    """
    if (source_type or "file").strip().lower() == "url":
        data, name = fetch_employee_source(
            url, username=username, password=password, verify_ssl=verify_ssl,
            timeout_s=timeout_s, fmt=fmt)
        rows, problems = parse_employee_file(data, name)
        return rows, problems, f"URL {url}"

    path = (path or "").strip()
    if not path:
        raise ValidationError(
            "Не задан файл со списком сотрудников.",
            hint="Укажите путь к файлу CSV/XLSX в настройках, раздел «Сотрудники» → «Файл-источник», "
                 "либо выберите источник «Адрес (URL)».")
    if not os.path.isfile(path):
        raise ValidationError(
            f"Файл со списком сотрудников не найден: {path}",
            hint="Проверьте путь и права доступа. Файл читает служба mailarchiver.")
    rows, problems = parse_employee_file(path, os.path.basename(path))
    return rows, problems, f"файл «{os.path.basename(path)}»"


# ---------------------------------------------------------------------------
#  Разбор файла
# ---------------------------------------------------------------------------
def parse_employee_file(path_or_bytes: Any, filename: str = "") -> Tuple[List[dict], List[dict]]:
    """Разобрать файл сотрудников.

    :param path_or_bytes: путь к файлу либо его содержимое (bytes);
    :param filename: имя файла — по расширению выбирается формат (CSV/Excel);
    :returns: ``(rows, problems)``, где ``rows`` — список словарей с полями
        FILE_FIELDS и номером строки в файле (``row``), а ``problems`` —
        список ``{"row": N, "reason": "..."}`` по строкам, которые пропущены
        или вызвали вопросы.
    """
    name = (filename or (path_or_bytes if isinstance(path_or_bytes, str) else "")).lower()
    data = _as_bytes(path_or_bytes)
    if name.endswith(EXCEL_EXTENSIONS):
        table = _read_excel_table(data)
    elif name.endswith(".xls"):
        raise ValidationError("Старый формат Excel (.xls) не поддерживается.",
                              hint="Откройте файл и сохраните как .xlsx или CSV.")
    else:
        table = _read_csv_table(data)
    return _rows_from_table(table)


def _rows_from_table(table: Sequence[Sequence[str]]) -> Tuple[List[dict], List[dict]]:
    """Превратить таблицу (первая строка — заголовки) в строки карточек."""
    rows: List[dict] = []
    problems: List[dict] = []

    header_idx = -1
    for idx, raw in enumerate(table):
        if any(_cell(c) for c in raw):
            header_idx = idx
            break
    if header_idx < 0:
        raise ValidationError("Файл пуст — в нём нет ни одной строки с данными.",
                              hint="Выгрузите список сотрудников заново или скачайте файл-образец.")

    mapping: Dict[int, str] = {}
    for col, title in enumerate(table[header_idx]):
        field = COLUMN_ALIASES.get(_norm_header(title))
        if field and field not in mapping.values():
            mapping[col] = field
    if "full_name" not in mapping.values() and "email" not in mapping.values():
        raise ValidationError(
            "В файле не найдены колонки «ФИО» и «E-mail».",
            hint="Первая строка должна содержать заголовки. Подойдут названия: "
                 "ФИО / Ф.И.О. / Имя / Сотрудник / Name, E-mail / Почта / Mail. "
                 "Скачайте файл-образец, если не уверены в формате.",
        )

    for idx in range(header_idx + 1, len(table)):
        raw = table[idx]
        line_no = idx + 1          # нумерация как в Excel: заголовок — строка 1
        if not any(_cell(c) for c in raw):
            continue               # пустые строки просто пропускаем
        item = {"row": line_no}
        for field in FILE_FIELDS:
            item[field] = ""
        for col, field in mapping.items():
            if col < len(raw):
                item[field] = _cell(raw[col]).strip()

        email = normalize_email(item["email"])
        if email and not looks_like_email(email):
            problems.append({"row": line_no, "reason": f"некорректный e-mail: «{item['email']}»"})
            email = ""
        item["email"] = email

        if not item["full_name"] and not item["email"]:
            # Строка без ФИО и без почты — опознать сотрудника нечем.
            if not any(p["row"] == line_no for p in problems):
                problems.append({"row": line_no, "reason": "нет ФИО и e-mail"})
            continue
        rows.append(item)
    return rows, problems


# ---------------------------------------------------------------------------
#  Синхронизация
# ---------------------------------------------------------------------------
#: Подстановки, доступные в шаблонах полей создаваемого ящика.
ACCOUNT_PLACEHOLDERS = ("email", "local", "domain", "full_name", "first_name",
                        "last_name", "position", "department", "external_id")


def account_placeholders(full_name: str = "", email: str = "", row: Optional[dict] = None) -> Dict[str, str]:
    """Собрать значения подстановок для шаблонов ящика.

    ``local``/``domain`` — части адреса до и после «@»: по ним собирают логин,
    когда он не совпадает с почтой (частый случай — вход по ``ivanov``, а не
    по ``ivanov@company.ru``).
    """
    row = row or {}
    email = (email or "").strip()
    local, _, domain = email.partition("@")
    full_name = (full_name or "").strip()
    name_parts = full_name.split()
    return {
        "email": email,
        "local": local,
        "domain": domain,
        "full_name": full_name,
        "last_name": name_parts[0] if name_parts else "",
        "first_name": name_parts[1] if len(name_parts) > 1 else "",
        "position": str(row.get("position") or "").strip(),
        "department": str(row.get("department") or "").strip(),
        "external_id": str(row.get("external_id") or "").strip(),
    }


def render_account_field(template: str, values: Dict[str, str]) -> str:
    """Подставить значения в шаблон поля ящика.

    Неизвестная подстановка не роняет синхронизацию: ``{отдел}`` останется в
    строке как есть, и администратор увидит опечатку в названии ящика, а не
    сломанное задание. Двойные пробелы, которые остаются от пустых полей
    (например от «{full_name} ({department})» без отдела), схлопываются.
    """
    text = str(template or "")
    for key in ACCOUNT_PLACEHOLDERS:
        text = text.replace("{" + key + "}", values.get(key, ""))
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text).strip(" -–—,;")
    return text.strip()


def account_template(svc) -> Dict[str, Any]:
    """Шаблон настроек создаваемых ящиков (раздел «Сотрудники» в настройках).

    Значения приводятся к нужным типам здесь, а не в месте использования:
    настройки правит человек, и «993 » или «SSL» не должны приводить к ящику,
    который не подключается.
    """
    def _text(key: str, default: str = "") -> str:
        value = svc.rt("employees", key)
        return str(default if value is None else value).strip()

    def _list(key: str) -> List[str]:
        value = svc.rt("employees", key)
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        return [str(item).strip() for item in (value or []) if str(item).strip()]

    try:
        port = int(svc.rt("employees", "account_port") or 993)
    except (TypeError, ValueError):
        port = 993
    security = _text("account_security", Security.SSL).lower()
    if security not in Security.ALL:
        security = Security.SSL
    try:
        retention_days = int(svc.rt("employees", "account_retention_days"))
    except (TypeError, ValueError):
        retention_days = -1

    return {
        "host": _text("account_host"),
        "port": port,
        "security": security,
        "name_template": _text("account_name_template", "{full_name}") or "{full_name}",
        "username_template": _text("account_username_template", "{email}") or "{email}",
        "notes_template": _text("account_notes_template"),
        "enabled": bool(svc.rt("employees", "account_enabled")),
        "folder_include": _list("account_folder_include"),
        "folder_exclude": _list("account_folder_exclude"),
        "retention_days": retention_days,
        "schedule_enabled": bool(svc.rt("employees", "account_schedule_enabled")),
        "schedule_cron": _text("account_schedule_cron", "0 2 * * *") or "0 2 * * *",
    }


#: Пример сотрудника для предпросмотра шаблона: заполнены все поля, поэтому
#: сразу видно, как отработает каждая подстановка.
PREVIEW_EMPLOYEE = {"position": "Менеджер", "department": "Отдел продаж", "external_id": "1234"}


def preview_account_template(svc, full_name: str = "Иванов Иван Иванович",
                             email: str = "ivanov@example.ru", row: Optional[dict] = None) -> Dict[str, Any]:
    """Как будет выглядеть ящик, созданный по шаблону, — для проверки настроек."""
    tpl = account_template(svc)
    values = account_placeholders(full_name, email, dict(PREVIEW_EMPLOYEE) if row is None else row)
    return {
        "name": render_account_field(tpl["name_template"], values) or (full_name or email),
        "username": render_account_field(tpl["username_template"], values) or email,
        "notes": render_account_field(tpl["notes_template"], values),
        "host": tpl["host"], "port": tpl["port"], "security": tpl["security"],
        "enabled": tpl["enabled"], "folder_include": tpl["folder_include"],
        "folder_exclude": tpl["folder_exclude"], "retention_days": tpl["retention_days"],
        "schedule_enabled": tpl["schedule_enabled"], "schedule_cron": tpl["schedule_cron"],
    }


def ensure_account_for_employee(svc, employee_id: int, full_name: str, email: str,
                                row: Optional[dict] = None) -> Tuple[Optional[int], str]:
    """Завести (или найти) почтовый ящик для сотрудника и привязать его.

    Ящик создаётся по ШАБЛОНУ из настроек раздела «Сотрудники»: сервер, порт,
    шифрование, название и логин по шаблонам, фильтры папок, срок хранения и —
    при желании — расписание копирования. Пароль в шаблон не входит и никогда
    не подставляется: его знает только администратор.

    :param row: строка выгрузки (должность, отдел, табельный номер) — нужна
        подстановкам в шаблонах названия и заметки;
    :returns: ``(account_id, action)``, где action — ``"created"``,
        ``"linked"`` или ``""`` (ничего не делали).
    """
    if not email:
        return None, ""
    tpl = account_template(svc)
    values = account_placeholders(full_name, email, row)
    username = render_account_field(tpl["username_template"], values) or email

    existing = svc.db.get_account_by_username(username)
    if existing is None and username != email:
        # Логин по шаблону мог измениться уже после того, как ящик завели:
        # проверяем и по адресу, иначе сотруднику создали бы второй ящик.
        existing = svc.db.get_account_by_username(email)
    if existing is not None:
        svc.db.set_employee_account(employee_id, existing.id)
        return existing.id, "linked"

    acc = Account(
        name=render_account_field(tpl["name_template"], values) or full_name or email,
        host=tpl["host"], port=tpl["port"], username=username, password="",
        auth_type=AuthType.PASSWORD, security=tpl["security"],
        # Пустой пароль означает, что копирование всё равно не начнётся: даже
        # включённый по шаблону ящик ждёт, пока администратор впишет пароль.
        enabled=bool(tpl["enabled"]),
        folder_include=list(tpl["folder_include"]),
        folder_exclude=list(tpl["folder_exclude"]),
        retention_days=int(tpl["retention_days"]),
        notes=render_account_field(tpl["notes_template"], values),
    )
    account_id = svc.db.create_account(acc)
    svc.db.set_employee_account(employee_id, account_id)

    if tpl["schedule_enabled"]:
        # Расписание — часть шаблона: без него новый ящик так и не попал бы в
        # копирование, пока администратор не завёл бы расписание руками.
        try:
            svc.db.create_schedule(account_id, ScheduleKind.CRON, JobType.BACKUP,
                                   cron_expr=tpl["schedule_cron"], enabled=True)
            if getattr(svc, "scheduler", None) is not None:
                svc.scheduler.reload()
        except Exception as exc:  # noqa: BLE001
            # Ящик уже создан — падать из-за расписания нельзя, иначе
            # синхронизация оборвётся на середине файла.
            log.warning("Не удалось создать расписание для ящика %s: %s", username, exc)

    return account_id, "created"


def sync_employees(svc, rows: Sequence[dict], *, create_accounts: bool,
                   progress_cb: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """Применить разобранные строки файла к справочнику сотрудников.

    Сопоставление с уже заведёнными: сначала по табельному номеру
    (``external_id``), затем по почте (без учёта регистра). Уволенные, то есть
    исчезнувшие из файла, НЕ трогаются — см. заголовок модуля.
    """
    db = svc.db
    result: Dict[str, Any] = {
        "created": 0, "updated": 0, "accounts_created": 0, "accounts_linked": 0,
        "total_rows": len(rows), "problems": [],
    }
    total = len(rows)
    for num, row in enumerate(rows, 1):
        line_no = int(row.get("row") or num)
        full_name = (row.get("full_name") or "").strip()
        email = normalize_email(row.get("email"))
        external_id = (row.get("external_id") or "").strip()
        if not full_name and not email:
            result["problems"].append({"row": line_no, "reason": "нет ФИО и e-mail"})
            continue

        existing = db.get_employee_by_external_id(external_id) if external_id else None
        if existing is None and email:
            existing = db.get_employee_by_email(email)
        if existing is None and not external_id and not email:
            # Ни табельного номера, ни почты — опознаём по ФИО среди таких же
            # карточек без признаков. Иначе такой сотрудник добавлялся бы
            # заново при каждой синхронизации (см. get_employee_by_full_name).
            existing = db.get_employee_by_full_name(full_name, only_unidentified=True)

        values = {
            "full_name": full_name or email,
            "email": email,
            "external_id": external_id,
            "position": (row.get("position") or "").strip(),
            "department": (row.get("department") or "").strip(),
            "phone": (row.get("phone") or "").strip(),
        }
        if existing is None:
            employee_id = db.create_employee(source="file", last_seen_at=utcnow_iso(), **values)
            account_id = None
            result["created"] += 1
        else:
            employee_id = int(existing["id"])
            account_id = existing["account_id"]
            # Пустые поля файла не затирают заполненные в карточке.
            changes = {k: v for k, v in values.items() if v and v != (existing[k] or "")}
            changes["last_seen_at"] = utcnow_iso()
            db.update_employee(employee_id, **changes)
            # «Обновлён» = встретился в файле (created + updated = разобранные строки).
            result["updated"] += 1

        if create_accounts and email and account_id is None:
            _, action = ensure_account_for_employee(svc, employee_id, values["full_name"], email,
                                                     row=values)
            if action == "created":
                result["accounts_created"] += 1
            elif action == "linked":
                result["accounts_linked"] += 1

        if progress_cb is not None:
            progress_cb(num, total, f"Обработано {num}/{total}")
    return result
