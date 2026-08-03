from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="node-express",
    slot="backend",
    provides=("node", "node.js", "express", "javascript"),
    root="backend",
    folder_map={
        "manifest": "package.json",
        "entrypoint": "server.js",
        "app": "src/app.js",
        "routes": "src/routes",
        "database": "src/db.js",
        "schema": "src/schema.sql",
        "tests": "tests",
    },
    config_files=("package.json", ".env.example", "Dockerfile"),
    verify=("npm install", "npm run build", "npm test"),
    description="Node.js Express API backend.",
)
