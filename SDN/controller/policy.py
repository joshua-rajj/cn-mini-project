"""Pure policy state for the SDN controller.

Deliberately free of Ryu imports so it can be unit-tested without a network.

A *grant* is a narrow allow for one client host to reach the protected
transfer service, e.g. ``10.0.0.11 -> 10.0.0.1:9000/tcp``. Every grant owns a
cookie so the controller can remove exactly the flows it installed.
"""

from __future__ import annotations

import threading
import time


class Grant:
    __slots__ = ("src_ip", "dst_ip", "dport", "cookie", "created_at")

    def __init__(self, src_ip: str, dst_ip: str, dport: int, cookie: int):
        self.src_ip = src_ip
        self.dst_ip = dst_ip
        self.dport = dport
        self.cookie = cookie
        self.created_at = time.time()

    def key(self):
        return (self.src_ip, self.dst_ip, self.dport)

    def as_dict(self) -> dict:
        return {"src_ip": self.src_ip, "dst_ip": self.dst_ip, "dport": self.dport,
                "cookie": self.cookie, "created_at": self.created_at}


class PolicyEngine:
    """Thread-safe in-memory grant store shared with the Ryu app."""

    COOKIE_BASE = 0x1000

    def __init__(self):
        self._lock = threading.Lock()
        self._grants: dict = {}
        self._next_cookie = self.COOKIE_BASE

    def grant(self, src_ip: str, dst_ip: str, dport: int):
        """Idempotently create a grant. Returns (grant, created: bool)."""
        key = (src_ip, dst_ip, int(dport))
        with self._lock:
            existing = self._grants.get(key)
            if existing is not None:
                return existing, False
            cookie = self._next_cookie
            self._next_cookie += 1
            g = Grant(src_ip, dst_ip, int(dport), cookie)
            self._grants[key] = g
            return g, True

    def revoke(self, src_ip: str, dst_ip: str, dport: int):
        """Remove a grant. Returns the removed Grant, or None if absent."""
        key = (src_ip, dst_ip, int(dport))
        with self._lock:
            return self._grants.pop(key, None)

    def is_allowed(self, src_ip: str, dst_ip: str, dport: int) -> bool:
        with self._lock:
            return (src_ip, dst_ip, int(dport)) in self._grants

    def state(self) -> list:
        with self._lock:
            return [g.as_dict() for g in sorted(self._grants.values(), key=lambda g: g.key())]
