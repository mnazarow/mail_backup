"""Поддельное S3-хранилище для тестов копии вне сервера.

Проверяет подпись SigV4 каждого запроса (тем же алгоритмом, что и клиент,
но из «сырой» строки запроса — так ловятся ошибки кодирования путей с
кириллицей и спецсимволами), контрольные суммы тела и Content-MD5.
Поддерживает то, чем пользуется клиент: PUT/GET/HEAD/DELETE объекта,
многочастную загрузку, пакетное удаление и ListObjectsV2 с постраничной
выдачей и разделителем.
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from mailarchiver.replica.s3 import sign_request

BUCKET = "archive"
ACCESS = "AKTEST"
SECRET = "secret/test+key"
REGION = "ru-central1"


class FakeS3:
    def __init__(self) -> None:
        self.objects = {}
        self.uploads = {}
        self.page_size = 1000
        self.requests = []
        self.errors = []
        self.fail_next = 0          # сколько следующих запросов ответить 503
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # тишина в выводе тестов
                pass

            def _reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD" and body:
                    self.wfile.write(body)

            def _error(self, status, code, message=""):
                body = f"<Error><Code>{code}</Code><Message>{escape(message)}</Message></Error>".encode()
                fake.errors.append((status, code, message))
                self._reply(status, body, {"Content-Type": "application/xml"})

            def _check_auth(self, body: bytes) -> bool:
                auth = self.headers.get("Authorization", "")
                m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,"
                             r"SignedHeaders=([^,]+),Signature=([0-9a-f]{64})$", auth)
                if not m:
                    self._error(403, "AccessDenied", "bad auth header")
                    return False
                access, _date, region, signed, sig = m.groups()
                if access != ACCESS or region != REGION:
                    self._error(403, "InvalidAccessKeyId", "unknown key")
                    return False
                raw_path, _, raw_query = self.path.partition("?")
                query = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
                headers = {name: self.headers.get(name, "") for name in signed.split(";")}
                payload_hash = self.headers.get("x-amz-content-sha256", "")
                expected = sign_request(self.command, raw_path, query, headers, payload_hash,
                                        access_key=ACCESS, secret_key=SECRET, region=REGION,
                                        amz_date=self.headers.get("x-amz-date", ""))
                if not expected.endswith("Signature=" + sig):
                    self._error(403, "SignatureDoesNotMatch", raw_path)
                    return False
                if payload_hash != "UNSIGNED-PAYLOAD" and hashlib.sha256(body).hexdigest() != payload_hash:
                    self._error(400, "XAmzContentSHA256Mismatch")
                    return False
                md5 = self.headers.get("Content-MD5")
                if md5 and base64.b64encode(hashlib.md5(body).digest()).decode() != md5:
                    self._error(400, "BadDigest")
                    return False
                return True

            def _key(self):
                raw_path, _, raw_query = self.path.partition("?")
                prefix = f"/{BUCKET}"
                if not raw_path.startswith(prefix):
                    return None, None
                rest = raw_path[len(prefix):]
                key = urllib.parse.unquote(rest[1:]) if rest.startswith("/") else ""
                return key, dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append((self.command, self.path))
                if fake.fail_next > 0:
                    fake.fail_next -= 1
                    return self._error(503, "SlowDown", "try later")
                if not self._check_auth(body):
                    return
                key, q = self._key()
                if key is None:
                    return self._error(404, "NoSuchBucket")
                cmd = self.command
                if not key:
                    if cmd == "HEAD":
                        return self._reply(200)
                    if cmd == "GET" and q.get("list-type") == "2":
                        return self._list(q)
                    if cmd == "POST" and "delete" in q:
                        return self._delete_batch(body)
                    return self._error(400, "NotImplemented")
                if cmd == "PUT" and "uploadId" in q:
                    parts = fake.uploads.get(q["uploadId"])
                    if parts is None:
                        return self._error(404, "NoSuchUpload")
                    parts[int(q["partNumber"])] = body
                    etag = '"' + hashlib.md5(body).hexdigest() + '"'
                    return self._reply(200, headers={"ETag": etag})
                if cmd == "PUT":
                    fake.objects[key] = body
                    return self._reply(200, headers={"ETag": '"' + hashlib.md5(body).hexdigest() + '"'})
                if cmd == "POST" and "uploads" in q:
                    upload_id = f"u{len(fake.uploads) + 1}"
                    fake.uploads[upload_id] = {}
                    xml = (f"<InitiateMultipartUploadResult><Bucket>{BUCKET}</Bucket><Key>{escape(key)}</Key>"
                           f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>").encode()
                    return self._reply(200, xml)
                if cmd == "POST" and "uploadId" in q:
                    parts = fake.uploads.pop(q["uploadId"], None)
                    if parts is None:
                        return self._error(404, "NoSuchUpload")
                    numbers = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", body)]
                    fake.objects[key] = b"".join(parts[n] for n in numbers)
                    return self._reply(200, b"<CompleteMultipartUploadResult></CompleteMultipartUploadResult>")
                if cmd == "DELETE" and "uploadId" in q:
                    fake.uploads.pop(q["uploadId"], None)
                    return self._reply(204)
                if cmd in ("GET", "HEAD"):
                    if key not in fake.objects:
                        return self._error(404, "NoSuchKey")
                    data = fake.objects[key]
                    if cmd == "HEAD":
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        return None
                    return self._reply(200, data)
                if cmd == "DELETE":
                    fake.objects.pop(key, None)
                    return self._reply(204)
                return self._error(400, "NotImplemented")

            def _list(self, q):
                prefix = q.get("prefix", "")
                delimiter = q.get("delimiter", "")
                token = q.get("continuation-token", "")
                keys = sorted(k for k in fake.objects if k.startswith(prefix))
                items, prefixes = [], []
                for k in keys:
                    if delimiter:
                        rest = k[len(prefix):]
                        if delimiter in rest:
                            p = prefix + rest.split(delimiter, 1)[0] + delimiter
                            if p not in prefixes:
                                prefixes.append(p)
                            continue
                    items.append(k)
                entries = [("k", k) for k in items] + [("p", p) for p in prefixes]
                entries.sort(key=lambda e: e[1])
                start = int(token) if token else 0
                page = entries[start:start + fake.page_size]
                truncated = start + fake.page_size < len(entries)
                xml = ['<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
                for kind, value in page:
                    if kind == "k":
                        xml.append(f"<Contents><Key>{escape(value)}</Key><Size>{len(fake.objects[value])}</Size></Contents>")
                    else:
                        xml.append(f"<CommonPrefixes><Prefix>{escape(value)}</Prefix></CommonPrefixes>")
                xml.append(f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>")
                if truncated:
                    xml.append(f"<NextContinuationToken>{start + fake.page_size}</NextContinuationToken>")
                xml.append("</ListBucketResult>")
                return self._reply(200, "".join(xml).encode("utf-8"))

            def _delete_batch(self, body):
                text = body.decode("utf-8")
                from xml.sax.saxutils import unescape
                for raw in re.findall(r"<Key>(.*?)</Key>", text):
                    fake.objects.pop(unescape(raw), None)
                return self._reply(200, b"<DeleteResult></DeleteResult>")

            do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _handle

        return Handler
