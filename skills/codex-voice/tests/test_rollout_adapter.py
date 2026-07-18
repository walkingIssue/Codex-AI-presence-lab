from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rollout_adapter import ActivityTracker, RolloutAdapter, pid_exists


class FakePlayback:
    def __init__(self) -> None:
        self.activities: list[tuple[str, str | None, str]] = []
        self.commentary: list[dict] = []
        self.finals: list[dict] = []

    def publish_activity(self, state: str, *, session_id: str | None, event_id: str) -> bool:
        self.activities.append((state, session_id, event_id))
        return True

    def publish_update(self, message: dict) -> bool:
        self.commentary.append(message)
        return True

    def enqueue(self, message: dict) -> bool:
        self.finals.append(message)
        return True

    def start(self) -> None:
        return

    def close(self) -> None:
        return


def test_pid_exists_recognizes_the_current_process() -> None:
    assert pid_exists(os.getpid()) is True
    assert pid_exists(-1) is False


def test_rollout_adapter_persists_cursor_and_emits_binding_scoped_events(
    tmp_path, monkeypatch
) -> None:
    codex_home = tmp_path / "codex"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    session_id = "019f69a4-6f05-7990-8160-90253f101ed6"
    rollout = codex_home / "sessions" / f"rollout-test-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-07-16T10:00:00Z",
            "type": "session_meta",
            "payload": {"cwd": str(project), "id": session_id},
        },
        {
            "timestamp": "2026-07-16T10:00:01Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "shell_command"},
        },
        {
            "timestamp": "2026-07-16T10:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "commentary",
                "message": "Working on it.",
            },
        },
        {
            "timestamp": "2026-07-16T10:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "final_answer",
                "message": "Done.",
                "turn_id": "turn-1",
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    playback = FakePlayback()
    adapter = RolloutAdapter(
        project,
        project / ".codex-voice" / "v0.2",
        start_time=0,
        playback=playback,  # type: ignore[arg-type]
    )

    assert adapter.discover() == [rollout]
    adapter.scan(rollout)
    assert [item[0] for item in playback.activities] == ["cli", "thinking", "idle"]
    assert playback.commentary[0]["session_id"] == session_id
    assert playback.finals[0]["session_id"] == session_id
    assert playback.finals[0]["turn_id"] == "turn-1"

    adapter.scan(rollout)
    assert len(playback.commentary) == 1
    assert len(playback.finals) == 1
    cursor = json.loads(
        (project / ".codex-voice" / "v0.2" / "rollout-cursors.json").read_text(
            encoding="utf-8"
        )
    )
    assert next(iter(cursor["offsets"].values())) == rollout.stat().st_size


def test_each_activity_timeout_emits_a_fresh_idle_event_id(tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr("rollout_adapter.time.monotonic", lambda: clock[0])
    playback = FakePlayback()
    tracker = ActivityTracker(playback)  # type: ignore[arg-type]
    session_id = "019f69a4-6f05-7990-8160-90253f101ed6"
    rollout = tmp_path / "rollout.jsonl"

    tracker.update(rollout, "thinking", session_id, "thinking:1")
    clock[0] = 13
    tracker.tick()
    first_idle = playback.activities[-1]

    clock[0] = 14
    tracker.update(rollout, "thinking", session_id, "thinking:2")
    clock[0] = 27
    tracker.tick()
    second_idle = playback.activities[-1]

    assert first_idle[0] == second_idle[0] == "idle"
    assert first_idle[2].startswith("activity-timeout:")
    assert second_idle[2].startswith("activity-timeout:")
    assert first_idle[2] != second_idle[2]
