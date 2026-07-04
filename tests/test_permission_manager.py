from __future__ import annotations

import json
from io import StringIO

from rich.console import Console

from shamsu.safety.approval import ask_remember_choice
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.commands import is_auto_approvable_action
from shamsu.safety.permission_store import PermissionMemory
from shamsu.types import ApprovalRequest


def _request(action_type: str, description: str = "do the thing") -> ApprovalRequest:
    return ApprovalRequest(action_type=action_type, description=description, risk_level="medium")


def test_is_auto_approvable_action_only_covers_file_write_and_edit():
    assert is_auto_approvable_action("file_write") is True
    assert is_auto_approvable_action("file_edit") is True
    assert is_auto_approvable_action("file_delete") is False
    assert is_auto_approvable_action("run_command") is False
    assert is_auto_approvable_action("web_search") is False
    assert is_auto_approvable_action("mcp_tool") is False


def test_permission_memory_session_scope_does_not_persist(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory.remember("file_edit", "session")

    assert memory.is_remembered("file_edit") is True
    assert not (tmp_path / ".shamsu" / "permissions.json").exists()

    reloaded = PermissionMemory(tmp_path)
    assert reloaded.is_remembered("file_edit") is False


def test_permission_memory_workspace_scope_persists_across_instances(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory.remember("file_write", "workspace")

    permissions_path = tmp_path / ".shamsu" / "permissions.json"
    assert permissions_path.exists()
    assert json.loads(permissions_path.read_text(encoding="utf-8")) == {
        "always_allow": ["file_write"]
    }

    reloaded = PermissionMemory(tmp_path)
    assert reloaded.is_remembered("file_write") is True


def test_permission_memory_forget_all_clears_session_and_workspace(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory.remember("file_edit", "session")
    memory.remember("file_write", "workspace")

    memory.forget_all()

    assert memory.is_remembered("file_edit") is False
    assert memory.is_remembered("file_write") is False
    assert PermissionMemory(tmp_path).is_remembered("file_write") is False


def test_permission_memory_list_remembered_reports_scope(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory.remember("file_edit", "session")
    memory.remember("file_write", "workspace")

    assert memory.list_remembered() == {"file_edit": "session", "file_write": "workspace"}


def test_approval_manager_auto_approves_remembered_file_edit(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory.remember("file_edit", "session")
    calls = []
    manager = ApprovalManager(approval_func=lambda _r: calls.append(1) or True, memory=memory)

    approved = manager.ask(_request("file_edit"))

    assert approved is True
    assert calls == []  # approval_func never called; auto-approved from memory


def test_approval_manager_never_auto_approves_run_command_even_if_remembered(tmp_path):
    memory = PermissionMemory(tmp_path)
    # Force a non-auto-approvable action type into memory directly, bypassing
    # the normal remember() flow, to prove the tier gate (not just the UI) enforces this.
    memory._session_remembered.add("run_command")
    calls = []
    manager = ApprovalManager(approval_func=lambda _r: calls.append(1) or True, memory=memory)

    approved = manager.ask(_request("run_command"))

    assert approved is True
    assert calls == [1]  # approval_func WAS called; no auto-approve for run_command


def test_approval_manager_never_auto_approves_file_delete_even_if_remembered(tmp_path):
    memory = PermissionMemory(tmp_path)
    memory._session_remembered.add("file_delete")
    calls = []
    manager = ApprovalManager(approval_func=lambda _r: calls.append(1) or True, memory=memory)

    manager.ask(_request("file_delete"))

    assert calls == [1]


def test_approval_manager_offers_remember_prompt_after_first_approval(tmp_path):
    memory = PermissionMemory(tmp_path)
    remember_calls = []
    manager = ApprovalManager(
        approval_func=lambda _r: True,
        memory=memory,
        remember_prompt=lambda action_type: remember_calls.append(action_type) or "session",
    )

    manager.ask(_request("file_edit"))

    assert remember_calls == ["file_edit"]
    assert memory.is_remembered("file_edit") is True


def test_approval_manager_remember_prompt_not_offered_for_run_command(tmp_path):
    remember_calls = []
    manager = ApprovalManager(
        approval_func=lambda _r: True,
        memory=PermissionMemory(tmp_path),
        remember_prompt=lambda action_type: remember_calls.append(action_type) or "session",
    )

    approved = manager.ask(_request("run_command"))

    assert approved is True
    assert remember_calls == []


def test_approval_manager_works_with_no_memory_at_all():
    """memory=None (the full default) must never crash and never auto-approve."""
    manager = ApprovalManager(approval_func=lambda _r: True)

    assert manager.ask(_request("file_edit")) is True


def test_approval_manager_remember_prompt_skipped_when_answer_is_none(tmp_path):
    memory = PermissionMemory(tmp_path)
    manager = ApprovalManager(
        approval_func=lambda _r: True,
        memory=memory,
        remember_prompt=lambda _action_type: "none",
    )

    manager.ask(_request("file_write"))

    assert memory.is_remembered("file_write") is False


def test_approval_manager_denied_request_never_triggers_remember_prompt(tmp_path):
    memory = PermissionMemory(tmp_path)
    remember_calls = []
    manager = ApprovalManager(
        approval_func=lambda _r: False,
        memory=memory,
        remember_prompt=lambda action_type: remember_calls.append(action_type) or "session",
    )

    approved = manager.ask(_request("file_edit"))

    assert approved is False
    assert remember_calls == []


def test_approval_manager_defaults_are_fully_backward_compatible():
    """No memory/remember_prompt passed => behaves exactly like the old ApprovalManager."""
    logged = []

    class FakeLogger:
        def log(self, event_type, payload, summary, workflow_id=None):
            logged.append(event_type)

    manager = ApprovalManager(approval_func=lambda _r: True, session_logger=FakeLogger())

    approved = manager.ask(_request("file_edit"))

    assert approved is True
    assert logged == ["approval.request", "approval.result"]


def test_ask_remember_choice_parses_session_workspace_and_none(monkeypatch):
    console = Console(file=StringIO(), force_terminal=False, width=100)

    monkeypatch.setattr("builtins.input", lambda _prompt="": "s")
    assert ask_remember_choice("file_edit", console=console) == "session"

    monkeypatch.setattr("builtins.input", lambda _prompt="": "w")
    assert ask_remember_choice("file_edit", console=console) == "workspace"

    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert ask_remember_choice("file_edit", console=console) == "none"

    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert ask_remember_choice("file_edit", console=console) == "none"
