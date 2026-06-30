[CmdletBinding()]
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
    if (-not (Test-Path $VenvPython)) {
        Write-Error "Local .venv not found. Run scripts\install.ps1 from the SHAMSU repo first."
    }

    $ResolvedWorkspace = Resolve-Path $Workspace

    if ($PipedInput.Count -gt 0) {
        $PipedInput -join [Environment]::NewLine | & $VenvPython -m shamsu.cli.repl --workspace $ResolvedWorkspace @ShamsuArgs
    }
    else {
        & $VenvPython -m shamsu.cli.repl --workspace $ResolvedWorkspace @ShamsuArgs
    }
}
