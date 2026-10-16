"""Mininet topology for Project 14 -- 1 server + 3 clients behind one switch.

Run with sudo:
    sudo python3 topology.py

Each host is its own network namespace, so the three clients can all bind
port 5001 without colliding -- they only clash when run on the host's own
network stack. Use CLIENT_PORT only if you ever need two clients per namespace.
"""

import os
import time

from mininet.net import Mininet
from mininet.node import Host
from mininet.log import setLogLevel
from mininet.link import TCLink

SDN_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SDN_DIR)
PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
SERVER_DIR = os.path.join(PROJECT_ROOT, "server")
CLIENT_DIR = os.path.join(PROJECT_ROOT, "client")

# Set SDN_CONTROLLER_IP to attach s1 to a running Ryu controller, e.g.
#   SDN_CONTROLLER_IP=127.0.0.1 sudo -E python3 SDN/topology.py
CONTROLLER_IP = os.environ.get("SDN_CONTROLLER_IP")
CONTROLLER_PORT = int(os.environ.get("SDN_CONTROLLER_PORT", "6653"))

SERVER_IP = "10.0.0.1"
CLIENT_IPS = {"client1": "10.0.0.11", "client2": "10.0.0.12", "client3": "10.0.0.13"}


def start_app(node, workdir, env, tag):
    """Launch the Flask app inside a Mininet host, logging to files."""
    os.makedirs(LOG_DIR, exist_ok=True)
    env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
    log = os.path.join(LOG_DIR, f"{tag}.log")
    cmd = (f"cd {workdir} && mkdir -p {LOG_DIR} && "
           f"{env_prefix} {PYTHON} -u app.py > {log} 2>&1 &")
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
    clients = {name: net.addHost(name, ip=ip)
               for name, ip in CLIENT_IPS.items()}

    switch = net.addSwitch("s1")
    for node in [server, *clients.values()]:
        net.addLink(node, switch, delay="10ms")

    net.start()
    print("\n=== network up ===")
    net.pingAll()

    # Server: its own dashboard on :5000, TCP on :9000.
    start_app(server, SERVER_DIR,
              {"SERVER_HOST_BIND": "0.0.0.0"}, "server")

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
    print("\n=== logs ===")
    print(f"  {LOG_DIR}")

    test_mode = os.environ.get("MN_TEST")
    if test_mode:
        print("\n=== test mode: pingall + flow dump ===")
        net.pingAll()
        os.system("ovs-ofctl -O OpenFlow13 dump-flows s1")
        time.sleep(2)
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