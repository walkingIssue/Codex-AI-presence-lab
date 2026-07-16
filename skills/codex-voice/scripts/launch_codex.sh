#!/usr/bin/env bash

set -euo pipefail

script_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="${CODEX_PRESENCE_PROJECT_ROOT:-$PWD}"
project_root="$(cd -- "$project_root" && pwd)"
voice_root="${CODEX_PRESENCE_VOICE_ROOT:-$project_root/.codex-voice}"
if [[ ! -d "$voice_root" ]]; then
    printf 'Project-local voice runtime is missing: %s\n' "$voice_root" >&2
    exit 1
fi
voice_root="$(cd -- "$voice_root" && pwd)"
launcher="$script_root/launch_codex.py"
if [[ -f "$voice_root/launch_codex.py" ]]; then
    launcher="$voice_root/launch_codex.py"
fi
python="$voice_root/.venv/bin/python"
provider=""
if [[ -f "$voice_root/provider" ]]; then
    provider="$(<"$voice_root/provider")"
fi
if [[ "$provider" == "openvino" && -x "$voice_root/.openvino-venv/bin/python" ]]; then
    python="$voice_root/.openvino-venv/bin/python"
fi

if [[ ! -x "$python" ]]; then
    printf 'Codex voice runtime is missing: %s\n' "$python" >&2
    exit 1
fi
if [[ ! -f "$launcher" ]]; then
    printf 'Codex presence launcher is missing: %s\n' "$launcher" >&2
    exit 1
fi

cd -- "$project_root"
exec "$python" "$launcher" --project-root "$project_root" --voice-root "$voice_root" "$@"
