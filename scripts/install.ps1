param(
    [string]$Python = "python",
    [switch]$Yes,
    [switch]$SkipOllamaInstall,
    [switch]$SkipCodebaseMemoryInstall,
    [switch]$SkipModels,
    [switch]$PrefetchModels,
    [switch]$SkipCommandInstall,
    [switch]$SkipPathUpdate,
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

function Test-PathContainsEntry {
    param(
        [string]$PathValue,
        [string]$PathEntry
    )
    $Needle = Normalize-PathEntry $PathEntry
    foreach ($entry in ($PathValue -split [IO.Path]::PathSeparator)) {
        if ((Normalize-PathEntry $entry) -ieq $Needle) {
            return $true
        }
    }
    return $false
}

function Send-EnvironmentChangeNotice {
    $TypeName = "Shamsu.NativeMethods"
    if (-not ($TypeName -as [type])) {
        Add-Type -Namespace Shamsu -Name NativeMethods -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError=true, CharSet=System.Runtime.InteropServices.CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(
    System.IntPtr hWnd,
    uint Msg,
    System.IntPtr wParam,
    string lParam,
    uint fuFlags,
    uint uTimeout,
    out System.IntPtr lpdwResult);
"@
    }
    $HWND_BROADCAST = [IntPtr]0xffff
    $WM_SETTINGCHANGE = 0x001A
    $SMTO_ABORTIFHUNG = 0x0002
    $Result = [IntPtr]::Zero
    [Shamsu.NativeMethods]::SendMessageTimeout(
        $HWND_BROADCAST,
        $WM_SETTINGCHANGE,
        [IntPtr]::Zero,
        "Environment",
        $SMTO_ABORTIFHUNG,
        5000,
        [ref]$Result
    ) | Out-Null
}

function Add-ProcessPathEntry {
    param([string]$PathEntry)
    if (-not (Test-PathContainsEntry -PathValue $env:PATH -PathEntry $PathEntry)) {
        $env:PATH = "$PathEntry$([IO.Path]::PathSeparator)$env:PATH"
    }
}

function Add-ShamsuUserPath {
    param([string]$BinDir)

    $ResolvedBinDir = Normalize-PathEntry $BinDir
    $StateDir = Split-Path $ResolvedBinDir -Parent
    $ManifestPath = Join-Path $StateDir "path.json"
    New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

    $UserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if (Test-PathContainsEntry -PathValue $UserPath -PathEntry $ResolvedBinDir) {
        $AddedByShamsu = $false
        $ShamsuLaunchersExist = (Test-Path (Join-Path $ResolvedBinDir "shamsu.ps1")) -and (Test-Path (Join-Path $ResolvedBinDir "shamsu.cmd"))
        if (Test-Path $ManifestPath) {
            try {
                $ExistingManifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
                $AddedByShamsu = [bool]$ExistingManifest.added_by_shamsu -or $ShamsuLaunchersExist
            }
            catch {
                $AddedByShamsu = $ShamsuLaunchersExist
            }
        }
        else {
            $AddedByShamsu = $ShamsuLaunchersExist
        }
        $Manifest = @{
            managed_by = "SHAMSU"
            path_entry = $ResolvedBinDir
            added_by_shamsu = $AddedByShamsu
        }
        Set-Content -Path $ManifestPath -Value ($Manifest | ConvertTo-Json -Depth 4) -Encoding UTF8
        Add-ProcessPathEntry -PathEntry $ResolvedBinDir
        Send-EnvironmentChangeNotice
        Write-Host "SHAMSU launcher directory is already present in user PATH."
        return
    }

    $NewPath = if ([string]::IsNullOrWhiteSpace($UserPath)) {
        $ResolvedBinDir
    }
    else {
        "$ResolvedBinDir$([IO.Path]::PathSeparator)$UserPath"
    }
    [Environment]::SetEnvironmentVariable("PATH", $NewPath, "User")
    Add-ProcessPathEntry -PathEntry $ResolvedBinDir
    Send-EnvironmentChangeNotice

    $Manifest = @{
        managed_by = "SHAMSU"
        path_entry = $ResolvedBinDir
        added_by_shamsu = $true
    }
    Set-Content -Path $ManifestPath -Value ($Manifest | ConvertTo-Json -Depth 4) -Encoding UTF8
    Write-Host "Added SHAMSU launcher directory to user PATH:"
    Write-Host "  $ResolvedBinDir"
    Write-Host "Open a new terminal for Windows to refresh PATH everywhere."
}

function Install-ShamsuLauncher {
    param(
        [string]$BinDir,
        [string]$RunScript,
        [bool]$WillUpdatePath = $false
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
`$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
`$RunScript = '$EscapedRunScript'
`$Workspace = (Get-Location).Path
`$ShamsuArgs = `$args
`$PipedInput = @(`$input)

if (`$PipedInput.Count -gt 0) {
    & `$RunScript -Workspace `$Workspace -InputObject (`$PipedInput -join [Environment]::NewLine) @ShamsuArgs
}
else {
    & `$RunScript -Workspace `$Workspace @ShamsuArgs
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

    $PathForCheck = $env:PATH
    if ($WillUpdatePath) {
        $PathForCheck = "$PathForCheck$([IO.Path]::PathSeparator)$([Environment]::GetEnvironmentVariable("PATH", "User"))"
    }
    $OnPath = Test-PathContainsEntry -PathValue $PathForCheck -PathEntry $ResolvedBinDir
    $script:LauncherOnPath = $OnPath

    if (-not $OnPath -and $WillUpdatePath) {
        Write-Host ""
        Write-Host "Launcher directory will be added to your user PATH by this install:"
        Write-Host "  $ResolvedBinDir"
    }
    elseif (-not $OnPath) {
        Write-Host ""
        Write-Host "Launcher directory is not on PATH for the current shell."
        Write-Host "Run directly with:"
        Write-Host "  & `"$PsLauncher`""
        Write-Host ""
        Write-Host "Or add this directory to PATH yourself if you want plain 'shamsu':"
        Write-Host "  $ResolvedBinDir"
    }
    else {
        $ResolvedPsLauncher = [System.IO.Path]::GetFullPath($PsLauncher)
        $ResolvedCmdLauncher = [System.IO.Path]::GetFullPath($CmdLauncher)
        $ExistingCommand = Get-Command shamsu -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ExistingCommand) {
            $ResolvedExisting = [System.IO.Path]::GetFullPath($ExistingCommand.Source)
        }
        if ($ExistingCommand -and $ResolvedExisting -ine $ResolvedPsLauncher -and $ResolvedExisting -ine $ResolvedCmdLauncher) {
            Write-Host ""
            Write-Warning "Plain 'shamsu' currently resolves to a different command:"
            Write-Host "  $($ExistingCommand.Source)"
            Write-Host "Run this launcher directly, or move $ResolvedBinDir earlier in PATH:"
            Write-Host "  & `"$PsLauncher`""
        }
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

    $PlaywrightMarker = Join-Path $VenvDir ".shamsu-playwright-chromium-ok"
    if (Test-Path $PlaywrightMarker) {
        Write-Host "Playwright Chromium already installed (skipping browser download check)."
    }
    else {
        try {
            & $VenvPython -m playwright install chromium
            if ($LASTEXITCODE -ne 0) {
                throw "playwright install chromium exited with code $LASTEXITCODE"
            }
            New-Item -ItemType File -Force -Path $PlaywrightMarker | Out-Null
        }
        catch {
            Write-Warning "Playwright Chromium install failed or was skipped: $_"
            Write-Warning "Browser-based debugging (/browse commands) may not work until this succeeds. Rerun this script to retry."
        }
    }

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
                try {
                    winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
                    if ($LASTEXITCODE -ne 0) {
                        throw "winget install exited with code $LASTEXITCODE"
                    }
                }
                catch {
                    Write-Warning "Ollama install through winget failed: $_"
                    Write-Warning "Install Ollama manually from https://ollama.com/download, then run ``models repair``."
                }
            }
            else {
                Write-Warning "winget was not found. Install Ollama from https://ollama.com/download, then rerun this script."
            }
        }
    }

    $RuntimeStatusJson = & $VenvPython -m shamsu.runtime.ollama status --json
    $RuntimeStatus = $RuntimeStatusJson | ConvertFrom-Json

    if ($PrefetchModels -and -not $SkipModels -and $RuntimeStatus.ollama_path) {
        Write-Host "Checking and pulling all required local models now. This can take a long time."
        & $VenvPython -m shamsu.runtime.ollama repair
    }
    elseif (-not $RuntimeStatus.ollama_path) {
        Write-Warning "Ollama is still missing. SHAMSU installed, but local inference needs `models repair` after Ollama is installed."
    }
    else {
        Write-Host "Skipping model downloads here. The first time you run 'shamsu' in a workspace it will"
        Write-Host "ask which model tier to use (light/default/heavy) and download that tier's models then."
        Write-Host "Pass -PrefetchModels to this script to download the default tier's models now instead."
    }

    & $VenvPython -m shamsu.runtime.ollama write-config

    if (-not $SkipCodebaseMemoryInstall) {
        $AbstractStatusJson = & $VenvPython -m shamsu.abstract.cli status --workspace $RepoRoot --json
        $AbstractStatus = $AbstractStatusJson | ConvertFrom-Json
        if (-not $AbstractStatus.health.available) {
            $InstallCodebaseMemory = $Yes
            if (-not $InstallCodebaseMemory) {
                $Answer = Read-Host "Install required local Codebase-Memory MCP tool? [y/N]"
                $InstallCodebaseMemory = $Answer.ToLowerInvariant() -in @("y", "yes")
            }
            if ($InstallCodebaseMemory) {
                & $VenvPython -m shamsu.abstract.cli setup --workspace $RepoRoot
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "Codebase-Memory MCP setup failed. Run '/abstract setup' or 'shamsu doctor' later to retry."
                }
            }
            else {
                Write-Warning "Skipping Codebase-Memory MCP install. SHAMSU codebase mode will not run normal code-agent workflows until '/abstract setup' completes."
            }
        }
    }

    if (-not $SkipCommandInstall) {
        Install-ShamsuLauncher -BinDir $BinDir -RunScript $RunScript -WillUpdatePath (-not $SkipPathUpdate)
        if (-not $SkipPathUpdate) {
            Add-ShamsuUserPath -BinDir $BinDir
            $LauncherOnPath = $true
        }
    }

    Write-Host ""
    Write-Host "Install complete."
    Write-Host "SHAMSU did not edit your PowerShell profile, registry, or global Python."
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
