---
manifest_schema: 1
manifest_revision: 2026-07-16-profile-curation-renderer
release_unit: codex-voice
---

# Codex AI Presence runtime manifest

This manifest is shipped with every projected `codex-voice` skill revision
and is copied into the project-local `.codex-voice` runtime during setup. It
is the inventory for files owned by the integration, including the managed
Codex hook boundary and the bundled Live2D runtime seam. The Live2D package is
part of the installable skill payload; it is not a second installed skill.

Update this file in the same PR or push whenever a new runtime artifact,
generated directory, managed hook, or cleanup rule is added. The uninstaller
removes the complete `.codex-voice` runtime boundary and removes only the
registered Codex AI Presence hook entry from `.codex/hooks.json`. The manifest
makes that ownership reviewable and remains useful for older installs that
predate a later file.

## Registered runtime artifacts

| Artifact | Project-relative path or pattern | Cleanup owner |
| --- | --- | --- |
| Runtime root | `.codex-voice/` | Uninstaller removes the complete directory |
| Runtime manifest | `.codex-voice/RUNTIME-MANIFEST.md` | Runtime-root cleanup |
| Activity bridge | `.codex-voice/activity.py` | Runtime-root cleanup |
| Global playback arbiter client | `skills/codex-voice/scripts/global_arbiter.py` | Skill projection/reinstall; the user-level daemon remains shared across project runtimes |
| Isolated Orb endpoint | Derived from the canonical `.codex-voice` path; `CODEX_ORB_PORT` may override it | No additional artifact |
| Codex TUI/server bridge | `.codex-voice/tui_bridge.py` | Runtime-root cleanup |
| Stock TUI launcher | `.codex-voice/launch_codex.py` | Runtime-root cleanup |
| Stock TUI launcher wrapper | `.codex-voice/launch_codex.sh` | Runtime-root cleanup |
| TUI Kokoro worker | `.codex-voice/tui_kokoro_worker.py` | Runtime-root cleanup |
| Avatar manager | `.codex-voice/avatar.py` | Runtime-root cleanup |
| Avatar state writer | `.codex-voice/avatar_state.py` | Runtime-root cleanup |
| Avatar selection | `.codex-voice/avatar-selection.json` | Runtime-root cleanup |
| Avatar state snapshot | `.codex-voice/avatar-state.json` | Runtime-root cleanup |
| Routed avatar state ledger | `.codex-voice/avatar-states.json` | Runtime-root cleanup |
| Avatar state temporary writes | `.codex-voice/.avatar-state.json.*.tmp` | Runtime-root cleanup |
| Routed avatar state temporary writes | `.codex-voice/.avatar-states.json.*.tmp` | Runtime-root cleanup |
| Avatar state diagnostics | `.codex-voice/avatar-state-status.json` | Runtime-root cleanup |
| Routed avatar state diagnostics | `.codex-voice/avatar-state-statuses.json` | Runtime-root cleanup |
| Routed avatar diagnostics temporary write | `.codex-voice/avatar-state-statuses.json.tmp` | Runtime-root cleanup |
| Presence profile, session, and settings helpers | `.codex-voice/{profiles.py,session_scope.py,configuration.py}` | Runtime-root cleanup |
| Presence profiles and session bindings | `.codex-voice/presence-profiles.json` | Runtime-root cleanup |
| Managed runtime refresh | `setup.py --project-root <project> --refresh --force` | Replaces only skill-owned runtime code and removes obsolete managed root files while preserving provider, models, environments, profiles, sessions, and user state |
| Voice lifecycle wrappers | `.codex-voice/{start_voice.ps1,start_voice.sh}` | Runtime-root cleanup |
| Configuration markers | `.codex-voice/{voice,mode,speed,volume,commentary-volume,provider,progress,enabled,orb.enabled}` | Runtime-root cleanup |
| Voice-input settings | `.codex-voice/input.json` | Runtime-root cleanup |
| Durable voice inbox | `.codex-voice/inbox.sqlite3*` | Runtime-root cleanup |
| Temporary recordings | `.codex-voice/inbox/recordings/` | Runtime-root cleanup |
| Presence service and voice-input helpers | `.codex-voice/{presence_service.py,inbox.py,voice_input.py,stt.py,delivery.py,clipboard.py}` | Runtime-root cleanup |
| Local STT runtime | `.codex-voice/.stt-venv/` | Runtime-root cleanup |
| Local STT model cache | `.codex-voice/stt-models/` | Runtime-root cleanup |
| Playback pause markers and cursor | `.codex-voice/{tts-player.pid,tts-stop.request,tts-resume.request,tts-progress.json,input.pid,input.log}` | Runtime-root cleanup |
| Session scope | `.codex-voice/sessions.json` | Runtime-root cleanup |
| Kokoro models | `.codex-voice/kokoro-v1.0*.onnx`, `.codex-voice/voices-v1.0.bin` | Runtime-root cleanup |
| Provider patch | `.codex-voice/gpu_patch/` | Runtime-root cleanup |
| Python environments | `.codex-voice/{.venv,.cuda-venv,.dml-venv,.openvino-venv}/` | Runtime-root cleanup |
| Orb package | `.codex-voice/orb/` and `.codex-voice/orb/node_modules/` | Runtime-root cleanup |
| Orb position and size | `.codex-voice/orb-position.json` | Runtime-root cleanup |
| Runtime traces | `.codex-voice/*.log`, `.codex-voice/*.pid`, `.codex-voice/*.wav` | Runtime-root cleanup |
| Managed hook | `.codex/hooks/speak.py` | Hook cleanup with ownership check |
| Hook backup | `.codex/hooks/speak.py.codex-voice-backup.py` | Hook cleanup / restore |
| Hook registration | `.codex/hooks.json` managed `Stop` entry only | JSON-aware hook cleanup |
| Bundled Live2D runtime | `skills/codex-voice/live2d-avatar-runtime/` | Skill projection/reinstall; not project-runtime cleanup |
| Unified Live2D launcher | `skills/codex-voice/scripts/live2d-avatar.{py,sh,ps1}` | Skill projection/reinstall; not project-runtime cleanup |
| Live2D model registry | `~/.codex/live2d-models/<model-id>/` | Live2D `model remove`; preserved by voice uninstall |
| Live2D project boundary | `<project>/.codex-live2d/` | Live2D `project uninstall`; preserved by voice uninstall |
| User avatar source | `.codex-voice-avatars/<avatar-id>/` | User-owned; not removed by skill uninstall |

## Revision ledger

| Revision | Runtime change | Cleanup impact |
| --- | --- | --- |
| `2026-07-12-activity-state` | Added rollout activity bridge, Orb activity states, and project-local `activity.py` | All new files remain inside `.codex-voice`; no new external cleanup path |
| `2026-07-12-activity-node` | Added a state-colored center node with a damped activity-swap bounce | No new artifact; renderer update remains inside `.codex-voice/orb/` |
| `2026-07-12-avatar-loader` | Added project-owned avatar bundle selection and a validated runtime loader | Selection marker is cleaned with the runtime; avatar source remains user-owned |
| `2026-07-12-avatar-state-bridge` | Added the model-agnostic full-state writer, Orb file watcher, preload delivery, and acceptance diagnostics | State writer, snapshot, temporary writes, and status file remain inside `.codex-voice` and are removed with the runtime |
| `2026-07-12-resizable-orb-window` | Added a persistent native resize gesture and host scaling for custom renderers | Size remains in the existing owned `orb-position.json`; no new cleanup boundary |
| `2026-07-12-avatar-local-resize` | Kept browser zoom at 1 for `avatar-state-v1` renderers and forwarded exact content dimensions for renderer-local fitting | No new artifact; avatar canvas sizing remains renderer-owned |
| `2026-07-13-voice-input-inbox` | Added opt-in Orb microphone capture, local STT, durable message inbox, playback arbiter, App Server delivery adapter, focus lock, and spoken session labels | New inbox, recording, STT, input-state, delivery, and interruption artifacts are project-local and removed by runtime cleanup; user avatar bundles and unrelated hooks remain protected |
| `2026-07-13-voice-input-recovery` | Requeued interrupted playback and released stale input state after watcher restart; cleared stale stop markers and orphan recordings | Recovery only touches the manifest-listed inbox/input artifacts and leaves avatar bundles and unrelated hooks protected |
| `2026-07-13-voice-input-conversational` | Switched recording to immediate Ctrl/Cmd+Alt + right-button hold, disabled spoken labels by default, accumulated released chunks, and resumed interrupted playback from a best-effort text cursor before local STT | Adds only the manifest-listed playback cursor; recordings remain temporary and are deleted after transcription, cancellation, timeout, or restart |
| `2026-07-13-unified-presence-service` | Added the renderer-neutral Presence Service boundary; the watcher now delegates activity, speech enqueueing, lifecycle, and completion draining through the existing single playback arbiter | Adds `.codex-voice/presence_service.py`; it is project-local and removed with the runtime |
| `2026-07-13-voice-input-clipboard` | Made clipboard delivery the safe default for GUI-originated sessions, added explicit App Server opt-in, and centralized built-in Orb gesture handling in the shared preload | Adds `.codex-voice/clipboard.py`; the helper remains inside the runtime boundary and is removed with it |
| `2026-07-13-pauseable-pcm-playback` | Decoupled executor-backed Kokoro inference from a frame-paced ffplay consumer so voice capture can terminate only the OS sink, buffer PCM during the hold, and resume without restarting or requeueing the model request | Adds the owned `tts-resume.request` marker; stop/resume/PID markers are removed by runtime cleanup |
| `2026-07-13-pauseable-pcm-tail-drain` | Uses one cumulative playback deadline to compensate both early Windows timer wakeups and per-frame overhead, and allows ffplay enough bounded time to drain EOF so resumed speech cannot lose its final buffered seconds | No new artifact or cleanup boundary |
| `2026-07-13-stateful-attention-updates` | Keeps real output in the durable inbox, routes commentary through a coalesced ephemeral update lane, persists the session that owns spoken attention, and retires legacy commentary rows on restart | Reuses the existing inbox/runtime-state database; no new cleanup boundary |
| `2026-07-13-adaptive-render-budget` | Caps renderer animation work at 20 FPS idle and 30 FPS active before custom avatar scripts load; values can be overridden or disabled through `CODEX_ORB_*` environment settings | Adds host modules inside the existing `.codex-voice/orb/` boundary; no external cleanup path |
| `2026-07-13-session-presence-profiles` | Resolves session-bound avatar and Kokoro voice/speed/mode identity at Presence Service, snapshots routing fields in the durable inbox, and materializes one Electron avatar window per bound session while retaining one playback arbiter and worker | Adds the owned `presence-profiles.json`, `profiles.py`, and `configuration.py` files inside `.codex-voice`; user avatar bundles remain protected |
| `2026-07-13-routed-avatar-windows` | Adds independently persisted session/profile avatar state, acceptance diagnostics, and window geometry; enables drag/resize on every rendered profile; raises the default idle and active animation budgets to 60 FPS after the shared Cubism renderer optimization; and moves ordered voice-control subprocesses and recording writes off Electron's main loop while removing the synchronous frame-policy IPC handshake | Adds routed `avatar-states.json` and `avatar-state-statuses.json` ledgers inside `.codex-voice`; the async control helper remains inside the existing Orb package, and per-window geometry remains in the existing `orb-position.json` artifact and cleanup boundary |
| `2026-07-14-fedora-voice-seam` | Adds platform-aware virtualenv paths, a POSIX watcher launcher, a POSIX voice lifecycle wrapper, and the Linux OpenVINO provider environment | The POSIX wrapper and OpenVINO environment are inside `.codex-voice`; no new cleanup boundary |
| `2026-07-14-tui-bridge-seam` | Adds a transparent Codex TUI/server JSONL proxy and an injectable mock Kokoro worker contract; only visible assistant deltas cross the voice seam | The bridge remains inside `.codex-voice`; no new external cleanup boundary |
| `2026-07-15-fedora-tui-launch` | Adds the stock Fedora/Linux TUI launcher and project-root forwarding contract; visible output is handed to the global singleton arbiter rather than creating a per-session Kokoro worker | Launcher remains inside `.codex-voice`; the global entrypoint shim is user-managed and is not removed by project voice uninstall |
| `2026-07-15-linux-window-controls-focus` | Makes Linux move/resize mode transitions edge-triggered, focuses and raises the transparent window while editing, restores click-through on exit, exposes an active mode border, and reports systemd-backed Orb status correctly | Uses the existing Orb renderer, geometry, shortcut, and status artifacts; no new cleanup boundary |
| `2026-07-15-linux-window-controls-all-renderers` | Broadcasts move/resize shortcut state to every ready renderer and reapplies it when a renderer finishes loading | Uses the existing Orb renderer and geometry artifacts; no new cleanup boundary |
| `2026-07-15-unified-live2d-skill` | Bundles the Live2D runtime package and launcher into the single `codex-voice` skill and merges its lifecycle/state reference | Skill reinstall owns the bundled source; Live2D model/project data remains under its own ownership-checked commands |
| `2026-07-15-isolated-orb-endpoints` | Derives the activity/audio UDP endpoint per project voice root for renderer delivery; the endpoint no longer selects a worker | No additional artifact or cleanup boundary |
| `2026-07-15-global-playback-arbiter` | Moves playback ownership, cross-project attention arbitration, session-transition announcements, and the persistent Kokoro worker into one user-level arbiter; project watchers become clients and pass the target Orb route per request | The daemon socket/log live in the user-level Codex voice state area and are intentionally shared; uninstalling one project must not stop it for other sessions |
| `2026-07-15-project-route-binding-refresh` | Intersects renderer and Live2D routes with each project's enabled session registry, rejects foreign profile bindings, adds a state-preserving managed runtime refresh, and adds one-command watcher/Orb restart | Refresh removes only obsolete skill-owned runtime copies (`main.cjs`, `preload.cjs`, `styles.css`, and `watcher.py` at the runtime root); profiles, routes, providers, model assets, environments, and user avatar data remain preserved |
| `2026-07-15-session-curation-cascade` | Adds semantic presence-profile curation overrides above neutral model-bundle defaults and below exact routed avatar state; child initial/activity fields explicitly replace parent fields, including empty arrays that clear inherited suppression | Reuses `presence-profiles.json` and the existing Orb preload/renderer bridge; no new runtime artifact or cleanup boundary |
| `2026-07-16-profile-curation-renderer` | Projects the bundled Live2D runtime and makes its renderer apply the validated profile curation event: child initial/activity fields replace the model bundle while an exact routed state remains authoritative | No new runtime artifact; the existing bundled runtime projection and Orb profile-curation bridge are reused |
