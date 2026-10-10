#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт обновления
#  Делает резервную копию текущей установки, заменяет код, обновляет
#  зависимости и перезапускает службу. При ошибке — автоматический откат к
#  предыдущей версии, с показом причины. Письма и конфигурация НЕ трогаются;
#  с базы данных перед первым запуском новой версии снимается копия, и при
#  откате база возвращается к ней.
#
#  Запуск (из каталога с НОВЫМИ исходниками):  sudo bash scripts/update.sh
#
#  Переменные окружения (все необязательны):
#    MA_START_TIMEOUT   сколько секунд ждать ответа веб-интерфейса (180)
#    MA_HEALTH_URL      свой адрес проверки вместо взятого из конфигурации
#    MA_SKIP_DB_BACKUP  1 — не копировать базу перед обновлением
#    MA_KEEP_DB_BACKUP  1 — не удалять копию базы после успешного обновления
#    MA_INSTALL_DIR, MA_CONFIG_DIR, MA_DATA_DIR, MA_USER, MA_GROUP — если не
#                       заданы, берутся из действующего unit-файла службы
#    MA_UNIT_FILE, MA_CLI_WRAPPER — пути unit-файла и команды mailarchiver
# ============================================================================
set -Eeuo pipefail

APP_NAME="mailarchiver"
UNIT_FILE="${MA_UNIT_FILE:-/etc/systemd/system/$APP_NAME.service}"
CLI_WRAPPER="${MA_CLI_WRAPPER:-/usr/local/bin/mailarchiver}"
LOG_PREFIX="[MailArchiver update]"

# Каталоги и пользователь службы. Если установка делалась с MA_* (другой
# каталог, другой пользователь), при обновлении их приходилось повторять —
# иначе unit переписывался со значениями по умолчанию, и служба не запускалась.
# Теперь значения берутся из действующего unit-файла.
unit_val(){ [ -f "$UNIT_FILE" ] || return 0; sed -n "s/^$1=//p" "$UNIT_FILE" | head -n 1; }
_unit_wd="$(unit_val WorkingDirectory)"
_unit_cfg="$(unit_val 'Environment=MAILARCHIVER_CONFIG')"
_unit_data="$(unit_val ReadWritePaths)"; _unit_data="${_unit_data%% *}"

INSTALL_DIR="${MA_INSTALL_DIR:-}"
if [ -z "$INSTALL_DIR" ]; then
  case "$_unit_wd" in */app) INSTALL_DIR="${_unit_wd%/app}" ;; *) INSTALL_DIR="/opt/mailarchiver" ;; esac
fi
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
BACKUP_DIR="$INSTALL_DIR/backups"
if [ -n "${MA_CONFIG_DIR:-}" ]; then CONFIG_FILE="$MA_CONFIG_DIR/config.yaml"
elif [ -n "$_unit_cfg" ]; then CONFIG_FILE="$_unit_cfg"
else CONFIG_FILE="/etc/mailarchiver/config.yaml"; fi
APP_USER="${MA_USER:-$(unit_val User)}";   APP_USER="${APP_USER:-mailarchiver}"
APP_GROUP="${MA_GROUP:-$(unit_val Group)}"; APP_GROUP="${APP_GROUP:-mailarchiver}"
DATA_DIR="${MA_DATA_DIR:-$_unit_data}";    DATA_DIR="${DATA_DIR:-/var/lib/mailarchiver}"

if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2 || true; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/common.sh" ]; then
  err "Не найден $SCRIPT_DIR/common.sh — запускайте скрипт из распакованного архива целиком."
  exit 1
fi
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/common.sh"       # info / ok / warn / err и общие с install.sh функции

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/app_$STAMP"
VENV_BACKUP_PATH="$BACKUP_DIR/venv_$STAMP"
DB_BACKUP_PATH="$BACKUP_DIR/db_$STAMP"
DB_PATH=""            # файл базы (узнаём у новой версии перед её первым запуском)
FINISHED=0            # обновление доведено до конца — откат не нужен
TOUCHED=0             # служба уже остановлена: дальше отказ означает откат
ROLLED_BACK=0
WAS_ACTIVE=0          # работала ли служба до обновления (при сбое — вернуть её в строй)
NEW_STARTED=0         # запускалась ли новая версия (значит, могла изменить базу)

# Вернуть базу из копии. Сначала копия кладётся рядом под временными именами и
# только потом встаёт на место: оборванное копирование не должно оставить
# вместо базы обрезанный файл (пустой файл SQLite — это «чистая» база, и
# прежняя версия поднялась бы на ней как только что установленная).
restore_database(){
  local dir name suffix failed=0
  dir="$(dirname "$DB_PATH")"; name="$(basename "$DB_PATH")"
  rm -f "$dir"/.ma-restore.* 2>/dev/null
  for suffix in "" -wal -shm -journal; do
    [ -f "$DB_BACKUP_PATH/$name$suffix" ] || continue
    cp -a "$DB_BACKUP_PATH/$name$suffix" "$dir/.ma-restore.$name$suffix" || failed=1
  done
  if [ "$failed" = "1" ] || [ ! -f "$dir/.ma-restore.$name" ]; then
    rm -f "$dir"/.ma-restore.* 2>/dev/null
    return 1
  fi
  # журналы новой версии к прежней базе не относятся
  rm -f "$DB_PATH-wal" "$DB_PATH-shm" "$DB_PATH-journal"
  for suffix in "" -wal -shm -journal; do
    [ -f "$dir/.ma-restore.$name$suffix" ] || continue
    mv -f "$dir/.ma-restore.$name$suffix" "$DB_PATH$suffix" || return 1
  done
}

# Откат возвращает и исходники, и venv. Пакет ставится НЕ в editable-режиме,
# то есть рабочий код лежит в $VENV_DIR/lib/*/site-packages/mailarchiver, а не
# в $APP_DIR — возврат одного только $APP_DIR ничего не откатывал: служба
# продолжала работать на новом коде и новых библиотеках, хотя скрипт
# рапортовал «Откат выполнен».
rollback(){
  local ec="${1:-1}" db_failed=0 back_rc=0
  err "Обновление прервано (код $ec)."
  if [ "$ROLLED_BACK" = "0" ] && [ -f "$BACKUP_PATH/.complete" ]; then
    warn "Откат к предыдущей версии…"
    ROLLED_BACK=1
    # Сначала остановить: новая версия могла остаться в цикле перезапусков, а
    # базу под работающей службой подменять нельзя.
    systemctl stop "$APP_NAME" 2>/dev/null || true
    rm -rf "$APP_DIR"
    mv "$BACKUP_PATH" "$APP_DIR"
    rm -f "$APP_DIR/.complete"
    if [ -d "$VENV_BACKUP_PATH" ]; then
      rm -rf "$VENV_DIR"
      mv "$VENV_BACKUP_PATH" "$VENV_DIR"
      warn "Виртуальное окружение восстановлено из резервной копии."
    else
      # venv не забэкапился — переустанавливаем прежний код поверх
      warn "Резервной копии venv нет: переустанавливаю прежнюю версию пакета."
      "$VENV_DIR/bin/pip" install -q --no-deps --force-reinstall "$APP_DIR" 2>/dev/null || \
        err "Не удалось переустановить прежнюю версию — проверьте службу вручную."
    fi
    if [ -f "$BACKUP_DIR/unit_$STAMP.service" ]; then
      cp -a "$BACKUP_DIR/unit_$STAMP.service" "$UNIT_FILE" 2>/dev/null || true
      systemctl daemon-reload 2>/dev/null || true
    fi
    # База: новая версия при запуске обновляет её схему. Если она успела
    # запуститься, прежнему коду возвращаем базу, с которой он работал.
    if [ -f "$DB_BACKUP_PATH/.complete" ] && [ -n "$DB_PATH" ]; then
      if [ "$NEW_STARTED" != "1" ]; then
        rm -rf "$DB_BACKUP_PATH"      # новая версия не запускалась — база не менялась
      elif restore_database; then
        warn "База данных возвращена к состоянию до обновления."
        rm -rf "$DB_BACKUP_PATH"
      else
        db_failed=1
      fi
    elif [ -d "$DB_BACKUP_PATH" ]; then
      rm -rf "$DB_BACKUP_PATH"        # копия не доснята — она ничему не соответствует
    fi
    if [ "$db_failed" = "1" ]; then
      err "Код и окружение возвращены, но вернуть базу из копии НЕ удалось."
      err "Служба оставлена остановленной: прежней версии нельзя работать на базе, изменённой новой."
      err "Освободите место и выполните:"
      err "  cp -a \"$DB_BACKUP_PATH\"/$(basename "$DB_PATH")* \"$(dirname "$DB_PATH")\"/ && systemctl start $APP_NAME"
      return 0
    fi
    # Не «служба перезапущена» вслепую: проверяем, что прежняя версия поднялась.
    SVC_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl reset-failed "$APP_NAME" 2>/dev/null || true
    systemctl restart "$APP_NAME" 2>/dev/null || true
    wait_service_up 60 || back_rc=$?
    if [ "$back_rc" = "0" ]; then
      warn "Откат выполнен. Служба работает, версия: $(installed_version)"
    else
      err "Откат выполнен (версия $(installed_version)), но служба НЕ поднялась — проверьте её вручную."
      show_service_failure
    fi
  elif [ "$ROLLED_BACK" = "0" ]; then
    warn "Резервная копия не создана (или создана не полностью) — откатывать нечего."
    warn "Прежняя установка осталась на месте: $APP_DIR"
    # неполную копию убираем, чтобы её не приняли за годную
    rm -rf "$BACKUP_PATH" "$VENV_BACKUP_PATH" 2>/dev/null || true
    if [ "$WAS_ACTIVE" = "1" ]; then
      # Служба была остановлена перед копированием — без этого она так и
      # оставалась стоять, и копирование почты всех ящиков не шло.
      systemctl start "$APP_NAME" 2>/dev/null && warn "Служба запущена снова (прежняя версия)." \
        || err "Не удалось запустить службу — проверьте: systemctl status $APP_NAME"
    fi
  fi
}

# Ловим именно EXIT, а не только ERR. Проверки вида
#   cmd || { err "…"; exit 1; }
# и `if ! cmd; then exit 1; fi` НЕ порождают событие ERR, а именно так оформлены
# «приложение не импортируется» и «служба не запустилась» — то есть самые
# вероятные отказы обновления откат просто не видел.
#
# О завершении судим по FINISHED, а не по коду: при гибели от сигнала (Ctrl-C,
# обрыв SSH) ловушка EXIT получает код 0, и прерванное обновление оставляло
# службу остановленной, а окружение — наполовину обновлённым, без отката.
# Внутри ловушки set -e выключен: первый же сбой в откате обрывал бы его.
on_exit(){
  local ec=$?
  trap - EXIT ERR
  trap '' INT TERM HUP PIPE
  set +e
  if [ "$FINISHED" != "1" ]; then
    [ "$ec" -ne 0 ] || ec=1
    if [ "$TOUCHED" = "1" ]; then rollback "$ec"; fi
  fi
  exit "$ec"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap '' PIPE
# Неожиданный отказ любой команды: называем её и место. Без этого скрипт при
# set -e молча уходил в откат, и было не понять, на чём он остановился.
trap 'err "Команда завершилась с ошибкой (${BASH_SOURCE[0]##*/}:$LINENO): $BASH_COMMAND"' ERR

require_root(){ [ "$(id -u)" -eq 0 ] || { err "Запускайте с sudo."; exit 1; }; }

detect_source(){
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  [ -d "$SRC_DIR/mailarchiver" ] || { err "Не найдены новые исходники."; exit 1; }
}

check_installed(){
  [ -d "$APP_DIR" ] || { err "MailArchiver не установлен ($APP_DIR не найден). Используйте install.sh."; exit 1; }
  [ -x "$VENV_DIR/bin/python" ] || { err "Не найдено окружение $VENV_DIR. Используйте install.sh."; exit 1; }
}

show_versions(){
  local old new
  old="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"
  new="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo '?')"
  info "Текущая версия: $old → новая версия: $new"
}

# Места должно хватить на копии кода и окружения. Проверяем до остановки
# службы: раньше нехватка обнаруживалась посреди копирования, когда служба уже
# стояла.
check_free_space(){
  local need free
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  need="$({ du -sk "$APP_DIR" "$VENV_DIR" 2>/dev/null || true; } | awk '{s+=$1} END{print s+0}')"
  free="$({ df -Pk "$BACKUP_DIR" 2>/dev/null || true; } | awk 'NR==2{print $4}')"
  [ -n "$free" ] || return 0
  if [ "$free" -lt $((need + 51200)) ]; then
    err "Мало места в $BACKUP_DIR: для копии текущей установки нужно $((need / 1024 + 50)) МБ, свободно $((free / 1024)) МБ."
    err "Освободите место (старые копии: $BACKUP_DIR) и запустите обновление заново."
    exit 1
  fi
}

# Копия базы перед первым запуском новой версии. Копируется только файл базы
# (с журналами WAL и отката), письма на диске не копируются и не трогаются.
backup_database(){
  if [ "${MA_SKIP_DB_BACKUP:-0}" = "1" ]; then
    warn "Копия базы пропущена (MA_SKIP_DB_BACKUP=1): при откате база останется как есть."
    return 0
  fi
  DB_PATH="$(cfg_value db_path)"
  if [ -z "$DB_PATH" ] || [ ! -f "$DB_PATH" ]; then
    warn "Файл базы не найден (${DB_PATH:-путь узнать не удалось}) — обновляю без её копии."
    DB_PATH=""
    return 0
  fi
  local need free suffix
  # du завершается с ошибкой, если файла -wal нет, — это не отказ
  need="$({ du -sk "$DB_PATH" "$DB_PATH-wal" 2>/dev/null || true; } | awk '{s+=$1} END{print s+0}')"
  free="$({ df -Pk "$BACKUP_DIR" 2>/dev/null || true; } | awk 'NR==2{print $4}')"
  if [ -n "$free" ] && [ "$free" -lt $((need + need / 10 + 10240)) ]; then
    warn "Для копии базы ($((need / 1024)) МБ) не хватает места в $BACKUP_DIR — обновляю без неё."
    warn "При откате база останется в том виде, в каком её оставит новая версия."
    DB_PATH=""
    return 0
  fi
  info "Копия базы данных ($((need / 1024)) МБ) → $DB_BACKUP_PATH"
  mkdir -p "$DB_BACKUP_PATH"
  chmod 700 "$DB_BACKUP_PATH"
  for suffix in "" -wal -shm -journal; do
    if [ -f "$DB_PATH$suffix" ]; then cp -a "$DB_PATH$suffix" "$DB_BACKUP_PATH"/; fi
  done
  touch "$DB_BACKUP_PATH/.complete"
}

install_cli_wrapper(){
  # Команда «mailarchiver» в PATH (её вызывают документация и подсказки).
  # От root команды выполняются от имени службы и с её конфигурацией: иначе
  # запуск через sudo оставлял бы в каталоге данных файлы root:root, и служба
  # переставала бы в них писать. storage-key выполняется как есть — ключ
  # обычно создают в /etc/mailarchiver, куда служба писать не может.
  local wrapper="$CLI_WRAPPER"
  if cat > "$wrapper" <<EOF
#!/bin/sh
# MailArchiver CLI (создан установщиком; перезаписывается при обновлении).
VENV_BIN="$VENV_DIR/bin/mailarchiver"
CONFIG="$CONFIG_FILE"
if [ "\$(id -u)" = "0" ] && [ "\${1:-}" != "storage-key" ] && command -v runuser >/dev/null 2>&1; then
  exec runuser -u "$APP_USER" -- "\$VENV_BIN" -c "\$CONFIG" "\$@"
fi
exec "\$VENV_BIN" -c "\$CONFIG" "\$@"
EOF
  then
    chmod 755 "$wrapper" || true
    ok "Команда mailarchiver доступна: $wrapper"
  else
    warn "Не удалось создать $wrapper — команда mailarchiver в PATH не обновлена."
  fi
}

# Необязательные программы для новых возможностей: rsync и ssh нужны копии
# архива на другой сервер (с версии 1.4.0). Их нет — обновление не прерываем:
# остальные способы копии (папка, S3) работают и без них.
ensure_optional_tools(){
  local missing=()
  command -v rsync >/dev/null 2>&1 || missing+=(rsync)
  command -v ssh >/dev/null 2>&1 || missing+=(ssh)
  [ "${#missing[@]}" -eq 0 ] && return 0
  info "Доустанавливаю ${missing[*]} (нужны для копии архива на сервер по SSH)…"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync openssh-client >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q rsync openssh-clients >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y -q rsync openssh-clients >/dev/null 2>&1 || true
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install rsync openssh-clients >/dev/null 2>&1 \
      || zypper --non-interactive install rsync openssh >/dev/null 2>&1 || true
  elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm rsync openssh >/dev/null 2>&1 || true
  fi
  if command -v rsync >/dev/null 2>&1 && command -v ssh >/dev/null 2>&1; then
    ok "rsync и ssh установлены."
  else
    warn "rsync/ssh установить не удалось — копия архива на сервер по SSH работать не будет (папка и S3 — будут)."
  fi
}

main(){
  require_root
  detect_source
  check_installed
  show_versions
  # Пока ничего не тронуто: есть ли пользователь службы, открыты ли для него
  # каталоги над установкой, конфигурация и данные, хватит ли места.
  check_service_access "$(dirname "$INSTALL_DIR")" || exit 1
  check_free_space

  if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then WAS_ACTIVE=1; fi
  info "Останавливаю службу…"
  TOUCHED=1
  systemctl stop "$APP_NAME" 2>/dev/null || warn "Служба не была запущена"

  info "Резервная копия текущей установки → $BACKUP_PATH"
  # Метка могла остаться в каталоге после отката прежним скриптом: тогда она
  # попадала в новую копию первой же и «подтверждала» её ещё до конца копирования.
  rm -f "$APP_DIR/.complete"
  cp -a "$APP_DIR" "$BACKUP_PATH"
  # venv копируем тоже: рабочий код и библиотеки живут именно там
  info "Резервная копия окружения → $VENV_BACKUP_PATH"
  cp -a "$VENV_DIR" "$VENV_BACKUP_PATH"
  if [ -f "$UNIT_FILE" ]; then cp -a "$UNIT_FILE" "$BACKUP_DIR/unit_$STAMP.service"; fi
  # Метка «копия снята полностью». Без неё оборванный на середине cp (кончилось
  # место) оставлял каталог, который откат принимал за годную копию и ставил
  # обрывок на место рабочей установки.
  touch "$BACKUP_PATH/.complete"

  info "Обновление файлов…"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      "$SRC_DIR"/ "$APP_DIR"/
  else
    rm -rf "$APP_DIR"/mailarchiver
    cp -a "$SRC_DIR"/. "$APP_DIR"/ || { err "Не удалось скопировать новые файлы."; exit 1; }
    rm -rf "$APP_DIR/venv" "$APP_DIR/.git" 2>/dev/null || true
  fi
  clean_build_leftovers
  # Права исходников (их принёс rsync -a / cp -a) приводим к своим: см. common.sh.
  normalize_app_perms

  info "Обновление зависимостей…"
  "$VENV_DIR/bin/pip" install --upgrade pip -q || true
  # Версии библиотек берём из requirements.txt, а пакет ставим с --no-deps:
  # `pip install --upgrade "$APP_DIR"` обновлял зависимости по нестрогим
  # границам pyproject.toml, и в систему приезжали непроверенные версии.
  #
  # Порядок: сначала пакет, потом библиотеки. Наоборот pip сверял новые
  # библиотеки с требованиями ЕЩЁ ПРЕЖНЕГО пакета и печатал «ERROR: pip's
  # dependency resolver… mailarchiver 1.2.0 requires IMAPClient<4» — ошибка
  # мнимая, но выглядела как сбой обновления.
  "$VENV_DIR/bin/pip" install -q --no-deps --upgrade "$APP_DIR"
  if [ -f "$APP_DIR/requirements.txt" ]; then
    "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  fi
  local pip_report
  if ! pip_report="$("$VENV_DIR/bin/pip" check 2>&1)"; then
    warn "pip сообщает о несогласованных библиотеках в окружении:"
    printf '%s\n' "$pip_report" | sed 's/^/    /' || true
  fi
  clean_build_leftovers
  normalize_app_perms

  info "Проверка установленного пакета…"
  check_package || exit 1

  info "Проверка доступа службы к файлам…"
  check_service_access || exit 1

  info "Проверка конфигурации…"
  # От имени службы: check-config ничего не создаёт, но так сразу видно, что
  # служба может прочитать конфигурацию. Владельца в каталоге данных чиним
  # только у чужих файлов (например созданных запуском команд от root).
  as_app_user "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" check-config \
    || { err "Новая версия не приняла конфигурацию $CONFIG_FILE (сообщение выше)."; exit 1; }
  if [ -d "$DATA_DIR" ]; then
    find "$DATA_DIR" \( ! -user "$APP_USER" -o ! -group "$APP_GROUP" \) -exec chown -h "$APP_USER:$APP_GROUP" {} + \
      2>/dev/null || chown -R "$APP_USER:$APP_GROUP" "$DATA_DIR"
  fi

  info "Пробный импорт веб-приложения от имени службы…"
  check_app_import || exit 1

  # обновить unit, если изменился шаблон. Свои правки (MemoryMax и т.п.)
  # держите в /etc/systemd/system/mailarchiver.service.d/override.conf —
  # сам unit перезаписывается при каждом обновлении.
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    render_unit "$APP_DIR/systemd/mailarchiver.service"
    systemctl daemon-reload
  fi

  install_cli_wrapper
  ensure_optional_tools

  # До этого места база не открывалась. Дальше новая версия обновит её схему —
  # снимаем копию, чтобы откат вернул и её.
  backup_database

  info "Запуск службы…"
  NEW_STARTED=1
  start_service_checked || exit 1

  FINISHED=1     # успех — откат не нужен
  rm -f "$BACKUP_PATH/.complete"
  if [ -d "$DB_BACKUP_PATH" ]; then
    if [ "${MA_KEEP_DB_BACKUP:-0}" = "1" ]; then
      info "Копия базы до обновления оставлена: $DB_BACKUP_PATH"
    else
      rm -rf "$DB_BACKUP_PATH"
    fi
  fi
  # Печатаем версию: без неё после обновления невозможно убедиться, что
  # запустилась именно новая сборка (а не осталась прежняя).
  ok "Обновление завершено успешно. Установленная версия: $(installed_version)"
  # чистим старые копии (оставляем 5 последних каталогов кода и 3 окружения:
  # venv весит на порядок больше, а нужен только для самого свежего отката)
  keep_newest 5 "$BACKUP_DIR" "app_"
  keep_newest 3 "$BACKUP_DIR" "venv_"
  keep_newest 5 "$BACKUP_DIR" "unit_"
  keep_newest 1 "$BACKUP_DIR" "db_"
  info "Резервная копия прежней версии: $BACKUP_PATH"
}
main "$@"
