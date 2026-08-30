#!/usr/bin/env bash
# Build a Linux .deb package for the Punishment Manager.
#
# Requirements (run on a Debian/Ubuntu host):
#   - Python 3.9+ on PATH
#   - pip install pyinstaller
#   - dpkg, fakeroot
#
# Output: dist/punishment-manager_1.0.0_amd64.deb
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

VERSION="1.0.0"
ARCH="amd64"
PKG_NAME="punishment-manager"
DEB_FILE="${PKG_NAME}_${VERSION}_${ARCH}.deb"

echo "==> Cleaning previous PyInstaller output (keeps build/ source dir)"
rm -rf dist
mkdir -p dist

echo "==> Building binary with PyInstaller"
pyinstaller --noconfirm --clean build/pyinstaller.spec
if [ ! -x "dist/punishment-manager/punishment-manager" ]; then
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
cp -r dist/punishment-manager/. "$STAGE/opt/${PKG_NAME}/"
chmod 755 "$STAGE/opt/${PKG_NAME}/punishment-manager"

# Symlink the binary into /usr/bin so it's on PATH.
ln -s "/opt/${PKG_NAME}/punishment-manager" "$STAGE/usr/bin/${PKG_NAME}"

# DEBIAN metadata.
cat > "$STAGE/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: net
Priority: optional
Architecture: ${ARCH}
Depends: libc6 (>= 2.31)
Maintainer: Arena <noreply@arena.ai>
Description: Discord bot for temporary role-based punishments.
 Punishment Manager swaps a user's role between a "punished" state
 and a "post-punished" state on a timer, and posts Discord embeds
 to a staff channel and to the punished user.
Homepage: https://github.com/mob5824m-wq/Punishment-Manager
EOF

cat > "$STAGE/DEBIAN/conffiles" <<EOF
/etc/punishment-manager/config.json
EOF

# Maintainer scripts.
install -m 755 build/linux/postinst  "$STAGE/DEBIAN/postinst"
install -m 755 build/linux/postrm    "$STAGE/DEBIAN/postrm"
install -m 755 build/linux/prerm     "$STAGE/DEBIAN/prerm"

# Systemd unit.
install -m 644 build/linux/punishment-manager.service \
    "$STAGE/lib/systemd/system/punishment-manager.service"

# Desktop entry.
install -m 644 build/linux/punishment-manager.desktop \
    "$STAGE/usr/share/applications/punishment-manager.desktop"

# Icon (optional but recommended).
if [ -f "build/icon.png" ]; then
    install -m 644 "build/icon.png" \
        "$STAGE/usr/share/pixmaps/punishment-manager.png"
fi

# Default config (gets overwritten by the installer on first run).
install -d "$STAGE/etc/punishment-manager"
install -m 600 config.json "$STAGE/etc/punishment-manager/config.json"

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
echo "Start with:      sudo systemctl start punishment-manager"
echo "Enable on boot:  sudo systemctl enable punishment-manager"
echo "Run installer:   punishment-manager"
