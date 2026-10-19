"""Server dashboard on port 5000.

Also:
  * starts the TCP file server (tcp_server.py) in a background thread
  * checks the login password and tells the SDN controller to allow or
    block that client's IP
"""

import json
import os
import threading
import urllib.request

from flask import Flask, jsonify, render_template, request

from tcp_server import SERVER_PASSWORD as PASSWORD, active_clients, serve

CONTROLLER = os.environ.get("CONTROLLER_URL", "http://10.0.0.254:8080")

app = Flask(__name__)


def call_controller(path, ip):
    """Ask the Ryu controller to grant or revoke one client IP."""
    body = json.dumps({"ip": ip}).encode()
    req = urllib.request.Request(CONTROLLER + path, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read())


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/clients")
def api_clients():
    return jsonify({"ok": True, "clients": active_clients()})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") != PASSWORD:
        return jsonify({"ok": False, "error": "wrong password"}), 401

    ip = request.remote_addr
    try:
        call_controller("/grant", ip)
    except OSError as error:
        return jsonify({"ok": False, "error": "controller: %s" % error}), 502

    print("[server] login from %s" % ip)
    return jsonify({"ok": True, "ip": ip})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    ip = request.remote_addr
    try:
        call_controller("/revoke", ip)
    except OSError as error:
        return jsonify({"ok": False, "error": "controller: %s" % error}), 502

    print("[server] logout from %s" % ip)
    return jsonify({"ok": True, "ip": ip})


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
