#!/usr/bin/env python3
"""
Генератор документации MailArchiver.

Собирает:
  * docs/ru/05-parameters-reference.md — справочник по каждому параметру
    (из mailarchiver.web.i18n, поэтому всегда совпадает с реальностью);
  * разделы 21.9 «Что умеет каждый сервер» и 21.10 «Настройка на стороне
    сервера» в docs/ru/21-mailservers.md — из описаний драйверов почтовых
    серверов (возможности, summary, setup, limits, поля); остальной текст
    главы пишется вручную и не трогается;
  * web/static/docs/index.html — единая оформленная документация со
    скриншотами, схемами и справочником (её открывает кнопка «Документация»
    в интерфейсе).

Запуск:  python scripts/gen_docs.py            — записать
         python scripts/gen_docs.py --check    — только проверить, что всё
                                                  уже записано (код выхода 1 — нет)
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mailarchiver.web.i18n import (  # noqa: E402
    PARAM_HELP, SETTINGS_SECTIONS, ACCOUNT_HELP,
)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------
#  1) Markdown-справочник параметров
# --------------------------------------------------------------------------
def params_md():
    lines = ["# Справочник параметров MailArchiver", "",
             "Полное описание каждого параметра: назначение, рекомендация по настройке, "
             "пример значения и значение по умолчанию. Параметры сгруппированы так же, как "
             "на странице «Настройки» в веб-интерфейсе.", ""]
    for sec in SETTINGS_SECTIONS:
        lines.append(f"## {sec.get('icon','')} {sec['title']}")
        lines.append("")
        for key in sec["keys"]:
            full = f"{sec['section']}.{key}"
            hp = PARAM_HELP.get(full)
            if not hp:
                continue
            lines.append(f"### {hp['title']}  \n`{full}`")
            lines.append("")
            lines.append(hp.get("help", ""))
            lines.append("")
            if hp.get("recommend"):
                lines.append(f"- **Рекомендация:** {hp['recommend']}")
            if hp.get("example"):
                lines.append(f"- **Пример:** `{hp['example']}`")
            if hp.get("default"):
                lines.append(f"- **По умолчанию:** `{hp['default']}`")
            lines.append("")
    # поля ящика
    lines.append("## 📬 Поля почтового ящика")
    lines.append("")
    for key, hp in ACCOUNT_HELP.items():
        lines.append(f"### {hp['title']} (`{key}`)")
        lines.append("")
        lines.append(hp.get("help", ""))
        lines.append("")
        if hp.get("recommend"):
            lines.append(f"- **Рекомендация:** {hp['recommend']}")
        if hp.get("example"):
            lines.append(f"- **Пример:** `{hp['example']}`")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
#  2) HTML-фрагмент справочника параметров (для index.html)
# --------------------------------------------------------------------------
def params_html():
    parts = []
    for sec in SETTINGS_SECTIONS:
        parts.append(f'<h3 class="pg">{esc(sec.get("icon",""))} {esc(sec["title"])}</h3>')
        parts.append('<div class="ptable">')
        for key in sec["keys"]:
            full = f'{sec["section"]}.{key}'
            hp = PARAM_HELP.get(full)
            if not hp:
                continue
            parts.append(f'''<div class="prow">
  <div class="pname"><span class="ptitle">{esc(hp["title"])}</span><code>{esc(full)}</code></div>
  <div class="pdesc"><p>{esc(hp.get("help",""))}</p>
    {f'<p class="prec">💡 {esc(hp["recommend"])}</p>' if hp.get("recommend") else ''}
    <p class="pmeta">{f'Пример: <code>{esc(hp["example"])}</code>' if hp.get("example") else ''}{f' · По умолчанию: <code>{esc(hp["default"])}</code>' if hp.get("default") else ''}</p>
  </div></div>''')
        parts.append('</div>')
    # поля ящика
    parts.append('<h3 class="pg">📬 Поля почтового ящика</h3><div class="ptable">')
    for key, hp in ACCOUNT_HELP.items():
        parts.append(f'''<div class="prow"><div class="pname"><span class="ptitle">{esc(hp["title"])}</span><code>{esc(key)}</code></div>
  <div class="pdesc"><p>{esc(hp.get("help",""))}</p>
    {f'<p class="prec">💡 {esc(hp["recommend"])}</p>' if hp.get("recommend") else ''}
    <p class="pmeta">{f'Пример: <code>{esc(hp["example"])}</code>' if hp.get("example") else ''}</p></div></div>''')
    parts.append('</div>')
    return "\n".join(parts)


# --------------------------------------------------------------------------
#  3) Почтовые серверы: что умеет каждый (таблица) и настройка на стороне сервера
# --------------------------------------------------------------------------
def _ms():
    from mailarchiver.mailservers.base import Cap
    return Cap


def ms_columns():
    """Колонки таблицы «что умеет каждый сервер» — одни для главы 21 и встроенной справки."""
    Cap = _ms()
    return [("Вход", [Cap.BLOCK]), ("Пароли", [Cap.PASSWORD, Cap.MUST_CHANGE]), ("Квота", [Cap.QUOTA]),
            ("Псевдонимы", [Cap.ALIASES]), ("ФИО, отдел", [Cap.CONTACT]),
            ("Создание, удаление", [Cap.CREATE, Cap.DELETE]), ("Автоответ, пересылка", [Cap.AUTOREPLY, Cap.FORWARD]),
            ("Место, вход", [Cap.USAGE, Cap.LAST_LOGIN]), ("Без паролей", [Cap.IMAP_ADMIN])]


def ms_drivers():
    from mailarchiver.errors import ValidationError
    from mailarchiver.mailservers.service import TYPE_ORDER, driver_class
    out = []
    for t in TYPE_ORDER:
        try:
            out.append(driver_class(t))
        except ValidationError as exc:
            raise SystemExit(f"нет драйвера {t}: {exc.message}")
    return out


def ms_cell(cls, caps):
    Cap = _ms()
    sub = {Cap.MUST_CHANGE: "смена при входе", Cap.PASSWORD: "пароль", Cap.CREATE: "создание",
           Cap.DELETE: "удаление", Cap.AUTOREPLY: "автоответ", Cap.FORWARD: "пересылка", Cap.USAGE: "место",
           Cap.LAST_LOGIN: "вход"}
    have = [c for c in caps if cls.can(c)]
    if not have:
        return "—"
    return "✓" if len(have) == len(caps) else "только " + ", ".join(sub.get(c, c) for c in have)


def mailservers_html():
    """Таблица «что умеет каждый почтовый сервер» — из описаний драйверов (всегда совпадает с кодом)."""
    cols = ms_columns()
    rows = [f"<tr><td><b>{esc(cls.label)}</b></td>" + "".join(f"<td>{esc(ms_cell(cls, caps))}</td>"
                                                             for _n, caps in cols) + "</tr>"
            for cls in ms_drivers()]
    head = "<tr><th>Сервер</th>" + "".join(f"<th>{esc(n)}</th>" for n, _c in cols) + "</tr>"
    return f'<div class="tw"><table class="t">{head}{"".join(rows)}</table></div>'


def _ms_matrix(classes):
    cols = ms_columns()
    lines = ["| Сервер | " + " | ".join(c[0] for c in cols) + " |", "|---|" + "|".join("---" for _ in cols) + "|"]
    lines += [f"| **{cls.label}** | " + " | ".join(ms_cell(cls, caps) for _n, caps in cols) + " |" for cls in classes]
    lines += ["", "✓ — умеет; «—» — API сервера этого не позволяет (или сервер этого не сообщает). Сверка учётных "
                  "записей и слежение есть у всех. Чего ещё не позволяет каждый сервер — ниже, в "
                  "[21.10](#2110-настройка-на-стороне-сервера)."]
    return "\n".join(lines)


def _ms_setup(classes):
    parts = []
    for cls in classes:
        parts += [f"### {cls.label}", "", cls.summary, ""]
        if cls.setup:
            parts += ["**Что настроить:**", ""] + [f"{n}. {s}" for n, s in enumerate(cls.setup, 1)] + [""]
        req = [f.label for f in cls.fields if f.required]
        if req:
            parts += ["**Поля подключения:** " + ", ".join(f"«{r}»" for r in req) + ".", ""]
        parts += [f"**Пароль:** {cls.password_rule}.", ""]
        if cls.limits:
            parts += ["**Ограничения:**", ""] + [f"- {s}" for s in cls.limits] + [""]
    return "\n".join(parts).rstrip()


def mailservers_md(current: str) -> str:
    """Глава 21 с разделами 21.9 и 21.10, построенными из драйверов (остальной текст — как был)."""
    classes = ms_drivers()
    if not re.search(r"^## 21\.9\. ", current, flags=re.M) or not re.search(r"^## 21\.10\. ", current, flags=re.M):
        raise SystemExit("в 21-mailservers.md нет разделов «## 21.9.» и «## 21.10.»")
    out = re.sub(r"(## 21\.9\. [^\n]*\n\n).*?(?=\n## 21\.10)", lambda m: m.group(1) + _ms_matrix(classes) + "\n",
                 current, flags=re.S)
    return re.sub(r"(## 21\.10\. [^\n]*\n\n).*", lambda m: m.group(1) + _ms_setup(classes) + "\n", out, flags=re.S)


def index_html():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from doc_template import TEMPLATE  # локальный модуль рядом
    # Номер версии в HTML не пишем: страница документации доступна без входа,
    # а точная версия подсказывает, какие уязвимости искать (как и /health).
    return (TEMPLATE.replace("%%VERSION%%", "").replace("%%PARAMS%%", params_html())
            .replace("%%MAILSERVERS%%", mailservers_html()))


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def outputs():
    """Что должно лежать в файлах: {путь: содержимое}."""
    ms_path = os.path.join(ROOT, "docs", "ru", "21-mailservers.md")
    return {
        os.path.join(ROOT, "docs", "ru", "05-parameters-reference.md"): params_md(),
        ms_path: mailservers_md(_read(ms_path) or ""),
        os.path.join(ROOT, "mailarchiver", "web", "static", "docs", "index.html"): index_html(),
    }


def stale():
    """Файлы, которые не совпадают с тем, что построил бы генератор (пусто — всё свежее)."""
    return [os.path.relpath(p, ROOT) for p, data in outputs().items() if _read(p) != data]


def write_all():
    out = []
    for path, data in outputs().items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if _read(path) != data:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
            out.append(path)
    return out


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        old = stale()
        for p in old:
            print("устарел:", p, "— запустите python scripts/gen_docs.py")
        sys.exit(1 if old else 0)
    written = write_all()
    for p in written:
        print("написан:", p)
    if not written:
        print("всё и так свежее")
