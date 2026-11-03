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
CHUNK_LOG_LIMIT = 400
CHUNK_EVENT_EVERY = 50

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
client_buckets = {}
client_totals = {}
recent = []
totals = {
    "upload_bytes": 0,
    "download_bytes": 0,
    "upload_count": 0,
    "download_count": 0,
}

chunk_lock = threading.Lock()
chunk_events = []
chunk_transfers = {}
chunk_id = 0

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


def empty_client(name):
    return {
        "name": name,
        "ip": "",
        "upload_bytes": 0,
        "download_bytes": 0,
        "upload_count": 0,
        "download_count": 0,
        "last_seen": 0,
    }


def remember_client(username, ip):
    now = time.time()
    with clients_lock:
        clients[username] = {
            "name": username,
            "ip": ip,
            "last_seen": now,
        }
    with stats_lock:
        if username not in client_totals:
            client_totals[username] = empty_client(username)
        client_totals[username]["ip"] = ip
        client_totals[username]["last_seen"] = now


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


def record_bytes(username, direction, amount):
    second = int(time.time())
    now = time.time()
    with stats_lock:
        bucket = buckets.setdefault(second, [0, 0])
        if username not in client_totals:
            client_totals[username] = empty_client(username)
        item = client_totals[username]
        client_map = client_buckets.setdefault(username, {})
        cbucket = client_map.setdefault(second, [0, 0])
        item["last_seen"] = now

        if direction == "upload":
            bucket[0] += amount
            cbucket[0] += amount
            totals["upload_bytes"] += amount
            item["upload_bytes"] += amount
        else:
            bucket[1] += amount
            cbucket[1] += amount
            totals["download_bytes"] += amount
            item["download_bytes"] += amount


def record_transfer(direction, username, filename, size, seconds):
    speed = 0
    if seconds > 0:
        speed = round(size * 8 / seconds / 1000000, 2)

    with stats_lock:
        if username not in client_totals:
            client_totals[username] = empty_client(username)
        item = client_totals[username]
        item["last_seen"] = time.time()

        if direction == "upload":
            totals["upload_count"] += 1
            item["upload_count"] += 1
        else:
            totals["download_count"] += 1
            item["download_count"] += 1

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


def rates_from(bucket_map, now):
    upload = 0
    download = 0
    for second, values in bucket_map.items():
        if second > now - 5:
            upload += values[0]
            download += values[1]
    return upload / 5, download / 5


def history_from(bucket_map, now):
    history = []
    for i in range(29, -1, -1):
        second = int(now) - i
        if second in bucket_map:
            history.append({"upload": bucket_map[second][0], "download": bucket_map[second][1]})
        else:
            history.append({"upload": 0, "download": 0})
    return history


def last_seen_text(epoch):
    if not epoch:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def client_info(name, item, bucket_map, online, now):
    up_rate, down_rate = rates_from(bucket_map, now)
    info = online.get(name)
    return {
        "name": name,
        "ip": item.get("ip", "") or (info["ip"] if info else ""),
        "online": name in online,
        "last_seen": info["last_seen"] if info else last_seen_text(item.get("last_seen", 0)),
        "upload_rate": up_rate,
        "download_rate": down_rate,
        "upload_bytes": item["upload_bytes"],
        "download_bytes": item["download_bytes"],
        "upload_count": item["upload_count"],
        "download_count": item["download_count"],
        "history": history_from(bucket_map, now),
    }


def bandwidth_stats(client=None):
    now = time.time()
    online = {}
    for item in active_clients():
        online[item["name"]] = item

    with stats_lock:
        for second in list(buckets):
            if second < now - 120:
                del buckets[second]

        for name in list(client_buckets):
            bucket_map = client_buckets[name]
            for second in list(bucket_map):
                if second < now - 120:
                    del bucket_map[second]
            if not bucket_map:
                del client_buckets[name]

        if client:
            item = client_totals.get(client, empty_client(client))
            data = client_info(client, item, client_buckets.get(client, {}), online, now)
            data["recent"] = [row for row in recent if row["client"] == client][:20]
            data["clients"] = []
            return data

        upload_rate, download_rate = rates_from(buckets, now)
        names = set(client_totals)
        names.update(client_buckets)
        names.update(online)

        clients_out = []
        for name in sorted(names):
            item = client_totals.get(name, empty_client(name))
            clients_out.append(client_info(name, item, client_buckets.get(name, {}), online, now))

        return {
            "upload_rate": upload_rate,
            "download_rate": download_rate,
            "upload_bytes": totals["upload_bytes"],
            "download_bytes": totals["download_bytes"],
            "upload_count": totals["upload_count"],
            "download_count": totals["download_count"],
            "history": history_from(buckets, now),
            "recent": list(recent),
            "clients": clients_out,
        }


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


def start_chunk_transfer(client, direction, filename, size):
    total_chunks = 0
    if size > 0:
        total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    transfer_id = uuid.uuid4().hex[:10]
    now = time.time()
    with chunk_lock:
        chunk_transfers[transfer_id] = {
            "id": transfer_id,
            "client": client,
            "direction": direction,
            "filename": filename,
            "size": size,
            "bytes": 0,
            "chunks": 0,
            "total_chunks": total_chunks,
            "done": False,
            "ok": True,
            "error": "",
            "started": now,
            "updated": now,
            "ended": 0,
        }
        prune_chunk_transfers()
    return transfer_id


def update_chunk_transfer(transfer_id, size):
    with chunk_lock:
        item = chunk_transfers.get(transfer_id)
        if item:
            item["chunks"] += 1
            item["bytes"] += size
            item["updated"] = time.time()


def finish_chunk_transfer(transfer_id, ok=True, error=""):
    with chunk_lock:
        item = chunk_transfers.get(transfer_id)
        if item and not item["done"]:
            item["done"] = True
            item["ok"] = ok
            item["error"] = error[:200]
            item["updated"] = time.time()
            item["ended"] = item["updated"]
            prune_chunk_transfers()


def prune_chunk_transfers():
    now = time.time()
    done = []
    for item in chunk_transfers.values():
        if item["done"]:
            done.append(item)
    done.sort(key=lambda item: item["ended"])
    while len(done) > 10:
        old = done.pop(0)
        chunk_transfers.pop(old["id"], None)
    for transfer_id in list(chunk_transfers):
        item = chunk_transfers[transfer_id]
        if item["done"] and now - item["ended"] > 120:
            del chunk_transfers[transfer_id]


def add_chunk_event(client, direction, filename, number, size, total_chunks):
    global chunk_id
    if number != 0 and number % CHUNK_EVENT_EVERY != 0:
        if total_chunks <= 0 or number != total_chunks - 1:
            return

    if direction == "upload":
        text = client + " -> server: chunk " + str(number + 1) + " received"
    else:
        text = "server -> " + client + ": chunk " + str(number + 1) + " sent"

    with chunk_lock:
        chunk_id += 1
        chunk_events.insert(0, {
            "id": chunk_id,
            "time": time.strftime("%H:%M:%S"),
            "client": client,
            "direction": direction,
            "filename": filename,
            "chunk": number + 1,
            "size": size,
            "text": text,
        })
        if len(chunk_events) > CHUNK_LOG_LIMIT:
            chunk_events.pop()


def chunk_stats(client=None):
    with chunk_lock:
        events = []
        for event in chunk_events:
            if client is None or event["client"] == client:
                events.append(dict(event))
        transfers = []
        for item in chunk_transfers.values():
            if client is None or item["client"] == client:
                transfers.append(dict(item))
    transfers.sort(key=lambda item: (item["done"], -item["started"]))
    return {"events": events[:120], "transfers": transfers[:12]}


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
    total_chunks = 0
    if size > 0:
        total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    transfer_id = start_chunk_transfer(username, "upload", filename, size)

    try:
        send_frame(sock, "OK")

        digest = hashlib.sha256()
        total = 0
        number = 0

        with open(temp, "wb") as file:
            while True:
                frame, data = recv_frame(sock)
                if frame.get("type") == "CHUNK":
                    file.write(data)
                    digest.update(data)
                    total += len(data)
                    record_bytes(username, "upload", len(data))
                    update_chunk_transfer(transfer_id, len(data))
                    add_chunk_event(username, "upload", filename, number, len(data), total_chunks)
                    number += 1
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

        finish_chunk_transfer(transfer_id, True)
        send_frame(sock, "UPLOAD_OK", filename=filename, size=size, sha256=sha)
        record_transfer("upload", username, filename, size, time.monotonic() - start)
        add_activity(username + " uploaded " + filename + " (" + str(size) + " bytes)")
        print("uploaded", filename)

    except Exception as error:
        finish_chunk_transfer(transfer_id, False, str(error))
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
    total_chunks = 0
    if size > 0:
        total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    transfer_id = start_chunk_transfer(username, "download", filename, size)

    try:
        send_frame(sock, "DOWNLOAD_INIT", filename=filename, size=size, sha256=sha)

        number = 0
        with open(path, "rb") as file:
            while True:
                data = file.read(CHUNK_SIZE)
                if not data:
                    break
                send_frame(sock, "CHUNK", data, seq=number)
                record_bytes(username, "download", len(data))
                update_chunk_transfer(transfer_id, len(data))
                add_chunk_event(username, "download", filename, number, len(data), total_chunks)
                number += 1

        send_frame(sock, "DOWNLOAD_DONE", chunks=number)
        finish_chunk_transfer(transfer_id, True)
        record_transfer("download", username, filename, size, time.monotonic() - start)
        add_activity(username + " downloaded " + filename + " (" + str(size) + " bytes)")
        print("downloaded", filename)

    except Exception as error:
        finish_chunk_transfer(transfer_id, False, str(error))
        raise


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
