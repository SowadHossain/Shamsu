"""Phase 2: 2D game scaffold hole-fill + scaffold pipeline (verify + repair)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from shamsu.agents.scaffold_filler import ScaffoldFiller, find_hole_region
from shamsu.agents.scaffold_pipeline import ScaffoldPipeline, ScaffoldRunResult
from shamsu.prd.contract import extract_contract
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.registry import load_registry_entry
from shamsu.registry.suitability import GenerationStrategy

PONG_PRD = """# Pong

## Overview
A local 2D Pong for two players on one keyboard. No networking.

## Mechanics
- Two paddles and a ball; first to 11 wins.

## Controls
- Left paddle: W and S. Right paddle: Arrow up and down.
"""


def _hole_id_from_prompt(prompt: str) -> str:
    match = re.search(r"## Hole to fill: (\S+)", prompt)
    return match.group(1) if match else "unknown"


def _fake_fill_generate(system: str, user: str, schema: dict) -> str:
    hole_id = _hole_id_from_prompt(user)
    return '{"code": "/* filled:%s */"}' % hole_id


# --- marker region ------------------------------------------------------------

def test_find_hole_region_locates_body_between_markers():
    text = "a\n// HOLE:x\nplaceholder1\nplaceholder2\n// END:x\nb\n"
    region = find_hole_region(text, "// HOLE:x")
    assert region is not None
    start, end = region
    assert text[start:end] == "placeholder1\nplaceholder2\n"


def test_find_hole_region_none_when_no_end_marker():
    text = "// HOLE:x\nplaceholder\n"
    assert find_hole_region(text, "// HOLE:x") is None


# --- ScaffoldFiller against the real game-2d template -------------------------

def _scaffold_game_2d(tmp_path: Path):
    entry = load_registry_entry("game-2d")
    from shamsu.registry.scaffold import scaffold_template
    scaffold = scaffold_template(
        entry, tmp_path, tmp_path / "game", approval_func=lambda _r: True
    )
    return entry, scaffold.target_dir


def test_scaffold_filler_replaces_placeholder_bodies(tmp_path: Path):
    entry, target = _scaffold_game_2d(tmp_path)
    contract = extract_contract(parse_prd_text(PONG_PRD, markdown=True))

    result = ScaffoldFiller(tmp_path, _fake_fill_generate).fill(entry, target, contract)

    # Every hole in the manifest was filled.
    assert set(result.filled) == {"entity", "input", "update", "score", "win", "render"}
    assert result.skipped == []

    update_ts = (target / "src/game/update.ts").read_text()
    # Placeholder body is gone; the fill and the markers/exports remain.
    assert "/* filled:update */" in update_ts
    assert "bounce a box" not in update_ts
    assert "// HOLE:update" in update_ts and "// END:update" in update_ts
    assert "export function update" in update_ts
    assert "export function scorePoints" in update_ts

    state_ts = (target / "src/game/state.ts").read_text()
    assert "/* filled:entity */" in state_ts
    assert "export function createState" in state_ts


def test_scaffold_filler_skips_holes_it_cannot_fill(tmp_path: Path):
    entry, target = _scaffold_game_2d(tmp_path)
    contract = extract_contract(parse_prd_text(PONG_PRD, markdown=True))
    # Model returns nothing -> every hole is skipped, no file changes.
    before = (target / "src/game/update.ts").read_text()
    result = ScaffoldFiller(tmp_path, lambda s, u, sc: "").fill(entry, target, contract)
    assert result.filled == []
    assert set(result.skipped) == {"entity", "input", "update", "score", "win", "render"}
    assert (target / "src/game/update.ts").read_text() == before


# --- ScaffoldPipeline: fill + verify + DoD -----------------------------------

class FakeRunner:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.commands: list[str] = []

    def run(self, command: str, cwd) -> tuple[int, str, str]:
        self.commands.append(command)
        return (self._exit_code, "", "" if self._exit_code == 0 else "build error")


def test_scaffold_pipeline_pong_builds_and_succeeds(tmp_path: Path):
    project = build_project_spec(parse_prd_text(PONG_PRD, markdown=True))
    assert project.suitability.strategy is GenerationStrategy.SCAFFOLD

    pipeline = ScaffoldPipeline(
        tmp_path,
        generate=_fake_fill_generate,
        command_runner=FakeRunner(0),
        approval_func=lambda _r: True,
    )
    result = pipeline.run(project, tmp_path / "game")

    assert result.candidate == "game-2d"
    assert result.success is True
    assert result.exit_code == 0
    assert result.fill_result is not None and result.fill_result.filled
    # A repair loop ran and reported only on verifier ground truth.
    assert "passed" in result.final_message.lower()
    assert (result.target_dir / "src/game/update.ts").read_text().count("/* filled:update */") == 1


def test_scaffold_pipeline_reports_failure_honestly_on_bad_build(tmp_path: Path):
    project = build_project_spec(parse_prd_text(PONG_PRD, markdown=True))
    # Build never passes and the model proposes no repair -> honest failure.
    pipeline = ScaffoldPipeline(
        tmp_path,
        generate=lambda s, u, sc: "",   # no fills, no repair plans
        command_runner=FakeRunner(2),
        approval_func=lambda _r: True,
        max_repair_attempts=2,
    )
    result = pipeline.run(project, tmp_path / "game")
    assert result.success is False
    assert result.exit_code != 0
    lowered = result.final_message.lower()
    assert "passed" not in lowered
    assert "fixed" not in lowered


# --- full pipeline routing ----------------------------------------------------

class _DummySearch:
    def search(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
async def test_full_pipeline_routes_pong_to_2d_scaffold(tmp_path: Path, monkeypatch):
    from shamsu.agents import scaffold_pipeline as sp_mod
    from shamsu.agents.full_pipeline import FullDjangoPipeline

    # Template scaffolds are opt-in now (disabled by default); this test exercises
    # the enabled path, so turn them on explicitly.
    monkeypatch.setenv("SHAMSU_ENABLE_TEMPLATES", "1")

    prd = tmp_path / "pong.md"
    prd.write_text(PONG_PRD)
    captured: dict = {}

    def fake_run(self, project, target_dir):
        captured["candidate"] = project.suitability.candidate
        captured["strategy"] = project.suitability.strategy
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        return ScaffoldRunResult(
            target_dir=target,
            candidate=project.suitability.candidate,
            success=True,
            exit_code=0,
            final_message="Verifier passed (exit code 0).",
            preview_url="http://localhost:5173",
        )

    monkeypatch.setattr(sp_mod.ScaffoldPipeline, "run", fake_run)

    result = await FullDjangoPipeline(
        tmp_path, search=_DummySearch(), approval_func=lambda _r: True
    ).run(prd, tmp_path / "game")

    assert captured["candidate"] == "game-2d"
    assert result.success is True
    assert result.preview_url == "http://localhost:5173"


@pytest.mark.asyncio
async def test_templates_disabled_routes_scaffold_prd_to_freeform(tmp_path: Path, monkeypatch):
    """With templates disabled (the default), a scaffold-eligible PRD (a 2D game)
    is built from scratch via the freeform generator, never the copy-paste
    scaffold. assess() still reports SCAFFOLD; the pipeline reroutes it."""
    from shamsu.agents import freeform_generator as ff_mod
    from shamsu.agents.freeform_generator import FreeformRunResult
    from shamsu.agents.full_pipeline import FullDjangoPipeline

    monkeypatch.delenv("SHAMSU_ENABLE_TEMPLATES", raising=False)

    prd = tmp_path / "pong.md"
    prd.write_text(PONG_PRD)

    # assess() is pure: it still ranks the 2D scaffold as the best fit.
    project = build_project_spec(parse_prd_text(PONG_PRD))
    assert project.suitability.strategy is GenerationStrategy.SCAFFOLD

    captured: dict = {}

    def fake_run(self, project, target_dir):
        captured["ran_freeform"] = True
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        return FreeformRunResult(
            target_dir=target, stack="node", written_files=["index.html"],
            verified=True, success=True, exit_code=0,
            final_message="Verifier passed (exit code 0).",
        )

    monkeypatch.setattr(ff_mod.FreeformGenerator, "run", fake_run)

    result = await FullDjangoPipeline(
        tmp_path, search=_DummySearch(), approval_func=lambda _r: True,
        generate=lambda s, u, sc: "",
    ).run(prd, tmp_path / "game")

    assert captured.get("ran_freeform") is True
    assert result.success is True
    assert result.written_files == ["index.html"]
