from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from inbox import Inbox, database_path
from tui_bridge import ArbiterInboxAdapter, BRIDGE_SCHEMA, MockKokoroWorker, TuiServerBridge, VoiceChunkRouter


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

    def test_stock_adapter_enqueues_into_shared_arbiter_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            voice = project / ".codex-voice"
            voice.mkdir(parents=True)
            (voice / "enabled").write_text("on\n", encoding="utf-8")
            (voice / "volume").write_text("37\n", encoding="utf-8")
            adapter = ArbiterInboxAdapter(project, voice)

            self.assertTrue(adapter.start())
            self.assertTrue(adapter.send({"type": "start", "stream_id": "s:t", "session_id": "s", "turn_id": "t"}))
            self.assertTrue(adapter.send({"type": "delta", "stream_id": "s:t", "text": "Hello"}))
            self.assertTrue(adapter.send({"type": "delta", "stream_id": "s:t", "text": " world"}))
            self.assertTrue(adapter.send({"type": "finish", "stream_id": "s:t"}))

            message = Inbox(database_path(voice)).claim_next()
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message["text"], "Hello world")
            self.assertEqual(message["session_id"], "s")
            self.assertEqual(message["turn_id"], "t")
            self.assertEqual(message["volume"], 37)
            adapter.close()

    def test_v02_adapter_enqueues_each_visible_chunk_before_finish(self) -> None:
        instances = []

        class FakeRuntime:
            @staticmethod
            def available() -> bool:
                return True

            def __init__(self, project_root, *, adapter):
                self.project_root = project_root
                self.adapter = adapter
                self.messages = []
                self.cancelled = []
                instances.append(self)

            def start(self):
                return None

            def enqueue(self, message):
                self.messages.append(dict(message))
                return True

            def cancel(self, session_id, event_ids):
                self.cancelled.append((session_id, list(event_ids)))
                return len(event_ids)

            def close(self):
                return None

            def publish_activity(self, *_args, **_kwargs):
                return True

        with tempfile.TemporaryDirectory() as directory, patch(
            "tui_bridge.RuntimePlaybackAdapter", FakeRuntime
        ):
            root = Path(directory)
            voice = root / ".codex-voice"
            voice.mkdir()
            adapter = ArbiterInboxAdapter(root, voice)
            self.assertTrue(adapter.start())
            self.assertTrue(
                adapter.send(
                    {
                        "type": "start",
                        "stream_id": "thread:turn",
                        "session_id": "thread",
                        "turn_id": "turn",
                    }
                )
            )
            self.assertTrue(
                adapter.send(
                    {"type": "delta", "stream_id": "thread:turn", "text": "Hello"}
                )
            )
            self.assertEqual([item["text"] for item in instances[0].messages], ["Hello"])
            self.assertTrue(
                adapter.send(
                    {"type": "delta", "stream_id": "thread:turn", "text": " world"}
                )
            )
            self.assertEqual(
                [item["text"] for item in instances[0].messages], ["Hello", " world"]
            )
            self.assertTrue(adapter.send({"type": "finish", "stream_id": "thread:turn"}))
            self.assertEqual(len(instances[0].messages), 2)
            adapter.close()

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
