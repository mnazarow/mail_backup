"""
Роли пользователей и то, что разрешено оператору.

* **Администратор** — всё.
* **Оператор** следит за копированием и запускает его: копия ящика, «Докачать
  потерянные», проверки входа, папок и целостности, отмена и повтор этих
  заданий, журнал службы. Писем не читает, настроек не меняет, ничего не
  удаляет и не выгружает.
* **Вход по ящику** — сотрудник, вошедший паролем своего ящика: только свой ящик.

Разрешения оператора заданы белыми списками: новое действие или новая точка API
оператору по умолчанию НЕ доступны.
"""
from __future__ import annotations

import json
from typing import Any, Dict

from .models import JobType

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_MAILBOX = "mailbox"
#: Роли учётных записей архива (вход по имени и паролю, а не по ящику).
STAFF_ROLES = (ROLE_ADMIN, ROLE_OPERATOR)
ROLE_LABELS = {ROLE_ADMIN: "администратор", ROLE_OPERATOR: "оператор", ROLE_MAILBOX: "вход по ящику"}

#: Задания, которые оператор видит целиком (итоги, параметры, ход), отменяет и
#: повторяет. Остальные (выгрузки, восстановление, анализ писем, сравнение
#: прежних копий…) он видит в списке, но без подробностей: в их итогах бывают
#: темы и отправители писем.
OPERATOR_JOB_TYPES = frozenset({
    JobType.BACKUP, JobType.BACKUP_ALL, JobType.CHECK_LOGINS, JobType.FOLDERS_CHECK,
    JobType.VERIFY, JobType.TEST,
})

#: Групповые действия, доступные оператору.
OPERATOR_BULK_ACTIONS = frozenset({
    "backup", "backup_sequence", "rebuild_missing", "verify", "check_logins", "folders_check", "cancel_jobs",
})


def is_operator(user: Any) -> bool:
    return bool(user) and user.get("role") == ROLE_OPERATOR


def job_params(row) -> Dict[str, Any]:
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
    return params if isinstance(params, dict) else {}


def operator_sees_job(row) -> bool:
    """Видит ли оператор подробности задания (итог, параметры, события)."""
    return row is not None and row["type"] in OPERATOR_JOB_TYPES


def operator_may_manage(row) -> bool:
    """Может ли оператор отменить или повторить задание.

    Только копирование и проверки — и не «Скопировать с нуля» (стирает
    локальную копию) и не «последняя копия» уволенного сотрудника или по
    групповому действию (после неё копирование ящика выключается).
    """
    if not operator_sees_job(row):
        return False
    if row["type"] == JobType.BACKUP:
        params = job_params(row)
        if str(params.get("rebuild") or "").strip().lower() == "full":
            return False
        if params.get("final") or params.get("disable_after"):
            return False
    return True
