"""
Действия раздела «Ящики на сервере», которые есть только у подключённых почтовых серверов:
автоответ («нет на месте») и пересылка писем.

У Axigen их через CLI не настроить — для учётных записей Axigen и ящиков без учётной
записи на сервере действия пропускаются с причиной. Выполняет их
:mod:`.server_accounts` (через драйвер подключения).
"""
from __future__ import annotations

import re
from datetime import date

from ..bulk import Param
from ..errors import ValidationError
from ..mailservers.base import check_email
from . import base
from .base import ServerAction

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AutoreplyAction(ServerAction):
    key = "srv_autoreply"
    label = "Автоответ"
    group = "accounts"
    icon = "🏖️"
    needs = "server"
    axigen_change = True
    desc = ("Включить или выключить автоответ («нет на месте») — например, уволенному сотруднику: «Я больше не "
            "работаю в компании, пишите petrova@company.ru». Можно задать период. Только для учётных записей "
            "подключённых почтовых серверов, которые умеют это через API (раздел «Почтовые серверы» показывает, "
            "что умеет каждый).")
    note = "Текст автоответа видят все, кто пишет на адрес, — включая внешних отправителей."
    params = (
        Param("mode", "Автоответ", "select", "on", options=(("on", "включить"), ("off", "выключить"))),
        Param("subject", "Тема", "text", "Нет на месте", show_if={"mode": ["on"]}),
        Param("text", "Текст", "textarea", "", show_if={"mode": ["on"]},
              placeholder="Здравствуйте! Я больше не работаю в компании. По всем вопросам пишите petrova@company.ru."),
        Param("start", "С (необязательно)", "date", "", show_if={"mode": ["on"]}),
        Param("end", "По (необязательно)", "date", "", show_if={"mode": ["on"]},
              help="Без дат автоответ работает, пока его не выключат."),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        if ctx.params.get("mode") == "off":
            return
        if not (ctx.params.get("text") or "").strip():
            raise ValidationError("Напишите текст автоответа.")
        if len(ctx.params.get("text") or "") > 8000:
            raise ValidationError("Текст автоответа — не длиннее 8000 знаков.")
        for key in ("start", "end"):
            value = (ctx.params.get(key) or "").strip()
            if value and not DATE_RE.match(value):
                raise ValidationError("Даты автоответа — в виде ГГГГ-ММ-ДД, например 2026-11-01.")
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise ValidationError(f"Такой даты нет: {value}.") from None
            ctx.params[key] = value
        if ctx.params.get("start") and ctx.params.get("end") and ctx.params["end"] < ctx.params["start"]:
            raise ValidationError("Дата окончания автоответа раньше даты начала.")


class ForwardingAction(ServerAction):
    key = "srv_forwarding"
    label = "Пересылка писем"
    group = "accounts"
    icon = "↪️"
    danger = 1
    needs = "server"
    axigen_change = True
    desc = ("Пересылать письма учётной записи на другие адреса (например, руководителю уволенного сотрудника) — "
            "или выключить пересылку. Можно оставлять копию в ящике (тогда письма продолжат копироваться в архив). "
            "Только для учётных записей подключённых почтовых серверов, которые умеют это через API.")
    note = ("Пересылка на внешние адреса может быть запрещена политикой сервера (Microsoft 365, Google) — тогда "
            "сервер вернёт ошибку. Без копии в ящике письма в архив не попадут.")
    params = (
        Param("mode", "Пересылка", "select", "on", options=(("on", "включить"), ("off", "выключить"))),
        Param("addresses", "Куда пересылать", "textarea", "", show_if={"mode": ["on"]},
              placeholder="petrova@company.ru", help="Один или несколько адресов, по одному в строке."),
        Param("keep_copy", "Оставлять копию в ящике", "bool", True, show_if={"mode": ["on"]}),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        if ctx.params.get("mode") == "off":
            return
        found = [a for a in re.split(r"[\s,;]+", ctx.params.get("addresses") or "") if a.strip()]
        if not found:
            raise ValidationError("Укажите, куда пересылать письма.")
        if len(found) > 10:
            raise ValidationError("Не больше 10 адресов пересылки.")
        ctx.params["addresses"] = "\n".join(check_email(a, "Адрес пересылки") for a in found)
        if not ctx.params.get("keep_copy", True):
            ctx.warnings.append("Копия в ящике не остаётся — новые письма этих учётных записей в архив не попадут.")


base.register(AutoreplyAction(), ForwardingAction())
