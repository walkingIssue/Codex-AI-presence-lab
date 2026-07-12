---
manifest_schema: 1
manifest_revision: 2026-07-12-avatar-state-bridge
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
| Avatar manager | `.codex-voice/avatar.py` | Runtime-root cleanup |
| Avatar state writer | `.codex-voice/avatar_state.py` | Runtime-root cleanup |
| Avatar selection | `.codex-voice/avatar-selection.json` | Runtime-root cleanup |
| Avatar state snapshot | `.codex-voice/avatar-state.json` | Runtime-root cleanup |
| Avatar state temporary writes | `.codex-voice/.avatar-state.json.*.tmp` | Runtime-root cleanup |
| Avatar state diagnostics | `.codex-voice/avatar-state-status.json` | Runtime-root cleanup |
| Voice lifecycle wrapper | `.codex-voice/start_voice.ps1` | Runtime-root cleanup |
| Configuration markers | `.codex-voice/{voice,mode,speed,volume,commentary-volume,provider,progress,enabled,orb.enabled}` | Runtime-root cleanup |
| Session scope | `.codex-voice/sessions.json` | Runtime-root cleanup |
| Kokoro models | `.codex-voice/kokoro-v1.0*.onnx`, `.codex-voice/voices-v1.0.bin` | Runtime-root cleanup |
| Provider patch | `.codex-voice/gpu_patch/` | Runtime-root cleanup |
| Python environments | `.codex-voice/{.venv,.cuda-venv,.dml-venv}/` | Runtime-root cleanup |
| Orb package | `.codex-voice/orb/` and `.codex-voice/orb/node_modules/` | Runtime-root cleanup |
| Orb position and size | `.codex-voice/orb-position.json` | Runtime-root cleanup |
| Runtime traces | `.codex-voice/*.log`, `.codex-voice/*.pid`, `.codex-voice/*.wav` | Runtime-root cleanup |
| Managed hook | `.codex/hooks/speak.py` | Hook cleanup with ownership check |
| Hook backup | `.codex/hooks/speak.py.codex-voice-backup.py` | Hook cleanup / restore |
| Hook registration | `.codex/hooks.json` managed `Stop` entry only | JSON-aware hook cleanup |
| User avatar source | `.codex-voice-avatars/<avatar-id>/` | User-owned; not removed by skill uninstall |

## Revision ledger

| Revision | Runtime change | Cleanup impact |
| --- | --- | --- |
| `2026-07-12-activity-state` | Added rollout activity bridge, Orb activity states, and project-local `activity.py` | All new files remain inside `.codex-voice`; no new external cleanup path |
| `2026-07-12-activity-node` | Added a state-colored center node with a damped activity-swap bounce | No new artifact; renderer update remains inside `.codex-voice/orb/` |
| `2026-07-12-avatar-loader` | Added project-owned avatar bundle selection and a validated runtime loader | Selection marker is cleaned with the runtime; avatar source remains user-owned |
| `2026-07-12-avatar-state-bridge` | Added the model-agnostic full-state writer, Orb file watcher, preload delivery, and acceptance diagnostics | State writer, snapshot, temporary writes, and status file remain inside `.codex-voice` and are removed with the runtime |
| `2026-07-12-resizable-orb-window` | Added a persistent native resize gesture and host scaling for custom renderers | Size remains in the existing owned `orb-position.json`; no new cleanup boundary |
| `2026-07-12-avatar-local-resize` | Kept browser zoom at 1 for `avatar-state-v1` renderers and forwarded exact content dimensions for renderer-local fitting | No new artifact; avatar canvas sizing remains renderer-owned |
