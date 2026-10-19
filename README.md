# Project 14 — Concurrent File Sharing System (SDN + Socket Programming)

A multi-user file-sharing application whose file transfers run over a **real low-level TCP
socket protocol** between hosts in a **Mininet** topology, with a **Ryu SDN controller**
installing OpenFlow rules that genuinely allow or block reachability to the protected
transfer service. Authentication is intended to drive that SDN policy.

Course: Computer Networks (UE25CS243A) — *SDN-Socket Programming* mini project, Project 14.

---

## 1. Status of this stage

| Area | State |
| --- | --- |
| Repository structure | ✅ clean layout (`server/`, `client/`, `SDN/`, `common/`, …) |
| Concurrent TCP transfer service | ✅ threaded, list/download working |
| Framed transfer protocol module | ✅ written (`common/protocol.py`), **not yet wired into the server** |
| Upload path | ⬜ not implemented yet |
| Ryu controller (L2 learning + policy) | ✅ working |
| SDN gate over the transfer port | ✅ **verified end-to-end** (see §9) |
| Flask dashboards | ✅ server + 3 client dashboards run inside Mininet |
| Authentication / metadata DB | ⬜ Phase 4 (not started) |
| Upload/download in the browser | download flow exists; upload pending |
| Tests | ⬜ placeholder dirs only |
| Performance experiments | ⬜ not started |

Evidence for the ✅ rows is in `docs/current-state.md` and reproduced in §9 below.

---

## 2. What the system does (behaviour)

1. A Mininet topology starts **one OVS switch `s1`**, **one server host** and **three client
   hosts**, all connected to `s1`, with the switch attached to the **Ryu controller**.
2. The server host runs the **TCP file-transfer service on port 9000** and a dashboard on 5000;
   each client host runs a dashboard on 5001.
3. The controller **drops** any TCP packet from a client to `server:9000` unless that client
   has an active **grant**.
4. A grant is created/removed through the controller's REST API (later, by a successful
   login/logout). Installing a grant pushes a `priority=200` allow flow; revoking removes it.
5. Client hosts can download files over the TCP protocol; the controller's flow counters show
   allowed/denied attempts.

**HTTP vs raw TCP (important distinction).** Browsers speak HTTP to the Flask dashboards.
The *file bytes* move over the **custom TCP protocol** between Mininet hosts, which is what
the OpenFlow policy controls. These are deliberately separate planes.

---

## 3. Architecture

```
        VM browser
            │  HTTP (dashboards / control)
            ▼
   ┌────────────────────┐         REST (control plane, 127.0.0.1:8080)
   │  Flask dashboards  │───────────────────────────────┐
   │  server/ app.py    │                               ▼
   │  client/ app.py    │                     ┌──────────────────────────┐
   └─────────┬──────────┘                     │  Ryu controller          │
             │ orchestration                  │  SDN/controller/ryu_app  │
             ▼                                │  + policy.py + rest.py   │
   ┌────────────────────┐                     └────────────┬─────────────┘
   │ TCP transfer svc   │  raw TCP (data plane, port 9000) │ OpenFlow 1.3
   │ server/tcp_server  │◄────────────────────────────┐    │ (6653)
   └─────────┬──────────┘                             │    ▼
             │                              ┌─────────┴──────────┐
    ┌────────┴────────┐                     │   OVS switch s1    │
    │ server 10.0.0.1 │◄───────────────────►│  (learning+policy) │
    └─────────────────┘                     └───┬─────┬─────┬────┘
                                          ┌─────┘     │     └─────┐
                                    client1        client2      client3
                                   10.0.0.11      10.0.0.12    10.0.0.13
```

---

## 4. Repository layout (every component)

```
CN PROJECT/
├── README.md                     this file
├── requirements.txt              app deps (Flask, requests)
├── requirements-controller.txt   Ryu deps + version notes
├── .env.example                  config template
├── .gitignore
│
├── server/                       ── file-server host ──────────────────────
│   ├── tcp_server.py             concurrent TCP transfer service (port 9000)
│   ├── app.py                    Flask server dashboard + control API (port 5000)
│   ├── templates/dashboard.html  server dashboard UI
│   └── storage/                  files served to clients (data dir)
│
├── client/                       ── client host ───────────────────────────
│   ├── app.py                    Flask client dashboard + heartbeat (port 5001)
│   ├── templates/dashboard.html  client dashboard UI
│   └── downloads/                received files (data dir)
│
├── SDN/                          ── Software-Defined Networking ───────────
│   ├── topology.py               Mininet topology; optional RemoteController
│   └── controller/
│       ├── ryu_app.py            Ryu app: L2 learning + protected-port policy
│       ├── policy.py             pure grant store (no Ryu imports; testable)
│       └── rest.py               REST control plane (grant/revoke/state/stats/flows)
│
├── common/                       ── shared code ───────────────────────────
│   ├── protocol.py               framed TCP protocol + helpers + error codes
│   └── config.py                 addresses, ports, limits
│
├── envs/ryu/                     Ryu Python 3.8 virtualenv (not committed)
├── docs/                         current-state.md, plan.md + course material
└── legacy/                       archived earlier prototypes (not committed)
```

### 4.1 `server/tcp_server.py`

The concurrent transfer service.

- Binds `0.0.0.0:9000`, `SO_REUSEADDR`, `listen()`, accepts in a loop, and spawns **one daemon
  thread per client** (`handle_client`).
- Current wire protocol: **one line of JSON** per request, then (for downloads) a JSON header
  line followed by raw file bytes. This is the *pre-framing* protocol; `common/protocol.py`
  is the intended replacement.
- Commands handled: `REGISTER`, `HEARTBEAT`, `LIST`, `CLIENTS`, `DOWNLOAD`.
- `list_shared_files()` returns name/size/mtime for files in `server/storage/`.
- `safe_file_path()` rejects empty/`.`/`..`, path separators and anything resolving outside
  `storage/` (path-traversal defence).
- Active clients are tracked in memory with a 30 s heartbeat timeout (`get_active_clients`).
- Per-connection `settimeout(15)`; errors are caught and logged, sockets closed in `finally`.
- Has a `__main__` guard so it can run standalone (`python tcp_server.py`).

### 4.2 `server/app.py`

Flask server dashboard.

- `GET /` renders `templates/dashboard.html`.
- `GET /api/clients` calls the TCP service over loopback (`tcp_request`) and returns active
  clients; returns HTTP 503 if the TCP service is down.
- On start, launches `serve_forever` in a daemon thread, then serves Flask on
  `SERVER_HOST_BIND` (default `0.0.0.0:5000`).

### 4.3 `server/templates/dashboard.html`

Single-file dark dashboard; polls `/api/clients` every 3 s and renders the client table.

### 4.4 `client/app.py`

Per-client Flask dashboard and transfer client.

- `GET /` renders the client dashboard.
- `GET /api/files` sends `LIST` to the server and returns the file list.
- `GET /download/<filename>` sends `DOWNLOAD`, streams the bytes to `client/downloads/`,
  then serves them with `send_file`. Filenames are reduced to a single path component;
  `..`/empty are rejected (HTTP 400).
- A background `heartbeat_loop` sends `HEARTBEAT` every 5 s so the server shows the client.
- Configured entirely by env: `FILE_SERVER_HOST/PORT`, `CLIENT_NAME`, `CLIENT_ID`,
  `CLIENT_HOST`, `CLIENT_PORT`.

### 4.5 `client/templates/dashboard.html`

Client dashboard UI (file list + download links).

### 4.6 `common/protocol.py` (framed protocol — the intended data-plane format)

Defines the robust framing the transfer path will use:

```
Frame = [4-byte big-endian header length][header JSON][optional payload of header["size"] bytes]
```

- Helpers: `recv_exact(sock, n)`, `send_header`, `recv_header`, `send_response`,
  `send_file`, `recv_file`, `sanitize_filename`.
- Safety: `MAX_HEADER_BYTES` (64 KiB) and `MAX_FILE_BYTES` caps; streaming in 64 KiB chunks
  (never loads a whole file into memory).
- Stable error codes: `BAD_FRAME`, `BAD_COMMAND`, `UNAUTHORIZED`, `NOT_FOUND`, `TOO_LARGE`,
  `BAD_NAME`, `CHECKSUM_MISMATCH`, `SIZE_MISMATCH`, `IO_ERROR`, `INTERNAL`.
- `sanitize_filename` rejects `..`, separators, absolute paths, hidden names, over-long names.

> Status: written and unit-smoke-tested; the server still uses the older newline-JSON
> protocol. Wiring it in is Phase 3.

### 4.7 `common/config.py`

Single source of truth for addressing/ports/limits: `SERVER_IP`, `CLIENT_IPS`
(`10.0.0.11–13`), `TRANSFER_PORT` (9000), `SERVER_HTTP_PORT` (5000), `CLIENT_HTTP_PORT`
(5001), `CONTROLLER_OFP_PORT` (6653), `CONTROLLER_REST`, chunk/size limits. Overridable by
environment.

### 4.8 `SDN/topology.py`

Mininet topology.

- Builds `server` (10.0.0.1) + `client1-3` (10.0.0.11–13) on switch `s1`, TCLink 10 ms.
- If `SDN_CONTROLLER_IP` is set, attaches a `RemoteController` on
  `SDN_CONTROLLER_PORT` (default 6653); otherwise runs a standalone switch.
- Launches the server and client Flask apps inside their host namespaces, logging to
  `logs/`, and prints the dashboard URLs.
- `MN_TEST=1` runs a non-interactive mode (pingall + `ovs-ofctl dump-flows`) and exits —
  used for verification.

### 4.9 `SDN/controller/ryu_app.py`

The controller application (`PolicySwitch`, a `ryu.base.app_manager.RyuApp`).

- **Learning switch:** table-miss → controller; on `PacketIn` learns `src MAC → in_port`,
  installs `priority=10` forwarding flows; floods unknown destinations.
- **Protected service:** for IPv4/TCP packets with `dst == SERVER_IP` and
  `tcp.dst_port == TRANSFER_PORT`, it consults the policy engine:
  - allowed → forward, count `allowed`;
  - not allowed → **drop**, count `denied`, log `DENY <src> -> <dst>:<port>`.
- **Policy application (from REST):**
  - `grant_policy(src,dst,dport)` registers a grant and installs two `priority=200` flows
    (forward `...tp_dst=9000` and reverse `...tp_src=9000`) tagged with the grant's
    **cookie**, actions `output=NORMAL`, optional `hard_timeout` (`GRANT_TIMEOUT`).
  - `revoke_policy(...)` deletes all flows with that cookie.
- Registers the REST controller via WSGI (`ryu.app.wsgi.WSGIApplication`).
- Maintains per-switch datapaths and a flow-stats snapshot (`EventOFPFlowStatsReply`).

Priorities: `table-miss = 0` < `learned = 10` < `allow = 200`.

### 4.10 `SDN/controller/policy.py`

`PolicyEngine` — pure Python, no Ryu imports, so it is unit-testable: `grant()` (idempotent,
returns a `Grant` with a unique cookie), `revoke()`, `is_allowed()`, `state()`. Thread-safe.

### 4.11 `SDN/controller/rest.py`

`PolicyRest(ControllerBase)` — the REST control plane:

| Method & path | Body | Effect |
| --- | --- | --- |
| `POST /policy/grant` | `{"src_ip","dst_ip","dport"}` | install allow flows; returns `applied_switches` |
| `POST /policy/revoke` | `{"src_ip","dst_ip","dport"}` | remove the grant's flows |
| `GET /policy/state` | — | current grants |
| `GET /policy/stats` | — | allowed/denied counters + connected switch ids |
| `GET /flows` | — | per-switch flow-stat snapshot |

Invalid input returns HTTP 400.

### 4.12 Running the project

There are no wrapper scripts — three commands, one per terminal:

```bash
# terminal 1 — SDN controller (OFP 6653, REST 8080)
envs/ryu/bin/ryu-manager --ofp-tcp-listen-port 6653 \
  --wsapi-host 127.0.0.1 --wsapi-port 8080 SDN/controller/ryu_app.py

# terminal 2 — topology (needs root); starts the server + client apps
sudo env SDN_CONTROLLER_IP=127.0.0.1 MN_CLIENTS=client1,client2 \
  MN_MGMT=1 MN_HOLD=1 python3 SDN/topology.py

# terminal 3 — tear down
sudo pkill -f '[t]opology.py' && sudo mn -c
```

To check the gate by hand, grant a client and watch its traffic succeed:

```bash
curl -X POST localhost:8080/policy/grant -H 'Content-Type: application/json' \
  -d '{"src_ip":"10.0.0.11","dst_ip":"10.0.0.1","dport":9000}'
curl -s localhost:8080/policy/stats
```

---

## 5. TCP protocol

### 5.1 Current (server on port 9000)

Newline-delimited JSON request; for downloads the server first sends a JSON header line then
streams the file bytes:

```
→ {"command":"DOWNLOAD","filename":"x.bin","client_id":"c001","name":"client1"}\n
← {"ok":true,"filename":"x.bin","size":512000}\n
← <512000 raw bytes>
```

Commands: `REGISTER`, `HEARTBEAT`, `LIST`, `CLIENTS`, `DOWNLOAD`.
Errors are returned as `{"ok":false,"error":"..."}`.

### 5.2 Framed protocol (target, `common/protocol.py`)

Length-prefixed header + optional payload (see §4.6). This removes the "read until newline
then assume the rest is payload" fragility and is what the transfer path will use once wired
in (Phase 3), including `UPLOAD` with size + SHA-256 verification and atomic file placement.

---

## 6. SDN policy model

- **Identity:** one Mininet client host per user → distinct source IP, so IP-based OpenFlow
  rules can actually separate users. (Limitation, to document in the viva: users sharing one
  host/IP cannot be told apart by an OpenFlow rule.)
- **Protected resource:** TCP `server:9000`.
- **Default posture:** unprotected by default — packets to `server:9000` with no grant are
  dropped in the controller.
- **Grant:** per `(src_ip → server_ip, tcp/9000)` allow flows at `priority=200`, tagged with a
  cookie; idempotent; optional hard timeout so a crash cannot leave a permanent grant.
- **Revoke:** delete-by-cookie; the port becomes unreachable again.
- **Evidence:** `ovs-ofctl dump-flows s1` shows allow flows appear/disappear; the controller
  log shows `DENY`/`ALLOW`; counters track both.

---

## 7. Network map

| Node | Role | IP | Ports |
| --- | --- | --- | --- |
| `server` | transfer service + dashboard | 10.0.0.1 | 9000 TCP, 5000 HTTP |
| `client1` | client dashboard | 10.0.0.11 | 5001 HTTP |
| `client2` | client dashboard | 10.0.0.12 | 5001 HTTP |
| `client3` | client dashboard | 10.0.0.13 | 5001 HTTP |
| `s1` | OVS switch (Ryu-controlled) | — | — |
| controller | Ryu | 127.0.0.1 | 6653 OpenFlow, 8080 REST |

---

## 8. Interpreters (why there are three processes on two Pythons)

| Piece | Python | Why |
| --- | --- | --- |
| Mininet + `SDN/topology.py` | **system 3.14** | `mininet` is an apt package bound to system Python |
| Flask apps + `common/` | **project `.venv` (3.13)** | application runtime (Flask already installed) |
| Ryu controller | **`envs/ryu` (3.8)** | Ryu is unmaintained for Python 3.10+ |

They communicate over OpenFlow and HTTP, so they do not need a shared interpreter.

---

## 9. Setup, run, and verify

### One-time setup

```bash
# application environment (reuse the existing .venv)
.venv/bin/pip install -r requirements.txt

# Ryu controller environment on Python 3.8
uv venv --python 3.8 envs/ryu
envs/ryu/bin/python -m ensurepip --upgrade
envs/ryu/bin/python -m pip install "setuptools<58" wheel
envs/ryu/bin/python -m pip install --no-build-isolation ryu==4.34
envs/ryu/bin/python -m pip install --no-build-isolation eventlet==0.30.2 greenlet==1.1.3
```

> `uv pip install ryu` fails: Ryu's PBR metadata reports version `0.0.0` while the
> distribution is `4.34`. pip tolerates the mismatch; `setuptools<58` is needed to build
> Ryu's sdist (`easy_install` API), and `eventlet==0.30.2` provides the WSGI symbol Ryu uses.

### Run

```bash
# terminal 1 — controller
envs/ryu/bin/ryu-manager --ofp-tcp-listen-port 6653 \
  --wsapi-host 127.0.0.1 --wsapi-port 8080 SDN/controller/ryu_app.py

# terminal 2 — topology with SDN (needs root)
sudo env SDN_CONTROLLER_IP=127.0.0.1 PATH="$PATH" python3 SDN/topology.py

# clean up
sudo pkill -f '[t]opology.py'; sudo mn -c
```

### Verify the SDN gate

Grant a client, then confirm the flows and counters change:

```bash
curl -X POST localhost:8080/policy/grant -H 'Content-Type: application/json' \
  -d '{"src_ip":"10.0.0.11","dst_ip":"10.0.0.1","dport":9000}'
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=200
curl -s localhost:8080/policy/stats
```

Observed result (this stage):

| Stage | Client probe to 10.0.0.1:9000 | Controller |
| --- | --- | --- |
| unauthorized | `BLOCKED TimeoutError` | `DENY 10.0.0.11 -> 10.0.0.1:9000 (no grant)`, `denied` counter increases |
| after `grant` | `OK {"ok":true,"files":[...]}` | flows `cookie=0x1001, priority=200,tcp,nw_src=10.0.0.11,nw_dst=10.0.0.1,tp_dst=9000 actions=NORMAL` (+ reverse) |
| after `revoke` | `BLOCKED TimeoutError` | those flows are gone |

Also verified: `sudo mn --test pingall` (0% dropped) and `pingall` through the Ryu-controlled
switch (12/12 received).

---

## 10. Troubleshooting

- **`sudo` needed** for `mn`, `ovs-*` and topology runs.
- **Clean state between runs:** `sudo pkill -f '[t]opology.py'; sudo mn -c`.
- **Ryu won't install:** see the `setuptools<58` / `eventlet==0.30.2` notes in §9.
- **Switch not connecting:** ensure the controller is running first and
  `SDN_CONTROLLER_IP=127.0.0.1` is exported into the sudo environment.
- **Paths with spaces:** pass through `shlex.quote` (the project directory contains a space).

---

## 11. Security notes

- Passwords must be hashed (Phase 4); never stored in plaintext.
- The controller REST API is bound to `127.0.0.1` and is intended only for the trusted
  application host — it must not be exposed to untrusted clients.
- Client-supplied filenames are never used as storage paths.
- If the controller cannot apply a policy, protected operations must **fail closed**.

---

## 12. Documentation index

- `docs/plan.md` — architecture, decisions and the phased plan.
- `docs/current-state.md` — audit + progress log with evidence.
- `docs/guidelines.txt`, `docs/mininet.txt` — original course material.
