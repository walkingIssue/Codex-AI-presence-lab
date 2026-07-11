# Roadmap

This is intentionally kept outside the distributable skill. The detailed backlog is in [WISHLIST.md](WISHLIST.md).

## Current foundation

- Project-local Kokoro worker with CPU, NVIDIA CUDA, and Intel Arc DirectML routes.
- Session or project activation scope.
- Stream and quality playback modes.
- Runtime configuration for voice, speed, volume, commentary volume, progress, Orb, and provider.
- Audio-synchronized Strand Orb playback.
- Clean skill projection and Windows E2E gate.

## Next experiments

- App-server response-delta bridge for lower-latency text-to-speech handoff.
- Optional [per-session configuration profiles](WISHLIST.md#per-session-configuration-profiles) without changing the project default.
- Movable, persistently positioned companion window with an explicit move mode.
- Semantic Orb states for thinking, tool activity, skill execution, and local CLI work.
- Linux CPU support, followed by optional CUDA and desktop-environment validation.
- A host-neutral presence event bridge with Codex and generic adapters.
- A documented visual-theme format for custom Orb geometry and palettes.
- Short, focused showcase clips for installation, voice changes, and Orb playback.
- NVIDIA hardware validation and provider performance comparison.
- Recovery and migration tooling for existing project-local installations.
- Release metadata and compatibility reporting for nightly snapshots.
