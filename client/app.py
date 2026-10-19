"""Client dashboard on port 5001.

The client talks to the file server over TCP, sends a heartbeat every few
seconds (so the server knows it is online), and can download files.
"""

import json
import os
import socket
import threading
import time

from flask import Flask, abort, jsonify, render_template, send_file

SERVER_HOST = os.environ.get("FILE_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("FILE_SERVER_PORT", "9000"))
CLIENT_NAME = os.environ.get("CLIENT_NAME", "client")
CLIENT_ID = os.environ.get("CLIENT_ID", "client1")

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "downloads")

app = Flask(__name__)


def request_server(request, timeout=10, save_as=None):
    """Send one JSON request to the file server.

    If save_as is given, the file bytes that follow the reply are saved there.
    """
    sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall((json.dumps(request) + "\n").encode())

        # Read the reply one byte at a time. If we used recv() with a big
        # buffer it could also read the first bytes of the file that follows.
        line = b""
        while not line.endswith(b"\n"):
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("no reply from server")
            line += byte
        reply = json.loads(line.decode())

        if save_as is None or not reply.get("ok"):
            return reply

        remaining = reply["size"]
        with open(save_as, "wb") as file:
            while remaining > 0:
                chunk = sock.recv(min(65536, remaining))
                if not chunk:
                    raise ConnectionError("download interrupted")
                file.write(chunk)
                remaining -= len(chunk)
        return reply
    finally:
        sock.close()


def send_heartbeat():
    while True:
        try:
            request_server({"command": "HEARTBEAT", "client_id": CLIENT_ID,
                            "name": CLIENT_NAME}, timeout=4)
        except OSError:
            pass
        time.sleep(5)


@app.route("/")
def dashboard():
    return render_template("dashboard.html", client_name=CLIENT_NAME,
                           client_id=CLIENT_ID, server_host=SERVER_HOST,
                           server_port=SERVER_PORT)


@app.route("/api/files")
def api_files():
    try:
        reply = request_server({"command": "LIST", "client_id": CLIENT_ID,
                                "name": CLIENT_NAME})
        return jsonify(reply)
    except (OSError, ValueError) as error:
        return jsonify({"ok": False, "error": str(error), "files": []}), 502


@app.route("/download/<name>")
def download(name):
    if name in ("", ".", ".."):
        abort(400)
    destination = os.path.join(DOWNLOAD_FOLDER, name)
    try:
        reply = request_server({"command": "DOWNLOAD", "client_id": CLIENT_ID,
                                "name": CLIENT_NAME, "filename": name},
                               timeout=60, save_as=destination)
    except (OSError, ValueError) as error:
        return "Download failed: %s" % error, 502
    if not reply.get("ok"):
        abort(404)
    return send_file(destination, as_attachment=True, download_name=name)


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    threading.Thread(target=send_heartbeat, daemon=True).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
