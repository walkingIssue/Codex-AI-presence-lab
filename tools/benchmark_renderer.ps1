param(
    [Parameter(Mandatory = $true)]
    [string]$AvatarBundle,

    [ValidateRange(1, 10)]
    [int]$Runs = 3,

    [ValidateRange(1, 120)]
    [int]$WarmupSeconds = 8,

    [ValidateRange(1, 120)]
    [int]$SampleSeconds = 12,

    [string]$Label = "benchmark",

    [string]$ElectronNodeModules = "C:\Users\Bartek\Documents\Playground\.codex-voice\orb\node_modules",

    [string]$OutputPath,

    [string]$ProfilesPath,

    [switch]$SimulateSpeaking,

    [switch]$KeepRuntime
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$orbSource = Join-Path $repoRoot "skills\codex-voice\scripts\orb"
$electron = Join-Path $ElectronNodeModules "electron\dist\electron.exe"
$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$runtimeRoot = Join-Path $tempBase "codex-presence-sol-renderer-benchmark"
$projectRoot = Join-Path $runtimeRoot "project"
$voiceRoot = Join-Path $projectRoot ".codex-voice"
$orbRoot = Join-Path $voiceRoot "orb"
$junctionPath = Join-Path $orbRoot "node_modules"
$pidPath = Join-Path $orbRoot "orb.pid"
$port = 18931

function Assert-UnderTemp([string]$Path) {
    $resolved = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to mutate benchmark path outside the system temp directory: $resolved"
    }
}

function Remove-BenchmarkRuntime {
    Assert-UnderTemp $runtimeRoot
    if (Test-Path -LiteralPath $junctionPath) {
        $junction = Get-Item -LiteralPath $junctionPath -Force
        if (($junction.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) {
            throw "Refusing to remove a non-junction node_modules path: $junctionPath"
        }
        [System.IO.Directory]::Delete($junctionPath)
    }
    if (Test-Path -LiteralPath $runtimeRoot) {
        Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
    }
}

function Get-BenchmarkProcesses([string]$UserData, [int]$RootPid) {
    $resolvedUserData = [System.IO.Path]::GetFullPath($UserData)
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                [int]$_.ProcessId -eq $RootPid -or (
                    [string]$_.CommandLine
                ).IndexOf($resolvedUserData, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
            } |
            Select-Object ProcessId, ParentProcessId, CommandLine
    )
}

function Get-ProcessRole([string]$CommandLine, [int]$ProcessId, [int]$RootPid) {
    if ($ProcessId -eq $RootPid) { return "main" }
    if ($CommandLine -match "--type=renderer") { return "renderer" }
    if ($CommandLine -match "--type=gpu-process") { return "gpu" }
    if ($CommandLine -match "--type=utility") { return "utility" }
    if ($CommandLine -match "--type=crashpad-handler") { return "crashpad" }
    return "other"
}

function Stop-BenchmarkProcesses([string]$UserData, [int]$RootPid) {
    $processes = @(Get-BenchmarkProcesses $UserData $RootPid | Sort-Object ProcessId -Descending)
    foreach ($entry in $processes) {
        Stop-Process -Id ([int]$entry.ProcessId) -Force -ErrorAction SilentlyContinue
    }
}

function Send-AudioEvent([int]$DestinationPort, [hashtable]$Payload) {
    $client = [System.Net.Sockets.UdpClient]::new()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($Payload | ConvertTo-Json -Compress))
        [void]$client.Send($bytes, $bytes.Length, "127.0.0.1", $DestinationPort)
    }
    finally {
        $client.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $orbSource -PathType Container)) {
    throw "Orb source was not found at $orbSource"
}
if (-not (Test-Path -LiteralPath $electron -PathType Leaf)) {
    throw "Electron runtime was not found at $electron"
}
if (-not (Test-Path -LiteralPath $AvatarBundle -PathType Container)) {
    throw "Avatar bundle was not found at $AvatarBundle"
}

$avatarManifestPath = Join-Path $AvatarBundle "avatar.json"
$avatarManifest = Get-Content -LiteralPath $avatarManifestPath -Raw | ConvertFrom-Json
$avatarId = [string]$avatarManifest.id
if ($avatarId -notmatch "^[a-z0-9][a-z0-9-]{0,63}$") {
    throw "Avatar manifest has an invalid id: $avatarId"
}

$profileWindowCount = 1
if ($ProfilesPath) {
    if (-not (Test-Path -LiteralPath $ProfilesPath -PathType Leaf)) {
        throw "Presence profile document was not found at $ProfilesPath"
    }
    $profileDocument = Get-Content -LiteralPath $ProfilesPath -Raw | ConvertFrom-Json
    if ($profileDocument.schema -ne "codex-ai-presence/profiles/v0.1") {
        throw "Presence profile document has an unsupported schema"
    }
    if ($null -ne $profileDocument.sessions) {
        $profileWindowCount = [Math]::Max(1, @($profileDocument.sessions.PSObject.Properties).Count)
    }
}

Remove-BenchmarkRuntime
New-Item -ItemType Directory -Path $orbRoot -Force | Out-Null
Copy-Item -Path (Join-Path $orbSource "*") -Destination $orbRoot -Recurse -Force
New-Item -ItemType Junction -Path $junctionPath -Target $ElectronNodeModules | Out-Null

$avatarDestination = Join-Path (Join-Path $projectRoot ".codex-voice-avatars") $avatarId
New-Item -ItemType Directory -Path $avatarDestination -Force | Out-Null
Copy-Item -Path (Join-Path $AvatarBundle "*") -Destination $avatarDestination -Recurse -Force

$selectionJson = @{
    schema = "codex-ai-presence/avatar-selection/v0.1"
    avatar_id = $avatarId
} | ConvertTo-Json
[System.IO.File]::WriteAllText(
    (Join-Path $voiceRoot "avatar-selection.json"),
    $selectionJson,
    [System.Text.UTF8Encoding]::new($false)
)
if ($ProfilesPath) {
    Copy-Item -LiteralPath $ProfilesPath -Destination (Join-Path $voiceRoot "presence-profiles.json") -Force
}

$samples = @()
$previousPort = $env:CODEX_ORB_PORT
$activeUserData = $null
$activeRootPid = 0
try {
    for ($run = 1; $run -le $Runs; $run += 1) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        $userData = Join-Path $runtimeRoot "user-data-$run"
        $activeUserData = $userData
        New-Item -ItemType Directory -Path $userData -Force | Out-Null
        $env:CODEX_ORB_PORT = [string]($port + $run)
        $arguments = @("--user-data-dir=$userData", $orbRoot)
        $launcher = Start-Process -FilePath $electron -ArgumentList $arguments -WorkingDirectory $orbRoot -WindowStyle Hidden -PassThru
        $activeRootPid = $launcher.Id

        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
            if ([DateTime]::UtcNow -ge $deadline) {
                Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
                throw "Timed out waiting for the benchmark Orb PID file"
            }
            Start-Sleep -Milliseconds 100
        }
        $rootPid = [int](Get-Content -LiteralPath $pidPath -Raw).Trim()
        $activeRootPid = $rootPid
        Start-Sleep -Seconds $WarmupSeconds
        if ($SimulateSpeaking) {
            Send-AudioEvent ($port + $run) @{ type = "state"; state = "speaking" }
            Start-Sleep -Milliseconds 750
        }

        $tree = @(Get-BenchmarkProcesses $userData $rootPid)
        $before = @{}
        foreach ($entry in $tree) {
            $process = Get-Process -Id ([int]$entry.ProcessId) -ErrorAction SilentlyContinue
            if ($null -ne $process) {
                $before[[int]$entry.ProcessId] = [double]$process.CPU
            }
        }

        Start-Sleep -Seconds $SampleSeconds
        $runSamples = @()
        foreach ($entry in $tree) {
            $processId = [int]$entry.ProcessId
            if (-not $before.ContainsKey($processId)) { continue }
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($null -eq $process) { continue }
            $cpuPercent = (([double]$process.CPU - [double]$before[$processId]) / $SampleSeconds) * 100
            $runSamples += [pscustomobject]@{
                run = $run
                pid = $processId
                role = Get-ProcessRole ([string]$entry.CommandLine) $processId $rootPid
                cpu_percent = [Math]::Round($cpuPercent, 2)
                working_set_mb = [Math]::Round(([double]$process.WorkingSet64 / 1MB), 2)
            }
        }
        $samples += $runSamples
        Stop-BenchmarkProcesses $userData $rootPid
        $activeUserData = $null
        $activeRootPid = 0
        Start-Sleep -Milliseconds 750
    }
}
finally {
    $env:CODEX_ORB_PORT = $previousPort
    if ($activeUserData -and $activeRootPid -gt 0) {
        Stop-BenchmarkProcesses $activeUserData $activeRootPid
    }
}

$runsSummary = @()
foreach ($run in 1..$Runs) {
    $runRows = @($samples | Where-Object run -eq $run)
    $runsSummary += [pscustomobject]@{
        run = $run
        total_cpu_percent = [Math]::Round((($runRows | Measure-Object cpu_percent -Sum).Sum), 2)
        renderer_cpu_percent = [Math]::Round((($runRows | Where-Object role -eq "renderer" | Measure-Object cpu_percent -Sum).Sum), 2)
        gpu_cpu_percent = [Math]::Round((($runRows | Where-Object role -eq "gpu" | Measure-Object cpu_percent -Sum).Sum), 2)
        total_working_set_mb = [Math]::Round((($runRows | Measure-Object working_set_mb -Sum).Sum), 2)
    }
}

$orderedTotals = @($runsSummary.total_cpu_percent | Sort-Object)
$medianTotal = if ($orderedTotals.Count % 2 -eq 1) {
    $orderedTotals[[Math]::Floor($orderedTotals.Count / 2)]
} else {
    ($orderedTotals[$orderedTotals.Count / 2 - 1] + $orderedTotals[$orderedTotals.Count / 2]) / 2
}

$result = [pscustomobject]@{
    schema = "codex-ai-presence/renderer-benchmark/v0.1"
    label = $Label
    avatar_id = $avatarId
    measured_at = [DateTime]::UtcNow.ToString("o")
    warmup_seconds = $WarmupSeconds
    sample_seconds = $SampleSeconds
    run_count = $Runs
    profile_window_count = $profileWindowCount
    median_total_cpu_percent = [Math]::Round($medianTotal, 2)
    runs = $runsSummary
    processes = $samples
}

$json = $result | ConvertTo-Json -Depth 6
if ($OutputPath) {
    $outputDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputPath))
    if ($outputDirectory) { New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null }
    $json | Set-Content -LiteralPath $OutputPath -Encoding utf8
}
$json

if (-not $KeepRuntime) {
    Remove-BenchmarkRuntime
}
