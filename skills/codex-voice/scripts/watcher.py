"""Bridge Codex Desktop transcript messages to the project-local Kokoro player."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from configuration import configured_commentary_volume
from session_scope import (
    is_project_mode,
    load_state,
    registered_session_ids,
    state_path,
)


POLL_SECONDS = 0.4


def log(voice_root: Path, message: str) -> None:
    try:
        with (voice_root / "watcher.log").open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def normal_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


_SESSION_METADATA_CACHE: dict[Path, tuple[int, str | None, str | None]] = {}


def session_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, None
    cached = _SESSION_METADATA_CACHE.get(path)
    if cached is not None and cached[0] == size:
        return cached[1], cached[2]
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = json.loads(handle.readline())
        payload = first.get("payload", {})
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        session_id = payload.get("session_id", payload.get("id")) if isinstance(payload, dict) else None
        cwd = cwd if isinstance(cwd, str) else None
        session_id = session_id if isinstance(session_id, str) else None
    except (OSError, json.JSONDecodeError, AttributeError):
        cwd, session_id = None, None
    _SESSION_METADATA_CACHE[path] = (size, cwd, session_id)
    return cwd, session_id


def is_project_session(
    path: Path,
    project_root: Path,
    allowed_session_ids: set[str] | None = None,
) -> bool:
    cwd, session_id = session_metadata(path)
    if not isinstance(cwd, str) or normal_path(cwd) != normal_path(project_root):
        return False
    return allowed_session_ids is None or session_id in allowed_session_ids


def session_files(project_root: Path, scope_state: dict) -> list[Path]:
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return []
    allowed_session_ids = registered_session_ids(scope_state)
    if is_project_mode(scope_state):
        paths = root.rglob("rollout-*.jsonl")
        allowed_session_ids = None
    else:
        if not allowed_session_ids:
            return []
        paths = (
            path
            for thread_id in allowed_session_ids
            for path in root.rglob(f"rollout-*-{thread_id}.jsonl")
        )
    candidates: dict[Path, None] = {}
    for path in paths:
        if path.is_file() and is_project_session(path, project_root, allowed_session_ids):
            candidates[path] = None
    return list(candidates)


def timestamp_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def final_message(record: dict) -> str | None:
    return agent_message(record, "final_answer")


def commentary_message(record: dict) -> str | None:
    """Return only visible Codex progress commentary.

    This deliberately does not inspect reasoning or tool-output records.
    """
    return agent_message(record, "commentary")


def agent_message(record: dict, phase: str) -> str | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "agent_message" or payload.get("phase") != phase:
        return None
    message = payload.get("message")
    return message if isinstance(message, str) and message.strip() else None


def progress_enabled(voice_root: Path) -> bool:
    try:
        value = voice_root.joinpath("progress").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return value in {"1", "true", "on", "enabled"}


def configured_volume(voice_root: Path) -> int:
    try:
        environment_volume = os.environ.get("CODEX_TTS_VOLUME")
        if environment_volume:
            value = int(environment_volume)
        else:
            value = int(voice_root.joinpath("volume").read_text(encoding="utf-8").strip())
    except ValueError:
        value = 20
    except OSError:
        value = 20
    return max(0, min(100, value))


def configured_provider(voice_root: Path) -> str:
    provider = os.environ.get("CODEX_TTS_PROVIDER", "").strip().lower()
    if not provider:
        try:
            provider = voice_root.joinpath("provider").read_text(encoding="utf-8").strip().lower()
        except OSError:
            provider = ""
    if provider in {"cuda", "cudaexecutionprovider", "nvidia", "nvidia-cuda"}:
        return "cuda"
    return "directml" if provider in {"directml", "dml", "gpu"} else "cpu"


def runtime_for_provider(voice_root: Path) -> tuple[Path, str]:
    """Select the interpreter and model family for the project provider."""
    provider = configured_provider(voice_root)
    cpu_python = voice_root / ".venv" / "Scripts" / "python.exe"
    if provider == "cuda":
        cuda_python = voice_root / ".cuda-venv" / "Scripts" / "python.exe"
        model = voice_root / "kokoro-v1.0.int8.onnx"
        if cuda_python.is_file() and model.is_file():
            return cuda_python, provider
        log(voice_root, "CUDA provider requested but .cuda-venv or the base model is missing; using CPU")
        return cpu_python, "cpu"
    if provider == "directml":
        dml_python = voice_root / ".dml-venv" / "Scripts" / "python.exe"
        dml_model = voice_root / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx"
        if dml_python.is_file() and dml_model.is_file():
            return dml_python, provider
        log(voice_root, "DirectML provider requested but patched runtime/model is missing; using CPU")
    return cpu_python, "cpu"


class TTSWorker:
    """Keep one Kokoro process/model alive for sequential watcher requests."""

    def __init__(self, project_root: Path, voice_root: Path) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.process: subprocess.Popen[str] | None = None
        self.ready = False

    def start(self) -> bool:
        if self.process is not None:
            if self.process.poll() is not None:
                self.close()
            elif self.ready:
                return True
            else:
                return self._read_ready()

        python, provider = runtime_for_provider(self.voice_root)
        hook = self.project_root / ".codex" / "hooks" / "speak.py"
        if not python.is_file() or not hook.is_file():
            log(self.voice_root, "persistent worker skipped: runtime or speak.py missing")
            return False

        environment = os.environ.copy()
        environment["CODEX_TTS_FROM_WATCHER"] = "1"
        environment["CODEX_TTS_PROVIDER"] = provider
        log(self.voice_root, f"starting persistent TTS worker provider={provider}")
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
        except OSError as exc:
            log(self.voice_root, f"persistent worker start error: {type(exc).__name__}")
            self.process = None
            return False
        return self._read_ready()

    def _read_ready(self) -> bool:
        if self.process is None or self.process.stdout is None:
            return False
        try:
            line = self.process.stdout.readline()
            response = json.loads(line) if line else {}
        except (OSError, json.JSONDecodeError):
            response = {}
        self.ready = bool(response.get("ready"))
        if self.ready:
            log(self.voice_root, "persistent TTS worker ready")
        else:
            log(self.voice_root, "persistent TTS worker failed to preload")
            self.close()
        return self.ready

    def send(self, message: str, volume: int | None = None) -> bool:
        if not self.start() or self.process is None:
            return False
        if self.process.stdin is None or self.process.stdout is None:
            return False
        request: dict[str, object] = {
            "hook_event_name": "Stop",
            "last_assistant_message": message,
        }
        if volume is not None:
            request["tts_volume"] = max(0, min(100, volume))
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            response = json.loads(line) if line else {}
            if response.get("done") and response.get("ok"):
                return True
            log(self.voice_root, "persistent worker returned an unsuccessful response")
        except (OSError, json.JSONDecodeError) as exc:
            log(self.voice_root, f"persistent worker request error: {type(exc).__name__}")
        self.close()
        return False

    def close(self) -> None:
        process = self.process
        self.process = None
        self.ready = False
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def speak(
    project_root: Path,
    voice_root: Path,
    message: str,
    *,
    volume: int | None = None,
    label: str = "final answer",
    worker: TTSWorker | None = None,
) -> None:
    python, provider = runtime_for_provider(voice_root)
    hook = project_root / ".codex" / "hooks" / "speak.py"
    if not python.is_file() or not hook.is_file():
        log(voice_root, "skipped: voice runtime or speak.py missing")
        return

    if worker is not None and worker.send(message, volume=volume):
        log(voice_root, f"{label} persistent worker returned")
        return

    payload = json.dumps({
        "hook_event_name": "Stop",
        "last_assistant_message": message,
    })
    environment = os.environ.copy()
    environment["CODEX_TTS_FROM_WATCHER"] = "1"
    environment["CODEX_TTS_PROVIDER"] = provider
    if volume is not None:
        environment["CODEX_TTS_VOLUME"] = str(max(0, min(100, volume)))
    try:
        subprocess.run(
            [str(python), str(hook)],
            input=payload,
            text=True,
            cwd=str(project_root),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        log(voice_root, f"{label} speak process returned")
    except OSError as exc:
        log(voice_root, f"speak process error: {type(exc).__name__}")


def read_new_records(path: Path, offset: int, partial: bytes) -> tuple[int, bytes, list[dict]]:
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
            partial = b""
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = partial + handle.read()
            new_offset = handle.tell()
    except OSError:
        return offset, partial, []

    lines = chunk.splitlines(keepends=True)
    if lines and not lines[-1].endswith((b"\n", b"\r")):
        partial = lines.pop()
    else:
        partial = b""

    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return new_offset, partial, records


def marker_enabled(voice_root: Path) -> bool:
    try:
        return voice_root.joinpath("enabled").read_text(encoding="utf-8").strip().lower() in {
            "1",
            "true",
            "on",
            "enabled",
        }
    except OSError:
        return False


def scope_mtime(voice_root: Path) -> int | None:
    try:
        return state_path(voice_root).stat().st_mtime_ns
    except OSError:
        return None


def initial_offset(path: Path, start_time: float) -> int:
    try:
        stat = path.stat()
    except OSError:
        return 0
    # Existing sessions begin at their current end. New rollouts, or a file
    # updated after activation, are read from the beginning and filtered by
    # record timestamp so activation never replays old responses.
    return stat.st_size if stat.st_mtime < start_time else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--voice-root", required=True, type=Path)
    parser.add_argument("--start-time", required=True, type=float)
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    voice_root = args.voice_root.resolve()
    pid_path = voice_root / "watcher.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    log(voice_root, f"started for {project_root}")
    log(voice_root, f"visible progress: {'on' if progress_enabled(voice_root) else 'off'}")
    log(voice_root, f"provider: {configured_provider(voice_root)}")

    scope_state = load_state(voice_root)
    scope_state_mtime = scope_mtime(voice_root)
    log(
        voice_root,
        f"scope: {'project' if is_project_mode(scope_state) else 'session'} "
        f"({len(registered_session_ids(scope_state))} registered sessions)",
    )
    streams: dict[Path, tuple[int, bytes]] = {}
    announced: set[Path] = set()
    seen: set[tuple[str, str, str, str]] = set()
    worker = TTSWorker(project_root, voice_root)
    worker.start()

    try:
        while marker_enabled(voice_root):
            current_scope_mtime = scope_mtime(voice_root)
            if current_scope_mtime != scope_state_mtime:
                scope_state = load_state(voice_root)
                scope_state_mtime = current_scope_mtime
                streams.clear()
                announced.clear()
                log(
                    voice_root,
                    f"scope changed: {'project' if is_project_mode(scope_state) else 'session'} "
                    f"({len(registered_session_ids(scope_state))} registered sessions)",
                )

            candidates = session_files(project_root, scope_state)
            candidate_set = set(candidates)
            for path in list(streams):
                if path not in candidate_set:
                    streams.pop(path, None)
                    announced.discard(path)

            events: list[tuple[float | None, Path, dict]] = []
            for path in sorted(candidates, key=str):
                if path not in streams:
                    streams[path] = (initial_offset(path, args.start_time), b"")
                    if path not in announced:
                        announced.add(path)
                        log(voice_root, f"watching {path.name}")
                offset, partial = streams[path]
                offset, partial, records = read_new_records(path, offset, partial)
                streams[path] = (offset, partial)
                for record in records:
                    events.append((timestamp_seconds(record.get("timestamp")), path, record))

            events.sort(
                key=lambda item: (
                    item[0] is None,
                    item[0] if item[0] is not None else 0,
                    str(item[1]),
                )
            )
            for record_time, path, record in events:
                if record_time is not None and record_time < args.start_time:
                    continue
                commentary = commentary_message(record) if progress_enabled(voice_root) else None
                if commentary is not None:
                    key = (str(path), str(record.get("timestamp")), "commentary", commentary)
                    if key not in seen:
                        seen.add(key)
                        commentary_ratio = configured_commentary_volume(voice_root) / 100
                        volume = round(configured_volume(voice_root) * commentary_ratio)
                        log(voice_root, f"speaking visible commentary at {volume}%: {len(commentary)} characters")
                        speak(
                            project_root,
                            voice_root,
                            commentary,
                            volume=volume,
                            label="commentary",
                            worker=worker,
                        )
                    continue

                message = final_message(record)
                if message is None:
                    continue
                key = (str(path), str(record.get("timestamp")), "final_answer", message)
                if key in seen:
                    continue
                seen.add(key)
                log(voice_root, f"speaking final answer: {len(message)} characters")
                speak(project_root, voice_root, message, worker=worker)
            time.sleep(POLL_SECONDS)
        log(voice_root, "stopping: voice marker is off")
    except KeyboardInterrupt:
        log(voice_root, "stopping: keyboard interrupt")
    except Exception as exc:
        log(voice_root, f"crashed: {type(exc).__name__}: {exc}")
    finally:
        worker.close()
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        log(voice_root, "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
