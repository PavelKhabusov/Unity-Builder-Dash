#!/bin/bash
# Install Unity Builder Dash as a user desktop entry.
#
#   ./install.sh            → install to ~/.local/share/applications/
#   ./install.sh --system   → install to /usr/share/applications/ (needs sudo)
#   ./install.sh --uninstall
#
# Detects the repo's absolute path at install time, substitutes it into
# unity-builder-dash.desktop.in, and writes the resulting .desktop file.
set -e

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="unity-builder-dash.desktop"
TPL="$REPO/unity-builder-dash.desktop.in"

if [ ! -f "$TPL" ]; then
    echo "Missing template: $TPL" >&2
    exit 1
fi

SYSTEM=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --system)    SYSTEM=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            sed -n '2,6p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
    esac
done

if [ "$SYSTEM" = "1" ]; then
    DEST_DIR="/usr/share/applications"
    SUDO="sudo"
else
    DEST_DIR="$HOME/.local/share/applications"
    SUDO=""
fi
DEST="$DEST_DIR/$NAME"

if [ "$UNINSTALL" = "1" ]; then
    [ -f "$DEST" ] && $SUDO rm -f "$DEST" && echo "Removed $DEST" \
                   || echo "Not installed at $DEST"
    exit 0
fi

$SUDO mkdir -p "$DEST_DIR"
$SUDO sh -c "sed 's|@REPO@|$REPO|g' '$TPL' > '$DEST'"
$SUDO chmod 644 "$DEST"

# Refresh desktop cache (best-effort)
if command -v update-desktop-database >/dev/null 2>&1; then
    $SUDO update-desktop-database "$DEST_DIR" 2>/dev/null || true
fi

echo "Installed: $DEST"
echo "  Repo:  $REPO"
echo "  Exec:  python3 $REPO/build.py"
