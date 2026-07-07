"""Build ProjectSpec values from parsed PRDs."""
from __future__ import annotations

import re

from shamsu.prd.classifier import classify_archetype
from shamsu.prd.contract import extract_contract
from shamsu.prd.extractor import extract_entities
from shamsu.registry import load_registry_entry
from shamsu.registry.categories import CATEGORY_TO_ARCHETYPE
from shamsu.registry.detector import detect_category
from shamsu.registry.schema import Category
from shamsu.registry.suitability import assess
from shamsu.types import Archetype, DjangoFileSpec, EndpointSpec, PageSpec, ParsedPRD, ProjectSpec


def build_project_spec(parsed: ParsedPRD) -> ProjectSpec:
    project_name = _to_snake_case(parsed.title)
    app_name = _default_app_name(project_name)
    entities = extract_entities(parsed)
    endpoints = _extract_or_infer_endpoints(parsed, entities)
    pages = _extract_or_infer_pages(parsed, entities)
    theme = _select_theme(parsed.raw_text)
    archetype = classify_archetype(parsed)
    category_decision = detect_category(parsed.raw_text)
    category = _resolve_category(category_decision.category, archetype.archetype)
    selected_archetype = CATEGORY_TO_ARCHETYPE.get(category, archetype.archetype)

    generation_order = (
        _fixed_generation_order(project_name, app_name, pages)
        if selected_archetype in {Archetype.WEB_CRUD, Archetype.REST_API}
        else _generic_generation_order()
    )
    master_prompt, manifest_path, dod_path = _registry_metadata(category)

    # Source-of-truth PRD contract + generation strategy. Suitability decides
    # whether a template fits (and which) or whether to generate template-free;
    # it does not mutate `category` (kept as the raw detected category).
    contract = extract_contract(parsed)
    suitability = assess(contract, category, selected_archetype)

    return ProjectSpec(
        project_name=project_name,
        app_name=app_name,
        entities=entities,
        endpoints=endpoints,
        pages=pages,
        theme=theme,
        generation_order=generation_order,
        archetype=selected_archetype,
        archetype_confidence=max(archetype.confidence, category_decision.confidence),
        archetype_spec={
            "reason": archetype.reason,
            "category_reason": category_decision.reason,
            "category_scores": category_decision.scores,
        },
        category=category.value,
        master_prompt=master_prompt,
        manifest_path=manifest_path,
        dod_path=dod_path,
        feature_requests=_feature_requests(parsed),
        prd_contract=contract,
        suitability=suitability,
    )


def _resolve_category(category: Category, archetype: Archetype) -> Category:
    if category != Category.GENERAL_WEB:
        return category
    if archetype == Archetype.WEB_CRUD:
        return Category.WEB_CRUD
    if archetype == Archetype.REST_API:
        return Category.REST_API
    return category


def _registry_metadata(category: Category) -> tuple[str, str, str]:
    try:
        entry = load_registry_entry(category)
    except (FileNotFoundError, ValueError):
        return "", "", ""
    return (
        entry.master_prompt,
        str(entry.root / "manifest.yaml"),
        str(entry.root / "dod.yaml"),
    )


def _feature_requests(parsed: ParsedPRD) -> list[str]:
    requests: list[str] = []
    for heading, lines in parsed.sections.items():
        if any(word in heading.lower() for word in ["feature", "requirement", "gameplay"]):
            requests.extend(line.strip("- ").strip() for line in lines if line.strip())
    return requests[:20]


def _generic_generation_order() -> list[DjangoFileSpec]:
    return [
        DjangoFileSpec("index.html", "generic_template", None),
        DjangoFileSpec("README.md", "generic_docs", None),
    ]


def _extract_or_infer_endpoints(parsed: ParsedPRD, entities) -> list[EndpointSpec]:
    explicit = _extract_endpoints(parsed)
    if explicit:
        return explicit

    endpoints: list[EndpointSpec] = []
    for entity in entities:
        if entity.name.lower() == "user":
            continue
        resource = _pluralize(_to_kebab_case(entity.name))
        endpoints.extend(
            [
                EndpointSpec("GET", f"/api/{resource}/", entity.name),
                EndpointSpec("POST", f"/api/{resource}/", entity.name),
                EndpointSpec("GET", f"/api/{resource}/{{id}}/", entity.name),
                EndpointSpec("PUT", f"/api/{resource}/{{id}}/", entity.name),
                EndpointSpec("DELETE", f"/api/{resource}/{{id}}/", entity.name),
            ]
        )
    return endpoints


def _extract_endpoints(parsed: ParsedPRD) -> list[EndpointSpec]:
    endpoints: list[EndpointSpec] = []
    for heading, lines in parsed.sections.items():
        if "endpoint" not in heading.lower() and "api" not in heading.lower():
            continue
        for line in lines:
            match = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b\s+([^\s]+)", line, re.IGNORECASE)
            if not match:
                continue
            method = match.group(1).upper()
            path = match.group(2)
            resource = _resource_from_path(path)
            auth_required = "public" not in line.lower() and "no auth" not in line.lower()
            endpoints.append(EndpointSpec(method, path, resource, auth_required=auth_required))
    return endpoints


def _extract_or_infer_pages(parsed: ParsedPRD, entities) -> list[PageSpec]:
    pages = _extract_pages(parsed)
    if pages:
        return pages

    inferred = [PageSpec("Dashboard", "dashboard", "Overview and recent activity")]
    for entity in entities:
        if entity.name.lower() == "user":
            continue
        fields = [field.name for field in entity.fields]
        inferred.append(
            PageSpec(
                name=f"{entity.name} List",
                page_type="list",
                purpose=f"List and manage {entity.name} records",
                resource=entity.name,
                fields_shown=fields,
            )
        )
        inferred.append(
            PageSpec(
                name=f"{entity.name} Detail",
                page_type="detail",
                purpose=f"Show one {entity.name} record",
                resource=entity.name,
                fields_shown=fields,
            )
        )
        inferred.append(
            PageSpec(
                name=f"{entity.name} Form",
                page_type="form",
                purpose=f"Create and edit {entity.name} records",
                resource=entity.name,
                fields_shown=fields,
            )
        )
    return inferred


def _extract_pages(parsed: ParsedPRD) -> list[PageSpec]:
    pages: list[PageSpec] = []
    for heading, lines in parsed.sections.items():
        if "page" not in heading.lower() and "screen" not in heading.lower():
            continue
        for line in lines:
            name, purpose = _split_name_and_purpose(line)
            page_type = _detect_page_type(name, purpose)
            resource = _detect_resource(name)
            pages.append(
                PageSpec(
                    name=name,
                    page_type=page_type,
                    purpose=purpose,
                    resource=resource,
                    requires_login=page_type != "auth" and "public" not in purpose.lower(),
                )
            )
    return pages


def _fixed_generation_order(
    project_name: str,
    app_name: str,
    pages: list[PageSpec],
) -> list[DjangoFileSpec]:
    files = [
        DjangoFileSpec("manage.py", "fixed_template", None),
        DjangoFileSpec(f"{project_name}/__init__.py", "fixed_template", None),
        DjangoFileSpec(f"{project_name}/settings.py", "fixed_template", None),
        DjangoFileSpec(f"{project_name}/urls.py", "fixed_template", None),
        DjangoFileSpec(f"{project_name}/wsgi.py", "fixed_template", None),
        DjangoFileSpec(f"{project_name}/asgi.py", "fixed_template", None),
        DjangoFileSpec(f"{app_name}/__init__.py", "fixed_template", None),
        DjangoFileSpec(f"{app_name}/apps.py", "fixed_template", None),
        DjangoFileSpec(f"{app_name}/models.py", "model_generator", "coder"),
        DjangoFileSpec(
            f"{app_name}/serializers.py",
            "serializer_generator",
            "coder",
            depends_on=[f"{app_name}/models.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/forms.py",
            "form_generator",
            "coder",
            depends_on=[f"{app_name}/models.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/views.py",
            "view_generator",
            "coder",
            depends_on=[
                f"{app_name}/models.py",
                f"{app_name}/serializers.py",
                f"{app_name}/forms.py",
            ],
        ),
        DjangoFileSpec(
            f"{app_name}/urls.py",
            "url_generator",
            "coder",
            depends_on=[f"{app_name}/views.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/admin.py",
            "admin_generator",
            "coder",
            depends_on=[f"{app_name}/models.py"],
        ),
        DjangoFileSpec(f"{app_name}/templates/base.html", "fixed_template", None),
        DjangoFileSpec(f"{app_name}/templates/login.html", "fixed_template", None),
        DjangoFileSpec(f"{app_name}/templates/register.html", "fixed_template", None),
        DjangoFileSpec(
            f"{app_name}/templates/dashboard.html",
            "frontend_generator",
            "coder",
            depends_on=[f"{app_name}/views.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/templates/resource_list.html",
            "frontend_generator",
            "coder",
            depends_on=[f"{app_name}/views.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/templates/resource_detail.html",
            "frontend_generator",
            "coder",
            depends_on=[f"{app_name}/views.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/templates/resource_form.html",
            "frontend_generator",
            "coder",
            depends_on=[f"{app_name}/views.py", f"{app_name}/forms.py"],
        ),
        DjangoFileSpec(
            f"{app_name}/tests.py",
            "test_generator",
            "test_gen",
            depends_on=[f"{app_name}/models.py", f"{app_name}/views.py"],
        ),
        DjangoFileSpec("requirements.txt", "fixed_template", None),
        DjangoFileSpec(".env.example", "fixed_template", None),
        DjangoFileSpec("README.md", "doc_generator", "doc_agent"),
    ]
    seen_resources: set[str] = set()
    for page in pages:
        if page.page_type != "list" or not page.resource:
            continue
        resource_key = _to_kebab_case(page.resource)
        if not resource_key or resource_key in seen_resources:
            continue
        seen_resources.add(resource_key)
        files.extend(
            [
                DjangoFileSpec(
                    f"{app_name}/templates/{resource_key}/list.html",
                    "fixed_template",
                    None,
                ),
                DjangoFileSpec(
                    f"{app_name}/templates/{resource_key}/_item.html",
                    "fixed_template",
                    None,
                ),
            ]
        )
    return files


def _split_name_and_purpose(line: str) -> tuple[str, str]:
    cleaned = line.replace("**", "").strip()
    if ":" in cleaned:
        name, purpose = cleaned.split(":", 1)
    elif "-" in cleaned:
        name, purpose = cleaned.split("-", 1)
    else:
        name, purpose = cleaned, cleaned
    return name.strip(" /"), purpose.strip()


def _detect_page_type(name: str, purpose: str):
    text = f"{name} {purpose}".lower()
    if "login" in text or "register" in text or "auth" in text:
        return "auth"
    if "dashboard" in text:
        return "dashboard"
    if "detail" in text:
        return "detail"
    if "form" in text or "create" in text or "new" in text:
        return "form"
    return "list"


def _detect_resource(name: str) -> str | None:
    lowered = name.lower()
    if "dashboard" in lowered or "login" in lowered or "register" in lowered:
        return None
    tokens = [token for token in re.split(r"[^A-Za-z0-9]+", name) if token]
    if not tokens:
        return None
    return _to_pascal_case(tokens[0].rstrip("s"))


def _select_theme(raw_text: str) -> str:
    text = raw_text.lower()
    if any(word in text for word in ["finance", "expense", "budget", "business"]):
        return "corporate"
    if any(word in text for word in ["blog", "creative", "writing"]):
        return "nord"
    if any(word in text for word in ["developer", "technical", "code"]):
        return "dark"
    if any(word in text for word in ["health", "wellness", "clinic"]):
        return "cupcake"
    return "corporate"


def _resource_from_path(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part and not part.startswith("{")]
    if not parts:
        return "Resource"
    return _to_pascal_case(parts[-1].rstrip("s"))


def _default_app_name(project_name: str) -> str:
    parts = [part for part in project_name.split("_") if part]
    if not parts:
        return "app"
    if len(parts) == 1:
        return parts[0]
    return parts[-1]


def _to_snake_case(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.strip("_").lower() or "project"


def _to_kebab_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _to_pascal_case(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def _pluralize(text: str) -> str:
    if text.endswith("y"):
        return f"{text[:-1]}ies"
    if text.endswith("s"):
        return text
    return f"{text}s"
