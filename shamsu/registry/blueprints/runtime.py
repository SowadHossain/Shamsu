"""Runtime plan derived from selected stack blueprints."""
from __future__ import annotations

from typing import Any

from shamsu.prd.contract import PRDContract
from shamsu.registry.blueprints.resolver import resolve_blueprints
from shamsu.registry.blueprints.types import BlueprintResolution


def runtime_plan_for_contract(contract: PRDContract) -> dict[str, Any]:
    """Return Docker/runtime files implied by explicit blueprint selections.

    Suggestions deliberately do not create runtime files. A compose stack is a
    commitment, not a default.
    """
    return runtime_plan_for_resolution(resolve_blueprints(contract))


def runtime_plan_for_resolution(resolution: BlueprintResolution) -> dict[str, Any]:
    selected = resolution.selected
    runtime_files: dict[str, dict[str, str]] = {}
    services: list[dict[str, str]] = []

    backend = selected.get("backend")
    frontend = selected.get("frontend")
    database = selected.get("database")

    for slot, blueprint in selected.items():
        for path in blueprint.config_paths():
            runtime_files[path] = {
                "slot": slot,
                "blueprint": blueprint.id,
                "purpose": "runtime configuration",
            }

    if database and database.id == "postgres":
        runtime_files["docker-compose.yml"] = {
            "slot": "database",
            "blueprint": "postgres",
            "purpose": "compose stack tying database/backend/frontend services together",
        }
        runtime_files[".env.example"] = {
            "slot": "database",
            "blueprint": "postgres",
            "purpose": "shared Docker/Postgres environment example",
        }
        services.append({"name": "postgres", "blueprint": "postgres", "role": "database"})

    if backend:
        services.append({"name": "backend", "blueprint": backend.id, "role": "backend"})
    if frontend:
        services.append({"name": "frontend", "blueprint": frontend.id, "role": "frontend"})

    verifiers = [
        command
        for blueprint in selected.values()
        for command in blueprint.verify
    ]

    return {
        "schema_version": 1,
        "files": runtime_files,
        "services": services,
        "verifiers": list(dict.fromkeys(verifiers)),
        "compose": {
            "path": "docker-compose.yml",
            "services": [service["name"] for service in services],
            "database": database.id if database else "",
        }
        if database and database.id == "postgres"
        else {},
    }


def runtime_file_paths_for_contract(contract: PRDContract) -> list[str]:
    return sorted(runtime_plan_for_contract(contract)["files"])
