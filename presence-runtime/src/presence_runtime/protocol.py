"""Length-prefixed UTF-8 JSON over Windows named pipes or Unix sockets."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import threading
from dataclasses import dataclass
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from typing import Any, Mapping

from .errors import ConflictError, ValidationError
from .paths import codex_home, presence_home


MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RuntimeAddress:
    transport: str
    address: str


def default_address() -> RuntimeAddress:
    if os.name == "nt":
        identity = hashlib.sha256(str(codex_home()).encode("utf-8")).hexdigest()[:16]
        return RuntimeAddress("named-pipe", rf"\\.\pipe\codex-presence-v02-{identity}")
    return RuntimeAddress("unix", str(presence_home() / "presence.sock"))


def _encode(document: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError("IPC document is not JSON serializable") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise ValidationError("IPC frame exceeds the 8 MiB limit")
    return payload


def _decode(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_FRAME_BYTES:
        raise ValidationError("IPC frame exceeds the 8 MiB limit")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("IPC frame is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValidationError("IPC frame must contain a JSON object")
    return document


class FramedConnection:
    def recv(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def send(self, document: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SocketFramedConnection(FramedConnection):
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self._send_lock = threading.Lock()

    def _read_exact(self, size: int) -> bytes | None:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self.connection.recv(remaining)
            if not chunk:
                if not chunks:
                    return None
                raise ValidationError("IPC connection closed mid-frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv(self) -> dict[str, Any] | None:
        header = self._read_exact(4)
        if header is None:
            return None
        size = struct.unpack("!I", header)[0]
        if size > MAX_FRAME_BYTES:
            raise ValidationError("IPC frame exceeds the 8 MiB limit")
        payload = self._read_exact(size)
        if payload is None:
            raise ValidationError("IPC connection closed before frame payload")
        return _decode(payload)

    def send(self, document: Mapping[str, Any]) -> None:
        payload = _encode(document)
        frame = struct.pack("!I", len(payload)) + payload
        with self._send_lock:
            self.connection.sendall(frame)

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class PipeFramedConnection(FramedConnection):
    """multiprocessing AF_PIPE provides byte framing; payloads remain UTF-8 JSON."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._send_lock = threading.Lock()

    def recv(self) -> dict[str, Any] | None:
        try:
            payload = self.connection.recv_bytes(MAX_FRAME_BYTES)
        except EOFError:
            return None
        except OSError as exc:
            raise ValidationError(f"Named-pipe receive failed: {exc}") from exc
        return _decode(payload)

    def send(self, document: Mapping[str, Any]) -> None:
        payload = _encode(document)
        with self._send_lock:
            self.connection.send_bytes(payload)

    def close(self) -> None:
        self.connection.close()


class RuntimeListener:
    def __init__(self, address: RuntimeAddress | None = None) -> None:
        self.address = address or default_address()
        self._pipe: Listener | None = None
        self._socket: socket.socket | None = None
        self._closed = False

    def open(self) -> None:
        if self._pipe is not None or self._socket is not None:
            return
        if self.address.transport == "named-pipe":
            self._pipe = Listener(self.address.address, family="AF_PIPE")
            return
        if self.address.transport != "unix":
            raise ValidationError(f"Unsupported IPC transport: {self.address.transport}")
        path = Path(self.address.address)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
            except OSError:
                path.unlink(missing_ok=True)
            else:
                raise ConflictError(f"Presence Runtime is already listening at {path}")
            finally:
                probe.close()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen()
        self._socket = listener

    def accept(self) -> FramedConnection:
        self.open()
        if self._pipe is not None:
            return PipeFramedConnection(self._pipe.accept())
        if self._socket is None:
            raise ConflictError("Runtime listener is closed")
        connection, _address = self._socket.accept()
        return SocketFramedConnection(connection)

    def close(self) -> None:
        self._closed = True
        if self._pipe is not None:
            self._pipe.close()
            self._pipe = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self.address.transport == "unix":
            Path(self.address.address).unlink(missing_ok=True)

    def __enter__(self) -> "RuntimeListener":
        self.open()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def connect(address: RuntimeAddress | None = None, *, timeout: float = 5.0) -> FramedConnection:
    selected = address or default_address()
    if selected.transport == "named-pipe":
        deadline = __import__("time").monotonic() + timeout
        while True:
            try:
                return PipeFramedConnection(Client(selected.address, family="AF_PIPE"))
            except OSError:
                if __import__("time").monotonic() >= deadline:
                    raise
                __import__("time").sleep(0.05)
    if selected.transport != "unix":
        raise ValidationError(f"Unsupported IPC transport: {selected.transport}")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    connection.connect(selected.address)
    connection.settimeout(None)
    return SocketFramedConnection(connection)
