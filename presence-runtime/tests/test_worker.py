from __future__ import annotations

import json
import sys
import uuid

from presence_runtime.worker import KokoroWorkerSupervisor


def test_one_user_level_worker_is_reused_and_receives_binding_identity(tmp_path) -> None:
    runtime_root = tmp_path / "presence"
    runtime_root.mkdir()
    worker_script = tmp_path / "fake_worker.py"
    worker_script.write_text(
        """
import json
import sys
from pathlib import Path

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    Path("last-request.json").write_text(json.dumps(request), encoding="utf-8")
    print(json.dumps({"done": True, "ok": True}), flush=True)
""",
        encoding="utf-8",
    )
    worker = KokoroWorkerSupervisor(
        runtime_root=runtime_root,
        python=__import__("pathlib").Path(sys.executable),
        worker_script=worker_script,
    )

    assert worker.start() is True
    first_pid = worker.status()["pid"]
    assert worker.start() is True
    assert worker.status()["pid"] == first_pid
    binding_id = str(uuid.uuid4())
    utterance_id = str(uuid.uuid4())
    result = worker.speak(
        {
            "binding_id": binding_id,
            "utterance_id": utterance_id,
            "event_id": "event:worker",
            "text": "Warm worker.",
            "kind": "final",
            "tts": {
                "voice_id": "af_heart",
                "speed": 1.1,
                "playback_mode": "stream",
                "volume": 50,
            },
        }
    )
    request = json.loads(
        (runtime_root / "last-request.json").read_text(encoding="utf-8")
    )
    assert result == "completed"
    assert request["tts_binding_id"] == binding_id
    assert request["tts_utterance_id"] == utterance_id
    worker.stop()
    assert worker.status()["running"] is False
