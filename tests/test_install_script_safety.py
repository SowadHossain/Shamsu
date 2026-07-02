from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_scripts_do_not_edit_shell_profiles_or_path():
    scripts = [
        REPO_ROOT / "scripts" / "install.ps1",
        REPO_ROOT / "scripts" / "install.sh",
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
