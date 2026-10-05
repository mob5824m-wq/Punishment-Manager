# shellcheck shell=bash
# Architecture resolution shared by the build scripts.
#
# Sentinel's installers are built per architecture, because PyInstaller does
# not cross-compile: a Linux .deb, a macOS .dmg and a Windows .exe each have
# to be built on a machine (or CI runner) of the architecture they target.
# Every build script therefore asks this file what it is building for, rather
# than hard-coding amd64.
#
# SENTINEL_TARGET_ARCH overrides the detected host architecture, which is what
# CI uses to ask for one explicitly (and to fail loudly if the runner's Python
# turns out to be the wrong architecture). Accepted spellings:
#
#   amd64, x86_64, x64   -> amd64   (the Debian/Ubuntu name for x86-64)
#   arm64, aarch64       -> arm64   (Apple Silicon, Windows on ARM, Debian arm64)
#
# Usage:
#
#   # shellcheck disable=SC1091
#   . "$PROJECT_ROOT/scripts/arch.sh"
#   ARCH="$(resolve_arch)"          # prints amd64 or arm64, or errors out
#   MAC="$(macos_arch_label "$ARCH")"   # x86_64 or arm64, for .dmg names
#   verify_binary_arch dist/sentinel/sentinel "$ARCH"

# Directory this file lives in, so callers can use `verify_binary_arch` without
# having set PROJECT_ROOT first.
ARCH_SH_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Normalize any accepted spelling to `amd64` / `arm64`. Prints nothing and
# returns 1 for an empty or unknown value.
canonical_arch() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        amd64|x86_64|x64|intel) printf 'amd64\n' ;;
        arm64|aarch64|armv8*)   printf 'arm64\n' ;;
        *) return 1 ;;
    esac
}

# The architecture of the machine running this script, canonicalized.
#
# `uname -m` is the only thing available in a POSIX shell; on Windows it is
# Git Bash's MSYS answer, which is why the Windows build is a batch script and
# uses `scripts/check_arch.py host` instead (PyInstaller has the same split:
# it reads PROCESSOR_ARCHITECTURE on Windows).
host_arch() {
    canonical_arch "$(uname -m 2>/dev/null || true)"
}

# The architecture to build for: $SENTINEL_TARGET_ARCH if set, else the host.
# Prints the canonical name, or explains what's wrong on stderr and fails.
resolve_arch() {
    if [ -n "${SENTINEL_TARGET_ARCH:-}" ]; then
        if ! canonical_arch "$SENTINEL_TARGET_ARCH"; then
            printf 'ERROR: SENTINEL_TARGET_ARCH=%s is not a known architecture (use amd64 or arm64).\n' \
                "$SENTINEL_TARGET_ARCH" >&2
            return 1
        fi
        return 0
    fi
    if ! host_arch; then
        printf 'ERROR: could not detect the host architecture (%s); set SENTINEL_TARGET_ARCH=amd64 or arm64.\n' \
            "$(uname -m 2>/dev/null || echo unknown)" >&2
        return 1
    fi
}

# The label macOS uses for a canonical architecture: `x86_64` (not `amd64`)
# for Intel, `arm64` for Apple Silicon. Used for .dmg filenames and for the
# Mach-O check, both of which follow Apple's spelling.
macos_arch_label() {
    case "$(canonical_arch "${1:-}" || true)" in
        amd64) printf 'x86_64\n' ;;
        arm64) printf 'arm64\n' ;;
        *) return 1 ;;
    esac
}

# Fail unless the binary at $1 was built for canonical architecture $2.
# Reads the file's own header (ELF / Mach-O / PE), so it catches a build that
# silently produced the host's architecture instead of the requested one - the
# failure mode that makes an "arm64" download crash on an arm64 machine.
verify_binary_arch() {
    local path="$1"
    local arch="$2"
    local python=""

    for candidate in "${SENTINEL_PYTHON:-}" python3 python; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
    if [ -z "$python" ]; then
        echo "ERROR: no python3/python on PATH to verify $path." >&2
        return 1
    fi

    "$python" "$ARCH_SH_DIR/check_arch.py" file "$path" --expect "$arch"
}
