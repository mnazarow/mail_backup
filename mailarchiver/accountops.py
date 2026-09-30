"""
Общие операции над почтовыми ящиками — для REST API одного ящика и для
групповых действий (:mod:`mailarchiver.bulk`).

Раньше эти проверки жили прямо в обработчиках веб-запросов, и групповое
действие пришлось бы писать второй раз — с риском, что две копии правил
разойдутся (например, восстановление в рабочие папки ящика проверялось бы в
одном месте и молча пропускалось в другом).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .errors import ValidationError
from .models import Account, account_has_credentials


def longer_retention(old_days: int, new_days: int) -> bool:
    """Новый срок хранения длиннее прежнего (0 — «хранить всё» — длиннее любого)."""
    if old_days <= 0:
        return False
    return new_days <= 0 or new_days > old_days


def after_account_retention_change(svc, before: Account) -> None:
    """Срок хранения ящика стал длиннее — письма, вычищенные по старому сроку и
    ещё лежащие на сервере, снова должны скачаться при следующем копировании."""
    from .queue.jobs import effective_retention_days
    old_effective = effective_retention_days(svc, before)
    after = svc.db.get_account(before.id)
    if after is None:
        return
    if longer_retention(old_effective, effective_retention_days(svc, after)):
        svc.db.clear_retired(before.id)


def after_hold_set(svc, account_id: int) -> None:
    """Архив ящика взят на удержание: сроки хранения больше не действуют.

    Письма, которые копирование не скачивало (или очистка удалила) по сроку
    хранения и которые ещё лежат на сервере, должны скачаться при следующем
    копировании — ради этого удержание и ставят (увольнение, проверка).
    """
    svc.db.clear_retired(int(account_id))


def validate_cron(expr: str) -> None:
    """Проверить cron-выражение (5 полей и понятный APScheduler синтаксис)."""
    expr = (expr or "").strip()
    hint = "Пример: «0 3 * * *» — каждый день в 03:00."
    if len(expr.split()) != 5:
        raise ValidationError(f"Некорректное cron-выражение: «{expr}» (нужно 5 полей).", hint=hint)
    try:
        from .cronutil import crontab_trigger
        crontab_trigger(expr)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"Некорректное cron-выражение: «{expr}» ({exc}).", hint=hint) from exc


def validate_restore_options(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Проверить и нормализовать параметры восстановления.

    Общая для ручного запуска, расписаний и групповых действий: заливка в
    ИСХОДНЫЕ папки живого ящика должна быть только осознанным выбором, а пустой
    префикс — не молчаливым синонимом этого выбора.
    """
    out = dict(data or {})
    mode = str(out.get("target_mode") or "prefixed").strip().lower()
    if mode not in ("original", "single", "prefixed"):
        raise ValidationError(f"Неизвестный режим восстановления «{out.get('target_mode')}».",
                              hint="Допустимо: original, single, prefixed.")
    out["target_mode"] = mode
    out["target_prefix"] = str(out.get("target_prefix") or "").strip()
    out["target_folder"] = str(out.get("target_folder") or "").strip()
    if mode == "prefixed" and not out["target_prefix"]:
        raise ValidationError("Укажите префикс папок — иначе письма попадут прямо в рабочие папки ящика.",
                              hint="Например «Восстановлено». Для заливки в исходные папки выберите режим «в исходные папки».")
    if mode == "single" and not out["target_folder"]:
        raise ValidationError("Укажите папку назначения.", hint="Например «Восстановлено».")
    try:
        limit = int(out.get("limit") or 0)
    except (TypeError, ValueError):
        raise ValidationError("Ограничение числа писем должно быть целым числом.")
    if limit < 0:
        raise ValidationError("Ограничение числа писем не может быть отрицательным.")
    out["limit"] = limit
    return out


def hold_label(until: str) -> str:
    """«до 31.12.2027» / «бессрочно» / «нет» — срок удержания архива так же, как его показывает интерфейс."""
    from datetime import datetime
    from .employees import HOLD_FOREVER
    if not until:
        return "нет"
    if until == HOLD_FOREVER:
        return "бессрочно"
    try:
        return "до " + datetime.strptime(until[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return f"до {until}"


def credential_problem(acc: Account) -> str:
    """Почему к ящику нельзя подключиться (пусто — можно).

    Те же правила, что у задания копирования (queue.jobs._require_credentials),
    но без исключения: групповому действию нужна причина пропуска, а не ошибка.
    """
    if acc.secret_broken:
        return "пароль не расшифровывается текущим ключом — введите его в карточке ящика заново"
    if not account_has_credentials(acc):
        return "не задан пароль" if acc.auth_type != "oauth2" else "не задан refresh-токен OAuth2"
    if not (acc.host or "").strip():
        return "не указан IMAP-сервер"
    return ""
