from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tui_bridge import BRIDGE_SCHEMA, MockKokoroWorker, TuiServerBridge, VoiceChunkRouter


class TuiBridgeTests(unittest.TestCase):
    def make_router(self) -> tuple[MockKokoroWorker, VoiceChunkRouter]:
        worker = MockKokoroWorker()
        return worker, VoiceChunkRouter(worker)

    def test_visible_app_server_deltas_route_to_mock_worker(self) -> None:
        worker, router = self.make_router()
        self.assertTrue(
            router.handle(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                        "delta": "Hello",
                        "sequence": 1,
                    },
                }
            )
        )
        self.assertTrue(
            router.handle(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                        "delta": " world",
                        "sequence": 2,
                    },
                }
            )
        )
        self.assertTrue(
            router.handle(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "item-1"},
                    },
                }
            )
        )

        self.assertEqual([event["type"] for event in worker.events], ["start", "delta", "delta", "finish"])
        self.assertEqual([event.get("text") for event in worker.events if event["type"] == "delta"], ["Hello", "world"])
        self.assertTrue(all(event["schema"] == BRIDGE_SCHEMA for event in worker.events))

    def test_reasoning_and_tool_payloads_never_become_voice_chunks(self) -> None:
        worker, router = self.make_router()
        self.assertFalse(
            router.handle(
                {
                    "method": "item/reasoning/delta",
                    "params": {"threadId": "thread-1", "delta": "private reasoning"},
                }
            )
        )
        self.assertFalse(
            router.handle(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "item": {"type": "commandExecution", "command": "secret"},
                    },
                }
            )
        )
        self.assertEqual(worker.events, [])

    def test_duplicate_sequence_is_dropped_and_new_stream_is_cancelled(self) -> None:
        worker, router = self.make_router()
        first = {"type": "voice/chunk", "stream_id": "stream-a", "text": "one", "sequence": 1}
        self.assertTrue(router.handle(first))
        self.assertFalse(router.handle(first))
        self.assertTrue(
            router.handle(
                {"type": "voice/chunk", "stream_id": "stream-b", "text": "two", "sequence": 1}
            )
        )

        self.assertEqual(
            [(event["type"], event["stream_id"]) for event in worker.events],
            [
                ("start", "stream-a"),
                ("delta", "stream-a"),
                ("cancel", "stream-a"),
                ("start", "stream-b"),
                ("delta", "stream-b"),
            ],
        )

    def test_explicit_envelope_can_be_finished_without_app_server_shapes(self) -> None:
        worker, router = self.make_router()
        self.assertTrue(router.handle({"type": "voice/start", "session_id": "s", "turn_id": "t"}))
        self.assertTrue(
            router.handle(
                {
                    "type": "voice/chunk",
                    "session_id": "s",
                    "turn_id": "t",
                    "text": "visible output",
                }
            )
        )
        self.assertTrue(router.handle({"type": "voice/finish", "stream_id": "s:t"}))
        self.assertEqual([event["type"] for event in worker.events], ["start", "delta", "finish"])

    def test_turn_completion_matches_stream_without_item_id(self) -> None:
        worker, router = self.make_router()
        router.handle(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "delta": "done soon",
                },
            }
        )
        self.assertTrue(
            router.handle(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
                }
            )
        )
        self.assertEqual(worker.events[-1]["type"], "finish")

    def test_item_completion_can_add_identity_missing_from_delta(self) -> None:
        worker, router = self.make_router()
        router.handle(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "delta": "done soon",
                },
            }
        )
        self.assertTrue(
            router.handle(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "item-1"},
                    },
                }
            )
        )
        self.assertEqual(worker.events[-1]["type"], "finish")

    def test_proxy_forwards_server_lines_while_tapping_voice(self) -> None:
        line = (
            '{"method":"item/agentMessage/delta","params":'
            '{"threadId":"thread","turnId":"turn","itemId":"item",'
            '"delta":"visible"}}\n'
        )
        worker = MockKokoroWorker()
        output = io.StringIO()
        command = [
            sys.executable,
            "-u",
            "-c",
            "import sys; [print(line, end=\"\", flush=True) for line in sys.stdin]",
        ]
        with tempfile.TemporaryDirectory() as directory:
            bridge = TuiServerBridge(
                Path(directory),
                Path(directory) / ".codex-voice",
                command,
                worker,
                stdin=io.StringIO(line),
                stdout=output,
            )
            self.assertEqual(bridge.run(), 0)

        self.assertEqual(output.getvalue(), line)
        self.assertEqual([event["type"] for event in worker.events], ["start", "delta", "cancel"])
        self.assertTrue(worker.closed)


if __name__ == "__main__":
    unittest.main()
