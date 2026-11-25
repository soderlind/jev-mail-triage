#!/usr/bin/env bash
# Package a signed app into a compressed DMG with an Applications link.
# Usage: [SIGN_IDENTITY=...] make-dmg.sh [app]   The DMG itself is signed when SIGN_IDENTITY is set.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-$ROOT/build/app/JevMailTriage.app}"
[[ -d "$APP" ]] || { echo "No app at $APP" >&2; exit 1; }
codesign --verify --strict "$APP"

VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP/Contents/Info.plist")"
DMG="$ROOT/build/app/JevMailTriage-$VERSION-arm64.dmg"
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

ditto "$APP" "$STAGING/JevMailTriage.app"
ln -s /Applications "$STAGING/Applications"
rm -f "$DMG"
hdiutil create -volname "Jev Mail Triage $VERSION" -srcfolder "$STAGING" -fs APFS -format ULFO -ov "$DMG" >/dev/null

if [[ -n "${SIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG"
  codesign --verify --strict "$DMG"
fi
hdiutil verify "$DMG" >/dev/null
echo "$DMG"
