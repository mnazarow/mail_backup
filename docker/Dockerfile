# ============================================================================
#  MailArchiver — образ Docker
#  Многоступенчатая сборка: зависимости ставятся в venv, затем копируются в
#  минимальный финальный образ. Включён readpst (pst-utils) для импорта .pst.
# ============================================================================
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /venv \
 && /venv/bin/pip install --upgrade pip setuptools wheel \
 && /venv/bin/pip install -r requirements.txt

COPY . /app
# Пакет ставим БЕЗ зависимостей: они уже установлены строго по requirements.txt
# выше. Иначе pip заново разрешил бы их по границам pyproject.toml и в образ
# попали бы непроверенные версии библиотек.
RUN /venv/bin/pip install --no-deps . \
 && /venv/bin/python -c "import mailarchiver, fastapi, uvicorn, jinja2, yaml, apscheduler, imapclient, cryptography"

# ---------------------------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="MailArchiver" \
      org.opencontainers.image.description="Резервное копирование почтовых ящиков по IMAP с веб-интерфейсом" \
      org.opencontainers.image.version="1.1.0"

# readpst нужен только для импорта .pst; ca-certificates — для TLS/OAuth2
RUN apt-get update \
 && apt-get install -y --no-install-recommends pst-utils ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# непривилегированный пользователь
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin mailarchiver \
 && mkdir -p /data && chown mailarchiver:mailarchiver /data

COPY --from=build /venv /venv
COPY --from=build /app /app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV MAILARCHIVER_DATA=/data \
    MA_HOST=0.0.0.0 \
    MA_PORT=8493 \
    PATH="/venv/bin:$PATH"

USER mailarchiver
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8493

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${MA_PORT:-8493}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
