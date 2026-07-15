# Manifest and State Reference

The importer creates a model-local `manifest.json` and `state.json` under the registry. They are generated state, not hand-maintained project files.

## Manifest roles

- `model.path`: one validated Cubism `.model3.json` relative to the copied asset root.
- `actions`: stable action IDs, labels, expression source, optional VTube metadata, model-local replay order, and compiled model parameter operations.
- `lifecycle`: records the runtime owner and paths that `model remove` may delete.

The v0.1 importer gives expressions deterministic `expression.<hash>` IDs because filenames can be non-Latin, duplicated, or vendor-specific. A later model profile may add semantic aliases without changing the source archive. It cannot infer conflicts, dependencies, or complete poses from the expression files.

Start profile work with `model profile scaffold <model-id> --output <user-owned-profile.json>`. The draft uses exact manifest `source` selectors so duplicate expression filenames remain unambiguous. Legacy `source_file` selectors remain supported only when their basename is unique. The scaffold sets `semantic_status: "draft"` and a compact model-revision fingerprint; it is a setup artifact, not a visual claim. After user-confirmed visual testing, refine semantic IDs, labels, and descriptions and set `semantic_status: "curated"` before using it as normal agent context. Use `model profile export <model-id> --output <user-owned-profile.json>` to preserve the applied mapping as a user-owned portable pack. Applying a pack with that fingerprint to another model id is allowed only when the copied model revision matches; legacy profiles without a fingerprint remain supported but lack that compatibility check.

Profiles without an explicit `semantic_status` are treated as `draft` for safety.

Apply a profile with `model profile apply <model-id> --file <profile.json>`. A profile assigns stable IDs and descriptions and can specify `safe_default_actions`, `initial_actions`, renderer framing, and bounded renderer-local speech motion. `renderer.halo.enabled` is a boolean on/off switch. `renderer.activity_actions` may map the known coarse activity states (`idle`, `thinking`, `tool`, `skill`, `cli`, `waiting`, `error`) to `{ "add": [...], "suppress": [...] }` rules of valid semantic action IDs. An optional `renderer.speech_motion.mouth` channel can route a smoothed speech envelope to a primary aperture, optional secondary aperture, and optional jaw control; use it only after visual validation. The renderer derives its local effective actions by adding the temporary actions and suppressing only the current controller action set; it clears or recomputes that overlay on state changes and never adds it to the Voice avatar-state envelope. Profiles use `state_semantics: "active-toggle-set"`: active actions are independent expression toggles, not an inferred exclusive pose system. Reusable profiles may keep descriptions in a top-level `action_descriptions` map keyed by semantic action ID. Safe defaults and speech-motion controls remain in the renderer-local capability file; they are never inserted into the voice state envelope or turn context.

## State roles

- `active_actions` is the complete desired toggle set; it is unordered at the Voice boundary.
- `effective_parameter_operations` is the renderer-local compiled plan in model-local replay order.
- `revision` increases only when the resolved state changes.

For the voice bridge, emit only `avatar_id`, `source`, `scope`, `revision`, and `actions`, plus `session_id`, `profile_id`, and `route_key` for routed state; do not emit the compiled operations. The renderer reads its local `avatar-capabilities.json` after the host validates the selected avatar and `avatar-state-v1` capability.

## Toggle policy

The runtime does not declare inferred conflicts. `state set` replaces the complete set, `state enable` adds independent toggles, and `state disable` removes them. The renderer uses the VTube declaration order stored in the local capability file when it composes the set. A later, user-curated preset can describe visually tested combinations, but that is separate from the underlying toggle state.

## Project readiness

`project doctor --project <path>` is read-only. It reports only generic setup facts: whether the selected model has an unprofiled, draft, or curated semantic layer; whether the owned renderer bundle advertises `avatar-state-v1`; whether Codex Voice currently selects that bundle; and whether the host last accepted its state. It never reads model controls or writes Voice files.

`project bind --project <path> --model <id> --profile <user-owned-profile.json>` is the fast lifecycle step for a reviewed mapping. It validates and applies the profile pack, installs the project boundary as needed, and calls the Voice-owned avatar installer with `--use`; it never writes the selection marker itself. A changed or refreshed binding requires an Orb restart because Voice reads selection at startup.
