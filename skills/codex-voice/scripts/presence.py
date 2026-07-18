"""Bootstrap the canonical Presence Runtime CLI from source or projection."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    script = Path(__file__).resolve()
    skill_root = script.parents[1]
    repository_root = script.parents[3]
    candidates = [
        skill_root / "presence-runtime" / "src",
        repository_root / "presence-runtime" / "src",
    ]
    for source in candidates:
        if (source / "presence_runtime").is_dir():
            sys.path.insert(0, str(source))
            from presence_runtime.cli import main as presence_main

            return presence_main()
    print(
        "Canonical Presence Runtime package is missing; reinstall the codex-voice projection.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
