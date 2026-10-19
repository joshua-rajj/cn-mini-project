"""Wire protocol shared by the server and the client.

Every message is:

    MAGIC (4 bytes) | header length (4 bytes, big endian) | JSON header | payload

The JSON header always has "type" and "payload_len". Extra fields depend on
the message type (for example "token" or "filename").
"""

import json
import struct

MAGIC        = b"\xC0\xDE\x00\x01"   # marker + version 1
MAX_HEADER   = 1 << 20               # 1 MiB
MAX_PAYLOAD  = 8 << 20               # 8 MiB
CHUNK_SIZE   = 64 * 1024             # 64 KiB


class ProtocolError(Exception):
    """Raised on any malformed frame. Fatal to the connection."""


def recv_exact(sock, n):
    """Read exactly n bytes, or raise. The one correct way to read a socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(
                f"peer closed after {len(buf)}/{n} bytes"
            )
        buf += chunk
    return bytes(buf)


def send_frame(sock, msg_type, payload=b"", **fields):
    """Pack and send one frame."""
    header = {"type": msg_type, "payload_len": len(payload)}
    header.update(fields)
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER:
        raise ProtocolError(f"header too large: {len(raw)}")
    sock.sendall(MAGIC + struct.pack(">I", len(raw)) + raw + payload)


def recv_frame(sock):
    """Read exactly one frame. Returns (header_dict, payload_bytes)."""
    magic = recv_exact(sock, 4)
    if magic != MAGIC:
        raise ProtocolError(f"bad magic {magic!r}")

    (header_len,) = struct.unpack(">I", recv_exact(sock, 4))
    if header_len > MAX_HEADER:
        raise ProtocolError(f"header too large: {header_len}")

    header = json.loads(recv_exact(sock, header_len).decode("utf-8"))

    payload_len = header.get("payload_len", 0)
    if not isinstance(payload_len, int) or payload_len < 0:
        raise ProtocolError(f"bad payload_len {payload_len!r}")
    if payload_len > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large: {payload_len}")

    payload = recv_exact(sock, payload_len) if payload_len else b""
    return header, payload


def sha256_file(path, block=1 << 20):
    """Lowercase hex SHA-256 of a file on disk."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block_bytes in iter(lambda: f.read(block), b""):
            h.update(block_bytes)
    return h.hexdigest()
