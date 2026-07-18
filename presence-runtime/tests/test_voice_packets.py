from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import numpy as np


SPEAK_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "codex-voice"
    / "scripts"
    / "speak.py"
)


def load_speak():
    spec = importlib.util.spec_from_file_location("presence_packet_speak", SPEAK_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PacketSocket:
    def __init__(self) -> None:
        self.packets = []
        self.closed = False

    def send(self, payload: bytes) -> None:
        self.packets.append(json.loads(payload.decode("utf-8")))

    def close(self) -> None:
        self.closed = True


def test_every_v02_playback_packet_carries_binding_and_utterance_identity() -> None:
    speak = load_speak()
    target = PacketSocket()
    binding_id = str(uuid.uuid4())
    utterance_id = str(uuid.uuid4())
    timeline = speak.OrbPlaybackTimeline(
        target,
        1000,
        binding_id=binding_id,
        utterance_id=utterance_id,
    )
    timeline.add(np.ones(80, dtype=np.float32) * 0.1)
    timeline.finish()

    assert {packet["type"] for packet in target.packets} == {"state", "audio"}
    assert all(packet["binding_id"] == binding_id for packet in target.packets)
    assert all(packet["utterance_id"] == utterance_id for packet in target.packets)
    assert target.closed is True
