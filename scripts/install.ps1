param(
    [string]$Python = "python",
    [switch]$Yes,
    [switch]$SkipOllamaInstall,
    [switch]$SkipModels,
    [switch]$SkipCommandInstall,
    [string]$BinDir = (Join-Path $HOME ".shamsu\bin"),
    [string]$ModelsPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$env:PYTHONUTF8 = "1"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RunScript = Join-Path $RepoRoot "scripts\run-shamsu.ps1"
$InstalledLauncher = ""
$LauncherOnPath = $false

function Install-ShamsuLauncher {
    param(
        [string]$BinDir,
        [string]$RunScript
    )

    if (-not (Test-Path $RunScript)) {
        Write-Error "Could not find SHAMSU run script: $RunScript"
    }

    $ResolvedBinDir = [System.IO.Path]::GetFullPath($BinDir)
    New-Item -ItemType Directory -Force -Path $ResolvedBinDir | Out-Null

    $PsLauncher = Join-Path $ResolvedBinDir "shamsu.ps1"
    $CmdLauncher = Join-Path $ResolvedBinDir "shamsu.cmd"
    $EscapedRunScript = $RunScript.Replace("'", "''")
    $PsContent = @"
[CmdletBinding()]
param(
    [Parameter(ValueFromPipeline = `$true)]
    [string]`$InputObject,
    [Parameter(ValueFromRemainingArguments = `$true)]
    [string[]]`$ShamsuArgs
)

begin {
    `$ErrorActionPreference = "Stop"
    Set-StrictMode -Version Latest
    `$RunScript = '$EscapedRunScript'
    `$PipedInput = [System.Collections.Generic.List[string]]::new()
}

process {
    if (`$null -ne `$InputObject) {
        `$PipedInput.Add(`$InputObject)
    }
}

end {
    `$Workspace = (Get-Location).Path
    if (`$PipedInput.Count -gt 0) {
        & `$RunScript -Workspace `$Workspace -InputObject (`$PipedInput -join [Environment]::NewLine) @ShamsuArgs
    }
    else {
        & `$RunScript -Workspace `$Workspace @ShamsuArgs
    }
}
"@

    $CmdRunScript = $RunScript.Replace("%", "%%")
    $CmdContent = @"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "$CmdRunScript" -Workspace "%CD%" %*
"@

    Set-Content -Path $PsLauncher -Value $PsContent -Encoding UTF8
    Set-Content -Path $CmdLauncher -Value $CmdContent -Encoding ASCII
    $script:InstalledLauncher = $PsLauncher

    Write-Host "Installed SHAMSU launchers:"
    Write-Host "  $PsLauncher"
    Write-Host "  $CmdLauncher"

    $PathEntries = [Environment]::GetEnvironmentVariable("PATH", "Process") -split [IO.Path]::PathSeparator
    $OnPath = $PathEntries | Where-Object {
        $_ -and ([System.IO.Path]::GetFullPath($_) -ieq $ResolvedBinDir)
    }
    $script:LauncherOnPath = [bool]$OnPath

    if (-not $OnPath) {
        Write-Host ""
        Write-Host "Launcher directory is not on PATH for the current shell."
        Write-Host "Run directly with:"
        Write-Host "  & `"$PsLauncher`""
        Write-Host ""
        Write-Host "Or add this directory to PATH yourself if you want plain 'shamsu':"
        Write-Host "  $ResolvedBinDir"
    }
}

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
        Write-Host "Checking and pulling missing local models. This can take a long time for first install."
        & $VenvPython -m shamsu.runtime.ollama repair
    }
    elseif (-not $RuntimeStatus.ollama_path) {
        Write-Warning "Ollama is still missing. SHAMSU installed, but local inference needs `models repair` after Ollama is installed."
    }

    & $VenvPython -m shamsu.runtime.ollama write-config

    if (-not $SkipCommandInstall) {
        Install-ShamsuLauncher -BinDir $BinDir -RunScript $RunScript
    }

    Write-Host ""
    Write-Host "Install complete."
    Write-Host "SHAMSU did not edit your PowerShell profile, PATH, registry, or global Python."
    Write-Host "Run from any workspace with:"
    if ($SkipCommandInstall) {
        Write-Host "  & `"$RepoRoot\scripts\run-shamsu.ps1`""
    }
    elseif (-not $LauncherOnPath) {
        Write-Host "  & `"$InstalledLauncher`""
        Write-Host "Add $BinDir to PATH if you want plain 'shamsu' in new terminals."
    }
    else {
        Write-Host "  shamsu"
    }
}
finally {
    Pop-Location
}
