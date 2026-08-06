"""Static consistency checks for generated Django templates."""
from __future__ import annotations

import re
from pathlib import Path

from shamsu.templates.django.checker import ConsistencyDiagnostic
from shamsu.types import EntitySpec, ProjectSpec

URL_RE = re.compile(r"{%\s*url\s+['\"]([^'\"]+)['\"]")
ID_RE = re.compile(r"\bid=['\"]([^'\"]+)['\"]")
HTMX_TARGET_RE = re.compile(r"\bhx-target=['\"]#([^'\"]+)['\"]")
RAW_FORM_RE = re.compile(r"<\s*(input|select|textarea)\b", re.IGNORECASE)
VARIABLE_RE = re.compile(r"{{\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")


class FrontendConsistencyChecker:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def check(self, project: ProjectSpec) -> list[ConsistencyDiagnostic]:
        templates_root = self.project_root / project.app_name / "templates"
        if not templates_root.exists():
            return [
                ConsistencyDiagnostic(
                    f"{project.app_name}/templates",
                    "templates",
                    "Generated templates directory is missing.",
                )
            ]
        diagnostics: list[ConsistencyDiagnostic] = []
        url_names = _url_names(project, self.project_root)
        model_fields = _model_fields(project)
        entity_vars = _entity_variable_map(project)
        for template in templates_root.rglob("*.html"):
            relative = template.relative_to(self.project_root).as_posix()
            content = template.read_text(encoding="utf-8")
            diagnostics.extend(_check_urls(relative, content, url_names))
            diagnostics.extend(_check_htmx_targets(relative, content))
            diagnostics.extend(_check_raw_form_inputs(relative, content))
            diagnostics.extend(_check_field_refs(relative, content, entity_vars, model_fields))
        return diagnostics


def _url_names(project: ProjectSpec, project_root: Path) -> set[str]:
    names = {"login", "logout", "register", "dashboard"}
    app_urls = project_root / project.app_name / "urls.py"
    if app_urls.exists():
        names.update(re.findall(r"name=['\"]([^'\"]+)['\"]", app_urls.read_text(encoding="utf-8")))
    for entity in _business_entities(project):
        base = _resource_url_name(entity.name)
        names.update({f"{base}-list", f"{base}-detail", f"{base}-form", f"{base}-delete"})
    return names


def _model_fields(project: ProjectSpec) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    for entity in _business_entities(project):
        values = {"id", "pk"}
        values.update(field.name for field in entity.fields)
        fields[entity.name] = values
    return fields


def _entity_variable_map(project: ProjectSpec) -> dict[str, str]:
    mapping: dict[str, str] = {"object": _business_entities(project)[0].name if _business_entities(project) else ""}
    for entity in _business_entities(project):
        singular = _to_snake_case(entity.name)
        mapping[singular] = entity.name
        mapping[_plural_name(singular)] = entity.name
    return {key: value for key, value in mapping.items() if value}


def _check_urls(file_path: str, content: str, url_names: set[str]) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    for name in URL_RE.findall(content):
        if name not in url_names:
            diagnostics.append(
                ConsistencyDiagnostic(file_path, name, f"Template references missing URL name {name}.")
            )
    return diagnostics


def _check_htmx_targets(file_path: str, content: str) -> list[ConsistencyDiagnostic]:
    ids = set(ID_RE.findall(content))
    diagnostics: list[ConsistencyDiagnostic] = []
    for target in HTMX_TARGET_RE.findall(content):
        if target not in ids:
            diagnostics.append(
                ConsistencyDiagnostic(file_path, target, f"HTMX target #{target} is not defined in this template.")
            )
    return diagnostics


def _check_raw_form_inputs(file_path: str, content: str) -> list[ConsistencyDiagnostic]:
    if "{{ form|crispy }}" in content:
        return []
    return [
        ConsistencyDiagnostic(file_path, match.group(1), "Generated forms should use crispy forms, not raw inputs.")
        for match in RAW_FORM_RE.finditer(content)
    ]


def _check_field_refs(
    file_path: str,
    content: str,
    entity_vars: dict[str, str],
    model_fields: dict[str, set[str]],
) -> list[ConsistencyDiagnostic]:
    diagnostics: list[ConsistencyDiagnostic] = []
    scoped_vars = dict(entity_vars)
    folder = Path(file_path).parent.name
    for entity in model_fields:
        if _resource_url_name(entity) == folder:
            scoped_vars["object"] = entity
            break
    for variable, field in VARIABLE_RE.findall(content):
        entity = scoped_vars.get(variable)
        if not entity:
            continue
        if field not in model_fields.get(entity, set()):
            diagnostics.append(
                ConsistencyDiagnostic(
                    file_path,
                    f"{variable}.{field}",
                    f"Template references missing field {field} on {entity}.",
                )
            )
    return diagnostics


def _business_entities(project: ProjectSpec) -> list[EntitySpec]:
    return [entity for entity in project.entities if entity.name.lower() != "user"]


def _resource_url_name(text: str) -> str:
    return _to_kebab_case(text)


def _plural_name(text: str) -> str:
    if text.endswith("y"):
        return f"{text[:-1]}ies"
    if text.endswith("s"):
        return text
    return f"{text}s"


def _to_snake_case(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.strip("_").lower()


def _to_kebab_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
