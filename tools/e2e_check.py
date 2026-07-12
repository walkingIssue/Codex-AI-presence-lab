"""Run the safe, no-model E2E gate for a projected Codex voice skill."""

from __future__ import annotations

import argparse
import ast
import json
import os
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
    "scripts/app_server_bridge.py",
    "scripts/app_server_launcher.py",
    "scripts/launch_codex.ps1",
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
    skill_text = (skill / "SKILL.md").read_text(encoding="utf-8")
    if "name: codex-voice" not in skill_text or "configure.py" not in skill_text:
        raise SystemExit("Skill metadata/configuration instructions are incomplete")

    sys.path.insert(0, str(skill / "scripts"))
    from activity import classify_activity
    from app_server_bridge import activity_for_item
    from app_server_launcher import bridge_environment, client_arguments, websocket_accept
    from setup import (
        install_activity_script,
        install_app_server_bridge,
        install_app_server_launcher,
        install_runtime_manifest,
    )
    from speak import SpeechChunker
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
    if activity_for_item({"type": "commandExecution"}) != "cli":
        raise SystemExit("App-server command execution was not classified as local CLI activity")
    if activity_for_item({"type": "mcpToolCall"}) != "tool":
        raise SystemExit("App-server MCP activity was not classified as tool activity")
    if client_arguments("codex --remote {remote}", "codex", "ws://127.0.0.1:1234") != [
        "codex",
        "--remote",
        "ws://127.0.0.1:1234",
    ]:
        raise SystemExit("Stock Codex client launcher arguments were not expanded safely")
    if os.name == "nt":
        windows_client = client_arguments(
            '"{codex}" resume --remote {remote} thread-id',
            r"C:\Program Files\OpenAI\Codex\codex.exe",
            "ws://127.0.0.1:1234",
        )
        if windows_client != [
            r"C:\Program Files\OpenAI\Codex\codex.exe",
            "resume",
            "--remote",
            "ws://127.0.0.1:1234",
            "thread-id",
        ]:
            raise SystemExit("Windows Codex client path was not preserved")
    if bridge_environment().get("CODEX_TTS_DISABLE") != "1":
        raise SystemExit("Bridged Codex processes did not disable the duplicate Stop hook")
    manifest_text = (skill / "RUNTIME-MANIFEST.md").read_text(encoding="utf-8")
    if ".codex-voice/bridge.active" not in manifest_text:
        raise SystemExit("Bridge activity marker was not registered in the runtime manifest")
    if websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") != "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=":
        raise SystemExit("WebSocket handshake accept key calculation failed")
    chunker = SpeechChunker(min_chars=10, max_chars=80)
    streamed_chunks = chunker.add("The first sentence arrives in deltas.")
    streamed_chunks.extend(chunker.add(" The second sentence is still coming"))
    streamed_chunks.extend(chunker.finish())
    if len(streamed_chunks) != 2 or not streamed_chunks[0].startswith("The first sentence"):
        raise SystemExit(f"Incremental speech chunking failed: {streamed_chunks!r}")
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
        install_app_server_bridge(voice_root)
        install_app_server_launcher(voice_root)
        install_runtime_manifest(voice_root)
        if not (voice_root / "activity.py").is_file():
            raise SystemExit("Setup did not install the activity bridge into the project runtime")
        if not (voice_root / "app_server_bridge.py").is_file():
            raise SystemExit("Setup did not install the app-server bridge into the project runtime")
        if not (voice_root / "app_server_launcher.py").is_file() or not (voice_root / "launch_codex.ps1").is_file():
            raise SystemExit("Setup did not install the stock-client launcher into the project runtime")
        manifest_entries = read_runtime_manifest(voice_root)
        if manifest_entries is None or ".codex-voice/activity.py" not in manifest_entries:
            raise SystemExit("Setup did not install a readable runtime manifest")
        if ".codex-voice/app_server_bridge.py" not in manifest_entries:
            raise SystemExit("Setup did not register the app-server bridge in the runtime manifest")
        for required in (".codex-voice/app_server_launcher.py", ".codex-voice/launch_codex.ps1"):
            if required not in manifest_entries:
                raise SystemExit(f"Setup did not register {required} in the runtime manifest")
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
        run([sys.executable, str(skill / "scripts" / "app_server_launcher.py"), "--help"], project)

        fake_upstream = root / "fake_app_server.py"
        fake_upstream.write_text(
            """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({\"id\": request.get(\"id\"), \"result\": {\"ok\": True}}), flush=True)
    print(json.dumps({\"method\": \"item/agentMessage/delta\", \"params\": {\"threadId\": \"thread-1\", \"turnId\": \"turn-1\", \"itemId\": \"item-1\", \"delta\": \"hello\"}}), flush=True)
    print(json.dumps({\"method\": \"turn/completed\", \"params\": {\"threadId\": \"thread-1\", \"turn\": {\"id\": \"turn-1\"}}}), flush=True)
    break
""".strip()
            + "\n",
            encoding="utf-8",
        )
        upstream_command = f'"{sys.executable}" "{fake_upstream}"'
        bridge_result = subprocess.run(
            [
                sys.executable,
                str(skill / "scripts" / "app_server_bridge.py"),
                "--project-root",
                str(project),
                "--upstream-command",
                upstream_command,
                "--no-voice",
                "--no-activity",
            ],
            input=json.dumps({"id": 1, "method": "initialize", "params": {}}) + "\n",
            cwd=project,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if bridge_result.returncode != 0:
            raise SystemExit(f"App-server bridge smoke failed: {bridge_result.stderr}")
        forwarded = [json.loads(line) for line in bridge_result.stdout.splitlines() if line.strip()]
        if len(forwarded) != 3 or forwarded[0].get("id") != 1:
            raise SystemExit(f"App-server bridge did not preserve upstream messages: {forwarded!r}")
        if forwarded[1].get("method") != "item/agentMessage/delta":
            raise SystemExit("App-server bridge did not forward agent message deltas")

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
