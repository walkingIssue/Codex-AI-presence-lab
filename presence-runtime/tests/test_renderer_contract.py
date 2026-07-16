from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_python_renderer_fixture_is_consumed_without_js_resolution() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    fixture = ROOT / "fixtures" / "renderer-effective-v0.2.json"
    contract = ROOT / "renderer-host" / "snapshot_contract.cjs"
    script = """
const fs = require("fs");
const contract = require(process.argv[1]);
const value = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const accepted = contract.acceptEffectiveSnapshot(value);
process.stdout.write(JSON.stringify({
  frozen: Object.isFrozen(accepted) && Object.isFrozen(accepted.semantic),
  binding_id: accepted.binding_id,
  actions: accepted.semantic.effective_actions,
}));
"""
    result = subprocess.run(
        [node, "-e", script, str(contract), str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    accepted = json.loads(result.stdout)
    source = json.loads(fixture.read_text(encoding="utf-8"))
    assert accepted["frozen"] is True
    assert accepted["binding_id"] == source["binding_id"]
    assert accepted["actions"] == source["semantic"]["effective_actions"]


def test_renderer_contract_rejects_raw_profile_documents() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    contract = ROOT / "renderer-host" / "snapshot_contract.cjs"
    script = """
const contract = require(process.argv[1]);
try {
  contract.acceptEffectiveSnapshot({
    schema: "presence/renderer-snapshot/v0.2",
    profile: { voice_id: "should-not-resolve-here" },
  });
  process.exit(9);
} catch (error) {
  process.stdout.write(error.message);
}
"""
    result = subprocess.run(
        [node, "-e", script, str(contract)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "unresolved fields" in result.stdout
