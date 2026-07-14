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
| `waiting` | Waiting for user, host approval, or another external input | explicit adapter state | Dim steady blue halo | 12 s |
| `error` | A bounded, non-sensitive failure condition | explicit adapter/runtime error | Short red pulse, then idle | 4 s |

The TTL is a lease, not a guaranteed animation duration. Non-idle TTLs are
clamped to `500..30000` ms at the bridge, and the renderer returns to `idle`
when the lease expires. Adapters should refresh long-running work with the
same state and a new sequence/timestamp. `idle` always has `ttl_ms: 0`.

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

`voice-input` is a UI status lane for local capture and delivery. Its states
are not replacements for activity states:

```text
idle -> listening -> transcribing
transcribing -> clipboard-ready
transcribing -> submitting -> target-response
transcribing -> error
```

The current Orb may display `listening`, `transcribing`, `submitting`,
`target-response`, `clipboard-ready`, and `error`. Input errors may share the
string `error` with activity in the UI, but they remain separate events and
must not be interpreted as a host/model failure.

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
- Keep activity, playback, and input status in separate local state.
- Route scoped packets only to the exact matching session/profile window.
- Never infer hidden reasoning or display provider-specific tool details.
- Treat packets as samples and leases, not frame-by-frame animation commands.
