"""Public intent-level Presence Runtime v0.2 command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import PresenceError, ValidationError
from .protocol import connect


def control_call(operation: str, arguments: dict[str, Any]) -> Any:
    connection = connect()
    try:
        connection.send({"type": "control", "client": "presence-cli/v0.2"})
        ready = connection.recv()
        if ready is None or ready.get("type") != "control/ready":
            raise RuntimeError(f"Presence Runtime control handshake failed: {ready}")
        connection.send(
            {
                "type": "command",
                "operation": operation,
                "arguments": arguments,
            }
        )
        response = connection.recv()
        if response is None:
            raise RuntimeError("Presence Runtime closed the control connection")
        if response.get("type") == "error":
            error = response.get("error", {})
            raise RuntimeError(str(error.get("message") or "Presence Runtime command failed"))
        if response.get("type") != "result":
            raise RuntimeError(f"Unexpected Presence Runtime response: {response}")
        return response.get("result")
    finally:
        connection.close()


def emit(value: Any, *, compact: bool = False) -> None:
    if isinstance(value, str):
        print(value)
        return
    print(
        json.dumps(
            value,
            indent=None if compact else 2,
            sort_keys=True,
            # Windows launchers may inherit a legacy console code page. JSON
            # escapes keep catalog paths and model labels lossless there.
            ensure_ascii=True,
        )
    )


def scope_arguments(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source, target in (
        ("project", "project_root"),
        ("project_id", "project_id"),
        ("session", "session_id"),
        ("binding", "binding_id"),
    ):
        value = getattr(args, source, None)
        if value:
            result[target] = value
    return result


def add_project_scope(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--project", type=str, help="registered local project root")
    group.add_argument("--project-id", help="registered project instance UUID")


def add_session_scope(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--session", help="session/thread UUID")
    group.add_argument("--binding", help="stable runtime binding UUID")
    add_project_scope(parser, required=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="presence", description=__doc__)
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    groups = parser.add_subparsers(dest="group", required=True)

    runtime = groups.add_parser("runtime")
    runtime_commands = runtime.add_subparsers(dest="action", required=True)
    install = runtime_commands.add_parser("install")
    install.add_argument("--source", type=Path)
    install.add_argument("--python", type=Path)
    install.add_argument("--provider", choices=("cpu", "cuda", "directml", "openvino"))
    install.add_argument("--with-input", action="store_true")
    install.add_argument("--no-start", action="store_true")
    for name in ("start", "stop", "restart", "status"):
        runtime_commands.add_parser(name)
    doctor = runtime_commands.add_parser("doctor")
    doctor.add_argument("--binding", dest="binding_id")
    policy = runtime_commands.add_parser("set-policy")
    policy.add_argument("--provider", choices=("cpu", "cuda", "directml", "openvino"))
    input_group = policy.add_mutually_exclusive_group()
    input_group.add_argument("--enable-input", action="store_true")
    input_group.add_argument("--disable-input", action="store_true")
    uninstall = runtime_commands.add_parser("uninstall")
    uninstall.add_argument("--all-projects", action="store_true")
    uninstall.add_argument("--purge-state", action="store_true")
    uninstall.add_argument("--purge-catalog", action="store_true")

    project = groups.add_parser("project")
    project_commands = project.add_subparsers(dest="action", required=True)
    register = project_commands.add_parser("register")
    register.add_argument("project_root", type=Path)
    unregister = project_commands.add_parser("unregister")
    add_project_scope(unregister, required=True)
    unregister.add_argument("--all-sources", action="store_true")
    status = project_commands.add_parser("status")
    add_project_scope(status, required=True)
    set_profile = project_commands.add_parser("set-profile")
    add_project_scope(set_profile, required=True)
    set_profile.add_argument("profile_ref")
    clear_profile = project_commands.add_parser("clear-profile")
    add_project_scope(clear_profile, required=True)
    relocate = project_commands.add_parser("relocate")
    add_project_scope(relocate, required=True)
    relocate.add_argument("new_root", type=Path)

    session = groups.add_parser("session")
    session_commands = session.add_subparsers(dest="action", required=True)
    session_list = session_commands.add_parser("list")
    add_project_scope(session_list, required=True)
    session_show = session_commands.add_parser("show")
    add_session_scope(session_show, required=True)
    session_profile = session_commands.add_parser("set-profile")
    add_session_scope(session_profile, required=True)
    session_profile.add_argument("profile_ref")
    session_set = session_commands.add_parser("set")
    add_session_scope(session_set, required=True)
    session_set.add_argument("--voice", dest="voice_id")
    session_set.add_argument("--speed", type=float)
    session_set.add_argument("--playback-mode", choices=("stream", "quality"))
    session_set.add_argument("--volume", type=int)
    session_set.add_argument("--commentary-ratio", type=float)
    session_set.add_argument("--avatar", dest="avatar_ref")
    session_set.add_argument("--preset", dest="preset_ref")
    session_set.add_argument("--clear-preset", action="store_true")
    session_set.add_argument("--progress-visible", choices=("on", "off"))
    session_set.add_argument("--renderer-visible", choices=("on", "off"))
    session_set.add_argument(
        "--patch",
        type=Path,
        help="validated sparse override JSON; runtime policy/routing fields are rejected",
    )
    session_clear = session_commands.add_parser("clear")
    add_session_scope(session_clear, required=True)
    session_clear.add_argument("fields", nargs="*")
    session_focus = session_commands.add_parser("focus")
    add_session_scope(session_focus, required=True)

    catalog = groups.add_parser("catalog")
    catalog.add_argument("kind", choices=("avatar", "preset", "profile"))
    catalog_commands = catalog.add_subparsers(dest="action", required=True)
    catalog_commands.add_parser("list")
    catalog_show = catalog_commands.add_parser("show")
    catalog_show.add_argument("reference")
    catalog_import = catalog_commands.add_parser("import")
    catalog_import.add_argument("source", type=Path)
    catalog_import.add_argument("--assets", type=Path)
    catalog_export = catalog_commands.add_parser("export")
    catalog_export.add_argument("reference")
    catalog_export.add_argument("output", type=Path)
    catalog_export.add_argument("--force", action="store_true")
    catalog_remove = catalog_commands.add_parser("remove")
    catalog_remove.add_argument("reference")
    catalog_remove.add_argument("--force", action="store_true")

    avatar = groups.add_parser("avatar")
    avatar_commands = avatar.add_subparsers(dest="action", required=True)
    avatar_use = avatar_commands.add_parser("use")
    avatar_use.add_argument("reference")
    add_session_scope(avatar_use, required=False)
    avatar_use.add_argument("--clear-preset", action="store_true")

    preset = groups.add_parser("preset")
    preset_commands = preset.add_subparsers(dest="action", required=True)
    preset_use = preset_commands.add_parser("use")
    preset_use.add_argument("reference", nargs="?")
    add_session_scope(preset_use, required=False)

    inspect = groups.add_parser("inspect")
    inspect_commands = inspect.add_subparsers(dest="action", required=True)
    effective = inspect_commands.add_parser("effective")
    add_session_scope(effective, required=False)
    effective.add_argument("--json", action="store_true")

    migrate = groups.add_parser("migrate")
    migrate_commands = migrate.add_subparsers(dest="action", required=True)
    for name in ("status", "retry", "rollback"):
        command = migrate_commands.add_parser(name)
        add_project_scope(command, required=True)
    return parser


def runtime_command(args: argparse.Namespace) -> Any:
    from . import installer, lifecycle

    if args.action == "install":
        return installer.install(
            source=args.source,
            python=args.python,
            provider=args.provider,
            with_input=args.with_input,
            start=not args.no_start,
        )
    if args.action == "start":
        return lifecycle.start()
    if args.action == "stop":
        return lifecycle.stop()
    if args.action == "restart":
        lifecycle.stop()
        return lifecycle.start()
    if args.action == "status":
        try:
            return control_call("runtime.status", {})
        except (OSError, RuntimeError):
            return lifecycle.status()
    if args.action == "doctor":
        return control_call(
            "runtime.doctor",
            {"binding_id": args.binding_id} if args.binding_id else {},
        )
    if args.action == "set-policy":
        microphone_permission = None
        if args.enable_input:
            microphone_permission = True
        elif args.disable_input:
            microphone_permission = False
        result = control_call(
            "runtime.set_policy",
            {
                "provider": args.provider,
                "microphone_permission": microphone_permission,
            },
        )
        if isinstance(result, dict) and result.get("restart_required"):
            lifecycle.stop()
            result = {**result, "runtime": lifecycle.start()}
        return result
    if args.action == "uninstall":
        return installer.uninstall(
            all_projects=args.all_projects,
            purge_state=args.purge_state,
            purge_catalog=args.purge_catalog,
        )
    raise ValidationError(f"unsupported runtime action: {args.action}")


def command_operation(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if args.group == "project":
        arguments = scope_arguments(args)
        if args.action == "register":
            arguments = {"project_root": str(args.project_root.resolve())}
        elif args.action == "unregister":
            arguments["all_sources"] = args.all_sources
        elif args.action == "set-profile":
            arguments["profile_ref"] = args.profile_ref
        elif args.action == "relocate":
            arguments["new_root"] = str(args.new_root.resolve())
        return f"project.{args.action.replace('-', '_')}", arguments

    if args.group == "session":
        arguments = scope_arguments(args)
        if args.action == "set-profile":
            arguments["profile_ref"] = args.profile_ref
        elif args.action == "set":
            changes: dict[str, Any] = {}
            if args.patch is not None:
                loaded = json.loads(args.patch.expanduser().resolve().read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValidationError("session patch file must contain a JSON object")
                changes.update(loaded)
            changes.update({
                name: value
                for name, value in (
                    ("voice_id", args.voice_id),
                    ("speed", args.speed),
                    ("playback_mode", args.playback_mode),
                    ("volume", args.volume),
                    ("commentary_ratio", args.commentary_ratio),
                    ("avatar_ref", args.avatar_ref),
                    ("preset_ref", args.preset_ref),
                )
                if value is not None
            })
            if args.clear_preset:
                changes["preset_ref"] = None
            if args.progress_visible is not None:
                changes["progress_visible"] = args.progress_visible == "on"
            if args.renderer_visible is not None:
                changes["renderer_visible"] = args.renderer_visible == "on"
            arguments["changes"] = changes
        elif args.action == "clear":
            arguments["fields"] = args.fields or None
        return f"session.{args.action.replace('-', '_')}", arguments

    if args.group == "catalog":
        arguments = {"kind": args.kind}
        for name in ("reference", "source", "assets", "output", "force"):
            value = getattr(args, name, None)
            if value is not None:
                arguments[name] = str(value.resolve()) if isinstance(value, Path) else value
        return f"catalog.{args.action}", arguments

    if args.group in {"avatar", "preset"}:
        arguments = scope_arguments(args)
        arguments["reference"] = args.reference
        if args.group == "avatar":
            arguments["clear_preset"] = args.clear_preset
        return f"{args.group}.use", arguments

    if args.group == "inspect":
        return "inspect.effective", scope_arguments(args)

    if args.group == "migrate":
        return f"migrate.{args.action}", scope_arguments(args)

    raise ValidationError(f"unsupported command group: {args.group}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.group == "runtime":
            result = runtime_command(args)
        else:
            operation, arguments = command_operation(args)
            result = control_call(operation, arguments)
        emit(result, compact=args.compact)
        return 0
    except (PresenceError, OSError, RuntimeError, ValueError) as exc:
        print(f"presence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
