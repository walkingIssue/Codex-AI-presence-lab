from __future__ import annotations

import uuid

import pytest

from presence_runtime.catalog import Catalog
from presence_runtime.cli import build_parser, command_operation
from presence_runtime.control import ControlAPI
from presence_runtime.controller import RecordingRenderer, RuntimeController
from presence_runtime.errors import ValidationError
from presence_runtime.store import PresenceStore
from presence_runtime.worker import RecordingWorker


def build_control(tmp_path, *, input_available: bool = False):
    store = PresenceStore(tmp_path / "state.sqlite3")
    catalog = Catalog(tmp_path / "catalog")
    voice = RecordingWorker()
    renderer = RecordingRenderer()
    controller = RuntimeController(
        store=store,
        catalog=catalog,
        voice=voice,
        renderer=renderer,
        input_status=lambda: {"installed": input_available, "ready": False},
    )
    control = ControlAPI(
        controller,
        available_providers={"cpu", "directml"},
        running_provider="cpu",
        input_available=input_available,
    )
    return control, controller


def test_runtime_policy_is_machine_wide_and_reports_required_restart(tmp_path) -> None:
    control, controller = build_control(tmp_path)
    with pytest.raises(ValidationError, match="not installed"):
        control.execute("runtime.set_policy", {"microphone_permission": True})
    with pytest.raises(ValidationError, match="available providers"):
        control.execute("runtime.set_policy", {"provider": "cuda"})

    changed = control.execute("runtime.set_policy", {"provider": "directml"})
    assert changed["provider"] == "directml"
    assert changed["restart_required"] is True
    assert controller.doctor()["voice_input"]["installed"] is False


def test_mutations_require_one_explicit_project_or_session_scope(tmp_path) -> None:
    control, controller = build_control(tmp_path)
    project = controller.store.register_project(tmp_path / "project")
    session_id = str(uuid.uuid4())

    with pytest.raises(ValidationError, match="explicit project or session"):
        control.execute("avatar.use", {"reference": "builtin"})
    binding = controller.store.ensure_binding(project["project_instance_id"], session_id)
    with pytest.raises(ValidationError, match="may not be combined"):
        control.execute(
            "avatar.use",
            {
                "reference": "builtin",
                "binding_id": binding["binding_id"],
                "project_id": project["project_instance_id"],
            },
        )

    result = control.execute(
        "avatar.use",
        {
            "reference": "builtin",
            "project_id": project["project_instance_id"],
            "session_id": session_id,
        },
    )
    assert result[0]["binding_id"] == binding["binding_id"]


def test_cli_preserves_provider_on_reinstall_and_builds_composite_session_scope() -> None:
    parser = build_parser()
    install = parser.parse_args(["runtime", "install", "--no-start"])
    assert install.provider is None

    session_id = str(uuid.uuid4())
    args = parser.parse_args(
        [
            "session",
            "set",
            "--project",
            "/tmp/project",
            "--session",
            session_id,
            "--voice",
            "af_heart",
        ]
    )
    operation, arguments = command_operation(args)
    assert operation == "session.set"
    assert arguments["session_id"] == session_id
    assert arguments["project_root"] == "/tmp/project"
    assert arguments["changes"] == {"voice_id": "af_heart"}
