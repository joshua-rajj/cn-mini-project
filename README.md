# Project 14 - Concurrent File Sharing System with SDN

A Mininet network with one file server and two clients. Clients upload and
download files over TCP (port 9000), and a Ryu SDN controller decides who is
allowed to reach that port.

## How it works

- `protocol.py` defines the framed messages used by the server and the client.
- `server/tcp_server.py` is a threaded TCP server. It stores files in
  `server/storage` and keeps their details in `server/metadata.csv`.
- `server/app.py` is the server dashboard (port 5000). It also starts the TCP
  server in a background thread and controls the SDN controller.
- `client/client.py` is the transfer client. It can be imported by the
  dashboard or used from the command line.
- `client/app.py` is the client dashboard (port 5001). It has a login form,
  sends a heartbeat, and uploads and downloads files.
- `SDN/controller.py` is the Ryu controller. It works as a learning switch and
  blocks port 9000 on the server until a client logs in.
- `SDN/topology.py` builds the Mininet network and starts the apps.

## Files

```
protocol.py                  shared message format

server/
  tcp_server.py              TCP file server (port 9000)
  app.py                     server dashboard (port 5000)
  templates/dashboard.html   server page (bandwidth + online clients)
  storage/                   files that clients can download
  metadata.csv               file list (created at runtime)

client/
  client.py                  transfer client and command line
  app.py                     client dashboard (port 5001)
  templates/dashboard.html   client page (login + upload + file list)
  downloads/                 downloaded files

SDN/
  controller.py              Ryu controller and control API
  topology.py                Mininet network

requirements.txt             Flask (application)
requirements-controller.txt  Ryu (controller, Python 3.8)
```

## What each file does

### `protocol.py`

This file is used by both the server and the client, so they send the same
bytes. Every message looks like this:

```
MAGIC (4 bytes) | header length (4 bytes) | JSON header | payload
```

The header always has `type` and `payload_len`. Extra fields such as `token`,
`filename` or `seq` are added when needed.

Constants:

- `MAGIC` - marks the start of a frame.
- `MAX_HEADER` - largest allowed JSON header (1 MiB).
- `MAX_PAYLOAD` - largest allowed payload (8 MiB).
- `CHUNK_SIZE` - file data is sent in 64 KiB pieces.

Functions:

- `recv_exact(sock, size)` - reads exactly `size` bytes from a socket. It keeps
  calling `recv` until it has everything and raises `ConnectionError` if the
  other side closes.
- `send_frame(sock, msg_type, payload=b"", **fields)` - builds the JSON header,
  adds the extra fields, and sends the magic, header length, header and payload.
- `recv_frame(sock)` - reads one full frame, checks the magic and the sizes, and
  returns `(header, payload)`.
- `sha256_file(path)` - reads a file in 1 MiB blocks and returns its SHA-256
  hash as a hex string.

`ProtocolError` is raised when a frame is malformed.

### `server/tcp_server.py`

This is the file server. It listens on port 9000 and starts one thread for each
client, so several transfers can run at the same time.

Settings:

- `HOST` and `PORT` - where the server listens.
- `MAX_FILE_SIZE` - largest upload (1 GiB).
- `SERVER_PASSWORD` - shared password, `pass123` by default. It can be changed
  with the `SERVER_PASSWORD` environment variable.
- `CLIENT_TIMEOUT` - a client is shown as online for 30 seconds after its last
  login.
- `STORAGE` - folder with the real files.
- `METADATA` - CSV file with one row per uploaded file.

Shared state:

- `metadata_lock` - protects the CSV and the reserved filenames.
- `reserved` - filenames that are being uploaded right now.
- `clients` - logged-in clients and when they were last seen.
- `buckets` - bytes per second, used for the bandwidth chart.
- `recent` - the last 20 finished transfers.
- `totals` - total bytes and transfer counts since the server started.

Functions:

- `start_metadata()` - creates `metadata.csv` with the column headers if it
  does not exist yet.
- `read_metadata_file()` - reads every row from the CSV. The caller must hold
  `metadata_lock`.
- `get_file(filename)` - looks up one file in the CSV and returns its row, or
  `None`.
- `list_files()` - returns all CSV rows sorted by upload time, newest first.
- `save_metadata(filename, size, owner, sha)` - appends one row with the
  filename, size, owner, UTC time and SHA-256.
- `valid_filename(name)` - returns `False` for empty names, `.`, `..`, names
  with `/` or `\`, and names with a NUL byte.
- `reserve_filename(name)` - checks the disk, the reservations and the CSV. If
  the name is free, it reserves it and returns `True`; otherwise `False`.
- `release_filename(name)` - removes a reservation when an upload finishes.
- `remember_client(username, ip)` - stores a client for the server dashboard.
- `active_clients()` - removes clients that have not logged in for 30 seconds
  and returns the rest with a readable `last_seen` time.
- `record_bytes(direction, amount)` - adds bytes to the current one-second
  bucket and to the total.
- `record_transfer(direction, username, filename, size, seconds)` - calculates
  Mbps, updates the transfer count, and puts the transfer at the front of
  `recent` (keeping only 20).
- `bandwidth_stats()` - returns the 5-second upload/download rates, the totals,
  the last 30 one-second buckets, and the recent transfers.
- `send_error(sock, code, message)` - sends an `ERROR` frame.
- `check_password(sock, address)` - reads the first frame, which must be `AUTH`.
  It hashes `SERVER_PASSWORD` and compares it with `password_sha256`. On success
  it creates a random token, sends `AUTH_OK`, records the client, and returns
  `(token, username)`.
- `do_upload(sock, header, username)` - handles `UPLOAD_INIT`. It checks the
  name and size, reserves the name, sends `OK`, receives `CHUNK` frames into a
  `.part` file, checks the size and SHA-256, renames the file, and saves the
  metadata.
- `do_download(sock, header, username)` - handles `DOWNLOAD_REQ`. It checks the
  metadata and the file, sends `DOWNLOAD_INIT` with the size and hash, sends
  `CHUNK` frames, and finishes with `DOWNLOAD_DONE`.
- `do_list(sock)` - builds a list with `filename`, `size`, `owner`,
  `upload_time` and `sha256`, and sends it as `LIST_RESP`.
- `handle_client(sock, address)` - logs the client in and then loops over
  messages: `LIST`, `UPLOAD_INIT`, `DOWNLOAD_REQ` or `QUIT`. It checks the token
  for commands that need a login.
- `serve()` - creates the metadata file, binds the socket, listens, and starts a
  daemon thread for every accepted connection.

Messages used by the server:

```
AUTH          client -> server    username, password_sha256
AUTH_OK       server -> client    token
LIST          client -> server    token
LIST_RESP     server -> client    JSON list in the payload
UPLOAD_INIT   client -> server    token, filename, size, sha256
OK            server -> client
CHUNK         both ways           file bytes, seq
UPLOAD_DONE   client -> server    sha256, chunks
UPLOAD_OK     server -> client    filename, size, sha256
DOWNLOAD_REQ  client -> server    token, filename
DOWNLOAD_INIT server -> client    filename, size, sha256
DOWNLOAD_DONE server -> client    chunks
ERROR         server -> client    code, message
QUIT          client -> server    token
```

### `server/app.py`

This is the Flask app for the server dashboard.

- `controller_call(path, ip)` - sends `{"ip": ip}` to the Ryu control API. It is
  used for `/grant` and `/revoke`.
- `home()` - renders `templates/dashboard.html`.
- `client_list()` - `GET /api/clients`, returns the output of `active_clients()`.
- `bandwidth()` - `GET /api/bandwidth`, returns the output of
  `bandwidth_stats()` plus `ok`.
- `login()` - `POST /api/login`. It checks the JSON password against
  `SERVER_PASSWORD`, then grants `request.remote_addr` on the controller.
- `logout()` - `POST /api/logout`. It revokes `request.remote_addr`.

At the bottom, it starts `serve()` in a daemon thread and runs Flask on port
5000.

### `client/client.py`

This file contains the transfer client and its command line.

`ClientError` stores an error `code` and `message` and prints them as
`code: message`.

`Client` methods:

- `connect()` - opens the TCP connection with a 10-second connect timeout and a
  30-second read timeout.
- `close()` - closes the socket and clears the token.
- `__enter__()` / `__exit__()` - allow `with Client(...) as client:`.
- `login(username, password)` - saves the username, hashes the password with
  SHA-256, and calls `auth()`.
- `auth()` - sends `AUTH`, reads the reply, raises on `ERROR`, and stores the
  token from `AUTH_OK`.
- `request(msg_type, **fields)` - sends one request with the token and reads
  one reply. If the server says `NOT_AUTHENTICATED`, it logs in again and
  retries once. Any other `ERROR` becomes a `ClientError`.
- `list_files()` - sends `LIST` and returns the decoded JSON list from
  `LIST_RESP`.
- `upload(path, name=None)` - calculates the size and SHA-256, sends
  `UPLOAD_INIT`, sends the file as `CHUNK` frames, sends `UPLOAD_DONE`, and
  waits for `UPLOAD_OK`.
- `download(filename, dest=None)` - sends `DOWNLOAD_REQ`, writes incoming
  chunks to `dest + ".part"`, checks the chunk numbers, size and SHA-256, then
  renames the file to `dest`.
- `quit()` - sends `QUIT` and closes the connection.
- `record(op, filename, size, start)` - calculates the duration and Mbps and
  keeps the result in `self.stats`.

Other functions:

- `format_stat(item)` - formats one transfer for the command line.
- `run_parallel(host, port, user, password, tasks, limit)` - starts one thread
  per file. A semaphore limits how many connections run at the same time, and a
  lock protects the result and error lists.
- `main(argv=None)` - reads the command line (`--host`, `--port`, `-u`, `-p`,
  `-j`, and `ls`, `put` or `get`) and runs one transfer or several parallel
  transfers.

### `client/app.py`

This is the Flask app for the client dashboard.

- `page(error=None)` - renders `templates/dashboard.html` with the client name,
  server address, login state and an optional error.
- `post_to_server(path, data)` - sends JSON to the server dashboard on port 5000
  and returns the JSON reply. An HTTP error status is also decoded so the
  password check can show `Wrong password.`
- `send_heartbeat()` - every 5 seconds, if the browser is logged in, it opens a
  TCP connection, logs in, and closes it. This keeps the client in the server's
  online list.
- `home()` - `GET /`, the dashboard page.
- `login()` - `POST /login`, checks the password through the server, sets
  `logged_in` and keeps the password for TCP logins.
- `logout()` - `POST /logout`, tells the server to revoke this IP, then clears
  the login state.
- `files()` - `GET /api/files`, logs in over TCP and returns `list_files()`.
- `download(name)` - `GET /download/<name>`, logs in over TCP, downloads the
  file into `downloads/`, and sends it to the browser.
- `upload()` - `POST /upload`, saves the browser upload to a temporary file,
  sends it with `Client.upload()`, and deletes the temporary file.

At the bottom, it starts the heartbeat thread and Flask on port 5001.

### `SDN/controller.py`

This is the Ryu application.

Settings:

- `SERVER_IP` - the protected server (`10.0.0.1`).
- `TRANSFER_PORT` - the protected port (`9000`).
- `REST_PORT` - the control API port (`8080`).
- Flow priorities: `TABLE_MISS` 0, `LEARNED` 10, `PROTECTED` 100, `ALLOW` 200.

Functions and classes:

- `add_flow(dp, priority, match, actions)` - sends one `OFPFlowMod` to a switch.
- `allow_client(dp, ip)` - installs two priority-200 flows: client to
  `SERVER_IP:TRANSFER_PORT`, and the replies from the server back to the client.
  Both use the `NORMAL` action.
- `remove_client(dp, ip)` - deletes those two flows.
- `ControlHandler` - the small HTTP server:
  - `reply(data, status)` - sends a JSON response.
  - `do_GET()` - `/state` returns the allowed IPs and the blocked packet count.
  - `do_POST()` - `/grant` adds an IP and installs its flows; `/revoke` removes
    the IP and deletes its flows.
- `Firewall` - the Ryu app:
  - `__init__` - starts the control API in a daemon thread.
  - `switch_connected` - installs the table-miss rule, the priority-100 rule
    that sends server port 9000 traffic to the controller, and any allow rules
    for clients that logged in earlier.
  - `packet_in` - learns MAC addresses. For traffic to `SERVER_IP:9000` it lets
    the packet through only if the source IP is in `allowed`; otherwise it
    increments `blocked`. All other traffic is forwarded like a normal learning
    switch.
  - `send_out` - sends an `OFPPacketOut` back to the switch.

### `SDN/topology.py`

- `start_app(host, folder, variables, name)` - builds the environment variables,
  log path and command, then starts `app.py` inside one Mininet host.
- `main()` - creates the controller, the server, two clients and the switch,
  adds the management host (`10.0.0.254`) so the browser can reach the
  dashboards, runs `pingAll()`, starts the apps, and stops the network on
  Ctrl-C.

### Templates

- `server/templates/dashboard.html` - the server page. It shows the upload and
  download rate cards, a canvas chart of the last 30 seconds, the last 20
  transfers, and the online clients. It calls `/api/bandwidth` every second and
  `/api/clients` every three seconds.
- `client/templates/dashboard.html` - the client page. It has the password
  form, the upload form, and the file list with download links. It calls
  `/api/files` when the page loads and every 10 seconds.

## Login and the SDN rules

The server has one shared password, set in `server/tcp_server.py` or with the
`SERVER_PASSWORD` environment variable. The default is `pass123`.

When you submit the password on a client dashboard:

1. The client app sends it to the server (`POST /api/login`).
2. The server checks the password and tells the Ryu controller to grant the
   client's IP.
3. The controller installs these rules:

```
priority 200  tcp, src=10.0.0.11, dst=10.0.0.1, dport=9000  -> NORMAL
priority 200  tcp, src=10.0.0.1, dst=10.0.0.11, sport=9000  -> NORMAL
```

Logging out tells the controller to delete them, so the client is blocked
again.

The controller runs in the root namespace, but the Flask server runs inside the
`server` host. That is why the control API listens on all interfaces and the
server calls it at `http://10.0.0.254:8080`. You can also call it by hand:

```bash
curl -X POST localhost:8080/grant  -d '{"ip":"10.0.0.11"}'
curl -X POST localhost:8080/revoke -d '{"ip":"10.0.0.11"}'
curl localhost:8080/state
```

## Setup

Two environments are needed because Ryu does not work on newer Python
versions.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

uv venv --python 3.8 envs/ryu
envs/ryu/bin/python -m ensurepip --upgrade
envs/ryu/bin/python -m pip install "setuptools<58" wheel
envs/ryu/bin/python -m pip install --no-build-isolation ryu==4.34
envs/ryu/bin/python -m pip install --no-build-isolation eventlet==0.30.2 greenlet==1.1.3
```

Mininet must already be installed (`sudo mn --test pingall`).

## Run

Open three terminals in the project folder.

```bash
envs/ryu/bin/ryu-manager SDN/controller.py
```

```bash
sudo env SDN_CONTROLLER_IP=127.0.0.1 python3 SDN/topology.py
```

Stop with Ctrl-C in the topology terminal, then run `sudo mn -c`.

The applications run inside the Mininet hosts. If the Ryu command says
`No such file or directory`, check that you are in the project folder and that
`envs/ryu` exists (it is not stored in git).

## Dashboards

- server: <http://10.0.0.1:5000>
- client1: <http://10.0.0.11:5001>
- client2: <http://10.0.0.12:5001>

The management interface `10.0.0.254` is added by the topology so the browser
can reach these addresses.

The server dashboard shows live upload and download rates, total bytes and
transfer counts, a 30-second chart, the last 20 transfers and the online
clients.

## Using the dashboards

1. Open a client dashboard, for example <http://10.0.0.11:5001>.
2. Log in with the server password (`pass123` by default).
3. Upload a file or download one from the list.
4. Press **Log out** to revoke access.

The client is online while its heartbeat can reach the server, which means it
is logged in.

## Command line client

Run it from inside a Mininet host, or from the management interface after
granting its IP:

```bash
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 ls
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 put file.bin
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 get file.bin
```

Use `-j` for parallel transfers:

```bash
.venv/bin/python client/client.py --host 10.0.0.1 -u alice -p pass123 -j 4 put a.bin b.bin c.bin
```

## Notes

- Uploads are written to a `.part` file first and renamed when complete.
- The server checks the SHA-256 before publishing an upload, and the client
  checks the size and SHA-256 after a download.
- A filename is reserved while it is being uploaded, so two clients cannot
  write the same name at once.
- Bandwidth counters live in memory and start again from zero when the server
  restarts.
- The control API on port 8080 is reachable from inside the Mininet network.
  That is fine for a class project, but in a real system it should be locked
  down.
