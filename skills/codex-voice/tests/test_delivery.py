from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from delivery import AppServerClient


class RecordingStdin(io.StringIO):
    def close(self) -> None:
        self.flush()


class FakeProcess:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.stdin = RecordingStdin()
        self.stdout = io.StringIO("\n".join(json.dumps(message) for message in messages) + "\n")

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


class DeliveryTests(unittest.TestCase):
    def run_exchange(self, resume_result: dict[str, object], turn_result: dict[str, object]) -> tuple[dict[str, object], FakeProcess]:
        process = FakeProcess(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"thread": resume_result}},
                {"id": 3, "result": turn_result},
                {"method": "turn/completed", "params": {"turn": {"id": "turn-2"}}},
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            client = AppServerClient(Path(directory), timeout_seconds=2)
            with patch("delivery.codex_executable", return_value="codex-test"), patch(
                "delivery.subprocess.Popen", return_value=process
            ):
                result = client.submit("thread-1", "say hello", wait_for_completion=True)
        return result, process

    def test_turn_start_submits_normal_user_text(self) -> None:
        result, process = self.run_exchange({"status": "idle"}, {"turn": {"id": "turn-2"}})
        self.assertEqual(result["method"], "turn/start")
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        turn_start = requests[-1]
        self.assertEqual(turn_start["method"], "turn/start")
        self.assertEqual(turn_start["params"]["input"], [{"type": "text", "text": "say hello"}])
        self.assertNotIn("system", json.dumps(turn_start))

    def test_active_turn_uses_turn_steer(self) -> None:
        result, process = self.run_exchange(
            {"status": "inProgress", "activeTurnId": "turn-2"},
            {"turnId": "turn-2"},
        )
        self.assertEqual(result["method"], "turn/steer")
        requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
        steer = requests[-1]
        self.assertEqual(steer["method"], "turn/steer")
        self.assertEqual(steer["params"]["expectedTurnId"], "turn-2")


if __name__ == "__main__":
    unittest.main()
