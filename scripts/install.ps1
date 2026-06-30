param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "SHAMSU installer"
Write-Host "Repo: $RepoRoot"
Write-Host "Creating local virtual environment: $VenvDir"

Push-Location $RepoRoot
try {
    if (-not (Test-Path $VenvPython)) {
        & $Python -m venv $VenvDir
    }

    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -e ".[dev]"

    Write-Host ""
    Write-Host "Install complete."
    Write-Host "Run from any workspace with:"
    Write-Host "  & `"$RepoRoot\scripts\run-shamsu.ps1`""
}
finally {
    Pop-Location
}
