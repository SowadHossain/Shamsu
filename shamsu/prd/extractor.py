"""
Rule-based extraction from parsed PRDs.

This module intentionally starts narrow: entities and fields from Markdown
sections. The LLM can resolve ambiguity later, but the cheap parser should
capture the common PRD shape without a model call.
"""
from __future__ import annotations

import re

from shamsu.types import EntityFieldSpec, EntitySpec, ParsedPRD

ENTITY_LINE_RE = re.compile(r"^(?:[-*+]\s*)?(?:\*\*)?([A-Za-z][\w ]+)(?:\*\*)?\s*:\s*(.+)$")
FIELD_RE = re.compile(r"^(?P<name>[A-Za-z_][\w ]*)\s*(?:\((?P<type>[^)]*)\))?$")


TYPE_MAP = {
    "bool": ("BooleanField", {}),
    "boolean": ("BooleanField", {}),
    "date": ("DateField", {}),
    "datetime": ("DateTimeField", {}),
    "decimal": ("DecimalField", {"max_digits": 10, "decimal_places": 2}),
    "email": ("EmailField", {}),
    "int": ("IntegerField", {}),
    "integer": ("IntegerField", {}),
    "long text": ("TextField", {}),
    "number": ("IntegerField", {}),
    "str": ("CharField", {"max_length": 200}),
    "string": ("CharField", {"max_length": 200}),
    "text": ("CharField", {"max_length": 200}),
}


def extract_entities(parsed: ParsedPRD) -> list[EntitySpec]:
    entities: list[EntitySpec] = []
    for heading, lines in parsed.sections.items():
        if "entit" not in heading.lower() and "data model" not in heading.lower():
            continue
        for line in lines:
            entity = _parse_entity_line(line)
            if entity is not None:
                entities.append(entity)
    return entities


def _parse_entity_line(line: str) -> EntitySpec | None:
    match = ENTITY_LINE_RE.match(line.strip())
    if not match:
        return None

    entity_name = _to_pascal_case(_strip_markdown(match.group(1)))
    fields: list[EntityFieldSpec] = []
    relationships: list[str] = []
    for part in _split_fields(match.group(2)):
        field = _parse_field(part)
        if field is None:
            continue
        fields.append(field)
        if field.django_type in {"ForeignKey", "ManyToManyField"}:
            relation = "many_to_many" if field.django_type == "ManyToManyField" else "belongs_to"
            relationships.append(f"{relation}:{field.kwargs.get('to', '')}")

    return EntitySpec(name=entity_name, fields=fields, relationships=relationships)


def _parse_field(raw_field: str) -> EntityFieldSpec | None:
    cleaned = _strip_markdown(raw_field).strip()
    match = FIELD_RE.match(cleaned)
    if not match:
        return None

    name = _to_snake_case(match.group("name"))
    raw_type = (match.group("type") or "text").strip().lower()
    nullable = "optional" in raw_type or "null" in raw_type
    raw_type = raw_type.replace("optional", "").replace("required", "").strip(" ,")

    relation = _parse_relation(raw_type)
    if relation is not None:
        django_type, target = relation
        return EntityFieldSpec(
            name=name,
            django_type=django_type,
            kwargs={"to": target, "on_delete": "CASCADE"} if django_type == "ForeignKey" else {"to": target},
        )

    if raw_type.startswith("choices"):
        kwargs: dict[str, object] = {"max_length": 50}
        choices = raw_type.split(":", 1)[1].strip() if ":" in raw_type else ""
        if choices:
            kwargs["choices"] = [choice.strip() for choice in choices.split("/") if choice.strip()]
        return EntityFieldSpec(name=name, django_type="CharField", kwargs=kwargs)

    django_type, kwargs = TYPE_MAP.get(raw_type, ("CharField", {"max_length": 200}))
    if nullable:
        kwargs = {**kwargs, "null": True, "blank": True}
    return EntityFieldSpec(name=name, django_type=django_type, kwargs=dict(kwargs))


def _parse_relation(raw_type: str) -> tuple[str, str] | None:
    patterns = [
        (r"^(?:fk|foreignkey|foreign key)\s+(?:to\s+)?(?P<target>[A-Za-z]\w*)$", "ForeignKey"),
        (r"^belongs\s+to\s+(?P<target>[A-Za-z]\w*)$", "ForeignKey"),
        (r"^(?:m2m|manytomany|many to many)\s+(?:to\s+)?(?P<target>[A-Za-z]\w*)$", "ManyToManyField"),
    ]
    for pattern, django_type in patterns:
        match = re.match(pattern, raw_type)
        if match:
            return django_type, _to_pascal_case(match.group("target"))
    return None


def _split_fields(fields_text: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    depth = 0
    for char in fields_text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if char == "," and depth == 0:
            value = "".join(current).strip()
            if value:
                fields.append(value)
            current = []
            continue
        current.append(char)
    value = "".join(current).strip()
    if value:
        fields.append(value)
    return fields


def _strip_markdown(text: str) -> str:
    return text.replace("**", "").replace("`", "").strip()


def _to_snake_case(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.strip("_").lower()


def _to_pascal_case(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", text) if part)
