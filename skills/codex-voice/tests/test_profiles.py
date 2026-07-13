from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from profiles import ProfileError, ProfileRegistry, normalize_document, write_document


class ProfileRegistryTests(unittest.TestCase):
    def test_legacy_project_settings_are_the_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voice_root = root / ".codex-voice"
            voice_root.mkdir()
            (voice_root / "voice").write_text("af_heart\n", encoding="utf-8")
            (voice_root / "speed").write_text("1.25\n", encoding="utf-8")
            (voice_root / "mode").write_text("stream\n", encoding="utf-8")
            (voice_root / "avatar-selection.json").write_text(
                json.dumps({"avatar_id": "higan-live2d"}), encoding="utf-8"
            )
            resolved = ProfileRegistry(root, voice_root).resolve("session-a")
            self.assertEqual(resolved.profile_id, "default")
            self.assertEqual(resolved.avatar_id, "higan-live2d")
            self.assertEqual(resolved.tts_voice, "af_heart")
            self.assertEqual(resolved.tts_speed, 1.25)
            self.assertEqual(resolved.route_key, "session:session-a|profile:default")

    def test_session_binding_resolves_its_own_avatar_and_voice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            voice_root = root / ".codex-voice"
            write_document(
                voice_root,
                {
                    "schema": "codex-ai-presence/profiles/v0.1",
                    "project_profile_id": "sol",
                    "profiles": {
                        "sol": {"avatar_id": "builtin", "voice": "af_heart", "speed": 1.0},
                        "luna": {"avatar_id": "higan-live2d", "voice": "bf_isabella", "speed": 1.2},
                    },
                    "sessions": {"session-luna": {"profile_id": "luna"}},
                },
            )
            registry = ProfileRegistry(root, voice_root)
            luna = registry.resolve("session-luna")
            sol = registry.resolve("session-sol")
            self.assertEqual(
                (luna.profile_id, luna.avatar_id, luna.tts_voice),
                ("luna", "higan-live2d", "bf_isabella"),
            )
            self.assertEqual(
                (sol.profile_id, sol.avatar_id, sol.tts_voice),
                ("sol", "builtin", "af_heart"),
            )
            self.assertNotEqual(luna.route_key, sol.route_key)

    def test_missing_profile_binding_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            normalize_document(
                {
                    "schema": "codex-ai-presence/profiles/v0.1",
                    "project_profile_id": "default",
                    "profiles": {"default": {}},
                    "sessions": {"session-a": "missing"},
                }
            )


if __name__ == "__main__":
    unittest.main()
