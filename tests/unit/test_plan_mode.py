"""Plan mode restricts, rather than describing a restriction.

`/mode plan` used to set `Settings.mode`, `_status` printed it, and the session
frame coloured it — and nothing else ever read it. The runtime built an
authoring gateway either way, so plan mode was a label on a build agent.

Both layers are asserted here, because either alone is a half-measure: tools
without step kinds leaves a change step demanding evidence it has no tool to
produce, and step kinds without tools leaves the capability one prompt away.
"""

from __future__ import annotations

from pathlib import Path

from shamsu.agent.planning import CHANGE_FLOOR, READ_ONLY_TOOLS, effective_kind, materialise
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.ids import TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.tools import ToolGateway, authoring_tools, read_only_tools
from shamsu.ui.repl import Settings


def a_change_step() -> PlanStepProposal:
    """A step that is unambiguously a change: named files and a mutating verb."""
    return PlanStepProposal(
        title="Fix the login handler",
        kind="change",
        files=("auth/views.py",),
        required_evidence=("the tests pass",),
    )


class TestStepKinds:
    def test_plan_mode_forces_investigate(self) -> None:
        assert effective_kind(a_change_step(), read_only=True) == "investigate"

    def test_build_mode_leaves_it_alone(self) -> None:
        assert effective_kind(a_change_step(), read_only=False) == "change"

    def test_a_read_only_step_cannot_patch(self) -> None:
        plan = ImplementationPlan(summary="s", steps=(a_change_step(),))
        built = materialise(TaskId("t"), plan, read_only=True)

        step = built.steps[0]
        assert step.allowed_tools == READ_ONLY_TOOLS
        assert "file.patch" not in step.allowed_tools

    def test_a_read_only_step_carries_no_change_floor(self) -> None:
        """Otherwise plan mode would demand proof of an edit it forbids."""
        plan = ImplementationPlan(summary="s", steps=(a_change_step(),))
        built = materialise(TaskId("t"), plan, read_only=True)
        assert not CHANGE_FLOOR & set(built.steps[0].required_evidence)

    def test_build_mode_still_gates_the_same_step(self) -> None:
        plan = ImplementationPlan(summary="s", steps=(a_change_step(),))
        built = materialise(TaskId("t"), plan, read_only=False)
        assert set(built.steps[0].required_evidence) >= CHANGE_FLOOR


class TestGatewaySurface:
    def test_plan_mode_registers_no_mutating_tool(self, tmp_path: Path) -> None:
        gateway = ToolGateway(read_only_tools(tmp_path))
        for name in ("file.patch", "test.run", "check.run", "git.checkpoint"):
            assert gateway.get(name) is None, name

    def test_build_mode_registers_them(self, tmp_path: Path) -> None:
        gateway = ToolGateway(authoring_tools(tmp_path))
        for name in ("file.patch", "test.run", "check.run", "git.checkpoint"):
            assert gateway.get(name) is not None, name

    def test_nothing_reachable_in_plan_mode_can_write(self, tmp_path: Path) -> None:
        """The property that makes this a restriction and not a request."""
        gateway = ToolGateway(read_only_tools(tmp_path))
        for phase in Phase:
            for contract in gateway.available(phase):
                assert contract.mutating is False, f"{contract.name} in {phase}"

    def test_reading_still_works(self, tmp_path: Path) -> None:
        """A read-only agent that cannot read is not useful."""
        gateway = ToolGateway(read_only_tools(tmp_path))
        names = {contract.name for contract in gateway.available(Phase.INSPECT)}
        assert {"file.read", "file.list", "code.search"} <= names


class TestSettingsThreadItThrough:
    def base(self, **overrides: object) -> Settings:
        return Settings(
            model_name="m",
            host="h",
            workspace=Path("."),
            **overrides,  # type: ignore[arg-type]
        )

    def test_plan_mode_sets_read_only(self) -> None:
        assert self.base(mode="plan").read_only is True

    def test_build_mode_does_not(self) -> None:
        assert self.base(mode="build").read_only is False

    def test_the_flag_survives_into_app_options(self) -> None:
        """The boundary where `mode` used to stop."""
        assert self.base(mode="plan").options("do a thing").read_only is True
        assert self.base(mode="build").options("do a thing").read_only is False
