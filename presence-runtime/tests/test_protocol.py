from __future__ import annotations

import socket
import threading
import uuid
import os

from presence_runtime.catalog import Catalog
from presence_runtime.controller import RecordingRenderer, RuntimeController
from presence_runtime.protocol import (
    RuntimeAddress,
    RuntimeListener,
    SocketFramedConnection,
    connect,
)
from presence_runtime.server import RuntimeProtocolHandler
from presence_runtime.store import PresenceStore
from presence_runtime.worker import RecordingWorker


def build_handler(tmp_path):
    store = PresenceStore(tmp_path / "state.sqlite3")
    controller = RuntimeController(
        store=store,
        catalog=Catalog(tmp_path / "catalog"),
        voice=RecordingWorker(),
        renderer=RecordingRenderer(),
    )
    return RuntimeProtocolHandler(controller), controller


def start_pair(handler):
    server_socket, client_socket = socket.socketpair()
    server = SocketFramedConnection(server_socket)
    client = SocketFramedConnection(client_socket)
    thread = threading.Thread(
        target=handler.serve_connection,
        args=(server,),
        daemon=True,
    )
    thread.start()
    return client, thread


def register(client, root, session_id):
    client.send(
        {
            "type": "register",
            "adapter": "codex-gui",
            "project_root": str(root),
            "session_id": session_id,
            "capabilities": ["speech", "activity"],
        }
    )
    return client.recv()


def test_socket_transport_preserves_utf8_json_frames() -> None:
    left_socket, right_socket = socket.socketpair()
    left = SocketFramedConnection(left_socket)
    right = SocketFramedConnection(right_socket)
    left.send({"type": "hello", "text": "Higan says hej 👋"})
    assert right.recv() == {"type": "hello", "text": "Higan says hej 👋"}
    left.close()
    right.close()


def test_platform_runtime_transport_round_trip(tmp_path) -> None:
    if os.name == "nt":
        address = RuntimeAddress(
            "named-pipe",
            rf"\\.\pipe\presence-contract-{uuid.uuid4()}",
        )
    else:
        address = RuntimeAddress("unix", str(tmp_path / "presence.sock"))
    listener = RuntimeListener(address)
    listener.open()

    def serve():
        connection = listener.accept()
        message = connection.recv()
        connection.send({"type": "ack", "echo": message["text"]})
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = connect(address)
    client.send({"type": "probe", "text": "framed utf8 ✓"})
    assert client.recv() == {"type": "ack", "echo": "framed utf8 ✓"}
    client.close()
    thread.join(timeout=2)
    listener.close()
    assert not thread.is_alive()


def test_connection_bound_registration_rejects_spoofed_authority_and_deduplicates(
    tmp_path,
) -> None:
    handler, controller = build_handler(tmp_path)
    client, thread = start_pair(handler)
    response = register(client, tmp_path / "project", str(uuid.uuid4()))

    assert response["type"] == "registered"
    assert response["source_id"]
    assert response["binding_id"]
    assert response["lease_token"]
    assert response["effective_revision"] == 1

    client.send(
        {
            "type": "activity",
            "event_id": "activity:spoof",
            "state": "thinking",
            "binding_id": str(uuid.uuid4()),
        }
    )
    error = client.recv()
    assert error["type"] == "error"
    assert "may not nominate authority" in error["error"]["message"]

    client.send(
        {
            "type": "activity",
            "event_id": "activity:1",
            "state": "thinking",
        }
    )
    activity = client.recv()
    assert activity["type"] == "activity/accepted"

    speech = {
        "type": "speech/enqueue",
        "event_id": "final:1",
        "utterance_id": str(uuid.uuid4()),
        "text": "One durable final.",
        "kind": "final",
    }
    client.send(speech)
    first = client.recv()
    client.send(speech)
    duplicate = client.recv()
    assert first["queue_id"] is not None
    assert duplicate["duplicate"] is True
    assert len(controller.store.speech_items()) == 1

    client.send({"type": "disconnect"})
    assert client.recv()["type"] == "disconnected"
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_registration_rejects_adapter_selected_renderer_fields(tmp_path) -> None:
    handler, _controller = build_handler(tmp_path)
    client, thread = start_pair(handler)
    client.send(
        {
            "type": "register",
            "adapter": "codex-tui",
            "project_root": str(tmp_path / "project"),
            "session_id": str(uuid.uuid4()),
            "capabilities": [],
            "orb_port": 17831,
            "profile_id": "hijack",
        }
    )
    response = client.recv()
    assert response["type"] == "error"
    assert "orb_port" in response["error"]["message"]
    thread.join(timeout=2)


def test_identical_session_identity_in_two_projects_gets_distinct_bindings(
    tmp_path,
) -> None:
    handler, _controller = build_handler(tmp_path)
    session_id = str(uuid.uuid4())
    first_client, first_thread = start_pair(handler)
    second_client, second_thread = start_pair(handler)
    first = register(first_client, tmp_path / "first", session_id)
    second = register(second_client, tmp_path / "second", session_id)

    assert first["project_instance_id"] != second["project_instance_id"]
    assert first["binding_id"] != second["binding_id"]
    first_client.send({"type": "disconnect"})
    first_client.recv()
    second_client.send({"type": "disconnect"})
    second_client.recv()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)


def test_playback_status_pause_resume_and_cancel_are_binding_scoped(tmp_path) -> None:
    handler, controller = build_handler(tmp_path)
    client, thread = start_pair(handler)
    registered = register(client, tmp_path / "project", str(uuid.uuid4()))
    event_id = "final:scoped-playback"
    utterance_id = str(uuid.uuid4())
    client.send(
        {
            "type": "speech/enqueue",
            "event_id": event_id,
            "utterance_id": utterance_id,
            "text": "Pause and cancel only this binding.",
            "kind": "final",
        }
    )
    assert client.recv()["type"] == "speech/enqueued"
    item = controller.store.claim_next_speech()
    assert item is not None
    controller.store.begin_playback(registered["binding_id"], utterance_id)
    controller.store.update_speech_status(item["queue_id"], "playing")

    client.send({"type": "playback/status", "event_ids": [event_id]})
    status = client.recv()
    assert status["speech"][event_id]["status"] == "playing"

    client.send({"type": "playback/pause"})
    assert client.recv()["paused"] is True
    assert controller.voice.pause_requests == 1
    client.send({"type": "playback/resume"})
    assert client.recv()["resumed"] is True
    assert controller.voice.resume_requests == 1

    client.send({"type": "speech/cancel", "event_ids": [event_id]})
    assert client.recv()["cancelled"] == 1
    assert controller.voice.cancel_requests == 1
    client.send({"type": "playback/status", "event_ids": [event_id]})
    assert client.recv()["speech"][event_id]["status"] == "cancelled"

    client.send({"type": "disconnect"})
    client.recv()
    thread.join(timeout=2)


def test_voice_input_acknowledges_delivery_on_the_exact_renderer_binding(tmp_path) -> None:
    handler, controller = build_handler(tmp_path)
    client, thread = start_pair(handler)
    registered = register(client, tmp_path / "project", str(uuid.uuid4()))
    input_id = controller.store.begin_input(registered["binding_id"], "capture-1")
    controller.store.finish_input(input_id, transcript="hello from speech input")

    client.send({"type": "input/poll"})
    pending = client.recv()
    assert pending["type"] == "input/pending"
    assert len(pending["items"]) == 1
    assert pending["items"][0]["input_id"] == input_id
    assert pending["items"][0]["capture_id"] == "capture-1"
    assert pending["items"][0]["transcript"] == "hello from speech input"

    client.send({"type": "input/ack", "input_id": input_id})
    assert client.recv() == {"type": "input/acknowledged", "input_id": input_id}
    assert controller.renderer.inputs[-1] == {
        "binding_id": registered["binding_id"],
        "capture_id": "capture-1",
        "state": "delivered",
    }

    client.send({"type": "disconnect"})
    client.recv()
    thread.join(timeout=2)
