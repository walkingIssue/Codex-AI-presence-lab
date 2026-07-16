[CmdletBinding()]
param(
    [switch]$Reinstall
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$CodexRoot = Join-Path $env:USERPROFILE '.codex'
$RuntimeRoot = Join-Path $CodexRoot 'live2d-avatar-runtime'
$MarkerPath = Join-Path $RuntimeRoot 'installation.json'
$VenvPath = Join-Path $RuntimeRoot 'venv'
$PackageRoot = Join-Path $RuntimeRoot 'package'
$ManifestSource = Join-Path $RepoRoot 'RUNTIME-MANIFEST.md'

if (Test-Path -LiteralPath $RuntimeRoot) {
    if (-not $Reinstall) {
        throw "Live2D runtime is already installed at $RuntimeRoot. Use -Reinstall after reviewing its lifecycle manifest."
    }
    & (Join-Path $PSScriptRoot 'uninstall-runtime.ps1') -Yes
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$createdRuntime = $true
try {
    & py -3.12 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the isolated Python runtime.'
    }
    New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $RepoRoot 'src\live2d_avatar') -Destination (Join-Path $PackageRoot 'live2d_avatar') -Recurse

    $installation = [ordered]@{
        schema = 'live2d-avatar/runtime-install/v0.1'
        owner = 'live2d-avatar-runtime'
        source = [System.IO.Path]::GetFullPath($RepoRoot)
        owned_paths = @('venv', 'package', 'installation.json', 'RUNTIME-MANIFEST.md')
        owned_processes = @()
        owned_listeners = @()
    }
    $temporary = Join-Path $RuntimeRoot '.installation.json.tmp'
    $installation | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $MarkerPath -Force
    Copy-Item -LiteralPath $ManifestSource -Destination (Join-Path $RuntimeRoot 'RUNTIME-MANIFEST.md') -Force

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = $PackageRoot
    & (Join-Path $VenvPath 'Scripts\python.exe') -m live2d_avatar --help | Out-Null
    $env:PYTHONPATH = $previousPythonPath
    if ($LASTEXITCODE -ne 0) {
        throw 'Installed runtime did not pass its command smoke test.'
    }
}
catch {
    if ($createdRuntime -and -not (Test-Path -LiteralPath $MarkerPath) -and (Test-Path -LiteralPath $RuntimeRoot)) {
        $expected = [System.IO.Path]::GetFullPath($RuntimeRoot)
        $resolved = (Resolve-Path -LiteralPath $RuntimeRoot).Path
        if ([string]::Equals($resolved, $expected, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
        }
    }
    throw
}

Write-Output "Installed live2d-avatar runtime at $RuntimeRoot"
Write-Output "Python: $(Join-Path $VenvPath 'Scripts\python.exe')"
Write-Output "Package: $PackageRoot"
