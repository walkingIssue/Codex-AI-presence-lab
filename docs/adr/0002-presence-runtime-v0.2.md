# ADR 0002: Presence Runtime v0.2 ownership and resolution contract

- Status: accepted
- Date: 2026-07-16
- Epic: [#9](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/9)
- Integration branch: `feature/presence-runtime-v2`

## Context

The v0.1 implementation grew several independent sources of truth. Project-local
profile ledgers, Live2D model state, Electron-side profile interpretation, caller
supplied route keys, and the first project to start the Kokoro arbiter could all
influence the same visible session. A child profile could therefore bypass its
project default, a mutable profile id could change a renderer address, and stale
wardrobe or pose controls could remain latched after an activity transition.

The system needs one machine-local authority that can serve multiple projects
without allowing one project or adapter to nominate another session's voice,
renderer, provider, or route.

## Decision

Presence Runtime v0.2 is one user-level Python supervisor with one warm Kokoro
worker and one central Electron renderer host. It owns configuration resolution,
persistence, source registration, stable routing, queueing, renderer lifecycle,
migration, and uninstall metadata. Adapters are untrusted event sources. They do
not resolve profiles or choose runtime destinations.

All runtime state is machine-local. No user avatar, voice, texture, archive,
profile, preset, binding, geometry, or migration data is committed to Git or
included in a projected release.

### Vocabulary and ownership

| Term | Definition | Authority |
| --- | --- | --- |
| Avatar Model Pack | Immutable model assets, a model fingerprint, safe defaults, named semantic slots, and declared capabilities. | User catalog |
| Preset | Revisioned curated semantic selections and activity behavior compatible with one or more model fingerprints. | User catalog |
| Presence Profile | Revisioned reusable voice, avatar, preset, visibility, and semantic defaults. Provider and microphone policy are excluded. | User catalog |
| Project Default | A reusable profile reference plus a sparse project patch. | Presence database |
| Session Override | A sparse child patch for one session within one project instance. | Presence database |
| Binding | A stable server-generated identity for one project/session target. Human profile ids and adapter route keys are not binding ids. | Presence database |
| Effective Snapshot | A fully validated, immutable configuration revision consumed by TTS and rendering. | Python resolver |
| Activity Overlay | Temporary semantic actions recomputed over the persistent effective state for each transition. | Python resolver |
| Last-known-good | The most recent effective revision acknowledged by required consumers. | Presence database |

### Resolution contract

Every subsystem consumes the output of the same pure Python resolver:

```text
runtime built-ins
<- avatar/model safe defaults
<- reusable presence profile
<- project override
<- session override
<- temporary activity overlay
```

The merge rules are normative:

1. An omitted field inherits.
2. A scalar replaces its parent.
3. `null` clears only schema fields explicitly marked clearable.
4. Objects merge by field.
5. Lists replace; an explicit empty list clears the inherited list.
6. Invalid child data rejects the complete update. No partial application is
   permitted.
7. Provider selection and microphone permission are machine-runtime policies
   and are invalid in project, session, profile, or preset patches.
8. Requested profile, avatar, port, or route ids arriving from adapters never
   have routing authority.

The resolver returns field-level provenance as diagnostic metadata, but model
control selectors and compiled renderer operations do not cross the public
inspection or renderer boundary.

### Semantic composition

Model packs declare named slots such as `body.pose`, `body.legs`,
`wardrobe.outer`, or `prop.hand`, and mark each slot exclusive or composable.
Actions declare every slot they claim. The resolver never infers relationships
from filenames or action naming conventions.

When a higher-precedence action claims an exclusive slot, lower-precedence
actions claiming any of those exclusive slots are evicted as complete actions.
A slot clear removes inherited actions that claim that slot. Independent actions
remain composable. An activity transition always starts from persistent
selections and reapplies the overlay, so clearing the activity restores the exact
persistent wardrobe, pose, accessory, and prop state.

### Persistence and identity

`$CODEX_HOME/presence/state.sqlite3` is the single state authority and runs in
WAL mode. Projects are generated UUID instances keyed by explicit normalized
local roots. Sessions are stable children of a project instance. Moving a project
requires `presence project relocate`; path guessing is forbidden.

Source registration is bound to its IPC connection and returns a server-created
source id, project instance id, binding id, lease token, and expiry. Sources
refresh every 15 seconds and expire after 45 seconds. Expiry makes bindings
dormant without deleting configuration. A dormant or foreign connection cannot
submit audio or activity.

The portable protocol payload is length-prefixed UTF-8 JSON. Windows transports
it over a named pipe; Fedora transports it over a Unix-domain socket.

### Queue and renderer routing

A queued utterance stores binding id, effective revision, utterance id, event id,
and a snapshot of voice, speed, playback mode, and volumes. Those TTS settings do
not change after enqueue. The renderer destination is looked up from the stable
binding when playback begins. Removing a binding cancels its queue; it never
falls through to a profile, foreground window, port, or sibling session.

One Electron root owns one transparent window for each active binding. Windows
receive only resolved snapshots and sanitized events. Geometry is keyed by
binding id. Every high-frequency state/audio packet carries binding id and
utterance id. Hot swaps preload invisibly and replace the old renderer only after
readiness acknowledgement; failure preserves last-known-good and the old window.
The built-in renderer is used only when explicitly selected.

### Catalog and lifecycle

`$CODEX_HOME/presence/catalog/` stores immutable model-pack versions keyed by
fingerprint and revisioned mutable profile/preset records. Deletion is rejected
while referenced unless the caller explicitly requests forced cleanup and accepts
the dependent rebinding/removal plan. Original user archives are never deleted.

The machine-readable runtime manifest is the uninstall authority. Its generated
Markdown view is review material, not a second ownership source. Project
unregister removes only that adapter registration and managed project files.
Runtime uninstall preserves state and catalog unless independent purge flags are
provided, and refuses active sources unless `--all-projects` is explicit.

### Source and release ownership

Root `live2d-avatar-runtime/` is the only tracked Live2D implementation. The
installable skill projection is generated by `tools/project_release.py` and
contains a hash manifest verified by `tools/e2e_check.py`. Skill-only legacy
metadata is archived under `docs/legacy/`; it is not a discoverable second skill.

The same rule applies to the new root `presence-runtime/` package. Release
projection copies it into the skill artifact; changes are never maintained in a
parallel tracked skill subtree.

### Migration

First registration performs a locked, inspected, transactional v0.1 migration.
It drains owned playback, validates all references before writing, imports durable
final speech once by event id, retires ephemeral commentary, and requires worker
plus renderer health acknowledgement before committing the migration ledger. A
failure rolls back the database transaction, leaves every v0.1 file unchanged,
and restarts the owned v0.1 runtime. Legacy files remain read-only rollback input
for the complete compatibility release.

## v0.1 schema freeze

The following v0.1 contracts are frozen on acceptance of this ADR:

- `avatar-state/v0.1` and routed avatar-state ledgers;
- the existing Live2D model manifest, profile, state, and installation documents;
- project-local `presence-profiles.json` and `sessions.json`;
- current project-local inbox and arbiter request records;
- current Orb profile/route/window configuration payloads.

They may receive compatibility or security fixes only. New behavior must be
expressed through v0.2 schemas and the public Presence Runtime IPC.

## Consequences

- Project/session inheritance becomes deterministic and inspectable.
- A profile rename or adapter restart cannot change the renderer address.
- Voice and rendering share one binding identity without caller-selected ports.
- Catalog data can be shared across local projects without Git or project copies.
- Installation becomes user-level; project setup is registration rather than a
  provider/model/Electron install.
- The compatibility release carries migration and wrappers, increasing temporary
  code size, but every legacy entry point delegates to one authority.
- Executable/tray packaging and additional IDE adapters are deferred until the
  v0.2 IPC is stable.

## Rejected alternatives

- Per-platform branches or repositories: they invite contract drift and duplicate
  fixes. Windows and Fedora remain lanes in one source tree.
- Electron-side resolution: it recreates a second inheritance implementation and
  makes malformed partial state observable.
- Adapter-supplied route/profile ids: they cannot establish connection ownership
  and allow cross-project collisions.
- Per-project workers or renderer roots: they duplicate provider assets and make
  the first-started project an accidental machine authority.
- Mutable global Live2D state: it cannot represent independent project and session
  selections and causes latched wardrobe/pose behavior.

## Phase traceability

- Phase 0: [#12](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/12)
- Phase 1: [#13](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/13)
- Phase 2: [#11](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/11)
- Phase 3: [#15](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/15)
- Phase 4: [#10](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/10)
- Phase 5: [#14](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/14)
- Phase 6: [#16](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/16)

