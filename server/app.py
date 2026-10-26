import json
import os
import threading
import urllib.request

from flask import Flask, jsonify, render_template, request

from tcp_server import (
    SERVER_PASSWORD,
    active_clients,
    add_activity,
    activity_log,
    bandwidth_stats,
    serve,
)

CONTROLLER = os.environ.get("CONTROLLER_URL", "http://10.0.0.254:8080")

app = Flask(__name__)


def controller_call(path, ip):
    data = json.dumps({"ip": ip}).encode()
    req = urllib.request.Request(
        CONTROLLER + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    response = urllib.request.urlopen(req, timeout=5)
    with response:
        return json.loads(response.read())


@app.route("/")
def home():
    return render_template("dashboard.html")


@app.route("/api/clients")
def client_list():
    return jsonify({"ok": True, "clients": active_clients()})


@app.route("/api/bandwidth")
def bandwidth():
    data = bandwidth_stats()
    data["ok"] = True
    return jsonify(data)


@app.route("/api/activity")
def activity():
    since = request.args.get("since", 0, type=int)
    data = activity_log(since)
    data["ok"] = True
    return jsonify(data)


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    if data.get("password") != SERVER_PASSWORD:
        return jsonify({"ok": False, "error": "wrong password"}), 401

    ip = request.remote_addr
    try:
        controller_call("/grant", ip)
    except OSError as error:
        return jsonify({"ok": False, "error": str(error)}), 502

    name = data.get("name", ip)
    add_activity(name + " logged in from " + ip)
    print("login from", ip)
    return jsonify({"ok": True, "ip": ip})


@app.route("/api/logout", methods=["POST"])
def logout():
    ip = request.remote_addr
    try:
        controller_call("/revoke", ip)
    except OSError as error:
        return jsonify({"ok": False, "error": str(error)}), 502

    data = request.get_json(silent=True) or {}
    name = data.get("name", ip)
    add_activity(name + " logged out from " + ip)
    print("logout from", ip)
    return jsonify({"ok": True, "ip": ip})


if __name__ == "__main__":
    thread = threading.Thread(target=serve)
    thread.daemon = True
    thread.start()
    app.run(host="0.0.0.0", port=5000)
