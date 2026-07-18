"""Materialize a Live2D renderer inside one immutable v0.2 catalog version."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from .errors import AvatarRuntimeError


def _template_root() -> Path:
    root = Path(__file__).resolve().parent / "assets" / "renderer-template"
    if not (root / "index.html").is_file():
        raise AvatarRuntimeError(f"renderer template is incomplete: {root}")
    return root


def renderer_template_fingerprint() -> str:
    """Fingerprint derived renderer code independently of user model assets."""

    root = _template_root()
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def catalog_bundle_is_current(
    destination: Path,
    model_pack: Mapping[str, Any],
) -> bool:
    """Return whether derived renderer files match this runtime template."""

    target = destination.expanduser().resolve()
    required = ("index.html", "renderer.js", "styles.css", "avatar-capabilities.js")
    if any(not (target / name).is_file() for name in required):
        return False
    try:
        metadata = json.loads(
            (target / "catalog-renderer.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    if metadata != {
        "schema": "presence/catalog-renderer/v0.2",
        "avatar_ref": f"{model_pack['avatar_id']}@{model_pack['version']}",
        "model_fingerprint": model_pack["model_fingerprint"],
        "renderer_template_fingerprint": renderer_template_fingerprint(),
    }:
        return False
    template = _template_root()
    for source in (item for item in template.rglob("*") if item.is_file()):
        projected = target / source.relative_to(template)
        if not projected.is_file() or projected.read_bytes() != source.read_bytes():
            return False
    return True


def _capability_document(model_pack: Mapping[str, Any]) -> dict[str, Any]:
    entrypoint = str(model_pack["renderer"]["entrypoint"]).replace("\\", "/")
    if entrypoint.startswith("/") or ".." in Path(entrypoint).parts:
        raise AvatarRuntimeError("model pack renderer entrypoint is unsafe")
    if entrypoint.startswith("model/"):
        model_path = entrypoint
    else:
        model_path = "model/" + entrypoint
    actions = []
    for action_id, action in model_pack["actions"].items():
        actions.append(
            {
                "id": action_id,
                "label": action.get("label", action_id),
                "description": action.get("description", ""),
                "parameter_operations": action.get("operations", []),
            }
        )
    return {
        "schema": "live2d-avatar/capabilities/v0.2",
        "avatar_id": model_pack["avatar_id"],
        "state_semantics": "resolved-effective-snapshot",
        "model": {"path": model_path},
        "actions": actions,
        "safe_default_operations": [],
        "initial_actions": [],
        "renderer": {
            "scale": model_pack["renderer"].get("scale", 1.0),
            "bottom_inset": model_pack["renderer"].get("bottom_inset", 6.0),
            "halo": model_pack["renderer"].get("halo", {"enabled": False}),
            "fixed_parameters": model_pack["renderer"].get("fixed_parameters", []),
            "fixed_parts": model_pack["renderer"].get("fixed_parts", []),
            "speech_motion": model_pack["renderer"].get("speech_motion", {}),
        },
    }


def materialize_catalog_bundle(
    model_pack: Mapping[str, Any],
    source_assets: Path,
    destination: Path,
) -> Path:
    """Create a self-contained renderer without any project installation."""

    source = source_assets.expanduser().resolve()
    target = destination.expanduser().resolve()
    if not source.is_dir():
        raise AvatarRuntimeError(f"model asset source does not exist: {source}")
    if target.exists():
        raise AvatarRuntimeError(f"renderer destination already exists: {target}")
    shutil.copytree(_template_root(), target)
    shutil.copytree(source, target / "model")
    capabilities = _capability_document(model_pack)
    model_path = target / capabilities["model"]["path"]
    if not model_path.is_file():
        shutil.rmtree(target)
        raise AvatarRuntimeError(
            f"renderer entrypoint does not exist in copied model assets: {model_path}"
        )
    encoded = json.dumps(capabilities, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    (target / "avatar-capabilities.json").write_text(
        json.dumps(capabilities, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "avatar-capabilities.js").write_text(
        f"window.__LIVE2D_AVATAR_CAPABILITIES__ = {encoded};\n",
        encoding="utf-8",
    )
    (target / "catalog-renderer.json").write_text(
        json.dumps(
            {
                "schema": "presence/catalog-renderer/v0.2",
                "avatar_ref": f"{model_pack['avatar_id']}@{model_pack['version']}",
                "model_fingerprint": model_pack["model_fingerprint"],
                "renderer_template_fingerprint": renderer_template_fingerprint(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target / "index.html"
