"""
Описание групповых действий раздела «Ящики на сервере», проверка и запуск.

Действие — класс с теми же шагами, что у групповых действий архива
(:mod:`mailarchiver.bulk`): параметры, ``prepare`` (проверка параметров
целиком), ``check`` (почему ящик пропускается), ``plan`` (что будет сделано).
Само изменение — ``execute``: его выполняет задание «Действия с ящиками на
сервере» по одному ящику, и перед ним ``check`` вызывается ещё раз — по свежим
данным (за время между проверкой и заданием что-то могло поменяться).

Действия, которые ничего не меняют на сервере и выполняются сразу (завести
ящики в архиве), объявляют ``immediate = True`` и делают работу в ``apply``.

Опасные действия (``danger >= 2``) подтверждаются числом ящиков, как в
групповых действиях архива. Операция записывается в историю групповых
действий (``bulk_ops``) с ключом ``srv_<действие>`` — по этому префиксу раздел
показывает только свою историю.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..bulk import FAIL, OK, SKIP, Param, _safe_params
from ..errors import MailArchiverError, ValidationError
from ..logging_setup import get_logger
from ..models import JobStatus, JobType
from . import targets as tg

log = get_logger("serverops")

#: Префикс ключа операции в истории групповых действий.
HISTORY_PREFIX = "srv_"

GROUPS: List[Tuple[str, str]] = [
    ("access", "Вход и пароли (Axigen)"),
    ("accounts", "Учётные записи (Axigen)"),
    ("folders", "Папки и письма (IMAP)"),
    ("cleanup", "Очистка — только при полном архиве"),
    ("reports", "Проверки и отчёты"),
    ("archive", "Архив"),
]


class Ctx:
    """Состояние одной проверки или запуска (и того же действия внутри задания)."""

    def __init__(self, svc, user: str, params: Dict[str, Any], preview: bool, job=None) -> None:
        self.svc = svc
        self.db = svc.db
        self.user = user or "admin"
        self.params = params
        self.preview = preview
        #: контекст задания очереди (в задании; при проверке — None)
        self.job = job
        self.data: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self._totals = None
        self._active = None
        self._keeps: Dict[int, Tuple[bool, str]] = {}

    @property
    def totals(self) -> Dict[int, Dict[str, int]]:
        if self._totals is None:
            self._totals = self.db.message_totals_by_account()
        return self._totals

    def busy(self, account_id: int) -> bool:
        """Идёт ли по ящику архива задание с его копией (кроме самого этого задания)."""
        if self._active is None:
            out: Dict[int, List[Any]] = {}
            for job in self.db.active_jobs():
                if job["account_id"] is not None:
                    out.setdefault(int(job["account_id"]), []).append(job)
            self._active = out
        own = getattr(self.job, "job_id", None)
        return any(j["id"] != own for j in self._active.get(account_id, []))

    def keeps(self, acc) -> Tuple[bool, str]:
        """Хранит ли архив ящика все письма навсегда (условие удаления с сервера)."""
        if acc.id not in self._keeps:
            from ..axigen.jobs import archive_keeps_everything
            self._keeps[acc.id] = archive_keeps_everything(self.svc, acc)
        return self._keeps[acc.id]

    def axigen(self):
        """Настройки Axigen (загружаются один раз)."""
        if "axigen_settings" not in self.data:
            from ..axigen.common import load_settings
            self.data["axigen_settings"] = load_settings(self.svc)
        return self.data["axigen_settings"]


class ServerAction:
    key = ""
    label = ""
    group = "access"
    icon = "•"
    desc = ""
    note = ""
    #: 0 — обычное, 1 — внимательно, 2 — опасное (подтверждается числом ящиков)
    danger = 0
    #: что нужно ящику: "axigen" — учётная запись Axigen; "imap" — вход в ящик по IMAP;
    #: "mailbox" — ящик в архиве
    needs = "axigen"
    #: меняет учётные записи Axigen — нужно «Разрешить изменения на сервере»
    axigen_change = False
    #: выполняется сразу при запуске (без задания): ничего не меняет на сервере
    immediate = False
    params: Tuple[Param, ...] = ()

    # -- описание ------------------------------------------------------------
    def params_for(self, svc) -> Sequence[Param]:
        return self.params

    def available(self, svc) -> str:
        """Почему действие сейчас недоступно (пусто — доступно)."""
        if self.needs == "axigen" or self.axigen_change:
            from ..axigen.common import load_settings
            st = load_settings(svc)
            if not st.enabled:
                return "работа с сервером Axigen выключена (Настройки → Сервер Axigen)"
            if self.axigen_change and not st.allow_changes:
                return "изменения на сервере Axigen не разрешены (Настройки → Сервер Axigen)"
        return ""

    def describe(self, svc) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "group": self.group, "icon": self.icon, "desc": self.desc,
                "note": self.note, "danger": self.danger, "needs": self.needs, "immediate": self.immediate,
                "unavailable": self.available(svc),
                "schedulable": self.key in _schedulable(),
                "params": [p.to_dict() for p in self.params_for(svc)]}

    # -- шаги ----------------------------------------------------------------
    def prepare(self, ctx: Ctx) -> None:
        """Проверка параметров и настроек (ValidationError — операция не начнётся)."""
        problem = self.available(ctx.svc)
        if problem:
            raise ValidationError(problem[0].upper() + problem[1:] + ".")
        if self.needs == "axigen" or self.axigen_change:
            st = ctx.axigen()
            try:
                st.require()
            except Exception as exc:  # noqa: BLE001
                raise ValidationError(getattr(exc, "message", str(exc)), hint=getattr(exc, "hint", None)) from exc

    def base_check(self, ctx: Ctx, t: tg.Target) -> str:
        if self.needs == "axigen":
            if t.axigen is None:
                return "это не учётная запись Axigen (ящик на другом сервере)"
            if t.deleted:
                return "учётная запись удалена на сервере"
        elif self.needs == "imap":
            problem = tg.imap_problem(ctx.svc, t.mailbox)
            if problem:
                return problem
            if t.deleted:
                return "учётная запись удалена на сервере"
        elif self.needs == "mailbox":
            if t.mailbox is None:
                return "ящика нет в архиве"
        return ""

    def check(self, ctx: Ctx, t: tg.Target) -> str:
        return ""

    def validate_plan(self, ctx: Ctx, items: List[tg.Target]) -> None:
        """Проверка всего набора (например, одинаковые псевдонимы у двух ящиков)."""

    def plan(self, ctx: Ctx, t: tg.Target) -> str:
        return "будет выполнено"

    def options(self, ctx: Ctx) -> Dict[str, Any]:
        """Параметры для задания (секреты — зашифрованными)."""
        return dict(ctx.params)

    def apply(self, ctx: Ctx, t: tg.Target) -> str:
        """Сразу выполняемые действия (``immediate``)."""
        raise NotImplementedError

    def execute(self, ctx: Ctx, t: tg.Target) -> Tuple[str, str]:
        """Изменение одного ящика в задании: ("ok"|"skip"|"fail", что сделано)."""
        raise NotImplementedError

    def job_started(self, ctx: Ctx) -> None:
        """Перед первым ящиком в задании (подготовить общее)."""

    def job_finished(self, ctx: Ctx, result: Dict[str, Any]) -> None:
        """После последнего ящика (дописать в итог задания общее)."""


def _schedulable() -> Tuple[str, ...]:
    from .schedules import SCHEDULABLE
    return SCHEDULABLE


REGISTRY: Dict[str, ServerAction] = {}


def register(*actions: ServerAction) -> None:
    for action in actions:
        if action.key in REGISTRY:
            raise RuntimeError(f"действие {action.key} уже есть")
        REGISTRY[action.key] = action


def _load_actions() -> None:
    # модули регистрируют свои действия при импорте (повторный импорт ничего не делает)
    from . import axigen_ops, imap_ops, report  # noqa: F401


def get(key: str) -> ServerAction:
    _load_actions()
    action = REGISTRY.get(key or "")
    if action is None:
        raise ValidationError(f"Неизвестное действие с ящиками на сервере: «{key}».")
    return action


def describe_actions(svc) -> Dict[str, Any]:
    _load_actions()
    actions = [a.describe(svc) for a in REGISTRY.values()]
    used = {a["group"] for a in actions}
    return {"groups": [{"key": k, "label": t} for k, t in GROUPS if k in used], "actions": actions}


def coerce_params(action: ServerAction, svc, raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(raw or {})
    return {p.key: p.coerce(raw.get(p.key)) for p in action.params_for(svc)}


#: Параметры с секретами (общий пароль и т. п.): в историю пишется только «задан». Имена — точно:
#: «random_password» (флажок) и «password_mode» (способ) секретами не являются.
SECRET_PARAMS = ("password", "secret", "token")
SECRET_GIVEN = "задан (не сохраняется)"


def is_secret(key: str) -> bool:
    key = str(key or "").lower()
    return key in SECRET_PARAMS or key.endswith("_enc")


def history_params(values: Dict[str, Any]) -> Dict[str, Any]:
    """Параметры для истории операций — без секретов (история видна всем администраторам)."""
    out = {}
    for key, value in (values or {}).items():
        if is_secret(key):
            value = SECRET_GIVEN if value else ""
        out[key] = value
    return _safe_params(out)


def _item(t: tg.Target, status: str, detail: str) -> Dict[str, Any]:
    return {"id": t.key, "key": t.key, "name": t.name, "username": t.email, "status": status, "detail": detail}


def run(svc, user: dict, action_key: str, keys: Sequence, params: Optional[Dict[str, Any]] = None, *,
        preview: bool = True, confirm: str = "") -> Dict[str, Any]:
    """Проверить (``preview``) или запустить действие над ящиками ``keys``."""
    action = get(action_key)
    keys = tg.clean_keys(keys)
    if not keys:
        raise ValidationError("Не выбрано ни одного ящика.", hint="Отметьте ящики в таблице или выберите их отбором.")
    username = (user or {}).get("username") or "admin"
    values = coerce_params(action, svc, params)
    ctx = Ctx(svc, username, values, preview)
    action.prepare(ctx)
    found = tg.resolve(svc, keys)
    results: List[Dict[str, Any]] = []
    to_apply: List[tg.Target] = []
    for key in keys:
        t = found.get(key)
        if t is None:
            results.append({"id": key, "key": key, "name": key.split(":", 1)[-1], "username": "", "status": FAIL,
                            "detail": "ящик не найден (удалён или не было в сверке)"})
            continue
        try:
            reason = action.base_check(ctx, t) or action.check(ctx, t)
        except MailArchiverError as exc:
            reason = exc.message
        if reason:
            results.append(_item(t, SKIP, reason))
        else:
            to_apply.append(t)
    action.validate_plan(ctx, to_apply)
    if not preview and to_apply and action.danger >= 2 and str(confirm or "").strip() != str(len(to_apply)):
        raise ValidationError(f"Для подтверждения введите число ящиков, которые будут обработаны: {len(to_apply)}.",
                              hint="Число видно в окне проверки перед запуском.")
    op_id = None
    job_id = None
    if not preview:
        op_id = svc.db.create_bulk_op(username, HISTORY_PREFIX + action.key, action.label, history_params(values),
                                      len(keys))
    for t in to_apply:
        if preview:
            try:
                results.append(_item(t, OK, action.plan(ctx, t)))
            except MailArchiverError as exc:
                results.append(_item(t, FAIL, exc.message))
            continue
        if action.immediate:
            try:
                results.append(_item(t, OK, action.apply(ctx, t)))
            except MailArchiverError as exc:
                results.append(_item(t, FAIL, exc.message + (f" {exc.hint}" if exc.hint else "")))
            except Exception as exc:  # noqa: BLE001
                log.exception("Действие %s с ящиком %s не выполнено", action.key, t.key)
                results.append(_item(t, FAIL, f"{type(exc).__name__}: {exc}"))
            continue
        results.append(_item(t, OK, "в задании «Действия с ящиками на сервере»"))
    if not preview and not action.immediate and to_apply:
        job_id = svc.queue.enqueue(JobType.SERVER_ACTION, None,
                                   {"action": action.key, "keys": [t.key for t in to_apply],
                                    "options": action.options(ctx), "requested_by": username, "op_id": op_id},
                                   priority=4, max_attempts=1, created_by=username)
        for item in results:
            if item["status"] == OK:
                item["job_id"] = job_id
    order = {key: i for i, key in enumerate(keys)}
    results.sort(key=lambda item: order.get(item["key"], 0))
    counts = {OK: 0, SKIP: 0, FAIL: 0}
    reasons: Dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        if item["status"] == SKIP:
            reasons[item["detail"]] = reasons.get(item["detail"], 0) + 1
    verb = "будет обработано" if preview else ("обработано" if action.immediate else "в задании")
    summary = f"{action.label}: {verb} {counts[OK]} из {len(keys)}"
    if counts[SKIP]:
        summary += f", пропущено {counts[SKIP]}"
    if counts[FAIL]:
        summary += f", ошибок {counts[FAIL]}"
    summary += "."
    if job_id:
        summary += f" Задание №{job_id}."
    if op_id is not None:
        svc.db.finish_bulk_op(op_id, ok=counts[OK], skipped=counts[SKIP], failed=counts[FAIL],
                              jobs=1 if job_id else 0, summary=summary, results=results)
        names = ", ".join(item["username"] or item["name"] for item in results if item["status"] == OK)
        svc.db.add_audit(username, HISTORY_PREFIX + action.key, f"операция №{op_id}: {summary} {names}"[:900])
    return {"ok": True, "preview": preview, "op_id": op_id, "action": action.key, "label": action.label,
            "danger": action.danger, "confirm_value": str(counts[OK]) if action.danger >= 2 else "",
            "total": len(keys), "counts": counts, "reasons": reasons, "warnings": ctx.warnings,
            "summary": summary, "jobs": [job_id] if job_id else [], "extra": ctx.data.get("extra") or {},
            "results": results}


# ---------------------------------------------------------------------------
#  История
# ---------------------------------------------------------------------------
def history(svc, limit: int = 50) -> List[Dict[str, Any]]:
    out = []
    for row in svc.db.list_bulk_ops(limit=max(1, min(300, int(limit))), prefix=HISTORY_PREFIX):
        try:
            params = json.loads(row["params"] or "{}")
        except (TypeError, ValueError):
            params = {}
        out.append({"id": row["id"], "created_at": row["created_at"], "finished_at": row["finished_at"],
                    "user": row["user"], "action": row["action"][len(HISTORY_PREFIX):], "label": row["label"],
                    "params": params, "total": row["total"], "ok": row["ok"], "skipped": row["skipped"],
                    "failed": row["failed"], "jobs": row["jobs"], "summary": row["summary"]})
    return out


def history_item(svc, op_id: int) -> Optional[Dict[str, Any]]:
    from ..bulk import history_item as bulk_item
    item = bulk_item(svc, op_id, server=True)
    if item is None:
        return None
    item["action"] = item["action"][len(HISTORY_PREFIX):]
    job_ids = sorted({int(r["job_id"]) for r in item["results"] if r.get("job_id")})
    item["job_id"] = job_ids[0] if job_ids else None
    item["job"] = None
    if item["job_id"]:
        row = svc.db.get_job(item["job_id"])
        if row is not None:
            try:
                result = json.loads(row["result"] or "{}") if row["result"] else {}
            except (TypeError, ValueError):
                result = {}
            item["job"] = {"id": row["id"], "status": row["status"], "summary": (result or {}).get("summary", ""),
                           "items": (result or {}).get("items", [])[:5000],
                           "report": bool((result or {}).get("report")),
                           "passwords": _passwords_waiting(svc, op_id)}
    return item


def history_item_full(svc, op_id: int) -> Optional[Dict[str, Any]]:
    """Операция для окна «Подробнее»: с числом ящиков для «Повторить для неудачных»."""
    item = history_item(svc, op_id)
    if item is not None:
        job = item.get("job")
        if job is not None and job.get("status") in JobStatus.ACTIVE:
            item["retry"], item["retry_problem"] = 0, ""
        else:
            item["retry"], item["retry_problem"] = retry_count(svc, op_id)
    return item


def _passwords_waiting(svc, op_id: int) -> int:
    from .secrets import count
    try:
        return count(svc.db, op_id)
    except Exception:  # noqa: BLE001
        return 0


_UNKNOWN = ("Какие ящики задание успело обработать, неизвестно ({why}) — повтор вслепую мог бы ещё раз изменить "
            "уже изменённые (например, выдать новые пароли тем, кому их уже раздали).")
_UNKNOWN_HINT = "Запустите действие заново из раздела: проверка перед запуском покажет, что осталось сделать."


def _retry_params(svc, item: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Параметры для повтора: полные — из задания (в истории длинные тексты укорочены), без секретов.

    → (параметры, какие секреты ввести заново).
    """
    from . import create
    hist = item.get("params") or {}
    if item["action"] == create.KEY:
        params = {k: v for k, v in hist.items() if not is_secret(k)}
        return params, (["password"] if hist.get("password_mode") == "same" else [])
    try:
        declared = [p.key for p in get(item["action"]).params_for(svc)]
    except ValidationError:
        declared = list(hist)
    source, from_job = None, False
    if item.get("job_id"):
        row = svc.db.get_job(item["job_id"])
        if row is not None:
            try:
                source = (json.loads(row["params"] or "{}") or {}).get("options")
                from_job = isinstance(source, dict)
            except (TypeError, ValueError):
                source = None
    if not from_job:
        source = hist
    params: Dict[str, Any] = {}
    blank: List[str] = []
    for key in declared:
        if is_secret(key):
            params[key] = ""
            if hist.get(key):                       # «задан (не сохраняется)» — был введён, введите заново
                blank.append(key)
            continue
        if key not in source:
            continue
        value = source[key]
        if not from_job and isinstance(value, str) and len(value) > 300 and value.endswith("…"):
            continue                                # укороченный текст из истории не подставляем
        params[key] = value
    return params, blank


def retry_info(svc, op_id: int) -> Dict[str, Any]:
    """Что запустить заново после операции: то же действие для ящиков с ошибкой и не обработанных.

    Само ничего не запускает: интерфейс подставляет ящики и параметры, а запуск — как обычно, через
    проверку (условия перечитываются, опасное подтверждается числом ящиков). Какие ящики сделаны, задание
    отмечает по ходу работы (``progress``): после перезапуска службы или у операции больше 5000 ящиков
    повтор всё равно знает их точно. Ящик, на котором задание прервалось посреди работы, предлагается с
    предупреждением (``uncertain``). Удаление прошло, а адрес коллеге не передан — такие ящики идут в
    «Передать адрес коллеге» (``follow``), а не в повторное удаление. Секретные параметры (общий пароль)
    не сохранялись — их вводят заново (``blank``).
    """
    from . import progress
    item = history_item(svc, op_id)
    if item is None:
        raise ValidationError("Операция не найдена.")
    job = item.get("job")
    if job is not None and job.get("status") in JobStatus.ACTIVE:
        raise ValidationError("Задание этой операции ещё идёт — дождитесь его конца или отмените.")
    results = item.get("results") or []
    launched = [r["key"] for r in results if r.get("status") == OK]
    keys: List[str] = []
    uncertain: List[str] = []
    follow: List[str] = []
    marks = progress.items(svc.db, op_id)
    if marks is not None:
        for key in launched:
            mark = marks.get(key)
            if mark is None or mark["status"] == progress.FAILED and not mark["next"]:
                keys.append(key)                    # не начат или ошибка
            elif mark["status"] == progress.RUNNING:
                keys.append(key)                    # задание прервалось посреди этого ящика
                uncertain.append(key)
            elif mark["status"] == progress.FAILED and mark["next"] == "ax_forward":
                follow.append(key)
    elif item.get("job_id") and launched:
        # задание до 1.13 — отметок нет, есть только итог задания
        if job is None:
            raise ValidationError(_UNKNOWN.format(why="задания уже нет в журнале"), hint=_UNKNOWN_HINT)
        items = job.get("items") or []
        if not job.get("summary"):
            raise ValidationError(_UNKNOWN.format(why="задание прервано, его итог не сохранился"), hint=_UNKNOWN_HINT)
        if len(items) >= 5000:
            raise ValidationError(_UNKNOWN.format(why="итог задания сохранён не полностью"), hint=_UNKNOWN_HINT)
        done = {i.get("key"): i for i in items}
        for key in launched:
            got = done.get(key)
            if got is None or got.get("status") == "failed":
                keys.append(key)
    else:
        keys = [r["key"] for r in results if r.get("status") == FAIL and r.get("username")]
    params, blank = _retry_params(svc, item)
    out: Dict[str, Any] = {"op_id": op_id, "action": item["action"], "label": item["label"], "params": params,
                           "blank": blank, "keys": keys, "uncertain": uncertain, "follow": None}
    if follow:
        fwd = {"action": "ax_forward", "label": get("ax_forward").label, "keys": follow,
               "params": {"to": params.get("forward_to") or "", "with_aliases": bool(params.get("forward_aliases", True))}}
        if keys:
            out["follow"] = fwd                     # сначала — повтор, затем — передача адресов
        else:
            out.update({"action": fwd["action"], "label": fwd["label"], "keys": follow, "params": fwd["params"],
                        "blank": []})
    from . import create
    if out["action"] == create.KEY:
        # строки списка — с именем и фамилией из задания (в истории их нет)
        names: Dict[str, Tuple[str, str]] = {}
        if item.get("job_id"):
            row = svc.db.get_job(item["job_id"])
            try:
                opts = (json.loads(row["params"] or "{}") or {}).get("options") or {} if row is not None else {}
            except (TypeError, ValueError):
                opts = {}
            for it in opts.get("items") or []:
                names[it.get("email")] = (it.get("first") or "", it.get("last") or "")
        lines = []
        for k in keys:
            first, last = names.get(k, ("", ""))
            lines.append(f"{k}; {first}; {last}" if (first or last) else k)
        out["lines"] = "\n".join(lines)
    return out


def retry_count(svc, op_id: int) -> Tuple[int, str]:
    """Сколько ящиков предложит «Повторить для неудачных» (и почему 0, если повтор невозможен)."""
    try:
        info = retry_info(svc, op_id)
    except ValidationError as exc:
        return 0, exc.message
    return len(info["keys"]) + len((info.get("follow") or {}).get("keys") or []), ""


def cancel_op(svc, op_id: int) -> int:
    item = history_item(svc, op_id)
    if item is None:
        raise ValidationError("Операция не найдена.")
    cancelled = 0
    if item.get("job_id"):
        row = svc.db.get_job(item["job_id"])
        if row is not None and row["status"] in JobStatus.ACTIVE:
            svc.queue.cancel(item["job_id"])
            cancelled = 1
    return cancelled
