"""Текст вложений для поиска (1.20.0): PDF, Office, OpenDocument, RTF, текст — и индексация."""
import io
import zipfile
import zlib
from email.message import EmailMessage

from mailarchiver import attachtext, models, search


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _pdf(words, *, compress=True, tounicode=True):
    """PDF с составным шрифтом (Identity-H): коды глифов 1, 2, 3… и таблица ToUnicode для них."""
    chars = sorted({ch for w in words for ch in w})
    code = {ch: i + 1 for i, ch in enumerate(chars)}
    cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap 1 begincodespacerange <0000> <FFFF> "
            "endcodespacerange %d beginbfchar\n" % len(chars)
            + "\n".join("<%04X> <%04X>" % (code[ch], ord(ch)) for ch in chars)
            + "\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end").encode()
    # каждое слово — отдельный кусок со сдвигом Td: пробел определяется по ширинам глифов
    ops = [b"BT /F1 12 Tf 1 0 0 1 50 700 Tm"]
    for k, w in enumerate(words):
        if w == "\n":
            ops.append(b"0 -20 Td")
            continue
        hexs = "".join("%04X" % code[ch] for ch in w)
        ops.append(f"<{hexs}> Tj".encode())
        ops.append(f"{len(w) * 6 + 4} 0 Td".encode())
    ops.append(b"ET")
    content = b"\n".join(ops)
    objs = []

    def stream(data, extra=b""):
        if compress:
            data = zlib.compress(data)
            extra += b" /Filter /FlateDecode"
        return b"<< /Length %d%s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> "
                b"/Contents 4 0 R >>")
    objs.append(stream(content))
    objs.append(b"<< /Type /Font /Subtype /Type0 /BaseFont /X /Encoding /Identity-H /DescendantFonts [7 0 R]"
                + (b" /ToUnicode 6 0 R" if tounicode else b"") + b" >>")
    objs.append(stream(cmap))
    objs.append(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /X /DW 500 >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    out.write(b"trailer << /Root 1 0 R >>\n%%EOF\n")
    return out.getvalue()


def test_pdf_tounicode_and_spacing():
    data = _pdf(["Счёт", "на", "оплату", "\n", "Итого:", "100", "руб."])
    text = attachtext.extract("счёт.pdf", "application/pdf", data)
    assert text == "Счёт на оплату\nИтого: 100 руб."
    assert attachtext.extract("a.pdf", "", _pdf(["Договор"], compress=False)) == "Договор"
    # без таблицы ToUnicode коды глифов текстом не становятся — мусора в индексе нет
    assert attachtext.extract("a.pdf", "", _pdf(["Договор"], tounicode=False)) == ""
    assert attachtext.extract("a.pdf", "", b"%PDF-1.4 garbage") == ""


def test_office_and_odf():
    docx = _zip({"word/document.xml": "<w:document><w:body><w:p><w:r><w:t>Договор</w:t></w:r><w:r><w:t xml:space="
                 "\"preserve\"> поставки</w:t></w:r></w:p><w:p><w:r><w:t>Сумма &amp; срок</w:t></w:r></w:p>"
                 "</w:body></w:document>",
                 "word/footer1.xml": "<w:ftr><w:p><w:r><w:t>Конфиденциально</w:t></w:r></w:p></w:ftr>"})
    assert attachtext.extract("d.docx", "", docx) == "Договор поставки\nСумма & срок\nКонфиденциально"
    xlsx = _zip({"xl/sharedStrings.xml": "<sst><si><t>Цемент</t></si><si><t>Арматура</t></si></sst>",
                 "xl/worksheets/sheet1.xml": "<worksheet><sheetData><row><c r=\"A1\" t=\"s\"><v>0</v></c>"
                 "<c r=\"B1\"><v>4500</v></c><c r=\"C1\" t=\"inlineStr\"><is><t>Склад</t></is></c></row>"
                 "</sheetData></worksheet>"})
    text = attachtext.extract("t.xlsx", "", xlsx)
    assert "Цемент" in text and "Арматура" in text and "4500" in text and "Склад" in text and " 0 " not in text
    pptx = _zip({"ppt/slides/slide2.xml": "<p:sld><a:p><a:r><a:t>Второй</a:t></a:r></a:p></p:sld>",
                 "ppt/slides/slide10.xml": "<p:sld><a:p><a:r><a:t>Десятый</a:t></a:r></a:p></p:sld>",
                 "ppt/slides/slide1.xml": "<p:sld><a:p><a:r><a:t>Первый</a:t></a:r></a:p></p:sld>"})
    assert attachtext.extract("p.pptx", "", pptx) == "Первый\nВторой\nДесятый"
    odt = _zip({"content.xml": "<office:document-content><text:p>Служебная записка</text:p></office:document-content>"})
    assert attachtext.extract("z.odt", "", odt) == "Служебная записка"
    assert attachtext.extract("broken.docx", "", b"not a zip") == ""


def test_text_rtf_html_eml():
    assert attachtext.extract("a.csv", "", "Наименование;Цена".encode("cp1251")) == "Наименование;Цена"
    assert attachtext.extract("a.html", "", "<p>Привет</p><script>x()</script>".encode()) == "Привет"
    rtf = rb"{\rtf1\ansi\ansicpg1251{\fonttbl{\f0 Arial;}}\f0 \'d1\'f7\'e5\'f2 \par}"
    assert attachtext.extract("a.rtf", "", rtf) == "Счет"
    inner = EmailMessage()
    inner["Subject"] = "Пересланное"
    inner["From"] = "boss@x.ru"
    inner.set_content("Текст внутри")
    text = attachtext.extract("fwd.eml", "message/rfc822", inner.as_bytes())
    assert "Пересланное" in text and "Текст внутри" in text
    assert attachtext.kind_of("photo.jpg", "image/jpeg") == ""
    assert attachtext.kind_of("noname", "application/pdf") == ".pdf"


def test_index_finds_attachment_text(services):
    svc = services
    acc = svc.db.create_account(models.Account(name="A", host="h", username="a@x", password="p"))
    m = EmailMessage()
    m["From"] = "x@y.ru"
    m["Subject"] = "Документы"
    m.set_content("см. вложение")
    m.add_attachment(_pdf(["Поставка", "арматуры"]), maintype="application", subtype="pdf", filename="Счёт.pdf")
    raw = m.as_bytes()
    rel, sha, size = svc.store.store_message(acc, "INBOX", "/", 1, raw)
    svc.db.add_message_index_batch([(acc, "INBOX", 1, 1, "", size, "2026-01-01T00:00:00+00:00", "", rel, sha,
                                     "Документы", "x@y.ru", 1)])
    search.index_pending(svc)
    assert [r["id"] for r in search.search(svc, "арматуры")["results"]]
    assert search.search(svc, "вложение:поставка")["results"]
    assert search.status(svc)["attachments"] is True
    # выключено — текст вложений не индексируется (после перестройки индекса)
    svc.set_rt("search", "index_attachments", False)
    search.reset_index(svc.db)
    search.index_pending(svc)
    assert not search.search(svc, "арматуры")["results"]
    assert search.search(svc, "вложение:счет")["results"]          # имя файла по-прежнему в индексе
