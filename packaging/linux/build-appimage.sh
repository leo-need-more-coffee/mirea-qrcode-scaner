#!/usr/bin/env sh
# Собирает AppImage из готовой сборки PyInstaller в dist/Shalost-FOTUR
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
DIST="$ROOT/dist/Shalost-FOTUR"
APPDIR="$ROOT/build/Shalost-FOTUR.AppDir"
OUTPUT="${1:-$ROOT/releases/Shalost-FOTUR-x86_64.AppImage}"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"

if [ ! -x "$DIST/Shalost-FOTUR" ]; then
    echo "Сначала соберите приложение: pyinstaller ... main.py" >&2
    exit 1
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$DIST/." "$APPDIR/usr/bin/"
cp "$ROOT/assets/shalost-fotur.png" "$APPDIR/shalost-fotur.png"
cp "$ROOT/assets/shalost-fotur.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/shalost-fotur.png"
sed "s|APP_EXEC|Shalost-FOTUR|" "$ROOT/packaging/linux/shalost-fotur.desktop" > "$APPDIR/shalost-fotur.desktop"
cp "$APPDIR/shalost-fotur.desktop" "$APPDIR/usr/share/applications/shalost-fotur.desktop"

cat > "$APPDIR/AppRun" <<'RUN'
#!/usr/bin/env sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/bin/Shalost-FOTUR" "$@"
RUN
chmod +x "$APPDIR/AppRun"

mkdir -p "$(dirname "$OUTPUT")"
# APPIMAGETOOL может содержать аргументы, например --appimage-extract-and-run
# shellcheck disable=SC2086
ARCH="${ARCH:-x86_64}" $APPIMAGETOOL "$APPDIR" "$OUTPUT"
echo "Готово: $OUTPUT"
