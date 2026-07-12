# ACP adapter plan

Status: planning branch, lab-only.

This branch marks the transition from a Codex-specific streaming experiment to
a host-neutral Presence Client. The installable skill remains valuable as the
runtime and configuration surface, but new host integration should move behind
an adapter boundary.

## Decision

Use the Agent Client Protocol (ACP) as the first external integration boundary.
Keep Codex as the first backend through its ACP adapter, then validate the same
Presence Client against at least one independent ACP agent before considering a
fork of any host application.

ACP is appropriate because it provides bidirectional JSON-RPC communication,
real-time session updates, and permission requests between an agent and a
client. The Presence Client should consume the events an agent intentionally
emits; it must never attempt to recover hidden reasoning or scrape a private
GUI.

References:

- [ACP introduction](https://agentclientprotocol.com/get-started/introduction)
- [ACP architecture](https://agentclientprotocol.com/get-started/architecture)
- [Codex ACP adapter](https://github.com/agentclientprotocol/codex-acp)

## Target architecture

```text
        Codex / OpenCode / Goose / Cline
                       |
                       | ACP JSON-RPC over stdio first
                       v
              Presence Client / ACP adapter
                 |                    |
                 |                    +--> host UI and approvals
                 v
          Host-neutral PresenceEvent bus
                 |                    |
                 v                    v
          Kokoro voice worker     Orb or custom avatar
```

The first implementation should be a Presence Client that launches an ACP
agent and owns the local UI. A transparent ACP relay for existing editor clients
can follow later, but it is not the first milestone because ACP has
bidirectional requests and the relay must preserve approvals, cancellation,
session lifecycle, and backpressure correctly.

## Work packages

### 1. Freeze the skill boundary

- Keep project/session configuration, Kokoro provider selection, lifecycle
  cleanup, and the existing Orb runtime in `skills/codex-voice`.
- Treat the current app-server bridge as a preserved experiment, not as the
  default skill path.
- Keep runtime ownership in `RUNTIME-MANIFEST.md` for every new artifact.
- Do not add ACP dependencies to the normal Codex hook path.

### 2. Stabilize the PresenceEvent API

Use [`PRESENCE-EVENT-API.md`](PRESENCE-EVENT-API.md) as the draft internal
contract. The API must carry source, project, session, ordering, lifecycle,
activity, speech timing, and safe visible text metadata without coupling a
renderer to Codex, Kokoro, Electron, or a specific avatar.

Required properties:

- monotonic sequence numbers for de-duplication;
- project and session identity on every event;
- explicit event visibility and redaction rules;
- bounded activity TTLs so a crashed adapter returns to idle;
- optional capabilities so a renderer can degrade gracefully;
- a versioned schema with additive extension points.

### 3. Implement ACP ingress

Build a small adapter that:

1. starts an ACP agent subprocess;
2. performs initialization and capability negotiation;
3. maps ACP session updates to `PresenceEvent` values;
4. forwards user prompts and cancellation;
5. surfaces permission and question requests to the client UI;
6. preserves session IDs and project roots;
7. shuts down the child process and workers deterministically.

Codex is the first adapter target. The adapter must depend on ACP messages and
capabilities, not on Codex rollout JSONL or desktop process paths.

### 4. Extract the runtime consumer

Move the shared voice/visual consumer behind a small interface:

```text
PresenceEvent -> activity state machine
PresenceEvent(message.delta) -> Kokoro text queue
PresenceEvent(speech.envelope) -> renderer frame input
```

The current Strand Orb becomes one renderer. A custom renderer should be able
to subscribe to the same events and provide its own HTML, Canvas, WebGL, SVG,
or native implementation without modifying the Kokoro worker or ACP adapter.

### 5. Make custom avatars a supported extension

Do not force users to fork the Orb. Define a renderer package contract with:

- a manifest containing name, version, supported event capabilities, and entry
  point;
- a renderer lifecycle (`start`, `on_event`, `stop`);
- a theme/configuration object for colors, geometry, motion, and audio response;
- a safe default for unknown activity states;
- no access to secrets, raw command arguments, file paths, or hidden reasoning.

The first release can accept user-authored renderer code directly. A declarative
avatar format should be evaluated only after the event contract has survived
multiple renderers.

### 6. Port the platform runtime

Linux support should be implemented around a platform module rather than copied
PowerShell logic:

- Python process and path primitives are the source of truth;
- Bash scripts are thin convenience wrappers;
- use `bin/python` and POSIX process groups when creating/stopping runtimes;
- make audio backend selection explicit;
- validate Electron on X11 and Wayland separately;
- persist and recover Orb placement without assuming Windows work-area APIs;
- validate CPU first, then optional NVIDIA CUDA.

DirectML remains a Windows/Intel-specific provider path.

## Validation sequence

1. Unit-test the event envelope, ordering, TTL, redaction, and session
   isolation rules.
2. Run one Codex ACP session through a local Presence Client.
3. Confirm streamed visible text reaches Kokoro before turn completion.
4. Confirm tool/permission/session updates reach the Orb without exposing raw
   tool details.
5. Drive the same renderer with a synthetic event fixture.
6. Run the same client against an independent ACP agent.
7. Run the Linux CPU smoke path.
8. Run the full E2E and release projection gate.

Promotion requires the skill path and the ACP client path to be independently
safe. A green ACP demo must not automatically promote experimental host code
into the installable skill.

## Decision gates

- If Codex, OpenCode, and Goose can all provide the required visible events
  through ACP, keep the Presence Client thin and do not fork a host.
- If a backend lacks a required event, add a backend capability fallback before
  adding a host-specific workaround.
- Fork a host only if its server/client boundary is the smallest way to expose
  a required capability and the fork has an explicit maintenance owner.

## Explicit non-goals

- patching or replacing the closed-source Codex Desktop executable;
- speaking hidden chain-of-thought or raw tool payloads;
- making the existing Codex skill depend on a new GUI;
- committing model weights, voice bundles, or user runtime state;
- designing the final declarative avatar file format before the event API is
  proven in real clients.
