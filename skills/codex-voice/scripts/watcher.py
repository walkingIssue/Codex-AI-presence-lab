"""Bridge Codex Desktop transcript messages to the project-local Kokoro player."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from activity import classify_activity, orb_port_for_root, state_ttl_seconds
from clipboard import ClipboardError, copy_text
from configuration import configured_commentary_volume
from delivery import AppServerClient, DeliveryError, resolve_session_label
from global_arbiter import GlobalPlaybackArbiter
from inbox import Inbox, database_path, stable_event_id
from presence_service import PresenceService
from session_scope import (
    is_project_mode,
    load_state,
    registered_session_ids,
    state_path,
)
from stt import STTUnavailable


POLL_SECONDS = 0.4
ACTIVITY_HEARTBEAT_SECONDS = 1.25
INPUT_LOCK_TIMEOUT_SECONDS = 120.0
SESSION_LABEL_TEMPLATE = "{session_name} says"


def environment_python(root: Path) -> Path:
    """Return the virtualenv interpreter path for the current platform."""
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def log(voice_root: Path, message: str) -> None:
    try:
        with (voice_root / "watcher.log").open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def emit_orb_event(event: dict[str, object], voice_root: Path | None = None) -> None:
    """Send an optional local-only event; renderers may ignore unknown types."""
    try:
        port = orb_port_for_root(voice_root)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(json.dumps(event, separators=(",", ":")).encode("utf-8"), ("127.0.0.1", port))
    except (OSError, ValueError):
        pass


def discard_orphan_recordings(voice_root: Path) -> int:
    recordings = voice_root / "inbox" / "recordings"
    removed = 0
    try:
        paths = tuple(recordings.glob("*.webm"))
    except OSError:
        return 0
    for path in paths:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def read_tts_progress(voice_root: Path, event_id: str) -> dict[str, object] | None:
    """Read the best-effort playback cursor written by the local TTS worker."""
    path = voice_root / "tts-progress.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or str(payload.get("event_id")) != event_id:
        return None
    return payload


def clear_tts_progress(voice_root: Path, event_id: str) -> None:
    path = voice_root / "tts-progress.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and str(payload.get("event_id")) == event_id:
            path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
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


def record_turn_id(record: dict) -> str | None:
    payload = record.get("payload")
    candidates = [record.get("turn_id"), record.get("turnId")]
    if isinstance(payload, dict):
        candidates.extend((payload.get("turn_id"), payload.get("turnId"), payload.get("id")))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def record_session_label(project_root: Path, session_id: str | None, record: dict) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        for key in ("session_name", "sessionName", "thread_name", "threadName", "title", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    return resolve_session_label(project_root, session_id)


def progress_enabled(voice_root: Path) -> bool:
    try:
        value = voice_root.joinpath("progress").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return value in {"1", "true", "on", "enabled"}


@dataclass
class ActivityLease:
    state: str
    session_id: str | None
    seen_at: float


class ActivityTracker:
    """Aggregate selected rollout activity and keep the Orb state lease alive."""

    def __init__(self, presence: PresenceService) -> None:
        self.presence = presence
        self.leases: dict[Path, ActivityLease] = {}
        self.last_key: tuple[str, str | None, Path | None] | None = None
        self.last_sent_at = 0.0

    def _visible(self, now: float) -> tuple[str, str | None, Path | None, float]:
        for path, lease in list(self.leases.items()):
            if now - lease.seen_at > state_ttl_seconds(lease.state):
                self.leases.pop(path, None)
        if not self.leases:
            return "idle", None, None, 0.0
        path, lease = max(self.leases.items(), key=lambda item: item[1].seen_at)
        return lease.state, lease.session_id, path, lease.seen_at

    def update(self, path: Path, state: str, session_id: str | None, now: float) -> None:
        if state == "idle":
            self.leases.pop(path, None)
        else:
            self.leases[path] = ActivityLease(state, session_id, now)
        self.tick(now, force=True)

    def tick(self, now: float, *, force: bool = False) -> None:
        state, session_id, path, seen_at = self._visible(now)
        key = (state, session_id, path)
        if not force and key == self.last_key and now - self.last_sent_at < ACTIVITY_HEARTBEAT_SECONDS:
            return
        if state == "idle":
            ttl_ms = 0
        else:
            remaining = max(0.5, state_ttl_seconds(state) - (now - seen_at))
            ttl_ms = round(remaining * 1000)
        self.presence.publish_activity(
            state,
            source="codex-rollout",
            session_id=session_id,
            ttl_ms=ttl_ms,
        )
        self.last_key = key
        self.last_sent_at = now

    def reset(self, now: float) -> None:
        self.leases.clear()
        self.tick(now, force=True)

    def close(self, now: float) -> None:
        self.reset(now)


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
    if provider in {"openvino", "openvinoexecutionprovider", "intel", "arc", "arc-openvino"}:
        return "openvino"
    return "directml" if provider in {"directml", "dml", "gpu"} else "cpu"


def runtime_for_provider(voice_root: Path) -> tuple[Path, str]:
    """Select the interpreter and model family for the project provider."""
    provider = configured_provider(voice_root)
    cpu_python = environment_python(voice_root / ".venv")
    if provider == "cuda":
        cuda_python = environment_python(voice_root / ".cuda-venv")
        model = voice_root / "kokoro-v1.0.int8.onnx"
        if cuda_python.is_file() and model.is_file():
            return cuda_python, provider
        log(voice_root, "CUDA provider requested but .cuda-venv or the base model is missing; using CPU")
        return cpu_python, "cpu"
    if provider == "openvino":
        openvino_python = environment_python(voice_root / ".openvino-venv")
        model = voice_root / "kokoro-v1.0.int8.onnx"
        if openvino_python.is_file() and model.is_file():
            return openvino_python, provider
        log(voice_root, "OpenVINO provider requested but .openvino-venv or the base model is missing; using CPU")
        return cpu_python, "cpu"
    if provider == "directml":
        dml_python = environment_python(voice_root / ".dml-venv")
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
                self.close()

        python, provider = runtime_for_provider(self.voice_root)
        hook = self.project_root / ".codex" / "hooks" / "speak.py"
        if not hook.is_file():
            log(self.voice_root, "persistent worker skipped: runtime or speak.py missing")
            return False

        attempts: list[tuple[Path, str]] = [(python, provider)]
        if provider == "openvino":
            cpu = environment_python(self.voice_root / ".venv")
            if cpu.is_file() and cpu.absolute() != python.absolute():
                attempts.append((cpu, "cpu"))

        for index, (selected_python, selected_provider) in enumerate(attempts):
            if not selected_python.is_file():
                continue
            if not self._spawn(selected_python, selected_provider, hook):
                continue
            if self._read_ready():
                return True
            if index + 1 < len(attempts):
                log(self.voice_root, "persistent worker preload failed; retrying with CPU Kokoro")
        return False

    def _spawn(self, python: Path, provider: str, hook: Path) -> bool:
        environment = os.environ.copy()
        environment["CODEX_TTS_FROM_WATCHER"] = "1"
        environment["CODEX_TTS_PROVIDER"] = provider
        environment["CODEX_ORB_PORT"] = str(orb_port_for_root(self.voice_root))
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
        return True

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

    def send(
        self,
        message: str,
        volume: int | None = None,
        *,
        event_id: str | None = None,
        pauseable: bool = False,
        voice: str | None = None,
        speed: float | None = None,
        mode: str | None = None,
    ) -> str:
        if not self.start() or self.process is None:
            return "failed"
        if self.process.stdin is None or self.process.stdout is None:
            return "failed"
        request: dict[str, object] = {
            "hook_event_name": "Stop",
            "last_assistant_message": message,
        }
        if event_id:
            request["tts_event_id"] = event_id
        if pauseable:
            request["tts_pauseable"] = True
        if volume is not None:
            request["tts_volume"] = max(0, min(100, volume))
        if isinstance(voice, str) and voice.strip():
            request["tts_voice"] = voice.strip()
        if speed is not None:
            request["tts_speed"] = max(0.5, min(2.0, float(speed)))
        if mode in {"stream", "quality"}:
            request["tts_mode"] = mode
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            response = json.loads(line) if line else {}
            if response.get("done") and response.get("ok"):
                return "completed"
            if response.get("done") and response.get("interrupted"):
                log(self.voice_root, "persistent worker paused the current item for voice input")
                return "interrupted"
            log(self.voice_root, "persistent worker returned an unsuccessful response")
        except (OSError, json.JSONDecodeError) as exc:
            log(self.voice_root, f"persistent worker request error: {type(exc).__name__}")
        self.close()
        return "failed"

    def close(self) -> None:
        (self.voice_root / "tts-stop.request").unlink(missing_ok=True)
        (self.voice_root / "tts-resume.request").unlink(missing_ok=True)
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

    def request_stop(self) -> None:
        """Ask the active Kokoro player to stop without touching the model cache."""
        stop_path = self.voice_root / "tts-stop.request"
        try:
            stop_path.write_text("stop\n", encoding="utf-8")
        except OSError:
            pass


class STTWorker:
    """Keep one local Whisper model warm and serialize capture requests."""

    def __init__(self, voice_root: Path) -> None:
        self.voice_root = voice_root
        self.process: subprocess.Popen[str] | None = None
        self.ready = False
        self.lock = threading.Lock()

    def _python(self) -> Path:
        scripts = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        candidate = self.voice_root / ".stt-venv" / scripts / executable
        return candidate if candidate.is_file() else Path(sys.executable)

    def start(self) -> bool:
        with self.lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        if self.process is not None:
            if self.process.poll() is None and self.ready:
                return True
            self._close_locked()
        script = self.voice_root / "stt.py"
        if not script.is_file():
            log(self.voice_root, "persistent STT worker skipped: stt.py missing")
            return False
        log(self.voice_root, "starting persistent STT worker")
        try:
            self.process = subprocess.Popen(
                [
                    str(self._python()),
                    str(script),
                    "--server",
                    "--voice-root",
                    str(self.voice_root),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            line = self.process.stdout.readline() if self.process.stdout is not None else ""
            response = json.loads(line) if line else {}
        except (OSError, json.JSONDecodeError) as exc:
            log(self.voice_root, f"persistent STT worker start error: {type(exc).__name__}")
            self._close_locked()
            return False
        self.ready = bool(response.get("ready"))
        if not self.ready:
            detail = response.get("error")
            if isinstance(detail, str) and detail:
                log(self.voice_root, f"persistent STT preload failed: {detail}")
            self._close_locked()
            return False
        log(self.voice_root, "persistent STT worker ready")
        return True

    def transcribe(self, recording: Path, sequence: int) -> str:
        with self.lock:
            if not self._start_locked() or self.process is None:
                raise STTUnavailable("local STT worker could not start")
            if self.process.stdin is None or self.process.stdout is None:
                raise STTUnavailable("local STT worker pipes are unavailable")
            try:
                self.process.stdin.write(
                    json.dumps(
                        {"request_id": sequence, "recording": str(recording)},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                self.process.stdin.flush()
                line = self.process.stdout.readline()
                response = json.loads(line) if line else {}
            except (OSError, json.JSONDecodeError) as exc:
                self._close_locked()
                raise STTUnavailable(f"local STT worker failed: {type(exc).__name__}") from exc
            if response.get("request_id") != sequence:
                raise STTUnavailable("local STT response sequence mismatch")
            if not response.get("ok"):
                detail = response.get("error")
                raise STTUnavailable(str(detail).strip() if detail else "local STT failed")
            transcript = response.get("text")
            if not isinstance(transcript, str) or not transcript.strip():
                raise STTUnavailable("speech was not recognized")
            return transcript.strip()

    def _close_locked(self) -> None:
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

    def close(self) -> None:
        if self.lock.acquire(timeout=2):
            try:
                self._close_locked()
            finally:
                self.lock.release()
            return
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


@dataclass
class QueuedSpeech:
    event_id: str
    message: dict[str, object]


@dataclass(frozen=True)
class TranscriptionJob:
    sequence: int
    recordings: tuple[Path, ...]
    target: str
    resume_event_id: str | None


class PlaybackArbiter:
    """Own the single TTS worker and serialize every speech request."""

    def __init__(self, project_root: Path, voice_root: Path, inbox: Inbox) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.inbox = inbox
        self.worker = TTSWorker(project_root, voice_root)
        self.current: dict[str, object] | None = None
        self.current_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.completed: queue.Queue[dict[str, object]] = queue.Queue()
        self.update_lock = threading.Lock()
        self.pending_update: dict[str, object] | None = None
        self.update_history: deque[str] = deque(maxlen=512)
        self.thread = threading.Thread(target=self._run, name="codex-voice-playback", daemon=True)

    def start(self) -> None:
        self.worker.start()
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            focus = self.inbox.get_state("focus", {})
            focus = focus if isinstance(focus, dict) else {}
            focus_state = str(focus.get("state", "idle"))
            if focus_state in {"listening", "transcribing", "submitting"}:
                message = None
            else:
                focused_session = (
                    focus.get("session_id")
                    if focus_state in {"target-response", "resume-playback"}
                    else None
                )
                message = self.inbox.claim_next(focused_session if isinstance(focused_session, str) else None)
            if message is None:
                message = self._claim_update(focus_state)
                if message is None:
                    self.wake_event.wait(0.15)
                    self.wake_event.clear()
                    continue
            ephemeral_update = self._is_ephemeral_update(message)
            with self.current_lock:
                self.current = message
            session_id = message.get("session_id")
            self.inbox.set_state(
                "playback",
                {
                    "state": "playing",
                    "session_id": session_id,
                    "session_label": message.get("session_label"),
                    "turn_id": message.get("turn_id"),
                    "profile_id": message.get("profile_id"),
                    "avatar_id": message.get("avatar_id"),
                    "route_key": message.get("route_key"),
                    "kind": "commentary" if ephemeral_update else message.get("kind"),
                },
            )
            if not ephemeral_update and isinstance(session_id, str) and session_id:
                self.inbox.set_state("last_session_id", session_id)
            text = self._speech_text(message)
            with self.current_lock:
                if self.current and self.current.get("event_id") == message.get("event_id"):
                    self.current["speech_text"] = text
            emit_orb_event(
                {"type": "voice-output", "state": "playing", "session_id": session_id,
                  "session_label": message.get("session_label"),
                  "profile_id": message.get("profile_id"),
                  "avatar_id": message.get("avatar_id"),
                  "route_key": message.get("route_key"),
                      "kind": "commentary" if ephemeral_update else message.get("kind")},
                self.voice_root,
            )
            try:
                outcome = self.worker.send(
                    text,
                    volume=int(message.get("volume", 100)),
                    event_id=str(message.get("event_id")),
                    pauseable=not ephemeral_update,
                    voice=str(message["tts_voice"]) if message.get("tts_voice") else None,
                    speed=float(message["tts_speed"]) if message.get("tts_speed") is not None else None,
                    mode=str(message["tts_mode"]) if message.get("tts_mode") else None,
                )
                if outcome is True:
                    outcome = "completed"
                elif outcome is False:
                    outcome = "failed"
                if ephemeral_update:
                    clear_tts_progress(self.voice_root, str(message.get("event_id")))
                    if outcome == "interrupted":
                        log(self.voice_root, "ephemeral progress update interrupted by a real message")
                    elif outcome != "completed":
                        log(self.voice_root, "ephemeral progress update dropped after TTS failure")
                elif outcome == "completed":
                    self.inbox.complete(str(message["event_id"]))
                    self.completed.put(message)
                elif outcome == "interrupted":
                    resume_text, resume_offset = self._resume_after_interruption(message)
                    self.inbox.requeue(
                        str(message["event_id"]),
                        delay_seconds=0.0,
                        error="interrupted_for_voice_input",
                        resume_text=resume_text,
                        resume_offset=resume_offset,
                    )
                    clear_tts_progress(self.voice_root, str(message["event_id"]))
                else:
                    self.inbox.requeue(str(message["event_id"]), delay_seconds=0.25, error="tts_worker_failed")
            except Exception as exc:
                self.inbox.requeue(str(message["event_id"]), delay_seconds=0.5, error=type(exc).__name__)
            finally:
                with self.current_lock:
                    if self.current and self.current.get("event_id") == message.get("event_id"):
                        self.current = None
                self.wake_event.set()

    @staticmethod
    def _is_ephemeral_update(message: dict[str, object]) -> bool:
        return bool(message.get("_ephemeral_update"))

    @staticmethod
    def _attention_key(message: dict[str, object]) -> str:
        route_key = message.get("route_key")
        if isinstance(route_key, str) and route_key.strip():
            return route_key.strip()
        session_id = message.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            profile_id = message.get("profile_id")
            profile_key = profile_id.strip() if isinstance(profile_id, str) and profile_id.strip() else "default"
            return f"session:{session_id.strip()}|profile:{profile_key}"
        label = message.get("session_label")
        if isinstance(label, str) and label.strip():
            return f"label:{label.strip()}"
        return "unknown"

    def _attention_matches(self, message: dict[str, object]) -> bool:
        attention = self.inbox.get_state("presence_attention", {})
        return isinstance(attention, dict) and attention.get("owner_key") == self._attention_key(message)

    def _claim_attention(self, message: dict[str, object]) -> bool:
        """Persist the session that owns attention and report transitions once."""
        owner_key = self._attention_key(message)
        attention = self.inbox.get_state("presence_attention", {})
        if isinstance(attention, dict) and attention.get("owner_key") == owner_key:
            return False
        self.inbox.set_state(
            "presence_attention",
            {
                "state": "assigned",
                "owner_key": owner_key,
                "session_id": message.get("session_id"),
                "session_label": message.get("session_label") or "Codex",
                "profile_id": message.get("profile_id"),
                "avatar_id": message.get("avatar_id"),
                "turn_id": message.get("turn_id"),
                "event_id": message.get("event_id"),
                "updated_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            },
        )
        return True

    def _claim_update(self, focus_state: str) -> dict[str, object] | None:
        """Take one latest-only update after all durable messages have won."""
        if focus_state in {
            "listening",
            "transcribing",
            "submitting",
            "target-response",
            "resume-playback",
        }:
            return None
        if self.inbox.has_pending():
            return None
        with self.update_lock:
            update = self.pending_update
            self.pending_update = None
        if update is None or not self._attention_matches(update):
            return None
        return update

    def _preempt_update_for_real_message(self) -> None:
        with self.update_lock:
            self.pending_update = None
        with self.current_lock:
            current = dict(self.current) if self.current else None
        if current is not None and self._is_ephemeral_update(current):
            # Updates use an interruptible worker request.  Real messages use
            # the pauseable contract and are never stopped by another update.
            self.worker.request_stop()

    def _speech_text(self, message: dict[str, object]) -> str:
        ephemeral_update = self._is_ephemeral_update(message)
        attention_changed = False if ephemeral_update else self._claim_attention(message)
        resumed = message.get("resume_text")
        if isinstance(resumed, str) and resumed.strip():
            return resumed
        try:
            settings = json.loads((self.voice_root / "input.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}
        mode = settings.get("session_labels", "off") if isinstance(settings, dict) else "off"
        label = str(message.get("session_label") or "Codex")
        if ephemeral_update:
            return str(message.get("text", ""))
        if mode in {"first-message", "session-change"} and attention_changed:
            template = str(settings.get("session_label_template", SESSION_LABEL_TEMPLATE))
            try:
                prefix = template.format(session_name=label).strip()
            except (KeyError, ValueError):
                prefix = f"{label} says"
            return f"{prefix}: {message.get('text', '')}"
        if mode == "every-message":
            return f"{label}: {message.get('text', '')}"
        return str(message.get("text", ""))

    def _resume_after_interruption(self, message: dict[str, object]) -> tuple[str, int]:
        """Return the unplayed speech suffix and its cumulative text offset."""
        event_id = str(message["event_id"])
        progress = read_tts_progress(self.voice_root, event_id)
        base_text = str(message.get("speech_text") or message.get("text") or "")
        relative_offset = 0
        if progress is not None:
            try:
                relative_offset = max(0, min(len(base_text), int(progress.get("offset", 0))))
            except (TypeError, ValueError):
                relative_offset = 0
        resume_text = base_text[relative_offset:].lstrip() if relative_offset else base_text
        if not resume_text:
            resume_text = base_text
        try:
            previous_offset = max(0, int(message.get("resume_offset") or 0))
        except (TypeError, ValueError):
            previous_offset = 0
        return resume_text, previous_offset + relative_offset

    def enqueue(self, message: dict[str, object]) -> bool:
        inserted = self.inbox.enqueue(message)
        if inserted and not self._is_ephemeral_update(message):
            self._preempt_update_for_real_message()
        self.wake_event.set()
        return inserted

    def publish_update(self, message: dict[str, object]) -> bool:
        """Coalesce one ephemeral update without writing it to SQLite."""
        event_id = message.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("update requires event_id")
        if not isinstance(message.get("text"), str) or not str(message["text"]).strip():
            raise ValueError("update requires non-empty text")
        if not self._attention_matches(message) or self.inbox.has_pending():
            return False
        with self.current_lock:
            current_id = self.current.get("event_id") if self.current else None
        with self.update_lock:
            if event_id in self.update_history or event_id == current_id:
                return False
            if self.pending_update and self.pending_update.get("event_id") == event_id:
                return False
            self.pending_update = {**message, "_ephemeral_update": True}
            self.update_history.append(event_id)
        self.wake_event.set()
        return True

    def drain_completed(self) -> list[dict[str, object]]:
        completed: list[dict[str, object]] = []
        while True:
            try:
                completed.append(self.completed.get_nowait())
            except queue.Empty:
                return completed

    def interrupt_current(self) -> dict[str, object] | None:
        with self.current_lock:
            current = dict(self.current) if self.current else None
        self.wake_event.set()
        return current

    def close(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        self.worker.request_stop()
        if self.thread.is_alive():
            self.thread.join(timeout=4)
        self.worker.close()

    def is_idle(self) -> bool:
        with self.current_lock:
            return self.current is None


class VoiceInputController:
    """Handle Orb capture controls without blocking rollout polling."""

    def __init__(self, project_root: Path, voice_root: Path, inbox: Inbox, arbiter: PlaybackArbiter) -> None:
        self.project_root = project_root
        self.voice_root = voice_root
        self.inbox = inbox
        self.arbiter = arbiter
        self.target_session_id: str | None = None
        self.target_turn_id: str | None = None
        self.recordings: list[Path] = []
        self.resume_event_id: str | None = None
        self.lock_started_at = 0.0
        self.thread: threading.Thread | None = None
        self.active_capture_sequence: int | None = None
        self.active_target_session_id: str | None = None
        try:
            self.latest_started_sequence = int(
                inbox.get_state("input_capture_sequence", 0) or 0
            )
        except (TypeError, ValueError):
            self.latest_started_sequence = 0
        self.latest_delivered_sequence = 0
        self.pending_jobs: deque[TranscriptionJob] = deque()
        self.job_lock = threading.Lock()
        self.stt_worker = STTWorker(voice_root)
        self.warm_thread: threading.Thread | None = None

    def set_state(
        self,
        state: str,
        *,
        session_id: str | None = None,
        error: str | None = None,
        capture_sequence: int | None = None,
    ) -> None:
        payload: dict[str, object] = {"state": state}
        if session_id:
            payload["session_id"] = session_id
        if error:
            payload["error"] = error
        if capture_sequence is not None:
            payload["capture_sequence"] = capture_sequence
        self.inbox.set_state("input", payload)
        emit_orb_event({"type": "voice-input", **payload}, self.voice_root)

    def handle_controls(self) -> None:
        for control in self.inbox.consume_controls():
            command = control.get("command")
            payload = control.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if command == "capture-start":
                self._start_capture(payload)
            elif command == "capture-finish":
                self._finish_capture(payload)
            elif command == "capture-cancel":
                self._cancel_capture()

    def _start_capture(self, payload: dict[str, object]) -> None:
        target = payload.get("target_session_id")
        if not isinstance(target, str) or not target:
            self.set_state("error", error="no_target_session")
            return
        sequence = payload.get("capture_sequence")
        if not isinstance(sequence, int) or sequence < 1:
            sequence = max(self.latest_started_sequence, 0) + 1
        if self.active_capture_sequence is not None:
            self.set_state(
                "error",
                session_id=target,
                error="capture_already_active",
                capture_sequence=sequence,
            )
            return
        if self.target_session_id and self.target_session_id != target:
            self.set_state("error", error="voice_input_locked")
            return
        current = self.arbiter.interrupt_current()
        if current is not None:
            self.resume_event_id = str(current["event_id"])
        if self.target_session_id is None:
            self.target_session_id = target
            self.target_turn_id = None
        self.active_capture_sequence = sequence
        self.active_target_session_id = target
        self.latest_started_sequence = max(self.latest_started_sequence, sequence)
        self.lock_started_at = time.monotonic()
        self.inbox.set_state("focus", {"state": "listening", "session_id": target})
        self.set_state("listening", session_id=target, capture_sequence=sequence)
        self._warm_stt()

    def _warm_stt(self) -> None:
        if self.stt_worker.ready or (self.warm_thread is not None and self.warm_thread.is_alive()):
            return
        self.warm_thread = threading.Thread(
            target=self.stt_worker.start,
            name="codex-voice-stt-warm",
            daemon=True,
        )
        self.warm_thread.start()

    def _finish_capture(self, payload: dict[str, object]) -> None:
        recording = payload.get("recording")
        sequence = payload.get("capture_sequence")
        if not isinstance(recording, str) or not self.target_session_id:
            self.set_state("error", error="capture_without_target")
            return
        path = Path(recording)
        if not path.is_file():
            self.set_state("error", session_id=self.target_session_id, error="recording_missing")
            return
        if (
            not isinstance(sequence, int)
            or sequence < 1
            or sequence != self.active_capture_sequence
        ):
            path.unlink(missing_ok=True)
            self.set_state(
                "error",
                session_id=self.target_session_id,
                error="capture_sequence_mismatch",
                capture_sequence=sequence if isinstance(sequence, int) else None,
            )
            return
        self.recordings.append(path)
        # STT starts as soon as the button is released. Playback is allowed to
        # resume independently, so the clipboard never waits for the remainder
        # of the interrupted assistant message.
        self._begin_transcription(allow_playback=True)

    def _begin_transcription(self, *, allow_playback: bool = False) -> None:
        if (
            not self.target_session_id
            or not self.recordings
            or self.active_capture_sequence is None
        ):
            return
        target = self.target_session_id
        sequence = self.active_capture_sequence
        resume_event_id = self.resume_event_id
        recordings = tuple(self.recordings)
        self.recordings.clear()
        self.active_capture_sequence = None
        self.active_target_session_id = None
        if allow_playback:
            focus: dict[str, object] = {"state": "resume-playback", "session_id": target}
            if resume_event_id:
                focus["resume_event_id"] = resume_event_id
            self.inbox.set_state("focus", focus)
            self.arbiter.wake_event.set()
        else:
            self.inbox.set_state("focus", {"state": "transcribing", "session_id": target})
        self.set_state("transcribing", session_id=target, capture_sequence=sequence)
        self._enqueue_transcription(
            TranscriptionJob(sequence, recordings, target, resume_event_id)
        )

    def _enqueue_transcription(self, job: TranscriptionJob) -> None:
        start_thread = False
        with self.job_lock:
            self.pending_jobs.append(job)
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._drain_transcriptions,
                    name="codex-voice-input-submit",
                    daemon=True,
                )
                start_thread = True
        if start_thread and self.thread is not None:
            self.thread.start()

    def _drain_transcriptions(self) -> None:
        while True:
            with self.job_lock:
                if not self.pending_jobs:
                    self.thread = None
                    return
                job = self.pending_jobs.popleft()
            self._transcribe_and_submit(
                list(job.recordings), job.target, capture_sequence=job.sequence
            )

    def _stt_python(self) -> Path:
        candidate = self.voice_root / ".stt-venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        return candidate if candidate.is_file() else Path(sys.executable)

    def _delivery_mode(self) -> str:
        try:
            settings = json.loads((self.voice_root / "input.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            settings = {}
        mode = settings.get("delivery_mode") if isinstance(settings, dict) else None
        return mode if mode in {"clipboard", "app-server"} else "clipboard"

    def _durable_latest_capture_sequence(self) -> int:
        try:
            durable = int(self.inbox.get_state("input_capture_sequence", 0) or 0)
        except (TypeError, ValueError):
            durable = 0
        return max(durable, self.latest_started_sequence)

    def _deliver_transcript(
        self,
        target: str,
        transcript: str,
        *,
        capture_sequence: int | None = None,
    ) -> dict[str, object]:
        """Deliver through the explicitly selected safe boundary."""
        mode = self._delivery_mode()
        sequenced = capture_sequence is not None
        sequence = capture_sequence if capture_sequence is not None else 0
        latest = self._durable_latest_capture_sequence()
        if sequenced and sequence < latest:
            log(
                self.voice_root,
                f"suppressed superseded transcript sequence={sequence} latest={latest}",
            )
            return {
                "mode": mode,
                "capture_sequence": sequence,
                "superseded": True,
            }
        if mode == "clipboard":
            copy_text(transcript)
            focus_payload: dict[str, object] = {
                "state": "drain-queued",
                "session_id": target,
                "delivery": "clipboard",
                "char_count": len(transcript),
            }
            if sequenced:
                focus_payload["capture_sequence"] = sequence
            self.inbox.set_state(
                "focus",
                focus_payload,
            )
            self.set_state(
                "clipboard-ready",
                session_id=target,
                capture_sequence=sequence if sequenced else None,
            )
            self.latest_delivered_sequence = max(self.latest_delivered_sequence, sequence)
            if not sequenced or sequence >= self._durable_latest_capture_sequence():
                self.target_session_id = None
                self.target_turn_id = None
                self.resume_event_id = None
                self.lock_started_at = 0.0
            result: dict[str, object] = {"mode": "clipboard", "char_count": len(transcript)}
            if sequenced:
                result["capture_sequence"] = sequence
            return result

        self.inbox.set_state("focus", {"state": "submitting", "session_id": target})
        self.set_state(
            "submitting",
            session_id=target,
            capture_sequence=sequence if sequenced else None,
        )
        result = AppServerClient(self.project_root, timeout_seconds=INPUT_LOCK_TIMEOUT_SECONDS).submit(
            target, transcript, wait_for_completion=False
        )
        self.target_turn_id = result.get("turn_id") or None
        self.lock_started_at = time.monotonic()
        self.inbox.set_state(
            "focus",
            {"state": "target-response", "session_id": target, "turn_id": self.target_turn_id},
        )
        self.set_state(
            "target-response",
            session_id=target,
            capture_sequence=sequence if sequenced else None,
        )
        return result

    def _transcribe_and_submit(
        self,
        recordings: list[Path],
        target: str,
        *,
        capture_sequence: int | None = None,
    ) -> None:
        sequence = capture_sequence if capture_sequence is not None else 0
        started_at = time.monotonic()
        log(self.voice_root, f"STT start sequence={sequence} recordings={len(recordings)}")
        try:
            transcripts: list[str] = []
            for recording in recordings:
                transcripts.append(self.stt_worker.transcribe(recording, sequence))
            transcript = " ".join(transcripts).strip()
            if not transcript:
                raise STTUnavailable("speech was not recognized")
            self._deliver_transcript(
                target,
                transcript,
                capture_sequence=capture_sequence,
            )
            elapsed = time.monotonic() - started_at
            log(self.voice_root, f"STT complete sequence={sequence} elapsed={elapsed:.2f}s")
        except (STTUnavailable, DeliveryError, ClipboardError, OSError, json.JSONDecodeError) as exc:
            log(self.voice_root, f"voice input failed: {type(exc).__name__}: {exc}")
            latest = self._durable_latest_capture_sequence()
            if capture_sequence is None or sequence >= latest:
                self.inbox.set_state("focus", {"state": "idle"})
                self.set_state(
                    "error",
                    session_id=target,
                    error=type(exc).__name__,
                    capture_sequence=capture_sequence,
                )
            if self.target_session_id == target and (
                capture_sequence is None or sequence >= latest
            ):
                self.target_session_id = None
                self.target_turn_id = None
                self.resume_event_id = None
                self.lock_started_at = 0.0
        finally:
            for recording in recordings:
                try:
                    recording.unlink(missing_ok=True)
                except OSError:
                    pass

    def notify_completed(self, message: dict[str, object]) -> None:
        if not self.target_session_id or message.get("session_id") != self.target_session_id:
            return
        focus = self.inbox.get_state("focus", {})
        focus = focus if isinstance(focus, dict) else {}
        if focus.get("state") == "resume-playback":
            resume_event_id = focus.get("resume_event_id")
            if resume_event_id and str(message.get("event_id")) == str(resume_event_id):
                self.resume_event_id = None
            return
        if message.get("kind") != "final":
            return
        if self.target_turn_id and message.get("turn_id") and message.get("turn_id") != self.target_turn_id:
            return
        self.inbox.set_state("focus", {"state": "drain-queued", "session_id": self.target_session_id})
        self.set_state("drain-queued", session_id=self.target_session_id)
        self.target_session_id = None
        self.target_turn_id = None

    def _cancel_capture(self) -> None:
        self.inbox.set_state("focus", {"state": "idle"})
        for recording in self.recordings:
            try:
                recording.unlink(missing_ok=True)
            except OSError:
                pass
        self.recordings.clear()
        self.active_capture_sequence = None
        self.active_target_session_id = None
        self.resume_event_id = None
        self.target_session_id = None
        self.target_turn_id = None
        self.set_state("idle")

    def close(self) -> None:
        self._cancel_capture()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=4)
        self.stt_worker.close()

    def tick(self) -> None:
        focus = self.inbox.get_state("focus", {})
        focus = focus if isinstance(focus, dict) else {}
        state = focus.get("state")
        if state == "drain-queued" and self.arbiter.is_idle():
            self.inbox.set_state("focus", {"state": "idle"})
            self.set_state("idle")
            return
        if state not in {"idle", "drain-queued"} and self.lock_started_at:
            if time.monotonic() - self.lock_started_at > INPUT_LOCK_TIMEOUT_SECONDS:
                self.inbox.set_state("focus", {"state": "idle"})
                for recording in self.recordings:
                    try:
                        recording.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.recordings.clear()
                self.active_capture_sequence = None
                self.active_target_session_id = None
                self.resume_event_id = None
                self.target_session_id = None
                self.target_turn_id = None
                self.set_state("idle")


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
    environment["CODEX_ORB_PORT"] = str(orb_port_for_root(voice_root))
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
    seen_activity: set[tuple[str, str, str, str, str]] = set()
    inbox = Inbox(database_path(voice_root))
    recovered = inbox.recover_inflight()
    if recovered:
        log(voice_root, f"recovered {recovered} interrupted inbox item(s) after restart")
    discarded_legacy_updates = inbox.discard_legacy_updates()
    if discarded_legacy_updates:
        log(
            voice_root,
            f"discarded {discarded_legacy_updates} legacy commentary inbox item(s) after update-lane migration",
        )
    recovered_input = inbox.recover_input_state()
    if recovered_input is not None:
        removed_recordings = discard_orphan_recordings(voice_root)
        log(
            voice_root,
            f"released stale input state {recovered_input.get('state')}; "
            f"discarded {removed_recordings} orphan recording(s)",
        )
    else:
        removed_recordings = discard_orphan_recordings(voice_root)
        if removed_recordings:
            log(voice_root, f"discarded {removed_recordings} orphan recording(s) after clean restart")
    stale_stop_marker = voice_root / "tts-stop.request"
    if stale_stop_marker.exists():
        stale_stop_marker.unlink(missing_ok=True)
        log(voice_root, "cleared stale TTS stop marker after restart")
    stale_resume_marker = voice_root / "tts-resume.request"
    if stale_resume_marker.exists():
        stale_resume_marker.unlink(missing_ok=True)
        log(voice_root, "cleared stale TTS resume marker after restart")
    stale_progress = voice_root / "tts-progress.json"
    if stale_progress.exists():
        stale_progress.unlink(missing_ok=True)
        log(voice_root, "cleared stale TTS progress cursor after restart")
    # Playback is globally owned.  This watcher only adapts rollout records
    # and registers its project/session route with the user-level arbiter.
    arbiter = GlobalPlaybackArbiter(project_root, voice_root, inbox)
    presence = PresenceService(project_root, voice_root, inbox, arbiter)
    input_controller = VoiceInputController(project_root, voice_root, inbox, arbiter)
    activity_tracker = ActivityTracker(presence)
    presence.start()
    activity_tracker.reset(time.monotonic())

    try:
        while marker_enabled(voice_root):
            current_scope_mtime = scope_mtime(voice_root)
            if current_scope_mtime != scope_state_mtime:
                scope_state = load_state(voice_root)
                scope_state_mtime = current_scope_mtime
                streams.clear()
                announced.clear()
                seen_activity.clear()
                activity_tracker.reset(time.monotonic())
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
                activity_state = classify_activity(record)
                if activity_state is not None:
                    payload = record.get("payload")
                    payload_type = payload.get("type") if isinstance(payload, dict) else ""
                    activity_key = (
                        str(path),
                        str(record.get("timestamp")),
                        str(record.get("type")),
                        str(payload_type),
                        activity_state,
                    )
                    if activity_key not in seen_activity:
                        seen_activity.add(activity_key)
                        session_id = session_metadata(path)[1]
                        activity_tracker.update(path, activity_state, session_id, time.monotonic())
                commentary = commentary_message(record) if progress_enabled(voice_root) else None
                session_id = session_metadata(path)[1]
                turn_id = record_turn_id(record)
                session_label = record_session_label(project_root, session_id, record)
                if commentary is not None:
                    key = (str(path), str(record.get("timestamp")), "commentary", commentary)
                    if key not in seen:
                        seen.add(key)
                        commentary_ratio = configured_commentary_volume(voice_root) / 100
                        volume = round(configured_volume(voice_root) * commentary_ratio)
                        log(voice_root, f"publishing ephemeral update at {volume}%: {len(commentary)} characters")
                        presence.publish_update(
                            {
                                "schema": "codex-voice/message/v0.1",
                                "event_id": stable_event_id(path, record.get("timestamp"), "commentary", commentary),
                                "project_root": str(project_root),
                                "session_id": session_id,
                                "thread_id": session_id,
                                "turn_id": turn_id,
                                "session_label": session_label,
                                "kind": "commentary",
                                "text": commentary,
                                "sequence": 0,
                                "volume": volume,
                            }
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
                presence.enqueue_speech(
                    {
                        "schema": "codex-voice/message/v0.1",
                        "event_id": stable_event_id(path, record.get("timestamp"), "final_answer", message),
                        "project_root": str(project_root),
                        "session_id": session_id,
                        "thread_id": session_id,
                        "turn_id": turn_id,
                        "session_label": session_label,
                        "kind": "final",
                        "text": message,
                        "sequence": 1,
                        "volume": configured_volume(voice_root),
                        "announced_key": f"{session_id}:{turn_id or 'session'}",
                    }
                )
            input_controller.handle_controls()
            for completed in presence.drain_completed():
                input_controller.notify_completed(completed)
            input_controller.tick()
            arbiter.sync_inbox()
            activity_tracker.tick(time.monotonic())
            time.sleep(POLL_SECONDS)
        log(voice_root, "stopping: voice marker is off")
    except KeyboardInterrupt:
        log(voice_root, "stopping: keyboard interrupt")
    except Exception as exc:
        log(voice_root, f"crashed: {type(exc).__name__}: {exc}")
    finally:
        input_controller.close()
        activity_tracker.close(time.monotonic())
        presence.close()
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        log(voice_root, "stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
