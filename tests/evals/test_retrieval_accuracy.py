"""Milestone 8's exit condition: retrieval selects useful code accurately.

A scored evaluation, not a unit test. The index runs against *this* repository,
where the answers are realistic and the failure modes are the ones that will
actually occur — name collisions across packages, tests that import a package
rather than a module, symbols that appear in a dozen files.

Ground truth is expressed as "this file must be in the top N", never as an
exact hit list. A retrieval that returns the right file plus two neighbours is
useful; one that returns the right file at rank 40 is not, and one that scores
perfectly only because the assertion was written around its output is worse
than no evaluation at all.

Thresholds are asserted so a regression fails CI rather than being noticed
later. They are deliberately below current measured performance, so ordinary
refactoring does not break the build — a *drop* does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from shamsu.code_intelligence.index import PythonCodeIndex
from shamsu.code_intelligence.retrieval import StructuredRetriever
from shamsu.interfaces.code_intelligence import SearchHit

pytestmark = pytest.mark.eval

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Case:
    """One retrieval question with a known-good answer."""

    query: str
    expects: str
    within: int = 3
    note: str = ""


#: Queries a planner or a repair actually issues, by shape.
CASES: tuple[Case, ...] = (
    # Identifier queries — the most common shape.
    Case("check_completion", "src/shamsu/verification/evidence.py", within=1),
    Case("PathSandbox", "src/shamsu/security/paths.py", within=1),
    Case("RepairScope", "src/shamsu/agent/repair.py", within=1),
    Case("digest_test_output", "src/shamsu/verification/digest.py", within=1),
    Case("build_capsule", "src/shamsu/verification/failure.py", within=1),
    Case("materialise", "src/shamsu/agent/planning.py", within=1),
    Case("TRANSITIONS", "src/shamsu/state/transitions.py", within=1),
    Case("EvidenceRecorder", "src/shamsu/verification/evidence.py", within=1),
    # A name defined in two places: the protocol and the implementation. Both
    # are correct answers; the evaluation only requires the concrete one to be
    # reachable near the top.
    Case(
        "ToolGateway",
        "src/shamsu/tools/gateway.py",
        within=2,
        note="also defined as a Protocol in interfaces/tools.py",
    ),
    Case(
        "Tool",
        "src/shamsu/tools/base.py",
        within=2,
        note="collides with the Tool protocol",
    ),
    # Qualified names must narrow rather than return every same-named method.
    Case("CompletionGate.check_task", "src/shamsu/verification/completion.py", within=1),
    Case("Planner.replan", "src/shamsu/agent/planning.py", within=1),
    # Path queries.
    Case("src/shamsu/state/store.py", "src/shamsu/state/store.py", within=1),
    Case("store.py", "src/shamsu/state/store.py", within=2),
    # Literal queries — the shape text search exists for.
    Case("required_evidence ⊆ verified_evidence", "src/shamsu/verification/evidence.py", within=3),
    Case("PYTHONPYCACHEPREFIX", "src/shamsu/tools/testing.py", within=2),
)

#: Files whose tests the index must be able to name. Structural, so it stays
#: true as tests are added.
TEST_CASES: tuple[tuple[str, str], ...] = (
    ("src/shamsu/agent/repair.py", "tests/unit/test_repair.py"),
    ("src/shamsu/agent/planning.py", "tests/unit/test_planning.py"),
    ("src/shamsu/verification/completion.py", "tests/unit/test_completion.py"),
    ("src/shamsu/verification/digest.py", "tests/unit/test_digest.py"),
    ("src/shamsu/security/paths.py", "tests/adversarial/test_path_sandbox.py"),
    ("src/shamsu/state/store.py", "tests/unit/test_state_store.py"),
)


@pytest.fixture(scope="module")
def index() -> PythonCodeIndex:
    return PythonCodeIndex(REPO).build()


@pytest.fixture(scope="module")
def retriever(index: PythonCodeIndex) -> StructuredRetriever:
    return StructuredRetriever(index)


class TestRetrievalAccuracy:
    def test_the_index_covers_the_repository(self, index: PythonCodeIndex) -> None:
        assert len(index.indexed_files) > 50
        assert index.parse_failures == (), "every source file must parse"

    @pytest.mark.parametrize("case", CASES, ids=lambda case: case.query[:40])
    def test_a_query_finds_its_file(self, retriever: StructuredRetriever, case: Case) -> None:
        result = retriever.retrieve(case.query)
        assert result.found, f"{case.query!r} returned nothing (stage {result.attempted})"

        paths = _ranked_paths(result.hits)
        assert case.expects in paths[: case.within], (
            f"{case.query!r} → expected {case.expects} within top {case.within}, "
            f"got {paths[: case.within + 2]} via {result.stage}"
        )

    def test_overall_precision_at_one(self, retriever: StructuredRetriever) -> None:
        """The metric that matters when only one file fits the budget."""
        correct = sum(
            1
            for case in CASES
            if (paths := _ranked_paths(retriever.retrieve(case.query).hits))
            and paths[0] == case.expects
        )
        precision = correct / len(CASES)
        assert precision >= 0.75, f"precision@1 fell to {precision:.0%} ({correct}/{len(CASES)})"

    @pytest.mark.parametrize(("source", "test"), TEST_CASES)
    def test_related_tests_are_found(self, index: PythonCodeIndex, source: str, test: str) -> None:
        assert test in index.related_tests(source)

    def test_related_tests_stay_selective(self, index: PythonCodeIndex) -> None:
        """Recall is easy; a rule that relates every test to every file is useless."""
        every_test = [path for path in index.indexed_files if path.startswith("tests/")]
        related = index.related_tests("src/shamsu/security/paths.py")
        assert 0 < len(related) < len(every_test) / 2


class TestImpactAccuracy:
    def test_a_widely_used_symbol_reports_real_callers(self, index: PythonCodeIndex) -> None:
        symbol = index.lookup_symbol("shamsu.verification.evidence.check_completion")[0]
        report = index.impact(symbol)

        callers = {reference.path for reference in report.direct_callers}
        assert "src/shamsu/agent/planning.py" in callers
        assert "src/shamsu/verification/completion.py" in callers

    def test_impact_bounds_are_reported_not_hidden(self, index: PythonCodeIndex) -> None:
        """A truncated report is not proof that nothing else is affected."""
        symbol = index.lookup_symbol("shamsu.state.store.StateStore")[0]
        report = index.impact(symbol)
        assert report.transitive_modules or report.truncated is False

    def test_a_private_helper_has_a_small_blast_radius(self, index: PythonCodeIndex) -> None:
        """Impact has to discriminate, or it is just 'the whole repository'."""
        narrow = index.impact(index.lookup_symbol("shamsu.agent.repair.looks_like_a_test")[0])
        assert len(narrow.transitive_modules) <= 5


def _ranked_paths(hits: Sequence[SearchHit]) -> list[str]:
    """Distinct paths in rank order, excluding this file.

    The exclusion is not cosmetic. Every literal in `CASES` is written *in this
    file*, so the index finds it here too — the evaluation would be scoring
    itself, and a case could pass purely because its own query string is
    nearby. Dropping this path makes the score reflect the repository.
    """
    here = "tests/evals/test_retrieval_accuracy.py"
    ordered: list[str] = []
    for hit in hits:
        if hit.path == here or hit.path in ordered:
            continue
        ordered.append(hit.path)
    return ordered
