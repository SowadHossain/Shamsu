from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="django-templates",
    slot="frontend",
    provides=("django-templates", "server-rendered-html", "html"),
    root="backend",
    folder_map={
        "templates": "core/templates",
        "base_template": "core/templates/base.html",
        "dashboard": "core/templates/dashboard.html",
        "list": "core/templates/resource_list.html",
        "detail": "core/templates/resource_detail.html",
        "form": "core/templates/resource_form.html",
    },
    verify=("python manage.py check",),
    description="Server-rendered Django template frontend.",
)
