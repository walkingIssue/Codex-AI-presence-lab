"""Strict v0.2 document validation without partial coercion."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from .errors import ValidationError


IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SLOT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTIVITY_STATES = {"idle", "thinking", "tool", "skill", "cli", "waiting", "error"}
CONFIG_FIELDS = {
    "voice_id",
    "speed",
    "playback_mode",
    "volume",
    "commentary_ratio",
    "avatar_ref",
    "preset_ref",
    "progress_visible",
    "renderer_visible",
    "semantic",
}
PATCH_FIELDS = CONFIG_FIELDS | {"profile_ref"}
PROFILE_METADATA = {"schema", "profile_id", "revision"}
FORBIDDEN_POLICY_FIELDS = {
    "provider",
    "microphone_permission",
    "orb_port",
    "route_key",
    "binding_id",
    "avatar_id",
    "profile_id_requested",
}


def _object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("must be an object", path=path)
    return value


def _string(value: Any, path: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("must be a non-empty string", path=path)
    if identifier and not IDENTIFIER.fullmatch(value):
        raise ValidationError("contains unsupported characters", path=path)
    return value


def _unique_strings(value: Any, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ValidationError("must be a list", path=path)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item = _string(item, f"{path}[{index}]")
        if item in seen:
            raise ValidationError(f"contains duplicate {item!r}", path=path)
        seen.add(item)
        result.append(item)
    return result


def validate_semantic(value: Any, *, path: str = "semantic") -> dict[str, Any]:
    document = _object(value, path)
    unknown = set(document) - {"slots", "clear_slots", "activity"}
    if unknown:
        raise ValidationError(f"unknown fields: {sorted(unknown)}", path=path)
    result: dict[str, Any] = {}
    if "slots" in document:
        slots = _object(document["slots"], f"{path}.slots")
        normalized_slots: dict[str, list[str]] = {}
        for slot, actions in slots.items():
            if not isinstance(slot, str) or not SLOT_IDENTIFIER.fullmatch(slot):
                raise ValidationError("invalid semantic slot id", path=f"{path}.slots.{slot}")
            normalized_slots[slot] = _unique_strings(actions, f"{path}.slots.{slot}")
        result["slots"] = normalized_slots
    if "clear_slots" in document:
        clear_slots = _unique_strings(document["clear_slots"], f"{path}.clear_slots")
        for slot in clear_slots:
            if not SLOT_IDENTIFIER.fullmatch(slot):
                raise ValidationError("invalid semantic slot id", path=f"{path}.clear_slots")
        result["clear_slots"] = clear_slots
    if "activity" in document:
        activities = _object(document["activity"], f"{path}.activity")
        normalized_activity: dict[str, Any] = {}
        for state, rule in activities.items():
            if state not in ACTIVITY_STATES:
                raise ValidationError(
                    f"unsupported activity state {state!r}",
                    path=f"{path}.activity",
                )
            if rule is None:
                normalized_activity[state] = None
                continue
            rule_document = _object(rule, f"{path}.activity.{state}")
            unknown_rule = set(rule_document) - {"add", "clear_slots"}
            if unknown_rule:
                raise ValidationError(
                    f"unknown fields: {sorted(unknown_rule)}",
                    path=f"{path}.activity.{state}",
                )
            normalized_rule: dict[str, list[str]] = {}
            if "add" in rule_document:
                normalized_rule["add"] = _unique_strings(
                    rule_document["add"],
                    f"{path}.activity.{state}.add",
                )
            if "clear_slots" in rule_document:
                normalized_rule["clear_slots"] = _unique_strings(
                    rule_document["clear_slots"],
                    f"{path}.activity.{state}.clear_slots",
                )
            normalized_activity[state] = normalized_rule
        result["activity"] = normalized_activity
    return result


def _validate_config_field(name: str, value: Any, path: str) -> Any:
    if name in {"voice_id", "avatar_ref"}:
        return _string(value, path)
    if name in {"preset_ref", "profile_ref"}:
        if value is None:
            return None
        return _string(value, path)
    if name == "speed":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("must be a number", path=path)
        value = float(value)
        if not 0.25 <= value <= 4.0:
            raise ValidationError("must be between 0.25 and 4.0", path=path)
        return value
    if name == "playback_mode":
        if value not in {"stream", "quality"}:
            raise ValidationError("must be stream or quality", path=path)
        return value
    if name == "volume":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValidationError("must be an integer from 0 to 100", path=path)
        return value
    if name == "commentary_ratio":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError("must be a number", path=path)
        value = float(value)
        if not 0 <= value <= 1:
            raise ValidationError("must be between 0 and 1", path=path)
        return value
    if name in {"progress_visible", "renderer_visible"}:
        if not isinstance(value, bool):
            raise ValidationError("must be a boolean", path=path)
        return value
    if name == "semantic":
        return validate_semantic(value, path=path)
    raise ValidationError(f"unsupported field {name!r}", path=path)


def validate_patch(value: Any, *, path: str = "patch") -> dict[str, Any]:
    document = _object(value, path)
    forbidden = set(document) & FORBIDDEN_POLICY_FIELDS
    if forbidden:
        raise ValidationError(
            f"runtime policy/routing fields are not allowed: {sorted(forbidden)}",
            path=path,
        )
    unknown = set(document) - PATCH_FIELDS
    if unknown:
        raise ValidationError(f"unknown fields: {sorted(unknown)}", path=path)
    return {
        name: _validate_config_field(name, copy.deepcopy(value), f"{path}.{name}")
        for name, value in document.items()
    }


def validate_profile(value: Any) -> dict[str, Any]:
    document = _object(value, "profile")
    unknown = set(document) - (PROFILE_METADATA | CONFIG_FIELDS)
    if unknown:
        raise ValidationError(f"unknown fields: {sorted(unknown)}", path="profile")
    if document.get("schema") != "presence/profile/v0.2":
        raise ValidationError("unsupported schema", path="profile.schema")
    profile_id = _string(document.get("profile_id"), "profile.profile_id", identifier=True)
    revision = document.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValidationError("must be a positive integer", path="profile.revision")
    result = {
        "schema": "presence/profile/v0.2",
        "profile_id": profile_id,
        "revision": revision,
    }
    for name in CONFIG_FIELDS:
        if name in document:
            result[name] = _validate_config_field(
                name,
                copy.deepcopy(document[name]),
                f"profile.{name}",
            )
    return result


def validate_preset(value: Any) -> dict[str, Any]:
    document = _object(value, "preset")
    required = {"schema", "preset_id", "revision", "compatible_model_fingerprints", "semantic"}
    unknown = set(document) - required
    missing = required - set(document)
    if unknown or missing:
        raise ValidationError(
            f"missing={sorted(missing)}, unknown={sorted(unknown)}",
            path="preset",
        )
    if document["schema"] != "presence/preset/v0.2":
        raise ValidationError("unsupported schema", path="preset.schema")
    preset_id = _string(document["preset_id"], "preset.preset_id", identifier=True)
    revision = document["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValidationError("must be a positive integer", path="preset.revision")
    fingerprints = _unique_strings(
        document["compatible_model_fingerprints"],
        "preset.compatible_model_fingerprints",
    )
    if not fingerprints or any(not FINGERPRINT.fullmatch(item) for item in fingerprints):
        raise ValidationError(
            "must contain at least one sha256 fingerprint",
            path="preset.compatible_model_fingerprints",
        )
    return {
        "schema": "presence/preset/v0.2",
        "preset_id": preset_id,
        "revision": revision,
        "compatible_model_fingerprints": fingerprints,
        "semantic": validate_semantic(document["semantic"], path="preset.semantic"),
    }


def validate_model_pack(value: Any) -> dict[str, Any]:
    document = _object(value, "avatar")
    required = {
        "schema",
        "avatar_id",
        "version",
        "model_fingerprint",
        "renderer",
        "semantic_slots",
        "actions",
        "safe_defaults",
        "capabilities",
    }
    missing = required - set(document)
    unknown = set(document) - required
    if missing or unknown:
        raise ValidationError(
            f"missing={sorted(missing)}, unknown={sorted(unknown)}",
            path="avatar",
        )
    if document["schema"] != "presence/avatar-model-pack/v0.2":
        raise ValidationError("unsupported schema", path="avatar.schema")
    avatar_id = _string(document["avatar_id"], "avatar.avatar_id", identifier=True)
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("must be a positive integer", path="avatar.version")
    fingerprint = _string(document["model_fingerprint"], "avatar.model_fingerprint")
    if not FINGERPRINT.fullmatch(fingerprint):
        raise ValidationError("must be a sha256 fingerprint", path="avatar.model_fingerprint")
    renderer = _object(document["renderer"], "avatar.renderer")
    if set(renderer) - {"kind", "entrypoint", "minimum_runtime"}:
        raise ValidationError("contains unknown fields", path="avatar.renderer")
    if renderer.get("kind") not in {"builtin", "live2d"}:
        raise ValidationError("kind must be builtin or live2d", path="avatar.renderer.kind")
    _string(renderer.get("entrypoint"), "avatar.renderer.entrypoint")
    slots = _object(document["semantic_slots"], "avatar.semantic_slots")
    normalized_slots: dict[str, dict[str, Any]] = {}
    for slot, definition in slots.items():
        if not isinstance(slot, str) or not SLOT_IDENTIFIER.fullmatch(slot):
            raise ValidationError("invalid semantic slot id", path=f"avatar.semantic_slots.{slot}")
        definition = _object(definition, f"avatar.semantic_slots.{slot}")
        if set(definition) - {"exclusive", "description"} or "exclusive" not in definition:
            raise ValidationError(
                "requires exclusive and no unknown fields",
                path=f"avatar.semantic_slots.{slot}",
            )
        if not isinstance(definition["exclusive"], bool):
            raise ValidationError(
                "must be a boolean",
                path=f"avatar.semantic_slots.{slot}.exclusive",
            )
        normalized_slots[slot] = copy.deepcopy(dict(definition))
    actions = _object(document["actions"], "avatar.actions")
    normalized_actions: dict[str, dict[str, Any]] = {}
    for action, definition in actions.items():
        if not isinstance(action, str) or not SLOT_IDENTIFIER.fullmatch(action):
            raise ValidationError("invalid action id", path=f"avatar.actions.{action}")
        definition = _object(definition, f"avatar.actions.{action}")
        if set(definition) - {"slots", "label", "description", "operations"}:
            raise ValidationError("contains unknown fields", path=f"avatar.actions.{action}")
        action_slots = _unique_strings(definition.get("slots"), f"avatar.actions.{action}.slots")
        if not action_slots or any(slot not in normalized_slots for slot in action_slots):
            raise ValidationError(
                "references an unknown or empty slot set",
                path=f"avatar.actions.{action}.slots",
            )
        normalized_actions[action] = copy.deepcopy(dict(definition))
        normalized_actions[action]["slots"] = action_slots
    safe_defaults = validate_semantic(document["safe_defaults"], path="avatar.safe_defaults")
    _validate_semantic_references(
        safe_defaults,
        normalized_slots,
        normalized_actions,
        "avatar.safe_defaults",
    )
    capabilities = _unique_strings(document["capabilities"], "avatar.capabilities")
    return {
        "schema": "presence/avatar-model-pack/v0.2",
        "avatar_id": avatar_id,
        "version": version,
        "model_fingerprint": fingerprint,
        "renderer": copy.deepcopy(dict(renderer)),
        "semantic_slots": normalized_slots,
        "actions": normalized_actions,
        "safe_defaults": safe_defaults,
        "capabilities": capabilities,
    }


def _validate_semantic_references(
    semantic: Mapping[str, Any],
    slots: Mapping[str, Any],
    actions: Mapping[str, Any],
    path: str,
) -> None:
    for slot in semantic.get("clear_slots", ()):
        if slot not in slots:
            raise ValidationError("references an unknown slot", path=f"{path}.clear_slots")
    for slot, selected in semantic.get("slots", {}).items():
        if slot not in slots:
            raise ValidationError("references an unknown slot", path=f"{path}.slots.{slot}")
        for action in selected:
            if action not in actions:
                raise ValidationError("references an unknown action", path=f"{path}.slots.{slot}")
            if slot not in actions[action]["slots"]:
                raise ValidationError(
                    f"action {action!r} does not claim slot {slot!r}",
                    path=f"{path}.slots.{slot}",
                )
    for state, rule in semantic.get("activity", {}).items():
        if rule is None:
            continue
        for slot in rule.get("clear_slots", ()):
            if slot not in slots:
                raise ValidationError(
                    "references an unknown slot",
                    path=f"{path}.activity.{state}.clear_slots",
                )
        for action in rule.get("add", ()):
            if action not in actions:
                raise ValidationError(
                    "references an unknown action",
                    path=f"{path}.activity.{state}.add",
                )


def validate_semantic_references(
    semantic: Mapping[str, Any],
    model_pack: Mapping[str, Any],
    path: str,
) -> None:
    _validate_semantic_references(
        semantic,
        model_pack["semantic_slots"],
        model_pack["actions"],
        path,
    )
