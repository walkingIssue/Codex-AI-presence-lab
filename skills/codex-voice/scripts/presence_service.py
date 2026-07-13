"""Local, renderer-neutral Presence Service control plane."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from activity import ACTIVITY_STATES, ActivityEmitter, state_ttl_seconds
from inbox import Inbox


SERVICE_SCHEMA = "codex-voice/presence-service/v0.1"
SERVICE_STATE_KEY = "presence_service"
ACTIVITY_STATE_KEY = "presence_activity"
LAST_SPEECH_KEY = "presence_last_speech"
LAST_UPDATE_KEY = "presence_last_update"


class PlaybackOwner(Protocol):
    """The small playback surface a host adapter needs from the service."""

    def start(self) -> None: ...

    def close(self) -> None: ...

    def enqueue(self, message: dict[str, object]) -> bool: ...

    def publish_update(self, message: dict[str, object]) -> bool: ...

    def drain_completed(self) -> list[dict[str, object]]: ...

    def is_idle(self) -> bool: ...


@dataclass(frozen=True)
class PresenceEvent:
    """A sanitized event suitable for the local renderer boundary."""

    event_type: str
    project_root: str
    session_id: str | None
    sequence: int
    timestamp: str
    fields: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": SERVICE_SCHEMA,
            "type": self.event_type,
            "project_root": self.project_root,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
        }
        if self.session_id:
            result["session_id"] = self.session_id
        result.update(self.fields)
        return result


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_source(value: object) -> str:
    if not isinstance(value, str):
        return "adapter"
    value = value.strip()
    return value[:64] or "adapter"


class PresenceService:
    """Own normalized presence state while delegating audio to one arbiter."""

    def __init__(
        self,
        project_root: Path,
        voice_root: Path,
        inbox: Inbox,
        playback: PlaybackOwner,
        *,
        adapter_name: str = "codex-rollout",
    ) -> None:
        self.project_root = project_root.resolve()
        self.voice_root = voice_root.resolve()
        self.inbox = inbox
        self.playback = playback
        self.adapter_name = _bounded_source(adapter_name)
        self.emitter = ActivityEmitter()
        self._lock = threading.Lock()
        self._sequence = 0
        self._running = False

    def _event(self, event_type: str, session_id: str | None, fields: dict[str, object]) -> PresenceEvent:
        with self._lock:
            sequence = self._sequence
            self._sequence += 1
        return PresenceEvent(
            event_type=event_type,
            project_root=str(self.project_root),
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            sequence=sequence,
            timestamp=_timestamp(),
            fields=fields,
        )

    def _lifecycle(self, state: str) -> None:
        self.inbox.set_state(
            SERVICE_STATE_KEY,
            {
                "schema": SERVICE_SCHEMA,
                "state": state,
                "adapter": self.adapter_name,
                "project_root": str(self.project_root),
                "updated_at": _timestamp(),
            },
        )

    def start(self) -> None:
        if self._running:
            return
        self.playback.start()
        self._running = True
        self._lifecycle("running")

    def close(self) -> None:
        if not self._running:
            self.emitter.close()
            return
        try:
            self.playback.close()
        finally:
            self._running = False
            self._lifecycle("stopped")
            self.emitter.close()

    def publish_activity(
        self,
        state: str,
        *,
        source: str = "adapter",
        session_id: str | None = None,
        ttl_ms: int | None = None,
    ) -> bool:
        """Publish only the coarse, privacy-bounded activity category."""
        if state not in ACTIVITY_STATES:
            raise ValueError(f"Unknown activity state: {state}")
        if ttl_ms is None:
            ttl_ms = round(state_ttl_seconds(state) * 1000)
        ttl_ms = 0 if state == "idle" else max(500, min(30000, int(ttl_ms)))
        event = self._event(
            "activity",
            session_id,
            {"state": state, "source": _bounded_source(source), "ttl_ms": ttl_ms},
        )
        self.inbox.set_state(ACTIVITY_STATE_KEY, event.as_dict())
        return self.emitter.send(
            state,
            source=_bounded_source(source),
            session_id=session_id,
            ttl_ms=ttl_ms,
        )

    def enqueue_speech(self, message: dict[str, object]) -> bool:
        """Send a validated visible-output envelope to the single playback owner."""
        event_id = message.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("speech message requires event_id")
        if message.get("project_root") != str(self.project_root):
            raise ValueError("speech message project_root does not belong to this service")
        inserted = self.playback.enqueue(message)
        self.inbox.set_state(
            LAST_SPEECH_KEY,
            {
                "event_id": event_id,
                "session_id": message.get("session_id"),
                "turn_id": message.get("turn_id"),
                "kind": message.get("kind"),
                "accepted": inserted,
                "updated_at": _timestamp(),
            },
        )
        return inserted

    def publish_update(self, message: dict[str, object]) -> bool:
        """Publish ephemeral progress without inserting it into SQLite.

        Updates are deliberately latest-value/coalesced state.  The playback
        owner may drop them when a durable message is pending or when the
        update belongs to a session that does not currently own attention.
        """
        event_id = message.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("update requires event_id")
        if message.get("project_root") != str(self.project_root):
            raise ValueError("update project_root does not belong to this service")
        if not isinstance(message.get("text"), str) or not str(message["text"]).strip():
            raise ValueError("update requires non-empty text")
        accepted = self.playback.publish_update(message)
        self.inbox.set_state(
            LAST_UPDATE_KEY,
            {
                "event_id": event_id,
                "session_id": message.get("session_id"),
                "turn_id": message.get("turn_id"),
                "kind": message.get("kind", "commentary"),
                "accepted": accepted,
                "updated_at": _timestamp(),
            },
        )
        return accepted

    def drain_completed(self) -> list[dict[str, object]]:
        return self.playback.drain_completed()

    def is_idle(self) -> bool:
        return self.playback.is_idle()

    def status(self) -> dict[str, object]:
        return {
            "schema": SERVICE_SCHEMA,
            "running": self._running,
            "adapter": self.adapter_name,
            "project_root": str(self.project_root),
            "state": self.inbox.get_state(SERVICE_STATE_KEY, {"state": "unknown"}),
            "attention": self.inbox.get_state("presence_attention", {"state": "unassigned"}),
            "last_update": self.inbox.get_state(LAST_UPDATE_KEY, {}),
        }
