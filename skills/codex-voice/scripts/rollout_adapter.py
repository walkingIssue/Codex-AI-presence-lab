"""Feed Codex rollout events into the authoritative Presence Runtime v0.2.

The adapter deliberately owns no voice, renderer, profile, or durable message
state.  Its only project-local files are a resumable rollout cursor and
diagnostics beneath ``.codex-voice/v0.2``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime_adapter import RuntimePlaybackAdapter


POLL_SECONDS = 0.4
DISCOVERY_SECONDS = 2.0
ACTIVITY_TTL_SECONDS = {
    "idle": 0.0,
    "thinking": 12.0,
    "tool": 8.0,
    "skill": 12.0,
    "cli": 8.0,
    "waiting": 12.0,
    "error": 4.0,
}
LOCAL_TOOL_NAMES = {
    "bash",
    "cmd",
    "exec",
    "powershell",
    "run_command",
    "shell_command",
    "terminal",
}
SKILL_TOOL_NAMES = {
    "skill",
    "skill_invoke",
    "skill_read",
    "skill_view",
    "skills.list",
    "skills.read",
}


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def normal_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(value))))


def stable_event_id(*parts: object) -> str:
    value = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def session_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
        payload = first.get("payload", {})
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        session_id = (
            payload.get("session_id", payload.get("id"))
            if isinstance(payload, dict)
            else None
        )
        return (
            cwd if isinstance(cwd, str) else None,
            session_id if isinstance(session_id, str) else None,
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return None, None


def record_turn_id(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    candidates = [record.get("turn_id"), record.get("turnId")]
    if isinstance(payload, dict):
        candidates.extend((payload.get("turn_id"), payload.get("turnId"), payload.get("id")))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def agent_message(record: dict[str, Any], phase: str) -> str | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "agent_message" or payload.get("phase") != phase:
        return None
    message = payload.get("message")
    return message if isinstance(message, str) and message.strip() else None


def classify_activity(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    outer_type = record.get("type")
    inner_type = payload.get("type")
    if outer_type == "event_msg":
        if inner_type in {"agent_reasoning", "task_started", "turn_started", "agent_turn_started"}:
            return "thinking"
        if inner_type in {
            "mcp_tool_call_start",
            "mcp_tool_call_end",
            "web_search_start",
            "web_search_end",
        }:
            return "tool"
        if inner_type in {"patch_apply_start", "patch_apply_end"}:
            return "cli"
        if inner_type in {"skill_start", "skill_invoked"}:
            return "skill"
        if inner_type == "skill_end":
            return "thinking"
        if inner_type in {"task_complete", "turn_complete", "session_end", "turn_aborted"}:
            return "idle"
        if inner_type in {"error", "agent_error", "stream_error", "turn_failed"}:
            return "error"
        if inner_type in {"approval_request", "user_input_required", "waiting"}:
            return "waiting"
        if inner_type == "agent_message":
            return "idle" if payload.get("phase") == "final_answer" else "thinking"
    if outer_type == "response_item":
        if inner_type == "reasoning":
            return "thinking"
        if inner_type in {"custom_tool_call", "function_call", "web_search_call"}:
            name = payload.get("name")
            normalized = name.strip().lower() if isinstance(name, str) else ""
            if normalized in SKILL_TOOL_NAMES:
                return "skill"
            if normalized in {"request_user_input", "ask_user", "approval_request"}:
                return "waiting"
            return "cli" if normalized in LOCAL_TOOL_NAMES else "tool"
        if inner_type in {"custom_tool_call_output", "function_call_output"}:
            return "thinking"
    return None


class CursorStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offsets: dict[str, int] = {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            values = document.get("offsets", {}) if isinstance(document, dict) else {}
            if isinstance(values, dict):
                self.offsets = {
                    str(key): int(value)
                    for key, value in values.items()
                    if isinstance(key, str) and isinstance(value, int) and value >= 0
                }
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def get(self, path: Path) -> int | None:
        return self.offsets.get(normal_path(path))

    def set(self, path: Path, offset: int) -> None:
        self.offsets[normal_path(path)] = max(0, int(offset))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema": "presence/rollout-cursors/v0.2", "offsets": self.offsets}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class AdapterLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "AdapterLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("another rollout adapter owns this project") from exc
        self.handle = handle
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self.path.unlink(missing_ok=True)


@dataclass
class ActivityLease:
    state: str
    session_id: str
    seen_at: float
    event_id: str


class ActivityTracker:
    def __init__(self, playback: RuntimePlaybackAdapter) -> None:
        self.playback = playback
        self.leases: dict[Path, ActivityLease] = {}
        self.visible: tuple[str, str | None, Path | None] = ("idle", None, None)

    def update(self, path: Path, state: str, session_id: str, event_id: str) -> None:
        if state == "idle":
            self.leases.pop(path, None)
        else:
            self.leases[path] = ActivityLease(state, session_id, time.monotonic(), event_id)
        self.tick(force=True)

    def tick(self, *, force: bool = False) -> None:
        now = time.monotonic()
        for path, lease in tuple(self.leases.items()):
            if now - lease.seen_at > ACTIVITY_TTL_SECONDS.get(lease.state, 12.0):
                self.leases.pop(path, None)
        if self.leases:
            path, lease = max(self.leases.items(), key=lambda item: item[1].seen_at)
            current = (lease.state, lease.session_id, path)
            event_id = lease.event_id
        else:
            current = ("idle", self.visible[1], None)
            # A timeout is a new lifecycle transition every time. Reusing the
            # same stable id caused the runtime event ledger to deduplicate the
            # second and later idle reset for a session.
            event_id = f"activity-timeout:{uuid.uuid4()}"
        if not force and current == self.visible:
            return
        if current == self.visible:
            return
        self.playback.publish_activity(
            current[0],
            session_id=current[1],
            event_id=event_id,
        )
        self.visible = current


class RolloutAdapter:
    def __init__(
        self,
        project_root: Path,
        state_root: Path,
        *,
        start_time: float | None = None,
        playback: RuntimePlaybackAdapter | None = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.state_root = state_root.expanduser().resolve()
        self.start_time = start_time if start_time is not None else time.time()
        self.playback = playback or RuntimePlaybackAdapter(
            self.project_root, adapter="codex-rollout-v0.2"
        )
        self.cursors = CursorStore(self.state_root / "rollout-cursors.json")
        self.stop_event = threading.Event()
        self.paths: list[Path] = []
        self.last_discovery = 0.0
        self.activity = ActivityTracker(self.playback)

    def log(self, message: str) -> None:
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            with (self.state_root / "adapter.log").open("a", encoding="utf-8") as handle:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                handle.write(f"{stamp} {message}\n")
        except OSError:
            pass

    def discover(self) -> list[Path]:
        sessions = codex_home() / "sessions"
        if not sessions.is_dir():
            return []
        result: list[Path] = []
        for path in sessions.rglob("rollout-*.jsonl"):
            if not path.is_file():
                continue
            cwd, session_id = session_metadata(path)
            if cwd is None or session_id is None:
                continue
            if normal_path(cwd) == normal_path(self.project_root):
                result.append(path)
        return sorted(result, key=str)

    def _initial_offset(self, path: Path) -> int:
        stored = self.cursors.get(path)
        try:
            stat = path.stat()
        except OSError:
            return stored or 0
        if stored is not None:
            return 0 if stat.st_size < stored else stored
        return stat.st_size if stat.st_mtime < self.start_time else 0

    def _dispatch(
        self,
        path: Path,
        record: dict[str, Any],
        *,
        enforce_start_time: bool,
    ) -> None:
        _cwd, session_id = session_metadata(path)
        if session_id is None:
            return
        record_time = timestamp_seconds(record.get("timestamp"))
        if enforce_start_time and record_time is not None and record_time < self.start_time:
            return
        activity = classify_activity(record)
        if activity is not None:
            event_id = stable_event_id(
                "activity", path, record.get("timestamp"), record.get("type"), activity
            )
            self.activity.update(path, activity, session_id, event_id)
        turn_id = record_turn_id(record)
        commentary = agent_message(record, "commentary")
        if commentary is not None:
            self.playback.publish_update(
                {
                    "event_id": stable_event_id(
                        path, record.get("timestamp"), "commentary", commentary
                    ),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "kind": "commentary",
                    "text": commentary,
                }
            )
        final = agent_message(record, "final_answer")
        if final is not None:
            self.playback.enqueue(
                {
                    "event_id": stable_event_id(
                        "codex-visible-final", self.project_root, session_id, turn_id, final
                    ),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "kind": "final",
                    "text": final,
                }
            )

    def scan(self, path: Path) -> None:
        enforce_start_time = self.cursors.get(path) is None
        offset = self._initial_offset(path)
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                while not self.stop_event.is_set():
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith((b"\n", b"\r")):
                        handle.seek(line_start)
                        break
                    try:
                        value = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self.log(f"skipped malformed rollout line path={path.name} offset={line_start}")
                    else:
                        if isinstance(value, dict):
                            self._dispatch(
                                path,
                                value,
                                enforce_start_time=enforce_start_time,
                            )
                    self.cursors.set(path, handle.tell())
        except OSError as exc:
            self.log(f"rollout read failed path={path.name}: {type(exc).__name__}: {exc}")

    def run(self, *, parent_pid: int | None = None) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        (self.state_root / "adapter.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.playback.start()
        self.log(f"started project={self.project_root}")
        try:
            while not self.stop_event.wait(POLL_SECONDS):
                if parent_pid is not None and not pid_exists(parent_pid):
                    self.log(f"stopping because supervisor pid {parent_pid} exited")
                    break
                now = time.monotonic()
                if now - self.last_discovery >= DISCOVERY_SECONDS:
                    self.paths = self.discover()
                    self.last_discovery = now
                for path in self.paths:
                    self.scan(path)
                self.activity.tick()
        finally:
            self.playback.close()
            (self.state_root / "adapter.pid").unlink(missing_ok=True)
            self.log("stopped")


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a portable existence probe on Windows;
        # Python routes it through TerminateProcess and it can fail even while
        # the queried process is healthy.  A synchronize handle lets us check
        # liveness without requiring process-query privileges.
        import ctypes

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    adapter = RolloutAdapter(args.project_root, args.state_root)

    def stop(_signal: int, _frame: Any) -> None:
        adapter.stop_event.set()

    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(selected, stop)
        except (ValueError, OSError):
            pass
    try:
        with AdapterLock(args.state_root.expanduser().resolve() / "adapter.lock"):
            adapter.run(parent_pid=args.parent_pid)
    except RuntimeError as exc:
        adapter.log(str(exc))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
