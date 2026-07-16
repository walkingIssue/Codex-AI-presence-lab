[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'

if (-not $Yes) {
    throw 'Runtime uninstall requires -Yes.'
}

$CodexRoot = Join-Path $env:USERPROFILE '.codex'
$RuntimeRoot = Join-Path $CodexRoot 'live2d-avatar-runtime'
$ExpectedRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
if (-not (Test-Path -LiteralPath $RuntimeRoot)) {
    Write-Output "Live2D runtime is not installed: $RuntimeRoot"
    exit 0
}

$ResolvedRoot = (Resolve-Path -LiteralPath $RuntimeRoot).Path
if (-not [string]::Equals($ResolvedRoot, $ExpectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove an unexpected runtime path: $ResolvedRoot"
}

$MarkerPath = Join-Path $RuntimeRoot 'installation.json'
if (-not (Test-Path -LiteralPath $MarkerPath)) {
    throw "Refusing to remove runtime without installation marker: $MarkerPath"
}
$marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
if ($marker.schema -ne 'live2d-avatar/runtime-install/v0.1' -or $marker.owner -ne 'live2d-avatar-runtime') {
    throw 'Refusing to remove a runtime not owned by live2d-avatar-runtime.'
}

Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
Write-Output "Removed live2d-avatar runtime at $RuntimeRoot"
Write-Output 'Imported models were preserved. Remove one explicitly with live2d-avatar model remove <id> --yes.'
