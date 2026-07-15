# Unified skill packaging

The installable agent-facing surface is `skills/codex-voice/SKILL.md`. The
Live2D runtime is an internal package under `live2d-avatar-runtime/` and is
projected into the `codex-voice` skill by `tools/project_release.py`.

The old `live2d-avatar-controls` skill document and agent manifest are no
longer discoverable. Their workflow is merged into `codex-voice/SKILL.md` and
their manifest/state reference is available at
`skills/codex-voice/references/live2d-manifest-and-state.md`.

The unified skill owns the installation of the project-local Voice runtime,
Orb, and bridge. The bundled Live2D package owns model import, profiles,
project bindings, and renderer bundles. Those are intentionally separate
cleanup boundaries even though they share one skill: Voice uninstall removes
`.codex-voice`, while Live2D `project uninstall` and `model remove` remove only
their own ownership-marked data.

Use `skills/codex-voice/scripts/live2d-avatar.py` (or the platform launcher
beside it) for all Live2D operations. The legacy standalone PowerShell install
scripts now stop with a migration message rather than creating a second
global runtime or skill.
