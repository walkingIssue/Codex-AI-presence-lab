---
name: codex-voice
description: Set up, uninstall, and control project-local Kokoro voice output, the optional WebGL Strand Orb, and experimental custom avatar renderers for Codex, with full voice, speed, volume, commentary-volume, playback, scope, progress, and provider configuration. Use when the user asks to enable, disable, configure, install, clean up, troubleshoot, speak Codex responses aloud, or define a custom presence renderer, including CPU, NVIDIA CUDA, or Intel DirectML provider selection.
---

# Codex AI Presence

Use the bundled scripts from the active project directory. The setup is
project-local and does not modify other Python environments.

## Set up

If the active project has no `.codex-voice` directory, run:

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/setup.py"
```

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

```powershell
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --cuda
python "$HOME/.codex/skills/codex-voice/scripts/setup.py" --directml
```

CPU is the validated baseline. The NVIDIA CUDA path uses a separate
`.cuda-venv`, `CUDAExecutionProvider`, and the base INT8 model; it is included
for NVIDIA users but is untested on the maintainer's hardware. The DirectML
path uses a separate `.dml-venv` and a generated local graph patch. The setup
pulls the maintained [Intel Arc Kokoro fork](https://github.com/walkingIssue/kokoro-onnx-intel-arc/tree/intel-arc-directml).
Do not describe the DirectML patch as an upstream Kokoro contribution yet.

## Uninstall and clean up

To remove the project-local integration after a failed or superseded install, run:

```powershell
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

The project-local watcher is an adapter, not a second presentation owner. It
hands sanitized activity and visible-output envelopes to
`.codex-voice/presence_service.py`, which owns the local lifecycle boundary
and delegates all playback to the existing single inbox/playback arbiter.
Renderer-specific code stays behind the existing activity and avatar-state
bridges. A future host adapter should target Codex app-server events; ACP in
the adjacent research notes is Agentic Commerce Protocol and is not part of
this local runtime.

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
| Provider | `cpu`, `cuda`, or `directml` | `cpu` |
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

`configure.py` validates provider readiness before selecting CUDA or DirectML,
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

Codex rollout metadata automatically drives `thinking`, `tool`, `cli`, and
`idle`. A host adapter or skill can emit an explicit category through the
project-local bridge:

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
`avatar-capabilities.json`. A separate avatar-control skill can then write a
complete high-level action set through the generic managed writer:

```powershell
py .codex-voice/avatar_state.py write --project-root . `
  --avatar-id higan-live2d `
  --source live2d-avatar-controls `
  --scope project `
  --revision 12 `
  --actions-json '["pose.sweater-default", "effect.dazed-eyes"]'
py .codex-voice/avatar_state.py status --project-root .
py .codex-voice/avatar_state.py sync --project-root .
```

The voice layer forwards action ids only. The avatar-control runtime owns
action discovery, conflicts, safe defaults, and compiled model operations.
The full state is project-scoped in v0.1; an empty action list resets the
avatar, and older revisions are ignored.

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
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-directml
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" provider-status
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" progress-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" progress-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" orb-on
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" orb-off
python "$HOME/.codex/skills/codex-voice/scripts/toggle.py" status
```

When the Orb is running, hold `Ctrl+Alt` on Windows/Linux or `Cmd+Alt` on
macOS and use the left mouse button to drag it. The position is saved per
project; press `Escape` to cancel a move in progress. Voice input uses the
separate right-button hold described below.

The Orb window is resizable from its native transparent surface. Hold
`Ctrl+Alt+Shift` and drag from the lower-right corner to resize it; the gesture
works for the built-in Strand Orb and custom avatar renderers. The size is
saved alongside the position in `.codex-voice/orb-position.json`. The host
forwards the exact content `{width, height}` through `window-resize`; renderers
with `avatar-state-v1` keep Electron zoom at `1` and fit their own canvas so
Live2D and other high-resolution avatars do not become raster-scaled. Legacy
custom renderers without that capability may still use host scaling.

Report the resulting state briefly. The skill controls future responses; it
does not speak the current response directly.

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
py .codex-voice/voice_input.py settings --enabled on
py .codex-voice/voice_input.py status
py .codex-voice/voice_input.py settings --labels session-change --max-record-seconds 60 --lock-timeout-seconds 120
py .codex-voice/voice_input.py settings --delivery-mode clipboard
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
