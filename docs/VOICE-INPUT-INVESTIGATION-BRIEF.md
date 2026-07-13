# Codex Voice Input: Focused Investigation Brief

Status: local experimental implementation, 2026-07-13. This document is a
handoff for diagnosing and regression-testing the voice-input behavior. It
intentionally excludes Live2D/avatar rendering, Kokoro waveform shaders, and
Codex GUI internals unless they are directly involved in the input boundary.

Resolution under test, 2026-07-13: the lab source now gates queue claims before
signalling the player, terminates only the disposable ffplay sink, keeps Kokoro
inference filling a PCM buffer, and resumes the same request without a durable
requeue. STT is prewarmed during capture and sequenced through clipboard
delivery. Live mouse/microphone validation passed. A follow-up tail-clipping
reproduction found that repeated relative 20 ms sleeps could wake early on
Windows, feed ffplay several seconds ahead of the audio device, and hit the
two-second EOF drain cap. Playback now rechecks one cumulative deadline so
pipe-write and event-loop overhead are compensated without feeding ahead, and
retains a bounded ten-second final drain fallback.

## Current intended behavior

Voice input is opt-in. The intended gesture is:

1. Hold `Ctrl+Alt` and press the Orb's right mouse button.
2. The OS playback sink stops immediately while the active Kokoro request keeps
   generating into a local PCM buffer.
3. Keep holding the right button to record; movement must not turn this into a drag.
4. Release the right button, or either modifier, to finish that audio chunk.
5. Local STT starts immediately while the same buffered speech stream resumes.
6. Default delivery copies the transcript text to the clipboard as soon as STT
   finishes; it does not wait for resumed speech. The user pastes it into the
   Codex GUI manually.

The runtime does not paste audio, automate the private Codex GUI, or send raw
audio to Codex. Direct App Server submission exists but is explicit opt-in and
is intended for a supported CLI/App Server session, not the desktop GUI.

### Known capture-termination bug (documented, not implemented)

If the pointer leaves the Orb window while capture is active, a later modifier
or right-button release may not reach the Electron page. The recorder can then
remain in `listening` even though the user is no longer holding the gesture.

A future fix must make capture termination idempotent and enforce both of these
conditions:

- Releasing **any** required gesture input (`Ctrl`/`Cmd`, `Alt`, or the right
  mouse button) immediately finishes the current recording, regardless of
  window hover or focus.
- `pointerleave`/`mouseleave` from the Orb window immediately finishes the
  current recording even if every gesture input still appears held.

Both routes should use the normal `capture-finish` path so recorded audio still
reaches STT; repeated stop signals must be harmless. `blur`, `Escape`, and the
maximum-recording timeout remain independent safety fallbacks.

## Runtime topology

```text
Orb Electron page
  preload.cjs
    MediaRecorder + getUserMedia
    Ctrl/Cmd+Alt + right-button gesture
    IPC: voice-record-start / finish / cancel
        |
        v
Orb Electron host: main.cjs
  validates input settings
  writes temporary .webm under .codex-voice/inbox/recordings/
  invokes project voice_input.py control commands
        |
        v
SQLite inbox.sqlite3
  controls: capture-start / capture-finish / capture-cancel
  messages: queued / retry / playing / played / failed
  runtime_state: focus, input, playback, session target
        |
        v
Global-skill watcher.py
  VoiceInputController owns capture state and lock
  PlaybackArbiter is the only TTS playback owner
  persistent stt.py server -> one prewarmed faster-whisper model in .stt-venv
  clipboard.py, or explicit delivery.py AppServerClient
```

Important process distinction: `start_voice.ps1` runs the global skill's
`toggle.py`. That toggle launches the watcher from:

```text
C:\Users\Bartek\.codex\skills\codex-voice\scripts\watcher.py
```

The active project runtime contains helper copies under:
`C:\Users\Bartek\Documents\Playground\.codex-voice\`. Do not assume the
project-local `watcher.py` copy is the process currently executing; compare the
global skill copy and the lab source before changing behavior.

Source of truth for the current implementation:

```text
C:\Users\Bartek\Documents\Codex-AI-presence-lab\skills\codex-voice\scripts\
```

## Files to inspect, in order

### 1. Gesture and Electron boundary

`skills/codex-voice/scripts/orb/preload.cjs`

- `voiceInputEnabled`, `gesture`, `voiceRecorder`, and `voiceStream` are the
  only recorder state.
- `beginVoiceCapture()` calls the host's `voice-record-start` IPC, obtains the
  microphone with `getUserMedia`, creates `MediaRecorder`, and sends bytes on
  `voice-record-finish`.
- Right-button `pointerdown` starts capture immediately.
- Right-button `pointermove` is swallowed, so it cannot become a drag.
- Handlers exist for right-button `pointerup`, modifier release, `blur`, and
  `Escape`, but key/button releases can be missed after the pointer leaves the
  Electron window. There is not yet an idempotent `pointerleave` finish hook;
  see the known capture-termination bug above.
- Left `Ctrl+Alt` remains the Orb movement gesture; `Ctrl+Alt+Shift` remains
  resize.
- The Orb is click-through by default. The preload must arm host move mode
  before pointer events can arrive. This is the first place to instrument if
  recording never starts.

`skills/codex-voice/scripts/orb/main.cjs`

- `inputSettings()` and `inputEnabled()` read `.codex-voice/input.json`.
- `voice-record-start` invokes `voice_input.py control capture-start`.
- `voice-record-finish` writes the received bytes to the recordings directory,
  then invokes `control capture-finish --recording ...`.
- `voice-record-cancel` invokes `control capture-cancel`.
- Check Electron microphone permission, IPC return values, recording byte size,
  and whether the renderer actually receives `voice-input-state` events.

`skills/codex-voice/scripts/orb/renderer.js`

- This is display/state-label code only for voice input.
- The duplicate built-in `MediaRecorder` implementation was removed. Do not
  add a second recorder here; that was a likely source of double/haunted input.

### 2. Control ingestion and target selection

`skills/codex-voice/scripts/voice_input.py`

- `capture-start` selects `focus.session_id`, falling back to
  `last_session_id`; it does not guess a target.
- It gates focus as `listening`, allocates a durable capture sequence, adds a
  `capture-start` control row, writes `tts-stop.request`, and terminates only
  the PID in `tts-player.pid` so no later audio reaches the OS.
- `capture-finish` validates that the recording is inside the owned recordings
  directory, adds a control row, and marks input as transcribing.
- `capture-cancel` adds a cancel control and marks input idle.
- `delivery_mode` defaults to `clipboard`; `app-server` must be explicit.

### 3. Playback interruption and focus lock

`skills/codex-voice/scripts/watcher.py`

Inspect these symbols first:

- `PlaybackArbiter._run()` — claims and serializes every queued speech item;
  the active row stays `playing` across a capture pause and is not requeued.
- `PlaybackArbiter.interrupt_current()` — snapshots the active item for focus
  bookkeeping; the short-lived input helper controls the disposable audio sink.
- `VoiceInputController.handle_controls()` — consumes SQLite controls.
- `_start_capture()` — pins the target and establishes the listening lock.
- `_finish_capture()` — begins transcription immediately and independently
  releases interrupted playback.
- `_begin_transcription()` and `_transcribe_and_submit()` — enqueue ordered STT
  jobs and route only sequence-current text.
- `_deliver_transcript()` — clipboard path versus App Server path.
- `notify_completed()` — observes resumed playback or an App Server target
  response; resumed playback completion no longer gates transcription.
- the timeout/recovery branch near the end of `VoiceInputController` — releases
  stale locks and deletes orphan recordings.

The intended focus states are:

```text
idle
  -> listening
  -> input: transcribing + focus: resume-playback
  -> clipboard-ready | submitting
  -> target-response (App Server only)
  -> drain-queued
  -> idle
```

### 4. Durable queue and haunted replay behavior

`skills/codex-voice/scripts/inbox.py`

Inspect:

- `enqueue()` — stable `event_id` deduplication.
- `claim_next()` — FIFO selection, optionally restricted to the focused session.
- `complete()` — clears status and interruption cursor.
- `requeue()` — retry/replay behavior and saved resume text.
- `recover_inflight()` — turns `playing` rows into retry rows after restart.
- `recover_input_state()` — releases a non-idle focus state after restart.
- `consume_controls()` — exactly-once control consumption under SQLite lock.

The most useful diagnostic is status-only SQLite inspection; do not dump message
text when debugging:

```powershell
py .codex-voice\voice_input.py --voice-root .codex-voice status
```

Check `messages` grouped by `status`, the `focus`, `input`, and `playback`
runtime-state records, and any `last_error`, `attempts`, `replay_count`, or
`resume_offset` on a problematic row. A temporary `playing` row during active
speech is normal; it is suspicious only if `playback.state` is idle and the
row remains playing after the worker has completed.

### 5. Local STT and delivery

`skills/codex-voice/scripts/stt.py`

- `STTWorker` lazily starts `stt.py --server` during the recording hold.
- `stt.py` loads one `faster_whisper.WhisperModel`, serializes local WebM
  requests, and emits sequence-correlated JSON containing only text.
- Temporary audio is deleted in the controller's `finally` block.
- `stt-models/` is the local model cache.

`skills/codex-voice/scripts/clipboard.py`

- Copies transcript text to the Windows clipboard only.
- It never pastes or clicks in Codex.

`skills/codex-voice/scripts/delivery.py`

- `AppServerClient` launches a short-lived `codex app-server --stdio` process.
- It performs `initialize`, `thread/resume`, then `turn/start`, or `turn/steer`
  if the target thread reports an active turn.
- This path is not used by default and is not a GUI injection mechanism.

## Likely fault boundaries

Investigate in this order rather than redesigning the whole service:

1. **Pointer delivery:** Is the click-through Electron window armed when the
   right-button pointerdown occurs? Does `voice-record-start` return success?
2. **Microphone permission/MediaRecorder:** Is `getUserMedia` resolving? Is a
   non-zero WebM emitted? Is `voice-record-finish` receiving the bytes?
3. **Control timing:** Does SQLite show capture-start and capture-finish rows
   consumed by the watcher? Do the Orb and watcher disagree about input state?
4. **TTS pause race:** Does `capture-start` terminate the PID in
   `tts-player.pid` immediately? Does inference continue while no replacement
   ffplay process is created until `tts-resume.request` appears?
5. **Immediate transcription:** Does `capture-finish` start STT at once while
   the interrupted event resumes independently?
6. **STT runtime:** Does the persistent `.stt-venv` worker return a response
   with the same capture sequence, or an error/empty transcript? Measure warmup
   separately from steady-state capture.
7. **Queue drain:** After clipboard delivery, does focus become `drain-queued`
   and then return idle after all queued messages are played?

Do not start with Codex GUI file-registry or SQLite injection. That is not a
supported user-message path and would make the diagnosis impossible to separate
from private-client state corruption.

## Minimal evidence to collect

For one short capture attempt, record timestamps and IDs only:

```text
pointerdown
voice-record-start result
capture-start consumed
tts-stop.request written/observed
ffplay PID terminated; active event remains playing
recording path + byte count
capture-finish consumed
STT start/end + capture sequence
clipboard-ready or App Server method
resume event completion
focus/input final state
```

Useful commands:

```powershell
py .codex-voice\voice_input.py --voice-root .codex-voice status
Get-Content .codex-voice\watcher.log -Tail 120
Get-Content .codex-voice\hook.log -Tail 80
Get-ChildItem .codex-voice\inbox\recordings -Force
```

Code-level regression gates:

```powershell
py -3 -m unittest discover -s skills\codex-voice\tests -v
node --check skills\codex-voice\scripts\orb\preload.cjs
node --check skills\codex-voice\scripts\orb\main.cjs
py -3 tools\e2e_check.py --source .
```

The current implementation is local and uncommitted. Any proposed fix should
first isolate the failing boundary with the evidence above, then add a focused
regression test before changing the Codex-facing adapter.
