"""Connection-bound Presence Runtime registration and adapter event server."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .controller import RuntimeController
from .errors import PresenceError, ValidationError
from .protocol import FramedConnection, RuntimeAddress, RuntimeListener


AUTHORITY_FIELDS = {
    "source_id",
    "project_instance_id",
    "binding_id",
    "lease_token",
    "profile_id",
    "profile_ref",
    "avatar_id",
    "avatar_ref",
    "route_key",
    "orb_port",
}


@dataclass(slots=True)
class ConnectionContext:
    source_id: str
    project_instance_id: str
    binding_id: str
    lease_token: str


class RuntimeProtocolHandler:
    def __init__(self, controller: RuntimeController) -> None:
        self.controller = controller

    def serve_connection(self, connection: FramedConnection) -> None:
        context: ConnectionContext | None = None
        try:
            first = connection.recv()
            if first is None:
                return
            try:
                registration = self._register(first)
                context = ConnectionContext(
                    source_id=registration["source_id"],
                    project_instance_id=registration["project_instance_id"],
                    binding_id=registration["binding_id"],
                    lease_token=registration["lease_token"],
                )
                connection.send(
                    {
                        "type": "registered",
                        **registration,
                    }
                )
            except PresenceError as exc:
                connection.send(self._error(exc))
                return

            while True:
                message = connection.recv()
                if message is None:
                    return
                try:
                    response = self._dispatch(context, message)
                except PresenceError as exc:
                    response = self._error(exc)
                connection.send(response)
                if response.get("type") == "disconnected":
                    return
        finally:
            if context is not None:
                self.controller.store.disconnect_source(context.source_id)
            connection.close()

    def _register(self, message: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"type", "adapter", "project_root", "session_id", "capabilities"}
        unknown = set(message) - allowed
        if message.get("type") != "register":
            raise ValidationError("first IPC message must be register")
        if unknown:
            raise ValidationError(
                f"registration contains non-authoritative or unknown fields: {sorted(unknown)}"
            )
        required = {"adapter", "project_root", "capabilities"}
        missing = required - set(message)
        if missing:
            raise ValidationError(f"registration omitted fields: {sorted(missing)}")
        registration = self.controller.store.register_source(
            adapter=message["adapter"],
            project_root=message["project_root"],
            session_id=message.get("session_id"),
            capabilities=message["capabilities"],
        )
        snapshot = self.controller.ensure_effective(registration["binding_id"])
        registration["effective_revision"] = snapshot.revision
        return registration

    def _dispatch(
        self,
        context: ConnectionContext,
        message: Mapping[str, Any],
    ) -> dict[str, Any]:
        forbidden = set(message) & AUTHORITY_FIELDS
        if forbidden:
            raise ValidationError(
                f"connection-bound message may not nominate authority fields: {sorted(forbidden)}"
            )
        message_type = message.get("type")
        if message_type == "lease/refresh":
            self._require_fields(message, {"type"})
            refreshed = self.controller.store.refresh_lease(
                context.source_id,
                context.lease_token,
            )
            return {"type": "lease/refreshed", **refreshed}
        if message_type == "activity":
            self._require_fields(message, {"type", "event_id", "state"})
            snapshot = self.controller.set_activity(
                source_id=context.source_id,
                binding_id=context.binding_id,
                event_id=message["event_id"],
                activity=message["state"],
            )
            return {
                "type": "activity/accepted",
                "event_id": message["event_id"],
                "effective_revision": snapshot.revision,
                "effective_actions": list(snapshot.semantic.effective_actions),
            }
        if message_type == "speech/enqueue":
            self._require_fields(
                message,
                {"type", "event_id", "utterance_id", "text", "kind"},
            )
            queue_id = self.controller.enqueue_speech(
                source_id=context.source_id,
                binding_id=context.binding_id,
                event_id=message["event_id"],
                utterance_id=message["utterance_id"],
                text=message["text"],
                kind=message["kind"],
            )
            return {
                "type": "speech/enqueued",
                "event_id": message["event_id"],
                "queue_id": queue_id,
                "duplicate": queue_id is None,
            }
        if message_type == "binding/status":
            self._require_fields(message, {"type"})
            binding = self.controller.store.binding(context.binding_id)
            snapshot = self.controller.store.effective_snapshot(context.binding_id)
            return {
                "type": "binding/status",
                "binding": binding,
                "effective": snapshot.to_document() if snapshot else None,
            }
        if message_type == "ping":
            self._require_fields(message, {"type"})
            self.controller.store.assert_source_active(
                context.source_id,
                binding_id=context.binding_id,
            )
            return {"type": "pong"}
        if message_type == "disconnect":
            self._require_fields(message, {"type"})
            return {"type": "disconnected"}
        raise ValidationError(f"unsupported adapter message type: {message_type!r}")

    @staticmethod
    def _require_fields(message: Mapping[str, Any], allowed: set[str]) -> None:
        unknown = set(message) - allowed
        missing = allowed - set(message)
        if unknown or missing:
            raise ValidationError(
                f"message fields are invalid: missing={sorted(missing)}, unknown={sorted(unknown)}"
            )

    @staticmethod
    def _error(error: PresenceError) -> dict[str, Any]:
        return {
            "type": "error",
            "error": {
                "code": error.code,
                "message": str(error),
            },
        }


class PresenceServer:
    """Threaded local server; one handler context is bound to one connection."""

    def __init__(
        self,
        controller: RuntimeController,
        *,
        address: RuntimeAddress | None = None,
    ) -> None:
        self.controller = controller
        self.listener = RuntimeListener(address)
        self.handler = RuntimeProtocolHandler(controller)
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()

    def serve_forever(self) -> None:
        self.listener.open()
        try:
            while not self._stop.is_set():
                connection = self.listener.accept()
                thread = threading.Thread(
                    target=self._serve_thread,
                    args=(connection,),
                    name="presence-client",
                    daemon=True,
                )
                self._threads.add(thread)
                thread.start()
                self.controller.store.expire_leases()
        finally:
            self.listener.close()
            for thread in tuple(self._threads):
                thread.join(timeout=2)

    def _serve_thread(self, connection: FramedConnection) -> None:
        try:
            self.handler.serve_connection(connection)
        finally:
            self._threads.discard(threading.current_thread())

    def stop(self) -> None:
        self._stop.set()
        self.listener.close()
