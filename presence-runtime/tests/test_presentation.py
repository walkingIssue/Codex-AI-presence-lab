from __future__ import annotations

import threading
import time
import uuid
from dataclasses import replace

from presence_runtime.models import (
    EffectiveSnapshot,
    RendererSettings,
    SemanticSnapshot,
    TTSSettings,
)
from presence_runtime.presentation import (
    DeterministicSemanticPlanner,
    PresentationCue,
    PresentationScheduler,
)


def snapshot(
    binding_id: str,
    *,
    persistent: tuple[str, ...] = ("pose.default",),
    effective: tuple[str, ...] | None = None,
    activity: str | None = None,
) -> EffectiveSnapshot:
    return EffectiveSnapshot(
        binding_id=binding_id,
        revision=3,
        profile_ref=None,
        avatar_ref="higan@1",
        model_fingerprint="sha256:" + "1" * 64,
        preset_ref=None,
        tts=TTSSettings("af_heart", 1, "stream", 100, 0.5),
        semantic=SemanticSnapshot(
            persistent_actions=persistent,
            effective_actions=effective or persistent,
            activity=activity,
        ),
        renderer=RendererSettings(True, True, "live2d"),
        capabilities=("semantic-slots",),
        provenance={},
    )


class BlockingRenderer:
    def __init__(self) -> None:
        self.calls: list[PresentationCue] = []
        self.restored: list[EffectiveSnapshot] = []
        self._gates: dict[tuple[str, int], threading.Event] = {}
        self._statuses: dict[tuple[str, int], str] = {}
        self.changed = threading.Condition()

    def apply_presentation(self, cue: PresentationCue) -> str:
        gate = threading.Event()
        with self.changed:
            self.calls.append(cue)
            self._gates[(cue.binding_id, cue.sequence)] = gate
            self.changed.notify_all()
        gate.wait(2)
        return self._statuses.get((cue.binding_id, cue.sequence), "completed")

    def release(
        self,
        sequence: int,
        status: str = "completed",
        *,
        binding_id: str | None = None,
    ) -> None:
        matches = [
            key for key in self._gates
            if key[1] == sequence and (binding_id is None or key[0] == binding_id)
        ]
        assert len(matches) == 1
        key = matches[0]
        self._statuses[key] = status
        self._gates[key].set()

    def wait_calls(self, count: int) -> None:
        deadline = time.monotonic() + 2
        with self.changed:
            while len(self.calls) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0
                self.changed.wait(remaining)

    def cancel_presentation(self, binding_id: str) -> bool:
        for cue in reversed(self.calls):
            if (cue.binding_id, cue.sequence) in self._gates:
                self.release(cue.sequence, "cancelled", binding_id=binding_id)
                return True
        return False

    def restore_snapshot(self, value: EffectiveSnapshot) -> bool:
        self.restored.append(value)
        return True


def test_planner_has_balanced_and_error_timing() -> None:
    binding_id = str(uuid.uuid4())
    base = snapshot(binding_id)
    thinking = replace(
        base,
        semantic=SemanticSnapshot(
            base.semantic.persistent_actions,
            ("pose.thinking",),
            "thinking",
        ),
    )
    error = replace(
        base,
        semantic=SemanticSnapshot(
            base.semantic.persistent_actions,
            ("expression.error",),
            "error",
        ),
    )
    planner = DeterministicSemanticPlanner()

    normal = planner.plan(
        base=base,
        target=thinking,
        activity="thinking",
        event_id="thinking:1",
        sequence=1,
    )
    failure = planner.plan(
        base=base,
        target=error,
        activity="error",
        event_id="error:1",
        sequence=2,
    )

    assert normal.enter_ms == 180
    assert normal.minimum_visible_ms == 900
    assert normal.exit_ms == 180
    assert normal.duration_ms == 1080
    assert failure.minimum_visible_ms == 1400
    assert failure.duration_ms == 1580


def test_latest_pending_coalesces_bursts_and_returns_to_base() -> None:
    binding_id = str(uuid.uuid4())
    base = snapshot(binding_id)
    renderer = BlockingRenderer()
    scheduler = PresentationScheduler(renderer)

    first = snapshot(binding_id, effective=("pose.thinking",), activity="thinking")
    tool = snapshot(binding_id, effective=("pose.tool",), activity="tool")
    cli = snapshot(binding_id, effective=("pose.cli",), activity="cli")

    assert scheduler.submit(base=base, target=first, activity="thinking", event_id="1") == (1, "scheduled")
    renderer.wait_calls(1)
    assert scheduler.submit(base=base, target=tool, activity="tool", event_id="2") == (2, "queued")
    assert scheduler.submit(base=base, target=cli, activity="cli", event_id="3") == (3, "queued")
    assert scheduler.submit(base=base, target=cli, activity="cli", event_id="4") == (3, "coalesced")

    renderer.release(1)
    renderer.wait_calls(2)
    assert [cue.activity for cue in renderer.calls] == ["thinking", "cli"]
    renderer.release(3)
    deadline = time.monotonic() + 1
    while scheduler.status(binding_id)["phase"] != "idle" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scheduler.status(binding_id)["last_acknowledged_sequence"] == 3
    scheduler.close()


def test_diagnostics_report_timed_phase_and_cue_remaining_time() -> None:
    binding_id = str(uuid.uuid4())
    base = snapshot(binding_id)
    renderer = BlockingRenderer()
    now = [100.0]
    scheduler = PresentationScheduler(renderer, clock=lambda: now[0])
    thinking = snapshot(binding_id, effective=("pose.thinking",), activity="thinking")

    scheduler.submit(base=base, target=thinking, activity="thinking", event_id="1")
    renderer.wait_calls(1)
    assert scheduler.status(binding_id)["phase"] == "enter"
    assert scheduler.status(binding_id)["remaining_ms"] == 1080

    now[0] += 0.18
    assert scheduler.status(binding_id)["phase"] == "minimum-visible"
    assert scheduler.status(binding_id)["remaining_ms"] == 900

    now[0] += 0.72
    assert scheduler.status(binding_id)["phase"] == "exit"
    assert scheduler.status(binding_id)["remaining_ms"] == 180

    now[0] += 0.18
    assert scheduler.status(binding_id)["phase"] == "awaiting-ack"
    assert scheduler.status(binding_id)["remaining_ms"] == 0
    assert scheduler.status(binding_id)["watchdog_remaining_ms"] == 2000

    renderer.release(1)
    scheduler.close()


def test_idle_clears_pending_without_preempting_the_active_cue() -> None:
    binding_id = str(uuid.uuid4())
    base = snapshot(binding_id)
    renderer = BlockingRenderer()
    scheduler = PresentationScheduler(renderer)
    thinking = snapshot(binding_id, effective=("pose.thinking",), activity="thinking")
    tool = snapshot(binding_id, effective=("pose.tool",), activity="tool")

    scheduler.submit(base=base, target=thinking, activity="thinking", event_id="1")
    renderer.wait_calls(1)
    scheduler.submit(base=base, target=tool, activity="tool", event_id="2")
    assert scheduler.submit(base=base, target=base, activity="idle", event_id="3") == (None, "cleared")
    renderer.release(1)
    time.sleep(0.05)

    assert [cue.activity for cue in renderer.calls] == ["thinking"]
    assert scheduler.status(binding_id)["phase"] == "idle"
    scheduler.close()


def test_bindings_present_independently_and_failure_restores_persistent_base() -> None:
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first = snapshot(first_id)
    second = snapshot(second_id)
    renderer = BlockingRenderer()
    scheduler = PresentationScheduler(renderer)

    scheduler.submit(
        base=first,
        target=snapshot(first_id, effective=("pose.first",), activity="thinking"),
        activity="thinking",
        event_id="first",
    )
    scheduler.submit(
        base=second,
        target=snapshot(second_id, effective=("pose.second",), activity="error"),
        activity="error",
        event_id="second",
    )
    renderer.wait_calls(2)
    assert {cue.binding_id for cue in renderer.calls} == {first_id, second_id}

    first_cue = next(cue for cue in renderer.calls if cue.binding_id == first_id)
    second_cue = next(cue for cue in renderer.calls if cue.binding_id == second_id)
    renderer.release(first_cue.sequence, "failed", binding_id=first_id)
    renderer.release(second_cue.sequence, binding_id=second_id)
    deadline = time.monotonic() + 1
    while not renderer.restored and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [item.binding_id for item in renderer.restored] == [first_id]
    assert scheduler.status(first_id)["last_error"]
    assert scheduler.status(second_id)["last_acknowledged_sequence"] == second_cue.sequence
    scheduler.close()
