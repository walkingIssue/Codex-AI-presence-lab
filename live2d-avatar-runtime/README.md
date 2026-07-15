# Live2D Avatar Runtime

`live2d-avatar` is a local-only CLI for importing a Live2D Cubism model, exposing its usable capabilities as a manifest, applying validated avatar state, and recording every runtime-owned artifact for clean removal.

It deliberately separates responsibilities:

- The runtime owns archives, extracted model assets, expression parsing, manifests, state, and lifecycle cleanup.
- The unified `codex-voice` skill is the agent-facing workflow wrapper; this
  package is its internal Live2D implementation seam.
- `codex-voice` owns playback and generic state delivery and receives only the
  negotiated semantic avatar-state contract.

No model assets, license files, or downloaded archives belong in this repository.
The old standalone `live2d-avatar-controls` install scripts are retained only
as compatibility references; use the unified `codex-voice` skill launcher.

## Layout

```text
~/.codex/live2d-models/<model-id>/
  source/                 # imported user-owned model assets
  manifest.json           # generated capabilities and expression metadata
  state.json              # current resolved avatar state

<project>/.codex-live2d/
  installation.json       # project binding to a registry model
  RUNTIME-MANIFEST.md     # exact project-local ownership ledger
```

The registry can be overridden with `CODEX_LIVE2D_REGISTRY` or `--registry` for development and tests.

## Current commands

```powershell
live2d-avatar model import <zip-or-folder> --id <model-id>
live2d-avatar model list
live2d-avatar model inspect <model-id> --json
live2d-avatar actions list <model-id> --json
live2d-avatar model profile scaffold <model-id> --output <user-owned-profile.json>
live2d-avatar model profile apply <model-id> --file <profile.json>
live2d-avatar model profile export <model-id> --output <user-owned-profile.json>
live2d-avatar state show <model-id> --json
live2d-avatar state set <model-id> <action-id> ...
live2d-avatar state enable <model-id> <action-id> ...
live2d-avatar state disable <model-id> <action-id> ...
live2d-avatar project install --project <path> --model <model-id>
live2d-avatar project bind --project <path> --model <model-id> [--profile <user-owned-profile.json>]
live2d-avatar project materialize --project <path> --model <model-id> [--replace]
live2d-avatar project publish --project <path>
live2d-avatar project publish --project <path> --session-id <bound-session-id>
live2d-avatar project sync --project <path>
live2d-avatar project voice-status --project <path> --json
live2d-avatar project context --project <path> --format markdown
live2d-avatar project context-hook enable --project <path>
live2d-avatar project context-hook status --project <path> --json
live2d-avatar project context-hook disable --project <path>
live2d-avatar project status --project <path> --json
live2d-avatar project doctor --project <path> --json
live2d-avatar project uninstall --project <path> --yes
live2d-avatar model remove <model-id> --yes
```

`model import` accepts a directory or ZIP, rejects archive traversal and symbolic links, copies assets rather than modifying the source, finds one `.model3.json` file, and extracts `.exp3.json` actions plus optional VTube Studio trigger metadata. It does not execute files from an imported model.

A profile translates vendor-specific expression files into stable semantic action IDs, visible descriptions, renderer framing, and renderer-local safe defaults. Start with `model profile scaffold`, which writes a user-owned draft with exact source selectors and generated IDs but no claim about visual meaning. New drafts carry a content fingerprint for the imported model revision; `apply` rejects a portable profile pack for a different revision. After visual user confirmation, refine it and mark `semantic_status` as `curated`. Export a reviewed profile with `model profile export`, then reuse it through `project bind --profile`. All models use `active-toggle-set` semantics: each action is an independent expression toggle, and the archive does not provide a trustworthy conflict, dependency, or pose-replacement graph.

`state set` replaces the complete active toggle set. `state enable` and `state disable` add or remove independent toggles. The renderer replays active toggles in the model-local VTube declaration order, so the sorted generic Voice envelope cannot change composition order. A future user-curated semantic preset layer may record combinations that have been visually tested, but it must not be inferred from expression filenames or parameter overlap.

Profiles may define bounded renderer-local presentation: `halo.enabled` turns the visual halo on or off, `activity_actions` maps coarse Voice activity states to temporary curated `{ add, suppress }` action rules, and `speech_motion` routes smoothed voice energy into known model rig controls, including an optional jaw-led mouth channel. These are deliberately separate from avatar state: no renderer controls or values cross the Voice bridge or enter Codex turn context. Activity rules can add local toggles or temporarily suppress a controller-selected toggle only for their current activity; they are not inferred from filenames and must be visually curated per model.

`project bind` is the standard idempotent route: it creates the project binding as needed, materializes the renderer, and asks Codex Voice's own avatar installer to select it. With `--profile`, it first applies a verified user-owned profile pack and refreshes the bundle in the same command. It reports when an Orb restart is required because Voice loads avatar selection at startup. `project materialize` remains available for explicit bundle refreshes. `project publish` calls the voice-provided writer; it never writes Voice state files directly or sends raw Cubism parameters across the bridge. It routes to the current bound Codex task when possible, accepts `--session-id` for an explicit target, and refuses ambiguous multi-session wardrobe broadcasts unless `--project-wide` is intentional.

## Agent turn context

`project doctor` is the bounded, read-only setup check. It compares the intended model, semantic-profile review status, managed bundle, Voice selection, and host state diagnostics without creating a process or writing Voice state.

`project context` emits a bounded semantic snapshot: the selected avatar, its Voice-host accepted state when available, the controller's desired state, and valid action IDs with their descriptions. It labels unprofiled and draft mappings so agents do not treat them as visually confirmed controls. It deliberately omits model paths, expression filenames, hotkeys, Cubism parameters, texture names, and compiled operations.

`project context-hook enable` copies one generated hook into `<project>/.codex-live2d/` and JSON-merges one `UserPromptSubmit` handler into `<project>/.codex/hooks.json`. On each Codex turn it uses only the hook cwd/event metadata, emits the current semantic context as developer context, and otherwise exits silently. It starts no service or listener and does not inspect or forward the prompt or transcript. Codex may ask to review/trust the new local hook before it runs. Disable it explicitly or run project uninstall to remove only this handler; existing Voice `Stop` hooks and other user handlers are preserved.

## Development

Renderer hot-path design, local baselines, and the intentionally deferred configuration matrix are recorded in [docs/RENDERER-PERFORMANCE.md](docs/RENDERER-PERFORMANCE.md).

```powershell
py -3.12 -m pip install --editable .
$env:PYTHONPATH = "$PWD/src"
py -3.12 -m unittest discover -s tests -v
```

Read [RUNTIME-MANIFEST.md](RUNTIME-MANIFEST.md) before changing installation behavior. Every branch that creates, downloads, starts, listens on, or deletes a user-machine resource must update that ledger and its matching uninstaller in the same change.
