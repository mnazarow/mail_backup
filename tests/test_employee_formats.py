"""Тесты форматов выгрузок: XML-таблица (SpreadsheetML) и файл паролей.

Формат XML-таблицы — это реальная выгрузка корпоративного интранета: ФИО там
разложено по колонкам, у части колонок нет заголовка, внутри ячеек попадается
HTML, а сам файл невалиден как XML (префикс ss: без объявления пространства
имён). Всё это разбирается здесь.
"""
import io

import pytest
from openpyxl import Workbook

from mailarchiver.employees import (
    apply_passwords, looks_like_spreadsheetml, parse_employee_file, parse_password_file,
    sync_employees,
)
from mailarchiver.errors import ValidationError
from mailarchiver.models import Account

SPREADSHEET_ML = """    <?mso-application progid="Excel.Sheet"?>
<Workbook>
<Worksheet ss:Name="Vodokomfort employees">
<Table>
<Row>
<Cell></Cell>
<Cell><Data ss:Type="String">работает до</Data></Cell>
<Cell><Data ss:Type="String">фамилия</Data></Cell>
<Cell><Data ss:Type="String">имя</Data></Cell>
<Cell></Cell>
<Cell><Data ss:Type="String">должность</Data></Cell>
<Cell><Data ss:Type="String">отдел</Data></Cell>
<Cell><Data ss:Type="String">тел. рабочий</Data></Cell>
<Cell><Data ss:Type="String">тел. внутренний</Data></Cell>
<Cell><Data ss:Type="String">тел. мобильный</Data></Cell>
<Cell><Data ss:Type="String">электропочта</Data></Cell>
<Cell><Data ss:Type="String">поставщики</Data></Cell>
</Row>
<Row>
<Cell><Data ss:Type="String">да</Data></Cell>
<Cell><Data ss:Type="String"/></Cell>
<Cell><Data ss:Type="String">Абдрахманова</Data></Cell>
<Cell><Data ss:Type="String">Лилия</Data></Cell>
<Cell><Data ss:Type="String">Рифатовна</Data></Cell>
<Cell><Data ss:Type="String">Менеджер проектных продаж</Data></Cell>
<Cell><Data ss:Type="String">Отдел проектных продаж <span>(ЧЛБ)</span></Data></Cell>
<Cell><Data ss:Type="String"/></Cell>
<Cell><Data ss:Type="String">457</Data></Cell>
<Cell><Data ss:Type="String">+7-951-772-66-95</Data></Cell>
<Cell><Data ss:Type="String">L.Abdrakhmanova@Vodokomfort.RU</Data></Cell>
<Cell><Data ss:Type="String"/></Cell>
</Row>
<Row>
<Cell><Data ss:Type="String">нет</Data></Cell>
<Cell><Data ss:Type="String">2025-05-30</Data></Cell>
<Cell><Data ss:Type="String">Уволенный</Data></Cell>
<Cell><Data ss:Type="String">Иван</Data></Cell>
<Cell><Data ss:Type="String">Иванович</Data></Cell>
<Cell><Data ss:Type="String">Слесарь</Data></Cell>
<Cell><Data ss:Type="String">Склад</Data></Cell>
<Cell><Data ss:Type="String">+7-495-000-00-00</Data></Cell>
<Cell><Data ss:Type="String">101</Data></Cell>
<Cell><Data ss:Type="String"/></Cell>
<Cell><Data ss:Type="String">bye@vodokomfort.ru</Data></Cell>
<Cell><Data ss:Type="String"/></Cell>
</Row>
</Table>
</Worksheet>
</Workbook>"""


# ---------------------------------------------------------------------------
#  XML-таблица (SpreadsheetML 2003)
# ---------------------------------------------------------------------------
def test_spreadsheetml_is_detected_by_content():
    data = SPREADSHEET_ML.encode("utf-8")
    assert looks_like_spreadsheetml(data) is True
    # имя файла может быть любым — формат определяется по содержимому
    for name in ("vygruzka.xls", "employees.csv", "export", "list.xml"):
        rows, problems = parse_employee_file(data, name)
        assert len(rows) == 2, name
        assert problems == []


def test_spreadsheetml_splits_name_columns_and_strips_html():
    rows, _ = parse_employee_file(SPREADSHEET_ML.encode("utf-8"), "export.xml")
    first = rows[0]
    assert first["full_name"] == "Абдрахманова Лилия Рифатовна"   # фамилия + имя + отчество
    assert first["email"] == "l.abdrakhmanova@vodokomfort.ru"     # приведён к нижнему регистру
    assert first["department"] == "Отдел проектных продаж (ЧЛБ)"  # разметка из ячейки убрана
    assert first["position"] == "Менеджер проектных продаж"
    assert first["phone"] == "+7-951-772-66-95"                   # мобильный важнее внутреннего
    assert first["inactive"] is False


def test_spreadsheetml_marks_dismissed_employees():
    """Колонка «да/нет» без заголовка — это признак «работает»."""
    rows, _ = parse_employee_file(SPREADSHEET_ML.encode("utf-8"), "export.xml")
    assert rows[1]["full_name"] == "Уволенный Иван Иванович"
    assert rows[1]["inactive"] is True
    assert rows[1]["phone"] == "+7-495-000-00-00"    # мобильного нет — берём рабочий


def test_sync_skips_dismissed_and_reports_them(services):
    rows, _ = parse_employee_file(SPREADSHEET_ML.encode("utf-8"), "export.xml")
    result = sync_employees(services, rows, create_accounts=False)
    assert result["created"] == 1 and result["skipped_inactive"] == 1
    assert services.db.count_employees() == 1
    assert services.db.get_employee_by_email("bye@vodokomfort.ru") is None


def test_spreadsheetml_handles_ss_index_gaps():
    """ss:Index «перепрыгивает» пустые ячейки — колонки не должны съезжать."""
    xml = """<?mso-application progid="Excel.Sheet"?>
<Workbook><Worksheet><Table>
<Row><Cell><Data ss:Type="String">ФИО</Data></Cell>
     <Cell ss:Index="3"><Data ss:Type="String">E-mail</Data></Cell></Row>
<Row><Cell><Data ss:Type="String">Иванов Иван</Data></Cell>
     <Cell ss:Index="3"><Data ss:Type="String">ivanov@x.ru</Data></Cell></Row>
</Table></Worksheet></Workbook>"""
    rows, problems = parse_employee_file(xml.encode("utf-8"), "x.xml")
    assert problems == []
    assert rows[0]["full_name"] == "Иванов Иван" and rows[0]["email"] == "ivanov@x.ru"


def test_binary_xls_gives_a_clear_error():
    with pytest.raises(ValidationError) as err:
        parse_employee_file(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64, "old.xls")
    assert "xls" in str(err.value).lower()


# ---------------------------------------------------------------------------
#  Файл паролей
# ---------------------------------------------------------------------------
def _xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_password_file_with_and_without_header():
    with_header = _xlsx([("email", "пароль"), ("ivanov@x.ru", "Secret1!"), ("petrova@x.ru", "Secret2!")])
    rows, problems = parse_password_file(with_header, "p.xlsx")
    assert problems == []
    assert [r["email"] for r in rows] == ["ivanov@x.ru", "petrova@x.ru"]
    assert rows[0]["password"] == "Secret1!"

    bare = _xlsx([("ivanov@x.ru", "Secret1!")])
    rows, problems = parse_password_file(bare, "p.xlsx")
    assert problems == [] and rows[0]["password"] == "Secret1!"


def test_password_file_reports_bad_rows():
    data = _xlsx([("ivanov@x.ru", "Secret1!"), ("petrova@x.ru", ""), ("не адрес", "x"),
                  ("ivanov@x.ru", "Другой")])
    rows, problems = parse_password_file(data, "p.xlsx")
    assert len(rows) == 1
    reasons = " ".join(p["reason"] for p in problems)
    assert "пустой пароль" in reasons
    assert "не похоже на e-mail" in reasons     # само значение не печатаем: там может быть пароль
    assert "повторно" in reasons                # два пароля на один ящик — не берём молча
    assert "не адрес" not in reasons            # исходное значение наружу не уходит


def test_password_file_reads_csv_and_spreadsheetml():
    csv_rows, _ = parse_password_file("email;пароль\nivanov@x.ru;Secret1!\n".encode("utf-8"), "p.csv")
    assert csv_rows[0]["email"] == "ivanov@x.ru"
    xml = ("""<?mso-application progid="Excel.Sheet"?><Workbook><Worksheet><Table>"""
           """<Row><Cell><Data ss:Type="String">ivanov@x.ru</Data></Cell>"""
           """<Cell><Data ss:Type="String">Secret1!</Data></Cell></Row>"""
           """</Table></Worksheet></Workbook>""")
    xml_rows, _ = parse_password_file(xml.encode("utf-8"), "p.xml")
    assert xml_rows[0]["password"] == "Secret1!"


def test_apply_passwords_sets_and_enables_accounts(services):
    acc_id = services.db.create_account(Account(name="Иванов", host="imap.x.ru", username="ivanov@x.ru",
                                                password="", enabled=False))
    rows = [{"row": 1, "email": "ivanov@x.ru", "password": "Secret1!"},
            {"row": 2, "email": "nobody@x.ru", "password": "Secret2!"}]
    result = apply_passwords(services, rows, enable=True)

    assert result["updated"] == 1 and result["enabled"] == 1
    assert result["not_found"] == ["nobody@x.ru"]
    acc = services.db.get_account(acc_id)
    assert acc.password == "Secret1!" and acc.enabled is True


def test_apply_passwords_finds_account_through_employee(services):
    """Логин ящика может не совпадать с адресом — ищем через карточку сотрудника."""
    acc_id = services.db.create_account(Account(name="Петрова", host="imap.x.ru",
                                                username="petrova", password="", enabled=False))
    emp_id = services.db.create_employee(full_name="Петрова Анна", email="petrova@x.ru")
    services.db.set_employee_account(emp_id, acc_id)

    result = apply_passwords(services, [{"row": 1, "email": "petrova@x.ru", "password": "Pw!"}])
    assert result["updated"] == 1 and result["not_found"] == []
    assert services.db.get_account(acc_id).password == "Pw!"
    # без enable ящик остаётся выключенным — включение это отдельное решение
    assert services.db.get_account(acc_id).enabled is False


def test_sync_reports_shared_addresses(services):
    """Один адрес у нескольких строк (общий ящик) — об этом сообщаем."""
    xml = SPREADSHEET_ML.replace("bye@vodokomfort.ru", "l.abdrakhmanova@vodokomfort.ru")
    xml = xml.replace("<Data ss:Type=\"String\">нет</Data>", "<Data ss:Type=\"String\">да</Data>")
    rows, _ = parse_employee_file(xml.encode("utf-8"), "x.xml")
    result = sync_employees(services, rows, create_accounts=False)
    assert result["duplicate_rows"] == 1
    assert result["duplicate_emails"] == ["l.abdrakhmanova@vodokomfort.ru"]
    assert services.db.count_employees() == 1      # карточка на адрес одна


def test_password_file_rejects_single_column():
    """Одна колонка — раньше паролем становился сам адрес и затирал настоящий."""
    with pytest.raises(ValidationError) as err:
        parse_password_file(_xlsx([("ivanov@x.ru",), ("petrova@x.ru",)]), "p.xlsx")
    assert "одна колонка" in str(err.value)


def test_password_file_warns_about_dropped_header():
    """Первая строка, принятая за заголовок, не пропадает молча."""
    rows, problems = parse_password_file(_xlsx([("ivanov", "Pass1"), ("petrova@x.ru", "Pass2")]),
                                         "p.xlsx")
    assert [r["email"] for r in rows] == ["petrova@x.ru"]
    assert any("как заголовок" in p["reason"] for p in problems)


def test_password_equal_to_email_is_rejected():
    rows, problems = parse_password_file(
        _xlsx([("email", "пароль"), ("ivanov@x.ru", "ivanov@x.ru")]), "p.xlsx")
    assert rows == []
    assert any("совпадает с адресом" in p["reason"] for p in problems)


def test_spreadsheetml_self_closing_cell_does_not_shift_columns():
    """«<Cell ss:StyleID="s1"/>» раньше съедала следующую ячейку — колонки ехали."""
    xml = ('<?mso-application progid="Excel.Sheet"?><Workbook><Worksheet><Table>'
           '<Row><Cell><Data ss:Type="String">ФИО</Data></Cell>'
           '<Cell><Data ss:Type="String">Должность</Data></Cell>'
           '<Cell><Data ss:Type="String">E-mail</Data></Cell></Row>'
           '<Row><Cell><Data ss:Type="String">Иванов Иван</Data></Cell>'
           '<Cell ss:StyleID="s1"/>'
           '<Cell><Data ss:Type="String">ivanov@x.ru</Data></Cell></Row>'
           '</Table></Worksheet></Workbook>')
    rows, problems = parse_employee_file(xml.encode("utf-8"), "x.xml")
    assert problems == []
    assert rows[0]["full_name"] == "Иванов Иван"
    assert rows[0]["position"] == ""          # пустая ячейка осталась пустой
    assert rows[0]["email"] == "ivanov@x.ru"  # адрес не уехал в «должность»


def test_spreadsheetml_reads_windows_1251():
    """Выгрузки 1С объявляют windows-1251 — читаем по объявленной кодировке."""
    xml = ('<?xml version="1.0" encoding="windows-1251"?>'
           '<?mso-application progid="Excel.Sheet"?><Workbook><Worksheet><Table>'
           '<Row><Cell><Data>ФИО</Data></Cell><Cell><Data>E-mail</Data></Cell></Row>'
           '<Row><Cell><Data>Иванов Иван</Data></Cell><Cell><Data>i@x.ru</Data></Cell></Row>'
           '</Table></Worksheet></Workbook>')
    rows, _ = parse_employee_file(xml.encode("cp1251"), "x.xml")
    assert rows[0]["full_name"] == "Иванов Иван"


def test_dash_column_is_not_a_dismissal_flag():
    """Колонка-заглушка из прочерков не должна объявлять уволенными всех."""
    csv_data = "ФИО;E-mail;\nИванов Иван;ivanov@x.ru;-\nПетров Пётр;petrov@x.ru;-\n"
    rows, _ = parse_employee_file(csv_data.encode("utf-8"), "x.csv")
    assert [r["inactive"] for r in rows] == [False, False]


def test_url_is_redacted_in_logs_and_answers():
    from mailarchiver.employees import redact_url
    assert redact_url("https://hr/export?token=SECRET&id=5") == "https://hr/export?token=***&id=5"
    assert redact_url("https://user:pass@hr/x.csv") == "https://***@hr/x.csv"
    assert redact_url("http://hr/plain.csv") == "http://hr/plain.csv"
