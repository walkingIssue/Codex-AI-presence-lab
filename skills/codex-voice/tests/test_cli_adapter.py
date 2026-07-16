from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli_adapter import command_args, prepare_command
from delivery import AppServerClient


class StartupProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO('{"id":1,"result":{}}\n')

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class CliAdapterTests(unittest.TestCase):
    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            command_args("   ")

    @unittest.skipUnless(os.name == "nt", "Windows command-line parsing regression")
    def test_windows_command_line_round_trips_quoted_paths_and_arguments(self) -> None:
        expected = [
            r"C:\Program Files\Codex\codex.exe",
            "app-server",
            "--listen",
            "stdio://",
            "hello world",
            'quote"inside',
        ]
        self.assertEqual(command_args(subprocess.list2cmdline(expected)), expected)

    @unittest.skipUnless(os.name == "nt", "Windows wrapper regression")
    def test_windows_cmd_shim_is_launched_through_cmd_without_rewriting_arguments(self) -> None:
        command = [r"C:\Program Files\Codex\codex.cmd", "app-server", "hello world"]
        self.assertEqual(
            prepare_command(command),
            ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline(command)],
        )

    def test_gui_app_server_startup_uses_shared_cli_boundary(self) -> None:
        process = StartupProcess()
        executable = r"C:\Program Files\Codex\codex.cmd" if os.name == "nt" else "codex-test"
        with tempfile.TemporaryDirectory() as directory:
            client = AppServerClient(Path(directory), timeout_seconds=2)
            with patch("delivery.codex_executable", return_value=executable), patch(
                "delivery.subprocess.Popen", return_value=process
            ) as popen:
                client._start()
                client.close()

        command = popen.call_args.args[0]
        self.assertEqual(command, prepare_command([executable, "app-server", "--stdio"]))
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
