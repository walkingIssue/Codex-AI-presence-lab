"""Adapt TUI bridge packets to the persistent project-local Kokoro worker."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def log(voice_root: Path, message: str) -> None:
    try:
        with (voice_root / "bridge.log").open("a", encoding="utf-8") as handle:
            handle.write(f"tui-kokoro: {message}\n")
    except OSError:
        pass


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def runtime_provider(voice_root: Path) -> str:
    try:
        return (voice_root / "provider").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return "cpu"


def cpu_python(voice_root: Path) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    directory = "Scripts" if os.name == "nt" else "bin"
    return voice_root / ".venv" / directory / executable


class PersistentHook:
    def __init__(self, project_root: Path, voice_root: Path) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> bool:
        hook = self.project_root / ".codex" / "hooks" / "speak.py"
        selected_python = Path(sys.executable)
        if not selected_python.is_file() or not hook.is_file():
            log(self.voice_root, "speak.py or selected Python runtime is missing")
            return False
        attempts: list[tuple[Path, dict[str, str]]] = [(selected_python, {})]
        if runtime_provider(self.voice_root) == "openvino":
            fallback = cpu_python(self.voice_root)
            different_runtime = fallback.absolute() != selected_python.absolute()
            if fallback.is_file() and different_runtime:
                attempts.append((fallback, {"CODEX_TTS_PROVIDER": "cpu"}))

        for index, (python, overrides) in enumerate(attempts):
            environment = os.environ.copy()
            environment.pop("CODEX_TTS_DISABLE", None)
            environment.update(overrides)
            environment["CODEX_TTS_FROM_WATCHER"] = "1"
            environment["CODEX_TTS_FROM_BRIDGE"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            provider = overrides.get("CODEX_TTS_PROVIDER", runtime_provider(self.voice_root))
            log(self.voice_root, f"persistent hook starting provider={provider} python={python}")
            try:
                self.process = subprocess.Popen(
                    [str(python), str(hook), "--server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    cwd=str(self.project_root),
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                line = self.process.stdout.readline() if self.process.stdout is not None else ""
                ready = json.loads(line).get("ready") if line else False
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                log(self.voice_root, f"persistent hook start failed: {type(exc).__name__}")
                ready = False
            if ready:
                log(self.voice_root, f"persistent hook ready provider={provider}")
                return True
            self.close()
            if index + 1 < len(attempts):
                log(self.voice_root, "persistent hook provider failed; retrying with CPU Kokoro")

        log(self.voice_root, "persistent hook did not preload Kokoro")
        return False

    def send(self, text: str, event_id: str) -> bool:
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None or process.stdout is None:
            return False
        request = {
            "hook_event_name": "Stop",
            "last_assistant_message": text,
            "tts_event_id": event_id,
        }
        try:
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
        except (BrokenPipeError, OSError, ValueError, json.JSONDecodeError):
            return False
        return bool(response.get("done") and response.get("ok"))

    def stop_playback(self) -> None:
        try:
            (self.voice_root / "tts-stop.request").write_text("stop\n", encoding="utf-8")
        except OSError:
            pass

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def run(project_root: Path, voice_root: Path) -> int:
    voice_root.mkdir(parents=True, exist_ok=True)
    hook = PersistentHook(project_root, voice_root)
    if not hook.start():
        emit({"event": "ready", "ok": False})
        return 2
    emit({"event": "ready", "ok": True})
    active_stream: str | None = None
    text_parts: list[str] = []
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                emit({"event": "error", "ok": False, "reason": "invalid_json"})
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            stream_id = event.get("stream_id")
            stream_id = stream_id if isinstance(stream_id, str) and stream_id else None
            if event_type == "start":
                if active_stream is not None and active_stream != stream_id:
                    hook.stop_playback()
                active_stream = stream_id
                text_parts = []
                continue
            if event_type == "delta":
                if active_stream is None:
                    active_stream = stream_id
                if stream_id == active_stream:
                    text = event.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
                continue
            if event_type == "cancel":
                if active_stream is None or stream_id == active_stream:
                    hook.stop_playback()
                    active_stream = None
                    text_parts = []
                continue
            if event_type == "finish":
                if active_stream is None or stream_id != active_stream:
                    continue
                text = "".join(text_parts).strip()
                current = active_stream
                active_stream = None
                text_parts = []
                if not text:
                    emit({"event": "done", "ok": True, "stream_id": current})
                    continue
                ok = hook.send(text, current)
                emit({"event": "done", "ok": ok, "stream_id": current})
                continue
            if event_type == "shutdown":
                break
    finally:
        hook.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--voice-root", type=Path, required=True)
    args = parser.parse_args()
    return run(args.project_root.resolve(), args.voice_root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
