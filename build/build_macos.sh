#!/usr/bin/env bash
# Build a .dmg of the Sentinel on macOS.
#
# Requirements (on a Mac of the architecture you are building for):
#   - Python 3.9+ on PATH
#   - pip install pyinstaller
#   - Either `create-dmg` (brew install create-dmg) or the built-in
#     `hdiutil` (always present on macOS).
#
# Output: dist/Sentinel-<VERSION>-<ARCH>.dmg
#         (ARCH: arm64 on Apple Silicon, x86_64 on Intel)
#
# One .dmg per architecture, because PyInstaller cannot cross-compile: run
# this on an Apple Silicon Mac for the arm64 disk image and on an Intel Mac
# for the x86_64 one (CI does both). The architecture defaults to the host's;
# SENTINEL_TARGET_ARCH overrides it, and a mismatch fails the build instead of
# producing a mislabelled .dmg.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

# shellcheck disable=SC1091
. "$PROJECT_ROOT/scripts/version.sh"
# shellcheck disable=SC1091
. "$PROJECT_ROOT/scripts/arch.sh"
VERSION="$(app_version "$PROJECT_ROOT")"
ARCH="$(resolve_arch)"
# Apple spells Intel x86_64; the .dmg name and the Mach-O check use that.
MACOS_ARCH="$(macos_arch_label "$ARCH")"
# Keep the whole build - PyInstaller and the arch check in the spec - on the
# architecture this script resolved.
export SENTINEL_TARGET_ARCH="$ARCH"
APP_NAME="Sentinel"
DMG_NAME="Sentinel-${VERSION}-${MACOS_ARCH}.dmg"

echo "==> Cleaning previous PyInstaller output (keeps build/ source dir)"
rm -rf dist
mkdir -p dist

echo "==> Building .app bundle with PyInstaller"
pyinstaller --noconfirm --clean build/pyinstaller.spec

APP_PATH="dist/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: PyInstaller did not produce ${APP_PATH}" >&2
    exit 1
fi

echo "==> Verifying the bundled binary is ${MACOS_ARCH}"
verify_binary_arch "$APP_PATH/Contents/MacOS/sentinel" "$ARCH"

# Optional: codesign with a Developer ID.
# Uncomment the lines below and replace the identity string.
#
# CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
# if security find-identity -p codesigning -v | grep -q "$CODESIGN_IDENTITY"; then
#     echo "==> Codesigning .app bundle"
#     codesign --deep --force --options runtime \
#         --sign "$CODESIGN_IDENTITY" "$APP_PATH"
#     codesign --verify --strict --verbose=2 "$APP_PATH"
# else
#     echo "WARNING: codesign identity not found; producing unsigned .dmg"
# fi

echo "==> Building .dmg"
DMG_STAGING="dist/dmg-staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_PATH" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

# Prefer create-dmg for a prettier result.
if command -v create-dmg >/dev/null 2>&1; then
    create-dmg \
        --volname "$APP_NAME" \
        --window-size 540 360 \
        --icon-size 96 \
        --icon "${APP_NAME}.app" 130 170 \
        --app-drop-link 380 170 \
        --no-internet-enable \
        "dist/${DMG_NAME}" \
        "$DMG_STAGING" || true
fi

# Fallback: hdiutil (always present on macOS).
if [ ! -f "dist/${DMG_NAME}" ]; then
    echo "==> Using hdiutil fallback"
    hdiutil create \
        -volname "$APP_NAME" \
        -srcfolder "$DMG_STAGING" \
        -ov \
        -format UDZO \
        "dist/${DMG_NAME}"
fi

echo
echo "Built: dist/${DMG_NAME}"
ls -lh "dist/${DMG_NAME}"
