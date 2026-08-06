from __future__ import annotations

from shamsu.registry.blueprints.types import StackBlueprint


BLUEPRINT = StackBlueprint(
    id="react-vite",
    slot="frontend",
    provides=("react", "vite", "typescript", "tsx", "jsx"),
    root="frontend",
    folder_map={
        "manifest": "package.json",
        "html": "index.html",
        "entrypoint": "src/main.tsx",
        "app": "src/App.tsx",
        "styles": "src/styles.css",
        "tests": "src",
    },
    config_files=("package.json", "index.html", "vite.config.ts", ".env.example", "Dockerfile"),
    verify=("npm install", "npm run build", "npm test"),
    description="React single-page frontend built with Vite.",
)
