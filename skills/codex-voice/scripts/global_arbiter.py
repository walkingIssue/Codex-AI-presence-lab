"""One user-level playback arbiter shared by every Codex voice runtime.

Project watchers remain adapters: they keep their rollout cursors and local
diagnostics, but speech ownership, attention, session announcements, and the
single warm Kokoro worker live here.  The transport is a localhost Unix socket
on POSIX and a loopback TCP endpoint on Windows.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid


SOCKET_NAME = "codex-voice-arbiter.sock"
DEFAULT_TCP_PORT = 37831
MAX_LINE = 2 * 1024 * 1024


def code_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def state_root() -> Path:
    configured = os.environ.get("CODEX_VOICE_ARBITER_HOME")
    root = Path(configured).expanduser() if configured else code_home() / "voice"
    root.mkdir(parents=True, exist_ok=True)
    return root


def unix_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime).expanduser() if runtime else state_root()
    return root / SOCKET_NAME


def tcp_port() -> int:
    configured = os.environ.get("CODEX_VOICE_ARBITER_PORT")
    try:
        value = int(configured) if configured else DEFAULT_TCP_PORT
    except ValueError:
        value = DEFAULT_TCP_PORT
    return value if 1024 <= value <= 65535 else DEFAULT_TCP_PORT


def use_unix_socket() -> bool:
    return os.name != "nt" and hasattr(socket, "AF_UNIX")


def orb_port_for_root(voice_root: Path) -> int:
    configured = os.environ.get("CODEX_ORB_PORT")
    try:
        value = int(configured) if configured else 0
    except ValueError:
        value = 0
    if 1024 <= value <= 65535:
        return value
    digest = hashlib.sha256(str(voice_root.expanduser().resolve()).encode("utf-8")).digest()
    return 20000 + int.from_bytes(digest[:4], "big") % 30000


def send_udp(port: int, event: dict[str, object]) -> None:
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(json.dumps(event, separators=(",", ":")).encode("utf-8"), ("127.0.0.1", port))
    except OSError:
        pass


def environment_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def provider_runtime(voice_root: Path) -> tuple[Path, str]:
    try:
        provider = (voice_root / "provider").read_text(encoding="utf-8").strip().lower()
    except OSError:
        provider = "cpu"
    cpu = environment_python(voice_root / ".venv")
    if provider == "openvino":
        candidate = environment_python(voice_root / ".openvino-venv")
        if candidate.is_file() and (voice_root / "kokoro-v1.0.int8.onnx").is_file():
            return candidate, "openvino"
        return cpu, "cpu"
    if provider == "cuda":
        candidate = environment_python(voice_root / ".cuda-venv")
        if candidate.is_file() and (voice_root / "kokoro-v1.0.int8.onnx").is_file():
            return candidate, "cuda"
        return cpu, "cpu"
    if provider == "directml":
        candidate = environment_python(voice_root / ".dml-venv")
        if candidate.is_file() and (voice_root / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx").is_file():
            return candidate, "directml"
        return cpu, "cpu"
    return cpu, "cpu"


class GlobalTTSWorker:
    """A single persistent speak.py child owned by the arbiter daemon."""

    def __init__(self, project_root: Path, voice_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.voice_root = voice_root.resolve()
        self.process: subprocess.Popen[str] | None = None
        self.ready = False
        self.lock = threading.Lock()

    def start(self) -> bool:
        with self.lock:
            if self.process is not None and self.process.poll() is None and self.ready:
                return True
            self.close()
            python, provider = provider_runtime(self.voice_root)
            hook = self.project_root / ".codex" / "hooks" / "speak.py"
            if not python.is_file() or not hook.is_file():
                return False
            attempts: list[tuple[Path, str]] = [(python, provider)]
            if provider == "openvino":
                cpu = environment_python(self.voice_root / ".venv")
                if cpu.is_file() and cpu.absolute() != python.absolute():
                    attempts.append((cpu, "cpu"))
            for selected_python, selected_provider in attempts:
                if self._spawn(selected_python, selected_provider, hook):
                    if self._read_ready():
                        return True
            return False

    def _spawn(self, python: Path, provider: str, hook: Path) -> bool:
        environment = os.environ.copy()
        environment["CODEX_TTS_FROM_WATCHER"] = "1"
        environment["CODEX_TTS_FROM_ARBITER"] = "1"
        environment["CODEX_TTS_PROVIDER"] = provider
        try:
            self.process = subprocess.Popen(
                [str(python), str(hook), "--server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError:
            self.process = None
            return False

    def _read_ready(self) -> bool:
        if self.process is None or self.process.stdout is None:
            return False
        try:
            line = self.process.stdout.readline()
            response = json.loads(line) if line else {}
        except (OSError, json.JSONDecodeError):
            response = {}
        self.ready = bool(response.get("ready"))
        if not self.ready:
            self.close()
        return self.ready

    def send(self, item: dict[str, object]) -> str:
        if not self.start() or self.process is None or self.process.stdin is None or self.process.stdout is None:
            return "failed"
        request: dict[str, object] = {
            "hook_event_name": "Stop",
            "last_assistant_message": str(item.get("text", "")),
            "tts_pauseable": item.get("kind") not in {"session-announcement", "update"},
        }
        for source, target in (
            ("event_id", "tts_event_id"),
            ("volume", "tts_volume"),
            ("tts_voice", "tts_voice"),
            ("tts_speed", "tts_speed"),
            ("tts_mode", "tts_mode"),
            ("orb_port", "tts_orb_port"),
        ):
            if item.get(source) is not None:
                request[target] = item[source]
        try:
            self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            response = json.loads(self.process.stdout.readline() or "{}")
        except (OSError, json.JSONDecodeError):
            self.close()
            return "failed"
        if response.get("done") and response.get("ok"):
            return "completed"
        if response.get("done") and response.get("interrupted"):
            return "interrupted"
        return "failed"

    def request_stop(self) -> None:
        try:
            (self.voice_root / "tts-stop.request").write_text("stop\n", encoding="utf-8")
        except OSError:
            pass

    def close(self) -> None:
        process = self.process
        self.process = None
        self.ready = False
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
            except OSError:
                pass


@dataclass
class WorkItem:
    message: dict[str, object]
    client: "ServerClient | None"
    kind: str = "speech"


class ServerClient:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.lock = threading.Lock()

    def send(self, payload: dict[str, object]) -> None:
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.lock:
                self.connection.sendall(data)
        except OSError:
            pass


class GlobalArbiterCore:
    """Global FIFO queue and attention owner, independent of any project root."""

    def __init__(self, worker: GlobalTTSWorker) -> None:
        self.worker = worker
        self.condition = threading.Condition()
        self.queue: deque[WorkItem] = deque()
        self.pending_update: WorkItem | None = None
        self.known_event_ids: set[str] = set()
        self.event_items: dict[str, WorkItem] = {}
        self.completed_items: dict[str, dict[str, object]] = {}
        self.current: WorkItem | None = None
        self.paused: WorkItem | None = None
        self.stop_requested = False
        self.attention_owner: str | None = None
        self.planned_owner: str | None = None
        self.shutdown = False
        self.thread = threading.Thread(target=self._run, name="codex-global-playback-arbiter", daemon=True)

    @staticmethod
    def attention_key(message: dict[str, object]) -> str:
        route = message.get("route_key")
        if isinstance(route, str) and route.strip():
            return route.strip()
        session = message.get("session_id")
        profile = message.get("profile_id") or "default"
        if isinstance(session, str) and session.strip():
            return f"session:{session.strip()}|profile:{str(profile).strip() or 'default'}"
        return "unknown"

    def start(self) -> None:
        self.worker.start()
        self.thread.start()

    def enqueue(self, message: dict[str, object], client: ServerClient | None) -> bool:
        item = WorkItem(dict(message), client)
        with self.condition:
            event_id = item.message.get("event_id")
            if isinstance(event_id, str) and event_id in self.known_event_ids:
                existing = self.event_items.get(event_id)
                if existing is not None:
                    existing.client = client
                completed = self.completed_items.get(event_id)
                if completed is not None and client is not None:
                    client.send({"event": "complete", "message": completed})
                return True
            if isinstance(event_id, str) and event_id:
                self.known_event_ids.add(event_id)
                self.event_items[event_id] = item
            owner = self.attention_key(item.message)
            labels = item.message.get("session_labels")
            if (
                owner != self.planned_owner
                and labels in {"first-message", "session-change"}
                and str(item.message.get("session_label") or "").strip()
            ):
                template = str(item.message.get("session_label_template") or "{session_name} says")
                try:
                    announcement = template.format(session_name=str(item.message.get("session_label"))).strip()
                except (KeyError, ValueError):
                    announcement = f"{item.message.get('session_label')} says"
                self.queue.append(WorkItem({**item.message, "text": announcement}, client, "session-announcement"))
            self.planned_owner = owner
            if self.pending_update is not None:
                self.pending_update = None
            if self.current is not None and self.current.kind == "update":
                self.stop_requested = True
                self.worker.request_stop()
            self.queue.append(item)
            self.condition.notify_all()
        return True

    def publish_update(self, message: dict[str, object], client: ServerClient | None) -> bool:
        item = WorkItem(dict(message), client, "update")
        with self.condition:
            if self.queue:
                return False
            if self.attention_key(item.message) != self.attention_owner:
                return False
            self.pending_update = item
            if self.current is not None and self.current.kind == "update":
                self.stop_requested = True
                self.worker.request_stop()
            self.condition.notify_all()
        return True

    def interrupt(self, client: ServerClient | None) -> dict[str, object] | None:
        with self.condition:
            if self.current is None or self.current.kind not in {"speech", "update"}:
                return None
            if client is not None and self.current.client is not client:
                return None
            self.stop_requested = True
            current = dict(self.current.message)
            self.worker.request_stop()
            return current

    def wake(self) -> None:
        with self.condition:
            self.stop_requested = False
            if self.paused is not None:
                self.queue.appendleft(self.paused)
                self.paused = None
            self.condition.notify_all()

    def is_idle(self) -> bool:
        with self.condition:
            return self.current is None and not self.queue and self.pending_update is None

    def _run(self) -> None:
        while True:
            with self.condition:
                while not self.shutdown and not self.queue and self.pending_update is None:
                    self.condition.wait()
                if self.shutdown:
                    return
                if self.queue:
                    item = self.queue.popleft()
                else:
                    item = self.pending_update
                    self.pending_update = None
                self.current = item
                self.stop_requested = False
                if item is None:
                    continue
                owner = self.attention_key(item.message)
                owner_changed = owner != self.attention_owner
                self.attention_owner = owner

            message = item.message
            port = message.get("orb_port")
            try:
                port = int(port)
            except (TypeError, ValueError):
                port = 0
            send_udp(
                port,
                {
                    "type": "voice-output",
                    "state": "playing",
                    "session_id": message.get("session_id"),
                    "session_label": message.get("session_label"),
                    "profile_id": message.get("profile_id"),
                    "avatar_id": message.get("avatar_id"),
                    "route_key": message.get("route_key"),
                    "kind": item.kind,
                },
            )
            if item.client is not None:
                if owner_changed:
                    item.client.send({"event": "attention", "message": message, "owner_key": owner})
                item.client.send({"event": "start", "message": message, "kind": item.kind})
            outcome = self.worker.send({**message, "kind": item.kind})
            with self.condition:
                self.current = None
                paused = self.stop_requested and outcome == "interrupted"
                self.stop_requested = False
                if paused and item.kind == "speech":
                    self.paused = item
                elif outcome == "interrupted":
                    # Ephemeral updates can be discarded; durable speech is
                    # retried without losing the queue item.
                    if item.kind == "speech":
                        self.queue.appendleft(item)
                elif outcome != "completed" and item.kind == "speech":
                    self.queue.appendleft(item)
                self.condition.notify_all()
            if outcome == "completed" and item.kind == "speech" and item.client is not None:
                event_id = message.get("event_id")
                if isinstance(event_id, str):
                    with self.condition:
                        self.completed_items[event_id] = message
                item.client.send({"event": "complete", "message": message})
            elif outcome == "failed" and item.kind == "speech" and item.client is not None:
                item.client.send({"event": "retry", "message": message})

    def close(self) -> None:
        with self.condition:
            self.shutdown = True
            self.condition.notify_all()
        self.worker.close()


class ArbiterClient:
    """Persistent client used by project-local watcher adapters."""

    def __init__(self, project_root: Path, voice_root: Path, inbox, orb_port: int) -> None:
        self.project_root = project_root.resolve()
        self.voice_root = voice_root.resolve()
        self.inbox = inbox
        self.orb_port = orb_port
        self.connection: socket.socket | None = None
        self.reader_thread: threading.Thread | None = None
        self.write_lock = threading.Lock()
        self.pending: dict[str, tuple[threading.Event, dict[str, object]]] = {}
        self.pending_lock = threading.Lock()
        self.notifications: deque[dict[str, object]] = deque()
        self.notification_lock = threading.Lock()
        self.connected = False
        self.wake_event = _RemoteWake(self)

    def _connect_socket(self) -> socket.socket:
        if use_unix_socket():
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(str(unix_socket_path()))
            return connection
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.connect(("127.0.0.1", tcp_port()))
        return connection

    def _spawn_server(self) -> None:
        script = Path(__file__).resolve()
        log_path = state_root() / "arbiter.log"
        handle = log_path.open("a", encoding="utf-8")
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--server",
                    "--owner-project-root",
                    str(self.project_root),
                    "--owner-voice-root",
                    str(self.voice_root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            handle.close()

    def start(self) -> bool:
        if self.connected:
            return True
        connection: socket.socket | None = None
        try:
            connection = self._connect_socket()
        except OSError:
            self._spawn_server()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    connection = self._connect_socket()
                    break
                except OSError:
                    time.sleep(0.1)
        if connection is None:
            return False
        self.connection = connection
        self.connected = True
        self.reader_thread = threading.Thread(target=self._read_loop, name="codex-global-arbiter-client", daemon=True)
        self.reader_thread.start()
        response = self.request(
            "register",
            project_root=str(self.project_root),
            voice_root=str(self.voice_root),
            orb_port=self.orb_port,
        )
        if not response.get("ok"):
            self.close()
            return False
        return True

    def _read_loop(self) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            stream = connection.makefile("r", encoding="utf-8")
            for line in stream:
                if not line.strip():
                    continue
                payload = json.loads(line)
                request_id = payload.get("request_id")
                if isinstance(request_id, str):
                    with self.pending_lock:
                        entry = self.pending.get(request_id)
                        if entry is not None:
                            entry[1].update(payload)
                            entry[0].set()
                    continue
                if payload.get("event"):
                    with self.notification_lock:
                        self.notifications.append(payload)
        except (OSError, json.JSONDecodeError):
            pass
        self.connected = False

    def request(self, operation: str, **fields: object) -> dict[str, object]:
        if not self.connected or self.connection is None:
            return {"ok": False, "error": "arbiter_disconnected"}
        request_id = uuid.uuid4().hex
        event = threading.Event()
        result: dict[str, object] = {}
        with self.pending_lock:
            self.pending[request_id] = (event, result)
        payload = {"request_id": request_id, "operation": operation, **fields}
        try:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            with self.write_lock:
                self.connection.sendall(data)
            if not event.wait(30):
                return {"ok": False, "error": "arbiter_timeout"}
            return result
        except OSError:
            self.connected = False
            return {"ok": False, "error": "arbiter_disconnected"}
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def enqueue(self, message: dict[str, object]) -> bool:
        if not self.start():
            return False
        remote = dict(message)
        remote.update(
            {
                "orb_port": self.orb_port,
                "session_labels": self._session_setting("session_labels", "off"),
                "session_label_template": self._session_setting("session_label_template", "{session_name} says"),
            }
        )
        return bool(self.request("enqueue", message=remote).get("ok"))

    def sync_inbox(self) -> None:
        if not self.start():
            return
        for message in self.inbox.pending_messages():
            remote = dict(message)
            remote.update(
                {
                    "orb_port": self.orb_port,
                    "session_labels": self._session_setting("session_labels", "off"),
                    "session_label_template": self._session_setting("session_label_template", "{session_name} says"),
                }
            )
            self.request("enqueue", message=remote)

    def publish_update(self, message: dict[str, object]) -> bool:
        if not self.start():
            return False
        remote = {**message, "orb_port": self.orb_port}
        return bool(self.request("update", message=remote).get("ok"))

    def interrupt_current(self) -> dict[str, object] | None:
        if not self.start():
            return None
        response = self.request("interrupt")
        value = response.get("message")
        return value if isinstance(value, dict) else None

    def _resume(self) -> None:
        if self.connected:
            self.request("wake")

    def is_idle(self) -> bool:
        if not self.start():
            return False
        return bool(self.request("status").get("idle"))

    def drain_completed(self) -> list[dict[str, object]]:
        completed: list[dict[str, object]] = []
        with self.notification_lock:
            while self.notifications:
                event = self.notifications.popleft()
                message = event.get("message")
                if not isinstance(message, dict):
                    continue
                event_id = message.get("event_id")
                if event.get("event") == "attention":
                    self.inbox.set_state(
                        "presence_attention",
                        {
                            "state": "assigned",
                            "owner_key": event.get("owner_key"),
                            "session_id": message.get("session_id"),
                            "session_label": message.get("session_label") or "Codex",
                            "profile_id": message.get("profile_id"),
                            "avatar_id": message.get("avatar_id"),
                            "turn_id": message.get("turn_id"),
                            "event_id": message.get("event_id"),
                            "updated_at": time.time(),
                        },
                    )
                elif event.get("event") == "start":
                    self.inbox.set_state(
                        "playback",
                        {
                            "state": "playing",
                            "session_id": message.get("session_id"),
                            "session_label": message.get("session_label"),
                            "turn_id": message.get("turn_id"),
                            "profile_id": message.get("profile_id"),
                            "avatar_id": message.get("avatar_id"),
                            "route_key": message.get("route_key"),
                            "kind": event.get("kind") or message.get("kind"),
                        },
                    )
                    if event.get("kind") == "speech" and isinstance(message.get("session_id"), str):
                        self.inbox.set_state("last_session_id", message.get("session_id"))
                elif event.get("event") == "complete" and isinstance(event_id, str):
                    self.inbox.complete(event_id)
                    self.inbox.set_state("playback", {"state": "idle"})
                    completed.append(message)
                elif event.get("event") == "retry" and isinstance(event_id, str):
                    self.inbox.requeue(event_id, delay_seconds=0.25, error="global_tts_worker_failed")
        return completed

    def _session_setting(self, key: str, default: str) -> str:
        try:
            settings = json.loads((self.voice_root / "input.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}
        value = settings.get(key) if isinstance(settings, dict) else None
        return str(value) if isinstance(value, str) else default

    def close(self) -> None:
        connection = self.connection
        self.connected = False
        self.connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


class _RemoteWake:
    def __init__(self, client: ArbiterClient) -> None:
        self.client = client

    def set(self) -> None:
        self.client._resume()


class GlobalPlaybackArbiter:
    """Watcher-facing adapter with the same surface as PlaybackArbiter."""

    def __init__(self, project_root: Path, voice_root: Path, inbox) -> None:
        self.client = ArbiterClient(project_root, voice_root, inbox, orb_port_for_root(voice_root))
        self.wake_event = self.client.wake_event

    def start(self) -> bool:
        return self.client.start()

    def enqueue(self, message: dict[str, object]) -> bool:
        inserted = self.client.enqueue(message)
        if inserted:
            self.wake_event.set()
        return inserted

    def sync_inbox(self) -> None:
        self.client.sync_inbox()

    def publish_update(self, message: dict[str, object]) -> bool:
        return self.client.publish_update(message)

    def interrupt_current(self) -> dict[str, object] | None:
        return self.client.interrupt_current()

    def drain_completed(self) -> list[dict[str, object]]:
        return self.client.drain_completed()

    def is_idle(self) -> bool:
        return self.client.is_idle()

    def close(self) -> None:
        self.client.close()


def serve(owner_project_root: Path, owner_voice_root: Path) -> int:
    if use_unix_socket():
        path = unix_socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.connect(str(path))
            stale.close()
            return 0
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
    else:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", tcp_port()))
    listener.listen(16)
    core = GlobalArbiterCore(GlobalTTSWorker(owner_project_root, owner_voice_root))
    core.start()

    def handle(connection: socket.socket) -> None:
        client = ServerClient(connection)
        try:
            stream = connection.makefile("r", encoding="utf-8")
            for line in stream:
                if not line.strip():
                    continue
                request = json.loads(line)
                operation = request.get("operation")
                request_id = request.get("request_id")
                response: dict[str, object] = {"request_id": request_id, "ok": False}
                if operation == "register":
                    response.update({"ok": True, "ready": core.worker.ready})
                elif operation == "enqueue" and isinstance(request.get("message"), dict):
                    response["ok"] = core.enqueue(request["message"], client)
                elif operation == "update" and isinstance(request.get("message"), dict):
                    response["ok"] = core.publish_update(request["message"], client)
                elif operation == "interrupt":
                    response["ok"] = True
                    response["message"] = core.interrupt(client)
                elif operation == "wake":
                    core.wake()
                    response["ok"] = True
                elif operation == "status":
                    response["ok"] = True
                    response["idle"] = core.is_idle()
                client.send(response)
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    try:
        while True:
            connection, _ = listener.accept()
            threading.Thread(target=handle, args=(connection,), daemon=True).start()
    except KeyboardInterrupt:
        return 0
    finally:
        core.close()
        listener.close()
        if use_unix_socket():
            try:
                unix_socket_path().unlink(missing_ok=True)
            except OSError:
                pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--owner-project-root", type=Path)
    parser.add_argument("--owner-voice-root", type=Path)
    args = parser.parse_args()
    if not args.server or args.owner_project_root is None or args.owner_voice_root is None:
        parser.error("--server, --owner-project-root, and --owner-voice-root are required")
    return serve(args.owner_project_root.resolve(), args.owner_voice_root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
