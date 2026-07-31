"""Load registry entries from ``shamsu/templates/<category>/``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from shamsu.registry.schema import (
    Category,
    DefinitionOfDone,
    DoDItem,
    Hole,
    Manifest,
    RegistryEntry,
)

TEMPLATES_ROOT = Path(__file__).resolve().parents[1] / "templates"


def load_registry_entry(category: Category | str) -> RegistryEntry:
    resolved = Category(category)
    root = TEMPLATES_ROOT / resolved.value
    if not root.exists():
        raise FileNotFoundError(f"No registry template found for category: {resolved.value}")
    manifest = parse_manifest(root / "manifest.yaml")
    dod = parse_dod(root / "dod.yaml")
    master_prompt_path = root / "master_prompt.md"
    if not master_prompt_path.exists():
        raise FileNotFoundError(f"Missing master prompt: {master_prompt_path}")
    return RegistryEntry(
        category=resolved,
        root=root,
        master_prompt=master_prompt_path.read_text(encoding="utf-8"),
        manifest=manifest,
        dod=dod,
    )


def parse_manifest(path: Path) -> Manifest:
    data = _read_yaml(path)
    holes = [
        Hole(
            id=str(item["id"]),
            target_file=str(item["target_file"]),
            marker=str(item["marker"]),
            kind=str(item["kind"]),
            signature=item.get("signature"),
            description=str(item["description"]),
            depends_on=[str(dep) for dep in item.get("depends_on", [])],
        )
        for item in data.get("holes", [])
    ]
    return Manifest(
        category=Category(data["category"]),
        stack={str(key): str(value) for key, value in data.get("stack", {}).items()},
        entry=str(data.get("entry", "")),
        build_cmd=str(data.get("build_cmd", "")),
        run_cmd=str(data.get("run_cmd", "")),
        preview_url=str(data.get("preview_url", "")),
        holes=holes,
    )


def parse_dod(path: Path) -> DefinitionOfDone:
    data = _read_yaml(path)
    items = [
        DoDItem(
            id=str(item["id"]),
            description=str(item["description"]),
            # `check` is optional: some templates (e.g. the multiplayer monorepo)
            # verify via an external smoke runner rather than a built-in check.
            check=str(item.get("check", "")),
            args=dict(item.get("args", {})),
            severity=str(item.get("severity", "required")),
        )
        for item in data.get("items", [])
    ]
    return DefinitionOfDone(category=Category(data["category"]), items=items)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing registry file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Registry YAML must be an object: {path}")
    return data
