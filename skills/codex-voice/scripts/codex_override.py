"""Manage the optional user-wide Windows Codex command override.

The override is deliberately narrow: ``codex app-server ...`` is proxied
through the presence TUI bridge, while ordinary ``codex`` commands continue
to invoke the real CLI unchanged.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from cli_adapter import codex_executable, prepare_command


OVERRIDE_SCHEMA = "codex-voice/codex-command-override/v0.1"
OVERRIDE_CONFIG_NAME = "codex-voice-override.json"
SHIM_DIRECTORY_NAME = "bin"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def override_paths() -> tuple[Path, Path]:
    home = codex_home()
    return home / SHIM_DIRECTORY_NAME, home / OVERRIDE_CONFIG_NAME


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve() == right.expanduser().resolve()
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def render_cmd_shim(python: Path, script: Path, config: Path) -> str:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        f'"{python}" "{script}" --override-config "{config}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )


def render_powershell_shim(python: Path, script: Path, config: Path) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {_ps_quote(python)} {_ps_quote(script)} '--override-config' {_ps_quote(config)} @args\n"
        "exit $LASTEXITCODE\n"
    )


def is_app_server_invocation(arguments: list[str]) -> bool:
    return bool(arguments) and arguments[0].strip().lower() == "app-server"


def _load_config(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_user_path() -> tuple[str, int]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, value_type = winreg.QueryValueEx(key, "Path")
            return str(value), int(value_type)
    except FileNotFoundError:
        return "", winreg.REG_EXPAND_SZ


def _write_user_path(value: str, value_type: int) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, value_type, value)


def _broadcast_environment_change() -> None:
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 1000, None)
    except (AttributeError, OSError):
        pass


def _path_entries_without(directory: Path, entries: list[str]) -> list[str]:
    return [entry for entry in entries if entry and not _same_path(Path(os.path.expandvars(entry)), directory)]


def update_user_path(directory: Path, *, enabled: bool) -> None:
    if os.name != "nt":
        return
    current, value_type = _read_user_path()
    entries = _path_entries_without(directory, current.split(os.pathsep))
    if enabled:
        entries.insert(0, str(directory.resolve()))
    updated = os.pathsep.join(entries)
    if updated != current:
        _write_user_path(updated, value_type)
        _broadcast_environment_change()


def _resolve_real_cli(shim_directory: Path) -> str | None:
    """Resolve the real CLI while excluding our own shim directory."""

    original_path = os.environ.get("PATH", "")
    filtered_path = os.pathsep.join(_path_entries_without(shim_directory, original_path.split(os.pathsep)))
    old_path = os.environ.get("PATH")
    try:
        os.environ["PATH"] = filtered_path
        return codex_executable(exclude_paths=(shim_directory,))
    finally:
        if old_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old_path


def install_override(project_root: Path, voice_root: Path, python: Path, script: Path) -> dict[str, str]:
    """Install the user-wide shim and record enough ownership to remove it safely."""

    if os.name != "nt":
        raise RuntimeError("The user-wide Codex command override is supported only on Windows.")
    shim_directory, config_path = override_paths()
    real_cli = _resolve_real_cli(shim_directory)
    if not real_cli:
        raise RuntimeError("Could not find the real Codex CLI; set CODEX_CLI_PATH and retry the override.")

    shim_directory.mkdir(parents=True, exist_ok=True)
    config = {
        "schema": OVERRIDE_SCHEMA,
        "project_root": str(project_root.resolve()),
        "voice_root": str(voice_root.resolve()),
        "real_cli": str(Path(real_cli).resolve()),
        "python": str(python.resolve()),
        "script": str(script.resolve()),
        "shim_directory": str(shim_directory.resolve()),
    }
    cmd_path = shim_directory / "codex.cmd"
    ps_path = shim_directory / "codex.ps1"
    cmd_text = render_cmd_shim(python, script, config_path)
    ps_text = render_powershell_shim(python, script, config_path)
    for path, content in ((cmd_path, cmd_text), (ps_path, ps_text)):
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise RuntimeError(f"Refusing to replace a changed user command shim: {path}")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".codex-voice.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)
    cmd_path.write_text(cmd_text, encoding="utf-8", newline="")
    ps_path.write_text(ps_text, encoding="utf-8", newline="")
    update_user_path(shim_directory, enabled=True)
    return config


def remove_override(project_root: Path | None = None) -> bool:
    """Remove only the managed override owned by ``project_root``."""

    if os.name != "nt":
        return True
    shim_directory, config_path = override_paths()
    config = _load_config(config_path)
    if config is None or config.get("schema") != OVERRIDE_SCHEMA:
        return True
    if project_root is not None and not _same_path(Path(str(config.get("project_root", ""))), project_root):
        print("Preserving a Codex command override owned by another project.")
        return True

    python = Path(str(config.get("python", "")))
    script = Path(str(config.get("script", "")))
    recorded_config = config_path
    expected = {
        shim_directory / "codex.cmd": render_cmd_shim(python, script, recorded_config),
        shim_directory / "codex.ps1": render_powershell_shim(python, script, recorded_config),
    }
    changed = False
    for path, content in expected.items():
        if not path.exists():
            continue
        try:
            managed = path.read_text(encoding="utf-8") == content
        except OSError:
            managed = False
        if not managed:
            print(f"Preserving changed user command shim: {path}")
            changed = True
            continue
        path.unlink()
    if changed:
        return False
    config_path.unlink(missing_ok=True)
    update_user_path(shim_directory, enabled=False)
    return True


def run_override(config_path: Path, arguments: list[str]) -> int:
    config = _load_config(config_path)
    if config is None or config.get("schema") != OVERRIDE_SCHEMA:
        print(f"Invalid Codex command override configuration: {config_path}", file=sys.stderr)
        return 2
    project_root = Path(str(config.get("project_root", ""))).resolve()
    voice_root = Path(str(config.get("voice_root", project_root / ".codex-voice"))).resolve()
    real_cli = Path(str(config.get("real_cli", "")))
    if not real_cli.is_file():
        print(f"Configured real Codex CLI is missing: {real_cli}", file=sys.stderr)
        return 2

    command = [str(real_cli), *arguments]
    if not is_app_server_invocation(arguments):
        return subprocess.call(prepare_command(command), cwd=str(project_root), env=os.environ.copy())

    from tui_bridge import ArbiterInboxAdapter, TuiServerBridge

    bridge = TuiServerBridge(
        project_root,
        voice_root,
        command,
        ArbiterInboxAdapter(project_root, voice_root),
    )
    return bridge.run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--override-config", type=Path, required=True)
    args, command_arguments = parser.parse_known_args()
    return run_override(args.override_config.resolve(), command_arguments)


if __name__ == "__main__":
    raise SystemExit(main())
