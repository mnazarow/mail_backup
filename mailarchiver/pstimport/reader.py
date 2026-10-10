"""
Чтение файлов Outlook .pst ([MS-PST]) без внешних программ.

Пара к писателю ``mailarchiver.export.pstformat``: модуль знает только формат
и ничего не знает о почте IMAP. Письма в виде RFC 822 собирает ``eml``, а
заливает их на сервер или в локальную копию ``importer``.

Что читается:

* оба варианта файла — Unicode (Outlook 2003 и новее) и ANSI (Outlook 97–2002);
* все три способа «шифрования» данных: без него, перестановочное
  («сжимаемое шифрование» — так Outlook создаёт файлы по умолчанию) и
  циклическое («усиленное шифрование»). Пароль файла на содержимое не влияет:
  это всего лишь свойство хранилища, которое проверяет сам Outlook;
* слои формата снизу вверх: блоки и B-деревья (NDB), деревья данных
  XBLOCK/XXBLOCK и подузлов SLBLOCK/SIBLOCK, куча на узле (HN), B-дерево в
  куче (BTH), контекст свойств (PC) и таблица (TC), а над ними — хранилище,
  папки, письма, получатели и вложения.

Не читается формат с блоками по 4 КБ (``.ost`` Outlook 2013 и новее).

Файл приходит от пользователя, поэтому ничему в нём не верим: каждое смещение
и размер проверяются, глубина деревьев и число записей ограничены, а ошибка в
одном письме не мешает прочитать остальные (:class:`PstReadError` ловит тот,
кто перебирает письма). Контрольные суммы блоков и страниц сверяются, но
несовпадение чтение не останавливает — оно считается в ``damaged``: файл с
одной битой суммой не должен терять все письма.

Память. Файл целиком не читается. B-дерево блоков (BBT) обходится по запросу
с небольшим кэшем страниц; из B-дерева узлов (NBT) один раз забираются номера
узлов в компактные массивы (28 байт на письмо). Вложения отдаются кусками.
"""
from __future__ import annotations

import struct
import zlib
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from typing import Callable, Dict, Iterator, List, Optional, Tuple

# ---------------------------------------------------------------------------
#  Константы формата
# ---------------------------------------------------------------------------
PAGE_SIZE = 512
MAGIC = 0x4E444221               # «!BDN»
MAGIC_CLIENT = 0x4D53            # «SM» — файл хранилища Outlook (.pst)
MAGIC_CLIENT_OST = 0x4F53        # «SO» — автономная копия ящика Exchange (.ost)
PTYPE_BBT = 0x80
PTYPE_NBT = 0x81

CRYPT_NONE = 0
CRYPT_PERMUTE = 1
CRYPT_CYCLIC = 2

NT_NORMAL_FOLDER = 0x02
NT_NORMAL_MESSAGE = 0x04
NT_HIERARCHY_TABLE = 0x0D
NT_CONTENTS_TABLE = 0x0E

NID_MESSAGE_STORE = 0x21
NID_ROOT_FOLDER = 0x122
NID_IPM_SUBTREE = 0x8022
NID_ATTACHMENT_TABLE = 0x671
NID_RECIPIENT_TABLE = 0x692

# Типы свойств MAPI
PT_I2 = 0x0002
PT_I4 = 0x0003
PT_FLOAT = 0x0004
PT_DOUBLE = 0x0005
PT_CURRENCY = 0x0006
PT_APPTIME = 0x0007
PT_ERROR = 0x000A
PT_BOOL = 0x000B
PT_OBJECT = 0x000D
PT_I8 = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_BINARY = 0x0102

#: Размер значения фиксированных типов.
_FIXED = {PT_I2: 2, PT_I4: 4, PT_FLOAT: 4, PT_DOUBLE: 8, PT_CURRENCY: 8, PT_APPTIME: 8,
          PT_ERROR: 4, PT_BOOL: 1, PT_I8: 8, PT_SYSTIME: 8, PT_CLSID: 16}

FILETIME_EPOCH_DIFF = 11644473600

# -- Пределы: файл чужой, и ни одно его число не должно съесть память службы --
#: Глубже этого B-деревья страниц не бывают (в файле на 50 ГБ — 4–5 уровней).
MAX_BTREE_DEPTH = 16
#: Сколько разобранных страниц BBT держать в памяти.
PAGE_CACHE = 4096
#: Узлов в файле (письма, папки, служебные) — больше не читаем.
MAX_NODES = 5_000_000
#: Подузлов у одного узла (вложения письма и их большие значения).
MAX_SUBNODES = 400_000
#: Блоков в одном дереве данных (≈ 32 ГБ по 8 КБ — заведомо больше любого значения).
MAX_TREE_BLOCKS = 4_000_000
#: Куча одного узла: свойства письма или таблица папки.
MAX_HEAP_BYTES = 64 * 1024 * 1024
#: Куча свойств одного вложения (само вложение лежит отдельно, в подузле).
MAX_ATTACHMENT_HEAP = 4 * 1024 * 1024
#: Кучи свойств всех вложений одного письма вместе: объекты вложений живут, пока
#: собирается письмо, и в повреждённом файле сотни вложений могут ссылаться на одну кучу.
MAX_ATTACHMENTS_HEAP_TOTAL = 64 * 1024 * 1024
#: Записей в одном B-дереве кучи (у таблицы содержимого большой папки — по записи на письмо).
MAX_BTH_RECORDS = 5_000_000
#: Свойств у одного объекта.
MAX_PROPERTIES = 20_000
MAX_BTH_DEPTH = 8
#: Одна ячейка таблицы получателей или вложений (имя, адрес) и вся таблица вместе.
MAX_CELL_BYTES = 64 * 1024
MAX_TABLE_BYTES = 64 * 1024 * 1024
#: Значение свойства, которое читается в память целиком (строки, тело письма).
MAX_VALUE_BYTES = 64 * 1024 * 1024
#: Строк в таблице получателей одного письма.
MAX_TABLE_ROWS = 100_000
#: Вложений у одного письма.
MAX_ATTACHMENTS = 10_000
#: После стольких нечитаемых вложений остальные у письма не читаются.
MAX_ATTACHMENT_ERRORS = 50
#: Вложенность папок и их число.
MAX_FOLDER_DEPTH = 64
MAX_FOLDERS = 50_000
#: Куча свойств папки (там имя, счётчики и класс — сотни байт).
MAX_FOLDER_HEAP = 1024 * 1024


class PstReadError(Exception):
    """Файл или его часть не читается: повреждение либо нарушение формата."""


class PstUnsupportedError(PstReadError):
    """Файл цел, но этот его вид читатель не умеет (или это вовсе не .pst)."""


# ---------------------------------------------------------------------------
#  «Шифрование» данных ([MS-PST] 5.1, 5.2)
# ---------------------------------------------------------------------------
# Таблицы перестановок mpbbR и mpbbS из спецификации. Третья, mpbbI, — обратная
# к mpbbR (ею расшифровывается перестановочное кодирование), поэтому не
# хранится, а вычисляется; тест проверяет, что mpbbR — перестановка, а mpbbS —
# инволюция (иначе циклическое кодирование не было бы обратимо само собой).
_MPBB_R = bytes((
    65, 54, 19, 98, 168, 33, 110, 187, 244, 22, 204, 4, 127, 100, 232, 93,
    30, 242, 203, 42, 116, 197, 94, 53, 210, 149, 71, 158, 150, 45, 154, 136,
    76, 125, 132, 63, 219, 172, 49, 182, 72, 95, 246, 196, 216, 57, 139, 231,
    35, 59, 56, 142, 200, 193, 223, 37, 177, 32, 165, 70, 96, 78, 156, 251,
    170, 211, 86, 81, 69, 124, 85, 0, 7, 201, 43, 157, 133, 155, 9, 160,
    143, 173, 179, 15, 99, 171, 137, 75, 215, 167, 21, 90, 113, 102, 66, 191,
    38, 74, 107, 152, 250, 234, 119, 83, 178, 112, 5, 44, 253, 89, 58, 134,
    126, 206, 6, 235, 130, 120, 87, 199, 141, 67, 175, 180, 28, 212, 91, 205,
    226, 233, 39, 79, 195, 8, 114, 128, 207, 176, 239, 245, 40, 109, 190, 48,
    77, 52, 146, 213, 14, 60, 34, 50, 229, 228, 249, 159, 194, 209, 10, 129,
    18, 225, 238, 145, 131, 118, 227, 151, 230, 97, 138, 23, 121, 164, 183, 220,
    144, 122, 92, 140, 2, 166, 202, 105, 222, 80, 26, 17, 147, 185, 82, 135,
    88, 252, 237, 29, 55, 73, 27, 106, 224, 41, 51, 153, 189, 108, 217, 148,
    243, 64, 84, 111, 240, 198, 115, 184, 214, 62, 101, 24, 68, 31, 221, 103,
    16, 241, 12, 25, 236, 174, 3, 161, 20, 123, 169, 11, 255, 248, 163, 192,
    162, 1, 247, 46, 188, 36, 104, 117, 13, 254, 186, 47, 181, 208, 218, 61))
_MPBB_S = bytes((
    20, 83, 15, 86, 179, 200, 122, 156, 235, 101, 72, 23, 22, 21, 159, 2,
    204, 84, 124, 131, 0, 13, 12, 11, 162, 98, 168, 118, 219, 217, 237, 199,
    197, 164, 220, 172, 133, 116, 214, 208, 167, 155, 174, 154, 150, 113, 102, 195,
    99, 153, 184, 221, 115, 146, 142, 132, 125, 165, 94, 209, 93, 147, 177, 87,
    81, 80, 128, 137, 82, 148, 79, 78, 10, 107, 188, 141, 127, 110, 71, 70,
    65, 64, 68, 1, 17, 203, 3, 63, 247, 244, 225, 169, 143, 60, 58, 249,
    251, 240, 25, 48, 130, 9, 46, 201, 157, 160, 134, 73, 238, 111, 77, 109,
    196, 45, 129, 52, 37, 135, 27, 136, 170, 252, 6, 161, 18, 56, 253, 76,
    66, 114, 100, 19, 55, 36, 106, 117, 119, 67, 255, 230, 180, 75, 54, 92,
    228, 216, 53, 61, 69, 185, 44, 236, 183, 49, 43, 41, 7, 104, 163, 14,
    105, 123, 24, 158, 33, 57, 190, 40, 26, 91, 120, 245, 35, 202, 42, 176,
    175, 62, 254, 4, 140, 231, 229, 152, 50, 149, 211, 246, 74, 232, 166, 234,
    233, 243, 213, 47, 112, 32, 242, 31, 5, 103, 173, 85, 16, 206, 205, 227,
    39, 59, 218, 186, 215, 194, 38, 212, 145, 29, 210, 28, 34, 51, 248, 250,
    241, 90, 239, 207, 144, 182, 139, 181, 189, 192, 191, 8, 151, 30, 108, 226,
    97, 224, 198, 193, 89, 171, 187, 88, 222, 95, 223, 96, 121, 126, 178, 138))


def _inverse(table: bytes) -> bytes:
    out = bytearray(256)
    for index, value in enumerate(table):
        out[value] = index
    return bytes(out)


_MPBB_I = _inverse(_MPBB_R)
_LOW7 = int.from_bytes(b"\x7f" * 8192, "little")
_HIGH = int.from_bytes(b"\x80" * 8192, "little")
_COUNTER = bytes(range(256)) * 34                       # младший байт счётчика, с запасом на блок
_NEGATE = bytes((-i) & 0xFF for i in range(256))
_cyclic_tables: Dict[int, bytes] = {}


def _cyclic_table(high: int) -> bytes:
    """Средняя часть циклического кодирования при старшем байте счётчика ``high``."""
    table = _cyclic_tables.get(high)
    if table is None:
        table = bytes(_MPBB_I[(_MPBB_S[(_MPBB_R[x] + high) & 0xFF] - high) & 0xFF] for x in range(256))
        _cyclic_tables[high] = table
    return table


def _add_bytes(data: bytes, addend: bytes) -> bytes:
    """Побайтное сложение по модулю 256 двух строк одной длины — без цикла по байтам.

    Обе строки берутся как большие целые: младшие семь бит складываются без
    переноса между байтами, старший бит — отдельно исключающим «или».
    """
    size = len(data)
    mask7 = _LOW7 & ((1 << (8 * size)) - 1)
    mask8 = _HIGH & ((1 << (8 * size)) - 1)
    a = int.from_bytes(data, "little")
    b = int.from_bytes(addend, "little")
    return (((a & mask7) + (b & mask7)) ^ ((a ^ b) & mask8)).to_bytes(size, "little")


def decode_permute(data: bytes) -> bytes:
    return data.translate(_MPBB_I)


def encode_permute(data: bytes) -> bytes:
    return data.translate(_MPBB_R)


def crypt_cyclic(data: bytes, key: int) -> bytes:
    """Циклическое кодирование блока. Оно обратимо само собой: тем же вызовом и
    кодируется, и раскодируется. ``key`` — младшие 32 бита номера блока.

    В спецификации это цикл по байтам со счётчиком ``w``; здесь он развёрнут в
    несколько операций над блоком целиком — иначе файл в гигабайт читался бы часами.
    """
    size = len(data)
    if not size:
        return b""
    out = bytearray()
    w = (key ^ (key >> 16)) & 0xFFFF
    for start in range(0, size, 8192):                  # блоки не длиннее 8 КБ — на всякий случай
        part = data[start:start + 8192]
        low0 = w & 0xFF
        lows = _COUNTER[low0:low0 + len(part)]
        stage = _add_bytes(part, lows)                  # b + младший байт счётчика
        pieces = []
        pos = 0
        high = (w >> 8) & 0xFF
        run = 256 - low0                                # старший байт меняется, когда младший обнуляется
        while pos < len(stage):
            pieces.append(stage[pos:pos + run].translate(_cyclic_table(high)))
            pos += run
            run = 256
            high = (high + 1) & 0xFF
        out += _add_bytes(b"".join(pieces), lows.translate(_NEGATE))
        w = (w + len(part)) & 0xFFFF
    return bytes(out)


def crc32(data) -> int:
    """CRC-32 из [MS-PST] 5.3 — обычный CRC-32 без начальной и конечной инверсии."""
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def compute_sig(ib: int, bid: int) -> int:
    value = (ib ^ bid) & 0xFFFFFFFF
    return ((value >> 16) ^ value) & 0xFFFF


def filetime_to_epoch(value: Optional[int]) -> Optional[float]:
    """FILETIME (сотни наносекунд с 1601 года) → секунды UNIX. Нулевое и немыслимое — ``None``."""
    if not value or value < 0:
        return None
    epoch = value / 10_000_000 - FILETIME_EPOCH_DIFF
    # до 1971 года электронных писем не было, а после 2200-го дату не напечатать
    if epoch < 31_536_000 or epoch > 7_258_118_400:
        return None
    return epoch


def nid_type(nid: int) -> int:
    return nid & 0x1F


# ---------------------------------------------------------------------------
#  NDB: заголовок, страницы B-деревьев, блоки, деревья данных и подузлов
# ---------------------------------------------------------------------------
class _Layout:
    """Различия вариантов файла: в ANSI номера блоков и смещения — 4 байта, в Unicode — 8."""

    def __init__(self, unicode_: bool) -> None:
        self.unicode = unicode_
        if unicode_:
            self.page_entries = 488          # под записи B-дерева
            self.page_trailer = "<BBHIQ"     # ptype, ptypeRepeat, wSig, dwCRC, bid
            self.page_trailer_at = 496
            self.bt_entry = "<QQQ"           # ключ, BID потомка, смещение потомка
            self.bbt_entry = "<QQHH"         # BID, смещение, размер, счётчик ссылок
            self.nbt_entry = "<QQQI"         # NID, BID данных, BID подузлов, NID родителя
            self.block_trailer = 16
            self.bid_fmt = "Q"
            self.sub_header = 8
            self.sl_entry = "<QQQ"
            self.si_entry = "<QQ"
            self.block_max = 8192 - 16
        else:
            self.page_entries = 496
            self.page_trailer = "<BBHII"     # ptype, ptypeRepeat, wSig, bid, dwCRC
            self.page_trailer_at = 500
            self.bt_entry = "<III"
            self.bbt_entry = "<IIHH"
            self.nbt_entry = "<IIII"
            self.block_trailer = 12
            self.bid_fmt = "I"
            self.sub_header = 4
            self.sl_entry = "<III"
            self.si_entry = "<II"
            self.block_max = 8192 - 12
        self.bt_entry_size = struct.calcsize(self.bt_entry)
        self.bbt_entry_size = struct.calcsize(self.bbt_entry)
        self.nbt_entry_size = struct.calcsize(self.nbt_entry)
        self.sl_entry_size = struct.calcsize(self.sl_entry)
        self.si_entry_size = struct.calcsize(self.si_entry)
        self.bid_size = struct.calcsize(self.bid_fmt)


class Ndb:
    """Нижний слой: блоки по номеру (BID) и узлы по номеру (NID)."""

    def __init__(self, fh, size: int) -> None:
        self.fh = fh
        self.size = size
        #: блоков и страниц с неверной контрольной суммой или трейлером
        self.damaged = 0
        self._pages: "OrderedDict[int, tuple]" = OrderedDict()
        self._read_header()
        self._node_nid = array("I")
        self._node_data = array("Q")
        self._node_sub = array("Q")
        self._node_parent = array("I")
        self._scan_nbt()

    # -- файл --------------------------------------------------------------
    def _read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise PstReadError(f"обращение за пределы файла (смещение {offset:#x}, {size} байт)")
        self.fh.seek(offset)
        data = self.fh.read(size)
        if len(data) != size:
            raise PstReadError(f"файл обрывается на смещении {offset:#x}")
        return data

    def _read_header(self) -> None:
        if self.size < 564:
            raise PstUnsupportedError("файл слишком мал для .pst")
        head = self._read(0, 564)
        magic, _crc, client, version = struct.unpack_from("<IIHH", head, 0)
        if magic != MAGIC:
            raise PstUnsupportedError("это не файл Outlook (.pst): нет подписи «!BDN»")
        if client not in (MAGIC_CLIENT, MAGIC_CLIENT_OST):
            raise PstUnsupportedError("это не хранилище писем Outlook (.pst): файл другого назначения, "
                                      "например автономная адресная книга")
        self.version = version
        self.ost = client == MAGIC_CLIENT_OST
        if version in (14, 15):
            self.layout = _Layout(False)
            (_reserved, eof, _amap, _free, _pfree, nbt_bid, nbt_ib, bbt_bid, bbt_ib) = \
                struct.unpack_from("<IIIIIIIII", head, 0xA4)
            self.crypt = head[0x1CD]
        elif version >= 23:
            self.layout = _Layout(True)
            (_reserved, eof, _amap, _free, _pfree, nbt_bid, nbt_ib, bbt_bid, bbt_ib) = \
                struct.unpack_from("<IQQQQQQQQ", head, 0xB4)
            self.crypt = head[0x201]
            # Версии новее 23 бывают с блоками по 4 КБ (.ost Outlook 2013 и новее, wVer 36)
            # — у них и страницы другие. Смотрим на корень B-дерева узлов: если там
            # обычная страница в 512 байт, файл читается как Unicode, иначе — не наш.
            if version > 23 and not self._looks_like_page(nbt_ib):
                raise PstUnsupportedError("файл в формате с блоками по 4 КБ (.ost Outlook 2013 и новее) — "
                                          "встроенный читатель его не разбирает")
        else:
            raise PstUnsupportedError(f"неизвестная версия формата .pst (wVer={version})")
        if self.crypt not in (CRYPT_NONE, CRYPT_PERMUTE, CRYPT_CYCLIC):
            raise PstUnsupportedError(f"неизвестный способ кодирования данных (bCryptMethod={self.crypt})")
        self.eof = eof
        self.nbt_root = nbt_ib
        self.bbt_root = bbt_ib
        for name, ib in (("узлов", nbt_ib), ("блоков", bbt_ib)):
            if ib < 564 or ib % PAGE_SIZE or ib + PAGE_SIZE > self.size:
                raise PstReadError(f"корень B-дерева {name} указывает за пределы файла — файл обрезан "
                                   f"или повреждён")

    def _looks_like_page(self, ib: int) -> bool:
        """Лежит ли по смещению обычная (512 байт) страница B-дерева узлов формата Unicode."""
        if ib < 564 or ib % PAGE_SIZE or ib + PAGE_SIZE > self.size:
            return False
        page = self._read(ib, PAGE_SIZE)
        return page[496] == PTYPE_NBT and page[497] == PTYPE_NBT

    # -- страницы B-деревьев -------------------------------------------------
    def _bt_page(self, ib: int, ptype: int) -> tuple:
        """Страница B-дерева → (уровень, ключи, первое поле, второе поле[, третье…])."""
        cached = self._pages.get(ib)
        if cached is not None and cached[0] == ptype:
            self._pages.move_to_end(ib)
            return cached[1]
        if ib % PAGE_SIZE:
            raise PstReadError(f"страница B-дерева не выровнена ({ib:#x})")
        page = self._read(ib, PAGE_SIZE)
        lay = self.layout
        trailer = struct.unpack_from(lay.page_trailer, page, lay.page_trailer_at)
        if trailer[0] != ptype or trailer[1] != ptype:
            raise PstReadError(f"страница {ib:#x}: не страница нужного B-дерева (тип {trailer[0]:#x})")
        crc = trailer[3] if lay.unicode else trailer[4]
        if crc32(page[:lay.page_trailer_at]) != crc:
            self.damaged += 1
        count, _count_max, entry_size, level = struct.unpack_from("<BBBB", page, lay.page_entries)
        if level > MAX_BTREE_DEPTH:
            raise PstReadError(f"страница {ib:#x}: уровень {level}")
        if level:
            fmt, need = lay.bt_entry, lay.bt_entry_size
        elif ptype == PTYPE_BBT:
            fmt, need = lay.bbt_entry, lay.bbt_entry_size
        else:
            fmt, need = lay.nbt_entry, lay.nbt_entry_size
        if entry_size < need or count * entry_size > lay.page_entries:
            raise PstReadError(f"страница {ib:#x}: записей {count} по {entry_size} байт")
        columns: List[list] = [[] for _ in range(4 if not level else 3)]
        for i in range(count):
            for column, value in zip(columns, struct.unpack_from(fmt, page, i * entry_size)):
                column.append(value)
        if ptype == PTYPE_BBT and not level:
            columns[0] = [bid & ~1 for bid in columns[0]]      # служебный бит в ключе не значим
        parsed = (level, *columns)
        if ptype == PTYPE_BBT:
            self._pages[ib] = (ptype, parsed)
            if len(self._pages) > PAGE_CACHE:
                self._pages.popitem(last=False)
        return parsed

    def _scan_nbt(self) -> None:
        """Обойти всё B-дерево узлов по порядку и запомнить узлы в массивах."""
        seen = set()
        nids, datas, subs, parents = self._node_nid, self._node_data, self._node_sub, self._node_parent
        stack: List[Tuple[int, Optional[int]]] = [(self.nbt_root, None)]
        while stack:
            ib, want_level = stack.pop()
            if ib in seen:
                raise PstReadError("B-дерево узлов зациклено")
            seen.add(ib)
            parsed = self._bt_page(ib, PTYPE_NBT)
            level = parsed[0]
            if want_level is not None and level != want_level:
                raise PstReadError(f"страница {ib:#x}: уровень {level} вместо {want_level}")
            if level:
                # потомки — в стек в обратном порядке, чтобы узлы шли по возрастанию
                for child in reversed(parsed[3]):
                    stack.append((child, level - 1))
                continue
            for nid, bid_data, bid_sub, parent in zip(parsed[1], parsed[2], parsed[3], parsed[4]):
                nids.append(nid & 0xFFFFFFFF)
                datas.append(bid_data)
                subs.append(bid_sub)
                parents.append(parent)
            if len(nids) > MAX_NODES:
                raise PstReadError(f"в файле больше {MAX_NODES} узлов")
        if any(nids[i] >= nids[i + 1] for i in range(len(nids) - 1)):
            # дерево не по порядку (файл чинили сторонней программой) — сортируем сами
            order = sorted(range(len(nids)), key=nids.__getitem__)
            self._node_nid = array("I", (nids[i] for i in order))
            self._node_data = array("Q", (datas[i] for i in order))
            self._node_sub = array("Q", (subs[i] for i in order))
            self._node_parent = array("I", (parents[i] for i in order))

    # -- узлы ---------------------------------------------------------------
    def node_count(self) -> int:
        return len(self._node_nid)

    def nodes(self) -> Iterator[Tuple[int, int]]:
        """Все узлы файла: (NID, NID родителя)."""
        return zip(self._node_nid, self._node_parent)

    def has_node(self, nid: int) -> bool:
        i = bisect_left(self._node_nid, nid)
        return i < len(self._node_nid) and self._node_nid[i] == nid

    def node(self, nid: int) -> Tuple[int, int, int]:
        """(BID данных, BID подузлов, NID родителя) узла."""
        i = bisect_left(self._node_nid, nid)
        if i >= len(self._node_nid) or self._node_nid[i] != nid:
            raise PstReadError(f"узла {nid:#x} нет в файле")
        return self._node_data[i], self._node_sub[i], self._node_parent[i]

    # -- блоки --------------------------------------------------------------
    def _find_block(self, bid: int) -> Tuple[int, int]:
        """Смещение и размер данных блока по его номеру — поиском по B-дереву блоков."""
        key = bid & ~1                       # младший бит служебный, в индексе его нет
        ib = self.bbt_root
        want_level: Optional[int] = None
        for _ in range(MAX_BTREE_DEPTH + 1):
            parsed = self._bt_page(ib, PTYPE_BBT)
            level, keys = parsed[0], parsed[1]
            if want_level is not None and level != want_level:
                raise PstReadError(f"страница {ib:#x}: уровень {level} вместо {want_level}")
            if level == 0:
                i = bisect_left(keys, key)
                if i < len(keys) and keys[i] == key:
                    return parsed[2][i], parsed[3][i]
                break
            i = bisect_right(keys, key) - 1
            if i < 0:
                break
            ib = parsed[3][i]
            want_level = level - 1
        raise PstReadError(f"блок {bid:#x} не найден в файле")

    def block_size(self, bid: int) -> int:
        return self._find_block(bid)[1]

    def block(self, bid: int) -> bytes:
        """Данные одного блока (раскодированные, если файл «зашифрован»)."""
        ib, cb = self._find_block(bid)
        lay = self.layout
        if cb > lay.block_max:
            raise PstReadError(f"блок {bid:#x}: размер {cb} больше допустимого")
        total = (cb + lay.block_trailer + 63) & ~63
        raw = self._read(ib, total)
        if lay.unicode:
            t_cb, _sig, t_crc, t_bid = struct.unpack_from("<HHIQ", raw, total - 16)
        else:
            t_cb, _sig, t_bid, t_crc = struct.unpack_from("<HHII", raw, total - 12)
        data = raw[:cb]
        if t_cb != cb or (t_bid & ~1) != (bid & ~1) or crc32(data) != t_crc:
            self.damaged += 1
        if self.crypt and not bid & 2:       # кодируются только блоки данных, служебные — нет
            if self.crypt == CRYPT_PERMUTE:
                data = decode_permute(data)
            else:
                data = crypt_cyclic(data, bid & 0xFFFFFFFE)
        return data

    def _tree_leaves(self, bid: int) -> Iterator[int]:
        """Номера блоков данных дерева по порядку (сам блок, XBLOCK или XXBLOCK)."""
        if not bid:
            return
        if not bid & 2:
            yield bid
            return
        lay = self.layout
        top = self.block(bid)
        if len(top) < 8 or top[0] != 0x01 or top[1] not in (1, 2):
            raise PstReadError(f"блок {bid:#x}: не дерево данных")
        count = struct.unpack_from("<H", top, 2)[0]
        if 8 + count * lay.bid_size > len(top):
            raise PstReadError(f"блок {bid:#x}: ссылок {count} больше, чем помещается")
        children = struct.unpack_from(f"<{count}{lay.bid_fmt}", top, 8)
        if top[1] == 1:
            for child in children:
                if child & 2:
                    raise PstReadError(f"блок {bid:#x}: XBLOCK ссылается на служебный блок")
                yield child
            return
        emitted = 0
        for child in children:
            if not child & 2:
                raise PstReadError(f"блок {bid:#x}: XXBLOCK ссылается на блок данных")
            mid = self.block(child)
            if len(mid) < 8 or mid[0] != 0x01 or mid[1] != 1:
                raise PstReadError(f"блок {child:#x}: не XBLOCK")
            n = struct.unpack_from("<H", mid, 2)[0]
            if 8 + n * lay.bid_size > len(mid):
                raise PstReadError(f"блок {child:#x}: ссылок {n} больше, чем помещается")
            emitted += n
            if emitted > MAX_TREE_BLOCKS:
                raise PstReadError("значение слишком велико")
            for leaf in struct.unpack_from(f"<{n}{lay.bid_fmt}", mid, 8):
                if leaf & 2:
                    raise PstReadError(f"блок {child:#x}: XBLOCK ссылается на служебный блок")
                yield leaf

    def tree_blocks(self, bid: int) -> Iterator[bytes]:
        """Содержимое дерева данных — блоками по порядку."""
        for leaf in self._tree_leaves(bid):
            yield self.block(leaf)

    def tree_size(self, bid: int) -> int:
        """Полный размер значения в дереве данных, не читая сами данные."""
        if not bid:
            return 0
        if not bid & 2:
            return self.block_size(bid)
        top = self.block(bid)
        if len(top) < 8 or top[0] != 0x01:
            raise PstReadError(f"блок {bid:#x}: не дерево данных")
        return struct.unpack_from("<I", top, 4)[0]

    def tree_bytes(self, bid: int, limit: int = MAX_VALUE_BYTES) -> bytes:
        """Значение из дерева данных целиком — не больше ``limit`` байт."""
        parts = []
        total = 0
        for blk in self.tree_blocks(bid):
            total += len(blk)
            if total > limit:
                raise PstReadError(f"значение больше {limit} байт")
            parts.append(blk)
        return b"".join(parts)

    def subnodes(self, bid: int) -> Dict[int, Tuple[int, int]]:
        """Подузлы узла: NID → (BID данных, BID вложенных подузлов)."""
        out: Dict[int, Tuple[int, int]] = {}
        if not bid:
            return out
        if not bid & 2:
            raise PstReadError(f"блок {bid:#x}: подузлы в блоке данных")
        lay = self.layout
        top = self.block(bid)
        if len(top) < lay.sub_header or top[0] != 0x02 or top[1] not in (0, 1):
            raise PstReadError(f"блок {bid:#x}: не дерево подузлов")
        count = struct.unpack_from("<H", top, 2)[0]
        if top[1] == 0:
            self._sl_entries(bid, top, count, out)
            return out
        if lay.sub_header + count * lay.si_entry_size > len(top):
            raise PstReadError(f"блок {bid:#x}: записей {count} больше, чем помещается")
        for i in range(count):
            _nid, child = struct.unpack_from(lay.si_entry, top, lay.sub_header + i * lay.si_entry_size)
            if not child & 2:
                raise PstReadError(f"блок {bid:#x}: SIBLOCK ссылается на блок данных")
            leaf = self.block(child)
            if len(leaf) < lay.sub_header or leaf[0] != 0x02 or leaf[1] != 0:
                raise PstReadError(f"блок {child:#x}: не SLBLOCK")
            self._sl_entries(child, leaf, struct.unpack_from("<H", leaf, 2)[0], out)
        return out

    def _sl_entries(self, bid: int, body: bytes, count: int, out: Dict[int, Tuple[int, int]]) -> None:
        lay = self.layout
        if lay.sub_header + count * lay.sl_entry_size > len(body):
            raise PstReadError(f"блок {bid:#x}: записей {count} больше, чем помещается")
        if len(out) + count > MAX_SUBNODES:
            raise PstReadError(f"у узла больше {MAX_SUBNODES} подузлов")
        for i in range(count):
            nid, bid_data, bid_sub = struct.unpack_from(lay.sl_entry, body, lay.sub_header + i * lay.sl_entry_size)
            out[nid & 0xFFFFFFFF] = (bid_data, bid_sub)


# ---------------------------------------------------------------------------
#  LTP: куча на узле, B-дерево в куче, контекст свойств, таблица
# ---------------------------------------------------------------------------
class Heap:
    """Куча на узле (HN): кусочки данных, адресуемые номером HID."""

    def __init__(self, ndb: Ndb, bid: int, limit: int = MAX_HEAP_BYTES) -> None:
        self.blocks: List[bytes] = []
        total = 0
        for blk in ndb.tree_blocks(bid):
            total += len(blk)
            if total > limit:
                raise PstReadError("куча узла больше допустимого")
            self.blocks.append(blk)
        self.size = total
        if not self.blocks or len(self.blocks[0]) < 12:
            raise PstReadError("пустая куча узла")
        _map_at, sig, self.client, self.root = struct.unpack_from("<HBBI", self.blocks[0], 0)
        if sig != 0xEC:
            raise PstReadError("узел не содержит кучу (нет подписи 0xEC)")
        self._maps: Dict[int, tuple] = {}

    def _map(self, index: int) -> tuple:
        offsets = self._maps.get(index)
        if offsets is None:
            blk = self.blocks[index]
            if len(blk) < 2:
                raise PstReadError("блок кучи пуст")
            at = struct.unpack_from("<H", blk, 0)[0]
            if at + 4 > len(blk):
                raise PstReadError("карта кучи за пределами блока")
            count = struct.unpack_from("<H", blk, at)[0]
            if at + 4 + 2 * (count + 1) > len(blk):
                raise PstReadError("карта кучи обрезана")
            offsets = struct.unpack_from(f"<{count + 1}H", blk, at + 4)
            self._maps[index] = offsets
        return offsets

    def get(self, hid: int) -> bytes:
        if hid & 0x1F:
            raise PstReadError(f"{hid:#x} — не номер в куче")
        block_index = hid >> 16
        index = (hid >> 5) & 0x7FF
        if block_index >= len(self.blocks) or index == 0:
            raise PstReadError(f"номера {hid:#x} нет в куче")
        offsets = self._map(block_index)
        if index >= len(offsets):
            raise PstReadError(f"номера {hid:#x} нет в куче")
        start, end = offsets[index - 1], offsets[index]
        if start > end or end > len(self.blocks[block_index]):
            raise PstReadError(f"кусок кучи {hid:#x} за пределами блока")
        return self.blocks[block_index][start:end]


def read_bth(heap: Heap, hid: int, max_records: int = MAX_BTH_RECORDS) -> List[Tuple[bytes, bytes]]:
    """B-дерево в куче: список (ключ, значение) по возрастанию ключа.

    Узел дерева, на который ссылаются дважды, — признак порчи: так несколько
    байт индекса раздувались бы в миллионы записей. Поэтому каждый узел читается
    один раз, а записей — не больше ``max_records``.
    """
    if not hid:
        return []
    header = heap.get(hid)
    if len(header) < 8 or header[0] != 0xB5:
        raise PstReadError("не заголовок B-дерева кучи")
    key_size, entry_size, levels = header[1], header[2], header[3]
    root = struct.unpack_from("<I", header, 4)[0]
    if key_size not in (2, 4, 8, 16) or not 1 <= entry_size <= 32 or levels > MAX_BTH_DEPTH:
        raise PstReadError("неверные размеры в заголовке B-дерева кучи")
    out: List[Tuple[bytes, bytes]] = []
    if not root:
        return out
    seen = set()
    stack: List[Tuple[int, int]] = [(root, levels)]
    while stack:
        node, level = stack.pop()
        if node in seen:
            raise PstReadError("B-дерево кучи ссылается на один узел дважды")
        seen.add(node)
        if len(seen) > max_records + 1024:
            raise PstReadError("B-дерево кучи слишком велико")
        data = heap.get(node)
        if level == 0:
            step = key_size + entry_size
            for at in range(0, len(data) - step + 1, step):
                out.append((data[at:at + key_size], data[at + key_size:at + step]))
            if len(out) > max_records:
                raise PstReadError("B-дерево кучи слишком велико")
            continue
        step = key_size + 4
        children = [struct.unpack_from("<I", data, at + key_size)[0] for at in range(0, len(data) - step + 1, step)]
        for child in reversed(children):            # в стек — в обратном порядке, чтобы ключи шли по возрастанию
            stack.append((child, level - 1))
    return out


def _decode_value(ptype: int, data: bytes, codec: str):
    """Байты значения → значение Python по типу свойства."""
    if ptype == PT_UNICODE:
        return data[:len(data) & ~1].decode("utf-16-le", "replace").rstrip("\x00")
    if ptype == PT_STRING8:
        return data.decode(codec or "cp1252", "replace").rstrip("\x00")
    if ptype in (PT_I8, PT_SYSTIME, PT_CURRENCY):
        return struct.unpack_from("<q", data)[0] if len(data) >= 8 else None
    if ptype in (PT_DOUBLE, PT_APPTIME):
        return struct.unpack_from("<d", data)[0] if len(data) >= 8 else None
    return data


def _decode_inline(ptype: int, raw: int):
    if ptype == PT_I2:
        value = raw & 0xFFFF
        return value - 0x10000 if value & 0x8000 else value
    if ptype in (PT_I4, PT_ERROR):
        return raw - 0x100000000 if raw & 0x80000000 else raw
    if ptype == PT_BOOL:
        return bool(raw & 0xFF)
    if ptype == PT_FLOAT:
        return struct.unpack("<f", struct.pack("<I", raw))[0]
    return raw


class PropertyContext:
    """Контекст свойств (PC): набор свойств одного объекта — хранилища, папки, письма, вложения."""

    def __init__(self, ndb: Ndb, bid_data: int, bid_sub: int, codec: str = "cp1252",
                 heap_limit: int = MAX_HEAP_BYTES) -> None:
        self.ndb = ndb
        self.codec = codec
        self.subs = ndb.subnodes(bid_sub)
        self.heap = Heap(ndb, bid_data, heap_limit)
        if self.heap.client != 0xBC:
            raise PstReadError("узел не содержит свойств (подпись кучи не 0xBC)")
        self._props: Dict[int, Tuple[int, int]] = {}
        for key, value in read_bth(self.heap, self.heap.root, MAX_PROPERTIES):
            if len(key) != 2 or len(value) < 6:
                raise PstReadError("неверная запись в наборе свойств")
            ptype, raw = struct.unpack_from("<HI", value, 0)
            self._props[struct.unpack("<H", key)[0]] = (ptype, raw)

    def __contains__(self, pid: int) -> bool:
        return pid in self._props

    def ids(self) -> List[int]:
        return list(self._props)

    def type_of(self, pid: int) -> Optional[int]:
        entry = self._props.get(pid)
        return entry[0] if entry else None

    def _raw(self, hnid: int, limit: int) -> bytes:
        if not hnid:
            return b""
        if hnid & 0x1F:
            if hnid not in self.subs:
                raise PstReadError(f"подузел значения {hnid:#x} не найден")
            return self.ndb.tree_bytes(self.subs[hnid][0], limit)
        return self.heap.get(hnid)

    def get(self, pid: int, default=None, *, limit: int = MAX_VALUE_BYTES):
        """Значение свойства или ``default``, если свойства нет."""
        entry = self._props.get(pid)
        if entry is None:
            return default
        ptype, raw = entry
        size = _FIXED.get(ptype)
        if size is not None and size <= 4:
            return _decode_inline(ptype, raw)
        if ptype & 0x1000:
            return default                    # многозначные свойства письму не нужны
        return _decode_value(ptype, self._raw(raw, limit), self.codec)

    def raw(self, pid: int, *, limit: int = MAX_VALUE_BYTES) -> bytes:
        """Байты значения как они лежат в файле (для строк ANSI — без перекодирования)."""
        entry = self._props.get(pid)
        if entry is None or _FIXED.get(entry[0]) is not None or entry[0] & 0x1000:
            return b""
        return self._raw(entry[1], limit)

    def text(self, pid: int, default: str = "", *, limit: int = MAX_VALUE_BYTES) -> str:
        value = self.get(pid, None, limit=limit)
        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray)) and self.type_of(pid) == PT_BINARY:
            return bytes(value).decode(self.codec or "cp1252", "replace")
        return default

    def number(self, pid: int, default: int = 0) -> int:
        value = self.get(pid, None)
        return int(value) if isinstance(value, (int, bool)) and not isinstance(value, float) else default

    def size(self, pid: int) -> int:
        """Размер значения в байтах, не читая его (для вложений)."""
        entry = self._props.get(pid)
        if entry is None:
            return 0
        ptype, raw = entry
        fixed = _FIXED.get(ptype)
        if fixed is not None:
            return fixed
        if not raw:
            return 0
        if raw & 0x1F:
            if raw not in self.subs:
                raise PstReadError(f"подузел значения {raw:#x} не найден")
            return self.ndb.tree_size(self.subs[raw][0])
        return len(self.heap.get(raw))

    def chunks(self, pid: int) -> Iterator[bytes]:
        """Значение свойства кусками — большое вложение целиком в память не читается."""
        entry = self._props.get(pid)
        if entry is None:
            return
        _ptype, raw = entry
        if not raw:
            return
        if raw & 0x1F:
            if raw not in self.subs:
                raise PstReadError(f"подузел значения {raw:#x} не найден")
            yield from self.ndb.tree_blocks(self.subs[raw][0])
        else:
            yield self.heap.get(raw)

    def object_ref(self, pid: int) -> Optional[Tuple[int, int]]:
        """Для свойства-объекта (вложенное письмо, объект OLE): (NID подузла, размер)."""
        entry = self._props.get(pid)
        if entry is None or entry[0] != PT_OBJECT or not entry[1]:
            return None
        data = self._raw(entry[1], 64)
        if len(data) < 8:
            return None
        return struct.unpack_from("<II", data, 0)


def read_table(ndb: Ndb, bid_data: int, bid_sub: int, codec: str = "cp1252",
               *, max_rows: int = MAX_TABLE_ROWS) -> List[Dict[int, object]]:
    """Таблица (TC) целиком: список строк «тег столбца → значение».

    Так читаются только таблицы получателей и вложений письма — они малы.
    Содержимое папок перечисляется по узлам (:meth:`PstReader.message_nids`).
    """
    subs = ndb.subnodes(bid_sub)
    heap = Heap(ndb, bid_data)
    if heap.client != 0x7C:
        raise PstReadError("узел не содержит таблицу (подпись кучи не 0x7C)")
    info = heap.get(heap.root)
    if len(info) < 22 or info[0] != 0x7C:
        raise PstReadError("неверный заголовок таблицы")
    n_cols = info[1]
    rgib = struct.unpack_from("<4H", info, 2)
    hid_index, hnid_rows = struct.unpack_from("<II", info, 10)
    if len(info) < 22 + 8 * n_cols:
        raise PstReadError("описание столбцов таблицы обрезано")
    row_size = rgib[3]
    if not row_size or rgib[2] > row_size or row_size > ndb.layout.block_max:
        raise PstReadError("неверный размер строки таблицы")
    cols = []
    for i in range(n_cols):
        tag, at, size, bit = struct.unpack_from("<IHBB", info, 22 + 8 * i)
        if at + size > rgib[2] or rgib[2] + bit // 8 >= row_size or size not in (1, 2, 4, 8, 16):
            continue                         # столбец за пределами строки — пропускаем его, а не таблицу
        cols.append((tag, at, size, bit))
    if not hnid_rows:
        return []
    if hnid_rows & 0x1F:
        if hnid_rows not in subs:
            raise PstReadError("строки таблицы не найдены")
        blocks = []
        total = 0
        for blk in ndb.tree_blocks(subs[hnid_rows][0]):
            total += len(blk)
            if total > MAX_TABLE_BYTES:
                raise PstReadError("таблица больше допустимого")
            blocks.append(blk)
        per_block = ndb.layout.block_max // row_size
    else:
        blocks = [heap.get(hnid_rows)]
        per_block = max(1, len(blocks[0]) // row_size)

    def row_at(index: int) -> Optional[bytes]:
        blk_no, in_blk = divmod(index, per_block)
        if blk_no >= len(blocks):
            return None
        row = blocks[blk_no][in_blk * row_size:(in_blk + 1) * row_size]
        return row if len(row) == row_size else None

    # Действующие строки перечислены в индексе (номер строки → её место): место
    # удалённой строки в матрице остаётся, но в индексе его уже нет.
    index_size = 4 if ndb.layout.unicode else 2
    places = sorted(int.from_bytes(value[:index_size], "little")
                    for _key, value in read_bth(heap, hid_index, max_rows))
    if not places:
        places = list(range(sum(len(b) // row_size for b in blocks)))
    if len(places) > max_rows:
        raise PstReadError(f"в таблице больше {max_rows} строк")
    out: List[Dict[int, object]] = []
    budget = MAX_TABLE_BYTES                     # на значения ячеек всей таблицы
    for place in places:
        row = row_at(place)
        if row is None:
            continue
        values: Dict[int, object] = {}
        for tag, at, size, bit in cols:
            if not row[rgib[2] + bit // 8] & (0x80 >> (bit % 8)):
                continue
            ptype = tag & 0xFFFF
            cell = row[at:at + size]
            fixed = _FIXED.get(ptype)
            if fixed is not None and fixed <= 4 and size >= fixed:
                values[tag] = _decode_inline(ptype, int.from_bytes(cell[:fixed], "little"))
            elif fixed is not None and fixed == size:
                values[tag] = _decode_value(ptype, cell, codec)
            elif size == 4 and not ptype & 0x1000:
                hnid = struct.unpack("<I", cell)[0]
                try:
                    if not hnid:
                        data = b""
                    elif hnid & 0x1F:
                        # имя или адрес длиннее предела — порча: много ячеек, ссылающихся
                        # на одно большое значение, съели бы память
                        data = ndb.tree_bytes(subs[hnid][0], MAX_CELL_BYTES) if hnid in subs else b""
                    else:
                        data = heap.get(hnid)
                except PstReadError:
                    continue                 # одна нечитаемая ячейка не теряет всю строку
                budget -= len(data)
                if budget < 0:
                    raise PstReadError("таблица больше допустимого")
                values[tag] = _decode_value(ptype, data, codec)
        out.append(values)
    return out


def table_row_ids(ndb: Ndb, bid_data: int) -> List[int]:
    """Номера строк таблицы (у таблиц папок это NID писем и подпапок) — без чтения самих строк."""
    heap = Heap(ndb, bid_data)
    if heap.client != 0x7C:
        raise PstReadError("узел не содержит таблицу")
    info = heap.get(heap.root)
    if len(info) < 22 or info[0] != 0x7C:
        raise PstReadError("неверный заголовок таблицы")
    hid_index = struct.unpack_from("<I", info, 10)[0]
    return [int.from_bytes(key[:4], "little") for key, _value in read_bth(heap, hid_index, MAX_NODES)]


# ---------------------------------------------------------------------------
#  Обмен сообщениями: хранилище, папки, письма, получатели, вложения
# ---------------------------------------------------------------------------
#: Кодовые страницы Windows → кодеки Python (остальные — «cpNNN», если такой кодек есть).
_CODEPAGE_CODECS = {
    65001: "utf-8", 65000: "utf-7", 1200: "utf-16-le", 1201: "utf-16-be", 20127: "ascii",
    20866: "koi8-r", 21866: "koi8-u", 10000: "mac-roman", 10007: "mac-cyrillic",
    28591: "iso-8859-1", 28592: "iso-8859-2", 28593: "iso-8859-3", 28594: "iso-8859-4",
    28595: "iso-8859-5", 28596: "iso-8859-6", 28597: "iso-8859-7", 28598: "iso-8859-8",
    28599: "iso-8859-9", 28603: "iso-8859-13", 28605: "iso-8859-15", 38598: "iso-8859-8",
    50220: "iso-2022-jp", 50221: "iso-2022-jp", 50222: "iso-2022-jp", 50225: "iso-2022-kr",
    51932: "euc-jp", 51949: "euc-kr", 52936: "hz", 54936: "gb18030", 936: "gbk", 950: "big5",
    932: "cp932", 949: "cp949", 874: "cp874",
}


def codepage_codec(codepage) -> Optional[str]:
    """Кодек Python для кодовой страницы Windows или ``None``, если такого нет."""
    import codecs
    try:
        number = int(codepage)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    name = _CODEPAGE_CODECS.get(number) or f"cp{number}"
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


class Folder:
    """Папка хранилища: её номер, имя и путь от корня видимых папок."""
    __slots__ = ("nid", "name", "path", "container_class", "count")

    def __init__(self, nid: int, name: str, path: Tuple[str, ...], container_class: str, count: int) -> None:
        self.nid = nid
        self.name = name
        self.path = path
        self.container_class = container_class
        self.count = count


class Attachment:
    """Вложение письма."""

    def __init__(self, reader: "PstReader", pc: PropertyContext, codec: str, depth: int,
                 message_subs: Dict[int, Tuple[int, int]]) -> None:
        self._reader = reader
        self.pc = pc
        self._codec = codec
        self._depth = depth
        self._message_subs = message_subs
        #: 1 — данные внутри, 5 — вложенное письмо, 6 — объект OLE, 2–4 и 7 — ссылка на файл
        self.method = pc.number(0x3705, 1)
        self.long_filename = pc.text(0x3707)
        self.filename = pc.text(0x3704)
        self.display_name = pc.text(0x3001)
        self.mime_type = pc.text(0x370E)
        self.content_id = pc.text(0x3712)
        self.content_location = pc.text(0x3713)
        self.hidden = bool(pc.get(0x7FFE, False))
        self.flags = pc.number(0x3714, 0)

    @property
    def is_embedded_message(self) -> bool:
        return self.method == 5 and self.pc.type_of(0x3701) == PT_OBJECT

    @property
    def has_data(self) -> bool:
        return self.pc.type_of(0x3701) == PT_BINARY

    def data_size(self) -> int:
        return self.pc.size(0x3701) if self.has_data else 0

    def chunks(self) -> Iterator[bytes]:
        if self.has_data:
            yield from self.pc.chunks(0x3701)

    def embedded(self) -> "Message":
        """Вложенное письмо (у вложения с ``method == 5``)."""
        ref = self.pc.object_ref(0x3701)
        if ref is None:
            raise PstReadError("у вложенного письма нет данных")
        nid = ref[0]
        # Вложенное письмо — подузел самого вложения; на случай файлов сторонних
        # программ ищем его и среди подузлов письма.
        entry = self.pc.subs.get(nid) or self._message_subs.get(nid)
        if entry is None:
            raise PstReadError(f"вложенное письмо {nid:#x} не найдено")
        return Message(self._reader, nid, entry[0], entry[1], self._depth + 1)

    def object_chunks(self) -> Iterator[bytes]:
        """Данные вложения-объекта (OLE) как есть — кусками."""
        ref = self.pc.object_ref(0x3701)
        if ref is None:
            return
        entry = self.pc.subs.get(ref[0]) or self._message_subs.get(ref[0])
        if entry is None:
            raise PstReadError(f"данные объекта {ref[0]:#x} не найдены")
        yield from self._reader.ndb.tree_blocks(entry[0])


class Message:
    """Письмо: свойства, получатели, вложения."""

    def __init__(self, reader: "PstReader", nid: int, bid_data: int, bid_sub: int, depth: int = 0) -> None:
        self._reader = reader
        self.nid = nid
        self.depth = depth
        self.pc = PropertyContext(reader.ndb, bid_data, bid_sub, reader.ansi_codec)
        # Строки ANSI-файла — в кодировке письма, если она в нём названа
        # (PidTagMessageCodepage). Кодировка тела «для Интернета» (0x3FDE) тут
        # не годится: письмо в KOI8-R хранит строки всё равно в кодировке Windows.
        self.codec = reader.ansi_codec
        if self.pc.type_of(0x3FFD) == PT_I4:
            codec = codepage_codec(self.pc.get(0x3FFD, None))
            if codec and not codec.startswith("utf-16"):
                self.codec = codec
        self.pc.codec = self.codec
        self.message_class = self.pc.text(0x001A)
        #: вложения, которые не удалось прочитать (заполняет :meth:`attachments`)
        self.attachment_errors: List[str] = []

    def recipients(self) -> List[Dict[int, object]]:
        entry = self.pc.subs.get(NID_RECIPIENT_TABLE)
        if entry is None:
            return []
        return read_table(self._reader.ndb, entry[0], entry[1], self.codec)

    def attachments(self) -> List[Attachment]:
        entry = self.pc.subs.get(NID_ATTACHMENT_TABLE)
        if entry is None:
            return []
        out: List[Attachment] = []
        self.attachment_errors = []
        rows = read_table(self._reader.ndb, entry[0], entry[1], self.codec, max_rows=MAX_ATTACHMENTS)
        heaps = 0
        for number, row in enumerate(rows, start=1):
            nid = row.get(0x67F20003)
            if not isinstance(nid, int):
                continue
            nid &= 0xFFFFFFFF
            # Одно нечитаемое вложение не должно отнимать у письма остальные.
            try:
                if nid not in self.pc.subs:
                    raise PstReadError(f"узел {nid:#x} не найден")
                bid_data, bid_sub = self.pc.subs[nid]
                pc = PropertyContext(self._reader.ndb, bid_data, bid_sub, self.codec, MAX_ATTACHMENT_HEAP)
                heaps += pc.heap.size
                if heaps > MAX_ATTACHMENTS_HEAP_TOTAL:
                    self.attachment_errors.append(f"вложения с {number}-го не прочитаны (их свойства "
                                                  f"занимают больше допустимого)")
                    break
                out.append(Attachment(self._reader, pc, self.codec, self.depth, self.pc.subs))
            except PstReadError as exc:
                self.attachment_errors.append(f"вложение {number} не прочитано ({exc})")
                if len(self.attachment_errors) >= MAX_ATTACHMENT_ERRORS:
                    self.attachment_errors.append("остальные вложения не читались: слишком много нечитаемых")
                    break
        return out


class PstReader:
    """Открытый файл .pst: папки и письма.

    ``ansi_codec`` — кодировка строк старого файла ANSI для писем, в которых
    она не названа (в Unicode-файле строки хранятся в UTF-16 и она не нужна).
    """

    def __init__(self, path: str, *, ansi_codec: str = "cp1251") -> None:
        import os
        self.path = path
        self.ansi_codec = ansi_codec or "cp1251"
        self.fh = open(path, "rb")
        try:
            self.ndb = Ndb(self.fh, os.fstat(self.fh.fileno()).st_size)
            self._index_nodes()
        except BaseException:
            self.fh.close()
            raise

    def close(self) -> None:
        self.fh.close()

    def __enter__(self) -> "PstReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- сведения о файле ----------------------------------------------------
    @property
    def unicode(self) -> bool:
        return self.ndb.layout.unicode

    @property
    def crypt(self) -> int:
        return self.ndb.crypt

    @property
    def damaged(self) -> int:
        return self.ndb.damaged

    def describe(self) -> str:
        kind = "Unicode (Outlook 2003 и новее)" if self.unicode else "ANSI (Outlook 97–2002)"
        if self.ndb.ost:
            kind = "автономная копия ящика Exchange (.ost), " + kind
        crypt = {CRYPT_NONE: "без кодирования", CRYPT_PERMUTE: "сжимаемое шифрование",
                 CRYPT_CYCLIC: "усиленное шифрование"}[self.crypt]
        return f"{kind}, {crypt}"

    def store_name(self) -> str:
        try:
            bid_data, bid_sub, _parent = self.ndb.node(NID_MESSAGE_STORE)
            return PropertyContext(self.ndb, bid_data, bid_sub, self.ansi_codec).text(0x3001)
        except PstReadError:
            return ""

    # -- папки и письма -------------------------------------------------------
    def _index_nodes(self) -> None:
        """Разложить узлы по родителям: подпапки и письма каждой папки."""
        self._subfolders: Dict[int, List[int]] = {}
        self._messages: Dict[int, array] = {}
        #: письма папок, найденные только по таблице содержимого (запасной путь)
        self._table_messages: Dict[int, List[int]] = {}
        for nid, parent in self.ndb.nodes():
            kind = nid & 0x1F
            if kind == NT_NORMAL_MESSAGE:
                bucket = self._messages.get(parent)
                if bucket is None:
                    bucket = self._messages[parent] = array("I")
                bucket.append(nid)
            elif kind == NT_NORMAL_FOLDER and parent != nid:
                self._subfolders.setdefault(parent, []).append(nid)

    def _table_ids(self, folder_nid: int, table_type: int, want_type: int) -> List[int]:
        """Номера из таблицы папки (подпапки или письма) — запасной путь, когда связей узлов нет."""
        nid = (folder_nid & ~0x1F) | table_type
        try:
            bid_data, _bid_sub, _parent = self.ndb.node(nid)
            if not bid_data:
                return []
            return [rid for rid in table_row_ids(self.ndb, bid_data)
                    if rid & 0x1F == want_type and self.ndb.has_node(rid)]
        except PstReadError:
            return []                        # таблица повреждена — остаёмся с тем, что есть

    def _folder_pc(self, nid: int) -> Optional[PropertyContext]:
        try:
            bid_data, bid_sub, _parent = self.ndb.node(nid)
            return PropertyContext(self.ndb, bid_data, bid_sub, self.ansi_codec, MAX_FOLDER_HEAP)
        except PstReadError:
            return None

    def root_nid(self) -> int:
        """Корень видимых папок («Top of Personal Folders»)."""
        try:
            bid_data, bid_sub, _parent = self.ndb.node(NID_MESSAGE_STORE)
            entry = PropertyContext(self.ndb, bid_data, bid_sub, self.ansi_codec).get(0x35E0, None)
            if isinstance(entry, (bytes, bytearray)) and len(entry) >= 24:
                nid = struct.unpack_from("<I", entry, 20)[0]
                if nid & 0x1F == NT_NORMAL_FOLDER and self.ndb.has_node(nid):
                    return nid
        except PstReadError:
            pass
        for nid in (NID_IPM_SUBTREE, NID_ROOT_FOLDER):
            if self.ndb.has_node(nid):
                return nid
        raise PstReadError("в файле нет корневой папки")

    def folders(self, check: Optional[Callable[[], None]] = None) -> List[Folder]:
        """Все папки с письмами — в порядке обхода дерева; у корня видимых папок путь пуст.

        Подпапки и письма находятся по связям узлов (у каждого узла записан
        родитель) — так их находит и Outlook, и таблицы папок для этого читать не
        нужно. Таблица читается, только если связей нет, а сама папка говорит, что
        в ней что-то есть: так пишут некоторые сторонние программы.
        ``check`` вызывается на каждой папке — им прерывают долгий обход.
        """
        out: List[Folder] = []
        seen = set()

        def visit(nid: int, parent_path: Optional[Tuple[str, ...]], depth: int) -> None:
            if nid in seen or depth > MAX_FOLDER_DEPTH:
                return
            if len(seen) >= MAX_FOLDERS:
                raise PstReadError(f"в файле больше {MAX_FOLDERS} папок")
            seen.add(nid)
            if check is not None:
                check()
            pc = self._folder_pc(nid)
            name = ((pc.text(0x3001) if pc is not None else "") or "").strip()
            container = (pc.text(0x3613) if pc is not None else "") or ""
            path = () if parent_path is None else parent_path + (name or f"Папка {nid:#x}",)
            if nid not in self._messages and pc is not None and pc.number(0x3602, 0) > 0:
                self._table_messages[nid] = self._table_ids(nid, NT_CONTENTS_TABLE, NT_NORMAL_MESSAGE)
            out.append(Folder(nid, name, path, container, len(self.message_nids(nid))))
            children = list(self._subfolders.get(nid, ()))
            if not children and pc is not None and pc.get(0x360A, False):
                children = [child for child in self._table_ids(nid, NT_HIERARCHY_TABLE, NT_NORMAL_FOLDER)
                            if child != nid]
            for child in sorted(children):
                visit(child, path, depth + 1)

        visit(self.root_nid(), None, 0)
        return out

    def message_nids(self, folder_nid: int):
        """Номера писем папки (после :meth:`folders`)."""
        found = self._messages.get(folder_nid)
        if found is not None:
            return found
        return self._table_messages.get(folder_nid, ())

    def open_message(self, nid: int) -> Message:
        bid_data, bid_sub, _parent = self.ndb.node(nid)
        return Message(self, nid, bid_data, bid_sub)
