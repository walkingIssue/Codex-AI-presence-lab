"""Cross-platform user-runtime process lifecycle."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .errors import ConflictError
from .managed import read_installation
from .paths import codex_home, installation_path, log_path, pid_path, presence_home, runtime_python
from .protocol import connect


SYSTEMD_UNIT = "codex-presence.service"


def _read_pid() -> int | None:
    try:
        value = int(pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _pid_running(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == "nt":
        # Windows does not provide POSIX signal-0 semantics: ``os.kill(pid,
        # 0)`` can report a healthy process as missing.  Probe a synchronize
        # handle instead so startup and shutdown do not orphan the supervisor
        # (and therefore its worker and Electron children).
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
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _control(operation: str, arguments: dict[str, Any] | None = None) -> Any:
    connection = connect(timeout=1.5)
    try:
        connection.send({"type": "control", "client": "presence-lifecycle/v0.2"})
        ready = connection.recv()
        if ready is None or ready.get("type") != "control/ready":
            raise ConflictError("Presence Runtime control handshake failed")
        connection.send(
            {
                "type": "command",
                "operation": operation,
                "arguments": arguments or {},
            }
        )
        response = connection.recv()
        if response is None or response.get("type") != "result":
            message = None
            if isinstance(response, dict):
                message = response.get("error", {}).get("message")
            raise ConflictError(str(message or "Presence Runtime command failed"))
        return response.get("result")
    finally:
        connection.close()


def status() -> dict[str, Any]:
    installed = installation_path().is_file()
    pid = _read_pid()
    process_running = _pid_running(pid)
    runtime: dict[str, Any] | None = None
    if process_running:
        try:
            runtime = _control("runtime.status")
        except (OSError, ConflictError):
            runtime = None
    return {
        "installed": installed,
        "running": runtime is not None,
        "process_running": process_running,
        "pid": pid if process_running else None,
        "responsive": runtime is not None,
        "home": str(presence_home()),
        "runtime": runtime,
    }


def _systemctl(*arguments: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def start(*, timeout: float = 30.0) -> dict[str, Any]:
    read_installation(installation_path())
    current = status()
    if current["responsive"]:
        return current
    if current["process_running"]:
        raise ConflictError(
            f"PID file points to running process {current['pid']}, but Presence IPC is unresponsive; "
            "refusing to launch a second supervisor or signal an unverified process"
        )
    stale = _read_pid()
    if stale is not None and not _pid_running(stale):
        pid_path().unlink(missing_ok=True)

    if os.name != "nt" and shutil_which("systemctl") and _unit_path().is_file():
        result = _systemctl("start", SYSTEMD_UNIT)
        if result.returncode != 0:
            raise ConflictError(
                "Could not start the user Presence Runtime service: "
                + (result.stderr.strip() or result.stdout.strip())
            )
    else:
        python = runtime_python()
        if not python.is_file():
            raise ConflictError(f"Managed runtime Python is missing: {python}")
        log = log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a", encoding="utf-8")
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            subprocess.Popen(
                [str(python), "-m", "presence_runtime.daemon"],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=presence_home(),
                close_fds=os.name != "nt",
                creationflags=flags,
                startupinfo=startupinfo,
            )
        finally:
            handle.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status()
        if current["responsive"]:
            return current
        time.sleep(0.1)
    raise ConflictError(
        f"Presence Runtime did not become healthy within {timeout:g}s; inspect {log_path()}"
    )


def stop(*, timeout: float = 15.0) -> dict[str, Any]:
    pid = _read_pid()
    if not _pid_running(pid):
        pid_path().unlink(missing_ok=True)
        return status()
    control_succeeded = False
    try:
        _control("runtime.shutdown")
        control_succeeded = True
    except (OSError, ConflictError):
        pass
    if not control_succeeded and os.name == "nt":
        raise ConflictError(
            f"Presence PID {pid} is running but did not authenticate over IPC; "
            "refusing to terminate an unverified Windows process"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_running(pid):
        time.sleep(0.1)
    if _pid_running(pid):
        if os.name != "nt" and shutil_which("systemctl") and _unit_path().is_file():
            _systemctl("stop", SYSTEMD_UNIT)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_running(pid):
            time.sleep(0.1)
    if _pid_running(pid):
        raise ConflictError(f"Presence Runtime process {pid} did not stop cleanly")
    pid_path().unlink(missing_ok=True)
    return status()


def _unit_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = Path(configured).expanduser() if configured else Path.home() / ".config"
    return root / "systemd" / "user" / SYSTEMD_UNIT


def shutil_which(command: str) -> str | None:
    # Kept behind a tiny seam so lifecycle tests do not patch global shutil.
    return __import__("shutil").which(command)


def _systemd_quote(value: str | Path) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def write_systemd_unit() -> Path | None:
    if os.name == "nt" or not shutil_which("systemctl"):
        return None
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "[Unit]",
            "Description=Codex Presence Runtime",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_systemd_quote(runtime_python())} -m presence_runtime.daemon",
            f"WorkingDirectory={_systemd_quote(presence_home())}",
            f"Environment={_systemd_quote('CODEX_HOME=' + str(codex_home()))}",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    unit.write_text(content, encoding="utf-8", newline="\n")
    _systemctl("daemon-reload")
    _systemctl("enable", SYSTEMD_UNIT)
    return unit


def remove_systemd_unit() -> None:
    if os.name == "nt":
        return
    unit = _unit_path()
    if unit.is_file():
        if shutil_which("systemctl"):
            _systemctl("disable", "--now", SYSTEMD_UNIT)
        unit.unlink()
        if shutil_which("systemctl"):
            _systemctl("daemon-reload")
