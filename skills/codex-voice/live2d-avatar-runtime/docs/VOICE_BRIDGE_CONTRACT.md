# Voice Bridge Contract

Status: **implemented and jointly validated with the Codex Voice runtime.** The Live2D runtime creates no bridge process, port, watcher, or Voice-owned file. It uses the host-provided CLI and records only its own bundle and project lifecycle artifacts in [RUNTIME-MANIFEST.md](../RUNTIME-MANIFEST.md).

## Responsibility split

| Component | Owns | Must not own |
| --- | --- | --- |
| Live2D runtime | import, model manifest, semantic-profile validation, parameter-operation compilation, persisted state | voice playback or generic Orb lifecycle |
| `codex-voice` | project-local Orb lifecycle, selected-avatar routing, generic state delivery to the renderer | model-specific controls, expression files, or texture details |
| Avatar renderer | loading assets and applying compiled local state | archive extraction, global registry mutation, or agent policy |
| Extension skill | user/agent-facing workflows and safe action selection | private renderer protocols or cleanup guesses |

## State envelopes

The voice layer receives a full, monotonic replacement state, not model-specific commands:

```json
{
  "schema": "codex-ai-presence/avatar-state/v0.1",
  "type": "avatar-state",
  "avatar_id": "example-live2d",
  "source": "live2d-avatar-controls",
  "scope": "project",
  "revision": 12,
  "actions": ["pose.default", "effect.dazed"],
  "issued_at": "2026-07-12T00:00:00Z"
}
```

`actions` is the complete desired active toggle set; an empty array resets the avatar. Revisions increase per project, avatar, and source. The generic envelope contains no model paths, raw controls, or compiled operations.

The legacy v0.1 envelope remains project-wide. A routed v0.2 envelope sets
`scope` to `route` and adds `session_id`, `profile_id`, and the canonical
`route_key`. Codex Voice persists those snapshots in `avatar-states.json` so
two sessions using the same model keep independent complete action sets across
Orb restarts.

## Transport and capability boundary

- Keep UDP `127.0.0.1:17831` for transient activity and audio only.
- `codex-voice` owns `<project>/.codex-voice/avatar-state.json`, routed `avatar-states.json`, and matching route-keyed acceptance diagnostics; it validates state, watches it, and forwards only the exact matching snapshot through `window.orbApi.onAvatarState(callback)`.
- The Live2D runtime invokes a generic Voice writer. It never writes the state file directly or creates a second watcher, port, or daemon.
- A selected renderer bundle advertises `avatar-state-v1` in `avatar.json` and keeps `avatar-capabilities.json` beside it. The sibling file contains actions, safe defaults, renderer settings, and compiled operations for the renderer only.
- The generic host checks the active avatar and capability but does not parse model-local renderer operations. Built-in avatars safely no-op.
- v0.1 remains project-scoped for compatibility; v0.2 is session/profile-route scoped and wins over the project state for its exact window.

## Host writer API

```powershell
py .codex-voice/avatar_state.py write `
  --project-root <project> `
  --avatar-id <id> `
  --source live2d-avatar-controls `
  --scope project `
  --revision <monotonic-project-revision> `
  --actions-json '<full JSON array>'
```

The host owns atomic snapshot replacement, selected-avatar validation, renderer delivery, `sync`, and `status`. The Live2D runtime uses only those commands and records its own generated bundle in its lifecycle manifest.

`project publish` targets `CODEX_THREAD_ID` when that task is bound to the
model. Use `--session-id <id>` to target a different bound session. If the
model is bound to multiple sessions and no exact target can be derived, the
publisher fails closed instead of broadcasting wardrobe state. The explicit
`--project-wide` flag retains the legacy behavior.

## Selection lifecycle

- The Live2D runtime binds a bundle only through Voice's `avatar.py install --use` command; it never writes `avatar-selection.json` itself.
- Voice reads selection and renderer entry when the Orb starts. `project bind` returns `restart_required: true` whenever it materializes, refreshes, or reselects a bundle.
- `project doctor` is read-only. It compares the intended model with the selection marker, bundle manifest, sibling capability file, and Voice status without starting a process or mutating either runtime.

## Activity presentation

Voice sends coarse activity through the existing `audio-event` bridge as `{ "type": "activity", "state": "..." }`. The Live2D renderer may use its model-local `renderer.activity_actions` profile map to derive temporary curated `{ add, suppress }` actions for the current state. It keeps the latest Voice snapshot as the base set, adds local overlay actions, and can temporarily suppress only base-set actions; the overlay is cleared on idle or expiry and never written back. This is presentation-only: it does not extend the avatar-state envelope, alter Voice selection, or cause Voice to parse model data. Unknown future activity states are ignored by the renderer.

## Agent context boundary

Per-turn agent context is Live2D-owned, not Voice-owned. `live2d-avatar project context` projects only semantic action IDs, descriptions, review status, and current renderer/controller state. The optional `UserPromptSubmit` hook emits this bounded developer context; it does not modify `.codex-voice`, inspect a prompt or transcript, or make Voice parse model data.
