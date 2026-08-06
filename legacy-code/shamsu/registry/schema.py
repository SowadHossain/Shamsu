"""Registry dataclasses for category templates, manifests, and DoD gates."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Category(str, Enum):
    MULTIPLAYER_GAME = "multiplayer-game"
    GAME_2D = "game-2d"
    PORTFOLIO_SITE = "portfolio-site"
    MULTI_TENANT_ADMIN = "multi-tenant-admin"
    ECOMMERCE = "ecommerce"
    GENERAL_WEB = "general-web"
    WEB_CRUD = "web-crud"
    REST_API = "rest-api"


@dataclass(frozen=True)
class Hole:
    id: str
    target_file: str
    marker: str
    kind: str
    description: str
    signature: str | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Manifest:
    category: Category
    stack: dict[str, str]
    entry: str
    build_cmd: str
    run_cmd: str
    preview_url: str
    holes: list[Hole] = field(default_factory=list)


@dataclass(frozen=True)
class DoDItem:
    id: str
    description: str
    check: str
    args: dict
    severity: str = "required"


@dataclass(frozen=True)
class DefinitionOfDone:
    category: Category
    items: list[DoDItem] = field(default_factory=list)


@dataclass(frozen=True)
class RegistryEntry:
    category: Category
    root: Path
    master_prompt: str
    manifest: Manifest
    dod: DefinitionOfDone
