# Lifecycle Policy

The Markdown lifecycle manifest is the human-reviewed source of truth; `installation.json` is the machine-readable receipt created at install time. Both are required because an uninstall must be able to prove what it owns even after the source checkout is gone.

## Required ownership fields

Every installed component must declare:

- owner and schema version;
- exact file or directory boundary;
- process name and PID marker, when it starts a process;
- listener/port/socket ownership, when it opens one;
- install command, status command, and removal command;
- whether an asset is copied, downloaded, linked, or merely referenced.

## Uninstall discipline

- Never infer ownership from a filename alone.
- Require an installation marker with the expected schema before deleting a boundary.
- Keep global model assets and project bindings separate so removing one never silently removes the other.
- Treat `codex-voice` as an external owner until its bridge contract explicitly delegates a file or process to this runtime.
- Reinstall the discoverable skill from the repository source after its contents change; do not leave a hand-edited deployed copy as the only version.
- Treat a profile scaffold as user-owned even when this runtime generated its initial draft. It must use an explicit output path, require confirmation before replacement, and remain outside model/project uninstall boundaries.
