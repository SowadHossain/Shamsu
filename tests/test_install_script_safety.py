from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_scripts_do_not_edit_shell_profiles_or_path():
    scripts = [
        REPO_ROOT / "scripts" / "install.ps1",
        REPO_ROOT / "scripts" / "install.sh",
        REPO_ROOT / "scripts" / "install-command.ps1",
        REPO_ROOT / "scripts" / "install-command.sh",
        REPO_ROOT / "scripts" / "run-shamsu.ps1",
        REPO_ROOT / "scripts" / "run-shamsu.sh",
    ]
    forbidden = [
        "$PROFILE",
        "SetEnvironmentVariable",
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
    assert "$ModelsPath" in ps1
    assert "--yes" in sh
    assert "--skip-ollama-install" in sh
    assert "--skip-models" in sh
    assert "--models-path" in sh


def test_windows_runtime_scripts_force_python_utf8_for_ollama_output():
    install_ps1 = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    run_ps1 = (REPO_ROOT / "scripts" / "run-shamsu.ps1").read_text(encoding="utf-8")
    install_sh = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    run_sh = (REPO_ROOT / "scripts" / "run-shamsu.sh").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in install_ps1
    assert '$env:PYTHONUTF8 = "1"' in run_ps1
    assert 'export PYTHONUTF8="${PYTHONUTF8:-1}"' in install_sh
    assert 'export PYTHONUTF8="${PYTHONUTF8:-1}"' in run_sh


def test_command_installers_create_thin_launchers_without_profile_edits():
    ps1 = (REPO_ROOT / "scripts" / "install-command.ps1").read_text(encoding="utf-8")
    sh = (REPO_ROOT / "scripts" / "install-command.sh").read_text(encoding="utf-8")

    assert "scripts\\run-shamsu.ps1" in ps1
    assert "shamsu.ps1" in ps1
    assert "shamsu.cmd" in ps1
    assert "@ShamsuArgs" in ps1
    assert "(Get-Location).Path" in ps1
    assert "-InputObject (`$PipedInput -join [Environment]::NewLine)" in ps1
    assert "-Workspace `$Workspace @ShamsuArgs" in ps1
    assert '-Workspace "%CD%" %*' in ps1
    assert "did not edit your PowerShell profile, PATH, registry, or global Python" in ps1

    assert "scripts/run-shamsu.sh" in sh
    assert 'exec "${RUN_SCRIPT}" "\\$@"' in sh
    assert "${HOME}/.local/bin" in sh
    assert "did not edit your shell profile, PATH, global Python, or system registry" in sh
