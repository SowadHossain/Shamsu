from __future__ import annotations


from shamsu.abstract.types import AdapterResult
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter, is_local_uri


def test_is_local_uri_accepts_localhost_variants():
    assert is_local_uri("") is True
    assert is_local_uri("http://localhost:9749") is True
    assert is_local_uri("http://127.0.0.1:9749") is True
    assert is_local_uri("http://[::1]:9749") is True
    assert is_local_uri("file:///home/me/project") is True
    assert is_local_uri("/home/me/project") is True


def test_is_local_uri_rejects_remote_host():
    assert is_local_uri("http://example.com:9749") is False
    assert is_local_uri("https://cbm.saas-provider.io/api") is False


def test_healthcheck_rejects_remote_uri(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAMSU_CODEBASE_MEMORY_URI", "https://example.com/cbm")
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "Rejected non-local" in health.message


def test_healthcheck_accepts_local_uri_and_falls_through_to_binary_check(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAMSU_CODEBASE_MEMORY_URI", "http://127.0.0.1:9749")
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    # No binary installed in the test sandbox - honest "unavailable", not "rejected".
    assert health.ok is False
    assert "Rejected" not in health.message


def test_resolve_binary_uses_explicit_cmd_override(monkeypatch, tmp_path):
    fake_binary = tmp_path / "codebase-memory-mcp-custom"
    fake_binary.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    monkeypatch.setenv("SHAMSU_CODEBASE_MEMORY_CMD", str(fake_binary))

    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "unused")

    assert adapter.resolve_binary() == fake_binary


def test_resolve_binary_returns_none_when_nothing_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAMSU_CODEBASE_MEMORY_CMD", raising=False)
    monkeypatch.delenv("SHAMSU_CODEBASE_MEMORY_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "does-not-exist")

    assert adapter.resolve_binary() is None


def test_healthcheck_is_unavailable_when_binary_missing(tmp_path):
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "does-not-exist")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "not found" in health.message


def test_run_cli_does_not_fake_success_when_binary_missing(tmp_path):
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "does-not-exist")

    result = adapter.run_cli(tmp_path, "search_graph", {"name_pattern": "Foo"})

    assert result.ok is False
    assert result.error
    assert result.to_dict()["ok"] is False


def test_query_methods_return_dict_with_ok_false_when_tool_missing(tmp_path):
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "does-not-exist")

    assert adapter.get_exports(tmp_path, "foo.py")["ok"] is False
    assert adapter.get_imports(tmp_path, "foo.py")["ok"] is False
    assert adapter.get_references(tmp_path, "Foo")["ok"] is False
    assert adapter.get_impact(tmp_path, "foo.py")["ok"] is False
    assert adapter.get_module_contract(tmp_path, "foo.py")["ok"] is False


def test_index_workspace_installs_cbmignore_without_replacing_user_rules(monkeypatch, tmp_path):
    ignore = tmp_path / ".cbmignore"
    ignore.write_text("private-notes/\n", encoding="utf-8")
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "unused")
    monkeypatch.setattr(
        adapter,
        "run_cli",
        lambda workspace, tool, args: AdapterResult(ok=True, data={"status": "indexed"}),
    )

    result = adapter.index_workspace(tmp_path)
    first = ignore.read_text(encoding="utf-8")
    adapter.index_workspace(tmp_path)

    assert result["ok"] is True
    assert "private-notes/" in first
    assert ".shamsu/" in first
    assert "node_modules/" in first
    assert first.count("BEGIN SHAMSU MANAGED EXCLUSIONS") == 1
    assert ignore.read_text(encoding="utf-8") == first


def test_refresh_rebuilds_an_existing_index_that_contains_internal_paths(monkeypatch, tmp_path):
    adapter = CodebaseMemoryAdapter(tool_dir=tmp_path / "unused")
    index_calls = []
    monkeypatch.setattr(
        adapter,
        "index_workspace",
        lambda workspace: index_calls.append(workspace) or {"ok": True, "status": "indexed"},
    )
    monkeypatch.setattr(adapter, "_internal_index_paths", lambda workspace: [".shamsu/runs/old.json"])
    monkeypatch.setattr(adapter, "delete_workspace_index", lambda workspace: {"ok": True})

    result = adapter.refresh_workspace(tmp_path)

    assert result["ok"] is True
    assert result["rebuilt_for_policy"] is True
    assert result["removed_internal_paths"] == [".shamsu/runs/old.json"]
    assert len(index_calls) == 2
