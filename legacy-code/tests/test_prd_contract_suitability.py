"""Phase 1: PRDContract extraction + template suitability / strategy routing."""
from __future__ import annotations

from pathlib import Path

from shamsu.prd.contract import PRDContract, extract_contract
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.registry.schema import Category
from shamsu.registry.suitability import GenerationStrategy

PONG_PRD = """# Pong

## Overview
A classic 2D Pong game for two players on one keyboard. Single player vs a
simple AI is also supported. No networking.

## Mechanics
- Two paddles, one ball
- Ball speeds up after each paddle hit
- First player to 11 points wins

## Controls
- Left paddle: W and S keys
- Right paddle: Arrow up and arrow down

## Screens
- Main menu
- Game screen
- Game over screen

## Acceptance Criteria
- The ball bounces off the top and bottom walls
- A point is scored when the ball passes a paddle
- The winner is shown on the game over screen
"""

MULTIPLAYER_PRD = """# Arena Brawl

## Overview
A real-time multiplayer 3D arena shooter. Players join a lobby by room code
and battle online with server-authoritative netcode.

## Mechanics
- Up to 8 players per match
- Score by eliminating opponents
"""

CMS_PRD = """# Markdown Knowledge Base

## Overview
A headless CMS for managing markdown documentation with a REST API for content
and a tag-based navigation system. Built as a bespoke content service.

## Features
- Create, edit, and version markdown documents
- Full-text search across documents
"""


def test_contract_extracts_pong_details():
    parsed = parse_prd_text(PONG_PRD, markdown=True)
    contract = extract_contract(parsed)
    assert contract.project_kind == "game"
    assert contract.game_type == "pong"
    assert contract.is_multiplayer is False
    assert contract.is_3d is False
    assert any("paddle" in m.lower() for m in contract.mechanics)
    assert contract.controls  # W/S + arrows captured
    assert any("menu" in s.lower() for s in contract.screens)
    assert any("score" in c.lower() or "point" in c.lower() for c in contract.acceptance_criteria)


def test_contract_extracts_acceptance_section_alias():
    parsed = parse_prd_text(
        "# Expense CLI\n\n"
        "## Acceptance\n"
        "- `python ledgerlite.py seed --db data.json` prints `seeded 4 expenses`.\n",
        markdown=True,
    )
    contract = extract_contract(parsed)

    assert contract.acceptance_criteria == [
        "`python ledgerlite.py seed --db data.json` prints `seeded 4 expenses`."
    ]


def test_contract_joins_ocr_wrapped_acceptance_and_drops_corrupted_noise():
    parsed = parse_prd_text(
        "# Canvas Lite\n\n"
        "## Acceptance Criteria\n"
        "- An admin can create a course and assign it to the\n"
        "teacher account.\n"
        "- A student can submit work.\n"
        "- bs a sg s ss a as s s ss n error, not a crash.\n",
        markdown=True,
    )

    contract = extract_contract(parsed)

    assert contract.acceptance_criteria == [
        "An admin can create a course and assign it to the teacher account.",
        "A student can submit work.",
    ]


def test_contract_roundtrips_through_dict():
    contract = extract_contract(parse_prd_text(PONG_PRD, markdown=True))
    restored = PRDContract.from_dict(contract.to_dict())
    assert restored == contract


def test_suitability_routes_pong_to_2d_scaffold():
    spec = build_project_spec(parse_prd_text(PONG_PRD, markdown=True))
    assert spec.suitability.strategy is GenerationStrategy.SCAFFOLD
    assert spec.suitability.candidate == Category.GAME_2D.value
    # It should say what must change (fill holes from the PRD).
    assert spec.suitability.must_change


def test_suitability_routes_multiplayer_to_3d_template():
    spec = build_project_spec(parse_prd_text(MULTIPLAYER_PRD, markdown=True))
    assert spec.prd_contract.is_multiplayer is True
    assert spec.suitability.strategy is GenerationStrategy.SCAFFOLD
    assert spec.suitability.candidate == Category.MULTIPLAYER_GAME.value


def test_suitability_routes_cms_to_freeform():
    spec = build_project_spec(parse_prd_text(CMS_PRD, markdown=True))
    # No template is forced onto a bespoke CMS.
    assert spec.suitability.strategy is GenerationStrategy.FREEFORM
    assert spec.suitability.candidate == ""


def test_suitability_routes_explicit_django_to_django_writer():
    spec = build_project_spec(
        parse_prd_text(
            "# Notes\n\n"
            "## Tech Stack\n- Django\n- SQLite\n\n"
            "## Entities\n- Note: title (text), body (long text)\n",
            markdown=True,
        )
    )

    assert spec.suitability.strategy is GenerationStrategy.DJANGO
    assert spec.generation_order[0].path == "manage.py"


def test_suitability_does_not_route_generic_crud_to_django_writer():
    spec = build_project_spec(
        parse_prd_text(
            "# Notes\n\n"
            "## Entities\n- Note: title (text), body (long text)\n",
            markdown=True,
        )
    )

    assert spec.suitability.strategy is GenerationStrategy.FREEFORM
    assert [item.path for item in spec.generation_order] == ["index.html", "README.md"]


def test_build_project_spec_attaches_contract_and_suitability():
    spec = build_project_spec(parse_prd_text(PONG_PRD, markdown=True))
    assert isinstance(spec.prd_contract, PRDContract)
    assert spec.suitability is not None


def test_contract_keeps_long_prd_workflows_scripts_tests_and_entities():
    prd_text = Path("evals/fixtures/prds/atlasdesk_long.md").read_text(encoding="utf-8")
    contract = extract_contract(parse_prd_text(prd_text, markdown=True))

    assert contract.project_kind == "web_app"
    assert {"react", "vite", "node", "typescript", "sqlite"} <= set(contract.required_stack)
    assert {"Incident", "Note", "HealthMetric"} <= {
        str(entity.get("name")) for entity in contract.entities
    }
    assert any("Seed realistic demo data" in item for item in contract.features)
    assert any("scripts/seed.mjs" in item for item in contract.features)
    assert any("status-count computation" in item for item in contract.required_tests)
