"""Phase 3: DoD no longer auto-passes; soft PRD checklist."""
from __future__ import annotations

from pathlib import Path

from shamsu.registry.schema import (
    Category,
    DefinitionOfDone,
    DoDItem,
    Manifest,
    RegistryEntry,
)
from shamsu.prd.contract import PRDContract
from shamsu.verify import build_prd_checklist, run_dod


def _entry(items: list[DoDItem], root: Path) -> RegistryEntry:
    return RegistryEntry(
        category=Category.GAME_2D,
        root=root,
        master_prompt="",
        manifest=Manifest(
            category=Category.GAME_2D, stack={}, entry="", build_cmd="",
            run_cmd="", preview_url="", holes=[],
        ),
        dod=DefinitionOfDone(category=Category.GAME_2D, items=items),
    )


def test_no_check_item_is_unverified_not_passed(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / "main.py").write_text("print('hi')\n")
    entry = _entry(
        [
            DoDItem(id="entry.exists", description="", check="file_exists",
                    args={"path": "main.py"}, severity="required"),
            DoDItem(id="smoke.only", description="", check="", args={}, severity="required"),
        ],
        tmp_path,
    )
    run = run_dod(entry, tmp_path, target)

    by_id = {r.item_id: r for r in run.results}
    assert by_id["entry.exists"].passed is True and by_id["entry.exists"].verified is True
    # The old behavior auto-passed this; now it is unverified, not passed.
    assert by_id["smoke.only"].passed is False
    assert by_id["smoke.only"].verified is False
    # Unverified items are neither a pass nor a hard failure.
    assert run.required_failures == []
    assert run.ok is True
    assert [r.item_id for r in run.unverified] == ["smoke.only"]


def test_verified_required_failure_blocks_dod(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    entry = _entry(
        [DoDItem(id="entry.exists", description="", check="file_exists",
                 args={"path": "missing.py"}, severity="required")],
        tmp_path,
    )
    run = run_dod(entry, tmp_path, target)
    assert run.ok is False
    assert [r.item_id for r in run.required_failures] == ["entry.exists"]


def test_prd_checklist_flags_found_and_missing(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / "game.ts").write_text(
        "// ball bounces off the top and bottom walls\nfunction bounce() {}\n"
    )
    contract = PRDContract(
        acceptance_criteria=[
            "The ball bounces off the top and bottom walls",
            "A leaderboard persists high scores to a remote database",
        ]
    )
    checklist = build_prd_checklist(contract, target)
    found = {item.requirement: item.found_in_code for item in checklist}
    assert found["The ball bounces off the top and bottom walls"] is True
    assert found["A leaderboard persists high scores to a remote database"] is False
