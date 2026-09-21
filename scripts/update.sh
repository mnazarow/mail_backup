#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт обновления
#  Делает резервную копию текущей установки, заменяет код, обновляет
#  зависимости и перезапускает службу. При ошибке — автоматический откат к
#  предыдущей версии. Данные (БД, письма) и конфигурация НЕ трогаются.
#
#  Запуск (из каталога с НОВЫМИ исходниками):  sudo bash scripts/update.sh
# ============================================================================
set -Eeuo pipefail

APP_NAME="mailarchiver"
INSTALL_DIR="${MA_INSTALL_DIR:-/opt/mailarchiver}"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="${MA_CONFIG_DIR:-/etc/mailarchiver}"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
BACKUP_DIR="$INSTALL_DIR/backups"
APP_USER="${MA_USER:-mailarchiver}"
APP_GROUP="${MA_GROUP:-mailarchiver}"
DATA_DIR="${MA_DATA_DIR:-/var/lib/mailarchiver}"
LOG_PREFIX="[MailArchiver update]"

if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*"; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*"; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*"; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2; }

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_PATH="$BACKUP_DIR/app_$STAMP"
ROLLED_BACK=0

rollback(){
  local ec=$?
  err "Обновление прервано (код $ec)."
  if [ -d "$BACKUP_PATH" ] && [ "$ROLLED_BACK" = "0" ]; then
    warn "Откат к предыдущей версии…"
    rm -rf "$APP_DIR"
    mv "$BACKUP_PATH" "$APP_DIR"
    systemctl restart "$APP_NAME" 2>/dev/null || true
    ROLLED_BACK=1
    warn "Откат выполнен. Служба перезапущена на прежней версии."
  fi
  exit "$ec"
}
trap rollback ERR

require_root(){ [ "$(id -u)" -eq 0 ] || { err "Запускайте с sudo."; exit 1; }; }

detect_source(){
  local sd; sd="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SRC_DIR="$(cd "$sd/.." && pwd)"
  [ -d "$SRC_DIR/mailarchiver" ] || { err "Не найдены новые исходники."; exit 1; }
}

check_installed(){
  [ -d "$APP_DIR" ] || { err "MailArchiver не установлен ($APP_DIR не найден). Используйте install.sh."; exit 1; }
}

show_versions(){
  local old new
  old="$(cat "$APP_DIR/VERSION" 2>/dev/null || echo '?')"
  new="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo '?')"
  info "Текущая версия: $old → новая версия: $new"
}

main(){
  require_root
  detect_source
  check_installed
  show_versions

  info "Останавливаю службу…"
  systemctl stop "$APP_NAME" 2>/dev/null || warn "Служба не была запущена"

  info "Резервная копия текущей установки → $BACKUP_PATH"
  mkdir -p "$BACKUP_DIR"
  cp -a "$APP_DIR" "$BACKUP_PATH"

  info "Обновление файлов…"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      "$SRC_DIR"/ "$APP_DIR"/
  else
    rm -rf "$APP_DIR"/mailarchiver
    cp -a "$SRC_DIR"/. "$APP_DIR"/
    rm -rf "$APP_DIR/venv" "$APP_DIR/.git" 2>/dev/null || true
  fi

  info "Обновление зависимостей…"
  "$VENV_DIR/bin/pip" install --upgrade pip -q || true
  # Как и в install.sh: версии библиотек берём из requirements.txt, а пакет
  # ставим с --no-deps. Прежний `pip install --upgrade "$APP_DIR"` обновлял
  # зависимости по нестрогим границам pyproject.toml, из-за чего при обычном
  # обновлении в систему приезжали непроверенные версии библиотек.
  if [ -f "$APP_DIR/requirements.txt" ]; then
    "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"
  fi
  "$VENV_DIR/bin/pip" install -q --no-deps --upgrade "$APP_DIR"

  info "Проверка импорта приложения…"
  "$VENV_DIR/bin/python" -c "import mailarchiver, fastapi, uvicorn, jinja2, yaml, apscheduler, imapclient, cryptography" \
    || { err "После обновления приложение не импортируется."; exit 1; }

  info "Проверка конфигурации…"
  "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" check-config

  # обновить unit, если изменился шаблон
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    sed -e "s|@VENV@|$VENV_DIR|g" -e "s|@APPDIR@|$APP_DIR|g" -e "s|@CONFIG@|$CONFIG_FILE|g" \
        -e "s|@USER@|$APP_USER|g" -e "s|@GROUP@|$APP_GROUP|g" -e "s|@DATADIR@|$DATA_DIR|g" \
        "$APP_DIR/systemd/mailarchiver.service" > /etc/systemd/system/mailarchiver.service
    systemctl daemon-reload
  fi

  chown -R root:root "$APP_DIR"

  info "Запуск службы…"
  systemctl start "$APP_NAME"
  sleep 3
  if ! systemctl is-active --quiet "$APP_NAME"; then
    err "Служба не запустилась после обновления."
    exit 1
  fi

  ROLLED_BACK=1  # успех — откат не нужен
  # Печатаем версию: без неё после обновления невозможно убедиться, что
  # запустилась именно новая сборка (а не осталась прежняя).
  NEW_VERSION="$("$VENV_DIR/bin/python" -c 'from mailarchiver.version import __version__; print(__version__)' 2>/dev/null || echo '?')"
  ok "Обновление завершено успешно. Установленная версия: $NEW_VERSION"
  # чистим старые бэкапы (оставляем 5 последних)
  ls -1dt "$BACKUP_DIR"/app_* 2>/dev/null | tail -n +6 | xargs -r rm -rf || true
  info "Резервная копия прежней версии: $BACKUP_PATH"
}
main "$@"
