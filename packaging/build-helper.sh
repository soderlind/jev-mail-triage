#!/usr/bin/env bash
# Build the bundled Python helper (PyInstaller onedir, Apple Silicon) into build/helper/.
#
# Prerequisites: Xcode command line tools and uv (installs python-build-standalone 3.12,
# which targets macOS 11, unlike Homebrew's Python, which targets the host OS).
# Override with PYTHON=/path/to/python3.12. Every bundled Mach-O is checked against
# MIN_MACOS (default 27.0, the app's deployment target).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$(uv python find --managed-python 3.12 2>/dev/null || true)}"
BUILD="$ROOT/build/helper"
VENV="$BUILD/venv"
MIN_MACOS="${MIN_MACOS:-27.0}"

[[ "$(uname -m)" == "arm64" ]] || { echo "Apple Silicon build host required." >&2; exit 1; }
[[ -x "$PYTHON" ]] || { echo "Python not found at $PYTHON (run: uv python install 3.12, or set PYTHON=...)." >&2; exit 1; }

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
[[ -n "$VERSION" ]] || { echo "Could not read version from pyproject.toml." >&2; exit 1; }

echo "==> Helper $VERSION, Python $("$PYTHON" -c 'import platform;print(platform.python_version())'), min macOS $MIN_MACOS"
rm -rf "$BUILD"
mkdir -p "$BUILD"
"$PYTHON" -m venv "$VENV"
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
"$VENV/bin/python" -m pip install --quiet --require-virtualenv \
  -r "$ROOT/packaging/requirements-helper.txt" -r "$ROOT/packaging/requirements-build.txt"
"$VENV/bin/python" -m pip install --quiet --require-virtualenv --no-deps "$ROOT"

cd "$ROOT/packaging"
JEV_VERSION="$VERSION" "$VENV/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --distpath "$BUILD/dist" --workpath "$BUILD/work" helper.spec

HELPER_APP="$BUILD/dist/JevMailTriageHelper.app"
[[ -d "$HELPER_APP" ]] || { echo "PyInstaller produced no helper app." >&2; exit 1; }

echo "==> Checking architecture and minimum macOS of bundled binaries"
"$ROOT/packaging/check-macho.sh" "$HELPER_APP" "$MIN_MACOS"

echo "==> Smoke test outside any virtual environment"
"$ROOT/packaging/smoke-helper.sh" "$HELPER_APP/Contents/MacOS/jev-mail-triage-helper"

echo "Built $HELPER_APP"
