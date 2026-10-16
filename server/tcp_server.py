import json
import socket
import threading
import time
from pathlib import Path

HOST = "0.0.0.0"
PORT = 9000
SHARED_DIR = Path(__file__).resolve().parent / "storage"
HEARTBEAT_TIMEOUT = 30
_clients = {}
_clients_lock = threading.Lock()


def send_json(conn, data):
    conn.sendall((json.dumps(data) + "\n").encode("utf-8"))


def receive_json(reader):
    line = reader.readline()
    if not line:
        raise ConnectionError("Client disconnected")
    return json.loads(line.decode("utf-8"))


def safe_file_path(filename):
    if not isinstance(filename, str) or filename in ("", ".", ".."):
        return None
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        return None
    path = (SHARED_DIR / filename).resolve()
    if path.parent != SHARED_DIR.resolve() or not path.is_file():
        return None
    return path


def list_shared_files():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for path in sorted(SHARED_DIR.iterdir(), key=lambda p: p.name.lower()):
        if path.is_file() and path.name != "README.txt":
            stat = path.stat()
            result.append({
                "name": path.name,
                "size": stat.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            })
    return result


def update_client(request, addr):
    client_id = str(request.get("client_id", "unknown"))[:100]
    name = str(request.get("name", client_id))[:100]
    with _clients_lock:
        _clients[client_id] = {
            "client_id": client_id,
            "name": name,
            "ip": addr[0],
            "last_seen": time.time(),
        }


def get_active_clients():
    now = time.time()
    with _clients_lock:
        expired = [key for key, item in _clients.items()
                   if now - item["last_seen"] > HEARTBEAT_TIMEOUT]
        for key in expired:
            del _clients[key]
        return [
            {
                "client_id": item["client_id"],
                "name": item["name"],
                "ip": item["ip"],
                "last_seen": time.strftime("%H:%M:%S", time.localtime(item["last_seen"])),
            }
            for item in sorted(_clients.values(), key=lambda item: item["name"].lower())
        ]


def handle_client(conn, addr):
    try:
        conn.settimeout(15)
        reader = conn.makefile("rb")
        request = receive_json(reader)
        command = request.get("command")

        if command in ("REGISTER", "HEARTBEAT", "LIST", "DOWNLOAD"):
            update_client(request, addr)

        if command in ("REGISTER", "HEARTBEAT"):
            send_json(conn, {"ok": True, "message": "Client registered"})
        elif command == "LIST":
            send_json(conn, {"ok": True, "files": list_shared_files()})
        elif command == "CLIENTS":
            send_json(conn, {"ok": True, "clients": get_active_clients()})
        elif command == "DOWNLOAD":
            path = safe_file_path(request.get("filename"))
            if path is None:
                send_json(conn, {"ok": False, "error": "File not found"})
                return
            send_json(conn, {"ok": True, "filename": path.name, "size": path.stat().st_size})
            with path.open("rb") as file:
                while True:
                    chunk = file.read(64 * 1024)
                    if not chunk:
                        break
                    conn.sendall(chunk)
        else:
            send_json(conn, {"ok": False, "error": "Unknown command"})
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[TCP] {addr[0]}: {exc}")
    finally:
        conn.close()


def serve_forever():
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen()
        print(f"[TCP] Listening on {HOST}:{PORT}")
        print(f"[TCP] Shared folder: {SHARED_DIR}")
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    serve_forever()