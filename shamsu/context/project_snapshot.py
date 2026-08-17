"""Persistent, deterministic project context for small executor prompts."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shamsu.agents.project_instructions import find_instruction_file
from shamsu.safety.sandbox import Sandbox


SCHEMA_VERSION = 1
SNAPSHOT_PATH = Path(".shamsu") / "project" / "context.json"

IMPORTANT_FILENAMES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "Cargo.toml",
    "go.mod",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yml",
    "vite.config.js",
    "vite.config.ts",
    "tsconfig.json",
    "pytest.ini",
)

EXCLUDED_SCAN_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".shamsu",
    ".tox",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
}


@dataclass(frozen=True)
class ProjectFact:
    key: str
    value: str
    source: str
    confidence: str = "high"


@dataclass(frozen=True)
class ProjectSnapshot:
    schema_version: int
    refreshed_at: str
    fingerprint: str
    identity: dict[str, Any] = field(default_factory=dict)
    tech_stack: dict[str, list[str]] = field(default_factory=dict)
    invariants: list[str] = field(default_factory=list)
    facts: list[ProjectFact] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    important_files: list[str] = field(default_factory=list)
    explicit_instruction_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["facts"] = [asdict(fact) for fact in self.facts]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSnapshot":
        facts = [
            ProjectFact(**item)
            for item in data.get("facts", [])
            if isinstance(item, dict) and item.get("key") and item.get("value")
        ]
        return cls(
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            refreshed_at=str(data.get("refreshed_at") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            identity=dict(data.get("identity") or {}),
            tech_stack={
                str(key): [str(item) for item in value if str(item)]
                for key, value in dict(data.get("tech_stack") or {}).items()
                if isinstance(value, list)
            },
            invariants=[str(item) for item in data.get("invariants", []) if str(item)],
            facts=facts,
            commands={str(key): str(value) for key, value in dict(data.get("commands") or {}).items()},
            important_files=[str(item) for item in data.get("important_files", []) if str(item)],
            explicit_instruction_source=str(data.get("explicit_instruction_source") or ""),
        )


def load_project_snapshot(workspace: Path) -> ProjectSnapshot:
    """Return a fresh-enough snapshot, rebuilding and persisting when needed."""
    workspace = Path(workspace).resolve()
    fingerprint = _fingerprint(workspace)
    path = Sandbox(workspace).validate(SNAPSHOT_PATH)
    cached = _load_cached(path)
    if cached is not None and cached.schema_version == SCHEMA_VERSION and cached.fingerprint == fingerprint:
        return cached
    snapshot = build_project_snapshot(workspace, fingerprint=fingerprint)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8")
    except OSError:
        pass
    return snapshot


def build_project_snapshot(workspace: Path, *, fingerprint: str | None = None) -> ProjectSnapshot:
    workspace = Path(workspace).resolve()
    facts: list[ProjectFact] = []
    commands: dict[str, str] = {}
    important_files = _important_files(workspace)
    package = _read_json(workspace / "package.json")
    pyproject = _read_toml(workspace / "pyproject.toml")
    requirements = _read_requirements(workspace)
    compose_text = _read_any_text(workspace, ("docker-compose.yml", "compose.yml"))

    if package:
        _collect_node_facts(package, facts, commands)
    if pyproject or requirements:
        _collect_python_facts(pyproject, requirements, facts, commands)
    if compose_text:
        _collect_compose_facts(compose_text, facts)
    _collect_language_facts(workspace, facts)

    instruction_source, explicit_rules = _explicit_instruction_rules(workspace)
    tech_stack = _tech_stack_from_facts(facts)
    identity = _identity(workspace, package, pyproject, facts)
    invariants = _invariants(identity, tech_stack, facts, explicit_rules)
    return ProjectSnapshot(
        schema_version=SCHEMA_VERSION,
        refreshed_at=datetime.now(UTC).isoformat(),
        fingerprint=fingerprint or _fingerprint(workspace),
        identity=identity,
        tech_stack=tech_stack,
        invariants=invariants,
        facts=facts,
        commands=commands,
        important_files=important_files,
        explicit_instruction_source=instruction_source,
    )


def render_project_identity(snapshot: ProjectSnapshot) -> str:
    identity = snapshot.identity
    lines = [
        f"project: {identity.get('name') or 'unknown'}",
        f"type: {identity.get('project_type') or 'unknown'}",
    ]
    languages = identity.get("primary_languages") or []
    lines.append("primary_languages: " + (", ".join(languages) if languages else "unknown"))
    return "\n".join(lines)


def render_tech_stack(snapshot: ProjectSnapshot) -> str:
    if not snapshot.tech_stack:
        return "No deterministic tech stack detected."
    labels = {
        "frontend": "Frontend",
        "backend": "Backend",
        "language_runtime": "Language/runtime",
        "database": "Database",
        "orm": "ORM/database client",
        "package_manager": "Package manager",
        "build_system": "Build system",
        "testing": "Testing",
        "infrastructure": "Infrastructure",
    }
    lines = [
        f"{labels.get(key, key)}: {', '.join(values)}"
        for key, values in snapshot.tech_stack.items()
        if values
    ]
    lines.append(
        "STACK RULE: Do not introduce or replace major framework, database, or build-system "
        "components unless the task explicitly requires it or scope is deliberately replanned."
    )
    return "\n".join(lines)


def render_project_invariants(snapshot: ProjectSnapshot) -> str:
    if not snapshot.invariants:
        return "No deterministic project invariants registered."
    return "\n".join(f"- {item}" for item in snapshot.invariants[:8])


def _load_cached(path: Path) -> ProjectSnapshot | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ProjectSnapshot.from_dict(data)
    except (TypeError, ValueError):
        return None


def _fingerprint(workspace: Path) -> str:
    digest = hashlib.sha256()
    for relative in _important_files(workspace):
        path = workspace / relative
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_size).encode("ascii"))
    instruction = find_instruction_file(workspace)
    if instruction is not None:
        try:
            stat = instruction.stat()
            digest.update(str(instruction.relative_to(workspace)).encode("utf-8", errors="replace"))
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
            digest.update(str(stat.st_size).encode("ascii"))
        except (OSError, ValueError):
            pass
    return digest.hexdigest()


def _important_files(workspace: Path) -> list[str]:
    found: list[str] = []
    for name in IMPORTANT_FILENAMES:
        path = workspace / name
        try:
            if path.is_file():
                found.append(name)
        except OSError:
            continue
    instruction = find_instruction_file(workspace)
    if instruction is not None:
        try:
            relative = instruction.relative_to(workspace).as_posix()
        except ValueError:
            relative = instruction.name
        if relative not in found:
            found.append(relative)
    return found


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_requirements(workspace: Path) -> list[str]:
    packages: list[str] = []
    for name in ("requirements.txt", "requirements-dev.txt"):
        try:
            text = (workspace / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            cleaned = line.split("#", 1)[0].strip()
            if cleaned:
                packages.append(cleaned)
    return packages


def _read_any_text(workspace: Path, names: tuple[str, ...]) -> str:
    for name in names:
        try:
            return (workspace / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


def _collect_node_facts(
    package: dict[str, Any],
    facts: list[ProjectFact],
    commands: dict[str, str],
) -> None:
    deps = _package_names(package.get("dependencies"), package.get("devDependencies"))
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    for command_name in ("dev", "start", "build", "test", "lint", "typecheck", "format"):
        value = scripts.get(command_name)
        if isinstance(value, str) and value.strip():
            commands.setdefault(command_name, value.strip())
    if "react" in deps:
        _fact(facts, "frontend", _with_version("React", deps["react"]), "package.json")
    if "vite" in deps or any("vite" in command for command in scripts.values() if isinstance(command, str)):
        _fact(facts, "build_system", "Vite", "package.json")
    if "typescript" in deps:
        _fact(facts, "language_runtime", "TypeScript", "package.json")
    for name, label in (("next", "Next.js"), ("vue", "Vue"), ("svelte", "Svelte")):
        if name in deps:
            _fact(facts, "frontend", label, "package.json")
    for name, label in (("express", "Express"), ("fastify", "Fastify"), ("@nestjs/core", "NestJS")):
        if name in deps:
            _fact(facts, "backend", label, "package.json")
    for name, label in (("prisma", "Prisma"), ("@prisma/client", "Prisma")):
        if name in deps:
            _fact(facts, "orm", label, "package.json")
    for name, label in (("vitest", "Vitest"), ("jest", "Jest"), ("playwright", "Playwright")):
        if name in deps:
            _fact(facts, "testing", label, "package.json")
    _fact(facts, "language_runtime", "Node.js", "package.json")


def _collect_python_facts(
    pyproject: dict[str, Any],
    requirements: list[str],
    facts: list[ProjectFact],
    commands: dict[str, str],
) -> None:
    deps = _python_dep_names(pyproject, requirements)
    source = "pyproject.toml" if pyproject else "requirements.txt"
    if "fastapi" in deps:
        _fact(facts, "backend", "FastAPI", source)
    if "django" in deps:
        _fact(facts, "backend", "Django", source)
        _fact(facts, "orm", "Django ORM", source)
    if "flask" in deps:
        _fact(facts, "backend", "Flask", source)
    if "pygame" in deps:
        _fact(facts, "application_framework", "Pygame", source)
    if "sqlalchemy" in deps:
        _fact(facts, "orm", "SQLAlchemy", source)
    if "psycopg" in deps or "psycopg2" in deps or "asyncpg" in deps:
        _fact(facts, "database_client", "PostgreSQL client", source)
    if "pytest" in deps:
        _fact(facts, "testing", "Pytest", source)
        commands.setdefault("test", "pytest")
    _fact(facts, "language_runtime", "Python", source)


def _collect_compose_facts(text: str, facts: list[ProjectFact]) -> None:
    lowered = text.lower()
    source = "compose.yml/docker-compose.yml"
    if "postgres" in lowered:
        _fact(facts, "database", "PostgreSQL", source)
    if "mysql" in lowered or "mariadb" in lowered:
        _fact(facts, "database", "MySQL/MariaDB", source)
    if "redis" in lowered:
        _fact(facts, "infrastructure", "Redis", source)
    if "docker" in lowered or "services:" in lowered:
        _fact(facts, "infrastructure", "Docker Compose", source)


def _collect_language_facts(workspace: Path, facts: list[ProjectFact]) -> None:
    counts: dict[str, int] = {}
    for path in workspace.rglob("*"):
        if any(part in EXCLUDED_SCAN_DIRS for part in path.parts) or not path.is_file():
            continue
        suffix = path.suffix.lower()
        language = {
            ".py": "Python",
            ".ts": "TypeScript",
            ".tsx": "TypeScript",
            ".js": "JavaScript",
            ".jsx": "JavaScript",
            ".rs": "Rust",
            ".go": "Go",
        }.get(suffix)
        if language:
            counts[language] = counts.get(language, 0) + 1
    for language, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]:
        _fact(facts, "primary_language", language, "file extensions", confidence="medium")


def _explicit_instruction_rules(workspace: Path) -> tuple[str, list[str]]:
    path = find_instruction_file(workspace)
    if path is None:
        return "", []
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", []
    rules: list[str] = []
    for line in body.splitlines():
        cleaned = line.strip().lstrip("-*0123456789. ").strip()
        lowered = cleaned.lower()
        if not cleaned or len(cleaned) > 180:
            continue
        if any(marker in lowered for marker in ("do not ", "don't ", "never ", "must ", "keep ", "use existing", "no ")):
            rules.append(cleaned)
        if len(rules) >= 5:
            break
    try:
        source = path.relative_to(workspace).as_posix()
    except ValueError:
        source = path.name
    return source, rules


def _tech_stack_from_facts(facts: list[ProjectFact]) -> dict[str, list[str]]:
    slots = {
        "frontend": "frontend",
        "backend": "backend",
        "application_framework": "frontend",
        "language_runtime": "language_runtime",
        "database": "database",
        "orm": "orm",
        "database_client": "orm",
        "build_system": "build_system",
        "testing": "testing",
        "infrastructure": "infrastructure",
    }
    stack: dict[str, list[str]] = {}
    for fact in facts:
        slot = slots.get(fact.key)
        if not slot:
            continue
        stack.setdefault(slot, [])
        if fact.value not in stack[slot]:
            stack[slot].append(fact.value)
    package_manager = _package_manager_from_facts(facts)
    if package_manager:
        stack["package_manager"] = package_manager
    order = (
        "frontend",
        "backend",
        "language_runtime",
        "database",
        "orm",
        "package_manager",
        "build_system",
        "testing",
        "infrastructure",
    )
    return {key: stack[key] for key in order if stack.get(key)}


def _package_manager_from_facts(facts: list[ProjectFact]) -> list[str]:
    sources = {fact.source for fact in facts}
    managers: list[str] = []
    if any(source == "package.json" for source in sources):
        managers.append("npm")
    if any(source.startswith("pyproject") for source in sources):
        managers.append("pip/pyproject")
    if any(source.startswith("requirements") for source in sources):
        managers.append("pip")
    return managers


def _identity(
    workspace: Path,
    package: dict[str, Any],
    pyproject: dict[str, Any],
    facts: list[ProjectFact],
) -> dict[str, Any]:
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    name = str(project.get("name") or package.get("name") or workspace.name)
    languages = _values_for(facts, "primary_language") or _values_for(facts, "language_runtime")
    project_type = "unknown"
    backends = set(_values_for(facts, "backend"))
    frontends = set(_values_for(facts, "frontend"))
    frameworks = set(_values_for(facts, "application_framework"))
    if "Pygame" in frameworks:
        project_type = "standalone Python/Pygame application"
    elif frontends and backends:
        project_type = "full-stack web application"
    elif frontends:
        project_type = "frontend web application"
    elif backends:
        project_type = "backend service"
    elif "Python" in languages:
        project_type = "Python project"
    return {
        "name": name,
        "project_type": project_type,
        "workspace_root": str(workspace),
        "primary_languages": languages[:3],
    }


def _invariants(
    identity: dict[str, Any],
    tech_stack: dict[str, list[str]],
    facts: list[ProjectFact],
    explicit_rules: list[str],
) -> list[str]:
    invariants: list[str] = []
    project_type = str(identity.get("project_type") or "")
    if project_type and project_type != "unknown":
        invariants.append(f"This project is a {project_type}.")
    if "Pygame" in tech_stack.get("frontend", []):
        invariants.extend(
            [
                "Do not introduce a web backend, HTML templates, or forms framework for game logic.",
                "Extend existing Python game modules instead of creating web application layers.",
            ]
        )
    if "PostgreSQL" in tech_stack.get("database", []):
        invariants.append("PostgreSQL is the detected project database; do not switch databases casually.")
    if "FastAPI" in tech_stack.get("backend", []):
        invariants.append("Keep backend API work inside the existing FastAPI architecture.")
    if "React" in ", ".join(tech_stack.get("frontend", [])):
        invariants.append("Keep frontend work inside the existing React application.")
    for rule in explicit_rules:
        if rule not in invariants:
            invariants.append(rule)
    return invariants[:8]


def _collect_versions(raw: dict[str, Any]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in raw.items() if isinstance(key, str)}


def _package_names(*sections: Any) -> dict[str, str]:
    packages: dict[str, str] = {}
    for section in sections:
        if isinstance(section, dict):
            packages.update(_collect_versions(section))
    return packages


def _python_dep_names(pyproject: dict[str, Any], requirements: list[str]) -> set[str]:
    values: list[str] = []
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    dependencies = project.get("dependencies") if isinstance(project.get("dependencies"), list) else []
    values.extend(str(item) for item in dependencies)
    optional = project.get("optional-dependencies") if isinstance(project.get("optional-dependencies"), dict) else {}
    for group in optional.values():
        if isinstance(group, list):
            values.extend(str(item) for item in group)
    poetry = pyproject.get("tool", {}).get("poetry", {}) if isinstance(pyproject.get("tool"), dict) else {}
    poetry_deps = poetry.get("dependencies") if isinstance(poetry, dict) else {}
    if isinstance(poetry_deps, dict):
        values.extend(str(key) for key in poetry_deps)
    values.extend(requirements)
    return {_normalize_package_name(value) for value in values if value}


def _normalize_package_name(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", value)
    if not match:
        return value.lower().strip()
    return match.group(1).lower().replace("_", "-")


def _values_for(facts: list[ProjectFact], key: str) -> list[str]:
    values: list[str] = []
    for fact in facts:
        if fact.key == key and fact.value not in values:
            values.append(fact.value)
    return values


def _with_version(label: str, version: str) -> str:
    cleaned = str(version or "").strip()
    if not cleaned:
        return label
    return f"{label} {cleaned}"


def _fact(
    facts: list[ProjectFact],
    key: str,
    value: str,
    source: str,
    *,
    confidence: str = "high",
) -> None:
    fact = ProjectFact(key=key, value=value, source=source, confidence=confidence)
    if fact not in facts:
        facts.append(fact)
