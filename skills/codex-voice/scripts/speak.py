"""Speak the final Codex response with local Kokoro TTS on Windows."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VOICE_ROOT = ROOT / ".codex-voice"
MODEL_PATH = VOICE_ROOT / "kokoro-v1.0.int8.onnx"
DML_MODEL_PATH = VOICE_ROOT / "gpu_patch" / "kokoro-v1.0.int8.dml-conv2d.onnx"
VOICES_PATH = VOICE_ROOT / "voices-v1.0.bin"
ENABLED_MARKER = VOICE_ROOT / "enabled"
LOG_PATH = VOICE_ROOT / "hook.log"
WATCHER_PID_PATH = VOICE_ROOT / "watcher.pid"
PLAYER_PID_PATH = VOICE_ROOT / "tts-player.pid"
STOP_REQUEST_PATH = VOICE_ROOT / "tts-stop.request"
RESUME_REQUEST_PATH = VOICE_ROOT / "tts-resume.request"
PROGRESS_PATH = VOICE_ROOT / "tts-progress.json"
VOICE_CONFIG_PATH = VOICE_ROOT / "voice"
MODE_CONFIG_PATH = VOICE_ROOT / "mode"
SPEED_CONFIG_PATH = VOICE_ROOT / "speed"
PROVIDER_CONFIG_PATH = VOICE_ROOT / "provider"
VOLUME_CONFIG_PATH = VOICE_ROOT / "volume"
ORB_ENABLED_MARKER = VOICE_ROOT / "orb.enabled"
DEFAULT_ORB_PORT = 17831
DEFAULT_VOICE = "bf_isabella"
DEFAULT_MODE = "stream"
DEFAULT_SPEED = 1.08
DEFAULT_PROVIDER = "cpu"
DEFAULT_VOLUME = 20
PLAYER_DRAIN_TIMEOUT_SECONDS = 10.0
MAX_PACING_CATCHUP_SECONDS = 0.1
_TTS_CACHE = None
_FFPLAY_CACHE: str | None = None
_FFPLAY_RESOLVED = False


class PlaybackInterrupted(RuntimeError):
    """The host requested that the current speech item stop immediately."""


async def wait_until_playback_deadline(
    deadline: float,
    *,
    clock=None,
    sleeper=None,
) -> None:
    """Do not advance PCM pacing when a platform timer wakes up early."""
    import asyncio

    if clock is None:
        clock = asyncio.get_running_loop().time
    if sleeper is None:
        sleeper = asyncio.sleep
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return
        await sleeper(remaining)


def advance_playback_deadline(
    previous: float | None,
    frame_seconds: float,
    now: float,
) -> float:
    """Pace against one clock while bounding catch-up after a scheduler stall."""
    if previous is None:
        anchor = now
    else:
        anchor = max(previous, now - MAX_PACING_CATCHUP_SECONDS)
    return anchor + max(0.0, frame_seconds)


def ffplay_executable() -> str | None:
    """Resolve the real player binary instead of a process-spawning shim."""
    global _FFPLAY_CACHE, _FFPLAY_RESOLVED
    if _FFPLAY_RESOLVED:
        return _FFPLAY_CACHE
    _FFPLAY_RESOLVED = True
    candidate_value = shutil.which("ffplay")
    if not candidate_value:
        return None
    candidate = Path(candidate_value)
    if os.name == "nt":
        chocolatey_root = Path(
            os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")
        )
        try:
            is_chocolatey_shim = (
                candidate.resolve().parent
                == (chocolatey_root / "bin").resolve()
            )
        except OSError:
            is_chocolatey_shim = False
        if is_chocolatey_shim:
            try:
                real_players = [
                    path
                    for path in (chocolatey_root / "lib").rglob("ffplay.exe")
                    if path.is_file() and path.stat().st_size > 1_000_000
                ]
            except OSError:
                real_players = []
            if real_players:
                candidate = max(real_players, key=lambda path: path.stat().st_size)
    _FFPLAY_CACHE = str(candidate)
    return _FFPLAY_CACHE


def stop_requested() -> bool:
    if not STOP_REQUEST_PATH.is_file():
        return False
    try:
        STOP_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def resume_requested() -> bool:
    if not RESUME_REQUEST_PATH.is_file():
        return False
    try:
        RESUME_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def write_player_pid(player: subprocess.Popen[bytes] | None) -> None:
    if player is None:
        return
    try:
        PLAYER_PID_PATH.write_text(str(player.pid), encoding="utf-8")
    except OSError:
        pass


def clear_player_pid(player: subprocess.Popen[bytes] | None) -> None:
    if player is None:
        return
    try:
        if PLAYER_PID_PATH.read_text(encoding="utf-8").strip() == str(player.pid):
            PLAYER_PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def write_tts_progress(event_id: str | None, text: str, fraction: float) -> None:
    if not event_id:
        return
    fraction = max(0.0, min(0.99, float(fraction)))
    payload = {
        "event_id": event_id,
        "offset": min(len(text), max(0, int(round(len(text) * fraction)))),
        "fraction": fraction,
        "updated_at": time.time(),
    }
    temporary = PROGRESS_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(PROGRESS_PATH)
    except OSError:
        pass


def clear_tts_progress(event_id: str | None) -> None:
    if not event_id:
        return
    try:
        payload = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and str(payload.get("event_id")) == event_id:
            PROGRESS_PATH.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def debug(message: str) -> None:
    if os.environ.get("CODEX_TTS_DEBUG"):
        print(f"[codex-tts] {message}", file=sys.stderr)


def hook_log(message: str) -> None:
    """Leave a small local trace without recording the assistant's text."""
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{timestamp} {message}\n")
    except OSError:
        pass


def finish(*, emit: bool = True) -> int:
    """Return the JSON response required by Codex Stop hooks."""
    if emit:
        print(json.dumps({"continue": True}), flush=True)
    return 0


def transcript_watcher_is_active() -> bool:
    """Avoid duplicate speech if the desktop transcript bridge is running."""
    if os.environ.get("CODEX_TTS_FROM_WATCHER") == "1":
        return False
    try:
        pid = int(WATCHER_PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def configured_voice() -> str:
    environment_voice = os.environ.get("CODEX_TTS_VOICE")
    if environment_voice:
        return environment_voice
    try:
        file_voice = VOICE_CONFIG_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        file_voice = ""
    return file_voice or DEFAULT_VOICE


def language_for_voice(voice: str) -> str:
    if voice.startswith(("bf_", "bm_")):
        return "en-gb"
    return "en-us"


def configured_mode() -> str:
    environment_mode = os.environ.get("CODEX_TTS_MODE", "").strip().lower()
    if environment_mode in {"stream", "quality"}:
        return environment_mode
    legacy_stream = os.environ.get("CODEX_TTS_STREAM")
    if legacy_stream is not None:
        return "stream" if legacy_stream.lower() not in {"0", "false", "off"} else "quality"
    try:
        file_mode = MODE_CONFIG_PATH.read_text(encoding="utf-8").strip().lower()
    except OSError:
        file_mode = ""
    return file_mode if file_mode in {"stream", "quality"} else DEFAULT_MODE


def configured_speed() -> float:
    environment_speed = os.environ.get("CODEX_TTS_SPEED")
    try:
        value = float(environment_speed) if environment_speed else None
    except ValueError:
        value = None
    if value is None:
        try:
            value = float(SPEED_CONFIG_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            value = DEFAULT_SPEED
    return max(0.5, min(2.0, value))


def configured_volume() -> int:
    environment_volume = os.environ.get("CODEX_TTS_VOLUME")
    try:
        value = int(environment_volume) if environment_volume else None
    except ValueError:
        value = None
    if value is None:
        try:
            value = int(VOLUME_CONFIG_PATH.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            value = DEFAULT_VOLUME
    return max(0, min(100, value))


def configured_provider() -> str:
    environment_provider = os.environ.get("CODEX_TTS_PROVIDER", "").strip().lower()
    if not environment_provider:
        try:
            environment_provider = PROVIDER_CONFIG_PATH.read_text(encoding="utf-8").strip().lower()
        except OSError:
            environment_provider = ""
    if environment_provider in {"cuda", "cudaexecutionprovider", "nvidia", "nvidia-cuda"}:
        return "cuda"
    if environment_provider in {"directml", "dml", "gpu"}:
        return "directml"
    return DEFAULT_PROVIDER


def configured_model_path() -> Path:
    return DML_MODEL_PATH if configured_provider() == "directml" else MODEL_PATH


def clean_for_speech(value: str) -> str:
    """Reduce Markdown/code noise before sending text to the speech model."""
    text = re.sub(r"```[\s\S]*?```", " ", value)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    max_chars = int(os.environ.get("CODEX_TTS_MAX_CHARS", "5000"))
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
        text += ". Response truncated."
    return text


def read_payload() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def voice_is_enabled() -> bool:
    if not ENABLED_MARKER.is_file():
        return False
    return ENABLED_MARKER.read_text(encoding="utf-8").strip().lower() in {
        "1",
        "true",
        "on",
        "enabled",
    }


def orb_is_enabled() -> bool:
    try:
        value = ORB_ENABLED_MARKER.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return value in {"1", "true", "on", "enabled"}


def orb_socket() -> socket.socket | None:
    if not orb_is_enabled():
        return None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        return sock
    except OSError:
        return None


def orb_send(sock: socket.socket | None, payload: dict) -> None:
    if sock is None:
        return
    try:
        port = int(os.environ.get("CODEX_ORB_PORT", str(DEFAULT_ORB_PORT)))
        message = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sock.sendto(message, ("127.0.0.1", port))
    except (OSError, ValueError):
        pass


def spectral_bands(samples, count: int = 16) -> list[float]:
    """Summarize a short audio frame into frequency bands for the orb."""
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size < 32:
        return [0.0] * count
    windowed = samples * np.hanning(samples.size)
    spectrum = np.abs(np.fft.rfft(windowed))[1:]
    if spectrum.size == 0:
        return [0.0] * count
    edges = np.linspace(0, spectrum.size, count + 1, dtype=int)
    values = []
    for index in range(count):
        part = spectrum[edges[index] : edges[index + 1]]
        values.append(float(np.mean(part)) if part.size else 0.0)
    reference = max(float(np.percentile(spectrum, 85)), 1e-8)
    return [float(np.clip(value / (reference * 1.35), 0.0, 1.0)) for value in values]


class OrbPlaybackTimeline:
    """Schedule orb frames against the moment audio is sent to the player."""

    def __init__(self, orb: socket.socket | None, sample_rate: int) -> None:
        self.orb = orb
        self.sample_rate = float(sample_rate)
        self.events: queue.PriorityQueue[tuple[float, int, dict]] = queue.PriorityQueue()
        self.producer_done = threading.Event()
        self.scheduler_done = threading.Event()
        self.sequence = 0
        self.cursor = 0
        self.started_at: float | None = None
        self.scheduler: threading.Thread | None = None

    def start(self) -> None:
        if self.orb is None or self.scheduler is not None:
            return
        self.started_at = time.monotonic()
        orb_send(self.orb, {"type": "state", "state": "speaking"})
        self.scheduler = threading.Thread(
            target=self._send_events,
            name="codex-orb-playback-clock",
            daemon=True,
        )
        self.scheduler.start()

    def add(self, samples) -> None:
        if self.orb is None:
            return
        import numpy as np

        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        self.start()
        if self.started_at is None:
            return
        frame_size = max(256, int(self.sample_rate * 0.08))
        for frame_start in range(0, samples.size, frame_size):
            frame = samples[frame_start : frame_start + frame_size]
            rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
            peak = float(np.max(np.abs(frame))) if frame.size else 0.0
            payload = {
                "type": "audio",
                "amplitude": min(1.0, rms * 5.0 + peak * 0.10),
                "rms": rms,
                "peak": peak,
                "bands": spectral_bands(frame),
            }
            due_time = self.started_at + (self.cursor + frame_start) / self.sample_rate
            self.events.put((due_time, self.sequence, payload))
            self.sequence += 1
        self.cursor += samples.size

    def _send_events(self) -> None:
        try:
            while not self.producer_done.is_set() or not self.events.empty():
                try:
                    due_time, _, payload = self.events.get(timeout=0.05)
                except queue.Empty:
                    continue
                delay = due_time - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                orb_send(self.orb, payload)
                self.events.task_done()
        finally:
            self.scheduler_done.set()

    def finish(self) -> None:
        if self.orb is None:
            return
        if self.scheduler is not None:
            self.producer_done.set()
            self.scheduler.join(timeout=max(2.0, self.cursor / self.sample_rate + 1.0))
            if not self.scheduler_done.is_set():
                hook_log("orb playback scheduler timed out")
        orb_send(self.orb, {"type": "state", "state": "idle"})
        self.orb.close()


def get_tts():
    """Load Kokoro once per worker process and reuse it for later messages."""
    global _TTS_CACHE
    if _TTS_CACHE is None:
        provider = configured_provider()
        model_path = configured_model_path()
        if provider == "directml":
            os.environ["ONNX_PROVIDER"] = "DmlExecutionProvider"
        elif provider == "cuda":
            os.environ["ONNX_PROVIDER"] = "CUDAExecutionProvider"
            try:
                import onnxruntime as ort

                preload_dlls = getattr(ort, "preload_dlls", None)
                if callable(preload_dlls):
                    preload_dlls()
            except Exception as exc:
                hook_log(f"CUDA DLL preload unavailable: {type(exc).__name__}: {exc}")
        else:
            os.environ.pop("ONNX_PROVIDER", None)
        from kokoro_onnx import Kokoro

        if not model_path.is_file() or not VOICES_PATH.is_file():
            raise FileNotFoundError(
                f"Kokoro assets for provider '{provider}' are missing under {VOICE_ROOT}."
            )
        _TTS_CACHE = Kokoro(str(model_path), str(VOICES_PATH))
        hook_log(
            f"Kokoro model loaded into persistent worker provider={provider} "
            f"model={model_path.name}"
        )
    return _TTS_CACHE


def generate_audio(text: str) -> Path:
    import soundfile as sf

    if not configured_model_path().is_file() or not VOICES_PATH.is_file():
        raise FileNotFoundError(
            f"Kokoro assets are missing under {VOICE_ROOT}. Run the setup command again."
        )

    voice = configured_voice()
    speed = configured_speed()
    tts = get_tts()
    audio, sample_rate = tts.create(
        text, voice, speed=speed, lang=language_for_voice(voice)
    )

    handle = tempfile.NamedTemporaryFile(
        prefix="codex-tts-",
        suffix=".wav",
        dir=VOICE_ROOT,
        delete=False,
    )
    audio_path = Path(handle.name)
    handle.close()
    sf.write(str(audio_path), audio, sample_rate)
    return audio_path


def stream_audio(
    text: str,
    *,
    event_id: str | None = None,
    interruptible: bool = True,
    pauseable: bool = False,
) -> None:
    """Generate PCM independently and feed a pauseable, frame-paced OS sink."""
    import asyncio
    from contextlib import suppress

    import numpy as np
    if not configured_model_path().is_file() or not VOICES_PATH.is_file():
        raise FileNotFoundError(
            f"Kokoro assets are missing under {VOICE_ROOT}. Run the setup command again."
        )

    ffplay = ffplay_executable()
    if not ffplay:
        raise RuntimeError("ffplay is required for streaming playback")

    voice = configured_voice()
    speed = configured_speed()
    volume = configured_volume()
    tts = get_tts()
    orb = orb_socket()

    async def consume() -> None:
        audio_queue: asyncio.Queue[tuple[object, int] | None] = asyncio.Queue()
        playback_allowed = asyncio.Event()
        playback_allowed.set()
        abort_requested = asyncio.Event()
        shutdown = asyncio.Event()
        player: subprocess.Popen[bytes] | None = None
        timeline: OrbPlaybackTimeline | None = None
        first_frame = True
        completed = False
        total_samples = 0
        control_generation = 0
        producer_error: BaseException | None = None
        estimated_seconds = max(0.5, len(text) / (13.0 * max(speed, 0.5)))

        def close_player(*, graceful: bool = False) -> None:
            nonlocal player
            current = player
            player = None
            if current is None:
                return
            try:
                if current.stdin is not None:
                    current.stdin.close()
            except OSError:
                pass
            try:
                if graceful:
                    drain_started = time.monotonic()
                    current.wait(timeout=PLAYER_DRAIN_TIMEOUT_SECONDS)
                    hook_log(
                        "stream sink drained after EOF in "
                        f"{time.monotonic() - drain_started:.3f}s"
                    )
                elif current.poll() is None:
                    current.terminate()
                    current.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if graceful:
                    hook_log(
                        "stream sink drain timed out after "
                        f"{PLAYER_DRAIN_TIMEOUT_SECONDS:.1f}s; forcing close"
                    )
                try:
                    current.kill()
                    current.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                try:
                    current.kill()
                except OSError:
                    pass
            clear_player_pid(current)

        def ensure_player(sample_rate: int) -> subprocess.Popen[bytes]:
            nonlocal player
            if player is not None and player.poll() is None:
                return player
            close_player()
            player = subprocess.Popen(
                [
                    ffplay,
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "error",
                    "-f",
                    "f32le",
                    "-ar",
                    str(sample_rate),
                    "-ch_layout",
                    "mono",
                    "-volume",
                    str(volume),
                    "-i",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if player.stdin is None:
                raise RuntimeError("Could not open ffplay input")
            write_player_pid(player)
            return player

        async def produce() -> None:
            nonlocal producer_error
            try:
                async for audio, sample_rate in tts.create_stream(
                    text,
                    voice,
                    speed=speed,
                    lang=language_for_voice(voice),
                ):
                    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
                    frame_samples = max(1, int(sample_rate * 0.02))
                    for offset in range(0, len(samples), frame_samples):
                        await audio_queue.put(
                            (samples[offset : offset + frame_samples].copy(), int(sample_rate))
                        )
            except BaseException as exc:
                producer_error = exc
            finally:
                await audio_queue.put(None)

        async def monitor_controls() -> None:
            nonlocal control_generation
            while not shutdown.is_set():
                if stop_requested():
                    control_generation += 1
                    close_player()
                    if pauseable:
                        playback_allowed.clear()
                        hook_log("stream playback paused; inference continues")
                    elif interruptible:
                        abort_requested.set()
                        playback_allowed.set()
                if resume_requested() and pauseable:
                    playback_allowed.set()
                    hook_log("stream playback resumed from buffered PCM")
                await asyncio.sleep(0.01)

        async def play_frames() -> None:
            nonlocal timeline, first_frame, total_samples
            loop = asyncio.get_running_loop()
            paced_player: subprocess.Popen[bytes] | None = None
            playback_deadline: float | None = None
            while True:
                item = await audio_queue.get()
                if item is None:
                    return
                frame, sample_rate = item
                failures = 0
                while True:
                    if abort_requested.is_set():
                        raise PlaybackInterrupted()
                    await playback_allowed.wait()
                    if abort_requested.is_set():
                        raise PlaybackInterrupted()
                    # The input helper writes the marker before terminating
                    # ffplay. Give the monitor a turn before a replacement sink
                    # can be created.
                    if STOP_REQUEST_PATH.is_file():
                        await asyncio.sleep(0.01)
                        continue
                    generation = control_generation
                    current = ensure_player(sample_rate)
                    if paced_player is not current:
                        paced_player = current
                        playback_deadline = None
                    if timeline is None:
                        timeline = OrbPlaybackTimeline(orb, sample_rate)
                    try:
                        assert current.stdin is not None
                        current.stdin.write(frame.tobytes())
                        current.stdin.flush()
                    except (BrokenPipeError, OSError):
                        close_player()
                        failures += 1
                        if failures >= 3 and playback_allowed.is_set():
                            raise RuntimeError("ffplay repeatedly closed the streaming input")
                        await asyncio.sleep(0.01)
                        continue
                    if first_frame:
                        hook_log("stream first frame")
                        first_frame = False
                    playback_deadline = advance_playback_deadline(
                        playback_deadline,
                        len(frame) / max(1, sample_rate),
                        loop.time(),
                    )
                    await wait_until_playback_deadline(
                        playback_deadline
                    )
                    if generation != control_generation or not playback_allowed.is_set():
                        # At most one 20 ms frame is replayed after an immediate
                        # sink termination; no later audio reaches the OS while
                        # capture is held.
                        continue
                    timeline.add(frame)
                    total_samples += len(frame)
                    elapsed_seconds = total_samples / max(1, sample_rate)
                    write_tts_progress(event_id, text, elapsed_seconds / estimated_seconds)
                    break

        producer_task = asyncio.create_task(produce())
        monitor_task = asyncio.create_task(monitor_controls())
        try:
            await play_frames()
            await producer_task
            if producer_error is not None:
                raise producer_error
            completed = True
        finally:
            shutdown.set()
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
            if not producer_task.done():
                producer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await producer_task
            close_player(graceful=completed)
            if first_frame:
                hook_log("stream ended without audio frames")
            if timeline is not None:
                timeline.finish()
            elif orb is not None:
                orb.close()
            if completed:
                clear_tts_progress(event_id)
            STOP_REQUEST_PATH.unlink(missing_ok=True)
            RESUME_REQUEST_PATH.unlink(missing_ok=True)

    asyncio.run(consume())
    hook_log("stream completed")


def play_audio(
    audio_path: Path,
    *,
    event_id: str | None = None,
    text: str | None = None,
    interruptible: bool = True,
) -> None:
    ffplay = ffplay_executable()
    if ffplay:
        volume = configured_volume()
        if orb_is_enabled():
            player: subprocess.Popen[bytes] | None = None
            timeline: OrbPlaybackTimeline | None = None
            completed = False
            try:
                import soundfile as sf

                audio, sample_rate = sf.read(
                    str(audio_path), dtype="float32", always_2d=False
                )
                timeline = OrbPlaybackTimeline(orb_socket(), int(sample_rate))
                player = subprocess.Popen(
                    [
                        ffplay,
                        "-nodisp",
                        "-autoexit",
                        "-loglevel",
                        "error",
                        "-volume",
                        str(volume),
                        str(audio_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                write_player_pid(player)
                timeline.add(audio)
                duration = max(0.1, len(audio) / max(1, int(sample_rate)))
                started_at = time.monotonic()
                progress_text = text or audio_path.name
                write_tts_progress(event_id, progress_text, 0.0)
                while player.poll() is None:
                    if interruptible and stop_requested():
                        player.terminate()
                        raise PlaybackInterrupted()
                    write_tts_progress(event_id, progress_text, (time.monotonic() - started_at) / duration)
                    time.sleep(0.05)
                completed = True
                return
            except PlaybackInterrupted:
                raise
            except Exception as exc:
                hook_log(f"buffered orb playback failed: {type(exc).__name__}: {exc}")
                if player is not None:
                    if player.poll() is None:
                        player.wait()
                    clear_player_pid(player)
                    return
            finally:
                if timeline is not None:
                    timeline.finish()
                if completed:
                    clear_tts_progress(event_id)

        player = subprocess.Popen(
            [
                ffplay,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                str(volume),
                str(audio_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_player_pid(player)
        completed = False
        try:
            while player.poll() is None:
                if interruptible and stop_requested():
                    player.terminate()
                    raise PlaybackInterrupted()
                time.sleep(0.05)
            completed = True
        finally:
            clear_player_pid(player)
            if completed:
                clear_tts_progress(event_id)
        return

    if os.environ.get("CODEX_TTS_ALLOW_FULL_VOLUME") != "1":
        raise RuntimeError(
            "ffplay was not found; refusing to use an uncontrolled system-volume player"
        )

    import winsound

    # Without SND_ASYNC, PlaySound waits until playback is complete.
    winsound.PlaySound(str(audio_path), winsound.SND_FILENAME)


def handle_payload(payload: dict, *, emit_finish: bool = True) -> int:
    requested_volume = payload.get("tts_volume")
    previous_volume = os.environ.get("CODEX_TTS_VOLUME")
    if requested_volume is not None:
        try:
            os.environ["CODEX_TTS_VOLUME"] = str(max(0, min(100, int(requested_volume))))
        except (TypeError, ValueError):
            pass
    try:
        return _handle_payload(payload, emit_finish=emit_finish)
    finally:
        if previous_volume is None:
            os.environ.pop("CODEX_TTS_VOLUME", None)
        else:
            os.environ["CODEX_TTS_VOLUME"] = previous_volume


def _handle_payload(payload: dict, *, emit_finish: bool) -> int:
    def done() -> int:
        return finish(emit=emit_finish)

    hook_log("invoked")
    if os.environ.get("CODEX_TTS_DISABLE"):
        hook_log("skipped: CODEX_TTS_DISABLE")
        return done()
    if transcript_watcher_is_active():
        hook_log("skipped: desktop transcript watcher active")
        return done()
    if not voice_is_enabled():
        hook_log("skipped: voice disabled")
        return done()

    message = payload.get("last_assistant_message")
    if not isinstance(message, str):
        hook_log("skipped: no last_assistant_message")
        return done()

    text = clean_for_speech(message)
    if not text:
        hook_log("skipped: message cleaned to empty")
        return done()

    audio_path: Path | None = None
    event_id = payload.get("tts_event_id")
    event_id = event_id.strip() if isinstance(event_id, str) and event_id.strip() else None
    pauseable = bool(payload.get("tts_pauseable"))
    interruptible = not pauseable
    try:
        hook_log(
            f"speaking: {len(text)} characters mode={configured_mode()} "
            f"voice={configured_voice()} speed={configured_speed()} "
            f"provider={configured_provider()}"
        )
        debug(f"generating {len(text)} characters")
        if configured_mode() == "stream" and ffplay_executable():
            stream_audio(
                text,
                event_id=event_id,
                interruptible=interruptible,
                pauseable=pauseable,
            )
        else:
            audio_path = generate_audio(text)
            play_audio(
                audio_path,
                event_id=event_id,
                text=text,
                interruptible=interruptible,
            )
        hook_log("completed")
    except PlaybackInterrupted:
        hook_log("interrupted by voice input")
        return finish(emit=emit_finish) if emit_finish else 3
    except Exception as exc:  # Never make a Codex turn fail because TTS failed.
        hook_log(f"error: {type(exc).__name__}: {exc}")
        print(f"Codex TTS skipped: {exc}", file=sys.stderr)
    finally:
        if audio_path is not None:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass
    return done()


def server_main() -> int:
    """Keep one preloaded Kokoro process alive for the desktop watcher."""
    hook_log("persistent TTS worker starting")
    preload_ok = True
    try:
        get_tts()
    except Exception as exc:
        preload_ok = False
        hook_log(f"persistent TTS preload failed: {type(exc).__name__}: {exc}")
    print(json.dumps({"ready": preload_ok}), flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            result = handle_payload(payload, emit_finish=False)
            print(
                json.dumps(
                    {
                        "done": True,
                        "ok": result == 0,
                        "interrupted": result == 3,
                    }
                ),
                flush=True,
            )
        except Exception as exc:
            hook_log(f"persistent TTS worker request failed: {type(exc).__name__}: {exc}")
            print(json.dumps({"done": True, "ok": False}), flush=True)
    hook_log("persistent TTS worker stopped")
    return 0


def main() -> int:
    return handle_payload(read_payload(), emit_finish=True)


if __name__ == "__main__":
    if "--server" in sys.argv[1:]:
        raise SystemExit(server_main())
    raise SystemExit(main())
