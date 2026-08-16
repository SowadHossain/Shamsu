from __future__ import annotations

from pathlib import Path

from shamsu.agents.chat_loop import AGENT_SYSTEM_PROMPT, AgentChatLoop, _system_prompt
from shamsu.agents.prompting import PromptProfile, prompt_profile_for_model
from shamsu.runtime.phase_contracts import ExecutionPhase
from shamsu.runtime.task_state import ExecutionPlan, PlanStep, PlanStepStatus, RuntimeStateStore
from shamsu.tools.agent_tools import AgentToolRegistry


class NoPlanLLM:
    pass


def test_small_prompt_is_fragmented_strict_and_shorter_than_legacy_prompt():
    step = PlanStep(
        step_id="hash-passwords",
        title="Implement password hashing",
        goal="Hash passwords before storage.",
        allowed_tools=["code.search", "file.read", "file.patch", "test.run"],
        acceptance_criteria=["passwords are hashed before persistence"],
        required_evidence=["file_changed", "test_passed"],
        status=PlanStepStatus.ACTIVE,
    )

    prompt = _system_prompt(
        Path("/ws"),
        profile=PromptProfile.SMALL,
        phase=ExecutionPhase.AUTHOR,
        current_step=step,
        available_tools=step.allowed_tools,
    )

    for section in (
        "[BASE_RULES]",
        "[PHASE_RULES]",
        "[CURRENT_STEP]",
        "[TOOL_PROTOCOL]",
        "[OUTPUT_SCHEMA]",
        "[FAILURE_RECOVERY_RULES]",
    ):
        assert section in prompt
    assert "Current phase: AUTHOR" in prompt
    assert "- Implement the active step only." in prompt
    assert "- Choose exactly one action for this response." in prompt
    assert "- Do not claim the task or step is complete in this phase." in prompt
    assert "- file.patch" in prompt
    assert len(prompt) < len(AGENT_SYSTEM_PROMPT)


def test_verify_phase_prompt_blocks_source_modification():
    prompt = _system_prompt(Path("/ws"), profile=PromptProfile.SMALL, phase=ExecutionPhase.VERIFY)

    assert "Source modification is blocked." in prompt
    assert "Report only verification evidence." in prompt


def test_standard_profile_is_available_without_family_specific_prompt_sprawl():
    prompt = _system_prompt(Path("/ws"), profile=PromptProfile.STANDARD, phase=ExecutionPhase.EXPLORE)

    assert "[BASE_RULES]" in prompt
    assert "Briefly summarize observed facts" in prompt
    assert prompt_profile_for_model("custom:large", explicit="standard") == PromptProfile.STANDARD
    assert prompt_profile_for_model("qwen2.5-coder:3b-instruct") == PromptProfile.SMALL


def test_agent_refreshes_prompt_from_persistent_phase_and_active_step(tmp_path: Path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(
        tmp_path,
        tools=tools,
        llm=NoPlanLLM(),
        use_planner=False,
        use_long_term_memory=False,
        hydrate_history=False,
        run_id="prompt-runtime",
    )
    store = RuntimeStateStore(tmp_path)
    store.create_run(loop.run_id)
    task = store.create_task(run_id=loop.run_id, task_id=loop.runtime_task_id, user_request="hash passwords")
    task.current_phase = ExecutionPhase.AUTHOR.value
    store.save_task(task, checkpoint_kind="test_prompt")
    store.save_execution_plan(
        ExecutionPlan(
            plan_id="plan-prompt",
            task_id=loop.runtime_task_id,
            run_id=loop.run_id,
            title="Prompt plan",
            summary="One authoring step.",
            steps=[
                PlanStep(
                    step_id="step-1",
                    title="Implement password hashing",
                    goal="Hash passwords.",
                    allowed_tools=["file.read", "file.patch", "test.run"],
                    acceptance_criteria=["passwords are hashed"],
                    required_evidence=["file_changed"],
                    status=PlanStepStatus.ACTIVE,
                )
            ],
        ),
        valid_tool_names={"file.read", "file.patch", "test.run"},
    )
    task = store.require_task(loop.runtime_task_id)
    task.current_phase = ExecutionPhase.AUTHOR.value
    store.save_task(task, checkpoint_kind="test_author_phase")
    tools.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")

    loop._refresh_system_prompt()

    assert "Current phase: AUTHOR" in loop.state.system_prompt
    assert "Implement password hashing" in loop.state.system_prompt
    assert "- file.patch" in loop.state.system_prompt
