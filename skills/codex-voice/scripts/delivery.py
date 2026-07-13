"""Codex App Server delivery adapter used by project-local voice input."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class DeliveryError(RuntimeError):
    """A transcript could not be delivered as a normal Codex user turn."""


def codex_executable() -> str | None:
    explicit = os.environ.get("CODEX_CLI_PATH", "").strip().strip('"')
    if explicit and Path(explicit).is_file():
        return explicit
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"(?m)^\s*CODEX_CLI_PATH\s*=\s*[\"']([^\"']+)[\"']\s*$", text)
    if match and Path(match.group(1)).is_file():
        return match.group(1)
    return shutil.which("codex")


class AppServerClient:
    """One short-lived JSONL App Server connection per voice submission.

    The Codex app-server is deliberately treated as an external boundary. The
    client submits a normal user turn and waits for its terminal notification;
    it never injects hidden context or raw audio.
    """

    def __init__(self, project_root: Path, *, timeout_seconds: float = 120.0):
        self.project_root = project_root
        self.timeout_seconds = timeout_seconds
        self._request_id = 0
        self._process: subprocess.Popen[str] | None = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send(self, method: str, params: dict[str, Any] | None = None, *, request: bool = True) -> int | None:
        if self._process is None or self._process.stdin is None:
            raise DeliveryError("App Server connection is not open")
        request_id = self._next_id() if request else None
        payload: dict[str, Any] = {"method": method, "params": params or {}}
        if request_id is not None:
            payload["id"] = request_id
        self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._process.stdin.flush()
        return request_id

    def _read_until(self, request_id: int, *, terminal_turn_id: str | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise DeliveryError("App Server connection is not open")
        deadline = time.monotonic() + self.timeout_seconds
        response: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            line = self._process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error") or {}
                    raise DeliveryError(str(error.get("message", "App Server request failed")))
                response = message.get("result") or {}
                if terminal_turn_id is None:
                    return response
            if (
                terminal_turn_id
                and message.get("method") == "turn/completed"
                and isinstance(message.get("params"), dict)
            ):
                turn = message["params"].get("turn") or {}
                if turn.get("id") == terminal_turn_id:
                    return response or {"turn": turn}
        if terminal_turn_id:
            raise DeliveryError("Timed out waiting for the Codex turn to complete")
        raise DeliveryError("Timed out waiting for the Codex App Server response")

    def _start(self) -> None:
        executable = codex_executable()
        if not executable:
            raise DeliveryError("Codex CLI was not found; set CODEX_CLI_PATH to the working executable")
        try:
            self._process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                cwd=str(self.project_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DeliveryError(f"Could not start Codex App Server: {exc}") from exc
        initialize_id = self._send(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_voice_input",
                    "title": "Codex Voice Input",
                    "version": "0.1.0",
                }
            },
        )
        self._read_until(initialize_id or 0)
        self._send("initialized", {}, request=False)

    def submit(self, thread_id: str, transcript: str, *, wait_for_completion: bool = False) -> dict[str, Any]:
        transcript = transcript.strip()
        if not transcript:
            raise DeliveryError("transcript is empty")
        self._start()
        handoff = False
        try:
            resume_id = self._send("thread/resume", {"threadId": thread_id})
            resumed = self._read_until(resume_id or 0)
            thread = resumed.get("thread") if isinstance(resumed, dict) else {}
            thread = thread if isinstance(thread, dict) else {}
            status = str(thread.get("status", "")).lower()
            active_turn_id = thread.get("activeTurnId") or thread.get("turnId")
            input_items = [{"type": "text", "text": transcript}]
            if active_turn_id and status in {"inprogress", "in_progress", "running", "active"}:
                request_id = self._send(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": active_turn_id,
                        "input": input_items,
                    },
                )
                result = self._read_until(request_id or 0)
                turn_id = str(result.get("turnId") or active_turn_id)
                method = "turn/steer"
            else:
                request_id = self._send(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items,
                        "cwd": str(self.project_root),
                    },
                )
                result = self._read_until(request_id or 0)
                turn = result.get("turn") if isinstance(result, dict) else {}
                turn = turn if isinstance(turn, dict) else {}
                turn_id = str(turn.get("id") or result.get("turnId") or "")
                method = "turn/start"
            if turn_id:
                if wait_for_completion:
                    self._read_until(-1, terminal_turn_id=turn_id)
                else:
                    handoff = True
                    threading.Thread(
                        target=self._wait_and_close,
                        args=(turn_id,),
                        name="codex-app-server-turn-drain",
                        daemon=True,
                    ).start()
            return {"method": method, "thread_id": thread_id, "turn_id": turn_id}
        finally:
            if not handoff:
                self.close()

    def _wait_and_close(self, turn_id: str) -> None:
        try:
            self._read_until(-1, terminal_turn_id=turn_id)
        except DeliveryError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def resolve_session_label(project_root: Path, session_id: str | None, thread: dict[str, Any] | None = None) -> str:
    """Resolve a safe short label without exposing rollout internals to TTS."""
    if isinstance(thread, dict):
        for key in ("name", "title"):
            value = thread.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    project_name = project_root.name or "Codex"
    suffix = (session_id or "")[-8:]
    return f"{project_name} {suffix}".strip()
