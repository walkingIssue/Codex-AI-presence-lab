"""Optional model-local semantic action profiles."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import AvatarRuntimeError
from .importer import _expression_metadata
from .manifest import ACTIVE_TOGGLE_SET, action_index, load_manifest, load_state, manifest_path
from .paths import atomic_write_json, model_directory, read_json, resolve_registry
from .state import set_actions


PROFILE_SCHEMA = "live2d-avatar/profile/v0.1"
PROFILE_TARGET_SCHEMA = "live2d-avatar/profile-target/v0.1"
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PARAMETER_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
PROFILE_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DESCRIPTION_MAX_LENGTH = 240
SPEECH_MOTION_TARGET_LIMIT = 12
SEMANTIC_STATUS_DRAFT = "draft"
SEMANTIC_STATUS_CURATED = "curated"
SEMANTIC_STATUS_VALUES = frozenset({SEMANTIC_STATUS_DRAFT, SEMANTIC_STATUS_CURATED})
ACTIVITY_STATES = ("idle", "thinking", "tool", "skill", "cli", "waiting", "error")


def _action_description(value: Any, action_id: str) -> str:
    if not isinstance(value, str):
        raise AvatarRuntimeError(f"profile description is invalid for {action_id}")
    description = " ".join(value.split())
    if not description or len(description) > DESCRIPTION_MAX_LENGTH:
        raise AvatarRuntimeError(f"profile description is invalid for {action_id}")
    if any(ord(character) < 32 or ord(character) == 127 for character in description):
        raise AvatarRuntimeError(f"profile description is invalid for {action_id}")
    return description


def _profile_action_targets(manifest: dict[str, Any], profile_actions: list[Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    source_index: dict[str, dict[str, Any]] = {}
    file_index: dict[str, list[dict[str, Any]]] = {}
    for action in manifest["actions"]:
        source = action.get("source")
        if not isinstance(source, str):
            raise AvatarRuntimeError("manifest action has no source path")
        if source in source_index:
            raise AvatarRuntimeError(f"manifest repeats action source: {source}")
        source_index[source] = action
        file_index.setdefault(Path(source).name, []).append(action)

    replacement: dict[str, dict[str, Any]] = {}
    old_to_new: dict[str, str] = {}
    assigned_ids: set[str] = set()
    for rule in profile_actions:
        if not isinstance(rule, dict):
            raise AvatarRuntimeError("profile actions must be objects")
        source = rule.get("source")
        source_file = rule.get("source_file")
        action_id = rule.get("id")
        if not isinstance(action_id, str) or not ACTION_ID_PATTERN.fullmatch(action_id):
            raise AvatarRuntimeError(f"profile action id is invalid: {action_id!r}")
        if source is not None:
            if not isinstance(source, str) or not source:
                raise AvatarRuntimeError("profile action source must be a non-empty manifest path")
            original = source_index.get(source)
            if original is None:
                raise AvatarRuntimeError(f"profile action source was not found: {source}")
            if source_file is not None and (
                not isinstance(source_file, str) or Path(source).name != source_file
            ):
                raise AvatarRuntimeError("profile action source and source_file do not identify the same expression")
        else:
            if not isinstance(source_file, str) or not source_file:
                raise AvatarRuntimeError("profile action requires source or source_file")
            candidates = file_index.get(source_file, [])
            if not candidates:
                raise AvatarRuntimeError(f"profile action source was not found: {source_file}")
            if len(candidates) > 1:
                raise AvatarRuntimeError(
                    f"profile action source_file is ambiguous: {source_file}; use the exact source path"
                )
            original = candidates[0]
        if action_id in assigned_ids:
            raise AvatarRuntimeError(f"profile repeats action id: {action_id}")
        assigned_ids.add(action_id)
        updated = dict(original)
        old_id = updated["id"]
        updated["id"] = action_id
        if "label" in rule:
            label = rule["label"]
            if not isinstance(label, str) or not label.strip() or len(label) > 120:
                raise AvatarRuntimeError(f"profile label is invalid for {action_id}")
            updated["label"] = label
        if "description" in rule:
            updated["description"] = _action_description(rule["description"], action_id)
        if "exclusive_group" in rule:
            raise AvatarRuntimeError(
                f"profile action {action_id} declares an exclusive_group; "
                "this runtime uses independent active-toggle-set semantics"
            )
        # A prior profile may have written an inferred group into the
        # generated manifest. It is not source metadata and must not survive
        # an unstructured profile refresh.
        updated.pop("exclusive_group", None)
        replacement[original["source"]] = updated
        old_to_new[old_id] = action_id

    actions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for original in manifest["actions"]:
        source = original["source"]
        action = replacement.get(source, dict(original))
        action_id = action["id"]
        if action_id in seen_ids:
            raise AvatarRuntimeError(f"profile creates duplicate action id: {action_id}")
        seen_ids.add(action_id)
        actions.append(action)
    return actions, old_to_new


def _refresh_replay_metadata(model_root: Path, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give the renderer a model-local replay order without inventing a state graph.

    The VTube metadata contains individual ToggleExpression entries. Their
    declaration order is useful when combining independently selected toggles,
    but it says nothing about compatibility, dependency, or replacement.
    """

    metadata = _expression_metadata(model_root / "source", [])
    discovered_orders = [
        entry.get("replay_order")
        for entry in metadata.values()
        if isinstance(entry.get("replay_order"), int) and entry["replay_order"] >= 0
    ]
    fallback_order = max(discovered_orders, default=-1) + 1
    refreshed: list[dict[str, Any]] = []
    for action in actions:
        source = action.get("source")
        if not isinstance(source, str) or not source:
            raise AvatarRuntimeError("manifest action has no source path")
        entry = metadata.get(Path(source).name.casefold(), {})
        replay_order = entry.get("replay_order")
        if not isinstance(replay_order, int) or replay_order < 0:
            replay_order = fallback_order
            fallback_order += 1
        updated = dict(action)
        updated.pop("exclusive_group", None)
        updated["replay_order"] = replay_order
        hotkeys = entry.get("hotkeys")
        if isinstance(hotkeys, list) and all(isinstance(item, str) for item in hotkeys):
            updated["hotkeys"] = hotkeys
        refreshed.append(updated)
    return sorted(refreshed, key=lambda action: (action["replay_order"], action["source"].casefold()))


def _profile_action_descriptions(
    profile: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Read the compact top-level description map used by reusable profiles."""

    descriptions = profile.get("action_descriptions", {})
    if not isinstance(descriptions, dict):
        raise AvatarRuntimeError("profile action_descriptions must be an object")
    result: dict[str, str] = {}
    for action_id, description in descriptions.items():
        if not isinstance(action_id, str) or action_id not in index:
            raise AvatarRuntimeError(f"profile action_descriptions has unknown action id: {action_id!r}")
        result[action_id] = _action_description(description, action_id)
    return result


def _profile_action_ids(profile: dict[str, Any], field: str, index: dict[str, dict[str, Any]]) -> list[str]:
    action_ids = profile.get(field, [])
    if not isinstance(action_ids, list) or not all(isinstance(item, str) for item in action_ids):
        raise AvatarRuntimeError(f"profile {field} must be an array of action ids")
    unknown = sorted(set(action_ids) - set(index))
    if unknown:
        raise AvatarRuntimeError(f"profile {field} has unknown action ids: {', '.join(unknown)}")
    return list(dict.fromkeys(action_ids))


def _semantic_status(profile: dict[str, Any]) -> str:
    """Return profile-review state without inferring it from model files."""

    status = profile.get("semantic_status", SEMANTIC_STATUS_DRAFT)
    if not isinstance(status, str) or status not in SEMANTIC_STATUS_VALUES:
        raise AvatarRuntimeError("profile semantic_status must be draft or curated")
    return status


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _profile_source_digest(model_root: Path, source: Any) -> tuple[str, str]:
    """Hash only copied model files that define a semantic profile's target."""

    if not isinstance(source, str) or not source.startswith("source/"):
        raise AvatarRuntimeError("manifest profile target has an unsafe source path")
    relative = Path(source)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise AvatarRuntimeError("manifest profile target has an unsafe source path")
    source_root = (model_root / "source").resolve()
    candidate = model_root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise AvatarRuntimeError("manifest profile target source file is missing or unsafe")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(source_root)
    except ValueError as exc:
        raise AvatarRuntimeError("manifest profile target source escapes the managed model") from exc
    return source, _sha256_file(resolved)


def profile_target(
    registry: Path, model_id: str, *, manifest: dict[str, Any] | None = None
) -> dict[str, str]:
    """Return a portable profile fingerprint without exposing model paths or contents."""

    registry = resolve_registry(registry)
    manifest = manifest or load_manifest(registry, model_id)
    model_root = model_directory(registry, model_id)
    model = manifest.get("model")
    model_path = model.get("path") if isinstance(model, dict) else None
    model_source, model_digest = _profile_source_digest(model_root, model_path)
    actions = manifest.get("actions")
    if not isinstance(actions, list):
        raise AvatarRuntimeError("manifest profile target has invalid actions")
    action_sources: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    for action in actions:
        source = action.get("source") if isinstance(action, dict) else None
        source_path, digest = _profile_source_digest(model_root, source)
        if source_path in seen_sources:
            raise AvatarRuntimeError("manifest profile target repeats an action source")
        seen_sources.add(source_path)
        action_sources.append({"source": source_path, "sha256": digest})
    payload = {
        "model": {"source": model_source, "sha256": model_digest},
        "actions": sorted(action_sources, key=lambda entry: entry["source"].casefold()),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return {
        "schema": PROFILE_TARGET_SCHEMA,
        "fingerprint": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _validate_profile_target(profile: dict[str, Any], target: dict[str, str]) -> None:
    """Allow legacy profiles but reject a portable profile for the wrong model revision."""

    declared = profile.get("target")
    if declared is None:
        return
    if not isinstance(declared, dict):
        raise AvatarRuntimeError("profile target must be an object")
    if set(declared) != {"schema", "fingerprint"}:
        raise AvatarRuntimeError("profile target must contain only schema and fingerprint")
    fingerprint = declared.get("fingerprint")
    if declared.get("schema") != PROFILE_TARGET_SCHEMA or not isinstance(fingerprint, str):
        raise AvatarRuntimeError("profile target schema or fingerprint is invalid")
    if not PROFILE_FINGERPRINT_PATTERN.fullmatch(fingerprint):
        raise AvatarRuntimeError("profile target fingerprint is invalid")
    if fingerprint != target["fingerprint"]:
        raise AvatarRuntimeError("profile target does not match this imported model revision")


def _profile_output_path(output: Path, *, operation: str, force: bool) -> Path:
    requested_output = output.expanduser()
    if requested_output.exists():
        if requested_output.is_symlink() or not requested_output.is_file():
            raise AvatarRuntimeError(f"profile {operation} output must be a regular file path")
        if not force:
            raise AvatarRuntimeError(
                f"profile {operation} output already exists: {requested_output}; pass --force to replace it"
            )
    return requested_output.resolve()


def scaffold_profile(registry: Path, model_id: str, output: Path, *, force: bool = False) -> dict[str, Any]:
    """Write a user-owned semantic-profile draft bound to one imported model revision.

    The draft deliberately uses stable imported ids and exact manifest source
    selectors, but makes no claim about visual meaning. A user or agent can
    refine it after observing the model and then apply it normally.
    """

    registry = resolve_registry(registry)
    manifest = load_manifest(registry, model_id)
    output = _profile_output_path(output, operation="scaffold", force=force)

    actions: list[dict[str, str]] = []
    descriptions: dict[str, str] = {}
    for position, action in enumerate(manifest["actions"], start=1):
        source = action.get("source")
        action_id = action.get("id")
        if not isinstance(source, str) or not source.startswith("source/"):
            raise AvatarRuntimeError("manifest action has an unsafe source path")
        if not isinstance(action_id, str) or not ACTION_ID_PATTERN.fullmatch(action_id):
            raise AvatarRuntimeError("manifest action has an invalid generated id")
        actions.append(
            {
                "source": source,
                "id": action_id,
                "label": f"Imported expression {position}",
            }
        )
        descriptions[action_id] = "A model expression toggle whose visual effect has not been confirmed."

    document = {
        "schema": PROFILE_SCHEMA,
        "target": profile_target(registry, model_id, manifest=manifest),
        "name": f"{model_id} profile draft",
        "state_semantics": ACTIVE_TOGGLE_SET,
        "semantic_status": SEMANTIC_STATUS_DRAFT,
        "action_descriptions": descriptions,
        "actions": actions,
        "safe_default_actions": [],
        "initial_actions": [],
        "renderer": {"halo": {"enabled": True}, "activity_actions": {}},
    }
    atomic_write_json(output, document)
    return {
        "id": model_id,
        "profile_path": str(output),
        "fingerprint": document["target"]["fingerprint"],
        "action_count": len(actions),
        "semantic_status": SEMANTIC_STATUS_DRAFT,
        "status": "scaffolded",
    }


def export_profile(registry: Path, model_id: str, output: Path, *, force: bool = False) -> dict[str, Any]:
    """Export the applied profile as a user-owned, portable profile pack."""

    registry = resolve_registry(registry)
    manifest = load_manifest(registry, model_id)
    model_root = model_directory(registry, model_id)
    profile = read_json(model_root / "profile.json")
    if profile.get("schema") != PROFILE_SCHEMA:
        raise AvatarRuntimeError(f"applied profile schema must be {PROFILE_SCHEMA}")
    semantic_status = _semantic_status(profile)
    output = _profile_output_path(output, operation="export", force=force)
    exported = json.loads(json.dumps(profile, ensure_ascii=False))
    exported["target"] = profile_target(registry, model_id, manifest=manifest)
    atomic_write_json(output, exported)
    return {
        "id": model_id,
        "profile_path": str(output),
        "fingerprint": exported["target"]["fingerprint"],
        "semantic_status": semantic_status,
        "status": "exported",
    }


def _speech_mouth_settings(raw_mouth: Any) -> dict[str, float | str]:
    """Validate optional, model-local speech aperture and jaw controls."""

    if not isinstance(raw_mouth, dict):
        raise AvatarRuntimeError("profile renderer speech_motion mouth must be an object")
    unknown_mouth_fields = sorted(
        set(raw_mouth)
        - {
            "primary_parameter_id",
            "secondary_parameter_id",
            "jaw_parameter_id",
            "base_open",
            "mouth_gain",
            "secondary_gain",
            "jaw_gain",
            "attack",
            "release",
        }
    )
    if unknown_mouth_fields:
        raise AvatarRuntimeError(
            "profile renderer speech_motion mouth has unsupported field(s): "
            + ", ".join(unknown_mouth_fields)
        )
    primary_parameter_id = raw_mouth.get("primary_parameter_id")
    if not isinstance(primary_parameter_id, str) or not PARAMETER_ID_PATTERN.fullmatch(primary_parameter_id):
        raise AvatarRuntimeError("profile renderer speech_motion mouth has an invalid primary_parameter_id")
    mouth: dict[str, float | str] = {"primary_parameter_id": primary_parameter_id}
    for field in ("secondary_parameter_id", "jaw_parameter_id"):
        parameter_id = raw_mouth.get(field)
        if parameter_id is None:
            continue
        if not isinstance(parameter_id, str) or not PARAMETER_ID_PATTERN.fullmatch(parameter_id):
            raise AvatarRuntimeError(f"profile renderer speech_motion mouth has an invalid {field}")
        mouth[field] = parameter_id
    for field, default, minimum, maximum in (
        ("base_open", 0.0, 0.0, 1.0),
        ("mouth_gain", 0.6, 0.0, 1.0),
        ("secondary_gain", 0.0, 0.0, 1.0),
        ("jaw_gain", 0.0, 0.0, 1.0),
        ("attack", 0.22, 0.02, 1.0),
        ("release", 0.10, 0.01, 1.0),
    ):
        value = raw_mouth.get(field, default)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AvatarRuntimeError(f"profile renderer speech_motion mouth has an invalid {field}")
        value = float(value)
        if not minimum <= value <= maximum:
            raise AvatarRuntimeError(f"profile renderer speech_motion mouth has an out-of-range {field}")
        mouth[field] = value
    if mouth["secondary_gain"] > 0 and "secondary_parameter_id" not in mouth:
        raise AvatarRuntimeError("profile renderer speech_motion mouth secondary_gain requires secondary_parameter_id")
    if mouth["jaw_gain"] > 0 and "jaw_parameter_id" not in mouth:
        raise AvatarRuntimeError("profile renderer speech_motion mouth jaw_gain requires jaw_parameter_id")
    return mouth


def _speech_motion_settings(renderer: dict[str, Any]) -> dict[str, Any]:
    """Validate model-local rig motion that never crosses the Voice boundary."""

    raw_motion = renderer.get("speech_motion", {"targets": []})
    if not isinstance(raw_motion, dict):
        raise AvatarRuntimeError("profile renderer speech_motion must be an object")
    raw_targets = raw_motion.get("targets", [])
    if not isinstance(raw_targets, list) or len(raw_targets) > SPEECH_MOTION_TARGET_LIMIT:
        raise AvatarRuntimeError("profile renderer speech_motion targets are invalid")
    targets: list[dict[str, float | str]] = []
    for index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise AvatarRuntimeError("profile renderer speech_motion target must be an object")
        parameter_id = raw_target.get("parameter_id")
        if not isinstance(parameter_id, str) or not PARAMETER_ID_PATTERN.fullmatch(parameter_id):
            raise AvatarRuntimeError(f"profile renderer speech_motion target {index} has an invalid parameter id")
        values: dict[str, float | str] = {"parameter_id": parameter_id}
        for field, default, minimum, maximum in (
            ("idle_gain", 0.0, -4.0, 4.0),
            ("speech_gain", 0.0, -4.0, 4.0),
            ("frequency", 0.5, 0.05, 4.0),
            ("phase", 0.0, -math.tau, math.tau),
        ):
            value = raw_target.get(field, default)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise AvatarRuntimeError(f"profile renderer speech_motion target {index} has an invalid {field}")
            value = float(value)
            if not minimum <= value <= maximum:
                raise AvatarRuntimeError(f"profile renderer speech_motion target {index} has an out-of-range {field}")
            values[field] = value
        targets.append(values)
    result: dict[str, Any] = {"targets": targets}
    raw_mouth = raw_motion.get("mouth")
    if raw_mouth is not None:
        result["mouth"] = _speech_mouth_settings(raw_mouth)
    raw_eyelids = raw_motion.get("eyelids")
    if raw_eyelids is None:
        return result
    if not isinstance(raw_eyelids, dict):
        raise AvatarRuntimeError("profile renderer speech_motion eyelids must be an object")
    eyelids: dict[str, float | str] = {}
    for side in ("left", "right"):
        parameter_id = raw_eyelids.get(f"{side}_parameter_id")
        if not isinstance(parameter_id, str) or not PARAMETER_ID_PATTERN.fullmatch(parameter_id):
            raise AvatarRuntimeError(
                f"profile renderer speech_motion eyelids has an invalid {side} parameter id"
            )
        eyelids[f"{side}_parameter_id"] = parameter_id
    legacy_rest_open = raw_eyelids.get("rest_open")
    for field, fallback in (
        ("idle_open_min", legacy_rest_open),
        ("idle_open_max", legacy_rest_open),
        ("speech_open", None),
    ):
        value = raw_eyelids.get(field, fallback)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AvatarRuntimeError(f"profile renderer speech_motion eyelids has an invalid {field}")
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise AvatarRuntimeError(f"profile renderer speech_motion eyelids has an out-of-range {field}")
        eyelids[field] = value
    idle_frequency = raw_eyelids.get("idle_frequency", 0.2)
    if not isinstance(idle_frequency, (int, float)) or not math.isfinite(float(idle_frequency)):
        raise AvatarRuntimeError("profile renderer speech_motion eyelids has an invalid idle_frequency")
    idle_frequency = float(idle_frequency)
    if not 0.02 <= idle_frequency <= 2.0:
        raise AvatarRuntimeError("profile renderer speech_motion eyelids has an out-of-range idle_frequency")
    eyelids["idle_frequency"] = idle_frequency
    for field, default, minimum, maximum in (
        ("wake_gain", 1.0, 0.1, 3.0),
        ("attack", 0.18, 0.02, 1.0),
        ("release", 0.10, 0.01, 1.0),
        ("talking_wake_floor", 0.0, 0.0, 1.0),
    ):
        value = raw_eyelids.get(field, default)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise AvatarRuntimeError(f"profile renderer speech_motion eyelids has an invalid {field}")
        value = float(value)
        if not minimum <= value <= maximum:
            raise AvatarRuntimeError(f"profile renderer speech_motion eyelids has an out-of-range {field}")
        eyelids[field] = value
    if eyelids["idle_open_min"] > eyelids["idle_open_max"]:
        raise AvatarRuntimeError("profile renderer speech_motion eyelids has an invalid idle range")
    if eyelids["speech_open"] < eyelids["idle_open_max"]:
        raise AvatarRuntimeError("profile renderer speech_motion eyelids must open more during speech")
    result["eyelids"] = eyelids
    return result


def _halo_settings(renderer: dict[str, Any]) -> dict[str, bool]:
    """Validate the deliberately small local halo switch."""

    halo = renderer.get("halo", {})
    if not isinstance(halo, dict):
        raise AvatarRuntimeError("profile renderer halo must be an object")
    unknown = sorted(set(halo) - {"enabled"})
    if unknown:
        raise AvatarRuntimeError("profile renderer halo has unsupported field(s): " + ", ".join(unknown))
    enabled = halo.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AvatarRuntimeError("profile renderer halo enabled must be a boolean")
    return {"enabled": enabled}


def _fixed_controls(renderer: dict[str, Any]) -> dict[str, list[dict[str, float | str]]]:
    """Validate renderer-local controls that must be reasserted every frame."""

    parameters = renderer.get("fixed_parameters", [])
    parts = renderer.get("fixed_parts", [])
    if not isinstance(parameters, list) or len(parameters) > 32:
        raise AvatarRuntimeError("profile renderer fixed_parameters must be an array of at most 32 entries")
    if not isinstance(parts, list) or len(parts) > 32:
        raise AvatarRuntimeError("profile renderer fixed_parts must be an array of at most 32 entries")
    fixed_parameters: list[dict[str, float | str]] = []
    for item in parameters:
        if not isinstance(item, dict) or set(item) != {"parameter_id", "value"}:
            raise AvatarRuntimeError("profile renderer fixed_parameters entries must contain parameter_id and value")
        parameter_id = item.get("parameter_id")
        value = item.get("value")
        if not isinstance(parameter_id, str) or not PARAMETER_ID_PATTERN.fullmatch(parameter_id):
            raise AvatarRuntimeError("profile renderer fixed parameter id is invalid")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not -1.0 <= float(value) <= 1.0:
            raise AvatarRuntimeError(f"profile renderer fixed parameter {parameter_id} value is out of range")
        fixed_parameters.append({"parameter_id": parameter_id, "value": float(value)})
    fixed_parts: list[dict[str, float | str]] = []
    for item in parts:
        if not isinstance(item, dict) or set(item) != {"part_id", "opacity"}:
            raise AvatarRuntimeError("profile renderer fixed_parts entries must contain part_id and opacity")
        part_id = item.get("part_id")
        opacity = item.get("opacity")
        if not isinstance(part_id, str) or not PARAMETER_ID_PATTERN.fullmatch(part_id):
            raise AvatarRuntimeError("profile renderer fixed part id is invalid")
        if not isinstance(opacity, (int, float)) or not math.isfinite(float(opacity)) or not 0.0 <= float(opacity) <= 1.0:
            raise AvatarRuntimeError(f"profile renderer fixed part {part_id} opacity is out of range")
        fixed_parts.append({"part_id": part_id, "opacity": float(opacity)})
    return {"fixed_parameters": fixed_parameters, "fixed_parts": fixed_parts}


def _activity_action_settings(
    renderer: dict[str, Any], index: dict[str, dict[str, Any]]
) -> dict[str, dict[str, list[str]]]:
    """Map coarse host activity categories to temporary local action overlays."""

    activity_actions = renderer.get("activity_actions", {})
    if not isinstance(activity_actions, dict):
        raise AvatarRuntimeError("profile renderer activity_actions must be an object")
    unknown_states = sorted(set(activity_actions) - set(ACTIVITY_STATES))
    if unknown_states:
        raise AvatarRuntimeError(
            "profile renderer activity_actions has unknown state(s): " + ", ".join(unknown_states)
        )
    result: dict[str, dict[str, list[str]]] = {}
    for state in ACTIVITY_STATES:
        rule = activity_actions.get(state, [])
        if isinstance(rule, list):
            add_actions = rule
            suppressed_actions: list[Any] = []
        elif isinstance(rule, dict):
            unknown_fields = sorted(set(rule) - {"add", "suppress"})
            if unknown_fields:
                raise AvatarRuntimeError(
                    f"profile renderer activity_actions {state} has unsupported field(s): {', '.join(unknown_fields)}"
                )
            add_actions = rule.get("add", [])
            suppressed_actions = rule.get("suppress", [])
        else:
            raise AvatarRuntimeError(
                f"profile renderer activity_actions {state} must be an action array or an add/suppress object"
            )
        for field, action_ids in (("add", add_actions), ("suppress", suppressed_actions)):
            if not isinstance(action_ids, list) or not all(isinstance(item, str) for item in action_ids):
                raise AvatarRuntimeError(
                    f"profile renderer activity_actions {state} {field} must be an array of action ids"
                )
            unknown_actions = sorted(set(action_ids) - set(index))
            if unknown_actions:
                raise AvatarRuntimeError(
                    f"profile renderer activity_actions {state} {field} has unknown action ids: {', '.join(unknown_actions)}"
                )
        add = list(dict.fromkeys(add_actions))
        suppress = list(dict.fromkeys(suppressed_actions))
        overlap = sorted(set(add) & set(suppress))
        if overlap:
            raise AvatarRuntimeError(
                f"profile renderer activity_actions {state} both adds and suppresses: {', '.join(overlap)}"
            )
        result[state] = {"add": add, "suppress": suppress}
    return result


def _renderer_settings(profile: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    renderer = profile.get("renderer", {})
    if not isinstance(renderer, dict):
        raise AvatarRuntimeError("profile renderer must be an object")
    scale = renderer.get("scale", 1.0)
    bottom_inset = renderer.get("bottom_inset", 6.0)
    if not isinstance(scale, (int, float)) or not 0.1 <= float(scale) <= 4.0:
        raise AvatarRuntimeError("profile renderer scale must be between 0.1 and 4")
    if not isinstance(bottom_inset, (int, float)) or not -100.0 <= float(bottom_inset) <= 200.0:
        raise AvatarRuntimeError("profile renderer bottom_inset is out of range")
    return {
        "scale": float(scale),
        "bottom_inset": float(bottom_inset),
        "halo": _halo_settings(renderer),
        **_fixed_controls(renderer),
        "activity_actions": _activity_action_settings(renderer, index),
        "speech_motion": _speech_motion_settings(renderer),
    }


def apply_profile(registry: Path, model_id: str, profile_path: Path) -> dict[str, Any]:
    """Attach a semantic profile without editing a copied model asset."""

    profile_path = profile_path.expanduser().resolve()
    profile = read_json(profile_path)
    if profile.get("schema") != PROFILE_SCHEMA:
        raise AvatarRuntimeError(f"profile schema must be {PROFILE_SCHEMA}")
    state_semantics = profile.get("state_semantics", ACTIVE_TOGGLE_SET)
    if state_semantics != ACTIVE_TOGGLE_SET:
        raise AvatarRuntimeError("profile must use active-toggle-set state semantics")
    semantic_status = _semantic_status(profile)
    raw_actions = profile.get("actions")
    if not isinstance(raw_actions, list):
        raise AvatarRuntimeError("profile actions must be an array")
    registry = resolve_registry(registry)
    manifest = load_manifest(registry, model_id)
    _validate_profile_target(profile, profile_target(registry, model_id, manifest=manifest))
    actions, old_to_new = _profile_action_targets(manifest, raw_actions)
    model_root = model_directory(registry, model_id)
    actions = _refresh_replay_metadata(model_root, actions)
    updated_manifest = dict(manifest)
    updated_manifest["actions"] = actions
    updated_manifest["state_semantics"] = ACTIVE_TOGGLE_SET
    updated_index = action_index(updated_manifest)
    descriptions = _profile_action_descriptions(profile, updated_index)
    for action in actions:
        action_id = action["id"]
        if action_id in descriptions:
            action["description"] = descriptions[action_id]
    updated_manifest["safe_default_actions"] = _profile_action_ids(
        profile, "safe_default_actions", updated_index
    )
    updated_manifest["initial_actions"] = _profile_action_ids(profile, "initial_actions", updated_index)
    updated_manifest["renderer"] = _renderer_settings(profile, updated_index)
    profile_name = profile.get("name", profile_path.stem)
    if not isinstance(profile_name, str) or not profile_name.strip() or len(profile_name) > 120:
        raise AvatarRuntimeError("profile name must be a non-empty string")
    updated_manifest["profile"] = {
        "schema": PROFILE_SCHEMA,
        "name": profile_name,
        "semantic_status": semantic_status,
    }

    atomic_write_json(model_root / "profile.json", profile)
    atomic_write_json(manifest_path(registry, model_id), updated_manifest)

    previous = load_state(registry, model_id)
    previous_actions = previous.get("active_actions", [])
    if not isinstance(previous_actions, list) or not all(isinstance(item, str) for item in previous_actions):
        raise AvatarRuntimeError("existing state has invalid active_actions")
    remapped_actions = [old_to_new.get(action_id, action_id) for action_id in previous_actions]
    set_actions(registry, model_id, remapped_actions)
    return {
        "id": model_id,
        "profile": profile_name,
        "profile_path": str(model_root / "profile.json"),
        "semantic_status": semantic_status,
        "safe_default_actions": updated_manifest["safe_default_actions"],
        "initial_actions": updated_manifest["initial_actions"],
    }
