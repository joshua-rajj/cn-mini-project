"""Mininet topology: one file server and two clients behind one switch.

Start the Ryu controller first, then run this with sudo:

    sudo env SDN_CONTROLLER_IP=127.0.0.1 python3 SDN/topology.py

Addresses:
    server   10.0.0.1    dashboard on :5000, file service on :9000
    client1  10.0.0.11   dashboard on :5001
    client2  10.0.0.12   dashboard on :5001

Press Ctrl-C to stop everything.
"""

import os
import time

from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
LOGS = os.path.join(ROOT, "logs")

CONTROLLER_IP = os.environ.get("SDN_CONTROLLER_IP", "127.0.0.1")


def start_app(host, folder, variables, name):
    """Start a Flask app inside one Mininet host."""
    os.makedirs(LOGS, exist_ok=True)
    env = " ".join("%s=%s" % (key, value) for key, value in variables.items())
    log = os.path.join(LOGS, name + ".log")
    script = os.path.join(folder, "app.py")
    command = "%s '%s' -u '%s' > '%s' 2>&1 &" % (env, PYTHON, script, log)
    host.cmd(command)
    print("  started %s (log: %s)" % (name, log))


def main():
    setLogLevel("info")

    net = Mininet(controller=RemoteController)
    net.addController("c0", ip=CONTROLLER_IP, port=6653)

    server = net.addHost("server", ip="10.0.0.1")
    client1 = net.addHost("client1", ip="10.0.0.11")
    client2 = net.addHost("client2", ip="10.0.0.12")

    switch = net.addSwitch("s1")
    for host in (server, client1, client2):
        net.addLink(host, switch)

    # This interface lives in the real machine, not in a Mininet namespace.
    # It gives the browser a route into 10.0.0.0/24 so the dashboards open.
    mgmt = net.addHost("mgmt", ip="10.0.0.254/24", inNamespace=False)
    net.addLink(mgmt, switch)

    net.start()
    net.pingAll()

    start_app(server, os.path.join(ROOT, "server"), {}, "server")
    time.sleep(3)

    start_app(client1, os.path.join(ROOT, "client"),
              {"FILE_SERVER_HOST": "10.0.0.1", "FILE_SERVER_PORT": "9000",
               "CLIENT_NAME": "client1", "CLIENT_ID": "c1",
               "CLIENT_HOST": "0.0.0.0"}, "client1")
    start_app(client2, os.path.join(ROOT, "client"),
              {"FILE_SERVER_HOST": "10.0.0.1", "FILE_SERVER_PORT": "9000",
               "CLIENT_NAME": "client2", "CLIENT_ID": "c2",
               "CLIENT_HOST": "0.0.0.0"}, "client2")
    time.sleep(3)

    print("\nDashboards:")
    print("  server   http://10.0.0.1:5000")
    print("  client1  http://10.0.0.11:5001")
    print("  client2  http://10.0.0.12:5001")
    print("\nCtrl-C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        net.stop()


if __name__ == "__main__":
    main()
