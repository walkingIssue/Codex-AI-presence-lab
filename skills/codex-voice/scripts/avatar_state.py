"""Write and inspect model-agnostic project and routed avatar state."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


STATE_SCHEMA = "codex-ai-presence/avatar-state/v0.1"
ROUTE_STATE_SCHEMA = "codex-ai-presence/avatar-state/v0.2"
STATE_LEDGER_SCHEMA = "codex-ai-presence/avatar-state-ledger/v0.1"
STATUS_SCHEMA = "codex-ai-presence/avatar-state-status/v0.1"
SELECTION_SCHEMA = "codex-ai-presence/avatar-selection/v0.1"
AVATAR_SCHEMA = "codex-ai-presence/avatar/v0.1"
STATE_CAPABILITY = "avatar-state-v1"
STATE_FILE = "avatar-state.json"
STATE_LEDGER_FILE = "avatar-states.json"
STATUS_FILE = "avatar-state-status.json"
STATUS_LEDGER_FILE = "avatar-state-statuses.json"
PROFILES_FILE = "presence-profiles.json"
PROFILES_SCHEMA = "codex-ai-presence/profiles/v0.1"
CAPABILITIES_FILE = "avatar-capabilities.json"
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROFILE_ID_PATTERN = MODEL_ID_PATTERN
MAX_ACTIONS = 128
MAX_STATE_BYTES = 64 * 1024
MAX_LEDGER_BYTES = 512 * 1024


class AvatarStateError(RuntimeError):
    """A user-correctable state bridge error."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _read_json(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not required:
            return None
        raise AvatarStateError(f"required JSON file does not exist: {path}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AvatarStateError(f"could not read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise AvatarStateError(f"JSON file must contain an object: {path}")
    return value


def _atomic_write_json(
    path: Path, value: dict[str, Any], *, max_bytes: int = MAX_STATE_BYTES
) -> None:
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        raise AvatarStateError(f"avatar state exceeds the {max_bytes // 1024} KiB limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _project_root(value: Path | None) -> Path:
    root = (value or Path.cwd()).expanduser().resolve()
    if not root.is_dir():
        raise AvatarStateError(f"project directory does not exist: {root}")
    runtime = root / ".codex-voice"
    if not runtime.is_dir():
        raise AvatarStateError(f"project-local voice runtime does not exist: {runtime}")
    return root


def _runtime_paths(project_root: Path) -> tuple[Path, Path, Path]:
    runtime = project_root / ".codex-voice"
    return runtime / STATE_FILE, runtime / STATE_LEDGER_FILE, runtime / STATUS_FILE


def _selected_avatar_id(project_root: Path) -> str:
    selection = _read_json(project_root / ".codex-voice" / "avatar-selection.json")
    if selection.get("schema") != SELECTION_SCHEMA:
        raise AvatarStateError("avatar selection has an unsupported schema")
    avatar_id = selection.get("avatar_id")
    if not isinstance(avatar_id, str) or not MODEL_ID_PATTERN.fullmatch(avatar_id):
        raise AvatarStateError("avatar selection has an invalid avatar id")
    return avatar_id


def _validate_avatar(project_root: Path, avatar_id: str, *, require_selected: bool) -> Path:
    if not isinstance(avatar_id, str) or not MODEL_ID_PATTERN.fullmatch(avatar_id):
        raise AvatarStateError("avatar id must use lowercase letters, digits, and hyphens")

    if require_selected and _selected_avatar_id(project_root) != avatar_id:
        raise AvatarStateError(
            f"selected avatar is {_selected_avatar_id(project_root)!r}, not {avatar_id!r}"
        )

    source_root = (project_root / ".codex-voice-avatars").resolve()
    bundle = (source_root / avatar_id).resolve()
    try:
        bundle.relative_to(source_root)
    except ValueError as exc:
        raise AvatarStateError("avatar bundle escaped the project avatar root") from exc
    manifest = _read_json(bundle / "avatar.json")
    if manifest.get("schema") != AVATAR_SCHEMA or manifest.get("id") != avatar_id:
        raise AvatarStateError("selected avatar manifest schema or id is invalid")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or STATE_CAPABILITY not in capabilities:
        raise AvatarStateError(f"avatar {avatar_id!r} does not advertise {STATE_CAPABILITY}")

    capability_path = bundle / CAPABILITIES_FILE
    capability_document = _read_json(capability_path)
    declared_id = capability_document.get("avatar_id")
    if declared_id is not None and declared_id != avatar_id:
        raise AvatarStateError(f"{CAPABILITIES_FILE} does not match avatar id {avatar_id!r}")
    return bundle


def _route_binding(
    project_root: Path,
    *,
    avatar_id: str,
    session_id: str | None,
    profile_id: str | None,
) -> tuple[str, str, str]:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id.strip()):
        raise AvatarStateError("route scope requires a valid --session-id")
    normalized_session = session_id.strip()
    profiles = _read_json(project_root / ".codex-voice" / PROFILES_FILE)
    if profiles.get("schema") != PROFILES_SCHEMA:
        raise AvatarStateError("presence profiles have an unsupported schema")
    bindings = profiles.get("sessions")
    definitions = profiles.get("profiles")
    if not isinstance(bindings, dict) or not isinstance(definitions, dict):
        raise AvatarStateError("presence profiles are missing sessions or profiles")
    binding = bindings.get(normalized_session)
    if isinstance(binding, str):
        bound_profile_id = binding
    elif isinstance(binding, dict):
        bound_profile_id = binding.get("profile_id")
    else:
        raise AvatarStateError(f"session is not bound to a presence profile: {normalized_session}")
    if not isinstance(bound_profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(bound_profile_id):
        raise AvatarStateError("session binding has an invalid profile id")
    if profile_id is not None and profile_id != bound_profile_id:
        raise AvatarStateError(
            f"session {normalized_session} is bound to profile {bound_profile_id!r}, not {profile_id!r}"
        )
    profile = definitions.get(bound_profile_id)
    if not isinstance(profile, dict):
        raise AvatarStateError(f"bound presence profile does not exist: {bound_profile_id}")
    routed_avatar_id = profile.get("avatar_id") or _selected_avatar_id(project_root)
    if routed_avatar_id != avatar_id:
        raise AvatarStateError(
            f"route uses avatar {routed_avatar_id!r}, not {avatar_id!r}"
        )
    route_key = f"session:{normalized_session}|profile:{bound_profile_id}"
    return normalized_session, bound_profile_id, route_key


def _parse_actions(raw: str) -> list[str]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AvatarStateError("--actions-json must be a JSON array of action ids") from exc
    if not isinstance(values, list):
        raise AvatarStateError("--actions-json must be a JSON array of action ids")
    if len(values) > MAX_ACTIONS:
        raise AvatarStateError(f"at most {MAX_ACTIONS} actions may be active")
    actions: list[str] = []
    for value in values:
        if not isinstance(value, str) or not ACTION_ID_PATTERN.fullmatch(value):
            raise AvatarStateError(f"invalid avatar action id: {value!r}")
        if value in actions:
            raise AvatarStateError(f"duplicate avatar action id: {value!r}")
        actions.append(value)
    return sorted(actions)


def _validate_revision(value: int) -> int:
    if value < 0:
        raise AvatarStateError("revision must be a non-negative integer")
    return value


def _validate_source(value: str) -> str:
    if not isinstance(value, str) or not SOURCE_PATTERN.fullmatch(value):
        raise AvatarStateError("source must contain only letters, digits, '.', '_', ':', or '-'")
    return value


def _existing_revision(path: Path, source: str, avatar_id: str) -> int | None:
    existing = _read_json(path, required=False)
    if not existing or existing.get("schema") != STATE_SCHEMA:
        return None
    if existing.get("avatar_id") != avatar_id or existing.get("source") != source:
        return None
    revision = existing.get("revision")
    return revision if isinstance(revision, int) and revision >= 0 else None


def _state_ledger(path: Path) -> dict[str, Any]:
    document = _read_json(path, required=False)
    if document is None:
        return {"schema": STATE_LEDGER_SCHEMA, "type": "avatar-state-ledger", "states": {}}
    if (
        document.get("schema") != STATE_LEDGER_SCHEMA
        or document.get("type") != "avatar-state-ledger"
        or not isinstance(document.get("states"), dict)
    ):
        raise AvatarStateError("routed avatar-state ledger is invalid")
    return document


def _existing_route_revision(
    ledger: dict[str, Any], route_key: str, source: str, avatar_id: str
) -> int | None:
    state = ledger["states"].get(route_key)
    if not isinstance(state, dict):
        return None
    if state.get("source") != source or state.get("avatar_id") != avatar_id:
        return None
    revision = state.get("revision")
    return revision if isinstance(revision, int) and revision >= 0 else None


def _build_state(
    *,
    project_root: Path,
    avatar_id: str,
    source: str,
    scope: str,
    revision: int,
    actions: list[str],
    session_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    state_path, ledger_path, _ = _runtime_paths(project_root)
    route: tuple[str, str, str] | None = None
    if scope == "project":
        _validate_avatar(project_root, avatar_id, require_selected=True)
        previous_revision = _existing_revision(state_path, source, avatar_id)
    elif scope == "route":
        route = _route_binding(
            project_root,
            avatar_id=avatar_id,
            session_id=session_id,
            profile_id=profile_id,
        )
        _validate_avatar(project_root, avatar_id, require_selected=False)
        previous_revision = _existing_route_revision(
            _state_ledger(ledger_path), route[2], source, avatar_id
        )
    else:
        raise AvatarStateError("scope must be project or route")
    if previous_revision is not None and revision < previous_revision:
        raise AvatarStateError(
            f"revision {revision} is older than the existing revision {previous_revision}"
        )
    state: dict[str, Any] = {
        "schema": STATE_SCHEMA if scope == "project" else ROUTE_STATE_SCHEMA,
        "type": "avatar-state",
        "avatar_id": avatar_id,
        "source": source,
        "scope": scope,
        "revision": revision,
        "actions": actions,
        "issued_at": _now(),
    }
    if route is not None:
        state.update(
            {
                "session_id": route[0],
                "profile_id": route[1],
                "route_key": route[2],
            }
        )
    return state


def write_state(args: argparse.Namespace) -> dict[str, Any]:
    project_root = _project_root(args.project_root)
    source = _validate_source(args.source)
    revision = _validate_revision(args.revision)
    actions = _parse_actions(args.actions_json)
    state = _build_state(
        project_root=project_root,
        avatar_id=args.avatar_id,
        source=source,
        scope=args.scope,
        revision=revision,
        actions=actions,
        session_id=args.session_id,
        profile_id=args.profile_id,
    )
    state_path, ledger_path, _ = _runtime_paths(project_root)
    if state["scope"] == "project":
        _atomic_write_json(state_path, state)
    else:
        ledger = _state_ledger(ledger_path)
        ledger["states"][state["route_key"]] = state
        ledger["updated_at"] = state["issued_at"]
        _atomic_write_json(ledger_path, ledger, max_bytes=MAX_LEDGER_BYTES)
    return state


def sync_state(args: argparse.Namespace) -> dict[str, Any]:
    project_root = _project_root(args.project_root)
    state_path, ledger_path, _ = _runtime_paths(project_root)
    if args.session_id:
        _, _, route_key = _route_binding(
            project_root,
            avatar_id=args.avatar_id or _selected_avatar_id(project_root),
            session_id=args.session_id,
            profile_id=args.profile_id,
        )
        ledger = _state_ledger(ledger_path)
        current = ledger["states"].get(route_key)
        if not isinstance(current, dict):
            raise AvatarStateError(f"no routed avatar state exists for {route_key}")
    else:
        current = _read_json(state_path)
    if current.get("type") != "avatar-state" or current.get("schema") not in {STATE_SCHEMA, ROUTE_STATE_SCHEMA}:
        raise AvatarStateError("existing avatar state has an unsupported schema")
    actions = current.get("actions")
    if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
        raise AvatarStateError("existing avatar state has invalid actions")
    state = _build_state(
        project_root=project_root,
        avatar_id=current.get("avatar_id", ""),
        source=current.get("source", ""),
        scope=current.get("scope", ""),
        revision=current.get("revision", -1),
        actions=_parse_actions(json.dumps(actions)),
        session_id=current.get("session_id"),
        profile_id=current.get("profile_id"),
    )
    if state["scope"] == "project":
        _atomic_write_json(state_path, state)
    else:
        ledger = _state_ledger(ledger_path)
        ledger["states"][state["route_key"]] = state
        ledger["updated_at"] = state["issued_at"]
        _atomic_write_json(ledger_path, ledger, max_bytes=MAX_LEDGER_BYTES)
    return state


def status(args: argparse.Namespace) -> dict[str, Any]:
    project_root = _project_root(args.project_root)
    state_path, ledger_path, status_path = _runtime_paths(project_root)
    ledger = _state_ledger(ledger_path)
    routed_state = None
    if args.session_id:
        states = ledger["states"]
        matches = [
            value
            for value in states.values()
            if isinstance(value, dict)
            and value.get("session_id") == args.session_id
            and (args.profile_id is None or value.get("profile_id") == args.profile_id)
        ]
        if len(matches) > 1:
            raise AvatarStateError("session matches multiple routed avatar states; specify --profile-id")
        routed_state = matches[0] if matches else None
    return {
        "project_root": str(project_root),
        "state": _read_json(state_path, required=False),
        "routed_state": routed_state,
        "routed_states": ledger,
        "status": _read_json(status_path, required=False),
        "routed_statuses": _read_json(
            project_root / ".codex-voice" / STATUS_LEDGER_FILE, required=False
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    write = commands.add_parser("write", help="atomically replace the project avatar state")
    write.add_argument("--project-root", type=Path, default=Path.cwd())
    write.add_argument("--avatar-id", required=True)
    write.add_argument("--source", default="live2d-avatar-controls")
    write.add_argument("--scope", choices=("project", "route"), default="project")
    write.add_argument("--session-id")
    write.add_argument("--profile-id")
    write.add_argument("--revision", type=int, required=True)
    write.add_argument("--actions-json", required=True)
    write.set_defaults(handler=write_state)

    sync = commands.add_parser("sync", help="rewrite the current state for Orb startup replay")
    sync.add_argument("--project-root", type=Path, default=Path.cwd())
    sync.add_argument("--session-id")
    sync.add_argument("--profile-id")
    sync.add_argument("--avatar-id")
    sync.set_defaults(handler=sync_state)

    inspect = commands.add_parser("status", help="show state and host acceptance diagnostics")
    inspect.add_argument("--project-root", type=Path, default=Path.cwd())
    inspect.add_argument("--session-id")
    inspect.add_argument("--profile-id")
    inspect.set_defaults(handler=status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except AvatarStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
