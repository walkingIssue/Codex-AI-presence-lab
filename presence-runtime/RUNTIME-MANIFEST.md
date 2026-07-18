# Presence Runtime Manifest

> Generated from `presence-runtime/runtime-manifest.json`; do not edit by hand.

Schema: `presence/runtime-manifest/v0.2`
Revision: `1`
Release unit: `codex-voice`

## Preserved user data

- `$CODEX_HOME/presence/catalog/`
- `$CODEX_HOME/presence/state.sqlite3`
- `original avatar archives and profile/preset export files`

## `supervisor`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/runtime/`, `$CODEX_HOME/bin/presence*`
- Dependencies: `python-runtime`, `state-store`, `catalog`
- Dependents: `adapter-registration`, `kokoro-worker`, `renderer-host`, `stt-runtime`
- Preserved data: `state-store`, `catalog`
- Removal: Stop the runtime after active-source guard, then remove only manifest-owned runtime code and launchers.

## `python-runtime`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/.venv/`
- Dependencies: none
- Dependents: `supervisor`, `adapter-registration`, `kokoro-worker`, `stt-runtime`
- Preserved data: none
- Removal: Remove only the managed virtual environment after dependents stop.

## `state-store`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/state.sqlite3`, `$CODEX_HOME/presence/state.sqlite3-wal`, `$CODEX_HOME/presence/state.sqlite3-shm`
- Dependencies: none
- Dependents: `supervisor`, `adapter-registration`, `speech-queue`, `renderer-host`, `migration-ledger`
- Preserved data: `project instances`, `bindings`, `overrides`, `effective snapshots`, `geometry`, `queue`, `migration ledger`
- Removal: Preserve by default; remove only with --purge-state after active-source guard and backup/export checks.

## `catalog`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/catalog/`
- Dependencies: none
- Dependents: `resolver`, `renderer-host`, `bindings`
- Preserved data: `avatar model packs`, `profile revisions`, `preset revisions`
- Removal: Preserve by default; remove only with --purge-catalog after reference checks. Never remove original user archives.

## `resolver`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/runtime/presence_runtime/resolver.py`, `$CODEX_HOME/presence/runtime/schemas/`
- Dependencies: `catalog`, `state-store`
- Dependents: `speech-queue`, `renderer-host`, `public-cli`
- Preserved data: `last-known-good effective snapshots`
- Removal: Removed with supervisor code; persisted snapshots remain unless --purge-state is explicit.

## `adapter-registration`

- Scope: `project`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/adapters/`, `project .codex-voice/v0.2 diagnostics and rollout cursors`, `$CODEX_HOME/presence/presence.pipe or presence.sock`
- Dependencies: `supervisor`, `python-runtime`, `state-store`
- Dependents: `bindings`, `speech-queue`, `renderer-host`
- Preserved data: `dormant binding configuration`
- Removal: Project unregister stops its thin rollout source and removes only the manifest-owned .codex-voice/v0.2 files; shared runtime, legacy rollback inputs, and catalog remain.

## `bindings`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `state-store binding records`
- Dependencies: `state-store`, `catalog`
- Dependents: `speech-queue`, `renderer-host`
- Preserved data: `project defaults`, `session overrides`, `geometry`, `last-known-good revisions`
- Removal: Deleting a binding explicitly cancels its queued speech and releases catalog references; never reroute dependents.

## `kokoro-worker`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/models/`, `$CODEX_HOME/presence/providers/`, `$CODEX_HOME/presence/kokoro-worker.*`
- Dependencies: `supervisor`, `python-runtime`
- Dependents: `speech-queue`
- Preserved data: none
- Removal: Stop one worker, then remove only managed provider/model assets after all active sources are gone.

## `speech-queue`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `state-store speech_queue and event_dedup records`
- Dependencies: `state-store`, `bindings`, `resolver`, `kokoro-worker`
- Dependents: `renderer-host`
- Preserved data: `durable final utterances`
- Removal: Drain or explicitly cancel by binding; purge only with state-store purge.

## `renderer-host`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/renderer/`, `$CODEX_HOME/presence/renderer.*`, `one Electron root and child windows`
- Dependencies: `supervisor`, `bindings`, `resolver`, `catalog`, `electron-runtime`
- Dependents: none
- Preserved data: `binding geometry and last-known-good renderer revision in state-store`
- Removal: Terminate the Electron root and all children before removing managed renderer code; preserve geometry unless --purge-state.

## `electron-runtime`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/renderer/node_modules/`
- Dependencies: none
- Dependents: `renderer-host`
- Preserved data: none
- Removal: Remove after renderer-host termination and ownership verification.

## `stt-runtime`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/presence/stt/`, `$CODEX_HOME/presence/stt-models/`
- Dependencies: `supervisor`, `python-runtime`
- Dependents: none
- Preserved data: `explicit machine input permission`
- Removal: Stop capture, revoke managed gesture registration, and remove managed STT code/models; never alter OS microphone policy.

## `public-cli`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `$CODEX_HOME/bin/presence`, `$CODEX_HOME/bin/presence.cmd`, `$CODEX_HOME/bin/presence.ps1`
- Dependencies: `supervisor`, `resolver`
- Dependents: `compatibility-wrappers`
- Preserved data: none
- Removal: Remove only launchers whose managed hash matches the manifest.

## `compatibility-wrappers`

- Scope: `project`
- Owner: `codex-voice-skill`
- Artifacts: `toggle.py`, `profiles.py`, `avatar tools`, `Live2D launchers`
- Dependencies: `public-cli`
- Dependents: none
- Preserved data: `read-only v0.1 rollback inputs during the compatibility release`
- Removal: Project unregister removes managed wrappers only; compatibility-release retirement requires successful migration ledger and backup/export check.

## `migration-ledger`

- Scope: `user`
- Owner: `presence-runtime`
- Artifacts: `state-store migration_ledger records`, `project migration lock`
- Dependencies: `state-store`, `supervisor`
- Dependents: `compatibility-wrappers`
- Preserved data: `v0.1 source hashes`, `migration status`, `rollback metadata`
- Removal: Preserve for the compatibility release; purge only with explicit state purge after export verification.
