"""REST control plane for the PolicySwitch Ryu app.

Kept intentionally small and restricted to the trusted application host
(bind the WSGI server to 127.0.0.1). Every request is validated; the app on
the control host is the only intended caller.
"""

import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response


class PolicyRest(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.app = data["app"]

    @staticmethod
    def _json(payload, status=200):
        return Response(content_type="application/json",
                        charset="utf-8", status=status,
                        body=json.dumps(payload).encode("utf-8"))

    @staticmethod
    def _body(req):
        if not req.body:
            return {}
        return json.loads(req.body.decode("utf-8"))

    @route("policy", "/policy/grant", methods=["POST"])
    def grant(self, req, **kwargs):
        try:
            body = self._body(req)
            src_ip = str(body["src_ip"])
            dst_ip = str(body.get("dst_ip") or "0.0.0.0")
            dport = int(body["dport"])
        except (KeyError, ValueError, TypeError) as exc:
            return self._json({"ok": False, "error": f"bad request: {exc}"}, status=400)
        return self._json(self.app.grant_policy(src_ip, dst_ip, dport))

    @route("policy", "/policy/revoke", methods=["POST"])
    def revoke(self, req, **kwargs):
        try:
            body = self._body(req)
            src_ip = str(body["src_ip"])
            dst_ip = str(body.get("dst_ip") or "0.0.0.0")
            dport = int(body["dport"])
        except (KeyError, ValueError, TypeError) as exc:
            return self._json({"ok": False, "error": f"bad request: {exc}"}, status=400)
        return self._json(self.app.revoke_policy(src_ip, dst_ip, dport))

    @route("policy", "/policy/state", methods=["GET"])
    def state(self, req, **kwargs):
        return self._json({"ok": True, "grants": self.app.policy.state()})

    @route("policy", "/policy/stats", methods=["GET"])
    def stats(self, req, **kwargs):
        return self._json({"ok": True, "counters": self.app.counters,
                           "switches": list(self.app.datapaths.keys())})

    @route("policy", "/flows", methods=["GET"])
    def flows(self, req, **kwargs):
        self.app.request_flow_stats()
        return self._json({"ok": True, "flows": self.app.flow_snapshots})
