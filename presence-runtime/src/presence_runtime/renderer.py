"""Python-owned lifecycle and acknowledgement bridge for one Electron root."""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .catalog import Catalog
from .models import EffectiveSnapshot
from .presentation import PresentationCue
from .resolver import PresenceResolver
from .store import PresenceStore


class ElectronRendererSupervisor:
    """Manage one Electron process and binding-keyed windows through stdin IPC."""

    def __init__(
        self,
        *,
        host_root: Path,
        catalog: Catalog,
        store: PresenceStore,
        command: Sequence[str] | None = None,
        udp_port: int = 17839,
        input_enabled: bool = False,
        input_root: Path | None = None,
        input_handler: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.host_root = host_root.expanduser().resolve()
        self.catalog = catalog
        self.store = store
        self.udp_port = udp_port
        self.command = list(command) if command is not None else self._default_command()
        self.socket_control = command is None
        self.input_enabled = input_enabled
        self.input_root = input_root
        self.input_handler = input_handler
        self.process: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._responses_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stdout_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._control_socket: socket.socket | None = None
        self._control_reader: Any | None = None
        self._control_writer: Any | None = None
        self._acknowledged: dict[str, int] = {}
        self._last_error: str | None = None

    def _default_command(self) -> list[str]:
        if os.name == "nt":
            electron = (
                self.host_root
                / "node_modules"
                / "electron"
                / "dist"
                / "electron.exe"
            )
        elif __import__("sys").platform == "darwin":
            electron = (
                self.host_root
                / "node_modules"
                / "electron"
                / "dist"
                / "Electron.app"
                / "Contents"
                / "MacOS"
                / "Electron"
            )
        else:
            electron = (
                self.host_root
                / "node_modules"
                / "electron"
                / "dist"
                / "electron"
            )
        return [str(electron), str(self.host_root)]

    def start(self, *, timeout: float = 20.0) -> bool:
        process = self.process
        if process is not None and process.poll() is None and self._ready.is_set():
            return True
        self.close()
        executable = Path(self.command[0])
        if (executable.is_absolute() or executable.parent != Path(".")) and not executable.is_file():
            self._last_error = f"renderer executable does not exist: {executable}"
            return False
        environment = os.environ.copy()
        environment["CODEX_PRESENCE_CATALOG"] = str(self.catalog.root)
        environment["CODEX_PRESENCE_UDP_PORT"] = str(self.udp_port)
        environment["CODEX_PRESENCE_INPUT_ENABLED"] = "1" if self.input_enabled else "0"
        if self.input_root is not None:
            environment["CODEX_PRESENCE_INPUT_ROOT"] = str(self.input_root)
        listener: socket.socket | None = None
        control_token: str | None = None
        if self.socket_control:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.2)
            control_token = uuid.uuid4().hex
            environment["CODEX_PRESENCE_CONTROL_PORT"] = str(listener.getsockname()[1])
            environment["CODEX_PRESENCE_CONTROL_TOKEN"] = control_token
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.host_root,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            if listener is not None:
                listener.close()
            self._last_error = str(exc)
            self.process = None
            return False
        if listener is not None:
            connection: socket.socket | None = None
            deadline = __import__("time").monotonic() + timeout
            try:
                while __import__("time").monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise RuntimeError(
                            f"renderer host exited before control registration ({self.process.returncode})"
                        )
                    try:
                        connection, _address = listener.accept()
                        break
                    except TimeoutError:
                        continue
                if connection is None:
                    raise RuntimeError("renderer control registration timed out")
                remaining = max(0.1, deadline - __import__("time").monotonic())
                connection.settimeout(remaining)
                reader = connection.makefile("r", encoding="utf-8", newline="\n")
                writer = connection.makefile("w", encoding="utf-8", newline="\n")
                registration = json.loads(reader.readline() or "{}")
                if (
                    registration.get("type") != "renderer/auth"
                    or registration.get("token") != control_token
                ):
                    raise RuntimeError("renderer control authentication failed")
                connection.settimeout(None)
                self._control_socket = connection
                self._control_reader = reader
                self._control_writer = writer
            except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
                if connection is not None:
                    connection.close()
                self._last_error = str(exc)
                listener.close()
                self.close()
                return False
            finally:
                listener.close()
        self._ready.clear()
        self._reader = threading.Thread(
            target=(
                self._read_control
                if self._control_reader is not None
                else self._read_stdout
            ),
            name="presence-renderer-control",
            daemon=True,
        )
        self._stdout_reader = (
            threading.Thread(
                target=self._drain_stdout,
                name="presence-renderer-stdout",
                daemon=True,
            )
            if self._control_reader is not None
            else None
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name="presence-renderer-stderr",
            daemon=True,
        )
        self._reader.start()
        if self._stdout_reader is not None:
            self._stdout_reader.start()
        self._stderr_reader.start()
        if not self._ready.wait(timeout):
            self._last_error = "renderer host readiness timed out"
            self.close()
            return False
        return True

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        self._read_messages(process.stdout)

    def _read_control(self) -> None:
        reader = self._control_reader
        if reader is not None:
            self._read_messages(reader)

    def _read_messages(self, stream: Any) -> None:
        for line in stream:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "renderer/error":
                self._last_error = str(message.get("error") or "renderer host failed")
                self._ready.clear()
                continue
            if message_type == "renderer/ready":
                self.udp_port = int(message.get("udp_port", self.udp_port))
                self._ready.set()
                continue
            if message_type == "renderer/geometry":
                binding_id = message.get("binding_id")
                geometry = message.get("geometry")
                if isinstance(binding_id, str) and isinstance(geometry, dict):
                    try:
                        self.store.set_geometry(binding_id, geometry)
                    except Exception as exc:
                        self._last_error = str(exc)
                continue
            if message_type == "renderer/input":
                if self.input_handler is not None:
                    try:
                        self.input_handler(message)
                    except Exception as exc:
                        self._last_error = str(exc)
                continue
            if message_type == "response":
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    continue
                with self._responses_lock:
                    target = self._responses.get(request_id)
                if target is not None:
                    target.put(message)
        self._ready.clear()

    def _drain_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            stripped = line.strip()
            if stripped:
                self._last_error = stripped[-2048:]

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            stripped = line.strip()
            if stripped:
                self._last_error = stripped[-2048:]

    def _request(
        self,
        document: Mapping[str, Any],
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        if not self.start(timeout=timeout):
            raise RuntimeError(self._last_error or "renderer host is unavailable")
        process = self.process
        writer = self._control_writer or (process.stdin if process is not None else None)
        if process is None or writer is None:
            raise RuntimeError("renderer host control channel is unavailable")
        request_id = str(uuid.uuid4())
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._responses_lock:
            self._responses[request_id] = response_queue
        try:
            payload = {"id": request_id, **dict(document)}
            with self._write_lock:
                writer.write(
                    json.dumps(payload, separators=(",", ":")) + "\n"
                )
                writer.flush()
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("renderer host response timed out") from exc
        finally:
            with self._responses_lock:
                self._responses.pop(request_id, None)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "renderer command failed"))
        self._last_error = None
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _resource(self, snapshot: EffectiveSnapshot) -> dict[str, Any]:
        if snapshot.renderer.kind == "builtin":
            return {"kind": "builtin"}
        pack = self.catalog.get_avatar(snapshot.avatar_ref)
        if pack["model_fingerprint"] != snapshot.model_fingerprint:
            raise RuntimeError("catalog avatar fingerprint changed after resolution")
        key = snapshot.model_fingerprint.removeprefix("sha256:")
        entrypoint = self.catalog.root / "avatars" / key / "renderer" / "index.html"
        if not entrypoint.is_file():
            raise RuntimeError(
                f"catalog avatar has no materialized renderer: {snapshot.avatar_ref}"
            )
        return {"kind": "live2d", "url": entrypoint.resolve().as_uri()}

    def apply_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        try:
            binding = self.store.binding(snapshot.binding_id)
            resource = self._resource(snapshot)
            result = self._request(
                {
                    "type": "snapshot",
                    "snapshot": PresenceResolver.renderer_document(snapshot),
                    "resource": resource,
                    "geometry": self.store.geometry(snapshot.binding_id),
                    "active": binding["state"] == "active",
                    # Eligibility is per binding; the separate machine policy
                    # gates it dynamically without requiring a snapshot swap.
                    "input_allowed": binding["scope"] == "session",
                },
                timeout=75.0 if resource["kind"] == "live2d" else 20.0,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        acknowledged = (
            result.get("binding_id") == snapshot.binding_id
            and result.get("revision") == snapshot.revision
        )
        if acknowledged:
            self._acknowledged[snapshot.binding_id] = snapshot.revision
        return acknowledged

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        return self.apply_snapshot(snapshot)

    def activity_event(self, event: Mapping[str, Any]) -> bool:
        try:
            result = self._request(
                {"type": "activity", "event": dict(event)},
                timeout=5,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        return bool(result.get("routed"))

    def apply_presentation(self, cue: PresentationCue) -> str:
        timeout = (cue.duration_ms + 2000) / 1000
        try:
            result = self._request(
                {"type": "presentation", "cue": cue.to_document()},
                timeout=timeout,
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise RuntimeError(self._last_error) from exc
        if (
            result.get("binding_id") != cue.binding_id
            or result.get("configuration_revision") != cue.configuration_revision
            or result.get("presentation_sequence") != cue.sequence
        ):
            self._last_error = "renderer acknowledged a foreign presentation cue"
            raise RuntimeError(self._last_error)
        status = str(result.get("status") or "failed")
        if status in {"completed", "cancelled"}:
            self._last_error = None
        else:
            self._last_error = f"renderer returned presentation status {status!r}"
            raise RuntimeError(self._last_error)
        return status

    def cancel_presentation(self, binding_id: str) -> bool:
        try:
            result = self._request(
                {"type": "presentation-cancel", "binding_id": binding_id},
                timeout=5,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        return bool(result.get("found"))

    def playback_event(self, event: Mapping[str, Any]) -> None:
        try:
            self._request({"type": "event", "event": dict(event)}, timeout=5)
        except Exception as exc:
            self._last_error = str(exc)

    def set_binding_active(self, binding_id: str, active: bool) -> bool:
        try:
            result = self._request(
                {
                    "type": "binding-state",
                    "binding_id": binding_id,
                    "active": active,
                },
                timeout=5,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        return bool(result.get("found"))

    def remove_binding(self, binding_id: str) -> bool:
        try:
            result = self._request(
                {"type": "remove", "binding_id": binding_id},
                timeout=5,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        self._acknowledged.pop(binding_id, None)
        return bool(result.get("removed"))

    def set_input_enabled(self, enabled: bool) -> bool:
        self.input_enabled = bool(enabled)
        try:
            result = self._request(
                {"type": "input-policy", "enabled": self.input_enabled}, timeout=5
            )
        except Exception as exc:
            self._last_error = str(exc)
            return False
        return result.get("enabled") == self.input_enabled

    def status(self, binding_id: str | None = None) -> Mapping[str, Any]:
        process = self.process
        running = process is not None and process.poll() is None
        result: dict[str, Any] = {}
        if running and self._ready.is_set():
            try:
                result = self._request({"type": "status"}, timeout=5)
            except Exception as exc:
                self._last_error = str(exc)
        acknowledged = (
            self._acknowledged.get(binding_id) if binding_id is not None else None
        )
        return {
            "running": running,
            "ready": bool(running and self._ready.is_set()),
            "root_pid": process.pid if running else None,
            "udp_port": self.udp_port,
            "acknowledged_revision": acknowledged,
            "last_error": self._last_error,
            **result,
        }

    def close(self) -> None:
        process = self.process
        self.process = None
        self._ready.clear()
        reader = self._reader
        stdout_reader = self._stdout_reader
        stderr_reader = self._stderr_reader
        self._reader = None
        self._stdout_reader = None
        self._stderr_reader = None
        control_socket = self._control_socket
        control_reader = self._control_reader
        control_writer = self._control_writer
        self._control_socket = None
        self._control_reader = None
        self._control_writer = None
        writer = control_writer or (process.stdin if process is not None else None)
        if process is not None and process.poll() is None and writer is not None:
            try:
                request_id = str(uuid.uuid4())
                writer.write(
                    json.dumps(
                        {"id": request_id, "type": "shutdown"},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                writer.flush()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._force_stop_process(process)
        for stream in (
            control_reader,
            control_writer,
            *( (process.stdin, process.stdout, process.stderr) if process is not None else () ),
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if control_socket is not None:
            try:
                control_socket.close()
            except OSError:
                pass
        current = threading.current_thread()
        for thread in (reader, stdout_reader, stderr_reader):
            if thread is not None and thread is not current:
                thread.join(timeout=2)
        if process is not None and process.poll() is not None:
            self._cleanup_process_data(process.pid)

    @staticmethod
    def _cleanup_process_data(pid: int) -> None:
        """Remove only the terminated renderer's private Chromium data directory."""

        temp_root = Path(tempfile.gettempdir()).resolve()
        target = (temp_root / f"codex-presence-renderer-host-{pid}").resolve()
        if target.parent != temp_root or not target.name.startswith(
            "codex-presence-renderer-host-"
        ):
            return
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError:
            # Windows can hold Chromium files briefly after process exit. A
            # stale directory is harmless because every root gets a unique PID.
            pass

    @staticmethod
    def _force_stop_process(process: subprocess.Popen[str]) -> None:
        """Terminate the managed renderer tree after graceful shutdown expires."""

        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
