from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from shamsu.action_ledger import store
from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.chat_loop import AgentLoopResult
from shamsu.cli import repl
from shamsu.routing.operations import recover_original_prompt


@pytest.mark.parametrize(
    ("prompt", "kinds"),
    [
        ("edit app.py, run it, then show the diff", ["mutation", "verify", "git_inspect"]),
        (
            "fix the failure, rerun the failed command, then summarize",
            ["mutation", "verify", "summarize"],
        ),
        ("read a.py and b.py, then compare them", ["read", "compare"]),
        ("search current docs, then update config.py", ["web", "mutation"]),
        (
            "create a project, run it, and return the local URL",
            ["mutation", "verify", "summarize"],
        ),
    ],
)
def test_required_compound_scenarios_build_ordered_dependencies(
    tmp_path: Path,
    prompt: str,
    kinds: list[str],
):
    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is True
    assert [step.kind for step in plan.steps] == kinds
    assert [step.depends_on for step in plan.steps] == [()] + [
        (index,) for index in range(1, len(kinds))
    ]
    assert repl._classify_route_label(prompt, tmp_path) == "composite"


def test_live_git_hijack_prompt_is_composite_with_git_last(tmp_path: Path):
    prompt = (
        "Fix the bug in qa_probe.py so add returns 5. "
        "Run a safe verification and report exactly what changed."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert [step.kind for step in plan.steps] == ["mutation", "verify", "git_inspect"]
    assert [step.route for step in plan.steps] == ["file.write", "agent-chat", "git"]
    assert plan.candidates[:2] == ("git", "file.write")


def test_pure_git_question_keeps_read_only_git_route(tmp_path: Path):
    assert repl._classify_route_label("what is git status", tmp_path) == "git"
    assert repl._operation_plan("what is git status", tmp_path).is_composite is False


def test_clear_single_file_write_keeps_fast_route(tmp_path: Path):
    assert repl._classify_route_label("create hello.py", tmp_path) == "file.write"


def test_special_single_run_routes_are_not_generalized(tmp_path: Path):
    (tmp_path / "index.html").write_text("<canvas></canvas>", encoding="utf-8")

    assert repl._classify_route_label("run the game", tmp_path) == "run_game"
    assert repl._classify_route_label("start the dev server", tmp_path) == "dev_server"


def test_explicit_prd_mention_summary_stays_prd_summary(tmp_path: Path):
    (tmp_path / "prd.pdf").write_bytes(b"not parsed by routing")

    plan = repl._operation_plan("summarize @prd.pdf", tmp_path)

    assert plan.is_composite is False
    assert plan.primary_route == "prd_summary"


def test_same_operation_clauses_keep_dedicated_routes(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    assert repl._classify_route_label(
        "Use read_file on app.py. Then explain its value.", tmp_path
    ) == "file.read"
    assert repl._classify_route_label(
        "what is the weather today? Please check on the web for this.", tmp_path
    ) == "web"


def test_colloquial_checkout_prd_keeps_summary_route(tmp_path: Path):
    (tmp_path / "Product Requirements Document.md").write_text("# App", encoding="utf-8")
    prompt = "can you checkout the prd and tell me what is the project about?"

    assert repl._classify_route_label(prompt, tmp_path) == "prd_summary"


def test_clarification_resume_recovers_original_composite_request(tmp_path: Path):
    original = "edit app.py, run it, then show the diff"
    expanded = repl._operation_plan(original, tmp_path).agent_prompt()
    resumed = expanded + '\n\n(Answering the earlier question "Which file?": app.py)'

    recovered = recover_original_prompt(resumed)
    rebuilt = repl._operation_plan(recovered, tmp_path)

    assert recovered.startswith(original)
    assert "app.py" in recovered
    assert [step.kind for step in rebuilt.steps] == ["mutation", "verify", "git_inspect"]


@pytest.mark.asyncio
async def test_composite_dispatch_passes_ordered_plan_to_agent(tmp_path: Path, monkeypatch):
    captured: dict[str, object] = {}

    class _Orchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, prompt):
            return SimpleNamespace(handled=False, effective_input=prompt, context="", action="")

    async def _fake_composite(plan, workspace, console, **kwargs):
        captured["plan"] = plan
        captured["workspace"] = workspace

    monkeypatch.setattr(repl, "AgentOrchestrator", _Orchestrator)
    monkeypatch.setattr(repl, "_run_composite_request", _fake_composite)
    await repl._handle_request(
        "edit app.py, run it, then show the diff",
        tmp_path,
        Console(record=True),
        object(),
        object(),
    )

    assert captured["workspace"] == tmp_path
    assert [step.kind for step in captured["plan"].steps] == [
        "mutation",
        "verify",
        "git_inspect",
    ]


@pytest.mark.asyncio
async def test_skipped_followup_finalizes_as_partial(tmp_path: Path, monkeypatch):
    ledger = start_run(tmp_path, "edit app.py then show the diff")
    set_current_run(ledger)

    async def _fake_agent(*args, **kwargs):
        call_id = ledger.log_tool_call("edit_file", {"filepath": "app.py"})
        ledger.log_tool_result(call_id, "edit_file", True, "edited")
        return AgentLoopResult(final="Edited app.py.", changed_files=("app.py",))

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("edit app.py, then show the diff", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))

        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert summary["status"] == "partial"
    events = store.load_events(tmp_path, ledger.run_id)
    finished = [event for event in events if event["type"] == "operation_step_finished"]
    assert [event["status"] for event in finished] == ["success", "not_run"]
    assert "Composite execution status: partial" in store.load_final_output(tmp_path, ledger.run_id)


@pytest.mark.asyncio
async def test_all_composite_steps_have_evidence_and_finalize_success(tmp_path: Path, monkeypatch):
    ledger = start_run(tmp_path, "edit app.py, run it, then show the diff")
    set_current_run(ledger)

    async def _fake_agent(*args, **kwargs):
        for tool_name in ("edit_file", "run_command", "git_diff"):
            call_id = ledger.log_tool_call(tool_name, {})
            ledger.log_tool_result(call_id, tool_name, True, "ok")
        ledger.log_event("verification_passed", command="python -m py_compile app.py")
        return AgentLoopResult(final="All requested steps completed.", changed_files=("app.py",))

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("edit app.py, run it, then show the diff", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert summary["status"] == "success"
    events = store.load_events(tmp_path, ledger.run_id)
    finished = [event for event in events if event["type"] == "operation_step_finished"]
    assert [event["status"] for event in finished] == ["success", "success", "success"]
    assert any(event["type"] == "composite_completed" for event in events)


@pytest.mark.asyncio
async def test_dispatch_decision_records_candidates_and_sequence(tmp_path: Path, monkeypatch):
    ledger = start_run(tmp_path, "edit app.py then show the diff")
    set_current_run(ledger)

    class _Orchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, prompt):
            return SimpleNamespace(handled=False, effective_input=prompt, context="", action="")

    async def _fake_composite(*args, **kwargs):
        return None

    monkeypatch.setattr(repl, "AgentOrchestrator", _Orchestrator)
    monkeypatch.setattr(repl, "_run_composite_request", _fake_composite)
    try:
        await repl._handle_request(
            "edit app.py, then show the diff",
            tmp_path,
            Console(record=True),
            object(),
            object(),
        )
    finally:
        clear_current_run()

    decision = store.load_decisions(tmp_path, ledger.run_id)[0]
    assert decision["chosen_action"] == "composite"
    assert any(item.startswith("route_candidates:") for item in decision["evidence"])
    assert "operation_sequence:1:mutation,2:git_inspect" in decision["evidence"]


def test_git_error_output_is_rendered_as_literal_text(tmp_path: Path):
    class _Registry:
        def __init__(self, *args, **kwargs):
            pass

        def execute(self, name, arguments):
            return SimpleNamespace(
                ok=False,
                message="bad [/<m>] markup",
                data={"stderr": "bad [/<m>] markup"},
            )

    console = Console(record=True)
    original = repl.AgentToolRegistry
    try:
        repl.AgentToolRegistry = _Registry
        repl._run_git_read_only("show changes", tmp_path, console, None, lambda _text: None)
    finally:
        repl.AgentToolRegistry = original

    assert "bad [/<m>] markup" in console.export_text()


def test_read_only_constraint_does_not_become_a_mutation_step(tmp_path: Path):
    prompt = "Where is the add function defined and what does it return? Do not change any files."

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert plan.steps[0].kind == "answer"
    assert plan.steps[0].route == "qa"


def test_location_prefix_does_not_orphan_the_edit(tmp_path: Path):
    """"In calc.py, change X" used to split at the comma into a bogus "In
    calc.py" step plus an edit clause with no filename - which then could not
    route to file.write and fell into the tool-less QA brain. It must stay one
    instruction. Observed live 2026-07-21: the fix never touched the file."""
    prompt = "In calc.py, change the subtract body from 'return a + b' to 'return a - b'."

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert "calc.py" in plan.steps[0].instruction


def test_descriptive_context_sentence_is_not_its_own_step(tmp_path: Path):
    prompt = (
        "There is a bug in calc.py: the subtract function adds instead of "
        "subtracting. Fix it so subtract returns a - b."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert "calc.py" in plan.steps[0].instruction


def test_genuine_multi_action_prompt_still_splits(tmp_path: Path):
    """The merge must not swallow real, separately-actionable steps."""
    plan = repl._operation_plan("read calc.py and then run the tests", tmp_path)

    assert plan.is_composite is True
    assert [s.kind for s in plan.steps] == ["read", "verify"]


@pytest.mark.asyncio
async def test_second_mutation_step_is_not_credited_for_the_first(tmp_path: Path, monkeypatch):
    """The dogfood disaster, pinned: "edit greet() AND update __main__" changed
    greet() only, never touched __main__, yet reported "Step 2: success" because
    the old executor judged every mutation step from GLOBAL changed_files. With
    per-step turns, step 2 only writes if ITS turn writes."""
    ledger = start_run(tmp_path, "edit greet in greeting.py and update the __main__ block")
    set_current_run(ledger)

    calls = {"n": 0}

    async def _fake_agent(prompt, *args, **kwargs):
        calls["n"] += 1
        # Step 1 actually edits; step 2 does NOTHING (the real failure mode).
        if calls["n"] == 1:
            call_id = ledger.log_tool_call("edit_file", {"filepath": "greeting.py"})
            ledger.log_tool_result(call_id, "edit_file", True, "edited greet")
            return AgentLoopResult(final="Edited greet().", changed_files=("greeting.py",))
        return AgentLoopResult(final="Here is what step 2 would do...", changed_files=())

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan(
            "edit greet in greeting.py, and update the __main__ block to call greet", tmp_path
        )
        assert [s.kind for s in plan.steps] == ["mutation", "mutation"]
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert calls["n"] == 2, "each step must get its own agent turn"
    events = store.load_events(tmp_path, ledger.run_id)
    finished = [e for e in events if e["type"] == "operation_step_finished"]
    assert [e["status"] for e in finished] == ["success", "not_run"]
    assert summary["status"] == "partial"


@pytest.mark.asyncio
async def test_composite_stops_after_a_step_asks_the_user(tmp_path: Path, monkeypatch):
    """A step awaiting input must not let later steps run blind on a guess."""
    ledger = start_run(tmp_path, "edit app.py, then run it")
    set_current_run(ledger)

    calls = {"n": 0}

    async def _fake_agent(*args, **kwargs):
        calls["n"] += 1
        return AgentLoopResult(final="Which value?", awaiting_user=True)

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("edit app.py, then run it", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
    finally:
        clear_current_run()

    assert calls["n"] == 1, "must not run step 2 while step 1 awaits the user"
    events = store.load_events(tmp_path, ledger.run_id)
    finished = [e for e in events if e["type"] == "operation_step_finished"]
    assert finished[-1]["status"] == "not_run"
