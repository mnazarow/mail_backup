#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт установки для Linux
#  Автоопределение дистрибутива (apt/dnf/yum/zypper/pacman), установка
#  зависимостей, создание пользователя, службы systemd и первичная настройка.
#  Максимальная обработка ошибок: остановка при любой ошибке, откат частичной
#  установки, подробные сообщения.
#
#  Запуск (из каталога с исходниками):   sudo bash scripts/install.sh
#          справка:                        bash scripts/install.sh --help
#
#  Переменные окружения (все необязательны). Задаются ПОСЛЕ sudo:
#      sudo MA_PORT=8080 bash scripts/install.sh
#  (написанные перед sudo, они до скрипта не доходят: sudo очищает окружение).
#    MA_INSTALL_DIR     каталог установки (/opt/mailarchiver)
#    MA_CONFIG_DIR      каталог конфигурации (/etc/mailarchiver)
#    MA_DATA_DIR        каталог данных (/var/lib/mailarchiver)
#    MA_USER, MA_GROUP  пользователь и группа службы (mailarchiver)
#    MA_HOST, MA_PORT   адрес и порт веб-интерфейса в новой конфигурации
#                       (127.0.0.1, 8493)
#    MA_START_TIMEOUT   сколько секунд ждать ответа веб-интерфейса (180)
#    MA_HEALTH_URL      свой адрес проверки вместо взятого из конфигурации
#    MA_RELAX_SANDBOX   1 — ослабить защиту службы (контейнеры LXC/OpenVZ без
#                       вложенности, где служба падает со status=226/NAMESPACE):
#                       создаётся дополнение <unit>.d/50-relax-sandbox.conf
#    MA_WHEELS          каталог с пакетами .whl для установки без интернета
#                       (по умолчанию — wheels/ рядом с исходниками, если есть)
#    MA_INDEX_URL       свой индекс пакетов (зеркало PyPI) для установки из сети
#    MA_UNIT_FILE, MA_CLI_WRAPPER — пути unit-файла и команды mailarchiver
# ============================================================================
set -Eeuo pipefail

usage(){
  cat <<'EOF'
Использование: sudo bash scripts/install.sh
(запускать из каталога с исходниками; для обновления версии — scripts/update.sh)

  -h, --help   эта справка

Переменные окружения (все необязательны). Задаются ПОСЛЕ sudo:
    sudo MA_PORT=8080 bash scripts/install.sh
(написанные перед sudo, они до скрипта не доходят: sudo очищает окружение)
  MA_INSTALL_DIR=КАТАЛОГ   каталог установки (/opt/mailarchiver)
  MA_CONFIG_DIR=КАТАЛОГ    каталог конфигурации (/etc/mailarchiver)
  MA_DATA_DIR=КАТАЛОГ      каталог данных (/var/lib/mailarchiver)
  MA_USER, MA_GROUP        пользователь и группа службы (mailarchiver)
  MA_HOST, MA_PORT         адрес и порт веб-интерфейса в новой конфигурации
  MA_START_TIMEOUT=N       сколько секунд ждать ответа веб-интерфейса (180)
  MA_HEALTH_URL=URL        свой адрес проверки вместо взятого из конфигурации
  MA_RELAX_SANDBOX=1       ослабить защиту службы: для контейнеров LXC/OpenVZ без
                           вложенности, где служба падает со status=226/NAMESPACE
  MA_WHEELS=КАТАЛОГ        пакеты .whl для установки без интернета (по умолчанию —
                           каталог wheels рядом с исходниками, если он есть)
  MA_INDEX_URL=URL         свой индекс пакетов (зеркало PyPI) для установки из сети
  MA_UNIT_FILE, MA_CLI_WRAPPER
                           пути unit-файла и команды mailarchiver
EOF
}

# Параметры разбираются до установки ловушек: справка и опечатка — не
# «прерванная установка». Параметров у скрипта нет; незнакомый — отказ, а не
# молчаливая установка (например в ответ на «--check» от update.sh).
for _arg in "$@"; do
  case "$_arg" in
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $_arg" >&2; usage >&2; exit 2 ;;
  esac
done

# ---------- Параметры (можно переопределить переменными окружения) ----------
APP_NAME="mailarchiver"
UNIT_FILE="${MA_UNIT_FILE:-/etc/systemd/system/${APP_NAME}.service}"

# Установка поверх существующей: каталоги и пользователя, не заданные в MA_*,
# берём из её unit-файла — как update.sh. Иначе переустановка «по умолчанию» (её
# советуют сообщения update.sh, когда испорчено окружение) переписывала unit
# каталогом /var/lib/mailarchiver и пользователем mailarchiver, хотя установка
# делалась с MA_DATA_DIR или MA_USER: служба теряла данные из виду, а защита
# «диск не подключён — служба не стартует» пропадала до следующего обновления.
unit_val(){ [ -f "$UNIT_FILE" ] || return 0; sed -n "s/^$1=//p" "$UNIT_FILE" | head -n 1; }
_unit_wd="$(unit_val WorkingDirectory)"
_unit_cfg="$(unit_val 'Environment=MAILARCHIVER_CONFIG')"
_unit_paths=()
read -r -a _unit_paths <<< "$(unit_val ReadWritePaths)" || true
_unit_data="${_unit_paths[0]:-}"
while [ "${_unit_data#[-+]}" != "$_unit_data" ]; do _unit_data="${_unit_data#[-+]}"; done

APP_USER="${MA_USER:-$(unit_val User)}";    APP_USER="${APP_USER:-mailarchiver}"
APP_GROUP="${MA_GROUP:-$(unit_val Group)}"; APP_GROUP="${APP_GROUP:-mailarchiver}"
INSTALL_DIR="${MA_INSTALL_DIR:-}"
if [ -z "$INSTALL_DIR" ]; then
  case "$_unit_wd" in */app) INSTALL_DIR="${_unit_wd%/app}" ;; *) INSTALL_DIR="/opt/mailarchiver" ;; esac
fi
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
if [ -n "${MA_CONFIG_DIR:-}" ]; then CONFIG_DIR="$MA_CONFIG_DIR"; CONFIG_FILE="$CONFIG_DIR/config.yaml"
elif [ -n "$_unit_cfg" ]; then CONFIG_FILE="$_unit_cfg"; CONFIG_DIR="$(dirname "$_unit_cfg")"
else CONFIG_DIR="/etc/mailarchiver"; CONFIG_FILE="$CONFIG_DIR/config.yaml"; fi
DATA_DIR="${MA_DATA_DIR:-$_unit_data}";     DATA_DIR="${DATA_DIR:-/var/lib/mailarchiver}"
SERVICE_FILE="$UNIT_FILE"
CLI_WRAPPER="${MA_CLI_WRAPPER:-/usr/local/bin/mailarchiver}"
BIND_HOST="${MA_HOST:-127.0.0.1}"
BIND_PORT="${MA_PORT:-8493}"
LOG_PREFIX="[MailArchiver install]"
SCRIPT_NAME="install.sh"

# ---------- Цвета ----------
if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*"; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*"; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*"; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2; }
# (после подключения common.sh эти четыре функции заменяются его вариантами,
# которые не обрывают скрипт, если терминал пропал)

# ---------- Общие с update.sh функции (права, проверки, запуск службы) ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$SCRIPT_DIR/common.sh" ]; then
  err "Не найден $SCRIPT_DIR/common.sh — запускайте скрипт из распакованного архива целиком."
  exit 1
fi
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/common.sh"
DATA_DIR="$(strip_slash "$DATA_DIR")"
if [ -n "${MA_DATA_DIR:-}" ]; then DATA_DIR_FROM="из переменной MA_DATA_DIR"
elif [ -n "$_unit_data" ]; then DATA_DIR_FROM="из unit-файла прежней установки $UNIT_FILE (ReadWritePaths)"
else DATA_DIR_FROM="по умолчанию (другой задаётся в MA_DATA_DIR)"; fi

# ---------- Обработка ошибок и откат ----------
ROLLBACK_ENABLED=0
FRESH_INSTALL=0       # установки здесь ещё не было (нет $APP_DIR)
INSTALL_DIR_EXISTED=0 # сам каталог установки был до нас (создан заранее, точка монтирования)
VENV_EXISTED=0
ROLLED_BACK=0
FINISHED=0            # установка доведена до конца
UNIT_EXISTED=0        # unit-файл был до нас (например установка в другой каталог) — при откате не трогаем
WRAPPER_CREATED=0     # команду mailarchiver создали мы
REINSTALL_BACKUP=""   # каталог с копией прежней установки (при переустановке)
cleanup_on_error(){
  local ec="${1:-1}" back_rc=0 reload=0
  err "Установка прервана (код $ec)."
  if [ "$ROLLBACK_ENABLED" = "1" ] && [ "$ROLLED_BACK" = "0" ]; then
    ROLLED_BACK=1
    if [ "$FRESH_INSTALL" = "1" ]; then
      warn "Выполняю откат частичной установки…"
      if [ "$UNIT_EXISTED" = "0" ]; then
        systemctl stop "$APP_NAME" 2>/dev/null || true
        systemctl disable "$APP_NAME" 2>/dev/null || true
        rm -f "$SERVICE_FILE" 2>/dev/null || true
        reload=1
      fi
      # дополнение с ослабленной защитой — только если его создал этот запуск
      if remove_created_relax_sandbox; then reload=1; fi
      if [ "$reload" = "1" ]; then systemctl daemon-reload 2>/dev/null || true; fi
      if [ "$WRAPPER_CREATED" = "1" ]; then rm -f "$CLI_WRAPPER" 2>/dev/null || true; fi
      if [ "$INSTALL_DIR_EXISTED" = "0" ]; then
        rm -rf "$INSTALL_DIR" 2>/dev/null || true
        warn "Откат завершён. Каталоги данных ($DATA_DIR) и конфигурации ($CONFIG_DIR) НЕ тронуты."
      else
        # Каталог установки был до нас (создан заранее или это точка
        # монтирования): убираем только то, что положили в него сами. Раньше
        # для такого каталога откат не делал ничего — оставались и unit службы
        # в цикле перезапусков, и дополнение с ослабленной защитой.
        rm -rf "$APP_DIR" 2>/dev/null || true
        if [ "$VENV_EXISTED" = "0" ]; then rm -rf "$VENV_DIR" 2>/dev/null || true; fi
        warn "Откат завершён. Каталог $INSTALL_DIR был до установки и оставлен на месте; каталоги данных ($DATA_DIR) и конфигурации ($CONFIG_DIR) НЕ тронуты."
      fi
    elif [ -n "$REINSTALL_BACKUP" ] && [ -f "$REINSTALL_BACKUP/.complete" ]; then
      # Установка ПОВЕРХ существующей. Раньше откат здесь был выключен
      # совсем: rsync --delete уже стирал рабочий код, а при падении
      # pip install сервер оставался с наполовину обновлённым окружением
      # и без единого способа вернуться назад.
      warn "Возврат прежней установки из $REINSTALL_BACKUP…"
      systemctl stop "$APP_NAME" 2>/dev/null || true
      if [ -d "$REINSTALL_BACKUP/app" ]; then rm -rf "$APP_DIR"; cp -a "$REINSTALL_BACKUP/app" "$APP_DIR"; fi
      if [ -d "$REINSTALL_BACKUP/venv" ]; then rm -rf "$VENV_DIR"; cp -a "$REINSTALL_BACKUP/venv" "$VENV_DIR"; fi
      if [ -f "$REINSTALL_BACKUP/mailarchiver.service" ]; then
        cp -a "$REINSTALL_BACKUP/mailarchiver.service" "$SERVICE_FILE" 2>/dev/null || true
        reload=1
      fi
      if remove_created_relax_sandbox; then reload=1; fi
      if [ "$reload" = "1" ]; then systemctl daemon-reload 2>/dev/null || true; fi
      # не «служба перезапущена» вслепую: проверяем, что прежняя установка поднялась
      SVC_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
      systemctl reset-failed "$APP_NAME" 2>/dev/null || true
      systemctl start "$APP_NAME" 2>/dev/null || true
      wait_service_up 60 || back_rc=$?
      if [ "$back_rc" = "0" ]; then
        warn "Прежняя установка возвращена, служба работает. Копия осталась в $REINSTALL_BACKUP."
      else
        err "Прежняя установка возвращена, но служба НЕ поднялась — проверьте её вручную."
        show_service_failure
      fi
    else
      # установка поверх, а копия прежней не снята: прежняя не тронута, но
      # созданное этим запуском дополнение оставлять нельзя
      if remove_created_relax_sandbox; then systemctl daemon-reload 2>/dev/null || true; fi
    fi
  fi
  err "Подробности выше. Исправьте причину и запустите скрипт заново."
}

# Ловим EXIT, а не только ERR: проверки вида `cmd || { err "…"; exit 1; }`
# события ERR не порождают, а именно так оформлены «не удалось установить
# пакет» и «служба не запустилась» — самые вероятные отказы установки.
#
# О завершении судим по FINISHED, а не по коду: при гибели от сигнала (Ctrl-C,
# обрыв SSH) ловушка EXIT получает код 0, и прерванная установка оставалась
# без отката. Внутри ловушки set -e выключен: первый же сбой обрывал бы откат.
on_exit(){
  local ec=$?
  trap - EXIT ERR
  trap '' INT TERM HUP PIPE
  set +e
  if [ "$FINISHED" != "1" ]; then
    [ "$ec" -ne 0 ] || ec=1
    cleanup_on_error "$ec"
  fi
  exit "$ec"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap '' PIPE
# Неожиданный отказ любой команды: называем её и место (при set -e скрипт иначе
# останавливался молча).
trap 'err "Команда завершилась с ошибкой (${BASH_SOURCE[0]##*/}:$LINENO): $BASH_COMMAND"' ERR

backup_existing(){
  # Копия прежней установки перед перезаписью (только при установке поверх).
  [ "$FRESH_INSTALL" = "1" ] && return 0
  [ -d "$APP_DIR" ] || return 0
  REINSTALL_BACKUP="$INSTALL_DIR/backups/reinstall_$(date +%Y%m%d_%H%M%S)"
  info "Обнаружена существующая установка. Резервная копия → $REINSTALL_BACKUP"
  warn "Для обычного обновления версии предназначен scripts/update.sh."
  mkdir -p "$REINSTALL_BACKUP"
  cp -a "$APP_DIR" "$REINSTALL_BACKUP/app"
  [ -d "$VENV_DIR" ] && cp -a "$VENV_DIR" "$REINSTALL_BACKUP/venv"
  touch "$REINSTALL_BACKUP/.complete"   # метка «копия снята полностью»
  systemctl stop "$APP_NAME" 2>/dev/null || true
  ok "Резервная копия создана."
}

# ---------- Проверки окружения ----------
require_root(){
  if [ "$(id -u)" -ne 0 ]; then
    err "Скрипт нужно запускать с правами root: sudo bash scripts/install.sh"
    exit 1
  fi
}

SRC_DIR=""
NEW_VERSION=""        # из файла VERSION исходников (нужен установке без сети)
detect_source(){
  # каталог, где лежит этот скрипт → корень проекта на уровень выше
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  if [ ! -d "$SRC_DIR/mailarchiver" ] || [ ! -f "$SRC_DIR/requirements.txt" ]; then
    err "Не найден исходный код (каталог mailarchiver/ и requirements.txt)."
    err "Запускайте скрипт из распакованного архива проекта."
    exit 1
  fi
  NEW_VERSION="$(source_version)"
  info "Источник: $SRC_DIR"
}

# Установка без интернета: каталог с пакетами (.whl) — $MA_WHEELS или wheels/
# рядом с исходниками. Проверяется в самом начале, пока ничего не тронуто:
# неверный MA_WHEELS или архив без пакета приложения — отказ до любых изменений.
detect_offline(){
  detect_wheels || exit 1
  if [ -z "$WHEELS_DIR" ]; then return 0; fi
  require_app_wheel "$NEW_VERSION" || exit 1
  info "Установка без сети: пакеты ($(count_wheels "$WHEELS_DIR") шт.) будут взяты из $WHEELS_DIR."
}

# Системные пакеты не поставились. При установке без сети это не всегда отказ:
# репозиторий дистрибутива бывает недоступен так же, как PyPI, а python3 с
# модулями venv и ensurepip на сервере уже есть — тогда продолжаем.
sys_deps_failed(){
  if system_python_ready; then
    warn "Не удалось установить пакеты через $1 (нет доступа к репозиторию?): python3 с модулем venv уже есть — продолжаю."
    return 0
  fi
  err "Не удалось установить пакеты через $1"
  exit 1
}

# Установка без сети, а python3 с модулями venv и ensurepip уже есть: менеджер
# пакетов не нужен. На сервере без интернета «apt-get update» только ждал бы
# недоступные репозитории.
system_python_ready(){
  [ -n "$WHEELS_DIR" ] && command -v python3 >/dev/null 2>&1 \
    && python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

PKG=""
detect_pkg_manager(){
  if command -v apt-get >/dev/null 2>&1; then PKG="apt";
  elif command -v dnf >/dev/null 2>&1; then PKG="dnf";
  elif command -v yum >/dev/null 2>&1; then PKG="yum";
  elif command -v zypper >/dev/null 2>&1; then PKG="zypper";
  elif command -v pacman >/dev/null 2>&1; then PKG="pacman";
  else
    warn "Не удалось определить менеджер пакетов. Установите вручную: python3 (>=3.10), python3-venv, python3-pip."
    PKG="none"
  fi
  # if, а не «&&»: иначе при PKG=none функция возвращала ошибку, и установка
  # без менеджера пакетов обрывалась, хотя выше обещано только предупреждение
  if [ "$PKG" != "none" ]; then info "Менеджер пакетов: $PKG"; fi
}

install_system_deps(){
  info "Установка системных зависимостей…"
  case "$PKG" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq || warn "apt-get update завершился с предупреждениями"
      apt-get install -y python3 python3-venv python3-pip ca-certificates || sys_deps_failed apt
      apt-get install -y pst-utils >/dev/null 2>&1 || warn "pst-utils не установлен (не обязателен: это запасной способ импорта .pst, основной встроен)"
      apt-get install -y rsync openssh-client >/dev/null 2>&1 || warn "rsync/openssh-client не установлены (нужны только для копии архива на сервер по SSH)"
      ;;
    dnf|yum)
      $PKG install -y python3 python3-pip ca-certificates || sys_deps_failed "$PKG"
      $PKG install -y libpst >/dev/null 2>&1 || warn "libpst не установлен (не обязателен: это запасной способ импорта .pst, основной встроен)"
      $PKG install -y rsync openssh-clients >/dev/null 2>&1 || warn "rsync/openssh-clients не установлены (нужны только для копии архива на сервер по SSH)"
      ;;
    zypper)
      zypper --non-interactive install python3 python3-pip python3-virtualenv ca-certificates || sys_deps_failed zypper
      zypper --non-interactive install libpst >/dev/null 2>&1 || warn "libpst не установлен (не обязателен: запасной способ импорта .pst)"
      zypper --non-interactive install rsync openssh-clients >/dev/null 2>&1 \
        || zypper --non-interactive install rsync openssh >/dev/null 2>&1 \
        || warn "rsync/openssh не установлены (нужны только для копии архива на сервер по SSH)"
      ;;
    pacman)
      pacman -Sy --noconfirm python python-pip ca-certificates || sys_deps_failed pacman
      pacman -S --noconfirm libpst >/dev/null 2>&1 || warn "libpst не установлен (не обязателен: запасной способ импорта .pst)"
      pacman -S --noconfirm rsync openssh >/dev/null 2>&1 || warn "rsync/openssh не установлены (нужны только для копии архива на сервер по SSH)"
      ;;
    none) warn "Пропускаю установку системных пакетов." ;;
  esac
  ok "Системные зависимости готовы."
}

check_python(){
  if ! command -v python3 >/dev/null 2>&1; then err "python3 не найден после установки."; exit 1; fi
  local ver; ver="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  local major minor; major="${ver%%.*}"; minor="${ver##*.}"
  if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    err "Требуется Python >= 3.10, найден $ver. Установите более новую версию."
    exit 1
  fi
  # Сервис проверен на 3.10–3.14. На более новых версиях установка, скорее всего,
  # тоже пройдёт, но части зависимостей может не хватать готовых пакетов (колёс).
  if [ "$major" -eq 3 ] && [ "$minor" -gt 14 ]; then
    warn "Python $ver новее проверенных версий (3.10–3.14)."
    warn "Если установка зависимостей не удастся — используйте Python 3.12–3.14."
  fi
  ok "Python $ver подходит."
  # пакеты автономного архива годятся только под свою версию Python и архитектуру
  check_wheels_fit python3 || true
}

create_user(){
  # Группа: useradd --system создаёт одноимённую группу не на всех дистрибутивах,
  # а на неё дальше опирается chown. Создаём явно, если её ещё нет.
  if ! getent group "$APP_GROUP" >/dev/null 2>&1; then
    groupadd --system "$APP_GROUP" 2>/dev/null || warn "Не удалось создать группу $APP_GROUP"
  fi
  if id "$APP_USER" >/dev/null 2>&1; then
    info "Пользователь $APP_USER уже существует."
  else
    info "Создаю системного пользователя $APP_USER…"
    # Группу указываем явно (-g): она создана выше, а без -g useradd на
    # Debian/Ubuntu/RHEL/Arch (USERGROUPS_ENAB yes) пытается создать
    # одноимённую группу ещё раз и падает: «group mailarchiver exists».
    local group_opt=()
    if getent group "$APP_GROUP" >/dev/null 2>&1; then group_opt=(-g "$APP_GROUP"); fi
    useradd --system ${group_opt[@]+"${group_opt[@]}"} --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null \
      || useradd --system ${group_opt[@]+"${group_opt[@]}"} --home-dir "$DATA_DIR" --shell /bin/false "$APP_USER" \
      || { err "Не удалось создать пользователя $APP_USER"; exit 1; }
    ok "Пользователь $APP_USER создан."
  fi
}

# Команды от имени службы выполняет as_app_user (common.sh): файлы, которые она
# создаёт (база, каталоги данных), сразу получают правильного владельца, и не
# нужен chown -R всего каталога данных (на миллионах писем — долгая пауза, а
# резервные копии вроде borg/restic из-за смены ctime перечитывают весь архив).
# Владельца у чужих файлов возвращает fix_data_owner (common.sh).

copy_source(){
  info "Копирование файлов в $APP_DIR…"
  mkdir -p "$APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      --exclude '/wheels' "$SRC_DIR"/ "$APP_DIR"/ || { err "Не удалось скопировать файлы в $APP_DIR."; exit 1; }
  else
    # Без rsync: раньше ошибка копирования глоталась («|| true»), и установка
    # рапортовала «успешно» на старом коде.
    rm -rf "$APP_DIR"/mailarchiver
    cp -a "$SRC_DIR"/. "$APP_DIR"/ || { err "Не удалось скопировать файлы в $APP_DIR."; exit 1; }
    rm -rf "$APP_DIR/venv" "$APP_DIR/.git" "$APP_DIR/data" "$APP_DIR/wheels" 2>/dev/null || true
  fi
  # (каталог wheels в установку не копируется: пакеты нужны только pip)
  # Права исходников (их принёс rsync -a / cp -a) приводим к своим: см. common.sh.
  normalize_app_perms
  ok "Файлы скопированы."
}

setup_venv(){
  info "Создание виртуального окружения и установка зависимостей…"
  python3 -m venv "$VENV_DIR" || { err "Не удалось создать venv (установлен ли python3-venv?)"; exit 1; }
  if [ -n "$WHEELS_DIR" ]; then
    # Без сети pip, setuptools и wheel не обновляем: всё ставится из готовых
    # пакетов, собирать ничего не нужно.
    info "Установка без сети: пакеты берутся из $WHEELS_DIR."
  else
    "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q || warn "Обновление pip завершилось с предупреждениями"
  fi

  # ВАЖНО: зависимости ставим ИЗ requirements.txt, а сам пакет — с --no-deps.
  # Раньше здесь было `pip install "$APP_DIR"`, из-за чего версии библиотек
  # подтягивались по нестрогим границам pyproject.toml и «уплывали» при каждой
  # установке — на новых версиях Starlette это роняло веб-интерфейс.
  if [ -f "$APP_DIR/requirements.txt" ]; then
    if ! "$VENV_DIR/bin/pip" install -q ${PIP_OFFLINE[@]+"${PIP_OFFLINE[@]}"} -r "$APP_DIR/requirements.txt"; then
      err "Не удалось установить зависимости Python из requirements.txt."
      if [ -n "$WHEELS_DIR" ]; then
        err "В каталоге $WHEELS_DIR не нашлось всех библиотек под этот Python (сообщение pip выше)."
        err "Автономный архив собирают на машине с той же версией Python и той же архитектурой, что у сервера."
      else
        err "Проверьте доступ в интернет / прокси и версию Python (нужен 3.10+)."
      fi
      exit 1
    fi
  else
    warn "requirements.txt не найден — ставлю зависимости по pyproject.toml."
  fi
  clean_build_leftovers
  if [ -n "$WHEELS_DIR" ]; then
    # Приложение — из его пакета: сборка из исходников потребовала бы скачать
    # setuptools. --force-reinstall: при установке поверх и та же версия
    # ставится заново, из пакета.
    if ! "$VENV_DIR/bin/pip" install -q "${PIP_OFFLINE[@]}" --no-deps --upgrade --force-reinstall "mailarchiver==$NEW_VERSION"; then
      err "Не удалось установить пакет mailarchiver $NEW_VERSION из $WHEELS_DIR (сообщение pip выше)."
      exit 1
    fi
  elif ! "$VENV_DIR/bin/pip" install -q --no-deps "$APP_DIR"; then
    err "Не удалось установить пакет mailarchiver."
    exit 1
  fi

  # Контроль: пакет должен импортироваться, видеть все зависимости и подходить
  # установленной версии Python.
  check_package || exit 1
  clean_build_leftovers
  normalize_app_perms
  ok "Зависимости установлены."
}

setup_dirs(){
  mkdir -p "$CONFIG_DIR" "$DATA_DIR"
  fix_data_owner
  chmod 750 "$DATA_DIR"
  chmod 755 "$CONFIG_DIR"
}

write_config(){
  if [ -f "$CONFIG_FILE" ]; then
    info "Файл конфигурации уже существует: $CONFIG_FILE (не перезаписываю)."
    return
  fi
  info "Создаю конфигурацию $CONFIG_FILE…"
  cp "$APP_DIR/config/config.example.yaml" "$CONFIG_FILE"
  # подставить каталог данных и адрес/порт
  sed -i "s|^  data_dir:.*|  data_dir: \"$DATA_DIR\"|" "$CONFIG_FILE" || true
  sed -i "s|^  host:.*|  host: \"$BIND_HOST\"|" "$CONFIG_FILE" || true
  sed -i "s|^  port:.*|  port: $BIND_PORT|" "$CONFIG_FILE" || true
  chown root:"$APP_GROUP" "$CONFIG_FILE"
  chmod 640 "$CONFIG_FILE"
  ok "Конфигурация создана."
}

install_service(){
  info "Установка службы systemd…"
  # прежний unit сохраняем для отката (переустановка поверх)
  if [ -n "$REINSTALL_BACKUP" ] && [ -f "$SERVICE_FILE" ]; then
    cp -a "$SERVICE_FILE" "$REINSTALL_BACKUP/mailarchiver.service" || true
  fi
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    render_unit "$APP_DIR/systemd/mailarchiver.service"
  else
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=MailArchiver — резервное копирование почты по IMAP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
Environment=MAILARCHIVER_CONFIG=$CONFIG_FILE
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/mailarchiver serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
  fi
  # MA_RELAX_SANDBOX=1 — дополнение с ослабленной защитой (для контейнеров)
  apply_relax_sandbox
  systemctl daemon-reload
  systemctl enable "$APP_NAME" >/dev/null 2>&1 || warn "Не удалось включить автозапуск"
  ok "Служба установлена."
}

install_cli_wrapper(){
  # Команда «mailarchiver» в PATH (её вызывают документация и подсказки).
  # От root команды выполняются от имени службы и с её конфигурацией: иначе
  # запуск через sudo оставлял бы в каталоге данных файлы root:root, и служба
  # переставала бы в них писать. storage-key выполняется как есть — ключ
  # обычно создают в /etc/mailarchiver, куда служба писать не может.
  local wrapper="$CLI_WRAPPER" existed=0
  if [ -e "$wrapper" ]; then existed=1; fi
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
    if [ "$existed" = "0" ]; then WRAPPER_CREATED=1; fi
    chmod 755 "$wrapper" || true
    ok "Команда mailarchiver доступна: $wrapper"
  else
    warn "Не удалось создать $wrapper — команда mailarchiver в PATH недоступна."
  fi
}

start_service(){
  # Заранее — то, из-за чего служба чаще всего не поднимается: systemd об этом
  # пишет только в журнал, а установка заканчивалась строкой «не запустилась».
  info "Проверка доступа службы к файлам…"
  check_service_access || exit 1
  info "Пробный импорт веб-приложения от имени службы…"
  check_app_import || exit 1

  info "Запуск службы…"
  local rc=0
  start_service_checked || rc=$?
  case "$rc" in
    0) ok "Служба запущена, веб-интерфейс отвечает." ;;
    2) warn "Служба работает, но подтвердить доступность веб-интерфейса не удалось — проверьте вручную." ;;
    *) exit 1 ;;
  esac
}

create_admin_prompt(){
  echo ""
  info "Создание администратора веб-интерфейса."
  if [ -t 0 ]; then
    # от имени службы: база и каталоги сразу получают правильного владельца
    as_app_user "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" create-admin \
      || warn "Администратора можно создать позже (см. ниже)."
    fix_data_owner
    systemctl restart "$APP_NAME" 2>/dev/null || true
  else
    warn "Неинтерактивный режим — админ не создан."
    echo "    Создайте его командой:"
    echo "      sudo -u $APP_USER $VENV_DIR/bin/mailarchiver -c $CONFIG_FILE create-admin"
    echo "    Либо откройте веб-интерфейс — при первом входе будет предложено создать администратора."
  fi
}

print_summary(){
  echo ""
  ok "Установка завершена!"
  echo -e "  ${GRN}Веб-интерфейс:${NC}   http://$BIND_HOST:$BIND_PORT"
  echo -e "  ${GRN}Конфигурация:${NC}    $CONFIG_FILE"
  echo -e "  ${GRN}Данные/копии:${NC}    $DATA_DIR"
  echo -e "  ${GRN}Служба:${NC}          systemctl status $APP_NAME"
  echo -e "  ${GRN}Логи:${NC}            journalctl -u $APP_NAME -f"
  echo ""
  echo "  Если веб-интерфейс слушает 127.0.0.1, откройте его через SSH-туннель"
  echo "  или настройте reverse proxy (nginx/traefik) с HTTPS — см. docs/ru/03-installation.md."
  echo ""
}

main(){
  info "Запуск установки MailArchiver…"
  require_root
  detect_source
  detect_offline
  # Каталог данных — ссылка на диск, который не подключён (или вовсе не
  # каталог), либо он лежит под точкой монтирования из /etc/fstab, которая не
  # подключена: говорим сразу, пока ничего не установлено и не остановлено.
  # Иначе установка создала бы каталог и базу на месте точки монтирования.
  local unmounted=""
  unmounted="$(unmounted_fstab_mount "$DATA_DIR")"
  if data_dir_broken || [ -n "$unmounted" ]; then
    if ! check_data_dir; then exit 1; fi
    warn_data_dir_under_mount "$unmounted"      # каталог не пуст и доступен службе — не отказ
  fi
  # Чистая установка (её откат убирает всё, что создано) — когда здесь ещё
  # ничего не установлено. Сам каталог при этом мог существовать.
  if [ -d "$INSTALL_DIR" ]; then INSTALL_DIR_EXISTED=1; fi
  if [ ! -d "$APP_DIR" ]; then FRESH_INSTALL=1; fi
  if [ -d "$VENV_DIR" ]; then VENV_EXISTED=1; fi
  if [ -f "$SERVICE_FILE" ]; then UNIT_EXISTED=1; fi
  ROLLBACK_ENABLED=1
  if system_python_ready; then
    info "Установка без сети: python3 с модулями venv и ensurepip уже есть — менеджер пакетов не вызывается (необязательные pst-utils, rsync и ssh при необходимости установите сами)."
  else
    detect_pkg_manager
    install_system_deps
  fi
  check_python
  create_user
  backup_existing
  copy_source
  setup_venv
  setup_dirs
  write_config
  install_service
  install_cli_wrapper
  start_service
  ROLLBACK_ENABLED=0   # дальше откат не нужен
  FINISHED=1
  # чистим старые копии переустановок (оставляем 3 последних)
  keep_newest 3 "$INSTALL_DIR/backups" "reinstall_"
  create_admin_prompt
  print_summary
}
main "$@"
