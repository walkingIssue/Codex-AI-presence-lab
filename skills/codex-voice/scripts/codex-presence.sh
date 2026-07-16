#!/usr/bin/env bash

set -euo pipefail

# This file is intended to be installed as the user's global `codex` shim.
# Keep the real binary separate so the presence launcher can start app-server
# and the remote TUI without recursively invoking this shim.
if [[ "${CODEX_PRESENCE_BYPASS:-0}" == "1" ]]; then
    real_codex="${CODEX_REAL_BIN:-$HOME/.codex/packages/standalone/current/bin/codex}"
    exec "$real_codex" "$@"
fi

real_codex="${CODEX_REAL_BIN:-}"
if [[ -z "$real_codex" ]]; then
    for candidate in \
        "$HOME/.codex/packages/standalone/current/bin/codex" \
        "$HOME/.local/bin/codex.real"; do
        if [[ -x "$candidate" ]]; then
            real_codex="$candidate"
            break
        fi
    done
fi
if [[ -z "$real_codex" || ! -x "$real_codex" ]]; then
    printf 'Could not locate the real Codex binary. Set CODEX_REAL_BIN to its path.\n' >&2
    exit 1
fi

# These commands are intentionally direct: they are used by setup, login,
# diagnostics, and by the presence launcher itself.
case "${1:-}" in
    app-server|exec|login|logout|mcp|completion|update|--help|--version)
        exec "$real_codex" "$@"
        ;;
esac

project_root="${CODEX_PRESENCE_PROJECT_ROOT:-$PWD}"
project_root="$(cd -- "$project_root" && pwd -P)"
voice_root="${CODEX_PRESENCE_VOICE_ROOT:-$project_root/.codex-voice}"

# Prefer the project-wide skill reinstall requested for this workstation, but
# also support the conventional Codex skill location and an explicit override.
skill_roots=()
if [[ -n "${CODEX_PRESENCE_SKILL_ROOT:-}" ]]; then
    skill_roots+=("$CODEX_PRESENCE_SKILL_ROOT")
fi
skill_roots+=(
    "$HOME/source/.codex/skills/codex-voice"
    "$HOME/.codex/skills/codex-voice"
)
launcher=""
for skill_root in "${skill_roots[@]}"; do
    candidate="$skill_root/scripts/launch_codex.sh"
    if [[ -f "$candidate" ]]; then
        launcher="$candidate"
        break
    fi
done

if [[ -z "$launcher" || ! -d "$voice_root" ]]; then
    # Unconfigured projects keep the normal Codex behavior. A configured
    # project is always routed through the bridge above.
    exec "$real_codex" "$@"
fi

export CODEX_CLI="$real_codex"
export CODEX_PRESENCE_PROJECT_ROOT="$project_root"
export CODEX_PRESENCE_VOICE_ROOT="$(cd -- "$voice_root" && pwd -P)"
exec "$launcher" "$@"
