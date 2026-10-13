"""
Групповые действия с ящиками, связанные с сервером Axigen (раздел «Почтовые
ящики → Групповые действия», группа «Сервер Axigen»).

Проверка (предпросмотр) работает по последней сверке с сервером и ничего не
меняет. Выполнение ставит одно задание «Действия на сервере Axigen», которое
обрабатывает ящики по одному и перед каждым изменением ещё раз читает
состояние учётной записи на сервере (см. :mod:`.jobs`).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from ..bulk import OK, BulkContext, Handler, Param
from ..errors import ValidationError
from ..models import Account, AuthType, JobType
from . import directory
from . import store as axstore
from .common import load_settings
from .jobs import archive_keeps_everything, server_row

GROUP = ("axigen", "Сервер Axigen")


class _AxigenBase(Handler):
    group = "axigen"
    #: действие меняет что-то на сервере Axigen (нужно «Разрешить изменения на сервере»)
    server_change = True
    creates_jobs = True
    action = ""

    def visible(self, svc) -> bool:
        return bool(load_settings(svc).enabled)

    def prepare(self, ctx: BulkContext) -> None:
        st = load_settings(ctx.svc)
        try:
            st.require()
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(getattr(exc, "message", str(exc)), hint=getattr(exc, "hint", None)) from exc
        if self.server_change and not st.allow_changes:
            raise ValidationError(
                "Изменения на сервере Axigen не разрешены.",
                hint="Включите «Разрешить изменения на сервере» в «Настройки → Сервер Axigen».")
        ctx.data["st"] = st
        ctx.data["rows"] = axstore.list_accounts(ctx.db)
        if not ctx.data["rows"]:
            raise ValidationError("Сверки с сервером Axigen ещё не было.",
                                  hint="Откройте раздел «Сервер Axigen» и нажмите «Сверить сейчас».")

    def row(self, ctx: BulkContext, acc: Account) -> Optional[Dict]:
        return server_row(ctx.data["rows"], acc)

    def check(self, ctx: BulkContext, acc: Account) -> str:
        st = ctx.data["st"]
        if not directory.is_axigen_mailbox(acc, st):
            return (f"ящик копируется не с сервера Axigen ({acc.host}); адрес IMAP Axigen в настройках — "
                    f"{st.imap_host or 'не задан'}")
        row = self.row(ctx, acc)
        if row is None:
            return "учётной записи с таким логином нет в последней сверке с Axigen"
        if row.get("deleted_at"):
            return "учётная запись удалена на сервере"
        if ctx.busy(acc.id, (JobType.AXIGEN_ACTION,)):
            return "по ящику уже идёт действие на сервере Axigen"
        return self.check_row(ctx, acc, row)

    def check_row(self, ctx: BulkContext, acc: Account, row: Dict) -> str:
        return ""

    def options(self, ctx: BulkContext) -> Dict:
        return dict(ctx.params)

    def apply(self, ctx: BulkContext, acc: Account):
        ctx.data.setdefault("ids", []).append(acc.id)
        return "в задании «Действия на сервере Axigen»", None

    def finish(self, ctx: BulkContext) -> None:
        ids = ctx.data.get("ids") or []
        if ctx.preview or not ids:
            return
        job_id = ctx.enqueue(JobType.AXIGEN_ACTION, None,
                             {"action": self.action, "account_ids": ids, "options": self.options(ctx),
                              "requested_by": ctx.username}, priority=4)
        for item in ctx.results:
            if item["status"] == OK:
                item["job_id"] = job_id

    def describe(self, svc) -> Dict:
        out = super().describe(svc)
        out["server_change"] = self.server_change
        return out


class AxigenUseFtpAction(_AxigenBase):
    key = "axigen_use_ftp"
    label = "Копировать через FTP Backup Axigen"
    icon = "📮"
    server_change = False
    creates_jobs = False
    desc = ("Ящики будут копироваться через службу FTP Backup сервера Axigen — входом администратора Axigen, без "
            "паролей сотрудников. Письма, уже скопированные по IMAP, второй раз не записываются. Сохранённые "
            "пароли ящиков не удаляются: они нужны для восстановления на сервер. Вернуть копирование по паролю "
            "можно тем же действием.")
    params = (
        Param("to", "Копировать", "select", AuthType.AXIGEN_FTP,
              options=((AuthType.AXIGEN_FTP, "через FTP Backup Axigen (без пароля ящика)"),
                       (AuthType.PASSWORD, "по IMAP с паролем ящика"))),
    )

    def prepare(self, ctx):
        st = load_settings(ctx.svc)
        if ctx.params.get("to") == AuthType.AXIGEN_FTP:
            try:
                st.require()
            except Exception as exc:  # noqa: BLE001
                raise ValidationError(getattr(exc, "message", str(exc)), hint=getattr(exc, "hint", None)) from exc
        ctx.data["st"] = st
        ctx.data["rows"] = axstore.list_accounts(ctx.db)

    def check(self, ctx, acc):
        st = ctx.data["st"]
        to = ctx.params.get("to")
        if acc.auth_type == to:
            return "уже так и копируется"
        if acc.auth_type == AuthType.OAUTH2:
            return "ящик с входом OAuth2 — это не Axigen"
        if to == AuthType.AXIGEN_FTP:
            if "@" not in (acc.username or ""):
                return "логин ящика — не адрес почты: каталог ящика на FTP Backup ищется по адресу"
            if not directory.is_axigen_mailbox(acc, st) and (st.imap_host or "").strip():
                return f"ящик на другом сервере ({acc.host}), а не на Axigen ({st.imap_host})"
        elif not acc.password:
            return "у ящика нет сохранённого пароля — копирование по IMAP не пойдёт"
        return ""

    def plan(self, ctx, acc):
        if ctx.params.get("to") == AuthType.AXIGEN_FTP:
            known = server_row(ctx.data["rows"], acc) is not None
            return "будет копироваться через FTP Backup" + ("" if known or not ctx.data["rows"] else
                                                            " (учётной записи нет в последней сверке — проверьте "
                                                            "логин)")
        return "будет копироваться по IMAP с сохранённым паролем"

    def apply(self, ctx, acc):
        fresh = ctx.db.get_account(acc.id) or acc
        fresh.auth_type = ctx.params.get("to")
        ctx.db.update_account(fresh, update_password=False, update_oauth_secret=False, update_oauth_token=False)
        ctx.db.set_login_status(acc.id, "", "")
        return ("копируется через FTP Backup" if fresh.auth_type == AuthType.AXIGEN_FTP
                else "копируется по IMAP"), None

    def finish(self, ctx):
        return None


class AxigenBlockAction(_AxigenBase):
    key = "axigen_block"
    label = "Заблокировать вход на сервере"
    icon = "🔒"
    danger = 1
    action = "block"
    desc = ("Закрыть учётной записи вход на сервере Axigen (POP3, IMAP, WebMail, синхронизация телефона) — "
            "например, уволенному сотруднику. Перед этим делается последняя копия ящика: не удалась — вход не "
            "закрывается. Прежний список служб запоминается, «Разблокировать вход» вернёт его.")
    note = ("Ящик, который копируется по IMAP с паролем, после блокировки копироваться не сможет — переведите его "
            "на копирование через FTP Backup.")
    params = (
        Param("final_copy", "Сначала сделать последнюю копию ящика", "bool", True),
        Param("keep_receiving", "Продолжать принимать письма", "bool", True,
              help="Письма на адрес уволенного будут приходить и копироваться в архив. Иначе отправители получат "
                   "отказ."),
        Param("random_password", "Сменить пароль на случайный", "bool", False,
              help="Чтобы никто не вошёл, даже если вход откроют. Пароль никому не показывается; если ящик "
                   "копируется по IMAP — сохраняется в его карточке."),
    )

    def check_row(self, ctx, acc, row):
        return ""

    def plan(self, ctx, acc):
        row = self.row(ctx, acc)
        parts = []
        if ctx.params.get("final_copy", True) and acc.enabled:
            parts.append("последняя копия")
        parts.append("вход уже закрыт — службы не меняются" if row.get("login_blocked") else "вход будет закрыт")
        if ctx.params.get("random_password"):
            parts.append("пароль сменится на случайный")
        if acc.auth_type == AuthType.PASSWORD and acc.enabled:
            parts.append("⚠ копирование по IMAP после этого не пройдёт")
        return ", ".join(parts)


class AxigenUnblockAction(_AxigenBase):
    key = "axigen_unblock"
    label = "Разблокировать вход на сервере"
    icon = "🔓"
    danger = 1
    action = "unblock"
    desc = "Вернуть учётной записи службы, которые были до блокировки из MailArchiver."

    def check_row(self, ctx, acc, row):
        if row.get("login_blocked") is False:
            return "вход на сервере не закрыт"
        if not row.get("saved_services"):
            return "вход закрывали не из MailArchiver — прежний список служб неизвестен (откройте вход в WebAdmin)"
        return ""

    def plan(self, ctx, acc):
        row = self.row(ctx, acc)
        return "службы станут: " + ", ".join(row.get("saved_services") or [])


class AxigenPasswordAction(_AxigenBase):
    key = "axigen_password"
    label = "Сменить пароль на сервере на случайный"
    icon = "🔑"
    danger = 1
    action = "password"
    desc = ("Задать учётной записи на сервере новый случайный пароль. Никому не показывается: если ящик копируется "
            "по IMAP — сохраняется в его карточке (копирование продолжится), иначе нигде не хранится. Задать "
            "конкретный пароль одной учётной записи можно в разделе «Сервер Axigen».")

    def plan(self, ctx, acc):
        if acc.auth_type == AuthType.PASSWORD or acc.password:
            return "пароль сменится; новый будет сохранён в карточке ящика"
        return "пароль сменится; новый нигде не сохраняется"


class AxigenPurgeAction(_AxigenBase):
    key = "axigen_purge"
    label = "Удалить старые письма с сервера"
    icon = "🧹"
    danger = 2
    action = "purge"
    desc = ("Освободить место на почтовом сервере: удалить в папках ящика письма старше заданного срока — только "
            "если каждое письмо папки уже в архиве. Перед удалением ящик копируется заново, а по каждой папке "
            "сверяется список писем на сервере с архивом; хоть одного нет — папка не очищается. Только для ящиков, "
            "которые копируются через FTP Backup и хранят в архиве всё (срок хранения «хранить всё» или "
            "бессрочное удержание).")
    note = ("Удалённое с сервера возвращается только из архива («Восстановить на сервер»). Очищаются папки, которые "
            "разрешены домену (WebAdmin → домен → Admin Limits → Purge folders), если не указаны явно.")
    params = (
        Param("days", "Удалить письма старше, дней", "int", 365, min=30, max=36500),
        Param("folders", "Папки", "text", "", placeholder="пусто — все, которые домену разрешено очищать",
              help="Через запятую, как на сервере: INBOX, Sent Items. Папку, которую домену очищать не разрешено, "
                   "сервер не очистит."),
    )

    def check_row(self, ctx, acc, row):
        if acc.auth_type != AuthType.AXIGEN_FTP:
            return "ящик копируется не через FTP Backup — полноту архива по папкам не проверить"
        keeps, why = archive_keeps_everything(ctx.svc, acc)
        if not keeps:
            return why
        if not acc.enabled:
            return "копирование ящика выключено"
        return ""

    def plan(self, ctx, acc):
        folders = ctx.params.get("folders") or "разрешённые домену папки"
        return f"свежая копия, сверка папок, затем удаление писем старше {ctx.params.get('days')} дн.: {folders}"


class AxigenDeleteAction(_AxigenBase):
    key = "axigen_delete"
    label = "Удалить учётную запись на сервере"
    icon = "🗑️"
    danger = 2
    action = "delete"
    desc = ("Удалить учётную запись с сервера Axigen, сохранив её письма в архиве. Только если вход уже "
            "заблокирован. На время удаления останавливается приём писем, архив ставится на бессрочное удержание, "
            "ящик копируется ещё раз, и все папки на сервере сверяются с архивом: хоть одного письма нет в архиве "
            "(папка исключена из копирования, письмо больше лимита размера, папка не читается) — учётная запись не "
            "удаляется, приём писем и удержание возвращаются. Копирование ящика выключается.")
    note = "Удаление на сервере необратимо: вернуть письма можно только из архива, создав учётную запись заново."
    params = (
        Param("hold", "Поставить архив ящика на бессрочное удержание", "bool", True,
              help="Ставится до последней копии: со сроком хранения копия не скачала бы старые письма."),
        Param("allow_loss", "Удалить, даже если часть писем не в архиве", "bool", False,
              help="Например, папка «Спам» исключена из копирования, а её письма не нужны. Без этой отметки "
                   "учётная запись удаляется, только если в архиве есть каждое письмо ящика."),
    )

    def check_row(self, ctx, acc, row):
        if not row.get("login_blocked"):
            return "вход на сервере не заблокирован — сначала «Заблокировать вход на сервере»"
        if acc.auth_type != AuthType.AXIGEN_FTP and not ctx.params.get("allow_loss"):
            return ("ящик копируется по IMAP — полноту архива не проверить; переведите его на копирование через "
                    "FTP Backup")
        if acc.auth_type == AuthType.AXIGEN_FTP and not ctx.params.get("hold", True):
            keeps, why = archive_keeps_everything(ctx.svc, acc)
            if not keeps and not ctx.params.get("allow_loss"):
                return why
        return ""

    def plan(self, ctx, acc):
        stats = ctx.totals.get(acc.id) or {}
        tail = f"; в архиве писем {int(stats.get('messages', 0))}"
        if acc.auth_type == AuthType.AXIGEN_FTP:
            return "приём писем остановится, удержание архива, последняя копия, сверка всех папок, удаление" + tail
        return ("удаление без проверки полноты архива (ящик копируется по IMAP; нужна удачная копия незадолго до "
                "блокировки)") + tail


HANDLERS: List[Handler] = [AxigenUseFtpAction(), AxigenBlockAction(), AxigenUnblockAction(), AxigenPasswordAction(),
                           AxigenPurgeAction(), AxigenDeleteAction()]


def register(registry: Dict[str, Handler], groups: list) -> None:
    for handler in HANDLERS:
        registry[handler.key] = handler
    if GROUP not in groups:
        groups.insert(len(groups) - 1, GROUP)       # перед «Удаление»
