# Presence Runtime, Voice Input, and Host Adapter Roadmap

**Status:** planning baseline; no ACP adapter or packaged runtime is promoted
**Date:** 2026-07-13
**Scope:** `codex-voice`, the local Presence Runtime, host adapters, and the
optional `live2d-avatar-controls` renderer integration

This document consolidates the current direction after the ACP feasibility
pass and the unfinished voice-input experiments. It belongs to the lab and is
not part of the installable skill.

## Product decision

Keep `codex-voice` as an installable skill, but move long-lived ownership into
a user-level Presence Runtime.

The skill remains the agent-facing control plane. It installs, updates,
configures, diagnoses, registers, and uninstalls the runtime. The runtime owns
the persistent Kokoro worker, playback arbitration, inbox, session registry,
renderer lifecycle, and local control surface.

This is a packaged runtime, not necessarily one monolithic executable. The
Windows target may be a tray/controller executable supervising the existing
Python Kokoro worker and Electron renderer as child processes. That preserves
the validated inference paths without forcing a rewrite of Kokoro in Node or
Electron.

The Live2D runtime remains a renderer/control extension. It does not own
global playback, session focus, inbox ordering, or process shutdown.

The authoritative source, branch, artifact, and ownership policy for this
packaged runtime is in
[`PACKAGING-AND-REPOSITORY-STRATEGY.md`](PACKAGING-AND-REPOSITORY-STRATEGY.md).

## What ACP is and is not

ACP is an optional host adapter transport. It is not a magic shared provider
daemon and it is not the Presence Runtime itself.

An ACP-capable editor commonly launches an ACP agent process over stdio. A
different editor or thread may launch a different process. The protocol can
support multiple sessions on one connection, but it does not require all
provider sessions on a machine to share one background server.

Therefore:

- do not build the product around attaching to an arbitrary existing ACP PID;
- do not assume Claude, Codex, or another provider exposes one reusable local
  ACP server;
- use a relay/wrapper when a GUI lets us configure the ACP command;
- keep non-ACP integrations such as the current Codex watcher as first-class
  adapters;
- aggregate all adapters into one local Presence Runtime for arbitration.

Target topology:

```text
Codex GUI watcher  ─┐
Codex TUI bridge    ─┤
ACP relay(s)        ─┤──> Presence Runtime
Future adapters     ─┘          │
                       session registry + attention arbiter
                                  │
                 Kokoro + inbox + Orb/Live2D renderer
```

The runtime is transport-neutral. ACP is one ingress path alongside Codex
rollout/app-server, TUI, and future adapters.

## Spatial multi-display presence

The original product direction treats each agent/session as an identity bundle:
session or project identity, voice, avatar, behavior policy, and attention
priority. A future desktop can make that identity spatial without becoming a
replacement coding GUI.

The runtime should manage a virtual desktop coordinate space, but should not
use one enormous transparent window across every monitor. Each presence should
get its own small transparent renderer window, supervised by the same runtime.
This avoids unnecessary GPU surface size, per-monitor DPI problems, and fragile
click-through/z-order behavior.

An identity profile may eventually declare:

```json
{
  "home_display": "display-2",
  "attention_display": "primary",
  "home_position": "bottom-right",
  "attention_behavior": "walk-wave",
  "return_behavior": "walk-home",
  "attention_priority": "normal"
}
```

Avatar capabilities remain declarative and bounded:

```text
can_move_between_displays
can_wave
can_knock
can_request_attention
```

The avatar may provide the animation, but the Presence Runtime decides whether
movement, focus, sound, or foreground attention is allowed. A waiting agent
may request attention; it must not steal focus or move arbitrarily.

Target flow:

```text
background agent reaches waiting state
    -> attention request enters the arbiter
    -> avatar moves to the configured main display
    -> avatar performs a bounded attention animation
    -> user acknowledges or dismisses it
    -> avatar returns to its home position
```

Only one presence owns the foreground attention channel at a time. Other
agents remain visible in their home locations with low-energy background state.

## Context surfacing

The companion should eventually be able to surface the relevant work context
without becoming an editor of its own. This is a runtime capability with a
thin optional skill wrapper, not a renderer-specific feature.

The agent can request a bounded context action:

```json
{
  "type": "surface-context",
  "session_id": "...",
  "target": {
    "kind": "file",
    "path": "src/main.py",
    "line": 142
  },
  "mode": "focus"
}
```

Supported modes should remain explicit:

- `open-context`: show the target without stealing focus;
- `focus-context`: intentionally foreground the configured editor;
- `preview-context`: show a temporary Presence context card;
- `return-to-agent`: return to the originating agent surface.

The runtime resolves the request through editor/window adapters. If no
compatible application is registered, it falls back to a local context card.
It must not allow an agent to manipulate arbitrary windows or silently steal
focus.

The optional skill should expose semantic actions such as `show file`,
`show diff`, `show terminal`, and `return to session`; the runtime owns the
platform-specific window operations.

## Current known unfinished behavior

These are the immediate bugs to repair before widening the host surface.

### 1. Stateful session identity is incomplete

Session names were experimented with as per-message prefixes and then disabled
because the result was noisy. The desired behavior is a stateful identity
transition:

- announce or display a session when it becomes foreground/selected;
- announce it once at the start of a new active turn when appropriate;
- do not prepend the name to every commentary or final item;
- never modify the Codex prompt or stored assistant text;
- preserve project and session as separate identifiers.

Required contract:

```json
{
  "type": "session-identity",
  "source_id": "codex-tui-1",
  "provider": "codex",
  "project_root": "...",
  "session_id": "...",
  "session_label": "Orb renderer redesign",
  "revision": 3,
  "foreground": true,
  "announce": true
}
```

The runtime, not the watcher, decides whether an announcement is needed.

### 2. Voice capture reaches the microphone but does not complete reliably

The current path appears to detect the recording gesture, but the failure is
somewhere after capture: recording handoff, local STT, or submission. The
implementation must split those phases and expose a fixed state/error code for
each one:

```text
idle -> listening -> recording-ready -> transcribing -> submitting
      -> target-response | error
```

The intended gesture is fixed:

- hold `Ctrl+Alt` and press the right mouse button;
- begin recording immediately;
- ignore pointer movement while recording;
- release the right button or either modifier to stop the chunk;
- repeated chunks append to the same pending user input;
- `Escape` cancels and deletes temporary audio;
- capture remains opt-in.

The first debugging pass must prove, independently:

1. the Orb emits `capture-start` and `capture-stop` exactly once;
2. the host creates a valid temporary recording;
3. the STT process receives that exact file and returns text;
4. the delivery adapter receives the transcript and target session ID;
5. the raw recording is deleted after success, cancellation, timeout, or
   restart.

### 3. TTS does not pause/requeue cleanly on capture

Capture must interrupt the current item at the arbiter, not merely send a
second command to the worker. The arbiter must be the only component allowed
to start, stop, requeue, resume, or complete speech.

Required behavior:

```text
speaking
  -> interrupted-for-input
  -> listening/transcribing/submitting
  -> target-response
  -> resume-interrupted-item
  -> drain-queued-items
```

The interrupted item must resume from a recorded text/chunk cursor or from a
deterministic requeue boundary. It must never replay the completed portion,
play the tail before the new response, or be duplicated after a watcher
restart.

### 4. Inbox state becomes haunted after interruption

The durable inbox currently contains the right concepts—deduplication,
requeue, focus state, recovery, and fair draining—but the live behavior needs
an explicit trace for every transition.

The repair must verify:

- one event ID produces one spoken item;
- interrupted items have one and only one replay;
- stale `focus`, `input`, and `in-flight` state is recovered on startup;
- target-session output is locked until its response completes;
- other sessions queue without stealing playback;
- the interrupted tail is drained before unrelated queued sessions;
- a failed STT or delivery attempt remains retryable without replaying TTS;
- a restart cannot resurrect an already completed item.

The database should retain compact lifecycle diagnostics, but never raw audio,
hidden reasoning, secrets, or arbitrary tool payloads.

### 5. Runtime lifecycle and desktop integration are incomplete

The Orb is currently project-launched and taskbar-hidden. It has no proper
tray-owned shutdown path, so stopping the Codex session can leave a renderer or
worker orphaned until Task Manager is used.

Known desktop issue to track separately: the Orb can fall behind a normal
application despite its current always-on-top configuration. The fix must be
verified on the real Windows desktop and must not simply increase z-order until
the window becomes intrusive.

## Runtime architecture

### Presence Runtime responsibilities

- single-instance user-level supervisor;
- local IPC endpoint: Windows named pipe first, Unix socket on Linux;
- session/source registry with stale-registration expiry;
- normalized event ingress;
- stateful foreground/session identity;
- one attention and playback arbiter;
- Kokoro worker supervision;
- inbox and interruption recovery;
- Orb/Live2D renderer supervision;
- tray menu and explicit stop/restart/status controls;
- startup/shutdown and uninstall cleanup.

The runtime should remain local-only by default. No public TCP listener is
needed for the first version.

### Skill responsibilities

`codex-voice` remains responsible for:

- installing or updating the runtime;
- creating the isolated provider/STT environments;
- selecting CPU, NVIDIA CUDA, or Intel DirectML;
- configuring voice, playback, volume, commentary, progress, and renderer;
- registering the current project/session source;
- starting, stopping, diagnosing, and uninstalling the runtime;
- installing host-specific adapter recipes.

The skill rollout should tell an installing agent to discover the local runtime,
register the current project/session, and report the resulting source ID. It
must not guess by scraping arbitrary process names.

### Tray/control surface

The runtime should expose a small native tray item, not a new coding GUI.

Minimum menu:

- active sessions and current focus;
- mute/pause/resume voice;
- show/hide Orb or avatar;
- register/connect source;
- open configuration/status;
- restart runtime;
- stop runtime;
- quit all child processes.

Closing the renderer should hide it. Quitting from the tray should terminate
the worker, adapters, and renderer, remove stale PID/lock state, and leave no
orphan process. If no sessions remain, the default should be idle-in-tray;
auto-exit can be a later setting.

## Registration and adapter model

Every input source registers a stable source and session identity:

```json
{
  "schema": "codex-voice/source/v0.1",
  "source_id": "codex-tui-1",
  "provider": "codex",
  "transport": "rollout|app-server|acp-stdio|jsonl",
  "project_root": "...",
  "session_id": "...",
  "thread_id": "...",
  "session_label": "...",
  "capabilities": ["activity", "visible-output", "submit-input"],
  "lease_expires_at": "ISO-8601"
}
```

Proposed control commands:

```text
presence status
presence sessions
presence register --provider codex --transport rollout ...
presence connect --transport acp-stdio --command ...
presence disconnect --source-id ...
presence focus --session-id ...
presence mute
presence stop
```

Process discovery may suggest known installed commands, but it cannot attach
to a private stdio ACP stream solely from a PID. A source must either launch
through a relay, register cooperatively, or expose a supported endpoint.

## ACP adapter plan

ACP work begins only after the local runtime and playback state machine are
reliable.

### ACP relay

Build a transparent relay that:

- is launched by an ACP-capable GUI as its configured agent command;
- starts the downstream ACP agent/adapter;
- forwards client requests, agent responses, notifications, and approvals;
- preserves JSON-RPC IDs, session IDs, cancellations, and permission flows;
- emits sanitized normalized events to the Presence Runtime;
- never sends hidden reasoning or raw provider internals to the renderer.

The relay is not a shared provider daemon. Multiple relays may feed one
Presence Runtime.

### First host targets

1. Zed custom ACP agent command.
2. JetBrains ACP agent configuration.
3. VS Code ACP extension/configuration where the extension permits a custom
   command.
4. Codex app-server adapter where the local endpoint is available.
5. Claude/OpenCode adapters according to their public machine interface.
6. Warp only through a supported CLI/MCP/agent integration unless it exposes a
   real ACP command path; MCP alone does not provide the complete streamed
   response seam needed for playback arbitration.

The existing Codex watcher remains a compatibility adapter until a new host
path passes the same E2E contract.

## Implementation order

### Phase 0 - preserve the decision

- Keep ACP as an optional adapter, not the runtime core.
- Keep Live2D/avatar-state protocol unchanged.
- Do not build a replacement coding/editor GUI.
- Keep microphone input opt-in.
- Add this roadmap to lab/release review, not to the distributable skill.

### Phase 1 - instrument and repair current voice input

- Add phase-specific input logs and status codes.
- Verify Orb gesture delivery and exact-once start/stop.
- Verify recording file creation and deletion.
- Verify STT independently with a known recording.
- Verify delivery independently against a mock session.
- Repair arbiter stop/requeue/resume semantics.
- Repair inbox recovery, deduplication, and fair drain ordering.
- Add stateful session identity without per-message prefixes.

### Phase 2 - make the current runtime user-controllable

- Extract a stable service/control boundary around the existing local shell.
- Add single-instance ownership and local IPC.
- Add tray icon/menu and explicit stop/restart/status commands.
- Supervise Kokoro, watcher/adapters, and Electron renderer.
- Ensure last-session shutdown unregisters cleanly without killing unrelated
  sessions.
- Diagnose and fix the behind-Codex window behavior.

### Phase 3 - package without losing the skill

- Build a Windows runtime package with a tray/controller executable and child
  worker/renderer assets.
- Keep source-build and Python fallback paths for development.
- Let the skill install a pinned runtime or build it locally when requested.
- Add startup registration as an explicit opt-in.
- Extend `RUNTIME-MANIFEST.md` and uninstall to cover the global runtime,
  project adapters, tray state, IPC lock, workers, and logs.

### Phase 4 - registration and multi-session arbitration

- Add source registration and lease refresh.
- Add source/session list and manual focus selection.
- Add focus lock, target-response ownership, and queued-session fairness.
- Add the skill rollout instructions for registering the current session.
- Run two-project and two-provider synthetic E2E tests.

### Phase 5 - ACP relays and host integrations

- Implement one transparent ACP relay.
- Prove it with a mock ACP agent/client pair first.
- Integrate Zed, then JetBrains/VS Code.
- Register each relay with the shared runtime.
- Validate concurrent sessions, approvals, cancellation, turn completion, and
  output deduplication.

### Phase 6 - Linux and release promotion

- Port the supervisor/IPC/process lifecycle to Linux.
- Validate CPU Kokoro first, then available NVIDIA paths.
- Validate Electron behavior on X11 and Wayland.
- Run full install, restart, two-session, interruption, uninstall, and clean
  reinstall tests.
- Promote only after the E2E release card is green.

## Acceptance matrix

The roadmap is not complete until these cases pass:

- one session speaks normally with no duplicate playback;
- session identity announces once on a real foreground/turn transition;
- Ctrl+Alt + right-button capture starts immediately and stops on release;
- capture interrupts playback, and the interrupted tail resumes exactly once;
- STT failure is visible, retryable, and does not replay unrelated audio;
- transcript delivery targets the pinned session and never modifies assistant
  text or system/developer context;
- two sessions queue fairly while the target response owns playback;
- stale inbox/focus state recovers deterministically after process kill;
- tray Stop terminates all owned workers and renderer processes;
- the Orb remains controllable without Task Manager;
- registered sources can be listed, focused, disconnected, and reconnected;
- ACP relay traffic preserves protocol behavior while emitting sanitized
  presence events;
- Live2D and built-in Orb receive the same existing activity/audio contracts;
- uninstall removes only manifest-listed runtime resources and preserves
  user-owned avatar bundles and unrelated hooks.

## Explicit non-goals

- building a new coding/editor GUI;
- assuming one ACP daemon per provider;
- scraping arbitrary GUI windows as the primary architecture;
- merging `codex-voice` and `live2d-avatar-controls` into one skill;
- exposing hidden reasoning, raw tool calls, commands, paths, or provider
  secrets to avatars;
- changing the generic Live2D avatar-state envelope for the first runtime pass.
