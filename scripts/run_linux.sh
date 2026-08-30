#!/usr/bin/env bash
# Launcher for Linux.
# On first run (or whenever the bot token is missing), this will launch
# the interactive installer which writes to config.json.
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

# If config.json has no bot_token and DISCORD_TOKEN isn't set, run the
# installer so the user can fill in the values interactively.
if [ -z "${DISCORD_TOKEN:-}" ]; then
  if ! python -c "import json,sys;cfg=json.load(open('config.json'));sys.exit(0 if (cfg.get('bot_token') or cfg.get('token')) else 1)" 2>/dev/null; then
    echo
    echo "No bot token found in config.json. Starting installer ..."
    echo
    python installer.py || {
      echo "Installer exited without saving a config. Aborting."
      exit 1
    }
  fi
fi

echo "Starting bot ..."
exec python bot.py
