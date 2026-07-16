"""Machine-local immutable avatars and revisioned profile/preset catalog."""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import CatalogReferenceError, ConflictError, NotFoundError, ValidationError
from .paths import catalog_path
from .validation import (
    IDENTIFIER,
    validate_model_pack,
    validate_preset,
    validate_profile,
)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotFoundError(f"Catalog entry was not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Catalog entry is unreadable: {path}") from exc
    if not isinstance(document, dict):
        raise ValidationError(f"Catalog entry is not an object: {path}")
    return document


def _split_revision_ref(reference: str) -> tuple[str, int | None]:
    if not isinstance(reference, str) or not reference:
        raise ValidationError("catalog reference must be a non-empty string")
    name, separator, revision_text = reference.rpartition("@")
    if separator:
        if not name or not revision_text.isdigit() or int(revision_text) < 1:
            raise ValidationError(f"Invalid catalog revision reference: {reference!r}")
        identifier = name
        revision = int(revision_text)
    else:
        identifier = reference
        revision = None
    if not IDENTIFIER.fullmatch(identifier):
        raise ValidationError(f"Invalid catalog id: {identifier!r}")
    return identifier, revision


class Catalog:
    """Filesystem catalog with immutable writes and monotonic JSON revisions."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or catalog_path()).expanduser().resolve()
        self._lock = threading.RLock()

    def initialize(self) -> None:
        for name in ("avatars", "profiles", "presets"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def register_avatar(
        self,
        model_pack: Mapping[str, Any],
        *,
        assets: Path | None = None,
    ) -> str:
        pack = validate_model_pack(model_pack)
        fingerprint = pack["model_fingerprint"]
        key = fingerprint.removeprefix("sha256:")
        target = self.root / "avatars" / key
        with self._lock:
            self.initialize()
            if target.exists():
                existing = validate_model_pack(_read_json(target / "pack.json"))
                if existing != pack:
                    raise ConflictError(
                        f"Avatar fingerprint {fingerprint} already has different metadata"
                    )
                return f"{pack['avatar_id']}@{pack['version']}"

            # Keep the staging name short: pytest/user temp roots can already
            # be deep enough to approach the legacy Windows MAX_PATH boundary.
            temporary = target.parent / f".new-{uuid.uuid4().hex[:8]}"
            temporary.mkdir(parents=False)
            try:
                _atomic_json(temporary / "pack.json", pack)
                if assets is not None:
                    source = assets.expanduser().resolve()
                    if not source.is_dir():
                        raise NotFoundError(f"Avatar asset directory was not found: {source}")
                    shutil.copytree(source, temporary / "assets")
                    if pack["renderer"]["kind"] == "live2d":
                        try:
                            from live2d_avatar.catalog_bundle import (
                                materialize_catalog_bundle,
                            )
                        except ImportError as exc:
                            raise ConflictError(
                                "Canonical Live2D runtime is unavailable; "
                                "cannot materialize a catalog renderer"
                            ) from exc
                        materialize_catalog_bundle(
                            pack,
                            source,
                            temporary / "renderer",
                        )
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        return f"{pack['avatar_id']}@{pack['version']}"

    def list_avatars(self) -> list[dict[str, Any]]:
        root = self.root / "avatars"
        if not root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(root.glob("*/pack.json")):
            entries.append(validate_model_pack(_read_json(path)))
        return sorted(entries, key=lambda item: (item["avatar_id"], item["version"]))

    def get_avatar(self, reference: str) -> dict[str, Any]:
        if reference.startswith("sha256:"):
            key = reference.removeprefix("sha256:")
            return validate_model_pack(_read_json(self.root / "avatars" / key / "pack.json"))
        identifier, version = _split_revision_ref(reference)
        candidates = [
            item
            for item in self.list_avatars()
            if item["avatar_id"] == identifier
            and (version is None or item["version"] == version)
        ]
        if not candidates:
            raise NotFoundError(f"Avatar {reference!r} is not installed")
        return max(candidates, key=lambda item: item["version"])

    def put_profile(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        profile_id = document.get("profile_id")
        if not isinstance(profile_id, str) or not IDENTIFIER.fullmatch(profile_id):
            raise ValidationError("profile.profile_id contains unsupported characters")
        payload = {
            key: copy.deepcopy(value)
            for key, value in document.items()
            if key not in {"schema", "revision"}
        }
        return self._put_revisioned(
            kind="profiles",
            identifier=profile_id,
            payload=payload,
            expected_revision=expected_revision,
            validator=validate_profile,
            schema="presence/profile/v0.2",
            id_field="profile_id",
        )

    def put_preset(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        preset_id = document.get("preset_id")
        if not isinstance(preset_id, str) or not IDENTIFIER.fullmatch(preset_id):
            raise ValidationError("preset.preset_id contains unsupported characters")
        payload = {
            key: copy.deepcopy(value)
            for key, value in document.items()
            if key not in {"schema", "revision"}
        }
        return self._put_revisioned(
            kind="presets",
            identifier=preset_id,
            payload=payload,
            expected_revision=expected_revision,
            validator=validate_preset,
            schema="presence/preset/v0.2",
            id_field="preset_id",
        )

    def _put_revisioned(
        self,
        *,
        kind: str,
        identifier: str,
        payload: Mapping[str, Any],
        expected_revision: int | None,
        validator: Any,
        schema: str,
        id_field: str,
    ) -> dict[str, Any]:
        directory = self.root / kind / identifier
        with self._lock:
            self.initialize()
            revisions = self._revisions(directory)
            current = revisions[-1] if revisions else 0
            if expected_revision is not None and current != expected_revision:
                raise ConflictError(
                    f"{identifier!r} is at revision {current}, expected {expected_revision}"
                )
            revision = current + 1
            candidate = {
                "schema": schema,
                id_field: identifier,
                "revision": revision,
                **copy.deepcopy(dict(payload)),
            }
            validated = validator(candidate)
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / f"{revision}.json"
            try:
                with destination.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(validated, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:
                raise ConflictError(
                    f"Concurrent revision write detected for {identifier!r}"
                ) from exc
            return validated

    def get_profile(self, reference: str) -> dict[str, Any]:
        return validate_profile(self._get_revisioned("profiles", reference))

    def get_preset(self, reference: str) -> dict[str, Any]:
        return validate_preset(self._get_revisioned("presets", reference))

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._list_latest("profiles", validate_profile)

    def list_presets(self) -> list[dict[str, Any]]:
        return self._list_latest("presets", validate_preset)

    def _get_revisioned(self, kind: str, reference: str) -> dict[str, Any]:
        identifier, revision = _split_revision_ref(reference)
        directory = self.root / kind / identifier
        if revision is None:
            revisions = self._revisions(directory)
            if not revisions:
                raise NotFoundError(f"{kind[:-1].title()} {identifier!r} is not installed")
            revision = revisions[-1]
        return _read_json(directory / f"{revision}.json")

    def _list_latest(self, kind: str, validator: Any) -> list[dict[str, Any]]:
        root = self.root / kind
        if not root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            revisions = self._revisions(directory)
            if revisions:
                entries.append(validator(_read_json(directory / f"{revisions[-1]}.json")))
        return entries

    @staticmethod
    def _revisions(directory: Path) -> list[int]:
        if not directory.is_dir():
            return []
        return sorted(
            int(path.stem)
            for path in directory.glob("*.json")
            if path.stem.isdigit() and int(path.stem) > 0
        )

    def export(self, kind: str, reference: str, output: Path, *, force: bool = False) -> Path:
        if kind == "profile":
            document = self.get_profile(reference)
        elif kind == "preset":
            document = self.get_preset(reference)
        elif kind == "avatar":
            document = self.get_avatar(reference)
        else:
            raise ValidationError(f"Unsupported catalog kind: {kind}")
        destination = output.expanduser().resolve()
        if destination.exists() and not force:
            raise ConflictError(f"Refusing to overwrite user file: {destination}")
        _atomic_json(destination, document)
        return destination

    def import_portable(self, source: Path) -> tuple[str, str]:
        document = _read_json(source.expanduser().resolve())
        schema = document.get("schema")
        if schema == "presence/profile/v0.2":
            saved = self.put_profile(document)
            return "profile", f"{saved['profile_id']}@{saved['revision']}"
        if schema == "presence/preset/v0.2":
            saved = self.put_preset(document)
            return "preset", f"{saved['preset_id']}@{saved['revision']}"
        if schema == "presence/avatar-model-pack/v0.2":
            return "avatar", self.register_avatar(document)
        raise ValidationError(f"Unsupported portable catalog schema: {schema!r}")

    def remove(
        self,
        kind: str,
        reference: str,
        *,
        references: Iterable[str] = (),
        force: bool = False,
    ) -> None:
        active_references = tuple(references)
        if active_references and not force:
            raise CatalogReferenceError(
                f"{kind} {reference!r} is referenced by {list(active_references)}"
            )
        if kind == "avatar":
            pack = self.get_avatar(reference)
            target = self.root / "avatars" / pack["model_fingerprint"].removeprefix("sha256:")
        elif kind in {"profile", "preset"}:
            identifier, _revision = _split_revision_ref(reference)
            target = self.root / f"{kind}s" / identifier
            if not target.is_dir():
                raise NotFoundError(f"{kind.title()} {reference!r} is not installed")
        else:
            raise ValidationError(f"Unsupported catalog kind: {kind}")
        resolved_root = self.root.resolve()
        resolved_target = target.resolve()
        expected_parent = resolved_root / (
            "avatars" if kind == "avatar" else f"{kind}s"
        )
        if resolved_target.parent != expected_parent:
            raise ConflictError(f"Refusing unsafe catalog removal: {resolved_target}")
        shutil.rmtree(resolved_target)
