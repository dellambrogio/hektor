#!/bin/bash
# Run a developer tool with the app's code and venv, e.g. dev/run.sh monitor (dev/run.sh -h lists them)
set -e
cd "$(dirname "$0")/.."
[[ -x app/.venv/bin/python ]] || app/run.sh --help >/dev/null  # makes the venv
PYTHONPATH=app exec app/.venv/bin/python -m dev "$@"
