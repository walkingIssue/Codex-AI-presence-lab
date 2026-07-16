"""Transactional orchestration above catalog, resolver, store, voice, and renderer."""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Protocol

from .catalog import Catalog
from .errors import ConflictError, PresenceError, ValidationError
from .models import EffectiveSnapshot
from .resolver import PresenceResolver, builtin_model_pack
from .store import PresenceStore


_UNSET = object()


class VoiceConsumer(Protocol):
    def apply_snapshot(self, snapshot: EffectiveSnapshot) -> bool: ...

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool: ...

    def speak(self, item: Mapping[str, Any]) -> str: ...

    def status(self) -> Mapping[str, Any]: ...


class RendererConsumer(Protocol):
    def apply_snapshot(self, snapshot: EffectiveSnapshot) -> bool: ...

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool: ...

    def apply_activity(self, snapshot: EffectiveSnapshot) -> bool: ...

    def playback_event(self, event: Mapping[str, Any]) -> None: ...

    def status(self, binding_id: str | None = None) -> Mapping[str, Any]: ...


class RecordingRenderer:
    """Deterministic renderer consumer used by tests and dry-run diagnostics."""

    def __init__(self) -> None:
        self.snapshots: dict[str, EffectiveSnapshot] = {}
        self.activities: list[EffectiveSnapshot] = []
        self.playback: list[dict[str, Any]] = []
        self.ready = True

    def apply_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        if self.ready:
            self.snapshots[snapshot.binding_id] = snapshot
        return self.ready

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool:
        return self.apply_snapshot(snapshot)

    def apply_activity(self, snapshot: EffectiveSnapshot) -> bool:
        if self.ready:
            self.activities.append(snapshot)
        return self.ready

    def playback_event(self, event: Mapping[str, Any]) -> None:
        self.playback.append(dict(event))

    def status(self, binding_id: str | None = None) -> Mapping[str, Any]:
        acknowledged = (
            binding_id in self.snapshots if binding_id is not None else bool(self.snapshots)
        )
        return {"running": self.ready, "ready": self.ready, "acknowledged": acknowledged}


class RuntimeController:
    """One mutation path for persistent configuration and runtime consumers."""

    def __init__(
        self,
        *,
        store: PresenceStore,
        catalog: Catalog,
        voice: VoiceConsumer,
        renderer: RendererConsumer,
        resolver: PresenceResolver | None = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.voice = voice
        self.renderer = renderer
        self.resolver = resolver or PresenceResolver()

    def ensure_effective(self, binding_id: str) -> EffectiveSnapshot:
        current = self.store.effective_snapshot(binding_id)
        if current is not None:
            return current
        candidate = self.resolve_binding(binding_id)
        self._stage_and_ack(candidate)
        self.store.promote_candidates({binding_id: candidate.revision})
        return self.store.effective_snapshot(binding_id)

    def resolve_binding(
        self,
        binding_id: str,
        *,
        project_patch: Mapping[str, Any] | None = None,
        session_patch: Mapping[str, Any] | None | object = _UNSET,
        activity: str | None = None,
        revision: int | None = None,
    ) -> EffectiveSnapshot:
        binding = self.store.binding(binding_id)
        project = (
            dict(project_patch)
            if project_patch is not None
            else self.store.project_default(binding["project_instance_id"])
        )
        if binding["scope"] == "session":
            session = (
                self.store.session_override(binding_id)
                if session_patch is _UNSET
                else dict(session_patch or {})
            )
        else:
            session = {}
        profile_ref = self._selected_reference("profile_ref", None, project, session)
        profile = self.catalog.get_profile(profile_ref) if profile_ref else None
        avatar_ref = self._selected_reference(
            "avatar_ref",
            profile.get("avatar_ref") if profile else "builtin",
            project,
            session,
        )
        if avatar_ref in {"builtin", "builtin@1"}:
            avatar = builtin_model_pack()
        else:
            avatar = self.catalog.get_avatar(avatar_ref)
        preset_ref = self._selected_reference(
            "preset_ref",
            profile.get("preset_ref") if profile else None,
            project,
            session,
        )
        preset = self.catalog.get_preset(preset_ref) if preset_ref else None
        return self.resolver.resolve(
            binding_id=binding_id,
            revision=revision or self.store.next_revision(binding_id),
            model_pack=avatar,
            profile=profile,
            profile_ref=profile_ref,
            preset=preset,
            project_patch=project,
            session_patch=session,
            activity=activity,
        )

    @staticmethod
    def _selected_reference(
        name: str,
        base: str | None,
        project: Mapping[str, Any],
        session: Mapping[str, Any],
    ) -> str | None:
        selected = base
        if name in project:
            selected = project[name]
        if name in session:
            selected = session[name]
        return selected

    def set_session_override(
        self,
        binding_id: str,
        patch: Mapping[str, Any] | None,
    ) -> EffectiveSnapshot:
        binding = self.store.binding(binding_id)
        if binding["scope"] != "session":
            raise ValidationError("session mutation requires a session binding")
        previous_patch = self.store.session_override(binding_id)
        candidate_patch = dict(patch or {})
        candidate = self.resolve_binding(
            binding_id,
            session_patch=candidate_patch,
        )
        transaction_id = self.store.begin_configuration_transaction(
            scope="session",
            target_id=binding_id,
            previous=previous_patch,
            candidate=candidate_patch,
        )
        previous_snapshot = self.store.effective_snapshot(binding_id)
        try:
            self._stage_and_ack(candidate)
            promoted = self.store.promote_candidates(
                {binding_id: candidate.revision},
                session_update=(binding_id, candidate_patch if patch is not None else None),
            )[0]
        except BaseException as exc:
            self._fail_and_restore(
                [(candidate, previous_snapshot)],
                str(exc),
            )
            self.store.finish_configuration_transaction(
                transaction_id,
                status="rolled_back",
                diagnostic=str(exc),
            )
            raise
        self.store.finish_configuration_transaction(
            transaction_id,
            status="committed",
        )
        return promoted.acknowledged()

    def set_project_default(
        self,
        project_id: str,
        patch: Mapping[str, Any],
    ) -> list[EffectiveSnapshot]:
        previous_patch = self.store.project_default(project_id)
        bindings = self.store.list_bindings(project_id=project_id)
        if not bindings:
            bindings = [self.store.ensure_binding(project_id)]
        candidates = [
            self.resolve_binding(
                binding["binding_id"],
                project_patch=patch,
            )
            for binding in bindings
        ]
        previous_snapshots = {
            binding["binding_id"]: self.store.effective_snapshot(binding["binding_id"])
            for binding in bindings
        }
        transaction_id = self.store.begin_configuration_transaction(
            scope="project",
            target_id=project_id,
            previous=previous_patch,
            candidate=patch,
        )
        try:
            for candidate in candidates:
                self._stage_and_ack(candidate)
            promoted = self.store.promote_candidates(
                {
                    candidate.binding_id: candidate.revision
                    for candidate in candidates
                },
                project_update=(project_id, patch),
            )
        except BaseException as exc:
            self._fail_and_restore(
                [
                    (candidate, previous_snapshots[candidate.binding_id])
                    for candidate in candidates
                ],
                str(exc),
            )
            self.store.finish_configuration_transaction(
                transaction_id,
                status="rolled_back",
                diagnostic=str(exc),
            )
            raise
        self.store.finish_configuration_transaction(
            transaction_id,
            status="committed",
        )
        return [snapshot.acknowledged() for snapshot in promoted]

    def _stage_and_ack(self, candidate: EffectiveSnapshot) -> None:
        self.store.stage_snapshot(candidate)
        try:
            if not self.voice.apply_snapshot(candidate):
                raise ConflictError("voice consumer rejected the effective snapshot")
            self.store.acknowledge_snapshot(
                candidate.binding_id,
                candidate.revision,
                "voice",
                promote=False,
            )
            if not self.renderer.apply_snapshot(candidate):
                raise ConflictError("renderer rejected the effective snapshot")
            ready = self.store.acknowledge_snapshot(
                candidate.binding_id,
                candidate.revision,
                "renderer",
                promote=False,
            )
            if not ready:
                raise ConflictError("effective snapshot lacks required acknowledgements")
        except BaseException as exc:
            if self.store.candidate_snapshot(candidate.binding_id) is not None:
                self.store.fail_snapshot(
                    candidate.binding_id,
                    candidate.revision,
                    str(exc),
                )
            raise

    def _fail_and_restore(
        self,
        candidates: list[tuple[EffectiveSnapshot, EffectiveSnapshot | None]],
        diagnostic: str,
    ) -> None:
        for candidate, previous in candidates:
            staged = self.store.candidate_snapshot(candidate.binding_id)
            if staged is not None and staged.revision == candidate.revision:
                self.store.fail_snapshot(
                    candidate.binding_id,
                    candidate.revision,
                    diagnostic,
                )
            if previous is not None:
                self.voice.restore_snapshot(previous)
                self.renderer.restore_snapshot(previous)

    def set_activity(
        self,
        *,
        source_id: str,
        binding_id: str,
        event_id: str,
        activity: str,
    ) -> EffectiveSnapshot:
        changed = self.store.set_activity(
            source_id=source_id,
            binding_id=binding_id,
            event_id=event_id,
            activity=activity,
        )
        current = self.ensure_effective(binding_id)
        if not changed:
            return current
        overlay = self.resolve_binding(
            binding_id,
            activity=activity,
            revision=current.revision,
        )
        if not self.renderer.apply_activity(overlay):
            self.renderer.restore_snapshot(current)
            raise ConflictError("renderer rejected the activity overlay")
        return overlay

    def enqueue_speech(
        self,
        *,
        source_id: str,
        binding_id: str,
        event_id: str,
        utterance_id: str,
        text: str,
        kind: str,
    ) -> int | None:
        snapshot = self.ensure_effective(binding_id)
        volume = snapshot.tts.volume
        if kind == "commentary":
            volume = round(volume * snapshot.tts.commentary_ratio)
        return self.store.enqueue_speech(
            source_id=source_id,
            binding_id=binding_id,
            effective_revision=snapshot.revision,
            utterance_id=utterance_id,
            event_id=event_id,
            text=text,
            kind=kind,
            tts={
                "voice_id": snapshot.tts.voice_id,
                "speed": snapshot.tts.speed,
                "playback_mode": snapshot.tts.playback_mode,
                "volume": volume,
                "main_volume": snapshot.tts.volume,
                "commentary_ratio": snapshot.tts.commentary_ratio,
            },
        )

    def play_next(self) -> dict[str, Any] | None:
        item = self.store.claim_next_speech()
        if item is None:
            return None
        # The current destination is always the stable binding. Profile ids,
        # foreground windows, and old ports are never consulted.
        started = {
            "type": "voice-output",
            "state": "started",
            "binding_id": item["binding_id"],
            "utterance_id": item["utterance_id"],
        }
        self.renderer.playback_event(started)
        self.store.update_speech_status(item["queue_id"], "playing")
        try:
            result = self.voice.speak(item)
        except PresenceError:
            result = "failed"
        if result == "completed":
            status = "finished"
        elif result == "interrupted":
            status = "paused"
        else:
            status = "failed"
        self.store.update_speech_status(item["queue_id"], status)
        self.renderer.playback_event(
            {
                "type": "voice-output",
                "state": status,
                "binding_id": item["binding_id"],
                "utterance_id": item["utterance_id"],
            }
        )
        return {**item, "status": status}

    def doctor(self, binding_id: str | None = None) -> dict[str, Any]:
        binding = self.store.binding(binding_id) if binding_id else None
        snapshot = self.store.effective_snapshot(binding_id) if binding_id else None
        return {
            "runtime": {
                "database": str(self.store.path),
                "journal_mode": self.store.journal_mode,
                "policy": self.store.runtime_settings(),
            },
            "source_leases": self.store.active_sources(),
            "binding": binding,
            "effective_revision": snapshot.revision if snapshot else None,
            "last_known_good": snapshot.to_document() if snapshot else None,
            "worker": dict(self.voice.status()),
            "renderer": dict(self.renderer.status(binding_id)),
        }
