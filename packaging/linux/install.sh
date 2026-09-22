#!/usr/bin/env sh
# Устанавливает portable-сборку Shalost FOTUR для текущего пользователя
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/Shalost-FOTUR"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
BIN_DIR="$HOME/.local/bin"

if [ ! -x "$SOURCE_DIR/Shalost-FOTUR" ]; then
    echo "Запустите install.sh из распакованной папки Shalost-FOTUR" >&2
    exit 1
fi

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR" "$DESKTOP_DIR" "$ICON_DIR" "$BIN_DIR"
cp -a "$SOURCE_DIR/." "$TARGET_DIR/"

cp -f "$TARGET_DIR/_internal/assets/shalost-fotur.png" "$ICON_DIR/shalost-fotur.png" 2>/dev/null || true
sed "s|APP_EXEC|$TARGET_DIR/Shalost-FOTUR|" "$TARGET_DIR/shalost-fotur.desktop" > "$DESKTOP_DIR/shalost-fotur.desktop"
ln -sf "$TARGET_DIR/Shalost-FOTUR" "$BIN_DIR/shalost-fotur"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" || true

echo "Shalost FOTUR установлен в $TARGET_DIR"
echo "Запуск: ярлык в меню приложений или команда shalost-fotur"
