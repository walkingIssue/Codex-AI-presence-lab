"""Connection-bound adapter client for the user Presence Runtime v0.2."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import threading
import uuid
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any


MAX_FRAME_BYTES = 8 * 1024 * 1024


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def runtime_installed() -> bool:
    return (codex_home() / "presence" / "installation.json").is_file()


def _address() -> tuple[str, str]:
    if os.name == "nt":
        identity = hashlib.sha256(str(codex_home()).encode("utf-8")).hexdigest()[:16]
        return "pipe", rf"\\.\pipe\codex-presence-v02-{identity}"
    return "unix", str(codex_home() / "presence" / "presence.sock")


class _Transport:
    def __init__(self) -> None:
        transport, address = _address()
        self.transport = transport
        if transport == "pipe":
            self.connection = Client(address, family="AF_PIPE")
        else:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(address)
            self.connection = connection

    def send(self, document: dict[str, Any]) -> None:
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise RuntimeError("Presence IPC frame is too large")
        if self.transport == "pipe":
            self.connection.send_bytes(payload)
        else:
            self.connection.sendall(struct.pack("!I", len(payload)) + payload)

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        while size:
            chunk = self.connection.recv(size)
            if not chunk:
                raise RuntimeError("Presence IPC connection closed")
            chunks.append(chunk)
            size -= len(chunk)
        return b"".join(chunks)

    def recv(self) -> dict[str, Any]:
        if self.transport == "pipe":
            payload = self.connection.recv_bytes(MAX_FRAME_BYTES)
        else:
            size = struct.unpack("!I", self._read_exact(4))[0]
            if size > MAX_FRAME_BYTES:
                raise RuntimeError("Presence IPC frame is too large")
            payload = self._read_exact(size)
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("Presence IPC response is not an object")
        return value

    def close(self) -> None:
        try:
            self.connection.close()
        except OSError:
            pass


class BindingClient:
    def __init__(
        self,
        project_root: Path,
        session_id: str | None,
        *,
        adapter: str,
        capabilities: list[str],
    ) -> None:
        self.project_root = project_root.resolve()
        self.session_id = session_id
        self.adapter = adapter
        self.capabilities = capabilities
        self.transport: _Transport | None = None
        self.registration: dict[str, Any] | None = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.lease_thread: threading.Thread | None = None

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.transport is not None and self.registration is not None:
                return self.registration
            transport = _Transport()
            request: dict[str, Any] = {
                "type": "register",
                "adapter": self.adapter,
                "project_root": str(self.project_root),
                "capabilities": self.capabilities,
            }
            if self.session_id is not None:
                request["session_id"] = self.session_id
            transport.send(request)
            response = transport.recv()
            if response.get("type") == "error":
                transport.close()
                raise RuntimeError(str(response.get("error", {}).get("message") or "registration failed"))
            if response.get("type") != "registered":
                transport.close()
                raise RuntimeError(f"Unexpected Presence registration response: {response}")
            self.transport = transport
            self.registration = response
            self.stop_event.clear()
            self.lease_thread = threading.Thread(
                target=self._lease_loop,
                name=f"presence-lease-{str(response['binding_id'])[:8]}",
                daemon=True,
            )
            self.lease_thread.start()
            return response

    def _request(self, document: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.start()
            assert self.transport is not None
            try:
                self.transport.send(document)
                response = self.transport.recv()
            except BaseException:
                self.transport.close()
                self.transport = None
                self.registration = None
                raise
            if response.get("type") == "error":
                raise RuntimeError(str(response.get("error", {}).get("message") or "Presence request failed"))
            return response

    def _lease_loop(self) -> None:
        while not self.stop_event.wait(15):
            try:
                self._request({"type": "lease/refresh"})
            except (OSError, RuntimeError):
                # The next adapter event attempts a fresh connection.  A dead
                # runtime is not replaced by a project-owned worker.
                return

    def speech(self, message: dict[str, Any]) -> bool:
        event_id = str(message["event_id"])
        utterance = str(uuid.uuid5(uuid.NAMESPACE_URL, f"presence:{event_id}"))
        response = self._request(
            {
                "type": "speech/enqueue",
                "event_id": event_id,
                "utterance_id": utterance,
                "text": str(message["text"]),
                "kind": str(message.get("kind") or "final"),
            }
        )
        return response.get("type") == "speech/enqueued" and not bool(response.get("duplicate"))

    def activity(self, state: str, event_id: str) -> bool:
        response = self._request(
            {"type": "activity", "event_id": event_id, "state": state}
        )
        return response.get("type") == "activity/accepted"

    def cancel(self, event_ids: list[str]) -> int:
        response = self._request(
            {"type": "speech/cancel", "event_ids": event_ids}
        )
        return int(response.get("cancelled", 0))

    def playback(self, operation: str) -> dict[str, Any]:
        return self._request({"type": f"playback/{operation}"})

    def status(self, event_ids: list[str] | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"type": "playback/status"}
        if event_ids:
            request["event_ids"] = event_ids
        return self._request(request)

    def pending_inputs(self) -> list[dict[str, Any]]:
        response = self._request({"type": "input/poll"})
        items = response.get("items", [])
        return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def acknowledge_input(self, input_id: str) -> None:
        self._request({"type": "input/ack", "input_id": input_id})

    def close(self) -> None:
        self.stop_event.set()
        with self.lock:
            transport = self.transport
            self.transport = None
            self.registration = None
            if transport is not None:
                try:
                    transport.send({"type": "disconnect"})
                    transport.recv()
                except (OSError, RuntimeError):
                    pass
                transport.close()
        thread = self.lease_thread
        self.lease_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)


class CallbackEvent:
    def __init__(self, callback: Any) -> None:
        self.callback = callback

    def set(self) -> None:
        self.callback()


class RuntimePlaybackAdapter:
    """Watcher playback seam backed only by authenticated v0.2 bindings."""

    def __init__(self, project_root: Path, *, adapter: str = "codex-rollout") -> None:
        self.project_root = project_root.resolve()
        self.adapter = adapter
        self.clients: dict[str | None, BindingClient] = {}
        self.accepted: list[dict[str, Any]] = []
        self.paused_session: str | None = None
        self.wake_event = CallbackEvent(self._resume)
        self.input_stop = threading.Event()
        self.input_thread: threading.Thread | None = None

    @staticmethod
    def available() -> bool:
        return runtime_installed()

    def _client(self, session_id: str | None) -> BindingClient:
        client = self.clients.get(session_id)
        if client is None:
            client = BindingClient(
                self.project_root,
                session_id,
                adapter=self.adapter,
                capabilities=["speech", "activity", "voice-input", "streaming-chunks"],
            )
            self.clients[session_id] = client
        return client

    def start(self) -> None:
        # Do not create a project binding merely to prove connectivity.  A
        # project-scoped source receives its dedicated binding only when it
        # actually emits an unscoped event; otherwise the lease would create a
        # duplicate visible window beside every active session.
        if self.input_thread is None or not self.input_thread.is_alive():
            self.input_stop.clear()
            self.input_thread = threading.Thread(
                target=self._input_loop,
                name="presence-input-delivery",
                daemon=True,
            )
            self.input_thread.start()

    def _input_mode(self) -> str:
        try:
            document = json.loads(
                (self.project_root / ".codex-voice" / "input.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            document = {}
        mode = document.get("delivery_mode") if isinstance(document, dict) else None
        return mode if mode in {"clipboard", "app-server"} else "clipboard"

    def _deliver_input(self, session_id: str, transcript: str) -> None:
        mode = self._input_mode()
        if mode == "app-server":
            from delivery import AppServerClient

            AppServerClient(self.project_root).submit(
                session_id, transcript, wait_for_completion=False
            )
            return
        from clipboard import copy_text

        copy_text(transcript)

    def _input_loop(self) -> None:
        while not self.input_stop.wait(0.4):
            for session_id, client in tuple(self.clients.items()):
                if session_id is None:
                    continue
                try:
                    items = client.pending_inputs()
                    for item in items:
                        transcript = item.get("transcript")
                        input_id = item.get("input_id")
                        if not isinstance(transcript, str) or not isinstance(input_id, str):
                            continue
                        self._deliver_input(session_id, transcript)
                        client.acknowledge_input(input_id)
                except BaseException:
                    # Delivery remains ready in SQLite and is retried; no
                    # transcript is acknowledged merely because a connector failed.
                    continue

    def enqueue(self, message: dict[str, Any]) -> bool:
        session = message.get("session_id")
        session_id = str(session) if isinstance(session, str) and session else None
        inserted = self._client(session_id).speech(message)
        if inserted:
            self.accepted.append(dict(message))
        return inserted

    def publish_update(self, message: dict[str, Any]) -> bool:
        return self.enqueue({**message, "kind": "commentary"})

    def publish_activity(self, state: str, *, session_id: str | None, event_id: str) -> bool:
        return self._client(session_id).activity(state, event_id)

    def cancel(self, session_id: str | None, event_ids: list[str]) -> int:
        return self._client(session_id).cancel(event_ids)

    def interrupt_current(self) -> dict[str, Any] | None:
        for session_id, client in tuple(self.clients.items()):
            response = client.playback("pause")
            if response.get("paused"):
                self.paused_session = session_id
                for message in reversed(self.accepted):
                    if message.get("session_id") == session_id:
                        return message
                return {"event_id": response.get("event_id"), "session_id": session_id}
        return None

    def _resume(self) -> None:
        if self.paused_session in self.clients:
            self.clients[self.paused_session].playback("resume")
        self.paused_session = None

    def drain_completed(self) -> list[dict[str, Any]]:
        completed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        grouped: dict[str | None, list[dict[str, Any]]] = {}
        for message in self.accepted:
            session = message.get("session_id")
            session_id = str(session) if isinstance(session, str) and session else None
            grouped.setdefault(session_id, []).append(message)
        terminal = {"finished", "cancelled", "failed"}
        statuses: dict[str, str] = {}
        for session_id, messages in grouped.items():
            try:
                response = self._client(session_id).status(
                    [str(item["event_id"]) for item in messages]
                )
            except (OSError, RuntimeError):
                continue
            speech = response.get("speech", {})
            if isinstance(speech, dict):
                for event_id, item in speech.items():
                    if isinstance(item, dict) and isinstance(item.get("status"), str):
                        statuses[str(event_id)] = item["status"]
        for message in self.accepted:
            if statuses.get(str(message.get("event_id"))) in terminal:
                completed.append(message)
            else:
                remaining.append(message)
        self.accepted = remaining
        return completed

    def is_idle(self) -> bool:
        if not self.clients:
            return True
        for client in self.clients.values():
            try:
                attention = client.status().get("attention", {})
            except (OSError, RuntimeError):
                return False
            if isinstance(attention, dict) and attention.get("state") != "idle":
                return False
        return True

    def sync_inbox(self) -> None:
        return

    def close(self) -> None:
        self.input_stop.set()
        thread = self.input_thread
        self.input_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        for client in tuple(self.clients.values()):
            client.close()
        self.clients.clear()
        self.accepted.clear()

    def status(self) -> dict[str, Any]:
        return {
            "runtime": "presence/v0.2",
            "bindings": [client.registration for client in self.clients.values()],
        }
