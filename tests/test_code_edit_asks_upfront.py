"""Gap J1 (code-edit half): the mutating workflows computed the planner's
"this needs the user's decision" verdict and silently ignored it - only the
chat loop ever acted on it. CodeEditWorkflow now stops BEFORE generating a
diff against a guess, and the REPL turns that into the same cross-turn
pending question every other ask uses.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.session.manager import SessionManager
from shamsu.types import LLMResponse


class _DecidingLLM:
    """Free-text planner + a decision call that says: ask the user."""

    def __init__(self, needs_input: bool) -> None:
        self._needs_input = needs_input
        self.coder_calls = 0

    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        if specialist == "coder":
            self.coder_calls += 1
            return LLMResponse(raw="--- a/x.py\n+++ b/x.py\n", model_used="fake")
        return LLMResponse(raw="short plan", model_used="fake")

    async def generate_structured(self, role, system, prompt, schema, **kwargs):  # noqa: ANN001
        return json.dumps(
            {
                "needs_input": self._needs_input,
                "question": "Sessions or JWT?",
                "options": [{"label": "Sessions"}, {"label": "JWT"}],
            }
        )


class _NoHitsSearch:
    def search(self, query, top_k=8, boost_paths=None):  # noqa: ANN001
        return []


def test_workflow_stops_before_the_coder_when_a_decision_is_needed(tmp_path: Path):
    llm = _DecidingLLM(needs_input=True)
    result = asyncio.run(
        CodeEditWorkflow(tmp_path, search=_NoHitsSearch(), llm=llm).run("add auth")
    )

    assert result.needs_input is True
    assert result.question == "Sessions or JWT?"
    assert llm.coder_calls == 0, "no diff may be generated against a guess"
    assert result.applied is False


def test_workflow_proceeds_when_no_decision_is_needed(tmp_path: Path):
    from shamsu.patch.engine import PatchEngine
    from shamsu.safety.approval_manager import ApprovalManager

    llm = _DecidingLLM(needs_input=False)
    # Deny-all approvals: this test only asserts the CODER ran (i.e. the
    # decision gate let the work proceed), not that a patch applied - a real
    # approval prompt would block on stdin under pytest.
    engine = PatchEngine(tmp_path, approval_manager=ApprovalManager(lambda _r: False, None))
    result = asyncio.run(
        CodeEditWorkflow(tmp_path, search=_NoHitsSearch(), llm=llm, patch_engine=engine).run("add auth")
    )

    assert result.needs_input is False
    # >= 1: a denied patch legitimately falls back to a second coder call
    # (full rewrite); the point here is only that the gate let work START.
    assert llm.coder_calls >= 1


def test_repl_turns_the_stop_into_a_pending_question(tmp_path: Path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("EditAsk")

    class _StubWorkflow:
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

        async def run(self, request):  # noqa: ANN001
            from shamsu.agents.code_edit_workflow import CodeEditResult
            from shamsu.types import ContextPack

            return CodeEditResult(
                request=request,
                pack=ContextPack(task_id="t", step_id=1, specialist="coder", user_request=request),
                needs_input=True,
                question="Sessions or JWT?",
                options=[{"label": "Sessions", "description": ""}],
            )

    monkeypatch.setattr(repl, "CodeEditWorkflow", _StubWorkflow)
    console = Console(record=True, width=100)

    asyncio.run(
        repl._run_code_edit("add auth to app.py", tmp_path, None, console, llm=None, session_logger=logger)
    )

    out = console.export_text()
    assert "Sessions or JWT?" in out
    assert "1. Sessions" in out
    pending = logger.get_pending_question()
    assert pending["question"] == "Sessions or JWT?"
    assert pending["created_from_prompt"] == "add auth to app.py"
    assert pending["source"] == "code_edit_upfront"
