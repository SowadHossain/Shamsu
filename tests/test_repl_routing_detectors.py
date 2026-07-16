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
    _extract_prd_path_from_prompt,
    _looks_like_capabilities_question,
    _looks_like_code_edit_request,
    _looks_like_direct_code_request,
    _looks_like_django_generation_request,
    _looks_like_file_write_request,
    _looks_like_prd_build_request,
    _looks_like_prd_plan_request,
    _looks_like_vague_action_request,
    _looks_like_workspace_files_prompt,
    _looks_like_workspace_location_prompt,
    _looks_like_workspace_prd_request,
)


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
