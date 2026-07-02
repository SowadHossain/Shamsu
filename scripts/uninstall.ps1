[CmdletBinding()]
param(
    [string]$BinDir = (Join-Path $HOME ".shamsu\bin"),
    [switch]$KeepVenv,
    [switch]$KeepLauncher
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$RuntimeDir = Join-Path $RepoRoot ".shamsu"
$PsLauncher = Join-Path $BinDir "shamsu.ps1"
$CmdLauncher = Join-Path $BinDir "shamsu.cmd"

Write-Host "SHAMSU uninstall"
Write-Host "Repo: $RepoRoot"

if (-not $KeepLauncher) {
    foreach ($launcher in @($PsLauncher, $CmdLauncher)) {
        if (Test-Path $launcher) {
            Remove-Item -LiteralPath $launcher -Force
            Write-Host "Removed launcher: $launcher"
        }
    }
}
else {
    Write-Host "Keeping user-local launchers."
}

if (-not $KeepVenv -and (Test-Path $VenvDir)) {
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
    Write-Host "Removed repo virtual environment: $VenvDir"
}
elseif ($KeepVenv) {
    Write-Host "Keeping repo virtual environment."
}

if (Test-Path $RuntimeDir) {
    Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
    Write-Host "Removed repo runtime state: $RuntimeDir"
}

Write-Host ""
Write-Host "SHAMSU uninstall complete."
Write-Host "This removed SHAMSU-managed files from this repo and your user-local launcher directory."
Write-Host "It did not remove Ollama or workspace .shamsu folders from your other projects."
