"""Command-line interface for the Live2D avatar runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .errors import AvatarRuntimeError
from .bundle import materialize_bundle, publish_state, sync_voice_state, voice_state_status
from .context import build_project_context, render_context_json, render_context_markdown
from .hook_registration import context_hook_status, disable_context_hook, enable_context_hook
from .importer import import_model
from .lifecycle import bind_project, install_project, project_doctor, project_status, remove_model, uninstall_project
from .manifest import list_models, load_manifest
from .paths import resolve_registry
from .profile import apply_profile, export_profile, scaffold_profile
from .state import disable_actions, enable_actions, set_actions, show_state


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        # Windows PowerShell may still expose a legacy cp1252 stdout. Escaping
        # non-ASCII keeps manifests with vendor-provided labels printable while
        # retaining valid, lossless JSON for callers.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=True)
            print(f"{key}: {value}")
        return
    if isinstance(payload, list):
        if not payload:
            print("No entries.")
            return
        for item in payload:
            if isinstance(item, dict):
                print("\t".join(str(item.get(key, "")) for key in ("id", "status", "label", "hotkeys")))
            else:
                print(item)
        return
    print(payload)


def _registry(args: argparse.Namespace) -> Path:
    return resolve_registry(args.registry)


def _command_model_import(args: argparse.Namespace) -> dict[str, Any]:
    return import_model(args.source, args.model_id, _registry(args), replace=args.replace)


def _command_model_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    return list_models(_registry(args))


def _command_model_inspect(args: argparse.Namespace) -> dict[str, Any]:
    return load_manifest(_registry(args), args.model_id)


def _command_model_remove(args: argparse.Namespace) -> dict[str, Any]:
    return remove_model(_registry(args), args.model_id, confirm=args.yes)


def _command_model_profile_apply(args: argparse.Namespace) -> dict[str, Any]:
    return apply_profile(_registry(args), args.model_id, args.file)


def _command_model_profile_scaffold(args: argparse.Namespace) -> dict[str, Any]:
    return scaffold_profile(_registry(args), args.model_id, args.output, force=args.force)


def _command_model_profile_export(args: argparse.Namespace) -> dict[str, Any]:
    return export_profile(_registry(args), args.model_id, args.output, force=args.force)


def _command_actions_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest = load_manifest(_registry(args), args.model_id)
    return manifest["actions"]


def _command_state_show(args: argparse.Namespace) -> dict[str, Any]:
    return show_state(_registry(args), args.model_id)


def _command_state_set(args: argparse.Namespace) -> dict[str, Any]:
    return set_actions(_registry(args), args.model_id, args.actions)


def _command_state_enable(args: argparse.Namespace) -> dict[str, Any]:
    return enable_actions(_registry(args), args.model_id, args.actions)


def _command_state_disable(args: argparse.Namespace) -> dict[str, Any]:
    return disable_actions(_registry(args), args.model_id, args.actions)


def _command_project_install(args: argparse.Namespace) -> dict[str, Any]:
    return install_project(args.project, args.model_id, _registry(args), replace=args.replace)


def _command_project_status(args: argparse.Namespace) -> dict[str, Any]:
    return project_status(args.project)


def _command_project_doctor(args: argparse.Namespace) -> dict[str, Any]:
    return project_doctor(args.project)


def _command_project_bind(args: argparse.Namespace) -> dict[str, Any]:
    applied_profile = (
        apply_profile(_registry(args), args.model_id, args.profile) if args.profile is not None else None
    )
    result = bind_project(
        args.project,
        args.model_id,
        _registry(args),
        replace=args.replace or applied_profile is not None,
    )
    if applied_profile is not None:
        result["applied_profile"] = {
            "name": applied_profile["profile"],
            "semantic_status": applied_profile["semantic_status"],
        }
    return result


def _command_project_uninstall(args: argparse.Namespace) -> dict[str, Any]:
    return uninstall_project(args.project, confirm=args.yes)


def _command_project_materialize(args: argparse.Namespace) -> dict[str, Any]:
    return materialize_bundle(args.project, args.model_id, _registry(args), replace=args.replace)


def _command_project_publish(args: argparse.Namespace) -> dict[str, Any]:
    return publish_state(
        args.project,
        _registry(args),
        session_id=args.session_id,
        profile_id=args.profile_id,
        project_wide=args.project_wide,
    )


def _command_project_sync(args: argparse.Namespace) -> dict[str, Any]:
    return sync_voice_state(
        args.project,
        session_id=args.session_id,
        profile_id=args.profile_id,
    )


def _command_project_voice_status(args: argparse.Namespace) -> dict[str, Any]:
    return voice_state_status(
        args.project,
        session_id=args.session_id,
        profile_id=args.profile_id,
    )


def _command_project_context(args: argparse.Namespace) -> str:
    context = build_project_context(
        args.project,
        session_id=args.session_id,
        profile_id=args.profile_id,
    )
    return render_context_json(context) if args.format == "json" else render_context_markdown(context)


def _command_project_context_hook_enable(args: argparse.Namespace) -> dict[str, Any]:
    return enable_context_hook(args.project)


def _command_project_context_hook_disable(args: argparse.Namespace) -> dict[str, Any]:
    return disable_context_hook(args.project)


def _command_project_context_hook_status(args: argparse.Namespace) -> dict[str, Any]:
    return context_hook_status(args.project)


def _set_handler(parser: argparse.ArgumentParser, handler: Callable[[argparse.Namespace], Any]) -> None:
    parser.set_defaults(handler=handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="model registry root (default: CODEX_LIVE2D_REGISTRY or ~/.codex/live2d-models)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    model = commands.add_parser("model", help="import, inspect, list, or remove models")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_import = model_commands.add_parser("import", help="copy a Cubism ZIP or folder into the local registry")
    model_import.add_argument("source", type=Path)
    model_import.add_argument("--id", dest="model_id", required=True)
    model_import.add_argument("--replace", action="store_true", help="replace an existing managed model id")
    _add_json_option(model_import)
    _set_handler(model_import, _command_model_import)

    model_list = model_commands.add_parser("list", help="list registry models")
    _add_json_option(model_list)
    _set_handler(model_list, _command_model_list)

    model_inspect = model_commands.add_parser("inspect", help="print a generated model manifest")
    model_inspect.add_argument("model_id")
    _add_json_option(model_inspect)
    _set_handler(model_inspect, _command_model_inspect)

    model_remove = model_commands.add_parser("remove", help="remove one managed model and its copied assets")
    model_remove.add_argument("model_id")
    model_remove.add_argument("--yes", action="store_true", help="confirm permanent removal")
    _add_json_option(model_remove)
    _set_handler(model_remove, _command_model_remove)

    model_profile = model_commands.add_parser("profile", help="scaffold, apply, or export a semantic action profile")
    model_profile_commands = model_profile.add_subparsers(dest="model_profile_command", required=True)
    model_profile_apply = model_profile_commands.add_parser("apply", help="copy and apply a profile to one managed model")
    model_profile_apply.add_argument("model_id")
    model_profile_apply.add_argument("--file", type=Path, required=True)
    _add_json_option(model_profile_apply)
    _set_handler(model_profile_apply, _command_model_profile_apply)
    model_profile_scaffold = model_profile_commands.add_parser(
        "scaffold", help="write a user-owned semantic-profile draft without inferring visual meaning"
    )
    model_profile_scaffold.add_argument("model_id")
    model_profile_scaffold.add_argument("--output", type=Path, required=True)
    model_profile_scaffold.add_argument("--force", action="store_true", help="replace an existing user-owned draft")
    _add_json_option(model_profile_scaffold)
    _set_handler(model_profile_scaffold, _command_model_profile_scaffold)
    model_profile_export = model_profile_commands.add_parser(
        "export", help="write the applied profile as a user-owned portable profile pack"
    )
    model_profile_export.add_argument("model_id")
    model_profile_export.add_argument("--output", type=Path, required=True)
    model_profile_export.add_argument("--force", action="store_true", help="replace an existing user-owned profile pack")
    _add_json_option(model_profile_export)
    _set_handler(model_profile_export, _command_model_profile_export)

    actions = commands.add_parser("actions", help="inspect model actions")
    actions_commands = actions.add_subparsers(dest="actions_command", required=True)
    actions_list = actions_commands.add_parser("list", help="list imported expression actions")
    actions_list.add_argument("model_id")
    _add_json_option(actions_list)
    _set_handler(actions_list, _command_actions_list)

    state = commands.add_parser("state", help="show or change resolved avatar state")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    state_show = state_commands.add_parser("show", help="show current resolved state")
    state_show.add_argument("model_id")
    _add_json_option(state_show)
    _set_handler(state_show, _command_state_show)
    for name, handler, help_text in (
        ("set", _command_state_set, "replace all active actions; omit actions to reset"),
        ("enable", _command_state_enable, "add independent action ids to the active toggle set"),
        ("disable", _command_state_disable, "disable action ids"),
    ):
        action_parser = state_commands.add_parser(name, help=help_text)
        action_parser.add_argument("model_id")
        action_parser.add_argument("actions", nargs="*")
        _add_json_option(action_parser)
        _set_handler(action_parser, handler)

    project = commands.add_parser("project", help="bind or clean up a project-local runtime boundary")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_install = project_commands.add_parser("install", help="create a project-local ownership marker")
    project_install.add_argument("--project", type=Path, required=True)
    project_install.add_argument("--model", dest="model_id", required=True)
    project_install.add_argument("--replace", action="store_true", help="replace an existing managed project binding")
    _add_json_option(project_install)
    _set_handler(project_install, _command_project_install)
    project_show = project_commands.add_parser("status", help="show project-local lifecycle status")
    project_show.add_argument("--project", type=Path, required=True)
    _add_json_option(project_show)
    _set_handler(project_show, _command_project_status)
    project_doctor = project_commands.add_parser(
        "doctor", help="read-only readiness check for model, binding, Voice selection, and state"
    )
    project_doctor.add_argument("--project", type=Path, required=True)
    _add_json_option(project_doctor)
    _set_handler(project_doctor, _command_project_doctor)
    project_bind = project_commands.add_parser(
        "bind", help="install and materialize one managed, state-capable avatar binding"
    )
    project_bind.add_argument("--project", type=Path, required=True)
    project_bind.add_argument("--model", dest="model_id", required=True)
    project_bind.add_argument(
        "--profile",
        type=Path,
        help="apply one verified user-owned profile before materializing the binding",
    )
    project_bind.add_argument("--replace", action="store_true", help="refresh an existing binding for the same model")
    _add_json_option(project_bind)
    _set_handler(project_bind, _command_project_bind)
    project_uninstall = project_commands.add_parser("uninstall", help="remove only the managed project-local boundary")
    project_uninstall.add_argument("--project", type=Path, required=True)
    project_uninstall.add_argument("--yes", action="store_true", help="confirm removal")
    _add_json_option(project_uninstall)
    _set_handler(project_uninstall, _command_project_uninstall)
    project_materialize = project_commands.add_parser("materialize", help="generate and select a state-capable project avatar bundle")
    project_materialize.add_argument("--project", type=Path, required=True)
    project_materialize.add_argument("--model", dest="model_id", required=True)
    project_materialize.add_argument("--replace", action="store_true", help="replace an existing project avatar bundle")
    _add_json_option(project_materialize)
    _set_handler(project_materialize, _command_project_materialize)
    project_publish = project_commands.add_parser("publish", help="publish the complete model state through Codex Voice")
    project_publish.add_argument("--project", type=Path, required=True)
    project_publish.add_argument("--session-id", help="target one bound session/profile avatar window")
    project_publish.add_argument("--profile-id", help="assert the target session's presence profile")
    project_publish.add_argument(
        "--project-wide",
        action="store_true",
        help="use the legacy project-wide broadcast state instead of routed state",
    )
    _add_json_option(project_publish)
    _set_handler(project_publish, _command_project_publish)
    project_sync = project_commands.add_parser("sync", help="request Codex Voice to replay its accepted avatar state")
    project_sync.add_argument("--project", type=Path, required=True)
    project_sync.add_argument("--session-id")
    project_sync.add_argument("--profile-id")
    _add_json_option(project_sync)
    _set_handler(project_sync, _command_project_sync)
    project_voice_status = project_commands.add_parser("voice-status", help="show Codex Voice avatar-state diagnostics")
    project_voice_status.add_argument("--project", type=Path, required=True)
    project_voice_status.add_argument("--session-id")
    project_voice_status.add_argument("--profile-id")
    _add_json_option(project_voice_status)
    _set_handler(project_voice_status, _command_project_voice_status)
    project_context = project_commands.add_parser(
        "context", help="show the selected avatar's safe semantic state and available actions"
    )
    project_context.add_argument("--project", type=Path, required=True)
    project_context.add_argument("--format", choices=("markdown", "json"), default="markdown")
    project_context.add_argument("--session-id")
    project_context.add_argument("--profile-id")
    _set_handler(project_context, _command_project_context)
    project_context_hook = project_commands.add_parser(
        "context-hook", help="manage the project-owned Codex UserPromptSubmit avatar context hook"
    )
    project_context_hook_commands = project_context_hook.add_subparsers(
        dest="project_context_hook_command", required=True
    )
    for name, handler, help_text in (
        ("enable", _command_project_context_hook_enable, "merge the Live2D context hook into Codex hooks"),
        ("disable", _command_project_context_hook_disable, "remove only the Live2D context hook"),
        ("status", _command_project_context_hook_status, "show context-hook registration status"),
    ):
        context_hook_command = project_context_hook_commands.add_parser(name, help=help_text)
        context_hook_command.add_argument("--project", type=Path, required=True)
        _add_json_option(context_hook_command)
        _set_handler(context_hook_command, handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.handler(args)
    except AvatarRuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _emit(payload, as_json=getattr(args, "as_json", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
