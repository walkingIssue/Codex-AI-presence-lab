from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from avatar_state import AvatarStateError, sync_state, write_state


class AvatarStateRoutingTests(unittest.TestCase):
    def project(self, directory: str) -> Path:
        root = Path(directory)
        voice = root / ".codex-voice"
        voice.mkdir()
        (voice / "avatar-selection.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/avatar-selection/v0.1",
                    "avatar_id": "higan-live2d",
                }
            ),
            encoding="utf-8",
        )
        (voice / "presence-profiles.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/profiles/v0.1",
                    "project_profile_id": "luna",
                    "profiles": {"luna": {"avatar_id": "higan-live2d"}},
                    "sessions": {
                        "session-a": {"profile_id": "luna"},
                        "session-b": {"profile_id": "luna"},
                    },
                }
            ),
            encoding="utf-8",
        )
        bundle = root / ".codex-voice-avatars" / "higan-live2d"
        bundle.mkdir(parents=True)
        (bundle / "avatar.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/avatar/v0.1",
                    "id": "higan-live2d",
                    "capabilities": ["avatar-state-v1"],
                }
            ),
            encoding="utf-8",
        )
        (bundle / "avatar-capabilities.json").write_text(
            json.dumps({"avatar_id": "higan-live2d"}), encoding="utf-8"
        )
        return root

    def args(self, root: Path, session_id: str, revision: int, actions: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            project_root=root,
            avatar_id="higan-live2d",
            source="live2d-avatar-controls",
            scope="route",
            revision=revision,
            actions_json=json.dumps(actions),
            session_id=session_id,
            profile_id="luna",
        )

    def test_routes_keep_independent_complete_action_sets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            first = write_state(self.args(root, "session-a", 1, ["pose.a"]))
            second = write_state(self.args(root, "session-b", 1, ["pose.b"]))
            ledger = json.loads(
                (root / ".codex-voice" / "avatar-states.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first["route_key"], "session:session-a|profile:luna")
            self.assertEqual(second["route_key"], "session:session-b|profile:luna")
            self.assertEqual(ledger["states"][first["route_key"]]["actions"], ["pose.a"])
            self.assertEqual(ledger["states"][second["route_key"]]["actions"], ["pose.b"])
            self.assertFalse((root / ".codex-voice" / "avatar-state.json").exists())

    def test_revisions_are_monotonic_per_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            write_state(self.args(root, "session-a", 2, ["pose.a"]))
            write_state(self.args(root, "session-b", 1, ["pose.b"]))
            with self.assertRaises(AvatarStateError):
                write_state(self.args(root, "session-a", 1, ["pose.old"]))

    def test_sync_replays_only_the_selected_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            write_state(self.args(root, "session-a", 3, ["pose.a"]))
            replay = sync_state(
                argparse.Namespace(
                    project_root=root,
                    session_id="session-a",
                    profile_id="luna",
                    avatar_id="higan-live2d",
                )
            )
            self.assertEqual(replay["route_key"], "session:session-a|profile:luna")
            self.assertEqual(replay["revision"], 3)


if __name__ == "__main__":
    unittest.main()
