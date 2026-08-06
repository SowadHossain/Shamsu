"""Safe skill package discovery and validation."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from shamsu.skills.types import SkillCatalog, SkillIssue, SkillPackage, SkillSource

SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
MAX_SKILL_INSTRUCTION_CHARS = 20_000
_SOURCE_ORDER: tuple[SkillSource, ...] = ("bundled", "user", "workspace")
_BLOCKED_POLICY_KEYS = {
    "approval",
    "approvals",
    "auto_approve",
    "bypass_sandbox",
    "elevated",
    "permissions",
    "run_without_approval",
    "unsafe",
}


def skills_mode_from_env() -> str:
    raw = os.environ.get("SHAMSU_SKILLS", "on").strip().lower()
    if raw in {"0", "false", "off", "no", "disabled"}:
        return "off"
    if raw in {"shadow", "dry-run", "dry_run", "log"}:
        return "shadow"
    return "on"


def discover_skills(workspace: Path | None = None) -> SkillCatalog:
    """Discover bundled, user, then workspace skills.

    Later sources override earlier skills with the same name. Invalid packages
    are reported as issues and excluded from the catalog; they never partially
    load and never grant permissions.
    """
    discovered: dict[str, SkillPackage] = {}
    issues: list[SkillIssue] = []
    for source, root in _discovery_roots(workspace):
        if not root.is_dir():
            continue
        for child in sorted(item for item in root.iterdir() if item.is_dir()):
            skill, skill_issues = _load_skill(child, source)
            issues.extend(skill_issues)
            if skill is not None:
                discovered[skill.name] = skill
    return SkillCatalog(skills=discovered, issues=tuple(issues))


def bundled_skills_root() -> Path:
    return Path(__file__).resolve().parent / "bundled"


def _discovery_roots(workspace: Path | None) -> list[tuple[SkillSource, Path]]:
    roots: list[tuple[SkillSource, Path]] = [
        ("bundled", bundled_skills_root()),
        ("user", Path.home() / ".shamsu" / "skills"),
    ]
    if workspace is not None:
        roots.append(("workspace", Path(workspace).resolve() / ".shamsu" / "skills"))
    return roots


def _load_skill(root: Path, source: SkillSource) -> tuple[SkillPackage | None, list[SkillIssue]]:
    issues: list[SkillIssue] = []
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        return None, [_issue(source, root, "missing SKILL.md")]
    try:
        raw = skill_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, [_issue(source, skill_path, f"SKILL.md is not valid UTF-8: {exc}")]
    if len(raw) > MAX_SKILL_INSTRUCTION_CHARS:
        return None, [_issue(source, skill_path, "SKILL.md exceeds the maximum supported size")]

    try:
        frontmatter, instructions = _split_frontmatter(raw)
    except yaml.YAMLError as exc:
        return None, [_issue(source, skill_path, f"SKILL.md frontmatter is invalid YAML: {exc}")]
    metadata, metadata_ok = _load_metadata(root, frontmatter, source, issues)
    if not metadata_ok:
        return None, issues
    name = str(metadata.get("name") or root.name).strip()
    if not SKILL_NAME_RE.fullmatch(name):
        return None, [*issues, _issue(source, root, f"invalid skill name `{name}`")]

    description = str(metadata.get("description") or "").strip()
    if not description:
        return None, [*issues, _issue(source, root, "skill description is required")]

    blocked = sorted(key for key in metadata if key.lower() in _BLOCKED_POLICY_KEYS)
    if blocked:
        return None, [
            *issues,
            _issue(
                source,
                root,
                "skill metadata may not request permissions or approval bypass: "
                + ", ".join(blocked),
            ),
        ]

    for key in ("resources", "templates", "scripts", "checks"):
        for rel_path in _as_string_tuple(metadata.get(key)):
            if not _is_safe_relative_path(rel_path):
                issues.append(_issue(source, root, f"unsafe {key} path `{rel_path}`"))
    if any(issue.severity == "error" for issue in issues):
        return None, issues

    return (
        SkillPackage(
            name=name,
            description=description,
            source=source,
            root=root,
            skill_path=skill_path,
            instructions=instructions.strip(),
            metadata=dict(metadata),
            version=str(metadata.get("version") or "0.1.0"),
            tags=_as_string_tuple(metadata.get("tags")),
            triggers=_as_string_tuple(metadata.get("triggers")),
            applies_to=_as_string_tuple(metadata.get("applies_to")),
            dependencies=_as_string_tuple(metadata.get("dependencies")),
            conflicts=_as_string_tuple(metadata.get("conflicts")),
            allowed_tools=_as_string_tuple(metadata.get("allowed_tools")),
            required_mcp=_as_string_tuple(metadata.get("required_mcp")),
            context_budget_tokens=_as_positive_int(metadata.get("context_budget_tokens"), 1200),
        ),
        issues,
    )


def _load_metadata(
    root: Path,
    frontmatter: dict[str, Any],
    source: SkillSource,
    issues: list[SkillIssue],
) -> tuple[dict[str, Any], bool]:
    metadata = dict(frontmatter)
    policy_path = root / "skill.json"
    if not policy_path.is_file():
        return metadata, True
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        issues.append(_issue(source, policy_path, f"skill.json is invalid JSON: {exc}"))
        return metadata, False
    if not isinstance(policy, dict):
        issues.append(_issue(source, policy_path, "skill.json must contain an object"))
        return metadata, False
    metadata.update(policy)
    return metadata, True


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    end = -1
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
    if end < 0:
        return {}, raw
    frontmatter_text = "\n".join(lines[1:end])
    parsed = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, "\n".join(lines[end + 1 :])


def _as_string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list | tuple | set):
        raw_items = list(value)
    else:
        return ()
    result: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _as_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _is_safe_relative_path(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _issue(
    source: SkillSource,
    path: Path,
    message: str,
    severity: str = "error",
) -> SkillIssue:
    return SkillIssue(
        source=source,
        path=str(path),
        message=message,
        severity="error" if severity == "error" else "warning",
    )
