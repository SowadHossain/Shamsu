[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$Port = 5174,
    [string]$Workspace = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not $Workspace) {
    $Workspace = $RepoRoot.Path
}

if (Test-Path $VenvPython) {
    $Python = $VenvPython
}
else {
    $Python = "python"
}

Write-Host "Starting SHAMSU Web UI at http://localhost:$Port"
Write-Host "Press Ctrl+C to stop."
Push-Location $RepoRoot
try {
    & $Python -m shamsu.web.server --port $Port --workspace $Workspace
}
finally {
    Pop-Location
}
