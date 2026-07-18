"""Deterministic, binding-scoped semantic presentation scheduling.

Persistent profile resolution deliberately stops at :class:`EffectiveSnapshot`.
This module turns an already validated activity overlay into a finite renderer
cue, while keeping all clocks and queue state ephemeral.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from .models import EffectiveSnapshot


ENTER_MS = 180
MINIMUM_VISIBLE_MS = 900
ERROR_MINIMUM_VISIBLE_MS = 1400
EXIT_MS = 180
EASING = "easeInOutCubic"


@dataclass(frozen=True, slots=True)
class PresentationCue:
    binding_id: str
    configuration_revision: int
    sequence: int
    event_id: str
    activity: str
    base_actions: tuple[str, ...]
    target_actions: tuple[str, ...]
    enter_ms: int = ENTER_MS
    minimum_visible_ms: int = MINIMUM_VISIBLE_MS
    exit_ms: int = EXIT_MS
    easing: str = EASING

    @property
    def duration_ms(self) -> int:
        # The minimum visible lifetime starts when the renderer accepts the
        # cue, so entry easing is included and exit begins no earlier than it.
        return max(self.enter_ms, self.minimum_visible_ms) + self.exit_ms

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": "presence/presentation-cue/v0.1",
            "binding_id": self.binding_id,
            "configuration_revision": self.configuration_revision,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "activity": self.activity,
            "base_actions": list(self.base_actions),
            "target_actions": list(self.target_actions),
            "enter_ms": self.enter_ms,
            "minimum_visible_ms": self.minimum_visible_ms,
            "exit_ms": self.exit_ms,
            "easing": self.easing,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "activity": self.activity,
            "configuration_revision": self.configuration_revision,
            "target_actions": list(self.target_actions),
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class PresentationAcceptance:
    binding_id: str
    effective_revision: int
    activity: str
    effective_actions: tuple[str, ...]
    presentation_sequence: int | None
    disposition: str
    duplicate: bool = False


class SemanticPlanner(Protocol):
    def plan(
        self,
        *,
        base: EffectiveSnapshot,
        target: EffectiveSnapshot,
        activity: str,
        event_id: str,
        sequence: int,
    ) -> PresentationCue | None: ...


class DeterministicSemanticPlanner:
    """Map the current deterministic activity table onto a timed cue."""

    def plan(
        self,
        *,
        base: EffectiveSnapshot,
        target: EffectiveSnapshot,
        activity: str,
        event_id: str,
        sequence: int,
    ) -> PresentationCue | None:
        if base.binding_id != target.binding_id:
            raise ValueError("presentation snapshots belong to different bindings")
        if base.revision != target.revision:
            raise ValueError("presentation target changed configuration revision")
        base_actions = tuple(base.semantic.persistent_actions)
        target_actions = tuple(target.semantic.effective_actions)
        if activity == "idle" or target_actions == base_actions:
            return None
        return PresentationCue(
            binding_id=base.binding_id,
            configuration_revision=base.revision,
            sequence=sequence,
            event_id=event_id,
            activity=activity,
            base_actions=base_actions,
            target_actions=target_actions,
            minimum_visible_ms=(
                ERROR_MINIMUM_VISIBLE_MS
                if activity == "error"
                else MINIMUM_VISIBLE_MS
            ),
        )


class PresentationRenderer(Protocol):
    def apply_presentation(self, cue: PresentationCue) -> str: ...

    def cancel_presentation(self, binding_id: str) -> bool: ...

    def restore_snapshot(self, snapshot: EffectiveSnapshot) -> bool: ...


@dataclass(slots=True)
class _QueuedCue:
    cue: PresentationCue
    snapshot: EffectiveSnapshot


@dataclass(slots=True)
class _BindingState:
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.RLock())
    )
    active: _QueuedCue | None = None
    pending: _QueuedCue | None = None
    phase: str = "idle"
    started_at: float | None = None
    deadline: float | None = None
    last_acknowledged_sequence: int | None = None
    last_error: str | None = None
    stopping: bool = False
    thread: threading.Thread | None = None


class PresentationScheduler:
    """One active and one latest-pending cue for each stable binding."""

    def __init__(
        self,
        renderer: PresentationRenderer,
        *,
        planner: SemanticPlanner | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.renderer = renderer
        self.planner = planner or DeterministicSemanticPlanner()
        self.clock = clock
        self._states: dict[str, _BindingState] = {}
        self._sequences: dict[str, int] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _state(self, binding_id: str) -> _BindingState:
        with self._lock:
            state = self._states.get(binding_id)
            if state is None:
                state = _BindingState()
                self._states[binding_id] = state
            return state

    def _next_sequence(self, binding_id: str) -> int:
        with self._lock:
            sequence = self._sequences.get(binding_id, 0) + 1
            self._sequences[binding_id] = sequence
            return sequence

    def submit(
        self,
        *,
        base: EffectiveSnapshot,
        target: EffectiveSnapshot,
        activity: str,
        event_id: str,
    ) -> tuple[int | None, str]:
        if self._closed:
            return None, "closed"
        state = self._state(base.binding_id)
        if activity == "idle":
            with state.condition:
                state.pending = None
                if state.active is None:
                    state.phase = "idle"
                state.condition.notify_all()
            return None, "cleared"

        sequence = self._next_sequence(base.binding_id)
        cue = self.planner.plan(
            base=base,
            target=target,
            activity=activity,
            event_id=event_id,
            sequence=sequence,
        )
        if cue is None:
            return None, "no-pose"
        queued = _QueuedCue(cue=cue, snapshot=base)
        with state.condition:
            active_target = state.active.cue.target_actions if state.active else None
            pending_target = state.pending.cue.target_actions if state.pending else None
            if cue.target_actions == active_target:
                return state.active.cue.sequence, "coalesced"
            if cue.target_actions == pending_target:
                return state.pending.cue.sequence, "coalesced"
            disposition = "queued" if state.active is not None else "scheduled"
            state.pending = queued
            state.last_error = None
            if state.thread is None or not state.thread.is_alive():
                state.thread = threading.Thread(
                    target=self._run_binding,
                    args=(base.binding_id, state),
                    name=f"presence-presentation-{base.binding_id[:8]}",
                    daemon=True,
                )
                state.thread.start()
            state.condition.notify_all()
        return cue.sequence, disposition

    def _run_binding(self, binding_id: str, state: _BindingState) -> None:
        while True:
            with state.condition:
                while state.pending is None and not state.stopping:
                    state.condition.wait()
                if state.stopping:
                    state.active = None
                    state.pending = None
                    state.phase = "closed"
                    state.condition.notify_all()
                    return
                queued = state.pending
                state.pending = None
                state.active = queued
                state.phase = "presenting"
                state.started_at = self.clock()
                state.deadline = state.started_at + (queued.cue.duration_ms + 2000) / 1000

            status = "failed"
            diagnostic: str | None = None
            try:
                status = self.renderer.apply_presentation(queued.cue)
                if status not in {"completed", "cancelled"}:
                    diagnostic = f"renderer returned presentation status {status!r}"
            except BaseException as exc:
                diagnostic = str(exc)

            restore = status not in {"completed", "cancelled"}
            if restore:
                try:
                    self.renderer.cancel_presentation(binding_id)
                except BaseException as exc:
                    diagnostic = f"{diagnostic}; {exc}" if diagnostic else str(exc)
                try:
                    if not self.renderer.restore_snapshot(queued.snapshot):
                        suffix = "renderer rejected persistent snapshot restore"
                        diagnostic = f"{diagnostic}; {suffix}" if diagnostic else suffix
                except BaseException as exc:
                    diagnostic = f"{diagnostic}; {exc}" if diagnostic else str(exc)

            with state.condition:
                # Cancellation can race a replacement snapshot, but the worker
                # still owns this exact active cue until the renderer request
                # returns. Never let an old completion clear a newer pending cue.
                if state.active is queued:
                    if status == "completed":
                        state.last_acknowledged_sequence = queued.cue.sequence
                    state.last_error = diagnostic
                    state.active = None
                    state.started_at = None
                    state.deadline = None
                    state.phase = "pending" if state.pending is not None else "idle"
                    state.condition.notify_all()

    def clear_pending(self, binding_id: str) -> None:
        state = self._state(binding_id)
        with state.condition:
            state.pending = None
            if state.active is None:
                state.phase = "idle"
            state.condition.notify_all()

    def cancel(
        self,
        binding_id: str,
        *,
        wait: bool = True,
        timeout: float = 3.0,
    ) -> bool:
        state = self._state(binding_id)
        with state.condition:
            state.pending = None
            active = state.active is not None
            if active:
                state.phase = "cancelling"
        if active:
            try:
                self.renderer.cancel_presentation(binding_id)
            except BaseException as exc:
                with state.condition:
                    state.last_error = str(exc)
        if wait and active:
            deadline = self.clock() + timeout
            with state.condition:
                while state.active is not None:
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        state.last_error = "presentation cancellation timed out"
                        return False
                    state.condition.wait(min(remaining, 0.1))
        return True

    def status(self, binding_id: str | None = None) -> Mapping[str, Any]:
        if binding_id is not None:
            return self._status_one(binding_id, self._state(binding_id))
        with self._lock:
            items = tuple(self._states.items())
        return {
            key: self._status_one(key, state)
            for key, state in items
        }

    def _status_one(self, binding_id: str, state: _BindingState) -> dict[str, Any]:
        with state.condition:
            remaining_ms = None
            watchdog_remaining_ms = None
            phase = state.phase
            if state.active is not None and state.started_at is not None:
                cue = state.active.cue
                elapsed_ms = max(0, round((self.clock() - state.started_at) * 1000))
                remaining_ms = max(0, cue.duration_ms - elapsed_ms)
                if state.phase != "cancelling":
                    if elapsed_ms < cue.enter_ms:
                        phase = "enter"
                    elif elapsed_ms < cue.minimum_visible_ms:
                        phase = "minimum-visible"
                    elif elapsed_ms < cue.duration_ms:
                        phase = "exit"
                    else:
                        phase = "awaiting-ack"
            if state.deadline is not None:
                watchdog_remaining_ms = max(
                    0,
                    round((state.deadline - self.clock()) * 1000),
                )
            return {
                "binding_id": binding_id,
                "phase": phase,
                "remaining_ms": remaining_ms,
                "watchdog_remaining_ms": watchdog_remaining_ms,
                "active": state.active.cue.summary() if state.active else None,
                "pending": state.pending.cue.summary() if state.pending else None,
                "last_acknowledged_sequence": state.last_acknowledged_sequence,
                "last_error": state.last_error,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            states = tuple(self._states.items())
        for binding_id, state in states:
            with state.condition:
                state.pending = None
                state.stopping = True
                active = state.active is not None
                state.condition.notify_all()
            if active:
                try:
                    self.renderer.cancel_presentation(binding_id)
                except BaseException:
                    pass
        current = threading.current_thread()
        for _binding_id, state in states:
            thread = state.thread
            if thread is not None and thread is not current:
                thread.join(timeout=3)
