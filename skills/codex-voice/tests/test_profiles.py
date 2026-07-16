from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from profiles import ProfileError, ProfileRegistry, normalize_document, require_project_session, write_document


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

    def test_profile_curation_preserves_explicit_child_overrides(self) -> None:
        document = normalize_document(
            {
                "schema": "codex-ai-presence/profiles/v0.1",
                "project_profile_id": "codex",
                "profiles": {
                    "codex": {
                        "avatar_id": "higan-live2d",
                        "curation": {
                            "initial_actions": ["eyes.dazed", "pose.sweater-default"],
                            "activity_actions": {
                                "idle": {"add": [], "suppress": []},
                                "thinking": {"add": ["eyes.dazed"], "suppress": []},
                            },
                        },
                    }
                },
                "sessions": {},
            }
        )
        curation = document["profiles"]["codex"]["curation"]
        self.assertEqual(curation["initial_actions"], ["eyes.dazed", "pose.sweater-default"])
        self.assertEqual(curation["activity_actions"]["idle"]["suppress"], [])
        self.assertEqual(curation["activity_actions"]["thinking"]["add"], ["eyes.dazed"])

    def test_profile_curation_rejects_raw_or_unknown_controls(self) -> None:
        for curation in (
            {"fixed_parameters": [{"parameter_id": "Key7", "value": 1}]},
            {"activity_actions": {"deploying": {"add": []}}},
            {"initial_actions": ["../../model.json"]},
            {"activity_actions": {"idle": {"suppress": ["pose.sweater-default"]}, "extra": {}}},
        ):
            with self.subTest(curation=curation), self.assertRaises(ProfileError):
                normalize_document(
                    {
                        "schema": "codex-ai-presence/profiles/v0.1",
                        "project_profile_id": "codex",
                        "profiles": {"codex": {"curation": curation}},
                        "sessions": {},
                    }
                )

    def test_session_binding_must_belong_to_the_target_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            voice_root = root / ".codex-voice"
            voice_root.mkdir()
            (voice_root / "sessions.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mode": "session",
                        "sessions": {
                            "session-local": {
                                "enabled": True,
                                "project_root": str(root),
                            },
                            "session-foreign": {
                                "enabled": True,
                                "project_root": str(root.parent / "other"),
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            require_project_session(root, voice_root, "session-local")
            with self.assertRaisesRegex(ProfileError, "is not enabled"):
                require_project_session(root, voice_root, "session-missing")
            with self.assertRaisesRegex(ProfileError, "belongs to"):
                require_project_session(root, voice_root, "session-foreign")


if __name__ == "__main__":
    unittest.main()
