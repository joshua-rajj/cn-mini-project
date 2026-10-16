# Current State — Phase 0 Audit

> Project 14 — Concurrent File Sharing System (CN mini project, SDN + socket programming).
> This file records **what actually exists** before any changes. Update it at the end of
> every phase. Do not mark anything "done" here unless it has been run and observed.

Last updated: 2026-10-16 (Phase 1 in progress — repository restructured, Ryu controller running)

## 1. Environment (verified)

| Item | Value | Notes |
| --- | --- | --- |
| OS | Ubuntu **26.04.1 LTS** (Resolute Raccoon), aarch64 | brand new; repo/mirror packages may be thin |
| Kernel | 7.0.0-38-generic | |
| System Python | **3.14.3** (`/usr/bin/python3.14`) | only system interpreter present |
| Mininet | **2.3.0** apt package, module at `/usr/lib/python3/dist-packages/mininet` | bound to system Python 3.14 |
| Open vSwitch | **3.7.1** (`ovs-vsctl`), `openvswitch-switch` service **active**, kernel module loaded | datapath available |
| Ryu | **not installed**, no apt package on 26.04 | must be pip-installed into a dedicated old-Python venv |
| OS-Ken | 4.2.2 in `/home/josh/osken-venv` | maintained Ryu fork; not the chosen controller |
| uv | `/home/josh/.local/bin/uv` (0.13.0) | can fetch arbitrary CPython builds (3.8, 3.9, …) |
| Internet | reachable (pypi.org HTTP/2 200) | installs possible |
| git | **not installed** | required for the GitHub deliverable — needs `apt install git` |
| Build tools | gcc, g++, make present; `pkg-config` missing | enough for greenlet/eventlet wheels |
| sudo | requires a password | agent cannot run `mn`, `ovs-*`, `apt`, system pkg installs unaided |

## 2. Repository layout (as found)

`/home/josh/Desktop/CN PROJECT` is the real project. It is **not a git repo**.

```
CN PROJECT/
├── .python-version              # "3.13" (uv)
├── .venv/                       # uv venv, CPython 3.13.16  -> has Flask 3.1.3 etc.
├── pyproject.toml               # uv build, name cn-project, dependencies = []
├── uv.lock
├── README.md                    # EMPTY
├── src/cn_project/__init__.py   # hello-world placeholder only
├── app/
│   ├── server.py                # OLD raw-socket demo (download only, port 5001)
│   └── client.py                # OLD raw-socket demo client
├── basic_file_sharing/          # NEWER iteration (Flask + Mininet)
│   ├── .venv/                   # separate system-Python 3.14 venv, has Flask
│   ├── requirements.txt         # Flask only
│   ├── topology.py              # Mininet: 1 server + 3 clients, one switch, controller=None
│   ├── server/
│   │   ├── tcp_server.py        # concurrent threaded TCP server, JSON-line proto, port 9000
│   │   ├── app.py               # Flask server dashboard, port 5000
│   │   ├── templates/dashboard.html
│   │   └── shared/              # server-side shared folder (has testfile.bin, README.txt)
│   └── client/
│       ├── app.py               # Flask client dashboard, heartbeat, list, download, port 5001
│       ├── templates/dashboard.html
│       └── downloads/           # (has testfile.bin, README.txt)
└── docs/
    ├── guidelines.txt           # rubric (D1=15, D2=25) — authoritative
    ├── mininet.txt              # Mininet install guide
    ├── Guidelines_Rubrics.pdf
    ├── 30_Projects.xlsx
    └── Mininet Installation Guide on UBUNTU.pdf
```

Also present outside the repo: `/home/josh/l2_switch.py` (an **OS-Ken** OpenFlow 1.3 L2
learning switch, no REST API, not wired to the topology) and `/home/josh/osken-venv`.

## 3. Component inventory

| Component | Exists? | Works? | Notes |
| --- | --- | --- | --- |
| Concurrent TCP server | yes (`basic_file_sharing/server/tcp_server.py`) | likely | threads per client; JSON-line protocol; **download + list only** |
| TCP client/agent | partial (`client/app.py`) | likely | heartbeat, list, download; **no upload** |
| Flask server dashboard | yes | likely | proxies to TCP server, port 5000 |
| Flask client dashboard | yes | likely | per-host, port 5001 |
| Mininet topology | yes (`topology.py`) | unverified | 1 switch, 1 server + 3 clients; **`controller=None`** |
| SDN controller | **no** | — | none in repo; OS-Ken switch is standalone |
| OpenFlow flow rules / policies | **no** | — | nothing |
| Socket–SDN integration | **no** | — | not started |
| Authentication | **no** | — | no users, no passwords, no sessions |
| Authorization / ownership | **no** | — | no access model |
| Metadata DB | **no** | — | no SQLite/schema |
| Audit / events | **no** | — | not implemented |
| Checksums / integrity | **no** | — | not implemented |
| Framed length-prefixed protocol | **no** | — | current protocol is newline-JSON + raw bytes |
| Upload path | **no** | — | only download/list exist |
| Metrics / bandwidth monitoring | **no** | — | not implemented |
| Tests | **no** | — | none |
| Experiment scripts / results | **no** | — | none |
| Git repo / README | **no** | — | README empty; not a git repo |
| Duplicate/legacy code | yes (`app/server.py`, `app/client.py`) | — | older download-only demo, superseded by `basic_file_sharing/` |

## 4. Known conflicts / inconsistencies to resolve

1. **Two venvs, three Pythons.** Top `.venv` = CPython 3.13 (uv); `basic_file_sharing/.venv`
   = system 3.14; Mininet lives on system 3.14. `topology.py` launches apps with the *top*
   `.venv` Python (`..‎/.venv/bin/python`), so the two environments are already mixed.
2. **Two overlapping implementations.** `app/{server,client}.py` (old) vs
   `basic_file_sharing/{server,client}` (new). The old pair should be retired/marked legacy.
3. **Framework choice.** Roadmap assumed **FastAPI**; the repo already uses **Flask**
   (installed, working). Flask is a defensible, simpler choice for this scope.
4. **No SDN at all.** The rubric's heaviest items (Mininet+SDN 4, Socket–SDN 3, Dynamic SDN 6)
   are currently unimplemented.
5. **Instructions never connect the browser to a Mininet-hosted dashboard.** Host IPs
   (10.0.0.x) are not reachable from the root namespace as-is (no route/management link),
   so "open the dashboard in a browser" is not yet proven to work.

## 5. Unverified (needs root / sudo — cannot be checked yet)

- [ ] `sudo mn` starts a topology on this host and `pingall` succeeds.
- [ ] OVS datapath works with the Ryu controller (`RemoteController` on 6653).
- [ ] `topology.py` actually starts the three Flask apps and they are reachable.
- [ ] Mininet creates named netns (`ip netns list`) on this version.
- [ ] Ryu installs and runs on a downgraded Python (3.8/3.9) on aarch64.
- [ ] `apt install git` for the GitHub deliverable.

## 6. Requirement coverage (Project 14 / rubric)

| Requirement | Status |
| --- | --- |
| Concurrent TCP transfers | partial (download concurrency exists; no upload) |
| Upload + download | download only |
| File metadata | minimal (name/size/mtime listed live; not persisted) |
| Authenticated access | missing |
| SDN access policies | missing |
| Bandwidth monitoring | missing |
| Simultaneous-transfer testing | missing |
| Transfer-performance evaluation | missing |
| Mininet topology + controller + flow rules | topology exists, controller/rules missing |
| Socket–SDN integration | missing |
| Dynamic SDN behavior | missing |
| Challenging scenario | missing |
| Tests / docs / GitHub | missing (docs = source PDFs only) |

## 7. Baseline reproduction (to be confirmed once sudo is available)

```
# in root namespace
sudo mn -c                 # clean stale state
sudo mn                    # default topology, then: pingall / exit
sudo mn --topo single,3
```

Expected: `pingall` reports 0% dropped. This is the connectivity baseline before any
custom topology or controller is introduced.

---

## 8. Progress log

### Phase 1 (partial) — 2026-10-16

**Repository restructured** (old layout archived under `legacy/`, nothing deleted):

```
server/     tcp_server.py, app.py, templates/, storage/     (moved from basic_file_sharing/server)
client/     app.py, templates/, downloads/                  (moved from basic_file_sharing/client)
SDN/        topology.py, controller/{ryu_app,policy,rest}.py
common/     protocol.py (framed TCP protocol), config.py
scripts/    start_controller.sh, start_services.sh, cleanup.sh
tests/      unit/, integration/, scenarios/   (empty placeholder)
results/    raw/, figures/
legacy/     old_raw_socket_app/, src/, pyproject.toml, uv.lock, .python-version, basic_file_sharing-requirements.txt
```

**Changes made**
- `server/tcp_server.py`: storage dir renamed `shared/` -> `storage/`; added a `__main__`
  guard so it can run standalone (it previously only ran under `server/app.py`).
- `SDN/topology.py`: paths updated to the new layout; client IPs changed to
  `10.0.0.11-13`; optional Ryu attachment via `SDN_CONTROLLER_IP` / `SDN_CONTROLLER_PORT`
  (defaults to a standalone switch when unset).
- New `common/protocol.py`: length-prefixed framed protocol (`recv_exact`, `recv_header`,
  `send_header`, `send_file`, `recv_file`, `sanitize_filename`, stable error codes).
- New `SDN/controller/{ryu_app,policy,rest}.py`: Ryu L2-learning + policy switch with a
  REST control plane (grant/revoke/state/stats/flows).
- New `README.md`, `requirements.txt`, `requirements-controller.txt`, `.gitignore`,
  `.env.example`, `scripts/*.sh`.

**Evidence — worked (no root needed)**

| Check | Result |
| --- | --- |
| Moved TCP server: `LIST` + `DOWNLOAD` | ok; SHA-256 of download == stored file |
| Path-traversal download (`../tcp_server.py`) | rejected (`File not found`) |
| `common/protocol.py` round-trip with 1-byte fragmentation | header + 5-byte payload intact |
| Oversized header length | rejected (`BAD_FRAME`) |
| `sanitize_filename` on `../evil`, `/etc/passwd`, `.hidden`, `a/b`, `..`, `""`, 300-char | all rejected; `ok.txt` accepted |
| Ryu 4.34 install on Python 3.8.20 (aarch64) | **works** (pip, `setuptools<58`, `--no-build-isolation`, `eventlet==0.30.2`, `greenlet==1.1.3`) |
| `ryu-manager SDN/controller/ryu_app.py` starts | loads `PolicySwitch`, OFP on 6653, WSGI/REST on 127.0.0.1:8080 |
| `GET /policy/state`, `/policy/stats` | respond correctly |
| `POST /policy/grant` -> `state` -> `revoke` -> `state` | grant created (cookie 4096), listed, removed |
| `POST /policy/grant` missing `dport` | `400 bad request` |
| `PolicyEngine` unit check | idempotent grants, per-source isolation, revoke clears state |

**Verified once sudo was available (Phase 1 exit + Phase 2)**

| Check | Result |
| --- | --- |
| `sudo mn -c` / `sudo mn --test pingall` | clean; 0% dropped (2/2) |
| `git` installed | `git version 2.53.0` |
| `SDN/topology.py` under Ryu (`SDN_CONTROLLER_IP=127.0.0.1`) | s1 connects to `127.0.0.1:6653`; `pingall` 0% dropped (12/12) |
| `ovs-ofctl dump-flows s1` after up | priority=10 learned L2 flows + `priority=0 actions=CONTROLLER:65535` table-miss |
| Server + 3 client Flask apps started inside hosts | dashboards logged at 10.0.0.1:5000, 10.0.0.11-13:5001 |

**Verified — SDN gate (Socket<->SDN integration), `scripts/verify_sdn_gate.py`**

| Stage | Probe from client1 (10.0.0.11) to server:9000 | Controller |
| --- | --- | --- |
| Unauthorized | **BLOCKED** (`TimeoutError`) | `DENY 10.0.0.11 -> 10.0.0.1:9000 (no grant)`; counter `denied` increments |
| After `POST /policy/grant` | **OK** — LIST exchange succeeded | allow flows installed: `cookie=0x1001, priority=200,tcp,nw_src=10.0.0.11,nw_dst=10.0.0.1,tp_dst=9000 actions=NORMAL` (+ reverse) |
| After `POST /policy/revoke` | **BLOCKED** again | allow flows removed; only table-miss remains |

**Still to do**
- [ ] Login/logout in the Flask app wired to grant/revoke (currently driven by REST directly).
- [ ] Already-established TCP sessions during revocation.
- [ ] Consolidate to one app venv / freeze deps; `git init` + first commit.
- [ ] `docs/architecture.md`, `docs/protocol.md`.
