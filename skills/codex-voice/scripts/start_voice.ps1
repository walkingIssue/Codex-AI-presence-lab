param()

$ErrorActionPreference = "Stop"
$voiceRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $voiceRoot
$python = Join-Path $voiceRoot ".venv\Scripts\python.exe"
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$toggle = Join-Path $codexHome "skills\codex-voice\scripts\toggle.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Codex voice CPU environment is missing: $python"
}
if (-not (Test-Path -LiteralPath $toggle -PathType Leaf)) {
    throw "Codex voice toggle script is missing: $toggle"
}

Set-Location -LiteralPath $projectRoot
& $python $toggle on
if ($LASTEXITCODE -ne 0) {
    throw "Could not start the Codex voice watcher (exit code $LASTEXITCODE)."
}

$orbMarker = Join-Path $voiceRoot "orb.enabled"
$orbStarter = Join-Path $voiceRoot "orb\start_orb.ps1"
if ((Test-Path -LiteralPath $orbMarker -PathType Leaf) -and (Test-Path -LiteralPath $orbStarter -PathType Leaf)) {
    & $orbStarter
}
