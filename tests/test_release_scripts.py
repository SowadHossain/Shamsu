from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UNIX_SCRIPTS = tuple(ROOT / "scripts" / name for name in (
    "install.sh",
    "run-shamsu.sh",
    "doctor.sh",
    "uninstall.sh",
))
POWERSHELL_SCRIPTS = tuple(ROOT / "scripts" / name for name in (
    "install.ps1",
    "run-shamsu.ps1",
    "doctor.ps1",
    "uninstall.ps1",
))


def test_release_lifecycle_scripts_exist_and_are_nonempty():
    assert all(path.stat().st_size > 0 for path in UNIX_SCRIPTS + POWERSHELL_SCRIPTS)


def test_unix_scripts_are_committed_with_lf_endings():
    for path in UNIX_SCRIPTS:
        assert b"\r\n" not in path.read_bytes(), path


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_unix_lifecycle_scripts_parse_with_bash():
    result = subprocess.run(
        ["bash", "-n", *(path.relative_to(ROOT).as_posix() for path in UNIX_SCRIPTS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell parser test is Windows-only")
def test_windows_lifecycle_scripts_parse_with_powershell():
    quoted = ",".join(f"'{path}'" for path in POWERSHELL_SCRIPTS)
    command = (
        "$failed=$false; foreach($file in @("
        + quoted
        + ")) { $tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$tokens,[ref]$errors) | Out-Null; "
        "if($errors){$errors | ForEach-Object {Write-Error $_}; $failed=$true} }; if($failed){exit 1}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installers_and_uninstallers_share_managed_paths():
    unix_install = UNIX_SCRIPTS[0].read_text(encoding="utf-8")
    unix_uninstall = UNIX_SCRIPTS[-1].read_text(encoding="utf-8")
    windows_install = POWERSHELL_SCRIPTS[0].read_text(encoding="utf-8")
    windows_uninstall = POWERSHELL_SCRIPTS[-1].read_text(encoding="utf-8")

    assert '${HOME}/.local/bin' in unix_install and '${HOME}/.local/bin' in unix_uninstall
    assert '.shamsu\\bin' in windows_install and '.shamsu\\bin' in windows_uninstall
    assert 'REPO_ROOT' in unix_install and 'REPO_ROOT' in unix_uninstall
    assert '$RepoRoot' in windows_install and '$RepoRoot' in windows_uninstall
