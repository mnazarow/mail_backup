"""
Связующий слой для веб-интерфейса и заданий: проверка подключения, сессии CLI
с записью в журнал, консоль только для чтения, диагностика.
"""
from __future__ import annotations

import socket
import ssl
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional

from ..errors import ValidationError
from ..logging_setup import get_logger
from . import parse
from . import store as axstore
from .cli import AxigenCLI, has_control, quote
from .common import (AxigenCertError, AxigenError, AxigenSettings, cert_fingerprint, load_settings,
                     open_socket, pretty_fingerprint)
from .ftp import AxigenFTP, describe_tls

log = get_logger("axigen")

#: Команды консоли «только чтение»: первое слово команды. QUERY и JSONQUERY в
#: консоль не пускаются: по документации не понять, только ли читают их запросы.
CONSOLE_VERBS = ("LIST", "SHOW", "GET", "HELP", "UPDATE", "CONFIG", "ENTER", "BACK", "SEARCH", "LOOKUP")
#: Что можно открыть командой UPDATE в консоли (вход в контекст ничего не меняет).
CONSOLE_UPDATE = ("DOMAIN", "ACCOUNT", "GROUP", "LIST", "FOLDERRCPT", "ACCOUNTCLASS")
#: ENTER — только эти контексты (в AACL можно менять права администраторов).
CONSOLE_ENTER = ("QUEUE",)
MAX_CONSOLE_COMMANDS = 30


def settings(svc) -> AxigenSettings:
    return load_settings(svc)


@contextmanager
def logged_session(svc, *, username: str, kind: str, summary: str,
                   st: Optional[AxigenSettings] = None) -> Iterator[AxigenCLI]:
    """Сессия CLI, которая по завершении записывается в журнал команд Axigen."""
    st = st or load_settings(svc)
    cli: Optional[AxigenCLI] = None
    ok = False
    note = summary
    try:
        cli = AxigenCLI.open(st)
        yield cli
        ok = True
    except AxigenError as exc:
        note = f"{summary}: {exc.message}"
        raise
    finally:
        if cli is not None:
            cli.close()
            try:
                # неудачная сессия записи остаётся «записью» (её хранят дольше), прочие — «ошибкой»,
                # кроме плановых проверок: их ошибки не должны вытеснять из журнала действия людей
                axstore.add_log(svc.db, username=username,
                                kind=kind if ok or kind in ("write", "monitor") else "error",
                                summary=note, ok=ok, commands=cli.commands, transcript=cli.transcript_text())
            except Exception:  # noqa: BLE001 — журнал не должен ронять операцию
                log.debug("Не удалось записать журнал команд Axigen", exc_info=True)


# ---------------------------------------------------------------------------
#  Отпечатки сертификатов (чтобы администратор мог доверять самоподписанному)
# ---------------------------------------------------------------------------
def _grab_cert(sock: socket.socket, host: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with ctx.wrap_socket(sock, server_hostname=host or None) as tls:
        return cert_fingerprint(tls)


def cli_fingerprint(st: AxigenSettings) -> str:
    """Отпечаток сертификата SSL-слушателя CLI (только рукопожатие, без входа)."""
    sock = open_socket(st.host, st.cli_port, min(15, st.timeout_s), "CLI Axigen")
    try:
        return _grab_cert(sock, st.host)
    except (ssl.SSLError, OSError):
        return ""


def ftp_fingerprint(st: AxigenSettings) -> str:
    """Отпечаток сертификата FTP Backup: для explicit — после AUTH TLS, до входа."""
    sock = open_socket(st.host, st.ftp_port, min(15, st.timeout_s), "FTP Backup Axigen")
    try:
        if st.ftp_tls == "explicit":
            fh = sock.makefile("rb")
            line = fh.readline()
            while line[3:4] == b"-":
                line = fh.readline()
            sock.sendall(b"AUTH TLS\r\n")
            line = fh.readline()
            while line[3:4] == b"-":
                line = fh.readline()
            if not line.startswith(b"234"):
                return ""
        return _grab_cert(sock, st.host)
    except (ssl.SSLError, OSError):
        return ""
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
#  Проверка подключения
# ---------------------------------------------------------------------------
def check_connection(svc, *, username: str, ftp: bool = True) -> Dict:
    """Проверить CLI и FTP Backup: версия, лицензия, домены, очередь, корень FTP.

    Никогда не бросает исключение — всё в ответе. Пароль в ответ не попадает.
    """
    st = load_settings(svc)
    out: Dict = {"cli": {"ok": False}, "ftp": {"ok": False, "skipped": not ftp}, "changes_allowed": st.allow_changes}
    try:
        st.require()
    except AxigenError as exc:
        out["error"] = exc.message
        out["hint"] = exc.hint
        return out
    out["cli"] = _check_cli(svc, st, username)
    if ftp:
        out["ftp"] = _check_ftp(st)
    out["ok"] = bool(out["cli"].get("ok") and (out["ftp"].get("ok") or not ftp))
    return out


def _check_cli(svc, st: AxigenSettings, username: str) -> Dict:
    res: Dict = {"ok": False, "tls": st.cli_tls == "tls"}
    cli: Optional[AxigenCLI] = None
    try:
        cli = AxigenCLI.open(st, login=False)
        res["fingerprint"] = pretty_fingerprint(cli.fingerprint)
        try:
            res["version"] = cli.get_version()
        except AxigenError:
            res["version"] = ""
        cli.login()
        from .monitor import read_status
        status = read_status(cli)
        res.update(status)
        res["ok"] = True
    except AxigenCertError as exc:
        res["error"] = exc.message
        res["hint"] = exc.hint
        res["fingerprint"] = pretty_fingerprint(exc.fingerprint or cli_fingerprint(st))
        res["cert_problem"] = True
    except AxigenError as exc:
        res["error"] = exc.message
        res["hint"] = exc.hint
    finally:
        if cli is not None:
            cli.close()
            try:
                axstore.add_log(svc.db, username=username, kind="check" if res["ok"] else "error",
                                summary="Проверка подключения" + ("" if res["ok"] else f": {res.get('error', '')}"),
                                ok=res["ok"], commands=cli.commands, transcript=cli.transcript_text())
            except Exception:  # noqa: BLE001
                pass
    if not res["ok"] and st.cli_tls == "tls" and not res.get("fingerprint"):
        res["fingerprint"] = pretty_fingerprint(cli_fingerprint(st))
    return res


def _check_ftp(st: AxigenSettings) -> Dict:
    res: Dict = {"ok": False, "tls": st.ftp_tls}
    try:
        with AxigenFTP.open(st) as client:
            res["fingerprint"] = pretty_fingerprint(client.fingerprint)
            res["welcome"] = (client.welcome or "")[:200]
            res["encryption"] = describe_tls(client)
            root = client.listdir("/")
            res["root"] = [e.name + ("/" if e.is_dir else "") for e in root[:40]]
            res["mlsd"] = bool(client._mlsd)
            res["ok"] = True
    except AxigenCertError as exc:
        res["error"] = exc.message
        res["hint"] = exc.hint
        res["fingerprint"] = pretty_fingerprint(exc.fingerprint or ftp_fingerprint(st))
        res["cert_problem"] = True
    except AxigenError as exc:
        res["error"] = exc.message
        res["hint"] = exc.hint
        if st.ftp_tls != "none":
            res["fingerprint"] = pretty_fingerprint(ftp_fingerprint(st))
    return res


# ---------------------------------------------------------------------------
#  Консоль только для чтения и диагностика
# ---------------------------------------------------------------------------
def console_allowed(command: str) -> Optional[str]:
    """Почему команду нельзя выполнить в консоли (None — можно)."""
    text = (command or "").strip()
    if not text:
        return "пустая команда"
    if len(text) > 500 or has_control(text):
        return "слишком длинная команда или управляющие знаки"
    words = text.split()
    verb = words[0].upper()
    if verb not in CONSOLE_VERBS:
        return (f"команда «{words[0]}» меняет сервер или не входит в список разрешённых (в консоли можно: "
                f"{', '.join(CONSOLE_VERBS)})")
    if verb == "UPDATE" and (len(words) < 3 or words[1].upper() not in CONSOLE_UPDATE):
        return "UPDATE в консоли — только чтобы открыть домен, учётную запись, группу или список"
    if verb == "ENTER" and (len(words) != 2 or words[1].upper() not in CONSOLE_ENTER):
        return f"ENTER в консоли — только {', '.join(CONSOLE_ENTER)}"
    return None


def run_console(svc, *, username: str, commands: List[str]) -> Dict:
    """Выполнить команды чтения одной сессией. Изменения невозможны: COMMIT/SET/… не пропускаются."""
    cmds = [c.strip() for c in commands if c and c.strip()]
    if not cmds:
        raise ValidationError("Введите команду, например LIST Domains.")
    if len(cmds) > MAX_CONSOLE_COMMANDS:
        raise ValidationError(f"За раз — не больше {MAX_CONSOLE_COMMANDS} команд.")
    for cmd in cmds:
        problem = console_allowed(cmd)
        if problem:
            raise ValidationError(f"«{cmd}»: {problem}.",
                                  hint="Консоль — только для просмотра. Изменения на сервере делаются кнопками "
                                       "MailArchiver (с проверкой и журналом) или в WebAdmin Axigen.")
    replies = []
    with logged_session(svc, username=username, kind="console", summary=f"Консоль: {cmds[0]}"
                        + (f" и ещё {len(cmds) - 1}" if len(cmds) > 1 else "")) as cli:
        for cmd in cmds:
            reply = cli.run(cmd, check=False)
            replies.append({"command": reply.command, "ok": reply.ok, "status": reply.status,
                            "prompt": reply.prompt, "text": reply.text[:200_000]})
        cli.to_root()
    return {"replies": replies}


DIAGNOSTIC_STEPS = (
    ("Версия", ["GET VERSION"]),
    ("Лицензия", ["SHOW LicenseInfo"]),
    ("Домены", ["LIST Domains"]),
)


def diagnostics(svc, *, username: str, email: str = "") -> Dict:
    """Сырые ответы ключевых команд — чтобы увидеть, как сервер их пишет."""
    from .directory import list_domains
    st = load_settings(svc)
    sections = []

    def add(title: str, cli: AxigenCLI, cmd: str) -> None:
        reply = cli.run(cmd, check=False)
        sections.append({"title": title, "command": reply.command, "ok": reply.ok, "status": reply.status,
                         "text": reply.text[:50_000]})

    with logged_session(svc, username=username, kind="read", summary="Диагностика", st=st) as cli:
        for title, cmds in DIAGNOSTIC_STEPS:
            for cmd in cmds:
                add(title, cli, cmd)
        domains = [d for d in list_domains(cli) if st.domain_allowed(d)]
        domain = ""
        name = ""
        if email and "@" in email:
            name, _, domain = email.strip().rpartition("@")
            domain = domain.lower()
        elif domains:
            domain = domains[0]
        if domain:
            cli.run(f"UPDATE Domain name {quote(domain, 'имя домена')}", check=False)
            add(f"Учётные записи домена {domain}", cli, "LIST Accounts")
            if not name:
                from .directory import list_account_names
                cli.to_root()
                names = list_account_names(cli, domain)
                name = names[0] if names else ""
                cli.run(f"UPDATE Domain name {quote(domain, 'имя домена')}", check=False)
            if name:
                reply = cli.run(f"UPDATE Account name {quote(name, 'имя учётной записи')}", check=False)
                if reply.ok is not False:
                    add(f"Учётная запись {name}@{domain}", cli, "SHOW")
                    add("Папки", cli, "LIST Folders")
                    add("Псевдонимы", cli, "LIST Aliases")
                    add("Регистрационные сведения", cli, "SHOW RegistryInformation")
                    for ctx in ("ContactInfo", "Quotas", "Limits"):
                        cli.run(f"CONFIG {ctx}", check=False)
                        add(ctx, cli, "SHOW")
                        cli.run("BACK", check=False)
            cli.to_root()
            cli.run(f"UPDATE Domain name {quote(domain, 'имя домена')}", check=False)
            cli.run("CONFIG adminLimits", check=False)
            add(f"Ограничения домена {domain} (adminLimits)", cli, "SHOW")
            cli.to_root()
        cli.run("ENTER QUEUE", check=False)
        add("Очередь", cli, "LIST")
        cli.to_root()
    return {"sections": sections}


def read_queue(svc, *, username: str, limit: int = 1000) -> Dict:
    """Очередь писем сервера: таблица ``LIST`` контекста QUEUE как есть (только чтение)."""
    from .monitor import count_queue
    with logged_session(svc, username=username, kind="read", summary="Очередь писем") as cli:
        cli.run("ENTER QUEUE")
        try:
            reply = cli.run("LIST")
        finally:
            cli.to_root()
    table = parse.parse_table(reply.lines)
    rows = table.rows[:limit]
    return {"count": count_queue(reply.lines), "columns": table.titles, "keys": table.columns, "rows": rows,
            "truncated": len(table.rows) > limit, "raw": reply.text[:20000] if not table.rows else ""}
