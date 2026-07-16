"""User-level runtime paths and explicit project-root normalization."""

from __future__ import annotations

import os
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

