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

## Activity visualization states

The coarse visual state contract is implemented and documented in
[`docs/VISUAL-LAYER-CONTRACT.md`](VISUAL-LAYER-CONTRACT.md). The renderer
currently accepts `idle`, `thinking`, `tool`, `skill`, `cli`, `waiting`, and
`error`; MCP invocation and external tool work normalize to `tool`, while
`speaking` remains a separate playback lifecycle state.

- Make the state palette part of the future visual-theme format instead of
  hard-coding one set of colors.
- Support nested or rapidly alternating activity without making transitions
  visually noisy.

## Orb representation refinements

Feedback to preserve:

- Add a faint, slow breathing motion during idle.
- Introduce slight asymmetry so the Orb feels less like a perfect visualizer.
- Give listening and thinking a dim inner pulse.
- Make speech ripples respond to cadence and phrasing, not only raw volume.

Additional design direction:

- Use the inner aperture as an attention signal: subtly contract or focus it
  while listening/thinking without turning it into an eye or face.
- Let the outer strands carry speech and activity while the inner geometry
  carries attention, creating layered meaning without adding icons or text.
- Add a restrained post-speech afterglow that decays naturally instead of
  cutting the presence off at the exact end of audio.
- Generate asymmetry from slow, bounded phase drift or seeded noise so it feels
  alive but never becomes visual jitter.
- Prefer one strong motion idea per state; avoid stacking every effect at once.
- Keep all of these parameters theme-configurable: breathing rate, asymmetry,
  inner pulse, cadence sensitivity, afterglow, and transition softness.

## Companion window and placement

- Make the Strand Orb movable from the desktop instead of fixing it to the lower-right corner.
- Add an explicit move mode so the Orb remains click-through during normal use and only captures the pointer while being dragged.
- Persist the window position per project, clamp it to the current display work area, and provide a reset-position command.
- Handle display changes, resolution changes, and multi-monitor layouts without stranding the window off-screen.
- Add a small visual affordance while move mode is active so users know when the Orb can be grabbed.

## Linux and cross-platform runtime

- Run the voice worker and Orb on Linux without requiring PowerShell.
- Add a platform abstraction for process launch/stop, virtual-environment paths, audio playback, hook installation, and desktop integration.
- Keep Bash scripts as thin convenience wrappers around the platform-neutral Python runtime instead of duplicating lifecycle logic.
- Validate CPU first, then Intel OpenVINO on Arc hardware; keep NVIDIA CUDA as an optional comparison path.
- Test the Electron companion on X11 and Wayland, treating persisted absolute placement as a compatibility item until both desktop modes are verified.
- Add macOS support only after the Linux lifecycle and audio abstractions are stable.

## Host adapters and portable presence protocol

- Separate the Kokoro worker and Orb runtime from the Codex-specific hook and rollout watcher.
- Define a small local event protocol carrying host, project, session, response phase, text, and timing metadata.
- Keep Codex as the first adapter, then add a generic JSONL/stdin adapter that Warp or another agentic environment can feed without knowing the Kokoro internals.
- Add host adapters as installable integration layers rather than multiplying platform-specific copies of the core skill.
- Preserve the current safety rule across adapters: speak visible assistant output and explicitly enabled commentary only, never hidden reasoning or raw tool output.

## Release and maintenance

- Add release metadata and compatibility reporting to nightly snapshots.
- Add automated checks for stale session state and safe cleanup.
- Add a small configuration matrix test covering project defaults, session overrides, and fallback behavior.
