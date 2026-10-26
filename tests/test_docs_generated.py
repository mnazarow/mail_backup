"""Сгенерированная документация не отстала от кода.

Справочник параметров (docs/ru/05-parameters-reference.md), разделы 21.9–21.10 главы о почтовых серверах (из
описаний драйверов) и встроенная справка (web/static/docs/index.html) строятся командой
``python scripts/gen_docs.py``. Если код поменяли, а её не запустили, документация говорит неправду — этот тест
ловит такое до выпуска."""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs_for_tests", os.path.join(ROOT, "scripts", "gen_docs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_docs_are_up_to_date():
    stale = _gen_docs().stale()
    assert stale == [], f"устарели {stale} — запустите python scripts/gen_docs.py"


def test_mailserver_matrix_matches_drivers():
    gen = _gen_docs()
    text = open(os.path.join(ROOT, "docs", "ru", "21-mailservers.md"), encoding="utf-8").read()
    for cls in gen.ms_drivers():
        assert f"| **{cls.label}** |" in text and f"### {cls.label}" in text, cls.label
