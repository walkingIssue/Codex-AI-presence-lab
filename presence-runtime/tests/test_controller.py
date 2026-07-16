from __future__ import annotations

import uuid

import pytest

from presence_runtime.catalog import Catalog
from presence_runtime.controller import RecordingRenderer, RuntimeController
from presence_runtime.errors import ConflictError
from presence_runtime.store import PresenceStore
from presence_runtime.worker import RecordingWorker


def runtime(tmp_path, higan_pack):
    store = PresenceStore(tmp_path / "state.sqlite3")
    catalog = Catalog(tmp_path / "catalog")
    catalog.register_avatar(higan_pack)
    preset = catalog.put_preset(
        {
            "preset_id": "plain",
            "compatible_model_fingerprints": [higan_pack["model_fingerprint"]],
            "semantic": {
                "slots": {
                    "accessory.shoulders": [],
                    "body.legs": [],
                }
            },
        }
    )
    profile = catalog.put_profile(
        {
            "profile_id": "higan-default",
            "voice_id": "af_heart",
            "speed": 1.1,
            "volume": 50,
            "avatar_ref": "higan",
            "preset_ref": f"plain@{preset['revision']}",
        }
    )
    voice = RecordingWorker()
    renderer = RecordingRenderer()
    controller = RuntimeController(
        store=store,
        catalog=catalog,
        voice=voice,
        renderer=renderer,
    )
    return controller, profile, voice, renderer


def register(controller, root, session_id=None):
    return controller.store.register_source(
        adapter="codex-gui",
        project_root=root,
        session_id=session_id or str(uuid.uuid4()),
        capabilities=["speech", "activity"],
    )


def test_project_and_session_config_promote_only_after_both_consumer_acks(
    tmp_path,
    higan_pack,
) -> None:
    controller, _profile, _voice, renderer = runtime(tmp_path, higan_pack)
    first = register(controller, tmp_path / "project")
    second = register(controller, tmp_path / "project")
    controller.ensure_effective(first["binding_id"])
    controller.ensure_effective(second["binding_id"])
    project_id = first["project_instance_id"]

    promoted = controller.set_project_default(
        project_id,
        {"profile_ref": "higan-default", "volume": 42},
    )
    assert {item.binding_id for item in promoted} >= {
        first["binding_id"],
        second["binding_id"],
    }
    assert all(item.avatar_ref == "higan@3" for item in promoted)
    assert all(
        "accessory.shawl" not in item.semantic.persistent_actions
        and "legs.stockings" not in item.semantic.persistent_actions
        for item in promoted
    )

    explicit = controller.set_session_override(
        second["binding_id"],
        {"volume": 81},
    )
    assert explicit.tts.volume == 81
    controller.set_project_default(
        project_id,
        {"profile_ref": "higan-default", "volume": 25},
    )
    assert controller.store.effective_snapshot(first["binding_id"]).tts.volume == 25
    assert controller.store.effective_snapshot(second["binding_id"]).tts.volume == 81

    previous = controller.store.project_default(project_id)
    previous_snapshot = controller.store.effective_snapshot(first["binding_id"])
    renderer.ready = False
    with pytest.raises(ConflictError, match="renderer rejected"):
        controller.set_project_default(
            project_id,
            {"profile_ref": "higan-default", "volume": 5},
        )
    assert controller.store.project_default(project_id) == previous
    restored = controller.store.effective_snapshot(first["binding_id"])
    assert restored.revision == previous_snapshot.revision
    assert restored.tts.volume == previous_snapshot.tts.volume


def test_clearing_session_override_immediately_restores_project_inheritance(
    tmp_path,
    higan_pack,
) -> None:
    controller, _profile, _voice, _renderer = runtime(tmp_path, higan_pack)
    source = register(controller, tmp_path / "project")
    controller.ensure_effective(source["binding_id"])
    controller.set_project_default(
        source["project_instance_id"],
        {"profile_ref": "higan-default", "voice_id": "af_heart"},
    )
    controller.set_session_override(
        source["binding_id"],
        {"voice_id": "bf_isabella"},
    )
    cleared = controller.set_session_override(source["binding_id"], None)

    assert controller.store.session_override(source["binding_id"]) == {}
    assert cleared.tts.voice_id == "af_heart"


def test_queued_tts_is_immutable_but_playback_uses_stable_current_binding(
    tmp_path,
    higan_pack,
) -> None:
    controller, _profile, voice, renderer = runtime(tmp_path, higan_pack)
    source = register(controller, tmp_path / "project")
    controller.ensure_effective(source["binding_id"])
    controller.set_project_default(
        source["project_instance_id"],
        {"profile_ref": "higan-default"},
    )
    queue_id = controller.enqueue_speech(
        source_id=source["source_id"],
        binding_id=source["binding_id"],
        event_id="final:stable",
        utterance_id=str(uuid.uuid4()),
        text="Keep the old voice, follow the same binding.",
        kind="final",
    )
    controller.set_session_override(
        source["binding_id"],
        {
            "voice_id": "bf_isabella",
            "avatar_ref": "builtin",
            "preset_ref": None,
        },
    )
    played = controller.play_next()

    assert queue_id is not None
    assert played["binding_id"] == source["binding_id"]
    assert played["tts"]["voice_id"] == "af_heart"
    assert controller.store.effective_snapshot(source["binding_id"]).tts.voice_id == "bf_isabella"
    assert voice.items[-1]["binding_id"] == source["binding_id"]
    assert {
        event["binding_id"]
        for event in renderer.playback
    } == {source["binding_id"]}
    assert {
        event["utterance_id"]
        for event in renderer.playback
    } == {played["utterance_id"]}


def test_activity_overlay_is_recomputed_without_changing_configuration_revision(
    tmp_path,
    higan_pack,
) -> None:
    controller, _profile, _voice, renderer = runtime(tmp_path, higan_pack)
    source = register(controller, tmp_path / "project")
    controller.ensure_effective(source["binding_id"])
    controller.set_project_default(
        source["project_instance_id"],
        {"profile_ref": "higan-default"},
    )
    persistent = controller.store.effective_snapshot(source["binding_id"])
    overlay = controller.set_activity(
        source_id=source["source_id"],
        binding_id=source["binding_id"],
        event_id="activity:cli",
        activity="cli",
    )
    idle = controller.set_activity(
        source_id=source["source_id"],
        binding_id=source["binding_id"],
        event_id="activity:idle",
        activity="idle",
    )

    assert overlay.revision == persistent.revision
    assert "pose.pipe" in overlay.semantic.effective_actions
    assert "pose.sweater-default" not in overlay.semantic.effective_actions
    assert idle.semantic.effective_actions == persistent.semantic.persistent_actions
    assert len(renderer.activities) == 2
