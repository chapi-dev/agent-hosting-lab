<#
.SYNOPSIS
    Copies the shared agent into the hosted agent's deployment directory.

.DESCRIPTION
    A Foundry hosted agent deploys a directory. Everything the entry point
    imports has to be inside it - there is no editable install, no PYTHONPATH,
    and no way to reference a sibling package.

    So `src/agent_core/` is copied into `src/hosted/agent_core/` before every
    deploy. The copy is generated and gitignored; `src/agent_core/` is the
    source of truth and the only one to edit.

    Forgetting this step is quiet rather than loud: azd deploys the stale copy,
    the deployment succeeds, and the agent runs code you changed hours ago.
    Run it from CI as well as locally.

.EXAMPLE
    ./scripts/prepare-hosted.ps1
    azd deploy trip-planner
#>
[CmdletBinding()]
param(
    [string] $Source = "src/agent_core",
    [string] $Destination = "src/hosted/agent_core"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$src = Join-Path $repoRoot $Source
$dst = Join-Path $repoRoot $Destination

if (-not (Test-Path $src)) {
    throw "Shared agent not found at $src"
}

if (Test-Path $dst) {
    Remove-Item -Recurse -Force $dst
}

New-Item -ItemType Directory -Path $dst -Force | Out-Null
Copy-Item -Path (Join-Path $src "*.py") -Destination $dst -Force

$copied = Get-ChildItem $dst -Filter *.py | Select-Object -ExpandProperty Name
Write-Host "prepared $Destination : $($copied -join ', ')" -ForegroundColor Green

# Fail fast rather than let azd deploy something that cannot import.
$entry = Join-Path $repoRoot "src/hosted/main.py"
if (-not (Test-Path $entry)) {
    throw "Entry point missing: src/hosted/main.py"
}
foreach ($required in @("__init__.py", "agent.py", "state.py")) {
    if (-not (Test-Path (Join-Path $dst $required))) {
        throw "Expected $required in $Destination"
    }
}

Write-Host "ready to deploy: azd deploy trip-planner" -ForegroundColor Cyan
