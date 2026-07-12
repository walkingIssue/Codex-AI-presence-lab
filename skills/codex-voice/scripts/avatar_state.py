"""Write and inspect the model-agnostic project-local avatar state bridge."""

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
STATUS_SCHEMA = "codex-ai-presence/avatar-state-status/v0.1"
SELECTION_SCHEMA = "codex-ai-presence/avatar-selection/v0.1"
AVATAR_SCHEMA = "codex-ai-presence/avatar/v0.1"
STATE_CAPABILITY = "avatar-state-v1"
STATE_FILE = "avatar-state.json"
STATUS_FILE = "avatar-state-status.json"
CAPABILITIES_FILE = "avatar-capabilities.json"
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
MAX_ACTIONS = 128
MAX_STATE_BYTES = 64 * 1024


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


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise AvatarStateError(f"avatar state exceeds the {MAX_STATE_BYTES // 1024} KiB limit")
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


def _runtime_paths(project_root: Path) -> tuple[Path, Path]:
    runtime = project_root / ".codex-voice"
    return runtime / STATE_FILE, runtime / STATUS_FILE


def _validate_avatar(project_root: Path, avatar_id: str) -> Path:
    if not isinstance(avatar_id, str) or not MODEL_ID_PATTERN.fullmatch(avatar_id):
        raise AvatarStateError("avatar id must use lowercase letters, digits, and hyphens")

    runtime = project_root / ".codex-voice"
    selection = _read_json(runtime / "avatar-selection.json")
    if selection.get("schema") != SELECTION_SCHEMA:
        raise AvatarStateError("avatar selection has an unsupported schema")
    if selection.get("avatar_id") != avatar_id:
        raise AvatarStateError(
            f"selected avatar is {selection.get('avatar_id')!r}, not {avatar_id!r}"
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


def _build_state(
    *, project_root: Path, avatar_id: str, source: str, scope: str, revision: int, actions: list[str]
) -> dict[str, Any]:
    if scope != "project":
        raise AvatarStateError("avatar-state/v0.1 supports only scope=project")
    _validate_avatar(project_root, avatar_id)
    state_path, _ = _runtime_paths(project_root)
    previous_revision = _existing_revision(state_path, source, avatar_id)
    if previous_revision is not None and revision < previous_revision:
        raise AvatarStateError(
            f"revision {revision} is older than the existing revision {previous_revision}"
        )
    return {
        "schema": STATE_SCHEMA,
        "type": "avatar-state",
        "avatar_id": avatar_id,
        "source": source,
        "scope": scope,
        "revision": revision,
        "actions": actions,
        "issued_at": _now(),
    }


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
    )
    state_path, _ = _runtime_paths(project_root)
    _atomic_write_json(state_path, state)
    return state


def sync_state(args: argparse.Namespace) -> dict[str, Any]:
    project_root = _project_root(args.project_root)
    state_path, _ = _runtime_paths(project_root)
    current = _read_json(state_path)
    if current.get("schema") != STATE_SCHEMA or current.get("type") != "avatar-state":
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
    )
    _atomic_write_json(state_path, state)
    return state


def status(args: argparse.Namespace) -> dict[str, Any]:
    project_root = _project_root(args.project_root)
    state_path, status_path = _runtime_paths(project_root)
    return {
        "project_root": str(project_root),
        "state": _read_json(state_path, required=False),
        "status": _read_json(status_path, required=False),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    write = commands.add_parser("write", help="atomically replace the project avatar state")
    write.add_argument("--project-root", type=Path, default=Path.cwd())
    write.add_argument("--avatar-id", required=True)
    write.add_argument("--source", default="live2d-avatar-controls")
    write.add_argument("--scope", choices=("project",), default="project")
    write.add_argument("--revision", type=int, required=True)
    write.add_argument("--actions-json", required=True)
    write.set_defaults(handler=write_state)

    sync = commands.add_parser("sync", help="rewrite the current state for Orb startup replay")
    sync.add_argument("--project-root", type=Path, default=Path.cwd())
    sync.set_defaults(handler=sync_state)

    inspect = commands.add_parser("status", help="show state and host acceptance diagnostics")
    inspect.add_argument("--project-root", type=Path, default=Path.cwd())
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
