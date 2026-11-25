#!/usr/bin/env bash
# Verify every Mach-O under a bundle is arm64 and targets at most the given macOS version.
# Usage: check-macho.sh <bundle-or-dir> <max-minos, e.g. 27.0>
set -euo pipefail

TARGET="$1"
MAX="$2"
failures=0
count=0

version_le() { [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$1" ]]; }

while IFS= read -r -d '' file; do
  file -b "$file" | grep -q 'Mach-O' || continue
  count=$((count + 1))
  archs="$(lipo -archs "$file" 2>/dev/null || true)"
  if [[ " $archs " != *" arm64 "* ]]; then
    echo "missing arm64: $file ($archs)" >&2
    failures=$((failures + 1))
    continue
  fi
  minos="$(vtool -arch arm64 -show-build "$file" 2>/dev/null | awk '/minos/ {print $2; exit}')"
  if [[ -z "$minos" ]]; then
    minos="$(otool -arch arm64 -l "$file" | awk '/LC_VERSION_MIN_MACOSX/ {f=1} f && /version/ {print $2; exit}')"
  fi
  if [[ -n "$minos" ]] && ! version_le "$minos" "$MAX"; then
    echo "minos $minos > $MAX: $file" >&2
    failures=$((failures + 1))
  fi
done < <(find "$TARGET" -type f -print0)

echo "Checked $count Mach-O files; $failures problems."
[[ $failures -eq 0 ]]
