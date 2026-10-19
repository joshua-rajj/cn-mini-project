"""File server: a small TCP server that clients talk to on port 9000.

Every request is one line of JSON. For a download the server replies with a
JSON header (name and size) and then sends the raw file bytes.

This runs in a background thread started by app.py.
"""

import json
import os
import socket
import threading
import time

PORT = 9000
FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage")
CHUNK = 64 * 1024
CLIENT_TIMEOUT = 30   # a client is "active" for this many seconds

clients = {}          # client id -> details, shared by all threads
clients_lock = threading.Lock()


def send_json(conn, data):
    conn.sendall((json.dumps(data) + "\n").encode())


def read_json(reader):
    line = reader.readline()
    if not line:
        raise ConnectionError("client disconnected")
    return json.loads(line.decode())


def list_files():
    files = []
    for name in sorted(os.listdir(FOLDER)):
        path = os.path.join(FOLDER, name)
        if os.path.isfile(path) and name != "README.txt":
            files.append({
                "name": name,
                "size": os.path.getsize(path),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(os.path.getmtime(path))),
            })
    return files


def remember_client(request, ip):
    """Store who is online, so the server dashboard can show it."""
    client_id = str(request.get("client_id", "unknown"))[:100]
    with clients_lock:
        clients[client_id] = {
            "client_id": client_id,
            "name": str(request.get("name", client_id))[:100],
            "ip": ip,
            "last_seen": time.time(),
        }


def active_clients():
    now = time.time()
    result = []
    with clients_lock:
        for client_id in list(clients):
            item = clients[client_id]
            if now - item["last_seen"] > CLIENT_TIMEOUT:
                del clients[client_id]      # heartbeat stopped, so it left
            else:
                result.append({
                    "client_id": item["client_id"],
                    "name": item["name"],
                    "ip": item["ip"],
                    "last_seen": time.strftime("%H:%M:%S",
                                               time.localtime(item["last_seen"])),
                })
    return result


def find_file(filename):
    """Return a path inside the storage folder, or None if it is not safe."""
    if not filename or "/" in filename or "\\" in filename or filename == "..":
        return None
    path = os.path.join(FOLDER, filename)
    if os.path.isfile(path):
        return path
    return None


def handle_client(conn, addr):
    ip = addr[0]
    try:
        conn.settimeout(15)
        reader = conn.makefile("rb")
        request = read_json(reader)
        command = request.get("command")

        if command in ("REGISTER", "HEARTBEAT", "LIST", "DOWNLOAD"):
            remember_client(request, ip)

        if command in ("REGISTER", "HEARTBEAT"):
            send_json(conn, {"ok": True})
        elif command == "LIST":
            send_json(conn, {"ok": True, "files": list_files()})
        elif command == "CLIENTS":
            send_json(conn, {"ok": True, "clients": active_clients()})
        elif command == "DOWNLOAD":
            path = find_file(request.get("filename"))
            if path is None:
                send_json(conn, {"ok": False, "error": "file not found"})
            else:
                send_json(conn, {"ok": True, "filename": os.path.basename(path),
                                 "size": os.path.getsize(path)})
                with open(path, "rb") as file:
                    while True:
                        chunk = file.read(CHUNK)
                        if not chunk:
                            break
                        conn.sendall(chunk)
        else:
            send_json(conn, {"ok": False, "error": "unknown command"})
    except (OSError, ValueError) as error:
        print("[server] %s: %s" % (ip, error))
    finally:
        conn.close()


def serve():
    os.makedirs(FOLDER, exist_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen()
    print("[server] listening on port %d, sharing %s" % (PORT, FOLDER))

    while True:
        conn, addr = server.accept()
        # One thread per client, so many downloads can run at once.
        threading.Thread(target=handle_client, args=(conn, addr),
                         daemon=True).start()


if __name__ == "__main__":
    serve()
