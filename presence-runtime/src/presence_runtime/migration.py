"""Guarded, idempotent migration from project-local v0.1 state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .catalog import Catalog
from .controller import RuntimeController
from .errors import ConflictError, NotFoundError, ValidationError
from .paths import codex_home, normalize_project_root, presence_home
from .validation import validate_model_pack, validate_patch


LEGACY_PROFILE_SCHEMA = "codex-ai-presence/profiles/v0.1"
LEGACY_FILES = (
    "voice",
    "speed",
    "mode",
    "provider",
    "volume",
    "commentary-volume",
    "progress",
    "orb.enabled",
    "input.json",
    "presence-profiles.json",
    "sessions.json",
    "avatar-selection.json",
    "avatar-states.json",
    "orb-position.json",
    "inbox.sqlite3",
)
SESSION_ROUTE = re.compile(r"session:([0-9a-f-]{36})(?:\||$)", re.IGNORECASE)


HIGAN_SLOTS: dict[str, tuple[str, ...]] = {
    "pose.qipao-pipe": ("wardrobe.base", "body.pose", "gesture.arms", "prop.hand"),
    "pose.sweater-heart": ("wardrobe.base", "body.pose", "gesture.arms"),
    "pose.sweater-default": ("wardrobe.base", "body.pose"),
    "style.hairstyle": ("style.hair",),
    "eyes.dazed": ("expression.eyes",),
    "accessory.fur-shawl": ("accessory.shoulders",),
    "eyes.heart": ("expression.eyes",),
    "effect.tears": ("effect.tears",),
    "effect.dark-face": ("effect.face",),
    "accessory.black-stockings": ("body.legs",),
    "accessory.dragon-horns": ("accessory.head",),
    "motion.dynamic-1": ("motion.dynamic",),
    "motion.dynamic-2": ("motion.dynamic",),
    "mouth.unhappy": ("expression.mouth",),
    "motion.lean": ("motion.body",),
    "motion.horizontal": ("motion.body",),
    "safety.clean-state": ("safety.baseline",),
    "effect.drool": ("effect.drool",),
    "pose.right-hand-pipe": ("gesture.arms", "prop.hand"),
    "mouth.tongue": ("expression.mouth",),
    "pose.qipao-hand-mouth": ("wardrobe.base", "body.pose", "gesture.arms"),
    "pose.sweater-hand-mouth-left": ("wardrobe.base", "body.pose", "gesture.arms"),
    "pose.sweater-hand-mouth-right": ("wardrobe.base", "body.pose", "gesture.arms"),
    "effect.blush": ("effect.face",),
}
NONEXCLUSIVE_HIGAN_SLOTS = {"effect.tears", "effect.drool", "safety.baseline"}


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ValidationError(f"Required legacy file is missing: {path}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Legacy JSON is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"Legacy JSON must be an object: {path}")
    return value


def _read_text(path: Path, default: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or default
    except OSError:
        return default


def _marker(path: Path) -> bool:
    return _read_text(path, "off").lower() in {"1", "on", "true", "enabled"}


def _canonical_session(value: object) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValidationError(f"Legacy session id is not a UUID: {value!r}") from exc
    canonical = str(parsed)
    if str(value).lower() != canonical:
        raise ValidationError(f"Legacy session id is not canonical: {value!r}")
    return canonical


def _tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _source_hash(project_root: Path) -> tuple[str, list[str]]:
    digest = hashlib.sha256()
    paths: list[str] = []
    voice_root = project_root / ".codex-voice"
    candidates = [voice_root / name for name in LEGACY_FILES]
    # SQLite WAL frames can contain pending speech newer than the main inbox
    # file.  Include both sidecars in the migration identity so an idempotent
    # ledger can never silently ignore a changed durable queue.
    candidates.extend(
        [voice_root / "inbox.sqlite3-wal", voice_root / "inbox.sqlite3-shm"]
    )
    candidates.extend(
        [
            project_root / ".codex-live2d" / "installation.json",
            project_root / ".codex-live2d" / "avatar-state-revisions.json",
        ]
    )
    for path in sorted(candidates, key=lambda item: str(item).lower()):
        if not path.is_file():
            continue
        relative = path.relative_to(project_root).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        paths.append(relative)
    return digest.hexdigest(), paths


class LegacyRuntimeCoordinator:
    """Stop current playback without modifying rollback source documents."""

    def pause_and_drain(self, project_root: Path, *, timeout: float = 10.0) -> dict[str, Any]:
        voice_root = project_root / ".codex-voice"
        player_file = voice_root / "tts-player.pid"
        (voice_root / "tts-stop.request").write_text("stop\n", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while player_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"playback_drained": not player_file.is_file()}

    def restart(self, _project_root: Path) -> None:
        # The v0.1 watcher is intentionally not killed by migration.  On a
        # failed handover it therefore remains the rollback owner immediately.
        return


class LegacyMigrator:
    def __init__(
        self,
        controller: RuntimeController,
        *,
        coordinator: LegacyRuntimeCoordinator | None = None,
        registry_root: Path | None = None,
    ) -> None:
        self.controller = controller
        self.store = controller.store
        self.catalog = controller.catalog
        self.coordinator = coordinator or LegacyRuntimeCoordinator()
        self.registry_root = (
            registry_root or codex_home() / "live2d-models"
        ).expanduser().resolve()
        self._thread_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    @contextmanager
    def _lock(self, project_id: str) -> Iterator[None]:
        with self._locks_guard:
            thread_lock = self._thread_locks.setdefault(project_id, threading.Lock())
        if not thread_lock.acquire(blocking=False):
            raise ConflictError(f"Migration is already running for {project_id}")
        directory = presence_home() / "migrations"
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / f"{project_id}.lock"
        descriptor: int | None = None
        try:
            for attempt in range(2):
                try:
                    descriptor = os.open(
                        marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                    break
                except FileExistsError as exc:
                    if attempt or not self._remove_stale_lock(marker):
                        raise ConflictError(
                            f"Migration lock already exists: {marker}"
                        ) from exc
            if descriptor is None:
                raise ConflictError(f"Could not acquire migration lock: {marker}")
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
                marker.unlink(missing_ok=True)
            thread_lock.release()

    @staticmethod
    def _remove_stale_lock(marker: Path) -> bool:
        try:
            owner = int(marker.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return False
        # Import locally to keep migration's pure document helpers independent
        # of service lifecycle initialization.
        from .lifecycle import _pid_running

        if _pid_running(owner):
            return False
        try:
            marker.unlink()
        except OSError:
            return False
        return True

    def status(self, project_id: str) -> dict[str, Any]:
        return self.store.migration_record(project_id) or {
            "project_instance_id": project_id,
            "status": "not-started",
        }

    def migrate_on_registration(
        self,
        project_id: str,
        project_root: Path,
    ) -> dict[str, Any]:
        existing = self.store.migration_record(project_id)
        if existing and existing["status"] == "committed":
            return {**existing, "idempotent": True}
        if existing and existing["status"] == "pending":
            self._recover_pending(project_id, existing)
            raise ConflictError(
                "Recovered an interrupted migration; run `presence migrate retry` explicitly"
            )
        if existing and existing["status"] in {"failed", "rolled_back"}:
            raise ConflictError(
                "Migration is not committed; inspect it and run `presence migrate retry` explicitly"
            )
        return self._migrate(project_id, project_root)

    def retry(self, project_id: str, project_root: Path) -> dict[str, Any]:
        existing = self.store.migration_record(project_id)
        if existing and existing["status"] == "committed":
            raise ConflictError("Migration is already committed; roll it back before retrying")
        if existing and existing["status"] == "pending":
            self._recover_pending(project_id, existing)
        elif existing and existing["status"] in {"failed", "rolled_back"}:
            checkpoint = existing.get("details", {}).get("checkpoint")
            if isinstance(checkpoint, dict):
                # Idempotently restore once more so retry can repair state
                # left by an older or interrupted rollback implementation.
                self.store.rollback_legacy_configuration(
                    project_id=project_id, checkpoint=checkpoint
                )
        return self._migrate(project_id, project_root)

    def _recover_pending(self, project_id: str, record: Mapping[str, Any]) -> None:
        checkpoint = record.get("details", {}).get("checkpoint")
        if isinstance(checkpoint, dict):
            self.store.rollback_legacy_configuration(
                project_id=project_id, checkpoint=checkpoint
            )
        self.store.set_migration_record(
            project_id=project_id,
            source_hash=str(record["source_hash"]),
            status="failed",
            details={
                **record.get("details", {}),
                "diagnostic": "recovered pending migration",
            },
        )

    def rollback(self, project_id: str) -> dict[str, Any]:
        record = self.store.migration_record(project_id)
        if record is None or record["status"] not in {"committed", "failed"}:
            raise ConflictError("No committed or failed migration is available to roll back")
        checkpoint = record.get("details", {}).get("checkpoint")
        if not isinstance(checkpoint, dict):
            raise ConflictError("Migration record has no rollback checkpoint")
        project = self.store.project(project_id)
        with self._lock(project_id):
            try:
                self.store.rollback_legacy_configuration(
                    project_id=project_id, checkpoint=checkpoint
                )
                # Re-resolve from the restored sparse documents without
                # creating an empty project default that did not exist before.
                self.controller.reconcile_project(project_id)
                self.controller.sync_binding_visibility()
                created = record.get("details", {}).get("catalog_created", {})
                if isinstance(created, dict):
                    self._cleanup_catalog(created)
                self.store.set_migration_record(
                    project_id=project_id,
                    source_hash=record["source_hash"],
                    status="rolled_back",
                    details={**record["details"], "rolled_back": True},
                )
            finally:
                self.coordinator.restart(Path(project["project_root"]))
        return self.store.migration_record(project_id)

    def _migrate(self, project_id: str, project_root: Path) -> dict[str, Any]:
        root = project_root.expanduser().resolve()
        with self._lock(project_id):
            source_hash, source_paths = _source_hash(root)
            if not source_paths:
                details = {"legacy_detected": False, "source_paths": []}
                self.store.set_migration_record(
                    project_id=project_id,
                    source_hash=source_hash,
                    status="committed",
                    details=details,
                )
                return self.store.migration_record(project_id)
            drain = self.coordinator.pause_and_drain(root)
            created = {"avatars": [], "profiles": []}
            imported: dict[str, Any] | None = None
            try:
                self._inspect_legacy_documents(root)
                plan = self._build_plan(project_id, root, created)
                details = {
                    "legacy_detected": True,
                    "source_paths": source_paths,
                    "profile_map": plan["profile_map"],
                    "retired_commentary": plan["retired_commentary"],
                    "catalog_created": created,
                    **drain,
                }
                imported = self.store.import_legacy_configuration(
                    project_id=project_id,
                    source_hash=source_hash,
                    project_patch=plan["project_patch"],
                    session_patches=plan["session_patches"],
                    geometry=plan["geometry"],
                    speech=plan["speech"],
                    details=details,
                )
                # Re-resolve every imported sparse child from one project
                # transaction and require worker plus renderer acknowledgement.
                self.controller.set_project_default(project_id, plan["project_patch"])
                health = self.controller.doctor()
                if not health.get("worker", {}).get("ready"):
                    raise ConflictError("v0.2 worker did not acknowledge migration health")
                if not health.get("renderer", {}).get("ready"):
                    raise ConflictError("v0.2 renderer did not acknowledge migration health")
                self.store.finalize_migrated_speech(
                    imported["checkpoint"]["imported_event_ids"]
                )
                committed_details = {
                    **details,
                    "checkpoint": imported["checkpoint"],
                    "imported_speech": imported["imported_speech"],
                    "health": health,
                }
                self.store.set_migration_record(
                    project_id=project_id,
                    source_hash=source_hash,
                    status="committed",
                    details=committed_details,
                )
                return self.store.migration_record(project_id)
            except BaseException as exc:
                if imported is not None:
                    self.store.rollback_legacy_configuration(
                        project_id=project_id,
                        checkpoint=imported["checkpoint"],
                    )
                    try:
                        self.controller.reconcile_project(project_id)
                        self.controller.sync_binding_visibility()
                    except Exception:
                        # The v0.1 runtime is restored below; keep the prior
                        # last-known-good v0.2 renderer if consumers remain ill.
                        pass
                self._cleanup_catalog(created)
                self.store.set_migration_record(
                    project_id=project_id,
                    source_hash=source_hash,
                    status="failed",
                    details={
                        "legacy_detected": True,
                        "source_paths": source_paths,
                        "diagnostic": str(exc),
                        **(
                            {"checkpoint": imported["checkpoint"]}
                            if imported is not None
                            else {}
                        ),
                    },
                )
                self.coordinator.restart(root)
                raise

    @staticmethod
    def _inspect_legacy_documents(project_root: Path) -> None:
        voice_root = project_root / ".codex-voice"
        for name in (
            "input.json",
            "sessions.json",
            "avatar-selection.json",
            "avatar-states.json",
            "orb-position.json",
        ):
            path = voice_root / name
            if path.is_file():
                _read_json(path, required=True)
        for path in (
            project_root / ".codex-live2d" / "installation.json",
            project_root / ".codex-live2d" / "avatar-state-revisions.json",
        ):
            if path.is_file():
                _read_json(path, required=True)

        sessions = _read_json(voice_root / "sessions.json")
        if sessions:
            mode = sessions.get("mode")
            if mode not in {"project", "session"}:
                raise ValidationError("Legacy session scope mode is invalid")
            definitions = sessions.get("sessions", {})
            if not isinstance(definitions, dict):
                raise ValidationError("Legacy session scope bindings must be an object")
            for raw_session, details in definitions.items():
                _canonical_session(raw_session)
                if not isinstance(details, dict):
                    raise ValidationError(
                        f"Legacy session scope entry is invalid: {raw_session}"
                    )
                declared = details.get("project_root")
                if declared is not None and normalize_project_root(str(declared))[0] != normalize_project_root(project_root)[0]:
                    raise ValidationError(
                        f"Legacy session {raw_session} belongs to a foreign project"
                    )

    def _build_plan(
        self,
        project_id: str,
        project_root: Path,
        created: dict[str, list[str]],
    ) -> dict[str, Any]:
        voice_root = project_root / ".codex-voice"
        profiles_path = voice_root / "presence-profiles.json"
        profiles_document = _read_json(profiles_path)
        if not profiles_document:
            selected = _read_json(voice_root / "avatar-selection.json").get(
                "avatar_id", "builtin"
            )
            profiles_document = {
                "schema": LEGACY_PROFILE_SCHEMA,
                "project_profile_id": "default",
                "profiles": {"default": {"avatar_id": selected}},
                "sessions": {},
            }
        elif profiles_document.get("schema") != LEGACY_PROFILE_SCHEMA:
            raise ValidationError("Legacy presence profile schema is unsupported")
        profiles = profiles_document.get("profiles")
        sessions = profiles_document.get("sessions", {})
        if not isinstance(profiles, dict) or not profiles:
            raise ValidationError("Legacy profile document has no profiles")
        if not isinstance(sessions, dict):
            raise ValidationError("Legacy profile sessions must be an object")
        default_id = profiles_document.get("project_profile_id", "default")
        if not isinstance(default_id, str) or default_id not in profiles:
            raise ValidationError("Legacy project profile reference is missing")

        selected_avatar = _read_json(voice_root / "avatar-selection.json").get(
            "avatar_id", "builtin"
        )
        settings = self._settings(voice_root)
        avatar_documents: dict[str, tuple[dict[str, Any], Path | None, str]] = {}
        needed_avatars = {
            str(profile.get("avatar_id") or selected_avatar or "builtin")
            for profile in profiles.values()
            if isinstance(profile, dict)
        }
        for avatar_id in sorted(needed_avatars):
            if avatar_id == "builtin":
                avatar_documents[avatar_id] = ({}, None, "builtin")
                continue
            pack, assets = self._legacy_avatar_pack(avatar_id, project_root)
            reference, was_created = self._register_avatar(pack, assets)
            if was_created:
                created["avatars"].append(pack["model_fingerprint"])
            avatar_documents[avatar_id] = (pack, assets, reference)

        profile_map: dict[str, str] = {}
        for legacy_id, legacy_profile in profiles.items():
            if not isinstance(legacy_id, str) or not isinstance(legacy_profile, dict):
                raise ValidationError("Legacy profile ids and values must be valid objects")
            avatar_id = str(legacy_profile.get("avatar_id") or selected_avatar or "builtin")
            pack, _assets, avatar_ref = avatar_documents[avatar_id]
            identifier = self._legacy_profile_id(project_id, legacy_id)
            document: dict[str, Any] = {
                "profile_id": identifier,
                "voice_id": str(legacy_profile.get("voice") or settings["voice_id"]),
                "speed": float(legacy_profile.get("speed", settings["speed"])),
                "playback_mode": str(
                    legacy_profile.get("mode") or settings["playback_mode"]
                ),
                "volume": settings["volume"],
                "commentary_ratio": settings["commentary_ratio"],
                "avatar_ref": avatar_ref,
                "progress_visible": settings["progress_visible"],
                "renderer_visible": settings["renderer_visible"],
            }
            curation = legacy_profile.get("curation")
            if curation is not None:
                if avatar_id == "builtin":
                    if curation not in ({}, {"initial_actions": [], "activity_actions": {}}):
                        raise ValidationError("Built-in legacy profile cannot carry Live2D curation")
                else:
                    document["semantic"] = self._legacy_semantic(curation, pack)
            saved, was_created = self._put_profile_idempotent(document)
            reference = f"{saved['profile_id']}@{saved['revision']}"
            if was_created:
                created["profiles"].append(reference)
            profile_map[legacy_id] = reference

        project_patch = {
            "profile_ref": profile_map[default_id],
            "progress_visible": settings["progress_visible"],
            "renderer_visible": settings["renderer_visible"],
        }
        session_patches: dict[str, dict[str, Any]] = {}
        for raw_session, binding in sessions.items():
            session_id = _canonical_session(raw_session)
            if isinstance(binding, str):
                profile_id = binding
            elif isinstance(binding, dict):
                profile_id = binding.get("profile_id")
            else:
                raise ValidationError(f"Legacy session binding is invalid: {raw_session}")
            if profile_id not in profile_map:
                raise ValidationError(
                    f"Legacy session {session_id} references missing profile {profile_id!r}"
                )
            session_patches[session_id] = {"profile_ref": profile_map[profile_id]}
        geometry = self._geometry(voice_root)
        speech, retired = self._pending_speech(voice_root, settings)
        validate_patch(project_patch, path="migration.project")
        for session_id, patch in session_patches.items():
            validate_patch(patch, path=f"migration.sessions.{session_id}")
        return {
            "project_patch": project_patch,
            "session_patches": session_patches,
            "geometry": geometry,
            "speech": speech,
            "retired_commentary": retired,
            "profile_map": profile_map,
        }

    @staticmethod
    def _settings(voice_root: Path) -> dict[str, Any]:
        try:
            speed = float(_read_text(voice_root / "speed", "1.08"))
            volume = int(_read_text(voice_root / "volume", "20"))
            commentary = int(_read_text(voice_root / "commentary-volume", "50"))
        except ValueError as exc:
            raise ValidationError("Legacy numeric voice settings are malformed") from exc
        mode = _read_text(voice_root / "mode", "stream").lower()
        if mode not in {"stream", "quality"}:
            raise ValidationError(f"Legacy playback mode is invalid: {mode}")
        return {
            "voice_id": _read_text(voice_root / "voice", "bf_isabella"),
            "speed": speed,
            "playback_mode": mode,
            "volume": max(0, min(100, volume)),
            "commentary_ratio": max(0.0, min(1.0, commentary / 100)),
            "progress_visible": _marker(voice_root / "progress"),
            "renderer_visible": _marker(voice_root / "orb.enabled"),
        }

    def _legacy_avatar_pack(
        self, avatar_id: str, project_root: Path
    ) -> tuple[dict[str, Any], Path]:
        registry = self.registry_root / avatar_id
        manifest = _read_json(registry / "manifest.json")
        profile = _read_json(registry / "profile.json")
        assets = registry / "source"
        if not manifest or not profile or not assets.is_dir():
            bundle = project_root / ".codex-live2d" / "bundles" / avatar_id
            capabilities = _read_json(bundle / "avatar-capabilities.json")
            if not capabilities or not (bundle / "model").is_dir():
                raise ValidationError(
                    f"Legacy avatar {avatar_id!r} has no complete registry or project bundle"
                )
            manifest = {
                "id": avatar_id,
                "model": capabilities.get("model"),
                "actions": capabilities.get("actions"),
            }
            profile = _read_json(
                self.registry_root / avatar_id / "profile.json", required=True
            )
            assets = bundle / "model"
        if avatar_id != "higan-live2d":
            raise ValidationError(
                f"Legacy avatar {avatar_id!r} has no curated v0.2 semantic-slot map; import a v0.2 model pack explicitly"
            )
        model = manifest.get("model")
        model_path = model.get("path") if isinstance(model, dict) else None
        if not isinstance(model_path, str):
            raise ValidationError(f"Legacy avatar {avatar_id!r} has no model entrypoint")
        if model_path.startswith("source/"):
            model_path = model_path[len("source/") :]
        actions = manifest.get("actions")
        if not isinstance(actions, list):
            raise ValidationError(f"Legacy avatar {avatar_id!r} has no actions")
        action_documents: dict[str, dict[str, Any]] = {}
        for action in actions:
            if not isinstance(action, dict) or action.get("id") not in HIGAN_SLOTS:
                continue
            action_id = action["id"]
            action_documents[action_id] = {
                "slots": list(HIGAN_SLOTS[action_id]),
                "label": str(action.get("label") or action_id),
                "description": str(action.get("description") or ""),
                "operations": list(action.get("parameter_operations") or []),
            }
        missing = set(HIGAN_SLOTS) - set(action_documents)
        if missing:
            raise ValidationError(
                f"Legacy Higan avatar is missing curated actions: {sorted(missing)}"
            )
        slots = {
            slot: {
                "exclusive": slot not in NONEXCLUSIVE_HIGAN_SLOTS,
                "description": f"Curated Higan semantic slot {slot}",
            }
            for claimed in HIGAN_SLOTS.values()
            for slot in claimed
        }
        renderer = profile.get("renderer")
        renderer = dict(renderer) if isinstance(renderer, dict) else {}
        renderer.pop("activity_actions", None)
        pack = {
            "schema": "presence/avatar-model-pack/v0.2",
            "avatar_id": avatar_id,
            "version": 1,
            "model_fingerprint": _tree_fingerprint(assets),
            "renderer": {
                "kind": "live2d",
                "entrypoint": model_path,
                **{
                    key: value
                    for key, value in renderer.items()
                    if key
                    in {
                        "scale",
                        "bottom_inset",
                        "halo",
                        "fixed_parameters",
                        "fixed_parts",
                        "speech_motion",
                    }
                },
            },
            "semantic_slots": slots,
            "actions": action_documents,
            # v0.1 model state was global and was the source of the cursed
            # shawl/stockings latch.  It is intentionally not a model default.
            "safe_defaults": {},
            "capabilities": [
                "activity-state",
                "audio-cadence",
                "geometry",
                "semantic-slots",
            ],
        }
        return validate_model_pack(pack), assets

    def _register_avatar(
        self, pack: Mapping[str, Any], assets: Path
    ) -> tuple[str, bool]:
        fingerprint = str(pack["model_fingerprint"])
        target = self.catalog.root / "avatars" / fingerprint.removeprefix("sha256:")
        existed = target.is_dir()
        reference = self.catalog.register_avatar(pack, assets=assets)
        return reference, not existed

    @staticmethod
    def _legacy_profile_id(project_id: str, legacy_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9._-]+", "-", legacy_id.lower()).strip("-._")
        if not normalized:
            normalized = "default"
        return f"legacy-{project_id[:8]}-{normalized}"[:128]

    def _put_profile_idempotent(
        self, document: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        identifier = str(document["profile_id"])
        try:
            current = self.catalog.get_profile(identifier)
        except NotFoundError:
            return self.catalog.put_profile(document), True
        comparable = {key: value for key, value in current.items() if key not in {"schema", "revision"}}
        requested = {key: value for key, value in document.items() if key not in {"schema", "revision"}}
        if comparable == requested:
            return current, False
        saved = self.catalog.put_profile(document, expected_revision=current["revision"])
        return saved, True

    @staticmethod
    def _legacy_semantic(
        curation: object, pack: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(curation, dict):
            raise ValidationError("Legacy curation must be an object")
        actions = pack["actions"]
        slots: dict[str, list[str]] = {}
        initial = curation.get("initial_actions", [])
        if not isinstance(initial, list):
            raise ValidationError("Legacy initial_actions must be a list")
        for action_id in initial:
            if action_id not in actions:
                raise ValidationError(f"Legacy curation references unknown action {action_id!r}")
            primary = actions[action_id]["slots"][0]
            slots.setdefault(primary, []).append(action_id)
        activity: dict[str, dict[str, list[str]]] = {}
        raw_activity = curation.get("activity_actions", {})
        if not isinstance(raw_activity, dict):
            raise ValidationError("Legacy activity_actions must be an object")
        for state, rule in raw_activity.items():
            if not isinstance(rule, dict):
                raise ValidationError(f"Legacy activity rule {state!r} must be an object")
            add = rule.get("add", [])
            suppress = rule.get("suppress", [])
            if not isinstance(add, list) or not isinstance(suppress, list):
                raise ValidationError(f"Legacy activity rule {state!r} has invalid lists")
            unknown = [action for action in [*add, *suppress] if action not in actions]
            if unknown:
                raise ValidationError(
                    f"Legacy activity rule {state!r} references unknown actions: {unknown}"
                )
            clear_slots: list[str] = []
            for action in suppress:
                for slot in actions[action]["slots"]:
                    if slot not in clear_slots:
                        clear_slots.append(slot)
            converted: dict[str, list[str]] = {}
            if clear_slots:
                converted["clear_slots"] = clear_slots
            if add:
                converted["add"] = list(add)
            activity[state] = converted
        result: dict[str, Any] = {}
        if slots:
            result["slots"] = slots
        if activity:
            result["activity"] = activity
        return result

    @staticmethod
    def _geometry(voice_root: Path) -> dict[str, dict[str, Any]]:
        document = _read_json(voice_root / "orb-position.json")
        windows = document.get("windows", {})
        if not isinstance(windows, dict):
            raise ValidationError("Legacy orb geometry windows must be an object")
        result: dict[str, dict[str, Any]] = {}
        for route, raw in windows.items():
            match = SESSION_ROUTE.search(str(route))
            if match is None or not isinstance(raw, dict):
                continue
            session_id = _canonical_session(match.group(1).lower())
            geometry = {
                name: raw[name]
                for name in ("x", "y", "width", "height")
                if name in raw
            }
            if geometry:
                result[session_id] = geometry
        return result

    @staticmethod
    def _pending_speech(
        voice_root: Path, settings: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], int]:
        database = voice_root / "inbox.sqlite3"
        if not database.is_file():
            return [], 0
        uri = f"file:{database.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE status IN ('queued', 'retry', 'playing')
                ORDER BY id
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise ValidationError(f"Legacy inbox is unreadable: {database}") from exc
        finally:
            connection.close()
        speech: list[dict[str, Any]] = []
        retired = 0
        for row in rows:
            columns = set(row.keys())

            def legacy(name: str, default: Any = None) -> Any:
                return row[name] if name in columns and row[name] is not None else default

            kind = str(legacy("kind", ""))
            if kind in {"commentary", "update"}:
                retired += 1
                continue
            if kind not in {"final", "announcement"}:
                raise ValidationError(f"Legacy inbox has unsupported pending kind {kind!r}")
            session_id = legacy("session_id")
            if session_id:
                session_id = _canonical_session(session_id)
            raw_event_id = legacy("event_id")
            if not isinstance(raw_event_id, str) or not raw_event_id:
                raise ValidationError("Legacy inbox item has no stable event id")
            event_id = raw_event_id
            text = legacy("text")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError(f"Legacy inbox item {event_id!r} has no speech text")
            speech.append(
                {
                    "session_id": session_id,
                    "event_id": event_id,
                    "utterance_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"presence:migrated:{event_id}")
                    ),
                    "text": text,
                    "kind": kind,
                    "tts": {
                        "voice_id": legacy("tts_voice", settings["voice_id"]),
                        "speed": legacy("tts_speed", settings["speed"]),
                        "playback_mode": legacy("tts_mode", settings["playback_mode"]),
                        "volume": int(legacy("volume", settings["volume"])),
                        "main_volume": settings["volume"],
                        "commentary_ratio": settings["commentary_ratio"],
                    },
                }
            )
        return speech, retired

    def _cleanup_catalog(self, created: Mapping[str, list[str]]) -> None:
        for reference in reversed(created.get("profiles", [])):
            identifier, revision_text = reference.rsplit("@", 1)
            path = self.catalog.root / "profiles" / identifier / f"{revision_text}.json"
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        for fingerprint in reversed(created.get("avatars", [])):
            target = self.catalog.root / "avatars" / fingerprint.removeprefix("sha256:")
            if target.is_dir() and not self.store.catalog_references("avatar", fingerprint):
                shutil.rmtree(target)
