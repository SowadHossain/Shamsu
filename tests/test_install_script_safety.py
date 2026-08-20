from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _code_only(text: str, comment: str = "#") -> str:
    """The script with its comment lines removed.

    Several of these tests assert that a broken pattern is ABSENT, and the fix
    for each of those patterns left a comment explaining what it used to be and
    why it was wrong. That comment is the most useful line in the file and it
    must not fail the test that guards it.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(comment)
    )


def test_install_scripts_do_not_edit_shell_profiles_registry_or_global_python():
    scripts = [
        REPO_ROOT / "scripts" / "install.ps1",
        REPO_ROOT / "scripts" / "install.sh",
        REPO_ROOT / "scripts" / "run-shamsu.ps1",
        REPO_ROOT / "scripts" / "run-shamsu.sh",
        REPO_ROOT / "scripts" / "uninstall.ps1",
        REPO_ROOT / "scripts" / "uninstall.sh",
    ]
    forbidden = [
        "$PROFILE",
        "setx ",
        "reg add",
        ">> ~/.bashrc",
        ">> ~/.zshrc",
        ">> ~/.profile",
        "pip install -g",
        "export PATH=",
    ]

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in text


def test_install_scripts_expose_safe_runtime_flags():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "$Yes" in ps1
    assert "$SkipOllamaInstall" in ps1
    assert "$SkipModels" in ps1
    assert "$PrefetchModels" in ps1
    assert "$SkipCommandInstall" in ps1
    assert "$SkipPathUpdate" in ps1
    assert "$BinDir" in ps1
    assert "$ModelsPath" in ps1
    assert "--yes" in sh
    assert "--skip-ollama-install" in sh
    assert "--skip-models" in sh
    assert "--prefetch-models" in sh
    assert "--skip-command-install" in sh
    assert "--bin-dir" in sh
    assert "--models-path" in sh


def test_install_scripts_default_to_lazy_model_downloads():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "if ($PrefetchModels -and -not $SkipModels -and $OllamaPath)" in ps1
    assert "ask which model tier to use" in ps1

    assert 'if [[ "${PREFETCH_MODELS}" -eq 1 && "${SKIP_MODELS}" -eq 0 ]]' in sh
    assert "ask which model tier to use" in sh


def test_install_scripts_install_playwright_browser_support():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "-m playwright install chromium" in ps1
    assert '-m playwright install chromium' in sh


def test_windows_runtime_scripts_force_python_utf8_for_ollama_output():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    run_ps1 = (REPO_ROOT / "scripts" / "run-shamsu.ps1").read_text(encoding="utf-8")
    install_sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    run_sh = (REPO_ROOT / "scripts" / "run-shamsu.sh").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in install_ps1
    assert '$env:PYTHONUTF8 = "1"' in run_ps1
    assert 'export PYTHONUTF8="${PYTHONUTF8:-1}"' in install_sh
    assert 'export PYTHONUTF8="${PYTHONUTF8:-1}"' in run_sh


def test_install_scripts_create_thin_launchers_without_profile_edits():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "scripts\\run-shamsu.ps1" in ps1
    assert "shamsu.ps1" in ps1
    assert "shamsu.cmd" in ps1
    assert '$BareLauncher = Join-Path $ResolvedBinDir "shamsu"' in ps1
    assert "@ShamsuArgs" in ps1
    assert '$ShamsuArgs = `$args' in ps1
    assert "`$PipedInput = @(`$input)" in ps1
    assert "(Get-Location).Path" in ps1
    assert "-InputObject (`$PipedInput -join [Environment]::NewLine)" in ps1
    assert "-Workspace `$Workspace @ShamsuArgs" in ps1
    assert '-Workspace "%CD%" %*' in ps1
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass" in ps1
    assert "$LauncherOnPath" in ps1
    assert "Plain 'shamsu' currently resolves to a different command" in ps1
    assert "Add $BinDir to PATH if you want plain 'shamsu'" in ps1
    assert "Add-ShamsuUserPath" in ps1
    assert "Send-EnvironmentChangeNotice" in ps1
    assert "path.json" in ps1
    assert "did not edit your PowerShell profile, registry, or global Python" in ps1

    assert "scripts/run-shamsu.sh" in sh
    assert 'exec "${RUN_SCRIPT}" "\\$@"' in sh
    assert "${HOME}/.local/bin" in sh
    assert "LAUNCHER_ON_PATH" in sh
    assert "plain 'shamsu' currently resolves to a different command" in sh
    assert "Add ${BIN_DIR} to PATH if you want plain 'shamsu'" in sh
    assert "did not edit your shell profile, PATH, global Python, or system registry" in sh


def test_install_scripts_skip_playwright_reinstall_when_marker_present():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert ".shamsu-playwright-chromium-ok" in ps1
    assert ".shamsu-playwright-chromium-ok" in sh
    assert "already installed (skipping browser download check)" in ps1
    assert "already installed (skipping browser download check)" in sh


def test_install_scripts_do_not_abort_on_flaky_playwright_or_ollama_install():
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "try {" in ps1
    assert "Playwright Chromium install failed or was skipped" in ps1
    assert "Ollama install through winget failed" in ps1

    assert "Playwright Chromium install failed or was skipped" in sh
    assert "Ollama install script failed" in sh
    assert "'brew install ollama' failed" in sh


def test_doctor_scripts_exist_and_invoke_runtime_doctor():
    ps1 = (REPO_ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "doctor.sh").read_text(encoding="utf-8")

    assert "shamsu.runtime.doctor" in ps1
    assert "shamsu.runtime.doctor" in sh
    assert "--workspace" in ps1
    assert "--workspace" in sh


def test_uninstall_scripts_remove_only_shamsu_managed_files():
    ps1 = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")

    assert ".venv" in ps1
    assert ".shamsu" in ps1
    assert "shamsu.ps1" in ps1
    assert "shamsu.cmd" in ps1
    assert '$BareLauncher = Join-Path $BinDir "shamsu"' in ps1
    assert "Remove-ShamsuUserPath" in ps1
    assert "added_by_shamsu" in ps1
    assert "path.json" in ps1
    assert "did not remove Ollama" in ps1
    assert "$PROFILE" not in ps1

    assert ".venv" in sh
    assert ".shamsu" in sh
    assert "/shamsu" in sh
    assert "did not remove Ollama" in sh
    assert "export PATH=" not in sh


def test_uninstall_scripts_clean_up_stray_nested_shamsu_folders():
    ps1 = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")

    assert "-Recurse -Directory -Filter \".shamsu\"" in ps1
    assert "stray nested workspace state" in ps1
    assert '-notmatch \'\\\\\\.venv\\\\\'' in ps1

    assert 'find "${REPO_ROOT}" -type d -name ".shamsu"' in sh
    assert "stray nested workspace state" in sh
    assert "-not -path \"*/.venv/*\"" in sh


# --- proven defects, 2026-08-20 ---------------------------------------------


def test_install_sh_reads_the_ollama_status_instead_of_grepping_for_a_semicolon():
    """`grep -q '"ollama_path": "";'` can never match - no JSON contains `";`.

    Every test built on it therefore read the wrong way round: the "install
    Ollama now?" prompt was unreachable, the "Ollama is still missing" warning
    never printed, and --prefetch-models ran `models repair` against an Ollama
    that was not there. install.ps1 always parsed the JSON properly.
    """
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert '"";' not in _code_only(sh), "the pattern that can never match is back"
    assert "have_ollama" in sh
    assert "json.load(sys.stdin)" in sh


def test_install_scripts_survive_a_status_command_that_fails():
    """A status check that cannot answer is not a reason to stop installing.

    Every caller used to be a bare `& $VenvPython ... | ConvertFrom-Json`. With
    $ErrorActionPreference = "Stop" a partly-installed venv or a traceback on
    stderr leaves nothing on stdout, ConvertFrom-Json throws on the empty
    string, and the install dies AFTER pip install and BEFORE the launcher is
    written.
    """
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "function Read-ShamsuJson" in ps1
    assert "function Get-JsonValue" in ps1
    # No status command may be piped straight into ConvertFrom-Json any more.
    for line in ps1.splitlines():
        if "ConvertFrom-Json" in line and "shamsu." in line:
            raise AssertionError(f"unguarded status parse: {line.strip()}")


def test_install_scripts_do_not_abort_when_writing_the_ollama_config_fails():
    """Reached after the package is installed and before the launcher exists.
    The config is a convenience; the install is not."""
    ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert "Could not write the Ollama config" in ps1
    assert "could not write the ollama config" in sh.lower()


def test_uninstall_scripts_do_not_stop_at_the_first_problem():
    """Proven 2026-08-20: a `path.json` missing one property made uninstall.ps1
    die inside the PATH step - after deleting a launcher, before removing the
    virtual environment or the runtime state. Both were left on disk and the
    script exited 1: a half uninstall that reports failure and does not say what
    remains."""
    ps1 = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")

    assert '$ErrorActionPreference = "Continue"' in ps1
    assert "function Invoke-Step" in ps1
    assert "finished with" in ps1

    assert "set -e" not in _code_only(sh), "an uninstaller must not abort on the first failure"
    assert "set -uo pipefail" in sh
    assert "FAILURES" in sh


def test_uninstall_scripts_read_the_path_manifest_defensively():
    """StrictMode makes a missing property a terminating error, so
    `$Manifest.added_by_shamsu` is only safe while the file is perfect."""
    ps1 = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")

    assert "function Get-JsonValue" in ps1
    assert "function Read-PathManifest" in ps1
    assert "$Manifest.added_by_shamsu" not in ps1
    assert "$Manifest.managed_by" not in ps1


def test_uninstall_never_removes_an_empty_path():
    """`set -u` is kept precisely so an unset variable cannot become `rm -rf ''`
    with the caller's working directory as the target."""
    sh = (REPO_ROOT / "scripts" / "uninstall.sh").read_text(encoding="utf-8")

    assert 'if [[ -z "${target}" ]]; then' in sh
    assert "no path was resolved" in sh


def test_uninstall_removes_its_own_path_manifest():
    """A stale manifest is what makes the NEXT install think it already owns a
    PATH entry it no longer has - and it is the file that broke uninstall."""
    ps1 = (REPO_ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")

    assert 'Describe "PATH manifest"' in ps1
    assert 'Describe "empty launcher directory"' in ps1


def test_the_prompt_and_bundled_skills_are_packaged():
    """The system prompt is a markdown file now, and skills are markdown too.
    A packaging rule that only ships `*.py` would leave the installed agent with
    no instructions and no skills - and the fallback in `simple_prompt` would
    quietly hide it."""
    import shamsu.agents.simple_prompt as simple_prompt
    from shamsu.skills.loader import bundled_skills_root

    assert simple_prompt.PROMPT_FILE.is_file()
    assert (bundled_skills_root() / "large-file-surgery" / "SKILL.md").is_file()
