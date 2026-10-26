#!/bin/bash
# Руководство Word (docs/MailArchiver_Руководство.docx) — из docs/ru и записи CHANGELOG.md о текущей версии.
#
#     bash scripts/manual/build.sh                 # от руководства последнего коммита, где оно менялось
#     bash scripts/manual/build.sh --base v1.14.0  # от руководства этой метки (коммита)
#     bash scripts/manual/build.sh --base-docx old.docx --base-root DIR   # без git: прежнее руководство и
#                                                  # каталог с docs/ru и CHANGELOG.md, из которых оно собрано
#     … --no-install                               # не записывать результат в docs/ (только build/manual/out.docx)
#
# Руководство не собирается заново целиком, а правится на месте (update.py): прежнее оформление и всё, что не
# менялось в Markdown, остаётся побайтно. Потом: PDF (LibreOffice) → оглавление по страницам PDF → повтор, пока
# номера страниц не перестанут меняться → проверки (verify.py, layout_check.py, whatsnew_pages.py). Любая
# ошибка — ненулевой код, и docs/ не трогается. Подробно — scripts/manual/README.md.
#
# Нужно: python3 с pypdf и Pillow, LibreOffice (soffice), poppler-utils (pdftotext, pdfinfo), unzip, zip, git.
# SOFFICE_CMD — как запускать LibreOffice, если не просто «soffice» (например, обёртка для песочницы).
# DOCX_VALIDATE_CMD — дополнительная проверка схемы: команда, которой передаются out.docx и --original orig.docx.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
DOCX_REL="docs/MailArchiver_Руководство.docx"
WORK=${MANUAL_WORK:-$ROOT/build/manual}
BASE_REV=""
BASE_DOCX=""
BASE_ROOT=""
INSTALL=1
while [ $# -gt 0 ]; do
    case "$1" in
        --base) BASE_REV=$2; shift 2 ;;
        --base-docx) BASE_DOCX=$2; shift 2 ;;
        --base-root) BASE_ROOT=$2; shift 2 ;;
        --no-install) INSTALL=0; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
    esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "нужна программа $1 ($2)" >&2; exit 1; }; }
need unzip unzip
need zip zip
need pdftotext poppler-utils
need pdfinfo poppler-utils
python3 -c "import pypdf, PIL" 2>/dev/null || { echo "нужны модули Python pypdf и Pillow: pip install pypdf Pillow" >&2; exit 1; }
if [ -z "${SOFFICE_CMD:-}" ]; then
    if command -v soffice >/dev/null 2>&1; then SOFFICE_CMD=soffice
    elif command -v libreoffice >/dev/null 2>&1; then SOFFICE_CMD=libreoffice
    elif [ -x /Applications/LibreOffice.app/Contents/MacOS/soffice ]; then
        SOFFICE_CMD=/Applications/LibreOffice.app/Contents/MacOS/soffice
    else echo "нужен LibreOffice (soffice) — или укажите SOFFICE_CMD" >&2; exit 1
    fi
fi

mkdir -p "$WORK"
WORK=$(cd "$WORK" && pwd)
rm -rf "$WORK/unpacked" "$WORK/orig_unpacked" "$WORK/base" "$WORK/pdf" "$WORK/manifest.json"
mkdir -p "$WORK/pdf"
if [ -n "$BASE_DOCX" ]; then
    [ -n "$BASE_ROOT" ] || { echo "с --base-docx нужен и --base-root" >&2; exit 2; }
    cp "$BASE_DOCX" "$WORK/orig.docx"
    OLD_ROOT=$(cd "$BASE_ROOT" && pwd)
else
    if [ -z "$BASE_REV" ]; then
        BASE_REV=$(git -C "$ROOT" log -1 --format=%H -- "$DOCX_REL")
        [ -n "$BASE_REV" ] || { echo "в истории git нет $DOCX_REL — укажите --base-docx и --base-root" >&2; exit 1; }
    fi
    echo "прежнее руководство: $DOCX_REL из $(git -C "$ROOT" log -1 --format='%h «%s»' "$BASE_REV")"
    git -C "$ROOT" show "$BASE_REV:$DOCX_REL" > "$WORK/orig.docx"
    mkdir -p "$WORK/base"
    git -C "$ROOT" archive "$BASE_REV" docs/ru CHANGELOG.md | tar -x -C "$WORK/base"
    OLD_ROOT="$WORK/base"
fi

cd "$WORK"
mkdir unpacked orig_unpacked
unzip -q orig.docx -d unpacked
unzip -q orig.docx -d orig_unpacked
find unpacked orig_unpacked -type l -delete            # ссылок в пакете .docx не бывает
python3 "$HERE/update.py" unpacked "$ROOT" "$OLD_ROOT" update.log

pack() { (cd unpacked && rm -f ../out.docx && zip -Xqr ../out.docx .); }
pdf() {
    rm -f pdf/out.pdf
    $SOFFICE_CMD "-env:UserInstallation=file://$WORK/lo_profile" --headless --convert-to pdf --outdir "$WORK/pdf" \
        "$WORK/out.docx" >/dev/null 2>&1 || true
    [ -s pdf/out.pdf ] || { echo "LibreOffice не сделал PDF (SOFFICE_CMD=$SOFFICE_CMD)" >&2; exit 1; }
}
rm -f toc_prev.json toc_new.json
for pass in 1 2 3 4 5; do
    pack; pdf
    (cd "$HERE" && python3 toc.py "$WORK/pdf/out.pdf" "$WORK/unpacked/word/document.xml" "$WORK/toc_new.json")
    if [ -f toc_prev.json ] && cmp -s toc_prev.json toc_new.json; then
        echo "оглавление устойчиво (проход $pass)"
        break
    fi
    (cd "$HERE" && python3 toc_apply.py "$WORK/toc_new.json" "$WORK/unpacked/word/document.xml")
    cp toc_new.json toc_prev.json
    [ "$pass" = 5 ] && { echo "оглавление не сошлось за 5 проходов" >&2; exit 1; }
done
# последний PDF построен из того же document.xml, что лежит в out.docx: оглавление в нём уже верное

python3 - "$WORK/out.docx" <<'PY'
import sys, zipfile, xml.dom.minidom
with zipfile.ZipFile(sys.argv[1]) as z:
    for n in z.namelist():
        if n.endswith((".xml", ".rels")):
            xml.dom.minidom.parseString(z.read(n))
print("XML всех частей пакета разбирается")
PY
if [ -n "${DOCX_VALIDATE_CMD:-}" ]; then $DOCX_VALIDATE_CMD "$WORK/out.docx" --original "$WORK/orig.docx"; fi
python3 "$HERE/verify.py" orig_unpacked unpacked "$ROOT" "$OLD_ROOT"
python3 "$HERE/layout_check.py" pdf/out.pdf unpacked/word/document.xml
python3 "$HERE/check_pages.py" pdf/out.pdf 2>/dev/null | tail -3
python3 "$HERE/whatsnew_pages.py" pdf/out.pdf "$(cat "$ROOT/VERSION")"
mkdir -p pdf_orig
cp orig.docx pdf_orig/orig.docx
$SOFFICE_CMD "-env:UserInstallation=file://$WORK/lo_profile" --headless --convert-to pdf --outdir "$WORK/pdf_orig" \
    "$WORK/pdf_orig/orig.docx" >/dev/null 2>&1 || true
echo "страниц было: $(pdfinfo pdf_orig/orig.pdf 2>/dev/null | awk '/^Pages/{print $2}'), стало: $(pdfinfo pdf/out.pdf | awk '/^Pages/{print $2}')"

if [ "$INSTALL" = 1 ]; then
    cp out.docx "$ROOT/$DOCX_REL"
    echo "записано: $DOCX_REL (PDF для просмотра: $WORK/pdf/out.pdf)"
else
    echo "готово: $WORK/out.docx (в docs/ не записано)"
fi
