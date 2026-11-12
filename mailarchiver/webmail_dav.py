"""
Календарь и контакты почтового сервера (CalDAV, CardDAV — 1.20.0).

Администратор указывает адрес сервера (например ``https://mail.company.ru`` — тогда
адрес ищется сам через ``/.well-known/caldav`` и ``/.well-known/carddav``, или прямой
адрес календаря / адресной книги). Вход — логином и паролем ящика. Найденные календарь
и адресная книга запоминаются для ящика:

* **Контакты** сервера загружаются в адресную книгу клиента (кнопкой и при открытии
  настроек): подсказка адресов при наборе письма знает их.
* **Календарь**: ближайшие события (на 30 дней вперёд) — в окне «📅 Календарь»; новое
  событие создаётся там же; принятое приглашение («Принять», «Под вопросом») кладётся
  в календарь сервера.

Протоколы — WebDAV (RFC 4918), CalDAV (RFC 4791), CardDAV (RFC 6352): PROPFIND для поиска
коллекций, REPORT для выборки, PUT для нового события. Пароль ящика уходит только по
https (по http — лишь на эту же машину).
"""
from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from . import webmail_tools as wt
from .errors import ValidationError
from .logging_setup import get_logger
from .models import Account, AuthType
from .util import utcnow_iso

log = get_logger("webmail.dav")

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "urn:ietf:params:xml:ns:carddav"}
MAX_RESPONSE = 8 * 1024 * 1024
TIMEOUT = 20


# ---------------------------------------------------------------------------
#  HTTP
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401 - перенаправления разбираем сами
        return None


def _local(host: str) -> bool:
    return (host or "").lower() in ("localhost", "127.0.0.1", "::1")


def _request(account: Account, method: str, url: str, body: bytes = b"", depth: Optional[str] = None,
             ctype: str = "application/xml; charset=utf-8", extra: Optional[Dict[str, str]] = None,
             username: str = "") -> Tuple[int, Dict[str, str], bytes, str]:
    """Запрос к серверу с входом ящика; перенаправления (до 5) — с тем же методом. → (код, заголовки, тело, адрес).

    Перенаправление — только в пределах исходного сервера (схема, имя, порт; 1.21.0): пароль ящика не
    уходит на другой сервер, а чужой сервер не направит запрос архива во внутреннюю сеть.
    """
    origin = _origin(url)
    for _ in range(5):
        parts = urlsplit(url)
        if _origin(url) != origin:
            raise ValidationError(f"Сервер календаря перенаправляет на другой адрес: {url[:300]}",
                                  hint="Если это сервер вашей организации, укажите этот адрес в настройках календаря "
                                       "и контактов — пароль ящика на другой сервер сам не отправляется.",
                                  code="dav_redirect")
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValidationError("Адрес календаря и контактов — вида https://сервер/…")
        if parts.scheme == "http" and not _local(parts.hostname):
            raise ValidationError("Пароль ящика уходит только по https:// — укажите адрес с https.", code="dav_https")
        headers = {"User-Agent": "MailArchiver", "Authorization": "Basic " + base64.b64encode(
            f"{username or account.username}:{account.password}".encode("utf-8")).decode("ascii")}
        if body:
            headers["Content-Type"] = ctype
        if depth is not None:
            headers["Depth"] = depth
        headers.update(extra or {})
        req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_RESPONSE), url
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                continue
            return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read(65536) if exc.fp else b"", url
        except (urllib.error.URLError, OSError) as exc:
            raise ValidationError(f"Сервер календаря и контактов не отвечает: {getattr(exc, 'reason', exc)}") from None
    raise ValidationError("Слишком много перенаправлений на сервере календаря и контактов.")


def _origin(url: str) -> Tuple[str, str, int]:
    parts = urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = -1
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def _xml(data: bytes):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValidationError(f"Сервер прислал непонятный ответ: {exc}") from None


def _check(status: int, what: str) -> None:
    if status == 401:
        raise ValidationError(f"{what}: сервер не принял логин и пароль ящика.", code="dav_auth",
                              hint="Некоторые серверы (Яндекс, Mail.ru, Nextcloud) требуют пароль приложения — "
                                   "задайте его ящику.")
    if status >= 400:
        raise ValidationError(f"{what}: сервер ответил {status}.")


def _propfind(account: Account, url: str, props: str, depth: str = "0", username: str = "") -> Tuple[object, str]:
    body = (f'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="{NS["c"]}" '
            f'xmlns:a="{NS["a"]}"><d:prop>{props}</d:prop></d:propfind>').encode("utf-8")
    status, _h, data, final = _request(account, "PROPFIND", url, body, depth=depth, username=username)
    _check(status, "Поиск календаря и контактов")
    return _xml(data), final


def _href(el, path: str) -> str:
    x = el.find(path, NS)
    return (x.text or "").strip() if x is not None and x.text else ""


# ---------------------------------------------------------------------------
#  Поиск коллекций
# ---------------------------------------------------------------------------
def discover(account: Account, url: str, username: str = "") -> Dict[str, List[Dict[str, str]]]:
    """Календари и адресные книги по адресу сервера (или прямому адресу коллекции)."""
    url = (url or "").strip()
    if not url:
        raise ValidationError("Укажите адрес сервера календаря и контактов.")
    if not re.match(r"(?i)^https?://", url):
        url = "https://" + url
    found = {"calendars": [], "addressbooks": []}
    homes: List[str] = []
    starts = [url]
    base = "{0.scheme}://{0.netloc}".format(urlsplit(url))
    if urlsplit(url).path in ("", "/"):
        starts = [base + "/.well-known/caldav", base + "/.well-known/carddav", base + "/"]
    seen = set()
    origin = _origin(url)
    elsewhere: List[ValidationError] = []
    for start in starts:
        try:
            root, final = _propfind(account, start, "<d:resourcetype/><d:displayname/><d:current-user-principal/>"
                                                    "<c:calendar-home-set/><a:addressbook-home-set/>", username=username)
        except ValidationError as exc:
            if exc.code in ("dav_auth", "dav_https"):
                raise
            if exc.code == "dav_redirect":
                elsewhere.append(exc)
            continue
        for resp in root.findall("d:response", NS):
            href = urljoin(final, _href(resp, "d:href"))
            if _origin(href) == origin:
                _collect(resp, href, found, seen)
            principal = _href(resp, ".//d:current-user-principal/d:href")
            for path in (".//c:calendar-home-set/d:href", ".//a:addressbook-home-set/d:href"):
                h = _href(resp, path)
                if h:
                    homes.append(urljoin(final, h))
            if principal and _origin(urljoin(final, principal)) != origin:
                principal = ""                    # пароль ящика — только исходному серверу (1.21.0)
            if principal:
                try:
                    proot, pfinal = _propfind(account, urljoin(final, principal),
                                              "<c:calendar-home-set/><a:addressbook-home-set/>", username=username)
                    for presp in proot.findall("d:response", NS):
                        for path in (".//c:calendar-home-set/d:href", ".//a:addressbook-home-set/d:href"):
                            h = _href(presp, path)
                            if h:
                                homes.append(urljoin(pfinal, h))
                except ValidationError:
                    pass
    for home in dict.fromkeys(homes):
        if _origin(home) != origin:
            continue
        try:
            root, final = _propfind(account, home, "<d:resourcetype/><d:displayname/>", depth="1", username=username)
        except ValidationError:
            continue
        for resp in root.findall("d:response", NS):
            href = urljoin(final, _href(resp, "d:href"))
            if _origin(href) == origin:
                _collect(resp, href, found, seen)
    if not found["calendars"] and not found["addressbooks"]:
        if elsewhere:
            raise elsewhere[0]
        raise ValidationError("По этому адресу не найдено ни календаря, ни адресной книги.",
                              hint="Укажите адрес сервера (https://mail.company.ru) или прямой адрес календаря.")
    return found


def _collect(resp, href: str, found: Dict, seen: set) -> None:
    rt = resp.find(".//d:resourcetype", NS)
    if rt is None or href in seen:
        return
    name = _href(resp, ".//d:displayname") or urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]
    if rt.find("c:calendar", NS) is not None:
        seen.add(href)
        found["calendars"].append({"href": href, "name": name})
    elif rt.find("a:addressbook", NS) is not None:
        seen.add(href)
        found["addressbooks"].append({"href": href, "name": name})


# ---------------------------------------------------------------------------
#  Настройки ящика
# ---------------------------------------------------------------------------
def settings(svc, account: Account) -> Dict:
    try:
        cur = json.loads(wt._kv_get(svc, int(account.id), "dav") or "{}")  # noqa: SLF001
    except (TypeError, ValueError):
        cur = {}
    cur["supported"] = account.auth_type == AuthType.PASSWORD and bool(account.password)
    return cur


def _store(svc, account: Account, **changes) -> Dict:
    cur = {k: v for k, v in settings(svc, account).items() if k != "supported"}
    cur.update(changes)
    wt._kv_set(svc, int(account.id), "dav", json.dumps(cur, ensure_ascii=False))  # noqa: SLF001
    return settings(svc, account)


def configure(svc, account: Account, url: str, *, calendar: str = "", addressbook: str = "", username: str = "",
              actor: str = "") -> Dict:
    """Найти календари и адресные книги и запомнить выбранные (или первые)."""
    if not (account.auth_type == AuthType.PASSWORD and account.password):
        raise ValidationError("Календарь и контакты доступны ящику с паролем.")
    if not (url or "").strip():
        wt._kv_set(svc, int(account.id), "dav", "{}")  # noqa: SLF001
        return settings(svc, account)
    found = discover(account, url, username)
    cal = calendar if calendar in [c["href"] for c in found["calendars"]] else \
        (found["calendars"][0]["href"] if found["calendars"] else "")
    book = addressbook if addressbook in [b["href"] for b in found["addressbooks"]] else \
        (found["addressbooks"][0]["href"] if found["addressbooks"] else "")
    out = _store(svc, account, url=url.strip(), username=username.strip(), calendar=cal, addressbook=book,
                 found=found, error="", configured_at=utcnow_iso())
    if actor:
        svc.db.add_audit(actor, "webmail_dav", f"ящик «{account.name}»: {url.strip()} — календарей "
                         f"{len(found['calendars'])}, адресных книг {len(found['addressbooks'])}")
    return out


def _need(svc, account: Account, key: str) -> Tuple[Dict, str]:
    cur = settings(svc, account)
    href = cur.get(key) or ""
    if not href:
        raise ValidationError("Календарь не подключён." if key == "calendar" else "Адресная книга не подключена.",
                              hint="Почта → ⚙ Настройки ящика → «Календарь и контакты».")
    return cur, href


# ---------------------------------------------------------------------------
#  Контакты
# ---------------------------------------------------------------------------
def sync_contacts(svc, account: Account) -> Dict:
    cur, href = _need(svc, account, "addressbook")
    body = (f'<?xml version="1.0" encoding="utf-8"?><a:addressbook-query xmlns:d="DAV:" xmlns:a="{NS["a"]}">'
            f'<d:prop><d:getetag/><a:address-data/></d:prop></a:addressbook-query>').encode("utf-8")
    status, _h, data, _f = _request(account, "REPORT", href, body, depth="1", username=cur.get("username", ""))
    _check(status, "Загрузка контактов")
    root = _xml(data)
    cards = [x.text for x in root.iter("{urn:ietf:params:xml:ns:carddav}address-data") if x.text]
    res = wt.import_book(svc, int(account.id), "server.vcf", "\r\n".join(cards).encode("utf-8")) if cards else \
        {"added": 0, "updated": 0, "skipped": 0, "total": 0}
    _store(svc, account, contacts_synced_at=utcnow_iso(), contacts=len(cards))
    return res


def _vcard_escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def push_contact(svc, account: Account, *, name: str, email: str, phone: str = "", org: str = "",
                 note: str = "") -> bool:
    """Новый контакт адресной книги — и в адресную книгу сервера (CardDAV, 1.21.0). True — записан.

    Книга сервера не подключена — ничего не делает: контакт остаётся в книге архива.
    """
    cur = settings(svc, account)
    href = cur.get("addressbook") or ""
    if not href or not email:
        return False
    uid = str(uuid.uuid4())
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{_vcard_escape(name or email)}",
             f"N:{_vcard_escape(name or email)};;;;", f"EMAIL;TYPE=INTERNET:{email}"]
    if phone:
        lines.append(f"TEL:{_vcard_escape(phone)}")
    if org:
        lines.append(f"ORG:{_vcard_escape(org)}")
    if note:
        lines.append(f"NOTE:{_vcard_escape(note[:2000])}")
    lines.append("END:VCARD")
    target = href.rstrip("/") + f"/{uid}.vcf"
    status, _h, _d, _f = _request(account, "PUT", target, ("\r\n".join(lines) + "\r\n").encode("utf-8"),
                                  ctype="text/vcard; charset=utf-8", extra={"If-None-Match": "*"},
                                  username=cur.get("username", ""))
    _check(status, "Запись контакта")
    return True


# ---------------------------------------------------------------------------
#  Календарь
# ---------------------------------------------------------------------------
def _parse_dt(value: str, params: Dict[str, str]) -> Optional[datetime]:
    v = (value or "").strip()
    try:
        if len(v) == 8:
            return datetime.strptime(v, "%Y%m%d").replace(tzinfo=timezone.utc)
        if v.endswith("Z"):
            return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
        tzid = params.get("TZID", "")
        if tzid:
            try:
                from zoneinfo import ZoneInfo
                return dt.replace(tzinfo=ZoneInfo(tzid))
            except Exception:  # noqa: BLE001 - нестандартное имя пояса (Windows) — считаем местным
                pass
        return dt.astimezone()
    except ValueError:
        return None


def parse_events(ical: str) -> List[Dict]:
    """События (VEVENT) из текста iCalendar."""
    out: List[Dict] = []
    cur: Optional[Dict] = None
    for name, params, value in wt._ical_unfold(ical):  # noqa: SLF001
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {}
            continue
        if name == "END" and value.upper() == "VEVENT" and cur is not None:
            start = _parse_dt(cur.get("DTSTART", ({}, ""))[1], cur.get("DTSTART", ({}, ""))[0])
            end = _parse_dt(cur.get("DTEND", ({}, ""))[1], cur.get("DTEND", ({}, ""))[0])
            if start is not None:
                all_day = len(cur.get("DTSTART", ({}, ""))[1].strip()) == 8
                out.append({"uid": cur.get("UID", ({}, ""))[1], "summary": wt._ical_text(cur.get("SUMMARY", ({}, ""))[1]),  # noqa: SLF001
                            "location": wt._ical_text(cur.get("LOCATION", ({}, ""))[1]),  # noqa: SLF001
                            "description": wt._ical_text(cur.get("DESCRIPTION", ({}, ""))[1])[:1000],  # noqa: SLF001
                            "start": start.isoformat(), "end": end.isoformat() if end else "",
                            "all_day": all_day, "rrule": bool(cur.get("RRULE"))})
            cur = None
            continue
        if cur is not None and name not in cur:
            cur[name] = (params, value)
    return out


def events(svc, account: Account, days: int = 30, since: str = "", until: str = "") -> List[Dict]:
    """События календаря: ближайшие ``days`` дней или промежуток ``since``…``until`` (вид «месяц», 1.21.0)."""
    cur, href = _need(svc, account, "calendar")
    now = datetime.now(timezone.utc)
    lo_dt, hi_dt = now - timedelta(days=1), now + timedelta(days=max(1, min(366, days)))
    if since and until:
        try:
            lo_dt, hi_dt = datetime.fromisoformat(since), datetime.fromisoformat(until)
        except ValueError:
            raise ValidationError("Промежуток календаря — даты ГГГГ-ММ-ДД.") from None
        if lo_dt.tzinfo is None:
            lo_dt, hi_dt = lo_dt.astimezone(), hi_dt.astimezone()
        if hi_dt <= lo_dt or (hi_dt - lo_dt).days > 400:
            raise ValidationError("Промежуток календаря — не больше года.")
    start = lo_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = hi_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = (f'<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="{NS["c"]}">'
            f'<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR">'
            f'<c:comp-filter name="VEVENT"><c:time-range start="{start}" end="{end}"/></c:comp-filter>'
            f'</c:comp-filter></c:filter></c:calendar-query>').encode("utf-8")
    status, _h, data, _f = _request(account, "REPORT", href, body, depth="1", username=cur.get("username", ""))
    _check(status, "Загрузка календаря")
    root = _xml(data)
    out: List[Dict] = []
    for resp in root.findall("d:response", NS):
        x = resp.find(".//c:calendar-data", NS)
        if x is None or not x.text:
            continue
        res_href = urljoin(href, _href(resp, "d:href"))
        etag = _href(resp, ".//d:getetag")
        for ev in parse_events(x.text):
            ev["href"], ev["etag"] = res_href, etag
            out.append(ev)
    lo = lo_dt.astimezone(timezone.utc).isoformat()
    out = [e for e in out if (e["end"] or e["start"]) >= lo or e["rrule"]]
    return sorted(out, key=lambda e: e["start"])[:1000]


def _event_url(svc, account: Account, href: str) -> Tuple[Dict, str]:
    """Адрес события — только внутри подключённого календаря (иначе пароль ушёл бы по чужому адресу)."""
    cur, cal = _need(svc, account, "calendar")
    target = urljoin(cal, href or "")
    if _origin(target) != _origin(cal) or not urlsplit(target).path.startswith(urlsplit(cal).path) \
            or urlsplit(target).path.rstrip("/") == urlsplit(cal).path.rstrip("/"):
        raise ValidationError("Событие не из подключённого календаря.")
    return cur, target


def _set_prop(block: str, name: str, value: Optional[str]) -> str:
    """Заменить (или добавить, или убрать — value is None) свойство в тексте VEVENT (строки уже развёрнуты)."""
    lines = [ln for ln in block.split("\r\n") if not re.match(rf"(?i)^{name}[;:]", ln)]
    if value is not None:
        lines.insert(len(lines) - 1 if lines and lines[-1].upper() == "END:VEVENT" else len(lines), value)
    return "\r\n".join(lines)


def update_event(svc, account: Account, href: str, etag: str, *, summary: str, start: str, end: str = "",
                 location: str = "", description: str = "", all_day: bool = False, actor: str = "") -> Dict:
    """Изменить событие календаря сервера (1.21.0): название, время, место, описание.

    Остальное (участники, напоминания) сохраняется; повторяющиеся события меняются в программе календаря.
    Событие изменили на сервере с момента загрузки (другой ETag) — отказ, без перезаписи чужой правки.
    """
    cur, target = _event_url(svc, account, href)
    status, headers, data, _f = _request(account, "GET", target, username=cur.get("username", ""))
    _check(status, "Чтение события")
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\n", "\r\n")
    text = re.sub(r"\r\n[ \t]", "", text)                           # развернуть продолжения строк
    if re.search(r"(?im)^RRULE[;:]", text):
        raise ValidationError("Повторяющееся событие меняйте в программе календаря (Outlook, Thunderbird, телефон).")
    summary = (summary or "").strip()[:500]
    if not summary:
        raise ValidationError("Укажите название события.")
    dts = _event_times(start, end, all_day)
    m = re.search(r"(?is)BEGIN:VEVENT\r\n.*?END:VEVENT", text)
    if m is None:
        raise ValidationError("В событии нет VEVENT.")
    block = m.group(0)
    seq = re.search(r"(?im)^SEQUENCE:(\d+)", block)
    for name, value in (("DTSTART", dts[0]), ("DTEND", dts[1]), ("DURATION", None),
                        ("SUMMARY", f"SUMMARY:{_ical_escape(summary)}"),
                        ("LOCATION", f"LOCATION:{_ical_escape(location[:500])}" if location else None),
                        ("DESCRIPTION", f"DESCRIPTION:{_ical_escape(description[:4000])}" if description else None),
                        ("SEQUENCE", f"SEQUENCE:{int(seq.group(1)) + 1 if seq else 1}"),
                        ("DTSTAMP", "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))):
        block = _set_prop(block, name, value)
    text = text[:m.start()] + block + text[m.end():]
    extra = {"If-Match": etag} if etag else {}
    status, _h, _d, _f = _request(account, "PUT", target, text.encode("utf-8"), ctype="text/calendar; charset=utf-8",
                                  extra=extra, username=cur.get("username", ""))
    if status == 412:
        raise ValidationError("Событие изменили в другой программе — обновите календарь и повторите.")
    _check(status, "Запись события")
    if actor:
        svc.db.add_audit(actor, "webmail_event", f"ящик «{account.name}»: изменено событие «{summary[:100]}»")
    return {"ok": True}


def delete_event(svc, account: Account, href: str, etag: str = "", actor: str = "") -> Dict:
    cur, target = _event_url(svc, account, href)
    extra = {"If-Match": etag} if etag else {}
    status, _h, _d, _f = _request(account, "DELETE", target, extra=extra, username=cur.get("username", ""))
    if status == 412:
        raise ValidationError("Событие изменили в другой программе — обновите календарь и повторите.")
    if status != 404:
        _check(status, "Удаление события")
    if actor:
        svc.db.add_audit(actor, "webmail_event", f"ящик «{account.name}»: удалено событие {target.rsplit('/', 1)[-1]}")
    return {"ok": True}


def _event_times(start: str, end: str, all_day: bool) -> Tuple[str, str]:
    try:
        st = datetime.fromisoformat(start)
        en = datetime.fromisoformat(end) if end else st + (timedelta(days=1) if all_day else timedelta(hours=1))
    except ValueError:
        raise ValidationError("Время события — в виде ГГГГ-ММ-ДДTЧЧ:ММ.") from None
    if st.tzinfo is None:
        st, en = st.astimezone(), (en.astimezone() if en.tzinfo is None else en)
    if en <= st:
        raise ValidationError("Конец события раньше начала.")
    if all_day:
        return f"DTSTART;VALUE=DATE:{st.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{en.strftime('%Y%m%d')}"
    return (f"DTSTART:{st.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{en.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")


def _ical_escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def put_ics(svc, account: Account, ical: str, uid: str) -> str:
    cur, href = _need(svc, account, "calendar")
    name = re.sub(r"[^A-Za-z0-9._@-]+", "-", uid or str(uuid.uuid4()))[:120] + ".ics"
    target = href.rstrip("/") + "/" + name
    status, _h, _d, _f = _request(account, "PUT", target, ical.encode("utf-8"), ctype="text/calendar; charset=utf-8",
                                  username=cur.get("username", ""))
    if status == 412:
        # такое событие уже есть (приглашение приняли второй раз) — заменяем
        status, _h, _d, _f = _request(account, "PUT", target, ical.encode("utf-8"),
                                      ctype="text/calendar; charset=utf-8", username=cur.get("username", ""))
    _check(status, "Запись события")
    return target


def add_event(svc, account: Account, *, summary: str, start: str, end: str = "", location: str = "",
              description: str = "", all_day: bool = False, actor: str = "") -> Dict:
    summary = (summary or "").strip()[:500]
    if not summary:
        raise ValidationError("Укажите название события.")
    dts = list(_event_times(start, end, all_day))
    uid = f"{uuid.uuid4()}@mailarchiver"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MailArchiver//RU", "BEGIN:VEVENT", f"UID:{uid}",
             f"DTSTAMP:{stamp}", *dts, f"SUMMARY:{_ical_escape(summary)}"]
    if location:
        lines.append(f"LOCATION:{_ical_escape(location[:500])}")
    if description:
        lines.append(f"DESCRIPTION:{_ical_escape(description[:4000])}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    put_ics(svc, account, "\r\n".join(lines) + "\r\n", uid)
    if actor:
        svc.db.add_audit(actor, "webmail_event", f"ящик «{account.name}»: событие «{summary[:100]}»")
    return {"ok": True, "uid": uid}


def add_invitation(svc, account: Account, ical_text: str, partstat: str) -> bool:
    """Принятое приглашение — в календарь сервера (если подключён). True — записано."""
    cur = settings(svc, account)
    if not cur.get("calendar") or not ical_text:
        return False
    info = wt.parse_invite(ical_text)
    if not info.get("uid"):
        return False
    text = re.sub(r"(?im)^METHOD:[^\r\n]*\r?\n", "", ical_text)
    if partstat == "TENTATIVE" and "STATUS:" not in text:
        text = re.sub(r"(?i)(BEGIN:VEVENT\r?\n)", r"\1STATUS:TENTATIVE\r\n", text, count=1)
    try:
        put_ics(svc, account, text, info["uid"])
        return True
    except ValidationError as exc:
        log.info("Приглашение не записано в календарь: %s", exc.message)
        return False
