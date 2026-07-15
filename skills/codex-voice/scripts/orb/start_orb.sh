#!/usr/bin/env bash

set -euo pipefail

orb_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
electron="$orb_root/node_modules/electron/dist/electron"
pid_path="$orb_root/orb.pid"
log_path="$orb_root/orb.log"
unit_suffix="$(printf '%s' "$orb_root" | sha256sum | cut -c1-12)"
unit_name="codex-strand-orb-$unit_suffix"

if [[ -f "$pid_path" ]]; then
    pid="$(<"$pid_path")"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
        exit 0
    fi
fi

if command -v systemctl >/dev/null 2>&1 && [[ -n "${XDG_RUNTIME_DIR:-}" ]] \
    && systemctl --user is-active --quiet "$unit_name.service" 2>/dev/null; then
    exit 0
fi

if [[ ! -x "$electron" ]]; then
    node="$(command -v node || true)"
    install_script="$orb_root/node_modules/electron/install.js"
    if [[ -n "$node" && -f "$install_script" ]]; then
        "$node" "$install_script"
    fi
fi

if [[ ! -x "$electron" ]]; then
    printf 'Strand Orb Electron runtime is unavailable. Run npm ci in %s and retry.\n' "$orb_root" >&2
    exit 1
fi

cd -- "$orb_root"
ozone_platform="${CODEX_ORB_PLATFORM:-}"
if [[ -z "$ozone_platform" && "${XDG_SESSION_TYPE:-}" == "wayland" && -n "${DISPLAY:-}" ]]; then
    ozone_platform="x11"
fi
electron_args=("--no-sandbox")
if [[ -n "$ozone_platform" ]]; then
    electron_args+=("--ozone-platform=$ozone_platform")
fi
if [[ "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
    # Electron's Wayland global-shortcut support is exposed through the
    # desktop portal. The launcher still prefers XWayland when DISPLAY exists,
    # but this keeps the explicit interaction toggles available on native
    # Wayland compositors that provide the portal.
    electron_args+=("--enable-features=GlobalShortcutsPortal")
fi
electron_args+=("$orb_root")
printf '%s launch platform=%s display=%s wayland=%s\n' \
    "$(date --iso-8601=seconds)" \
    "${ozone_platform:-electron-default}" \
    "${DISPLAY:-none}" \
    "${WAYLAND_DISPLAY:-none}" >>"$log_path"

launched_with_systemd=false
if command -v systemd-run >/dev/null 2>&1 && [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
    if systemd-run --user --unit="$unit_name" --collect --quiet \
        --working-directory="$orb_root" \
        --property="StandardOutput=append:$log_path" \
        --property="StandardError=append:$log_path" \
        "$electron" "${electron_args[@]}"; then
        launched_with_systemd=true
    else
        printf '%s systemd user launch unavailable; using detached fallback\n' \
            "$(date --iso-8601=seconds)" >>"$log_path"
    fi
fi

if [[ "$launched_with_systemd" != true ]]; then
    nohup "$electron" "${electron_args[@]}" >>"$log_path" 2>&1 </dev/null &
    pid=$!
    printf '%s\n' "$pid" >"$pid_path"
fi

for _ in {1..50}; do
    if [[ -f "$pid_path" ]]; then
        pid="$(<"$pid_path")"
        if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
            exit 0
        fi
    fi
    sleep 0.1
done

rm -f "$pid_path"
printf 'Strand Orb failed to stay running; inspect %s.\n' "$log_path" >&2
exit 1
