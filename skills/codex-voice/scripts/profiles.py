"""Resolve project and session presence profiles without owning playback."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from configuration import load_settings
from session_scope import is_project_mode, load_state, registered_session_ids


SCHEMA = "codex-ai-presence/profiles/v0.1"
FILE_NAME = "presence-profiles.json"
DEFAULT_PROFILE_ID = "default"
BUILTIN_AVATAR_ID = "builtin"
PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
AVATAR_ID_PATTERN = PROFILE_ID_PATTERN
VOICE_PATTERN = re.compile(r"^[a-z]{2}_[a-z0-9_]+$")
MODES = {"stream", "quality"}
ACTIVITY_STATES = {"idle", "thinking", "tool", "skill", "cli", "waiting", "error"}
ACTION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MAX_CURATION_ACTIONS = 128


class ProfileError(RuntimeError):
    """A user-correctable profile document or binding error."""


@dataclass(frozen=True)
class ResolvedProfile:
    profile_id: str
    avatar_id: str
    tts_voice: str
    tts_speed: float
    tts_mode: str
    route_key: str

    def routing_fields(self) -> dict[str, object]:
        return asdict(self)


def profile_path(voice_root: Path) -> Path:
    return voice_root / FILE_NAME


def default_document() -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "project_profile_id": DEFAULT_PROFILE_ID,
        "profiles": {DEFAULT_PROFILE_ID: {}},
        "sessions": {},
    }


def _profile_id(value: object, *, field: str = "profile id") -> str:
    if not isinstance(value, str) or not PROFILE_ID_PATTERN.fullmatch(value.strip()):
        raise ProfileError(f"{field} must be lowercase letters, digits, and hyphens")
    return value.strip()


def _curation_actions(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_CURATION_ACTIONS:
        raise ProfileError(f"{field} must be an array of at most {MAX_CURATION_ACTIONS} action ids")
    normalized: list[str] = []
    for action_id in value:
        if not isinstance(action_id, str) or not ACTION_ID_PATTERN.fullmatch(action_id):
            raise ProfileError(f"{field} contains an invalid action id")
        if action_id in normalized:
            raise ProfileError(f"{field} contains duplicate action id {action_id}")
        normalized.append(action_id)
    return normalized


def _validate_curation(profile_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"profile {profile_id} curation must be an object")
    unknown = sorted(set(value) - {"initial_actions", "activity_actions"})
    if unknown:
        raise ProfileError(f"profile {profile_id} curation has unsupported fields: {', '.join(unknown)}")
    normalized: dict[str, object] = {}
    if "initial_actions" in value:
        normalized["initial_actions"] = _curation_actions(
            value["initial_actions"], field=f"profile {profile_id} curation.initial_actions"
        )
    if "activity_actions" in value:
        raw_activity = value["activity_actions"]
        if not isinstance(raw_activity, dict):
            raise ProfileError(f"profile {profile_id} curation.activity_actions must be an object")
        unknown_states = sorted(set(raw_activity) - ACTIVITY_STATES)
        if unknown_states:
            raise ProfileError(
                f"profile {profile_id} curation has unsupported activity states: {', '.join(unknown_states)}"
            )
        activity: dict[str, dict[str, list[str]]] = {}
        for state, raw_rule in raw_activity.items():
            if not isinstance(raw_rule, dict):
                raise ProfileError(f"profile {profile_id} curation activity {state} must be an object")
            unknown_rule_fields = sorted(set(raw_rule) - {"add", "suppress"})
            if unknown_rule_fields:
                raise ProfileError(
                    f"profile {profile_id} curation activity {state} has unsupported fields: "
                    + ", ".join(unknown_rule_fields)
                )
            rule: dict[str, list[str]] = {}
            for field in ("add", "suppress"):
                if field in raw_rule:
                    rule[field] = _curation_actions(
                        raw_rule[field],
                        field=f"profile {profile_id} curation.activity_actions.{state}.{field}",
                    )
            activity[state] = rule
        normalized["activity_actions"] = activity
    return normalized


def _validate_profile(profile_id: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProfileError(f"profile {profile_id} must be an object")
    allowed = {"avatar_id", "voice", "speed", "mode", "curation"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ProfileError(f"profile {profile_id} has unsupported fields: {', '.join(unknown)}")
    normalized: dict[str, object] = {}
    avatar_id = value.get("avatar_id")
    if avatar_id is not None:
        if not isinstance(avatar_id, str) or not AVATAR_ID_PATTERN.fullmatch(avatar_id.strip()):
            raise ProfileError(f"profile {profile_id} has an invalid avatar_id")
        normalized["avatar_id"] = avatar_id.strip()
    voice = value.get("voice")
    if voice is not None:
        if not isinstance(voice, str) or not VOICE_PATTERN.fullmatch(voice.strip().lower()):
            raise ProfileError(f"profile {profile_id} has an invalid Kokoro voice id")
        normalized["voice"] = voice.strip().lower()
    speed = value.get("speed")
    if speed is not None:
        try:
            parsed_speed = float(speed)
        except (TypeError, ValueError) as exc:
            raise ProfileError(f"profile {profile_id} speed must be numeric") from exc
        if not 0.5 <= parsed_speed <= 2.0:
            raise ProfileError(f"profile {profile_id} speed must be between 0.5 and 2.0")
        normalized["speed"] = parsed_speed
    mode = value.get("mode")
    if mode is not None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in MODES:
            raise ProfileError(f"profile {profile_id} mode must be stream or quality")
        normalized["mode"] = normalized_mode
    if "curation" in value:
        normalized["curation"] = _validate_curation(profile_id, value["curation"])
    return normalized


def normalize_document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ProfileError(f"profile document schema must be {SCHEMA}")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ProfileError("profile document requires at least one profile")
    profiles: dict[str, dict[str, object]] = {}
    for raw_id, raw_profile in raw_profiles.items():
        profile_id = _profile_id(raw_id)
        profiles[profile_id] = _validate_profile(profile_id, raw_profile)

    project_profile_id = _profile_id(
        value.get("project_profile_id", DEFAULT_PROFILE_ID),
        field="project_profile_id",
    )
    if project_profile_id not in profiles:
        raise ProfileError(f"project profile does not exist: {project_profile_id}")

    raw_sessions = value.get("sessions", {})
    if not isinstance(raw_sessions, dict):
        raise ProfileError("sessions must be an object")
    sessions: dict[str, dict[str, str]] = {}
    for raw_session_id, raw_binding in raw_sessions.items():
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            raise ProfileError("session ids must be non-empty strings")
        if isinstance(raw_binding, str):
            bound_profile_id = _profile_id(raw_binding, field="session profile id")
        elif isinstance(raw_binding, dict):
            bound_profile_id = _profile_id(raw_binding.get("profile_id"), field="session profile id")
        else:
            raise ProfileError(f"session binding {raw_session_id} must be a string or object")
        if bound_profile_id not in profiles:
            raise ProfileError(f"session {raw_session_id} references missing profile {bound_profile_id}")
        sessions[raw_session_id.strip()] = {"profile_id": bound_profile_id}

    return {
        "schema": SCHEMA,
        "project_profile_id": project_profile_id,
        "profiles": profiles,
        "sessions": sessions,
    }


def read_document(voice_root: Path, *, strict: bool = False) -> dict[str, object]:
    path = profile_path(voice_root)
    if not path.is_file():
        return default_document()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return normalize_document(value)
    except (OSError, json.JSONDecodeError, ProfileError):
        if strict:
            raise
        return default_document()


def write_document(voice_root: Path, value: dict[str, object]) -> dict[str, object]:
    normalized = normalize_document(value)
    voice_root.mkdir(parents=True, exist_ok=True)
    destination = profile_path(voice_root)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return normalized


def require_project_session(project_root: Path, voice_root: Path, session_id: str) -> None:
    """Reject accidental bindings into a different project runtime."""
    state = load_state(voice_root)
    if is_project_mode(state):
        return
    if session_id not in registered_session_ids(state):
        raise ProfileError(
            f"session {session_id} is not enabled in {project_root}; run session-on "
            "from that Codex session before binding its profile"
        )
    sessions = state.get("sessions")
    details = sessions.get(session_id) if isinstance(sessions, dict) else None
    registered_project = details.get("project_root") if isinstance(details, dict) else None
    if isinstance(registered_project, str) and Path(registered_project).expanduser().resolve() != project_root:
        raise ProfileError(
            f"session {session_id} belongs to {Path(registered_project).expanduser().resolve()}, "
            f"not {project_root}"
        )


class ProfileRegistry:
    """Resolve immutable routing data; playback remains owned by the arbiter."""

    def __init__(self, project_root: Path, voice_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.voice_root = voice_root.resolve()

    def resolve(
        self,
        session_id: object = None,
        *,
        requested_profile_id: object = None,
    ) -> ResolvedProfile:
        document = read_document(self.voice_root)
        profiles = document["profiles"]
        assert isinstance(profiles, dict)
        sessions = document["sessions"]
        assert isinstance(sessions, dict)
        selected = None
        if isinstance(requested_profile_id, str) and requested_profile_id in profiles:
            selected = requested_profile_id
        elif isinstance(session_id, str) and session_id in sessions:
            binding = sessions[session_id]
            if isinstance(binding, dict):
                selected = binding.get("profile_id")
        if not isinstance(selected, str) or selected not in profiles:
            selected = str(document["project_profile_id"])
        profile = profiles.get(selected, {})
        profile = profile if isinstance(profile, dict) else {}
        settings = load_settings(self.voice_root)
        avatar_id = profile.get("avatar_id")
        if not isinstance(avatar_id, str):
            avatar_id = self._legacy_avatar_id()
        voice = profile.get("voice") or settings["voice"]
        speed = profile.get("speed") if profile.get("speed") is not None else settings["speed"]
        mode = profile.get("mode") or settings["mode"]
        session_key = session_id.strip() if isinstance(session_id, str) and session_id.strip() else "unscoped"
        return ResolvedProfile(
            profile_id=selected,
            avatar_id=str(avatar_id),
            tts_voice=str(voice),
            tts_speed=float(speed),
            tts_mode=str(mode),
            route_key=f"session:{session_key}|profile:{selected}",
        )

    def _legacy_avatar_id(self) -> str:
        path = self.voice_root / "avatar-selection.json"
        try:
            selection = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return BUILTIN_AVATAR_ID
        avatar_id = selection.get("avatar_id") if isinstance(selection, dict) else None
        if isinstance(avatar_id, str) and AVATAR_ID_PATTERN.fullmatch(avatar_id):
            return avatar_id
        return BUILTIN_AVATAR_ID


def command_set(args: argparse.Namespace) -> int:
    document = read_document(args.voice_root, strict=True)
    profiles = document["profiles"]
    assert isinstance(profiles, dict)
    profile_id = _profile_id(args.profile_id)
    current = dict(profiles.get(profile_id, {}))
    for key in ("avatar_id", "voice", "speed", "mode"):
        value = getattr(args, key)
        if value is not None:
            current[key] = value
    if args.clear_curation:
        current.pop("curation", None)
    elif args.curation_json is not None:
        try:
            current["curation"] = json.loads(args.curation_json)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"curation JSON is invalid: {exc.msg}") from exc
    profiles[profile_id] = _validate_profile(profile_id, current)
    write_document(args.voice_root, document)
    print(f"Saved profile {profile_id}")
    return 0


def command_bind(args: argparse.Namespace) -> int:
    document = read_document(args.voice_root, strict=True)
    profile_id = _profile_id(args.profile_id)
    profiles = document["profiles"]
    assert isinstance(profiles, dict)
    if profile_id not in profiles:
        raise ProfileError(f"profile does not exist: {profile_id}")
    session_id = str(args.session_id).strip()
    if not session_id:
        raise ProfileError("session id must be non-empty")
    require_project_session(args.project_root, args.voice_root, session_id)
    sessions = document["sessions"]
    assert isinstance(sessions, dict)
    sessions[session_id] = {"profile_id": profile_id}
    write_document(args.voice_root, document)
    print(f"Bound session {session_id} to profile {profile_id}; restart the Orb to add its window.")
    return 0


def command_unbind(args: argparse.Namespace) -> int:
    document = read_document(args.voice_root, strict=True)
    sessions = document["sessions"]
    assert isinstance(sessions, dict)
    sessions.pop(args.session_id, None)
    write_document(args.voice_root, document)
    print(f"Removed profile binding for session {args.session_id}")
    return 0


def command_default(args: argparse.Namespace) -> int:
    document = read_document(args.voice_root, strict=True)
    profile_id = _profile_id(args.profile_id)
    profiles = document["profiles"]
    assert isinstance(profiles, dict)
    if profile_id not in profiles:
        raise ProfileError(f"profile does not exist: {profile_id}")
    document["project_profile_id"] = profile_id
    write_document(args.voice_root, document)
    print(f"Selected project profile {profile_id}")
    return 0


def command_list(args: argparse.Namespace) -> int:
    document = read_document(args.voice_root, strict=True)
    print(json.dumps(document, indent=2))
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    resolved = ProfileRegistry(args.project_root, args.voice_root).resolve(
        args.session_id,
        requested_profile_id=args.profile_id,
    )
    print(json.dumps(resolved.routing_fields(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    set_profile = commands.add_parser("set")
    set_profile.add_argument("profile_id")
    set_profile.add_argument("--avatar-id")
    set_profile.add_argument("--voice")
    set_profile.add_argument("--speed", type=float)
    set_profile.add_argument("--mode", choices=tuple(sorted(MODES)))
    curation = set_profile.add_mutually_exclusive_group()
    curation.add_argument(
        "--curation-json",
        help="semantic initial/activity overrides as a JSON object; empty arrays explicitly clear parent fields",
    )
    curation.add_argument("--clear-curation", action="store_true")
    set_profile.set_defaults(handler=command_set)
    bind = commands.add_parser("bind")
    bind.add_argument("session_id")
    bind.add_argument("profile_id")
    bind.set_defaults(handler=command_bind)
    unbind = commands.add_parser("unbind")
    unbind.add_argument("session_id")
    unbind.set_defaults(handler=command_unbind)
    default = commands.add_parser("default")
    default.add_argument("profile_id")
    default.set_defaults(handler=command_default)
    listing = commands.add_parser("list")
    listing.set_defaults(handler=command_list)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--session-id")
    resolve.add_argument("--profile-id")
    resolve.set_defaults(handler=command_resolve)
    return parser


def main() -> int:
    from presence_compat import delegate

    delegated = delegate("profiles", sys.argv[1:])
    if delegated is not None:
        return delegated
    parser = build_parser()
    args = parser.parse_args()
    args.project_root = args.project_root.expanduser().resolve()
    args.voice_root = args.project_root / ".codex-voice"
    try:
        return args.handler(args)
    except (OSError, ProfileError) as exc:
        print(f"Profile error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
