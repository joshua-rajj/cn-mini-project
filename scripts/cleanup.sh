#!/usr/bin/env bash
# Tear down stray processes and Mininet/OVS state between runs.
set -uo pipefail

echo "[*] stopping app/controller processes"
pkill -f "ryu-manager" 2>/dev/null || true
pkill -f "server/app.py" 2>/dev/null || true
pkill -f "client/app.py" 2>/dev/null || true

echo "[*] cleaning Mininet state (needs sudo)"
sudo mn -c
