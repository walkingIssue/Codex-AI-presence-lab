from __future__ import annotations

import time
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from global_arbiter import GlobalArbiterCore


class FakeWorker:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.started = False

    def start(self) -> bool:
        self.started = True
        return True

    def send(self, item: dict[str, object]) -> str:
        self.sent.append(item)
        return "completed"

    def request_stop(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def send(self, event: dict[str, object]) -> None:
        self.events.append(event)


def message(project: str, session: str, text: str, event_id: str) -> dict[str, object]:
    return {
        "event_id": event_id,
        "project_root": project,
        "session_id": session,
        "profile_id": "default",
        "route_key": f"session:{session}|profile:default",
        "session_label": session,
        "session_labels": "session-change",
        "session_label_template": "{session_name} says",
        "orb_port": 21667 if project.endswith("a") else 28956,
        "text": text,
        "kind": "final",
        "volume": 100,
    }


class GlobalArbiterTests(unittest.TestCase):
    def wait_for(self, predicate) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("global arbiter did not drain in time")

    def test_one_worker_serializes_cross_project_sessions_and_announces_swaps(self) -> None:
        worker = FakeWorker()
        arbiter = GlobalArbiterCore(worker)
        client_a = FakeClient()
        client_b = FakeClient()
        arbiter.start()
        try:
            self.assertTrue(arbiter.enqueue(message("project-a", "alpha", "one", "a-1"), client_a))
            self.wait_for(lambda: any(event.get("event") == "complete" for event in client_a.events))
            self.assertTrue(arbiter.enqueue(message("project-b", "beta", "two", "b-1"), client_b))
            self.wait_for(lambda: any(event.get("event") == "complete" for event in client_b.events))
            self.assertTrue(worker.started)
            self.assertEqual(
                [(item["kind"], item["text"]) for item in worker.sent],
                [
                    ("session-announcement", "alpha says"),
                    ("speech", "one"),
                    ("session-announcement", "beta says"),
                    ("speech", "two"),
                ],
            )
            self.assertEqual(arbiter.attention_owner, "session:beta|profile:default")
        finally:
            arbiter.close()
