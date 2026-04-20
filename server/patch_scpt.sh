#!/bin/bash
# Compile server/IOSbuild.applescript into IOSbuild.scpt on the Mac,
# substituting {{PLACEHOLDERS}} with values from env vars.
#
# Called by install_mac_server (over SSH) after the source + helpers are
# scp'd to WORK_DIR. Also safe to run manually on the Mac for debugging.
#
# Usage:  bash patch_scpt.sh
#
# Env vars (all read with fallbacks; install_mac_server fills them from cfg):
#   WORK_DIR            Mac work folder (default: ~/Desktop)
#   WIDGET_BUNDLE_ID    Widget CFBundleIdentifier
#   WIDGET_TEAM_ID      Apple Dev Team ID
#   WIDGET_TARGET_NAME  Widget Xcode target (e.g. URLImageWidget)
#   WIDGET_FOLDER_NAME  Widget source folder name (e.g. kartoteka.widget)
#   APP_GROUP_ID        App Group ID
#   SMB_USER            SMB user for Windows-host fallback (optional)
#   SMB_PASS            SMB password (optional)
#   SMB_BUILD_PATH      Path under /Volumes/Users/ (optional)
set -e

WORK_DIR="${WORK_DIR:-$HOME/Desktop}"
WORK_DIR="${WORK_DIR%/}"
SRC="$WORK_DIR/IOSbuild.applescript"
DEST="$WORK_DIR/IOSbuild.scpt"

[ -f "$SRC" ] || { echo "Source not found: $SRC" >&2; exit 1; }

# sed escapes: | is our delimiter and \ / & are special to replacement.
esc() {
    printf '%s' "$1" | sed -e 's/[\\/&|]/\\&/g'
}

TMP="$(mktemp -t IOSbuild).applescript"
sed \
  -e "s|{{WORK_DIR}}|$(esc "$WORK_DIR")|g" \
  -e "s|{{WIDGET_BUNDLE_ID}}|$(esc "${WIDGET_BUNDLE_ID:-com.example.myapp.widget}")|g" \
  -e "s|{{WIDGET_TEAM_ID}}|$(esc "${WIDGET_TEAM_ID:-XXXXXXXXXX}")|g" \
  -e "s|{{WIDGET_TARGET}}|$(esc "${WIDGET_TARGET_NAME:-URLImageWidget}")|g" \
  -e "s|{{WIDGET_FOLDER}}|$(esc "${WIDGET_FOLDER_NAME:-kartoteka.widget}")|g" \
  -e "s|{{APP_GROUP_ID}}|$(esc "${APP_GROUP_ID:-group.com.example.myapp}")|g" \
  -e "s|{{SMB_USER}}|$(esc "${SMB_USER:-Admin}")|g" \
  -e "s|{{SMB_PASS}}|$(esc "${SMB_PASS:-}")|g" \
  -e "s|{{SMB_BUILD_PATH}}|$(esc "${SMB_BUILD_PATH:-Admin/Desktop/build}")|g" \
  "$SRC" > "$TMP"

mkdir -p "$WORK_DIR"
osacompile -o "$DEST" "$TMP"
rm "$TMP"
echo "Compiled: $DEST"
