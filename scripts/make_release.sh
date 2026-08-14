#!/usr/bin/env bash
# Tag a release and push it, which triggers the release.yml workflow
# to build installers and attach them to a GitHub Release.
#
# Usage:
#   ./scripts/make_release.sh 1.0.0
#   ./scripts/make_release.sh 1.1.0-rc1   # prerelease
#
# Requires: git, gh (authenticated).
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"

if [ $# -ne 1 ]; then
    echo "Usage: $0 VERSION  (e.g. 1.0.0 or 1.0.0-rc1)"
    exit 1
fi

VERSION="$1"
TAG="v${VERSION}"

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
    echo "ERROR: '$VERSION' doesn't look like a semver tag (e.g. 1.0.0, 1.0.0-rc1)."
    exit 1
fi

# Make sure the tree is clean.
if ! git diff --quiet HEAD; then
    echo "ERROR: working tree has uncommitted changes. Commit or stash first."
    exit 1
fi
if ! git diff --cached --quiet HEAD; then
    echo "ERROR: index has staged but uncommitted changes. Commit first."
    exit 1
fi

# Make sure we're on main (or a known release branch).
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
    echo "WARNING: you're on branch '$BRANCH', not 'main'."
    if ! gh_input "Continue anyway?" N; then
        exit 1
    fi
fi

# Confirm the tag doesn't already exist.
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag $TAG already exists. Delete it first or pick a new version."
    exit 1
fi

echo "==> Cutting release $TAG on $BRANCH"
git tag -a "$TAG" -m "Release $VERSION"
git push origin "$TAG"

echo
echo "==> Done. Watch the release workflow at:"
echo "    $(gh repo view --json url -q .url)/actions/workflows/release.yml"
echo
echo "When the workflow finishes, the release will be at:"
echo "    $(gh repo view --json url -q .url)/releases/tag/$TAG"
