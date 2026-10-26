import hashlib
import json
import struct

MAGIC = b"\xC0\xDE\x00\x01"
MAX_HEADER = 1024 * 1024
MAX_PAYLOAD = 8 * 1024 * 1024
CHUNK_SIZE = 64 * 1024


class ProtocolError(Exception):
    pass


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise ConnectionError("connection closed")
        data += part
    return data


def send_frame(sock, msg_type, payload=b"", **fields):
    header = {"type": msg_type, "payload_len": len(payload)}
    header.update(fields)
    header_data = json.dumps(header).encode()

    if len(header_data) > MAX_HEADER:
        raise ProtocolError("header too big")

    sock.sendall(MAGIC)
    sock.sendall(struct.pack(">I", len(header_data)))
    sock.sendall(header_data)
    sock.sendall(payload)


def recv_frame(sock):
    magic = recv_exact(sock, 4)
    if magic != MAGIC:
        raise ProtocolError("wrong magic number")

    size_data = recv_exact(sock, 4)
    header_size = struct.unpack(">I", size_data)[0]
    if header_size > MAX_HEADER:
        raise ProtocolError("header too big")

    header_data = recv_exact(sock, header_size)
    header = json.loads(header_data.decode())

    payload_size = header.get("payload_len", 0)
    if payload_size < 0 or payload_size > MAX_PAYLOAD:
        raise ProtocolError("bad payload size")

    if payload_size > 0:
        payload = recv_exact(sock, payload_size)
    else:
        payload = b""

    return header, payload


def sha256_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as file:
        while True:
            data = file.read(1024 * 1024)
            if not data:
                break
            sha.update(data)
    return sha.hexdigest()
