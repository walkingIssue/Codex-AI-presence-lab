"""Copy and verify canonical runtime packages in projected skill artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


PROJECTION_MANIFEST = "PROJECTION-MANIFEST.json"
IGNORED_NAMES = {"__pycache__", ".pytest_cache", "tests", "profiles"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _ignored(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORED_NAMES or Path(name).suffix.lower() in IGNORED_SUFFIXES
    }


def _payload_files(root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != PROJECTION_MANIFEST
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in _payload_files(root)
    }


def tree_digest(files: dict[str, str]) -> str:
    canonical = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def copy_canonical_tree(source: Path, destination: Path, *, package: str) -> dict[str, object]:
    if not source.is_dir():
        raise FileNotFoundError(f"Canonical package was not found: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=_ignored)
    files = payload_hashes(destination)
    document: dict[str, object] = {
        "schema": "presence/canonical-projection/v0.2",
        "package": package,
        "algorithm": "sha256",
        "tree_digest": tree_digest(files),
        "files": files,
    }
    (destination / PROJECTION_MANIFEST).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def verify_projection(root: Path, *, package: str | None = None) -> dict[str, object]:
    manifest_path = root / PROJECTION_MANIFEST
    if not manifest_path.is_file():
        raise ValueError(f"Projection manifest is missing: {manifest_path}")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Projection manifest is invalid: {manifest_path}") from exc
    if document.get("schema") != "presence/canonical-projection/v0.2":
        raise ValueError(f"Unsupported projection schema in {manifest_path}")
    if package is not None and document.get("package") != package:
        raise ValueError(
            f"Projection package is {document.get('package')!r}, expected {package!r}"
        )
    expected = document.get("files")
    if not isinstance(expected, dict) or not all(
        isinstance(name, str) and isinstance(digest, str)
        for name, digest in expected.items()
    ):
        raise ValueError(f"Projection file hashes are invalid: {manifest_path}")
    actual = payload_hashes(root)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            name for name in set(actual) & set(expected) if actual[name] != expected[name]
        )
        raise ValueError(
            "Projection differs from its canonical hash manifest: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    if document.get("tree_digest") != tree_digest(actual):
        raise ValueError(f"Projection tree digest is invalid: {manifest_path}")
    return document
