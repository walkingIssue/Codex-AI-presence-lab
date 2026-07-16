#!/usr/bin/env bash
set -euo pipefail

runtime_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$runtime_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m live2d_avatar "$@"
