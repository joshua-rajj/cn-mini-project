import socket
import csv
import os
import uuid
import threading
from pathlib import Path
from datetime import datetime, timezone

# ---------------- CONFIGURATION ----------------

HOST = "0.0.0.0"
PORT = 5000
MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB
CHUNK_SIZE = 65536
Server_password="pass123"
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
CSV_PATH = BASE_DIR / "metadata.csv"

STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Protects CSV access and filename reservations across client threads.
metadata_lock = threading.Lock()
reserved_filenames = set()


# ---------------- CSV METADATA ----------------

CSV_FIELDS = ["filename", "size_bytes", "owner", "uploaded_at"]


def init_metadata():
    """Create the metadata CSV with a header if it does not exist."""
    with metadata_lock:
        if not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0:
            with CSV_PATH.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                writer.writeheader()


def read_all_metadata_unlocked():
    """Read all metadata. Caller must hold metadata_lock."""
    if not CSV_PATH.exists():
        return []

    with CSV_PATH.open("r", newline="", encoding="utf-8") as csv_file:
        return list(csv.DictReader(csv_file))


def get_file_metadata(filename):
    """Return one metadata dictionary, or None if not found."""
    with metadata_lock:
        for record in read_all_metadata_unlocked():
            if record.get("filename") == filename:
                return record
    return None


def list_file_metadata():
    """Return all metadata records, newest first."""
    with metadata_lock:
        records = read_all_metadata_unlocked()
    return sorted(
        records,
        key=lambda row: row.get("uploaded_at", ""),
        reverse=True
    )


def save_metadata(filename, size, owner):
    """Append one metadata record. Caller must hold metadata_lock."""
    with CSV_PATH.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writerow({
            "filename": filename,
            "size_bytes": size,
            "owner": owner ,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
        })


# ---------------- TCP HELPERS ----------------

def receive_line(client):
    """Receive one newline-terminated UTF-8 field."""
    data = bytearray()

    while True:
        byte = client.recv(1)

        if not byte:
            raise ConnectionError("Client disconnected before completing a field")

        if byte == b"\n":
            return data.decode("utf-8")

        data.extend(byte)

        if len(data) > 4096:
            raise ValueError("Request field is too long")


def send_line(client, message):
    client.sendall((message + "\n").encode("utf-8"))


def receive_exact_file(client, path, size):
    """Receive exactly size bytes and write them to path."""
    remaining = size

    with open(path, "wb") as file:
        while remaining > 0:
            chunk = client.recv(min(CHUNK_SIZE, remaining))

            if not chunk:
                raise ConnectionError("Incomplete file transfer")

            file.write(chunk)
            remaining -= len(chunk)


def send_file(client, path):
    """Send OK, file size, and then the exact file bytes."""
    size = path.stat().st_size

    send_line(client, "OK")
    send_line(client, str(size))

    with path.open("rb") as file:
        while True:
            chunk = file.read(CHUNK_SIZE)
            if not chunk:
                break
            client.sendall(chunk)


# ---------------- VALIDATION / RESERVATION ----------------

def valid_filename(filename):
    return (
        bool(filename)
        and filename not in (".", "..")
        and Path(filename).name == filename
        and "/" not in filename
        and "\\" not in filename
        and "\x00" not in filename
    )


def reserve_filename(filename):
    """
    Atomically check whether a filename exists and reserve it.
    Returns False if it already exists or is being uploaded.
    """
    with metadata_lock:
        if filename in reserved_filenames:
            return False

        if (STORAGE_DIR / filename).exists():
            return False

        if any(
            record.get("filename") == filename
            for record in read_all_metadata_unlocked()
        ):
            return False

        reserved_filenames.add(filename)
        return True


def release_filename(filename):
    with metadata_lock:
        reserved_filenames.discard(filename)


# ---------------- REQUEST HANDLERS ----------------

def handle_upload(client, filename, size, owner):
    if not reserve_filename(filename):
        send_line(client, "ERR")
        send_line(client, "File already exists or is being uploaded")
        return

    temporary = STORAGE_DIR / f".{uuid.uuid4().hex}.part"
    destination = STORAGE_DIR / filename
    final_file_created = False

    try:
        # Client must wait for READY before sending file bytes.
        send_line(client, "READY")
        receive_exact_file(client, temporary, size)

        # Hold the lock while publishing the file and adding metadata.
        # This prevents list/download/other upload checks from observing
        # the short interval between those two operations.
        with metadata_lock:
            if destination.exists():
                raise FileExistsError("Destination file already exists")

            os.replace(temporary, destination)
            final_file_created = True

            try:
                save_metadata(filename, size,owner)
            except Exception:
                destination.unlink(missing_ok=True)
                final_file_created = False
                raise

        send_line(client, "OK")
        print(f"Upload completed: {filename} ({size} bytes)")

    except Exception:
        # If transfer or metadata saving fails, do not leave an incomplete
        # final file. The handler logs the error and closes the connection.
        if final_file_created:
            with metadata_lock:
                destination.unlink(missing_ok=True)
        raise

    finally:
        temporary.unlink(missing_ok=True)
        release_filename(filename)


def handle_download(client,filename):
    # Read metadata under lock; then open the file. Completed uploads are
    # published only after all bytes have been received.
    metadata = get_file_metadata(filename)

    if metadata is None:
        send_line(client, "ERR")
        send_line(client, "File not found")
        return

    path = STORAGE_DIR / filename

    if not path.is_file():
        send_line(client, "ERR")
        send_line(client, "Stored file is missing")
        return

    send_file(client, path)
    print(f"Download completed: {filename}")


def handle_list(client):
    records = list_file_metadata()
    for record in records:
        send_line(client,str(record))
    
    send_line(client,"END")

def authenticate_client(client,address):
    send_line(client,"Enter password: ")
    password = receive_line(client)
    if not Server_password==password:
        send_line(client,"ERR")
        send_line(client,"Invalid Password")
        print("Authentication failed for client ip: ",address[0])
        return False
    
    send_line(client,"OK")
    print("Authenticated client IP:",address[0])
    return True

def handle_client(client, address):
    print(f"\nClient connected: {address}")
    

    try:
        if not authenticate_client(client,address):
            return
        command = receive_line(client).upper()

        if command == "UPLOAD":
            filename = receive_line(client)
            size_text = receive_line(client)

            if not valid_filename(filename):
                send_line(client, "ERR")
                send_line(client, "Invalid filename")
                return

            try:
                size = int(size_text)
            except ValueError:
                send_line(client, "ERR")
                send_line(client, "Invalid file size")
                return

            if not 0 <= size <= MAX_FILE_SIZE:
                send_line(client, "ERR")
                send_line(client, "File size is outside the allowed limit")
                return

            handle_upload(client, filename, size,address[0])

        elif command == "DOWNLOAD":
            filename = receive_line(client)

            if not valid_filename(filename):
                send_line(client, "ERR")
                send_line(client, "Invalid filename")
                return

            handle_download(client, filename)

        elif command == "LIST":
            handle_list(client)

        else:
            send_line(client, "ERR")
            send_line(client, "Unknown command")

    except (ConnectionError, OSError, ValueError) as error:
        print(f"Request failed for {address}: {error}")
    except Exception as error:
        print(f"Unexpected error for {address}: {error}")

    finally:
        client.close()
        print(f"Connection closed: {address}")


# ---------------- MAIN SERVER ----------------

def main():
    init_metadata()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen(50)

        print(f"Server listening on {HOST}:{PORT}")
        print(f"File storage: {STORAGE_DIR}")
        print(f"Metadata CSV: {CSV_PATH}")
        print("Concurrent client handling is enabled.")

        while True:
            client, address = server_socket.accept()

            client_thread = threading.Thread(
                target=handle_client,
                args=(client,address),
                daemon=True
            )
            client_thread.start()


if __name__ == "__main__":
    main()