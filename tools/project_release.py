"""Project only the installable skill into a clean release artifact."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
