"""Structured generation must actually come back.

Live 2026-08-22, session 20260822-090221-f144 on qwen3.5:9b-q4_K_M / Ollama
0.31.1: two consecutive `/plan` runs wrote a plan file whose Steps section read
`_No steps were produced._`, while the CLI printed the model's reasoning and the
reasoning WAS the complete plan JSON. `generate_structured` had returned "".

The cause is that omitting `think` is not neutral. This model defaults to
thinking ON, and a `format` grammar constrains the `response` channel only - so
asked for JSON while thinking, it satisfies the schema inside `thinking` and
returns an empty `response`. Measured against the real PLAN_SCHEMA:

    think omitted -> response    0   thinking  624
    think: true   -> response    0   thinking  667
    think: false  -> response  561   thinking    0    (2 steps)

SHAMSU sent the first two shapes and never the third, so EVERY schema-
constrained call on the default tier's own anchor came back empty: the planner,
the router, the PRD development plan, the repair proposer, the scaffold filler.
"""
from __future__ import annotations

import json

import pytest

from shamsu.runtime.models import TIER_MODEL_SPECS, ModelTier

#: The default tier's thinking anchor, by lookup rather than by name - the
#: anchor has been renamed once already and took 20 tests with it.
ANCHOR = TIER_MODEL_SPECS[ModelTier.DEFAULT][0].name
NOT_REASONING = "qwen2.5-coder:7b-instruct"

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "steps": {"type": "array"}},
    "required": ["title"],
}


class _CapturedStream:
    """Stands in for `_stream_once`, keeping every payload it was sent."""

    def __init__(self, response: str = "{}", thinking: str = "") -> None:
        self.payloads: list[dict] = []
        self.response = response
        self.thinking = thinking

    async def __call__(self, model, payload, on_token=None, on_progress=None):
        self.payloads.append(payload)
        return self.response, self.thinking, 0

    @property
    def last(self) -> dict:
        return self.payloads[-1]


def _manager(monkeypatch, stream: _CapturedStream):
    from shamsu.llm import manager as manager_module

    # A model remembered as rejecting `think` would skip the branch under test.
    monkeypatch.setattr(manager_module, "_THINK_UNSUPPORTED", set())
    llm = manager_module.LLMManager()
    monkeypatch.setattr(llm, "_stream_once", stream)
    return llm


# -- the flag ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_schema_constrained_call_says_think_false_out_loud(monkeypatch):
    """The whole bug in one assertion: the key must be PRESENT and False.

    Absent means "use your default", and this model's default is to think.
    """
    stream = _CapturedStream()
    llm = _manager(monkeypatch, stream)

    await llm._generate(ANCHOR, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert "think" in stream.last, "omitting the flag is what emptied the planner"
    assert stream.last["think"] is False


@pytest.mark.asyncio
async def test_an_unconstrained_call_still_gets_to_think(monkeypatch):
    """The fix must not cost chain-of-thought on ordinary generation - that is
    the reasoning anchor's whole reason for being the default."""
    stream = _CapturedStream()
    llm = _manager(monkeypatch, stream)

    await llm._generate(ANCHOR, "sys", "prompt", _role="planner")

    assert stream.last["think"] is True


@pytest.mark.asyncio
async def test_a_mechanical_role_is_told_false_rather_than_left_to_default(monkeypatch):
    """`role_should_think("router") is False` was already true and already
    meant to skip the chain-of-thought. It skipped SENDING the flag, which on
    this model asked for exactly the thing it was avoiding."""
    stream = _CapturedStream()
    llm = _manager(monkeypatch, stream)

    await llm._generate(ANCHOR, "sys", "prompt", _role="router")

    assert stream.last["think"] is False


@pytest.mark.asyncio
async def test_a_non_reasoning_model_is_not_sent_the_flag_at_all(monkeypatch):
    """It has no thinking channel to turn off, and `think` is not a key every
    build accepts - sending it buys a 400 for nothing."""
    stream = _CapturedStream()
    llm = _manager(monkeypatch, stream)

    await llm._generate(NOT_REASONING, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert "think" not in stream.last


@pytest.mark.asyncio
async def test_a_model_that_rejects_the_flag_still_gets_its_answer(monkeypatch):
    """One 400 per model per process, then plain payloads - the retry that was
    already there for `think: true` has to cover `think: false` too."""
    import httpx

    from shamsu.llm import manager as manager_module

    monkeypatch.setattr(manager_module, "_THINK_UNSUPPORTED", set())
    llm = manager_module.LLMManager()
    payloads: list[dict] = []

    async def stream_once(model, payload, on_token=None, on_progress=None):
        payloads.append(payload)
        if "think" in payload:
            raise httpx.HTTPStatusError(
                "no think here",
                request=httpx.Request("POST", "http://localhost:11434/api/generate"),
                response=httpx.Response(400),
            )
        return '{"title": "ok"}', "", 0

    monkeypatch.setattr(llm, "_stream_once", stream_once)
    text = await llm._generate(ANCHOR, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert text == '{"title": "ok"}'
    assert "think" not in payloads[-1]
    assert ANCHOR in manager_module._THINK_UNSUPPORTED


# -- the safety net ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_structured_response_is_recovered_from_the_thinking_channel(
    monkeypatch,
):
    """The exact shape of the reported failure: schema asked for, response
    empty, complete JSON sitting in the reasoning trace."""
    plan = json.dumps({"title": "Fix Movement Math", "steps": [{"description": "a"}]})
    stream = _CapturedStream(response="", thinking=plan)
    llm = _manager(monkeypatch, stream)

    text = await llm._generate(ANCHOR, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert json.loads(text)["steps"], "the plan was in the trace and still got dropped"


@pytest.mark.asyncio
async def test_salvage_never_overrides_an_answer_that_arrived(monkeypatch):
    """A model that reasons AND answers must keep its answer. Reading the trace
    over the response would swap a final decision for the thinking behind it."""
    stream = _CapturedStream(
        response='{"title": "the answer"}',
        thinking='{"title": "an idea I discarded"}',
    )
    llm = _manager(monkeypatch, stream)

    text = await llm._generate(ANCHOR, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert json.loads(text)["title"] == "the answer"


@pytest.mark.asyncio
async def test_salvage_does_not_invent_json_out_of_prose(monkeypatch):
    """An honestly empty answer must stay empty. `repair_json` returns "" or
    "{}" for text with no JSON in it, and passing that off as a result would
    turn "the model said nothing" into "the model returned an empty plan"."""
    stream = _CapturedStream(response="", thinking="I am not sure how to do this.")
    llm = _manager(monkeypatch, stream)

    text = await llm._generate(ANCHOR, "sys", "prompt", json_schema=SCHEMA, _role="planner")

    assert text.strip() == ""


@pytest.mark.asyncio
async def test_salvage_leaves_unstructured_calls_alone(monkeypatch):
    """No schema was asked for, so an empty answer is an empty answer and the
    trace is a trace. Chat already has its own <think> handling."""
    stream = _CapturedStream(response="", thinking='{"title": "not an answer"}')
    llm = _manager(monkeypatch, stream)

    text = await llm._generate(ANCHOR, "sys", "prompt", _role="planner")

    assert text == ""


# -- end to end through the planner ----------------------------------------


@pytest.mark.asyncio
async def test_the_planner_produces_steps_when_the_model_answers_in_its_trace(
    tmp_path, monkeypatch
):
    """The user-visible failure, from the top: `/plan` wrote a file saying
    `_No steps were produced._` twice while the model had produced six."""
    from shamsu.agents.plan_mode import PlanningWorkflow

    plan = json.dumps(
        {
            "title": "Fix Movement Math",
            "steps": [
                {"description": "Review the movement math", "target_file": "js/PlayerShip.js"},
                {"description": "Correct the velocity integration", "target_file": "js/main.js"},
            ],
        }
    )
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "PlayerShip.js").write_text("// ship\n", encoding="utf-8")
    (tmp_path / "js" / "main.js").write_text("// main\n", encoding="utf-8")

    from shamsu.llm import manager as manager_module

    monkeypatch.setattr(manager_module, "_THINK_UNSUPPORTED", set())
    llm = manager_module.LLMManager()
    monkeypatch.setattr(llm, "_stream_once", _CapturedStream(response="", thinking=plan))
    monkeypatch.setattr(manager_module, "model_for_role", lambda role: ANCHOR)

    workflow = PlanningWorkflow(tmp_path, llm=llm)
    doc = await workflow.run("fix the movement math", route="bug_fix")

    assert len(doc.steps) == 2
    assert "_No steps were produced._" not in doc.markdown
