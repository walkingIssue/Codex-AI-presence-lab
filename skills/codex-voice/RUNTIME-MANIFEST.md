---
manifest_schema: 1
manifest_revision: 2026-07-12-activity-node
release_unit: codex-voice
---

# Codex AI Presence runtime manifest

This manifest is shipped with every projected `codex-voice` skill revision
and is copied into the project-local `.codex-voice` runtime during setup. It
is the inventory for files owned by the integration, including the managed
Codex hook boundary.

Update this file in the same PR or push whenever a new runtime artifact,
generated directory, managed hook, or cleanup rule is added. The uninstaller
removes the complete `.codex-voice` runtime boundary and removes only the
registered Codex AI Presence hook entry from `.codex/hooks.json`. The manifest
makes that ownership reviewable and remains useful for older installs that
predate a later file.

## Registered runtime artifacts

| Artifact | Project-relative path or pattern | Cleanup owner |
| --- | --- | --- |
| Runtime root | `.codex-voice/` | Uninstaller removes the complete directory |
| Runtime manifest | `.codex-voice/RUNTIME-MANIFEST.md` | Runtime-root cleanup |
| Activity bridge | `.codex-voice/activity.py` | Runtime-root cleanup |
| Voice lifecycle wrapper | `.codex-voice/start_voice.ps1` | Runtime-root cleanup |
| Configuration markers | `.codex-voice/{voice,mode,speed,volume,commentary-volume,provider,progress,enabled,orb.enabled}` | Runtime-root cleanup |
| Session scope | `.codex-voice/sessions.json` | Runtime-root cleanup |
| Kokoro models | `.codex-voice/kokoro-v1.0*.onnx`, `.codex-voice/voices-v1.0.bin` | Runtime-root cleanup |
| Provider patch | `.codex-voice/gpu_patch/` | Runtime-root cleanup |
| Python environments | `.codex-voice/{.venv,.cuda-venv,.dml-venv}/` | Runtime-root cleanup |
| Orb package | `.codex-voice/orb/` and `.codex-voice/orb/node_modules/` | Runtime-root cleanup |
| Orb position | `.codex-voice/orb-position.json` | Runtime-root cleanup |
| Runtime traces | `.codex-voice/*.log`, `.codex-voice/*.pid`, `.codex-voice/*.wav` | Runtime-root cleanup |
| Managed hook | `.codex/hooks/speak.py` | Hook cleanup with ownership check |
| Hook backup | `.codex/hooks/speak.py.codex-voice-backup.py` | Hook cleanup / restore |
| Hook registration | `.codex/hooks.json` managed `Stop` entry only | JSON-aware hook cleanup |

## Revision ledger

| Revision | Runtime change | Cleanup impact |
| --- | --- | --- |
| `2026-07-12-activity-state` | Added rollout activity bridge, Orb activity states, and project-local `activity.py` | All new files remain inside `.codex-voice`; no new external cleanup path |
| `2026-07-12-activity-node` | Added a state-colored center node with a damped activity-swap bounce | No new artifact; renderer update remains inside `.codex-voice/orb/` |
