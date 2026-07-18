from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path


def test_renderer_template_is_declared_as_package_data() -> None:
    root = Path(__file__).resolve().parents[1]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = document["tool"]["setuptools"]["package-data"]["live2d_avatar"]
    package = root / "src" / "live2d_avatar"
    renderer = package / "assets" / "renderer-template"
    assets = [path.relative_to(package).as_posix() for path in renderer.rglob("*") if path.is_file()]

    assert assets
    assert all(any(fnmatch.fnmatch(asset, pattern) for pattern in patterns) for asset in assets)
