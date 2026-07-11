# Wishlist

This list belongs to the development lab. It records desirable experiments and product improvements; it is not part of the installable skill and does not promise a release date.

## Highest priority

### Per-session configuration profiles

Keep one project profile as the default, while allowing an individual Codex session to override it without changing the rest of the project.

The intended precedence is:

```text
session override  ->  project profile  ->  built-in defaults
```

The session profile should be able to override the same user-facing settings as the project profile:

- voice
- speed
- stream or quality mode
- CPU, CUDA, or DirectML provider
- main response volume
- commentary volume
- progress visibility
- Strand Orb visibility

Desired behavior:

- A session inherits the project profile until it explicitly changes a setting.
- `configure.py --scope session` edits only the current session profile.
- `configure.py --scope project` edits the project default.
- Disabling the session override returns that session to the project profile.
- Session identity is registered explicitly, so the watcher does not mix audio from other Codex projects or sessions.
- Stale session registrations can be listed and removed safely.
- A missing or malformed session identity falls back to the project profile instead of speaking another session's response.

Acceptance target: two simultaneous sessions in one project can use different voices or playback modes, while a separate project remains completely isolated.

## Latency and runtime

- Bridge app-server response deltas directly into the watcher for lower-latency speech handoff.
- Keep one persistent Kokoro worker warm between responses.
- Add provider readiness diagnostics and a performance comparison for CPU, NVIDIA CUDA, and Intel Arc DirectML.
- Add recovery and migration tooling for existing project-local installations.

## Companion rendering

- Define a documented visual-theme format for custom Strand Orb geometry, palettes, and motion.
- Make audio-reactive deformation configurable without coupling it to the voice implementation.
- Add short, focused showcase clips for installation, voice changes, configuration, and Orb playback.

## Release and maintenance

- Add release metadata and compatibility reporting to nightly snapshots.
- Add automated checks for stale session state and safe cleanup.
- Add a small configuration matrix test covering project defaults, session overrides, and fallback behavior.

