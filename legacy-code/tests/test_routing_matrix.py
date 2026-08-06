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
import re
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
    ("find the login requirements from the prd", "prd_summary"),
    ("find the schema from the prd", "prd_summary"),
    ("what database schema is in the prd", "prd_summary"),
    ("use the prd to tell me the login requirements", "prd_summary"),
    ("look in the prd and tell me the tech stack", "prd_summary"),
    ("from the prd what are the user roles", "prd_summary"),
    ("find the schema from the prd and create the database model", "prd.build"),
    ("use the prd to configure the postgres schema", "prd.build"),
    ("review the prd and make a development plan", "plan_prd"),
    # Narrow work must not trigger a full autonomous product build.
    ("build the navbar", "agent-chat"),
    ("fix the build", "agent-chat"),
    ("find App.jsx", "qa"),
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


# --- the table is the single source of truth ---------------------------------
# Routing used to be decided twice: the real if/elif chain in `_handle_request`,
# and a hand-maintained copy in `_classify_route_label` for the session trace.
# They had drifted (11 of 20 rules, different order), so "run the game" ran the
# game while the trace recorded "qa". Now one ordered table decides, and the
# dispatcher keys off its label. These pin that coupling.


def test_every_rule_label_has_a_dispatch_branch():
    """A rule that routes nowhere silently falls through to the QA tail - the
    exact silent-degradation this whole doc is about."""
    from shamsu.cli.repl import _ROUTE_RULES, _handle_request

    dispatcher = inspect.getsource(_handle_request)
    missing = [
        label for label, _ in _ROUTE_RULES if f'route_label == "{label}"' not in dispatcher
    ]
    assert not missing, f"_ROUTE_RULES labels with no handler in _handle_request: {missing}"


def test_every_dispatch_branch_has_a_rule():
    """The reverse: a handler nothing can route to is dead code."""
    from shamsu.cli.repl import _ROUTE_RULES, _handle_request

    dispatcher = inspect.getsource(_handle_request)
    handled = set(re.findall(r'route_label == "([^"]+)"', dispatcher))
    labels = {label for label, _ in _ROUTE_RULES}
    # A few labels are assigned by _handle_request itself, not by a _ROUTE_RULES
    # detector - e.g. "general_chat", set when a prompt is pure small talk that
    # matched no rule. Those are reachable, so require the dispatcher to actually
    # assign them somewhere; they just don't come from the rules table.
    override_labels = set(re.findall(r'route_label = "([^"]+)"', dispatcher))
    orphans = handled - labels - override_labels
    assert not orphans, f"_handle_request branches unreachable from _ROUTE_RULES: {orphans}"


def test_rule_labels_are_unique_and_ordered():
    """First match wins, so a duplicate label means the second is unreachable."""
    from shamsu.cli.repl import _ROUTE_RULES

    labels = [label for label, _ in _ROUTE_RULES]
    assert len(labels) == len(set(labels)), f"duplicate route labels: {labels}"
    assert labels[0] == "prd_summary", (
        "PRD summary must stay first: otherwise 'checkout the prd' trips git and "
        "'what is it about' falls into the tool loop and stalls."
    )
    assert labels[1] == "git", (
        "git must stay ahead of web/QA/code-edit: otherwise 'commit the current "
        "changes' trips the web keyword and 'stage the files' falls into weak QA."
    )


def test_a_raising_detector_does_not_take_down_routing():
    """A detector touching the filesystem can raise (permissions, races). A bad
    routing guess must degrade to the QA tail, not kill the request."""
    from shamsu.cli import repl

    def _boom(_text, _ws):
        raise OSError("disk gone")

    original = repl._ROUTE_RULES
    try:
        repl._ROUTE_RULES = (("explodes", _boom), *original)
        assert repl._classify_route_label("anything", Path(".")) != "explodes"
    finally:
        repl._ROUTE_RULES = original
