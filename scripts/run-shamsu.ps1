param(
    [string]$Workspace = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Local .venv not found. Run scripts\install.ps1 from the SHAMSU repo first."
}

$ResolvedWorkspace = Resolve-Path $Workspace
& $VenvPython -m shamsu.cli.repl --workspace $ResolvedWorkspace @args
