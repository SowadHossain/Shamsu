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

_ENTITY_HEADING_RE = re.compile(r"^entity\s*:\s*(?P<name>[A-Za-z][\w -]*)$", re.IGNORECASE)
_FIELD_NAME_ENTITY_HINTS = {
    "action", "active", "amount", "approvedat", "assetid", "assettag", "body",
    "category", "city", "code", "completedat", "contactname", "country",
    "createdat", "currency", "deletedat", "description", "detectedat", "email",
    "entityid", "entitytype", "id", "name", "notes", "phone", "priority",
    "quantity", "region", "role", "severity", "siteid", "status", "summary",
    "timestamp", "title", "type", "updatedat", "vendorid", "visibility",
}


# Explicit signals that a PRD asks for a static HTML/CSS/JS frontend, so the
# planner must NOT fall back to a generic Django project (manage.py / settings /
# login / register / dashboard). Grounded: we only take this branch when the PRD
# text itself says so.
_STATIC_FRONTEND_PHRASES = (
    "static site", "static website", "static web app", "static web page",
    "vanilla javascript", "vanilla js", "plain html", "plain javascript",
    "no backend", "without a backend", "frontend only", "front-end only",
    "client-side only", "client side only", "browser only",
)
# Backend/framework signals that disqualify the static-frontend branch: if the
# PRD really needs a server, keep the normal (possibly Django) planning path.
_BACKEND_FRAMEWORK_PHRASES = (
    "django", "flask", "fastapi", "express", "rails", "spring", "laravel",
    "node backend", "node.js backend", "backend service", "server-side",
    "server side", "microservice", "rest api", "graphql",
    "postgres", "postgresql", "mysql", "sqlite", "mongodb", "database",
    " sql ", "orm", "authentication service", "oauth",
)


def _mentions_html_css_js(text: str) -> bool:
    return "html" in text and ("css" in text or "javascript" in text or " js" in text or "js." in text)


def is_static_frontend_prd(parsed: ParsedPRD, request_text: str = "") -> bool:
    """True when the PRD explicitly asks for a static HTML/CSS/JS frontend and
    shows no backend/framework/database signals. Used to stop the planner from
    defaulting to a generic Django CRUD project on a small frontend PRD.

    A request that names a backend itself always wins: "build this as a Django
    project" produced a static `index.html` because this gate ran first and
    only ever read the document.
    """
    text = f"{parsed.title}\n{parsed.raw_text}".lower()
    request_lowered = (request_text or "").lower()
    if any(phrase in request_lowered for phrase in _BACKEND_FRAMEWORK_PHRASES):
        return False
    signal = _mentions_html_css_js(text) or any(phrase in text for phrase in _STATIC_FRONTEND_PHRASES)
    if not signal:
        return False
    return not any(phrase in text for phrase in _BACKEND_FRAMEWORK_PHRASES)


def _frontend_generation_order() -> list[DjangoFileSpec]:
    """Deterministic static-frontend file set - the three files a 'build with
    HTML/CSS/JS' PRD expects, plus a README. Never manage.py/settings/login."""
    return [
        DjangoFileSpec("index.html", "frontend_template", "coder"),
        DjangoFileSpec("style.css", "frontend_template", "coder"),
        DjangoFileSpec("script.js", "frontend_template", "coder"),
        DjangoFileSpec("README.md", "generic_docs", "doc_agent"),
    ]


def _build_static_frontend_spec(parsed: ParsedPRD) -> ProjectSpec:
    project_name = _to_snake_case(parsed.title)
    app_name = _default_app_name(project_name)
    entities = extract_entities(parsed)
    # Only pages the PRD explicitly lists - never an inferred Dashboard/CRUD set.
    pages = _extract_pages(parsed)
    theme = _select_theme(parsed.raw_text)
    contract = extract_contract(parsed)
    category = Category.GENERAL_WEB
    return ProjectSpec(
        project_name=project_name,
        app_name=app_name,
        entities=entities,
        endpoints=[],
        pages=pages,
        theme=theme,
        generation_order=_frontend_generation_order(),
        archetype=Archetype.GENERIC_WEB,
        archetype_confidence=0.9,
        archetype_spec={
            "reason": "PRD explicitly requests a static HTML/CSS/JS frontend; no backend signals.",
            "category_reason": "static-frontend override",
            "category_scores": {},
        },
        category=category.value,
        master_prompt="",
        manifest_path="",
        dod_path="",
        feature_requests=_feature_requests(parsed),
        prd_contract=contract,
        suitability=assess(contract, category, Archetype.GENERIC_WEB),
    )


def build_project_spec(
    parsed: ParsedPRD,
    request_text: str = "",
    extra_entities: list | None = None,
) -> ProjectSpec:
    # Grounding gate: a PRD that explicitly asks for static HTML/CSS/JS (and
    # shows no backend signals) must not become a generic Django CRUD project.
    if is_static_frontend_prd(parsed, request_text):
        return _build_static_frontend_spec(parsed)
    # `request_text` carries the user's own instruction so an explicit "build
    # this as a Django project" outranks whatever stack the document implies.
    contract = extract_contract(
        parsed, request_text=request_text, extra_entities=extra_entities
    )
    project_name = _to_snake_case(parsed.title)
    app_name = _default_app_name(project_name)
    entities = extract_entities(parsed)
    if extra_entities:
        known = {entity.name.lower() for entity in entities}
        entities = [
            *entities,
            *[item for item in extra_entities if item.name.lower() not in known],
        ]
    extraction_error = _entity_extraction_error(parsed, entities)
    if extraction_error:
        contract.extraction_warnings.append(extraction_error)
    endpoints = (
        [
            EndpointSpec(
                str(item["method"]),
                str(item["path"]),
                _resource_from_path(str(item["path"])),
                auth_required=bool(item.get("auth_required", True)),
            )
            for item in contract.api_endpoints
        ]
        if contract.api_endpoints
        else _extract_or_infer_endpoints(parsed, entities)
    )
    pages = _extract_or_infer_pages(parsed, entities)
    theme = _select_theme(parsed.raw_text)
    archetype = classify_archetype(parsed)
    category_decision = detect_category(parsed.raw_text)
    if contract.requires_full_stack:
        category = Category.WEB_CRUD
        selected_archetype = Archetype.WEB_CRUD
    else:
        category = _resolve_category(category_decision.category, archetype.archetype)
        selected_archetype = CATEGORY_TO_ARCHETYPE.get(category, archetype.archetype)

    domain_entities = [entity for entity in entities if entity.name.lower() not in {"user", "session"}]
    needs_input = (contract.requires_full_stack and not domain_entities) or bool(extraction_error)
    assumptions = list(contract.assumptions)
    if contract.requires_full_stack and not contract.stack_hint:
        assumptions = [item for item in assumptions if "framework" not in item.lower()]
        assumptions.append(
            "Django is selected as SHAMSU's supported local full-stack default; "
            "the PRD does not specify an application framework."
        )

    generation_order = (
        _fixed_generation_order(project_name, app_name, pages)
        if not needs_input and selected_archetype in {Archetype.WEB_CRUD, Archetype.REST_API}
        else _generic_generation_order()
    )
    if needs_input:
        generation_order = []
    master_prompt, manifest_path, dod_path = _registry_metadata(category)

    # Source-of-truth PRD contract + generation strategy. Suitability decides
    # whether a template fits (and which) or whether to generate template-free;
    # it does not mutate `category` (kept as the raw detected category).
    suitability = assess(contract, category, selected_archetype)
    if not needs_input and getattr(getattr(suitability, "strategy", None), "value", "") == "freeform":
        generation_order = _generic_generation_order()

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
        generation_ready=not needs_input,
        needs_input=needs_input,
        clarification_question=(
            extraction_error
            if extraction_error
            else "Which persistent domain entities and fields should the full-stack application manage?"
            if needs_input
            else ""
        ),
        assumptions=_dedupe_strings(assumptions),
        definition_of_done=list(contract.acceptance_criteria),
    )


def _entity_extraction_error(parsed: ParsedPRD, entities) -> str:
    heading_names = _entity_heading_names(parsed)
    if not heading_names:
        return ""
    extracted = {entity.name.lower() for entity in entities}
    missing = [name for name in heading_names if name.lower() not in extracted]
    generic_entities = [
        entity.name for entity in entities
        if entity.name.lower() in _FIELD_NAME_ENTITY_HINTS and len(entity.fields) <= 2
    ]
    too_many_generic = len(generic_entities) >= max(5, len(heading_names))
    mostly_missing = bool(missing) and len(missing) >= max(1, len(heading_names) // 2)
    if mostly_missing or too_many_generic:
        return (
            "PRD entity extraction looks unsafe: the parser appears to have extracted field "
            "names as entities instead of the heading-defined domain model. Expected entity "
            f"headings include {', '.join(heading_names[:8])}; extracted suspicious entities "
            f"include {', '.join(generic_entities[:8]) or 'none'}."
        )
    return ""


def _entity_heading_names(parsed: ParsedPRD) -> list[str]:
    names: list[str] = []
    for heading in parsed.sections:
        normalized = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).strip().rstrip(":")
        match = _ENTITY_HEADING_RE.match(normalized)
        if match:
            names.append(_to_pascal_case(match.group("name")))
    return names


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


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
    pages = _extract_pages(parsed, entities)
    if pages:
        return _ensure_resource_pages(pages, entities)

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


def _ensure_resource_pages(pages: list[PageSpec], entities) -> list[PageSpec]:
    complete = list(pages)
    existing = {(page.resource, page.page_type) for page in pages if page.resource}
    for entity in entities:
        if entity.name.lower() in {"user", "session"}:
            continue
        fields = [field.name for field in entity.fields]
        for page_type, label, purpose in (
            ("list", "List", "List and manage records"),
            ("form", "Form", "Create and edit records"),
            ("detail", "Detail", "View one record"),
        ):
            if (entity.name, page_type) in existing:
                continue
            complete.append(
                PageSpec(
                    name=f"{entity.name} {label}",
                    page_type=page_type,
                    purpose=f"{purpose} for {entity.name}",
                    resource=entity.name,
                    fields_shown=fields,
                )
            )
    return complete


def _extract_pages(parsed: ParsedPRD, entities=()) -> list[PageSpec]:
    pages: list[PageSpec] = []
    for heading, lines in parsed.sections.items():
        normalized_heading = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", heading).lower()
        if normalized_heading not in {"pages", "screens", "frontend pages"}:
            continue
        for line in lines:
            name, purpose = _split_name_and_purpose(line)
            if not name or name.lower().startswith(("the application should", "required elements")):
                continue
            page_type = _detect_page_type(name, purpose)
            resource = _detect_resource(name, entities)
            public_page = any(
                token in name.lower()
                for token in ("landing", "login", "registration", "register", "not found", "error")
            )
            pages.append(
                PageSpec(
                    name=name,
                    page_type=page_type,
                    purpose=purpose,
                    resource=resource,
                    requires_login=not public_page and "public" not in purpose.lower(),
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
        DjangoFileSpec(f"{app_name}/migrations/__init__.py", "fixed_template", None),
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
    cleaned = line.replace("**", "").strip().lstrip("-*+• ").strip()
    if ":" in cleaned:
        name, purpose = cleaned.split(":", 1)
    elif "-" in cleaned:
        name, purpose = cleaned.split("-", 1)
    else:
        name, purpose = cleaned, cleaned
    return name.strip(" /"), purpose.strip()


def _detect_page_type(name: str, purpose: str):
    text = f"{name} {purpose}".lower()
    if "login" in text or "register" in text or "registration" in text or "auth" in text:
        return "auth"
    if "dashboard" in text:
        return "dashboard"
    if "detail" in text:
        return "detail"
    if "form" in text or "create" in text or "new" in text or "edit" in text or "profile" in text:
        return "form"
    return "list"


def _detect_resource(name: str, entities=()) -> str | None:
    lowered = name.lower()
    if any(token in lowered for token in ("dashboard", "login", "register", "profile", "password")):
        return None
    words = set(re.findall(r"[a-z0-9]+", lowered))
    for entity in entities:
        entity_name = entity.name.lower()
        plural = _pluralize(entity_name)
        if entity_name in words or plural in words:
            return entity.name
    if entities:
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
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def _pluralize(text: str) -> str:
    if text.endswith("y"):
        return f"{text[:-1]}ies"
    if text.endswith("s"):
        return text
    return f"{text}s"
