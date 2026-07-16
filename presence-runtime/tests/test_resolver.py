from __future__ import annotations

import uuid

import pytest

from presence_runtime.errors import ValidationError
from presence_runtime.resolver import PresenceResolver


def binding_id() -> str:
    return str(uuid.uuid4())


def resolve(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
    *,
    project: dict | None = None,
    session: dict | None = None,
    activity: str | None = None,
):
    project_patch = {
        "profile_ref": "higan-default",
        **(project or {}),
    }
    return PresenceResolver().resolve(
        binding_id=binding_id(),
        revision=1,
        model_pack=higan_pack,
        profile=higan_profile,
        preset=higan_preset,
        project_patch=project_patch,
        session_patch=session or {},
        activity=activity,
    )


def test_sparse_session_voice_override_inherits_every_other_field(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    snapshot = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        session={"voice_id": "bf_isabella"},
    )

    assert snapshot.tts.voice_id == "bf_isabella"
    assert snapshot.tts.speed == 1.15
    assert snapshot.tts.volume == 64
    assert snapshot.avatar_ref == "higan@3"
    assert snapshot.profile_ref == "higan-default@4"
    assert snapshot.preset_ref == "plain-sweater@2"
    assert snapshot.semantic.persistent_actions == (
        "wardrobe.sweater",
        "pose.sweater-default",
    )
    assert snapshot.provenance["tts.voice_id"] == "session"
    assert snapshot.provenance["tts.speed"] == "profile"


def test_empty_slot_lists_clear_only_the_selected_parent_slots(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    snapshot = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        session={"semantic": {"slots": {"body.pose": []}}},
    )
    assert snapshot.semantic.persistent_actions == ("wardrobe.sweater",)


def test_project_changes_flow_to_inheriting_children_but_not_explicit_overrides(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    inherited = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        project={"volume": 40},
        session={"voice_id": "bf_isabella"},
    )
    explicit = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        project={"volume": 40},
        session={"volume": 77},
    )
    updated_inherited = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        project={"volume": 52},
        session={"voice_id": "bf_isabella"},
    )
    updated_explicit = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        project={"volume": 52},
        session={"volume": 77},
    )

    assert inherited.tts.volume == 40
    assert updated_inherited.tts.volume == 52
    assert explicit.tts.volume == updated_explicit.tts.volume == 77


def test_multislot_activity_action_evicts_conflicting_pose_and_restores_exact_state(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    cli = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        activity="cli",
    )
    idle = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        activity=None,
    )

    assert cli.semantic.persistent_actions == (
        "wardrobe.sweater",
        "pose.sweater-default",
    )
    assert cli.semantic.effective_actions == (
        "wardrobe.sweater",
        "pose.pipe",
    )
    assert idle.semantic.effective_actions == idle.semantic.persistent_actions


def test_independent_error_actions_compose(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    snapshot = resolve(
        higan_pack,
        higan_profile,
        higan_preset,
        activity="error",
    )
    assert snapshot.semantic.effective_actions == (
        "wardrobe.sweater",
        "pose.sweater-default",
        "mouth.unhappy",
        "effect.dark-face",
    )


def test_invalid_child_retains_last_known_good_with_visible_diagnostic(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    resolver = PresenceResolver()
    stable = resolver.resolve(
        binding_id=binding_id(),
        revision=1,
        model_pack=higan_pack,
        profile=higan_profile,
        preset=higan_preset,
        project_patch={"profile_ref": "higan-default"},
    ).acknowledged()
    candidate = resolver.resolve_or_last_known_good(
        last_known_good=stable,
        binding_id=stable.binding_id,
        revision=2,
        model_pack=higan_pack,
        profile=higan_profile,
        preset=higan_preset,
        project_patch={"profile_ref": "higan-default"},
        session_patch={"speed": "extremely fast"},
    )

    assert candidate.revision == 1
    assert candidate.last_known_good is True
    assert candidate.valid is False
    assert "session.speed" in candidate.diagnostics[0]


def test_provider_and_routing_authority_are_rejected_from_children(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    with pytest.raises(ValidationError, match="provider"):
        resolve(
            higan_pack,
            higan_profile,
            higan_preset,
            session={"provider": "directml"},
        )
    with pytest.raises(ValidationError, match="orb_port"):
        resolve(
            higan_pack,
            higan_profile,
            higan_preset,
            session={"orb_port": 4567},
        )


def test_missing_or_mismatched_catalog_reference_never_creates_ghost_renderer(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    with pytest.raises(ValidationError, match="selected but not loaded"):
        PresenceResolver().resolve(
            binding_id=binding_id(),
            revision=1,
            model_pack=higan_pack,
            profile=higan_profile,
            preset=None,
            project_patch={"profile_ref": "higan-default"},
        )
    with pytest.raises(ValidationError, match="does not match"):
        resolve(
            higan_pack,
            higan_profile,
            higan_preset,
            session={"avatar_ref": "some-other-avatar"},
        )


def test_renderer_payload_is_resolved_and_contains_no_raw_controls(
    higan_pack: dict,
    higan_profile: dict,
    higan_preset: dict,
) -> None:
    snapshot = resolve(higan_pack, higan_profile, higan_preset, activity="thinking")
    payload = PresenceResolver.renderer_document(snapshot)
    rendered = repr(payload)
    assert "operations" not in rendered
    assert "profile_ref" not in payload
    assert "provenance" not in payload
    assert payload["binding_id"] == snapshot.binding_id
    assert payload["semantic"]["effective_actions"][-1] == "gesture.hand-mouth"

