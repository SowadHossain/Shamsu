from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

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

    rendered = render_preflight_context(preflight)

    assert "## Milestone Preflight" in rendered
    assert "Requirement IDs: FEAT-001" in rendered
    assert "Search tasks" in rendered
    assert len(rendered) < 2000


def test_model_preflight_schema_declares_bounded_fields():
    schema = model_preflight_schema()

    assert schema["type"] == "object"
    assert "milestone_id" in schema["required"]
    assert "expected_files" in schema["properties"]
    assert "allowed_tools" in schema["properties"]


def test_validate_model_preflight_accepts_allowlisted_focus(tmp_path: Path):
    root, _state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    preflight, errors = validate_model_preflight(
        deterministic,
        {
            "milestone_id": "M-002",
            "requirement_ids": ["FEAT-001"],
            "active_skills": ["developer", "react-vite"],
            "expected_files": ["src/SearchPanel.tsx"],
            "allowed_tools": ["read_file", "write_file", "edit_file"],
            "verifier": "focused app tests/build",
            "context_focus": ["Search task workflow", "preserve existing App wiring"],
            "risk_flags": ["do not replace previous milestone files"],
            "notes": "Keep the milestone small.",
        },
    )

    assert errors == []
    assert preflight["preflight_source"] == "model"
    assert preflight["requirement_ids"] == ["FEAT-001"]
    assert preflight["active_skills"] == ["developer", "react-vite"]
    assert "src/SearchPanel.tsx" in preflight["expected_files"]
    assert preflight["allowed_tools"] == ["read_file", "write_file", "edit_file"]
    assert preflight["context_focus"][0] == "Search task workflow"


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

    state = record_milestone_preflight(root, state, "M-002", preflight)
    reloaded = json.loads((root / "state.json").read_text(encoding="utf-8"))
    decision = json.loads((root / "preflight-decisions.jsonl").read_text(encoding="utf-8"))

    assert state["preflight_decisions"][0]["accepted"] is True
    assert reloaded["preflight_decisions"][0]["source"] == "model"
    assert (root / "preflight" / "M-002.effective.json").is_file()
    assert decision["context_focus"] == ["Search task workflow"]


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


def test_prd_milestone_verifier_keeps_node_lightweight_unverified(tmp_path: Path):
    status, verification = asyncio.run(
        repl._verify_prd_milestone(
            "M-002",
            {
                "active_skills": ["developer", "react-vite"],
                "expected_files": ["src/App.tsx", "package.json"],
                "verifier": "focused app tests/build",
            },
            ["src/App.tsx"],
            tmp_path,
            _console(),
            None,
        )
    )

    assert status == "implemented"
    assert verification["status"] == "unverifiable"
    assert verification["unverifiable"] is True


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
                    "active_skills": ["developer", "react-vite"],
                    "expected_files": ["src/SearchPanel.tsx"],
                    "allowed_tools": ["read_file", "write_file"],
                    "verifier": "focused app tests/build",
                    "context_focus": ["Search task workflow"],
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
    assert "src/SearchPanel.tsx" in preflight["expected_files"]
    assert state["preflight_decisions"][0]["accepted"] is True
    assert (root / "preflight" / "M-002.effective.json").is_file()


def test_prepare_prd_milestone_preflight_is_disabled_by_default(
    monkeypatch,
    tmp_path: Path,
):
    root, state = initialize_prd_execution(tmp_path, "build from PRD", _contract())
    deterministic = load_milestone_preflight(root, "M-002")

    class BoomLLM:  # pragma: no cover - must not be instantiated
        def __init__(self, **_kwargs):
            raise AssertionError("model preflight should be opt-in")

    monkeypatch.delenv("SHAMSU_PRD_MODEL_PREFLIGHT", raising=False)
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

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_verify_completed_plan", fake_verify)

    asyncio.run(
        repl._handle_prd_build_request(
            "build the product from prd.md",
            tmp_path,
            _console(),
        )
    )

    assert len(calls) == 1
    assert "Current milestone 2/2: M-004" in calls[0]
    assert "[x] M-002" in calls[0]
    assert "[>] M-004" in calls[0]
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
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
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
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
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
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
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
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
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
    assert calls[1][1]["allowed_write_paths"] == ("app.py",)
    assert verify_counts["M-002"] == 2
    assert [record["phase"] for record in repair_records] == ["started", "finished"]
    assert repair_records[1]["status"] == "verified"
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
    monkeypatch.setattr(repl, "build_project_spec", lambda _parsed: project)
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
