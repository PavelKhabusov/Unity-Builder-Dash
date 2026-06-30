#!/bin/bash
# release.sh — App Store release pipeline for Unity Builder Dash (Mac side).
#
# Called by ios_build.scpt as:  bash release.sh <archive|validate|distribute>
# Keeps the giant xcodebuild/altool command lines OUT of the .scpt (which
# truncated them and made debugging impossible) and emits clean, line-by-line
# progress that the host's ProgressListener (TCP:8080) renders.
#
# All parameters are read from $WORK_DIR/config.json at runtime:
#   asc_key_id, asc_issuer_id, release_team_id, mac_unlock_password
# The .p8 is expected at ~/.appstoreconnect/private_keys/AuthKey_<id>.p8
# (the host installs it there before each run).
#
# Progress: every stage prints "[n/m] message" — ProgressListener parses the
# fraction. Stdout/stderr are line-buffered so the Terminal `tee >(nc ...)` in
# ios_build.scpt streams them live to the host instead of buffering to the end.

set -o pipefail

WORK_DIR="${WORK_DIR:-$HOME/Desktop}"
ACTION="$1"
IOS_DIR="$WORK_DIR/iOS"
ARCHIVE="$IOS_DIR/build/Unity-iPhone.xcarchive"
EXPORT_DIR="$IOS_DIR/build/export"
EXPORT_PLIST="$IOS_DIR/build/ExportOptions.plist"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

# ── config readers ──
cfg() { /usr/bin/python3 -c "import json,sys; print(json.load(open('$WORK_DIR/config.json')).get(sys.argv[1],''))" "$1" 2>/dev/null; }

KEY_ID="$(cfg asc_key_id)"
ISSUER_ID="$(cfg asc_issuer_id)"
TEAM_ID="$(cfg release_team_id)"
UNLOCK_PW="$(cfg mac_unlock_password)"
P8="$HOME/.appstoreconnect/private_keys/AuthKey_${KEY_ID}.p8"

# total step count per action, for the [n/m] progress fractions
case "$ACTION" in
  archive)    TOTAL=3 ;;
  validate)   TOTAL=4 ;;
  distribute) TOTAL=3 ;;
  *) echo "release.sh: unknown action '$ACTION' (use archive|validate|distribute)"; exit 2 ;;
esac

step() { echo "[$1/$TOTAL] $2"; }   # ProgressListener parses [n/m]
fail() { echo "ERROR: $1"; exit "${2:-1}"; }

# ── shared prep: keep awake + unlock keychain + grant codesign key access ──
# caffeinate as a sibling so the Mac can't sleep mid-step; killed on exit.
caffeinate -i -s & CAF=$!
trap 'kill $CAF 2>/dev/null' EXIT

unlock_keychain() {
  # Without unlock+partition-list, codesign pops a modal "allow key access?"
  # dialog that beeps and hangs over SSH (no one to click it).
  if [ -n "$UNLOCK_PW" ]; then
    security unlock-keychain -p "$UNLOCK_PW" "$KEYCHAIN" \
      && security set-keychain-settings -t 3600 -l "$KEYCHAIN"
    security set-key-partition-list -S apple-tool:,apple:,codesign: \
      -s -k "$UNLOCK_PW" "$KEYCHAIN" >/dev/null 2>&1
  fi
}

require_api_key() {
  [ -n "$KEY_ID" ]    || fail "API Key ID not set (iOS settings)"
  [ -n "$ISSUER_ID" ] || fail "API Issuer ID not set (iOS settings)"
  [ -f "$P8" ]        || fail "API .p8 not found at $P8"
}

# ── actions ──
do_archive() {
  cd "$IOS_DIR" || fail "iOS project dir missing: $IOS_DIR"
  step 1 "Preparing keychain"
  unlock_keychain
  local team_arg=""
  [ -n "$TEAM_ID" ] && team_arg="DEVELOPMENT_TEAM=$TEAM_ID"
  step 2 "Archiving (xcodebuild archive, Release)…"
  xcodebuild -workspace Unity-iPhone.xcworkspace -scheme Unity-iPhone \
    -configuration Release -destination 'generic/platform=iOS' \
    -allowProvisioningUpdates -archivePath "$ARCHIVE" $team_arg archive \
    || fail "archive failed" $?
  step 3 "Archive created: $ARCHIVE"
}

do_validate() {
  require_api_key
  cd "$IOS_DIR" || fail "iOS project dir missing: $IOS_DIR"
  [ -d "$ARCHIVE" ] || fail "No archive — run Archive first ($ARCHIVE)"
  step 1 "Preparing keychain"
  unlock_keychain
  step 2 "Exporting .ipa (App Store Connect signing)…"
  rm -rf "$EXPORT_DIR"
  xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportPath "$EXPORT_DIR" \
    -exportOptionsPlist "$EXPORT_PLIST" -allowProvisioningUpdates \
    -authenticationKeyPath "$P8" -authenticationKeyID "$KEY_ID" \
    -authenticationKeyIssuerID "$ISSUER_ID" \
    || fail "exportArchive failed" $?
  local ipa
  ipa="$(/usr/bin/find "$EXPORT_DIR" -maxdepth 1 -name '*.ipa' | /usr/bin/head -1)"
  [ -n "$ipa" ] || fail "no .ipa produced by export"
  step 3 "Validating $ipa with App Store Connect…"
  xcrun altool --validate-app -f "$ipa" -t ios \
    --apiKey "$KEY_ID" --apiIssuer "$ISSUER_ID" \
    || fail "altool validate failed" $?
  step 4 "Validation OK: $ipa"
}

do_distribute() {
  require_api_key
  cd "$IOS_DIR" || fail "iOS project dir missing: $IOS_DIR"
  local ipa
  ipa="$(/usr/bin/find "$EXPORT_DIR" -maxdepth 1 -name '*.ipa' | /usr/bin/head -1)"
  [ -n "$ipa" ] || fail "No .ipa found — run Validate (export) first"
  step 1 "Found $ipa"
  step 2 "Uploading to App Store Connect (TestFlight after processing)…"
  xcrun altool --upload-app -f "$ipa" -t ios \
    --apiKey "$KEY_ID" --apiIssuer "$ISSUER_ID" \
    || fail "altool upload failed" $?
  step 3 "Upload complete — check App Store Connect → TestFlight"
}

case "$ACTION" in
  archive)    do_archive ;;
  validate)   do_validate ;;
  distribute) do_distribute ;;
esac
echo "${ACTION} step finished."
