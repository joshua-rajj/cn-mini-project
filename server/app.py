import json
import os
import socket
import threading
from flask import Flask, jsonify, render_template
from tcp_server import PORT, serve_forever

app = Flask(__name__)


def tcp_request(payload):
    with socket.create_connection(("127.0.0.1", PORT), timeout=5) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        reader = sock.makefile("rb")
        line = reader.readline()
        if not line:
            raise ConnectionError("TCP server returned no response")
        return json.loads(line.decode("utf-8"))


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/clients")
def api_clients():
    try:
        result = tcp_request({"command": "CLIENTS"})
        return jsonify(result)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc), "clients": []}), 503


if __name__ == "__main__":
    threading.Thread(target=serve_forever, daemon=True).start()
    app.run(host=os.environ.get("SERVER_HOST_BIND", "0.0.0.0"), port=5000,
            debug=False)