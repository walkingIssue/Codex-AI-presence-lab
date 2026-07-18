---
name: codex-voice
description: Install, configure, inspect, migrate, troubleshoot, and uninstall the user-level Presence Runtime for Codex voice, speech input, semantic activity, and dynamically swappable built-in or Live2D avatars. Use for Kokoro providers, voiced updates, project defaults, session overrides, reusable profiles and presets, renderer geometry, migration, routing, or runtime health on Windows and Fedora.
---

# Codex AI Presence

Operate Presence Runtime v0.2 through the intent-level `presence` CLI. It is one
user-level Python supervisor with one warm Kokoro worker and one Electron root.
Projects register thin event adapters; they do not own models, environments,
profiles, workers, or renderers.

Never recreate the v0.1 architecture. Do not:

- edit `.codex-voice` ledgers as active configuration;
- copy models, Python environments, or Electron packages into a project;
- publish mutable Live2D `state.json` as session state;
- choose a renderer by profile id, UDP port, foreground window, or route key;
- uninstall a model or restart the renderer to change an avatar;
- pass provider or microphone policy as a project/session/profile field.

The root `live2d-avatar-runtime/` package is canonical in a source checkout.
Installed projections are generated artifacts, not editing targets.

## Locate the CLI

After installation, use `presence`. If it is not on `PATH`, use the managed
launcher under `$CODEX_HOME/bin` (`presence.cmd` or `presence.ps1` on Windows).
For a first source/projection install, bootstrap with:

```text
python <codex-voice-skill>/scripts/presence.py runtime install --provider cpu
```

Use Python 3.11 or 3.12. On this Windows machine, Intel Arc means the DirectML
provider:

```text
py -3.12 <codex-voice-skill>/scripts/presence.py runtime install --provider directml
```

Provider choices are `cpu`, `cuda`, `directml`, and `openvino`. DirectML is
Windows-only. Install optional speech input at the same user scope:

```text
presence runtime install --provider directml --with-input
```

Reinstalling preserves the selected provider, state, catalog, profiles,
bindings, and geometry unless the user explicitly changes or purges them.

## Register and enable a project

Registration is the only project setup step:

```text
presence project register C:\path\to\project
presence project status --project C:\path\to\project
```

The runtime launches a thin rollout adapter from its user-level installation.
Only its cursor and diagnostics live under the project’s
`.codex-voice/v0.2/`. Project registration watches sessions whose recorded cwd
matches that exact normalized root. Project moves are never guessed:

```text
presence project relocate --project C:\old\root C:\new\root
```

Use `presence project unregister --project PATH` to remove only this project’s
registration and managed adapter files. Shared runtime and catalog data remain.

## Always make mutation scope explicit

Project defaults apply to every session in a project. Session overrides are
sparse children. For a session mutation, provide both the project and canonical
session/thread UUID unless using a known binding id:

```text
presence session set --project PATH --session SESSION_UUID --voice af_heart
presence session set --project PATH --session SESSION_UUID --speed 1.1 --volume 60
presence session set --project PATH --session SESSION_UUID --playback-mode stream
```

Use the real `CODEX_THREAD_ID`/session UUID. Never invent or reuse an id from a
different project. The runtime creates the binding id and rejects caller-chosen
routing authority.

To voice visible progress updates for one session:

```text
presence session set --project PATH --session SESSION_UUID --progress-visible on --commentary-ratio 0.5
```

`commentary-ratio` is 0 through 1 and multiplies the main volume. Final and
commentary utterances are deduplicated by stable event id in the central queue.

Clear the whole child patch to restore project inheritance:

```text
presence session clear --project PATH --session SESSION_UUID
```

Clear named child fields to inherit those fields again:

```text
presence session clear --project PATH --session SESSION_UUID voice_id preset_ref
```

Omitted fields inherit. Scalars replace. Objects merge by field. Lists replace,
and an explicit empty list clears an inherited list. `null` is accepted only on
schema fields marked clearable. Invalid children reject atomically and preserve
last-known-good.

## Reusable profiles and project defaults

Profiles are machine-local catalog records. They may contain voice, speed,
playback mode, volume, commentary ratio, avatar/preset references, visibility,
and semantic curation. They may not contain provider or microphone policy.

```text
presence catalog profile import C:\profiles\higan-default.json
presence catalog profile list
presence project set-profile --project PATH higan-default
presence session set-profile --project PATH --session SESSION_UUID higan-default
presence project clear-profile --project PATH
```

Increasing a profile or preset revision creates a catalog revision. Multiple
local projects may reference the same reusable profile without copying it into
Git. Export portable JSON explicitly:

```text
presence catalog profile export higan-default C:\exports\higan-default.json
presence catalog preset export plain-sweater C:\exports\plain-sweater.json
```

## Import and swap avatars and presets

An avatar model pack declares an immutable model fingerprint, renderer assets,
semantic slots, actions, safe defaults, and capabilities. Import the validated
pack with its user-owned model directory:

```text
presence catalog avatar import C:\packs\higan-v2.json --assets C:\models\Higan
presence catalog avatar list
presence catalog avatar show higan
```

Original archives remain user-owned and are never deleted. Assets are copied
once into the user catalog, not into each project.

Swap dynamically without restart:

```text
presence avatar use higan --project PATH
presence avatar use higan --project PATH --session SESSION_UUID
presence avatar use builtin --project PATH --session SESSION_UUID
presence preset use plain-sweater --project PATH --session SESSION_UUID
```

The runtime validates the model/preset, resolves a candidate snapshot, commits
a new binding revision, preloads an invisible replacement, transfers geometry
and speaking/activity state, and destroys the old window only after renderer
acknowledgement. Failure keeps the previous window and last-known-good revision.
There is no implicit built-in fallback.

Catalog deletion refuses referenced entries unless force is explicit. Before
forcing, inspect bindings and rebind dependents; force must never delete the
original archive.

## Curate semantic states

Read the avatar’s public slot/action taxonomy first:

```text
presence catalog avatar show higan
```

Slots, exclusivity, and multi-slot claims come only from the model pack. Never
infer conflicts from filenames or words such as `arms`, `legs`, `pipe`, or
`sweater`.

Apply a validated sparse patch with `presence session set --patch FILE`. For
example, explicit empty arrays remove inherited shoulder/leg accessories and
activity rules add model-declared actions:

```json
{
  "semantic": {
    "slots": {
      "accessory.shoulders": [],
      "body.legs": []
    },
    "activity": {
      "thinking": {"add": ["pose.sweater-hand-mouth-left"]},
      "tool": {"add": ["pose.sweater-hand-mouth-right"]},
      "skill": {"add": ["pose.sweater-heart"]},
      "cli": {"add": ["pose.right-hand-pipe"]},
      "waiting": {"add": ["eyes.dazed"]},
      "error": {"add": ["mouth.unhappy", "effect.dark-face"]}
    }
  }
}
```

Only use action ids actually declared by the selected model. A higher-level
action evicts lower-precedence actions when any exclusive claimed slot
conflicts. Every activity overlay is recomputed from persistent state, so idle
must restore the exact clothing, accessory, pose, and prop selection.

For reusable curation, import a `presence/preset/v0.2` document with compatible
model fingerprints and bind that preset instead of repeating session patches.

## Voice input and window interaction

Speech input requires both installation and explicit machine permission:

```text
presence runtime install --with-input
presence runtime set-policy --enable-input
```

Hold Ctrl+Alt+right mouse on an active session avatar to record. Recording start
pauses that binding’s current voice; release stops capture, transcription is
delivered to the same binding, and buffered playback resumes cleanly. Escape,
modifier release, focus loss, window close, or a failed capture cancels safely.

Ctrl+Alt+left drag repositions a window. Ctrl+Alt+Shift+left drag resizes it.
Geometry is keyed by binding id and survives avatar swaps and runtime restarts.

## Inspect and diagnose the real path

Do not treat process markers or unit tests alone as proof. Check the effective
snapshot, authenticated source lease, queue, worker, renderer acknowledgement,
and live visible/audible result:

```text
presence inspect effective --project PATH --session SESSION_UUID --json
presence session show --project PATH --session SESSION_UUID
presence runtime doctor
presence runtime doctor --binding BINDING_UUID
presence runtime status
```

`inspect effective` includes field provenance and resolved semantic selections,
not raw model controls. `doctor` reports provider/worker health, STT permission,
binding state, renderer root/window acknowledgement, catalog references,
last-known-good revision, and project adapter diagnostics.

If a session is dormant, verify its adapter lease and exact project root. Do not
repair it by assigning another session’s profile/port/window. Restart the one
user runtime only when its health requires it:

```text
presence runtime restart
```

Queued utterances retain the voice/speed/mode/volume captured at enqueue, while
renderer routing follows the stable binding at playback. Removing a binding
cancels its queue; it never falls through to a sibling.

## Migration and rollback

The first v0.2 project registration automatically inspects and migrates v0.1
profiles, session patches, avatar selection, geometry, durable final speech,
and model definitions. Migration is locked, idempotent, and commits only after
worker and renderer health acknowledgement. Ephemeral legacy commentary is not
replayed. Failure preserves every v0.1 file and last-known-good state.

```text
presence migrate status --project PATH
presence migrate retry --project PATH
presence migrate rollback --project PATH
```

Treat legacy files as read-only rollback input throughout the compatibility
release. `toggle.py`, `profiles.py`, avatar tools, Live2D launchers, and
`setup.py` are warning-producing wrappers only; they must delegate to
`presence` and must not keep independent state.

## Uninstall boundaries

The machine-readable runtime manifest is the ownership authority. Its generated
`RUNTIME-MANIFEST.md` is for human review. Every component declares scope,
owner, dependencies, dependents, preserved data, and removal behavior.

```text
presence runtime uninstall
presence runtime uninstall --all-projects
presence runtime uninstall --purge-state
presence runtime uninstall --purge-catalog
```

Default uninstall preserves `state.sqlite3`, the catalog, profiles, presets,
bindings, geometry, and original user archives. Active sources block uninstall
unless `--all-projects` is explicit. State and catalog are independent purge
boundaries; never imply either from a normal uninstall.

For contract details, read `docs/adr/0002-presence-runtime-v0.2.md`, the schemas
under `presence-runtime/schemas/`, and `presence-runtime/RUNTIME-MANIFEST.md` in
a source checkout. On an installed projection, use its bundled equivalents.
