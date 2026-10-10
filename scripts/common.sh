# shellcheck shell=bash
# ============================================================================
#  MailArchiver — общие функции install.sh и update.sh
#  Файл подключается командой source и сам по себе не запускается.
#
#  Ожидает переменные: APP_NAME APP_USER APP_GROUP INSTALL_DIR APP_DIR VENV_DIR
#  CONFIG_FILE DATA_DIR UNIT_FILE LOG_PREFIX и цвета RED GRN YLW BLU NC.
# ============================================================================

# Всё, что создают скрипты (код, окружение, unit, команда mailarchiver), должно
# читаться службой. У root бывает umask 077 (усиленные серверы) — тогда каталог
# установки получался закрытым, и служба не запускалась без внятной причины.
umask 022

# Сообщения. «|| true»: если терминал пропал (оборвалась сессия SSH), запись
# завершается ошибкой, и при set -e на ней обрывался бы сам откат.
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*" || true; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*" || true; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*" || true; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2 || true; }

# Команда от имени пользователя службы. Из корня: текущий каталог того, кто
# запустил скрипт (обычно /root/…), пользователю службы недоступен.
as_app_user(){
  if command -v runuser >/dev/null 2>&1; then
    ( trap - ERR; cd / && runuser -u "$APP_USER" -- "$@" )
  else
    # оболочка bash: printf %q записывает нелатинские пути так, что sh их не разберёт
    ( trap - ERR; cd / && su -s "${BASH:-/bin/sh}" "$APP_USER" -c "$(printf '%q ' "$@")" )
  fi
}

# Python из окружения службы. Ключ -I обязателен: скрипт запускают из каталога
# с исходниками, и без него «import mailarchiver» брал пакет из текущего
# каталога, а не установленный, — проверки и номер версии относились не к той
# установке, которая будет работать.
venv_py(){ ( trap - ERR; cd / && "$VENV_DIR/bin/python" -I "$@" ); }

installed_version(){
  venv_py -c 'from mailarchiver.version import __version__; print(__version__)' 2>/dev/null || echo '?'
}

# Старые копии: оставить N последних. По имени (в нём дата), а не по времени
# изменения: cp -a сохраняет время каталога, у копий окружения оно одинаковое,
# и «ls -t» удалял самые свежие копии вместо самых старых.
keep_newest(){
  local keep="$1" dir="$2" prefix="$3" old
  { find "$dir" -mindepth 1 -maxdepth 1 -name "$prefix*" 2>/dev/null || true; } \
    | sort -r | tail -n +"$((keep + 1))" | while IFS= read -r old; do
        rm -rf "$old"
      done || true
}

# ---------------------------------------------------------------------------
#  Права на файлы
# ---------------------------------------------------------------------------
# Код и окружение: читать и входить в каталоги может любой, менять — только root.
# Права исходников переносить нельзя. Архив, собранный там, где у файлов права
# 0600, а у каталогов 0700 (или распакованный с umask 077), давал каталог
# $APP_DIR с правами 0700 root: служба не могла войти в свой рабочий каталог,
# systemd завершал её со status=200/CHDIR, а обновление откатывалось с одной
# строкой «Служба не запустилась».
#
# Каталог данных, если он оказался внутри $APP_DIR (установка «рядом с кодом»),
# не трогаем: письма и ключи не должны стать читаемыми для всех.
normalize_app_perms(){
  find "$APP_DIR" \( -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o \
       \( ! -user root -o ! -group root \) -exec chown -h root:root {} +
  find "$APP_DIR" \( -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o \
       ! -type l -exec chmod u=rwX,go=rX {} +
  chmod 755 "$INSTALL_DIR" "$APP_DIR"
  if [ -d "$VENV_DIR" ]; then
    chmod -R a+rX "$VENV_DIR" 2>/dev/null || true
  fi
}

# Пользователь службы существует, и от его имени можно выполнять команды.
check_service_user(){
  if ! id "$APP_USER" >/dev/null 2>&1; then
    err "В системе нет пользователя службы «$APP_USER». Если служба работает от другого — задайте MA_USER."
    return 1
  fi
  if ! as_app_user true 2>/dev/null; then
    err "Не удаётся выполнить команду от имени пользователя $APP_USER (runuser/su)."
    return 1
  fi
}

# Служба должна суметь войти в каталог (по умолчанию — свой рабочий), запустить
# свой исполняемый файл, прочитать конфигурацию и писать в каталог данных.
# systemd об отказе сообщает только в журнале (status=200/CHDIR, 203/EXEC) —
# проверяем заранее и называем причину сами.
#   check_service_access            — всё, перед запуском службы;
#   check_service_access КАТАЛОГ    — только вход в каталог, конфигурация и данные
#                                     (до остановки службы, пока ничего не тронуто).
check_service_access(){
  local target="${1:-$APP_DIR}" bad=0 rest part path="" dir=""
  check_service_user || return 1
  if ! as_app_user test -x "$target" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может войти в каталог $target."
    # какой именно каталог на пути закрыт
    rest="${target#/}"
    while [ -n "$rest" ]; do
      part="${rest%%/*}"
      path="$path/$part"
      if [ "$rest" = "$part" ]; then rest=""; else rest="${rest#*/}"; fi
      if ! as_app_user test -x "$path" 2>/dev/null; then dir="$path"; break; fi
    done
    if [ -n "$dir" ]; then
      err "Закрыт каталог $dir (права $(stat -c '%A %U:%G' "$dir" 2>/dev/null || echo '?'))."
      err "Исправьте: chmod o+x \"$dir\"  — и запустите скрипт заново."
    fi
  elif [ -z "${1:-}" ] && ! as_app_user test -x "$VENV_DIR/bin/mailarchiver" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может запустить $VENV_DIR/bin/mailarchiver — недоступно окружение."
    err "Проверьте права: ls -ld \"$INSTALL_DIR\" \"$VENV_DIR\" \"$VENV_DIR/bin\""
  fi
  if [ "$bad" = "0" ] && [ -f "$CONFIG_FILE" ] && ! as_app_user test -r "$CONFIG_FILE" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может прочитать конфигурацию $CONFIG_FILE."
    err "Исправьте: chown root:$APP_GROUP \"$CONFIG_FILE\" && chmod 640 \"$CONFIG_FILE\""
  fi
  if [ "$bad" = "0" ] && [ -d "$DATA_DIR" ] && ! as_app_user test -w "$DATA_DIR" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может писать в каталог данных $DATA_DIR."
    err "Исправьте: chown $APP_USER:$APP_GROUP \"$DATA_DIR\" && chmod 750 \"$DATA_DIR\""
  fi
  return "$bad"
}

# ---------------------------------------------------------------------------
#  Проверки установленного пакета
# ---------------------------------------------------------------------------
# «import mailarchiver» проверяет один файл __init__.py. Служба же при запуске
# импортирует веб-приложение со всеми модулями — и ошибка в любом из них
# (нет библиотеки, конструкция не для этой версии Python) раньше всплывала
# только после запуска, строкой «Служба не запустилась».
check_package(){
  local pkg
  venv_py -c "import mailarchiver, fastapi, uvicorn, jinja2, yaml, apscheduler, imapclient, cryptography" \
    || { err "Приложение не импортируется: установлены не все библиотеки."; return 1; }
  pkg="$(venv_py -c 'import os, mailarchiver; print(os.path.dirname(mailarchiver.__file__))')" \
    || { err "Не удалось найти установленный пакет mailarchiver."; return 1; }
  venv_py -m compileall -q "$pkg" >/dev/null \
    || { err "Часть модулей не подходит для этой версии Python ($("$VENV_DIR/bin/python" -V 2>&1))."
         venv_py -m compileall -q "$pkg" || true
         return 1; }
}

# Пробный импорт веб-приложения — то же, с чего начинает сама служба, от её
# имени и с её конфигурацией. Базу данных это не открывает.
check_app_import(){
  if ! as_app_user env MAILARCHIVER_CONFIG="$CONFIG_FILE" "$VENV_DIR/bin/python" -I -c "import mailarchiver.web.app"; then
    err "Веб-приложение не импортируется (ошибка выше) — служба с ним не запустится."
    return 1
  fi
}

# Остатки прежней сборки пакета в каталоге кода. Без rsync (он удаляет лишнее
# сам) каталог build/ переживал обновление, и pip собирал новый пакет вместе с
# модулями прежней версии.
clean_build_leftovers(){
  rm -rf "$APP_DIR/build" "$APP_DIR"/*.egg-info 2>/dev/null || true
}

# ---------------------------------------------------------------------------
#  Служба systemd
# ---------------------------------------------------------------------------
# Unit из шаблона. Свои правки (MemoryMax и т.п.) держите в дополнении
# (systemctl edit mailarchiver): сам unit перезаписывается при обновлении.
render_unit(){
  local q='s/[\\&|]/\\&/g'      # путь может содержать & или | — для sed это служебные знаки
  sed -e "s|@VENV@|$(printf '%s' "$VENV_DIR" | sed "$q")|g" \
      -e "s|@APPDIR@|$(printf '%s' "$APP_DIR" | sed "$q")|g" \
      -e "s|@CONFIG@|$(printf '%s' "$CONFIG_FILE" | sed "$q")|g" \
      -e "s|@USER@|$(printf '%s' "$APP_USER" | sed "$q")|g" \
      -e "s|@GROUP@|$(printf '%s' "$APP_GROUP" | sed "$q")|g" \
      -e "s|@DATADIR@|$(printf '%s' "$DATA_DIR" | sed "$q")|g" \
      "$1" > "$UNIT_FILE"
  chmod 644 "$UNIT_FILE"
}

# Значение свойства службы (пусто, если эта версия systemd такого не знает).
svc_prop(){ { systemctl show "$APP_NAME" -p "$1" 2>/dev/null || true; } | sed -n "s/^$1=//p" | head -n 1; }

# Значение из конфигурации так, как его прочитает служба: db_path | health_url.
cfg_value(){
  venv_py - "$CONFIG_FILE" "$1" <<'PY' 2>/dev/null || true
import sys
from mailarchiver.config import load_config
cfg = load_config(sys.argv[1], create_dirs=False)
if sys.argv[2] == "db_path":
    print(cfg.db_path)
else:
    host = str(cfg.server.get("host") or "127.0.0.1").strip()
    port = int(cfg.server.get("port") or 8493)
    # служба слушает «все адреса» — стучимся на локальный
    if host in ("0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    print("http://%s:%d/health" % (host, port))
PY
}

# 0 — адрес отвечает кодом 200; иначе в stdout — причина одной строкой.
# Прокси из окружения не используется: на серверах, где для pip задан
# http_proxy, запрос к 127.0.0.1 уходил на прокси, и рабочая служба считалась
# неотвечающей.
http_probe(){
  venv_py - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(sys.argv[1], timeout=5) as resp:
        if resp.status == 200:
            sys.exit(0)
        print("ответ HTTP %s" % resp.status)
except Exception as exc:  # noqa: BLE001
    print(getattr(exc, "reason", None) or exc)
sys.exit(1)
PY
}
http_ok(){ http_probe "$1" >/dev/null; }

# Дождаться, пока служба поднимется и ответит на /health.
#   0 — работает;
#   1 — упала (systemd её перезапускает или сдался);
#   2 — процесс жив, но веб-интерфейс за отведённое время не ответил.
# Не «sleep 3»: первый запуск новой версии обновляет базу и на большом архиве
# длится дольше; а упавшая служба при Restart=on-failure между попытками
# числится «activating», и проверка is-active ничего не говорила о причине.
# Срок ожидания — MA_START_TIMEOUT секунд (по умолчанию 180); адрес проверки
# берётся из конфигурации, свой можно задать в MA_HEALTH_URL.
HEALTH_URL=""
wait_service_up(){
  local timeout="${1:-${MA_START_TIMEOUT:-180}}"
  local began=$SECONDS active sub restarts base stable=0 told=0 elapsed
  case "$timeout" in ''|*[!0-9]*) timeout=180 ;; esac
  HEALTH_URL="${MA_HEALTH_URL:-}"
  [ -n "$HEALTH_URL" ] || HEALTH_URL="$(cfg_value health_url)"
  base="$(svc_prop NRestarts)"
  while :; do
    active="$(svc_prop ActiveState)"; sub="$(svc_prop SubState)"; restarts="$(svc_prop NRestarts)"
    case "$active/$sub" in
      failed/*|inactive/*|*/auto-restart*) return 1 ;;
    esac
    if [ -n "$base" ] && [ -n "$restarts" ] && [ "$restarts" -gt "$base" ] 2>/dev/null; then
      return 1
    fi
    if [ "$active" = "active" ]; then
      if [ -z "$HEALTH_URL" ]; then
        # адрес узнать не удалось — считаем запущенной, если прожила заметно дольше RestartSec
        stable=$((stable + 1))
        if [ "$stable" -ge 10 ]; then return 0; fi
      elif http_ok "$HEALTH_URL"; then
        return 0
      fi
    fi
    elapsed=$((SECONDS - began))
    if [ "$elapsed" -ge "$timeout" ]; then return 2; fi
    if [ "$told" = "0" ] && [ "$elapsed" -ge 8 ]; then
      info "Служба запущена, жду ответа веб-интерфейса (до $timeout с; первый запуск новой версии обновляет базу)…"
      told=1
    fi
    sleep 1
  done
}

# Причина отказа — прямо в выводе скрипта: последние строки журнала службы и,
# если причина узнаётся, что с ней делать.
show_service_failure(){
  local out=""
  err "Состояние службы: $(svc_prop ActiveState)/$(svc_prop SubState)."
  if command -v journalctl >/dev/null 2>&1; then
    if [ -n "${SVC_STARTED_AT:-}" ]; then
      out="$(journalctl -q -u "$APP_NAME" --since "$SVC_STARTED_AT" -n 40 --no-pager 2>/dev/null || true)"
    fi
    [ -n "$out" ] || out="$(journalctl -q -u "$APP_NAME" -n 40 --no-pager 2>/dev/null || true)"
  fi
  if [ -n "$out" ]; then
    {
      echo "----- журнал службы (journalctl -u $APP_NAME), последние строки -----"
      printf '%s\n' "$out"
      echo "----------------------------------------------------------------------"
    } >&2 || true
  elif command -v journalctl >/dev/null 2>&1; then
    err "В журнале службы записей нет (journalctl -u $APP_NAME -n 50 --no-pager)."
  else
    err "journalctl не найден — причину ищите в журнале системы и в $DATA_DIR/logs."
  fi
  case "$out" in
    *"200/CHDIR"*|*"requested working directory failed"*)
      err "Причина: служба (пользователь $APP_USER) не может войти в рабочий каталог $APP_DIR."
      err "Проверьте права на него и на каталоги выше: namei -l \"$APP_DIR\"" ;;
    *"203/EXEC"*)
      err "Причина: служба не может запустить $VENV_DIR/bin/mailarchiver (файла нет или нет прав)." ;;
    *"217/USER"*)
      err "Причина: в системе нет пользователя или группы службы ($APP_USER:$APP_GROUP)." ;;
    *"226/NAMESPACE"*)
      err "Причина: systemd не смог собрать защищённое окружение службы (status=226/NAMESPACE)."
      err "Так бывает в контейнерах LXC/OpenVZ без вложенности (nesting) и когда нет каталога данных $DATA_DIR."
      err "См. docs/ru/11-troubleshooting.md, строка «status=226/NAMESPACE»." ;;
    *"ddress already in use"*)
      err "Причина: порт веб-интерфейса занят другой программой (server.port в $CONFIG_FILE)." ;;
    *"ModuleNotFoundError"*|*"ImportError"*)
      err "Причина: в окружении не хватает библиотеки Python (см. строку с ModuleNotFoundError/ImportError выше)." ;;
    *"Application startup failed"*)
      err "Причина: приложение не смогло подготовиться к работе — ошибка в строках журнала выше." ;;
  esac
}

# Запустить службу заново и дождаться ответа веб-интерфейса.
# Возвращает код wait_service_up; при отказе сам показывает причину.
start_service_checked(){
  local rc=0 url reason
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl reset-failed "$APP_NAME" 2>/dev/null || true
  # Служба остановлена, а адрес отвечает — на порту кто-то ещё (например
  # mailarchiver, запущенный вручную). Иначе его ответ приняли бы за ответ
  # службы, которая на самом деле не может занять порт.
  url="${MA_HEALTH_URL:-}"
  [ -n "$url" ] || url="$(cfg_value health_url)"
  if [ -n "$url" ] && http_ok "$url"; then
    err "Служба остановлена, но $url отвечает: порт занят другим процессом"
    err "(например mailarchiver запущен вручную). Найдите его: ss -ltnp — и остановите."
    return 1
  fi
  SVC_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
  if ! systemctl start "$APP_NAME"; then
    err "systemd не принял команду запуска службы."
    show_service_failure
    return 1
  fi
  wait_service_up "${1:-}" || rc=$?
  if [ "$rc" = "1" ]; then
    err "Служба не запустилась."
    show_service_failure
  elif [ "$rc" = "2" ]; then
    reason="$(http_probe "$HEALTH_URL" || true)"
    err "Служба запущена, но веб-интерфейс так и не ответил: $HEALTH_URL${reason:+ ($reason)}."
    err "Если база большая и первый запуск новой версии длится дольше — повторите с MA_START_TIMEOUT=900;"
    err "если служба слушает другой адрес — задайте его в MA_HEALTH_URL."
    show_service_failure
  fi
  return "$rc"
}
