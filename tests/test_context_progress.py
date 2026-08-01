"""Tests for context discipline (G10): the compact progress checklist and the
plan/PRD-milestone request builders that carry it instead of re-dumping the full
plan/PRD text into every step."""
from __future__ import annotations

from pathlib import Path

from shamsu.cli.repl import (
    _build_prd_milestone_request,
    _plan_step_request,
    _prd_brief,
    _prd_milestones_for_execution,
)
from shamsu.context.progress import render_progress_checklist
from shamsu.prd.parser import parse_prd_text
from shamsu.types import ParsedPRD

# ---------------------------------------------------------------------------
# render_progress_checklist
# ---------------------------------------------------------------------------


def test_checklist_marks_done_current_pending():
    text = render_progress_checklist(["a", "b", "c"], 1, header="Steps")
    lines = text.splitlines()
    assert lines[0].startswith("## Steps")
    assert lines[1] == "1. [x] a"
    assert lines[2].startswith("2. [>] b") and "implement THIS one now" in lines[2]
    assert lines[3] == "3. [ ] c"


def test_checklist_first_step_has_no_done_items():
    text = render_progress_checklist(["only"], 0)
    assert "[x]" not in text
    assert "1. [>] only" in text


def test_checklist_is_ascii_only():
    text = render_progress_checklist(["x", "y"], 0, header="Milestones")
    text.encode("ascii")  # raises if any non-ASCII marker slipped in


def test_checklist_empty_returns_blank():
    assert render_progress_checklist([], 0) == ""
    assert render_progress_checklist(["  ", ""], 0) == ""


def test_checklist_collapses_whitespace_and_caps_length():
    long_item = "word " * 100
    text = render_progress_checklist(["first\n\n  line   here", long_item], 0)
    assert "first line here" in text
    # each rendered item is capped
    assert all(len(line) < 240 for line in text.splitlines())


# ---------------------------------------------------------------------------
# _plan_step_request (no more full-markdown re-dump)
# ---------------------------------------------------------------------------


def test_plan_step_request_carries_checklist_not_full_markdown():
    steps = ["Wire entities", "Game loop", "HUD"]
    req = _plan_step_request("Build pong", steps, 2, 3)
    assert "## Plan steps" in req
    for step in steps:  # completeness: every step is visible
        assert step in req
    assert "[x] Wire entities" in req
    assert "[>] Game loop" in req
    assert "Build pong" in req
    # The old behavior dumped the whole plan markdown under this header.
    assert "Full approved plan" not in req


def test_plan_step_request_shrinks_with_step_count_not_grows():
    many = [f"step number {i}" for i in range(12)]
    req = _plan_step_request("t", many, 6, 12)
    # Every step still present, but the request stays compact (< raw-dump size).
    assert all(s in req for s in many)
    assert len(req) < 2000


# ---------------------------------------------------------------------------
# PRD milestone request + brief
# ---------------------------------------------------------------------------


def _parsed_prd() -> ParsedPRD:
    raw = "PONG GAME\n" + ("some very long requirement detail line. " * 200)
    return ParsedPRD(
        title="Pong",
        sections={"Mechanics": ["ball bounces", "paddles move"], "Controls": ["arrow keys"]},
        raw_text=raw,
    )


def test_prd_brief_is_compact_not_raw_text():
    parsed = _parsed_prd()
    brief = _prd_brief(parsed)
    assert "PRD contract" in brief
    assert len(brief) < len(parsed.raw_text) // 2  # much smaller than the raw PRD


def test_milestone_request_uses_brief_and_checklist_not_raw_text():
    parsed = _parsed_prd()
    brief = _prd_brief(parsed)
    milestones = ["Render board", "Add ball physics", "Add scoring"]
    req = _build_prd_milestone_request(parsed.title, Path("prd.md"), brief, milestones, 2, 3)
    assert "## Milestones" in req
    for m in milestones:
        assert m in req
    assert "[x] Render board" in req
    assert "[>] Add ball physics" in req
    assert "prd.md" in req  # the agent is told where to read full detail
    # The verbose raw PRD text must NOT be dumped into the per-milestone prompt.
    assert parsed.raw_text not in req


def test_compiled_prd_milestones_are_feature_flagged(monkeypatch):
    parsed = parse_prd_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        markdown=True,
    )

    monkeypatch.delenv("SHAMSU_MILESTONE_EXECUTOR", raising=False)
    disabled, disabled_source = _prd_milestones_for_execution(parsed)
    assert disabled == []
    assert disabled_source == "disabled"

    monkeypatch.setenv("SHAMSU_MILESTONE_EXECUTOR", "1")
    milestones, source = _prd_milestones_for_execution(parsed)
    assert source == "compiled_requirement_ledger"
    assert any(item.startswith("M-002") for item in milestones)
    assert any(item.startswith("M-004") for item in milestones)
