"""Shared project-local Codex voice configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_VOICE = "bf_isabella"
DEFAULT_SPEED = 1.08
DEFAULT_MODE = "stream"
DEFAULT_PROVIDER = "cpu"
DEFAULT_VOLUME = 20
DEFAULT_COMMENTARY_VOLUME = 50
MIN_SPEED = 0.5
MAX_SPEED = 2.0

CONFIG_FILES = {
    "voice": "voice",
    "speed": "speed",
    "mode": "mode",
    "provider": "provider",
    "volume": "volume",
    "commentary_volume": "commentary-volume",
}


def _read(path: Path, default: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or default
    except OSError:
        return default


def _environment(name: str) -> str:
    return os.environ.get(name, "").strip()


def _clamp_int(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0, min(100, parsed))


def configured_voice(voice_root: Path) -> str:
    return _environment("CODEX_TTS_VOICE") or _read(
        voice_root / CONFIG_FILES["voice"], DEFAULT_VOICE
    )


def configured_speed(voice_root: Path) -> float:
    raw = _environment("CODEX_TTS_SPEED") or _read(
        voice_root / CONFIG_FILES["speed"], str(DEFAULT_SPEED)
    )
    try:
        value = float(raw)
    except ValueError:
        value = DEFAULT_SPEED
    return max(MIN_SPEED, min(MAX_SPEED, value))


def configured_mode(voice_root: Path) -> str:
    mode = _environment("CODEX_TTS_MODE").lower()
    if mode in {"stream", "quality"}:
        return mode
    legacy_stream = os.environ.get("CODEX_TTS_STREAM")
    if legacy_stream is not None:
        return "stream" if legacy_stream.lower() not in {"0", "false", "off"} else "quality"
    mode = _read(voice_root / CONFIG_FILES["mode"], DEFAULT_MODE).lower()
    return mode if mode in {"stream", "quality"} else DEFAULT_MODE


def normalize_provider(value: str) -> str:
    provider = value.strip().lower()
    if provider in {"cuda", "cudaexecutionprovider", "nvidia", "nvidia-cuda"}:
        return "cuda"
    if provider in {"directml", "dml", "gpu"}:
        return "directml"
    if provider in {"openvino", "openvinoexecutionprovider", "intel", "arc", "arc-openvino"}:
        return "openvino"
    return "cpu"


def configured_provider(voice_root: Path) -> str:
    provider = _environment("CODEX_TTS_PROVIDER") or _read(
        voice_root / CONFIG_FILES["provider"], DEFAULT_PROVIDER
    )
    return normalize_provider(provider)


def configured_volume(voice_root: Path) -> int:
    raw = _environment("CODEX_TTS_VOLUME") or _read(
        voice_root / CONFIG_FILES["volume"], str(DEFAULT_VOLUME)
    )
    return _clamp_int(raw, DEFAULT_VOLUME)


def configured_commentary_volume(voice_root: Path) -> int:
    raw = _environment("CODEX_TTS_COMMENTARY_VOLUME") or _read(
        voice_root / CONFIG_FILES["commentary_volume"], str(DEFAULT_COMMENTARY_VOLUME)
    )
    return _clamp_int(raw, DEFAULT_COMMENTARY_VOLUME)


def marker_enabled(path: Path) -> bool:
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return False
    return value in {"1", "true", "on", "enabled"}


def load_settings(voice_root: Path) -> dict[str, object]:
    return {
        "voice": configured_voice(voice_root),
        "speed": configured_speed(voice_root),
        "mode": configured_mode(voice_root),
        "provider": configured_provider(voice_root),
        "volume": configured_volume(voice_root),
        "commentary_volume": configured_commentary_volume(voice_root),
        "progress": marker_enabled(voice_root / "progress"),
        "orb": marker_enabled(voice_root / "orb.enabled"),
    }


def write_setting(voice_root: Path, name: str, value: object) -> None:
    if name not in CONFIG_FILES:
        raise KeyError(name)
    (voice_root / CONFIG_FILES[name]).write_text(f"{value}\n", encoding="utf-8")
