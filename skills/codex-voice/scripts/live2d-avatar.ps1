[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AvatarArguments
)

$ErrorActionPreference = 'Stop'

$Python = Get-Command python -ErrorAction SilentlyContinue
$PythonArguments = @()
if (-not $Python) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
    $PythonArguments = @('-3')
}
if (-not $Python) {
    throw 'Python 3.11 or 3.12 is required for the bundled Live2D runtime.'
}

& $Python.Source @PythonArguments (Join-Path $PSScriptRoot 'live2d-avatar.py') @AvatarArguments
exit $LASTEXITCODE
