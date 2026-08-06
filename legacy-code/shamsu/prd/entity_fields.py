"""Infer fields for entities a PRD names but does not define.

Requirement documents routinely list their data model as bare nouns — "the
system should contain the following major entities: Organization, Branch, Loan,
Fine, …" — with the fields left implied. Both entity parsers require field
definitions, so such a list produces **zero** entities. Zero entities means no
model layer, which means the planner has no backend project to generate and
degrades to a single static page. A 45-entity library specification became one
`index.html` for exactly this reason.

Naming the fields of a `Loan` in a library system is a judgement about meaning,
not something a regex can recover, so it is asked of the reasoning model —
bounded, batched, and always optional: any failure leaves extraction exactly as
it was.
"""
from __future__ import annotations

import json
import os
import re

from shamsu.types import EntityFieldSpec, EntitySpec, ParsedPRD

# Django field types the generator understands. Anything else is coerced to
# CharField rather than trusted through to a model file.
_ALLOWED_TYPES = {
    "CharField",
    "TextField",
    "IntegerField",
    "PositiveIntegerField",
    "DecimalField",
    "BooleanField",
    "DateField",
    "DateTimeField",
    "EmailField",
    "URLField",
    "ForeignKey",
}
_DEFAULT_TYPE = "CharField"

_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9 /-]{1,34}$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Bounded on purpose: every entity becomes milestones, and a 45-model build is
# not a first run. Document order is roughly core-first in practice.
_MAX_ENTITIES = 12
_BATCH = 4

ENTITY_FIELDS_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "type": {"type": "string", "enum": sorted(_ALLOWED_TYPES)},
                                "target": {"type": "string"},
                            },
                            "required": ["name", "type"],
                        },
                    },
                },
                "required": ["name", "fields"],
            },
        }
    },
    "required": ["entities"],
}


def max_inferred_entities() -> int:
    raw = os.environ.get("SHAMSU_MAX_INFERRED_ENTITIES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _MAX_ENTITIES
    return max(1, value)


def _is_entity_container(heading: str) -> bool:
    lowered = heading.lower()
    return any(
        token in lowered
        for token in ("data model", "domain model", "entit", "schema")
    )


def bare_entity_names(parsed: ParsedPRD) -> list[str]:
    """Entity names listed without any field definition.

    Only bullets that read as a plain noun phrase count; a line carrying a
    colon, a parenthesis or a type keyword is a real definition and belongs to
    the deterministic parsers.
    """
    names: list[str] = []
    for heading, lines in (parsed.sections or {}).items():
        if not _is_entity_container(heading):
            continue
        for line in lines:
            cleaned = line.strip().lstrip("-*+• ").strip().rstrip(".")
            if not cleaned or any(token in cleaned for token in (":", "(", "=", "|")):
                continue
            if _NAME_RE.fullmatch(cleaned) and cleaned not in names:
                names.append(cleaned)
    return names


def to_class_name(value: str) -> str:
    parts = re.split(r"[\s/_-]+", value.strip())
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _coerce_field(raw: object, known: set[str]) -> EntityFieldSpec | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip().lower().replace(" ", "_")
    if not _FIELD_NAME_RE.fullmatch(name) or name == "id":
        return None
    django_type = str(raw.get("type") or "").strip()
    if django_type not in _ALLOWED_TYPES:
        django_type = _DEFAULT_TYPE
    kwargs: dict[str, object] = {}
    if django_type == "ForeignKey":
        target = to_class_name(str(raw.get("target") or ""))
        # A relation to an entity that will not exist is the dangling-FK bug
        # that failed whole milestones; demote instead of emitting it.
        if not target or target not in known:
            django_type = _DEFAULT_TYPE
        else:
            kwargs["to"] = target
            kwargs["on_delete"] = "CASCADE"
    if django_type == "CharField":
        kwargs.setdefault("max_length", 200)
    if django_type == "DecimalField":
        kwargs.setdefault("max_digits", 10)
        kwargs.setdefault("decimal_places", 2)
    return EntityFieldSpec(name=name, django_type=django_type, kwargs=kwargs)


def parse_entity_response(raw: str, known: set[str]) -> list[EntitySpec]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    entities: list[EntitySpec] = []
    for item in data.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = to_class_name(str(item.get("name") or ""))
        if not name or name not in known:
            continue
        fields: list[EntityFieldSpec] = []
        seen: set[str] = set()
        for raw_field in item.get("fields") or []:
            field_spec = _coerce_field(raw_field, known)
            if field_spec and field_spec.name not in seen:
                seen.add(field_spec.name)
                fields.append(field_spec)
        if fields:
            entities.append(EntitySpec(name=name, fields=fields, inferred=True))
    return entities


def _prompt(names: list[str], title: str, context: str) -> str:
    return (
        f"Product: {title}\n\n"
        f"{context}\n\n"
        "For each entity below, list the database fields it needs in this "
        "product. Use snake_case field names. Never include an id field. Use "
        "ForeignKey only for a relation to another entity in this list, and "
        'give its class name in "target".\n\n'
        "Entities:\n" + "\n".join(f"- {name}" for name in names) + "\n\n"
        "Return JSON only."
    )


async def infer_entity_fields(
    parsed: ParsedPRD,
    names: list[str],
    manager=None,
) -> list[EntitySpec]:
    """Ask the reasoning model for the fields of each named entity."""
    from shamsu.llm.manager import LLMManager
    from shamsu.runtime.models import model_for_role

    selected = names[: max_inferred_entities()]
    if not selected:
        return []
    known = {to_class_name(name) for name in selected}
    manager = manager or LLMManager()
    model = model_for_role("prd_entities")
    context = (parsed.raw_text or "")[:2500]

    entities: list[EntitySpec] = []
    for start in range(0, len(selected), _BATCH):
        batch = selected[start : start + _BATCH]
        try:
            raw = await manager._generate(
                model,
                "You design database schemas. Output JSON only.",
                _prompt(batch, parsed.title, context),
                temperature=0.0,
                json_schema=ENTITY_FIELDS_SCHEMA,
                _role="prd_entities",
            )
        except Exception:
            continue
        entities.extend(parse_entity_response(raw, known))

    deduped: dict[str, EntitySpec] = {}
    for entity in entities:
        deduped.setdefault(entity.name, entity)
    return list(deduped.values())
