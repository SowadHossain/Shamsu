from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from shamsu.runtime.doctor import (
    DoctorCheck,
    DoctorReport,
    check_ancestor_workspace,
    check_editable_install,
    check_nested_workspaces,
    check_ollama,
    check_path_manifest,
    find_ancestor_workspace,
    find_nested_workspaces,
    format_report,
    run_doctor,
)
from shamsu.runtime.ollama import RuntimeStatus


def test_check_editable_install_ok_when_package_is_inside_repo_root():
    import shamsu

    repo_root = Path(shamsu.__file__).resolve().parents[1]

    check = check_editable_install(repo_root)

    assert check.ok is True


def test_check_editable_install_warns_when_package_is_outside_repo_root(tmp_path):
    check = check_editable_install(tmp_path)

    assert check.ok is False
    assert "Reinstall" in check.suggestion


def test_check_ollama_ok_when_ready():
    status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b"],
        missing_models=[],
    )

    check = check_ollama(status)

    assert check.ok is True


def test_check_ollama_warns_when_not_ready():
    status = RuntimeStatus(missing_models=["qwen3:8b"])

    check = check_ollama(status)

    assert check.ok is False
    assert "models repair" in check.suggestion


def test_find_nested_workspaces_ignores_repo_root_shamsu(tmp_path):
    (tmp_path / ".shamsu").mkdir()

    assert find_nested_workspaces(tmp_path) == []


def test_find_nested_workspaces_finds_stray_subfolder(tmp_path):
    (tmp_path / ".shamsu").mkdir()
    nested = tmp_path / "scripts" / ".shamsu"
    nested.mkdir(parents=True)

    found = find_nested_workspaces(tmp_path)

    assert found == [nested.resolve()]


def test_find_nested_workspaces_skips_venv_and_git(tmp_path):
    (tmp_path / ".venv" / "lib" / ".shamsu").mkdir(parents=True)
    (tmp_path / ".git" / ".shamsu").mkdir(parents=True)

    assert find_nested_workspaces(tmp_path) == []


def test_check_nested_workspaces_reports_found_paths(tmp_path):
    nested = tmp_path / "scripts" / ".shamsu"
    nested.mkdir(parents=True)

    check = check_nested_workspaces(tmp_path)

    assert check.ok is False
    assert str(nested.resolve()) in check.detail


def test_find_ancestor_workspace_finds_parent_with_shamsu(tmp_path):
    (tmp_path / ".shamsu").mkdir()
    child = tmp_path / "scripts"
    child.mkdir()

    assert find_ancestor_workspace(child) == tmp_path.resolve()


def test_find_ancestor_workspace_returns_none_when_absent(tmp_path):
    child = tmp_path / "scripts"
    child.mkdir()

    assert find_ancestor_workspace(child) is None


def test_find_ancestor_workspace_ignores_global_launcher_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".shamsu").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    child = fake_home / "projects" / "my-app"
    child.mkdir(parents=True)

    assert find_ancestor_workspace(child) is None


def test_find_nested_workspaces_ignores_global_launcher_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".shamsu").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    assert find_nested_workspaces(fake_home) == []


def test_check_ancestor_workspace_warns_with_suggestion(tmp_path):
    (tmp_path / ".shamsu").mkdir()
    child = tmp_path / "scripts"
    child.mkdir()

    check = check_ancestor_workspace(child)

    assert check.ok is False
    assert "--workspace" in check.suggestion


def test_check_path_manifest_ok_when_manifest_missing(tmp_path):
    bin_dir = tmp_path / ".shamsu" / "bin"

    check = check_path_manifest(bin_dir)

    assert check.ok is True


@pytest.mark.skipif(sys.platform != "win32", reason="PATH manifest checking is Windows-only")
def test_check_path_manifest_ok_when_entry_present(tmp_path, monkeypatch):
    bin_dir = tmp_path / ".shamsu" / "bin"
    bin_dir.mkdir(parents=True)
    manifest_path = bin_dir.parent / "path.json"
    manifest_path.write_text(json.dumps({"path_entry": str(bin_dir)}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "C:\\Windows")

    check = check_path_manifest(bin_dir)

    assert check.ok is True


@pytest.mark.skipif(sys.platform != "win32", reason="PATH manifest checking is Windows-only")
def test_check_path_manifest_handles_powershell_utf8_bom(tmp_path, monkeypatch):
    bin_dir = tmp_path / ".shamsu" / "bin"
    bin_dir.mkdir(parents=True)
    manifest_path = bin_dir.parent / "path.json"
    # PowerShell's `Set-Content -Encoding UTF8` always writes a BOM.
    manifest_path.write_text(
        json.dumps({"path_entry": str(bin_dir)}), encoding="utf-8-sig"
    )
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "C:\\Windows")

    check = check_path_manifest(bin_dir)

    assert check.ok is True


@pytest.mark.skipif(sys.platform != "win32", reason="PATH manifest checking is Windows-only")
def test_check_path_manifest_warns_when_entry_missing_from_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / ".shamsu" / "bin"
    bin_dir.mkdir(parents=True)
    manifest_path = bin_dir.parent / "path.json"
    manifest_path.write_text(json.dumps({"path_entry": str(bin_dir)}), encoding="utf-8")
    monkeypatch.setenv("PATH", "C:\\Windows")

    check = check_path_manifest(bin_dir)

    assert check.ok is False
    assert "new terminal" in check.suggestion


def test_run_doctor_combines_all_checks(tmp_path):
    import shamsu

    repo_root = Path(shamsu.__file__).resolve().parents[1]
    ready_status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b"],
        missing_models=[],
    )

    report = run_doctor(
        workspace=tmp_path,
        repo_root=repo_root,
        ollama_status=ready_status,
        bin_dir=tmp_path / "bin",
    )

    assert isinstance(report, DoctorReport)
    assert len(report.checks) == 5
    assert report.all_ok is True


def test_run_doctor_flags_problems_when_present(tmp_path):
    nested = tmp_path / "scripts" / ".shamsu"
    nested.mkdir(parents=True)
    not_ready = RuntimeStatus(missing_models=["qwen3:8b"])

    report = run_doctor(
        workspace=tmp_path,
        repo_root=tmp_path,
        ollama_status=not_ready,
        bin_dir=tmp_path / "bin",
    )

    assert report.all_ok is False


def test_format_report_marks_warnings():
    report = DoctorReport(checks=(DoctorCheck("thing", False, "bad", "fix it"),))

    text = format_report(report)

    assert "WARN" in text
    assert "fix it" in text


def test_format_report_marks_ok():
    report = DoctorReport(checks=(DoctorCheck("thing", True, "great"),))

    text = format_report(report)

    assert "OK" in text
    assert "Everything looks fine." in text
