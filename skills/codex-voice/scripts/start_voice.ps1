param()

$ErrorActionPreference = "Stop"
$voiceRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $voiceRoot
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$presence = Join-Path $codexHome "bin\presence.ps1"

Write-Warning "start_voice.ps1 is a v0.1 compatibility wrapper; use 'presence project register'."
if (-not (Test-Path -LiteralPath $presence -PathType Leaf)) {
    throw "Presence Runtime launcher is missing: $presence"
}
& $presence project register $projectRoot
exit $LASTEXITCODE
