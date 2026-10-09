"""Независимый читатель .pst для тестов: проверяет файл по [MS-PST] и достаёт письма.

Написан отдельно от писателя (``mailarchiver.export.pstformat``) и ничего из
него не берёт: проверяются CRC и подписи страниц и блоков, B-деревья, карты
размещения (AMap совпадает с реально занятым местом), кучи, таблицы.
"""
from __future__ import annotations

import re
import struct
from collections import OrderedDict

_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc32_pst(data) -> int:
    crc = 0
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF


def sig(ib: int, bid: int) -> int:
    x = (ib ^ bid) & 0xFFFFFFFF
    return ((x >> 16) ^ x) & 0xFFFF


AMAP_FIRST = 0x4400
AMAP_SPAN = 0x3E000
FIXED = {0x0002: 2, 0x0003: 4, 0x0004: 4, 0x0005: 8, 0x0006: 8, 0x0007: 8, 0x000A: 4, 0x000B: 1,
         0x0014: 8, 0x0040: 8, 0x0048: 16}


class PstError(Exception):
    pass


class Pst:
    def __init__(self, path: str) -> None:
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.problems = []
        self.bbt = OrderedDict()      # bid -> (ib, cb, cRef)
        self.nbt = OrderedDict()      # nid -> (bidData, bidSub, parent)
        self.pages = []               # (ib, ptype, bid)
        self._header()
        self._walk(self.root["nbt"][1], self.root["nbt"][0], 0x81, self._nbt_leaf, None)
        self._walk(self.root["bbt"][1], self.root["bbt"][0], 0x80, self._bbt_leaf, None)

    def problem(self, text: str) -> None:
        self.problems.append(text)

    # ------------------------------------------------------------ заголовок
    def _header(self) -> None:
        d = self.data
        magic, crc_partial, magic_client, ver = struct.unpack_from("<IIHH", d, 0)
        if magic != 0x4E444221 or magic_client != 0x4D53:
            raise PstError("не файл PST")
        if ver < 23:
            raise PstError(f"не Unicode PST (wVer={ver})")
        self.ver = ver
        self.bid_next_p, self.unique = struct.unpack_from("<QI", d, 0x20)
        self.rgnid = struct.unpack_from("<32I", d, 0x2C)
        r = struct.unpack_from("<IQQQQQQQQBBH", d, 0xB4)
        self.root = {"eof": r[1], "amap_last": r[2], "amap_free": r[3], "pmap_free": r[4],
                     "nbt": (r[5], r[6]), "bbt": (r[7], r[8]), "valid": r[9]}
        self.rgbFM = d[0x100:0x180]
        self.rgbFP = d[0x180:0x200]
        if d[0x200] != 0x80:
            self.problem("bSentinel != 0x80")
        self.crypt = d[0x201]
        if self.crypt != 0:
            raise PstError("шифрованный PST этим читателем не поддерживается")
        self.bid_next_b, crc_full = struct.unpack_from("<QI", d, 0x204)
        if crc32_pst(d[8:8 + 471]) != crc_partial:
            self.problem("dwCRCPartial")
        if crc32_pst(d[8:8 + 516]) != crc_full:
            self.problem("dwCRCFull")
        if self.root["eof"] != len(d):
            self.problem(f"ibFileEof {self.root['eof']} != размер {len(d)}")
        if self.root["valid"] != 2:
            self.problem("fAMapValid != 2")

    # ------------------------------------------------------------ страницы
    def page(self, ib: int, expect_type=None, expect_bid=None):
        if ib % 512 or ib + 512 > len(self.data):
            raise PstError(f"страница {ib:#x} вне файла или не выровнена")
        page = self.data[ib:ib + 512]
        ptype, prep, wsig, crc, bid = struct.unpack_from("<BBHIQ", page, 496)
        if ptype != prep:
            self.problem(f"страница {ib:#x}: ptype != ptypeRepeat")
        if expect_type is not None and ptype != expect_type:
            self.problem(f"страница {ib:#x}: тип {ptype:#x}, ожидался {expect_type:#x}")
        if crc32_pst(page[:496]) != crc:
            self.problem(f"страница {ib:#x}: CRC")
        if ptype in (0x80, 0x81, 0x86):
            if wsig != sig(ib, bid):
                self.problem(f"страница {ib:#x}: wSig")
        elif wsig != 0 or bid != ib:
            self.problem(f"карта {ib:#x}: wSig/bid")
        if expect_bid is not None and bid != expect_bid:
            self.problem(f"страница {ib:#x}: bid {bid:#x} != {expect_bid:#x}")
        self.pages.append((ib, ptype, bid))
        return page, ptype, bid

    def _walk(self, ib, bid, ptype, leaf_cb, level_expect):
        page, _pt, _b = self.page(ib, ptype, bid)
        c_ent, c_max, cb_ent, level = struct.unpack_from("<BBBB", page, 488)
        if level_expect is not None and level != level_expect:
            self.problem(f"B-дерево {ib:#x}: уровень {level} вместо {level_expect}")
        if c_max != 488 // cb_ent or c_ent > c_max:
            self.problem(f"B-дерево {ib:#x}: cEntMax/cEnt")
        prev = None
        first = None
        for i in range(c_ent):
            off = i * cb_ent
            if level > 0:
                key, cbid, cib = struct.unpack_from("<QQQ", page, off)
                child_first = self._walk(cib, cbid, ptype, leaf_cb, level - 1)
                if child_first != key:
                    self.problem(f"B-дерево {ib:#x}: ключ {key:#x} != первому ключу потомка")
            else:
                key = leaf_cb(page, off)
            if prev is not None and key <= prev:
                self.problem(f"B-дерево {ib:#x}: ключи не по возрастанию")
            prev = key
            if first is None:
                first = key
        return first

    def _nbt_leaf(self, page, off):
        nid, bd, bs, parent, _pad = struct.unpack_from("<QQQII", page, off)
        if nid in self.nbt:
            self.problem(f"повтор узла {nid:#x}")
        self.nbt[nid] = (bd, bs, parent)
        return nid

    def _bbt_leaf(self, page, off):
        bid, ib, cb, cref, _pad = struct.unpack_from("<QQHHI", page, off)
        if bid in self.bbt:
            self.problem(f"повтор блока {bid:#x}")
        if cref < 1:
            self.problem(f"блок {bid:#x}: cRef {cref}")
        self.bbt[bid] = (ib, cb, cref)
        return bid

    # ------------------------------------------------------------ блоки
    def block(self, bid: int):
        key = bid & ~1
        if key not in self.bbt:
            raise PstError(f"блока {bid:#x} нет в BBT")
        ib, cb, _cref = self.bbt[key]
        size = (cb + 16 + 63) & ~63
        raw = self.data[ib:ib + size]
        if len(raw) != size:
            raise PstError(f"блок {bid:#x} за концом файла")
        t_cb, t_sig, t_crc, t_bid = struct.unpack_from("<HHIQ", raw, size - 16)
        body = raw[:cb]
        if t_cb != cb or t_bid != key:
            self.problem(f"блок {bid:#x}: трейлер cb/bid")
        if t_sig != sig(ib, key):
            self.problem(f"блок {bid:#x}: wSig")
        if crc32_pst(body) != t_crc:
            self.problem(f"блок {bid:#x}: CRC")
        if cb > 8176:
            self.problem(f"блок {bid:#x}: {cb} байт")
        return body, bool(key & 2)

    def blocks(self, bid: int):
        if bid == 0:
            return []
        body, internal = self.block(bid)
        if not internal:
            return [body]
        btype, level, count, total = struct.unpack_from("<BBHI", body, 0)
        if btype != 1 or level not in (1, 2):
            raise PstError(f"XBLOCK {bid:#x}: тип {btype} уровень {level}")
        if len(body) != 8 + 8 * count:
            self.problem(f"XBLOCK {bid:#x}: размер")
        out = []
        for i in range(count):
            child = struct.unpack_from("<Q", body, 8 + 8 * i)[0]
            if level == 1:
                data, inner = self.block(child)
                if inner:
                    self.problem(f"XBLOCK {bid:#x} ссылается на внутренний блок")
                out.append(data)
            else:
                if not child & 2:
                    self.problem(f"XXBLOCK {bid:#x} ссылается на блок данных")
                out.extend(self.blocks(child))
        if sum(len(x) for x in out) != total:
            self.problem(f"XBLOCK {bid:#x}: lcbTotal")
        return out

    def stream(self, bid: int) -> bytes:
        return b"".join(self.blocks(bid))

    def subnodes(self, bid: int):
        out = OrderedDict()
        if not bid:
            return out
        body, internal = self.block(bid)
        if not internal:
            raise PstError(f"подузлы {bid:#x}: не внутренний блок")
        btype, level, count = struct.unpack_from("<BBH", body, 0)
        if btype != 2:
            raise PstError(f"подузлы {bid:#x}: тип {btype}")
        prev = None
        for i in range(count):
            if level == 0:
                nid, bd, bs = struct.unpack_from("<QQQ", body, 8 + 24 * i)
                if prev is not None and nid <= prev:
                    self.problem(f"SLBLOCK {bid:#x}: порядок")
                prev = nid
                if nid in out:
                    self.problem(f"SLBLOCK {bid:#x}: повтор {nid:#x}")
                out[nid] = (bd, bs)
            else:
                nid, child = struct.unpack_from("<QQ", body, 8 + 16 * i)
                out.update(self.subnodes(child))
        return out

    # ------------------------------------------------------------ HN / BTH
    def heap(self, bid: int):
        blocks = self.blocks(bid)
        if not blocks:
            raise PstError("пустая куча")
        allocs = {}
        client = root = None
        for index, blk in enumerate(blocks):
            if index == 0:
                ibh, bsig, client, root, _fill = struct.unpack_from("<HBBII", blk, 0)
                if bsig != 0xEC:
                    raise PstError("HN: bSig")
                start = 12
            else:
                ibh = struct.unpack_from("<H", blk, 0)[0]
                start = 66 if index % 128 == 8 else 2
            c_alloc, _c_free = struct.unpack_from("<HH", blk, ibh)
            offs = struct.unpack_from(f"<{c_alloc + 1}H", blk, ibh + 4)
            if offs[0] != start:
                self.problem(f"HN блок {index}: первый кусок с {offs[0]}, а не {start}")
            if ibh != (offs[-1] + 1) & ~1:
                self.problem(f"HN блок {index}: ibHnpm {ibh} при конце данных {offs[-1]}")
            if ibh + 4 + 2 * (c_alloc + 1) != len(blk):
                self.problem(f"HN блок {index}: лишние байты после карты")
            for i in range(c_alloc):
                if offs[i + 1] < offs[i]:
                    self.problem("HN: смещения убывают")
                if offs[i + 1] - offs[i] > 3580:
                    self.problem("HN: кусок больше 3580")
                allocs[(index << 16) | ((i + 1) << 5)] = blk[offs[i]:offs[i + 1]]
        return {"client": client, "root": root, "allocs": allocs}

    def value(self, heap, subs, hnid: int) -> bytes:
        if hnid == 0:
            return b""
        if hnid & 0x1F == 0:
            if hnid not in heap["allocs"]:
                raise PstError(f"HID {hnid:#x} не найден")
            return heap["allocs"][hnid]
        if hnid not in subs:
            raise PstError(f"подузел {hnid:#x} не найден")
        return self.stream(subs[hnid][0])

    def bth(self, heap, hid: int):
        hdr = heap["allocs"][hid]
        btype, cb_key, cb_ent, levels, root = struct.unpack_from("<BBBBI", hdr, 0)
        if btype != 0xB5:
            raise PstError("BTH: bType")
        out = []
        if root == 0:
            return cb_key, cb_ent, out

        def walk(h, level):
            data = heap["allocs"][h]
            if level == 0:
                size = cb_key + cb_ent
                if len(data) % size:
                    self.problem("BTH: размер листа")
                for i in range(len(data) // size):
                    out.append((data[i * size:i * size + cb_key], data[i * size + cb_key:(i + 1) * size]))
            else:
                size = cb_key + 4
                for i in range(len(data) // size):
                    walk(struct.unpack_from("<I", data, i * size + cb_key)[0], level - 1)

        walk(root, levels)
        keys = [int.from_bytes(k, "little") for k, _v in out]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            self.problem("BTH: ключи не по возрастанию")
        return cb_key, cb_ent, out

    # ------------------------------------------------------------ PC / TC
    def pc(self, bid_data: int, bid_sub: int):
        subs = self.subnodes(bid_sub)
        heap = self.heap(bid_data)
        if heap["client"] != 0xBC:
            raise PstError("PC: подпись кучи")
        cb_key, cb_ent, recs = self.bth(heap, heap["root"])
        if (cb_key, cb_ent) != (2, 6):
            self.problem("PC: размеры ключа")
        props = OrderedDict()
        for k, v in recs:
            pid = struct.unpack("<H", k)[0]
            ptype, raw = struct.unpack("<HI", v)
            size = FIXED.get(ptype)
            if size is not None and size <= 4:
                props[pid] = (ptype, raw & ((1 << (8 * size)) - 1))
                continue
            data = self.value(heap, subs, raw)
            props[pid] = (ptype, self._decode(ptype, data))
        return props, subs

    @staticmethod
    def _decode(ptype: int, data: bytes):
        if ptype == 0x001F:
            if len(data) % 2:
                raise PstError("строка нечётной длины")
            return data.decode("utf-16-le")
        if ptype in (0x0014, 0x0040):
            return struct.unpack("<Q", data)[0]
        return data

    def tc(self, bid_data: int, bid_sub: int):
        subs = self.subnodes(bid_sub)
        heap = self.heap(bid_data)
        if heap["client"] != 0x7C:
            raise PstError("TC: подпись кучи")
        info = heap["allocs"][heap["root"]]
        btype, n_cols = struct.unpack_from("<BB", info, 0)
        rgib = struct.unpack_from("<4H", info, 2)
        hid_index, hnid_rows, hid_extra = struct.unpack_from("<III", info, 10)
        if btype != 0x7C or hid_extra != 0 or len(info) != 22 + 8 * n_cols:
            self.problem("TCINFO")
        cols = [struct.unpack_from("<IHBB", info, 22 + 8 * i) for i in range(n_cols)]
        if [c[0] for c in cols] != sorted(c[0] for c in cols):
            self.problem("TCINFO: столбцы не по порядку")
        row_size = rgib[3]
        if hnid_rows == 0:
            blocks = []
        elif hnid_rows & 0x1F:
            blocks = self.blocks(subs[hnid_rows][0])
        else:
            blocks = [heap["allocs"][hnid_rows]]
        rows = []
        for n, blk in enumerate(blocks):
            per = len(blk) // row_size
            if n < len(blocks) - 1 and len(blk) != 8176:
                self.problem("TC: неполный блок строк")
            for i in range(per):
                rows.append(blk[i * row_size:(i + 1) * row_size])
        _ck, _ce, idx = self.bth(heap, hid_index)
        index = {int.from_bytes(k, "little"): int.from_bytes(v, "little") for k, v in idx}
        if sorted(index.values()) != list(range(len(rows))):
            self.problem(f"TC: индекс строк ({len(index)}) не совпадает с числом строк ({len(rows)})")
        out = []
        for row in rows:
            values = OrderedDict()
            for tag, ib, cb, bit in cols:
                if not row[rgib[2] + bit // 8] & (0x80 >> (bit % 8)):
                    continue
                ptype = tag & 0xFFFF
                raw = row[ib:ib + cb]
                if ptype in FIXED and FIXED[ptype] == cb:
                    values[tag] = int.from_bytes(raw, "little")
                elif cb == 4:
                    values[tag] = self._decode(ptype, self.value(heap, subs, struct.unpack("<I", raw)[0]))
                else:
                    self.problem(f"TC: столбец {tag:#x} размером {cb}")
            out.append(values)
        for rid, pos in index.items():
            if out and out[pos].get(0x67F20003) != rid:
                self.problem(f"TC: индекс {rid:#x} указывает на чужую строку")
        return {"cols": cols, "rgib": rgib, "rows": out, "subs": subs}

    # ------------------------------------------------------------ карты
    def check_maps(self) -> None:
        """AMap должна совпадать с реально занятым местом; PMap/FMap/DList — на своих местах."""
        eof = self.root["eof"]
        regions = (eof - AMAP_FIRST) // AMAP_SPAN
        if AMAP_FIRST + regions * AMAP_SPAN != eof:
            self.problem("размер файла не кратен областям AMap")
        used = bytearray((eof - AMAP_FIRST) // 64)

        def mark(ib, size, what):
            for slot in range((ib - AMAP_FIRST) // 64, (ib - AMAP_FIRST + size) // 64):
                if used[slot]:
                    self.problem(f"пересечение: {what} {ib:#x}")
                    return
                used[slot] = 1

        longest = []
        for k in range(regions):
            base = AMAP_FIRST + k * AMAP_SPAN
            self.page(base, 0x84)
            mark(base, 512, "AMap")
            if k % 8 == 0:
                page, _t, _b = self.page(base + 512, 0x83)
                mark(base + 512, 512, "PMap")
                if page[:496] != b"\xFF" * 496:
                    self.problem("PMap не заполнена 0xFF")
            if k >= 128 and (k - 128) % 496 == 0:
                self.page(base + 1024, 0x82)
                mark(base + 1024, 512, "FMap")
        for bid, (ib, cb, _r) in self.bbt.items():
            mark(ib, (cb + 16 + 63) & ~63, f"блок {bid:#x}")
        for ib, ptype, _bid in list(self.pages):
            if ptype in (0x80, 0x81):
                mark(ib, 512, "страница B-дерева")
        free = 0
        for k in range(regions):
            base = AMAP_FIRST + k * AMAP_SPAN
            bits = self.data[base:base + 496]
            expect = bytearray(496)
            for slot in range(3968):
                if used[k * 3968 + slot]:
                    expect[slot // 8] |= 0x80 >> (slot % 8)
            if bytes(expect) != bits:
                self.problem(f"AMap {k}: биты не совпадают с занятым местом")
            as_int = int.from_bytes(bits, "big")
            free += 3968 - bin(as_int).count("1")
            runs = re.findall("0+", format(as_int, "03968b"))
            longest.append(min(255, max((len(r) for r in runs), default=0)))
        if free * 64 != self.root["amap_free"]:
            self.problem("cbAMapFree не совпадает с AMap")
        if self.root["amap_last"] != AMAP_FIRST + (regions - 1) * AMAP_SPAN:
            self.problem("ibAMapLast")
        if bytes(longest[:128]).ljust(128, b"\x00") != self.rgbFM:
            self.problem("rgbFM")
        for k in range(128, regions, 496):
            page = self.data[AMAP_FIRST + k * AMAP_SPAN + 1024:][:496]
            if bytes(longest[k:k + 496]).ljust(496, b"\x00") != page:
                self.problem(f"FMap {k}")
        dlist, _t, dbid = self.page(0x4200, 0x86)
        if dbid != self.bid_next_p:
            self.problem("DList: bid != bidNextP")
        top_block = max(self.bbt) if self.bbt else 0
        if self.bid_next_b <= top_block:
            self.problem("bidNextB не больше наибольшего BID")
        if any(b >= self.bid_next_p for _i, t, b in self.pages if t in (0x80, 0x81)):
            self.problem("bidNextP не больше номеров страниц")

    def check_references(self) -> None:
        """Каждый блок из BBT должен быть нужен какому-то узлу — «брошенных» блоков нет."""
        used = set()

        def tree(bid):
            if not bid:
                return
            key = bid & ~1
            if key in used:
                self.problem(f"блок {key:#x} использован дважды")
            used.add(key)
            body, internal = self.block(bid)
            if internal and body[0] == 1:
                count = struct.unpack_from("<H", body, 2)[0]
                for i in range(count):
                    tree(struct.unpack_from("<Q", body, 8 + 8 * i)[0])

        def subtree(bid):
            if not bid:
                return
            key = bid & ~1
            used.add(key)
            body, _internal = self.block(bid)
            level, count = body[1], struct.unpack_from("<H", body, 2)[0]
            for i in range(count):
                if level == 0:
                    _nid, bd, bs = struct.unpack_from("<QQQ", body, 8 + 24 * i)
                    tree(bd)
                    subtree(bs)
                else:
                    subtree(struct.unpack_from("<Q", body, 8 + 16 * i + 8)[0])

        for bd, bs, _parent in self.nbt.values():
            tree(bd)
            subtree(bs)
        orphans = set(self.bbt) - used
        if orphans:
            self.problem(f"брошенные блоки: {len(orphans)}")

    # ------------------------------------------------------------ сообщения
    def node_pc(self, nid: int):
        bd, bs, _p = self.nbt[nid]
        return self.pc(bd, bs)[0]

    def node_tc(self, nid: int):
        bd, bs, _p = self.nbt[nid]
        return self.tc(bd, bs)

    def folder_tree(self, nid: int = 0x122, path: str = ""):
        """[(путь, nid)] по таблицам иерархии."""
        out = []
        rows = self.node_tc((nid & ~0x1F) | 0x0D)["rows"]
        for row in rows:
            child = row[0x67F20003]
            if child & 0x1F != 0x02:
                continue
            name = row.get(0x3001001F, "")
            sub = f"{path}/{name}" if path else name
            out.append((sub, child))
            out.extend(self.folder_tree(child, sub))
        return out

    def messages(self, folder_nid: int):
        return [row[0x67F20003] for row in self.node_tc((folder_nid & ~0x1F) | 0x0E)["rows"]]

    def message(self, nid: int):
        bd, bs, _p = self.nbt[nid]
        props, subs = self.pc(bd, bs)
        recips = []
        if 0x692 in subs:
            recips = self.tc(*subs[0x692])["rows"]
        attachments = []
        if 0x671 in subs:
            for row in self.tc(*subs[0x671])["rows"]:
                anid = row[0x67F20003]
                att, _s = self.pc(*subs[anid])
                attachments.append((row, att))
        return props, recips, attachments
