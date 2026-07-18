from __future__ import annotations

import sys

from presence_runtime.stt import STTWorkerSupervisor


def test_one_stt_worker_is_warm_and_returns_binding_delivery_text(tmp_path) -> None:
    script = tmp_path / "fake_stt.py"
    script.write_text(
        """
import json
import sys

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"ok": True, "request_id": request["request_id"], "text": "hello from mic"}), flush=True)
""",
        encoding="utf-8",
    )
    recording = tmp_path / "capture.webm"
    recording.write_bytes(b"fake")
    worker = STTWorkerSupervisor(
        python=__import__("pathlib").Path(sys.executable),
        script=script,
        runtime_root=tmp_path,
    )

    assert worker.start() is True
    first_pid = worker.status()["pid"]
    assert worker.transcribe(recording) == "hello from mic"
    assert worker.status()["pid"] == first_pid
    worker.stop()
    assert worker.status()["running"] is False
