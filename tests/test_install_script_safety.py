from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    assert "if ($PrefetchModels -and -not $SkipModels -and $RuntimeStatus.ollama_path)" in ps1
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
    assert "Removed stray nested workspace state" in ps1
    assert '-notmatch \'\\\\\\.venv\\\\\'' in ps1

    assert 'find "${REPO_ROOT}" -type d -name ".shamsu"' in sh
    assert "Removed stray nested workspace state" in sh
    assert "-not -path \"*/.venv/*\"" in sh
