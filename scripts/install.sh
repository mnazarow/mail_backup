#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — скрипт установки для Linux
#  Автоопределение дистрибутива (apt/dnf/yum/zypper/pacman), установка
#  зависимостей, создание пользователя, службы systemd и первичная настройка.
#  Максимальная обработка ошибок: остановка при любой ошибке, откат частичной
#  установки, подробные сообщения.
#
#  Запуск (из каталога с исходниками):   sudo bash scripts/install.sh
# ============================================================================
set -Eeuo pipefail

# ---------- Параметры (можно переопределить переменными окружения) ----------
APP_NAME="mailarchiver"
APP_USER="${MA_USER:-mailarchiver}"
APP_GROUP="${MA_GROUP:-mailarchiver}"
INSTALL_DIR="${MA_INSTALL_DIR:-/opt/mailarchiver}"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
CONFIG_DIR="${MA_CONFIG_DIR:-/etc/mailarchiver}"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
DATA_DIR="${MA_DATA_DIR:-/var/lib/mailarchiver}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
BIND_HOST="${MA_HOST:-127.0.0.1}"
BIND_PORT="${MA_PORT:-8493}"
LOG_PREFIX="[MailArchiver install]"

# ---------- Цвета ----------
if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*"; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*"; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*"; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2; }

# ---------- Обработка ошибок и откат ----------
ROLLBACK_ENABLED=0
FRESH_INSTALL=0
cleanup_on_error(){
  local ec=$?
  err "Установка прервана (код $ec) на строке ${BASH_LINENO[0]}."
  if [ "$ROLLBACK_ENABLED" = "1" ] && [ "$FRESH_INSTALL" = "1" ]; then
    warn "Выполняю откат частичной установки…"
    systemctl stop "$APP_NAME" 2>/dev/null || true
    systemctl disable "$APP_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE" 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$INSTALL_DIR" 2>/dev/null || true
    warn "Откат завершён. Каталоги данных ($DATA_DIR) и конфигурации ($CONFIG_DIR) НЕ тронуты."
  fi
  err "Подробности выше. Исправьте причину и запустите скрипт заново."
  exit "$ec"
}
trap cleanup_on_error ERR

# ---------- Проверки окружения ----------
require_root(){
  if [ "$(id -u)" -ne 0 ]; then
    err "Скрипт нужно запускать с правами root: sudo bash scripts/install.sh"
    exit 1
  fi
}

SRC_DIR=""
detect_source(){
  # каталог, где лежит этот скрипт → корень проекта на уровень выше
  local script_dir; script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SRC_DIR="$(cd "$script_dir/.." && pwd)"
  if [ ! -d "$SRC_DIR/mailarchiver" ] || [ ! -f "$SRC_DIR/requirements.txt" ]; then
    err "Не найден исходный код (каталог mailarchiver/ и requirements.txt)."
    err "Запускайте скрипт из распакованного архива проекта."
    exit 1
  fi
  info "Источник: $SRC_DIR"
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
  [ "$PKG" != "none" ] && info "Менеджер пакетов: $PKG"
}

install_system_deps(){
  info "Установка системных зависимостей…"
  case "$PKG" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq || warn "apt-get update завершился с предупреждениями"
      apt-get install -y python3 python3-venv python3-pip ca-certificates || { err "Не удалось установить пакеты через apt"; exit 1; }
      apt-get install -y pst-utils >/dev/null 2>&1 || warn "pst-utils не установлен (нужен только для импорта .pst)"
      ;;
    dnf|yum)
      $PKG install -y python3 python3-pip ca-certificates || { err "Не удалось установить пакеты через $PKG"; exit 1; }
      $PKG install -y libpst >/dev/null 2>&1 || warn "libpst не установлен (нужен только для импорта .pst)"
      ;;
    zypper)
      zypper --non-interactive install python3 python3-pip python3-virtualenv ca-certificates || { err "Не удалось установить пакеты через zypper"; exit 1; }
      zypper --non-interactive install libpst >/dev/null 2>&1 || warn "libpst не установлен (импорт .pst)"
      ;;
    pacman)
      pacman -Sy --noconfirm python python-pip ca-certificates || { err "Не удалось установить пакеты через pacman"; exit 1; }
      pacman -S --noconfirm libpst >/dev/null 2>&1 || warn "libpst не установлен (импорт .pst)"
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
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null \
      || useradd --system --home-dir "$DATA_DIR" --shell /bin/false "$APP_USER" \
      || { err "Не удалось создать пользователя $APP_USER"; exit 1; }
    ok "Пользователь $APP_USER создан."
  fi
}

copy_source(){
  info "Копирование файлов в $APP_DIR…"
  mkdir -p "$APP_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude 'venv' --exclude '__pycache__' --exclude '.git' --exclude 'data' \
      "$SRC_DIR"/ "$APP_DIR"/
  else
    cp -a "$SRC_DIR"/. "$APP_DIR"/ 2>/dev/null || true
    rm -rf "$APP_DIR/venv" "$APP_DIR/.git" "$APP_DIR/data" 2>/dev/null || true
  fi
  ok "Файлы скопированы."
}

setup_venv(){
  info "Создание виртуального окружения и установка зависимостей…"
  python3 -m venv "$VENV_DIR" || { err "Не удалось создать venv (установлен ли python3-venv?)"; exit 1; }
  "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel -q || warn "Обновление pip завершилось с предупреждениями"

  # ВАЖНО: зависимости ставим ИЗ requirements.txt, а сам пакет — с --no-deps.
  # Раньше здесь было `pip install "$APP_DIR"`, из-за чего версии библиотек
  # подтягивались по нестрогим границам pyproject.toml и «уплывали» при каждой
  # установке — на новых версиях Starlette это роняло веб-интерфейс.
  if [ -f "$APP_DIR/requirements.txt" ]; then
    if ! "$VENV_DIR/bin/pip" install -q -r "$APP_DIR/requirements.txt"; then
      err "Не удалось установить зависимости Python из requirements.txt."
      err "Проверьте доступ в интернет / прокси и версию Python (нужен 3.10+)."
      exit 1
    fi
  else
    warn "requirements.txt не найден — ставлю зависимости по pyproject.toml."
  fi
  if ! "$VENV_DIR/bin/pip" install -q --no-deps "$APP_DIR"; then
    err "Не удалось установить пакет mailarchiver."
    exit 1
  fi

  # Контроль: пакет должен импортироваться и видеть все зависимости.
  if ! "$VENV_DIR/bin/python" -c "import mailarchiver, fastapi, uvicorn, jinja2, yaml, apscheduler, imapclient, cryptography" 2>/dev/null; then
    err "Зависимости установлены не полностью — приложение не импортируется."
    "$VENV_DIR/bin/python" -c "import mailarchiver" || true
    exit 1
  fi
  ok "Зависимости установлены."
}

setup_dirs(){
  mkdir -p "$CONFIG_DIR" "$DATA_DIR"
  chown -R "$APP_USER:$APP_GROUP" "$DATA_DIR"
  chmod 750 "$DATA_DIR"
  chown -R root:root "$APP_DIR"
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
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    sed -e "s|@VENV@|$VENV_DIR|g" -e "s|@APPDIR@|$APP_DIR|g" -e "s|@CONFIG@|$CONFIG_FILE|g" \
        -e "s|@USER@|$APP_USER|g" -e "s|@GROUP@|$APP_GROUP|g" -e "s|@DATADIR@|$DATA_DIR|g" \
        "$APP_DIR/systemd/mailarchiver.service" > "$SERVICE_FILE"
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
  systemctl daemon-reload
  systemctl enable "$APP_NAME" >/dev/null 2>&1 || warn "Не удалось включить автозапуск"
  ok "Служба установлена."
}

start_service(){
  info "Запуск службы…"
  systemctl restart "$APP_NAME"
  sleep 3
  if systemctl is-active --quiet "$APP_NAME"; then
    ok "Служба запущена."
  else
    err "Служба не запустилась. Логи: journalctl -u $APP_NAME -n 50 --no-pager"
    journalctl -u "$APP_NAME" -n 20 --no-pager 2>/dev/null || true
    exit 1
  fi
}

wait_health(){
  info "Проверка доступности веб-интерфейса…"
  for i in $(seq 1 15); do
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://$BIND_HOST:$BIND_PORT/health" >/dev/null 2>&1; then
      ok "Веб-интерфейс отвечает."
      return 0
    fi
    sleep 1
  done
  warn "Не удалось подтвердить доступность (возможно, curl отсутствует). Проверьте вручную."
}

create_admin_prompt(){
  echo ""
  info "Создание администратора веб-интерфейса."
  if [ -t 0 ]; then
    "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" create-admin || warn "Администратора можно создать позже (см. ниже)."
    # переназначим владельца БД, созданной от root
    chown -R "$APP_USER:$APP_GROUP" "$DATA_DIR" 2>/dev/null || true
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
  # если это чистая установка — включим откат
  [ -d "$INSTALL_DIR" ] || FRESH_INSTALL=1
  ROLLBACK_ENABLED=1
  detect_pkg_manager
  install_system_deps
  check_python
  create_user
  copy_source
  setup_venv
  setup_dirs
  write_config
  install_service
  start_service
  wait_health
  ROLLBACK_ENABLED=0   # дальше откат не нужен
  create_admin_prompt
  print_summary
}
main "$@"
