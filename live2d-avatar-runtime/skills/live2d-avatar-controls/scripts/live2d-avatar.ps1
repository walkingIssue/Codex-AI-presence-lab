[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AvatarArguments
)

$ErrorActionPreference = 'Stop'

$RuntimePython = Join-Path $env:USERPROFILE '.codex\live2d-avatar-runtime\venv\Scripts\python.exe'
$PackageRoot = Join-Path $env:USERPROFILE '.codex\live2d-avatar-runtime\package'
if (-not (Test-Path -LiteralPath $RuntimePython) -or -not (Test-Path -LiteralPath (Join-Path $PackageRoot 'live2d_avatar'))) {
    throw "The Live2D runtime is not installed at $env:USERPROFILE\.codex\live2d-avatar-runtime. Run scripts/install-runtime.ps1 from the live2d-avatar-runtime repository."
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$PackageRoot;$env:PYTHONPATH" } else { $PackageRoot }
& $RuntimePython -m live2d_avatar @AvatarArguments
exit $LASTEXITCODE
