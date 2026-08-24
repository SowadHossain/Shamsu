r"""The plan the user asked for, re-shown by its real step names.

Live 2026-08-24, `F:\Work\shamsu test - 24aug\demo-3\asteroid`. The user asked
for a plan in a file and got one: PLAN.md, written turn 1 at 02:45, with eight
named phases. It was never read back. Its real headings - `### Phase 3: Player
Ship Module (player.js)` - appear in ZERO of the 24 surviving prompts.

What the model saw every turn instead was the rolling conversation summary,
which had invented a different decomposition and stamped it finished:

    - Phase 1 complete: index.html, package.json, vite.config.js created and validated.
    - Phase 2 complete: src/main.js, player.js, ... scaffolded.

PLAN.md says Phase 1 is "Project Setup & Scaffolding" and Phase 2 is "Core Game
Loop & Scene Setup (main.js)". Neither line matches, and "validated" never
happened - three commands succeeded in the whole session. So "lets proceed with
phase 2" resolved against the summary, and the model improvised something that
was in no plan anywhere.

`plan_anchor.anchor` already re-injects the CONTRACT every turn. A contract is
what SHAMSU means by a plan. It is not what the user meant.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.agents.plan_anchor import (
    document_anchor,
    plan_document_steps,
)

DEMO_3_PLAN = """# 3D Asteroids Survival Game - Development Plan

## Project Overview
A game.

## Step-by-Step Approach

### Phase 1: Project Setup & Scaffolding
- npm init

### Phase 2: Core Game Loop & Scene Setup (main.js)
- scene, camera, renderer

### Phase 3: Player Ship Module (player.js)
- triangular ship

## Exact File Structure
main.js, player.js
"""


def test_the_real_phase_names_are_recovered(tmp_path: Path):
    (tmp_path / "PLAN.md").write_text(DEMO_3_PLAN, encoding="utf-8")

    steps = plan_document_steps(tmp_path)

    assert steps == [
        "Phase 1: Project Setup & Scaffolding",
        "Phase 2: Core Game Loop & Scene Setup (main.js)",
        "Phase 3: Player Ship Module (player.js)",
    ]


def test_a_section_about_the_steps_is_not_one_of_them(tmp_path: Path):
    """`## Step-by-Step Approach` came back as a step until the keyword was
    required to be followed by a space and something."""
    (tmp_path / "PLAN.md").write_text(DEMO_3_PLAN, encoding="utf-8")

    assert not any("Step-by-Step" in step for step in plan_document_steps(tmp_path))


def test_a_workspace_with_no_plan_anchors_nothing(tmp_path: Path):
    assert plan_document_steps(tmp_path) == []
    assert document_anchor([]) == ""


def test_the_anchor_names_the_file_and_says_where_a_step_name_resolves(tmp_path: Path):
    (tmp_path / "PLAN.md").write_text(DEMO_3_PLAN, encoding="utf-8")

    anchor = document_anchor(plan_document_steps(tmp_path))

    assert "PLAN.md" in anchor
    assert "Phase 2: Core Game Loop & Scene Setup (main.js)" in anchor
    assert "means the one with that name HERE" in anchor
    assert "do not work from your memory of it" in anchor


def test_a_long_plan_is_capped_but_still_points_at_the_file(tmp_path: Path):
    plan = "\n".join(f"### Phase {n}: {'x' * 60}" for n in range(1, 40))
    (tmp_path / "PLAN.md").write_text(plan, encoding="utf-8")

    anchor = document_anchor(plan_document_steps(tmp_path))

    assert len(anchor) < 1200
    assert "read PLAN.md for the rest" in anchor


def test_the_turn_shows_the_document_even_with_no_contract(tmp_path: Path):
    """Turns 2 to 4 of that session had no contract at all, and those are the
    turns where the phases came apart."""
    from shamsu.agents.simple_chat import SimpleChatLoop

    (tmp_path / "PLAN.md").write_text(DEMO_3_PLAN, encoding="utf-8")
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = tmp_path

    standing = loop._standing_plan()

    assert "Phase 2: Core Game Loop & Scene Setup (main.js)" in standing


def test_a_plan_document_does_not_stand_in_for_a_contract(tmp_path: Path):
    """`_standing_plan` gates the "write down the parts" ask. Folding the
    document into it suppressed `contract_create` on every workspace that had a
    PLAN.md - which would take the assertions, the evidence rule and the
    done-guard with it, on exactly the projects most likely to need them."""
    from shamsu.agents.simple_chat import SimpleChatLoop

    (tmp_path / "PLAN.md").write_text(DEMO_3_PLAN, encoding="utf-8")
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = tmp_path

    assert loop._plan_document(), "the document is shown to the model"
    assert not loop._standing_contract(), "but it is not a contract"
    assert "Phase 2" in loop._standing_plan(), "and both halves reach the prompt"
