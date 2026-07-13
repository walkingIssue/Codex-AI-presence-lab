"""CLI control surface for the Electron Orb voice-input bridge."""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path

from inbox import Inbox, database_path


DEFAULT_SETTINGS = {
    "input_enabled": False,
    "input_gesture": "hold-ctrl-alt-right",
    "delivery_mode": "clipboard",
    "session_lock": "through-response",
    "session_labels": "session-change",
    "session_label_template": "{session_name} says",
    "max_record_seconds": 60,
    "lock_timeout_seconds": 120,
}


def settings_path(voice_root: Path) -> Path:
    return voice_root / "input.json"


def load_settings(voice_root: Path) -> dict[str, object]:
    try:
        value = json.loads(settings_path(voice_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(value, dict):
        settings.update(value)
    return settings


def save_settings(voice_root: Path, settings: dict[str, object]) -> None:
    voice_root.mkdir(parents=True, exist_ok=True)
    temporary = settings_path(voice_root).with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, settings_path(voice_root))


def runtime(voice_root: Path) -> Inbox:
    return Inbox(database_path(voice_root))


def request_immediate_playback_stop(voice_root: Path) -> None:
    """Pause the stream and terminate only its disposable OS audio sink."""
    try:
        (voice_root / "tts-resume.request").unlink(missing_ok=True)
        (voice_root / "tts-stop.request").write_text("pause\n", encoding="utf-8")
    except OSError:
        pass
    pid_path = voice_root / "tts-player.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def request_playback_resume(voice_root: Path) -> None:
    """Allow a paused playback consumer to drain already-generated PCM."""
    try:
        (voice_root / "tts-resume.request").write_text("resume\n", encoding="utf-8")
    except OSError:
        pass


def inside_recordings(voice_root: Path, value: str) -> Path:
    recordings = (voice_root / "inbox" / "recordings").resolve()
    candidate = Path(value).expanduser().resolve()
    try:
        candidate.relative_to(recordings)
    except ValueError as exc:
        raise ValueError("recording must be inside .codex-voice/inbox/recordings") from exc
    if not candidate.is_file():
        raise ValueError("recording file does not exist")
    return candidate


def emit(value: dict[str, object], code: int = 0) -> int:
    print(json.dumps(value, separators=(",", ":")))
    return code


def control(voice_root: Path, command: str, payload: dict[str, object]) -> int:
    settings = load_settings(voice_root)
    inbox = runtime(voice_root)
    if not bool(settings.get("input_enabled")):
        return emit({"ok": False, "error": "voice_input_disabled"}, 3)
    if command == "capture-start":
        focus = inbox.get_state("focus", {})
        focus = focus if isinstance(focus, dict) else {}
        requested_target = payload.get("target_session_id")
        target = requested_target or focus.get("session_id") or inbox.get_state("last_session_id")
        if not isinstance(target, str) or not target:
            return emit({"ok": False, "error": "no_target_session"}, 4)
        # Gate queue claims before asking ffplay to stop. Otherwise the playback
        # thread can reclaim the interrupted row before the watcher consumes the
        # capture-start control, producing an audible restart while recording.
        capture_sequence = inbox.next_counter("input_capture_sequence")
        inbox.set_state("focus", {"state": "listening", "session_id": target})
        inbox.set_state(
            "input",
            {"state": "listening", "session_id": target, "capture_sequence": capture_sequence},
        )
        command_id = inbox.add_control(
            command,
            {**payload, "target_session_id": target, "capture_sequence": capture_sequence},
        )
        request_immediate_playback_stop(voice_root)
        return emit(
            {
                "ok": True,
                "command_id": command_id,
                "target_session_id": target,
                "capture_sequence": capture_sequence,
            }
        )
    if command == "capture-finish":
        recording = inside_recordings(voice_root, str(payload.get("recording", "")))
        sequence = payload.get("capture_sequence")
        current_input = inbox.get_state("input", {})
        current_input = current_input if isinstance(current_input, dict) else {}
        if not isinstance(sequence, int) or sequence < 1:
            sequence = current_input.get("capture_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            return emit({"ok": False, "error": "capture_sequence_missing"}, 5)
        active_sequence = current_input.get("capture_sequence")
        if isinstance(active_sequence, int) and active_sequence != sequence:
            return emit({"ok": False, "error": "capture_sequence_mismatch"}, 6)
        command_id = inbox.add_control(
            command,
            {**payload, "recording": str(recording), "capture_sequence": sequence},
        )
        inbox.set_state("input", {"state": "transcribing", "capture_sequence": sequence})
        request_playback_resume(voice_root)
        return emit({"ok": True, "command_id": command_id, "capture_sequence": sequence})
    if command == "capture-cancel":
        command_id = inbox.add_control(command, payload)
        inbox.set_state("input", {"state": "idle"})
        request_playback_resume(voice_root)
        return emit({"ok": True, "command_id": command_id})
    return emit({"ok": False, "error": "unknown_control"}, 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    settings_parser = subparsers.add_parser("settings")
    settings_parser.add_argument("--enabled", choices=("on", "off"))
    settings_parser.add_argument("--delivery-mode", choices=("clipboard", "app-server"))
    settings_parser.add_argument(
        "--labels",
        choices=("off", "first-message", "session-change", "every-message"),
    )
    settings_parser.add_argument("--template")
    settings_parser.add_argument("--max-record-seconds", type=int)
    settings_parser.add_argument("--lock-timeout-seconds", type=int)

    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("kind", choices=("capture-start", "capture-finish", "capture-cancel"))
    control_parser.add_argument("--recording")
    control_parser.add_argument("--capture-sequence", type=int)
    control_parser.add_argument("--target-session-id")

    subparsers.add_parser("status")

    args = parser.parse_args()
    voice_root = args.voice_root.resolve()
    if args.command == "settings":
        settings = load_settings(voice_root)
        if args.enabled is not None:
            settings["input_enabled"] = args.enabled == "on"
        if args.delivery_mode is not None:
            settings["delivery_mode"] = args.delivery_mode
        if args.labels is not None:
            settings["session_labels"] = args.labels
        if args.template is not None:
            settings["session_label_template"] = args.template[:200]
        if args.max_record_seconds is not None:
            settings["max_record_seconds"] = max(1, min(60, args.max_record_seconds))
        if args.lock_timeout_seconds is not None:
            settings["lock_timeout_seconds"] = max(10, min(600, args.lock_timeout_seconds))
        if any(
            value is not None
            for value in (
                args.enabled,
                args.delivery_mode,
                args.labels,
                args.template,
                args.max_record_seconds,
                args.lock_timeout_seconds,
            )
        ):
            save_settings(voice_root, settings)
        return emit({"ok": True, "settings": settings})
    if args.command == "status":
        return emit({"ok": True, "settings": load_settings(voice_root), "runtime": runtime(voice_root).status()})
    payload = {"recording": args.recording} if args.recording else {}
    if args.capture_sequence is not None:
        payload["capture_sequence"] = args.capture_sequence
    if args.target_session_id:
        payload["target_session_id"] = args.target_session_id
    return control(voice_root, args.kind, payload)


if __name__ == "__main__":
    raise SystemExit(main())
