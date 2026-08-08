# Fedora Codex Voice Regression Results

Recorded: 2026-08-03T11:15:03+02:00
Updated through: 2026-08-07T02:43:55+02:00

## Outcome so far

Phases 0 through 5 of `FEDORA-VOICE-SKILL-REGRESSION-PLAN.md`, guarded
rollback/retry, and the native Higan semantic-lane check are complete against
a clean clone of lab `main` at
`80e44a1511e316968806568e84c81e2c204df19a` (`Add binding-scoped voice input
feedback`) plus six Fedora source fixes found during the live upgrade and one
additional legacy-hook migration defect mitigated in the live configuration. All
Fedora-applicable source and projected-artifact gates pass. The single
Windows-only Presence test is recorded as not applicable rather than silently
counted as green.

The exact patched projection is installed live. Project registration,
automatic v0.1 migration, native Higan catalog import, two controlled restarts,
guarded migration rollback/retry, and live two-project renderer/binding
isolation all pass. A live durable final/announcement FIFO test also passes
deduplication, binding-scoped pause, buffered resume, and completion after a
SQLite concurrency defect found by the first run was fixed. The runtime is
healthy with one active session and no stale historical-session windows. The
GNOME Wayland movement lane exposed a native-Wayland backend regression. The
managed Electron root now selects the session's XWayland bridge when available.
The exact projected fix is installed live, stacking is user-accepted, and the
user confirmed Ctrl+Alt+left-drag now repositions Higan. Managed STT
installation, permission, warm-worker health, physical input-device discovery,
and clipboard delivery now pass. The previously unexplained screenshot
correlation is resolved: captured pipes kept the daemonized `wl-copy` owner and
adapter delivery thread alive until another program replaced the clipboard.
The repaired live adapter drained both durable transcripts in order, marked
both delivered, and the clipboard exactly matched the newest 63-character
transcript. After the final reinstall, two additional real transcripts reached
`delivered` and the clipboard matched the newest 72-character result exactly.
Isolated uninstall, native-X11 desktop, and VS Code remain untested and are not
claimed green.

## Safety and checkout state

- The Windows installation and its filesystem were not mounted or modified.
- The protected development checkout at
  `/home/walkinissue/source/codex-ai-presence-lab` was inspected read-only. It
  remains on `poc/fedora-kokoro-arc`, two commits ahead of its upstream, with
  the same tracked and untracked Windows/Fedora development work present.
- The continuation checkout at
  `/home/walkinissue/source/codex-ai-presenece-lab-sarah` is now on
  `test/fedora-v02-regression-20260803`, based exactly on remote v0.2 SHA
  `80e44a1`. Eighteen tracked Presence source/test/documentation files contain
  the Fedora fixes;
  this results file and the pre-existing plan remain untracked.
- Candidate work used the disposable clean clone
  `/tmp/codex-presence-regression.5hcxul/candidate`. The final clean-base
  patched source is this continuation checkout and the latest exact projected
  artifact is `/tmp/codex-presence-stt-clipboard-final-20260807/dist`.
- Scoped v0.1 rollback inputs were copied to
  `/tmp/codex-presence-v01-rollback.FELxyb` before installation. User-owned
  model archives and full model environments were not duplicated.
- The clean candidate worktree remained clean after the test run.

## Revalidated remote state

- Canonical repository: `walkingIssue/Codex-AI-presence-lab`.
- Default branch: `main`.
- Remote `main`: `80e44a1511e316968806568e84c81e2c204df19a`.
- Fedora fix commit: `1f89e4507535186876023f6272b880663209cd4c`
  on `test/fedora-v02-regression-20260803`, pushed to `origin`; the branch base is
  `80e44a1511e316968806568e84c81e2c204df19a`.
- Latest `main` E2E run: Actions run `29659276981`, successful for the exact
  candidate SHA. The workflow has a Windows job only, so it is not Fedora
  installation evidence.
- Regression cards #1, #4, #6, #8, and #16 remain open and unchecked. Issue
  #16 still records Fedora migration, restart, uninstall, two-project proof,
  and nightly promotion as open.
- Issue #21 tracked the Fedora/Wayland voice-input `ready` frame. The clipboard
  deadlock explains and repairs that terminal-state symptom; the issue closed
  after commit `1f89e45` was pushed.
- Issue #28 tracks the Fedora/Wayland movement regression. Its local source fix
  and regression tests are installed live, movement is user-accepted, and the
  issue is closed. Issue #27 separately tracked the renderer
  falling behind ordinary application windows; the same XWayland change is
  installed live and user-accepted as fixing its stacking behavior; it is also
  closed.
- Issue #29 tracked the flaky newest-transcript clipboard delivery. Its local
  fix, sequential/failure regression tests, durable backlog drain, and exact
  live clipboard comparison pass, and the issue closed after the branch push.
- Installation/runtime defects fixed by the pushed regression branch were
  tracked by #22 (systemd unit), #24 (SQLite shared
  handle), #25 (stale geometry migration), and #26 (POSIX venv executable
  identity); all four are now closed. Issue #23 remains open for the separately
  mitigated legacy Stop-hook coexistence defect and has no source fix on this
  branch.
- Stable release head `382115bd4a333bccadc4b112a86c5926bc2990d3`
  and nightly head `579c6eab53fab221e96d0c0fe762ccca19f5fdea`
  predate Presence Runtime v0.2. Neither is the tested candidate. The tested
  artifact is the clean projection from lab `main`.

## Fedora baseline and current live state

- Fedora 44, kernel `7.1.3-201.fc44.x86_64`.
- GNOME Wayland session. The desktop and Codex remain native Wayland; the
  Presence Electron root now uses XWayland for global geometry control.
- Python 3.12.13 and 3.11.15 are available; the system `python3` is 3.14.6.
- Node 22.23.1 and npm 10.9.8 are available.
- `CODEX_HOME` is unset, so the effective home is `/home/walkinissue/.codex`.
- The installed `codex-voice` skill is a real directory and byte-matches the
  final projected artifact. The active runtime source and built package match
  the same projection for every patched component.
- `$CODEX_HOME/presence`, `$CODEX_HOME/bin/presence`, and the enabled
  `codex-presence.service` are installed. Fedora systemd accepts the generated
  unquoted absolute `WorkingDirectory=/home/walkinissue/.codex/presence`.
- After the latest managed reinstall there is one daemon (PID 76174), one warm
  CPU Kokoro worker (PID 76175), one warm STT worker (PID 76261), and one
  Electron root (PID 76216). Process IDs are observation evidence, not stable
  IDs.
- The reinstall did stop the old persistent worker. A subsequent duplicate
  narration was traced instead to this checkout's separately trusted v0.1 Stop
  hook running alongside the v0.2 rollout adapter. Only the current Sarah
  checkout's hook state was disabled in `~/.codex/config.toml`; the protected
  checkout's hook remains enabled and both legacy hook files remain intact.
- Runtime policy remains CPU. Voice input is installed, machine microphone
  permission is true, and the `base.en` faster-whisper worker reports ready
  with no error. The current authenticated session lease advertises the
  `voice-input` capability.
- PipeWire reports `Built-in Audio Analog Stereo` as the default audio source.
  No recording was initiated during the installation diagnostics; a later
  user-initiated capture did reach the runtime's transcription-ready state.
- The registered project instance is
  `6d1fb91a-e294-4f6c-b003-8503f8c8e17b`. It has exactly two bindings: one
  dormant project binding and the active current-session binding
  `2ea87895-add9-4fbc-a9e7-1d4dbcdccc28`. The renderer has exactly two windows,
  with only the current session active and visible.

### Preserved v0.1 migration inputs

- Supported status reports voice enabled in session scope, one registered
  session, CPU provider, `bf_isabella`, speed 1.08, stream playback, volume
  50%, commentary ratio 50%, visible progress on, Orb on, and microphone input
  disabled.
- The configured project profile is `codex`, using `higan-live2d`; the stored
  session binding resolves to that profile and the expected composite route.
- No watcher, global arbiter, Kokoro server/worker, or Electron renderer
  process is running. The saved watcher PID `299401` is stale.
- The v0.1 SQLite status contains 10 played messages and no pending message
  count. Its persisted service/attention snapshot is stale from July 15 and
  still says running/assigned.
- That stale attention snapshot names `profile:default`, while canonical
  profile resolution names `profile:codex`. The v0.2 migration plan reads
  `presence-profiles.json`, settings, geometry, and pending durable speech; it
  does not use the stale v0.1 attention owner as routing authority.
- The managed Stop hook SHA-256 exactly matches the v0.1 source `speak.py`.
- The v0.2 installer preserved that hook as rollback input but did not retire
  its existing trusted/enabled Codex configuration entry. Exact-length log
  pairs (1404 and 708 characters) prove the legacy project-local hook and the
  user-level v0.2 worker each spoke the same finals. The live mitigation backup
  is `/tmp/codex-config-before-disable-legacy-stop-20260803.toml`.
- Source and deployed v0.1 `RUNTIME-MANIFEST.md` files match exactly.
- The old user-level arbiter state contains only an empty `arbiter.log`; there
  is no arbiter socket.
- Legacy runtime footprint is approximately 1.4 GiB in `.codex-voice`, plus
  approximately 62 MiB each in `.codex-live2d` and `.codex-voice-avatars`.
- Both the global `higan-live2d` model registry and the project materialized
  bundle contain the manifest/profile/model inputs expected by the v0.2
  migrator.

## Architecture archaeology

Presence Runtime v0.2 is a replacement ownership model, not an in-place v0.1
watcher refresh:

1. `presence` is the intent-level CLI and talks through framed local IPC.
2. `RuntimeProtocolHandler` authenticates a connection-bound source lease and
   assigns the project instance and binding UUIDs. Adapters cannot nominate a
   profile, avatar, route key, port, or binding.
3. `ControlAPI` and `RuntimeController` are the mutation/orchestration seam.
4. `PresenceResolver` applies built-ins, model defaults, reusable profile,
   project override, session override, and temporary activity overlay.
5. `PresenceStore` is the WAL-mode SQLite authority for projects, bindings,
   source leases, snapshots, geometry, speech, input, attention, and migration
   ledgers.
6. One daemon owns one Kokoro worker, optional STT worker, one Electron root,
   the playback loop, project adapter supervision, and the IPC server.
7. Project rollout adapters retain only a cursor and diagnostics under
   `.codex-voice/v0.2`; legacy tools delegate through `presence_compat`.

The live installation writes under `/home/walkinissue/.codex/presence`, installs
`/home/walkinissue/.codex/bin/presence`, owns the user systemd unit
`~/.config/systemd/user/codex-presence.service`, and adds only managed adapter
state under the registered project's `.codex-voice/v0.2`. Legacy v0.1 files
remain intact as rollback inputs during the compatibility release.

## Native Higan v0.2 curation

No production `presence/avatar-model-pack/v0.2`, `presence/profile/v0.2`, or
`presence/preset/v0.2` Higan document exists anywhere in the fetched local or
remote Git history. The repository contains the v0.1 Higan renderer profile,
synthetic v0.2 test fixtures, schemas, and the public semantic-curation example
in `skills/codex-voice/SKILL.md`; the synthetic fixture cannot be imported
because its fingerprint and parameter operations are not the real model.

The v0.2 migrator converted the genuine global Higan registry assets into
`higan-live2d@1`. The global registry and project rollback bundle both hash to
model fingerprint
`sha256:b25cda9ed51fa6b81af902b5a39162c1c833c8e0edfa216ea121aaadd568f10c`.
Its model-level `safe_defaults` deliberately remain empty so migration does not
promote the v0.1 global accessory selection into every binding. Fedora visual
testing subsequently proved that this alone is insufficient: the model's own
native baseline still displays the shawl and dark stockings unless its declared
toggle operations are applied.

The first native preset, `plain-sweater@1`, incorrectly used empty shoulder and
leg slots. Resolution was internally consistent but left the model-native shawl
and stockings visible. Corrected preset `plain-sweater@2` now persists:

- persistent `pose.sweater-default`;
- model-declared `accessory.fur-shawl` and `accessory.black-stockings` toggle
  operations in the shoulder and leg slots; for this model these are off-switch
  deltas from visible native defaults, despite their legacy action names;
- thinking: `pose.sweater-hand-mouth-left`;
- tool: `pose.sweater-hand-mouth-right`;
- skill: `pose.sweater-heart`;
- CLI: `pose.right-hand-pipe`;
- waiting: `eyes.dazed`;
- error: `mouth.unhappy` plus `effect.dark-face`.

Native profile `higan-default@2` binds corrected preset `plain-sweater@2` and
the avatar to
`bf_isabella`, speed 1.08, stream mode, volume 50, commentary ratio 0.5, and
visible renderer/progress. It is applied only to current session
`019fc6d8-bc52-7aa1-ad48-37428966b1d0`; the migrated project default remains a
compatibility profile. Both native records are durable in the machine-local
user catalog. The original portable test exports are under
`/tmp/codex-presence-v02-live.fEKK7N/higan-native/`; corrected import documents
are `/tmp/higan-plain-sweater-v2.json` and `/tmp/higan-default-v2.json`. They
are deliberately not tracked in Git because ADR 0002 makes user profile,
preset, and avatar catalog data machine-local rather than projected release
source.

A short-lived authenticated adapter exercised thinking, tool, skill, CLI,
waiting, error, and idle on the exact live binding. Every state resolved to the
declared actions above. Final doctor state is idle with no active/pending cue,
no renderer error, acknowledged revision 5, and exact persistent baseline
`pose.sweater-default`. The test source disconnected cleanly.

After the Fedora accessory regression was reported, the corrected profile was
hot-applied without a renderer restart. The active binding advanced to effective
revision 6, the renderer acknowledged revision 6, and doctor reports
`higan-default@2`, `plain-sweater@2`, and all three persistent toggle actions
with no validation or renderer error. User-visible confirmation of the removed
accessories remains the final acceptance check.

A disposable second project then registered a distinct builtin session binding
while the real Higan session remained connected. During simultaneous live
leases, one Electron root reported both bindings active and visible with their
independent renderer keys and revisions: Higan revision 5 for the real project,
builtin revision 2 for the disposable project. A read-only runtime control call
returned immediately while both sources were connected. The temporary source,
project, adapter, bindings, and windows were removed through managed project
unregistration; final status returned to one real adapter/source and the two
expected Higan windows (dormant project plus active session). No Higan profile,
voice, activity, visibility, or geometry state crossed into the disposable
binding. An initially suspected control stall was disproved: elapsed time came
from the execution permission prompt before the command ran, while the approved
in-process measurement completed immediately.

The central speech ledger also records real CPU Kokoro/playback activity on
this binding: 15 commentary items reached `finished`, including revision-5
items after the native profile and final restart. Two commentary items were
marked `worker: Kokoro request failed`; their failure timestamps coincide to
the second with the two deliberate service replacement/restart windows
(12:35:09 and 12:38:48 local). They are ephemeral commentary, so they were not
replayed. A dedicated final/announcement interruption-and-resume test was then
run against the native Higan binding.

That dedicated test subsequently exposed and closed a fourth Fedora/runtime
defect. The first run queued final item 29 and announcement item 30 on the same
revision-5 Higan binding. The final was deduplicated, paused, held on the same
utterance, resumed from buffered PCM, and finished. The immediately following
announcement failed before Kokoro with
`begin-playback: NotFoundError: Binding ... does not exist`, even though the
binding remained present and active. PresenceStore used one SQLite connection
across daemon threads but serialized only writes; concurrent playback and
status reads could therefore observe an impossible transient result. All
shared-handle reads now use the store's existing reentrant lock, backed by a
deterministic test proving reads wait for an active transaction.

After the managed v5 reinstall, the unchanged test produced final queue item
37 and announcement item 38. Hook evidence records the final pausing at
13:13:31, resuming at 13:13:32, and completing at 13:14:01; the announcement
then began on the same binding and completed at 13:14:18. Both authoritative
rows are `finished` with no diagnostic. The harness's 120-second wall expired
while item 38 was already `playing` because earlier commentary occupied the
FIFO; it did not extend or replay the item, and the terminal database state plus
hook log establish completion.

Catalog/effective/doctor inspection at the end of the audio-queue test confirmed
the then-current `higan-default@1`, `plain-sweater@1`, and effective revision 5.
The later Fedora accessory correction advanced the live binding to
`higan-default@2`, `plain-sweater@2`, and acknowledged revision 6 without
changing the validated queue evidence. There is one authenticated rollout
lease, no candidate revision, no presentation error, and healthy CPU TTS/STT
workers. The installed skill byte-matches projected v5. The projected,
installed-skill, and active-runtime copies of `store.py` all hash to
`59b8e7aa5eedcdfea3887134be1956bf4c9a01744ea182678b113e097cf6c160`.

## Automated regression evidence

| Gate | Result |
| --- | --- |
| Runtime manifest source check | PASS: 15 components validated |
| Source no-network E2E | PASS: canonical projection, manifest, two-project bindings, queue routing, cursor-only adapter |
| Source renderer-host Node suite | PASS: 6 tests |
| Source Presence Runtime Python suite | PASS: 73 applicable tests; 1 Windows-only skip; 1 systemd-uninstall case deliberately not run outside the safety sandbox |
| Source codex-voice Python suite | PASS: 71 plus 4 subtests; 2 Windows CLI/wrapper tests not applicable on Fedora |
| Source Live2D Python suite | PASS: 16 |
| Release projection | PASS: 190 files |
| Projected no-network E2E | PASS |
| Projected Presence Runtime Python suite | PASS: 67; same 1 Windows-only skip |
| Projected codex-voice Python suite | PASS: 71 plus 4 subtests; same 2 Windows-only skips |
| Projected Live2D Python suite | PASS: 16 |
| Projected renderer-host Node suite | PASS: 6 tests |
| Patched migration suite | PASS: 7, including stale disabled-session geometry |
| Patched full Presence suite | PASS: 71; 1 Windows-only skip |
| Patched manifest check | PASS: 15 components |
| Patched projected no-network E2E | PASS: canonical projection, manifest, two-project bindings, queue routing, cursor-only adapter |
| Live install/restart/doctor | PASS: one daemon, worker, Electron root, adapter; current native Higan binding acknowledged |
| Guarded live rollback/retry | PASS: committed; stale geometry bindings reduced from four to two |
| Live CPU commentary synthesis/playback | PASS with bounded restart evidence: 15 finished; 2 ephemeral items failed only across controlled restarts and were not replayed |
| Live two-project isolation | PASS: simultaneous independent Higan/builtin bindings under one daemon, worker, and Electron root; managed cleanup restored the original singleton state |
| Live final/announcement queue and pause/resume | PASS after fix: duplicate rejected; exact Higan utterance paused/resumed; FIFO final 37 and announcement 38 both finished without diagnostic |
| Legacy Stop-hook coexistence | DEFECT MITIGATED LIVE: old persistent worker stopped, but an enabled v0.1 hook duplicated v0.2 final narration; current-checkout hook disabled, source-level migration guard still open |
| GNOME Wayland move/retarget | FIXED LOCALLY AND ACCEPTED LIVE: [#28](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/28); managed Electron root uses `--ozone-platform=x11`, the user confirmed Ctrl+Alt+left-drag moves Higan, and two later reinstalls preserved its program-specified location at `5406,87` |
| GNOME Wayland topmost stacking | FIXED LOCALLY AND ACCEPTED LIVE: [#27](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/27); the XWayland-hosted window advertises `_NET_WM_STATE_ABOVE`, and the user confirmed it no longer falls behind ordinary windows; push remains pending |
| Fedora Higan shawl/stockings baseline | REGRESSION, CATALOG FIX APPLIED: empty slots left model-native accessories visible; `plain-sweater@2` applies the declared off-switch toggles and renderer revision 6 is acknowledged; visual confirmation pending |
| Managed STT install and readiness | PASS: input installed, machine permission enabled, one warm `base.en` worker ready, current lease has `voice-input`, and PipeWire exposes a default microphone |
| Live microphone capture/transcription | PASS: sequential real captures produced durable distinct transcripts; the original backlog and two post-restart captures all reached `delivered` |
| Fedora newest-transcript clipboard delivery | FIXED LOCALLY AND LIVE: [#29](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/29); daemonized clipboard owners no longer inherit captured pipes; the original backlog drained, two further captures delivered after reinstall, and private comparisons matched the newest clipboard exactly at 63/63 then 72/72 characters |
| Fedora voice-input terminal feedback | FIXED BY SAME ROOT CAUSE: [#21](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/21); replacing the clipboard had closed inherited pipes and released the blocked adapter, explaining why the screenshot coincided with the amber frame disappearing |

Projection digests:

- Live2D canonical digest:
  `4a8ecbd6cacaad38b2fce96c44f141317f35bcc2631cb84fb494917cb2244e30`
- Presence canonical digest:
  `0243d795e3619fe6f7ea498159395fa264801324193f9ce78fc231374f44a1db`

The seven initial Presence protocol failures were caused by the execution
sandbox rejecting local `socketpair` and Unix-domain-socket operations with
`EPERM`. The exact suite passed when rerun with narrowly scoped local-socket
permission; these are not candidate failures.

## Gate and Fedora defects found

The workflow's dependency step installs `pytest` only, but these checked-in
tests import NumPy:

- `presence-runtime/tests/test_voice_packets.py`
- `skills/codex-voice/tests/test_stream_pause.py`

A fresh Python 3.12 environment stopped at collection with
`ModuleNotFoundError: numpy`. The suites passed after installing `numpy` in the
disposable test environment. The workflow currently relies on runner image
state rather than declaring the complete test dependencies; this should be
fixed before treating the release gate as reproducible.

GitHub tracking and post-push resolution boundary:

| Issue | Defect | Current state | Resolution boundary |
| --- | --- | --- | --- |
| [#22](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/22) | Fedora systemd rejects quoted `WorkingDirectory` | Fixed and tested locally | Closed after commit `1f89e45` was pushed |
| [#26](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/26) | POSIX worker launch dereferences venv Python symlinks | Fixed and tested locally | Closed after commit `1f89e45` was pushed |
| [#25](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/25) | Migration revives disabled-session geometry as windows | Fixed and tested locally and live | Closed after commit `1f89e45` was pushed |
| [#24](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/24) | Shared SQLite reads observe impossible transient state | Fixed and tested locally and live | Closed after commit `1f89e45` was pushed |
| [#23](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/23) | Enabled v0.1 Stop hook duplicates v0.2 narration | Live configuration mitigated only | Keep open after this branch is pushed; a rollback-safe source transition is still required |
| [#28](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/28) | Native Wayland prevents active Presence binding repositioning | Fixed, tested, installed, and user-accepted live | Closed after commit `1f89e45` was pushed |
| [#27](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/27) | Presence renderer can fall behind ordinary application windows | Fixed locally by the XWayland backend change and user-accepted live | Closed after commit `1f89e45` was pushed |
| [#29](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/29) | Daemonized Linux clipboard owner blocks newer STT transcripts | Fixed and tested in source/projection and live | Closed after commit `1f89e45` was pushed |
| [#21](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/21) | STT `ready` feedback frame remains amber | Same clipboard deadlock root cause fixed and proven live | Closed with #29 after commit `1f89e45` was pushed |

Four live Fedora defects were exposed and fixed on the regression branch:

1. [#22](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/22):
   Fedora systemd 259 rejects a quoted `WorkingDirectory` as non-absolute. Unit
   rendering now emits the validated absolute path without quotes.
2. [#26](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/26):
   Resolving a POSIX virtual-environment `bin/python` symlink selected the base
   interpreter, so the supervised worker could not import `kokoro_onnx`.
   Runtime, STT, and adapter executable paths now preserve the venv launcher.
3. [#25](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/25):
   Migration restored every session-shaped geometry record, reviving two
   disabled historical sessions as hidden Live2D windows. Geometry is now
   imported only for sessions enabled by the authoritative v0.1 session-scope
   document (and none for project scope). The live rollback/retry plus restart
   reduced four renderer windows to the expected two.
4. [#24](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/24):
   The daemon shared one SQLite connection between playback, leases, adapters,
   and controls while locking only transactions. A concurrent status read at a
   FIFO handoff made an existing binding transiently appear absent and failed a
   durable announcement. Shared-connection reads are now serialized with the
   same reentrant lock; the full Presence suite and the repeated live
   final/announcement handoff pass.

A fifth migration/configuration defect was exposed and mitigated live, but does
not yet have a source-level prevention fix:

5. [#23](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/23): The
   managed v0.2 reinstall correctly stopped the v0.1 persistent worker, but
   left this checkout's already trusted and enabled legacy Stop hook in Codex
   configuration. The hook launched the project-local Kokoro path for each
   final while the v0.2 rollout adapter independently queued the same final to
   the user runtime. The matching 1404- and 708-character log records identify
   both speakers precisely. The Sarah hook is now disabled; the protected
   checkout and rollback files were not changed. Installation or registration
   should detect and safely retire, delegate, or clearly block on this
   coexistence state. This issue is not part of the after-push resolution set.

A sixth Fedora regression now has a local implementation fix and live backend
evidence:

6. On GNOME Wayland, Ctrl+Alt+left-drag could not retarget and move the active
   avatar after initial launch. Electron's native Wayland backend cannot expose
   or change global window coordinates, while the v0.2 pointer-driven drag and
   persisted geometry path requires both. On Linux Wayland sessions with an
   XWayland `DISPLAY`, the managed Electron command now includes
   `--ozone-platform=x11`; no other desktop process is moved off Wayland.
   Focused Python tests, the source/projected no-network E2E gates, and both
   source/projected renderer suites pass. The exact projection is live, its
   Electron process includes the X11 flag, and GNOME exposes the Higan window
   as an X11 client. The user confirmed Ctrl+Alt+left-drag now moves Higan, and
   two later reinstalls preserved its program-specified location at `5406,87`;
   [#28](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/28) is
   ready to resolve with the branch push.

A seventh Fedora-observed regression was corrected in the machine-local Higan
catalog and remains pending visual acceptance:

7. `plain-sweater@1` cleared the `accessory.shoulders` and `body.legs` semantic
   slots, but Higan's model-native baseline itself renders the fur shawl and
   dark stockings. The public actions are relative toggle expressions rather
   than positive item selections: `accessory.fur-shawl` applies `Key19 -1`, and
   `accessory.black-stockings` switches the leg controls away from the dark-leg
   default. Corrected `plain-sweater@2` persists those declared operations, and
   `higan-default@2` is live at acknowledged renderer revision 6. No raw model
   parameter or installed renderer file was edited.

An eighth Fedora-observed regression is repaired by the clipboard delivery fix:

8. After a live voice capture on GNOME Wayland, the Presence surface retained
   a bright amber rounded border around the whole transparent renderer window.
   The screenshot appearance exactly matches the renderer's `ready` feedback
   rule (`#ffd166`), and a contemporaneous runtime doctor report corroborated
   `input_feedback.phase = "ready"` at revision 14 for the active capture while
   STT, renderer acknowledgement, and renderer health were otherwise green.
   `ready` is an active registry phase with no settling timer; the exact session
   adapter must acknowledge the durable transcript before the runtime routes
   `delivered` and schedules return to idle. The later screenshot replaced the
   clipboard owner, closing the pipe inherited by daemonized `wl-copy` and
   releasing the adapter's blocked `subprocess.run`. The no-captured-pipes fix
   now lets delivery acknowledge and settle without an external clipboard
   replacement. GitHub issue
   [#21](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/21)
   tracks reproduction and repair.

A ninth Fedora regression is fixed locally and user-accepted live:

9. The Presence renderer can fall behind ordinary application windows. This is
   tracked as [#27](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/27)
   and was not conflated with the movement symptom. After the XWayland restart,
   the live Higan window advertises `_NET_WM_STATE_ABOVE`, and the user
   confirmed the window no longer falls behind ordinary application windows.
   The issue remains open only until the shared backend repair is pushed.

A tenth Fedora regression is fixed in source, projection, and the live runtime:

10. [#29](https://github.com/walkingIssue/Codex-AI-presence-lab/issues/29)
    tracks newest-transcript clipboard delivery. `wl-copy` and `xclip` retain
    selection ownership in background processes. The adapter launched them
    with captured stdout/stderr pipes, so the background owner inherited the
    pipe writers and Python waited indefinitely for EOF. The first transcript
    reached the clipboard but remained `ready`, while every newer transcript
    queued behind the blocked delivery thread. Clipboard owners now inherit
    `DEVNULL` instead of captured pipes. Regression coverage proves two distinct
    ready transcripts deliver and acknowledge in order and a failed clipboard
    write remains ready for retry. The exact projected artifact drained both
    live backlog rows to `delivered`; a private comparison confirmed the
    clipboard exactly matched the newest 63-character transcript without
    printing its contents. Two additional post-restart captures then reached
    `delivered`, with a second private comparison matching the newest result at
    72/72 characters.

## Remaining evidence and approval boundary

- Cross-project audio fairness remains unproven. Same-binding durable
  final/announcement FIFO, pause, buffered resume, and exact completion are
  proven. The live policy is still CPU; the Fedora OpenVINO lane has not been
  switched on in this v0.2 runtime.
- Exercise isolated uninstall. Controlled restart and guarded rollback/retry
  are complete.
- Native-X11 desktop and VS Code window-interaction lanes remain unproven.
  Wayland Ctrl+Alt+left-drag, stacking, and post-restart geometry are green.
- Obtain user-visible confirmation that Higan revision 6 removed both the shawl
  and stockings; runtime resolution and renderer acknowledgement are green.
- Exact durable delivery, acknowledgement, and newest-clipboard content are
  proven live; #21 and #29 are closed.
- Add a source-level migration/registration guard for enabled v0.1 Stop hooks;
  the current machine state is mitigated, but a clean repeat must not require a
  manual Codex configuration edit.
- Commit `1f89e45` and the regression branch are pushed. No PR was requested.

Current conclusion: the patched v0.2 candidate is source/projection healthy and
the single-project Fedora migration, native Higan profile, semantic renderer,
singleton ownership, live two-project isolation, restart, and rollback/retry
lanes are green; same-binding real audio queue/interruption is also green after
the SQLite fix. This is not yet a complete release certification:
cross-project audio fairness, OpenVINO, isolated uninstall, native-X11 desktop,
and VS Code remain open as listed
above. Wayland movement/stacking and newest-transcript clipboard delivery are
green in the exact live projection.
