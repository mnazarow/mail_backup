"""Инструменты для тестов чтения .pst.

Писатель службы создаёт только Unicode-файлы без «шифрования», а читать надо и
то, что создаёт Outlook. Здесь из файла писателя получаются другие виды того же
файла:

* :func:`encrypt_copy` — данные перекодированы перестановочным или циклическим
  способом ([MS-PST] 5.1, 5.2), как их хранит Outlook;
* :func:`to_ansi` — файл переложен в формат ANSI (Outlook 97–2002) на уровне
  блоков и B-деревьев: номера блоков и смещения по 4 байта, свои размеры
  страниц и трейлеров. Содержимое куч (свойства, таблицы) остаётся как было —
  строки в UTF-16; настоящие строки ANSI проверяются отдельным тестом.

Файл разбирается независимым строгим читателем :mod:`pst_reader`, а не тем,
который проверяется.
"""
from __future__ import annotations

import struct

from pst_reader import Pst, crc32_pst, sig

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
