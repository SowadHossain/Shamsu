"""Characterization tests for the REPL routing detectors (G7).

`repl._handle_request` dispatches on a long, order-dependent chain of
`_looks_like_*` predicates that had NO test coverage — the doc flags them as
brittle ("the plan-prose dead-loop slipped through"). These tests pin the
current behavior of each detector so the pile can be trimmed/reordered later
behind a safety net, instead of "by feel". They assert only stable,
unambiguous cases (not implementation minutiae)."""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.cli.repl import (
    _classify_route_label,
    _command_for_existing_script_request,
    _direct_file_write_handoff,
    _enforce_investigative_question_decision,
    _extract_prd_path_from_prompt,
    _is_conversational_prompt,
    _looks_like_capabilities_question,
    _looks_like_code_edit_request,
    _looks_like_direct_code_request,
    _looks_like_django_generation_request,
    _looks_like_docs_ingest_request,
    _looks_like_docs_query_request,
    _looks_like_file_write_request,
    _looks_like_investigative_question,
    _looks_like_prd_build_request,
    _looks_like_prd_plan_request,
    _looks_like_vague_action_request,
    _looks_like_workspace_files_prompt,
    _looks_like_workspace_location_prompt,
    _looks_like_workspace_prd_request,
    _prd_target_directory,
    _resolve_build_prd,
)
from shamsu.types import RoutingDecision


def test_prd_target_directory_accepts_quoted_folder_name():
    project = type("Project", (), {"project_name": "fallback"})()

    assert (
        _prd_target_directory(
            "Build it inside a new folder named `canvas-lite-universal-build-2026-08-01`.",
            project,
        )
        == "canvas-lite-universal-build-2026-08-01"
    )


def test_direct_file_write_handoff_includes_skills_and_append_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SHAMSU_SKILLS", "on")
    target = tmp_path / "src" / "calculator.py"
    target.parent.mkdir(parents=True)
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    handoff, plan = _direct_file_write_handoff(
        "write pytest tests for src/calculator.py",
        tmp_path,
    )
    selected = [item.skill.name for item in plan.skill_selection.selected]

    assert plan.mode == "test_generation"
    assert "append_file" in plan.required_tools
    assert plan.target_files == ["tests/test_calculator.py"]
    assert {"developer", "testing"} <= set(selected)
    assert "## Active SHAMSU Skills" in handoff
    assert "### developer" in handoff
    assert "### testing" in handoff
    assert "Source under test: src/calculator.py" in handoff
    assert "Required test output: tests/test_calculator.py" in handoff
    assert "def add(a, b):" in handoff


def test_markdown_target_beats_test_word_inside_document_name(tmp_path: Path):
    handoff, plan = _direct_file_write_handoff(
        "Create SCOPE_NOTES.md from the Shamsu Test PRD documentation.",
        tmp_path,
    )

    assert plan.mode == "documentation"
    assert plan.executor_role == "doc_agent"
    assert plan.target_files == ["SCOPE_NOTES.md"]
    assert "Mode: documentation" in handoff


def test_direct_python_add_handoff_preserves_existing_functions(tmp_path: Path):
    target = tmp_path / "src" / "calculator.py"
    target.parent.mkdir(parents=True)
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    handoff, plan = _direct_file_write_handoff(
        "add a subtract(a,b) function to src/calculator.py",
        tmp_path,
    )

    assert plan.target_files == ["src/calculator.py"]
    assert "`subtract` must exist as a new function" in handoff
    assert "Preserve existing functions and their behavior" in handoff
    assert "not changing an existing function's return expression" in handoff


@pytest.mark.parametrize(
    "text, expected",
    [
        # Pure small talk -> conversational (must reach general chat, not the
        # task router that turned "hey how are you" into a "QA task" + plan).
        ("hey how are you", True),
        ("hi", True),
        ("hello there", True),
        ("hey there", True),
        ("thanks", True),
        ("thank you", True),
        ("how are you", True),
        ("how are you doing today", True),
        ("whats up", True),
        ("how's it going?", True),
        ("good morning", True),
        ("yo", True),
        # A greeting followed by a real request is NOT small talk: only the
        # greeting token is stripped and the remainder must itself be small talk.
        ("hey, fix the login bug", False),
        # Real questions/work must never be swallowed as chit-chat. In
        # particular "explain the caching" is where the broad
        # `_is_general_chat_prompt` (no project markers) would wrongly fire.
        ("explain the caching", False),
        ("how does auth work", False),
        ("whats the weather today", False),
        ("fix the login bug", False),
        ("how do I run the tests", False),
        ("write python for the first 100 primes", False),
    ],
)
def test_is_conversational_prompt(text, expected):
    assert _is_conversational_prompt(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("where am i", True),
        ("what folder are you in", True),
        ("current directory", True),
        ("what is the capital of France", False),
        ("read the main file", False),
    ],
)
def test_workspace_location_prompt(text, expected):
    assert _looks_like_workspace_location_prompt(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("list files", True),
        ("show me the files", True),
        ("what's in this folder", True),
        ("what is a file descriptor", False),
        ("create a file", False),
    ],
)
def test_workspace_files_prompt(text, expected):
    assert _looks_like_workspace_files_prompt(text) is expected


def test_existing_script_run_routes_to_direct_command(tmp_path: Path):
    (tmp_path / "qa_probe.py").write_text("print(5)\n", encoding="utf-8")

    prompt = "Run qa_probe.py and tell me the command output. Do not change files."

    assert _command_for_existing_script_request(prompt, tmp_path) == "python qa_probe.py"
    assert _classify_route_label(prompt, tmp_path) == "command.run"


def test_script_run_route_requires_existing_workspace_file(tmp_path: Path):
    prompt = "Run qa_probe.py and tell me the command output. Do not change files."

    assert _command_for_existing_script_request(prompt, tmp_path) == ""
    assert _classify_route_label(prompt, tmp_path) != "command.run"


def test_explicit_backtick_command_is_not_replaced_by_inference(tmp_path: Path):
    """The user named an exact command; inferring one from a filename inside it
    ran `python ok.py` (executes the script) instead of the requested
    `python -m py_compile ok.py` (compile check only)."""
    (tmp_path / "ok.py").write_text("x = 1 + 1\nprint(x)\n", encoding="utf-8")

    prompt = "Run `python -m py_compile ok.py` and tell me whether it succeeded."

    command = _command_for_existing_script_request(prompt, tmp_path)
    assert command == "python -m py_compile ok.py"
    assert _classify_route_label(prompt, tmp_path) == "command.run"


def test_explicit_single_quoted_command_is_not_replaced_by_named_source_file(tmp_path: Path):
    (tmp_path / "backend/core").mkdir(parents=True)
    (tmp_path / "backend/core/models.py").write_text("VALUE = 1\n", encoding="utf-8")

    prompt = (
        "Read 'backend/core/models.py', update the failing test, then run "
        "'python manage.py test' from the backend directory."
    )

    assert _command_for_existing_script_request(prompt, tmp_path) == "python manage.py test"
    assert _classify_route_label(prompt, tmp_path) == "composite"


def test_explicit_react_edit_keeps_read_edit_verify_contract_atomic(tmp_path: Path):
    (tmp_path / "backend/core/tests").mkdir(parents=True)
    (tmp_path / "backend/core/tests/test_canvas.py").write_text("OLD = True\n", encoding="utf-8")
    prompt = (
        "Fix one issue through the ReAct tool loop. Read 'backend/core/models.py' and the "
        "canonical file 'backend/core/tests/test_canvas.py'. Update only the canonical file, "
        "then run 'python manage.py test' and repair it until the command passes."
    )

    assert _classify_route_label(prompt, tmp_path) == "file.write"
    handoff, plan = _direct_file_write_handoff(prompt, tmp_path)
    assert plan.mode == "code_edit"
    assert plan.target_files == ["backend/core/tests/test_canvas.py"]
    assert "python manage.py test" in handoff


def test_explicit_command_outside_the_runner_allowlist_stands_down(tmp_path: Path):
    """An explicit command that names no known runner is not executed by the
    deterministic path; it falls through to the fully gated agent loop."""
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")

    prompt = "Run `rm -rf ok.py` now."

    assert _command_for_existing_script_request(prompt, tmp_path) == ""
    assert _classify_route_label(prompt, tmp_path) != "command.run"


def test_backticked_filename_still_infers_the_command(tmp_path: Path):
    """A single backticked token is a filename, not a command, so inference
    must still apply."""
    (tmp_path / "qa_probe.py").write_text("print(5)\n", encoding="utf-8")

    prompt = "Run `qa_probe.py` and tell me the command output."

    assert _command_for_existing_script_request(prompt, tmp_path) == "python qa_probe.py"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("what tools do you have", True),
        ("what can you do", True),
        ("list your tools", True),
        ("what is the weather", False),
        ("build the app", False),
    ],
)
def test_capabilities_question(text, expected):
    assert _looks_like_capabilities_question(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("change the header color", True),
        ("edit the config", True),
        ("update the readme", True),
        ("remove the old test", True),
        ("add a function to parse dates", True),
        ("add a route for login", True),
        ("add two numbers", False),          # 'add ' with no code target
        ("what does this function do", False),
    ],
)
def test_code_edit_request(text, expected):
    assert _looks_like_code_edit_request(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("go", True),
        ("just do it", True),
        ("finish the task", True),
        ("keep going", True),
        ("build a full authentication system with tests and docs", False),  # > 6 words
        ("what should i do next", False),
    ],
)
def test_vague_action_request(text, expected):
    assert _looks_like_vague_action_request(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("create hello.py", True),
        ("create a config file", True),
        ("write a new test", True),
        ("what is hello.py", False),          # question prefix
        ("how are you", False),
        ("explain the build script", False),  # question prefix, no write intent
    ],
)
def test_file_write_request(text, expected):
    assert _looks_like_file_write_request(text) is expected


@pytest.mark.parametrize(
    "prompt",
    [
        "ingest acme-docs.md as the Acme SDK reference",
        "import documentation from https://docs.example.com/acme",
        "register docs in references/acme.txt for future library tasks",
    ],
)
def test_docs_ingestion_routes_to_dedicated_agent_tool(tmp_path: Path, prompt: str):
    assert _looks_like_docs_ingest_request(prompt) is True
    assert _classify_route_label(prompt, tmp_path) == "docs.ingest"


def test_plain_doc_read_is_not_mistaken_for_ingestion(tmp_path: Path):
    prompt = "read and summarize acme-docs.md"

    assert _looks_like_docs_ingest_request(prompt) is False
    assert _classify_route_label(prompt, tmp_path) != "docs.ingest"


@pytest.mark.parametrize(
    "prompt",
    [
        "search the docs for webhook signature validation",
        "according to the Acme manual, how long do tokens last?",
        "summarize the registered document Acme Platform",
    ],
)
def test_registered_document_queries_route_to_document_tools(tmp_path: Path, prompt: str):
    assert _looks_like_docs_query_request(prompt) is True
    assert _classify_route_label(prompt, tmp_path) == "docs.query"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("write python code to print the first 100 primes", True),
        ("write a function to reverse a string", True),
        ("give me a regex for emails", True),
        ("write hello.py that prints hi", False),      # file write -> tool loop, not direct
        ("save a script to a file", False),            # workspace signal
        ("what is a closure", False),                  # no produce verb
    ],
)
def test_direct_code_request(text, expected):
    assert _looks_like_direct_code_request(text) is expected


def test_prd_path_extraction_and_dependent_detectors():
    # Path extraction underpins the PRD-plan / django-generation detectors.
    assert _extract_prd_path_from_prompt("plan from spec.md") == "spec.md"
    assert (
        _extract_prd_path_from_prompt("build from `requirements/Product Brief.pdf`")
        == "requirements/Product Brief.pdf"
    )
    assert _extract_prd_path_from_prompt('summarize @"docs/Release Notes.txt"') == "docs/Release Notes.txt"
    assert _extract_prd_path_from_prompt("read @docs/spec.md") == "docs/spec.md"
    assert _extract_prd_path_from_prompt("no path here") == ""

    # prd plan: phrase AND a PRD path required.
    assert _looks_like_prd_plan_request("create a project plan from spec.md") is True
    assert _looks_like_prd_plan_request("make a project plan") is False   # no path

    # django generation: phrase AND a PRD path required.
    assert _looks_like_django_generation_request("generate django from spec.md") is True
    assert _looks_like_django_generation_request("generate django") is False


@pytest.mark.parametrize(
    "text, expected",
    [
        ("check the prd in my workspace", True),
        ("look at the prd", True),
        ("build me a game", False),
        ("what is a prd", False),
    ],
)
def test_workspace_prd_request(text, expected):
    assert _looks_like_workspace_prd_request(text) is expected


# ---------------------------------------------------------------------------
# PRD build routing: detection is INTENT-only and must never silently fall
# through to QA. Regression net for "asked it to build from the PRD, got QA".
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_text("# Spec\n\nBuild a todo app.", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "prompt",
    [
        "build me from the prd",
        "build the app from the prd",
        "implement the prd",
        "build the product from the prd",
    ],
)
def test_prd_build_intent_survives_unresolvable_prd(tmp_path: Path, prompt: str):
    """The PRD exists but isn't *named* `prd` (`spec.md`), so it can't resolve.

    The build intent is still unambiguous, so it must route to the build
    handler (which reports honestly) - NOT fall through to the tool-less QA
    brain, which just talks about building instead of building.
    """
    workspace = _workspace(tmp_path, "spec.md")
    assert _looks_like_prd_build_request(prompt, workspace) is True
    assert _classify_route_label(prompt, workspace) == "prd.build"


def test_prd_build_intent_survives_ambiguous_and_missing_prd(tmp_path: Path):
    # Several PRDs -> still a build request; the handler asks which one.
    many = tmp_path / "many"
    many.mkdir()
    _workspace(many, "prd.md", "other-prd.md")
    assert _looks_like_prd_build_request("build the app from the prd", many) is True

    # No PRD at all -> still a build request; the handler says it can't find one.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _looks_like_prd_build_request("build the app from the prd", empty) is True


def test_prd_build_accepts_a_directly_named_spec(tmp_path: Path):
    """No "prd" wording, but the prompt names a real doc - build from it."""
    workspace = _workspace(tmp_path, "spec.md")
    assert _looks_like_prd_build_request("build the app from spec.md", workspace) is True
    # ...but only when that named doc actually exists.
    assert _looks_like_prd_build_request("build the app from missing.md", workspace) is False


def test_prd_build_resolves_a_quoted_relative_spec_with_spaces(tmp_path: Path):
    docs = tmp_path / "requirements"
    docs.mkdir()
    spec = docs / "Product Brief.pdf"
    spec.write_bytes(b"%PDF-1.4 routing fixture")
    prompt = "Build the complete application from `requirements/Product Brief.pdf`."

    assert _resolve_build_prd(prompt, tmp_path) == spec
    assert _looks_like_prd_build_request(prompt, tmp_path) is True


def test_prd_build_accepts_a_mentioned_pdf_without_prd_wording(tmp_path: Path):
    """`@canvas lite.pdf` names no PRD phrase, but an @-mentioned PDF in a
    build prompt IS the requirements document - the 2026-08-01 dogfood needed
    a rename to CANVAS_LITE_PRD.pdf to route at all."""
    spec = tmp_path / "canvas lite.pdf"
    spec.write_bytes(b"%PDF-1.4 routing fixture")

    assert _resolve_build_prd(
        'build the complete app from @"canvas lite.pdf"', tmp_path
    ) == spec
    assert _resolve_build_prd(
        "build the complete app from @canvas lite.pdf", tmp_path
    ) == spec


@pytest.mark.parametrize(
    "prompt",
    [
        "build the navbar",       # no product noun -> narrow edit, not a product build
        "fix the build",          # 'build' as a noun
        "build the app",          # product-ish, but no PRD signal at all
        "add two numbers",
    ],
)
def test_prd_build_stays_conservative(tmp_path: Path, prompt: str):
    """A PRD being present must not turn every build-ish prompt into a full
    autonomous product build."""
    workspace = _workspace(tmp_path, "prd.md")
    assert _looks_like_prd_build_request(prompt, workspace) is False


def test_prd_summary_still_wins_over_build(tmp_path: Path):
    """Reading the PRD is not building it - summary is checked first."""
    workspace = _workspace(tmp_path, "prd.md")
    assert _classify_route_label("what is the prd about", workspace) == "prd_summary"


@pytest.mark.parametrize(
    "prompt",
    [
        "plan the implementation from PRD.md",
        "Make a step by step plan to implement PRD.md. Just the plan, do not write any code yet.",
        "outline the approach for PRD.md",
        "give me a plan for the converter in PRD.md",
        "review the prd and make a devolopment plan, here is the prd file @OpenBazaar_Marketplace_PRD.docx",
    ],
)
def test_plan_requests_route_to_planning_not_build(tmp_path: Path, prompt: str):
    """"plan the implementation ..." used to route to prd.build because
    "implementation" contains "implement", kicking off a full build (and a stray
    .gitignore). A plan intent must reach the plan route, not build/write.
    Found live 2026-07-21."""
    workspace = _workspace(tmp_path, "PRD.md", "OpenBazaar_Marketplace_PRD.docx")
    assert _looks_like_prd_build_request(prompt, workspace) is False
    assert _looks_like_file_write_request(prompt) is False
    assert _classify_route_label(prompt, workspace) == "plan_prd"


def test_real_build_requests_are_unaffected_by_the_plan_guard(tmp_path: Path):
    workspace = _workspace(tmp_path, "PRD.md")
    assert _classify_route_label("build the app from the prd", workspace) == "prd.build"
    # "make hello.py" is still a file write, not a plan.
    assert _looks_like_file_write_request("make hello.py") is True


# -- investigative-question routing guard (live repro 2026-07-23) --------------


def _mutating_decision(intent: str) -> RoutingDecision:
    return RoutingDecision(
        intent=intent,
        complexity="single",
        steps=[{"id": 1, "specialist": intent, "task": "x"}],
        needs_tools=["search"],
        confidence=0.5,
    )


@pytest.mark.parametrize(
    "prompt, expected",
    [
        # Investigative questions - a change verb is absent, so these are asking,
        # not requesting work. The live repro that started this: SHAMSU jumped
        # into bug_fix and proposed an unrequested patch for the first one.
        ("Look at webapp/utils.py. Is there a bug in the divide function?", True),
        ("is there a bug in divide?", True),
        ("does the divide function handle zero?", True),
        ("what is wrong with this code?", True),
        ("why does this crash?", True),
        # Real work - an explicit change verb means do not downgrade.
        ("fix the bug in utils.py", False),
        ("can you fix the bug?", False),
        ("add a delete endpoint to app.py", False),
        ("please repair the divide function", False),
        ("refactor the divide function", False),
        ("update the readme", False),
    ],
)
def test_looks_like_investigative_question(prompt: str, expected: bool):
    assert _looks_like_investigative_question(prompt) is expected


def test_investigative_question_downgrades_bug_fix_to_qa():
    decision = _enforce_investigative_question_decision(
        "Is there a bug in the divide function?", _mutating_decision("bug_fix")
    )
    assert decision.intent == "qa"


def test_investigative_question_downgrades_code_edit_to_qa():
    decision = _enforce_investigative_question_decision(
        "does list_tasks return the right thing?", _mutating_decision("code_edit")
    )
    assert decision.intent == "qa"


def test_real_fix_request_is_not_downgraded():
    decision = _enforce_investigative_question_decision(
        "fix the bug in the divide function", _mutating_decision("bug_fix")
    )
    assert decision.intent == "bug_fix"


def test_investigative_guard_leaves_non_code_intents_alone():
    # A question-shaped generate/test_gen intent keeps its own routing; only
    # bug_fix and code_edit are in scope for this downgrade.
    decision = _enforce_investigative_question_decision(
        "is there a bug in divide?", _mutating_decision("generate")
    )
    assert decision.intent == "generate"


def test_plan_route_resolves_an_unquoted_spaced_mention(tmp_path: Path):
    """Live 2026-08-02: `plan ... @canvas lite.pdf` reached the agent as "Read
    lite.pdf", a file that does not exist, so the turn stopped to ask what that
    document was. The plan prompt must name the real resolved path."""
    from shamsu.cli.repl import _resolved_prd_reference

    spec = tmp_path / "canvas lite.pdf"
    spec.write_bytes(b"%PDF-1.4 fixture")

    assert _resolved_prd_reference(
        "plan a web app based on @canvas lite.pdf", tmp_path
    ) == "canvas lite.pdf"


def test_plan_route_falls_back_to_the_raw_token_when_nothing_resolves(tmp_path: Path):
    from shamsu.cli.repl import _resolved_prd_reference

    assert _resolved_prd_reference("plan the app from missing.md", tmp_path) == "missing.md"
    assert _resolved_prd_reference("plan the app", tmp_path) == "the PRD"
