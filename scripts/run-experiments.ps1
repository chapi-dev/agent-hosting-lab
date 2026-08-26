<#
.SYNOPSIS
    Runs every experiment against the deployed lab.

.DESCRIPTION
    Sources .env.lab, then runs the six experiments in order. Results are
    written to experiments/results/ as timestamped JSON - those files are the
    evidence behind every number in docs/ and README.md.

      01  session state       does the agent remember across replicas?
      02  cold start          what does the first turn of a session cost?
      03  deployment surface  how much code does each model make you own?
      04  pre-warm            does priming hide the hosted cold start?
      05  session pre-create  body field vs header for attaching a session
      06  session isolation   what actually keeps one user out of another's state?

    Experiment 3 is static analysis and needs no deployment. The others call
    the live endpoints.

.PARAMETER Rounds
    Repetitions for the timing experiments. More rounds, tighter medians.

.EXAMPLE
    ./scripts/run-experiments.ps1
    ./scripts/run-experiments.ps1 -Rounds 5 -Only 01,02
#>
[CmdletBinding()]
param(
    [int] $Rounds = 4,
    [string[]] $Only
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$envFile = Join-Path $repoRoot ".env.lab"
if (-not (Test-Path $envFile)) {
    throw ".env.lab not found. Run ./scripts/deploy.ps1 first."
}

Get-Content $envFile | ForEach-Object {
    $key, $value = $_ -split '=', 2
    if ($key) { Set-Item -Path "env:$key" -Value $value }
}

# Python on a Windows console defaults to cp1252 and dies on the arrows and
# box characters the experiments print.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$python = Join-Path $repoRoot ".venv/Scripts/python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$experiments = @(
    @{ Id = "01"; Script = "experiments/01_session_state.py";    Args = @() }
    @{ Id = "02"; Script = "experiments/02_cold_start.py";       Args = @("--rounds", $Rounds) }
    @{ Id = "03"; Script = "experiments/03_deployment_surface.py"; Args = @() }
    @{ Id = "04"; Script = "experiments/04_prewarm.py";          Args = @("--rounds", $Rounds) }
    @{ Id = "05"; Script = "experiments/05_session_precreate.py"; Args = @("--rounds", $Rounds) }
    @{ Id = "06"; Script = "experiments/06_session_isolation.py"; Args = @() }
)

$failed = @()
# PowerShell parses `-Only 01,02` as the integers 1 and 2, which never match
# the zero-padded ids and would silently skip every experiment - the run looks
# successful and does nothing. Pad whatever we were given back to two digits.
$selected = $null
if ($Only) {
    $selected = @($Only | ForEach-Object { ([string]$_).Trim().PadLeft(2, '0') })
    $unknown = @($selected | Where-Object { $_ -notin $experiments.Id })
    if ($unknown) { throw "Unknown experiment id(s): $($unknown -join ', '). Valid: $($experiments.Id -join ', ')" }
}

foreach ($experiment in $experiments) {
    if ($selected -and ($experiment.Id -notin $selected)) { continue }

    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor DarkGray
    Write-Host "  $($experiment.Script)" -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor DarkGray

    & $python $experiment.Script @($experiment.Args)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $($experiment.Script)" -ForegroundColor Red
        $failed += $experiment.Id
    }
}

Write-Host ""
if ($failed) {
    Write-Host "Experiments failed: $($failed -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host "All experiments completed. Results in experiments/results/" -ForegroundColor Green
