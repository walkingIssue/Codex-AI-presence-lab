#!/usr/bin/env bash

set -euo pipefail

voice_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(dirname -- "$voice_root")"
python="$voice_root/.venv/bin/python"
codex_home="${CODEX_HOME:-$HOME/.codex}"
toggle="$codex_home/skills/codex-voice/scripts/toggle.py"

if [[ ! -x "$python" ]]; then
    printf 'Codex voice CPU environment is missing: %s\n' "$python" >&2
    exit 1
fi
if [[ ! -f "$toggle" ]]; then
    printf 'Codex voice toggle script is missing: %s\n' "$toggle" >&2
    exit 1
fi

cd -- "$project_root"
exec "$python" "$toggle" on
