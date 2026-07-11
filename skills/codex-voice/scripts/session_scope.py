"""Persist the project-local Codex voice session scope."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


STATE_FILE_NAME = "sessions.json"
STATE_VERSION = 1
SESSION_MODE = "session"
PROJECT_MODE = "project"


def state_path(voice_root: Path) -> Path:
    return voice_root / STATE_FILE_NAME


def current_thread_id() -> str | None:
    """Return the Codex thread identifier inherited by the current tool call."""
    for variable in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    return None


def default_state(mode: str = SESSION_MODE) -> dict:
    return {
        "version": STATE_VERSION,
        "mode": mode if mode in {SESSION_MODE, PROJECT_MODE} else SESSION_MODE,
        "sessions": {},
    }


def _normalize_state(document: object, *, missing: bool = False) -> dict:
    # Existing installations predate sessions.json and were project-scoped.
    # Preserve that behavior until the user explicitly chooses a new scope.
    if missing or not isinstance(document, dict):
        return default_state(PROJECT_MODE)

    mode = document.get("mode")
    if mode not in {SESSION_MODE, PROJECT_MODE}:
        mode = SESSION_MODE
    raw_sessions = document.get("sessions")
    sessions: dict[str, dict] = {}
    if isinstance(raw_sessions, dict):
        for thread_id, details in raw_sessions.items():
            if not isinstance(thread_id, str) or not thread_id.strip():
                continue
            sessions[thread_id] = details if isinstance(details, dict) else {"enabled": True}
    return {"version": STATE_VERSION, "mode": mode, "sessions": sessions}


def load_state(voice_root: Path) -> dict:
    path = state_path(voice_root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _normalize_state({}, missing=not path.exists())
    return _normalize_state(document)


def write_state(voice_root: Path, document: dict) -> dict:
    normalized = _normalize_state(document)
    path = state_path(voice_root)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return normalized


def ensure_state_file(voice_root: Path) -> None:
    path = state_path(voice_root)
    if not path.exists():
        write_state(voice_root, default_state())


def registered_session_ids(document: dict) -> set[str]:
    if document.get("mode") == PROJECT_MODE:
        return set()
    sessions = document.get("sessions")
    return set(sessions) if isinstance(sessions, dict) else set()


def register_session(voice_root: Path, project_root: Path, thread_id: str) -> dict:
    document = load_state(voice_root)
    document["mode"] = SESSION_MODE
    sessions = document.setdefault("sessions", {})
    sessions[thread_id] = {
        "enabled": True,
        "project_root": str(project_root.resolve()),
        "registered_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    return write_state(voice_root, document)


def unregister_session(voice_root: Path, thread_id: str) -> dict:
    document = load_state(voice_root)
    sessions = document.setdefault("sessions", {})
    sessions.pop(thread_id, None)
    return write_state(voice_root, document)


def set_project_mode(voice_root: Path) -> dict:
    document = load_state(voice_root)
    document["mode"] = PROJECT_MODE
    return write_state(voice_root, document)


def is_project_mode(document: dict) -> bool:
    return document.get("mode") == PROJECT_MODE
