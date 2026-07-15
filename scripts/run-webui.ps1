[CmdletBinding(PositionalBinding = $false)]
param(
    [int]$Port = 5174
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$WebRoot = Join-Path $RepoRoot "webui"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $WebRoot)) {
    Write-Error "webui folder not found."
}

if (Test-Path $VenvPython) {
    $Python = $VenvPython
}
else {
    $Python = "python"
}

Write-Host "Starting SHAMSU Web UI at http://localhost:$Port"
Write-Host "Press Ctrl+C to stop."
Push-Location $WebRoot
try {
    & $Python -m http.server $Port --bind 127.0.0.1
}
finally {
    Pop-Location
}
