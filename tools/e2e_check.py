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
    "scripts/setup.py",
    "scripts/session_scope.py",
    "scripts/speak.py",
    "scripts/toggle.py",
    "scripts/uninstall.py",
    "scripts/watcher.py",
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
    if "activeAvatar?.stateSupported" not in orb_main or 'send("window-resize", { width, height });' not in orb_main:
        raise SystemExit("Avatar-local resize contract is incomplete")
    skill_text = (skill / "SKILL.md").read_text(encoding="utf-8")
    if "name: codex-voice" not in skill_text or "configure.py" not in skill_text:
        raise SystemExit("Skill metadata/configuration instructions are incomplete")

    sys.path.insert(0, str(skill / "scripts"))
    from activity import classify_activity
    from setup import install_activity_script, install_runtime_manifest
    from uninstall import read_runtime_manifest

    manifest_text = (skill / "RUNTIME-MANIFEST.md").read_text(encoding="utf-8")
    for required_entry in (".codex-voice/activity.py", ".codex-voice/orb/", ".codex/hooks/speak.py"):
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
    if classify_activity({"type": "response_item", "payload": {"type": "reasoning"}}) is not None:
        raise SystemExit("Hidden reasoning content was incorrectly exposed as an activity event")
    if (skill / "html").exists() or (skill / "media").exists():
        raise SystemExit("Release skill still contains showcase media")

    with tempfile.TemporaryDirectory(prefix="codex-voice-e2e-") as temporary:
        root = Path(temporary)
        project = root / "project"
        scripts = project / "scripts"
        voice_root = project / ".codex-voice"
        scripts.mkdir(parents=True)
        voice_root.mkdir()
        install_activity_script(voice_root)
        install_runtime_manifest(voice_root)
        if not (voice_root / "activity.py").is_file():
            raise SystemExit("Setup did not install the activity bridge into the project runtime")
        manifest_entries = read_runtime_manifest(voice_root)
        if manifest_entries is None or ".codex-voice/activity.py" not in manifest_entries:
            raise SystemExit("Setup did not install a readable runtime manifest")
        for path in (skill / "scripts").glob("*.py"):
            shutil.copy2(path, scripts / path.name)
        (voice_root / "sessions.json").write_text(
            json.dumps({"version": 1, "mode": "session", "sessions": {}}),
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
        run([sys.executable, str(skill / "scripts" / "setup.py"), "--help"], project)

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
