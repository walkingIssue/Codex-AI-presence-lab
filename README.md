# Codex AI Presence Lab

Development source and release-validation workspace for [Codex AI Presence](https://github.com/walkingIssue/Codex-AI-presence).

Current Sol-lane renderer/profile work and measured CPU results are recorded in
[`docs/SOL-ATTENTION-ORBITER-IMPLEMENTATION.md`](docs/SOL-ATTENTION-ORBITER-IMPLEMENTATION.md).

This repository is intentionally richer than the distributable skill. It holds the roadmap, experiments, release projection, and end-to-end checks. The release repository remains the clean surface that users install.

## Repository roles

| Repository | Role |
| --- | --- |
| [`Codex-AI-presence-lab`](https://github.com/walkingIssue/Codex-AI-presence-lab) | Development source, roadmap, experiments, and release gates |
| [`Codex-AI-presence`](https://github.com/walkingIssue/Codex-AI-presence) | Clean installable skill and concise user documentation |
| [`Codex-AI-presence-nightly`](https://github.com/walkingIssue/Codex-AI-presence-nightly) | Automated prerelease snapshots for testers |

## Layout

The source, branching, artifact, and manifest-ownership policy for the
packaged runtime is in
[`docs/PACKAGING-AND-REPOSITORY-STRATEGY.md`](docs/PACKAGING-AND-REPOSITORY-STRATEGY.md).

- `skills/codex-voice/` — source skill under development.
- `live2d-avatar-runtime/` — first-class, self-contained Live2D runtime package bundled into the single `codex-voice` skill release. It is vendored without its upstream `.git` directory and has no external Python dependencies; there is no separate Live2D skill install.
- `tools/project_release.py` — projects only the installable skill into a clean artifact.
- `tools/e2e_check.py` — runs the configuration and package-boundary smoke test.
- `skills/codex-voice/RUNTIME-MANIFEST.md` — tracks project-local runtime ownership and cleanup for each release revision.
- `docs/ROADMAP.md` — current foundation and near-term experiments.
- `docs/WISHLIST.md` — lab-only feature backlog, including per-session profiles.
- `docs/IMPLEMENTATION-NOTES.md` — feasibility notes for movement, Linux, and host adapters.
- `docs/VISUAL-LAYER-CONTRACT.md` — activity, playback, input-status, routing, and privacy contracts.
- `docs/LIVE2D-RUNTIME-INTEGRATION.md` — the Live2D ownership seam, provenance, and local development commands.
- `docs/RELEASE-GATE.md` — the promotion contract.
- `.github/workflows/e2e.yml` — reusable Windows E2E gate.
- `.github/workflows/promote.yml` — manual, secret-gated release projection.

## Development loop

```powershell
python tools/e2e_check.py --source .
python tools/project_release.py --source . --output dist
```

The projected artifact contains only `skills/codex-voice`. It deliberately does not carry the lab roadmap, recordings, experiments, or other project-only material.

The Live2D runtime is developed and tested as part of this lab, but its model
registry and materialized avatar bundles remain user-owned runtime data. The
generic Codex Voice bridge receives semantic avatar state only; it does not
receive model paths or compiled Cubism operations.

## Promotion

The release repository's pull requests call the reusable E2E workflow from this repository. The manual promotion workflow can update the release repository only after the gate passes and `RELEASE_REPO_TOKEN` has been added as a repository secret with write access to `walkingIssue/Codex-AI-presence`.

Create that secret only when you are ready to enable automated promotion; no SSH key is required:

```powershell
$env:RELEASE_REPO_TOKEN | gh secret set RELEASE_REPO_TOKEN --repo walkingIssue/Codex-AI-presence-lab
```

Never commit the token or an SSH private key to this repository.
