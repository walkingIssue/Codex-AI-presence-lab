from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock

from presence_runtime.catalog import Catalog
from presence_runtime.renderer import ElectronRendererSupervisor
from presence_runtime.resolver import PresenceResolver, builtin_model_pack
from presence_runtime.presentation import PresentationCue
from presence_runtime.store import PresenceStore


def test_one_renderer_root_acknowledges_binding_and_persists_geometry(tmp_path) -> None:
    host = tmp_path / "renderer-host"
    host.mkdir()
    fake = host / "fake_renderer.py"
    fake.write_text(
        """
import json
import sys

windows = {}
print(json.dumps({"type": "renderer/ready", "root_pid": 123, "udp_port": 19444}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    kind = command.get("type")
    if kind == "snapshot":
        snapshot = command["snapshot"]
        if snapshot["avatar_ref"].startswith("reject"):
            response = {"type": "response", "id": command["id"], "ok": False, "error": "preload rejected"}
        else:
            windows[snapshot["binding_id"]] = snapshot
            print(json.dumps({
                "type": "renderer/geometry",
                "binding_id": snapshot["binding_id"],
                "geometry": {"x": 31, "y": 47, "width": 420, "height": 640},
            }), flush=True)
            response = {
                "type": "response",
                "id": command["id"],
                "ok": True,
                "result": {
                    "binding_id": snapshot["binding_id"],
                    "revision": snapshot["revision"],
                    "renderer_key": snapshot["avatar_ref"],
                },
            }
    elif kind in {"event", "activity"}:
        response = {"type": "response", "id": command["id"], "ok": True, "result": {"routed": True}}
    elif kind == "presentation":
        cue = command["cue"]
        response = {
            "type": "response",
            "id": command["id"],
            "ok": True,
            "result": {
                "binding_id": cue["binding_id"],
                "configuration_revision": cue["configuration_revision"],
                "presentation_sequence": cue["sequence"],
                "status": "completed",
            },
        }
    elif kind == "presentation-cancel":
        response = {"type": "response", "id": command["id"], "ok": True, "result": {"found": True}}
    elif kind == "status":
        response = {
            "type": "response",
            "id": command["id"],
            "ok": True,
            "result": {"root_pid": 123, "windows": list(windows)},
        }
    elif kind == "binding-state":
        response = {"type": "response", "id": command["id"], "ok": True, "result": {"found": True}}
    elif kind == "remove":
        windows.pop(command["binding_id"], None)
        response = {"type": "response", "id": command["id"], "ok": True, "result": {"removed": True}}
    elif kind == "shutdown":
        print(json.dumps({"type": "response", "id": command["id"], "ok": True, "result": {}}), flush=True)
        break
    else:
        response = {"type": "response", "id": command["id"], "ok": False, "error": "unknown"}
    print(json.dumps(response), flush=True)
""",
        encoding="utf-8",
    )
    store = PresenceStore(tmp_path / "state.sqlite3")
    project = store.register_project(tmp_path / "project")
    binding = store.ensure_binding(project["project_instance_id"])
    catalog = Catalog(tmp_path / "catalog")
    renderer = ElectronRendererSupervisor(
        host_root=host,
        catalog=catalog,
        store=store,
        command=[sys.executable, str(fake)],
    )
    snapshot = PresenceResolver().resolve(
        binding_id=binding["binding_id"],
        revision=1,
        model_pack=builtin_model_pack(),
    )

    assert renderer.apply_snapshot(snapshot) is True
    assert renderer.status(binding["binding_id"])["root_pid"] == 123
    assert renderer.status(binding["binding_id"])["acknowledged_revision"] == 1
    assert store.geometry(binding["binding_id"]) == {
        "x": 31,
        "y": 47,
        "width": 420,
        "height": 640,
    }
    renderer.playback_event(
        {
            "type": "voice-output",
            "state": "started",
            "binding_id": binding["binding_id"],
            "utterance_id": "utterance-1",
        }
    )
    assert renderer.activity_event(
        {
            "type": "activity",
            "state": "thinking",
            "binding_id": binding["binding_id"],
            "event_id": "activity:1",
        }
    ) is True
    cue = PresentationCue(
        binding_id=binding["binding_id"],
        configuration_revision=1,
        sequence=1,
        event_id="activity:1",
        activity="thinking",
        base_actions=(),
        target_actions=("pose.thinking",),
    )
    assert renderer.apply_presentation(cue) == "completed"
    assert renderer.cancel_presentation(binding["binding_id"]) is True
    assert renderer.set_binding_active(binding["binding_id"], False) is True
    assert renderer.remove_binding(binding["binding_id"]) is True
    renderer.close()
    assert renderer.status()["running"] is False


def test_windows_force_stop_targets_only_the_managed_renderer_tree(monkeypatch) -> None:
    process = Mock()
    process.pid = 4242
    process.poll.side_effect = [None, 1]
    invoked = []
    monkeypatch.setattr("presence_runtime.renderer.os.name", "nt")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: invoked.append(command),
    )

    ElectronRendererSupervisor._force_stop_process(process)

    assert invoked == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_renderer_cleanup_removes_only_its_pid_scoped_data(tmp_path, monkeypatch) -> None:
    own = tmp_path / "codex-presence-renderer-host-4242"
    sibling = tmp_path / "codex-presence-renderer-host-4343"
    own.mkdir()
    sibling.mkdir()
    (own / "lockfile").write_text("released", encoding="utf-8")
    monkeypatch.setattr("presence_runtime.renderer.tempfile.gettempdir", lambda: str(tmp_path))

    ElectronRendererSupervisor._cleanup_process_data(4242)

    assert not own.exists()
    assert sibling.is_dir()
