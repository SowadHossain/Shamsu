"""Category signals and compatibility mapping."""
from __future__ import annotations

from shamsu.registry.schema import Category
from shamsu.types import Archetype

SIGNALS: dict[Category, list[tuple[str, int]]] = {
    Category.MULTIPLAYER_GAME: [
        ("multiplayer", 3),
        ("game", 2),
        ("3d", 2),
        ("player", 2),
        ("lobby", 3),
        ("real-time", 2),
        ("realtime", 2),
        ("pvp", 3),
        ("co-op", 2),
        ("runner", 1),
        ("arena", 2),
    ],
    Category.ECOMMERCE: [
        ("shop", 3),
        ("cart", 3),
        ("checkout", 3),
        ("product", 2),
        ("catalog", 2),
        ("payment", 2),
        ("store", 2),
        ("inventory", 1),
    ],
    Category.MULTI_TENANT_ADMIN: [
        ("tenant", 3),
        ("multi-tenant", 3),
        ("rbac", 3),
        ("role", 2),
        ("admin", 2),
        ("dashboard", 1),
        ("organization", 2),
        ("permission", 2),
        ("saas", 2),
    ],
    Category.PORTFOLIO_SITE: [
        ("portfolio", 3),
        ("resume", 2),
        ("cv", 2),
        ("gallery", 2),
        ("showcase", 2),
        ("personal site", 3),
        ("about me", 2),
        ("projects page", 2),
    ],
}

ARCHETYPE_TO_CATEGORY = {
    Archetype.REALTIME_3D_GAME: Category.MULTIPLAYER_GAME,
    Archetype.GENERIC_WEB: Category.GENERAL_WEB,
    Archetype.WEB_CRUD: Category.WEB_CRUD,
    Archetype.REST_API: Category.REST_API,
    Archetype.SAAS_FULLSTACK: Category.MULTI_TENANT_ADMIN,
}

CATEGORY_TO_ARCHETYPE = {
    Category.MULTIPLAYER_GAME: Archetype.REALTIME_3D_GAME,
    Category.GENERAL_WEB: Archetype.GENERIC_WEB,
    Category.WEB_CRUD: Archetype.WEB_CRUD,
    Category.REST_API: Archetype.REST_API,
    Category.MULTI_TENANT_ADMIN: Archetype.SAAS_FULLSTACK,
    Category.ECOMMERCE: Archetype.GENERIC_WEB,
    Category.PORTFOLIO_SITE: Archetype.GENERIC_WEB,
}
