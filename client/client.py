import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import CHUNK_SIZE, recv_frame, send_frame, sha256_file

DEFAULT_HOST = "10.0.0.1"
DEFAULT_PORT = 9000


class ClientError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        if message:
            super().__init__(code + ": " + message)
        else:
            super().__init__(code)


class Client:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.token = None
        self.username = None
        self.secret = None
        self.stats = []

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(30)
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.token = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()

    def login(self, username, password):
        self.username = username
        self.secret = hashlib.sha256(password.encode()).hexdigest()
        return self.auth()

    def auth(self):
        send_frame(self.sock, "AUTH", username=self.username, password_sha256=self.secret)
        header, payload = recv_frame(self.sock)
        if header["type"] == "ERROR":
            raise ClientError(header.get("code", "ERROR"), header.get("message", ""))
        if header["type"] != "AUTH_OK":
            raise ClientError("BAD_RESPONSE", header["type"])
        self.token = header["token"]
        return header

    def request(self, msg_type, **fields):
        send_frame(self.sock, msg_type, token=self.token, **fields)
        header, payload = recv_frame(self.sock)

        if header["type"] == "ERROR" and header.get("code") == "NOT_AUTHENTICATED":
            self.auth()
            send_frame(self.sock, msg_type, token=self.token, **fields)
            header, payload = recv_frame(self.sock)

        if header["type"] == "ERROR":
            raise ClientError(header.get("code", "ERROR"), header.get("message", ""))

        return header, payload

    def list_files(self):
        header, payload = self.request("LIST")
        if header["type"] != "LIST_RESP":
            raise ClientError("BAD_RESPONSE", header["type"])
        if payload:
            return json.loads(payload.decode())
        return []

    def upload(self, path, name=None):
        if name is None:
            name = os.path.basename(path)

        size = os.path.getsize(path)
        sha = sha256_file(path)
        start = time.time()

        header, payload = self.request("UPLOAD_INIT", filename=name, size=size, sha256=sha)
        if header["type"] != "OK":
            raise ClientError("BAD_RESPONSE", header["type"])

        number = 0
        with open(path, "rb") as file:
            while True:
                data = file.read(CHUNK_SIZE)
                if not data:
                    break
                send_frame(self.sock, "CHUNK", data, seq=number)
                number += 1

        send_frame(self.sock, "UPLOAD_DONE", sha256=sha, chunks=number)
        header, payload = recv_frame(self.sock)
        if header["type"] == "ERROR":
            raise ClientError(header.get("code", "ERROR"), header.get("message", ""))
        if header["type"] != "UPLOAD_OK":
            raise ClientError("BAD_RESPONSE", header["type"])

        return self.record("upload", name, size, start)

    def download(self, filename, dest=None):
        if dest is None:
            dest = os.path.basename(filename)

        start = time.time()
        header, payload = self.request("DOWNLOAD_REQ", filename=filename)
        if header["type"] != "DOWNLOAD_INIT":
            raise ClientError("BAD_RESPONSE", header["type"])

        expected_sha = header["sha256"]
        expected_size = header["size"]
        temp = dest + ".part"
        digest = hashlib.sha256()
        total = 0
        number = 0

        try:
            with open(temp, "wb") as file:
                while True:
                    frame, data = recv_frame(self.sock)
                    if frame["type"] == "ERROR":
                        raise ClientError(frame.get("code", "ERROR"), frame.get("message", ""))
                    if frame["type"] == "CHUNK":
                        if frame.get("seq") != number:
                            raise ClientError("BAD_SEQ", "wrong chunk number")
                        file.write(data)
                        digest.update(data)
                        total += len(data)
                        number += 1
                        continue
                    if frame["type"] == "DOWNLOAD_DONE":
                        if frame.get("chunks") != number:
                            raise ClientError("TRUNCATED", "file is incomplete")
                        break
                    raise ClientError("BAD_RESPONSE", frame["type"])
        except Exception:
            if os.path.exists(temp):
                os.remove(temp)
            raise

        if total != expected_size or digest.hexdigest() != expected_sha:
            os.remove(temp)
            raise ClientError("HASH_MISMATCH", filename)

        os.replace(temp, dest)
        return self.record("download", filename, total, start)

    def quit(self):
        try:
            if self.token:
                self.request("QUIT")
        finally:
            self.close()

    def record(self, op, filename, size, start):
        end = time.time()
        seconds = end - start
        speed = 0
        if seconds > 0:
            speed = size * 8 / seconds / 1000000

        item = {
            "op": op,
            "filename": filename,
            "bytes": size,
            "start": start,
            "end": end,
            "seconds": seconds,
            "mbps": speed,
        }
        self.stats.append(item)
        return item


def format_stat(item):
    return (f"{item['op']:8s} {item['filename']:32s} "
            f"{item['bytes']:>12,d} B  {item['seconds']:>8.3f} s  "
            f"{item['mbps']:>9.2f} Mbps")


def run_parallel(host, port, user, password, tasks, limit):
    results = []
    errors = []
    lock = threading.Lock()
    slots = threading.Semaphore(limit)

    def worker(op, filename):
        with slots:
            client = Client(host, port)
            try:
                client.connect()
                client.login(user, password)
                if op == "upload":
                    result = client.upload(filename)
                else:
                    result = client.download(filename)
                with lock:
                    results.append(result)
            except Exception as error:
                with lock:
                    errors.append((op, filename, error))
            finally:
                client.close()

    threads = []
    for op, filename in tasks:
        thread = threading.Thread(target=worker, args=(op, filename))
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return results, errors


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-u", "--user", required=True)
    parser.add_argument("-p", "--password", required=True)
    parser.add_argument("-j", "--jobs", type=int, default=1)
    parser.add_argument("command", choices=["ls", "put", "get"])
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)

    try:
        if args.command == "ls":
            client = Client(args.host, args.port)
            client.connect()
            try:
                client.login(args.user, args.password)
                files = client.list_files()
            finally:
                client.quit()

            if not files:
                print("(no files)")
            for item in files:
                print(f"{item['filename']:32s} {item['size']:>12,d} B  "
                      f"{item['owner']:10s} {item['upload_time']}  "
                      f"{item['sha256'][:12]}")
            return 0

        if not args.files:
            parser.error("give at least one file")

        if args.jobs <= 1 and len(args.files) == 1:
            client = Client(args.host, args.port)
            client.connect()
            try:
                client.login(args.user, args.password)
                if args.command == "put":
                    item = client.upload(args.files[0])
                else:
                    item = client.download(args.files[0])
                results = [item]
                errors = []
            finally:
                client.quit()
        else:
            tasks = []
            for filename in args.files:
                if args.command == "put":
                    tasks.append(("upload", filename))
                else:
                    tasks.append(("download", filename))
            results, errors = run_parallel(
                args.host, args.port, args.user, args.password, tasks, args.jobs
            )

        for item in sorted(results, key=lambda row: row["start"]):
            print(format_stat(item))
        for op, filename, error in errors:
            print(f"{op} {filename}: FAILED - {error}", file=sys.stderr)

        if len(results) > 1:
            start = min(item["start"] for item in results)
            end = max(item["end"] for item in results)
            span = end - start
            total = sum(item["bytes"] for item in results)
            speed = 0
            if span > 0:
                speed = total * 8 / span / 1000000
            print(f"aggregate: {len(results)} transfers, {total:,d} B, "
                  f"{span:.3f} s wall, {speed:.2f} Mbps")

        if errors:
            return 1
        return 0

    except ClientError as error:
        print("error:", error, file=sys.stderr)
        return 2
    except OSError as error:
        print("connection error:", error, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
