"""Transparent Codex TUI/server proxy with a mocked Kokoro worker seam.

The bridge owns the transport boundary, not speech inference.  It forwards
every upstream JSONL line unchanged and observes only explicit visible
assistant-delta notifications.  Those notifications become a small,
transport-neutral stream protocol for a Kokoro worker:

    start -> delta* -> finish

The default worker is an in-memory recorder so this path is safe to exercise
before the inference worker is available.  ``--worker-command`` can later
point at the real worker without changing the TUI/server protocol.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, TextIO


BRIDGE_SCHEMA = "codex-voice/tui-bridge/v0.1"
DEFAULT_SERVER_COMMAND = "codex app-server --listen stdio://"
MAX_DELTA_CHARS = 8_000


def log(voice_root: Path, message: str) -> None:
    """Write diagnostics away from the TUI/server protocol stream."""

    try:
        voice_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with (voice_root / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def command_args(command: str) -> list[str]:
    """Parse a configured child command without invoking a shell."""

    try:
        args = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"invalid bridge command: {exc}") from exc
    if not args:
        raise ValueError("bridge command cannot be empty")
    return args


class KokoroWorker(Protocol):
    """Worker contract kept deliberately independent from inference."""

    def start(self) -> bool: ...

    def send(self, event: dict[str, object]) -> bool: ...

    def close(self) -> None: ...


class MockKokoroWorker:
    """Record worker packets for dry runs and bridge-level tests."""

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.started = False
        self.closed = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        self.started = True
        return True

    def send(self, event: dict[str, object]) -> bool:
        if not self.started or self.closed:
            return False
        with self._lock:
            self.events.append(dict(event))
        return True

    def close(self) -> None:
        self.closed = True


class JsonlKokoroWorker:
    """Send normalized stream packets to an external worker over JSONL.

    Worker responses are consumed on a background thread so they can never
    appear on the TUI stdout stream.  The worker protocol is intentionally
    loose: a worker may emit readiness or diagnostic events, but the bridge
    only requires that it accept one JSON object per input line.
    """

    def __init__(self, command: list[str], *, cwd: Path, voice_root: Path) -> None:
        self.command = command
        self.cwd = cwd
        self.voice_root = voice_root
        self.process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None
        self.write_lock = threading.Lock()

    def start(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(self.cwd),
                env=os.environ.copy(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log(self.voice_root, f"Kokoro worker start error: {type(exc).__name__}")
            self.process = None
            return False

        self.reader = threading.Thread(
            target=self._read_responses,
            name="codex-tui-bridge-worker-reader",
            daemon=True,
        )
        self.reader.start()
        log(self.voice_root, "Kokoro worker process started")
        return True

    def send(self, event: dict[str, object]) -> bool:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            return False
        try:
            with self.write_lock:
                process.stdin.write(json.dumps(event, separators=(",", ":")) + "\n")
                process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            log(self.voice_root, f"Kokoro worker write error: {type(exc).__name__}")
            return False
        return True

    def _read_responses(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    log(self.voice_root, "Kokoro worker returned invalid JSON")
                    continue
                if isinstance(response, dict) and response.get("event") in {"ready", "error"}:
                    log(self.voice_root, f"Kokoro worker event={response.get('event')}")
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
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
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass


@dataclass(frozen=True)
class StreamIdentity:
    stream_id: str
    session_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None

    def fields(self) -> dict[str, object]:
        result: dict[str, object] = {"stream_id": self.stream_id}
        for key, value in (
            ("session_id", self.session_id),
            ("turn_id", self.turn_id),
            ("item_id", self.item_id),
        ):
            if value:
                result[key] = value
        return result


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _first_string(*values: object) -> str | None:
    for value in values:
        value = _text(value)
        if value:
            return value
    return None


def _normalized_type(value: object) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "")


class VoiceChunkRouter:
    """Observe safe TUI events and route one active stream to the worker."""

    def __init__(self, worker: KokoroWorker, *, source: str = "codex-tui") -> None:
        self.worker = worker
        self.source = _text(source) or "codex-tui"
        self.active: StreamIdentity | None = None
        self.last_sequence: int | None = None
        self.started = False

    def start(self) -> bool:
        if self.started:
            return True
        self.started = self.worker.start()
        return self.started

    def close(self) -> None:
        if self.active is not None:
            self._send("cancel", self.active, reason="bridge_closed")
            self.active = None
        self.worker.close()
        self.started = False

    def handle_line(self, line: str) -> bool:
        """Observe one upstream line. Return whether it fed the worker."""

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return False
        return self.handle(message)

    def handle(self, message: object) -> bool:
        if not isinstance(message, dict):
            return False

        # This explicit envelope is the stable seam for a future TUI adapter.
        event_type = message.get("type")
        if event_type == "voice/start":
            identity = self._identity(message, message)
            return identity is not None and self._start(identity)
        if event_type == "voice/chunk":
            identity = self._identity(message, message)
            return identity is not None and self._delta(identity, message, message)
        if event_type == "voice/finish":
            return self._finish(self._identity(message, message))
        if event_type == "voice/cancel":
            return self._cancel(self._identity(message, message), reason="source_cancelled")

        # Codex app-server's visible assistant stream.  Reasoning/tool deltas
        # are deliberately not accepted here, even if they contain text.
        if message.get("method") == "item/agentMessage/delta":
            params = message.get("params")
            if not isinstance(params, dict):
                return False
            identity = self._identity(message, params)
            return identity is not None and self._delta(identity, message, params)

        if message.get("method") == "item/completed":
            params = message.get("params")
            if not isinstance(params, dict):
                return False
            item = params.get("item")
            if not isinstance(item, dict) or _normalized_type(item.get("type")) != "agentmessage":
                return False
            identity = self._identity(message, {**params, "itemId": item.get("id")})
            return self._finish(identity)

        if message.get("method") == "turn/completed":
            params = message.get("params")
            if not isinstance(params, dict):
                return False
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            identity = self._identity(message, {**params, "turnId": turn_id})
            return self._finish(identity)

        if message.get("method") in {"turn/failed", "turn/cancelled", "error"}:
            params = message.get("params")
            identity = self._identity(message, params if isinstance(params, dict) else {})
            return self._cancel(identity, reason="upstream_failed")
        return False

    def _identity(self, message: dict[str, object], fields: dict[str, object]) -> StreamIdentity | None:
        session_id = _first_string(
            fields.get("session_id"), fields.get("sessionId"),
            fields.get("thread_id"), fields.get("threadId"),
        )
        turn_id = _first_string(fields.get("turn_id"), fields.get("turnId"))
        item_id = _first_string(fields.get("item_id"), fields.get("itemId"))
        explicit = _first_string(fields.get("stream_id"), fields.get("streamId"))
        stream_id = explicit or ":".join(part for part in (session_id, turn_id, item_id) if part)
        if not stream_id:
            return None
        return StreamIdentity(stream_id, session_id, turn_id, item_id)

    def _send(self, event_type: str, identity: StreamIdentity, **fields: object) -> bool:
        if not self.started:
            return False
        event: dict[str, object] = {
            "schema": BRIDGE_SCHEMA,
            "type": event_type,
            "source": self.source,
            **identity.fields(),
        }
        event.update(fields)
        return self.worker.send(event)

    def _start(self, identity: StreamIdentity) -> bool:
        if not self.start():
            return False
        if self.active is not None and self.active.stream_id != identity.stream_id:
            self._send("cancel", self.active, reason="stream_switched")
            self.last_sequence = None
        if self.active is not None and self.active.stream_id == identity.stream_id:
            return True
        self.active = identity
        self.last_sequence = None
        return self._send("start", identity)

    def _delta(self, identity: StreamIdentity, message: dict[str, object], fields: dict[str, object]) -> bool:
        text = _first_string(fields.get("text"), fields.get("delta"))
        if not text:
            return False
        if not self._start(identity):
            return False
        sequence_value = fields.get("sequence", message.get("sequence"))
        sequence: int | None
        try:
            sequence = int(sequence_value) if sequence_value is not None else None
        except (TypeError, ValueError):
            sequence = None
        if sequence is not None and self.last_sequence is not None and sequence <= self.last_sequence:
            return False
        if sequence is not None:
            self.last_sequence = sequence

        accepted = True
        for offset in range(0, len(text), MAX_DELTA_CHARS):
            chunk = text[offset : offset + MAX_DELTA_CHARS]
            packet_fields: dict[str, object] = {"text": chunk}
            if sequence is not None:
                packet_fields["sequence"] = sequence
            if offset:
                packet_fields["chunk_offset"] = offset
            accepted = self._send("delta", identity, **packet_fields) and accepted
        return accepted

    def _matches_active(self, identity: StreamIdentity | None) -> bool:
        if self.active is None:
            return False
        if identity is None:
            return True
        if identity.stream_id == self.active.stream_id:
            return True
        # A turn-completed notification normally has thread/turn identity but
        # no item id.  Match known identity fields without allowing an
        # unrelated session or turn to close the active stream.
        compared = False
        for incoming, active in (
            (identity.session_id, self.active.session_id),
            (identity.turn_id, self.active.turn_id),
            (identity.item_id, self.active.item_id),
        ):
            if incoming:
                if active is None:
                    continue
                compared = True
                if incoming != active:
                    return False
        return compared

    def _finish(self, identity: StreamIdentity | None) -> bool:
        if not self._matches_active(identity) or self.active is None:
            return False
        current = self.active
        accepted = self._send("finish", current)
        self.active = None
        self.last_sequence = None
        return accepted

    def _cancel(self, identity: StreamIdentity | None, *, reason: str) -> bool:
        if not self._matches_active(identity) or self.active is None:
            return False
        current = self.active
        accepted = self._send("cancel", current, reason=reason)
        self.active = None
        self.last_sequence = None
        return accepted


class TuiServerBridge:
    """Proxy a TUI's JSONL connection to a child server process."""

    def __init__(
        self,
        project_root: Path,
        voice_root: Path,
        server_command: list[str],
        worker: KokoroWorker,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.server_command = server_command
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.server: subprocess.Popen[str] | None = None
        self.write_lock = threading.Lock()
        self.router = VoiceChunkRouter(worker)

    def _forward_client(self) -> None:
        server = self.server
        if server is None or server.stdin is None:
            return
        try:
            for line in self.stdin:
                with self.write_lock:
                    server.stdin.write(line)
                    server.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                server.stdin.close()
            except (OSError, ValueError):
                pass

    def run(self) -> int:
        self.voice_root.mkdir(parents=True, exist_ok=True)
        log(self.voice_root, f"starting TUI bridge server={self.server_command[0]}")
        try:
            self.server = subprocess.Popen(
                self.server_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(self.project_root),
                env=os.environ.copy(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            log(self.voice_root, f"TUI bridge server start error: {type(exc).__name__}")
            return 2

        if not self.router.start():
            log(self.voice_root, "TUI bridge worker unavailable; protocol remains active")
        forwarder = threading.Thread(
            target=self._forward_client,
            name="codex-tui-bridge-client-forwarder",
            daemon=True,
        )
        forwarder.start()
        try:
            if self.server.stdout is not None:
                for line in self.server.stdout:
                    with self.write_lock:
                        self.stdout.write(line)
                        self.stdout.flush()
                    self.router.handle_line(line)
            return self.server.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            return 0
        finally:
            self.close()

    def close(self) -> None:
        self.router.close()
        server = self.server
        self.server = None
        if server is None:
            return
        if server.poll() is None:
            try:
                server.terminate()
                server.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    server.kill()
                except OSError:
                    pass
        if server.stdin is not None:
            try:
                server.stdin.close()
            except OSError:
                pass
        if server.stdout is not None:
            try:
                server.stdout.close()
            except OSError:
                pass
        log(self.voice_root, "TUI bridge stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--voice-root", type=Path)
    parser.add_argument(
        "--server-command",
        default=os.environ.get("CODEX_TUI_SERVER_COMMAND", DEFAULT_SERVER_COMMAND),
        help="TUI/server child command; parsed without a shell",
    )
    parser.add_argument(
        "--worker-command",
        help="optional JSONL Kokoro worker command; defaults to the mock worker",
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    voice_root = (args.voice_root or project_root / ".codex-voice").resolve()
    worker: KokoroWorker
    if args.worker_command:
        worker = JsonlKokoroWorker(
            command_args(args.worker_command), cwd=project_root, voice_root=voice_root
        )
    else:
        worker = MockKokoroWorker()
        log(voice_root, "using mock Kokoro worker; no audio will be generated")
    bridge = TuiServerBridge(
        project_root,
        voice_root,
        command_args(args.server_command),
        worker,
    )
    try:
        return bridge.run()
    except KeyboardInterrupt:
        bridge.close()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
