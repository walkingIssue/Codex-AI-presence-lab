# Release gate

The release repository is promoted only after the E2E workflow is green.

## Required checks

- Skill metadata is valid and the package parses as Python.
- The projected package contains only `skills/codex-voice`.
- A clean temporary project can show and change the complete configuration matrix.
- Voice, speed, playback mode, provider, main volume, and commentary-volume values persist.
- Progress, Orb, and scope markers behave safely when disabled.
- Invalid speed and volume values are rejected.
- Provider selection refuses unavailable CUDA or DirectML runtimes instead of silently claiming readiness.
- The skill does not package recordings, roadmap files, model weights, voice bundles, secrets, or local runtime state.

## Promotion rule

The lab E2E workflow is the release card. A green run permits a projection; a failed or skipped run does not. Promotion is manual and secret-gated so a repository credential is never embedded in the skill artifact.
