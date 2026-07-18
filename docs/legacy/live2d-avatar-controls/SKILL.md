---
name: live2d-avatar-controls
status: archived-v0.1-metadata
---

# Archived Live2D Avatar Controls skill metadata

This file preserves the former standalone skill identifier for v0.1 migration
and audit work. It is intentionally outside every discoverable `skills/` tree.
Presence Runtime v0.2 exposes these intents through the `presence catalog`,
`presence avatar`, `presence preset`, and `presence inspect` commands.

The former workflow imported one model into `~/.codex/live2d-models`, installed
one materialized bundle per project, mutated model-global state, published that
state into Codex Voice, and required renderer restart for selection changes.
Those instructions are retained only as migration context and must not be used
to operate a v0.2 runtime.

Legacy source identifier: `live2d-avatar-controls`.

