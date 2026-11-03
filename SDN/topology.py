import os
import time

from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import RemoteController

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
LOGS = os.path.join(ROOT, "logs")
CONTROLLER_IP = os.environ.get("SDN_CONTROLLER_IP", "127.0.0.1")


def start_app(host, folder, variables, name):
    if not os.path.exists(LOGS):
        os.makedirs(LOGS)

    env = ""
    for key in variables:
        env += key + "=" + variables[key] + " "

    script = os.path.join(folder, "app.py")
    log = os.path.join(LOGS, name + ".log")
    command = "%s'%s' -u '%s' > '%s' 2>&1 &" % (env, PYTHON, script, log)
    host.cmd(command)
    print("started", name, "log:", log)


def main():
    setLogLevel("info")

    net = Mininet(controller=RemoteController)
    net.addController("c0", ip=CONTROLLER_IP, port=6653)

    server = net.addHost("server", ip="10.0.0.1")
    client1 = net.addHost("client1", ip="10.0.0.11")
    client2 = net.addHost("client2", ip="10.0.0.12")

    switch = net.addSwitch("s1")
    net.addLink(server, switch, cls=TCLink, bw=100)
    for host in (client1, client2):
        net.addLink(host, switch)

    mgmt = net.addHost("mgmt", ip="10.0.0.254/24", inNamespace=False)
    net.addLink(mgmt, switch)

    net.start()
    net.pingAll()

    start_app(server, os.path.join(ROOT, "server"), {}, "server")
    time.sleep(3)

    start_app(client1, os.path.join(ROOT, "client"),
              {"FILE_SERVER_HOST": "10.0.0.1",
               "FILE_SERVER_PORT": "9000",
               "CLIENT_NAME": "client1",
               "CLIENT_ID": "c1"}, "client1")
    start_app(client2, os.path.join(ROOT, "client"),
              {"FILE_SERVER_HOST": "10.0.0.1",
               "FILE_SERVER_PORT": "9000",
               "CLIENT_NAME": "client2",
               "CLIENT_ID": "c2"}, "client2")
    time.sleep(3)

    print("server:  http://10.0.0.1:5000")
    print("client1: http://10.0.0.11:5001")
    print("client2: http://10.0.0.12:5001")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        net.stop()


if __name__ == "__main__":
    main()
