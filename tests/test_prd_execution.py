from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from shamsu.action_ledger import clear_current_run, set_current_run, start_run
from shamsu.cli import repl
from shamsu.prd.contract import extract_contract
from shamsu.prd.execution import (
    block_milestone,
    checkpoint_milestone,
    first_incomplete_milestone_index,
    initialize_prd_execution,
    load_milestone_preflight,
    milestone_lines_from_state,
    model_preflight_schema,
    record_milestone_preflight,
    record_milestone_repair,
    record_milestone_rollback,
    render_preflight_context,
    validate_model_preflight,
)
from shamsu.prd.parser import parse_prd_text
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.verify.gate import VerifyOutcome


def _contract():
    return extract_contract(
        parse_prd_text(
            "# Demo\n\n"
            "## Features\n- Search tasks\n\n"
            "## Acceptance\n- `npm test` exits 0.\n",
            markdown=True,
        )
    )


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=120)


def _transactional_write(workspace: Path, relative_path: str, content: str) -> str:
    transactions = TransactionWorkspace(workspace)
    transaction_id = transactions.begin(
        f"test write {relative_path}",
        [{"op": "write_file", "path": relative_path}],
        destructive=False,
    )
    transactions.backup_file(transaction_id, relative_path)
    target = workspace / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    transactions.record_after(transaction_id, relative_path)
    transactions.finalize(transaction_id, "applied")
    return transaction_id


def test_initialize_prd_execution_writes_state_preflights_and_artifacts(tmp_path: Path):
    root, state = initialize_prd_execution(
        tmp_path,
        "build from PRD",
        _contract(),
        prd_path="PRD.md",
    )

    assert root == next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    assert (root / "state.json").is_file()
    assert (root / "requirements.jsonl").is_file()
    assert (root / "milestones.json").is_file()
    assert (root / "acceptance-matrix.json").is_file()
    assert state["current_milestone_id"] == "M-002"
    preflight = load_milestone_preflight(root, "M-002")
    assert preflight["milestone_id"] == "M-002"
    assert preflight["requirement_ids"] == ["FEAT-001"]
    assert "developer" in preflight["active_skills"]


def test_prd_execution_without_acceptance_criteria_hard_stops(tmp_path: Path):
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n"
            "## Features\n"
            "- Search tasks\n",
            markdown=True,
        )
    )

    root, state = initialize_prd_execution(tmp_path, "build from PRD", contract)
    matrix = json.loads((root / "acceptance-matrix.json").read_text(encoding="utf-8"))

    assert matrix["criteria"] == []
    assert state["status"] == "blocked"
    assert state["current_milestone_id"] == ""
    assert state["blockers"][0]["kind"] == "missing_acceptance_criteria"


def test_prd_execution_isolated_by_target_project_root(tmp_path: Path):
    first_root, first_state = initialize_prd_execution(
        tmp_path,
        "build in first-app",
        _contract(),
        execution_key="first-app",
    )
    first_state = checkpoint_milestone(
        first_root,
        first_state,
        "M-002",
        changed_files=["first-app/src/App.tsx"],
        evidence=["changed:first-app/src/App.tsx"],
    )

    second_root, second_state = initialize_prd_execution(
        tmp_path,
        "build in second-app",
        _contract(),
        execution_key="second-app",
    )

    assert second_root != first_root
    assert first_incomplete_milestone_index(first_state) == 1
    assert first_incomplete_milestone_index(second_state) == 0
    assert second_state["execution_key"] == "second-app"


def test_resume_reopens_completed_checkpoint_when_architecture_files_are_missing(tmp_path: Path):
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n## Tech Stack\n- Django\n- SQLite\n\n"
            "## Data Model\nCourse\n- id, title\n",
            markdown=True,
        )
    )
    root, state = initialize_prd_execution(
        tmp_path,
        "build in demo",
        contract,
        execution_key="demo",
    )
    state = checkpoint_milestone(root, state, "M-001", status="verified")

    reopened = repl._reopen_invalid_prd_checkpoint(
        root, state, "demo", tmp_path, _console()
    )

    assert first_incomplete_milestone_index(reopened) == 0
    assert reopened["milestones"][0]["status"] == "pending"
    assert "Checkpoint revalidation failed" in reopened["milestones"][0]["last_message"]


def test_prd_execution_checkpoints_and_resumes_from_first_incomplete(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())

    state = checkpoint_milestone(
        root,
        state,
        "M-002",
        changed_files=["src/App.tsx"],
        evidence=["changed:src/App.tsx"],
    )
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))

    assert first_incomplete_milestone_index(reloaded) == 1
    assert reloaded["current_milestone_id"] == "M-004"
    assert reloaded["milestones"][0]["status"] == "implemented"
    assert reloaded["milestones"][0]["changed_files"] == ["src/App.tsx"]
    assert (root / "checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
    assert milestone_lines_from_state(reloaded)[1].startswith("M-004")


def test_prd_execution_records_milestone_verification(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())

    state = checkpoint_milestone(
        root,
        state,
        "M-002",
        changed_files=["app.py"],
        evidence=["changed:app.py", "verification:verified:python -m py_compile app.py"],
        status="verified",
        message="Verification passed.",
        verification={
            "status": "verified",
            "verified": True,
            "unverifiable": False,
            "exit_code": 0,
            "command": "python -m py_compile app.py",
            "files": ["app.py"],
            "summary": "Verification passed.",
        },
    )
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    verification_lines = (root / "verification.jsonl").read_text(encoding="utf-8").splitlines()

    assert state["milestones"][0]["last_verification"]["status"] == "verified"
    assert reloaded["verifications"][0]["command"] == "python -m py_compile app.py"
    assert json.loads(verification_lines[0])["files"] == ["app.py"]


def test_prd_milestone_failure_controls_run_outcome_until_recovered(tmp_path: Path):
    ledger = start_run(tmp_path, "build from PRD")
    set_current_run(ledger)
    try:
        repl._log_prd_milestone_verification(
            "M-102",
            repl._milestone_verification_payload(
                "failed",
                files=["backend/core/views.py"],
                summary="Login routes are not reachable.",
            ),
        )
        assert ledger.evidence_outcome() == "failed"

        repl._log_prd_milestone_verification(
            "M-102",
            repl._milestone_verification_payload(
                "verified",
                files=["backend/core/views.py", "backend/config/urls.py"],
                summary="Authentication behavior verified.",
                verified=True,
                exit_code=0,
                command="python manage.py test core.tests.test_auth",
                cwd="backend",
            ),
        )
        assert ledger.evidence_outcome() == "success"
    finally:
        clear_current_run()


def test_failed_checkpoint_keeps_execution_failed_and_resumable(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())

    state = checkpoint_milestone(
        root,
        state,
        "M-002",
        status="failed",
        message="Verification failed.",
    )

    assert state["status"] == "failed"
    assert state["current_milestone_id"] == "M-002"
    assert first_incomplete_milestone_index(state) == 0


def test_prd_execution_records_milestone_repair_attempt(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())

    state = record_milestone_repair(
        root,
        state,
        "M-002",
        attempt=1,
        phase="started",
        status="repairing",
        changed_files=["app.py"],
        verification={
            "status": "failed",
            "verified": False,
            "unverifiable": False,
            "exit_code": 1,
            "command": "python -m py_compile app.py",
            "files": ["app.py"],
            "summary": "Verification failed.",
        },
        message="Verification failed.",
    )
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    repair = json.loads((root / "repairs.jsonl").read_text(encoding="utf-8"))

    assert state["status"] == "running"
    assert reloaded["repairs"][0]["phase"] == "started"
    assert reloaded["milestones"][0]["status"] == "repairing"
    assert reloaded["milestones"][0]["last_repair"]["verification"]["status"] == "failed"
    assert repair["changed_files"] == ["app.py"]


def test_prd_execution_records_milestone_rollback(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    state["changed_files"] = ["src/App.tsx", "app.py"]

    state = record_milestone_rollback(
        root,
        state,
        "M-002",
        phase="finished",
        status="rolled_back",
        transaction_ids=["tx-1"],
        restored_files=["app.py"],
        policy="rollback changed files on failed verifier",
        message="Rolled back one transaction.",
        preserved_changed_files=["src/App.tsx"],
    )
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    rollback = json.loads((root / "rollbacks.jsonl").read_text(encoding="utf-8"))

    assert state["status"] == "running"
    assert state["changed_files"] == ["src/App.tsx"]
    assert reloaded["rollbacks"][0]["status"] == "rolled_back"
    assert reloaded["milestones"][0]["last_rollback"]["restored_files"] == ["app.py"]
    assert rollback["transaction_ids"] == ["tx-1"]


def test_prd_execution_blocker_is_durable(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())

    state = block_milestone(root, state, "M-002", "Need product owner decision.")

    assert state["status"] == "blocked"
    assert state["current_milestone_id"] == "M-002"
    assert state["blockers"][0]["reason"] == "Need product owner decision."
    assert (root / "blockers.jsonl").is_file()


def test_render_preflight_context_is_compact_and_grounded(tmp_path: Path):
    root, _state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    preflight = load_milestone_preflight(root, "M-002")
    preflight["implementation_steps"] = ["Create src/SearchPanel.tsx.", "Run the focused check."]

    rendered = render_preflight_context(preflight)

    assert "## Milestone Preflight" in rendered
    assert "Requirement IDs: FEAT-001" in rendered
    assert "Search tasks" in rendered
    assert "Implementation plan:" in rendered
    assert "1. Create src/SearchPanel.tsx." in rendered
    assert len(rendered) < 2000


def test_model_preflight_schema_declares_bounded_fields():
    schema = model_preflight_schema()

    assert schema["type"] == "object"
    assert "milestone_id" in schema["required"]
    assert "expected_files" in schema["properties"]
    assert "allowed_tools" in schema["properties"]
    assert "implementation_steps" in schema["properties"]


def test_validate_model_preflight_accepts_allowlisted_focus(tmp_path: Path):
    root, _state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    preflight, errors = validate_model_preflight(
        deterministic,
        {
            "milestone_id": "M-002",
            "requirement_ids": ["FEAT-001"],
            "active_skills": ["developer"],
            "expected_files": ["src/SearchPanel.tsx"],
            "allowed_tools": ["read_file", "write_file", "edit_file"],
            "verifier": "focused app tests/build",
            "context_focus": ["Search task workflow", "preserve existing App wiring"],
            "implementation_steps": ["Create the search panel.", "Run the focused check."],
            "risk_flags": ["do not replace previous milestone files"],
            "notes": "Keep the milestone small.",
        },
    )

    assert errors == []
    assert preflight["preflight_source"] == "model"
    assert preflight["requirement_ids"] == ["FEAT-001"]
    assert preflight["active_skills"] == ["developer"]
    assert "src/SearchPanel.tsx" in preflight["expected_files"]
    assert preflight["allowed_tools"] == ["read_file", "write_file", "edit_file"]
    assert preflight["context_focus"][0] == "Search task workflow"
    assert preflight["implementation_steps"] == ["Create the search panel.", "Run the focused check."]


def test_validate_model_preflight_rejects_ledger_or_tool_drift(tmp_path: Path):
    root, _state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    preflight, errors = validate_model_preflight(
        deterministic,
        {
            "milestone_id": "M-999",
            "requirement_ids": [],
            "active_skills": ["developer"],
            "expected_files": ["src/App.tsx; echo nope"],
            "allowed_tools": ["delete_file"],
            "verifier": "rm -rf",
        },
    )

    assert errors
    assert preflight["preflight_source"] == "deterministic_fallback"
    assert preflight["requirement_ids"] == deterministic["requirement_ids"]
    assert preflight["active_skills"] == deterministic["active_skills"]
    assert preflight["expected_files"] == deterministic["expected_files"]


def test_record_milestone_preflight_is_durable(tmp_path: Path):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    preflight = load_milestone_preflight(root, "M-002")
    preflight["preflight_source"] = "model"
    preflight["context_focus"] = ["Search task workflow"]
    preflight["implementation_steps"] = ["Create the search panel.", "Run the focused check."]

    state = record_milestone_preflight(root, state, "M-002", preflight)
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    decision = json.loads((root / "preflight-decisions.jsonl").read_text(encoding="utf-8"))

    assert state["preflight_decisions"][0]["accepted"] is True
    assert reloaded["preflight_decisions"][0]["source"] == "model"
    assert (root / "preflight" / "M-002.effective.json").is_file()
    assert decision["context_focus"] == ["Search task workflow"]
    assert decision["implementation_steps"] == ["Create the search panel.", "Run the focused check."]


def test_prd_milestone_verifier_marks_python_file_verified(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {"active_skills": ["developer"], "expected_files": ["app.py"], "verifier": "python"},
            ["app.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "verified"
    assert verification["status"] == "verified"
    assert verification["command"]
    assert verification["files"] == ["app.py"]


def test_prd_milestone_verifier_does_not_run_field_name_evidence_gate(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "backend/core").mkdir(parents=True)
    (tmp_path / "backend/core/models.py").write_text(
        "from django.db import models\n"
        "from django.contrib.auth.models import AbstractUser\n\n"
        "class User(AbstractUser):\n"
        "    role = models.CharField(max_length=20)\n",
        encoding="utf-8",
    )
    preflight = {
        "active_skills": ["django"],
        "expected_files": ["backend/core/models.py"],
        "verifier": "python manage.py check",
        "requirements": [
            {
                "id": "ENT-001",
                "kind": "entity",
                "scope": "in",
                "priority": "must",
                "text": "User: fields email, password_hash, role",
            }
        ],
    }

    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *args, **kwargs: VerifyOutcome(
            verified=True,
            exit_code=0,
            command="python manage.py check",
            summary="Django checks passed",
        ),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("field-name evidence gate should not run after verification")

    monkeypatch.setattr(repl, "_prd_requirement_evidence_errors", fail_if_called)

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            preflight,
            ["backend/core/models.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "verified"
    assert verification["status"] == "verified"
    assert verification["verified"] is True


def test_prd_milestone_verifier_records_empty_outcome_as_unverifiable(
    tmp_path: Path,
    monkeypatch,
):
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *args, **kwargs: VerifyOutcome(
            verified=False,
            unverifiable=False,
            exit_code=None,
            command="",
            summary="",
        ),
    )

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {"active_skills": ["developer"], "expected_files": ["app.py"], "verifier": "python"},
            ["app.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "implemented"
    assert verification["status"] == "unverifiable"
    assert verification["unverifiable"] is True
    assert verification["command"] == ""
    assert verification["exit_code"] is None


def test_prd_milestone_verifier_stops_on_python_failure(tmp_path: Path):
    (tmp_path / "app.py").write_text("def nope(:\n", encoding="utf-8")

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {"active_skills": ["developer"], "expected_files": ["app.py"], "verifier": "python"},
            ["app.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["status"] == "failed"
    assert verification["exit_code"] != 0


def test_incomplete_creation_is_kept_but_a_fatal_one_is_rolled_back():
    """Live 2026-08-01: a first-creation models.py missing one contract field
    was rolled back to NOTHING, so the milestone failed on "missing expected
    architecture files" instead of the real defect and burned the repair
    budget. A syntactically valid but incomplete CREATION must survive; a
    fatally broken one must not."""
    assert repl._prd_fatal_file_regression("missing entity contract:User.name") is False
    assert repl._prd_fatal_file_regression("invalid Python syntax") is True
    assert repl._prd_fatal_file_regression("empty") is True
    assert repl._prd_fatal_file_regression("no persisted model declarations") is True


def _live_manage_check_summary(workspace: Path) -> str:
    """The verifier summary shape seen live 2026-08-01: front-truncated, so the
    `Traceback (most recent call last):` header is gone, vendor frames first,
    the real frame and the exception last."""
    settings = workspace / "backend" / "config" / "settings.py"
    return (
        "Verification FAILED at required framework stage: `python manage.py check` (exit 1).\n"
        "Primary error:\n"
        'init__.py", line 442, in execute_from_command_line\n'
        "    utility.execute()\n"
        '  File "C:\\Python313\\Lib\\site-packages\\django\\conf\\__init__.py", line 76, in _setup\n'
        "    self._wrapped = Settings(settings_module)\n"
        f'  File "{settings}", line 50, in <module>\n'
        "    'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),\n"
        "NameError: name 'BASE_DIR' is not defined"
    )


def test_runtime_exception_diagnostic_survives_a_front_truncated_traceback(tmp_path: Path):
    settings = tmp_path / "backend" / "config" / "settings.py"
    settings.parent.mkdir(parents=True)
    settings.write_text("import os\n", encoding="utf-8")

    diagnostic = repl._prd_runtime_exception_diagnostic(
        {"summary": _live_manage_check_summary(tmp_path)}, tmp_path
    )

    assert diagnostic == (
        "backend/config/settings.py",
        50,
        "NameError",
        "name 'BASE_DIR' is not defined",
    )


def test_runtime_exception_guidance_forces_a_minimal_edit(tmp_path: Path):
    settings = tmp_path / "backend" / "config" / "settings.py"
    settings.parent.mkdir(parents=True)
    settings.write_text("import os\n", encoding="utf-8")
    verification = {"summary": _live_manage_check_summary(tmp_path)}

    assert repl._prd_runtime_exception_repair_files(verification, tmp_path) == [
        "backend/config/settings.py"
    ]
    guidance = repl._prd_runtime_exception_edit_guidance(verification, tmp_path)

    assert "backend/config/settings.py" in guidance
    assert "NameError: name 'BASE_DIR' is not defined" in guidance
    assert "call edit_file" in guidance
    assert "do NOT reprint" in guidance
    assert "`BASE_DIR` is used but never defined" in guidance


def test_runtime_exception_diagnostic_ignores_vendor_only_frames(tmp_path: Path):
    summary = (
        "Verification FAILED at required framework stage: `python manage.py check` (exit 1).\n"
        '  File "C:\\Python313\\Lib\\site-packages\\django\\conf\\__init__.py", line 76, in _setup\n'
        "ImportError: cannot import name 'x'"
    )

    assert repl._prd_runtime_exception_diagnostic({"summary": summary}, tmp_path) is None
    assert repl._prd_runtime_exception_edit_guidance({"summary": summary}, tmp_path) == ""


def test_runtime_exception_diagnostic_ignores_a_passing_verifier(tmp_path: Path):
    assert repl._prd_runtime_exception_diagnostic({"summary": "Verification passed."}, tmp_path) is None


def test_prd_milestone_acceptance_commands_promotes_declared_runner(tmp_path: Path):
    (tmp_path / "manage.py").write_text("print('ok')\n", encoding="utf-8")

    commands = repl._prd_milestone_acceptance_commands(
        {"verifier": "python manage.py check", "project_root": "."}, tmp_path
    )

    assert commands == ["python manage.py check"]


def test_prd_milestone_acceptance_commands_rejects_prose_and_shell(tmp_path: Path):
    assert repl._prd_milestone_acceptance_commands(
        {"verifier": "focused app tests/build"}, tmp_path
    ) == []
    assert repl._prd_milestone_acceptance_commands({"verifier": "rm -rf"}, tmp_path) == []
    assert repl._prd_milestone_acceptance_commands(
        {"verifier": "python manage.py check; rm -rf /"}, tmp_path
    ) == []
    assert repl._prd_milestone_acceptance_commands({"verifier": "python"}, tmp_path) == []


def test_prd_milestone_acceptance_commands_waits_for_the_entry_point(tmp_path: Path):
    """`python manage.py check` declared by an early milestone must not fail
    the run before manage.py exists - it activates once the file appears."""
    assert repl._prd_milestone_acceptance_commands(
        {"verifier": "python manage.py check", "project_root": "."}, tmp_path
    ) == []


def test_prd_milestone_verifier_runs_the_declared_acceptance_command(tmp_path: Path):
    """The 2026-08-01 dogfood gap: the declared verifier was only a stack hint,
    so a milestone whose command would have failed reported "no deterministic
    verifier" instead of running it."""
    (tmp_path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "fail.py").write_text("raise SystemExit(1)\n", encoding="utf-8")

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {
                "active_skills": ["developer"],
                "expected_files": ["app.py"],
                "verifier": "python fail.py",
                "project_root": ".",
            },
            ["app.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["exit_code"] != 0


def test_prd_milestone_verifier_blocks_mandatory_unverified_node_work(tmp_path: Path):
    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-002",
            {
                "active_skills": ["developer", "react-vite"],
                "expected_files": ["src/App.tsx", "package.json"],
                "verifier": "focused app tests/build",
                "requirements": [
                    {"id": "FEAT-001", "kind": "feature", "scope": "in", "priority": "must"}
                ],
            },
            ["src/App.tsx"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["status"] == "failed"
    assert "missing expected architecture files" in verification["summary"]


def test_prd_milestone_verifier_rejects_partial_framework_bootstrap(tmp_path: Path):
    (tmp_path / "demo" / "backend" / "core").mkdir(parents=True)
    (tmp_path / "demo" / "backend" / "core" / "models.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {
                "active_skills": ["developer"],
                "expected_files": [
                    "demo/backend/core/models.py",
                    "demo/backend/manage.py",
                    "demo/backend/config/settings.py",
                ],
                "requirements": [
                    {"id": "DATA-001", "kind": "entity", "scope": "in", "priority": "must"}
                ],
            },
            ["demo/backend/core/models.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["status"] == "failed"
    assert "demo/backend/manage.py" in verification["summary"]


def test_prd_milestone_verifier_rejects_empty_expected_source(tmp_path: Path):
    target = tmp_path / "demo" / "backend" / "core" / "models.py"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")

    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-001",
            {
                "active_skills": ["developer"],
                "expected_files": ["demo/backend/core/models.py"],
                "requirements": [
                    {"id": "DATA-001", "kind": "entity", "scope": "in", "priority": "must"}
                ],
            },
            ["demo/backend/core/models.py"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["status"] == "failed"
    assert "models.py (empty)" in verification["summary"]


def test_expected_architecture_rejects_wrong_django_settings_module(tmp_path: Path):
    backend = tmp_path / "demo/backend"
    (backend / "config").mkdir(parents=True)
    (backend / "config/settings.py").write_text(
        "SECRET_KEY='x'\nINSTALLED_APPS=[]\nDATABASES={}\n",
        encoding="utf-8",
    )
    (backend / "manage.py").write_text(
        "from django.core.management import execute_from_command_line\n"
        "import os\n"
        "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wrong.settings')\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": [
            "demo/backend/manage.py",
            "demo/backend/config/settings.py",
        ],
        "requirements": [
            {"id": "DATA-001", "kind": "entity", "scope": "in", "priority": "must"}
        ],
    }

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)

    assert "demo/backend/manage.py (wrong Django settings module)" in invalid


def test_expected_architecture_requires_exact_root_urlconf_module(tmp_path: Path):
    config = tmp_path / "demo/backend/config"
    config.mkdir(parents=True)
    (config / "urls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    (config / "settings.py").write_text(
        "SECRET_KEY='x'\nINSTALLED_APPS=[]\nDATABASES={}\n"
        "ROOT_URLCONF='fake_prefix.config.urls'\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": [
            "demo/backend/config/settings.py",
            "demo/backend/config/urls.py",
        ]
    }

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)

    assert "demo/backend/config/settings.py (wrong ROOT_URLCONF module)" in invalid


def test_manage_repair_guidance_names_exact_settings_module():
    guidance = repl._prd_file_repair_guidance(
        "demo/backend/manage.py",
        {
            "expected_files": [
                "demo/backend/manage.py",
                "demo/backend/config/settings.py",
            ]
        },
    )

    assert "exactly `config.settings`" in guidance
    assert "any other module name is invalid" in guidance


def test_settings_repair_guidance_removes_unexpected_wsgi_assignment():
    guidance = repl._prd_file_repair_guidance(
        "demo/backend/config/settings.py",
        {
            "expected_files": [
                "demo/backend/config/settings.py",
                "demo/backend/config/urls.py",
                "demo/backend/core/apps.py",
            ]
        },
    )

    assert "remove the WSGI_APPLICATION assignment entirely" in guidance


def test_settings_repair_guidance_includes_exact_edit_recipe(tmp_path: Path):
    target = tmp_path / "demo/backend/config/settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "ROOT_URLCONF = 'config.urls'\n"
        "WSGI_APPLICATION = 'missing.wsgi.application'\n",
        encoding="utf-8",
    )

    guidance = repl._prd_file_repair_guidance(
        "demo/backend/config/settings.py",
        {
            "expected_files": [
                "demo/backend/config/settings.py",
                "demo/backend/config/urls.py",
            ]
        },
        workspace=tmp_path,
    )

    assert '"name": "edit_file"' in guidance
    assert '"old_string": "WSGI_APPLICATION' in guidance
    assert '"new_string": ""' in guidance


def test_target_validation_errors_splits_combined_diagnostics():
    errors = repl._prd_target_validation_errors(
        "demo/settings.py",
        [
            "demo/settings.py (wrong ROOT_URLCONF module; WSGI module does not exist)",
            "demo/manage.py (wrong Django settings module)",
        ],
    )

    assert errors == {"wrong ROOT_URLCONF module", "WSGI module does not exist"}


def test_prd_verification_summary_includes_primary_stderr():
    outcome = SimpleNamespace(
        summary="Verification FAILED at framework stage.",
        steps=(
            SimpleNamespace(
                passed=False,
                stderr="ModuleNotFoundError: No module named 'core.urls'",
                stdout="",
            ),
        ),
    )

    summary = repl._prd_verification_summary(outcome)

    assert "Primary error" in summary
    assert "core.urls" in summary


def test_prd_migration_summary_prioritizes_actionable_stdout():
    outcome = SimpleNamespace(
        summary="Verification FAILED at migration stage.",
        steps=(
            SimpleNamespace(
                passed=False,
                step=SimpleNamespace(stage="migration"),
                stdout="Migrations for 'core':\n  core/migrations/0001_initial.py",
                stderr="WARNINGS:\nDEFAULT_AUTO_FIELD is not configured",
            ),
        ),
    )

    summary = repl._prd_verification_summary(outcome)

    assert summary.index("Migrations for 'core'") < summary.index("DEFAULT_AUTO_FIELD")


def test_prd_migration_failure_provides_exact_recovery_command():
    guidance = repl._prd_verifier_recovery_guidance(
        {
            "command": "python manage.py makemigrations --check --dry-run",
            "cwd": "demo/backend",
        }
    )

    assert "From `demo/backend`" in guidance
    assert "python manage.py makemigrations" in guidance
    assert "Do not edit settings" in guidance


def test_prd_migration_guidance_distinguishes_required_and_unexpected_fields():
    guidance = repl._prd_verifier_recovery_guidance(
        {
            "command": "python manage.py makemigrations --check --dry-run",
            "cwd": "demo/backend",
            "summary": (
                "Migrations for 'core':\n"
                "  + Add field name to assignment\n"
                "  + Add field name to user\n"
            ),
        },
        {
            "requirements": [
                {"kind": "entity", "text": "Assignment: fields title, due_date"},
                {"kind": "entity", "text": "User: fields name, email, password"},
            ]
        },
    )

    assert "out-of-contract model fields" in guidance
    assert "assignment.name" in guidance.lower()
    assert "migration-safe deterministic default" in guidance
    assert "user.name" in guidance.lower()


def test_prd_migration_unexpected_field_focuses_models_file(tmp_path: Path):
    models = tmp_path / "demo/backend/core/models.py"
    models.parent.mkdir(parents=True)
    models.write_text("class Assignment:\n    name = 1\n", encoding="utf-8")
    preflight = {
        "expected_files": ["demo/backend/core/models.py"],
        "requirements": [
            {"kind": "entity", "text": "Assignment: fields title, due_date"}
        ],
    }
    verification = {
        "command": "python manage.py makemigrations --check --dry-run",
        "summary": "+ Add field name to assignment",
    }

    assert repl._prd_migration_source_repair_files(
        verification, preflight, tmp_path
    ) == ["demo/backend/core/models.py"]


def test_prd_migration_source_recipes_remove_extra_and_default_required_field(tmp_path: Path):
    models = tmp_path / "demo/backend/core/models.py"
    models.parent.mkdir(parents=True)
    models.write_text(
        "from django.db import models\n"
        "class User(models.Model):\n"
        "    name = models.CharField(max_length=255)\n"
        "class Assignment(models.Model):\n"
        "    name = models.CharField(max_length=255)\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": ["demo/backend/core/models.py"],
        "requirements": [
            {"kind": "entity", "text": "User: fields name"},
            {"kind": "entity", "text": "Assignment: fields title"},
        ],
    }
    verification = {
        "summary": "+ Add field name to user\n+ Add field name to assignment"
    }

    recipes = repl._prd_migration_source_edit_recipes(
        verification, preflight, tmp_path
    )

    assert len(recipes) == 2
    assert "default=''" in recipes[0]["arguments"]["new_string"]
    assert "class Assignment" in recipes[1]["arguments"]["old_string"]
    assert "name =" not in recipes[1]["arguments"]["new_string"]


def test_prd_command_failure_keeps_actionable_stdout():
    verification = repl._prd_command_failure_verification(
        [
            {
                "message": "Command exited with 1.",
                "data": {
                    "resolved_command": "python manage.py makemigrations",
                    "exit_code": 1,
                    "stdout": "It is impossible to add a non-nullable field 'name' without a default.",
                    "stderr": "EOF when reading a line",
                    "diagnostics": "No structured diagnostics were extracted.",
                },
            }
        ]
    )

    assert "non-nullable field 'name'" in verification["summary"]
    assert "EOF when reading a line" in verification["summary"]


def test_prd_command_failure_becomes_stale_after_successful_edit():
    records = [
        {
            "phase": "called",
            "tool": "run_command",
            "tool_call_id": "test-1",
            "arguments": {"command": "python manage.py test"},
        },
        {
            "phase": "finished",
            "tool": "run_command",
            "tool_call_id": "test-1",
            "ok": False,
            "message": "ImportError",
        },
        {
            "phase": "finished",
            "tool": "edit_file",
            "tool_call_id": "edit-1",
            "ok": True,
        },
    ]

    assert repl._prd_unrecovered_command_failures_from_records(records) == []


def test_prd_latest_command_failure_after_edit_remains_actionable():
    records = [
        {
            "phase": "called",
            "tool": "run_command",
            "tool_call_id": "test-1",
            "arguments": {"command": "python manage.py test"},
        },
        {
            "phase": "finished",
            "tool": "run_command",
            "tool_call_id": "test-1",
            "ok": False,
        },
        {"phase": "finished", "tool": "edit_file", "tool_call_id": "edit-1", "ok": True},
        {
            "phase": "called",
            "tool": "run_command",
            "tool_call_id": "test-2",
            "arguments": {"command": "python manage.py test"},
        },
        {
            "phase": "finished",
            "tool": "run_command",
            "tool_call_id": "test-2",
            "ok": False,
            "message": "TypeError",
        },
    ]

    failures = repl._prd_unrecovered_command_failures_from_records(records)

    assert len(failures) == 1
    assert failures[0]["message"] == "TypeError"


def test_prd_verification_cwd_is_workspace_relative(tmp_path: Path):
    backend = tmp_path / "demo/backend"
    backend.mkdir(parents=True)
    step = SimpleNamespace(cwd=backend)
    outcome = SimpleNamespace(failed_step=step, steps=())

    assert repl._prd_verification_cwd(outcome, tmp_path) == "demo/backend"


def test_milestone_verifier_files_keep_expected_architecture_before_resume_change(tmp_path: Path):
    backend = tmp_path / "demo/backend"
    (backend / "config").mkdir(parents=True)
    (backend / "manage.py").write_text("print('backend')\n", encoding="utf-8")
    (backend / "config/settings.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "demo/manage.py").write_text("print('wrong root')\n", encoding="utf-8")

    files = repl._milestone_verifier_files(
        {
            "expected_files": [
                "demo/backend/manage.py",
                "demo/backend/config/settings.py",
            ]
        },
        ["demo/manage.py"],
        tmp_path,
    )

    assert files == [
        "demo/backend/manage.py",
        "demo/backend/config/settings.py",
        "demo/manage.py",
    ]


def test_repair_rejects_duplicate_manage_entrypoint_change(tmp_path: Path):
    duplicate = tmp_path / "demo/manage.py"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text("print('wrong root')\n", encoding="utf-8")

    invalid = repl._unexpected_prd_entrypoint_changes(
        {"expected_files": ["demo/backend/manage.py"]},
        ["demo/manage.py"],
        tmp_path,
    )

    assert invalid == [
        "demo/manage.py (unexpected duplicate Django entry point; expected demo/backend/manage.py)"
    ]


def test_expected_architecture_rejects_django_model_import_from_app_init(tmp_path: Path):
    app = tmp_path / "demo/backend/core"
    app.mkdir(parents=True)
    (app / "apps.py").write_text(
        "from django.apps import AppConfig\nclass CoreConfig(AppConfig):\n    name = 'core'\n",
        encoding="utf-8",
    )
    (app / "__init__.py").write_text("from .models import Course\n", encoding="utf-8")
    preflight = {
        "expected_files": [
            "demo/backend/core/apps.py",
            "demo/backend/core/__init__.py",
        ]
    }

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)
    guidance = repl._prd_file_repair_guidance(
        "demo/backend/core/__init__.py", preflight, workspace=tmp_path
    )

    assert "demo/backend/core/__init__.py (imports Django models during app loading)" in invalid
    assert '"name": "edit_file"' in guidance
    assert '"new_string": ""' in guidance


def test_expected_architecture_rejects_missing_included_url_module(tmp_path: Path):
    config = tmp_path / "demo/backend/config"
    config.mkdir(parents=True)
    (config / "urls.py").write_text(
        "from django.urls import include, path\n"
        "urlpatterns = [path('', include('core.urls'))]\n",
        encoding="utf-8",
    )
    preflight = {"expected_files": ["demo/backend/config/urls.py"]}

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)
    guidance = repl._prd_file_repair_guidance(
        "demo/backend/config/urls.py", preflight, workspace=tmp_path
    )

    assert "demo/backend/config/urls.py (included URL module core.urls does not exist)" in invalid
    assert "Remove those route entries" in guidance
    assert '"name": "edit_file"' in guidance


def test_expected_architecture_requires_custom_django_user_setting(tmp_path: Path):
    backend = tmp_path / "demo/backend"
    (backend / "config").mkdir(parents=True)
    (backend / "core").mkdir()
    (backend / "config/settings.py").write_text(
        "SECRET_KEY='x'\nINSTALLED_APPS=['core']\nDATABASES={}\n",
        encoding="utf-8",
    )
    (backend / "core/models.py").write_text(
        "from django.contrib.auth.models import AbstractUser\n"
        "class User(AbstractUser):\n    pass\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": [
            "demo/backend/config/settings.py",
            "demo/backend/core/models.py",
        ]
    }

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)
    guidance = repl._prd_file_repair_guidance(
        "demo/backend/config/settings.py", preflight, workspace=tmp_path
    )

    assert (
        "demo/backend/config/settings.py (custom Django user model is not configured)"
        in invalid
    )
    assert "AUTH_USER_MODEL must be `core.User`" in guidance
    assert '"name": "append_file"' in guidance
    assert "AUTH_USER_MODEL = 'core.User'" in guidance


def test_expected_architecture_does_not_require_exact_prd_django_entity_fields(tmp_path: Path):
    models = tmp_path / "demo/backend/core/models.py"
    models.parent.mkdir(parents=True)
    models.write_text(
        "from django.db import models\n"
        "class Submission(models.Model):\n"
        "    assignment = models.CharField(max_length=100)\n"
        "    student = models.CharField(max_length=100)\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": ["demo/backend/core/models.py"],
        "requirements": [
            {
                "id": "DATA-005",
                "kind": "entity",
                "scope": "in",
                "priority": "must",
                "text": "Submission: fields assignment, student, content, submitted_at",
            },
            {
                "id": "DATA-006",
                "kind": "entity",
                "scope": "in",
                "priority": "must",
                "text": "Grade: fields submission, score, feedback, graded_by",
            },
        ],
    }

    invalid = repl._invalid_expected_architecture_files(preflight, tmp_path)

    assert invalid == []


def test_django_entity_requirement_check_passes_complete_models():
    content = (
        "class Submission:\n"
        "    assignment = 1\n    student = 1\n    content = 1\n    submitted_at = 1\n"
        "class Grade:\n"
        "    submission = 1\n    score = 1\n    feedback = 1\n    graded_by = 1\n"
    )
    preflight = {
        "requirements": [
            {"kind": "entity", "text": "Submission: fields assignment, student, content, submitted_at"},
            {"kind": "entity", "text": "Grade: fields submission, score, feedback, graded_by"},
        ]
    }

    assert repl._missing_django_entity_requirements(content, preflight) == []


def test_django_entity_requirement_counts_abstract_user_inherited_fields():
    content = (
        "from django.contrib.auth.models import AbstractUser\n"
        "class User(AbstractUser):\n"
        "    name = 1\n    role = 1\n    created_at = 1\n"
    )
    preflight = {
        "requirements": [
            {
                "kind": "entity",
                "text": "User: fields name, email, password, role, created_at",
            }
        ]
    }

    assert repl._missing_django_entity_requirements(content, preflight) == []


def test_django_entity_requirement_rejects_annotation_only_field():
    content = (
        "from django.db import models\n"
        "class Submission(models.Model):\n"
        "    assignment = models.ForeignKey('Assignment', on_delete=models.CASCADE)\n"
        "    student: models.ForeignKey('User', on_delete=models.CASCADE)\n"
    )
    preflight = {
        "requirements": [
            {"kind": "entity", "text": "Submission: fields assignment, student"}
        ]
    }

    assert repl._missing_django_entity_requirements(content, preflight) == [
        "Submission.student"
    ]


def test_django_model_structure_detects_small_model_append_mistakes():
    content = (
        "from django.db import models\n"
        "class User(models.Model):\n"
        "    pass\n"
        "from django.contrib.auth.models import User\n"
        "class Submission(models.Model):\n"
        "    student: models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)\n"
    )

    errors = repl._django_model_structure_errors(content)

    assert "local model class shadowed by later import: User" in errors
    assert "Django settings is referenced but not imported" in errors
    assert "Django field uses annotation instead of assignment: Submission.student" in errors


def test_django_model_structure_guidance_supplies_exact_edit_recipes(tmp_path: Path):
    target = tmp_path / "demo/backend/core/models.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# models\n\n"
        "from django.db import models\n"
        "class User(models.Model):\n"
        "    pass\n"
        "from django.contrib.auth.models import User\n"
        "class Submission(models.Model):\n"
        "    student: models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": ["demo/backend/core/models.py"],
        "requirements": [
            {"kind": "entity", "text": "Submission: fields student"}
        ],
    }

    guidance = repl._prd_file_repair_guidance(
        "demo/backend/core/models.py", preflight, workspace=tmp_path
    )

    assert "Deterministic edit recipe:" in guidance
    assert "from django.conf import settings" in guidance
    assert '"old_string": "from django.contrib.auth.models import User\\n"' in guidance
    assert "student = models.ForeignKey" in guidance


def test_entity_file_pass_treats_reduced_missing_contracts_as_progress():
    before = {
        "missing entity contract:Submission",
        "missing entity contract:Grade",
    }
    after = {
        "missing entity contract:Submission.student",
        "Django field uses annotation instead of assignment: Submission.student",
    }

    assert repl._prd_entity_validation_progress(before, after)
    assert not repl._prd_fatal_file_regression(
        "Django field uses annotation instead of assignment: Submission.student"
    )


def test_auth_requirement_evidence_rejects_placeholder_unwired_views(tmp_path: Path):
    root = tmp_path / "demo/backend"
    (root / "core").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "core/views.py").write_text(
        "def login(request):\n    # Add authentication logic here\n    return None\n"
        "def logout(request):\n    # Add logout logic here\n    return None\n",
        encoding="utf-8",
    )
    (root / "config/urls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    preflight = {
        "project_root": "demo",
        "requirements": [
            {"kind": "role", "text": "Student", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Login with email + password", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Logout", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Session persists across page reloads", "priority": "must", "scope": "in"},
        ],
    }

    errors = repl._prd_requirement_evidence_errors(preflight, tmp_path)

    assert any("placeholder behavior" in item for item in errors)
    assert "roles have no executable source declarations: student" in errors
    assert "login does not call Django authenticate() and login()" in errors
    assert "logout does not call Django logout()" in errors
    assert "session persistence has no focused test evidence" in errors


def test_auth_requirement_evidence_accepts_routed_tested_session_auth(tmp_path: Path):
    root = tmp_path / "demo/backend"
    (root / "core/tests").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "core/models.py").write_text(
        "ROLE_CHOICES = [('student', 'Student')]\n", encoding="utf-8"
    )
    (root / "core/views.py").write_text(
        "from django.contrib.auth import authenticate, login, logout\n"
        "def login_view(request):\n"
        "    user = authenticate(request, username='x', password='y')\n"
        "    login(request, user)\n"
        "def logout_view(request):\n    logout(request)\n",
        encoding="utf-8",
    )
    (root / "config/urls.py").write_text(
        "urlpatterns = [path('login/', login_view), path('logout/', logout_view)]\n",
        encoding="utf-8",
    )
    (root / "core/tests/test_auth.py").write_text(
        "class TestAuth:\n"
        "    def test_login_logout_session(self):\n"
        "        session = self.client.session\n"
        "        assert session is not None\n",
        encoding="utf-8",
    )
    preflight = {
        "project_root": "demo",
        "requirements": [
            {"kind": "role", "text": "Student", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Login with email + password", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Logout", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Session persists across page reloads", "priority": "must", "scope": "in"},
        ],
    }

    assert repl._prd_requirement_evidence_errors(preflight, tmp_path) == []


def test_behavior_test_count_reads_django_found_test_parenthetical():
    outcome = SimpleNamespace(
        steps=[
            SimpleNamespace(
                step=SimpleNamespace(stage="test"),
                stdout="Found 0 test(s).\n",
                stderr="Ran 0 tests in 0.000s\n",
            )
        ]
    )

    assert repl._prd_behavior_test_count(outcome) == 0


def test_role_only_requirement_ignores_later_view_placeholders(tmp_path: Path):
    root = tmp_path / "demo/backend/core"
    root.mkdir(parents=True)
    (root / "models.py").write_text(
        "ROLE_CHOICES = [('admin', 'Admin'), ('teacher', 'Teacher')]\n",
        encoding="utf-8",
    )
    (root / "views.py").write_text(
        "def future_view(request):\n    # Add authentication logic here\n    return None\n",
        encoding="utf-8",
    )
    preflight = {
        "project_root": "demo",
        "requirements": [
            {"kind": "role", "text": "Admin", "priority": "must", "scope": "in"},
            {"kind": "role", "text": "Teacher", "priority": "must", "scope": "in"},
        ],
    }

    assert repl._prd_requirement_evidence_errors(preflight, tmp_path) == []


def test_semantic_requirement_recovery_does_not_prescribe_migrations():
    guidance = repl._prd_verifier_recovery_guidance(
        {
            "command": "python manage.py makemigrations --check --dry-run",
            "summary": "Requirement evidence validation failed: login endpoint is not wired",
        },
        {
            "expected_files": [
                "demo/backend/core/models.py",
                "demo/backend/config/urls.py",
            ],
            "requirements": [
                {"kind": "auth", "text": "Login with email + password"}
            ],
        },
    )

    assert "semantic requirement failure" in guidance
    assert "views.py" in guidance
    assert "tests/test_auth.py" in guidance
    assert "Do not rerun makemigrations" in guidance


def test_semantic_role_recipe_adds_required_choices_to_user_model(tmp_path: Path):
    target = tmp_path / "demo/backend/core/models.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from django.db import models\n"
        "class User(models.Model):\n"
        "    role = models.CharField(max_length=50)\n",
        encoding="utf-8",
    )
    preflight = {
        "expected_files": ["demo/backend/core/models.py"],
        "requirements": [
            {"kind": "role", "text": "Admin"},
            {"kind": "role", "text": "Teacher"},
        ],
    }
    verification = {
        "summary": (
            "Requirement evidence validation failed: roles have no executable source "
            "declarations: admin, teacher"
        )
    }

    recipes = repl._prd_semantic_source_edit_recipes(
        verification, preflight, tmp_path
    )

    assert len(recipes) == 1
    assert "class User" in recipes[0]["arguments"]["old_string"]
    assert "choices=[('admin', 'Admin'), ('teacher', 'Teacher')]" in recipes[0][
        "arguments"
    ]["new_string"]
    assert repl._prd_semantic_source_repair_files(
        verification, preflight, tmp_path
    ) == ["demo/backend/core/models.py"]


def test_semantic_role_recipe_preserves_existing_choices(tmp_path: Path):
    target = tmp_path / "demo/backend/core/models.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from django.db import models\n"
        "class User(models.Model):\n"
        "    role = models.CharField(max_length=50, choices=[('admin', 'Admin'), ('teacher', 'Teacher')])\n",
        encoding="utf-8",
    )
    preflight = {"expected_files": ["demo/backend/core/models.py"]}
    verification = {
        "summary": (
            "Requirement evidence validation failed: roles have no executable source "
            "declarations: student; session persistence has no focused test evidence"
        )
    }

    recipes = repl._prd_semantic_source_edit_recipes(
        verification, preflight, tmp_path
    )

    assert len(recipes) == 1
    updated = recipes[0]["arguments"]["new_string"]
    assert "('admin', 'Admin')" in updated
    assert "('teacher', 'Teacher')" in updated
    assert "('student', 'Student')" in updated


def test_auth_url_evidence_requires_app_urls_to_be_included_by_root(tmp_path: Path):
    root = tmp_path / "demo/backend"
    (root / "core/tests").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "core/models.py").write_text(
        "ROLE_CHOICES = [('student', 'Student')]\n", encoding="utf-8"
    )
    (root / "core/views.py").write_text(
        "from django.contrib.auth import authenticate, login, logout\n"
        "def login_view(request):\n"
        "    user = authenticate(request, username='x', password='y')\n"
        "    login(request, user)\n"
        "def logout_view(request):\n    logout(request)\n",
        encoding="utf-8",
    )
    (root / "core/urls.py").write_text(
        "urlpatterns = [path('login/', login_view), path('logout/', logout_view)]\n",
        encoding="utf-8",
    )
    (root / "config/urls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    (root / "core/tests/test_auth.py").write_text(
        "class TestAuth:\n"
        "    def test_login_logout_session(self):\n"
        "        session = self.client.session\n",
        encoding="utf-8",
    )
    preflight = {
        "project_root": "demo",
        "expected_files": ["demo/backend/config/urls.py"],
        "requirements": [
            {"kind": "role", "text": "Student", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Login", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Logout", "priority": "must", "scope": "in"},
            {"kind": "auth", "text": "Session persists", "priority": "must", "scope": "in"},
        ],
    }

    errors = repl._prd_requirement_evidence_errors(preflight, tmp_path)

    assert "login endpoint is not wired into Django URL patterns" in errors
    assert "logout endpoint is not wired into Django URL patterns" in errors


def test_semantic_repair_context_embeds_behavior_files_and_missing_targets(tmp_path: Path):
    root = tmp_path / "demo/backend"
    (root / "core").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    (root / "core/models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (root / "core/views.py").write_text("def login_view(request):\n    pass\n", encoding="utf-8")
    (root / "config/urls.py").write_text("urlpatterns = []\n", encoding="utf-8")
    preflight = {
        "project_root": "demo",
        "expected_files": [
            "demo/backend/core/models.py",
            "demo/backend/config/urls.py",
        ],
    }

    context = repl._prd_semantic_repair_source_context(preflight, tmp_path)

    assert "Do not list or search the project" in context
    assert "demo/backend/core/views.py" in context
    assert "def login_view" in context
    assert "demo/backend/core/urls.py" in context
    assert "<MISSING" in context
    assert "demo/backend/core/tests/test_auth.py" in context
    assert "demo/backend/core/tests/__init__.py" in context


def test_missing_whole_entity_classes_selects_append_only_repair():
    assert repl._prd_requires_entity_declaration_append(
        [
            "demo/backend/core/models.py "
            "(missing required entities or fields: Submission, Grade)"
        ]
    )
    assert not repl._prd_requires_entity_declaration_append(
        [
            "demo/backend/core/models.py "
            "(missing required entities or fields: Submission.content/submitted_at, Grade)"
        ]
    )


def test_prd_milestone_verifier_blocks_mandatory_prose_only_completion(tmp_path: Path):
    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-002",
            {
                "active_skills": ["developer"],
                "expected_files": [],
                "verifier": "focused app tests/build",
                "requirements": [
                    {"id": "FEAT-001", "kind": "feature", "scope": "in", "priority": "must"}
                ],
            },
            [],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "failed"
    assert verification["status"] == "failed"
    assert "without a confirmed source mutation" in verification["summary"]


def test_scope_prd_preflight_anchors_expected_files_under_project_root():
    scoped = repl._scope_prd_preflight(
        {
            "expected_files": ["frontend/package.json", "demo/backend/manage.py"],
            "allowed_tools": ["read_file", "write_file", "ask_user"],
            "rollback_policy": "rollback changed files on failed verifier",
        },
        "demo",
    )

    assert scoped["project_root"] == "demo"
    assert scoped["expected_files"] == [
        "demo/frontend/package.json",
        "demo/backend/manage.py",
    ]
    assert scoped["allowed_tools"] == [
        "read_file",
        "write_file",
        "append_file",
        "file_info",
        "find_file",
    ]
    assert "no rollback" in scoped["rollback_policy"]


def test_expected_file_passes_focus_one_target_and_accumulate(monkeypatch, tmp_path: Path):
    calls: list[dict[str, object]] = []

    async def fake_run_agent_chat(_prompt, workspace, _console, **kwargs):
        calls.append(kwargs)
        target = kwargs["allowed_write_paths"][0]
        path = workspace / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ready\n", encoding="utf-8")
        return SimpleNamespace(changed_files=[target], stopped=False, awaiting_user=False)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    preflight = {
        "requirements": [
            {"id": "F-001", "kind": "feature", "text": "Build it", "scope": "in", "priority": "must"}
        ],
        "expected_files": ["demo/a.txt", "demo/b.txt"],
        "allowed_tools": ["read_file", "write_file", "edit_file", "run_command"],
        "active_skills": [],
    }

    changed = asyncio.run(
        repl._run_prd_expected_file_passes(
            title="Demo",
            relative_path="prd.md",
            prd_brief="A demo app.",
            milestone="M-001 foundation",
            preflight=preflight,
            project_root="demo",
            workspace=tmp_path,
            console=_console(),
            session_logger=None,
        )
    )

    assert changed == ["demo/a.txt", "demo/b.txt"]
    assert [call["allowed_write_paths"] for call in calls] == [
        ("demo/a.txt",),
        ("demo/b.txt",),
    ]
    assert all("ask_user" not in call["allowed_tools"] for call in calls)


def test_prd_milestone_brief_keeps_cross_cutting_context_not_every_feature():
    parsed = parse_prd_text(
        "# Large App\n\n"
        "## Overview\nA full-stack operations app.\n\n"
        "## Tech Stack\n- Django\n- React\n- SQLite\n\n"
        "## Features\n"
        + "\n".join(f"- Implement workflow {index}." for index in range(1, 35)),
        markdown=True,
    )

    brief = repl._prd_brief(parsed)

    assert "Summary: A full-stack operations app." in brief
    assert "Required stack:" in brief
    assert "Implement workflow 34" not in brief
    assert len(brief) < 1500


def test_prd_milestone_verifier_ignores_unsafe_file_inputs(tmp_path: Path):
    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-002",
            {"active_skills": ["developer"], "expected_files": [], "verifier": ""},
            ["app.py; echo nope"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "implemented"
    assert verification["status"] == "unverifiable"
    assert verification["files"] == []


def test_prepare_prd_milestone_preflight_accepts_valid_model_output(
    monkeypatch,
    tmp_path: Path,
):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")
    seen: dict[str, object] = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs

        async def generate_structured(self, role, system, prompt, schema, **kwargs):
            seen["role"] = role
            seen["system"] = system
            seen["prompt"] = prompt
            seen["schema"] = schema
            seen["options"] = kwargs
            return json.dumps(
                {
                    "milestone_id": "M-002",
                    "requirement_ids": ["FEAT-001"],
                    "active_skills": ["developer"],
                    "expected_files": ["src/SearchPanel.tsx"],
                    "allowed_tools": ["read_file", "write_file"],
                    "verifier": "focused app tests/build",
                    "context_focus": ["Search task workflow"],
                    "implementation_steps": [
                        "Create the search panel target file.",
                        "Run the focused app check.",
                    ],
                    "risk_flags": [],
                    "blocker_question": "",
                    "notes": "Use existing app state.",
                }
            )

    monkeypatch.setenv("SHAMSU_PRD_MODEL_PREFLIGHT", "1")
    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    preflight, state = asyncio.run(
        repl._prepare_prd_milestone_preflight(
            root,
            state,
            "M-002",
            deterministic,
            tmp_path,
            _console(),
            None,
        )
    )

    assert seen["role"] == "planner"
    assert preflight["preflight_source"] == "model"
    assert preflight["context_focus"] == ["Search task workflow"]
    assert preflight["implementation_steps"] == [
        "Create the search panel target file.",
        "Run the focused app check.",
    ]
    assert "src/SearchPanel.tsx" in preflight["expected_files"]
    assert state["preflight_decisions"][0]["accepted"] is True
    assert state["preflight_decisions"][0]["implementation_steps"][1] == (
        "Run the focused app check."
    )
    assert (root / "preflight" / "M-002.effective.json").is_file()


def test_prepare_prd_milestone_preflight_runs_by_default(
    monkeypatch,
    tmp_path: Path,
):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    class FakeLLM:
        def __init__(self, **_kwargs):
            pass

        async def generate_structured(self, role, system, prompt, schema, **kwargs):
            return json.dumps(
                {
                    "milestone_id": "M-002",
                    "requirement_ids": ["FEAT-001"],
                    "active_skills": ["developer"],
                    "expected_files": ["src/SearchPanel.tsx"],
                    "allowed_tools": ["read_file", "write_file"],
                    "verifier": "focused app tests/build",
                    "context_focus": ["Search task workflow"],
                    "implementation_steps": [
                        "Create the search panel target file.",
                        "Wire the task search requirements into that file.",
                        "Run the focused app check.",
                    ],
                    "risk_flags": [],
                    "blocker_question": "",
                    "notes": "Use existing app state.",
                }
            )

    monkeypatch.delenv("SHAMSU_PRD_MODEL_PREFLIGHT", raising=False)
    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    preflight, next_state = asyncio.run(
        repl._prepare_prd_milestone_preflight(
            root,
            state,
            "M-002",
            deterministic,
            tmp_path,
            _console(),
            None,
        )
    )

    assert preflight["preflight_source"] == "model"
    assert preflight["context_focus"] == ["Search task workflow"]
    assert preflight["implementation_steps"][1] == (
        "Wire the task search requirements into that file."
    )
    assert next_state["preflight_decisions"][0]["accepted"] is True


def test_prepare_prd_milestone_preflight_can_be_disabled_by_env(
    monkeypatch,
    tmp_path: Path,
):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    class BoomLLM:  # pragma: no cover - must not be instantiated
        def __init__(self, **_kwargs):
            raise AssertionError("model preflight should obey the explicit off switch")

    monkeypatch.setenv("SHAMSU_PRD_MODEL_PREFLIGHT", "0")
    monkeypatch.setattr(repl, "LLMManager", BoomLLM)

    preflight, next_state = asyncio.run(
        repl._prepare_prd_milestone_preflight(
            root,
            state,
            "M-002",
            deterministic,
            tmp_path,
            _console(),
            None,
        )
    )

    assert preflight["preflight_source"] == "deterministic"
    assert next_state == state
    assert not (root / "preflight-decisions.jsonl").exists()


def test_compiled_prd_build_uses_existing_checkpoint_to_resume(monkeypatch, tmp_path: Path):
    contract = _contract()
    root, state = initialize_prd_execution(
        tmp_path,
        "build the product from prd.md",
        contract,
        prd_path="prd.md",
        execution_key="demo",
    )
    checkpoint_milestone(
        root,
        state,
        "M-002",
        changed_files=["src/App.tsx"],
        evidence=["changed:src/App.tsx"],
    )
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    calls: list[str] = []

    async def fake_run_agent_chat(user_input, *_args, **_kwargs):
        calls.append(user_input)
        return SimpleNamespace(
            changed_files=["src/app.test.ts"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify(*_args, **_kwargs):
        return True

    async def fake_verify_milestone(*_args, **_kwargs):
        return (
            "verified",
            {
                "status": "verified",
                "verified": True,
                "unverifiable": False,
                "exit_code": 0,
                "command": "focused-check",
                "files": ["src/app.test.ts"],
                "summary": "Verification passed.",
            },
        )

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    remaining = len(state["milestones"]) - 1
    assert len(calls) == remaining
    assert f"Current milestone 2/{len(state['milestones'])}:" in calls[0]
    assert "[x] M-002" in calls[0]
    assert "[>]" in calls[0]
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert {item["status"] for item in final_state["milestones"]} == {"verified"}


def test_compiled_prd_build_stops_on_failed_milestone_verifier(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    calls: list[str] = []

    async def fake_run_agent_chat(user_input, *_args, **_kwargs):
        calls.append(user_input)
        return SimpleNamespace(
            changed_files=["app.py"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify_milestone(*_args, **_kwargs):
        return (
            "failed",
            {
                "status": "failed",
                "verified": False,
                "unverifiable": False,
                "exit_code": 1,
                "command": "python -m py_compile app.py",
                "files": ["app.py"],
                "summary": "Verification FAILED.",
            },
        )

    async def fake_final_verify(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("final verifier should not run after a failed milestone")

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_MILESTONE_REPAIR", "0")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert final_state["status"] == "failed"
    assert final_state["current_milestone_id"] == "M-002"
    assert final_state["milestones"][0]["status"] == "failed"
    assert final_state["milestones"][1]["status"] == "pending"


def test_compiled_prd_build_rolls_back_failed_milestone_transactions(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )

    async def fake_run_agent_chat(*_args, **_kwargs):
        _transactional_write(tmp_path, "app.py", "def nope(:\n")
        return SimpleNamespace(
            changed_files=["app.py"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify_milestone(*_args, **_kwargs):
        return (
            "failed",
            {
                "status": "failed",
                "verified": False,
                "unverifiable": False,
                "exit_code": 1,
                "command": "python -m py_compile app.py",
                "files": ["app.py"],
                "summary": "Verification FAILED.",
            },
        )

    async def fake_final_verify(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("final verifier should not run after a failed milestone")

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_MILESTONE_REPAIR", "0")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    rollback_records = [
        json.loads(line)
        for line in (root / "rollbacks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    transaction_id = rollback_records[-1]["transaction_ids"][0]
    manifest = TransactionWorkspace(tmp_path).load_manifest(transaction_id)

    assert not (tmp_path / "app.py").exists()
    assert [record["phase"] for record in rollback_records] == ["started", "finished"]
    assert rollback_records[-1]["status"] == "rolled_back"
    assert final_state["changed_files"] == []
    assert final_state["milestones"][0]["last_rollback"]["status"] == "rolled_back"
    assert manifest is not None
    assert manifest["status"] == "rolled_back"


def test_compiled_prd_build_can_disable_failed_milestone_rollback(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )

    async def fake_run_agent_chat(*_args, **_kwargs):
        _transactional_write(tmp_path, "app.py", "def nope(:\n")
        return SimpleNamespace(
            changed_files=["app.py"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify_milestone(*_args, **_kwargs):
        return (
            "failed",
            {
                "status": "failed",
                "verified": False,
                "unverifiable": False,
                "exit_code": 1,
                "command": "python -m py_compile app.py",
                "files": ["app.py"],
                "summary": "Verification FAILED.",
            },
        )

    async def fake_final_verify(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("final verifier should not run after a failed milestone")

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_MILESTONE_REPAIR", "0")
    monkeypatch.setenv("SHAMSU_PRD_MILESTONE_ROLLBACK", "0")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def nope(:\n"
    assert not (root / "rollbacks.jsonl").exists()
    assert final_state["changed_files"] == ["app.py"]
    assert final_state["milestones"][0]["last_rollback"] == {}


def test_compiled_prd_build_repairs_failed_milestone_and_continues(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    calls: list[tuple[str, dict[str, object]]] = []
    verify_counts: dict[str, int] = {}

    async def fake_run_agent_chat(user_input, *_args, **kwargs):
        calls.append((user_input, kwargs))
        changed = ["app.py"] if "M-002" in user_input else ["tests/app.test.py"]
        return SimpleNamespace(
            changed_files=changed,
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify_milestone(milestone_id, *_args, **_kwargs):
        verify_counts[milestone_id] = verify_counts.get(milestone_id, 0) + 1
        if milestone_id == "M-002" and verify_counts[milestone_id] == 1:
            return (
                "failed",
                {
                    "status": "failed",
                    "verified": False,
                    "unverifiable": False,
                    "exit_code": 1,
                    "command": "python -m py_compile app.py",
                    "files": ["app.py"],
                    "summary": "Verification FAILED.",
                },
            )
        return (
            "verified",
            {
                "status": "verified",
                "verified": True,
                "unverifiable": False,
                "exit_code": 0,
                "command": "python -m py_compile app.py",
                "files": ["app.py"],
                "summary": "Verification passed.",
            },
        )

    async def fake_final_verify(*_args, **_kwargs):
        return True

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_REPAIR_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    repair_records = [
        json.loads(line)
        for line in (root / "repairs.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(calls) == 3
    assert "Repair attempt 1/2" in calls[1][0]
    assert calls[1][1]["allowed_write_paths"] == ("demo",)
    assert verify_counts["M-002"] == 2
    assert [record["phase"] for record in repair_records] == ["started", "finished"]
    assert repair_records[1]["status"] == "verified"
    assert {item["status"] for item in final_state["milestones"]} == {"verified"}


def test_compiled_prd_build_repairs_a_stalled_agent_pass(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    calls: list[str] = []

    async def fake_run_agent_chat(user_input, *_args, **_kwargs):
        calls.append(user_input)
        stalled = len(calls) == 1
        return SimpleNamespace(
            changed_files=[] if stalled else ["demo/app.py"],
            stopped=stalled,
            awaiting_user=False,
            final="prose-only stall" if stalled else "done",
        )

    async def fake_verify_milestone(*_args, **_kwargs):
        return "verified", {
            "status": "verified",
            "verified": True,
            "unverifiable": False,
            "exit_code": 0,
            "command": "python -m py_compile demo/app.py",
            "files": ["demo/app.py"],
            "summary": "Verification passed.",
        }

    async def fake_final_verify(*_args, **_kwargs):
        return True

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_REPAIR_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request("build the product from prd.md", tmp_path, _console())
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert "Repair attempt 1/2" in calls[1]
    assert {item["status"] for item in final_state["milestones"]} == {"verified"}


def test_compiled_prd_build_stops_after_repair_budget(monkeypatch, tmp_path: Path):
    contract = _contract()
    prd = tmp_path / "prd.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="demo",
        generation_ready=True,
        needs_input=False,
        prd_contract=contract,
        suitability=SimpleNamespace(strategy="generic"),
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    calls: list[str] = []
    verify_calls: list[str] = []

    async def fake_run_agent_chat(user_input, *_args, **_kwargs):
        calls.append(user_input)
        return SimpleNamespace(
            changed_files=["app.py"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify_milestone(milestone_id, *_args, **_kwargs):
        verify_calls.append(milestone_id)
        return (
            "failed",
            {
                "status": "failed",
                "verified": False,
                "unverifiable": False,
                "exit_code": 1,
                "command": "python -m py_compile app.py",
                "files": ["app.py"],
                "summary": "Verification FAILED.",
            },
        )

    async def fake_final_verify(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("final verifier should not run after repair budget is exhausted")

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setenv("SHAMSU_PRD_REPAIR_MAX_ATTEMPTS", "1")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed, **_kw: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_prd_milestone", fake_verify_milestone)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_final_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    root = next((tmp_path / ".shamsu" / "prd-executions").iterdir())
    final_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    repair_records = [
        json.loads(line)
        for line in (root / "repairs.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(calls) == 2
    assert verify_calls == ["M-002", "M-002"]
    assert [record["phase"] for record in repair_records] == ["started", "finished"]
    assert repair_records[1]["status"] == "failed"
    assert final_state["status"] == "failed"
    assert final_state["milestones"][0]["status"] == "failed"
    assert final_state["milestones"][1]["status"] == "pending"


def test_partial_entity_contract_progress_is_not_a_regression():
    """Live 2026-08-02 (run 8): a repair correctly added `role`, taking
    models.py from "missing User.name/role" to "missing User.name" - and was
    ROLLED BACK, because the guard subtracted raw strings and those two simply
    differ. The validator groups every field of one entity into a single
    slash-joined value, so progress must be measured per field."""
    before = {"core/models.py (missing required entities or fields: User.name/role)"}
    after = {"core/models.py (missing required entities or fields: User.name)"}

    assert repl._prd_repair_regressions(before, after) == []
    assert repl._prd_entity_validation_progress(
        repl._prd_target_validation_errors("core/models.py", sorted(before)),
        repl._prd_target_validation_errors("core/models.py", sorted(after)),
    ) is True


def test_a_genuinely_new_missing_field_is_still_a_regression():
    before = {"core/models.py (missing required entities or fields: User.name)"}
    after = {"core/models.py (missing required entities or fields: User.name/role)"}

    regressions = repl._prd_repair_regressions(before, after)

    assert len(regressions) == 1
    assert "User.role" in regressions[0]


def test_a_new_non_entity_error_is_still_a_regression():
    before = set()
    after = {"config/settings.py (WSGI module does not exist)"}

    regressions = repl._prd_repair_regressions(before, after)

    assert regressions == ["config/settings.py (WSGI module does not exist)"]


def test_fully_resolved_contracts_report_no_regression():
    before = {"core/models.py (missing required entities or fields: User.name/role)"}

    assert repl._prd_repair_regressions(before, set()) == []


def test_missing_entity_atoms_expand_grouped_fields():
    errors = {"missing entity contract:User.name/role", "missing entity contract:Course"}

    assert repl._prd_missing_entity_atoms(errors) == {"User.name", "User.role", "Course"}


def test_failed_milestone_blocks_only_its_dependents():
    """A single failed milestone used to `return` out of the WHOLE build, so one
    failure ended all 23 - at 90% per-milestone that is 0.9^23 ~ 9%, which is
    why no run ever finished. Only declared dependents may be blocked."""
    failed = {"M-001": "verifier failed"}

    # Depends on the failed milestone -> blocked.
    assert repl._prd_blocking_dependencies({"dependencies": ["M-001"]}, failed, {}) == ["M-001"]
    # Independent -> still runs.
    assert repl._prd_blocking_dependencies({"dependencies": []}, failed, {}) == []
    assert repl._prd_blocking_dependencies({"dependencies": ["M-002"]}, failed, {}) == []


def test_blocking_propagates_through_skipped_milestones():
    """A milestone skipped because its dependency failed must itself block its
    own dependents - otherwise the cascade leaks."""
    failed = {"M-001": "verifier failed"}
    skipped = {"M-002": "blocked by M-001"}

    assert repl._prd_blocking_dependencies({"dependencies": ["M-002"]}, failed, skipped) == ["M-002"]


def test_completion_report_states_what_landed_and_what_did_not():
    report = repl._prd_build_completion_report(
        ["M-001 Foundation", "M-002 Workflows", "M-003 Persistence", "M-004 Release"],
        {"M-002": "login endpoint not wired"},
        {"M-004": "Skipped: depends on M-002, which did not complete."},
    )

    assert "2/4 milestone(s) completed" in report
    assert "M-002: login endpoint not wired" in report
    assert "M-004" in report


def test_blocking_dependencies_tolerates_a_missing_preflight():
    assert repl._prd_blocking_dependencies({}, {"M-001": "x"}, {}) == []
    assert repl._prd_blocking_dependencies(None, {"M-001": "x"}, {}) == []


def test_dangling_relation_target_is_caught_at_write_time():
    """Live runs 10 and 11: the model wrote ForeignKey('Teacher')/('Student')
    while defining neither, having already given User a role field. Django only
    reports that at check time (fields.E300/E307), after the milestone failed."""
    content = (
        "from django.db import models\n"
        "from django.contrib.auth.models import AbstractUser\n\n"
        "class User(AbstractUser):\n    role = models.CharField(max_length=20)\n\n"
        "class Grade(models.Model):\n"
        "    graded_by = models.ForeignKey('Teacher', on_delete=models.CASCADE)\n"
    )

    errors = repl._django_model_structure_errors(content)

    assert any("undefined model 'Teacher'" in error for error in errors)
    assert any("User" in error for error in errors)


def test_legitimate_relation_targets_are_not_flagged():
    content = (
        "from django.db import models\n"
        "from django.conf import settings\n\n"
        "class Course(models.Model):\n    title = models.CharField(max_length=10)\n\n"
        "class Node(models.Model):\n"
        "    parent = models.ForeignKey('self', on_delete=models.CASCADE)\n"
        "    course = models.ForeignKey('Course', on_delete=models.CASCADE)\n"
        "    other = models.ForeignKey('otherapp.Thing', on_delete=models.CASCADE)\n"
        "    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)\n"
    )

    assert repl._django_model_structure_errors(content) == []


def test_django_check_errors_are_parsed_into_repair_guidance(tmp_path: Path):
    """Run 11 died with a fully machine-readable error and logged "no structured
    diagnostics were extracted" - fields.EXXX has no file or line."""
    models = tmp_path / "backend" / "core" / "models.py"
    models.parent.mkdir(parents=True)
    models.write_text("from django.db import models\n", encoding="utf-8")
    verification = {
        "summary": (
            "Verification FAILED at required framework stage: `python manage.py check` (exit 1).\n"
            "SystemCheckError: System check identified some issues:\n\n"
            "ERRORS:\n"
            "core.Grade.graded_by: (fields.E300) Field defines a relation with model "
            "'Teacher', which is either not installed, or is abstract.\n"
            "core.Submission.student: (fields.E307) The field core.Submission.student was "
            "declared with a lazy reference to 'core.student'.\n"
        )
    }
    preflight = {"expected_files": ["backend/core/models.py"]}

    assert repl._prd_django_check_repair_files(verification, preflight, tmp_path) == [
        "backend/core/models.py"
    ]
    guidance = repl._prd_django_check_edit_guidance(verification, preflight, tmp_path)

    assert "backend/core/models.py" in guidance
    assert "Grade.graded_by (fields.E300)" in guidance
    assert "call edit_file" in guidance
    assert "the correct target is the User model" in guidance


def test_django_check_parser_ignores_a_passing_verifier(tmp_path: Path):
    assert repl._prd_django_check_diagnostic({"summary": "Verification passed."}, {}, tmp_path) is None
    assert repl._prd_django_check_edit_guidance({"summary": ""}, {}, tmp_path) == ""
