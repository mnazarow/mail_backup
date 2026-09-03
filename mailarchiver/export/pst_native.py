"""
Встроенный (native) ЭКСПЕРИМЕНТАЛЬНЫЙ генератор .pst без внешних зависимостей.

⚠️  ВАЖНО. Корректных open-source генераторов .pst под Linux не существует, а
    формат MS-PST закрыто-сложный (документирован как [MS-PST], но реализация
    писателя огромна и хрупка). Этот модуль реализует подмножество формата
    ANSI PST, достаточное, чтобы файл открывался утилитами семейства libpst
    (readpst) и, как правило, Microsoft Outlook. Он покрывает: иерархию папок и
    письма с основными свойствами (тема, отправитель, получатель, дата, текст,
    заголовки). Вложения и сложные MIME-структуры в native-режиме
    упрощаются — для полной точности используйте движок Aspose либо экспорт
    eml/mbox, которые Outlook импортирует без потерь.

    Формат по умолчанию — ANSI (лимит 2 ГБ). Для больших ящиков берите Aspose
    (Unicode) или разбивайте экспорт по папкам/датам.

Реализованы слои формата: заголовок PST, NDB (BTree узлов NBT и блоков BBT,
блоки с трейлерами), LTP (куча HN, BTH, контекст свойств PC). Дерево папок
libpst строит по полю nidParent записей NBT.
"""
from __future__ import annotations

import email
import email.utils
import struct
import time
from email.header import decode_header, make_header
from typing import Dict, Iterable, List, Optional, Tuple

from ..errors import PstEngineError
from ..logging_setup import get_logger
from .base import CancelCB, ExportEngine, ExportResult, MailItem, ProgressCB, folder_to_fs

log = get_logger("export.native")

# --- Константы формата ------------------------------------------------------
PST_MAGIC = 0x4E444221          # "!BDN"
MAGIC_CLIENT = 0x4D53           # "SM"
VER_ANSI = 14                   # версия формата (ANSI)
VER_CLIENT = 19

# Типы страниц (ptype)
PTYPE_BBT = 0x80
PTYPE_NBT = 0x81

# Типы свойств MAPI
PT_INT32 = 0x0003
PT_BOOL = 0x000B
PT_TIME = 0x0040
PT_STRING = 0x001F     # UTF-16LE
PT_BINARY = 0x0102

# Идентификаторы свойств (PidTag*)
TAG_DISPLAY_NAME = 0x3001
TAG_CONTENT_COUNT = 0x3602
TAG_CONTENT_UNREAD = 0x3603
TAG_SUBFOLDERS = 0x360A
TAG_MESSAGE_CLASS = 0x001A
TAG_SUBJECT = 0x0037
TAG_BODY = 0x1000
TAG_SENDER_NAME = 0x0C1A
TAG_SENDER_EMAIL = 0x0C1F
TAG_SENT_REP_NAME = 0x0042
TAG_DISPLAY_TO = 0x0E04
TAG_DISPLAY_CC = 0x0E03
TAG_DELIVERY_TIME = 0x0E06
TAG_SUBMIT_TIME = 0x0039
TAG_TRANSPORT_HEADERS = 0x007D
TAG_MESSAGE_FLAGS = 0x0E07

# Специальные NID
NID_MESSAGE_STORE = 0x21
NID_ROOT_FOLDER = 0x122
NID_TYPE_NORMAL_FOLDER = 0x02
NID_TYPE_NORMAL_MESSAGE = 0x04

# Windows FILETIME epoch difference (секунды между 1601 и 1970)
_FILETIME_EPOCH_DIFF = 11644473600

# Максимальный размер данных PC в ОДНОМ блоке. ANSI PST: блок не больше 8192 байт
# (включая трейлер). native-движок не поддерживает деревья данных (XBLOCK), поэтому
# свойства одного письма должны помещаться в один блок; слишком длинные тело и
# заголовки автоматически обрезаются под этот бюджет (см. _message_props).
MAX_PC_BLOCK = 8000


def _sig(ib: int, bid: int) -> int:
    """Блочная/страничная сигнатура wSig (MS-PST 5.5)."""
    x = (ib ^ bid) & 0xFFFFFFFF
    return ((x >> 16) ^ x) & 0xFFFF


def _to_filetime(epoch: Optional[float]) -> int:
    if not epoch:
        epoch = time.time()
    return int((epoch + _FILETIME_EPOCH_DIFF) * 10_000_000)


def _decode_hdr(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return value


class _Heap:
    """Строитель кучи HN в пределах ОДНОГО блока (для PC небольшого размера)."""

    def __init__(self, client_sig: int) -> None:
        self.client_sig = client_sig
        self.allocs: List[bytes] = []      # индексы с 1
        self.user_root_hid = 0

    def alloc(self, data: bytes) -> int:
        self.allocs.append(data)
        index = len(self.allocs)           # 1-based
        # HID: type=0 (5 бит), hidIndex (11 бит) с 1, hidBlockIndex (16 бит)=0
        return (index << 5) & 0xFFFFFFFF

    def build(self) -> bytes:
        # Раскладка: HNHDR (12), затем аллокации подряд, затем HNPAGEMAP.
        header_size = 12
        body = bytearray()
        offsets = [header_size]            # rgibAlloc[0] = начало первой аллокации
        for a in self.allocs:
            body += a
            offsets.append(header_size + len(body))
        ib_hnpm = header_size + len(body)
        # Куча HN должна помещаться в один блок (native-движок не поддерживает
        # деревья данных XBLOCK). Смещения хранятся как 16-бит, поэтому > 64 КБ
        # невозможно физически — в этом случае возбуждаем управляемую ошибку.
        if ib_hnpm > 0xFFF0 or any(o > 0xFFFF for o in offsets):
            raise PstEngineError("native PST: узел не помещается в один блок (слишком длинное письмо).",
                                 code="pst_engine_error")
        # HNPAGEMAP
        c_alloc = len(self.allocs)
        pagemap = bytearray()
        pagemap += struct.pack("<HH", c_alloc, 0)   # cAlloc, cFree
        for off in offsets:
            pagemap += struct.pack("<H", off)
        # HNHDR
        hdr = struct.pack("<HBBI I", ib_hnpm, 0xEC, self.client_sig, self.user_root_hid, 0)
        # struct above: H(ibHnpm) B(bSig=0xEC) B(bClientSig) I(hidUserRoot) I(rgbFillLevel)
        return bytes(hdr) + bytes(body) + bytes(pagemap)


def build_pc_block(props: List[Tuple[int, int, object]]) -> bytes:
    """
    Собрать блок данных PC (Property Context) в одной куче HN.
    props: список (propId, propType, value). value:
        PT_INT32/PT_BOOL  -> int (хранится инлайн)
        PT_TIME           -> int epoch (хранится как 8 байт через HID)
        PT_STRING         -> str  (UTF-16LE через HID)
        PT_BINARY         -> bytes (через HID)
    """
    heap = _Heap(client_sig=0xBC)  # bTypePC
    # Сначала сформируем записи BTH (по возрастанию propId)
    records: List[bytes] = []
    for prop_id, prop_type, value in sorted(props, key=lambda p: p[0]):
        if prop_type in (PT_INT32, PT_BOOL):
            hnid = int(value) & 0xFFFFFFFF
        elif prop_type == PT_TIME:
            hnid = heap.alloc(struct.pack("<Q", _to_filetime(value)))
        elif prop_type == PT_STRING:
            data = (value or "").encode("utf-16-le")
            hnid = heap.alloc(data)
        elif prop_type == PT_BINARY:
            hnid = heap.alloc(bytes(value or b""))
        else:
            raise PstEngineError(f"native PST: неподдерживаемый тип свойства {prop_type:#06x}")
        records.append(struct.pack("<HHI", prop_id, prop_type, hnid))
    # BTH-лист: массив записей (8 байт каждая), затем BTHHEADER, ссылающийся на него
    leaf = b"".join(records)
    leaf_hid = heap.alloc(leaf)
    bth_header = struct.pack("<BBBBI", 0xB5, 2, 6, 0, leaf_hid)  # bType,cbKey,cbEnt,idxLevels,hidRoot
    bth_hid = heap.alloc(bth_header)
    heap.user_root_hid = bth_hid
    data = heap.build()
    if len(data) > MAX_PC_BLOCK:
        raise PstEngineError("native PST: свойства письма не помещаются в один блок.",
                             code="pst_engine_error")
    return data


class _Node:
    __slots__ = ("nid", "parent_nid", "data", "bid")

    def __init__(self, nid: int, parent_nid: int, data: bytes) -> None:
        self.nid = nid
        self.parent_nid = parent_nid
        self.data = data
        self.bid = 0


class PstWriter:
    """Сборщик ANSI PST-файла из набора узлов (папки и письма)."""

    def __init__(self) -> None:
        self.nodes: List[_Node] = []
        self._next_bid = 4
        self._next_folder_index = 0x20
        self._next_msg_index = 0x2000

    def _alloc_bid(self) -> int:
        bid = self._next_bid
        self._next_bid += 4
        return bid

    def new_folder_nid(self) -> int:
        idx = self._next_folder_index
        self._next_folder_index += 1
        return ((idx << 5) | NID_TYPE_NORMAL_FOLDER) & 0xFFFFFFFF

    def new_message_nid(self) -> int:
        idx = self._next_msg_index
        self._next_msg_index += 1
        return ((idx << 5) | NID_TYPE_NORMAL_MESSAGE) & 0xFFFFFFFF

    def add_node(self, nid: int, parent_nid: int, data: bytes) -> None:
        self.nodes.append(_Node(nid, parent_nid, data))

    # -- сериализация --------------------------------------------------------
    def _block_bytes(self, data: bytes, bid: int, ib: int) -> bytes:
        cb = len(data)
        total = cb + 12  # ANSI BLOCKTRAILER = 12
        padded = (total + 63) & ~63
        buf = bytearray(padded)
        buf[0:cb] = data
        # ANSI BLOCKTRAILER: cb(2), wSig(2), bid(4), dwCRC(4)  — bid ДО dwCRC!
        trailer = struct.pack("<HHII", cb, _sig(ib, bid), bid, 0)
        buf[padded - 12:padded] = trailer
        return bytes(buf)

    # libpst (ANSI) читает служебные поля страницы по фиксированным смещениям:
    #   0x1F0 cEnt, 0x1F1 cEntMax, 0x1F2 cbEnt, 0x1F3 cLevel (0 = лист),
    #   0x1F4 ptype, 0x1F5 ptypeRepeat, 0x1F6 wSig, 0x1F8 backlink(bid), 0x1FC dwCRC.
    _PAGE_ENTRY_REGION = 0x1F0  # 496 байт под записи

    def _btpage_bytes(self, entries_bytes: bytes, count: int, ent_size: int, level: int,
                      ptype: int, bid: int, ib: int) -> bytes:
        buf = bytearray(512)
        buf[0:len(entries_bytes)] = entries_bytes
        buf[0x1F0] = count & 0xFF
        buf[0x1F1] = (self._PAGE_ENTRY_REGION // ent_size) & 0xFF
        buf[0x1F2] = ent_size & 0xFF
        buf[0x1F3] = level & 0xFF
        struct.pack_into("<BBH", buf, 0x1F4, ptype, ptype, _sig(ib, bid))
        struct.pack_into("<I", buf, 0x1F8, bid)   # backlink
        struct.pack_into("<I", buf, 0x1FC, 0)     # dwCRC (readpst не проверяет)
        return bytes(buf)

    def _align_page(self) -> None:
        rem = len(self._buf) % 512
        if rem:
            self._buf += b"\x00" * (512 - rem)

    def _emit_page(self, entries_bytes: bytes, count: int, ent_size: int, level: int, ptype: int):
        self._align_page()
        bid = self._alloc_bid()
        ib = len(self._buf)
        self._buf += self._btpage_bytes(entries_bytes, count, ent_size, level, ptype, bid, ib)
        return bid, ib

    def _build_btree(self, entry_list: List[bytes], keys: List[int], ent_size: int, ptype: int):
        """Собрать многоуровневый BTree страниц из отсортированных листовых записей."""
        max_leaf = self._PAGE_ENTRY_REGION // ent_size
        max_branch = self._PAGE_ENTRY_REGION // 12
        if not entry_list:
            return self._emit_page(b"", 0, ent_size, 0, ptype)
        pages: List[tuple] = []  # (start_key, bid, ib)
        for start in range(0, len(entry_list), max_leaf):
            chunk = entry_list[start:start + max_leaf]
            chunk_keys = keys[start:start + max_leaf]
            bid, ib = self._emit_page(b"".join(chunk), len(chunk), ent_size, 0, ptype)
            pages.append((chunk_keys[0], bid, ib))
        level = 1
        while len(pages) > 1:
            new_pages: List[tuple] = []
            for start in range(0, len(pages), max_branch):
                chunk = pages[start:start + max_branch]
                # BTENTRY (32-bit): start_key(4), backpointer=child_bid(4), offset=child_ib(4)
                blob = b"".join(struct.pack("<III", k, cbid, cib) for (k, cbid, cib) in chunk)
                bid, ib = self._emit_page(blob, len(chunk), 12, level, ptype)
                new_pages.append((chunk[0][0], bid, ib))
            pages = new_pages
            level += 1
        return pages[0][1], pages[0][2]

    def write(self) -> bytes:
        self._buf = bytearray(512)  # заголовок (заполним в конце)
        ib = 512
        bbt_leaves: List[tuple] = []  # (bid, entry_bytes)
        nbt_leaves: List[tuple] = []  # (nid, entry_bytes)

        for node in self.nodes:
            node.bid = self._alloc_bid()
            block = self._block_bytes(node.data, node.bid, ib)
            self._buf += block
            # BBTENTRY (ANSI): bid(4), ib(4), cb(2), cRef(2)
            bbt_leaves.append((node.bid, struct.pack("<IIHH", node.bid, ib, len(node.data), 2)))
            # NBTENTRY (ANSI): nid(4), bidData(4), bidSub(4), nidParent(4)
            nbt_leaves.append((node.nid, struct.pack("<IIII", node.nid, node.bid, 0, node.parent_nid)))
            ib += len(block)

        bbt_leaves.sort(key=lambda x: x[0])
        nbt_leaves.sort(key=lambda x: x[0])
        bbt_bid, bbt_ib = self._build_btree([e[1] for e in bbt_leaves], [e[0] for e in bbt_leaves], 12, PTYPE_BBT)
        nbt_bid, nbt_ib = self._build_btree([e[1] for e in nbt_leaves], [e[0] for e in nbt_leaves], 16, PTYPE_NBT)

        file_eof = len(self._buf)
        header = self._build_header(nbt_bid, nbt_ib, bbt_bid, bbt_ib, file_eof)
        self._buf[0:len(header)] = header
        return bytes(self._buf)

    def _build_header(self, nbt_bid: int, nbt_ib: int, bbt_bid: int, bbt_ib: int, file_eof: int) -> bytes:
        h = bytearray(512)
        struct.pack_into("<I", h, 0x00, PST_MAGIC)
        struct.pack_into("<I", h, 0x04, 0)              # dwCRCPartial (не проверяется readpst)
        struct.pack_into("<H", h, 0x08, MAGIC_CLIENT)
        struct.pack_into("<H", h, 0x0A, VER_ANSI)
        struct.pack_into("<H", h, 0x0C, VER_CLIENT)
        struct.pack_into("<B", h, 0x0E, 0x01)
        struct.pack_into("<B", h, 0x0F, 0x01)
        struct.pack_into("<I", h, 0x10, 0)              # dwReserved1
        struct.pack_into("<I", h, 0x14, 0)              # dwReserved2
        struct.pack_into("<I", h, 0x18, self._next_bid + 8)   # bidNextB
        struct.pack_into("<I", h, 0x1C, self._next_bid + 16)  # bidNextP
        struct.pack_into("<I", h, 0x20, 1)              # dwUnique
        # rgnid[32] с 0x24 (128 байт) — оставим нулями
        root_off = 0x24 + 128                            # 0xA4
        # ROOT (ANSI, 40 байт): dwReserved, ibFileEof, ibAMapLast, cbAMapFree,
        #   cbPMapFree, BREF_NBT{bid,ib}, BREF_BBT{bid,ib}, fAMapValid, bReserved, wReserved
        struct.pack_into("<I", h, root_off + 0, 0)
        struct.pack_into("<I", h, root_off + 4, file_eof)
        struct.pack_into("<I", h, root_off + 8, 0)
        struct.pack_into("<I", h, root_off + 12, 0)
        struct.pack_into("<I", h, root_off + 16, 0)
        struct.pack_into("<II", h, root_off + 20, nbt_bid, nbt_ib)   # BREFNBT
        struct.pack_into("<II", h, root_off + 28, bbt_bid, bbt_ib)   # BREFBBT
        struct.pack_into("<B", h, root_off + 36, 0)     # fAMapValid = INVALID (0)
        struct.pack_into("<B", h, root_off + 37, 0)
        struct.pack_into("<H", h, root_off + 38, 0)
        # bCryptMethod = 0 (NDB_CRYPT_NONE) — найдём поле; для ANSI оно после rgbFP.
        # Для совместимости с readpst достаточно валидных BREF в ROOT и магии.
        return bytes(h)


# ---------------------------------------------------------------------------
#  Высокоуровневый движок экспорта
# ---------------------------------------------------------------------------
class NativePstExportEngine(ExportEngine):
    name = "native"
    fmt = "pst"
    experimental = True

    @classmethod
    def available(cls):
        return True, ""

    def _folder_props(self, name: str, count: int) -> bytes:
        return build_pc_block([
            (TAG_DISPLAY_NAME, PT_STRING, name),
            (TAG_CONTENT_COUNT, PT_INT32, count),
            (TAG_CONTENT_UNREAD, PT_INT32, 0),
            (TAG_SUBFOLDERS, PT_BOOL, 0),
        ])

    def _message_props(self, item: MailItem) -> bytes:
        try:
            msg = email.message_from_bytes(item.raw)
        except Exception:  # noqa: BLE001
            msg = None
        subject = _decode_hdr(msg.get("Subject", "")) if msg else ""
        from_hdr = _decode_hdr(msg.get("From", "")) if msg else ""
        to_hdr = _decode_hdr(msg.get("To", "")) if msg else ""
        cc_hdr = _decode_hdr(msg.get("Cc", "")) if msg else ""
        sender_name, sender_email = email.utils.parseaddr(from_hdr)
        body = self._extract_body(msg) if msg else ""
        headers_blob = item.raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
        try:
            headers_text = headers_blob.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            headers_text = ""

        def make(body_txt: str, headers_txt: str):
            return [
                (TAG_MESSAGE_CLASS, PT_STRING, "IPM.Note"),
                (TAG_SUBJECT, PT_STRING, (subject or "(без темы)")[:400]),
                (TAG_BODY, PT_STRING, body_txt),
                (TAG_SENDER_NAME, PT_STRING, (sender_name or sender_email or "")[:255]),
                (TAG_SENDER_EMAIL, PT_STRING, (sender_email or "")[:255]),
                (TAG_SENT_REP_NAME, PT_STRING, (sender_name or sender_email or "")[:255]),
                (TAG_DISPLAY_TO, PT_STRING, to_hdr[:500]),
                (TAG_DISPLAY_CC, PT_STRING, cc_hdr[:500]),
                (TAG_TRANSPORT_HEADERS, PT_STRING, headers_txt),
                (TAG_DELIVERY_TIME, PT_TIME, item.internaldate),
                (TAG_SUBMIT_TIME, PT_TIME, item.internaldate),
                (TAG_MESSAGE_FLAGS, PT_INT32, 1),  # MSGFLAG_READ
            ]

        # Тело и заголовки в UTF-16LE занимают вдвое больше байт, а весь узел
        # должен поместиться в один блок. Подбираем длины так, чтобы блок влез в
        # бюджет: сперва укорачиваем тело, затем заголовки (native — упрощённый
        # экспорт; для полной точности используйте Aspose или экспорт eml).
        body = body[:20000]
        headers_text = headers_text[:10000]
        cut = ""
        for _ in range(40):
            try:
                return build_pc_block(make(body + cut, headers_text))
            except PstEngineError:
                if len(body) > 300:
                    body = body[: max(300, len(body) // 2)]
                    cut = "\n\n[…текст письма обрезан для native-PST; полная копия — в экспорте eml…]"
                elif len(headers_text) > 200:
                    headers_text = headers_text[: len(headers_text) // 2]
                else:
                    # крайний случай — минимальный набор свойств
                    return build_pc_block([
                        (TAG_MESSAGE_CLASS, PT_STRING, "IPM.Note"),
                        (TAG_SUBJECT, PT_STRING, (subject or "(без темы)")[:200]),
                        (TAG_BODY, PT_STRING, (body[:200] + " […обрезано…]")),
                        (TAG_DELIVERY_TIME, PT_TIME, item.internaldate),
                        (TAG_MESSAGE_FLAGS, PT_INT32, 1),
                    ])
        # теоретически недостижимо
        return build_pc_block(make(body[:300], ""))

    @staticmethod
    def _extract_body(msg) -> str:
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                        payload = part.get_payload(decode=True) or b""
                        charset = part.get_content_charset() or "utf-8"
                        return payload.decode(charset, "replace")
                return ""
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, "replace")
        except Exception:  # noqa: BLE001
            return ""

    def export(self, items: Iterable[MailItem], out_path: str, *,
               options: Optional[dict] = None, progress_cb: Optional[ProgressCB] = None,
               cancel_cb: Optional[CancelCB] = None, total_hint: int = 0) -> ExportResult:
        result = ExportResult(path=out_path, is_dir=False, engine=self.name, fmt="pst",
                              warning="Файл .pst создан встроенным ЭКСПЕРИМЕНТАЛЬНЫМ движком. "
                                      "Обязательно проверьте открытие в вашей версии Outlook. "
                                      "Для гарантированного результата используйте движок Aspose или экспорт eml/mbox.")
        writer = PstWriter()
        # Узел «хранилище сообщений»
        writer.add_node(NID_MESSAGE_STORE, 0, self._store_props())
        # Корневая папка
        writer.add_node(NID_ROOT_FOLDER, NID_MESSAGE_STORE, self._folder_props("Верхний уровень PST", 0))

        folder_nids: Dict[str, int] = {"": NID_ROOT_FOLDER}
        materialized: List[MailItem] = list(items)
        # создать узлы папок
        for folder in sorted({folder_to_fs(i.folder).replace("\\", "/") for i in materialized}):
            parts = folder.split("/")
            accum = ""
            parent = NID_ROOT_FOLDER
            for part in parts:
                accum = f"{accum}/{part}" if accum else part
                if accum not in folder_nids:
                    nid = writer.new_folder_nid()
                    writer.add_node(nid, parent, self._folder_props(part, 0))
                    folder_nids[accum] = nid
                parent = folder_nids[accum]

        # создать узлы писем
        for item in materialized:
            if cancel_cb and cancel_cb():
                result.cancelled = True
                break
            key = folder_to_fs(item.folder).replace("\\", "/")
            parent = folder_nids.get(key, NID_ROOT_FOLDER)
            nid = writer.new_message_nid()
            try:
                writer.add_node(nid, parent, self._message_props(item))
                result.count += 1
                result.bytes_written += len(item.raw)
            except PstEngineError as exc:
                result.errors += 1
                result.error_details.append(str(exc))
            if progress_cb and result.count % 20 == 0:
                progress_cb(result.count, total_hint, f"PST(native): {result.count}")

        try:
            data = writer.write()
        except PstEngineError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PstEngineError(f"Ошибка сборки PST (native): {exc}", cause=exc) from exc

        with open(out_path, "wb") as fh:
            fh.write(data)
        result.bytes_written = len(data)
        if progress_cb:
            progress_cb(result.count, total_hint or result.count, "PST(native): готово")
        return result

    def _store_props(self) -> bytes:
        # PidTagIpmSubTreeEntryId (0x35E0): EntryID корневой папки (24 байта):
        #   flags(4)=0, GUID хранилища(16), nid(4)=NID_ROOT_FOLDER.
        subtree_entryid = b"\x00\x00\x00\x00" + b"\x00" * 16 + struct.pack("<I", NID_ROOT_FOLDER)
        return build_pc_block([
            (TAG_DISPLAY_NAME, PT_STRING, "MailArchiver PST"),
            (0x0FF9, PT_BINARY, b"\x00" * 16),      # PidTagRecordKey (GUID хранилища)
            (0x35E0, PT_BINARY, subtree_entryid),   # PidTagIpmSubTreeEntryId -> корневая папка
        ])
