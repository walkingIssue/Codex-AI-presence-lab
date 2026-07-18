from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from presence_runtime.catalog import Catalog
from presence_runtime.controller import RecordingRenderer, RuntimeController
from presence_runtime.errors import ConflictError, ValidationError
from presence_runtime.migration import HIGAN_SLOTS, LegacyMigrator
from presence_runtime.paths import presence_home
from presence_runtime.store import PresenceStore
from presence_runtime.worker import RecordingWorker


class Coordinator:
    def __init__(self) -> None:
        self.pauses = 0
        self.restarts = 0

    def pause_and_drain(self, _root: Path, *, timeout: float = 10.0):
        self.pauses += 1
        return {"playback_drained": True}

    def restart(self, _root: Path) -> None:
        self.restarts += 1


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def build_runtime(tmp_path):
    store = PresenceStore(tmp_path / "state.sqlite3")
    controller = RuntimeController(
        store=store,
        catalog=Catalog(tmp_path / "catalog"),
        voice=RecordingWorker(),
        renderer=RecordingRenderer(),
    )
    coordinator = Coordinator()
    return controller, LegacyMigrator(controller, coordinator=coordinator), coordinator


def write_legacy_project(root: Path, session_id: str) -> None:
    voice = root / ".codex-voice"
    voice.mkdir(parents=True)
    (voice / "voice").write_text("af_heart\n", encoding="utf-8")
    (voice / "speed").write_text("1.2\n", encoding="utf-8")
    (voice / "mode").write_text("stream\n", encoding="utf-8")
    (voice / "volume").write_text("44\n", encoding="utf-8")
    (voice / "commentary-volume").write_text("25\n", encoding="utf-8")
    (voice / "orb.enabled").write_text("on\n", encoding="utf-8")
    (voice / "presence-profiles.json").write_text(
        json.dumps(
            {
                "schema": "codex-ai-presence/profiles/v0.1",
                "project_profile_id": "base",
                "profiles": {
                    "base": {"avatar_id": "builtin", "voice": "af_heart"},
                    "child": {"avatar_id": "builtin", "voice": "bf_isabella"},
                },
                "sessions": {session_id: {"profile_id": "child"}},
            }
        ),
        encoding="utf-8",
    )
    (voice / "sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "mode": "session",
                "sessions": {
                    session_id: {
                        "enabled": True,
                        "project_root": str(root.resolve()),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (voice / "orb-position.json").write_text(
        json.dumps(
            {
                "windows": {
                    f"session:{session_id}|profile:child": {
                        "x": 41,
                        "y": 52,
                        "width": 500,
                        "height": 760,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    inbox = sqlite3.connect(voice / "inbox.sqlite3")
    inbox.execute(
        """
        CREATE TABLE messages(
            id INTEGER PRIMARY KEY,
            status TEXT,
            kind TEXT,
            session_id TEXT,
            event_id TEXT,
            text TEXT,
            tts_voice TEXT,
            tts_speed REAL,
            tts_mode TEXT,
            volume INTEGER
        )
        """
    )
    inbox.executemany(
        "INSERT INTO messages VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "queued", "final", session_id, "legacy-final-1", "Migrated final", None, None, None, 44),
            (2, "queued", "commentary", session_id, "legacy-update-1", "stale update", None, None, None, 11),
        ],
    )
    inbox.commit()
    inbox.close()


def test_full_migration_is_atomic_idempotent_and_explicitly_rollback_safe(tmp_path) -> None:
    controller, migrator, coordinator = build_runtime(tmp_path)
    root = tmp_path / "project"
    session_id = str(uuid.uuid4())
    write_legacy_project(root, session_id)
    project = controller.store.register_project(root)

    committed = migrator.migrate_on_registration(project["project_instance_id"], root)
    assert committed["status"] == "committed"
    assert committed["details"]["retired_commentary"] == 1
    assert committed["details"]["imported_speech"] == 1
    assert coordinator.pauses == 1

    bindings = controller.store.list_bindings(project_id=project["project_instance_id"])
    session = next(item for item in bindings if item["session_id"] == session_id)
    override = controller.store.session_override(session["binding_id"])
    assert override["profile_ref"].startswith(
        f"legacy-{project['project_instance_id'][:8]}-child@"
    )
    assert controller.store.geometry(session["binding_id"])["x"] == 41
    queued = controller.store.speech_items(binding_id=session["binding_id"])
    assert [(item["event_id"], item["status"]) for item in queued] == [
        ("legacy-final-1", "queued")
    ]

    repeated = migrator.migrate_on_registration(project["project_instance_id"], root)
    assert repeated["idempotent"] is True
    assert coordinator.pauses == 1

    rolled_back = migrator.rollback(project["project_instance_id"])
    assert rolled_back["status"] == "rolled_back"
    assert controller.store.project_default(project["project_instance_id"]) == {}
    assert controller.store.speech_items() == []
    assert controller.catalog.list_profiles() == []
    assert coordinator.restarts == 1

    retried = migrator.retry(project["project_instance_id"], root)
    assert retried["status"] == "committed"
    assert coordinator.pauses == 2


def test_partial_legacy_install_synthesizes_a_reusable_default_profile(tmp_path) -> None:
    controller, migrator, _coordinator = build_runtime(tmp_path)
    root = tmp_path / "partial"
    voice = root / ".codex-voice"
    voice.mkdir(parents=True)
    (voice / "voice").write_text("bf_isabella\n", encoding="utf-8")
    (voice / "speed").write_text("1.1\n", encoding="utf-8")
    project = controller.store.register_project(root)

    result = migrator.migrate_on_registration(project["project_instance_id"], root)
    assert result["status"] == "committed"
    assert result["details"]["legacy_detected"] is True
    profile = controller.catalog.list_profiles()[0]
    assert profile["profile_id"].endswith("-default")


def test_migration_reclaims_a_lock_owned_by_a_dead_process(tmp_path) -> None:
    controller, migrator, _coordinator = build_runtime(tmp_path)
    root = tmp_path / "stale-lock"
    voice = root / ".codex-voice"
    voice.mkdir(parents=True)
    (voice / "voice").write_text("bf_isabella\n", encoding="utf-8")
    project = controller.store.register_project(root)
    marker = (
        presence_home()
        / "migrations"
        / f"{project['project_instance_id']}.lock"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text("2147483647", encoding="ascii")

    result = migrator.migrate_on_registration(project["project_instance_id"], root)

    assert result["status"] == "committed"
    assert not marker.exists()


def test_interrupted_migration_rollback_clears_a_staged_candidate(tmp_path) -> None:
    controller, _migrator, _coordinator = build_runtime(tmp_path)
    project = controller.store.register_project(tmp_path / "interrupted")
    binding = controller.store.ensure_binding(project["project_instance_id"])
    candidate = controller.resolve_binding(binding["binding_id"])
    controller.store.stage_snapshot(candidate)

    controller.store.rollback_legacy_configuration(
        project_id=project["project_instance_id"],
        checkpoint={
            "project_default": None,
            "binding_ids": [binding["binding_id"]],
            "session_overrides": {binding["binding_id"]: None},
            "geometry": {binding["binding_id"]: None},
            "imported_event_ids": [],
        },
    )

    assert controller.store.candidate_snapshot(binding["binding_id"]) is None
    assert controller.store.binding(binding["binding_id"])["candidate_revision"] is None


def test_malformed_child_rejects_without_partial_state_or_catalog(tmp_path) -> None:
    controller, migrator, coordinator = build_runtime(tmp_path)
    root = tmp_path / "malformed"
    session_id = str(uuid.uuid4())
    write_legacy_project(root, session_id)
    profiles = root / ".codex-voice" / "presence-profiles.json"
    document = json.loads(profiles.read_text(encoding="utf-8"))
    document["sessions"][session_id] = {"profile_id": "missing"}
    profiles.write_text(json.dumps(document), encoding="utf-8")
    project = controller.store.register_project(root)

    with pytest.raises(ValidationError, match="missing profile"):
        migrator.migrate_on_registration(project["project_instance_id"], root)
    assert migrator.status(project["project_instance_id"])["status"] == "failed"
    assert controller.store.project_default(project["project_instance_id"]) == {}
    assert controller.catalog.list_profiles() == []
    assert coordinator.restarts == 1
    with pytest.raises(ConflictError, match="migrate retry"):
        migrator.migrate_on_registration(project["project_instance_id"], root)
    assert coordinator.restarts == 1


def test_higan_migration_defines_explicit_wardrobe_slots_without_global_latches(
    tmp_path,
) -> None:
    controller, _unused, coordinator = build_runtime(tmp_path)
    registry = tmp_path / "registry"
    model = registry / "higan-live2d"
    source = model / "source"
    source.mkdir(parents=True)
    (source / "Higan.model3.json").write_text("{}", encoding="utf-8")
    (model / "manifest.json").write_text(
        json.dumps(
            {
                "model": {"path": "source/Higan.model3.json"},
                "actions": [
                    {
                        "id": action_id,
                        "parameter_operations": [
                            {
                                "parameter_id": f"Param{index}",
                                "value": 1,
                                "blend": "overwrite",
                            }
                        ],
                    }
                    for index, action_id in enumerate(HIGAN_SLOTS)
                ],
            }
        ),
        encoding="utf-8",
    )
    (model / "profile.json").write_text(
        json.dumps({"renderer": {"scale": 0.8, "bottom_inset": 12}}),
        encoding="utf-8",
    )
    migrator = LegacyMigrator(
        controller,
        coordinator=coordinator,
        registry_root=registry,
    )

    pack, assets = migrator._legacy_avatar_pack("higan-live2d", tmp_path / "project")
    assert assets == source
    assert pack["safe_defaults"] == {}
    assert pack["actions"]["accessory.fur-shawl"]["slots"] == [
        "accessory.shoulders"
    ]
    assert pack["actions"]["accessory.black-stockings"]["slots"] == ["body.legs"]
    assert pack["actions"]["pose.qipao-pipe"]["slots"] == [
        "wardrobe.base",
        "body.pose",
        "gesture.arms",
        "prop.hand",
    ]
