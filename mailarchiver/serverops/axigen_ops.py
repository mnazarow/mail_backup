"""
Действия с учётными записями Axigen (через CLI администратора).

Каждое изменение — отдельная короткая сессия CLI в журнале команд (пароли в
журнал не попадают); после изменения значение перечитывается с сервера и, если
сервер его не принял, это ошибка, а не «сделано». Снимок учётной записи в
базе обновляется сразу — раздел показывает новое значение, не дожидаясь
сверки.

Блокировка, разблокировка, очистка папок и удаление учётной записи — те же,
что в групповых действиях архива (:mod:`mailarchiver.axigen.jobs`): здесь они
доступны и для учётных записей без ящика в архиве (кроме удаления и очистки —
они требуют полного архива).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..bulk import Param
from ..errors import ValidationError
from ..models import AuthType
from ..util import human_size
from . import base
from . import targets as tg
from .base import Ctx, ServerAction

# ---------------------------------------------------------------------------
#  Общее
# ---------------------------------------------------------------------------
#: Категории служб Axigen → как они называются в списке служб (в нижнем регистре).
SERVICE_CATEGORIES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "pop3": ("POP3", ("pop3",)),
    "imap": ("IMAP", ("imap",)),
    "webmail": ("WebMail", ("webmail",)),
    "mobile": ("телефоны и Outlook (ActiveSync, MAPI)", ("activesync", "mobility", "eas", "outlookconnector", "mapi")),
    "smtp": ("отправка писем (SMTP)", ("remotedelivery", "smtp", "smtpauth", "smtpsubmission")),
    "dav": ("календари и контакты (CalDAV, CardDAV, WebDAV)", ("caldav", "carddav", "webdav")),
}


def _category(service: str) -> Optional[str]:
    low = (service or "").strip().lower()
    for key, (_title, names) in SERVICE_CATEGORIES.items():
        if low in names:
            return key
    return None


def _service_params(default: bool = False) -> Tuple[Param, ...]:
    return tuple(Param(f"svc_{key}", title, "bool", default) for key, (title, _n) in SERVICE_CATEGORIES.items())


def _chosen(params: Dict) -> List[str]:
    return [key for key in SERVICE_CATEGORIES if params.get(f"svc_{key}")]


def _cli(ctx: Ctx, summary: str, kind: str = "write"):
    from ..axigen.service import logged_session
    return logged_session(ctx.svc, username=ctx.user, kind=kind, summary=summary, st=ctx.axigen())


def _emit(ctx: Ctx, t: tg.Target):
    def emit(level: str, text: str) -> None:
        if ctx.job is not None:
            ctx.job.event(level, f"{t.email}: {text}"[:600])
    return emit


def _open_cli(ctx: Ctx):
    return lambda summary: _cli(ctx, summary)


#: Сервер принял команду, но значение после неё не показал: подтвердить изменение нечем.
UNCONFIRMED = " (сервер не показал значение после записи — проверьте в WebAdmin)"


_TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
             "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
             "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y",
             "ь": "", "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g"}


def translit(text: str) -> str:
    """Латиница для адреса: «Иванов Пётр» → «ivanov petr» (буквы, цифры, точка, дефис)."""
    out = []
    for ch in (text or "").strip().lower():
        out.append(_TRANSLIT.get(ch, ch))
    value = "".join(out)
    value = re.sub(r"[^a-z0-9.\-_ ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


# ---------------------------------------------------------------------------
#  Вход и пароли
# ---------------------------------------------------------------------------
class BlockAction(ServerAction):
    key = "ax_block"
    label = "Заблокировать вход"
    group = "access"
    icon = "🔒"
    danger = 1
    axigen_change = True
    desc = ("Закрыть учётной записи вход на сервере Axigen: POP3, IMAP, WebMail, телефоны, отправку писем. Если у "
            "учётной записи есть ящик в архиве, сначала делается его последняя копия; не удалась — вход не "
            "закрывается. Прежний список служб запоминается — «Разблокировать вход» вернёт его.")
    note = "Ящик, который копируется по IMAP с паролем, после блокировки копироваться не сможет — переведите его " \
           "на копирование через FTP Backup."
    params = (
        Param("final_copy", "Сначала сделать последнюю копию ящика (если он есть в архиве)", "bool", True),
        Param("keep_receiving", "Продолжать принимать письма", "bool", True,
              help="Письма на адрес будут приходить (и копироваться в архив). Иначе отправители получат отказ."),
        Param("random_password", "Сменить пароль на случайный", "bool", False,
              help="Чтобы никто не вошёл, даже если вход откроют. Пароль никому не показывается."),
    )

    def check(self, ctx, t):
        if t.axigen.get("login_blocked") is True and not ctx.params.get("random_password"):
            return "вход уже закрыт"
        return ""

    def plan(self, ctx, t):
        parts = []
        if ctx.params.get("final_copy", True) and t.mailbox is not None and t.mailbox.enabled:
            parts.append("последняя копия")
        parts.append("вход уже закрыт" if t.axigen.get("login_blocked") else "вход будет закрыт")
        if ctx.params.get("random_password"):
            parts.append("пароль сменится на случайный")
        if t.mailbox is not None and t.mailbox.auth_type == AuthType.PASSWORD and t.mailbox.enabled:
            parts.append("⚠ копирование по IMAP после этого не пройдёт")
        return ", ".join(parts)

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import jobs as axjobs
        from ..axigen import store as axstore
        from ..axigen.common import services_blocked
        if t.mailbox is not None:
            return axjobs._do_block(ctx.job, _open_cli(ctx), t.mailbox, t.axigen, t.email, ctx.params, ctx.user,
                                    _emit(ctx, t))
        with _cli(ctx, f"Блокировка входа {t.email}") as cli:
            by_us = bool(t.axigen.get("blocked_at"))
            before, after = actions.block(cli, t.email, keep_receiving=bool(ctx.params.get("keep_receiving", True)),
                                          blocked_by_us=by_us)
            saved = t.axigen.get("saved_services")
            if not services_blocked(before, blocked_by_us=by_us):
                saved = before
            axstore.note_blocked(ctx.db, t.email, saved_services=saved, user=ctx.user, services=after)
            notes = [f"вход закрыт (службы были: {actions.describe_services(before)}; стали: "
                     f"{actions.describe_services(after)})"]
            if ctx.params.get("random_password"):
                actions.set_password(cli, t.email, actions.random_password())
                notes.append("пароль сменён на случайный (нигде не сохранён)")
        return "ok", "; ".join(notes)


class UnblockAction(ServerAction):
    key = "ax_unblock"
    label = "Разблокировать вход"
    group = "access"
    icon = "🔓"
    danger = 1
    axigen_change = True
    desc = "Вернуть учётной записи службы, которые были до блокировки из MailArchiver."

    def check(self, ctx, t):
        if t.axigen.get("login_blocked") is False:
            return "вход не закрыт"
        if not t.axigen.get("saved_services"):
            return "вход закрывали не из MailArchiver — прежний список служб неизвестен (откройте вход в WebAdmin)"
        return ""

    def plan(self, ctx, t):
        return "службы станут: " + ", ".join(t.axigen.get("saved_services") or [])

    def execute(self, ctx, t):
        from ..axigen import jobs as axjobs
        return axjobs._do_unblock(ctx.job, _open_cli(ctx), t.mailbox, t.axigen, t.email, ctx.params, ctx.user,
                                  _emit(ctx, t))


PASSWORD_MODES = (("random_file", "случайные — файлом для раздачи сотрудникам (скачать один раз)"),
                  ("random_hidden", "случайные — никому не показывать"),
                  ("same", "один и тот же пароль, который я введу"))


class PasswordAction(ServerAction):
    key = "ax_password"
    label = "Сменить пароли"
    group = "access"
    icon = "🔑"
    danger = 1
    axigen_change = True
    desc = ("Задать учётным записям новые пароли: случайные (файлом для раздачи сотрудникам или никому не "
            "показывая) или один общий временный. Можно потребовать сменить пароль при следующем входе. У ящиков, "
            "которые копируются по IMAP с паролем, новый пароль сохраняется в карточке ящика — копирование "
            "продолжится.")
    note = ("Файл с паролями скачивается в «Истории» раздела один раз и не позже чем через сутки; пароли хранятся "
            "до выдачи зашифрованными.")
    params = (
        Param("mode", "Какие пароли", "select", "random_file", options=PASSWORD_MODES),
        Param("password", "Пароль", "text", "", show_if={"mode": ["same"]},
              help="От 6 до 64 знаков, без пробелов, кавычек и «\\»."),
        Param("must_change", "Потребовать сменить пароль при следующем входе", "bool", False),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        if ctx.params.get("mode") == "same":
            from ..axigen.actions import check_password
            if ctx.job is None:
                check_password(ctx.params.get("password") or "")
            else:
                # в задании пароль — зашифрованным (см. options)
                check_password(ctx.db.secret.decrypt(ctx.params.get("password_enc") or ""))

    def options(self, ctx):
        opts = dict(ctx.params)
        password = opts.pop("password", "") or ""
        if opts.get("mode") == "same":
            opts["password_enc"] = ctx.db.secret.encrypt(password)
        return opts

    def plan(self, ctx, t):
        mode = ctx.params.get("mode")
        text = {"random_file": "случайный пароль — в файл для раздачи", "random_hidden": "случайный пароль",
                "same": "общий пароль"}.get(mode, "новый пароль")
        if ctx.params.get("must_change"):
            text += "; смена пароля при следующем входе"
        if t.mailbox is not None and t.mailbox.auth_type == AuthType.PASSWORD:
            text += "; сохранится в карточке ящика"
        return text

    def execute(self, ctx, t):
        from ..axigen import actions
        from . import secrets
        mode = ctx.params.get("mode")
        if mode == "same":
            password = ctx.db.secret.decrypt(ctx.params.get("password_enc") or "")
            actions.check_password(password)
        else:
            password = actions.random_password()
        notes = []
        acc = t.mailbox
        op_id = ctx.data.get("op_id")
        to_file = mode == "random_file" and bool(op_id)
        with _cli(ctx, f"Смена пароля {t.email}") as cli:
            # Новый пароль — в файл для раздачи ДО смены на сервере: если что-то упадёт после смены, у
            # учётной записи не останется пароля, которого никто не знает.
            if to_file:
                secrets.save(ctx.db, op_id, t.email, password, name=t.name)
            try:
                actions.set_password(cli, t.email, password)
            except Exception:
                if to_file:
                    secrets.set_note(ctx.db, op_id, t.email, "смена не подтверждена сервером — проверьте вход")
                raise
            notes.append("пароль сменён")
            if to_file:
                notes.append("пароль — в файле для раздачи (История → операция)")
            if acc is not None and (acc.auth_type == AuthType.PASSWORD or acc.password):
                fresh = ctx.db.get_account(acc.id) or acc
                fresh.password = password
                ctx.db.update_account(fresh, update_password=True, update_oauth_secret=False,
                                      update_oauth_token=False)
                ctx.db.set_login_status(acc.id, "", "")
                notes.append("новый пароль сохранён в карточке ящика")
            if ctx.params.get("must_change"):
                try:
                    actions.set_account_flag(cli, t.email, "mustChangePass", "yes")
                    notes.append("при следующем входе потребуется сменить пароль")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"требование сменить пароль не записано ({getattr(exc, 'message', exc)})")
        return "ok", "; ".join(notes)


class MustChangeAction(ServerAction):
    key = "ax_must_change"
    label = "Требовать смену пароля при входе"
    group = "access"
    icon = "⏏️"
    axigen_change = True
    desc = "Потребовать (или перестать требовать), чтобы при следующем входе сотрудник сменил пароль."
    params = (Param("value", "Требовать", "select", "yes", options=(("yes", "да"), ("no", "нет"))),)

    def plan(self, ctx, t):
        return "при следующем входе — смена пароля" if ctx.params.get("value") == "yes" else "требование снимется"

    def execute(self, ctx, t):
        from ..axigen import actions
        value = ctx.params.get("value")
        with _cli(ctx, f"Смена пароля при входе {t.email}: {value}") as cli:
            actions.set_account_flag(cli, t.email, "mustChangePass", value)
            shown = actions.read_account_flags(cli, t.email).get("mustChangePass", "")
        if shown and shown != value:
            return "fail", f"сервер показывает mustChangePass = {shown}"
        text = "при следующем входе потребуется сменить пароль" if value == "yes" else "требование снято"
        return "ok", text + ("" if shown else UNCONFIRMED)


class ServicesOffAction(ServerAction):
    key = "ax_services_off"
    label = "Отключить службы"
    group = "access"
    icon = "🚫"
    danger = 1
    axigen_change = True
    desc = ("Убрать у учётных записей выбранные службы — например, запретить POP3 и вход с телефонов, оставив "
            "WebMail. Остальные службы не меняются. Если не останется ни одной службы входа, это блокировка: прежний "
            "список запоминается, и «Разблокировать вход» вернёт его.")
    params = _service_params()

    def prepare(self, ctx):
        super().prepare(ctx)
        if not _chosen(ctx.params):
            raise ValidationError("Отметьте хотя бы одну службу.")

    def check(self, ctx, t):
        services = t.axigen.get("services")
        if not services:
            return "служб учётной записи не видно (наследуются от домена) — задайте их в WebAdmin"
        chosen = set(_chosen(ctx.params))
        if not [s for s in services if _category(s) in chosen]:
            return "выбранных служб у учётной записи и так нет"
        return ""

    def plan(self, ctx, t):
        chosen = set(_chosen(ctx.params))
        drop = [s for s in t.axigen.get("services") or [] if _category(s) in chosen]
        return "отключатся: " + ", ".join(drop)

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import store as axstore
        from ..axigen.common import services_blocked
        chosen = set(_chosen(ctx.params))
        with _cli(ctx, f"Отключение служб {t.email}") as cli:
            current = actions.get_services(cli, t.email)
            if not current:
                return "skip", "служб учётной записи не видно (наследуются от домена) — задайте их в WebAdmin"
            new = [s for s in current if _category(s) not in chosen]
            if len(new) == len(current):
                return "skip", "выбранных служб у учётной записи нет"
            after = actions.set_services(cli, t.email, new)
        blocked = services_blocked(after)
        if blocked and not t.axigen.get("saved_services"):
            axstore.note_blocked(ctx.db, t.email, saved_services=current, user=ctx.user, services=after)
        else:
            axstore.note_services(ctx.db, t.email, after, blocked)
        dropped = [s for s in current if s not in new]
        text = f"отключены: {', '.join(dropped)}; остались: {actions.describe_services(after)}"
        if blocked:
            text += " — вход закрыт"
        return "ok", text


class ServicesOnAction(ServerAction):
    key = "ax_services_on"
    label = "Включить службы"
    group = "access"
    icon = "✅"
    danger = 1
    axigen_change = True
    desc = ("Добавить учётным записям выбранные службы. Названия служб берутся из списка, разрешённого домену "
            "(WebAdmin → домен → Admin Limits), — так, как их пишет сервер.")
    params = _service_params()

    def prepare(self, ctx):
        super().prepare(ctx)
        if not _chosen(ctx.params):
            raise ValidationError("Отметьте хотя бы одну службу.")

    def check(self, ctx, t):
        if not t.axigen.get("services"):
            return "служб учётной записи не видно (наследуются от домена) — задайте их в WebAdmin"
        return ""

    def plan(self, ctx, t):
        names = [SERVICE_CATEGORIES[k][0] for k in _chosen(ctx.params)]
        return "добавятся (если домен их разрешает): " + ", ".join(names)

    def _available(self, ctx, cli, domain: str) -> Optional[List[str]]:
        cache = ctx.data.setdefault("domain_services", {})
        if domain not in cache:
            from ..axigen import actions
            try:
                cache[domain] = actions.domain_available_services(cli, domain)
            except Exception:  # noqa: BLE001
                cache[domain] = None
        return cache[domain]

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import store as axstore
        from ..axigen.common import services_blocked
        chosen = set(_chosen(ctx.params))
        _local, domain = actions.parse_email(t.email)
        with _cli(ctx, f"Включение служб {t.email}") as cli:
            available = self._available(ctx, cli, domain)
            if not available:
                return "skip", (f"не удалось прочитать службы, разрешённые домену {domain} (adminLimits → "
                                f"availableServices) — включите службы в WebAdmin")
            current = actions.get_services(cli, t.email)
            if not current:
                return "skip", "служб учётной записи не видно (наследуются от домена) — задайте их в WebAdmin"
            have = {s.lower() for s in current}
            add = [s for s in available if _category(s) in chosen and s.lower() not in have]
            if not add:
                missing = [SERVICE_CATEGORIES[k][0] for k in chosen
                           if not any(_category(s) == k for s in available)]
                if missing:
                    return "skip", f"домен не разрешает: {', '.join(missing)}"
                return "skip", "выбранные службы уже включены"
            after = actions.set_services(cli, t.email, list(current) + add)
        blocked = services_blocked(after, blocked_by_us=bool(t.axigen.get("blocked_at")))
        if blocked is False and t.axigen.get("blocked_at"):
            axstore.note_unblocked(ctx.db, t.email, after)
        else:
            axstore.note_services(ctx.db, t.email, after, blocked)
        return "ok", f"включены: {', '.join(add)}"


# ---------------------------------------------------------------------------
#  Учётные записи
# ---------------------------------------------------------------------------
class QuotaAction(ServerAction):
    key = "ax_quota"
    label = "Квота ящика"
    group = "accounts"
    icon = "📦"
    danger = 1
    axigen_change = True
    desc = ("Задать учётным записям предел объёма писем. Учётные записи, у которых уже занято больше новой квоты, "
            "пропускаются: с такой квотой они перестали бы принимать письма.")
    params = (
        Param("quota_mb", "Квота, МБ", "int", 2048, min=1, max=4 * 1024 * 1024,
              help="1 ГБ = 1024 МБ. Наибольшая квота Axigen — 4 ТБ."),
        Param("only_raise", "Только увеличивать (меньшую квоту не ставить)", "bool", True),
        Param("threshold", "Предупреждать при заполнении, %", "int", 0, min=0, max=100,
              help="0 — не менять настройку предупреждения. Иначе сервер пришлёт письмо, когда ящик заполнится на "
                   "столько процентов."),
    )

    def check(self, ctx, t):
        new_kb = int(ctx.params["quota_mb"]) * 1024
        quota = t.axigen.get("quota_kb")
        used = t.axigen.get("used_bytes")
        if ctx.params.get("only_raise") and quota and quota >= new_kb:
            return f"квота и так {human_size(quota * 1024)} — не меньше новой"
        if used is not None and used > new_kb * 1024:
            return f"занято {human_size(used)} — больше новой квоты: учётная запись перестала бы принимать письма"
        return ""

    def plan(self, ctx, t):
        quota = t.axigen.get("quota_kb")
        new = human_size(int(ctx.params["quota_mb"]) * 1024 * 1024)
        return f"квота: {human_size(quota * 1024) if quota else 'не задана'} → {new}"

    def execute(self, ctx, t):
        from ..axigen import actions, directory
        from ..axigen import store as axstore
        new_kb = int(ctx.params["quota_mb"]) * 1024
        threshold = int(ctx.params.get("threshold") or 0) or None
        local, domain = actions.parse_email(t.email)
        with _cli(ctx, f"Квота {t.email}: {new_kb} КБ") as cli:
            # занятое место — прямо сейчас, а не по сверке
            directory.enter_account(cli, domain, local)
            try:
                folders, _table = directory.account_folders(cli)
            finally:
                cli.to_root()
            sizes = [f.size for f in folders if f.size is not None]
            used = sum(sizes) if sizes else None
            if used is not None and used > new_kb * 1024:
                return "skip", f"занято {human_size(used)} — больше новой квоты"
            before = actions.read_quota_kb(cli, t.email)
            if ctx.params.get("only_raise") and before and before >= new_kb:
                return "skip", f"квота и так {human_size(before * 1024)}"
            after = actions.set_quota(cli, t.email, new_kb, threshold=threshold)
        if after is not None and after != new_kb:
            return "fail", f"сервер показывает квоту {after} КБ вместо {new_kb} КБ"
        axstore.update_fields(ctx.db, t.email, quota_kb=new_kb)
        text = f"квота: {human_size(before * 1024) if before else 'не задана'} → {human_size(new_kb * 1024)}"
        if threshold:
            text += f"; предупреждение при {threshold} %"
        return "ok", text + (UNCONFIRMED if after is None else "")


ALIAS_VARS = ("login", "first", "last", "f", "l", "middle")


def render_alias(template: str, row: Dict, employee=None) -> str:
    """Псевдоним по шаблону: {first}.{last} → ivan.ivanov (латиницей, в нижнем регистре)."""
    first = row.get("first_name") or ""
    last = row.get("last_name") or ""
    middle = ""
    if employee is not None and (not first or not last):
        words = (employee["full_name"] or "").split()
        if len(words) >= 2:
            last, first = last or words[0], first or words[1]
            middle = " ".join(words[2:])
    values = {"login": (row.get("name") or row.get("email", "").split("@")[0]).lower(),
              "first": translit(first).replace(" ", "-"), "last": translit(last).replace(" ", "-"),
              "middle": translit(middle).replace(" ", "-")}
    values["f"] = values["first"][:1]
    values["l"] = values["last"][:1]
    out = template
    for key in ALIAS_VARS:
        token = "{" + key + "}"
        if token in out and not values[key]:
            return ""
        out = out.replace(token, values[key])
    return out.strip(".-_").lower()


class AliasAddAction(ServerAction):
    key = "ax_alias_add"
    label = "Добавить псевдоним по шаблону"
    group = "accounts"
    icon = "🏷️"
    axigen_change = True
    desc = ("Добавить каждой учётной записи псевдоним (второй адрес) по шаблону из её имени: {first}.{last} → "
            "ivan.ivanov. Имя и фамилия — из сведений учётной записи, а если их нет — из раздела «Сотрудники»; "
            "кириллица пишется латиницей. Адрес, уже занятый учётной записью или псевдонимом, пропускается.")
    params = (
        Param("template", "Шаблон", "text", "{first}.{last}",
              help="{first} — имя, {last} — фамилия, {middle} — отчество, {f} и {l} — их первые буквы, {login} — "
                   "имя учётной записи. Например: {f}.{last} → i.ivanov"),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        template = ctx.params.get("template") or ""
        if not any("{" + v + "}" in template for v in ALIAS_VARS):
            raise ValidationError("В шаблоне нет ни одной подстановки ({first}, {last}, {login}…).")
        if "@" in template:
            raise ValidationError("Шаблон — только имя псевдонима, без «@домен»: псевдоним заводится в домене "
                                  "учётной записи.")
        from ..axigen import store as axstore
        taken = set()
        for row in axstore.list_accounts(ctx.db, include_deleted=False):
            taken.add(row["email"])
            taken.update(row.get("aliases") or [])
        ctx.data["taken"] = taken
        ctx.data["claimed"] = {}

    def _alias(self, ctx, t) -> str:
        employee = ctx.db.get_employee_by_email(t.email)
        return render_alias(ctx.params.get("template") or "", t.axigen, employee)

    def check(self, ctx, t):
        from ..axigen.directory import valid_account_name
        name = self._alias(ctx, t)
        if not name:
            return "не заполнены имя и фамилия (ни у учётной записи, ни в «Сотрудниках»)"
        if not valid_account_name(name):
            return f"«{name}» — недопустимое имя псевдонима"
        domain = t.email.split("@", 1)[1]
        address = f"{name}@{domain}"
        if address in (t.axigen.get("aliases") or []) or address == t.email:
            return f"псевдоним {address} уже есть"
        if address in ctx.data["taken"]:
            return f"адрес {address} уже занят другой учётной записью или псевдонимом"
        other = ctx.data["claimed"].get(address)
        if other and other != t.key:
            return f"такой же псевдоним {address} получается у другой выбранной учётной записи"
        ctx.data["claimed"][address] = t.key
        return ""

    def plan(self, ctx, t):
        return f"псевдоним {self._alias(ctx, t)}@{t.email.split('@', 1)[1]}"

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import store as axstore
        name = self._alias(ctx, t)
        with _cli(ctx, f"Псевдоним {name} для {t.email}") as cli:
            after = actions.add_alias(cli, t.email, name)
        axstore.update_fields(ctx.db, t.email, aliases=after)
        return "ok", f"добавлен псевдоним {name}@{t.email.split('@', 1)[1]}"


class AliasRemoveAction(ServerAction):
    key = "ax_alias_remove"
    label = "Удалить псевдонимы"
    group = "accounts"
    icon = "✂️"
    danger = 1
    axigen_change = True
    desc = ("Удалить у учётных записей псевдонимы — перечисленные или все. Письма на удалённый псевдоним перестанут "
            "доходить (отправитель получит отказ).")
    params = (
        Param("which", "Какие", "select", "list", options=(("list", "перечисленные"), ("all", "все псевдонимы"))),
        Param("aliases", "Псевдонимы", "textarea", "", show_if={"which": ["list"]},
              placeholder="i.ivanov\npetrov.old@company.ru",
              help="По одному в строке; можно без домена — тогда в домене учётной записи."),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        if ctx.params.get("which") == "list" and not (ctx.params.get("aliases") or "").strip():
            raise ValidationError("Перечислите псевдонимы, которые нужно удалить.")

    def _remove(self, ctx, t) -> List[str]:
        have = list(t.axigen.get("aliases") or [])
        if ctx.params.get("which") == "all":
            return have
        domain = t.email.split("@", 1)[1]
        wanted = set()
        for raw in re.split(r"[\s,;]+", ctx.params.get("aliases") or ""):
            raw = raw.strip().lower()
            if raw:
                wanted.add(raw if "@" in raw else f"{raw}@{domain}")
        return [a for a in have if a in wanted]

    def check(self, ctx, t):
        return "" if self._remove(ctx, t) else "таких псевдонимов у учётной записи нет"

    def plan(self, ctx, t):
        return "удалятся: " + ", ".join(self._remove(ctx, t))

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import store as axstore
        remove = self._remove(ctx, t)
        after = list(t.axigen.get("aliases") or [])
        with _cli(ctx, f"Удаление псевдонимов {t.email}") as cli:
            current = actions.list_aliases(cli, t.email)
            remove = [a for a in remove if a in current]
            if not remove:
                axstore.update_fields(ctx.db, t.email, aliases=current)
                return "skip", "этих псевдонимов на сервере уже нет"
            for alias in remove:
                after = actions.remove_alias(cli, t.email, alias)
        axstore.update_fields(ctx.db, t.email, aliases=after)
        return "ok", "удалены: " + ", ".join(remove)


def split_full_name(full: str, order: str) -> Dict[str, str]:
    words = (full or "").split()
    if not words:
        return {}
    if order == "first_last":
        if len(words) == 1:
            return {"firstName": words[0]}
        return {"firstName": words[0], "lastName": words[-1], "middleName": " ".join(words[1:-1])}
    if len(words) == 1:
        return {"lastName": words[0]}
    return {"lastName": words[0], "firstName": words[1], "middleName": " ".join(words[2:])}


class ContactAction(ServerAction):
    key = "ax_contact"
    label = "ФИО, отдел, должность"
    group = "accounts"
    icon = "📇"
    axigen_change = True
    desc = ("Заполнить контактные данные учётных записей (их видно в адресной книге): из раздела «Сотрудники» — "
            "ФИО, отдел, должность и телефон, — или одинаковые для всех (организация, отдел, офис). По умолчанию "
            "заполняются только пустые поля.")
    params = (
        Param("source", "Откуда", "select", "employees",
              options=(("employees", "из раздела «Сотрудники» (по адресу)"), ("fixed", "одинаковые значения всем"))),
        Param("name_order", "Как записано ФИО у сотрудников", "select", "last_first",
              options=(("last_first", "Фамилия Имя Отчество"), ("first_last", "Имя Фамилия")),
              show_if={"source": ["employees"]}),
        Param("phone", "Телефон сотрудника — в «рабочий телефон»", "bool", True, show_if={"source": ["employees"]}),
        Param("company", "Организация", "text", "", placeholder="пусто — не менять"),
        Param("department", "Отдел", "text", "", placeholder="пусто — не менять", show_if={"source": ["fixed"]}),
        Param("office", "Офис", "text", "", placeholder="пусто — не менять", show_if={"source": ["fixed"]}),
        Param("overwrite", "Заменять уже заполненные поля", "bool", False),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        from ..axigen.cli import quote
        for key in ("company", "department", "office"):
            value = (ctx.params.get(key) or "").strip()
            if value:
                try:
                    quote(value, "значение")
                except Exception as exc:  # noqa: BLE001
                    raise ValidationError(getattr(exc, "message", str(exc))) from exc
        if ctx.params.get("source") == "fixed" and not any((ctx.params.get(k) or "").strip()
                                                          for k in ("company", "department", "office")):
            raise ValidationError("Заполните хотя бы одно значение: организацию, отдел или офис.")

    def _wanted(self, ctx, t) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if ctx.params.get("source") == "employees":
            emp = ctx.db.get_employee_by_email(t.email)
            if emp is None:
                return {}
            out.update(split_full_name(emp["full_name"] or "", ctx.params.get("name_order") or "last_first"))
            if emp["department"]:
                out["department"] = emp["department"]
            if emp["position"]:
                out["position"] = emp["position"]
            if ctx.params.get("phone") and emp["phone"]:
                out["businessPhone"] = emp["phone"]
        else:
            for key in ("department", "office"):
                if (ctx.params.get(key) or "").strip():
                    out[key] = ctx.params[key].strip()
        if (ctx.params.get("company") or "").strip():
            out["company"] = ctx.params["company"].strip()
        clean = {}
        for key, value in out.items():
            value = re.sub(r"[\"\\\x00-\x1f\x7f]", "", str(value)).strip()[:200]
            if value:
                clean[key] = value
        return clean

    def check(self, ctx, t):
        if ctx.params.get("source") == "employees" and ctx.db.get_employee_by_email(t.email) is None:
            return "сотрудника с этим адресом нет в разделе «Сотрудники»"
        return "" if self._wanted(ctx, t) else "нечего записывать (у сотрудника не заполнены нужные поля)"

    def plan(self, ctx, t):
        from ..axigen.actions import CONTACT_FIELDS
        wanted = self._wanted(ctx, t)
        mode = "" if ctx.params.get("overwrite") else " (только пустые поля)"
        return "; ".join(f"{CONTACT_FIELDS[k]}: {v}" for k, v in wanted.items()) + mode

    def execute(self, ctx, t):
        from ..axigen import actions
        from ..axigen import store as axstore
        wanted = self._wanted(ctx, t)
        with _cli(ctx, f"Контактные данные {t.email}") as cli:
            current = actions.read_contact(cli, t.email)
            change = {k: v for k, v in wanted.items()
                      if current.get(k) != v and (ctx.params.get("overwrite") or not current.get(k))}
            if not change:
                return "skip", "поля уже заполнены" if not ctx.params.get("overwrite") else "всё уже так"
            after = actions.set_contact(cli, t.email, change)
        axstore.update_fields(ctx.db, t.email, first_name=after.get("firstName") or t.axigen.get("first_name") or "",
                              last_name=after.get("lastName") or t.axigen.get("last_name") or "")
        text = "записано: " + "; ".join(f"{actions.CONTACT_FIELDS[k]}: {v}" for k, v in change.items())
        unshown = [actions.CONTACT_FIELDS[k] for k in change if not after.get(k)]
        if unshown:
            text += f"; после записи сервер не показал: {', '.join(unshown)} — проверьте в WebAdmin"
        return "ok", text


class _FlagAction(ServerAction):
    group = "accounts"
    axigen_change = True
    attr = ""
    values: Tuple[Tuple[str, str], ...] = ()

    def plan(self, ctx, t):
        return f"станет: {dict(self.values).get(ctx.params.get('value'), ctx.params.get('value'))}"

    def execute(self, ctx, t):
        from ..axigen import actions
        value = ctx.params.get("value")
        with _cli(ctx, f"{self.label} {t.email}: {value}") as cli:
            before = actions.read_account_flags(cli, t.email).get(self.attr, "")
            if before == value:
                return "skip", "уже так"
            actions.set_account_flag(cli, t.email, self.attr, value)
            shown = actions.read_account_flags(cli, t.email).get(self.attr, "")
        if shown and shown != value:
            return "fail", f"сервер показывает {self.attr} = {shown}"
        return "ok", f"{dict(self.values).get(value, value)} (было: {before or 'неизвестно'})" + \
            ("" if shown else UNCONFIRMED)


class AccountTypeAction(_FlagAction):
    key = "ax_account_type"
    label = "Тип учётной записи"
    icon = "⭐"
    danger = 1
    attr = "accountType"
    values = (("basic", "basic"), ("premium", "premium"))
    desc = ("Перевести учётные записи в тип basic или premium (от типа зависят доступные возможности и число "
            "лицензий). Если лицензий premium не хватит, сервер откажет — это будет видно по каждой учётной записи.")
    params = (Param("value", "Тип", "select", "basic", options=values),)


class PublishContactAction(_FlagAction):
    key = "ax_publish"
    label = "Показывать в контактах домена"
    icon = "👁️"
    attr = "publishContactInfo"
    values = (("yes", "показывать"), ("no", "не показывать"))
    desc = "Показывать учётные записи в общих контактах домена (адресной книге сотрудников) или скрыть их."
    params = (Param("value", "В контактах домена", "select", "yes", options=values),)


class AddToArchiveAction(ServerAction):
    key = "ax_add_mailboxes"
    label = "Завести ящики в архиве"
    group = "archive"
    icon = "📥"
    immediate = True
    desc = ("Завести в MailArchiver ящики для учётных записей, у которых их ещё нет, — на сервере ничего не "
            "меняется. Ящики берут настройки из шаблона ящиков сотрудников (срок хранения, папки).")
    params = (
        Param("auth", "Как копировать", "select", AuthType.AXIGEN_FTP,
              options=((AuthType.AXIGEN_FTP, "через FTP Backup Axigen — без паролей сотрудников"),
                       (AuthType.PASSWORD, "по IMAP с паролем (пароль нужно будет вписать)"))),
        Param("enable", "Сразу включить копирование", "bool", True),
    )

    def check(self, ctx, t):
        if t.mailbox is not None:
            return f"уже копируется: ящик «{t.mailbox.name}»"
        return ""

    def plan(self, ctx, t):
        return "будет заведён ящик " + ("(через FTP Backup)" if ctx.params.get("auth") == AuthType.AXIGEN_FTP
                                        else "(по паролю)")

    def apply(self, ctx, t):
        from ..axigen.jobs import add_mailboxes
        items = add_mailboxes(ctx.svc, [t.email], user=ctx.user, auth=ctx.params.get("auth"),
                              enable=bool(ctx.params.get("enable", True)))
        item = items[0] if items else {"status": "skip", "text": "не заведён"}
        if item["status"] != "ok":
            raise ValidationError(item["text"])
        return item["text"]


class RefreshAction(ServerAction):
    key = "ax_refresh"
    label = "Перечитать с сервера"
    group = "reports"
    icon = "🔄"
    read_only = True
    desc = ("Прочитать учётные записи с сервера заново — ФИО, квоту, занятое место, службы, псевдонимы, последний "
            "вход, — не дожидаясь общей сверки. На сервере ничего не меняется.")

    def plan(self, ctx, t):
        return "будет перечитана"

    def execute(self, ctx, t):
        from ..axigen import actions, directory
        from ..axigen import store as axstore
        local, domain = actions.parse_email(t.email)
        with _cli(ctx, f"Учётная запись {t.email}", kind="read") as cli:
            names = directory.list_account_names(cli, domain)
            name = next((n for n in names if n.lower() == local.lower()), None)
            if name is None:
                axstore.note_deleted(ctx.db, t.email)
                return "ok", "учётной записи на сервере больше нет — помечена удалённой (архив сохранён)"
            info = directory.account_details(cli, domain, name)
        axstore.save_snapshot(ctx.db, [info], [])
        from .usage import record_axigen_rows, today
        record_axigen_rows(ctx.db, [dict(info, email=t.email)], day=today(ctx.svc))
        parts = []
        if info.get("used_bytes") is not None:
            parts.append(f"занято {human_size(info['used_bytes'])}")
        if info.get("quota_kb"):
            parts.append(f"квота {human_size(info['quota_kb'] * 1024)}")
        parts.append("вход закрыт" if info.get("login_blocked") else
                     ("вход открыт" if info.get("login_blocked") is False else "вход: неизвестно"))
        if info.get("errors"):
            parts.append(f"не прочитано: {info['errors'][:200]}")
        return "ok", "перечитана: " + ", ".join(parts)


# ---------------------------------------------------------------------------
#  Очистка и удаление (только при полном архиве)
# ---------------------------------------------------------------------------
class AxPurgeAction(ServerAction):
    key = "ax_purge"
    label = "Удалить старые письма (Axigen)"
    group = "cleanup"
    icon = "🧹"
    danger = 2
    axigen_change = True
    desc = ("Удалить в папках учётной записи письма старше заданного срока — только если каждое письмо папки уже в "
            "архиве: сначала свежая копия, затем каждая папка на сервере сверяется с архивом; хоть одного письма "
            "нет — папка не очищается. Только для ящиков, которые копируются через FTP Backup и хранят в архиве всё "
            "(«хранить всё» или бессрочное удержание).")
    note = ("Очищаются папки, которые разрешено очищать домену (WebAdmin → домен → Admin Limits → Purge folders), "
            "если не указаны явно. Удалённое вернуть можно только из архива.")
    params = (
        Param("days", "Удалить письма старше, дней", "int", 365, min=30, max=36500),
        Param("folders", "Папки", "text", "", placeholder="пусто — все, которые домену разрешено очищать",
              help="Через запятую, как на сервере: INBOX, Sent Items, Trash."),
    )

    def check(self, ctx, t):
        acc = t.mailbox
        if acc is None:
            return "ящика нет в архиве — удалять с сервера нельзя"
        if acc.auth_type != AuthType.AXIGEN_FTP:
            return "ящик копируется не через FTP Backup — для очистки по IMAP есть «Удалить старые письма (IMAP)»"
        keeps, why = ctx.keeps(acc)
        if not keeps:
            return why
        if not acc.enabled:
            return "копирование ящика выключено"
        return ""

    def plan(self, ctx, t):
        folders = ctx.params.get("folders") or "разрешённые домену папки"
        return f"свежая копия, сверка папок, затем удаление писем старше {ctx.params.get('days')} дн.: {folders}"

    def execute(self, ctx, t):
        from ..axigen import jobs as axjobs
        return axjobs._do_purge(ctx.job, _open_cli(ctx), t.mailbox, t.axigen, t.email, ctx.params, ctx.user,
                                _emit(ctx, t))


class AxDeleteAction(ServerAction):
    key = "ax_delete"
    label = "Удалить учётную запись"
    group = "cleanup"
    icon = "🗑️"
    danger = 2
    axigen_change = True
    desc = ("Удалить учётную запись на сервере Axigen, сохранив письма в архиве. Только если вход уже заблокирован и "
            "ящик копируется через FTP Backup: на время удаления останавливается приём писем, архив ставится на "
            "бессрочное удержание, ящик копируется ещё раз, и все папки сверяются с архивом — хоть одного письма "
            "нет, учётная запись не удаляется. Копирование ящика выключается.")
    note = ("Удаление на сервере необратимо: вернуть письма можно только из архива, создав учётную запись заново. "
            "Чтобы письма на адрес уволенного не терялись, укажите коллегу — адрес станет его псевдонимом.")
    params = (
        Param("forward_to", "Передать адрес коллеге", "text", "", placeholder="пусто — не передавать",
              help="Адрес учётной записи коллеги в том же домене. После удаления адрес (и псевдонимы, если "
                   "отмечено) станет её псевдонимом — письма будут приходить коллеге."),
        Param("forward_aliases", "Передать и псевдонимы удаляемой учётной записи", "bool", True),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        to = (ctx.params.get("forward_to") or "").strip().lower()
        if to:
            ctx.params["forward_to"] = to
            ctx.data["colleague"] = colleague_row(ctx, to)

    def validate_plan(self, ctx, items):
        to = ctx.params.get("forward_to")
        if to and any(t.email == to for t in items):
            raise ValidationError(f"Коллега {to} сам среди удаляемых учётных записей.")

    def check(self, ctx, t):
        acc = t.mailbox
        if acc is None:
            return "ящика нет в архиве — удалять учётную запись нельзя (письма пропали бы)"
        if not t.axigen.get("login_blocked"):
            return "вход на сервере не заблокирован — сначала «Заблокировать вход»"
        if acc.auth_type != AuthType.AXIGEN_FTP:
            return ("ящик копируется по IMAP — полноту архива перед удалением не проверить; переведите его на "
                    "копирование через FTP Backup")
        to = ctx.params.get("forward_to")
        if to:
            if t.email == ctx.data["colleague"]["email"]:
                return "это коллега, которому передаются адреса, — её не удаляем"
            problem = transfer_problem(ctx, t, to)
            if problem:
                # просили передать адрес — без этого не удаляем (письма на адрес иначе пойдут в отказ)
                return f"адрес коллеге передать нельзя: {problem}"
        return ""

    def plan(self, ctx, t):
        stats = ctx.totals.get(t.mailbox.id) or {}
        text = (f"приём писем остановится, удержание архива, последняя копия, сверка всех папок, удаление; в архиве "
                f"писем {int(stats.get('messages', 0))}")
        to = ctx.params.get("forward_to")
        if to:
            colleague = ctx.data["colleague"]["email"]
            text += (f"; затем {', '.join(transfer_addresses(t, colleague, ctx.params.get('forward_aliases', True)))}"
                     f" → {colleague}")
        return text

    def execute(self, ctx, t):
        from ..axigen import jobs as axjobs
        to = ctx.params.get("forward_to")
        before = None
        if to:
            # прямо перед удалением, в той же сессии CLI: коллега на сервере есть, адрес передать можно —
            # сверка могла устареть (коллегу удалили в WebAdmin)
            def before(cli):
                return transfer_live_problem(ctx, cli, t)
        status, text = axjobs._do_delete(ctx.job, _open_cli(ctx), t.mailbox, t.axigen, t.email,
                                         {"hold": True, "allow_loss": False}, ctx.user, _emit(ctx, t),
                                         before_remove=before)
        if status != "ok" or not to:
            return status, text
        # учётная запись удалена — это необратимо: в аудит сразу, что бы ни случилось с передачей адреса
        ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + self.key, f"{t.email}: {text}"[:300])
        ctx.data.setdefault("audited", set()).add(t.key)
        try:
            problem = transfer_problem(ctx, t, to)
            if problem:
                raise ValidationError(problem)
            added = transfer(ctx, t, to, ctx.params.get("forward_aliases", True))
        except Exception as exc:  # noqa: BLE001
            ctx.data.setdefault("item_extra", {})[t.key] = {"next_action": "ax_forward"}
            return "fail", (f"{text}; адрес коллеге НЕ передан: {getattr(exc, 'message', exc)}. Учётная запись уже "
                            f"удалена — передайте адрес действием «Передать адрес коллеге» («Повторить для "
                            f"неудачных» подставит его сам)")
        return "ok", f"{text}; письма на {', '.join(added)} теперь приходят {ctx.data['colleague']['email']}"


# ---------------------------------------------------------------------------
#  Передать адрес коллеге (после удаления учётной записи)
# ---------------------------------------------------------------------------
def colleague_row(ctx, address: str) -> Dict:
    """Учётная запись коллеги по последней сверке (существует, не удалена)."""
    from ..axigen import store as axstore
    from ..axigen.directory import valid_account_name, valid_domain
    address = (address or "").strip().lower()
    local, _sep, domain = address.partition("@")
    if not local or not domain or not valid_account_name(local) or not valid_domain(domain):
        raise ValidationError(f"Адрес коллеги указан неверно: «{address}».")
    row = axstore.get_account(ctx.db, address)
    if row is None:
        # адрес может быть псевдонимом коллеги — тогда берём его учётную запись
        for other in axstore.list_accounts(ctx.db, include_deleted=False):
            if address in (other.get("aliases") or []):
                row = other
                break
    if row is None or row.get("deleted_at"):
        raise ValidationError(f"Учётной записи {address} нет на сервере (по последней сверке).",
                              hint="Проверьте адрес или сверьте учётные записи заново.")
    return row


def transfer_addresses(t: tg.Target, colleague: str, with_aliases: bool) -> List[str]:
    """Какие адреса удалённой учётной записи станут псевдонимами коллеги (только из его домена)."""
    domain = colleague.split("@", 1)[1]
    out = [t.email] + (list((t.axigen or {}).get("aliases") or []) if with_aliases else [])
    return [a for a in out if a.split("@", 1)[-1] == domain]


def transfer_problem(ctx, t: tg.Target, colleague: str) -> str:
    """Почему адрес нельзя передать (пусто — можно)."""
    from ..axigen import store as axstore
    row = ctx.data.get("colleague") or colleague_row(ctx, colleague)
    target = row["email"]
    if target == t.email:
        return "коллега — та же учётная запись"
    if t.email.split("@", 1)[1] != target.split("@", 1)[1]:
        return f"коллега {target} в другом домене — псевдоним бывает только в своём домене"
    have = set(row.get("aliases") or [])
    busy = []
    for other in axstore.list_accounts(ctx.db, include_deleted=False):
        if other["email"] in (t.email, target):
            continue
        for address in transfer_addresses(t, target, ctx.params.get("forward_aliases", True)):
            if address == other["email"] or address in (other.get("aliases") or []):
                busy.append(f"{address} занят учётной записью {other['email']}")
    if busy:
        return "; ".join(busy[:3])
    if all(a in have for a in transfer_addresses(t, target, ctx.params.get("forward_aliases", True))):
        return "адреса уже переданы этому коллеге"
    return ""


def transfer_live_problem(ctx, cli, t: tg.Target) -> str:
    """Проверка на сервере прямо перед удалением: учётная запись коллеги есть (сверка могла устареть).

    Заодно запоминает нынешние псевдонимы удаляемой учётной записи — передаются они, а не из сверки.
    """
    from ..axigen import actions, directory
    target = ctx.data["colleague"]["email"]
    local, domain = actions.parse_email(target)
    try:
        names = directory.list_account_names(cli, domain)
    except Exception as exc:  # noqa: BLE001
        return f"не удалось проверить учётную запись коллеги {target} ({getattr(exc, 'message', exc)})"
    if not any(n.lower() == local.lower() for n in names):
        return f"учётной записи коллеги {target} на сервере нет — адрес передать некому"
    try:
        ctx.data.setdefault("live_aliases", {})[t.key] = actions.list_aliases(cli, t.email)
    except Exception:  # noqa: BLE001 — не прочитали: передаются псевдонимы из сверки
        pass
    return ""


def transfer(ctx, t: tg.Target, colleague: str, with_aliases: bool) -> List[str]:
    """Сделать адреса удалённой учётной записи псевдонимами коллеги. → какие адреса переданы.

    Каждый добавленный псевдоним — сразу в аудит и в сверку: сбой посередине не теряет сделанного.
    """
    from ..axigen import actions, directory
    from ..axigen import store as axstore
    row = ctx.data.get("colleague") or colleague_row(ctx, colleague)
    target = row["email"]
    local, domain = actions.parse_email(t.email)
    live = (ctx.data.get("live_aliases") or {}).get(t.key)
    if live is not None:
        t = tg.Target(key=t.key, email=t.email, name=t.name, host=t.host,
                      axigen=dict(t.axigen or {}, aliases=list(live)), mailbox=t.mailbox)
    added: List[str] = []
    after = list(row.get("aliases") or [])
    try:
        with _cli(ctx, f"Передача адреса {t.email} → {target}") as cli:
            # учётной записи на сервере уже нет — иначе адрес занят ею самой
            names = directory.list_account_names(cli, domain)
            if any(n.lower() == local.lower() for n in names):
                raise ValidationError(f"учётная запись {t.email} ещё есть на сервере — адрес занят ею")
            for address in transfer_addresses(t, target, with_aliases):
                if address in after:
                    continue
                after = actions.add_alias(cli, target, address.split("@", 1)[0])
                added.append(address)
                ctx.db.add_audit(ctx.user, base.HISTORY_PREFIX + "ax_forward", f"{address} → {target}"[:300])
                if ctx.job is not None:
                    ctx.job.event("INFO", f"{address} → {target}: псевдоним добавлен")
    finally:
        if added:
            axstore.update_fields(ctx.db, target, aliases=after)
            axstore.merge_info(ctx.db, t.email, {"forwarded_to": target})
            ctx.data.setdefault("audited", set()).add(t.key)
    return added


class ForwardAction(ServerAction):
    key = "ax_forward"
    label = "Передать адрес коллеге"
    group = "accounts"
    icon = "📨"
    danger = 1
    axigen_change = True
    desc = ("Для учётных записей, удалённых на сервере: их адрес (и псевдонимы) станет псевдонимом учётной записи "
            "коллеги — письма на адрес уволенного будут приходить коллеге. Только в том же домене и только если "
            "адрес не занят другой учётной записью.")
    note = ("Пока учётная запись есть на сервере, её адрес занят ею самой; для удаления с передачей есть поле "
            "«Передать адрес коллеге» в «Удалить учётную запись».")
    params = (
        Param("to", "Адрес коллеги", "text", "", placeholder="petrova@company.ru"),
        Param("with_aliases", "Передать и псевдонимы удалённой учётной записи", "bool", True),
    )

    def prepare(self, ctx):
        super().prepare(ctx)
        to = (ctx.params.get("to") or "").strip().lower()
        if not to:
            raise ValidationError("Укажите адрес коллеги.")
        ctx.params["to"] = to
        ctx.params["forward_aliases"] = bool(ctx.params.get("with_aliases", True))
        ctx.data["colleague"] = colleague_row(ctx, to)

    def base_check(self, ctx, t):
        if t.axigen is None:
            return "это не учётная запись Axigen (ящик на другом сервере)"
        if not t.deleted:
            return "учётная запись ещё есть на сервере — сначала удалите её (там же можно сразу передать адрес)"
        return ""

    def check(self, ctx, t):
        return transfer_problem(ctx, t, ctx.params["to"])

    def plan(self, ctx, t):
        return f"{', '.join(transfer_addresses(t, ctx.data['colleague']['email'], ctx.params['forward_aliases']))} " \
               f"→ {ctx.data['colleague']['email']}"

    def execute(self, ctx, t):
        added = transfer(ctx, t, ctx.params["to"], ctx.params["forward_aliases"])
        if not added:
            return "skip", "адреса уже переданы"
        return "ok", f"письма на {', '.join(added)} теперь приходят {ctx.data['colleague']['email']}"


base.register(BlockAction(), UnblockAction(), PasswordAction(), MustChangeAction(), ServicesOffAction(),
              ServicesOnAction(), QuotaAction(), AliasAddAction(), AliasRemoveAction(), ContactAction(),
              AccountTypeAction(), PublishContactAction(), AddToArchiveAction(), RefreshAction(), AxPurgeAction(),
              AxDeleteAction(), ForwardAction())
