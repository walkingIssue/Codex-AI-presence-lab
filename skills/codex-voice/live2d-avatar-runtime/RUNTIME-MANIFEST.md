# Live2D Avatar Runtime — Lifecycle Manifest

This file is the source-controlled ownership ledger for this runtime. It is required review material for every branch. Do not add a user-machine resource without updating this file and its matching cleanup path in the same change.

## Ownership boundary

| Resource | Created by | Owner | Removal command | Current status |
| --- | --- | --- | --- | --- |
| `~/.codex/live2d-models/<model-id>/source/` | `model import` | live2d-avatar runtime | `model remove <model-id> --yes` | implemented |
| `~/.codex/live2d-models/<model-id>/manifest.json` | `model import` | live2d-avatar runtime | `model remove <model-id> --yes` | implemented |
| `~/.codex/live2d-models/<model-id>/state.json` | `model import` / `state` | live2d-avatar runtime | `model remove <model-id> --yes` | implemented |
| `~/.codex/live2d-models/<model-id>/profile.json` | `model profile apply` | live2d-avatar runtime | `model remove <model-id> --yes` | implemented |
| `<user-selected profile draft or export>.json` | `model profile scaffold` / `model profile export` | user | never removed by this runtime | implemented |
| `<project>/.codex-live2d/installation.json` | `project install` | live2d-avatar runtime | `project uninstall --project <path> --yes` | implemented |
| `<project>/.codex-live2d/RUNTIME-MANIFEST.md` | `project install` | live2d-avatar runtime | `project uninstall --project <path> --yes` | implemented |
| `<project>/.codex-live2d/bundles/<model-id>/` | `project materialize` | live2d-avatar runtime | `project uninstall --project <path> --yes` | implemented |
| `<project>/.codex-live2d/avatar-state-revisions.json` | `project publish` | live2d-avatar runtime | `project uninstall --project <path> --yes` | implemented |
| `<project>/.codex-live2d/live2d_context_hook.py` | `project context-hook enable` | live2d-avatar runtime | `project context-hook disable` / `project uninstall --project <path> --yes` | implemented |
| `<project>/.codex/hooks.json` managed `UserPromptSubmit` entry | `project context-hook enable` | live2d-avatar runtime (entry only; file is user-owned) | `project context-hook disable` / project uninstall | implemented |
| `~/.codex/live2d-avatar-runtime/venv/` | `scripts/install-runtime.ps1` | live2d-avatar runtime | `scripts/uninstall-runtime.ps1 -Yes` | implemented |
| `~/.codex/live2d-avatar-runtime/package/live2d_avatar/` | `scripts/install-runtime.ps1` | live2d-avatar runtime | `scripts/uninstall-runtime.ps1 -Yes` | implemented |
| `~/.codex/live2d-avatar-runtime/installation.json` | `scripts/install-runtime.ps1` | live2d-avatar runtime | `scripts/uninstall-runtime.ps1 -Yes` | implemented |
| `~/.codex/skills/live2d-avatar-controls/` | `scripts/install-skill.ps1` | live2d-avatar runtime | `scripts/uninstall-skill.ps1 -Yes` | implemented |
| `<project>/.codex-voice-avatars/<id>/` | `project materialize` through `avatar.py` | live2d-avatar runtime | `project uninstall --project <path> --yes` | implemented |
| Watcher process | codex-voice bridge | codex-voice | codex-voice uninstall | not created here |
| Local port/socket | no v0.1 bridge listener | none | n/a | not created |
| `.codex-voice/*` artifacts | codex-voice | codex-voice | codex-voice uninstall | not owned here |

## Safety rules

- Imported assets are copied; the original ZIP or folder is never altered or deleted.
- Profile scaffolds and exports are user-owned files. The runtime writes one only to an explicit output path, requires `--force` to replace it, and never removes it during model or project cleanup.
- All destructive commands require `--yes` and refuse paths outside a managed child boundary.
- Project uninstall removes `<project>/.codex-live2d` plus only the verified external avatar bundles recorded in its installation marker; it never removes the global model registry or an unmarked bundle.
- If a materialized bundle is present, project uninstall first checks its `bundle-ownership.json`, deselects it through Codex Voice when active, and then removes only that owned bundle.
- Model removal removes only a validated `<model-id>` child of the registry.
- The runtime currently starts no process, opens no port, and writes no file inside `.codex-voice`.
- The accepted voice-bridge direction is a generic project-local legacy `avatar-state.json` plus routed `avatar-states.json`, both owned by `codex-voice`; this runtime uses a voice-provided writer rather than writing either file directly.
- The optional Codex context hook is a short-lived `UserPromptSubmit` command. It uses only hook cwd/event metadata, derives bounded semantic state through this runtime, and never inspects or forwards a user prompt or transcript.
- Context injection exposes only the selected avatar's semantic action ids, labels, descriptions, current state, and valid choices. It never emits model paths, expression files, hotkeys, parameter ids/values, textures, or compiled operations.
- Context-hook cleanup is JSON-aware: it removes only this runtime's tagged `UserPromptSubmit` handler and generated script, preserving Codex Voice's `Stop` handler and all other user hooks.

## Branch checklist

Before a branch that changes lifecycle behavior is considered complete:

1. Add every generated file, asset download, process, port, service, and external ownership boundary to this table.
2. Add or update the matching `install`, `status`, and `uninstall` behavior.
3. Make the uninstaller validate ownership before removal.
4. Add a test that proves the cleanup boundary.
5. If a skill installation changes, update the skill's own lifecycle reference and reinstall it from the repository source.
