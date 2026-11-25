#!/usr/bin/env bash
# Re-sign an assembled app with a real identity (hardened runtime, secure timestamp).
# Usage: SIGN_IDENTITY="Developer ID Application: Name (TEAMID)" sign.sh [app]
# An "Apple Development" identity also works for local hardened-runtime testing, but only
# a Developer ID Application identity can be notarized.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-$ROOT/build/app/JevMailTriage.app}"
: "${SIGN_IDENTITY:?Set SIGN_IDENTITY to a codesigning identity name or SHA-1 hash}"

# shellcheck source=sign-lib.sh
source "$ROOT/macos/scripts/sign-lib.sh"

[[ -d "$APP" ]] || { echo "No app at $APP; run build-app.sh first." >&2; exit 1; }
sign_app "$APP" "$SIGN_IDENTITY"

codesign -dv --verbose=2 "$APP" 2>&1 | grep -E '^(Authority|TeamIdentifier|Runtime|Timestamp)' || true
"$ROOT/packaging/smoke-helper.sh" "$APP/Contents/Helpers/JevMailTriageHelper.app/Contents/MacOS/jev-mail-triage-helper"
echo "Signed $APP"
