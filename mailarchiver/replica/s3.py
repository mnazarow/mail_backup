"""
Минимальный клиент S3-совместимого хранилища (AWS Signature Version 4).

Без сторонних библиотек — только ``http.client`` и ``hmac``: копии архива
нужны PUT (в том числе многочастная загрузка крупных файлов), GET в файл,
HEAD, DELETE (по одному и пачкой) и список объектов (ListObjectsV2).
Подходит для Yandex Object Storage, VK Cloud, MinIO, AWS и других хранилищ с
API S3.

Подпись проверяется тестами на эталонных примерах из документации AWS
(tests/test_replica.py). Тело каждого запроса подписывается целиком
(x-amz-content-sha256) и дополнительно сверяется хранилищем по Content-MD5:
испорченная в пути порция будет отвергнута, а не молча сохранена.

Соединения переиспользуются (keep-alive) — отдельное на каждый поток: копия
сотен тысяч небольших писем упирается в задержку сети, и новое TLS-рукопожатие
на каждый файл замедлило бы её в разы.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import os
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from ..errors import ReplicaError

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
#: Файлы крупнее грузятся по частям (многочастная загрузка), и в памяти
#: одновременно держится только одна часть.
PART_SIZE = 32 * 1024 * 1024
#: Сколько ключей удалять одним запросом (предел API — 1000).
DELETE_BATCH = 1000
#: Повторы при временных сбоях (сеть, 5xx, SlowDown): паузы 1, 2, 4 с.
RETRIES = 3
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
#: Коды ошибок, которые лечатся повтором. Перекос часов (RequestTimeTooSkewed)
#: сюда намеренно не входит: повтор с теми же часами ничего не даст.
_RETRY_CODES = {"RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable"}
_READ_CHUNK = 1024 * 1024


# ---------------------------------------------------------------------------
#  Подпись AWS SigV4
# ---------------------------------------------------------------------------
def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """Кодирование по правилам SigV4: всё, кроме A-Z a-z 0-9 - _ . ~ (и «/» в пути)."""
    return urllib.parse.quote(value, safe="-_.~" if encode_slash else "-_.~/")


def canonical_query(params) -> str:
    items = params.items() if isinstance(params, dict) else (params or [])
    pairs = sorted((uri_encode(str(k)), uri_encode("" if v is None else str(v))) for k, v in items)
    return "&".join(f"{k}={v}" for k, v in pairs)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str = "s3") -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    return _hmac(_hmac(_hmac(k_date, region), service), "aws4_request")


def sign_request(method: str, canonical_uri: str, query, headers: Dict[str, str], payload_hash: str, *,
                 access_key: str, secret_key: str, region: str, amz_date: str,
                 service: str = "s3") -> str:
    """Значение заголовка Authorization (AWS4-HMAC-SHA256).

    ``canonical_uri`` — путь уже в закодированном виде (ровно как в строке
    запроса); ``headers`` — все подписываемые заголовки. Значения заголовков
    нормализуются по правилам SigV4: пробелы по краям убираются, повторы
    схлопываются.
    """
    norm: Dict[str, str] = {}
    for name, value in headers.items():
        norm[str(name).strip().lower()] = " ".join(str(value).strip().split())
    signed = ";".join(sorted(norm))
    canon_headers = "".join(f"{name}:{norm[name]}\n" for name in sorted(norm))
    creq = "\n".join([method.upper(), canonical_uri or "/", canonical_query(query), canon_headers,
                      signed, payload_hash])
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(creq.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, date, region, service),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},"
            f"SignedHeaders={signed},Signature={signature}")


def _md5_b64(data: bytes) -> str:
    try:
        digest = hashlib.md5(data, usedforsecurity=False).digest()  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - старые сборки без usedforsecurity
        digest = hashlib.md5(data).digest()  # noqa: S324 - контрольная сумма, не криптография
    return base64.b64encode(digest).decode("ascii")


def _local(tag: str) -> str:
    """Имя XML-элемента без пространства имён (MinIO и др. иногда его не ставят)."""
    return tag.rsplit("}", 1)[-1]


def _child_text(elem, name: str, default: str = "") -> str:
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return default


def _parse_error(body: bytes) -> Tuple[str, str]:
    """(код, сообщение) из XML-ответа об ошибке."""
    if not body:
        return "", ""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = body[:300].decode("utf-8", "replace").strip()
        return "", text
    return _child_text(root, "Code"), _child_text(root, "Message")


# ---------------------------------------------------------------------------
#  Клиент
# ---------------------------------------------------------------------------
@dataclass
class S3Config:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    path_style: bool = True
    verify_ssl: bool = True
    timeout: float = 120.0
    storage_class: str = ""


class S3Client:
    def __init__(self, cfg: S3Config) -> None:
        endpoint = (cfg.endpoint or "").strip()
        parts = urllib.parse.urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ReplicaError(f"Адрес хранилища S3 указан неверно: «{endpoint}».",
                               hint="Пример: https://storage.yandexcloud.net")
        if not (cfg.bucket or "").strip():
            raise ReplicaError("Не указан бакет S3.", hint="Укажите имя бакета в настройках копии.")
        if not cfg.access_key or not cfg.secret_key:
            raise ReplicaError("Не заданы ключи доступа S3.",
                               hint="Укажите идентификатор ключа и секретный ключ сервисного аккаунта.")
        self.cfg = cfg
        self.bucket = cfg.bucket.strip()
        self.region = (cfg.region or "").strip() or "us-east-1"
        self.https = parts.scheme == "https"
        self.hostname = parts.hostname
        self.port = parts.port
        self.base_path = (parts.path or "").rstrip("/")
        self._local = threading.local()

    # -- адресация ------------------------------------------------------------
    def _connect_host(self) -> str:
        return self.hostname if self.cfg.path_style else f"{self.bucket}.{self.hostname}"

    def _host_header(self) -> str:
        host = self._connect_host()
        if ":" in host:                     # IPv6
            host = f"[{host}]"
        default = 443 if self.https else 80
        if self.port and self.port != default:
            host += f":{self.port}"
        return host

    def _path(self, key: str = "", *, bucket_level: bool = False) -> str:
        base = self.base_path
        if self.cfg.path_style:
            path = f"{base}/{uri_encode(self.bucket)}"
            if not bucket_level:
                path += "/" + uri_encode(key, encode_slash=False)
            return path
        if bucket_level:
            return f"{base}/"
        return f"{base}/" + uri_encode(key, encode_slash=False)

    def describe(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.hostname}/{self.bucket}"

    # -- соединения --------------------------------------------------------------
    def _conn(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            host = self._connect_host()
            if self.https:
                ctx = ssl.create_default_context()
                if not self.cfg.verify_ssl:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                conn = http.client.HTTPSConnection(host, self.port or 443, timeout=self.cfg.timeout,
                                                   context=ctx)
            else:
                conn = http.client.HTTPConnection(host, self.port or 80, timeout=self.cfg.timeout)
            self._local.conn = conn
        return conn

    def _drop_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._drop_conn()

    # -- запрос с подписью и повторами -------------------------------------------
    def _send(self, method: str, path: str, query: Optional[dict], body: bytes,
              extra_signed: Optional[Dict[str, str]] = None,
              payload_hash: Optional[str] = None):
        """Отправить запрос и вернуть ответ (тело ещё не прочитано)."""
        amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if payload_hash is None:
            payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        signed = {"host": self._host_header(), "x-amz-date": amz_date,
                  "x-amz-content-sha256": payload_hash}
        for name, value in (extra_signed or {}).items():
            signed[name.lower()] = value
        auth = sign_request(method, path, query or {}, signed, payload_hash,
                            access_key=self.cfg.access_key, secret_key=self.cfg.secret_key,
                            region=self.region, amz_date=amz_date)
        headers = dict(signed)
        headers["Authorization"] = auth
        headers["Content-Length"] = str(len(body))
        qs = canonical_query(query or {})
        url = path + ("?" + qs if qs else "")
        conn = self._conn()
        conn.request(method, url, body=body if body else None, headers=headers)
        return conn.getresponse()

    def _call(self, method: str, key: str = "", *, query: Optional[dict] = None, body: bytes = b"",
              extra_signed: Optional[Dict[str, str]] = None, bucket_level: bool = False,
              what: str = "", ok=(200, 204)) -> Tuple[int, Dict[str, str], bytes]:
        path = self._path(key, bucket_level=bucket_level)
        last_error = ""
        for attempt in range(RETRIES + 1):
            try:
                resp = self._send(method, path, query, body, extra_signed)
                data = resp.read()
                status = resp.status
                headers = {k.lower(): v for k, v in resp.getheaders()}
                if resp.will_close:
                    self._drop_conn()
            except (OSError, http.client.HTTPException) as exc:
                self._drop_conn()
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise ReplicaError(f"Нет связи с хранилищем S3 ({self.describe()}): {last_error}",
                                   hint="Проверьте адрес хранилища, сеть и доступ с сервера наружу.",
                                   retryable=True) from exc
            if status in ok:
                return status, headers, data
            code, message = _parse_error(data)
            if (status in _RETRY_STATUSES or code in _RETRY_CODES) and attempt < RETRIES:
                last_error = f"{status} {code}".strip()
                time.sleep(2 ** attempt)
                continue
            raise self._error(status, code, message, what or f"{method} {key or self.bucket}")
        raise ReplicaError(f"Хранилище S3 не ответило: {last_error}", retryable=True)  # pragma: no cover

    def _error(self, status: int, code: str, message: str, what: str) -> ReplicaError:
        detail = f"{status} {code}".strip() + (f" — {message}" if message else "")
        if code == "RequestTimeTooSkewed":
            return ReplicaError(f"Хранилище S3 отвергло запрос: часы сервера расходятся с точным временем ({detail}).",
                                hint="Включите синхронизацию времени на сервере (chrony или systemd-timesyncd).")
        if code in ("SignatureDoesNotMatch", "InvalidAccessKeyId", "AccessDenied", "InvalidToken") \
                or status == 403:
            return ReplicaError(f"Хранилище S3 отказало в доступе ({what}): {detail}.",
                                hint="Проверьте идентификатор ключа и секретный ключ, а также права "
                                     "сервисного аккаунта на бакет (чтение, запись, удаление).")
        if code == "NoSuchBucket":
            return ReplicaError(f"Бакет «{self.bucket}» не найден ({detail}).",
                                hint="Проверьте имя бакета, адрес хранилища и регион.")
        if code in ("AuthorizationHeaderMalformed", "PermanentRedirect", "IllegalLocationConstraintException") \
                or status == 301:
            return ReplicaError(f"Хранилище S3 не принимает запрос ({detail}).",
                                hint="Вероятно, указан не тот регион или адрес хранилища. Для Yandex Object "
                                     "Storage регион ru-central1, адрес https://storage.yandexcloud.net.")
        return ReplicaError(f"Ошибка хранилища S3 ({what}): {detail}.", retryable=status >= 500)

    # -- операции ------------------------------------------------------------------
    def head_bucket(self) -> None:
        """Проверить, что бакет есть и доступен (у HEAD нет тела — разбираем по коду)."""
        status, _h, _b = self._call("HEAD", bucket_level=True, what="проверка бакета",
                                    ok=(200, 301, 400, 403, 404))
        if status == 200:
            return
        if status == 404:
            raise self._error(404, "NoSuchBucket", "", "проверка бакета")
        if status == 403:
            raise self._error(403, "AccessDenied", "", "проверка бакета")
        raise self._error(status, "PermanentRedirect" if status == 301 else "AuthorizationHeaderMalformed",
                          "", "проверка бакета")

    def put_bytes(self, key: str, data: bytes) -> None:
        extra = {"content-md5": _md5_b64(data)}
        if self.cfg.storage_class:
            extra["x-amz-storage-class"] = self.cfg.storage_class
        self._call("PUT", key, body=data, extra_signed=extra, what=f"загрузка {key}")

    def put_file(self, key: str, path: str) -> int:
        """Загрузить файл; крупный — по частям. Возвращает размер."""
        size = os.path.getsize(path)
        if size <= PART_SIZE:
            with open(path, "rb") as fh:
                data = fh.read()
            if len(data) != size:
                raise ReplicaError(f"Файл изменился во время загрузки: {path}", retryable=True)
            self.put_bytes(key, data)
            return size
        self._multipart(key, path, size)
        return size

    def _multipart(self, key: str, path: str, size: int) -> None:
        extra = {}
        if self.cfg.storage_class:
            extra["x-amz-storage-class"] = self.cfg.storage_class
        _s, _h, body = self._call("POST", key, query={"uploads": ""}, extra_signed=extra,
                                  what=f"начало загрузки {key}", ok=(200,))
        try:
            upload_id = _child_text(ET.fromstring(body), "UploadId")
        except ET.ParseError:
            upload_id = ""
        if not upload_id:
            raise ReplicaError(f"Хранилище S3 не начало многочастную загрузку {key}.")
        parts: List[Tuple[int, str]] = []
        try:
            with open(path, "rb") as fh:
                number = 0
                while True:
                    chunk = fh.read(PART_SIZE)
                    if not chunk:
                        break
                    number += 1
                    _s, headers, _b = self._call(
                        "PUT", key, query={"partNumber": str(number), "uploadId": upload_id}, body=chunk,
                        extra_signed={"content-md5": _md5_b64(chunk)}, what=f"часть {number} файла {key}")
                    parts.append((number, headers.get("etag", "")))
            xml = ["<CompleteMultipartUpload>"]
            for number, etag in parts:
                xml.append(f"<Part><PartNumber>{number}</PartNumber><ETag>{xml_escape(etag)}</ETag></Part>")
            xml.append("</CompleteMultipartUpload>")
            payload = "".join(xml).encode("utf-8")
            _s, _h, done = self._call("POST", key, query={"uploadId": upload_id}, body=payload,
                                      what=f"завершение загрузки {key}", ok=(200,))
            # Ошибка может прийти с кодом 200 — внутри тела ответа.
            if b"<Error>" in done:
                code, message = _parse_error(done)
                raise ReplicaError(f"Хранилище S3 не собрало файл {key}: {code} {message}".strip(),
                                   retryable=True)
        except BaseException:
            try:
                self._call("DELETE", key, query={"uploadId": upload_id}, what="отмена загрузки",
                           ok=(200, 204, 404))
            except Exception:  # noqa: BLE001
                pass
            raise

    def head(self, key: str) -> Optional[int]:
        """Размер объекта или None, если его нет."""
        status, headers, _b = self._call("HEAD", key, what=f"проверка {key}", ok=(200, 404))
        if status == 404:
            return None
        try:
            return int(headers.get("content-length", "0"))
        except ValueError:
            return 0

    def get_bytes(self, key: str) -> Optional[bytes]:
        status, _h, data = self._call("GET", key, what=f"чтение {key}", ok=(200, 404))
        return None if status == 404 else data

    def get_to_file(self, key: str, dest: str) -> int:
        """Скачать объект в файл потоком (через .part и атомарную замену)."""
        path = self._path(key)
        tmp = dest + ".part"
        last_exc: Optional[BaseException] = None
        for attempt in range(RETRIES + 1):
            try:
                resp = self._send("GET", path, None, b"")
                if resp.status != 200:
                    data = resp.read()
                    code, message = _parse_error(data)
                    if resp.status in _RETRY_STATUSES and attempt < RETRIES:
                        time.sleep(2 ** attempt)
                        continue
                    raise self._error(resp.status, code, message, f"скачивание {key}")
                expected = resp.getheader("Content-Length")
                got = 0
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with open(tmp, "wb") as fh:
                    while True:
                        block = resp.read(_READ_CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        got += len(block)
                if expected is not None and int(expected) != got:
                    raise ReplicaError(f"Файл {key} скачан не полностью ({got} из {expected} байт).",
                                       retryable=True)
                os.replace(tmp, dest)
                if resp.will_close:
                    self._drop_conn()
                return got
            except ReplicaError as exc:
                last_exc = exc
                self._drop_conn()
                if not exc.retryable or attempt >= RETRIES:
                    raise
                time.sleep(2 ** attempt)
            except (OSError, http.client.HTTPException) as exc:
                last_exc = exc
                self._drop_conn()
                if attempt >= RETRIES:
                    raise ReplicaError(f"Не удалось скачать {key}: {exc}", retryable=True) from exc
                time.sleep(2 ** attempt)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        raise ReplicaError(f"Не удалось скачать {key}: {last_exc}", retryable=True)  # pragma: no cover

    def delete(self, key: str) -> None:
        self._call("DELETE", key, what=f"удаление {key}", ok=(200, 204, 404))

    def delete_many(self, keys: List[str]) -> List[str]:
        """Удалить ключи пачками. Возвращает ключи, которые удалить не удалось."""
        failed: List[str] = []
        for start in range(0, len(keys), DELETE_BATCH):
            batch = keys[start:start + DELETE_BATCH]
            xml = ['<?xml version="1.0" encoding="UTF-8"?>'
                   '<Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Quiet>true</Quiet>']
            for key in batch:
                xml.append(f"<Object><Key>{xml_escape(key)}</Key></Object>")
            xml.append("</Delete>")
            payload = "".join(xml).encode("utf-8")
            try:
                _s, _h, body = self._call("POST", query={"delete": ""}, body=payload, bucket_level=True,
                                          extra_signed={"content-md5": _md5_b64(payload)},
                                          what="пакетное удаление", ok=(200,))
            except ReplicaError as exc:
                if "NotImplemented" not in exc.message and "MethodNotAllowed" not in exc.message:
                    raise
                # Хранилище без пакетного удаления — удаляем по одному.
                for key in batch:
                    try:
                        self.delete(key)
                    except ReplicaError:
                        failed.append(key)
                continue
            try:
                root = ET.fromstring(body) if body else None
            except ET.ParseError:
                root = None
            if root is not None:
                for child in root:
                    if _local(child.tag) == "Error":
                        failed.append(_child_text(child, "Key"))
        return failed

    def list_prefixes(self, prefix: str) -> List[str]:
        """«Подкаталоги» первого уровня под префиксом (CommonPrefixes с разделителем «/»)."""
        out: List[str] = []
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000", "prefix": prefix, "delimiter": "/"}
            if token:
                query["continuation-token"] = token
            _s, _h, body = self._call("GET", query=query, bucket_level=True, what="список каталогов",
                                      ok=(200,))
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise ReplicaError("Хранилище S3 вернуло непонятный ответ на запрос списка.") from exc
            for child in root:
                if _local(child.tag) == "CommonPrefixes":
                    value = _child_text(child, "Prefix")
                    if value:
                        out.append(value)
            truncated = _child_text(root, "IsTruncated").lower() == "true"
            token = _child_text(root, "NextContinuationToken")
            if not truncated or not token:
                return out

    def list(self, prefix: str = "") -> Iterator[Tuple[str, int]]:
        """Все объекты с префиксом: (ключ, размер)."""
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            _s, _h, body = self._call("GET", query=query, bucket_level=True, what="список объектов",
                                      ok=(200,))
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise ReplicaError("Хранилище S3 вернуло непонятный ответ на запрос списка объектов.") from exc
            for child in root:
                if _local(child.tag) != "Contents":
                    continue
                key = _child_text(child, "Key")
                try:
                    size = int(_child_text(child, "Size", "0") or 0)
                except ValueError:
                    size = 0
                if key:
                    yield key, size
            truncated = _child_text(root, "IsTruncated").lower() == "true"
            token = _child_text(root, "NextContinuationToken")
            if not truncated or not token:
                return
