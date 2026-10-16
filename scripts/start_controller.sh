#!/usr/bin/env bash
# Start the Ryu SDN controller from the dedicated Python 3.8 environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RYU_MANAGER="${RYU_MANAGER:-$ROOT/envs/ryu/bin/ryu-manager}"

if [[ ! -x "$RYU_MANAGER" ]]; then
  echo "error: $RYU_MANAGER not found. Create the Ryu env first (see README)." >&2
  exit 1
fi

export SERVER_IP="${SERVER_IP:-10.0.0.1}"
export TRANSFER_PORT="${TRANSFER_PORT:-9000}"

exec "$RYU_MANAGER" \
  --ofp-tcp-listen-port "${CONTROLLER_OFP_PORT:-6653}" \
  "$ROOT/SDN/controller/ryu_app.py"
