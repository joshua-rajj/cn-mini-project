#!/usr/bin/env bash
# Run the server + one client Flask app outside Mininet (local development).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
export PYTHONPATH="$ROOT"

(cd "$ROOT/server" && SERVER_HOST_BIND=127.0.0.1 "$PY" app.py) &
(cd "$ROOT/client" && FILE_SERVER_HOST=127.0.0.1 "$PY" app.py) &
wait
