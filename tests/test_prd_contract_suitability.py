"""Phase 1: PRDContract extraction + template suitability / strategy routing."""
from __future__ import annotations

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


def test_build_project_spec_attaches_contract_and_suitability():
    spec = build_project_spec(parse_prd_text(PONG_PRD, markdown=True))
    assert isinstance(spec.prd_contract, PRDContract)
    assert spec.suitability is not None
