"""The chat loop consults the Codebase-Memory MCP for files a turn names.

`CodeEditWorkflow` and `BugfixWorkflow` always did this. But the route table has
no `edit` entry and `file.write` sits above everything that reaches them, so
"edit core/views.py to ..." is dispatched to the chat loop instead - and the
code graph, however healthy and however fresh, was never asked. Live on
2026-08-03 the index was ready and in `external` retrieval mode for an entire
40-turn build and contributed nothing; meanwhile the model invented
`Bid(user=..., amount=...)` for a model whose fields are `bidder`/`bid_amount`,
which is exactly the question the graph answers.
"""
from __future__ import annotations

from pathlib import Path

import shamsu.agents.chat_loop as chat_loop_module
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse

BRIEF = (
    "Codebase-Memory MCP facts (prefer these over guessing):\n"
    "- core/views.py exports: home, item_detail\n"
    "- core/views.py imports: core/models.py"
)


class _LLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        return LLMResponse(raw="", model_used="fake")

    async def generate_structured(self, role, system, prompt, schema, **kwargs):  # noqa: ANN001
        return '{"needs_input": false}'


def _loop(workspace: Path) -> AgentChatLoop:
    return AgentChatLoop(
        workspace,
        client=None,
        tools=AgentToolRegistry(workspace, approval_func=lambda _r: True),
        llm=_LLM(),
    )


def _with_brief(monkeypatch, brief: str) -> list[list[str]]:
    """Stub the MCP lookup and record the target paths it was asked about."""
    seen: list[list[str]] = []

    def _fake(workspace_root, targets, service=None):  # noqa: ANN001
        seen.append(list(targets))
        # Mirrors the real builder, which returns "" for no targets.
        return brief if targets else ""

    monkeypatch.setattr("shamsu.abstract.context.build_codebase_memory_brief", _fake)
    return seen


def test_facts_about_a_named_existing_file_are_attached(tmp_path: Path, monkeypatch):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "views.py").write_text("def home(request):\n    pass\n", encoding="utf-8")
    seen = _with_brief(monkeypatch, BRIEF)

    result = _loop(tmp_path)._append_codebase_memory("edit core/views.py to add a listing form")

    assert BRIEF in result
    assert result.startswith("edit core/views.py to add a listing form")
    assert seen == [["core/views.py"]]


def test_a_request_naming_no_file_is_untouched(tmp_path: Path, monkeypatch):
    """And the MCP is not called at all - no healthcheck round-trip on an
    ordinary conversational turn."""
    seen = _with_brief(monkeypatch, BRIEF)

    assert _loop(tmp_path)._append_codebase_memory("say hello") == "say hello"
    assert seen == []


def test_creating_a_new_file_looks_nothing_up(tmp_path: Path, monkeypatch):
    """A file that does not exist yet has no facts to offer."""
    seen = _with_brief(monkeypatch, BRIEF)

    request = "create core/brand_new.py with a helper"
    assert _loop(tmp_path)._append_codebase_memory(request) == request
    assert seen == []


def test_an_empty_brief_changes_nothing(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _with_brief(monkeypatch, "")

    assert _loop(tmp_path)._append_codebase_memory("edit app.py") == "edit app.py"


def test_an_unavailable_service_never_breaks_the_turn(tmp_path: Path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("codebase-memory-mcp is not installed")

    monkeypatch.setattr("shamsu.abstract.context.build_codebase_memory_brief", _boom)

    assert _loop(tmp_path)._append_codebase_memory("edit app.py") == "edit app.py"


def test_it_runs_even_when_graphiti_memory_is_off(tmp_path: Path, monkeypatch):
    """The two answer different questions: cross-session recall vs. code
    structure. One being unavailable must not mute the other."""
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _with_brief(monkeypatch, BRIEF)

    loop = _loop(tmp_path)
    loop.use_long_term_memory = False

    assert BRIEF in loop._append_codebase_memory("edit app.py")


def test_the_run_loop_wires_it_in(tmp_path: Path):
    """Guard against the hook being defined but never called - the exact shape
    of the bug this fixes."""
    import inspect

    source = inspect.getsource(chat_loop_module.AgentChatLoop.run)

    assert "_append_codebase_memory" in source
