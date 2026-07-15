"""Safe archive/directory import and generic Cubism expression discovery."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import AvatarRuntimeError
from .manifest import ACTIVE_TOGGLE_SET, MANIFEST_SCHEMA, STATE_SCHEMA, load_manifest
from .paths import atomic_write_json, model_directory, remove_owned_tree, resolve_registry, validate_model_id


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_archive_parts(name: str) -> tuple[str, ...]:
    candidate = PurePosixPath(name.replace("\\", "/"))
    if not name or candidate.is_absolute() or ".." in candidate.parts:
        raise AvatarRuntimeError(f"archive contains an unsafe path: {name!r}")
    if any(part.endswith(":") for part in candidate.parts):
        raise AvatarRuntimeError(f"archive contains a drive-qualified path: {name!r}")
    parts = tuple(part for part in candidate.parts if part not in ("", "."))
    if not parts:
        raise AvatarRuntimeError(f"archive contains an empty path: {name!r}")
    return parts


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    try:
        bundle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise AvatarRuntimeError(f"not a readable ZIP archive: {archive}") from exc
    with bundle:
        for entry in bundle.infolist():
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise AvatarRuntimeError(f"archive contains a symbolic link: {entry.filename!r}")
            parts = _safe_archive_parts(entry.filename)
            target = destination.joinpath(*parts)
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise AvatarRuntimeError(f"archive path escapes destination: {entry.filename!r}") from exc
            if entry.is_dir() or entry.filename.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(entry, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _safe_copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise AvatarRuntimeError("refusing to import a symbolic-link source directory")
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise AvatarRuntimeError(f"refusing to import symbolic link: {entry}")
        relative = entry.relative_to(source)
        target = destination / relative
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)


def _read_json_lenient(path: Path, warnings: list[str]) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        warnings.append(f"could not parse JSON metadata: {path.name}")
        return None
    return document if isinstance(document, dict) else None


def _walk_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_objects(child)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def _expression_metadata(root: Path, warnings: list[str]) -> dict[str, dict[str, Any]]:
    """Collect local VTube metadata without inferring expression compatibility.

    VTube Studio hotkeys are individual expression toggles. Their order is useful
    as a model-local replay order, but the file format does not declare conflicts,
    dependencies, or composite poses.
    """

    result: dict[str, dict[str, Any]] = {}
    replay_order = 0
    for metadata_path in sorted(root.rglob("*.vtube.json"), key=lambda path: path.as_posix().casefold()):
        metadata = _read_json_lenient(metadata_path, warnings)
        if metadata is None:
            continue
        hotkeys = metadata.get("Hotkeys")
        if isinstance(hotkeys, list):
            nodes = [node for node in hotkeys if isinstance(node, dict)]
        else:
            nodes = list(_walk_objects(metadata))
        for node in nodes:
            file_name = node.get("File")
            if not isinstance(file_name, str) or not file_name.lower().endswith(".exp3.json"):
                continue
            key = Path(file_name).name.casefold()
            entry = result.setdefault(key, {"hotkeys": [], "replay_order": None})
            triggers = {trigger for trigger in _strings(node.get("Triggers")) if trigger.strip()}
            entry["hotkeys"].extend(triggers)
            if entry["replay_order"] is None and node.get("Action") in (None, "ToggleExpression"):
                entry["replay_order"] = replay_order
                replay_order += 1
    for entry in result.values():
        entry["hotkeys"] = sorted(set(entry["hotkeys"]))
    return result


def _expression_operations(expression: Path, warnings: list[str]) -> list[dict[str, Any]]:
    document = _read_json_lenient(expression, warnings)
    if document is None:
        return []
    parameters = document.get("Parameters")
    if not isinstance(parameters, list):
        warnings.append(f"expression has no Parameters array: {expression.name}")
        return []
    operations: list[dict[str, Any]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        parameter_id = parameter.get("Id")
        value = parameter.get("Value")
        if not isinstance(parameter_id, str) or not isinstance(value, (int, float)):
            warnings.append(f"ignored invalid expression parameter in: {expression.name}")
            continue
        blend = parameter.get("Blend", "Add")
        operations.append(
            {
                "parameter_id": parameter_id,
                "value": float(value),
                "blend": blend if isinstance(blend, str) else "Add",
            }
        )
    return operations


def _action_id(relative_expression_path: str) -> str:
    digest = hashlib.sha256(relative_expression_path.encode("utf-8")).hexdigest()[:12]
    return f"expression.{digest}"


def _expression_label(path: Path) -> str:
    suffix = ".exp3.json"
    return path.name[: -len(suffix)] if path.name.lower().endswith(suffix) else path.stem


def _discover_actions(source_root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    metadata = _expression_metadata(source_root, warnings)
    actions: list[dict[str, Any]] = []
    expressions = sorted(source_root.rglob("*.exp3.json"), key=lambda path: path.as_posix().casefold())
    fallback_order = len(expressions)
    for fallback_index, expression in enumerate(expressions):
        relative = expression.relative_to(source_root).as_posix()
        expression_metadata = metadata.get(expression.name.casefold(), {})
        replay_order = expression_metadata.get("replay_order")
        if not isinstance(replay_order, int) or replay_order < 0:
            replay_order = fallback_order + fallback_index
        actions.append(
            {
                "id": _action_id(relative),
                "label": _expression_label(expression),
                "kind": "expression",
                "source": f"source/{relative}",
                "hotkeys": expression_metadata.get("hotkeys", []),
                "replay_order": replay_order,
                "parameter_operations": _expression_operations(expression, warnings),
            }
        )
    return sorted(actions, key=lambda action: (action["replay_order"], action["source"].casefold()))


def _find_model(source_root: Path) -> Path:
    candidates = sorted(
        (path for path in source_root.rglob("*.model3.json") if path.is_file()),
        key=lambda path: path.as_posix().casefold(),
    )
    if not candidates:
        raise AvatarRuntimeError("imported source does not contain a .model3.json file")
    if len(candidates) > 1:
        names = ", ".join(path.relative_to(source_root).as_posix() for path in candidates)
        raise AvatarRuntimeError(
            "source contains multiple .model3.json files; explicit model selection is required: " + names
        )
    return candidates[0]


def _initial_state(model_id: str) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "model_id": model_id,
        "revision": 0,
        "active_actions": [],
        "effective_parameter_operations": [],
    }


def import_model(source: Path, model_id: str, registry: Path, *, replace: bool = False) -> dict[str, Any]:
    """Copy a user-owned model into the registry and generate manifest/state files."""

    source = source.expanduser().resolve()
    model_id = validate_model_id(model_id)
    registry = resolve_registry(registry)
    registry.mkdir(parents=True, exist_ok=True)
    target = model_directory(registry, model_id)
    if target.exists():
        if not replace:
            raise AvatarRuntimeError(f"model already exists: {target}; pass --replace to import over it")
        if not target.is_dir() or target.is_symlink():
            raise AvatarRuntimeError(f"refusing to replace a non-managed model path: {target}")
        existing_manifest = load_manifest(registry, model_id)
        lifecycle = existing_manifest.get("lifecycle")
        if not isinstance(lifecycle, dict) or lifecycle.get("owner") != "live2d-avatar-runtime":
            raise AvatarRuntimeError(f"refusing to replace a model not owned by this runtime: {target}")
    if not source.exists():
        raise AvatarRuntimeError(f"model source does not exist: {source}")
    if not source.is_dir() and source.suffix.casefold() != ".zip":
        raise AvatarRuntimeError("model source must be a directory or a .zip archive")

    staging = registry / f".{model_id}.import-{uuid.uuid4().hex}"
    warnings: list[str] = []
    try:
        imported_source = staging / "source"
        imported_source.mkdir(parents=True)
        if source.is_dir():
            _safe_copy_tree(source, imported_source)
            source_description: dict[str, Any] = {"kind": "directory", "label": source.name}
        else:
            _safe_extract_zip(source, imported_source)
            source_description = {
                "kind": "zip",
                "label": source.name,
                "sha256": _sha256_file(source),
            }
        model = _find_model(imported_source)
        model_relative = model.relative_to(imported_source).as_posix()
        actions = _discover_actions(imported_source, warnings)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "id": model_id,
            "kind": "live2d-cubism",
            "state_semantics": ACTIVE_TOGGLE_SET,
            "model": {"path": f"source/{model_relative}"},
            "import": source_description,
            "actions": actions,
            "lifecycle": {
                "owner": "live2d-avatar-runtime",
                "owned_paths": ["source/", "manifest.json", "state.json"],
            },
            "warnings": warnings,
        }
        atomic_write_json(staging / "manifest.json", manifest)
        atomic_write_json(staging / "state.json", _initial_state(model_id))
        if target.exists():
            remove_owned_tree(target, registry)
        staging.replace(target)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "id": model_id,
        "registry": str(registry),
        "model_directory": str(target),
        "manifest": str(target / "manifest.json"),
        "state": str(target / "state.json"),
        "warning_count": len(warnings),
    }
