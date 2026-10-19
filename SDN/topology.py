"""Mininet topology for Project 14 -- 1 server + N clients behind one switch.

Run with sudo:
    sudo python3 topology.py

Environment
-----------
MN_CLIENTS          comma list of client names (default client1,client2,client3);
                    IPs are assigned 10.0.0.11, .12, ... in order
MN_MGMT=1           add a management interface in the root namespace so the VM's
                    browser can reach the Mininet host IPs (10.0.0.254/24)
MN_HOLD=1           run the network without the interactive CLI (for background use)
MN_TEST=1           pingall + dump flows, then exit
SDN_CONTROLLER_IP   attach s1 to a running Ryu controller, e.g. 127.0.0.1

Each host is its own network namespace, so every client can bind port 5001
without colliding.
"""

import os
import shlex
import signal
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet

SDN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SDN_DIR)
PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
SERVER_DIR = os.path.join(PROJECT_ROOT, "server")
CLIENT_DIR = os.path.join(PROJECT_ROOT, "client")

CONTROLLER_IP = os.environ.get("SDN_CONTROLLER_IP")
CONTROLLER_PORT = int(os.environ.get("SDN_CONTROLLER_PORT", "6653"))

SERVER_IP = "10.0.0.1"
CLIENT_BASE = 11
CLIENT_NAMES = [n.strip() for n in
                os.environ.get("MN_CLIENTS", "client1,client2,client3").split(",")
                if n.strip()]
CLIENT_IPS = {name: f"10.0.0.{CLIENT_BASE + i}" for i, name in enumerate(CLIENT_NAMES)}

MGMT_IP = os.environ.get("MN_MGMT_IP", "10.0.0.254")
USE_MGMT = os.environ.get("MN_MGMT", "1").lower() not in ("0", "false", "no")


def start_app(node, workdir, env, tag):
    """Launch the Flask app inside a Mininet host, logging to a file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    env_prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    log = os.path.join(LOG_DIR, f"{tag}.log")
    cmd = (f"cd {shlex.quote(workdir)} && "
           f"{env_prefix} {shlex.quote(PYTHON)} -u app.py > {shlex.quote(log)} 2>&1 &")
    node.cmd(cmd)
    print(f"  [{tag}] started, log -> {log}")


def main():
    setLogLevel("info")

    if CONTROLLER_IP:
        from mininet.node import RemoteController
        net = Mininet(controller=RemoteController, link=TCLink, waitConnected=True)
        net.addController("c0", controller=RemoteController,
                          ip=CONTROLLER_IP, port=CONTROLLER_PORT)
        print(f"[SDN] s1 -> Ryu controller {CONTROLLER_IP}:{CONTROLLER_PORT}")
    else:
        net = Mininet(controller=None, link=TCLink, waitConnected=False)
        print("[SDN] no controller (standalone switch); set SDN_CONTROLLER_IP to attach Ryu")

    server = net.addHost("server", ip=SERVER_IP)
    clients = {name: net.addHost(name, ip=ip) for name, ip in CLIENT_IPS.items()}

    switch = net.addSwitch("s1")
    for node in [server, *clients.values()]:
        net.addLink(node, switch, delay="10ms")

    if USE_MGMT:
        # Lives in the root namespace, giving the VM a route into 10.0.0.0/24.
        mgmt = net.addHost("mgmt", ip=f"{MGMT_IP}/24", inNamespace=False)
        net.addLink(mgmt, switch)

    net.start()
    print("\n=== network up ===")
    net.pingAll()

    # Server: its own dashboard on :5000, TCP on :9000.
    start_app(server, SERVER_DIR, {"SERVER_HOST_BIND": "0.0.0.0"}, "server")
    time.sleep(3)  # let the TCP server bind before clients dial in

    for i, (name, node) in enumerate(clients.items(), start=1):
        start_app(node, CLIENT_DIR,
                  {"FILE_SERVER_HOST": SERVER_IP,
                   "FILE_SERVER_PORT": "9000",
                   "CLIENT_NAME": name,
                   "CLIENT_ID": f"c{i:03d}",
                   "CLIENT_HOST": "0.0.0.0"},
                  name)
    time.sleep(4)

    print("\n=== dashboards ===")
    print(f"  server    http://{SERVER_IP}:5000")
    for name, ip in CLIENT_IPS.items():
        print(f"  {name:<9} http://{ip}:5001")
    print(f"\n=== logs ===\n  {LOG_DIR}")

    if os.environ.get("MN_TEST"):
        print("\n=== test mode: pingall + flow dump ===")
        net.pingAll()
        os.system("ovs-ofctl -O OpenFlow13 dump-flows s1")
        time.sleep(2)
        net.stop()
        return

    if os.environ.get("MN_HOLD"):
        print("\n[hold] topology up; SIGTERM/Ctrl-C to stop\n", flush=True)
        stop = {"flag": False}

        def _handler(signum, frame):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
        try:
            while not stop["flag"]:
                time.sleep(1)
        finally:
            net.stop()
        return

    print("\nCtrl-C to tear down, or run:  sudo mn -c\n")
    try:
        net.interactive()
    except KeyboardInterrupt:
        pass
    finally:
        net.stop()


if __name__ == "__main__":
    main()
