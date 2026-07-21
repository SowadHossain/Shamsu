"""Definition-of-Done runner."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.registry.schema import DoDItem, RegistryEntry
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.tools.executor import CommandRunner
from shamsu.verify.checks import CHECKS


@dataclass(frozen=True)
class DoDCheckResult:
    item_id: str
    description: str
    passed: bool
    severity: str
    detail: str
    suggested_fix_context: str = ""
    # False when the item has no inline check and could only be confirmed by an
    # external smoke runner: it is NOT passed (so it can't be mistaken for PRD
    # completion) and NOT counted as a hard failure (we simply did not verify it).
    verified: bool = True


@dataclass(frozen=True)
class DoDRunResult:
    category: str
    results: list[DoDCheckResult] = field(default_factory=list)

    @property
    def required_failures(self) -> list[DoDCheckResult]:
        return [
            result for result in self.results
            if not result.passed and result.severity == "required" and result.verified
        ]

    @property
    def unverified(self) -> list[DoDCheckResult]:
        return [result for result in self.results if not result.verified]

    @property
    def ok(self) -> bool:
        return not self.required_failures


def run_dod(
    entry: RegistryEntry,
    workspace: Path,
    target_dir: Path | str,
    command_runner: CommandRunner | None = None,
    session_logger: SessionLogger | None = None,
) -> DoDRunResult:
    sandbox = Sandbox(Path(workspace))
    target = sandbox.validate(target_dir)
    results: list[DoDCheckResult] = []
    runner = command_runner or CommandRunner(workspace, approval_func=lambda _request: True)
    for item in entry.dod.items:
        passed, detail, verified = _run_item(item, target, runner)
        result = DoDCheckResult(
            item_id=item.id,
            description=item.description,
            passed=passed,
            severity=item.severity,
            detail=detail,
            suggested_fix_context=_suggestion(item, detail),
            verified=verified,
        )
        results.append(result)
        if session_logger:
            session_logger.log(
                "dod.item",
                {
                    "category": entry.category.value,
                    "item_id": item.id,
                    "passed": passed,
                    "severity": item.severity,
                    "detail": detail,
                },
                f"DoD {item.id}: {'passed' if passed else 'failed'}",
                workflow_id="dod",
            )
    run = DoDRunResult(category=entry.category.value, results=results)
    if session_logger:
        session_logger.log(
            "dod.finished",
            {
                "category": entry.category.value,
                "ok": run.ok,
                "required_failures": [failure.item_id for failure in run.required_failures],
            },
            "Definition of Done finished",
            workflow_id="dod",
        )
    return run


def dod_failures(result: DoDRunResult) -> list[DoDCheckResult]:
    return result.required_failures


def _run_item(item: DoDItem, target: Path, runner: CommandRunner) -> tuple[bool, str, bool]:
    if not item.check:
        # No inline check: only the template's external smoke runner can confirm
        # this. Report it as UNVERIFIED (not passed) so a green scaffold is never
        # mistaken for PRD completion, but do not hard-fail on it either.
        return False, "not verified inline (needs the template smoke runner)", False
    fn = CHECKS.get(item.check)
    if fn is None:
        return False, f"Unknown DoD check: {item.check}", True
    try:
        passed, detail = fn(target, runner=runner, **item.args)
        return passed, detail, True
    except Exception as exc:
        return False, f"{item.check} failed: {exc}", True


def _suggestion(item: DoDItem, detail: str) -> str:
    return f"Fix required DoD item {item.id}: {item.description}. Current detail: {detail}"
