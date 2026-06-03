#!/usr/bin/env bash
# IRIS TUI Launcher

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Ensure virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "\033[1;35m[IRIS]\033[0m Initializing environment..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
else
    source .venv/bin/activate
fi

# Launch the app
exec python3 iris.py "$@"
