param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$voiceRoot = $PSScriptRoot
$projectRoot = Split-Path -Parent $voiceRoot
$python = Join-Path $voiceRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $voiceRoot "app_server_launcher.py"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Codex voice CPU environment is missing: $python"
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Codex app-server launcher is missing: $launcher"
}

Set-Location -LiteralPath $projectRoot
& $python $launcher --project-root $projectRoot @Arguments
exit $LASTEXITCODE
