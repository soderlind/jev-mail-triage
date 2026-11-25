#!/usr/bin/env bash
# Notarize and staple a Developer ID signed DMG.
# One-time setup (stores the app-specific password in your login Keychain, never in the repo):
#   xcrun notarytool store-credentials jev-notary --apple-id YOU@example.com --team-id TEAMID
# Usage: NOTARY_PROFILE=jev-notary notarize.sh build/app/JevMailTriage-<version>-arm64.dmg
set -euo pipefail

DMG="${1:?Usage: notarize.sh <dmg>}"
: "${NOTARY_PROFILE:?Set NOTARY_PROFILE to a notarytool keychain profile name}"

codesign --verify --strict "$DMG"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
echo "Notarized and stapled $DMG"
