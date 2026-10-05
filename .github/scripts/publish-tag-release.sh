#!/usr/bin/env bash
# Attach the installers built for a `v*` tag to that tag's GitHub Release.
#
# Called by .github/workflows/release.yml after the three build jobs
# (build-installers.yml) copied the .deb, .dmg and .exe into ./artifacts. The
# tag is the one that triggered the workflow, so `GITHUB_REF_NAME` names both
# the release and its assets' version (both come from the VERSION file, which
# scripts/make_release.sh refuses to tag past).
#
# Re-running on an existing release replaces its assets instead of failing, so
# re-pushing a tag repairs a release rather than erroring out.
#
# Usage:
#   GITHUB_REF_NAME=v1.2.3 .github/scripts/publish-tag-release.sh [artifacts-dir]
#
# Requires: gh (authenticated through GH_TOKEN) and git.
# Written for bash 3.2 (the macOS CI job runs the tests that execute it).
set -euo pipefail

ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../.." && pwd )"
cd "$ROOT"

ARTIFACTS="${1:-artifacts}"
TAG="${GITHUB_REF_NAME:-}"
if [ -z "$TAG" ]; then
    echo "ERROR: GITHUB_REF_NAME (the tag being released) is not set." >&2
    exit 1
fi
if [ ! -f VERSION ]; then
    echo "ERROR: no VERSION file at $ROOT; can't check the tag against it." >&2
    exit 1
fi
VERSION="$(tr -d '[:space:]' < VERSION)"

# Collect the installers. Explicit tests rather than a bare glob, so a missing
# platform is reported instead of being attached without it.
files=()
count=0
for pattern in "$ARTIFACTS"/*.deb "$ARTIFACTS"/*.dmg "$ARTIFACTS"/*.exe; do
    if [ -f "$pattern" ]; then
        files[$count]="$pattern"
        count=$((count + 1))
    fi
done
if [ "$count" -eq 0 ]; then
    echo "ERROR: no installers in $ARTIFACTS (expected .deb, .dmg and .exe)." >&2
    exit 1
fi

echo "Releasing ${TAG} (VERSION ${VERSION}) with ${count} installer(s):"
for file in "${files[@]}"; do
    echo "  $(basename "$file")"
done

if ! git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
    echo "WARNING: tag ${TAG} is not in this checkout; trusting the remote." >&2
fi

# The installers are named from VERSION, so a tag that disagrees would publish
# assets whose names contradict the release (the drift fix 13 in
# docs/CI_FIXES.md). make_release.sh refuses to tag in that case; a hand-pushed
# tag gets a loud warning instead of a silent mismatch.
case "$TAG" in
    "v${VERSION}"|"v${VERSION}"-*) ;;
    *)
        echo "WARNING: VERSION says ${VERSION} but the tag is ${TAG}; the" >&2
        echo "         installers will carry the ${VERSION} name." >&2
        ;;
esac

if gh release view "$TAG" >/dev/null 2>&1; then
    # A tag re-push (or a repaired release): replace the assets.
    gh release upload "$TAG" "${files[@]}" --clobber
else
    # v1.2.3-rc1 and friends: a prerelease, exactly as before this script
    # existed (anything with a hyphen).
    args=(--verify-tag --title "$TAG" --generate-notes)
    case "$TAG" in
        *-*) args=(--prerelease "${args[@]}") ;;
    esac
    # --generate-notes: GitHub's automatic "what changed since the last
    # release" notes. --verify-tag keeps a typo'd tag from creating one.
    gh release create "$TAG" "${args[@]}" "${files[@]}"
fi
