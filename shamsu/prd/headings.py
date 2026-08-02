"""Resolve arbitrary PRD section headings onto the extractor's vocabulary.

``extract_contract`` finds requirements by matching section headings against a
fixed list of names - ``"users and roles"``, ``"data model"``, ``"features"``.
Real PRDs name their sections freely. A document that says "User Types and
Roles" instead of "Users & Roles", and "Member Management" instead of
"Features", matches nothing, and a section SHAMSU cannot match is a section
SHAMSU cannot see. When every heading misses, extraction yields zero entities
and zero features, and the compiled plan contains nothing to build.

Resolution runs in two layers:

* :func:`resolve_headings` - a deterministic token match. Free, synchronous,
  and enough for headings that merely reword a known section ("User Types and
  Roles" -> ``users and roles``).
* :func:`resolve_headings_with_model` - a model pass over whatever the token
  match could not place. This is what recognises that "Circulation Management"
  is a feature section, which no amount of string matching can know.

Both layers only ever produce *aliases*; :func:`apply_heading_aliases` rewrites
the parsed sections so the existing deterministic extractors run unchanged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from shamsu.types import ParsedPRD

_HEADING_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+")

# Words that carry no matching signal. Dropping them lets "User Types and
# Roles" and "Users & Roles" compare on {user, type, role} vs {user, role}.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "or",
        "its", "their", "all", "any", "by", "at", "from", "into", "per",
    }
)

# Every heading name the extractors match on. Keep in sync with the terms
# passed to ``contract._section_lines`` and ``extractor._is_entity_container_heading``.
CANONICAL_TERMS: tuple[str, ...] = (
    "product summary",
    "overview",
    "target users",
    "users and roles",
    "user roles",
    "roles",
    "mechanics",
    "gameplay",
    "controls",
    "input",
    "features",
    "functional requirements",
    "core workflows",
    "core features and user flows",
    "admin flows",
    "teacher flows",
    "student flows",
    "screens",
    "frontend pages",
    "pages",
    "browser ui requirements",
    "script requirements",
    "demo data",
    "data model",
    "domain model",
    "entities",
    "database schema",
    "database requirements",
    "database characteristics",
    "foreign keys",
    "transactions",
    "backups",
    "authentication",
    "authentication and authorization",
    "user registration",
    "user login",
    "logout",
    "protected routes",
    "authentication security",
    "password security",
    "permissions and authorization",
    "permissions rules",
    "ownership validation",
    "unauthorized requests",
    "authorization security",
    "main user journey",
    "new user journey",
    "returning user journey",
    "data validation rules",
    "validation",
    "error handling",
    "validation errors",
    "common error types",
    "security requirements",
    "input security",
    "request protection",
    "secrets",
    "constraints",
    "non functional requirements",
    "performance requirements",
    "accessibility requirements",
    "responsive design requirements",
    "audit and logging requirements",
    "date and time handling",
    "testing requirements",
    "test requirements",
    "acceptance criteria",
    "acceptance",
    "out of scope",
    "out of scope for initial release",
    "non goals",
)

# The coarse roles offered to the model, and the canonical heading each one
# rewrites to. Deliberately smaller than CANONICAL_TERMS - a 7B picks reliably
# from twenty obvious buckets and badly from seventy near-synonyms.
MODEL_ROLES: dict[str, str] = {
    "features": "features",
    "entities": "data model",
    "roles": "users and roles",
    "screens": "screens",
    "workflows": "core workflows",
    "journeys": "main user journey",
    "authentication": "authentication",
    "authorization": "permissions and authorization",
    "persistence": "database requirements",
    "validation": "data validation rules",
    "errors": "error handling",
    "security": "security requirements",
    "nonfunctional": "non functional requirements",
    "constraints": "constraints",
    "tests": "testing requirements",
    "acceptance": "acceptance criteria",
    "out_of_scope": "out of scope",
    "summary": "product summary",
    "users": "target users",
    "none": "",
}

_ROLE_GUIDE = """\
features        - a capability the product must provide (e.g. "Member Management")
entities        - data records/tables the system stores
roles           - the kinds of user and what each may do
screens         - pages or views in the interface
workflows       - ordered steps through a task
journeys        - end-to-end narrative of a user's path
authentication  - login, logout, sessions, passwords
authorization   - permissions, access control, who may do what
persistence     - database/storage requirements
validation      - rules input must satisfy
errors          - error handling and failure states
security        - security requirements
nonfunctional   - performance, accessibility, reliability, scale
constraints     - limits and technical restrictions
tests           - testing requirements
acceptance      - acceptance criteria / definition of done
out_of_scope    - explicitly excluded from this release
summary         - product summary or overview
users           - the target audience
none            - front matter, metrics, changelog: nothing to build from"""

HEADING_ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "role": {"type": "string", "enum": sorted(MODEL_ROLES)},
                },
                "required": ["heading", "role"],
            },
        }
    },
    "required": ["mappings"],
}

# A canonical term must explain at least this share of the heading's content
# words. Without a floor, the single word "features" would claim
# "Detailed Notes On Feature Flag Rollout Sequencing".
_MIN_COVERAGE = 0.34

# Terms too generic to claim a heading on one shared word. "Financial Controls"
# is not game controls; "Backdated Transactions" is not a database transaction
# requirement. These still match exactly, just never fuzzily.
_EXACT_ONLY_TERMS = frozenset(
    {
        "controls",
        "input",
        "mechanics",
        "gameplay",
        "transactions",
        "backups",
        "secrets",
        "pages",
    }
)

# Headings are sent to the model in batches; a 7B degrades on long lists.
_MODEL_BATCH_SIZE = 12
_MAX_MODEL_HEADINGS = 60


@dataclass
class HeadingResolution:
    """Aliases from a PRD's own heading text to canonical extractor terms."""

    aliases: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    already_canonical: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return len(self.aliases) + len(self.already_canonical)


def normalize_heading(heading: str) -> str:
    """Lower-case a heading and strip numbering/punctuation.

    Mirrors ``contract._normalize_heading`` so an alias produced here matches
    the same way the extractor compares.
    """
    value = _HEADING_NUMBER_RE.sub("", heading.strip()).rstrip(":").lower()
    value = value.replace("&", " and ").replace("-", " ")
    return " ".join(re.sub(r"[^a-z0-9()]+", " ", value).split())


def _singular(token: str) -> str:
    if len(token) > 3 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _heading_depth(heading: str) -> int:
    """Nesting depth implied by a heading's own numbering ("4.11 ..." -> 2)."""
    match = _HEADING_NUMBER_RE.match(heading.strip())
    if not match:
        return 1
    return match.group(0).strip().rstrip(".").count(".") + 1


def _content_tokens(normalized: str) -> frozenset[str]:
    return frozenset(
        _singular(token)
        for token in normalized.split()
        if token and token not in _STOPWORDS
    )


def _matches_today(normalized: str) -> bool:
    """True when the extractor already finds this heading unaided."""
    return any(
        normalized == term or normalized.startswith(f"{term} ")
        for term in CANONICAL_TERMS
    )


def resolve_headings(headings: Iterable[str]) -> HeadingResolution:
    """Map headings onto canonical terms by token containment.

    A canonical term claims a heading when all of its content words appear in
    the heading. The most specific surviving term wins; a tie between terms
    that mean *different* things is left unresolved rather than guessed, so the
    model layer can decide. Synonyms that reduce to the same words ("users and
    roles" / "user roles") are not a real tie.
    """
    index = {
        term: _content_tokens(term)
        for term in CANONICAL_TERMS
        if term not in _EXACT_ONLY_TERMS
    }
    resolution = HeadingResolution()

    for heading in headings:
        normalized = normalize_heading(heading)
        if not normalized:
            continue
        if _matches_today(normalized):
            resolution.already_canonical.append(heading)
            continue

        tokens = _content_tokens(normalized)
        if not tokens:
            resolution.unresolved.append(heading)
            continue

        candidates: list[tuple[int, float, str]] = []
        for term, term_tokens in index.items():
            if not term_tokens or not term_tokens <= tokens:
                continue
            coverage = len(term_tokens) / len(tokens)
            if coverage < _MIN_COVERAGE:
                continue
            candidates.append((len(term_tokens), coverage, term))

        if not candidates:
            resolution.unresolved.append(heading)
            continue

        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        best = candidates[0]
        tied = [item for item in candidates if (item[0], item[1]) == (best[0], best[1])]
        # Distinct wording, identical words - "users and roles" vs "user roles"
        # - is one concept, not a contested match.
        if len({index[term] for _, _, term in tied}) > 1:
            resolution.unresolved.append(heading)
            continue
        resolution.aliases[heading] = best[2]

    return resolution


def apply_heading_aliases(
    parsed: ParsedPRD, aliases: Mapping[str, str]
) -> ParsedPRD:
    """Return *parsed* with aliased headings rewritten to canonical names.

    Sections that collapse onto the same canonical name are concatenated, which
    is what the extractor would have done had they been named that way.
    ``source_refs`` is remapped alongside so page provenance survives.
    """
    if not aliases:
        return parsed

    sections: dict[str, list[str]] = {}
    for heading, lines in parsed.sections.items():
        target = aliases.get(heading) or heading
        sections.setdefault(target, []).extend(lines)

    source_refs: dict[str, list] = {}
    for heading, refs in parsed.source_refs.items():
        target = aliases.get(heading) or heading
        source_refs.setdefault(target, []).extend(refs)

    return replace(parsed, sections=sections, source_refs=source_refs)


def _section_preview(lines: list[str], limit: int = 3) -> str:
    preview: list[str] = []
    for line in lines:
        cleaned = line.strip().lstrip("-*+• ").strip()
        if cleaned:
            preview.append(cleaned[:110])
        if len(preview) >= limit:
            break
    return " | ".join(preview)


def _model_prompt(items: list[tuple[str, list[str]]]) -> str:
    listing: list[str] = []
    for heading, lines in items:
        preview = _section_preview(lines)
        entry = f'- "{heading}"'
        if preview:
            entry += f"\n  contains: {preview}"
        listing.append(entry)
    return (
        "Classify each PRD section heading into exactly one role.\n\n"
        "Roles:\n"
        f"{_ROLE_GUIDE}\n\n"
        "Sections:\n"
        + "\n".join(listing)
        + "\n\nReturn JSON only, one mapping per heading, echoing each heading "
        "exactly as written above."
    )


async def resolve_headings_with_model(
    parsed: ParsedPRD,
    unresolved: Iterable[str],
    manager=None,
) -> dict[str, str]:
    """Ask the thinking model which canonical role each unplaced heading fills.

    Returns aliases for the headings the model could place. Any failure - no
    Ollama, bad JSON, an unknown role - yields no alias for that heading rather
    than an error, leaving behaviour exactly as it was before this layer.
    """
    from shamsu.llm.manager import LLMManager
    from shamsu.runtime.models import model_for_role

    scored = [
        (_heading_depth(heading), order, heading, parsed.sections.get(heading) or [])
        for order, heading in enumerate(unresolved)
        if (parsed.sections.get(heading) or [])
    ]
    # A long PRD has more headings than is worth a model call, and its
    # subsections ("4.11 Library Member") carry far less plan signal than its
    # top-level sections ("12. Circulation Management"). Spend the budget on
    # the shallowest headings first.
    scored.sort(key=lambda item: (item[0], item[1]))
    candidates = [(heading, lines) for _, _, heading, lines in scored][
        :_MAX_MODEL_HEADINGS
    ]
    if not candidates:
        return {}

    manager = manager or LLMManager()
    # Unmapped role -> the tier's thinking anchor. Placing a section is a
    # judgement about meaning, which is what the reasoning model is for.
    model = model_for_role("prd_headings")
    aliases: dict[str, str] = {}

    for start in range(0, len(candidates), _MODEL_BATCH_SIZE):
        batch = candidates[start : start + _MODEL_BATCH_SIZE]
        try:
            raw = await manager._generate(
                model,
                "You classify PRD section headings. Output JSON only.",
                _model_prompt(batch),
                temperature=0.0,
                json_schema=HEADING_ROLE_SCHEMA,
                _role="prd_headings",
            )
        except Exception:
            continue
        aliases.update(_parse_model_mappings(raw, [heading for heading, _ in batch]))

    return _demote_implausible_entities(parsed, aliases)


def _demote_implausible_entities(
    parsed: ParsedPRD, aliases: dict[str, str]
) -> dict[str, str]:
    """Re-label "entities" guesses whose section holds no field definitions.

    A section the model reads as a data model but that contains prose bullets
    ("Main responsibilities") would otherwise be parsed into Django fields.
    Such a section is almost always describing a capability, so it becomes a
    feature rather than being discarded.
    """
    from shamsu.prd.extractor import _looks_like_field_section

    entity_target = MODEL_ROLES["entities"]
    adjusted: dict[str, str] = {}
    for heading, target in aliases.items():
        lines = parsed.sections.get(heading) or []
        if target == entity_target and not _looks_like_field_section(lines):
            adjusted[heading] = MODEL_ROLES["features"]
            continue
        adjusted[heading] = target
    return adjusted


def _parse_model_mappings(raw: str, batch: list[str]) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    lookup = {normalize_heading(heading): heading for heading in batch}
    aliases: dict[str, str] = {}
    for item in data.get("mappings") or []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "")
        role = str(item.get("role") or "").strip().lower()
        target = MODEL_ROLES.get(role)
        if not target:
            continue
        original = lookup.get(normalize_heading(heading))
        if original:
            aliases[original] = target
    return aliases


def resolve_parsed_headings(parsed: ParsedPRD) -> tuple[ParsedPRD, HeadingResolution]:
    """Deterministic-only resolution, applied. Safe to call anywhere."""
    resolution = resolve_headings(parsed.sections)
    return apply_heading_aliases(parsed, resolution.aliases), resolution
