"""Shared process boundary for the Codex CLI adapters."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence


WINDOWS_WRAPPER_SUFFIXES = {".bat", ".cmd"}


def _windows_command_args(command: str) -> list[str]:
    """Parse a Windows command line with the same rules as CreateProcess."""

    import ctypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    argc = ctypes.c_int()
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        error = ctypes.get_last_error()
        raise ValueError(f"Windows command line could not be parsed (error {error})")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def command_args(command: str) -> list[str]:
    """Parse a configured child command without invoking a shell."""

    if not isinstance(command, str) or not command.strip():
        raise ValueError("bridge command cannot be empty")
    try:
        args = _windows_command_args(command) if os.name == "nt" else shlex.split(command, posix=True)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid bridge command: {exc}") from exc
    if not args:
        raise ValueError("bridge command cannot be empty")
    return args


def prepare_command(command: Sequence[str]) -> list[str]:
    """Make a command launchable without a shell on the current platform."""

    args = list(command)
    if not args:
        raise ValueError("bridge command cannot be empty")
    if os.name == "nt" and Path(args[0]).suffix.lower() in WINDOWS_WRAPPER_SUFFIXES:
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(args)]
    return args


def _same_path(value: str | Path, excluded: Iterable[Path]) -> bool:
    try:
        candidate = Path(value).expanduser().resolve()
        return any(candidate == path.expanduser().resolve() for path in excluded)
    except OSError:
        normalized = os.path.normcase(str(value))
        return any(normalized == os.path.normcase(str(path)) for path in excluded)


def _valid_executable(value: object, excluded: Iterable[Path]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value.strip().strip('"'))
    if path.is_file() and not _same_path(path, excluded):
        return str(path)
    return None


def codex_executable(*, exclude_paths: Iterable[Path] = ()) -> str | None:
    """Resolve the configured Codex CLI, including Windows install paths."""

    excluded = tuple(exclude_paths)
    explicit = _valid_executable(os.environ.get("CODEX_CLI_PATH", ""), excluded)
    if explicit:
        return explicit
    config = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"(?m)^\s*CODEX_CLI_PATH\s*=\s*[\"']([^\"']+)[\"']\s*$", text)
    configured = _valid_executable(match.group(1) if match else None, excluded)
    if configured:
        return configured
    override_config = config.with_name("codex-voice-override.json")
    try:
        override = json.loads(override_config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        override = {}
    recorded = _valid_executable(override.get("real_cli"), excluded) if isinstance(override, dict) else None
    if recorded:
        return recorded
    search_path = os.environ.get("PATH")
    if excluded and search_path:
        entries = [entry for entry in search_path.split(os.pathsep) if not _same_path(entry, excluded)]
        search_path = os.pathsep.join(entries)
    return shutil.which("codex", path=search_path)
