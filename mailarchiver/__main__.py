"""
Точка входа и командная строка MailArchiver.

Использование::

    mailarchiver serve                 # запустить веб-сервис
    mailarchiver create-admin          # создать администратора
    mailarchiver reset-password -u имя # сбросить пароль пользователя
    mailarchiver check-config          # проверить конфигурацию
    mailarchiver version
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import load_config
from .version import __version__, APP_TITLE


def _build_db(cfg):
    from .security import SecretBox
    from .database import Database
    box = SecretBox(cfg.secret_key())
    db = Database(cfg.db_path, box, busy_timeout_ms=int(cfg.get("database", "busy_timeout_ms", 10000)),
                  wal=bool(cfg.get("database", "wal", True)))
    db.init_schema()
    return db


def cmd_serve(args) -> int:
    import uvicorn
    cfg = load_config(args.config)
    if args.config:
        os.environ["MAILARCHIVER_CONFIG"] = args.config
    host = args.host or cfg.server.get("host", "127.0.0.1")
    port = args.port or int(cfg.server.get("port", 8493))
    print(f"{APP_TITLE} {__version__}")
    print(f"Запуск веб-сервиса на http://{host}:{port}  (данные: {cfg.data_dir})")
    # workers=1 обязательно: планировщик и очередь должны быть в одном процессе
    uvicorn.run("mailarchiver.web.app:app", host=host, port=port, workers=1,
                log_level=str(cfg.logging_cfg.get("level", "info")).lower(),
                proxy_headers=bool(cfg.server.get("behind_proxy", False)),
                forwarded_allow_ips="*" if cfg.server.get("behind_proxy") else None)
    return 0


def cmd_create_admin(args) -> int:
    from .security import hash_password, check_password_policy
    cfg = load_config(args.config)
    db = _build_db(cfg)
    username = args.username or input("Имя администратора [admin]: ").strip() or "admin"
    if db.get_user_by_name(username):
        print(f"Пользователь «{username}» уже существует.", file=sys.stderr)
        return 1
    password = args.password or getpass.getpass("Пароль: ")
    err = check_password_policy(password, int(cfg.security.get("min_password_length", 8)))
    if err:
        print(err, file=sys.stderr)
        return 1
    uid = db.create_user(username, hash_password(password), role="admin")
    print(f"Администратор «{username}» создан (id={uid}).")
    return 0


def cmd_reset_password(args) -> int:
    from .security import hash_password, check_password_policy
    cfg = load_config(args.config)
    db = _build_db(cfg)
    user = db.get_user_by_name(args.username)
    if not user:
        print(f"Пользователь «{args.username}» не найден.", file=sys.stderr)
        return 1
    password = args.password or getpass.getpass("Новый пароль: ")
    err = check_password_policy(password, int(cfg.security.get("min_password_length", 8)))
    if err:
        print(err, file=sys.stderr)
        return 1
    db.set_user_password(user["id"], hash_password(password))
    print(f"Пароль пользователя «{args.username}» изменён.")
    return 0


def cmd_check_config(args) -> int:
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА конфигурации: {exc}", file=sys.stderr)
        return 1
    print(f"{APP_TITLE} {__version__}")
    print(f"Файл конфигурации : {cfg.source_path or '(значения по умолчанию)'}")
    print(f"Каталог данных    : {cfg.data_dir}")
    print(f"База данных        : {cfg.db_path}")
    print(f"Каталог копий      : {cfg.mail_root}")
    print(f"Веб-интерфейс      : http://{cfg.server['host']}:{cfg.server['port']}")
    print("Конфигурация корректна ✓")
    return 0


def cmd_version(args) -> int:
    print(f"{APP_TITLE} {__version__}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mailarchiver", description=f"{APP_TITLE} — резервное копирование почты по IMAP")
    parser.add_argument("--config", "-c", help="путь к config.yaml")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="запустить веб-сервис")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("create-admin", help="создать администратора")
    p.add_argument("-u", "--username"); p.add_argument("-p", "--password")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("reset-password", help="сбросить пароль пользователя")
    p.add_argument("-u", "--username", required=True); p.add_argument("-p", "--password")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("check-config", help="проверить конфигурацию")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("version", help="показать версию")
    p.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_serve
        args.host = None
        args.port = None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
