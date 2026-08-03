from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="postgres",
    slot="database",
    provides=("postgres", "postgresql", "psql"),
    root=".",
    folder_map={
        "compose": "docker-compose.yml",
        "env": ".env.example",
        "schema": "backend/src/schema.sql",
        "migrations": "backend/migrations",
    },
    config_files=("docker-compose.yml", ".env.example"),
    verify=("docker compose config -q",),
    description="PostgreSQL service and environment contract.",
)
