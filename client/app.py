"""Client dashboard on port 5001.

The client logs in with the server's password, then it can upload and
download files over the TCP connection to the server.
"""

import json
import os
import socket
import threading
import time
import urllib.request
from urllib.error import HTTPError

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file

SERVER_HOST = os.environ.get("FILE_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("FILE_SERVER_PORT", "9000"))
CLIENT_NAME = os.environ.get("CLIENT_NAME", "client")
CLIENT_ID = os.environ.get("CLIENT_ID", "client1")

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "downloads")

app = Flask(__name__)
logged_in = False


def page(error=None):
    return render_template("dashboard.html", client_name=CLIENT_NAME,
                           client_id=CLIENT_ID, server_host=SERVER_HOST,
                           server_port=SERVER_PORT, logged_in=logged_in,
                           error=error)


def read_line(sock):
    """Read one line of JSON from the socket."""
    line = b""
    while not line.endswith(b"\n"):
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("no reply from server")
        line += byte
    return json.loads(line.decode())


def request_server(request, timeout=10, save_as=None):
    """Send one JSON request to the file server.

    If save_as is given, the file bytes that follow the reply are saved there.
    """
    sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall((json.dumps(request) + "\n").encode())
        reply = read_line(sock)

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


def post_to_server(path, data):
    """Call one of the server dashboard's JSON endpoints."""
    url = "http://%s:5000%s" % (SERVER_HOST, path)
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read())
    except HTTPError as error:
        # The server answered, but with an error status (e.g. wrong password).
        return json.loads(error.read())


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
    return page()


@app.route("/login", methods=["POST"])
def login():
    global logged_in
    password = request.form.get("password", "")

    try:
        reply = post_to_server("/api/login", {"password": password})
    except OSError as error:
        return page("Cannot reach the server: %s" % error), 502

    if not reply.get("ok"):
        return page("Wrong password."), 401

    logged_in = True
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout():
    global logged_in
    try:
        post_to_server("/api/logout", {})
    except OSError:
        pass
    logged_in = False
    return redirect("/")


@app.route("/api/files")
def api_files():
    if not logged_in:
        return jsonify({"ok": False, "error": "not logged in", "files": []}), 401

    try:
        reply = request_server({"command": "LIST", "client_id": CLIENT_ID,
                                "name": CLIENT_NAME})
        return jsonify(reply)
    except (OSError, ValueError) as error:
        return jsonify({"ok": False, "error": str(error), "files": []}), 502


@app.route("/download/<name>")
def download(name):
    if not logged_in:
        abort(401)

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


@app.route("/upload", methods=["POST"])
def upload():
    if not logged_in:
        abort(401)

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return page("Choose a file first."), 400

    name = os.path.basename(uploaded.filename)
    if name != uploaded.filename or name in ("", ".", ".."):
        return page("That filename is not allowed."), 400

    # Find out how many bytes we are sending before we send them.
    stream = uploaded.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)

    sock = socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=60)
    sock.settimeout(60)
    try:
        header = {"command": "UPLOAD", "client_id": CLIENT_ID,
                  "name": CLIENT_NAME, "filename": name, "size": size}
        sock.sendall((json.dumps(header) + "\n").encode())
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            sock.sendall(chunk)
        reply = read_line(sock)
    except (OSError, ValueError) as error:
        return page("Upload failed: %s" % error), 502
    finally:
        sock.close()

    if not reply.get("ok"):
        return page("Upload failed: %s" % reply.get("error")), 500
    return redirect("/")


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    threading.Thread(target=send_heartbeat, daemon=True).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
