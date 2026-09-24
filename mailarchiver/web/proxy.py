"""
Определение настоящего адреса клиента за обратным прокси и защитные
HTTP-заголовки.

Зачем отдельный модуль. Раньше сервис запускался с ``forwarded_allow_ips="*"``,
то есть uvicorn доверял заголовку ``X-Forwarded-For`` от ЛЮБОГО пира и брал из
него ЛЕВОЕ значение — целиком подконтрольное клиенту. Вся защита от подбора
пароля (она считает неудачи по адресу) обходилась одной строкой заголовка:
каждый запрос приходил «с нового адреса». Зеркальная беда при выключенном
``behind_proxy`` за nginx: у всех клиентов адрес один и тот же (адрес прокси),
и полсотни неудач одного злоумышленника закрывали вход всей организации.

Правильный разбор: идти по цепочке ``X-Forwarded-For`` СПРАВА НАЛЕВО, отбрасывая
хопы, которые перечислены в ``server.trusted_proxies``, и брать первый
недоверенный — его подделать нельзя, его подставил наш собственный прокси.
"""
from __future__ import annotations

import functools
import ipaddress
import json
import socket
from typing import Iterable, List, Optional, Sequence, Tuple

#: Что считается доверенным прокси по умолчанию: только локальные адреса.
DEFAULT_TRUSTED_PROXIES = "127.0.0.1, ::1"

#: Значение, отключающее проверку (совместимость со старым поведением).
_ANY_TOKENS = {"*", "all", "any", "0.0.0.0/0"}


def parse_trusted(value) -> Tuple[List, bool]:
    """Разобрать настройку в список сетей ipaddress и флаг «доверять всем».

    Принимает строку с разделителями «,»/«;»/пробел или готовый список.
    Отдельные адреса (``10.0.0.5``) превращаются в сети /32 и /128.
    Нераспознанные элементы молча пропускаются: конфигурация не должна
    обрушивать запуск сервиса из-за опечатки в одном адресе.
    """
    if value is None:
        items: Sequence = ()
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = str(value).replace(";", ",").replace(" ", ",").split(",")
    nets: List = []
    allow_any = False
    for raw in items:
        item = str(raw).strip()
        if not item:
            continue
        if item.lower() in _ANY_TOKENS:
            allow_any = True
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets, allow_any


def is_trusted(addr: str, nets: Iterable, allow_any: bool = False) -> bool:
    """Входит ли адрес в список доверенных прокси."""
    if allow_any:
        return True
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    for net in nets:
        try:
            if ip in net:
                return True
        except TypeError:      # смешение IPv4/IPv6 — не ошибка, просто не совпало
            continue
    return False


def _split_header(value: str) -> List[str]:
    out = []
    for part in (value or "").split(","):
        item = part.strip()
        if not item:
            continue
        # формат nginx/HAProxy иногда содержит порт или скобки IPv6
        if item.startswith("[") and "]" in item:
            item = item[1:item.index("]")]
        elif item.count(":") == 1 and "." in item:
            item = item.split(":", 1)[0]
        out.append(item)
    return out


def resolve_client_ip(peer: str, forwarded_for: Optional[str], nets, allow_any: bool = False) -> str:
    """Настоящий адрес клиента по цепочке X-Forwarded-For.

    ``peer`` — адрес непосредственного подключения (его подделать нельзя).
    Если сам пир не доверенный, заголовок игнорируется целиком.
    Иначе цепочка просматривается справа налево и возвращается первый хоп,
    которого нет в списке доверенных.
    """
    if not is_trusted(peer, nets, allow_any):
        return peer
    if allow_any:
        # «Доверять всем» означает, что недоверенных хопов нет вовсе, и разбор
        # цепочки теряет смысл. Раньше в этом случае возвращалось ЛЕВОЕ значение —
        # целиком подконтрольное клиенту: защита от подбора пароля отключалась
        # полностью, а в login_attempts.ip попадала любая строка. Отдаём адрес
        # соединения: подделать его нельзя.
        return peer
    chain = _split_header(forwarded_for or "")
    for candidate in reversed(chain):
        if not is_trusted(candidate, nets, allow_any):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue      # мусор в цепочке — идём дальше влево
            return candidate
    # вся цепочка доверенная (или её нет) — значит клиент и есть сам прокси
    return peer


def client_ip(request) -> str:
    """Адрес клиента для журналов, аудита и защиты от подбора пароля."""
    scope_ip = request.scope.get("ma_client_ip")
    if scope_ip:
        return scope_ip
    return request.client.host if request.client else ""


class ProxyHeadersMiddleware:
    """ASGI-обёртка: подставляет настоящий адрес клиента и схему.

    Собственная реализация вместо ``uvicorn --proxy-headers`` нужна по двум
    причинам: uvicorn берёт левое (подделываемое) значение цепочки и не умеет
    пропускать несколько доверенных хопов подряд.

    Настройки читаются на КАЖДОМ запросе через ``settings_getter`` (значение из
    БД перекрывает config.yaml). Иначе переключатель «За обратным прокси» в
    разделе «Настройки» сохранялся бы, показывался включённым — и не действовал
    никогда, потому что значения брались из файла один раз при сборке
    приложения.
    """

    def __init__(self, app, settings_getter=None, trusted="", enabled: bool = False):
        self.app = app
        self._settings_getter = settings_getter
        self._fallback = (str(trusted or ""), bool(enabled))
        self._cache_key = None
        self._cache_val = None

    def _settings(self):
        trusted, enabled = self._fallback
        if self._settings_getter is not None:
            try:
                got = self._settings_getter()
                if got is not None:
                    trusted, enabled = got
            except Exception:  # noqa: BLE001 — настройка не должна ронять запрос
                pass
        key = (str(trusted), bool(enabled))
        if key != self._cache_key:       # parse_trusted только при смене значения
            self._cache_key = key
            self._cache_val = parse_trusted(trusted) + (bool(enabled),)
        return self._cache_val

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        nets, allow_any, enabled = self._settings()
        client = scope.get("client")
        peer = client[0] if client else ""
        real = peer
        proxy_ok = False
        if enabled:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            proxy_ok = is_trusted(peer, nets, allow_any)
            real = resolve_client_ip(peer, headers.get("x-forwarded-for"), nets, allow_any)
            if proxy_ok:
                proto = (headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
                if proto in ("http", "https", "ws", "wss"):
                    if scope["type"] == "websocket":
                        scope["scheme"] = "wss" if proto in ("https", "wss") else "ws"
                    else:
                        scope["scheme"] = "https" if proto in ("https", "wss") else "http"
            if real and real != peer:
                scope["client"] = (real, client[1] if client else 0)
        scope["ma_client_ip"] = real
        scope["ma_peer_ip"] = peer
        scope["ma_proxy_ok"] = proxy_ok
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------
# Защитные HTTP-заголовки и проверка источника запроса
# --------------------------------------------------------------------------

#: Методы, меняющие состояние: для них требуется «свой» Origin.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_BAD_HOST_BODY = json.dumps({
    "error": True, "code": "bad_host",
    "message": "Запрос пришёл на незнакомое имя сервера и отклонён.",
    "hint": ("Первичную настройку (и работу без входа) выполняйте, открыв интерфейс по IP-адресу, "
             "localhost или имени из параметра server.public_url. Это защита от атаки DNS rebinding."),
}, ensure_ascii=False).encode("utf-8")

_BAD_ORIGIN_BODY = json.dumps({
    "error": True, "code": "bad_origin",
    "message": "Запрос пришёл с чужого сайта и отклонён.",
    "hint": ("Откройте интерфейс по его собственному адресу. Скрипты и утилиты, "
             "обращающиеся к API напрямую, должны передавать заголовок X-Requested-With."),
}, ensure_ascii=False).encode("utf-8")

#: Заголовки, которые ставятся на каждый ответ.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
}


#: Порт по умолчанию для схемы.
_DEFAULT_PORTS = {"http": "80", "https": "443", "ws": "80", "wss": "443"}


def split_origin(value: str):
    """Разобрать URL в (схема, хост, порт). Пустые значения — если это не URL.

    Хост IPv6 отдаётся без квадратных скобок. Порт всегда явный: если в URL его
    не было, подставляется порт по умолчанию для схемы.
    """
    item = (value or "").strip()
    if not item or item == "null" or "://" not in item:
        return "", "", ""
    scheme, rest = item.split("://", 1)
    scheme = scheme.lower()
    for sep in ("/", "?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    rest = rest.strip()
    if "@" in rest:                       # user:pass@host — отбрасываем
        rest = rest.rsplit("@", 1)[1]
    port = ""
    if rest.startswith("["):              # IPv6-литерал: [::1]:8493
        end = rest.find("]")
        if end == -1:
            return "", "", ""
        host = rest[1:end]
        tail = rest[end + 1:]
        if tail.startswith(":"):
            port = tail[1:]
    elif rest.count(":") == 1:
        host, port = rest.split(":", 1)
    else:
        host = rest
    return scheme, host.lower(), (port or _DEFAULT_PORTS.get(scheme, ""))


def origin_host(value: str) -> str:
    """Хост с портом из Origin/Referer (для сообщений и совместимости)."""
    _scheme, host, port = split_origin(value)
    if not host:
        return ""
    return f"{host}:{port}" if port else host


def _host_header_parts(host_header: str, scheme_hint: str):
    """Разобрать заголовок Host (без схемы) в (хост, порт)."""
    item = (host_header or "").strip().lower()
    if not item:
        return "", ""
    if item.startswith("["):
        end = item.find("]")
        if end == -1:
            return "", ""
        tail = item[end + 1:]
        return item[1:end], (tail[1:] if tail.startswith(":") else _DEFAULT_PORTS.get(scheme_hint, ""))
    if item.count(":") == 1:
        host, port = item.split(":", 1)
        return host, port
    return item, _DEFAULT_PORTS.get(scheme_hint, "")


def same_origin(origin: str, host_header: str, public_url: str = "", scheme: str = "") -> bool:
    """Совпадает ли источник запроса с адресом самого сервиса.

    Сравниваются СХЕМА, ХОСТ и ПОРТ. Отбрасывать порт нельзя: любая страница,
    отданная с того же имени хоста на другом порту (второй сервис на сервере,
    dev-сервер, соседний контейнер), иначе считалась бы «своей» и могла бы от
    имени вошедшего администратора слать запросы в API и открывать /ws.
    Порт по умолчанию для схемы подставляется с обеих сторон, поэтому
    ``https://mail.example.ru`` и ``mail.example.ru:443`` совпадают.

    Кроме заголовка Host допускается адрес из ``server.public_url`` — за прокси,
    который не переписывает Host, они различаются.
    """
    src_scheme, src_host, src_port = split_origin(origin)
    if not src_host:
        return False
    candidates = []
    h, p = _host_header_parts(host_header, src_scheme)
    if h:
        # Схему заголовок Host не несёт: берём схему запроса, а если её не
        # передали — считаем совместимой со схемой источника.
        candidates.append((scheme.lower() or src_scheme, h, p or _DEFAULT_PORTS.get(src_scheme, "")))
    pub_scheme, pub_host, pub_port = split_origin(public_url)
    if pub_host:
        candidates.append((pub_scheme, pub_host, pub_port))
    for c_scheme, c_host, c_port in candidates:
        if src_host != c_host:
            continue
        if src_port != c_port:
            continue
        if c_scheme and src_scheme != c_scheme:
            # http-страница не считается своей для https-сервиса и наоборот
            continue
        return True
    return False


@functools.lru_cache(maxsize=1)
def _machine_names() -> frozenset:
    names = set()
    for fn in (socket.gethostname, socket.getfqdn):
        try:
            name = (fn() or "").strip().lower().rstrip(".")
        except Exception:  # noqa: BLE001
            name = ""
        if name:
            names.add(name)
    return frozenset(names)


def host_is_trusted(host_header: str, public_url: str = "", forwarded_host: str = "") -> bool:
    """Указывает ли заголовок Host на «свой» адрес сервиса (защита от DNS rebinding).

    DNS rebinding: страница злоумышленника (evil.example) переключает DNS своего
    имени на адрес MailArchiver и шлёт запросы «со своего же» источника — Origin
    и Host совпадают, проверка Origin проходит. Cookie пользователя браузер при
    этом не отправит (имя другое), поэтому опасны места, где она не нужна:
    первичная настройка на свежей установке (/api/setup) и режим без входа.

    Доверяем IP-адресам (их набирают вручную — DNS не переключить), именам без
    точки (localhost, короткие имена локальной сети), имени из
    ``server.public_url``, имени самой машины и (за доверенным прокси)
    X-Forwarded-Host с тем же правилом.
    """
    for candidate in (host_header, forwarded_host):
        host, _port = _host_header_parts(candidate or "", "http")
        host = host.rstrip(".")
        if not host:
            continue
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            pass
        if "." not in host or host.endswith(".localhost"):
            # Имя без точки (localhost, backup, имя из hosts) разрешается
            # локальным DNS: зарегистрировать его и переключить злоумышленник
            # не может — для rebinding нужен его собственный домен.
            return True
        _scheme, pub_host, _p = split_origin(public_url)
        if pub_host and host == pub_host.rstrip("."):
            return True
        if host in _machine_names():
            return True
    return False


#: Служебный заголовок, который шлёт интерфейс. Страница чужого сайта не может
#: поставить его на межсайтовый запрос без разрешения CORS (а его сервис не
#: даёт), поэтому его наличие доказывает, что запрос не подделан формой.
MARKER_HEADER = "x-requested-with"


def check_origin(scope, public_url: str = "", require_marker: bool = False) -> bool:
    """Проверить Origin/Referer для WebSocket и небезопасных методов.

    ``require_marker`` — для запросов, меняющих состояние: если браузер не
    прислал ни Origin, ни Referer (старые браузеры на form-POST, расширения
    приватности, политика no-referrer), источник не проверить, и такой запрос
    принимается только со служебным заголовком ``X-Requested-With``. Раньше
    он просто пропускался — это была лазейка для межсайтовой подделки.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    origin = headers.get("origin") or ""
    if not origin:
        referer = headers.get("referer") or ""
        if not referer:
            if require_marker:
                return bool((headers.get(MARKER_HEADER) or "").strip())
            # WebSocket без Origin — не браузер (браузер шлёт его всегда).
            return True
        origin = referer
    scheme = str(scope.get("scheme") or "")
    if scheme in ("ws", "wss"):
        scheme = "http" if scheme == "ws" else "https"
    if same_origin(origin, headers.get("host", ""), public_url, scheme=scheme):
        return True
    # За прокси, который не переписывает Host, «свой» адрес приходит в
    # X-Forwarded-Host — иначе собственный интерфейс отвергал бы сам себя.
    # Заголовок принимаем ТОЛЬКО от доверенного прокси: иначе его подставил бы
    # сам клиент (ProxyHeadersMiddleware помечает такие запросы ma_proxy_ok).
    if not scope.get("ma_proxy_ok"):
        return False
    fwd_host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    return bool(fwd_host) and same_origin(origin, fwd_host, public_url, scheme=scheme)


class SecurityHeadersMiddleware:
    """Заголовки безопасности + проверка Origin на изменяющих запросах.

    Единственной защитой от CSRF был ``SameSite=lax`` у cookie, а он
    разрешает отправку cookie при переходе по ссылке. Здесь добавлены
    серверная проверка источника для POST/PUT/PATCH/DELETE и запрет
    встраивания интерфейса в чужой iframe (кликджекинг).
    """

    def __init__(self, app, public_url_getter=None, auth_enabled_getter=None):
        self.app = app
        self._public_url_getter = public_url_getter
        self._auth_enabled_getter = auth_enabled_getter

    def _auth_enabled(self) -> bool:
        if self._auth_enabled_getter is None:
            return True
        try:
            return bool(self._auth_enabled_getter())
        except Exception:      # noqa: BLE001
            return True

    def _host_guard_needed(self, path: str) -> bool:
        """Где проверять Host: там, где запрос опасен и без cookie (см. host_is_trusted)."""
        if path == "/api/setup":
            return True
        return path.startswith("/api/") and not self._auth_enabled()

    def _public_url(self) -> str:
        if self._public_url_getter is None:
            return ""
        try:
            return str(self._public_url_getter() or "")
        except Exception:      # noqa: BLE001 — настройка не должна ронять запрос
            return ""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self._host_guard_needed(path):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            fwd = (headers.get("x-forwarded-host") or "").split(",")[0].strip() if scope.get("ma_proxy_ok") else ""
            if not host_is_trusted(headers.get("host", ""), self._public_url(), fwd):
                body = _BAD_HOST_BODY
                await send({"type": "http.response.start", "status": 421,
                            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        if scope.get("method", "").upper() in _UNSAFE_METHODS and \
                not check_origin(scope, self._public_url(), require_marker=True):
            body = _BAD_ORIGIN_BODY
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return

        no_store = path.startswith("/api/")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.decode("latin-1").lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name.lower() not in present:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                if no_store and "cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, send_wrapper)
