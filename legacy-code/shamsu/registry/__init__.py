"""Disk-backed project category registry for SHAMSU v2.3."""
from shamsu.registry.blueprints import (
    BlueprintResolution,
    StackBlueprint,
    all_blueprints,
    blueprint_by_id,
    resolve_blueprints,
)
from shamsu.registry.detector import CategoryDecision, detect_category
from shamsu.registry.loader import load_registry_entry
from shamsu.registry.scaffold import ScaffoldResult, scaffold_template
from shamsu.registry.schema import Category, DefinitionOfDone, DoDItem, Hole, Manifest, RegistryEntry
from shamsu.registry.stack_policy import StackPolicy, stack_policy_for

__all__ = [
    "Category",
    "CategoryDecision",
    "DefinitionOfDone",
    "DoDItem",
    "Hole",
    "Manifest",
    "RegistryEntry",
    "ScaffoldResult",
    "StackPolicy",
    "BlueprintResolution",
    "StackBlueprint",
    "all_blueprints",
    "blueprint_by_id",
    "detect_category",
    "load_registry_entry",
    "resolve_blueprints",
    "scaffold_template",
    "stack_policy_for",
]
