from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import runtime_adapter


class FakeBindingClient:
    created: list[str | None] = []
    started: list[str | None] = []

    def __init__(
        self,
        _project_root: Path,
        session_id: str | None,
        *,
        adapter: str,
        capabilities: list[str],
    ) -> None:
        assert adapter == "test-adapter"
        assert "speech" in capabilities
        self.session_id = session_id
        self.registration = {"binding_id": f"binding-{session_id or 'project'}"}
        self.created.append(session_id)

    def start(self) -> dict:
        self.started.append(self.session_id)
        return self.registration

    def activity(self, _state: str, _event_id: str) -> bool:
        self.start()
        return True

    def close(self) -> None:
        return


def test_adapter_start_does_not_lease_an_unused_project_binding(
    tmp_path, monkeypatch
) -> None:
    FakeBindingClient.created.clear()
    FakeBindingClient.started.clear()
    monkeypatch.setattr(runtime_adapter, "BindingClient", FakeBindingClient)
    adapter = runtime_adapter.RuntimePlaybackAdapter(
        tmp_path, adapter="test-adapter"
    )
    try:
        adapter.start()
        assert FakeBindingClient.created == []
        adapter.publish_activity(
            "thinking",
            session_id="019f694f-8273-7120-a2c2-53147a089da9",
            event_id="event-1",
        )
        assert FakeBindingClient.created == [
            "019f694f-8273-7120-a2c2-53147a089da9"
        ]
        assert None not in FakeBindingClient.started
    finally:
        adapter.close()


class PendingInputClient:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items
        self.acknowledged: list[str] = []

    def pending_inputs(self) -> list[dict[str, str]]:
        return list(self.items)

    def acknowledge_input(self, input_id: str) -> None:
        self.acknowledged.append(input_id)

    def close(self) -> None:
        return


def test_sequential_ready_transcripts_replace_clipboard_and_acknowledge_in_order(
    tmp_path, monkeypatch
) -> None:
    adapter = runtime_adapter.RuntimePlaybackAdapter(tmp_path)
    client = PendingInputClient(
        [
            {"input_id": "input-1", "transcript": "older transcript"},
            {"input_id": "input-2", "transcript": "newest transcript"},
        ]
    )
    adapter.clients["session-a"] = client
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        adapter,
        "_deliver_input",
        lambda session_id, transcript: delivered.append((session_id, transcript)),
    )

    adapter._poll_inputs_once()

    assert delivered == [
        ("session-a", "older transcript"),
        ("session-a", "newest transcript"),
    ]
    assert client.acknowledged == ["input-1", "input-2"]


def test_failed_clipboard_delivery_keeps_transcript_ready_for_retry(
    tmp_path, monkeypatch
) -> None:
    adapter = runtime_adapter.RuntimePlaybackAdapter(tmp_path)
    client = PendingInputClient(
        [{"input_id": "input-1", "transcript": "retry this transcript"}]
    )
    adapter.clients["session-a"] = client

    def fail_delivery(_session_id: str, _transcript: str) -> None:
        raise RuntimeError("clipboard unavailable")

    monkeypatch.setattr(adapter, "_deliver_input", fail_delivery)

    adapter._poll_inputs_once()

    assert client.acknowledged == []
