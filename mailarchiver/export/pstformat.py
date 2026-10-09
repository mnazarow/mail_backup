"""
Запись файлов Outlook .pst (формат Unicode, [MS-PST]) без внешних программ.

Модуль знает только сам формат и ничего не знает о письмах IMAP: движок
экспорта (``pst_native``) разбирает письма и передаёт сюда готовые свойства.

Слои формата — снизу вверх:

* NDB (node database): блоки данных с трейлерами и CRC, деревья блоков
  (XBLOCK/XXBLOCK), деревья подузлов (SLBLOCK/SIBLOCK), B-деревья узлов (NBT)
  и блоков (BBT), карты размещения AMap/PMap/FMap, список плотности DList и
  заголовок файла;
* LTP (lists, tables, properties): куча на узле (HN), B-дерево в куче (BTH),
  контекст свойств (PC) и контекст таблицы (TC);
* обмен сообщениями: хранилище, корневая папка, обязательные служебные узлы
  и шаблоны таблиц, папки, письма, получатели и вложения.

Раскладка шаблонов таблиц и набор обязательных узлов повторяют файлы,
созданные самим Outlook; значения, которые спецификация объявляет
устаревшими (PMap, FMap), заполняются так, как это делает Outlook.

Файл пишется последовательно и потоково: блоки уходят на диск сразу, в памяти
остаются только индексы (номер блока → смещение) и строки таблиц текущей
папки. Вложения любого размера пишутся кусками по 8 КБ, целиком в память не
читаются.

Ограничение: одна часть — до ~1,9 ГБ (дальше формат требует страниц FPMap,
которые не создаются), поэтому крупный ящик делится на несколько файлов.
"""
from __future__ import annotations

import contextlib
import os
import re
import struct
import uuid
import zlib
from array import array
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
#  Константы формата
# ---------------------------------------------------------------------------
BLOCK_MAX_DATA = 8176            # данных в одном блоке (8192 минус трейлер)
BLOCK_TRAILER = 16
PAGE_SIZE = 512
PAGE_DATA = 496                  # полезная часть страницы (без трейлера)
AMAP_FIRST = 0x4400              # первая карта размещения
AMAP_SLOTS = PAGE_DATA * 8       # 3968 ячеек по 64 байта
AMAP_SPAN = AMAP_SLOTS * 64      # 0x3E000 байт на одну карту
DLIST_OFFSET = 0x4200
HEADER_SIZE = 564
FMAP_FIRST_INDEX = 128           # первые 128 карт описаны в заголовке (rgbFM)
FMAP_EVERY = PAGE_DATA           # одна страница FMap на 496 карт
#: Больше этого числа карт понадобились бы страницы FPMap (≈2,08 ГБ).
MAX_AMAPS = 8192
#: Жёсткий предел размера одной части: с запасом до границы FPMap.
HARD_LIMIT_BYTES = AMAP_FIRST + (MAX_AMAPS - 16) * AMAP_SPAN

XBLOCK_MAX = (BLOCK_MAX_DATA - 8) // 8        # 1021 ссылка в XBLOCK
SLBLOCK_MAX = (BLOCK_MAX_DATA - 8) // 24      # 340 подузлов в SLBLOCK
SIBLOCK_MAX = (BLOCK_MAX_DATA - 8) // 16      # 510 ссылок в SIBLOCK
HEAP_MAX_ALLOC = 3580                         # самый большой кусок кучи
HEAP_MAX_INDEX = 0x7FF

PTYPE_BBT = 0x80
PTYPE_NBT = 0x81
PTYPE_FMAP = 0x82
PTYPE_PMAP = 0x83
PTYPE_AMAP = 0x84
PTYPE_DLIST = 0x86

# Типы свойств MAPI
PT_I2 = 0x0002
PT_I4 = 0x0003
PT_BOOL = 0x000B
PT_I8 = 0x0014
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_BINARY = 0x0102

# Типы узлов (NID_TYPE)
NT_INTERNAL = 0x01
NT_NORMAL_FOLDER = 0x02
NT_SEARCH_FOLDER = 0x03
NT_NORMAL_MESSAGE = 0x04
NT_ATTACHMENT = 0x05
NT_SEARCH_UPDATE_QUEUE = 0x06
NT_SEARCH_CRITERIA = 0x07
NT_HIERARCHY_TABLE = 0x0D
NT_CONTENTS_TABLE = 0x0E
NT_ASSOC_CONTENTS_TABLE = 0x0F
NT_SEARCH_CONTENTS_TABLE = 0x10
NT_LTP = 0x1F

NID_MESSAGE_STORE = 0x21
NID_NAME_TO_ID_MAP = 0x61
NID_ROOT_FOLDER = 0x122
NID_SEARCH_MANAGEMENT_QUEUE = 0x1E1
NID_SEARCH_ACTIVITY_LIST = 0x201
NID_HIERARCHY_TEMPLATE = 0x60D
NID_CONTENTS_TEMPLATE = 0x60E
NID_ASSOC_TEMPLATE = 0x60F
NID_SEARCH_CONTENTS_TEMPLATE = 0x610
NID_RECEIVE_FOLDER_TABLE = 0x62B
NID_OUTGOING_QUEUE_TABLE = 0x64C
NID_ATTACHMENT_TABLE = 0x671
NID_RECIPIENT_TABLE = 0x692
NID_TEMPLATE_6B6 = 0x6B6
NID_TEMPLATE_6D7 = 0x6D7
NID_TEMPLATE_6F8 = 0x6F8
NID_SPAM_SEARCH_FOLDER = 0x2223
NID_SPAM_UPDATE_QUEUE = 0x2226
NID_SPAM_CRITERIA = 0x2227
NID_SPAM_CONTENTS = 0x2230
NID_IPM_SUBTREE = 0x8022        # «Top of Personal Folders» — видимый корень папок
NID_SEARCH_ROOT = 0x8042
NID_DELETED_ITEMS = 0x8062

#: Начальные значения счётчиков rgnid по типам узлов ([MS-PST] 2.2.2.6).
_RGNID_START = {NT_SEARCH_FOLDER: 0x4000, NT_NORMAL_MESSAGE: 0x10000, 0x08: 0x8000}

#: Провайдер «одноразовых» адресов SMTP (MS-OXCDATA, One-Off EntryID).
ONE_OFF_PROVIDER = bytes.fromhex("812B1FA4BEA310199D6E00DD010F5402")

FILETIME_EPOCH_DIFF = 11644473600


class PstFormatError(Exception):
    """Нарушение ограничений формата (слишком большой узел, файл и т.п.)."""


class PstFullError(PstFormatError):
    """Часть файла упёрлась в предел размера — данные надо писать в новую часть."""


# ---------------------------------------------------------------------------
#  Мелкие функции формата
# ---------------------------------------------------------------------------
def crc32(data) -> int:
    """CRC-32 из [MS-PST] 5.3 — обычный CRC-32 без начальной и конечной инверсии."""
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def compute_sig(ib: int, bid: int) -> int:
    """Подпись wSig блока или страницы ([MS-PST] 5.5)."""
    value = (ib ^ bid) & 0xFFFFFFFF
    return ((value >> 16) ^ value) & 0xFFFF


def make_nid(nid_type: int, index: int) -> int:
    return ((index << 5) | nid_type) & 0xFFFFFFFF


def to_filetime(epoch: Optional[float]) -> int:
    """Секунды UNIX → FILETIME (сотни наносекунд с 1601 года)."""
    if epoch is None:
        return 0
    value = int((float(epoch) + FILETIME_EPOCH_DIFF) * 10_000_000)
    return max(0, min(value, 0x7FFFFFFFFFFFFFFF))


def entry_id(record_key: bytes, nid: int) -> bytes:
    """EntryID объекта этого хранилища: флаги, UID хранилища, NID."""
    return b"\x00\x00\x00\x00" + record_key + struct.pack("<I", nid)


def one_off_entry_id(name: str, address: str) -> bytes:
    """«Одноразовый» EntryID адреса SMTP (как пишет Outlook: Unicode-строки)."""
    return (b"\x00\x00\x00\x00" + ONE_OFF_PROVIDER + b"\x00\x00" + b"\x01\x80"
            + (name or address).encode("utf-16-le") + b"\x00\x00"
            + "SMTP".encode("utf-16-le") + b"\x00\x00"
            + address.encode("utf-16-le") + b"\x00\x00")


def smtp_search_key(address: str) -> bytes:
    return b"SMTP:" + address.upper().encode("utf-8", "replace") + b"\x00"


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def _region(ib: int) -> int:
    return (ib - AMAP_FIRST) // AMAP_SPAN


def _reserved_pages(k: int) -> int:
    """Сколько страниц в начале области k занято картами (AMap, PMap, FMap)."""
    pages = 1
    if k % 8 == 0:
        pages += 1                                   # PMap — у каждой восьмой карты
        if k >= FMAP_FIRST_INDEX and (k - FMAP_FIRST_INDEX) % FMAP_EVERY == 0:
            pages += 1                               # FMap
    return pages


def _fill_level(free: int) -> int:
    """Уровень заполнения блока кучи по числу свободных байт ([MS-PST] 2.3.1.5)."""
    for level, bound in enumerate((3584, 2560, 2048, 1792, 1536, 1280, 1024, 768,
                                   512, 256, 128, 64, 32, 16, 8)):
        if free >= bound:
            return level
    return 0x0F


def _pack_fill(levels: Sequence[int], count: int) -> bytes:
    out = bytearray((count + 1) // 2)
    for i, level in enumerate(list(levels)[:count]):
        out[i // 2] |= (level & 0x0F) << (4 * (i % 2))
    return bytes(out)


# ---------------------------------------------------------------------------
#  NDB: блоки, страницы, B-деревья, карты, заголовок
# ---------------------------------------------------------------------------
class Checkpoint:
    __slots__ = ("pos", "gaps", "blocks")

    def __init__(self, pos: int, gaps: int, blocks: int) -> None:
        self.pos = pos
        self.gaps = gaps
        self.blocks = blocks


class NdbWriter:
    """Нижний слой: пишет блоки и страницы строго последовательно.

    Место выделяется подряд, поэтому карта размещения (AMap) в конце строится
    из единственного указателя конца данных и короткого списка «дыр»
    (выравнивание страниц, хвосты областей перед следующей картой).
    """

    _FLUSH = 4 * 1024 * 1024

    def __init__(self, path: str, *, max_bytes: int = HARD_LIMIT_BYTES) -> None:
        self.path = path
        self.max_bytes = min(int(max_bytes), HARD_LIMIT_BYTES)
        self.f = open(path, "w+b")
        self._buf = bytearray()
        self.pos = AMAP_FIRST + PAGE_SIZE * _reserved_pages(0)
        self._buf_start = self.pos             # смещение в файле первого байта буфера
        self._finishing = False                # при сборке индексов предел части не действует
        self.gaps: List[Tuple[int, int]] = []  # свободные промежутки [начало, конец)
        self.next_bid = 4                      # блоки: шаг 4, бит 0x2 — внутренний
        self.next_pid = 1                      # страницы
        self.bbt_bid = array("Q")
        self.bbt_ib = array("Q")
        self.bbt_cb = array("H")
        # узлы NBT: четыре массива вместо кортежа на узел (письмо — 28 байт, а не ~100)
        self._node_nid = array("I")
        self._node_data = array("Q")
        self._node_sub = array("Q")
        self._node_parent = array("I")
        self.closed = False

    # -- размещение -------------------------------------------------------
    @property
    def position(self) -> int:
        return self.pos

    def _place(self, size: int, alignment: int) -> int:
        pos = self.pos
        while True:
            start = _align(pos, alignment)
            k = _region(start)
            base = AMAP_FIRST + k * AMAP_SPAN
            data_start = base + PAGE_SIZE * _reserved_pages(k)
            if start < data_start:
                start = _align(data_start, alignment)
            if start + size <= base + AMAP_SPAN:
                break
            pos = base + AMAP_SPAN                     # не влезло — в следующую область
        limit = HARD_LIMIT_BYTES if self._finishing else self.max_bytes
        if start + size > limit:
            raise PstFullError("достигнут предел размера части .pst")
        if start > self.pos:
            # «дыра»: выравнивание или хвост области (карты следующей области
            # отмечаются занятыми отдельно — они не дыра)
            self._gap(self.pos, start)
        self.pos = start + size
        return start

    def _gap(self, begin: int, end: int) -> None:
        # Внутри дыры могут лежать страницы карт (при переходе в новую
        # область) — их из дыры исключаем.
        while begin < end:
            k = _region(begin)
            base = AMAP_FIRST + k * AMAP_SPAN
            reserved_end = base + PAGE_SIZE * _reserved_pages(k)
            if begin < reserved_end:
                begin = reserved_end
                continue
            stop = min(end, base + AMAP_SPAN)
            if stop > begin:
                self.gaps.append((begin, stop))
            begin = stop

    def _emit(self, ib: int, data) -> None:
        end_of_buf = self._buf_start + len(self._buf)
        if ib < end_of_buf:
            raise PstFormatError("внутренняя ошибка: запись назад по файлу")
        if ib > end_of_buf:
            self._buf += bytes(ib - end_of_buf)
        self._buf += data
        if len(self._buf) >= self._FLUSH:
            self._flush()

    def _flush(self) -> None:
        if self._buf:
            self.f.seek(self._buf_start)
            self.f.write(self._buf)
            self._buf_start += len(self._buf)
            self._buf = bytearray()

    @contextlib.contextmanager
    def relaxed(self):
        """Записать служебное (таблицы папок, индексы) сверх предела части.

        Предел части — мягкий: о нём заботится движок, заранее закрывая часть.
        Таблица, которую уже начали, должна дописаться целиком — иначе
        оборвалась бы вся выгрузка. Жёсткий предел формата действует всегда.
        """
        previous = self._finishing
        self._finishing = True
        try:
            yield
        finally:
            self._finishing = previous

    # -- откат недописанного письма ---------------------------------------
    def checkpoint(self) -> Checkpoint:
        return Checkpoint(self.pos, len(self.gaps), len(self.bbt_bid))

    def rollback(self, cp: Checkpoint) -> None:
        """Забыть всё, что записано после отметки (письмо разобрать не удалось)."""
        if cp.pos < self._buf_start:
            self._flush()
            self._buf_start = cp.pos
        else:
            del self._buf[cp.pos - self._buf_start:]
        self.pos = cp.pos
        del self.gaps[cp.gaps:]
        del self.bbt_bid[cp.blocks:]
        del self.bbt_ib[cp.blocks:]
        del self.bbt_cb[cp.blocks:]

    # -- блоки ------------------------------------------------------------
    def write_block(self, data, internal: bool = False) -> int:
        cb = len(data)
        if cb > BLOCK_MAX_DATA:
            raise PstFormatError(f"блок {cb} байт больше предела {BLOCK_MAX_DATA}")
        size = _align(cb + BLOCK_TRAILER, 64)
        bid = self.next_bid | (0x2 if internal else 0)
        self.next_bid += 4
        ib = self._place(size, 64)
        trailer = struct.pack("<HHIQ", cb, compute_sig(ib, bid), crc32(data), bid)
        self._emit(ib, bytes(data) + bytes(size - cb - BLOCK_TRAILER) + trailer)
        self.bbt_bid.append(bid)
        self.bbt_ib.append(ib)
        self.bbt_cb.append(cb)
        return bid

    def write_tree(self, bids: Sequence[int], sizes: Sequence[int]) -> int:
        """Дерево данных над готовыми блоками: один блок, XBLOCK или XXBLOCK."""
        if not bids:
            return 0
        if len(bids) == 1:
            return bids[0]
        level1: List[Tuple[int, int]] = []
        for start in range(0, len(bids), XBLOCK_MAX):
            part = bids[start:start + XBLOCK_MAX]
            total = sum(sizes[start:start + XBLOCK_MAX])
            payload = struct.pack(f"<BBHI{len(part)}Q", 0x01, 0x01, len(part), total, *part)
            level1.append((self.write_block(payload, internal=True), total))
        if len(level1) == 1:
            return level1[0][0]
        if len(level1) > XBLOCK_MAX:
            raise PstFormatError("значение слишком велико для одного узла .pst")
        total = sum(t for _b, t in level1)
        if total > 0xFFFFFFFF:
            raise PstFormatError("значение больше 4 ГБ не помещается в узел .pst")
        payload = struct.pack(f"<BBHI{len(level1)}Q", 0x01, 0x02, len(level1), total,
                              *[b for b, _t in level1])
        return self.write_block(payload, internal=True)

    def write_stream(self, data) -> int:
        """Произвольные байты как дерево данных (блоки по 8176 байт, последний — короче)."""
        view = memoryview(bytes(data))
        bids: List[int] = []
        sizes: List[int] = []
        if not len(view):
            return self.write_block(b"")
        for start in range(0, len(view), BLOCK_MAX_DATA):
            chunk = view[start:start + BLOCK_MAX_DATA]
            bids.append(self.write_block(chunk))
            sizes.append(len(chunk))
        return self.write_tree(bids, sizes)

    def write_blocks(self, blocks: Sequence[bytes]) -> int:
        """Узел из заранее разбитых блоков (куча HN, строки таблицы)."""
        bids = [self.write_block(b) for b in blocks]
        return self.write_tree(bids, [len(b) for b in blocks])

    def write_subnodes(self, entries: Iterable[Tuple[int, int, int]]) -> int:
        """Дерево подузлов (SLBLOCK, при большом числе — SIBLOCK над ними)."""
        items = sorted(entries)
        if not items:
            return 0
        seen = set()
        for nid, _d, _s in items:
            if nid in seen:
                raise PstFormatError(f"повтор подузла {nid:#x}")
            seen.add(nid)
        leaves: List[Tuple[int, int]] = []
        for start in range(0, len(items), SLBLOCK_MAX):
            part = items[start:start + SLBLOCK_MAX]
            payload = bytearray(struct.pack("<BBHI", 0x02, 0x00, len(part), 0))
            for nid, bid_data, bid_sub in part:
                payload += struct.pack("<QQQ", nid, bid_data, bid_sub)
            leaves.append((part[0][0], self.write_block(payload, internal=True)))
        if len(leaves) == 1:
            return leaves[0][1]
        if len(leaves) > SIBLOCK_MAX:
            raise PstFormatError("слишком много подузлов у одного узла .pst")
        payload = bytearray(struct.pack("<BBHI", 0x02, 0x01, len(leaves), 0))
        for nid, bid in leaves:
            payload += struct.pack("<QQ", nid, bid)
        return self.write_block(payload, internal=True)

    def add_node(self, nid: int, bid_data: int, bid_sub: int = 0, parent: int = 0) -> None:
        self._node_nid.append(nid)
        self._node_data.append(bid_data)
        self._node_sub.append(bid_sub)
        self._node_parent.append(parent)

    @property
    def node_count(self) -> int:
        return len(self._node_nid)

    # -- страницы и завершение ---------------------------------------------
    def _page(self, ptype: int, body: bytes, ib: int, bid: int, sig: bool = True) -> bytes:
        if len(body) != PAGE_DATA:
            raise PstFormatError("страница должна содержать 496 байт данных")
        wsig = compute_sig(ib, bid) if sig else 0
        return body + struct.pack("<BBHIQ", ptype, ptype, wsig, crc32(body), bid)

    def _btree(self, entries: Iterable[Tuple[int, bytes]], ptype: int,
               leaf_size: int) -> Tuple[int, int]:
        """Страницы B-дерева (NBT/BBT) из записей (ключ, байты) по возрастанию → (bid, ib) корня."""
        per_leaf = 488 // leaf_size
        level: List[Tuple[int, int, int]] = []          # (первый ключ, bid, ib)

        def emit_leaf(part: List[bytes], first: int) -> None:
            body = b"".join(part).ljust(488, b"\x00") + struct.pack("<BBBBI", len(part), per_leaf,
                                                                    leaf_size, 0, 0)
            ib = self._place(PAGE_SIZE, PAGE_SIZE)
            bid = self.next_pid
            self.next_pid += 1
            self._emit(ib, self._page(ptype, body, ib, bid))
            level.append((first, bid, ib))

        part: List[bytes] = []
        first = 0
        for key, entry in entries:
            if not part:
                first = key
            part.append(entry)
            if len(part) == per_leaf:
                emit_leaf(part, first)
                part = []
        if part or not level:
            emit_leaf(part, first)
        depth = 0
        while len(level) > 1:
            depth += 1
            upper: List[Tuple[int, int, int]] = []
            for start in range(0, len(level), 20):
                part = level[start:start + 20]
                raw = b"".join(struct.pack("<QQQ", k, b, i) for k, b, i in part)
                body = raw.ljust(488, b"\x00") + struct.pack("<BBBBI", len(part), 20, 24, depth, 0)
                ib = self._place(PAGE_SIZE, PAGE_SIZE)
                bid = self.next_pid
                self.next_pid += 1
                self._emit(ib, self._page(ptype, body, ib, bid))
                upper.append((part[0][0], bid, ib))
            level = upper
        return level[0][1], level[0][2]

    def finish(self, *, rgnid: Dict[int, int], unique: int = 1) -> int:
        """Дописать B-деревья, карты и заголовок. Возвращает размер файла."""
        self._finishing = True
        # --- NBT
        nids, datas, subs, parents = self._node_nid, self._node_data, self._node_sub, self._node_parent
        order = sorted(range(len(nids)), key=nids.__getitem__)
        for a, b in zip(order, order[1:]):
            if nids[a] == nids[b]:
                raise PstFormatError(f"узел {nids[a]:#x} записан дважды")
        nbt_root = self._btree(((nids[i], struct.pack("<QQQII", nids[i], datas[i], subs[i], parents[i], 0))
                                for i in order), PTYPE_NBT, 32)
        del order
        # --- BBT (у блоков одна ссылка; cRef = 2, как у Outlook). Номера блоков
        # выдаются по возрастанию, поэтому массив уже упорядочен.
        bids, ibs, cbs = self.bbt_bid, self.bbt_ib, self.bbt_cb
        if any(bids[i] >= bids[i + 1] for i in range(len(bids) - 1)):
            raise PstFormatError("внутренняя ошибка: номера блоков не по возрастанию")
        bbt_root = self._btree(((bids[i], struct.pack("<QQHHI", bids[i], ibs[i], cbs[i], 2, 0))
                                for i in range(len(bids))), PTYPE_BBT, 24)
        self._flush()

        # --- карты размещения
        last = _region(self.pos - 1)
        eof = AMAP_FIRST + (last + 1) * AMAP_SPAN
        gaps = sorted(self.gaps)
        bitmaps: List[bytearray] = []
        for k in range(last + 1):
            base = AMAP_FIRST + k * AMAP_SPAN
            used_end = min(self.pos, base + AMAP_SPAN)
            bits = bytearray(PAGE_DATA)
            _set_bits(bits, 0, (used_end - base) // 64, True)
            bitmaps.append(bits)
        for begin, end in gaps:
            k = _region(begin)
            base = AMAP_FIRST + k * AMAP_SPAN
            _set_bits(bitmaps[k], (begin - base) // 64, (end - base) // 64, False)
        free_slots = 0
        longest: List[int] = []
        for bits in bitmaps:
            as_int = int.from_bytes(bits, "big")
            free_slots += AMAP_SLOTS - as_int.bit_count()
            runs = re.findall("0+", format(as_int, f"0{AMAP_SLOTS}b"))
            longest.append(min(255, max((len(r) for r in runs), default=0)))

        f = self.f
        for k, bits in enumerate(bitmaps):
            base = AMAP_FIRST + k * AMAP_SPAN
            f.seek(base)
            f.write(self._page(PTYPE_AMAP, bytes(bits), base, base, sig=False))
            reserved = _reserved_pages(k)
            if reserved >= 2:
                ib = base + PAGE_SIZE
                f.seek(ib)
                f.write(self._page(PTYPE_PMAP, b"\xFF" * PAGE_DATA, ib, ib, sig=False))
            if reserved >= 3:
                ib = base + 2 * PAGE_SIZE
                fmap = bytes(longest[k:k + FMAP_EVERY]).ljust(PAGE_DATA, b"\x00")
                f.seek(ib)
                f.write(self._page(PTYPE_FMAP, fmap, ib, ib, sig=False))
        # DList: пустой список с верной подписью; BID — следующий номер страницы
        dlist = struct.pack("<BBHI", 0, 0, 0, last).ljust(PAGE_DATA, b"\x00")
        f.seek(DLIST_OFFSET)
        f.write(self._page(PTYPE_DLIST, dlist, DLIST_OFFSET, self.next_pid))

        # --- заголовок
        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, 0, 0x4E444221)        # "!BDN"
        struct.pack_into("<HHHBB", header, 8, 0x4D53, 23, 19, 1, 1)
        struct.pack_into("<Q", header, 0x20, self.next_pid)  # bidNextP
        struct.pack_into("<I", header, 0x28, unique)
        for nid_type in range(32):
            value = rgnid.get(nid_type, _RGNID_START.get(nid_type, 0x400))
            struct.pack_into("<I", header, 0x2C + 4 * nid_type, value)
        struct.pack_into("<IQQQQQQQQBBH", header, 0xB4,
                         0, eof, AMAP_FIRST + last * AMAP_SPAN, free_slots * 64, 0,
                         nbt_root[0], nbt_root[1], bbt_root[0], bbt_root[1], 2, 0, 0)
        fm = bytes(longest[:FMAP_FIRST_INDEX]).ljust(128, b"\x00")
        header[0x100:0x180] = fm
        header[0x180:0x200] = b"\xFF" * 128
        header[0x200] = 0x80                                  # bSentinel
        header[0x201] = 0                                     # без шифрования
        struct.pack_into("<Q", header, 0x204, self.next_bid)  # bidNextB
        struct.pack_into("<I", header, 0x20C, crc32(bytes(header[8:8 + 516])))
        struct.pack_into("<I", header, 4, crc32(bytes(header[8:8 + 471])))
        f.seek(0)
        f.write(header)
        f.truncate(eof)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
        f.close()
        self.closed = True
        return eof

    def close(self) -> None:
        if not self.closed:
            try:
                self.f.close()
            finally:
                self.closed = True


def _set_bits(bits: bytearray, start: int, end: int, value: bool) -> None:
    """Установить (или сбросить) биты [start, end) — старший бит байта первый."""
    if end <= start:
        return
    first_full = _align(start, 8)
    last_full = end & ~7
    if first_full >= last_full:
        for slot in range(start, end):
            _set_bit(bits, slot, value)
        return
    for slot in range(start, first_full):
        _set_bit(bits, slot, value)
    bits[first_full // 8:last_full // 8] = (b"\xFF" if value else b"\x00") * ((last_full - first_full) // 8)
    for slot in range(last_full, end):
        _set_bit(bits, slot, value)


def _set_bit(bits: bytearray, slot: int, value: bool) -> None:
    mask = 0x80 >> (slot % 8)
    if value:
        bits[slot // 8] |= mask
    else:
        bits[slot // 8] &= ~mask & 0xFF


class StreamValue:
    """Большое значение, уже записанное деревом данных (например, вложение)."""

    __slots__ = ("bid", "size")

    def __init__(self, bid: int, size: int) -> None:
        self.bid = bid
        self.size = size


class DataStream:
    """Потоковая запись значения: блоки уходят на диск по мере поступления.

    Небольшое значение (до 3580 байт) остаётся в памяти — оно ляжет в кучу
    узла, как у Outlook.
    """

    def __init__(self, ndb: NdbWriter) -> None:
        self.ndb = ndb
        self._buf = bytearray()
        self._bids: List[int] = []
        self._sizes: List[int] = []
        self.size = 0

    def write(self, data) -> None:
        if not data:
            return
        self._buf += data
        self.size += len(data)
        while len(self._buf) >= BLOCK_MAX_DATA:
            chunk = bytes(self._buf[:BLOCK_MAX_DATA])
            del self._buf[:BLOCK_MAX_DATA]
            self._bids.append(self.ndb.write_block(chunk))
            self._sizes.append(len(chunk))

    def finish(self):
        """bytes (малое значение) или StreamValue (записано на диск)."""
        if not self._bids and len(self._buf) <= HEAP_MAX_ALLOC:
            return bytes(self._buf)
        if self._buf:
            self._bids.append(self.ndb.write_block(bytes(self._buf)))
            self._sizes.append(len(self._buf))
            self._buf = bytearray()
        return StreamValue(self.ndb.write_tree(self._bids, self._sizes), self.size)


# ---------------------------------------------------------------------------
#  LTP: куча (HN), BTH, PC, TC
# ---------------------------------------------------------------------------
class Heap:
    """Куча на узле. Блоки кучи — разного размера, как у Outlook."""

    def __init__(self, client_sig: int) -> None:
        self.client_sig = client_sig
        self.root = 0
        self._blocks: List[List[bytes]] = [[]]
        self._used: List[int] = [12]

    @staticmethod
    def _header_size(index: int) -> int:
        if index == 0:
            return 12
        return 66 if index % 128 == 8 else 2

    def alloc(self, data) -> int:
        size = len(data)
        if not 0 < size <= HEAP_MAX_ALLOC:
            raise PstFormatError(f"кусок кучи {size} байт вне пределов")
        index = len(self._blocks) - 1
        count = len(self._blocks[index])
        need = _align(self._used[index] + size, 2) + 4 + 2 * (count + 2)
        if need > BLOCK_MAX_DATA or count + 1 > HEAP_MAX_INDEX:
            if index + 1 > 0xFFFF:
                raise PstFormatError("куча узла слишком велика")
            self._blocks.append([])
            index += 1
            self._used.append(self._header_size(index))
            count = 0
        self._blocks[index].append(bytes(data))
        self._used[index] += size
        return (index << 16) | ((count + 1) << 5)

    def set(self, hid: int, data) -> None:
        index, slot = hid >> 16, ((hid >> 5) & HEAP_MAX_INDEX) - 1
        if len(self._blocks[index][slot]) != len(data):
            raise PstFormatError("размер куска кучи нельзя менять")
        self._blocks[index][slot] = bytes(data)

    def build(self) -> List[bytes]:
        raw_blocks: List[bytearray] = []
        for index, allocations in enumerate(self._blocks):
            body = bytearray(self._header_size(index))
            offsets = [len(body)]
            for item in allocations:
                body += item
                offsets.append(len(body))
            if len(body) % 2:
                body.append(0)
            ib_hnpm = len(body)
            body += struct.pack(f"<HH{len(offsets)}H", len(allocations), 0, *offsets)
            struct.pack_into("<H", body, 0, ib_hnpm)
            raw_blocks.append(body)
        levels = [_fill_level(BLOCK_MAX_DATA - len(b)) for b in raw_blocks]
        first = raw_blocks[0]
        struct.pack_into("<BBI4s", first, 2, 0xEC, self.client_sig, self.root, _pack_fill(levels[:8], 8))
        for index in range(8, len(raw_blocks), 128):
            struct.pack_into("<64s", raw_blocks[index], 2, _pack_fill(levels[index:index + 128], 128))
        return [bytes(b) for b in raw_blocks]


class NodeImage:
    """Узел, готовый к записи: блоки данных и список подузлов."""

    __slots__ = ("blocks", "subnodes")

    def __init__(self, blocks: List[bytes], subnodes: List[Tuple[int, int, int]]) -> None:
        self.blocks = blocks
        self.subnodes = subnodes


class LtpContext:
    """Общее для построителей PC/TC: выдача номеров подузлов и запись больших значений."""

    def __init__(self, ndb: NdbWriter, counters: "NidCounters") -> None:
        self.ndb = ndb
        self.counters = counters

    def big_value(self, data, subnodes: List[Tuple[int, int, int]]) -> int:
        """Значение больше 3580 байт — в отдельный подузел; вернуть его NID."""
        nid = self.counters.next(NT_LTP)
        if isinstance(data, StreamValue):
            bid = data.bid
        else:
            bid = self.ndb.write_stream(data)
        subnodes.append((nid, bid, 0))
        return nid

    def write_node(self, image: NodeImage) -> Tuple[int, int]:
        """Записать узел: (bidData, bidSub)."""
        bid_data = self.ndb.write_blocks(image.blocks)
        bid_sub = self.ndb.write_subnodes(image.subnodes)
        return bid_data, bid_sub


def _encode_var(ptype: int, value) -> Optional[bytes]:
    if isinstance(value, StreamValue):
        return None
    if ptype == PT_UNICODE:
        return str(value).encode("utf-16-le", "surrogatepass")
    if ptype in (PT_BINARY, PT_CLSID):
        return bytes(value)
    raise PstFormatError(f"тип свойства {ptype:#x} не поддерживается")


def build_pc(ctx: LtpContext, props: Iterable[Tuple[int, int, object]],
             extra_subnodes: Sequence[Tuple[int, int, int]] = ()) -> NodeImage:
    """Контекст свойств: [(идентификатор, тип, значение)], None — свойства нет."""
    items = sorted(((pid, ptype, value) for pid, ptype, value in props if value is not None),
                   key=lambda item: item[0])
    if len(items) > HEAP_MAX_ALLOC // 8:
        raise PstFormatError("слишком много свойств в одном объекте")
    heap = Heap(0xBC)
    header = heap.alloc(bytes(8))
    leaf = heap.alloc(bytes(8 * len(items))) if items else 0
    subnodes: List[Tuple[int, int, int]] = list(extra_subnodes)
    records = bytearray()
    last_pid = -1
    for pid, ptype, value in items:
        if pid == last_pid:
            raise PstFormatError(f"свойство {pid:#06x} задано дважды")
        last_pid = pid
        if ptype in (PT_I4,):
            dw = int(value) & 0xFFFFFFFF
        elif ptype == PT_I2:
            dw = int(value) & 0xFFFF
        elif ptype == PT_BOOL:
            dw = 1 if value else 0
        elif ptype in (PT_I8, PT_SYSTIME):
            dw = heap.alloc(struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF))
        else:
            raw = _encode_var(ptype, value)
            if raw is None:                                   # уже записано на диск
                dw = ctx.big_value(value, subnodes)
            elif not raw:
                dw = 0                                        # пустая строка — HNID 0
            elif len(raw) <= HEAP_MAX_ALLOC:
                dw = heap.alloc(raw)
            else:
                dw = ctx.big_value(raw, subnodes)
        records += struct.pack("<HHI", pid, ptype, dw)
    if leaf:
        heap.set(leaf, records)
    heap.set(header, struct.pack("<BBBBI", 0xB5, 2, 6, 0, leaf))
    heap.root = header
    return NodeImage(heap.build(), subnodes)


class TableLayout:
    """Раскладка строки таблицы: столбцы (тег, смещение, размер, бит) и rgib."""

    def __init__(self, rgib: Tuple[int, int, int, int],
                 columns: Sequence[Tuple[int, int, int, int]]) -> None:
        self.rgib = rgib
        self.columns = sorted(columns)
        self.row_size = rgib[3]
        self.by_tag = {tag: (ib, cb, bit) for tag, ib, cb, bit in self.columns}
        self.tcinfo_tail = b"".join(struct.pack("<IHBB", *c) for c in self.columns)


ROW_ID = 0x67F20003
ROW_VER = 0x67F30003
_ROW_KEYS = [(ROW_ID, 0, 4, 0), (ROW_VER, 4, 4, 1)]

# Раскладки шаблонов — как в свежем файле Outlook.
LAYOUT_HIERARCHY = TableLayout((52, 52, 53, 55), _ROW_KEYS + [
    (0x0E300102, 20, 4, 6), (0x0E330014, 24, 8, 7), (0x0E340102, 32, 4, 8),
    (0x0E380003, 36, 4, 9), (0x3001001F, 8, 4, 2), (0x36020003, 12, 4, 3),
    (0x36030003, 16, 4, 4), (0x360A000B, 52, 1, 5), (0x3613001F, 40, 4, 10),
    (0x66350003, 44, 4, 11), (0x66360003, 48, 4, 12)])
LAYOUT_CONTENTS = TableLayout((80, 80, 82, 85), _ROW_KEYS + [
    (0x00170003, 20, 4, 5), (0x001A001F, 12, 4, 3), (0x00360003, 60, 4, 15),
    (0x0037001F, 28, 4, 7), (0x00390040, 40, 8, 9), (0x0042001F, 24, 4, 6),
    (0x0057000B, 80, 1, 13), (0x0058000B, 81, 1, 14), (0x0070001F, 68, 4, 17),
    (0x00710102, 72, 4, 18), (0x0E03001F, 56, 4, 12), (0x0E04001F, 52, 4, 11),
    (0x0E060040, 32, 8, 8), (0x0E070003, 16, 4, 4), (0x0E080003, 48, 4, 10),
    (0x0E170003, 8, 4, 2), (0x10970003, 64, 4, 16), (0x65C60003, 76, 4, 19)])
LAYOUT_ASSOC = TableLayout((56, 56, 57, 59), _ROW_KEYS + [
    (0x001A001F, 12, 4, 3), (0x0E070003, 16, 4, 4), (0x0E170003, 8, 4, 2),
    (0x3001001F, 20, 4, 5), (0x6800001F, 44, 4, 11), (0x6803000B, 56, 1, 12),
    (0x68051003, 48, 4, 13), (0x682F001F, 52, 4, 14), (0x70030003, 24, 4, 6),
    (0x70040102, 28, 4, 7), (0x70050102, 32, 4, 8), (0x7006001F, 36, 4, 9),
    (0x70070003, 40, 4, 10)])
LAYOUT_SEARCH_CONTENTS = TableLayout((72, 72, 75, 78), _ROW_KEYS + [
    (0x00170003, 28, 4, 7), (0x001A001F, 20, 4, 5), (0x00360003, 60, 4, 16),
    (0x0037001F, 36, 4, 9), (0x0042001F, 32, 4, 8), (0x0057000B, 72, 1, 14),
    (0x0058000B, 73, 1, 15), (0x0E03001F, 56, 4, 13), (0x0E04001F, 52, 4, 12),
    (0x0E05001F, 12, 4, 3), (0x0E060040, 40, 8, 10), (0x0E070003, 24, 4, 6),
    (0x0E080003, 48, 4, 11), (0x0E170003, 16, 4, 4), (0x0E2A000B, 74, 1, 17),
    (0x30080040, 64, 8, 18), (0x67F10003, 8, 4, 2)])
LAYOUT_RECEIVE_FOLDERS = TableLayout((16, 16, 16, 17), _ROW_KEYS + [
    (0x001A001F, 12, 4, 3), (0x66050003, 8, 4, 2)])
LAYOUT_OUTGOING_QUEUE = TableLayout((24, 24, 24, 25), _ROW_KEYS + [
    (0x00390040, 16, 8, 4), (0x0E100003, 12, 4, 3), (0x0E140003, 8, 4, 2)])
LAYOUT_ATTACHMENTS = TableLayout((24, 24, 24, 25), _ROW_KEYS + [
    (0x0E200003, 12, 4, 3), (0x3704001F, 20, 4, 5), (0x37050003, 16, 4, 4),
    (0x370B0003, 8, 4, 2)])
LAYOUT_RECIPIENTS = TableLayout((48, 48, 50, 52), _ROW_KEYS + [
    (0x0C150003, 24, 4, 7), (0x0E0F000B, 48, 1, 2), (0x0FF90102, 32, 4, 9),
    (0x0FFE0003, 36, 4, 10), (0x0FFF0102, 16, 4, 5), (0x3001001F, 20, 4, 6),
    (0x3002001F, 8, 4, 3), (0x3003001F, 12, 4, 4), (0x300B0102, 28, 4, 8),
    (0x39000003, 40, 4, 11), (0x39FF001F, 44, 4, 13), (0x3A40000B, 49, 1, 12)])
LAYOUT_6B6 = TableLayout((24, 24, 24, 25), _ROW_KEYS + [
    (0x0E330014, 8, 8, 2), (0x0E370102, 16, 4, 3), (0x0E380003, 20, 4, 4)])
LAYOUT_6D7 = TableLayout((40, 40, 40, 42), _ROW_KEYS + [
    (0x001A001F, 36, 4, 8), (0x0E300102, 16, 4, 4), (0x0E310102, 12, 4, 3),
    (0x0E330014, 28, 8, 7), (0x0E340102, 24, 4, 6), (0x0E380003, 20, 4, 5),
    (0x0E3E0102, 8, 4, 2)])
LAYOUT_6F8 = TableLayout((24, 24, 24, 25), _ROW_KEYS + [
    (0x0E330014, 16, 8, 3), (0x30070040, 8, 8, 2)])

_FIXED_TYPES = {PT_I2: "<H", PT_I4: "<I", PT_BOOL: "<B", PT_I8: "<Q", PT_SYSTIME: "<Q",
                0x0005: "<Q", 0x000A: "<I", 0x0004: "<I"}


class StreamingHeap:
    """Куча таблицы, блоки которой уходят на диск по мере заполнения.

    В таблице содержимого папки могут быть сотни тысяч строк — держать всю
    её кучу в памяти нельзя. Сразу записываются все блоки, кроме нулевого (в
    его заголовке корень кучи и уровни заполнения первых восьми блоков) и
    блоков с картой уровней (8, 136, 264, …): их заголовки известны только в
    конце. Их не больше одного на 128 блоков.
    """

    def __init__(self, ndb: NdbWriter, client_sig: int) -> None:
        self.ndb = ndb
        self.client_sig = client_sig
        self.root = 0
        self._index = 0
        self._allocs: List[bytes] = []
        self._used = 12
        self._bids: List[int] = []
        self._sizes: List[int] = []
        self._levels: List[int] = []
        self._deferred: Dict[int, List[bytes]] = {}

    def alloc(self, data) -> int:
        size = len(data)
        if not 0 < size <= HEAP_MAX_ALLOC:
            raise PstFormatError(f"кусок кучи {size} байт вне пределов")
        count = len(self._allocs)
        need = _align(self._used + size, 2) + 4 + 2 * (count + 2)
        if need > BLOCK_MAX_DATA or count + 1 > HEAP_MAX_INDEX:
            if self._index + 1 > 0xFFFF:
                raise PstFormatError("куча таблицы слишком велика")
            self._close_block()
            self._index += 1
            self._allocs = []
            self._used = Heap._header_size(self._index)
            count = 0
        self._allocs.append(bytes(data))
        self._used += size
        return (self._index << 16) | ((count + 1) << 5)

    def set(self, hid: int, data) -> None:
        index, slot = hid >> 16, ((hid >> 5) & HEAP_MAX_INDEX) - 1
        allocs = self._allocs if index == self._index else self._deferred.get(index)
        if allocs is None or len(allocs[slot]) != len(data):
            raise PstFormatError("кусок кучи уже записан или другого размера")
        allocs[slot] = bytes(data)

    def _body(self, index: int, allocs: List[bytes]) -> bytearray:
        body = bytearray(Heap._header_size(index))
        offsets = [len(body)]
        for item in allocs:
            body += item
            offsets.append(len(body))
        if len(body) % 2:
            body.append(0)
        ib_hnpm = len(body)
        body += struct.pack(f"<HH{len(offsets)}H", len(allocs), 0, *offsets)
        struct.pack_into("<H", body, 0, ib_hnpm)
        return body

    def _close_block(self) -> None:
        body = self._body(self._index, self._allocs)
        self._levels.append(_fill_level(BLOCK_MAX_DATA - len(body)))
        self._sizes.append(len(body))
        if self._index == 0 or self._index % 128 == 8:
            self._deferred[self._index] = self._allocs
            self._bids.append(0)
        else:
            self._bids.append(self.ndb.write_block(bytes(body)))

    @property
    def blocks(self) -> int:
        return self._index + 1

    @property
    def pending_bytes(self) -> int:
        """Сколько ещё допишется при завершении: текущий и отложенные блоки, XBLOCK-и."""
        trees = (self._index // XBLOCK_MAX + 2) * (BLOCK_MAX_DATA + BLOCK_TRAILER)
        return self._used + 64 + len(self._deferred) * (BLOCK_MAX_DATA + 80) + trees

    def finish(self) -> int:
        """Записать оставшиеся блоки; вернуть BID дерева данных кучи."""
        self._close_block()
        levels = self._levels
        for index, allocs in sorted(self._deferred.items()):
            body = self._body(index, allocs)
            if index == 0:
                struct.pack_into("<BBI4s", body, 2, 0xEC, self.client_sig, self.root,
                                 _pack_fill(levels[:8], 8))
            else:
                struct.pack_into("<64s", body, 2, _pack_fill(levels[index:index + 128], 128))
            self._bids[index] = self.ndb.write_block(bytes(body))
        self._deferred = {}
        return self.ndb.write_tree(self._bids, self._sizes)


class TableWriter:
    """Контекст таблицы (TC), который пишется построчно.

    Строки и значения ячеек уходят на диск по мере добавления, в памяти
    остаются только номера строк (4 байта на строку). None в ячейке — ячейки
    нет; пустая строка — ячейка есть, но пустая (HNID 0, как у Outlook).
    """

    def __init__(self, ctx: LtpContext, layout: TableLayout, *, relaxed: bool = False) -> None:
        self.ctx = ctx
        self.layout = layout
        self.relaxed = relaxed
        self.heap = StreamingHeap(ctx.ndb, 0x7C)
        self.subnodes: List[Tuple[int, int, int]] = []
        self.index_header = self.heap.alloc(bytes(8))
        self.tcinfo = self.heap.alloc(bytes(22 + 8 * len(layout.columns)))
        self.ids = array("I")
        self._ordered = True
        self._matrix = bytearray()               # строки, пока помещаются в кучу
        self._row_bids: Optional[List[int]] = None
        self._row_sizes: List[int] = []
        self._per_block = BLOCK_MAX_DATA // layout.row_size

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def pending_bytes(self) -> int:
        """Оценка того, что допишет finish(): индекс строк, отложенные блоки кучи, хвост строк."""
        row_trees = (len(self._row_bids or ()) // XBLOCK_MAX + 1) * (BLOCK_MAX_DATA + BLOCK_TRAILER)
        return self.heap.pending_bytes + len(self._matrix) + 9 * len(self.ids) + row_trees + 1024

    def heap_nearly_full(self) -> bool:
        """Куча таблицы близка к пределу формата (65 536 блоков) с учётом индекса строк."""
        index_blocks = len(self.ids) * 8 // 7000 + 16
        return self.heap.blocks + index_blocks >= 64_000

    def _scope(self):
        return self.ctx.ndb.relaxed() if self.relaxed else _NoLimitChange()

    def add(self, row_id: int, cells: Dict[int, object]) -> None:
        layout = self.layout
        with self._scope():
            row = bytearray(layout.row_size)
            values = dict(cells)
            values[ROW_ID] = row_id
            ceb_at = layout.rgib[2]
            for tag, value in values.items():
                if value is None:
                    continue
                spec = layout.by_tag.get(tag)
                if spec is None:
                    raise PstFormatError(f"в таблице нет столбца {tag:#010x}")
                ib, cb, bit = spec
                ptype = tag & 0xFFFF
                fmt = _FIXED_TYPES.get(ptype)
                if fmt is not None and struct.calcsize(fmt) == cb:
                    number = (1 if value else 0) if ptype == PT_BOOL else int(value)
                    row[ib:ib + cb] = struct.pack(fmt, number & ((1 << (8 * cb)) - 1))
                elif cb == 4:
                    raw = _encode_var(ptype, value)
                    if raw is None:
                        hnid = self.ctx.big_value(value, self.subnodes)
                    elif not raw:
                        hnid = 0
                    elif len(raw) <= HEAP_MAX_ALLOC:
                        hnid = self.heap.alloc(raw)
                    else:
                        hnid = self.ctx.big_value(raw, self.subnodes)
                    row[ib:ib + 4] = struct.pack("<I", hnid)
                else:
                    raise PstFormatError(f"столбец {tag:#010x}: размер {cb} не поддерживается")
                row[ceb_at + bit // 8] |= 0x80 >> (bit % 8)
            if self.ids and row_id <= self.ids[-1]:
                self._ordered = False
            self.ids.append(row_id)
            self._matrix += row
            if self._row_bids is None and len(self._matrix) > HEAP_MAX_ALLOC:
                self._row_bids = []
            if self._row_bids is not None:
                self._flush_rows(final=False)

    def _flush_rows(self, final: bool) -> None:
        """Строки в подузле: строка не пересекает границу блока, неполные
        блоки (кроме последнего) добиваются нулями до 8176 байт."""
        chunk = self._per_block * self.layout.row_size
        while len(self._matrix) >= chunk and (not final or len(self._matrix) > chunk):
            block = bytes(self._matrix[:chunk]).ljust(BLOCK_MAX_DATA, b"\x00")
            del self._matrix[:chunk]
            self._row_bids.append(self.ctx.ndb.write_block(block))
            self._row_sizes.append(len(block))
        if final and self._matrix:
            self._row_bids.append(self.ctx.ndb.write_block(bytes(self._matrix)))
            self._row_sizes.append(len(self._matrix))
            self._matrix = bytearray()

    def _index(self) -> Tuple[int, int]:
        """BTH «номер строки → её позиция» (ключ и значение — по 4 байта)."""
        ids = self.ids
        if self._ordered:
            order: Iterable[int] = range(len(ids))
        else:
            order = sorted(range(len(ids)), key=ids.__getitem__)
            for a, b in zip(order, order[1:]):
                if ids[a] == ids[b]:
                    raise PstFormatError(f"повтор строки {ids[a]:#x} в таблице")
        per_leaf = HEAP_MAX_ALLOC // 8
        level: List[Tuple[int, int]] = []
        leaf = bytearray()
        first = 0
        for position in order:
            if not leaf:
                first = ids[position]
            leaf += struct.pack("<II", ids[position], position)
            if len(leaf) == per_leaf * 8:
                level.append((first, self.heap.alloc(leaf)))
                leaf = bytearray()
        if leaf:
            level.append((first, self.heap.alloc(leaf)))
        depth = 0
        while len(level) > 1:
            depth += 1
            upper: List[Tuple[int, int]] = []
            for start in range(0, len(level), per_leaf):
                part = level[start:start + per_leaf]
                upper.append((part[0][0], self.heap.alloc(b"".join(struct.pack("<II", k, h)
                                                                    for k, h in part))))
            level = upper
        return (level[0][1] if level else 0), depth

    def finish(self) -> Tuple[int, int]:
        """Дописать индекс, строки и описание таблицы → (bidData, bidSub)."""
        with self._scope():
            root, depth = self._index()
            self.heap.set(self.index_header, struct.pack("<BBBBI", 0xB5, 4, 4, depth, root))
            if self._row_bids is None:
                rows_hnid = self.heap.alloc(self._matrix) if self._matrix else 0
            else:
                self._flush_rows(final=True)
                rows_hnid = self.ctx.counters.next(NT_LTP)
                self.subnodes.append((rows_hnid, self.ctx.ndb.write_tree(self._row_bids, self._row_sizes), 0))
            self.heap.set(self.tcinfo, struct.pack("<BBHHHHIII", 0x7C, len(self.layout.columns),
                                                   *self.layout.rgib, self.index_header, rows_hnid, 0)
                          + self.layout.tcinfo_tail)
            self.heap.root = self.tcinfo
            bid_data = self.heap.finish()
            bid_sub = self.ctx.ndb.write_subnodes(self.subnodes)
        return bid_data, bid_sub


class _NoLimitChange:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def write_tc(ctx: LtpContext, layout: TableLayout, rows: Iterable[Tuple[int, Dict[int, object]]],
             *, relaxed: bool = False) -> Tuple[int, int]:
    """Записать таблицу целиком → (bidData, bidSub)."""
    writer = TableWriter(ctx, layout, relaxed=relaxed)
    for row_id, cells in rows:
        writer.add(row_id, cells)
    return writer.finish()


# ---------------------------------------------------------------------------
#  Уровень сообщений: хранилище, папки, письма
# ---------------------------------------------------------------------------
#: Служебное именованное свойство MailArchiver. Письма его не используют; оно
#: нужно, чтобы потоки карты именованных свойств не были пустыми — libpff
#: (а значит, и многие конвертеры) не открывает файл с пустой картой.
NAMEID_GUID = uuid.UUID("5f0d1a6e-3b0c-5c55-9d8e-6d61696c6172")
NAMEID_NAME = "MailArchiverExport"
NAMEID_BUCKETS = 251


def name_to_id_map() -> List[Tuple[int, int, object]]:
    """Свойства узла карты именованных свойств (NID 0x61, [MS-PST] 2.4.7)."""
    name = NAMEID_NAME.encode("utf-16-le")
    strings = struct.pack("<I", len(name)) + name
    strings += bytes(-len(strings) % 4)
    packed_guid = (3 << 1) | 1          # первый GUID из потока, имя — строка
    entry = struct.pack("<IHH", 0, packed_guid, 0)                # свойство 0x8000
    name_hash = crc32(name)
    bucket = (name_hash ^ packed_guid) % NAMEID_BUCKETS
    return [(0x0001, PT_I4, NAMEID_BUCKETS),
            (0x0002, PT_BINARY, NAMEID_GUID.bytes_le),
            (0x0003, PT_BINARY, entry),
            (0x0004, PT_BINARY, strings),
            (0x1000 + bucket, PT_BINARY, struct.pack("<IHH", name_hash, packed_guid, 0))]


class NidCounters:
    """Счётчики номеров узлов по типам; попадают в rgnid заголовка."""

    def __init__(self) -> None:
        self.last: Dict[int, int] = {NT_NORMAL_FOLDER: 0x403,     # 0x401–0x403 — служебные папки
                                     NT_NORMAL_MESSAGE: 0x10000,
                                     NT_ATTACHMENT: 0x400,
                                     NT_LTP: 0x400}

    def next(self, nid_type: int) -> int:
        value = self.last.get(nid_type, 0x400) + 1
        if value >= 1 << 27:
            raise PstFormatError("исчерпаны номера узлов")
        self.last[nid_type] = value
        return make_nid(nid_type, value)

    def rgnid(self) -> Dict[int, int]:
        out = dict(self.last)
        folders = out.get(NT_NORMAL_FOLDER, 0x403)
        for table in (NT_HIERARCHY_TABLE, NT_CONTENTS_TABLE, NT_ASSOC_CONTENTS_TABLE):
            out[table] = folders
        return out


class FolderSpec:
    """Папка файла .pst."""

    __slots__ = ("nid", "name", "parent", "container_class", "children", "count", "unread",
                 "contents")

    def __init__(self, nid: int, name: str, parent: int, container_class: Optional[str]) -> None:
        self.nid = nid
        self.name = name
        self.parent = parent
        self.container_class = container_class
        self.children: List[int] = []
        self.count = 0
        self.unread = 0
        self.contents: Optional[Tuple[int, int]] = None   # записанная таблица содержимого


class PstFile:
    """Один файл .pst уровня «хранилище/папки/письма»."""

    def __init__(self, path: str, display_name: str, *, max_bytes: int = HARD_LIMIT_BYTES) -> None:
        self.ndb = NdbWriter(path, max_bytes=max_bytes)
        self.counters = NidCounters()
        self.ctx = LtpContext(self.ndb, self.counters)
        self.display_name = display_name or "MailArchiver"
        self.record_key = uuid.uuid4().bytes
        self.row_version = 0x100
        self.folders: Dict[int, FolderSpec] = {}
        self.inbox_nid = 0
        self.sent_nid = 0
        self._open_tables: Dict[int, "ContentsTable"] = {}
        self.add_special_folder(NID_ROOT_FOLDER, "", NID_ROOT_FOLDER, None)
        self.add_special_folder(NID_IPM_SUBTREE, "Top of Personal Folders", NID_ROOT_FOLDER, None)
        self.add_special_folder(NID_SEARCH_ROOT, "Search Root", NID_ROOT_FOLDER, None)
        self.add_special_folder(NID_DELETED_ITEMS, "Deleted Items", NID_IPM_SUBTREE, None)

    # -- папки ------------------------------------------------------------
    def add_special_folder(self, nid: int, name: str, parent: int, container_class: Optional[str]) -> None:
        spec = FolderSpec(nid, name, parent, container_class)
        self.folders[nid] = spec
        if parent != nid and parent in self.folders:
            self.folders[parent].children.append(nid)

    def rename_folder(self, nid: int, name: str) -> None:
        self.folders[nid].name = name

    def add_folder(self, name: str, parent: int, container_class: str = "IPF.Note") -> int:
        nid = self.counters.next(NT_NORMAL_FOLDER)
        self.add_special_folder(nid, name, parent, container_class)
        return nid

    def next_row_version(self) -> int:
        self.row_version += 1
        return self.row_version

    def _stamp(self, cells: Dict[int, object]) -> Dict[int, object]:
        """Копия ячеек строки с новой версией строки (PidTagLtpRowVer)."""
        out = dict(cells)
        out[ROW_VER] = self.next_row_version()
        return out

    # -- запись значений --------------------------------------------------
    @property
    def position(self) -> int:
        return self.ndb.position

    def stream(self) -> DataStream:
        return DataStream(self.ndb)

    def checkpoint(self) -> Checkpoint:
        return self.ndb.checkpoint()

    def rollback(self, cp: Checkpoint) -> None:
        self.ndb.rollback(cp)

    def overhead_estimate(self) -> int:
        """Сколько ещё допишется при закрытии части: B-деревья, узлы папок,
        недописанные таблицы содержимого."""
        pages = (len(self.ndb.bbt_bid) // 20 + self.ndb.node_count // 15 + 16) * PAGE_SIZE
        tables = sum(t.pending_bytes for t in self._open_tables.values())
        return pages * 2 + tables + len(self.folders) * 4096 + 64 * 1024

    # -- письма -------------------------------------------------------------
    def write_message(self, folder_nid: int, props: List[Tuple[int, int, object]],
                      recipients: List[Dict[int, object]],
                      attachments: List[Tuple[List[Tuple[int, int, object]], Dict[int, object]]]) -> int:
        """Записать письмо. recipients — ячейки строк таблицы получателей;
        attachments — [(свойства вложения, ячейки строки таблицы вложений)]."""
        nid = self.counters.next(NT_NORMAL_MESSAGE)
        subnodes: List[Tuple[int, int, int]] = []
        rows = [(i + 1, self._stamp(cells)) for i, cells in enumerate(recipients)]
        bid_data, bid_sub = write_tc(self.ctx, LAYOUT_RECIPIENTS, rows)
        subnodes.append((NID_RECIPIENT_TABLE, bid_data, bid_sub))
        if attachments:
            table_rows = []
            for att_props, cells in attachments:
                att_nid = self.counters.next(NT_ATTACHMENT)
                bid_data, bid_sub = self.ctx.write_node(build_pc(self.ctx, att_props))
                subnodes.append((att_nid, bid_data, bid_sub))
                table_rows.append((att_nid, self._stamp(cells)))
            bid_data, bid_sub = write_tc(self.ctx, LAYOUT_ATTACHMENTS, table_rows)
            subnodes.append((NID_ATTACHMENT_TABLE, bid_data, bid_sub))
        image = build_pc(self.ctx, props, subnodes)
        bid_data, bid_sub = self.ctx.write_node(image)
        self.ndb.add_node(nid, bid_data, bid_sub, folder_nid)
        return nid

    def open_contents(self, folder_nid: int) -> "ContentsTable":
        """Начать таблицу содержимого папки: строки добавляются по мере записи писем."""
        folder = self.folders[folder_nid]
        if folder.contents is not None or folder_nid in self._open_tables:
            raise PstFormatError("таблица содержимого папки уже записана")
        table = ContentsTable(self, folder)
        self._open_tables[folder_nid] = table
        return table

    def set_contents(self, folder_nid: int, rows: Iterable[Tuple[int, Dict[int, object]]],
                     unread: int) -> None:
        """Таблица содержимого папки целиком: [(nid письма, ячейки)]."""
        table = self.open_contents(folder_nid)
        for nid, cells in rows:
            table.add(nid, cells, False)
        table.unread = unread
        table.close()

    # -- завершение -------------------------------------------------------
    def _write_tc(self, nid: int, layout: TableLayout, rows, parent: int = 0) -> None:
        bid_data, bid_sub = write_tc(self.ctx, layout, rows, relaxed=True)
        self.ndb.add_node(nid, bid_data, bid_sub, parent)

    def _write_pc(self, nid: int, props, parent: int = 0) -> None:
        bid_data, bid_sub = self.ctx.write_node(build_pc(self.ctx, props))
        self.ndb.add_node(nid, bid_data, bid_sub, parent)

    def _folder_row(self, spec: FolderSpec) -> Tuple[int, Dict[int, object]]:
        return spec.nid, {
            0x3001001F: spec.name,
            0x36020003: spec.count,
            0x36030003: spec.unread,
            0x360A000B: bool(spec.children),
            0x3613001F: spec.container_class,
            ROW_VER: self.next_row_version(),
        }

    def close(self) -> int:
        """Дописать служебные узлы, папки и индексы; вернуть размер файла."""
        self.ndb._finishing = True          # служебные узлы пишутся и сверх предела части
        for table in list(self._open_tables.values()):
            table.close()
        rk = self.record_key
        mask = 0x01 | 0x08 | 0x80          # IPM-дерево, «Удалённые», поиск
        store = [
            (0x0FF9, PT_BINARY, rk),
            (0x3001, PT_UNICODE, self.display_name),
            (0x35E0, PT_BINARY, entry_id(rk, NID_IPM_SUBTREE)),
            (0x35E3, PT_BINARY, entry_id(rk, NID_DELETED_ITEMS)),
            (0x35E7, PT_BINARY, entry_id(rk, NID_SEARCH_ROOT)),
            (0x67FF, PT_I4, 0),                                  # без пароля
        ]
        if self.inbox_nid:
            mask |= 0x02
        if self.sent_nid:
            mask |= 0x10
            store.append((0x35E4, PT_BINARY, entry_id(rk, self.sent_nid)))
        store.append((0x35DF, PT_I4, mask))
        self._write_pc(NID_MESSAGE_STORE, store)
        self._write_pc(NID_NAME_TO_ID_MAP, name_to_id_map())
        for nid in (NID_SEARCH_MANAGEMENT_QUEUE, NID_SEARCH_ACTIVITY_LIST):
            self.ndb.add_node(nid, 0, 0, 0)
        # Шаблоны таблиц
        self._write_tc(NID_HIERARCHY_TEMPLATE, LAYOUT_HIERARCHY, [])
        self._write_tc(NID_CONTENTS_TEMPLATE, LAYOUT_CONTENTS, [])
        self._write_tc(NID_ASSOC_TEMPLATE, LAYOUT_ASSOC, [])
        self._write_tc(NID_SEARCH_CONTENTS_TEMPLATE, LAYOUT_SEARCH_CONTENTS, [])
        inbox = self.inbox_nid or NID_IPM_SUBTREE
        receive = [(1, {0x001A001F: "", 0x66050003: inbox}), (2, {0x001A001F: "IPM", 0x66050003: inbox}),
                   (3, {0x001A001F: "Report.IPM", 0x66050003: inbox}),
                   (4, {0x001A001F: "IPC", 0x66050003: NID_ROOT_FOLDER})]
        self._write_tc(NID_RECEIVE_FOLDER_TABLE, LAYOUT_RECEIVE_FOLDERS,
                       [(r, self._stamp(c)) for r, c in receive])
        self._write_tc(NID_OUTGOING_QUEUE_TABLE, LAYOUT_OUTGOING_QUEUE, [])
        self._write_tc(NID_ATTACHMENT_TABLE, LAYOUT_ATTACHMENTS, [])
        self._write_tc(NID_RECIPIENT_TABLE, LAYOUT_RECIPIENTS, [])
        self._write_tc(NID_TEMPLATE_6B6, LAYOUT_6B6, [])
        self._write_tc(NID_TEMPLATE_6D7, LAYOUT_6D7, [])
        self._write_tc(NID_TEMPLATE_6F8, LAYOUT_6F8, [])
        # Обязательная поисковая папка «SPAM Search Folder 2»
        self._write_pc(NID_SPAM_SEARCH_FOLDER, [(0x3001, PT_UNICODE, "SPAM Search Folder 2"),
                                                (0x3602, PT_I4, 0), (0x3603, PT_I4, 0),
                                                (0x360A, PT_BOOL, False)], NID_ROOT_FOLDER)
        self.ndb.add_node(NID_SPAM_UPDATE_QUEUE, 0, 0, 0)
        self._write_pc(NID_SPAM_CRITERIA, [(0x660B, PT_I4, 0)])
        self._write_tc(NID_SPAM_CONTENTS, LAYOUT_SEARCH_CONTENTS, [])
        # Папки
        for spec in self.folders.values():
            props = [(0x3001, PT_UNICODE, spec.name), (0x3602, PT_I4, spec.count),
                     (0x3603, PT_I4, spec.unread), (0x360A, PT_BOOL, bool(spec.children))]
            if spec.container_class:
                props.append((0x3613, PT_UNICODE, spec.container_class))
            self._write_pc(spec.nid, props, spec.parent)
            index = spec.nid >> 5
            rows = [self._folder_row(self.folders[c]) for c in spec.children]
            if spec.nid == NID_ROOT_FOLDER:
                rows.append((NID_SPAM_SEARCH_FOLDER, {0x3001001F: "SPAM Search Folder 2", 0x36020003: 0,
                                                      0x36030003: 0, 0x360A000B: False,
                                                      ROW_VER: self.next_row_version()}))
            self._write_tc(make_nid(NT_HIERARCHY_TABLE, index), LAYOUT_HIERARCHY, rows)
            contents = spec.contents
            if contents is None:
                contents = write_tc(self.ctx, LAYOUT_CONTENTS, [], relaxed=True)
            self.ndb.add_node(make_nid(NT_CONTENTS_TABLE, index), contents[0], contents[1], 0)
            self._write_tc(make_nid(NT_ASSOC_CONTENTS_TABLE, index), LAYOUT_ASSOC, [])
        return self.ndb.finish(rgnid=self.counters.rgnid())

    def abort(self) -> None:
        self.ndb.close()


class ContentsTable:
    """Таблица содержимого папки, которая растёт вместе с письмами."""

    def __init__(self, pst: PstFile, folder: FolderSpec) -> None:
        self.pst = pst
        self.folder = folder
        self.writer = TableWriter(pst.ctx, LAYOUT_CONTENTS, relaxed=True)
        self.unread = 0

    def __len__(self) -> int:
        return len(self.writer)

    @property
    def pending_bytes(self) -> int:
        return self.writer.pending_bytes

    def nearly_full(self) -> bool:
        return self.writer.heap_nearly_full()

    def add(self, nid: int, cells: Dict[int, object], unread: bool) -> None:
        self.writer.add(nid, self.pst._stamp(cells))
        if unread:
            self.unread += 1

    def close(self) -> None:
        if self.pst._open_tables.pop(self.folder.nid, None) is None:
            return
        self.folder.contents = self.writer.finish()
        self.folder.count = len(self.writer)
        self.folder.unread = self.unread
