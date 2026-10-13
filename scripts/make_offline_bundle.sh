#!/usr/bin/env bash
# ============================================================================
#  MailArchiver — архив для установки и обновления БЕЗ доступа в интернет
#
#  Запускается на машине С интернетом. Собирает пакет приложения, получает
#  готовые пакеты (.whl) всех библиотек из requirements.txt и складывает их
#  вместе с исходниками в архив
#      mailarchiver-<версия>-offline-<python>-<платформа>.tar.gz
#  На сервере архив распаковывают и запускают install.sh / update.sh как
#  обычно: найдя в нём каталог wheels, скрипты ставят всё из него и в сеть не
#  ходят.
#
#  Пакеты годятся только для той версии Python (3.10, 3.12…) и той архитектуры
#  (x86_64, aarch64), под которые получены, а собранные под новую glibc не
#  встанут на систему со старой. Поэтому собирайте архив на машине с тем же
#  дистрибутивом и той же версией Python, что на сервере (подойдёт контейнер
#  или виртуальная машина), либо на более старом дистрибутиве с тем же Python.
#
#  Запуск:  bash scripts/make_offline_bundle.sh [параметры] [КУДА_ПОЛОЖИТЬ_АРХИВ]
#    --python ПУТЬ      интерпретатор, под который собирать (по умолчанию
#                       python3; то же задаёт переменная MA_PYTHON)
#    --only-binary      не собирать библиотеки из исходников: нет готового
#                       пакета под этот Python — отказ, а не сборка
#    --wheels-dir КАТ   складывать пакеты в этот каталог и оставить его (по
#                       умолчанию — временный каталог); должен быть пустым.
#                       Если сборка не удалась, каталог очищается (созданный
#                       скриптом — удаляется): следующий запуск не откажет
#                       из-за остатков
#    -h, --help         справка
#
#  Права root не нужны. В каталоге проекта скрипт ничего не меняет, кроме самого
#  архива, если он кладётся туда (без параметра ВЫХОД): пакет приложения
#  собирается из распакованного архива выпуска во временном каталоге (в нём ровно
#  те файлы, что попадают в обычный архив).
# ============================================================================
set -Eeuo pipefail

LOG_PREFIX="[MailArchiver bundle]"
if [ -t 1 ]; then RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; NC='\033[0m'; else RED=''; GRN=''; YLW=''; BLU=''; NC=''; fi
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*" || true; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*" || true; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*" || true; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2 || true; }

usage(){
  cat <<'EOF'
Использование: bash scripts/make_offline_bundle.sh [параметры] [КУДА_ПОЛОЖИТЬ_АРХИВ]
Собирает архив для установки MailArchiver без интернета. Запускать на машине
С интернетом, с той же версией Python и той же архитектурой, что у сервера.

  --python ПУТЬ      интерпретатор, под который собирать (по умолчанию python3;
                     то же задаёт переменная MA_PYTHON)
  --only-binary      не собирать библиотеки из исходников: нет готового пакета
                     под этот Python — отказ, а не сборка
  --wheels-dir КАТ   складывать пакеты в этот каталог и оставить его (по умолчанию —
                     временный каталог); должен быть пустым. При неудачной сборке
                     каталог очищается (созданный скриптом — удаляется)
  -h, --help         эта справка
EOF
}

PY="${MA_PYTHON:-python3}"
ONLY_BINARY=0
KEEP_WHEELS=""
OUT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --python|--wheels-dir)
      if [ $# -lt 2 ] || [ -z "$2" ]; then err "После $1 нужно значение."; usage >&2; exit 2; fi
      if [ "$1" = "--python" ]; then PY="$2"; else KEEP_WHEELS="$2"; fi
      shift ;;
    --python=*) PY="${1#*=}" ;;
    --wheels-dir=*) KEEP_WHEELS="${1#*=}" ;;
    --only-binary) ONLY_BINARY=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) err "Неизвестный параметр: $1"; usage >&2; exit 2 ;;
    *)
      if [ -n "$OUT_DIR" ]; then err "Лишний параметр: $1"; usage >&2; exit 2; fi
      OUT_DIR="$1" ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
for rel in scripts/make_release.py requirements.txt pyproject.toml VERSION; do
  if [ ! -f "$ROOT/$rel" ]; then
    err "Не найден $ROOT/$rel — скрипт запускают из каталога проекта (из git или из распакованного архива)."
    exit 1
  fi
done

# ---------- Чем собирать ----------
if ! command -v "$PY" >/dev/null 2>&1; then
  err "Не найден интерпретатор «$PY». Укажите его: --python /путь/к/python3"
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  err "Интерпретатор $PY ($("$PY" -V 2>&1 || true)) не подходит: MailArchiver нужен Python 3.10 или новее."
  exit 1
fi
# Именно «python -m pip», а не команда pip: пакеты должны быть получены под
# тот интерпретатор, который назван в имени архива.
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  err "У интерпретатора $PY нет pip — получать пакеты нечем."
  err "Установите его (Debian/Ubuntu: apt install python3-pip; иначе: $PY -m ensurepip --upgrade) и запустите скрипт заново."
  exit 1
fi
TARGET="$("$PY" -c 'import platform, sys, sysconfig
libc = " ".join(platform.libc_ver()).strip()
print("Python %d.%d.%d (cp%d%d), %s%s" % (sys.version_info[:3] + sys.version_info[:2]
      + (sysconfig.get_platform(), ", " + libc if libc else "")))')"
info "Архив собирается для: $TARGET"
info "Пакеты годятся только для этой версии Python и этой архитектуры (и системы не старее этой)."

# ---------- Рабочие каталоги ----------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ma-bundle.XXXXXX")"
DONE=0                # архив собран — каталог пакетов (--wheels-dir) остаётся
WHEELS=""
WHEELS_OURS=""        # created — каталог создан этим запуском; filled — был пуст, наполняем мы
# Сборка не удалась — убираем за собой и каталог --wheels-dir: был пуст, значит,
# всё в нём положено этим запуском. Иначе следующий запуск отказывал: «не пуст».
cleanup(){
  rm -rf "$WORK" 2>/dev/null || true
  if [ "$DONE" != "1" ] && [ -n "$WHEELS" ] && [ -d "$WHEELS" ]; then
    if [ "$WHEELS_OURS" = "created" ]; then
      rm -rf "$WHEELS" 2>/dev/null || true
    elif [ "$WHEELS_OURS" = "filled" ]; then
      find "$WHEELS" -mindepth 1 -delete 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'err "Команда завершилась с ошибкой (${BASH_SOURCE[0]##*/}:$LINENO): $BASH_COMMAND"' ERR

if [ -n "$KEEP_WHEELS" ]; then
  if [ -d "$KEEP_WHEELS" ]; then
    WHEELS="$(cd -- "$KEEP_WHEELS" >/dev/null 2>&1 && pwd)"
    # Прежние пакеты (другой версии приложения, под другой Python) попали бы в архив.
    if [ -n "$(ls -A "$WHEELS" 2>/dev/null || true)" ]; then
      err "Каталог $WHEELS не пуст: прежние пакеты попали бы в архив. Очистите его или укажите другой."
      WHEELS=""           # чужое содержимое — не наше, при выходе не трогаем
      exit 1
    fi
    WHEELS_OURS="filled"
  else
    mkdir -p "$KEEP_WHEELS"
    WHEELS="$(cd -- "$KEEP_WHEELS" >/dev/null 2>&1 && pwd)"
    WHEELS_OURS="created"
  fi
else
  WHEELS="$WORK/wheels"
  mkdir -p "$WHEELS"
fi
OUT_DIR="${OUT_DIR:-$ROOT}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd -- "$OUT_DIR" >/dev/null 2>&1 && pwd)"

# ---------- 1. Исходники выпуска ----------
# Пакет приложения собирается не из рабочего каталога, а из распакованного
# архива выпуска: в пакет попадает ровно то, что получит сервер при обычной
# установке, а в каталоге проекта не остаётся build/ и *.egg-info (остатки
# прежней сборки в build/ попадали бы в новый пакет).
info "Архив выпуска (исходники)…"
mkdir -p "$WORK/release" "$WORK/src"
if ! "$PY" "$ROOT/scripts/make_release.py" "$WORK/release" > "$WORK/release.log" 2>&1; then
  cat "$WORK/release.log" >&2 || true
  err "Не удалось собрать архив выпуска (сообщение выше)."
  exit 1
fi
"$PY" - "$WORK/release" "$WORK/src" <<'PY'
import os
import sys
import tarfile

folder, target = sys.argv[1], sys.argv[2]
archives = [name for name in os.listdir(folder) if name.endswith(".tar.gz")]
if len(archives) != 1:
    sys.exit("Ожидался один архив выпуска, найдено: %s" % (archives or "ничего"))
with tarfile.open(os.path.join(folder, archives[0])) as tar:
    # фильтр «data» есть не во всех версиях Python; архив только что собран нами
    if hasattr(tarfile, "data_filter"):
        tar.extractall(target, filter="data")
    else:
        tar.extractall(target)
PY
SRC="$WORK/src/mail_backup"
VERSION="$(head -n 1 "$SRC/VERSION")"

# ---------- 2. Пакет приложения ----------
info "Пакет приложения mailarchiver $VERSION…"
"$PY" -m pip wheel "$SRC" --no-deps -w "$WHEELS" \
  || { err "Не удалось собрать пакет приложения (сообщение pip выше). Нужен доступ в интернет: pip скачивает setuptools."; exit 1; }

# ---------- 3. Пакеты библиотек ----------
# «pip wheel», а не «pip download»: download кладёт в каталог и архивы
# исходников (если у библиотеки нет готового пакета), а собрать их без сети
# сервер не сможет. wheel собирает из них пакеты здесь — в каталоге остаются
# только .whl. С --only-binary сборка из исходников запрещена: тогда
# отсутствие готового пакета — отказ.
PIP_ARGS=(-r "$SRC/requirements.txt" -w "$WHEELS")
if [ "$ONLY_BINARY" = "1" ]; then PIP_ARGS+=(--only-binary=:all:); fi
info "Пакеты библиотек из requirements.txt (и всего, что им нужно)…"
"$PY" -m pip wheel "${PIP_ARGS[@]}" \
  || { err "Не удалось получить пакеты библиотек (сообщение pip выше)."
       if [ "$ONLY_BINARY" = "1" ]; then
         err "С --only-binary библиотека без готового пакета под этот Python — отказ; без параметра pip соберёт её из исходников (нужен компилятор)."
       fi
       exit 1; }

# ---------- 4. Архив ----------
info "Сборка автономного архива…"
RESULT="$("$PY" "$ROOT/scripts/make_release.py" --wheels "$WHEELS" "$OUT_DIR")" \
  || { err "Не удалось собрать автономный архив (сообщение выше)."; exit 1; }
printf '%s\n' "$RESULT"
ARCHIVE="${RESULT##*$'\n'}"       # последняя строка: «путь: N файлов, M байт»
ARCHIVE="${ARCHIVE%: *}"
if [ ! -f "$ARCHIVE" ]; then
  err "Архив собран, но найти его не удалось — смотрите в $OUT_DIR."
  exit 1
fi

DONE=1
ok "Готово: $ARCHIVE"
if [ -n "$KEEP_WHEELS" ]; then info "Пакеты оставлены в $WHEELS."; fi
cat <<EOF

  Архив собран для: $TARGET
  Он подойдёт серверу с той же версией Python (первые два числа) и той же
  архитектурой; система на сервере должна быть не старее, чем здесь.

  На сервере (доступ в интернет не нужен):
    tar -xzf ${ARCHIVE##*/}
    cd mail_backup
    sudo bash scripts/update.sh --check   # проверка: служба не останавливается, ничего не меняется
    sudo bash scripts/update.sh           # обновление  (первая установка: sudo bash scripts/install.sh)

  Скрипты сами найдут каталог wheels из архива. Пакеты, лежащие в другом
  месте, задаются переменной MA_WHEELS=/путь/к/wheels.
EOF
