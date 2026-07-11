---
name: codex-voice
description: Set up, uninstall, and control project-local Kokoro voice output and the optional WebGL Strand Orb for Codex, with full voice, speed, volume, commentary-volume, playback, scope, progress, and provider configuration. Use when the user asks to enable, disable, configure, install, clean up, troubleshoot, or speak Codex responses aloud, including CPU, NVIDIA CUDA, or Intel DirectML provider selection.
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
macOS, then press and drag with the left mouse button. The window captures the
pointer only for that deliberate gesture and returns to click-through mode
when the drag ends. The position is saved per project; press `Escape` to
cancel a move in progress.

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
