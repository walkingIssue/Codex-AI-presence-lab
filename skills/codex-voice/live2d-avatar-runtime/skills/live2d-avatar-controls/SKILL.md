---
name: live2d-avatar-controls
description: Import, bind, inspect, control, troubleshoot, or clean up local Live2D Cubism and VTube model archives used as Codex avatars. Use when Codex needs to load a ZIP or model folder, prepare semantic avatar controls, attach an avatar to Codex Voice, change its expression toggles, inspect readiness, or remove the managed runtime without deleting user-owned source archives.
---

# Live2D Avatar Controls

Use the installed `live2d-avatar` launcher. Do not edit imported assets, generated manifests, state files, or Voice selection markers directly. The runtime owns copied assets and resolved state; the original model archive stays user-owned.

## Start safely

For an existing project, inspect the bounded setup state first:

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project doctor --project <project-path> --json
```

Before installing, binding, refreshing, or removing resources, read the source repository's `RUNTIME-MANIFEST.md`. Update its ownership table and matching cleanup when the runtime gains a user-machine resource.

## Prepare a generic model

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" model import <zip-or-folder> --id <model-id>
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" model profile scaffold <model-id> --output <user-owned-profile.json>
```

The scaffold is a user-owned draft. It has exact model selectors and stable generated IDs, but makes no claim about an expression's visual meaning. Inspect or test the model with the user, then refine IDs, labels, and descriptions in that draft. Set `semantic_status` to `curated` only after visual confirmation; keep `state_semantics: active-toggle-set` and do not invent conflicts, dependencies, or exclusive pose groups. Apply it with:

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" model profile apply <model-id> --file <user-owned-profile.json>
```

After a profile has been visually curated, preserve the semantic mapping as a user-owned portable pack. It is fingerprinted to the copied model revision, so it is reusable under another local model id only when the actual model revision matches:

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" model profile export <model-id> --output <user-owned-profile.json>
```

The profile's renderer config has two bounded optional fields: `halo.enabled` is a boolean switch for the background halo, and `activity_actions` maps an existing Voice activity state (`thinking`, `tool`, `skill`, `cli`, `waiting`, or `error`) to a temporary `{ "add": [...], "suppress": [...] }` rule of already-curated action IDs. `add` overlays local actions; `suppress` temporarily hides a controller-selected action until the activity clears. Neither changes the Voice avatar state. Do not map actions from filenames or guesses—visually test and curate each mapping with the user.

Read [manifest and state reference](references/manifest-and-state.md) when editing a profile. Do not expose model paths, expression filenames, VTube hotkeys, texture names, raw controls, or compiled operations in normal user-facing output or Codex turn context.

## Bind and use the avatar

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project bind --project <project-path> --model <model-id> --profile <user-owned-profile.json>
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project publish --project <project-path>
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project publish --project <project-path> --session-id <session-id>
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project voice-status --project <project-path> --json
```

`project bind --profile` is the fast path for a reviewed reusable mapping: it validates and applies the user-owned profile first, then materializes through Codex Voice's installer rather than writing its selection file. When it reports `restart_required: true`, restart the Orb through Codex Voice before judging the renderer. Binding is idempotent for one model; remove the project boundary before switching to a different model.

Publishing follows the current `CODEX_THREAD_ID` when that task is bound to
the model. With multiple bound sessions, use `--session-id` for an explicit
target; the runtime refuses an ambiguous broadcast. Use `--project-wide` only
when the legacy shared state is intentional.

At the start of an avatar-related task, inspect semantic context:

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project context --project <project-path> --format markdown
```

After a curated profile exists, opt in to per-turn context with `project context-hook enable --project <project-path>`. The hook is separate from Voice, preserves other Codex handlers, and silently no-ops outside a bound project.

## Change state

Use only action IDs from semantic context. Actions are independent expression toggles: `state set` replaces the complete desired set, while `enable` and `disable` adjust it. Publish to the intended session after a change and check host acceptance.

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" state set <model-id> <action-id> ...
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" state enable <model-id> <action-id>
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" state disable <model-id> <action-id>
```

## Clean up deliberately

```powershell
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" project uninstall --project <project-path> --yes
& "$HOME\.codex\skills\live2d-avatar-controls\scripts\live2d-avatar.ps1" model remove <model-id> --yes
```

Project uninstall removes only the verified project boundary and its owned external bundle. Model removal removes only the managed registry copy. Neither removes original archives or user-owned profile drafts.
