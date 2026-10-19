"""Concurrent file server (TCP, port 9000).

This is the teammate server.py structure -- CSV metadata, filename
reservation, .part temporary files and one thread per client -- but it speaks
the framed protocol from protocol.py, because that is what client.py uses.

Messages the server understands (after AUTH):
    LIST                 -> LIST_RESP (JSON list in the payload)
    UPLOAD_INIT          -> OK, then CHUNK... and UPLOAD_DONE -> UPLOAD_OK
    DOWNLOAD_REQ         -> DOWNLOAD_INIT, then CHUNK... and DOWNLOAD_DONE
    QUIT                 -> OK, then the connection closes
"""

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

# protocol.py lives in the project root, one level above this file.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from protocol import CHUNK_SIZE, ProtocolError, recv_frame, send_frame, sha256_file

# ---------------- CONFIGURATION ----------------

HOST = "0.0.0.0"
PORT = 9000
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB
SERVER_PASSWORD = os.environ.get("SERVER_PASSWORD", "pass123")
CLIENT_TIMEOUT = 30

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
CSV_PATH = BASE_DIR / "metadata.csv"

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Protects CSV access and filename reservations across client threads.
metadata_lock = threading.Lock()
reserved_filenames = set()

# Who is online, for the server dashboard.
clients = {}
clients_lock = threading.Lock()


# ---------------- CSV METADATA ----------------

CSV_FIELDS = ["filename", "size_bytes", "owner", "uploaded_at", "sha256"]


def init_metadata():
    """Create the metadata CSV with a header if it does not exist."""
    with metadata_lock:
        if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
            with CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                writer.writeheader()


def read_all_metadata_unlocked():
    """Read all metadata. Caller must hold metadata_lock."""
    if not CSV_PATH.exists():
        return []

    with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def get_file_metadata(filename):
    """Return one metadata dictionary, or None if not found."""
    with metadata_lock:
        for record in read_all_metadata_unlocked():
            if record.get("filename") == filename:
                return record
    return None


def list_file_metadata():
    """Return all metadata records, newest first."""
    with metadata_lock:
        records = read_all_metadata_unlocked()
    return sorted(records, key=lambda row: row.get("uploaded_at", ""),
                  reverse=True)


def save_metadata(filename, size, owner, sha256):
    """Append one metadata record. Caller must hold metadata_lock."""
    with CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writerow({
            "filename": filename,
            "size_bytes": size,
            "owner": owner,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": sha256,
        })


# ---------------- VALIDATION / RESERVATION ----------------

def valid_filename(filename):
    return (
        bool(filename)
        and filename not in (".", "..")
        and Path(filename).name == filename
        and "/" not in filename
        and "\\" not in filename
        and "\x00" not in filename
    )


def reserve_filename(filename):
    """
    Atomically check whether a filename exists and reserve it.
    Returns False if it already exists or is being uploaded.
    """
    with metadata_lock:
        if filename in reserved_filenames:
            return False

        if (STORAGE_DIR / filename).exists():
            return False

        if any(record.get("filename") == filename
               for record in read_all_metadata_unlocked()):
            return False

        reserved_filenames.add(filename)
        return True


def release_filename(filename):
    with metadata_lock:
        reserved_filenames.discard(filename)


# ---------------- ONLINE CLIENTS (dashboard) ----------------

def remember_client(username, ip):
    with clients_lock:
        clients[username] = {
            "client_id": username,
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
                del clients[username]        # heartbeat stopped
            else:
                result.append({
                    "client_id": item["client_id"],
                    "name": item["name"],
                    "ip": item["ip"],
                    "last_seen": time.strftime("%H:%M:%S",
                                               time.localtime(item["last_seen"])),
                })
    return result


# ---------------- AUTHENTICATION ----------------

def send_error(client, code, message=""):
    send_frame(client, "ERROR", code=code, message=message)


def authenticate_client(client, address):
    """First frame must be AUTH. Returns (token, username) or None."""
    header, _ = recv_frame(client)

    if header.get("type") != "AUTH":
        send_error(client, "NOT_AUTHENTICATED", "send AUTH first")
        return None

    expected = hashlib.sha256(SERVER_PASSWORD.encode("utf-8")).hexdigest()
    if header.get("password_sha256") != expected:
        send_error(client, "AUTH_FAILED", "invalid password")
        print("Authentication failed for client ip: ", address[0])
        return None

    username = str(header.get("username", "unknown"))[:100]
    token = uuid.uuid4().hex
    send_frame(client, "AUTH_OK", token=token)
    remember_client(username, address[0])
    print("Authenticated client IP:", address[0])
    return token, username


# ---------------- REQUEST HANDLERS ----------------

def handle_upload(client, header, username):
    filename = header.get("filename", "")
    size = int(header.get("size", -1))
    expected_sha = header.get("sha256", "")

    if not valid_filename(filename):
        send_error(client, "BAD_FILENAME", "invalid filename")
        return

    if not 0 <= size <= MAX_FILE_SIZE:
        send_error(client, "BAD_SIZE", "file size is outside the allowed limit")
        return

    if not reserve_filename(filename):
        send_error(client, "FILE_EXISTS", "File already exists or is being uploaded")
        return

    temporary = STORAGE_DIR / f".{uuid.uuid4().hex}.part"
    destination = STORAGE_DIR / filename

    try:
        # The client waits for this OK before sending file bytes.
        send_frame(client, "OK")

        digest = hashlib.sha256()
        received = 0
        chunks = 0

        with temporary.open("wb") as file:
            while True:
                frame, payload = recv_frame(client)
                kind = frame.get("type")

                if kind == "CHUNK":
                    file.write(payload)
                    digest.update(payload)
                    received += len(payload)
                    chunks += 1
                elif kind == "UPLOAD_DONE":
                    break
                else:
                    raise ValueError("unexpected frame during upload")

        if received != size:
            raise ValueError("size mismatch")
        if digest.hexdigest() != expected_sha:
            raise ValueError("sha256 mismatch")

        # Publish the file and its metadata together.
        with metadata_lock:
            os.replace(temporary, destination)
            try:
                save_metadata(filename, size, username, expected_sha)
            except Exception:
                destination.unlink(missing_ok=True)
                raise

        send_frame(client, "UPLOAD_OK", filename=filename, size=size,
                   sha256=expected_sha)
        print(f"Upload completed: {filename} ({size} bytes)")

    except Exception as error:
        temporary.unlink(missing_ok=True)
        send_error(client, "UPLOAD_FAILED", str(error))
        print(f"Upload failed: {error}")

    finally:
        release_filename(filename)


def handle_download(client, header):
    filename = header.get("filename", "")

    if not valid_filename(filename):
        send_error(client, "BAD_FILENAME", "invalid filename")
        return

    metadata = get_file_metadata(filename)
    path = STORAGE_DIR / filename

    if metadata is None or not path.is_file():
        send_error(client, "FILE_NOT_FOUND", "File not found")
        return

    size = path.stat().st_size
    digest = sha256_file(path)

    send_frame(client, "DOWNLOAD_INIT", filename=filename, size=size,
               sha256=digest)

    seq = 0
    with path.open("rb") as file:
        while True:
            block = file.read(CHUNK_SIZE)
            if not block:
                break
            send_frame(client, "CHUNK", block, seq=seq)
            seq += 1

    send_frame(client, "DOWNLOAD_DONE", chunks=seq)
    print(f"Download completed: {filename}")


def handle_list(client):
    records = []
    for row in list_file_metadata():
        records.append({
            "filename": row.get("filename"),
            "size": int(row.get("size_bytes", 0)),
            "owner": row.get("owner", ""),
            "upload_time": row.get("uploaded_at", ""),
            "sha256": row.get("sha256", ""),
        })

    send_frame(client, "LIST_RESP", json.dumps(records).encode("utf-8"))


# ---------------- CLIENT LOOP ----------------

def handle_client(client, address):
    print(f"\nClient connected: {address}")
    client.settimeout(60)

    try:
        authenticated = authenticate_client(client, address)
        if authenticated is None:
            return

        token, username = authenticated

        while True:
            header, _ = recv_frame(client)
            message = header.get("type")

            if message == "QUIT":
                send_frame(client, "OK")
                return

            if message in ("LIST", "UPLOAD_INIT", "DOWNLOAD_REQ"):
                if header.get("token") != token:
                    send_error(client, "NOT_AUTHENTICATED", "invalid token")
                    continue

            if message == "LIST":
                handle_list(client)
            elif message == "UPLOAD_INIT":
                handle_upload(client, header, username)
            elif message == "DOWNLOAD_REQ":
                handle_download(client, header)
            else:
                send_error(client, "UNKNOWN_COMMAND", str(message))

    except (ConnectionError, OSError, ProtocolError, ValueError) as error:
        print(f"Request failed for {address}: {error}")
    except Exception as error:
        print(f"Unexpected error for {address}: {error}")
    finally:
        client.close()
        print(f"Connection closed: {address}")


# ---------------- MAIN SERVER ----------------

def serve():
    init_metadata()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(50)

        print(f"Server listening on {HOST}:{PORT}")
        print(f"File storage: {STORAGE_DIR}")
        print(f"Metadata CSV: {CSV_PATH}")
        print("Concurrent client handling is enabled.")

        while True:
            client, address = server_socket.accept()
            threading.Thread(target=handle_client, args=(client, address),
                             daemon=True).start()


if __name__ == "__main__":
    serve()
