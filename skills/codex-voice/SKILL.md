---
name: codex-voice
description: Set up, uninstall, and control project-local Kokoro voice output, the optional WebGL Strand Orb, and integrated Live2D/Cubism avatar renderers for Codex, with full voice, speed, volume, commentary-volume, playback, scope, progress, provider, model-profile, and visual-state configuration. Use when the user asks to enable, disable, configure, install, clean up, troubleshoot, speak Codex responses aloud, import or bind a local Live2D model, or define a custom presence renderer, including CPU, NVIDIA CUDA, Intel OpenVINO, or Intel DirectML provider selection.
---

# Codex AI Presence

Use the bundled scripts from the active project directory. The setup is
project-local and does not modify other Python environments. In a development
checkout, `skills/codex-voice/` is source; `.codex-voice/` is generated runtime
state and `$CODEX_HOME/skills/codex-voice/` is an installed projection. Never
patch generated runtime copies as the source of truth.

## Set up

If the active project has no `.codex-voice` directory, run:

```sh
python "$HOME/.codex/skills/codex-voice/scripts/setup.py"
```

On Windows, the same command may be run from PowerShell. On Fedora/Linux,
setup creates a POSIX `start_voice.sh` wrapper and uses the virtualenv's
`bin/python` interpreter.

Use `--force` only when setup reports a different existing `.codex/hooks/speak.py`.
Use `--no-orb` when the machine should not install the optional Electron orb.
Setup selects Python 3.11 or 3.12 for the isolated environments because the
current Kokoro package pins do not support Python 3.13+. Use `--python PATH` to
choose a specific compatible interpreter when automatic selection is not
available.

After setup, ask the user which voice scope they want before enabling it:

> Should voice apply only to this Codex session, or to all sessions in this project?

Use `session-on` for the current session or `project-on` for all sessions whose
rollout belongs to the active project. Do not silently choose project-wide
voice. If the user already specified a scope, use that choice without asking
again.

Provider setup options:

```sh
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --cuda
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --openvino
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --directml
```

CPU is the validated baseline. The NVIDIA CUDA path uses a separate
`.cuda-venv`, `CUDAExecutionProvider`, and the base INT8 model; it is included
for NVIDIA users but is untested on the maintainer's hardware. The Intel
OpenVINO path uses `.openvino-venv`, `OpenVINOExecutionProvider`, and the base
INT8 model with the GPU device selected at session creation. The DirectML path
uses a separate `.dml-venv` and a generated local graph patch.
Do not describe the DirectML patch as an upstream Kokoro contribution yet.

### Refresh an existing installation

Use the managed refresh when the skill source changed but models, virtual
environments, provider selection, profiles, avatar curation, and geometry must
be preserved:

```sh
python /path/to/latest/codex-voice/scripts/setup.py --project-root . --refresh --force
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" runtime-restart
```

`--refresh` replaces only managed hooks, scripts, launchers, the Orb package,
and the runtime manifest. It reuses Orb dependencies when package manifests
are unchanged and removes known obsolete managed files from older layouts. It
does not download models, rebuild environments, change provider/settings,
copy another project's profiles or ledgers, or create a new global worker.
Run it once per project runtime that should receive the revision. Project the
same canonical skill revision into `$CODEX_HOME/skills/codex-voice/` before
restarting watchers, because watcher and global-arbiter code is loaded from
the installed skill.

## Uninstall and clean up

To remove the project-local integration after a failed or superseded install, run:

```sh
python "$HOME/.codex/skills/codex-voice/scripts/uninstall.py" --yes
```

The uninstaller stops the watcher and Orb, removes only the registered Codex
voice Stop hook, restores a hook backed up by setup, and removes the local
`.codex-voice` directory with its models and virtual environments. It refuses
to remove a changed `speak.py` unless `--force` is supplied. Use
`--keep-assets` when only the hook and runtime markers should be removed while
retaining downloaded models and environments.

Every projected revision includes `RUNTIME-MANIFEST.md`. Setup copies it to
`.codex-voice/RUNTIME-MANIFEST.md`; it records the project-local files and the
managed hook boundary. New runtime artifacts must be added to that manifest
in the same PR or push. Full uninstall removes the entire `.codex-voice`
boundary, so newly added files inside it are cleaned up automatically.

## Unified presence runtime

The project-local watcher is an adapter, not a presentation or playback owner.
It hands sanitized activity and visible-output envelopes to the user-level
global arbiter in `scripts/global_arbiter.py`. That arbiter owns one
serialized attention queue, one spoken-session owner, one session-transition announcement
policy, and one warm Kokoro worker for every connected project and session.
Renderer-specific code stays behind the existing activity and avatar-state
bridges. Future Codex app-server, Agent Client Protocol (ACP), and other host
adapters should all target this same arbiter instead of introducing another
speech or presentation owner.

Each project Orb still has its own isolated localhost UDP endpoint. That is a
renderer delivery address only; it does not create another worker. Every
speech request carries its session/profile route and target Orb port through
the global arbiter.

The routing identity is the full tuple `(project_root, orb_port, session_id,
profile_id, route_key)`. Voice output, coarse activity, and semantic avatar
state must resolve from the same owning project and exact composite route.
Never repair a route mismatch by adding the foreign session to a second
project's `presence-profiles.json`; that creates a duplicate visual while
speech remains attached to the original project's Orb endpoint.

The first enabled watcher starts the user-level arbiter daemon under the
active user's Codex voice state directory; later watchers connect to its
localhost socket. Do not start `speak.py --server` manually and do not create
a project-specific Kokoro worker. Stopping one project watcher deliberately
leaves the global worker available to the remaining sessions.

## Integrated Live2D runtime

Live2D model import, semantic profiles, renderer bundle materialization, state
publication, project lifecycle, and bounded context inspection are part of this
skill. The runtime package is bundled under `live2d-avatar-runtime/` in a
project checkout and in projected skill releases; it is not a separately
installed skill or global Python dependency.

Use the unified launcher from the active project or installed skill:

```sh
python skills/codex-voice/scripts/live2d-avatar.py project doctor --project . --json
python skills/codex-voice/scripts/live2d-avatar.py model import <zip-or-folder> --id <model-id>
python skills/codex-voice/scripts/live2d-avatar.py model profile scaffold <model-id> --output <user-owned-profile.json>
python skills/codex-voice/scripts/live2d-avatar.py project bind --project . --model <model-id> --profile <user-owned-profile.json>
python skills/codex-voice/scripts/live2d-avatar.py project publish --project .
python skills/codex-voice/scripts/live2d-avatar.py project voice-status --project . --json
```

On Windows use `scripts/live2d-avatar.ps1`; on Fedora/Linux the equivalent
`scripts/live2d-avatar.sh` is available. The launcher resolves the bundled
package in both a source checkout and a projected skill release. Read
[`references/live2d-manifest-and-state.md`](references/live2d-manifest-and-state.md)
before changing model profiles or lifecycle behavior.

The Live2D runtime owns imported model copies, generated manifests, curated
profiles, project bindings, and materialized renderer bundles. Codex Voice owns
the Orb, TTS, generic semantic state writer, and state delivery. Only semantic
action IDs cross that boundary; compiled Cubism operations, model paths,
expression filenames, hotkeys, textures, and raw controls remain renderer-local.
The runtime keeps the historical `live2d-avatar-controls` source identifier in
state envelopes for compatibility; it does not denote a second installed skill.

For a reviewed model, `project bind` is the visual integration path: it applies
the profile, materializes the bundled Pixi/Cubism renderer, installs the bundle
through Codex Voice's validated avatar installer, selects it, and reports when
the Orb must restart. Use `state set`, `state enable`, or `state disable` with
semantic IDs, then `project publish` to apply the complete desired toggle set.
`project uninstall` removes only the Live2D project boundary and bundles owned
by it; the unified voice uninstaller preserves the global model registry,
user-owned profile drafts, and unmarked avatar bundles.

## Codex TUI/server bridge (experimental)

The project runtime includes a transparent JSONL bridge for a Codex TUI or
custom app-server client. It forwards the child server protocol unchanged and
observes only visible assistant-message deltas. The bridge converts those
deltas into `start`, `delta`, `finish`, and `cancel` packets for one Kokoro
worker seam; reasoning, tool payloads, commands, and paths are never routed to
speech.

The default worker is an in-memory mock, so the transport can be exercised
before the inference worker is ready:

```sh
python .codex-voice/tui_bridge.py --server-command "mock-server --stdio"
```

Pass `--worker-command` only for a deliberately custom bridge or transport
test. The stock launcher does not use it: it places completed visible output
in the project-local adapter inbox, where the global arbiter sends it through
the one already-warm Kokoro worker shared by all sessions and projects. For
the stock Fedora TUI, use the installed launcher:

```sh
.codex-voice/launch_codex.sh
```

It starts a local app-server WebSocket, launches the normal Codex TUI with
`--remote`, forwards the protocol unchanged, and routes only visible
assistant-message deltas to the project-local adapter inbox. The rollout
watcher forwards that envelope to the global arbiter; it does not start a
Kokoro process of its own.

The launcher may also be bound to the global `codex` command. It uses the
working directory (or `CODEX_PRESENCE_PROJECT_ROOT`) to select the project and
passes that project’s `.codex-voice` directory (or
`CODEX_PRESENCE_VOICE_ROOT`) explicitly. The wrapper does not need to be
copied into each project, but each project that should use presence must have
its own configured `.codex-voice` runtime and managed `.codex/hooks/speak.py`.
Commands that directly target Codex administration or `app-server` bypass the
presence wrapper and go to the real Codex binary.

## Configuration

When the user asks what can be changed, run the complete matrix first:

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/configure.py" show
```

| Setting | Values | Default |
| --- | --- | --- |
| Voice / timbre | Any installed Kokoro voice ID, such as `bf_isabella` | `bf_isabella` |
| Speed | `0.5` to `2.0` | `1.08` |
| Playback | `stream` or `quality` | `stream` |
| Provider | `cpu`, `cuda`, `openvino`, or `directml` | `cpu` |
| Volume | `0` to `100` percent | `20` percent |
| Commentary volume | `0` to `100` percent of the main volume | `50` percent |
| Visible progress | `on` or `off` | `off` |
| Strand Orb | `on` or `off` | optional/off |
| Scope | `session`, `project`, or `off` | chosen at enable time |
| Voice input | `on` or `off` | `off` |
| Input gesture | `hold-ctrl-alt-right` | `hold-ctrl-alt-right` |
| Input delivery | `clipboard` or `app-server` | `clipboard` |
| Session lock | `through-response` | `through-response` |
| Session labels | `off`, `first-message`, or `every-message` | `off` |
| Maximum recording | `1` to `60` seconds | `60` seconds |

Use the deterministic command for direct changes, or `interactive` for a
guided pass through every setting:

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/configure.py" interactive
python "$HOME/.codex/skills/codex-voice/scripts/configure.py" set --voice bf_isabella --speed 1.08 --mode stream --volume 20 --commentary-volume 50
```

`configure.py` validates provider readiness before selecting CUDA, OpenVINO, or DirectML,
and `scope session` requires the current `CODEX_THREAD_ID`. Visible progress
uses the configured commentary-volume ratio of the main response volume.
Environment variables such as
`CODEX_TTS_VOICE` and `CODEX_TTS_SPEED` override project markers for advanced
use; prefer the configure command for normal project-local changes.

## Orb activity states

The Orb has a separate coarse activity channel for work that is not speech.
It never sends the underlying reasoning, tool name, command, arguments, or
paths to the renderer.

| State | Visual role |
| --- | --- |
| `idle` | Calm cyan baseline |
| `thinking` | Slow indigo/violet breathing |
| `tool` | Amber external-tool pulse |
| `skill` | Magenta integration pulse |
| `cli` | Green local-command pulse |
| `waiting` | Dim blue waiting halo |
| `error` | Short red warning pulse |

Codex rollout metadata automatically drives `thinking`, `tool`, `skill`,
`cli`, and `idle`. Current rollout records use `response_item/reasoning` for
thinking, tool-call names for tool/skill/CLI classification, and completion
events for idle. Waiting and error remain available for explicit host events
or adapter calls; they are not inferred from arbitrary tool output:

MCP invocation, web search, and external function/tool work use `tool`; there
is no provider-specific `mcp-invocation` renderer state. `speaking` is a
separate playback lifecycle event, not an activity state. The full packet,
TTL, routing, and privacy contract is in
[references/PRESENCE-EVENT-API.md](references/PRESENCE-EVENT-API.md).

```powershell
python .codex-voice/activity.py skill
python .codex-voice/activity.py cli --ttl-ms 5000
python .codex-voice/activity.py idle
```

Use `--project-root PATH` when the command is launched outside the project.
Activity packets expire automatically, and the Orb falls back to `idle` if a
watcher or adapter disappears. Activity is independent from audio playback;
the speaking waveform takes visual priority while Kokoro is playing.

## Custom avatar renderers (experimental)

The skill has a versioned, host-neutral renderer contract for users or agents
who want to create a different presence instead of the built-in Strand Orb.
Read [references/PRESENCE-EVENT-API.md](references/PRESENCE-EVENT-API.md) before
creating or reviewing an avatar, and use the files in
`assets/avatar-template/` as the smallest working starting point. Validate the
manifest against [references/avatar-manifest.schema.json](references/avatar-manifest.schema.json).

The renderer receives sanitized local events for activity, speaking state,
audio amplitude, and spectral bands from which it can derive cadence. It does
not receive hidden reasoning, raw tool names or arguments, file paths, secrets,
or arbitrary host APIs. Renderer code runs in the isolated Electron page with
context isolation and Node integration disabled.

An avatar may also advertise `avatar-state-v1` and include a sibling
`avatar-capabilities.json`. The integrated Live2D path writes a complete
high-level action set through the generic managed writer:

```powershell
py .codex-voice/avatar_state.py write --project-root . `
  --avatar-id higan-live2d `
  --source live2d-avatar-controls `
  --scope project `
  --revision 12 `
  --actions-json '["pose.sweater-default", "effect.dazed-eyes"]'
py .codex-voice/avatar_state.py write --project-root . `
  --avatar-id higan-live2d `
  --source live2d-avatar-controls `
  --scope route `
  --session-id <session-id> `
  --profile-id <profile-id> `
  --revision 1 `
  --actions-json '["pose.sweater-default"]'
py .codex-voice/avatar_state.py status --project-root .
py .codex-voice/avatar_state.py sync --project-root .
```

The voice layer forwards action ids only. The avatar-control runtime owns
action discovery, conflicts, safe defaults, and compiled model operations.
The legacy v0.1 state is project-scoped. Routed v0.2 states are keyed by the
same composite session/profile route used by Presence Service, persisted in
`avatar-states.json`, and delivered only to that exact avatar window. An empty
action list resets the target avatar, and revisions are monotonic per route.

Avatar state is runtime-local presentation state. Do not append its status,
active-action list, available-action list, or acceptance diagnostics to normal
turn context or to the end of user turns. Use `avatar_state.py status` or
`avatar_state.py sync` only when explicitly requested, when a host/classifier
asks for it, or while diagnosing the bridge. State remains fully controllable
through the `write` command; this policy only removes the automatic context
noise.

Install and select a bundle without replacing the built-in renderer:

```powershell
python .codex-voice/avatar.py validate --source .\my-avatar
python .codex-voice/avatar.py install --source .\my-avatar --use
python .codex-voice/avatar.py list
python .codex-voice/avatar.py use builtin
python .codex-voice/avatar.py remove my-avatar
```

Custom source is stored in the project-owned `.codex-voice-avatars/` directory;
the managed `.codex-voice/` runtime stores the active selection marker and the
generic avatar-state bridge snapshot/diagnostic files. The runtime manifest
records those files and the uninstaller removes them with the voice runtime;
the user-owned avatar source remains intact.
Restart the Orb after changing the selection. The loader validates the bundle
path and falls back to the built-in renderer if the manifest or entry is
invalid. Skill uninstall removes the managed runtime but leaves
`.codex-voice-avatars/` intact.

## Session presence profiles (experimental)

Profiles bind a high-level avatar identity and Kokoro voice/speed/mode to a
session. Presence Service resolves the profile before enqueueing speech, the
durable inbox snapshots those routing fields, and the existing single
global PlaybackArbiter sends them to the one existing warm Kokoro worker per
request. Profiles do not create another worker or put identity data into
spoken text. Provider/model selection belongs to the global worker owner, not
to an individual session.

Create profiles and bind sessions with the managed project-local command:

```powershell
py .codex-voice/profiles.py --project-root . set sol --avatar-id builtin --voice af_heart --speed 1.0 --mode stream
py .codex-voice/profiles.py --project-root . set luna --avatar-id higan-live2d --voice bf_isabella --speed 1.2 --mode stream
py .codex-voice/profiles.py --project-root . bind $env:CODEX_THREAD_ID luna
py .codex-voice/profiles.py --project-root . default sol
py .codex-voice/profiles.py --project-root . list
```

Bind in this order:

1. Change to the project that owns the Codex session and run `session-on`.
2. Create/update the profile in that same project's `.codex-voice` runtime.
3. Bind the current `CODEX_THREAD_ID` with an explicit `--project-root`.
4. Run `profiles.py resolve --session-id <id>` and verify `avatar_id`,
   `profile_id`, and `route_key` before publishing semantic state.
5. Publish Live2D state from the same project, then run `runtime-restart`.

In session scope, `profiles.py bind` rejects an ID that is not enabled in the
target project or whose session registry names a different project root.
Do not copy `presence-profiles.json`, `sessions.json`, `avatar-state*.json`, or
`.codex-live2d/avatar-state-revisions.json` between projects. To move a live
session, unbind it from the old project, enable and bind it in the owning
project, and republish its desired action set to the new exact route.

The canonical file is `.codex-voice/presence-profiles.json`. Resolution order
is an explicit profile requested at the Presence boundary, then the session
binding, then `project_profile_id`, then legacy project voice/avatar defaults.
`session_id`, `thread_id`, `turn_id`, `profile_id`, and `avatar_id` remain
separate fields. The spoken-attention owner is the composite
`session:<id>|profile:<id>` route.

Restart the project runtime after adding or removing bindings. It creates one
transparent window per explicitly bound session that is also enabled by that
project's session registry. Inactive or foreign bindings fail closed and
cannot create a fallback/default window. If there are no explicit bindings,
one enabled session may use the project profile as a legacy single-window
fallback. Session-scoped activity goes to that session's avatar; unscoped
Kokoro amplitude/state packets go only to the most recent exact
`voice-output` owner. Holding `Ctrl+Alt`/`Cmd+Alt` and the right mouse button on
any profile avatar targets voice input to that avatar's bound session.

The host budgets animation callbacks before renderer scripts load: 60 FPS by
default while idle, speaking, recording, applying avatar state, or interacting. Override with
`CODEX_ORB_IDLE_FPS` and `CODEX_ORB_ACTIVE_FPS`; set
`CODEX_ORB_FRAME_LIMIT=off` only for renderer diagnosis.

## Controls

Run the requested operation:

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" session-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" session-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" project-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" project-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" on             # alias for session-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" off            # alias for project-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" stream
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" quality
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-cpu
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-cuda
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-openvino
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-directml
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-status
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" progress-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" progress-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" orb-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" orb-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" runtime-restart
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" status
```

When the Orb is running, hold `Ctrl+Alt` on Windows/Linux or `Cmd+Alt` on
macOS and use the left mouse button to drag any rendered profile. Each
session/profile window saves its own position inside the project-local
`orb-position.json`; press `Escape` to cancel a move in progress. Voice input uses the
separate right-button hold described below.

For a more reliable gesture—especially when the transparent window is not
focused—press `Ctrl+Alt+M` on Windows/Linux or `Cmd+Option+M` on macOS to
toggle move mode, then drag with the left button. Press the same shortcut again
to return to click-through mode. Press `Ctrl+Alt+Shift+K` (or
`Cmd+Option+Shift+K`) to arm one resize operation, then drag the lower-right
corner; resize mode exits automatically after the resize. On native Wayland,
global shortcut support depends on the compositor's Global Shortcuts portal;
the original hold-modifier gesture remains available as a fallback. On
Fedora/Linux that fallback is guarded by the Linux platform gate: hold
`Ctrl+Alt+Shift` while dragging the lower-right corner. Integrated Live2D
renderers receive the same Linux platform flag and draw the resize border and
corner affordance themselves, so the model page remains visually consistent
with the host window. Entering either shortcut mode now focuses and raises the
transparent window for the edit, shows an active border, and restores
click-through behavior when the mode ends.

The shortcut request targets one exact renderer window, including when that
window belongs to another project-local Orb process. On a Wayland desktop,
press `Super` to open the compositor overview and select the renderer first;
that compositor focus drives an explicit `idle -> selected -> armed -> active
-> idle` interaction state machine. The selected renderer remains the only
eligible shortcut target until the operation completes, loses focus, or is
cancelled. If no renderer is selected, the shortcut deliberately does nothing
instead of falling back to the first window, stale hover, or compositor cursor
coordinates. Wayland renderers are included in the desktop overview for this
selection flow. X11, Windows, and macOS retain pointer targeting. The desktop
shortcut owner forwards the command with its session/profile window key over
the local Orb interaction channel and clears any previously armed renderer.
This is independent of the voice/Kokoro arbiter and keeps resize and movement
attached to one explicitly selected visual window.

The Orb window is resizable from its native transparent surface. Hold
`Ctrl+Alt+Shift` and drag from the lower-right corner to resize it; the gesture
works for the built-in Strand Orb and custom avatar renderers. The size is
saved alongside that window's routed position in `.codex-voice/orb-position.json`. The host
forwards the exact content `{width, height}` through `window-resize`; renderers
with `avatar-state-v1` keep Electron zoom at `1` and fit their own canvas so
Live2D and other high-resolution avatars do not become raster-scaled. Legacy
custom renderers without that capability may still use host scaling.

The skill controls future responses; it does not speak the current response
directly. Do not report model status unless the user explicitly asks for it or
the operation failed and the diagnostic is needed to explain the failure.

`session-on` registers the current `CODEX_THREAD_ID` in the project-local
`.codex-voice/sessions.json` file. `session-off` removes only the current
session. `project-on` is the explicit always-on mode for every matching
session in the project. The registration file is runtime state and is ignored
by the generated `.codex-voice/.gitignore`.

`stream` starts playback as Kokoro chunks arrive. `quality` buffers the full
waveform first. Visible progress commentary is optional and uses the configured
commentary-volume ratio; never speak hidden reasoning or raw tool output.

The watcher uses the persistent worker and the provider selected in the
project's `.codex-voice/provider` marker. Keep the base model and voice bundle
out of source control; setup downloads them locally.

## Optional voice input

Voice input is disabled by default. Install the local speech-to-text runtime
with setup's explicit opt-in flag, then enable the input layer only after the
user has chosen to allow microphone capture:

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --with-input
py .codex-voice/voice_input.py --voice-root .codex-voice settings --enabled on
py .codex-voice/voice_input.py --voice-root .codex-voice status
py .codex-voice/voice_input.py --voice-root .codex-voice settings --labels session-change --max-record-seconds 60 --lock-timeout-seconds 120
py .codex-voice/voice_input.py --voice-root .codex-voice settings --delivery-mode clipboard
```

While the Orb is speaking, hold `Ctrl+Alt` and press the right mouse button.
Recording starts immediately, pointer movement is ignored, and releasing the
right button or either modifier ends that audio chunk. `Ctrl+Alt+Shift` resize
remains separate, and `Escape` cancels an active recording. Capture stops the
disposable OS audio sink immediately and gates the playback queue for as long
as the gesture is held. Kokoro inference remains alive in its persistent worker
and continues filling a frame-buffered PCM queue; no model request is killed or
requeued. Releasing the gesture resumes that same buffered stream and starts
local transcription independently. Clipboard delivery therefore becomes ready
as soon as STT finishes; it never waits for the assistant audio to finish.
Real session output is durable in `.codex-voice/inbox.sqlite3` and drains
afterward. Visible progress commentary is an ephemeral, latest-only update
lane: it never enters the inbox, only the current attention session's updates
are eligible, and a real message always preempts an update without replaying
stale commentary.

The input layer uses one lazily prewarmed `faster-whisper` worker under
`.codex-voice/.stt-venv` and deletes temporary recordings after transcription.
Every capture, WebM handoff, STT job, UI state, and clipboard write carries the
same durable sequence number. Jobs drain in order, and a superseded result is
never allowed to overwrite the clipboard for a newer recording.
Raw audio is never sent to Codex. `clipboard` delivery is the safe default for
Codex GUI-originated sessions: the transcript is copied to the local
clipboard, the Orb displays `Copied — paste into Codex`, and playback resumes
without attempting to automate the private GUI. `app-server` delivery is an
explicit opt-in for sessions launched through the supported App Server/TUI
wrapper; it uses `thread/resume` plus `turn/start`, or `turn/steer` when the
resumed thread reports an active turn. The runtime never pastes or sends GUI
text on the user's behalf.

Configure labels and timing in `.codex-voice/input.json`:

```json
{
  "input_enabled": true,
  "input_gesture": "hold-ctrl-alt-right",
  "session_lock": "through-response",
  "session_labels": "session-change",
  "session_label_template": "{session_name} says",
  "max_record_seconds": 60,
  "lock_timeout_seconds": 120
}
```

The label is added only to synthesized playback, not to stored assistant text
or the submitted user transcript. `session-change` speaks the human-readable
session name only when a real message changes the attention owner; it is
persistent across watcher restarts. `first-message` remains accepted as a
backward-compatible alias, and `every-message` is available when explicit
labels are desired. The inbox and all input runtime artifacts are listed in
`RUNTIME-MANIFEST.md` and are removed by uninstall; unrelated Codex hooks and
user-owned avatar bundles are preserved.
