from __future__ import annotations

import io
import json
import signal
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from clipboard import ClipboardError, copy_text
from inbox import Inbox
from stt import STTUnavailable
import voice_input
from watcher import VoiceInputController


class FakeArbiter:
    def __init__(self) -> None:
        self.wake_event = threading.Event()

    def interrupt_current(self):
        return None


class VoiceInputDeliveryTests(unittest.TestCase):
    def make_controller(self, directory: str) -> tuple[VoiceInputController, Inbox]:
        root = Path(directory)
        inbox = Inbox(root / "inbox.sqlite3")
        controller = VoiceInputController(root, root, inbox, FakeArbiter())
        controller.target_session_id = "session-a"
        return controller, inbox

    def test_clipboard_is_safe_default_and_releases_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.copy_text") as copied, patch(
            "watcher.emit_orb_event"
        ):
            controller, inbox = self.make_controller(directory)
            result = controller._deliver_transcript("session-a", "paste this into Codex")

            self.assertEqual(result, {"mode": "clipboard", "char_count": 21})
            copied.assert_called_once_with("paste this into Codex")
            self.assertEqual(inbox.get_state("focus")["state"], "drain-queued")
            self.assertEqual(inbox.get_state("focus")["delivery"], "clipboard")
            self.assertEqual(inbox.get_state("input")["state"], "clipboard-ready")
            self.assertIsNone(controller.target_session_id)

    def test_app_server_requires_explicit_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"), patch(
            "watcher.AppServerClient"
        ) as client_type:
            root = Path(directory)
            (root / "input.json").write_text(
                json.dumps({"delivery_mode": "app-server"}), encoding="utf-8"
            )
            controller, _inbox = self.make_controller(directory)
            client_type.return_value.submit.return_value = {
                "method": "turn/start",
                "turn_id": "turn-2",
            }

            result = controller._deliver_transcript("session-a", "send directly")

            self.assertEqual(result["method"], "turn/start")
            client_type.return_value.submit.assert_called_once_with(
                "session-a", "send directly", wait_for_completion=False
            )

    def test_empty_clipboard_text_is_rejected(self) -> None:
        with self.assertRaises(ClipboardError):
            copy_text("   ")

    def test_capture_finish_starts_stt_immediately_while_playback_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            controller, inbox = self.make_controller(directory)
            controller.resume_event_id = "interrupted-event"
            controller.active_capture_sequence = 1
            controller.latest_started_sequence = 1
            recording = Path(directory) / "capture.webm"
            recording.write_bytes(b"recording")

            with patch.object(controller, "_transcribe_and_submit") as transcribe:
                controller._finish_capture(
                    {"recording": str(recording), "capture_sequence": 1}
                )
                deadline = time.monotonic() + 2
                while not transcribe.called and time.monotonic() < deadline:
                    time.sleep(0.01)

            transcribe.assert_called_once_with(
                [recording], "session-a", capture_sequence=1
            )
            self.assertEqual(inbox.get_state("focus")["state"], "resume-playback")
            self.assertEqual(inbox.get_state("focus")["resume_event_id"], "interrupted-event")
            self.assertEqual(inbox.get_state("input")["state"], "transcribing")
            self.assertEqual(inbox.get_state("input")["capture_sequence"], 1)
            self.assertTrue(controller.arbiter.wake_event.is_set())

    def test_capture_start_gates_queue_before_requesting_player_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.json").write_text(
                json.dumps({"input_enabled": True}), encoding="utf-8"
            )
            inbox = Inbox(root / "inbox.sqlite3")
            inbox.set_state("last_session_id", "session-a")

            def assert_queue_is_gated(_voice_root: Path) -> None:
                self.assertEqual(
                    inbox.get_state("focus"),
                    {"state": "listening", "session_id": "session-a"},
                )

            with patch(
                "voice_input.request_immediate_playback_stop",
                side_effect=assert_queue_is_gated,
            ), redirect_stdout(io.StringIO()):
                result = voice_input.control(root, "capture-start", {})

            self.assertEqual(result, 0)
            controls = inbox.consume_controls()
            self.assertEqual(len(controls), 1)
            self.assertEqual(controls[0]["command"], "capture-start")
            self.assertEqual(controls[0]["payload"]["capture_sequence"], 1)
            self.assertEqual(inbox.get_state("input")["capture_sequence"], 1)

    def test_immediate_stop_terminates_only_the_player_and_sets_pause_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tts-player.pid").write_text("4321", encoding="utf-8")
            (root / "tts-resume.request").write_text("stale", encoding="utf-8")

            with patch("voice_input.os.kill") as killed:
                voice_input.request_immediate_playback_stop(root)

            self.assertEqual(
                killed.call_args_list,
                [call(4321, 0), call(4321, signal.SIGTERM)],
            )
            self.assertTrue((root / "tts-stop.request").is_file())
            self.assertFalse((root / "tts-resume.request").exists())

            voice_input.request_playback_resume(root)
            self.assertTrue((root / "tts-resume.request").is_file())

    def test_stt_json_error_is_preserved_in_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("watcher.emit_orb_event"):
            controller, inbox = self.make_controller(directory)
            recording = Path(directory) / "capture.webm"
            recording.write_bytes(b"invalid recording")
            with patch.object(
                controller.stt_worker,
                "transcribe",
                side_effect=STTUnavailable("decoder rejected recording"),
            ), patch("watcher.log") as logged:
                controller._transcribe_and_submit([recording], "session-a")

            self.assertIn("decoder rejected recording", logged.call_args.args[1])
            self.assertEqual(inbox.get_state("focus"), {"state": "idle"})
            self.assertEqual(inbox.get_state("input")["state"], "error")
            self.assertIsNone(controller.target_session_id)
            self.assertFalse(recording.exists())

    def test_superseded_transcript_never_overwrites_current_clipboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "watcher.copy_text"
        ) as copied, patch("watcher.emit_orb_event"):
            controller, inbox = self.make_controller(directory)
            inbox.set_state("input_capture_sequence", 2)
            controller.latest_started_sequence = 2

            stale = controller._deliver_transcript(
                "session-a", "recording one", capture_sequence=1
            )
            current = controller._deliver_transcript(
                "session-a", "recording two", capture_sequence=2
            )

            self.assertTrue(stale["superseded"])
            self.assertEqual(current["capture_sequence"], 2)
            copied.assert_called_once_with("recording two")
            self.assertEqual(inbox.get_state("input")["capture_sequence"], 2)


if __name__ == "__main__":
    unittest.main()
