# Presence event API

Status: experimental draft, schema `codex-ai-presence/avatar/v0.1`.

This contract separates the presence renderer from the Codex rollout watcher,
Kokoro, and Electron host. It describes the packets the current local Orb
already consumes and gives future custom avatars a stable target. The first
version is deliberately small: it exposes activity and audio features, not
agent text or private execution details.

## Renderer entry point

An avatar is an HTML page loaded into the isolated local Orb window. It may use
the following preload API:

```javascript
const removeAudioListener = window.orbApi.onAudioEvent((event) => {
  // Consume an activity, state, or audio event.
});

const removeMoveListener = window.orbApi.onMoveMode((enabled) => {
  document.body.classList.toggle("move-mode", enabled);
});
```

Listener functions return an unsubscribe callback. The preload exposes no
Node.js integration, filesystem access, network client, or host process API.
The existing move helpers (`setMoveMode`, `dragStart`, `drag`, `dragEnd`, and
`close`) are optional window controls; an avatar may ignore them.

## Event envelope

Every packet is a JSON object with a `type` field. Activity packets include
source, sequence, timestamp, and optional routed session/profile identity:

```json
{
  "type": "activity",
  "state": "thinking",
  "source": "rollout-watcher",
  "sequence": 12,
  "timestamp": "2026-07-12T01:00:00.000Z",
  "ttl_ms": 12000,
  "session_id": "019...",
  "profile_id": "luna",
  "avatar_id": "higan-live2d"
}
```

The activity state vocabulary is:

```text
idle, thinking, tool, skill, cli, waiting, error
```

Transient states expire after `ttl_ms`; a renderer should return to `idle` when
the lease expires or when it receives an explicit idle packet.

When multiple profiles are bound, the Electron host creates one avatar window
per session binding. A `voice-output` ownership packet selects the foreground
route before Kokoro begins:

```json
{
  "type": "voice-output",
  "state": "playing",
  "session_id": "019...",
  "profile_id": "luna",
  "avatar_id": "higan-live2d",
  "route_key": "session:019...|profile:luna",
  "kind": "final"
}
```

Session-scoped activity is delivered only to its matching window. Kokoro's
following `state` and `audio` packets intentionally contain no text and may be
unscoped; the host delivers them only to the current `voice-output` owner, not
to every avatar. A scoped event for an unknown or unbound route is dropped
rather than falling through to the avatar that spoke previously.

Playback lifecycle packets bracket the audio stream:

```json
{ "type": "state", "state": "speaking" }
{ "type": "state", "state": "idle" }
```

Audio packets are scheduled against the actual Kokoro playback clock:

```json
{
  "type": "audio",
  "amplitude": 0.62,
  "rms": 0.11,
  "peak": 0.38,
  "bands": [0.12, 0.21, 0.44, 0.60, 0.31, 0.18, 0.10, 0.08,
            0.06, 0.04, 0.03, 0.02, 0.01, 0.01, 0.00, 0.00]
}
```

`amplitude`, `rms`, `peak`, and every `bands` value are normalized numbers in
the range `0..1`. `bands` currently contains sixteen spectral buckets. A
renderer should tolerate missing or additional buckets and clamp values before
using them.

## Renderer responsibilities

The avatar owns visual interpretation. It may turn the same data into a ring,
character, creature, geometric field, or any other local visual form.

- Use `audio` for speech amplitude and spectral response.
- Use `state:speaking` and `state:idle` for playback lifecycle.
- Use `activity` for semantic state color or geometry.
- Use move-mode notifications to show a drag affordance.
- Smooth and decay values locally; packets are samples, not frame commands.
- Expect the host to budget animation callbacks at 60 FPS by default; lower
  environment overrides remain valid, so use elapsed time rather than assuming
  a fixed update rate.
- Ignore unknown event types and states for forward compatibility.

The renderer must not infer or display hidden reasoning. The current voice
runtime intentionally sends no assistant text, tool names, commands, paths, or
raw tool output to the Orb channel.

## Generic avatar-state bridge

Low-rate avatar actions use a project-local full-state snapshot rather than the
transient UDP audio channel. The Live2D or other avatar-control runtime writes
the snapshot through the managed writer:

```powershell
py .codex-voice/avatar_state.py write --project-root . `
  --avatar-id higan-live2d `
  --source live2d-avatar-controls `
  --scope project `
  --revision 12 `
  --actions-json '["pose.sweater-default", "effect.dazed-eyes"]'
```

The writer validates the selected avatar, requires `avatar-state-v1` in
`avatar.json`, requires the fixed sibling `avatar-capabilities.json`, and
atomically replaces `.codex-voice/avatar-state.json`. It does not inspect or
interpret model-specific parameter operations.

The state envelope is a complete desired action set. An empty `actions` array
returns the renderer to its safe defaults:

```json
{
  "schema": "codex-ai-presence/avatar-state/v0.1",
  "type": "avatar-state",
  "avatar_id": "higan-live2d",
  "source": "live2d-avatar-controls",
  "scope": "project",
  "revision": 12,
  "actions": ["effect.dazed-eyes", "pose.sweater-default"],
  "issued_at": "2026-07-12T09:00:00.000Z"
}
```

The Orb validates the schema, avatar id, capability, action shape, and
monotonic revision before forwarding the complete snapshot through:

```javascript
const removeStateListener = window.orbApi.onAvatarState((state) => {
  // Interpret action ids using the avatar-owned capabilities manifest.
});
```

The host never resolves action ids into model parameters. Use
`avatar_state.py status` to inspect the latest snapshot and host acceptance
diagnostic, or `avatar_state.py sync` to replay the current snapshot after an
Orb restart. State bridge diagnostics are written to
`.codex-voice/avatar-state-status.json`.

## Manifest

Each avatar bundle has an `avatar.json` file conforming to
`avatar-manifest.schema.json`:

```json
{
  "schema": "codex-ai-presence/avatar/v0.1",
  "id": "example-avatar",
  "name": "Example Avatar",
  "version": "0.1.0",
  "entry": "index.html",
  "capabilities": ["activity", "audio", "move-mode", "avatar-state-v1"]
}
```

The entry path is relative to the bundle and must not escape it. Capabilities
are descriptive so the host can provide graceful fallbacks; they are not a
permission to access host resources. An avatar advertising `avatar-state-v1`
must provide the fixed sibling `avatar-capabilities.json`; only the avatar
runtime interprets its model-specific contents.

## Bundle template and rollout status

See `assets/avatar-template/` for a small HTML/CSS/JavaScript renderer that
consumes this contract. The project-local `avatar.py` manager validates and
installs bundles into `.codex-voice-avatars/`, while the Electron host reads
only the active selection marker from the managed runtime. Skill upgrades may
replace the built-in `orb/` files without touching user-owned avatar source.
