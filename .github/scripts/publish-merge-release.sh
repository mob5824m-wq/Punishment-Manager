#!/usr/bin/env bash
# Publish the installers a merge to main just built.
#
# Called by .github/workflows/merge-release.yml after the three build jobs
# (build-installers.yml) copied the .deb, .dmg and .exe into ./artifacts. It
# maintains two prereleases:
#
#   latest-build              rolling: its tag is moved to the merge commit and
#                             its assets are replaced, so one URL always
#                             carries the newest main build
#   v<VERSION>-build.<run>    one release per merge, named from the VERSION
#                             file and the workflow run number, so a build
#                             stays downloadable after the next merge
#
# Both are prereleases, which keeps <repo>/releases/latest pointing at the
# newest versioned release (release.yml / make_release.sh) instead of at an
# unreleased build from main.
#
# Usage:
#   GITHUB_SHA=<sha> GITHUB_RUN_NUMBER=<n> \
#       .github/scripts/publish-merge-release.sh [artifacts-dir]
#
# Requires: git, gh (authenticated through GH_TOKEN) and a VERSION file.
# Written for bash 3.2 (the macOS CI job runs the tests that execute it).
set -euo pipefail

ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../.." && pwd )"
cd "$ROOT"

ARTIFACTS="${1:-artifacts}"
ROLLING_TAG="latest-build"

if [ ! -f VERSION ]; then
    echo "ERROR: no VERSION file at $ROOT; can't name this build." >&2
    exit 1
fi
VERSION="$(tr -d '[:space:]' < VERSION)"
SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
RUN_NUMBER="${GITHUB_RUN_NUMBER:-0}"
BUILD_TAG="v${VERSION}-build.${RUN_NUMBER}"
ROLLING_TITLE="Latest main build (${VERSION} build ${RUN_NUMBER})"

# Collect the installers. Explicit tests rather than a bare glob, so a missing
# platform is reported instead of being published without it.
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

# The three names this run publishes; used to spot stale assets below.
names=()
i=0
while [ "$i" -lt "$count" ]; do
    names[$i]="$(basename "${files[$i]}")"
    i=$((i + 1))
done

notes_file="$(mktemp)"
trap 'rm -f "$notes_file"' EXIT
{
    echo "Automated build of \`${SHA}\` from \`main\` (${VERSION}, workflow run ${RUN_NUMBER})."
    echo
    echo "This is an unreleased prerelease, rebuilt by CI on every merge; it has"
    echo "not been through a version bump. See the newest tagged release for a"
    echo "curated download."
    echo
    echo "Assets:"
    for name in "${names[@]}"; do
        echo "- \`${name}\`"
    done
} > "$notes_file"

echo "Publishing ${count} installer(s) as ${ROLLING_TAG} and ${BUILD_TAG}"

# --- Rolling release ------------------------------------------------------- #
# Move the tag to this merge, then create or refresh the release it points at,
# so an existing release keeps its id (and therefore its asset URLs).
if git tag -f "$ROLLING_TAG" "$SHA" && git push --force origin "refs/tags/$ROLLING_TAG"; then
    if ! gh release view "$ROLLING_TAG" >/dev/null 2>&1; then
        gh release create "$ROLLING_TAG" \
            --prerelease \
            --title "$ROLLING_TITLE" \
            --notes-file "$notes_file"
    fi
else
    # Pushing tags can be blocked (protected tags, a restricted token). Fall
    # back to replacing the release and its tag outright, so a merge still
    # publishes either way.
    echo "WARNING: could not move the ${ROLLING_TAG} tag; recreating the release." >&2
    git tag -d "$ROLLING_TAG" >/dev/null 2>&1 || true
    gh release delete "$ROLLING_TAG" --yes --cleanup-tag >/dev/null 2>&1 || true
    gh release create "$ROLLING_TAG" \
        --target "$SHA" \
        --prerelease \
        --title "$ROLLING_TITLE" \
        --notes-file "$notes_file"
fi
gh release edit "$ROLLING_TAG" --title "$ROLLING_TITLE" --notes-file "$notes_file"
gh release upload "$ROLLING_TAG" "${files[@]}" --clobber

# Drop assets left over from an earlier merge (typically a build of the
# previous version), so the rolling release only lists the newest installers.
gh release view "$ROLLING_TAG" --json assets -q '.assets[].name' | while read -r asset; do
    [ -n "$asset" ] || continue
    keep=0
    for name in "${names[@]}"; do
        if [ "$asset" = "$name" ]; then
            keep=1
        fi
    done
    if [ "$keep" -eq 0 ]; then
        echo "Removing stale asset ${asset} from ${ROLLING_TAG}"
        gh release delete-asset "$ROLLING_TAG" "$asset" --yes
    fi
done

# --- One release per merge ------------------------------------------------- #
if gh release view "$BUILD_TAG" >/dev/null 2>&1; then
    # Same run re-run: replace the assets instead of failing.
    gh release upload "$BUILD_TAG" "${files[@]}" --clobber
else
    gh release create "$BUILD_TAG" \
        --target "$SHA" \
        --prerelease \
        --title "Build ${RUN_NUMBER} (${VERSION})" \
        --notes-file "$notes_file" \
        "${files[@]}"
fi

repo_url="$(gh repo view --json url -q .url 2>/dev/null || echo "https://github.com/mob5824m-wq/Sentinel")"
echo
echo "Published:"
echo "  ${repo_url}/releases/tag/${ROLLING_TAG}    (rolling, newest main build)"
echo "  ${repo_url}/releases/tag/${BUILD_TAG}    (this merge)"
