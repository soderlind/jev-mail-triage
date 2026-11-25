# Inside-out signing for JevMailTriage.app. Sourced by build-app.sh and sign.sh.
# Deliberately avoids `codesign --deep`: every nested Mach-O is signed explicitly, deepest
# first, then each bundle is sealed with its own entitlements.

RESOURCES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"

# sign_app <app> <identity>   identity "-" means ad-hoc (no timestamp).
sign_app() {
  local app="$1" identity="$2"
  local helper="$app/Contents/Helpers/JevMailTriageHelper.app"
  local helper_exe="$helper/Contents/MacOS/jev-mail-triage-helper"
  local -a flags=(--force --sign "$identity")
  # Hardened runtime enforces library validation (same Team ID for every loaded image).
  # Ad-hoc signatures have no Team ID, so local ad-hoc builds run without it; any real
  # identity gets the hardened runtime and, for Developer ID, a secure timestamp.
  if [[ "$identity" != "-" ]]; then
    flags+=(--options runtime --timestamp)
  fi

  # 1. Nested code inside the helper: dylibs and Python extension modules. Only real files;
  #    PyInstaller symlinks Resources/ entries to Frameworks/ and those are sealed as links.
  local count=0
  while IFS= read -r -d '' file; do
    [[ "$file" == "$helper_exe" ]] && continue
    file -b "$file" | grep -q 'Mach-O' || continue
    codesign "${flags[@]}" "$file"
    count=$((count + 1))
  done < <(find "$helper/Contents" -type f -print0 | sort -z -r)
  echo "Signed $count nested helper binaries."

  # 2. The helper bundle (seals its executable, resources, and the signatures above).
  codesign "${flags[@]}" --identifier com.soderlind.JevMailTriage.Helper \
    --entitlements "$RESOURCES_DIR/Helper.entitlements" "$helper"

  # 3. The app bundle (seals the main executable, the agent plist, and the helper).
  codesign "${flags[@]}" --identifier com.soderlind.JevMailTriage \
    --entitlements "$RESOURCES_DIR/JevMailTriage.entitlements" "$app"

  codesign --verify --strict --verbose=2 "$helper"
  codesign --verify --strict --verbose=2 "$app"
}
