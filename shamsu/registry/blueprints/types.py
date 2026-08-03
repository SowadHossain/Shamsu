"""Composable stack blueprint primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StackBlueprint:
    id: str
    slot: str
    provides: tuple[str, ...]
    root: str
    folder_map: dict[str, str]
    config_files: tuple[str, ...] = ()
    verify: tuple[str, ...] = ()
    description: str = ""

    def path_for(self, kind: str) -> str:
        return _under_root(self.root, self.folder_map[kind])

    def config_paths(self) -> tuple[str, ...]:
        return tuple(_under_root(self.root, path) for path in self.config_files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slot": self.slot,
            "provides": list(self.provides),
            "root": self.root,
            "folder_map": dict(self.folder_map),
            "config_files": list(self.config_files),
            "verify": list(self.verify),
            "description": self.description,
        }


def _under_root(root: str, relative: str) -> str:
    normalized_root = root.strip().strip("/\\").replace("\\", "/")
    normalized_path = relative.strip().strip("/\\").replace("\\", "/")
    if not normalized_path:
        return normalized_root or "."
    if not normalized_root or normalized_root == ".":
        return normalized_path
    if normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/"):
        return normalized_path
    return f"{normalized_root}/{normalized_path}"


@dataclass(frozen=True)
class BlueprintResolution:
    selected: dict[str, StackBlueprint] = field(default_factory=dict)
    suggestions: dict[str, StackBlueprint] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    unavailable: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unsupported: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": {
                slot: blueprint.to_dict()
                for slot, blueprint in sorted(self.selected.items())
            },
            "suggestions": {
                slot: blueprint.to_dict()
                for slot, blueprint in sorted(self.suggestions.items())
            },
            "assumptions": list(self.assumptions),
            "unavailable": {
                key: list(value) for key, value in sorted(self.unavailable.items())
            },
            "unsupported": list(self.unsupported),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
        }
