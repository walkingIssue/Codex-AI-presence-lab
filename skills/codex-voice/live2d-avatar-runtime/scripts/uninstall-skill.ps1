[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'

if (-not $Yes) {
    throw 'Skill uninstall requires -Yes.'
}

$SkillsRoot = Join-Path $env:USERPROFILE '.codex\skills'
$TargetSkill = Join-Path $SkillsRoot 'live2d-avatar-controls'
$ExpectedTarget = [System.IO.Path]::GetFullPath($TargetSkill)
if (-not (Test-Path -LiteralPath $TargetSkill)) {
    Write-Output "Skill is not installed: $TargetSkill"
    exit 0
}

$ResolvedTarget = (Resolve-Path -LiteralPath $TargetSkill).Path
if (-not [string]::Equals($ResolvedTarget, $ExpectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove an unexpected skill path: $ResolvedTarget"
}
$SkillDocument = Get-Content -LiteralPath (Join-Path $TargetSkill 'SKILL.md') -Raw
if ($SkillDocument -notmatch '(?m)^name:\s*live2d-avatar-controls\s*$') {
    throw 'Refusing to remove a skill that does not identify as live2d-avatar-controls.'
}

Remove-Item -LiteralPath $TargetSkill -Recurse -Force
Write-Output "Removed live2d-avatar-controls skill at $TargetSkill"
