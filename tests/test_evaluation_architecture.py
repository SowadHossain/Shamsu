from __future__ import annotations

from pathlib import Path

from shamsu.evaluation import (
    ARCHITECTURE_METRIC_NAMES,
    ADVERSARIAL_TASK_SPECS,
    CORE_CODING_WORKFLOW_STAGES,
    INITIAL_TASK_SPECS,
    ArchitectureTaskSample,
    BenchmarkCategory,
    BenchmarkTaskSpec,
    MetricName,
    RetrievalJudgement,
    WorkspaceCheck,
    WorkspaceCheckKind,
    evaluate_advanced_readiness,
    evaluate_architecture_samples,
    evaluate_task_sample,
)
from shamsu.runtime.failures import FailureType
from shamsu.runtime.task_state import (
    EvidenceRecord,
    EvidenceStatus,
    EvidenceType,
    RuntimeStateStore,
)
from shamsu.types import RunStatus


def _store(tmp_path: Path) -> RuntimeStateStore:
    return RuntimeStateStore(tmp_path / ".shamsu")


def _make_running_task(store: RuntimeStateStore, task_id: str = "task-a"):
    task = store.create_task(run_id=f"run-{task_id}", task_id=task_id, user_request="work")
    task.status = RunStatus.RUNNING
    return store.save_task(task, checkpoint_kind="started")


def _force_completed(store: RuntimeStateStore, task_id: str) -> None:
    task = store.require_task(task_id)
    task.status = RunStatus.COMPLETED
    task.current_phase = "completed"
    store.save_task(task, checkpoint_kind="forced_for_eval", allow_completion=True)


def _record_evidence(
    store: RuntimeStateStore,
    task_id: str,
    evidence_type: EvidenceType,
    status: EvidenceStatus = EvidenceStatus.PASSED,
) -> None:
    store.record_evidence(
        EvidenceRecord(
            evidence_id=f"ev-{task_id}-{evidence_type.value}-{status.value}",
            task_id=task_id,
            step_id="",
            evidence_type=evidence_type,
            source_tool="test.run",
            status=status,
            related_command="pytest",
            exit_code=0 if status == EvidenceStatus.PASSED else 1,
        )
    )


def test_architecture_suite_names_are_repeatable():
    initial = {spec.task_id for spec in INITIAL_TASK_SPECS}
    adversarial = {spec.task_id for spec in ADVERSARIAL_TASK_SPECS}

    assert initial == {
        "edit_documentation",
        "fix_one_simple_bug",
        "add_one_unit_test",
        "fix_one_failing_test",
        "implement_small_validation_rule",
        "implement_small_multi_file_feature",
    }
    assert adversarial == {
        "stale_artifact",
        "huge_tool_result",
        "malicious_instruction_in_repository_text",
        "irrelevant_failing_test",
        "repeated_repair_failure",
        "malformed_tool_arguments",
        "attempted_path_escape",
        "model_claiming_success_without_verification",
    }
    assert tuple(ARCHITECTURE_METRIC_NAMES) == tuple(metric.value for metric in MetricName)


def test_scores_success_from_workspace_and_evidence_not_final_answer_metadata(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    (tmp_path / "README.md").write_text("patched docs\n", encoding="utf-8")
    _record_evidence(store, "task-a", EvidenceType.FILE_CHANGED)
    _record_evidence(store, "task-a", EvidenceType.GIT_DIFF_REVIEWED)
    spec = BenchmarkTaskSpec(
        "doc",
        "Docs",
        BenchmarkCategory.INITIAL,
        required_evidence=(EvidenceType.FILE_CHANGED, EvidenceType.GIT_DIFF_REVIEWED),
        workspace_checks=(WorkspaceCheck(WorkspaceCheckKind.FILE_CONTAINS, "README.md", "patched"),),
    )

    result = evaluate_task_sample(
        ArchitectureTaskSample(
            "task-a",
            spec,
            tmp_path,
            store,
            metadata={"model_final_answer": "I did absolutely nothing."},
        )
    )

    assert result.verified_success is True
    assert result.false_success is False


def test_runtime_completion_without_required_evidence_is_false_success(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    (tmp_path / "app.py").write_text("def fixed():\n    return True\n", encoding="utf-8")
    spec = BenchmarkTaskSpec(
        "bugfix",
        "Bugfix",
        BenchmarkCategory.INITIAL,
        required_evidence=(EvidenceType.TEST_PASSED,),
        workspace_checks=(WorkspaceCheck(WorkspaceCheckKind.FILE_CONTAINS, "app.py", "fixed"),),
    )

    result = evaluate_task_sample(ArchitectureTaskSample("task-a", spec, tmp_path, store))

    assert result.verified_success is False
    assert result.false_success is True
    assert result.success_without_verification is True
    assert result.missing_evidence == (EvidenceType.TEST_PASSED.value,)


def test_failed_evidence_does_not_verify_success(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    _record_evidence(store, "task-a", EvidenceType.TEST_PASSED, EvidenceStatus.FAILED)
    spec = BenchmarkTaskSpec(
        "test",
        "Test",
        BenchmarkCategory.INITIAL,
        required_evidence=(EvidenceType.TEST_PASSED,),
    )

    result = evaluate_task_sample(ArchitectureTaskSample("task-a", spec, tmp_path, store))

    assert result.verified_success is False
    assert result.failed_evidence == (EvidenceType.TEST_PASSED.value,)


def test_evidence_from_another_task_is_ignored(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store, "task-a")
    _force_completed(store, "task-a")
    _make_running_task(store, "task-b")
    _record_evidence(store, "task-b", EvidenceType.TEST_PASSED)
    spec = BenchmarkTaskSpec(
        "isolated",
        "Isolated",
        BenchmarkCategory.INITIAL,
        required_evidence=(EvidenceType.TEST_PASSED,),
    )

    result = evaluate_task_sample(ArchitectureTaskSample("task-a", spec, tmp_path, store))

    assert result.verified_success is False
    assert result.missing_evidence == (EvidenceType.TEST_PASSED.value,)


def test_missing_runtime_state_is_reported_without_crashing(tmp_path: Path):
    store = _store(tmp_path)
    spec = BenchmarkTaskSpec("missing", "Missing", BenchmarkCategory.ADVERSARIAL)

    result = evaluate_task_sample(ArchitectureTaskSample("missing-task", spec, tmp_path, store))

    assert result.runtime_status == "missing"
    assert result.verified_success is False
    assert result.notes


def test_workspace_check_blocks_path_escape(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    spec = BenchmarkTaskSpec(
        "path_escape",
        "Path escape",
        BenchmarkCategory.ADVERSARIAL,
        workspace_checks=(WorkspaceCheck(WorkspaceCheckKind.FILE_MISSING, "../escaped.txt"),),
    )

    result = evaluate_task_sample(ArchitectureTaskSample("task-a", spec, tmp_path, store))

    assert result.verified_success is False
    assert "path escapes workspace" in result.notes[0]


def test_aggregate_metrics_include_failure_retrieval_freshness_and_tokens(tmp_path: Path):
    store = _store(tmp_path)
    task = _make_running_task(store, "task-good")
    task.action_count = 3
    store.save_task(task, checkpoint_kind="actions")
    _force_completed(store, "task-good")
    _record_evidence(store, "task-good", EvidenceType.TEST_PASSED)
    store.create_failure("task-good", FailureType.WRONG_TOOL, action="git.push")
    repeated = store.create_failure("task-good", FailureType.REPEATED_ACTION, action="file.read")
    store.record_failure(repeated)
    task = store.require_task("task-good")
    task.repair_count = 1
    store.save_task(task, checkpoint_kind="repair_count", allow_completion=True)

    good_spec = BenchmarkTaskSpec(
        "good",
        "Good",
        BenchmarkCategory.INITIAL,
        required_evidence=(EvidenceType.TEST_PASSED,),
    )
    report = evaluate_architecture_samples(
        [
            ArchitectureTaskSample(
                "task-good",
                good_spec,
                tmp_path,
                store,
                prompt_tokens=90,
                completion_tokens=10,
                retrieval_judgements=(
                    RetrievalJudgement(
                        "find symbol",
                        retrieved_items=4,
                        relevant_items=3,
                        stale_items_used=1,
                        artifact_freshness_errors=1,
                        artifact_items_checked=2,
                    ),
                ),
            )
        ]
    )

    metrics = report.metrics
    assert metrics[MetricName.VERIFIED_TASK_SUCCESS_RATE.value] == 1.0
    assert metrics[MetricName.REPAIR_SUCCESS_RATE.value] == 1.0
    assert metrics[MetricName.CONTEXT_RETRIEVAL_PRECISION.value] == 0.75
    assert metrics[MetricName.STALE_CONTEXT_USAGE_RATE.value] == 0.25
    assert metrics[MetricName.ARTIFACT_FRESHNESS_ERROR_RATE.value] == 0.5
    assert metrics[MetricName.TOKENS_PER_VERIFIED_TASK.value] == 100.0
    assert metrics[MetricName.WRONG_TOOL_RATE.value] > 0
    assert metrics[MetricName.REPEATED_ACTION_RATE.value] > 0


def test_report_json_shape_is_repeatable(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    spec = BenchmarkTaskSpec("empty", "Empty", BenchmarkCategory.INITIAL)

    report = evaluate_architecture_samples([ArchitectureTaskSample("task-a", spec, tmp_path, store)])
    data = report.to_dict()
    text = report.to_json()

    assert data["suite_version"] == "architecture-eval-v1"
    assert list(data["metrics"]) == list(ARCHITECTURE_METRIC_NAMES)
    assert '"verified_task_success_rate"' in text
    assert '"model_final_answer"' not in text


def test_advanced_readiness_blocks_when_core_stages_are_missing(tmp_path: Path):
    store = _store(tmp_path)
    _make_running_task(store)
    _force_completed(store, "task-a")
    report = evaluate_architecture_samples(
        [
            ArchitectureTaskSample(
                "task-a",
                BenchmarkTaskSpec("inspect", "Inspect", BenchmarkCategory.INITIAL),
                tmp_path,
                store,
            )
        ]
    )

    readiness = evaluate_advanced_readiness(report)

    assert readiness.ready is False
    assert "plan" in readiness.missing_stages
    assert readiness.enabled_capabilities == ()
    assert "docker" in readiness.blocked_capabilities


def test_advanced_readiness_opens_after_verified_core_workflow(tmp_path: Path):
    store = _store(tmp_path)
    samples: list[ArchitectureTaskSample] = []
    for stage in CORE_CODING_WORKFLOW_STAGES:
        task_id = f"task-{stage}"
        task = _make_running_task(store, task_id)
        task.action_count = 1
        store.save_task(task, checkpoint_kind="action")
        _force_completed(store, task_id)
        samples.append(
            ArchitectureTaskSample(
                task_id,
                BenchmarkTaskSpec(stage, stage.title(), BenchmarkCategory.INITIAL),
                tmp_path,
                store,
            )
        )
    report = evaluate_architecture_samples(samples)

    readiness = evaluate_advanced_readiness(report)

    assert readiness.ready is True
    assert readiness.blocked_capabilities == ()
    assert "documentation_retrieval" in readiness.enabled_capabilities
    assert "larger_project_autonomy" in readiness.enabled_capabilities
