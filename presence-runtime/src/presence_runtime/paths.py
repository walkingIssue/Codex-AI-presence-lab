"""User-level runtime paths and explicit project-root normalization."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def presence_home() -> Path:
    return codex_home() / "presence"


def normalize_project_root(root: str | Path) -> tuple[str, str]:
    path = Path(root).expanduser().resolve(strict=False)
    display = str(path)
    normalized = os.path.normcase(os.path.normpath(display))
    return normalized, display


def state_database_path() -> Path:
    return presence_home() / "state.sqlite3"


def catalog_path() -> Path:
    return presence_home() / "catalog"


def runtime_code_path() -> Path:
    return presence_home() / "runtime"


def live2d_code_path() -> Path:
    return presence_home() / "live2d-runtime"


def renderer_host_path() -> Path:
    return presence_home() / "renderer"


def worker_path() -> Path:
    return presence_home() / "worker"


def adapter_code_path() -> Path:
    return presence_home() / "adapters"


def installation_path() -> Path:
    return presence_home() / "installation.json"


def pid_path() -> Path:
    return presence_home() / "runtime.pid"


def lock_path() -> Path:
    return presence_home() / "runtime.lock"


def log_path() -> Path:
    return presence_home() / "runtime.log"


def runtime_python() -> Path:
    environment = presence_home() / ".venv"
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def provider_python(provider: str) -> Path:
    environment = presence_home() / "providers" / provider / ".venv"
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def stt_python() -> Path:
    environment = presence_home() / "stt" / ".venv"
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def bin_path() -> Path:
    return codex_home() / "bin"


def renderer_udp_port() -> int:
    """Return one stable high-frequency endpoint per user runtime home."""

    digest = hashlib.sha256(str(codex_home()).encode("utf-8")).digest()
    return 30000 + int.from_bytes(digest[:4], "big") % 20000
