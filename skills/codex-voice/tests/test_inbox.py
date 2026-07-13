from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inbox import Inbox, stable_event_id


def message(event_id: str, session_id: str = "session-a") -> dict[str, object]:
    return {
        "schema": "codex-voice/message/v0.1",
        "event_id": event_id,
        "project_root": "C:/project",
        "session_id": session_id,
        "thread_id": session_id,
        "turn_id": "turn-1",
        "session_label": session_id,
        "kind": "final",
        "text": f"message {event_id}",
        "sequence": 1,
        "volume": 100,
    }


class InboxTests(unittest.TestCase):
    def test_deduplicates_and_requeues_interrupted_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            event_id = stable_event_id("rollout", "turn-1", "final", "hello")
            self.assertTrue(inbox.enqueue(message(event_id)))
            self.assertFalse(inbox.enqueue(message(event_id)))

            claimed = inbox.claim_next("session-a")
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["event_id"], event_id)
            inbox.requeue(event_id, error="interrupted_for_voice_input")

            replay = inbox.claim_next("session-a")
            self.assertIsNotNone(replay)
            self.assertEqual(replay["replay_count"], 1)
            inbox.complete(event_id)
            self.assertEqual(inbox.status()["messages"], {"played": 1})

    def test_resume_text_survives_requeue_and_clears_on_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            inbox.enqueue(message("resume-1"))
            inbox.claim_next("session-a")
            inbox.requeue("resume-1", resume_text="remaining words", resume_offset=12)
            replay = inbox.claim_next("session-a")
            self.assertEqual(replay["resume_text"], "remaining words")
            self.assertEqual(replay["resume_offset"], 12)
            inbox.complete("resume-1")
            with inbox.connection() as connection:
                row = connection.execute(
                    "SELECT resume_text, resume_offset FROM messages WHERE event_id = ?",
                    ("resume-1",),
                ).fetchone()
            self.assertIsNone(row["resume_text"])
            self.assertIsNone(row["resume_offset"])

    def test_fifo_and_focus_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            inbox.enqueue(message("a-1", "session-a"))
            inbox.enqueue(message("b-1", "session-b"))
            inbox.enqueue(message("a-2", "session-a"))

            first = inbox.claim_next("session-a")
            self.assertEqual(first["event_id"], "a-1")
            inbox.complete("a-1")
            second = inbox.claim_next("session-a")
            self.assertEqual(second["event_id"], "a-2")

            command_id = inbox.add_control("capture-start", {"target_session_id": "session-a"})
            controls = inbox.consume_controls()
            self.assertEqual(controls[0]["id"], command_id)
            self.assertEqual(controls[0]["payload"]["target_session_id"], "session-a")
            self.assertEqual(inbox.consume_controls(), [])

    def test_focus_state_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.sqlite3"
            Inbox(path).set_state("focus", {"state": "target-response", "session_id": "session-a"})
            reopened = Inbox(path)
            self.assertEqual(
                reopened.get_state("focus"),
                {"state": "target-response", "session_id": "session-a"},
            )

    def test_runtime_counter_is_atomic_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inbox.sqlite3"
            inbox = Inbox(path)

            self.assertEqual(inbox.next_counter("capture"), 1)
            self.assertEqual(Inbox(path).next_counter("capture"), 2)
            self.assertEqual(inbox.get_state("capture"), 2)

    def test_recover_inflight_requeues_interrupted_playback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            inbox.enqueue(message("playing-1"))
            claimed = inbox.claim_next("session-a")
            self.assertEqual(claimed["event_id"], "playing-1")
            self.assertEqual(inbox.recover_inflight(), 1)
            recovered = inbox.claim_next("session-a")
            self.assertEqual(recovered["event_id"], "playing-1")
            self.assertEqual(recovered["replay_count"], 1)

    def test_recover_input_state_releases_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            inbox.set_state("focus", {"state": "submitting", "session_id": "session-a"})
            recovered = inbox.recover_input_state()
            self.assertEqual(recovered["state"], "submitting")
            self.assertEqual(inbox.get_state("focus"), {"state": "idle"})
            self.assertEqual(inbox.get_state("input")["state"], "idle")


if __name__ == "__main__":
    unittest.main()
