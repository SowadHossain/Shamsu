"""Resolve explicit PRD stack choices into composable blueprints."""
from __future__ import annotations

import re
from collections.abc import Iterable

from shamsu.prd.contract import PRDContract
from shamsu.registry.blueprints.backend_django import BLUEPRINT as DJANGO
from shamsu.registry.blueprints.backend_node_express import BLUEPRINT as NODE_EXPRESS
from shamsu.registry.blueprints.database_postgres import BLUEPRINT as POSTGRES
from shamsu.registry.blueprints.database_sqlite import BLUEPRINT as SQLITE
from shamsu.registry.blueprints.frontend_django_templates import (
    BLUEPRINT as DJANGO_TEMPLATES,
)
from shamsu.registry.blueprints.frontend_react_vite import BLUEPRINT as REACT_VITE
from shamsu.registry.blueprints.types import BlueprintResolution, StackBlueprint

BLUEPRINTS: tuple[StackBlueprint, ...] = (
    DJANGO,
    NODE_EXPRESS,
    REACT_VITE,
    DJANGO_TEMPLATES,
    POSTGRES,
    SQLITE,
)

SUGGESTIONS: dict[str, str] = {
    "backend": "node-express",
    "frontend": "react-vite",
    "database": "postgres",
}

TECHNOLOGY_SLOTS: dict[str, str] = {
    "django": "backend",
    "python": "backend",
    "node": "backend",
    "node.js": "backend",
    "express": "backend",
    "rails": "backend",
    "ruby": "backend",
    "laravel": "backend",
    "php": "backend",
    "go": "backend",
    "rust": "backend",
    "react": "frontend",
    "vite": "frontend",
    "typescript": "frontend",
    "tsx": "frontend",
    "jsx": "frontend",
    "django-templates": "frontend",
    "server-rendered-html": "frontend",
    "postgres": "database",
    "postgresql": "database",
    "mysql": "database",
    "mariadb": "database",
    "mongodb": "database",
    "sqlite": "database",
}

_ALIASES = {
    "postgresql": "postgres",
    "node.js": "node",
    "express.js": "express",
    "sqlite3": "sqlite",
}


def all_blueprints() -> tuple[StackBlueprint, ...]:
    return BLUEPRINTS


def blueprint_by_id(blueprint_id: str) -> StackBlueprint | None:
    normalized = _normalize_token(blueprint_id)
    for blueprint in BLUEPRINTS:
        if blueprint.id == normalized:
            return blueprint
    return None


def resolve_blueprints(contract: PRDContract) -> BlueprintResolution:
    """Resolve explicit stack tokens and separately report suggestions.

    Suggestions are not selected and never mutate ``required_stack``. They are
    returned as assumptions so the planner can ask or log a defaultable slot
    without pretending the PRD required it.
    """
    prohibited = {_normalize_token(item) for item in contract.prohibitions}
    available = [
        blueprint for blueprint in BLUEPRINTS
        if not (set(blueprint.provides) & prohibited)
    ]
    unavailable = {
        blueprint.id: tuple(sorted(set(blueprint.provides) & prohibited))
        for blueprint in BLUEPRINTS
        if set(blueprint.provides) & prohibited
    }
    tokens = _contract_tokens(contract)
    selected: dict[str, StackBlueprint] = {}
    unsupported: list[str] = []
    conflicts: list[str] = []

    for slot in ("backend", "frontend", "database"):
        slot_tokens = sorted(token for token in tokens if TECHNOLOGY_SLOTS.get(token) == slot)
        if not slot_tokens:
            continue
        matches = [
            blueprint for blueprint in available
            if blueprint.slot == slot and set(blueprint.provides) & set(slot_tokens)
        ]
        if not matches:
            unsupported.extend(slot_tokens)
            continue
        matches.sort(
            key=lambda blueprint: (
                len(set(blueprint.provides) & set(slot_tokens)),
                -BLUEPRINTS.index(blueprint),
            ),
            reverse=True,
        )
        selected[slot] = matches[0]
        if len({match.id for match in matches}) > 1:
            conflicts.append(
                f"{slot} stack names several compatible blueprints; selected {matches[0].id}."
            )

    suggestions: dict[str, StackBlueprint] = {}
    assumptions: list[str] = []
    for slot, blueprint_id in SUGGESTIONS.items():
        if slot in selected:
            continue
        blueprint = blueprint_by_id(blueprint_id)
        if blueprint is None:
            continue
        if blueprint not in available:
            conflicts.append(
                f"Suggested {slot} blueprint {blueprint.id} is unavailable because it is prohibited."
            )
            continue
        suggestions[slot] = blueprint
        assumptions.append(
            f"No {slot} stack was specified; suggested blueprint: {blueprint.id}."
        )

    errors = [
        f"No blueprint exists for explicit stack token: {token}."
        for token in sorted(dict.fromkeys(unsupported))
    ]
    return BlueprintResolution(
        selected=selected,
        suggestions=suggestions,
        assumptions=tuple(assumptions),
        unavailable=unavailable,
        unsupported=tuple(sorted(dict.fromkeys(unsupported))),
        conflicts=tuple(conflicts),
        errors=tuple(errors),
    )


def _contract_tokens(contract: PRDContract) -> set[str]:
    parts = [contract.stack_hint, *contract.required_stack]
    return {
        normalized for normalized in (_normalize_token(token) for token in parts) if normalized
    }


def _normalize_token(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    return _ALIASES.get(text, text)


def token_slots(tokens: Iterable[str]) -> dict[str, str]:
    return {
        token: TECHNOLOGY_SLOTS[token]
        for token in (_normalize_token(item) for item in tokens)
        if token in TECHNOLOGY_SLOTS
    }
