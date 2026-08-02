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


def test_package_install_and_import_probe_are_separate_mutating_and_verify_steps(
    tmp_path: Path,
):
    prompt = (
        "Install boltons==24.0.0 for this Python project. Then verify it imports "
        "and report the exact Python executable and boltons module path used. "
        "Do not use the global Python environment."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert [step.kind for step in plan.steps] == ["mutation", "verify", "summarize"]
    assert plan.steps[0].instruction == "Install boltons==24.0.0 for this Python project"
    assert "Do not use the global Python environment" in plan.steps[2].instruction
    command = repl._composite_verification_command(plan, plan.steps[1])
    assert command.startswith("python -c ")
    assert "import boltons, sys" in command
    assert "boltons.__file__" in command


def test_single_package_install_routes_to_tool_calling_agent(tmp_path: Path):
    prompt = "Install boltons==24.0.0 in this Python project."

    assert repl._classify_route_label(prompt, tmp_path) == "package.install"


def test_named_file_creation_keeps_followup_content_directives_in_one_turn(tmp_path: Path):
    prompt = (
        "Create SCOPE_NOTES.md from the retained documentation. "
        "Add an Out of Scope heading, list excluded features, and cite the source page."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert plan.primary_route == "file.write"
    assert plan.steps[0].instruction.startswith("Create SCOPE_NOTES.md")
    assert "cite the source page" in plan.steps[0].instruction


def test_exactly_one_file_keeps_pronoun_based_content_directives_atomic(tmp_path: Path):
    prompt = (
        "Create exactly one file: `src/schema.py`. "
        "Use the write tool immediately. "
        "Implement the complete domain model in this one file with validation and timestamps."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert len(plan.steps) == 1
    assert plan.primary_route == "file.write"
    assert "complete domain model" in plan.steps[0].instruction


def test_single_file_creation_still_splits_required_verification(tmp_path: Path):
    plan = repl._operation_plan("Create app.py, then run it", tmp_path)

    assert [step.kind for step in plan.steps] == ["mutation", "verify"]


def test_special_single_run_routes_are_not_generalized(tmp_path: Path):
    (tmp_path / "index.html").write_text("<canvas></canvas>", encoding="utf-8")

    assert repl._classify_route_label("run the game", tmp_path) == "run_game"
    assert repl._classify_route_label("start the dev server", tmp_path) == "dev_server"


def test_explicit_prd_mention_summary_stays_prd_summary(tmp_path: Path):
    (tmp_path / "prd.pdf").write_bytes(b"not parsed by routing")

    plan = repl._operation_plan("summarize @prd.pdf", tmp_path)

    assert plan.is_composite is False
    assert plan.primary_route == "prd_summary"


def test_prd_implementation_with_acceptance_uses_dedicated_build_route(tmp_path: Path):
    (tmp_path / "PRD.md").write_text("# Converter", encoding="utf-8")
    prompt = (
        "Read PRD.md and implement the requested CLI in converter.py. "
        "Then run the acceptance commands from the PRD."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert plan.primary_route == "prd.build"


def test_prd_acceptance_extraction_preserves_commands_and_expected_stdout():
    text = (
        "## Acceptance\n"
        "- `python converter.py c2f 100` prints `212.0`.\n"
        "- `python converter.py f2c 32` prints `0.0`.\n"
        "- `converter.py` exists.\n"
    )

    assert repl._extract_prd_acceptance_commands(text) == [
        ("python converter.py c2f 100", "212.0"),
        ("python converter.py f2c 32", "0.0"),
    ]


def test_prd_acceptance_runner_records_exact_output_verdicts(tmp_path: Path):
    (tmp_path / "converter.py").write_text(
        "import sys\nprint('212.0' if sys.argv[1] == 'c2f' else '0.0')\n",
        encoding="utf-8",
    )
    ledger = start_run(tmp_path, "build converter from PRD")
    set_current_run(ledger)
    try:
        passed = repl._run_prd_acceptance_commands(
            [
                ("python converter.py c2f 100", "212.0"),
                ("python converter.py f2c 32", "0.0"),
            ],
            tmp_path,
            Console(record=True),
        )
    finally:
        clear_current_run()

    assert passed is True
    events = store.load_events(tmp_path, ledger.run_id)
    verification = [event for event in events if event["type"] == "verification_passed"]
    assert len(verification) == 2
    assert all(event["source"] == "prd_acceptance" for event in verification)
    assert all(event["required"] is True for event in verification)
    assert all(str(event["verifier_id"]).startswith("verifier_") for event in verification)
    calls = store.load_tool_calls(tmp_path, ledger.run_id)
    assert sum(record.get("phase") == "called" for record in calls) == 2
    finished = [record for record in calls if record.get("phase") == "finished"]
    assert all(record.get("original_tokens") is not None for record in finished)


def test_prd_acceptance_runner_returns_failure_diagnostics(tmp_path: Path):
    (tmp_path / "converter.py").write_text("raise SyntaxError('broken')\n", encoding="utf-8")
    failures: list[str] = []

    passed = repl._run_prd_acceptance_commands(
        [("python converter.py c2f 100", "212.0")],
        tmp_path,
        Console(record=True),
        failure_details=failures,
    )

    assert passed is False
    assert len(failures) == 1
    assert "Failed command: python converter.py c2f 100" in failures[0]
    assert "SyntaxError: broken" in failures[0]


def test_prd_conformance_checks_named_functions_and_invalid_cli(tmp_path: Path):
    prd = (
        "Functions `celsius_to_fahrenheit(c)`, `fahrenheit_to_celsius(f)`, and `main()`. "
        "If arguments are missing or invalid, print usage."
    )
    (tmp_path / "converter.py").write_text(
        "import sys\n"
        "def celsius_to_fahrenheit(c): return c * 9 / 5 + 32\n"
        "def fahrenheit_to_celsius(f): return (f - 32) * 5 / 9\n"
        "def main():\n"
        "    if len(sys.argv) != 3 or sys.argv[1] not in {'c2f', 'f2c'}:\n"
        "        print('Usage: converter.py c2f|f2c number')\n"
        "        return\n"
        "    print(celsius_to_fahrenheit(float(sys.argv[2])))\n"
        "if __name__ == '__main__': main()\n",
        encoding="utf-8",
    )
    failures: list[str] = []

    passed = repl._run_prd_conformance_checks(
        prd,
        ("converter.py",),
        [("python converter.py c2f 100", "212.0")],
        tmp_path,
        Console(record=True),
        failure_details=failures,
    )

    assert passed is True
    assert failures == []


def test_prd_conformance_rejects_missing_main_and_invalid_arg_traceback(tmp_path: Path):
    prd = "Provide `main()`. If arguments are missing or invalid, print usage."
    (tmp_path / "converter.py").write_text(
        "import sys\n"
        "if len(sys.argv) < 2:\n"
        "    print('Usage')\n"
        "else:\n"
        "    print(result)\n",
        encoding="utf-8",
    )
    failures: list[str] = []

    passed = repl._run_prd_conformance_checks(
        prd,
        ("converter.py",),
        [("python converter.py c2f 100", "212.0")],
        tmp_path,
        Console(record=True),
        failure_details=failures,
    )

    assert passed is False
    assert any("missing functions: main" in failure for failure in failures)
    assert any("invalid arguments" in failure for failure in failures)


def test_prd_build_prompt_enforces_named_output_scope():
    parsed = SimpleNamespace(raw_text="# Converter", sections={})

    prompt = repl._build_prd_build_request(
        parsed,
        Path("PRD.md"),
        output_scope=("converter.py",),
        acceptance=[("python converter.py c2f 100", "212.0")],
    )

    assert "modify ONLY these explicitly requested output files: converter.py" in prompt
    assert "python converter.py c2f 100" in prompt


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


def test_clarification_resume_recovers_original_from_per_step_contract(tmp_path: Path):
    """A pending ask_user raised DURING a composite step stores the per-step
    contract, not the whole-plan wrapper. Its answer must still unwrap to the
    original request - the unrecognized wrapper used to re-route the internal
    contract (with its "Do not modify any files" line) as a fresh prompt,
    stripping the user's mutation intent into QA (observed live 2026-08-01)."""
    original = "edit app.py, run it, then show the diff"
    plan = repl._operation_plan(original, tmp_path)
    step_prompt = repl._composite_step_prompt(plan, plan.steps[0], [], "")
    resumed = step_prompt + '\n\n(Answering the earlier question "Which value?": 42)'

    recovered = recover_original_prompt(resumed)

    assert recovered.startswith(original)
    assert '(Answering the earlier question "Which value?": 42)' in recovered
    assert "one step at a time" not in recovered


def test_targeted_continuation_is_not_shredded_into_composite_steps(tmp_path: Path):
    plan = repl._operation_plan(
        "Continue the milestone: fix BASE_DIR in backend/settings.py, then verify the app",
        tmp_path,
    )

    assert not plan.is_composite
    assert len(plan.steps) == 1
    assert plan.steps[0].kind == "mutation"
    assert plan.steps[0].route != "qa"


def test_multi_artifact_continuation_still_gets_per_step_evidence(tmp_path: Path):
    plan = repl._operation_plan(
        "Continue the build: create models.py, create views.py, create urls.py, "
        "then commit the result",
        tmp_path,
    )

    assert plan.is_composite


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
async def test_direct_file_write_ignores_stale_memory_and_speculative_planner(
    tmp_path: Path, monkeypatch
):
    captured: dict[str, object] = {}

    class _Orchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, prompt):
            return SimpleNamespace(handled=False, effective_input=prompt, context="", action="")

    async def _fake_agent(*args, **kwargs):
        captured.update(kwargs)
        return AgentLoopResult(final="stopped", stopped=True)

    monkeypatch.setattr(repl, "AgentOrchestrator", _Orchestrator)
    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)

    await repl._handle_request(
        "Create exact-output.txt containing hello. Do not modify any other files.",
        tmp_path,
        Console(record=True),
        object(),
        object(),
    )

    assert captured["use_long_term_memory"] is False
    assert captured["use_planner"] is False
    assert captured["user_request"] == (
        "Create exact-output.txt containing hello. Do not modify any other files."
    )


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


def test_quoted_single_file_repair_keeps_read_edit_and_verify_atomic(tmp_path: Path):
    prompt = (
        "Fix the missing table in `database/schema.sql` so the existing INSERT in "
        "database/seed.sql is valid. Read both files first. Add the declaration, preserve "
        "existing tables, and edit only the real schema file. Then run verification and "
        "report its result."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert plan.primary_route == "file.write"
    assert "run verification" in plan.steps[0].instruction


def test_delete_only_duplicate_preserves_exact_paths_in_one_react_turn(tmp_path: Path):
    (tmp_path / "canvas-lite-react-loop-build-v4-2026-08-01").mkdir()
    prompt = (
        "In the existing project folder 'canvas-lite-react-loop-build-v4-2026-08-01', "
        "fix one file-topology bug. The accidental file 'test_canvas.py' at the project root is a duplicate. "
        "The canonical file is 'backend/core/tests/test_canvas.py'. Read both paths, "
        "delete only the accidental duplicate, keep the canonical file, and verify it is gone. "
        "Do not change application behavior or any other file."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert plan.primary_route == "file.write"
    assert "canvas-lite-react-loop-build-v4-2026-08-01" in plan.steps[0].instruction
    assert "backend/core/tests/test_canvas.py" in plan.steps[0].instruction

    handoff, direct = repl._direct_file_write_handoff(prompt, tmp_path)
    assert direct.mode == "code_edit"
    assert direct.target_files == [
        "canvas-lite-react-loop-build-v4-2026-08-01/test_canvas.py"
    ]
    assert "delete_file" in direct.required_tools
    assert "File Deletion Contract" in handoff


def test_negated_change_constraint_does_not_become_mutation_step(tmp_path: Path):
    plan = repl._operation_plan(
        "Verify the duplicate is gone. Do not change application behavior or any other file.",
        tmp_path,
    )

    assert plan.steps[0].kind == "verify"


def test_genuine_multi_action_prompt_still_splits(tmp_path: Path):
    """The merge must not swallow real, separately-actionable steps."""
    plan = repl._operation_plan("read calc.py and then run the tests", tmp_path)

    assert plan.is_composite is True
    assert [s.kind for s in plan.steps] == ["read", "verify"]


def test_composite_verify_infers_python_command_for_named_script(tmp_path: Path):
    plan = repl._operation_plan("fix calc.py, then run the script", tmp_path)

    assert repl._composite_verification_command(plan, plan.steps[1]) == "python calc.py"
    prompt = repl._composite_step_prompt(plan, plan.steps[1], [], "")
    assert "Do not modify any files while doing this." in prompt


def test_composite_read_and_verify_accept_successful_mcp_read_evidence():
    result = AgentLoopResult(final="Listed the directory.")

    read_status, read_evidence = repl._composite_step_outcome(
        SimpleNamespace(kind="read"),
        result,
        ["mcp__filesystem__list_directory"],
        set(),
    )
    verify_status, verify_evidence = repl._composite_step_outcome(
        SimpleNamespace(kind="verify"),
        result,
        ["mcp__filesystem__list_directory"],
        set(),
    )

    assert read_status == "success"
    assert verify_status == "success"
    assert "tool:mcp__filesystem__list_directory" in read_evidence
    assert "tool:mcp__filesystem__list_directory" in verify_evidence


def test_composite_mutation_accepts_successful_mcp_write_evidence():
    status, evidence = repl._composite_step_outcome(
        SimpleNamespace(kind="mutation"),
        AgentLoopResult(final="Wrote the file."),
        ["mcp__filesystem__write_file"],
        set(),
    )

    assert status == "success"
    assert evidence == ["tool:mcp__filesystem__write_file"]


def test_scoped_mcp_create_clause_remains_a_mutation(tmp_path: Path):
    target = tmp_path / "mcp-smoke.txt"
    prompt = (
        f"Use the external MCP filesystem server to create {target} with hello. "
        "Do not modify anything else. After it succeeds, report the exact MCP tool name."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.steps[0].kind == "mutation"
    assert plan.steps[1].kind == "summarize"


@pytest.mark.asyncio
async def test_composite_verify_runs_clear_script_when_model_only_describes_command(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "calc.py").write_text("print(3)\n", encoding="utf-8")
    ledger = start_run(tmp_path, "fix calc.py, then run the script")
    set_current_run(ledger)
    calls = {"n": 0}

    async def _fake_agent(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            call_id = ledger.log_tool_call("edit_file", {"filepath": "calc.py"})
            ledger.log_tool_result(call_id, "edit_file", True, "edited")
            return AgentLoopResult(final="Edited.", changed_files=("calc.py",))
        return AgentLoopResult(final="Run `python calc.py`.")

    class AllowAll:
        session_logger = None

        def ask(self, _request):
            return True

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *args, **kwargs: AllowAll())
    try:
        plan = repl._operation_plan("fix calc.py, then run the script", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert summary["status"] == "success"
    output = store.load_final_output(tmp_path, ledger.run_id)
    assert "$ python calc.py" in output
    assert "\n3" in output


@pytest.mark.asyncio
async def test_package_composite_owns_install_verify_and_summary_without_agent(
    tmp_path: Path, monkeypatch
):
    ledger = start_run(tmp_path, "install and verify")
    set_current_run(ledger)

    async def _unexpected_agent(*args, **kwargs):
        raise AssertionError("deterministic package flow must not call the model")

    def _fake_install(command, workspace, console, session_logger, active_ledger):
        call_id = active_ledger.log_tool_call("run_command", {"command": command})
        active_ledger.log_tool_result(call_id, "run_command", True, "installed")
        return SimpleNamespace(ok=True), "installed"

    def _fake_verify(plan, step, workspace, console, session_logger, active_ledger):
        command = repl._composite_verification_command(plan, step)
        call_id = active_ledger.log_tool_call("run_command", {"command": command})
        active_ledger.log_tool_result(call_id, "run_command", True, "verified")
        verifier_id = active_ledger.verifier_id_for(command, "composite_fallback")
        active_ledger.log_verification_result(
            True,
            "verified",
            command=command,
            verifier_id=verifier_id,
            source="composite_fallback",
            required=True,
            exit_code=0,
        )
        return "verified", True

    monkeypatch.setattr(repl, "_run_agent_chat", _unexpected_agent)
    monkeypatch.setattr(repl, "_execute_package_install", _fake_install)
    monkeypatch.setattr(repl, "_execute_deterministic_composite_verification", _fake_verify)
    prompt = (
        "Install boltons==24.0.0 for this Python project. Then verify it imports "
        "and report the exact Python executable and boltons module path used."
    )
    try:
        plan = repl._operation_plan(prompt, tmp_path)
        result = await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert result.final.startswith("Step 1 (mutation): success")
    assert summary["status"] == "success"


@pytest.mark.asyncio
async def test_composite_final_includes_command_output_when_model_runs_verification(
    tmp_path: Path, monkeypatch
):
    ledger = start_run(tmp_path, "fix calc.py, then run the script")
    set_current_run(ledger)
    calls = {"n": 0}

    async def _fake_agent(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            call_id = ledger.log_tool_call("edit_file", {"filepath": "calc.py"})
            ledger.log_tool_result(call_id, "edit_file", True, "edited")
            return AgentLoopResult(final="Edited.", changed_files=("calc.py",))
        call_id = ledger.log_tool_call("run_command", {"command": "python calc.py"})
        ledger.log_tool_result(
            call_id,
            "run_command",
            True,
            "Command completed",
            {"stdout": "add(2, 3) = 5\nsubtract(5, 2) = 3\n", "exit_code": 0},
        )
        return AgentLoopResult(final="The script passed.")

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("fix calc.py, then run the script", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    assert summary["status"] == "success"
    output = store.load_final_output(tmp_path, ledger.run_id)
    assert "$ python calc.py" in output
    assert "subtract(5, 2) = 3" in output


@pytest.mark.asyncio
async def test_composite_verify_rejects_unrelated_agent_run_command(
    tmp_path: Path, monkeypatch
):
    ledger = start_run(tmp_path, "fix calc.py, then run the script")
    set_current_run(ledger)
    calls = {"n": 0}

    async def _fake_agent(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            call_id = ledger.log_tool_call("edit_file", {"filepath": "calc.py"})
            ledger.log_tool_result(call_id, "edit_file", True, "edited")
            return AgentLoopResult(final="Edited.", changed_files=("calc.py",))
        call_id = ledger.log_tool_call("run_command", {"command": "python other.py"})
        ledger.log_tool_result(
            call_id,
            "run_command",
            True,
            "Command completed",
            {"stdout": "ok\n", "exit_code": 0},
        )
        return AgentLoopResult(final="The script passed.")

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("fix calc.py, then run the script", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
        summary = ledger.finalize_from_evidence()
    finally:
        clear_current_run()

    events = store.load_events(tmp_path, ledger.run_id)
    failed = [event for event in events if event["type"] == "verification_failed"][-1]
    assert summary["status"] == "partial"
    assert failed["source"] == "composite_agent_verify"
    assert failed["expected_command"] == "python calc.py"
    assert failed["actual_command"] == "python other.py"


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
async def test_failed_step_blocks_its_dependents(tmp_path: Path, monkeypatch):
    """A FAILED step must stop the chain: in the 2026-08-01 dogfood, dependents
    of a failed step still ran blind against the broken state."""
    ledger = start_run(tmp_path, "edit app.py, run it, then show the diff")
    set_current_run(ledger)

    calls = {"n": 0}

    async def _fake_agent(*args, **kwargs):
        calls["n"] += 1
        return AgentLoopResult(final="Could not apply the edit.", stopped=True)

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)
    try:
        plan = repl._operation_plan("edit app.py, run it, then show the diff", tmp_path)
        await repl._run_composite_request(plan, tmp_path, Console(record=True))
    finally:
        clear_current_run()

    assert calls["n"] == 1, "dependents of a failed step must not run"
    events = store.load_events(tmp_path, ledger.run_id)
    finished = [e for e in events if e["type"] == "operation_step_finished"]
    assert [e["status"] for e in finished] == ["failed", "not_run", "not_run"]
    assert any(
        "dependency_failed" in str(e.get("evidence", "")) for e in finished[1:]
    )


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


@pytest.mark.asyncio
async def test_plan_route_keeps_the_users_own_request(tmp_path: Path, monkeypatch):
    """Live 2026-08-02: the plan route replaced the user's prompt with a canned
    "Read X and produce a plan", so a detailed request for an ordered Django
    feature breakdown reached the model as nothing but a filename - and came
    back as a generic "convert the PDF with pdftotext" pipeline."""
    (tmp_path / "canvas lite.pdf").write_bytes(b"%PDF-1.4 fixture")
    captured: dict[str, object] = {}

    class _Orchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, prompt):
            return SimpleNamespace(handled=False, effective_input=prompt, context="", action="")

    async def _fake_agent(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return AgentLoopResult(final="plan text")

    monkeypatch.setattr(repl, "AgentOrchestrator", _Orchestrator)
    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent)

    await repl._handle_request(
        "plan a Canvas LMS Lite app from @canvas lite.pdf. Build the Django foundation "
        "with a User model that has roles first, then login and logout, then courses.",
        tmp_path,
        Console(record=True),
        object(),
        object(),
    )

    prompt = str(captured.get("prompt", ""))
    assert "User model that has roles" in prompt
    assert "then login and logout, then courses" in prompt
    assert "canvas lite.pdf" in prompt
    assert "Do NOT write any code" in prompt


def test_instruction_and_its_file_list_stay_one_step(tmp_path: Path):
    """Live 2026-08-02: "Build ONLY the Django foundation... Create: <8 files>"
    split into a targetless step 1 (which failed) and a step 2 holding the
    entire file list (which then never ran). The specification belongs to the
    instruction it specifies."""
    prompt = (
        "Build ONLY the Django project foundation for Canvas LMS Lite inside the folder "
        "canvas_lms_lite. Create: canvas_lms_lite/manage.py, canvas_lms_lite/config/settings.py, "
        "canvas_lms_lite/core/models.py. Verify by running python manage.py check."
    )

    plan = repl._operation_plan(prompt, tmp_path)
    kinds = [step.kind for step in plan.steps]
    first = plan.steps[0].instruction

    assert kinds == ["mutation", "verify"]
    assert "Build ONLY the Django project foundation" in first
    assert "canvas_lms_lite/manage.py" in first
    assert "canvas_lms_lite/core/models.py" in first


def test_two_independent_targeted_mutations_still_split(tmp_path: Path):
    """A first clause that already names its own target is a real step."""
    plan = repl._operation_plan(
        "edit greet in greeting.py, and update main.py to call greet", tmp_path
    )

    assert [step.kind for step in plan.steps] == ["mutation", "mutation"]


def test_dotted_module_paths_are_not_counted_as_files(tmp_path: Path):
    """Live 2026-08-02: "Import AbstractUser from django.contrib.auth.models"
    contributed two phantom file targets, so a ONE-file request looked like
    three and was shredded into composite steps."""
    from shamsu.routing.operations import file_targets

    text = (
        "Create the file canvas_lms_lite/core/models.py. Import AbstractUser from "
        "django.contrib.auth.models and models from django.db."
    )

    assert file_targets(text) == {"canvas_lms_lite/core/models.py"}


def test_single_file_creation_with_import_instructions_stays_one_step(tmp_path: Path):
    prompt = (
        "Create the file canvas_lms_lite/core/models.py. It must contain a Django custom "
        "user model. Import AbstractUser from django.contrib.auth.models and models from "
        "django.db. Write the complete file now with write_file."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert plan.primary_route == "file.write"


def test_single_file_spec_with_trailing_write_imperative_stays_one_step(tmp_path: Path):
    """Live 2026-08-02: a full settings.py spec ending "Write the complete file
    now with write_file" split into spec (which wrote a 35-byte stub) and a
    targetless "write it" step (which failed holding the real content)."""
    prompt = (
        "Create canvas_lms_lite/config/settings.py for the project. It must define BASE_DIR "
        "with pathlib, INSTALLED_APPS including core, and AUTH_USER_MODEL = core.User. "
        "Write the complete file now with write_file."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert "AUTH_USER_MODEL" in plan.steps[0].instruction
    assert "Write the complete file now" in plan.steps[0].instruction
