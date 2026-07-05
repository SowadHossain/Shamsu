from __future__ import annotations

from shamsu.indexer.walker import FileWalker
from shamsu.retriever.search import SearchAgent


def _index(tmp_path):
    db_path = tmp_path / ".shamsu" / "index.db"
    FileWalker(tmp_path, db_path=db_path).index()
    return SearchAgent(db_path)


def test_bm25_index_is_not_built_until_first_search_call(tmp_path):
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    agent = _index(tmp_path)

    assert agent._bm25_built is False
    assert agent._bm25_index is None

    agent.search("helper")

    assert agent._bm25_built is True


def test_fts_query_quotes_boolean_words():
    query = SearchAgent._build_fts_query("run the game now and give me the link")

    assert '"and"' in query
    assert " AND " not in query


def test_bm25_index_builds_only_once_per_instance(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    agent = _index(tmp_path)

    build_calls = []
    original = agent._ensure_bm25_index

    def spy():
        build_calls.append(1)
        original()

    monkeypatch.setattr(agent, "_ensure_bm25_index", spy)

    agent.search("helper")
    agent.search("helper again")

    assert len(build_calls) == 2  # called each time...
    assert agent._bm25_built is True  # ...but the guard inside prevents rebuilding


def test_symbol_match_boosts_file_containing_matching_symbol(tmp_path):
    (tmp_path / "auth_helper.py").write_text(
        "def authenticate_user():\n"
        "    # verifies credentials\n"
        "    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text(
        "def other_thing():\n"
        "    # mentions authenticate_user in a comment only, no such symbol here\n"
        "    return authenticate_user_flag\n",
        encoding="utf-8",
    )
    agent = _index(tmp_path)

    results = agent.search("authenticate_user", top_k=5)

    assert results
    assert results[0].file_path == "auth_helper.py"


def test_path_match_boosts_file_whose_path_contains_query_term(tmp_path):
    (tmp_path / "payments.py").write_text(
        "class PaymentGateway:\n"
        "    def charge(self):\n"
        "        # payments logic lives here, mentions payments twice\n"
        "        return True\n",
        encoding="utf-8",
    )
    (tmp_path / "misc.py").write_text(
        "def unrelated():\n"
        "    # also references payments in passing, same term frequency-ish\n"
        "    return True\n",
        encoding="utf-8",
    )
    agent = _index(tmp_path)

    results = agent.search("payments", top_k=5)

    assert results
    assert results[0].file_path == "payments.py"


def test_boost_paths_promotes_traceback_file_above_a_stronger_fts_match(tmp_path):
    # "popular.py" repeats the query term many times so plain FTS5 bm25
    # ranks it above "rare.py", which mentions it only once.
    (tmp_path / "popular.py").write_text(
        "def target_function():\n"
        "    # target_function target_function target_function target_function\n"
        "    return target_function\n",
        encoding="utf-8",
    )
    (tmp_path / "rare.py").write_text(
        "def target_function():\n"
        "    return True\n",
        encoding="utf-8",
    )
    agent = _index(tmp_path)

    unboosted = agent.search("target_function", top_k=1)
    assert unboosted[0].file_path == "popular.py"

    boosted = agent.search("target_function", top_k=1, boost_paths=["rare.py"])
    assert boosted[0].file_path == "rare.py"


def test_boost_paths_matches_absolute_traceback_paths_with_backslashes(tmp_path):
    (tmp_path / "app.py").write_text(
        "def crashes():\n    raise ValueError('boom')\n", encoding="utf-8"
    )
    (tmp_path / "other.py").write_text(
        "def crashes_too():\n    raise ValueError('boom boom boom')\n", encoding="utf-8"
    )
    agent = _index(tmp_path)

    boosted = agent.search("boom", top_k=1, boost_paths=[r"C:\repo\workspace\app.py"])

    assert boosted[0].file_path == "app.py"


def test_search_still_returns_results_with_no_boosts_matching(tmp_path):
    (tmp_path / "plain.py").write_text(
        "def ordinary_function():\n    return 42\n", encoding="utf-8"
    )
    agent = _index(tmp_path)

    results = agent.search("ordinary_function")

    assert results
    assert results[0].file_path == "plain.py"


def test_search_returns_empty_when_no_fts_matches(tmp_path):
    (tmp_path / "plain.py").write_text("def ordinary_function():\n    return 42\n", encoding="utf-8")
    agent = _index(tmp_path)

    assert agent.search("zzz_nonexistent_term_zzz") == []
