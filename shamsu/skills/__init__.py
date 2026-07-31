"""Skill package discovery, ingestion, selection, and context rendering."""
from shamsu.skills.ingest import PreparedReference, ReferenceIngestError, prepare_reference
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
    "PreparedReference",
    "ReferenceIngestError",
    "SkillCatalog",
    "SkillIssue",
    "SkillPackage",
    "SkillSelection",
    "discover_skills",
    "prepare_reference",
    "render_skill_context",
    "select_skills_for_task",
    "skills_mode_from_env",
]
