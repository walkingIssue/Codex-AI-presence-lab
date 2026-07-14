from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import speak


class ProfileTtsTests(unittest.TestCase):
    def test_openvino_uses_cpu_synthesis_tail_and_arc_bert_graph(self) -> None:
        with patch.object(speak, "configured_provider", return_value="openvino"):
            self.assertEqual(
                speak.configured_model_path(), speak.OPENVINO_TAIL_MODEL_PATH
            )
            self.assertEqual(
                speak.OPENVINO_BERT_MODEL_PATH.name,
                "kokoro-v1.0.bert-openvino.onnx",
            )

    def test_request_voice_speed_and_mode_are_scoped_to_one_worker_request(self) -> None:
        observed: dict[str, object] = {}

        def capture(_payload: dict, *, emit_finish: bool) -> int:
            observed.update(
                {
                    "voice": speak.configured_voice(),
                    "speed": speak.configured_speed(),
                    "mode": speak.configured_mode(),
                    "volume": speak.configured_volume(),
                    "emit_finish": emit_finish,
                }
            )
            return 0

        original = {
            "CODEX_TTS_VOICE": "af_heart",
            "CODEX_TTS_SPEED": "1.0",
            "CODEX_TTS_MODE": "quality",
            "CODEX_TTS_VOLUME": "10",
        }
        with patch.dict(os.environ, original, clear=False), patch.object(
            speak, "_handle_payload", side_effect=capture
        ):
            result = speak.handle_payload(
                {
                    "tts_voice": "bf_isabella",
                    "tts_speed": 1.2,
                    "tts_mode": "stream",
                    "tts_volume": 35,
                },
                emit_finish=False,
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                observed,
                {
                    "voice": "bf_isabella",
                    "speed": 1.2,
                    "mode": "stream",
                    "volume": 35,
                    "emit_finish": False,
                },
            )
            for name, value in original.items():
                self.assertEqual(os.environ[name], value)


if __name__ == "__main__":
    unittest.main()
