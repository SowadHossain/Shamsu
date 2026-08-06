[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Workspace = (Get-Location).Path,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$env:PYTHONUTF8 = "1"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Local .venv not found. Run scripts\install.ps1 from the SHAMSU repo first."
}

$ResolvedWorkspace = Resolve-Path $Workspace
if ($Json) {
    & $VenvPython -m shamsu.runtime.doctor --workspace $ResolvedWorkspace --json
}
else {
    & $VenvPython -m shamsu.runtime.doctor --workspace $ResolvedWorkspace
}
