"""Deterministic, model-local avatar state resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import AvatarRuntimeError
from .manifest import ACTIVE_TOGGLE_SET, STATE_SCHEMA, action_index, load_manifest, load_state, state_path
from .paths import atomic_write_json


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _unique_requested(action_ids: Iterable[str]) -> list[str]:
    result: list[str] = []
    for action_id in action_ids:
        if action_id not in result:
            result.append(action_id)
    return result


def _ordered_actions(index: dict[str, dict[str, Any]], action_ids: Iterable[str]) -> list[str]:
    requested = set(action_ids)
    unknown = sorted(requested - set(index))
    if unknown:
        raise AvatarRuntimeError("unknown action id(s): " + ", ".join(unknown))
    return [action_id for action_id in index if action_id in requested]


def _require_toggle_set_semantics(manifest: dict[str, Any]) -> None:
    """Keep state changes declarative and model-local.

    A Live2D/VTube expression archive supplies independent toggle commands, not
    a trustworthy pose/conflict graph.  An omitted value is accepted for
    backward compatibility with pre-profile manifests and means the same
    active-toggle-set behavior.
    """

    if manifest.get("state_semantics", ACTIVE_TOGGLE_SET) != ACTIVE_TOGGLE_SET:
        raise AvatarRuntimeError("model does not support active-toggle-set state semantics")


def _compiled_operations(index: dict[str, dict[str, Any]], action_ids: list[str]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for action_id in action_ids:
        raw_operations = index[action_id].get("parameter_operations", [])
        if not isinstance(raw_operations, list):
            raise AvatarRuntimeError(f"invalid parameter operations for action: {action_id}")
        for operation in raw_operations:
            if not isinstance(operation, dict):
                raise AvatarRuntimeError(f"invalid parameter operation for action: {action_id}")
            parameter_id = operation.get("parameter_id")
            value = operation.get("value")
            blend = operation.get("blend")
            if not isinstance(parameter_id, str) or not isinstance(value, (int, float)) or not isinstance(blend, str):
                raise AvatarRuntimeError(f"invalid parameter operation for action: {action_id}")
            operations.append(
                {
                    "action_id": action_id,
                    "parameter_id": parameter_id,
                    "value": float(value),
                    "blend": blend,
                }
            )
    return operations


def _write_resolved_state(
    registry: Path, model_id: str, manifest: dict[str, Any], previous: dict[str, Any], action_ids: Iterable[str]
) -> dict[str, Any]:
    _require_toggle_set_semantics(manifest)
    index = action_index(manifest)
    resolved_actions = _ordered_actions(index, _unique_requested(action_ids))
    previous_actions = previous.get("active_actions")
    if not isinstance(previous_actions, list) or not all(isinstance(item, str) for item in previous_actions):
        raise AvatarRuntimeError("existing state has invalid active_actions")
    if resolved_actions == previous_actions:
        return previous
    revision = previous.get("revision")
    if not isinstance(revision, int) or revision < 0:
        raise AvatarRuntimeError("existing state has invalid revision")
    state = {
        "schema": STATE_SCHEMA,
        "model_id": model_id,
        "revision": revision + 1,
        "active_actions": resolved_actions,
        "effective_parameter_operations": _compiled_operations(index, resolved_actions),
        "updated_at": _now(),
    }
    atomic_write_json(state_path(registry, model_id), state)
    return state


def show_state(registry: Path, model_id: str) -> dict[str, Any]:
    load_manifest(registry, model_id)
    return load_state(registry, model_id)


def set_actions(registry: Path, model_id: str, action_ids: Iterable[str]) -> dict[str, Any]:
    manifest = load_manifest(registry, model_id)
    previous = load_state(registry, model_id)
    return _write_resolved_state(registry, model_id, manifest, previous, action_ids)


def enable_actions(registry: Path, model_id: str, action_ids: Iterable[str]) -> dict[str, Any]:
    manifest = load_manifest(registry, model_id)
    previous = load_state(registry, model_id)
    existing = previous.get("active_actions")
    if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
        raise AvatarRuntimeError("existing state has invalid active_actions")
    index = action_index(manifest)
    requested = _unique_requested(action_ids)
    _ordered_actions(index, requested)
    _ordered_actions(index, existing)

    candidate = list(existing)
    for action_id in requested:
        if action_id not in candidate:
            candidate.append(action_id)
    return _write_resolved_state(registry, model_id, manifest, previous, candidate)


def disable_actions(registry: Path, model_id: str, action_ids: Iterable[str]) -> dict[str, Any]:
    manifest = load_manifest(registry, model_id)
    previous = load_state(registry, model_id)
    existing = previous.get("active_actions")
    if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
        raise AvatarRuntimeError("existing state has invalid active_actions")
    index = action_index(manifest)
    requested = _unique_requested(action_ids)
    _ordered_actions(index, requested)
    _ordered_actions(index, existing)
    return _write_resolved_state(
        registry,
        model_id,
        manifest,
        previous,
        [action_id for action_id in existing if action_id not in requested],
    )
