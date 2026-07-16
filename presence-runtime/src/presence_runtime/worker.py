"""One user-owned warm Kokoro subprocess and testable speech worker seam."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Mapping

from .errors import ConflictError
from .models import EffectiveSnapshot


class KokoroWorkerSupervisor:
    """Own exactly one persistent speak.py server for the user runtime."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        python: Path,
        worker_script: Path,
        renderer_udp_port: int = 17839,
    ) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.python = python.expanduser().resolve()
        self.worker_script = worker_script.expanduser().resolve()
        self.renderer_udp_port = renderer_udp_port
        self.process: subprocess.Popen[str] | None = None
        self.ready = False
        self._lock = threading.RLock()

    def start(self) -> bool:
        with self._lock:
            if self.process is not None and self.process.poll() is None and self.ready:
                return True
            self.stop()
            if not self.python.is_file() or not self.worker_script.is_file():
                return False
            environment = os.environ.copy()
            environment["CODEX_PRESENCE_HOME"] = str(self.runtime_root)
            environment["CODEX_TTS_FROM_ARBITER"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            try:
                self.process = subprocess.Popen(
                    [str(self.python), str(self.worker_script), "--server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=self.runtime_root,
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                line = self.process.stdout.readline() if self.process.stdout else ""
                response = json.loads(line) if line else {}
                self.ready = bool(response.get("ready"))
            except (OSError, json.JSONDecodeError):
                self.ready = False
            if not self.ready:
                self.stop()
            return self.ready

    def apply_snapshot(self, _snapshot: EffectiveSnapshot) -> bool:
        return self.start()

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        return self.apply_snapshot(snapshot)

    def speak(self, item: Mapping[str, Any]) -> str:
        with self._lock:
            if (
                not self.start()
                or self.process is None
                or self.process.stdin is None
                or self.process.stdout is None
            ):
                return "failed"
            tts = item["tts"]
            request = {
                "hook_event_name": "Stop",
                "last_assistant_message": item["text"],
                "tts_event_id": item["event_id"],
                "tts_utterance_id": item["utterance_id"],
                "tts_binding_id": item["binding_id"],
                "tts_voice": tts["voice_id"],
                "tts_speed": tts["speed"],
                "tts_mode": tts["playback_mode"],
                "tts_volume": tts["volume"],
                "tts_pauseable": item["kind"] != "commentary",
                "tts_orb_port": self.renderer_udp_port,
            }
            try:
                self.process.stdin.write(
                    json.dumps(request, separators=(",", ":")) + "\n"
                )
                self.process.stdin.flush()
                response = json.loads(self.process.stdout.readline() or "{}")
            except (BrokenPipeError, OSError, json.JSONDecodeError):
                self.stop()
                return "failed"
            if response.get("done") and response.get("ok"):
                return "completed"
            if response.get("done") and response.get("interrupted"):
                return "interrupted"
            return "failed"

    def stop_playback(self) -> None:
        marker = self.runtime_root / "tts-stop.request"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("stop\n", encoding="utf-8")

    def status(self) -> dict[str, Any]:
        process = self.process
        running = process is not None and process.poll() is None
        return {
            "running": running,
            "ready": bool(running and self.ready),
            "pid": process.pid if running else None,
            "runtime_root": str(self.runtime_root),
        }

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.ready = False
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


class RecordingWorker:
    """Deterministic worker used by contract tests."""

    def __init__(self) -> None:
        self.snapshots: list[EffectiveSnapshot] = []
        self.items: list[dict[str, Any]] = []
        self.ready = True

    def apply_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        self.snapshots.append(snapshot)
        return self.ready

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        self.snapshots.append(snapshot)
        return self.ready

    def speak(self, item: Mapping[str, Any]) -> str:
        if not self.ready:
            raise ConflictError("recording worker is unavailable")
        self.items.append(dict(item))
        return "completed"

    def status(self) -> dict[str, Any]:
        return {"running": self.ready, "ready": self.ready, "pid": None}

    def stop(self) -> None:
        self.ready = False
