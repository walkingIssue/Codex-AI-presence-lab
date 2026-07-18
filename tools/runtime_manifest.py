"""Validate the machine manifest and generate its human review document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_COMPONENT_FIELDS = {
    "id",
    "scope",
    "owner",
    "artifacts",
    "dependencies",
    "dependents",
    "preserved_data",
    "removal",
}


def load_manifest(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != "presence/runtime-manifest/v0.2":
        raise ValueError("runtime manifest schema must be presence/runtime-manifest/v0.2")
    components = document.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("runtime manifest must define components")
    identifiers: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise ValueError(f"component {index} must be an object")
        missing = REQUIRED_COMPONENT_FIELDS - set(component)
        unknown = set(component) - REQUIRED_COMPONENT_FIELDS
        if missing or unknown:
            raise ValueError(
                f"component {index} fields invalid: missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        identifier = component["id"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError(f"component id is invalid or duplicated: {identifier!r}")
        identifiers.add(identifier)
        if component["scope"] not in {"user", "project"}:
            raise ValueError(f"component {identifier} has invalid scope")
        for field in ("artifacts", "dependencies", "dependents", "preserved_data"):
            if not isinstance(component[field], list) or any(
                not isinstance(item, str) for item in component[field]
            ):
                raise ValueError(f"component {identifier}.{field} must be a string list")
    for component in components:
        unknown_refs = (
            set(component["dependencies"]) | set(component["dependents"])
        ) - identifiers
        if unknown_refs:
            raise ValueError(
                f"component {component['id']} references unknown components: {sorted(unknown_refs)}"
            )
    return document


def render(document: dict[str, Any]) -> str:
    lines = [
        "# Presence Runtime Manifest",
        "",
        "> Generated from `presence-runtime/runtime-manifest.json`; do not edit by hand.",
        "",
        f"Schema: `{document['schema']}`",
        f"Revision: `{document['revision']}`",
        f"Release unit: `{document['release_unit']}`",
        "",
        "## Preserved user data",
        "",
    ]
    for item in document.get("preserved_user_data", []):
        lines.append(f"- `{item}`")
    for component in document["components"]:
        lines.extend(
            [
                "",
                f"## `{component['id']}`",
                "",
                f"- Scope: `{component['scope']}`",
                f"- Owner: `{component['owner']}`",
                "- Artifacts: " + (", ".join(f"`{item}`" for item in component["artifacts"]) or "none"),
                "- Dependencies: " + (", ".join(f"`{item}`" for item in component["dependencies"]) or "none"),
                "- Dependents: " + (", ".join(f"`{item}`" for item in component["dependents"]) or "none"),
                "- Preserved data: " + (", ".join(f"`{item}`" for item in component["preserved_data"]) or "none"),
                f"- Removal: {component['removal']}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("presence-runtime/runtime-manifest.json"),
    )
    parser.add_argument("--output", type=Path, action="append")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document = load_manifest(args.manifest.resolve())
    rendered = render(document)
    outputs = args.output or [
        Path("presence-runtime/RUNTIME-MANIFEST.md"),
        Path("skills/codex-voice/RUNTIME-MANIFEST.md"),
    ]
    for output in outputs:
        destination = output.resolve()
        if args.check:
            if not destination.is_file() or destination.read_text(encoding="utf-8") != rendered:
                raise SystemExit(f"Generated runtime manifest is stale: {destination}")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Validated {len(document['components'])} runtime components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
