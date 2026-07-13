from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inbox import Inbox
from presence_service import PresenceService
from profiles import write_document


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def send(self, state: str, **kwargs: object) -> bool:
        self.events.append((state, kwargs))
        return True

    def close(self) -> None:
        self.closed = True


class FakePlayback:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.messages: list[dict[str, object]] = []
        self.completed: list[dict[str, object]] = []

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def enqueue(self, message: dict[str, object]) -> bool:
        self.messages.append(message)
        return True

    def publish_update(self, message: dict[str, object]) -> bool:
        self.messages.append({**message, "ephemeral": True})
        return True

    def drain_completed(self) -> list[dict[str, object]]:
        completed, self.completed = self.completed, []
        return completed

    def is_idle(self) -> bool:
        return not self.messages


def speech(project_root: Path) -> dict[str, object]:
    return {
        "schema": "codex-voice/message/v0.1",
        "event_id": "event-1",
        "project_root": str(project_root.resolve()),
        "session_id": "session-a",
        "thread_id": "thread-a",
        "turn_id": "turn-a",
        "kind": "final",
        "text": "hello",
        "sequence": 1,
        "volume": 100,
    }


class PresenceServiceTests(unittest.TestCase):
    def make_service(self, directory: str) -> tuple[PresenceService, Inbox, FakePlayback, FakeEmitter]:
        root = Path(directory)
        inbox = Inbox(root / "inbox.sqlite3")
        playback = FakePlayback()
        emitter = FakeEmitter()
        with patch("presence_service.ActivityEmitter", return_value=emitter):
            service = PresenceService(root, root / ".codex-voice", inbox, playback)
        return service, inbox, playback, emitter

    def test_activity_is_sanitized_and_published_through_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, inbox, playback, emitter = self.make_service(directory)
            service.start()
            try:
                self.assertTrue(service.publish_activity("thinking", source="codex-rollout", session_id="s-1"))
                activity = inbox.get_state("presence_activity", {})
                self.assertEqual(activity["schema"], "codex-voice/presence-service/v0.1")
                self.assertEqual(activity["type"], "activity")
                self.assertEqual(activity["state"], "thinking")
                self.assertEqual(activity["session_id"], "s-1")
                self.assertEqual(emitter.events[0][0], "thinking")
                self.assertTrue(playback.started)
            finally:
                service.close()
            self.assertTrue(playback.closed)
            self.assertTrue(emitter.closed)
            self.assertEqual(inbox.get_state("presence_service", {})["state"], "stopped")

    def test_speech_is_delegated_to_one_playback_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, inbox, playback, _ = self.make_service(directory)
            service.start()
            try:
                self.assertTrue(service.enqueue_speech(speech(Path(directory))))
                self.assertEqual([item["event_id"] for item in playback.messages], ["event-1"])
                self.assertEqual(playback.messages[0]["profile_id"], "default")
                self.assertEqual(playback.messages[0]["route_key"], "session:session-a|profile:default")
                last = inbox.get_state("presence_last_speech", {})
                self.assertEqual(last["event_id"], "event-1")
                self.assertEqual(service.status()["state"]["state"], "running")
                with self.assertRaises(ValueError):
                    service.enqueue_speech({**speech(Path(directory)), "project_root": "C:/other"})
            finally:
                service.close()

    def test_session_profile_is_attached_at_the_presence_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_document(
                root / ".codex-voice",
                {
                    "schema": "codex-ai-presence/profiles/v0.1",
                    "project_profile_id": "default",
                    "profiles": {
                        "default": {},
                        "luna": {"avatar_id": "higan-live2d", "voice": "bf_isabella", "speed": 1.2},
                    },
                    "sessions": {"session-a": {"profile_id": "luna"}},
                },
            )
            service, _, playback, emitter = self.make_service(directory)
            service.start()
            try:
                self.assertTrue(service.enqueue_speech(speech(root)))
                routed = playback.messages[-1]
                self.assertEqual(routed["profile_id"], "luna")
                self.assertEqual(routed["avatar_id"], "higan-live2d")
                self.assertEqual(routed["tts_voice"], "bf_isabella")
                service.publish_activity("thinking", session_id="session-a")
                self.assertEqual(emitter.events[-1][1]["profile_id"], "luna")
                self.assertEqual(emitter.events[-1][1]["avatar_id"], "higan-live2d")
            finally:
                service.close()

    def test_update_is_delegated_without_becoming_a_durable_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, inbox, playback, _ = self.make_service(directory)
            service.start()
            try:
                update = {
                    "schema": "codex-voice/message/v0.1",
                    "event_id": "update-1",
                    "project_root": str(Path(directory).resolve()),
                    "session_id": "session-a",
                    "kind": "commentary",
                    "text": "working",
                }
                self.assertTrue(service.publish_update(update))
                self.assertEqual(playback.messages[-1]["ephemeral"], True)
                self.assertEqual(inbox.status()["messages"], {})
                self.assertEqual(
                    inbox.get_state("presence_last_update", {})["event_id"],
                    "update-1",
                )
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
