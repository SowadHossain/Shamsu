"""Structural retrieval: symbols, references, callers, callees, impact.

Built on a small synthetic repository rather than on SHAMSU itself, so the
expected answers are exhaustively known. `tests/evals/test_retrieval_accuracy.py`
does the opposite — it scores the same index against this repository, where the
answers are realistic but not enumerable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.code_intelligence.index import PythonCodeIndex
from shamsu.code_intelligence.retrieval import (
    STAGES,
    StructuredRetriever,
    related_files_for,
)
from shamsu.interfaces.code_intelligence import LineRange, SearchHit, SymbolRef

CALC = '''"""Arithmetic."""

TAX_RATE = 0.2


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def subtract(a: int, b: int) -> int:
    return add(a, -b)


class Ledger:
    """Holds entries."""

    def total(self, values: list[int]) -> int:
        running = 0
        for value in values:
            running = add(running, value)
        return running

    def record(self, value: int) -> None:
        self.entries.append(value)
'''

REPORT = '''"""Reporting."""

from calc import Ledger, add


def summarise(values: list[int]) -> str:
    ledger = Ledger()
    return f"total={ledger.total(values)} first={add(values[0], 0)}"
'''

UNRELATED = '''"""Nothing here calls anything."""


class Store:
    def record(self, value: int) -> None:
        """A name collision with Ledger.record, on purpose."""
'''

TEST_CALC = """from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""

TEST_VIA_PACKAGE = """import calc


def test_tax() -> None:
    assert calc.TAX_RATE == 0.2
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "report.py").write_text(REPORT, encoding="utf-8")
    (root / "unrelated.py").write_text(UNRELATED, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    (root / "tests" / "test_tax.py").write_text(TEST_VIA_PACKAGE, encoding="utf-8")
    return root


@pytest.fixture
def index(repo: Path) -> PythonCodeIndex:
    return PythonCodeIndex(repo, use_git=False).build()


def _only(refs: object) -> SymbolRef:
    assert isinstance(refs, tuple) and len(refs) == 1, refs
    assert isinstance(refs[0], SymbolRef)
    return refs[0]


# ---------------------------------------------------------------------------
# Stage 1-2: paths and text
# ---------------------------------------------------------------------------


class TestPathsAndText:
    def test_an_exact_path_wins_outright(self, index: PythonCodeIndex) -> None:
        assert index.find_file("calc.py") == ("calc.py",)

    def test_a_glob_matches(self, index: PythonCodeIndex) -> None:
        assert index.find_file("tests/*.py") == ("tests/test_calc.py", "tests/test_tax.py")

    def test_a_basename_is_the_last_resort(self, index: PythonCodeIndex) -> None:
        assert index.find_file("some/where/test_calc.py") == ("tests/test_calc.py",)

    def test_text_search_is_case_sensitive_because_code_is(self, index: PythonCodeIndex) -> None:
        assert index.search_text("TAX_RATE") != ()
        assert index.search_text("tax_rate") == ()

    def test_text_hits_carry_their_line(self, index: PythonCodeIndex) -> None:
        hit = index.search_text("TAX_RATE")[0]
        assert hit.path == "calc.py"
        assert hit.lines == LineRange(start=3, end=3)
        assert hit.provenance == "text"


# ---------------------------------------------------------------------------
# Stage 3: symbols
# ---------------------------------------------------------------------------


class TestSymbols:
    def test_functions_classes_methods_and_constants_are_all_indexed(
        self, index: PythonCodeIndex
    ) -> None:
        kinds = {_only(index.lookup_symbol(name)).kind for name in ("add", "Ledger", "TAX_RATE")}
        assert kinds == {"function", "class", "constant"}
        assert _only(index.lookup_symbol("calc.Ledger.total")).kind == "method"

    def test_a_qualified_name_beats_a_bare_one(self, index: PythonCodeIndex) -> None:
        """A caller taking `[0]` must get the best match, not an arbitrary one."""
        assert _only(index.lookup_symbol("calc.add")).path == "calc.py"

    def test_a_dotted_name_narrows_across_a_collision(self, index: PythonCodeIndex) -> None:
        """`Ledger.record` and `Store.record` share a bare name."""
        assert len(index.lookup_symbol("record")) == 2
        assert _only(index.lookup_symbol("Ledger.record")).path == "calc.py"
        assert _only(index.lookup_symbol("Store.record")).path == "unrelated.py"

    def test_a_symbol_knows_where_it_lives(self, index: PythonCodeIndex) -> None:
        symbol = _only(index.lookup_symbol("calc.add"))
        assert symbol.lines.start == 6
        assert symbol.signature is not None and "a: int" in symbol.signature

    def test_an_unknown_name_returns_nothing_rather_than_a_guess(
        self, index: PythonCodeIndex
    ) -> None:
        assert index.lookup_symbol("does_not_exist") == ()


# ---------------------------------------------------------------------------
# Stages 4-5: references and calls
# ---------------------------------------------------------------------------


class TestReferencesAndCalls:
    def test_references_span_files(self, index: PythonCodeIndex) -> None:
        paths = {hit.path for hit in index.references(_only(index.lookup_symbol("calc.add")))}
        assert paths == {"calc.py", "report.py", "tests/test_calc.py"}

    def test_a_definitions_own_body_is_not_a_reference_to_itself(
        self, index: PythonCodeIndex
    ) -> None:
        """Recursion is not 'somewhere else that would break'."""
        symbol = _only(index.lookup_symbol("calc.add"))
        assert all(
            not (
                hit.path == symbol.path
                and symbol.lines.start <= hit.lines.start <= symbol.lines.end
            )
            for hit in index.references(symbol)
            if hit.lines
        )

    def test_callers_are_found_across_files(self, index: PythonCodeIndex) -> None:
        callers = {
            reference.qualified_name
            for reference in index.callers(_only(index.lookup_symbol("calc.add")))
        }
        assert "calc.subtract" in callers
        assert "calc.Ledger.total" in callers
        assert "report.summarise" in callers

    def test_a_call_belongs_to_the_innermost_symbol(self, index: PythonCodeIndex) -> None:
        """The call in `Ledger.total` is the method's, not the class's."""
        callers = {
            reference.qualified_name
            for reference in index.callers(_only(index.lookup_symbol("calc.add")))
        }
        assert "calc.Ledger" not in callers

    def test_callees_are_repository_symbols_only(self, index: PythonCodeIndex) -> None:
        """`SymbolRef` must point at a real location; `len` has none here."""
        callees = {
            reference.qualified_name
            for reference in index.callees(_only(index.lookup_symbol("report.summarise")))
        }
        assert "calc.add" in callees
        assert "calc.Ledger" in callees
        assert not any(name.endswith(".len") for name in callees)

    def test_a_symbol_that_calls_nothing_has_no_callees(self, index: PythonCodeIndex) -> None:
        assert index.callees(_only(index.lookup_symbol("Store.record"))) == ()


# ---------------------------------------------------------------------------
# Stage 6: related tests
# ---------------------------------------------------------------------------


class TestRelatedTests:
    def test_a_direct_import_relates_a_test(self, index: PythonCodeIndex) -> None:
        assert "tests/test_calc.py" in index.related_tests("calc.py")

    def test_a_package_import_plus_a_used_name_relates_a_test(self, index: PythonCodeIndex) -> None:
        """`import calc` never names `TAX_RATE`; the usage is what connects them."""
        assert "tests/test_tax.py" in index.related_tests("calc.py")

    def test_an_untested_file_says_so(self, index: PythonCodeIndex) -> None:
        assert index.related_tests("unrelated.py") == ()

    def test_a_test_is_not_related_to_itself(self, index: PythonCodeIndex) -> None:
        assert "tests/test_calc.py" not in index.related_tests("tests/test_calc.py")


# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------


class TestImpact:
    def test_impact_reaches_transitively_and_names_the_tests(self, index: PythonCodeIndex) -> None:
        report = index.impact(_only(index.lookup_symbol("calc.add")))
        assert {caller.qualified_name for caller in report.direct_callers} >= {
            "calc.subtract",
            "report.summarise",
        }
        assert set(report.transitive_modules) >= {"calc.py", "report.py"}
        assert "tests/test_calc.py" in report.related_tests

    def test_an_unused_symbol_has_no_impact(self, index: PythonCodeIndex) -> None:
        report = index.impact(_only(index.lookup_symbol("Store.record")))
        assert report.direct_callers == ()
        assert report.transitive_modules == ()

    def test_a_complete_traversal_is_not_marked_truncated(self, index: PythonCodeIndex) -> None:
        """`truncated` must mean something, or it means nothing."""
        assert index.impact(_only(index.lookup_symbol("calc.add"))).truncated is False


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_an_unbuilt_index_is_not_ready(self, repo: Path) -> None:
        assert PythonCodeIndex(repo, use_git=False).is_ready() is False

    def test_a_built_index_is_ready(self, index: PythonCodeIndex) -> None:
        assert index.is_ready() is True

    def test_an_edited_workspace_makes_the_index_stale(
        self, index: PythonCodeIndex, repo: Path
    ) -> None:
        """v1 gated on a marker that could disagree with the actual index."""
        (repo / "calc.py").write_text(CALC + "\n\ndef extra() -> None: ...\n", encoding="utf-8")
        assert index.is_ready() is False

    def test_a_syntax_error_degrades_one_file_not_the_index(self, repo: Path) -> None:
        (repo / "broken.py").write_text("def oops(:\n", encoding="utf-8")
        index = PythonCodeIndex(repo, use_git=False).build()

        assert index.parse_failures == ("broken.py",)
        assert index.lookup_symbol("calc.add") != ()


# ---------------------------------------------------------------------------
# The ordered pipeline
# ---------------------------------------------------------------------------


class TestRetrievalOrder:
    def test_an_exact_path_answers_at_stage_one(self, index: PythonCodeIndex) -> None:
        result = StructuredRetriever(index).retrieve("calc.py")
        assert result.stage == "exact_path"
        assert result.attempted == ("exact_path",)

    def test_a_literal_answers_at_the_text_stage(self, index: PythonCodeIndex) -> None:
        result = StructuredRetriever(index).retrieve("total=")
        assert result.stage == "text"

    def test_an_identifier_answers_at_the_symbol_stage(self, index: PythonCodeIndex) -> None:
        """Text search on a name returns the definition *and* every mention."""
        result = StructuredRetriever(index).retrieve("subtract")
        assert result.stage == "symbol"
        assert len(result.hits) == 1
        assert result.hits[0].lines is not None and result.hits[0].lines.start == 11

    def test_every_stage_is_recorded_when_nothing_answers(self, index: PythonCodeIndex) -> None:
        """'Found nothing' has to be diagnosable, not a shrug."""
        result = StructuredRetriever(index).retrieve("qqzzx_no_such_thing")
        assert result.found is False
        assert result.attempted == STAGES
        assert "Stages tried" in result.render()

    def test_a_stale_index_says_so_in_the_result(self, index: PythonCodeIndex, repo: Path) -> None:
        (repo / "calc.py").write_text(CALC + "\nX = 1\n", encoding="utf-8")
        result = StructuredRetriever(index).retrieve("calc.py")
        assert "stale" in result.degraded
        assert "stale" in result.render()

    def test_semantic_search_runs_last_and_only_last(self, index: PythonCodeIndex) -> None:
        class Spy:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def search(self, query: str, *, limit: int = 10) -> tuple[SearchHit, ...]:
                self.queries.append(query)
                return (
                    SearchHit(path="calc.py", excerpt="guess", score=0.4, provenance="semantic"),
                )

        spy = Spy()
        retriever = StructuredRetriever(index, semantic=spy)

        assert retriever.retrieve("subtract").stage == "symbol"
        assert spy.queries == []  # a structural answer existed; no guessing needed

        assert retriever.retrieve("qqzzx_no_such_thing").stage == "semantic"
        assert spy.queries == ["qqzzx_no_such_thing"]

    def test_a_broken_semantic_backend_degrades_to_no_hits(self, index: PythonCodeIndex) -> None:
        """A fallback that can fail the task is not a fallback."""

        class Broken:
            def search(self, query: str, *, limit: int = 10) -> tuple[SearchHit, ...]:
                raise RuntimeError("embedding service is down")

        result = StructuredRetriever(index, semantic=Broken()).retrieve("qqzzx_no_such_thing")
        assert result.found is False


class TestRelatedFilesForRepair:
    def test_it_widens_a_change_to_its_callers_and_tests(self, index: PythonCodeIndex) -> None:
        related = related_files_for(index, ["calc.py"])
        assert "calc.py" in related
        assert "report.py" in related
        assert "tests/test_calc.py" in related

    def test_an_isolated_file_stays_isolated(self, index: PythonCodeIndex) -> None:
        assert related_files_for(index, ["unrelated.py"]) == ("unrelated.py",)


class TestProtocolConformance:
    def test_the_index_satisfies_the_code_index_protocol(self, index: PythonCodeIndex) -> None:
        """Callers depend on the protocol, so this is what keeps them honest."""
        from shamsu.interfaces.code_intelligence import CodeIndex

        assert isinstance(index, CodeIndex)
