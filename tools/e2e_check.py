"""Run the no-network Presence Runtime v0.2 source/projection E2E gate."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from canonical_projection import verify_projection


REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "RUNTIME-MANIFEST.md",
    "agents/openai.yaml",
    "scripts/presence.py",
    "scripts/presence_compat.py",
    "scripts/rollout_adapter.py",
    "scripts/runtime_adapter.py",
    "scripts/setup.py",
    "scripts/toggle.py",
    "scripts/profiles.py",
    "scripts/avatar.py",
    "scripts/avatar_state.py",
    "scripts/live2d-avatar.py",
    "scripts/uninstall.py",
    "scripts/tui_bridge.py",
    "scripts/launch_codex.py",
    "scripts/launch_codex.sh",
)
REQUIRED_SCHEMAS = (
    "avatar-model-pack-v0.2.schema.json",
    "preset-v0.2.schema.json",
    "presence-profile-v0.2.schema.json",
    "override-patch-v0.2.schema.json",
    "effective-snapshot-v0.2.schema.json",
    "registration-v0.2.schema.json",
    "renderer-snapshot-v0.2.schema.json",
)
FORBIDDEN_ASSET_SUFFIXES = {
    ".zip",
    ".vtube",
    ".moc3",
    ".model3.json",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".onnx",
    ".bin",
    ".wav",
}


def find_layout(source: Path) -> tuple[Path, Path, Path, bool]:
    source_skill = source / "skills" / "codex-voice"
    if source_skill.is_dir() and (source / "presence-runtime").is_dir():
        skill = source_skill
        presence = source / "presence-runtime"
        live2d = source / "live2d-avatar-runtime"
        if not presence.is_dir() or not live2d.is_dir():
            raise SystemExit("Source checkout is missing a canonical runtime package")
        for duplicate in (skill / "presence-runtime", skill / "live2d-avatar-runtime"):
            if duplicate.exists():
                raise SystemExit(f"Source skill contains a duplicate tracked runtime: {duplicate}")
        return skill, presence, live2d, True
    if source_skill.is_dir():
        skill = source_skill
    elif (source / "SKILL.md").is_file():
        skill = source
    elif (source / "codex-voice" / "SKILL.md").is_file():
        skill = source / "codex-voice"
    else:
        raise SystemExit(f"Could not find a codex-voice skill under {source}")
    presence = skill / "presence-runtime"
    live2d = skill / "live2d-avatar-runtime"
    verify_projection(presence, package="presence-runtime")
    verify_projection(live2d, package="live2d-avatar-runtime")
    return skill, presence, live2d, False


def require(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"Required release file is missing: {path}")


def validate_static_contracts(
    skill: Path,
    presence: Path,
    live2d: Path,
    *,
    source_checkout: bool,
) -> None:
    for relative in REQUIRED_SKILL_FILES:
        require(skill / relative)
    for path in skill.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for path in presence.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for name in REQUIRED_SCHEMAS:
        schema = presence / "schemas" / name
        require(schema)
        document = json.loads(schema.read_text(encoding="utf-8"))
        if document.get("type") != "object" and not isinstance(document.get("oneOf"), list):
            raise SystemExit(f"Presence schema is not an object contract: {schema}")

    manifest = json.loads(
        (presence / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("schema") != "presence/runtime-manifest/v0.2":
        raise SystemExit("Presence runtime manifest schema is invalid")
    components = {item.get("id"): item for item in manifest.get("components", [])}
    expected = {
        "supervisor",
        "state-store",
        "catalog",
        "adapter-registration",
        "kokoro-worker",
        "renderer-host",
        "public-cli",
        "migration-ledger",
    }
    if not expected.issubset(components):
        raise SystemExit(f"Runtime manifest omitted components: {sorted(expected - components.keys())}")
    for identifier, component in components.items():
        for field in (
            "scope",
            "owner",
            "artifacts",
            "dependencies",
            "dependents",
            "preserved_data",
            "removal",
        ):
            if field not in component:
                raise SystemExit(f"Runtime component {identifier!r} omitted {field}")

    skill_text = (skill / "SKILL.md").read_text(encoding="utf-8")
    for required_text in (
        "name: codex-voice",
        "presence runtime install",
        "presence project register",
        "presence inspect effective",
        "presence runtime uninstall",
        "one warm Kokoro worker",
    ):
        if required_text not in skill_text:
            raise SystemExit(f"Skill omitted v0.2 instruction: {required_text}")
    for forbidden_text in (
        "Use the bundled scripts from the active project directory",
        "copying a model bundle into every project",
        "uninstall the current model before",
    ):
        if forbidden_text in skill_text:
            raise SystemExit(f"Skill retained stale v0.1 guidance: {forbidden_text}")

    setup = (skill / "scripts" / "setup.py").read_text(encoding="utf-8")
    if "return v2_setup(args)" not in setup or "presence project register" not in setup:
        raise SystemExit("Legacy setup does not terminate at the v0.2 compatibility seam")
    tui = (skill / "scripts" / "tui_bridge.py").read_text(encoding="utf-8")
    if "RuntimePlaybackAdapter" not in tui or 'None if self.runtime is not None else Inbox' not in tui:
        raise SystemExit("TUI bridge can still create a project inbox on the v0.2 path")
    rollout = (skill / "scripts" / "rollout_adapter.py").read_text(encoding="utf-8")
    if "rollout-cursors.json" not in rollout or "inbox.sqlite3" in rollout:
        raise SystemExit("Managed rollout adapter owns state beyond cursors/diagnostics")

    renderer = (presence / "renderer-host" / "main.cjs").read_text(encoding="utf-8")
    for token in ("snapshot.binding_id", "event.utterance_id", "renderer/ready"):
        if token not in renderer:
            raise SystemExit(f"Central renderer omitted binding contract token: {token}")
    live2d_renderer = (
        live2d
        / "src"
        / "live2d_avatar"
        / "assets"
        / "renderer-template"
        / "renderer.js"
    ).read_text(encoding="utf-8")
    if "getParameterDefaultValue" not in live2d_renderer or "semanticParameterDefaults" not in live2d_renderer:
        raise SystemExit("Canonical Live2D renderer does not reset removed controls to model defaults")

    if not source_checkout:
        forbidden = [
            path.relative_to(skill).as_posix()
            for path in skill.rglob("*")
            if path.is_file()
            and any(path.name.lower().endswith(suffix) for suffix in FORBIDDEN_ASSET_SUFFIXES)
        ]
        if forbidden:
            raise SystemExit(f"User/model assets entered the release: {forbidden}")
        if any(path.is_dir() and path.name == "profiles" for path in skill.rglob("profiles")):
            raise SystemExit("A user profile directory entered the projected release")


class FakePlayback:
    def __init__(self) -> None:
        self.activities: list[tuple[str, str | None]] = []
        self.speech: list[dict[str, Any]] = []

    def publish_activity(self, state: str, *, session_id: str | None, event_id: str) -> bool:
        del event_id
        self.activities.append((state, session_id))
        return True

    def publish_update(self, message: dict[str, Any]) -> bool:
        self.speech.append(dict(message))
        return True

    def enqueue(self, message: dict[str, Any]) -> bool:
        self.speech.append(dict(message))
        return True

    def start(self) -> None:
        return

    def close(self) -> None:
        return


def validate_runtime_flow(skill: Path, presence: Path, live2d: Path) -> None:
    for value in (
        str(presence / "src"),
        str(live2d / "src"),
        str(skill / "scripts"),
    ):
        if value not in sys.path:
            sys.path.insert(0, value)

    from presence_runtime.catalog import Catalog
    from presence_runtime.controller import RecordingRenderer, RuntimeController
    from presence_runtime.store import PresenceStore
    from presence_runtime.worker import RecordingWorker
    from rollout_adapter import RolloutAdapter

    with tempfile.TemporaryDirectory(prefix="presence-v02-e2e-") as temporary:
        root = Path(temporary)
        old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(root / "codex-home")
        try:
            store = PresenceStore(root / "state.sqlite3")
            catalog = Catalog(root / "catalog")
            voice = RecordingWorker()
            renderer = RecordingRenderer()
            controller = RuntimeController(
                store=store,
                catalog=catalog,
                voice=voice,
                renderer=renderer,
            )
            session_id = str(uuid.uuid4())
            project_a = root / "project-a"
            project_b = root / "project-b"
            project_a.mkdir()
            project_b.mkdir()
            source_a = store.register_source(
                adapter="e2e-a",
                project_root=project_a,
                session_id=session_id,
                capabilities=["speech", "activity"],
            )
            source_b = store.register_source(
                adapter="e2e-b",
                project_root=project_b,
                session_id=session_id,
                capabilities=["speech", "activity"],
            )
            if source_a["binding_id"] == source_b["binding_id"]:
                raise SystemExit("Two projects with the same session id shared a binding")
            controller.ensure_effective(source_a["binding_id"])
            controller.ensure_effective(source_b["binding_id"])
            controller.set_session_override(
                source_a["binding_id"],
                {"voice_id": "af_heart", "volume": 61},
            )
            if store.effective_snapshot(source_b["binding_id"]).tts.voice_id == "af_heart":
                raise SystemExit("A session override leaked into a sibling project")
            controller.enqueue_speech(
                source_id=source_a["source_id"],
                binding_id=source_a["binding_id"],
                event_id="e2e-final",
                utterance_id=str(uuid.uuid4()),
                text="Presence Runtime v0.2 E2E",
                kind="final",
            )
            played = controller.play_next()
            if played is None or voice.items[-1]["binding_id"] != source_a["binding_id"]:
                raise SystemExit("Queued speech did not retain its stable binding")

            codex_home = Path(os.environ["CODEX_HOME"])
            rollout = codex_home / "sessions" / f"rollout-e2e-{session_id}.jsonl"
            rollout.parent.mkdir(parents=True)
            records = [
                {
                    "timestamp": "2026-07-16T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": str(project_a), "id": session_id},
                },
                {
                    "timestamp": "2026-07-16T10:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "Adapter final",
                    },
                },
            ]
            rollout.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            playback = FakePlayback()
            adapter = RolloutAdapter(
                project_a,
                project_a / ".codex-voice" / "v0.2",
                start_time=0,
                playback=playback,  # type: ignore[arg-type]
            )
            adapter.scan(rollout)
            adapter.scan(rollout)
            finals = [item for item in playback.speech if item.get("kind") == "final"]
            if len(finals) != 1 or finals[0].get("session_id") != session_id:
                raise SystemExit("Rollout cursor did not emit exactly one binding-scoped final")
            if not (project_a / ".codex-voice" / "v0.2" / "rollout-cursors.json").is_file():
                raise SystemExit("Rollout adapter did not persist its project-local cursor")
            store.close()
        finally:
            if old_codex_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_codex_home


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    skill, presence, live2d, source_checkout = find_layout(source)
    validate_static_contracts(
        skill,
        presence,
        live2d,
        source_checkout=source_checkout,
    )
    validate_runtime_flow(skill, presence, live2d)
    print(
        "Passed Presence Runtime v0.2 E2E: canonical projection, manifest, "
        "two-project bindings, queue routing, and cursor-only adapter."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
