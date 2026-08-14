#!/usr/bin/env bash
# Cross-platform-friendly launcher for Linux.
# Creates a venv on first run and installs requirements.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR/.."

PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment in $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "Installing dependencies ..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

mkdir -p data
echo "Starting bot ..."
exec python bot.py
