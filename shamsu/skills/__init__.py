"""Skill package discovery, selection, and context rendering."""
from shamsu.skills.loader import discover_skills, skills_mode_from_env
from shamsu.skills.selector import render_skill_context, select_skills_for_task
from shamsu.skills.types import (
    SelectedSkill,
    SkillCatalog,
    SkillIssue,
    SkillPackage,
    SkillSelection,
)

__all__ = [
    "SelectedSkill",
    "SkillCatalog",
    "SkillIssue",
    "SkillPackage",
    "SkillSelection",
    "discover_skills",
    "render_skill_context",
    "select_skills_for_task",
    "skills_mode_from_env",
]
