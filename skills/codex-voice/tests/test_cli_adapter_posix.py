from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli_adapter import command_args, prepare_command


class PosixCliAdapterTests(unittest.TestCase):
    def test_posix_command_parsing_and_launch_remain_shell_free(self) -> None:
        with patch("cli_adapter.os.name", "posix"):
            self.assertEqual(
                command_args("python -c \"print('linux')\""),
                ["python", "-c", "print('linux')"],
            )
            command = ["codex", "app-server", "--listen", "stdio://"]
            self.assertEqual(prepare_command(command), command)


if __name__ == "__main__":
    unittest.main()
