# Project 14 - Concurrent File Sharing System with SDN

A Mininet network with one file server and two clients. Clients upload and
download files over TCP (port 9000), and a Ryu SDN controller decides who is
allowed to reach that port.

## How it works

- `protocol.py` is the shared wire protocol: a 4-byte magic marker, a JSON
  header and an optional binary payload. File data travels in 64 KiB `CHUNK`
  frames.
- `server/tcp_server.py` is a normal TCP socket server. It spawns one thread
  per client, so several clients can transfer files at the same time. File
  details are kept in `server/metadata.csv`.
- `server/app.py` is the server dashboard (port 5000) and also starts the TCP
  server in a background thread.
- `client/client.py` is the transfer client (also usable from the command
  line). It logs in, lists files, uploads and downloads, and checks SHA-256.
- `client/app.py` is the client dashboard (port 5001). It has a login form,
  logs in over TCP every 5 seconds as a heartbeat, and can upload and download
  files.
- `SDN/controller.py` is the Ryu controller. It works as a learning switch and
  also blocks traffic going to `10.0.0.1:9000` unless the client has logged in.
- `SDN/topology.py` builds the Mininet network and starts the apps inside each
  host.

### Login and the SDN rules

The server has one shared password, set in `server/tcp_server.py` (or with the
`SERVER_PASSWORD` environment variable). The default is `pass123`.

The login form is on the client dashboard. When you submit the password:

1. The client app forwards it to the server (`POST /api/login`).
2. The server checks the password. If it is right, it tells the Ryu controller
   to grant the client's IP address.
3. The controller installs an OpenFlow rule for that IP:

```
priority 200  tcp, src=10.0.0.11, dst=10.0.0.1, dport=9000  -> NORMAL
priority 200  tcp, src=10.0.0.1, dst=10.0.0.11, sport=9000  -> NORMAL
```

Now that client's uploads, downloads and heartbeats are allowed.

Logging out tells the controller to delete those rules, so the client is
blocked again.

The Ryu controller runs in the root namespace, but the Flask server runs inside
the `server` host, which has its own loopback. That is why the controller's
control API listens on all interfaces and the server calls it at
`http://10.0.0.254:8080` (the management interface from `topology.py`). You can
also call it by hand from the real machine:

```bash
curl -X POST localhost:8080/grant  -d '{"ip":"10.0.0.11"}'
curl -X POST localhost:8080/revoke -d '{"ip":"10.0.0.11"}'
curl localhost:8080/state
```

## Files

```
protocol.py                shared framed protocol (magic + JSON + payload)

server/
  tcp_server.py          TCP file server (port 9000)
  app.py                 server dashboard (port 5000)
  templates/             dashboard page
  storage/               files that clients can download
  metadata.csv           file list (created at runtime)

client/
  client.py              transfer client + command line
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

## Using the dashboards

1. Open a client dashboard, for example <http://10.0.0.11:5001>.
2. Log in with the server password (`pass123` by default). You can change it
   by starting the server with `SERVER_PASSWORD=mypass`.
3. After logging in you can upload a file and download any file from the list.
4. Click **Log out** to revoke access. The files list will stop working because
   the controller blocks port 9000 again.

## Command line client

`client/client.py` is the same client without the browser. Run it from inside a
Mininet host, or from the management interface after granting its IP:

```bash
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 ls
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 put file.bin
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 get file.bin
```

Use `-j` to transfer several files at the same time:

```bash
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 -j 4 put a.bin b.bin c.bin
```

The server dashboard at <http://10.0.0.1:5000> shows:

- live upload and download rates (a 5-second average)
- total bytes and transfer counts since the server started
- a chart of the last 30 seconds
- the last 20 transfers, with client, file, size and speed
- which clients are online (a client is online while its heartbeat is getting
  through, which means it is logged in)

`curl localhost:8080/state` shows which IPs are allowed and how many packets
were blocked.

## Notes

- Uploads are written to a `.part` file first and renamed when complete, so a
  half-finished upload never appears in the file list.
- The server checks the SHA-256 sent by the client before publishing an upload,
  and the client checks the hash again after a download.
- A filename is reserved while it is being uploaded, so two clients cannot
  write the same name at once.
- Bandwidth counters live in memory and start again from zero when the server
  restarts.
- The control API on port 8080 is reachable from inside the Mininet network.
  That is fine for a class project, but in a real system it should be locked
  down.
