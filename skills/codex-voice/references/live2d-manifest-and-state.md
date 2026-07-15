# Live2D manifest and state reference

The integrated Live2D runtime creates a model-local `manifest.json` and
`state.json` under the registry. They are generated state, not hand-maintained
project files.

## Manifest roles

- `model.path` is one validated Cubism `.model3.json` relative to the copied
  asset root.
- `actions` contains stable action IDs, labels, expression sources, optional
  VTube metadata, replay order, and renderer-local compiled operations.
- `lifecycle` records the runtime owner and paths that `model remove` may
  delete.

The importer gives expressions deterministic `expression.<hash>` IDs because
filenames can be non-Latin, duplicated, or vendor-specific. A curated profile
may add semantic aliases without changing the source archive. It must not infer
conflicts, dependencies, or complete poses from filenames alone.

Start profile work with `model profile scaffold <model-id> --output
<user-owned-profile.json>`. The draft uses exact manifest selectors and is a
setup artifact, not a visual claim. After visual testing, refine semantic IDs,
labels, descriptions, renderer framing, and activity mappings, then set
`semantic_status: "curated"`. Applying a profile with a mismatched model
fingerprint is rejected.

`renderer.halo.enabled` controls the halo. `renderer.activity_actions` maps
known coarse states (`idle`, `thinking`, `tool`, `skill`, `cli`, `waiting`, and
`error`) to `{ "add": [...], "suppress": [...] }` rules. Optional speech
motion is renderer-local and must be visually validated. `renderer.fixed_parameters`
and `renderer.fixed_parts` may reassert a visually verified model-local
watermark/accessory control every frame; they are not state-envelope actions.
None of these values are inserted into the Voice state envelope or turn
context.

## State roles

- `active_actions` is the complete desired toggle set and is unordered at the
  Voice boundary.
- `effective_parameter_operations` is the renderer-local compiled plan in
  model-local replay order.
- `revision` increases only when the resolved state changes.

The bridge emits only `avatar_id`, `source`, `scope`, `revision`, and `actions`,
plus route fields when needed. The renderer reads its local capabilities after
the host validates the selected avatar and `avatar-state-v1` capability.

## Toggle policy

`state set` replaces the complete set, `state enable` adds independent toggles,
and `state disable` removes them. The runtime does not invent an exclusive
pose graph. A user-curated preset may describe a visually tested combination,
but that is separate from the underlying toggle state.

## Project readiness

`project doctor --project <path>` is read-only. It reports whether the selected
model is unprofiled, draft, or curated; whether the owned renderer bundle
advertises `avatar-state-v1`; whether Codex Voice selects that bundle; and
whether the host last accepted its state. It never reads raw model controls or
writes Voice files.

`project bind --project <path> --model <id> --profile <profile.json>` is the
reviewed visual integration path. It applies the profile, installs the project
boundary as needed, materializes the renderer, and calls the Voice-owned avatar
installer with `--use`; it never writes the selection marker itself. A changed
binding requires an Orb restart because Voice reads selection at startup.
