from __future__ import annotations

import sys
from pathlib import Path

from shamsu.tools.agent_tools import AgentToolRegistry

PASS_CMD = f'"{sys.executable}" -c "print(1)"'
FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def test_write_file_creates_and_reads_back(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("app.py", "print('hi')\n")

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_file_rejects_paths_outside_workspace(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("../escape.py", "hacked\n")

    assert result.ok is False
    assert not (tmp_path.parent / "escape.py").exists()


def test_list_files_reports_not_a_directory(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.list_files("app.py")

    assert result.ok is False


def test_run_command_omits_diagnostics_on_success(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(PASS_CMD)

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert "diagnostics" not in result.data


def test_run_command_surfaces_diagnostics_on_failure(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(FAIL_CMD)

    assert result.ok is False
    assert result.data["exit_code"] == 1
    assert "diagnostics" in result.data
    assert isinstance(result.data["diagnostics"], str)
    assert result.data["diagnostics"]
    assert FAIL_CMD.strip('"') in result.data["diagnostics"] or "exit 1" in result.data["diagnostics"]


def test_blocked_command_is_policy_outcome_and_clears_prior_error_packet(tmp_path: Path):
    registry = _registry(tmp_path)
    assert registry.run_command(FAIL_CMD).data["actionable"] is True

    blocked = registry.run_command("rm -rf /")

    assert blocked.ok is False
    assert blocked.data["outcome_classification"] == "policy_decision"
    assert blocked.data["actionable"] is False
    assert registry.command_runner.last_error_packet is None


def test_run_command_missing_command_is_rejected(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command("   ")

    assert result.ok is False
    assert result.data == {}


def test_read_only_run_command_blocks_shell_redirection(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_read_only(True)

    result = registry.run_command('python calc.py 2>&1 > output.txt')

    assert result.ok is False
    assert result.data["read_only"] is True
    assert not (tmp_path / "output.txt").exists()


def test_read_only_run_command_allows_nonwriting_execution(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_read_only(True)

    result = registry.run_command(PASS_CMD)

    assert result.ok is True


def test_find_file_returns_matching_candidates(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "admin" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
    (tmp_path / "admin" / "src" / "App.tsx").write_text("export default 2\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.find_file("App.tsx")

    assert result.ok is True
    assert set(result.data["candidates"]) == {"client/src/App.tsx", "admin/src/App.tsx"}


def test_grep_files_finds_content_locations(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\nNEEDLE here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.grep_files("NEEDLE")

    assert result.ok is True
    matches = result.data["matches"]
    assert len(matches) == 1
    assert matches[0]["file"] == "a.py"
    assert matches[0]["line"] == 2


def test_grep_files_ignores_vcs_and_vendor_dirs(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("NEEDLE\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.grep_files("NEEDLE")

    assert result.ok is True
    assert result.data["matches"] == []


def test_ask_user_returns_structured_pending_question(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.ask_user(
        "Which file should I read?",
        [{"label": "client/src/App.tsx", "description": "frontend"}, {"label": "admin/src/App.tsx"}],
        allow_free_text=True,
    )

    assert result.ok is True
    assert result.data["ask_user"] is True
    pending = result.data["pending_question"]
    assert pending["question"] == "Which file should I read?"
    assert [option["label"] for option in pending["options"]] == [
        "client/src/App.tsx",
        "admin/src/App.tsx",
    ]
    assert pending["awaiting"] == "user_input"


def test_ask_user_rejects_empty_question(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.ask_user("   ")

    assert result.ok is False


def test_execute_routes_new_tools(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 42\n", encoding="utf-8")
    registry = _registry(tmp_path)

    assert registry.execute("find_file", {"query": "app.py"}).ok is True
    assert registry.execute("grep_files", {"query": "value"}).ok is True
    assert registry.execute(
        "append_file",
        {"filepath": "app.py", "content": "extra = 7\n"},
    ).ok is True
    assert registry.execute("ask_user", {"question": "Which one?"}).data["ask_user"] is True


def test_tool_schemas_expose_clarification_and_discovery_tools(tmp_path: Path):
    registry = _registry(tmp_path)

    names = {schema["function"]["name"] for schema in registry.tool_schemas()}

    assert {"find_file", "grep_files", "append_file", "ask_user"}.issubset(names)
