#!/bin/bash
# Run once on the Mac. Decompiles IOSbuild.scpt, makes two edits, recompiles:
#   1. Rewrites hardcoded "/Users/pavel/Desktop" → "$WORK_DIR" if it differs,
#      so you can keep build artefacts anywhere (e.g. ~/Builds/iOS).
#   2. Wraps the SMB-mount block in an existence check so the Linux/Windows
#      host can scp "IOS.zip" directly to $WORK_DIR/IOS.zip and skip the mount.
#
# Idempotent — re-running is safe. A sentinel comment flags the SMB patch.
#
# Usage:    bash patch_scpt.sh [path/to/IOSbuild.scpt]
# Env vars: WORK_DIR   Mac work folder (default: /Users/pavel/Desktop)
set -e

SCPT="${1:-$HOME/Desktop/IOSbuild.scpt}"
WORK_DIR="${WORK_DIR:-/Users/pavel/Desktop}"
# Normalize: strip trailing slash
WORK_DIR="${WORK_DIR%/}"
[ -f "$SCPT" ] || { echo "Not found: $SCPT"; exit 1; }

BACKUP="$SCPT.bak"
SRC="$(mktemp -t IOSbuild).applescript"

# Preserve a one-time backup of the original binary
[ -f "$BACKUP" ] || cp "$SCPT" "$BACKUP"

echo "Decompiling $SCPT..."
osadecompile "$SCPT" > "$SRC"

# ── Step 1: path rewrite ──
if [ "$WORK_DIR" != "/Users/pavel/Desktop" ]; then
    # Escape for sed (only | needs special care since we use | as delim)
    esc_work=$(printf '%s' "$WORK_DIR" | sed 's|[|]|\\|g')
    echo "Rewriting /Users/pavel/Desktop → $WORK_DIR"
    # macOS sed requires '' after -i
    sed -i '' "s|/Users/pavel/Desktop|$esc_work|g" "$SRC"
fi

if grep -q "CMB_PATCH_LOCAL_ZIP" "$SRC"; then
    echo "SMB block already patched — recompiling with updated paths only."
    osacompile -o "$SCPT" "$SRC"
    rm "$SRC"
    exit 0
fi

# Patch strategy:
# Find the SMB-mount block (`mount volume "smb://..."`) and wrap it in
#   `if not (POSIX file "/Users/pavel/Desktop/IOS.zip" exists) then ... end if`
# so that it's only executed when the host hasn't already scp'd IOS.zip.
#
# The sentinel comment `CMB_PATCH_LOCAL_ZIP` marks it so we can detect
# re-runs.
python3 - "$SRC" "$WORK_DIR" <<'PY'
import re, sys
path, work_dir = sys.argv[1], sys.argv[2]
text = open(path).read()

pattern = re.compile(r'(\s*)(mount volume\s+"smb://[^"\n]+"[^\n]*)', re.IGNORECASE)
m = pattern.search(text)
if not m:
    sys.stderr.write("WARN: mount volume block not found — nothing to patch.\n")
    sys.exit(0)

indent = m.group(1)
line = m.group(2)
zip_path = f"{work_dir}/IOS.zip"
replacement = (
    f'{indent}-- CMB_PATCH_LOCAL_ZIP: skip SMB mount when host already scp\'d IOS.zip\n'
    f'{indent}tell application "System Events" to set zipExists to (exists file "{zip_path}")\n'
    f'{indent}if not zipExists then\n'
    f'{indent}    {line}\n'
    f'{indent}end if'
)
text = text[:m.start()] + replacement + text[m.end():]
open(path, "w").write(text)
print("Patched SMB-mount block.")
PY

echo "Recompiling → $SCPT"
osacompile -o "$SCPT" "$SRC"
rm "$SRC"
echo "Done. Backup kept at $BACKUP"
