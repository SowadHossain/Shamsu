"""Run control: registration, cancellation, pause/resume, feedback, limits.

The tests that matter most here are the cancellation ones. v1 shipped a full
control plane -- register_run, cancel_run, add_feedback, in-flight model
cancellation -- that the live loop never imported, so `cancel_run` could be
called and nothing happened. These assert the opposite property: that a
cancellation request actually reaches running work.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from shamsu.interfaces.cancellation import Cancelled, FeedbackInterrupt
from shamsu.interfaces.enums import AgentState, RunStatus
from shamsu.interfaces.ids import ProjectId, RunId, TaskId
from shamsu.runtime import (
    EventKind,
    ExecutionLimits,
    LimitExceeded,
    RunController,
    RunToken,
    UnknownRun,
)
from shamsu.state import ProjectRecord, StateStore, TaskRecord


@pytest.fixture
def store() -> StateStore:
    store = StateStore(":memory:")
    store.upsert_project(
        ProjectRecord(project_id=ProjectId("p1"), root="/workspace/demo", name="demo")
    )
    store.create_task(
        TaskRecord(task_id=TaskId("t1"), project_id=ProjectId("p1"), request="do a thing")
    )
    return store


@pytest.fixture
def controller(store: StateStore) -> RunController:
    return RunController(store)


@pytest.fixture
def run_id(controller: RunController) -> RunId:
    identifier = controller.register(ProjectId("p1"), TaskId("t1"))
    controller.start(identifier)
    return identifier


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


class TestRunToken:
    def test_starts_uncancelled(self) -> None:
        token = RunToken()
        assert token.cancelled is False
        assert token.reason is None
        token.raise_if_cancelled()

    def test_request_is_observed(self) -> None:
        token = RunToken()
        token.request("user interrupt")
        assert token.cancelled is True
        assert token.reason == "user interrupt"
        with pytest.raises(Cancelled, match="user interrupt"):
            token.raise_if_cancelled()

    def test_the_first_reason_wins(self) -> None:
        """A later cancel must not overwrite what the user will be told."""
        token = RunToken()
        token.request("first")
        token.request("second")
        assert token.reason == "first"

    def test_is_cancellable_from_another_thread(self) -> None:
        """A tool in a worker thread must be able to poll without a loop."""
        token = RunToken()
        threading.Thread(target=lambda: token.request("from a worker")).start()

        for _ in range(1000):
            if token.cancelled:
                break
            threading.Event().wait(0.001)

        assert token.cancelled is True
        assert token.reason == "from a worker"

    def test_await_resolves_when_cancelled(self) -> None:
        async def scenario() -> str:
            token = RunToken()
            waiter = asyncio.create_task(token.wait_cancelled())
            await asyncio.sleep(0)
            token.request("stop now")
            return await waiter

        assert asyncio.run(scenario()) == "stop now"

    def test_a_cancel_before_the_first_await_is_not_lost(self) -> None:
        """The token may be cancelled before anything awaits it."""

        async def scenario() -> str:
            token = RunToken()
            token.request("early")
            return await asyncio.wait_for(token.wait_cancelled(), timeout=1.0)

        assert asyncio.run(scenario()) == "early"

    def test_cancellation_lands_mid_call_not_only_between_calls(self) -> None:
        """The property v1 lacked.

        A long call must be abandonable while it is running, not merely
        checkable once it finishes.
        """

        async def scenario() -> str:
            token = RunToken()

            async def long_call() -> str:
                await asyncio.sleep(30)
                return "finished"

            call = asyncio.create_task(long_call())
            watch = asyncio.create_task(token.wait_cancelled())

            loop = asyncio.get_running_loop()
            loop.call_later(0.01, token.request, "interrupted")

            done, pending = await asyncio.wait([call, watch], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            assert call not in done, "the long call should not have completed"
            return watch.result()

        assert asyncio.run(scenario()) == "interrupted"


class TestFeedback:
    def test_feedback_is_queued_and_drained(self) -> None:
        token = RunToken()
        token.submit_feedback("also update the README")
        assert token.has_feedback is True
        assert token.take_feedback() == ["also update the README"]
        assert token.has_feedback is False

    def test_feedback_raises_a_distinct_type(self) -> None:
        """Feedback interrupts a call; it does not stop the run."""
        token = RunToken()
        token.submit_feedback("use bcrypt")
        with pytest.raises(FeedbackInterrupt) as excinfo:
            token.raise_if_interrupted()
        assert excinfo.value.feedback == "use bcrypt"
        assert token.cancelled is False

    def test_cancellation_takes_priority_over_feedback(self) -> None:
        """If the user did both, they wanted it to stop."""
        token = RunToken()
        token.submit_feedback("try this instead")
        token.request("stop")
        with pytest.raises(Cancelled):
            token.raise_if_interrupted()


# ---------------------------------------------------------------------------
# Registration and status
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_a_registered_run_is_persisted_before_work_starts(
        self, controller: RunController, store: StateStore
    ) -> None:
        """A crash must leave an observable record, not a run that never existed."""
        run_id = controller.register(ProjectId("p1"), TaskId("t1"))
        assert store.get_run(run_id) is not None
        assert controller.status(run_id) is RunStatus.PENDING

    def test_start_moves_to_running(self, controller: RunController) -> None:
        run_id = controller.register(ProjectId("p1"), TaskId("t1"))
        controller.start(run_id)
        assert controller.status(run_id) is RunStatus.RUNNING

    def test_active_runs_are_listable(self, controller: RunController, run_id: RunId) -> None:
        """A run that cannot be listed cannot be cancelled."""
        assert run_id in controller.active()

    def test_completed_runs_leave_the_active_list(
        self, controller: RunController, run_id: RunId
    ) -> None:
        controller.complete(run_id)
        assert controller.status(run_id) is RunStatus.COMPLETED
        assert run_id not in controller.active()

    def test_unknown_runs_raise(self, controller: RunController) -> None:
        with pytest.raises(UnknownRun):
            controller.token(RunId("nope"))

    def test_a_finished_run_cannot_change_status(
        self, controller: RunController, run_id: RunId
    ) -> None:
        from shamsu.runtime import RunAlreadyFinished

        controller.complete(run_id)
        with pytest.raises(RunAlreadyFinished):
            controller.fail(run_id, "too late")


# ---------------------------------------------------------------------------
# Cancellation through the controller
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_reaches_the_token(self, controller: RunController, run_id: RunId) -> None:
        """The end-to-end property. In v1 this call did nothing."""
        controller.cancel(run_id, "user pressed ctrl-c")
        token = controller.token(run_id)
        assert token.cancelled is True
        assert token.reason == "user pressed ctrl-c"

    def test_cancelling_moves_to_cancelling_not_cancelled(
        self, controller: RunController, run_id: RunId
    ) -> None:
        """Requested is not the same as acknowledged.

        Reporting CANCELLED before the run has stopped would claim a stop that
        has not happened.
        """
        controller.cancel(run_id)
        assert controller.status(run_id) is RunStatus.CANCELLING

    def test_finish_cancelled_records_the_actual_stop(
        self, controller: RunController, run_id: RunId
    ) -> None:
        controller.cancel(run_id, "user interrupt")
        controller.finish_cancelled(run_id)
        assert controller.status(run_id) is RunStatus.CANCELLED
        assert run_id not in controller.active()

    def test_cancel_is_idempotent(self, controller: RunController, run_id: RunId) -> None:
        controller.cancel(run_id, "first")
        controller.cancel(run_id, "second")
        assert controller.token(run_id).reason == "first"

    def test_cancel_reason_is_persisted(
        self, controller: RunController, store: StateStore, run_id: RunId
    ) -> None:
        controller.cancel(run_id, "out of scope")
        controller.finish_cancelled(run_id)
        record = store.get_run(run_id)
        assert record is not None
        assert record.cancel_reason == "out of scope"
        assert record.ended_at is not None

    def test_checkpoint_raises_once_cancelled(
        self, controller: RunController, run_id: RunId
    ) -> None:
        controller.cancel(run_id)
        with pytest.raises(Cancelled):
            asyncio.run(controller.checkpoint(run_id))

    def test_checkpoint_passes_while_running(
        self, controller: RunController, run_id: RunId
    ) -> None:
        asyncio.run(controller.checkpoint(run_id))

    def test_cancel_from_another_thread(self, controller: RunController, run_id: RunId) -> None:
        """Signal handlers and UI threads must be able to stop a run.

        Asserts the *whole* call succeeds, not just that the token flipped.
        Cancelling writes run status, and SQLite connections are thread-bound
        by default -- an earlier version of this raised ProgrammingError after
        setting the token, leaving the token cancelled but the database still
        claiming RUNNING.
        """
        errors: list[BaseException] = []

        def cancel() -> None:
            try:
                controller.cancel(run_id, "from a thread")
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
                errors.append(exc)

        thread = threading.Thread(target=cancel)
        thread.start()
        thread.join()

        assert errors == [], f"cancel() failed off-thread: {errors}"
        assert controller.token(run_id).cancelled is True
        assert controller.status(run_id) is RunStatus.CANCELLING

    def test_the_store_is_writable_from_another_thread(self, store: StateStore) -> None:
        """The store backs cross-thread cancellation, so it must not be thread-bound."""
        errors: list[BaseException] = []

        def write() -> None:
            try:
                store.upsert_project(
                    ProjectRecord(project_id=ProjectId("p2"), root="/w/other", name="other")
                )
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised
                errors.append(exc)

        thread = threading.Thread(target=write)
        thread.start()
        thread.join()

        assert errors == []
        assert store.get_project(ProjectId("p2")) is not None

    def test_pause_does_not_defeat_cancellation_from_another_thread(
        self, controller: RunController, run_id: RunId
    ) -> None:
        """A paused run cancelled from a UI thread must still be released."""

        async def scenario() -> None:
            controller.pause(run_id)
            waiter = asyncio.create_task(controller.wait_if_paused(run_id))
            await asyncio.sleep(0.01)

            thread = threading.Thread(target=controller.cancel, args=(run_id, "off-thread"))
            thread.start()
            thread.join()

            with pytest.raises(Cancelled, match="off-thread"):
                await asyncio.wait_for(waiter, timeout=2.0)

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Pause and resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    def test_pause_then_resume(self, controller: RunController, run_id: RunId) -> None:
        controller.pause(run_id)
        assert controller.is_paused(run_id) is True
        assert controller.status(run_id) is RunStatus.PAUSED

        controller.resume(run_id)
        assert controller.is_paused(run_id) is False
        assert controller.status(run_id) is RunStatus.RUNNING

    def test_a_paused_run_blocks_at_the_gate(
        self, controller: RunController, run_id: RunId
    ) -> None:
        async def scenario() -> bool:
            controller.pause(run_id)
            waiter = asyncio.create_task(controller.wait_if_paused(run_id))
            await asyncio.sleep(0.01)
            still_waiting = not waiter.done()

            controller.resume(run_id)
            await asyncio.wait_for(waiter, timeout=1.0)
            return still_waiting

        assert asyncio.run(scenario()) is True

    def test_a_running_run_does_not_block(self, controller: RunController, run_id: RunId) -> None:
        asyncio.run(asyncio.wait_for(controller.wait_if_paused(run_id), timeout=1.0))

    def test_cancelling_a_paused_run_releases_it(
        self, controller: RunController, run_id: RunId
    ) -> None:
        """A paused run must not sit in the gate forever after being cancelled."""

        async def scenario() -> None:
            controller.pause(run_id)
            waiter = asyncio.create_task(controller.wait_if_paused(run_id))
            await asyncio.sleep(0.01)
            controller.cancel(run_id, "stop while paused")
            with pytest.raises(Cancelled):
                await asyncio.wait_for(waiter, timeout=1.0)

        asyncio.run(scenario())

    def test_pause_is_idempotent(self, controller: RunController, run_id: RunId) -> None:
        controller.pause(run_id)
        controller.pause(run_id)
        assert controller.is_paused(run_id) is True

    def test_resuming_a_running_run_is_a_no_op(
        self, controller: RunController, run_id: RunId
    ) -> None:
        controller.resume(run_id)
        assert controller.status(run_id) is RunStatus.RUNNING


# ---------------------------------------------------------------------------
# Feedback injection
# ---------------------------------------------------------------------------


class TestFeedbackInjection:
    def test_feedback_reaches_the_run(self, controller: RunController, run_id: RunId) -> None:
        controller.submit_feedback(run_id, "prefer argon2")
        assert controller.take_feedback(run_id) == ["prefer argon2"]

    def test_feedback_does_not_cancel(self, controller: RunController, run_id: RunId) -> None:
        controller.submit_feedback(run_id, "also add a test")
        assert controller.token(run_id).cancelled is False
        assert controller.status(run_id) is RunStatus.RUNNING

    def test_feedback_is_recorded_as_an_event(
        self, controller: RunController, run_id: RunId
    ) -> None:
        controller.submit_feedback(run_id, "use the existing helper")
        kinds = [event.kind for event in controller.events(run_id)]
        assert EventKind.FEEDBACK_RECEIVED in kinds


# ---------------------------------------------------------------------------
# Wall clock
# ---------------------------------------------------------------------------


class TestWallClock:
    def test_within_budget_passes(self, controller: RunController, run_id: RunId) -> None:
        controller.check_wall_clock(run_id)
        assert controller.remaining_seconds(run_id) > 0

    def test_an_exhausted_budget_stops_the_run(self, store: StateStore) -> None:
        controller = RunController(store, ExecutionLimits(wall_clock_seconds=0.01))
        run_id = controller.register(ProjectId("p1"), TaskId("t1"))
        controller.start(run_id)

        async def scenario() -> None:
            await asyncio.sleep(0.05)
            with pytest.raises(Cancelled, match="wall-clock"):
                controller.check_wall_clock(run_id)

        asyncio.run(scenario())
        assert controller.status(run_id) is RunStatus.TIMED_OUT

    def test_timeout_is_distinct_from_failure(self, store: StateStore) -> None:
        """An exhausted run is stopped, not failed. Reports must not conflate them."""
        controller = RunController(store, ExecutionLimits(wall_clock_seconds=0.01))
        run_id = controller.register(ProjectId("p1"), TaskId("t1"))
        controller.start(run_id)

        async def scenario() -> None:
            await asyncio.sleep(0.05)
            with pytest.raises(Cancelled):
                controller.check_wall_clock(run_id)

        asyncio.run(scenario())
        assert controller.status(run_id) is not RunStatus.FAILED
        assert controller.status(run_id) is RunStatus.TIMED_OUT


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestExecutionLimits:
    def test_defaults_match_the_plan(self) -> None:
        """Plan section 11's initial table. Drift here changes agent behaviour.

        `actions_per_step` was raised from the plan's 4 to 8 on measurement,
        which is what §34 requires of a higher ceiling: a step needing
        file_changed, git_diff_reviewed and tests_passed cannot be finished in
        four calls once locating the code is counted, and a live run proved it
        by stopping one call short of the tests.
        """
        limits = ExecutionLimits()
        assert limits.actions_per_step == 8
        assert limits.repair_attempts_per_step == 2
        assert limits.replans_per_task == 2
        assert limits.consecutive_failed_actions == 3
        assert limits.mutating_calls_per_decision == 1
        assert limits.logical_actions_per_turn == 1

    def test_long_running_is_off_by_default(self) -> None:
        """Stays disabled until evaluations justify it (plan section 34.13)."""
        assert ExecutionLimits().long_running_enabled is False

    def test_automatic_production_actions_are_off_by_default(self) -> None:
        assert ExecutionLimits().automatic_production_actions is False

    def test_limits_are_immutable(self) -> None:
        from pydantic import ValidationError

        limits = ExecutionLimits()
        with pytest.raises(ValidationError):
            limits.actions_per_step = 100  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("method", "count", "name"),
        [
            ("check_actions", 8, "actions_per_step"),
            ("check_repairs", 2, "repair_attempts_per_step"),
            ("check_replans", 2, "replans_per_task"),
            ("check_consecutive_failures", 3, "consecutive_failed_actions"),
        ],
    )
    def test_bounds_raise_at_the_limit(self, method: str, count: int, name: str) -> None:
        limits = ExecutionLimits()
        with pytest.raises(LimitExceeded) as excinfo:
            getattr(limits, method)(count)
        assert excinfo.value.limit == name

    def test_bounds_pass_below_the_limit(self) -> None:
        limits = ExecutionLimits()
        limits.check_actions(3)
        limits.check_repairs(1)
        limits.check_replans(1)
        limits.check_consecutive_failures(2)

    def test_exhausted_reports_without_raising(self) -> None:
        limits = ExecutionLimits()
        assert limits.exhausted(actions=7) is False
        assert limits.exhausted(actions=8) is True
        assert limits.exhausted(repairs=2) is True
        assert limits.exhausted(replans=2) is True


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_lifecycle_is_recorded_in_order(self, controller: RunController, run_id: RunId) -> None:
        controller.record_state(run_id, AgentState.INSPECT_PROJECT)
        controller.complete(run_id)

        kinds = [event.kind for event in controller.events(run_id)]
        assert kinds == [
            EventKind.REGISTERED,
            EventKind.STARTED,
            EventKind.STATE_CHANGED,
            EventKind.COMPLETED,
        ]

    def test_state_changes_carry_the_state(self, controller: RunController, run_id: RunId) -> None:
        controller.record_state(run_id, AgentState.EXECUTE_CURRENT_STEP)
        event = controller.events(run_id)[-1]
        assert event.state is AgentState.EXECUTE_CURRENT_STEP

    def test_cancellation_leaves_a_trail(self, controller: RunController, run_id: RunId) -> None:
        controller.cancel(run_id, "user interrupt")
        controller.finish_cancelled(run_id)
        kinds = [event.kind for event in controller.events(run_id)]
        assert EventKind.CANCEL_REQUESTED in kinds
        assert EventKind.CANCELLED in kinds

    def test_events_render_as_log_lines(self, controller: RunController, run_id: RunId) -> None:
        controller.note(run_id, EventKind.TOOL_INVOKED, "file.read tests/test_auth.py")
        assert "file.read tests/test_auth.py" in controller.events(run_id)[-1].render()
