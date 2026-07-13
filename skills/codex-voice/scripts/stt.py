"""Optional local speech-to-text provider for Codex Voice recordings."""

from __future__ import annotations

import os
import argparse
import json
import sys
from pathlib import Path


class STTUnavailable(RuntimeError):
    pass


_MODEL_CACHE: dict[tuple[str, str, str, str], object] = {}


def get_model(voice_root: Path):
    """Load Whisper once per process so successive captures avoid N-1 latency."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise STTUnavailable(
            "local STT is not installed; run setup.py --with-input or install faster-whisper in .stt-venv"
        ) from exc

    model_name = os.environ.get("CODEX_STT_MODEL", "base.en").strip() or "base.en"
    provider = os.environ.get("CODEX_STT_PROVIDER", "cpu").strip().lower()
    device = "cuda" if provider in {"cuda", "nvidia", "nvidia-cuda"} else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    key = (model_name, device, compute_type, str(voice_root.resolve()))
    model = _MODEL_CACHE.get(key)
    if model is None:
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            download_root=str(voice_root / "stt-models"),
        )
        _MODEL_CACHE[key] = model
    return model


def transcribe(recording: Path, voice_root: Path) -> str:
    if not recording.is_file():
        raise STTUnavailable(f"recording not found: {recording.name}")
    model = get_model(voice_root)
    segments, _info = model.transcribe(str(recording), beam_size=1, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()
    if not text:
        raise STTUnavailable("speech was not recognized")
    return text


def server_main(voice_root: Path) -> int:
    """Preload one local model and serialize newline-delimited capture requests."""
    try:
        get_model(voice_root)
    except (STTUnavailable, OSError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return 2
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        request_id: object = None
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            request_id = payload.get("request_id")
            recording = payload.get("recording")
            if not isinstance(recording, str) or not recording:
                raise STTUnavailable("recording path is missing")
            text = transcribe(Path(recording), voice_root)
            response = {"ok": True, "text": text, "request_id": request_id}
        except (STTUnavailable, OSError, ValueError, json.JSONDecodeError) as exc:
            response = {"ok": False, "error": str(exc), "request_id": request_id}
        print(json.dumps(response), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--voice-root", required=True, type=Path)
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    if args.server:
        return server_main(args.voice_root.resolve())
    if args.recording is None:
        parser.error("--recording is required unless --server is used")
    try:
        print(json.dumps({"ok": True, "text": transcribe(args.recording, args.voice_root)}))
        return 0
    except (STTUnavailable, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
