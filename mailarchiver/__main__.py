"""
Точка входа и командная строка MailArchiver.

Использование::

    mailarchiver serve                 # запустить веб-сервис
    mailarchiver create-admin          # создать администратора
    mailarchiver reset-2fa -u NAME     # отключить 2FA пользователя (потерян телефон)
    mailarchiver storage-key           # состояние шифрования копии (--generate ПУТЬ — новый ключ)
    mailarchiver reset-password -u имя # сбросить пароль пользователя
    mailarchiver replica-pull          # скачать копию вне сервера (после аварии)
    mailarchiver restore-snapshot F    # восстановить базу из снимка
    mailarchiver replica-ssh-key       # SSH-ключ службы для копии на сервер по rsync
    mailarchiver check-config          # проверить конфигурацию
    mailarchiver version
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import load_config
from .errors import MailArchiverError
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
    # proxy_headers=False намеренно: разбор X-Forwarded-For делает собственный
    # ProxyHeadersMiddleware (mailarchiver/web/proxy.py). uvicorn брал ЛЕВОЕ
    # значение цепочки — целиком подконтрольное клиенту, из-за чего защита от
    # подбора пароля обходилась подстановкой заголовка.
    uvicorn.run("mailarchiver.web.app:app", host=host, port=port, workers=1,
                log_level=str(cfg.logging_cfg.get("level", "info")).lower(),
                proxy_headers=False, forwarded_allow_ips=None)
    return 0


def _password_from_args(args, prompt: str) -> str:
    """Пароль из --password-file/--password-stdin/-p или интерактивно.

    Аргумент командной строки виден в ``ps`` всем пользователям машины, поэтому
    в скриптах и контейнерах пароль следует передавать файлом или через stdin.
    """
    path = getattr(args, "password_file", None)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip("\r\n")
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().strip("\r\n")
    if getattr(args, "password", None):
        return args.password
    return getpass.getpass(prompt)


def cmd_create_admin(args) -> int:
    from .security import hash_password, check_password_policy
    cfg = load_config(args.config)
    db = _build_db(cfg)
    username = args.username or input("Имя администратора [admin]: ").strip() or "admin"
    if db.get_user_by_name(username):
        print(f"Пользователь «{username}» уже существует.", file=sys.stderr)
        return 1
    password = _password_from_args(args, "Пароль: ")
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
    password = _password_from_args(args, "Новый пароль: ")
    err = check_password_policy(password, int(cfg.security.get("min_password_length", 8)))
    if err:
        print(err, file=sys.stderr)
        return 1
    db.set_user_password(user["id"], hash_password(password))
    print(f"Пароль пользователя «{args.username}» изменён.")
    return 0


def cmd_reset_2fa(args) -> int:
    """Отключить двухфакторный вход пользователя (потерян телефон)."""
    cfg = load_config(args.config)
    db = _build_db(cfg)
    user = db.get_user_by_name(args.username)
    if not user:
        print(f"Пользователь «{args.username}» не найден.", file=sys.stderr)
        return 1
    if not user["totp_enabled"]:
        # Ничего не меняем и сеансы не трогаем: 2FA и так выключена
        # (частая ошибка — опечатка в имени соседней учётной записи).
        db.disable_totp(user["id"])  # сбросить недонастроенный секрет, если был
        print(f"У пользователя «{user['username']}» двухфакторный вход не был включён — "
              f"для входа хватает пароля.")
        return 0
    db.disable_totp(user["id"])
    db.delete_user_sessions(user["id"])
    db.add_audit("cli", "2fa_reset", user["username"])
    print(f"Двухфакторный вход пользователя «{user['username']}» отключён, его сеансы завершены.")
    print("При следующем входе хватит пароля; включите 2FA заново в профиле.")
    return 0


def cmd_storage_key(args) -> int:
    """Ключ шифрования локальной копии: создать или показать состояние.

    Просмотр состояния ТОЛЬКО читает: ни ключ, ни каталоги, ни файлы базы не
    создаются (раньше команда, запущенная через sudo, создавала ключ
    root:root 0600, который служба потом не могла прочитать).
    """
    from .storage import crypto
    cfg = load_config(args.config)
    if args.generate:
        path = os.path.abspath(args.generate)
        parent = os.path.dirname(path)
        parent_existed = os.path.isdir(parent)
        try:
            key = crypto.generate_key_file(path)
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось создать ключ: {exc}", file=sys.stderr)
            return 1
        print(f"Ключ создан: {path} (отпечаток {crypto.key_id_of(key).hex()}).")
        print("Дальше:")
        if not parent_existed:
            print(f"  1) chown root:mailarchiver {parent} {path} && chmod 750 {parent} && chmod 640 {path}")
        else:
            print(f"  1) chown root:mailarchiver {path} && chmod 640 {path}")
        print(f"  2) в config.yaml: storage.encryption_key_file: \"{path}\" и storage.encrypt: true")
        print("  3) СКОПИРУЙТЕ ключ в надёжное место отдельно от каталога данных.")
        return 0
    import json
    import sqlite3
    settings, meta, counts = {}, {}, {"total": 0, "encrypted": 0}
    if os.path.exists(cfg.db_path):
        try:
            conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=5)
            try:
                for key, value in conn.execute(
                        "SELECT key, value FROM settings WHERE key LIKE 'storage.%'"):
                    try:
                        settings[key] = json.loads(value)
                    except (TypeError, ValueError):
                        settings[key] = value
                for key, value in conn.execute("SELECT key, value FROM meta"):
                    meta[key] = value
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(CASE WHEN stored_path LIKE '%.enc' "
                                   "THEN 1 ELSE 0 END), 0) FROM messages").fetchone()
                counts = {"total": int(row[0] or 0), "encrypted": int(row[1] or 0)}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"База данных не читается ({exc}) — показываю только файл ключа.", file=sys.stderr)

    def opt(key: str):
        return settings.get(f"storage.{key}", cfg.get("storage", key))

    raw_path = str(opt("encryption_key_file") or "").strip()
    path = os.path.abspath(raw_path) if raw_path else os.path.join(cfg.data_dir, "storage.key")
    inside = path.startswith(os.path.abspath(cfg.data_dir) + os.sep)
    key_id, error = None, None
    if os.path.exists(path):
        try:
            key_id = crypto.key_id_of(crypto.load_key_file(path)).hex()
        except Exception as exc:  # noqa: BLE001
            error = f"Файл ключа не читается: {getattr(exc, 'message', exc)}"
    elif bool(opt("encrypt")) or meta.get("storage_key_id"):
        error = f"Файл ключа не найден: {path}"
    recorded = meta.get("storage_key_id")
    if key_id and recorded and key_id != recorded:
        error = f"Ключ {path} не тот, которым шифровался архив (ожидался отпечаток {recorded})."
    print(f"Файл ключа:          {path}" + ("  (внутри каталога данных!)" if inside else ""))
    print(f"Отпечаток ключа:     {key_id or '—'}")
    print(f"Ожидаемый отпечаток: {recorded or '— (архив ещё не шифровался)'}")
    print(f"Шифрование новых:    {'включено' if opt('encrypt') else 'выключено'} (в настройках)")
    print(f"Писем зашифровано:   {counts['encrypted']} из {counts['total']}")
    for warning in crypto.key_file_warnings(path):
        print(f"ВНИМАНИЕ: {warning}", file=sys.stderr)
    if error:
        print(f"ОШИБКА: {error}", file=sys.stderr)
        return 1
    return 0


def _replica_values(cfg, args) -> dict:
    """Настройки копии для командной строки: config.yaml → база (если есть) → ключи команды."""
    import json
    import sqlite3
    from .config import DEFAULTS
    values = {key: cfg.get("replica", key) for key in DEFAULTS["replica"]}
    if os.path.exists(cfg.db_path):
        try:
            conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=5)
            try:
                for key, value in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'replica.%'"):
                    try:
                        values[key.split(".", 1)[1]] = json.loads(value)
                    except (TypeError, ValueError):
                        values[key.split(".", 1)[1]] = value
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        secret = values.get("s3_secret_key")
        if secret and os.path.exists(cfg.secret_key_path):
            from .security import SecretBox
            try:
                values["s3_secret_key"] = SecretBox(cfg.secret_key()).decrypt(secret)
            except Exception:  # noqa: BLE001 - значение могло быть сохранено открытым текстом
                pass
    if getattr(args, "dir", None):
        values.update(target="dir", dir_path=args.dir)
    if getattr(args, "s3_endpoint", None):
        values.update(target="s3", s3_endpoint=args.s3_endpoint)
    for name in ("s3_bucket", "s3_prefix", "s3_region", "s3_access_key"):
        if getattr(args, name, None) is not None:
            values[name] = getattr(args, name)
    if getattr(args, "s3_secret_key_file", None):
        with open(args.s3_secret_key_file, "r", encoding="utf-8") as fh:
            values["s3_secret_key"] = fh.read().strip()
    if getattr(args, "s3_virtual_host", False):
        values["s3_path_style"] = False
    return values


def cmd_replica_pull(args) -> int:
    """Скачать копию вне сервера в каталог данных (восстановление после аварии)."""
    from types import SimpleNamespace
    from .replica.runner import build_target, pull
    cfg = load_config(args.config, create_dirs=False)
    values = _replica_values(cfg, args)
    dest = os.path.abspath(args.to or cfg.data_dir)
    target = build_target(SimpleNamespace(cfg=cfg), values)
    print(f"Копия: {target.describe()}")
    print(f"Куда:  {dest}" + (" (только снимки базы)" if args.only_db else ""))
    try:
        res = pull(target, dest, include_mail=not args.only_db)
    finally:
        target.close()
    from .util import human_size
    print(f"Готово: скачано файлов {res['files']} ({human_size(res['bytes'])}), уже были на месте {res['skipped']}.")
    marker = res.get("marker") or {}
    if marker.get("host"):
        print(f"Копия сделана на сервере {marker.get('host')}.")
    print("Дальше: sudo mailarchiver restore-snapshot <самый свежий файл из snapshots/>, затем верните "
          "secret.key (и ключ шифрования писем) и запустите службу.")
    return 0


def cmd_restore_snapshot(args) -> int:
    """Развернуть снимок базы в каталог данных."""
    import time as _time
    from .replica import snapshots
    from .storage import crypto
    cfg = load_config(args.config, create_dirs=False)
    path = args.snapshot
    if os.path.sep not in path and not os.path.exists(path):
        candidate = os.path.join(snapshots.snapshot_dir(cfg), path)
        if os.path.exists(candidate):
            path = candidate
    path = os.path.abspath(path)
    cipher = None
    if path.endswith(".enc"):
        key_path = args.key
        if not key_path:
            raw = str(cfg.get("storage", "encryption_key_file") or "").strip()
            key_path = os.path.abspath(raw) if raw else os.path.join(cfg.data_dir, "storage.key")
        cipher = crypto.StorageCipher(crypto.load_key_file(key_path))
        print(f"Снимок зашифрован; ключ: {key_path} (отпечаток {cipher.key_id_hex}).")
    dest = cfg.db_path
    if os.path.exists(dest):
        if not args.force:
            print(f"База уже есть: {dest}. Остановите службу (sudo systemctl stop mailarchiver) и повторите "
                  f"с --force — текущая база будет сохранена рядом.", file=sys.stderr)
            return 1
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(dest + suffix):
                os.replace(dest + suffix, f"{dest}.before-restore-{stamp}{suffix}")
        print(f"Текущая база сохранена как {dest}.before-restore-{stamp}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    size = snapshots.restore_snapshot(path, dest, cipher)
    from .util import human_size
    print(f"База восстановлена из {os.path.basename(path)}: {dest} ({human_size(size)}).")
    print("Проверьте, что на месте secret.key (пароли ящиков) и ключ шифрования писем, затем запустите службу.")
    return 0


def cmd_replica_ssh_key(args) -> int:
    """Показать (и при необходимости создать) SSH-ключ службы для копии по rsync."""
    from types import SimpleNamespace
    from .replica.runner import generate_ssh_key
    cfg = load_config(args.config)
    key = generate_ssh_key(SimpleNamespace(cfg=cfg))
    print(key)
    print("Добавьте эту строку в ~/.ssh/authorized_keys пользователя на сервере-получателе.", file=sys.stderr)
    return 0


def cmd_check_config(args) -> int:
    try:
        # create_dirs=False: проверка ничего не создаёт и не меняет права каталогов
        cfg = load_config(args.config, create_dirs=False)
    except Exception as exc:  # noqa: BLE001
        print(f"ОШИБКА конфигурации: {getattr(exc, 'message', exc)}", file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"Подсказка: {hint}", file=sys.stderr)
        return 1
    print(f"{APP_TITLE} {__version__}")
    print(f"Файл конфигурации : {cfg.source_path or '(значения по умолчанию)'}")
    print(f"Каталог данных    : {cfg.data_dir}")
    print(f"База данных        : {cfg.db_path}")
    print(f"Каталог копий      : {cfg.mail_root}")
    print(f"Веб-интерфейс      : http://{cfg.server['host']}:{cfg.server['port']}")
    for warning in cfg.warnings:
        print(f"Предупреждение: {warning}")
    print("Конфигурация корректна ✓" + (" (есть предупреждения)" if cfg.warnings else ""))
    return 0


def cmd_version(args) -> int:
    print(f"{APP_TITLE} {__version__}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mailarchiver", description=APP_TITLE)
    parser.add_argument("--config", "-c", help="путь к config.yaml")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="запустить веб-сервис")
    p.add_argument("--host"); p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("create-admin", help="создать администратора")
    p.add_argument("-u", "--username"); p.add_argument("-p", "--password")
    p.add_argument("--password-file", help="файл с паролем (безопаснее, чем -p: аргументы видны в ps)")
    p.add_argument("--password-stdin", action="store_true", help="прочитать пароль из стандартного ввода")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("reset-password", help="сбросить пароль пользователя")
    p.add_argument("-u", "--username", required=True); p.add_argument("-p", "--password")
    p.add_argument("--password-file", help="файл с паролем")
    p.add_argument("--password-stdin", action="store_true", help="прочитать пароль из стандартного ввода")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("reset-2fa", help="отключить двухфакторный вход пользователя (потерян телефон)")
    p.add_argument("-u", "--username", required=True)
    p.set_defaults(func=cmd_reset_2fa)

    p = sub.add_parser("storage-key", help="ключ шифрования локальной копии: состояние или создание")
    p.add_argument("--generate", metavar="ПУТЬ", help="создать новый ключ в указанном файле (не перезаписывает)")
    p.set_defaults(func=cmd_storage_key)

    p = sub.add_parser("replica-pull", help="скачать копию вне сервера (восстановление после аварии)")
    p.add_argument("--to", metavar="КАТАЛОГ", help="куда скачать (по умолчанию — каталог данных)")
    p.add_argument("--only-db", action="store_true", help="только снимки базы")
    p.add_argument("--dir", metavar="ПАПКА", help="копия в сетевой папке (если настроек нет в базе)")
    p.add_argument("--s3-endpoint", metavar="URL", help="копия в S3: адрес хранилища")
    p.add_argument("--s3-bucket"); p.add_argument("--s3-prefix"); p.add_argument("--s3-region")
    p.add_argument("--s3-access-key")
    p.add_argument("--s3-secret-key-file", metavar="ФАЙЛ", help="файл с секретным ключом S3")
    p.add_argument("--s3-virtual-host", action="store_true", help="адресация бакета в имени сервера (AWS)")
    p.set_defaults(func=cmd_replica_pull)

    p = sub.add_parser("restore-snapshot", help="восстановить базу из снимка")
    p.add_argument("snapshot", metavar="СНИМОК", help="файл снимка (mailarchiver-….db.gz[.enc])")
    p.add_argument("--key", metavar="ФАЙЛ", help="ключ шифрования (для зашифрованного снимка)")
    p.add_argument("--force", action="store_true", help="заменить существующую базу (она сохранится рядом)")
    p.set_defaults(func=cmd_restore_snapshot)

    p = sub.add_parser("replica-ssh-key", help="SSH-ключ службы для копии по rsync (создаётся при первом вызове)")
    p.set_defaults(func=cmd_replica_ssh_key)

    p = sub.add_parser("check-config", help="проверить конфигурацию")
    p.set_defaults(func=cmd_check_config)

    p = sub.add_parser("version", help="показать версию")
    p.set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_serve
        args.host = None
        args.port = None
    try:
        return args.func(args)
    except MailArchiverError as exc:
        # понятное сообщение вместо трассировки (неверный путь, конфиг и т. п.)
        print(f"ОШИБКА: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"Подсказка: {exc.hint}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ОШИБКА: {exc.strerror or exc}: {exc.filename or ''}".rstrip(": "), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
