from __future__ import annotations

from pathlib import Path
import json

import pytest

from shamsu.registry import detect_category, load_registry_entry, stack_policy_for
from shamsu.registry.scaffold import scaffold_template
from shamsu.registry.schema import Category
from shamsu.safety.sandbox import SecurityError


@pytest.mark.parametrize(
    ("prompt", "category"),
    [
        ("Build a multiplayer 3D cube runner with a lobby", Category.MULTIPLAYER_GAME),
        ("Create a personal portfolio with a gallery and projects page", Category.PORTFOLIO_SITE),
        ("Make a store with product catalog, cart, and checkout", Category.ECOMMERCE),
        ("Build a multi-tenant admin app with roles and permissions", Category.MULTI_TENANT_ADMIN),
    ],
)
def test_detector_routes_category_fixtures(prompt: str, category: Category) -> None:
    decision = detect_category(prompt)

    assert decision.category == category
    assert decision.confidence > 0.45


def test_detector_falls_back_to_general_web_for_ambiguous_prompt() -> None:
    decision = detect_category("simple bakery landing page")

    assert decision.category == Category.GENERAL_WEB
    assert decision.reason.startswith("ambiguous")


def test_loads_multiplayer_registry_entry() -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)

    assert entry.category == Category.MULTIPLAYER_GAME
    assert "multiplayer" in entry.master_prompt.lower()
    assert entry.manifest.stack["net"] == "colyseus-relay"
    assert entry.manifest.stack["physics"] == "rapier"
    assert entry.manifest.entry == "client/src/main.tsx"
    assert {hole.id for hole in entry.manifest.holes} >= {"entity.player", "rule.update"}
    assert {item.id for item in entry.dod.items} >= {"menu.renders", "net.two_players_visible"}


def test_multiplayer_stack_policy_is_forced_into_template() -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)
    policy = stack_policy_for(Category.MULTIPLAYER_GAME)
    client = json.loads((entry.root / "template" / "client" / "package.json").read_text(encoding="utf-8"))
    server = json.loads((entry.root / "template" / "server" / "package.json").read_text(encoding="utf-8"))
    client_deps = {**client.get("dependencies", {}), **client.get("devDependencies", {})}
    server_deps = {**server.get("dependencies", {}), **server.get("devDependencies", {})}

    assert "Colyseus" in policy.backend
    assert "@dimforge/rapier3d-compat" in policy.required_packages
    assert "colyseus" in server_deps
    assert "better-sqlite3" in server_deps
    assert "colyseus.js" in client_deps
    assert "@dimforge/rapier3d-compat" in client_deps


def test_scaffold_template_copies_inside_workspace(tmp_path: Path) -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)

    result = scaffold_template(entry, tmp_path, "game", approval_func=lambda _request: True)

    assert (tmp_path / "game" / "package.json").exists()
    assert (tmp_path / "game" / "client" / "src" / "App.tsx").exists()
    assert (tmp_path / "game" / "server" / "src" / "db.ts").exists()
    assert "package.json" in result.copied_files


def test_scaffold_template_rejects_path_escape(tmp_path: Path) -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)

    with pytest.raises(SecurityError):
        scaffold_template(entry, tmp_path, tmp_path.parent / "escape")
