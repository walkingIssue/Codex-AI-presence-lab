"""Filesystem boundaries and atomic JSON helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .errors import AvatarRuntimeError


MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_model_id(model_id: str) -> str:
    if not MODEL_ID_PATTERN.fullmatch(model_id):
        raise AvatarRuntimeError(
            "model id must use lowercase letters, digits, and hyphens "
            "(1-64 characters)"
        )
    return model_id


def default_registry() -> Path:
    configured = os.environ.get("CODEX_LIVE2D_REGISTRY")
    return Path(configured).expanduser() if configured else Path.home() / ".codex" / "live2d-models"


def resolve_registry(value: Path | None) -> Path:
    return (value or default_registry()).expanduser().resolve()


def model_directory(registry: Path, model_id: str) -> Path:
    return resolve_registry(registry) / validate_model_id(model_id)


def project_runtime_directory(project: Path) -> Path:
    return project.expanduser().resolve() / ".codex-live2d"


def require_managed_child(path: Path, owner_root: Path) -> tuple[Path, Path]:
    """Return resolved paths only when `path` is a real child of `owner_root`."""

    original = path.expanduser()
    if original.is_symlink():
        raise AvatarRuntimeError(f"refusing to manage symbolic link: {original}")
    resolved_root = owner_root.expanduser().resolve()
    resolved_path = original.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise AvatarRuntimeError(
            f"refusing to manage a path outside its owned boundary: {resolved_path}"
        ) from exc
    if resolved_path == resolved_root:
        raise AvatarRuntimeError("refusing to remove an ownership root itself")
    return resolved_path, resolved_root


def remove_owned_tree(path: Path, owner_root: Path) -> None:
    target, _ = require_managed_child(path, owner_root)
    if not target.is_dir():
        raise AvatarRuntimeError(f"managed directory does not exist: {target}")
    shutil.rmtree(target)


def atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AvatarRuntimeError(f"required file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AvatarRuntimeError(f"could not read JSON file: {path}") from exc
    if not isinstance(document, dict):
        raise AvatarRuntimeError(f"JSON document must be an object: {path}")
    return document
