"""Project only the installable skill into a clean release artifact."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from canonical_projection import copy_canonical_tree

IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    # Canonical runtime packages are projected explicitly below. A stale local
    # copy beneath the skill must never win merely because it exists on disk.
    "live2d-avatar-runtime",
    "presence-runtime",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_root = args.source.resolve()
    output_root = args.output.resolve()
    source_skill = source_root / "skills" / "codex-voice"
    destination = output_root / "skills" / "codex-voice"

    if not source_skill.is_dir():
        raise SystemExit(f"Skill source was not found: {source_skill}")
    if output_root.exists():
        raise SystemExit(f"Refusing to overwrite existing output: {output_root}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_skill, destination, ignore=IGNORE)

    # Runtime implementations are canonical root packages. Project them into
    # the installable skill and record every payload hash; do not maintain a
    # second tracked implementation below skills/codex-voice.
    source_live2d = source_root / "live2d-avatar-runtime"
    destination_live2d = destination / "live2d-avatar-runtime"
    source_presence = source_root / "presence-runtime"
    destination_presence = destination / "presence-runtime"
    try:
        live2d_projection = copy_canonical_tree(
            source_live2d,
            destination_live2d,
            package="live2d-avatar-runtime",
        )
        presence_projection = copy_canonical_tree(
            source_presence,
            destination_presence,
            package="presence-runtime",
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    forbidden = []
    for path in output_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(output_root)
        if any(part.lower() in {"html", "media", ".git"} for part in relative.parts):
            forbidden.append(str(relative))
    if forbidden:
        raise SystemExit(f"Forbidden release files were projected: {forbidden}")

    files = sum(1 for path in output_root.rglob("*") if path.is_file())
    print(f"Projected {files} files into {output_root}")
    print(f"Installable root: {destination}")
    print(f"Live2D canonical digest: {live2d_projection['tree_digest']}")
    print(f"Presence canonical digest: {presence_projection['tree_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
