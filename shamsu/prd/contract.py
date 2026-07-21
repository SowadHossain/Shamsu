"""Deterministic, source-grounded PRD contract extraction."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from shamsu.prd.extractor import extract_entities
from shamsu.types import ParsedPRD

_GAME_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("pong", ("pong",)),
    ("snake", ("snake",)),
    ("breakout", ("breakout", "brick breaker", "arkanoid")),
    ("tetris", ("tetris",)),
    ("flappy", ("flappy",)),
    ("space-invaders", ("space invaders", "space-invaders", "invaders")),
    ("asteroids", ("asteroids",)),
    ("2048", ("2048",)),
    ("minesweeper", ("minesweeper",)),
    ("tic-tac-toe", ("tic tac toe", "tic-tac-toe", "noughts and crosses")),
    ("platformer", ("platformer", "jump and run", "side-scroller", "side scroller")),
    ("shooter", ("shooter", "shmup", "bullet hell")),
    ("racing", ("racing", "racer")),
    ("puzzle", ("puzzle",)),
    ("rpg", ("rpg", "role-playing", "role playing")),
]

_MULTIPLAYER_HINTS = (
    "multiplayer", "multi-player", "online", "lobby", "matchmaking", "pvp",
    "co-op", "coop", "rooms", "netcode", "networked", "server-authoritative",
    "players connect", "join a room", "real-time multiplayer",
)
_3D_HINTS = ("3d", "three.js", "webgl", "r3f", "react-three", "voxel")
_CONTROL_TOKENS = (
    "arrow key", "arrow keys", "wasd", "space bar", "spacebar", "space key",
    "mouse", "click", "tap", "swipe", "up/down", "left/right", "enter key",
    "w/s", "a/d", "touch", "drag",
)
_STACK_PATTERNS: list[tuple[str, str]] = [
    ("django", r"\bdjango\b"),
    ("python", r"\b(?:flask|fastapi|python|pytest)\b"),
    ("node", r"\b(?:react|vue|svelte|node(?:\.js)?|express|vite|typescript|javascript|phaser)\b|\bnext\.js\b"),
    ("typescript", r"\btypescript\b"),
    ("react", r"\breact\b"),
    ("vite", r"\bvite\b"),
    ("zod", r"\bzod\b"),
    ("vitest", r"\bvitest\b"),
    ("playwright", r"\bplaywright\b"),
    ("go", r"\b(?:golang|go module)\b"),
    ("rust", r"\b(?:rust|cargo)\b"),
]
_HEADING_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+")
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[^\s,]+)", re.IGNORECASE)


@dataclass
class PRDContract:
    """Structured source of truth shared by planning, generation, and verification."""

    title: str = ""
    project_kind: str = "unknown"
    game_type: str = ""
    is_multiplayer: bool = False
    is_3d: bool = False
    mechanics: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    stack_hint: str = ""
    product_summary: str = ""
    users: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    architecture: list[str] = field(default_factory=list)
    required_stack: list[str] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    authentication_rules: list[str] = field(default_factory=list)
    authorization_rules: list[str] = field(default_factory=list)
    user_journeys: list[str] = field(default_factory=list)
    api_endpoints: list[dict[str, Any]] = field(default_factory=list)
    query_capabilities: list[str] = field(default_factory=list)
    persistence_requirements: list[str] = field(default_factory=list)
    validation_rules: list[str] = field(default_factory=list)
    error_states: list[str] = field(default_factory=list)
    security_requirements: list[str] = field(default_factory=list)
    nonfunctional_requirements: list[str] = field(default_factory=list)
    required_tests: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    source_refs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    extraction_confidence: float = 1.0
    extraction_warnings: list[str] = field(default_factory=list)

    @property
    def is_game(self) -> bool:
        return self.project_kind == "game"

    @property
    def requires_full_stack(self) -> bool:
        text = " ".join([self.product_summary, *self.architecture]).lower()
        return self.project_kind == "web_app" and (
            "full-stack" in text
            or "full stack" in text
            or bool(self.authentication_rules and self.persistence_requirements)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PRDContract":
        values: dict[str, Any] = {}
        for name, definition in cls.__dataclass_fields__.items():
            if name not in data:
                continue
            value = data[name]
            if definition.default_factory is list:
                value = list(value or [])
            elif definition.default_factory is dict:
                value = dict(value or {})
            values[name] = value
        return cls(**values)

    def render_brief(self) -> str:
        lines = [f"# PRD contract: {self.title or 'Untitled'}", f"kind: {self.project_kind}"]
        if self.product_summary:
            lines.append(f"summary: {self.product_summary}")
        if self.game_type:
            flavor = "multiplayer" if self.is_multiplayer else "single/local"
            dimensions = "3D" if self.is_3d else "2D"
            lines.append(f"game: {self.game_type} ({dimensions}, {flavor})")
        if self.required_stack:
            lines.append(f"required stack: {', '.join(self.required_stack)}")
        elif self.stack_hint:
            lines.append(f"stack hint: {self.stack_hint}")
        for label, items in (
            ("architecture", self.architecture),
            ("entities", [str(item.get("name", "")) for item in self.entities]),
            ("authentication", self.authentication_rules),
            ("authorization", self.authorization_rules),
            ("features", self.features or self.mechanics),
            ("screens", self.screens),
            ("acceptance criteria", self.acceptance_criteria),
            ("required tests", self.required_tests),
            ("constraints", self.constraints),
            ("assumptions", self.assumptions),
        ):
            if items:
                lines.append(f"{label}:")
                lines.extend(f"  - {item}" for item in items[:20] if item)
        if self.extraction_warnings:
            lines.append("extraction warnings:")
            lines.extend(f"  - {item}" for item in self.extraction_warnings)
        return "\n".join(lines)


def extract_contract(parsed: ParsedPRD) -> PRDContract:
    raw = parsed.raw_text or ""
    lowered = raw.lower()
    game_type = _detect_game_type(lowered)
    project_kind = _detect_kind(lowered, game_type)
    stack_hint = _detect_stack(lowered)
    summary_lines = _section_lines(parsed, "product summary", exact=True)
    if not summary_lines:
        summary_lines = _section_lines(parsed, "overview", exact=True)
    summary = " ".join(summary_lines[:2])

    mechanics = _section_lines(parsed, "mechanics", "gameplay", "functional requirements")
    controls = _section_lines(parsed, "controls", "input") or _scan_controls(lowered)
    screens = _section_lines(parsed, "screens", "frontend pages", "pages")
    acceptance = _section_lines(parsed, "acceptance criteria", exact=True)
    constraints = _section_lines(
        parsed, "constraints", "non-functional requirements", "performance requirements"
    )
    feature_lines = _section_lines(parsed, "features", "functional requirements")
    if not feature_lines:
        feature_lines = [line for line in summary_lines if line.startswith(("-", "*", "\u2022"))]

    roles = _role_names(parsed)
    authentication = _section_lines(
        parsed, "user registration", "user login", "logout", "authentication and authorization",
        "protected routes", "authentication security", "password security",
    )
    authorization = _section_lines(
        parsed, "ownership validation", "unauthorized requests", "authorization security"
    )
    journeys = _section_lines(parsed, "main user journey", "new user journey", "returning user journey")
    persistence = _section_lines(
        parsed, "database requirements", "database schema", "sqlite-specific requirements",
        "database characteristics", "database file", "foreign keys", "transactions", "backups",
    )
    validation = _section_lines(parsed, "data validation rules")
    validation.extend(_section_lines(parsed, "validation", exclude=("error",)))
    errors = _section_lines(parsed, "error handling", "validation errors", "common error types")
    security = _section_lines(parsed, "security requirements", "password security", "authentication security",
                              "authorization security", "input security", "request protection", "secrets")
    nonfunctional = _section_lines(
        parsed, "accessibility requirements", "responsive design requirements", "performance requirements",
        "audit and logging requirements", "date and time handling",
    )
    tests = _section_lines(parsed, "testing requirements", "authentication tests", "authorization tests",
                           "task tests", "category tests", "profile tests", "database tests")
    out_of_scope = _section_lines(parsed, "out of scope for initial release", exact=True)
    entities = [asdict(entity) for entity in extract_entities(parsed)]
    endpoints = _extract_api_endpoints(parsed)

    architecture: list[str] = []
    for phrase in ("full-stack", "full stack", "backend api", "sqlite", "responsive user interface"):
        if phrase in lowered:
            architecture.append(phrase)
    required_stack = [name for name, pattern in _STACK_PATTERNS if re.search(pattern, lowered)]
    if re.search(r"\bsqlite\b", lowered):
        required_stack.append("sqlite")
    assumptions: list[str] = []
    if project_kind == "web_app" and architecture and not stack_hint:
        assumptions.append("The PRD does not specify an application framework.")

    warnings = list(parsed.extraction_warnings)
    if project_kind == "web_app" and persistence and not entities:
        warnings.append("The PRD requires persistence, but no entities were extracted.")

    return PRDContract(
        title=parsed.title,
        project_kind=project_kind,
        game_type=game_type,
        is_multiplayer=_any(lowered, _MULTIPLAYER_HINTS),
        is_3d=_any(lowered, _3D_HINTS),
        mechanics=_dedupe(_clean_items(mechanics))[:40],
        controls=_dedupe(_clean_items(controls))[:20],
        screens=_dedupe(_clean_items(screens))[:30],
        acceptance_criteria=_dedupe(_clean_items(acceptance))[:60],
        constraints=_dedupe(_clean_items(constraints))[:30],
        features=_dedupe(_clean_items(feature_lines or mechanics))[:40],
        stack_hint=stack_hint,
        product_summary=summary,
        users=_dedupe(_clean_items(_section_lines(parsed, "target users", exact=True)))[:20],
        roles=roles,
        architecture=_dedupe(architecture),
        required_stack=_dedupe(required_stack),
        entities=entities,
        authentication_rules=_dedupe(_clean_items(authentication))[:80],
        authorization_rules=_dedupe(_clean_items(authorization))[:40],
        user_journeys=_dedupe(_clean_items(journeys))[:40],
        api_endpoints=endpoints,
        query_capabilities=_query_capabilities(parsed, lowered),
        persistence_requirements=_dedupe(_clean_items(persistence))[:60],
        validation_rules=_dedupe(_clean_items(validation))[:60],
        error_states=_dedupe(_clean_items(errors))[:40],
        security_requirements=_dedupe(_clean_items(security))[:80],
        nonfunctional_requirements=_dedupe(_clean_items(nonfunctional))[:60],
        required_tests=_dedupe(_clean_items(tests))[:100],
        out_of_scope=_dedupe(_clean_items(out_of_scope))[:40],
        assumptions=assumptions,
        source_refs={key: list(value) for key, value in parsed.source_refs.items()},
        extraction_confidence=parsed.extraction_confidence,
        extraction_warnings=_dedupe(warnings),
    )


def _section_lines(
    parsed: ParsedPRD,
    *terms: str,
    exact: bool = False,
    exclude: tuple[str, ...] = (),
) -> list[str]:
    result: list[str] = []
    normalized_terms = tuple(term.lower() for term in terms)
    for heading, lines in parsed.sections.items():
        normalized = _normalize_heading(heading)
        if any(term in normalized for term in exclude):
            continue
        matches = (
            normalized in normalized_terms
            if exact
            else any(normalized == term or normalized.startswith(f"{term} ") for term in normalized_terms)
        )
        if matches:
            result.extend(lines)
    return result


def _normalize_heading(heading: str) -> str:
    return _HEADING_NUMBER_RE.sub("", heading.strip()).rstrip(":").lower()


def _role_names(parsed: ParsedPRD) -> list[str]:
    roles: list[str] = []
    for heading in parsed.sections:
        normalized = _normalize_heading(heading)
        if normalized in {"regular user", "administrator", "admin", "user"}:
            roles.append(normalized.title())
    return _dedupe(roles)


def _extract_api_endpoints(parsed: ParsedPRD) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for heading, lines in parsed.sections.items():
        context = " ".join([heading, *lines]).lower()
        for line in lines:
            match = _ENDPOINT_RE.search(line)
            if not match:
                continue
            method = match.group(1).upper()
            path = match.group(2).rstrip(".;:")
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            public_endpoint = path.rstrip("/").endswith(("/register", "/login"))
            endpoints.append(
                {
                    "method": method,
                    "path": path,
                    "auth_required": not public_endpoint and not any(
                        token in context for token in ("public endpoint", "no authentication")
                    ),
                }
            )
    return endpoints


def _query_capabilities(parsed: ParsedPRD, lowered: str) -> list[str]:
    capabilities: list[str] = []
    headings = {_normalize_heading(heading) for heading in parsed.sections}
    checks = {
        "search": ("search",),
        "filter": ("filtering", "filter"),
        "sort": ("sorting", "sort"),
        "pagination": ("pagination", "page", "limit"),
    }
    for name, signals in checks.items():
        if any(signal in headings or re.search(rf"\b{re.escape(signal)}\b", lowered) for signal in signals):
            capabilities.append(name)
    return capabilities


def _detect_game_type(lowered: str) -> str:
    for name, needles in _GAME_TYPES:
        if _any(lowered, needles):
            return name
    return ""


def _detect_kind(lowered: str, game_type: str) -> str:
    if game_type or re.search(r"\bgame\b", lowered):
        return "game"
    if _any(lowered, ("cms", "content management", "headless cms", "blog engine", "wiki")):
        return "cms"
    if _any(lowered, ("full-stack", "full stack", "web app", "web application", "website", "dashboard", "portal")):
        return "web_app"
    if _any(lowered, ("rest api", "restful", "graphql", "api endpoint", "json api", "web service")):
        return "api"
    if re.search(r"\bcli\b|command[- ]line|terminal tool", lowered):
        return "cli"
    if _any(lowered, ("microservice", "worker", "daemon", "background service", "cron")):
        return "service"
    return "unknown"


def _detect_stack(lowered: str) -> str:
    for name, pattern in _STACK_PATTERNS:
        if re.search(pattern, lowered):
            return name
    return ""


def _scan_controls(lowered: str) -> list[str]:
    return [token for token in _CONTROL_TOKENS if token in lowered]


def _clean_items(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        value = item.strip().lstrip("-*+\u2022 ").strip()
        if value and value.lower() not in {"test:", "fields:", "constraints:", "indexes:"}:
            cleaned.append(value)
    return cleaned


def _any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result
