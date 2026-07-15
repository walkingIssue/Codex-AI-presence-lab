from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

from live2d_avatar.errors import AvatarRuntimeError
from live2d_avatar.bundle import _publish_route, materialize_bundle, publish_state
from live2d_avatar.cli import build_parser, main
from live2d_avatar.context import _confirmed_renderer_state, build_project_context, render_context_markdown
from live2d_avatar.hook_registration import context_hook_status, enable_context_hook
from live2d_avatar.importer import import_model
from live2d_avatar.lifecycle import bind_project, install_project, project_doctor, project_status, uninstall_project
from live2d_avatar.manifest import load_manifest
from live2d_avatar.profile import apply_profile, export_profile, scaffold_profile
from live2d_avatar.state import disable_actions, enable_actions, set_actions, show_state


class Live2DAvatarRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = self.root / "registry"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_route_uses_current_thread_and_rejects_ambiguous_wardrobe_broadcast(self) -> None:
        project = self.root / "project"
        voice = project / ".codex-voice"
        voice.mkdir(parents=True)
        (voice / "avatar-selection.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/avatar-selection/v0.1",
                    "avatar_id": "demo-avatar",
                }
            ),
            encoding="utf-8",
        )
        (voice / "presence-profiles.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/profiles/v0.1",
                    "project_profile_id": "higan",
                    "profiles": {"higan": {"avatar_id": "demo-avatar"}},
                    "sessions": {
                        "session-a": {"profile_id": "higan"},
                        "session-b": {"profile_id": "higan"},
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "session-a"}):
            self.assertEqual(
                _publish_route(
                    project,
                    "demo-avatar",
                    session_id=None,
                    profile_id=None,
                    project_wide=False,
                ),
                ("session-a", "higan"),
            )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(AvatarRuntimeError, "multiple sessions"):
                _publish_route(
                    project,
                    "demo-avatar",
                    session_id=None,
                    profile_id=None,
                    project_wide=False,
                )
        (voice / "sessions.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "mode": "session",
                    "sessions": {
                        "session-a": {
                            "enabled": True,
                            "project_root": str(project.resolve()),
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                _publish_route(
                    project,
                    "demo-avatar",
                    session_id=None,
                    profile_id=None,
                    project_wide=False,
                ),
                ("session-a", "higan"),
            )
        self.assertIsNone(
            _publish_route(
                project,
                "demo-avatar",
                session_id=None,
                profile_id=None,
                project_wide=True,
            )
        )

    def test_renderer_context_uses_acceptance_for_the_exact_route(self) -> None:
        voice = self.root / ".codex-voice"
        voice.mkdir()
        routes = {}
        statuses = {}
        for session_id, revision, action in (
            ("session-a", 3, "pose.a"),
            ("session-b", 8, "pose.b"),
        ):
            route_key = f"session:{session_id}|profile:higan"
            routes[route_key] = {
                "schema": "codex-ai-presence/avatar-state/v0.2",
                "type": "avatar-state",
                "avatar_id": "demo-avatar",
                "source": "live2d-avatar-controls",
                "scope": "route",
                "session_id": session_id,
                "profile_id": "higan",
                "route_key": route_key,
                "revision": revision,
                "actions": [action],
            }
            statuses[route_key] = {
                "schema": "codex-ai-presence/avatar-state-status/v0.1",
                "type": "avatar-state-status",
                "avatar_id": "demo-avatar",
                "accepted": True,
                "revision": revision,
                "action_count": 1,
                "route_key": route_key,
            }
        (voice / "avatar-states.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/avatar-state-ledger/v0.1",
                    "type": "avatar-state-ledger",
                    "states": routes,
                }
            ),
            encoding="utf-8",
        )
        (voice / "avatar-state-statuses.json").write_text(
            json.dumps(
                {
                    "schema": "codex-ai-presence/avatar-state-status-ledger/v0.1",
                    "type": "avatar-state-status-ledger",
                    "statuses": statuses,
                }
            ),
            encoding="utf-8",
        )
        index = {
            "pose.a": {"id": "pose.a", "label": "Pose A"},
            "pose.b": {"id": "pose.b", "label": "Pose B"},
        }
        state = _confirmed_renderer_state(
            self.root,
            "demo-avatar",
            index,
            session_id="session-a",
            profile_id="higan",
        )
        self.assertIsNotNone(state)
        self.assertEqual(state["revision"], 3)
        self.assertEqual(state["actions"][0]["id"], "pose.a")

    def _write_model_source(self) -> Path:
        source = self.root / "source"
        source.mkdir()
        (source / "avatar.model3.json").write_text("{}", encoding="utf-8")
        (source / "dazed.exp3.json").write_text(
            json.dumps(
                {
                    "Parameters": [
                        {"Id": "Key2", "Value": 1, "Blend": "Add"},
                        {"Id": "Key4", "Value": -1, "Blend": "Add"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (source / "metadata.vtube.json").write_text(
            json.dumps(
                {
                    "HotkeySettings": {
                        "Hotkeys": [
                            {
                                "File": "dazed.exp3.json",
                                "Triggers": {"Trigger1": "N1", "Trigger2": ""},
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        return source

    def _zip_source(self, source: Path) -> Path:
        archive = self.root / "avatar.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for item in source.rglob("*"):
                if item.is_file():
                    bundle.write(item, item.relative_to(source).as_posix())
        return archive

    def test_import_discovers_expression_hotkey_and_state(self) -> None:
        archive = self._zip_source(self._write_model_source())

        result = import_model(archive, "demo-avatar", self.registry)

        self.assertEqual(result["id"], "demo-avatar")
        manifest = load_manifest(self.registry, "demo-avatar")
        self.assertEqual(manifest["model"]["path"], "source/avatar.model3.json")
        self.assertEqual(manifest["state_semantics"], "active-toggle-set")
        self.assertEqual(len(manifest["actions"]), 1)
        action = manifest["actions"][0]
        self.assertEqual(action["label"], "dazed")
        self.assertEqual(action["hotkeys"], ["N1"])
        self.assertEqual(action["parameter_operations"][0]["parameter_id"], "Key2")

        updated = set_actions(self.registry, "demo-avatar", [action["id"]])
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["active_actions"], [action["id"]])
        self.assertEqual(updated["effective_parameter_operations"][1]["parameter_id"], "Key4")

        reset = disable_actions(self.registry, "demo-avatar", [action["id"]])
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["active_actions"], [])
        self.assertEqual(show_state(self.registry, "demo-avatar")["revision"], 2)

    def test_import_rejects_archive_traversal(self) -> None:
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escaped.model3.json", "{}")

        with self.assertRaises(AvatarRuntimeError):
            import_model(archive, "unsafe-avatar", self.registry)

        self.assertFalse((self.root / "escaped.model3.json").exists())
        self.assertFalse((self.registry / "unsafe-avatar").exists())

    def test_project_uninstall_removes_only_the_project_boundary(self) -> None:
        archive = self._zip_source(self._write_model_source())
        import_model(archive, "demo-avatar", self.registry)
        project = self.root / "project"
        project.mkdir()

        installed = install_project(project, "demo-avatar", self.registry)
        self.assertEqual(installed["status"], "installed")
        runtime = project / ".codex-live2d"
        self.assertTrue((runtime / "RUNTIME-MANIFEST.md").is_file())
        self.assertEqual(project_status(project)["status"], "installed")

        removed = uninstall_project(project, confirm=True)

        self.assertEqual(removed["status"], "removed")
        self.assertFalse(runtime.exists())
        self.assertTrue((self.registry / "demo-avatar" / "manifest.json").is_file())

    def test_active_toggle_set_keeps_multiple_profile_actions_enabled(self) -> None:
        source = self._write_model_source()
        (source / "overlay.exp3.json").write_text(
            json.dumps({"Parameters": [{"Id": "Overlay", "Value": 1, "Blend": "Add"}]}),
            encoding="utf-8",
        )
        archive = self._zip_source(source)
        import_model(archive, "demo-avatar", self.registry)
        profile = self.root / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "schema": "live2d-avatar/profile/v0.1",
                    "name": "Toggle profile",
                    "state_semantics": "active-toggle-set",
                    "actions": [
                        {"source_file": "dazed.exp3.json", "id": "toggle.base", "label": "Base toggle"},
                        {"source_file": "overlay.exp3.json", "id": "toggle.overlay", "label": "Overlay toggle"},
                    ],
                    "safe_default_actions": [],
                    "initial_actions": [],
                    "renderer": {},
                }
            ),
            encoding="utf-8",
        )

        apply_profile(self.registry, "demo-avatar", profile)
        enabled = enable_actions(self.registry, "demo-avatar", ["toggle.base", "toggle.overlay"])

        self.assertEqual(enabled["active_actions"], ["toggle.base", "toggle.overlay"])
        manifest = load_manifest(self.registry, "demo-avatar")
        self.assertEqual(manifest["state_semantics"], "active-toggle-set")
        self.assertEqual(manifest["profile"]["semantic_status"], "draft")
        self.assertTrue(all("exclusive_group" not in action for action in manifest["actions"]))
        self.assertEqual(
            [action["id"] for action in manifest["actions"]],
            ["toggle.base", "toggle.overlay"],
        )
        invalid = json.loads(profile.read_text(encoding="utf-8"))
        invalid["renderer"] = {"halo": {"enabled": "yes"}}
        profile.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(AvatarRuntimeError):
            apply_profile(self.registry, "demo-avatar", profile)
        invalid["renderer"] = {"activity_actions": {"future": ["toggle.base"]}}
        profile.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(AvatarRuntimeError):
            apply_profile(self.registry, "demo-avatar", profile)
        invalid["renderer"] = {
            "activity_actions": {"cli": {"add": ["toggle.base"], "suppress": ["toggle.base"]}}
        }
        profile.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(AvatarRuntimeError):
            apply_profile(self.registry, "demo-avatar", profile)
        invalid["renderer"] = {
            "speech_motion": {"mouth": {"primary_parameter_id": "Mouth", "jaw_gain": 0.5}}
        }
        profile.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaises(AvatarRuntimeError):
            apply_profile(self.registry, "demo-avatar", profile)
        valid = json.loads(profile.read_text(encoding="utf-8"))
        valid["renderer"] = {
            "speech_motion": {"mouth": {"primary_parameter_id": "Mouth", "mouth_gain": 0.4}}
        }
        profile.write_text(json.dumps(valid), encoding="utf-8")
        apply_profile(self.registry, "demo-avatar", profile)
        applied_profile = json.loads(
            (self.registry / "demo-avatar" / "profile.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            applied_profile["renderer"]["speech_motion"]["mouth"]["mouth_gain"],
            0.4,
        )

    def test_portable_profile_pack_matches_only_the_same_model_revision(self) -> None:
        source = self._write_model_source()
        archive = self._zip_source(source)
        import_model(archive, "demo-avatar", self.registry)
        draft = self.root / "draft.json"
        scaffold_profile(self.registry, "demo-avatar", draft)
        draft_document = json.loads(draft.read_text(encoding="utf-8"))
        self.assertEqual(draft_document["target"]["schema"], "live2d-avatar/profile-target/v0.1")
        draft_document["semantic_status"] = "curated"
        draft.write_text(json.dumps(draft_document), encoding="utf-8")
        apply_profile(self.registry, "demo-avatar", draft)

        exported = self.root / "reviewed-profile.json"
        result = export_profile(self.registry, "demo-avatar", exported)
        exported_document = json.loads(exported.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "exported")
        self.assertEqual(exported_document["target"]["fingerprint"], result["fingerprint"])

        import_model(archive, "matching-avatar", self.registry)
        matched = apply_profile(self.registry, "matching-avatar", exported)
        self.assertEqual(matched["semantic_status"], "curated")

        changed_source = self.root / "changed-source"
        shutil.copytree(source, changed_source)
        (changed_source / "avatar.model3.json").write_text('{"revision":2}', encoding="utf-8")
        import_model(self._zip_source(changed_source), "changed-avatar", self.registry)
        with self.assertRaises(AvatarRuntimeError):
            apply_profile(self.registry, "changed-avatar", exported)

        project = self.root / "profile-bind-project"
        project.mkdir()
        self._write_mock_voice_tools(project)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(
                [
                    "--registry",
                    str(self.registry),
                    "project",
                    "bind",
                    "--project",
                    str(project),
                    "--model",
                    "demo-avatar",
                    "--profile",
                    str(exported),
                    "--json",
                ]
            )
        bound = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(bound["status"], "bound")
        self.assertTrue(bound["restart_required"])
        self.assertEqual(bound["applied_profile"]["semantic_status"], "curated")

    def _write_mock_voice_tools(self, project: Path) -> None:
        voice = project / ".codex-voice"
        voice.mkdir()
        (voice / "avatar.py").write_text(
            """
import argparse
import json
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--project-root', type=Path, required=True)
commands = parser.add_subparsers(dest='command', required=True)
install = commands.add_parser('install')
install.add_argument('--source', type=Path, required=True)
install.add_argument('--replace', action='store_true')
install.add_argument('--use', action='store_true')
use = commands.add_parser('use')
use.add_argument('avatar_id')
args = parser.parse_args()
selection = args.project_root / '.codex-voice' / 'avatar-selection.json'
if args.command == 'use':
    selection.write_text(json.dumps({'schema': 'codex-ai-presence/avatar-selection/v0.1', 'avatar_id': args.avatar_id}), encoding='utf-8')
    print('selected')
else:
    target = args.project_root / '.codex-voice-avatars' / 'demo-avatar'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(args.source, target)
    selection.write_text('{"schema":"codex-ai-presence/avatar-selection/v0.1","avatar_id":"demo-avatar"}', encoding='utf-8')
    print('installed')
""".strip(),
            encoding="utf-8",
        )
        (voice / "avatar_state.py").write_text(
            """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest='command', required=True)
write = commands.add_parser('write')
write.add_argument('--project-root', type=Path, required=True)
write.add_argument('--avatar-id', required=True)
write.add_argument('--source', required=True)
write.add_argument('--scope', required=True)
write.add_argument('--revision', type=int, required=True)
write.add_argument('--actions-json', required=True)
args = parser.parse_args()
actions = json.loads(args.actions_json)
payload = {
    'schema': 'codex-ai-presence/avatar-state/v0.1',
    'type': 'avatar-state',
    'avatar_id': args.avatar_id,
    'source': args.source,
    'scope': args.scope,
    'revision': args.revision,
    'actions': actions,
    'issued_at': '2026-07-12T00:00:00.000Z',
}
(args.project_root / '.codex-voice' / 'avatar-state.json').write_text(json.dumps(payload), encoding='utf-8')
(args.project_root / '.codex-voice' / 'avatar-state-status.json').write_text(json.dumps({
    'schema': 'codex-ai-presence/avatar-state-status/v0.1',
    'type': 'avatar-state-status',
    'avatar_id': args.avatar_id,
    'accepted': True,
    'reason': 'accepted',
    'revision': args.revision,
    'action_count': len(actions),
}), encoding='utf-8')
print(json.dumps(payload))
""".strip(),
            encoding="utf-8",
        )

    def test_profile_materialize_and_publish_use_only_action_ids(self) -> None:
        archive = self._zip_source(self._write_model_source())
        import_model(archive, "demo-avatar", self.registry)
        profile = self.root / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "schema": "live2d-avatar/profile/v0.1",
                    "name": "Demo profile",
                    "state_semantics": "active-toggle-set",
                    "actions": [
                        {
                            "source_file": "dazed.exp3.json",
                            "id": "eyes.dazed",
                            "label": "Dazed eyes",
                        }
                    ],
                    "action_descriptions": {
                        "eyes.dazed": "Makes the character visibly unfocused and dazed."
                    },
                    "safe_default_actions": ["eyes.dazed"],
                    "initial_actions": ["eyes.dazed"],
                    "renderer": {
                        "scale": 1.2,
                        "bottom_inset": 12,
                        "halo": {"enabled": False},
                        "fixed_parameters": [{"parameter_id": "Param13", "value": 0}],
                        "fixed_parts": [{"part_id": "Part157", "opacity": 0}],
                        "activity_actions": {"thinking": {"add": ["eyes.dazed"]}},
                        "speech_motion": {
                            "targets": [
                                {
                                    "parameter_id": "BodySway",
                                    "idle_gain": 0.1,
                                    "speech_gain": 0.4,
                                    "frequency": 0.5,
                                    "phase": 0,
                                }
                            ],
                            "mouth": {
                                "primary_parameter_id": "MouthPrimary",
                                "secondary_parameter_id": "MouthSecondary",
                                "jaw_parameter_id": "Jaw",
                                "base_open": 0.02,
                                "mouth_gain": 0.44,
                                "secondary_gain": 0.5,
                                "jaw_gain": 0.76,
                                "attack": 0.3,
                                "release": 0.12,
                            },
                            "eyelids": {
                                "left_parameter_id": "EyeLeft",
                                "right_parameter_id": "EyeRight",
                                "idle_open_min": 0.34,
                                "idle_open_max": 0.5,
                                "idle_frequency": 0.2,
                                "speech_open": 0.98,
                                "wake_gain": 1.45,
                                "attack": 0.42,
                                "release": 0.08,
                                "talking_wake_floor": 0.68,
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        apply_profile(self.registry, "demo-avatar", profile)
        set_actions(self.registry, "demo-avatar", ["eyes.dazed"])
        project = self.root / "project"
        project.mkdir()
        self._write_mock_voice_tools(project)
        install_project(project, "demo-avatar", self.registry)

        materialized = materialize_bundle(project, "demo-avatar", self.registry)

        self.assertEqual(materialized["status"], "materialized")
        bundle = project / ".codex-voice-avatars" / "demo-avatar"
        avatar_manifest = json.loads((bundle / "avatar.json").read_text(encoding="utf-8"))
        self.assertIn("avatar-state-v1", avatar_manifest["capabilities"])
        capabilities = json.loads((bundle / "avatar-capabilities.json").read_text(encoding="utf-8"))
        self.assertEqual(capabilities["state_semantics"], "active-toggle-set")
        self.assertEqual(
            capabilities["renderer"]["speech_motion"]["targets"][0]["parameter_id"],
            "BodySway",
        )
        self.assertEqual(
            capabilities["renderer"]["speech_motion"]["eyelids"]["idle_open_min"],
            0.34,
        )
        self.assertEqual(
            capabilities["renderer"]["speech_motion"]["eyelids"]["talking_wake_floor"],
            0.68,
        )
        self.assertEqual(
            capabilities["renderer"]["speech_motion"]["mouth"]["jaw_gain"],
            0.76,
        )
        self.assertFalse(capabilities["renderer"]["halo"]["enabled"])
        self.assertEqual(
            capabilities["renderer"]["fixed_parameters"],
            [{"parameter_id": "Param13", "value": 0.0}],
        )
        self.assertEqual(
            capabilities["renderer"]["fixed_parts"],
            [{"part_id": "Part157", "opacity": 0.0}],
        )
        self.assertEqual(
            capabilities["renderer"]["activity_actions"]["thinking"],
            {"add": ["eyes.dazed"], "suppress": []},
        )
        self.assertEqual(
            capabilities["renderer"]["activity_actions"]["tool"],
            {"add": [], "suppress": []},
        )
        self.assertEqual(capabilities["initial_actions"], ["eyes.dazed"])
        self.assertEqual(
            capabilities["actions"][0]["description"],
            "Makes the character visibly unfocused and dazed.",
        )
        self.assertTrue((bundle / "renderer.js").is_file())
        project_manifest = (project / ".codex-live2d" / "RUNTIME-MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn(".codex-live2d/bundles/", project_manifest)
        self.assertIn(str(bundle), project_manifest)

        published = publish_state(project, self.registry)

        self.assertEqual(published["revision"], 1)
        self.assertEqual(published["actions"], ["eyes.dazed"])
        host_state = json.loads((project / ".codex-voice" / "avatar-state.json").read_text(encoding="utf-8"))
        self.assertEqual(host_state["actions"], ["eyes.dazed"])
        self.assertNotIn("parameter_operations", host_state)
        context = build_project_context(project)
        self.assertEqual(context["schema"], "live2d-avatar/context/v0.1")
        self.assertEqual(context["state_semantics"], "active-toggle-set")
        self.assertEqual(context["current"]["source"], "voice-host")
        self.assertEqual(context["current"]["actions"][0]["id"], "eyes.dazed")
        self.assertEqual(
            context["current"]["actions"][0]["description"],
            "Makes the character visibly unfocused and dazed.",
        )
        context_json = json.dumps(context)
        self.assertNotIn("Key2", context_json)
        self.assertNotIn("parameter_operations", context_json)
        self.assertNotIn("dazed.exp3.json", context_json)
        self.assertIn("eyes.dazed", render_context_markdown(context))

        removed = uninstall_project(project, confirm=True)
        self.assertEqual(removed["status"], "removed")
        self.assertFalse(bundle.exists())
        self.assertFalse((project / ".codex-live2d").exists())
        self.assertTrue((self.registry / "demo-avatar" / "manifest.json").exists())

    def test_profile_scaffold_uses_exact_sources_and_marks_semantics_as_draft(self) -> None:
        source = self._write_model_source()
        for folder, value in (("outfit-a", 1), ("outfit-b", -1)):
            expression = source / folder / "toggle.exp3.json"
            expression.parent.mkdir()
            expression.write_text(
                json.dumps({"Parameters": [{"Id": f"Control{value}", "Value": value, "Blend": "Add"}]}),
                encoding="utf-8",
            )
        import_model(self._zip_source(source), "demo-avatar", self.registry)
        draft = self.root / "user-owned-profile.json"

        scaffolded = scaffold_profile(self.registry, "demo-avatar", draft)

        self.assertEqual(scaffolded["status"], "scaffolded")
        document = json.loads(draft.read_text(encoding="utf-8"))
        self.assertEqual(document["semantic_status"], "draft")
        self.assertTrue(all("source" in action and "source_file" not in action for action in document["actions"]))
        self.assertEqual(len({action["source"] for action in document["actions"]}), 3)
        apply_profile(self.registry, "demo-avatar", draft)
        manifest = load_manifest(self.registry, "demo-avatar")
        self.assertEqual(manifest["profile"]["semantic_status"], "draft")

        project = self.root / "project"
        project.mkdir()
        install_project(project, "demo-avatar", self.registry)
        context = build_project_context(project)
        self.assertEqual(context["semantic_status"], "draft")
        self.assertIn("Semantic mapping: draft.", render_context_markdown(context))

    def test_generic_bind_and_doctor_use_voice_owned_selection(self) -> None:
        archive = self._zip_source(self._write_model_source())
        import_model(archive, "demo-avatar", self.registry)
        draft = self.root / "draft.json"
        scaffold_profile(self.registry, "demo-avatar", draft)
        profile = json.loads(draft.read_text(encoding="utf-8"))
        profile["semantic_status"] = "curated"
        draft.write_text(json.dumps(profile), encoding="utf-8")
        apply_profile(self.registry, "demo-avatar", draft)
        project = self.root / "project"
        project.mkdir()
        self._write_mock_voice_tools(project)

        bound = bind_project(project, "demo-avatar", self.registry)

        self.assertEqual(bound["status"], "bound")
        self.assertTrue(bound["restart_required"])
        self.assertTrue((project / ".codex-voice-avatars" / "demo-avatar" / "avatar-capabilities.json").is_file())
        self.assertEqual(bind_project(project, "demo-avatar", self.registry)["status"], "already-bound")
        selection_path = project / ".codex-voice" / "avatar-selection.json"
        selection_path.write_text(
            json.dumps({"schema": "codex-ai-presence/avatar-selection/v0.1", "avatar_id": "builtin"}),
            encoding="utf-8",
        )
        reselected = bind_project(project, "demo-avatar", self.registry)
        self.assertEqual(reselected["status"], "refreshed")
        self.assertTrue(reselected["restart_required"])
        self.assertEqual(json.loads(selection_path.read_text(encoding="utf-8"))["avatar_id"], "demo-avatar")
        before_publish = project_doctor(project)
        self.assertEqual(before_publish["selection"]["status"], "selected")
        self.assertEqual(before_publish["bundle"]["status"], "ready")
        self.assertEqual(before_publish["voice"]["renderer_state"]["status"], "missing")
        self.assertNotIn("parameter_operations", json.dumps(before_publish))

        publish_state(project, self.registry)
        ready = project_doctor(project)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["model"]["semantic_status"], "curated")

        import_model(archive, "other-avatar", self.registry)
        with self.assertRaises(AvatarRuntimeError):
            bind_project(project, "other-avatar", self.registry)

    def test_cli_parses_generic_setup_commands(self) -> None:
        import_model(self._zip_source(self._write_model_source()), "demo-avatar", self.registry)
        draft = self.root / "draft.json"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = main(
                [
                    "--registry",
                    str(self.registry),
                    "model",
                    "profile",
                    "scaffold",
                    "demo-avatar",
                    "--output",
                    str(draft),
                    "--json",
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "scaffolded")
        parser = build_parser()
        scaffold = parser.parse_args(
            ["model", "profile", "scaffold", "demo-avatar", "--output", str(draft)]
        )
        self.assertEqual(scaffold.model_id, "demo-avatar")
        exported = parser.parse_args(
            ["model", "profile", "export", "demo-avatar", "--output", str(draft)]
        )
        self.assertEqual(exported.model_id, "demo-avatar")
        doctor = parser.parse_args(["project", "doctor", "--project", str(self.root)])
        self.assertEqual(doctor.project, self.root)
        bind = parser.parse_args(
            [
                "project",
                "bind",
                "--project",
                str(self.root),
                "--model",
                "demo-avatar",
                "--profile",
                str(draft),
            ]
        )
        self.assertEqual(bind.model_id, "demo-avatar")
        self.assertEqual(bind.profile, draft)

    def test_context_hook_preserves_existing_stop_hook_on_uninstall(self) -> None:
        archive = self._zip_source(self._write_model_source())
        import_model(archive, "demo-avatar", self.registry)
        project = self.root / "project"
        project.mkdir()
        install_project(project, "demo-avatar", self.registry)
        hooks = project / ".codex"
        hooks.mkdir()
        (hooks / "hooks.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "existing-stop-hook"}
                                ]
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        enabled = enable_context_hook(project, runtime_python=Path(sys.executable))

        self.assertEqual(enabled["status"], "enabled")
        self.assertEqual(context_hook_status(project)["status"], "enabled")
        registered = json.loads((hooks / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("Stop", registered["hooks"])
        self.assertIn("UserPromptSubmit", registered["hooks"])
        self.assertTrue((project / ".codex-live2d" / "live2d_context_hook.py").is_file())
        lifecycle_manifest = (project / ".codex-live2d" / "RUNTIME-MANIFEST.md").read_text(encoding="utf-8")
        self.assertIn("live2d_context_hook.py", lifecycle_manifest)
        self.assertIn("UserPromptSubmit", lifecycle_manifest)

        removed = uninstall_project(project, confirm=True)

        self.assertEqual(removed["status"], "removed")
        preserved = json.loads((hooks / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("Stop", preserved["hooks"])
        self.assertNotIn("UserPromptSubmit", preserved["hooks"])
        self.assertFalse((project / ".codex-live2d").exists())

    def test_context_hook_marks_unreviewed_semantic_metadata(self) -> None:
        hook_path = REPOSITORY / "src" / "live2d_avatar" / "assets" / "live2d_context_hook.py"
        spec = importlib.util.spec_from_file_location("live2d_context_hook_test", hook_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        document = {
            "schema": "live2d-avatar/context/v0.1",
            "avatar": {"id": "demo-avatar", "name": "Demo avatar"},
            "semantic_status": "draft",
            "current": {"actions": []},
            "available_actions": [
                {
                    "id": "expression.demo",
                    "label": "Imported expression 1",
                    "description": "A model expression toggle whose visual effect has not been confirmed.",
                }
            ],
        }

        rendered = module._validated_markdown(document)

        self.assertIsNotNone(rendered)
        self.assertIn("Semantic mapping: draft.", rendered)
        self.assertIsNone(module._validated_markdown({**document, "semantic_status": "unknown"}))

    def test_renderer_template_applies_full_state_in_cubism_lifecycle(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for the renderer lifecycle regression")
        renderer = REPOSITORY / "src" / "live2d_avatar" / "assets" / "renderer-template" / "renderer.js"
        harness = textwrap.dedent(
            """
            const fs = require("fs");
            const vm = require("vm");
            const rendererPath = process.argv[1];
            const callbacks = {};
            const lifecycle = {};
            const values = new Map([["control.base", 0], ["control.overlay", 0]]);
            const calls = [];
            const setCalls = [];
            const positionCalls = [];
            const scaleCalls = [];
            const resizeCalls = [];
            let applicationOptions = null;
            let live2dOptions = null;
            let appStarted = false;
            let tickerCallback = null;
            let tickerPriority = null;
            let clockMs = 0;
            const cssProperties = new Map();
            const parameterIndices = new Map();
            const parameterIds = [];
            const indexLookups = [];
            function parameterIndex(id) {
              indexLookups.push(id);
              if (!parameterIndices.has(id)) {
                parameterIndices.set(id, parameterIds.length);
                parameterIds.push(id);
              }
              return parameterIndices.get(id);
            }
            function parameterId(index) { return parameterIds[index]; }
            const core = {
              getParameterIndex(id) { return parameterIndex(id); },
              addParameterValueByIndex(index, value) { const id = parameterId(index); calls.push(id); values.set(id, (values.get(id) || 0) + value); },
              setParameterValueByIndex(index, value) { const id = parameterId(index); setCalls.push([id, value]); values.set(id, value); },
              multiplyParameterValueByIndex(index, value) { const id = parameterId(index); values.set(id, (values.get(id) || 0) * value); },
            };
            const model = {
              internalModel: { coreModel: core, on(name, callback) { lifecycle[name] = callback; } },
              scale: { set(...args) { scaleCalls.push(args); } },
              position: { set(...args) { positionCalls.push(args); } },
              getLocalBounds() { return { x: 0, y: 0, width: 100, height: 100 }; },
              update() {},
              rotation: 0,
            };
            const app = {
              stage: { addChild() {} },
              ticker: { calls: 0, elapsedMS: 1000 / 60, add(callback, _context, priority) { this.calls += 1; tickerCallback = callback; tickerPriority = priority; } },
              renderer: { resize(width, height) { resizeCalls.push([width, height]); } },
              start() { appStarted = true; },
            };
            const noOp = () => {};
            global.console = { info: noOp, error: noOp };
            global.performance = { now() { return clockMs; } };
            global.window = global;
            window.innerWidth = 440;
            window.innerHeight = 440;
            window.devicePixelRatio = 2.5;
            window.__LIVE2D_AVATAR_CAPABILITIES__ = {
              avatar_id: "demo-avatar",
              state_semantics: "active-toggle-set",
              model: { path: "demo.model3.json" },
              actions: [
                { id: "toggle.base", parameter_operations: [{ parameter_id: "control.base", value: 1, blend: "Add" }] },
                { id: "toggle.overlay", parameter_operations: [{ parameter_id: "control.overlay", value: 1, blend: "Add" }] },
              ],
              safe_default_operations: [],
              initial_actions: [],
              renderer: {
                halo: { enabled: false },
                activity_actions: {
                  thinking: { add: ["toggle.overlay"] },
                  cli: { suppress: ["toggle.base"] },
                },
                speech_motion: {
                  targets: [
                    { parameter_id: "rig.chest", idle_gain: 0, speech_gain: 0.5, frequency: 0.4, phase: 0 },
                    { parameter_id: "rig.hip", idle_gain: 0, speech_gain: -0.25, frequency: 0.4, phase: 0 },
                    { parameter_id: "rig.shoulder", idle_gain: 0, speech_gain: 0.15, frequency: 0.8, phase: 1 },
                  ],
                  mouth: { primary_parameter_id: "rig.mouth.primary", secondary_parameter_id: "rig.mouth.secondary", jaw_parameter_id: "rig.jaw", base_open: 0.02, mouth_gain: 0.44, secondary_gain: 0.5, jaw_gain: 0.76, attack: 0.3, release: 0.12 },
                  eyelids: { left_parameter_id: "rig.eye.left", right_parameter_id: "rig.eye.right", idle_open_min: 0.34, idle_open_max: 0.5, idle_frequency: 0.2, speech_open: 0.98, wake_gain: 1.45, attack: 0.42, release: 0.08, talking_wake_floor: 0.68 },
                },
              },
            };
            window.PIXI = {
              Application: class { constructor(options) { applicationOptions = options; return app; } },
              UPDATE_PRIORITY: { HIGH: 25 },
              live2d: { CubismConfig: {}, Live2DModel: { from: async (_path, options) => { live2dOptions = options; return model; } } },
            };
            window.orbApi = {
              onAudioEvent(callback) { callbacks.audio = callback; },
              onProfileCuration(callback) { callbacks.curation = callback; },
              onAvatarState(callback) { callbacks.avatar = callback; },
              onMoveMode(callback) { callbacks.move = callback; },
              onWindowResize(callback) { callbacks.resize = callback; },
              dragEnd: noOp,
              setMoveMode: noOp,
              drag: noOp,
              dragStart: noOp,
            };
            window.addEventListener = noOp;
            global.document = {
              getElementById() {
                return { addEventListener: noOp, hasPointerCapture: () => false, setPointerCapture: noOp, releasePointerCapture: noOp };
              },
              documentElement: { style: { setProperty(name, value) { cssProperties.set(name, value); } } },
              body: { classList: { add: noOp, remove: noOp, toggle: noOp } },
            };
            vm.runInThisContext(fs.readFileSync(rendererPath, "utf8"), { filename: rendererPath });
            setImmediate(() => setImmediate(() => {
              if (app.ticker.calls !== 1 || typeof tickerCallback !== "function" || tickerPriority !== 25 || !appStarted) {
                throw new Error("renderer did not consolidate model updates onto the application ticker");
              }
              if (!live2dOptions || live2dOptions.autoUpdate !== false || live2dOptions.autoInteract !== false) {
                throw new Error("renderer retained the shared Live2D ticker or interaction path");
              }
              if (typeof lifecycle.beforeModelUpdate !== "function") throw new Error("renderer did not register the Cubism lifecycle hook");
              if (!applicationOptions || applicationOptions.width !== 440 || applicationOptions.height !== 440
                || applicationOptions.autoDensity !== true || applicationOptions.resolution !== 2
                || applicationOptions.antialias !== true || applicationOptions.autoStart !== false
                || applicationOptions.powerPreference !== "high-performance") {
                throw new Error("renderer did not initialize its optimized density-aware canvas");
              }
              const startupLookupCount = indexLookups.length;
              if (cssProperties.get("--halo-display") !== "none") throw new Error("renderer did not honor the halo enabled setting");
              if (typeof callbacks.resize !== "function") throw new Error("renderer did not subscribe to the window-resize bridge");
              const initialScale = scaleCalls.at(-1)?.[0];
              callbacks.resize({ width: 880, height: 660 });
              if (resizeCalls.length !== 1 || resizeCalls[0][0] !== 880 || resizeCalls[0][1] !== 660) {
                throw new Error("renderer did not resize its backing surface to the window dimensions");
              }
              const resizedScale = scaleCalls.at(-1)?.[0];
              if (!(resizedScale > initialScale)) throw new Error("renderer did not re-fit the model after resize");
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 1, actions: ["toggle.overlay", "toggle.base"] });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 1 || values.get("control.overlay") !== 1) throw new Error("initial state was not composed");
              if (calls.filter((id) => id.startsWith("control.")).join(",") !== "control.base,control.overlay") throw new Error("renderer did not use model-local replay order");
              const idleEye = setCalls.find(([id]) => id === "rig.eye.left");
              if (!idleEye || !(idleEye[1] >= 0.34 && idleEye[1] <= 0.5)) throw new Error("idle eyelid sway escaped its configured range");
              values.set("control.base", 0); values.set("control.overlay", 0); calls.length = 0;
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 2, actions: [] });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 0 || values.get("control.overlay") !== 0) throw new Error("reset retained an old action");
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 3, actions: ["toggle.overlay"] });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 0 || values.get("control.overlay") !== 1) throw new Error("replacement state retained an old action");
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 4, actions: [] });
              calls.length = 0; setCalls.length = 0; positionCalls.length = 0; model.rotation = 0;
              values.set("control.base", 0); values.set("control.overlay", 0);
              callbacks.audio({ type: "activity", state: "thinking", ttl_ms: 500 });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 0 || values.get("control.overlay") !== 1) {
                throw new Error("activity overlay did not apply its local action set");
              }
              values.set("control.base", 0); values.set("control.overlay", 0); calls.length = 0;
              clockMs = 501;
              lifecycle.beforeModelUpdate();
              if (calls.includes("control.overlay")) throw new Error("activity overlay did not clear after its ttl elapsed");
              callbacks.audio({ type: "activity", state: "idle" });
              lifecycle.beforeModelUpdate();
              if (calls.includes("control.overlay")) throw new Error("idle activity retained a prior overlay action");
              calls.length = 0;
              callbacks.audio({ type: "activity", state: "future-state" });
              lifecycle.beforeModelUpdate();
              if (calls.includes("control.overlay")) throw new Error("unknown activity applied a configured overlay");
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 5, actions: ["toggle.base"] });
              values.set("control.base", 0); values.set("control.overlay", 0); calls.length = 0;
              callbacks.audio({ type: "activity", state: "cli" });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 0) throw new Error("CLI activity did not suppress the configured controller action");
              values.set("control.base", 0); calls.length = 0;
              callbacks.audio({ type: "activity", state: "idle" });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 1) throw new Error("controller action did not return after activity suppression cleared");
              callbacks.curation({
                schema: "codex-ai-presence/profile-curation/v0.1",
                profile_id: "sarah",
                route_key: "session:sarah|profile:sarah",
                initial_actions: ["toggle.overlay"],
                activity_actions: { cli: { suppress: [] } },
              });
              values.set("control.base", 0); values.set("control.overlay", 0); calls.length = 0;
              callbacks.audio({ type: "activity", state: "cli" });
              lifecycle.beforeModelUpdate();
              if (values.get("control.base") !== 1 || values.get("control.overlay") !== 0) {
                throw new Error("child curation did not clear inherited suppression without replacing routed state");
              }
              callbacks.avatar({ avatar_id: "demo-avatar", revision: 6, actions: [] });
              calls.length = 0; setCalls.length = 0; positionCalls.length = 0; model.rotation = 0;
              callbacks.audio({ type: "state", state: "speaking" });
              lifecycle.beforeModelUpdate();
              const heldEye = setCalls.filter(([id]) => id === "rig.eye.left").at(-1);
              if (!heldEye || !(heldEye[1] > 0.75 && heldEye[1] <= 0.98)) throw new Error("speaking did not hold the eyelids open between syllables");
              setCalls.length = 0;
              callbacks.audio({ type: "audio", amplitude: 1, bands: [1, 0.8, 0.6] });
              for (let frame = 0; frame < 12; frame += 1) lifecycle.beforeModelUpdate();
              for (const target of ["rig.chest", "rig.hip", "rig.shoulder"]) {
                if (!calls.includes(target)) throw new Error("speech motion did not reach a rig target");
              }
              for (const eye of ["rig.eye.left", "rig.eye.right"]) {
                const eyeCalls = setCalls.filter(([id]) => id === eye);
                const call = eyeCalls[eyeCalls.length - 1];
                if (!call || !(call[1] > 0.9 && call[1] <= 0.98)) throw new Error("volume did not produce a strong eyelid wake-up response");
              }
              const mouth = setCalls.filter(([id]) => id === "rig.mouth.primary").at(-1);
              const jaw = setCalls.filter(([id]) => id === "rig.jaw").at(-1);
              if (!mouth || !(mouth[1] > 0.1 && mouth[1] <= 0.46)) throw new Error("speech mouth was not softly driven");
              if (!jaw || !(jaw[1] > mouth[1] && jaw[1] <= 0.76)) throw new Error("speech cadence did not reach the jaw control");
              if (positionCalls.length !== 0 || model.rotation !== 0) throw new Error("speech motion still shakes the full model node");
              if (indexLookups.length !== startupLookupCount) throw new Error("renderer repeated Cubism parameter-id scans after startup");
              process.stdout.write(JSON.stringify({ lifecycle: true, responsiveResize: true, haloConfig: true, activityOverlay: true, activityExpiry: true, activitySuppress: true, childCuration: true, reset: true, replacement: true, replayOrder: true, rigMotion: true, eyelids: true, jaw: true }));
            }));
            """
        )
        result = subprocess.run(
            [node, "-e", harness, str(renderer)],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"lifecycle": True, "responsiveResize": True, "haloConfig": True, "activityOverlay": True, "activityExpiry": True, "activitySuppress": True, "childCuration": True, "reset": True, "replacement": True, "replayOrder": True, "rigMotion": True, "eyelids": True, "jaw": True},
        )

    def test_local_cubism_fork_keeps_the_per_frame_physics_loop_allocation_free(self) -> None:
        vendor = (
            REPOSITORY
            / "src"
            / "live2d_avatar"
            / "assets"
            / "renderer-template"
            / "vendor"
            / "cubism4-local.js"
        ).read_text(encoding="utf-8")
        evaluate = vendor[vendor.index("    evaluate(model, deltaTimeSeconds) {") : vendor.index("    setOptions(options) {")]
        particles = vendor[vendor.index("  function updateParticles(") : vendor.index("  function updateOutputParameterValue(")]

        self.assertNotIn(".slice(", evaluate)
        self.assertNotIn("new CubismVector2", evaluate)
        self.assertNotIn("new CubismVector2", particles)
        self.assertNotIn("parameterValue.slice", evaluate)
        self.assertIn("updateOutputParameterValue(\n          parameterValue,", evaluate)
        self.assertIn("const MaximumPhysicsFps = 30", vendor)
        self.assertIn("this._applyOutputs(model, this._physicsAccumulator / interval)", evaluate)
        self.assertIn("output.previousValue", evaluate)
        self.assertIn("this._parameterIndices = /* @__PURE__ */ new Map()", vendor)
        self.assertIn("this._savedParameters.set(this._parameterValues.subarray", vendor)
        self.assertNotIn("motionData.points.slice(segment.basePointIndex)", vendor)
        self.assertNotIn("const result = new CubismMotionPoint()", vendor)
        self.assertIn("this._drawColor = new CubismTextureColor()", vendor)
        self.assertIn("this.renderer.setRenderState(null, this.viewport)", vendor)

        translate = vendor[vendor.index("    translateRelative(x, y) {") : vendor.index("    translate(x, y) {")]
        scale = vendor[vendor.index("    scaleRelative(x, y) {") : vendor.index("    scale(x, y) {")]
        self.assertNotIn("new Float32Array", translate)
        self.assertNotIn("new Float32Array", scale)


if __name__ == "__main__":
    unittest.main()
