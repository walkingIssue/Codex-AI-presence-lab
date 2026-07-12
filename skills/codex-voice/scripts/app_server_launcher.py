"""Launch the stock Codex TUI through a local app-server WebSocket proxy.

The launcher starts a local Codex app-server, exposes a second localhost
WebSocket endpoint, and starts the stock Codex client with ``--remote`` aimed
at that endpoint. WebSocket frames are forwarded unchanged. Server-to-client
text messages are observed for the existing voice and Orb side channels.

This module intentionally implements the small RFC 6455 subset needed by the
local app-server protocol with the Python standard library, so the launcher
does not add another runtime dependency to the skill.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from app_server_bridge import (
    ActivityTap,
    NotificationInterceptor,
    TTSStreamProxy,
    log,
    marker_enabled,
    resolve_codex,
)


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME_SIZE = 16 * 1024 * 1024


class WebSocketProtocolError(RuntimeError):
    """Raised when a peer sends an invalid local WebSocket frame."""


def websocket_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def read_http_headers(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    try:
        payload = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
        raise WebSocketProtocolError("incomplete WebSocket handshake") from exc
    lines = payload.decode("iso-8859-1").split("\r\n")
    if not lines or not lines[0]:
        raise WebSocketProtocolError("missing WebSocket handshake status line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return lines[0], headers


async def accept_websocket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    status, headers = await read_http_headers(reader)
    if not status.startswith("GET "):
        raise WebSocketProtocolError("expected a WebSocket GET request")
    key = headers.get("sec-websocket-key")
    if not key:
        raise WebSocketProtocolError("missing Sec-WebSocket-Key")
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {websocket_accept(key)}\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(response)
    await writer.drain()


async def connect_websocket(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(request)
    await writer.drain()
    status, headers = await read_http_headers(reader)
    if not status.startswith("HTTP/1.1 101"):
        raise WebSocketProtocolError(f"upstream rejected WebSocket handshake: {status}")
    if headers.get("sec-websocket-accept") != websocket_accept(key):
        raise WebSocketProtocolError("upstream WebSocket accept key did not match")


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    try:
        first, second = await reader.readexactly(2)
    except asyncio.IncompleteReadError as exc:
        raise EOFError from exc
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > MAX_FRAME_SIZE:
        raise WebSocketProtocolError("WebSocket frame exceeds local safety limit")
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length)
    if mask is not None:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return fin, opcode, payload


async def write_frame(
    writer: asyncio.StreamWriter,
    fin: bool,
    opcode: int,
    payload: bytes,
    *,
    mask: bool,
) -> None:
    if len(payload) > MAX_FRAME_SIZE:
        raise WebSocketProtocolError("WebSocket frame exceeds local safety limit")
    first = (0x80 if fin else 0) | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([first, (0x80 if mask else 0) | length])
    elif length <= 0xFFFF:
        header = bytes([first, (0x80 if mask else 0) | 126]) + struct.pack(">H", length)
    else:
        header = bytes([first, (0x80 if mask else 0) | 127]) + struct.pack(">Q", length)
    if mask:
        mask_key = os.urandom(4)
        payload = mask_key + bytes(
            value ^ mask_key[index % 4] for index, value in enumerate(payload)
        )
    writer.write(header + payload)
    await writer.drain()


async def relay_frames(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    target_requires_mask: bool,
    observer: Callable[[bytes], None] | None = None,
) -> None:
    message_opcode: int | None = None
    fragments: list[bytes] = []
    while True:
        fin, opcode, payload = await read_frame(reader)
        await write_frame(
            writer,
            fin,
            opcode,
            payload,
            mask=target_requires_mask,
        )
        if opcode == 0x8:  # close
            return
        if opcode in {0x9, 0xA}:  # ping/pong are control frames, not messages
            continue
        if opcode in {0x1, 0x2}:
            if message_opcode is not None:
                raise WebSocketProtocolError("new WebSocket message before continuation ended")
            message_opcode = opcode
            fragments = [payload]
        elif opcode == 0x0:
            if message_opcode is None:
                raise WebSocketProtocolError("unexpected WebSocket continuation frame")
            fragments.append(payload)
        else:
            raise WebSocketProtocolError(f"unsupported WebSocket opcode: {opcode}")
        if fin:
            message = b"".join(fragments)
            if message_opcode == 0x1 and observer is not None:
                observer(message)
            message_opcode = None
            fragments = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for_port(process: subprocess.Popen[str], port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Codex app-server exited with code {process.returncode}")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for Codex app-server WebSocket on port {port}")


def _split_client_command(command: str) -> list[str]:
    """Split a client template without corrupting Windows paths."""

    if os.name != "nt":
        return shlex.split(command, posix=True)

    # PowerShell can remove the template's inner quotes before Python sees
    # them.  POSIX parsing then treats the backslashes in C:\\Users\\... as
    # escape characters.  Windows parsing keeps those backslashes literal;
    # remove only the quote characters that shlex preserves in this mode.
    parts = shlex.split(command, posix=False)
    return [
        part[1:-1]
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}
        else part
        for part in parts
    ]


def client_arguments(command: str | None, codex: str, remote: str) -> list[str]:
    if command:
        parts = _split_client_command(command.replace("{remote}", remote))
        resolved_codex = resolve_codex(codex)
        return [part.replace("{codex}", resolved_codex) for part in parts]
    return [resolve_codex(codex), "--remote", remote]


def bridge_environment() -> dict[str, str]:
    """Prevent the project Stop hook from speaking the same turn twice."""

    environment = os.environ.copy()
    environment["CODEX_TTS_DISABLE"] = "1"
    return environment


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                return bool(
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    and exit_code.value == 259
                )
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class AppServerLauncher:
    def __init__(
        self,
        project_root: Path,
        voice_root: Path,
        codex: str,
        client_command: str | None,
        upstream_port: int,
        bridge_port: int,
        voice_enabled: bool,
        activity_enabled: bool,
    ) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.codex = codex
        self.client_command = client_command
        self.upstream_port = upstream_port or free_port()
        self.bridge_port = bridge_port or free_port()
        self.upstream: subprocess.Popen[str] | None = None
        self.client: subprocess.Popen[str] | None = None
        self.server: asyncio.AbstractServer | None = None
        self.connection_closed = asyncio.Event()
        self.bridge_active_marker = voice_root / "bridge.active"
        self.bridge_marker_owned = False
        self.tts = TTSStreamProxy(project_root, voice_root, voice_enabled)
        self.activity = ActivityTap(voice_root, activity_enabled)
        self.interceptor = NotificationInterceptor(self.tts, self.activity, voice_root)

    def _capture_upstream_stderr(self) -> None:
        process = self.upstream
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                if line.strip():
                    log(self.voice_root, "upstream WebSocket stderr received")
        except (OSError, ValueError):
            pass

    def _activate_bridge_marker(self) -> None:
        marker = self.bridge_active_marker
        if marker.is_file():
            try:
                existing_pid = int(marker.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                existing_pid = 0
            if existing_pid != os.getpid() and process_is_alive(existing_pid):
                raise RuntimeError(
                    f"Another Codex app-server bridge is already active (PID {existing_pid})"
                )
            try:
                marker.unlink()
            except OSError as exc:
                raise RuntimeError(f"Could not clear stale bridge marker: {exc}") from exc
        try:
            marker.write_text(str(os.getpid()), encoding="ascii")
        except OSError as exc:
            raise RuntimeError(f"Could not create bridge marker: {exc}") from exc
        self.bridge_marker_owned = True

    def _deactivate_bridge_marker(self) -> None:
        if not self.bridge_marker_owned:
            return
        try:
            if self.bridge_active_marker.read_text(encoding="ascii").strip() == str(os.getpid()):
                self.bridge_active_marker.unlink(missing_ok=True)
        except OSError:
            pass
        self.bridge_marker_owned = False

    async def start(self) -> None:
        self._activate_bridge_marker()
        codex = resolve_codex(self.codex)
        command = [codex, "app-server", "--listen", f"ws://127.0.0.1:{self.upstream_port}"]
        log(self.voice_root, f"starting WebSocket app-server: {codex}")
        try:
            self.upstream = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                cwd=str(self.project_root),
                env=bridge_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise RuntimeError(f"Could not start Codex app-server: {exc}") from exc
        threading.Thread(
            target=self._capture_upstream_stderr,
            name="codex-launcher-upstream-stderr",
            daemon=True,
        ).start()
        await wait_for_port(self.upstream, self.upstream_port)
        self.tts.start()
        self.server = await asyncio.start_server(
            self.handle_client,
            host="127.0.0.1",
            port=self.bridge_port,
        )
        sockets = self.server.sockets or []
        if sockets:
            self.bridge_port = int(sockets[0].getsockname()[1])
        print(
            f"Codex bridge ready: ws://127.0.0.1:{self.bridge_port} "
            f"(upstream {self.upstream_port})",
            flush=True,
        )

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", self.upstream_port
            )
            await connect_websocket(
                upstream_reader,
                upstream_writer,
                "127.0.0.1",
                self.upstream_port,
            )
            await accept_websocket(reader, writer)
            log(self.voice_root, "Codex client connected through WebSocket launcher")
            tasks = {
                asyncio.create_task(
                    relay_frames(
                        reader,
                        upstream_writer,
                        target_requires_mask=True,
                    )
                ),
                asyncio.create_task(
                    relay_frames(
                        upstream_reader,
                        writer,
                        target_requires_mask=False,
                        observer=self._observe_server_message,
                    )
                ),
            }
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exception = task.exception()
                if exception is not None and not isinstance(exception, EOFError):
                    log(self.voice_root, f"WebSocket relay ended: {type(exception).__name__}")
        except (OSError, EOFError, WebSocketProtocolError) as exc:
            log(self.voice_root, f"WebSocket client connection ended: {type(exc).__name__}")
        finally:
            self.connection_closed.set()
            for stream in (upstream_writer, writer):
                if stream is not None:
                    stream.close()
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass

    def _observe_server_message(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self.interceptor.handle(message)

    async def run(self, *, launch_client: bool, exit_after_client: bool) -> int:
        try:
            await self.start()
            if not launch_client:
                if exit_after_client:
                    await self.connection_closed.wait()
                else:
                    await asyncio.Event().wait()
                return 0

            remote = f"ws://127.0.0.1:{self.bridge_port}"
            command = client_arguments(self.client_command, self.codex, remote)
            log(self.voice_root, "launching Codex client through WebSocket bridge")
            try:
                self.client = subprocess.Popen(
                    command,
                    cwd=str(self.project_root),
                    env=bridge_environment(),
                )
            except OSError as exc:
                raise RuntimeError(f"Could not launch Codex client: {exc}") from exc
            while self.client.poll() is None:
                await asyncio.sleep(0.2)
            return int(self.client.returncode or 0)
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.tts.stop()
        self.activity.close()
        for process in (self.client, self.upstream):
            if process is None or process.poll() is not None:
                continue
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        self._deactivate_bridge_marker()
        log(self.voice_root, "WebSocket app-server launcher stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--voice-root", type=Path)
    parser.add_argument("--codex", default=os.environ.get("CODEX_CLI", "codex"))
    parser.add_argument(
        "--client-command",
        help="client command template; use {remote} and {codex} placeholders",
    )
    parser.add_argument("--upstream-port", type=int, default=0)
    parser.add_argument("--bridge-port", type=int, default=0)
    parser.add_argument("--no-client", action="store_true", help="start only the proxy")
    parser.add_argument(
        "--exit-after-client",
        action="store_true",
        help="when --no-client is used, exit after one client disconnects",
    )
    parser.add_argument("--no-voice", action="store_true")
    parser.add_argument("--no-activity", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    voice_root = (args.voice_root or project_root / ".codex-voice").resolve()
    launcher = AppServerLauncher(
        project_root,
        voice_root,
        args.codex,
        args.client_command or os.environ.get("CODEX_CLIENT_COMMAND"),
        args.upstream_port,
        args.bridge_port,
        not args.no_voice and marker_enabled(voice_root / "enabled"),
        not args.no_activity,
    )
    try:
        return asyncio.run(
            launcher.run(
                launch_client=not args.no_client,
                exit_after_client=args.exit_after_client,
            )
        )
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, TimeoutError) as exc:
        log(voice_root, f"launcher error: {type(exc).__name__}: {exc}")
        print(f"Codex bridge launcher failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
