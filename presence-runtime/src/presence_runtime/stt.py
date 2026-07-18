"""One optional user-level Whisper worker for binding-scoped voice input."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any


class STTWorkerSupervisor:
    def __init__(self, *, python: Path, script: Path, runtime_root: Path) -> None:
        self.python = python.expanduser().resolve()
        self.script = script.expanduser().resolve()
        self.runtime_root = runtime_root.expanduser().resolve()
        self.process: subprocess.Popen[str] | None = None
        self.ready = False
        self.last_error: str | None = None
        self.lock = threading.RLock()
        self.stderr_thread: threading.Thread | None = None

    @staticmethod
    def _readline(stream: Any, *, timeout: float, label: str) -> str:
        result: queue.Queue[str] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                result.put(stream.readline())
            except BaseException:
                result.put("")

        threading.Thread(
            target=read,
            name=f"presence-stt-{label}",
            daemon=True,
        ).start()
        try:
            return result.get(timeout=timeout)
        except queue.Empty as exc:
            raise RuntimeError(f"STT worker {label} timed out after {timeout:g}s") from exc

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        for line in stream:
            message = line.strip()
            if message:
                self.last_error = message[-2048:]

    def start(self) -> bool:
        with self.lock:
            if self.process is not None and self.process.poll() is None and self.ready:
                return True
            self.stop()
            if not self.python.is_file() or not self.script.is_file():
                self.last_error = "STT environment or worker script is missing"
                return False
            try:
                self.process = subprocess.Popen(
                    [
                        str(self.python),
                        str(self.script),
                        "--voice-root",
                        str(self.runtime_root),
                        "--server",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    cwd=self.runtime_root,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    args=(self.process,),
                    name="presence-stt-stderr",
                    daemon=True,
                )
                self.stderr_thread.start()
                response = (
                    json.loads(
                        self._readline(
                            self.process.stdout,
                            timeout=60,
                            label="preload",
                        )
                        or "{}"
                    )
                    if self.process.stdout
                    else {}
                )
                self.ready = bool(response.get("ready"))
                self.last_error = None if self.ready else str(response.get("error") or "STT preload failed")
            except (OSError, json.JSONDecodeError, RuntimeError) as exc:
                self.last_error = str(exc)
                self.ready = False
            if not self.ready:
                self.stop()
            return self.ready

    def transcribe(self, recording: Path) -> str:
        with self.lock:
            if not self.start() or self.process is None or self.process.stdin is None or self.process.stdout is None:
                raise RuntimeError(self.last_error or "STT worker is unavailable")
            request_id = str(uuid.uuid4())
            try:
                self.process.stdin.write(
                    json.dumps(
                        {"request_id": request_id, "recording": str(recording.resolve())},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self.process.stdin.flush()
                response = json.loads(
                    self._readline(
                        self.process.stdout,
                        timeout=120,
                        label="transcription",
                    )
                    or "{}"
                )
            except (OSError, BrokenPipeError, json.JSONDecodeError, RuntimeError) as exc:
                self.stop()
                raise RuntimeError(f"STT worker request failed: {exc}") from exc
            if response.get("request_id") != request_id or not response.get("ok"):
                raise RuntimeError(str(response.get("error") or "STT worker returned an invalid response"))
            text = response.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("STT worker returned an empty transcript")
            return text.strip()

    def status(self) -> dict[str, Any]:
        process = self.process
        running = process is not None and process.poll() is None
        return {
            "installed": self.python.is_file() and self.script.is_file(),
            "running": running,
            "ready": bool(running and self.ready),
            "pid": process.pid if running else None,
            "last_error": self.last_error,
        }

    def stop(self) -> None:
        process = self.process
        self.process = None
        self.ready = False
        stderr_thread = self.stderr_thread
        self.stderr_thread = None
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
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if stderr_thread is not None and stderr_thread is not threading.current_thread():
            stderr_thread.join(timeout=2)
