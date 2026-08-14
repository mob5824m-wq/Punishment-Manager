#!/usr/bin/env bash
# Print the contents of every GitHub Actions workflow file with a
# clear header, so you can copy-paste them into the GitHub web UI
# without having to open each file individually.
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
# Copy each section into the GitHub "Create new file" editor at
# https://github.com/mob5824m-wq/Punishment-Manager (one file per
# commit). See .github/SETUP_WORKFLOWS.md for full instructions.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
WF_DIR="$PROJECT_ROOT/.github/workflows"

if [ ! -d "$WF_DIR" ]; then
    echo "ERROR: $WF_DIR does not exist. Are you in the Punishment-Manager repo?" >&2
    exit 1
fi

files=(
    "release.yml"
    "build.yml"
    "install-nsis.ps1"
)

for f in "${files[@]}"; do
    path="$WF_DIR/$f"
    if [ ! -f "$path" ]; then
        echo "WARNING: missing $path, skipping." >&2
        continue
    fi
    sep="============================================================"
    echo
    echo "$sep"
    echo "FILE: .github/workflows/$f"
    echo "Lines: $(wc -l < "$path")"
    echo "$sep"
    echo
    cat "$path"
    echo
done
