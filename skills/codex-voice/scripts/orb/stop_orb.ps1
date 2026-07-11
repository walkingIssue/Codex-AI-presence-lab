param()

$ErrorActionPreference = "SilentlyContinue"
$pidPath = Join-Path $PSScriptRoot "orb.pid"
if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    try {
        $orbPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
        Stop-Process -Id $orbPid -Force
    } catch {
        # The process may already have exited.
    }
}
