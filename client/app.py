"""Client dashboard on port 5001.

The browser login checks the password with the server and asks the SDN
controller to allow this client's IP. After that, uploads and downloads use
the Client class from client.py (the teammate code, framed protocol).
"""

import json
import os
import tempfile
import threading
import time
import urllib.request
from urllib.error import HTTPError

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file

from client import Client, ClientError

SERVER_HOST = os.environ.get("FILE_SERVER_HOST", "10.0.0.1")
SERVER_PORT = int(os.environ.get("FILE_SERVER_PORT", "9000"))
CLIENT_NAME = os.environ.get("CLIENT_NAME", "client")
CLIENT_ID = os.environ.get("CLIENT_ID", "client1")

DOWNLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "downloads")

app = Flask(__name__)
logged_in = False
session_password = None


def page(error=None):
    return render_template("dashboard.html", client_name=CLIENT_NAME,
                           client_id=CLIENT_ID, server_host=SERVER_HOST,
                           server_port=SERVER_PORT, logged_in=logged_in,
                           error=error)


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
    """Log in over TCP every 5 seconds so the server lists us as online."""
    while True:
        if logged_in and session_password:
            client = Client(SERVER_HOST, SERVER_PORT)
            try:
                client.connect()
                client.login(CLIENT_NAME, session_password)
            except (OSError, ClientError):
                pass
            finally:
                client.close()
        time.sleep(5)


@app.route("/")
def dashboard():
    return page()


@app.route("/login", methods=["POST"])
def login():
    global logged_in, session_password
    password = request.form.get("password", "")

    try:
        reply = post_to_server("/api/login", {"password": password})
    except OSError as error:
        return page("Cannot reach the server: %s" % error), 502

    if not reply.get("ok"):
        return page("Wrong password."), 401

    logged_in = True
    session_password = password
    return redirect("/")


@app.route("/logout", methods=["POST"])
def logout():
    global logged_in, session_password
    try:
        post_to_server("/api/logout", {})
    except OSError:
        pass
    logged_in = False
    session_password = None
    return redirect("/")


@app.route("/api/files")
def api_files():
    if not logged_in:
        return jsonify({"ok": False, "error": "not logged in", "files": []}), 401

    client = Client(SERVER_HOST, SERVER_PORT)
    try:
        client.connect()
        client.login(CLIENT_NAME, session_password)
        files = client.list_files()
    except (OSError, ClientError) as error:
        return jsonify({"ok": False, "error": str(error), "files": []}), 502
    finally:
        client.close()

    return jsonify({"ok": True, "files": files})


@app.route("/download/<name>")
def download(name):
    if not logged_in:
        abort(401)

    if os.path.basename(name) != name or name in ("", ".", ".."):
        abort(400)

    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    destination = os.path.join(DOWNLOAD_FOLDER, name)

    client = Client(SERVER_HOST, SERVER_PORT)
    try:
        client.connect()
        client.login(CLIENT_NAME, session_password)
        client.download(name, dest=destination)
    except (OSError, ClientError) as error:
        return "Download failed: %s" % error, 502
    finally:
        client.close()

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

    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    temporary = tempfile.NamedTemporaryFile(delete=False, dir=DOWNLOAD_FOLDER,
                                            suffix=".part")
    temporary.close()
    uploaded.save(temporary.name)

    client = Client(SERVER_HOST, SERVER_PORT)
    try:
        client.connect()
        client.login(CLIENT_NAME, session_password)
        client.upload(temporary.name, name=name)
    except (OSError, ClientError) as error:
        return page("Upload failed: %s" % error), 502
    finally:
        client.close()
        if os.path.exists(temporary.name):
            os.remove(temporary.name)

    return redirect("/")


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    threading.Thread(target=send_heartbeat, daemon=True).start()
    app.run(host="0.0.0.0", port=5001, debug=False)
