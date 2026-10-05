# shellcheck shell=bash
# Resolve the version to build, and echo it.
#
# Precedence: $SENTINEL_VERSION (explicit override, e.g. CI) > the VERSION file at
# the project root > a matching git tag > a clearly-bogus dev version.
#
# The VERSION file is the single source of truth: build_linux.sh,
# build_macos.sh, build_windows.bat and build/pyinstaller.spec all read it, so
# a release can't produce sentinel_1.0.0_arm64.deb under a v1.0.1 tag.
#
# Usage: . scripts/version.sh && VERSION="$(app_version)"
app_version() {
    local root="${1:-$PWD}"
    if [ -n "${SENTINEL_VERSION:-}" ]; then
        printf '%s\n' "$SENTINEL_VERSION"
        return 0
    fi
    if [ -f "$root/VERSION" ]; then
        local from_file
        from_file="$(tr -d '[:space:]' < "$root/VERSION")"
        if [ -n "$from_file" ]; then
            printf '%s\n' "$from_file"
            return 0
        fi
    fi
    # Falls back to the tag when the file is missing (e.g. a tarball without it).
    local tag
    tag="$(git -C "$root" describe --tags --exact-match 2>/dev/null || true)"
    case "$tag" in
        v*) printf '%s\n' "${tag#v}"; return 0 ;;
    esac
    printf '0.0.0+unknown\n'
}
