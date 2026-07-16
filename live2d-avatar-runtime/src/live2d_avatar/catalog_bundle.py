"""Materialize a Live2D renderer inside one immutable v0.2 catalog version."""

from __future__ import annotations

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
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return target / "index.html"
