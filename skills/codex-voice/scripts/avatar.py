"""Install, validate, select, and remove project-local presence avatars."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


SCHEMA = "codex-ai-presence/avatar/v0.1"
SELECTION_SCHEMA = "codex-ai-presence/avatar-selection/v0.1"
SOURCE_ROOT_NAME = ".codex-voice-avatars"
SELECTION_NAME = "avatar-selection.json"
MANIFEST_NAME = "avatar.json"
BUILTIN_ID = "builtin"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CAPABILITIES = {"activity", "audio", "move-mode", "avatar-state-v1"}


class AvatarError(RuntimeError):
    """A user-correctable avatar bundle or selection error."""


def project_root(value: Path) -> Path:
    return value.expanduser().resolve()


def runtime_root(root: Path) -> Path:
    return root / ".codex-voice"


def source_root(root: Path) -> Path:
    return root / SOURCE_ROOT_NAME


def selection_path(root: Path) -> Path:
    return runtime_root(root) / SELECTION_NAME


def is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def validate_entry(bundle: Path, entry: object) -> Path:
    if not isinstance(entry, str) or not entry or entry.startswith(("/", "\\")) or ":" in entry:
        raise AvatarError("avatar manifest entry must be a relative HTML path")
    entry_path = Path(entry)
    if entry_path.is_absolute() or ".." in entry_path.parts or entry_path.suffix.lower() != ".html":
        raise AvatarError("avatar manifest entry must be a relative .html file inside the bundle")
    bundle_root = bundle.resolve()
    resolved = (bundle_root / entry_path).resolve()
    if not is_within(bundle_root, resolved) or not resolved.is_file():
        raise AvatarError(f"avatar entry does not exist inside the bundle: {entry}")
    return resolved


def validate_bundle(bundle: Path) -> tuple[dict, Path]:
    bundle = bundle.resolve()
    manifest_path = bundle / MANIFEST_NAME
    if not bundle.is_dir() or not manifest_path.is_file():
        raise AvatarError(f"avatar bundle must contain {MANIFEST_NAME}: {bundle}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AvatarError(f"could not read avatar manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise AvatarError("avatar manifest must be a JSON object")
    allowed = {"schema", "id", "name", "version", "entry", "capabilities"}
    unknown = sorted(set(manifest) - allowed)
    if unknown:
        raise AvatarError(f"avatar manifest contains unsupported fields: {', '.join(unknown)}")
    if manifest.get("schema") != SCHEMA:
        raise AvatarError(f"avatar manifest schema must be {SCHEMA}")
    avatar_id = manifest.get("id")
    if not isinstance(avatar_id, str) or not ID_PATTERN.fullmatch(avatar_id):
        raise AvatarError("avatar manifest id must be lowercase letters, digits, and hyphens")
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise AvatarError("avatar manifest name must be 1-80 characters")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise AvatarError("avatar manifest version must use major.minor.patch form")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or len(capabilities) != len(set(capabilities)):
        raise AvatarError("avatar manifest capabilities must be a unique array")
    unknown_capabilities = sorted(set(capabilities) - CAPABILITIES)
    if unknown_capabilities:
        raise AvatarError(f"unsupported avatar capabilities: {', '.join(unknown_capabilities)}")
    return manifest, validate_entry(bundle, manifest.get("entry"))


def read_selection(root: Path) -> str:
    path = selection_path(root)
    if not path.is_file():
        return BUILTIN_ID
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BUILTIN_ID
    avatar_id = document.get("avatar_id") if isinstance(document, dict) else None
    return avatar_id if isinstance(avatar_id, str) and ID_PATTERN.fullmatch(avatar_id) else BUILTIN_ID


def write_selection(root: Path, avatar_id: str) -> None:
    runtime = runtime_root(root)
    runtime.mkdir(parents=True, exist_ok=True)
    path = selection_path(root)
    if avatar_id == BUILTIN_ID:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"schema": SELECTION_SCHEMA, "avatar_id": avatar_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def command_validate(args: argparse.Namespace) -> int:
    manifest, entry = validate_bundle(args.source)
    print(f"Valid avatar: {manifest['id']} ({manifest['name']})")
    print(f"Entry: {entry}")
    return 0


def command_install(args: argparse.Namespace) -> int:
    manifest, _ = validate_bundle(args.source)
    destination = source_root(args.project_root) / manifest["id"]
    if destination.exists():
        if not args.replace:
            raise AvatarError(f"avatar already exists: {destination}; use --replace to update it")
        shutil.rmtree(destination)
    source_root(args.project_root).mkdir(parents=True, exist_ok=True)
    shutil.copytree(args.source.resolve(), destination)
    print(f"Installed avatar {manifest['id']} to {destination}")
    if args.use:
        write_selection(args.project_root, manifest["id"])
        print("Selected avatar; restart the Orb to load it.")
    return 0


def command_list(args: argparse.Namespace) -> int:
    active = read_selection(args.project_root)
    print(f"builtin\t{'active' if active == BUILTIN_ID else 'available'}\tStrand Orb")
    root = source_root(args.project_root)
    if not root.is_dir():
        return 0
    for bundle in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            manifest, _ = validate_bundle(bundle)
        except AvatarError as exc:
            print(f"{bundle.name}\tinvalid\t{exc}")
            continue
        state = "active" if active == manifest["id"] else "available"
        print(f"{manifest['id']}\t{state}\t{manifest['name']}")
    return 0


def command_use(args: argparse.Namespace) -> int:
    if args.avatar_id == BUILTIN_ID:
        write_selection(args.project_root, BUILTIN_ID)
        print("Selected the built-in Strand Orb; restart the Orb to load it.")
        return 0
    if not ID_PATTERN.fullmatch(args.avatar_id):
        raise AvatarError("avatar id must be lowercase letters, digits, and hyphens")
    bundle = source_root(args.project_root) / args.avatar_id
    manifest, _ = validate_bundle(bundle)
    if manifest["id"] != args.avatar_id:
        raise AvatarError("avatar directory and manifest id do not match")
    write_selection(args.project_root, args.avatar_id)
    print(f"Selected avatar {args.avatar_id}; restart the Orb to load it.")
    return 0


def command_remove(args: argparse.Namespace) -> int:
    if args.avatar_id == BUILTIN_ID:
        raise AvatarError("the built-in Strand Orb cannot be removed")
    if not ID_PATTERN.fullmatch(args.avatar_id):
        raise AvatarError("avatar id must be lowercase letters, digits, and hyphens")
    if read_selection(args.project_root) == args.avatar_id:
        write_selection(args.project_root, BUILTIN_ID)
        print("Removed the active selection; the built-in Strand Orb is selected.")
    bundle = source_root(args.project_root) / args.avatar_id
    if not bundle.is_dir():
        raise AvatarError(f"avatar was not found: {args.avatar_id}")
    shutil.rmtree(bundle)
    print(f"Removed avatar {args.avatar_id}")
    return 0


def main() -> int:
    from presence_compat import delegate

    delegated = delegate("avatar", __import__("sys").argv[1:])
    if delegated is not None:
        return delegated
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--source", type=Path, required=True)
    validate.set_defaults(handler=command_validate)

    install = commands.add_parser("install")
    install.add_argument("--source", type=Path, required=True)
    install.add_argument("--replace", action="store_true")
    install.add_argument("--use", action="store_true")
    install.set_defaults(handler=command_install)

    listing = commands.add_parser("list")
    listing.set_defaults(handler=command_list)

    use = commands.add_parser("use")
    use.add_argument("avatar_id")
    use.set_defaults(handler=command_use)

    remove = commands.add_parser("remove")
    remove.add_argument("avatar_id")
    remove.set_defaults(handler=command_remove)

    args = parser.parse_args()
    args.project_root = project_root(args.project_root)
    try:
        return args.handler(args)
    except AvatarError as exc:
        print(f"Avatar error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
