from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli_adapter import codex_executable
from codex_override import (
    OVERRIDE_SCHEMA,
    is_app_server_invocation,
    render_cmd_shim,
    render_powershell_shim,
)


class CodexOverrideTests(unittest.TestCase):
    def test_only_app_server_commands_are_routed(self) -> None:
        self.assertTrue(is_app_server_invocation(["app-server", "--stdio"]))
        self.assertTrue(is_app_server_invocation(["APP-SERVER", "--listen", "stdio://"]))
        self.assertFalse(is_app_server_invocation([]))
        self.assertFalse(is_app_server_invocation(["--help"]))

    def test_shims_forward_the_original_argument_tail(self) -> None:
        python = Path(r"C:\Python\python.exe")
        script = Path(r"C:\Users\User\.codex\skills\codex-voice\scripts\codex_override.py")
        config = Path(r"C:\Users\User\.codex\codex-voice-override.json")
        cmd = render_cmd_shim(python, script, config)
        powershell = render_powershell_shim(python, script, config)
        self.assertIn("--override-config", cmd)
        self.assertIn("%*", cmd)
        self.assertIn("@args", powershell)
        self.assertIn("codex_override.py", powershell)

    def test_codex_resolution_uses_the_recorded_real_cli_when_override_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_cli = root / "real-codex.cmd"
            real_cli.write_text("@echo off\n", encoding="utf-8")
            (root / "codex-voice-override.json").write_text(
                json.dumps({"schema": OVERRIDE_SCHEMA, "real_cli": str(real_cli)}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(root), "CODEX_CLI_PATH": ""}, clear=False):
                self.assertEqual(codex_executable(), str(real_cli))


if __name__ == "__main__":
    unittest.main()
