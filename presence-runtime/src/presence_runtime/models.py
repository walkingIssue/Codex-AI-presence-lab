"""Immutable values crossing the resolver, voice, and renderer boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping


def immutable_mapping(value: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class TTSSettings:
    voice_id: str
    speed: float
    playback_mode: str
    volume: int
    commentary_ratio: float

    def to_document(self) -> dict[str, Any]:
        return {
            "voice_id": self.voice_id,
            "speed": self.speed,
            "playback_mode": self.playback_mode,
            "volume": self.volume,
            "commentary_ratio": self.commentary_ratio,
        }


@dataclass(frozen=True, slots=True)
class SemanticSnapshot:
    persistent_actions: tuple[str, ...]
    effective_actions: tuple[str, ...]
    activity: str | None

    def to_document(self) -> dict[str, Any]:
        return {
            "persistent_actions": list(self.persistent_actions),
            "effective_actions": list(self.effective_actions),
            "activity": self.activity,
        }


@dataclass(frozen=True, slots=True)
class RendererSettings:
    visible: bool
    progress_visible: bool
    kind: str

    def to_document(self) -> dict[str, Any]:
        return {
            "visible": self.visible,
            "progress_visible": self.progress_visible,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class EffectiveSnapshot:
    binding_id: str
    revision: int
    profile_ref: str | None
    avatar_ref: str
    model_fingerprint: str
    preset_ref: str | None
    tts: TTSSettings
    semantic: SemanticSnapshot
    renderer: RendererSettings
    capabilities: tuple[str, ...]
    provenance: Mapping[str, str]
    valid: bool = True
    diagnostics: tuple[str, ...] = ()
    last_known_good: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "provenance", immutable_mapping(self.provenance))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": "presence/effective-snapshot/v0.2",
            "binding_id": self.binding_id,
            "revision": self.revision,
            "profile_ref": self.profile_ref,
            "avatar_ref": self.avatar_ref,
            "model_fingerprint": self.model_fingerprint,
            "preset_ref": self.preset_ref,
            "tts": self.tts.to_document(),
            "semantic": self.semantic.to_document(),
            "renderer": self.renderer.to_document(),
            "capabilities": list(self.capabilities),
            "provenance": dict(self.provenance),
            "validation": {
                "valid": self.valid,
                "diagnostics": list(self.diagnostics),
            },
            "last_known_good": self.last_known_good,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "EffectiveSnapshot":
        tts = document["tts"]
        semantic = document["semantic"]
        renderer = document["renderer"]
        validation = document.get("validation", {})
        return cls(
            binding_id=str(document["binding_id"]),
            revision=int(document["revision"]),
            profile_ref=document.get("profile_ref"),
            avatar_ref=str(document["avatar_ref"]),
            model_fingerprint=str(document["model_fingerprint"]),
            preset_ref=document.get("preset_ref"),
            tts=TTSSettings(
                voice_id=str(tts["voice_id"]),
                speed=float(tts["speed"]),
                playback_mode=str(tts["playback_mode"]),
                volume=int(tts["volume"]),
                commentary_ratio=float(tts["commentary_ratio"]),
            ),
            semantic=SemanticSnapshot(
                persistent_actions=tuple(semantic.get("persistent_actions", ())),
                effective_actions=tuple(semantic.get("effective_actions", ())),
                activity=semantic.get("activity"),
            ),
            renderer=RendererSettings(
                visible=bool(renderer["visible"]),
                progress_visible=bool(renderer["progress_visible"]),
                kind=str(renderer["kind"]),
            ),
            capabilities=tuple(document.get("capabilities", ())),
            provenance=document.get("provenance", {}),
            valid=bool(validation.get("valid", True)),
            diagnostics=tuple(validation.get("diagnostics", ())),
            last_known_good=bool(document.get("last_known_good", False)),
        )

    def as_last_known_good_error(self, diagnostic: str) -> "EffectiveSnapshot":
        return replace(
            self,
            valid=False,
            diagnostics=(diagnostic,),
            last_known_good=True,
        )

    def acknowledged(self) -> "EffectiveSnapshot":
        return replace(
            self,
            valid=True,
            diagnostics=(),
            last_known_good=True,
        )

