from __future__ import annotations

import uuid

import pytest

from presence_runtime.errors import ValidationError
from presence_runtime.resolver import PresenceResolver
from presence_runtime.store import LEASE_EXPIRY_SECONDS, PresenceStore


def new_session() -> str:
    return str(uuid.uuid4())


def active_snapshot(store, binding_id, higan_pack):
    snapshot = PresenceResolver().resolve(
        binding_id=binding_id,
        revision=store.next_revision(binding_id),
        model_pack=higan_pack,
        project_patch={},
    )
    store.stage_snapshot(snapshot)
    assert store.acknowledge_snapshot(binding_id, snapshot.revision, "voice") is False
    assert store.acknowledge_snapshot(binding_id, snapshot.revision, "renderer") is True
    return store.effective_snapshot(binding_id)


def test_wal_project_identity_and_same_session_across_projects_are_isolated(
    tmp_path,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    session_id = new_session()
    first = store.register_project(tmp_path / "first")
    repeated = store.register_project(tmp_path / "first")
    second = store.register_project(tmp_path / "second")
    binding_a = store.ensure_binding(first["project_instance_id"], session_id)
    binding_b = store.ensure_binding(second["project_instance_id"], session_id)

    assert store.journal_mode == "wal"
    assert repeated["project_instance_id"] == first["project_instance_id"]
    assert binding_a["binding_id"] != binding_b["binding_id"]
    assert binding_a["session_id"] == binding_b["session_id"] == session_id


def test_project_binding_is_dedicated_and_session_overrides_persist_and_clear(
    tmp_path,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    project = store.register_project(tmp_path / "project")
    project_binding = store.ensure_binding(project["project_instance_id"])
    session_binding = store.ensure_binding(project["project_instance_id"], new_session())
    store.set_project_default(
        project["project_instance_id"],
        {"voice_id": "af_heart", "volume": 30},
    )
    store.set_session_override(session_binding["binding_id"], {"voice_id": "bf_isabella"})

    assert project_binding["scope"] == "project"
    assert project_binding["binding_id"] != session_binding["binding_id"]
    assert store.project_default(project["project_instance_id"])["volume"] == 30
    assert store.session_override(session_binding["binding_id"]) == {
        "voice_id": "bf_isabella"
    }
    store.clear_session_override(session_binding["binding_id"])
    assert store.session_override(session_binding["binding_id"]) == {}


def test_registration_rejects_malformed_session_and_spoof_fields(
    tmp_path,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    with pytest.raises(ValidationError, match="UUID"):
        store.register_source(
            adapter="codex-gui",
            project_root=tmp_path / "project",
            session_id="profile-shaped-not-a-session",
            capabilities=[],
        )
    project = store.register_project(tmp_path / "project")
    binding = store.ensure_binding(project["project_instance_id"], new_session())
    with pytest.raises(ValidationError, match="provider"):
        store.set_session_override(binding["binding_id"], {"provider": "cpu"})


def test_lease_expiry_dormants_binding_without_losing_configuration(
    tmp_path,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    registration = store.register_source(
        adapter="codex-gui",
        project_root=tmp_path / "project",
        session_id=new_session(),
        capabilities=["speech", "activity"],
        now=100,
    )
    store.set_session_override(registration["binding_id"], {"voice_id": "af_heart"})
    assert store.assert_source_active(registration["source_id"], now=110)
    expired = store.expire_leases(now=100 + LEASE_EXPIRY_SECONDS + 0.1)

    assert expired == [registration["source_id"]]
    assert store.binding(registration["binding_id"])["state"] == "dormant"
    assert store.session_override(registration["binding_id"])["voice_id"] == "af_heart"
    with pytest.raises(ValidationError, match="expired"):
        store.assert_source_active(
            registration["source_id"],
            binding_id=registration["binding_id"],
            now=200,
        )
    store.refresh_lease(
        registration["source_id"],
        registration["lease_token"],
        now=201,
    )
    assert store.binding(registration["binding_id"])["state"] == "active"


def test_snapshot_ack_queue_stability_dedup_and_binding_removal(
    tmp_path,
    higan_pack: dict,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    registration = store.register_source(
        adapter="codex-gui",
        project_root=tmp_path / "project",
        session_id=new_session(),
        capabilities=["speech"],
    )
    binding_id = registration["binding_id"]
    first = active_snapshot(store, binding_id, higan_pack)
    queue_id = store.enqueue_speech(
        source_id=registration["source_id"],
        binding_id=binding_id,
        effective_revision=first.revision,
        utterance_id=str(uuid.uuid4()),
        event_id="final:event:1",
        text="This setting must stay fixed.",
        kind="final",
        tts={
            "voice_id": "af_heart",
            "speed": 1.1,
            "playback_mode": "stream",
            "volume": 40,
        },
    )
    duplicate = store.enqueue_speech(
        source_id=registration["source_id"],
        binding_id=binding_id,
        effective_revision=first.revision,
        utterance_id=str(uuid.uuid4()),
        event_id="final:event:1",
        text="duplicate",
        kind="final",
        tts={"voice_id": "bf_isabella"},
    )
    second = PresenceResolver({"voice_id": "bf_isabella"}).resolve(
        binding_id=binding_id,
        revision=store.next_revision(binding_id),
        model_pack=higan_pack,
    )
    store.stage_snapshot(second)
    store.acknowledge_snapshot(binding_id, second.revision, "voice")
    store.acknowledge_snapshot(binding_id, second.revision, "renderer")
    claimed = store.claim_next_speech()

    assert queue_id is not None
    assert duplicate is None
    assert claimed["binding_id"] == binding_id
    assert claimed["tts"]["voice_id"] == "af_heart"
    assert store.effective_snapshot(binding_id).tts.voice_id == "bf_isabella"
    assert store.remove_binding(binding_id) == 1
    item = store.speech_items(binding_id=binding_id)[0]
    assert item["status"] == "cancelled"
    assert item["cancel_reason"] == "binding removed"


def test_geometry_is_keyed_by_binding_not_profile(
    tmp_path,
) -> None:
    store = PresenceStore(tmp_path / "state.sqlite3")
    project = store.register_project(tmp_path / "project")
    first = store.ensure_binding(project["project_instance_id"], new_session())
    second = store.ensure_binding(project["project_instance_id"], new_session())
    store.set_geometry(first["binding_id"], {"x": 10, "y": 20, "width": 400, "height": 600})

    assert store.geometry(first["binding_id"])["x"] == 10
    assert store.geometry(second["binding_id"]) is None
