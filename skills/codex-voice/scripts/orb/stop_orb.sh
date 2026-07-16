#!/usr/bin/env bash

set -euo pipefail

orb_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pid_path="$orb_root/orb.pid"
unit_suffix="$(printf '%s' "$orb_root" | sha256sum | cut -c1-12)"
unit_name="codex-strand-orb-$unit_suffix"

if command -v systemctl >/dev/null 2>&1 && [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    systemctl --user stop "$unit_name.service" 2>/dev/null || true
fi

if [[ -f "$pid_path" ]]; then
    pid="$(<"$pid_path")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_path"
fi
