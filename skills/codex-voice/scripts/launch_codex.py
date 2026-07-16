"""Launch the stock Codex TUI through the local presence WebSocket adapter.

Visible assistant output is handed to the project-local adapter inbox. The
user-level global PlaybackArbiter owns the single warm Kokoro worker; this
launcher never loads Kokoro or creates a per-session speech process.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from activity import ActivityEmitter
from tui_bridge import ArbiterInboxAdapter, VoiceChunkRouter


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_SIZE = 16 * 1024 * 1024


def log(voice_root: Path, message: str) -> None:
    try:
        with (voice_root / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(f"launch: {message}\n")
    except OSError:
        pass


def resolve_codex(value: str) -> str:
    configured = os.environ.get("CODEX_CLI")
    if value in {"codex", "codex.exe"} and configured:
        return configured
    return value


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def marker_enabled(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip().lower() in {"1", "true", "on", "enabled"}
    except OSError:
        return False


def runtime_python(voice_root: Path) -> Path:
    try:
        provider = (voice_root / "provider").read_text(encoding="utf-8").strip().lower()
    except OSError:
        provider = "cpu"
    environment = voice_root / (".openvino-venv" if provider == "openvino" else ".venv")
    executable = "python.exe" if os.name == "nt" else "python"
    candidate = environment / ("Scripts" if os.name == "nt" else "bin") / executable
    return candidate if candidate.is_file() else Path(sys.executable)


def websocket_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def headers(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("iso-8859-1").split("\r\n")
    values: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            values[name.strip().lower()] = value.strip()
    return lines[0] if lines else "", values


async def accept_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    status, values = await headers(reader)
    key = values.get("sec-websocket-key")
    if not status.startswith("GET ") or not key:
        raise RuntimeError("invalid TUI WebSocket handshake")
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {websocket_accept(key)}\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()


async def connect_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    writer.write(
        (
            f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    status, values = await headers(reader)
    if not status.startswith("HTTP/1.1 101") or values.get("sec-websocket-accept") != websocket_accept(key):
        raise RuntimeError("upstream app-server rejected the WebSocket handshake")


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    first, second = await reader.readexactly(2)
    final = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > MAX_FRAME_SIZE:
        raise RuntimeError("WebSocket frame is too large")
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length)
    if mask is not None:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return final, opcode, payload


async def write_frame(writer: asyncio.StreamWriter, final: bool, opcode: int, payload: bytes, *, mask: bool) -> None:
    length = len(payload)
    if length > MAX_FRAME_SIZE:
        raise RuntimeError("WebSocket frame is too large")
    first = (0x80 if final else 0) | opcode
    if length < 126:
        header = bytes((first, (0x80 if mask else 0) | length))
    elif length <= 0xFFFF:
        header = bytes((first, (0x80 if mask else 0) | 126)) + struct.pack(">H", length)
    else:
        header = bytes((first, (0x80 if mask else 0) | 127)) + struct.pack(">Q", length)
    if mask:
        key = os.urandom(4)
        payload = bytes(key[index % 4] ^ value for index, value in enumerate(payload))
        header += key
    writer.write(header + payload)
    await writer.drain()


async def relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    mask_target: bool,
    observe: Callable[[bytes], None] | None = None,
) -> None:
    message_opcode: int | None = None
    fragments: list[bytes] = []
    while True:
        final, opcode, payload = await read_frame(reader)
        await write_frame(writer, final, opcode, payload, mask=mask_target)
        if opcode == 0x8:
            return
        if opcode in {0x9, 0xA}:
            continue
        if opcode in {0x1, 0x2}:
            message_opcode = opcode
            fragments = [payload]
        elif opcode == 0x0 and message_opcode is not None:
            fragments.append(payload)
        if final and message_opcode is not None:
            combined = b"".join(fragments)
            if message_opcode == 0x1 and observe is not None:
                observe(combined)
            message_opcode = None
            fragments = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_port(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"app-server exited with code {process.returncode}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError("timed out waiting for Codex app-server")


def activity_state(message: dict[str, object]) -> str | None:
    method = message.get("method")
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "turn/started":
        return "thinking"
    if method == "turn/completed":
        return "idle"
    if method in {"error", "warning"}:
        return "error"
    if method == "item/started":
        item = params.get("item")
        kind = str(item.get("type", "")).lower() if isinstance(item, dict) else ""
        if any(value in kind for value in ("command", "shell", "filechange")):
            return "cli"
        if any(value in kind for value in ("tool", "function", "search", "mcp")):
            return "tool"
        if any(value in kind for value in ("skill", "hook")):
            return "skill"
        return "thinking"
    return None


class CodexPresenceLauncher:
    def __init__(self, project_root: Path, voice_root: Path, codex: str, client_args: list[str]) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.codex = resolve_codex(codex)
        self.client_args = client_args
        self.upstream: subprocess.Popen[str] | None = None
        self.client: subprocess.Popen[str] | None = None
        self.server: asyncio.AbstractServer | None = None
        self._upstream_port = 0
        self.bridge_marker_owned = False
        self.arbiter_adapter: ArbiterInboxAdapter | None = None
        self.router: VoiceChunkRouter | None = None
        self.activity: ActivityEmitter | None = None

    def activate_marker(self) -> None:
        marker = self.voice_root / "bridge.active"
        if marker.is_file():
            try:
                pid = int(marker.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pid = 0
            if pid != os.getpid() and process_alive(pid):
                raise RuntimeError(f"another Codex presence bridge is active (PID {pid})")
            marker.unlink(missing_ok=True)
        marker.write_text(f"{os.getpid()}\n", encoding="ascii")
        self.bridge_marker_owned = True

    def deactivate_marker(self) -> None:
        marker = self.voice_root / "bridge.active"
        if not self.bridge_marker_owned:
            return
        try:
            if marker.read_text(encoding="ascii").strip() == str(os.getpid()):
                marker.unlink(missing_ok=True)
        except OSError:
            pass
        self.bridge_marker_owned = False

    def observe(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        if self.router is not None:
            self.router.handle(message)
        state = activity_state(message)
        if state is not None and self.activity is not None:
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            session_id = params.get("threadId")
            self.activity.send(state, source="codex-app-server", session_id=session_id if isinstance(session_id, str) else None)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader = upstream_writer = None
        observer_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def observe_loop() -> None:
            while True:
                payload = await observer_queue.get()
                try:
                    if payload is None:
                        return
                    await asyncio.to_thread(self.observe, payload)
                finally:
                    observer_queue.task_done()

        observer_task = asyncio.create_task(observe_loop())
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self._upstream_port)
            await connect_upstream(upstream_reader, upstream_writer, "127.0.0.1", self._upstream_port)
            await accept_client(reader, writer)
            tasks = {
                asyncio.create_task(relay(reader, upstream_writer, mask_target=True)),
                asyncio.create_task(relay(upstream_reader, writer, mask_target=False, observe=observer_queue.put_nowait)),
            }
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await observer_queue.join()
        except (EOFError, OSError, RuntimeError, asyncio.IncompleteReadError) as exc:
            log(self.voice_root, f"client connection ended: {type(exc).__name__}")
        finally:
            observer_queue.put_nowait(None)
            await asyncio.gather(observer_task, return_exceptions=True)
            for stream in (upstream_writer, writer):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass

    async def run(self) -> int:
        self.voice_root.mkdir(parents=True, exist_ok=True)
        self.activate_marker()
        self._upstream_port = free_port()
        environment = os.environ.copy()
        environment["CODEX_TTS_DISABLE"] = "1"
        command = [self.codex, "app-server", "--listen", f"ws://127.0.0.1:{self._upstream_port}"]
        try:
            self.upstream = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(self.project_root),
                env=environment,
            )
            await wait_for_port(self.upstream, self._upstream_port)
            self.arbiter_adapter = ArbiterInboxAdapter(self.project_root, self.voice_root)
            self.router = VoiceChunkRouter(self.arbiter_adapter, source="codex-app-server")
            self.router.start()
            if marker_enabled(self.voice_root / "orb.enabled"):
                self.activity = ActivityEmitter(voice_root=self.voice_root)
            self.server = await asyncio.start_server(self.handle_client, "127.0.0.1", 0)
            bridge_port = int(self.server.sockets[0].getsockname()[1]) if self.server.sockets else 0
            remote = f"ws://127.0.0.1:{bridge_port}"
            client_command = [self.codex, "--remote", remote, *self.client_args]
            log(self.voice_root, f"starting stock Codex TUI through {remote}")
            self.client = subprocess.Popen(client_command, cwd=str(self.project_root), env=environment)
            while self.client.poll() is None:
                await asyncio.sleep(0.2)
            return int(self.client.returncode or 0)
        finally:
            await self.close()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.router is not None:
            self.router.close()
            self.router = None
        if self.activity is not None:
            self.activity.send("idle", source="codex-app-server")
            self.activity.close()
            self.activity = None
        for process in (self.client, self.upstream):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.client = None
        self.upstream = None
        self.deactivate_marker()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--voice-root", type=Path)
    parser.add_argument("--codex", default=os.environ.get("CODEX_CLI", "codex"))
    parser.add_argument("client_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    voice_root = (args.voice_root or project_root / ".codex-voice").resolve()
    launcher = CodexPresenceLauncher(project_root, voice_root, args.codex, args.client_args)
    try:
        return asyncio.run(launcher.run())
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, TimeoutError) as exc:
        log(voice_root, f"launcher failed: {type(exc).__name__}: {exc}")
        print(f"Codex presence launcher failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
