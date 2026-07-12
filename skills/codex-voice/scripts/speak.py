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
_TTS_CACHE = None


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


def stream_audio(text: str) -> None:
    """Stream Kokoro chunks directly to ffplay instead of waiting for a WAV."""
    import asyncio

    import numpy as np
    if not configured_model_path().is_file() or not VOICES_PATH.is_file():
        raise FileNotFoundError(
            f"Kokoro assets are missing under {VOICE_ROOT}. Run the setup command again."
        )

    ffplay = shutil.which("ffplay")
    if not ffplay:
        raise RuntimeError("ffplay is required for streaming playback")

    voice = configured_voice()
    speed = configured_speed()
    volume = configured_volume()
    tts = get_tts()
    orb = orb_socket()

    async def consume() -> None:
        player: subprocess.Popen[bytes] | None = None
        timeline: OrbPlaybackTimeline | None = None
        first_chunk = True
        try:
            async for audio, sample_rate in tts.create_stream(
                text,
                voice,
                speed=speed,
                lang=language_for_voice(voice),
            ):
                if player is None:
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
                    timeline = OrbPlaybackTimeline(orb, int(sample_rate))
                    hook_log("stream first chunk")
                samples = np.asarray(audio, dtype=np.float32).reshape(-1)
                raw_audio = samples.tobytes()
                if timeline is not None:
                    timeline.add(samples)
                try:
                    player.stdin.write(raw_audio)
                    player.stdin.flush()
                except BrokenPipeError:
                    raise RuntimeError("ffplay closed the streaming input")
                first_chunk = False
        finally:
            if player is not None:
                if player.stdin is not None:
                    player.stdin.close()
                player.wait()
                if first_chunk:
                    hook_log("stream ended without audio chunks")
            if timeline is not None:
                timeline.finish()
            elif orb is not None:
                orb.close()

    asyncio.run(consume())
    hook_log("stream completed")


STREAM_MIN_CHARS = 18
STREAM_MAX_CHARS = 220


class SpeechChunker:
    """Turn app-server text deltas into safe, sentence-sized speech chunks."""

    def __init__(
        self,
        *,
        min_chars: int = STREAM_MIN_CHARS,
        max_chars: int = STREAM_MAX_CHARS,
    ) -> None:
        self.buffer = ""
        self.min_chars = min_chars
        self.max_chars = max_chars

    def add(self, text: str) -> list[str]:
        if text:
            self.buffer += text
        return self._extract(final=False)

    def finish(self) -> list[str]:
        return self._extract(final=True)

    def _extract(self, *, final: bool) -> list[str]:
        chunks: list[str] = []
        while self.buffer:
            boundary = self._find_boundary()
            if boundary is None and len(self.buffer) >= self.max_chars:
                boundary = self._find_soft_boundary()
            if boundary is None:
                if not final:
                    break
                boundary = len(self.buffer)

            raw = self.buffer[:boundary]
            self.buffer = self.buffer[boundary:]
            cleaned = self._clean(raw)
            if cleaned:
                chunks.append(cleaned)
            if not final and boundary == len(raw) and not self.buffer:
                break
        return chunks

    def _find_boundary(self) -> int | None:
        for index, character in enumerate(self.buffer):
            if character in ".!?":
                end = index + 1
                while end < len(self.buffer) and self.buffer[end] in "\"'\u201d\u2019)]":
                    end += 1
                if end < len(self.buffer) and not self.buffer[end].isspace():
                    continue
                if end >= self.min_chars and self._balanced_code_fence(end):
                    while end < len(self.buffer) and self.buffer[end].isspace():
                        end += 1
                    return end
            elif character == "\n" and index + 1 >= self.min_chars:
                if self._balanced_code_fence(index + 1):
                    return index + 1
        return None

    def _find_soft_boundary(self) -> int | None:
        limit = min(len(self.buffer), self.max_chars)
        for index in range(limit - 1, self.min_chars - 1, -1):
            if self.buffer[index].isspace() and self._balanced_code_fence(index):
                return index + 1
        return None

    def _balanced_code_fence(self, end: int) -> bool:
        return self.buffer[:end].count("```") % 2 == 0

    @staticmethod
    def _clean(raw: str) -> str:
        if raw.count("```") % 2:
            return ""
        return clean_for_speech(raw)


class IncrementalAudioPlayer:
    """Keep one ffplay pipe open while multiple Kokoro chunks are generated."""

    def __init__(self) -> None:
        self.player: subprocess.Popen[bytes] | None = None
        self.timeline: OrbPlaybackTimeline | None = None
        self.orb: socket.socket | None = None
        self.sample_rate: int | None = None

    def write(self, audio, sample_rate: int) -> None:
        import numpy as np

        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        if self.player is None:
            ffplay = shutil.which("ffplay")
            if not ffplay:
                raise RuntimeError("ffplay is required for streamed playback")
            self.sample_rate = int(sample_rate)
            self.orb = orb_socket()
            self.player = subprocess.Popen(
                [
                    ffplay,
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "error",
                    "-f",
                    "f32le",
                    "-ar",
                    str(self.sample_rate),
                    "-ch_layout",
                    "mono",
                    "-volume",
                    str(configured_volume()),
                    "-i",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if self.player.stdin is None:
                raise RuntimeError("Could not open ffplay input")
            self.timeline = OrbPlaybackTimeline(self.orb, self.sample_rate)
            hook_log("bridge stream first audio chunk")
        elif self.sample_rate != int(sample_rate):
            raise RuntimeError("Kokoro changed sample rate during one streamed response")

        if self.timeline is not None:
            self.timeline.add(samples)
        try:
            if self.player.stdin is None:
                raise RuntimeError("Streamed player input closed")
            self.player.stdin.write(samples.tobytes())
            self.player.stdin.flush()
        except BrokenPipeError as exc:
            raise RuntimeError("ffplay closed the streaming input") from exc

    def close(self) -> None:
        player = self.player
        timeline = self.timeline
        self.player = None
        self.timeline = None
        if player is not None:
            try:
                if player.stdin is not None:
                    player.stdin.close()
                player.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    player.kill()
                except OSError:
                    pass
        if timeline is not None:
            timeline.finish()
        elif self.orb is not None:
            self.orb.close()
        self.orb = None
        self.sample_rate = None


def render_stream_segment(text: str, player: IncrementalAudioPlayer) -> None:
    """Generate one text segment and append its audio to the shared player."""

    import asyncio
    import numpy as np

    voice = configured_voice()
    speed = configured_speed()
    tts = get_tts()

    async def consume() -> None:
        async for audio, sample_rate in tts.create_stream(
            text,
            voice,
            speed=speed,
            lang=language_for_voice(voice),
        ):
            player.write(np.asarray(audio, dtype=np.float32), int(sample_rate))

    asyncio.run(consume())


class IncrementalSpeechServer:
    """Consume start/delta/finish events while rendering audio off-thread."""

    def __init__(self, *, ready: bool) -> None:
        self.ready = ready
        self.events: queue.Queue[tuple[str, str, str | None] | None] = queue.Queue()
        self.chunkers: dict[str, SpeechChunker] = {}
        self.renderer: threading.Thread | None = None

    @staticmethod
    def emit(event: dict[str, object]) -> None:
        print(json.dumps(event, separators=(",", ":")), flush=True)

    def run(self) -> None:
        self.renderer = threading.Thread(
            target=self._render,
            name="codex-tts-incremental-renderer",
            daemon=True,
        )
        self.renderer.start()
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            stream_id = event.get("stream_id")
            if not isinstance(stream_id, str):
                stream_id = "default"
            if kind == "start":
                self.chunkers[stream_id] = SpeechChunker()
            elif kind == "delta":
                text = event.get("text")
                if not isinstance(text, str):
                    continue
                chunker = self.chunkers.setdefault(stream_id, SpeechChunker())
                for chunk in chunker.add(text):
                    self.events.put(("text", stream_id, chunk))
            elif kind == "finish":
                chunker = self.chunkers.pop(stream_id, SpeechChunker())
                for chunk in chunker.finish():
                    self.events.put(("text", stream_id, chunk))
                self.events.put(("finish", stream_id, None))
            elif kind == "cancel":
                self.chunkers.pop(stream_id, None)
                self.events.put(("cancel", stream_id, None))
            elif kind == "shutdown":
                break
        self.events.put(("shutdown", "", None))
        if self.renderer is not None:
            self.renderer.join(timeout=60)

    def _render(self) -> None:
        player: IncrementalAudioPlayer | None = None
        active_stream: str | None = None
        while True:
            event = self.events.get()
            if event is None:
                return
            kind, stream_id, text = event
            if kind == "shutdown":
                if player is not None:
                    player.close()
                return
            if kind == "cancel":
                if active_stream == stream_id and player is not None:
                    player.close()
                    player = None
                    active_stream = None
                continue
            if kind == "text":
                if not self.ready or not text:
                    continue
                if active_stream != stream_id:
                    if player is not None:
                        player.close()
                    player = IncrementalAudioPlayer()
                    active_stream = stream_id
                try:
                    render_stream_segment(text, player)
                except Exception as exc:  # Presence failures must not kill the bridge.
                    hook_log(f"bridge incremental speech error: {type(exc).__name__}: {exc}")
                    if player is not None:
                        player.close()
                    player = None
                    active_stream = None
                    self.emit(
                        {
                            "event": "error",
                            "stream_id": stream_id,
                            "ok": False,
                        }
                    )
                continue
            if kind == "finish":
                if active_stream == stream_id and player is not None:
                    player.close()
                    player = None
                    active_stream = None
                self.emit(
                    {
                        "event": "done",
                        "stream_id": stream_id,
                        "ok": self.ready,
                    }
                )


def stream_server_main() -> int:
    """Run the incremental Kokoro worker used by app_server_bridge.py."""

    ready = voice_is_enabled() and shutil.which("ffplay") is not None
    if ready:
        try:
            get_tts()
        except Exception as exc:
            ready = False
            hook_log(f"bridge stream preload failed: {type(exc).__name__}: {exc}")
    IncrementalSpeechServer.emit({"event": "ready", "ok": ready})
    IncrementalSpeechServer(ready=ready).run()
    return 0


def play_audio(audio_path: Path) -> None:
    ffplay = shutil.which("ffplay")
    if ffplay:
        volume = configured_volume()
        if orb_is_enabled():
            player: subprocess.Popen[bytes] | None = None
            timeline: OrbPlaybackTimeline | None = None
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
                timeline.add(audio)
                player.wait()
                return
            except Exception as exc:
                hook_log(f"buffered orb playback failed: {type(exc).__name__}: {exc}")
                if player is not None:
                    if player.poll() is None:
                        player.wait()
                    return
            finally:
                if timeline is not None:
                    timeline.finish()

        subprocess.run(
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
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
    try:
        hook_log(
            f"speaking: {len(text)} characters mode={configured_mode()} "
            f"voice={configured_voice()} speed={configured_speed()} "
            f"provider={configured_provider()}"
        )
        debug(f"generating {len(text)} characters")
        if configured_mode() == "stream" and shutil.which("ffplay"):
            stream_audio(text)
        else:
            audio_path = generate_audio(text)
            play_audio(audio_path)
        hook_log("completed")
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
            print(json.dumps({"done": True, "ok": result == 0}), flush=True)
        except Exception as exc:
            hook_log(f"persistent TTS worker request failed: {type(exc).__name__}: {exc}")
            print(json.dumps({"done": True, "ok": False}), flush=True)
    hook_log("persistent TTS worker stopped")
    return 0


def main() -> int:
    return handle_payload(read_payload(), emit_finish=True)


if __name__ == "__main__":
    if "--stream-server" in sys.argv[1:]:
        raise SystemExit(stream_server_main())
    if "--server" in sys.argv[1:]:
        raise SystemExit(server_main())
    raise SystemExit(main())
