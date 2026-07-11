"""Transparent Codex app-server proxy with streamed voice and Orb taps.

The bridge speaks the app-server stdio protocol on its own stdin/stdout. It
forwards every upstream line unchanged and observes selected server
notifications on side channels. Voice and Orb failures are deliberately
non-fatal: a client must never lose its app-server connection because a local
presence component is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from activity import ActivityEmitter


ACTIVITY_BY_ITEM_TYPE = {
    "commandexecution": "cli",
    "command_execution": "cli",
    "filechange": "cli",
    "file_change": "cli",
    "mcpToolCall": "tool",
    "mcptoolcall": "tool",
    "mcp_tool_call": "tool",
    "functioncall": "tool",
    "function_call": "tool",
    "websearch": "tool",
    "web_search": "tool",
    "customtoolcall": "tool",
    "custom_tool_call": "tool",
    "hook": "skill",
    "skill": "skill",
    "reasoning": "thinking",
}


def log(voice_root: Path, message: str) -> None:
    """Write bridge diagnostics without putting them on the protocol stream."""

    try:
        voice_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with (voice_root / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def marker_enabled(path: Path) -> bool:
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return value in {"1", "true", "on", "enabled"}


def provider_name(voice_root: Path) -> str:
    try:
        value = os.environ.get("CODEX_TTS_PROVIDER") or (
            voice_root / "provider"
        ).read_text(encoding="utf-8")
    except OSError:
        value = "cpu"
    value = value.strip().lower()
    if value in {"cuda", "cudaexecutionprovider", "nvidia", "nvidia-cuda"}:
        return "cuda"
    if value in {"directml", "dml", "gpu"}:
        return "directml"
    return "cpu"


def environment_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def runtime_python(voice_root: Path) -> Path | None:
    provider = provider_name(voice_root)
    environment = {
        "cpu": voice_root / ".venv",
        "cuda": voice_root / ".cuda-venv",
        "directml": voice_root / ".dml-venv",
    }[provider]
    python = environment_python(environment)
    if python.is_file():
        return python
    if provider != "cpu":
        fallback = environment_python(voice_root / ".venv")
        if fallback.is_file():
            log(voice_root, f"{provider} runtime missing; bridge using CPU runtime")
            return fallback
    return None


def item_type(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    value = item.get("type")
    return value.strip().lower() if isinstance(value, str) else ""


def activity_for_item(item: object) -> str:
    kind = item_type(item)
    if kind in ACTIVITY_BY_ITEM_TYPE:
        return ACTIVITY_BY_ITEM_TYPE[kind]
    if "command" in kind or "shell" in kind or "process" in kind:
        return "cli"
    if "mcp" in kind or "tool" in kind or "function" in kind or "search" in kind:
        return "tool"
    if "skill" in kind or "hook" in kind:
        return "skill"
    return "thinking"


class ActivityTap:
    """Forward only coarse activity categories to the existing Orb bridge."""

    def __init__(self, voice_root: Path, enabled: bool) -> None:
        self.emitter: ActivityEmitter | None = None
        if enabled and marker_enabled(voice_root / "orb.enabled"):
            try:
                self.emitter = ActivityEmitter()
            except OSError as exc:
                log(voice_root, f"Orb activity bridge unavailable: {type(exc).__name__}")

    def send(self, state: str, session_id: str | None = None) -> None:
        if self.emitter is None:
            return
        try:
            self.emitter.send(state, source="app-server-bridge", session_id=session_id)
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self.emitter is None:
            return
        try:
            self.emitter.send("idle", source="app-server-bridge")
            self.emitter.close()
        except (OSError, ValueError):
            pass


class TTSStreamProxy:
    """Queue incremental speech events into the project-local Kokoro worker."""

    def __init__(self, project_root: Path, voice_root: Path, enabled: bool) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.enabled = enabled
        self.events: queue.Queue[dict[str, object] | None] = queue.Queue()
        self.process: subprocess.Popen[str] | None = None
        self.writer: threading.Thread | None = None
        self.reader: threading.Thread | None = None
        self.lock = threading.Lock()
        self.started = False

    def start(self) -> bool:
        if self.started:
            return self.process is not None and self.process.poll() is None
        self.started = True
        if not self.enabled:
            return False
        python = runtime_python(self.voice_root)
        hook = self.project_root / ".codex" / "hooks" / "speak.py"
        if python is None or not hook.is_file():
            log(self.voice_root, "stream worker skipped: runtime or speak.py missing")
            return False

        environment = os.environ.copy()
        environment["CODEX_TTS_FROM_WATCHER"] = "1"
        environment["CODEX_TTS_FROM_BRIDGE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        try:
            self.process = subprocess.Popen(
                [str(python), str(hook), "--stream-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log(self.voice_root, f"stream worker start error: {type(exc).__name__}")
            self.process = None
            return False

        self.writer = threading.Thread(
            target=self._write_events,
            name="codex-bridge-tts-writer",
            daemon=True,
        )
        self.reader = threading.Thread(
            target=self._read_events,
            name="codex-bridge-tts-reader",
            daemon=True,
        )
        self.writer.start()
        self.reader.start()
        log(self.voice_root, "stream worker started")
        return True

    def send(self, event: dict[str, object]) -> bool:
        if not self.start():
            return False
        process = self.process
        if process is None or process.poll() is not None:
            return False
        self.events.put(event)
        return True

    def _write_events(self) -> None:
        while True:
            event = self.events.get()
            if event is None:
                return
            process = self.process
            if process is None or process.stdin is None:
                return
            try:
                process.stdin.write(json.dumps(event, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                log(self.voice_root, f"stream worker write error: {type(exc).__name__}")
                return

    def _read_events(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    kind = event.get("event")
                    if kind == "ready":
                        log(self.voice_root, f"stream worker ready={event.get('ok')}")
                    elif kind in {"done", "error"}:
                        log(
                            self.voice_root,
                            f"stream worker {kind} ok={event.get('ok')}",
                        )
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            self.events.put({"type": "shutdown"})
            self.events.put(None)
            if self.writer is not None:
                self.writer.join(timeout=10)
            try:
                if process.stdin is not None:
                    process.stdin.close()
                # A completed turn can still have queued audio in Kokoro and
                # ffplay. Let that speech finish before tearing down the
                # sidecar when a client closes its stdio connection.
                process.wait(timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
        self.process = None


class NotificationInterceptor:
    """Observe safe app-server lifecycle fields without rewriting protocol data."""

    def __init__(self, tts: TTSStreamProxy, activity: ActivityTap, voice_root: Path) -> None:
        self.tts = tts
        self.activity = activity
        self.voice_root = voice_root
        self.active_stream: str | None = None

    @staticmethod
    def _stream_id(params: dict[str, object]) -> str:
        return "|".join(
            str(params.get(name, ""))
            for name in ("threadId", "turnId", "itemId")
        )

    @staticmethod
    def _thread_id(params: dict[str, object]) -> str | None:
        value = params.get("threadId")
        return value if isinstance(value, str) else None

    def _start_stream(self, params: dict[str, object]) -> None:
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        stream_id = self._stream_id(params)
        if self.active_stream != stream_id:
            if self.active_stream is not None:
                self.tts.send({"type": "cancel", "stream_id": self.active_stream})
            self.active_stream = stream_id
            self.tts.send({"type": "start", "stream_id": stream_id})
        self.tts.send(
            {
                "type": "delta",
                "stream_id": stream_id,
                "text": delta,
            }
        )

    def _finish_stream(self, stream_id: str | None = None) -> None:
        if self.active_stream is None:
            return
        if stream_id is not None and stream_id != self.active_stream:
            return
        current = self.active_stream
        self.tts.send({"type": "finish", "stream_id": current})
        self.active_stream = None

    def handle(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        thread_id = self._thread_id(params)

        if method == "item/agentMessage/delta":
            self._start_stream(params)
            return

        if method == "turn/started":
            self.activity.send("thinking", thread_id)
            return

        if method == "hook/started":
            self.activity.send("skill", thread_id)
            return

        if method == "item/started":
            item = params.get("item")
            self.activity.send(activity_for_item(item), thread_id)
            return

        if method == "item/completed":
            item = params.get("item")
            if item_type(item) in {"agentmessage", "agent_message", "agent-message"}:
                item_id = item.get("id") if isinstance(item, dict) else None
                turn_id = params.get("turnId")
                stream_id = "|".join(
                    str(value if value is not None else "")
                    for value in (thread_id, turn_id, item_id)
                )
                self._finish_stream(stream_id)
            else:
                self.activity.send("thinking", thread_id)
            return

        if method == "turn/completed":
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if self.active_stream is not None and (
                thread_id is None
                or self.active_stream.startswith(f"{thread_id}|{turn_id}")
            ):
                self._finish_stream()
            self.activity.send("idle", thread_id)
            return

        if method in {"error", "warning"}:
            self.activity.send("error", thread_id)


class AppServerBridge:
    def __init__(
        self,
        project_root: Path,
        voice_root: Path,
        upstream: list[str],
        voice_enabled: bool,
        activity_enabled: bool,
    ) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.upstream_args = upstream
        self.upstream: subprocess.Popen[str] | None = None
        self.write_lock = threading.Lock()
        self.client_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.tts = TTSStreamProxy(project_root, voice_root, voice_enabled)
        self.activity = ActivityTap(voice_root, activity_enabled)
        self.interceptor = NotificationInterceptor(self.tts, self.activity, voice_root)

    def _forward_client(self) -> None:
        process = self.upstream
        if process is None or process.stdin is None:
            return
        try:
            for line in sys.stdin:
                with self.write_lock:
                    process.stdin.write(line)
                    process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def _capture_stderr(self) -> None:
        process = self.upstream
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                if line.strip():
                    log(self.voice_root, "upstream stderr received")
        except (OSError, ValueError):
            pass

    def run(self) -> int:
        log(self.voice_root, f"starting app-server bridge: {self.upstream_args[0]}")
        try:
            self.upstream = subprocess.Popen(
                self.upstream_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log(self.voice_root, f"app-server start error: {type(exc).__name__}")
            return 2

        self.tts.start()
        self.client_thread = threading.Thread(
            target=self._forward_client,
            name="codex-bridge-client-forwarder",
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._capture_stderr,
            name="codex-bridge-upstream-stderr",
            daemon=True,
        )
        self.client_thread.start()
        self.stderr_thread.start()

        try:
            if self.upstream.stdout is not None:
                for line in self.upstream.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    try:
                        self.interceptor.handle(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except (BrokenPipeError, OSError):
            pass
        finally:
            self.stop()
        return 0

    def stop(self) -> None:
        self.tts.stop()
        self.activity.close()
        process = self.upstream
        self.upstream = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        log(self.voice_root, "app-server bridge stopped")


def resolve_codex(codex: str) -> str:
    """Prefer the runnable per-user binary over the WindowsApps shim."""

    if codex not in {"codex", "codex.exe"}:
        return codex
    configured = os.environ.get("CODEX_CLI")
    if configured:
        return configured
    path = shutil.which(codex)
    if path and "windowsapps" not in path.lower():
        return path
    install_root = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    candidates = [candidate for candidate in install_root.glob("*\\codex.exe") if candidate.is_file()]
    if candidates:
        candidates.sort(key=lambda candidate: candidate.stat().st_mtime, reverse=True)
        return str(candidates[0])
    return codex


def command_args(command: str | None, codex: str) -> list[str]:
    if command:
        return shlex.split(command, posix=True)
    return [resolve_codex(codex), "app-server", "--listen", "stdio://"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--voice-root", type=Path)
    parser.add_argument("--codex", default=os.environ.get("CODEX_CLI", "codex"))
    parser.add_argument("--upstream-command")
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--no-activity", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    voice_root = (args.voice_root or project_root / ".codex-voice").resolve()
    upstream = command_args(
        args.upstream_command or os.environ.get("CODEX_APP_SERVER_COMMAND"),
        args.codex,
    )
    voice_enabled = not args.no_voice and marker_enabled(voice_root / "enabled")
    bridge = AppServerBridge(
        project_root,
        voice_root,
        upstream,
        voice_enabled,
        not args.no_activity,
    )
    try:
        return bridge.run()
    except KeyboardInterrupt:
        bridge.stop()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
