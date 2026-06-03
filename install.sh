#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
DESKTOP_DIR="$HOME/.local/share/applications"

echo "Installing IRIS launcher to $DESKTOP_DIR..."

mkdir -p "$DESKTOP_DIR"

# Create a customized .desktop file with the absolute path
cat << DESKTOP > "$DESKTOP_DIR/iris.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=IRIS Music
Comment=IRIS terminal music client powered by the Verome API
Exec=bash -c "cd '$DIR' && ./launcher.sh"
Icon=utilities-terminal
Terminal=true
Categories=AudioVideo;Audio;Music;ConsoleOnly;
Keywords=Music;Terminal;TUI;Player;
DESKTOP

chmod +x "$DESKTOP_DIR/iris.desktop"

if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$DESKTOP_DIR"
fi

echo "Done! You should now see 'IRIS Music' in your application launcher."
