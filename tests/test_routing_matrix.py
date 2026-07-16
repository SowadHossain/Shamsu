"""Gap F1: a deterministic routing eval net.

The governing rule is "no prompt/loop change ships without an eval delta", but
`evals/cases.py` only covers single-turn agent-loop file ops - so every keyword
added to a `_looks_like_*` list shipped unmeasured. Both bugs found on
2026-07-17 (the PRD build falling through to QA, natural plan requests falling
through to QA) sat entirely outside what any eval measured.

This is that net: a prompt -> expected-route matrix over the real routing
decision, run against real workspace fixtures. Deterministic, no Ollama,
sub-second. It exists to make routing regressions loud, and to be the safety
net the `_handle_request` dispatch trim (G7/B2) needs.

Route labels come from `_classify_route_label`, which mirrors the real
`_handle_request` chain. `test_dispatch_mirror_is_honest` guards that mirror
against drift - it is the one thing that could make this whole file lie.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from shamsu.cli.repl import _classify_route_label


def _workspace(tmp_path: Path, *names: str) -> Path:
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Spec\n\nBuild a todo app.", encoding="utf-8")
    return tmp_path


# --- PRD workspace: exactly one file named like a PRD -------------------------

PRD_CASES: list[tuple[str, str]] = [
    # Building from the PRD must reach the build route, not the tool-less QA brain.
    ("build the app from the prd", "prd.build"),
    ("build me from the prd", "prd.build"),
    ("implement the prd", "prd.build"),
    ("build the product from the prd", "prd.build"),
    ("finish the prd", "prd.build"),
    # Terse imperatives in a PRD workspace mean "build that PRD".
    ("do it", "prd.build"),
    ("continue", "prd.build"),
    # Reading the PRD is NOT building it.
    ("what is the prd about", "prd_summary"),
    ("summarize the prd", "prd_summary"),
    ("read the prd", "prd_summary"),
    # Narrow work must not trigger a full autonomous product build.
    ("build the navbar", "qa"),
    ("fix the build", "qa"),
]


@pytest.mark.parametrize("prompt, expected", PRD_CASES)
def test_routing_in_a_prd_workspace(tmp_path: Path, prompt: str, expected: str):
    workspace = _workspace(tmp_path, "prd.md")
    assert _classify_route_label(prompt, workspace) == expected


# --- PRD that isn't NAMED like a PRD (the 2026-07-17 regression) --------------


@pytest.mark.parametrize(
    "prompt",
    [
        "build the app from the prd",
        "build me from the prd",
        "implement the prd",
    ],
)
def test_build_intent_survives_a_differently_named_spec(tmp_path: Path, prompt: str):
    """`spec.md` IS the PRD; it just doesn't pass `is_prd_filename`. The build
    intent is unambiguous, so it must not silently degrade to QA - the handler
    reports what it needs instead."""
    workspace = _workspace(tmp_path, "spec.md")
    assert _classify_route_label(prompt, workspace) == "prd.build"


def test_build_intent_survives_an_ambiguous_workspace(tmp_path: Path):
    workspace = _workspace(tmp_path, "prd.md", "other-prd.md")
    assert _classify_route_label("build the app from the prd", workspace) == "prd.build"


def test_build_intent_survives_an_empty_workspace(tmp_path: Path):
    assert _classify_route_label("build the app from the prd", tmp_path) == "prd.build"


# --- plain workspace ----------------------------------------------------------

PLAIN_CASES: list[tuple[str, str]] = [
    ("where am i", "workspace.location"),
    ("what folder are you in", "workspace.location"),
    ("list files", "workspace.files"),
    ("show me the files", "workspace.files"),
    ("create hello.py", "file.write"),
    ("write a new test file", "file.write"),
    ("write python code to print the first 100 primes", "direct_code"),
    ("write a function to reverse a string", "direct_code"),
    ("commit the current changes", "git"),
    ("what are the unstaged changes", "git"),
    ("stage the files", "git"),
]


@pytest.mark.parametrize("prompt, expected", PLAIN_CASES)
def test_routing_in_a_plain_workspace(tmp_path: Path, prompt: str, expected: str):
    assert _classify_route_label(prompt, tmp_path) == expected


# --- the mirror must not lie --------------------------------------------------


def test_dispatch_mirror_is_honest():
    """`_classify_route_label` mirrors `_handle_request` BY HAND (gap B2), and
    the whole matrix above trusts it. Pin the coupling: every detector the
    mirror consults must still be consulted by the real dispatcher, so a
    detector renamed/removed in one and not the other fails here loudly
    instead of silently reporting a route that never ran."""
    mirror = inspect.getsource(_classify_route_label)
    from shamsu.cli.repl import _handle_request

    dispatcher = inspect.getsource(_handle_request)

    detectors = [
        name
        for name in (
            "_looks_like_prd_summary_request",
            "is_git_request",
            "_looks_like_workspace_location_prompt",
            "_looks_like_workspace_files_prompt",
            "_looks_like_prd_build_request",
            "_looks_like_file_write_request",
            "_looks_like_direct_code_request",
        )
        if name in mirror
    ]
    assert detectors, "mirror consults no known detectors - it was rewritten"
    missing = [name for name in detectors if name not in dispatcher]
    assert not missing, (
        f"_classify_route_label consults {missing} but _handle_request no longer does. "
        "The session trace would report a route that never ran."
    )


def test_mirror_and_dispatcher_agree_on_detector_order():
    """Order IS the routing logic in an if/elif chain: same detectors in a
    different order = a different router. The mirror's shared detectors must
    appear in the same relative order as the dispatcher's."""
    from shamsu.cli.repl import _handle_request

    mirror = inspect.getsource(_classify_route_label)
    dispatcher = inspect.getsource(_handle_request)

    shared = [
        "_looks_like_prd_summary_request",
        "is_git_request",
        "_looks_like_workspace_location_prompt",
        "_looks_like_workspace_files_prompt",
        "_looks_like_prd_build_request",
        "_looks_like_file_write_request",
        "_looks_like_direct_code_request",
    ]
    in_mirror = [name for name in shared if name in mirror]
    mirror_order = sorted(in_mirror, key=mirror.index)
    dispatch_order = sorted(in_mirror, key=dispatcher.index)
    assert mirror_order == dispatch_order, (
        "Detector order drifted between _classify_route_label and _handle_request:\n"
        f"  mirror:     {mirror_order}\n"
        f"  dispatcher: {dispatch_order}"
    )
