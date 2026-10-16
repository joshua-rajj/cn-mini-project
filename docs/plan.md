# Architecture & Phased Plan

> Project 14 — Concurrent File Sharing System. Draft architecture and implementation plan.
> Edit freely; this is the working blueprint, not a course-mandated spec.

## 1. Goal (restated)

Build a multi-user file-sharing application where **multiple clients upload/download
concurrently over a real low-level TCP socket protocol across Mininet hosts**, while a
**Ryu SDN controller** installs/removes OpenFlow rules that genuinely control reachability
to the protected transfer service, driven by **authentication state**. Ship tests,
performance comparison (static vs dynamic), docs, and a reproducible demo.

Rubric weighting to optimise for: Dynamic SDN (6), Docs/Demo/Viva (6), Performance (5),
Complete functionality (4), Challenging scenario (4), Mininet+SDN (4), TCP sockets (4),
Socket–SDN integration (3), Architecture (2), Initial demo (2).

## 2. Key decisions (confirmed 2026-10-16)

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | **Keep Flask** (not FastAPI) for the web/control layer | already installed and working; roadmap says preserve existing conventions; Flask is sufficient for pages + JSON APIs + sessions |
| D2 | **Ryu as controller, run in a dedicated Python 3.8 (fallback 3.9) venv** via `uv` | user chose Ryu; Ryu is unmaintained for 3.10+ and cannot run on system 3.14. Ryu is a separate process speaking OpenFlow, so it does **not** need to share an interpreter with Mininet or the app |
| D3 | **Mininet + topology stay on system Python 3.14** | `mininet` is an apt package bound to system Python (`/usr/lib/python3/dist-packages/mininet`) |
| D4 | **App/transfer code runs on one venv** (consolidate to the top `.venv`, CPython 3.13) | fixes the current two-venv mismatch |
| D5 | **One Mininet client host per user** | gives distinct source IPs so OpenFlow rules can actually enforce per-user policy |
| D6 | **SQLite via stdlib `sqlite3`** (thin repo layer), password hashing via `werkzeug.security` | zero extra native deps; werkzeug already present |
| D7 | **Length-prefixed framed TCP protocol** (4-byte big-endian header length + UTF-8 JSON + optional payload) replacing the newline-JSON protocol | removes the fragile "read until newline then raw bytes" framing; robust to fragmentation |
| D8 | Migrate `basic_file_sharing/` into the roadmap layout (`app/`, `transfer/`, `controller/`, `mininet/`, …) | matches the brief's suggested structure; keeps one source of truth |

### Open sub-decisions
- **Browser → dashboard reachability.** Mininet host IPs (10.0.0.x) are not reachable from the
  VM's root namespace by default. Two workable options:
  - **(A) Management veth** — attach a veth from the OVS switch to root ns with IP
    `10.0.0.254/24`; the VM browser then reaches `http://10.0.0.x:5000`. Simple, keeps the
    per-host Flask dashboards. *(recommended)*
  - **(B) Portal in root ns** — one Flask portal on the VM (browser-friendly) that drives
    transfers by executing the transfer agent inside the relevant host netns
    (`ip netns exec <host> ...`). Cleaner separation of UI vs data plane.
- **Where the transfer agent runs** so its traffic carries the user's host IP (required for
  IP-based rules): per-host agent daemon **or** `ip netns exec` invocation. Prefer the latter
  for simplicity; a small agent daemon is the fallback if we need long-lived control.

## 3. Target architecture

```
        VM browser (root ns)
              |  HTTP (pages, login, file list, transfer actions)
              v
   ┌──────────────────────────────┐        REST (control plane, trusted host only)
   │  Flask Portal / Control API  │──────────────┐
   │  app/  (sessions, metadata,  │              v
   │  transfer orchestration)     │      ┌───────────────────────────┐
   └──────────────┬───────────────┘      │  Ryu Controller (Py3.8)   │
                  │ orchestrate           │  l2 learning + policy +   │
                  │                       │  REST: grant/revoke/state │
                  v                       │        /flow-stats        │
   ┌──────────────────────────┐           └──────────────┬────────────┘
   │  TCP File Transfer Svc   │                          │ OpenFlow 1.3 (6653)
   │  transfer/server.py      │                          v
   │  (threaded, framed proto)│                 ┌──────────────────┐
   └──────────────┬───────────┘                 │  OVS switch s1   │
                  │                             └───┬────┬────┬────┘
                  │  raw TCP (data plane)           │    │    │
        ┌─────────┴─────────┐          ┌──────────┐ │ ┌──┴─────┐ ┌┴────────┐
        │ server host       │          │ client1  │─┘ │client2 │ │client3  │
        │ 10.0.0.1:9000     │◄────────►│10.0.0.11 │   │10.0.0.12│ │10.0.0.13│
        └───────────────────┘          └──────────┘   └────────┘ └─────────┘
              \____________ Mininet hosts on s1 ____________/

   SQLite metadata/audit  |  transfer metrics  |  OpenFlow flow counters
```

**HTTP path** = browser ↔ Flask portal. **Raw TCP path** = transfer client in a Mininet host ↔
transfer server, carrying the file bytes and subject to OpenFlow rules. These are explicitly
different planes (documented and demonstrated).

## 4. Component responsibilities

- **Flask portal (`app/`)** — HTML pages (register/login/dashboard/file list), sessions,
  CSRF, metadata + ownership, transfer orchestration, calls Ryu REST to grant/revoke, shows
  real controller outcome, fails closed. Never the *only* authorization layer.
- **TCP transfer service (`transfer/`)** — low-level `socket` API; threaded concurrency;
  framed protocol; streaming upload/download; SHA-256 verify; temp file + atomic rename;
  bounded sizes; timeouts; independent authorization check (short-lived token); structured
  logs; stable error codes.
- **Ryu controller (`controller/`)** — L2 learning + table-miss; policy engine keyed by
  (src IP → dst IP, TCP dport 9000); REST API for grant/revoke/state/stats; idempotent
  install/remove by cookie/priority; collects flow stats; reports `applied|failed|unknown`.
- **Mininet (`mininet/`)** — repeatable topology (1 server, 3 clients, 1 OVS switch,
  `RemoteController` → Ryu), documented IPs/ports, TCLink for bottleneck experiments,
  startup/cleanup scripts.
- **Metadata/audit (`app/db/`)** — SQLite: users (hashed pw), files (owner, size, sha256),
  access policy, transfers, audit events. Metadata separate from file bytes.

## 5. Application TCP protocol (v1)

```
Frame = [4-byte BE header_len][header_len bytes UTF-8 JSON][optional payload_len bytes]

Request header: { "v":1, "id":"uuid", "cmd":"AUTH|UPLOAD|DOWNLOAD|LIST|STATUS|CANCEL",
                  "token":"<short-lived>", "name":..., "file_id":..., "size":...,
                  "sha256":... }
Response header:{ "v":1, "id":..., "ok":true|false, "code":"...", "msg":"...",
                  "size":..., "sha256":... }
```
Rules: `recv_exact(sock,n)`, `recv_frame`, `send_frame`, streaming copy in bounded chunks
(64 KiB); cap header length and file size; sanitize filenames (no `/`, `\`, `..`, absolute,
reserved); resolve `file_id` → server-controlled path (never trust client paths); write to
`*.part` then `os.replace` only after size+sha256 match; clean up on error/disconnect.

## 6. Authentication ↔ SDN policy model

States: **Unauthenticated → Authorized → Revoked/Denied**.

| Trigger | App action | Controller action |
| --- | --- | --- |
| login OK | create session, map user→client host IP | `POST /policy/grant {src_ip, dst_ip, dport:9000}` → install high-priority allow |
| login fail | reject, audit | none (no access) |
| logout / expiry | destroy session | `POST /policy/revoke {src_ip,...}` → remove allow, install deny |
| denied attempt | reject + audit | (observable drop counter) |

- Match fields: `eth_type=IPv4, ipv4_src, ipv4_dst, ip_proto=6, tcp_dst=9000` (+ reverse).
- Priorities: deny **higher** than default allow; specific beats broad; explicit priorities
  documented. Cookie = per-user so removal is precise; idempotent add/remove; rule timeouts
  to avoid stale grants after a crash.
- Table-miss → controller (learning + default posture). ARP handled so discovery isn't broken.
- **Fail closed** if the controller is unreachable or returns `failed`.
- Transfer service independently validates a short-lived token; revocation tested for **new**
  and **already-established** TCP connections (server must close/reject active sessions).

## 7. Metadata schema (SQLite)

- `users(id, username UNIQUE, pw_hash, created_at, is_active)`
- `files(id, owner_id, display_name, storage_key, size, sha256, content_type, created_at)`
- `access(user_id, file_id, can_read)`  — owner implicit; sharing is optional extension
- `transfers(id, user_id, file_id, direction, src_host, dst_host, bytes, started_at,
   ended_at, result, error)`
- `audit_events(id, ts, event_type, user_id, host_ip, decision, detail)`

## 8. Repository layout (implemented — Phase 1)

```
CN PROJECT/
├── server/         File server host
│   ├── tcp_server.py       concurrent TCP transfer service (port 9000)
│   ├── app.py              Flask server dashboard + control API
│   ├── templates/          dashboard.html
│   └── storage/            server-side stored files (data)
├── client/         Client host
│   ├── app.py              Flask client dashboard (heartbeat/list/download)
│   ├── templates/          dashboard.html
│   └── downloads/          client-side downloads (data)
├── SDN/            SDN component
│   ├── controller/
│   │   ├── ryu_app.py      Ryu app: L2 learning + protected-service policy
│   │   ├── policy.py       pure policy state (unit-testable, no Ryu imports)
│   │   └── rest.py         REST control plane (grant/revoke/state/stats/flows)
│   └── topology.py         Mininet topology (optional RemoteController -> Ryu)
├── common/         protocol.py (framed TCP protocol), config.py
├── scripts/        start_controller.sh, start_services.sh, cleanup.sh
├── tests/          unit/ integration/ scenarios/
├── results/        raw/ figures/
├── docs/           current-state.md, plan.md, + original course material
├── envs/ryu/       Ryu Python 3.8 virtualenv (not committed)
├── legacy/         archived earlier prototypes (not used)
├── requirements.txt, requirements-controller.txt
├── .env.example, .gitignore
└── README.md
```

Migration map used: `basic_file_sharing/server/*` -> `server/`;
`basic_file_sharing/client/*` -> `client/`; `basic_file_sharing/topology.py` -> `SDN/topology.py`;
old `app/`, `src/`, uv project files -> `legacy/`.

## 9. Phased plan (in order)

### Phase 0 — Audit ✅ (this doc + current-state.md)
Exit: codebase understood and baseline reproducible (pending sudo verification).

### Phase 1 — Toolchain & baseline *(complete)*
- [x] Create Ryu venv on Python 3.8 via `uv`; install Ryu (pip workaround documented).
- [x] Ryu starts and loads `SDN/controller/ryu_app.py`; REST responds.
- [x] Install `git` (2.53.0).
- [x] Verify `sudo mn -c`, `sudo mn --test pingall` (0% dropped).
- [x] OVS s1 connects to Ryu on 6653; `dump-flows` captured.
- **Exit:** baseline Mininet run works; Ryu process starts and a switch connects (log proof). ✅

### Phase 2 — Controller skeleton (learning + policy) *(complete)*
- [x] L2 learning + table-miss in `SDN/controller/ryu_app.py`.
- [x] Protected endpoint (TCP dport 9000) gated by policy.
- [x] REST control plane (`grant/revoke/state/stats/flows`).
- [x] Verified with `scripts/verify_sdn_gate.py`: unauthorized BLOCKED, granted OK, revoked BLOCKED; flow dumps captured.
- **Exit:** pingall works through the Ryu-controlled switch and policy changes forwarding behaviour. ✅

### Phase 3 — TCP transfer core
- Implement framed protocol + `recv_exact/recv_frame/send_frame` + streaming copy.
- Add **upload** (temp + atomic + sha256), keep download; timeouts, limits, error codes.
- Cap concurrency, per-connection isolation, structured transfer IDs/logs.
- Tests: empty/small/large files, fragmented frames, early disconnect, wrong size, bad
  checksum, duplicate names, simultaneous transfers.
- **Exit:** files transfer both ways between two Mininet hosts over raw TCP; tests pass.

### Phase 4 — Metadata & auth
- SQLite schema + init; users with werkzeug-hashed passwords; Flask register/login/logout
  with sessions + CSRF; file ownership; list/download authorization; audit events.
- **Exit:** docs/current-state.md updated; a user can log in, upload, list, download own file;
  cross-user access denied at app layer.

### Phase 5 — SDN auth integration (priority milestone)
- Ryu REST: `grant / revoke / state / flow-stats`.
- Map user→host IP; login→grant, logout/expiry→revoke; fail closed.
- Transfer service validates short-lived token; revocation for new **and** established conns.
- **Exit:** before/after demo shows a client either can or cannot reach `server:9000`
  depending on login state, proven with `dump-flows` + traffic outcome.

### Phase 6 — Concurrent multi-user E2E
- 3 client hosts, simultaneous upload/download; isolated per-transfer state; same-name policy;
  ownership integrity; activity/status view.
- **Exit:** concurrent transfers succeed, hashes match, per-user restrictions hold.

### Phase 7 — Monitoring & dynamic behavior
- App transfer metrics (size, duration, throughput, success) + controller flow counters;
  metrics view + CSV/JSON export.
- Keep the dynamic behavior focused (auth grant/revoke) + optional congestion-aware response.
- **Exit:** measurements correlate with transfer activity and flow state.

### Phase 8 — Challenging scenario & experiments
- Scenarios: (1) unauthorized/revocation, (2) simultaneous transfers, (3) controlled
  bottleneck (TCLink `bw`/`delay`/`loss`).
- Compare **static/baseline** vs **dynamic** policy; ≥3 trials; save raw results; compute
  throughput/completion/success; explain app-counter vs OpenFlow-counter differences.
- **Exit:** scripts rerun and support every number in the report; no fabricated results.

### Phase 9 — Hardening, docs, demo, GitHub
- Startup/cleanup scripts; pinned deps; docs (architecture, protocol, topology, testing,
  experiments, troubleshooting); demo checklist; no secrets committed; push to GitHub.
- Prepare viva answers (framing/concurrency, Mininet, OpenFlow, policy mapping, IP-identity
  limits, metrics, results).
- **Exit:** another student can clone and reproduce the demo and experiments.

## 10. Risks

- **Ryu on aarch64 + Py3.8**: greenlet/eventlet may need compilation (gcc present, should be OK;
  `pkg-config` missing — watch for it).
- **Mininet 2.3.0 on Ubuntu 26.04 / Py3.14**: possible breakage on such a new base; verify early.
- **Browser reachability** into Mininet namespaces (see §2 open sub-decision).
- **Established-connection revocation** is a known-hard problem; we test and document it honestly.
- **Scope**: many rubric items — stay breadth-first to protect D1, then deepen for D2.

## 11. Evidence to produce per phase

Files changed · exact commands run · tests performed · observed results (logs/dumps) ·
remaining limitations — appended to `docs/current-state.md`.
