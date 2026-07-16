from __future__ import annotations

import json
import sys

from presence_runtime.catalog import Catalog
from presence_runtime.renderer import ElectronRendererSupervisor
from presence_runtime.resolver import PresenceResolver, builtin_model_pack
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
    elif kind == "event":
        response = {"type": "response", "id": command["id"], "ok": True, "result": {"routed": True}}
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
    assert renderer.set_binding_active(binding["binding_id"], False) is True
    assert renderer.remove_binding(binding["binding_id"]) is True
    renderer.close()
    assert renderer.status()["running"] is False
