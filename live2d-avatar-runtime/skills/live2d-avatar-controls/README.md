# Compatibility source directory

The agent-facing Live2D instructions are merged into the repository's single
`skills/codex-voice/SKILL.md`. This directory is retained as provenance for the
upstream runtime tree, but it is not an installable or discoverable skill.

Use `skills/codex-voice/scripts/live2d-avatar.py` (or its `.sh`/`.ps1`
counterpart) from the unified skill instead of installing this directory under
`~/.codex/skills/live2d-avatar-controls`.
