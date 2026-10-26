"""Состав руководства Word (scripts/manual/chapters.py) совпадает с docs/ru.

Само руководство собирается командой bash scripts/manual/build.sh (нужны LibreOffice и история git), а этот
тест ловит раньше то, на чём сборка остановилась бы: новый файл docs/ru не вписан в chapters.py, номера глав
с пропуском, номер раздела в файле («## 21.3.») не совпадает с MD_NUM, ширины колонок не в сумму таблицы."""
import importlib.util
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs", "ru")


def _chapters():
    spec = importlib.util.spec_from_file_location("manual_chapters", os.path.join(ROOT, "scripts", "manual",
                                                                                  "chapters.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_doc_is_a_chapter_or_excluded():
    ch = _chapters()
    files = sorted(f for f in os.listdir(DOCS) if f.endswith(".md"))
    for f in files:
        assert f in ch.CHAPTERS or f in ch.NOT_IN_MANUAL or f == ch.APPENDIX_FILE, \
            f"docs/ru/{f} нет в scripts/manual/chapters.py — впишите главу (или в NOT_IN_MANUAL)"
    for f in ch.CHAPTERS:
        assert f in files, f"в chapters.py есть {f}, а в docs/ru такого файла нет"


def test_chapter_numbers_and_section_numbers():
    ch = _chapters()
    numbers = sorted(n for n, _t in ch.CHAPTERS.values())
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    for f, n in ch.MD_NUM.items():
        text = open(os.path.join(DOCS, f), encoding="utf-8").read()
        assert text.startswith(f"# {n}. "), f"{f}: первая строка не «# {n}. …»"
        fence = False
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                fence = not fence
            m = None if fence else re.match(r"^## (\d+)\.\d+\. ", line)
            if m:
                assert int(m.group(1)) == n, f"{f}: раздел «{line}» — номер не {n}"


def test_settings_refer_to_known_files():
    ch = _chapters()
    assert ch.CONTINUE_LISTS <= set(ch.CHAPTERS)
    assert set(ch.REFS.values()) <= set(ch.CHAPTERS)
    for head, widths in ch.TABLE_WIDTHS.items():
        assert len(head) == len(widths) and sum(widths) == ch.TABLE_WIDTH, head
