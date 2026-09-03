#!/usr/bin/env bash
# Точка входа контейнера MailArchiver.
set -Eeuo pipefail

: "${MA_HOST:=0.0.0.0}"
: "${MA_PORT:=8493}"
: "${MAILARCHIVER_DATA:=/data}"

# Первый аргумент — команда mailarchiver (serve по умолчанию)
CMD="${1:-serve}"; shift || true

if [ "$CMD" = "serve" ]; then
  echo "[entrypoint] Запуск MailArchiver на ${MA_HOST}:${MA_PORT}, данные: ${MAILARCHIVER_DATA}"
  # Автосоздание администратора из переменных окружения (если заданы и БД пуста)
  if [ -n "${MA_ADMIN_USER:-}" ] && [ -n "${MA_ADMIN_PASSWORD:-}" ]; then
    mailarchiver create-admin -u "$MA_ADMIN_USER" -p "$MA_ADMIN_PASSWORD" 2>/dev/null \
      && echo "[entrypoint] Администратор '$MA_ADMIN_USER' создан." \
      || echo "[entrypoint] Администратор уже существует или не создан — пропускаю."
  fi
  exec mailarchiver serve --host "$MA_HOST" --port "$MA_PORT"
else
  exec mailarchiver "$CMD" "$@"
fi
