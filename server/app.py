"""Server dashboard on port 5000.

Also starts the TCP file server (tcp_server.py) in a background thread.
"""

import threading

from flask import Flask, jsonify, render_template

from tcp_server import active_clients, serve

app = Flask(__name__)


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/clients")
def api_clients():
    return jsonify({"ok": True, "clients": active_clients()})


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
