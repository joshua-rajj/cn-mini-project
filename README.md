# Project 14 - Concurrent File Sharing System with SDN

A Mininet network with one file server and two clients. Clients download files
from the server over TCP sockets (port 9000), and a Ryu SDN controller decides
who is allowed to reach that port.

## How it works

- `server/tcp_server.py` is a normal TCP socket server. It spawns one thread
  per client, so several clients can download at the same time.
- `server/app.py` is the server dashboard (port 5000) and also starts the TCP
  server in a background thread.
- `client/app.py` is the client dashboard (port 5001). It sends a heartbeat
  every 5 seconds and downloads files through the TCP socket.
- `SDN/controller.py` is the Ryu controller. It works as a learning switch and
  also blocks traffic going to `10.0.0.1:9000` unless the client has logged in.
- `SDN/topology.py` builds the Mininet network and starts the apps inside each
  host.

### Login and the SDN rules

The controller starts by sending all traffic for `10.0.0.1:9000` to itself.
If the client is not logged in, the packet is dropped. When the client logs in,
the controller adds an OpenFlow rule that lets that client's IP through:

```
priority 200  tcp, src=10.0.0.11, dst=10.0.0.1, dport=9000  -> NORMAL
priority 200  tcp, src=10.0.0.1, dst=10.0.0.11, sport=9000  -> NORMAL
```

Logging out deletes those rules, so the client is blocked again.

The Flask server controls the controller with a small HTTP API on
`127.0.0.1:8080`:

```bash
curl -X POST localhost:8080/grant  -d '{"ip":"10.0.0.11"}'
curl -X POST localhost:8080/revoke -d '{"ip":"10.0.0.11"}'
curl localhost:8080/state
```

## Files

```
server/
  tcp_server.py          TCP file server (port 9000)
  app.py                 server dashboard (port 5000)
  templates/             dashboard page
  storage/               files that clients can download

client/
  app.py                 client dashboard (port 5001)
  templates/             dashboard page
  downloads/             downloaded files

SDN/
  controller.py          Ryu controller (learning switch + login check)
  topology.py            Mininet network

requirements.txt           Flask (application)
requirements-controller.txt Ryu (controller, Python 3.8)
```

## Setup

Two environments are needed because Ryu does not work on newer Python
versions.

```bash
# application environment (Flask)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# controller environment (Ryu on Python 3.8)
uv venv --python 3.8 envs/ryu
envs/ryu/bin/python -m ensurepip --upgrade
envs/ryu/bin/python -m pip install "setuptools<58" wheel
envs/ryu/bin/python -m pip install --no-build-isolation ryu==4.34
envs/ryu/bin/python -m pip install --no-build-isolation eventlet==0.30.2 greenlet==1.1.3
```

Mininet must already be installed on the system (`sudo mn --test pingall`).

## Run

Open three terminals in the project folder.

```bash
# 1. controller
envs/ryu/bin/ryu-manager SDN/controller.py

# 2. network + apps (needs root)
sudo env SDN_CONTROLLER_IP=127.0.0.1 python3 SDN/topology.py

# 3. stop everything
#    press Ctrl-C in terminal 2, then:
sudo mn -c
```

Dashboards:

- server: <http://10.0.0.1:5000>
- client1: <http://10.0.0.11:5001>
- client2: <http://10.0.0.12:5001>

The topology adds a management interface (`10.0.0.254`) in the real machine,
so the browser can reach those `10.0.0.x` addresses.

## Trying the login gate

With the network running, a client cannot list or download files yet, because
all traffic to port 9000 is blocked. Allow it from the controller:

```bash
curl -X POST localhost:8080/grant -d '{"ip":"10.0.0.11"}'
```

Reload the client dashboard and the files appear. To block it again:

```bash
curl -X POST localhost:8080/revoke -d '{"ip":"10.0.0.11"}'
```

`curl localhost:8080/state` shows which IPs are allowed and how many packets
were blocked.

## Not done yet

The dashboards do not have a login form, so the grant/revoke calls are made by
hand with `curl` for now. Uploads are also not implemented yet - clients can
only download files that are placed in `server/storage/`.
