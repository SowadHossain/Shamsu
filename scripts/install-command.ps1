[CmdletBinding()]
param(
    [string]$BinDir = (Join-Path $HOME ".shamsu\bin"),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunScript = Join-Path $RepoRoot "scripts\run-shamsu.ps1"

if (-not (Test-Path $RunScript)) {
    Write-Error "Could not find SHAMSU run script: $RunScript"
}

$ResolvedBinDir = [System.IO.Path]::GetFullPath($BinDir)
New-Item -ItemType Directory -Force -Path $ResolvedBinDir | Out-Null

$PsLauncher = Join-Path $ResolvedBinDir "shamsu.ps1"
$CmdLauncher = Join-Path $ResolvedBinDir "shamsu.cmd"

if ((Test-Path $PsLauncher) -and -not $Force) {
    Write-Error "Launcher already exists: $PsLauncher. Re-run with -Force to overwrite."
}
if ((Test-Path $CmdLauncher) -and -not $Force) {
    Write-Error "Launcher already exists: $CmdLauncher. Re-run with -Force to overwrite."
}

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

Write-Host "Installed SHAMSU launchers:"
Write-Host "  $PsLauncher"
Write-Host "  $CmdLauncher"
Write-Host ""
Write-Host "SHAMSU did not edit your PowerShell profile, PATH, registry, or global Python."

$PathEntries = [Environment]::GetEnvironmentVariable("PATH", "Process") -split [IO.Path]::PathSeparator
$OnPath = $PathEntries | Where-Object {
    $_ -and ([System.IO.Path]::GetFullPath($_) -ieq $ResolvedBinDir)
}

if (-not $OnPath) {
    Write-Host ""
    Write-Host "This bin directory is not on PATH for the current shell."
    Write-Host "Run directly with:"
    Write-Host "  & `"$PsLauncher`""
    Write-Host ""
    Write-Host "Or add this directory to PATH yourself if you want plain 'shamsu':"
    Write-Host "  $ResolvedBinDir"
}
else {
    Write-Host ""
    Write-Host "Run SHAMSU from any project with:"
    Write-Host "  shamsu"
}
