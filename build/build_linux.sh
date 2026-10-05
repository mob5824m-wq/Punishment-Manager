#!/usr/bin/env bash
# Build a Linux .deb package for the Sentinel.
#
# Requirements (run on a Debian/Ubuntu host):
#   - Python 3.9+ on PATH
#   - pip install pyinstaller
#   - dpkg, fakeroot
#
# Output: dist/sentinel_<VERSION>_amd64.deb
#
# The version comes from the VERSION file at the project root, so a release
# tag and the file it produces can't disagree (see scripts/version.sh).
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

# shellcheck disable=SC1091
. "$PROJECT_ROOT/scripts/version.sh"
VERSION="$(app_version "$PROJECT_ROOT")"
ARCH="amd64"
PKG_NAME="sentinel"
DEB_FILE="${PKG_NAME}_${VERSION}_${ARCH}.deb"
echo "==> Building ${PKG_NAME} ${VERSION}"

echo "==> Cleaning previous PyInstaller output (keeps build/ source dir)"
rm -rf dist
mkdir -p dist

echo "==> Building binary with PyInstaller"
pyinstaller --noconfirm --clean build/pyinstaller.spec
if [ ! -x "dist/sentinel/sentinel" ]; then
    echo "ERROR: PyInstaller did not produce the expected binary" >&2
    exit 1
fi

echo "==> Staging .deb structure"
STAGE="dist/deb-staging"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN"
mkdir -p "$STAGE/opt/${PKG_NAME}"
mkdir -p "$STAGE/usr/bin"
mkdir -p "$STAGE/usr/share/applications"
mkdir -p "$STAGE/usr/share/pixmaps"
mkdir -p "$STAGE/lib/systemd/system"
mkdir -p "$STAGE/etc"

# Copy the binary tree.
cp -r dist/sentinel/. "$STAGE/opt/${PKG_NAME}/"
chmod 755 "$STAGE/opt/${PKG_NAME}/sentinel"

# Symlink the binary into /usr/bin so it's on PATH.
ln -s "/opt/${PKG_NAME}/sentinel" "$STAGE/usr/bin/${PKG_NAME}"

# DEBIAN metadata.
cat > "$STAGE/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Depends: libc6 (>= 2.31)
Maintainer: Arena <noreply@arena.ai>
Description: Discord server management bot (moderation, rules, roles).
 Sentinel manages a Discord server from one /manage command tree:
 timed punishment roles, warnings, rules posts with reaction-role
 acceptance, reaction-role menus, and an authenticated web dashboard.
Homepage: https://github.com/mob5824m-wq/Sentinel
EOF

cat > "$STAGE/DEBIAN/conffiles" <<EOF
/etc/sentinel/config.json
EOF

# Maintainer scripts.
install -m 755 build/linux/postinst  "$STAGE/DEBIAN/postinst"
install -m 755 build/linux/postrm    "$STAGE/DEBIAN/postrm"
install -m 755 build/linux/prerm     "$STAGE/DEBIAN/prerm"

# Systemd unit.
install -m 644 build/linux/sentinel.service \
    "$STAGE/lib/systemd/system/sentinel.service"

# A read-only copy of the service/desktop templates, so `sentinel
# --install-service` works from the installed binary (paths.resource_path
# looks in /usr/share/sentinel when the source tree isn't present).
install -d "$STAGE/usr/share/sentinel/build/linux"
install -m 644 build/linux/sentinel.service \
    "$STAGE/usr/share/sentinel/build/linux/sentinel.service"
install -m 644 build/linux/sentinel.desktop \
    "$STAGE/usr/share/sentinel/build/linux/sentinel.desktop"

# Desktop entry.
install -m 644 build/linux/sentinel.desktop \
    "$STAGE/usr/share/applications/sentinel.desktop"

# Icon (optional but recommended).
if [ -f "build/icon.png" ]; then
    install -m 644 "build/icon.png" \
        "$STAGE/usr/share/pixmaps/sentinel.png"
fi

# Default config (gets overwritten by the installer on first run).
install -d "$STAGE/etc/sentinel"
install -m 600 config.json "$STAGE/etc/sentinel/config.json"

# Optional: lint the staged tree with lintian.
if command -v lintian >/dev/null 2>&1; then
    echo "==> Running lintian"
    lintian --info --display-info --display-experimental \
        --pedantic --no-tag-display-limit \
        "dist/${DEB_FILE}" 2>&1 || true
fi

echo "==> Building .deb"
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "dist/${DEB_FILE}"

echo
echo "Built: dist/${DEB_FILE}"
ls -lh "dist/${DEB_FILE}"
echo
echo "Install with:    sudo dpkg -i dist/${DEB_FILE}"
echo "Start with:      sudo systemctl start sentinel"
echo "Enable on boot:  sudo systemctl enable sentinel"
echo "Run installer:   sentinel"
