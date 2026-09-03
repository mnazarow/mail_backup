# 3. Установка

Два способа: скриптом (служба systemd) или через Docker. Выберите один.

## Способ А. Скрипт установки (systemd)

Подходит для Debian/Ubuntu, RHEL/Rocky/Alma/Fedora, openSUSE, Arch. Скрипт сам определит дистрибутив.

```bash
cd mail_backup
sudo bash scripts/install.sh
```

Что делает скрипт:
- ставит системные зависимости (`python3`, `python3-venv`, `python3-pip`, `ca-certificates`, `pst-utils`/`libpst`);
- создаёт системного пользователя `mailarchiver`;
- копирует код в `/opt/mailarchiver/app`, создаёт venv и ставит зависимости;
- пишет конфигурацию `/etc/mailarchiver/config.yaml` и каталог данных `/var/lib/mailarchiver`;
- устанавливает и запускает службу systemd, включает автозапуск;
- предлагает создать администратора.

При ошибке на любом шаге установка **откатывается** (для чистой установки), данные и конфигурация
не трогаются.

Создать администратора вручную (если не создан при установке):
```bash
sudo -u mailarchiver /opt/mailarchiver/venv/bin/mailarchiver \
  -c /etc/mailarchiver/config.yaml create-admin
```

Управление службой:
```bash
systemctl status mailarchiver
journalctl -u mailarchiver -f
systemctl restart mailarchiver
```

Веб-интерфейс: `http://127.0.0.1:8493` (по умолчанию слушает только локально — см. «Доступ снаружи»).

## Способ Б. Docker Compose

```bash
cd docker
# при желании задайте MA_ADMIN_USER / MA_ADMIN_PASSWORD в docker-compose.yml
docker compose up -d
```
Данные хранятся в томе `mailarchiver-data` и переживают перезапуск/обновление. `readpst` уже включён
в образ.

## Обновление

```bash
cd mail_backup   # каталог с НОВОЙ версией исходников
sudo bash scripts/update.sh
```
Скрипт делает резервную копию текущей установки, обновляет код и зависимости, проверяет конфигурацию
и перезапускает службу. При ошибке — **автоматический откат** к прежней версии. Данные и конфигурация
не трогаются.

Для Docker: пересоберите образ и поднимите заново — `docker compose up -d --build`.

## Удаление

```bash
sudo bash scripts/uninstall.sh          # удалить сервис, ДАННЫЕ СОХРАНИТЬ
sudo bash scripts/uninstall.sh --purge  # удалить всё, включая данные и конфигурацию
```

## Доступ снаружи (reverse proxy + HTTPS)

По умолчанию сервис слушает `127.0.0.1` — это безопасно. Чтобы открыть доступ из сети, поставьте
перед ним nginx/traefik с TLS (см. раздел «Безопасность» в полной документации). Не открывайте порт
`8493` напрямую в интернет по HTTP.

Быстрый доступ без публикации — SSH-туннель:
```bash
ssh -L 8493:127.0.0.1:8493 user@ваш-сервер
# затем откройте http://127.0.0.1:8493 на своём компьютере
```
