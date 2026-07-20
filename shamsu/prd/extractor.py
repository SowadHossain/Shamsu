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
FIELD_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w ]*)\s*(?:\((?P<type>[^)]*)\)|:\s*(?P<colon_type>.+))?$"
)


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
    "markdown": ("TextField", {}),
    "number": ("IntegerField", {}),
    "str": ("CharField", {"max_length": 200}),
    "string": ("CharField", {"max_length": 200}),
    "text": ("CharField", {"max_length": 200}),
    "url": ("URLField", {}),
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
    table_entities = _extract_table_entities(parsed)
    by_name = {entity.name.lower(): entity for entity in entities}
    for entity in table_entities:
        by_name[entity.name.lower()] = entity
    return list(by_name.values())


def _extract_table_entities(parsed: ParsedPRD) -> list[EntitySpec]:
    table_entity_names: list[str] = []
    for heading, refs in parsed.source_refs.items():
        normalized = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).strip()
        if not normalized.lower().endswith(" table"):
            continue
        name = normalized[:-6].strip()
        if any(ref.get("kind") == "heading" for ref in refs):
            table_entity_names.append(_to_pascal_case(_singularize(name)))

    schema_tables = [
        table
        for table in parsed.tables
        if table.get("rows")
        and "field type requirements" in " ".join(
            str(cell or "") for cell in table["rows"][0]
        ).lower()
    ]

    entities: list[EntitySpec] = []
    for entity_name, table in zip(table_entity_names, schema_tables, strict=False):
        fields: list[EntityFieldSpec] = []
        relationships: list[str] = []
        for row in table.get("rows", []):
            text = " ".join(str(cell or "").strip() for cell in row).strip()
            field = _parse_schema_row(text)
            if field is None:
                continue
            fields.append(field)
            if field.django_type == "ForeignKey":
                relationships.append(f"belongs_to:{field.kwargs.get('to', '')}")
        _apply_schema_semantics(entity_name, fields, parsed)
        if fields:
            entities.append(EntitySpec(entity_name, fields, relationships))
    return entities


def _apply_schema_semantics(
    entity_name: str,
    fields: list[EntityFieldSpec],
    parsed: ParsedPRD,
) -> None:
    if entity_name != "Task":
        return
    choices_by_field = {
        "status": _choices_from_section(parsed, "task status options"),
        "priority": _choices_from_section(parsed, "priority options"),
    }
    for field in fields:
        choices = choices_by_field.get(field.name, [])
        if choices:
            field.kwargs["choices"] = choices
            field.kwargs["max_length"] = max(len(item) for item in choices)


def _choices_from_section(parsed: ParsedPRD, section_name: str) -> list[str]:
    choices: list[str] = []
    for heading, lines in parsed.sections.items():
        normalized = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).lower()
        if normalized != section_name:
            continue
        for line in lines:
            value = line.strip().lstrip("-*+• ").strip()
            if re.fullmatch(r"[a-z][a-z0-9_ -]{0,30}", value, re.IGNORECASE):
                choices.append(value.lower().replace(" ", "_"))
    return choices


def _parse_schema_row(row: str) -> EntityFieldSpec | None:
    match = re.match(
        r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)\s+"
        r"(?P<type>Boolean/Integer|DateTime|Integer|Text)\s+"
        r"(?P<requirements>.+)$",
        row,
        re.IGNORECASE,
    )
    if not match or match.group("name").lower() == "id":
        return None
    name = match.group("name").lower()
    raw_type = match.group("type").lower()
    requirements = match.group("requirements").lower()
    optional = "optional" in requirements
    kwargs: dict[str, object] = {}

    relation = re.search(r"foreign key to\s+([a-z][a-z0-9_]*)", requirements)
    if relation:
        target = _to_pascal_case(_singularize(relation.group(1)))
        kwargs = {
            "to": target,
            "on_delete": "SET_NULL" if optional else "CASCADE",
        }
        if optional:
            kwargs.update({"null": True, "blank": True})
        return EntityFieldSpec(name=name.removesuffix("_id"), django_type="ForeignKey", kwargs=kwargs)

    if raw_type == "datetime":
        if name == "created_at":
            kwargs["auto_now_add"] = True
        elif name == "updated_at":
            kwargs["auto_now"] = True
        elif optional:
            kwargs.update({"null": True, "blank": True})
        return EntityFieldSpec(name=name, django_type="DateTimeField", kwargs=kwargs)

    if raw_type == "boolean/integer":
        kwargs["default"] = "default true" in requirements
        return EntityFieldSpec(name=name, django_type="BooleanField", kwargs=kwargs)

    if raw_type == "integer":
        django_type = "IntegerField"
    elif name == "email":
        django_type = "EmailField"
    elif name in {"description"}:
        django_type = "TextField"
    else:
        django_type = "CharField"
        length = re.search(r"maximum\s+([\d,]+)\s+characters", requirements)
        kwargs["max_length"] = int(length.group(1).replace(",", "")) if length else 200

    if optional:
        kwargs.update({"null": True, "blank": True})
    if "unique" in requirements:
        kwargs["unique"] = True
    default = re.search(r"default\s+([a-z0-9_]+)", requirements)
    if default:
        kwargs["default"] = default.group(1)
    return EntityFieldSpec(name=name, django_type=django_type, kwargs=kwargs)


def _singularize(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith("ies"):
        return f"{name[:-3]}y"
    if lowered.endswith("s"):
        return name[:-1]
    return name


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
    raw_type = (match.group("type") or match.group("colon_type") or "text").strip().lower()
    nullable = any(marker in raw_type for marker in ("optional", "nullable", "null", "blank"))
    raw_type = _strip_constraints(raw_type)

    relation = _parse_relation(raw_type)
    if relation is not None:
        django_type, target = relation
        kwargs = {"to": target}
        if django_type == "ForeignKey":
            kwargs["on_delete"] = "CASCADE"
        if nullable:
            kwargs.update({"null": True, "blank": True})
        return EntityFieldSpec(
            name=name,
            django_type=django_type,
            kwargs=kwargs,
        )

    if raw_type.startswith(("choices", "choice", "enum")):
        kwargs: dict[str, object] = {"max_length": _parse_max_length(raw_type) or 50}
        choices = _parse_choices(raw_type)
        if choices:
            kwargs["choices"] = choices
        if nullable:
            kwargs.update({"null": True, "blank": True})
        return EntityFieldSpec(name=name, django_type="CharField", kwargs=kwargs)

    base_type = _base_type(raw_type)
    django_type, kwargs = TYPE_MAP.get(base_type, ("CharField", {"max_length": 200}))
    kwargs = {**kwargs, **_parse_numeric_kwargs(raw_type)}
    max_length = _parse_max_length(raw_type)
    if max_length and django_type in {"CharField", "EmailField", "URLField"}:
        kwargs["max_length"] = max_length
    default = _parse_default(raw_type)
    if default is not None:
        kwargs["default"] = default
    if nullable:
        kwargs = {**kwargs, "null": True, "blank": True}
    return EntityFieldSpec(name=name, django_type=django_type, kwargs=dict(kwargs))


def _parse_relation(raw_type: str) -> tuple[str, str] | None:
    patterns = [
        (r"^(?:fk|foreignkey|foreign key)\s+(?:to\s+)?(?P<target>[A-Za-z]\w*)$", "ForeignKey"),
        (r"^belongs\s+to\s+(?P<target>[A-Za-z]\w*)$", "ForeignKey"),
        (r"^owner\s+(?:user|auth user)$", "ForeignKey"),
        (r"^(?:user|auth user|django user)$", "ForeignKey"),
        (r"^(?:m2m|manytomany|many to many)\s+(?:to\s+)?(?P<target>[A-Za-z]\w*)$", "ManyToManyField"),
    ]
    for pattern, django_type in patterns:
        match = re.match(pattern, raw_type)
        if match:
            target = match.groupdict().get("target") or "User"
            return django_type, _to_pascal_case(target)
    return None


def _strip_constraints(raw_type: str) -> str:
    cleaned = raw_type
    for marker in ("optional", "nullable", "required", "blank", "null"):
        cleaned = re.sub(rf"\b{marker}\b", "", cleaned)
    return cleaned.strip(" ,;")


def _parse_choices(raw_type: str) -> list[str]:
    choices = ""
    if ":" in raw_type:
        choices = raw_type.split(":", 1)[1]
    elif " " in raw_type:
        choices = raw_type.split(" ", 1)[1]
    choices = re.sub(r"\bmax_length\s*=?\s*\d+\b", "", choices)
    return [choice.strip(" '\"") for choice in re.split(r"[/|,]", choices) if choice.strip(" '\"")]


def _parse_max_length(raw_type: str) -> int | None:
    match = re.search(r"max[_ -]?length\s*[=:]?\s*(\d+)", raw_type)
    return int(match.group(1)) if match else None


def _parse_numeric_kwargs(raw_type: str) -> dict[str, int]:
    if "decimal" not in raw_type:
        return {}
    max_digits = re.search(r"max[_ -]?digits\s*[=:]?\s*(\d+)", raw_type)
    decimal_places = re.search(r"decimal[_ -]?places\s*[=:]?\s*(\d+)", raw_type)
    kwargs = {"max_digits": 10, "decimal_places": 2}
    if max_digits:
        kwargs["max_digits"] = int(max_digits.group(1))
    if decimal_places:
        kwargs["decimal_places"] = int(decimal_places.group(1))
    return kwargs


def _base_type(raw_type: str) -> str:
    for known in sorted(TYPE_MAP, key=len, reverse=True):
        if raw_type == known or raw_type.startswith(f"{known} "):
            return known
    return raw_type


def _parse_default(raw_type: str) -> object | None:
    match = re.search(r"default\s*[=:]\s*([^,;)]+)", raw_type)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"")
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.isdigit():
        return int(value)
    return value


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
