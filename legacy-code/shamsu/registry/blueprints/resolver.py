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

    Suggestions are not selected for completely unspecified slots and never
    mutate ``required_stack``. When the PRD explicitly requires a layer (for
    example a web-only responsive frontend) but does not name that layer's
    framework, the suggested blueprint is selected as the concrete local
    implementation and recorded as an assumption.
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
    tokens = _without_frontend_tooling_node(tokens, contract)
    selected: dict[str, StackBlueprint] = {}
    unsupported: list[str] = []
    conflicts: list[str] = []
    implied_selected: set[str] = set()

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

    if "frontend" not in selected and _contract_requires_frontend_layer(contract):
        blueprint = blueprint_by_id(SUGGESTIONS["frontend"])
        if blueprint is not None and blueprint in available:
            selected["frontend"] = blueprint
            implied_selected.add("frontend")
        elif blueprint is not None:
            conflicts.append(
                f"Frontend layer is required, but suggested blueprint {blueprint.id} is prohibited."
            )

    suggestions: dict[str, StackBlueprint] = {}
    assumptions: list[str] = []
    for slot, blueprint_id in SUGGESTIONS.items():
        if slot in selected:
            if slot in implied_selected:
                assumptions.append(
                    "Frontend layer is required but no frontend framework was specified; "
                    f"using suggested blueprint: {selected[slot].id}."
                )
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


def _without_frontend_tooling_node(tokens: set[str], contract: PRDContract) -> set[str]:
    """Do not turn React/Vite's Node tooling into a backend service."""
    frontend_tokens = {"react", "vite", "vue", "svelte", "typescript", "tsx", "jsx"}
    backend_tokens = {"express", "fastify", "koa", "nest", "hapi"}
    if "node" not in tokens or not (tokens & frontend_tokens) or tokens & backend_tokens:
        return tokens
    if _contract_needs_backend_service(contract, tokens):
        return tokens
    return {token for token in tokens if token != "node"}


def _contract_needs_backend_service(contract: PRDContract, tokens: set[str]) -> bool:
    database_tokens = {"postgres", "mysql", "mariadb", "mongodb"}
    if tokens & database_tokens:
        return True
    if (
        contract.requires_full_stack
        or contract.entities
        or contract.api_endpoints
        or contract.authentication_rules
        or contract.authorization_rules
        or contract.persistence_requirements
    ):
        return True
    text = " ".join([contract.product_summary, *contract.architecture, *contract.features]).lower()
    return any(token in text for token in ("backend", "api", "server", "database"))


def _contract_requires_frontend_layer(contract: PRDContract) -> bool:
    text = " ".join(
        [
            contract.title,
            contract.product_summary,
            contract.stack_hint,
            *contract.required_stack,
            *contract.architecture,
            *contract.features,
            *contract.screens,
            *contract.nonfunctional_requirements,
        ]
    ).lower()
    return bool(
        "web-only" in text
        or "web only" in text
        or "frontend" in text
        or "front-end" in text
        or "browser ui" in text
        or "responsive user interface" in text
        or "responsive design" in text
    )


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
