#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт удаления
#  Останавливает и удаляет службу и файлы приложения. Данные (БД и локальные
#  копии писем) и конфигурация по умолчанию СОХРАНЯЮТСЯ — их удаление нужно
#  подтвердить отдельно (флаг --purge или интерактивный вопрос).
#
#  Запуск:  sudo bash scripts/uninstall.sh            # удалить, данные оставить
#           sudo bash scripts/uninstall.sh --purge    # удалить всё, включая данные
# ============================================================================
# Без -e: удаление — «по возможности». Сбой одного шага (служба уже удалена,
# каталога нет) не должен оставлять половину установки; ловушка ERR сообщает
# о каждом сбое, и скрипт идёт дальше — как и обещает её текст.
set -Euo pipefail

APP_NAME="mailarchiver"
APP_USER="${MA_USER:-mailarchiver}"
INSTALL_DIR="${MA_INSTALL_DIR:-/opt/mailarchiver}"
CONFIG_DIR="${MA_CONFIG_DIR:-/etc/mailarchiver}"
DATA_DIR="${MA_DATA_DIR:-/var/lib/mailarchiver}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
LOG_PREFIX="[MailArchiver uninstall]"

if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*"; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*"; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*"; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2; }

trap 'err "Ошибка на строке ${BASH_LINENO[0]} (продолжаю по возможности)."' ERR

PURGE=0
REMOVE_USER=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --remove-user) REMOVE_USER=1 ;;
    -h|--help) echo "Использование: sudo bash uninstall.sh [--purge] [--remove-user]"; exit 0 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { err "Запускайте с sudo."; exit 1; }

info "Останавливаю и отключаю службу…"
systemctl stop "$APP_NAME" 2>/dev/null || warn "Служба не запущена"
systemctl disable "$APP_NAME" 2>/dev/null || true
if [ -f "$SERVICE_FILE" ]; then rm -f "$SERVICE_FILE"; systemctl daemon-reload 2>/dev/null || true; ok "Служба удалена."; fi

if [ -f /usr/local/bin/mailarchiver ] && grep -q "MailArchiver CLI" /usr/local/bin/mailarchiver 2>/dev/null; then
  rm -f /usr/local/bin/mailarchiver && ok "Команда mailarchiver удалена."
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
  echo "    Данные:       $DATA_DIR"
  echo "    Конфигурация: $CONFIG_DIR (там же обычно лежит ключ шифрования storage.key)"
  read -r -p "    Удалить безвозвратно? [y/N] " ans
  case "$ans" in y|Y|yes|да) PURGE=1 ;; esac
fi

if [ "$PURGE" = "1" ]; then
  info "Удаляю данные и конфигурацию…"
  rm -rf "$DATA_DIR" "$CONFIG_DIR"
  ok "Данные и конфигурация удалены."
  REMOVE_USER=1
else
  warn "Данные сохранены: $DATA_DIR"
  warn "Конфигурация сохранена: $CONFIG_DIR"
  echo "    Для полного удаления запустите: sudo bash uninstall.sh --purge"
fi

if [ "$REMOVE_USER" = "1" ] && id "$APP_USER" >/dev/null 2>&1; then
  info "Удаляю пользователя $APP_USER…"
  userdel "$APP_USER" 2>/dev/null || warn "Не удалось удалить пользователя (возможно, есть его процессы/файлы)."
fi

echo ""
ok "Удаление MailArchiver завершено."
