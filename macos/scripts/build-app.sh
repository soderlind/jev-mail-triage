#!/usr/bin/env bash
# Build JevMailTriage.app for Apple Silicon with the bundled Python helper.
# Produces an ad-hoc signed development build at build/app/JevMailTriage.app; use sign.sh
# to re-sign it with a Developer ID identity for distribution.
#
# Env: MIN_MACOS (default 27.0), REBUILD_HELPER=1 to force a fresh helper build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MACOS="$ROOT/macos"
MIN_MACOS="${MIN_MACOS:-27.0}"
HELPER_SRC="$ROOT/build/helper/dist/JevMailTriageHelper.app"
OUT="$ROOT/build/app"
APP="$OUT/JevMailTriage.app"

# shellcheck source=sign-lib.sh
source "$MACOS/scripts/sign-lib.sh"

[[ "$(uname -m)" == "arm64" ]] || { echo "Apple Silicon build host required." >&2; exit 1; }

if [[ "${REBUILD_HELPER:-0}" == "1" || ! -x "$HELPER_SRC/Contents/MacOS/jev-mail-triage-helper" ]]; then
  MIN_MACOS="$MIN_MACOS" "$ROOT/packaging/build-helper.sh"
fi

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
BUILD_NUMBER="$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 1)"

echo "==> Building Swift app (release, arm64)"
swift build --package-path "$MACOS" -c release --arch arm64
BIN_DIR="$(swift build --package-path "$MACOS" -c release --arch arm64 --show-bin-path)"

echo "==> Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Helpers" "$APP/Contents/Library/LaunchAgents" "$APP/Contents/Resources"
cp "$BIN_DIR/JevMailTriage" "$APP/Contents/MacOS/JevMailTriage"
sed -e "s/__VERSION__/$VERSION/" -e "s/__BUILD__/$BUILD_NUMBER/" -e "s/__MIN_MACOS__/$MIN_MACOS/" \
  "$MACOS/Resources/Info.plist" >"$APP/Contents/Info.plist"
printf 'APPL????' >"$APP/Contents/PkgInfo"
cp "$MACOS/Resources/com.soderlind.JevMailTriage.worker.plist" "$APP/Contents/Library/LaunchAgents/"
# ditto preserves the helper's internal symlinks.
ditto "$HELPER_SRC" "$APP/Contents/Helpers/JevMailTriageHelper.app"
plutil -lint "$APP/Contents/Info.plist" "$APP/Contents/Library/LaunchAgents/"*.plist >/dev/null

echo "==> Ad-hoc signing (inside-out)"
sign_app "$APP" "-"

echo "==> Checking Mach-O architecture and minimum macOS"
"$ROOT/packaging/check-macho.sh" "$APP" "$MIN_MACOS"

echo "==> Smoke-testing the embedded helper"
"$ROOT/packaging/smoke-helper.sh" "$APP/Contents/Helpers/JevMailTriageHelper.app/Contents/MacOS/jev-mail-triage-helper"

echo "Built $APP ($VERSION build $BUILD_NUMBER, macOS $MIN_MACOS+, arm64, $(du -sh "$APP" | cut -f1))."
