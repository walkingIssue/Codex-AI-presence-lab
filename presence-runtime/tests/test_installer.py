from __future__ import annotations

import uuid
import sys
from pathlib import Path

import pytest

from presence_runtime.errors import ConflictError
from presence_runtime.installer import _electron_executable, _write_launchers, uninstall
from presence_runtime.managed import INSTALLATION_SCHEMA, atomic_json, file_inventory, sha256_file
from presence_runtime.store import PresenceStore


def test_electron_runtime_path_is_platform_specific(tmp_path) -> None:
    candidate = _electron_executable(tmp_path)
    if __import__("os").name == "nt":
        assert candidate == tmp_path / "node_modules" / "electron" / "dist" / "electron.exe"
    elif __import__("sys").platform == "darwin":
        assert candidate.name == "Electron"
    else:
        assert candidate == tmp_path / "node_modules" / "electron" / "dist" / "electron"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows launcher contract")
def test_windows_launcher_uses_external_python_bootstrap(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    launchers = _write_launchers(Path(sys.executable))

    assert {path.name for path in launchers} == {
        "presence.cmd",
        "presence.ps1",
        "presence.py",
    }
    command = (tmp_path / "codex-home" / "bin" / "presence.cmd").read_text(
        encoding="utf-8"
    )
    assert "presence.py" in command
    assert "-m presence_runtime.cli" not in command


def test_uninstall_guards_active_sources_and_preserves_state_and_catalog(
    tmp_path, monkeypatch
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    home = codex_home / "presence"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    managed_file = runtime / "owned.py"
    managed_file.write_text("owned = True\n", encoding="utf-8")
    catalog = home / "catalog"
    catalog.mkdir()
    (catalog / "profile.json").write_text("{}\n", encoding="utf-8")
    launcher = codex_home / "bin" / "presence.cmd"
    launcher.parent.mkdir()
    launcher.write_text("@echo off\n", encoding="utf-8")

    store = PresenceStore(home / "state.sqlite3")
    registration = store.register_source(
        adapter="test",
        project_root=tmp_path / "project",
        session_id=str(uuid.uuid4()),
        capabilities=[],
    )
    store.close()
    atomic_json(
        home / "installation.json",
        {
            "schema": INSTALLATION_SCHEMA,
            "managed_files": file_inventory(home, [managed_file]),
            "managed_external_files": {str(launcher.resolve()): sha256_file(launcher)},
            "owned_directories": ["runtime"],
        },
    )

    with pytest.raises(ConflictError, match="active sources"):
        uninstall()

    store = PresenceStore(home / "state.sqlite3")
    store.disconnect_source(registration["source_id"])
    store.close()
    result = uninstall()

    assert result == {
        "uninstalled": True,
        "state_preserved": True,
        "catalog_preserved": True,
    }
    assert not runtime.exists()
    assert not launcher.exists()
    assert (home / "state.sqlite3").is_file()
    assert (catalog / "profile.json").is_file()
