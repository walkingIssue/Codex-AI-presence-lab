"""Python-owned lifecycle and acknowledgement bridge for one Electron root."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import Catalog
from .models import EffectiveSnapshot
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
    ) -> None:
        self.host_root = host_root.expanduser().resolve()
        self.catalog = catalog
        self.store = store
        self.udp_port = udp_port
        self.command = list(command) if command is not None else self._default_command()
        self.process: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._responses_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
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
            self._last_error = str(exc)
            self.process = None
            return False
        self._ready.clear()
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="presence-renderer-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            name="presence-renderer-stderr",
            daemon=True,
        )
        self._reader.start()
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
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
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
            if message_type == "response":
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    continue
                with self._responses_lock:
                    target = self._responses.get(request_id)
                if target is not None:
                    target.put(message)
        self._ready.clear()

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
        if process is None or process.stdin is None:
            raise RuntimeError("renderer host stdin is unavailable")
        request_id = str(uuid.uuid4())
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._responses_lock:
            self._responses[request_id] = response_queue
        try:
            payload = {"id": request_id, **dict(document)}
            with self._write_lock:
                process.stdin.write(
                    json.dumps(payload, separators=(",", ":")) + "\n"
                )
                process.stdin.flush()
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError("renderer host response timed out") from exc
        finally:
            with self._responses_lock:
                self._responses.pop(request_id, None)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "renderer command failed"))
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
            result = self._request(
                {
                    "type": "snapshot",
                    "snapshot": PresenceResolver.renderer_document(snapshot),
                    "resource": self._resource(snapshot),
                    "geometry": self.store.geometry(snapshot.binding_id),
                    "active": binding["state"] == "active",
                }
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

    def apply_activity(self, snapshot: EffectiveSnapshot) -> bool:
        # The activity overlay is already resolved in Python. Sending the same
        # configuration revision updates exact actions without a JS merge.
        return self.apply_snapshot(snapshot)

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
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                request_id = str(uuid.uuid4())
                process.stdin.write(
                    json.dumps(
                        {"id": request_id, "type": "shutdown"},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                process.stdin.flush()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
