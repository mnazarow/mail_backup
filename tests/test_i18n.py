"""Языки интерфейса (1.24.0): каталог английского перевода и его применение."""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import i18n_check  # noqa: E402


def test_catalog_covers_login_and_mail():
    """Каждая русская строка входа, оболочки, «Почты» и её сообщений сервера — в каталоге."""
    assert i18n_check.missing() == []


def test_catalog_placeholders_match():
    cat = i18n_check.catalog()
    assert len(cat) > 1000
    bad = {k: v for k, v in cat.items() if v is not None and k.count("{}") != v.count("{}")}
    assert not bad
    # кириллица в переводе — только буква русской раскладки в справке по клавишам
    assert [v for v in cat.values() if v and i18n_check.CYR.search(v)] == [
        "Keys also work in the Russian layout (for example, “у” — archive)."]


def test_bundle_order_and_setting(client):
    from mailarchiver.web.assets import JS_FILES
    assert JS_FILES[:3] == ("i18n-en.js", "i18n.js", "app.js")
    r = client.get("/api/needs-setup").json()
    assert r["ui_language"] == "ru"
    from mailarchiver.config import VALUE_CHOICES
    assert VALUE_CHOICES["server.ui_language"] == ("ru", "en", "auto")


_NODE = r"""
const vm = require('vm'), fs = require('fs');
const store = {};
const ctx = {
  localStorage: {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }},
  navigator: {language: 'de-DE'}, location: {reload(){}},
  document: {cookie: '', documentElement: {setAttribute(){}}, addEventListener(){}, createTreeWalker(){ return {nextNode(){ return null; }}; }},
  NodeFilter: {SHOW_ELEMENT: 1, SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2},
};
vm.createContext(ctx);
for (const f of process.argv.slice(2)) vm.runInContext(fs.readFileSync(f, 'utf8'), ctx, {filename: f});
vm.runInContext(`
  const out = {};
  out.ru = t('Входящие');
  i18nInit('auto');
  out.lang = uiLang(); out.locale = UI_LOCALE;
  out.exact = t('Входящие');
  out.args = t('Писем: {}', 5);
  out.pattern = i18nLookup('Писем: 7 — в архиве они остались.');
  out.nested = i18nLookup('Все входящие: 3 письма в 2 ящиках (из архива — новые письма появятся после проверки почты в каждом ящике).');
  out.server = i18nLookup('Папки «Проекты» на сервере нет.');
  out.unknown = i18nLookup('Совсем новая строка');
  out.plural = plural(1, 'письмо', 'письма', 'писем') + '/' + plural(5, 'письмо', 'письма', 'писем');
  globalThis.__out = JSON.stringify(out);
`, ctx);
console.log(ctx.__out);
"""


@pytest.mark.skipif(not shutil.which("node"), reason="нет node")
def test_runtime_translation(tmp_path):
    js = os.path.join(ROOT, "mailarchiver", "web", "static", "js")
    script = tmp_path / "run.js"
    script.write_text(_NODE, encoding="utf-8")
    plural_src = tmp_path / "plural.js"
    admin = open(os.path.join(js, "admin.js"), encoding="utf-8").read()
    start = admin.index("function plural(")
    plural_src.write_text(admin[start:admin.index("\n}\n", start) + 3], encoding="utf-8")
    res = subprocess.run(["node", str(script), os.path.join(js, "i18n-en.js"), os.path.join(js, "i18n.js"),
                          str(plural_src)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["ru"] == "Входящие"                       # до выбора языка — русский
    assert out["lang"] == "en" and out["locale"] == "en-GB"   # auto + браузер не на русском
    assert out["exact"] == "Inbox"
    assert out["args"] == "Messages: 5"
    assert out["pattern"] == "Messages: 7 — they remain in the archive."
    assert out["nested"].startswith("All inboxes: 3 messages in 2 mailboxes")
    assert out["server"] == "The folder “Проекты” is not on the server."
    assert out["unknown"] is None
    assert out["plural"] == "письмо/писем"
