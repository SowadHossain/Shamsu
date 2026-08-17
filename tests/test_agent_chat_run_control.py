from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shamsu.agents.chat_loop import (
    AgentChatLoop,
    TIMEOUT_STEP,
    _chat_keep_alive_for_mode,
    _timeout_config_for_mode,
)
from shamsu.runtime.run_control import (
    active_run_ids,
    add_feedback,
    cancel_run,
    complete_run,
    get_run_events,
    get_run_status,
    register_run,
)
from shamsu.runtime.task_state import (
    ExecutionPlan,
    PlanStep,
    PlanStepStatus,
    RuntimeStateStore,
    StepState,
)
from shamsu.runtime.failures import FailureType
from shamsu.runtime.timeouts import TimeoutCategory
from shamsu.types import RunStatus, TaskStepStatus


def _final_response(content: str = "done") -> dict:
    return {"message": {"content": content, "tool_calls": []}}


class NoPlanLLM:
    pass


class ImmediateClient:
    def __init__(self, content: str = "done") -> None:
        self.content = content
        self.calls = 0
        self.kwargs_seen: list[dict] = []

    async def chat(self, **kwargs):
        self.calls += 1
        self.kwargs_seen.append(dict(kwargs))
        return _final_response(self.content)


class SlowClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        self.started.set()
        await asyncio.sleep(60)
        return _final_response("too late")


class FeedbackClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.calls = 0
        self.messages_seen: list[list[dict]] = []

    async def chat(self, **kwargs):
        self.calls += 1
        self.messages_seen.append(list(kwargs["messages"]))
        if self.calls == 1:
            self.started.set()
            await asyncio.sleep(60)
        return _final_response("feedback handled")


class TokenIdleClient:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        if kwargs.get("stream"):
            return self._stream()
        return _final_response("non-stream")

    async def _stream(self):
        yield {"message": {"content": "started "}}
        await asyncio.sleep(60)


def _loop(
    tmp_path: Path,
    client,
    *,
    run_id: str,
    max_runtime_seconds: float = 30,
) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=client,
        llm=NoPlanLLM(),
        use_planner=False,
        use_long_term_memory=False,
        hydrate_history=False,
        run_id=run_id,
        max_runtime_seconds=max_runtime_seconds,
    )


def test_long_running_timeout_config_uses_real_defaults(monkeypatch):
    for name in (
        "SHAMSU_TASK_TIMEOUT_SECONDS",
        "SHAMSU_RUN_TIMEOUT_SECONDS",
        "SHAMSU_FIRST_TOKEN_TIMEOUT_SECONDS",
        "SHAMSU_MODEL_TIMEOUT_SECONDS",
        "SHAMSU_MIN_MODEL_CALL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    normal = _timeout_config_for_mode(False)
    long_running = _timeout_config_for_mode(True)

    # Interactive task budget covers one atomic task AND its verification. Once
    # verification became per-file it can run a real test command, and at 300s a
    # turn could write correctly then time out before proving it - reported as a
    # failure, with the evidence lost.
    assert normal.task_timeout == 600.0
    assert normal.first_token_timeout == 180.0
    # Long-running keeps the opposite trade: a bigger task budget, but each
    # individual call fails fast because there are many of them.
    assert long_running.task_timeout == 900.0
    assert long_running.first_token_timeout == 90.0
    assert long_running.min_model_call_seconds == 60.0
    assert _chat_keep_alive_for_mode(False) == "10m"
    assert _chat_keep_alive_for_mode(True) == "30m"


@pytest.mark.asyncio
async def test_chat_calls_keep_model_loaded(tmp_path: Path):
    client = ImmediateClient("ready")
    loop = _loop(tmp_path, client, run_id="keep-alive")

    result = await loop.run("answer")

    assert result.final == "ready"
    assert client.kwargs_seen
    assert client.kwargs_seen[0]["keep_alive"] == "10m"


@pytest.mark.asyncio
async def test_production_chat_run_registers_run_id_and_events(tmp_path: Path):
    client = SlowClient()
    loop = _loop(tmp_path, client, run_id="chat-start")

    task = asyncio.create_task(loop.run("start"))
    await client.started.wait()

    status = get_run_status("chat-start")
    assert status is not None
    assert status["run_id"] == "chat-start"
    assert status["status"] == RunStatus.RUNNING.value
    assert status["active"] is True
    assert status["current_model_task_active"] is True
    assert any(event["type"] == "run_created" for event in get_run_events("chat-start"))

    assert cancel_run("chat-start") is True
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_cancel_before_model_generation_stops_without_chat_call(tmp_path: Path):
    client = ImmediateClient()
    loop = _loop(tmp_path, client, run_id="cancel-before")

    task = asyncio.create_task(loop.run("start"))
    await asyncio.sleep(0)
    assert cancel_run("cancel-before") is True
    result = await asyncio.wait_for(task, timeout=5)

    assert result.status == RunStatus.CANCELLED
    assert client.calls == 0
    status = get_run_status("cancel-before")
    assert status is not None
    assert status["status"] == RunStatus.CANCELLED.value
    assert status["active"] is False


@pytest.mark.asyncio
async def test_cancel_during_model_generation_cancels_active_task(tmp_path: Path):
    client = SlowClient()
    loop = _loop(tmp_path, client, run_id="cancel-during")

    task = asyncio.create_task(loop.run("start"))
    await client.started.wait()
    assert cancel_run("cancel-during") is True
    result = await asyncio.wait_for(task, timeout=5)

    assert result.status == RunStatus.CANCELLED
    assert "cancelled" in result.final.lower()
    status = get_run_status("cancel-during")
    assert status is not None
    assert status["status"] == RunStatus.CANCELLED.value
    assert status["current_model_task_active"] is False


@pytest.mark.asyncio
async def test_feedback_during_generation_interrupts_and_injects_next_turn(tmp_path: Path):
    client = FeedbackClient()
    loop = _loop(tmp_path, client, run_id="feedback-during")

    task = asyncio.create_task(loop.run("start"))
    await client.started.wait()
    assert add_feedback("feedback-during", "please run tests first") is True
    result = await asyncio.wait_for(task, timeout=5)

    assert result.status == RunStatus.COMPLETED
    assert client.calls == 2
    user_messages = [m["content"] for m in client.messages_seen[-1] if m.get("role") == "user"]
    assert any("please run tests first" in content for content in user_messages)
    assert any(event["type"] == "feedback_injected" for event in get_run_events("feedback-during"))


@pytest.mark.asyncio
async def test_normal_completion_status_and_cleanup(tmp_path: Path):
    client = ImmediateClient("all done")
    loop = _loop(tmp_path, client, run_id="normal-complete")

    result = await loop.run("answer")

    assert result.run_id == "normal-complete"
    assert result.status == RunStatus.COMPLETED
    assert result.final == "all done"
    status = get_run_status("normal-complete")
    assert status is not None
    assert status["status"] == RunStatus.COMPLETED.value
    assert status["active"] is False
    assert "normal-complete" not in active_run_ids()
    task = RuntimeStateStore(tmp_path).require_task(result.task_id)
    assert task.run_id == "normal-complete"
    assert task.status == RunStatus.COMPLETED
    assert task.user_request == "answer"
    assert task.last_checkpoint.startswith("before_final_completion:")


@pytest.mark.asyncio
async def test_wall_clock_timeout_status_and_cleanup(tmp_path: Path):
    client = SlowClient()
    loop = _loop(tmp_path, client, run_id="timeout-run", max_runtime_seconds=0.01)

    result = await loop.run("start")

    assert result.status == RunStatus.TIMED_OUT
    assert result.timeout_category is not None
    status = get_run_status("timeout-run")
    assert status is not None
    assert status["status"] == RunStatus.TIMED_OUT.value
    assert status["active"] is False
    assert "timeout-run" not in active_run_ids()


@pytest.mark.asyncio
async def test_first_token_timeout_records_specific_failure(tmp_path: Path):
    client = SlowClient()
    loop = _loop(tmp_path, client, run_id="first-token-timeout", max_runtime_seconds=30)
    loop.timeout_config = replace(loop.timeout_config, first_token_timeout=0.01, task_timeout=30)

    result = await loop.run("start")

    assert result.status == RunStatus.TIMED_OUT
    assert result.timeout_category == TimeoutCategory.FIRST_TOKEN_TIMEOUT.value
    failures = RuntimeStateStore(tmp_path).list_failures(result.task_id, FailureType.FIRST_TOKEN_TIMEOUT)
    assert failures
    assert failures[0].evidence == [TimeoutCategory.FIRST_TOKEN_TIMEOUT.value]


@pytest.mark.asyncio
async def test_token_idle_timeout_records_specific_failure(tmp_path: Path):
    client = TokenIdleClient()
    loop = _loop(tmp_path, client, run_id="token-idle-timeout", max_runtime_seconds=30)
    loop.timeout_config = replace(loop.timeout_config, token_idle_timeout=0.01, task_timeout=30)

    result = await loop.run("start")

    assert result.status == RunStatus.TIMED_OUT
    assert result.timeout_category == TimeoutCategory.TOKEN_IDLE_TIMEOUT.value
    failures = RuntimeStateStore(tmp_path).list_failures(result.task_id, FailureType.TOKEN_IDLE_TIMEOUT)
    assert failures
    first_token_events = [
        event for event in get_run_events("token-idle-timeout")
        if event["type"] == "model_first_token"
    ]
    assert first_token_events
    assert first_token_events[0]["elapsed_seconds"] >= 0
    finished_events = [
        event for event in get_run_events("token-idle-timeout")
        if event["type"] == "model_stream_finished"
    ]
    assert not finished_events


def test_active_plan_step_timeout_records_specific_failure(tmp_path: Path):
    loop = _loop(tmp_path, ImmediateClient(), run_id="step-timeout", max_runtime_seconds=30)
    loop.timeout_config = replace(loop.timeout_config, step_timeout=1)
    control = register_run("step-timeout", max_runtime_seconds=30)
    try:
        loop._initialize_runtime_task("start", control)
        store = RuntimeStateStore(tmp_path)
        store.save_execution_plan(
            ExecutionPlan(
                plan_id="plan-step-timeout",
                task_id=loop.runtime_task_id,
                run_id=loop.run_id,
                title="Timeout test",
                summary="A plan with an active step.",
                steps=[
                    PlanStep(
                        step_id="step-1",
                        title="Slow step",
                        goal="Prove step timeout status.",
                        acceptance_criteria=["step either finishes or times out"],
                        status=PlanStepStatus.ACTIVE,
                    )
                ],
            ),
            valid_tool_names=set(),
        )
        step = StepState(
            step_id="step-1",
            task_id=loop.runtime_task_id,
            run_id=loop.run_id,
            status=TaskStepStatus.RUNNING,
            started_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        )
        store.record_step(step)
        task = store.require_task(loop.runtime_task_id)
        task.current_step_id = "step-1"
        store.save_task(task, checkpoint_kind="test_active_step")

        assert loop._active_step_timed_out() is True
        loop._mark_active_step_timed_out()
        result = loop._timeout_result(control, 0, TIMEOUT_STEP, seconds=1)

        assert result.status == RunStatus.TIMED_OUT
        assert result.timeout_category == TimeoutCategory.STEP_TIMEOUT.value
        assert store.load_step(loop.runtime_task_id, "step-1").status == TaskStepStatus.FAILED
        failures = store.list_failures(loop.runtime_task_id, FailureType.STEP_TIMEOUT)
        assert failures
    finally:
        complete_run("step-timeout", RunStatus.TIMED_OUT, "test cleanup")


@pytest.mark.asyncio
async def test_cancel_one_run_does_not_cancel_another(tmp_path: Path):
    first = SlowClient()
    second = SlowClient()
    first_task = asyncio.create_task(_loop(tmp_path, first, run_id="run-one").run("one"))
    second_task = asyncio.create_task(_loop(tmp_path, second, run_id="run-two").run("two"))
    await first.started.wait()
    await second.started.wait()

    assert cancel_run("run-one") is True
    first_result = await asyncio.wait_for(first_task, timeout=5)

    assert first_result.status == RunStatus.CANCELLED
    second_status = get_run_status("run-two")
    assert second_status is not None
    assert second_status["status"] == RunStatus.RUNNING.value
    assert second_status["cancel_requested"] is False

    assert cancel_run("run-two") is True
    second_result = await asyncio.wait_for(second_task, timeout=5)
    assert second_result.status == RunStatus.CANCELLED
