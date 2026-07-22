#!/usr/bin/env bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

cd "$DIR"

# Use project virtual environment if available
if [ -d "$DIR/.venv" ]; then
    # shellcheck source=/dev/null
    source "$DIR/.venv/bin/activate"
fi

exec python3 "$DIR/iris.py" tui "$@"
