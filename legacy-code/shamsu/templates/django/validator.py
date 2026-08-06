"""Validation helpers for generated Django templates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from shamsu.types import ProjectSpec

URL_TAG_RE = re.compile(r"\{%\s*url\s+['\"](?P<name>[^'\"]+)['\"]")
URL_NAME_RE = re.compile(r"\bname\s*=\s*['\"](?P<name>[^'\"]+)['\"]")
MODEL_FIELD_RE = re.compile(
    r"\{\{\s*(?:object|item|row|resource|(?P<model>[a-z][A-Za-z0-9_]*))"
    r"\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)"
)
HX_TARGET_RE = re.compile(r"\bhx-target\s*=\s*['\"]#(?P<id>[A-Za-z][A-Za-z0-9_-]*)['\"]")
ID_RE = re.compile(r"\bid\s*=\s*['\"](?P<id>[A-Za-z][A-Za-z0-9_-]*)['\"]")
RAW_INPUT_RE = re.compile(r"<input\b", re.IGNORECASE)

COMMON_TEMPLATE_FIELDS = {
    "id",
    "pk",
    "user",
    "username",
    "is_authenticated",
    "messages",
    "message",
}


@dataclass(frozen=True)
class TemplateValidationError:
    path: str
    rule: str
    message: str


@dataclass
class TemplateValidationResult:
    errors: list[TemplateValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_generated_templates(
    project: ProjectSpec,
    files: dict[str, str],
) -> TemplateValidationResult:
    errors: list[TemplateValidationError] = []
    allowed_urls = _allowed_url_names(files)
    allowed_fields = _allowed_field_names(project)

    for path, content in files.items():
        if not path.endswith(".html"):
            continue
        errors.extend(_validate_url_tags(path, content, allowed_urls))
        errors.extend(_validate_model_fields(path, content, allowed_fields))
        errors.extend(_validate_htmx_targets(path, content))
        errors.extend(_validate_raw_inputs(path, content))

    return TemplateValidationResult(errors=errors)


def _validate_url_tags(
    path: str,
    content: str,
    allowed_urls: set[str],
) -> list[TemplateValidationError]:
    errors: list[TemplateValidationError] = []
    for match in URL_TAG_RE.finditer(content):
        name = match.group("name")
        if name not in allowed_urls:
            errors.append(
                TemplateValidationError(
                    path=path,
                    rule="url_name",
                    message=f"Unknown URL name: {name}",
                )
            )
    return errors


def _validate_model_fields(
    path: str,
    content: str,
    allowed_fields: set[str],
) -> list[TemplateValidationError]:
    errors: list[TemplateValidationError] = []
    for match in MODEL_FIELD_RE.finditer(content):
        field_name = match.group("field")
        if field_name not in allowed_fields:
            errors.append(
                TemplateValidationError(
                    path=path,
                    rule="model_field",
                    message=f"Unknown model field reference: {field_name}",
                )
            )
    return errors


def _validate_htmx_targets(path: str, content: str) -> list[TemplateValidationError]:
    ids = {match.group("id") for match in ID_RE.finditer(content)}
    errors: list[TemplateValidationError] = []
    for match in HX_TARGET_RE.finditer(content):
        target = match.group("id")
        if target not in ids:
            errors.append(
                TemplateValidationError(
                    path=path,
                    rule="htmx_target",
                    message=f"HTMX target #{target} has no matching id in template",
                )
            )
    return errors


def _validate_raw_inputs(path: str, content: str) -> list[TemplateValidationError]:
    if not RAW_INPUT_RE.search(content):
        return []
    return [
        TemplateValidationError(
            path=path,
            rule="raw_input",
            message="Generated forms must use {{ form|crispy }} instead of raw <input> tags",
        )
    ]


def _allowed_url_names(files: dict[str, str]) -> set[str]:
    names = {
        "token_obtain_pair",
        "token_refresh",
    }
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        names.update(match.group("name") for match in URL_NAME_RE.finditer(content))
    return names


def _allowed_field_names(project: ProjectSpec) -> set[str]:
    fields = set(COMMON_TEMPLATE_FIELDS)
    for entity in project.entities:
        fields.add(_to_snake_case(entity.name))
        for field_spec in entity.fields:
            fields.add(field_spec.name)
    return fields


def _to_snake_case(text: str) -> str:
    normalized = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return re.sub(r"[^a-z0-9_]+", "_", normalized.lower()).strip("_")
