"""The only v0.2 profile, inheritance, and activity resolver."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Mapping

from .errors import ValidationError
from .models import (
    EffectiveSnapshot,
    RendererSettings,
    SemanticSnapshot,
    TTSSettings,
)
from .semantic import SemanticComposer
from .validation import (
    CONFIG_FIELDS,
    validate_model_pack,
    validate_patch,
    validate_preset,
    validate_profile,
)


BUILTIN_FINGERPRINT = "sha256:" + hashlib.sha256(
    b"presence-builtin-orb-v1"
).hexdigest()
RUNTIME_BUILTINS: dict[str, Any] = {
    "voice_id": "bf_isabella",
    "speed": 1.0,
    "playback_mode": "stream",
    "volume": 100,
    "commentary_ratio": 0.5,
    "avatar_ref": "builtin@1",
    "preset_ref": None,
    "progress_visible": True,
    "renderer_visible": True,
}


def builtin_model_pack() -> dict[str, Any]:
    return {
        "schema": "presence/avatar-model-pack/v0.2",
        "avatar_id": "builtin",
        "version": 1,
        "model_fingerprint": BUILTIN_FINGERPRINT,
        "renderer": {"kind": "builtin", "entrypoint": "orb"},
        "semantic_slots": {},
        "actions": {},
        "safe_defaults": {},
        "capabilities": ["audio-cadence", "activity-state", "geometry"],
    }


class PresenceResolver:
    """Resolve validated source documents into one immutable snapshot."""

    def __init__(self, builtins: Mapping[str, Any] | None = None) -> None:
        self.builtins = dict(RUNTIME_BUILTINS)
        if builtins:
            unknown = set(builtins) - (CONFIG_FIELDS - {"semantic"})
            if unknown:
                raise ValidationError(
                    f"unknown runtime built-ins: {sorted(unknown)}",
                    path="runtime",
                )
            self.builtins.update(builtins)

    def resolve(
        self,
        *,
        binding_id: str,
        revision: int,
        model_pack: Mapping[str, Any],
        profile: Mapping[str, Any] | None = None,
        profile_ref: str | None = None,
        preset: Mapping[str, Any] | None = None,
        project_patch: Mapping[str, Any] | None = None,
        session_patch: Mapping[str, Any] | None = None,
        activity: str | None = None,
    ) -> EffectiveSnapshot:
        self._validate_identity(binding_id, revision)
        avatar = validate_model_pack(model_pack)
        normalized_profile = validate_profile(profile) if profile is not None else None
        normalized_preset = validate_preset(preset) if preset is not None else None
        project = validate_patch(project_patch or {}, path="project")
        session = validate_patch(session_patch or {}, path="session")

        requested_profile = self._requested_profile_ref(profile_ref, project, session)
        resolved_profile_ref = self._match_profile(requested_profile, normalized_profile)

        values = dict(self.builtins)
        provenance = {name: "runtime:builtins" for name in values}
        canonical_avatar = f"{avatar['avatar_id']}@{avatar['version']}"
        values["avatar_ref"] = canonical_avatar
        provenance["avatar_ref"] = "avatar:model-safe-defaults"

        semantic_layers: list[tuple[str, Mapping[str, Any] | None]] = [
            ("avatar:model-safe-defaults", avatar["safe_defaults"]),
        ]

        selected_preset_ref = self._select_preset_ref(
            normalized_profile,
            project,
            session,
        )
        preset_source = self._highest_source_with(
            "preset_ref",
            normalized_profile,
            project,
            session,
        )
        preset_semantic: Mapping[str, Any] | None = None
        if normalized_preset is not None:
            resolved_preset_ref = self._match_preset(
                selected_preset_ref,
                normalized_preset,
                avatar["model_fingerprint"],
            )
            preset_semantic = normalized_preset["semantic"]
        elif selected_preset_ref is not None:
            raise ValidationError(
                f"preset {selected_preset_ref!r} was selected but not loaded",
                path="preset_ref",
            )
        else:
            resolved_preset_ref = None

        if preset_semantic is not None and preset_source == "runtime:builtins":
            semantic_layers.append(("runtime:preset", preset_semantic))
        if normalized_profile is not None:
            self._merge_config(values, provenance, normalized_profile, "profile")
            if preset_semantic is not None and preset_source == "profile":
                semantic_layers.append(("profile:preset", preset_semantic))
            semantic_layers.append(("profile", normalized_profile.get("semantic")))
        self._merge_config(values, provenance, project, "project")
        if preset_semantic is not None and preset_source == "project":
            semantic_layers.append(("project:preset", preset_semantic))
        semantic_layers.append(("project", project.get("semantic")))
        self._merge_config(values, provenance, session, "session")
        if preset_semantic is not None and preset_source == "session":
            semantic_layers.append(("session:preset", preset_semantic))
        semantic_layers.append(("session", session.get("semantic")))

        values["preset_ref"] = resolved_preset_ref
        if selected_preset_ref is not None:
            provenance["preset_ref"] = self._highest_source_with(
                "preset_ref",
                normalized_profile,
                project,
                session,
            )

        self._match_avatar(values["avatar_ref"], avatar)
        values["avatar_ref"] = canonical_avatar
        semantic = SemanticComposer(avatar).compose(
            semantic_layers,
            activity=activity,
        )
        provenance.update(semantic.provenance)

        return EffectiveSnapshot(
            binding_id=binding_id,
            revision=revision,
            profile_ref=resolved_profile_ref,
            avatar_ref=canonical_avatar,
            model_fingerprint=avatar["model_fingerprint"],
            preset_ref=resolved_preset_ref,
            tts=TTSSettings(
                voice_id=values["voice_id"],
                speed=float(values["speed"]),
                playback_mode=values["playback_mode"],
                volume=int(values["volume"]),
                commentary_ratio=float(values["commentary_ratio"]),
            ),
            semantic=SemanticSnapshot(
                persistent_actions=semantic.persistent_actions,
                effective_actions=semantic.effective_actions,
                activity=activity,
            ),
            renderer=RendererSettings(
                visible=bool(values["renderer_visible"]),
                progress_visible=bool(values["progress_visible"]),
                kind=avatar["renderer"]["kind"],
            ),
            capabilities=tuple(sorted(avatar["capabilities"])),
            provenance=self._public_provenance(provenance),
        )

    def resolve_or_last_known_good(
        self,
        *,
        last_known_good: EffectiveSnapshot | None,
        **arguments: Any,
    ) -> EffectiveSnapshot:
        try:
            return self.resolve(**arguments)
        except ValidationError as exc:
            if last_known_good is None:
                raise
            return last_known_good.as_last_known_good_error(str(exc))

    @staticmethod
    def renderer_document(snapshot: EffectiveSnapshot) -> dict[str, Any]:
        document = snapshot.to_document()
        # Renderers get fully resolved, sanitized state. They never receive
        # provenance, validation internals, model operations, or raw patches.
        return {
            "schema": "presence/renderer-snapshot/v0.2",
            "binding_id": document["binding_id"],
            "revision": document["revision"],
            "avatar_ref": document["avatar_ref"],
            "model_fingerprint": document["model_fingerprint"],
            "preset_ref": document["preset_ref"],
            "semantic": document["semantic"],
            "renderer": document["renderer"],
            "capabilities": document["capabilities"],
        }

    @staticmethod
    def _validate_identity(binding_id: str, revision: int) -> None:
        try:
            parsed = uuid.UUID(binding_id)
        except (ValueError, AttributeError) as exc:
            raise ValidationError("must be a UUID", path="binding_id") from exc
        if str(parsed) != binding_id.lower():
            raise ValidationError("must use canonical UUID form", path="binding_id")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValidationError("must be a positive integer", path="revision")

    @staticmethod
    def _requested_profile_ref(
        profile_ref: str | None,
        project: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> str | None:
        selected = profile_ref
        if "profile_ref" in project:
            selected = project["profile_ref"]
        if "profile_ref" in session:
            selected = session["profile_ref"]
        return selected

    @staticmethod
    def _match_profile(
        requested: str | None,
        profile: Mapping[str, Any] | None,
    ) -> str | None:
        if requested is None:
            if profile is not None:
                raise ValidationError(
                    "a profile document was loaded after its reference was cleared",
                    path="profile_ref",
                )
            return None
        if profile is None:
            raise ValidationError(
                f"profile {requested!r} was selected but not loaded",
                path="profile_ref",
            )
        canonical = f"{profile['profile_id']}@{profile['revision']}"
        if requested not in {profile["profile_id"], canonical}:
            raise ValidationError(
                f"loaded profile {canonical!r} does not match {requested!r}",
                path="profile_ref",
            )
        return canonical

    @staticmethod
    def _select_preset_ref(
        profile: Mapping[str, Any] | None,
        project: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> str | None:
        selected = profile.get("preset_ref") if profile else None
        if "preset_ref" in project:
            selected = project["preset_ref"]
        if "preset_ref" in session:
            selected = session["preset_ref"]
        return selected

    @staticmethod
    def _match_preset(
        requested: str | None,
        preset: Mapping[str, Any],
        fingerprint: str,
    ) -> str:
        if requested is None:
            raise ValidationError(
                "a preset document was loaded after its reference was cleared",
                path="preset_ref",
            )
        canonical = f"{preset['preset_id']}@{preset['revision']}"
        if requested not in {preset["preset_id"], canonical}:
            raise ValidationError(
                f"loaded preset {canonical!r} does not match {requested!r}",
                path="preset_ref",
            )
        if fingerprint not in preset["compatible_model_fingerprints"]:
            raise ValidationError(
                f"preset {canonical!r} is incompatible with model {fingerprint}",
                path="preset_ref",
            )
        return canonical

    @staticmethod
    def _match_avatar(requested: str, avatar: Mapping[str, Any]) -> None:
        canonical = f"{avatar['avatar_id']}@{avatar['version']}"
        if requested not in {
            avatar["avatar_id"],
            canonical,
            avatar["model_fingerprint"],
        }:
            raise ValidationError(
                f"loaded avatar {canonical!r} does not match {requested!r}",
                path="avatar_ref",
            )

    @staticmethod
    def _merge_config(
        values: dict[str, Any],
        provenance: dict[str, str],
        layer: Mapping[str, Any],
        source: str,
    ) -> None:
        for name in CONFIG_FIELDS - {"semantic"}:
            if name not in layer:
                continue
            values[name] = layer[name]
            provenance[name] = source

    @staticmethod
    def _highest_source_with(
        field: str,
        profile: Mapping[str, Any] | None,
        project: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> str:
        if field in session:
            return "session"
        if field in project:
            return "project"
        if profile is not None and field in profile:
            return "profile"
        return "runtime:builtins"

    @staticmethod
    def _public_provenance(provenance: Mapping[str, str]) -> dict[str, str]:
        aliases = {
            "voice_id": "tts.voice_id",
            "speed": "tts.speed",
            "playback_mode": "tts.playback_mode",
            "volume": "tts.volume",
            "commentary_ratio": "tts.commentary_ratio",
            "progress_visible": "renderer.progress_visible",
            "renderer_visible": "renderer.visible",
        }
        return {
            aliases.get(field, field): source
            for field, source in provenance.items()
        }
