"""Hash-owned installation records used by lifecycle and uninstall."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import ConflictError, ValidationError


INSTALLATION_SCHEMA = "presence/installation/v0.2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
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


def read_installation(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConflictError("Presence Runtime is not installed") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Presence installation record is unreadable: {path}") from exc
    if not isinstance(document, dict) or document.get("schema") != INSTALLATION_SCHEMA:
        raise ValidationError("Presence installation record has an unsupported schema")
    managed = document.get("managed_files")
    if not isinstance(managed, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        for name, digest in managed.items()
    ):
        raise ValidationError("Presence installation record has invalid managed_files")
    return document


def file_inventory(root: Path, paths: Iterable[Path]) -> dict[str, str]:
    resolved_root = root.resolve()
    inventory: dict[str, str] = {}
    for candidate in paths:
        path = candidate.resolve()
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(resolved_root)
        except ValueError as exc:
            raise ConflictError(f"Managed file is outside Presence home: {path}") from exc
        inventory[relative.as_posix()] = sha256_file(path)
    return dict(sorted(inventory.items()))


def verify_managed_file(root: Path, relative: str, digest: str) -> Path:
    candidate = (root / Path(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ConflictError(f"Unsafe managed path in installation record: {relative}") from exc
    if candidate.is_file() and sha256_file(candidate) != digest:
        raise ConflictError(
            f"Refusing to remove modified managed file: {candidate}"
        )
    return candidate
