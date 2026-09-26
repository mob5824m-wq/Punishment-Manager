#!/usr/bin/env bash
# Build a .dmg of the Punishment Manager on macOS.
#
# Requirements:
#   - Python 3.9+ on PATH
#   - pip install pyinstaller
#   - Either `create-dmg` (brew install create-dmg) or the built-in
#     `hdiutil` (always present on macOS).
#
# Output: dist/PunishmentManager-1.0.0.dmg
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

VERSION="1.0.0"
APP_NAME="Punishment Manager"
DMG_NAME="PunishmentManager-${VERSION}.dmg"

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
