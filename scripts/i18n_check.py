#!/usr/bin/env python3
"""
Перевод интерфейса (1.24.0): какие русские строки раздела ещё без английского перевода.

    python3 scripts/i18n_check.py            # недостающие строки каталога (код выхода 1, если есть)
    python3 scripts/i18n_check.py --json     # то же списком JSON — заготовка для перевода

Строки берутся из исходников так же, как их видит пользователь: текст между тегами разметки и
подсказки (placeholder, title, aria-label, alt) из строк и шаблонов JS, а также сообщения об ошибках и
подсказки из модулей сервера, которые показывает «Почта». Подстановка ``${…}`` / ``{…}`` — «{}».
Каталог — mailarchiver/web/static/js/i18n-en.js (ключ — русская строка, значение — английская).
"""
from __future__ import annotations

import ast
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "mailarchiver", "web", "static", "js")
#: Что переведено: вход и оболочка, «Почта», сообщения сервера для «Почты».
JS_FILES = ("app.js", "mail.js", "mail-settings.js")
PY_FILES = ("mailarchiver/webmail.py", "mailarchiver/webmail_dav.py", "mailarchiver/webmail_more.py",
            "mailarchiver/webmail_secure.py", "mailarchiver/webmail_tools.py", "mailarchiver/web/webmail_api.py",
            "mailarchiver/web/auth.py", "mailarchiver/web/api_auth.py", "mailarchiver/web/api_common.py",
            "mailarchiver/web/api_state.py")
#: Части строк, которые не переводятся: буквы раскладки в справке по клавишам.
IGNORE = {"о", "л", "к", "ф", "а", "г", "м", "д", "у"}
CYR = re.compile(r"[А-Яа-яЁё]")


def literals(src):
    """Генератор (kind, text) для строковых литералов, включая вложенные в ${}."""
    i, n = 0, len(src)
    out = []
    def read_template(i):
        # src[i] == '`'
        i += 1
        parts, buf = [], []
        while i < n:
            c = src[i]
            if c == '\\':
                buf.append(src[i:i+2]); i += 2; continue
            if c == '`':
                parts.append(''.join(buf)); return i + 1, parts
            if c == '$' and i + 1 < n and src[i+1] == '{':
                parts.append(''.join(buf)); buf = []
                i, inner = read_expr(i + 2)
                parts.append(None)   # placeholder
                continue
            buf.append(c); i += 1
        return i, parts
    def read_expr(i):
        depth = 1
        start = i
        while i < n:
            c = src[i]
            if c in "'\"":
                i = read_simple(i)
                continue
            if c == '`':
                i, parts = read_template(i)
                out.append(('tpl', parts))
                continue
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: return i + 1, src[start:i]
            i += 1
        return i, src[start:i]
    def read_simple(i):
        q = src[i]; j = i + 1; buf = []
        while j < n and src[j] != q:
            if src[j] == '\\': buf.append(src[j:j+2]); j += 2; continue
            if src[j] == '\n': break
            buf.append(src[j]); j += 1
        out.append(('str', ''.join(buf)))
        return j + 1
    while i < n:
        c = src[i]
        if src.startswith('//', i):
            j = src.find('\n', i); i = n if j < 0 else j; continue
        if src.startswith('/*', i):
            j = src.find('*/', i); i = n if j < 0 else j + 2; continue
        if c in "'\"":
            i = read_simple(i); continue
        if c == '`':
            i, parts = read_template(i); out.append(('tpl', parts)); continue
        # регулярные выражения: /…/ после ( , = : [ ! & | ? ; {
        if c == '/' and i > 0:
            k = i - 1
            while k >= 0 and src[k] in ' \t': k -= 1
            if k >= 0 and src[k] in '(,=:[!&|?;{}+' :
                j = i + 1
                while j < n and src[j] != '/' and src[j] != '\n':
                    if src[j] == '\\': j += 1
                    elif src[j] == '[':
                        while j < n and src[j] != ']': j += 1
                    j += 1
                i = j + 1; continue
        i += 1
    return out

def unescape_js(s):
    return s.replace("\\'", "'").replace('\\"', '"').replace('\\`', '`').replace('\\n', '\n').replace('\\\\', '\\')

ATTR = re.compile(r'\b(?:placeholder|title|aria-label|alt|data-tip|label)\s*=\s*"([^"]*)"')
TAG = re.compile(r"<[^<>]*>")

def pieces(text_parts):
    """Части литерала (None — подстановка) → ключи текстовых узлов и атрибутов."""
    joined = ''.join('\x00' if p is None else p for p in text_parts)
    keys = []
    for m in ATTR.finditer(joined):
        keys.append(m.group(1))
    # атрибуты удаляем вместе с тегами; текст между тегами — узлы
    for chunk in TAG.split(joined):
        keys.append(chunk)
    out = []
    for k in keys:
        k = html.unescape(unescape_js(k)).replace('\xa0', ' ')
        k = re.sub(r"\s+", " ", k).strip()
        if not CYR.search(k):
            continue
        # подстановки → {}
        k = re.sub(r"\x00+", "{}", k)
        out.append(k)
    return out


def js_keys(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    keys = []
    for kind, val in literals(src):
        for k in pieces([val] if kind == "str" else val):
            if k not in keys:
                keys.append(k)
    return keys


def _text_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "\x00" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text_of(node.left), _text_of(node.right)
        if a is not None or b is not None:
            return (a if a is not None else "\x00") + (b if b is not None else "\x00")
    return None


def py_keys(path: str) -> list:
    """Сообщения ошибок и подсказки: первый аргумент и hint=/detail=/message= вызовов …Error(…)."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    keys = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None) or ""
        if not (name.endswith("Error") or name in ("HTTPException", "_err", "fail", "_fail")):
            continue
        for c in list(node.args[:1]) + [k.value for k in node.keywords if k.arg in ("hint", "detail", "message")]:
            t = _text_of(c)
            if t and CYR.search(t):
                t = re.sub(r"\x00+", "{}", re.sub(r"\s+", " ", t)).strip()
                if t not in keys:
                    keys.append(t)
    return keys


def catalog() -> dict:
    with open(os.path.join(JS_DIR, "i18n-en.js"), encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("const I18N_EN = ") + len("const I18N_EN = ")
    return json.loads(text[start:text.rindex(";")])


def all_keys() -> list:
    keys = []
    for name in JS_FILES:
        keys += [k for k in js_keys(os.path.join(JS_DIR, name)) if k not in keys]
    for rel in PY_FILES:
        keys += [k for k in py_keys(os.path.join(ROOT, rel)) if k not in keys]
    return [k for k in keys if k not in IGNORE]


def missing() -> list:
    cat = catalog()
    return [k for k in all_keys() if k not in cat]


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    miss = missing()
    if "--json" in argv:
        json.dump({k: "" for k in miss}, sys.stdout, ensure_ascii=False, indent=1)
        print()
    else:
        for k in miss:
            print(k)
        print(f"Без перевода: {len(miss)}", file=sys.stderr)
    return 1 if miss else 0


if __name__ == "__main__":
    sys.exit(main())
