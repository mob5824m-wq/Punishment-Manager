#!/usr/bin/env bash
# Installs the system packages needed to build the Sentinel
# .deb on a GitHub-hosted Ubuntu runner, on either architecture
# (ubuntu-24.04 for amd64, ubuntu-24.04-arm for arm64).
#
# Why this lives in its own file: pasting a multi-line `run: |` block
# into the GitHub web editor can strip leading whitespace, which
# silently breaks the YAML literal block scalar. A separate file is
# pasted as a single `bash <path>` invocation and the indentation
# is preserved.
#
# Strategy: the shared library PyInstaller links against comes from the
# libpythonX.Y package of the interpreter doing the build, so ask that
# interpreter for its version instead of assuming one - the runner's Python
# and the arm64 images can differ. Try the bare package first (Ubuntu 22.04),
# then the -dev package (24.04+). Either way, PyInstaller finds the runtime it
# needs.
set -euo pipefail

echo "==> apt-get update"
sudo apt-get update

echo "==> Installing fakeroot, dpkg, lintian (always available)"
sudo apt-get install -y fakeroot dpkg lintian

# The interpreter that will run PyInstaller - setup-python put it first on
# PATH - not a hard-coded version. Falls back to the apt default if python3
# is missing for some reason.
LIBPYTHON="$({ python  -c 'import sys; print("libpython%d.%d" % sys.version_info[:2])' 2>/dev/null \
    || python3 -c 'import sys; print("libpython%d.%d" % sys.version_info[:2])' 2>/dev/null \
    || echo libpython3.11; })"
echo "==> Installing ${LIBPYTHON} (with fallback to ${LIBPYTHON}-dev)"
# Try the bare package; on 24.04+ the shared library lives in the -dev package.
if sudo apt-get install -y "$LIBPYTHON" 2>/dev/null; then
    echo "    Installed ${LIBPYTHON} (bare package)"
elif sudo apt-get install -y "${LIBPYTHON}-dev" 2>/dev/null; then
    echo "    Installed ${LIBPYTHON}-dev (fallback for newer Ubuntu)"
else
    echo "    WARNING: could not install ${LIBPYTHON}; build may fail"
fi

echo "==> Linux dependencies installed for $(uname -m)."
