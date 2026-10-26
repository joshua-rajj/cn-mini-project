import csv
import hashlib
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import CHUNK_SIZE, ProtocolError, recv_frame, send_frame, sha256_file

HOST = "0.0.0.0"
PORT = 9000
MAX_FILE_SIZE = 1024 * 1024 * 1024
SERVER_PASSWORD = os.environ.get("SERVER_PASSWORD", "pass123")
CLIENT_TIMEOUT = 30

BASE_DIR = Path(__file__).parent
STORAGE = BASE_DIR / "storage"
METADATA = BASE_DIR / "metadata.csv"
FIELDS = ["filename", "size_bytes", "owner", "uploaded_at", "sha256"]

STORAGE.mkdir(exist_ok=True)

metadata_lock = threading.Lock()
reserved = set()

clients = {}
clients_lock = threading.Lock()

stats_lock = threading.Lock()
buckets = {}
recent = []
totals = {
    "upload_bytes": 0,
    "download_bytes": 0,
    "upload_count": 0,
    "download_count": 0,
}

activity_lock = threading.Lock()
activity = []
activity_id = 0


def start_metadata():
    with metadata_lock:
        if not METADATA.exists() or METADATA.stat().st_size == 0:
            with open(METADATA, "w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=FIELDS)
                writer.writeheader()


def read_metadata_file():
    if not METADATA.exists():
        return []
    with open(METADATA, newline="") as file:
        return list(csv.DictReader(file))


def get_file(filename):
    with metadata_lock:
        for row in read_metadata_file():
            if row["filename"] == filename:
                return row
    return None


def list_files():
    with metadata_lock:
        rows = read_metadata_file()
    return sorted(rows, key=lambda row: row["uploaded_at"], reverse=True)


def save_metadata(filename, size, owner, sha):
    with open(METADATA, "a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writerow({
            "filename": filename,
            "size_bytes": size,
            "owner": owner,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": sha,
        })


def valid_filename(name):
    if not name:
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if "\x00" in name:
        return False
    return True


def reserve_filename(name):
    with metadata_lock:
        if name in reserved:
            return False
        if (STORAGE / name).exists():
            return False
        for row in read_metadata_file():
            if row["filename"] == name:
                return False
        reserved.add(name)
        return True


def release_filename(name):
    with metadata_lock:
        reserved.discard(name)


def remember_client(username, ip):
    with clients_lock:
        clients[username] = {
            "name": username,
            "ip": ip,
            "last_seen": time.time(),
        }


def active_clients():
    now = time.time()
    result = []
    with clients_lock:
        for username in list(clients):
            item = clients[username]
            if now - item["last_seen"] > CLIENT_TIMEOUT:
                del clients[username]
            else:
                result.append({
                    "client_id": username,
                    "name": item["name"],
                    "ip": item["ip"],
                    "last_seen": time.strftime("%H:%M:%S", time.localtime(item["last_seen"])),
                })
    return result


def record_bytes(direction, amount):
    second = int(time.time())
    with stats_lock:
        if second not in buckets:
            buckets[second] = [0, 0]
        if direction == "upload":
            buckets[second][0] += amount
            totals["upload_bytes"] += amount
        else:
            buckets[second][1] += amount
            totals["download_bytes"] += amount


def record_transfer(direction, username, filename, size, seconds):
    speed = 0
    if seconds > 0:
        speed = round(size * 8 / seconds / 1000000, 2)

    with stats_lock:
        if direction == "upload":
            totals["upload_count"] += 1
        else:
            totals["download_count"] += 1

        recent.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "direction": direction,
            "client": username,
            "filename": filename,
            "bytes": size,
            "seconds": round(seconds, 3),
            "mbps": speed,
        })
        if len(recent) > 20:
            recent.pop()


def add_activity(text):
    global activity_id
    with activity_lock:
        activity_id += 1
        activity.insert(0, {
            "id": activity_id,
            "time": time.strftime("%H:%M:%S"),
            "text": text,
        })
        if len(activity) > 100:
            activity.pop()


def activity_log(since=0):
    with activity_lock:
        events = []
        for event in activity:
            if event["id"] > since:
                events.append(event)
        events.reverse()
        return {"last": activity_id, "events": events}


def bandwidth_stats():
    now = time.time()
    with stats_lock:
        for second in list(buckets):
            if second < now - 120:
                del buckets[second]

        upload = 0
        download = 0
        for second in buckets:
            if second > now - 5:
                upload += buckets[second][0]
                download += buckets[second][1]

        history = []
        for i in range(29, -1, -1):
            second = int(now) - i
            if second in buckets:
                history.append({"upload": buckets[second][0], "download": buckets[second][1]})
            else:
                history.append({"upload": 0, "download": 0})

        return {
            "upload_rate": upload / 5,
            "download_rate": download / 5,
            "upload_bytes": totals["upload_bytes"],
            "download_bytes": totals["download_bytes"],
            "upload_count": totals["upload_count"],
            "download_count": totals["download_count"],
            "history": history,
            "recent": list(recent),
        }


def send_error(sock, code, message=""):
    send_frame(sock, "ERROR", code=code, message=message)


def check_password(sock, address):
    header, payload = recv_frame(sock)

    if header.get("type") != "AUTH":
        send_error(sock, "NOT_AUTHENTICATED", "send AUTH first")
        return None

    password_hash = hashlib.sha256(SERVER_PASSWORD.encode()).hexdigest()
    if header.get("password_sha256") != password_hash:
        send_error(sock, "AUTH_FAILED", "wrong password")
        print("wrong password from", address[0])
        add_activity("login failed from " + address[0])
        return None

    username = str(header.get("username", "unknown"))[:100]
    token = uuid.uuid4().hex
    send_frame(sock, "AUTH_OK", token=token)
    remember_client(username, address[0])
    print("client logged in:", address[0])
    return token, username


def do_upload(sock, header, username):
    filename = header.get("filename", "")
    sha = header.get("sha256", "")

    try:
        size = int(header.get("size", -1))
    except (TypeError, ValueError):
        send_error(sock, "BAD_SIZE", "bad file size")
        return

    if not valid_filename(filename):
        send_error(sock, "BAD_FILENAME", "bad filename")
        return
    if size < 0 or size > MAX_FILE_SIZE:
        send_error(sock, "BAD_SIZE", "file is too big")
        return
    if not reserve_filename(filename):
        send_error(sock, "FILE_EXISTS", "file already exists")
        return

    temp = STORAGE / ("." + uuid.uuid4().hex + ".part")
    final = STORAGE / filename
    start = time.monotonic()

    try:
        send_frame(sock, "OK")

        digest = hashlib.sha256()
        total = 0

        with open(temp, "wb") as file:
            while True:
                frame, data = recv_frame(sock)
                if frame.get("type") == "CHUNK":
                    file.write(data)
                    digest.update(data)
                    total += len(data)
                    record_bytes("upload", len(data))
                elif frame.get("type") == "UPLOAD_DONE":
                    break
                else:
                    raise ValueError("unexpected message")

        if total != size:
            raise ValueError("size does not match")
        if digest.hexdigest() != sha:
            raise ValueError("sha256 does not match")

        with metadata_lock:
            try:
                os.replace(temp, final)
                save_metadata(filename, size, username, sha)
            except Exception:
                if final.exists():
                    final.unlink()
                raise

        send_frame(sock, "UPLOAD_OK", filename=filename, size=size, sha256=sha)
        record_transfer("upload", username, filename, size, time.monotonic() - start)
        add_activity(username + " uploaded " + filename + " (" + str(size) + " bytes)")
        print("uploaded", filename)

    except Exception as error:
        if temp.exists():
            temp.unlink()
        send_error(sock, "UPLOAD_FAILED", str(error))
        add_activity("upload of " + (filename or "file") + " failed")
        print("upload failed:", error)

    finally:
        release_filename(filename)


def do_download(sock, header, username):
    filename = header.get("filename", "")

    if not valid_filename(filename):
        send_error(sock, "BAD_FILENAME", "bad filename")
        return

    path = STORAGE / filename
    if get_file(filename) is None or not path.is_file():
        send_error(sock, "FILE_NOT_FOUND", "file not found")
        return

    size = path.stat().st_size
    sha = sha256_file(path)
    start = time.monotonic()

    send_frame(sock, "DOWNLOAD_INIT", filename=filename, size=size, sha256=sha)

    number = 0
    with open(path, "rb") as file:
        while True:
            data = file.read(CHUNK_SIZE)
            if not data:
                break
            send_frame(sock, "CHUNK", data, seq=number)
            record_bytes("download", len(data))
            number += 1

    send_frame(sock, "DOWNLOAD_DONE", chunks=number)
    record_transfer("download", username, filename, size, time.monotonic() - start)
    add_activity(username + " downloaded " + filename + " (" + str(size) + " bytes)")
    print("downloaded", filename)


def do_list(sock):
    files = []
    for row in list_files():
        files.append({
            "filename": row["filename"],
            "size": int(row["size_bytes"]),
            "owner": row["owner"],
            "upload_time": row["uploaded_at"],
            "sha256": row["sha256"],
        })
    send_frame(sock, "LIST_RESP", json.dumps(files).encode())


def handle_client(sock, address):
    print("connected:", address)
    sock.settimeout(60)

    try:
        login = check_password(sock, address)
        if login is None:
            return

        token, username = login

        while True:
            header, payload = recv_frame(sock)
            kind = header.get("type")

            if kind == "QUIT":
                send_frame(sock, "OK")
                return

            if kind in ("LIST", "UPLOAD_INIT", "DOWNLOAD_REQ"):
                if header.get("token") != token:
                    send_error(sock, "NOT_AUTHENTICATED", "bad token")
                    continue

            if kind == "LIST":
                do_list(sock)
            elif kind == "UPLOAD_INIT":
                do_upload(sock, header, username)
            elif kind == "DOWNLOAD_REQ":
                do_download(sock, header, username)
            else:
                send_error(sock, "UNKNOWN_COMMAND", "unknown command")

    except (ConnectionError, OSError, ProtocolError, ValueError) as error:
        print("connection error:", error)
    except Exception as error:
        print("unexpected error:", error)
    finally:
        sock.close()
        print("closed:", address)


def serve():
    start_metadata()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(50)

    print("server listening on port", PORT)
    print("files are in", STORAGE)

    while True:
        sock, address = server.accept()
        thread = threading.Thread(target=handle_client, args=(sock, address))
        thread.daemon = True
        thread.start()


if __name__ == "__main__":
    serve()
