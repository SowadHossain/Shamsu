"""An unverifiable milestone is reported honestly, not failed.

"No deterministic verifier exists for this kind of change" is a gap in the
gate, not a defect in the work. Failing the milestone for it also blocked every
dependent, so one uncheckable behavioural milestone ended the whole build.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.cli import repl
from shamsu.verify.gate import VerifyOutcome


def _quiet_console():
    from rich.console import Console

    return Console(quiet=True, no_color=True)


def _preflight(mandatory: bool) -> dict:
    return {
        "requirements": [
            {
                "id": "R1",
                "kind": "feature",
                "text": "Members can borrow a copy",
                "scope": "in",
                "priority": "must" if mandatory else "should",
            }
        ],
        "dependencies": [],
    }


def _verify(monkeypatch, outcome: VerifyOutcome, workspace: Path | None = None):
    monkeypatch.setattr(repl, "verify_only", lambda *a, **k: outcome)
    monkeypatch.setattr(repl, "_log_prd_milestone_verification", lambda *a, **k: None)
    # Evidence validation is a separate gate with its own tests; this module is
    # about what an unverifiable verdict means.
    monkeypatch.setattr(repl, "_prd_requirement_evidence_errors", lambda *a, **k: [])
    if workspace is not None:
        target = workspace / "core"
        target.mkdir(parents=True, exist_ok=True)
        (target / "views.py").write_text(
            "def book_list(request):\n    pass\n", encoding="utf-8"
        )


class TestUnverifiableIsNotFailure:
    @pytest.mark.parametrize("mandatory", [True, False])
    def test_unverifiable_milestone_is_implemented_not_failed(
        self, tmp_path: Path, monkeypatch, mandatory: bool
    ):
        _verify(
            monkeypatch,
            VerifyOutcome(
                verified=False,
                unverifiable=True,
                summary="No deterministic verifier is available for these changes (UNVERIFIED).",
            ),
            tmp_path,
        )
        status, _ = asyncio.run(
            repl._verify_prd_milestone(
                "M-002",
                _preflight(mandatory),
                ["core/views.py"],
                tmp_path,
                _quiet_console(),
                None,
            )
        )
        assert status == "implemented"

    def test_a_real_verification_failure_still_fails(self, tmp_path: Path, monkeypatch):
        _verify(
            monkeypatch,
            VerifyOutcome(
                verified=False,
                unverifiable=False,
                exit_code=1,
                command="python -m py_compile core/views.py",
                summary="SyntaxError",
            ),
            tmp_path,
        )
        status, _ = asyncio.run(
            repl._verify_prd_milestone(
                "M-002",
                _preflight(True),
                ["core/views.py"],
                tmp_path,
                _quiet_console(),
                None,
            )
        )
        assert status == "failed"

    def test_a_verified_milestone_is_still_verified(self, tmp_path: Path, monkeypatch):
        _verify(
            monkeypatch,
            VerifyOutcome(
                verified=True, exit_code=0, command="manage.py check", summary="ok"
            ),
            tmp_path,
        )
        status, _ = asyncio.run(
            repl._verify_prd_milestone(
                "M-002",
                _preflight(True),
                ["core/views.py"],
                tmp_path,
                _quiet_console(),
                None,
            )
        )
        assert status == "verified"


class TestDependentsAreNotBlocked:
    def test_implemented_milestones_do_not_block_dependents(self):
        # Only failed/skipped milestones block; "implemented" must not appear
        # in either, which is what lets the build continue past M-002.
        blocking = repl._prd_blocking_dependencies(
            {"dependencies": ["M-002"]}, failed={}, skipped={}
        )
        assert blocking == []

    def test_failed_dependency_still_blocks(self):
        blocking = repl._prd_blocking_dependencies(
            {"dependencies": ["M-002"]}, failed={"M-002": "boom"}, skipped={}
        )
        assert blocking == ["M-002"]
