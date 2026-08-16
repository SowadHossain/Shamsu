[CmdletBinding()]
param(
    [string]$BinDir = (Join-Path $HOME ".shamsu\bin"),
    [switch]$KeepVenv,
    [switch]$KeepLauncher,
    [switch]$KeepPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$RuntimeDir = Join-Path $RepoRoot ".shamsu"
$PsLauncher = Join-Path $BinDir "shamsu.ps1"
$CmdLauncher = Join-Path $BinDir "shamsu.cmd"
$BareLauncher = Join-Path $BinDir "shamsu"
$PathManifest = Join-Path (Split-Path $BinDir -Parent) "path.json"

function Normalize-PathEntry {
    param([string]$PathEntry)
    if (-not $PathEntry) {
        return ""
    }
    $Expanded = [Environment]::ExpandEnvironmentVariables($PathEntry.Trim())
    try {
        return [System.IO.Path]::GetFullPath($Expanded).TrimEnd('\')
    }
    catch {
        return $Expanded.TrimEnd('\')
    }
}

function Remove-ShamsuUserPath {
    param(
        [string]$BinDir,
        [string]$ManifestPath
    )

    if (-not (Test-Path $ManifestPath)) {
        Write-Host "No SHAMSU PATH manifest found; leaving user PATH unchanged."
        return
    }
    $Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
    if ($Manifest.managed_by -ne "SHAMSU" -or -not $Manifest.added_by_shamsu) {
        Write-Host "SHAMSU did not add the PATH entry; leaving user PATH unchanged."
        return
    }
    $Target = Normalize-PathEntry ([string]$Manifest.path_entry)
    $UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    $Kept = @()
    foreach ($entry in ($UserPath -split [IO.Path]::PathSeparator)) {
        if ((Normalize-PathEntry $entry) -and (Normalize-PathEntry $entry) -ine $Target) {
            $Kept += $entry
        }
    }
    [Environment]::SetEnvironmentVariable("PATH", ($Kept -join [IO.Path]::PathSeparator), "User")
    Write-Host "Removed SHAMSU launcher directory from user PATH:"
    Write-Host "  $Target"
}

Write-Host "SHAMSU uninstall"
Write-Host "Repo: $RepoRoot"

if (-not $KeepLauncher) {
    foreach ($launcher in @($PsLauncher, $CmdLauncher, $BareLauncher)) {
        if (Test-Path $launcher) {
            Remove-Item -LiteralPath $launcher -Force
            Write-Host "Removed launcher: $launcher"
        }
    }
}
else {
    Write-Host "Keeping user-local launchers."
}

if (-not $KeepPath) {
    Remove-ShamsuUserPath -BinDir $BinDir -ManifestPath $PathManifest
}
else {
    Write-Host "Keeping SHAMSU user PATH entry."
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

$NestedShamsuDirs = Get-ChildItem -Path $RepoRoot -Recurse -Directory -Filter ".shamsu" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\\.git\\' }
foreach ($dir in $NestedShamsuDirs) {
    Remove-Item -LiteralPath $dir.FullName -Recurse -Force
    Write-Host "Removed stray nested workspace state: $($dir.FullName)"
}

Write-Host ""
Write-Host "SHAMSU uninstall complete."
Write-Host "This removed SHAMSU-managed files from this repo, your user-local launcher directory, and SHAMSU-managed PATH entry."
Write-Host "It did not remove Ollama or workspace .shamsu folders from your other projects."
