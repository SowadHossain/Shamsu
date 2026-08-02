"""Template suitability + generation-strategy selection.

Templates are accelerators, not requirements. Before generating anything,
`assess()` decides *how* to build the project from the PRD contract:

  - SCAFFOLD : a game scaffold fits (game-2d for simple/local games, the
               3D multiplayer template for networked games) -> copy + fill holes.
  - DJANGO   : a CRUD/REST web app -> the existing deterministic Django writer.
  - FREEFORM : nothing fits (a CMS, a bespoke service, anything we have no
               template for) -> generate from the PRD with no scaffold.

It also produces an honest report (why this candidate, what matches the PRD,
what conflicts, what must change) so a template is never silently forced onto a
PRD it does not fit - which is exactly how a Pong PRD became a snake-like game.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

from shamsu.prd.contract import PRDContract
from shamsu.registry.schema import Category
from shamsu.types import Archetype

# Master switch for the copy-paste template scaffolds (game-2d, the 3D
# multiplayer monorepo). Disabled by default: SHAMSU builds every scaffold-
# eligible project from the PRD via the freeform generator instead of copying
# boilerplate a small model then can't reconcile (e.g. a 2D Pong request that
# became a 39-file 3D scaffold). The deterministic Django writer is unaffected.
# Nothing is deleted - set SHAMSU_ENABLE_TEMPLATES=1 to restore the old routing.
_ENABLE_TEMPLATES_ENV = "SHAMSU_ENABLE_TEMPLATES"


def templates_enabled() -> bool:
    """True only when SHAMSU_ENABLE_TEMPLATES is explicitly truthy. Consulted at
    the generation-strategy decision points (full_pipeline, the CLI build flow);
    `assess()` itself stays pure so its unit tests keep exercising real fit logic."""
    return os.environ.get(_ENABLE_TEMPLATES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}

# Simple/local 2D game types the 2D-canvas scaffold fits well.
_SIMPLE_2D_GAMES = {
    "pong", "snake", "breakout", "tetris", "flappy", "space-invaders",
    "asteroids", "2048", "minesweeper", "tic-tac-toe", "platformer",
    "shooter", "racing", "puzzle",
}


class GenerationStrategy(str, Enum):
    SCAFFOLD = "scaffold"    # copy a template scaffold + fill holes
    DJANGO = "django"        # deterministic Django writer (web-crud / rest-api)
    FREEFORM = "freeform"    # generate from the PRD, no template

    @property
    def uses_template(self) -> bool:
        return self in (GenerationStrategy.SCAFFOLD, GenerationStrategy.DJANGO)


@dataclass(frozen=True)
class TemplateSuitability:
    strategy: GenerationStrategy
    candidate: str                       # category value to scaffold, or "" for freeform/django
    reason: str = ""
    matches: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    must_change: list[str] = field(default_factory=list)
    fit_score: float = 0.0               # 0..1 confidence the candidate fits the PRD

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "candidate": self.candidate,
            "reason": self.reason,
            "matches": list(self.matches),
            "conflicts": list(self.conflicts),
            "must_change": list(self.must_change),
            "fit_score": self.fit_score,
        }

    def render_brief(self) -> str:
        lines = [f"Strategy: {self.strategy.value}"
                 + (f" (template: {self.candidate})" if self.candidate else "")]
        if self.reason:
            lines.append(f"Why: {self.reason}")
        for label, items in (("Matches", self.matches),
                             ("Conflicts", self.conflicts),
                             ("Must change", self.must_change)):
            if items:
                lines.append(f"{label}:")
                lines.extend(f"  - {item}" for item in items)
        return "\n".join(lines)


def assess(
    contract: PRDContract,
    category: Category,
    archetype: Archetype,
) -> TemplateSuitability:
    """Pick the generation strategy and report template fit for this PRD."""
    if contract.is_game:
        return _assess_game(contract)
    if archetype in (Archetype.WEB_CRUD, Archetype.REST_API):
        return _assess_django(contract, archetype)
    return _assess_freeform(contract, category)


def _assess_game(contract: PRDContract) -> TemplateSuitability:
    wants_multiplayer = contract.is_multiplayer
    simple_2d = (contract.game_type in _SIMPLE_2D_GAMES) or (not wants_multiplayer)

    if wants_multiplayer or (contract.is_3d and not simple_2d):
        matches = ["real-time / networked multiplayer play"]
        if contract.is_3d:
            matches.append("3D rendering")
        conflicts: list[str] = []
        if not contract.is_3d:
            conflicts.append("PRD reads 2D but the multiplayer template renders in 3D (R3F)")
        return TemplateSuitability(
            strategy=GenerationStrategy.SCAFFOLD,
            candidate=Category.MULTIPLAYER_GAME.value,
            reason="multiplayer/networked game — the 3D relay template provides the netcode/lobby.",
            matches=matches,
            conflicts=conflicts,
            must_change=[f"fill game holes for '{contract.game_type or 'the game'}' from the PRD"],
            fit_score=0.85 if contract.is_3d else 0.6,
        )

    # Simple/local 2D game -> the 2D canvas scaffold, NOT the 3D relay.
    conflicts = []
    must_change = [f"fill 2D game holes for '{contract.game_type or 'the game'}' from the PRD"]
    return TemplateSuitability(
        strategy=GenerationStrategy.SCAFFOLD,
        candidate=Category.GAME_2D.value,
        reason=(
            f"'{contract.game_type or 'simple'}' is a 2D single/local game; the 2D-canvas "
            "scaffold fits without the unused 3D + networking stack."
        ),
        matches=["2D canvas rendering", "single/local play", "keyboard input"],
        conflicts=conflicts,
        must_change=must_change,
        fit_score=0.9 if contract.game_type in _SIMPLE_2D_GAMES else 0.7,
    )


def _assess_django(contract: PRDContract, archetype: Archetype) -> TemplateSuitability:
    label = "REST API" if archetype == Archetype.REST_API else "CRUD web app"
    requested_stack = {item.lower() for item in contract.required_stack}
    non_django_stack = requested_stack & {"go", "node", "rust"}
    if non_django_stack and not (requested_stack & {"django", "python"}):
        return TemplateSuitability(
            strategy=GenerationStrategy.FREEFORM,
            candidate="",
            reason=(
                f"{label} asks for {', '.join(sorted(non_django_stack))}; build from the "
                "PRD instead of forcing the deterministic Django CRUD generator."
            ),
            matches=[f"{label} structure", "entity-backed product"],
            conflicts=["Django generator would ignore the requested non-Django stack."],
            must_change=["derive architecture, schema, UI, CLI, and tests from the PRD"],
            fit_score=0.0,
        )
    spa_stack = requested_stack & {"react", "vite", "typescript"}
    if spa_stack:
        return TemplateSuitability(
            strategy=GenerationStrategy.FREEFORM,
            candidate="",
            reason=(
                f"{label} requires a separate {', '.join(sorted(spa_stack))} frontend; "
                "build through the project harness instead of the server-rendered Django writer."
            ),
            matches=[f"{label} backend", "entity-backed product"],
            conflicts=["The deterministic Django writer does not create the required SPA frontend."],
            must_change=["derive backend, frontend, database, integration, and tests from the PRD"],
            fit_score=0.0,
        )
    if _contract_requires_cli(contract):
        return TemplateSuitability(
            strategy=GenerationStrategy.FREEFORM,
            candidate="",
            reason=(
                f"{label} includes a terminal/CLI surface; build from the PRD instead "
                "of using the browser-only Django CRUD generator."
            ),
            matches=[f"{label} structure", "CLI requirement"],
            conflicts=["Django generator does not create the required CLI commands."],
            must_change=["derive browser UI, CLI commands, persistence, and tests from the PRD"],
            fit_score=0.0,
        )
    return TemplateSuitability(
        strategy=GenerationStrategy.DJANGO,
        candidate="",
        reason=f"{label} — the deterministic Django generators cover models/views/serializers.",
        matches=[f"{label} structure", "entity-backed CRUD"],
        conflicts=[],
        must_change=["generate models/views from PRD entities"],
        fit_score=0.8,
    )


def _contract_requires_cli(contract: PRDContract) -> bool:
    sections = " ".join(contract.source_refs)
    text = " ".join(
        [
            sections,
            contract.product_summary,
            *contract.features,
            *contract.acceptance_criteria,
            *contract.required_tests,
            *contract.constraints,
            *contract.nonfunctional_requirements,
        ]
    ).lower()
    return bool(re.search(r"\bcli\b|command[- ]line|terminal|workspace commands|atlas\s+\w+", text))


def _assess_freeform(contract: PRDContract, category: Category) -> TemplateSuitability:
    return TemplateSuitability(
        strategy=GenerationStrategy.FREEFORM,
        candidate="",
        reason=(
            f"No template fits a '{contract.project_kind}' project; generating directly from the "
            "PRD instead of forcing an unrelated scaffold."
        ),
        matches=[],
        conflicts=[],
        must_change=["derive a file plan from the PRD and generate each file"],
        fit_score=0.0,
    )
