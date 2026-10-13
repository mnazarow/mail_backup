"""
Разбор ответов CLI Axigen.

Формат ответов описан в документации не полностью, поэтому разбор нарочно
терпим к вариантам:

* итог команды — последняя строка ``+OK…`` или ``-ERR…`` перед приглашением;
* таблица ``LIST`` — строка заголовков, под ней черта из «-» и строки данных
  (колонки выровнены пробелами, в названии колонки могут быть одиночные
  пробелы: «total size»); без черты каждая строка — одно значение;
* ``SHOW`` — строки «имя = значение» (или «имя: значение»), значение может быть
  в кавычках, список — в скобках: ``services = (localDelivery pop3 imap)``.

Что разобрать не удалось, не теряется: исходные строки остаются в ответе, и
интерфейс показывает их как есть («Сервер Axigen → Диагностика»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Tuple

_STATUS = re.compile(r"^\s*([+-])(OK|ERR)\b[:\s-]*(.*)$", re.I)
_SEPARATOR = re.compile(r"^[\s\-=+|_]{3,}$")
_SHOW_LINE = re.compile(r"^\s*([A-Za-z][\w.\-]*(?:\s[\w.\-]+){0,4}?)\s*(?:=|:)\s*(.*?)\s*$")
_TITLE = re.compile(r"^\s*(the\s+)?(list|following|available)\b.*:\s*$", re.I)


@dataclass
class Reply:
    """Ответ CLI на одну команду."""

    command: str                       # команда (пароли замаскированы)
    lines: List[str] = field(default_factory=list)   # строки данных, без итога и приглашения
    ok: Optional[bool] = None          # True: +OK, False: -ERR, None: итога не было
    status: str = ""                   # текст после +OK/-ERR
    prompt: str = ""                   # приглашение после ответа (контекст CLI)
    raw: str = ""                      # ответ целиком (для журнала и диагностики)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def split_reply(text: str) -> Tuple[List[str], Optional[bool], str]:
    """Строки ответа → (строки данных, итог, текст итога).

    Итог — последняя строка ``+OK``/``-ERR``. Обычно она в конце, но если сервер
    пишет её до данных, данные после неё не теряются: убирается только сама
    строка итога.
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    ok: Optional[bool] = None
    status = ""
    status_at = None
    for i in range(len(lines) - 1, -1, -1):
        m = _STATUS.match(lines[i])
        if m:
            ok = m.group(1) == "+"
            status = m.group(3).strip()
            status_at = i
            break
    data = (lines[:status_at] + lines[status_at + 1:]) if status_at is not None else lines
    while data and not data[-1].strip():
        data.pop()
    while data and not data[0].strip():
        data.pop(0)
    return data, ok, status


def _key(name: str) -> str:
    text = re.sub(r"[^0-9a-zа-я]+", "_", (name or "").strip().lower()).strip("_")
    return text or "col"


@dataclass
class Table:
    columns: List[str] = field(default_factory=list)   # ключи колонок (name, total_size…)
    titles: List[str] = field(default_factory=list)    # заголовки как у сервера
    rows: List[Dict[str, str]] = field(default_factory=list)
    raw_rows: List[str] = field(default_factory=list)

    def column(self, *names: str) -> Optional[str]:
        """Ключ первой колонки, название которой содержит одно из ``names``."""
        for want in names:
            want = _key(want)
            for col in self.columns:
                if col == want:
                    return col
        for want in names:
            want = _key(want)
            for col in self.columns:
                if want in col:
                    return col
        return None


def _header_columns(header: str) -> List[Tuple[int, str]]:
    """Начала колонок и их названия по строке заголовков."""
    parts = [(m.start(), m.group(0)) for m in re.finditer(r"\S+(?: \S+)*", header)]
    # Заголовок, где все слова разделены одним пробелом («name size count»), —
    # это тоже отдельные колонки, а не одно название из трёх слов.
    if len(parts) == 1 and " " in parts[0][1]:
        parts = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", header)]
    return parts


def parse_table(lines: Iterable[str]) -> Table:
    """Таблица ответа ``LIST`` (см. описание модуля)."""
    rows = [ln for ln in lines if ln.strip()]
    table = Table()
    sep = next((i for i, ln in enumerate(rows) if _SEPARATOR.match(ln) and ln.count("-") + ln.count("=") >= 3), None)
    if sep is None or sep == 0:
        body = rows[(sep + 1) if sep == 0 else 0:]
        # «The list of accounts for domain x:» — подпись, а не значение
        body = [ln for ln in body if not _TITLE.match(ln)]
        table.columns, table.titles = ["name"], ["name"]
        for ln in body:
            if _SEPARATOR.match(ln):
                continue
            table.raw_rows.append(ln)
            table.rows.append({"name": ln.strip()})
        return table
    header = rows[sep - 1]
    cols = _header_columns(header)
    table.titles = [title for _pos, title in cols]
    keys: List[str] = []
    for _pos, title in cols:
        key = _key(title)
        while key in keys:
            key += "_"
        keys.append(key)
    table.columns = keys
    starts = [pos for pos, _title in cols]
    for ln in rows[sep + 1:]:
        if _SEPARATOR.match(ln):
            continue
        table.raw_rows.append(ln)
        parts = re.split(r"\s{2,}|\t", ln.strip())
        if len(parts) == len(keys):
            values = [p.strip() for p in parts]
        elif len(keys) == 1:
            values = [ln.strip()]
        else:
            values = []
            for i, start in enumerate(starts):
                end = starts[i + 1] if i + 1 < len(starts) else None
                values.append(ln[start:end].strip() if start < len(ln) else "")
            if not values[0] and parts:
                values[0] = parts[0]
        table.rows.append(dict(zip(keys, values)))
    return table


def unquote(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def parse_show(lines: Iterable[str]) -> Dict[str, str]:
    """Пары «имя = значение» ответа ``SHOW`` (имена — как у сервера)."""
    out: Dict[str, str] = {}
    for ln in lines:
        if not ln.strip() or ln.strip() in ("{", "}") or _SEPARATOR.match(ln):
            continue
        m = _SHOW_LINE.match(ln)
        if not m:
            continue
        name, value = m.group(1).strip(), m.group(2)
        if value.endswith("{"):
            continue                      # начало вложенного блока — его поля пойдут отдельно
        out.setdefault(name, unquote(value))
    return out


def get(values: Dict[str, str], *names: str, default: str = "") -> str:
    """Значение по одному из имён без учёта регистра (первое найденное)."""
    low = {k.lower().replace(" ", ""): v for k, v in values.items()}
    for name in names:
        hit = low.get(name.lower().replace(" ", ""))
        if hit is not None:
            return hit
    return default


def find(values: Dict[str, str], *parts: str) -> Tuple[str, str]:
    """(имя, значение) первого поля, в имени которого есть все ``parts`` (без учёта регистра)."""
    for key, value in values.items():
        low = key.lower()
        if all(p.lower() in low for p in parts):
            return key, value
    return "", ""


_QUOTED = r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\''


def parse_list_value(value: str) -> List[str]:
    """``(a b "c d")`` / ``a, b c`` / ``[a,b]`` → список строк.

    Если в списке есть запятая или точка с запятой (вне кавычек), элементы
    разделяют они, а пробел — часть имени: «Sent Items, Trash» — две папки, а
    не три. Иначе элементы разделяет пробел, имя с пробелом — в кавычках.
    """
    text = (value or "").strip()
    if text[:1] in "([{" and text[-1:] in ")]}":
        text = text[1:-1]
    bare = re.sub(_QUOTED, "", text)
    out: List[str] = []
    if "," in bare or ";" in bare:
        for m in re.finditer(_QUOTED + r'|([^,;]+)', text):
            if m.group(1) is not None:
                item = m.group(1).replace('\\"', '"')
            elif m.group(2) is not None:
                item = m.group(2)
            else:
                item = m.group(3).strip()
                if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
                    item = item[1:-1]
            if item:
                out.append(item)
        return out
    for m in re.finditer(_QUOTED + r'|([^\s,;]+)', text):
        item = m.group(1) if m.group(1) is not None else (m.group(2) if m.group(2) is not None else m.group(3))
        item = item.replace('\\"', '"')
        if item:
            out.append(item)
    return out


_UNITS = {"": 1, "b": 1, "kb": 1024, "k": 1024, "kib": 1024, "mb": 1024 ** 2, "m": 1024 ** 2, "mib": 1024 ** 2,
          "gb": 1024 ** 3, "g": 1024 ** 3, "gib": 1024 ** 3, "tb": 1024 ** 4, "t": 1024 ** 4, "tib": 1024 ** 4,
          "кб": 1024, "мб": 1024 ** 2, "гб": 1024 ** 3, "тб": 1024 ** 4, "байт": 1}


def parse_size(value: str, default_unit: str = "") -> Optional[int]:
    """«12 MB» / «1.5GB» / «2048» → байты (None — не число)."""
    m = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)\s*([A-Za-zА-Яа-я]*)\s*$", str(value or ""))
    if not m:
        return None
    unit = (m.group(2) or default_unit).lower()
    if unit not in _UNITS:
        return None
    try:
        return int(float(m.group(1).replace(",", ".")) * _UNITS[unit])
    except ValueError:
        return None


def parse_int(value: str) -> Optional[int]:
    m = re.match(r"^\s*(-?\d+)\s*$", str(value or "").replace(" ", "").replace(" ", ""))
    return int(m.group(1)) if m else None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%b %d %Y", "%d %b %Y",
                 "%a %b %d %Y", "%B %d %Y", "%d %B %Y", "%Y%m%d")


def parse_date(value: str) -> Optional[date]:
    """Дата из ответа сервера в одном из распространённых видов (None — не дата)."""
    text = re.sub(r"\s+", " ", str(value or "").strip().strip(".,;"))
    if not text:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # дата со временем: «Mon Jan 05 10:00:00 2027», «05.01.2027 10:00»
    cleaned = re.sub(r"\b\d{1,2}:\d{2}(:\d{2})?\b", "", text)
    cleaned = re.sub(r"\b(UTC|GMT|[+-]\d{4})\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def parse_epoch_or_date(value: str) -> Optional[datetime]:
    """Время из ответа: число секунд с 1970 года или дата (с временем или без)."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{9,11}", text):
        try:
            from datetime import timezone
            return datetime.fromtimestamp(int(text), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    day = parse_date(text)
    return datetime(day.year, day.month, day.day) if day else None


def parse_version(text: str) -> str:
    """«10.6.30|Linux|x86_64» → «10.6.30» (номер версии из ответа GET VERSION)."""
    m = re.search(r"\b(\d+\.\d+(?:\.\d+){0,2})", text or "")
    return m.group(1) if m else ""


def version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or "")[:4])


def parse_license(values: Dict[str, str], lines: Iterable[str]) -> Dict[str, object]:
    """Сведения о лицензии из ``SHOW LicenseInfo``: срок и пределы (что нашлось)."""
    info: Dict[str, object] = {}
    expires = None
    for key, value in values.items():
        low = key.lower()
        if any(word in low for word in ("expir", "valid", "until", "end")):
            day = parse_date(value)
            if day:
                expires = day if expires is None else min(expires, day)
    if expires is None:
        for ln in lines:
            if re.search(r"expir|valid|until", ln, re.I):
                day = parse_date(ln.split(":", 1)[-1] if ":" in ln else ln)
                if day:
                    expires = day
                    break
    if expires:
        info["expires"] = expires.isoformat()
    for key, value in values.items():
        low = key.lower()
        number = parse_int(value)
        if number is None:
            continue
        if "account" in low or "mailbox" in low or "user" in low:
            info.setdefault("accounts_limit", number)
        elif "domain" in low:
            info.setdefault("domains_limit", number)
    kind = get(values, "type", "licenseType", "license type", "edition", "product")
    if kind:
        info["type"] = kind
    return info
