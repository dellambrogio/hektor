#!/bin/bash
# Run Hektor from its venv, creating it on first run. Arguments go to the app, e.g. ./run.sh --window
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
    .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m hektor "$@"
