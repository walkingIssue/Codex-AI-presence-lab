from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from activity import classify_activity


class ActivityClassifierTests(unittest.TestCase):
    def test_current_codex_reasoning_item_is_thinking(self) -> None:
        self.assertEqual(
            classify_activity({"type": "response_item", "payload": {"type": "reasoning"}}),
            "thinking",
        )

    def test_local_exec_and_skill_calls_are_distinct(self) -> None:
        self.assertEqual(
            classify_activity(
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "name": "exec"},
                }
            ),
            "cli",
        )
        self.assertEqual(
            classify_activity(
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "name": "skill_view"},
                }
            ),
            "skill",
        )

    def test_waiting_and_error_lifecycles_are_supported(self) -> None:
        self.assertEqual(
            classify_activity(
                {"type": "event_msg", "payload": {"type": "approval_request"}}
            ),
            "waiting",
        )
        self.assertEqual(
            classify_activity(
                {"type": "event_msg", "payload": {"type": "turn_failed"}}
            ),
            "error",
        )

    def test_completion_and_visible_final_answer_return_to_idle(self) -> None:
        self.assertEqual(
            classify_activity(
                {"type": "event_msg", "payload": {"type": "task_complete"}}
            ),
            "idle",
        )
        self.assertEqual(
            classify_activity(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "phase": "final_answer"},
                }
            ),
            "idle",
        )


if __name__ == "__main__":
    unittest.main()
