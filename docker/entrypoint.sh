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
  # Автосоздание администратора (только если явно задан пароль).
  # Пароль НЕ передаётся аргументом: аргументы видны в `ps` любому процессу
  # контейнера. Предпочтительно MA_ADMIN_PASSWORD_FILE (docker secret).
  # Ошибки create-admin не прячем: раньше «файл пароля не читается» и «пароль
  # слишком короткий» выглядели одинаково — «уже существует или не создан».
  if [ -n "${MA_ADMIN_USER:-}" ]; then
    if [ -n "${MA_ADMIN_PASSWORD_FILE:-}" ]; then
      if [ -r "${MA_ADMIN_PASSWORD_FILE}" ]; then
        mailarchiver create-admin -u "$MA_ADMIN_USER" --password-file "$MA_ADMIN_PASSWORD_FILE" \
          && echo "[entrypoint] Администратор '$MA_ADMIN_USER' создан." \
          || echo "[entrypoint] Администратор не создан (причина — строкой выше); продолжаю запуск."
      else
        echo "[entrypoint] Файл пароля ${MA_ADMIN_PASSWORD_FILE} не читается пользователем контейнера (uid $(id -u))."
        echo "[entrypoint] На хосте: sudo chown $(id -u) <файл> && sudo chmod 400 <файл>. Администратор не создан."
      fi
    elif [ -n "${MA_ADMIN_PASSWORD:-}" ]; then
      printf '%s\n' "$MA_ADMIN_PASSWORD" \
        | mailarchiver create-admin -u "$MA_ADMIN_USER" --password-stdin \
        && echo "[entrypoint] Администратор '$MA_ADMIN_USER' создан." \
        || echo "[entrypoint] Администратор не создан (причина — строкой выше); продолжаю запуск."
    else
      echo "[entrypoint] MA_ADMIN_PASSWORD(_FILE) не задан — администратор не создаётся."
      echo "[entrypoint] Заведите его при первом входе в интерфейс или командой create-admin."
    fi
  fi
  exec mailarchiver serve --host "$MA_HOST" --port "$MA_PORT"
else
  exec mailarchiver "$CMD" "$@"
fi
