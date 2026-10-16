"""Framed TCP application protocol shared by the transfer server and client.

Wire format (protocol v1)
-------------------------
    +----------------+--------------------+----------------------+
    | header_len (4) | header JSON bytes  | payload (optional)   |
    | big-endian u32 | header_len bytes   | header["size"] bytes |
    +----------------+--------------------+----------------------+

Only the *header* is length-prefixed. When a message carries file bytes the
header declares ``size`` and exactly that many payload bytes follow. TCP is a
byte stream, so one ``recv()`` never maps 1:1 to a message -- always read with
``recv_exact()`` / the frame helpers below.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

PROTOCOL_VERSION = 1

MAX_HEADER_BYTES = 64 * 1024          # reject absurd/abusive headers
CHUNK_SIZE = 64 * 1024                # streaming copy granularity
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024

# Stable error codes returned in response headers.
ERR_BAD_FRAME = "BAD_FRAME"
ERR_BAD_COMMAND = "BAD_COMMAND"
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_TOO_LARGE = "TOO_LARGE"
ERR_BAD_NAME = "BAD_NAME"
ERR_CHECKSUM = "CHECKSUM_MISMATCH"
ERR_SIZE_MISMATCH = "SIZE_MISMATCH"
ERR_IO = "IO_ERROR"
ERR_INTERNAL = "INTERNAL"


class ProtocolError(Exception):
    """A framing/protocol violation carrying a stable error code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_response(self, request_id=None) -> dict:
        return {"v": PROTOCOL_VERSION, "id": request_id, "ok": False,
                "code": self.code, "msg": self.message}


def recv_exact(sock, n: int) -> bytes:
    """Read exactly *n* bytes or raise ConnectionError/ProtocolError."""
    if n < 0:
        raise ProtocolError(ERR_BAD_FRAME, "negative length")
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(CHUNK_SIZE, n - len(buf)))
        if not chunk:
            raise ConnectionError(f"connection closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def send_header(sock, header: dict) -> None:
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise ProtocolError(ERR_TOO_LARGE, "header too large")
    sock.sendall(struct.pack("!I", len(raw)) + raw)


def recv_header(sock) -> dict:
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    if length > MAX_HEADER_BYTES:
        raise ProtocolError(ERR_BAD_FRAME, f"header length {length} exceeds limit")
    raw = recv_exact(sock, length)
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(ERR_BAD_FRAME, f"invalid header JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise ProtocolError(ERR_BAD_FRAME, "header must be a JSON object")
    return header


def send_response(sock, request_id, ok: bool, msg: str = "",
                  extra: dict | None = None) -> None:
    header = {"v": PROTOCOL_VERSION, "id": request_id, "ok": ok, "msg": msg}
    if extra:
        header.update(extra)
    send_header(sock, header)


def send_file(sock, source, size: int) -> int:
    """Stream *size* bytes from a binary file object to the socket."""
    remaining, sent = size, 0
    while remaining > 0:
        chunk = source.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise ProtocolError(ERR_IO, "source file ended early")
        sock.sendall(chunk)
        sent += len(chunk)
        remaining -= len(chunk)
    return sent


def recv_file(sock, sink, size: int) -> int:
    """Stream *size* bytes from the socket into a binary file object."""
    remaining, received = size, 0
    while remaining > 0:
        chunk = sock.recv(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise ConnectionError(f"connection closed after {received}/{size} bytes")
        sink.write(chunk)
        received += len(chunk)
        remaining -= len(chunk)
    return received


def sanitize_filename(name) -> str | None:
    """Return a safe single-component filename, or None if unacceptable."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if name in ("", ".", "..") or len(name) > 255:
        return None
    if name.startswith("."):                       # no hidden/reserved names
        return None
    if any(bad in name for bad in ("/", "\\", "\x00")):
        return None
    if Path(name).name != name:                     # belt and braces
        return None
    return name
