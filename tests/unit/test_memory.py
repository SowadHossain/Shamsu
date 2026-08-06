"""Project memory: facts, decisions, lessons, confidence, staleness.

Milestone 9's exit condition has two halves and the second is the hard one:
memory must improve task success **without increasing stale-context errors**.
Most of these tests are about the second half — that nothing which stopped
being true is ever stated as though it still is.
"""

from __future__ import annotations

import pytest

from shamsu.interfaces.enums import DecisionStatus, FactKind, FactOrigin, Phase
from shamsu.interfaces.ids import DecisionId, ProjectId, RunId, TaskId, ToolEventId
from shamsu.memory import CONFIRM_STEP, TRUSTED, MemoryStore, combined_hash
from shamsu.memory.records import BASE_CONFIDENCE
from shamsu.state import (
    ProjectRecord,
    RunRecord,
    StateStore,
    TaskRecord,
    ToolEventRecord,
    new_id,
)


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def project(store: StateStore) -> ProjectRecord:
    return store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root="/workspace", name="demo")
    )


@pytest.fixture
def memory(store: StateStore, project: ProjectRecord) -> MemoryStore:
    return MemoryStore(store, project.project_id)


@pytest.fixture
def event(store: StateStore, project: ProjectRecord) -> ToolEventId:
    """A real tool event, so an OBSERVED fact has something behind it."""
    task = store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="inspect")
    )
    run = store.create_run(
        RunRecord(run_id=RunId(new_id()), project_id=project.project_id, task_id=task.task_id)
    )
    recorded = store.record_tool_event(
        ToolEventRecord(
            event_id=ToolEventId(new_id()),
            run_id=run.run_id,
            task_id=task.task_id,
            step_id=None,
            tool="file.read",
            phase=Phase.INSPECT,
            arguments_json="{}",
            ok=True,
            output="[tool.pytest.ini_options]",
        )
    )
    return recorded.event_id


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_origin_sets_the_starting_confidence(
        self, memory: MemoryStore, event: ToolEventId
    ) -> None:
        """A model's claim and a tool's observation must not weigh the same."""
        asserted = memory.learn(
            FactKind.CONVENTION, "style", "uses tabs", origin=FactOrigin.ASSERTED
        )
        observed = memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.OBSERVED,
            source_event_id=event,
        )
        user = memory.learn(
            FactKind.CONSTRAINT, "deploys", "never deploy to production", origin=FactOrigin.USER
        )

        assert asserted.confidence < observed.confidence < user.confidence
        assert user.confidence == BASE_CONFIDENCE[FactOrigin.USER]

    def test_a_model_cannot_declare_its_own_confidence(self, memory: MemoryStore) -> None:
        """There is no parameter for it, and that is the design."""
        import inspect

        assert "confidence" not in inspect.signature(memory.learn).parameters

    def test_an_observed_fact_needs_the_event_behind_it(self, memory: MemoryStore) -> None:
        """Otherwise 'observed' is an assertion wearing a better label."""
        with pytest.raises(ValueError, match="needs the tool event"):
            memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.OBSERVED)

    def test_confirmation_raises_confidence(self, memory: MemoryStore) -> None:
        first = memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.DERIVED)
        second = memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.DERIVED)

        assert second.confirmations == 1
        assert second.confidence == pytest.approx(first.confidence + CONFIRM_STEP)

    def test_confidence_is_capped(self, memory: MemoryStore) -> None:
        for _ in range(30):
            fact = memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.DERIVED)
        assert fact.confidence == 1.0


# ---------------------------------------------------------------------------
# Contradiction
# ---------------------------------------------------------------------------


class TestContradiction:
    def test_a_contradiction_lowers_confidence_and_is_counted(self, memory: MemoryStore) -> None:
        memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.DERIVED)
        updated = memory.learn(FactKind.STACK, "runner", "unittest", origin=FactOrigin.DERIVED)

        assert updated.contradictions == 1
        assert updated.confidence < BASE_CONFIDENCE[FactOrigin.DERIVED] + CONFIRM_STEP

    def test_a_stronger_origin_may_restate_a_fact(
        self, memory: MemoryStore, event: ToolEventId
    ) -> None:
        memory.learn(FactKind.STACK, "runner", "unittest", origin=FactOrigin.ASSERTED)
        updated = memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.OBSERVED,
            source_event_id=event,
        )
        assert updated.statement == "pytest"
        assert updated.origin is FactOrigin.OBSERVED

    def test_a_weaker_origin_may_not_overwrite_a_stronger_one(
        self, memory: MemoryStore, event: ToolEventId
    ) -> None:
        """A model asserting something must not overwrite what a tool observed."""
        memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.OBSERVED,
            source_event_id=event,
        )
        updated = memory.learn(
            FactKind.STACK, "runner", "definitely unittest", origin=FactOrigin.ASSERTED
        )

        assert updated.statement == "pytest"
        assert updated.contradictions == 1  # disagreement is still recorded
        assert updated.confidence < BASE_CONFIDENCE[FactOrigin.OBSERVED]

    def test_a_restated_fact_loses_the_old_claims_confirmations(self, memory: MemoryStore) -> None:
        """Those confirmations belonged to the statement that was replaced."""
        memory.learn(FactKind.STACK, "runner", "unittest", origin=FactOrigin.DERIVED)
        memory.learn(FactKind.STACK, "runner", "unittest", origin=FactOrigin.DERIVED)
        updated = memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.USER)

        assert updated.confirmations == 0

    def test_whitespace_and_case_do_not_make_a_contradiction(self, memory: MemoryStore) -> None:
        memory.learn(FactKind.STACK, "runner", "pytest -q", origin=FactOrigin.DERIVED)
        updated = memory.learn(FactKind.STACK, "runner", "PyTest  -q", origin=FactOrigin.DERIVED)
        assert updated.contradictions == 0

    def test_a_negation_is_a_contradiction_not_a_near_match(self, memory: MemoryStore) -> None:
        """A fuzzy comparison would call these similar at some threshold."""
        memory.learn(FactKind.STACK, "runner", "uses pytest", origin=FactOrigin.DERIVED)
        updated = memory.learn(
            FactKind.STACK, "runner", "does not use pytest", origin=FactOrigin.DERIVED
        )
        assert updated.contradictions == 1


# ---------------------------------------------------------------------------
# Staleness and invalidation
# ---------------------------------------------------------------------------


class TestStaleness:
    def _learn_from(self, memory: MemoryStore, hashes: dict[str, str]) -> None:
        memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.DERIVED,
            evidence_paths=("pyproject.toml",),
            evidence_hash=combined_hash(("pyproject.toml",), hashes),
        )

    def test_unchanged_evidence_keeps_a_fact_verified(self, memory: MemoryStore) -> None:
        hashes = {"pyproject.toml": "abc"}
        self._learn_from(memory, hashes)

        assert memory.revalidate(hashes) == ()
        assert memory.facts()[0].verified is True

    def test_changed_evidence_marks_a_fact_unverified(self, memory: MemoryStore) -> None:
        self._learn_from(memory, {"pyproject.toml": "abc"})

        invalidated = memory.revalidate({"pyproject.toml": "def"})

        assert len(invalidated) == 1
        assert memory.facts()[0].verified is False

    def test_a_deleted_file_invalidates_too(self, memory: MemoryStore) -> None:
        """Skipping a missing path would make deletion the one change memory misses."""
        self._learn_from(memory, {"pyproject.toml": "abc"})
        assert len(memory.revalidate({})) == 1

    def test_an_unverified_fact_is_kept_not_deleted(self, memory: MemoryStore) -> None:
        """It is probably still true; it just has not been rechecked."""
        self._learn_from(memory, {"pyproject.toml": "abc"})
        memory.revalidate({"pyproject.toml": "def"})

        assert len(memory.facts()) == 1
        assert memory.facts()[0].statement == "pytest"

    def test_relearning_restores_verification(self, memory: MemoryStore) -> None:
        self._learn_from(memory, {"pyproject.toml": "abc"})
        memory.revalidate({"pyproject.toml": "def"})

        self._learn_from(memory, {"pyproject.toml": "def"})
        assert memory.facts()[0].verified is True

    def test_a_fact_with_no_evidence_paths_is_never_invalidated(self, memory: MemoryStore) -> None:
        """A user-stated constraint does not expire because a file changed."""
        memory.learn(
            FactKind.CONSTRAINT, "deploys", "never deploy on Friday", origin=FactOrigin.USER
        )
        assert memory.revalidate({"anything.py": "changed"}) == ()
        assert memory.facts()[0].verified is True


# ---------------------------------------------------------------------------
# Recall — the stale-context half of the exit condition
# ---------------------------------------------------------------------------


class TestRecall:
    def test_a_stale_fact_is_labelled_in_the_frame(self, memory: MemoryStore) -> None:
        """Invariant 4: stale context reaches the model only with a label."""
        memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.USER,
            evidence_paths=("pyproject.toml",),
            evidence_hash=combined_hash(("pyproject.toml",), {"pyproject.toml": "abc"}),
        )
        memory.revalidate({"pyproject.toml": "changed"})

        recalled = memory.recall()
        assert "UNVERIFIED" in recalled
        assert "pytest" in recalled

    def test_a_low_confidence_fact_is_labelled_too(self, memory: MemoryStore) -> None:
        memory.learn(FactKind.CONVENTION, "style", "uses tabs", origin=FactOrigin.ASSERTED)
        assert "low confidence" in memory.recall()

    def test_a_trusted_fact_is_stated_plainly(self, memory: MemoryStore) -> None:
        memory.learn(
            FactKind.CONSTRAINT, "deploys", "never deploy on Friday", origin=FactOrigin.USER
        )
        recalled = memory.recall()
        assert recalled == "deploys: never deploy on Friday"

    def test_stale_facts_can_be_excluded_entirely(self, memory: MemoryStore) -> None:
        memory.learn(
            FactKind.STACK,
            "runner",
            "pytest",
            origin=FactOrigin.USER,
            evidence_paths=("a.py",),
            evidence_hash=combined_hash(("a.py",), {"a.py": "1"}),
        )
        memory.revalidate({"a.py": "2"})
        assert memory.recall(include_unverified=False) == ""

    def test_recall_is_budgeted(self, memory: MemoryStore) -> None:
        """Memory competes for the same frame budget as source code."""
        for index in range(20):
            memory.learn(FactKind.CONVENTION, f"topic-{index}", "something", origin=FactOrigin.USER)
        assert len(memory.recall(limit=5).splitlines()) == 5

    def test_a_verified_fact_outranks_a_stale_one_that_scored_higher(
        self, memory: MemoryStore
    ) -> None:
        memory.learn(
            FactKind.CONSTRAINT,
            "old",
            "high confidence but stale",
            origin=FactOrigin.USER,
            evidence_paths=("a.py",),
            evidence_hash=combined_hash(("a.py",), {"a.py": "1"}),
        )
        memory.revalidate({"a.py": "2"})
        memory.learn(FactKind.STACK, "new", "current and derived", origin=FactOrigin.DERIVED)

        assert memory.recall(limit=1).startswith("new:")

    def test_recall_is_empty_when_nothing_is_known(self, memory: MemoryStore) -> None:
        """An empty string, not a header with nothing under it."""
        assert memory.recall() == ""

    def test_the_label_appears_exactly_below_the_trusted_threshold(
        self, memory: MemoryStore
    ) -> None:
        """`TRUSTED` has to be the one boundary, or the label drifts from the flag."""
        fact = memory.learn(FactKind.CONVENTION, "style", "uses tabs", origin=FactOrigin.ASSERTED)
        assert fact.confidence < TRUSTED
        assert fact.trusted is False
        assert "low confidence" in fact.render()

        # Confirm it up past the threshold.
        while fact.confidence < TRUSTED:
            fact = memory.learn(
                FactKind.CONVENTION, "style", "uses tabs", origin=FactOrigin.ASSERTED
            )

        assert fact.trusted is True
        assert "low confidence" not in fact.render()


# ---------------------------------------------------------------------------
# Architecture decisions
# ---------------------------------------------------------------------------


class TestDecisions:
    def test_a_decision_is_recorded_and_readable(self, memory: MemoryStore) -> None:
        record = memory.record_decision(
            "SQLite is authoritative",
            "Runtime state lives in SQLite; artifacts are derived.",
            consequences=("Artifacts can be regenerated at any time.",),
            related_paths=("src/shamsu/state/store.py",),
        )
        assert record.status is DecisionStatus.ACCEPTED
        assert "SQLite is authoritative" in record.render()

    def test_superseding_keeps_both(self, memory: MemoryStore) -> None:
        """'What did we decide before?' is the question an ADR exists to answer."""
        first = memory.record_decision("Use Graphiti", "Graphiti holds project memory.")
        second = memory.record_decision(
            "Drop Graphiti",
            "Memory is four lightweight layers in SQLite.",
            supersedes=first.decision_id,
        )

        stored = memory.decision(first.decision_id)
        assert stored is not None and stored.status is DecisionStatus.SUPERSEDED
        assert second.supersedes == first.decision_id
        assert len(memory.decisions()) == 2

    def test_a_dangling_supersedes_is_refused(self, memory: MemoryStore) -> None:
        with pytest.raises(KeyError, match="to supersede"):
            memory.record_decision("x", "y", supersedes=DecisionId("nope"))

    def test_decisions_are_retrievable_by_the_files_they_touch(self, memory: MemoryStore) -> None:
        """The retrieval that makes ADRs useful during a task."""
        memory.record_decision(
            "SQLite is authoritative",
            "Runtime state lives in SQLite.",
            related_paths=("src/shamsu/state/store.py",),
        )
        memory.record_decision(
            "No cloud inference", "Inference is local.", related_paths=("src/shamsu/models/",)
        )

        found = memory.decisions_for_paths(["src/shamsu/state/store.py"])
        assert [record.title for record in found] == ["SQLite is authoritative"]

    def test_a_superseded_decision_is_not_offered_as_current(self, memory: MemoryStore) -> None:
        first = memory.record_decision(
            "Use Graphiti", "Graphiti holds memory.", related_paths=("src/shamsu/memory/",)
        )
        memory.record_decision(
            "Drop Graphiti",
            "SQLite holds memory.",
            related_paths=("src/shamsu/memory/",),
            supersedes=first.decision_id,
        )

        titles = [record.title for record in memory.decisions_for_paths(["src/shamsu/memory/"])]
        assert titles == ["Drop Graphiti"]

    def test_a_decision_does_not_go_stale_when_files_change(self, memory: MemoryStore) -> None:
        """A decision that was made stays made, whatever happened to the code."""
        memory.record_decision(
            "SQLite is authoritative",
            "Runtime state lives in SQLite.",
            related_paths=("src/shamsu/state/store.py",),
        )
        memory.revalidate({"src/shamsu/state/store.py": "rewritten"})

        assert memory.decisions()[0].status is DecisionStatus.ACCEPTED


# ---------------------------------------------------------------------------
# Failure lessons
# ---------------------------------------------------------------------------


class TestLessons:
    def test_a_lesson_is_recalled_by_error_signature(self, memory: MemoryStore) -> None:
        memory.remember_failure(
            "sig-abc",
            "ModuleNotFoundError for 'requests'",
            resolution="Added requests to pyproject dependencies.",
        )
        found = memory.lesson("sig-abc")

        assert found is not None
        assert "Added requests" in found.render()

    def test_a_recurrence_increments_rather_than_duplicates(self, memory: MemoryStore) -> None:
        """'This has happened four times' has to be answerable."""
        for _ in range(3):
            memory.remember_failure("sig-abc", "the same failure")

        assert len(memory.lessons()) == 1
        assert memory.lessons()[0].occurrences == 3

    def test_a_resolution_can_arrive_later(self, memory: MemoryStore) -> None:
        """The failure is recorded before anyone knows what fixes it."""
        first = memory.remember_failure("sig-abc", "import error")
        assert first.resolution == ""

        updated = memory.remember_failure("sig-abc", "import error", resolution="add the dep")
        assert updated.resolution == "add the dep"
        assert updated.confidence > first.confidence

    def test_a_known_resolution_is_not_erased_by_a_bare_recurrence(
        self, memory: MemoryStore
    ) -> None:
        memory.remember_failure("sig-abc", "import error", resolution="add the dep")
        updated = memory.remember_failure("sig-abc", "import error")
        assert updated.resolution == "add the dep"

    def test_an_unseen_signature_recalls_nothing(self, memory: MemoryStore) -> None:
        assert memory.lesson("never-seen") is None

    def test_an_empty_signature_never_matches(self, memory: MemoryStore) -> None:
        """A passing run has an empty signature; it must not match every lesson."""
        memory.remember_failure("", "something without a signature")
        assert memory.lesson("") is None

    def test_related_paths_accumulate_across_occurrences(self, memory: MemoryStore) -> None:
        memory.remember_failure("sig", "flaky", related_paths=("a.py",))
        updated = memory.remember_failure("sig", "flaky", related_paths=("b.py",))
        assert set(updated.related_paths) == {"a.py", "b.py"}


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class TestProjectIsolation:
    def test_one_projects_memory_is_not_anothers(self, store: StateStore) -> None:
        """A convention learned in one repository must not leak into another."""
        first = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root="/a", name="a")
        )
        second = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root="/b", name="b")
        )

        MemoryStore(store, first.project_id).learn(
            FactKind.STACK, "runner", "pytest", origin=FactOrigin.USER
        )

        assert MemoryStore(store, second.project_id).facts() == []
        assert MemoryStore(store, second.project_id).recall() == ""


class TestMemoryInRepair:
    """The integration that makes memory improve task success (Milestone 9)."""

    FAILING = """\
tests/test_calc.py:6: in test_add
    assert add(2, 3) == 5
E   ModuleNotFoundError: No module named 'requests'
FAILED tests/test_calc.py::test_add
"""

    def _controller(self, store: StateStore, project: ProjectRecord, task: TaskRecord):
        from shamsu.agent.repair import RepairController

        return RepairController(store, task.task_id, memory=MemoryStore(store, project.project_id))

    @pytest.fixture
    def task(self, store: StateStore, project: ProjectRecord) -> TaskRecord:
        return store.create_task(
            TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="fix it")
        )

    def test_a_failure_is_remembered_across_tasks(
        self, store: StateStore, project: ProjectRecord, task: TaskRecord
    ) -> None:
        from shamsu.verification.digest import digest_test_output

        digest = digest_test_output(self.FAILING, "", exit_code=1)
        self._controller(store, project, task).consider(digest, related_files=("calc.py",))

        lesson = MemoryStore(store, project.project_id).lesson(digest.signature)
        assert lesson is not None
        assert "dependency_conflict" in lesson.statement

    def test_a_known_resolution_reaches_the_next_tasks_capsule(
        self, store: StateStore, project: ProjectRecord, task: TaskRecord
    ) -> None:
        """The whole point: the second task starts where the first finished."""
        from shamsu.verification.digest import digest_test_output

        digest = digest_test_output(self.FAILING, "", exit_code=1)
        memory = MemoryStore(store, project.project_id)
        memory.remember_failure(
            digest.signature,
            "missing dependency",
            resolution="add requests to pyproject dependencies",
        )

        decision = self._controller(store, project, task).consider(
            digest, related_files=("calc.py",)
        )

        assert decision.capsule is not None
        assert "add requests to pyproject" in decision.capsule.prior_lesson
        assert "From a previous task" in decision.capsule.render()

    def test_a_lesson_without_a_fix_is_not_carried_over(
        self, store: StateStore, project: ProjectRecord, task: TaskRecord
    ) -> None:
        """'This failed before' is noise; the capsule already says it is failing."""
        from shamsu.verification.digest import digest_test_output

        digest = digest_test_output(self.FAILING, "", exit_code=1)
        MemoryStore(store, project.project_id).remember_failure(
            digest.signature, "missing dependency"
        )

        decision = self._controller(store, project, task).consider(digest)
        assert decision.capsule is not None
        assert decision.capsule.prior_lesson == ""

    def test_repair_works_with_no_memory_at_all(self, store: StateStore, task: TaskRecord) -> None:
        """A project with no history must still be repairable."""
        from shamsu.agent.repair import RepairController
        from shamsu.verification.digest import digest_test_output

        decision = RepairController(store, task.task_id).consider(
            digest_test_output(self.FAILING, "", exit_code=1), related_files=("calc.py",)
        )
        assert decision.proceed is True
        assert decision.capsule is not None and decision.capsule.prior_lesson == ""
