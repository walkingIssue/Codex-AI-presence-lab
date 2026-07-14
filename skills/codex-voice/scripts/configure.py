"""Show or change the complete project-local Codex voice configuration."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from configuration import (
    DEFAULT_MODE,
    DEFAULT_COMMENTARY_VOLUME,
    DEFAULT_PROVIDER,
    DEFAULT_SPEED,
    DEFAULT_VOICE,
    DEFAULT_VOLUME,
    MAX_SPEED,
    MIN_SPEED,
    load_settings,
    write_setting,
)
from session_scope import (
    current_thread_id,
    is_project_mode,
    load_state,
    register_session,
    set_project_mode,
)
from toggle import (
    environment_python,
    find_voice_root,
    restart_watcher,
    run_orb_script,
    stop_watcher,
    watcher_is_running,
)


VOICE_PATTERN = re.compile(r"^[a-z]{2}_[a-z0-9_]+$")


def require_voice_root() -> Path:
    voice_root = find_voice_root()
    if voice_root is None:
        raise RuntimeError("No project-local .codex-voice directory was found.")
    return voice_root


def validate_voice(value: str) -> str:
    value = value.strip().lower()
    if not VOICE_PATTERN.fullmatch(value):
        raise ValueError("voice must look like a Kokoro voice ID, for example bf_isabella")
    return value


def validate_speed(value: float) -> float:
    if not MIN_SPEED <= value <= MAX_SPEED:
        raise ValueError(f"speed must be between {MIN_SPEED} and {MAX_SPEED}")
    return value


def validate_volume(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError("volume must be between 0 and 100")
    return value


def validate_provider(voice_root: Path, provider: str) -> None:
    if provider == "directml":
        if not (
            environment_python(voice_root / ".dml-venv")
        ).is_file() or not (
            voice_root / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx"
        ).is_file():
            raise RuntimeError(
                "DirectML is not ready; run setup.py --directml before selecting it."
            )
    if provider == "cuda":
        if not (
            environment_python(voice_root / ".cuda-venv")
        ).is_file() or not (voice_root / "kokoro-v1.0.int8.onnx").is_file():
            raise RuntimeError(
                "CUDA is not ready; run setup.py --cuda before selecting it."
            )
    if provider == "openvino":
        if not environment_python(voice_root / ".openvino-venv").is_file() or not (
            voice_root / "gpu_patch" / "kokoro-v1.0.fp16-gpu.openvino.onnx"
        ).is_file():
            raise RuntimeError(
                "OpenVINO is not ready; run setup.py --openvino before selecting it."
            )


def set_enabled(voice_root: Path, enabled: bool) -> None:
    marker = voice_root / "enabled"
    if enabled:
        state = load_state(voice_root)
        if is_project_mode(state):
            marker.write_text("on\n", encoding="utf-8")
        else:
            thread_id = current_thread_id()
            if thread_id is None:
                raise RuntimeError(
                    "No CODEX_THREAD_ID was found; use --scope project outside a Codex session."
                )
            register_session(voice_root, voice_root.parent, thread_id)
            marker.write_text("on\n", encoding="utf-8")
        restart_watcher(voice_root)
    else:
        marker.unlink(missing_ok=True)
        stop_watcher(voice_root)


def set_scope(voice_root: Path, scope: str) -> None:
    if scope == "off":
        set_enabled(voice_root, False)
        return
    if scope == "project":
        set_project_mode(voice_root)
        (voice_root / "enabled").write_text("on\n", encoding="utf-8")
        restart_watcher(voice_root)
        return
    thread_id = current_thread_id()
    if thread_id is None:
        raise RuntimeError(
            "No CODEX_THREAD_ID was found; session scope must be configured from a Codex session."
        )
    register_session(voice_root, voice_root.parent, thread_id)
    (voice_root / "enabled").write_text("on\n", encoding="utf-8")
    restart_watcher(voice_root)


def apply_values(voice_root: Path, values: dict[str, object]) -> None:
    provider_changed = False
    for name in ("voice", "speed", "mode", "provider", "volume", "commentary_volume"):
        value = values.get(name)
        if value is None:
            continue
        if name == "voice":
            value = validate_voice(str(value))
        elif name == "speed":
            value = validate_speed(float(value))
        elif name == "mode" and value not in {"stream", "quality"}:
            raise ValueError("mode must be stream or quality")
        elif name == "provider":
            validate_provider(voice_root, str(value))
            provider_changed = True
        elif name == "volume":
            value = validate_volume(int(value))
        elif name == "commentary_volume":
            value = validate_volume(int(value))
        write_setting(voice_root, name, value)

    progress = values.get("progress")
    if progress is not None:
        path = voice_root / "progress"
        if progress == "on":
            path.write_text("on\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    orb = values.get("orb")
    if orb is not None:
        marker = voice_root / "orb.enabled"
        if orb == "on":
            marker.write_text("on\n", encoding="utf-8")
            try:
                run_orb_script(voice_root, "start_orb.ps1")
            except FileNotFoundError as exc:
                marker.unlink(missing_ok=True)
                raise RuntimeError(
                    "The Strand Orb is not installed; rerun setup without --no-orb."
                ) from exc
        else:
            marker.unlink(missing_ok=True)
            if (voice_root / "orb" / "stop_orb.ps1").is_file():
                run_orb_script(voice_root, "stop_orb.ps1")

    scope = values.get("scope")
    if scope is not None:
        set_scope(voice_root, str(scope))
    elif values.get("enabled") is not None:
        set_enabled(voice_root, values["enabled"] == "on")
    elif provider_changed and watcher_is_running(voice_root):
        restart_watcher(voice_root)


def current_scope(voice_root: Path) -> str:
    return "project" if is_project_mode(load_state(voice_root)) else "session"


def show(voice_root: Path) -> None:
    settings = load_settings(voice_root)
    enabled = (voice_root / "enabled").is_file()
    print("Codex AI Presence configuration")
    print(f"  voice:    {settings['voice']} (Kokoro voice ID)")
    print(f"  speed:    {settings['speed']:.2f}x ({MIN_SPEED}-{MAX_SPEED}x)")
    print(f"  mode:     {settings['mode']} (stream or quality)")
    print(f"  provider: {settings['provider']} (cpu, cuda, directml, or openvino)")
    print(f"  volume:   {settings['volume']}% (main response)")
    print(f"  commentary volume: {settings['commentary_volume']}% of main volume (default {DEFAULT_COMMENTARY_VOLUME}%)")
    print(f"  progress: {'on' if settings['progress'] else 'off'}")
    print(f"  orb:      {'on' if settings['orb'] else 'off'}")
    print(f"  scope:    {current_scope(voice_root)}")
    print(f"  enabled:  {'on' if enabled else 'off'}")
    print(f"  project:  {voice_root.parent}")


def prompt(label: str, current: object) -> str:
    answer = input(f"{label} [{current}]: ").strip()
    return answer or str(current)


def interactive(voice_root: Path) -> None:
    settings = load_settings(voice_root)
    values: dict[str, object] = {
        "voice": prompt("Voice ID", settings["voice"]),
        "speed": float(prompt("Speed", settings["speed"])),
        "mode": prompt("Mode (stream/quality)", settings["mode"]).lower(),
        "provider": prompt("Provider (cpu/cuda/directml/openvino)", settings["provider"]).lower(),
        "volume": int(prompt("Volume (0-100)", settings["volume"])),
        "commentary_volume": int(prompt("Commentary volume as % of main (0-100)", settings["commentary_volume"])),
        "progress": prompt("Visible progress (on/off)", "on" if settings["progress"] else "off").lower(),
        "orb": prompt("Strand Orb (on/off)", "on" if settings["orb"] else "off").lower(),
        "scope": prompt("Scope (session/project/off)", current_scope(voice_root)).lower(),
    }
    apply_values(voice_root, values)
    show(voice_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("show", help="print all current settings and choices")
    subparsers.add_parser("interactive", help="walk through every setting")
    set_parser = subparsers.add_parser("set", help="change one or more settings")
    set_parser.add_argument("--voice")
    set_parser.add_argument("--speed", type=float)
    set_parser.add_argument("--mode", choices=("stream", "quality"))
    set_parser.add_argument("--provider", choices=("cpu", "cuda", "directml", "openvino"))
    set_parser.add_argument("--volume", type=int)
    set_parser.add_argument("--commentary-volume", dest="commentary_volume", type=int)
    set_parser.add_argument("--progress", choices=("on", "off"))
    set_parser.add_argument("--orb", choices=("on", "off"))
    set_parser.add_argument("--scope", choices=("session", "project", "off"))
    set_parser.add_argument("--enabled", choices=("on", "off"))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        voice_root = require_voice_root()
        if args.command in {None, "interactive"}:
            interactive(voice_root)
        elif args.command == "show":
            show(voice_root)
        else:
            values = {
                name: getattr(args, name)
                for name in (
                    "voice",
                    "speed",
                    "mode",
                    "provider",
                    "volume",
                    "commentary_volume",
                    "progress",
                    "orb",
                    "scope",
                    "enabled",
                )
                if getattr(args, name) is not None
            }
            if not values:
                parser.error("set requires at least one setting")
            apply_values(voice_root, values)
            show(voice_root)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
