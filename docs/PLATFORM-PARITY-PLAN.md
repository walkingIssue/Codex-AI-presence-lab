# Platform Parity and Host-Adapter Implementation Plan

**Status:** active planning and regression baseline
**Date:** 2026-07-16
**Scope:** Codex GUI, Codex CLI/TUI, VS Code integrations, and future ACP-capable hosts

## Decision

The Codex GUI runtime is the current reference behavior. The next work is not
to widen the number of hosts immediately; it is to make that behavior
explicit, testable, and portable through one user-level Presence Runtime.

The Presence Runtime is a machine-user service. It owns the long-lived
Kokoro worker, attention/playback arbiter, inbox, and renderer supervision.
Project-local skills and host adapters are clients of that runtime: they
install/configure it and cooperatively register sources and sessions. They
must not create a competing project-scoped worker or treat the runtime as a
child process of an adapter.

ACP remains an optional adapter transport. It is not the core runtime and is
not a reason to attach to arbitrary GUI processes or private stdio streams.

## Lifecycle ownership gate

Every feature has an explicit lifecycle acceptance criterion. Before it can be
closed, the implementation must either update `RUNTIME-MANIFEST.md` or record
why it adds no managed runtime dependency. The manifest entry names every
introduced or changed process, environment/package, model/cache, IPC endpoint,
configuration file, log, and temporary artifact that applies to the feature;
it also identifies whether the resource is user-level or project-local, its
cleanup owner, and any update/migration rule.

The corresponding uninstall test must remove only those manifest-listed
resources. It must preserve unrelated hooks, projects, registered sessions,
and user-owned avatar assets. This gate applies to feature work, adapter work,
and packaging work alike; documentation-only work records an explicit
no-managed-resource result instead.

## GUI reference capability set

The following capability set is the common parity contract. “Reference” means
it is the behavior to preserve; a platform gets a pass only from an observed
end-to-end regression run, not by matching unit tests or source structure.

| Capability | Codex GUI reference | Other platform status |
| --- | --- | --- |
| Default presence renderer | Reference behavior | Not certified |
| One user-level Kokoro worker and shared attention arbiter | Reference behavior | Not certified |
| Session- and project-scoped activation | Reference behavior | Not certified |
| Default profile plus per-session voice/rendered-presence profiles | Reference behavior | Not certified |
| Per-session curation and exact routed-state precedence | Reference behavior | Not certified |
| Session identity, foreground ownership, and fair session queuing | Reference behavior | Not certified |
| Hold-to-record local input from a presence window | Reference behavior | Not certified |
| Pause voice for recording and resume the interrupted output exactly once | Reference behavior | Not certified |
| Local transcript delivery appropriate to the host | Reference behavior | Not certified |
| Thinking, tool, CLI, skill, waiting, and error state forwarding | Reference behavior | Not certified |
| Speech cadence/amplitude forwarding to the renderer | Reference behavior | Not certified |
| Persistent per-session resize and reposition | Reference behavior | Not certified |
| Ephemeral visible/spoken progress updates without stale replay | Reference behavior | Not certified |
| Restart, recovery, status, and ownership-safe cleanup | Reference behavior | Not certified |

The GUI card must also keep these privacy rules true: renderer events contain
only sanitized semantic state and audio features; no hidden reasoning, raw tool
payloads, paths, secrets, or retained recordings are passed through.

## Required proof scenarios

The capability table cannot be satisfied by a feature existing in source or by
a process-count check. Every supported host/platform must capture evidence for
these scenarios before it marks the related capability as proven:

1. **Routed-state precedence:** conflict a model default, profile curation,
   and exact route state; drive every activity overlay, restart/rebind, and
   prove the leaf state wins without changing a second session's route.
2. **Foreground ownership and fairness:** drive two overlapping sessions
   through output, activity, commentary, interruption, and restart; one voice
   owner at a time, pinned target-response ownership, eventual fair drain, and
   no cross-session or duplicate replay are required.
3. **Capture and delivery:** record, transcribe, and deliver to exactly the
   intended session through the host-safe route; verify cancellation, timeout,
   restart, and temporary-recording cleanup as well as the success path.
4. **Interruption and durable recovery:** interrupt speech for input, then
   complete or fail input processing and restart at lifecycle boundaries; the
   interrupted stream resumes from the correct cursor once, while failed input
   remains retryable without duplicating unrelated output.
5. **Ephemeral progress lane:** rapid progress updates coalesce, never become
   durable inbox items, never outrank real output, and cannot reappear after a
   restart.
6. **Lifecycle visibility and cleanup:** status reports the real owner and
   route; restart/shutdown leaves no orphan or stale registration; uninstall
   follows the lifecycle ownership gate above.
7. **Privacy:** renderer and voice envelopes stay limited to allowed semantic
   state, routing, and audio features. They exclude hidden reasoning, raw tool
   payloads, arbitrary paths, secrets, and retained recordings.
8. **TUI-only incremental streaming:** where the host uses the Codex TUI/CLI
   bridge, [issue #8](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/8)
   must prove inference starts from visible deltas before the turn finishes.

## Work streams

### 0. Correct the runtime-registration contract

Track in [issue #2](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/2).

1. Define the user-level runtime service, local IPC, source/session
   registration, and lease renewal.
2. Make the skill a control plane: install, update, diagnose, register, and
   uninstall the user-level runtime.
3. Define the canonical Windows and Fedora TUI launch/resume flow.
4. Make a `codex` wrapper strictly opt-in. A normal invocation that is not
   intercepted must be able to register with the runtime through the supported
   path.
5. Update runtime manifests and cleanup so global and project-local resources
   cannot be mistaken for each other.

**Exit gate:** two registered sessions from different projects share one
worker; disconnecting one does not orphan or stop the other.

### 1. Certify the GUI reference card

Track in [issue #1](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/1).

Turn the table above into a manual-first regression card with captured
evidence. A capability is marked `proven`, `unproven`, or `not applicable`;
there is no implicit green status. This also closes the known gap that the GUI
path has not received a deliberate full regression pass.

### 2. Bring Codex CLI/TUI to parity

- Windows: [issue #3](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/3)
- Fedora: [issue #4](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/4)

Both lanes run the same capability card. Fedora adds explicit X11 and Wayland
coverage where focus, click-through, resize, or global input differs. The
Windows lane first converts the existing partial observations—resize,
reposition, shared worker, session connection, and session announcement—into
repeatable evidence, then tests the previously untested capture,
pause/resume, and arbiter paths.

The TUI/CLI bridge also has one adapter-specific requirement:
[issue #8](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/8)
must stream voice inference from ordered **visible assistant-content** deltas
while a turn is active. A final-message or Stop hook may seal the stream, but
must never be the event that first allows synthesis to begin. The contract
needs deterministic start, delta, finish, cancellation, ordering, and
deduplication coverage while continuing to use the shared arbiter.

**Exit gate:** each lane has a full pass/fail card, a two-session run, and a
mixed-host run that proves the same user-level worker serves both registered
sources.

### 3. Establish the VS Code adapter seam before feature work

- Windows: [issue #5](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/5)
- Fedora: [issue #6](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/6)

Do not assume a VS Code plugin, a Codex integration, and an ACP relay offer
the same lifecycle or stream access. The first deliverable is a small
capability ledger identifying the public seam for session identity, visible
output, cancellation, approvals, and safe user-input delivery. Only then build
a minimal transparent adapter and run the shared parity card.

**Exit gate:** one documented, supported adapter path with a mock-first test
and a complete parity result. Platform differences must not fork the normalized
event or registration protocol.

### 4. Select the next third-party host deliberately

Track in [issue #7](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/7).

Evaluate candidate IDEs and agentic CLIs with a weighted rubric:

1. Cooperative, documented event/stream seam.
2. Session and cancellation fidelity.
3. Ability to launch/register with the shared runtime.
4. Safe input-delivery boundary and permission model.
5. Windows and Fedora reach, deterministic E2E testing, and maintenance cost.

The first selected host proves one transparent adapter/relay. It does not
replace the editor or create a second global worker. ACP work begins only when
the core runtime and TUI interruption/recovery behavior are certified.

## Tracking map

| Issue | Purpose |
| --- | --- |
| [#1](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/1) | GUI reference baseline and regression card |
| [#2](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/2) | User-level runtime registration and SkillRollout correction |
| [#3](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/3) | Windows Codex CLI/TUI parity |
| [#4](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/4) | Fedora Codex CLI/TUI parity |
| [#5](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/5) | Windows VS Code adapter discovery and parity |
| [#6](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/6) | Fedora VS Code adapter discovery and parity |
| [#7](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/7) | Next-host selection rubric and pilot decision |
| [#8](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/8) | Incremental visible-content streaming for the Codex TUI bridge |

## Non-goals for this phase

- Building a replacement coding GUI.
- Scraping arbitrary GUI windows or attaching to a private ACP stdio stream by
  process ID.
- Automatically replacing the user's normal `codex` command.
- Letting a host integration create a second long-lived Kokoro worker.
- Treating an untested feature as parity because it exists in another host's
  source tree.
