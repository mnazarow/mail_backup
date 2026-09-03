#!/usr/bin/env python3
"""
Генератор документации MailArchiver.

Собирает:
  * docs/ru/05-parameters-reference.md — справочник по каждому параметру
    (из mailarchiver.web.i18n, поэтому всегда совпадает с реальностью);
  * web/static/docs/index.html — единая оформленная документация со
    скриншотами, схемами и справочником (её открывает кнопка «Документация»
    в интерфейсе).

Запуск:  python scripts/gen_docs.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mailarchiver.web.i18n import (  # noqa: E402
    PARAM_HELP, SETTINGS_SECTIONS, ACCOUNT_HELP, EXPORT_HELP, RESTORE_HELP, SCHEDULE_HELP,
)
from mailarchiver.version import __version__  # noqa: E402


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --------------------------------------------------------------------------
#  1) Markdown-справочник параметров
# --------------------------------------------------------------------------
def gen_params_md():
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
    out = os.path.join(ROOT, "docs", "ru", "05-parameters-reference.md")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out


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


def gen_html():
    from doc_template import TEMPLATE  # локальный модуль рядом
    html = TEMPLATE.replace("%%VERSION%%", __version__).replace("%%PARAMS%%", params_html())
    out = os.path.join(ROOT, "mailarchiver", "web", "static", "docs", "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    p1 = gen_params_md()
    print("написан:", p1)
    p2 = gen_html()
    print("написан:", p2)
