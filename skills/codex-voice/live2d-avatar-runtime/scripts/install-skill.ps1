[CmdletBinding()]
param(
    [switch]$Reinstall
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$SourceSkill = Join-Path $RepoRoot 'skills\live2d-avatar-controls'
$SkillsRoot = Join-Path $env:USERPROFILE '.codex\skills'
$TargetSkill = Join-Path $SkillsRoot 'live2d-avatar-controls'

if (-not (Test-Path -LiteralPath (Join-Path $SourceSkill 'SKILL.md'))) {
    throw "Skill source is incomplete: $SourceSkill"
}
if (Test-Path -LiteralPath $TargetSkill) {
    if (-not $Reinstall) {
        throw "Skill is already installed at $TargetSkill. Use -Reinstall to replace the managed copy."
    }
    & (Join-Path $PSScriptRoot 'uninstall-skill.ps1') -Yes
}

New-Item -ItemType Directory -Force -Path $SkillsRoot | Out-Null
Copy-Item -LiteralPath $SourceSkill -Destination $TargetSkill -Recurse
$InstalledSkillDocument = Get-Content -LiteralPath (Join-Path $TargetSkill 'SKILL.md') -Raw
if (
    $InstalledSkillDocument -notmatch '(?m)^name:\s*live2d-avatar-controls\s*$' -or
    $InstalledSkillDocument -notmatch '(?m)^description:\s*.+$'
) {
    throw 'Installed skill does not contain the required identity frontmatter.'
}
Write-Output "Installed live2d-avatar-controls skill at $TargetSkill"
