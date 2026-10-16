"""Central configuration: addresses, ports and limits.

Environment overrides let the same code run outside Mininet (127.0.0.1) and
inside the Mininet topology (10.0.0.x).
"""

import os

# --- Mininet addressing (documented, fixed) ---
SERVER_IP = os.environ.get("SERVER_IP", "10.0.0.1")
CLIENT_IPS = {
    "client1": "10.0.0.11",
    "client2": "10.0.0.12",
    "client3": "10.0.0.13",
}

# --- ports ---
TRANSFER_PORT = int(os.environ.get("TRANSFER_PORT", "9000"))      # protected TCP service
SERVER_HTTP_PORT = int(os.environ.get("SERVER_HTTP_PORT", "5000"))
CLIENT_HTTP_PORT = int(os.environ.get("CLIENT_HTTP_PORT", "5001"))

# --- SDN controller ---
CONTROLLER_OFP_PORT = int(os.environ.get("CONTROLLER_OFP_PORT", "6653"))
CONTROLLER_REST = os.environ.get("CONTROLLER_REST", "http://127.0.0.1:8080")

# --- limits ---
CHUNK_SIZE = 64 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
