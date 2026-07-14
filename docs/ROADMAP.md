# Roadmap

This is intentionally kept outside the distributable skill. The detailed backlog is in [WISHLIST.md](WISHLIST.md).

The current implementation sequence for the local runtime, haunted voice-input
slice, tray lifecycle, source registration, and optional ACP relays is recorded
in [PRESENCE-RUNTIME-ROADMAP.md](PRESENCE-RUNTIME-ROADMAP.md).

For a focused handoff on diagnosing voice capture, STT, interruption, and inbox
replay, see [VOICE-INPUT-INVESTIGATION-BRIEF.md](VOICE-INPUT-INVESTIGATION-BRIEF.md).

## Current foundation

- Project-local Kokoro worker with CPU, NVIDIA CUDA, and Intel Arc DirectML routes.
- Session or project activation scope.
- Stream and quality playback modes.
- Runtime configuration for voice, speed, volume, commentary volume, progress, Orb, and provider.
- Audio-synchronized Strand Orb playback.
- Canonical activity states for thinking, tool/MCP work, skills, CLI, waiting, and errors.
- Separate playback and input-status contracts with session-aware routing and safe TTL expiry.
- Mocked Codex TUI/server bridge seam for visible response chunks.
- Clean skill projection and Windows E2E gate.

## Next experiments

- Connect the TUI/server bridge to the working incremental Kokoro inference worker.
- Optional [per-session configuration profiles](WISHLIST.md#per-session-configuration-profiles) without changing the project default.
- Movable, persistently positioned companion window with an explicit move mode.
- Orb representation refinements: idle breathing, bounded asymmetry, inner attention pulse, and cadence-aware speech motion.
- Linux CPU support, followed by optional CUDA and desktop-environment validation.
- A host-neutral presence event bridge with Codex and generic adapters.
- A documented visual-theme format for custom Orb geometry and palettes.
- Short, focused showcase clips for installation, voice changes, and Orb playback.
- NVIDIA hardware validation and provider performance comparison.
- Recovery and migration tooling for existing project-local installations.
- Release metadata and compatibility reporting for nightly snapshots.
