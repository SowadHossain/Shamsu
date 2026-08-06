"""Skill package contracts.

Skills are instructions and resources, not authority. They can narrow context,
name useful checks, and provide templates, but permissions, tools, sandboxing,
and command execution remain owned by SHAMSU's harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


SkillSource = Literal["bundled", "user", "workspace"]
SkillMode = Literal["off", "shadow", "on"]


@dataclass(frozen=True)
class SkillIssue:
    source: SkillSource
    path: str
    message: str
    severity: Literal["warning", "error"] = "error"


@dataclass(frozen=True)
class SkillPackage:
    name: str
    description: str
    source: SkillSource
    root: Path
    skill_path: Path
    instructions: str
    metadata: dict[str, Any] = field(default_factory=dict)
    version: str = "0.1.0"
    tags: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    applies_to: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    required_mcp: tuple[str, ...] = ()
    context_budget_tokens: int = 1200

    def to_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "path": str(self.skill_path),
            "version": self.version,
            "tags": list(self.tags),
            "triggers": list(self.triggers),
            "applies_to": list(self.applies_to),
            "dependencies": list(self.dependencies),
            "conflicts": list(self.conflicts),
            "allowed_tools": list(self.allowed_tools),
            "required_mcp": list(self.required_mcp),
            "context_budget_tokens": self.context_budget_tokens,
        }


@dataclass(frozen=True)
class SkillCatalog:
    skills: dict[str, SkillPackage] = field(default_factory=dict)
    issues: tuple[SkillIssue, ...] = ()

    def sorted_skills(self) -> list[SkillPackage]:
        return sorted(self.skills.values(), key=lambda skill: skill.name)


@dataclass(frozen=True)
class SelectedSkill:
    skill: SkillPackage
    score: float
    reasons: tuple[str, ...] = ()

    def to_summary(self) -> dict[str, Any]:
        data = self.skill.to_summary()
        data.update({"score": self.score, "reasons": list(self.reasons)})
        return data


@dataclass(frozen=True)
class SkillSelection:
    mode: SkillMode
    selected: tuple[SelectedSkill, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()
    issues: tuple[SkillIssue, ...] = ()
    budget_tokens: int = 0

    @property
    def active(self) -> bool:
        return self.mode == "on" and bool(self.selected)

    def to_summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "selected": [item.to_summary() for item in self.selected],
            "rejected": list(self.rejected),
            "issues": [
                {
                    "source": issue.source,
                    "path": issue.path,
                    "message": issue.message,
                    "severity": issue.severity,
                }
                for issue in self.issues
            ],
            "budget_tokens": self.budget_tokens,
        }
