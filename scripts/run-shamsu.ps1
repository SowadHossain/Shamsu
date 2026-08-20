[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Workspace = (Get-Location).Path,
    [Parameter(ValueFromPipeline = $true)]
    [string]$InputObject,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ShamsuArgs
)

begin {
    $ErrorActionPreference = "Stop"
    Set-StrictMode -Version Latest
    $env:PYTHONUTF8 = "1"

    $RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
    $VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $PipedInput = [System.Collections.Generic.List[string]]::new()
}

process {
    if ($null -ne $InputObject) {
        $PipedInput.Add($InputObject)
    }
}

end {
    # `python.exe` EXISTING is not the same as the environment working.
    #
    # Live 2026-08-20: `.venv\pyvenv.cfg` was gone - removed with a whole
    # alphabetical block of site-packages, the signature of an antivirus
    # quarantine - while `Scripts\python.exe` was still on disk. This check
    # passed, the launcher ran it anyway, and the user got
    # `failed to locate pyvenv.cfg` with no idea what to do. The one question
    # worth asking is whether the environment can import SHAMSU, so ask that.
    $VenvBroken = ""
    if (-not (Test-Path $VenvPython)) {
        $VenvBroken = "there is no .venv in $RepoRoot"
    }
    elseif (-not (Test-Path (Join-Path $RepoRoot ".venv\pyvenv.cfg"))) {
        $VenvBroken = "the .venv is missing pyvenv.cfg, so it is no longer a usable environment"
    }
    else {
        & $VenvPython -c "import shamsu" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $VenvBroken = "the .venv cannot import SHAMSU"
        }
    }
    if ($VenvBroken) {
        Write-Error @"
SHAMSU cannot start: $VenvBroken.

Repair it by running the installer again from the repo:
  powershell -NoProfile -ExecutionPolicy Bypass -File "$(Join-Path $RepoRoot 'scripts\install.ps1')"

It will rebuild the environment in place. If this keeps happening, check whether
antivirus is quarantining files under $RepoRoot\.venv.
"@
    }

    $ResolvedWorkspace = Resolve-Path $Workspace
    & $VenvPython -m shamsu.runtime.ollama status

    if ($PipedInput.Count -gt 0) {
        $PipedInput -join [Environment]::NewLine | & $VenvPython -m shamsu.cli.repl --workspace $ResolvedWorkspace @ShamsuArgs
    }
    else {
        & $VenvPython -m shamsu.cli.repl --workspace $ResolvedWorkspace @ShamsuArgs
    }
}
