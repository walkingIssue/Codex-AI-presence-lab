# Packaging and Repository Strategy

**Status:** accepted working direction
**Date:** 2026-07-16
**Applies to:** Presence Runtime, `codex-voice`, host adapters, renderers,
installers, and release automation

## Decision

Maintain **one canonical source repository and one cross-platform `main`
branch**. Build and publish platform-specific artifacts from the same tagged
source revision.

“Self-contained” means one owned, versioned local installation for a user; it
does **not** require one giant process or a rewrite of validated components.
The first packaged runtime may be a small native tray/supervisor executable
that owns the existing inference worker and renderer as child processes.

## Repository roles

| Surface | Role |
| --- | --- |
| `Codex-AI-presence-lab` | Canonical source for runtime, adapters, renderers, installers, tests, docs, and release automation. |
| `Codex-AI-presence` | Current clean skill-install surface, projected from the lab while skill installation remains supported. It is not a competing source of truth. |
| `Codex-AI-presence-nightly` | Prerelease/testing channel. Over time, prefer prerelease artifacts from the same source commit over long-lived divergent code. |
| GitHub Releases | The eventual distribution surface for versioned Windows and Fedora runtime artifacts. |

Do not create platform-specific source repositories or platform-specific
`main` branches. A Windows fix and a Fedora fix should meet in the same source
tree, share contracts/tests, and produce separate artifacts in CI.

## Branching and versioning rules

- `main` is the sole cross-platform integration branch.
- Use short-lived feature branches for implementation work.
- Use version tags and release artifacts for released versions.
- Create `release/vX.Y` only when a supported released line needs targeted
  backports; it is never a Windows or Fedora fork.
- Do not use branches as distribution channels. Stable and nightly are release
  channels, not places to accumulate platform-specific source divergence.

## Product ownership model

| Component | Ownership |
| --- | --- |
| `codex-voice` skill | Bootstrap/control plane: install, update, configure, diagnose, register adapters, and uninstall. |
| Presence Runtime | One user-level service: worker supervision, attention/playback arbiter, durable inbox, session/source registry, renderer supervision, local IPC, and tray/control surface. |
| Host adapters | Lightweight project/host clients that register sessions with the runtime. They do not own a second persistent worker. |
| Renderer and model integration | Runtime-controlled presentation extension. User-owned models and avatar bundles remain outside packaged cleanup ownership. |

The runtime and adapters must carry explicit compatibility versions. A skill may
install or upgrade a pinned compatible runtime, but it must never silently
replace an unrelated user-level runtime.

## Packaging plan

1. Stabilize the user-level service, registration contract, and two-layer
   ownership manifests.
2. Package the Windows runtime first as a versioned `win-x64` bundle with
   install, upgrade, status, restart, and scoped-uninstall tests.
3. Keep validated inference and renderer components intact inside that bundle;
   source-build and Python fallback paths remain for development.
4. Build a Fedora artifact from the same revision, initially with a
   user-service lifecycle and explicit X11/Wayland validation. Introduce native
   distro packages only after the lifecycle is reliable.
5. Publish both artifacts from one CI release matrix, alongside compatibility
   metadata and checksums.

The initial distribution form may be a portable archive. Native installers or
packages are a delivery improvement, not a reason to fork the source tree.

## Manifest and uninstall rule

There are two ownership layers:

1. The **user-level runtime manifest** covers the supervisor, worker/renderer
   assets, IPC state, tray state, logs, caches, update metadata, and service
   registration.
2. The **project-adapter manifest** covers project-local hooks, adapter
   shims, and project configuration.

Every feature must name its resources, scope, cleanup owner, and
update/migration behavior in the appropriate manifest. Uninstall removes only
that layer's owned resources and preserves unrelated hooks, projects, sessions,
and user-owned avatar data.

## When a separate repository is justified

Split out a dedicated runtime repository only after all of these are true:

- the runtime has a stable public adapter API;
- it has an independent release cadence from the skill;
- multiple client packages consume it; and
- separate ownership reduces coordination cost more than it duplicates release
  and compatibility work.

Per-platform repositories are not a planned outcome. Separate packaging
pipelines or artifact registries are enough unless a real external constraint
requires otherwise.

## Explicit non-decisions

- No commitment yet to a particular native language, installer technology, or
  package format.
- No automatic replacement of the user's normal `codex` command.
- No requirement that GPU provider assets or user model assets be bundled into
  every platform artifact.
- No replacement coding/editor GUI.
