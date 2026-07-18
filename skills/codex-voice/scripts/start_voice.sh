#!/usr/bin/env bash

set -euo pipefail

voice_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(dirname -- "$voice_root")"
codex_home="${CODEX_HOME:-$HOME/.codex}"
presence="$codex_home/bin/presence"

printf '%s\n' "warning: start_voice.sh is a v0.1 compatibility wrapper; use 'presence project register'." >&2
if [[ ! -x "$presence" ]]; then
    printf 'Presence Runtime launcher is missing: %s\n' "$presence" >&2
    exit 1
fi
exec "$presence" project register "$project_root"
