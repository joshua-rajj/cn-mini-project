"""One-shot connectivity probe used by the SDN gate test.

Runs *inside* a Mininet host namespace, so its source IP is the host's IP.
Exits 0 with "OK ..." if it can complete a LIST exchange with the transfer
service, or prints "BLOCKED ..." if the controller drops the SYN.
"""

import json
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

try:
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall((json.dumps({"command": "LIST", "client_id": "probe",
                                  "name": "probe"}) + "\n").encode())
        reader = sock.makefile("rb")
        line = reader.readline().decode().strip()
        print("OK", line[:80])
except (OSError, socket.timeout) as exc:
    print("BLOCKED", type(exc).__name__)
