#!/usr/bin/env bash
set -euo pipefail

VECTIS_HOME="${VECTIS_HOME:-$HOME/.local/share/vectis}"
VENV="$VECTIS_HOME/venv"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "First run: creating venv at $VENV ..." >&2
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR"
fi

exec "$VENV/bin/vectis" "$@"
