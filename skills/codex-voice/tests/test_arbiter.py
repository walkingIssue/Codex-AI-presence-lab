from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inbox import Inbox
from watcher import PlaybackArbiter


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def start(self) -> bool:
        return True

    def send(
        self,
        text: str,
        volume: int | None = None,
        *,
        event_id: str | None = None,
        pauseable: bool = False,
    ) -> str:
        self.sent.append(text)
        return "completed"

    def request_stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class PausingWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def send(
        self,
        text: str,
        volume: int | None = None,
        *,
        event_id: str | None = None,
        pauseable: bool = False,
    ) -> str:
        self.sent.append(text)
        self.started.set()
        if not self.release.wait(2):
            return "failed"
        return "completed"


class PreemptibleUpdateWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.update_started = threading.Event()
        self.stop_requested = threading.Event()

    def send(
        self,
        text: str,
        volume: int | None = None,
        *,
        event_id: str | None = None,
        pauseable: bool = False,
    ) -> str:
        self.sent.append(text)
        if not pauseable:
            self.update_started.set()
            self.stop_requested.wait(2)
            return "interrupted"
        return "completed"

    def request_stop(self) -> None:
        self.stop_requested.set()


def queued(event_id: str, session_id: str) -> dict[str, object]:
    return {
        "schema": "codex-voice/message/v0.1",
        "event_id": event_id,
        "project_root": "C:/project",
        "session_id": session_id,
        "thread_id": session_id,
        "turn_id": "turn-1",
        "session_label": "Session A" if session_id == "a" else "Session B",
        "kind": "final",
        "text": event_id,
        "sequence": 1,
        "volume": 100,
        "announced_key": f"{session_id}:turn-1",
    }


class ArbiterTests(unittest.TestCase):
    def wait_for_status(self, inbox: Inbox, event_id: str, status: str) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with inbox.connection() as connection:
                row = connection.execute(
                    "SELECT status FROM messages WHERE event_id = ?", (event_id,)
                ).fetchone()
            if row is not None and row["status"] == status:
                return
            time.sleep(0.02)
        self.fail(f"message {event_id} did not reach {status}: {inbox.status()}")

    def test_target_focus_blocks_other_session_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            inbox = Inbox(Path(directory) / "inbox.sqlite3")
            (Path(directory) / "input.json").write_text(
                '{"session_labels":"first-message","session_label_template":"{session_name} says"}\n',
                encoding="utf-8",
            )
            inbox.enqueue(queued("a-1", "a"))
            inbox.enqueue(queued("b-1", "b"))
            inbox.set_state("focus", {"state": "target-response", "session_id": "a"})
            arbiter = PlaybackArbiter(Path(directory), Path(directory), inbox)
            fake = FakeWorker()
            arbiter.worker = fake
            arbiter.start()
            try:
                self.wait_for_status(inbox, "a-1", "played")
                self.assertEqual(fake.sent, ["Session A says: a-1"])
                self.assertEqual(inbox.status()["messages"].get("queued"), 1)

                inbox.set_state("focus", {"state": "idle"})
                arbiter.wake_event.set()
                self.wait_for_status(inbox, "b-1", "played")
                self.assertEqual(fake.sent, ["Session A says: a-1", "Session B says: b-1"])
            finally:
                arbiter.close()

    def test_voice_pause_keeps_one_inflight_request_without_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            root = Path(directory)
            inbox = Inbox(root / "inbox.sqlite3")
            message = queued("interrupt-1", "a")
            message["text"] = "One uninterrupted model request stays buffered."
            inbox.enqueue(message)
            arbiter = PlaybackArbiter(root, root, inbox)
            worker = PausingWorker()
            arbiter.worker = worker
            arbiter.start()
            try:
                self.assertTrue(worker.started.wait(2))
                inbox.set_state("focus", {"state": "listening", "session_id": "a"})
                current = arbiter.interrupt_current()
                self.assertEqual(current["event_id"], "interrupt-1")
                time.sleep(0.1)
                with inbox.connection() as connection:
                    row = connection.execute(
                        "SELECT status, attempts, replay_count, resume_text, resume_offset "
                        "FROM messages WHERE event_id = ?",
                        ("interrupt-1",),
                    ).fetchone()
                self.assertEqual(row["status"], "playing")
                self.assertEqual(row["attempts"], 1)
                self.assertEqual(row["replay_count"], 0)
                self.assertIsNone(row["resume_text"])
                self.assertIsNone(row["resume_offset"])
                self.assertEqual(worker.sent, [message["text"]])

                inbox.set_state("focus", {"state": "resume-playback", "session_id": "a"})
                worker.release.set()
                self.wait_for_status(inbox, "interrupt-1", "played")
                self.assertEqual(worker.sent, [message["text"]])
            finally:
                worker.release.set()
                arbiter.close()

    def test_session_label_is_stateful_across_turns_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = Inbox(root / "inbox.sqlite3")
            (root / "input.json").write_text(
                '{"session_labels":"first-message","session_label_template":"{session_name} says"}\n',
                encoding="utf-8",
            )
            arbiter = PlaybackArbiter(root, root, inbox)
            self.assertEqual(arbiter._speech_text(queued("a-1", "a")), "Session A says: a-1")
            same_session = queued("a-2", "a")
            same_session["turn_id"] = "turn-2"
            self.assertEqual(arbiter._speech_text(same_session), "a-2")
            other_session = queued("b-1", "b")
            self.assertEqual(arbiter._speech_text(other_session), "Session B says: b-1")

            reopened = PlaybackArbiter(root, root, Inbox(root / "inbox.sqlite3"))
            next_a = queued("a-3", "a")
            next_a["turn_id"] = "turn-3"
            self.assertEqual(reopened._speech_text(next_a), "Session A says: a-3")

    def test_updates_are_latest_only_and_not_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            root = Path(directory)
            inbox = Inbox(root / "inbox.sqlite3")
            arbiter = PlaybackArbiter(root, root, inbox)
            arbiter._speech_text(queued("seed", "a"))
            first = {
                "event_id": "update-1",
                "project_root": "C:/project",
                "session_id": "a",
                "session_label": "Session A",
                "kind": "commentary",
                "text": "old progress",
                "volume": 20,
            }
            second = {**first, "event_id": "update-2", "text": "latest progress"}
            self.assertTrue(arbiter.publish_update(first))
            self.assertTrue(arbiter.publish_update(second))
            worker = FakeWorker()
            arbiter.worker = worker
            arbiter.start()
            try:
                deadline = time.monotonic() + 2
                while not worker.sent and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(worker.sent, ["latest progress"])
                with inbox.connection() as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT 1 FROM messages WHERE event_id = ?", ("update-2",)
                        ).fetchone()
                    )
            finally:
                arbiter.close()

    def test_real_message_preempts_ephemeral_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            root = Path(directory)
            inbox = Inbox(root / "inbox.sqlite3")
            arbiter = PlaybackArbiter(root, root, inbox)
            arbiter._speech_text(queued("seed", "a"))
            worker = PreemptibleUpdateWorker()
            arbiter.worker = worker
            arbiter.start()
            try:
                self.assertTrue(
                    arbiter.publish_update(
                        {
                            "event_id": "update-1",
                            "project_root": "C:/project",
                            "session_id": "a",
                            "session_label": "Session A",
                            "kind": "commentary",
                            "text": "ephemeral progress",
                            "volume": 20,
                        }
                    )
                )
                self.assertTrue(worker.update_started.wait(2))
                self.assertTrue(arbiter.enqueue(queued("b-1", "b")))
                self.wait_for_status(inbox, "b-1", "played")
                self.assertEqual(worker.sent, ["ephemeral progress", "b-1"])
                self.assertEqual(inbox.status()["messages"], {"played": 1})
            finally:
                worker.stop_requested.set()
                arbiter.close()


if __name__ == "__main__":
    unittest.main()
