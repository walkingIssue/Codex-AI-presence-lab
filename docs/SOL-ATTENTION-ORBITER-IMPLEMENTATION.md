# Sol attention-orbiter implementation checkpoint

Date: 2026-07-13  
Branch: `sol/attention-orbiter`  
Working copy: `C:\Users\Bartek\Documents\Codex-AI-presence-lab-Sol`

## Outcome

This checkpoint establishes a processor-efficient multi-session presence path
without introducing another playback owner:

- Electron budgets renderer animation callbacks at 20 FPS idle and 30 FPS
  active before built-in or custom avatar scripts load.
- Session presence profiles resolve `avatar_id`, Kokoro voice, speed, and mode
  at `PresenceService`.
- Durable speech rows snapshot profile routing fields. Ephemeral commentary
  remains in Luna's latest-only in-memory lane.
- `PlaybackArbiter` remains the only TTS owner and uses the existing persistent
  Kokoro worker. Voice/speed/mode are per-request settings on that worker.
- Spoken attention uses a composite `session:<id>|profile:<id>` route key.
- Electron creates one avatar window per bound session in one process. Activity
  is routed by session; unscoped Kokoro audio/state goes only to the current
  `voice-output` owner.
- Ctrl/Cmd+Alt plus right-button hold on a profile window explicitly targets
  voice input to that window's bound session.

No user-owned Live2D bundle was edited.

## CPU gate

The benchmark hard-copies the source Orb and the selected Higan bundle into an
isolated temporary project, reuses the installed Electron runtime through a
temporary junction, assigns a unique user-data directory and UDP port, samples
CPU time, and removes only processes matching that unique user-data path.

| Scenario | Median aggregate CPU | Change from baseline |
| --- | ---: | ---: |
| Higan, original unbounded idle loop, 3 x 10 s | 150.47% | baseline |
| Higan, optimized idle, 3 x 10 s | 32.03% | -78.7% |
| Higan, optimized speaking cadence, 2 x 8 s | 43.76% | -70.9% |
| Two session windows, Higan plus built-in, 2 x 8 s | 39.84% | -73.5% |

The original Higan renderer had both the Pixi application ticker and Live2D's
shared ticker running at display cadence. The host-side budget intercepts
`requestAnimationFrame` before those libraries initialize, so both loops are
bounded without requiring a patch to each user-owned avatar bundle.

Reproduce the main gate:

```powershell
.\tools\benchmark_renderer.ps1 `
  -AvatarBundle 'C:\Users\Bartek\Documents\Playground\.codex-voice-avatars\higan-live2d' `
  -Runs 3 -WarmupSeconds 6 -SampleSeconds 10
```

Run the two-session smoke with
`tools/fixtures/two-session-profiles.json` through `-ProfilesPath`.

## Profile contract

`.codex-voice/presence-profiles.json` uses schema
`codex-ai-presence/profiles/v0.1`:

```json
{
  "schema": "codex-ai-presence/profiles/v0.1",
  "project_profile_id": "sol",
  "profiles": {
    "sol": { "avatar_id": "builtin", "voice": "af_heart", "speed": 1.0, "mode": "stream" },
    "luna": { "avatar_id": "higan-live2d", "voice": "bf_isabella", "speed": 1.2, "mode": "stream" }
  },
  "sessions": {
    "session-sol": { "profile_id": "sol" },
    "session-luna": { "profile_id": "luna" }
  }
}
```

Resolution order is explicit Presence-boundary profile, session binding,
project profile, then legacy project defaults. Provider selection remains
project-wide because it owns the persistent model runtime.

## Preserved invariants

- Durable final/normal output remains FIFO in SQLite.
- Commentary/activity cannot switch the spoken owner and do not become durable.
- A durable message preempts and discards an ephemeral update, never another
  durable message.
- Voice capture pauses only the disposable playback sink while one real Kokoro
  request continues buffering and resumes without replay/requeue.
- `session_id`, `thread_id`, `turn_id`, `profile_id`, and `avatar_id` are stored
  independently.
- Renderer packets contain only sanitized activity, ownership, and audio data.

## Current limits

- Orb restart is required after profile bindings change.
- The original primary Orb position/size is persisted; additional windows use
  deterministic cascaded positions for this first slice.
- Right-button voice capture works from every profile window. Drag/resize
  persistence is still primary-window-only.
- Avatar action snapshots remain project-scoped and are delivered to every
  loaded window whose `avatar_id` matches. Per-profile action state is future
  work.
- Live2D model memory remains substantial; this checkpoint targets processor
  efficiency and shared-process rendering, not model-memory deduplication.

## Verification

- 36 Python unit/integration tests pass.
- 6 Electron host/renderer unit tests pass.
- Two real session-bound renderer processes loaded under one Electron main/GPU
  host in the isolated smoke run: Higan for `luna`, built-in for `sol`.
- `git diff --check` passes.
