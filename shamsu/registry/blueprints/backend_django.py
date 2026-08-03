from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="django",
    slot="backend",
    provides=("django", "python"),
    root="backend",
    folder_map={
        "entrypoint": "manage.py",
        "settings": "config/settings.py",
        "urls": "config/urls.py",
        "models": "core/models.py",
        "views": "core/views.py",
        "forms": "core/forms.py",
        "tests": "core/tests",
    },
    config_files=("requirements.txt", ".env.example", "Dockerfile"),
    verify=("python manage.py check", "python manage.py migrate --check"),
    description="Django backend and app package layout.",
)
