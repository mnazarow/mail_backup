"""Чтение файлов с большими страницами — ``.ost`` Outlook 2013 и новее (wVer 36).

Настоящих файлов в тестах нет: файл создаёт писатель службы, а ``pst_tools.to_4k``
перекладывает его в формат с большими страницами — страницы по 4 КБ, блоки до
64 КБ, сжатие zlib, свои номера в кучах. Устройство такого файла снято с
настоящих ``.ost``; строгий разбор ``pst_tools.Pst4k`` проходит на них без
замечаний и здесь проверяет то, что получилось у ``to_4k``.

Главная проверка одна: копия читается так же, как исходный файл, — папки,
письма, получатели, вложения байт в байт, собранные письма RFC 822.

Чего эти тесты НЕ доказывают. Часть устройства формата настоящими файлами не
подтверждена: в четырёх доступных образцах ``.ost`` этого нет, и ``to_4k``, и
читатель написаны по одним и тем же описаниям формата. Тесты на это сверяют две
наши программы друг с другом — расхождение между ними поймают, ошибку в самих
описаниях нет:

* деление номера кучи (HID): 14 бит на номер кусочка, номер блока — с 19-го бита.
  Так делит libpff; java-libpst, pst-extractor и go-pst делят по-старому (11 бит,
  номер блока с 16-го) во всех видах файла. В образцах нет ни кучи длиннее одного
  блока, ни блока больше чем с 2047 кусочками (наибольшее — 1703), а до этих
  пределов оба деления дают одно и то же. Сюда же — куча и таблица длиннее блока
  (``test_heaps_and_tables_longer_than_a_block``);
* XXBLOCK и SIBLOCK в файле с большими блоками (``test_xxblock_and_subnode_index``):
  в образцах нет значений длиннее 536 МБ и узлов с тысячами подузлов;
* сжатие вместе с кодированием данных, в любом из двух порядков (варианты с
  ``method`` 1 и 2 при ``compress``, ``test_compressed_block_of_a_coded_file``):
  все образцы — без кодирования.
"""
import collections
import email
import hashlib
import os
import random
import shutil
import struct
import time
import tracemalloc
import zlib
from email import policy
from email.message import EmailMessage

import pytest

import pst_tools
from mailarchiver.export import pst_native, pstformat as pf
from mailarchiver.export.base import MailItem
from mailarchiver.pstimport import eml, importer, reader as rd

DATE = 1_756_700_000                      # 1 сентября 2025 года
BLOCK = pst_tools.BLOCK_4K


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def _mail(folder, raw, flags=(), date=DATE):
    return MailItem(folder=folder, raw=raw, flags=list(flags), internaldate=date, size=len(raw))


def _export(tmp_path, items, name="box.pst"):
    out = str(tmp_path / name)
    res = pst_native.NativePstExportEngine().export(iter(items), out, options={"tmp_dir": str(tmp_path)},
                                                    total_hint=len(items))
    assert res.errors == 0
    return out


def _lines(count: int, what: str = "строка отчёта") -> bytes:
    """Текст, который хорошо сжимается."""
    return "".join(f"{what} номер {i}\r\n" for i in range(count)).encode("utf-8")


def _dump(path, **kw):
    """Всё, что читатель достаёт из файла, — в виде, удобном для сравнения двух файлов."""
    out = {"folders": [], "messages": {}}
    with rd.PstReader(path, **kw) as pst:
        out["store"] = pst.store_name()
        for folder in pst.folders():
            nids = list(pst.message_nids(folder.nid))
            out["folders"].append((folder.nid, folder.path, folder.name, folder.container_class, folder.count, nids))
            for nid in nids:
                msg = pst.open_message(nid)
                built = eml.build_eml(msg)
                files = [(a.method, a.long_filename, a.filename, a.mime_type, a.content_id, a.data_size(),
                          _sha(b"".join(a.chunks()))) for a in msg.attachments()]
                out["messages"][nid] = {
                    "class": msg.message_class, "raw": built.raw, "flags": built.flags, "date": built.internaldate,
                    "id": built.message_id, "subject": built.subject, "lost": built.lost, "notes": built.notes,
                    "recipients": msg.recipients(), "attachments": files, "attachment_errors": msg.attachment_errors,
                    "props": {pid: (msg.pc.type_of(pid), msg.pc.get(pid)) for pid in msg.pc.ids()}}
        out["damaged"] = pst.damaged
    return out


def _strict(path):
    """Строгий разбор копии: замечаний быть не должно."""
    strict = pst_tools.Pst4k(path)
    assert strict.check() == []
    return strict


def _internal_kinds(strict) -> set:
    """Какие служебные блоки есть в файле: (1, 1) — XBLOCK, (1, 2) — XXBLOCK, (2, 0) — SLBLOCK, (2, 1) — SIBLOCK."""
    return {tuple(strict.block(bid)[:2]) for bid in strict.bbt if bid & 2}


# ---------------------------------------------------------------------------
#  Письма, которые кладутся в файл наборами свойств MAPI
# ---------------------------------------------------------------------------
class _Crafted:
    """Файл .pst, в который письма пишутся свойствами — как их хранит Outlook."""

    def __init__(self, path, monkeypatch):
        original = pf._encode_var

        def encode(ptype, value):
            if ptype == 0x000D:                        # объект (вложенное письмо) — писатель службы их не пишет
                return bytes(value)
            return original(ptype, value)

        monkeypatch.setattr(pf, "_encode_var", encode)
        self.path = path
        self.pst = pf.PstFile(path, "crafted")
        self.folder = self.pst.add_folder("Почта", pf.NID_IPM_SUBTREE)
        self.rows = {}

    def add(self, props, recipients=(), attachments=(), images=(), folder=None, layout=pf.LAYOUT_RECIPIENTS):
        """attachments — [(свойства, ячейки)]; images — готовые образы вложений [(NodeImage, ячейки)]."""
        pst = self.pst
        nid = pst.counters.next(pf.NT_NORMAL_MESSAGE)
        bid_data, bid_sub = pst.ctx.write_node(pf.build_pc(pst.ctx, props, self._parts(recipients, attachments,
                                                                                         images, layout)))
        pst.ndb.add_node(nid, bid_data, bid_sub, folder or self.folder)
        subject = next((value for pid, _ptype, value in props if pid == 0x0037), "")
        self.rows.setdefault(folder or self.folder, []).append(
            (nid, {0x001A001F: "IPM.Note", 0x0037001F: subject, 0x0042001F: "Отправитель", 0x0E070003: 0,
                   0x0E080003: 100, 0x0E170003: 0, 0x00170003: 1, 0x00360003: 0}))
        return nid

    def _parts(self, recipients, attachments, images, layout=pf.LAYOUT_RECIPIENTS):
        pst = self.pst
        rows = [(i + 1, pst._stamp(cells)) for i, cells in enumerate(recipients)]
        bid_data, bid_sub = pf.write_tc(pst.ctx, layout, rows)
        subnodes = [(pf.NID_RECIPIENT_TABLE, bid_data, bid_sub)]
        table = []
        for image, cells in [(pf.build_pc(pst.ctx, props), cells) for props, cells in attachments] + list(images):
            att_nid = pst.counters.next(pf.NT_ATTACHMENT)
            bid_data, bid_sub = pst.ctx.write_node(image)
            subnodes.append((att_nid, bid_data, bid_sub))
            table.append((att_nid, pst._stamp(cells)))
        if table:
            bid_data, bid_sub = pf.write_tc(pst.ctx, pf.LAYOUT_ATTACHMENTS, table)
            subnodes.append((pf.NID_ATTACHMENT_TABLE, bid_data, bid_sub))
        return subnodes

    def embedded(self, props, recipients=(), attachments=()):
        """Образ вложения «вложенное письмо»: письмо лежит подузлом самого вложения."""
        pst = self.pst
        bid_data, bid_sub = pst.ctx.write_node(pf.build_pc(pst.ctx, props, self._parts(recipients, attachments, ())))
        inner_nid = pst.counters.next(pf.NT_NORMAL_MESSAGE)
        image = pf.build_pc(pst.ctx, [(0x3705, pf.PT_I4, 5), (0x3001, pf.PT_UNICODE, "Вложенное"),
                                      (0x3701, 0x000D, struct.pack("<II", inner_nid, 1234))],
                            [(inner_nid, bid_data, bid_sub)])
        return image, {0x37050003: 5, 0x0E200003: 1234, 0x3704001F: "inner", 0x370B0003: -1}

    def close(self):
        for folder, rows in self.rows.items():
            self.pst.set_contents(folder, rows, 0)
        self.pst.close()
        return self.path


def _note(subject, *more):
    return [(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, subject),
            (0x0039, pf.PT_SYSTIME, pf.to_filetime(DATE)),
            (0x0E06, pf.PT_SYSTIME, pf.to_filetime(DATE + 60))] + list(more)


def _recipient(kind, name, addrtype, address):
    return {0x0C150003: kind, 0x3001001F: name, 0x3002001F: addrtype, 0x3003001F: address,
            0x0FFE0003: 6, 0x39000003: 0, 0x0FFF0102: b"\x00" * 4, 0x300B0102: b"k"}


def _file(name, data, mime="application/octet-stream"):
    props = [(0x3705, pf.PT_I4, 1), (0x3707, pf.PT_UNICODE, name), (0x370E, pf.PT_UNICODE, mime),
             (0x3701, pf.PT_BINARY, data)]
    return props, {0x0E200003: len(data), 0x3704001F: name, 0x37050003: 1, 0x370B0003: -1}


# ---------------------------------------------------------------------------
#  Ящик «как в жизни» и его копии во всех видах
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def box(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pst4k")
    rnd = random.Random(21)
    expect = {}
    items = []

    m = EmailMessage()
    m["From"] = "Иван Петров <ivan@example.ru>"
    m["To"] = "Мария <maria@example.ru>, bob@example.com"
    m["Cc"] = '"Сидоров, Пётр" <petr@example.ru>'
    m["Subject"] = "Отчёт за сентябрь"
    m["Date"] = "Mon, 01 Sep 2025 10:00:00 +0300"
    m["Message-ID"] = "<rep1@example.ru>"
    m.set_content("Текстовая версия отчёта.\n" * 40)
    m.add_alternative('<html><body><p>HTML <b>отчёт</b></p><img src="cid:logo1@x"></body></html>', subtype="html")
    logo = rnd.randbytes(5000)
    m.get_payload()[1].add_related(logo, maintype="image", subtype="png", cid="<logo1@x>")
    table = _lines(9000)                               # 300 КБ текста: дерево блоков, каждый сжимается
    m.add_attachment(table, maintype="text", subtype="csv", filename="Таблица.csv")
    photo = rnd.randbytes(200_000)                     # не сжимается: в дереве полные блоки по 65 512 байт
    m.add_attachment(photo, maintype="image", subtype="jpeg", filename="photo.jpg")
    small = _lines(900)                                # один блок, сжатый: размер вложения — не размер блока в файле
    m.add_attachment(small, maintype="text", subtype="plain", filename="small.txt")
    items.append(_mail("INBOX", m.as_bytes(), ["\\Seen", "\\Answered"]))
    expect.update(logo=_sha(logo), table=_sha(table), photo=_sha(photo), small=_sha(small), small_size=len(small))

    m = EmailMessage()
    m["From"] = "boss@example.com"
    m["To"] = "me@example.ru"
    m["Subject"] = "Fwd: вложенное"
    m["Message-ID"] = "<fwd@example.com>"
    m.set_content("Смотри вложение")
    inner = EmailMessage()
    inner["From"] = "x@y.z"
    inner["Subject"] = "Внутреннее письмо"
    inner.set_content("Привет из вложенного письма")
    m.add_attachment(inner)
    items.append(_mail("INBOX/Проекты", m.as_bytes(), ["\\Seen", "\\Flagged", "$Forwarded"]))

    html = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
            '</head><body>Только HTML: привет</body></html>').encode("cp1251")
    items.append(_mail("Trash", b"From: a@b.c\r\nTo: d@e.f\r\nSubject: html only\r\nMIME-Version: 1.0\r\n"
                                b"Message-ID: <html@b.c>\r\nContent-Type: text/html; charset=windows-1251\r\n\r\n"
                       + html, ["\\Seen"]))
    items.append(_mail("Drafts", b"Subject: draft\r\nTo: x@y.z\r\n\r\nnot sent yet", ["\\Draft"]))
    for i in range(40):
        items.append(_mail("INBOX/Проекты/2024", f"Subject: n{i}\r\nMessage-ID: <n{i}@x>\r\n\r\nтело {i}".encode(),
                           ["\\Seen"] if i % 2 else [], DATE + i))
    path = _export(tmp, items)
    return path, _dump(path), expect, tmp


#: (сжатие, кодирование данных, обратный порядок сжатия и кодирования)
VARIANTS = [(False, 0, False), (True, 0, False), (False, 1, False), (True, 1, False), (True, 1, True),
            (False, 2, False), (True, 2, False), (True, 2, True)]


@pytest.mark.parametrize("compress,method,reverse", VARIANTS)
def test_4k_copy_reads_like_the_original(box, compress, method, reverse):
    path, original, expect, tmp = box
    copy = str(tmp / f"copy{int(compress)}{method}{int(reverse)}.ost")
    pst_tools.to_4k(path, copy, compress=compress, method=method, reverse=reverse, ost=True)
    strict = _strict(copy)
    packed = sum(1 for _ib, cb, inflated, _cref in strict.bbt.values() if cb != inflated)
    # маленькие блоки не сжимаются (места не выгадать), а после циклического кодирования
    # сжимать нечего — если только сжатие не шло первым
    assert (packed >= 10) == (compress and (method != 2 or reverse)) and (packed == 0) == (packed < 10)
    assert (1, 1) in _internal_kinds(strict)           # вложения длиннее блока лежат деревом XBLOCK

    with rd.PstReader(copy) as pst:
        assert pst.four_k and pst.unicode and pst.crypt == method and pst.ndb.ost
        assert "64 КБ" in pst.describe() and ".ost" in pst.describe()
    got = _dump(copy)
    assert got["damaged"] == 0 and original["damaged"] == 0
    assert got["store"] == original["store"] == "box"
    assert got["folders"] == original["folders"] and len(got["folders"]) == 6
    assert got["messages"].keys() == original["messages"].keys() and len(got["messages"]) == 44
    for nid, mail in original["messages"].items():
        assert got["messages"][nid] == mail, f"письмо {nid:#x} «{mail['subject']}»"

    # и это не пустое совпадение: вложения — те самые байты, что были в письмах
    report = next(m for m in got["messages"].values() if m["subject"] == "Отчёт за сентябрь")
    files = {name: (size, digest) for _m, name, _short, _mime, _cid, size, digest in report["attachments"]}
    assert files["Таблица.csv"][1] == expect["table"] and files["photo.jpg"] == (200_000, expect["photo"])
    assert files["small.txt"] == (expect["small_size"], expect["small"])
    assert expect["logo"] in {digest for _size, digest in files.values()}
    assert report["flags"] == ["\\Seen", "\\Answered"] and report["date"] == DATE
    assert not any(m["lost"] or m["attachment_errors"] for m in got["messages"].values())


def test_format_is_told_by_pages_not_by_version(box, tmp_path):
    """Вид файла определяют страницы B-деревьев: номер версии у .ost бывает разный."""
    path, original, _expect, tmp = box
    copy = str(tmp / "versions.ost")
    pst_tools.to_4k(path, copy, compress=True, ost=True)
    data = bytearray(open(copy, "rb").read())
    for version in (24, 36, 37, 60):
        struct.pack_into("<H", data, 10, version)
        target = tmp_path / f"v{version}.ost"
        target.write_bytes(data)
        with rd.PstReader(str(target)) as pst:
            assert pst.four_k and sum(f.count for f in pst.folders()) == 44
    struct.pack_into("<H", data, 10, 23)               # версия обычного Unicode: страницы читаются по 512 байт
    (tmp_path / "v23.ost").write_bytes(data)
    with pytest.raises(rd.PstReadError):
        rd.PstReader(str(tmp_path / "v23.ost"))
    # обычный файл с номером версии «как у больших страниц» остаётся обычным
    plain = bytearray(open(path, "rb").read())
    struct.pack_into("<H", plain, 10, 36)
    (tmp_path / "plain36.pst").write_bytes(plain)
    with rd.PstReader(str(tmp_path / "plain36.pst")) as pst:
        assert not pst.four_k and pst.unicode and sum(f.count for f in pst.folders()) == 44
    # ни те страницы, ни другие — файл не наш
    root = struct.unpack_from("<Q", data, 0xB4 + 44)[0]
    struct.pack_into("<H", data, 10, 36)
    data[root + 4072:root + 4074] = b"\x00\x00"
    (tmp_path / "junk.ost").write_bytes(data)
    with pytest.raises(rd.PstUnsupportedError, match="wVer=36"):
        rd.PstReader(str(tmp_path / "junk.ost"))


def test_page_kind_needs_both_type_bytes(box, heavy, tmp_path):
    """Тип страницы записан в её трейлере дважды, и вид файла узнаётся по обоим байтам: один
    совпавший байт там, где был бы трейлер страницы другого размера, — ещё не страница."""
    def opened(name, data):
        (tmp_path / name).write_bytes(data)
        return rd.PstReader(str(tmp_path / name))

    # Обычный файл с версией «как у больших страниц»: сначала пробуется страница в 4 КБ. Корень
    # B-дерева узлов переносится в конец файла — за ним появляется место, где был бы её трейлер.
    data = bytearray(open(box[0], "rb").read())
    root = struct.unpack_from("<Q", data, 0xB4 + 44)[0]
    page = bytes(data[root:root + 512])
    data += bytes(-len(data) % 512)
    moved = len(data)
    data += page + bytes(4096 - 512)
    struct.pack_into("<H", data, moved + 498, pst_tools.sig(moved, struct.unpack_from("<Q", page, 504)[0]))
    struct.pack_into("<H", data, 10, 36)
    struct.pack_into("<Q", data, 0xB4 + 4, len(data))
    struct.pack_into("<Q", data, 0xB4 + 44, moved)
    struct.pack_into("<I", data, 0x20C, pst_tools._crc(bytes(data[8:8 + 516])))
    struct.pack_into("<I", data, 4, pst_tools._crc(bytes(data[8:8 + 471])))
    for pair in (b"\x00\x00", b"\x81\x00", b"\x00\x81"):
        data[moved + 4072:moved + 4074] = pair
        with opened("plain.pst", data) as pst:
            assert not pst.four_k and pst.unicode and pst.damaged == 0
            assert sum(f.count for f in pst.folders()) == 44
    data[moved + 4072:moved + 4074] = b"\x81\x81"      # оба байта на месте — это уже «страница в 4 КБ»
    with pytest.raises(rd.PstReadError):
        opened("plain.pst", data)

    # Файл с большими страницами и версией прежнего .ost: сначала пробуется страница в 512 байт.
    # Её трейлер пришёлся бы на свободное место корня B-дерева узлов (записей в корне мало).
    data = bytearray(open(heavy[1], "rb").read())
    root = struct.unpack_from("<Q", data, 0xB4 + 44)[0]
    count, _limit, entry, level = struct.unpack_from("<HHBB", data, root + 4056)
    assert level and count * entry <= 496
    struct.pack_into("<H", data, 10, 24)
    for pair in (b"\x00\x00", b"\x81\x00", b"\x00\x81", b"\x81\x81"):
        data[root + 496:root + 498] = pair
        struct.pack_into("<I", data, root + 4076, pst_tools._crc(bytes(data[root:root + 4072])))
        if pair == b"\x81\x81":
            with pytest.raises(rd.PstReadError):
                opened("large.ost", data)
            continue
        with opened("large.ost", data) as pst:
            assert pst.four_k and pst.damaged == 0 and sum(f.count for f in pst.folders()) == 1703


# ---------------------------------------------------------------------------
#  Корень папок в .ost
# ---------------------------------------------------------------------------
def _ost_like(path, monkeypatch, *, fixed_nid: bool, subtree: bool = True, rival: bool = False) -> str:
    """Файл с деревом папок, как в .ost: корня видимых папок в хранилище нет, ящик лежит в
    «Root - Mailbox/IPM_SUBTREE», а узел 0x8022 — посторонняя папка. ``rival`` — второй
    «IPM_SUBTREE», у «Root - Public», тоже не пуст: в нём папка и письма."""
    original = pf.PstFile._write_pc

    def write_pc(self, nid, props, parent=0):
        if nid == pf.NID_MESSAGE_STORE:
            props = [prop for prop in props if prop[0] != 0x35E0]
        return original(self, nid, props, parent)

    monkeypatch.setattr(pf.PstFile, "_write_pc", write_pc)
    pst = pf.PstFile(path, "")
    pst.rename_folder(pf.NID_IPM_SUBTREE, "Files")
    pst.add_special_folder(0x2002, "Root - Public", pf.NID_ROOT_FOLDER, None)
    pst.add_special_folder(0x2022, rd.OST_IPM_SUBTREE_NAME, 0x2002, None)
    pst.add_special_folder(0x20A2, "Root - Mailbox", pf.NID_ROOT_FOLDER, None)
    pst.add_special_folder(0x20C2, "Common Views", 0x20A2, None)
    top = 0x20A2
    if subtree and fixed_nid:
        top = rd.NID_OST_IPM_SUBTREE
        pst.add_special_folder(top, rd.OST_IPM_SUBTREE_NAME, 0x20A2, None)
    elif subtree:
        top = pst.add_folder(rd.OST_IPM_SUBTREE_NAME, 0x20A2)
    inbox = pst.add_folder("Inbox", top)
    sent = pst.add_folder("Sent Items", top)
    letters = [(inbox, "первое"), (inbox, "второе"), (sent, "отправленное"), (pf.NID_IPM_SUBTREE, "чужое")]
    if rival:
        pst.add_folder("Favorites", 0x2022)
        letters += [(0x2022, f"общее {i}") for i in range(3)]
    contents = {}
    for folder, subject in letters:
        nid = pst.write_message(folder, _note(subject, (0x1000, pf.PT_UNICODE, "текст")), [], [])
        contents.setdefault(folder, []).append((nid, {0x001A001F: "IPM.Note", 0x0037001F: subject}))
    for folder, rows in contents.items():
        pst.set_contents(folder, rows, 0)
    pst.close()
    return path


@pytest.mark.parametrize("fixed_nid", [True, False])
def test_ost_root_is_the_mailbox_subtree(tmp_path, monkeypatch, fixed_nid):
    path = _ost_like(str(tmp_path / "like-ost.pst"), monkeypatch, fixed_nid=fixed_nid)
    copy = str(tmp_path / "like.ost")
    pst_tools.to_4k(path, copy, ost=True)
    _strict(copy)
    old = bytearray(open(path, "rb").read())           # .ost прежних версий Outlook устроен так же
    old[8:10] = b"SO"
    (tmp_path / "old.ost").write_bytes(old)
    for name in (copy, str(tmp_path / "old.ost")):
        with rd.PstReader(name) as pst:
            assert pst.ndb.ost
            folders = {"/".join(f.path): f.count for f in pst.folders()}
            assert folders == {"": 0, "Inbox": 2, "Sent Items": 1}
            subjects = sorted(eml.build_eml(pst.open_message(nid)).subject
                              for f in pst.folders() for nid in pst.message_nids(f.nid))
            assert subjects == ["второе", "отправленное", "первое"]
    # тот же файл, но не .ost: корень ищется, как прежде, — по узлу 0x8022
    with rd.PstReader(path) as pst:
        assert {"/".join(f.path): f.count for f in pst.folders()} == {"": 1, "Deleted Items": 0}


def test_ost_root_among_several_subtrees_is_the_one_with_more_folders(tmp_path, monkeypatch):
    """Папок «IPM_SUBTREE» в .ost две; вторая (у «Root - Public») в образцах пуста. Если она не
    пуста, а постоянного номера у ящика нет, ящик узнаётся по числу подпапок: здесь у чужой
    папки номер меньше — она находится первой, и писем прямо в ней больше, но подпапка одна."""
    path = _ost_like(str(tmp_path / "rival.pst"), monkeypatch, fixed_nid=False, rival=True)
    copy = str(tmp_path / "rival.ost")
    pst_tools.to_4k(path, copy, ost=True)
    _strict(copy)
    with rd.PstReader(copy) as pst:
        mailbox = pst.root_nid()
        weights = {nid: (len(pst._subfolders.get(nid, ())), len(pst._messages.get(nid, ())))
                   for nid in (0x2022, mailbox)}
        assert mailbox > 0x2022 and weights == {0x2022: (1, 3), mailbox: (2, 0)}
        assert {"/".join(f.path): f.count for f in pst.folders()} == {"": 0, "Inbox": 2, "Sent Items": 1}


def test_ost_without_the_subtree_shows_everything(tmp_path, monkeypatch):
    path = _ost_like(str(tmp_path / "bare.pst"), monkeypatch, fixed_nid=False, subtree=False)
    copy = str(tmp_path / "bare.ost")
    pst_tools.to_4k(path, copy, ost=True)
    with rd.PstReader(copy) as pst:
        folders = {"/".join(f.path): f.count for f in pst.folders()}
    # нужной папки нет — корнем остаётся корень файла: почта не теряется
    assert folders["Root - Mailbox/Inbox"] == 2 and folders["Root - Mailbox/Sent Items"] == 1
    assert folders["Files"] == 1 and "Root - Public/IPM_SUBTREE" in folders


# ---------------------------------------------------------------------------
#  Деревья данных и подузлов, кучи и таблицы длиннее одного блока
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def heavy(tmp_path_factory):
    """Файл с тем, что в большом формате занимает больше одного блока в 64 КБ."""
    tmp = tmp_path_factory.mktemp("pst4k-heavy")
    monkeypatch = pytest.MonkeyPatch()
    crafted = _Crafted(str(tmp / "heavy.pst"), monkeypatch)
    rnd = random.Random(33)
    nids = {}
    # куча свойств длиннее блока: 60 значений по 3 КБ (больше 3580 байт в кучу не кладётся)
    blobs = {0x6800 + i: rnd.randbytes(3000) for i in range(60)}
    nids["props"] = crafted.add(_note("много свойств", (0x1000, pf.PT_UNICODE, "тело"),
                                      *[(pid, pf.PT_BINARY, blob) for pid, blob in blobs.items()]))
    # таблица получателей: и строки, и куча — в несколько блоков, в блоке кучи больше 2047 кусочков
    people = [_recipient(1 + i % 3, f"А{i}", "SMTP", f"u{i}@e{i % 7}.ru") for i in range(3000)]
    nids["crowd"] = crafted.add(_note("много получателей", (0x1000, pf.PT_UNICODE, "всем привет")), recipients=people)
    # вложения: много подузлов, большое значение, вложенное письмо со своими подузлами
    big = rnd.randbytes(300_000) + _lines(8000)
    inner = crafted.embedded(_note("Внутри", (0x1000, pf.PT_UNICODE, "Текст вложенного письма"),
                                   (0x0C1A, pf.PT_UNICODE, "Автор"), (0x0C1E, pf.PT_UNICODE, "SMTP"),
                                   (0x0C1F, pf.PT_UNICODE, "author@example.ru")),
                             recipients=[_recipient(1, "Кому", "SMTP", "to@example.ru")],
                             attachments=[_file("Договор.docx", _lines(3000, "пункт договора"))])
    files = [_file(f"файл {i}.txt", _lines(200 + i, f"файл {i}"), "text/plain") for i in range(12)]
    nids["files"] = crafted.add(_note("много вложений", (0x1000, pf.PT_UNICODE, "см. вложения")),
                                attachments=[_file("big.bin", big)] + files, images=[inner])
    # папка, таблица содержимого которой длиннее блока
    many = crafted.pst.add_folder("Много", pf.NID_IPM_SUBTREE)
    for i in range(1700):
        crafted.add(_note(f"письмо {i}", (0x1000, pf.PT_UNICODE, f"тело {i}")), folder=many)
    path = crafted.close()
    monkeypatch.undo()
    copy = str(tmp / "heavy.ost")
    pst_tools.to_4k(path, copy, compress=True)
    return path, copy, nids, {"blobs": blobs, "big": big, "many": many}, tmp


def test_heaps_and_tables_longer_than_a_block(heavy):
    path, copy, nids, expect, _tmp = heavy
    strict = _strict(copy)
    limit = 1 << pst_tools.HID_4K_INDEX_BITS

    def heap_shape(bid):
        hids = list(strict.heap(bid))
        return max(hid >> 19 for hid in hids) + 1, max((hid >> 5) % limit for hid in hids)

    # куча свойств письма — три блока
    props_data, _sub, _parent = strict.nbt[nids["props"]]
    assert heap_shape(props_data)[0] >= 3
    # таблица получателей: куча в несколько блоков, номера кусочков — за пределом 11 бит
    crowd = strict.subnodes(strict.nbt[nids["crowd"]][1])[pf.NID_RECIPIENT_TABLE]
    blocks, top_index = heap_shape(crowd[0])
    assert blocks >= 2 and top_index > 2047
    rows = [len(b) for nid, (bid, _s) in strict.subnodes(crowd[1]).items() for b in strict.blocks(bid, full=False)]
    assert len(rows) == 3 and rows[0] == rows[1] == BLOCK      # 3000 строк по 52 байта: 1259 в блоке
    # таблица содержимого папки
    contents = strict.nbt[(expect["many"] & ~0x1F) | 0x0E]
    blocks, top_index = heap_shape(contents[0])
    assert blocks >= 2 and top_index > 2047
    assert sum(len(strict.blocks(bid, full=False)) for bid, _s in strict.subnodes(contents[1]).values()) >= 3

    original, got = _dump(path), _dump(copy)
    assert got == original and got["damaged"] == 0 and len(got["messages"]) == 1703
    mail = got["messages"][nids["props"]]
    assert all(mail["props"][pid] == (pf.PT_BINARY, blob) for pid, blob in expect["blobs"].items())
    crowd_mail = got["messages"][nids["crowd"]]
    assert len(crowd_mail["recipients"]) == 3000
    assert [row[0x3003001F] for row in crowd_mail["recipients"]] == [f"u{i}@e{i % 7}.ru" for i in range(3000)]
    assert crowd_mail["raw"].count(b".ru>") == 3000 and not crowd_mail["lost"]
    files = got["messages"][nids["files"]]
    assert files["attachments"][0][5:] == (len(expect["big"]), _sha(expect["big"]))
    assert len(files["attachments"]) == 14 and not files["lost"]
    parts = list(_parse(files["raw"]).iter_attachments())
    assert [p.get_filename() for p in parts] == ["big.bin"] + [f"файл {i}.txt" for i in range(12)] + ["Вложенное.eml"]
    assert parts[0].get_payload(decode=True) == expect["big"]
    inner = parts[-1].get_payload()[0]                 # вложенное письмо — со своими получателями и вложением
    assert parts[-1].get_content_type() == "message/rfc822" and inner["Subject"] == "Внутри"
    assert inner["From"].addresses[0].addr_spec == "author@example.ru"
    assert inner["To"].addresses[0].addr_spec == "to@example.ru"
    assert [p.get_payload(decode=True) for p in inner.iter_attachments()] == [_lines(3000, "пункт договора")]

    # таблицы целиком — те же строки, что и в исходном файле
    for name in (path, copy):
        with rd.PstReader(name) as pst:
            bid_data, bid_sub, _parent = pst.ndb.node((expect["many"] & ~0x1F) | rd.NT_CONTENTS_TABLE)
            table = rd.read_table(pst.ndb, bid_data, bid_sub)
            ids = rd.table_row_ids(pst.ndb, bid_data)
            folder = next(f for f in pst.folders() if f.nid == expect["many"])
            assert ids == list(pst.message_nids(folder.nid)) and len(ids) == 1700
            assert [row[0x67F20003] for row in table] == ids
            assert [row[0x0037001F] for row in table] == [f"письмо {i}" for i in range(1700)]
    # и запасной путь — письма папки по её таблице, когда связей узлов нет
    with rd.PstReader(copy) as pst:
        pst._messages.clear()
        folder = next(f for f in pst.folders() if f.nid == expect["many"])
        assert folder.count == 1700 and list(pst.message_nids(folder.nid)) == ids


def test_rows_of_a_long_table_are_found_by_block_lengths():
    """Таблица длиннее блока в настоящих файлах не встретилась. Читатель не вычисляет, сколько
    строк «положено» блоку, а смотрит на длину каждого блока — и читает таблицу и с полными
    блоками, и с блоками другого размера (так файл могла переложить сторонняя программа)."""
    class Layout:
        unicode = True
        four_k = True
        block_max = BLOCK
        hid_index_bits = 14

    class Ndb:
        layout = Layout()

        def __init__(self, heap, rows):
            self._heap, self._rows = heap, rows

        def subnodes(self, bid):
            return {0x3F: (7, 0)} if bid else {}

        def tree_blocks(self, bid):
            return iter(self._rows if bid == 7 else self._heap)

    heap = pf.Heap(0x7C)
    rows = [struct.pack("<IIB", 100 + n, n * n, 0xC0) for n in range(9)]          # номер строки, число, биты ячеек
    leaf = heap.alloc(b"".join(struct.pack("<II", 100 + n, n) for n in range(9)))
    index = heap.alloc(struct.pack("<BBBBI", 0xB5, 4, 4, 0, leaf))
    heap.root = heap.alloc(struct.pack("<BB4HIII", 0x7C, 2, 8, 8, 8, 9, index, 0x3F, 0)
                           + struct.pack("<IHBBIHBB", 0x67F20003, 0, 4, 0, 0x66050003, 4, 4, 1))
    blocks = [b"".join(rows[:3]) + bytes(5), b"".join(rows[3:5]), b"".join(rows[5:])]
    table = rd.read_table(Ndb(heap.build(), blocks), 5, 9)
    assert [(row[0x67F20003], row[0x66050003]) for row in table] == [(100 + n, n * n) for n in range(9)]
    # в обычном формате строк в блоке — сколько положено по размеру блока: дальше первого блока их не найти
    Layout.four_k = False
    assert [row[0x67F20003] for row in rd.read_table(Ndb(heap.build(), blocks), 5, 9)] == [100, 101, 102]


def test_xxblock_and_subnode_index(heavy):
    """XXBLOCK и SIBLOCK при настоящих пределах — это сотни мегабайт и тысячи вложений;
    здесь в служебные блоки кладётся по четыре ссылки."""
    path, _copy, nids, expect, tmp = heavy
    copy = str(tmp / "fanout.ost")
    pst_tools.to_4k(path, copy, compress=True, method=1, fanout=4)
    strict = _strict(copy)
    assert _internal_kinds(strict) == {(1, 1), (1, 2), (2, 0), (2, 1)}
    with rd.PstReader(copy) as pst, rd.PstReader(path) as source:
        for reader in (pst, source):
            reader.folders()
        for nid in nids.values():
            assert eml.build_eml(pst.open_message(nid)).raw == eml.build_eml(source.open_message(nid)).raw
        big = pst.open_message(nids["files"]).attachments()[0]
        assert big.data_size() == len(expect["big"])           # размер — из заголовка XXBLOCK, без чтения данных
        chunks = list(big.chunks())
        assert b"".join(chunks) == expect["big"] and max(map(len, chunks)) == BLOCK
        assert pst.damaged == 0


# ---------------------------------------------------------------------------
#  Сжатые блоки: распаковка с пределом, порча
# ---------------------------------------------------------------------------
def test_inflate_never_gives_more_than_declared():
    data = _lines(2000)
    packed = zlib.compress(data)
    assert rd.inflate(packed, len(data)) == data
    assert rd.inflate(packed + b"tail", len(data)) == data             # за концом потока — заполнение блока
    for wrong in (len(data) - 1, len(data) + 1, 1, 0, -5):
        with pytest.raises(rd.PstReadError):
            rd.inflate(packed, wrong)
    for broken in (packed[:len(packed) // 2], packed[:-1], b"", b"\x78\x9c", os.urandom(300),
                   packed[:40] + bytes(len(packed) - 40), zlib.compress(data, wbits=-15)):
        with pytest.raises(rd.PstReadError):
            rd.inflate(broken, len(data))
    flipped = bytearray(packed)
    flipped[-2] ^= 0x55                                # сумма потока не сходится
    with pytest.raises(rd.PstReadError):
        rd.inflate(bytes(flipped), len(data))
    # «бомба»: 60 КБ потока разворачиваются в 64 МБ — распаковывается не больше названного
    bomb = zlib.compress(bytes(64 * 1024 * 1024), 9)
    tracemalloc.start()
    with pytest.raises(rd.PstReadError, match="больше названных"):
        rd.inflate(bomb, 60_000)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def _rewrite_block(path, bid, *, stored=None, inflated=None, fix_sum=True):
    """Подменить данные блока и (или) его размер после распаковки — в самом блоке и в его записи BBT."""
    strict = pst_tools.Pst4k(path)
    data = bytearray(strict.data)
    ib, cb, old_inflated, _cref = strict.bbt[bid]
    span = pst_tools._block_span
    old_sum = struct.unpack_from("<I", data, ib + span(cb) - 20)[0]
    stored = bytes(data[ib:ib + cb]) if stored is None else stored
    inflated = old_inflated if inflated is None else inflated
    assert span(len(stored)) <= span(cb)
    data[ib:ib + len(stored)] = stored
    struct.pack_into("<HHIQHHI", data, ib + span(len(stored)) - 24, len(stored), pst_tools.sig(ib, bid),
                     pst_tools._crc(stored) if fix_sum else old_sum, bid, 2, inflated, 0)
    for page, ptype, level in strict.pages:
        if ptype != 0x80 or level:
            continue
        for at in range(page, page + 24 * struct.unpack_from("<H", data, page + 4056)[0], 24):
            if struct.unpack_from("<Q", data, at)[0] == bid:
                struct.pack_into("<HH", data, at + 16, len(stored), inflated)
                struct.pack_into("<I", data, page + 4076, pst_tools._crc(bytes(data[page:page + 4072])))
    with open(path, "wb") as fh:
        fh.write(data)


def _attachment_trees(strict, nid: int) -> list:
    """Деревья данных вложений письма, по порядку вложений: значение вложения — подузел вложения."""
    subnodes = strict.subnodes(strict.nbt[nid][1])
    trees = []
    for attachment in sorted(sub for sub in subnodes if sub & 0x1F == 0x05):
        (tree, _sub), = strict.subnodes(subnodes[attachment][1]).values()
        trees.append(tree)
    return trees


@pytest.fixture(scope="module")
def victim(tmp_path_factory):
    """Три письма; у первого сжимаются и свойства, и вложение, а второе вложение — полный несжатый блок."""
    tmp = tmp_path_factory.mktemp("pst4k-victim")
    monkeypatch = pytest.MonkeyPatch()
    crafted = _Crafted(str(tmp / "victim.pst"), monkeypatch)
    noise = random.Random(44).randbytes(BLOCK + 5000)
    first = crafted.add(_note("жертва", (0x1000, pf.PT_UNICODE, "повтор " * 200)),      # тело — в куче свойств
                        recipients=[_recipient(1, "Кому", "SMTP", "to@example.ru")],
                        attachments=[_file("text.txt", _lines(6000), "text/plain"), _file("noise.bin", noise)])
    crafted.add(_note("второе", (0x1000, pf.PT_UNICODE, "текст второго")))
    crafted.add(_note("третье", (0x1000, pf.PT_UNICODE, "текст третьего")))
    path = crafted.close()
    monkeypatch.undo()
    copy = str(tmp / "victim.ost")
    pst_tools.to_4k(path, copy, compress=True)
    strict = _strict(copy)
    heap = strict.nbt[first][0]
    text_tree, noise_tree = _attachment_trees(strict, first)
    # первый блок данных каждого вложения
    text, noise_block = (struct.unpack_from("<Q", strict.block(tree), 8)[0] for tree in (text_tree, noise_tree))
    stored, size = strict.bbt[heap][1:3]
    assert stored < size                               # свойства письма сжаты
    stored, size = strict.bbt[text][1:3]
    assert stored < size == BLOCK                      # блок текста сжат
    assert strict.bbt[noise_block][1:3] == (BLOCK, BLOCK)      # а шум — нет
    return copy, {"heap": heap, "text": text, "noise": noise_block, "text_tree": text_tree}, tmp


def _read_victim(path):
    with rd.PstReader(path) as pst:
        total, items = importer.builtin_source(pst)
        items = list(items)
        return total, items, pst.damaged


#: порча → (какой блок, как испортить)
DAMAGE = {
    "поток обрезан": ("heap", lambda body, size: {"stored": body[:len(body) // 2]}),
    "поток без хвоста": ("heap", lambda body, size: {"stored": body[:-4]}),
    "мусор вместо потока": ("heap", lambda body, size: {"stored": random.Random(1).randbytes(len(body))}),
    "мусор и сумма не та": ("heap", lambda body, size: {"stored": random.Random(2).randbytes(len(body)),
                                                         "fix_sum": False}),
    "размер меньше настоящего": ("heap", lambda body, size: {"inflated": size - 100}),
    "размер больше настоящего": ("heap", lambda body, size: {"inflated": size + 100}),
    "размер больше блока": ("heap", lambda body, size: {"inflated": 0xFFFF}),
    "размер больше блока, и поток такой": ("heap", lambda body, size: {"stored": zlib.compress(bytes(65530)),
                                                                        "inflated": 65530}),
    "не сжато, а размер другой": ("heap", lambda body, size: {"stored": zlib.decompress(body)[:len(body)],
                                                               "inflated": size}),
    "вложение: поток обрезан": ("text", lambda body, size: {"stored": body[:len(body) // 2]}),
    "вложение: размер меньше": ("text", lambda body, size: {"inflated": size - 1}),
    "вложение: размер больше блока": ("text", lambda body, size: {"inflated": 0xFFF0}),
    "вложение: бомба": ("noise", lambda body, size: {"stored": zlib.compress(bytes(48 * 1024 * 1024), 9),
                                                     "inflated": 60_000}),
}


@pytest.mark.parametrize("name", list(DAMAGE))
def test_damaged_compressed_block_is_an_error_of_one_item(victim, tmp_path, name):
    copy, blocks, _tmp = victim
    total, items, damaged = _read_victim(copy)
    assert (total, damaged) == (3, 0) and all(item.raw and not item.lost for item in items)
    clean = {item.label: item.raw for item in items}

    which, change = DAMAGE[name]
    target = str(tmp_path / "damaged.ost")
    with open(copy, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    strict = pst_tools.Pst4k(target)
    _ib, _cb, size, _cref = strict.bbt[blocks[which]]
    _rewrite_block(target, blocks[which], **change(strict.stored(blocks[which]), size))

    tracemalloc.start()
    total, items, damaged = _read_victim(target)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert total == 3 and len(items) == 3
    assert peak < 16 * 1024 * 1024                     # «бомба» развернулась бы в 48 МБ
    if which == "heap":                                # не читаются свойства письма — письма нет, остальные целы
        assert [bool(item.error) for item in items] == [True, False, False]
        assert "блок" in items[0].error and not items[0].raw
        assert ("больше допустимого" in items[0].error) == ("больше блока" in name)
    else:                                              # не читается вложение — письмо есть, без этого вложения
        assert not any(item.error for item in items)
        lost = " | ".join(items[0].lost)
        assert ("noise.bin" if which == "noise" else "text.txt") in lost and "не прочитано" in lost
        kept = b"noise.bin" if which == "text" else b"text.txt"
        assert kept in items[0].raw and items[0].raw != clean[items[0].label]
    assert [item.raw for item in items[1:]] == [clean[item.label] for item in items[1:]]
    # Повреждение сосчитано, и один раз. Блок с размером, какого у блока не бывает, не читается
    # вовсе: суммы в нём сверять не с чем (как и в обычном файле).
    assert damaged == (0 if "больше блока" in name else 1)


def test_block_trailer_disagreeing_with_the_index_is_counted_but_read(victim, tmp_path):
    """Трейлер блока повторяет то, что записано о блоке в индексе. Расхождение — повод
    посчитать повреждение, но не терять письмо: данные читаются по индексу."""
    copy, blocks, _tmp = victim
    strict = pst_tools.Pst4k(copy)
    ib, cb, size, _cref = strict.bbt[blocks["heap"]]
    trailer = ib + pst_tools._block_span(cb) - 24
    # размер в файле, номер блока, размер после распаковки, сумма
    for at, fmt, value in ((0, "<H", cb + 1), (8, "<Q", blocks["heap"] + 4), (18, "<H", size + 1), (4, "<I", 1)):
        data = bytearray(strict.data)
        struct.pack_into(fmt, data, trailer + at, value)
        target = tmp_path / "trailer.ost"
        target.write_bytes(data)
        total, items, damaged = _read_victim(str(target))
        assert (total, damaged) == (3, 1) and all(item.raw and not item.error and not item.lost for item in items)


def test_damaged_block_in_coded_file_is_tried_both_ways_and_fails_once(victim, tmp_path):
    """В закодированном файле нераспаковавшийся блок пробуется в обратном порядке; это не
    должно ни спрятать ошибку, ни посчитать повреждение дважды."""
    copy, _blocks, tmp = victim
    for reverse in (False, True):
        target = str(tmp_path / f"coded{int(reverse)}.ost")
        pst_tools.to_4k(str(tmp / "victim.pst"), target, compress=True, method=1, reverse=reverse)
        strict = _strict(target)
        total, items, damaged = _read_victim(target)
        assert (total, damaged) == (3, 0) and all(item.raw and not item.lost for item in items)
        victim_nid = min(nid for nid in strict.nbt if nid & 0x1F == 0x04)
        heap = strict.nbt[victim_nid][0]
        assert strict.bbt[heap][1] < strict.bbt[heap][2]
        body = strict.stored(heap)
        _rewrite_block(target, heap, stored=body[:12] + bytes(len(body) - 12))
        with rd.PstReader(target) as pst:
            pst.folders()
            with pytest.raises(rd.PstReadError, match="блок"):
                pst.open_message(victim_nid)
            assert pst.damaged == 1


class _Blocks(rd.Ndb):
    """Читатель блоков без файла — для блоков, собранных прямо в тесте."""

    def __init__(self, crypt: int) -> None:
        self.crypt, self.damaged = crypt, 0


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("method", [rd.CRYPT_PERMUTE, rd.CRYPT_CYCLIC])
def test_compressed_block_of_a_coded_file(method, reverse):
    """Сжатый блок закодированного файла — при обоих способах кодирования и в обоих порядках.
    Через ``to_4k`` получаются не все сочетания: после циклического кодирования сжимать нечего,
    и блок «закодирован, потом сжат» в файл не попадает. Здесь блоки собраны напрямую.
    Настоящих файлов с таким сочетанием нет: проверяется только согласие с описаниями формата."""
    bid = 0x00A5F0C4                                   # блок данных; у циклического кодирования он же ключ
    for plain in (_lines(1500), random.Random(5).randbytes(3000), b"\x00"):
        coded = pst_tools._encode(plain, bid, method)
        stored = pst_tools._encode(zlib.compress(plain), bid, method) if reverse else zlib.compress(coded)
        assert stored != zlib.compress(plain) and coded != plain
        ndb = _Blocks(method)
        assert ndb._unpack(bid, stored, len(plain), True, True) == plain and ndb.damaged == 0
        # оборванный поток не читается ни в одном порядке, и повреждение сосчитано один раз
        with pytest.raises(rd.PstReadError, match="блок 0xa5f0c4"):
            ndb._unpack(bid, stored[:len(stored) // 2], len(plain), True, True)
        assert ndb.damaged == 1
        with pytest.raises(rd.PstReadError):           # суммы не сошлись ещё до распаковки — уже сосчитано
            ndb._unpack(bid, stored[:len(stored) // 2], len(plain), True, False)
        assert ndb.damaged == 1


def _zero_inflated(path, *, index: bool, trailer: bool) -> tuple:
    """Обнулить размер после распаковки у несжатых блоков — в их записях BBT и (или) в трейлерах:
    так его оставляют сторонние программы. → (сколько блоков несжатых, сколько сжатых)."""
    strict = pst_tools.Pst4k(path)
    data = bytearray(strict.data)
    plain = {bid: ib + pst_tools._block_span(cb) for bid, (ib, cb, inflated, _cref) in strict.bbt.items()
             if cb == inflated}
    if trailer:
        for end in plain.values():                     # в трейлере: размер в файле … размер после распаковки
            assert struct.unpack_from("<H", data, end - 6)[0] == struct.unpack_from("<H", data, end - 24)[0]
            struct.pack_into("<H", data, end - 6, 0)
    leaves = [page for page, ptype, level in strict.pages if ptype == 0x80 and not level]
    for page in leaves if index else ():
        for at in range(page, page + 24 * struct.unpack_from("<H", data, page + 4056)[0], 24):
            if struct.unpack_from("<Q", data, at)[0] in plain:
                struct.pack_into("<H", data, at + 18, 0)
        struct.pack_into("<I", data, page + 4076, pst_tools._crc(bytes(data[page:page + 4072])))
    with open(path, "wb") as fh:
        fh.write(data)
    return len(plain), len(strict.bbt) - len(plain)


@pytest.mark.parametrize("index,trailer", [(True, False), (False, True), (True, True)])
def test_zero_inflated_size_means_not_compressed(box, tmp_path, index, trailer):
    """Ноль на месте размера после распаковки — «блок не сжат»: и в индексе блоков, и в трейлере
    блока, вместе и порознь. Файл читается как прежде, и повреждением это не считается."""
    path, original, _expect, _tmp = box
    copy = str(tmp_path / "zero.ost")
    pst_tools.to_4k(path, copy, compress=True)
    plain, packed = _zero_inflated(copy, index=index, trailer=trailer)
    assert plain >= 10 and packed >= 10                # рядом с несжатыми — сжатые, их размер на месте
    got = _dump(copy)
    assert got["damaged"] == 0 and got == original


# ---------------------------------------------------------------------------
#  Один блок, названный в дереве данных много раз, и кэш блоков
# ---------------------------------------------------------------------------
class _Work:
    """Счёт работы читателя: сколько раз прочитано каждое место файла и сколько было распаковок."""

    def __init__(self, monkeypatch) -> None:
        self.reads = collections.Counter()
        self.inflates = 0
        read, inflate = rd.Ndb._read, rd.inflate

        def counted_read(ndb, offset, size):
            self.reads[offset] += 1
            return read(ndb, offset, size)

        def counted_inflate(data, size):
            self.inflates += 1
            return inflate(data, size)

        monkeypatch.setattr(rd.Ndb, "_read", counted_read)
        monkeypatch.setattr(rd, "inflate", counted_inflate)

    def reset(self) -> None:
        self.reads.clear()
        self.inflates = 0


def _xblock(leaves, size: int = BLOCK) -> bytes:
    """XBLOCK над блоками данных ``leaves`` по ``size`` байт."""
    return struct.pack(f"<BBHI{len(leaves)}Q", 1, 1, len(leaves), len(leaves) * size, *leaves)


def _set_tree(path, tree: int, body: bytes) -> None:
    """Подменить служебный блок дерева данных. Сжатым он кладётся, как и в настоящих файлах,
    когда так занимает меньше места, — большой блок иначе не лёг бы на место прежнего."""
    packed = zlib.compress(body, 9)
    smaller = pst_tools._block_span(len(packed)) < pst_tools._block_span(len(body))
    _rewrite_block(path, tree, stored=packed if smaller else body, inflated=len(body))


def test_block_repeated_all_over_a_data_tree_is_an_error_not_a_flood(victim, tmp_path, monkeypatch):
    """XBLOCK, в котором все 8188 ссылок указывают на один сжатый блок, делал бы из 64 КБ файла
    536 МБ вложения: тысячи чтений и распаковок одного блока, пока сборка письма не упрётся в
    свой предел. В настоящих файлах блок в дереве не повторяется — это ошибка одного вложения,
    и до чтения самого блока."""
    copy, blocks, _tmp = victim
    work = _Work(monkeypatch)
    total, items, damaged = _read_victim(copy)
    assert (total, damaged) == (3, 0) and work.inflates >= 4
    clean = {item.label: item.raw for item in items}
    clean_inflates = work.inflates

    target = str(tmp_path / "flood.ost")
    shutil.copyfile(copy, target)
    count = (BLOCK - 8) // 8
    _set_tree(target, blocks["text_tree"], _xblock([blocks["text"]] * count))
    strict = pst_tools.Pst4k(target)
    stored, size = strict.bbt[blocks["text_tree"]][1:3]
    assert count == 8188 and size == BLOCK and stored < 488    # XBLOCK полон, а в файле он — сотня байт
    leaf_at = strict.bbt[blocks["text"]][0]

    work.reset()
    tracemalloc.start()
    total, items, damaged = _read_victim(target)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    # Работа ограничена: повторённый блок не прочитан ни разу, распаковок меньше, чем в целом
    # файле (блоки вложения не читались), и в памяти ничего не накоплено. У читателя без предела
    # повторов и без кэша блоков здесь было 2995 чтений этого блока, столько же распаковок и 269 МБ.
    assert work.reads[leaf_at] == 0 and work.inflates < clean_inflates, (work.reads[leaf_at], work.inflates)
    assert peak < 16 * 1024 * 1024
    assert (total, len(items), damaged) == (3, 3, 0)   # суммы в файле сходятся: это не «битый блок»
    assert not any(item.error for item in items)
    lost = " | ".join(items[0].lost)
    assert "text.txt" in lost and "не прочитано" in lost and f"больше {rd.MAX_LEAF_REPEATS} раз" in lost
    assert b"noise.bin" in items[0].raw and b"text.txt" not in items[0].raw
    assert [item.raw for item in items[1:]] == [clean[item.label] for item in items[1:]]


def test_block_named_several_times_is_read_once(victim, tmp_path, monkeypatch):
    """Несколько ссылок на один блок (порча задела запись XBLOCK) читаются: блок берётся из
    файла один раз, и его повреждение считается один раз. Сверх допустимого — ошибка."""
    copy, blocks, _tmp = victim
    leaf, tree, limit = blocks["text"], blocks["text_tree"], rd.MAX_LEAF_REPEATS
    target = str(tmp_path / "repeat.ost")
    shutil.copyfile(copy, target)
    strict = pst_tools.Pst4k(target)
    plain = strict.block(leaf)
    leaf_at, stored, size, _cref = strict.bbt[leaf]
    assert stored < size == len(plain) == BLOCK        # блок сжат
    work = _Work(monkeypatch)

    def read(leaves):
        _set_tree(target, tree, _xblock(leaves))
        work.reset()
        with rd.PstReader(target) as pst:
            out = list(pst.ndb.tree_blocks(tree))
            assert pst.ndb.tree_bytes(tree, limit * BLOCK) == b"".join(out)    # и второй раз подряд
            return out, pst.damaged

    assert read([leaf] * limit) == ([plain] * limit, 0)
    assert work.reads[leaf_at] == 1 and work.inflates == 1
    assert read([leaf, leaf | 1] * (limit // 2)) == ([plain] * limit, 0)      # младший бит номера не значим
    assert work.reads[leaf_at] == 1 and work.inflates == 1
    for leaves in ([leaf] * (limit + 1), [leaf, leaf | 1] * limit, [leaf] * limit + [leaf | 1]):
        with pytest.raises(rd.PstReadError, match=f"больше {limit} раз"):
            read(leaves)
        assert work.reads[leaf_at] == 0 and work.inflates == 0                 # ошибка — до чтения блоков

    # сумма блока не сходится: он читается, как и прежде, а повреждение одно, не по числу ссылок
    _set_tree(target, tree, _xblock([leaf] * limit))
    data = bytearray(open(target, "rb").read())
    struct.pack_into("<I", data, leaf_at + pst_tools._block_span(stored) - 20, 1)
    with open(target, "wb") as fh:
        fh.write(data)
    work.reset()
    with rd.PstReader(target) as pst:
        assert list(pst.ndb.tree_blocks(tree)) == [plain] * limit and pst.damaged == 1
        assert work.reads[leaf_at] == 1


def test_repeats_are_counted_over_the_whole_tree(victim, tmp_path, monkeypatch):
    """Счёт повторов — по всему дереву, а не по одному XBLOCK: XXBLOCK, который много раз
    называет один и тот же XBLOCK, повторяет и все его блоки."""
    _copy, _blocks, tmp = victim
    target = str(tmp_path / "wide.ost")
    pst_tools.to_4k(str(tmp / "victim.pst"), target, compress=True, fanout=2)
    strict = _strict(target)
    tree = _attachment_trees(strict, min(nid for nid in strict.nbt if nid & 0x1F == 0x04))[0]
    top = strict.block(tree)
    assert top[:2] == b"\x01\x02"                      # XXBLOCK: в XBLOCK здесь по две ссылки
    xblock = struct.unpack_from("<Q", top, 8)[0]
    leaves = struct.unpack_from("<2Q", strict.block(xblock), 8)
    parts = [strict.block(leaf) for leaf in leaves]
    places = [strict.bbt[bid & ~1][0] for bid in (xblock, *leaves)]
    limit = rd.MAX_LEAF_REPEATS
    work = _Work(monkeypatch)

    def again(times: int) -> None:
        body = struct.pack(f"<BBHI{times}Q", 1, 2, times, times * sum(map(len, parts)), *[xblock] * times)
        _rewrite_block(target, tree, stored=body, inflated=len(body))
        work.reset()

    again(limit)
    with rd.PstReader(target) as pst:
        assert list(pst.ndb.tree_blocks(tree)) == parts * limit and pst.damaged == 0
    assert [work.reads[place] for place in places] == [1, 1, 1]        # каждый блок взят из файла один раз
    again(limit + 1)
    with rd.PstReader(target) as pst:
        got = []
        with pytest.raises(rd.PstReadError, match=f"больше {limit} раз"):
            for chunk in pst.ndb.tree_blocks(tree):
                got.append(chunk)
        assert got == parts * limit                    # ошибка — на девятом заходе, не раньше
    assert [work.reads[place] for place in places] == [1, 1, 1]


def test_repeat_count_itself_is_bounded(monkeypatch):
    """Счёт повторов следит за ограниченным числом блоков дерева: иначе в дереве из миллионов
    блоков сам счёт занял бы сотни мегабайт."""
    monkeypatch.setattr(rd, "MAX_TRACKED_LEAVES", 100)
    seen = {}
    rd.Ndb._count_leaves(0x12, range(4, 4 + 4 * 5000, 4), seen)
    assert len(seen) == 100 and set(seen.values()) == {1}
    for _ in range(5 * rd.MAX_LEAF_REPEATS):           # блок за пределом счёта: повторы не видны
        rd.Ndb._count_leaves(0x12, [4 * 4000], seen)
    assert len(seen) == 100
    rd.Ndb._count_leaves(0x12, [8] * (rd.MAX_LEAF_REPEATS - 1), seen)   # а этот — в счёте с первого раза
    with pytest.raises(rd.PstReadError, match="блок 0x12: .* блок 0x8 "):
        rd.Ndb._count_leaves(0x12, [8], seen)


def test_block_cache_is_small_and_bounded(victim, monkeypatch):
    """Кэш блоков — несколько последних: блок из него не читается заново, вытесненный — читается,
    а памяти он занимает не больше нескольких блоков, сколько бы ни прочитали."""
    copy, _blocks, _tmp = victim
    strict = pst_tools.Pst4k(copy)
    bids = list(strict.bbt)[:rd.BLOCK_CACHE + 3]
    places = [strict.bbt[bid][0] for bid in bids]
    assert len(bids) == rd.BLOCK_CACHE + 3 and 4 <= rd.BLOCK_CACHE <= 16
    work = _Work(monkeypatch)
    with rd.PstReader(copy) as pst:
        cache = pst.ndb._blocks
        work.reset()
        for bid in bids:
            assert pst.ndb.block(bid) == strict.block(bid)
        assert len(cache) == rd.BLOCK_CACHE and [work.reads[place] for place in places] == [1] * len(bids)
        for bid in bids[3:] * 2:                       # последние — из памяти, файл не тронут
            assert pst.ndb.block(bid) == strict.block(bid)
        assert [work.reads[place] for place in places] == [1] * len(bids)
        pst.ndb.block(bids[3])                         # к самому давнему из них обратились ещё раз…
        pst.ndb.block(bids[0])                         # …и новый блок вытесняет не его, а следующий по давности
        pst.ndb.block(bids[3])
        assert [work.reads[place] for place in places[:5]] == [2, 1, 1, 1, 1] and len(cache) == rd.BLOCK_CACHE
        pst.ndb.block(bids[4])
        assert work.reads[places[4]] == 2 and len(cache) == rd.BLOCK_CACHE
        _total, items = importer.builtin_source(pst)
        assert len(list(items)) == 3
        assert len(cache) <= rd.BLOCK_CACHE and sum(map(len, cache.values())) <= rd.BLOCK_CACHE * BLOCK
        assert pst.damaged == 0


def test_random_damage_never_crashes_or_hangs(tmp_path):
    """То же, что и для обычного файла: любая порча кончается понятной ошибкой письма или
    файла — не падением, не зависанием и не распаковкой без предела."""
    small = _export(tmp_path, [_mail("INBOX", b"Subject: a\r\nTo: x@y.z\r\n\r\nbody one " + _lines(300)),
                               _mail("INBOX/Sub", b"Subject: b\r\n\r\nbody two" + b"z" * 20000)], "small.pst")
    copy = str(tmp_path / "small.ost")
    pst_tools.to_4k(small, copy, compress=True)
    strict = _strict(copy)
    assert sum(1 for _ib, cb, inflated, _cref in strict.bbt.values() if cb != inflated) >= 2
    original = open(copy, "rb").read()
    first = min(ib for ib, _cb, _inflated, _cref in strict.bbt.values())
    rnd = random.Random(8)
    started = time.monotonic()
    opened = failed = 0
    for round_no in range(300):
        data = bytearray(original)
        for _ in range(rnd.choice((1, 1, 2, 5, 20))):
            pos = rnd.randrange(first if round_no % 4 else 0, len(data))
            width = rnd.choice((1, 1, 2, 4, 8))
            data[pos:pos + width] = rnd.randbytes(width) if rnd.random() < 0.7 else b"\xff" * width
        target = tmp_path / "fuzz.ost"
        target.write_bytes(data)
        try:
            pst = rd.PstReader(str(target))
        except importer._MESSAGE_ERRORS:
            continue                                   # файл не открылся — так и скажет задание
        with pst:
            try:
                _total, items = importer.builtin_source(pst)
            except importer._MESSAGE_ERRORS:
                continue
            opened += 1
            failed += sum(1 for item in items if item.error or item.lost)      # перебор писем не бросает никогда
    assert opened > 100 and failed > 5                 # порча и вправду доходит до писем
    assert time.monotonic() - started < 120


# ---------------------------------------------------------------------------
#  Адрес отправителя Exchange — из других мест того же письма
# ---------------------------------------------------------------------------
#: Таблица получателей с адресом SMTP (0x39FE), как её пишет Outlook для ящика Exchange.
LAYOUT_EXCHANGE = pf.TableLayout((28, 28, 28, 29), pf._ROW_KEYS + [
    (0x0C150003, 8, 4, 2), (0x3001001F, 12, 4, 3), (0x3002001F, 16, 4, 4), (0x3003001F, 20, 4, 5),
    (0x39FE001F, 24, 4, 6)])
DN = "/o=Org/ou=Exchange Administrative Group (FYDIBOHF23SPDLT)/cn=Recipients/cn="


def _exchange(kind, name, cn, smtp=None):
    return {0x0C150003: kind, 0x3001001F: name, 0x3002001F: "EX", 0x3003001F: DN + cn, 0x39FE001F: smtp}


def _entry_id(cn: str) -> bytes:
    """Идентификатор записи адресной книги Exchange — как в свойствах отправителя."""
    provider = bytes.fromhex("dca740c8c042101ab4b908002b2fe182")
    return bytes(4) + provider + bytes(8) + (DN + cn).upper().encode() + b"\x00"


def test_exchange_sender_gets_the_address_the_message_has(tmp_path, monkeypatch):
    """У отправителя с адресом Exchange своего адреса SMTP в письме может не быть. Заглушка
    ставится, только если настоящего адреса того же лица нет нигде в письме."""
    crafted = _Crafted(str(tmp_path / "exchange.pst"), monkeypatch)
    people = [_exchange(1, "Иванов Иван", "ivanov", "ivanov@example.ru"),
              _exchange(2, "Директор", "DIRECTOR", "Director@Example.ru"),
              _exchange(2, "Без адреса", "nobody")]

    def add(subject, *props):
        return crafted.add(_note(subject, (0x1000, pf.PT_UNICODE, "текст"), *props), recipients=people,
                           layout=LAYOUT_EXCHANGE)

    def sender(cn):
        return [(0x0042, pf.PT_UNICODE, "Директор"), (0x0064, pf.PT_UNICODE, "EX"), (0x0065, pf.PT_UNICODE, DN + cn),
                (0x0C1A, pf.PT_UNICODE, "Директор"), (0x0C1E, pf.PT_UNICODE, "EX"), (0x0C1F, pf.PT_UNICODE, DN + cn)]

    # отправитель есть среди получателей (копия себе): адрес Exchange тот же, регистр букв другой
    add("себе в копию", *sender("director"))
    # адрес SMTP отправителя в самом письме важнее найденного
    add("свой адрес", *sender("director"), (0x5D02, pf.PT_UNICODE, "boss@example.ru"),
        (0x5D01, pf.PT_UNICODE, "boss@example.ru"))
    # среди получателей его нет, или он там без адреса SMTP — остаётся заглушка
    add("чужой", *sender("stranger"))
    add("без адреса", *sender("nobody"))
    # похожий, но другой адрес Exchange — не тот же человек
    add("похожий", *sender("director2"))
    # исходные заголовки с полем From: искать нечего, таблица получателей не читается вовсе
    add("из заголовков", *sender("director"), (0x007D, pf.PT_UNICODE, "From: real@example.com\r\n"
                                                                    "To: someone@example.com\r\n"))
    # «Отправленные» файла .ost: адрес владельца ящика записан у создавшего письмо
    add("создавший", *sender("owner"), (0x4023, pf.PT_UNICODE, (DN + "owner").upper().replace(" (", "(")),
        (0x5D0A, pf.PT_UNICODE, "owner@example.ru"))
    # создал письмо кто-то другой — его адрес отправителю не подходит
    add("чужой создавший", *sender("owner"), (0x4023, pf.PT_UNICODE, DN + "other"),
        (0x5D0A, pf.PT_UNICODE, "other@example.ru"), (0x4025, pf.PT_UNICODE, DN + "owner"))
    # под видом «EX» записан обычный адрес, а у «от имени» — только имя и тот же идентификатор записи
    add("адрес под видом EX", (0x0042, pf.PT_UNICODE, "Владелец"), (0x0041, pf.PT_BINARY, _entry_id("owner")),
        (0x0C1A, pf.PT_UNICODE, "Владелец"), (0x0C1E, pf.PT_UNICODE, "EX"), (0x0C1F, pf.PT_UNICODE, "owner@example.ru"),
        (0x0C19, pf.PT_BINARY, _entry_id("owner")))
    # то же, но «от имени» — другое лицо: его заглушка остаётся, настоящий адрес — в Sender
    add("за другого", (0x0042, pf.PT_UNICODE, "Начальник"), (0x0041, pf.PT_BINARY, _entry_id("chief")),
        (0x0C1A, pf.PT_UNICODE, "Владелец"), (0x0C1E, pf.PT_UNICODE, "EX"), (0x0C1F, pf.PT_UNICODE, "owner@example.ru"),
        (0x0C19, pf.PT_BINARY, _entry_id("owner")))
    # путь в каталоге Exchange — не адрес, даже если в нём есть «@»
    add("путь с собакой", (0x0C1A, pf.PT_UNICODE, "Гость"), (0x0C1E, pf.PT_UNICODE, "EX"),
        (0x0C1F, pf.PT_UNICODE, "/o=Org/cn=Recipients/cn=guest@host"))
    path = crafted.close()
    copy = str(tmp_path / "exchange.ost")
    pst_tools.to_4k(path, copy, compress=True)

    reads = []
    original = rd.Message.recipients

    def recipients(self):
        reads.append(self.nid)
        return original(self)

    monkeypatch.setattr(rd.Message, "recipients", recipients)
    for name in (path, copy):
        del reads[:]
        mails = {}
        with rd.PstReader(name) as pst:
            for folder in pst.folders():
                for nid in pst.message_nids(folder.nid):
                    built = eml.build_eml(pst.open_message(nid))
                    assert not built.lost
                    mails[built.subject] = (nid, _parse(built.raw))

        def author(subject):
            return mails[subject][1]["From"].addresses[0]

        def second(subject):
            return mails[subject][1]["Sender"]

        assert author("себе в копию").addr_spec == "Director@Example.ru"
        assert author("себе в копию").display_name == "Директор"
        assert second("себе в копию") is None                      # отправитель и «от имени» — один адрес
        assert [a.addr_spec for a in mails["себе в копию"][1]["To"].addresses] == ["ivanov@example.ru"]
        assert [a.addr_spec for a in mails["себе в копию"][1]["Cc"].addresses] == ["Director@Example.ru",
                                                                                     "nobody@exchange.invalid"]
        assert author("свой адрес").addr_spec == "boss@example.ru"
        assert author("чужой").addr_spec == "stranger@exchange.invalid"
        assert author("без адреса").addr_spec == "nobody@exchange.invalid"
        assert author("похожий").addr_spec == "director2@exchange.invalid"
        assert author("из заголовков").addr_spec == "real@example.com"
        assert author("создавший").addr_spec == "owner@example.ru" and second("создавший") is None
        assert author("чужой создавший").addr_spec == "owner@exchange.invalid"
        assert author("адрес под видом EX").addr_spec == "owner@example.ru"
        assert author("адрес под видом EX").display_name == "Владелец" and second("адрес под видом EX") is None
        assert author("за другого").display_name == "Начальник"
        assert author("за другого").addr_spec.endswith("@exchange.invalid")
        assert second("за другого").addresses[0].addr_spec == "owner@example.ru"
        assert author("путь с собакой").addr_spec == "guesthost@exchange.invalid"
        # таблица получателей читается один раз на письмо, а где From и To уже есть — ни разу
        assert sorted(reads) == sorted(nid for subject, (nid, _msg) in mails.items() if subject != "из заголовков")


def _built(tmp_path, monkeypatch, letters, recipients):
    """Собрать письма из свойств: {тема: свойства} → {тема: разобранное письмо}."""
    crafted = _Crafted(str(tmp_path / "senders.pst"), monkeypatch)
    for subject, props in letters.items():
        crafted.add(_note(subject, (0x1000, pf.PT_UNICODE, "текст"), *props), recipients=recipients,
                    layout=LAYOUT_EXCHANGE)
    out = {}
    with rd.PstReader(crafted.close()) as pst:
        for folder in pst.folders():
            for nid in pst.message_nids(folder.nid):
                built = eml.build_eml(pst.open_message(nid))
                assert not built.lost
                out[built.subject] = _parse(built.raw)
    assert out.keys() == letters.keys()
    return out


def _person(name, kind, address, slot):
    """Свойства отправителя (``slot`` 0x0C1A) или лица, от чьего имени письмо (0x0042)."""
    kind_pid, address_pid = (0x0C1E, 0x0C1F) if slot == 0x0C1A else (0x0064, 0x0065)
    return [(slot, pf.PT_UNICODE, name), (kind_pid, pf.PT_UNICODE, kind), (address_pid, pf.PT_UNICODE, address)]


#: Где в письме адрес Exchange записан рядом с адресом SMTP того же лица: отправитель, «от имени»,
#: получивший, «для кого получено», создавший письмо, изменивший его последним.
EXCHANGE_PAIRS = [(0x0C1F, 0x5D01), (0x0065, 0x5D02), (0x0076, 0x5D07), (0x0078, 0x5D08),
                  (0x4023, 0x5D0A), (0x4025, 0x5D0B)]


@pytest.mark.parametrize("address,smtp", EXCHANGE_PAIRS, ids=lambda pid: f"{pid:#06x}")
def test_exchange_sender_address_is_found_in_each_place(tmp_path, monkeypatch, address, smtp):
    """Адрес SMTP отправителя записан в письме ровно в одном месте — в проверяемой паре свойств;
    в остальных парах — другое лицо со своим адресом, среди получателей отправителя нет."""
    owner = DN + "owner"
    props = _person("Владелец", "EX", owner, 0x0042) + _person("Владелец", "EX", owner.upper(), 0x0C1A)
    for other_address, other_smtp in EXCHANGE_PAIRS:
        if other_smtp == smtp:
            props.append((smtp, pf.PT_UNICODE, "Owner@Example.ru"))
            if address not in (0x0065, 0x0C1F):
                props.append((address, pf.PT_UNICODE, owner.upper()))  # регистр букв в адресе Exchange не важен
        elif other_address not in (0x0065, 0x0C1F):
            props += [(other_address, pf.PT_UNICODE, DN + "other"), (other_smtp, pf.PT_UNICODE, "other@example.ru")]
    mail = _built(tmp_path, monkeypatch, {"письмо": props},
                  [_exchange(1, "Иванов Иван", "ivanov", "ivanov@example.ru")])["письмо"]
    assert mail["From"].addresses[0].addr_spec == "Owner@Example.ru"
    assert mail["From"].addresses[0].display_name == "Владелец"
    assert mail["Sender"] is None                      # адрес один и у отправителя, и у «от имени»
    # без этой пары адреса взять неоткуда — выходит заглушка: пара и вправду решает
    props = [prop for prop in props if prop[0] != smtp]
    mail = _built(tmp_path, monkeypatch, {"письмо": props},
                  [_exchange(1, "Иванов Иван", "ivanov", "ivanov@example.ru")])["письмо"]
    assert mail["From"].addresses[0].addr_spec == "owner@exchange.invalid" and mail["Sender"] is None


def test_sender_addresses_are_merged_only_for_one_person(tmp_path, monkeypatch):
    """Настоящий адрес одного из двух (отправитель и «от имени») переходит другому, только если
    это одно лицо — идентификаторы записи есть и равны — и настоящий адрес ровно один."""
    chief = _person("Начальник", "EX", DN + "chief", 0x0042)
    clerk = _person("Секретарь", "SMTP", "clerk@example.ru", 0x0C1A)
    same = [(0x0041, pf.PT_BINARY, _entry_id("chief")), (0x0C19, pf.PT_BINARY, _entry_id("chief"))]
    empty = [(0x0041, pf.PT_BINARY, b""), (0x0C19, pf.PT_BINARY, b"")]
    nobody = [(0x0C1A, pf.PT_UNICODE, "Некто"), (0x0C1E, pf.PT_UNICODE, "EX")]
    mails = _built(tmp_path, monkeypatch, {
        "одно лицо": chief + clerk + same,
        "одно лицо, адрес у первого": _person("Начальник", "SMTP", "chief@example.ru", 0x0042)
        + _person("Начальник", "EX", DN + "chief", 0x0C1A) + same,
        "идентификаторов нет": chief + clerk,
        "идентификаторы пусты": chief + clerk + empty,
        "оба адреса настоящие": _person("Отдел", "SMTP", "team@example.ru", 0x0042) + clerk + same,
        "оба без адреса": chief + _person("Начальник", "EX", DN + "chief", 0x0C1A) + same,
        # у отправителя адреса Exchange нет вовсе: «пустой адрес» ни с чьим не совпадает
        "пустой адрес, чужой создавший": nobody + [(0x5D0A, pf.PT_UNICODE, "creator@example.ru")],
        "пустой адрес, чужой получатель": nobody,
    }, [_exchange(1, "Иванов Иван", "ivanov", "ivanov@example.ru"),
        {0x0C150003: 2, 0x3001001F: "Без пути", 0x3002001F: "EX", 0x3003001F: None, 0x39FE001F: "stray@example.ru"}])

    def who(subject):
        mail = mails[subject]
        sender = mail["Sender"].addresses[0].addr_spec if mail["Sender"] else None
        return mail["From"].addresses[0].addr_spec, sender

    assert who("одно лицо") == ("clerk@example.ru", None)
    assert who("одно лицо, адрес у первого") == ("chief@example.ru", None)
    assert who("идентификаторов нет") == ("chief@exchange.invalid", "clerk@example.ru")
    assert who("идентификаторы пусты") == ("chief@exchange.invalid", "clerk@example.ru")
    assert who("оба адреса настоящие") == ("team@example.ru", "clerk@example.ru")
    assert who("оба без адреса") == ("chief@exchange.invalid", None)
    for subject in ("пустой адрес, чужой создавший", "пустой адрес, чужой получатель"):
        author = mails[subject]["From"].addresses[0]
        assert author.display_name == "Некто" and author.addr_spec.endswith("@exchange.invalid")
        assert mails[subject]["Sender"] is None
        assert [a.addr_spec for a in mails[subject]["Cc"].addresses] == ["stray@example.ru"]


# ---------------------------------------------------------------------------
#  Номер кусочка в куче: оба известных способа деления
# ---------------------------------------------------------------------------
def _heap_block(first: bool, pieces) -> bytes:
    """Блок кучи: заголовок, кусочки, карта (число кусочков, свободных, смещения)."""
    head = struct.pack("<HBBI", 0, 0xEC, 0xBC, 0x20) + bytes(4) if first else struct.pack("<H", 0)
    body = bytearray(head)
    offsets = [len(body)]
    for piece in pieces:
        body += piece
        offsets.append(len(body))
    if len(body) % 2:
        body += b"\x00"
    struct.pack_into("<H", body, 0, len(body))
    return bytes(body) + struct.pack(f"<HH{len(offsets)}H", len(pieces), 0, *offsets)


class _FakeNdb:
    """Ровно то, что куче нужно от файла: блоки дерева данных и раскладка."""

    def __init__(self, blocks, bits):
        self._blocks = blocks
        self.layout = type("Layout", (), {"hid_index_bits": bits})()

    def tree_blocks(self, _bid):
        return iter(self._blocks)


def _heap(blocks, bits=14):
    return rd.Heap(_FakeNdb(blocks, bits), 1)


def _blocks(count, per_block=3):
    """Куча из ``count`` блоков; кусочек j блока i — байты «Bi.j»."""
    return [_heap_block(i == 0, [f"B{i}.{j}".encode() for j in range(per_block)]) for i in range(count)]


def _narrow(block, index):                  # номер, как его пишут обычные файлы: 11 бит на кусочек
    return (block << 16) | (index << 5)


def _wide(block, index):                    # деление 14/13
    return (block << 19) | (index << 5)


def test_heap_id_is_read_with_either_known_split():
    """Куча из нескольких блоков в файле с большими блоками читается при обоих делениях номера.

    Настоящих файлов с такой кучей нет, а открытые читатели делят номер по-разному: 14 бит на
    номер кусочка (номер блока — с 19-го бита) или, как в обычных файлах, 11 бит (блок — с 16-го).
    Номер, верный лишь при одном делении, читается однозначно и показывает деление кучи.
    """
    blocks = [_heap_block(True, [b"A0", b"A1"]), _heap_block(False, [b"B0", b"B1", b"B2"]),
              _heap_block(False, [b"C0"])]
    wide = _heap(blocks)
    assert wide.get(0x20) == b"A0" and wide.get(0x40) == b"A1"
    assert wide.get(_wide(1, 3)) == b"B2" and wide.get(_wide(2, 1)) == b"C0"
    narrow = _heap(blocks)                      # те же блоки, номера записаны как в обычных файлах
    assert narrow.get(0x40) == b"A1"            # первый блок: оба деления дают одно и то же
    assert narrow.get(_narrow(1, 3)) == b"B2"
    assert narrow.get(_narrow(2, 1)) == b"C0" and narrow.get(0x20) == b"A0"
    for bad in (_wide(1, 4), _wide(3, 1), _narrow(3, 1), 0x60, 0x21):    # нет ни при каком делении
        with pytest.raises(rd.PstReadError):
            _heap(blocks).get(bad)
    plain = _heap(blocks, 11)                   # обычный файл: только 11 бит, «широкий» номер — ошибка
    assert plain.get(_narrow(1, 1)) == b"B0"
    with pytest.raises(rd.PstReadError):
        plain.get(_wide(1, 1))


def test_ambiguous_heap_id_is_never_read_by_guess():
    """Номер девятого блока при делении 11/16 при делении 14/13 указывает во второй блок.

    Раньше такой номер молча читался как кусочек второго блока (подмена данных). Теперь он
    читается только тогда, когда деление кучи известно по другим её номерам, а иначе — отказ.
    """
    blocks = _blocks(10)
    tricky = _narrow(8, 2)                      # = _wide(1, 2): разные кусочки при разных делениях
    assert tricky == _wide(1, 2)
    with pytest.raises(rd.PstReadError, match="двояко"):
        _heap(blocks).get(tricky)
    # деление видно по номеру, который верен лишь при 11/16
    heap = _heap(blocks)
    assert heap.get(_narrow(3, 1)) == b"B3.0"
    assert heap.get(tricky) == b"B8.1"
    # … или лишь при 14/13
    heap = _heap(blocks)
    assert heap.get(_wide(9, 1)) == b"B9.0"
    assert heap.get(tricky) == b"B1.1"
    # номера, переданные заранее (так делают набор свойств и таблица), — то же самое
    heap = _heap(blocks)
    heap.vote([0, 0x21, _narrow(5, 2), _narrow(8, 1)])        # неоднозначные и пустые не считаются
    assert heap.get(tricky) == b"B8.1"
    # голоса спорят — двусмысленный номер не читается, однозначные читаются как прежде
    heap = _heap(blocks)
    assert heap.get(_narrow(2, 1)) == b"B2.0" and heap.get(_wide(9, 3)) == b"B9.2"
    with pytest.raises(rd.PstReadError):
        heap.get(tricky)
    assert heap.get(0x40) == b"B0.1"
    # проверка содержимого снимает двусмысленность, если подходит ровно один кусочек
    heap = _heap(blocks)
    assert heap.get(tricky, lambda d: d.startswith(b"B8")) == b"B8.1"
    assert heap.get(_narrow(8, 3)) == b"B8.2"                 # и запоминается как деление кучи
    with pytest.raises(rd.PstReadError):
        _heap(blocks).get(tricky, lambda d: True)             # подходят оба — отказ


def test_heap_block_with_more_than_2047_pieces_means_wide_split():
    """Блок, где кусочков больше 2047, при делении 11/16 целиком не адресуется."""
    blocks = [_heap_block(True, [b"x"] * 2100)] + _blocks(10)[1:]
    heap = _heap(blocks)
    assert heap.get(_wide(1, 2)) == b"B1.1"                   # двусмысленный номер — по делению 14/13
    assert heap.get(_wide(0, 2100)) == b"x"


@pytest.mark.parametrize("bits", [14, 11])
def test_property_heap_of_many_blocks_reads_with_either_split(tmp_path, monkeypatch, bits):
    """Набор свойств письма в куче из 10+ блоков по 64 КБ: читается одинаково при обоих делениях."""
    crafted = _Crafted(str(tmp_path / "wide.pst"), monkeypatch)
    rnd = random.Random(7)
    blobs = {0x6800 + i: rnd.randbytes(3000) for i in range(230)}
    nid = crafted.add(_note("очень много свойств", (0x1000, pf.PT_UNICODE, "тело"),
                            *[(pid, pf.PT_BINARY, blob) for pid, blob in blobs.items()]))
    path = crafted.close()
    monkeypatch.setattr(pst_tools, "HID_4K_INDEX_BITS", bits)
    copy = str(tmp_path / f"wide{bits}.ost")
    pst_tools.to_4k(path, copy, compress=True)
    strict = _strict(copy)
    data, _sub, _parent = strict.nbt[nid]
    assert max(hid >> (5 + bits) for hid in strict.heap(data)) >= 9      # куча правда из 10+ блоков
    got = _dump(copy)
    assert got["damaged"] == 0
    mail = got["messages"][nid]
    assert all(mail["props"][pid] == (pf.PT_BINARY, blob) for pid, blob in blobs.items())
    assert got["messages"][nid]["raw"] == _dump(path)["messages"][nid]["raw"]
