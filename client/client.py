"""CNDF client: transfers files to/from the server using protocol.py.

This is the teammate client.py, kept close to the original: the Client class
handles one connection (login + upload/download/list), and the command line
below can run several transfers in parallel with -j.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time

# protocol.py lives in the project root, one level above this file.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import CHUNK_SIZE, recv_frame, send_frame, sha256_file

DEFAULT_HOST = "10.0.0.1"
DEFAULT_PORT = 9000
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0


class ClientError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class Client:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.token = None
        self.username = None
        self._secret = None
        self.stats = []

    def connect(self):
        self.sock = socket.create_connection(
            (self.host, self.port), timeout=CONNECT_TIMEOUT
        )
        self.sock.settimeout(READ_TIMEOUT)
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.token = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def login(self, username, password):
        self.username = username
        self._secret = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return self._auth()

    def _auth(self):
        if self._secret is None:
            raise ClientError("NOT_AUTHENTICATED", "no credentials stored")
        send_frame(
            self.sock,
            "AUTH",
            username=self.username,
            password_sha256=self._secret,
        )
        hdr, _ = recv_frame(self.sock)
        if hdr["type"] == "ERROR":
            raise ClientError(hdr.get("code", "ERROR"), hdr.get("message", ""))
        if hdr["type"] != "AUTH_OK":
            raise ClientError("BAD_RESPONSE", hdr["type"])
        self.token = hdr["token"]
        return hdr

    def _request(self, msg_type, **fields):
        if self.token is None:
            raise ClientError("NOT_AUTHENTICATED", "not logged in")
        for attempt in (0, 1):
            send_frame(self.sock, msg_type, token=self.token, **fields)
            hdr, payload = recv_frame(self.sock)
            if hdr["type"] != "ERROR":
                return hdr, payload
            if (
                hdr.get("code") == "NOT_AUTHENTICATED"
                and attempt == 0
                and self._secret is not None
            ):
                self._auth()
                continue
            raise ClientError(hdr.get("code", "ERROR"), hdr.get("message", ""))
        raise ClientError("NOT_AUTHENTICATED", "re-authentication failed")

    def list_files(self):
        hdr, payload = self._request("LIST")
        if hdr["type"] != "LIST_RESP":
            raise ClientError("BAD_RESPONSE", hdr["type"])
        return json.loads(payload.decode("utf-8")) if payload else []

    def upload(self, path, name=None):
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        digest = sha256_file(path)
        start = time.monotonic()

        hdr, _ = self._request(
            "UPLOAD_INIT", filename=name, size=size, sha256=digest
        )
        if hdr["type"] != "OK":
            raise ClientError("BAD_RESPONSE", hdr["type"])

        seq = 0
        with open(path, "rb") as f:
            while True:
                block = f.read(CHUNK_SIZE)
                if not block:
                    break
                send_frame(self.sock, "CHUNK", block, seq=seq)
                seq += 1

        send_frame(self.sock, "UPLOAD_DONE", sha256=digest, chunks=seq)
        hdr, _ = recv_frame(self.sock)
        if hdr["type"] == "ERROR":
            raise ClientError(hdr.get("code", "ERROR"), hdr.get("message", ""))
        if hdr["type"] != "UPLOAD_OK":
            raise ClientError("BAD_RESPONSE", hdr["type"])

        return self._record("upload", name, size, start)

    def download(self, filename, dest=None):
        dest = dest or os.path.basename(filename)
        start = time.monotonic()

        hdr, _ = self._request("DOWNLOAD_REQ", filename=filename)
        if hdr["type"] != "DOWNLOAD_INIT":
            raise ClientError("BAD_RESPONSE", hdr["type"])
        expected_sha = hdr["sha256"]
        expected_size = hdr["size"]

        tmp = dest + ".part"
        digest = hashlib.sha256()
        total = 0
        chunks = 0

        try:
            with open(tmp, "wb") as f:
                while True:
                    fhdr, payload = recv_frame(self.sock)
                    if fhdr["type"] == "ERROR":
                        raise ClientError(
                            fhdr.get("code", "ERROR"), fhdr.get("message", "")
                        )
                    if fhdr["type"] == "CHUNK":
                        if fhdr.get("seq") != chunks:
                            raise ClientError(
                                "BAD_SEQ", f"expected {chunks}, got {fhdr.get('seq')}"
                            )
                        f.write(payload)
                        digest.update(payload)
                        total += len(payload)
                        chunks += 1
                        continue
                    if fhdr["type"] == "DOWNLOAD_DONE":
                        if fhdr.get("chunks") != chunks:
                            raise ClientError(
                                "TRUNCATED", f"{chunks} of {fhdr.get('chunks')} chunks"
                            )
                        break
                    raise ClientError("BAD_RESPONSE", fhdr["type"])
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

        if total != expected_size or digest.hexdigest() != expected_sha:
            os.remove(tmp)
            raise ClientError("HASH_MISMATCH", filename)

        os.replace(tmp, dest)
        return self._record("download", filename, total, start)

    def quit(self):
        try:
            if self.token is not None:
                self._request("QUIT")
        finally:
            self.close()

    def _record(self, op, name, nbytes, start):
        end = time.monotonic()
        seconds = end - start
        mbps = (nbytes * 8 / seconds / 1e6) if seconds > 0 else 0.0
        entry = {
            "op": op,
            "filename": name,
            "bytes": nbytes,
            "start": start,
            "end": end,
            "seconds": seconds,
            "mbps": mbps,
        }
        self.stats.append(entry)
        return entry


def format_stat(entry):
    return (
        f"{entry['op']:8s} {entry['filename']:32s} "
        f"{entry['bytes']:>12,d} B  {entry['seconds']:>8.3f} s  "
        f"{entry['mbps']:>9.2f} Mbps"
    )


def run_parallel(host, port, user, password, tasks, limit):
    lock = threading.Lock()
    results = []
    errors = []
    slots = threading.Semaphore(limit)

    def worker(op, args):
        with slots:
            client = Client(host, port)
            try:
                client.connect()
                client.login(user, password)
                fn = client.upload if op == "upload" else client.download
                entry = fn(*args)
                with lock:
                    results.append(entry)
            except Exception as exc:
                with lock:
                    errors.append((op, args, exc))
            finally:
                client.close()

    threads = [threading.Thread(target=worker, args=t) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description="CNDF v1.0 client")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-u", "--user", required=True)
    parser.add_argument("-p", "--password", required=True)
    parser.add_argument(
        "-j", "--jobs", type=int, default=1, help="max simultaneous connections"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ls")
    put = sub.add_parser("put")
    put.add_argument("files", nargs="+")
    get = sub.add_parser("get")
    get.add_argument("files", nargs="+")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "ls":
            client = Client(args.host, args.port).connect()
            try:
                client.login(args.user, args.password)
                records = client.list_files()
            finally:
                client.quit()
            if not records:
                print("(no files)")
            for r in records:
                print(
                    f"{r['filename']:32s} {r['size']:>12,d} B  "
                    f"{r.get('owner', '-'):10s} {r.get('upload_time', '-')}  "
                    f"{r.get('sha256', '')[:12]}"
                )
            return 0

        tasks = [
            ("upload" if args.cmd == "put" else "download", (path,))
            for path in args.files
        ]

        if args.jobs <= 1 and len(tasks) == 1:
            client = Client(args.host, args.port).connect()
            try:
                client.login(args.user, args.password)
                fn = client.upload if args.cmd == "put" else client.download
                entry = fn(args.files[0])
                client.stats.append(entry)
                results, errors = [entry], []
            finally:
                client.quit()
        else:
            results, errors = run_parallel(
                args.host, args.port, args.user, args.password, tasks, args.jobs
            )

        for entry in sorted(results, key=lambda e: e["start"]):
            print(format_stat(entry))
        for op, task, exc in errors:
            print(f"{op} {task[0]}: FAILED — {exc}", file=sys.stderr)

        if len(results) > 1:
            span = max(e["end"] for e in results) - min(e["start"] for e in results)
            total = sum(e["bytes"] for e in results)
            agg = (total * 8 / span / 1e6) if span > 0 else 0.0
            print(
                f"aggregate: {len(results)} transfers, {total:,d} B, "
                f"{span:.3f} s wall, {agg:.2f} Mbps"
            )

        return 1 if errors else 0

    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ConnectionError, socket.timeout, OSError) as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
