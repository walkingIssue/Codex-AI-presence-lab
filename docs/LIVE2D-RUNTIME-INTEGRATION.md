# Live2D runtime integration

The Live2D runtime is a first-class package in this repository at
`live2d-avatar-runtime/`. It was imported from the clean upstream working tree
at:

- Repository: `https://github.com/walkingIssue/live2d-runtime-skill`
- Branch: `agent/cubism-renderer-baseline`
- Revision: `36200c9e82dd880cbc20c798ffddae744d16e9c4`
- Commit time: `2026-07-13T16:09:25+02:00`

The upstream `.git` directory and ignored build/test caches are intentionally
not copied. The package's own `pyproject.toml` remains the package boundary and
declares no external Python dependencies. From the lab root, use the source
package directly during development:

```sh
PYTHONPATH="$PWD/live2d-avatar-runtime/src" \
  python3 -m live2d_avatar --help

PYTHONPATH="$PWD/live2d-avatar-runtime/src" \
  python3 -m unittest discover -s live2d-avatar-runtime/tests -v
```

## Ownership seam

`codex-voice` owns the Presence Service, Kokoro playback, Electron host, and
generic activity/audio/avatar-state contracts. `live2d-avatar-runtime` owns
model import, fingerprints, curated profiles, compiled Cubism operations, and
the renderer bundle it materializes.

Only semantic action IDs and the complete desired action set cross the bridge.
Model paths, expression files, hotkeys, parameter IDs, parameter values,
textures, and compiled operations remain inside the Live2D runtime.

The runtime may create user-owned or generated data in the normal managed
locations, including `~/.codex/live2d-models/`, `<project>/.codex-live2d/`, and
`<project>/.codex-voice-avatars/`. Those artifacts are not source dependencies
and are not committed to this repository. The runtime must continue to update
its lifecycle manifest and ownership checks when those boundaries change.
