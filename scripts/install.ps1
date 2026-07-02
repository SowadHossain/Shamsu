param(
    [string]$Python = "python",
    [switch]$Yes,
    [switch]$SkipOllamaInstall,
    [switch]$SkipModels,
    [string]$ModelsPath = ""
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

    if ($ModelsPath) {
        $env:OLLAMA_MODELS = $ModelsPath
        Write-Host "Using Ollama model directory for this install run: $ModelsPath"
    }

    $RuntimeStatusJson = & $VenvPython -m shamsu.runtime.ollama status --json
    $RuntimeStatus = $RuntimeStatusJson | ConvertFrom-Json

    if (-not $RuntimeStatus.ollama_path -and -not $SkipOllamaInstall) {
        $InstallOllama = $Yes
        if (-not $InstallOllama) {
            $Answer = Read-Host "Ollama is required for local inference. Install Ollama with winget now? [y/N]"
            $InstallOllama = $Answer.ToLowerInvariant() -in @("y", "yes")
        }
        if ($InstallOllama) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                Write-Host "Installing Ollama through winget. SHAMSU will not edit PATH or shell profiles."
                winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
            }
            else {
                Write-Warning "winget was not found. Install Ollama from https://ollama.com/download, then rerun this script."
            }
        }
    }

    $RuntimeStatusJson = & $VenvPython -m shamsu.runtime.ollama status --json
    $RuntimeStatus = $RuntimeStatusJson | ConvertFrom-Json

    if (-not $SkipModels -and $RuntimeStatus.ollama_path) {
        & $VenvPython -m shamsu.runtime.ollama repair
    }
    elseif (-not $RuntimeStatus.ollama_path) {
        Write-Warning "Ollama is still missing. SHAMSU installed, but local inference needs `models repair` after Ollama is installed."
    }

    & $VenvPython -m shamsu.runtime.ollama write-config

    Write-Host ""
    Write-Host "Install complete."
    Write-Host "SHAMSU did not edit your PowerShell profile, PATH, registry, or global Python."
    Write-Host "Run from any workspace with:"
    Write-Host "  & `"$RepoRoot\scripts\run-shamsu.ps1`""
}
finally {
    Pop-Location
}
