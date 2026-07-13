"""Small local clipboard adapter used by the safe voice-input fallback."""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import time


class ClipboardError(RuntimeError):
    """The local desktop clipboard could not be updated."""


def copy_text(text: str) -> None:
    """Copy visible user text to the local clipboard without GUI automation."""
    value = str(text)
    if not value.strip():
        raise ClipboardError("clipboard text is empty")
    if os.name == "nt":
        _copy_windows(value)
        return

    for command in (("wl-copy",), ("xclip", "-selection", "clipboard")):
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(command, input=value, text=True, check=True, capture_output=True)
            return
        except OSError as exc:
            raise ClipboardError(f"{command[0]} failed: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "").strip()
            raise ClipboardError(f"{command[0]} failed{': ' + detail if detail else ''}") from exc
    raise ClipboardError("no supported local clipboard command is available")


def _copy_windows(value: str, *, attempts: int = 8) -> None:
    """Set CF_UNICODETEXT through Win32 so no PowerShell window is spawned."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    payload = value.replace("\n", "\r\n").encode("utf-16-le") + b"\x00\x00"
    last_error = 0
    for attempt in range(max(1, attempts)):
        if not user32.OpenClipboard(None):
            last_error = ctypes.get_last_error()
            time.sleep(0.025 * (attempt + 1))
            continue

        handle = None
        try:
            if not user32.EmptyClipboard():
                last_error = ctypes.get_last_error()
                continue
            handle = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
            if not handle:
                last_error = ctypes.get_last_error()
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                last_error = ctypes.get_last_error()
                continue
            ctypes.memmove(pointer, payload, len(payload))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                last_error = ctypes.get_last_error()
                continue
            handle = None  # Clipboard owns it after SetClipboardData succeeds.
            return
        finally:
            if handle:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()
        time.sleep(0.025 * (attempt + 1))

    suffix = f" (Win32 error {last_error})" if last_error else ""
    raise ClipboardError(f"Windows clipboard is busy or unavailable{suffix}")
