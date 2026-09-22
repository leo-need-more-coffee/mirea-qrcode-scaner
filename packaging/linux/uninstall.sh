#!/usr/bin/env sh
# Удаляет установленную для пользователя сборку Shalost FOTUR
set -eu

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

rm -rf "$DATA_HOME/Shalost-FOTUR"
rm -f "$DATA_HOME/applications/shalost-fotur.desktop"
rm -f "$DATA_HOME/icons/hicolor/256x256/apps/shalost-fotur.png"
rm -f "$HOME/.local/bin/shalost-fotur"

echo "Приложение удалено. Профиль Chrome остался в $DATA_HOME/Shalost"
echo "Удалить сессию Pulse полностью: rm -rf \"$DATA_HOME/Shalost\""
