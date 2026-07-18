from __future__ import annotations

import copy
import hashlib

import pytest


@pytest.fixture
def higan_pack() -> dict:
    fingerprint = "sha256:" + hashlib.sha256(b"higan-test-model").hexdigest()
    document = {
        "schema": "presence/avatar-model-pack/v0.2",
        "avatar_id": "higan",
        "version": 3,
        "model_fingerprint": fingerprint,
        "renderer": {
            "kind": "live2d",
            "entrypoint": "model/Higan.model3.json",
        },
        "semantic_slots": {
            "wardrobe.outer": {"exclusive": True},
            "accessory.shoulders": {"exclusive": True},
            "body.legs": {"exclusive": True},
            "body.pose": {"exclusive": True},
            "prop.hand": {"exclusive": True},
            "gesture.arms": {"exclusive": True},
            "expression.eyes": {"exclusive": True},
            "expression.mouth": {"exclusive": True},
            "effect.face": {"exclusive": False},
        },
        "actions": {
            "wardrobe.sweater": {
                "slots": ["wardrobe.outer"],
                "operations": [{"kind": "parameter", "id": "sweater", "value": 1}],
            },
            "accessory.shawl": {
                "slots": ["accessory.shoulders"],
                "operations": [{"kind": "parameter", "id": "shawl", "value": 1}],
            },
            "legs.stockings": {
                "slots": ["body.legs"],
                "operations": [{"kind": "parameter", "id": "stockings", "value": 1}],
            },
            "pose.sweater-default": {
                "slots": ["body.pose"],
                "operations": [{"kind": "expression", "id": "sweater-pose"}],
            },
            "pose.pipe": {
                "slots": ["body.pose", "prop.hand"],
                "operations": [{"kind": "expression", "id": "pipe"}],
            },
            "gesture.hand-mouth": {
                "slots": ["gesture.arms"],
                "operations": [{"kind": "expression", "id": "hand-mouth"}],
            },
            "gesture.heart": {
                "slots": ["gesture.arms"],
                "operations": [{"kind": "expression", "id": "heart"}],
            },
            "eyes.dazed": {
                "slots": ["expression.eyes"],
                "operations": [{"kind": "expression", "id": "dazed"}],
            },
            "mouth.unhappy": {
                "slots": ["expression.mouth"],
                "operations": [{"kind": "expression", "id": "unhappy"}],
            },
            "effect.dark-face": {
                "slots": ["effect.face"],
                "operations": [{"kind": "parameter", "id": "dark", "value": 1}],
            },
        },
        "safe_defaults": {
            "slots": {
                "wardrobe.outer": ["wardrobe.sweater"],
                "accessory.shoulders": ["accessory.shawl"],
                "body.legs": ["legs.stockings"],
                "body.pose": ["pose.sweater-default"],
            },
            "activity": {
                "thinking": {"add": ["gesture.hand-mouth"]},
                "tool": {"add": ["gesture.hand-mouth"]},
                "skill": {"add": ["gesture.heart"]},
                "cli": {"add": ["pose.pipe"]},
                "waiting": {"add": ["eyes.dazed"]},
                "error": {"add": ["mouth.unhappy", "effect.dark-face"]},
            },
        },
        "capabilities": [
            "activity-state",
            "audio-cadence",
            "geometry",
            "semantic-slots",
        ],
    }
    return copy.deepcopy(document)


@pytest.fixture
def higan_preset(higan_pack: dict) -> dict:
    return {
        "schema": "presence/preset/v0.2",
        "preset_id": "plain-sweater",
        "revision": 2,
        "compatible_model_fingerprints": [higan_pack["model_fingerprint"]],
        "semantic": {
            "slots": {
                "accessory.shoulders": [],
                "body.legs": [],
            }
        },
    }


@pytest.fixture
def higan_profile() -> dict:
    return {
        "schema": "presence/profile/v0.2",
        "profile_id": "higan-default",
        "revision": 4,
        "voice_id": "af_heart",
        "speed": 1.15,
        "playback_mode": "quality",
        "volume": 64,
        "commentary_ratio": 0.25,
        "avatar_ref": "higan",
        "preset_ref": "plain-sweater",
        "progress_visible": True,
        "renderer_visible": True,
    }

