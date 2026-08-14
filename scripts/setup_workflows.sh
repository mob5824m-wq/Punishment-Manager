#!/usr/bin/env bash
# Print the contents of every GitHub Actions workflow file with a
# clear header, so you can copy-paste them into the GitHub web UI
# if you ever need to recreate them on a fresh fork.
#
# Usage:
#   ./scripts/setup_workflows.sh
#   ./scripts/setup_workflows.sh > workflows.txt    # capture to a file
#
# The output is a series of sections, each looking like:
#
#   ============================================================
#   FILE: .github/workflows/release.yml
#   ============================================================
#   <contents>
#
#   ============================================================
#   FILE: .github/workflows/build.yml
#   ============================================================
#   <contents>
#
#   ...
#
# The three files that matter:
#   - .github/workflows/release.yml       (release builds on v* tags)
#   - .github/workflows/build.yml         (CI sanity build on push/PR)
#   - .github/scripts/install-nsis.ps1    (NSIS install helper for Win)
#
# Plus the helper scripts under .github/scripts/ which the workflows
# call into. To regenerate everything, run this script and follow the
# upload instructions in .github/SETUP_WORKFLOWS.md.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"

if [ ! -d "$PROJECT_ROOT/.github" ]; then
    echo "ERROR: $PROJECT_ROOT/.github does not exist. Are you in the Punishment-Manager repo?" >&2
    exit 1
fi

# Print the workflow files, then the helper scripts. These are the
# files that the workflows depend on.
files=(
    ".github/workflows/release.yml"
    ".github/workflows/build.yml"
    ".github/scripts/install-nsis.ps1"
    ".github/scripts/install-linux-deps.sh"
    ".github/scripts/install-windows-deps.ps1"
)

for rel in "${files[@]}"; do
    path="$PROJECT_ROOT/$rel"
    if [ ! -f "$path" ]; then
        echo "WARNING: missing $path, skipping." >&2
        continue
    fi
    sep="============================================================"
    echo
    echo "$sep"
    echo "FILE: $rel"
    echo "Lines: $(wc -l < "$path")"
    echo "$sep"
    echo
    cat "$path"
    echo
done
