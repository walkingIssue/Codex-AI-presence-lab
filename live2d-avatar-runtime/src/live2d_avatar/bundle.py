"""Project-local Live2D renderer bundle materialization and voice-state publishing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import AvatarRuntimeError
from .lifecycle import INSTALLATION_FILE, PROJECT_MANIFEST_FILE, _project_manifest_text, read_project_installation
from .manifest import ACTIVE_TOGGLE_SET, action_index, load_manifest
from .paths import (
    atomic_write_json,
    atomic_write_text,
    model_directory,
    project_runtime_directory,
    read_json,
    remove_owned_tree,
)
from .state import show_state


BUNDLE_SCHEMA = "live2d-avatar/bundle-ownership/v0.1"
CAPABILITY_SCHEMA = "live2d-avatar/capabilities/v0.1"
REVISION_SCHEMA = "live2d-avatar/project-revisions/v0.1"
VOICE_STATE_SOURCE = "live2d-avatar-controls"
VOICE_PROFILES_SCHEMA = "codex-ai-presence/profiles/v0.1"
VOICE_SELECTION_SCHEMA = "codex-ai-presence/avatar-selection/v0.1"


def _template_root() -> Path:
    root = Path(__file__).resolve().parent / "assets" / "renderer-template"
    if not (root / "index.html").is_file() or not (root / "vendor" / "pixi.min.js").is_file():
        raise AvatarRuntimeError(f"renderer template is incomplete: {root}")
    return root


def _capabilities(manifest: dict[str, Any]) -> dict[str, Any]:
    model_path = manifest["model"]["path"]
    if not isinstance(model_path, str) or not model_path.startswith("source/"):
        raise AvatarRuntimeError("manifest has an unsafe model path")
    index = action_index(manifest)
    safe_action_ids = manifest.get("safe_default_actions", [])
    initial_actions = manifest.get("initial_actions", [])
    if not isinstance(safe_action_ids, list) or not all(isinstance(item, str) for item in safe_action_ids):
        raise AvatarRuntimeError("manifest safe_default_actions must be an array of ids")
    if not isinstance(initial_actions, list) or not all(isinstance(item, str) for item in initial_actions):
        raise AvatarRuntimeError("manifest initial_actions must be an array of ids")
    unknown = sorted((set(safe_action_ids) | set(initial_actions)) - set(index))
    if unknown:
        raise AvatarRuntimeError("manifest profile references unknown action ids: " + ", ".join(unknown))
    safe_operations: list[dict[str, Any]] = []
    for action_id in safe_action_ids:
        operations = index[action_id].get("parameter_operations", [])
        if not isinstance(operations, list):
            raise AvatarRuntimeError(f"invalid parameter operations for safe default: {action_id}")
        safe_operations.extend(operations)
    renderer = manifest.get("renderer", {"scale": 1.0, "bottom_inset": 6.0})
    if not isinstance(renderer, dict):
        raise AvatarRuntimeError("manifest renderer settings must be an object")
    state_semantics = manifest.get("state_semantics", ACTIVE_TOGGLE_SET)
    if state_semantics != ACTIVE_TOGGLE_SET:
        raise AvatarRuntimeError("manifest has unsupported avatar state semantics")
    return {
        "schema": CAPABILITY_SCHEMA,
        "avatar_id": manifest["id"],
        "state_semantics": state_semantics,
        "model": {"path": "model/" + model_path.removeprefix("source/")},
        "actions": manifest["actions"],
        "safe_default_operations": safe_operations,
        "initial_actions": initial_actions,
        "renderer": renderer,
    }


def _capability_script(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f"window.__LIVE2D_AVATAR_CAPABILITIES__ = {encoded};\n"


def _bundle_manifest_text(project: Path, model_id: str, bundle: Path) -> str:
    return f"""# Managed Live2D Avatar Bundle

| Field | Value |
| --- | --- |
| Owner | `live2d-avatar-runtime` |
| Schema | `{BUNDLE_SCHEMA}` |
| Project | `{project}` |
| Model | `{model_id}` |
| Bundle | `{bundle}` |

## Contents

- `avatar.json`: generic Codex Voice avatar manifest with `avatar-state-v1`.
- `avatar-capabilities.json`: renderer-local action and compiled-operation metadata.
- `avatar-capabilities.js`: generated renderer configuration; not a voice protocol payload.
- `model/`: copied assets from the managed global model registry.
- `vendor/`: local Pixi/Cubism renderer dependencies and their notices.

## Cleanup

`live2d-avatar project uninstall --project "{project}" --yes` removes this bundle only after verifying `bundle-ownership.json`.
"""


def _run(command: list[str], description: str) -> dict[str, Any]:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
        raise AvatarRuntimeError(f"{description} failed: {detail}")
    output = completed.stdout.strip()
    try:
        return json.loads(output) if output else {}
    except json.JSONDecodeError:
        return {"output": output}


def _voice_avatar_tool(project: Path) -> Path:
    path = project / ".codex-voice" / "avatar.py"
    if not path.is_file():
        raise AvatarRuntimeError(f"Codex Voice avatar tool is not installed: {path}")
    return path


def _voice_state_tool(project: Path) -> Path:
    path = project / ".codex-voice" / "avatar_state.py"
    if not path.is_file():
        raise AvatarRuntimeError(f"Codex Voice avatar-state writer is not installed: {path}")
    return path


def materialize_bundle(project: Path, model_id: str, registry: Path, *, replace: bool = False) -> dict[str, Any]:
    """Generate a project avatar bundle and select it through the voice-owned tool."""

    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    if installation.get("model_id") != model_id:
        raise AvatarRuntimeError("project binding model does not match materialize request; use project install --replace")
    manifest = load_manifest(registry, model_id)
    registry_model = model_directory(registry, model_id)
    source = registry_model / "source"
    if not source.is_dir():
        raise AvatarRuntimeError("managed model source directory does not exist")
    runtime = project_runtime_directory(project)
    staging_root = runtime / "bundles"
    staged_bundle = staging_root / model_id
    if staged_bundle.exists():
        remove_owned_tree(staged_bundle, runtime)
    staging_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(_template_root(), staged_bundle)
    shutil.copytree(source, staged_bundle / "model")
    capabilities = _capabilities(manifest)
    avatar_manifest = {
        "schema": "codex-ai-presence/avatar/v0.1",
        "id": model_id,
        "name": f"Live2D {model_id}",
        "version": "1.0.0",
        "entry": "index.html",
        "capabilities": ["activity", "audio", "move-mode", "avatar-state-v1"],
    }
    atomic_write_json(staged_bundle / "avatar.json", avatar_manifest)
    atomic_write_json(staged_bundle / "avatar-capabilities.json", capabilities)
    atomic_write_text(staged_bundle / "avatar-capabilities.js", _capability_script(capabilities))
    atomic_write_json(
        staged_bundle / "bundle-ownership.json",
        {
            "schema": BUNDLE_SCHEMA,
            "owner": "live2d-avatar-runtime",
            "project": str(project),
            "model_id": model_id,
            "owned_paths": [
                "avatar.json",
                "avatar-capabilities.json",
                "avatar-capabilities.js",
                "bundle-ownership.json",
                "BUNDLE-MANIFEST.md",
                "index.html",
                "renderer.js",
                "styles.css",
                "vendor/",
                "model/",
            ],
        },
    )
    atomic_write_text(staged_bundle / "BUNDLE-MANIFEST.md", _bundle_manifest_text(project, model_id, staged_bundle))

    installed_bundle = project / ".codex-voice-avatars" / model_id
    if installed_bundle.exists() and not replace:
        raise AvatarRuntimeError(
            f"avatar bundle already exists: {installed_bundle}; pass --replace to migrate or refresh it"
        )
    install_command = [
        sys.executable,
        str(_voice_avatar_tool(project)),
        "--project-root",
        str(project),
        "install",
        "--source",
        str(staged_bundle),
    ]
    if replace:
        install_command.append("--replace")
    install_command.append("--use")
    _run(
        install_command,
        "Codex Voice avatar install",
    )
    if not (installed_bundle / "avatar-capabilities.json").is_file():
        raise AvatarRuntimeError("Codex Voice did not materialize the avatar capability file")
    installation["owned_external_paths"] = [str(installed_bundle)]
    installation["owned_paths"] = sorted(
        set(installation.get("owned_paths", [])) | {"bundles/", "avatar-state-revisions.json"}
    )
    atomic_write_json(runtime / INSTALLATION_FILE, installation)
    atomic_write_text(
        runtime / PROJECT_MANIFEST_FILE,
        _project_manifest_text(
            project,
            model_id,
            registry,
            str(installation.get("created_at", "unknown")),
            installation,
        ),
    )
    return {
        "project": str(project),
        "model_id": model_id,
        "staged_bundle": str(staged_bundle),
        "installed_bundle": str(installed_bundle),
        "status": "materialized",
    }


def _revision_ledger(runtime: Path) -> tuple[Path, dict[str, Any]]:
    path = runtime / "avatar-state-revisions.json"
    if not path.exists():
        return path, {"schema": REVISION_SCHEMA, "entries": {}}
    document = read_json(path)
    if document.get("schema") != REVISION_SCHEMA or not isinstance(document.get("entries"), dict):
        raise AvatarRuntimeError("project avatar-state revision ledger is invalid")
    return path, document


def _enabled_presence_sessions(project: Path) -> set[str] | None:
    path = project / ".codex-voice" / "sessions.json"
    if not path.is_file():
        return None
    document = read_json(path)
    if document.get("version") != 1 or document.get("mode") != "session":
        return None
    sessions = document.get("sessions")
    if not isinstance(sessions, dict):
        return set()
    enabled: set[str] = set()
    for session_id, details in sessions.items():
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        if isinstance(details, dict) and details.get("enabled") is False:
            continue
        registered_project = details.get("project_root") if isinstance(details, dict) else None
        if isinstance(registered_project, str) and Path(registered_project).expanduser().resolve() != project:
            continue
        enabled.add(session_id.strip())
    return enabled


def _presence_routes(project: Path, avatar_id: str) -> list[tuple[str, str]]:
    voice = project / ".codex-voice"
    profiles_path = voice / "presence-profiles.json"
    if not profiles_path.is_file():
        return []
    document = read_json(profiles_path)
    if document.get("schema") != VOICE_PROFILES_SCHEMA:
        raise AvatarRuntimeError("Codex Voice presence profiles have an unsupported schema")
    profiles = document.get("profiles")
    sessions = document.get("sessions")
    if not isinstance(profiles, dict) or not isinstance(sessions, dict):
        raise AvatarRuntimeError("Codex Voice presence profiles are invalid")
    selected_avatar_id = None
    selection_path = voice / "avatar-selection.json"
    if selection_path.is_file():
        selection = read_json(selection_path)
        if selection.get("schema") == VOICE_SELECTION_SCHEMA and isinstance(selection.get("avatar_id"), str):
            selected_avatar_id = selection["avatar_id"]
    enabled_sessions = _enabled_presence_sessions(project)
    routes: list[tuple[str, str]] = []
    for session_id, binding in sessions.items():
        if not isinstance(session_id, str) or not session_id:
            continue
        if enabled_sessions is not None and session_id not in enabled_sessions:
            continue
        profile_id = binding if isinstance(binding, str) else binding.get("profile_id") if isinstance(binding, dict) else None
        profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
        if not isinstance(profile_id, str) or not isinstance(profile, dict):
            continue
        routed_avatar_id = profile.get("avatar_id") or selected_avatar_id
        if routed_avatar_id == avatar_id:
            routes.append((session_id, profile_id))
    return sorted(routes)


def _publish_route(
    project: Path,
    model_id: str,
    *,
    session_id: str | None,
    profile_id: str | None,
    project_wide: bool,
) -> tuple[str, str] | None:
    if project_wide:
        if session_id is not None or profile_id is not None:
            raise AvatarRuntimeError("--project-wide cannot be combined with a session or profile target")
        return None
    routes = _presence_routes(project, model_id)
    requested_session = session_id or os.environ.get("CODEX_THREAD_ID")
    if requested_session:
        matches = [route for route in routes if route[0] == requested_session]
        if profile_id is not None:
            matches = [route for route in matches if route[1] == profile_id]
        if matches:
            return matches[0]
        if session_id is not None:
            raise AvatarRuntimeError(f"session is not bound to the Live2D avatar: {session_id}")
    if profile_id is not None:
        matches = [route for route in routes if route[1] == profile_id]
        if len(matches) == 1:
            return matches[0]
        raise AvatarRuntimeError("profile target is missing or ambiguous; specify --session-id")
    if len(routes) == 1:
        return routes[0]
    if len(routes) > 1:
        raise AvatarRuntimeError(
            "this avatar is bound to multiple sessions; publish with --session-id so wardrobe state stays isolated"
        )
    return None


def publish_state(
    project: Path,
    registry: Path,
    *,
    source: str = VOICE_STATE_SOURCE,
    session_id: str | None = None,
    profile_id: str | None = None,
    project_wide: bool = False,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    model_id = installation.get("model_id")
    if not isinstance(model_id, str):
        raise AvatarRuntimeError("project installation has no model id")
    state = show_state(registry, model_id)
    actions = state.get("active_actions")
    if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
        raise AvatarRuntimeError("model state has invalid active actions")
    runtime = project_runtime_directory(project)
    ledger_path, ledger = _revision_ledger(runtime)
    route = _publish_route(
        project,
        model_id,
        session_id=session_id,
        profile_id=profile_id,
        project_wide=project_wide,
    )
    route_key = f"session:{route[0]}|profile:{route[1]}" if route else "project"
    key = f"{source}:{model_id}:{route_key}"
    prior = ledger["entries"].get(key, {})
    if not route and not prior:
        # Migrate the v0.1 project-wide revision key without resetting its
        # monotonic sequence against an existing Voice snapshot.
        prior = ledger["entries"].get(f"{source}:{model_id}", {})
    prior_revision = prior.get("revision", 0) if isinstance(prior, dict) else 0
    if not isinstance(prior_revision, int) or prior_revision < 0:
        raise AvatarRuntimeError("project avatar-state revision ledger is invalid")
    revision = prior_revision + 1
    writer_command = [
            sys.executable,
            str(_voice_state_tool(project)),
            "write",
            "--project-root",
            str(project),
            "--avatar-id",
            model_id,
            "--source",
            source,
            "--scope",
            "route" if route else "project",
            "--revision",
            str(revision),
            "--actions-json",
            json.dumps(actions, ensure_ascii=True),
        ]
    if route:
        writer_command.extend(["--session-id", route[0], "--profile-id", route[1]])
    writer_result = _run(
        writer_command,
        "Codex Voice avatar-state write",
    )
    ledger["entries"][key] = {"revision": revision, "model_state_revision": state.get("revision", 0)}
    atomic_write_json(ledger_path, ledger)
    return {
        "project": str(project),
        "model_id": model_id,
        "revision": revision,
        "actions": actions,
        "scope": "route" if route else "project",
        "session_id": route[0] if route else None,
        "profile_id": route[1] if route else None,
        "route_key": route_key if route else None,
        "writer": writer_result,
        "status": "published",
    }


def sync_voice_state(
    project: Path,
    *,
    model_id: str | None = None,
    session_id: str | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    installation = read_project_installation(project)
    if model_id is None and isinstance(installation.get("model_id"), str):
        model_id = installation["model_id"]
    command = [sys.executable, str(_voice_state_tool(project)), "sync", "--project-root", str(project)]
    if session_id:
        command.extend(["--session-id", session_id])
        if profile_id:
            command.extend(["--profile-id", profile_id])
        if model_id:
            command.extend(["--avatar-id", model_id])
    return _run(
        command,
        "Codex Voice avatar-state sync",
    )


def voice_state_status(
    project: Path, *, session_id: str | None = None, profile_id: str | None = None
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    read_project_installation(project)
    command = [sys.executable, str(_voice_state_tool(project)), "status", "--project-root", str(project)]
    if session_id:
        command.extend(["--session-id", session_id])
    if profile_id:
        command.extend(["--profile-id", profile_id])
    return _run(
        command,
        "Codex Voice avatar-state status",
    )
