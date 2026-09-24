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
VENV_BACKUP_PATH="$BACKUP_DIR/venv_$STAMP"
ROLLED_BACK=0
WAS_ACTIVE=0          # работала ли служба до обновления (при сбое — вернуть её в строй)
UNIT_FILE="/etc/systemd/system/$APP_NAME.service"

# Откат возвращает и исходники, и venv. Пакет ставится НЕ в editable-режиме,
# то есть рабочий код лежит в $VENV_DIR/lib/*/site-packages/mailarchiver, а не
# в $APP_DIR — возврат одного только $APP_DIR ничего не откатывал: служба
# продолжала работать на новом коде и новых библиотеках, хотя скрипт
# рапортовал «Откат выполнен».
rollback(){
  local ec="${1:-1}"
  err "Обновление прервано (код $ec)."
  if [ "$ROLLED_BACK" = "0" ] && [ -f "$BACKUP_PATH/.complete" ]; then
    warn "Откат к предыдущей версии…"
    ROLLED_BACK=1
    rm -rf "$APP_DIR"
    mv "$BACKUP_PATH" "$APP_DIR"
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
    systemctl restart "$APP_NAME" 2>/dev/null || true
    local back_ver
    back_ver="$("$VENV_DIR/bin/python" -c 'from mailarchiver.version import __version__; print(__version__)' 2>/dev/null || echo '?')"
    warn "Откат выполнен. Служба перезапущена, версия: $back_ver"
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
on_exit(){
  local ec=$?
  if [ "$ec" -ne 0 ]; then
    rollback "$ec"
  fi
  exit "$ec"
}
trap on_exit EXIT

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

install_cli_wrapper(){
  # Команда «mailarchiver» в PATH (её вызывают документация и подсказки).
  # От root команды выполняются от имени службы и с её конфигурацией: иначе
  # запуск через sudo оставлял бы в каталоге данных файлы root:root, и служба
  # переставала бы в них писать. storage-key выполняется как есть — ключ
  # обычно создают в /etc/mailarchiver, куда служба писать не может.
  local wrapper="/usr/local/bin/mailarchiver"
  cat > "$wrapper" <<EOF
#!/bin/sh
# MailArchiver CLI (создан установщиком; перезаписывается при обновлении).
VENV_BIN="$VENV_DIR/bin/mailarchiver"
CONFIG="$CONFIG_FILE"
if [ "\$(id -u)" = "0" ] && [ "\${1:-}" != "storage-key" ] && command -v runuser >/dev/null 2>&1; then
  exec runuser -u "$APP_USER" -- "\$VENV_BIN" -c "\$CONFIG" "\$@"
fi
exec "\$VENV_BIN" -c "\$CONFIG" "\$@"
EOF
  chmod 755 "$wrapper" && ok "Команда mailarchiver доступна: $wrapper" || warn "Не удалось создать $wrapper"
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

  if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then WAS_ACTIVE=1; fi
  info "Останавливаю службу…"
  systemctl stop "$APP_NAME" 2>/dev/null || warn "Служба не была запущена"

  info "Резервная копия текущей установки → $BACKUP_PATH"
  mkdir -p "$BACKUP_DIR"
  cp -a "$APP_DIR" "$BACKUP_PATH"
  # venv копируем тоже: рабочий код и библиотеки живут именно там
  if [ -d "$VENV_DIR" ]; then
    info "Резервная копия окружения → $VENV_BACKUP_PATH"
    cp -a "$VENV_DIR" "$VENV_BACKUP_PATH"
  fi
  [ -f "$UNIT_FILE" ] && cp -a "$UNIT_FILE" "$BACKUP_DIR/unit_$STAMP.service"
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
  # check-config создаёт недостающие каталоги под data_dir. От root они
  # получались root:root 0700, и служба (от $APP_USER) не могла туда писать —
  # экспорт падал с «Permission denied» без внятной причины.
  # Проверяем от имени службы: так недостающие каталоги сразу получают
  # правильного владельца, а права уже существующих (например общего tmp)
  # не «ужимаются» до 0700 root. Владельца чиним только у чужих файлов.
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$VENV_DIR/bin/mailarchiver" -c "$CONFIG_FILE" check-config
  else
    su -s /bin/sh "$APP_USER" -c "'$VENV_DIR/bin/mailarchiver' -c '$CONFIG_FILE' check-config"
  fi
  if [ -d "$DATA_DIR" ]; then
    find "$DATA_DIR" \( ! -user "$APP_USER" -o ! -group "$APP_GROUP" \) -exec chown -h "$APP_USER:$APP_GROUP" {} + \
      2>/dev/null || chown -R "$APP_USER:$APP_GROUP" "$DATA_DIR"
  fi

  # обновить unit, если изменился шаблон. Свои правки (MemoryMax и т.п.)
  # держите в /etc/systemd/system/mailarchiver.service.d/override.conf —
  # сам unit перезаписывается при каждом обновлении.
  if [ -f "$APP_DIR/systemd/mailarchiver.service" ]; then
    sed -e "s|@VENV@|$VENV_DIR|g" -e "s|@APPDIR@|$APP_DIR|g" -e "s|@CONFIG@|$CONFIG_FILE|g" \
        -e "s|@USER@|$APP_USER|g" -e "s|@GROUP@|$APP_GROUP|g" -e "s|@DATADIR@|$DATA_DIR|g" \
        "$APP_DIR/systemd/mailarchiver.service" > "$UNIT_FILE"
    systemctl daemon-reload
  fi

  chown -R root:root "$APP_DIR"
  install_cli_wrapper
  ensure_optional_tools

  info "Запуск службы…"
  systemctl start "$APP_NAME"
  sleep 3
  if ! systemctl is-active --quiet "$APP_NAME"; then
    err "Служба не запустилась после обновления."
    exit 1
  fi
  # служба «active» ещё не значит, что веб-интерфейс отвечает
  local port
  port="$(awk '/^server:/{s=1;next} s&&/^[^ ]/{s=0} s&&/port:/{print $2; exit}' "$CONFIG_FILE" 2>/dev/null || true)"
  if command -v curl >/dev/null 2>&1 && [ -n "$port" ]; then
    local okh=0
    for _ in $(seq 1 20); do
      if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then okh=1; break; fi
      sleep 1
    done
    if [ "$okh" != "1" ]; then
      err "Служба запущена, но веб-интерфейс не отвечает на /health (порт $port)."
      exit 1
    fi
  fi

  ROLLED_BACK=1  # успех — откат не нужен
  rm -f "$BACKUP_PATH/.complete"
  # Печатаем версию: без неё после обновления невозможно убедиться, что
  # запустилась именно новая сборка (а не осталась прежняя).
  NEW_VERSION="$("$VENV_DIR/bin/python" -c 'from mailarchiver.version import __version__; print(__version__)' 2>/dev/null || echo '?')"
  ok "Обновление завершено успешно. Установленная версия: $NEW_VERSION"
  # чистим старые бэкапы (оставляем 5 последних каталогов кода и 3 окружения:
  # venv весит на порядок больше, а нужен только для самого свежего отката)
  ls -1dt "$BACKUP_DIR"/app_* 2>/dev/null | tail -n +6 | xargs -r rm -rf || true
  ls -1dt "$BACKUP_DIR"/venv_* 2>/dev/null | tail -n +4 | xargs -r rm -rf || true
  ls -1dt "$BACKUP_DIR"/unit_*.service 2>/dev/null | tail -n +6 | xargs -r rm -f || true
  info "Резервная копия прежней версии: $BACKUP_PATH"
}
main "$@"
