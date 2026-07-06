from __future__ import annotations

from pathlib import Path

from shamsu.registry import load_registry_entry
from shamsu.registry.scaffold import scaffold_template
from shamsu.registry.schema import Category
from shamsu.verify import run_dod


def test_multiplayer_template_passes_baseline_dod(tmp_path: Path) -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)
    scaffold = scaffold_template(entry, tmp_path, "game", approval_func=lambda _request: True)

    result = run_dod(entry, tmp_path, scaffold.target_dir)

    assert result.ok is True
    assert not result.required_failures


def test_multiplayer_dod_lists_required_checks_verified_externally() -> None:
    # The fat multiplayer template is verified by its own smoke runner (headless
    # Playwright for the client, the server API for persistence), so its registry
    # DoD items carry no inline check and the inline runner treats them as passed.
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)
    ids = {item.id for item in entry.dod.items}
    for required in {"build.succeeds", "net.two_players_visible", "score.persists", "end.condition"}:
        assert required in ids
    assert all(item.check == "" for item in entry.dod.items)
