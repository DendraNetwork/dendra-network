#!/usr/bin/env bash
# install_app.sh -- put the Dendra application in this user's applications menu (Linux).
#
# WHAT IT WRITES, AND NOTHING ELSE (all under $HOME, no root needed):
#   ~/.local/bin/dendra                          a two-line launcher for deploy/app/dendra_app.py
#   ~/.local/share/applications/dendra.desktop   the menu entry
#   ~/.local/share/icons/hicolor/scalable/apps/dendra.svg
# It installs no system package. A native window needs python3-gi and WebKit2GTK; without them the
# application opens in the default browser instead, and this script says which packages give the window.
#
# Usage:  bash deploy/app/install_app.sh            # install or update the entry
#         bash deploy/app/install_app.sh --remove   # remove the three files
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/dendra"
DESK="$HOME/.local/share/applications/dendra.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/dendra.svg"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$BIN" "$DESK" "$ICON"
  echo "[app] removed the launcher, the menu entry and the icon."
  exit 0
fi
command -v python3 >/dev/null 2>&1 || { echo "[app] REFUSED: python3 is required."; exit 2; }
[ -f "$HERE/dendra_app.py" ] || { echo "[app] REFUSED: $HERE/dendra_app.py is missing."; exit 2; }

mkdir -p "$(dirname "$BIN")" "$(dirname "$DESK")" "$(dirname "$ICON")"
# The launcher names the application by its ABSOLUTE path in this clone: moving the clone means
# running this script again, which it says rather than pointing at a file that is gone.
cat > "$BIN" <<EOF
#!/bin/sh
exec python3 "$HERE/dendra_app.py" "\$@"
EOF
chmod 755 "$BIN"
cp "$HERE/dendra.svg" "$ICON"
cat > "$DESK" <<EOF
[Desktop Entry]
Type=Application
Name=Dendra
Comment=Run and watch your Dendra miner
Exec=$BIN
Icon=dendra
Terminal=false
Categories=Network;Utility;
EOF
chmod 644 "$DESK"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$(dirname "$DESK")" >/dev/null 2>&1
echo "[app] Dendra is in your applications menu (launcher: $BIN)."
# The same test as `open_window` in dendra_app.py: GTK 3 alone is on most desktops, and without WebKit2
# the application silently opens in the browser instead.
if ! python3 - >/dev/null 2>&1 <<'PY'
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
PY
then
  echo "[app] It will open in your browser. For its own window: sudo apt install python3-gi gir1.2-webkit2-4.1"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "[app] Note: $HOME/.local/bin is not in PATH; the menu entry works anyway." ;; esac
