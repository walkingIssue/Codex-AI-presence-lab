# Presence event API

Status: draft internal contract, lab-only, schema `codex-ai-presence/v0.1`.

This is the boundary between an agent host and a voice/visual presence. It is
intentionally smaller than any one agent protocol. Codex, OpenCode, Goose,
Cline, or another host can translate its own events into this shape.

## Envelope

Every event uses the following envelope:

```json
{
  "schema": "codex-ai-presence/v0.1",
  "eventId": "evt_01J...",
  "sequence": 42,
  "timestampMs": 1730831111123,
  "source": {
    "host": "codex",
    "adapter": "codex-acp",
    "projectId": "project_sha256:...",
    "sessionId": "session_..."
  },
  "kind": "activity.changed",
  "visibility": "metadata",
  "payload": {}
}
```

`sequence` is monotonic within a source/session. `timestampMs` is wall-clock
metadata for logs; consumers should use arrival order and local monotonic time
for animation. Consumers must ignore events from a different project or
session unless explicitly configured for aggregation.

## Event kinds

### Lifecycle

```json
{
  "kind": "session.started",
  "visibility": "metadata",
  "payload": { "projectRoot": "..." }
}
```

Supported lifecycle kinds are `session.started`, `session.idle`,
`session.ended`, and `session.error`.

### Activity

```json
{
  "kind": "activity.changed",
  "visibility": "metadata",
  "payload": {
    "state": "tool",
    "ttlMs": 5000
  }
}
```

The initial state vocabulary is:

```text
idle, thinking, tool, skill, cli, waiting, error
```

`state` is a category, not a tool name. `ttlMs` is required for transient
states. If it expires without a refresh, the consumer returns to `idle`.

### Visible message streaming

```json
{
  "kind": "message.delta",
  "visibility": "visible",
  "payload": {
    "messageId": "msg_...",
    "text": "The next sentence",
    "role": "assistant"
  }
}
```

`message.completed` closes the visible message. Text is available to a voice
consumer, but a renderer does not need to display it.

### Speech timing and audio features

```json
{
  "kind": "speech.envelope",
  "visibility": "metadata",
  "payload": {
    "audioTimeMs": 1840,
    "durationMs": 40,
    "rms": 0.42,
    "peak": 0.71,
    "cadence": 0.63,
    "onset": 0.18
  }
}
```

The default renderer receives numeric features rather than raw PCM. This
keeps the visual API independent from the audio backend while still allowing
geometry to follow volume, cadence, and onsets. A future capability may expose
an audio stream, but raw audio is not part of the base contract.

`speech.started` and `speech.ended` bracket the envelope stream. Speech has
visual priority over activity coloration, but activity remains available for
renderers that want layered behavior.

### Permissions and questions

```json
{
  "kind": "permission.requested",
  "visibility": "metadata",
  "payload": {
    "requestId": "req_...",
    "category": "command"
  }
}
```

Only a safe category is emitted by default. Commands, arguments, paths, tool
names, and raw responses stay in the host client unless a future explicit
debugging mode is enabled by the user.

## Renderer contract

A renderer consumes events, not agent-specific messages:

```text
start(capabilities, theme)
on_event(PresenceEvent)
stop()
```

The renderer may implement any visual representation: the current Strand Orb,
a different procedural form, a cartoon avatar, an anime-styled character, or a
native desktop companion. The voice worker and host adapter must not know which
renderer is selected.

Recommended renderer capabilities:

```text
activity
speech_envelope
cadence
message_visibility
permission_state
```

Unknown event kinds and states must be ignored safely. A renderer that does not
support `speech_envelope` falls back to `speech.started`/`speech.ended`.

## Safety and privacy rules

- Never emit hidden reasoning as `message.delta` or `activity.changed`.
- Never emit raw tool payloads, command arguments, file paths, or secrets.
- Keep project and session identity on every event.
- Drop duplicate or out-of-order events using `eventId` and `sequence`.
- Expire busy states after a bounded timeout.
- Keep model weights, audio buffers, and runtime state outside source control.

## Transport mapping

The first transport is newline-delimited JSON over a local process boundary.
An ACP adapter maps ACP session notifications and permission requests into this
event API. A later client may use a local socket or another transport without
changing renderer code.

This contract is deliberately an internal draft. It should be tested with a
Codex adapter and one independent ACP agent before it becomes a public stable
API or a declarative avatar file format.
