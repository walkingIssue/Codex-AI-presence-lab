#!/usr/bin/env bash

set -euo pipefail

project_root="${CODEX_PRESENCE_PROJECT_ROOT:-$PWD}"
project_root="$(cd -- "$project_root" && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
python="$codex_home/presence/.venv/bin/python"
launcher="$codex_home/presence/adapters/codex/launch_codex.py"
voice_root="$project_root/.codex-voice/v0.2"

if [[ ! -x "$python" ]]; then
    printf 'Presence Runtime Python is missing: %s\n' "$python" >&2
    exit 1
fi
if [[ ! -f "$launcher" ]]; then
    printf 'Managed Codex TUI adapter is missing: %s\n' "$launcher" >&2
    exit 1
fi
mkdir -p -- "$voice_root"
cd -- "$project_root"
exec "$python" "$launcher" --project-root "$project_root" --voice-root "$voice_root" "$@"
