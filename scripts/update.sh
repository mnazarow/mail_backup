#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт обновления
#  Делает резервную копию текущей установки, заменяет код, обновляет
#  зависимости и перезапускает службу. При ошибке — автоматический откат к
#  предыдущей версии, с показом причины. Письма и конфигурация НЕ трогаются;
#  с базы данных перед первым запуском новой версии снимается копия, и при
#  откате база возвращается к ней.
#
#  Запуск (из каталога с НОВЫМИ исходниками):
#    sudo bash scripts/update.sh            обновить
#    sudo bash scripts/update.sh --check    только проверить, пройдёт ли обновление:
#                                           служба не останавливается, на диске
#                                           ничего не меняется; код возврата 0 —
#                                           обновлять можно
#    sudo bash scripts/update.sh --force    обновить, даже если проверка нашла то,
#                                           что после установки проверяется ещё
#                                           раз, с откатом (см. --help)
#    bash scripts/update.sh --help          справка
#
#  Перед обновлением те же проверки выполняются всегда — до остановки службы.
#
#  Переменные окружения (все необязательны). Задаются ПОСЛЕ sudo:
#      sudo MA_START_TIMEOUT=900 bash scripts/update.sh
#  (написанные перед sudo, они до скрипта не доходят: sudo очищает окружение).
#  Режим проверки включается только параметром --check: переменной для него нет
#  именно поэтому — потерянная в sudo, она превращала бы проверку в обновление.
#    MA_START_TIMEOUT   сколько секунд ждать ответа веб-интерфейса (180)
#    MA_START_TIMEOUT_MAX  если срок вышел, а служба занята (обновляет базу), —
#                       сколько секунд ждать её всего (3600)
#    MA_HEALTH_URL      свой адрес проверки вместо взятого из конфигурации
#    MA_SKIP_DB_BACKUP  1 — не копировать базу перед обновлением
#    MA_KEEP_DB_BACKUP  1 — не удалять копию базы после успешного обновления
#    MA_SKIP_OWNER_SCAN 1 — не обходить весь каталог данных в поисках файлов с
#                       чужим владельцем (проверяется только верхний уровень)
#    MA_PROGRESS_EVERY  раз в сколько секунд долгие шаги (этот обход, ожидание
#                       запуска службы) пишут, как идут дела (30)
#    MA_RELAX_SANDBOX   1 — ослабить защиту службы (контейнеры LXC/OpenVZ без
#                       вложенности, где служба падает со status=226/NAMESPACE):
#                       создаётся дополнение <unit>.d/50-relax-sandbox.conf
#    MA_WHEELS          каталог с пакетами .whl для обновления без интернета
#                       (по умолчанию — wheels/ рядом с исходниками, если есть)
#    MA_INDEX_URL       свой индекс пакетов (зеркало PyPI) для обновления из сети
#    MA_INSTALL_DIR, MA_CONFIG_DIR, MA_USER, MA_GROUP — если не заданы, берутся
#                       из действующего unit-файла службы
#    MA_DATA_DIR        каталог данных; если не задан — из конфигурации
#                       установленной версии, затем из unit-файла
#    MA_UNIT_FILE, MA_CLI_WRAPPER — пути unit-файла и команды mailarchiver
# ============================================================================
set -Eeuo pipefail

usage(){
  cat <<'EOF'
Использование: sudo bash scripts/update.sh [--check] [--force]
(запускать из каталога с НОВЫМИ исходниками)

  --check      только проверить, пройдёт ли обновление: служба не останавливается,
               на диске ничего не меняется. Код возврата 0 — обновлять можно.
  --force      не считать отказом то, что после установки проверяется ещё раз —
               уже с откатом: версию Python (requires-python), разбор модулей
               новой версии, проверку конфигурации новой версией и отсутствие
               файла базы. Такие отказы становятся предупреждениями с пометкой
               «--force». Остальные отказы (нет прав root, исходников или
               установки; пользователь службы и его доступ; каталог данных;
               место для копии; пакеты для обновления без сети) не обходятся.
               Можно вместе с --check.
  -h, --help   эта справка

Переменные окружения (все необязательны). Задаются ПОСЛЕ sudo:
    sudo MA_START_TIMEOUT=900 bash scripts/update.sh
(написанные перед sudo, они до скрипта не доходят: sudo очищает окружение)
  MA_START_TIMEOUT=N    сколько секунд ждать ответа веб-интерфейса (180)
  MA_START_TIMEOUT_MAX=N  если срок вышел, а служба ещё занята (первый запуск
                        обновляет большую базу), — сколько секунд ждать всего (3600)
  MA_HEALTH_URL=URL     свой адрес проверки вместо взятого из конфигурации
  MA_SKIP_DB_BACKUP=1   не копировать базу перед обновлением
  MA_KEEP_DB_BACKUP=1   не удалять копию базы после успешного обновления
  MA_SKIP_OWNER_SCAN=1  не обходить весь каталог данных в поисках файлов с чужим
                        владельцем (обход идёт до остановки службы; на большом
                        архиве — минуты); верхний уровень проверяется всё равно
  MA_PROGRESS_EVERY=N   раз в сколько секунд долгие шаги (этот обход, ожидание
                        запуска) пишут, как идут дела (30)
  MA_RELAX_SANDBOX=1    ослабить защиту службы: для контейнеров LXC/OpenVZ без
                        вложенности, где служба падает со status=226/NAMESPACE
  MA_WHEELS=КАТАЛОГ     пакеты .whl для обновления без интернета (по умолчанию —
                        каталог wheels рядом с исходниками, если он есть)
  MA_INDEX_URL=URL      свой индекс пакетов (зеркало PyPI) для обновления из сети
  MA_INSTALL_DIR, MA_CONFIG_DIR, MA_USER, MA_GROUP
                        если не заданы, берутся из действующего unit-файла службы
  MA_DATA_DIR=КАТАЛОГ   каталог данных; если не задан — из конфигурации
                        установленной версии, затем из unit-файла
  MA_UNIT_FILE, MA_CLI_WRAPPER
                        пути unit-файла и команды mailarchiver
EOF
}

# Параметры разбираются до установки ловушек: справка и опечатка в параметре —
# не «прерванное обновление», и до отката им дела нет.
# Режим проверки и --force — только параметрами. Переменной окружения для них
# нет: sudo по умолчанию очищает окружение (env_reset), и «ПЕРЕМЕННАЯ=1 sudo
# bash scripts/update.sh» молча выполняло бы настоящее обновление.
CHECK_ONLY=0
FORCE=0
for _arg in "$@"; do
  case "$_arg" in
    --check) CHECK_ONLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $_arg" >&2; usage >&2; exit 2 ;;
  esac
done

APP_NAME="mailarchiver"
UNIT_FILE="${MA_UNIT_FILE:-/etc/systemd/system/$APP_NAME.service}"
CLI_WRAPPER="${MA_CLI_WRAPPER:-/usr/local/bin/mailarchiver}"
LOG_PREFIX="[MailArchiver update]"
SCRIPT_NAME="update.sh"

# Каталоги и пользователь службы. Если установка делалась с MA_* (другой
# каталог, другой пользователь), при обновлении их приходилось повторять —
# иначе unit переписывался со значениями по умолчанию, и служба не запускалась.
# Теперь значения берутся из действующего unit-файла.
unit_val(){ [ -f "$UNIT_FILE" ] || return 0; sed -n "s/^$1=//p" "$UNIT_FILE" | head -n 1; }
_unit_wd="$(unit_val WorkingDirectory)"
_unit_cfg="$(unit_val 'Environment=MAILARCHIVER_CONFIG')"
# ReadWritePaths: первый путь, без знаков «-» (нет каталога — не ошибка) и «+»
# перед ним. С «-» путь получался «-/var/lib/…», и проверка каталога данных
# отказывала каждому обновлению.
_unit_paths=()
read -r -a _unit_paths <<< "$(unit_val ReadWritePaths)" || true
_unit_data="${_unit_paths[0]:-}"
while [ "${_unit_data#[-+]}" != "$_unit_data" ]; do _unit_data="${_unit_data#[-+]}"; done

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
# (каталог данных уточняется по конфигурации установленной версии — detect_data_dir)
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
DATA_DIR="$(strip_slash "$DATA_DIR")"

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/app_$STAMP"
VENV_BACKUP_PATH="$BACKUP_DIR/venv_$STAMP"
DB_BACKUP_PATH="$BACKUP_DIR/db_$STAMP"
DB_PATH=""            # файл базы (узнаём у новой версии перед её первым запуском)
FINISHED=0            # обновление доведено до конца — откат не нужен
TOUCHED=0             # служба уже остановлена: дальше отказ означает откат
ROLLED_BACK=0
WAS_ACTIVE=0          # работала ли служба до обновления (при сбое — вернуть её в строй)
SVC_FAILING_BEFORE=0  # проверка перед обновлением видела, что служба уже падает
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
  local ec="${1:-1}" db_failed=0 back_rc=0 reload=0
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
      reload=1
    fi
    # дополнение с ослабленной защитой — только если его создал этот запуск
    if remove_created_relax_sandbox; then reload=1; fi
    if [ "$reload" = "1" ]; then systemctl daemon-reload 2>/dev/null || true; fi
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
    wait_service_up 60 60 || back_rc=$?      # откат: ждём минуту, без продления
    if [ "$back_rc" = "0" ]; then
      warn "Откат выполнен. Служба работает, версия: $(installed_version)"
    else
      err "Откат выполнен (версия $(installed_version)), но служба НЕ поднялась — проверьте её вручную."
      # Только если проверка видела, что служба падала. По WAS_ACTIVE судить
      # нельзя: службу мог остановить сам администратор — исправную.
      if [ "$SVC_FAILING_BEFORE" = "1" ]; then
        err_more "До обновления служба тоже не работала: причина отказа, скорее всего, прежняя и не в обновлении."
      fi
      show_service_failure
    fi
  elif [ "$ROLLED_BACK" = "0" ]; then
    warn "Резервная копия не создана (или создана не полностью) — откатывать нечего."
    warn "Прежняя установка осталась на месте: $APP_DIR"
    # неполную копию убираем, чтобы её не приняли за годную
    rm -rf "$BACKUP_PATH" "$VENV_BACKUP_PATH" 2>/dev/null || true
    if remove_created_relax_sandbox; then systemctl daemon-reload 2>/dev/null || true; fi
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
  stop_owner_scan       # фоновый обход каталога данных Ctrl-C не получает
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

# ---------------------------------------------------------------------------
#  Проверка перед обновлением
# ---------------------------------------------------------------------------
# Всё, что можно проверить, пока служба работает и на диске ничего не тронуто.
# Одна и та же функция preflight выполняется и в режиме --check, и в начале
# обычного обновления. Раньше о конфигурации, которую не принимает новая
# версия, о неподходящем Python или о нехватке пакетов узнавали уже после
# остановки службы — ценой простоя и отката.
#
# Проверки ничего не создают и не меняют: Python запускается с ключом -B,
# исходники компилируются в памяти, каталог копий не создаётся.
PF_FAILED=0           # проверок, с которыми обновляться нельзя
PF_WARNED=0           # предупреждений (обновлению не мешают)
PF_FORCED=0           # отказов, пониженных до предупреждений параметром --force
pf_fail(){ PF_FAILED=$((PF_FAILED + 1)); err "$@"; }
pf_warn(){ PF_WARNED=$((PF_WARNED + 1)); warn "$@"; }
# (пояснения к отказу или предупреждению печатают err_more и warn_more — без знака)

# Отказ, который обходится параметром --force: то, что после установки
# проверяется ещё раз — уже с откатом (версия Python, разбор модулей,
# конфигурация, файл базы). С --force он становится предупреждением с пометкой.
# Остальные отказы --force не трогает: второй проверки за ними нет.
pf_soft_fail(){
  if [ "$FORCE" = "1" ]; then
    PF_FORCED=$((PF_FORCED + 1))
    pf_warn "--force: отказ понижен до предупреждения — $*"
  else
    pf_fail "$@"
  fi
}
pf_soft_more(){ if [ "$FORCE" = "1" ]; then warn_more "$@"; else err_more "$@"; fi; }
# (подробности такого отказа — туда же, куда его заголовок: stdout или stderr)
pf_soft_lines(){ if [ "$FORCE" = "1" ]; then cat; else cat >&2; fi; }

require_root(){ [ "$(id -u)" -eq 0 ] || { err "Запускайте с sudo."; exit 1; }; }

SRC_DIR=""
NEW_VERSION=""        # из файла VERSION новых исходников (пусто — прочитать не удалось)
detect_source(){
  local missing=() rel
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  [ -d "$SRC_DIR/mailarchiver" ] || { err "Не найдены новые исходники."; exit 1; }
  for rel in mailarchiver/__init__.py scripts/common.sh; do
    [ -f "$SRC_DIR/$rel" ] || missing+=("$rel")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    err "Не найдены новые исходники: в $SRC_DIR нет ${missing[*]}."
    err_more "Распакуйте архив новой версии целиком и запускайте scripts/update.sh из него."
    exit 1
  fi
  NEW_VERSION="$(source_version)"
}

check_installed(){
  [ -d "$APP_DIR" ] || { err "MailArchiver не установлен ($APP_DIR не найден). Используйте install.sh."; exit 1; }
  [ -x "$VENV_DIR/bin/python" ] || { err "Не найдено окружение $VENV_DIR. Используйте install.sh."; exit 1; }
  if ! venv_py -c 'import sys' >/dev/null 2>&1; then
    err "Python окружения не запускается ($VENV_DIR/bin/python) — так бывает после обновления Python в системе."
    err_more "Пересоздайте окружение установкой поверх: sudo bash scripts/install.sh"
    exit 1
  fi
  if [ ! -x "$VENV_DIR/bin/pip" ]; then
    err "В окружении нет pip ($VENV_DIR/bin/pip) — обновить пакет нечем."
    err_more "Пересоздайте окружение установкой поверх: sudo bash scripts/install.sh"
    exit 1
  fi
  # Файл на месте и исполняемый — ещё не значит, что pip запускается: после
  # обновления Python в системе его первая строка указывает на интерпретатор,
  # которого больше нет.
  if ! ( trap - ERR; cd / && PYTHONDONTWRITEBYTECODE=1 "$VENV_DIR/bin/pip" --version ) >/dev/null 2>&1; then
    err "pip окружения не запускается ($VENV_DIR/bin/pip --version) — так бывает после обновления Python в системе."
    err_more "Пересоздайте окружение установкой поверх: sudo bash scripts/install.sh"
    exit 1
  fi
}

# Каталог данных. MA_DATA_DIR — как сказано. Иначе — из конфигурации
# установленной версии: служба работает именно с ним. unit-файл — запасной
# источник: в ReadWritePaths бывает несколько путей, и первый из них — не
# обязательно каталог данных.
detect_data_dir(){
  local cfg=""
  if [ -n "${MA_DATA_DIR:-}" ]; then
    DATA_DIR_FROM="из переменной MA_DATA_DIR"
    return 0
  fi
  cfg="$(strip_slash "$(cfg_value data_dir)")"
  case "$cfg" in
    /*)
      if [ "$cfg" != "$DATA_DIR" ]; then
        if [ -n "$_unit_data" ]; then
          info "Каталог данных: $cfg — из конфигурации $CONFIG_FILE (в unit-файле первым в ReadWritePaths указан $_unit_data)."
        else
          info "Каталог данных: $cfg — из конфигурации $CONFIG_FILE (в unit-файле его нет)."
        fi
      fi
      DATA_DIR="$cfg"
      DATA_DIR_FROM="из конфигурации $CONFIG_FILE (paths.data_dir)" ;;
    *)
      # установленная версия не ответила (или она старая) — остаётся unit-файл
      if [ -n "$_unit_data" ]; then
        DATA_DIR_FROM="из unit-файла $UNIT_FILE (ReadWritePaths)"
      else
        DATA_DIR_FROM="по умолчанию: его не назвали ни MA_DATA_DIR, ни конфигурация, ни unit-файл"
      fi ;;
  esac
}

# Версии: какая стоит и какая в исходниках. Та же или более старая — не отказ,
# но обычно это ошибка (распаковали не тот архив).
pf_versions(){
  local old new="${NEW_VERSION:-?}" cmp
  old="$(installed_version)"
  if [ "$old" = "?" ]; then old="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"; fi
  if [ "$old" = "?" ] || [ "$new" = "?" ]; then
    info "Текущая версия: $old → новая версия: $new"
    return 0
  fi
  cmp="$(version_cmp "$old" "$new")"
  if [ "$cmp" = "0" ]; then
    pf_warn "Версия не меняется ($old → $new): обновление заново установит ту же версию."
  elif [ "$cmp" = "1" ]; then
    pf_warn "Понижение версии: установлена $old, в исходниках $new. Базу, которую уже обновила версия $old, более старая может не открыть."
  else
    ok "Текущая версия: $old → новая версия: $new"
  fi
}

# Исходники и установленная копия — один каталог (скрипт запустили из
# $APP_DIR/scripts). Без rsync копирование «самого в себя» заканчивается
# ошибкой уже после остановки службы.
#
# Исходники ВНУТРИ каталога установки (или он внутри них) — отказ всегда:
# rsync --delete стёр бы исходники посреди копирования, cp отказал бы «копировать
# каталог в самого себя» — в обоих случаях уже после остановки службы.
pf_same_dir(){
  local src app
  src="$(readlink -f -- "$SRC_DIR" 2>/dev/null || true)"
  app="$(readlink -f -- "$APP_DIR" 2>/dev/null || true)"
  if [ -n "$src" ] && [ -n "$app" ] && [ "$src" != "$app" ]; then
    case "$src/" in "$app"/*)
      pf_fail "Новые исходники ($SRC_DIR) лежат внутри каталога установки $APP_DIR: при обновлении его содержимое заменяется, и исходники были бы стёрты посреди копирования."
      err_more "Распакуйте архив новой версии в другой каталог (например в /root) и запускайте scripts/update.sh оттуда."
      return 0 ;;
    esac
    case "$app/" in "$src"/*)
      pf_fail "Каталог установки $APP_DIR лежит внутри каталога с новыми исходниками ($SRC_DIR): скопировать каталог в самого себя нельзя."
      err_more "Распакуйте архив новой версии в другой каталог (например в /root) и запускайте scripts/update.sh оттуда."
      return 0 ;;
    esac
  fi
  if [ ! "$SRC_DIR" -ef "$APP_DIR" ]; then return 0; fi
  if command -v rsync >/dev/null 2>&1; then
    pf_warn "Скрипт запущен из каталога установки ($APP_DIR): файлы не изменятся, пакет будет лишь установлен заново."
  else
    pf_fail "Скрипт запущен из каталога установки ($APP_DIR): новые исходники и установленная копия — один и тот же каталог."
    err_more "Распакуйте архив новой версии в другой каталог и запускайте scripts/update.sh оттуда."
  fi
}

pf_service_access(){
  local mp=""
  if check_service_access "$(dirname "$INSTALL_DIR")"; then
    ok "Пользователь службы $APP_USER: каталоги над установкой, конфигурация и каталог данных доступны."
    mp="$(unmounted_fstab_mount "$DATA_DIR")"
    if [ -n "$mp" ]; then
      PF_WARNED=$((PF_WARNED + 1))
      warn_data_dir_under_mount "$mp"
    fi
  else
    PF_FAILED=$((PF_FAILED + 1))
  fi
}

# В каталоге данных работающей установки должна быть база. Нет её — служба ни
# разу не запускалась, либо каталог не тот: например диск с данными не
# подключён, и обновление завело бы новую, пустую базу рядом с настоящей.
# Путь к базе — у установленной сейчас версии; не ответила — не отказ.
PF_DB=""              # файл базы (пусто — узнать не удалось)
pf_database(){
  PF_DB="$(cfg_value db_path)"
  if [ "$DATA_DIR_BAD" = "1" ]; then return 0; fi      # о каталоге данных уже сказано
  if [ -z "$PF_DB" ] || [ -f "$PF_DB" ]; then return 0; fi
  pf_soft_fail "В каталоге данных нет базы ($PF_DB): служба ещё ни разу не запускалась или это не тот каталог (диск не подключён?)."
  pf_soft_more "Проверьте, что данные на месте: ls \"$DATA_DIR/\"; findmnt   — если каталог не тот, подключите диск или задайте MA_DATA_DIR."
  if [ "$FORCE" != "1" ]; then
    err_more "Если базы и не должно быть (служба ещё не запускалась) — повторите с --force: sudo bash scripts/update.sh --force"
  fi
}

# Откуда pip возьмёт пакеты: каталог wheels (без сети) или PyPI.
pf_wheels(){
  local out="" rc=0
  if ! detect_wheels; then
    PF_FAILED=$((PF_FAILED + 1))
    return 0
  fi
  if [ -z "$WHEELS_DIR" ]; then
    if [ ! -f "$SRC_DIR/pyproject.toml" ] && [ ! -f "$SRC_DIR/setup.py" ]; then
      pf_fail "В новых исходниках нет pyproject.toml — pip не соберёт из них пакет. Распакуйте архив новой версии целиком."
    else
      info "Каталога wheels нет: библиотеки будут загружены из сети (PyPI). Для сервера без интернета — scripts/make_offline_bundle.sh."
      pf_index
    fi
    return 0
  fi
  if ! require_app_wheel "$NEW_VERSION"; then
    PF_FAILED=$((PF_FAILED + 1))
    return 0
  fi
  # Точный ответ даёт сам pip — пробной установкой; старый pip (до 22.2) её не
  # умеет, тогда остаётся оценка по именам файлов.
  out="$(wheels_dry_run "$NEW_VERSION" "$SRC_DIR/requirements.txt")" || rc=$?
  if [ "$rc" = "0" ]; then
    ok "Обновление без сети: пакеты ($(count_wheels "$WHEELS_DIR") шт.) будут взяты из $WHEELS_DIR — pip проверил пробной установкой, что в нём есть всё нужное."
  elif [ "$rc" = "2" ]; then
    if ! check_wheels_fit "$VENV_DIR/bin/python"; then PF_WARNED=$((PF_WARNED + 1)); fi
    info "pip окружения не умеет пробную установку (--dry-run появился в pip 22.2): подходят ли пакеты этому Python, точно выяснится при установке."
    ok "Обновление без сети: пакеты ($(count_wheels "$WHEELS_DIR") шт.) будут взяты из $WHEELS_DIR."
  else
    pf_fail "Обновление без сети не пройдёт: pip не может установить пакеты из $WHEELS_DIR (пробная установка, pip install --dry-run):"
    printf '%s\n' "$out" | sed 's/^/        /' >&2 || true
    err_more "В каталоге нет нужных пакетов под этот Python: архив собран под другую версию Python или архитектуру либо неполон."
    err_more "Соберите автономный архив на машине с той же версией Python и той же архитектурой, что у сервера: bash scripts/make_offline_bundle.sh"
    if ! check_wheels_fit "$VENV_DIR/bin/python"; then PF_WARNED=$((PF_WARNED + 1)); fi
  fi
}

# Обновление из сети: отвечает ли индекс пакетов. На сервере без интернета pip
# иначе зависал или отказывал уже после остановки службы. Только
# предупреждение: у pip могут быть зеркало, прокси или кэш, которых проверка не
# видит. Занимает не больше нескольких секунд.
pf_index(){
  local url shown reason=""
  url="$(pip_index_url)"
  # имя и пароль из адреса в вывод не попадают
  shown="$(printf '%s\n' "$url" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@#\1#')"
  if reason="$(index_probe "$url")"; then
    ok "Индекс пакетов отвечает: $shown"
  else
    pf_warn "Индекс пакетов $shown не отвечает${reason:+ ($reason)}: без доступа к нему pip не загрузит библиотеки — уже после остановки службы."
    warn_more "Для сервера без интернета соберите автономный архив (на машине с интернетом): bash scripts/make_offline_bundle.sh"
    warn_more "Если у pip есть зеркало, прокси или кэш, которых эта проверка не видит, предупреждение можно не учитывать;"
    warn_more "свой индекс задаётся так: sudo MA_INDEX_URL=https://зеркало/simple/ bash scripts/update.sh"
  fi
}

# Места должно хватить на копии кода и окружения. Проверяем до остановки
# службы: раньше нехватка обнаруживалась посреди копирования, когда служба уже
# стояла. Копия базы — отдельно и только предупреждением: без неё обновление
# идёт дальше (см. backup_database).
pf_free_space(){
  local need free db="" need_db=0 left
  need="$({ du -sk "$APP_DIR" "$VENV_DIR" 2>/dev/null || true; } | awk '{s+=$1} END{print s+0}')"
  free="$(free_kb "$BACKUP_DIR")"
  case "$free" in
    ''|*[!0-9]*)
      pf_warn "Не удалось узнать, сколько свободно в $INSTALL_DIR: для копии текущей установки нужно $((need / 1024 + 50)) МБ — проверьте сами (df -h)."
      return 0 ;;
  esac
  if [ "$free" -lt $((need + 51200)) ]; then
    pf_fail "Мало места в $BACKUP_DIR: для копии текущей установки нужно $((need / 1024 + 50)) МБ, свободно $((free / 1024)) МБ."
    err_more "Освободите место (старые копии: $BACKUP_DIR) и запустите обновление заново."
    return 0
  fi
  ok "Места для копии текущей установки хватает: нужно $((need / 1024 + 50)) МБ, свободно $((free / 1024)) МБ."

  if [ "${MA_SKIP_DB_BACKUP:-0}" = "1" ]; then
    info "Копия базы сниматься не будет (MA_SKIP_DB_BACKUP=1): при откате база останется как есть."
    return 0
  fi
  if [ "$DATA_DIR_BAD" = "1" ]; then return 0; fi      # о каталоге данных уже сказано
  db="$PF_DB"
  if [ -z "$db" ]; then
    pf_warn "Файл базы не найден (путь узнать не удалось: установленная версия не ответила) — обновление пройдёт без её копии."
    warn_more "Сделайте копию сами, если она нужна: файл mailarchiver.db в каталоге данных $DATA_DIR."
    return 0
  fi
  if [ ! -f "$db" ]; then return 0; fi                 # об отсутствии базы сказано в pf_database
  need_db="$({ du -sk "$db" "$db-wal" 2>/dev/null || true; } | awk '{s+=$1} END{print s+0}')"
  left=$((free - need))
  if [ "$left" -lt $((need_db + need_db / 10 + 10240)) ]; then
    pf_warn "Для копии базы ($((need_db / 1024)) МБ) места не хватит: после копии установки останется около $((left / 1024)) МБ."
    warn_more "Обновление пройдёт без копии базы — при откате она останется в том виде, в каком её оставит новая версия."
  else
    ok "Места для копии базы хватает ($((need_db / 1024)) МБ)."
  fi
}

# Коды проверок на Python ниже: 0 — годится, 3 — не годится, любой другой —
# проверить не удалось (это предупреждение, а не отказ: сомнение толкуется в
# пользу обновления, после установки всё проверяется ещё раз, с откатом).

# Подходит ли Python окружения условию requires-python. Разбирается простая
# запись вида «>=3.10» или «>=3.10,<3.15» — без библиотеки packaging, которой
# в окружении может не быть. В stdout — версия Python.
python_requirement_check(){
  venv_py - "$1" <<'PY'
import re
import sys

cur = tuple(sys.version_info[:3])
print("%d.%d.%d" % cur)


def pad(parts):
    return tuple(parts) + (0,) * (3 - len(parts))


for clause in (c.strip() for c in sys.argv[1].split(",")):
    if not clause:
        continue
    m = re.fullmatch(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+){0,2})(\.\*)?", clause)
    if not m:
        sys.exit(2)
    op, want, star = m.group(1), tuple(int(p) for p in m.group(2).split(".")), m.group(3)
    if (star and op not in ("==", "!=")) or (op == "~=" and len(want) < 2):
        sys.exit(2)
    if op == ">=":
        good = cur >= pad(want)
    elif op == "<=":
        good = cur <= pad(want)
    elif op == ">":
        good = cur > pad(want)
    elif op == "<":
        good = cur < pad(want)
    elif op == "==":
        good = cur[:len(want)] == want if star else cur == pad(want)
    elif op == "!=":
        good = cur[:len(want)] != want if star else cur != pad(want)
    else:                                   # ~=3.10 значит >=3.10 и ==3.*
        good = cur >= pad(want) and cur[:len(want) - 1] == want[:-1]
    if not good:
        sys.exit(3)
PY
}

pf_python_version(){
  local spec="" out="" rc=0
  if [ -f "$SRC_DIR/pyproject.toml" ]; then
    # Только из таблицы [project]: одноимённый ключ в другой таблице ([tool.…])
    # к пакету не относится.
    spec="$(awk '
        /^[[:space:]]*\[/ { inside = ($0 ~ /^[[:space:]]*\[[[:space:]]*project[[:space:]]*\][[:space:]]*(#.*)?$/); next }
        inside && /^[[:space:]]*requires-python[[:space:]]*=/ { print; exit }
      ' "$SRC_DIR/pyproject.toml" 2>/dev/null \
      | sed -n 's/^[[:space:]]*requires-python[[:space:]]*=[[:space:]]*["'\'']\([^"'\'']*\)["'\''].*$/\1/p' || true)"
    spec="${spec%%$'\n'*}"
  fi
  if [ -z "$spec" ]; then
    info "В pyproject.toml новой версии нет requires-python — версия Python не проверялась."
    return 0
  fi
  out="$(python_requirement_check "$spec" 2>/dev/null)" || rc=$?
  out="${out%%$'\n'*}"
  if [ "$rc" = "0" ]; then
    ok "Python окружения (${out:-?}) подходит новой версии (нужен $spec)."
  elif [ "$rc" = "3" ]; then
    pf_soft_fail "Python окружения (${out:-?}) не подходит новой версии: ей нужен Python $spec."
    pf_soft_more "Установите в системе подходящий Python и пересоздайте окружение установкой поверх: sudo bash scripts/install.sh"
  else
    info "Условие requires-python «$spec» разобрать не удалось — версия Python не проверялась."
  fi
}

# Каждый модуль новой версии компилируется Python'ом окружения — в памяти,
# файлы .pyc в исходники не пишутся. Ловит конструкции, которых этот Python
# ещё не знает. Проверяется пакет mailarchiver/ — то, что будет установлено.
# В stdout: число файлов, затем по строке на каждый негодный файл.
compile_sources(){
  venv_py - "$SRC_DIR/mailarchiver" <<'PY'
import os
import sys
import warnings

warnings.simplefilter("ignore")
root = sys.argv[1]
count, bad = 0, []
for base, dirs, files in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d != "__pycache__")
    for name in sorted(files):
        if not name.endswith(".py"):
            continue
        path = os.path.join(base, name)
        count += 1
        try:
            with open(path, "rb") as fh:
                compile(fh.read(), path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OSError) as exc:
            line = getattr(exc, "lineno", None)
            bad.append("%s%s: %s" % (os.path.relpath(path, os.path.dirname(root)),
                                     ":%s" % line if line else "", getattr(exc, "msg", None) or exc))
print(count)
for item in bad[:20]:
    print(item)
if len(bad) > 20:
    print("… и ещё %d" % (len(bad) - 20))
sys.exit(3 if bad else 0)
PY
}

pf_compile(){
  local out="" rc=0 count
  out="$(compile_sources 2>/dev/null)" || rc=$?
  count="${out%%$'\n'*}"
  if [ "$rc" = "0" ] && [ -n "$count" ] && [ "$count" != "0" ]; then
    ok "Модули новой версии ($count файлов .py) подходят Python окружения."
  elif [ "$rc" = "3" ]; then
    pf_soft_fail "Часть модулей новой версии не подходит Python окружения ($("$VENV_DIR/bin/python" -V 2>&1 || true)):"
    printf '%s\n' "$out" | tail -n +2 | sed 's/^/        /' | pf_soft_lines || true
    pf_soft_more "Нужен более новый Python (см. requires-python в pyproject.toml) — либо архив новой версии повреждён."
  else
    pf_warn "Проверить модули новой версии до установки не удалось — они будут проверены после установки пакета."
  fi
}

# check-config НОВОЙ версии на действующей конфигурации — Python'ом окружения,
# прямо из исходников, без установки. От root: каталог с исходниками службе
# может быть недоступен (архив распаковали в /root). check-config ничего не
# создаёт и не меняет. Вызывается та же функция, что и у команды mailarchiver
# (mailarchiver.__main__:main).
# Рабочий каталог — как у службы: относительные пути конфигурации (data_dir:
# "data") она считает от него, и проверка от корня находила бы «не те» каталоги.
new_config_check(){
  venv_py - "$SRC_DIR" "$CONFIG_FILE" "$APP_DIR" <<'PY'
import os
import sys

src, config = sys.argv[1], sys.argv[2]
try:
    os.chdir(sys.argv[3])
except OSError:
    pass
sys.argv = ["mailarchiver"]
sys.path.insert(0, src)
try:
    from mailarchiver.__main__ import main
except Exception as exc:        # например, новой версии нужна библиотека, которой ещё нет
    print("%s: %s" % (type(exc).__name__, exc))
    sys.exit(4)
try:
    code = main(["-c", config, "check-config"])
except SystemExit as exc:       # так выходит разбор параметров: команду не поняли
    code = None
    if exc.code not in (None, 0):
        print("check-config завершился с кодом %s" % (exc.code,))
        sys.exit(4)
sys.stdout.flush()
sys.exit(3 if code else 0)
PY
}

pf_new_config(){
  local out="" rc=0 line
  if [ ! -f "$CONFIG_FILE" ]; then
    pf_fail "Нет файла конфигурации $CONFIG_FILE. Если он лежит в другом месте — задайте MA_CONFIG_DIR."
    return 0
  fi
  # Каталог данных уже не прошёл проверку: настоящая программа отказала бы по
  # той же причине, и второй ✗ звал бы «исправить конфигурацию», в которой
  # ошибки нет.
  if [ "$DATA_DIR_BAD" = "1" ]; then
    info "Конфигурация новой версией не проверялась: сначала исправьте каталог данных (см. выше)."
    return 0
  fi
  out="$(new_config_check 2>&1)" || rc=$?
  if [ "$rc" = "3" ]; then
    # Проверка не состоялась по причине, не связанной с конфигурацией: новой
    # версии нужна библиотека, которой ещё нет в окружении, и т. п. Это не
    # «конфигурация не принята» — после установки пакета она проверяется снова.
    case "$out" in
      *"No module named"*|*"ImportError"*|*"AttributeError"*|*"has no attribute"*|*"Traceback (most recent call last)"*) rc=4 ;;
    esac
  fi
  if [ "$rc" = "0" ]; then
    ok "Новая версия принимает конфигурацию $CONFIG_FILE."
    while IFS= read -r line; do
      pf_warn "Замечание новой версии к конфигурации: $line"
    done < <(printf '%s\n' "$out" | sed -n 's/^Предупреждение:[[:space:]]*//p')
  elif [ "$rc" = "3" ]; then
    pf_soft_fail "Новая версия не принимает конфигурацию $CONFIG_FILE:"
    printf '%s\n' "$out" | sed 's/^/        /' | pf_soft_lines || true
    pf_soft_more "Исправьте конфигурацию и повторите. Служба пока работает на прежней версии."
  else
    line="$(printf '%s\n' "$out" | grep -m 1 -E 'No module named|ImportError|AttributeError|has no attribute' || true)"
    [ -n "$line" ] || line="${out##*$'\n'}"
    pf_warn "Проверить конфигурацию новой версией до установки не удалось${line:+ ($line)}."
    warn_more "Она будет проверена после установки пакета; если не подойдёт — обновление откатится."
  fi
}

# Служба падает ещё до обновления (systemd её перезапускает или сдался). Это
# не отказ — обновление может быть как раз лекарством, — но о нём надо сказать
# заранее: иначе после отката «служба не поднялась» читается как вина обновления.
#
# Служба в цикле перезапусков большую часть времени числится active/running —
# пока идёт очередная попытка запуска. Поэтому падающей считается и та, что
# «работает», но systemd её уже перезапускал (NRestarts), а адрес проверки
# молчит. Что служба падала, запоминается (SVC_FAILING_BEFORE) — для сообщения
# после отката.
PF_HEALTH_URL=""      # адрес проверки: MA_HEALTH_URL или из конфигурации (пусто — узнать не удалось)
pf_service_state(){
  local active sub tail restarts what=""
  active="$(svc_prop ActiveState)"; sub="$(svc_prop SubState)"
  case "$active/$sub" in
    failed/*|*/auto-restart*) what="состояние $active/$sub" ;;
    active/*)
      restarts="$(svc_prop NRestarts)"
      case "$restarts" in ''|*[!0-9]*) restarts=0 ;; esac
      if [ "$restarts" -gt 0 ] && [ -n "$PF_HEALTH_URL" ] && ! http_ok "$PF_HEALTH_URL"; then
        what="состояние $active/$sub, но systemd перезапускал её уже $restarts раз(а), а адрес проверки $PF_HEALTH_URL не отвечает"
      fi ;;
  esac
  if [ -z "$what" ]; then return 0; fi
  SVC_FAILING_BEFORE=1
  pf_warn "Служба не работает уже сейчас, до обновления ($what)."
  tail="$(journal_tail 8)"
  if [ -n "$tail" ]; then
    warn_more "Последние строки её журнала (journalctl -u $APP_NAME):"
    printf '%s\n' "$tail" || true
  fi
  warn_more "Если причина не в самой программе (нет каталога, занят порт), новая версия тоже не запустится."
}

# Адрес, по которому после обновления будет проверяться новая версия, и
# отвечает ли по нему служба сейчас. Не отвечает работающая — не ответит и
# обновлённая: обновление откатилось бы, хотя служба исправна.
pf_health(){
  local url="$PF_HEALTH_URL" reason=""
  if [ "$SVC_FAILING_BEFORE" = "1" ] || ! systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    info "Служба сейчас не работает — адрес проверки (${url:-узнать не удалось}) будет опрошен после обновления."
    return 0
  fi
  if [ -z "$url" ]; then
    pf_warn "Адрес проверки веб-интерфейса узнать не удалось: после обновления о службе можно будет судить только по тому, что она не упала."
    warn_more "Задайте его: MA_HEALTH_URL=http://адрес:порт/health"
  elif reason="$(http_probe "$url")"; then
    ok "Служба работает и отвечает по адресу проверки: $url"
  else
    pf_warn "Служба работает, но адрес проверки не отвечает: $url${reason:+ ($reason)}."
    warn_more "По нему же обновление будет ждать новую версию — и откатится, не дождавшись ответа."
    warn_more "Задайте верный адрес: MA_HEALTH_URL=http://адрес:порт/health"
  fi
}

pf_environment(){
  local kind file
  if command -v rsync >/dev/null 2>&1; then
    info "rsync найден — файлы будут обновлены им."
  else
    info "rsync не найден — файлы будут скопированы командой cp (так тоже работает)."
  fi
  file="$(relax_dropin)"
  kind="$(container_type)"
  if relax_dropin_active "$file"; then
    info "Служба работает с ослабленной защитой: действует дополнение $file."
  elif [ -e "$file" ] || [ -L "$file" ]; then
    if [ "${MA_RELAX_SANDBOX:-0}" = "1" ]; then
      pf_warn "MA_RELAX_SANDBOX=1: по пути дополнения $file уже что-то лежит, и оно отключено (пустой файл или ссылка на /dev/null) — защита службы ослаблена НЕ будет."
      warn_more "Чтобы ослабить защиту, удалите его (rm \"$file\") и запустите скрипт заново."
    fi
  elif [ "${MA_RELAX_SANDBOX:-0}" = "1" ]; then
    info "MA_RELAX_SANDBOX=1: при обновлении будет создано дополнение $file (ослабленная защита службы)."
  elif [ -n "${MA_RELAX_SANDBOX:-}" ] && [ "$MA_RELAX_SANDBOX" != "0" ]; then
    # (и совет «повторите с MA_RELAX_SANDBOX=1» для контейнера здесь не нужен:
    # переменную уже задали, только неверно)
    pf_warn "MA_RELAX_SANDBOX=${MA_RELAX_SANDBOX}: значение не понято, защита службы меняться не будет."
    warn_more "Чтобы ослабить её, задайте MA_RELAX_SANDBOX=1; чтобы оставить как есть — уберите переменную."
  elif [ -n "$kind" ]; then
    info "Сервер — контейнер ($kind). Если служба после обновления не запустится со status=226/NAMESPACE,"
    info "    повторите обновление с MA_RELAX_SANDBOX=1 (или включите вложенность — nesting — у контейнера)."
  fi
}

preflight(){
  require_root
  detect_source
  check_installed
  ok "Новые исходники: $SRC_DIR; установка: $INSTALL_DIR."
  detect_data_dir
  PF_HEALTH_URL="${MA_HEALTH_URL:-}"
  [ -n "$PF_HEALTH_URL" ] || PF_HEALTH_URL="$(cfg_value health_url)"
  pf_versions
  pf_same_dir
  pf_service_access
  pf_database
  pf_service_state
  pf_wheels
  pf_free_space
  pf_python_version
  pf_compile
  pf_new_config
  pf_health
  pf_environment
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
  if [ -n "$WHEELS_DIR" ]; then
    # Обновление без сети: репозиторий дистрибутива, скорее всего, недоступен
    # так же, как PyPI, а служба в эту минуту остановлена — не ждём его.
    warn "Нет ${missing[*]} (нужны только для копии архива на сервер по SSH); при обновлении без сети они не доустанавливаются."
    return 0
  fi
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
  # Пока ничего не тронуто: исходники, установка, пользователь службы и его
  # доступ к каталогам, место, версия Python, конфигурация, адрес проверки.
  if [ "$CHECK_ONLY" = "1" ]; then
    info "Проверка перед обновлением (служба не останавливается, на диске ничего не меняется)…"
  else
    info "Проверка перед обновлением…"
  fi
  preflight
  if [ "$PF_FORCED" -gt 0 ]; then
    info "--force: отказов, пониженных до предупреждений, — $PF_FORCED (строки с пометкой «--force» выше)."
    info "    После установки это проверяется ещё раз; не пройдёт — обновление откатится."
  fi
  if [ "$PF_FAILED" -gt 0 ]; then
    err "Проверка не пройдена: ошибок — $PF_FAILED, предупреждений — $PF_WARNED. Ничего не изменено, служба не останавливалась."
    err_more "Исправьте отмеченное знаком ✗ и запустите скрипт заново."
    exit 1
  fi
  if [ "$CHECK_ONLY" = "1" ]; then
    FINISHED=1       # проверка доведена до конца — это не прерванное обновление
    local again="sudo bash scripts/update.sh"
    # без --force обновление остановится на тех же отказах, что проверка пропустила
    if [ "$PF_FORCED" -gt 0 ]; then again="$again --force"; fi
    if [ "$PF_WARNED" -gt 0 ]; then
      ok "Проверка пройдена, предупреждений — $PF_WARNED (отмечены знаком ⚠). Обновление: $again"
    else
      ok "Проверка пройдена — можно обновлять: $again"
    fi
    return 0
  fi
  if [ "$PF_WARNED" -gt 0 ]; then
    ok "Проверка пройдена, предупреждений — $PF_WARNED (отмечены знаком ⚠)."
  else
    ok "Проверка пройдена."
  fi

  # Каталог копий создаётся только здесь: режим --check его не заводит.
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"

  # Владелец файлов в каталоге данных. Весь архив обходим, пока служба ещё
  # работает: каждое письмо — отдельный файл, и на большом архиве это минуты,
  # а после остановки службы они становились минутами простоя — без единой
  # строки на экране (обновление принимали за зависшее). Файлы, которые служба
  # создаёт тем временем, и так её. После остановки — только верхний уровень.
  scan_data_owner_early

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
      --exclude '/wheels' "$SRC_DIR"/ "$APP_DIR"/
  else
    rm -rf "$APP_DIR"/mailarchiver
    cp -a "$SRC_DIR"/. "$APP_DIR"/ || { err "Не удалось скопировать новые файлы."; exit 1; }
    rm -rf "$APP_DIR/venv" "$APP_DIR/.git" "$APP_DIR/wheels" 2>/dev/null || true
  fi
  # (каталог wheels в установку не копируется: пакеты нужны только pip, а
  # каждая резервная копия кода иначе росла бы на их размер)
  clean_build_leftovers
  # Права исходников (их принёс rsync -a / cp -a) приводим к своим: см. common.sh.
  normalize_app_perms

  info "Обновление зависимостей…"
  # Версии библиотек берём из requirements.txt, а пакет ставим с --no-deps:
  # `pip install --upgrade "$APP_DIR"` обновлял зависимости по нестрогим
  # границам pyproject.toml, и в систему приезжали непроверенные версии.
  #
  # Порядок: сначала пакет, потом библиотеки. Наоборот pip сверял новые
  # библиотеки с требованиями ЕЩЁ ПРЕЖНЕГО пакета и печатал «ERROR: pip's
  # dependency resolver… mailarchiver 1.2.0 requires IMAPClient<4» — ошибка
  # мнимая, но выглядела как сбой обновления.
  if [ -n "$WHEELS_DIR" ]; then
    # Без сети: pip не обновляем (его новой версии в каталоге нет), приложение
    # ставим из его пакета — сборка из исходников потребовала бы скачать
    # setuptools. --force-reinstall: и та же версия ставится заново, из пакета.
    info "Обновление без сети: пакеты берутся из $WHEELS_DIR."
    "$VENV_DIR/bin/pip" install -q "${PIP_OFFLINE[@]}" --no-deps --upgrade --force-reinstall "mailarchiver==$NEW_VERSION" \
      || { err "Не удалось установить пакет mailarchiver $NEW_VERSION из $WHEELS_DIR (сообщение pip выше)."; exit 1; }
    if [ -f "$APP_DIR/requirements.txt" ]; then
      "$VENV_DIR/bin/pip" install -q "${PIP_OFFLINE[@]}" -r "$APP_DIR/requirements.txt" \
        || { err "В каталоге $WHEELS_DIR не нашлось всех библиотек из requirements.txt под этот Python (сообщение pip выше)."
             err "Автономный архив собирают на машине с той же версией Python и той же архитектурой, что у сервера."
             exit 1; }
    fi
  else
    "$VENV_DIR/bin/pip" install --upgrade pip -q || true
    "$VENV_DIR/bin/pip" install -q --no-deps --upgrade "$APP_DIR"
    if [ -f "$APP_DIR/requirements.txt" ]; then
      "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
    fi
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
  # служба может прочитать конфигурацию. Владельца в каталоге данных здесь
  # проверяем только на верхнем уровне (база, ключ, каталоги): весь архив
  # обойдён до остановки службы — см. выше и fix_data_owner.
  as_app_user "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" check-config \
    || { err "Новая версия не приняла конфигурацию $CONFIG_FILE (сообщение выше)."; exit 1; }
  fix_data_owner 1

  info "Пробный импорт веб-приложения от имени службы…"
  check_app_import || exit 1

  # обновить unit, если изменился шаблон. Свои правки (MemoryMax и т.п.)
  # держите в /etc/systemd/system/mailarchiver.service.d/override.conf —
  # сам unit перезаписывается при каждом обновлении.
  local reload_units=0
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    render_unit "$APP_DIR/systemd/mailarchiver.service"
    reload_units=1
  fi
  # MA_RELAX_SANDBOX=1 — дополнение с ослабленной защитой (для контейнеров)
  apply_relax_sandbox
  if [ "$reload_units" = "1" ] || [ "$RELAX_CREATED" = "1" ]; then systemctl daemon-reload; fi

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
