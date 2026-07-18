from __future__ import annotations

import importlib.util
from pathlib import Path


def test_machine_manifest_is_valid_and_human_projections_match() -> None:
    repo = Path(__file__).resolve().parents[2]
    script = repo / "tools" / "runtime_manifest.py"
    spec = importlib.util.spec_from_file_location("runtime_manifest", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    document = module.load_manifest(repo / "presence-runtime" / "runtime-manifest.json")
    rendered = module.render(document)
    assert (repo / "presence-runtime" / "RUNTIME-MANIFEST.md").read_text(
        encoding="utf-8"
    ) == rendered
    assert (repo / "skills" / "codex-voice" / "RUNTIME-MANIFEST.md").read_text(
        encoding="utf-8"
    ) == rendered
