# Fedora Codex Voice Upgrade and Regression Plan

Prepared on 2026-08-03 for continuation from the Codex AI Presence source
directory. Move this file into that checkout, restart Codex from there, and
have the new session read this entire document before changing anything.

## Objective

Validate the current remote voice skill in a clean checkout, install or migrate
it on Fedora without losing the existing runtime configuration or Windows-side
development work, and then execute the open Fedora regression cards with
recorded evidence and a usable rollback path.

## Hard safety boundaries

- Do not mount, modify, remove, repartition, or otherwise write to the Windows
  installation or its filesystem.
- Preserve every tracked and untracked change in
  `/home/walkinissue/source/codex-ai-presence-lab`. It was dirty when this plan
  was written and contains Windows-related development work.
- Do not use `git reset --hard`, `git clean`, `git checkout --`, destructive
  rebases, or an in-place branch switch in that dirty checkout.
- Use a new temporary clone or another isolated clean checkout for the remote
  revision under test. Do not make the dirty checkout match remote `main`.
- Treat `skills/codex-voice/` as source, `.codex-voice/` as generated runtime
  state, and `$CODEX_HOME/skills/codex-voice/` as the installed projection.
  Never patch a generated runtime copy as the source of truth.
- Do not copy `sessions.json`, `presence-profiles.json`, avatar-state files,
  ledgers, or route data between projects to repair a routing problem.
- Do not use `--force` merely to get past an error. If setup reports a changed
  hook, inspect the exact difference and ask the user before proceeding.
- Do not enable project-wide voice without asking whether the desired scope is
  the current session or every session in the project.
- Do not enable or test microphone capture without explicit permission.
- Test uninstall and destructive rollback cases against an isolated temporary
  `CODEX_HOME` first. Do not uninstall the live profile merely to satisfy a
  checklist.

## Known state when this plan was written

- Canonical lab repository:
  `https://github.com/walkingIssue/Codex-AI-presence-lab`
- Remote default branch: `main`
- Remote `main` at inspection time:
  `80e44a1511e316968806568e84c81e2c204df19a`
  (`Add binding-scoped voice input feedback`, 2026-07-18)
- The latest remote E2E run for that SHA was green:
  `https://github.com/walkingIssue/Codex-AI-presence-lab/actions/runs/29659276981`
- That GitHub workflow currently contains a Windows job only. A green run is
  not evidence that the Fedora installation/migration card passed.
- This Codex profile resolved `codex-voice` from:
  `/home/walkinissue/source/codex-ai-presenece-lab-sarah/skills/codex-voice`
- `$HOME/.codex/skills/codex-voice` was a symlink to that same directory.
- The resolved checkout was at:
  `bd31f30ac59aca62ca1a9271d945003ac436c065`
  (`Move visual curation into session profiles`, 2026-07-15)
- That installed revision was an ancestor of remote `main` and was 18 commits
  behind it.
- `/home/walkinissue/stream-overlay` had no project-local `.codex-voice`
  runtime.
- The lab repository is development and release-validation source. Its README
  identifies `walkingIssue/Codex-AI-presence` as the clean release repository
  and `walkingIssue/Codex-AI-presence-nightly` as the prerelease repository.
  Reconfirm which artifact is intended before changing the live profile.

All revisions and issue states must be rechecked at the beginning of the new
session; do not assume these values are still current.

## Regression cards

- Fedora Codex TUI parity and E2E matrix:
  https://github.com/walkingIssue/Codex-AI-presence-lab/issues/4
- Guarded migration, cross-platform gates, and nightly promotion:
  https://github.com/walkingIssue/Codex-AI-presence-lab/issues/16
- Fedora VS Code parity, if VS Code is part of the test:
  https://github.com/walkingIssue/Codex-AI-presence-lab/issues/6
- GUI reference regression contract:
  https://github.com/walkingIssue/Codex-AI-presence-lab/issues/1
- Incremental TUI voice-streaming contract, when applicable:
  https://github.com/walkingIssue/Codex-AI-presence-lab/issues/8
- Release gate definition:
  https://github.com/walkingIssue/Codex-AI-presence-lab/blob/main/docs/RELEASE-GATE.md

Issue #16 explicitly recorded that Windows evidence was green while the Fedora
migration, restart, uninstall, two-project regression, and nightly publication
pass remained open.

GitHub Projects board membership was not verified because the available token
lacked `read:project`. The issues themselves are open, detailed test cards.

## Phase 0: reload authoritative instructions

Before running setup or migration commands:

1. Read the complete `skills/codex-voice/SKILL.md` from the exact clean revision
   being tested.
2. Read its `RUNTIME-MANIFEST.md` and any migration/release documentation that
   the current skill directly references.
3. Inspect the current remote default branch, commit, open cards, and latest
   E2E conclusion again.
4. Inspect the dirty source checkout read-only and confirm no plan step would
   modify it.
5. Prefer the current revision's documented v0.2 commands. Do not reuse an old
   v0.1 setup command simply because it appears in this session's older skill.

## Phase 1: capture the live baseline and rollback inputs

This phase should be read-only except for placing a deliberately scoped backup
outside the managed runtime boundary after obtaining approval.

Record at minimum:

- Fedora version, kernel, desktop session, and whether the session is Wayland
  or X11.
- Python 3.11/3.12 and Node versions available for the gate.
- Current `CODEX_HOME`, current installed skill path/symlink target, source
  commit, and file type.
- Current voice status, provider, configuration matrix, enablement scope, Orb
  selection, profile bindings, and installed avatar selection through the
  supported status commands.
- Current Presence/voice processes and the number of supervisors, warm Kokoro
  workers, watchers, and Electron renderer roots.
- The managed hook path and its checksum or exact diff from the source copy.
- Existing project runtime roots that are actually in scope. Do not scan or
  archive unrelated home-directory data.
- Managed settings, profiles, geometry, provider markers, runtime manifests,
  migration inputs, and user-owned avatar references needed to prove
  preservation. Do not archive model caches or raw recordings unnecessarily.

Create a timestamped results document named
`FEDORA-VOICE-SKILL-REGRESSION-RESULTS.md`. Never record secrets, tokens, raw
audio, hidden model reasoning, or full tool payloads in it.

Stop before installation if the old revision cannot be identified, rollback
inputs cannot be preserved, or the live runtime already has duplicate owners.

## Phase 2: make a clean candidate without touching the dirty checkout

1. Create a fresh temporary directory with `mktemp -d`.
2. Clone the canonical lab repository into it and resolve the exact remote SHA.
3. Do not fetch, switch, reset, clean, or rewrite the dirty Windows-development
   checkout as a shortcut.
4. Install missing test dependencies only after showing the user what will be
   installed and obtaining any required approval.
5. Use Python 3.12 when available because the remote workflow uses 3.12. The
   voice runtime itself supports Python 3.11 or 3.12.

## Phase 3: pre-install source and projection gates

Run the current workflow's Fedora-compatible equivalents from the clean
candidate. Exact commands should be taken from the candidate's
`.github/workflows/e2e.yml`; at the time this plan was written the substantive
checks were:

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=presence-runtime/src \
  python -m pytest -q -p no:cacheprovider presence-runtime/tests
python tools/runtime_manifest.py --check
env PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -q -p no:cacheprovider skills/codex-voice/tests
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=live2d-avatar-runtime/src \
  python -m pytest -q -p no:cacheprovider live2d-avatar-runtime/tests
npm --prefix presence-runtime/renderer-host test -- --runInBand
env PYTHONDONTWRITEBYTECODE=1 python tools/e2e_check.py --source .
```

Project a clean artifact into a temporary output directory using the candidate
revision's `tools/project_release.py`, then run `tools/e2e_check.py` and the
projected Python/Node contract suites against that artifact as the workflow
does.

The no-network E2E gate must prove at least canonical projection, manifest
integrity, two-project binding isolation, queue routing, and cursor-only
adapter behavior.

Do not install if any source, projected-artifact, manifest, Python, Node, or
E2E check fails or is skipped. Record the exact failing test and stop.

## Phase 4: approve and perform the managed installation/migration

Before this phase, show the user:

- Exact candidate SHA and repository.
- All pre-install results.
- Paths that will be written.
- Processes that will be stopped or restarted.
- The rollback target and backup location.

Then request approval for the live profile mutation.

Install the clean projected artifact through the exact current skill's managed
installation/migration procedure. Project the same canonical revision into
`$CODEX_HOME/skills/codex-voice/` before restarting runtime components. Do not
manually merge generated files.

If the current revision documents a managed refresh, use it only where its
stated preservation guarantees apply. Preserve providers, model environments,
profiles, avatar curation, user-owned avatar assets, geometry, and ledgers.

After setup, explicitly ask whether voice should apply only to the new session
or to all sessions in that project. Do not silently choose project scope.
Leave optional microphone input disabled unless the user asks to test it.

## Phase 5: immediate post-install health checks

Before subjective UI testing, verify:

- The installed projection resolves to the exact tested SHA/artifact.
- The runtime reports healthy and one authoritative user-level owner.
- There is one warm Kokoro worker and one central Electron renderer root, not a
  new worker or renderer owner per project.
- Existing profile values, provider choice, model/voice selection, volume,
  commentary volume, playback mode, scope, avatar choice, and saved geometry
  survived migration.
- Original migration/rollback inputs remain present and unchanged as promised.
- No unrelated project hook, Windows-development file, user-owned avatar, or
  overlay file changed.
- A runtime restart restores the same effective state without manual repair.

Stop and roll back if ownership duplicates, settings disappear, source and
installed hashes differ, migration inputs are altered unexpectedly, or the
runtime requires an undocumented repair.

## Phase 6: Fedora manual regression card

Use issue #1 as the GUI baseline, issue #4 for the Fedora TUI lane, and issue
#16 for migration/recovery. Mark every item pass, fail, unproven, or not
applicable; untested is not green.

Exercise and record:

- Default render and singleton runtime registration.
- Session-only and project scope, after the user chooses each test scope.
- Default and per-session voice/rendered profiles.
- Profile curation and exact routed-state precedence, including an explicitly
  empty child override.
- Two simultaneous projects with distinct profiles and no route/state/audio
  leakage.
- Foreground ownership and fair queueing during overlapping real output,
  commentary, activity, interruption, and restart events.
- Thinking, tool, CLI, skill, waiting, and error semantic states plus speaking
  cadence, without exposing hidden tool details.
- Visible progress coalescing and proof that stale commentary is not durable or
  replayed after a real response/restart.
- Playback interruption and exact-once resume from the correct cursor.
- Persisted resize/reposition and correct window targeting. Record Wayland and
  X11 separately where applicable; do not claim the untested compositor path.
- Controlled runtime restart, worker restart, status, and shutdown with no
  orphan process or stale route.
- TUI incremental `start`/ordered `delta`/`finish`/`cancel` behavior when the
  TUI lane is in scope, proving synthesis starts before turn completion.
- Privacy boundaries: no hidden reasoning, secrets, arbitrary paths, raw tool
  payloads, or retained recordings in normalized renderer/voice envelopes.

Only with explicit microphone permission, additionally test one short capture,
local transcription, intended-session-only delivery, cancellation, timeout,
restart recovery, temporary-audio deletion, and uninterrupted safe clipboard
behavior for GUI sessions. Never automate private GUI submission.

## Phase 7: isolated cleanup and rollback testing

Run uninstall, failed migration, and rollback fixtures first with a temporary
`CODEX_HOME` and disposable projects. Prove that cleanup removes only
manifest-owned resources while preserving unrelated hooks, other projects,
sessions, rollback inputs, and user-owned avatar assets.

Do not uninstall the live profile unless the user separately approves that
exact action after the isolated test passes.

If live rollback is required:

1. Stop only the new runtime using its documented controlled-shutdown command.
2. Restore the previous installed projection/symlink and only the backed-up
   managed state that the migration changed.
3. Never reset or clean either source checkout as part of rollback.
4. Restart the previous runtime through its supported command.
5. Re-run the baseline status/configuration/process checks and record the
   result.

## Completion criteria

The Fedora upgrade is green only when:

- All source and projected automated gates pass on the exact installed SHA.
- Migration preserves settings, providers, profiles, avatar data, geometry,
  rollback inputs, and unrelated development work.
- One runtime, one worker, and one Electron root serve two simultaneous
  projects without cross-route leakage.
- Restart and recovery are deterministic.
- Applicable GUI/TUI, Wayland/X11, input, privacy, and cleanup card items have
  explicit evidence.
- Every untested item is labeled unproven rather than silently accepted.
- A verified rollback path remains available.

## Suggested continuation prompt

After moving this file into the source checkout and restarting Codex there:

> Read `FEDORA-VOICE-SKILL-REGRESSION-PLAN.md` completely, then read the current
> `skills/codex-voice/SKILL.md` and its required migration/runtime references.
> Preserve the dirty Windows-development checkout and do not touch the Windows
> installation. Revalidate the remote state, execute Phases 0 through 3 in a
> clean temporary checkout, and report the evidence before requesting approval
> for the live installation in Phase 4.
