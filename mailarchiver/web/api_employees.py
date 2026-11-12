"""API: сотрудники, их ящики, пароли и источники списка.

Часть REST API веб-интерфейса (до 1.22.0 — один файл api.py; маршруты собирает api.py).
"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field

from ..employees import (
    ACCOUNT_PLACEHOLDERS, TEMPLATE_CSV, TEMPLATE_FILENAME, ensure_account_for_employee, fetch_employee_source,
    looks_like_email, normalize_email, parse_employee_file, preview_account_template, redact_url, sync_employees,
)
from ..errors import ValidationError
from ..models import JobType
from ..util import human_size, safe_filename
from . import auth as auth_mod

from .api_common import (
    EMPLOYEE_STATUSES, EmployeeBody, EmployeeCreateBody, MAX_EMPLOYEE_UPLOAD_BYTES, _bounded_id, log, svc_dep,
)

router = APIRouter(prefix="/api")

# =====================================================================
#  Сотрудники (только администратор)
#
#  ВНИМАНИЕ: маршруты с постоянными путями (/template.csv, /import, /sync)
#  объявлены ДО /employees/{employee_id} — иначе FastAPI сопоставит их с
#  маршрутом по идентификатору и попытается разобрать «template.csv» как int.
# =====================================================================
def _employee_account_out(row) -> dict:
    """Ящик в карточке сотрудника."""
    return {"id": row["id"], "name": row["name"] or "", "username": row["username"] or "",
            "enabled": bool(row["enabled"]), "hold_until": row["hold_until"] or "",
            "login_status": row["login_status"] or "", "last_backup_at": row["last_backup_at"] or ""}


def _employee_out(row, accounts=None) -> dict:
    """Карточка сотрудника для интерфейса. ``account_*`` — основной ящик (совместимость с прежним
    интерфейсом), ``accounts`` — все ящики сотрудника (1.15.0)."""
    account_enabled = row["account_enabled"]
    return {
        "id": row["id"],
        "external_id": row["external_id"] or "",
        "full_name": row["full_name"] or "",
        "email": row["email"] or "",
        "position": row["position"] or "",
        "department": row["department"] or "",
        "phone": row["phone"] or "",
        "status": row["status"] or "active",
        "account_id": row["account_id"],
        "account_name": row["account_name"] or "" if row["account_id"] else "",
        "account_enabled": bool(account_enabled) if account_enabled is not None else False,
        "notes": row["notes"] or "",
        "source": row["source"] or "manual",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_seen_at": row["last_seen_at"],
        "dismissed_at": _row_get(row, "dismissed_at") or "",
        "account_hold_until": (_row_get(row, "account_hold_until") or "") if row["account_id"] else "",
        "account_count": int(_row_get(row, "account_count") or 0),
        "accounts": [_employee_account_out(a) for a in (accounts or [])],
    }


def _row_get(row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _employee_values(body: "EmployeeBody") -> dict:
    """Нормализованные поля карточки из тела запроса."""
    status = (body.status or "active").strip().lower()
    if status not in EMPLOYEE_STATUSES:
        raise ValidationError(f"Неизвестный статус сотрудника: «{body.status}».",
                              hint="Допустимы «active» (работает) и «archived» (в архиве).")
    full_name = (body.full_name or "").strip()
    if not full_name:
        raise ValidationError("Укажите ФИО сотрудника.")
    email = normalize_email(body.email)
    if email and not looks_like_email(email):
        raise ValidationError(f"Некорректный e-mail: «{body.email}».",
                              hint="Адрес должен быть вида ivanov@example.ru.")
    return {
        "full_name": full_name,
        "email": email,
        "position": (body.position or "").strip(),
        "department": (body.department or "").strip(),
        "phone": (body.phone or "").strip(),
        "external_id": (body.external_id or "").strip(),
        "status": status,
        "notes": (body.notes or "").strip(),
    }


@router.get("/employees")
def list_employees(request: Request, query: Optional[str] = None, status: Optional[str] = None,
                   limit: int = 100, offset: int = 0, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    rows = svc.db.list_employees(query=query, status=status, limit=limit, offset=offset)
    boxes = svc.db.employee_accounts_map([int(r["id"]) for r in rows])
    return {
        "employees": [_employee_out(r, boxes.get(int(r["id"]), [])) for r in rows],
        # total — сколько строк подходит под фильтр (для постраничной навигации),
        # counts — общая сводка по всему справочнику (для плашек в шапке).
        "total": svc.db.count_employees(status=status, query=query),
        "counts": svc.db.employee_counts(),
    }


@router.get("/employees/template.csv")
def employees_template(user: dict = Depends(auth_mod.require_admin)):
    """Файл-образец: заголовки и одна строка для примера."""
    # BOM — чтобы Excel открыл файл в UTF-8, а не показал кракозябры.
    data = "﻿".encode("utf-8") + TEMPLATE_CSV.encode("utf-8")
    return Response(
        content=data, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{TEMPLATE_FILENAME}"'},
    )


@router.post("/employees/import")
def import_employees(request: Request, file: UploadFile = File(...),
                     user: dict = Depends(auth_mod.require_admin)):
    """Загрузить файл CSV/XLSX и сразу применить его к справочнику."""
    svc = svc_dep(request)
    # имя файла приходит от клиента: берём только базовое имя и чистим его,
    # иначе «../../» в имени увело бы запись за пределы каталога временных файлов
    origin_name = os.path.basename(file.filename or "")
    fname = safe_filename(origin_name, default="employees.csv")
    dest = os.path.join(svc.cfg.tmp_dir, f"employees_{uuid.uuid4().hex}_{fname}")
    written = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_EMPLOYEE_UPLOAD_BYTES:
                    raise ValidationError(
                        f"Файл слишком большой: допустимо не более {human_size(MAX_EMPLOYEE_UPLOAD_BYTES)}.",
                        hint="Выгрузите сотрудников по частям или уберите лишние листы и колонки.")
                fh.write(chunk)
        if not written:
            raise ValidationError("Файл пуст.", hint="Выберите выгрузку из кадровой системы (CSV или XLSX).")
        rows, problems = parse_employee_file(dest, origin_name or fname)
        # wait=False: если идёт синхронизация по расписанию — сразу понятный
        # отказ, а не зависший на минуты запрос
        result = sync_employees(svc, rows, create_accounts=bool(svc.rt("employees", "create_accounts")),
                                wait=False)
    finally:
        # временный файл не нужен ни при успехе, ни при ошибке
        try:
            os.unlink(dest)
        except OSError:
            pass
    problems = problems + list(result["problems"])
    svc.db.add_audit(user["username"], "employees_import",
                     f"файл={origin_name or fname} создано={result['created']} обновлено={result['updated']}")
    return {"ok": True, "created": result["created"], "updated": result["updated"],
            "accounts_created": result["accounts_created"], "accounts_linked": result["accounts_linked"],
            "total_rows": result["total_rows"],
            # Список режем: на кривом файле в 20 МБ это сотни тысяч записей —
            # и в памяти, и в JSON-ответе.
            "problems": problems[:200], "problem_count": len(problems),
            "skipped_inactive": result.get("skipped_inactive", 0),
            "duplicate_rows": result.get("duplicate_rows", 0),
            "duplicate_emails": result.get("duplicate_emails", [])[:50],
            "warnings": result.get("warnings", [])}


def _employee_source(svc) -> dict:
    """Текущий источник списка сотрудников в виде, пригодном для интерфейса.

    Пароль наружу не отдаём — только признак того, что он задан.
    """
    stype = str(svc.rt("employees", "source_type") or "file").strip().lower()
    if stype not in ("file", "url"):
        stype = "file"
    path = str(svc.rt("employees", "source_file") or "").strip()
    url = str(svc.rt("employees", "source_url") or "").strip()
    # Адрес отдаём БЕЗ секретов: выгрузку часто открывают ссылкой с токеном,
    # и этот токен не должен оседать в интерфейсе, аудите и журнале.
    safe_url = redact_url(url)
    return {
        "type": stype,
        "path": path,
        "url": safe_url,
        "target": safe_url if stype == "url" else path,
        "configured": bool(url if stype == "url" else path),
        "auth": bool(str(svc.rt("employees", "source_url_user") or "").strip()),
        "verify_ssl": bool(svc.rt("employees", "source_url_verify_ssl")),
        "format": str(svc.rt("employees", "source_url_format") or "auto"),
        "timeout_s": int(svc.rt("employees", "source_url_timeout_s") or 60),
        "sync_enabled": bool(svc.rt("employees", "sync_enabled")),
        "cron": str(svc.rt("employees", "cron") or ""),
    }


def _check_employee_source(svc) -> dict:
    """Проверить источник: он доступен и читается? Справочник не меняем."""
    src = _employee_source(svc)
    if src["type"] == "url":
        if not src["url"]:
            raise ValidationError(
                "Не задан адрес выгрузки сотрудников.",
                hint="Заполните «Адрес выгрузки (URL)» в настройках, раздел «Сотрудники», "
                     "либо переключите источник на «Файл на сервере».")
        # Скачиваем по НАСТОЯЩЕМУ адресу: в src["url"] он уже с вырезанными
        # секретами (token=***), и проверка ходила не туда — сервер отвечал 403,
        # хотя сама синхронизация работала.
        data, name = fetch_employee_source(
            str(svc.rt("employees", "source_url") or "").strip(),
            username=str(svc.rt("employees", "source_url_user") or ""),
            password=str(svc.rt("employees", "source_url_password") or ""),
            verify_ssl=src["verify_ssl"], timeout_s=src["timeout_s"], fmt=src["format"])
        rows, problems = parse_employee_file(data, name)
        return {"ok": True, "source": src, "filename": name, "bytes": len(data),
                "rows": len(rows), "problems": problems[:50], "problem_count": len(problems)}

    if not src["path"]:
        raise ValidationError(
            "Не задан файл со списком сотрудников.",
            hint="Укажите путь к файлу CSV/XLSX в настройках, раздел «Сотрудники» → «Файл-источник». "
                 "Либо загрузите файл кнопкой «Импорт из файла».")
    if not os.path.isfile(src["path"]):
        raise ValidationError(
            f"Файл со списком сотрудников не найден: {src['path']}",
            hint="Проверьте путь и права доступа: файл читает служба mailarchiver.")
    rows, problems = parse_employee_file(src["path"], os.path.basename(src["path"]))
    return {"ok": True, "source": src, "filename": os.path.basename(src["path"]),
            "bytes": os.path.getsize(src["path"]), "rows": len(rows),
            "problems": problems[:50], "problem_count": len(problems)}


@router.get("/employees/source")
def employee_source(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Какой источник списка настроен — для подсказок в разделе «Сотрудники»."""
    return {"source": _employee_source(svc_dep(request))}


@router.post("/employees/source/check")
def employee_source_check(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Проверить источник, ничего не записывая в справочник.

    Нужна, чтобы администратор убедился в правильности адреса или пути до
    того, как включит синхронизацию по расписанию: иначе первая же ошибка
    всплыла бы ночью, в журнале планировщика.
    """
    result = _check_employee_source(svc_dep(request))
    svc_dep(request).db.add_audit(user["username"], "employees_source_check",
                                  f"{result['source']['type']}: {result['source']['target']}"[:500])
    return result


@router.get("/employees/account-template")
def employee_account_template(request: Request, full_name: str = "Иванов Иван Иванович",
                              email: str = "ivanov@example.ru", position: str = "Менеджер",
                              department: str = "Отдел продаж",
                              user: dict = Depends(auth_mod.require_admin)):
    """Показать, какой ящик получится по шаблону из настроек.

    Нужен, чтобы не проверять шаблон «вживую»: ошибку в подстановке иначе
    видно только после синхронизации, когда ящики уже созданы.
    """
    svc = svc_dep(request)
    row = {"position": position, "department": department, "external_id": "1234"}
    return {"preview": preview_account_template(svc, full_name=full_name, email=email, row=row),
            "placeholders": list(ACCOUNT_PLACEHOLDERS),
            "create_accounts": bool(svc.rt("employees", "create_accounts"))}


@router.post("/employees/sync")
def sync_employees_now(request: Request, user: dict = Depends(auth_mod.require_admin)):
    """Поставить в очередь синхронизацию с источником, указанным в настройках.

    Источник — файл на сервере или адрес выгрузки; что именно, решает настройка
    «Источник списка». Наличие источника проверяем здесь же, чтобы ошибка
    показалась сразу в интерфейсе, а не только в журнале задания.
    """
    svc = svc_dep(request)
    src = _employee_source(svc)
    if src["type"] == "url":
        if not src["url"]:
            raise ValidationError(
                "Не задан адрес выгрузки сотрудников.",
                hint="Заполните «Адрес выгрузки (URL)» в настройках, раздел «Сотрудники», "
                     "либо переключите источник на «Файл на сервере».")
    else:
        if not src["path"]:
            raise ValidationError(
                "Не задан файл со списком сотрудников.",
                hint="Укажите путь к файлу CSV/XLSX в настройках, раздел «Сотрудники» → «Файл-источник». "
                     "Либо загрузите файл кнопкой «Импорт из файла».")
        if not os.path.isfile(src["path"]):
            raise ValidationError(
                f"Файл со списком сотрудников не найден: {src['path']}",
                hint="Проверьте путь и права доступа: файл читает служба mailarchiver.")
    # Синхронизация уже идёт или ждёт очереди — вторая копия не нужна (очередь
    # всё равно не запустит их одновременно, но двойной клик ставил две).
    busy = [j for j in svc.db.active_jobs() if j["type"] == JobType.SYNC_EMPLOYEES]
    if busy:
        return {"ok": True, "job_id": busy[0]["id"], "already": True, "source": src}
    jid = svc.queue.enqueue(JobType.SYNC_EMPLOYEES, None, {}, created_by=user["username"])
    svc.db.add_audit(user["username"], "employees_sync_start", f"{src['type']}: {src['target']}"[:500])
    return {"ok": True, "job_id": jid, "source": src}


@router.post("/employees")
def create_employee(request: Request, body: EmployeeCreateBody, user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    values = _employee_values(body)
    if body.create_account and not values["email"]:
        # проверяем ДО создания карточки: иначе карточка появлялась, а запрос
        # всё равно завершался ошибкой
        raise ValidationError("Чтобы завести ящик, укажите e-mail сотрудника.")
    employee_id = svc.db.create_employee(source="manual", **values)
    account_id = None
    warning = ""
    if body.create_account:
        # ящик создаётся по тому же шаблону, что и при синхронизации
        from ..employees import account_template
        tpl = account_template(svc)
        account_id, action = ensure_account_for_employee(
            svc, employee_id, values["full_name"], values["email"], row=values, tpl=tpl,
            create=bool(str(tpl.get("host") or "").strip()))
        if action == "no_host":
            warning = ("Карточка создана, но ящик не заведён: в шаблоне ящиков не указан IMAP-сервер "
                       "(кнопка «Шаблон ящиков» в разделе «Сотрудники»).")
        elif action == "owned":
            other = svc.db.employee_of_account(account_id)
            warning = (f"Карточка создана, но ящик не привязан: ящик с этим адресом уже принадлежит сотруднику "
                       f"«{other['full_name'] if other is not None else '?'}». Перенести его можно в карточке "
                       f"(«Привязать существующий»).")
            account_id = None
        elif action == "created" and tpl.get("schedule_enabled"):
            try:
                svc.scheduler.reload()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось перечитать расписания")
    svc.db.add_audit(user["username"], "employee_create", values["full_name"])
    return {"ok": True, "id": employee_id, "account_id": account_id, "warning": warning}


@router.put("/employees/{employee_id}")
def update_employee(request: Request, employee_id: int, body: EmployeeBody,
                    user: dict = Depends(auth_mod.require_admin)):
    svc = svc_dep(request)
    before = svc.db.get_employee(employee_id)
    if before is None:
        raise HTTPException(404, "Сотрудник не найден")
    values = _employee_values(body)
    status = values.pop("status")
    svc.db.update_employee(employee_id, **values)
    svc.db.add_audit(user["username"], "employee_update", values["full_name"])
    result: Dict[str, Any] = {"ok": True}
    # Увольнение и возврат — со всеми последствиями для ящика (удержание архива,
    # последняя копия и выключение копирования).
    from ..employees import dismiss_employee, rehire_employee
    if status == "archived" and before["status"] != "archived":
        result["dismissed"] = dismiss_employee(svc, employee_id, by=user["username"], reason="вручную")
    elif status == "active" and before["status"] == "archived":
        result["rehired"] = rehire_employee(svc, employee_id, by=user["username"])
    return result


@router.delete("/employees/{employee_id}")
def delete_employee(request: Request, employee_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Удалить карточку сотрудника. Почтовый ящик и локальные копии писем
    остаются на месте — их удаляют отдельно в разделе «Ящики»."""
    svc = svc_dep(request)
    row = svc.db.get_employee(employee_id)
    if row is None:
        raise HTTPException(404, "Сотрудник не найден")
    svc.db.delete_employee(employee_id)
    svc.db.add_audit(user["username"], "employee_delete", row["full_name"] or str(employee_id))
    return {"ok": True}


@router.get("/employees/{employee_id}")
def get_employee(request: Request, employee_id: int, user: dict = Depends(auth_mod.require_admin)):
    """Карточка сотрудника со всеми его ящиками."""
    svc = svc_dep(request)
    row = svc.db.get_employee(_bounded_id(employee_id))
    if row is None:
        raise HTTPException(404, "Сотрудник не найден")
    return _employee_out(row, svc.db.employee_accounts(employee_id))


def _link_account_checked(svc, user: dict, employee_id: int, account_id: int, move: bool) -> dict:
    """Записать ящик за сотрудником (общая часть для «привязать» и «завести»)."""
    emp = svc.db.get_employee(employee_id)
    if emp is None:
        raise HTTPException(404, "Сотрудник не найден")
    acc = svc.db.get_account(_bounded_id(int(account_id)))
    if acc is None:
        raise HTTPException(404, "Ящик не найден")
    if acc.employee_id is not None and int(acc.employee_id) != int(employee_id):
        other = svc.db.get_employee(int(acc.employee_id))
        other_name = other["full_name"] if other is not None else f"#{acc.employee_id}"
        if not move:
            raise ValidationError(f"Ящик «{acc.name}» уже принадлежит сотруднику «{other_name}».",
                                  hint="Ящик принадлежит одному сотруднику. Чтобы передать его, подтвердите "
                                       "перенос — у прежнего владельца он пропадёт из списка его ящиков.")
        svc.db.add_audit(user["username"], "employee_account_moved",
                         f"ящик «{acc.name}»: «{other_name}» → «{emp['full_name']}»"[:900])
    svc.db.link_employee_account(employee_id, acc.id)
    svc.db.add_audit(user["username"], "employee_link_account",
                     f"«{emp['full_name']}» ← ящик «{acc.name}» ({acc.username})"[:900])
    return {"ok": True, "account_id": acc.id}


@router.post("/employees/{employee_id}/link-account")
def link_employee_account(request: Request, employee_id: int, account_id: int = 0, move: bool = False,
                          user: dict = Depends(auth_mod.require_admin)):
    """Добавить сотруднику существующий ящик. У сотрудника может быть несколько ящиков (1.15.0).

    ``account_id=0`` — отвязать ВСЕ его ящики (как раньше «отвязать ящик»); отвязать один —
    ``DELETE /employees/{id}/accounts/{account_id}``. Ящик, уже принадлежащий другому сотруднику,
    переносится только с ``move=true``.
    """
    svc = svc_dep(request)
    if not account_id:
        row = svc.db.get_employee(_bounded_id(employee_id))
        if row is None:
            raise HTTPException(404, "Сотрудник не найден")
        # Отвязка НЕ удаляет ящики: они просто перестают числиться за сотрудником.
        svc.db.set_employee_account(employee_id, None)
        svc.db.add_audit(user["username"], "employee_link_account", f"«{row['full_name']}»: ящики отвязаны")
        return {"ok": True}
    return _link_account_checked(svc, user, _bounded_id(employee_id), int(account_id), move)


@router.delete("/employees/{employee_id}/accounts/{account_id}")
def unlink_employee_account(request: Request, employee_id: int, account_id: int,
                            user: dict = Depends(auth_mod.require_admin)):
    """Отвязать от сотрудника один ящик. Ящик и его архив остаются; синхронизация его не заводит снова."""
    svc = svc_dep(request)
    emp = svc.db.get_employee(_bounded_id(employee_id))
    acc = svc.db.get_account(_bounded_id(account_id))
    if emp is None or acc is None or acc.employee_id != employee_id:
        raise HTTPException(404, "У сотрудника нет такого ящика")
    svc.db.unlink_employee_account(account_id)
    svc.db.add_audit(user["username"], "employee_unlink_account",
                     f"«{emp['full_name']}» — ящик «{acc.name}» ({acc.username})"[:900])
    return {"ok": True}


class EmployeeAccountBody(BaseModel):
    #: адрес нового (дополнительного) ящика сотрудника
    email: str = Field("", max_length=320)
    #: ящик с таким логином уже есть у другого сотрудника — перенести его
    move: bool = False


@router.post("/employees/{employee_id}/accounts")
def add_employee_account(request: Request, employee_id: int, body: EmployeeAccountBody,
                         user: dict = Depends(auth_mod.require_admin)):
    """Завести сотруднику ещё один ящик — по адресу и шаблону ящиков (как при синхронизации).

    Ящик с таким логином уже есть — он привязывается (чужой — только с ``move``). Новый ящик создаётся,
    как задано в шаблоне: обычно выключенным и без пароля.
    """
    svc = svc_dep(request)
    emp = svc.db.get_employee(_bounded_id(employee_id))
    if emp is None:
        raise HTTPException(404, "Сотрудник не найден")
    email = normalize_email(body.email)
    if not looks_like_email(email):
        raise ValidationError(f"Некорректный адрес: «{body.email}».", hint="Адрес должен быть вида ivanov@example.ru.")
    other = svc.db.get_employee_by_email(email)
    if other is not None and int(other["id"]) != int(employee_id):
        raise ValidationError(f"{email} — основной адрес сотрудника «{other['full_name']}».",
                              hint="Его ящик заводится в его карточке. Уже заведённый ящик можно перенести "
                                   "кнопкой «Привязать существующий».")
    from ..employees import account_login_for, account_template
    tpl = account_template(svc)
    main = normalize_email(emp["email"] or "")
    main = main if main and main != email else ""
    login = account_login_for(tpl, emp["full_name"] or "", email, dict(emp), main)
    existing = svc.db.get_account_by_username(login) or svc.db.get_account_by_username(email)
    if existing is not None:
        if existing.employee_id is not None and int(existing.employee_id) == int(employee_id):
            return {"ok": True, "account_id": existing.id, "action": "owned_already"}
        out = _link_account_checked(svc, user, employee_id, existing.id, body.move)
        out["action"] = "linked"
        return out
    if not str(tpl.get("host") or "").strip():
        raise ValidationError("Ящик не заведён: в шаблоне ящиков не указан IMAP-сервер.",
                              hint="Кнопка «Шаблон ящиков» в разделе «Сотрудники» или Настройки → Сотрудники.")
    account_id, action = ensure_account_for_employee(svc, employee_id, emp["full_name"] or email, email,
                                                     row=dict(emp), tpl=tpl, create=True, main_email=main)
    if action == "created" and tpl.get("schedule_enabled"):
        try:
            svc.scheduler.reload()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось перечитать расписания")
    svc.db.add_audit(user["username"], "employee_add_account", f"«{emp['full_name']}» ← {email}"[:900])
    return {"ok": True, "account_id": account_id, "action": action}
