"""Ownership-aware registration for the optional Live2D Codex context hook."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .errors import AvatarRuntimeError
from .paths import atomic_write_json, atomic_write_text, project_runtime_directory, read_json


CONTEXT_HOOK_SCHEMA = "live2d-avatar/context-hook/v0.1"
CONTEXT_HOOK_FILE = "live2d_context_hook.py"
HOOK_EVENT = "UserPromptSubmit"
STATUS_MESSAGE = "Loading Live2D avatar context"


def _hook_template() -> str:
    path = Path(__file__).resolve().parent / "assets" / CONTEXT_HOOK_FILE
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AvatarRuntimeError(f"Live2D context hook template is unavailable: {path}") from exc


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quoted_command(python: Path, script: Path) -> str:
    if '"' in str(python) or '"' in str(script):
        raise AvatarRuntimeError("Live2D context hook path cannot contain a quote")
    return f'"{python}" "{script}"'


def _load_hook_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    if path.is_symlink():
        raise AvatarRuntimeError(f"refusing to modify symbolic-link hook configuration: {path}")
    document = read_json(path)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise AvatarRuntimeError("Codex hook configuration has no hooks object")
    return document


def _is_our_handler(value: Any, script: Path) -> bool:
    if not isinstance(value, dict) or value.get("statusMessage") != STATUS_MESSAGE:
        return False
    script_text = str(script).casefold()
    command = value.get("command")
    command_windows = value.get("commandWindows")
    return any(
        isinstance(candidate, str) and script_text in candidate.casefold()
        for candidate in (command, command_windows)
    )


def _remove_our_handlers(document: dict[str, Any], script: Path) -> int:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise AvatarRuntimeError("Codex hook configuration has no hooks object")
    entries = hooks.get(HOOK_EVENT)
    if entries is None:
        return 0
    if not isinstance(entries, list):
        raise AvatarRuntimeError(f"Codex {HOOK_EVENT} hook configuration must be an array")
    removed = 0
    retained_entries: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict):
            retained_entries.append(entry)
            continue
        handlers = entry.get("hooks")
        if not isinstance(handlers, list):
            retained_entries.append(entry)
            continue
        retained_handlers = [handler for handler in handlers if not _is_our_handler(handler, script)]
        removed += len(handlers) - len(retained_handlers)
        if retained_handlers:
            updated = dict(entry)
            updated["hooks"] = retained_handlers
            retained_entries.append(updated)
        elif set(entry).difference({"hooks"}):
            updated = dict(entry)
            updated["hooks"] = []
            retained_entries.append(updated)
    if retained_entries:
        hooks[HOOK_EVENT] = retained_entries
    else:
        hooks.pop(HOOK_EVENT, None)
    return removed


def _add_our_handler(document: dict[str, Any], python: Path, script: Path) -> None:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise AvatarRuntimeError("Codex hook configuration has no hooks object")
    _remove_our_handlers(document, script)
    command = _quoted_command(python, script)
    handlers = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(handlers, list):
        raise AvatarRuntimeError(f"Codex {HOOK_EVENT} hook configuration must be an array")
    handlers.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "commandWindows": command,
                    "timeout": 5,
                    "statusMessage": STATUS_MESSAGE,
                }
            ]
        }
    )


def _empty_hook_document(document: dict[str, Any]) -> bool:
    return set(document) == {"hooks"} and document.get("hooks") == {}


def _installed_runtime_python() -> Path:
    runtime = Path.home() / ".codex" / "live2d-avatar-runtime"
    python = runtime / ("venv/Scripts/python.exe" if __import__("os").name == "nt" else "venv/bin/python")
    if not python.is_file():
        raise AvatarRuntimeError(
            "installed Live2D runtime is unavailable; reinstall it before enabling the context hook"
        )
    return python


def _write_project_manifest(project: Path, installation: dict[str, Any]) -> None:
    # Imported lazily to keep lifecycle -> hook-registration cleanup free of an import cycle.
    from .lifecycle import INSTALLATION_FILE, PROJECT_MANIFEST_FILE, _project_manifest_text

    runtime = project_runtime_directory(project)
    model_id = installation.get("model_id")
    registry = installation.get("registry")
    if not isinstance(model_id, str) or not isinstance(registry, str):
        raise AvatarRuntimeError("project installation is missing its model binding")
    atomic_write_json(runtime / INSTALLATION_FILE, installation)
    atomic_write_text(
        runtime / PROJECT_MANIFEST_FILE,
        _project_manifest_text(
            project,
            model_id,
            Path(registry),
            str(installation.get("created_at", "unknown")),
            installation,
        ),
    )


def enable_context_hook(project: Path, *, runtime_python: Path | None = None) -> dict[str, Any]:
    """Install one project-owned UserPromptSubmit handler without touching voice hooks."""

    from .lifecycle import read_project_installation

    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    runtime = project_runtime_directory(project)
    script = runtime / CONTEXT_HOOK_FILE
    if script.is_symlink():
        raise AvatarRuntimeError(f"refusing to replace symbolic-link context hook: {script}")
    python = (runtime_python or _installed_runtime_python()).expanduser().resolve()
    if not python.is_file():
        raise AvatarRuntimeError("context-hook Python executable does not exist")

    hooks_directory = project / ".codex"
    hooks_path = hooks_directory / "hooks.json"
    hooks_directory_existed = hooks_directory.exists()
    config_existed = hooks_path.exists()
    existing_hook = installation.get("context_hook")
    old_config_created = existing_hook.get("config_created") if isinstance(existing_hook, dict) else False
    old_directory_created = existing_hook.get("hooks_directory_created") if isinstance(existing_hook, dict) else False
    document = _load_hook_document(hooks_path)

    atomic_write_text(script, _hook_template())
    _add_our_handler(document, python, script)
    atomic_write_json(hooks_path, document)
    installation["context_hook"] = {
        "schema": CONTEXT_HOOK_SCHEMA,
        "script": CONTEXT_HOOK_FILE,
        "script_sha256": _checksum(script),
        "config_path": ".codex/hooks.json",
        "event": HOOK_EVENT,
        "status_message": STATUS_MESSAGE,
        "config_created": bool(old_config_created or not config_existed),
        "hooks_directory_created": bool(old_directory_created or not hooks_directory_existed),
    }
    owned_paths = installation.get("owned_paths", [])
    if not isinstance(owned_paths, list) or not all(isinstance(item, str) for item in owned_paths):
        raise AvatarRuntimeError("project installation has invalid owned paths")
    installation["owned_paths"] = sorted(set(owned_paths) | {CONTEXT_HOOK_FILE})
    _write_project_manifest(project, installation)
    return {
        "project": str(project),
        "event": HOOK_EVENT,
        "status": "enabled",
        "handler_count": 1,
    }


def disable_context_hook(project: Path, *, for_uninstall: bool = False) -> dict[str, Any]:
    """Remove only this runtime's UserPromptSubmit handler and generated script."""

    from .lifecycle import read_project_installation

    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    record = installation.get("context_hook")
    if record is None:
        return {"project": str(project), "status": "not-enabled", "handler_count": 0}
    if not isinstance(record, dict) or record.get("schema") != CONTEXT_HOOK_SCHEMA:
        raise AvatarRuntimeError("project context-hook ownership record is invalid")
    if record.get("script") != CONTEXT_HOOK_FILE or record.get("event") != HOOK_EVENT:
        raise AvatarRuntimeError("project context-hook ownership record does not match this runtime")

    runtime = project_runtime_directory(project)
    script = runtime / CONTEXT_HOOK_FILE
    hooks_directory = project / ".codex"
    hooks_path = hooks_directory / "hooks.json"
    removed = 0
    if hooks_path.exists():
        document = _load_hook_document(hooks_path)
        removed = _remove_our_handlers(document, script)
        if bool(record.get("config_created")) and _empty_hook_document(document):
            hooks_path.unlink()
            if bool(record.get("hooks_directory_created")) and hooks_directory.exists() and not any(hooks_directory.iterdir()):
                hooks_directory.rmdir()
        else:
            atomic_write_json(hooks_path, document)
    if not for_uninstall and script.exists():
        if script.is_symlink() or not script.is_file():
            raise AvatarRuntimeError("refusing to remove an unsafe context-hook script")
        script.unlink()
    if not for_uninstall:
        installation.pop("context_hook", None)
        owned_paths = installation.get("owned_paths", [])
        if not isinstance(owned_paths, list) or not all(isinstance(item, str) for item in owned_paths):
            raise AvatarRuntimeError("project installation has invalid owned paths")
        installation["owned_paths"] = [item for item in owned_paths if item != CONTEXT_HOOK_FILE]
        _write_project_manifest(project, installation)
    return {
        "project": str(project),
        "status": "removed" if removed else "not-registered",
        "handler_count": removed,
    }


def context_hook_status(project: Path) -> dict[str, Any]:
    from .lifecycle import read_project_installation

    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    record = installation.get("context_hook")
    runtime = project_runtime_directory(project)
    script = runtime / CONTEXT_HOOK_FILE
    if not isinstance(record, dict) or record.get("schema") != CONTEXT_HOOK_SCHEMA:
        return {"project": str(project), "status": "not-enabled"}
    hooks_path = project / ".codex" / "hooks.json"
    registered = False
    if hooks_path.exists():
        document = _load_hook_document(hooks_path)
        entries = document.get("hooks", {}).get(HOOK_EVENT, [])
        if isinstance(entries, list):
            registered = any(
                isinstance(entry, dict)
                and isinstance(entry.get("hooks"), list)
                and any(_is_our_handler(handler, script) for handler in entry["hooks"])
                for entry in entries
            )
    return {
        "project": str(project),
        "event": HOOK_EVENT,
        "status": "enabled" if script.is_file() and registered else "incomplete",
        "script_present": script.is_file(),
        "registered": registered,
    }
