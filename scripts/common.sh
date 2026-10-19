# shellcheck shell=bash
# ============================================================================
#  MailArchiver — общие функции install.sh и update.sh
#  Файл подключается командой source и сам по себе не запускается.
#
#  Ожидает переменные: APP_NAME APP_USER APP_GROUP INSTALL_DIR APP_DIR VENV_DIR
#  CONFIG_FILE DATA_DIR UNIT_FILE LOG_PREFIX и цвета RED GRN YLW BLU NC;
#  для подсказок — SCRIPT_NAME (install.sh | update.sh), для установки без
#  сети — SRC_DIR (каталог с исходниками новой версии).
#
#  Переменные окружения, которые разбираются здесь:
#    MA_START_TIMEOUT   сколько секунд ждать ответа веб-интерфейса (180)
#    MA_START_TIMEOUT_MAX  сколько секунд всего ждать службу, которая ещё занята
#                       (обновляет базу), когда MA_START_TIMEOUT вышел (3600)
#    MA_HEALTH_URL      свой адрес проверки вместо взятого из конфигурации
#    MA_RELAX_SANDBOX   1 — ослабить защиту службы для контейнеров, где systemd
#                       не может создать пространства имён (status=226/NAMESPACE)
#    MA_WHEELS          каталог с пакетами .whl для установки без сети
#    MA_INDEX_URL       свой индекс пакетов (зеркало PyPI) для установки из сети
#    MA_PROGRESS_EVERY  раз в сколько секунд долгие шаги — обход каталога данных
#                       (проверка владельца файлов), ожидание запуска службы — пишут,
#                       как идут дела (30)
#
#  Только для тестов (в справке и документации их нет):
#    MA_FSTAB           файл вместо /etc/fstab — см. unmounted_fstab_mount
# ============================================================================

# Всё, что создают скрипты (код, окружение, unit, команда mailarchiver), должно
# читаться службой. У root бывает umask 077 (усиленные серверы) — тогда каталог
# установки получался закрытым, и служба не запускалась без внятной причины.
umask 022

# Сообщения. «|| true»: если терминал пропал (оборвалась сессия SSH), запись
# завершается ошибкой, и при set -e на ней обрывался бы сам откат.
info(){ echo -e "${BLU}${LOG_PREFIX}${NC} $*" || true; }
ok(){ echo -e "${GRN}${LOG_PREFIX} ✓${NC} $*" || true; }
warn(){ echo -e "${YLW}${LOG_PREFIX} ⚠${NC} $*" || true; }
err(){ echo -e "${RED}${LOG_PREFIX} ✗${NC} $*" >&2 || true; }
# Продолжение сообщения (пояснение, что делать) — без знака: в выводе проверок
# на каждую приходится один ✓, ⚠ или ✗, и по знакам ✗ видно число отказов.
warn_more(){ echo -e "${YLW}${LOG_PREFIX}${NC}     $*" || true; }
err_more(){ echo -e "${RED}${LOG_PREFIX}${NC}     $*" >&2 || true; }

# Свой индекс пакетов (зеркало PyPI в локальной сети): pip получает его как
# PIP_INDEX_URL, и этот же адрес опрашивает проверка перед обновлением.
if [ -n "${MA_INDEX_URL:-}" ]; then export PIP_INDEX_URL="$MA_INDEX_URL"; fi

# Путь без «/» на конце (сам «/» остаётся). С косой чертой проверка «[ -L ]»
# для ссылки в никуда ложна — и каталог данных на неподключённом диске
# выглядел просто отсутствующим.
strip_slash(){
  local p="$1"
  while [ "$p" != "/" ] && [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  printf '%s\n' "$p"
}

# Команда от имени пользователя службы. Из корня: текущий каталог того, кто
# запустил скрипт (обычно /root/…), пользователю службы недоступен.
as_app_user(){
  if command -v runuser >/dev/null 2>&1; then
    ( trap - ERR; cd / && runuser -u "$APP_USER" -- "$@" )
  else
    # оболочка bash: printf %q записывает нелатинские пути так, что sh их не разберёт
    ( trap - ERR; cd / && su -s "${BASH:-/bin/sh}" "$APP_USER" -c "$(printf '%q ' "$@")" )
  fi
}

# Python из окружения службы. Ключ -I обязателен: скрипт запускают из каталога
# с исходниками, и без него «import mailarchiver» брал пакет из текущего
# каталога, а не установленный, — проверки и номер версии относились не к той
# установке, которая будет работать.
# Ключ -B: проверки идут от root, и без него каждая оставляла бы в окружении
# свои файлы .pyc — а «update.sh --check» обещает ничего не менять на диске.
venv_py(){ ( trap - ERR; cd / && "$VENV_DIR/bin/python" -I -B "$@" ); }

installed_version(){
  venv_py -c 'from mailarchiver.version import __version__; print(__version__)' 2>/dev/null || echo '?'
}

# Старые копии: оставить N последних. По имени (в нём дата), а не по времени
# изменения: cp -a сохраняет время каталога, у копий окружения оно одинаковое,
# и «ls -t» удалял самые свежие копии вместо самых старых.
keep_newest(){
  local keep="$1" dir="$2" prefix="$3" old
  { find "$dir" -mindepth 1 -maxdepth 1 -name "$prefix*" 2>/dev/null || true; } \
    | sort -r | tail -n +"$((keep + 1))" | while IFS= read -r old; do
        rm -rf "$old"
      done || true
}

# Сравнить номера версий по числам («1.9.10» новее «1.9.9»): печатает -1, 0 или 1.
version_cmp(){
  local i a b
  local -a x=() y=()
  IFS=. read -r -a x <<< "$1" || true
  IFS=. read -r -a y <<< "$2" || true
  for ((i = 0; i < ${#x[@]} || i < ${#y[@]}; i++)); do
    a="${x[i]:-0}"; b="${y[i]:-0}"
    a="${a%%[!0-9]*}"; b="${b%%[!0-9]*}"      # «1rc2» → 1
    a=$((10#${a:-0})); b=$((10#${b:-0}))
    if [ "$a" -lt "$b" ]; then echo -1; return 0; fi
    if [ "$a" -gt "$b" ]; then echo 1; return 0; fi
  done
  echo 0
}

# Номер версии из файла VERSION исходников. Пусто, если файла нет или в нём не
# номер версии: значение подставляется в имя пакета для pip.
source_version(){
  local v=""
  if [ -f "$SRC_DIR/VERSION" ]; then IFS= read -r v < "$SRC_DIR/VERSION" || true; fi
  v="${v//[[:space:]]/}"
  case "$v" in ''|*[!0-9A-Za-z.+!_-]*) v="" ;; esac
  printf '%s\n' "$v"
}

# Свободное место (КБ) там, где будет лежать ПУТЬ. Самого каталога может ещё не
# быть (каталог копий создаётся только перед самим обновлением) — тогда смотрим
# ближайший существующий каталог выше. Пусто, если узнать не удалось.
free_kb(){
  local dir="$1"
  while [ -n "$dir" ] && [ ! -d "$dir" ]; do
    case "$dir" in */*) dir="${dir%/*}" ;; *) dir="" ;; esac
  done
  { df -Pk "${dir:-/}" 2>/dev/null || true; } | awk 'NR==2{print $4}'
}

# Тип контейнера (lxc, openvz, docker…). Пусто — не контейнер или узнать нечем.
container_type(){
  local kind=""
  command -v systemd-detect-virt >/dev/null 2>&1 || return 0
  kind="$(systemd-detect-virt --container 2>/dev/null || true)"
  if [ -n "$kind" ] && [ "$kind" != "none" ]; then printf '%s\n' "$kind"; fi
}

# ---------------------------------------------------------------------------
#  Установка без доступа в интернет
# ---------------------------------------------------------------------------
# Каталог с готовыми пакетами (.whl): $MA_WHEELS, иначе wheels/ рядом с
# исходниками (он есть в автономном архиве — см. scripts/make_offline_bundle.sh).
# Если каталог найден, pip работает только с ним (--no-index): из пакетов
# ставятся и библиотеки, и само приложение. Собрать приложение из исходников
# без сети нельзя — для сборки pip скачивает setuptools.
WHEELS_DIR=""
PIP_OFFLINE=()

# has_wheel КАТАЛОГ [НАЧАЛО_ИМЕНИ] — есть ли в каталоге такой пакет.
has_wheel(){
  local f
  for f in "$1"/"${2:-}"*.whl; do
    if [ -f "$f" ]; then return 0; fi
  done
  return 1
}

count_wheels(){
  local f n=0
  for f in "$1"/*.whl; do
    if [ -f "$f" ]; then n=$((n + 1)); fi
  done
  echo "$n"
}

# Определить каталог пакетов (WHEELS_DIR; пусто — установка из сети).
# Заданный явно MA_WHEELS обязан существовать и содержать пакеты: молча пойти
# вместо него в сеть значило бы зависнуть на сервере без интернета уже после
# остановки службы.
detect_wheels(){
  local dir="${MA_WHEELS:-}" abs=""
  WHEELS_DIR=""; PIP_OFFLINE=()
  if [ -n "$dir" ]; then
    if [ ! -d "$dir" ]; then
      err "Каталог пакетов MA_WHEELS=$dir не найден."
      err_more "Укажите каталог wheels из автономного архива или уберите переменную (тогда установка пойдёт из сети)."
      return 1
    fi
    abs="$(cd -- "$dir" >/dev/null 2>&1 && pwd)" || abs=""
    if [ -z "$abs" ]; then
      err "Каталог пакетов MA_WHEELS=$dir недоступен."
      return 1
    fi
    if ! has_wheel "$abs"; then
      err "В каталоге MA_WHEELS=$abs нет ни одного пакета .whl."
      err_more "Соберите автономный архив на машине с интернетом: bash scripts/make_offline_bundle.sh"
      return 1
    fi
    dir="$abs"
  elif [ -n "${SRC_DIR:-}" ] && [ -d "$SRC_DIR/wheels" ] && has_wheel "$SRC_DIR/wheels"; then
    dir="$SRC_DIR/wheels"
  fi
  if [ -n "$dir" ]; then
    WHEELS_DIR="$dir"
    PIP_OFFLINE=(--no-index --find-links "$WHEELS_DIR")
  fi
  return 0
}

# Без сети приложение ставится из своего пакета: он должен лежать в каталоге,
# и именно той версии, что в файле VERSION исходников.
require_app_wheel(){
  local version="$1"
  if [ -z "$WHEELS_DIR" ]; then return 0; fi
  if [ -z "$version" ]; then
    err "Не удалось прочитать номер версии из ${SRC_DIR:-.}/VERSION — без него не выбрать пакет приложения в $WHEELS_DIR."
    return 1
  fi
  if ! has_wheel "$WHEELS_DIR" "mailarchiver-$version-"; then
    err "В каталоге пакетов $WHEELS_DIR нет пакета приложения mailarchiver-$version-*.whl."
    err_more "Без сети приложение ставится из него (сборка из исходников требует интернета)."
    err_more "Соберите автономный архив заново на машине с интернетом: bash scripts/make_offline_bundle.sh"
    if [ -z "${MA_WHEELS:-}" ]; then
      err_more "Если пакеты должны загружаться из сети — уберите или переименуйте каталог $WHEELS_DIR."
    fi
    return 1
  fi
}

# Подходят ли пакеты каталога этому Python — по меткам в именах файлов (cp312,
# …x86_64). Печатает строки «TAG …» (под что нужны пакеты), «MISFIT …»
# (библиотеки, для которых подходящего пакета нет) и «SDIST n» (сколько в
# каталоге архивов исходников). Проверка приблизительная — точно решает pip.
wheels_report(){   # wheels_report PYTHON КАТАЛОГ
  ( trap - ERR; cd / && "$1" -I -B - "$2" <<'PY' 2>/dev/null
import os, re, sys, sysconfig

minor = sys.version_info[1]
machine = sysconfig.get_platform().rsplit("-", 1)[-1]          # linux-x86_64 → x86_64


def python_fits(py, abi):
    for tag in py.split("."):
        m = re.fullmatch(r"(py|cp)3(\d*)", tag)
        if not m:
            continue
        kind, want = m.group(1), m.group(2)
        if not want:
            return True                                   # py3 — любой Python 3
        if kind == "py" and int(want) <= minor:
            return True
        if kind == "cp" and (int(want) == minor or (int(want) < minor and "abi3" in abi.split("."))):
            return True
    return False


def platform_fits(plat):
    return any(tag == "any" or tag.endswith("_" + machine) for tag in plat.split("."))


seen, fits, sdists = set(), set(), 0
for name in sorted(os.listdir(sys.argv[1])):
    if name.endswith((".tar.gz", ".tar.bz2", ".zip")):
        sdists += 1
    if not name.endswith(".whl"):
        continue
    parts = name[:-4].split("-")
    if len(parts) < 5:
        continue
    dist = re.sub(r"[-_.]+", "-", parts[0]).lower()       # PyYAML и pyyaml — одна библиотека
    seen.add(dist)
    if python_fits(parts[-3], parts[-2]) and platform_fits(parts[-1]):
        fits.add(dist)
print("TAG cp3%d %s" % (minor, machine))
print("MISFIT " + " ".join(sorted(seen - fits)))
print("SDIST %d" % sdists)
PY
  ) || true
}

# Предупредить, если пакеты каталога похожи на собранные под другой Python.
# Возвращает 1, если было о чём предупредить.
check_wheels_fit(){   # check_wheels_fit PYTHON
  local report tag bad sdists warned=0
  if [ -z "$WHEELS_DIR" ]; then return 0; fi
  report="$(wheels_report "$1" "$WHEELS_DIR")"
  tag="$(printf '%s\n' "$report" | sed -n 's/^TAG //p')"
  bad="$(printf '%s\n' "$report" | sed -n 's/^MISFIT //p')"
  sdists="$(printf '%s\n' "$report" | sed -n 's/^SDIST //p')"
  if [ -n "$bad" ]; then
    warned=1
    warn "В каталоге пакетов $WHEELS_DIR нет пакетов под этот Python (${tag:-?}) для: $bad."
    warn_more "Похоже, архив собран под другую версию Python или другую архитектуру — pip их не установит."
    warn_more "Соберите архив на машине с той же версией Python и той же архитектурой, что у сервера."
  fi
  if [ -n "$sdists" ] && [ "$sdists" != "0" ]; then
    warned=1
    warn "В каталоге пакетов $WHEELS_DIR есть архивы исходников ($sdists): без сети pip, скорее всего, не сможет их собрать."
    warn_more "Соберите архив заново: bash scripts/make_offline_bundle.sh — он кладёт в каталог только готовые пакеты."
  fi
  if [ "$warned" = "1" ]; then return 1; fi
  return 0
}

# Точная проверка каталога пакетов: пробная установка самим pip (--dry-run) —
# те же библиотеки из requirements.txt и тот же пакет приложения, что будут
# ставиться после остановки службы. Ничего не устанавливает и не пишет (кэш и
# .pyc отключены), сеть не нужна. Метки в именах файлов (check_wheels_fit) —
# лишь догадка: архив, собранный под другой Python, проходил проверку и
# отказывал уже при остановленной службе.
#   0 — pip нашёл всё; 1 — не нашёл (в stdout — последние строки его сообщения);
#   2 — этот pip не знает --dry-run (он появился в pip 22.2).
wheels_dry_run(){   # wheels_dry_run ВЕРСИЯ [ФАЙЛ_REQUIREMENTS]
  local out="" rc=0
  local -a args=(install --dry-run --quiet --disable-pip-version-check --no-cache-dir "${PIP_OFFLINE[@]}")
  if [ -n "${2:-}" ] && [ -f "$2" ]; then args+=(-r "$2"); fi
  args+=("mailarchiver==$1")
  out="$( ( trap - ERR; cd / && PYTHONDONTWRITEBYTECODE=1 "$VENV_DIR/bin/pip" "${args[@]}" ) 2>&1 )" || rc=$?
  if [ "$rc" = "0" ]; then return 0; fi
  case "$out" in *"no such option: --dry-run"*) return 2 ;; esac
  printf '%s\n' "$out" | tail -n 6
  return 1
}

# ---------------------------------------------------------------------------
#  Права на файлы
# ---------------------------------------------------------------------------
# Код и окружение: читать и входить в каталоги может любой, менять — только root.
# Права исходников переносить нельзя. Архив, собранный там, где у файлов права
# 0600, а у каталогов 0700 (или распакованный с umask 077), давал каталог
# $APP_DIR с правами 0700 root: служба не могла войти в свой рабочий каталог,
# systemd завершал её со status=200/CHDIR, а обновление откатывалось с одной
# строкой «Служба не запустилась».
#
# Каталог данных, если он оказался внутри $APP_DIR (установка «рядом с кодом»),
# не трогаем: письма и ключи не должны стать читаемыми для всех.
normalize_app_perms(){
  find "$APP_DIR" \( -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o \
       \( ! -user root -o ! -group root \) -exec chown -h root:root {} +
  find "$APP_DIR" \( -path "$APP_DIR/data" -o -path "$DATA_DIR" \) -prune -o \
       ! -type l -exec chmod u=rwX,go=rX {} +
  chmod 755 "$INSTALL_DIR" "$APP_DIR"
  if [ -d "$VENV_DIR" ]; then
    chmod -R a+rX "$VENV_DIR" 2>/dev/null || true
  fi
}

# Каталог данных: вернуть службе файлы, у которых владелец не тот (например
# созданные командами от root). Меняется только то, что нужно: на миллионах
# писем «chown -R» — долгая пауза, а резервные копии (borg, restic) из-за смены
# ctime перечитывают весь архив.
#
# Содержимое import/ не трогаем: туда администратор кладёт свои файлы для
# импорта, службе достаточно их читать. Сам каталог import/ остаётся за службой.
#
#   fix_data_owner          — весь каталог. Каждое письмо — отдельный файл, и на
#                             большом архиве обход идёт минуты (на диске, который
#                             только что подключили, — десятки минут). Поэтому
#                             update.sh делает его ДО остановки службы, а сам обход
#                             раз в MA_PROGRESS_EVERY секунд (30) пишет, сколько
#                             проверено: молчащий скрипт принимали за зависший.
#   fix_data_owner ГЛУБИНА  — только верхние уровни (find -maxdepth): быстро.
#
# Каталог данных бывает ссылкой на другой диск: работаем с тем, куда она ведёт
# (find в каталог-ссылку не входит, а «chown -h» менял владельца самой ссылки).
OWNER_SCAN_PID=""     # фоновый обход (его останавливает stop_owner_scan)
OWNER_SCAN_TMP=""
OWNER_FIXED=""        # сколько файлов с чужим владельцем нашёл последний полный обход
OWNER_SCANNED=0       # весь каталог уже обойдён (scan_data_owner_early)

fix_data_owner(){
  local depth="${1:-}" dir
  [ -d "$DATA_DIR" ] || return 0
  dir="$(readlink -f -- "$DATA_DIR" 2>/dev/null || true)"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then dir="$DATA_DIR"; fi
  OWNER_FIXED=""
  if [ -z "$depth" ] && _find_reports_progress; then
    _scan_data_owner "$dir"
    return 0
  fi
  # Путь — из каталога: «-path ./import/*» не зависит от знаков [ ] * ? в самом пути.
  local -a lim=()
  if [ -n "$depth" ]; then lim=(-maxdepth "$depth"); fi
  if _find_owner_expr_works "$dir"; then
    local errors rc=0
    errors="$(mktemp)"
    ( cd "$dir" && LC_ALL=C exec find . "${lim[@]}" -path './import/*' -prune -o \
        \( ! -user "$APP_USER" -o ! -group "$APP_GROUP" \) -exec chown -h "$APP_USER:$APP_GROUP" {} + ) \
      2>"$errors" || rc=$?
    _report_owner_errors "$dir" "$rc" "$errors"
    rm -f "$errors"
    return 0
  fi
  # запасной путь (такого выражения find не понимает): то же, по каталогам
  # верхнего уровня; что не вышло — предупреждение, а не отказ всего обновления
  local item failed=0
  chown "$APP_USER:$APP_GROUP" "$dir" 2>/dev/null || failed=1
  for item in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    if [ ! -e "$item" ] && [ ! -L "$item" ]; then continue; fi
    if [ "$item" = "$dir/import" ] || [ -n "$depth" ]; then
      chown -h "$APP_USER:$APP_GROUP" "$item" 2>/dev/null || failed=1
    else
      chown -R -h "$APP_USER:$APP_GROUP" "$item" 2>/dev/null || failed=1
    fi
  done
  if [ "$failed" = "1" ]; then
    warn "Владельца части файлов в $dir исправить не удалось — проверьте: ls -la \"$dir\""
  fi
}

# Весь каталог данных — пока служба ещё работает (update.sh; install.sh поверх
# установки): иначе минуты обхода большого архива становились минутами простоя.
scan_data_owner_early(){
  [ -d "$DATA_DIR" ] || return 0
  if [ "${MA_SKIP_OWNER_SCAN:-0}" = "1" ]; then
    warn "Проверка владельца файлов в каталоге данных пропущена (MA_SKIP_OWNER_SCAN=1)."
    return 0
  fi
  if systemctl is-active --quiet "$APP_NAME" 2>/dev/null; then
    info "Проверка владельца файлов в каталоге данных (служба пока работает; на большом архиве — несколько минут)…"
  else
    info "Проверка владельца файлов в каталоге данных (на большом архиве — несколько минут)…"
  fi
  fix_data_owner
  OWNER_SCANNED=1
}

# GNU find: умеет -fprintf (по нему считаем проверенное) и -ignore_readdir_race
# (файл, который служба удалила во время обхода, — не ошибка).
_find_reports_progress(){
  find / -maxdepth 0 -ignore_readdir_race -fprintf /dev/null '' >/dev/null 2>&1
}

# find понимает выражение обхода (-path, -prune, -user, -group, -exec … +).
_find_owner_expr_works(){
  ( cd "$1" && find . -maxdepth 0 -path './import/*' -prune -o \
      \( ! -user "$APP_USER" -o ! -group "$APP_GROUP" \) -exec true {} + ) >/dev/null 2>&1
}

# Ошибки обхода — предупреждением. «No such file or directory» не в счёт: файл
# убрала работающая служба между проверкой и chown (так бывает с журналами и
# выгрузками).
_report_owner_errors(){   # _report_owner_errors КАТАЛОГ КОД ФАЙЛ_ОШИБОК
  local dir="$1" rc="$2" errors="$3" line real
  [ "$rc" != "0" ] || return 0
  real="$(grep -v 'No such file or directory' "$errors" 2>/dev/null || true)"
  [ -n "$real" ] || return 0
  warn "Владельца части файлов в $dir исправить не удалось (пути — от этого каталога):"
  while IFS= read -r line; do warn_more "$line"; done < <(printf '%s\n' "$real" | head -n 5)
  warn_more "Служба может не суметь записать в эти каталоги — проверьте их: ls -la."
  OWNER_SCAN_FAILED=1
}
OWNER_SCAN_FAILED=0

# Полный обход в фоне; на переднем плане — отчёт о ходе. Фоновое задание
# Ctrl-C не получает (у скрипта без управления заданиями оно его игнорирует),
# поэтому ловушки выхода в install.sh и update.sh вызывают stop_owner_scan.
_scan_data_owner(){   # _scan_data_owner КАТАЛОГ
  local dir="$1" every="${MA_PROGRESS_EVERY:-30}" rc=0 start="$SECONDS" next seen spent
  case "$every" in ''|*[!0-9]*|0) every=30 ;; esac
  OWNER_SCAN_TMP="$(mktemp -d)"
  OWNER_SCAN_FAILED=0
  : > "$OWNER_SCAN_TMP/seen"; : > "$OWNER_SCAN_TMP/fixed"
  ( cd "$dir" && LC_ALL=C exec find . -ignore_readdir_race -path './import/*' -prune -o \
       -fprintf "$OWNER_SCAN_TMP/seen" 'x' \
       \( ! -user "$APP_USER" -o ! -group "$APP_GROUP" \) -fprintf "$OWNER_SCAN_TMP/fixed" 'x' \
       -exec chown -h "$APP_USER:$APP_GROUP" {} + ) 2>"$OWNER_SCAN_TMP/errors" &
  OWNER_SCAN_PID=$!
  next=$((start + every))
  while kill -0 "$OWNER_SCAN_PID" 2>/dev/null; do
    sleep 1
    if [ "$SECONDS" -ge "$next" ]; then
      next=$((SECONDS + every))
      seen="$(_file_size "$OWNER_SCAN_TMP/seen")"
      # счётчик пишется блоками по 4 КБ: в начале обхода он ещё пуст
      if [ "$seen" -gt 0 ]; then
        info "  …проверено файлов: $(_group_digits "$seen") ($(_elapsed $((SECONDS - start))))"
      else
        info "  …проверка идёт ($(_elapsed $((SECONDS - start))))"
      fi
    fi
  done
  wait "$OWNER_SCAN_PID" || rc=$?
  OWNER_SCAN_PID=""
  seen="$(_file_size "$OWNER_SCAN_TMP/seen")"
  OWNER_FIXED="$(_file_size "$OWNER_SCAN_TMP/fixed")"
  spent=$((SECONDS - start))
  _report_owner_errors "$dir" "$rc" "$OWNER_SCAN_TMP/errors"
  if [ "$OWNER_FIXED" -gt 0 ] && [ "$OWNER_SCAN_FAILED" = "1" ]; then
    info "Файлов с чужим владельцем: $(_group_digits "$OWNER_FIXED") из $(_group_digits "$seen");" \
         "исправить удалось не все (см. выше)."
  elif [ "$OWNER_FIXED" -gt 0 ]; then
    info "Владелец возвращён службе у файлов: $(_group_digits "$OWNER_FIXED") из $(_group_digits "$seen")" \
         "(например, созданных командами от root)."
  elif [ "$spent" -ge "$every" ]; then
    info "Владелец у всех файлов верный (проверено $(_group_digits "$seen") за $(_elapsed "$spent"))."
  fi
  rm -rf "$OWNER_SCAN_TMP"
  OWNER_SCAN_TMP=""
}

# Прервать фоновый обход (для ловушек выхода; без обхода ничего не делает).
# Не дольше 5 секунд: find, застрявший в вводе-выводе (зависший диск), не
# умирает и от SIGKILL — тогда говорим об этом и не ждём.
stop_owner_scan(){
  local pid="$OWNER_SCAN_PID" i
  if [ -n "$pid" ]; then
    OWNER_SCAN_PID=""
    pkill -TERM -P "$pid" 2>/dev/null || true      # уже запущенный chown
    kill -TERM "$pid" 2>/dev/null || true
    for i in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      sleep 0.5
    fi
    if kill -0 "$pid" 2>/dev/null; then
      warn "Обход каталога данных (find, процесс $pid) не завершается — похоже, диск не отвечает; оставляю его."
    else
      wait "$pid" 2>/dev/null || true
    fi
  fi
  if [ -n "$OWNER_SCAN_TMP" ]; then rm -rf "$OWNER_SCAN_TMP"; OWNER_SCAN_TMP=""; fi
}

_file_size(){ local n; n="$(wc -c < "$1" 2>/dev/null)" || n=0; echo $((n + 0)); }

_group_digits(){   # 1234567 → 1 234 567
  local n="$1" out=""
  while [ "${#n}" -gt 3 ]; do out=" ${n: -3}$out"; n="${n:0:${#n}-3}"; done
  printf '%s\n' "$n$out"
}

_elapsed(){   # _elapsed СЕКУНД → «45 с», «3 мин 12 с», «1 ч 05 мин»
  local s="$1"
  if [ "$s" -lt 60 ]; then printf '%d с\n' "$s"
  elif [ "$s" -lt 3600 ]; then printf '%d мин %02d с\n' $((s / 60)) $((s % 60))
  else printf '%d ч %02d мин\n' $((s / 3600)) $((s % 3600 / 60)); fi
}

# Пользователь службы существует, и от его имени можно выполнять команды.
check_service_user(){
  if ! id "$APP_USER" >/dev/null 2>&1; then
    err "В системе нет пользователя службы «$APP_USER». Если служба работает от другого — задайте MA_USER."
    return 1
  fi
  if ! as_app_user true 2>/dev/null; then
    err "Не удаётся выполнить команду от имени пользователя $APP_USER (runuser/su)."
    return 1
  fi
}

# Первый каталог на пути, в который пользователь службы не может войти (пусто —
# весь путь открыт).
closed_component(){
  local rest="${1#/}" part path=""
  while [ -n "$rest" ]; do
    part="${rest%%/*}"
    path="$path/$part"
    if [ "$rest" = "$part" ]; then rest=""; else rest="${rest#*/}"; fi
    if ! as_app_user test -x "$path" 2>/dev/null; then printf '%s\n' "$path"; return 0; fi
  done
}

# Путь каталога данных занят не каталогом — в том числе ссылкой в никуда.
data_dir_broken(){ { [ -L "$DATA_DIR" ] || [ -e "$DATA_DIR" ]; } && [ ! -d "$DATA_DIR" ]; }

# Точка монтирования из /etc/fstab, под которой лежит ПУТЬ и которая сейчас не
# подключена (пусто — такой нет или узнать нечем: нет findmnt).
#
# Зачем: неподключённый диск с данными выглядит как пустой каталог root:root —
# сама точка монтирования. Совет «chown» для него вреден: служба запустится на
# пустом каталоге и заведёт новую базу поверх точки монтирования.
#
# Путь берётся в настоящем виде (ссылки раскрыты; каталога может и не быть) и
# проверяется снизу вверх: findmnt ищет в fstab только точную точку
# монтирования, поиск «по каталогу внутри» (--target) для fstab не работает.
# Дойдя до подключённой файловой системы, останавливаемся: что объявлено выше
# неё, к пути уже не относится. Из нескольких неподключённых называется верхняя —
# её нужно подключить первой.
#
# MA_FSTAB — файл вместо /etc/fstab. Только для тестов: настоящий fstab они
# менять не могут.
unmounted_fstab_mount(){   # unmounted_fstab_mount ПУТЬ
  local dir found=""
  local -a tab=()
  command -v findmnt >/dev/null 2>&1 || return 0
  if [ -n "${MA_FSTAB:-}" ]; then tab=(--tab-file "$MA_FSTAB"); fi
  dir="$(readlink -m -- "$1" 2>/dev/null || true)"
  case "$dir" in /*) ;; *) return 0 ;; esac
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if findmnt --kernel --noheadings --output TARGET --mountpoint "$dir" >/dev/null 2>&1; then break; fi
    if findmnt --fstab ${tab[@]+"${tab[@]}"} --noheadings --output TARGET --mountpoint "$dir" >/dev/null 2>&1; then
      found="$dir"
    fi
    dir="${dir%/*}"
  done
  if [ -n "$found" ]; then printf '%s\n' "$found"; fi
  return 0
}

# Ближайший существующий каталог на пути (ссылки раскрыты): для ссылки в никуда
# /storage/disk/mailarchiver — это /storage/disk, если его нет — /storage и т. д.
existing_parent(){   # existing_parent ПУТЬ
  local dir
  dir="$(readlink -m -- "$1" 2>/dev/null || true)"
  case "$dir" in /*) ;; *) return 0 ;; esac
  while [ -n "$dir" ] && [ ! -d "$dir" ]; do dir="${dir%/*}"; done
  printf '%s\n' "${dir:-/}"
}

# Файловые системы на дисках, которые сейчас не подключены (по lsblk), строками
# «УСТРОЙСТВО|ТИП|РАЗМЕР|UUID|МЕТКА». Без подкачки, образов (loop, squashfs,
# компакт-диски) и составных частей LVM, RAID, ZFS и зашифрованных разделов: их
# подключают не командой mount — о них говорится отдельно (unmounted_containers).
#
# MA_LSBLK — команда вместо lsblk. Только для тестов: настоящие диски машины
# тесты видеть не должны.
_lsblk_pairs(){
  local cmd="${MA_LSBLK:-lsblk}"
  command -v "$cmd" >/dev/null 2>&1 || return 0
  "$cmd" -P -p -o NAME,TYPE,FSTYPE,MOUNTPOINT,SIZE,UUID,LABEL 2>/dev/null || true
}
_lsblk_field(){   # _lsblk_field СТРОКА КЛЮЧ
  if [[ " $1" =~ \ $2=\"([^\"]*)\" ]]; then printf '%s' "${BASH_REMATCH[1]}"; fi
}
unmounted_filesystems(){
  local line type fstype mnt
  while IFS= read -r line; do
    type="$(_lsblk_field "$line" TYPE)"; fstype="$(_lsblk_field "$line" FSTYPE)"
    mnt="$(_lsblk_field "$line" MOUNTPOINT)"
    [ -n "$fstype" ] && [ -z "$mnt" ] || continue
    case "$type" in loop|rom) continue ;; esac
    case "$fstype" in
      swap|squashfs|iso9660|udf|LVM2_member|linux_raid_member|zfs_member|crypto_LUKS|BitLocker) continue ;;
    esac
    printf '%s|%s|%s|%s|%s\n' "$(_lsblk_field "$line" NAME)" "$fstype" "$(_lsblk_field "$line" SIZE)" \
      "$(_lsblk_field "$line" UUID)" "$(_lsblk_field "$line" LABEL)"
  done < <(_lsblk_pairs)
}
unmounted_containers(){   # зашифрованные разделы и части томов, которые сейчас не в работе: «УСТРОЙСТВО (ТИП)»
  local line fstype name
  local -a used=()
  while IFS= read -r line; do
    name="$(_lsblk_field "$line" NAME)"; fstype="$(_lsblk_field "$line" FSTYPE)"
    case "$fstype" in
      crypto_LUKS|BitLocker|LVM2_member|linux_raid_member) used+=("$name|$fstype") ;;
    esac
  done < <(_lsblk_pairs)
  # у открытого или собранного — есть устройство уровнем ниже (lsblk -P печатает его следом)
  local item dev kind
  for item in ${used[@]+"${used[@]}"}; do
    dev="${item%%|*}"; kind="${item#*|}"
    if ! "${MA_LSBLK:-lsblk}" -n -p -o NAME "$dev" 2>/dev/null | sed -n '2p' | grep -q .; then
      printf '%s (%s)\n' "$dev" "$kind"
    fi
  done
}

# Пулы ZFS, которые есть на дисках (по lsblk: FSTYPE=zfs_member, метка — имя
# пула), но сейчас не импортированы, строками «ПУЛ|УСТРОЙСТВА». Импортированный
# пул в lsblk выглядит так же (точки монтирования у раздела нет), поэтому
# импортированные берутся из «zpool list». Нет команды zpool — значит, и пул не
# импортирован (без неё ZFS не работает).
unimported_zfs_pools(){
  local line fstype name label imported=" " pool devs
  local -A pools=()
  while IFS= read -r line; do
    fstype="$(_lsblk_field "$line" FSTYPE)"
    [ "$fstype" = "zfs_member" ] || continue
    name="$(_lsblk_field "$line" NAME)"; label="$(_lsblk_field "$line" LABEL)"
    label="${label:-?}"
    pools[$label]="${pools[$label]:+${pools[$label]} }$name"
  done < <(_lsblk_pairs)
  [ "${#pools[@]}" -gt 0 ] || return 0
  if command -v zpool >/dev/null 2>&1; then
    imported=" $(zpool list -H -o name 2>/dev/null | tr '\n' ' ')"
  fi
  for pool in "${!pools[@]}"; do
    case "$imported" in *" $pool "*) continue ;; esac
    printf '%s|%s\n' "$pool" "${pools[$pool]}"
  done | sort
}

# Почему нет каталога, на который ведёт путь, и что подключить: ближайший
# существующий каталог, неподключённые файловые системы и строка для /etc/fstab.
# Только для пути, которого нет в /etc/fstab (иначе причину называет
# unmounted_fstab_mount): так бывает, когда диск или сетевой ресурс подключали
# вручную, а после перезагрузки его никто не подключил.
explain_missing_disk(){   # explain_missing_disk ЦЕЛЬ [found — база уже нашлась в другом месте]
  local target="$1" found="${2:-}" parent fs="" containers="" zpools="" line dev type size uuid label first="" count=0
  local pool devs here dropin_dir="${UNIT_FILE:-/etc/systemd/system/$APP_NAME.service}.d"
  parent="$(existing_parent "$target")"
  zpools="$(unimported_zfs_pools)"
  if [ -n "$found" ]; then
    :   # база нашлась в другом месте — гадать, что подключали в $parent, незачем
  elif [ -z "$parent" ] || [ "$parent" = "/" ]; then
    err_more "Ни одного каталога на пути $target нет."
  elif findmnt --kernel --noheadings --mountpoint "$parent" >/dev/null 2>&1; then
    err_more "Каталог $parent есть (к нему подключена файловая система), но в нём нет ${target#"$parent"/}."
  elif [ -n "$zpools" ] && [ -z "$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
    err_more "Каталог $parent есть, но он пуст и к нему ничего не подключено — так выглядит точка монтирования"
    err_more "пула ZFS, который не импортирован (о пулах — ниже)."
  elif [ -z "$(find "$parent" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
    err_more "Каталог $parent есть, но он пуст и к нему ничего не подключено, а в /etc/fstab о нём нет ни строки:"
    err_more "похоже, диск или сетевой ресурс с данными подключали в $parent вручную, и сейчас он не подключён"
    err_more "(например, после перезагрузки сервера)."
  else
    err_more "Каталог $parent есть, но в нём нет ${target#"$parent"/}."
  fi
  fs="$(unmounted_filesystems)"
  containers="$(unmounted_containers)"
  if [ -n "$fs" ]; then
    if [ -n "$found" ]; then
      err_more "Если данные всё же на диске, который не подключён, — вот какие файловые системы сейчас не подключены (lsblk):"
    else
      err_more "Файловые системы, которые сейчас не подключены (lsblk):"
    fi
    while IFS='|' read -r dev type size uuid label; do
      [ -n "$dev" ] || continue
      count=$((count + 1)); [ -n "$first" ] || first="$dev|$type|$uuid"
      err_more "  $dev — $type, $size${label:+, метка «$label»}${uuid:+, UUID=$uuid}"
    done <<< "$fs"
    if [ "$parent" != "/" ] && [ -n "$parent" ]; then
      if [ "$count" = "1" ]; then
        IFS='|' read -r dev type uuid <<< "$first"
        err_more "Подключите её и проверьте, что данные на месте: mount $dev \"$parent\"; ls -la \"$target/\""
        err_more "Чтобы диск подключался сам после перезагрузки, добавьте в /etc/fstab строку:"
        err_more "  UUID=$uuid $parent $type defaults,nofail 0 2"
      else
        err_more "Подключите ту, на которой данные, и проверьте: mount УСТРОЙСТВО \"$parent\"; ls -la \"$target/\""
        err_more "Чтобы диск подключался сам после перезагрузки — строка в /etc/fstab: UUID=… $parent ТИП defaults,nofail 0 2"
      fi
    fi
  fi
  if [ -n "$containers" ]; then
    err_more "Не открыты (не собраны) зашифрованные разделы или тома: $(printf '%s' "$containers" | tr '\n' ' ')"
    err_more "Если данные на них — откройте или соберите (cryptsetup open, vgchange -ay, mdadm --assemble --scan) и подключите."
  fi
  if [ -n "$zpools" ]; then
    while IFS='|' read -r pool devs; do
      [ -n "$pool" ] || continue
      here=""
      case "$target/" in "/$pool/"*) here=" — путь $target лежит в его точке монтирования по умолчанию (/$pool)" ;; esac
      err_more "Пул ZFS «$pool» ($devs) не импортирован$here."
      if command -v zpool >/dev/null 2>&1; then
        err_more "Подключите его и проверьте, что данные на месте: zpool import $pool; zfs mount -a; ls -la \"$target/\""
        err_more "(«pool was previously in use from another system» — zpool import -f $pool, только если диск больше нигде не подключён)."
      else
        err_more "Команды zpool нет — установите утилиты ZFS (Ubuntu, Debian: apt install zfsutils-linux), затем: zpool import $pool"
      fi
      err_more "Чтобы пул подключался сам после перезагрузки: zpool set cachefile=/etc/zfs/zpool.cache $pool;"
      err_more "  systemctl enable zfs-import-cache.service zfs-mount.service zfs.target"
      err_more "и чтобы служба его дожидалась — дополнение к unit-файлу (обновление его не трогает):"
      err_more "  mkdir -p \"$dropin_dir\""
      err_more "  printf '[Unit]\\\\nRequires=zfs-mount.service\\\\nAfter=zfs-mount.service\\\\n' > \"$dropin_dir/zfs.conf\"; systemctl daemon-reload"
    done <<< "$zpools"
  fi
  if [ -z "$fs" ] && [ -z "$containers" ] && [ -z "$zpools" ] && [ -z "$found" ]; then
    err_more "Неподключённых дисков lsblk не видит. Возможно, данные были на сетевом ресурсе (NFS, SMB) — подключите"
    err_more "его${parent:+ в $parent}; или каталог данных переносили и ссылку не поправили — найдите базу:"
    err_more "  find / \\( -path /proc -o -path /sys -o -path /run -o -path /dev \\) -prune -o -name mailarchiver.db -print"
  fi
}

# Где ещё лежит база службы, если путь каталога данных ведёт в никуда: каталоги
# с mailarchiver.db рядом с целью ссылки и в обычных местах для данных. Так
# бывает, когда каталог данных перенесли, а ссылку сделали с опечаткой
# (/storage/storage/… вместо /storage/…) или не поправили. Служба при этом
# могла неделями работать дальше — с каталогом, открытым до переноса, — и
# перестать запускаться только при первом перезапуске (обновлении).
#
# Ищем неглубоко и недолго (каждое место — не дольше 15 секунд), ничего не
# меняем. Строки «КАТАЛОГ|РАЗМЕР_БАЗЫ_БАЙТ|ВРЕМЯ_БАЗЫ_UNIX|secret.key|mailboxes»
# (последние два поля — 1/0), самые свежие первыми, не больше пяти.
#
# MA_DATA_SEARCH — места для поиска вместо обычных мест для данных (/srv, /mnt …;
# через «:»). Только для тестов: настоящие каталоги машины тесты видеть не должны.
find_data_candidates(){   # find_data_candidates ЦЕЛЬ
  local target="$1" parent top rest root depth db dir self found="" line
  local -a roots=() extra=()
  parent="$(existing_parent "$target")"
  rest="${target#/}"; top="/${rest%%/*}"
  self="$(readlink -m -- "$DATA_DIR" 2>/dev/null || true)"
  # рядом с целью ссылки: ближайший существующий каталог, каталог над ним и верхний каталог пути
  roots=("$parent" "${parent%/*}" "$top")
  if [ -n "${MA_DATA_SEARCH:-}" ]; then
    IFS=':' read -r -a extra <<< "$MA_DATA_SEARCH"
  else
    extra=("$(dirname -- "$DATA_DIR")" /srv /mnt /media /data /storage /backup /var/backups)
  fi
  roots+=(${extra[@]+"${extra[@]}"})
  for root in ${roots[@]+"${roots[@]}"}; do
    # /tmp целиком — не место для данных (служба видит свой /tmp: PrivateTmp), /home ей закрыт (ProtectHome)
    case "$root" in ""|/|/tmp|/var/tmp|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/run|/run/*|/home|/home/*) continue ;; esac
    [ -d "$root" ] || continue
    case ":$found:" in *":$root:"*) continue ;; esac
    found="$found:$root"
    depth=3
    if [ "$root" = "$top" ]; then depth=4; fi
    while IFS= read -r db; do
      dir="${db%/mailarchiver.db}"
      [ -n "$dir" ] && [ "$dir" != "$self" ] || continue
      printf '%s|%s|%s|%s|%s\n' "$dir" "$(stat -c %s "$db" 2>/dev/null || echo 0)" \
        "$(stat -c %Y "$db" 2>/dev/null || echo 0)" \
        "$([ -f "$dir/secret.key" ] && echo 1 || echo 0)" "$([ -d "$dir/mailboxes" ] && echo 1 || echo 0)"
    done < <(_with_timeout 15 find -H "$root" -maxdepth "$depth" -type f -name mailarchiver.db -print 2>/dev/null || true)
  done | sort -t'|' -k1,1 -u | sort -t'|' -k3,3nr | head -n 5
}

# Команда с пределом времени, если есть timeout (coreutils), иначе как есть.
_with_timeout(){   # _with_timeout СЕКУНД КОМАНДА…
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi
}

_human_bytes(){   # _human_bytes БАЙТ
  awk -v b="${1:-0}" 'BEGIN { split("Б КБ МБ ГБ ТБ", u, " "); i = 1;
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    if (i == 1) printf "%d %s", b, u[i]; else printf "%.1f %s", b, u[i] }' | sed 's/\./,/'
}

# Рассказать о найденных базах и дать команду, которая направит путь каталога
# данных на нужную (сами данные не перемещаются). Код возврата 0 — что-то нашлось.
suggest_data_candidates(){   # suggest_data_candidates ЦЕЛЬ
  local cands dir size when key boxes marks first="" count=0 how
  cands="$(find_data_candidates "$1")"
  [ -n "$cands" ] || return 1
  err_more "База службы (mailarchiver.db) нашлась в другом месте — похоже, каталог данных перенесли, а путь не"
  err_more "поправили (или в ссылке опечатка):"
  while IFS='|' read -r dir size when key boxes; do
    [ -n "$dir" ] || continue
    count=$((count + 1)); [ -n "$first" ] || first="$dir"
    marks=""
    if [ "$key" = "1" ]; then marks="$marks, есть secret.key"; else marks="$marks, НЕТ secret.key"; fi
    if [ "$boxes" = "1" ]; then marks="$marks и mailboxes/"; else marks="$marks, нет mailboxes/"; fi
    err_more "  $dir — база $(_human_bytes "$size"), изменена $(date -d "@$when" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')$marks"
  done <<< "$cands"
  if [ -L "$DATA_DIR" ]; then how="ln -sfn"; else how="ln -s"; fi
  if [ "$count" = "1" ]; then
    err_more "Если это они — направьте путь туда (сами данные не перемещаются): $how \"$first\" \"$DATA_DIR\""
  else
    err_more "Направьте путь на настоящий (самый свежий — первый в списке; сами данные не перемещаются):"
    err_more "  $how КАТАЛОГ \"$DATA_DIR\""
  fi
  err_more "Каталог с secret.key и mailboxes/ рядом с базой — настоящий каталог данных; без secret.key не"
  err_more "расшифруются пароли ящиков."
  return 0
}

# Каталог данных доступен службе и не пуст, но лежит под точкой монтирования,
# которая не подключена: служба работает с тем, что на диске под ней. Это не
# отказ (возможно, строка в fstab просто устарела), но сказать о нём нужно.
warn_data_dir_under_mount(){   # warn_data_dir_under_mount ТОЧКА
  warn "Каталог данных $DATA_DIR лежит под точкой монтирования $1 из /etc/fstab, а она сейчас не подключена."
  warn_more "Служба работает с тем, что лежит на диске под ней. Если данные должны быть на отдельном диске —"
  warn_more "остановите службу, подключите его (mount \"$1\") и выясните, какая из двух копий данных настоящая."
  warn_more "Если отдельного диска больше нет — уберите строку о $1 из /etc/fstab."
}

# Каталог данных: он есть, это каталог, служба может в него войти и писать, и
# он не лежит на неподключённом диске. После вызова DATA_DIR_BAD=1, если
# проверка не пройдена.
#
# Отдельно разбирается ссылка, ведущая в никуда. Так выглядит каталог данных,
# вынесенный на другой диск, когда этот диск не подключён (после перезагрузки,
# сбоя монтирования). С ним не запустится никакая версия: каталог указан в
# ReadWritePaths, и systemd завершает службу со status=226/NAMESPACE. Раньше
# скрипт этого не замечал («[ -d ]» для такой ссылки ложно, и проверка молча
# пропускалась): обновление доходило до пробного импорта, откатывалось — и
# прежняя версия тоже не поднималась, уже без понятной причины.
#
# Пустой каталог — не отказ (так выглядит новая установка), если только он не
# под неподключённой точкой монтирования из /etc/fstab.
DATA_DIR_BAD=0
DATA_DIR_FROM=""       # откуда взят путь («из переменной MA_DATA_DIR», «из конфигурации …») — для сообщений
check_data_dir(){
  local state="" mp="" real="" hop="" dir="" shown=""
  DATA_DIR_BAD=0
  if [ -L "$DATA_DIR" ] && [ ! -e "$DATA_DIR" ]; then state="dangling"
  elif [ -e "$DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then state="notdir"
  elif [ ! -e "$DATA_DIR" ]; then state="missing"
  elif ! as_app_user test -x "$DATA_DIR" 2>/dev/null; then state="closed"
  elif ! as_app_user test -w "$DATA_DIR" 2>/dev/null; then state="readonly"
  elif [ -z "$(find -H "$DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then state="empty"
  else return 0
  fi
  if [ "$state" != "notdir" ]; then mp="$(unmounted_fstab_mount "$DATA_DIR")"; fi
  if [ "$state" = "empty" ] && [ -z "$mp" ]; then return 0; fi
  DATA_DIR_BAD=1
  real="$(readlink -m -- "$DATA_DIR" 2>/dev/null || true)"      # куда путь ведёт на самом деле
  shown="$DATA_DIR"
  if [ -n "$real" ] && [ "$real" != "$DATA_DIR" ]; then shown="$DATA_DIR ($real)"; fi

  # Диск не подключён — причина в нём, каким бы ни выглядел каталог. Советов
  # «создайте каталог» и «смените владельца» здесь быть не должно.
  if [ -n "$mp" ]; then
    err "Каталог данных $shown лежит на неподключённом диске — файловая система $mp из /etc/fstab не подключена: mount \"$mp\""
    case "$state" in
      dangling) err_more "(сейчас $DATA_DIR — ссылка на каталог, которого нет)" ;;
      missing)  err_more "(сейчас каталога нет)" ;;
      empty)    err_more "(сейчас каталог пуст: это место под точкой монтирования, а не данные)" ;;
      *)        err_more "(сейчас служба — пользователь $APP_USER — не может в него войти или писать: это место под точкой монтирования, а не данные)" ;;
    esac
    err_more "Подключите её и убедитесь, что данные на месте: ls \"$DATA_DIR/\"   — затем: systemctl reset-failed $APP_NAME; systemctl start $APP_NAME"
    err_more "Владельца и права не меняйте, каталог не создавайте: служба начала бы работу с пустой базой на месте точки монтирования."
    return 1
  fi

  case "$state" in
    dangling)
      # конечная цель (ссылка может вести через другие ссылки или быть относительной)
      hop="$(readlink -- "$DATA_DIR" 2>/dev/null || true)"
      shown="${real:-${hop:-другой каталог}}"
      if [ -n "$hop" ] && [ "$hop" != "$shown" ]; then shown="$shown (в самой ссылке записано: $hop)"; fi
      err "Каталог данных $DATA_DIR — ссылка на $shown, а такого каталога сейчас нет."
      if suggest_data_candidates "${real:-$hop}"; then
        explain_missing_disk "${real:-$hop}" found
      else
        err_more "Обычно это значит, что не подключён диск, на котором лежат данные."
        explain_missing_disk "${real:-$hop}"
      fi
      err_more "Каталог ${real:-$hop} не создавайте: служба начала бы работу с пустой базой."
      err_more "Когда данные на месте (ls \"$DATA_DIR/\" показывает mailarchiver.db): systemctl reset-failed $APP_NAME; systemctl start $APP_NAME"
      err_more "Пока каталога нет, служба не запустится ни в какой версии — и обновление её не починит."
      ;;
    notdir)
      err "Каталог данных $DATA_DIR — не каталог ($(stat -c '%F' "$DATA_DIR" 2>/dev/null || echo '?'))."
      err_more "Если данные лежат в другом месте — задайте MA_DATA_DIR."
      ;;
    missing)
      # без слов «указан в ReadWritePaths»: unit-файла может не быть вовсе
      err "Нет каталога данных $shown: без него служба не запустится."
      if [ -n "$DATA_DIR_FROM" ]; then err_more "Путь взят $DATA_DIR_FROM."; fi
      if [ "${SCRIPT_NAME:-}" = "update.sh" ]; then
        # Служба уже установлена — данные были здесь. Совет создать каталог тут
        # вреден: если диск подключается не через /etc/fstab (unit .mount, autofs,
        # вручную), служба, которую systemd перезапускает каждые 5 секунд, тут же
        # поднялась бы на пустом каталоге с новой базой.
        err_more "Служба уже установлена, значит данные были в нём. Если они на отдельном диске или сетевом"
        err_more "ресурсе — подключите его и убедитесь, что данные на месте: ls \"$DATA_DIR/\""
        if suggest_data_candidates "${real:-$DATA_DIR}"; then
          explain_missing_disk "${real:-$DATA_DIR}" found
        else
          explain_missing_disk "${real:-$DATA_DIR}"
        fi
        err_more "Если данные в другом каталоге — задайте MA_DATA_DIR. Каталог не создавайте: служба начала бы работу с пустой базой."
      else
        err_more "Если данные лежат на отдельном диске — проверьте, что он подключён (findmnt; mount -a)."
        err_more "Если в другом каталоге — задайте MA_DATA_DIR. Пустой каталог для новой установки:"
        err_more "  install -d -o $APP_USER -g $APP_GROUP -m 750 \"$DATA_DIR\""
      fi
      ;;
    closed|readonly)
      # Обновление, а каталог пуст и закрыт для службы: так выглядит точка
      # монтирования диска или ресурса, который не подключён (и не описан в
      # /etc/fstab — иначе сработала бы ветка выше). Совет «chown» здесь вреден.
      if [ "${SCRIPT_NAME:-}" = "update.sh" ] && \
         [ -z "$(find -H "$DATA_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)" ]; then
        err "Каталог данных $shown пуст, и служба (пользователь $APP_USER) не может в него писать."
        err_more "Так выглядит точка монтирования неподключённого диска или сетевого ресурса: служба уже"
        err_more "установлена, значит данные были здесь. Подключите его (findmnt; lsblk; mount -a) и проверьте: ls \"$DATA_DIR/\""
        err_more "Владельца и права не меняйте, пока не убедитесь, что данные на месте: служба начала бы работу с пустой базой."
        return 1
      fi
      ;;&
    closed)
      # Какой каталог закрыт: сначала по пути, как он записан (ссылка может
      # лежать в закрытом каталоге), затем по тому, куда он ведёт.
      dir="$(closed_component "$DATA_DIR")"
      if [ "$dir" = "$DATA_DIR" ] && [ -n "$real" ] && [ "$real" != "$DATA_DIR" ]; then
        dir="$(closed_component "$real")"
      fi
      err "Служба (пользователь $APP_USER) не может войти в каталог данных $shown."
      if [ -z "$dir" ]; then
        err_more "Проверьте права на него и на каталоги выше: namei -l \"$DATA_DIR\""
      elif [ "$dir" = "$DATA_DIR" ] || [ "$dir" = "$real" ]; then
        err_more "Он закрыт для неё (права $(stat -c '%A %U:%G' "$dir" 2>/dev/null || echo '?'))."
        err_more "Исправьте: chown $APP_USER:$APP_GROUP \"$dir\" && chmod 750 \"$dir\""
      else
        err_more "Закрыт каталог $dir (права $(stat -c '%A %U:%G' "$dir" 2>/dev/null || echo '?'))."
        err_more "Исправьте: chmod o+x \"$dir\"  — и запустите скрипт заново."
      fi
      ;;
    *)
      err "Служба (пользователь $APP_USER) не может писать в каталог данных $shown."
      err_more "Исправьте: chown $APP_USER:$APP_GROUP \"${real:-$DATA_DIR}\" && chmod 750 \"${real:-$DATA_DIR}\""
      ;;
  esac
  return 1
}

# Служба должна суметь войти в каталог (по умолчанию — свой рабочий), запустить
# свой исполняемый файл, прочитать конфигурацию и писать в каталог данных.
# systemd об отказе сообщает только в журнале (status=200/CHDIR, 203/EXEC,
# 226/NAMESPACE) — проверяем заранее и называем причину сами.
#   check_service_access            — всё, перед запуском службы;
#   check_service_access КАТАЛОГ    — только вход в каталог, конфигурация и данные
#                                     (до остановки службы, пока ничего не тронуто).
check_service_access(){
  local target="${1:-$APP_DIR}" bad=0 dir=""
  check_service_user || return 1
  if ! as_app_user test -x "$target" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может войти в каталог $target."
    dir="$(closed_component "$target")"      # какой именно каталог на пути закрыт
    if [ -n "$dir" ]; then
      err_more "Закрыт каталог $dir (права $(stat -c '%A %U:%G' "$dir" 2>/dev/null || echo '?'))."
      err_more "Исправьте: chmod o+x \"$dir\"  — и запустите скрипт заново."
    else
      err_more "Проверьте права на него и на каталоги выше: namei -l \"$target\""
    fi
  elif [ -z "${1:-}" ] && ! as_app_user test -x "$VENV_DIR/bin/mailarchiver" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может запустить $VENV_DIR/bin/mailarchiver — недоступно окружение."
    err_more "Проверьте права: ls -ld \"$INSTALL_DIR\" \"$VENV_DIR\" \"$VENV_DIR/bin\""
  fi
  if [ "$bad" = "0" ] && [ -f "$CONFIG_FILE" ] && ! as_app_user test -r "$CONFIG_FILE" 2>/dev/null; then
    bad=1
    err "Служба (пользователь $APP_USER) не может прочитать конфигурацию $CONFIG_FILE."
    err_more "Исправьте: chown root:$APP_GROUP \"$CONFIG_FILE\" && chmod 640 \"$CONFIG_FILE\""
  fi
  if [ "$bad" = "0" ] && ! check_data_dir; then
    bad=1
  fi
  return "$bad"
}

# ---------------------------------------------------------------------------
#  Проверки установленного пакета
# ---------------------------------------------------------------------------
# «import mailarchiver» проверяет один файл __init__.py. Служба же при запуске
# импортирует веб-приложение со всеми модулями — и ошибка в любом из них
# (нет библиотеки, конструкция не для этой версии Python) раньше всплывала
# только после запуска, строкой «Служба не запустилась».
check_package(){
  local pkg
  venv_py -c "import mailarchiver, fastapi, uvicorn, jinja2, yaml, apscheduler, imapclient, cryptography" \
    || { err "Приложение не импортируется: установлены не все библиотеки."; return 1; }
  pkg="$(venv_py -c 'import os, mailarchiver; print(os.path.dirname(mailarchiver.__file__))')" \
    || { err "Не удалось найти установленный пакет mailarchiver."; return 1; }
  venv_py -m compileall -q "$pkg" >/dev/null \
    || { err "Часть модулей не подходит для этой версии Python ($("$VENV_DIR/bin/python" -V 2>&1))."
         venv_py -m compileall -q "$pkg" || true
         return 1; }
}

# Пробный импорт веб-приложения — то же, с чего начинает сама служба, от её
# имени и с её конфигурацией. Базу данных это не открывает.
check_app_import(){
  if ! as_app_user env MAILARCHIVER_CONFIG="$CONFIG_FILE" "$VENV_DIR/bin/python" -I -c "import mailarchiver.web.app"; then
    err "Веб-приложение не импортируется (ошибка выше) — служба с ним не запустится."
    return 1
  fi
}

# Остатки прежней сборки пакета в каталоге кода. Без rsync (он удаляет лишнее
# сам) каталог build/ переживал обновление, и pip собирал новый пакет вместе с
# модулями прежней версии.
clean_build_leftovers(){
  rm -rf "$APP_DIR/build" "$APP_DIR"/*.egg-info 2>/dev/null || true
}

# ---------------------------------------------------------------------------
#  Служба systemd
# ---------------------------------------------------------------------------
# Unit из шаблона. Свои правки (MemoryMax и т.п.) держите в дополнении
# (systemctl edit mailarchiver): сам unit перезаписывается при обновлении.
render_unit(){
  local q='s/[\\&|]/\\&/g'      # путь может содержать & или | — для sed это служебные знаки
  sed -e "s|@VENV@|$(printf '%s' "$VENV_DIR" | sed "$q")|g" \
      -e "s|@APPDIR@|$(printf '%s' "$APP_DIR" | sed "$q")|g" \
      -e "s|@CONFIG@|$(printf '%s' "$CONFIG_FILE" | sed "$q")|g" \
      -e "s|@USER@|$(printf '%s' "$APP_USER" | sed "$q")|g" \
      -e "s|@GROUP@|$(printf '%s' "$APP_GROUP" | sed "$q")|g" \
      -e "s|@DATADIR@|$(printf '%s' "$DATA_DIR" | sed "$q")|g" \
      "$1" > "$UNIT_FILE"
  chmod 644 "$UNIT_FILE"
}

# ---------------------------------------------------------------------------
#  Ослабленная защита службы (MA_RELAX_SANDBOX=1)
# ---------------------------------------------------------------------------
# В непривилегированных контейнерах LXC/OpenVZ без вложенности (nesting) systemd
# не может создать для службы пространство имён монтирования, и она завершается
# со status=226/NAMESPACE. Ослаблять защиту молча нельзя — это решение
# администратора: только MA_RELAX_SANDBOX=1 создаёт дополнение к unit-файлу,
# которое отключает настройки, требующие пространств имён или правил доступа к
# устройствам. Сам unit не меняется: чтобы вернуть полную защиту, достаточно
# удалить один файл.
RELAX_CREATED=0        # дополнение создано этим запуском (при откате — убрать)
RELAX_DIR_CREATED=0    # и каталог для него тоже
relax_dropin(){ printf '%s\n' "$UNIT_FILE.d/50-relax-sandbox.conf"; }

# Содержимое дополнения. Список должен покрывать все настройки шаблона
# systemd/mailarchiver.service, которым нужно пространство имён (монтирования,
# UTS) или правила доступа к устройствам, — за этим следит тест
# test_relax_dropin_covers_unit_template.
relax_sandbox_conf(){
  cat <<EOF
# MailArchiver — ослабленная защита службы.
# Файл создан скриптом установки/обновления по запросу MA_RELAX_SANDBOX=1.
#
# Зачем: в контейнерах LXC/OpenVZ без вложенности (nesting) systemd не может
# создать для службы отдельное пространство имён, и она не запускается
# (status=226/NAMESPACE). Этот файл отключает те настройки защиты из
# $APP_NAME.service, которым пространства имён нужны.
#
# Что остаётся: служба по-прежнему работает от непривилегированного
# пользователя ($APP_USER), не может получить новые привилегии
# (NoNewPrivileges), её файлы создаются с UMask=0077.
#
# При обновлениях файл не перезаписывается. Вернуть полную защиту (например
# после включения вложенности у контейнера):
#   sudo rm "$1"
#   sudo systemctl daemon-reload
#   sudo systemctl restart $APP_NAME
[Service]
ProtectProc=default
PrivateDevices=no
ProtectKernelTunables=no
ProtectKernelModules=no
ProtectKernelLogs=no
ProtectControlGroups=no
ProtectClock=no
ProtectHostname=no
PrivateTmp=no
ProtectSystem=no
ProtectHome=no
ReadWritePaths=
RestrictNamespaces=no
EOF
}

# Дополнение действует: по его пути лежит непустой файл (или ссылка на такой).
# Ссылка на /dev/null и пустой файл — способ systemd ОТКЛЮЧИТЬ дополнение.
relax_dropin_active(){ [ -f "$1" ] && [ -s "$1" ]; }

# Вызывается перед daemon-reload. Если по пути дополнения уже что-то есть —
# файл, ссылка (в том числе на /dev/null), что угодно, — не трогаем: это решение
# администратора. Запись «сквозь» ссылку меняла бы чужой файл и права на нём, а
# откат потом удалял бы саму ссылку. Без MA_RELAX_SANDBOX=1 ничего не создаём,
# но о действующем ослаблении напоминаем — оно не должно забыться.
apply_relax_sandbox(){
  local file dir what=""
  file="$(relax_dropin)"; dir="${file%/*}"
  if [ -e "$file" ] || [ -L "$file" ]; then
    if [ -L "$file" ]; then what="ссылка на $(readlink -- "$file" 2>/dev/null || echo '?')"
    elif [ ! -f "$file" ]; then what="$(stat -c '%F' "$file" 2>/dev/null || echo 'не файл')"
    elif [ ! -s "$file" ]; then what="пустой файл"
    fi
    if relax_dropin_active "$file"; then
      if [ "${MA_RELAX_SANDBOX:-0}" = "1" ]; then
        info "Ослабленная защита службы уже включена: $file${what:+ ($what)} — файл оставлен как есть."
      else
        warn "Служба работает с ослабленной защитой: действует дополнение $file${what:+ ($what)}."
        warn_more "Вернуть полную защиту: удалите этот файл, затем systemctl daemon-reload && systemctl restart $APP_NAME."
      fi
    elif [ "${MA_RELAX_SANDBOX:-0}" = "1" ]; then
      warn "MA_RELAX_SANDBOX=1: по пути дополнения $file уже лежит $what — оставлено как есть, защита службы НЕ ослаблена."
      warn_more "Так дополнение отключают намеренно. Чтобы ослабить защиту, удалите его (rm \"$file\") и запустите скрипт заново."
    fi
    return 0
  fi
  case "${MA_RELAX_SANDBOX:-0}" in
    1) ;;
    ''|0) return 0 ;;
    *) warn "MA_RELAX_SANDBOX=${MA_RELAX_SANDBOX}: значение не понято, защита службы не меняется."
       warn_more "Чтобы ослабить её, задайте MA_RELAX_SANDBOX=1; чтобы оставить как есть — уберите переменную."
       return 0 ;;
  esac
  if [ ! -d "$dir" ]; then
    RELAX_DIR_CREATED=1      # до mkdir: сигнал между двумя командами не оставит пустой каталог
    mkdir -p "$dir"
    chmod 755 "$dir"
  fi
  RELAX_CREATED=1          # до записи: оборванный файл откат тоже уберёт
  relax_sandbox_conf "$file" > "$file"
  chmod 644 "$file"
  warn "MA_RELAX_SANDBOX=1: защита службы ослаблена дополнением $file"
  warn_more "(отключены настройки systemd, которым нужны пространства имён; служба по-прежнему работает от пользователя $APP_USER)."
}

# Откат: убрать то, что создал этот запуск, — дополнение и каталог для него
# (каждое по своей отметке: каталог мог быть создан, а файл ещё нет).
# 0 — убрано дополнение (нужен daemon-reload), 1 — убирать его не пришлось.
# Вызывать только в условии if.
remove_created_relax_sandbox(){
  local file dir removed=1
  file="$(relax_dropin)"; dir="${file%/*}"
  if [ "$RELAX_CREATED" = "1" ]; then
    rm -f "$file" 2>/dev/null || true
    RELAX_CREATED=0
    removed=0
    warn "Дополнение с ослабленной защитой, созданное этим запуском, убрано: $file"
  fi
  if [ "$RELAX_DIR_CREATED" = "1" ]; then
    rmdir "$dir" 2>/dev/null || true      # не пуст — значит, в нём уже чужие файлы
    RELAX_DIR_CREATED=0
  fi
  return "$removed"
}

# Совет про MA_RELAX_SANDBOX — продолжение сообщения об отказе 226/NAMESPACE.
relax_sandbox_advice(){
  local file
  file="$(relax_dropin)"
  if relax_dropin_active "$file"; then
    err_more "Ослабленная защита уже включена ($file), но служба всё равно завершается с этой ошибкой."
    err_more "Проверьте, что файл действует и в нём отключены все настройки Protect*/Private*: systemctl cat $APP_NAME"
    err_more "(если файл правили вручную — удалите его и повторите с MA_RELAX_SANDBOX=1). Иначе включите вложенность (nesting) у контейнера."
    return 0
  fi
  if [ -e "$file" ] || [ -L "$file" ]; then
    err_more "Дополнение $file есть, но отключено (пустой файл или ссылка на /dev/null): удалите его перед повтором."
  fi
  err_more "Повторите с MA_RELAX_SANDBOX=1:   sudo MA_RELAX_SANDBOX=1 bash scripts/${SCRIPT_NAME:-update.sh}"
  err_more "или включите вложенность у контейнера (Proxmox: pct set <ID> --features nesting=1) и повторите как обычно."
  err_more "MA_RELAX_SANDBOX=1 создаёт дополнение $file: оно отключает настройки защиты systemd,"
  err_more "которым нужны пространства имён (ProtectSystem, ProtectHome, PrivateTmp, PrivateDevices, ProtectProc, ProtectKernel* и др.)."
  err_more "Служба по-прежнему работает от непривилегированного пользователя $APP_USER."
}

# Что делать при status=226/NAMESPACE — по тому, что видно в журнале ($1) и на
# этом сервере. Строки — продолжение сообщения «Причина: …». Причину systemd
# пишет в конце строки «Failed to set up mount namespacing: …» или «Failed at
# step NAMESPACE spawning …: …»:
#   «No such file or directory» — нет каталога из ReadWritePaths (каталога
#       данных): чаще всего он ссылка на диск, который не подключён. Сам путь
#       называет только systemd 246 и новее; RHEL 8 и Debian 10 пишут строку без
#       пути — по ней нельзя советовать «это контейнер»;
#   «Permission denied» — контейнер без вложенности не даёт создать
#       пространство имён;
#   иное — причина неясна, называем обе.
# Совет про MA_RELAX_SANDBOX — только при «Permission denied» или если сервер
# действительно контейнер.
service_namespace_hint(){
  local lines="" reason="other" missing="" mp="" kind=""
  kind="$(container_type)"
  lines="$(printf '%s\n' "${1:-}" \
    | grep -E 'Failed to set up mount namespacing|Failed at step NAMESPACE' | tail -n 2 || true)"
  case "$lines" in
    *"No such file or directory"*) reason="missing" ;;
    *"Permission denied"*|*"Operation not permitted"*) reason="denied" ;;
  esac
  if [ "$reason" = "missing" ]; then
    missing="$(printf '%s\n' "$lines" \
      | sed -n 's/.*Failed to set up mount namespacing: \(.*\): No such file or directory.*/\1/p' | tail -n 1)"
    missing="${missing#/run/systemd/unit-root}"     # так путь выглядит изнутри собираемого окружения
    missing="${missing/#\/.\//\/}"                  # а так — «/./каталог» — снаружи
    missing="$(strip_slash "$missing")"
    # Названный каталог на месте (/proc, /dev…) — значит, не хватает не его:
    # окружение не собралось по другой причине.
    if [ -n "$missing" ] && [ -d "$missing" ]; then reason="other"; missing=""; fi
  fi

  if [ "$reason" = "missing" ]; then
    if [ -n "$missing" ]; then
      err_more "systemd не нашёл каталог $missing — он нужен службе (ReadWritePaths), и без него она не запускается."
    else
      err_more "systemd не нашёл один из каталогов, нужных службе (ReadWritePaths), — без него она не запускается."
    fi
    if check_data_dir; then
      if [ -n "$missing" ]; then mp="$(unmounted_fstab_mount "$missing")"; fi
      if [ -n "$mp" ]; then
        err_more "Файловая система $mp из /etc/fstab не подключена: mount \"$mp\""
      fi
      err_more "Каталог данных $DATA_DIR на месте: сравните каталоги из ReadWritePaths (systemctl cat $APP_NAME) с тем, что есть на диске."
      if [ -n "$kind" ]; then
        err_more "Сервер — контейнер ($kind). Если все каталоги на месте, дело в нём:"
        relax_sandbox_advice
      fi
    fi
  elif [ "$reason" = "denied" ]; then
    err_more "Так бывает в контейнерах LXC/OpenVZ без вложенности (nesting): systemd там не может создать пространство имён для службы."
    relax_sandbox_advice
  elif check_data_dir; then
    err_more "Причин у такого отказа обычно две:"
    err_more "  — нет каталога из ReadWritePaths (например не подключён диск с данными): сравните systemctl cat $APP_NAME с тем, что есть на диске;"
    err_more "  — сервер — контейнер LXC/OpenVZ без вложенности (nesting), и systemd не может создать пространство имён для службы."
    if [ -n "$kind" ]; then
      err_more "Этот сервер — контейнер ($kind):"
      relax_sandbox_advice
    fi
  fi
  err_more "См. docs/ru/11-troubleshooting.md, строка «status=226/NAMESPACE»."
}

# Значение свойства службы (пусто, если эта версия systemd такого не знает).
svc_prop(){ { systemctl show "$APP_NAME" -p "$1" 2>/dev/null || true; } | sed -n "s/^$1=//p" | head -n 1; }

# Значение из конфигурации так, как его прочитает служба: db_path | data_dir |
# health_url. Пусто, если установленная версия не ответила (или она старая и
# такого значения не знает), — вызывающий обходится без него.
# Относительные пути конфигурации (data_dir: "data") служба считает от своего
# рабочего каталога — от него же считаем и здесь, иначе база «не находилась».
cfg_value(){
  venv_py - "$CONFIG_FILE" "$1" "${APP_DIR:-}" <<'PY' 2>/dev/null || true
import os, sys
try:
    os.chdir(sys.argv[3])
except OSError:
    pass
from mailarchiver.config import load_config
cfg = load_config(sys.argv[1], create_dirs=False)
if sys.argv[2] == "db_path":
    print(cfg.db_path)
elif sys.argv[2] == "data_dir":
    print(cfg.data_dir)
else:
    host = str(cfg.server.get("host") or "127.0.0.1").strip()
    port = int(cfg.server.get("port") or 8493)
    # служба слушает «все адреса» — стучимся на локальный
    if host in ("0.0.0.0", "*"):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    print("http://%s:%d/health" % (host, port))
PY
}

# 0 — адрес отвечает кодом 200; иначе в stdout — причина одной строкой.
# Прокси из окружения не используется: на серверах, где для pip задан
# http_proxy, запрос к 127.0.0.1 уходил на прокси, и рабочая служба считалась
# неотвечающей.
http_probe(){
  venv_py - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(sys.argv[1], timeout=5) as resp:
        if resp.status == 200:
            sys.exit(0)
        print("ответ HTTP %s" % resp.status)
except Exception as exc:  # noqa: BLE001
    print(getattr(exc, "reason", None) or exc)
sys.exit(1)
PY
}
http_ok(){ http_probe "$1" >/dev/null; }

# Адрес индекса пакетов, к которому обратится pip: MA_INDEX_URL (он же
# PIP_INDEX_URL), иначе настройка pip, иначе PyPI.
pip_index_url(){
  local url="${PIP_INDEX_URL:-}" conf=""
  if [ -z "$url" ]; then
    # «pip config list», а не «get»: get не видит файл, заданный в PIP_CONFIG_FILE
    conf="$( ( trap - ERR; cd / && PYTHONDONTWRITEBYTECODE=1 "$VENV_DIR/bin/pip" --disable-pip-version-check config list ) 2>/dev/null || true)"
    url="$(printf '%s\n' "$conf" | sed -n "s/^install\.index-url=//p" | tail -n 1)"
    [ -n "$url" ] || url="$(printf '%s\n' "$conf" | sed -n "s/^global\.index-url=//p" | tail -n 1)"
    url="${url#[\"\']}"; url="${url%[\"\']}"
  fi
  printf '%s\n' "${url:-https://pypi.org/simple/}"
}

# 0 — индекс пакетов отвечает (любым ответом HTTP: и 403, и 404 значат, что
# связь есть); иначе в stdout — причина одной строкой. В отличие от http_probe
# прокси из окружения используется — через него же пойдёт pip. Сертификат не
# проверяется: вопрос только «есть ли связь», ничего не загружается, а наборы
# доверенных сертификатов у Python и у pip разные. Ждёт не дольше 8 секунд
# (вместе с поиском имени в DNS, на который таймаут сокета не действует).
index_probe(){
  venv_py - "$1" <<'PY' 2>/dev/null
import os, ssl, sys, threading, urllib.error, urllib.request
from urllib.parse import urlsplit, urlunsplit

try:
    parts = urlsplit(sys.argv[1])
    if parts.scheme not in ("http", "https"):
        os._exit(0)                     # каталог или file:// — сеть не нужна
    host = parts.hostname or ""
    netloc = ("[%s]" % host if ":" in host else host) + (":%d" % parts.port if parts.port else "")
    url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))   # без имени и пароля
except ValueError as exc:
    print("адрес не разобран: %s" % exc)
    sys.exit(1)
result = []


def ask():
    try:
        https = urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.build_opener(https).open(request, timeout=5):
            pass
        result.append("")
    except urllib.error.HTTPError:
        result.append("")
    except Exception as exc:  # noqa: BLE001
        result.append(str(getattr(exc, "reason", None) or exc) or type(exc).__name__)


threading.Thread(target=ask, daemon=True).start()
for _ in range(80):
    if result:
        break
    threading.Event().wait(0.1)
reason = result[0] if result else "нет ответа за 8 с"
if reason:
    print(reason)
sys.stdout.flush()
os._exit(1 if reason else 0)
PY
}

# Дождаться, пока служба поднимется и ответит на /health.
#   0 — работает;
#   1 — упала (systemd её перезапускает или сдался);
#   2 — процесс жив, но веб-интерфейс за отведённое время не ответил.
# Не «sleep 3»: первый запуск новой версии обновляет базу и на большом архиве
# длится дольше; а упавшая служба при Restart=on-failure между попытками
# числится «activating», и проверка is-active ничего не говорила о причине.
# Срок ожидания — MA_START_TIMEOUT секунд (по умолчанию 180); адрес проверки
# берётся из конфигурации, свой можно задать в MA_HEALTH_URL.
#
# Срок вышел, а служба занята — её процесс тратит процессорное время, читает
# или пишет (по /proc), — ждём дальше, пока она работает, но не дольше
# MA_START_TIMEOUT_MAX секунд (3600) всего. Так первый запуск, который строит
# индексы большой базы (обновление через несколько версий), не принимается за
# зависший: откат выбросил бы сделанное, и следующая попытка начала бы заново.
# Процесс, который за SVC_IDLE_WINDOW секунд (10) ничего не сделал, — завис.
# Пока ждём, раз в MA_PROGRESS_EVERY секунд (30) пишем, сколько прошло.
HEALTH_URL=""
SVC_IDLE_WINDOW=10
WAIT_BUSY_CAP=""      # не пусто — сдались по MA_START_TIMEOUT_MAX, хотя служба была занята
WAIT_IDLE=""          # 1 — сдались, потому что служба ничего не делала (по /proc)
WAIT_EXTENDED=""      # 1 — срок продлевался: служба была занята
wait_service_up(){   # wait_service_up [СРОК [ПРЕДЕЛ]] — ПРЕДЕЛ = СРОК: не продлевать (откат)
  local timeout="${1:-${MA_START_TIMEOUT:-180}}" cap="${2:-${MA_START_TIMEOUT_MAX:-3600}}" every="${MA_PROGRESS_EVERY:-30}"
  local began=$SECONDS active sub restarts base stable=0 told=0 elapsed
  local window="$SVC_IDLE_WINDOW" check_at baseline="" act extended=0 report_at
  WAIT_BUSY_CAP=""; WAIT_IDLE=""; WAIT_EXTENDED=""
  case "$timeout" in ''|*[!0-9]*) timeout=180 ;; esac
  case "$cap" in ''|*[!0-9]*) cap=3600 ;; esac
  case "$every" in ''|*[!0-9]*|0) every=30 ;; esac
  if [ "$cap" -lt "$timeout" ]; then cap="$timeout"; fi
  # первый замер — за окно до срока: к сроку будет с чем сравнить
  check_at=$((timeout > window ? timeout - window : 0))
  report_at="$every"
  HEALTH_URL="${MA_HEALTH_URL:-}"
  [ -n "$HEALTH_URL" ] || HEALTH_URL="$(cfg_value health_url)"
  base="$(svc_prop NRestarts)"
  while :; do
    active="$(svc_prop ActiveState)"; sub="$(svc_prop SubState)"; restarts="$(svc_prop NRestarts)"
    case "$active/$sub" in
      failed/*|inactive/*|*/auto-restart*) return 1 ;;
    esac
    if [ -n "$base" ] && [ -n "$restarts" ] && [ "$restarts" -gt "$base" ] 2>/dev/null; then
      return 1
    fi
    if [ "$active" = "active" ]; then
      if [ -z "$HEALTH_URL" ]; then
        # адрес узнать не удалось — считаем запущенной, если прожила заметно дольше RestartSec
        stable=$((stable + 1))
        if [ "$stable" -ge 10 ]; then return 0; fi
      elif http_ok "$HEALTH_URL"; then
        return 0
      fi
    fi
    elapsed=$((SECONDS - began))
    if [ "$elapsed" -ge "$check_at" ]; then
      act="$(_svc_activity "$(svc_prop MainPID)")"
      if [ "$elapsed" -ge "$timeout" ]; then
        # срок вышел: ждём дальше, только если служба с прошлого замера что-то делала
        if [ -z "$act" ]; then
          # процесса нет: служба могла упасть между проверкой состояния и замером
          case "$(svc_prop ActiveState)/$(svc_prop SubState)" in
            failed/*|inactive/*|*/auto-restart*) return 1 ;;
          esac
          return 2
        fi
        if [ "$act" = "$baseline" ]; then WAIT_IDLE=1; return 2; fi
        if [ "$elapsed" -ge "$cap" ]; then WAIT_BUSY_CAP="$elapsed"; return 2; fi
        if [ "$extended" = "0" ]; then
          extended=1; WAIT_EXTENDED=1
          info "Срок ожидания ($timeout с) вышел, но служба занята — первый запуск новой версии, видимо, ещё"
          info "    обновляет базу. Жду, пока она работает (всего не дольше $cap с; Ctrl-C — откат)."
          report_at="$elapsed"
        fi
      fi
      baseline="$act"
      check_at=$((elapsed + window))
    fi
    if [ "$told" = "0" ] && [ "$elapsed" -ge 8 ]; then
      info "Служба запущена, жду ответа веб-интерфейса (до $timeout с; первый запуск новой версии обновляет базу)…"
      told=1
    elif [ "$told" = "1" ] && [ "$elapsed" -ge "$report_at" ]; then
      info "  …жду ответа веб-интерфейса: прошло $(_elapsed "$elapsed")$(_svc_activity_note "$(svc_prop MainPID)")"
    fi
    if [ "$elapsed" -ge "$report_at" ]; then report_at=$((elapsed + every)); fi
    sleep 1
  done
}

# Сколько главный процесс службы успел сделать: такты процессора и байты,
# прочитанные и записанные (/proc/ПРОЦЕСС/stat и io; root их видит и у службы
# в своём пространстве имён). Пусто — узнать нельзя.
_svc_activity(){   # _svc_activity PID
  local pid="$1" stat io
  case "$pid" in ''|0|*[!0-9]*) return 0 ;; esac
  stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 0
  stat="${stat##*) }"                        # имя процесса в скобках может содержать пробелы
  io="$(cat "/proc/$pid/io" 2>/dev/null || true)"
  awk -v st="$stat" 'BEGIN { split(st, f, " "); cpu = f[12] + f[13] }
       /^rchar:/ { r = $2 } /^wchar:/ { w = $2 }
       END { printf "%d %d %d\n", cpu, r, w }' <<< "$io"
}

# «; служба прочитала 2,1 ГБ, записала 310,4 МБ» — для строки о ходе ожидания.
_svc_activity_note(){   # _svc_activity_note PID
  local act cpu r w
  act="$(_svc_activity "$1")"
  [ -n "$act" ] || return 0
  read -r cpu r w <<< "$act"
  printf '; служба прочитала %s, записала %s' "$(_human_bytes "$r")" "$(_human_bytes "$w")"
}

# Последние N строк журнала службы с отступом (пусто, если журнала нет).
journal_tail(){
  command -v journalctl >/dev/null 2>&1 || return 0
  { journalctl -q -u "$APP_NAME" -n "${1:-8}" --no-pager 2>/dev/null || true; } | sed 's/^/      /'
}

# Причина отказа — прямо в выводе скрипта: последние строки журнала службы и,
# если причина узнаётся, что с ней делать.
show_service_failure(){
  local out=""
  err "Состояние службы: $(svc_prop ActiveState)/$(svc_prop SubState)."
  if command -v journalctl >/dev/null 2>&1; then
    if [ -n "${SVC_STARTED_AT:-}" ]; then
      out="$(journalctl -q -u "$APP_NAME" --since "$SVC_STARTED_AT" -n 40 --no-pager 2>/dev/null || true)"
    fi
    [ -n "$out" ] || out="$(journalctl -q -u "$APP_NAME" -n 40 --no-pager 2>/dev/null || true)"
  fi
  if [ -n "$out" ]; then
    {
      echo "----- журнал службы (journalctl -u $APP_NAME), последние строки -----"
      printf '%s\n' "$out"
      echo "----------------------------------------------------------------------"
    } >&2 || true
  elif command -v journalctl >/dev/null 2>&1; then
    err "В журнале службы записей нет (journalctl -u $APP_NAME -n 50 --no-pager)."
  else
    err "journalctl не найден — причину ищите в журнале системы и в $DATA_DIR/logs."
  fi
  case "$out" in
    *"200/CHDIR"*|*"requested working directory failed"*)
      err "Причина: служба (пользователь $APP_USER) не может войти в рабочий каталог $APP_DIR."
      err "Проверьте права на него и на каталоги выше: namei -l \"$APP_DIR\"" ;;
    *"203/EXEC"*)
      err "Причина: служба не может запустить $VENV_DIR/bin/mailarchiver (файла нет или нет прав)." ;;
    *"217/USER"*)
      err "Причина: в системе нет пользователя или группы службы ($APP_USER:$APP_GROUP)." ;;
    *"226/NAMESPACE"*)
      err "Причина: systemd не смог собрать защищённое окружение службы (status=226/NAMESPACE)."
      service_namespace_hint "$out" ;;
    *"ddress already in use"*)
      err "Причина: порт веб-интерфейса занят другой программой (server.port в $CONFIG_FILE)." ;;
    *"ModuleNotFoundError"*|*"ImportError"*)
      err "Причина: в окружении не хватает библиотеки Python (см. строку с ModuleNotFoundError/ImportError выше)." ;;
    *"Application startup failed"*)
      err "Причина: приложение не смогло подготовиться к работе — ошибка в строках журнала выше." ;;
  esac
}

# Запустить службу заново и дождаться ответа веб-интерфейса.
# Возвращает код wait_service_up; при отказе сам показывает причину.
start_service_checked(){
  local rc=0 url reason
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl reset-failed "$APP_NAME" 2>/dev/null || true
  # Служба остановлена, а адрес отвечает — на порту кто-то ещё (например
  # mailarchiver, запущенный вручную). Иначе его ответ приняли бы за ответ
  # службы, которая на самом деле не может занять порт.
  url="${MA_HEALTH_URL:-}"
  [ -n "$url" ] || url="$(cfg_value health_url)"
  if [ -n "$url" ] && http_ok "$url"; then
    err "Служба остановлена, но $url отвечает: порт занят другим процессом"
    err "(например mailarchiver запущен вручную). Найдите его: ss -ltnp — и остановите."
    return 1
  fi
  SVC_STARTED_AT="$(date '+%Y-%m-%d %H:%M:%S')"
  if ! systemctl start "$APP_NAME"; then
    err "systemd не принял команду запуска службы."
    show_service_failure
    return 1
  fi
  wait_service_up "${1:-}" || rc=$?
  if [ "$rc" = "1" ]; then
    err "Служба не запустилась."
    show_service_failure
  elif [ "$rc" = "2" ]; then
    reason="$(http_probe "$HEALTH_URL" || true)"
    if [ -n "$WAIT_BUSY_CAP" ]; then
      err "Служба занята уже $(_elapsed "$WAIT_BUSY_CAP") — дольше предела MA_START_TIMEOUT_MAX, — а веб-интерфейс так"
      err "и не ответил: $HEALTH_URL${reason:+ ($reason)}."
      err "Если база очень большая — повторите с MA_START_TIMEOUT_MAX=14400 (сколько секунд ждать всего);"
      err "чем занята служба, покажет её журнал (ниже)."
    else
      err "Служба запущена, но веб-интерфейс так и не ответил: $HEALTH_URL${reason:+ ($reason)}."
      if [ "$WAIT_IDLE" = "1" ]; then
        err "Служба при этом уже ничего не делала (не тратила процессорное время, не читала и не писала) — похоже, зависла."
      fi
      if [ "$WAIT_EXTENDED" != "1" ]; then
        err "Если база большая и первый запуск новой версии длится дольше — повторите с MA_START_TIMEOUT=900;"
        err "если служба слушает другой адрес — задайте его в MA_HEALTH_URL."
      else
        err "Если служба слушает другой адрес — задайте его в MA_HEALTH_URL."
      fi
    fi
    show_service_failure
  fi
  return "$rc"
}
