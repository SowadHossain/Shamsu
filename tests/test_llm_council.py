from __future__ import annotations

import pytest

from shamsu.llm.council import run_council, should_convene_council
from shamsu.types import ContextPack, LLMResponse, RoutingDecision


class FakeCouncilLLM:
    """Fake ILLMManager returning scripted responses per specialist call."""

    def __init__(self, responses_by_specialist: dict[str, list[str]]):
        self.responses_by_specialist = {k: list(v) for k, v in responses_by_specialist.items()}
        self.calls: list[tuple[str, str]] = []  # (specialist, user_request)

    async def route(self, prompt: str, project_summary: str) -> RoutingDecision:
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append((specialist, pack.user_request))
        raw = self.responses_by_specialist[specialist].pop(0)
        return LLMResponse(raw=raw, model_used=f"fake-{specialist}")


def _pack(request: str = "fix the bug") -> ContextPack:
    return ContextPack(task_id="t1", step_id=1, specialist="coder", user_request=request)


@pytest.mark.asyncio
async def test_run_council_skips_reconcile_when_critique_is_clean():
    llm = FakeCouncilLLM({"coder": ["draft output"], "reviewer": ["No issues found."]})

    result = await run_council(llm, _pack(), specialist="coder")

    assert result.reconciled is False
    assert result.final.raw == "draft output"
    assert [call[0] for call in llm.calls] == ["coder", "reviewer"]


@pytest.mark.asyncio
async def test_run_council_reconciles_when_critique_flags_an_issue():
    llm = FakeCouncilLLM({
        "coder": ["draft output", "corrected output"],
        "reviewer": ["This has a bug: off-by-one error."],
    })

    result = await run_council(llm, _pack(), specialist="coder")

    assert result.reconciled is True
    assert result.final.raw == "corrected output"
    assert [call[0] for call in llm.calls] == ["coder", "reviewer", "coder"]


@pytest.mark.asyncio
async def test_run_council_calls_draft_critique_reconcile_in_order():
    llm = FakeCouncilLLM({
        "coder": ["draft", "reconciled"],
        "reviewer": ["risk: missing validation"],
    })

    await run_council(llm, _pack("add validation"), specialist="coder")

    specialists_called = [call[0] for call in llm.calls]
    assert specialists_called == ["coder", "reviewer", "coder"]
    # the reconcile call's request must reference both the draft and critique
    reconcile_request = llm.calls[2][1]
    assert "draft" in reconcile_request
    assert "missing validation" in reconcile_request


@pytest.mark.asyncio
async def test_run_council_logs_events_when_session_logger_provided():
    llm = FakeCouncilLLM({"coder": ["draft output"], "reviewer": ["No issues found."]})

    class RecordingLogger:
        def __init__(self):
            self.events = []

        def log(self, event_type, payload, summary, workflow_id=None):
            self.events.append(event_type)

    logger = RecordingLogger()
    await run_council(llm, _pack(), specialist="coder", session_logger=logger)

    assert logger.events == ["council.draft", "council.critique"]


def test_should_convene_council_triggers_on_low_confidence():
    routing = RoutingDecision(intent="qa", complexity="single", confidence=0.2)

    assert should_convene_council(routing=routing) is True


def test_should_convene_council_does_not_trigger_on_high_confidence():
    routing = RoutingDecision(intent="qa", complexity="single", confidence=0.9)

    assert should_convene_council(routing=routing) is False


def test_should_convene_council_triggers_on_destructive_action_kind():
    assert should_convene_council(action_kind="file_delete") is True
    assert should_convene_council(action_kind="run_command") is True
    assert should_convene_council(action_kind="file_edit") is False


def test_should_convene_council_triggers_on_security_sensitive_path():
    assert should_convene_council(target_paths=["shamsu/safety/approval.py"]) is True
    assert should_convene_council(target_paths=["app/settings.py"]) is True
    assert should_convene_council(target_paths=[".env"]) is True
    assert should_convene_council(target_paths=["app/views.py"]) is False


def test_should_convene_council_handles_windows_style_paths():
    assert should_convene_council(target_paths=[r"shamsu\safety\approval.py"]) is True


def test_should_convene_council_false_by_default():
    assert should_convene_council() is False
