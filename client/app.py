import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, abort, jsonify, render_template, send_file

app = Flask(__name__)
SERVER_HOST = os.environ.get("FILE_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("FILE_SERVER_PORT", "9000"))
CLIENT_NAME = os.environ.get("CLIENT_NAME", socket.gethostname())
CLIENT_ID = os.environ.get("CLIENT_ID", str(uuid.uuid4())[:8])
# Where this client's own dashboard listens. 0.0.0.0 is required inside
# Mininet so the dashboard is reachable from outside the namespace.
CLIENT_HOST = os.environ.get("CLIENT_HOST", "127.0.0.1")
CLIENT_PORT = int(os.environ.get("CLIENT_PORT", "5001"))
DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def tcp_request(payload, timeout=10, download_to=None):
    with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        # Read the JSON header one byte at a time so buffered reads don't
        # consume bytes from the binary file that follows the header.
        header = bytearray()
        while not header.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("Server returned no response")
            header.extend(byte)
            if len(header) > 1024 * 1024:
                raise ValueError("Response header is too large")
        response = json.loads(bytes(header).decode("utf-8"))
        if not response.get("ok") or download_to is None:
            return response

        remaining = int(response["size"])
        with open(download_to, "wb") as output:
            while remaining:
                chunk = sock.recv(min(64 * 1024, remaining))
                if not chunk:
                    raise ConnectionError("Download interrupted")
                output.write(chunk)
                remaining -= len(chunk)
        return response


def client_payload(command):
    return {"command": command, "client_id": CLIENT_ID, "name": CLIENT_NAME}


def heartbeat_loop():
    while True:
        try:
            tcp_request(client_payload("HEARTBEAT"), timeout=4)
        except OSError:
            pass
        time.sleep(5)


@app.get("/")
def dashboard():
    return render_template("dashboard.html", client_name=CLIENT_NAME,
                           client_id=CLIENT_ID, server_host=SERVER_HOST,
                           server_port=SERVER_PORT)


@app.get("/api/files")
def api_files():
    try:
        result = tcp_request(client_payload("LIST"))
        return jsonify(result), 200 if result.get("ok") else 502
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc), "files": []}), 502


@app.get("/download/<path:filename>")
def download(filename):
    safe_name = Path(filename).name
    if safe_name != filename or safe_name in ("", ".", ".."):
        abort(400)
    destination = DOWNLOAD_DIR / safe_name
    try:
        result = tcp_request({**client_payload("DOWNLOAD"), "filename": safe_name},
                             timeout=60, download_to=destination)
        if not result.get("ok"):
            abort(404)
        return send_file(destination, as_attachment=True, download_name=safe_name)
    except FileNotFoundError:
        abort(404)
    except (OSError, ValueError):
        abort(502)


if __name__ == "__main__":
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    app.run(host=CLIENT_HOST, port=CLIENT_PORT, debug=False)