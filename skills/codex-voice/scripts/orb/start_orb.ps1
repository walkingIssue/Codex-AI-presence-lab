param()

$ErrorActionPreference = "Stop"
$orbRoot = $PSScriptRoot
$electron = Join-Path $orbRoot "node_modules\electron\dist\electron.exe"
$pidPath = Join-Path $orbRoot "orb.pid"

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    try {
        $existingPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
        if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
            exit 0
        }
    } catch {
        # The stale PID file will be replaced by the next launch.
    }
}

if (-not (Test-Path -LiteralPath $electron -PathType Leaf)) {
    $node = Get-Command node -ErrorAction SilentlyContinue
    $installScript = Join-Path $orbRoot "node_modules\electron\install.js"
    if ($null -ne $node -and (Test-Path -LiteralPath $installScript -PathType Leaf)) {
        Write-Host "Electron runtime is missing; running Electron's installer..."
        & $node.Source $installScript
    }
}

if (-not (Test-Path -LiteralPath $electron -PathType Leaf)) {
    throw "Strand Orb Electron runtime is unavailable. Run npm ci in $orbRoot and retry."
}

$appArgument = '"' + $orbRoot + '"'
Start-Process -FilePath $electron -ArgumentList $appArgument -WorkingDirectory $orbRoot -WindowStyle Hidden | Out-Null
