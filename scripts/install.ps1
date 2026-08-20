param(
    [string]$Python = "python",
    [switch]$Yes,
    [switch]$SkipOllamaInstall,
    [switch]$SkipCodebaseMemoryInstall,
    [switch]$SkipGraphitiInstall,
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

function Read-ShamsuJson {
    <#
    Run a SHAMSU status command and return the parsed JSON, or $null.

    Every caller of these status commands used to be a bare
    `& $VenvPython ... | ConvertFrom-Json`. With $ErrorActionPreference = "Stop"
    that turns any failure into an aborted install: a partly-installed venv, a
    missing optional dependency, or a traceback on stderr leaves nothing on
    stdout, ConvertFrom-Json throws on the empty string, and the script dies
    AFTER pip install has run but BEFORE the launcher is written. The user is
    left with a half install and a raw PowerShell error.

    A status check that cannot answer is not a reason to stop installing. It is
    a reason to say so and carry on.
    #>
    param(
        [string]$PythonExe,
        [string[]]$Arguments
    )
    try {
        $Raw = & $PythonExe @Arguments 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $Raw) {
            return $null
        }
        return ($Raw | Out-String | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Get-JsonValue {
    <#
    Read a dotted property path off a parsed JSON object, or $null.

    `Set-StrictMode -Version Latest` makes a missing property a terminating
    error, so `$Status.ollama_path` is only safe when the key is guaranteed -
    and the key is only guaranteed while the two sides agree. This keeps a
    renamed or absent field from ending an install.
    #>
    param($Object, [string]$Path)
    $Current = $Object
    foreach ($part in $Path.Split(".")) {
        if ($null -eq $Current) {
            return $null
        }
        $Property = $Current.PSObject.Properties[$part]
        if (-not $Property) {
            return $null
        }
        $Current = $Property.Value
    }
    return $Current
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
    $BareLauncher = Join-Path $ResolvedBinDir "shamsu"
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
    $BashRunScript = $RunScript.Replace("\", "/")
    $BareContent = @"
#!/usr/bin/env bash
set -euo pipefail

if command -v cygpath >/dev/null 2>&1; then
  WORKSPACE="`$(cygpath -aw "`$PWD")"
else
  WORKSPACE="`$PWD"
fi

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$BashRunScript" -Workspace "`$WORKSPACE" "`$@"
"@

    Set-Content -Path $PsLauncher -Value $PsContent -Encoding UTF8
    Set-Content -Path $CmdLauncher -Value $CmdContent -Encoding ASCII
    Set-Content -Path $BareLauncher -Value $BareContent -Encoding UTF8
    $script:InstalledLauncher = $PsLauncher

    Write-Host "Installed SHAMSU launchers:"
    Write-Host "  $PsLauncher"
    Write-Host "  $CmdLauncher"
    Write-Host "  $BareLauncher"

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
        $ResolvedBareLauncher = [System.IO.Path]::GetFullPath($BareLauncher)
        $ExistingCommand = Get-Command shamsu -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ExistingCommand) {
            $ResolvedExisting = [System.IO.Path]::GetFullPath($ExistingCommand.Source)
        }
        if ($ExistingCommand -and $ResolvedExisting -ine $ResolvedPsLauncher -and $ResolvedExisting -ine $ResolvedCmdLauncher -and $ResolvedExisting -ine $ResolvedBareLauncher) {
            Write-Host ""
            Write-Warning "Plain 'shamsu' currently resolves to a different command:"
            Write-Host "  $($ExistingCommand.Source)"
            Write-Host "Run this launcher directly, or move $ResolvedBinDir earlier in PATH:"
            Write-Host "  & `"$PsLauncher`""
        }
    }
}

function Test-VenvUsable {
    <#
    Can this environment actually run SHAMSU? Returns the reason it cannot, or "".

    `Scripts\python.exe` existing is not the same as the environment working.
    Live 2026-08-20 `pyvenv.cfg` was gone - removed along with a whole
    alphabetical block of site-packages, the signature of an antivirus
    quarantine - while the interpreter was still on disk. Everything downstream
    then failed with `failed to locate pyvenv.cfg`, and the installer happily
    reused the corpse because it only ever checked for the file.
    #>
    param([string]$VenvDir)

    $Exe = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $Exe)) {
        return "no interpreter at $Exe"
    }
    if (-not (Test-Path (Join-Path $VenvDir "pyvenv.cfg"))) {
        return "pyvenv.cfg is missing, so this is no longer a virtual environment"
    }
    & $Exe -c "import sys" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        return "the interpreter will not start"
    }
    return ""
}

function Initialize-Pip {
    <#
    Make sure pip is there before anything tries to use it.

    A venv can lose pip on its own - `ensurepip` is exactly the supported repair
    and costs nothing when pip is already present. Without this the installer
    fails at its very first real step with `No module named pip`, which tells
    the user nothing about what to do.
    #>
    param([string]$PythonExe)

    & $PythonExe -m pip --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return $true
    }
    Write-Host "pip is missing from the environment; restoring it with ensurepip."
    & $PythonExe -m ensurepip --upgrade 2>&1 | Out-Null
    & $PythonExe -m pip --version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-Prerequisites {
    <#
    Say what is missing BEFORE spending ten minutes installing, and say what
    each thing costs the user if it stays missing.

    Only two things here are fatal: a Python new enough to run the package, and
    the repo itself. Everything else degrades a feature rather than the install,
    so it is reported and carried past - a machine without node can still edit
    Python, and telling someone their install failed because of a JavaScript
    syntax checker would be false.
    #>
    param([string]$PythonExe)

    Write-Host ""
    Write-Host "Checking prerequisites..."

    $Version = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $Version) {
        Write-Error "Could not run '$PythonExe'. Install Python 3.11 or newer, or pass -Python <path>."
    }
    $Parts = $Version.Trim().Split(".")
    if ([int]$Parts[0] -lt 3 -or ([int]$Parts[0] -eq 3 -and [int]$Parts[1] -lt 11)) {
        Write-Error "SHAMSU needs Python 3.11 or newer; '$PythonExe' is $Version."
    }
    Write-Host "  python $Version - ok"

    foreach ($tool in @(
        @{ Name = "node";   Why = "JavaScript syntax checking falls back to a bracket scan"; Url = "https://nodejs.org/en/download" },
        @{ Name = "git";    Why = "diff review and history tools are unavailable";           Url = "https://git-scm.com/downloads" },
        @{ Name = "ollama"; Why = "there is no local model to run";                          Url = "https://ollama.com/download" }
    )) {
        if (Get-Command $tool.Name -ErrorAction SilentlyContinue) {
            Write-Host "  $($tool.Name) - ok"
        }
        else {
            Write-Warning "  $($tool.Name) is not installed - $($tool.Why)."
            Write-Host "    Install it from $($tool.Url)"
        }
    }
    Write-Host ""
}

Write-Host "SHAMSU installer"
Write-Host "Repo: $RepoRoot"

Test-Prerequisites -PythonExe $Python

Push-Location $RepoRoot
try {
    $VenvProblem = Test-VenvUsable -VenvDir $VenvDir
    if ($VenvProblem -and (Test-Path $VenvDir)) {
        Write-Warning "The existing .venv is unusable: $VenvProblem."
        Write-Host "Rebuilding it from scratch."
        # A half-deleted environment cannot be repaired in place: pip would
        # reinstall the packages it can see records for and skip the ones whose
        # metadata went with them.
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue

        # Windows will not delete a DLL that a running process has loaded, and
        # `-ErrorAction SilentlyContinue` hides that completely. Live 2026-08-20
        # this printed "Rebuilding it from scratch." and then, one line later,
        # "Using existing virtual environment" - because six stale SHAMSU
        # processes held python.exe and eight .pyd files open. The rebuild was a
        # no-op and the install failed further down for a reason that had
        # nothing to do with the real cause.
        #
        # So: check, and name the processes that are in the way. Anyone can act
        # on "close PID 54328"; nobody can act on "failed to locate pyvenv.cfg".
        if (Test-Path $VenvDir) {
            $Stuck = @(Get-ChildItem -LiteralPath $VenvDir -Recurse -File -Force -ErrorAction SilentlyContinue)
            if ($Stuck.Count -gt 0) {
                Write-Warning "Could not fully remove $VenvDir - $($Stuck.Count) file(s) are locked by a running process:"
                foreach ($file in $Stuck | Select-Object -First 5) {
                    Write-Host "  $($file.FullName)"
                }
                $Holders = @(
                    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
                        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$VenvDir*" }
                )
                if ($Holders.Count -gt 0) {
                    Write-Host ""
                    Write-Host "These processes are using this environment:"
                    foreach ($holder in $Holders) {
                        Write-Host "  PID $($holder.ProcessId)  $($holder.CommandLine)"
                    }
                    Write-Host ""
                    Write-Host "Stop them and run this installer again:"
                    Write-Host "  Stop-Process -Id $($Holders.ProcessId -join ', ')"
                }
                Write-Error "Cannot rebuild the virtual environment while it is in use."
            }
        }
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Host "Creating local virtual environment: $VenvDir"
        & $Python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Could not create the virtual environment at $VenvDir."
        }
    }
    else {
        Write-Host "Using existing virtual environment: $VenvDir"
    }

    if (-not (Initialize-Pip -PythonExe $VenvPython)) {
        Write-Error "pip is missing from $VenvDir and ensurepip could not restore it."
    }

    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Installing SHAMSU and its dependencies failed. Nothing above this point is at fault; re-run once the error above is dealt with."
    }

    # Prove it. An installer that reports success without ever importing what it
    # installed is how a half-quarantined environment kept passing for working.
    & $VenvPython -c "import shamsu" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SHAMSU installed but cannot be imported from $VenvDir. Check whether antivirus is quarantining files under it."
    }
    Write-Host "SHAMSU imports cleanly from the virtual environment."

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

    $RuntimeStatus = Read-ShamsuJson -PythonExe $VenvPython -Arguments @("-m", "shamsu.runtime.ollama", "status", "--json")
    $OllamaPath = Get-JsonValue -Object $RuntimeStatus -Path "ollama_path"
    if ($null -eq $RuntimeStatus) {
        Write-Warning "Could not read the Ollama status. Continuing; run 'shamsu doctor' afterwards."
    }

    if (-not $OllamaPath -and -not $SkipOllamaInstall) {
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

    $RuntimeStatus = Read-ShamsuJson -PythonExe $VenvPython -Arguments @("-m", "shamsu.runtime.ollama", "status", "--json")
    $OllamaPath = Get-JsonValue -Object $RuntimeStatus -Path "ollama_path"

    if ($PrefetchModels -and -not $SkipModels -and $OllamaPath) {
        Write-Host "Checking and pulling all required local models now. This can take a long time."
        try {
            & $VenvPython -m shamsu.runtime.ollama repair
        }
        catch {
            Write-Warning "Model prefetch failed: $_"
            Write-Warning "Run 'shamsu' and it will pull what it needs."
        }
    }
    elseif (-not $OllamaPath) {
        Write-Warning "Ollama is still missing. SHAMSU installed, but local inference needs `models repair` after Ollama is installed."
    }
    else {
        Write-Host "Skipping model downloads here. The first time you run 'shamsu' in a workspace it will"
        Write-Host "ask which model tier to use (light/default/heavy) and download that tier's models then."
        Write-Host "Pass -PrefetchModels to this script to download the default tier's models now instead."
    }

    try {
        & $VenvPython -m shamsu.runtime.ollama write-config
    }
    catch {
        # Reached AFTER the package is installed and BEFORE the launcher is
        # written. The config is a convenience; the install is not.
        Write-Warning "Could not write the Ollama config: $_"
        Write-Warning "'shamsu doctor' can retry it."
    }

    if (-not $SkipCodebaseMemoryInstall) {
        $AbstractStatus = Read-ShamsuJson -PythonExe $VenvPython -Arguments @("-m", "shamsu.abstract.cli", "status", "--workspace", $RepoRoot, "--json")
        if (-not (Get-JsonValue -Object $AbstractStatus -Path "health.available")) {
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

    if (-not $SkipGraphitiInstall) {
        $MemoryStatus = Read-ShamsuJson -PythonExe $VenvPython -Arguments @("-m", "shamsu.memory.cli", "status", "--workspace", $RepoRoot, "--json")
        if (-not (Get-JsonValue -Object $MemoryStatus -Path "health.available")) {
            $InstallGraphiti = $Yes
            if (-not $InstallGraphiti) {
                $Answer = Read-Host "Install required local Graphiti memory tool? [y/N]"
                $InstallGraphiti = $Answer.ToLowerInvariant() -in @("y", "yes")
            }
            if ($InstallGraphiti) {
                & $VenvPython -m shamsu.memory.cli setup --workspace $RepoRoot
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "Graphiti setup failed. Run '/memory setup' or 'shamsu doctor' later to retry."
                }
            }
            else {
                Write-Warning "Skipping Graphiti install. SHAMSU normal agent mode will not run until '/memory setup' completes."
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

