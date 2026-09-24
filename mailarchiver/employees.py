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
import html
import http.client
import io
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import ValidationError
from .logging_setup import get_logger
from .models import Account, AuthType, JobType, ScheduleKind, Security, account_has_credentials
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
    "full_name": ("фио", "ф.и.о.", "имя", "сотрудник", "full_name", "name", "фамилияимяотчество",
                  "фамилия,имя,отчество", "фамилияиимя", "сотрудник(фио)", "фиосотрудника", "fullname"),
    # ФИО по отдельным колонкам — так выгружают кадровые системы и интранет.
    "last_name": ("фамилия", "last_name", "surname", "lastname"),
    "first_name": ("first_name", "firstname", "givenname"),
    "middle_name": ("отчество", "middle_name", "middlename", "patronymic"),
    # «Адрес» сам по себе здесь НЕ псевдоним: обычно это почтовый адрес, и он
    # перекрывал настоящую колонку E-mail. Колонку с адресами почты без
    # узнаваемого заголовка находим по содержимому (см. _map_header).
    "email": ("email", "e-mail", "почта", "электропочта", "эл.почта", "mail", "электроннаяпочта",
              "адресэлектроннойпочты", "e-mailадрес", "emailадрес", "корпоративнаяпочта",
              "почта(email)", "почта(e-mail)", "эл.адрес", "электронныйадрес", "emailaddress",
              "e-mailaddress", "mailaddress", "адресe-mail", "адресemail"),
    "position": ("должность", "position", "title"),
    "department": ("отдел", "подразделение", "department"),
    "phone": ("телефон", "phone", "тел"),
    "phone_mobile": ("тел.мобильный", "мобильный", "сотовый", "mobile", "cell"),
    "phone_work": ("тел.рабочий", "рабочий", "workphone"),
    "phone_ext": ("тел.внутренний", "внутренний", "добавочный", "extension"),
    "employed": ("работает", "активен", "статус", "active"),
    "employed_until": ("работаетдо", "датаувольнения"),
    # «Уволен»: либо да/нет, либо дата увольнения — разбирается в _rows_from_table
    "dismissed": ("уволен", "уволена", "dismissed", "fired"),
    "external_id": ("id", "табельный", "табельный номер", "external_id", "код"),
}.items():
    for _name in _names:
        COLUMN_ALIASES[_name.replace(" ", "")] = _field

#: Значения колонки «работает», означающие, что сотрудник уже НЕ работает.
NOT_EMPLOYED_VALUES = {"нет", "no", "0", "-", "уволен", "уволена", "false", "неактивен"}
#: …и что работает. Нужны, чтобы распознать колонку «да/нет» без заголовка.
EMPLOYED_VALUES = {"да", "yes", "1", "+", "true", "активен", "работает"}

#: Поля, которые распознаются в файле помимо основных (см. FILE_FIELDS).
EXTRA_FILE_FIELDS = ("last_name", "first_name", "middle_name",
                     "phone_mobile", "phone_work", "phone_ext",
                     "employed", "employed_until", "dismissed")

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
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        # «Юникод-текст» Excel — UTF-16 с меткой порядка байтов
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
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
    """Определить разделитель по первым строкам файла.

    Считаем, сколько полей даёт каждый разделитель в первых строках, и берём
    тот, при котором их число больше единицы и одинаково в большинстве строк.
    Раньше смотрели только на шапку и при равенстве брали «,»: заголовок
    «Фамилия, имя;E-mail» превращал ФИО в мусор. При равенстве предпочтение —
    «;» (так сохраняет Excel с русской локалью).
    """
    lines = [line for line in text.splitlines() if line.strip()][:20]
    best, best_score = None, (0, 0)
    for delim in (";", "\t", ","):
        try:
            counts = [len(row) for row in csv.reader(lines, delimiter=delim)]
        except csv.Error:
            continue
        if not counts:
            continue
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        score = (counts.count(modal), modal)
        if score > best_score:
            best, best_score = delim, score
    if best:
        return best
    # Одна колонка (или экзотический разделитель) — пусть решает csv.Sniffer.
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters="".join(CSV_DELIMITERS)).delimiter
    except csv.Error:
        return CSV_DELIMITERS[0]


def _read_csv_table(data: bytes) -> List[List[str]]:
    text = _decode_csv(data).replace("\x00", "")
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        return [[_cell(c) for c in row] for row in reader]
    except csv.Error as exc:
        raise ValidationError(f"Файл CSV не читается: {exc}",
                              hint="Проверьте, что это текстовая таблица (CSV), и что в ней нет "
                                   "огромных ячеек или незакрытых кавычек.") from exc


#: Признаки выгрузки в формате SpreadsheetML 2003 («XML-таблица» из Excel и
#: многих корпоративных систем). Определяем по содержимому, а не по имени
#: файла: по ссылке такой файл часто приходит как .xls, .xml или вовсе без
#: расширения.
SPREADSHEETML_MARKERS = (b"<?mso-application", b"<Workbook", b"urn:schemas-microsoft-com:office:spreadsheet")

#: Разметка внутри ячейки SpreadsheetML: выгрузки из интранета кладут в ячейку
#: HTML («Отдел <span>(ЧЛБ)</span>», ссылки, переводы строк).
#: Тег — только если за «<» идёт буква: «<2 кат.>» в тексте ячейки тегом не считается.
_TAG_RE = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>")
_ROW_RE = re.compile(r"<(?:\w+:)?Row\b[^>]*>(.*?)</(?:\w+:)?Row>", re.S | re.I)
_EMPTY_ROW_RE = re.compile(r"<(?:\w+:)?Row\b[^>]*/>", re.I)
#: Ячейки режутся по открывающему тегу (линейно). Прежний поиск «<Cell…>(.*?)</Cell>»
#: на строке с незакрытой ячейкой работал квадратично: минута на строку в 600 КБ.
_CELL_SPLIT_RE = re.compile(r"<(?:\w+:)?Cell\b", re.I)
_CELL_END_RE = re.compile(r"</(?:\w+:)?Cell\s*>", re.I)
#: Префикс «ss:» у Data бывает у ячеек с форматированием Excel
#: (<ss:Data … xmlns="http://www.w3.org/TR/REC-html40">) — раньше они читались пустыми.
_DATA_RE = re.compile(r"<(?:\w+:)?Data\b[^>]*(?:/>|>(.*?)</(?:\w+:)?Data\s*>)", re.S | re.I)
_INDEX_RE = re.compile(r'(?:ss:)?Index\s*=\s*"(\d+)"', re.I)
_MERGE_RE = re.compile(r'(?:ss:)?MergeAcross\s*=\s*"(\d+)"', re.I)


#: Имена query-параметров, значение которых нельзя показывать: выгрузку часто
#: отдают по ссылке с токеном, и такой адрес ходит по логам и аудиту.
_SECRET_QUERY_KEYS = ("token", "key", "secret", "password", "pass", "pwd", "auth", "sig", "access",
                      "ticket", "code", "sid", "session", "jwt", "credential", "hash")
#: Сегмент пути, похожий на токен (длинная смесь букв и цифр): /export/3f9c…e1/emp.csv
_PATH_TOKEN_RE = re.compile(r"^(?=[A-Za-z0-9_\-.~%]*\d)(?=[A-Za-z0-9_\-.~%]*[A-Za-z])[A-Za-z0-9_\-.~%]{24,}$")


def redact_url(url: str) -> str:
    """Убрать из адреса всё, что является секретом: логин с паролем и токены.

    Пароль источника хранится зашифрованным и наружу не отдаётся, но тот же
    секрет часто сидит прямо в ссылке («?token=…»). Без этой чистки он попадал
    бы в журнал, в аудит и в текст ошибки — то есть оседал надолго.
    """
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return text
    netloc = parts.netloc
    if "@" in netloc:                      # https://user:pass@host → https://***@host
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        query.append((key, "***" if any(marker in low for marker in _SECRET_QUERY_KEYS) else value))
    path = "/".join("***" if _PATH_TOKEN_RE.match(seg) else seg for seg in parts.path.split("/"))
    return urllib.parse.urlunsplit((parts.scheme, netloc, path,
                                    urllib.parse.urlencode(query, safe="*"), ""))


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Редирект без переноса заголовка Authorization на чужой хост.

    ``urllib`` по умолчанию тащит все заголовки в новый запрос. Если
    сервер-источник (или тот, кто получил над ним контроль) ответит редиректом
    на посторонний адрес, туда уехал бы логин и пароль от выгрузки.
    """

    #: Переадресаций не больше этого (у urllib по умолчанию 10).
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in ("http", "https"):
            # urllib сам пускает и на ftp:// — выгрузка сотрудников туда не ходит
            raise urllib.error.HTTPError(newurl, code, "переадресация на недопустимый адрес", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_parts, new_parts = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old_parts.scheme, old_parts.netloc) != (new_parts.scheme, new_parts.netloc):
            for name in ("Authorization", "Proxy-authorization"):
                new.headers.pop(name, None)
                new.headers.pop(name.capitalize(), None)
            log.warning("Переадресация выгрузки на другой сервер (%s → %s): логин и пароль "
                        "источника туда не передаём.",
                        redact_url(req.full_url), redact_url(newurl))
        return new


def looks_like_spreadsheetml(data: bytes) -> bool:
    head = (data or b"")[:4096].lstrip()
    return any(marker in head for marker in SPREADSHEETML_MARKERS)


def _spreadsheetml_text(raw: str, collapse: bool = True) -> str:
    """Текст ячейки: снять разметку, раскрыть сущности, схлопнуть пробелы.

    HTML в ячейке бывает и настоящими тегами, и ЭКРАНИРОВАННЫМ текстом
    (&lt;span&gt;…) — после раскрытия сущностей теги снимаются ещё раз.
    ``collapse=False`` — не трогать пробелы внутри (для паролей).
    """
    text = raw or ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    if "<" in text:
        text = html.unescape(_TAG_RE.sub("", re.sub(r"<br\s*/?>", " ", text, flags=re.I)))
    if not collapse:
        return text
    return re.sub(r"\s+", " ", text).strip()


_XML_ENCODING_RE = re.compile(rb"""encoding\s*=\s*["']([\w.-]+)["']""", re.I)


def _decode_xml(data: bytes) -> str:
    """Расшифровать XML-таблицу: сначала по объявленной кодировке, потом перебором.

    Выгрузки 1С и интранета часто объявляют windows-1251. Если читать такой
    файл как UTF-8, все кириллические заголовки превращаются в мусор, колонка
    «ФИО» не опознаётся, и сотрудники заводятся с адресом вместо имени.
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    declared = _XML_ENCODING_RE.search(data[:400])
    encodings = []
    if declared:
        name = declared.group(1).decode("ascii", "ignore")
        # «utf-16» в объявлении при байтах в utf-8 (без нулей и без BOM) —
        # частая ошибка выгрузок: декодер utf-16 «прочитал» бы такой файл в мусор
        if not (name.lower().startswith("utf-16") and b"\x00" not in data[:400]):
            encodings.append(name)
    encodings.extend(CSV_ENCODINGS)
    for enc in encodings:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    log.warning("Не удалось определить кодировку XML-таблицы, читаем с заменой символов.")
    return data.decode("utf-8", errors="replace")


def _read_spreadsheetml_table(data: bytes, collapse: bool = True) -> List[List[str]]:
    """Прочитать таблицу из SpreadsheetML 2003 (Excel XML).

    Разбираем регулярными выражениями, а не XML-парсером, намеренно: реальные
    выгрузки такого вида сплошь и рядом невалидны как XML — префикс ``ss:``
    используется без объявления пространства имён, внутри ячеек встречаются
    незакрытые ``<br>``, а перед объявлением бывает лишний отступ. Строгий
    парсер на таком файле падает, хотя данные в нём читаются однозначно.

    Учитывается ``ss:Index`` — им сервер «перепрыгивает» пустые ячейки, и без
    его обработки колонки поехали бы.
    """
    text = _decode_xml(data) if isinstance(data, (bytes, bytearray)) else str(data)
    if "\ufeff" in text[:4]:
        text = text.lstrip("\ufeff")
    # Берём первую таблицу: лист с данными в таких выгрузках всегда один.
    table_match = re.search(r"<(?:\w+:)?Table\b[^>]*>(.*?)</(?:\w+:)?Table\s*>", text, re.S | re.I)
    if table_match is None and re.search(r"<(?:\w+:)?Table\b", text, re.I):
        # Начало таблицы есть, конца нет — выгрузка оборвалась на полпути.
        # Принимать её нельзя: последняя строка обрезана (адрес «…@vodokomfort.r»
        # превратился бы в новый ящик), а часть сотрудников просто пропала бы.
        raise ValidationError("XML-таблица обрезана: нет закрывающего тега </Table>.",
                              hint="Выгрузка получена не целиком — повторите загрузку позже.")
    body = table_match.group(1) if table_match else text

    rows: List[List[str]] = []
    for chunk in re.split(r"(?i)</(?:\w+:)?Row\s*>", body):
        if not re.search(r"(?i)<(?:\w+:)?Row\b", chunk):
            continue
        if _EMPTY_ROW_RE.search(chunk) and not _CELL_SPLIT_RE.search(chunk):
            rows.append([])
            continue
        row_start = re.search(r"(?i)<(?:\w+:)?Row\b[^>]*>", chunk)
        row_body = chunk[row_start.end():] if row_start else ""
        cells: List[str] = []
        for piece in _CELL_SPLIT_RE.split(row_body)[1:]:
            gt = piece.find(">")
            attrs = piece[:gt] if gt >= 0 else piece
            if gt < 0 or attrs.rstrip().endswith("/"):
                inner = ""
            else:
                inner = piece[gt + 1:]
                end = _CELL_END_RE.search(inner)
                if end:
                    inner = inner[:end.start()]
            index = _INDEX_RE.search(attrs or "")
            if index:
                # ss:Index — номер колонки (с единицы): дополняем пропуск.
                target = max(0, int(index.group(1)) - 1)
                while len(cells) < target:
                    cells.append("")
            data_match = _DATA_RE.search(inner or "")
            cells.append(_spreadsheetml_text(data_match.group(1) if data_match and data_match.group(1) else "",
                                             collapse=collapse))
            merge = _MERGE_RE.search(attrs or "")
            if merge:
                # объединённая ячейка занимает ещё N колонок — иначе всё правее съезжало бы
                cells.extend([""] * min(int(merge.group(1)), 1000))
        rows.append(cells)
    if not rows:
        raise ValidationError(
            "В XML-таблице не найдено ни одной строки.",
            hint="Проверьте, что по ссылке отдаётся выгрузка сотрудников, а не пустой шаблон.")
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def _read_excel_table(data: bytes, raw: bool = False) -> List[List[Any]]:
    """Первый лист книги Excel. ``raw=True`` — значения как есть (числа, даты,
    логические): нужно файлу паролей, где «TRUE» и дата — это ошибка, а не текст."""
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
        if raw:
            return [list(row) for row in ws.iter_rows(values_only=True)]
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
    elif looks_like_spreadsheetml(data) and not name.lower().endswith(".xml"):
        # XML-таблица Excel: пусть в отчёте будет видно, что пришла именно она,
        # а не «какой-то csv».
        name = "employees.xml"
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
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port                      # «[::1» или порт «abc» — ValueError
    except ValueError as exc:
        raise ValidationError(f"Некорректный адрес: «{redact_url(url)}» ({exc}).",
                              hint="Пример правильного адреса: https://hr.example.ru/export/employees.csv") from exc
    if any(ch.isspace() for ch in url):
        raise ValidationError("В адресе выгрузки есть пробелы.",
                              hint="Уберите пробелы (внутри адреса их заменяют на %20).")
    if parts.scheme not in ("http", "https"):
        raise ValidationError(f"Неподдерживаемый адрес: «{url}».",
                              hint="Адрес должен начинаться с http:// или https://. "
                                   "Для файла на диске выберите источник «Файл на сервере».")
    if not parts.netloc:
        raise ValidationError(f"В адресе «{url}» не указан сервер.",
                              hint="Пример правильного адреса: https://hr.example.ru/export/employees.csv")

    timeout = max(5, int(timeout_s or DEFAULT_URL_TIMEOUT_S))
    limit = max(1, int(max_bytes or MAX_DOWNLOAD_BYTES))

    try:
        request = urllib.request.Request(url, method="GET")
    except ValueError as exc:
        raise ValidationError(f"Некорректный адрес: «{redact_url(url)}».",
                              hint="Проверьте адрес выгрузки в настройках раздела «Сотрудники».") from exc
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

    safe_url = redact_url(url)
    log.info("Загрузка списка сотрудников по адресу %s", safe_url)
    opener = urllib.request.build_opener(
        _NoAuthRedirectHandler(),
        urllib.request.HTTPSHandler(context=context) if context is not None else urllib.request.HTTPSHandler(),
    )
    deadline = time.monotonic() + timeout
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise ValidationError(
                    f"Файл по адресу слишком большой: {human_size(int(declared))} "
                    f"при допустимых {human_size(limit)}.",
                    hint="Убедитесь, что ссылка ведёт на выгрузку сотрудников, а не на архив или дамп базы.")
            # Читаем порциями с общим сроком на ВСЮ загрузку: таймаут сокета
            # действует на каждое чтение, и сервер, отдающий по байту, держал
            # задание сколько угодно дольше заданного таймаута.
            chunks: List[bytes] = []
            got = 0
            while got <= limit:
                if time.monotonic() > deadline:
                    raise socket.timeout("download deadline")
                block = response.read(min(64 * 1024, limit + 1 - got))
                if not block:
                    break
                chunks.append(block)
                got += len(block)
            data = b"".join(chunks)
            if (declared and declared.isdigit() and got <= limit and got != int(declared)
                    and not response.headers.get("Content-Encoding")):
                # Обрыв на полпути: принять такое нельзя — последняя строка
                # обрезана («…@vodokomfort.r»), и из неё завелся бы чужой ящик.
                raise ValidationError(
                    f"Выгрузка получена не целиком: {human_size(got)} из {human_size(int(declared))}.",
                    hint="Соединение оборвалось — повторите загрузку позже.")
            disposition = response.headers.get("Content-Disposition", "")
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl() or url
    except http.client.IncompleteRead as exc:
        raise ValidationError("Выгрузка получена не целиком: соединение оборвалось.",
                              hint="Повторите загрузку позже.") from exc
    except http.client.HTTPException as exc:
        raise ValidationError(f"Сервер {safe_url} ответил не по протоколу HTTP: {type(exc).__name__}.",
                              hint="Проверьте адрес и порт источника.") from exc
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
        raise ValidationError(f"Не удалось подключиться к {safe_url}: {text}", hint=hint) from exc
    except socket.timeout as exc:
        raise ValidationError(f"Выгрузка не получена за {timeout} с.",
                              hint="Увеличьте «Таймаут загрузки» в настройках либо проверьте, "
                                   "не формируется ли выгрузка слишком долго.") from exc
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Не удалось загрузить {safe_url}: {exc}",
                              hint="Проверьте адрес и доступность сервера-источника.") from exc

    if len(data) > limit:
        raise ValidationError(
            f"Файл по адресу больше допустимых {human_size(limit)}.",
            hint="Убедитесь, что ссылка ведёт на выгрузку сотрудников, а не на архив или дамп базы.")
    if not data:
        raise ValidationError(f"По адресу {safe_url} вернулся пустой ответ.",
                              hint="Проверьте, формируется ли выгрузка на стороне кадровой системы.")
    _reject_html_page(data, safe_url)

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
        return rows, problems, f"URL {redact_url(url)}"

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
    # Формат определяем по СОДЕРЖИМОМУ, а не по имени: по ссылке выгрузка
    # приходит то как .xls, то как .xml, то вовсе без расширения, и имя файла
    # сплошь и рядом врёт.
    if looks_like_spreadsheetml(data):
        table = _read_spreadsheetml_table(data)
    elif data[:4] == b"PK\x03\x04" or name.endswith(EXCEL_EXTENSIONS):
        table = _read_excel_table(data)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".xls"):
        raise ValidationError("Старый двоичный формат Excel (.xls) не поддерживается.",
                              hint="Откройте файл и сохраните как .xlsx, CSV или «Таблица XML 2003».")
    else:
        table = _read_csv_table(data)
    return _rows_from_table(table)


#: Сколько первых непустых строк просматривать в поисках строки заголовков:
#: над шапкой в отчётах бывает название («Список сотрудников на 01.09»).
HEADER_SEARCH_ROWS = 10
#: Доля адресов почты среди значений, при которой колонка без узнаваемого
#: заголовка считается колонкой E-mail.
EMAIL_COLUMN_SHARE = 0.6

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%Y.%m.%d")


def _parse_date(value: str):
    """Дата из ячейки (01.09.2026, 2026-09-01, 2026-09-01 00:00:00…) или None."""
    from datetime import datetime
    text = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clean_text(value: str) -> str:
    """Текстовое поле карточки: без переводов строк, NUL и повторных пробелов."""
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", value or "").strip()


def _column_values(table: Sequence[Sequence[str]], header_idx: int, col: int) -> List[str]:
    """Непустые значения колонки ПО ВСЕЙ таблице (в нижнем регистре).

    Раньше смотрели только первые 60 строк: если среди них не было ни одного
    «нет», колонка «да/нет» без заголовка не распознавалась, и уволенные из
    конца списка импортировались вместе со всеми.
    """
    values = []
    for idx in range(header_idx + 1, len(table)):
        row = table[idx]
        if col < len(row):
            value = _cell(row[col]).strip()
            if value:
                values.append(value.lower())
    return values


def _map_header(header: Sequence[str], table: Sequence[Sequence[str]],
                header_idx: int) -> Dict[int, str]:
    """Сопоставить колонки файла полям карточки.

    Кроме прямых совпадений с псевдонимами здесь разбирается то, что
    встречается в реальных выгрузках из интранета:

    * колонка «имя» рядом с колонкой «фамилия» — это ИМЯ, а не ФИО целиком
      (само по себе «имя» чаще всего значит «ФИО», поэтому решаем по соседям);
    * заголовок почты в свободной форме («Адрес эл. почты сотрудника») — по
      подстрокам «mail»/«почт», а колонка без узнаваемого заголовка (или с
      заголовком «Адрес») — по содержимому: в ней в основном адреса почты;
    * колонки БЕЗ заголовка: пустой столбец сразу за «именем» — это отчество,
      а столбец, в котором стоят только «да»/«нет», — признак «работает».
    """
    mapping: Dict[int, str] = {}
    aliases: Dict[int, str] = {}
    for col, title in enumerate(header):
        norm = _norm_header(title)
        field = COLUMN_ALIASES.get(norm)
        if field and field not in mapping.values():
            mapping[col] = field
            aliases[col] = norm

    # «Фамилия» + «имя» → это раздельные части ФИО.
    if "last_name" in mapping.values():
        for col, field in list(mapping.items()):
            if field == "full_name" and aliases.get(col) in ("имя", "name"):
                mapping[col] = "first_name"

    cache: Dict[int, List[str]] = {}

    def values_of(col: int) -> List[str]:
        if col not in cache:
            cache[col] = _column_values(table, header_idx, col)
        return cache[col]

    def mostly_emails(col: int) -> bool:
        values = values_of(col)
        if not values:
            return False
        hits = sum(1 for v in values if looks_like_email(normalize_email(v)))
        return hits >= max(1, EMAIL_COLUMN_SHARE * len(values))

    if "email" not in mapping.values():
        # 1) заголовок в свободной форме: «E-mail сотрудника», «Почта (раб.)»
        for col, title in enumerate(header):
            norm = _norm_header(title)
            if col not in mapping and norm and ("mail" in norm or "почт" in norm) and mostly_emails(col):
                mapping[col] = "email"
                break
    if "email" not in mapping.values():
        # 2) по содержимому: в колонке в основном адреса почты
        for col in range(len(header)):
            if col not in mapping and mostly_emails(col):
                mapping[col] = "email"
                break

    # «Уволен» с датами — это дата увольнения, а не признак да/нет.
    for col, field in list(mapping.items()):
        if field == "dismissed":
            values = values_of(col)
            if values and not set(values) <= (EMPLOYED_VALUES | NOT_EMPLOYED_VALUES):
                mapping[col] = "employed_until"

    first_name_col = next((c for c, f in mapping.items() if f == "first_name"), None)
    for col, title in enumerate(header):
        if col in mapping or _norm_header(title):
            continue                      # заголовок есть — гадать не нужно
        values = values_of(col)
        if not values:
            continue
        unique = set(values)
        # Колонка-заглушка из прочерков или нулей — НЕ признак увольнения.
        # Раньше такой столбец (его создаёт, например, хвостовая «;» в CSV)
        # объявлял уволенными сразу всех, и импорт молча не делал ничего.
        # Колонка из одних «да» — тоже признак (все работают): иначе при явной
        # колонке «работает» в дело шла бы дата «работает до», которую в
        # интранете часто не обновляют.
        looks_employed = (
            "employed" not in mapping.values()
            and unique <= (EMPLOYED_VALUES | NOT_EMPLOYED_VALUES)
            and bool(unique & (EMPLOYED_VALUES - {"1", "+"}))     # есть явное «да» / «yes» / «работает»
        )
        if looks_employed:
            mapping[col] = "employed"
        elif (first_name_col is not None and col == first_name_col + 1
              and "middle_name" not in mapping.values()):
            mapping[col] = "middle_name"
    return mapping


def _find_header(table: Sequence[Sequence[str]]) -> Tuple[int, Dict[int, str]]:
    """Строка заголовков и сопоставление колонок.

    Заголовок — первая из нескольких первых непустых строк, в которой нашлись
    колонки ФИО или почты: над шапкой в отчётах бывает строка-название.
    """
    first = -1
    checked = 0
    for idx, raw in enumerate(table):
        if not any(_cell(c) for c in raw):
            continue
        if first < 0:
            first = idx
        mapping = _map_header(table[idx], table, idx)
        if set(mapping.values()) & {"full_name", "email", "last_name"} and \
                any(_norm_header(c) for c in table[idx]):
            return idx, mapping
        checked += 1
        if checked >= HEADER_SEARCH_ROWS:
            break
    if first < 0:
        raise ValidationError("Файл пуст — в нём нет ни одной строки с данными.",
                              hint="Выгрузите список сотрудников заново или скачайте файл-образец.")
    return first, _map_header(table[first], table, first)


def _rows_from_table(table: Sequence[Sequence[str]]) -> Tuple[List[dict], List[dict]]:
    """Превратить таблицу (строка заголовков, затем данные) в строки карточек."""
    from datetime import date
    rows: List[dict] = []
    problems: List[dict] = []

    header_idx, mapping = _find_header(table)
    known = set(mapping.values())
    if not known & {"full_name", "email", "last_name"}:
        raise ValidationError(
            "В файле не найдены колонки «ФИО» и «E-mail».",
            hint="Первая строка должна содержать заголовки. Подойдут названия: "
                 "ФИО / Ф.И.О. / Имя / Сотрудник / Name (или «Фамилия» + «Имя» + «Отчество» "
                 "отдельными колонками), E-mail / Почта / Электронная почта / Mail. "
                 "Скачайте файл-образец, если не уверены в формате.",
        )
    if "email" not in known:
        # Без почты ящики не заведутся — это стоит сказать сразу, а не
        # показывать «создано 580 карточек, ящиков 0, проблем нет».
        problems.append({"row": header_idx + 1,
                         "reason": "в файле не найдена колонка с адресами почты — карточки будут без "
                                   "e-mail, ящики не заведутся (переименуйте колонку в «E-mail»)"})

    data_rows = [idx for idx in range(header_idx + 1, len(table)) if any(_cell(c) for c in table[idx])]
    today = date.today()

    for idx in data_rows:
        raw = table[idx]
        line_no = idx + 1          # нумерация как в Excel: заголовок — строка 1
        item = {"row": line_no}
        for field in FILE_FIELDS + EXTRA_FILE_FIELDS:
            item[field] = ""
        for col, field in mapping.items():
            if col < len(raw):
                item[field] = _clean_text(_cell(raw[col]))

        # ФИО из отдельных колонок — только если целого ФИО в файле нет.
        if not item["full_name"]:
            item["full_name"] = " ".join(
                part for part in (item["last_name"], item["first_name"], item["middle_name"]) if part
            ).strip()
        # Телефон: мобильный полезнее рабочего, внутренний — на крайний случай.
        if not item["phone"]:
            item["phone"] = item["phone_mobile"] or item["phone_work"] or item["phone_ext"]

        email = normalize_email(item["email"])
        if email and not looks_like_email(email):
            problems.append({"row": line_no, "reason": f"некорректный e-mail: «{item['email'][:100]}»"})
            email = ""
        item["email"] = email

        # Сотрудник, помеченный в файле как не работающий, в справочник не
        # добавляется и ящик ему не заводится. Уже заведённые карточки при этом
        # НЕ трогаются — увольнение остаётся решением человека.
        inactive = item["employed"].strip().lower() in NOT_EMPLOYED_VALUES
        dismissed = item["dismissed"].strip().lower()
        if dismissed and dismissed in EMPLOYED_VALUES:          # «Уволен: да»
            inactive = True
        until = _parse_date(item["employed_until"])
        if until is not None and until < today and "employed" not in known:
            # дата увольнения прошла; явная колонка «работает да/нет», если она
            # есть, важнее даты (дату в интранете часто забывают обновить)
            inactive = True
        item["inactive"] = inactive

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
        # Вход через администратора почты — только если он включён и настроен на ТОТ ЖЕ сервер.
        "use_master": bool(svc.rt("employees", "account_use_master")) and bool(svc.rt("mailadmin", "enabled"))
        and _text("account_host").lower() == str(svc.rt("mailadmin", "host") or "").strip().lower() != "",
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
        "use_master": tpl["use_master"],
    }


def _row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def ensure_account_for_employee(svc, employee_id: int, full_name: str, email: str,
                                row: Optional[dict] = None,
                                tpl: Optional[Dict[str, Any]] = None,
                                create: bool = True) -> Tuple[Optional[int], str]:
    """Завести (или найти) почтовый ящик для сотрудника и привязать его.

    Ящик создаётся по ШАБЛОНУ из настроек раздела «Сотрудники»: сервер, порт,
    шифрование, название и логин по шаблонам, фильтры папок, срок хранения и —
    при желании — расписание копирования. Пароль в шаблон не входит и никогда
    не подставляется: его знает только администратор.

    :param row: строка выгрузки (должность, отдел, табельный номер) — нужна
        подстановкам в шаблонах названия и заметки;
    :param create: заводить ли новый ящик, если подходящего нет (False — только
        привязать существующий);
    :returns: ``(account_id, action)``, где action — ``"created"``,
        ``"linked"``, ``"no_host"`` (ящик нужен, но заводить нельзя) или ``""``.
    """
    if not email:
        return None, ""
    # Шаблон читается из настроек (это ~13 обращений к БД). На синхронизации в
    # 600 строк его пересчёт для каждой строки — тысячи лишних запросов, поэтому
    # вызывающий код передаёт готовый шаблон.
    tpl = account_template(svc) if tpl is None else tpl
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
    if not create:
        return None, "no_host"

    acc = Account(
        name=render_account_field(tpl["name_template"], values) or full_name or email,
        host=tpl["host"], port=tpl["port"], username=username, password="",
        auth_type=AuthType.MASTER if tpl.get("use_master") else AuthType.PASSWORD, security=tpl["security"],
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
            # Перезагрузку планировщика делает вызывающий код ОДИН раз в конце:
            # reload() перечитывает все расписания целиком, и вызов на каждый
            # созданный ящик давал квадратичный рост (600 сотрудников — минута
            # ожидания и сотни тысяч запросов к БД).
        except Exception as exc:  # noqa: BLE001
            # Ящик уже создан — падать из-за расписания нельзя, иначе
            # синхронизация оборвётся на середине файла.
            log.warning("Не удалось создать расписание для ящика %s: %s", username, exc)

    return account_id, "created"


#: Синхронизации сотрудников выполняются строго по одной: проверка «такой
#: карточки ещё нет» и её создание не атомарны, и два одновременных прогона
#: (ночной по расписанию и загрузка файла из интерфейса) плодили дубли карточек
#: и ящиков с одним логином.
_SYNC_LOCK = threading.Lock()


def sync_employees(svc, rows: Sequence[dict], *, create_accounts: bool,
                   progress_cb: Optional[Callable[..., None]] = None,
                   wait: bool = True) -> Dict[str, Any]:
    """Применить разобранные строки файла к справочнику сотрудников.

    Сопоставление с уже заведёнными: сначала по табельному номеру
    (``external_id``), затем по почте (без учёта регистра), затем — по ФИО среди
    карточек без почты и номера (их почту и номер дозаполняем, а не заводим
    вторую карточку). Уволенные, то есть исчезнувшие из файла, НЕ трогаются —
    см. заголовок модуля.

    ``wait=False`` — если идёт другая синхронизация, сразу отказать (для
    загрузки файла из интерфейса), а не ждать её окончания.
    """
    if not _SYNC_LOCK.acquire(blocking=wait):
        raise ValidationError("Сейчас уже идёт синхронизация сотрудников.",
                              hint="Дождитесь её окончания (раздел «Очередь и задания») и повторите.")
    try:
        return _sync_employees_locked(svc, rows, create_accounts=create_accounts, progress_cb=progress_cb)
    finally:
        _SYNC_LOCK.release()


def _sync_employees_locked(svc, rows: Sequence[dict], *, create_accounts: bool,
                           progress_cb: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    db = svc.db
    result: Dict[str, Any] = {
        "created": 0, "updated": 0, "accounts_created": 0, "accounts_linked": 0,
        "skipped_inactive": 0, "total_rows": len(rows), "problems": [],
        "duplicate_emails": [], "duplicate_rows": 0, "warnings": [],
    }
    seen_emails: Dict[str, int] = {}
    tpl = account_template(svc) if create_accounts else None
    # По умолчанию «заводить ящики» включено, а сервер не задан: раньше первая
    # же загрузка файла создавала сотни ящиков без сервера. Теперь без сервера
    # новые ящики не заводятся (уже существующие по-прежнему привязываются).
    can_create = bool(tpl is not None and str(tpl.get("host") or "").strip())
    no_host_skipped = 0
    schedules_created = False
    matched_by_name: set = set()
    # Карточки без почты и табельного номера — по ФИО, одним запросом: поиск
    # по ФИО на каждую строку был бы полным просмотром таблицы (O(N²) на
    # больших выгрузках).
    unidentified: Dict[str, List[int]] = {}
    for rec in db.query("SELECT id, full_name FROM employees "
                        "WHERE COALESCE(external_id,'')='' AND COALESCE(email,'')='' ORDER BY id"):
        unidentified.setdefault((rec["full_name"] or "").strip().lower(), []).append(int(rec["id"]))
    # Увольнения по выгрузке: применяются ПОСЛЕ разбора всего файла и с защитой
    # от испорченной выгрузки (см. DISMISS_GUARD_SHARE).
    dismiss_from_file = bool(svc.rt("employees", "dismiss_from_file"))
    to_dismiss: List[Tuple[int, str]] = []
    to_rehire: List[int] = []
    result["dismissed"] = 0
    result["rehired"] = 0
    total = len(rows)
    for num, row in enumerate(rows, 1):
        line_no = int(row.get("row") or num)
        if row.get("inactive"):
            # В файле сотрудник помечен как не работающий: новую карточку не
            # заводим и ящик не создаём. Уже заведённую карточку отмечаем
            # уволенной (если так настроено) — архив её ящика удерживается.
            result["skipped_inactive"] += 1
            if dismiss_from_file:
                ext = (row.get("external_id") or "").strip()
                mail = normalize_email(row.get("email"))
                known = db.get_employee_by_external_id(ext) if ext else None
                if known is None and mail:
                    known = db.get_employee_by_email(mail)
                if known is not None and known["status"] == "active":
                    to_dismiss.append((int(known["id"]), "уволен по данным выгрузки"))
            if progress_cb is not None:
                progress_cb(num, total, f"Обработано {num}/{total}")
            continue
        full_name = (row.get("full_name") or "").strip()
        email = normalize_email(row.get("email"))
        external_id = (row.get("external_id") or "").strip()
        if not full_name and not email:
            result["problems"].append({"row": line_no, "reason": "нет ФИО и e-mail"})
            continue

        if email:
            # Один адрес у нескольких строк — это общий ящик (склад, сервис) или
            # задвоенная строка. Карточка на адрес всё равно одна, но молчать об
            # этом нельзя: иначе «обновлено N» выглядит как обычное обновление.
            if email in seen_emails:
                result["duplicate_rows"] += 1
                if email not in result["duplicate_emails"]:
                    result["duplicate_emails"].append(email)
            else:
                seen_emails[email] = line_no

        existing = db.get_employee_by_external_id(external_id) if external_id else None
        if existing is None and email:
            existing = db.get_employee_by_email(email)
        if existing is None and full_name:
            # Опознаём по ФИО среди карточек БЕЗ номера и почты: так строка без
            # признаков не добавляется заново при каждой синхронизации, а
            # карточка, заведённая, когда колонку почты ещё не распознавали,
            # получает адрес вместо того, чтобы рядом появилась вторая.
            # Однофамильцы в одном файле: уже опознанную карточку не берём.
            for cand_id in unidentified.get(full_name.lower(), []):
                if cand_id not in matched_by_name:
                    existing = db.get_employee(cand_id)
                    matched_by_name.add(cand_id)
                    break

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
            had_account = False
            result["created"] += 1
            if not email and not external_id:
                unidentified.setdefault(values["full_name"].lower(), []).append(employee_id)
                matched_by_name.add(employee_id)
        else:
            employee_id = int(existing["id"])
            account_id = existing["account_id"]
            had_account = bool(_row_value(existing, "had_account", 0))
            # Пустые поля файла не затирают заполненные в карточке.
            changes = {k: v for k, v in values.items() if v and v != (existing[k] or "")}
            changes["last_seen_at"] = utcnow_iso()
            db.update_employee(employee_id, **changes)
            if dismiss_from_file and existing["status"] == "archived" and _row_value(existing, "dismissed_at"):
                # Уволенный снова числится работающим (повторный приём, ошибка кадров).
                to_rehire.append(employee_id)
            # «Обновлён» = встретился в файле (created + updated = разобранные строки).
            result["updated"] += 1

        # Ящик заводится, только если его у сотрудника не было никогда: ящик,
        # удалённый (или отвязанный) администратором, ночная синхронизация
        # больше не пересоздаёт.
        if create_accounts and email and account_id is None and not had_account:
            _, action = ensure_account_for_employee(svc, employee_id, values["full_name"], email,
                                                     row=values, tpl=tpl, create=can_create)
            if action == "no_host":
                no_host_skipped += 1
            elif action == "created":
                result["accounts_created"] += 1
                schedules_created = schedules_created or bool(tpl and tpl["schedule_enabled"])
            elif action == "linked":
                result["accounts_linked"] += 1

        if progress_cb is not None:
            progress_cb(num, total, f"Обработано {num}/{total}")

    _apply_dismissals(svc, result, to_dismiss, to_rehire, rows_total=total)

    if no_host_skipped:
        result["warnings"].append(
            f"Ящики не заведены ({no_host_skipped}): в шаблоне ящиков не указан IMAP-сервер "
            f"(Настройки → Сотрудники или кнопка «Шаблон ящиков»). Карточки сотрудников при этом "
            f"обновлены; после указания сервера ящики заведутся при следующей синхронизации.")
    if schedules_created and getattr(svc, "scheduler", None) is not None:
        # Один reload на всю синхронизацию — новые расписания подхватятся сразу.
        try:
            svc.scheduler.reload()
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось перечитать расписания после синхронизации: %s", exc)
    return result


# ---------------------------------------------------------------------------
#  Увольнение и повторный приём
# ---------------------------------------------------------------------------
def _apply_dismissals(svc, result: Dict[str, Any], to_dismiss: List[Tuple[int, str]], to_rehire: List[int],
                      *, rows_total: int) -> None:
    """Применить увольнения и возвраты, найденные синхронизацией."""
    db = svc.db
    try:
        missing_days = int(svc.rt("employees", "dismiss_missing_days") or 0)
    except (TypeError, ValueError):
        missing_days = 0
    if missing_days > 0 and rows_total > 0:
        cutoff = (_datetime.now(_timezone.utc) - _timedelta(days=missing_days)).isoformat()
        planned = {emp_id for emp_id, _ in to_dismiss}
        for rec in db.query("SELECT id FROM employees WHERE status='active' AND source='file' "
                            "AND last_seen_at IS NOT NULL AND last_seen_at < ?", (cutoff,)):
            if int(rec["id"]) not in planned:
                to_dismiss.append((int(rec["id"]), f"нет в выгрузке больше {missing_days} дн."))
    for emp_id in to_rehire:
        rehire_employee(svc, emp_id)
        result["rehired"] += 1
    if not to_dismiss:
        return
    active = db.employee_counts().get("active", 0)
    limit = max(DISMISS_GUARD_MIN, int(active * DISMISS_GUARD_SHARE))
    if len(to_dismiss) > limit:
        result["dismiss_blocked"] = len(to_dismiss)
        result["warnings"].append(
            f"Выгрузка отмечает уволенными {len(to_dismiss)} сотрудников из {active} — больше допустимого "
            f"({limit}). Так выглядит испорченная выгрузка, поэтому увольнения НЕ применены. Если это "
            f"правда, отметьте сотрудников уволенными вручную или временно выключите «Отмечать уволенных "
            f"по выгрузке» и проверьте файл.")
        return
    for emp_id, reason in to_dismiss:
        dismiss_employee(svc, emp_id, reason=reason)
        result["dismissed"] += 1
#: Удержание «бессрочно».
HOLD_FOREVER = "9999-12-31"
#: Сколько сотрудников одна синхронизация может отметить уволенными без
#: подтверждения: больше — похоже на испорченную выгрузку, а не на увольнения.
DISMISS_GUARD_SHARE = 0.2
DISMISS_GUARD_MIN = 5


def hold_until_for_dismissal(svc, today: Optional[_date] = None) -> str:
    """До какой даты удерживать архив уволенного (employees.dismissed_keep_years)."""
    try:
        years = int(svc.rt("employees", "dismissed_keep_years") or 0)
    except (TypeError, ValueError):
        years = 5
    if years <= 0:
        return HOLD_FOREVER
    day = today or _date.today()
    try:
        return day.replace(year=day.year + years).isoformat()
    except ValueError:                       # 29 февраля
        return day.replace(year=day.year + years, day=28).isoformat()


def dismiss_employee(svc, employee_id: int, *, by: str = "system", reason: str = "") -> Dict[str, Any]:
    """Сотрудник уволен: архив его ящика удерживается, копирование — после
    последней копии выключается (employees.dismissed_action)."""
    emp = svc.db.get_employee(employee_id)
    if emp is None:
        return {}
    now = utcnow_iso()
    svc.db.update_employee(employee_id, status="archived", dismissed_at=now)
    out: Dict[str, Any] = {"employee": emp["full_name"], "account": None, "action": "", "hold_until": ""}
    acc = svc.db.get_account(emp["account_id"]) if emp["account_id"] else None
    if acc is not None:
        until = hold_until_for_dismissal(svc)
        if not (acc.hold_until and acc.hold_until > until):
            # Ручное удержание дольше «увольнительного» не сокращаем.
            svc.db.set_account_hold(acc.id, until, "dismissed")
        out["hold_until"] = max(until, acc.hold_until or "")
        svc.db.mark_account_dismissed(acc.id, now)
        out["account"] = acc.name
        action = str(svc.rt("employees", "dismissed_action") or "final_backup_disable")
        if not acc.enabled:
            out["action"] = "already_disabled"
        elif action == "final_backup_disable":
            if account_has_credentials(acc) and not acc.secret_broken and getattr(svc, "queue", None) is not None:
                try:
                    attempts = int(svc.rt("backup", "retry_attempts") or 1)
                except (TypeError, ValueError):
                    attempts = 1
                out["job_id"] = svc.queue.enqueue(JobType.BACKUP, acc.id, {"final": True}, priority=3,
                                                  max_attempts=attempts, created_by=by)
                out["action"] = "final_backup"
            else:
                svc.db.set_account_auto_disabled(acc.id, True)
                out["action"] = "disabled"
        else:
            out["action"] = "kept"
    detail = emp["full_name"]
    if acc is not None:
        what = {"final_backup": "последняя копия, затем копирование выключится",
                "disabled": "копирование выключено", "kept": "копирование продолжается",
                "already_disabled": "копирование ящика уже было выключено"}.get(out["action"], "")
        until_text = "бессрочно" if out["hold_until"] == HOLD_FOREVER else f"до {out['hold_until']}"
        detail += f": ящик «{acc.name}», архив удерживается {until_text}; {what}"
    if reason:
        detail += f" ({reason})"
    svc.db.add_audit(by, "employee_dismissed", detail[:900])
    return out


def rehire_employee(svc, employee_id: int, *, by: str = "system") -> Dict[str, Any]:
    """Сотрудник снова работает: снять «увольнительное» удержание и вернуть копирование."""
    emp = svc.db.get_employee(employee_id)
    if emp is None:
        return {}
    svc.db.update_employee(employee_id, status="active", dismissed_at=None)
    acc = svc.db.get_account(emp["account_id"]) if emp["account_id"] else None
    out: Dict[str, Any] = {"employee": emp["full_name"], "account": acc.name if acc else None, "enabled": False}
    if acc is not None:
        if acc.hold_reason == "dismissed":
            svc.db.set_account_hold(acc.id, "", "")
        svc.db.mark_account_dismissed(acc.id, None)
        if acc.auto_disabled:
            svc.db.set_account_auto_disabled(acc.id, False)
            out["enabled"] = True
    svc.db.add_audit(by, "employee_rehired", emp["full_name"] + (
        f": ящик «{acc.name}»" + (", копирование снова включено" if out["enabled"] else "") if acc else ""))
    return out


# ---------------------------------------------------------------------------
#  Пароли ящиков из файла
# ---------------------------------------------------------------------------
#: Псевдонимы заголовков в файле паролей.
PASSWORD_ALIASES = {
    "email": ("email", "e-mail", "почта", "электропочта", "адрес", "mail", "логин", "login",
              "username", "ящик", "пользователь"),
    "password": ("пароль", "password", "pass", "pwd", "парольпочты"),
}
PASSWORD_COLUMNS: Dict[str, str] = {}
for _field, _names in PASSWORD_ALIASES.items():
    for _name in _names:
        PASSWORD_COLUMNS[_name.replace(" ", "")] = _field


_LOGIN_RE = re.compile(r"^[^\s@,;]{1,320}$")


def _password_value(value: Any) -> Tuple[str, str]:
    """Пароль из ячейки: ``(пароль, проблема)``.

    Excel сам превращает некоторые пароли в другие типы: «TRUE» — в логическое
    значение, «12.05.2024» — в дату, «1,5» — в число. Прочитанные «как есть»
    такие ячейки давали пароль «да» или «2024-05-12 00:00:00» и молча
    затирали рабочий пароль ящика. Теперь такие строки пропускаются с понятной
    причиной; целые числа (пароль из одних цифр) принимаются.
    """
    import datetime as _dt
    if value is None:
        return "", ""
    if isinstance(value, bool):
        return "", "Excel превратил пароль в логическое значение (TRUE/FALSE)"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time, _dt.timedelta)):
        return "", "Excel превратил пароль в дату или время"
    if isinstance(value, int):
        return str(value), ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)), ""
        return "", "Excel превратил пароль в дробное число"
    return str(value).strip(), ""


def parse_password_file(path_or_bytes: Any, filename: str = "") -> Tuple[List[dict], List[dict]]:
    """Разобрать файл «адрес — пароль» для массовой установки паролей ящиков.

    Понимает те же форматы, что и список сотрудников: XLSX, CSV и XML-таблицу.
    Заголовки необязательны, если колонок ровно две и первая строка уже похожа
    на пару «адрес и пароль» — такие файлы чаще всего готовят вручную. При
    большем числе колонок без заголовков не угадываем: паролем могло стать ФИО.

    :returns: ``(rows, problems)``; ``rows`` — ``[{"row": N, "email": …,
        "password": …}]``. Пароли НИКУДА не логируются и не возвращаются
        наружу дальше вызывающего кода. Ячейки, которые Excel превратил в
        дату, логическое или дробное число, пропускаются с причиной.
    """
    name = (filename or (path_or_bytes if isinstance(path_or_bytes, str) else "")).lower()
    data = _as_bytes(path_or_bytes)
    if looks_like_spreadsheetml(data):
        # пробелы внутри пароля — часть пароля, их не схлопываем
        table: List[List[Any]] = _read_spreadsheetml_table(data, collapse=False)
    elif data[:4] == b"PK\x03\x04" or name.endswith(EXCEL_EXTENSIONS):
        table = _read_excel_table(data, raw=True)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".xls"):
        raise ValidationError("Старый двоичный формат Excel (.xls) не поддерживается.",
                              hint="Сохраните файл как .xlsx или CSV.")
    else:
        table = _read_csv_table(data)

    rows: List[dict] = []
    problems: List[dict] = []
    if not table:
        raise ValidationError("Файл пуст.", hint="Нужны две колонки: адрес ящика и пароль.")

    email_col, password_col, start = 0, 1, 0
    by_header = False
    header = [_norm_header(_cell(c)) for c in table[0]]
    mapped = {PASSWORD_COLUMNS.get(h): i for i, h in enumerate(header) if PASSWORD_COLUMNS.get(h)}
    if "email" in mapped and "password" in mapped:
        email_col, password_col, start = mapped["email"], mapped["password"], 1
        by_header = True
    else:
        # Заголовков нет. Колонок должно быть ровно две: при трёх (адрес, ФИО,
        # пароль) паролем раньше становилось ФИО — и без единой жалобы.
        used = set()
        for raw in table[:50]:
            for idx, value in enumerate(raw):
                if _cell(value):
                    used.add(idx)
        if len(used) > 2:
            raise ValidationError(
                "В файле больше двух колонок, а заголовков нет — не видно, где пароль.",
                hint="Добавьте первую строку с заголовками «email» и «пароль» (остальные колонки "
                     "можно оставить) или оставьте в файле только две колонки: адрес и пароль.")
        # Определяем колонку с адресами по содержимому: пароль похож на что
        # угодно, а адрес — только на адрес.
        first = [_cell(c).strip() for c in table[0]]
        if not any(looks_like_email(normalize_email(c)) for c in first):
            # Первая строка — «шапка», названия в которой мы не опознали. Молча
            # выбрасывать её нельзя: в ней мог быть ящик с логином без домена,
            # и тогда он остался бы без пароля незаметно для администратора.
            start = 1
            problems.append({"row": 1, "reason": "первая строка пропущена как заголовок "
                                                 "(в ней не найдено ни одного адреса)"})
        probe = table[start] if start < len(table) else []
        for idx, value in enumerate(probe):
            if looks_like_email(normalize_email(_cell(value))):
                email_col = idx
                password_col = idx + 1 if idx + 1 < len(probe) else idx - 1
                break
    if password_col == email_col or password_col < 0:
        # Файл из одной колонки. Раньше сюда попадал сам адрес, и каждому ящику
        # ставился пароль, равный его адресу, — прежние пароли затирались
        # безвозвратно, а с включённой галочкой ящики ещё и включались.
        raise ValidationError(
            "В файле только одна колонка — не видно, где пароли.",
            hint="Нужны две колонки: адрес ящика и пароль. Заголовки «email» и «пароль» "
                 "необязательны, но с ними формат определяется точно.")

    seen: Dict[str, int] = {}
    for idx in range(start, len(table)):
        raw = table[idx]
        line_no = idx + 1
        if not any(_cell(c) for c in raw):
            continue
        email = normalize_email(raw[email_col] if email_col < len(raw) else "")
        password, bad = _password_value(raw[password_col] if password_col < len(raw) else None)
        if not email:
            problems.append({"row": line_no, "reason": "не указан адрес ящика"})
            continue
        # С заголовком «логин» в колонке может стоять логин без домена — он
        # ищется среди логинов ящиков. Без заголовка — только адрес.
        if not looks_like_email(email) and not (by_header and _LOGIN_RE.match(email)):
            # Значение НЕ подставляем: при сбитых колонках в «адресе» окажется
            # пароль, и он уехал бы в ответ API и в историю браузера.
            problems.append({"row": line_no,
                             "reason": "значение в колонке адреса не похоже на e-mail"})
            continue
        if bad:
            problems.append({"row": line_no, "reason": f"{bad} для {email} — пропущено; "
                                                      f"отформатируйте колонку паролей как текст"})
            continue
        if password == email:
            # Копия адреса вместо пароля — почти всегда съехавшие колонки.
            problems.append({"row": line_no, "reason": f"пароль совпадает с адресом {email} — "
                                                      f"похоже, колонки перепутаны"})
            continue
        if not password:
            problems.append({"row": line_no, "reason": f"пустой пароль для {email}"})
            continue
        if email in seen:
            # Два пароля на один ящик — почти наверняка ошибка подготовки файла,
            # и молча брать последний нельзя: пароль может оказаться не тем.
            problems.append({"row": line_no,
                             "reason": f"адрес {email} встречается повторно (строка {seen[email]})"})
            continue
        seen[email] = line_no
        rows.append({"row": line_no, "email": email, "password": password})
    return rows, problems


def apply_passwords(svc, rows: Sequence[dict], *, enable: bool = False,
                    progress_cb: Optional[Callable[..., None]] = None) -> Dict[str, Any]:
    """Проставить пароли ящикам по списку «адрес — пароль».

    Ящик ищется по логину, а если такого логина нет — по адресу сотрудника из
    справочника (в интранете логин и адрес иногда различаются). Пароль
    сохраняется в базе зашифрованным; в журнал и в ответ он не попадает.

    :param enable: включить ящик после установки пароля. Ящики, заведённые
        автоматически, создаются выключенными, и без этого их пришлось бы
        включать по одному руками.
    """
    result: Dict[str, Any] = {"updated": 0, "enabled": 0, "not_found": [], "total_rows": len(rows)}
    total = len(rows)
    for num, row in enumerate(rows, 1):
        email = row.get("email") or ""
        password = row.get("password") or ""
        account = svc.db.get_account_by_username(email)
        if account is None:
            employee = svc.db.get_employee_by_email(email)
            if employee is not None and employee["account_id"]:
                account = svc.db.get_account(int(employee["account_id"]))
        if account is None:
            result["not_found"].append(email)
            continue
        account.password = password
        if enable and not account.enabled:
            account.enabled = True
            result["enabled"] += 1
        svc.db.update_account(account, update_password=True,
                              update_oauth_secret=False, update_oauth_token=False)
        result["updated"] += 1
        if progress_cb is not None:
            progress_cb(num, total, f"Обработано {num}/{total}")
    return result
