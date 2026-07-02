from __future__ import annotations

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import (
    create_generation_state,
    load_generation_state,
    mark_step_done,
    mark_step_failed,
    mark_step_running,
    reset_generation_state,
    save_generation_state,
    state_path,
)
from shamsu.types import TaskStepStatus


def _project_spec():
    parsed = parse_prd_text(
        "# Todo App\n\n## Entities\n- Task: title (text), done (boolean)\n"
    )
    return build_project_spec(parsed)


def test_fresh_generation_state_has_deterministic_pending_steps(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")

    state = create_generation_state(_project_spec(), prd, tmp_path, accepted=True)

    assert state.accepted is True
    assert state.project_name == "todo_app"
    assert state.generation_order[0].file.path == "manage.py"
    assert state.next_pending().id == 1
    assert all(step.status == TaskStepStatus.PENDING for step in state.generation_order)


def test_marking_steps_done_advances_next_pending(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    state = create_generation_state(_project_spec(), prd, tmp_path)

    first = state.next_pending()
    mark_step_running(state, first.id)
    mark_step_done(state, first.id)

    assert first.file.path in state.completed_files
    assert state.next_pending().id != first.id


def test_interrupted_state_can_be_saved_loaded_and_resumed(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    state = create_generation_state(_project_spec(), prd, tmp_path)
    mark_step_done(state, 1)

    save_generation_state(state, tmp_path)
    loaded = load_generation_state(tmp_path)

    assert loaded.completed_files == ["manage.py"]
    assert loaded.next_pending().id == 2


def test_failed_step_records_error_without_losing_completed_steps(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    state = create_generation_state(_project_spec(), prd, tmp_path)
    mark_step_done(state, 1)
    mark_step_failed(state, 2, "template failed")

    assert state.completed_files == ["manage.py"]
    assert state.last_error == "template failed"
    assert state.generation_order[1].status == TaskStepStatus.FAILED


def test_reset_generation_state_removes_workspace_state_file(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    save_generation_state(create_generation_state(_project_spec(), prd, tmp_path), tmp_path)

    reset_generation_state(tmp_path)

    assert not state_path(tmp_path).exists()
