"""Disk-backed project category registry for SHAMSU v2.3."""
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
    "detect_category",
    "load_registry_entry",
    "scaffold_template",
    "stack_policy_for",
]
