"""Connection-bound Presence Runtime registration and adapter event server."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Mapping

from .controller import RuntimeController
from .control import ControlAPI
from .errors import ConflictError, PresenceError, ValidationError
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
    def __init__(
        self,
        controller: RuntimeController,
        control_api: ControlAPI | None = None,
        migrator: Any | None = None,
    ) -> None:
        self.controller = controller
        self.control_api = control_api
        self.migrator = migrator

    def serve_connection(self, connection: FramedConnection) -> None:
        context: ConnectionContext | None = None
        try:
            first = connection.recv()
            if first is None:
                return
            if first.get("type") == "control":
                self._serve_control(connection, first)
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
                self.controller.sync_binding_visibility()
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
                self.controller.sync_binding_visibility()
            connection.close()

    def _serve_control(
        self,
        connection: FramedConnection,
        first: Mapping[str, Any],
    ) -> None:
        if self.control_api is None:
            connection.send(
                self._error(ValidationError("runtime control API is unavailable"))
            )
            return
        if set(first) - {"type", "client"}:
            connection.send(
                self._error(ValidationError("control handshake contains unknown fields"))
            )
            return
        connection.send({"type": "control/ready"})
        while True:
            request = connection.recv()
            if request is None:
                return
            if request.get("type") == "disconnect":
                connection.send({"type": "disconnected"})
                return
            try:
                if set(request) != {"type", "operation", "arguments"}:
                    raise ValidationError("control command fields are invalid")
                if request["type"] != "command":
                    raise ValidationError("control message must be command")
                arguments = request["arguments"]
                if not isinstance(arguments, dict):
                    raise ValidationError("control command arguments must be an object")
                result = self.control_api.execute(
                    request["operation"],
                    arguments,
                )
                response = {"type": "result", "result": result}
            except PresenceError as exc:
                response = self._error(exc)
            except Exception as exc:
                response = self._error(
                    ConflictError(f"runtime command failed: {type(exc).__name__}: {exc}")
                )
            connection.send(response)

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
        try:
            if self.migrator is not None:
                project = self.controller.store.project(
                    registration["project_instance_id"]
                )
                self.migrator.migrate_on_registration(
                    registration["project_instance_id"],
                    __import__("pathlib").Path(project["project_root"]),
                )
            snapshot = self.controller.ensure_effective(registration["binding_id"])
        except PresenceError:
            self.controller.store.disconnect_source(registration["source_id"])
            raise
        except BaseException as exc:
            self.controller.store.disconnect_source(registration["source_id"])
            raise ConflictError(f"automatic v0.1 migration failed: {exc}") from exc
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
            accepted = self.controller.set_activity(
                source_id=context.source_id,
                binding_id=context.binding_id,
                event_id=message["event_id"],
                activity=message["state"],
            )
            return {
                "type": "activity/accepted",
                "event_id": message["event_id"],
                "effective_revision": accepted.effective_revision,
                "effective_actions": list(accepted.effective_actions),
                "presentation_sequence": accepted.presentation_sequence,
                "disposition": accepted.disposition,
                "duplicate": accepted.duplicate,
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
        if message_type == "speech/cancel":
            self._require_fields(message, {"type", "event_ids"})
            event_ids = message["event_ids"]
            if not isinstance(event_ids, list) or len(event_ids) > 1024:
                raise ValidationError("speech cancellation event_ids must be a bounded list")
            self.controller.store.assert_source_active(
                context.source_id, binding_id=context.binding_id
            )
            cancelled = self.controller.cancel_speech(context.binding_id, event_ids)
            return {"type": "speech/cancelled", "cancelled": cancelled}
        if message_type == "binding/status":
            self._require_fields(message, {"type"})
            binding = self.controller.store.binding(context.binding_id)
            snapshot = self.controller.store.effective_snapshot(context.binding_id)
            return {
                "type": "binding/status",
                "binding": binding,
                "effective": snapshot.to_document() if snapshot else None,
            }
        if message_type == "playback/pause":
            self._require_fields(message, {"type"})
            result = self.controller.pause_playback(context.binding_id)
            return {"type": "playback/paused", **result}
        if message_type == "playback/resume":
            self._require_fields(message, {"type"})
            result = self.controller.resume_playback(context.binding_id)
            return {"type": "playback/resumed", **result}
        if message_type == "playback/status":
            self._require_fields(message, {"type"}, optional={"event_ids"})
            self.controller.store.assert_source_active(
                context.source_id, binding_id=context.binding_id
            )
            event_ids = message.get("event_ids", [])
            if not isinstance(event_ids, list):
                raise ValidationError("playback status event_ids must be a list")
            return {
                "type": "playback/status",
                "binding_id": context.binding_id,
                "attention": self.controller.store.attention(),
                "speech": self.controller.store.speech_statuses(
                    context.binding_id, event_ids
                ),
            }
        if message_type == "input/poll":
            self._require_fields(message, {"type"})
            self.controller.store.assert_source_active(
                context.source_id, binding_id=context.binding_id
            )
            return {
                "type": "input/pending",
                "items": self.controller.store.pending_inputs(context.binding_id),
            }
        if message_type == "input/ack":
            self._require_fields(message, {"type", "input_id"})
            self.controller.store.assert_source_active(
                context.source_id, binding_id=context.binding_id
            )
            input_id = message["input_id"]
            if not isinstance(input_id, str) or not input_id:
                raise ValidationError("input_id must be a non-empty string")
            self.controller.store.acknowledge_input(context.binding_id, input_id)
            return {"type": "input/acknowledged", "input_id": input_id}
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
    def _require_fields(
        message: Mapping[str, Any],
        allowed: set[str],
        *,
        optional: set[str] | None = None,
    ) -> None:
        optional = optional or set()
        unknown = set(message) - allowed - optional
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
        control_api: ControlAPI | None = None,
        migrator: Any | None = None,
    ) -> None:
        self.controller = controller
        self.listener = RuntimeListener(address)
        self.handler = RuntimeProtocolHandler(controller, control_api, migrator)
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._reaper: threading.Thread | None = None

    def serve_forever(self) -> None:
        self.listener.open()
        self._reaper = threading.Thread(
            target=self._reap_leases,
            name="presence-lease-reaper",
            daemon=True,
        )
        self._reaper.start()
        try:
            while not self._stop.is_set():
                try:
                    connection = self.listener.accept()
                except (OSError, EOFError):
                    if self._stop.is_set():
                        break
                    raise
                thread = threading.Thread(
                    target=self._serve_thread,
                    args=(connection,),
                    name="presence-client",
                    daemon=True,
                )
                self._threads.add(thread)
                thread.start()
        finally:
            self.listener.close()
            for thread in tuple(self._threads):
                thread.join(timeout=2)
            if self._reaper is not None:
                self._reaper.join(timeout=2)

    def _reap_leases(self) -> None:
        while not self._stop.wait(5):
            expired = self.controller.store.expire_leases()
            if expired:
                self.controller.sync_binding_visibility()

    def _serve_thread(self, connection: FramedConnection) -> None:
        try:
            self.handler.serve_connection(connection)
        finally:
            self._threads.discard(threading.current_thread())

    def stop(self) -> None:
        self._stop.set()
        # Wake a blocking accept before closing the listener.  This is
        # particularly important for multiprocessing AF_PIPE on Windows.
        try:
            from .protocol import connect

            wake = connect(self.listener.address, timeout=0.2)
            wake.close()
        except (OSError, PresenceError):
            pass
        self.listener.close()
