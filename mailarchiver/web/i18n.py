"""
Справка по параметрам (на русском) — подробное описание, рекомендации и пример
для КАЖДОГО параметра. Используется:
  * в веб-интерфейсе (всплывающие подсказки «?» и раскрывающиеся описания);
  * при генерации документации (docs/ru/05-parameters-reference.md).

Структура записи PARAM_HELP["<секция>.<ключ>"]:
    {
      "title":     краткое название параметра,
      "help":      что это и зачем,
      "recommend": рекомендация по настройке,
      "example":   пример значения,
      "default":   значение по умолчанию,
    }
"""
from __future__ import annotations

from typing import Dict, List

PARAM_HELP: Dict[str, dict] = {
    # ---- server ----
    "server.host": {
        "title": "Адрес прослушивания",
        "help": "IP-адрес, на котором веб-интерфейс принимает подключения. 127.0.0.1 — только с самого сервера (безопасно, обычно за reverse proxy). 0.0.0.0 — со всех сетевых интерфейсов.",
        "recommend": "Оставьте 127.0.0.1 и поставьте перед сервисом nginx/traefik с HTTPS. Ставьте 0.0.0.0 только если понимаете риски и включили аутентификацию.",
        "example": "127.0.0.1", "default": "127.0.0.1",
    },
    "server.port": {
        "title": "Порт веб-интерфейса",
        "help": "TCP-порт, на котором работает веб-интерфейс.",
        "recommend": "Порт по умолчанию 8493 подходит в большинстве случаев. Меняйте, если он занят.",
        "example": "8493", "default": "8493",
    },
    "server.public_url": {
        "title": "Внешний URL",
        "help": "Полный адрес, по которому сервис доступен снаружи. Используется для формирования ссылок.",
        "recommend": "Заполните, если работаете за reverse proxy с доменом и HTTPS.",
        "example": "https://mail-backup.example.ru", "default": "(пусто)",
    },
    "server.behind_proxy": {
        "title": "За обратным прокси",
        "help": "Доверять заголовкам X-Forwarded-* (реальный IP клиента, протокол). Включайте только если перед сервисом стоит доверенный nginx/traefik.",
        "recommend": "true при работе за reverse proxy, иначе false.",
        "example": "false", "default": "false",
    },
    # ---- security ----
    "security.auth_enabled": {
        "title": "Требовать вход",
        "help": "Включает обязательную аутентификацию в веб-интерфейсе. При первом запуске создаётся администратор.",
        "recommend": "Всегда держите включённой, кроме изолированного стенда без доступа извне.",
        "example": "true", "default": "true",
    },
    "security.session_ttl_hours": {
        "title": "Срок жизни сессии, часов",
        "help": "Через сколько часов после входа сессия истечёт и потребуется войти заново.",
        "recommend": "8–12 часов для рабочего дня. Меньше — безопаснее, но чаще вход.",
        "example": "12", "default": "12",
    },
    "security.session_idle_minutes": {
        "title": "Тайм-аут бездействия, минут",
        "help": "Автоматический выход при отсутствии активности указанное число минут.",
        "recommend": "30–60 минут. Для строгих требований — 15.",
        "example": "60", "default": "60",
    },
    "security.min_password_length": {
        "title": "Минимальная длина пароля",
        "help": "Минимальное число символов в пароле пользователя веб-интерфейса.",
        "recommend": "Не меньше 8; лучше 12+ и парольная фраза.",
        "example": "8", "default": "8",
    },
    "security.max_login_attempts": {
        "title": "Порог блокировки входа",
        "help": "После скольких неудачных попыток подряд аккаунт временно блокируется (защита от подбора).",
        "recommend": "5 — разумный баланс. Слишком мало мешает при опечатках.",
        "example": "5", "default": "5",
    },
    "security.lockout_minutes": {
        "title": "Длительность блокировки, минут",
        "help": "На сколько минут блокируется вход после превышения порога попыток.",
        "recommend": "15 минут.",
        "example": "15", "default": "15",
    },
    "security.secure_cookie": {
        "title": "Cookie только по HTTPS",
        "help": "Помечает cookie сессии флагом Secure — браузер отправит её только по HTTPS.",
        "recommend": "Включите, когда сервис доступен по HTTPS. По HTTP держите выключенным, иначе вход не сработает.",
        "example": "false", "default": "false",
    },
    "security.imap_ssl_verify": {
        "title": "Проверять TLS-сертификат IMAP",
        "help": "Проверять подлинность сертификата почтового сервера при подключении по SSL/STARTTLS.",
        "recommend": "Держите включённым. Отключайте только для внутренних серверов с самоподписанным сертификатом — это снижает защиту от подмены.",
        "example": "true", "default": "true",
    },
    # ---- storage ----
    "storage.compress": {
        "title": "Сжимать письма (gzip)",
        "help": "Сжимать каждое письмо при сохранении. Экономит место (текстовые письма сжимаются в 2–4 раза), но чуть увеличивает нагрузку на процессор.",
        "recommend": "Включайте при нехватке места. Для максимальной скорости и совместимости с внешними инструментами — выключено.",
        "example": "false", "default": "false",
    },
    "storage.fsync": {
        "title": "Принудительный сброс на диск",
        "help": "После записи письма принудительно сбрасывать данные на диск (fsync). Надёжнее при сбоях питания, но медленнее.",
        "recommend": "Включено для надёжности. Выключайте только на быстрых SSD, где важна максимальная скорость.",
        "example": "true", "default": "true",
    },
    "storage.min_free_space_mb": {
        "title": "Минимум свободного места, МБ",
        "help": "Если свободного места меньше этого значения, бэкап не начнётся (защита от заполнения диска).",
        "recommend": "500–2000 МБ. Для больших ящиков увеличьте.",
        "example": "500", "default": "500",
    },
    "storage.verify_after_write": {
        "title": "Проверять после записи",
        "help": "После сохранения письма сверять его размер и контрольную сумму (SHA-256). Гарантирует, что копия записалась без повреждений.",
        "recommend": "Держите включённым — стоит недорого, а защищает от «тихой» порчи данных.",
        "example": "true", "default": "true",
    },
    # ---- backup ----
    "backup.max_concurrent_jobs": {
        "title": "Одновременных заданий",
        "help": "Сколько заданий (бэкап/экспорт/восстановление) выполняется параллельно.",
        "recommend": "2–4 для сервера с несколькими ядрами. Больше — быстрее при многих ящиках, но выше нагрузка на сеть и диск.",
        "example": "2", "default": "2",
    },
    "backup.per_account_concurrency": {
        "title": "Параллелизм на ящик",
        "help": "Сколько одновременных подключений к ОДНОМУ ящику разрешено.",
        "recommend": "1. Многие почтовые серверы ограничивают число сессий с одного аккаунта и банят за превышение.",
        "example": "1", "default": "1",
    },
    "backup.fetch_batch_size": {
        "title": "Размер пакета загрузки",
        "help": "Сколько писем запрашивать у сервера за одну команду. Больше — быстрее, но выше пиковое потребление памяти.",
        "recommend": "100–300. Для медленных серверов уменьшите, для быстрых можно увеличить.",
        "example": "200", "default": "200",
    },
    "backup.connect_timeout_s": {
        "title": "Тайм-аут подключения, сек",
        "help": "Сколько секунд ждать установления соединения с почтовым сервером.",
        "recommend": "30. Увеличьте для медленных/удалённых серверов.",
        "example": "30", "default": "30",
    },
    "backup.socket_timeout_s": {
        "title": "Тайм-аут операций, сек",
        "help": "Максимальное время ожидания ответа на команду IMAP.",
        "recommend": "120. Для очень больших писем/вложений увеличьте до 300.",
        "example": "120", "default": "120",
    },
    "backup.retry_attempts": {
        "title": "Число повторов",
        "help": "Сколько раз повторять задание при временных сбоях (обрыв связи, тайм-аут).",
        "recommend": "3–5. Помогает пережить кратковременные проблемы сети.",
        "example": "4", "default": "4",
    },
    "backup.retry_initial_delay_s": {
        "title": "Начальная задержка повтора, сек",
        "help": "Пауза перед первым повтором. Далее растёт по экспоненте.",
        "recommend": "2–5 секунд.",
        "example": "2", "default": "2",
    },
    "backup.retry_backoff": {
        "title": "Множитель задержки",
        "help": "Во сколько раз увеличивается пауза с каждой попыткой (2 = удвоение).",
        "recommend": "2.0.",
        "example": "2.0", "default": "2.0",
    },
    "backup.download_flags": {
        "title": "Сохранять флаги писем",
        "help": "Сохранять пометки писем (прочитано, важное, отвечено и т.п.). Нужно, чтобы при восстановлении статусы вернулись как были.",
        "recommend": "Включено.",
        "example": "true", "default": "true",
    },
    "backup.skip_larger_than_mb": {
        "title": "Пропускать письма крупнее, МБ",
        "help": "Не сохранять письма больше указанного размера (0 — сохранять все). Полезно, если не нужны гигантские вложения.",
        "recommend": "0 (сохранять всё). Ставьте, например, 50, если хотите исключить огромные вложения.",
        "example": "0", "default": "0",
    },
    "backup.folder_include": {
        "title": "Только эти папки",
        "help": "Белый список: копировать ТОЛЬКО перечисленные папки (и вложенные). Пусто — копировать все папки.",
        "recommend": "Оставьте пустым, чтобы копировать весь ящик. Заполняйте, если нужны только отдельные папки.",
        "example": '["INBOX", "Отправленные"]', "default": "[] (все)",
    },
    "backup.folder_exclude": {
        "title": "Исключить папки",
        "help": "Чёрный список: НЕ копировать перечисленные папки (и вложенные). Применяется после белого списка.",
        "recommend": 'Часто исключают спам и корзину: ["Спам", "Корзина"] или для Gmail ["[Gmail]/Spam", "[Gmail]/Trash"].',
        "example": '["Спам", "Корзина"]', "default": "[] (не исключать)",
    },
    # ---- retention ----
    "retention.enabled": {
        "title": "Включить ретеншн",
        "help": "Автоматическая очистка старых локальных копий по расписанию/после бэкапа.",
        "recommend": "Выключено, если храните всё «вечно». Включайте при ограниченном месте.",
        "example": "false", "default": "false",
    },
    "retention.keep_days": {
        "title": "Хранить дней",
        "help": "Удалять локальные письма старше указанного числа дней (0 — хранить бессрочно). Влияет только на ЛОКАЛЬНУЮ копию, письма на сервере не трогаются.",
        "recommend": "0 для полного архива. Например, 365 — хранить копии за последний год.",
        "example": "0", "default": "0",
    },
    "retention.keep_last_runs": {
        "title": "Хранить записей истории",
        "help": "Сколько последних записей истории бэкапов держать в журнале на каждый ящик.",
        "recommend": "30–100.",
        "example": "30", "default": "30",
    },
    "retention.delete_removed_from_server": {
        "title": "Удалять исчезнувшие с сервера",
        "help": "Удалять из локальной копии письма, которых больше нет на сервере. ВНИМАНИЕ: тогда локальная копия перестаёт быть архивом (удалённое на сервере пропадёт и локально).",
        "recommend": "Держите ВЫКЛЮЧЕННЫМ, если цель — надёжный архив, из которого ничего не пропадает.",
        "example": "false", "default": "false",
    },
    # ---- export ----
    "export.default_engine": {
        "title": "Движок экспорта по умолчанию",
        "help": "Какой движок использовать по умолчанию: auto (сам выберет), aspose (надёжный .pst, нужна лицензия), native (встроенный .pst, экспериментальный), mbox, eml.",
        "recommend": "auto. Для .pst он выберет Aspose при наличии лицензии, иначе встроенный. Самый надёжный бесплатный вариант — eml/mbox.",
        "example": "auto", "default": "auto",
    },
    "export.pst_format": {
        "title": "Формат PST",
        "help": "unicode — для Outlook 2003 и новее (рекомендуется, большой объём). ansi — для Outlook 97–2002 (лимит 2 ГБ). Встроенный (native) движок создаёт ANSI.",
        "recommend": "unicode для современных версий Outlook. ansi только для очень старого Outlook (до 2003).",
        "example": "unicode", "default": "unicode",
    },
    "export.pst_split_size_mb": {
        "title": "Разбивать PST по, МБ",
        "help": "Разбивать большой .pst на части заданного размера (0 — не разбивать).",
        "recommend": "0. Для ANSI-формата держите итоговый файл под 2 ГБ.",
        "example": "0", "default": "0",
    },
    "export.aspose_license_path": {
        "title": "Путь к лицензии Aspose",
        "help": "Путь к файлу лицензии Aspose.Email (.lic). Без лицензии Aspose работает в демо-режиме: не более 50 писем на папку и водяные знаки.",
        "recommend": "Укажите для промышленного использования Aspose. Иначе используйте native/eml/mbox.",
        "example": "/etc/mailarchiver/Aspose.Email.lic", "default": "(пусто)",
    },
    "export.outlook_target": {
        "title": "Целевая версия Outlook",
        "help": "Для какой версии Outlook готовить .pst. Влияет на выбор формата (старые версии — ANSI, новые — Unicode).",
        "recommend": "2016+ (Unicode) для актуальных версий. 2002 (ANSI) — для очень старых.",
        "example": "2016+", "default": "2016+",
    },
    "export.include_attachments": {
        "title": "Включать вложения",
        "help": "Экспортировать письма вместе с вложениями. В форматах eml/mbox вложения сохраняются всегда (они часть письма).",
        "recommend": "Включено.",
        "example": "true", "default": "true",
    },
    # ---- scheduler ----
    "scheduler.enabled": {
        "title": "Планировщик включён",
        "help": "Разрешает выполнение расписаний (регулярный автоматический бэкап и т.п.).",
        "recommend": "Включено, если хотите бэкап по расписанию.",
        "example": "true", "default": "true",
    },
    "scheduler.timezone": {
        "title": "Часовой пояс расписаний",
        "help": "В каком часовом поясе трактовать время в расписаниях (cron).",
        "recommend": "Ваш локальный пояс, например Europe/Moscow.",
        "example": "Europe/Moscow", "default": "Europe/Moscow",
    },
    "scheduler.misfire_grace_time_s": {
        "title": "Допуск пропуска, сек",
        "help": "Если сервис был выключен в момент запуска по расписанию, задание всё равно запустится, если с момента планового времени прошло не больше этого значения.",
        "recommend": "3600 (1 час).",
        "example": "3600", "default": "3600",
    },
    "scheduler.coalesce": {
        "title": "Объединять пропуски",
        "help": "Если из-за простоя накопилось несколько пропущенных запусков одного расписания, выполнить их как ОДИН, а не несколько подряд.",
        "recommend": "Включено.",
        "example": "true", "default": "true",
    },
    # ---- logging ----
    "logging.level": {
        "title": "Уровень логирования",
        "help": "DEBUG — максимум подробностей (для диагностики), INFO — обычный, WARNING — только предупреждения и ошибки, ERROR — только ошибки.",
        "recommend": "INFO в обычной работе. DEBUG временно при поиске проблем (логи растут быстро).",
        "example": "INFO", "default": "INFO",
    },
    "logging.to_stdout": {
        "title": "Вывод в stdout",
        "help": "Дублировать логи в стандартный вывод (нужно для systemd/journald и Docker).",
        "recommend": "Включено.",
        "example": "true", "default": "true",
    },
    # ---- notifications ----
    "notifications.enabled": {
        "title": "Уведомления по e-mail",
        "help": "Отправлять письма о результатах заданий (по SMTP).",
        "recommend": "Включите, чтобы узнавать об ошибках бэкапа вовремя.",
        "example": "false", "default": "false",
    },
    "notifications.smtp_host": {
        "title": "SMTP-сервер",
        "help": "Адрес почтового сервера для отправки уведомлений.",
        "recommend": "Например, smtp.yandex.ru или smtp.gmail.com.",
        "example": "smtp.yandex.ru", "default": "(пусто)",
    },
    "notifications.smtp_port": {
        "title": "SMTP-порт",
        "help": "Порт SMTP: 465 (SSL), 587 (STARTTLS), 25 (без шифрования).",
        "recommend": "587 со STARTTLS или 465 с SSL.",
        "example": "587", "default": "587",
    },
    "notifications.smtp_security": {
        "title": "Шифрование SMTP",
        "help": "starttls (обычно порт 587), ssl (обычно порт 465) или none (без шифрования, не рекомендуется).",
        "recommend": "starttls или ssl.",
        "example": "starttls", "default": "starttls",
    },
    "notifications.smtp_user": {
        "title": "Логин SMTP",
        "help": "Имя пользователя для входа на SMTP-сервер (обычно полный адрес почты).",
        "recommend": "Укажите, если сервер требует аутентификацию.",
        "example": "robot@example.ru", "default": "(пусто)",
    },
    "notifications.smtp_password": {
        "title": "Пароль SMTP",
        "help": "Пароль (или пароль приложения) для SMTP. Хранится в БД в зашифрованном виде на уровне настроек сервиса.",
        "recommend": "Используйте отдельный пароль приложения, а не основной пароль почты.",
        "example": "(секрет)", "default": "(пусто)",
    },
    "notifications.mail_from": {
        "title": "Отправитель",
        "help": "Адрес в поле «От» для писем-уведомлений.",
        "recommend": "Обычно совпадает с логином SMTP.",
        "example": "robot@example.ru", "default": "(пусто)",
    },
    "notifications.mail_to": {
        "title": "Получатели",
        "help": "Кому слать уведомления (можно несколько через запятую).",
        "recommend": "Адрес администратора.",
        "example": "admin@example.ru", "default": "[]",
    },
    "notifications.on_success": {
        "title": "Уведомлять об успехе",
        "help": "Слать письмо при успешном завершении задания.",
        "recommend": "Обычно выключено, чтобы не спамить. Включайте для важных ящиков.",
        "example": "false", "default": "false",
    },
    "notifications.on_failure": {
        "title": "Уведомлять об ошибках",
        "help": "Слать письмо при ошибке или частичном сбое задания.",
        "recommend": "Включено — это главная причина настраивать уведомления.",
        "example": "true", "default": "true",
    },
}

# Порядок и группировка секций для страницы настроек
SETTINGS_SECTIONS: List[dict] = [
    {"section": "server", "title": "Сервер и сеть", "icon": "🌐",
     "keys": ["host", "port", "public_url", "behind_proxy"]},
    {"section": "security", "title": "Безопасность", "icon": "🔒",
     "keys": ["auth_enabled", "session_ttl_hours", "session_idle_minutes", "min_password_length",
              "max_login_attempts", "lockout_minutes", "secure_cookie", "imap_ssl_verify"]},
    {"section": "storage", "title": "Хранилище", "icon": "💾",
     "keys": ["compress", "fsync", "min_free_space_mb", "verify_after_write"]},
    {"section": "backup", "title": "Резервное копирование", "icon": "📥",
     "keys": ["max_concurrent_jobs", "per_account_concurrency", "fetch_batch_size", "connect_timeout_s",
              "socket_timeout_s", "retry_attempts", "retry_initial_delay_s", "retry_backoff",
              "download_flags", "skip_larger_than_mb", "folder_include", "folder_exclude"]},
    {"section": "retention", "title": "Хранение и очистка", "icon": "🧹",
     "keys": ["enabled", "keep_days", "keep_last_runs", "delete_removed_from_server"]},
    {"section": "export", "title": "Экспорт (PST и др.)", "icon": "📤",
     "keys": ["default_engine", "pst_format", "pst_split_size_mb", "aspose_license_path",
              "outlook_target", "include_attachments"]},
    {"section": "scheduler", "title": "Планировщик", "icon": "⏰",
     "keys": ["enabled", "timezone", "misfire_grace_time_s", "coalesce"]},
    {"section": "logging", "title": "Логирование", "icon": "📋",
     "keys": ["level", "to_stdout"]},
    {"section": "notifications", "title": "Уведомления", "icon": "✉️",
     "keys": ["enabled", "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password",
              "mail_from", "mail_to", "on_success", "on_failure"]},
]

# Справка по полям ящика (при добавлении/редактировании аккаунта)
ACCOUNT_HELP: Dict[str, dict] = {
    "name": {"title": "Название", "help": "Произвольное имя ящика для отображения в интерфейсе.",
             "recommend": "Понятное имя, напр. «Почта директора» или «info@company».", "example": "Основная почта"},
    "host": {"title": "IMAP-сервер", "help": "Адрес IMAP-сервера почтового провайдера.",
             "recommend": "Смотрите в справке провайдера. Примеры: imap.yandex.ru, imap.mail.ru, imap.gmail.com, outlook.office365.com.",
             "example": "imap.yandex.ru"},
    "port": {"title": "Порт", "help": "Порт IMAP. 993 — SSL/TLS (обычный выбор), 143 — STARTTLS/без шифрования.",
             "recommend": "993 для SSL/TLS.", "example": "993"},
    "username": {"title": "Логин", "help": "Имя пользователя для входа в почту (обычно полный адрес).",
                 "recommend": "Полный адрес, напр. user@yandex.ru.", "example": "user@yandex.ru"},
    "password": {"title": "Пароль",
                 "help": "Пароль для доступа по IMAP. Хранится в БД в зашифрованном виде (ключом сервиса).",
                 "recommend": "Для Яндекс/Mail.ru/Gmail включите IMAP и создайте «пароль приложения» — обычный пароль часто не подходит.",
                 "example": "(пароль приложения)"},
    "security": {"title": "Шифрование", "help": "SSL/TLS (порт 993) — рекомендуется. STARTTLS (порт 143). Без шифрования — только для локальных серверов.",
                 "recommend": "SSL/TLS.", "example": "SSL/TLS (993)"},
    "auth_type": {"title": "Способ входа", "help": "«Пароль» — обычный логин/пароль. «OAuth2» — для Gmail и Microsoft 365, где вход по паролю отключён.",
                  "recommend": "«Пароль» для большинства. «OAuth2» для Gmail/Microsoft 365.", "example": "Пароль"},
    "folder_exclude": {"title": "Исключить папки", "help": "Папки, которые не нужно копировать для этого ящика (в дополнение к глобальным настройкам).",
                       "recommend": "Например, спам и корзина.", "example": "Спам, Корзина"},
    "oauth_client_id": {"title": "OAuth2 Client ID", "help": "Идентификатор клиентского приложения OAuth2 (из консоли Google/Microsoft).",
                        "recommend": "Создайте OAuth-приложение у провайдера, разрешите доступ к почте (IMAP).", "example": "1234-abcd.apps.googleusercontent.com"},
    "oauth_client_secret": {"title": "OAuth2 Client Secret", "help": "Секрет клиентского приложения OAuth2.",
                            "recommend": "Из той же консоли провайдера.", "example": "(секрет)"},
    "oauth_refresh_token": {"title": "OAuth2 Refresh Token", "help": "Долгоживущий токен обновления, полученный при первичной авторизации приложения.",
                            "recommend": "Получите его один раз через процедуру OAuth2 вашего провайдера.", "example": "(токен)"},
    "oauth_token_url": {"title": "OAuth2 Token URL", "help": "Адрес сервиса выдачи токенов провайдера.",
                        "recommend": "Google: https://oauth2.googleapis.com/token; Microsoft: https://login.microsoftonline.com/common/oauth2/v2.0/token.",
                        "example": "https://oauth2.googleapis.com/token"},
}

EXPORT_HELP: Dict[str, dict] = {
    "engine": {"title": "Движок", "help": "Чем создавать файл: eml (надёжно), mbox (надёжно), Aspose PST (надёжно, лицензия), native PST (встроенный, эксперимент.).",
               "recommend": "Для гарантированного .pst — Aspose. Бесплатно и надёжно — eml/mbox. Встроенный PST — проверяйте в своём Outlook.", "example": "auto"},
    "folders": {"title": "Папки", "help": "Какие папки экспортировать (пусто — все).",
                "recommend": "Оставьте пустым для всего ящика.", "example": "INBOX, Отправленные"},
    "date_from": {"title": "Дата с", "help": "Экспортировать письма не старше этой даты (по дате получения).",
                  "recommend": "Оставьте пустым, чтобы экспортировать всё.", "example": "2023-01-01"},
    "date_to": {"title": "Дата по", "help": "Экспортировать письма не новее этой даты.",
                "recommend": "Оставьте пустым для всех дат.", "example": "2024-12-31"},
    "pst_format": {"title": "Формат PST", "help": "unicode — Outlook 2003+; ansi — Outlook 97–2002 (до 2 ГБ).",
                   "recommend": "unicode для современных версий.", "example": "unicode"},
}

RESTORE_HELP: Dict[str, dict] = {
    "target_mode": {"title": "Куда восстанавливать",
                    "help": "«В исходные папки» — вернуть письма в те же папки. «С префиксом» — в папки вида Префикс/Исходная (безопасно, не смешивается с текущей почтой). «В одну папку» — всё в одну указанную папку.",
                    "recommend": "«С префиксом» — самый безопасный вариант, не затрагивает текущую почту.", "example": "С префиксом"},
    "target_prefix": {"title": "Префикс папки", "help": "Имя корневой папки, куда лягут восстановленные письма (для режима «С префиксом»).",
                      "recommend": "Например, «Восстановлено».", "example": "Восстановлено"},
    "check_duplicates": {"title": "Пропускать дубли", "help": "Не заливать письмо, если оно уже есть в целевой папке (проверка по Message-ID).",
                         "recommend": "Включено — позволяет безопасно перезапускать восстановление.", "example": "true"},
    "dry_run": {"title": "Пробный прогон", "help": "Только посчитать, что будет восстановлено, без реальной заливки на сервер.",
                "recommend": "Сделайте пробный прогон перед реальным восстановлением.", "example": "false"},
}

SCHEDULE_HELP: Dict[str, dict] = {
    "kind": {"title": "Тип расписания", "help": "cron — по времени (например, каждый день в 3:00). interval — каждые N минут/часов.",
             "recommend": "cron для ежедневного бэкапа ночью.", "example": "cron"},
    "cron_expr": {"title": "Cron-выражение", "help": "Пять полей: минуты часы день месяц день_недели. «0 3 * * *» — ежедневно в 03:00. «0 */6 * * *» — каждые 6 часов. «30 2 * * 1» — по понедельникам в 02:30.",
                  "recommend": "«0 3 * * *» — ежедневный бэкап ночью, когда нагрузка минимальна.", "example": "0 3 * * *"},
    "interval_seconds": {"title": "Интервал, сек", "help": "Как часто запускать (в секундах). 3600 — каждый час, 21600 — каждые 6 часов.",
                         "recommend": "Не чаще раза в час для бэкапа, чтобы не нагружать почтовый сервер.", "example": "21600"},
    "job_type": {"title": "Что выполнять", "help": "backup — резервное копирование, retention — очистка старого, verify — проверка целостности.",
                 "recommend": "backup для регулярных копий.", "example": "backup"},
}


def all_help() -> dict:
    """Собрать всю справку в один объект для отдачи в интерфейс."""
    return {
        "params": PARAM_HELP,
        "sections": SETTINGS_SECTIONS,
        "account": ACCOUNT_HELP,
        "export": EXPORT_HELP,
        "restore": RESTORE_HELP,
        "schedule": SCHEDULE_HELP,
    }
