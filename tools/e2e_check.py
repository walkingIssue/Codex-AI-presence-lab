"""Run the safe, no-model E2E gate for a projected Codex voice skill."""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REQUIRED_FILES = (
    "SKILL.md",
    "RUNTIME-MANIFEST.md",
    "agents/openai.yaml",
    "scripts/configuration.py",
    "scripts/configure.py",
    "scripts/activity.py",
    "scripts/tui_bridge.py",
    "scripts/launch_codex.py",
    "scripts/launch_codex.sh",
    "scripts/codex-presence.sh",
    "scripts/tui_kokoro_worker.py",
    "scripts/setup.py",
    "scripts/session_scope.py",
    "scripts/speak.py",
    "scripts/toggle.py",
    "scripts/uninstall.py",
    "scripts/watcher.py",
    "scripts/global_arbiter.py",
    "scripts/presence_service.py",
    "scripts/profiles.py",
    "scripts/clipboard.py",
)


def find_skill(source: Path) -> Path:
    candidate = source / "skills" / "codex-voice"
    if candidate.is_dir():
        return candidate
    if (source / "SKILL.md").is_file():
        return source
    raise SystemExit(f"Could not find skills/codex-voice under {source}")


def run(command: list[str], cwd: Path, *, expect: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    if result.returncode != expect:
        raise SystemExit(f"Command returned {result.returncode}, expected {expect}: {' '.join(command)}")
    return result


def assert_file(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Required file is missing: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    args = parser.parse_args()
    source = args.source.resolve()
    skill = find_skill(source)

    for relative in REQUIRED_FILES:
        assert_file(skill / relative)
    for path in skill.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    orb_main = (skill / "scripts" / "orb" / "main.cjs").read_text(encoding="utf-8")
    orb_preload = (skill / "scripts" / "orb" / "preload.cjs").read_text(encoding="utf-8")
    if "resizable: true" not in orb_main or "minWidth: MIN_SIZE" not in orb_main:
        raise SystemExit("Orb window resize configuration is incomplete")
    if "orb-resize-start" not in orb_main or "orb-resize" not in orb_preload:
        raise SystemExit("Orb resize gesture bridge is incomplete")
    if "focusRendererForInteraction" not in orb_main or "setIgnoreMouseEvents(false" not in orb_main:
        raise SystemExit("Orb Linux interaction focus bridge is incomplete")
    if "function interactionRenderers()" not in orb_main or "function applyShortcutModes(renderer)" not in orb_main:
        raise SystemExit("Orb multi-renderer interaction bridge is incomplete")
    if (
        "WAYLAND_INTERACTION_SELECTION" not in orb_main
        or "function transitionInteractionMachine" not in orb_main
        or "selected_key: interactionMachine.selectedKey" not in orb_main
        or "skipTaskbar: !WAYLAND_INTERACTION_SELECTION" not in orb_main
    ):
        raise SystemExit("Orb Wayland compositor-focus selection state machine is incomplete")
    orb_styles = (skill / "scripts" / "orb" / "styles.css").read_text(encoding="utf-8")
    if "body.move-mode::before" not in orb_styles or "body.resize-mode::before" not in orb_styles:
        raise SystemExit("Orb interaction mode border is incomplete")
    if "activeAvatar?.stateSupported" not in orb_main or 'send("window-resize", { width, height });' not in orb_main:
        raise SystemExit("Avatar-local resize contract is incomplete")
    skill_text = (skill / "SKILL.md").read_text(encoding="utf-8")
    if "name: codex-voice" not in skill_text or "configure.py" not in skill_text:
        raise SystemExit("Skill metadata/configuration instructions are incomplete")
    setup_text = (skill / "scripts" / "setup.py").read_text(encoding="utf-8")
    if '"--refresh"' not in setup_text or "install_managed_runtime_files" not in setup_text:
        raise SystemExit("Managed runtime refresh seam is incomplete")

    sys.path.insert(0, str(skill / "scripts"))
    from activity import classify_activity
    from setup import (
        ensure_gitignore,
        install_activity_script,
        install_profile_script,
        install_runtime_manifest,
        install_tui_bridge,
        install_tui_runtime,
    )
    from tui_bridge import MockKokoroWorker, VoiceChunkRouter
    from uninstall import read_runtime_manifest

    manifest_text = (skill / "RUNTIME-MANIFEST.md").read_text(encoding="utf-8")
    for required_entry in (
        ".codex-voice/activity.py",
        ".codex-voice/tui_bridge.py",
        ".codex-voice/launch_codex.py",
        ".codex-voice/launch_codex.sh",
        ".codex-voice/tui_kokoro_worker.py",
        ".codex-voice/presence_service.py",
        ".codex-voice/presence-profiles.json",
        ".codex-voice/orb/",
        ".codex/hooks/speak.py",
    ):
        if required_entry not in manifest_text:
            raise SystemExit(f"Runtime manifest omitted required artifact: {required_entry}")

    activity_cases = (
        ({"type": "event_msg", "payload": {"type": "agent_reasoning"}}, "thinking"),
        ({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec"}}, "cli"),
        ({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "other"}}, "tool"),
        ({"type": "response_item", "payload": {"type": "custom_tool_call_output"}}, "thinking"),
        ({"type": "event_msg", "payload": {"type": "patch_apply_end"}}, "cli"),
        ({"type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer"}}, "idle"),
    )
    for record, expected in activity_cases:
        if classify_activity(record) != expected:
            raise SystemExit(f"Activity classification failed: {record!r}")
    reasoning_record = {
        "type": "response_item",
        "payload": {"type": "reasoning", "text": "hidden reasoning must not cross the bridge"},
    }
    if classify_activity(reasoning_record) != "thinking":
        raise SystemExit("Reasoning activity metadata was not reduced to the category-only thinking state")
    worker = MockKokoroWorker()
    router = VoiceChunkRouter(worker)
    if not router.handle({"type": "voice/chunk", "stream_id": "e2e", "text": "visible", "sequence": 1}):
        raise SystemExit("TUI bridge did not route a visible mock chunk")
    if [event["type"] for event in worker.events] != ["start", "delta"]:
        raise SystemExit("TUI bridge mock worker contract was not preserved")
    if (skill / "html").exists() or (skill / "media").exists():
        raise SystemExit("Release skill still contains showcase media")

    with tempfile.TemporaryDirectory(prefix="codex-voice-e2e-") as temporary:
        root = Path(temporary)
        project = root / "project"
        scripts = project / "scripts"
        voice_root = project / ".codex-voice"
        scripts.mkdir(parents=True)
        voice_root.mkdir()
        (voice_root / ".gitignore").write_text(".venv/\nsessions.json\n", encoding="utf-8")
        ensure_gitignore(voice_root / ".gitignore")
        ignore_lines = (voice_root / ".gitignore").read_text(encoding="utf-8").splitlines()
        for required_pattern in (".stt-venv/", "input.json", "presence-profiles.json"):
            if required_pattern not in ignore_lines:
                raise SystemExit(f"Setup upgrade omitted runtime ignore pattern: {required_pattern}")
        if ignore_lines.count(".venv/") != 1 or ignore_lines.count("sessions.json") != 1:
            raise SystemExit("Setup upgrade duplicated existing runtime ignore patterns")
        install_activity_script(voice_root)
        install_tui_bridge(voice_root)
        install_tui_runtime(voice_root)
        install_profile_script(voice_root)
        install_runtime_manifest(voice_root)
        if not (voice_root / "activity.py").is_file():
            raise SystemExit("Setup did not install the activity bridge into the project runtime")
        if not (voice_root / "tui_bridge.py").is_file():
            raise SystemExit("Setup did not install the TUI/server bridge into the project runtime")
        if not all(
            (voice_root / name).is_file()
            for name in ("launch_codex.py", "launch_codex.sh", "tui_kokoro_worker.py")
        ):
            raise SystemExit("Setup did not install the stock TUI launcher runtime")
        if not (voice_root / "profiles.py").is_file() or not (voice_root / "configuration.py").is_file():
            raise SystemExit("Setup did not install the profile resolver into the project runtime")
        manifest_entries = read_runtime_manifest(voice_root)
        if manifest_entries is None or ".codex-voice/activity.py" not in manifest_entries:
            raise SystemExit("Setup did not install a readable runtime manifest")
        if ".codex-voice/tui_bridge.py" not in manifest_entries:
            raise SystemExit("Setup did not register the TUI/server bridge in the runtime manifest")
        for entry in (
            ".codex-voice/launch_codex.py",
            ".codex-voice/launch_codex.sh",
            ".codex-voice/tui_kokoro_worker.py",
        ):
            if entry not in manifest_entries:
                raise SystemExit(f"Setup did not register {entry} in the runtime manifest")
        for path in (skill / "scripts").glob("*.py"):
            shutil.copy2(path, scripts / path.name)
        (voice_root / "sessions.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mode": "session",
                    "sessions": {
                        "session-luna": {
                            "enabled": True,
                            "project_root": str(project.resolve()),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        configure = scripts / "configure.py"
        toggle = scripts / "toggle.py"
        run([sys.executable, str(configure), "show"], project)
        result = run(
            [
                sys.executable,
                str(configure),
                "set",
                "--voice",
                "bf_isabella",
                "--speed",
                "1.08",
                "--mode",
                "quality",
                "--provider",
                "cpu",
                "--volume",
                "60",
                "--commentary-volume",
                "100",
                "--progress",
                "on",
                "--orb",
                "off",
                "--scope",
                "off",
            ],
            project,
        )
        if "commentary volume: 100%" not in result.stdout:
            raise SystemExit("Configuration output did not report commentary volume")
        expected = {
            "voice": "bf_isabella",
            "speed": "1.08",
            "mode": "quality",
            "provider": "cpu",
            "volume": "60",
            "commentary-volume": "100",
        }
        for name, value in expected.items():
            actual = (voice_root / name).read_text(encoding="utf-8").strip()
            if actual != value:
                raise SystemExit(f"{name} persisted as {actual!r}, expected {value!r}")
        if not (voice_root / "progress").is_file() or (voice_root / "orb.enabled").exists():
            raise SystemExit("Progress or Orb marker state was not persisted safely")
        status = run([sys.executable, str(toggle), "status"], project)
        if "commentary volume: 100%" not in status.stdout:
            raise SystemExit("toggle status omitted commentary volume")
        run([sys.executable, str(configure), "set", "--speed", "3"], project, expect=2)
        run(
            [
                sys.executable,
                str(voice_root / "profiles.py"),
                "--project-root",
                str(project),
                "set",
                "luna",
                "--avatar-id",
                "builtin",
                "--voice",
                "af_heart",
                "--speed",
                "1.2",
            ],
            project,
        )
        run(
            [
                sys.executable,
                str(voice_root / "profiles.py"),
                "--project-root",
                str(project),
                "bind",
                "session-luna",
                "luna",
            ],
            project,
        )
        resolved_profile = run(
            [
                sys.executable,
                str(voice_root / "profiles.py"),
                "--project-root",
                str(project),
                "resolve",
                "--session-id",
                "session-luna",
            ],
            project,
        )
        if '"profile_id": "luna"' not in resolved_profile.stdout:
            raise SystemExit("Session presence profile did not resolve through the installed runtime")
        run([sys.executable, str(skill / "scripts" / "setup.py"), "--help"], project)

        refresh_project = root / "refresh-project"
        refresh_voice = refresh_project / ".codex-voice"
        refresh_voice.mkdir(parents=True)
        (refresh_voice / "provider").write_text("openvino\n", encoding="utf-8")
        (refresh_voice / "watcher.py").write_text("obsolete\n", encoding="utf-8")
        run(
            [
                sys.executable,
                str(skill / "scripts" / "setup.py"),
                "--project-root",
                str(refresh_project),
                "--refresh",
                "--no-orb",
                "--force",
            ],
            refresh_project,
        )
        if (refresh_voice / "watcher.py").exists():
            raise SystemExit("Runtime refresh retained an obsolete managed watcher copy")
        if (refresh_voice / "provider").read_text(encoding="utf-8").strip() != "openvino":
            raise SystemExit("Runtime refresh changed the selected provider")

        hooks_dir = project / ".codex" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "speak.py"
        shutil.copy2(skill / "scripts" / "speak.py", hook)
        hooks_path = project / ".codex" / "hooks.json"
        python_path = voice_root / ".venv" / "Scripts" / "python.exe"
        command = f'"{python_path}" "{hook}"'
        hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": command,
                                        "commandWindows": command,
                                        "statusMessage": "Speaking Codex response",
                                    }
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        run([sys.executable, str(skill / "scripts" / "uninstall.py"), "--project-root", str(project), "--yes"], project)
        if voice_root.exists() or hook.exists():
            raise SystemExit("Uninstaller did not remove the project-local integration")
        remaining_hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
        if remaining_hooks.get("hooks", {}).get("Stop"):
            print(json.dumps(remaining_hooks, indent=2))
            raise SystemExit("Uninstaller left the managed Stop hook registered")

    print("Codex AI Presence E2E gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
