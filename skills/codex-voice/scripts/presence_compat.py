"""One-release v0.1 command translations into the v0.2 Presence API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def _runtime_python() -> Path:
    root = _codex_home() / "presence" / ".venv"
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def installed() -> bool:
    return (
        os.environ.get("CODEX_PRESENCE_COMPAT_DISABLE") != "1"
        and (_codex_home() / "presence" / "installation.json").is_file()
        and _runtime_python().is_file()
    )


def _presence(
    arguments: list[str], *, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [str(_runtime_python()), "-m", "presence_runtime.cli", *arguments]
    return subprocess.run(
        command,
        text=True,
        capture_output=capture,
        check=False,
    )


def _json(arguments: list[str]) -> Any:
    result = _presence(["--compact", *arguments], capture=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or "Presence Runtime command failed")
    return json.loads(result.stdout)


def _project(argv: list[str]) -> tuple[Path, list[str]]:
    remaining = list(argv)
    for option in ("--project-root", "--project"):
        if option in remaining:
            index = remaining.index(option)
            if index + 1 >= len(remaining):
                raise RuntimeError(f"{option} requires a path")
            root = Path(remaining[index + 1]).expanduser().resolve()
            del remaining[index : index + 2]
            return root, remaining
    return Path.cwd().resolve(), remaining


def _session() -> str | None:
    for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_CONVERSATION_ID"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _warn(tool: str) -> None:
    print(
        f"warning: {tool} is a v0.1 compatibility wrapper; use the intent-level `presence` CLI",
        file=sys.stderr,
    )


def _run(arguments: list[str]) -> int:
    return _presence(arguments).returncode


def _require_session(project: Path) -> tuple[str, list[str]]:
    session = _session()
    if session is None:
        raise RuntimeError(
            "legacy routed avatar mutation is ambiguous without CODEX_THREAD_ID; "
            "use presence ... --project/--session explicitly"
        )
    return session, ["--project", str(project), "--session", session]


def _semantic_patch(project: Path, avatar: str, actions: list[str]) -> int:
    session, scope = _require_session(project)
    model = _json(["catalog", "avatar", "show", avatar])
    definitions = model.get("actions", {})
    slots = model.get("semantic_slots", {})
    if not isinstance(definitions, dict) or not isinstance(slots, dict):
        raise RuntimeError(f"avatar {avatar!r} has no semantic slot contract")
    selected: dict[str, list[str]] = {}
    for action in actions:
        definition = definitions.get(action)
        claimed = definition.get("slots") if isinstance(definition, dict) else None
        if not isinstance(claimed, list) or not claimed:
            raise RuntimeError(f"avatar {avatar!r} has no action {action!r}")
        selected.setdefault(str(claimed[0]), []).append(action)
    patch = {
        "semantic": {
            "clear_slots": list(slots),
            "slots": selected,
        }
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as handle:
        json.dump(patch, handle)
        temporary = Path(handle.name)
    try:
        return _run(["session", "set", *scope, "--patch", str(temporary)])
    finally:
        temporary.unlink(missing_ok=True)


def _current_actions(project: Path) -> list[str]:
    _session_id, scope = _require_session(project)
    effective = _json(["inspect", "effective", *scope])
    semantic = effective.get("semantic", {})
    actions = semantic.get("persistent_actions", []) if isinstance(semantic, dict) else []
    return [str(item) for item in actions]


def _toggle(argv: list[str], project: Path) -> int:
    operation = argv[0] if argv else "status"
    if operation in {"status", "provider-status", "orb-status"}:
        return _run(["runtime", "doctor"])
    if operation == "runtime-restart":
        return _run(["runtime", "restart"])
    providers = {
        "provider-cpu": "cpu",
        "provider-cuda": "cuda",
        "provider-directml": "directml",
        "provider-openvino": "openvino",
    }
    if operation in providers:
        return _run(["runtime", "set-policy", "--provider", providers[operation]])
    if operation in {"project-on", "all-on", "session-on"}:
        return _run(["project", "register", str(project)])
    if operation in {"off", "project-off", "all-off", "session-off"}:
        return _run(["project", "unregister", "--project", str(project), "--all-sources"])
    session, scope = _require_session(project)
    del session
    if operation in {"stream", "quality"}:
        return _run(["session", "set", *scope, "--playback-mode", operation])
    if operation in {"progress-on", "progress-off"}:
        return _run(
            ["session", "set", *scope, "--progress-visible", "on" if operation.endswith("on") else "off"]
        )
    if operation in {"orb-on", "orb-off"}:
        return _run(
            ["session", "set", *scope, "--renderer-visible", "on" if operation.endswith("on") else "off"]
        )
    raise RuntimeError(
        f"legacy toggle operation {operation!r} has no unambiguous v0.2 translation"
    )


def _profiles(argv: list[str], project: Path) -> int:
    if not argv:
        raise RuntimeError("legacy profiles wrapper requires a command")
    command = argv[0]
    if command == "list":
        return _run(["catalog", "profile", "list"])
    if command == "resolve":
        session = None
        if "--session-id" in argv:
            index = argv.index("--session-id")
            session = argv[index + 1]
        arguments = ["inspect", "effective", "--project", str(project)]
        if session:
            arguments.extend(["--session", session])
        return _run(arguments)
    if len(argv) < 2:
        raise RuntimeError(f"legacy profiles {command} requires an identifier")
    if command == "default":
        return _run(["project", "set-profile", "--project", str(project), argv[1]])
    if command == "bind" and len(argv) >= 3:
        return _run(
            [
                "session",
                "set-profile",
                "--project",
                str(project),
                "--session",
                argv[1],
                argv[2],
            ]
        )
    if command == "unbind":
        return _run(
            [
                "session",
                "clear",
                "--project",
                str(project),
                "--session",
                argv[1],
                "profile_ref",
            ]
        )
    raise RuntimeError(
        "legacy profile document mutation is no longer authoritative; import or revise a "
        "presence/profile/v0.2 document through `presence catalog profile import`"
    )


def _avatar(argv: list[str], project: Path) -> int:
    if not argv:
        raise RuntimeError("legacy avatar wrapper requires a command")
    command = argv[0]
    if command == "list":
        return _run(["catalog", "avatar", "list"])
    if command == "use" and len(argv) >= 2:
        return _run(["avatar", "use", argv[1], "--project", str(project)])
    if command == "remove" and len(argv) >= 2:
        return _run(["catalog", "avatar", "remove", argv[1]])
    raise RuntimeError(
        "v0.1 avatar bundles cannot become routing authority; import a validated v0.2 model pack "
        "with `presence catalog avatar import PACK --assets MODEL_DIR`"
    )


def _live2d(argv: list[str], project: Path) -> int:
    if len(argv) >= 2 and argv[0] == "state":
        command = argv[1]
        if len(argv) < 3:
            raise RuntimeError("legacy state command requires an avatar id")
        avatar = argv[2]
        requested = argv[3:]
        if command == "set":
            actions = requested
        elif command == "enable":
            actions = list(dict.fromkeys([*_current_actions(project), *requested]))
        elif command == "disable":
            removed = set(requested)
            actions = [item for item in _current_actions(project) if item not in removed]
        elif command == "show":
            _session_id, scope = _require_session(project)
            return _run(["inspect", "effective", *scope])
        else:
            raise RuntimeError(f"unsupported legacy state operation: {command}")
        return _semantic_patch(project, avatar, actions)
    if len(argv) >= 2 and argv[0] == "project":
        command = argv[1]
        if command == "publish":
            _session_id, scope = _require_session(project)
            return _run(["inspect", "effective", *scope])
        if command in {"status", "doctor", "voice-status", "context", "sync"}:
            return _run(["runtime", "doctor"])
        if command == "uninstall":
            return _run(["project", "unregister", "--project", str(project), "--all-sources"])
    raise RuntimeError(
        "legacy Live2D install/materialize operations are retired; use presence catalog/avatar/preset commands"
    )


def _avatar_state(argv: list[str], project: Path) -> int:
    if not argv:
        raise RuntimeError("legacy avatar_state wrapper requires a command")
    if argv[0] == "status":
        _session_id, scope = _require_session(project)
        return _run(["inspect", "effective", *scope])
    if argv[0] == "sync":
        _session_id, scope = _require_session(project)
        return _run(["inspect", "effective", *scope])
    if argv[0] == "write":
        try:
            avatar = argv[argv.index("--avatar-id") + 1]
            actions = json.loads(argv[argv.index("--actions-json") + 1])
        except (ValueError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy avatar_state write arguments are invalid") from exc
        if not isinstance(actions, list) or any(not isinstance(item, str) for item in actions):
            raise RuntimeError("legacy avatar_state actions must be a string list")
        return _semantic_patch(project, avatar, actions)
    raise RuntimeError(f"unsupported legacy avatar_state operation: {argv[0]}")


def delegate(tool: str, argv: list[str]) -> int | None:
    if not installed():
        return None
    _warn(tool)
    try:
        project, remaining = _project(argv)
        if tool == "toggle":
            return _toggle(remaining, project)
        if tool == "profiles":
            return _profiles(remaining, project)
        if tool == "avatar":
            return _avatar(remaining, project)
        if tool == "live2d-avatar":
            return _live2d(remaining, project)
        if tool == "avatar-state":
            return _avatar_state(remaining, project)
        raise RuntimeError(f"unknown compatibility wrapper: {tool}")
    except (OSError, RuntimeError) as exc:
        print(f"presence compatibility error: {exc}", file=sys.stderr)
        return 2
