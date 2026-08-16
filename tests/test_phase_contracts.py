from __future__ import annotations

from pathlib import Path

from shamsu.runtime.advanced_capabilities import (
    ADVANCED_CAPABILITY_CONTRACTS,
    AdvancedCapability,
)
from shamsu.runtime.phase_contracts import ExecutionPhase, phase_for_step
from shamsu.tools.agent_tools import AgentToolRegistry


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def _stub_command_runner(registry: AgentToolRegistry) -> None:
    registry.command_runner.last_command_resolution = None
    registry.command_runner.last_diagnostic_packet = None
    registry.command_runner.last_error_packet = None
    registry.command_runner.last_diagnostics_path = ""
    registry.command_runner.run = lambda _command, _cwd: (0, "ok", "")


def test_explore_denies_project_mutation_with_structured_payload(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.EXPLORE, task_risk="low")

    result = registry.execute("write_file", {"filepath": "app.py", "content": "x = 1\n"})

    assert result.ok is False
    assert result.data["requested_tool"] == "write_file"
    assert result.data["current_phase"] == "EXPLORE"
    assert "read_file" in result.data["allowed_tools"]
    assert result.data["phase_denied"] is True
    assert not (tmp_path / "app.py").exists()


def test_plan_hides_and_denies_mutating_tools(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.PLAN, task_risk="low")
    registry.set_allowed_tools({"read_file", "write_file"})

    names = {schema["function"]["name"] for schema in registry.tool_schemas()}
    result = registry.execute("write_file", {"filepath": "plan.txt", "content": "nope"})

    assert "read_file" in names
    assert "write_file" not in names
    assert result.ok is False
    assert result.data["current_phase"] == "PLAN"


def test_author_allows_file_patch_and_blocks_deploy_command(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
    _stub_command_runner(registry)

    written = registry.execute("write_file", {"filepath": "app.py", "content": "x = 1\n"})
    denied = registry.execute("run_command", {"command": "docker compose up", "cwd": "."})

    assert written.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert denied.ok is False
    assert denied.data["requested_tool"] == "run_command"
    assert denied.data["current_phase"] == "AUTHOR"


def test_verify_allows_checks_but_blocks_source_modification(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.VERIFY, task_risk="low")
    _stub_command_runner(registry)

    check = registry.execute("run_command", {"command": "pytest tests", "cwd": "."})
    write = registry.execute("edit_file", {"filepath": "app.py", "old_string": "x", "new_string": "y"})

    assert check.ok is True
    assert write.ok is False
    assert write.data["current_phase"] == "VERIFY"


def test_repair_allows_targeted_patch_and_verification_command(tmp_path: Path):
    (tmp_path / "bug.py").write_text("VALUE = 1\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.REPAIR, task_risk="medium")
    _stub_command_runner(registry)

    patch = registry.execute(
        "edit_file",
        {"filepath": "bug.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
    )
    verify = registry.execute("run_command", {"command": "python -m pytest tests/test_bug.py", "cwd": "."})

    assert patch.ok is True
    assert verify.ok is True


def test_deploy_allows_local_docker_and_blocks_source_write(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.DEPLOY, task_risk="medium")
    registry.set_enabled_advanced_capabilities({"docker"})
    _stub_command_runner(registry)

    deploy = registry.execute("run_command", {"command": "docker compose ps", "cwd": "."})
    write = registry.execute("append_file", {"filepath": "app.py", "content": "x = 1\n"})

    assert deploy.ok is True
    assert write.ok is False
    assert write.data["current_phase"] == "DEPLOY"


def test_advanced_docker_is_blocked_until_enabled(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.DEPLOY, task_risk="medium")
    _stub_command_runner(registry)

    denied = registry.execute("run_command", {"command": "docker compose ps", "cwd": "."})

    assert denied.ok is False
    assert "Advanced capability docker is disabled" in denied.data["reason"]
    assert denied.data["current_phase"] == "DEPLOY"


def test_documentation_retrieval_is_blocked_by_active_phase_until_enabled(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.EXPLORE, task_risk="low")

    hidden_names = {schema["function"]["name"] for schema in registry.tool_schemas()}
    denied = registry.execute("search_docs", {"query": "auth tokens"})

    assert "search_docs" not in hidden_names
    assert denied.ok is False
    assert "documentation_retrieval is disabled" in denied.data["reason"]

    registry.set_enabled_advanced_capabilities({"documentation_retrieval"})
    visible_names = {schema["function"]["name"] for schema in registry.tool_schemas()}
    assert "search_docs" in visible_names


def test_complete_closes_tools(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.COMPLETE, task_risk="low")

    names = {schema["function"]["name"] for schema in registry.tool_schemas()}
    result = registry.execute("read_file", {"filepath": "README.md"})

    assert names == set()
    assert result.ok is False
    assert result.data["current_phase"] == "COMPLETE"
    assert result.data["allowed_tools"] == []


def test_high_risk_author_step_blocks_mutation_before_dispatch(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="high")

    result = registry.execute("write_file", {"filepath": "risky.py", "content": "x = 1\n"})

    assert result.ok is False
    assert "High-risk" in result.data["reason"]
    assert not (tmp_path / "risky.py").exists()


def test_high_risk_repair_allows_file_edit_but_blocks_destructive_tool(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.REPAIR, task_risk="high")

    write = registry.execute("write_file", {"filepath": "risky.py", "content": "x = 1\n"})
    delete = registry.execute("delete_file", {"filepath": "risky.py"})

    assert write.ok is True
    assert delete.ok is False
    assert delete.data["current_phase"] == "REPAIR"
    assert delete.data["phase_denied"] is True
    assert (tmp_path / "risky.py").exists()


def test_phase_for_step_derives_author_and_verify():
    author = phase_for_step(
        allowed_tools=["read_file", "write_file"],
        required_evidence=["file_changed"],
        approval_required=True,
        risk_level="medium",
    )
    verify = phase_for_step(
        allowed_tools=["run_command"],
        required_evidence=["test_passed"],
        approval_required=False,
        risk_level="low",
    )

    assert author == ExecutionPhase.AUTHOR
    assert verify == ExecutionPhase.VERIFY


def test_advanced_capability_contracts_have_order_and_evaluation_hooks():
    ordered = sorted(ADVANCED_CAPABILITY_CONTRACTS.values(), key=lambda contract: contract.order)

    assert [contract.capability for contract in ordered] == [
        AdvancedCapability.DOCUMENTATION_RETRIEVAL,
        AdvancedCapability.PACKAGE_INSTALLATION,
        AdvancedCapability.DOCKER,
        AdvancedCapability.DATABASES,
        AdvancedCapability.PRD_WORKFLOWS,
        AdvancedCapability.LARGER_PROJECT_AUTONOMY,
    ]
    for contract in ordered:
        assert contract.phase_rules
        assert contract.risk_policy
        assert contract.evidence_types
        assert contract.verification_strategy
        assert contract.task_evaluations
