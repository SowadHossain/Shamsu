"""Composable stack blueprint registry."""
from __future__ import annotations

from shamsu.registry.blueprints.resolver import (
    BLUEPRINTS,
    SUGGESTIONS,
    all_blueprints,
    blueprint_by_id,
    resolve_blueprints,
    token_slots,
)
from shamsu.registry.blueprints.types import BlueprintResolution, StackBlueprint

__all__ = [
    "BLUEPRINTS",
    "SUGGESTIONS",
    "BlueprintResolution",
    "StackBlueprint",
    "all_blueprints",
    "blueprint_by_id",
    "resolve_blueprints",
    "token_slots",
]
