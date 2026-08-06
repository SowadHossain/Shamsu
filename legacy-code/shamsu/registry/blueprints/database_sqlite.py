from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="sqlite",
    slot="database",
    provides=("sqlite", "sqlite3"),
    root="backend",
    folder_map={
        "database": "db.sqlite3",
        "schema": "src/schema.sql",
        "migrations": "migrations",
        "seed": "scripts/seed.py",
    },
    config_files=(),
    verify=("python manage.py migrate --check",),
    description="SQLite local persistence.",
)
