# Overnight eval run.
#
# Produces the thing this project has never had: a MACHINE-READABLE baseline.
# `BENCHMARK.md` is rendered text, so `python -m evals.diff` - the tool whose
# whole job is to decide improved/regressed/noise mechanically - has never had
# two files to compare. Every comparison so far has been by eye, which the
# benchmark's own reading instructions forbid.
#
# Why 15 samples and not 7: the 2026-08-22 run flagged 4 of 16 cases flaky,
# meaning they passed some attempts and failed others on identical code. Four
# noisy rows out of sixteen is not a baseline you can justify a change against.
# Samples are the only cure, and the full run is ~90 minutes at 7, so ~3.2
# hours at 15 - which is what a night is for.
#
# Records the exact tree state first. This repo is edited by more than one
# agent at a time; a result whose provenance is "some version of Tuesday" is
# not a baseline. If the working tree is dirty the log says exactly how.
#
#   pwsh -File scripts/overnight-eval.ps1
#   pwsh -File scripts/overnight-eval.ps1 -Samples 7 -Label quick

param(
    [int]$Samples = 15,
    [string]$Label = "baseline",
    [double]$ProgressInterval = 60.0
)

$ErrorActionPreference = "Stop"

# Tee-Object has no -Encoding in Windows PowerShell 5.1 and defaults to UTF-16,
# so appending through it after a UTF-8 header produces a file that is half one
# encoding and half the other - unreadable by every tool that opens it later,
# including `git diff` and the morning's own eyes. Write UTF-8 explicitly.
function Write-Log {
    param([Parameter(ValueFromPipeline = $true)][string]$Line)
    process {
        Write-Host $Line
        Add-Content -Path $script:log -Value $Line -Encoding utf8
    }
}

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp   = Get-Date -Format "yyyy-MM-dd-HHmm"
$outDir  = Join-Path $repo "logs\evals"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$slug    = "$stamp-$Label-s$Samples"
$log     = Join-Path $outDir "$slug.log"
$json    = Join-Path $outDir "$slug.json"
$report  = Join-Path $outDir "$slug.md"
$art     = Join-Path $repo ".shamsu\eval-artifacts\$slug"

$python  = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# --- provenance -----------------------------------------------------------
# Written BEFORE the run, so a crash still leaves a record of what was being
# measured.
$head    = (& git rev-parse --short HEAD).Trim()
$branch  = (& git rev-parse --abbrev-ref HEAD).Trim()
$dirty   = (& git status --porcelain) -join "`n"

$header = @"
SHAMSU overnight eval
=====================
started      : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
branch       : $branch
HEAD         : $head
samples      : $Samples
report (md)  : $report
report (json): $json
artifacts    : $art

working tree at start:
$(if ($dirty) { $dirty } else { "  (clean)" })

NOTE: a dirty tree means this run measured uncommitted work. If another agent
edits this checkout mid-run, later cases see different code from earlier ones
and the result is not a baseline. Check `git status` against the list above
before trusting a comparison.
"@

$header | Out-File -FilePath $log -Encoding utf8
Write-Host $header

# --- fail fast, not at hour three ----------------------------------------
try {
    $tags = Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5
} catch {
    "ABORT: Ollama is not answering on 11434. Start it, then re-run." | Write-Log
    exit 1
}

$model = (& $python -c "import sys; sys.path.insert(0,'.'); from shamsu.runtime.models import initialize_model_tier, model_for_role; from pathlib import Path; initialize_model_tier(Path('.').resolve()); print(model_for_role('agent-chat'))").Trim()
if (-not ($tags.models.name -contains $model)) {
    "ABORT: the agent-chat model '$model' is not pulled. Run: ollama pull $model" | Write-Log
    exit 1
}

# The two server flags 32k depends on. Without them a 32k request spills to the
# CPU - measured 47.5s against 7.5s on the same card - which turns a 3-hour run
# into an overnight that is still going at lunchtime.
foreach ($pair in @(@("OLLAMA_FLASH_ATTENTION","1"), @("OLLAMA_KV_CACHE_TYPE","q8_0"))) {
    $name = $pair[0]; $want = $pair[1]
    $have = [Environment]::GetEnvironmentVariable($name, "User")
    if (-not $have) { $have = [Environment]::GetEnvironmentVariable($name, "Machine") }
    if ($have -ne $want) {
        "WARNING: $name is '$have', expected '$want'. The run will be far slower." | Write-Log
    }
}

"model        : $model" | Write-Log
"" | Write-Log

# --- run ------------------------------------------------------------------
$started = Get-Date
# Redirection done by cmd, NOT by PowerShell. In Windows PowerShell 5.1 a
# native command's stderr passed through `2>&1` is wrapped in an ErrorRecord
# per line (NativeCommandError) and sets `$?` false even on exit 0 - and the
# eval writes all of its progress to stderr, so the whole log would arrive as
# a wall of red errors and the exit code could not be trusted. cmd concatenates
# the two streams before PowerShell ever sees them.
$argline = "-m evals --samples $Samples --json-out `"$json`" --out `"$report`" --artifacts-dir `"$art`" --progress-interval $ProgressInterval"
& cmd /c "`"$python`" $argline 2>&1" | Write-Log
$code = $LASTEXITCODE
$elapsed = (Get-Date) - $started

$footer = @"

finished     : $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
elapsed      : $([math]::Round($elapsed.TotalMinutes,1)) min
exit code    : $code   (0 = every case passed; 1 = at least one did not)

working tree at END:
$(if ((& git status --porcelain) -join "`n") { (& git status --porcelain) -join "`n" } else { "  (clean)" })
HEAD at END  : $((& git rev-parse --short HEAD).Trim())

In the morning:
  compare mechanically, never by eye -
    $python -m evals.diff <older>.json $json
  exit 0 improved / 1 regressed / 2 noise.
  This is the first JSON baseline in the repo, so the first comparison it can
  serve is the NEXT run's.
"@
$footer -split "`r?`n" | Write-Log
exit $code
