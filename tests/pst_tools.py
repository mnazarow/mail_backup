"""Инструменты для тестов чтения .pst.

Писатель службы создаёт только Unicode-файлы без «шифрования», а читать надо и
то, что создаёт Outlook. Здесь из файла писателя получаются другие виды того же
файла:

* :func:`encrypt_copy` — данные перекодированы перестановочным или циклическим
  способом ([MS-PST] 5.1, 5.2), как их хранит Outlook;
* :func:`to_ansi` — файл переложен в формат ANSI (Outlook 97–2002) на уровне
  блоков и B-деревьев: номера блоков и смещения по 4 байта, свои размеры
  страниц и трейлеров. Содержимое куч (свойства, таблицы) остаётся как было —
  строки в UTF-16; настоящие строки ANSI проверяются отдельным тестом;
* :func:`to_4k` — файл переложен в формат с большими страницами (``.ost``
  Outlook 2013 и новее, wVer 36): страницы по 4 КБ, блоки до 64 КБ, сжатие
  zlib. Здесь перекладываются и кучи: в этом формате иначе делится номер HID,
  а блоки кучи и строк таблицы крупнее. :class:`Pst4k` — строгий разбор
  нижнего слоя такого файла, им проверяется и сам результат.

Файл разбирается независимым строгим читателем :mod:`pst_reader`, а не тем,
который проверяется.
"""
from __future__ import annotations

import struct
import zlib
from collections import OrderedDict

from pst_reader import FIXED, Pst, PstError, crc32_pst, sig

from mailarchiver.pstimport import reader as rd


def reference_cyclic(data: bytes, key: int) -> bytes:
    """Циклическое кодирование дословно по псевдокоду спецификации — цикл по байтам."""
    r, s, i = rd._MPBB_R, rd._MPBB_S, rd._MPBB_I  # noqa: SLF001
    out = bytearray(len(data))
    w = (key ^ (key >> 16)) & 0xFFFF
    for n, b in enumerate(data):
        b = (b + (w & 0xFF)) & 0xFF
        b = r[b]
        b = (b + (w >> 8)) & 0xFF
        b = s[b]
        b = (b - (w >> 8)) & 0xFF
        b = i[b]
        b = (b - (w & 0xFF)) & 0xFF
        out[n] = b
        w = (w + 1) & 0xFFFF
    return bytes(out)


def _encode(data: bytes, bid: int, method: int) -> bytes:
    if method == 1:
        return rd.encode_permute(data)
    if method == 2:
        return reference_cyclic(data, bid & 0xFFFFFFFF)
    return data


def encrypt_copy(src: str, dst: str, method: int) -> None:
    """Копия Unicode-файла с данными, перекодированными способом ``method`` (1 или 2)."""
    pst = Pst(src)
    buf = bytearray(pst.data)
    for bid, (ib, cb, _cref) in pst.bbt.items():
        if bid & 2:
            continue                                   # служебные блоки не кодируются
        body = _encode(bytes(buf[ib:ib + cb]), bid, method)
        buf[ib:ib + cb] = body
        total = (cb + 16 + 63) & ~63
        struct.pack_into("<I", buf, ib + total - 12, crc32_pst(body))
    buf[0x201] = method
    struct.pack_into("<I", buf, 4, crc32_pst(bytes(buf[8:8 + 471])))
    struct.pack_into("<I", buf, 0x20C, crc32_pst(bytes(buf[8:8 + 516])))
    with open(dst, "wb") as fh:
        fh.write(buf)


def _convert_internal(body: bytes) -> bytes:
    """Служебный блок Unicode → ANSI: номера блоков и узлов по 4 байта."""
    kind, level, count = body[0], body[1], struct.unpack_from("<H", body, 2)[0]
    if kind == 1:                                      # XBLOCK / XXBLOCK
        total = struct.unpack_from("<I", body, 4)[0]
        bids = struct.unpack_from(f"<{count}Q", body, 8)
        return struct.pack(f"<BBHI{count}I", 1, level, count, total, *bids)
    if kind == 2 and level == 0:                       # SLBLOCK
        out = bytearray(struct.pack("<BBH", 2, 0, count))
        for i in range(count):
            out += struct.pack("<III", *struct.unpack_from("<QQQ", body, 8 + 24 * i))
        return bytes(out)
    if kind == 2:                                      # SIBLOCK
        out = bytearray(struct.pack("<BBH", 2, level, count))
        for i in range(count):
            out += struct.pack("<II", *struct.unpack_from("<QQ", body, 8 + 16 * i))
        return bytes(out)
    raise ValueError(f"неизвестный служебный блок {kind}")


def to_ansi(src: str, dst: str, method: int = 0) -> None:
    """Переложить Unicode-файл писателя в формат ANSI (при ``method`` — ещё и перекодировать данные)."""
    pst = Pst(src)
    out = bytearray(0x4600)                            # заголовок и место под карты — как в настоящем файле
    blocks = []
    for bid, (_ib, _cb, cref) in pst.bbt.items():
        body, internal = pst.block(bid)
        body = _convert_internal(body) if internal else _encode(body, bid, method)
        pos = len(out)
        total = (len(body) + 12 + 63) & ~63
        out += body + bytes(total - len(body) - 12)
        out += struct.pack("<HHII", len(body), sig(pos, bid), bid, crc32_pst(body))
        blocks.append((bid, struct.pack("<IIHH", bid, pos, len(body), cref)))
    nodes = [(nid, struct.pack("<IIII", nid, bd, bs, parent)) for nid, (bd, bs, parent) in pst.nbt.items()]
    next_page = [1]

    def page(ptype: int, level: int, entries, entry_size: int):
        """Записать страницу B-дерева. → (первый ключ, bid, смещение)."""
        out.extend(bytes(-len(out) % 512))             # страницы выровнены на 512 байт
        pos = len(out)
        bid = next_page[0]
        next_page[0] += 1
        body = b"".join(packed for _key, packed in entries).ljust(496, b"\x00")
        body += struct.pack("<BBBB", len(entries), 496 // entry_size, entry_size, level)
        out.extend(body + struct.pack("<BBHII", ptype, ptype, sig(pos, bid), bid, crc32_pst(body)))
        return entries[0][0], bid, pos

    def tree(ptype: int, leaves, entry_size: int):
        per_page = 496 // entry_size
        level = 0
        layer = [page(ptype, 0, leaves[i:i + per_page], entry_size) for i in range(0, len(leaves), per_page)]
        while len(layer) > 1:
            level += 1
            entries = [(key, struct.pack("<III", key, bid, pos)) for key, bid, pos in layer]
            layer = [page(ptype, level, entries[i:i + 41], 12) for i in range(0, len(entries), 41)]
        return layer[0]

    _key, nbt_bid, nbt_ib = tree(0x81, nodes, 16)
    _key, bbt_bid, bbt_ib = tree(0x80, blocks, 12)
    struct.pack_into("<IIHHHBB", out, 0, 0x4E444221, 0, 0x4D53, 14, 19, 1, 1)
    struct.pack_into("<III", out, 24, max(pst.bbt) + 4, next_page[0], pst.unique)
    struct.pack_into("<32I", out, 36, *pst.rgnid)
    struct.pack_into("<IIIIIIIIIBBH", out, 0xA4, 0, len(out), 0x4400, 0, 0,
                     nbt_bid, nbt_ib, bbt_bid, bbt_ib, 2, 0, 0)
    out[0x1CC] = 0x80
    out[0x1CD] = method
    struct.pack_into("<I", out, 4, crc32_pst(bytes(out[8:8 + 471])))
    with open(dst, "wb") as fh:
        fh.write(out)


# ---------------------------------------------------------------------------
#  Сжатый RTF
# ---------------------------------------------------------------------------
def compress_rtf(data: bytes) -> bytes:
    """Сжать RTF по [MS-OXRTFCP] — жадно, со словарём, как это делает Outlook."""
    from mailarchiver.pstimport import rtf
    window = bytearray(4096)
    prebuf = rtf._PREBUF  # noqa: SLF001
    window[:len(prebuf)] = prebuf
    wpos = filled = len(prebuf)
    tokens = []
    pos = 0
    while pos < len(data):
        best_len, best_off = 0, 0
        limit = min(17, len(data) - pos)
        if limit >= 2:
            first = data[pos]
            for off in range(filled):
                if off == wpos or window[off] != first:
                    continue                           # ссылка на место записи — признак конца
                length, o = 0, off
                # до места записи не дочитываем: так копия при распаковке не перекрывается с записью
                while length < limit and o != wpos and window[o] == data[pos + length]:
                    o = (o + 1) & 0xFFF
                    length += 1
                if length > best_len:
                    best_len, best_off = length, off
                    if length == limit:
                        break
        if best_len >= 2:
            tokens.append((True, struct.pack(">H", (best_off << 4) | (best_len - 2))))
            step = best_len
        else:
            tokens.append((False, data[pos:pos + 1]))
            step = 1
        for i in range(step):
            window[wpos] = data[pos + i]
            wpos = (wpos + 1) & 0xFFF
        filled = min(4096, filled + step)
        pos += step
    tokens.append((True, struct.pack(">H", wpos << 4)))                # конец данных
    body = bytearray()
    for start in range(0, len(tokens), 8):
        group = tokens[start:start + 8]
        body.append(sum(1 << bit for bit, (is_ref, _raw) in enumerate(group) if is_ref))
        for _is_ref, raw in group:
            body += raw
    crc = crc32_pst(bytes(body))
    return struct.pack("<IIII", len(body) + 12, len(data), 0x75465A4C, crc) + bytes(body)


# ---------------------------------------------------------------------------
#  Формат с большими страницами (wVer 36)
# ---------------------------------------------------------------------------
# В [MS-PST] его нет. Раскладка ниже снята с настоящих файлов .ost Outlook 2013/2016
# (страницы, записи BBT, трейлеры блоков, сжатие, размеры блоков данных и строк таблиц,
# уровни заполнения кучи): :class:`Pst4k` разбирает их без единого замечания. Не из
# файлов, а из описаний формата взято то, чего в тех файлах не было: деление номера
# HID (14 бит на номер в блоке, 13 на номер блока), куча длиннее одного блока,
# XXBLOCK, SIBLOCK и сжатие вместе с кодированием данных. Это настоящими файлами НЕ
# подтверждено: читатель написан по тем же описаниям, и тест, где файл из to_4k
# читается верно, показывает согласие двух наших программ, а не верность описаний.
# Деление HID 14/13 — как в libpff; java-libpst, pst-extractor и go-pst делят 11/16
# во всех видах файла. В образцах кучи — из одного блока, кусочков в блоке — до 1703,
# и на них оба деления неразличимы.
PAGE_4K = 4096
PAGE_4K_ENTRIES = 4056               # под записи; дальше счётчики, заполнение и трейлер
PAGE_4K_TRAILER = 4072
BLOCK_4K = 65536 - 24                # данных в блоке
HID_4K_INDEX_BITS = 14
HEAP_MAX_ALLOC = 3580                # в образцах кусочков кучи крупнее тоже нет


def _crc(data) -> int:
    return zlib.crc32(data, 0xFFFFFFFF) ^ 0xFFFFFFFF


def _block_span(cb: int) -> int:
    """Сколько места в файле занимает блок с ``cb`` байтами данных: с трейлером, до 512."""
    return (cb + 24 + 511) & ~511


class _Source(Pst):
    """Исходный файл для :func:`to_4k`. Суммы блоков сверяются, как и в :class:`Pst`, но
    быстрой функцией: побайтный счёт на вложениях в мегабайты занял бы минуты."""

    def block(self, bid: int):
        key = bid & ~1
        if key not in self.bbt:
            raise PstError(f"блока {bid:#x} нет в BBT")
        ib, cb, _cref = self.bbt[key]
        size = (cb + 16 + 63) & ~63
        t_cb, t_sig, t_crc, t_bid = struct.unpack_from("<HHIQ", self.data, ib + size - 16)
        body = self.data[ib:ib + cb]
        if (t_cb, t_sig, t_crc, t_bid) != (cb, sig(ib, key), _crc(body), key):
            raise PstError(f"блок {bid:#x}: трейлер")
        return body, bool(key & 2)


class Pst4k:
    """Строгий разбор слоя NDB файла с большими страницами — отдельно от проверяемого читателя.

    Всё, что не сходится с раскладкой настоящих файлов, попадает в ``problems``.
    ``bbt``: BID → (смещение, размер в файле, размер после распаковки, счётчик ссылок).
    """

    def __init__(self, path: str) -> None:
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        self.problems = []
        magic, crc_partial, self.client, self.ver = struct.unpack_from("<IIHH", d, 0)
        if magic != 0x4E444221 or self.ver != 36:
            raise PstError("не файл с большими страницами")
        root = struct.unpack_from("<IQQQQQQQQ", d, 0xB4)
        self.crypt = d[0x201]
        if d[0x200] != 0x80 or _crc(d[8:8 + 471]) != crc_partial or \
                _crc(d[8:8 + 516]) != struct.unpack_from("<I", d, 0x20C)[0]:
            self.problems.append("заголовок: bSentinel или суммы")
        if root[1] != len(d):
            self.problems.append("ibFileEof не равен размеру файла")
        self.nbt = OrderedDict()
        self.bbt = OrderedDict()
        self.pages = []
        self._walk(root[6], root[5], 0x81, None)
        self._walk(root[8], root[7], 0x80, None)

    def _walk(self, ib: int, bid: int, ptype: int, level_expect):
        d = self.data
        if ib % PAGE_4K or ib + PAGE_4K > len(d):
            raise PstError(f"страница {ib:#x} вне файла или не выровнена")
        page = d[ib:ib + PAGE_4K]
        count, count_max, entry, level = struct.unpack_from("<HHBB", page, PAGE_4K_ENTRIES)
        pt, repeat, wsig, crc, pbid, tail = struct.unpack_from("<BBHIQQ", page, PAGE_4K_TRAILER)
        if (pt, repeat, wsig, crc, pbid) != (ptype, ptype, sig(ib, bid), _crc(page[:PAGE_4K_TRAILER]), bid):
            self.problems.append(f"страница {ib:#x}: трейлер")
        if tail != 1 or any(page[PAGE_4K_ENTRIES + 6:PAGE_4K_TRAILER]):
            self.problems.append(f"страница {ib:#x}: хвост трейлера или заполнение")
        want = 24 if level or ptype == 0x80 else 32
        if entry != want or count_max != PAGE_4K_ENTRIES // entry or not 0 < count <= count_max:
            self.problems.append(f"страница {ib:#x}: записей {count} из {count_max} по {entry}")
        if level_expect is not None and level != level_expect:
            self.problems.append(f"страница {ib:#x}: уровень {level}")
        self.pages.append((ib, ptype, level))
        keys = []
        for i in range(count):
            at = i * entry
            if level:
                key, child_bid, child_ib = struct.unpack_from("<QQQ", page, at)
                if self._walk(child_ib, child_bid, ptype, level - 1) != key:
                    self.problems.append(f"страница {ib:#x}: ключ не равен первому ключу потомка")
            elif ptype == 0x81:
                key, bid_data, bid_sub, parent, _pad = struct.unpack_from("<QQQII", page, at)
                self.nbt[key] = (bid_data, bid_sub, parent)
            else:
                # последние два байта записи — не заполнение: в настоящих файлах там 0 или 1
                key, block_ib, cb, inflated, cref, _unknown = struct.unpack_from("<QQHHHH", page, at)
                if key & 1 or block_ib % 512 or cb > inflated or inflated > BLOCK_4K:
                    self.problems.append(f"блок {key:#x}: запись BBT")
                self.bbt[key] = (block_ib, cb, inflated, cref)
            keys.append(key)
        if keys != sorted(set(keys)):
            self.problems.append(f"страница {ib:#x}: ключи не по возрастанию")
        return keys[0] if keys else None

    def stored(self, bid: int) -> bytes:
        """Байты блока, как они лежат в файле (сжатые и закодированные), с проверкой трейлера."""
        ib, cb, inflated, _cref = self.bbt[bid & ~1]
        end = ib + _block_span(cb)
        body = self.data[ib:ib + cb]
        trailer = struct.unpack_from("<HHIQHHI", self.data, end - 24)
        if trailer != (cb, sig(ib, bid & ~1), _crc(body), bid & ~1, 2, inflated, 0):
            self.problems.append(f"блок {bid:#x}: трейлер")
        # сжимаются и данные, и служебные блоки — но только когда блок от этого занимает в
        # файле меньше места (места меряются по 512 байт)
        if cb != inflated and _block_span(cb) >= _block_span(inflated):
            self.problems.append(f"блок {bid:#x}: сжатие без выгоды")
        return body

    def block(self, bid: int) -> bytes:
        """Данные блока. Сжатый поток — zlib (78 9C); в закодированном файле он лежит либо как
        есть (данные закодированы до сжатия), либо закодированным (:func:`to_4k` с ``reverse``)."""
        body = self.stored(bid)
        inflated = self.bbt[bid & ~1][2]
        method = 0 if bid & 2 else self.crypt
        if len(body) == inflated:
            return _decode(body, bid, method)
        if body[:2] == b"\x78\x9c":
            body = _decode(zlib.decompress(body), bid, method)
        elif method and _decode(body[:2], bid, method) == b"\x78\x9c":
            body = zlib.decompress(_decode(body, bid, method))
        else:
            raise PstError(f"блок {bid:#x}: сжатый поток не zlib")
        if len(body) != inflated:
            self.problems.append(f"блок {bid:#x}: после распаковки {len(body)} байт, не {inflated}")
        return body

    def blocks(self, bid: int, full: bool = True):
        """Блоки дерева данных. ``full`` — все, кроме последнего, должны быть полными (так у потоков)."""
        if not bid:
            return []
        if not bid & 2:
            return [self.block(bid)]
        top = self.block(bid)
        kind, level, count, total = struct.unpack_from("<BBHI", top, 0)
        if kind != 1 or level not in (1, 2) or len(top) != 8 + 8 * count:
            raise PstError(f"блок {bid:#x}: не XBLOCK")
        out = []
        for child in struct.unpack_from(f"<{count}Q", top, 8):
            if bool(child & 2) != (level == 2):
                self.problems.append(f"блок {bid:#x}: ссылка не того вида")
            out += self.blocks(child, full) if level == 2 else [self.block(child)]
        if sum(len(b) for b in out) != total or (full and any(len(b) != BLOCK_4K for b in out[:-1])):
            self.problems.append(f"блок {bid:#x}: lcbTotal или неполный блок в середине")
        return out

    def subnodes(self, bid: int):
        out = OrderedDict()
        if not bid:
            return out
        top = self.block(bid)
        kind, level, count, pad = struct.unpack_from("<BBHI", top, 0)
        if kind != 2 or level not in (0, 1) or pad or len(top) != 8 + (16 if level else 24) * count:
            raise PstError(f"блок {bid:#x}: не дерево подузлов")
        for i in range(count):
            if level:
                out.update(self.subnodes(struct.unpack_from("<QQ", top, 8 + 16 * i)[1]))
            else:
                # номер узла — младшие 4 байта; в старших в настоящих файлах бывает мусор
                nid, _junk, bid_data, bid_sub = struct.unpack_from("<IIQQ", top, 8 + 24 * i)
                out[nid] = (bid_data, bid_sub)
        if list(out) != sorted(out):
            self.problems.append(f"блок {bid:#x}: подузлы не по возрастанию")
        return out

    def heap(self, bid: int):
        """Куча узла: {HID: кусочек}. Проверяется устройство каждого блока кучи."""
        allocs = OrderedDict()
        blocks = self.blocks(bid, full=False)
        for index, blk in enumerate(blocks):
            at = struct.unpack_from("<H", blk, 0)[0]
            count, _free = struct.unpack_from("<HH", blk, at)
            offsets = struct.unpack_from(f"<{count + 1}H", blk, at + 4)
            start = 12 if index == 0 else (66 if index % 128 == 8 else 2)
            if offsets[0] != start or at != (offsets[-1] + 1) & ~1 or at + 4 + 2 * (count + 1) != len(blk) \
                    or list(offsets) != sorted(offsets) or count >= 1 << HID_4K_INDEX_BITS:
                self.problems.append(f"куча {bid:#x}, блок {index}: карта кусочков")
            if index == 0 and (blk[2] != 0xEC or blk[8] & 15 != _fill_4k(len(blk))):
                self.problems.append(f"куча {bid:#x}: заголовок")
            for i in range(count):
                if offsets[i + 1] - offsets[i] > HEAP_MAX_ALLOC:
                    self.problems.append(f"куча {bid:#x}: кусочек больше {HEAP_MAX_ALLOC}")
                allocs[(index << (5 + HID_4K_INDEX_BITS)) | ((i + 1) << 5)] = blk[offsets[i]:offsets[i + 1]]
        return allocs

    def check(self) -> list:
        """Пройти все узлы с подузлами: блоки, деревья данных и подузлов, кучи. → замечания."""
        seen = set()

        def node(nid: int, bid_data: int, bid_sub: int) -> None:
            key = (bid_data, bid_sub)
            if key in seen:
                return
            seen.add(key)
            first = self.blocks(bid_data, full=False)[:1] if bid_data else []
            is_heap = nid & 0x1F != 0x1F and first and len(first[0]) >= 12 and first[0][2] == 0xEC
            if is_heap:
                self.heap(bid_data)
            elif bid_data:
                self.blocks(bid_data)                  # поток: все блоки, кроме последнего, полные
            for sub, (sub_data, sub_sub) in self.subnodes(bid_sub).items():
                node(sub, sub_data, sub_sub)

        for nid, (bid_data, bid_sub, _parent) in self.nbt.items():
            try:
                node(nid, bid_data, bid_sub)
            except (PstError, struct.error, zlib.error, KeyError) as exc:
                self.problems.append(f"узел {nid:#x}: {type(exc).__name__}: {exc}")
        return self.problems


def _decode(data: bytes, bid: int, method: int) -> bytes:
    if method == 1:
        return rd.decode_permute(data)
    return reference_cyclic(data, bid & 0xFFFFFFFF) if method == 2 else data


def _fill_4k(used: int) -> int:
    """Уровень заполнения блока кучи. В настоящих файлах он растёт равными шагами: это
    занятая доля блока в пятнадцатых (в обычном формате — ступени по числу свободных байт)."""
    return min(15, used * 15 // BLOCK_4K)


class _Heap4k:
    """Куча на узле в раскладке больших блоков: кусочки те же, блоки — до 64 КБ, номера HID свои."""

    def __init__(self, client: int) -> None:
        self.client = client
        self.root = 0
        self._blocks = [[]]
        self._used = [12]

    def alloc(self, data: bytes) -> int:
        index = len(self._blocks) - 1
        count = len(self._blocks[index])
        need = ((self._used[index] + len(data) + 1) & ~1) + 4 + 2 * (count + 2)
        if need > BLOCK_4K or count + 1 >= 1 << HID_4K_INDEX_BITS:
            self._blocks.append([])
            index += 1
            self._used.append(66 if index % 128 == 8 else 2)
            count = 0
        self._blocks[index].append(bytes(data))
        self._used[index] += len(data)
        return (index << (5 + HID_4K_INDEX_BITS)) | ((count + 1) << 5)

    def set(self, hid: int, data: bytes) -> None:
        index, slot = hid >> (5 + HID_4K_INDEX_BITS), ((hid >> 5) & ((1 << HID_4K_INDEX_BITS) - 1)) - 1
        assert len(self._blocks[index][slot]) == len(data)
        self._blocks[index][slot] = bytes(data)

    def build(self) -> list:
        out = []
        for index, allocations in enumerate(self._blocks):
            body = bytearray(12 if index == 0 else (66 if index % 128 == 8 else 2))
            offsets = [len(body)]
            for item in allocations:
                body += item
                offsets.append(len(body))
            body += bytes(len(body) % 2)
            struct.pack_into("<H", body, 0, len(body))
            out.append(body + struct.pack(f"<HH{len(offsets)}H", len(allocations), 0, *offsets))
        levels = [_fill_4k(len(b)) for b in out]

        def fill(part, count):
            packed = bytearray(count // 2)
            for i, level in enumerate(part[:count]):
                packed[i // 2] |= level << (4 * (i % 2))
            return bytes(packed)

        struct.pack_into("<BBI4s", out[0], 2, 0xEC, self.client, self.root, fill(levels, 8))
        for index in range(8, len(out), 128):          # карта уровней — как в обычном формате
            struct.pack_into("<64s", out[index], 2, fill(levels[index:], 128))
        return [bytes(b) for b in out]


class _Ndb4k:
    """Писатель слоя NDB в раскладке больших страниц."""

    def __init__(self, compress: bool, method: int, reverse: bool, fanout) -> None:
        self.compress, self.method, self.reverse = compress, method, reverse
        self.xblock_max = fanout or (BLOCK_4K - 8) // 8
        self.slblock_max = fanout or (BLOCK_4K - 8) // 24
        self.out = bytearray(0x25000)                  # первый блок настоящего файла лежит здесь же
        self.next_bid = 4
        self.next_page = 1
        self.bbt = []

    def write_block(self, data: bytes, internal: bool = False) -> int:
        assert len(data) <= BLOCK_4K
        bid = self.next_bid | (2 if internal else 0)
        self.next_bid += 4
        # Кодируются только блоки данных, сжимаются любые: сначала кодирование, потом сжатие
        # (reverse — наоборот). Сжатым блок остаётся, только если так он занимает в файле
        # меньше места, — как в настоящих файлах.
        method = 0 if internal else self.method
        body = _encode(bytes(data), bid, method)
        packed = zlib.compress(bytes(data) if self.reverse else body)
        if self.compress and _block_span(len(packed)) < _block_span(len(body)):
            body = _encode(packed, bid, method) if self.reverse else packed
        ib = len(self.out)
        self.out += body + bytes(_block_span(len(body)) - len(body) - 24)
        self.out += struct.pack("<HHIQHHI", len(body), sig(ib, bid), _crc(body), bid, 2, len(data), 0)
        self.bbt.append((bid, struct.pack("<QQHHHH", bid, ib, len(body), len(data), 2, 0)))
        return bid

    def write_blocks(self, blocks) -> int:
        """Дерево данных над готовыми блоками: один блок, XBLOCK или XXBLOCK."""
        if not blocks:
            return 0
        layer = [(self.write_block(b), len(b)) for b in blocks]
        level = 0
        while len(layer) > 1:
            level += 1
            assert level <= 2, "значение не помещается в XXBLOCK"
            groups = [layer[i:i + self.xblock_max] for i in range(0, len(layer), self.xblock_max)]
            layer = [(self.write_block(struct.pack(f"<BBHI{len(g)}Q", 1, level, len(g), sum(t for _b, t in g),
                                                   *[b for b, _t in g]), True), sum(t for _b, t in g))
                     for g in groups]
        return layer[0][0]

    def write_stream(self, data: bytes) -> int:
        return self.write_blocks([data[i:i + BLOCK_4K] for i in range(0, len(data), BLOCK_4K)] or [b""])

    def write_subnodes(self, entries) -> int:
        items = sorted(entries)
        if not items:
            return 0
        leaves = []
        for start in range(0, len(items), self.slblock_max):
            part = items[start:start + self.slblock_max]
            body = struct.pack("<BBHI", 2, 0, len(part), 0) + b"".join(struct.pack("<QQQ", *item) for item in part)
            leaves.append((part[0][0], self.write_block(body, True)))
        if len(leaves) == 1:
            return leaves[0][1]
        return self.write_block(struct.pack("<BBHI", 2, 1, len(leaves), 0)
                                + b"".join(struct.pack("<QQ", *leaf) for leaf in leaves), True)

    def _page(self, ptype: int, level: int, entries, entry_size: int):
        self.out += bytes(-len(self.out) % PAGE_4K)
        ib, bid = len(self.out), self.next_page
        self.next_page += 1
        body = b"".join(packed for _key, packed in entries).ljust(PAGE_4K_ENTRIES, b"\x00")
        body += struct.pack("<HHBB10x", len(entries), PAGE_4K_ENTRIES // entry_size, entry_size, level)
        self.out += body + struct.pack("<BBHIQQ", ptype, ptype, sig(ib, bid), _crc(body), bid, 1)
        return entries[0][0], bid, ib

    def tree(self, ptype: int, leaves, entry_size: int):
        per_page = PAGE_4K_ENTRIES // entry_size
        level = 0
        layer = [self._page(ptype, 0, leaves[i:i + per_page], entry_size) for i in range(0, len(leaves), per_page)]
        while len(layer) > 1:
            level += 1
            entries = [(key, struct.pack("<QQQ", key, bid, ib)) for key, bid, ib in layer]
            layer = [self._page(ptype, level, entries[i:i + 169], 24) for i in range(0, len(entries), 169)]
        return layer[0]


class _Ltp4k:
    """Перекладка узлов: кучи пересобираются в большие блоки, номера HID в них пересчитываются."""

    def __init__(self, pst: Pst, out: _Ndb4k) -> None:
        self.pst, self.out = pst, out

    def _heap(self, bid: int):
        old = self.pst.heap(bid)
        new = _Heap4k(old["client"])
        hids = {0: 0}
        for hid, data in old["allocs"].items():       # по порядку: блок за блоком, кусочек за кусочком
            hids[hid] = new.alloc(data)
        new.root = hids[old["root"]]
        return old["allocs"], old["root"], new, hids

    @staticmethod
    def _bth(allocs, new: _Heap4k, hids, hid: int, leaf=None) -> None:
        """B-дерево кучи: поправить ссылки на узлы; ``leaf`` правит ссылки в записях листа."""
        if not hid:
            return
        header = allocs[hid]
        _kind, key_size, _entry_size, levels, root = struct.unpack_from("<BBBBI", header, 0)
        new.set(hids[hid], header[:4] + struct.pack("<I", hids[root]))

        def walk(node: int, level: int) -> None:
            data = bytearray(allocs[node])
            if level:
                for at in range(key_size, len(data), key_size + 4):
                    child = struct.unpack_from("<I", data, at)[0]
                    struct.pack_into("<I", data, at, hids[child])
                    walk(child, level - 1)
            elif leaf is not None:
                leaf(data)
            new.set(hids[node], data)

        if root:
            walk(root, levels)

    def node(self, bid_data: int, bid_sub: int):
        """Узел с кучей — набор свойств или таблица, по подписи кучи."""
        if not bid_data:
            assert not bid_sub
            return 0, 0
        client = self.pst.blocks(bid_data)[0][3]
        return self.pc(bid_data, bid_sub) if client == 0xBC else self.tc(bid_data, bid_sub)

    def _sub(self, nid: int, bid_data: int, bid_sub: int):
        kind = nid & 0x1F
        if kind in (0x04, 0x05):                       # вложенное письмо, вложение
            return self.pc(bid_data, bid_sub)
        if kind in (0x11, 0x12):                       # таблицы вложений и получателей
            return self.tc(bid_data, bid_sub)
        assert kind == 0x1F and not bid_sub            # большое значение — поток в блоках по 64 КБ
        return self.out.write_stream(self.pst.stream(bid_data)), 0

    def pc(self, bid_data: int, bid_sub: int):
        allocs, root, new, hids = self._heap(bid_data)

        def leaf(data: bytearray) -> None:
            for at in range(0, len(data), 8):
                ptype, value = struct.unpack_from("<HI", data, at + 2)
                if FIXED.get(ptype, 8) > 4 and value and not value & 0x1F:     # значение лежит в куче
                    struct.pack_into("<I", data, at + 4, hids[value])

        self._bth(allocs, new, hids, root, leaf)
        subs = [(nid, *self._sub(nid, *entry)) for nid, entry in self.pst.subnodes(bid_sub).items()]
        return self.out.write_blocks(new.build()), self.out.write_subnodes(subs)

    def tc(self, bid_data: int, bid_sub: int):
        allocs, root, new, hids = self._heap(bid_data)
        info = allocs[root]
        rgib = struct.unpack_from("<4H", info, 2)
        hid_index, hnid_rows, hid_extra = struct.unpack_from("<III", info, 10)
        row_size = rgib[3]
        cols = [struct.unpack_from("<IHBB", info, 22 + 8 * i) for i in range(info[1])]
        # ячейки-ссылки: всё, что не лежит в строке само (строки, двоичные данные, GUID)
        refs = [(at, bit) for tag, at, size, bit in cols if size == 4 and FIXED.get(tag & 0xFFFF) != 4]

        def fix(rows: bytes) -> bytes:
            data = bytearray(rows)
            for start in range(0, len(data) - row_size + 1, row_size):
                for at, bit in refs:
                    value = struct.unpack_from("<I", data, start + at)[0]
                    if data[start + rgib[2] + bit // 8] & (0x80 >> (bit % 8)) and value and not value & 0x1F:
                        struct.pack_into("<I", data, start + at, hids[value])
            return bytes(data)

        subs_in = self.pst.subnodes(bid_sub)
        subs = []
        rows_at = hnid_rows
        if hnid_rows & 0x1F:
            # Строки в подузле. В обычном формате в блоке 8176 // размер строк, остаток блока
            # пуст; здесь — 65512 // размер. Блоки, кроме последнего, добиваются до полного.
            rows = fix(b"".join(blk[:len(blk) // row_size * row_size]
                                for blk in self.pst.blocks(subs_in[hnid_rows][0])))
            per = BLOCK_4K // row_size * row_size
            chunks = [rows[i:i + per] for i in range(0, len(rows), per)]
            chunks = [c.ljust(BLOCK_4K, b"\x00") for c in chunks[:-1]] + chunks[-1:]
            subs.append((hnid_rows, self.out.write_blocks(chunks or [b""]), 0))
        elif hnid_rows:
            rows_at = hids[hnid_rows]
            new.set(rows_at, fix(allocs[hnid_rows]))
        new.set(hids[root], info[:10] + struct.pack("<III", hids[hid_index], rows_at, hids[hid_extra]) + info[22:])
        self._bth(allocs, new, hids, hid_index)        # в листьях индекса строк ссылок нет
        subs += [(nid, *self._sub(nid, *entry)) for nid, entry in subs_in.items() if nid != hnid_rows]
        return self.out.write_blocks(new.build()), self.out.write_subnodes(subs)


def to_4k(src: str, dst: str, *, compress: bool = True, method: int = 0, ost: bool = False,
          reverse: bool = False, fanout=None) -> None:
    """Переложить Unicode-файл писателя в формат с большими страницами (wVer 36).

    ``compress`` — сжимать блоки zlib (те, что от этого занимают в файле меньше места);
    ``method`` — закодировать данные (1 или 2); ``reverse`` — сначала сжать, потом
    закодировать (обычный порядок обратный); ``ost`` — пометить файл как ``.ost``;
    ``fanout`` — сколько ссылок класть в XBLOCK и SLBLOCK: настоящий предел — тысячи,
    и без этого XXBLOCK и SIBLOCK получились бы только на сотнях мегабайт.
    """
    pst = _Source(src)
    out = _Ndb4k(compress, method, reverse, fanout)
    ltp = _Ltp4k(pst, out)
    nodes = []
    for nid, (bid_data, bid_sub, parent) in pst.nbt.items():
        new_data, new_sub = ltp.node(bid_data, bid_sub)
        nodes.append((nid, struct.pack("<QQQII", nid, new_data, new_sub, parent, 0)))
    _key, nbt_bid, nbt_ib = out.tree(0x81, nodes, 32)
    _key, bbt_bid, bbt_ib = out.tree(0x80, out.bbt, 24)
    data = out.out
    data += bytes(-len(data) % 512)
    data[:564] = pst.data[:564]                        # заголовок у обоих видов Unicode один
    struct.pack_into("<HHH", data, 8, 0x4F53 if ost else 0x4D53, 36, 12 if ost else 19)
    struct.pack_into("<Q", data, 0x20, out.next_page)
    # карт размещения в файле нет — fAMapValid = 0 («карты надо построить заново»)
    struct.pack_into("<IQQQQQQQQBBH", data, 0xB4, 0, len(data), 0, 0, 0, nbt_bid, nbt_ib, bbt_bid, bbt_ib, 0, 0, 0)
    data[0x201] = method
    struct.pack_into("<Q", data, 0x204, out.next_bid)
    struct.pack_into("<I", data, 0x20C, _crc(data[8:8 + 516]))
    struct.pack_into("<I", data, 4, _crc(data[8:8 + 471]))
    with open(dst, "wb") as fh:
        fh.write(data)
