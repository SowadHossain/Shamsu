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


def test_missing_lobby_markup_blocks_dod(tmp_path: Path) -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)
    scaffold = scaffold_template(entry, tmp_path, "game", approval_func=lambda _request: True)
    app = scaffold.target_dir / "src" / "App.tsx"
    app.write_text(app.read_text(encoding="utf-8").replace('data-testid="player-list"', ""), encoding="utf-8")

    result = run_dod(entry, tmp_path, scaffold.target_dir)

    assert result.ok is False
    assert [failure.item_id for failure in result.required_failures] == ["lobby.renders"]


def test_single_player_output_blocks_dod(tmp_path: Path) -> None:
    entry = load_registry_entry(Category.MULTIPLAYER_GAME)
    scaffold = scaffold_template(entry, tmp_path, "game", approval_func=lambda _request: True)
    app = scaffold.target_dir / "src" / "App.tsx"
    app.write_text(app.read_text(encoding="utf-8").replace('testId="remote-player"', 'testId="spectator"'), encoding="utf-8")

    result = run_dod(entry, tmp_path, scaffold.target_dir)

    assert result.ok is False
    assert [failure.item_id for failure in result.required_failures] == ["net.two_players_visible"]
