"""Run the Live2D runtime bundled with the unified codex-voice skill."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _runtime_sources() -> list[Path]:
    skill_root = Path(__file__).resolve().parents[1]
    source_root = Path(__file__).resolve().parents[3]
    candidates = []
    configured = os.environ.get("CODEX_LIVE2D_RUNTIME")
    if configured:
        candidates.append(Path(configured).expanduser())
    # Projected skill: skills/codex-voice/live2d-avatar-runtime/src.
    candidates.append(skill_root / "live2d-avatar-runtime" / "src")
    # Source checkout: <project>/live2d-avatar-runtime/src.
    candidates.append(source_root / "live2d-avatar-runtime" / "src")
    return candidates


def main() -> int:
    for runtime_source in _runtime_sources():
        runtime_source = runtime_source.resolve()
        if (runtime_source / "live2d_avatar").is_dir():
            sys.path.insert(0, str(runtime_source))
            from live2d_avatar.cli import main as runtime_main

            return runtime_main()
    searched = ", ".join(str(path) for path in _runtime_sources())
    print(
        "The bundled Live2D runtime was not found. Re-project the unified "
        f"codex-voice skill; searched: {searched}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
