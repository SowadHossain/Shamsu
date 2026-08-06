"""Entities from SQL DDL embedded in a PRD.

A PRD that ships ``CREATE TABLE`` statements has already done the data-modelling
work, in the most precise notation available - and SHAMSU could not read a line
of it. :func:`shamsu.prd.extractor._parse_block_field` understands
``name: string, required`` bullets, so every column definition parsed as
nothing, and a PRD whose entire data model was a schema dump yielded **zero
entities**. Zero entities is the state that degrades a build to a single static
page, so a well-specified PRD did worse than a vague one.

The scan is deliberately tolerant of formatting. Word and PDF both flatten a
pasted schema onto one line, so nothing here may depend on newlines: statements
are found by scanning for balanced parentheses, and columns are split on
top-level commas so ``DECIMAL(12,2)`` and ``CHECK (x IN ('a','b'))`` survive.
"""
from __future__ import annotations

import re

from shamsu.types import EntityFieldSpec, EntitySpec

_CREATE_TABLE_RE = re.compile(
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?"
    r"[\"`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[\"`\]]?\s*\(",
    re.IGNORECASE,
)

# Column names Django supplies itself, or that carry no modelling information.
_SKIPPED_COLUMNS = frozenset({"id", "created_at", "updated_at", "deleted_at"})

# A comma-separated fragment starting with one of these is a table constraint,
# not a column.
_TABLE_CONSTRAINTS = (
    "primary key",
    "foreign key",
    "unique",
    "check",
    "constraint",
    "index",
    "key ",
    "exclude",
)

_REFERENCES_RE = re.compile(
    r"\breferences\s+[\"`\[]?(?P<table>[A-Za-z_][A-Za-z0-9_]*)[\"`\]]?",
    re.IGNORECASE,
)
_ON_DELETE_RE = re.compile(
    r"\bon\s+delete\s+(?P<action>cascade|set\s+null|set\s+default|restrict|no\s+action)",
    re.IGNORECASE,
)
_IN_CHOICES_RE = re.compile(r"\bin\s*\(\s*(?P<values>'[^)]*')\s*\)", re.IGNORECASE)
_QUOTED_VALUE_RE = re.compile(r"'([^']*)'")
_DEFAULT_RE = re.compile(r"\bdefault\s+(?P<value>'[^']*'|[A-Za-z0-9_.+-]+)", re.IGNORECASE)

_ON_DELETE_MAP = {
    "cascade": "CASCADE",
    "set null": "SET_NULL",
    "set default": "SET_DEFAULT",
    "restrict": "PROTECT",
    "no action": "DO_NOTHING",
}


def entities_from_sql(text: str) -> list[EntitySpec]:
    """Every table declared by ``CREATE TABLE`` in *text*, as entities."""
    entities: list[EntitySpec] = []
    seen: set[str] = set()
    for match in _CREATE_TABLE_RE.finditer(text or ""):
        body = _balanced_body(text, match.end() - 1)
        if body is None:
            continue
        entity = _entity_from_table(match.group("name"), body)
        if entity is None or entity.name.lower() in seen:
            continue
        seen.add(entity.name.lower())
        entities.append(entity)
    return entities


def _balanced_body(text: str, open_index: int) -> str | None:
    """Text between the parenthesis at *open_index* and its partner."""
    depth = 0
    in_quote = ""
    for index in range(open_index, len(text)):
        char = text[index]
        if in_quote:
            if char == in_quote:
                in_quote = ""
            continue
        if char in "'\"":
            in_quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return None


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_quote = ""
    current: list[str] = []
    for char in body:
        if in_quote:
            current.append(char)
            if char == in_quote:
                in_quote = ""
            continue
        if char in "'\"":
            in_quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def _entity_from_table(table_name: str, body: str) -> EntitySpec | None:
    from shamsu.prd.extractor import _singularize, _to_pascal_case

    entity_name = _to_pascal_case(_singularize(table_name))
    fields: list[EntityFieldSpec] = []
    relationships: list[str] = []

    for fragment in _split_top_level(body):
        column = _parse_column(fragment)
        if column is None:
            continue
        fields.append(column)
        if column.django_type == "ForeignKey":
            relationships.append(f"belongs_to:{column.kwargs.get('to', '')}")

    if not fields:
        return None
    return EntitySpec(name=entity_name, fields=fields, relationships=relationships)


def _parse_column(fragment: str) -> EntityFieldSpec | None:
    from shamsu.prd.extractor import _singularize, _to_pascal_case

    cleaned = " ".join(fragment.split())
    lowered = cleaned.lower()
    if any(lowered.startswith(marker) for marker in _TABLE_CONSTRAINTS):
        return None

    match = re.match(r"^[\"`\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)[\"`\]]?\s+(?P<rest>.+)$", cleaned)
    if not match:
        return None
    name = match.group("name")
    rest = match.group("rest")
    rest_lower = rest.lower()

    if name.lower() in _SKIPPED_COLUMNS or "primary key" in rest_lower:
        return None

    nullable = "not null" not in rest_lower
    reference = _REFERENCES_RE.search(rest)
    if reference:
        kwargs: dict[str, object] = {
            "to": _to_pascal_case(_singularize(reference.group("table"))),
            "on_delete": _foreign_key_on_delete(rest, nullable),
        }
        if nullable:
            kwargs.update({"null": True, "blank": True})
        return EntityFieldSpec(
            name=name.removesuffix("_id") or name,
            django_type="ForeignKey",
            kwargs=kwargs,
        )

    django_type, kwargs = _django_type_for(rest)
    choices = _choices_from_check(rest, name)
    if choices:
        django_type = "CharField"
        kwargs = {"max_length": max(len(value) for value, _ in choices) + 10, "choices": choices}
    if nullable and django_type != "BooleanField":
        kwargs.update({"null": True, "blank": True})
    if re.search(r"\bunique\b", rest_lower):
        kwargs["unique"] = True
    default = _default_value(rest, django_type)
    if default is not None:
        kwargs["default"] = default
    return EntityFieldSpec(name=name, django_type=django_type, kwargs=kwargs)


def _foreign_key_on_delete(rest: str, nullable: bool) -> str:
    match = _ON_DELETE_RE.search(rest)
    if not match:
        return "SET_NULL" if nullable else "CASCADE"
    action = " ".join(match.group("action").split()).lower()
    return _ON_DELETE_MAP.get(action, "CASCADE")


def _django_type_for(rest: str) -> tuple[str, dict[str, object]]:
    lowered = rest.lower()
    varchar = re.match(r"^(?:varchar|character varying|nvarchar|char)\s*\(\s*(\d+)\s*\)", lowered)
    if varchar:
        return "CharField", {"max_length": int(varchar.group(1))}
    numeric = re.match(r"^(?:decimal|numeric)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", lowered)
    if numeric:
        return "DecimalField", {
            "max_digits": int(numeric.group(1)),
            "decimal_places": int(numeric.group(2)),
        }
    if lowered.startswith(("varchar", "character varying", "nvarchar", "char")):
        return "CharField", {"max_length": 200}
    if lowered.startswith(("decimal", "numeric", "money")):
        return "DecimalField", {"max_digits": 12, "decimal_places": 2}
    if lowered.startswith("text"):
        return "TextField", {}
    if lowered.startswith(("timestamp", "datetime")):
        return "DateTimeField", {}
    if lowered.startswith("date"):
        return "DateField", {}
    if lowered.startswith("time"):
        return "TimeField", {}
    if lowered.startswith(("boolean", "bool", "bit")):
        return "BooleanField", {}
    if lowered.startswith(("smallint", "integer", "int", "bigint", "serial", "bigserial")):
        return "IntegerField", {}
    if lowered.startswith(("float", "double", "real")):
        return "FloatField", {}
    if lowered.startswith(("json", "jsonb")):
        return "JSONField", {}
    if lowered.startswith("uuid"):
        return "UUIDField", {}
    return "CharField", {"max_length": 200}


def _choices_from_check(rest: str, column: str) -> list[tuple[str, str]]:
    """Django choices from a column-level ``CHECK (col IN ('A','B'))``."""
    if "check" not in rest.lower():
        return []
    match = _IN_CHOICES_RE.search(rest)
    if not match:
        return []
    values = _QUOTED_VALUE_RE.findall(match.group("values"))
    return [(value, value.replace("_", " ").title()) for value in values if value]


def _default_value(rest: str, django_type: str) -> object | None:
    match = _DEFAULT_RE.search(rest)
    if not match:
        return None
    raw = match.group("value").strip()
    if raw.lower() in {"null", "current_timestamp", "now"} or raw.endswith("("):
        return None
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if django_type in {"IntegerField", "FloatField", "DecimalField"}:
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return None
    return None
