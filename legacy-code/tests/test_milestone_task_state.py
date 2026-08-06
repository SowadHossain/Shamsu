from __future__ import annotations

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import create_generation_state
from shamsu.tasks.state import (
    advance_phase,
    create_task,
    generation_state_to_milestone_task,
    list_task_ids,
    load_task,
    mark_step_blocked,
    mark_step_done,
    mark_step_failed,
    mark_step_running,
    mark_step_skipped,
    record_command,
    record_file_created,
    record_file_edited,
    record_test_result,
    save_task,
)
from shamsu.types import TaskStep, TaskStepStatus, TestRunResult


def _steps(phase: str = "default") -> list[TaskStep]:
    return [
        TaskStep(id=1, description="write models", type="file_create", phase=phase),
        TaskStep(id=2, description="write views", type="file_create", depends_on=[1], phase=phase),
    ]


def test_fresh_task_has_deterministic_pending_steps():
    task = create_task("build a todo app", _steps())

    assert task.phase == "default"
    assert task.next_pending().id == 1
    assert all(step.status == TaskStepStatus.PENDING for step in task.steps)
    assert task.blocked_steps == []


def test_marking_steps_done_advances_next_pending():
    task = create_task("build a todo app", _steps())
    first = task.next_pending()
    mark_step_running(task, first.id)
    mark_step_done(task, first.id, result="wrote models.py")

    assert task.steps[0].status == TaskStepStatus.DONE
    assert task.steps[0].result == "wrote models.py"
    assert task.next_pending().id == 2


def test_task_can_be_saved_loaded_and_resumed(tmp_path):
    task = create_task("build a todo app", _steps())
    mark_step_done(task, 1)
    record_file_created(task, "models.py")

    save_task(task, tmp_path)
    loaded = load_task(tmp_path, task.task_id)

    assert loaded.steps[0].status == TaskStepStatus.DONE
    assert loaded.files_created == ["models.py"]
    assert loaded.next_pending().id == 2


def test_failed_step_records_error_without_losing_completed_steps():
    task = create_task("build a todo app", _steps())
    mark_step_done(task, 1)
    mark_step_failed(task, 2, "template failed")

    assert task.steps[0].status == TaskStepStatus.DONE
    assert task.steps[1].status == TaskStepStatus.FAILED
    assert task.steps[1].error == "template failed"
    assert "template failed" in task.next_action


def test_mark_step_blocked_is_distinct_from_failed():
    task = create_task("build a todo app", _steps())
    mark_step_blocked(task, 1, "need clarification on schema")

    assert task.steps[0].status == TaskStepStatus.BLOCKED
    assert task.blocked_steps == [task.steps[0]]
    assert "need clarification" in task.next_action


def test_mark_step_skipped_records_reason():
    task = create_task("build a todo app", _steps())
    mark_step_skipped(task, 2, "generator scheduled for later milestone")

    assert task.steps[1].status == TaskStepStatus.SKIPPED
    assert task.steps[1].error == "generator scheduled for later milestone"


def test_list_task_ids_returns_saved_tasks(tmp_path):
    first = create_task("first request", _steps())
    second = create_task("second request", _steps())
    save_task(first, tmp_path)
    save_task(second, tmp_path)

    ids = list_task_ids(tmp_path)

    assert sorted(ids) == sorted([first.task_id, second.task_id])


def test_record_command_and_file_edited_are_tracked():
    task = create_task("build a todo app", _steps())
    record_file_edited(task, "views.py")
    record_file_edited(task, "views.py")  # duplicate should not double up
    record_command(task, "pytest", 0)

    assert task.files_edited == ["views.py"]
    assert task.commands_executed[0]["command"] == "pytest"
    assert task.commands_executed[0]["exit_code"] == 0


def test_advance_phase_blocks_when_current_phase_steps_are_incomplete():
    task = create_task("build a todo app", _steps(phase="build"), phase="build")
    mark_step_done(task, 1)
    # step 2 still pending

    advanced = advance_phase(task, "test")

    assert advanced is False
    assert task.phase == "build"
    assert "not yet done" in task.next_action


def test_advance_phase_succeeds_when_current_phase_steps_are_done():
    task = create_task("build a todo app", _steps(phase="build"), phase="build")
    mark_step_done(task, 1)
    mark_step_done(task, 2)

    advanced = advance_phase(task, "test")

    assert advanced is True
    assert task.phase == "test"
    assert task.next_action == ""


def test_advance_phase_blocks_on_failing_tests_even_if_steps_are_done():
    task = create_task("build a todo app", _steps(phase="build"), phase="build")
    mark_step_done(task, 1)
    mark_step_done(task, 2)
    failing = TestRunResult(passed=3, failed=2)

    advanced = advance_phase(task, "release", test_result=failing)

    assert advanced is False
    assert task.phase == "build"
    assert "2 test(s) failing" in task.next_action
    assert task.test_results == [failing]


def test_advance_phase_only_considers_steps_in_the_current_phase():
    steps = _steps(phase="build") + [
        TaskStep(id=3, description="deploy", type="run_command", phase="release")
    ]
    task = create_task("build a todo app", steps, phase="build")
    mark_step_done(task, 1)
    mark_step_done(task, 2)
    # step 3 belongs to the "release" phase, not "build" — must not block advancing.

    advanced = advance_phase(task, "test")

    assert advanced is True
    assert task.phase == "test"


def test_advance_phase_with_mismatched_task_and_step_phases_finds_nothing_to_block():
    """Guardrail: if the caller forgets to align task.phase with its steps'
    phase, advance_phase sees zero steps in the (wrong) current phase and
    vacuously allows advancing — this is a caller bug, not something
    advance_phase can detect, so pin down the actual (permissive) behavior."""
    task = create_task("build a todo app", _steps(phase="build"))  # task.phase stays "default"
    mark_step_done(task, 1)
    # step 2 deliberately left pending

    advanced = advance_phase(task, "test")

    assert advanced is True


def test_record_test_result_appends_without_gating():
    task = create_task("build a todo app", _steps())
    passing = TestRunResult(passed=5, failed=0)
    record_test_result(task, passing)

    assert task.test_results == [passing]


def test_generation_state_to_milestone_task_projects_django_state(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    spec = build_project_spec(
        parse_prd_text("# Todo App\n\n## Entities\n- Task: title (text)\n")
    )
    generation_state = create_generation_state(spec, prd, tmp_path, accepted=True)

    milestone_task = generation_state_to_milestone_task(generation_state)

    assert milestone_task.task_id == generation_state.task_id
    assert milestone_task.phase == "generate"
    assert len(milestone_task.steps) == len(generation_state.generation_order)
    assert milestone_task.steps[0].target_file == generation_state.generation_order[0].file.path
