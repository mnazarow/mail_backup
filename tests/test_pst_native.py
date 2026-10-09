"""Встроенный экспорт в .pst: формат по [MS-PST], письма с вложениями, части, отмена.

Файл проверяется тремя независимыми читателями: собственным строгим
(tests/pst_reader.py — CRC, подписи, карты размещения, кучи, таблицы),
libpff (pypff) и libpst (readpst), если они установлены.
"""
import email
import glob
import hashlib
import os
import random
import shutil
import subprocess
from email import policy
from email.message import EmailMessage

import pytest

from mailarchiver.export import pst_native, pstformat
from mailarchiver.export.base import MailItem

from pst_reader import Pst


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _export(tmp_path, items, **options):
    out = str(tmp_path / "box.pst")
    options.setdefault("tmp_dir", str(tmp_path))
    res = pst_native.NativePstExportEngine().export(iter(items), out, options=options,
                                                    total_hint=len(items))
    return out, res


def _check(path: str) -> Pst:
    pst = Pst(path)
    pst.check_maps()
    pst.check_references()
    assert not pst.problems, pst.problems
    return pst


def _tree(pst: Pst):
    return {path.split("/", 1)[1] if "/" in path else path: nid for path, nid in pst.folder_tree()}


def _mail(folder, raw, flags=(), date=1_756_700_000):
    return MailItem(folder=folder, raw=raw, flags=list(flags), internaldate=date, size=len(raw))


@pytest.fixture(scope="module")
def sample():
    """Письма «как в жизни» и то, что должно из них получиться."""
    rnd = random.Random(5)
    expect = {}
    items = []

    m = EmailMessage()
    m["From"] = "Иван Петров <ivan@example.ru>"
    m["To"] = "Мария <maria@example.ru>, bob@example.com"
    m["Cc"] = '"Сидоров, Пётр" <petr@example.ru>'
    m["Subject"] = "Отчёт за сентябрь"
    m["Date"] = "Mon, 01 Sep 2025 10:00:00 +0300"
    m["Message-ID"] = "<rep1@example.ru>"
    m.set_content("Текстовая версия отчёта.")
    m.add_alternative('<html><body><p>HTML <b>отчёт</b></p><img src="cid:logo1@x"></body></html>',
                      subtype="html")
    logo = rnd.randbytes(5000)
    m.get_payload()[1].add_related(logo, maintype="image", subtype="png", cid="<logo1@x>")
    pdf = rnd.randbytes(20000)
    m.add_attachment(pdf, maintype="application", subtype="pdf", filename="Отчёт сентябрь 2025.pdf")
    tiny = b"x" * 100
    m.add_attachment(tiny, maintype="text", subtype="plain", filename="заметка.txt")
    items.append(_mail("INBOX", m.as_bytes(), ["\\Seen", "\\Answered"]))
    expect["report"] = {"Отчёт сентябрь 2025.pdf": _sha(pdf), "заметка.txt": _sha(tiny),
                        "logo": _sha(logo)}

    m = EmailMessage()
    m["From"] = "boss@example.com"
    m["To"] = "me@example.ru"
    m["Subject"] = "Fwd: большой файл"
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

    koi = "Отдел кадров".encode("koi8-r")
    raw = (b"From: =?koi8-r?B?" + __import__("base64").b64encode(koi) + b"?= <hr@example.ru>\r\n"
           b"To: all@example.ru\r\nSubject: =?windows-1251?Q?=CF=F0=E8=EA=E0=E7?=\r\n"
           b"Content-Type: text/plain; charset=windows-1251\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
           + "Приказ №5 о переносе выходных.".encode("cp1251"))
    items.append(_mail("INBOX", raw))

    html = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1251">'
            '</head><body>Только HTML: привет</body></html>').encode("cp1251")
    raw = (b"From: a@b.c\r\nTo: d@e.f\r\nBcc: secret@e.f\r\nSubject: html only\r\nMIME-Version: 1.0\r\n"
           b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n--XX\r\nContent-Type: text/html\r\n\r\n"
           + html + b"\r\n--XX--\r\n")
    items.append(_mail("Trash", raw, ["\\Seen"]))
    expect["html"] = html

    raw = b"From: me@example.ru\r\nTo: \"Doe, John\" <j@x>, \"\r\nSubject: \x01broken\r\n\r\nbody"
    items.append(_mail("Sent", raw, ["\\Draft", "\\Seen"]))
    for i in range(600):                                   # индекс строк в несколько уровней
        raw = (f"From: n{i}@x.ru\r\nTo: me@x.ru\r\nSubject: письмо {i}\r\n"
               f"Date: Wed, 03 Sep 2025 10:{i % 60:02d}:00 +0000\r\n\r\nтекст {i}\r\n").encode()
        items.append(_mail("Архив/2024", raw, ["\\Seen"] if i % 3 else []))
    return items, expect


@pytest.fixture(scope="module")
def exported(sample, tmp_path_factory):
    items, expect = sample
    tmp = tmp_path_factory.mktemp("pst")
    out, res = _export(tmp, items, pst_display_name="Иван Петров")
    return out, res, expect


def _find(pst: Pst, folder: str, subject: str):
    for mid in pst.messages(_tree(pst)[folder]):
        props, recips, atts = pst.message(mid)
        if props[0x0037][1] == subject:
            return props, recips, atts
    raise AssertionError(f"письмо «{subject}» не найдено в «{folder}»")


def test_file_is_valid_unicode_pst(exported):
    out, res, _e = exported
    assert res.count == 605 and res.errors == 0 and not res.parts
    pst = _check(out)
    assert pst.ver == 23
    store = pst.node_pc(0x21)
    assert store[0x3001][1] == "Иван Петров"
    assert store[0x35DF][1] & 0x1B == 0x1B            # IPM, «Входящие», «Удалённые», «Отправленные»
    # обязательные узлы [MS-PST] 2.7.1
    for nid in (0x21, 0x61, 0x122, 0x12D, 0x12E, 0x12F, 0x1E1, 0x201, 0x60D, 0x60E, 0x60F, 0x610,
                0x671, 0x692, 0x2223, 0x8022, 0x802D, 0x802E, 0x802F, 0x8042, 0x804D, 0x804E,
                0x804F, 0x8062, 0x806D, 0x806E, 0x806F):
        assert nid in pst.nbt, hex(nid)
    tree = _tree(pst)
    assert {"Входящие", "Входящие/Проекты", "Удаленные", "Sent", "Архив", "Архив/2024"} <= set(tree)
    assert tree["Удаленные"] == 0x8062
    assert len(pst.messages(tree["Архив/2024"])) == 600
    folder = pst.node_pc(tree["Архив/2024"])
    assert folder[0x3602][1] == 600 and folder[0x3603][1] == 200     # всего / непрочитанных
    receive = pst.node_tc(0x62B)["rows"]
    assert {r[0x001A001F]: r[0x66050003] for r in receive}["IPM"] == tree["Входящие"]


def test_message_properties_and_attachments(exported):
    out, _res, expect = exported
    pst = _check(out)
    props, recips, atts = _find(pst, "Входящие", "Отчёт за сентябрь")
    assert props[0x0C1A][1] == "Иван Петров" and props[0x0C1F][1] == "ivan@example.ru"
    assert props[0x0E04][1] == "Мария; bob@example.com" and props[0x0E03][1] == "Сидоров, Пётр"
    assert [(r[0x0C150003], r[0x3003001F]) for r in recips] == [
        (1, "maria@example.ru"), (1, "bob@example.com"), (2, "petr@example.ru")]
    assert props[0x0E07][1] & 0x11 == 0x11                    # прочитано + есть вложения
    assert props[0x1081][1] == 102                            # «ответил»
    assert props[0x1035][1] == "<rep1@example.ru>"
    assert "Текстовая версия" in props[0x1000][1]
    assert b"cid:logo1@x" in props[0x1013][1] and props[0x3FDE][1] == 65001
    by_name = {a[0x3707][1]: a for _row, a in atts}
    assert _sha(by_name["Отчёт сентябрь 2025.pdf"][0x3701][1]) == expect["report"]["Отчёт сентябрь 2025.pdf"]
    assert _sha(by_name["заметка.txt"][0x3701][1]) == expect["report"]["заметка.txt"]
    logo = [a for a in by_name.values() if a.get(0x3712, (0, ""))[1] == "logo1@x"][0]
    assert logo[0x7FFE][1] == 1 and _sha(logo[0x3701][1]) == expect["report"]["logo"]
    assert by_name["Отчёт сентябрь 2025.pdf"][0x7FFE][1] == 0

    props, _r, atts = _find(pst, "Входящие/Проекты", "Fwd: большой файл")
    by_name = {a[0x3707][1]: a for _row, a in atts}
    assert _sha(by_name["big.bin"][0x3701][1]) == expect["big"]
    inner = email.message_from_bytes(by_name["Внутреннее письмо.eml"][0x3701][1], policy=policy.default)
    assert inner.get_content().strip() == "Привет из вложенного письма"
    assert props[0x1090][1] == 2 and props[0x1081][1] == 104  # помечено, переслано


def test_charsets_and_broken_headers(exported):
    out, _res, expect = exported
    pst = _check(out)
    props, _r, _a = _find(pst, "Входящие", "Приказ")
    assert props[0x0C1A][1] == "Отдел кадров"
    assert props[0x1000][1] == "Приказ №5 о переносе выходных."
    assert props[0x0E07][1] & 1 == 0                          # не прочитано
    props, recips, _a = _find(pst, "Удаленные", "html only")
    assert props[0x1013][1] == expect["html"] and props[0x3FDE][1] == 1251
    assert "Только HTML" in props[0x1000][1]                  # текст получен из HTML
    assert props[0x0E02][1] == "secret@e.f" and recips[-1][0x0C150003] == 3
    props, recips, _a = _find(pst, "Sent", "broken")
    assert props[0x0E07][1] & 0x08                            # черновик
    assert recips[0][0x3003001F] == "j@x"


def test_libpff_reads_everything(exported):
    pypff = pytest.importorskip("pypff")
    out, _res, expect = exported
    f = pypff.file()
    f.open(out)
    try:
        seen = {}

        def walk(folder):
            for i in range(folder.number_of_sub_messages):
                msg = folder.get_sub_message(i)
                for a in range(msg.number_of_attachments):
                    att = msg.get_attachment(a)
                    seen.setdefault(msg.subject, []).append(_sha(att.read_buffer(att.size) if att.size else b""))
                seen.setdefault(msg.subject, [])
            for i in range(folder.number_of_sub_folders):
                walk(folder.get_sub_folder(i))

        walk(f.get_root_folder())
    finally:
        f.close()
    assert len(seen) == 605
    assert expect["big"] in seen["Fwd: большой файл"]
    assert expect["report"]["Отчёт сентябрь 2025.pdf"] in seen["Отчёт за сентябрь"]


@pytest.mark.skipif(shutil.which("readpst") is None, reason="readpst не установлен")
def test_readpst_extracts_attachments(exported, tmp_path):
    out, _res, expect = exported
    proc = subprocess.run(["readpst", "-j", "0", "-e", "-o", str(tmp_path), out],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    files = glob.glob(str(tmp_path / "**" / "*.eml"), recursive=True)
    assert len(files) == 605
    found = {}
    for path in files:
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=policy.default)
        for part in msg.walk():
            if part.get_filename() and part.get_content_type() != "message/rfc822":
                found[part.get_filename()] = _sha(part.get_payload(decode=True) or b"")
    assert found["big.bin"] == expect["big"]
    assert found["Отчёт сентябрь 2025.pdf"] == expect["report"]["Отчёт сентябрь 2025.pdf"]


def test_many_recipients_long_subjects_multiblock_heaps(tmp_path):
    m = EmailMessage()
    m["From"] = "x@x.ru"
    m["Subject"] = "много получателей"
    m["To"] = ", ".join(f"Получатель {i} <r{i}@example.ru>" for i in range(2500))
    m.set_content("hi")
    items = [_mail("Рассылки", m.as_bytes())]
    long_subject = "Длинная тема письма для проверки кучи " * 7
    for i in range(1500):
        raw = f"From: a{i}@x.ru\r\nTo: b@x.ru\r\nSubject: {i} {long_subject}\r\n\r\nтело\r\n".encode()
        items.append(_mail("INBOX", raw, ["\\Seen"]))
    out, res = _export(tmp_path, items)
    assert res.count == 1501 and res.errors == 0
    pst = _check(out)
    tree = _tree(pst)
    contents = pst.nbt[(tree["Входящие"] & ~0x1F) | 0x0E]
    assert len(pst.heap(contents[0])["allocs"]) > 1500           # куча таблицы на десятках блоков
    assert len(pst.blocks(contents[0])) > 136                     # есть блоки с HNBITMAPHDR
    props, recips, _a = pst.message(pst.messages(tree["Рассылки"])[0])
    assert len(recips) == 2500 and props[0x0E04][1].count(";") == 2499


def test_large_file_has_fmap_page(tmp_path):
    rnd = random.Random(3)
    items = []
    for i in range(7):
        m = EmailMessage()
        m["From"] = "x@x.ru"
        m["Subject"] = f"вложение {i}"
        m.set_content("см.")
        m.add_attachment(rnd.randbytes(5 * 1024 * 1024), maintype="application",
                         subtype="octet-stream", filename=f"d{i}.bin")
        items.append(_mail("INBOX", m.as_bytes()))
    out, res = _export(tmp_path, items)
    assert res.count == 7
    size = os.path.getsize(out)
    assert size > 0x4400 + 128 * 0x3E000                          # больше 128 областей AMap
    _check(out)                                                   # FMap на месте и верна


def test_split_into_parts_each_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(pst_native, "MIN_SPLIT_BYTES", 256 * 1024)
    rnd = random.Random(1)
    items = []
    for i in range(300):
        body = " ".join(rnd.choice(["архив", "письмо", "договор", "отчёт"]) for _ in range(1500))
        raw = f"Subject: n{i}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n".encode()
        items.append(_mail(["INBOX", "Отправленные"][i % 2], raw, ["\\Seen"]))
    out, res = _export(tmp_path, items, pst_split_size_mb=1)
    assert res.count == 300 and len(res.parts) >= 2 and not os.path.exists(out)
    assert os.path.basename(res.parts[0]) == "box_part1.pst"
    total = 0
    for part in res.parts:
        assert os.path.getsize(part) <= 1024 * 1024
        pst = _check(part)
        tree = _tree(pst)
        assert {"Входящие", "Отправленные"} <= set(tree)          # дерево папок в каждой части
        total += sum(len(pst.messages(nid)) for nid in tree.values())
    assert total == 300
    assert "части" in res.warning


def test_ansi_request_gets_unicode_with_note(tmp_path):
    out, res = _export(tmp_path, [_mail("INBOX", b"Subject: a\r\n\r\nb")], pst_format="ansi")
    assert res.count == 1 and "ANSI" in res.warning
    assert _check(out).ver == 23


def test_broken_message_is_rolled_back(tmp_path, monkeypatch):
    real = pst_native.write_message
    calls = {"n": 0}

    def flaky(pst, fh, folder_nid, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            pst.stream().write(b"z" * 50000)                     # полписьма уже на диске
            stream = pst.stream()
            stream.write(b"y" * 20000)
            stream.finish()
            raise ValueError("сломалось посреди письма")
        return real(pst, fh, folder_nid, **kw)

    monkeypatch.setattr(pst_native, "write_message", flaky)
    items = [_mail("INBOX", f"Subject: m{i}\r\n\r\nbody {i}".encode()) for i in range(3)]
    out, res = _export(tmp_path, items)
    assert res.count == 2 and res.errors == 1
    assert "сломалось" in res.error_details[0]
    pst = _check(out)                                   # в файле нет «брошенных» блоков
    subjects = sorted(pst.message(m)[0][0x37][1] for m in pst.messages(_tree(pst)["Входящие"]))
    assert subjects == ["m0", "m2"]


def test_cancel_leaves_no_files(tmp_path):
    items = [_mail("INBOX", f"Subject: m{i}\r\n\r\nx".encode()) for i in range(50)]
    out = str(tmp_path / "box.pst")
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        return state["n"] > 60                          # отмена посреди записи писем

    res = pst_native.NativePstExportEngine().export(iter(items), out, options={"tmp_dir": str(tmp_path)},
                                                    cancel_cb=cancel)
    assert res.bytes_written == 0
    assert not glob.glob(str(tmp_path / "*.pst")) and not glob.glob(str(tmp_path / "pstspool_*"))


def test_folder_plan_special_folders_and_duplicates():
    plan, mapping = pst_native.plan_folders(["INBOX", "INBOX/Work", "Trash", "Корзина", "Sent",
                                             "Входящие", "inbox/sub", "A/B", "a"])
    names = {f.key: (f.name, f.role) for f in plan}
    assert names[mapping["INBOX"]] == ("Входящие", "inbox")
    assert mapping["inbox/sub"][0] == mapping["INBOX"][0]            # регистр INBOX не важен
    assert names[mapping["Trash"]] == ("Удаленные", "trash")
    assert names[mapping["Корзина"]][0] == "Корзина"                 # вторая корзина — обычная папка
    assert names[mapping["Sent"]][1] == "sent"
    tops = [f.name for f in plan if f.parent is None]
    assert tops[0] == "Входящие" and tops[-1] == "Удаленные"
    assert len({n.lower() for n in tops}) == len(tops)               # соседи с разными именами
    assert "Входящие (2)" in tops and "a (2)" in tops


def test_part_hard_limit_raises_full(tmp_path):
    writer = pstformat.NdbWriter(str(tmp_path / "x.pst"), max_bytes=0x4400 + 2 * pstformat.AMAP_SPAN)
    try:
        with pytest.raises(pstformat.PstFullError):
            for _ in range(200):
                writer.write_block(b"q" * 8000)
    finally:
        writer.close()


def test_export_job_writes_pst_with_attachments(services):
    """Задание «Экспорт» через очередь: .pst из локальной копии, имя хранилища — имя ящика."""
    from mailarchiver import models
    from mailarchiver.models import JobType
    from mailarchiver.queue.jobs import JobContext, handle_export

    acc_id = services.db.create_account(models.Account(name="Абрамова Екатерина", host="h",
                                                       username="u", password="p"))
    m = EmailMessage()
    m["From"] = "Иван <ivan@example.ru>"
    m["To"] = "e.abramova@example.ru"
    m["Subject"] = "Договор"
    m.set_content("Во вложении договор.")
    doc = random.Random(9).randbytes(30000)
    m.add_attachment(doc, maintype="application", subtype="msword", filename="Договор №7.doc")
    raws = [("INBOX", m.as_bytes()), ("INBOX", b"Subject: second\r\n\r\ntext"),
            ("Отправленные", b"Subject: sent one\r\nTo: x@y.z\r\n\r\ntext")]
    rows = []
    for uid, (folder, raw) in enumerate(raws, start=1):
        rel, sha, size = services.store.store_message(acc_id, folder, "/", uid, raw)
        rows.append((acc_id, folder, 1, uid, f"<{uid}@x>", size, "2026-09-01T10:00:00+00:00",
                     "\\Seen", rel, sha, "t", "a@b", 0))
    services.db.add_message_index_batch(rows)
    params = {"engine": "native", "format": "pst"}
    job_id = services.db.enqueue_job(JobType.EXPORT, acc_id, params, 5, 1, "tester")
    res = handle_export(JobContext(services, job_id, JobType.EXPORT, acc_id, params))
    assert res["count"] == 3 and res["errors"] == 0 and res["path"].endswith(".pst")
    pst = _check(res["path"])
    assert pst.node_pc(0x21)[0x3001][1] == "Абрамова Екатерина"
    tree = _tree(pst)
    assert len(pst.messages(tree["Входящие"])) == 2 and len(pst.messages(tree["Отправленные"])) == 1
    _props, _r, atts = _find(pst, "Входящие", "Договор")
    assert atts[0][1][0x3707][1] == "Договор №7.doc" and _sha(atts[0][1][0x3701][1]) == _sha(doc)


def test_html_to_text_is_linear_on_unclosed_tags():
    import time as _time
    html = "<style>" * 8000 + "x" * 1_000_000
    started = _time.monotonic()
    assert pst_native.html_to_text(html) == ""                 # незакрытый блок — до конца
    assert _time.monotonic() - started < 2
    assert pst_native.html_to_text("<header>Шапка</header><style>p{}</style>Текст") == "Шапка Текст"


def test_edge_cases_codepage_dates_headers_boundaryless(tmp_path):
    latin = "<html><head><meta charset=iso-8859-1></head><body>Café</body></html>".encode("latin-1")
    raw_latin = (b"Subject: latin\r\nContent-Type: text/html; charset=iso-8859-1\r\n\r\n" + latin)
    no_dates = b"Subject: no dates\r\n\r\nbody"
    huge_header = b"X-Big: " + b"a" * (1_500_000) + b"\r\nSubject: after big header\r\nFrom: z@z.z\r\n\r\nbody"
    boundaryless = (b"Subject: no boundary\r\nContent-Type: multipart/mixed\r\n\r\n"
                    b"plain text actually")
    items = [_mail("INBOX", raw_latin), MailItem(folder="INBOX", raw=no_dates, size=len(no_dates)),
             _mail("INBOX", huge_header), _mail("INBOX", boundaryless)]
    out, res = _export(tmp_path, items)
    assert res.count == 4 and res.errors == 0
    pst = _check(out)
    props, _r, _a = _find(pst, "Входящие", "latin")
    assert props[0x3FDE][1] == 28591 and props[0x1013][1] == latin     # байты и кодировка — как в письме
    props, _r, _a = _find(pst, "Входящие", "no dates")
    assert 0x3007 in props and 0x3008 in props and 0x0E06 not in props
    props, _r, _a = _find(pst, "Входящие", "after big header")
    assert props[0x0C1F][1] == "z@z.z"
    props, _r, atts = _find(pst, "Входящие", "no boundary")
    assert props[0x1000][1] == "plain text actually" and not atts


def test_message_bigger_than_part_goes_into_own_part(tmp_path, monkeypatch):
    monkeypatch.setattr(pst_native, "MIN_SPLIT_BYTES", 256 * 1024)
    rnd = random.Random(4)
    m = EmailMessage()
    m["Subject"] = "огромное"
    m.set_content("см.")
    m.add_attachment(rnd.randbytes(3 * 1024 * 1024), maintype="application", subtype="octet-stream",
                     filename="big.bin")
    items = [_mail("INBOX", b"Subject: first\r\n\r\nx"), _mail("INBOX", m.as_bytes()),
             _mail("INBOX", b"Subject: last\r\n\r\nx")]
    out, res = _export(tmp_path, items, pst_split_size_mb=1)
    assert res.count == 3 and res.errors == 0 and len(res.parts) == 3
    sizes = [os.path.getsize(p) for p in res.parts]
    assert sizes[0] <= 1024 * 1024 and sizes[1] > 3 * 1024 * 1024 and sizes[2] <= 1024 * 1024
    for part in res.parts:
        _check(part)


def test_dot_delimiter_namespace_is_lifted():
    plan, mapping = pst_native.plan_folders({"INBOX": ".", "INBOX.Sent": ".", "INBOX.Trash": ".",
                                             "INBOX.Работа.2024": "."})
    names = {f.key: (f.name, f.role, f.parent) for f in plan}
    assert names[mapping["INBOX.Sent"]][:2] == ("Sent", "sent")
    assert names[mapping["INBOX.Trash"]][:2] == ("Удаленные", "trash")
    assert names[mapping["INBOX.Работа.2024"]][0] == "2024"
    assert names[mapping["INBOX.Работа.2024"]][2] == ("Работа",)          # «Работа» — рядом с «Входящими»
    # на сервере с «/» подпапки INBOX так и остаются подпапками
    plan, mapping = pst_native.plan_folders({"INBOX": "/", "INBOX/Work": "/"})
    assert mapping["INBOX/Work"][0] == mapping["INBOX"][0]
    # разделитель неизвестен — прежнее поведение: «/» и «\\»
    plan, mapping = pst_native.plan_folders(["A/B", "C.D"])
    assert mapping["A/B"] == ("A", "B") and mapping["C.D"] == ("C.D",)


def test_folder_delimiter_is_recovered_from_storage(services):
    cache = {}
    store = services.store
    for folder, delim in (("INBOX", "/"), ("INBOX.Отчёты", "."), ("Sent.2024", "/")):
        rel = os.path.join(store.folder_relpath(folder, delim), "cur", "1.eml")
        got = services._folder_delimiter(folder, rel, cache)
        assert store.folder_relpath(folder, got) == store.folder_relpath(folder, delim)
    assert cache["INBOX.Отчёты"] == "." and cache["Sent.2024"] == "/"
    assert services._folder_delimiter("X", "", {}) == ""


def test_table_heap_limit_starts_new_part(tmp_path, monkeypatch):
    """Куча таблицы папки близка к пределу формата — дальше пишется новая часть, а не ошибка."""
    monkeypatch.setattr(pstformat.TableWriter, "heap_nearly_full", lambda self: len(self.ids) >= 5)
    items = [_mail("INBOX", f"Subject: m{i}\r\n\r\nx".encode()) for i in range(12)]
    out, res = _export(tmp_path, items)
    assert res.count == 12 and res.errors == 0 and len(res.parts) == 3
    counts = []
    for part in res.parts:
        pst = _check(part)
        counts.append(len(pst.messages(_tree(pst)["Входящие"])))
    assert counts == [5, 5, 2]


def test_table_text_limit_counts_utf16_units():
    assert len(pst_native._table_text("😀" * 300).encode("utf-16-le")) <= 510
    assert pst_native._table_text("я" * 300) == "я" * 255
