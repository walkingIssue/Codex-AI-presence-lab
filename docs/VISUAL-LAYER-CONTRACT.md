# Visual layer contract

Status: experimental but implemented locally. This is the contract between a
Codex/host adapter, the local Presence Service, and the Orb or another
renderer. The renderer receives category and playback signals; it does not
receive transcripts, hidden reasoning, tool names, commands, paths, or raw
provider payloads.

## Three separate signal lanes

The visual layer has three independent lanes:

| Lane | Event types | Purpose |
| --- | --- | --- |
| Activity | `activity` | Why the host is busy: thinking, tool work, waiting, or error |
| Presentation | `presence/presentation-cue/v0.1` or `v0.2` | A binding-scoped semantic gesture; v0.2 may be held until explicitly cleared |
| Playback | `state`, `audio`, `voice-output` | Whether speech is playing, its measured audio features, and the session that owns it |
| Input status | `voice-input` | Local microphone/STT/delivery status; not model activity |

An activity state may remain active while no audio is playing. Conversely,
speech may play while activity is `thinking`, `tool`, or `idle`. The renderer
must not merge these lanes into one state variable.

## Canonical activity states

`activity.state` is a closed vocabulary:

| State | Meaning | Typical source lifecycle | Orb direction | Default TTL |
| --- | --- | --- | --- | ---: |
| `idle` | No active host work | turn/session complete, explicit reset | Calm cyan baseline | none |
| `thinking` | Model reasoning or preparing the next visible action | reasoning, post-tool result, skill completion | Slow violet/indigo breathing | 12 s |
| `tool` | External or provider-mediated tool work | web search, function/tool call, MCP invocation | Amber/gold pulse | 8 s |
| `skill` | Named skill/integration lifecycle is active | skill start/invocation | Magenta/deep-violet pulse | 12 s |
| `cli` | Local command or patch execution is active | shell, terminal, command execution, patch apply | Green/teal pulse | 8 s |
| `waiting` | Waiting for user, host approval, or another external input | correlated lifecycle open/close | Dim steady blue halo | until cleared when correlated; 12 s fallback |
| `error` | A bounded, non-sensitive failure condition | explicit adapter/runtime error | Short red pulse, then idle | 4 s |

For transient activity, the TTL is a lease, not an animation duration. Non-idle TTLs are
clamped to `500..30000` ms at the bridge, and the renderer returns to `idle`
when the lease expires. Adapters should refresh long-running work with the
same state and a new sequence/timestamp. `idle` always has `ttl_ms: 0`. A
correlated waiting lifecycle has no TTL and remains authoritative until its
opaque lifecycle id is closed or the adapter explicitly resets its turn/session.

### Durable waiting lifecycle

Adapters that can correlate an external-input boundary use connection-bound
`activity/open`, `activity/close`, and `activity/reset` messages. `open` carries
only `state: "waiting"`, an event id, and an opaque lifecycle id. `close`
identifies that exact lifecycle and names the next coarse state. `reset` closes
every lifecycle owned by that adapter on the binding for a sanitized terminal
reason such as `turn-ended`, `cancelled`, or `error`.

Lifecycle ids are derived from session and upstream call identity. Prompt text,
choices, commands, arguments, tool output, paths, and other provider payloads
must never enter the Presence Runtime. Multiple open lifecycles are reference
counted: waiting remains effective until the last one closes. Ordinary transient
activity cannot displace an open waiting state.

## Semantic presentation contract

Activity telemetry and semantic gestures are intentionally separate. A
non-idle activity can remain visible in the halo/status lane after its finite
pose has returned to the persistent profile default. Effective configuration
snapshots contain only persistent actions; they are never republished with the
same revision merely to carry an activity overlay.

The Python runtime deterministically resolves transient activity into an internal
`presence/presentation-cue/v0.1`. A durable waiting lifecycle resolves to
`presence/presentation-cue/v0.2`, which adds `lifetime: "until-cleared"` and an
opaque lifecycle id. Both cue versions name their binding, configuration
revision, monotonic per-binding presentation sequence, source event, persistent
base actions, resolved target actions, entry/exit easing, and minimum visible
lifetime. The renderer validates that the cue base matches its acknowledged
snapshot and acknowledges completion with the same binding, revision, and
sequence. A held cue enters normally, stays at its target, and eases back to the
persistent base only after explicit cancellation.

Each binding has one active cue and one latest-pending cue. A transient active cue is
not interrupted before its minimum lifetime; newer pending states replace
obsolete pending states. `idle` clears pending work but lets the active cue
finish its bounded exit. A profile/avatar revision, dormant binding, removal,
or shutdown cancels renderer-local presentation state and restores the persistent
base. Transient cues are ephemeral and are not replayed after restart. Open
durable lifecycles are persisted centrally, and restart reconstructs a fresh cue
against the acknowledged snapshot rather than replaying stale rendered actions.

### Tool and MCP normalization

There is intentionally no public renderer state named `mcp-invocation` or
`tool-invocation`. Both are source-level lifecycle concepts rendered as
`state: "tool"` so custom renderers do not need provider-specific
vocabularies.

The current rollout classifier maps these records to `tool`:

- MCP invocation start and end;
- web-search start and end;
- non-local function, custom-tool, and web-search calls.

Local shell/terminal commands and patch application map to `cli`. A tool
completion currently remains in `tool` until the next lifecycle event or lease
expiry; adapters that know work has returned to model processing should emit
`thinking` explicitly.

`error` is an explicit category. Error details are diagnostics for the local
runtime only and must not be included in the renderer packet.

## Activity packet

The renderer-facing packet is JSON:

```json
{
  "type": "activity",
  "state": "tool",
  "source": "codex-rollout",
  "sequence": 13,
  "timestamp": "2026-07-14T12:00:01.000Z",
  "ttl_ms": 8000,
  "session_id": "session-id",
  "profile_id": "default",
  "avatar_id": "builtin",
  "route_key": "session:session-id|profile:default"
}
```

`source`, identity fields, and route fields are bounded routing metadata. The
packet must not contain `text`, `delta`, `reasoning`, `tool_name`, `command`,
`arguments`, `path`, `secret`, or a raw upstream event.

The packet sequence is monotonic per emitter. A scoped packet with an unknown
session/profile route is dropped; it must never fall through to another
session's avatar. An unscoped activity packet may be shown by every configured
avatar window. Audio and playback state use the foreground voice-output
owner instead.

## Playback contract

Speech lifecycle is separate from activity:

```json
{ "type": "voice-output", "state": "playing", "session_id": "session-id", "route_key": "session:session-id|profile:default", "kind": "final" }
{ "type": "state", "state": "speaking" }
{ "type": "audio", "amplitude": 0.62, "rms": 0.11, "peak": 0.38, "bands": [0.12, 0.21, 0.44] }
{ "type": "state", "state": "idle" }
```

`voice-output` selects the attention owner. `state:speaking` brackets the
actual playback clock. `audio` carries normalized `0..1` samples; renderers
must tolerate missing or additional spectral bands. `audio` and unscoped
`state` packets are delivered only to the current foreground owner when
multiple avatar windows exist.

While speech is active, the Orb gives measured audio motion visual priority:
activity tint and geometry remain available but are suppressed proportionally
to speaking intensity. This prevents a tool pulse from fighting the waveform.

## Input status contract

`input-state` is an internal, binding-scoped UI lane for local capture,
inference, and delivery. Its states are not replacements for activity states:

```text
idle -> recording -> transcribing -> ready -> delivered -> idle
recording | transcribing | ready -> failed -> idle
recording -> cancelled -> idle
```

Every transition carries the stable binding id and capture id. Terminal events
remove only that capture, so a slow completion cannot overwrite a newer
recording on the same binding. `ready` means inference succeeded and delivery
is pending; `delivered` means the adapter acknowledged clipboard copy or
app-server submission. Terminal feedback is ephemeral and is not replayed
after runtime restart.

The central renderer host owns the input overlay for both built-in and Live2D
windows. Targeting/move uses a cyan edge, resize uses an amber edge with corner
markers, recording uses a red pulse, transcription uses a violet pulse,
delivery-ready uses amber, successful delivery flashes green, and failure
flashes red. These effects never alter semantic model parameters.

## Transition guidance

The normal activity flow is:

```text
idle -> thinking
thinking -> tool | skill | cli | waiting | idle | error
tool -> thinking | error
skill -> thinking | error
cli -> thinking | error
waiting -> thinking | idle
error -> idle
```

`speaking` is intentionally absent from this activity diagram: it is the
playback lane's `state` event, not a value in `activity.state`. Activity may
continue underneath it and resumes visual prominence when playback returns to
idle.

## Renderer requirements

- Accept the closed activity vocabulary and ignore unknown future event types.
- Clamp numeric audio values and tolerate missing/extra bands.
- Expire non-idle activity leases safely to `idle`.
- Never expire an `until-cleared` presentation; hold it until exact cancellation.
- Keep activity, presentation, playback, and input status in separate local state.
- Acknowledge presentation completion only after the persistent semantic base
  has been reapplied.
- Route scoped packets only to the exact matching session/profile window.
- Never infer hidden reasoning or display provider-specific tool details.
- Treat packets as samples and leases, not frame-by-frame animation commands.
