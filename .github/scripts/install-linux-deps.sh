#!/usr/bin/env bash
# Installs the system packages needed to build the Punishment Manager
# .deb on a GitHub-hosted Ubuntu runner.
#
# Why this lives in its own file: pasting a multi-line `run: |` block
# into the GitHub web editor can strip leading whitespace, which
# silently breaks the YAML literal block scalar. A separate file is
# pasted as a single `bash <path>` invocation and the indentation
# is preserved.
#
# Strategy: try the bare libpython3.11 package first (works on Ubuntu
# 22.04), fall back to libpython3.11-dev (works on 24.04+). Either
# way, PyInstaller finds the runtime it needs.
set -euo pipefail

echo "==> apt-get update"
sudo apt-get update

echo "==> Installing fakeroot, dpkg, lintian (always available)"
sudo apt-get install -y fakeroot dpkg lintian

echo "==> Installing libpython3.11 (with fallback to -dev)"
# Try the bare package; on 24.04+ it lives in the -dev package.
if sudo apt-get install -y libpython3.11 2>/dev/null; then
    echo "    Installed libpython3.11 (bare package)"
elif sudo apt-get install -y libpython3.11-dev 2>/dev/null; then
    echo "    Installed libpython3.11-dev (fallback for newer Ubuntu)"
else
    echo "    WARNING: could not install libpython3.11; build may fail"
fi

echo "==> Linux dependencies installed."
