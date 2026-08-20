[CmdletBinding()]
param(
    [string]$BinDir = (Join-Path $HOME ".shamsu\bin"),
    [switch]$KeepVenv,
    [switch]$KeepLauncher,
    [switch]$KeepPath
)

# NOT "Stop". An uninstaller that aborts on the first problem is worse than one
# that reports it: whatever it had not reached yet stays installed, and the user
# is left half removed with a raw PowerShell error and no idea what remains.
#
# Proven 2026-08-20: a `path.json` missing one property (an older file, or a
# hand-edited one) made this script die inside the PATH step - after deleting a
# launcher, before removing the virtual environment or the runtime state. Both
# were left on disk and the script exited 1.
#
# So: every step runs, every failure is reported, and the exit code tells the
# truth at the end.
$ErrorActionPreference = "Continue"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $RepoRoot ".venv"
$RuntimeDir = Join-Path $RepoRoot ".shamsu"
$PsLauncher = Join-Path $BinDir "shamsu.ps1"
$CmdLauncher = Join-Path $BinDir "shamsu.cmd"
$BareLauncher = Join-Path $BinDir "shamsu"
$StateDir = Split-Path $BinDir -Parent
$PathManifest = Join-Path $StateDir "path.json"

$script:Failures = @()

function Invoke-Step {
    <#
    Run one removal. Report what happened; never let it end the uninstall.
    #>
    param(
        [string]$Describe,
        [scriptblock]$Action
    )
    try {
        & $Action
    }
    catch {
        $script:Failures += "$Describe : $($_.Exception.Message)"
        Write-Warning "Could not $Describe : $($_.Exception.Message)"
    }
}

function Remove-ItemSafely {
    param([string]$Path, [string]$Describe)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Invoke-Step -Describe $Describe -Action {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        Write-Host "Removed ${Describe}: $Path"
    }
}

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

function Get-JsonValue {
    <#
    Read a property off parsed JSON, or $null.

    StrictMode makes a missing property a terminating error, which is how a
    manifest one field short killed the whole uninstall.
    #>
    param($Object, [string]$Name)
    if ($null -eq $Object) {
        return $null
    }
    $Property = $Object.PSObject.Properties[$Name]
    if (-not $Property) {
        return $null
    }
    return $Property.Value
}

function Read-PathManifest {
    param([string]$ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json)
    }
    catch {
        Write-Warning "SHAMSU PATH manifest is unreadable; leaving user PATH unchanged."
        return $null
    }
}

function Remove-ShamsuUserPath {
    param([string]$ManifestPath)

    $Manifest = Read-PathManifest -ManifestPath $ManifestPath
    if ($null -eq $Manifest) {
        if (-not (Test-Path -LiteralPath $ManifestPath)) {
            Write-Host "No SHAMSU PATH manifest found; leaving user PATH unchanged."
        }
        return
    }
    if ((Get-JsonValue -Object $Manifest -Name "managed_by") -ne "SHAMSU" -or
        -not (Get-JsonValue -Object $Manifest -Name "added_by_shamsu")) {
        Write-Host "SHAMSU did not add the PATH entry; leaving user PATH unchanged."
        return
    }
    $Target = Normalize-PathEntry ([string](Get-JsonValue -Object $Manifest -Name "path_entry"))
    if (-not $Target) {
        Write-Host "The PATH manifest names no directory; leaving user PATH unchanged."
        return
    }
    $UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (-not $UserPath) {
        return
    }
    $Kept = @()
    $Removed = $false
    foreach ($entry in ($UserPath -split [IO.Path]::PathSeparator)) {
        $Normalized = Normalize-PathEntry $entry
        if (-not $Normalized) {
            continue
        }
        if ($Normalized -ieq $Target) {
            $Removed = $true
            continue
        }
        $Kept += $entry
    }
    if (-not $Removed) {
        Write-Host "SHAMSU launcher directory was not on user PATH; nothing to remove."
        return
    }
    [Environment]::SetEnvironmentVariable("PATH", ($Kept -join [IO.Path]::PathSeparator), "User")
    Write-Host "Removed SHAMSU launcher directory from user PATH:"
    Write-Host "  $Target"
}

Write-Host "SHAMSU uninstall"
Write-Host "Repo: $RepoRoot"

if (-not $KeepLauncher) {
    foreach ($launcher in @($PsLauncher, $CmdLauncher, $BareLauncher)) {
        Remove-ItemSafely -Path $launcher -Describe "launcher"
    }
}
else {
    Write-Host "Keeping user-local launchers."
}

if (-not $KeepPath) {
    Invoke-Step -Describe "update user PATH" -Action {
        Remove-ShamsuUserPath -ManifestPath $PathManifest
    }
}
else {
    Write-Host "Keeping SHAMSU user PATH entry."
}

# The manifest is SHAMSU's own bookkeeping, and leaving it behind is what makes
# the next install think it already owns a PATH entry it no longer has. Removed
# only once PATH has actually been dealt with, and only when the launchers went
# too - `-KeepLauncher` means the install is still in use.
if (-not $KeepPath -and -not $KeepLauncher) {
    Remove-ItemSafely -Path $PathManifest -Describe "PATH manifest"
    if ((Test-Path -LiteralPath $BinDir) -and
        -not (Get-ChildItem -LiteralPath $BinDir -Force -ErrorAction SilentlyContinue)) {
        Remove-ItemSafely -Path $BinDir -Describe "empty launcher directory"
    }
    if ((Test-Path -LiteralPath $StateDir) -and
        -not (Get-ChildItem -LiteralPath $StateDir -Force -ErrorAction SilentlyContinue)) {
        Remove-ItemSafely -Path $StateDir -Describe "empty SHAMSU state directory"
    }
}

if (-not $KeepVenv) {
    Remove-ItemSafely -Path $VenvDir -Describe "repo virtual environment"
}
else {
    Write-Host "Keeping repo virtual environment."
}

Remove-ItemSafely -Path $RuntimeDir -Describe "repo runtime state"

Invoke-Step -Describe "sweep nested workspace state" -Action {
    $NestedShamsuDirs = Get-ChildItem -Path $RepoRoot -Recurse -Directory -Filter ".shamsu" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\\.venv\\' -and $_.FullName -notmatch '\\\.git\\' }
    foreach ($dir in $NestedShamsuDirs) {
        Remove-ItemSafely -Path $dir.FullName -Describe "stray nested workspace state"
    }
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Warning "SHAMSU uninstall finished with $($script:Failures.Count) problem(s):"
    foreach ($failure in $script:Failures) {
        Write-Host "  - $failure"
    }
    Write-Host "Everything else was removed. Re-run this script after dealing with the above."
    exit 1
}

Write-Host "SHAMSU uninstall complete."
Write-Host "This removed SHAMSU-managed files from this repo, your user-local launcher directory, and SHAMSU-managed PATH entry."
Write-Host "It did not remove Ollama or workspace .shamsu folders from your other projects."
exit 0
