from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from shamsu.abstract.service import AbstractService
from shamsu.memory.service import MemoryService
from shamsu.runtime.doctor import (
    DoctorCheck,
    DoctorReport,
    check_ancestor_workspace,
    check_codebase_memory,
    check_cookbook,
    check_editable_install,
    check_nested_workspaces,
    check_ollama,
    check_path_manifest,
    check_state_schema,
    check_web_capability,
    check_workspace_state,
    find_ancestor_workspace,
    find_nested_workspaces,
    format_report,
    run_doctor,
    run_first_run_checks,
    write_first_run_report,
)
from shamsu.runtime.ollama import RuntimeStatus
from tests.test_abstract_service import FakeCodebaseMemoryAdapter
from tests.test_graphiti_memory import FakeGraphitiAdapter


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


def test_check_cookbook_ok_when_only_allowed_models_are_installed():
    status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b", "qwen2.5-coder:7b-instruct"],
    )

    check = check_cookbook(status)

    assert check.ok is True


def test_check_cookbook_warns_for_off_cookbook_models():
    status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b", "mistral:7b-instruct-q4_K_M"],
    )

    check = check_cookbook(status)

    assert check.ok is False
    assert "mistral" in check.detail


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


def test_check_codebase_memory_warns_when_unavailable(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    check = check_codebase_memory(tmp_path, service)

    assert check.ok is False
    assert "/abstract setup" in check.suggestion


def test_check_codebase_memory_ok_when_healthy_and_indexed(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))
    service.ensure_ready()

    check = check_codebase_memory(tmp_path, service)

    assert check.ok is True


def test_run_doctor_combines_all_checks(tmp_path):
    import shamsu

    repo_root = Path(shamsu.__file__).resolve().parents[1]
    ready_status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b"],
        missing_models=[],
    )
    cbm_service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))
    cbm_service.ensure_ready()

    report = run_doctor(
        workspace=tmp_path,
        repo_root=repo_root,
        ollama_status=ready_status,
        bin_dir=tmp_path / "bin",
        codebase_memory_service=cbm_service,
        graphiti_memory_service=MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=True)),
    )

    assert isinstance(report, DoctorReport)
    assert len(report.checks) == 13
    assert any(check.name == "diagnostics" for check in report.checks)
    assert report.all_ok is True


def test_first_run_checks_cover_productive_capabilities(tmp_path, monkeypatch):
    ready_status = RuntimeStatus(
        ollama_path="/usr/bin/ollama",
        server_running=True,
        installed_models=["qwen3:8b"],
    )
    cbm_service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))
    cbm_service.ensure_ready()
    monkeypatch.setattr(
        "shamsu.runtime.doctor.check_web_capability",
        lambda _workspace: DoctorCheck("web_capability", True, "ready"),
    )
    monkeypatch.setattr(
        "shamsu.runtime.doctor.check_browser_capability",
        lambda _workspace: DoctorCheck("browser_capability", True, "ready"),
    )

    report = run_first_run_checks(
        tmp_path,
        ollama_status=ready_status,
        codebase_memory_service=cbm_service,
        graphiti_memory_service=MemoryService(
            tmp_path, adapter=FakeGraphitiAdapter(available=True)
        ),
    )
    path = write_first_run_report(tmp_path, report)

    assert len(report.checks) == 6
    assert report.all_ok is True
    assert path.is_file()
    assert {item["name"] for item in json.loads(path.read_text(encoding="utf-8"))} == {
        "ollama",
        "model_cookbook",
        "codebase_memory",
        "graphiti_memory",
        "web_capability",
        "browser_capability",
    }


def test_web_capability_accepts_configured_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_WEB_ENABLED", "true")
    monkeypatch.setenv("SHAMSU_WEB_SEARCH_PROVIDER", "auto")

    check = check_web_capability(tmp_path)

    assert check.ok is True
    assert "fallback=ready" in check.detail


def test_state_schema_reports_old_marker(tmp_path):
    marker = tmp_path / ".shamsu" / "state.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    check = check_state_schema(tmp_path)

    assert check.ok is False
    assert "needs upgrade" in check.detail


def test_check_workspace_state_reports_corrupt_run_and_index_json(tmp_path):
    index_path = tmp_path / ".shamsu" / "abstract" / "last-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{broken", encoding="utf-8")
    run_dir = tmp_path / ".shamsu" / "runs" / "run_broken"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

    check = check_workspace_state(tmp_path)

    assert check.ok is False
    assert "invalid JSON" in check.detail
    assert "run_broken" in check.detail


def test_check_workspace_state_reports_contradictory_index_generations(tmp_path):
    index_path = tmp_path / ".shamsu" / "abstract" / "last-index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps(
            {
                "indexed": True,
                "forced_stale": False,
                "workspace_generation": 2,
                "indexed_generation": 3,
            }
        ),
        encoding="utf-8",
    )

    check = check_workspace_state(tmp_path)

    assert check.ok is False
    assert "ahead" in check.detail


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
