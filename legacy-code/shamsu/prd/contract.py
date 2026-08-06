"""Deterministic, source-grounded PRD contract extraction."""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from shamsu.prd import headings as _headings
from shamsu.prd.extractor import extract_entities
from shamsu.prd.parser import _is_structural_noise_line
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
    ("express", r"\bexpress(?:\.js)?\b"),
    ("typescript", r"\btypescript\b"),
    ("react", r"\breact\b"),
    ("vite", r"\bvite\b"),
    ("postgres", r"\bpostgres(?:ql)?\b"),
    ("mysql", r"\bmysql\b"),
    ("mariadb", r"\bmariadb\b"),
    ("mongodb", r"\bmongodb\b"),
    ("zod", r"\bzod\b"),
    ("vitest", r"\bvitest\b"),
    ("playwright", r"\bplaywright\b"),
    ("go", r"\b(?:golang|go module)\b"),
    ("rust", r"\b(?:rust|cargo)\b"),
]
# --- Prohibitions ----------------------------------------------------------
# Technologies a PRD or request can forbid. Deliberately wider than
# _STACK_PATTERNS: a PRD may forbid something SHAMSU would never have detected as
# a stack (sqlalchemy, jquery), and "not that" is still load-bearing.
_PROHIBITABLE_TECHNOLOGIES: tuple[str, ...] = (
    "django", "flask", "fastapi", "python", "node", "express", "react", "vue",
    "svelte", "vite", "typescript", "javascript", "next.js", "phaser", "jquery",
    "sqlite", "postgresql", "postgres", "mysql", "mariadb", "mongodb",
    "sqlalchemy", "go", "golang", "rust", "php", "laravel", "rails",
)
# Spelling variants collapse so a prohibition matches however it was written.
_TECHNOLOGY_ALIASES = {"postgresql": "postgres", "golang": "go"}
# A clause carrying one of these forbids the technologies named in it. Checked
# per clause, not per document: "use PostgreSQL, never SQLite" must forbid only
# SQLite, and a document-wide scan would forbid both.
_PROHIBITION_MARKERS: tuple[str, ...] = (
    "never", "do not use", "don't use", "dont use", "do not", "don't", "dont",
    "must not", "cannot use", "can't use", "avoid", "without", "no longer",
    "reject", "forbidden", "not allowed", "not permitted", "instead of",
    "rather than", "migrate away from",
)
# Deliberately NOT a marker: "replace". "replace SQLite with PostgreSQL" names the
# REPLACEMENT after the marker, so it would forbid the intended stack.
# Splits on sentence and parenthetical boundaries. A `.` only ends a clause when
# whitespace or end-of-text follows it, so dotted technology names survive: a bare
# `[.]` split turns "do not use Node.js or Express" into "do not use node" plus a
# marker-less "js or express", silently losing the Express prohibition.
_CLAUSE_SPLIT_RE = re.compile(r"[;\n]|\.(?=\s|$)|(?<=\))\s|\s(?=\()")

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
    # Technologies the PRD or request explicitly FORBIDS. Before this existed, a
    # negative constraint was unrepresentable: every contract field was a positive
    # accumulator, so "PostgreSQL 16, NEVER SQLite, NEVER Django" was not merely
    # ignored - the words were read as stack MENTIONS, so django landed in
    # required_stack and stack_hint, and render_brief then re-asserted the
    # forbidden stack to the model as a requirement on every turn. A prohibition
    # has nowhere to go, so it could not survive parsing.
    prohibitions: list[str] = field(default_factory=list)
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
        # Prohibitions come FIRST, and unmissably. This brief is re-asserted to the
        # model every turn, and it used to re-state the forbidden stack as
        # "required stack: django" - actively teaching the model to do the one thing
        # the PRD ruled out.
        if self.prohibitions:
            lines.append(
                "FORBIDDEN - do not use, and reject any plan that does: "
                + ", ".join(self.prohibitions)
            )
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


def extract_contract(
    parsed: ParsedPRD,
    *,
    resolve_headings: bool = True,
    request_text: str = "",
    extra_entities: list[Any] | None = None,
) -> PRDContract:
    # Section matching below is by heading name, so a PRD that words its
    # headings differently is invisible to every lookup. Fold the ones that
    # merely reword a known section onto the canonical name first. Idempotent:
    # headings that already match are left alone, so callers that resolved
    # headings themselves (including via the model) lose nothing.
    if resolve_headings:
        parsed, _ = _headings.resolve_parsed_headings(parsed)
    raw = parsed.raw_text or ""
    lowered = raw.lower()
    # The user's own instruction is part of the specification. A PRD that names
    # no framework used to fall through to a Node scaffold and run npm even
    # when the request said "build this as a Django project" - the request text
    # never reached stack detection at all.
    stack_text = f"{lowered}\n{(request_text or '').lower()}" if request_text else lowered
    game_type = _detect_game_type(lowered)
    project_kind = _detect_kind(lowered, game_type)
    # Prohibitions are resolved BEFORE any stack detection, so a forbidden name
    # can never become the detected stack.
    prohibitions = detect_prohibitions(stack_text)
    stack_hint = _detect_stack(stack_text, prohibitions)
    summary_lines = _section_lines(parsed, "product summary", exact=True)
    if not summary_lines:
        summary_lines = _section_lines(parsed, "overview", exact=True)
    summary = " ".join(summary_lines[:2])

    mechanics = _section_lines(parsed, "mechanics", "gameplay", "functional requirements", "core workflows")
    controls = _section_lines(parsed, "controls", "input") or _scan_controls(lowered)
    screens = _section_lines(parsed, "screens", "frontend pages", "pages", "browser ui requirements")
    acceptance = _section_lines(parsed, "acceptance criteria", "acceptance")
    constraints = _section_lines(
        parsed, "constraints", "non-functional requirements", "performance requirements"
    )
    feature_lines = _section_lines(
        parsed,
        "features",
        "functional requirements",
        "core workflows",
        "core features and user flows",
        "admin flows",
        "teacher flows",
        "student flows",
        "browser ui requirements",
        "script requirements",
        "demo data",
    )
    if not feature_lines:
        feature_lines = [line for line in summary_lines if line.startswith(("-", "*", "\u2022"))]

    roles = _role_names(parsed)
    authentication = _section_lines(
        parsed, "authentication", "user registration", "user login", "logout", "authentication and authorization",
        "protected routes", "authentication security", "password security",
    )
    authorization = _section_lines(
        parsed,
        "ownership validation",
        "unauthorized requests",
        "authorization security",
        "permissions rules",
        "permissions and authorization",
    )
    journeys = _section_lines(
        parsed,
        "main user journey",
        "new user journey",
        "returning user journey",
        "admin flows",
        "teacher flows",
        "student flows",
    )
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
        # A section named exactly this fed only `constraints`, so a PRD with a
        # "Non-Functional Requirements" heading reported none of them.
        "non functional requirements",
    )
    tests = _section_lines(parsed, "testing requirements", "test requirements", "authentication tests",
                           "authorization tests", "task tests", "category tests", "profile tests",
                           "database tests")
    if not tests:
        tests = [line for line in acceptance if re.search(r"\btests?\b|\bcoverage\b", line, re.I)]
    out_of_scope = _section_lines(
        parsed,
        "out of scope for initial release",
        "out of scope",
        "non goals",
    )
    parsed_entities = extract_entities(parsed)
    # `extra_entities` carries entities the document named but never defined,
    # recovered separately. Parsed definitions always win on a name clash.
    if extra_entities:
        known = {entity.name.lower() for entity in parsed_entities}
        parsed_entities = [
            *parsed_entities,
            *[item for item in extra_entities if item.name.lower() not in known],
        ]
    entities = [asdict(entity) for entity in parsed_entities]
    endpoints = _extract_api_endpoints(parsed)

    architecture: list[str] = []
    # Architecture describes SHAPE, so no technology names here. "sqlite" used to be
    # in this list, which meant merely mentioning SQLite - including to forbid it -
    # made `architecture` non-empty, and a non-empty architecture was a precondition
    # for the Django-default assumption path.
    for phrase in ("full-stack", "full stack", "backend api", "responsive user interface"):
        if phrase in lowered:
            architecture.append(phrase)
    # A forbidden technology must never land in a field named `required_stack`.
    _blocked = {_normalize_technology(name) for name in prohibitions}
    required_stack = [
        name
        for name, pattern in _STACK_PATTERNS
        if re.search(pattern, stack_text) and _normalize_technology(name) not in _blocked
    ]
    if re.search(r"\bsqlite\b", stack_text) and "sqlite" not in _blocked:
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
        acceptance_criteria=_dedupe(_clean_acceptance_items(acceptance))[:60],
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
        prohibitions=prohibitions,
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
    value = _HEADING_NUMBER_RE.sub("", heading.strip()).rstrip(":").lower()
    value = value.replace("&", " and ").replace("-", " ")
    return " ".join(re.sub(r"[^a-z0-9()]+", " ", value).split())


def _role_names(parsed: ParsedPRD) -> list[str]:
    roles: list[str] = []
    for heading, lines in parsed.sections.items():
        normalized = _normalize_heading(heading)
        if normalized in {"regular user", "administrator", "admin", "user"}:
            roles.append(normalized.title())
        if normalized not in {"users and roles", "user roles", "roles"}:
            continue
        for line in lines:
            candidate = line.strip().lstrip("-*+• ").split(":", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z /-]{1,30}", candidate) and len(candidate.split()) <= 3:
                roles.append(candidate.title())
    return _dedupe(roles)


def _extract_api_endpoints(parsed: ParsedPRD) -> list[dict[str, Any]]:
    endpoints: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for heading, lines in parsed.sections.items():
        context = " ".join([heading, *lines]).lower()
        for index, line in enumerate(lines):
            candidate = line
            if re.fullmatch(r"GET|POST|PUT|PATCH|DELETE", line.strip(), re.IGNORECASE):
                following = lines[index + 1].strip() if index + 1 < len(lines) else ""
                if following.startswith("/"):
                    candidate = f"{line.strip()} {following}"
            match = _ENDPOINT_RE.search(candidate)
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
    if _any(lowered, ("python package", "npm package", "software library", "sdk", "reusable library")):
        return "library"
    if _any(lowered, ("data pipeline", "etl", "data analysis", "notebook", "dataset")):
        return "data_project"
    if _any(lowered, ("frontend only", "front-end only", "static site", "static website")):
        return "frontend"
    if _any(lowered, ("microservice", "worker", "daemon", "background service", "cron")):
        return "service"
    return "unknown"


def _normalize_technology(name: str) -> str:
    return _TECHNOLOGY_ALIASES.get(name, name)


def detect_prohibitions(text: str) -> list[str]:
    """Technologies *text* explicitly forbids.

    Clause-scoped on purpose. A document-wide scan cannot tell
    "use PostgreSQL, never SQLite" (forbid SQLite) from "never use PostgreSQL,
    use SQLite" (forbid PostgreSQL) - it would forbid both and leave nothing to
    build with.

    Recall is favoured over precision here: a missed prohibition silently builds
    the wrong stack, while a false positive removes one candidate from a list that
    still has to be resolved and reported.
    """
    lowered = (text or "").lower()
    found: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(lowered):
        marker_at = min(
            (clause.find(marker) for marker in _PROHIBITION_MARKERS if marker in clause),
            default=-1,
        )
        if marker_at < 0:
            continue
        for technology in _PROHIBITABLE_TECHNOLOGIES:
            # `\b` alone would miss "django/python" and "Node.js"; the escape keeps
            # dotted names like next.js literal.
            match = re.search(rf"(?<![\w.]){re.escape(technology)}(?![\w])", clause)
            # Position matters, and getting this wrong is worse than missing a
            # prohibition: "Use PostgreSQL, never SQLite" is one clause (a comma is
            # not a boundary), so a clause-wide scan forbids PostgreSQL too - and
            # strips the very stack that was asked for. Only what follows the
            # negation is forbidden.
            if match is not None and match.start() > marker_at:
                normalized = _normalize_technology(technology)
                if normalized not in found:
                    found.append(normalized)
    return sorted(found)


def _detect_stack(lowered: str, prohibited: Iterable[str] = ()) -> str:
    """First matching stack name that is not forbidden.

    Skipping forbidden names is what stops the inversion: `("django", ...)` is
    entry 0 in _STACK_PATTERNS, so a PRD saying "NEVER Django" used to match it
    first and set stack_hint = "django".
    """
    blocked = {_normalize_technology(name) for name in prohibited}
    for name, pattern in _STACK_PATTERNS:
        if _normalize_technology(name) in blocked:
            continue
        if re.search(pattern, lowered):
            return name
    return ""


def _scan_controls(lowered: str) -> list[str]:
    return [token for token in _CONTROL_TOKENS if token in lowered]


def _clean_items(items: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        value = item.strip().lstrip("-*+\u2022 ").strip()
        if (
            value
            and value.lower() not in {"test:", "fields:", "constraints:", "indexes:"}
            and not _is_structural_noise_line(value)
        ):
            cleaned.append(value)
    return cleaned


def _clean_acceptance_items(items: list[str]) -> list[str]:
    joined: list[str] = []
    for raw in _clean_items(items):
        if (
            joined
            and not re.search(r"[.!?;:`]$", joined[-1])
            and raw[:1].islower()
        ):
            joined[-1] = f"{joined[-1]} {raw}"
        else:
            joined.append(raw)
    return [item for item in joined if not _looks_like_ocr_garbage(item)]


def _looks_like_ocr_garbage(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) < 8:
        return False
    short = sum(len(word) <= 2 for word in words)
    return short / len(words) >= 0.5


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
