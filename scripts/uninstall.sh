#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт удаления
#  Останавливает и удаляет службу и файлы приложения. Данные (БД и локальные
#  копии писем) и конфигурация по умолчанию СОХРАНЯЮТСЯ — их удаление нужно
#  подтвердить отдельно (флаг --purge или интерактивный вопрос).
#
#  Запуск:  sudo bash scripts/uninstall.sh                # удалить, данные оставить
#           sudo bash scripts/uninstall.sh --purge        # удалить всё, включая данные
#           sudo bash scripts/uninstall.sh --remove-user  # и пользователя службы
#           bash scripts/uninstall.sh --help              # справка
#
#  Если установка делалась с переменными MA_*, задайте те же при удалении
#  (ПОСЛЕ sudo: sudo MA_DATA_DIR=/srv/mail bash scripts/uninstall.sh):
#    MA_INSTALL_DIR, MA_CONFIG_DIR, MA_DATA_DIR, MA_USER — каталоги и пользователь
#    MA_UNIT_FILE, MA_CLI_WRAPPER — пути unit-файла и команды mailarchiver
#
#  Каталог данных (и конфигурации), который на деле — ссылка на другой диск,
#  при --purge удаляется вместе с каталогом, на который она ведёт. Если диск не
#  подключён, удаляется только ссылка, и об оставшихся данных сказано прямо.
#
#  Вместе с unit-файлом удаляется дополнение <unit>.d/50-relax-sandbox.conf
#  (его создаёт MA_RELAX_SANDBOX=1); другие дополнения из каталога <unit>.d —
#  правки администратора — остаются на месте.
# ============================================================================
# Без -e: удаление — «по возможности». Сбой одного шага (служба уже удалена,
# каталога нет) не должен оставлять половину установки; ловушка ERR сообщает
# о каждом сбое, и скрипт идёт дальше — как и обещает её текст.
set -Euo pipefail

usage(){
  cat <<'EOF'
Использование: sudo bash scripts/uninstall.sh [--purge] [--remove-user]

  --purge        удалить также данные (письма, база) и конфигурацию — безвозвратно
  --remove-user  удалить и пользователя службы
  -h, --help     эта справка

Без --purge данные и конфигурация остаются (в терминале скрипт спросит о них).

Переменные окружения — те же, что задавались при установке. Задаются ПОСЛЕ sudo:
    sudo MA_DATA_DIR=/srv/mail bash scripts/uninstall.sh --purge
(написанные перед sudo, они до скрипта не доходят: sudo очищает окружение)
  MA_INSTALL_DIR, MA_CONFIG_DIR, MA_DATA_DIR, MA_USER
  MA_UNIT_FILE, MA_CLI_WRAPPER
EOF
}

# Параметры — до любых действий. Незнакомый параметр — отказ: раньше он молча
# пропускался, и «uninstall.sh --check» (по привычке от update.sh) удалял
# установку.
PURGE=0
REMOVE_USER=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --remove-user) REMOVE_USER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Неизвестный параметр: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

APP_NAME="mailarchiver"
APP_USER="${MA_USER:-mailarchiver}"
INSTALL_DIR="${MA_INSTALL_DIR:-/opt/mailarchiver}"
CONFIG_DIR="${MA_CONFIG_DIR:-/etc/mailarchiver}"
DATA_DIR="${MA_DATA_DIR:-/var/lib/mailarchiver}"
SERVICE_FILE="${MA_UNIT_FILE:-/etc/systemd/system/${APP_NAME}.service}"
RELAX_DROPIN="$SERVICE_FILE.d/50-relax-sandbox.conf"
CLI_WRAPPER="${MA_CLI_WRAPPER:-/usr/local/bin/mailarchiver}"
LOG_PREFIX="[MailArchiver uninstall]"

if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*"; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*"; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*"; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2; }

# продолжение сообщения (что делать) — без знака, как в install.sh и update.sh
warn_more(){ echo -e "${YLW}${LOG_PREFIX}${NC}     $*"; }
err_more(){ echo -e "${RED}${LOG_PREFIX}${NC}     $*" >&2; }

trap 'err "Ошибка на строке ${BASH_LINENO[0]} (продолжаю по возможности)."' ERR

# Путь без «/» на конце: с косой чертой «[ -L ]» для ссылки ложно.
strip_slash(){
  local p="$1"
  while [ "$p" != "/" ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  printf '%s\n' "$p"
}
DATA_DIR="$(strip_slash "$DATA_DIR")"
CONFIG_DIR="$(strip_slash "$CONFIG_DIR")"

# Почему каталог удалять нельзя (пусто — можно). $1 — путь, куда он ведёт на
# самом деле. Защита от MA_DATA_DIR=/ и от ссылки, ведущей в корень или в
# каталог верхнего уровня: «rm -rf» по такому пути стёр бы не данные, а систему.
purge_refusal(){
  local real="$1"
  case "$real" in
    '')   echo "не удалось выяснить, куда ведёт путь" ;;
    /)    echo "это корень файловой системы" ;;
    /*/*) ;;
    /*)   echo "это каталог верхнего уровня ($real), а не каталог программы" ;;
    *)    echo "путь задан не от корня" ;;
  esac
}

# Что --purge сделает с каталогом — одной строкой, для вопроса перед удалением.
purge_plan(){
  local path="$1" real why
  real="$(readlink -m -- "$path" 2>/dev/null || true)"
  if [ -L "$path" ] && [ ! -e "$path" ]; then
    echo "$path — ссылка на $real, а такого каталога сейчас нет (диск не подключён?): будет удалена только ссылка, данные на том диске останутся"
  elif [ ! -e "$path" ]; then
    echo "$path — такого каталога нет"
  elif [ ! -d "$path" ]; then
    echo "$path — не каталог: будет удалён"
  else
    why="$(purge_refusal "$real")"
    if [ -n "$why" ]; then
      echo "$path НЕ будет удалён: $why"
    elif [ -L "$path" ]; then
      echo "$path — ссылка: будут удалены каталог $real и сама ссылка"
    else
      echo "$path"
    fi
  fi
}

# Удалить каталог данных или конфигурации. Ссылка раскрывается: «rm -rf» по
# ссылке удалял только её, данные оставались на другом диске, а скрипт сообщал
# «Данные и конфигурация удалены».
PURGE_FAILED=0
purge_dir(){   # purge_dir НАЗВАНИЕ ПУТЬ
  local label="$1" path="$2" real why
  real="$(readlink -m -- "$path" 2>/dev/null || true)"
  if [ -L "$path" ] && [ ! -e "$path" ]; then
    if ! rm -f -- "$path"; then
      PURGE_FAILED=1
      err "$label: не удалось удалить ссылку $path."
      err_more "Причина — в сообщении rm выше; удалите её сами: rm -f \"$path\""
      return 0
    fi
    # Удалено не всё: данные остались на неподключённом диске. Пользователя службы
    # не удаляем — иначе эти файлы остались бы с владельцем, которого больше нет.
    PURGE_FAILED=1
    err "$label $path — ссылка на $real, а такого каталога сейчас нет (диск не подключён?): удалена только ссылка."
    err_more "То, что лежит на том диске, НЕ удалено. Подключите его и удалите сами: rm -rf \"$real\""
    return 0
  fi
  if [ ! -e "$path" ]; then
    info "$label: $path — такого каталога нет, удалять нечего."
    return 0
  fi
  if [ ! -d "$path" ]; then
    if rm -f -- "$path"; then
      ok "$label: $path удалён (это был не каталог)."
    else
      PURGE_FAILED=1
      err "$label: не удалось удалить $path."
      err_more "Причина — в сообщении rm выше; удалите его сами: rm -f \"$path\""
    fi
    return 0
  fi
  why="$(purge_refusal "$real")"
  if [ -n "$why" ]; then
    PURGE_FAILED=1
    if [ "$real" != "$path" ]; then err "$label НЕ удалён: $path ведёт в $real — $why."
    else err "$label НЕ удалён: $path — $why."; fi
    err_more "Если это действительно каталог MailArchiver, удалите его сами — проверив путь."
    return 0
  fi
  if [ -L "$path" ]; then
    if rm -rf -- "$real" && rm -f -- "$path"; then
      ok "$label удалён: каталог $real и ссылка на него $path."
    else
      PURGE_FAILED=1
      err "$label удалён не полностью: каталог $real (на него ведёт ссылка $path)."
      err_more "Причина — в сообщении rm выше; удалите остатки сами: rm -rf \"$real\" \"$path\""
    fi
  elif rm -rf -- "$path"; then
    ok "$label удалён: $path."
  else
    PURGE_FAILED=1
    err "$label удалён не полностью: $path."
    err_more "Причина — в сообщении rm выше; удалите остатки сами: rm -rf \"$path\""
  fi
}

[ "$(id -u)" -eq 0 ] || { err "Запускайте с sudo."; exit 1; }

info "Останавливаю и отключаю службу…"
systemctl stop "$APP_NAME" 2>/dev/null || warn "Служба не запущена"
systemctl disable "$APP_NAME" 2>/dev/null || true
UNIT_REMOVED=0
RELOAD=0
if [ -f "$SERVICE_FILE" ]; then rm -f "$SERVICE_FILE"; UNIT_REMOVED=1; RELOAD=1; fi
# Дополнение с ослабленной защитой создают install.sh/update.sh — убираем и его.
# Каталог <unit>.d удаляем, только если он после этого пуст: в нём могут лежать
# правки администратора (systemctl edit), их не трогаем.
if [ -f "$RELAX_DROPIN" ]; then
  rm -f "$RELAX_DROPIN"
  RELOAD=1
  ok "Дополнение с ослабленной защитой удалено: $RELAX_DROPIN"
fi
if [ -d "$SERVICE_FILE.d" ]; then
  rmdir "$SERVICE_FILE.d" 2>/dev/null \
    || warn "В $SERVICE_FILE.d остались ваши дополнения к unit-файлу — удалите каталог сами, если они не нужны."
fi
if [ "$RELOAD" = "1" ]; then systemctl daemon-reload 2>/dev/null || true; fi
if [ "$UNIT_REMOVED" = "1" ]; then ok "Служба удалена."; fi

if [ -f "$CLI_WRAPPER" ] && grep -q "MailArchiver CLI" "$CLI_WRAPPER" 2>/dev/null; then
  rm -f "$CLI_WRAPPER" && ok "Команда mailarchiver удалена."
fi

if [ -d "$INSTALL_DIR" ]; then
  info "Удаляю файлы приложения ($INSTALL_DIR)…"
  rm -rf "$INSTALL_DIR"
  ok "Файлы приложения удалены."
fi

# Данные и конфигурация
if [ "$PURGE" != "1" ] && [ -t 0 ]; then
  echo ""
  warn "Удалить также ДАННЫЕ (локальные копии писем и БД) и конфигурацию?"
  # показываем то, что будет удалено на самом деле: путь может быть ссылкой
  echo "    Данные:       $(purge_plan "$DATA_DIR")"
  echo "    Конфигурация: $(purge_plan "$CONFIG_DIR")"
  echo "                  (в каталоге конфигурации обычно лежит и ключ шифрования storage.key)"
  ans=""
  read -r -p "    Удалить безвозвратно? [y/N] " ans
  case "$ans" in y|Y|yes|да) PURGE=1 ;; esac
fi

if [ "$PURGE" = "1" ]; then
  info "Удаляю данные и конфигурацию…"
  purge_dir "Каталог данных" "$DATA_DIR"
  purge_dir "Каталог конфигурации" "$CONFIG_DIR"
  # пользователя службы оставляем, если его файлы остались на месте
  if [ "$PURGE_FAILED" = "0" ]; then REMOVE_USER=1; fi
else
  warn "Данные сохранены: $DATA_DIR"
  warn "Конфигурация сохранена: $CONFIG_DIR"
  echo "    Для полного удаления запустите: sudo bash scripts/uninstall.sh --purge"
fi

if [ "$REMOVE_USER" = "1" ] && id "$APP_USER" >/dev/null 2>&1; then
  info "Удаляю пользователя $APP_USER…"
  userdel "$APP_USER" 2>/dev/null || warn "Не удалось удалить пользователя (возможно, есть его процессы/файлы)."
fi

echo ""
if [ "$PURGE_FAILED" = "1" ]; then
  warn "Удаление MailArchiver завершено, но удалено не всё: см. строки со знаком ✗ выше — там сказано, что осталось и почему."
  exit 1
fi
ok "Удаление MailArchiver завершено."
