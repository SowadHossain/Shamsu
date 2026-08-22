from __future__ import annotations

import json
import types
from io import StringIO

from rich.console import Console

import shamsu.safety.approval as approval_module
from shamsu.safety.approval import ask_approval_menu, ask_remember_choice
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.commands import is_auto_approvable_action
from shamsu.safety.permission_store import PermissionMemory
from shamsu.cli.repl import _install_console_status_tracker
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
        "schema_version": 2,
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


def test_approval_manager_logs_complete_request_and_decision_scope(tmp_path):
    events = []

    class FakeLogger:
        def log(self, event_type, payload, summary, workflow_id=None):
            events.append((event_type, payload))

    request = ApprovalRequest(
        action_type="file_edit",
        description="Edit settings.py",
        risk_level="medium",
        preview="- old\n+ new",
        working_dir="C:/workspace",
        reason="Apply the requested fix.",
        target_paths=["settings.py"],
    )
    manager = ApprovalManager(
        session_logger=FakeLogger(),
        memory=PermissionMemory(tmp_path),
        menu_prompt=lambda _request, _remember: (True, "workspace"),
    )

    assert manager.ask(request) is True

    result = events[1][1]
    assert result["request"]["target_paths"] == ["settings.py"]
    assert result["request"]["working_dir"] == "C:/workspace"
    assert result["request"]["risk_level"] == "medium"
    assert result["decision_scope"] == "workspace"
    assert result["decision_source"] == "interactive_menu"


def _menu_console() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=100)


def test_ask_approval_menu_accepts_semantic_allow_once(monkeypatch):
    console = _menu_console()
    for answer in ("y", "yes"):
        monkeypatch.setattr("builtins.input", lambda _prompt="", a=answer: a)
        approved, scope = ask_approval_menu(_request("file_write"), offer_remember=False, console=console)
        assert approved is True
        assert scope == "none"


def test_ask_approval_menu_numeric_input_is_never_inferred(monkeypatch):
    console = _menu_console()
    for answer in ("1", "2", "3"):
        monkeypatch.setattr("builtins.input", lambda _prompt="", a=answer: a)
        approved, scope = ask_approval_menu(
            _request("file_write"), offer_remember=True, console=console
        )
        assert approved is False
        assert scope == "none"


def test_ask_approval_menu_a_remembers_only_when_offered(monkeypatch):
    """`a` REMEMBERS only when remembering was offered - but it always ALLOWS.

    The second half of this used to assert `approved is False`, which is the
    bug it was guarding written down as the expectation: the single-key reader
    prints "press a to always allow" and accepts `a` whatever is on offer, so
    the key the user was told to press silently refused the action. See
    tests/test_approval_bug.py."""
    console = _menu_console()
    monkeypatch.setattr("builtins.input", lambda _prompt="": "a")
    approved, scope = ask_approval_menu(_request("file_write"), offer_remember=True, console=console)
    assert approved is True
    assert scope == "workspace"
    approved, scope = ask_approval_menu(_request("run_command"), offer_remember=False, console=console)
    assert approved is True
    assert scope == "none", "nothing was offered, so nothing may be remembered"
    rendered = console.file.getvalue()
    assert "[y] Allow once" in rendered
    assert "[a] Always allow" in rendered
    assert "[n] Deny" in rendered


def test_ask_approval_menu_empty_or_garbage_defaults_to_no(monkeypatch):
    console = _menu_console()
    for answer in ("", "nope", "5", "n", "no"):
        monkeypatch.setattr("builtins.input", lambda _prompt="", a=answer: a)
        approved, _scope = ask_approval_menu(_request("file_write"), offer_remember=True, console=console)
        assert approved is False


def test_ask_approval_menu_empty_tty_read_denies_immediately(monkeypatch):
    console = _menu_console()
    calls = []
    monkeypatch.setattr("builtins.input", lambda _prompt="": calls.append(True) or "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    approved, scope = ask_approval_menu(_request("run_command"), offer_remember=False, console=console)

    assert approved is False
    assert scope == "none"
    assert calls == [True]


def test_ask_approval_menu_cancels_on_eof(monkeypatch):
    console = _menu_console()

    def raise_eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    approved, scope = ask_approval_menu(_request("run_command"), offer_remember=False, console=console)

    assert approved is False
    assert scope == "none"
    assert "Action cancelled" in console.file.getvalue()


def test_ask_approval_menu_windows_console_fallback_accepts_key(monkeypatch):
    out = StringIO()
    console = Console(file=out, force_terminal=True, width=100)

    def raise_eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    monkeypatch.setattr(approval_module.sys, "platform", "win32")
    monkeypatch.setitem(
        approval_module.sys.modules,
        "msvcrt",
        types.SimpleNamespace(getwch=lambda: "y"),
    )

    approved, scope = ask_approval_menu(_request("run_command"), offer_remember=False, console=console)

    assert approved is True
    assert scope == "none"
    rendered = out.getvalue()
    # The hint names what is actually on offer. It used to say "press a to
    # always allow when offered" under a menu with no `a` in it, and `a` then
    # meant Deny - see tests/test_approval_bug.py.
    assert "Press y to allow, or n to deny." in rendered
    assert "[a]" not in rendered
    assert "Action cancelled" not in rendered


def test_ask_approval_menu_windows_console_fallback_rejects_key(monkeypatch):
    console = Console(file=StringIO(), force_terminal=True, width=100)

    def raise_eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    monkeypatch.setattr(approval_module.sys, "platform", "win32")
    monkeypatch.setitem(
        approval_module.sys.modules,
        "msvcrt",
        types.SimpleNamespace(getwch=lambda: "n"),
    )

    approved, scope = ask_approval_menu(_request("run_command"), offer_remember=False, console=console)

    assert approved is False
    assert scope == "none"


def test_ask_approval_menu_pauses_tracked_status_before_input(monkeypatch):
    console = _menu_console()
    _install_console_status_tracker(console)
    live_states_at_input = []

    def fake_input(_prompt=""):
        active = getattr(console, "_shamsu_active_statuses", [])
        assert active
        live_states_at_input.append(active[-1]._live.is_started)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    with console.status("thinking"):
        approved, scope = ask_approval_menu(
            _request("file_write"), offer_remember=False, console=console
        )

    assert approved is True
    assert scope == "none"
    assert live_states_at_input == [False]


def test_approval_manager_menu_prompt_folds_remember_into_one_prompt(tmp_path):
    memory = PermissionMemory(tmp_path)
    calls = []

    def fake_menu(request, offer_remember):
        calls.append((request.action_type, offer_remember))
        return True, "workspace"

    manager = ApprovalManager(memory=memory, menu_prompt=fake_menu)

    assert manager.ask(_request("file_write")) is True
    # offered remember (auto-approvable + memory), and it stuck for next time.
    assert calls == [("file_write", True)]
    assert memory.is_remembered("file_write")
    # Second identical request is auto-approved without hitting the menu again.
    assert manager.ask(_request("file_write")) is True
    assert len(calls) == 1


def test_approval_manager_menu_prompt_never_offers_remember_for_run_command(tmp_path):
    memory = PermissionMemory(tmp_path)
    calls = []

    def fake_menu(request, offer_remember):
        calls.append((request.action_type, offer_remember))
        return True, "workspace"

    manager = ApprovalManager(memory=memory, menu_prompt=fake_menu)

    assert manager.ask(_request("run_command")) is True
    assert calls == [("run_command", False)]
    # A "workspace" scope returned for a non-auto-approvable action is ignored.
    assert not memory.is_remembered("run_command")


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
