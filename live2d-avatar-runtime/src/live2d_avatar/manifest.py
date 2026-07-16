"""Manifest and state accessors shared by CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import AvatarRuntimeError
from .paths import model_directory, read_json, resolve_registry, validate_model_id


MANIFEST_SCHEMA = "live2d-avatar/manifest/v0.1"
STATE_SCHEMA = "live2d-avatar/state/v0.1"
PROJECT_INSTALL_SCHEMA = "live2d-avatar/project-install/v0.1"
ACTIVE_TOGGLE_SET = "active-toggle-set"


def manifest_path(registry: Path, model_id: str) -> Path:
    return model_directory(registry, model_id) / "manifest.json"


def state_path(registry: Path, model_id: str) -> Path:
    return model_directory(registry, model_id) / "state.json"


def load_manifest(registry: Path, model_id: str) -> dict[str, Any]:
    model_id = validate_model_id(model_id)
    document = read_json(manifest_path(registry, model_id))
    if document.get("schema") != MANIFEST_SCHEMA:
        raise AvatarRuntimeError(f"unsupported model manifest schema for {model_id}")
    if document.get("id") != model_id:
        raise AvatarRuntimeError("model manifest id does not match its registry directory")
    model = document.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("path"), str):
        raise AvatarRuntimeError("model manifest has no valid Live2D model path")
    actions = document.get("actions")
    if not isinstance(actions, list):
        raise AvatarRuntimeError("model manifest actions must be an array")
    state_semantics = document.get("state_semantics", ACTIVE_TOGGLE_SET)
    if state_semantics != ACTIVE_TOGGLE_SET:
        raise AvatarRuntimeError("model manifest has unsupported avatar state semantics")
    return document


def load_state(registry: Path, model_id: str) -> dict[str, Any]:
    model_id = validate_model_id(model_id)
    document = read_json(state_path(registry, model_id))
    if document.get("schema") != STATE_SCHEMA or document.get("model_id") != model_id:
        raise AvatarRuntimeError("state does not match the requested model")
    return document


def action_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions = manifest.get("actions")
    assert isinstance(actions, list)
    index: dict[str, dict[str, Any]] = {}
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("id"), str):
            raise AvatarRuntimeError("manifest contains an invalid action")
        action_id = action["id"]
        if action_id in index:
            raise AvatarRuntimeError(f"manifest contains duplicate action id: {action_id}")
        index[action_id] = action
    return index


def list_models(registry: Path) -> list[dict[str, Any]]:
    root = resolve_registry(registry)
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            manifest = load_manifest(root, child.name)
        except AvatarRuntimeError as exc:
            result.append({"id": child.name, "status": "invalid", "error": str(exc)})
            continue
        result.append(
            {
                "id": manifest["id"],
                "status": "available",
                "model_path": manifest["model"]["path"],
                "action_count": len(manifest["actions"]),
            }
        )
    return result
