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

    # A pytest tmp_path is a disposable workspace and is refused by default, so
    # this test - which is about the ignore file, not about the policy - says
    # explicitly that it means to index one.
    result = adapter.index_workspace(tmp_path, force=True)
    first = ignore.read_text(encoding="utf-8")
    adapter.index_workspace(tmp_path, force=True)

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


# --- throwaway directories must not enter a global store (G1) -----------
#
# Measured 2026-08-19: 243 indexed projects. 129 lived under the system temp
# directory - eval scratchpads from a single day in July that had not existed
# for weeks - and 30 more were eval-artifact folders inside this repository,
# indexed as separate projects alongside it. The store is global and keyed by a
# mangled absolute path, so nothing ever cleaned up.


def test_a_temp_directory_is_recognised_as_throwaway(tmp_path):
    from shamsu.tools.codebase_memory import disposable_workspace

    # pytest's tmp_path IS under the system temp directory, which is the point.
    assert disposable_workspace(tmp_path)


def test_an_eval_artifact_directory_is_recognised():
    """The 30 that sit INSIDE this repository, indexed as separate projects
    alongside it. Checked on a real project path so the temp-dir signal cannot
    answer for it."""
    from pathlib import Path

    from shamsu.tools.codebase_memory import disposable_workspace

    target = Path("F:/Work/PROJECTS/shamsu/Shamsu/tmp/universal-prd-eval/eval_20260801_0109")

    assert "evaluation artifact" in disposable_workspace(target)


def test_a_real_project_is_not_refused(tmp_path):
    """The repo this runs in is a real project and must stay indexable."""
    from pathlib import Path

    from shamsu.tools.codebase_memory import disposable_workspace

    assert disposable_workspace(Path(__file__).resolve().parent.parent) == ""


def test_a_project_merely_called_tmp_is_not_refused():
    """Matched on the PATH, not on a single name - a real project called `tmp`
    is somebody's actual work."""
    from pathlib import Path

    from shamsu.tools.codebase_memory import disposable_workspace

    assert disposable_workspace(Path("F:/Work/tmp")) == ""


def test_indexing_a_throwaway_workspace_is_refused_and_says_why(tmp_path):
    """Refused rather than skipped silently: a caller that asked for an index
    deserves to know it did not get one."""
    adapter = CodebaseMemoryAdapter()

    result = adapter.index_workspace(tmp_path)

    assert result["ok"] is False
    assert result["skipped"] == "disposable_workspace"
    assert "temporary directory" in result["error"]


def test_a_refused_index_leaves_the_directory_untouched(tmp_path):
    """The refusal comes first, so a throwaway directory is left as found."""
    adapter = CodebaseMemoryAdapter()

    adapter.index_workspace(tmp_path)

    assert not (tmp_path / ".cbmignore").exists()


def test_force_is_the_escape(tmp_path):
    """A guard with no way past it is a deadlock. `force=True` gets as far as
    installing the ignore file, which is proof it went past the refusal."""
    adapter = CodebaseMemoryAdapter()

    adapter.index_workspace(tmp_path, force=True)

    assert (tmp_path / ".cbmignore").exists()


# --- the graph lives in the workspace, like smallcode's (G1) -------------
#
# Codebase-Memory defaults to ONE global cache keyed by a mangled absolute path:
# 243 projects and 619 MB on 2026-08-19, of which 129 were temp directories that
# had not existed for weeks. smallcode keeps its graph at `.code-graph/graph.db`
# inside the project, and that one decision makes the whole class of problem
# impossible. `CBM_CACHE_DIR` - not in `--help` or `config list`, found in the
# binary's strings - lets SHAMSU do the same.


def test_the_graph_store_is_inside_the_workspace(tmp_path):
    from shamsu.paths import code_graph_dir

    adapter = CodebaseMemoryAdapter()

    env = adapter._cli_env(tmp_path)

    assert env["CBM_CACHE_DIR"] == str(code_graph_dir(tmp_path))
    assert str(tmp_path) in env["CBM_CACHE_DIR"], "the index must not outlive the project"


def test_the_store_directory_is_created(tmp_path):
    from shamsu.paths import code_graph_dir

    CodebaseMemoryAdapter()._cli_env(tmp_path)

    assert code_graph_dir(tmp_path).is_dir()


def test_two_workspaces_never_share_a_store(tmp_path):
    """Cross-project contamination is what put SmallCTL's `message_tokens` in
    an answer about SHAMSU. Separate stores make it structurally impossible."""
    adapter = CodebaseMemoryAdapter()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()

    assert adapter._cli_env(one)["CBM_CACHE_DIR"] != adapter._cli_env(two)["CBM_CACHE_DIR"]


def test_the_global_cache_can_be_restored(tmp_path, monkeypatch):
    """The escape, for anyone who wants one graph across several checkouts."""
    monkeypatch.setenv("SHAMSU_CBM_GLOBAL_CACHE", "1")

    assert "CBM_CACHE_DIR" not in CodebaseMemoryAdapter()._cli_env(tmp_path)


def test_an_unwritable_workspace_falls_back_rather_than_losing_the_graph(tmp_path, monkeypatch):
    from shamsu import paths

    def boom(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(paths.Path, "mkdir", boom)

    assert "CBM_CACHE_DIR" not in CodebaseMemoryAdapter()._cli_env(tmp_path)
