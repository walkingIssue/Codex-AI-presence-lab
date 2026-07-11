"""Remove the project-local Codex AI Presence integration safely."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from toggle import run_orb_script, stop_watcher


MANAGED_STATUS = "Speaking Codex response"


def resolve_project_root(value: Path | None) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".codex-voice").is_dir():
            return candidate
    return current


def normalized(value: object) -> str:
    return str(value).replace("\\", "/").lower()


def expected_hook_command(project_root: Path, voice_root: Path) -> str:
    python = voice_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    hook = project_root / ".codex" / "hooks" / "speak.py"
    return f'"{python}" "{hook}"'


def is_managed_wrapper(wrapper: object, expected: str, project_root: Path, voice_root: Path) -> bool:
    if not isinstance(wrapper, dict):
        return False
    entries = wrapper.get("hooks")
    if not isinstance(entries, list):
        return False
    expected_text = normalized(expected)
    hook_text = normalized(project_root / ".codex" / "hooks" / "speak.py")
    voice_text = normalized(voice_root)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        commands = [entry.get("command"), entry.get("commandWindows")]
        for command in commands:
            if not command:
                continue
            command_text = normalized(command)
            if command_text == expected_text:
                return True
            if (
                hook_text in command_text
                and voice_text in command_text
                and entry.get("statusMessage") == MANAGED_STATUS
            ):
                return True
    return False


def remove_hook_registration(project_root: Path, voice_root: Path) -> tuple[bool, int]:
    hooks_path = project_root / ".codex" / "hooks.json"
    if not hooks_path.is_file():
        return True, 0
    try:
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not safely inspect {hooks_path}: {exc}")
        return False, 0
    if not isinstance(document, dict):
        print(f"Preserving invalid hooks file: {hooks_path}")
        return False, 0
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return True, 0
    stop_hooks = hooks.get("Stop")
    if not isinstance(stop_hooks, list):
        return True, 0

    expected = expected_hook_command(project_root, voice_root)
    retained = [
        wrapper
        for wrapper in stop_hooks
        if not is_managed_wrapper(wrapper, expected, project_root, voice_root)
    ]
    removed = len(stop_hooks) - len(retained)
    if not removed:
        return True, 0

    if retained:
        hooks["Stop"] = retained
    else:
        hooks.pop("Stop", None)
    temporary = hooks_path.with_suffix(".codex-voice.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, hooks_path)
    return True, removed


def looks_like_managed_hook(path: Path, source: Path) -> bool:
    try:
        if path.read_bytes() == source.read_bytes():
            return True
        return b"local Kokoro TTS" in path.read_bytes()[:2048]
    except OSError:
        return False


def remove_hook_file(project_root: Path, *, force: bool) -> bool:
    hooks_dir = project_root / ".codex" / "hooks"
    hook = hooks_dir / "speak.py"
    backup = hooks_dir / "speak.py.codex-voice-backup.py"
    source = Path(__file__).with_name("speak.py")

    if backup.exists():
        if hook.exists() and not looks_like_managed_hook(hook, source) and not force:
            print(f"Preserving changed hook; use --force only if it is Codex AI Presence: {hook}")
            return False
        if hook.is_file():
            hook.unlink()
        elif hook.exists():
            print(f"Preserving unexpected hook path: {hook}")
            return False
        shutil.move(str(backup), str(hook))
        print(f"Restored previous hook: {hook}")
        return True

    if not hook.exists():
        return True
    if not hook.is_file():
        print(f"Preserving unexpected hook path: {hook}")
        return False
    if not force and not looks_like_managed_hook(hook, source):
        print(f"Preserving changed hook; use --force only if it is Codex AI Presence: {hook}")
        return False
    hook.unlink()
    print(f"Removed voice hook: {hook}")
    return True


def stop_runtime(voice_root: Path) -> None:
    if not voice_root.is_dir():
        return
    orb_stop = voice_root / "orb" / "stop_orb.ps1"
    if os.name == "nt" and orb_stop.is_file():
        try:
            run_orb_script(voice_root, "stop_orb.ps1")
        except OSError:
            pass
    stop_watcher(voice_root)
    for marker in (
        voice_root / "watcher.pid",
        voice_root / "orb" / "orb.pid",
        voice_root / "enabled",
        voice_root / "orb.enabled",
    ):
        marker.unlink(missing_ok=True)


def remove_voice_root(voice_root: Path, *, keep_assets: bool) -> None:
    if not voice_root.exists() and not voice_root.is_symlink():
        return
    if keep_assets:
        for marker in (
            voice_root / "sessions.json",
            voice_root / "watcher.pid",
            voice_root / "orb" / "orb.pid",
            voice_root / "enabled",
            voice_root / "orb.enabled",
            voice_root / "progress",
        ):
            marker.unlink(missing_ok=True)
        print(f"Removed runtime markers; retained models and environments: {voice_root}")
        return
    if voice_root.is_symlink():
        voice_root.unlink()
    else:
        shutil.rmtree(voice_root)
    print(f"Removed project-local voice installation: {voice_root}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, help="project to clean (defaults to the nearest project root)")
    parser.add_argument("--yes", action="store_true", help="confirm removal of the project-local installation")
    parser.add_argument(
        "--keep-assets",
        action="store_true",
        help="remove hooks and runtime markers but retain models, environments, and Orb files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="remove a changed speak.py hook instead of preserving it",
    )
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to uninstall without --yes; this removes the project-local voice runtime.")
        return 2

    project_root = resolve_project_root(args.project_root)
    voice_root = project_root / ".codex-voice"
    stop_runtime(voice_root)
    registration_ok, removed = remove_hook_registration(project_root, voice_root)
    hook_ok = remove_hook_file(project_root, force=args.force)
    remove_voice_root(voice_root, keep_assets=args.keep_assets)
    if removed:
        print(f"Removed {removed} Codex AI Presence Stop hook registration(s).")
    if not registration_ok or not hook_ok:
        print("Uninstall completed with protected files; inspect the warnings above.")
        return 1
    print(f"Codex AI Presence uninstalled from {project_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
