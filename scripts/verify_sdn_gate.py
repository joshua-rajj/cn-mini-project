"""End-to-end SDN gate test (needs root + a running Ryu controller).

Proves the Socket<->SDN integration claim:
  1. an ungranted client host cannot open the protected transfer port,
  2. a REST grant lets it through,
  3. revoking blocks it again,

all observed from inside the Mininet host namespaces.

Run:
    sudo env PYTHONPATH=. python3 scripts/verify_sdn_gate.py
"""

import json
import os
import shlex
import sys
import time
import urllib.request

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
PROBE = os.path.join(ROOT, "scripts", "probe.py")

SERVER_IP = "10.0.0.1"
PORT = 9000
CLIENT_IP = "10.0.0.11"
REST = "http://127.0.0.1:8080"


def policy(action, src_ip):
    body = json.dumps({"src_ip": src_ip, "dst_ip": SERVER_IP, "dport": PORT}).encode()
    req = urllib.request.Request(f"{REST}/policy/{action}", data=body,
                                 headers={"content-type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=5))


def probe(host):
    return host.cmd(f"{shlex.quote(PY)} {shlex.quote(PROBE)} {SERVER_IP} {PORT}").strip()


def dump_flows(tag):
    print(f"   --- s1 flows after {tag} (policy-relevant) ---")
    os.system("ovs-ofctl -O OpenFlow13 dump-flows s1 "
              "| grep -E 'priority=200|priority=0|cookie=0x1'")


def main():
    setLogLevel("warning")
    net = Mininet(controller=RemoteController, link=TCLink, waitConnected=True)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    server = net.addHost("server", ip=SERVER_IP)
    client = net.addHost("client1", ip=CLIENT_IP)
    switch = net.addSwitch("s1")
    net.addLink(server, switch)
    net.addLink(client, switch)
    net.start()

    server.cmd(f"cd {shlex.quote(os.path.join(ROOT, 'server'))} && "
               f"{shlex.quote(PY)} -u tcp_server.py > /tmp/opencode/gate_server.log 2>&1 &")
    time.sleep(2)

    print("=== 1. unauthorized client ===")
    print("   probe:", probe(client))
    dump_flows("unauthorized probe")

    print("=== 2. grant then probe ===")
    print("   grant:", policy("grant", CLIENT_IP))
    time.sleep(1)
    dump_flows("grant")
    print("   probe:", probe(client))

    print("=== 3. revoke then probe ===")
    print("   revoke:", policy("revoke", CLIENT_IP))
    time.sleep(1)
    dump_flows("revoke")
    print("   probe:", probe(client))

    print("=== counters ===")
    print("  ", urllib.request.urlopen(f"{REST}/policy/stats", timeout=5).read().decode())

    print("=== flows (policy-relevant) ===")
    os.system("ovs-ofctl -O OpenFlow13 dump-flows s1 | grep -E 'cookie=0x1|priority=200|priority=0'")

    net.stop()


if __name__ == "__main__":
    sys.exit(main())
