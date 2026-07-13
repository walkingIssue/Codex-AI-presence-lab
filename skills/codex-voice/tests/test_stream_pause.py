from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import speak


class FakeTimeline:
    def __init__(self, _socket, _sample_rate: int) -> None:
        self.frames = 0

    def add(self, frame) -> None:
        self.frames += len(frame)

    def finish(self) -> None:
        return None


class FakeStdin:
    def __init__(self, owner: "FakePlayer", first_write: threading.Event) -> None:
        self.owner = owner
        self.first_write = first_write
        self.closed = False

    def write(self, value: bytes) -> int:
        if self.owner.terminated:
            raise BrokenPipeError()
        self.owner.writes.append(len(value))
        self.first_write.set()
        return len(value)

    def flush(self) -> None:
        if self.owner.terminated:
            raise BrokenPipeError()

    def close(self) -> None:
        self.closed = True


class FakePlayer:
    next_pid = 1000

    def __init__(self, first_write: threading.Event) -> None:
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.terminated = False
        self.terminated_event = threading.Event()
        self.writes: list[int] = []
        self.stdin = FakeStdin(self, first_write)

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True
        self.terminated_event.set()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: float | None = None) -> int:
        self.terminated = True
        self.terminated_event.set()
        return 0


class FakeTTS:
    def __init__(self) -> None:
        self.inference_finished = threading.Event()

    async def create_stream(self, *_args, **_kwargs):
        yield np.ones(12_000, dtype=np.float32), 24_000  # 500 ms
        self.inference_finished.set()


class StreamPauseTests(unittest.TestCase):
    def test_cumulative_deadline_compensates_per_frame_overhead(self) -> None:
        now = 0.0
        deadline = None
        for _ in range(50):
            now += 0.002  # pipe write and event-loop overhead
            deadline = speak.advance_playback_deadline(deadline, 0.02, now)
            now = max(now, deadline)

        self.assertAlmostEqual(now, 1.002, places=6)

    def test_playback_deadline_rechecks_early_timer_wakeups(self) -> None:
        now = [0.0]
        requested_delays: list[float] = []

        async def early_sleep(delay: float) -> None:
            requested_delays.append(delay)
            now[0] += min(delay, 0.25)

        asyncio.run(
            speak.wait_until_playback_deadline(
                1.0,
                clock=lambda: now[0],
                sleeper=early_sleep,
            )
        )

        self.assertGreaterEqual(now[0], 1.0)
        self.assertGreater(len(requested_delays), 1)

    def test_pause_stops_sink_while_inference_buffers_then_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.onnx"
            voices = root / "voices.bin"
            model.write_bytes(b"model")
            voices.write_bytes(b"voices")
            stop = root / "tts-stop.request"
            resume = root / "tts-resume.request"
            progress = root / "tts-progress.json"
            player_pid = root / "tts-player.pid"
            first_write = threading.Event()
            players: list[FakePlayer] = []
            fake_tts = FakeTTS()
            errors: list[BaseException] = []

            def start_player(*_args, **_kwargs):
                player = FakePlayer(first_write)
                players.append(player)
                return player

            def run() -> None:
                try:
                    speak.stream_audio(
                        "This is a buffered pause regression.",
                        event_id="event-1",
                        pauseable=True,
                        interruptible=False,
                    )
                except BaseException as exc:  # surfaced in the assertion below
                    errors.append(exc)

            patches = (
                patch.object(speak, "VOICES_PATH", voices),
                patch.object(speak, "STOP_REQUEST_PATH", stop),
                patch.object(speak, "RESUME_REQUEST_PATH", resume),
                patch.object(speak, "PROGRESS_PATH", progress),
                patch.object(speak, "PLAYER_PID_PATH", player_pid),
                patch.object(speak, "configured_model_path", return_value=model),
                patch.object(speak, "configured_voice", return_value="voice"),
                patch.object(speak, "configured_speed", return_value=1.0),
                patch.object(speak, "configured_volume", return_value=20),
                patch.object(speak, "language_for_voice", return_value="en-us"),
                patch.object(speak, "get_tts", return_value=fake_tts),
                patch.object(speak, "orb_socket", return_value=None),
                patch.object(speak, "OrbPlaybackTimeline", FakeTimeline),
                patch.object(speak.shutil, "which", return_value="ffplay"),
                patch.object(speak.subprocess, "Popen", side_effect=start_player),
                patch.object(speak, "hook_log"),
            )
            for active_patch in patches:
                active_patch.start()
            try:
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                self.assertTrue(first_write.wait(2), "playback did not start")
                stop.write_text("pause\n", encoding="utf-8")
                self.assertTrue(players[0].terminated_event.wait(2), "sink was not terminated")
                self.assertTrue(
                    fake_tts.inference_finished.wait(2),
                    "inference did not finish while playback was paused",
                )
                writes_while_paused = sum(len(player.writes) for player in players)
                time.sleep(0.08)
                self.assertEqual(
                    sum(len(player.writes) for player in players),
                    writes_while_paused,
                )

                resume.write_text("resume\n", encoding="utf-8")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "buffered playback did not resume")
                self.assertEqual(errors, [])
                self.assertGreaterEqual(len(players), 2)
                self.assertGreater(
                    sum(len(player.writes) for player in players),
                    writes_while_paused,
                )
            finally:
                for active_patch in reversed(patches):
                    active_patch.stop()


if __name__ == "__main__":
    unittest.main()
