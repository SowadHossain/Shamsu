from __future__ import annotations

from shamsu.retriever.search import NullSearchAgent, SearchAgent


class FakeCodebaseMemorySearchAdapter:
    """Fake adapter simulating real Codebase-Memory MCP response shapes
    (verified live against the actual tool) - no subprocess, no real binary."""

    def __init__(self, code_matches=None, symbol_rows=None, snippets=None) -> None:
        self.code_matches = code_matches or []
        self.symbol_rows = symbol_rows or []
        self.snippets = snippets or {}

    def search_code(self, workspace, pattern, limit=20, ignore_case=True):
        return {"ok": True, "results": self.code_matches[:limit]}

    def get_symbols(self, workspace, query_or_path):
        return {"ok": True, "results": self.symbol_rows}

    def get_code_snippet(self, workspace, qualified_name):
        snippet = self.snippets.get(qualified_name)
        return {"ok": True, **snippet} if snippet else {"ok": False, "error": "not found"}


def test_fts_search_maps_search_code_results_to_search_results(tmp_path):
    (tmp_path / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    adapter = FakeCodebaseMemorySearchAdapter(
        code_matches=[
            {"node": "login", "qualified_name": "proj.auth.login", "file": "auth.py", "start_line": 1, "end_line": 2}
        ]
    )
    agent = SearchAgent(tmp_path, adapter=adapter)

    results = agent.fts_search("login")

    assert len(results) == 1
    assert results[0].file_path == "auth.py"
    assert results[0].language == "python"
    assert "def login" in results[0].content
    assert results[0].symbol_name == "login"


def test_search_returns_empty_when_adapter_reports_failure(tmp_path):
    class FailingAdapter:
        def search_code(self, workspace, pattern, limit=20, ignore_case=True):
            return {"ok": False, "error": "Codebase-Memory MCP binary is not available."}

    agent = SearchAgent(tmp_path, adapter=FailingAdapter())

    assert agent.search("anything") == []
    assert agent.fts_search("anything") == []


def test_search_falls_back_to_local_text_when_adapter_reports_failure(tmp_path):
    (tmp_path / "auth.py").write_text("def authenticate_user():\n    return True\n", encoding="utf-8")

    class FailingAdapter:
        def search_code(self, workspace, pattern, limit=20, ignore_case=True):
            return {"ok": False, "error": "offline"}

    results = SearchAgent(tmp_path, adapter=FailingAdapter()).fts_search("authenticate_user")

    assert [result.file_path for result in results] == ["auth.py"]


def test_external_search_results_cannot_expose_internal_shamsu_files(tmp_path):
    internal = tmp_path / ".shamsu" / "mutations"
    internal.mkdir(parents=True)
    (internal / "backup.py").write_text("def leaked_symbol():\n    pass\n", encoding="utf-8")
    adapter = FakeCodebaseMemorySearchAdapter(
        code_matches=[
            {
                "node": "leaked_symbol",
                "file": ".shamsu/mutations/backup.py",
                "start_line": 1,
                "end_line": 2,
            }
        ]
    )

    assert SearchAgent(tmp_path, adapter=adapter).fts_search("leaked_symbol") == []


def test_boost_paths_reorders_above_the_tools_own_ranking(tmp_path):
    (tmp_path / "popular.py").write_text("def target_function():\n    pass\n", encoding="utf-8")
    (tmp_path / "rare.py").write_text("def target_function():\n    pass\n", encoding="utf-8")
    adapter = FakeCodebaseMemorySearchAdapter(
        code_matches=[
            {"node": "target_function", "file": "popular.py", "start_line": 1, "end_line": 2},
            {"node": "target_function", "file": "rare.py", "start_line": 1, "end_line": 2},
        ]
    )
    agent = SearchAgent(tmp_path, adapter=adapter)

    unboosted = agent.search("target_function", top_k=1)
    assert unboosted[0].file_path == "popular.py"

    boosted = agent.search("target_function", top_k=1, boost_paths=["rare.py"])
    assert boosted[0].file_path == "rare.py"


def test_boost_paths_matches_absolute_traceback_paths_with_backslashes(tmp_path):
    (tmp_path / "app.py").write_text("def crashes():\n    pass\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("def crashes_too():\n    pass\n", encoding="utf-8")
    adapter = FakeCodebaseMemorySearchAdapter(
        code_matches=[
            {"node": "crashes_too", "file": "other.py", "start_line": 1, "end_line": 2},
            {"node": "crashes", "file": "app.py", "start_line": 1, "end_line": 2},
        ]
    )
    agent = SearchAgent(tmp_path, adapter=adapter)

    boosted = agent.search("crashes", top_k=1, boost_paths=[r"C:\repo\workspace\app.py"])

    assert boosted[0].file_path == "app.py"


def test_symbol_lookup_uses_get_code_snippet_for_real_source(tmp_path):
    adapter = FakeCodebaseMemorySearchAdapter(
        symbol_rows=[{"name": "login", "qualified_name": "proj.auth.login", "file_path": "auth.py", "signature": "()"}],
        snippets={"proj.auth.login": {"source": "def login():\n    return True\n", "start_line": 3, "end_line": 4}},
    )
    agent = SearchAgent(tmp_path, adapter=adapter)

    results = agent.symbol_lookup("login")

    assert len(results) == 1
    assert results[0].symbol_name == "login"
    assert results[0].line_start == 3
    assert results[0].line_end == 4
    assert "return True" in results[0].content


def test_symbol_lookup_falls_back_to_signature_when_snippet_unavailable(tmp_path):
    adapter = FakeCodebaseMemorySearchAdapter(
        symbol_rows=[{"name": "login", "qualified_name": "proj.auth.login", "file_path": "auth.py", "signature": "(request)"}],
    )
    agent = SearchAgent(tmp_path, adapter=adapter)

    results = agent.symbol_lookup("login")

    assert results[0].content == "(request)"


def test_null_search_agent_returns_empty_results():
    agent = NullSearchAgent()

    assert agent.search("anything") == []
    assert agent.symbol_lookup("anything") == []
    assert agent.fts_search("anything") == []
