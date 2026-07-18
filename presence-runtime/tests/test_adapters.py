from __future__ import annotations

import sys
from pathlib import Path

from presence_runtime.adapters import ProjectAdapterManager


class FakeStore:
    def __init__(self, project: dict) -> None:
        self._project = project

    def project(self, project_id: str) -> dict:
        assert project_id == self._project["project_instance_id"]
        return self._project

    def list_projects(self) -> list[dict]:
        return [self._project]

    def active_sources(self) -> list[dict]:
        return []


def test_project_adapter_manager_owns_only_v02_project_files(tmp_path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = {
        "project_instance_id": "project-1",
        "project_root": str(project_root),
    }
    script = tmp_path / "adapter.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    manager = ProjectAdapterManager(
        FakeStore(project),
        python=Path(sys.executable),
        script=script,
    )
    try:
        status = manager.start_project(project)
        assert status["running"] is True
        state_root = project_root / ".codex-voice" / "v0.2"
        assert (state_root / "managed.json").is_file()
        unrelated = project_root / ".codex-voice" / "user-owned.txt"
        unrelated.write_text("keep\n", encoding="utf-8")
        manager.stop_project("project-1", cleanup=True)
        assert not state_root.exists()
        assert unrelated.read_text(encoding="utf-8") == "keep\n"
    finally:
        manager.close()
