"""Встроенный импорт .pst: чтение формата, сборка писем, заливка на сервер и в локальную копию.

Файлы для чтения создаёт писатель службы (``export.pst_native``), а из них
``pst_tools`` получает другие виды того же файла — «зашифрованные» и в формате
ANSI. Письма с редкими свойствами (без исходных заголовков, с телом в RTF,
вложенные письма) пишутся прямо средствами ``export.pstformat``.
"""
import email
import hashlib
import os
import random
import shutil
import struct
import time
from email import policy
from email.message import EmailMessage

import pytest

import pst_tools
from mailarchiver.export import pst_native, pstformat as pf
from mailarchiver.export.base import MailItem
from mailarchiver.pstimport import eml, importer, reader as rd, rtf

DATE = 1_756_700_000                      # 1 сентября 2025 года


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mail(folder, raw, flags=(), date=DATE):
    return MailItem(folder=folder, raw=raw, flags=list(flags), internaldate=date, size=len(raw))


def _export(tmp_path, items, name="box.pst"):
    out = str(tmp_path / name)
    res = pst_native.NativePstExportEngine().export(iter(items), out, options={"tmp_dir": str(tmp_path)},
                                                    total_hint=len(items))
    assert res.errors == 0
    return out


def _read_all(path, **kw):
    """Все письма файла: {(путь папки, тема): собранное письмо}."""
    out = {}
    with rd.PstReader(path, **kw) as pst:
        for folder in pst.folders():
            for nid in pst.message_nids(folder.nid):
                built = eml.build_eml(pst.open_message(nid))
                out[("/".join(folder.path), built.subject)] = built
    return out


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


def _defects(msg) -> list:
    """Дефекты письма и всех его частей, включая заголовки и вложенные письма."""
    found = []
    for part in msg.walk():
        found += list(part.defects)
        for name, value in part.items():
            for defect in getattr(value, "defects", ()):
                # Имя файла в Content-Type пишется словами RFC 2047 в кавычках намеренно: так
                # его понимает Outlook (и так же пишет Thunderbird); по букве стандарта это
                # замечание, имя при этом читается верно.
                if name.lower() == "content-type" and "encoded" in str(defect) and "name=" in str(part.get(name)):
                    continue
                found.append(defect)
    return found


def _text(part) -> str:
    """Текст части письма с переводами строк «\n» (в письме они, как положено, CRLF)."""
    return part.get_content().replace("\r\n", "\n").strip()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """Ящик «как в жизни», выгруженный в .pst, и то, что в нём должно быть."""
    tmp = tmp_path_factory.mktemp("pstimport")
    rnd = random.Random(11)
    expect = {}
    items = []

    m = EmailMessage()
    m["From"] = "Иван Петров <ivan@example.ru>"
    m["To"] = "Мария <maria@example.ru>, bob@example.com"
    m["Cc"] = '"Сидоров, Пётр" <petr@example.ru>'
    m["Subject"] = "Отчёт за сентябрь"
    m["Date"] = "Mon, 01 Sep 2025 10:00:00 +0300"
    m["Message-ID"] = "<rep1@example.ru>"
    m["X-Custom"] = "keep me"
    m.set_content("Текстовая версия отчёта.\nВторая строка.")
    m.add_alternative('<html><body><p>HTML <b>отчёт</b></p><img src="cid:logo1@x"></body></html>',
                      subtype="html")
    logo = rnd.randbytes(5000)
    m.get_payload()[1].add_related(logo, maintype="image", subtype="png", cid="<logo1@x>")
    pdf = rnd.randbytes(20000)
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="Отчёт сентябрь 2025.pdf")
    items.append(_mail("INBOX", m.as_bytes(), ["\\Seen", "\\Answered"]))
    expect.update(logo=_sha(logo), pdf=_sha(pdf))

    m = EmailMessage()
    m["From"] = "boss@example.com"
    m["To"] = "me@example.ru"
    m["Subject"] = "Fwd: большой файл"
    m["Message-ID"] = "<big@example.com>"
    m.set_content("Смотри вложение")
    big = rnd.randbytes(9 * 1024 * 1024 + 7)          # > 1021 блоков — дерево XXBLOCK
    m.add_attachment(big, maintype="application", subtype="octet-stream", filename="big.bin")
    inner = EmailMessage()
    inner["From"] = "x@y.z"
    inner["Subject"] = "Внутреннее письмо"
    inner.set_content("Привет из вложенного письма")
    m.add_attachment(inner)
    items.append(_mail("INBOX/Проекты", m.as_bytes(), ["\\Seen", "\\Flagged", "$Forwarded"]))
    expect["big"] = _sha(big)

    html = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
            '</head><body>Только HTML: привет</body></html>').encode("cp1251")
    raw = (b"From: a@b.c\r\nTo: d@e.f\r\nBcc: secret@e.f\r\nSubject: html only\r\nMIME-Version: 1.0\r\n"
           b"Message-ID: <html@b.c>\r\nContent-Type: text/html; charset=windows-1251\r\n\r\n" + html)
    items.append(_mail("Trash", raw, ["\\Seen"]))
    expect["html"] = html

    items.append(_mail("Drafts", b"Subject: draft\r\nTo: x@y.z\r\n\r\nnot sent yet", ["\\Draft"]))
    for i in range(40):                               # папка побольше: таблица из нескольких блоков
        items.append(_mail("INBOX/Проекты/2024", f"Subject: n{i}\r\nMessage-ID: <n{i}@x>\r\n\r\nтело {i}".encode(),
                           ["\\Seen"] if i % 2 else [], DATE + i))
    return _export(tmp, items), expect, tmp


# ---------------------------------------------------------------------------
#  Кодирование данных и RTF
# ---------------------------------------------------------------------------
def test_crypt_tables_and_cyclic_matches_specification():
    assert sorted(rd._MPBB_R) == list(range(256)) and sorted(rd._MPBB_S) == list(range(256))
    assert all(rd._MPBB_I[rd._MPBB_R[x]] == x for x in range(256))
    assert all(rd._MPBB_S[rd._MPBB_S[x]] == x for x in range(256))        # иначе код не был бы обратим
    # первые байты таблиц — как в спецификации
    assert rd._MPBB_R[:4] == bytes((65, 54, 19, 98)) and rd._MPBB_S[:4] == bytes((20, 83, 15, 86))
    assert rd._MPBB_I[:4] == bytes((71, 241, 180, 230))
    rnd = random.Random(3)
    for size in (0, 1, 255, 256, 257, 4000, 8176, 8192, 20000):
        for key in (0, 1, 0x12345678, 0xFFFFFFFE, 0x00FF00FE):
            data = rnd.randbytes(size)
            coded = pst_tools.reference_cyclic(data, key)
            assert rd.crypt_cyclic(data, key) == coded
            assert rd.crypt_cyclic(coded, key) == data
    data = rnd.randbytes(5000)
    assert rd.decode_permute(rd.encode_permute(data)) == data


def test_rtf_decompress():
    # пример из [MS-OXRTFCP] 3.1
    vector = bytes.fromhex("2d0000002b0000004c5a4675f1c5c7a703000a007263706731323542320af32068656c0900"
                           "20627705b06c647d0a800fa0")
    assert rtf.decompress(vector) == b"{\\rtf1\\ansi\\ansicpg1252\\pard hello world}\r\n"
    rnd = random.Random(4)
    words = [b"\\par ", b"hello ", b"{\\b bold}", b"\\'cf\\'f0\\'e8", b"world", b"\r\n", b"\\f0\\fs20 "]
    for count in (0, 3, 60, 500):
        data = b"{\\rtf1\\ansi" + b"".join(rnd.choice(words) for _ in range(count)) + b"}"
        assert rtf.decompress(pst_tools.compress_rtf(data)) == data
    plain = b"{\\rtf1 not compressed}"
    assert rtf.decompress(struct.pack("<IIII", len(plain) + 12, len(plain), 0x414C454D, 0) + plain) == plain
    with pytest.raises(rtf.RtfError):
        rtf.decompress(b"short")
    with pytest.raises(rtf.RtfError):
        rtf.decompress(struct.pack("<IIII", 20, 8, 0x12345678, 0) + b"12345678")


ENCAPSULATED = (
    b"{\\rtf1\\ansi\\ansicpg1251\\fromhtml1 \\deff0{\\fonttbl{\\f0\\fswiss\\fcharset204 Arial;}"
    b"{\\f1\\fmodern\\fcharset0 Courier New;}{\\f2\\fnil\\fcharset161 Greek;}}\r\n"
    b"{\\colortbl\\red0\\green0\\blue0;}\r\n"
    b"{\\*\\generator Riched20 12.0;}\r\n"
    b"{\\*\\htmltag19 <html>}{\\*\\htmltag34 <head>}{\\*\\htmltag241 \\par <style>p \\{margin:0\\}</style>}"
    b"{\\*\\htmltag41 </head>}{\\*\\htmltag50 <body>}\\htmlrtf {\\htmlrtf0 "
    b"{\\*\\htmltag64 <p>}\\htmlrtf {\\htmlrtf0 \\'cf\\'f0\\'e8\\'e2\\'e5\\'f2, "
    b"{\\*\\htmltag84 <b>}\\htmlrtf {\\b \\htmlrtf0 world{\\*\\htmltag92 </b>}\\htmlrtf }\\htmlrtf0 "
    b" \\u8364?50{\\*\\htmltag84 &amp;}\\htmlrtf &\\htmlrtf0 {\\f2 \\'e1\\'e2}"
    b"\\htmlrtf\\par}\\htmlrtf0{\\*\\htmltag72 </p>}"
    b"{\\*\\mhtmltag84 ignored}\\htmlrtf {\\pict\\wmetafile8 0102030405}\\htmlrtf0 "
    b"{\\*\\htmltag58 </body>}{\\*\\htmltag27 </html>}}}")


def test_rtf_encapsulated_html_text_and_plain_rtf():
    kind, html = rtf.extract(ENCAPSULATED)
    assert kind == "html"
    assert html == ("<html><head>\r\n<style>p {margin:0}</style></head><body>"
                    "<p>Привет, <b>world</b> €50&amp;αβ</p></body></html>")
    assert rtf.body_from_compressed(pst_tools.compress_rtf(ENCAPSULATED)) == (kind, html)

    kind, text = rtf.extract(b"{\\rtf1\\ansi\\ansicpg1251\\fromtext \\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
                             b"\\uc1\\pard\\plain\\f0 \\'cf\\'f0\\'e8\\'e2\\'e5\\'f2\\par line 2\\tab x\\par }")
    assert (kind, text) == ("text", "Привет\nline 2\tx\n")

    kind, text = rtf.extract(b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Times;}}{\\stylesheet{\\s0 Normal;}}"
                             b"{\\info{\\title secret title}}\\pard Hello {\\b bold} \\u1055?\\u1088?\\par "
                             b"{\\field{\\*\\fldinst HYPERLINK \"http://x\"}{\\fldrslt link}}\\par "
                             b"\\u-10179?\\u-8704? end}")          # знак вне BMP — парой суррогатов
    assert kind == "text" and text == "Hello bold Пр\nlink\n\U0001F600 end\n"
    with pytest.raises(rtf.RtfError):
        rtf.extract(b"not rtf at all")
    # Exchange пишет таблицу шрифтов с \\fcharset0 при любой кодировке — текст в кодировке документа
    kind, html = rtf.extract(b"{\\rtf1\\ansi\\ansicpg1251\\fromhtml1 \\deff0{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}}"
                             b"{\\*\\htmltag64 <p>}\\htmlrtf {\\f0 \\htmlrtf0 \\'cf\\'f0\\'e8{\\*\\htmltag72 </p>}\\htmlrtf }\\htmlrtf0 }")
    assert (kind, html) == ("html", "<p>При</p>")
    # Word дописывает после закрывающей скобки перевод строки и NUL — в текст они не попадают
    assert rtf.extract(b"{\\rtf1\\ansi body\\par }\r\n\x00junk") == ("text", "body\n")
    assert rtf.extract(b"{\\rtf1\\ansi a\\u99999999?b}") == ("text", "a\ue0ffb\n")      # число вне 16 бит не роняет разбор
    with pytest.raises(rtf.RtfError, match="вложенность"):
        rtf.extract(b"{\\rtf1" + b"{" * 5000)


# ---------------------------------------------------------------------------
#  Сборка заголовков
# ---------------------------------------------------------------------------
def test_header_helpers():
    encoded = eml.encode_words("Очень длинная тема письма, которая не помещается в одно слово RFC 2047 " * 3)
    assert all(len(word) <= 75 for word in encoded.split(" "))
    assert str(email.header.make_header(email.header.decode_header(encoded))).startswith("Очень длинная")
    assert eml.encode_words("plain ascii") == "plain ascii"
    assert eml.format_address("Сидоров, Пётр", "petr@example.ru").endswith(" <petr@example.ru>")
    assert eml.format_address("Doe, John", "j@x") == '"Doe, John" <j@x>'
    assert eml.format_address("j@x", "j@x") == "j@x"
    assert eml.placeholder_address("/o=Org/ou=Site/cn=Recipients/cn=Ivanov.I", "Иванов") == \
        "ivanov.i@exchange.invalid"
    assert eml.placeholder_address("", "Иванов").endswith("@exchange.invalid")
    folded = eml.fold("To", ", ".join(f"user{i}@example.com" for i in range(30)))
    assert all(len(line) <= 78 for line in folded.split(b"\r\n")) and folded.endswith(b"\r\n")
    fields = eml.parse_header_block("Microsoft Mail Internet Headers Version 2.0\r\n"
                                    "Received: from a\r\n\tby b; Mon\r\nSubject: =?utf-8?B?0KLQtdGB0YI=?=\r\n"
                                    "Content-Type: text/plain\r\n\r\nbody is not a header: x\r\n")
    assert [name for name, _v in fields] == ["Received", "Subject", "Content-Type"]
    assert fields[0][1] == " from a\r\n\tby b; Mon"
    # длинное не-ASCII имя файла — частями RFC 2231, ни одна тройка %XX не разорвана
    parts = eml._rfc2231("filename", "Очень длинное имя файла договора поставки № 17 от 1 сентября.docx")
    assert len(parts) > 1 and parts[0].startswith("filename*0*=utf-8''")
    joined = "".join(part.split("=", 1)[1] for part in parts).replace("utf-8''", "")
    from urllib.parse import unquote
    assert unquote(joined) == "Очень длинное имя файла договора поставки № 17 от 1 сентября.docx"
    # каждая часть раскодируется сама по себе: знак UTF-8 частями не разорван
    for part in parts:
        unquote(part.split("=", 1)[1].replace("utf-8''", ""), errors="strict")
    # перенос не трогает пробелы внутри значения, а слишком длинное «слово» режется
    assert eml.fold("Content-Type", 'text/plain; name="a  b   c.txt"') == b'Content-Type: text/plain; name="a  b   c.txt"\r\n'
    long_name = "x" * 50 + "  " + "y" * 40
    assert eml.fold("X", long_name).replace(b"\r\n", b"") == b"X: " + long_name.encode()
    assert max(len(line) for line in eml.fold("References", "<" + "r" * 3000 + ">").split(b"\r\n")) <= 998
    assert eml.clean_filename("a" * 300 + ".docx", "f").endswith(".docx") and len(eml.clean_filename("a" * 300 + ".docx", "f")) <= 200
    assert eml.clean_filename("..\\..\\evil\x00.exe", "f") == "evil.exe" and eml.clean_filename(" . ", "f") == "f"
    assert b"".join(eml._crlf_chunks([b"a\nb\r", b"\nc\r\nd\n", b"", b"\ne"])) == b"a\r\nb\r\nc\r\nd\r\n\r\ne"
    assert eml.is_mail_class("IPM.Note") and eml.is_mail_class("IPM.Note.SMIME") and eml.is_mail_class("")
    assert eml.is_mail_class("IPM.Schedule.Meeting.Request") and eml.is_mail_class("REPORT.IPM.Note.NDR")
    assert not eml.is_mail_class("IPM.Contact") and not eml.is_mail_class("ipm.appointment.x")
    assert eml.is_mail_class("IPM.Contactless")                 # не «контакт»: совпадение только до точки


# ---------------------------------------------------------------------------
#  Чтение файла писателя: папки, письма, вложения
# ---------------------------------------------------------------------------
def test_roundtrip_export_then_read(sample):
    path, expect, _tmp = sample
    with rd.PstReader(path) as pst:
        assert pst.unicode and pst.crypt == 0 and pst.damaged == 0
        assert "Unicode" in pst.describe()
        folders = {"/".join(f.path): f for f in pst.folders()}
        assert set(folders) == {"", "Удаленные", "Входящие", "Входящие/Проекты", "Входящие/Проекты/2024",
                                "Drafts"}
        assert folders["Входящие/Проекты/2024"].count == 40 and folders[""].count == 0
    mails = _read_all(path)
    assert len(mails) == 44

    report = mails[("Входящие", "Отчёт за сентябрь")]
    assert report.flags == ["\\Seen", "\\Answered"] and report.internaldate == DATE
    assert report.message_id == "<rep1@example.ru>"
    msg = _parse(report.raw)
    assert not _defects(msg)
    assert msg["Subject"] == "Отчёт за сентябрь" and msg["X-Custom"] == "keep me"
    assert msg["From"].addresses[0].addr_spec == "ivan@example.ru"
    assert [a.addr_spec for a in msg["To"].addresses] == ["maria@example.ru", "bob@example.com"]
    assert msg["Cc"].addresses[0].display_name == "Сидоров, Пётр"
    assert msg["Date"].datetime.timestamp() == 1756710000
    assert msg.get_content_type() == "multipart/mixed"
    assert _text(msg.get_body(("plain",))) == "Текстовая версия отчёта.\nВторая строка."
    assert "<b>отчёт</b>" in msg.get_body(("html",)).get_content()
    related = msg.get_payload()[0]
    assert related.get_content_type() == "multipart/related"
    image = related.get_payload()[1]
    assert image["Content-ID"] == "<logo1@x>" and image.get_content_disposition() == "inline"
    assert _sha(image.get_payload(decode=True)) == expect["logo"]
    files = list(msg.iter_attachments())
    assert [f.get_filename() for f in files] == ["Отчёт сентябрь 2025.pdf"]
    assert files[0].get_content_type() == "application/pdf"
    assert _sha(files[0].get_payload(decode=True)) == expect["pdf"]

    big = mails[("Входящие/Проекты", "Fwd: большой файл")]
    assert big.flags == ["\\Seen", "$Forwarded", "\\Flagged"]
    files = {f.get_filename(): f for f in _parse(big.raw).iter_attachments()}
    assert _sha(files["big.bin"].get_payload(decode=True)) == expect["big"]
    nested = [name for name in files if name.endswith(".eml")]
    assert len(nested) == 1 and "Внутреннее письмо" in nested[0]
    # писатель кладёт вложенное письмо файлом .eml: тип message/rfc822 в base64 недопустим,
    # поэтому это обычный файл — и его байты на месте
    assert files[nested[0]].get_content_type() == "application/octet-stream"
    assert _parse(files[nested[0]].get_payload(decode=True))["Subject"] == "Внутреннее письмо"

    only_html = _parse(mails[("Удаленные", "html only")].raw)
    assert only_html["Bcc"] == "secret@e.f"
    # писатель добавил письму текстовую версию из HTML — в файле их две
    assert only_html.get_content_type() == "multipart/alternative"
    assert _text(only_html.get_body(("plain",))) == "Только HTML: привет"
    html = only_html.get_body(("html",))
    assert html.get_content_charset() == "windows-1251"
    assert html.get_payload(decode=True) == expect["html"]              # байты и кодировка — как в письме

    assert mails[("Drafts", "draft")].flags == ["\\Draft"]
    assert not any(_defects(_parse(built.raw)) for built in mails.values())
    assert not any(built.lost or built.notes for built in mails.values())
    # сборка детерминирована: то же письмо — те же байты
    again = _read_all(path)
    assert all(again[key].raw == built.raw for key, built in mails.items())


@pytest.mark.parametrize("method", [1, 2])
def test_encrypted_files_read_the_same(sample, method):
    path, _expect, tmp = sample
    coded = str(tmp / f"crypt{method}.pst")
    pst_tools.encrypt_copy(path, coded, method)
    assert open(coded, "rb").read() != open(path, "rb").read()
    with rd.PstReader(coded) as pst:
        assert pst.crypt == method and "шифрование" in pst.describe()
    plain, coded_mails = _read_all(path), _read_all(coded)
    assert plain.keys() == coded_mails.keys()
    assert all(coded_mails[key].raw == built.raw for key, built in plain.items())


@pytest.mark.parametrize("method", [0, 1])
def test_ansi_file_reads_the_same(sample, method):
    path, _expect, tmp = sample
    ansi = str(tmp / f"ansi{method}.pst")
    pst_tools.to_ansi(path, ansi, method)
    with rd.PstReader(ansi) as pst:
        assert not pst.unicode and pst.crypt == method and "ANSI" in pst.describe() and pst.damaged == 0
        assert pst.store_name() == "box"
    plain, ansi_mails = _read_all(path), _read_all(ansi)
    assert plain.keys() == ansi_mails.keys()
    assert all(ansi_mails[key].raw == built.raw for key, built in plain.items())


# ---------------------------------------------------------------------------
#  Письма с редкими свойствами — пишем их свойствами MAPI напрямую
# ---------------------------------------------------------------------------
class _Crafted:
    """Файл .pst, в который письма кладутся наборами свойств (как их хранит Outlook)."""

    def __init__(self, path, monkeypatch):
        original = pf._encode_var

        def encode(ptype, value):
            if ptype in (0x001E, 0x000D):              # строка ANSI и объект — писатель службы их не пишет
                return bytes(value)
            return original(ptype, value)

        monkeypatch.setattr(pf, "_encode_var", encode)
        self.path = path
        self.pst = pf.PstFile(path, "crafted")
        self.folder = self.pst.add_folder("Почта", pf.NID_IPM_SUBTREE)
        self.rows = []

    def add(self, props, recipients=(), attachments=(), images=(), ghost=False):
        """attachments — [(свойства, ячейки)]; images — готовые образы вложений [(NodeImage, ячейки)];
        ghost — добавить в таблицу вложений строку, для которой самого вложения в файле нет."""
        pst = self.pst
        nid = pst.counters.next(pf.NT_NORMAL_MESSAGE)
        subnodes = self._parts(recipients, attachments, images, ghost)
        bid_data, bid_sub = pst.ctx.write_node(pf.build_pc(pst.ctx, props, subnodes))
        pst.ndb.add_node(nid, bid_data, bid_sub, self.folder)
        self.rows.append((nid, {0x001A001F: "IPM.Note", 0x0E070003: 0, 0x0E080003: 100, 0x0E170003: 0,
                                0x00170003: 1, 0x00360003: 0}))
        return nid

    def _parts(self, recipients, attachments, images, ghost=False):
        pst = self.pst
        subnodes = []
        rows = [(i + 1, pst._stamp(cells)) for i, cells in enumerate(recipients)]
        bid_data, bid_sub = pf.write_tc(pst.ctx, pf.LAYOUT_RECIPIENTS, rows)
        subnodes.append((pf.NID_RECIPIENT_TABLE, bid_data, bid_sub))
        table = []
        prepared = [(pf.build_pc(pst.ctx, props), cells) for props, cells in attachments] + list(images)
        for image, cells in prepared:
            att_nid = pst.counters.next(pf.NT_ATTACHMENT)
            bid_data, bid_sub = pst.ctx.write_node(image)
            subnodes.append((att_nid, bid_data, bid_sub))
            table.append((att_nid, pst._stamp(cells)))
        if ghost:
            table.insert(0, (pst.counters.next(pf.NT_ATTACHMENT),
                             pst._stamp({0x0E200003: 5, 0x3704001F: "ghost", 0x37050003: 1, 0x370B0003: -1})))
        if table:
            bid_data, bid_sub = pf.write_tc(pst.ctx, pf.LAYOUT_ATTACHMENTS, table)
            subnodes.append((pf.NID_ATTACHMENT_TABLE, bid_data, bid_sub))
        return subnodes

    def embedded(self, props, recipients=(), attachments=()):
        """Образ вложения «вложенное письмо»: письмо лежит подузлом самого вложения."""
        pst = self.pst
        inner = pf.build_pc(pst.ctx, props, self._parts(recipients, attachments, ()))
        bid_data, bid_sub = pst.ctx.write_node(inner)
        inner_nid = pst.counters.next(pf.NT_NORMAL_MESSAGE)
        image = pf.build_pc(pst.ctx, [(0x3705, pf.PT_I4, 5), (0x3001, pf.PT_UNICODE, "Вложенное"),
                                      (0x3701, 0x000D, struct.pack("<II", inner_nid, 1234))],
                            [(inner_nid, bid_data, bid_sub)])
        return image, {0x37050003: 5, 0x0E200003: 1234, 0x3704001F: "inner", 0x370B0003: -1}

    def close(self):
        self.pst.set_contents(self.folder, self.rows, 0)
        self.pst.close()
        return self.path


def _recipient(kind, name, addrtype, address, smtp=None):
    row = {0x0C150003: kind, 0x3001001F: name, 0x3002001F: addrtype, 0x3003001F: address,
           0x0FFE0003: 6, 0x39000003: 0, 0x0FFF0102: b"\x00" * 4, 0x300B0102: b"k"}
    return row


def _file(name, data, mime="application/octet-stream", method=1, extra=()):
    props = [(0x3705, pf.PT_I4, method), (0x3707, pf.PT_UNICODE, name), (0x370E, pf.PT_UNICODE, mime)]
    if data is not None:
        props.append((0x3701, pf.PT_BINARY, data))
    props += list(extra)
    return props, {0x0E200003: len(data or b""), 0x3704001F: name, 0x37050003: method, 0x370B0003: -1}


def test_message_without_transport_headers(tmp_path, monkeypatch):
    """Отправленное письмо: исходных заголовков нет — всё составляется из свойств."""
    crafted = _Crafted(str(tmp_path / "sent.pst"), monkeypatch)
    crafted.add(
        [(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "\x01\x05RE: Счёт на оплату"),
         (0x0039, pf.PT_SYSTIME, pf.to_filetime(DATE)), (0x0E06, pf.PT_SYSTIME, pf.to_filetime(DATE + 60)),
         (0x0E07, pf.PT_I4, 0x01), (0x1081, pf.PT_I4, 103), (0x1090, pf.PT_I4, 2), (0x0017, pf.PT_I4, 2),
         (0x0036, pf.PT_I4, 2), (0x0070, pf.PT_UNICODE, "Счёт на оплату"), (0x0071, pf.PT_BINARY, b"\x01\x02\x03"),
         (0x0042, pf.PT_UNICODE, "Директор"), (0x0064, pf.PT_UNICODE, "EX"),
         (0x0065, pf.PT_UNICODE, "/o=Org/ou=First/cn=Recipients/cn=director"),
         (0x5D02, pf.PT_UNICODE, "director@example.ru"),
         (0x0C1A, pf.PT_UNICODE, "Секретарь"), (0x0C1E, pf.PT_UNICODE, "SMTP"),
         (0x0C1F, pf.PT_UNICODE, "office@example.ru"),
         (0x1042, pf.PT_UNICODE, "<orig@example.com>"), (0x300B, pf.PT_BINARY, b"search-key-1"),
         (0x1000, pf.PT_UNICODE, "Оплатим завтра.")],
        recipients=[_recipient(1, "Поставщик", "SMTP", "sales@example.com"),
                    _recipient(1, "Иванов Иван", "EX", "/o=Org/ou=First/cn=Recipients/cn=ivanov"),
                    _recipient(2, "'buh@example.ru'", "", ""),
                    _recipient(3, "Архив", "SMTP", "archive@example.ru")])
    built = _read_all(crafted.close())[("Почта", "RE: Счёт на оплату")]
    assert built.flags == ["\\Seen", "\\Answered", "\\Flagged"] and built.internaldate == DATE + 60
    msg = _parse(built.raw)
    assert not _defects(msg)
    assert msg["Subject"] == "RE: Счёт на оплату"
    assert msg["From"].addresses[0].addr_spec == "director@example.ru"       # адрес SMTP вместо адреса Exchange
    assert msg["From"].addresses[0].display_name == "Директор"
    assert msg["Sender"].addresses[0].addr_spec == "office@example.ru"
    assert [a.addr_spec for a in msg["To"].addresses] == ["sales@example.com", "ivanov@exchange.invalid"]
    assert msg["To"].addresses[1].display_name == "Иванов Иван"              # имя адресата не потеряно
    assert msg["Cc"].addresses[0].addr_spec == "buh@example.ru"
    assert msg["Bcc"].addresses[0].addr_spec == "archive@example.ru"
    assert msg["Date"].datetime.timestamp() == DATE
    assert msg["In-Reply-To"] == "<orig@example.com>" and msg["Importance"] == "high"
    assert msg["Sensitivity"] == "Private" and msg["Thread-Topic"] == "Счёт на оплату"
    assert msg["Thread-Index"] == "AQID"
    # у письма нет Message-ID — он выводится из ключа поиска и всегда один и тот же
    assert msg["Message-ID"] == f"<{hashlib.sha1(b'search-key-1').hexdigest()}@pst.mailarchiver.invalid>"
    assert built.message_id == msg["Message-ID"]
    assert _text(msg) == "Оплатим завтра."


def test_ansi_strings_use_message_codepage(tmp_path, monkeypatch):
    crafted = _Crafted(str(tmp_path / "ansi-strings.pst"), monkeypatch)
    crafted.add([(0x001A, 0x001E, b"IPM.Note"), (0x0037, 0x001E, "Привет из 1251".encode("cp1251")),
                 (0x3FFD, pf.PT_I4, 1251), (0x3FDE, pf.PT_I4, 20866),
                 (0x1000, 0x001E, "Тело в кодировке Windows".encode("cp1251"))])
    crafted.add([(0x001A, 0x001E, b"IPM.Note"), (0x0037, 0x001E, "Zażółć".encode("cp1250")),
                 (0x1000, 0x001E, "gęślą jaźń".encode("cp1250"))])
    path = crafted.close()
    mails = _read_all(path, ansi_codec="cp1250")
    assert ("Почта", "Привет из 1251") in mails            # кодировка названа в письме — берётся она
    assert _text(_parse(mails[("Почта", "Привет из 1251")].raw)) == "Тело в кодировке Windows"
    assert _text(_parse(mails[("Почта", "Zażółć")].raw)) == "gęślą jaźń"
    assert ("Почта", "Zażółć") not in _read_all(path)      # по умолчанию — Windows-1251


def test_rtf_body_embedded_message_and_odd_attachments(tmp_path, monkeypatch):
    crafted = _Crafted(str(tmp_path / "rtf.pst"), monkeypatch)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "html in rtf"),
                 (0x1000, pf.PT_UNICODE, "plain version"),
                 (0x1009, pf.PT_BINARY, pst_tools.compress_rtf(ENCAPSULATED))])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "text in rtf"),
                 (0x1009, pf.PT_BINARY, pst_tools.compress_rtf(b"{\\rtf1\\ansi\\fromtext \\pard only rtf\\par }"))])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "broken rtf"),
                 (0x1000, pf.PT_UNICODE, "still has text"), (0x1009, pf.PT_BINARY, b"garbage-not-rtf-at-all")])
    inner_file = _file("Договор.docx", b"docx-bytes")
    image = crafted.embedded([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "Внутри"),
                              (0x0042, pf.PT_UNICODE, "Автор"), (0x0064, pf.PT_UNICODE, "SMTP"),
                              (0x0065, pf.PT_UNICODE, "author@example.ru"),
                              (0x1000, pf.PT_UNICODE, "Текст вложенного письма")],
                             recipients=[_recipient(1, "Кому", "SMTP", "to@example.ru")],
                             attachments=[inner_file])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "with embedded"),
                 (0x1000, pf.PT_UNICODE, "см. вложение")],
                attachments=[_file("ссылка.lnk", None, method=2), _file("объект", None, method=6),
                             _file("пустое.txt", None), _file("../../etc/passwd", b"x", mime="not a mime"),
                             _file("picture.png", b"png", mime="image/png",
                                   extra=[(0x3712, pf.PT_UNICODE, "not-referenced@x")])],
                images=[image])
    mails = _read_all(crafted.close())

    msg = _parse(mails[("Почта", "html in rtf")].raw)
    assert msg.get_content_type() == "multipart/alternative"
    assert _text(msg.get_body(("plain",))) == "plain version"
    assert "<p>Привет, <b>world</b> €50&amp;αβ</p>" in msg.get_body(("html",)).get_content()
    assert _text(_parse(mails[("Почта", "text in rtf")].raw)) == "only rtf"
    broken = mails[("Почта", "broken rtf")]
    assert _text(_parse(broken.raw)) == "still has text"
    assert any("RTF" in text for text in broken.lost) and not broken.notes       # это потеря, а не «так устроено»

    built = mails[("Почта", "with embedded")]
    msg = _parse(built.raw)
    assert not _defects(msg)
    files = list(msg.iter_attachments())
    assert [f.get_filename() for f in files] == ["passwd", "picture.png", "Вложенное.eml"]   # путь из имени убран
    assert not built.lost                                               # пропущенное ниже — не повреждения
    assert files[0].get_content_type() == "application/octet-stream"     # негодный тип заменён
    assert files[1].get_content_disposition() == "attachment"           # HTML на картинку не ссылается
    assert files[2].get_content_type() == "message/rfc822"
    inner = files[2].get_payload()[0]
    assert inner["Subject"] == "Внутри" and inner["From"].addresses[0].addr_spec == "author@example.ru"
    assert inner["To"].addresses[0].addr_spec == "to@example.ru"
    assert _text(inner.get_body(("plain",))) == "Текст вложенного письма"
    assert [f.get_filename() for f in inner.iter_attachments()] == ["Договор.docx"]
    notes = " | ".join(built.notes)
    assert "ссылка.lnk" in notes and "OLE" in notes and "пустое.txt" in notes


def test_unreadable_attachment_does_not_take_the_others(tmp_path, monkeypatch):
    crafted = _Crafted(str(tmp_path / "ghost.pst"), monkeypatch)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "two files"),
                 (0x1000, pf.PT_UNICODE, "text")],
                attachments=[_file("a.txt", b"aaa", mime="text/plain"), _file("b.bin", b"bbb")], ghost=True)
    path = crafted.close()
    built = _read_all(path)[("Почта", "two files")]
    files = {f.get_filename(): f.get_payload(decode=True) for f in _parse(built.raw).iter_attachments()}
    assert files == {"a.txt": b"aaa", "b.bin": b"bbb"}
    assert len(built.lost) == 1 and "вложение 1 не прочитано" in built.lost[0] and not built.notes

    # вложение обрывается на середине чтения: его часть убирается целиком, письмо остаётся правильным
    original = rd.Attachment.chunks

    def chunks(self):
        if self.long_filename == "b.bin":
            yield b"partial"
            raise rd.PstReadError("блок 0x123 не найден в файле")
        yield from original(self)

    monkeypatch.setattr(rd.Attachment, "chunks", chunks)
    built = _read_all(path)[("Почта", "two files")]
    msg = _parse(built.raw)
    assert not _defects(msg) and b"partial" not in built.raw
    assert {f.get_filename(): f.get_payload(decode=True) for f in msg.iter_attachments()} == {"a.txt": b"aaa"}
    assert any("«b.bin» не прочитано" in text for text in built.lost)


def test_size_budget_is_shared_with_embedded_messages(tmp_path, monkeypatch):
    """Предел размера считает письмо вместе со всеми письмами внутри него — иначе вложенные
    письма собирались бы в памяти без счёта."""
    crafted = _Crafted(str(tmp_path / "nested.pst"), monkeypatch)
    blob = random.Random(1).randbytes(300_000)
    images = [crafted.embedded([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, f"inner {n}"),
                                (0x1000, pf.PT_UNICODE, "x")], attachments=[_file("big.bin", blob)])
              for n in range(4)]
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "outer"), (0x1000, pf.PT_UNICODE, "x")],
                images=images)
    with rd.PstReader(crafted.close()) as pst:
        folder = [f for f in pst.folders() if f.count][0]
        nid = pst.message_nids(folder.nid)[0]
        assert len(eml.build_eml(pst.open_message(nid)).raw) > 4 * 400_000
        with pytest.raises(eml.EmlError, match="больше"):
            eml.build_eml(pst.open_message(nid), limit=1_000_000)


def test_header_edge_cases_from_transport_headers(tmp_path, monkeypatch):
    crafted = _Crafted(str(tmp_path / "headers.pst"), monkeypatch)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "Тема из свойств"),
                 (0x300B, pf.PT_BINARY, b"key-7"), (0x1000, pf.PT_UNICODE, "body"),
                 (0x0C1A, pf.PT_UNICODE, "Имя"), (0x0C1E, pf.PT_UNICODE, "SMTP"), (0x0C1F, pf.PT_UNICODE, "from@x.ru"),
                 # пустые поля в исходных заголовках не должны заслонять значения из свойств
                 (0x007D, pf.PT_UNICODE, "Received: from a by b\r\nSubject:\r\nMessage-ID:   \r\nFrom:\r\n"
                                         "X-Evil: ok\r\nMIME-Version: 1.0\r\nContent-Type: text/x-old\r\n\r\n")])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"),
                 # перевод строки в теме и именах из чужого файла не должен рождать новые заголовки
                 (0x0037, pf.PT_UNICODE, "inject\r\nBcc: victim@x.ru"), (0x1000, pf.PT_UNICODE, "body"),
                 (0x0C1A, pf.PT_UNICODE, "Name\r\nX-Injected: 1"), (0x0C1E, pf.PT_UNICODE, "SMTP"),
                 (0x0C1F, pf.PT_UNICODE, "a@b.c\r\nX-Injected: 2"), (0x1035, pf.PT_UNICODE, "<id@x>\r\nX-Injected: 3")],
                attachments=[_file('fi"le\r\nX-Injected: 4.txt', b"data", mime="text/plain",
                                   extra=[(0x3712, pf.PT_UNICODE, "cid\r\nX-Injected: 5")])])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "cid check"),
                 (0x1013, pf.PT_BINARY, b'<html><body><img src="cid:img@x"></body></html>'), (0x3FDE, pf.PT_I4, 65001)],
                attachments=[_file("a.png", b"A", mime="image/png", extra=[(0x3712, pf.PT_UNICODE, "img")]),
                             _file("b.png", b"B", mime="image/png", extra=[(0x3712, pf.PT_UNICODE, "img@x")])])
    mails = _read_all(crafted.close())
    built = mails[("Почта", "Тема из свойств")]
    msg = _parse(built.raw)
    assert msg["Subject"] == "Тема из свойств" and msg["From"].addresses[0].addr_spec == "from@x.ru"
    assert msg["Message-ID"] == built.message_id == f"<{hashlib.sha1(b'key-7').hexdigest()}@pst.mailarchiver.invalid>"
    assert msg["X-Evil"] == "ok" and msg["Received"] == "from a by b"
    assert len(msg.get_all("MIME-Version")) == 1 and msg.get_content_type() == "text/plain"

    built = mails[("Почта", "inject Bcc: victim@x.ru")]
    msg = _parse(built.raw)
    assert msg["Bcc"] is None and not [name for name, _v in msg.items() if name.lower() == "x-injected"]
    assert not any(part.get("X-Injected") for part in msg.walk())
    assert msg["Message-ID"] == built.message_id == "<id@x>" and not _defects(msg)
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "fi_le__X-Injected_ 4.txt" and attachment.get_payload(decode=True) == b"data"

    related = _parse(mails[("Почта", "cid check")].raw)
    assert related.get_content_type() == "multipart/mixed"            # «img» — не «img@x»: это обычное вложение
    assert [(p.get_filename(), p.get_content_disposition()) for p in related.walk() if p.get_filename()] == \
        [("b.png", "inline"), ("a.png", "attachment")]


def test_smime_and_non_mail_items(tmp_path, monkeypatch):
    signed = (b"MIME-Version: 1.0\nContent-Type: multipart/signed; protocol=\"application/pkcs7-signature\"; boundary=\"sig\"\r\n\r\n"
              b"--sig\r\nContent-Type: text/plain\r\n\r\nsigned text\r\n--sig\r\n"
              b"Content-Type: application/pkcs7-signature\r\n\r\nMIIB\r\n--sig--\r\n")
    crafted = _Crafted(str(tmp_path / "smime.pst"), monkeypatch)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note.SMIME.MultipartSigned"), (0x0037, pf.PT_UNICODE, "signed"),
                 (0x007D, pf.PT_UNICODE, "From: a@b.c\r\nSubject: signed\r\nMessage-ID: <s@b.c>\r\n"
                                         "Content-Type: multipart/signed; boundary=old\r\n\r\n")],
                attachments=[_file("smime.p7m", signed, mime="multipart/signed")])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note.SMIME"), (0x0037, pf.PT_UNICODE, "encrypted"),
                 (0x007D, pf.PT_UNICODE, "From: a@b.c\r\nSubject: encrypted\r\nContent-Type: "
                                         "application/pkcs7-mime; smime-type=enveloped-data; name=\"smime.p7m\"\r\n")],
                attachments=[_file("smime.p7m", b"\x30\x80binary", mime="application/pkcs7-mime")])
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Contact"), (0x0037, pf.PT_UNICODE, "Иванов")])
    path = crafted.close()
    mails = _read_all(path)
    msg = _parse(mails[("Почта", "signed")].raw)
    assert msg.get_content_type() == "multipart/signed" and msg.get_boundary() == "sig"
    assert _text(msg.get_payload()[0]) == "signed text"
    assert msg["Message-ID"] == "<s@b.c>" and len(msg.get_all("Content-Type")) == 1
    assert len(msg.get_all("MIME-Version")) == 1 and b"\n" not in mails[("Почта", "signed")].raw.replace(b"\r\n", b"")
    msg = _parse(mails[("Почта", "encrypted")].raw)
    assert msg.get_content_type() == "application/pkcs7-mime"
    assert msg.get_param("smime-type") == "enveloped-data" and msg.get_payload(decode=True) == b"\x30\x80binary"
    # контакт читается, но источник импорта его письмом не считает
    with rd.PstReader(path) as pst:
        total, items = importer.builtin_source(pst)
        items = list(items)
    assert total == 3 and [item.not_mail for item in items] == [False, False, True]


# ---------------------------------------------------------------------------
#  Чужие и повреждённые файлы
# ---------------------------------------------------------------------------
def test_not_a_pst_and_unsupported(tmp_path, sample):
    path, _expect, _tmp = sample
    head = open(path, "rb").read(0x4600)
    cases = {"empty.pst": b"", "text.pst": b"just some text" * 100, "short.pst": head[:300]}
    client = bytearray(head)
    client[8:10] = b"AB"                               # не хранилище писем
    cases["oab.pst"] = bytes(client)
    whole = open(path, "rb").read()
    nbt_root = struct.unpack_from("<Q", whole, 0xB4 + 44)[0]
    four_k = bytearray(whole)
    struct.pack_into("<HH", four_k, 8, 0x4F53, 36)     # .ost Outlook 2013: блоки и страницы по 4 КБ
    four_k[nbt_root + 496:nbt_root + 498] = b"\x00\x00"      # на месте трейлера страницы 512 — данные
    cases["4k.pst"] = bytes(four_k)
    crypt = bytearray(head)
    crypt[0x201] = 0x10
    cases["crypt.pst"] = bytes(crypt)
    for name, data in cases.items():
        target = tmp_path / name
        target.write_bytes(data)
        with pytest.raises(rd.PstUnsupportedError):
            rd.PstReader(str(target))
    # .ost прежних версий Outlook устроен как .pst — читается; номер версии новее 23 сам по себе не отказ
    old_ost = bytearray(whole)
    struct.pack_into("<HH", old_ost, 8, 0x4F53, 24)
    (tmp_path / "old.ost").write_bytes(old_ost)
    with rd.PstReader(str(tmp_path / "old.ost")) as pst:
        assert ".ost" in pst.describe() and sum(f.count for f in pst.folders()) == 44
    truncated = tmp_path / "truncated.pst"
    truncated.write_bytes(open(path, "rb").read()[:0x8000])
    with pytest.raises(rd.PstReadError):
        rd.PstReader(str(truncated))


def test_damaged_block_is_counted_and_other_mail_survives(tmp_path, sample):
    path, _expect, _tmp = sample
    from pst_reader import Pst
    strict = Pst(path)
    data = bytearray(strict.data)
    victim = next(nid for nid in strict.nbt if nid & 0x1F == 0x04)
    ib, cb, _cref = strict.bbt[strict.nbt[victim][0] & ~1]
    data[ib:ib + 12] = b"\x00" * 12                    # затёрта подпись кучи первого письма
    broken = tmp_path / "broken.pst"
    broken.write_bytes(data)
    with rd.PstReader(str(broken)) as pst:
        total, items = importer.builtin_source(pst)
        items = list(items)
        assert pst.damaged >= 1                        # контрольная сумма блока не сошлась
    assert total == 44 and sum(1 for item in items if item.error) == 1
    assert sum(1 for item in items if item.raw) == 43


def test_random_damage_never_crashes_or_hangs(tmp_path, sample):
    """Файл чужой: любая порча должна кончаться понятной ошибкой письма или файла, а не падением."""
    path, _expect, _tmp = sample
    small = _export(tmp_path, [_mail("INBOX", b"Subject: a\r\nTo: x@y.z\r\n\r\nbody one"),
                               _mail("INBOX/Sub", b"Subject: b\r\n\r\nbody two" + b"z" * 20000)], "small.pst")
    original = open(small, "rb").read()
    rnd = random.Random(8)
    started = time.monotonic()
    opened = 0
    for round_no in range(250):
        data = bytearray(original)
        for _ in range(rnd.choice((1, 1, 2, 5, 20))):
            pos = rnd.randrange(0x4400 if round_no % 4 else 0, len(data))
            width = rnd.choice((1, 1, 2, 4, 8))
            data[pos:pos + width] = rnd.randbytes(width) if rnd.random() < 0.7 else b"\xff" * width
        target = tmp_path / "fuzz.pst"
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
            for _item in items:                        # перебор писем не бросает никогда — как в задании
                pass
    assert opened > 100                                # большинство порченых файлов всё же открывается
    assert time.monotonic() - started < 120


class _FakeHeap:
    def __init__(self, allocs):
        self.allocs = allocs

    def get(self, hid):
        if hid not in self.allocs:
            raise rd.PstReadError("нет в куче")
        return self.allocs[hid]


def test_crafted_structures_cannot_blow_up_memory_or_crash():
    # B-дерево кучи, все ссылки которого ведут в один узел: раздувается, а не читается
    header = struct.pack("<BBBBI", 0xB5, 4, 4, 2, 0x40)
    index = b"".join(struct.pack("<II", n, 0x60) for n in range(400))
    heap = _FakeHeap({0x20: header, 0x40: index, 0x60: index, 0x80: b"k" * 8 * 400})
    with pytest.raises(rd.PstReadError, match="дважды"):
        rd.read_bth(heap, 0x20)
    # честное дерево читается по порядку и без рекурсии
    leaf_a = b"".join(struct.pack("<II", n, n * 2) for n in range(0, 4))
    leaf_b = b"".join(struct.pack("<II", n, n * 2) for n in range(4, 8))
    heap = _FakeHeap({0x20: struct.pack("<BBBBI", 0xB5, 4, 4, 1, 0x40),
                      0x40: struct.pack("<IIII", 0, 0x60, 4, 0x80), 0x60: leaf_a, 0x80: leaf_b})
    assert [int.from_bytes(k, "little") for k, _v in rd.read_bth(heap, 0x20)] == list(range(8))
    with pytest.raises(rd.PstReadError, match="велико"):
        rd.read_bth(heap, 0x20, max_records=5)

    # таблица, строка которой «длиннее блока»: раньше — деление на ноль мимо всех обработчиков
    class Layout:
        unicode = True
        block_max = 8176

    class Ndb:
        layout = Layout()

        def __init__(self, blocks):
            self._blocks = blocks

        def subnodes(self, bid):
            return {0x3F: (7, 0)} if bid else {}

        def tree_blocks(self, bid):
            return iter(self._blocks) if bid == 5 else iter([b"x" * 8176])

    table_heap = pf.Heap(0x7C)
    info = table_heap.alloc(struct.pack("<BB4HIII", 0x7C, 0, 8, 8, 8, 9000, 0, 0x3F, 0))
    table_heap.root = info
    with pytest.raises(rd.PstReadError, match="размер строки"):
        rd.read_table(Ndb(table_heap.build()), 5, 9)


# ---------------------------------------------------------------------------
#  Задание импорта: локальная копия
# ---------------------------------------------------------------------------
def _job(services, acc_id, path, **params):
    from mailarchiver.models import JobType
    from mailarchiver.queue.jobs import JobContext, handle_import_pst
    params = dict({"pst_path": path, "target": "local", "target_prefix": "Импорт PST"}, **params)
    job_id = services.db.enqueue_job(JobType.IMPORT_PST, acc_id, params, 5, 1, "tester")
    return handle_import_pst(JobContext(services, job_id, JobType.IMPORT_PST, acc_id, params)), job_id


def _account(services, **kw):
    from mailarchiver import models
    return services.db.create_account(models.Account(name="Архив Иванова", host="h", username="u",
                                                     password="p", **kw))


def _copy(sample, tmp_path, name="upload.pst"):
    target = str(tmp_path / name)
    shutil.copy(sample[0], target)
    return target


def test_import_into_local_copy(services, sample, tmp_path):
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    upload = _copy(sample, tmp_path)
    res, job_id = _job(services, acc_id, upload)
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 44 and res["errors"] == 0
    assert "в локальную копию: 44 из 44" in res["summary"]
    assert not os.path.exists(upload)                          # загруженный файл больше не нужен
    rows = services.db.query("SELECT * FROM messages WHERE account_id=? ORDER BY id", (acc_id,))
    assert len(rows) == 44 and {r["uidvalidity"] for r in rows} == {importer.IMPORTED_UIDVALIDITY}
    assert {r["folder"] for r in rows} == {"Импорт PST/Входящие", "Импорт PST/Входящие/Проекты",
                                           "Импорт PST/Входящие/Проекты/2024", "Импорт PST/Удаленные",
                                           "Импорт PST/Drafts"}
    report = next(r for r in rows if r["subject"] == "Отчёт за сентябрь")
    assert report["flags"] == "\\Seen,\\Answered" and report["has_attach"] == 1
    assert report["message_id"] == "<rep1@example.ru>" and report["from_addr"] == "ivan@example.ru"
    assert report["internaldate"].startswith("2025-09-01T")
    raw = services.store.read_message(acc_id, report["stored_path"])
    assert _sha(raw) == report["sha256"] and _parse(raw)["Subject"] == "Отчёт за сентябрь"
    # копирование по IMAP такие письма не считает кандидатами на перепривязку
    assert services.db.rekey_candidates(acc_id, "Импорт PST/Входящие", 777) == []
    events = [e["message"] for e in services.db.query("SELECT message FROM job_events WHERE job_id=?", (job_id,))]
    assert any("Unicode" in text for text in events)

    # повторный импорт того же файла ничего не удваивает
    res, _ = _job(services, acc_id, _copy(sample, tmp_path))
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 0 and res["duplicates"] == 44
    assert services.db.count_messages(acc_id) == 44
    # без проверки повторов — записывает ещё раз, с новыми номерами
    res, _ = _job(services, acc_id, _copy(sample, tmp_path), skip_existing=False, target_prefix="")
    assert res["imported"] == 44 and services.db.count_messages(acc_id) == 88
    assert services.db.count_folder_messages(acc_id, "Входящие/Проекты/2024") == 40


def test_local_import_keeps_copies_in_different_folders(services, tmp_path):
    """Одно и то же письмо в двух папках файла остаётся в обеих; повторный импорт ничего не удваивает."""
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    raw = b"From: a@b.c\r\nSubject: same\r\nMessage-ID: <same@b.c>\r\n\r\nidentical body"
    items = [_mail("INBOX", raw), _mail("INBOX", raw), _mail("Копии", raw)]
    path = _export(tmp_path, items, "copies.pst")
    keep = str(tmp_path / "copies-again.pst")
    shutil.copy(path, keep)
    res, _ = _job(services, acc_id, path, target_prefix="")
    # два одинаковых письма в одной папке — одно и то же письмо; копия в другой папке — отдельная
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 2 and res["duplicates"] == 1
    assert services.db.count_folder_messages(acc_id, "Входящие") == 1
    assert services.db.count_folder_messages(acc_id, "Копии") == 1
    res, _ = _job(services, acc_id, keep, target_prefix="")
    assert res["imported"] == 0 and res["duplicates"] == 3 and services.db.count_messages(acc_id) == 2


def test_local_import_respects_retention_and_hold(services, sample, tmp_path):
    from mailarchiver.models import JobStatus
    acc_id = _account(services, retention_days=7)
    res, job_id = _job(services, acc_id, _copy(sample, tmp_path))
    # письмам 2025 года больше недели: ночная очистка удалила бы их — не записываем
    assert res["imported"] == 0 and res["outdated"] == 44 and res["final_status"] == JobStatus.SUCCESS
    assert "Старше срока хранения" in res["summary"] and services.db.count_messages(acc_id) == 0
    events = [e["message"] for e in services.db.query("SELECT message FROM job_events WHERE job_id=?", (job_id,))]
    assert any("удержание" in text for text in events)
    services.db.set_account_hold(acc_id, "2099-01-01")
    res, _ = _job(services, acc_id, _copy(sample, tmp_path))
    assert res["imported"] == 44 and res["outdated"] == 0


def test_import_job_rejects_bad_files_and_options(services, sample, tmp_path):
    from mailarchiver.errors import MailArchiverError, ValidationError
    acc_id = _account(services)
    with pytest.raises(ValidationError, match="не найден"):
        _job(services, acc_id, str(tmp_path / "missing.pst"))
    bad = tmp_path / "bad.pst"
    bad.write_bytes(b"this is not a pst file")
    with pytest.raises(ValidationError, match="не разобран") as info:
        _job(services, acc_id, str(bad))
    assert "readpst" in (info.value.hint or "") and not bad.exists()
    with pytest.raises(ValidationError, match="цель"):
        _job(services, acc_id, _copy(sample, tmp_path), target="elsewhere")
    with pytest.raises(ValidationError, match="способ"):
        _job(services, acc_id, _copy(sample, tmp_path), engine="magic")
    if shutil.which("readpst") is None:
        with pytest.raises(MailArchiverError, match="readpst") as info:
            _job(services, acc_id, _copy(sample, tmp_path), engine="readpst")
        assert info.value.code == "dependency_missing"
    assert services.db.count_messages(acc_id) == 0


def test_local_import_index_failures_are_not_reported_as_success(services, sample, tmp_path, monkeypatch):
    """Письма на диске без строк индекса в архиве не видны — такой импорт не «успешен»."""
    from mailarchiver.errors import MailArchiverError
    from mailarchiver.queue.manager import LOCAL_JOB_TYPES
    assert "import_pst" in LOCAL_JOB_TYPES          # два импорта в один ящик очередь разом не запускает
    acc_id = _account(services)
    small = _export(tmp_path, [_mail("INBOX", b"Subject: one\r\n\r\nbody"), _mail("INBOX", b"Subject: two\r\n\r\nb")],
                    "two.pst")
    keep = str(tmp_path / "two-copy.pst")
    shutil.copy(small, keep)

    def locked(rows):
        raise MailArchiverError("database is locked")

    monkeypatch.setattr(services.db, "add_message_index_batch", locked)
    with pytest.raises(MailArchiverError, match="locked"):
        _job(services, acc_id, small)
    monkeypatch.undo()
    assert services.db.count_messages(acc_id) == 0

    # в индекс легло меньше строк, чем записано писем (номера заняты) — тоже сбой, а не тихая потеря
    real = services.db.add_message_index_batch
    monkeypatch.setattr(services.db, "add_message_index_batch", lambda rows: real(rows[:1]))
    with pytest.raises(MailArchiverError, match="В индекс записано 1 писем из 2"):
        _job(services, acc_id, keep, target_prefix="Другая")


def test_local_import_stops_when_disk_is_full(services, sample, tmp_path, monkeypatch):
    from mailarchiver.errors import DiskSpaceError
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    real = services.store.store_message
    stored = [0]

    def store(*args, **kwargs):
        if stored[0] >= 7:
            raise DiskSpaceError("Недостаточно места на диске: свободно 10 МБ")
        stored[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(services.store, "store_message", store)
    upload = _copy(sample, tmp_path)
    res, _ = _job(services, acc_id, upload)
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 7
    assert "кончилось место" in res["summary"] and "не перенесено 37" in res["summary"]
    assert services.db.count_messages(acc_id) == 7             # записанное до остановки — в индексе
    # файл не оставляем: он часто лежит на том же диске, где кончилось место
    assert not os.path.exists(upload) and "«Повторить»" not in res["summary"]


def test_lossy_import_is_partial_and_service_log_has_no_subjects(services, tmp_path, monkeypatch):
    """Письмо, из которого не прочиталось вложение, переносится, но итог — «частично».
    В журнал службы темы писем не попадают: его читает оператор, которому письма не положены."""
    import logging
    from mailarchiver.models import JobStatus
    crafted = _Crafted(str(tmp_path / "lossy.pst"), monkeypatch)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "Секретная тема"),
                 (0x1000, pf.PT_UNICODE, "text")], attachments=[_file("a.txt", b"aaa", mime="text/plain")],
                ghost=True)
    crafted.add([(0x001A, pf.PT_UNICODE, "IPM.Note"), (0x0037, pf.PT_UNICODE, "С объектом"),
                 (0x1000, pf.PT_UNICODE, "text")], attachments=[_file("объект", None, method=6)])
    path = crafted.close()
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Catch()
    logger = importer.log
    logger.addHandler(handler)
    try:
        acc_id = _account(services)
        res, job_id = _job(services, acc_id, path)
    finally:
        logger.removeHandler(handler)
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 2 and res["lossy"] == 1
    assert res["errors"] == 0 and "с потерями" in res["summary"] and "OLE" in res["summary"]
    events = [e["message"] for e in services.db.query("SELECT message FROM job_events WHERE job_id=?", (job_id,))]
    assert any("Секретная тема" in text and "вложение 1 не прочитано" in text for text in events)
    assert records and not any("Секретная тема" in text or "С объектом" in text for text in records)


def test_import_job_can_be_cancelled(services, sample, tmp_path, monkeypatch):
    from mailarchiver.errors import JobCancelled
    from mailarchiver.queue.jobs import JobContext
    acc_id = _account(services)
    calls = [0]

    def cancelled(self):
        calls[0] += 1
        return calls[0] > 16            # шесть проверок — на папках при открытии файла, дальше по одной на письмо

    monkeypatch.setattr(JobContext, "is_cancelled", cancelled)
    upload = _copy(sample, tmp_path)
    with pytest.raises(JobCancelled, match="перенесено 10 из 44"):
        _job(services, acc_id, upload)
    assert services.db.count_messages(acc_id) == 10            # записанное до отмены — в индексе
    assert not os.path.exists(upload)


# ---------------------------------------------------------------------------
#  Задание импорта: сервер IMAP (подменный)
# ---------------------------------------------------------------------------
class FakeServer:
    def __init__(self):
        self.folders = {}
        self.appends = 0
        self.drop_on = set()          # номера APPEND, на которых рвётся связь
        self.quota_after = None       # после стольких писем ящик полон
        self.down = False
        self.bad_folders = set()
        self.delimiter = "."
        self.sessions = 0


class FakeConn:
    def __init__(self, server):
        self.server = server
        self.delimiter = None

    def list_folders(self):
        self.delimiter = self.server.delimiter
        return []

    def ensure_folder(self, folder):
        from mailarchiver.errors import MailArchiverError
        if folder in self.server.bad_folders:
            raise MailArchiverError("нет прав на создание папки")
        self.server.folders.setdefault(folder, [])

    def append(self, folder, raw, flags=(), msg_time=None):
        from mailarchiver.errors import ImapConnectionError, MailArchiverError
        self.server.appends += 1
        if self.server.down or self.server.appends in self.server.drop_on:
            raise ImapConnectionError("соединение разорвано")
        stored = sum(len(v) for v in self.server.folders.values())
        if self.server.quota_after is not None and stored >= self.server.quota_after:
            raise MailArchiverError("APPEND failed: [OVERQUOTA] Quota exceeded")
        self.server.folders[folder].append({"raw": raw, "flags": list(flags), "time": msg_time})

    def fetch_existing_message_ids(self, folder):
        return {_parse(m["raw"])["Message-ID"] for m in self.server.folders.get(folder, [])}

    def select(self, folder, readonly=True):
        return {"exists": len(self.server.folders.get(folder, []))}

    def search_header_messageid(self, message_id):
        return []

    def close(self, force=False):
        pass


@pytest.fixture()
def imap(monkeypatch):
    from mailarchiver.imap import client as client_mod
    server = FakeServer()

    class FakeSession:
        def __init__(self, account, options, emit, check_cancel):
            self.conn = None
            self.reconnects = 0
            self._fails = 0

        def open(self):
            from mailarchiver.errors import ImapConnectionError
            server.sessions += 1
            if server.down:
                raise ImapConnectionError("сервер недоступен")
            self.conn = FakeConn(server)
            return self.conn

        def progressed(self):
            self._fails = 0

        def reconnect(self, exc):
            self._fails += 1
            if self._fails > 2 or server.down:
                raise exc
            self.reconnects += 1
            return self.open()             # как настоящая сессия: список папок заново не запрашивается

        def close(self, force=False):
            self.conn = None

    monkeypatch.setattr(client_mod, "ReconnectingSession", FakeSession)
    return server


def test_import_to_imap_server(services, sample, tmp_path, imap):
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    imap.drop_on = {3, 4}                                      # связь рвётся дважды на одном письме
    upload = _copy(sample, tmp_path)
    res, _job_id = _job(services, acc_id, upload, target="imap", target_prefix="Архив")
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 44 and res["errors"] == 0
    assert "на сервер: 44 из 44" in res["summary"] and "Переподключений после обрыва связи: 2" in res["summary"]
    assert not os.path.exists(upload) and services.db.count_messages(acc_id) == 0     # в архив не пишем
    assert set(imap.folders) == {"Архив.Входящие", "Архив.Входящие.Проекты", "Архив.Входящие.Проекты.2024",
                                 "Архив.Удаленные", "Архив.Drafts"}        # разделитель — как у сервера
    assert sum(len(v) for v in imap.folders.values()) == 44
    report = next(m for m in imap.folders["Архив.Входящие"] if _parse(m["raw"])["Subject"] == "Отчёт за сентябрь")
    assert report["flags"] == ["\\Seen", "\\Answered"] and report["time"].timestamp() == DATE
    assert imap.folders["Архив.Drafts"][0]["flags"] == ["\\Draft"]
    forwarded = imap.folders["Архив.Входящие.Проекты"][0]
    assert forwarded["flags"] == ["\\Seen", "$Forwarded", "\\Flagged"]

    # повторная заливка того же файла: всё уже есть в папках назначения
    res, _ = _job(services, acc_id, _copy(sample, tmp_path), target="imap", target_prefix="Архив")
    assert res["imported"] == 0 and res["duplicates"] == 44
    assert sum(len(v) for v in imap.folders.values()) == 44


def test_imap_import_stops_on_lost_link_and_keeps_the_file(services, sample, tmp_path, imap):
    from mailarchiver.errors import ImapConnectionError
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    upload = _copy(sample, tmp_path)
    imap.drop_on = set(range(6, 100))                          # после пяти писем сервер перестал отвечать
    res, job_id = _job(services, acc_id, upload, target="imap")
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 5
    assert "связь с сервером потеряна" in res["summary"] and "не перенесено 39" in res["summary"]
    assert "«Повторить»" in res["summary"] and os.path.exists(upload)      # файл оставлен для повтора
    # повтор того же задания доливает остальное без дублей
    imap.drop_on = set()
    from mailarchiver.models import JobType
    from mailarchiver.queue.jobs import JobContext, handle_import_pst
    params = {"pst_path": upload, "target": "imap", "target_prefix": "Импорт PST"}
    res = handle_import_pst(JobContext(services, job_id, JobType.IMPORT_PST, acc_id, params))
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 39 and res["duplicates"] == 5
    assert sum(len(v) for v in imap.folders.values()) == 44 and not os.path.exists(upload)

    # сервер недоступен с самого начала: задание падает, но файл остаётся
    imap.down = True
    upload = _copy(sample, tmp_path, "again.pst")
    with pytest.raises(ImapConnectionError):
        _job(services, acc_id, upload, target="imap")
    assert os.path.exists(upload)


def test_imap_import_keeps_file_on_login_failure_and_retries_without_keywords(services, sample, tmp_path, imap,
                                                                             monkeypatch):
    from mailarchiver.errors import ImapAuthError, MailArchiverError
    from mailarchiver.imap import client as client_mod
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    # сервер, который не принимает пользовательские метки: «$Forwarded» ронял бы заливку письма
    real_append = FakeConn.append

    def append(self, folder, raw, flags=(), msg_time=None):
        if any(not flag.startswith("\\") for flag in flags):
            raise MailArchiverError("APPEND failed: [CANNOT] Keywords are not supported")
        return real_append(self, folder, raw, flags, msg_time)

    monkeypatch.setattr(FakeConn, "append", append)
    res, _ = _job(services, acc_id, _copy(sample, tmp_path), target="imap")
    assert res["final_status"] == JobStatus.SUCCESS and res["imported"] == 44
    assert imap.folders["Импорт PST.Входящие.Проекты"][0]["flags"] == ["\\Seen", "\\Flagged"]

    # неверный пароль: задание падает, а файл остаётся — исправят пароль и нажмут «Повторить»
    session = client_mod.ReconnectingSession

    class Refusing(session):
        def open(self):
            raise ImapAuthError("Сервер отклонил пароль")

    monkeypatch.setattr(client_mod, "ReconnectingSession", Refusing)
    upload = _copy(sample, tmp_path, "auth.pst")
    old = time.time() - 40 * 3600
    os.utime(upload, (old, old))
    with pytest.raises(ImapAuthError):
        _job(services, acc_id, upload, target="imap")
    assert os.path.exists(upload) and time.time() - os.path.getmtime(upload) < 60   # 48 часов — от этой минуты


def test_imap_import_quota_and_unavailable_folder(services, sample, tmp_path, imap):
    from mailarchiver.models import JobStatus
    acc_id = _account(services)
    imap.bad_folders = {"Импорт PST.Удаленные"}
    imap.quota_after = 20
    res, _ = _job(services, acc_id, _copy(sample, tmp_path), target="imap")
    assert res["final_status"] == JobStatus.PARTIAL and res["imported"] == 20
    assert res["errors"] == 1                                  # письмо недоступной папки
    assert "кончилось место в ящике" in res["summary"] and any("недоступна" in e for e in res["problems"])


# ---------------------------------------------------------------------------
#  API (нужен FastAPI)
# ---------------------------------------------------------------------------
def test_import_api_accepts_target_and_validates_options(client, sample):
    client.post("/api/setup", json={"username": "admin", "password": "Sw0rdfish!"})
    client.post("/api/login", json={"username": "admin", "password": "Sw0rdfish!"})
    aid = client.post("/api/accounts", json={"name": "Иванов", "host": "h", "port": 993,
                                             "username": "ivanov@x.ru", "password": "p"}).json()["id"]
    svc = client.app.state.services
    data = open(sample[0], "rb").read()

    def post(**form):
        return client.post(f"/api/accounts/{aid}/import-pst", data=form,
                           files={"file": ("Outlook.pst", data, "application/octet-stream")})

    assert post(target="elsewhere").status_code == 400
    assert post(engine="magic").status_code == 400
    assert post(ansi_codepage="utf-99").status_code == 400
    r = post(target="local", target_prefix="  Архив 2025  ", skip_existing="0", ansi_codepage="cp1250")
    assert r.status_code == 200
    job = svc.db.get_job(r.json()["job_id"])
    import json
    params = json.loads(job["params"])
    assert params["target"] == "local" and params["target_prefix"] == "Архив 2025"
    assert params["skip_existing"] is False and params["engine"] == "builtin"
    assert params["ansi_codepage"] == "cp1250" and os.path.basename(params["pst_path"]).startswith("import_")
    svc.queue.stop()                # дальше смотрим только параметры поставленных заданий
    # пустое поле префикса — это «не указано» (значение по умолчанию); без общей папки — только явно
    plain = json.loads(svc.db.get_job(post(target="local", target_prefix="").json()["job_id"])["params"])
    assert plain["target_prefix"] == "Импорт PST" and plain["target"] == "local"
    bare = json.loads(svc.db.get_job(post(target="local", no_prefix="1").json()["job_id"])["params"])
    assert bare["target_prefix"] == ""
    deadline = time.time() + 60
    while time.time() < deadline and svc.db.get_job(job["id"])["status"] not in ("success", "partial", "failed"):
        time.sleep(0.2)
    assert svc.db.get_job(job["id"])["status"] == "success"
    assert svc.db.count_messages(aid) == 44
    assert any(row["action"] == "import_pst" for row in svc.db.query("SELECT action FROM audit"))
    # подсказки к полям окна импорта отдаются интерфейсу
    assert "target" in client.get("/api/help").json()["import"]
